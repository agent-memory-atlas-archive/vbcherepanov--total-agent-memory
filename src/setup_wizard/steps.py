"""Step registry. The wizard runs ``STEPS`` in order; add a step by inserting one Step, without touching the others."""
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from setup_wizard.clients import Host
from setup_wizard.contracts import Answer, Mode, SetupRecord
from setup_wizard.prompts import Option, Prompter

AnswerT = TypeVar("AnswerT", bound=Answer)
MODES: frozenset[Mode] = frozenset(("personal", "company"))


@dataclass
class Context:
    prompter: Prompter
    host: Host
    install_root: Path
    memory_dir: Path
    previous: SetupRecord | None
    answers: dict[str, Answer] = field(default_factory=dict)
    mode: Mode | None = None
    base: SetupRecord | None = None
    preset_mode: Mode | None = None

    def has_personal(self) -> bool:
        return self.base is not None and self.base.personal is not None

    def answer(self, step_id: str, kind: type[AnswerT]) -> AnswerT | None:
        value = self.answers.get(step_id)
        return value if isinstance(value, kind) else None


@dataclass
class Action:
    """One unit of the apply phase: ``prepare`` validates without side effects, ``commit`` writes."""

    label: str
    commit: Callable[[], list[str]]
    prepare: Callable[[], None] = lambda: None


@dataclass
class Plan:
    env: dict[str, str] = field(default_factory=dict)
    secret_env: set[str] = field(default_factory=set)
    actions: list[Action] = field(default_factory=list)
    record: dict[str, object] = field(default_factory=dict)
    next_steps: list[str] = field(default_factory=list)
    result: dict[str, object] = field(default_factory=dict)
    on_success: list[Callable[[], None]] = field(default_factory=list)
    on_failure: list[Callable[[], None]] = field(default_factory=list)
    internal: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    applies_to: frozenset[Mode]
    run: Callable[[Context], Answer]
    contribute: Callable[[Context, Plan, Answer], None] | None = None
    when: Callable[[Context], bool] = lambda _ctx: True


class ModeAnswer(Answer):
    mode: Mode

    def summary(self) -> list[tuple[str, str]]:
        return [("Mode", "Just me" if self.mode == "personal" else "Company server")]


MODE_OPTIONS = (
    Option("personal", "Just me", "personal memory on this machine, for your own AI clients"),
    Option("company", "Company server", "one shared memory server that your teams connect to"),
)


ADD_COMPANY = Option("company", "Add a company server",
                     "a team server next to your personal memory, which stays as it is")


def ask_mode(ctx: Context) -> ModeAnswer:
    if ctx.preset_mode is not None:
        ctx.prompter.say("  " + {o.id: o.label for o in MODE_OPTIONS}[ctx.preset_mode] + " (from --mode)")
        return ModeAnswer(mode=ctx.preset_mode)
    default = ctx.previous.mode if ctx.previous else "personal"
    options = (MODE_OPTIONS[0], ADD_COMPANY) if ctx.has_personal() and ctx.base.company is None else MODE_OPTIONS
    return ModeAnswer(mode=ctx.prompter.choose("mode", "How will you use total-agent-memory?", options, default))


def registry() -> list[Step]:
    from setup_wizard import company, personal
    return [Step("mode", "Choose how you use it", MODES, ask_mode), *personal.STEPS, *company.STEPS]


def active(steps: list[Step], ctx: Context) -> list[Step]:
    return [step for step in steps if ctx.mode is None or ctx.mode in step.applies_to]
