"""`tam report`: print or write an activity report from the local memory store (read-only)."""
import argparse
import json
import logging
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from memory_reports.contracts import MAX_ITEMS, ReportError
from memory_reports.render import render_markdown
from memory_reports.storage import save_markdown
from memory_reports.tool import ToolArguments, build

LOGGER = logging.getLogger(__name__)
READ_TIMEOUT_SECONDS = 10
EXIT_USAGE = 2
EXIT_FAILURE = 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tam report", description="Activity report for a project and period.")
    p.add_argument("--project", help="project name (default: all projects)")
    p.add_argument("--period", choices=("day", "week", "month", "all", "custom"), default="week")
    p.add_argument("--since", help="custom period start: YYYY-MM-DD or ISO date-time")
    p.add_argument("--until", help="custom period end: YYYY-MM-DD (inclusive) or ISO date-time")
    p.add_argument("--offset", type=int, default=0, help="day/week/month: -1 = previous period")
    p.add_argument("--tz", help="IANA timezone, e.g. Europe/Berlin (default: system zone)")
    p.add_argument("--limit", type=int, default=20, help=f"items per section, 1..{MAX_ITEMS}")
    p.add_argument("--format", choices=("md", "json"), default="md")
    p.add_argument("--out", type=Path, help="write to this file instead of stdout")
    p.add_argument("--save", action="store_true", help="also save the Markdown under <memory dir>/reports/")
    p.add_argument("--llm-summary", action="store_true", help="add a paragraph from the configured LLM")
    p.add_argument("--memory-dir", type=Path, help="memory directory (default: TAM_MEMORY_DIR or ~/.tam)")
    return p


def _memory_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser()
    from paths import memory_dir
    return memory_dir()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        arguments = ToolArguments(project=args.project, period=args.period, since=args.since, until=args.until,
                                  offset=args.offset, tz=args.tz, limit=args.limit,
                                  format="json" if args.format == "json" else "markdown",
                                  include_llm_summary=args.llm_summary, save=args.save)
    except ValidationError as exc:
        sys.stderr.write("tam report: " + "; ".join(e["msg"] for e in exc.errors(include_input=False)) + "\n")
        return EXIT_USAGE
    root = _memory_dir(args.memory_dir)
    database = root / "memory.db"
    if not database.is_file():
        sys.stderr.write(f"tam report: no memory database at {database}\n")
        return EXIT_FAILURE
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=READ_TIMEOUT_SECONDS)) as db:
            report = build(db, arguments)
    except ReportError as exc:
        sys.stderr.write(f"tam report: {exc}\n")
        return EXIT_USAGE
    except sqlite3.Error as exc:
        LOGGER.error(json.dumps({"event": "report_cli_failed", "database": str(database), "error": str(exc)}))
        sys.stderr.write(f"tam report: cannot read {database}: {exc}\n")
        return EXIT_FAILURE
    markdown = render_markdown(report)
    saved = save_markdown(root, report, markdown) if args.save else None
    text = (json.dumps({"report": report.model_dump(mode="json"), "saved_to": str(saved) if saved else None},
                       ensure_ascii=False, indent=2) + "\n") if args.format == "json" else markdown
    if args.out is not None:
        _write(args.out, text)
        sys.stderr.write(f"Report written to {args.out}\n")
    else:
        sys.stdout.write(text)
    if saved is not None:
        sys.stderr.write(f"Saved to {saved}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
