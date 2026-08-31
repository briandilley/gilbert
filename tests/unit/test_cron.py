"""Tests for the cron expression engine.

The DST and day-of-month/day-of-week cases are the reason this module
exists rather than a third-party dependency, so they are asserted
directly rather than through the scheduler.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gilbert.interfaces.cron import (
    CronKind,
    CronParseError,
    parse,
)

LA = ZoneInfo("America/Los_Angeles")


def _at(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=LA)


# --- Parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "expr,kind",
    [
        ("30 3 * * *", CronKind.FIELDS),
        ("*/30 * * * * *", CronKind.FIELDS),
        ("0 9 * * MON-FRI", CronKind.FIELDS),
        ("@daily", CronKind.FIELDS),
        ("@yearly", CronKind.FIELDS),
        ("@annually", CronKind.FIELDS),
        ("@monthly", CronKind.FIELDS),
        ("@weekly", CronKind.FIELDS),
        ("@midnight", CronKind.FIELDS),
        ("@hourly", CronKind.FIELDS),
        ("@every 90s", CronKind.EVERY),
        ("@every 1h30m", CronKind.EVERY),
        ("@reboot", CronKind.ONCE),
        ("@once", CronKind.ONCE),
        ("@once+45s", CronKind.ONCE),
    ],
)
def test_parse_dialect_rows(expr: str, kind: CronKind) -> None:
    assert parse(expr).kind is kind


def test_parse_five_field_defaults_seconds_to_zero() -> None:
    assert parse("30 3 * * *").seconds == frozenset({0})


def test_parse_six_field_reads_leading_seconds() -> None:
    expr = parse("*/30 * * * * *")
    assert expr.seconds == frozenset({0, 30})


def test_parse_ranges_lists_and_steps() -> None:
    expr = parse("0-30/10 1,3,5 * * *")
    assert expr.minutes == frozenset({0, 10, 20, 30})
    assert expr.hours == frozenset({1, 3, 5})


def test_parse_bare_value_with_step_runs_to_top_of_range() -> None:
    assert parse("5/15 * * * *").minutes == frozenset({5, 20, 35, 50})


def test_parse_mixed_list_does_not_leak_step_across_items() -> None:
    """``1,5/15`` — the bare 1 must stay a single value."""
    assert parse("1,5/15 * * * *").minutes == frozenset({1, 5, 20, 35, 50})


def test_parse_month_and_day_names() -> None:
    assert parse("0 0 * JAN,MAR *").months == frozenset({1, 3})
    assert parse("0 0 * * MON-FRI").days_of_week == frozenset({1, 2, 3, 4, 5})


def test_parse_sunday_seven_normalises_to_zero() -> None:
    assert parse("0 0 * * 7").days_of_week == frozenset({0})


def test_parse_wrapping_range() -> None:
    assert parse("0 0 * * FRI-MON").days_of_week == frozenset({5, 6, 0, 1})


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "1 2 3",
        "1 2 3 4 5 6 7",
        "0 99 * * *",
        "0 0 * * NOTADAY",
        "@bogus",
        "@every",
        "@every 0s",
        "0 0/0 * * *",
    ],
)
def test_parse_rejects_invalid(expr: str) -> None:
    with pytest.raises(CronParseError):
        parse(expr)


def test_parse_error_names_the_field() -> None:
    with pytest.raises(CronParseError, match="hours"):
        parse("0 99 * * *")


# --- Basic next_after -------------------------------------------------


def test_next_after_daily() -> None:
    nxt = parse("30 3 * * *").next_after(_at(2026, 4, 1, 12, 0), LA)
    assert nxt == _at(2026, 4, 2, 3, 30)


def test_next_after_same_day_when_still_ahead() -> None:
    nxt = parse("30 3 * * *").next_after(_at(2026, 4, 1, 1, 0), LA)
    assert nxt == _at(2026, 4, 1, 3, 30)


def test_next_after_is_strictly_after() -> None:
    """Landing exactly on a fire time must yield the *next* one."""
    nxt = parse("30 3 * * *").next_after(_at(2026, 4, 1, 3, 30), LA)
    assert nxt == _at(2026, 4, 2, 3, 30)


def test_next_after_seconds_granularity() -> None:
    nxt = parse("*/30 * * * * *").next_after(_at(2026, 4, 1, 12, 0, 5), LA)
    assert nxt == _at(2026, 4, 1, 12, 0, 30)


def test_next_after_weekday_expression() -> None:
    """Friday 2026-04-03 at 10:00 -> next weekday 9am is Monday the 6th."""
    nxt = parse("0 9 * * MON-FRI").next_after(_at(2026, 4, 3, 10, 0), LA)
    assert nxt == _at(2026, 4, 6, 9, 0)


def test_next_after_month_rollover() -> None:
    nxt = parse("0 0 1 * *").next_after(_at(2026, 1, 15), LA)
    assert nxt == _at(2026, 2, 1, 0, 0)


def test_next_after_impossible_expression_returns_none() -> None:
    """Feb 30 never happens — the walk must terminate, not spin."""
    assert parse("0 0 30 2 *").next_after(_at(2026, 1, 1), LA) is None


def test_next_after_yearly_is_cheap_and_correct() -> None:
    nxt = parse("@yearly").next_after(_at(2026, 6, 1), LA)
    assert nxt == _at(2027, 1, 1, 0, 0)


def test_next_after_accepts_naive_input() -> None:
    nxt = parse("30 3 * * *").next_after(datetime(2026, 4, 1, 12, 0), LA)
    assert nxt == _at(2026, 4, 2, 3, 30)


# --- The day-of-month / day-of-week OR rule ---------------------------


def test_dom_and_dow_both_restricted_is_an_or() -> None:
    """``0 0 13 * FRI`` fires on the 13th *or* any Friday."""
    expr = parse("0 0 13 * FRI")
    # 2026-03-13 is itself a Friday; the 6th is a Friday but not the 13th.
    assert expr.next_after(_at(2026, 3, 1), LA) == _at(2026, 3, 6, 0, 0)
    # The 13th of a month whose 13th is not a Friday still matches.
    assert expr.next_after(_at(2026, 4, 11), LA) == _at(2026, 4, 13, 0, 0)


def test_only_dom_restricted_ignores_dow() -> None:
    expr = parse("0 0 13 * *")
    assert expr.next_after(_at(2026, 4, 1), LA) == _at(2026, 4, 13, 0, 0)


def test_only_dow_restricted_ignores_dom() -> None:
    expr = parse("0 0 * * WED")
    assert expr.next_after(_at(2026, 4, 1, 12), LA) == _at(2026, 4, 8, 0, 0)


def test_question_mark_means_unrestricted() -> None:
    expr = parse("0 0 ? * WED")
    assert not expr.dom_restricted
    assert expr.next_after(_at(2026, 4, 1, 12), LA) == _at(2026, 4, 8, 0, 0)


# --- Quartz day extensions --------------------------------------------


def test_last_day_of_month() -> None:
    nxt = parse("0 0 L * *").next_after(_at(2026, 4, 1), LA)
    assert nxt == _at(2026, 4, 30, 0, 0)


def test_last_day_of_month_february() -> None:
    nxt = parse("0 0 L * *").next_after(_at(2026, 2, 1), LA)
    assert nxt == _at(2026, 2, 28, 0, 0)


def test_last_day_offset() -> None:
    """``L-3`` is three days before the last day."""
    nxt = parse("0 0 L-3 * *").next_after(_at(2026, 4, 1), LA)
    assert nxt == _at(2026, 4, 27, 0, 0)


def test_nth_weekday() -> None:
    """``FRI#3`` — the third Friday of April 2026 is the 17th."""
    nxt = parse("0 0 * * FRI#3").next_after(_at(2026, 4, 1), LA)
    assert nxt == _at(2026, 4, 17, 0, 0)


