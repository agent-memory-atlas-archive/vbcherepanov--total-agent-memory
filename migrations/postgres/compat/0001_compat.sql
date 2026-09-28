-- SQLite compatibility functions for the team server's PostgreSQL backend.
--
-- Schema tam_compat holds functions (and one catalog view) only, no data. Every
-- workspace role has it in its search_path (ws_x, tam_compat, extensions), so SQLite SQL
-- calling strftime(), datetime(), json_extract(), ... runs unchanged; the translator
-- (src/tam_db/translate.py) qualifies these names so pg_catalog functions of the same
-- name never shadow them.
--
-- Date and time functions port SQLite's date.c (3.47): the same time-value formats
-- (YYYY-MM-DD[ HH:MM[:SS[.SSS]]][ tz], HH:MM[:SS[.SSS]], 'now', Julian day numbers), the
-- same modifiers ('NNN days|hours|minutes|seconds|months|years', '+HH:MM[:SS]',
-- '+YYYY-MM-DD', 'start of day|month|year', 'weekday N', 'unixepoch', 'julianday',
-- 'auto', 'subsec', 'ceiling', 'floor', 'localtime', 'utc'), millisecond Julian-day
-- arithmetic, month overflow normalization and NULL for anything invalid. Local time is
-- UTC (sessions run with TimeZone=UTC), so 'localtime' and 'utc' change nothing. 'now' is
-- the statement timestamp, stable within a statement as in SQLite.
--
-- Idempotent: safe to run again on upgrade.

CREATE SCHEMA IF NOT EXISTS tam_compat;
GRANT USAGE ON SCHEMA tam_compat TO PUBLIC;

DO $$
BEGIN
    IF to_regtype('tam_compat.sqlite_datetime') IS NULL THEN
        CREATE TYPE tam_compat.sqlite_datetime AS (
            jd bigint,               -- milliseconds since Julian day 0 (SQLite iJD)
            y integer, mo integer, d integer,
            h integer, mi integer, s double precision,
            tz integer,              -- minutes east of UTC from the time value
            raw double precision,    -- a numeric time value not yet interpreted
            valid_jd boolean, valid_ymd boolean, valid_hms boolean, valid_tz boolean,
            raw_s boolean, subsec boolean, nfloor integer, err boolean
        );
    END IF;
END
$$;

-- ── Julian day arithmetic (date.c: computeJD, computeYMD, computeHMS, computeFloor) ──

CREATE OR REPLACE FUNCTION tam_compat._new_state() RETURNS tam_compat.sqlite_datetime
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ROW(NULL, 2000, 1, 1, 0, 0, 0.0, 0, NULL, false, false, false, false, false, false, 0,
               false)::tam_compat.sqlite_datetime
$$;

CREATE OR REPLACE FUNCTION tam_compat._valid_jd(jd bigint) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT jd IS NOT NULL AND jd >= 0 AND jd <= 464269060799999
$$;

CREATE OR REPLACE FUNCTION tam_compat._compute_jd(p tam_compat.sqlite_datetime)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    yy integer;
    mm integer;
    dd integer;
    a integer;
    b integer;
    x1 integer;
    x2 integer;
BEGIN
    IF p.err OR p.valid_jd THEN
        RETURN p;
    END IF;
    IF p.valid_ymd THEN
        yy := p.y; mm := p.mo; dd := p.d;
    ELSE
        yy := 2000; mm := 1; dd := 1;
    END IF;
    IF yy < -4713 OR yy > 9999 OR p.raw_s THEN
        p.err := true;
        RETURN p;
    END IF;
    IF mm <= 2 THEN
        yy := yy - 1;
        mm := mm + 12;
    END IF;
    a := (yy + 4800) / 100;
    b := 38 - a + (a / 4);
    x1 := 36525 * (yy + 4716) / 100;
    x2 := 306001 * (mm + 1) / 10000;
    p.jd := (x1::bigint + x2 + dd + b) * 86400000 - 131716800000;
    p.valid_jd := true;
    IF p.valid_hms THEN
        p.jd := p.jd + p.h::bigint * 3600000 + p.mi::bigint * 60000 + trunc(p.s * 1000 + 0.5)::bigint;
        IF p.valid_tz THEN
            p.jd := p.jd - p.tz::bigint * 60000;
            p.valid_ymd := false;
            p.valid_hms := false;
            p.valid_tz := false;
        END IF;
    END IF;
    RETURN p;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._compute_ymd(p tam_compat.sqlite_datetime)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    z integer;
    alpha integer;
    a integer;
    b integer;
    c integer;
    dd integer;
    e integer;
    x1 integer;
BEGIN
    IF p.err OR p.valid_ymd THEN
        RETURN p;
    END IF;
    IF NOT p.valid_jd THEN
        p.y := 2000; p.mo := 1; p.d := 1;
    ELSIF NOT tam_compat._valid_jd(p.jd) THEN
        p.err := true;
        RETURN p;
    ELSE
        z := ((p.jd + 43200000) / 86400000)::integer;
        alpha := trunc((z + 32044.75::double precision) / 36524.25::double precision)::integer - 52;
        a := z + 1 + alpha - ((alpha + 100) / 4) + 25;
        b := a + 1524;
        c := trunc((b - 122.1::double precision) / 365.25::double precision)::integer;
        dd := (36525 * (c & 32767)) / 100;
        e := trunc((b - dd) / 30.6001::double precision)::integer;
        x1 := trunc(30.6001::double precision * e)::integer;
        p.d := b - dd - x1;
        p.mo := CASE WHEN e < 14 THEN e - 1 ELSE e - 13 END;
        p.y := CASE WHEN p.mo > 2 THEN c - 4716 ELSE c - 4715 END;
    END IF;
    p.valid_ymd := true;
    RETURN p;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._compute_hms(p tam_compat.sqlite_datetime)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    day_ms bigint;
    day_min integer;
BEGIN
    IF p.err OR p.valid_hms THEN
        RETURN p;
    END IF;
    p := tam_compat._compute_jd(p);
    IF p.err THEN
        RETURN p;
    END IF;
    day_ms := (p.jd + 43200000) % 86400000;
    p.s := (day_ms % 60000) / 1000.0::double precision;
    day_min := (day_ms / 60000)::integer;
    p.mi := day_min % 60;
    p.h := day_min / 60;
    p.raw_s := false;
    p.valid_hms := true;
    RETURN p;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._compute_ymd_hms(p tam_compat.sqlite_datetime)
RETURNS tam_compat.sqlite_datetime
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT tam_compat._compute_hms(tam_compat._compute_ymd(p))
$$;

CREATE OR REPLACE FUNCTION tam_compat._clear_ymd_hms_tz(p tam_compat.sqlite_datetime)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
BEGIN
    p.valid_ymd := false;
    p.valid_hms := false;
    p.valid_tz := false;
    RETURN p;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._compute_floor(p tam_compat.sqlite_datetime)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
