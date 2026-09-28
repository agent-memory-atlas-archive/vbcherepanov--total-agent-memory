"""Markdown rendering of a Report. Pure function of the report, so JSON and Markdown never disagree."""
from memory_reports.contracts import (
    Counted,
    Listing,
    Metric,
    Report,
    ReportItem,
    SourceRef,
)

REF_PREFIX = {"knowledge": "#", "error": "err#", "rule": "rule#", "observation": "obs#", "session_summary": "summary:"}
SUMMARY_ID_CHARS = 8
SUMMARY_ID_LIMIT = 12
DRILL_IDS = 30
OPEN_LABEL = {"next_step": "- [ ] ", "open_question": "- **Question:** ", "pitfall": "- **Pitfall:** "}


def ref(source: SourceRef) -> str:
    identifier = str(source.id)
    if source.kind == "session_summary" and len(identifier) > SUMMARY_ID_LIMIT:
        identifier = identifier[:SUMMARY_ID_CHARS]
    text = REF_PREFIX[source.kind] + identifier
    return f"`{source.workspace} {text}`" if source.workspace else f"`{text}`"


def refs(sources: list[SourceRef]) -> str:
    return " ".join(ref(source) for source in sources)


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def stamp(value: str) -> str:
    return value[:16].replace("T", " ")


def delta(metric: Metric) -> str:
    if metric.previous is None:
        return "n/a"
    if metric.delta == 0:
        return "0"
    sign = "+" if metric.delta > 0 else "−"
    pct = "new" if metric.change_pct is None else f"{sign}{abs(metric.change_pct):.1f}%"
    return f"{sign}{abs(metric.delta)} ({pct})"


def heading(title: str, listing: Listing) -> str:
    shown = len(listing.items)
    suffix = f"{listing.total}" if shown == listing.total else f"showing {shown} of {listing.total}"
    return f"## {title} ({suffix})"


def _meta(item: ReportItem) -> str:
    parts = [part for part in (item.importance if item.importance not in (None, "medium") else None,
                               item.status if item.status != "active" else None,
                               item.author, stamp(item.at)) if part]
    return " · ".join(parts)


def _record(item: ReportItem, why_label: str | None) -> str:
    line = f"- **{item.title}**"
    if why_label is not None:
        line += f" — {why_label}: " + (item.why or "_not recorded_")
    elif item.detail:
        line += f" — {item.detail}"
    return f"{line} · {_meta(item)} · {refs(item.sources)}"


def _counted_table(title: str, items: list[Counted], with_kind: bool) -> list[str]:
    head = f"| {title} | Type | Records | Sources |" if with_kind else f"| {title} | Touches | Sources |"
    lines = [head, "|---|---|--:|---|" if with_kind else "|---|--:|---|"]
    for item in items:
        kind = f" {cell(item.kind)} |" if with_kind else ""
        lines.append(f"| `{cell(item.name)}` |{kind} {item.count} | {refs(item.sources)} |")
    return lines