def test_last_weekday_of_month() -> None:
    """``FRI L`` form — the last Friday of April 2026 is the 24th."""
    nxt = parse("0 0 * * 5L").next_after(_at(2026, 4, 1), LA)
    assert nxt == _at(2026, 4, 24, 0, 0)


def test_nearest_weekday_shifts_off_saturday() -> None:
    """2026-08-15 is a Saturday, so ``15W`` fires Friday the 14th."""
    nxt = parse("0 0 15W * *").next_after(_at(2026, 8, 1), LA)
    assert nxt == _at(2026, 8, 14, 0, 0)


def test_nearest_weekday_shifts_off_sunday() -> None:
    """2026-11-15 is a Sunday, so ``15W`` fires Monday the 16th."""
    nxt = parse("0 0 15W * *").next_after(_at(2026, 11, 1), LA)
    assert nxt == _at(2026, 11, 16, 0, 0)


def test_bare_dow_still_matches_alongside_positional() -> None:
    """``MON,FRI#3`` matches every Monday and the third Friday."""
    expr = parse("0 0 * * MON,FRI#3")
    assert expr.next_after(_at(2026, 4, 1), LA) == _at(2026, 4, 6, 0, 0)


# --- DST --------------------------------------------------------------
#
# America/Los_Angeles 2026: spring forward Sun Mar 8 (02:00 -> 03:00),
# fall back Sun Nov 1 (02:00 -> 01:00).


