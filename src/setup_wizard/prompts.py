import getpass
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TextIO

from setup_wizard.contracts import UsageError

Validator = Callable[[str], str | None]


@dataclass(frozen=True)
class Option:
    id: str
    label: str
    hint: str = ""


class Prompter(Protocol):
    interactive: bool

    def section(self, number: int, total: int, title: str) -> None: ...

    def say(self, text: str = "") -> None: ...

    def choose(self, key: str, question: str, options: Sequence[Option], default: str | None) -> str: ...

    def choose_many(self, key: str, question: str, options: Sequence[Option], defaults: Sequence[str]) -> list[str]: ...

    def text(self, key: str, question: str, default: str | None = None, validate: Validator | None = None,
             required: bool = True) -> str | None: ...

    def secret(self, key: str, question: str, required: bool = True, can_keep: bool = False) -> str | None: ...

    def confirm(self, key: str, question: str, default: bool) -> bool: ...


class Style:
    def __init__(self, out: TextIO, environ: Mapping[str, str]):
        self.enabled = hasattr(out, "isatty") and out.isatty() and not environ.get("NO_COLOR")

    def bold(self, text: str) -> str:
        return f"\033[1m{text}\033[0m" if self.enabled else text

    def dim(self, text: str) -> str:
        return f"\033[2m{text}\033[0m" if self.enabled else text

    def accent(self, text: str) -> str:
        return f"\033[36m{text}\033[0m" if self.enabled else text


def _hidden_input(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\n")


class TerminalPrompter:
    interactive = True

    def __init__(self, out: TextIO = sys.stdout, read: Callable[[str], str] = input,
                 read_hidden: Callable[[str], str] = _hidden_input, environ: Mapping[str, str] | None = None):
        self.out, self.read, self.read_hidden = out, read, read_hidden
        self.style = Style(out, os.environ if environ is None else environ)

    def _ask(self, prompt: str, hidden: bool = False) -> str:
        try:
            return (self.read_hidden if hidden else self.read)(prompt).strip()
        except EOFError:
            self.say()
            raise KeyboardInterrupt from None

    def section(self, number: int, total: int, title: str) -> None:
        self.say()
        self.say(self.style.accent(f"Step {number}/{total}" if total else f"Step {number}") + "  " + self.style.bold(title))

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def _options(self, options: Sequence[Option], marks: Sequence[str] | None = None) -> None:
        width = max(len(o.label) for o in options)
        for number, option in enumerate(options, 1):
            mark = (marks[number - 1] + " ") if marks else ""
            hint = ("  " + self.style.dim(option.hint)) if option.hint else ""
            self.say(f"    {mark}{number}) {option.label.ljust(width)}{hint}")

    def choose(self, key: str, question: str, options: Sequence[Option], default: str | None) -> str:
        self.say("  " + question)
        self._options(options)
        ids = [o.id for o in options]
        shown = f" [{ids.index(default) + 1}]" if default in ids else ""
        while True:
            raw = self._ask(f"  Choose{shown}: ")
            if not raw and default in ids:
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return ids[int(raw) - 1]
            if raw in ids:
                return raw
            self.say(f"  Enter a number from 1 to {len(options)}.")

    def choose_many(self, key: str, question: str, options: Sequence[Option], defaults: Sequence[str]) -> list[str]:
        self.say("  " + question)
        ids = [o.id for o in options]
        self._options(options, ["[x]" if o.id in defaults else "[ ]" for o in options])
        shown = ",".join(str(ids.index(d) + 1) for d in defaults if d in ids) or "none"
        while True:
            raw = self._ask(f"  Numbers separated by commas, 'all' or 'none' [{shown}]: ").lower()
            if not raw:
                return [d for d in ids if d in defaults]
            if raw == "all":
                return ids
            if raw == "none":
                return []
            parts = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()]
            if all(p.isdigit() and 1 <= int(p) <= len(ids) for p in parts):
                picked = {ids[int(p) - 1] for p in parts}
                return [i for i in ids if i in picked]
            self.say(f"  Use numbers from 1 to {len(ids)}, for example 1,3.")

    def text(self, key: str, question: str, default: str | None = None, validate: Validator | None = None,
             required: bool = True) -> str | None:
        shown = f" [{default}]" if default else ("" if required else " (optional, Enter to skip)")
        while True:
            raw = self._ask(f"  {question}{shown}: ") or (default or "")
            if not raw:
                if not required:
                    return None
                self.say("  A value is required.")
                continue
            problem = validate(raw) if validate else None
            if problem is None:
                return raw
            self.say("  " + problem)

    def secret(self, key: str, question: str, required: bool = True, can_keep: bool = False) -> str | None:
        suffix = " (hidden; Enter keeps the current key)" if can_keep else (" (hidden)" if required else
                                                                            " (hidden, optional)")
        while True:
            raw = self._ask(f"  {question}{suffix}: ", hidden=True)
            if raw:
                return raw
            if can_keep or not required:
                return None
            self.say("  A key is required for this provider.")

    def confirm(self, key: str, question: str, default: bool) -> bool:
        while True:
            raw = self._ask(f"  {question} [{'Y/n' if default else 'y/N'}]: ").lower()
            if not raw:
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            self.say("  Answer y or n.")


class AnswerPrompter:
    """Answers prompts from command-line flags, for installers and containers."""

    interactive = False

    def __init__(self, answers: Mapping[str, object], flags: Mapping[str, str], out: TextIO = sys.stdout):
        self.answers, self.flags, self.out = answers, flags, out

    def _flag(self, key: str) -> str:
        return self.flags.get(key, key)

    def section(self, number: int, total: int, title: str) -> None:
        self.say(f"[{number}/{total}] {title}" if total else f"[{number}] {title}")

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def choose(self, key: str, question: str, options: Sequence[Option], default: str | None) -> str:
        value = self.answers.get(key)
        ids = [o.id for o in options]
        if value is None:
            if default is None:
                raise UsageError(f"{self._flag(key)} is required ({', '.join(ids)})")
            return default
        if value not in ids:
            raise UsageError(f"{self._flag(key)}: expected one of {', '.join(ids)}")
        return str(value)

    def choose_many(self, key: str, question: str, options: Sequence[Option], defaults: Sequence[str]) -> list[str]:
        value = self.answers.get(key)
        ids = [o.id for o in options]
        if value is None:
            return [i for i in ids if i in defaults]
        chosen = list(value) if isinstance(value, (list, tuple)) else [str(value)]
        unknown = [c for c in chosen if c not in ids]
        if unknown:
            raise UsageError(f"{self._flag(key)}: unknown {', '.join(unknown)}; expected {', '.join(ids)}")
        return [i for i in ids if i in chosen]

    def text(self, key: str, question: str, default: str | None = None, validate: Validator | None = None,
             required: bool = True) -> str | None:
        value = self.answers.get(key)
        raw = str(value).strip() if value is not None else (default or "")
        if not raw:
            if required:
                raise UsageError(f"{self._flag(key)} is required")
            return None
        problem = validate(raw) if validate else None
        if problem is not None:
            raise UsageError(f"{self._flag(key)}: {problem}")
        return raw

    def secret(self, key: str, question: str, required: bool = True, can_keep: bool = False) -> str | None:
        value = self.answers.get(key)
        if value:
            return str(value)
        if required and not can_keep:
            raise UsageError(f"{self._flag(key)} must name an environment variable that holds the key")
        return None

    def confirm(self, key: str, question: str, default: bool) -> bool:
        value = self.answers.get(key)
        return default if value is None else bool(value)
