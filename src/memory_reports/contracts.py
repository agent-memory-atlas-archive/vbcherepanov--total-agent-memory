from datetime import date, datetime
from typing import Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

PeriodKind = Literal["day", "week", "month", "all", "custom"]
OutputFormat = Literal["markdown", "json"]
SourceKind = Literal["knowledge", "error", "rule", "session_summary", "observation"]
OpenKind = Literal["next_step", "open_question", "pitfall"]
MAX_ITEMS = 200
DEFAULT_ITEMS = 20
MAX_OFFSET = 520
T = TypeVar("T")


class ReportError(ValueError):
    """Invalid report request (bad period, bounds or timezone)."""


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def parse_bound(value: str) -> date | datetime:
    """A custom-period bound: a calendar date (YYYY-MM-DD) or an ISO-8601 date-time."""
    text = value.strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{value!r} is not YYYY-MM-DD or an ISO-8601 date-time") from exc


class ReportRequest(DTO):
    project: str | None = Field(default=None, min_length=1, max_length=128)
    period: PeriodKind = "week"
    since: str | None = Field(default=None, max_length=40)
    until: str | None = Field(default=None, max_length=40)
    offset: int = Field(default=0, ge=-MAX_OFFSET, le=0)
    tz: str | None = Field(default=None, min_length=1, max_length=64)
    limit: int = Field(default=DEFAULT_ITEMS, ge=1, le=MAX_ITEMS)

    @field_validator("since", "until")
    @classmethod
    def valid_bound(cls, value: str | None) -> str | None:
        if value is not None:
            parse_bound(value)
        return value

    @model_validator(mode="after")
    def valid_period(self):
        if self.period == "custom":
            if self.since is None:
                raise ValueError("period=custom requires since (until defaults to today)")
        elif self.since is not None or self.until is not None:
            raise ValueError("since/until are only valid with period=custom")
        if self.offset and self.period not in ("day", "week", "month"):
            raise ValueError("offset is only valid with period=day, week or month")
        return self


class Window(DTO):
    kind: PeriodKind
    label: str
    timezone: str
    start: str
    end: str
    start_utc: str
    end_utc: str
    first_day: str
    last_day: str
    days: int
    in_progress: bool


class SourceRef(DTO):
    kind: SourceKind
    id: int | str
    workspace: str | None = None

    @model_serializer(mode="wrap")
    def compact(self, handler):
        data = handler(self)
        if data.get("workspace") is None:
            data.pop("workspace", None)
        return data


class ReportItem(DTO):
    title: str
    type: str
    at: str
    status: str
    why: str = ""
    detail: str = ""
    importance: str | None = None
    severity: str | None = None
    author: str | None = None
    workspace: str | None = None
    sources: list[SourceRef]


class Listing(DTO, Generic[T]):
    total: int
    items: list[T]


class ErrorPattern(DTO):
    pattern: str
    count: int
    total: int
    recurring: bool
    first_seen: str
    last_seen: str
    sources: list[SourceRef]


class OpenItem(DTO):
    kind: OpenKind
    text: str
    at: str
    occurrences: int
    picked_up: bool
    sources: list[SourceRef]


class Counted(DTO):
    name: str
    kind: str
    count: int
    sources: list[SourceRef]


class TimelineEntry(DTO):
    title: str
    type: str
    at: str
    sources: list[SourceRef]


class DayActivity(DTO):
    date: str
    weekday: str
    records: int
    by_type: dict[str, int]
    errors: int
    sessions: int
    summaries: int
    entries: list[TimelineEntry]
    more: int


class Metric(DTO):
    key: str
    label: str
    current: int
    previous: int | None
    delta: int | None
    change_pct: float | None


class Summary(DTO):
    records: int
    records_by_type: dict[str, int]
    new: int
    updated: int
    confirmed: int
    superseded: int
    errors: int
    session_summaries: int
    sessions: int
    active_days: int


class Contributor(DTO):
    user_id: str
    display_name: str
    saves: int
    updates: int
    deletes: int
    confirms: int


class Report(DTO):
    schema_version: int = 1
    project: str | None
    scope: str | None = None
    window: Window
    previous: Window | None
    generated_at: str
    empty: bool
    summary: Summary
    changes: list[Metric]
    decisions: Listing[ReportItem]
    solutions: Listing[ReportItem]
    errors: Listing[ReportItem]
    lessons: Listing[ReportItem]
    error_patterns: list[ErrorPattern]
    open_items: Listing[OpenItem]
    files: Listing[Counted]
    entities: Listing[Counted]
    tags: Listing[Counted]
    timeline: list[DayActivity]
    contributors: list[Contributor] = Field(default_factory=list)
    llm_summary: str | None = None
    llm_summary_error: str | None = None