def test_spring_forward_nonexistent_hour_fires_at_transition() -> None:
    """02:30 does not exist on 2026-03-08; fire once when the clock jumps."""
    nxt = parse("30 2 * * *").next_after(_at(2026, 3, 7, 12, 0), LA)
    assert nxt == _at(2026, 3, 8, 3, 0)
    assert nxt.utcoffset() == timedelta(hours=-7)


def test_spring_forward_resumes_normally_the_next_day() -> None:
    after_transition = parse("30 2 * * *").next_after(_at(2026, 3, 8, 4, 0), LA)
    assert after_transition == _at(2026, 3, 9, 2, 30)


def test_fall_back_ambiguous_hour_fires_only_on_first_occurrence() -> None:
    """01:30 happens twice on 2026-11-01; take the earlier (PDT) one."""
    nxt = parse("30 1 * * *").next_after(_at(2026, 10, 31, 12, 0), LA)
    assert nxt.utcoffset() == timedelta(hours=-7)  # PDT, the first pass
    assert nxt.astimezone(UTC) == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)


def test_fall_back_does_not_fire_twice() -> None:
    """From just after the first 01:30, the next fire is the following day."""
    first = parse("30 1 * * *").next_after(_at(2026, 10, 31, 12, 0), LA)
    second = parse("30 1 * * *").next_after(first, LA)
    assert second.date() == datetime(2026, 11, 2).date()


def test_wildcard_hours_span_spring_forward_without_repeats() -> None:
    """An every-15-minutes job just follows real elapsed time."""
    expr = parse("*/15 * * * *")
    cur = _at(2026, 3, 8, 1, 30)
    fires = []
    for _ in range(6):
        cur = expr.next_after(cur, LA)
        fires.append(cur)
    assert len(set(fires)) == len(fires)
    # Subtracting two aware datetimes that share a tzinfo OBJECT makes
    # Python ignore the offsets and subtract wall clock, which reads the
    # spring-forward jump as 75 minutes. Real elapsed time needs UTC.
    instants = [f.astimezone(UTC) for f in fires]
    gaps = [
        (b - a).total_seconds()
        for a, b in zip(instants, instants[1:], strict=False)
    ]
    assert all(g == 900 for g in gaps)


