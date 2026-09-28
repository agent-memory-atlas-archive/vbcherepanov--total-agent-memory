"""Activity reports over team server workspaces: personal (self only), one department, or the whole company.

Reads each workspace read-only through the control plane (memory.db or the workspace's PostgreSQL schema),
like the overview statistics; no workspace process is started.
Personal workspaces are only ever read for their owner and never enter department or company reports.
"""
import re
import sqlite3
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime

from pydantic import JsonValue

from memory_reports.contracts import Report, ReportError
from memory_reports.llm_summary import ReportSummarizer
from memory_reports.render import render_markdown
from memory_reports.repository import ReportRepository
from memory_reports.service import ReportService, Source, utc_now
from team_memory.contracts import (
    OVERSIGHT_ROLES,
    Actor,
    DomainError,
    Forbidden,
    Unavailable,
)
from team_memory.registry import Registry
from team_memory.reports.contracts import TeamReportRequest

SHARED_KEY = "shared"
FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class TeamReportService:
    def __init__(self, registry: Registry, summarizer: ReportSummarizer, clock: Callable[[], datetime] = utc_now):
        self.registry = registry
        self.builder = ReportService(clock)
        self.summarizer = summarizer

    def options(self, actor: Actor) -> dict:
        names = dict(self.registry.list_teams())
        teams = [{"team_id": team_id, "name": name} for team_id, name in sorted(names.items())
                 if self.registry.can_view_team_people(actor, team_id)]
        return {"personal": True, "teams": teams,
                "company": self.registry.org_role(actor.user_id) in OVERSIGHT_ROLES}

    def _targets(self, actor: Actor, request: TeamReportRequest) -> tuple[str, list[tuple[str | None, str]]]:
        """(scope label, [(source label, workspace key)]) after authorization."""
        if request.scope == "personal":
            personal = self.registry.workspaces(actor)[0]
            return "personal:" + actor.user_id, [(None, personal.key)]
        if request.scope == "team":
            if not self.registry.can_view_team_people(actor, request.team_id):
                raise Forbidden("Department reports are for its head, company viewers and superadmins")
            return "team:" + request.team_id, [(None, self.registry.team_workspace_key(request.team_id))]
        if self.registry.org_role(actor.user_id) not in OVERSIGHT_ROLES:
            raise Forbidden("Company reports need the company viewer or superadmin role")
        targets = [("team:" + team_id, self.registry.team_workspace_key(team_id))
                   for team_id, _ in self.registry.list_teams()]
        return "company", [*targets, (SHARED_KEY, SHARED_KEY)]

    def build(self, actor: Actor, request: TeamReportRequest) -> Report:
        label, targets = self._targets(actor, request)
        try:
            with ExitStack() as stack:
                sources = []
                for source_label, key in targets:
                    db = stack.enter_context(self.registry.plane.workspace_reader(key))
                    if db is not None:
                        sources.append(Source(source_label, ReportRepository(db)))
                report = self.builder.build(request.report_request(), sources, scope=label)
        except ReportError as exc:
            raise DomainError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise Unavailable("Workspace statistics are temporarily unavailable") from exc
        return self.summarizer.apply(report) if request.include_llm_summary else report

    def call(self, actor: Actor, request: TeamReportRequest) -> JsonValue:
        report = self.build(actor, request)
        if request.format == "json":
            return {"report": report.model_dump(mode="json")}
        return {"scope": report.scope, "markdown": render_markdown(report)}

    def markdown(self, actor: Actor, request: TeamReportRequest) -> tuple[str, str]:
        """(file name, markdown) for the dashboard download."""
        report = self.build(actor, request)
        window = report.window
        dates = window.first_day if window.first_day == window.last_day else f"{window.first_day}_{window.last_day}"
        name = FILENAME_UNSAFE.sub("-", f"report-{report.scope}-{request.project or 'all'}-{window.kind}-{dates}")
        return name.strip("-") + ".md", render_markdown(report)
