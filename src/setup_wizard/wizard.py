"""Runs the step registry: questions first, then one review, then an apply phase that Ctrl-C cannot split."""
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from setup_wizard import company, personal
from setup_wizard.contracts import (
    CompanyRecord,
    PersonalRecord,
    SetupRecord,
    WizardError,
)
from setup_wizard.files import atomic_write, deferred_interrupts
from setup_wizard.steps import Context, ModeAnswer, Plan, Step, registry
from team_memory.contracts import DomainError
from team_memory.setup import SUPPORT_LINE
from version import VERSION

LOGGER = logging.getLogger(__name__)
CANCELLED = 130
FAILED = 1
VERIFY_FAILED = 2
DECLINED = 3


@dataclass
class Outcome:
    code: int
    record: SetupRecord | None = None
    plan: Plan = field(default_factory=Plan)
    verified: tuple[bool, str] | None = None

    def as_json(self) -> dict:
        return {"ok": self.code == 0, "code": self.code,
                "record": self.record.model_dump(mode="json") if self.record else None,
                "invite": self.plan.result.get("invite"), "next_steps": list(dict.fromkeys(self.plan.next_steps)),
                "verify": {"ok": self.verified[0], "detail": self.verified[1]} if self.verified else None}


def load_record(path: Path) -> SetupRecord | None:
    if not path.is_file():
        return None
    try:
        return SetupRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise WizardError(f"{path} is not a valid setup record ({exc.__class__.__name__}); move it away and "
                          "run `tam setup` again") from exc


def describe(record: SetupRecord) -> list[str]:
    lines = [f"Mode: {'Just me' if record.mode == 'personal' else 'Company server'} (set up {record.completed_at})"]
    if record.personal:
        p = record.personal
        lines += [f"Memory directory: {p.memory_dir}", f"Clients: {', '.join(p.clients) or 'none'}",
                  f"Embeddings: {p.embed_preset}", f"Language model: {p.llm_provider}"]
    if record.company:
        c = record.company
        lines += [f"Data directory: {c.data_dir}", f"Public URL: {c.public_url}", f"Runs as: {c.deploy}"]
        if c.backup_replica:
            lines.append(f"Continuous backup: {c.backup_replica}")
    return lines