def test_wildcard_hours_span_fall_back_without_skipping_the_repeated_hour() -> None:
    """The counterpart to the spring-forward test, and the case a naive
    wall-clock walk gets wrong: an hour REPEATS on the fall-back day, and
    for a wildcard-hour expression both passes are real fires. Skipping
    the second pass leaves a silent 75-minute hole in an every-15-minutes
    schedule."""
    expr = parse("*/15 * * * *")
    cur = _at(2026, 11, 1, 0, 30)
    fires = []
    for _ in range(12):
        cur = expr.next_after(cur, LA)
        fires.append(cur)

    instants = [f.astimezone(UTC) for f in fires]
    assert len(set(instants)) == len(instants)
    gaps = [
        (b - a).total_seconds()
        for a, b in zip(instants, instants[1:], strict=False)
    ]
    assert all(g == 900 for g in gaps), gaps

    # The repeated hour must appear twice, once at each offset.
    one_am = [f for f in fires if f.hour == 1]
    assert {f.utcoffset() for f in one_am} == {
        timedelta(hours=-7),
        timedelta(hours=-8),
    }


def test_hourly_expression_fires_in_both_passes_of_the_repeated_hour() -> None:
    """``30 * * * *`` is wildcard-hour, so 01:30 happens twice — an hour
    apart in real time."""
    expr = parse("30 * * * *")
    cur = _at(2026, 11, 1, 0, 0)
    fires = []
    for _ in range(4):
        cur = expr.next_after(cur, LA)
        fires.append(cur)
    at_0130 = [f for f in fires if (f.hour, f.minute) == (1, 30)]
    assert len(at_0130) == 2
    assert (
        at_0130[1].astimezone(UTC) - at_0130[0].astimezone(UTC)
    ).total_seconds() == 3600


def test_hour_anchored_expression_does_not_gain_a_second_fall_back_fire() -> None:
    """The fold=1 pass must not leak into hour-anchored expressions —
    those still fire exactly once when their local time repeats."""
    expr = parse("30 1 * * *")
    assert expr.hour_anchored
    cur = _at(2026, 10, 30, 12, 0)
    fires = []
    for _ in range(4):
        cur = expr.next_after(cur, LA)
        fires.append(cur)
    nov_1 = [f for f in fires if (f.month, f.day) == (11, 1)]
    assert len(nov_1) == 1
    assert nov_1[0].utcoffset() == timedelta(hours=-7)


def test_repeated_hour_pass_respects_seconds_granularity() -> None:
    expr = parse("*/30 * * * * *")
    cur = _at(2026, 11, 1, 0, 59, 0)
    instants = []
    for _ in range(8):
        cur = expr.next_after(cur, LA)
        instants.append(cur.astimezone(UTC))
    gaps = [
        (b - a).total_seconds()
        for a, b in zip(instants, instants[1:], strict=False)
    ]
    assert all(g == 30 for g in gaps), gaps


def test_normal_days_are_unaffected_by_the_repeated_hour_pass() -> None:
    """Away from a transition the fold=1 scan must never fire."""
    expr = parse("*/15 * * * *")
    cur = _at(2026, 6, 15, 0, 0)
    instants = []
    for _ in range(20):
        cur = expr.next_after(cur, LA)
        instants.append(cur.astimezone(UTC))
    gaps = [
        (b - a).total_seconds()
        for a, b in zip(instants, instants[1:], strict=False)
    ]
    assert all(g == 900 for g in gaps)


def test_timezone_is_honoured_not_host_local() -> None:
    """The same expression resolves differently in two zones."""
    expr = parse("30 3 * * *")
    la = expr.next_after(datetime(2026, 4, 1, 0, 0, tzinfo=UTC), LA)
    ny = expr.next_after(
        datetime(2026, 4, 1, 0, 0, tzinfo=UTC), ZoneInfo("America/New_York")
    )
    assert la.astimezone(UTC) != ny.astimezone(UTC)


# --- @every -----------------------------------------------------------


