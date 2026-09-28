"""The `memory_report` MCP tool: validate arguments, build, render, optionally summarize and save."""
import json
import sqlite3
from pathlib import Path

from pydantic import Field

from memory_reports.contracts import OutputFormat, Report, ReportRequest
from memory_reports.llm_summary import ReportSummarizer
from memory_reports.render import render_markdown
from memory_reports.repository import ReportRepository
from memory_reports.service import ReportService, Source
from memory_reports.storage import save_markdown

NAME = "memory_report"
DESCRIPTION = (
    "Activity report for a project (or all projects) over a period: today (period=day), this week (week, ISO "
    "Monday-Sunday), this month (month), all time (all) or custom since/until dates; offset=-1 gives the previous "
    "day/week/month ('last week'). Sections: summary numbers with deltas against the previous equal period, key "
    "decisions with their WHY, solutions and fixes, errors with recurring patterns and lessons, open next steps and "
    "pitfalls from session summaries, most touched files, entities/technologies, and a day-by-day timeline. Every "
    "item carries source IDs (#id -> memory_get). Built deterministically from stored records, no LLM; "
    "include_llm_summary=true adds an optional paragraph from the configured LLM. Periods use the local timezone "
    "(or tz). save=true writes the Markdown to <memory dir>/reports/<project>/<period>-<date>.md. Use it when the "
    "user asks what happened, for a status/progress report, a weekly summary or a retrospective."
)


class ToolArguments(ReportRequest):
    format: OutputFormat = Field(default="markdown", description="markdown (readable) or json (structured)")
    include_llm_summary: bool = Field(default=False, description="Add an LLM-written paragraph (needs a configured LLM)")
    save: bool = Field(default=False, description="Also write the Markdown under <memory dir>/reports/")

    def request(self) -> ReportRequest:
        return ReportRequest.model_validate(self.model_dump(exclude={"format", "include_llm_summary", "save"}))


FIELD_HELP = {
    "project": "Project name; omit for all projects",
    "period": "day = today, week = this ISO week, month = this calendar month, all = since the first record, "
              "custom = since/until",
    "since": "custom only: first day YYYY-MM-DD (inclusive) or an ISO date-time",
    "until": "custom only: last day YYYY-MM-DD (inclusive) or an ISO date-time (exclusive); default today",
    "offset": "day/week/month only: 0 = current, -1 = previous (yesterday, last week, last month)",
    "tz": "IANA timezone for period boundaries, e.g. Europe/Berlin; default: MEMORY_REPORT_TZ, TZ or the system zone",
    "limit": "Maximum items per section",
}


def input_schema() -> dict:
    schema = ToolArguments.model_json_schema()
    for name, text in FIELD_HELP.items():
        schema["properties"][name]["description"] = text
    schema.pop("title", None)
    schema["additionalProperties"] = False
    return schema


def build(db: sqlite3.Connection, arguments: ToolArguments, service: ReportService | None = None,
          summarizer: ReportSummarizer | None = None) -> Report:
    report = (service or ReportService()).build(arguments.request(), [Source(None, ReportRepository(db))])
    if arguments.include_llm_summary:
        report = (summarizer or ReportSummarizer()).apply(report)
    return report


def handle(db: sqlite3.Connection, raw: dict, memory_dir: Path, service: ReportService | None = None,
           summarizer: ReportSummarizer | None = None) -> str:
    arguments = ToolArguments.model_validate(raw)
    report = build(db, arguments, service, summarizer)
    markdown = render_markdown(report)
    path = save_markdown(memory_dir, report, markdown) if arguments.save else None
    if arguments.format == "json":
        return json.dumps({"report": report.model_dump(mode="json"), "saved_to": str(path) if path else None},
                          ensure_ascii=False, indent=2)
    return markdown + (f"\n_Saved to: {path}_\n" if path else "")