class Wizard:
    def __init__(self, ctx: Context, record_path: Path, verify: bool = True, steps: list[Step] | None = None):
        self.ctx, self.record_path, self.verify = ctx, record_path, verify
        self.steps = steps if steps is not None else registry()
        self.say = ctx.prompter.say

    def run(self) -> Outcome:
        try:
            self._ask()
            if not self._review():
                self.say("Nothing was changed.")
                return Outcome(DECLINED)
            plan = self._plan()
            for action in plan.actions:
                action.prepare()
        except KeyboardInterrupt:
            self.say()
            self.say("Setup cancelled. Nothing was changed.")
            return Outcome(CANCELLED)
        done: list[str] = []
        try:
            record = self._apply(plan, done)
        except KeyboardInterrupt:
            self.say("Settings were applied; stopped before the final check.")
            return Outcome(CANCELLED, plan=plan)
        except (OSError, DomainError, ValueError) as exc:
            self.say(f"Setup stopped while applying: {exc}")
            self.say("Already applied: " + (", ".join(done) if done else "nothing") +
                     ". Fix the problem and run `tam setup --reconfigure`.")
            return Outcome(FAILED, plan=plan)
        outcome = Outcome(0, record, plan)
        try:
            outcome.verified = self._verify(plan)
        except KeyboardInterrupt:
            self.say("Check skipped.")
        if outcome.verified and not outcome.verified[0] and self.ctx.mode == "personal":
            outcome.code = VERIFY_FAILED
        self._finish(outcome)
        return outcome

    def _active(self) -> list[Step]:
        return [s for s in self.steps if self.ctx.mode is None or self.ctx.mode in s.applies_to]

    def _ask(self) -> None:
        number = 0
        for position, step in enumerate(self.steps):
            if self.ctx.mode is not None and self.ctx.mode not in step.applies_to:
                continue
            if not step.when(self.ctx):
                continue
            number += 1
            later = [s for s in self.steps[position + 1:] if self.ctx.mode is not None and self.ctx.mode in s.applies_to]
            self.ctx.prompter.section(number, number + len(later) + 1 if self.ctx.mode else 0, step.title)
            answer = step.run(self.ctx)
            self.ctx.answers[step.id] = answer
            if isinstance(answer, ModeAnswer):
                self.ctx.mode = answer.mode
        self._number = number + 1

    def _review(self) -> bool:
        self.ctx.prompter.section(self._number, self._number, "Review")
        rows = [row for step in self._active() if step.id in self.ctx.answers
                for row in self.ctx.answers[step.id].summary()]
        if self.ctx.mode == "personal":
            rows.append(("Memory directory", str(self.ctx.memory_dir)))
        width = max(len(label) for label, _ in rows)
        for label, value in rows:
            self.say(f"    {label.ljust(width)}  {value}")
        self.say()
        return self.ctx.prompter.confirm("apply", "Apply these settings?", True)

    def _plan(self) -> Plan:
        plan = Plan()
        for step in self._active():
            answer = self.ctx.answers.get(step.id)
            if answer is not None and step.contribute is not None:
                step.contribute(self.ctx, plan, answer)
        return plan

    def _record(self, plan: Plan) -> SetupRecord:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        base = self.ctx.base
        if self.ctx.mode == "personal":
            return SetupRecord(mode="personal", completed_at=now, tam_version=VERSION,
                               personal=PersonalRecord(memory_dir=str(self.ctx.memory_dir), **plan.record),
                               company=base.company if base else None)
        return SetupRecord(mode="company", completed_at=now, tam_version=VERSION, company=CompanyRecord(**plan.record),
                           personal=base.personal if base else None)

    def _apply(self, plan: Plan, done: list[str]) -> SetupRecord:
        record = self._record(plan)
        self.say()
        self.say("Applying...")
        with deferred_interrupts():
            try:
                for action in plan.actions:
                    for line in action.commit():
                        self.say("  + " + line)
                    done.append(action.label)
                for publish in plan.on_success:
                    publish()
            except BaseException:
                for undo in plan.on_failure:
                    undo()
                if plan.on_failure:
                    done.clear()
                raise
            atomic_write(self.record_path, record.model_dump_json(indent=2) + "\n")
            LOGGER.info(json.dumps({"event": "setup_applied", "mode": record.mode, "record": str(self.record_path)}))
        return record

    def _verify(self, plan: Plan) -> tuple[bool, str] | None:
        if not self.verify:
            return None
        self.say()
        if self.ctx.mode == "personal":
            self.say("Checking that the memory server starts (throwaway data directory)...")
            ok, detail = personal.verify(self.ctx, plan)
            self.say(("  OK: " if ok else "  Problem: ") + detail)
            return ok, detail
        network = self.ctx.answer("company.network", company.NetworkAnswer)
        if network is None:
            return None
        version = company.health(network.public_url)
        if version:
            detail = f"a server answers at {network.public_url} (version {version})"
        else:
            detail = f"no server answers at {network.public_url} yet; start it as shown below"
        self.say("  " + detail[0].upper() + detail[1:])
        return version is not None, detail

    def _finish(self, outcome: Outcome) -> None:
        plan = outcome.plan
        invite = plan.result.get("invite")
        network = self.ctx.answer("company.network", company.NetworkAnswer)
        if invite:
            self.say()
            self.say("Administrator invite code (shown only now, single use):")
            self.say(f"    {invite['code']}   for {invite['user_id']}, valid until {invite['expires_at']}")
            if network:
                self.say(f"  Open {network.public_url}/dashboard/, choose 'Invite code' and set a password.")
        steps = list(dict.fromkeys(s for s in plan.next_steps if s))
        if self.ctx.mode == "company" and network:
            snippet = json.dumps({"mcpServers": {"total-agent-memory": {"command": "tam-remote", "env": {
                "TAM_REMOTE_URL": network.public_url + "/mcp/",
                "TAM_REMOTE_TOKEN_FILE": "/absolute/path/to/personal.token"}}}}, indent=2)
            steps += [f"Dashboard: {network.public_url}/dashboard/",
                      "Invite people under Administration -> Users. Each person creates a personal token under "
                      "'Tokens & password' and adds this to their MCP client config:\n" + snippet]
        if self.ctx.mode == "personal":
            steps.append(f"Memory is stored in {self.ctx.memory_dir}.")
        elif self.ctx.has_personal():
            steps.append(f"Your personal memory in {self.ctx.base.personal.memory_dir} was not changed. Moving personal "
                         "records into the team server is not automatic; TAM has no migration for that yet.")
        steps.append("Change any of this later with: tam setup --reconfigure")
        self.say()
        self.say("Next steps:")
        for number, text in enumerate(steps, 1):
            first, *rest = text.splitlines()
            self.say(f"  {number}. {first}")
            for line in rest:
                self.say("       " + line)
        if self.ctx.mode == "company":
            self.say()
            self.say(SUPPORT_LINE)