BEGIN
    IF p.d <= 28 THEN
        p.nfloor := 0;
    ELSIF ((1 << p.mo) & 5546) <> 0 THEN  -- 0x15aa: months with 31 days
        p.nfloor := 0;
    ELSIF p.mo <> 2 THEN
        p.nfloor := CASE WHEN p.d = 31 THEN 1 ELSE 0 END;
    ELSIF p.y % 4 <> 0 OR (p.y % 100 = 0 AND p.y % 400 <> 0) THEN
        p.nfloor := p.d - 28;
    ELSE
        p.nfloor := p.d - 29;
    END IF;
    RETURN p;
END
$$;

-- ── parsing (date.c: parseHhMmSs, parseTimezone, parseYyyyMmDd, parseDateOrTime) ──

CREATE OR REPLACE FUNCTION tam_compat._is_number(value text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT value ~ '^\s*[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?\s*$'
$$;

CREATE OR REPLACE FUNCTION tam_compat._parse_hms(value text, p tam_compat.sqlite_datetime)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    parts text[];
    rest text;
    hh integer;
    mm integer;
    ss double precision := 0;
    tz_hours integer;
    tz_minutes integer;
BEGIN
    parts := regexp_match(value, '^([0-9]{2}):([0-9]{2})(.*)$');
    IF parts IS NULL THEN
        p.err := true;
        RETURN p;
    END IF;
    hh := parts[1]::integer;
    mm := parts[2]::integer;
    rest := parts[3];
    IF hh > 24 OR mm > 59 THEN
        p.err := true;
        RETURN p;
    END IF;
    IF left(rest, 1) = ':' THEN
        parts := regexp_match(rest, '^:([0-9]{2})(.*)$');
        IF parts IS NULL OR parts[1]::integer > 59 THEN
            p.err := true;
            RETURN p;
        END IF;
        ss := parts[1]::integer;
        rest := parts[2];
        parts := regexp_match(rest, '^\.([0-9]+)(.*)$');
        IF parts IS NOT NULL THEN
            ss := ss + parts[1]::numeric::double precision / power(10::double precision, length(parts[1]));
            rest := parts[2];
        END IF;
    END IF;
    -- parseTimezone
    rest := ltrim(rest, E' \t\n\r\f\v');
    p.tz := 0;
    IF rest = '' THEN
        NULL;
    ELSIF left(rest, 1) IN ('Z', 'z') THEN
        IF ltrim(substr(rest, 2), E' \t\n\r\f\v') <> '' THEN
            p.err := true;
            RETURN p;
        END IF;
    ELSE
        parts := regexp_match(rest, '^([+-])([0-9]{2}):([0-9]{2})\s*$');
        IF parts IS NULL THEN
            p.err := true;
            RETURN p;
        END IF;
        tz_hours := parts[2]::integer;
        tz_minutes := parts[3]::integer;
        IF tz_hours > 14 OR tz_minutes > 59 THEN
            p.err := true;
            RETURN p;
        END IF;
        p.tz := (CASE WHEN parts[1] = '-' THEN -1 ELSE 1 END) * (tz_minutes + tz_hours * 60);
    END IF;
    p.valid_jd := false;
    p.raw_s := false;
    p.valid_hms := true;
    p.h := hh;
    p.mi := mm;
    p.s := ss;
    p.valid_tz := p.tz <> 0;
    RETURN p;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._parse_ymd(value text, p tam_compat.sqlite_datetime)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    parts text[];
    rest text;
    timed tam_compat.sqlite_datetime;
BEGIN
    parts := regexp_match(value, '^(-?)([0-9]{4})-([0-9]{2})-([0-9]{2})(.*)$');
    IF parts IS NULL OR parts[3]::integer NOT BETWEEN 1 AND 12 OR parts[4]::integer NOT BETWEEN 1 AND 31 THEN
        p.err := true;
        RETURN p;
    END IF;
    rest := ltrim(parts[5], E' \t\n\r\f\vT');
    IF rest <> '' THEN
        timed := tam_compat._parse_hms(rest, p);
        IF timed.err THEN
            p.err := true;
            RETURN p;
        END IF;
        p := timed;
    ELSE
        p.valid_hms := false;
    END IF;
    p.valid_jd := false;
    p.valid_ymd := true;
    p.y := CASE WHEN parts[1] = '-' THEN -parts[2]::integer ELSE parts[2]::integer END;
    p.mo := parts[3]::integer;
    p.d := parts[4]::integer;
    IF p.valid_tz THEN
        p := tam_compat._compute_jd(p);
    END IF;
    RETURN p;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._set_raw_number(p tam_compat.sqlite_datetime, r double precision)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
BEGIN
    p.s := r;
    p.raw := r;
    p.raw_s := true;
    IF r >= 0.0 AND r < 5373484.5 THEN
        p.jd := trunc(r * 86400000.0 + 0.5)::bigint;
        p.valid_jd := true;
    END IF;
    RETURN p;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._now_jd() RETURNS bigint
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT floor(extract(epoch FROM statement_timestamp()) * 1000)::bigint + 210866760000000
$$;

CREATE OR REPLACE FUNCTION tam_compat._set_now(p tam_compat.sqlite_datetime)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
BEGIN
    p.jd := tam_compat._now_jd();
    p.valid_jd := true;
    RETURN p;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._parse_date_or_time(value text, p tam_compat.sqlite_datetime)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
DECLARE
    parsed tam_compat.sqlite_datetime;
BEGIN
    parsed := tam_compat._parse_ymd(value, p);
    IF NOT parsed.err THEN
        RETURN parsed;
    END IF;
    parsed := tam_compat._parse_hms(value, p);
    IF NOT parsed.err THEN
        RETURN parsed;
    END IF;
    IF lower(value) = 'now' THEN
        RETURN tam_compat._set_now(p);
    END IF;
    IF tam_compat._is_number(value) THEN
        RETURN tam_compat._set_raw_number(p, value::double precision);
    END IF;
    IF lower(value) IN ('subsec', 'subsecond') THEN
        p.subsec := true;
        RETURN tam_compat._set_now(p);
    END IF;
    p.err := true;
    RETURN p;
END
$$;

-- ── modifiers (date.c: parseModifier) ──

CREATE OR REPLACE FUNCTION tam_compat._apply_modifier(p tam_compat.sqlite_datetime, modifier text, idx integer)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
DECLARE
    z text := lower(modifier);
    n integer;
    r double precision;
    parts text[];
    unit text;
    rounder double precision;
    limit_value double precision;
    factor double precision;
    whole integer;
    carry integer;
    target integer;
    weekday integer;
    shift_y integer;
    shift_m integer;
    shift_d integer;
    tx tam_compat.sqlite_datetime;
    tx_jd bigint;
    day bigint;
BEGIN
    IF p.err THEN
        RETURN p;
    END IF;
    IF z = 'auto' THEN
        IF idx > 1 THEN
            p.err := true;
        ELSIF NOT p.raw_s OR p.valid_jd THEN
            p.raw_s := false;
        ELSIF p.raw >= -210866760000 AND p.raw <= 253402300799 THEN
            p := tam_compat._clear_ymd_hms_tz(p);
            p.jd := trunc(p.raw * 1000.0 + 210866760000000.0 + 0.5)::bigint;
            p.valid_jd := true;
            p.raw_s := false;
        ELSE
            p.err := true;
        END IF;
        RETURN p;
    END IF;
    IF z = 'ceiling' THEN
        p := tam_compat._clear_ymd_hms_tz(tam_compat._compute_jd(p));
        p.nfloor := 0;
        RETURN p;
    END IF;
    IF z = 'floor' THEN
        p := tam_compat._compute_jd(p);
        p.jd := p.jd - p.nfloor::bigint * 86400000;
        RETURN tam_compat._clear_ymd_hms_tz(p);
    END IF;
    IF z = 'julianday' THEN
        IF idx > 1 OR NOT (p.valid_jd AND p.raw_s) THEN
            p.err := true;
        ELSE
            p.raw_s := false;
        END IF;
        RETURN p;
    END IF;
    IF z IN ('localtime', 'utc') THEN
        -- Local time is UTC: the conversion keeps the instant and renormalizes the fields.
        RETURN tam_compat._clear_ymd_hms_tz(tam_compat._compute_jd(p));
    END IF;
    IF z = 'unixepoch' THEN
        IF idx > 1 OR NOT p.raw_s THEN
            p.err := true;
            RETURN p;
        END IF;
        r := p.raw * 1000.0 + 210866760000000.0;
        IF r >= 0.0 AND r < 464269060800000.0 THEN
            p := tam_compat._clear_ymd_hms_tz(p);
            p.jd := trunc(r + 0.5)::bigint;
            p.valid_jd := true;
            p.raw_s := false;
        ELSE
            p.err := true;
        END IF;
        RETURN p;
    END IF;
    IF z LIKE 'weekday %' THEN
        unit := substr(z, 9);
        IF NOT tam_compat._is_number(unit) THEN
            p.err := true;
            RETURN p;
        END IF;
        r := unit::double precision;
        IF r < 0.0 OR r >= 7.0 OR r <> trunc(r) THEN
            p.err := true;
            RETURN p;
        END IF;
        target := r::integer;
        p := tam_compat._compute_ymd_hms(p);
        p.valid_tz := false;
        p.tz := 0;
        p.valid_jd := false;
        p := tam_compat._compute_jd(p);
        IF p.err THEN
            RETURN p;
        END IF;
        weekday := ((p.jd + 129600000) / 86400000 % 7)::integer;
        IF weekday > target THEN
            weekday := weekday - 7;
        END IF;
        p.jd := p.jd + (target - weekday)::bigint * 86400000;
        RETURN tam_compat._clear_ymd_hms_tz(p);
    END IF;
    IF z LIKE 'start of %' THEN
        IF NOT p.valid_jd AND NOT p.valid_ymd AND NOT p.valid_hms THEN
            p.err := true;
            RETURN p;
        END IF;
        p := tam_compat._compute_ymd(p);
        p.valid_hms := true;
        p.h := 0;
        p.mi := 0;
        p.s := 0.0;
        p.raw_s := false;
        p.valid_tz := false;
        p.tz := 0;
        p.valid_jd := false;
        unit := substr(z, 10);
        IF unit = 'month' THEN
            p.d := 1;
        ELSIF unit = 'year' THEN
            p.mo := 1;
            p.d := 1;
        ELSIF unit <> 'day' THEN
            p.err := true;
        END IF;
        RETURN p;
    END IF;
    IF z IN ('subsec', 'subsecond') THEN
        p.subsec := true;
        RETURN p;
    END IF;
    IF z !~ '^[-+0-9.]' THEN
        p.err := true;
        RETURN p;
    END IF;
    -- Length of the leading number: up to ':' or whitespace, or the '-' of a +YYYY-MM-DD shift.
    n := 1;
    WHILE n < length(z) LOOP
        EXIT WHEN substr(z, n + 1, 1) = ':' OR substr(z, n + 1, 1) ~ '\s';
        EXIT WHEN substr(z, n + 1, 1) = '-' AND n = 5 AND substr(z, 2, 4) ~ '^[0-9]{4}$';
        n := n + 1;
    END LOOP;
    IF NOT tam_compat._is_number(left(z, n)) THEN
        p.err := true;
        RETURN p;
    END IF;
    r := left(z, n)::double precision;
    IF substr(z, n + 1, 1) = '-' THEN
        -- (+|-)YYYY-MM-DD[ HH:MM[:SS[.SSS]]]
        parts := regexp_match(z, '^([+-])([0-9]{4})-([0-9]{2})-([0-9]{2})(.*)$');
        IF parts IS NULL OR parts[3]::integer > 12 OR parts[4]::integer > 31 THEN
            p.err := true;
            RETURN p;
        END IF;
        shift_y := parts[2]::integer;
        shift_m := parts[3]::integer;
        shift_d := parts[4]::integer;
        p := tam_compat._compute_ymd_hms(p);
        IF p.err THEN
            RETURN p;
        END IF;
        p.valid_jd := false;
        IF parts[1] = '-' THEN
            p.y := p.y - shift_y;
            p.mo := p.mo - shift_m;
            shift_d := -shift_d;
        ELSE
            p.y := p.y + shift_y;
            p.mo := p.mo + shift_m;
        END IF;
        carry := CASE WHEN p.mo > 0 THEN (p.mo - 1) / 12 ELSE (p.mo - 12) / 12 END;
        p.y := p.y + carry;
        p.mo := p.mo - carry * 12;
        p := tam_compat._compute_floor(p);
        p := tam_compat._compute_jd(p);
        IF p.err THEN
            RETURN p;
        END IF;
        p.valid_hms := false;
        p.valid_ymd := false;
        p.jd := p.jd + shift_d::bigint * 86400000;
        IF parts[5] = '' THEN
            RETURN p;
        END IF;
        IF parts[5] !~ '^\s[0-9]{2}:[0-9]{2}' THEN
            p.err := true;
            RETURN p;
        END IF;
        z := left(z, 1) || substr(parts[5], 2);
        n := 3;
    END IF;
    IF substr(z, n + 1, 1) = ':' THEN
        -- (+|-)HH:MM[:SS[.FFF]]
        tx := tam_compat._parse_hms(CASE WHEN left(z, 1) ~ '[0-9]' THEN z ELSE substr(z, 2) END,
                                    tam_compat._new_state());
        IF tx.err THEN
            p.err := true;
            RETURN p;
        END IF;
        tx := tam_compat._compute_jd(tx);
        tx_jd := tx.jd - 43200000;
        day := tx_jd / 86400000;
        tx_jd := tx_jd - day * 86400000;
        IF left(z, 1) = '-' THEN
            tx_jd := -tx_jd;
        END IF;
        p := tam_compat._clear_ymd_hms_tz(tam_compat._compute_jd(p));
        p.jd := p.jd + tx_jd;
        RETURN p;
    END IF;
    -- NNN units
    unit := ltrim(substr(z, n + 1), E' \t\n\r\f\v');
    IF length(unit) < 3 OR length(unit) > 10 THEN
        p.err := true;
        RETURN p;
    END IF;
    IF right(unit, 1) = 's' THEN
        unit := left(unit, length(unit) - 1);
    END IF;
    p := tam_compat._compute_jd(p);
    IF p.err THEN
        RETURN p;
    END IF;
    rounder := CASE WHEN r < 0 THEN -0.5 ELSE 0.5 END;
    p.nfloor := 0;
    CASE unit
        WHEN 'second' THEN limit_value := 4.6427e+14; factor := 1.0;
        WHEN 'minute' THEN limit_value := 7.7379e+12; factor := 60.0;
        WHEN 'hour' THEN limit_value := 1.2897e+11; factor := 3600.0;
        WHEN 'day' THEN limit_value := 5373485.0; factor := 86400.0;
        WHEN 'month' THEN limit_value := 176546.0; factor := 2592000.0;
        WHEN 'year' THEN limit_value := 14713.0; factor := 31536000.0;
        ELSE
            p.err := true;
            RETURN p;
    END CASE;
    IF NOT (r > -limit_value AND r < limit_value) THEN
        p.err := true;
        RETURN p;
    END IF;
    IF unit = 'month' THEN
        p := tam_compat._compute_ymd_hms(p);
        whole := trunc(r)::integer;
        p.mo := p.mo + whole;
        carry := CASE WHEN p.mo > 0 THEN (p.mo - 1) / 12 ELSE (p.mo - 12) / 12 END;
        p.y := p.y + carry;
        p.mo := p.mo - carry * 12;
        p := tam_compat._compute_floor(p);
        p.valid_jd := false;
        r := r - whole;
    ELSIF unit = 'year' THEN
        whole := trunc(r)::integer;
        p := tam_compat._compute_ymd_hms(p);
        p.y := p.y + whole;
        p := tam_compat._compute_floor(p);
        p.valid_jd := false;
        r := r - whole;
    END IF;
    p := tam_compat._compute_jd(p);
    IF p.err THEN
        RETURN p;
    END IF;
    p.jd := p.jd + trunc(r * 1000.0 * factor + rounder)::bigint;
    RETURN tam_compat._clear_ymd_hms_tz(p);
END
$$;

-- isDate(): the time value, then every modifier, then a final validity check. A NULL
-- time value or modifier yields NULL (err).
CREATE OR REPLACE FUNCTION tam_compat._evaluate(value text, is_number boolean, modifiers text[])
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
DECLARE
    p tam_compat.sqlite_datetime := tam_compat._new_state();
    idx integer := 0;
    modifier text;
BEGIN
    IF value IS NULL THEN
        p.err := true;
        RETURN p;
    END IF;
    IF is_number THEN
        p := tam_compat._set_raw_number(p, value::double precision);
    ELSE
        p := tam_compat._parse_date_or_time(value, p);
    END IF;
    IF modifiers IS NOT NULL THEN
        FOREACH modifier IN ARRAY modifiers LOOP
            idx := idx + 1;
            IF modifier IS NULL THEN
                p.err := true;
            END IF;
            EXIT WHEN p.err;
            p := tam_compat._apply_modifier(p, modifier, idx);
        END LOOP;
    END IF;
    p := tam_compat._compute_jd(p);
    IF p.err OR NOT tam_compat._valid_jd(p.jd) THEN
        p.err := true;
        RETURN p;
    END IF;
    IF (modifiers IS NULL OR cardinality(modifiers) = 0) AND p.valid_ymd AND p.d > 28 THEN
        -- Normalize YYYY-MM-DD: 2023-02-31 -> 2023-03-03.
        p.valid_ymd := false;
    END IF;
    RETURN p;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._now_state() RETURNS tam_compat.sqlite_datetime
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT tam_compat._set_now(tam_compat._new_state())
$$;

-- ── formatting ──

CREATE OR REPLACE FUNCTION tam_compat._pad(value integer, width integer) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    -- printf("%0<width>d"): the sign counts toward the width, longer values are kept whole.
    SELECT CASE WHEN length(abs(value)::text) >= width - (value < 0)::integer
                     THEN value::text
                WHEN value < 0 THEN '-' || lpad((-value)::text, width - 1, '0')
                ELSE lpad(value::text, width, '0') END
$$;

CREATE OR REPLACE FUNCTION tam_compat._format_date(p tam_compat.sqlite_datetime) RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
BEGIN
    IF p.err THEN
        RETURN NULL;
    END IF;
    p := tam_compat._compute_ymd(p);
    RETURN CASE WHEN p.y < 0 THEN '-' ELSE '' END || lpad(abs(p.y)::text, 4, '0') || '-'
        || lpad(p.mo::text, 2, '0') || '-' || lpad(p.d::text, 2, '0');
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._format_time(p tam_compat.sqlite_datetime) RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    ms integer;
BEGIN
    IF p.err THEN
        RETURN NULL;
    END IF;
    p := tam_compat._compute_hms(p);
    IF p.subsec THEN
        ms := trunc(1000.0 * p.s + 0.5)::integer;
        RETURN lpad(p.h::text, 2, '0') || ':' || lpad(p.mi::text, 2, '0') || ':'
            || lpad((ms / 1000)::text, 2, '0') || '.' || lpad((ms % 1000)::text, 3, '0');
    END IF;
    RETURN lpad(p.h::text, 2, '0') || ':' || lpad(p.mi::text, 2, '0') || ':'
        || lpad(trunc(p.s)::integer::text, 2, '0');
END
$$;

-- The exact decimal value of a double (mantissa * 2^exponent), which printf rounds.
CREATE OR REPLACE FUNCTION tam_compat._exact(value double precision) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
    shortest numeric := abs(value)::text::numeric;
    exponent integer;
    mantissa numeric;
    digits text;
    places integer;
    result numeric;
BEGIN
    IF value = 0 THEN
        RETURN 0;
    END IF;
    exponent := floor(log(2, shortest))::integer;
    IF shortest >= power(2::numeric, exponent + 1) THEN
        exponent := exponent + 1;
    ELSIF shortest < power(2::numeric, exponent) THEN
        exponent := exponent - 1;
    END IF;
    -- The shortest decimal form is within half an ulp, so rounding recovers the mantissa.
    exponent := exponent - 52;
    IF exponent >= 0 THEN
        result := round(shortest / power(2::numeric, exponent)) * power(2::numeric, exponent);
    ELSE
        mantissa := round(shortest * power(2::numeric, -exponent));
        places := -exponent;
        digits := (mantissa * power(5::numeric, places))::text;
        IF length(digits) <= places THEN
            digits := repeat('0', places - length(digits) + 1) || digits;
        END IF;
        result := (left(digits, length(digits) - places) || '.' || right(digits, places))::numeric;
    END IF;
    RETURN CASE WHEN value < 0 THEN -result ELSE result END;
END
$$;

-- printf("%.16g").
CREATE OR REPLACE FUNCTION tam_compat._g16(value double precision) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
    exact numeric := abs(tam_compat._exact(value));
    magnitude integer;
    rounded numeric;
    digits text;
    result text;
BEGIN
    IF exact = 0 THEN
        RETURN '0';
    END IF;
    magnitude := floor(log(exact))::integer;
    IF exact >= power(10::numeric, magnitude + 1) THEN
        magnitude := magnitude + 1;
    ELSIF exact < power(10::numeric, magnitude) THEN
        magnitude := magnitude - 1;
    END IF;
    rounded := round(exact, 15 - magnitude);
    IF rounded >= power(10::numeric, magnitude + 1) THEN
        magnitude := magnitude + 1;
    END IF;
    IF magnitude < -4 OR magnitude >= 16 THEN
        digits := rtrim(replace(trim(leading '0' from replace(round(rounded * power(10::numeric, -magnitude), 15)::text,
                                                                '.', '')), '-', ''), '0');
        result := left(digits, 1) || CASE WHEN length(digits) > 1 THEN '.' || substr(digits, 2) ELSE '' END
            || 'e' || CASE WHEN magnitude < 0 THEN '-' ELSE '+' END || lpad(abs(magnitude)::text, 2, '0');
    ELSE
        result := rounded::text;
        IF position('.' IN result) > 0 THEN
            result := rtrim(rtrim(result, '0'), '.');
        END IF;
    END IF;
    RETURN CASE WHEN value < 0 THEN '-' ELSE '' END || result;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._days_after_jan01(p tam_compat.sqlite_datetime) RETURNS integer
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    jan01 tam_compat.sqlite_datetime := p;
BEGIN
    jan01.valid_jd := false;
    jan01.mo := 1;
    jan01.d := 1;
    jan01 := tam_compat._compute_jd(jan01);
    RETURN ((p.jd - jan01.jd + 43200000) / 86400000)::integer;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._strftime(format text, p tam_compat.sqlite_datetime) RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    result text := '';
    i integer := 1;
    c text;
    ms integer;
    hour12 integer;
    iso tam_compat.sqlite_datetime;
    after_monday integer;
    after_sunday integer;
BEGIN
    IF format IS NULL OR p.err THEN
        RETURN NULL;
    END IF;
    p := tam_compat._compute_jd(p);
    p := tam_compat._compute_ymd_hms(p);
    after_monday := ((p.jd + 43200000) / 86400000 % 7)::integer;
    after_sunday := ((p.jd + 129600000) / 86400000 % 7)::integer;
    WHILE i <= length(format) LOOP
        c := substr(format, i, 1);
        IF c <> '%' THEN
            result := result || c;
            i := i + 1;
            CONTINUE;
        END IF;
        i := i + 1;
        c := substr(format, i, 1);
        i := i + 1;
        CASE c
            WHEN 'd' THEN result := result || lpad(p.d::text, 2, '0');
            WHEN 'e' THEN result := result || lpad(p.d::text, 2, ' ');
            WHEN 'f' THEN
                ms := trunc(least(p.s, 59.999::double precision) * 1000.0 + 0.5)::integer;
                result := result || lpad((ms / 1000)::text, 2, '0') || '.' || lpad((ms % 1000)::text, 3, '0');
            WHEN 'F' THEN result := result || tam_compat._pad(p.y, 4) || '-' || lpad(p.mo::text, 2, '0') || '-'
                || lpad(p.d::text, 2, '0');
            WHEN 'G', 'g', 'V' THEN
                iso := p;
                iso.jd := iso.jd + (3 - after_monday)::bigint * 86400000;
                iso.valid_ymd := false;
                iso := tam_compat._compute_ymd(iso);
                IF c = 'g' THEN
                    result := result || tam_compat._pad(iso.y % 100, 2);
                ELSIF c = 'G' THEN
                    result := result || tam_compat._pad(iso.y, 4);
                ELSE
                    result := result || lpad((tam_compat._days_after_jan01(iso) / 7 + 1)::text, 2, '0');
                END IF;
            WHEN 'H' THEN result := result || lpad(p.h::text, 2, '0');
            WHEN 'k' THEN result := result || lpad(p.h::text, 2, ' ');
            WHEN 'I', 'l' THEN
                hour12 := p.h;
                IF hour12 > 12 THEN
                    hour12 := hour12 - 12;
                END IF;
                IF hour12 = 0 THEN
                    hour12 := 12;
                END IF;
                result := result || lpad(hour12::text, 2, CASE WHEN c = 'I' THEN '0' ELSE ' ' END);
            WHEN 'j' THEN result := result || lpad((tam_compat._days_after_jan01(p) + 1)::text, 3, '0');
            WHEN 'J' THEN result := result || tam_compat._g16(p.jd / 86400000.0::double precision);
            WHEN 'm' THEN result := result || lpad(p.mo::text, 2, '0');
            WHEN 'M' THEN result := result || lpad(p.mi::text, 2, '0');
            WHEN 'p' THEN result := result || CASE WHEN p.h >= 12 THEN 'PM' ELSE 'AM' END;
            WHEN 'P' THEN result := result || CASE WHEN p.h >= 12 THEN 'pm' ELSE 'am' END;
            WHEN 'R' THEN result := result || lpad(p.h::text, 2, '0') || ':' || lpad(p.mi::text, 2, '0');
            WHEN 's' THEN
                IF p.subsec THEN
                    result := result || to_char((p.jd - 210866760000000) / 1000.0, 'FM999999999990.000');
                ELSE
                    result := result || (p.jd / 1000 - 210866760000)::text;
                END IF;
            WHEN 'S' THEN result := result || lpad(trunc(p.s)::integer::text, 2, '0');
            WHEN 'T' THEN result := result || lpad(p.h::text, 2, '0') || ':' || lpad(p.mi::text, 2, '0') || ':'
                || lpad(trunc(p.s)::integer::text, 2, '0');
            WHEN 'u' THEN result := result || CASE WHEN after_sunday = 0 THEN '7' ELSE after_sunday::text END;
            WHEN 'w' THEN result := result || after_sunday::text;
            WHEN 'U' THEN result := result
                || lpad(((tam_compat._days_after_jan01(p) + 7 - after_sunday) / 7)::text, 2, '0');
            WHEN 'W' THEN result := result
                || lpad(((tam_compat._days_after_jan01(p) + 7 - after_monday) / 7)::text, 2, '0');
            WHEN 'Y' THEN result := result || tam_compat._pad(p.y, 4);
            WHEN '%' THEN result := result || '%';
            ELSE
                RETURN NULL;
        END CASE;
    END LOOP;
    RETURN result;
END
$$;

-- ── fast paths ──
-- The PL/pgSQL evaluator costs ~15 µs per call. Stored timestamps are plain ISO text and
-- the defaults ask for 'now' in two formats, so those cases are computed in plain SQL with
-- the same arithmetic (computeJD, milliseconds rounded as s*1000+0.5 in double precision);
-- anything else returns NULL here and takes the full path.

CREATE OR REPLACE FUNCTION tam_compat._iso_jd(value text) RETURNS bigint
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    core text := value;
    n integer;
    y integer;
    m integer;
    d integer;
    hh integer := 0;
    mm integer := 0;
    ss double precision := 0;
    fraction text;
BEGIN
    -- YYYY-MM-DD[(T| )HH:MM[:SS[.F+]]][Z], checked positionally (PostgreSQL regular
    -- expressions with optional capture groups cost ~50 µs per call).
    IF right(core, 1) = 'Z' THEN
        core := left(core, -1);
    END IF;
    n := length(core);
    IF n < 10 OR substr(core, 5, 1) <> '-' OR substr(core, 8, 1) <> '-'
       OR translate(substr(core, 1, 4) || substr(core, 6, 2) || substr(core, 9, 2), '0123456789', '') <> '' THEN
        RETURN NULL;
    END IF;
    IF n = 10 THEN
        IF core <> value THEN
            RETURN NULL;
        END IF;
    ELSE
        IF n < 16 OR substr(core, 11, 1) NOT IN (' ', 'T') OR substr(core, 14, 1) <> ':'
           OR translate(substr(core, 12, 2) || substr(core, 15, 2), '0123456789', '') <> '' THEN
            RETURN NULL;
        END IF;
        hh := substr(core, 12, 2)::integer;
        mm := substr(core, 15, 2)::integer;
        IF n > 16 THEN
            IF n < 19 OR substr(core, 17, 1) <> ':' OR translate(substr(core, 18, 2), '0123456789', '') <> '' THEN
                RETURN NULL;
            END IF;
            ss := substr(core, 18, 2)::integer;
            IF n > 19 THEN
                fraction := substr(core, 21);
                IF substr(core, 20, 1) <> '.' OR fraction = '' OR translate(fraction, '0123456789', '') <> '' THEN
                    RETURN NULL;
                END IF;
            END IF;
        END IF;
    END IF;
    y := substr(core, 1, 4)::integer;
    m := substr(core, 6, 2)::integer;
    d := substr(core, 9, 2)::integer;
    IF m NOT BETWEEN 1 AND 12 OR d NOT BETWEEN 1 AND 31 OR hh > 24 OR mm > 59 OR ss > 59 THEN
        RETURN NULL;
    END IF;
    IF fraction IS NOT NULL THEN
        ss := ss + fraction::numeric::double precision / power(10::double precision, length(fraction));
    END IF;
    IF m <= 2 THEN
        y := y - 1;
        m := m + 12;
    END IF;
    RETURN ((36525 * (y + 4716) / 100)::bigint + 306001 * (m + 1) / 10000 + d
            + 38 - (y + 4800) / 100 + (y + 4800) / 100 / 4) * 86400000 - 131716800000
        + hh::bigint * 3600000 + mm::bigint * 60000 + trunc(ss * 1000 + 0.5)::bigint;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._now_text(format text) RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT CASE format
        WHEN '%Y-%m-%dT%H:%M:%fZ' THEN to_char(statement_timestamp() AT TIME ZONE 'UTC',
                                               'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
        WHEN '%Y-%m-%d %H:%M:%S' THEN to_char(statement_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
    END
$$;

-- ── public date and time functions ──
-- Overloads: no argument ('now'), a text time value, a numeric time value (Julian day
-- number, or Unix time with 'unixepoch'/'auto'), each optionally followed by modifiers.

CREATE OR REPLACE FUNCTION tam_compat.date() RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$ SELECT tam_compat._format_date(tam_compat._now_state()) $$;
CREATE OR REPLACE FUNCTION tam_compat.date(value text, VARIADIC modifiers text[] DEFAULT '{}') RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT tam_compat._format_date(tam_compat._evaluate(value, false, modifiers))
$$;
CREATE OR REPLACE FUNCTION tam_compat.date(value double precision, VARIADIC modifiers text[] DEFAULT '{}')
RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT tam_compat._format_date(tam_compat._evaluate(value::text, true, modifiers))
$$;

CREATE OR REPLACE FUNCTION tam_compat.time() RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$ SELECT tam_compat._format_time(tam_compat._now_state()) $$;
CREATE OR REPLACE FUNCTION tam_compat.time(value text, VARIADIC modifiers text[] DEFAULT '{}') RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT tam_compat._format_time(tam_compat._evaluate(value, false, modifiers))
$$;
CREATE OR REPLACE FUNCTION tam_compat.time(value double precision, VARIADIC modifiers text[] DEFAULT '{}')
RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT tam_compat._format_time(tam_compat._evaluate(value::text, true, modifiers))
$$;

CREATE OR REPLACE FUNCTION tam_compat._format_datetime(p tam_compat.sqlite_datetime) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT tam_compat._format_date(p) || ' ' || tam_compat._format_time(p)
$$;

CREATE OR REPLACE FUNCTION tam_compat.datetime() RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$ SELECT tam_compat._now_text('%Y-%m-%d %H:%M:%S') $$;
CREATE OR REPLACE FUNCTION tam_compat.datetime(value text, VARIADIC modifiers text[] DEFAULT '{}') RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN lower(value) = 'now' AND cardinality(modifiers) = 0
                THEN tam_compat._now_text('%Y-%m-%d %H:%M:%S')
                ELSE tam_compat._format_datetime(tam_compat._evaluate(value, false, modifiers)) END
$$;
CREATE OR REPLACE FUNCTION tam_compat.datetime(value double precision, VARIADIC modifiers text[] DEFAULT '{}')
RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT tam_compat._format_datetime(tam_compat._evaluate(value::text, true, modifiers))
$$;

CREATE OR REPLACE FUNCTION tam_compat._julianday(p tam_compat.sqlite_datetime) RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN p.err THEN NULL ELSE p.jd / 86400000.0::double precision END
$$;

CREATE OR REPLACE FUNCTION tam_compat.julianday() RETURNS double precision
LANGUAGE sql STABLE PARALLEL SAFE AS $$ SELECT tam_compat._julianday(tam_compat._now_state()) $$;
CREATE OR REPLACE FUNCTION tam_compat.julianday(value text, VARIADIC modifiers text[] DEFAULT '{}')
RETURNS double precision
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT coalesce(CASE WHEN cardinality(modifiers) = 0
                         THEN tam_compat._iso_jd(value) / 86400000.0::double precision END,
                    tam_compat._julianday(tam_compat._evaluate(value, false, modifiers)))
$$;
CREATE OR REPLACE FUNCTION tam_compat.julianday(value double precision, VARIADIC modifiers text[] DEFAULT '{}')
RETURNS double precision
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT tam_compat._julianday(tam_compat._evaluate(value::text, true, modifiers))
$$;

-- numeric: an integer like SQLite's unixepoch(), or seconds with milliseconds under 'subsec'.
CREATE OR REPLACE FUNCTION tam_compat._unixepoch(p tam_compat.sqlite_datetime) RETURNS numeric
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN p.err THEN NULL
                WHEN p.subsec THEN round((p.jd - 210866760000000) / 1000.0, 3)
                ELSE (p.jd / 1000 - 210866760000)::numeric END
$$;

CREATE OR REPLACE FUNCTION tam_compat.unixepoch() RETURNS numeric
LANGUAGE sql STABLE PARALLEL SAFE AS $$ SELECT tam_compat._unixepoch(tam_compat._now_state()) $$;
CREATE OR REPLACE FUNCTION tam_compat.unixepoch(value text, VARIADIC modifiers text[] DEFAULT '{}')
RETURNS numeric
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT tam_compat._unixepoch(tam_compat._evaluate(value, false, modifiers))
$$;
CREATE OR REPLACE FUNCTION tam_compat.unixepoch(value double precision, VARIADIC modifiers text[] DEFAULT '{}')
RETURNS numeric
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT tam_compat._unixepoch(tam_compat._evaluate(value::text, true, modifiers))
$$;

CREATE OR REPLACE FUNCTION tam_compat.strftime(format text) RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$ SELECT tam_compat._strftime(format, tam_compat._now_state()) $$;
CREATE OR REPLACE FUNCTION tam_compat.strftime(format text, value text, VARIADIC modifiers text[] DEFAULT '{}')
RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT coalesce(CASE WHEN lower(value) = 'now' AND cardinality(modifiers) = 0
                         THEN tam_compat._now_text(format) END,
                    tam_compat._strftime(format, tam_compat._evaluate(value, false, modifiers)))
$$;
CREATE OR REPLACE FUNCTION tam_compat.strftime(format text, value double precision,
                                               VARIADIC modifiers text[] DEFAULT '{}')
RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT tam_compat._strftime(format, tam_compat._evaluate(value::text, true, modifiers))
$$;

-- ── JSON ──

-- JSON text without insignificant whitespace, as SQLite's json functions return it.
CREATE OR REPLACE FUNCTION tam_compat.json_compact(value json) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT regexp_replace(value::text, '("(?:[^"\\]|\\.)*")|\s+', '\1', 'g')
$$;
CREATE OR REPLACE FUNCTION tam_compat.json_compact(value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT tam_compat.json_compact(value::json)
$$;

-- The first member named key (SQLite semantics for duplicate keys; json -> takes the last).
CREATE OR REPLACE FUNCTION tam_compat._json_member(document json, key text) RETURNS json
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN json_typeof(document) = 'object' THEN
        (SELECT member.value FROM json_each(document) WITH ORDINALITY AS member(key, value, position)
         WHERE member.key = $2 ORDER BY member.position LIMIT 1)
    END
$$;

-- SQLite path ($, .key, ."quoted key", [N], [#-N]) applied with json operators, so object
-- key order is preserved (jsonb would reorder keys).
CREATE OR REPLACE FUNCTION tam_compat._json_navigate(document json, path text) RETURNS json
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    rest text;
    parts text[];
    current json := document;
BEGIN
    IF path IS NULL OR left(path, 1) <> '$' THEN
        RAISE EXCEPTION 'JSON path error near ''%''', path USING ERRCODE = '22023';
    END IF;
    rest := substr(path, 2);
    WHILE rest <> '' LOOP
        IF current IS NULL THEN
            RETURN NULL;
        END IF;
        IF left(rest, 2) = '."' THEN
            parts := regexp_match(rest, '^\."((?:[^"\\]|\\.)*)"(.*)$');
            IF parts IS NULL THEN
                RAISE EXCEPTION 'JSON path error near ''%''', rest USING ERRCODE = '22023';
            END IF;
            current := tam_compat._json_member(current, replace(replace(parts[1], '\"', '"'), '\\', '\'));
            rest := parts[2];
        ELSIF left(rest, 1) = '.' THEN
            parts := regexp_match(rest, '^\.([^.\[]+)(.*)$');
            IF parts IS NULL THEN
                RAISE EXCEPTION 'JSON path error near ''%''', rest USING ERRCODE = '22023';
            END IF;
            current := tam_compat._json_member(current, parts[1]);
            rest := parts[2];
        ELSIF left(rest, 1) = '[' THEN
            parts := regexp_match(rest, '^\[(#-)?([0-9]+)\](.*)$');
            IF parts IS NULL THEN
                IF rest ~ '^\[#\]' THEN
                    RETURN NULL;
                END IF;
                RAISE EXCEPTION 'JSON path error near ''%''', rest USING ERRCODE = '22023';
            END IF;
            IF json_typeof(current) <> 'array' THEN
                RETURN NULL;
            END IF;
            IF parts[1] IS NOT NULL THEN
                IF parts[2]::integer = 0 THEN
                    RETURN NULL;
                END IF;
                current := current -> (-parts[2]::integer);
            ELSE
                current := current -> parts[2]::integer;
            END IF;
            rest := parts[3];
        ELSE
            RAISE EXCEPTION 'JSON path error near ''%''', rest USING ERRCODE = '22023';
        END IF;
    END LOOP;
    RETURN current;
END
$$;

-- The SQL value SQLite's json_extract() returns, as text: strings unquoted, numbers as
-- written, true/false as 1/0, null as NULL, objects and arrays as compact JSON.
CREATE OR REPLACE FUNCTION tam_compat._json_value(value json) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE json_typeof(value)
               WHEN 'string' THEN value #>> '{}'
               WHEN 'number' THEN value::text
               WHEN 'boolean' THEN CASE WHEN value::text = 'true' THEN '1' ELSE '0' END
               WHEN 'null' THEN NULL
               WHEN 'object' THEN tam_compat.json_compact(value)
               WHEN 'array' THEN tam_compat.json_compact(value)
           END
$$;

CREATE OR REPLACE FUNCTION tam_compat.json_extract(document text, path text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN document IS NULL OR path IS NULL THEN NULL
                ELSE tam_compat._json_value(tam_compat._json_navigate(document::json, path)) END
$$;

-- Several paths: a JSON array of the selected values (SQLite semantics).
CREATE OR REPLACE FUNCTION tam_compat.json_extract(document text, path text, VARIADIC paths text[])
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN document IS NULL THEN NULL ELSE
        '[' || (SELECT string_agg(coalesce(tam_compat.json_compact(tam_compat._json_navigate(document::json, p)),
                                           'null'), ',' ORDER BY ordinality)
                FROM unnest(ARRAY[path] || paths) WITH ORDINALITY AS t(p, ordinality)) || ']'
    END
$$;

-- ── strings ──

CREATE OR REPLACE FUNCTION tam_compat.instr(haystack text, needle text) RETURNS integer
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$ SELECT strpos(haystack, needle) $$;
CREATE OR REPLACE FUNCTION tam_compat.instr(haystack bytea, needle bytea) RETURNS integer
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$ SELECT position(needle IN haystack) $$;

-- hex(): upper-case hex of the UTF-8 text (numbers are rendered as text first); NULL -> ''.
CREATE OR REPLACE FUNCTION tam_compat.hex(value bytea) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT coalesce(upper(encode(value, 'hex')), '') $$;
CREATE OR REPLACE FUNCTION tam_compat.hex(value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(upper(encode(convert_to(value, 'UTF8'), 'hex')), '')
$$;
CREATE OR REPLACE FUNCTION tam_compat.hex(value bigint) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT tam_compat.hex(value::text) $$;
CREATE OR REPLACE FUNCTION tam_compat.hex(value double precision) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT tam_compat.hex(value::text) $$;

CREATE OR REPLACE FUNCTION tam_compat.ifnull(value anycompatible, fallback anycompatible)
RETURNS anycompatible
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT coalesce(value, fallback) $$;

-- GLOB pattern -> anchored regular expression: * ? [...] [^...] ranges, case-sensitive.
CREATE OR REPLACE FUNCTION tam_compat._glob_regex(pattern text) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
    result text := '^';
    i integer := 1;
    j integer;
    c text;
    body text;
BEGIN
    WHILE i <= length(pattern) LOOP
        c := substr(pattern, i, 1);
        IF c = '*' THEN
            result := result || '.*';
        ELSIF c = '?' THEN
            result := result || '.';
        ELSIF c = '[' THEN
            j := i + 1;
            IF substr(pattern, j, 1) = '^' THEN
                j := j + 1;
            END IF;
            IF substr(pattern, j, 1) = ']' THEN
                j := j + 1;
            END IF;
            WHILE j <= length(pattern) AND substr(pattern, j, 1) <> ']' LOOP
                j := j + 1;
            END LOOP;
            IF j > length(pattern) THEN
                -- An unterminated class never matches in SQLite.
                RETURN '[^\s\S]';
            END IF;
            body := substr(pattern, i + 1, j - i - 1);
            IF left(body, 1) = '^' THEN
                body := '^' || replace(replace(substr(body, 2), '\', '\\'), '[', '\[');
            ELSE
                body := replace(replace(body, '\', '\\'), '[', '\[');
            END IF;
            IF body LIKE ']%' OR body LIKE '^]%' THEN
                body := regexp_replace(body, '^(\^?)\]', '\1\\]');
            END IF;
            result := result || '[' || body || ']';
            i := j;
        ELSE
            result := result || regexp_replace(c, '([.^$|()\[\]{}*+?\\])', '\\\1');
        END IF;
        i := i + 1;
    END LOOP;
    RETURN result || '$';
END
$$;

CREATE OR REPLACE FUNCTION tam_compat.glob(pattern text, value text) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT value ~ tam_compat._glob_regex(pattern)
$$;

-- "value GLOB pattern" (operator argument order), emitted by the translator.
CREATE OR REPLACE FUNCTION tam_compat.glob_match(value text, pattern text) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$ SELECT tam_compat.glob(pattern, value) $$;

-- ── aggregates and scalar max/min ──

CREATE OR REPLACE FUNCTION tam_compat._group_concat_step(state text, value anynonarray, separator text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN value IS NULL THEN state
                WHEN state IS NULL THEN value::text
                ELSE state || coalesce(separator, '') || value::text END
$$;
CREATE OR REPLACE FUNCTION tam_compat._group_concat_step(state text, value anynonarray) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT tam_compat._group_concat_step(state, value, ',') $$;

CREATE OR REPLACE AGGREGATE tam_compat.group_concat(anynonarray) (
    SFUNC = tam_compat._group_concat_step, STYPE = text
);
CREATE OR REPLACE AGGREGATE tam_compat.group_concat(anynonarray, text) (
    SFUNC = tam_compat._group_concat_step, STYPE = text
);

-- Multi-argument max()/min() are scalar in SQLite and NULL when any argument is NULL.
CREATE OR REPLACE FUNCTION tam_compat.max(a anycompatible, b anycompatible) RETURNS anycompatible
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN a IS NULL OR b IS NULL THEN NULL WHEN a >= b THEN a ELSE b END
$$;
CREATE OR REPLACE FUNCTION tam_compat.max(a anycompatible, b anycompatible, c anycompatible)
RETURNS anycompatible
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT tam_compat.max(tam_compat.max(a, b), c) $$;
CREATE OR REPLACE FUNCTION tam_compat.min(a anycompatible, b anycompatible) RETURNS anycompatible
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN a IS NULL OR b IS NULL THEN NULL WHEN a <= b THEN a ELSE b END
$$;
CREATE OR REPLACE FUNCTION tam_compat.min(a anycompatible, b anycompatible, c anycompatible)
RETURNS anycompatible
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT tam_compat.min(tam_compat.min(a, b), c) $$;

-- SQLite round(): with no digits r + 0.5 truncated in double arithmetic, otherwise the
-- exact binary value rounded half away from zero; always a real.
CREATE OR REPLACE FUNCTION tam_compat.round(value double precision, digits integer DEFAULT 0)
RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN value IS NULL OR digits IS NULL THEN NULL
        WHEN least(greatest(digits, 0), 30) = 0 AND abs(value) < 9223372036854775806 THEN
            CASE WHEN value >= 0 THEN trunc(value + 0.5::double precision)
                 ELSE -trunc(-value + 0.5::double precision) END
        ELSE round(tam_compat._exact(value), least(greatest(digits, 0), 30))::double precision
    END
$$;

-- ── catalog ──

-- sqlite_master for the connection's current schema: tables, indexes, views, triggers.
CREATE OR REPLACE VIEW tam_compat.sqlite_master AS
SELECT 'table'::text AS type, c.relname::text AS name, c.relname::text AS tbl_name, 0 AS rootpage,
       'CREATE TABLE ' || quote_ident(c.relname) || ' (' || concat_ws(', ',
           (SELECT string_agg(quote_ident(a.attname) || ' ' || format_type(a.atttypid, a.atttypmod)
                              || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
                              || coalesce(' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid), ''),
                              ', ' ORDER BY a.attnum)
            FROM pg_attribute a
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped),
           (SELECT string_agg(pg_get_constraintdef(con.oid), ', ' ORDER BY con.conname)
            FROM pg_constraint con WHERE con.conrelid = c.oid AND con.contype IN ('p', 'u', 'c', 'f'))
       ) || ')' AS sql
FROM pg_class c
WHERE c.relnamespace = current_schema()::regnamespace AND c.relkind IN ('r', 'p')
UNION ALL
SELECT 'index', ic.relname::text, tc.relname::text, 0, pg_get_indexdef(i.indexrelid)
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_class tc ON tc.oid = i.indrelid
WHERE ic.relnamespace = current_schema()::regnamespace
UNION ALL
SELECT 'view', c.relname::text, c.relname::text, 0,
       'CREATE VIEW ' || quote_ident(c.relname) || ' AS ' || pg_get_viewdef(c.oid)
FROM pg_class c
WHERE c.relnamespace = current_schema()::regnamespace AND c.relkind IN ('v', 'm')
UNION ALL
SELECT 'trigger', t.tgname::text, c.relname::text, 0, pg_get_triggerdef(t.oid)
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
WHERE c.relnamespace = current_schema()::regnamespace AND NOT t.tgisinternal;

GRANT SELECT ON tam_compat.sqlite_master TO PUBLIC;
