"""Saved reports: <memory dir>/reports/<project>/<period>-<date>.md, written atomically."""
import os
import re
import tempfile
from pathlib import Path

from memory_reports.contracts import Report

REPORTS_DIR = "reports"
ALL_PROJECTS = "all-projects"
SLUG_CHARS = 100
DIR_MODE = 0o700
FILE_MODE = 0o600


def slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")[:SLUG_CHARS]
    return cleaned or "project"


def report_path(memory_dir: Path, report: Report) -> Path:
    window = report.window
    if window.kind == "custom":
        name = f"custom-{window.first_day}_{window.last_day}.md"
    elif window.kind == "all":
        name = f"all-{window.last_day}.md"
    else:
        name = f"{window.kind}-{window.first_day}.md"
    return memory_dir / REPORTS_DIR / slug(report.project or ALL_PROJECTS) / name


def save_markdown(memory_dir: Path, report: Report, markdown: str) -> Path:
    path = report_path(memory_dir, report)
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    fd, temporary = tempfile.mkstemp(prefix=".report-", suffix=".md", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        os.chmod(temporary, FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path
