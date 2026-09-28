from team_memory.reports.contracts import TeamReportRequest

REPORT_TOOLS = {
    "memory_report": (TeamReportRequest, (
        "Activity report for a period: today (period=day), this ISO week (week), this month (month), all time (all) "
        "or custom since/until; offset=-1 for the previous day/week/month. scope=personal reports your own memory; "
        "scope=team with team_id is the department report for its head, company viewers and superadmins; "
        "scope=company covers every department plus shared memory (company viewer, superadmin). Sections: numbers "
        "with deltas against the previous period, decisions with WHY, solutions, errors and recurring patterns, "
        "lessons, open next steps, files, entities, contributors and a daily timeline, each with record IDs for "
        "memory_get. Deterministic; include_llm_summary=true adds a paragraph from the server LLM.")),
}
