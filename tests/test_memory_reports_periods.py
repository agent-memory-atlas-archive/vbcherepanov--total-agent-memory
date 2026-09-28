from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from memory_reports import periods
from memory_reports.contracts import ReportError, ReportRequest

BERLIN = periods.resolve_zone("Europe/Berlin")
NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)  # Friday


def span(now=NOW, zone=BERLIN, first=None, **fields):
    return periods.resolve(ReportRequest(**fields), zone, now, first)


def test_day_is_the_local_calendar_day():
    today = span(period="day")
    assert (today.start, today.end) == (datetime(2026, 9, 24, 22, tzinfo=UTC), datetime(2026, 9, 25, 22, tzinfo=UTC))
    late_utc = datetime(2026, 9, 25, 22, 30, tzinfo=UTC)  # already Saturday in Berlin
    assert span(now=late_utc, period="day").start == datetime(2026, 9, 25, 22, tzinfo=UTC)
    assert span(now=late_utc, zone=periods.resolve_zone("UTC"), period="day").start == datetime(2026, 9, 25, tzinfo=UTC)
    yesterday = span(period="day", offset=-1)
    assert yesterday.end == today.start and yesterday.label.startswith("2026-09-24 (Thursday)")


@pytest.mark.parametrize("day,hours", [((2026, 3, 29), 23), ((2026, 10, 25), 25), ((2026, 7, 1), 24)])
def test_day_length_follows_dst(day, hours):
    now = datetime(*day, 12, tzinfo=UTC)
    today = span(now=now, period="day")
    assert today.end - today.start == timedelta(hours=hours)
    window = periods.to_window(today, now)
    assert window.days == 1 and window.first_day == window.last_day == "{:04d}-{:02d}-{:02d}".format(*day)


def test_week_starts_on_monday_and_spans_dst_change():
    sunday_evening = datetime(2026, 10, 25, 20, tzinfo=UTC)
    week = span(now=sunday_evening, period="week")
    window = periods.to_window(week, sunday_evening)
    assert (window.first_day, window.last_day, window.days) == ("2026-10-19", "2026-10-25", 7)
    assert week.label.startswith("ISO week 2026-W43")
    assert week.end - week.start == timedelta(days=7, hours=1)
    monday_early = datetime(2026, 9, 20, 22, 30, tzinfo=UTC)  # Monday 00:30 in Berlin, Sunday in UTC
    assert periods.to_window(span(now=monday_early, period="week"), monday_early).first_day == "2026-09-21"


def test_iso_week_label_across_new_year():
    now = datetime(2026, 12, 31, 12, tzinfo=UTC)
    assert span(now=now, period="week").label == "ISO week 2026-W53 (2026-12-28 – 2027-01-03)"
    assert span(now=now, period="week", offset=-1).label == "ISO week 2026-W52 (2026-12-21 – 2026-12-27)"


@pytest.mark.parametrize("now,offset,first,last", [
    (datetime(2028, 2, 20, tzinfo=UTC), 0, "2028-02-01", "2028-02-29"),
    (datetime(2027, 1, 15, tzinfo=UTC), -1, "2026-12-01", "2026-12-31"),
    (datetime(2026, 3, 31, 21, 30, tzinfo=UTC), 0, "2026-03-01", "2026-03-31"),
    (datetime(2026, 3, 31, 22, 30, tzinfo=UTC), 0, "2026-04-01", "2026-04-30"),
    (datetime(2026, 9, 25, tzinfo=UTC), -13, "2025-08-01", "2025-08-31"),
])
def test_month_boundaries(now, offset, first, last):
    window = periods.to_window(span(now=now, period="month", offset=offset), now)
    assert (window.first_day, window.last_day) == (first, last)


def test_previous_periods_are_adjacent():
    for kind in ("day", "week", "month"):
        current = span(period=kind)
        before = periods.previous(current)
        assert before.end == current.start and before.kind == kind
    march = span(now=datetime(2026, 3, 15, tzinfo=UTC), period="month")
    assert periods.to_window(periods.previous(march), NOW).last_day == "2026-02-28"


def test_all_runs_from_the_first_record_and_has_no_previous():
    first = datetime(2025, 1, 3, 23, 30, tzinfo=UTC)  # 2025-01-04 00:30 in Berlin
    everything = span(period="all", first=first)
    window = periods.to_window(everything, NOW)
    assert (window.first_day, window.last_day) == ("2025-01-04", "2026-09-25")
    assert periods.previous(everything) is None
    assert periods.to_window(span(period="all"), NOW).first_day == "2026-09-25"


def test_custom_dates_are_inclusive_and_datetimes_exact():
    custom = span(period="custom", since="2026-09-01", until="2026-09-15")
    window = periods.to_window(custom, NOW)
    assert (window.first_day, window.last_day, window.days) == ("2026-09-01", "2026-09-15", 15)
    assert custom.end == datetime(2026, 9, 15, 22, tzinfo=UTC) and not window.in_progress
    before = periods.previous(custom)
    assert before.end == custom.start and before.end - before.start == custom.end - custom.start
    exact = span(period="custom", since="2026-09-01T12:00:00+00:00", until="2026-09-02T06:00:00")
    assert (exact.start, exact.end) == (datetime(2026, 9, 1, 12, tzinfo=UTC), datetime(2026, 9, 2, 4, tzinfo=UTC))
    open_ended = span(period="custom", since="2026-09-20")
    assert open_ended.end == datetime(2026, 9, 25, 22, tzinfo=UTC)
    assert periods.to_window(open_ended, NOW).in_progress


def test_custom_rejects_reversed_bounds():
    with pytest.raises(ReportError):
        span(period="custom", since="2026-09-10", until="2026-09-01")


@pytest.mark.parametrize("fields", [
    {"period": "custom"},
    {"period": "week", "since": "2026-09-01"},
    {"period": "all", "offset": -1},
    {"period": "custom", "since": "yesterday"},
    {"period": "day", "offset": 1},
    {"period": "week", "limit": 0},
    {"period": "fortnight"},
    {"period": "week", "unknown": 1},
])
def test_invalid_requests_are_rejected(fields):
    with pytest.raises(ValidationError):
        ReportRequest(**fields)


def test_zone_resolution(monkeypatch):
    assert periods.resolve_zone("Asia/Tokyo").name == "Asia/Tokyo"
    with pytest.raises(ReportError):
        periods.resolve_zone("Mars/Olympus")
    with pytest.raises(ReportError):
        periods.resolve_zone("../../etc/passwd")
    assert periods.resolve_zone(None, {"MEMORY_REPORT_TZ": "America/New_York", "TZ": "Asia/Tokyo"}).name == "America/New_York"
    assert periods.resolve_zone(None, {"TZ": ":Europe/Paris"}).name == "Europe/Paris"
    monkeypatch.setattr(periods.os.path, "realpath", lambda _path: "/usr/share/zoneinfo/Europe/Madrid")
    assert periods.resolve_zone(None, {"TZ": "Not/AZone"}).name == "Europe/Madrid"
    monkeypatch.setattr(periods.os.path, "realpath", lambda _path: "/etc/localtime")
    assert periods.resolve_zone(None, {}).name == "UTC"
