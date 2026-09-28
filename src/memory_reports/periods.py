"""Report windows in the reader's timezone: calendar day, ISO week, calendar month, all time or custom bounds."""
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from memory_reports.contracts import (
    PeriodKind,
    ReportError,
    ReportRequest,
    Window,
    parse_bound,
)

LOCALTIME = "/etc/localtime"
ZONEINFO_MARKER = "zoneinfo/"
TZ_ENV = ("MEMORY_REPORT_TZ", "TZ")
MONTHS_PER_YEAR = 12
DAYS_PER_WEEK = 7


@dataclass(frozen=True)
class Zone:
    name: str
    info: tzinfo


@dataclass(frozen=True)
class Span:
    kind: PeriodKind
    zone: Zone
    start: datetime
    end: datetime
    offset: int
    label: str

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def local_date(self, moment: datetime) -> date:
        return moment.astimezone(self.zone.info).date()


def _zone(name: str) -> Zone | None:
    key = name.strip().lstrip(":")
    if not key:
        return None
    try:
        return Zone(key, ZoneInfo(key))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def resolve_zone(name: str | None, environ: Mapping[str, str] | None = None) -> Zone:
    """Explicit zone, else MEMORY_REPORT_TZ / TZ, else the system zone from /etc/localtime, else UTC."""
    if name is not None:
        zone = _zone(name)
        if zone is None:
            raise ReportError(f"Unknown timezone {name!r}; use an IANA name such as Europe/Berlin")
        return zone
    env = os.environ if environ is None else environ
    for key in TZ_ENV:
        zone = _zone(env.get(key, ""))
        if zone is not None:
            return zone
    target = os.path.realpath(LOCALTIME)
    if ZONEINFO_MARKER in target:
        zone = _zone(target.rsplit(ZONEINFO_MARKER, 1)[1])
        if zone is not None:
            return zone
    return Zone("UTC", UTC)


def midnight(day: date, zone: Zone) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=zone.info).astimezone(UTC)


def _shift_month(day: date, months: int) -> date:
    index = day.year * MONTHS_PER_YEAR + day.month - 1 + months
    return date(index // MONTHS_PER_YEAR, index % MONTHS_PER_YEAR + 1, 1)


def _instant(value: str, zone: Zone, end: bool) -> datetime:
    bound = parse_bound(value)
    if isinstance(bound, datetime):
        moment = bound if bound.tzinfo is not None else bound.replace(tzinfo=zone.info)
        return moment.astimezone(UTC)
    return midnight(bound + timedelta(days=1) if end else bound, zone)


def _calendar(kind: PeriodKind, today: date, offset: int, zone: Zone) -> Span:
    if kind == "day":
        first = today + timedelta(days=offset)
        last, label = first, f"{first.isoformat()} ({first.strftime('%A')})"
    elif kind == "week":
        first = today - timedelta(days=today.weekday()) + timedelta(days=DAYS_PER_WEEK * offset)
        last = first + timedelta(days=DAYS_PER_WEEK - 1)
        year, week, _ = first.isocalendar()
        label = f"ISO week {year}-W{week:02d} ({first.isoformat()} – {last.isoformat()})"
    else:
        first = _shift_month(today.replace(day=1), offset)
        last = _shift_month(first, 1) - timedelta(days=1)
        label = first.strftime("%B %Y")
    return Span(kind, zone, midnight(first, zone), midnight(last + timedelta(days=1), zone), offset, label)


def resolve(request: ReportRequest, zone: Zone, now: datetime, first_activity: datetime | None) -> Span:
    today = now.astimezone(zone.info).date()
    if request.period in ("day", "week", "month"):
        return _calendar(request.period, today, request.offset, zone)
    if request.period == "all":
        first = first_activity.astimezone(zone.info).date() if first_activity else today
        first = min(first, today)
        return Span("all", zone, midnight(first, zone), midnight(today + timedelta(days=1), zone), 0,
                    f"All time ({first.isoformat()} – {today.isoformat()})")
    start = _instant(request.since, zone, end=False)
    end = _instant(request.until, zone, end=True) if request.until else midnight(today + timedelta(days=1), zone)
    if end <= start:
        raise ReportError("until must be after since")
    bare = Span("custom", zone, start, end, 0, "")
    return Span("custom", zone, start, end, 0, f"{_first_day(bare).isoformat()} – {_last_day(bare).isoformat()}")


def previous(span: Span) -> Span | None:
    """The period right before `span`: the previous calendar period, or an equal-length span for custom."""
    if span.kind == "all":
        return None
    if span.kind in ("day", "week", "month"):
        return _calendar(span.kind, _first_day(span), -1, span.zone)
    earlier = Span("custom", span.zone, span.start - (span.end - span.start), span.start, 0, "")
    return Span("custom", span.zone, earlier.start, earlier.end, 0,
                f"{_first_day(earlier).isoformat()} – {_last_day(earlier).isoformat()}")


def _first_day(span: Span) -> date:
    return span.local_date(span.start)


def _last_day(span: Span) -> date:
    return span.local_date(span.end - timedelta(microseconds=1))


def utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def to_window(span: Span, now: datetime) -> Window:
    first, last = _first_day(span), _last_day(span)
    return Window(kind=span.kind, label=span.label, timezone=span.zone.name,
                  start=span.start.astimezone(span.zone.info).isoformat(),
                  end=span.end.astimezone(span.zone.info).isoformat(),
                  start_utc=utc_text(span.start), end_utc=utc_text(span.end),
                  first_day=first.isoformat(), last_day=last.isoformat(), days=(last - first).days + 1,
                  in_progress=span.start <= now < span.end)