def render_markdown(report: Report) -> str:
    window = report.window
    subject = report.project or "all projects"
    scope = f" · {report.scope}" if report.scope else ""
    status = " · in progress" if window.in_progress else ""
    dates = f" · {window.first_day} → {window.last_day}" if window.kind == "month" else ""
    lines = [f"# Activity report: {subject}{scope}", "",
             (f"- **Period:** {window.label}{dates} ({window.days} day{'s' if window.days != 1 else ''}, "
              f"{window.timezone}){status}"),
             f"- **Compared with:** {report.previous.label}" if report.previous else
             "- **Compared with:** nothing (all-time report)",
             f"- **Generated:** {stamp(report.generated_at)} · built from stored records without an LLM", ""]
    if report.llm_summary:
        lines += ["> **LLM summary** (generated from the sections below; check the source IDs):", ">"]
        lines += [f"> {line}" if line else ">" for line in report.llm_summary.splitlines()] + [""]
    elif report.llm_summary_error:
        lines += [f"_LLM summary unavailable: {report.llm_summary_error}_", ""]
    if report.empty:
        lines += ["No activity was recorded in this period.", ""]

    lines += ["## Summary", "", "| Metric | This period | Previous | Change |", "|---|--:|--:|--:|"]
    for metric in report.changes:
        previous = "—" if metric.previous is None else str(metric.previous)
        lines.append(f"| {metric.label} | {metric.current} | {previous} | {delta(metric)} |")
    if report.summary.records_by_type:
        lines += ["", "Records by type: " + " · ".join(f"{kind} {count}"
                                                        for kind, count in report.summary.records_by_type.items())]
    lines.append("")

    if report.decisions.total:
        lines += [heading("Key decisions", report.decisions), ""]
        lines += [_record(item, "why") for item in report.decisions.items] + [""]
    if report.solutions.total:
        lines += [heading("Solutions and fixes", report.solutions), ""]
        lines += [_record(item, None) for item in report.solutions.items] + [""]
    if report.errors.total or report.lessons.total:
        lines += ["## Errors and lessons", ""]
        if report.error_patterns:
            lines += ["### Error patterns", "", "| Pattern | This period | All time | Recurring | First seen | Last seen | Errors |",
                      "|---|--:|--:|---|---|---|---|"]
            lines += [f"| `{cell(p.pattern)}` | {p.count} | {p.total} | {'yes' if p.recurring else 'no'} | "
                      f"{stamp(p.first_seen)} | {stamp(p.last_seen)} | {refs(p.sources)} |" for p in report.error_patterns]
            lines.append("")
        if report.errors.total:
            lines += ["#" + heading("Errors", report.errors), ""]
            for item in report.errors.items:
                parts = [f"root cause: {item.why}" if item.why else "", f"fix: {item.detail}" if item.detail else ""]
                detail = " — " + " · ".join(p for p in parts if p) if any(parts) else ""
                lines.append(f"- **[{item.severity} · {item.status}] {item.title}**{detail} · {stamp(item.at)} · "
                             f"{refs(item.sources)}")
            lines.append("")
        if report.lessons.total:
            lines += ["#" + heading("Lessons and rules", report.lessons), ""]
            lines += [_record(item, None) for item in report.lessons.items] + [""]
    if report.open_items.total:
        lines += [heading("Open tasks and next steps", report.open_items), "",
                  "From session summaries (`session_end`). \"picked up\" means a later session loaded it.", ""]
        for item in report.open_items.items:
            extra = [stamp(item.at)]
            if item.occurrences > 1:
                extra.append(f"mentioned {item.occurrences}×")
            if item.picked_up:
                extra.append("picked up")
            lines.append(f"{OPEN_LABEL[item.kind]}{item.text} · {' · '.join(extra)} · {refs(item.sources)}")
        lines.append("")
    if report.files.total:
        lines += [heading("Most touched files", report.files), ""]
        lines += _counted_table("File", report.files.items, with_kind=False) + [""]
    if report.entities.total or report.tags.total:
        lines += [heading("Entities and technologies", report.entities), ""]
        if report.entities.items:
            lines += _counted_table("Entity", report.entities.items, with_kind=True) + [""]
        if report.tags.items:
            lines += ["Tags: " + " · ".join(f"`{tag.name}` ({tag.count})" for tag in report.tags.items), ""]
    if report.contributors:
        lines += ["## Contributors", "", "| Person | Saves | Updates | Deletes | Confirms |", "|---|--:|--:|--:|--:|"]
        lines += [f"| {cell(c.display_name)} (`{c.user_id}`) | {c.saves} | {c.updates} | {c.deletes} | {c.confirms} |"
                  for c in report.contributors] + [""]
    if report.timeline:
        lines += ["## Timeline", ""]
        for day in report.timeline:
            counts = [f"{day.records} record{'s' if day.records != 1 else ''}"]
            if day.errors:
                counts.append(f"{day.errors} error{'s' if day.errors != 1 else ''}")
            if day.summaries:
                counts.append(f"{day.summaries} session summar{'ies' if day.summaries != 1 else 'y'}")
            counts.append(f"{day.sessions} session{'s' if day.sessions != 1 else ''}")
            lines += [f"### {day.date} {day.weekday} — {' · '.join(counts)}", ""]
            lines += [f"- {entry.at[11:16]} {entry.type}: {entry.title} {refs(entry.sources)}" for entry in day.entries]
            if day.more:
                lines.append(f"- … and {day.more} more")
            lines.append("")
    lines += _drill_down(report)
    return "\n".join(lines).rstrip() + "\n"


def _drill_down(report: Report) -> list[str]:
    ids: list[int] = []
    for listing in (report.decisions, report.solutions, report.lessons):
        for item in listing.items:
            ids.extend(int(s.id) for s in item.sources if s.kind == "knowledge" and s.workspace is None)
    ids = list(dict.fromkeys(ids))[:DRILL_IDS]
    lines = ["---", ""]
    if report.scope:
        lines.append("Drill down: `memory_get` with the record's scope (the workspace named next to the id, or this "
                     "report's scope) and the `#id`.")
    elif ids:
        lines.append(f"Drill down: `memory_get(ids={ids})` returns the full records.")
    else:
        lines.append("Drill down: pass any `#id` above to `memory_get`.")
    lines.append("`err#` = error log, `rule#` = learned rule, `summary:` = session summary, `obs#` = observation.")
    return lines
