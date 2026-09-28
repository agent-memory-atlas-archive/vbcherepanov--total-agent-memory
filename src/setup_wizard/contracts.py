from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["personal", "company"]
Deploy = Literal["service", "compose", "manual"]
RECORD_VERSION = 1


class WizardError(Exception):
    """A problem the user can fix; printed without a traceback."""


class UsageError(WizardError):
    """A missing or invalid non-interactive flag."""


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Answer(DTO):
    """What one step collected; ``summary`` feeds the review screen."""

    def summary(self) -> list[tuple[str, str]]:
        return []


class PersonalRecord(DTO):
    memory_dir: str
    clients: list[str] = Field(default_factory=list)
    embed_preset: str = "multilingual"
    llm_provider: str = "none"
    llm_settings: dict[str, str] = Field(default_factory=dict)
    llm_key_set: bool = False
    hooks: bool = False
    skills: bool = False


class CompanyRecord(DTO):
    data_dir: str
    host: str
    port: int
    public_url: str
    deploy: Deploy
    company_name: str | None = None
    admin_id: str | None = None
    departments: list[str] = Field(default_factory=list)
    llm_provider: str | None = None
    embed_provider: str | None = None
    backup_replica: str | None = None


class SetupRecord(DTO):
    """The first-run marker (``setup.json``). Never holds secrets."""

    version: int = RECORD_VERSION
    mode: Mode
    source: Literal["wizard", "upgrade", "installer"] = "wizard"
    completed_at: str
    tam_version: str
    personal: PersonalRecord | None = None
    company: CompanyRecord | None = None