def test_every_advances_by_its_interval() -> None:
    expr = parse("@every 90s")
    anchor = _at(2026, 4, 1, 12, 0)
    assert expr.next_after(anchor, LA, anchor=anchor) == anchor + timedelta(seconds=90)


def test_every_without_anchor_measures_from_now() -> None:
    expr = parse("@every 90s")
    now = _at(2026, 4, 1, 12, 0)
    assert expr.next_after(now, LA) == now + timedelta(seconds=90)


def test_every_preserves_phase_after_a_long_pause() -> None:
    """A suspend must not restart the cadence off an arbitrary moment."""
    expr = parse("@every 60s")
    anchor = _at(2026, 4, 1, 12, 0, 0)
    # Woke up 10.5 minutes later.
    now = anchor + timedelta(seconds=630)
    nxt = expr.next_after(now, LA, anchor=anchor)
    assert nxt > now
    assert (nxt - anchor).total_seconds() % 60 == 0


def test_every_duration_forms() -> None:
    assert parse("@every 1h30m").every_seconds == 5400
    assert parse("@every 45s").every_seconds == 45
    assert parse("@every 2h").every_seconds == 7200


def test_every_is_dst_immune() -> None:
    """A pure duration crosses the spring-forward gap unchanged."""
    expr = parse("@every 3600s")
    anchor = _at(2026, 3, 8, 1, 30)
    nxt = expr.next_after(anchor, LA, anchor=anchor)
    assert (nxt - anchor).total_seconds() == 3600


# --- @once / @reboot --------------------------------------------------


def test_once_fires_after_its_delay() -> None:
    expr = parse("@once+45s")
    anchor = _at(2026, 4, 1, 12, 0)
    assert expr.next_after(anchor, LA, anchor=anchor) == anchor + timedelta(seconds=45)


def test_once_retires_after_the_moment_passes() -> None:
    expr = parse("@once+45s")
    anchor = _at(2026, 4, 1, 12, 0)
    later = anchor + timedelta(seconds=60)
    assert expr.next_after(later, LA, anchor=anchor) is None


def test_reboot_is_zero_delay_once() -> None:
    expr = parse("@reboot")
    assert expr.is_one_shot
    assert expr.once_delay_seconds == 0.0


def test_zero_delay_once_fires_immediately() -> None:
    """``@reboot`` / ``@once+0s`` must fire at the anchor, not never.

    The delay is zero, so the fire instant *equals* the anchor. A
    strict "must be after" comparison retires the job before it has
    ever run — which silently stranded every boot job in the app.
    """
    for raw in ("@reboot", "@once", "@once+0s"):
        expr = parse(raw)
        anchor = _at(2026, 4, 1, 12, 0)
        assert expr.next_after(anchor, LA, anchor=anchor) == anchor, raw


def test_zero_delay_once_still_retires_after_its_moment() -> None:
    """Firing at the anchor must not make it fire forever."""
    expr = parse("@reboot")
    anchor = _at(2026, 4, 1, 12, 0)
    later = anchor + timedelta(seconds=1)
    assert expr.next_after(later, LA, anchor=anchor) is None


def test_once_accepts_bare_seconds() -> None:
    assert parse("@once+45").once_delay_seconds == 45.0


# --- describe() -------------------------------------------------------


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("30 3 * * *", "daily at 03:30"),
        ("15 * * * *", "hourly at :15"),
        ("@every 90s", "every 1m 30s"),
        ("@every 30s", "every 30 seconds"),
        ("@reboot", "once at startup"),
        ("@once+45s", "once, 45 seconds after startup"),
        ("*/5 * * * *", "every 5 minutes"),
    ],
)
def test_describe(expr: str, expected: str) -> None:
    assert parse(expr).describe() == expected


def test_describe_falls_back_to_raw_for_complex_expressions() -> None:
    raw = "0 9 * * MON-FRI"
    assert parse(raw).describe() == raw
