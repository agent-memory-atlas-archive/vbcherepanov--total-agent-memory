"""Provider questions shared by both modes; fields, labels and validation come from team_memory.settings."""
from collections.abc import Mapping
from typing import Literal

import httpx
from pydantic import Field, SecretStr

from setup_wizard.contracts import Answer, UsageError
from setup_wizard.prompts import Option, Prompter
from team_memory import provider_check
from team_memory.contracts import DomainError
from team_memory.settings import PROVIDERS, SPECS, SettingSpec, validate_value

Target = Literal["llm", "embed"]
NO_LLM = "none"
LLM_OPTIONS = (
    Option(NO_LLM, "No LLM", "fast mode: search and save work without a model; nothing leaves this machine"),
    Option("ollama", "Ollama", "local models on this machine or your network"),
    Option("openai", "OpenAI", "cloud; needs an API key"),
    Option("anthropic", "Anthropic", "cloud; needs an API key"),
    Option("openai-compatible", "OpenAI-compatible", "any Chat Completions server (vLLM, LM Studio, OpenRouter...)"),
)
EMBED_OPTIONS = (
    Option("fastembed", "FastEmbed (local)", "default; runs on the server, no key"),
    Option("openai", "OpenAI embeddings", "cloud; needs an API key"),
    Option("cohere", "Cohere", "cloud; needs an API key"),
    Option("dashscope", "DashScope", "Alibaba Cloud text-embedding-v4; needs an API key"),
)
FIELD_DEFAULTS = {"OLLAMA_URL": "http://localhost:11434"}
REQUIRED_FIELDS = {("llm", "openai-compatible"): ("MEMORY_LLM_API_BASE", "MEMORY_LLM_MODEL")}
KEY_OPTIONAL = {("llm", "ollama"), ("llm", "openai-compatible"), ("embed", "fastembed")}
LLM_SECRET_KEYS = tuple(spec.key for spec in SPECS.values() if spec.group == "llm" and spec.kind == "secret")
LLM_KEYS = tuple(spec.key for spec in SPECS.values() if spec.group == "llm")


class ProviderAnswer(Answer):
    target: Target
    provider: str
    values: dict[str, str] = Field(default_factory=dict)
    secret_name: str | None = None
    secret: SecretStr | None = None
    kept_secret: bool = False

    def summary(self) -> list[tuple[str, str]]:
        options = LLM_OPTIONS if self.target == "llm" else EMBED_OPTIONS
        label = next(o.label for o in options if o.id == self.provider)
        rows = [("Language model" if self.target == "llm" else "Embeddings", label)]
        rows += [(SPECS[key].label, value) for key, value in self.values.items()]
        if self.secret_name:
            rows.append(("API key", "kept" if self.kept_secret else "set (hidden)" if self.secret else "not set"))
        return rows

    def settings(self) -> dict[str, str]:
        """Environment-style values, including the key when one was entered."""
        values = dict(self.values)
        if self.target == "llm":
            values["MEMORY_LLM_ENABLED"] = "false" if self.provider == NO_LLM else "auto"
            if self.provider != NO_LLM:
                values["MEMORY_LLM_PROVIDER"] = self.provider
        else:
            values["MEMORY_EMBED_PROVIDER"] = self.provider
        if self.secret_name and self.secret is not None:
            values[self.secret_name] = self.secret.get_secret_value()
        return values


def problem(spec: SettingSpec, value: str) -> str | None:
    try:
        validate_value(spec, value)
    except DomainError as exc:
        return str(exc)
    return None


def ask_fields(prompter: Prompter, target: Target, provider: str, previous: Mapping[str, str],
               existing_secret: str | None, secret_is_set: bool = False) -> ProviderAnswer:
    """Ask the provider's own fields (as the dashboard's provider cards do); the first key field is hidden input."""
    if target == "llm" and provider == NO_LLM:
        return ProviderAnswer(target=target, provider=provider)
    spec = next(p for p in PROVIDERS if p.target == target and p.id == provider)
    values: dict[str, str] = {}
    secret_name, secret, kept = None, None, False
    for key in spec.fields:
        field = SPECS[key]
        if field.kind == "secret":
            if secret_name is not None:
                continue
            secret_name = key
            can_keep = existing_secret is not None or secret_is_set
            while True:
                raw = prompter.secret(f"{target}.key", field.label, required=(target, provider) not in KEY_OPTIONAL,
                                      can_keep=can_keep)
                if raw is None or problem(field, raw) is None:
                    break
                if not prompter.interactive:
                    raise UsageError(f"{field.label}: the key must be printable text without spaces around it")
                prompter.say("  The key must be printable text; paste it again.")
            if raw is None and can_keep:
                kept = True
                secret = SecretStr(existing_secret) if existing_secret is not None else None
            elif raw is not None:
                secret = SecretStr(raw)
            continue
        required = key in REQUIRED_FIELDS.get((target, provider), ())
        default = previous.get(key) or FIELD_DEFAULTS.get(key)
        raw = prompter.text(f"{target}.{key}", field.label, default, lambda v, f=field: problem(f, v), required)
        if raw:
            values[key] = raw
    return ProviderAnswer(target=target, provider=provider, values=values, secret_name=secret_name, secret=secret,
                          kept_secret=kept)


def test_connection(answer: ProviderAnswer, transport: httpx.BaseTransport | None = None) -> provider_check.CheckResult:
    endpoint = provider_check.resolve(answer.target, answer.settings())
    return provider_check.check(endpoint, transport)
