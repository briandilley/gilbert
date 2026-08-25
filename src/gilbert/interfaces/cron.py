"""Cron expression parsing and next-fire evaluation.

Pure standard library — this module is the scheduler's single source of
truth for *when* a job runs, and ``interfaces/`` may not depend on
anything outside stdlib.

The dialect deliberately spans three families so that every schedule
Gilbert needs is expressible as one expression string:

- **POSIX / Vixie 5-field** (``30 3 * * *``) plus the standard macros
  (``@daily``, ``@hourly``, …) and ``@reboot``.
- **Quartz extensions** — an optional leading seconds field
  (``*/30 * * * * *``) and the day specifiers ``L``, ``W``, ``#``, ``?``.
- **``robfig/cron`` interval macro** — ``@every 90s``, ``@every 1h30m``.

Only ``@once+<delay>`` is a Gilbert addition, and it generalises the
standard ``@reboot`` macro (``@reboot`` is exactly ``@once+0s``).

Two semantics are easy to get subtly wrong and are therefore specified
here, implemented deliberately, and covered by direct tests:

**Day-of-month / day-of-week OR rule.** When *both* the DOM and DOW
fields are restricted (neither is ``*`` nor ``?``), a day matches if
*either* matches. When only one is restricted, only that one applies.
This is classic Vixie behaviour and the most common source of silent
incompatibility between cron implementations.

**DST.** Field expressions are evaluated against local wall-clock time
in the job's timezone, then localised:

- A job anchored to an hour that does not exist on the spring-forward
  day fires **once**, at the transition instant.
- A job anchored to an ambiguous hour on the fall-back day fires on the
  **first** occurrence only, never both.
- Wildcard-hour expressions follow real elapsed time across both
  transitions — no skips, no repeats.
- ``@every`` is a pure duration: timezone-independent and DST-immune.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Any

__all__ = [
    "CronExpression",
    "CronKind",
    "CronParseError",
    "parse",
]


class CronParseError(ValueError):
    """Raised when an expression cannot be parsed.

    Carries a message naming the offending field so a user fixing a
    typo in the Settings UI gets something actionable rather than
    "invalid expression".
    """


class CronKind(StrEnum):
    """Which family an expression belongs to."""

    #: A 5- or 6-field recurring expression.
    FIELDS = "fields"
    #: ``@every <duration>`` — a pure interval, anchored on last fire.
    EVERY = "every"
    #: ``@once``/``@once+<delay>``/``@reboot`` — a single fire after startup.
    ONCE = "once"


# --- Field bounds -----------------------------------------------------

_SECOND_RANGE = (0, 59)
_MINUTE_RANGE = (0, 59)
_HOUR_RANGE = (0, 23)
_DOM_RANGE = (1, 31)
_MONTH_RANGE = (1, 12)
#: 0 and 7 both mean Sunday; 7 is normalised to 0 during parsing.
_DOW_RANGE = (0, 7)

_MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_DOW_NAMES = {
    "SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6,
}

#: Standard macros, expanded to their 5-field equivalents.
_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

#: How far ahead ``next_after`` will search before giving up. Bounds the
#: walk for expressions that can never match (``0 0 30 2 *`` — Feb 30).
_SEARCH_HORIZON_YEARS = 5

#: Maximum minutes scanned to find the far edge of a spring-forward gap.
#: Real gaps are 30-120 minutes; this is a safety bound, not a tuning knob.
_MAX_GAP_SCAN_MINUTES = 180

_DURATION_RE = re.compile(
    r"(?:(?P<hours>\d+(?:\.\d+)?)h)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)m)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)s)?$"
)


@dataclass(frozen=True)
class CronExpression:
    """A parsed expression, with all field values pre-expanded.

    Parsing happens once at schedule-construction time; ``next_after``
    then does pure set membership tests, so evaluating a job's next
    fire never re-parses.
    """

    raw: str
    kind: CronKind

    # --- FIELDS ---
    seconds: frozenset[int] = frozenset()
    minutes: frozenset[int] = frozenset()
    hours: frozenset[int] = frozenset()
    days_of_month: frozenset[int] = frozenset()
    months: frozenset[int] = frozenset()
    days_of_week: frozenset[int] = frozenset()
    #: True when the field is neither ``*`` nor ``?`` — drives the OR rule.
    dom_restricted: bool = False
    dow_restricted: bool = False
    #: ``L`` in the day-of-month field.
    last_day_of_month: bool = False
    #: ``L-3`` — n days before the last day of the month.
    last_dom_offset: int | None = None
    #: ``5L`` — the last Friday of the month.
    last_dow: int | None = None
    #: ``FRI#3`` — the 3rd Friday, as ``(dow, nth)``.
    nth_dow: tuple[int, int] | None = None
    #: ``15W`` — the weekday nearest the 15th.
    nearest_weekday: int | None = None

    # --- EVERY / ONCE ---
    every_seconds: float = 0.0
    once_delay_seconds: float = 0.0

    @property
    def is_one_shot(self) -> bool:
        """True for ``@once``/``@reboot`` — fires exactly once, then retires."""
        return self.kind is CronKind.ONCE

    def next_after(
        self,
        after: datetime,
        tz: tzinfo | None = None,
        *,
        anchor: datetime | None = None,
    ) -> datetime | None:
        """The first fire strictly after ``after``.

        ``after`` may be naive (interpreted in ``tz``) or aware. The
        return is always aware, in ``tz``.

        ``anchor`` is the reference point for the non-field kinds:
        ``EVERY`` measures its interval from it, and ``ONCE`` measures
        its delay from it. It defaults to ``after``. For a recurring
        interval the caller passes the previous fire so that drift does
        not accumulate across long sleeps.

        Returns ``None`` when nothing will ever match — a one-shot that
        already fired, or an impossible date like ``0 0 30 2 *``.
        """
        zone = tz if tz is not None else UTC
        if after.tzinfo is None:
            after = after.replace(tzinfo=zone)

        if self.kind is CronKind.EVERY:
            base = anchor if anchor is not None else after
            if base.tzinfo is None:
                base = base.replace(tzinfo=zone)
            candidate = base + timedelta(seconds=self.every_seconds)
            # A long pause (host suspend) can leave the anchor far in the
            # past. Advance in whole intervals so the phase of the
            # schedule is preserved rather than restarted from "now".
            if candidate.astimezone(UTC) <= after.astimezone(UTC):
                elapsed = (
                    after.astimezone(UTC) - candidate.astimezone(UTC)
                ).total_seconds()
                missed = elapsed // self.every_seconds
                candidate += timedelta(seconds=self.every_seconds * (missed + 1))
            return candidate.astimezone(zone)

        if self.kind is CronKind.ONCE:
            base = anchor if anchor is not None else after
            if base.tzinfo is None:
                base = base.replace(tzinfo=zone)
            candidate = base + timedelta(seconds=self.once_delay_seconds)
            # One-shot: once the moment has passed, it never fires again.
            if candidate.astimezone(UTC) <= after.astimezone(UTC):
                return None
            return candidate.astimezone(zone)

        return self._next_fields(after.astimezone(zone), zone)

    # --- Field walk ---------------------------------------------------

    def _next_fields(self, after: datetime, zone: tzinfo) -> datetime | None:
        """Walk coarse-to-fine to the next matching wall-clock time.

        Advancing a coarse field resets every finer one, so a yearly
        expression costs a handful of iterations rather than a scan of
        every minute in the year.
        """
        has_seconds = self.seconds != frozenset({0}) or len(self.seconds) > 1
        step = timedelta(seconds=1) if has_seconds else timedelta(minutes=1)

        cur = after.replace(tzinfo=None, microsecond=0) + step
        if not has_seconds:
            cur = cur.replace(second=0)
        horizon = cur.replace(year=cur.year + _SEARCH_HORIZON_YEARS)

        while cur < horizon:
            if cur.month not in self.months:
                cur = _first_of_next_month(cur)
                continue
            if not self._day_matches(cur):
                cur = (cur + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0
                )
                continue
            if cur.hour not in self.hours:
                cur = (cur + timedelta(hours=1)).replace(minute=0, second=0)
                continue
            if cur.minute not in self.minutes:
                cur = (cur + timedelta(minutes=1)).replace(second=0)
                continue
            if cur.second not in self.seconds:
                cur = cur + timedelta(seconds=1)
                continue

            localised = _localise(cur, zone)
            if localised is None:
                # Spring-forward gap. The wall-clock time the user asked
                # for does not exist today; fire once at the instant the
                # clock jumps past it.
                localised = _first_valid_after_gap(cur, zone)
                if localised is None:
                    cur = cur + step
                    continue
            if localised.astimezone(UTC) <= after.astimezone(UTC):
                # Fall-back: the same wall-clock time occurs twice. We
                # take the first occurrence, so the second one is behind
                # us and must be skipped rather than fired again.
                cur = cur + step
                continue
            return localised

        return None

    def _day_matches(self, d: datetime) -> bool:
        """Apply the DOM/DOW OR rule to a candidate date."""
        if self.dom_restricted and self.dow_restricted:
            return self._dom_matches(d) or self._dow_matches(d)
        if self.dom_restricted:
            return self._dom_matches(d)
        if self.dow_restricted:
            return self._dow_matches(d)
        return True

    def _dom_matches(self, d: datetime) -> bool:
        """Day-of-month, including the ``L`` / ``L-n`` / ``nW`` forms.

        The special forms are alternatives to the plain set, not
        replacements, so ``1,L`` matches both the first and last day.
        """
        if d.day in self.days_of_month:
            return True
        last = _last_day_of_month(d.year, d.month)
        if self.last_day_of_month and d.day == last:
            return True
        if self.last_dom_offset is not None and d.day == last - self.last_dom_offset:
            return True
        if self.nearest_weekday is not None:
            target = min(self.nearest_weekday, last)
            if d.day == _nearest_weekday_to(d.year, d.month, target):
                return True
        return False

    def _dow_matches(self, d: datetime) -> bool:
        """Day-of-week, including the ``nL`` (last) and ``n#k`` (nth) forms."""
        dow = _cron_dow(d)
        if self.nth_dow is not None:
            want_dow, nth = self.nth_dow
            if dow == want_dow and ((d.day - 1) // 7) + 1 == nth:
                return True
        if self.last_dow is not None:
            if dow == self.last_dow and d.day + 7 > _last_day_of_month(
                d.year, d.month
            ):
                return True
        # A positional form having failed, fall through to any bare
        # values the field also listed (``MON,FRI#3`` matches both).
        return dow in self.days_of_week

    # --- Human-readable summary ---------------------------------------

    def describe(self) -> str:
        """A short human summary, for the SPA and ``list_timers``.

        Returning prose from here is what keeps a cron parser out of the
        TypeScript frontend — the backend already has the parsed form.
        Uncommon expressions fall back to the raw string, which is
        honest rather than wrong.
        """
        if self.kind is CronKind.EVERY:
            if self.every_seconds == 1:
                return "every second"
            return f"every {_format_duration(self.every_seconds)}"
        if self.kind is CronKind.ONCE:
            if self.once_delay_seconds <= 0:
                return "once at startup"
            return f"once, {_format_duration(self.once_delay_seconds)} after startup"

        every_month = len(self.months) == 12
        every_dom = not self.dom_restricted
        every_dow = not self.dow_restricted
        one_hour = len(self.hours) == 1
        one_minute = len(self.minutes) == 1
        at_second_zero = self.seconds == frozenset({0})

        if (
            every_month
            and every_dom
            and every_dow
            and one_hour
            and one_minute
            and at_second_zero
        ):
            hour = next(iter(self.hours))
            minute = next(iter(self.minutes))
            return f"daily at {hour:02d}:{minute:02d}"

        if (
            every_month
            and every_dom
            and every_dow
            and len(self.hours) == 24
            and one_minute
            and at_second_zero
        ):
            return f"hourly at :{next(iter(self.minutes)):02d}"

        if (
            every_month
            and every_dom
            and every_dow
            and len(self.hours) == 24
            and len(self.minutes) == 60
            and len(self.seconds) > 1
        ):
            return f"every {_plural(_step_of(sorted(self.seconds), 60), 'second')}"

        if (
            every_month
            and every_dom
            and every_dow
            and len(self.hours) == 24
            and len(self.minutes) > 1
            and at_second_zero
        ):
            return f"every {_plural(_step_of(sorted(self.minutes), 60), 'minute')}"

        return self.raw


# --- Parsing ----------------------------------------------------------


def parse(expression: str) -> CronExpression:
    """Parse an expression string into a :class:`CronExpression`.

    Raises :class:`CronParseError` with a message naming the offending
    field. Field count may be 5 (POSIX) or 6 (leading seconds).
    """
    raw = (expression or "").strip()
    if not raw:
        raise CronParseError("Empty cron expression.")

    lowered = raw.lower()

    if lowered == "@reboot" or lowered == "@once":
        return CronExpression(raw=raw, kind=CronKind.ONCE, once_delay_seconds=0.0)

    if lowered.startswith("@once+"):
        delay = _parse_duration(lowered[len("@once+"):])
        if delay is None:
            raise CronParseError(
                f"Invalid @once delay: {raw!r}. Expected e.g. '@once+45s'."
            )
        return CronExpression(raw=raw, kind=CronKind.ONCE, once_delay_seconds=delay)

    if lowered.startswith("@every"):
        seconds = _parse_duration(lowered[len("@every"):].strip())
        if seconds is None or seconds <= 0:
            raise CronParseError(
                f"Invalid @every duration: {raw!r}. Expected e.g. '@every 90s'."
            )
        return CronExpression(raw=raw, kind=CronKind.EVERY, every_seconds=seconds)

    if lowered in _MACROS:
        return _parse_fields(_MACROS[lowered], raw)

    if lowered.startswith("@"):
        raise CronParseError(f"Unknown macro: {raw!r}.")

    return _parse_fields(raw, raw)


def _parse_fields(spec: str, raw: str) -> CronExpression:
    """Parse a 5- or 6-field expression body."""
    parts = spec.split()
    if len(parts) == 5:
        sec_part, min_part, hour_part, dom_part, month_part, dow_part = (
            "0", *parts,
        )
    elif len(parts) == 6:
        sec_part, min_part, hour_part, dom_part, month_part, dow_part = parts
    else:
        raise CronParseError(
            f"Expected 5 or 6 fields, got {len(parts)}: {raw!r}."
        )

    seconds = _parse_field(sec_part, _SECOND_RANGE, "seconds")
    minutes = _parse_field(min_part, _MINUTE_RANGE, "minutes")
    hours = _parse_field(hour_part, _HOUR_RANGE, "hours")
    months = _parse_field(month_part, _MONTH_RANGE, "month", names=_MONTH_NAMES)

    dom_values, dom_specials = _parse_dom_field(dom_part)
    dow_values, dow_specials = _parse_dow_field(dow_part)

    dom_restricted = dom_part not in ("*", "?")
    dow_restricted = dow_part not in ("*", "?")

    return CronExpression(
        raw=raw,
        kind=CronKind.FIELDS,
        seconds=seconds,
        minutes=minutes,
        hours=hours,
        days_of_month=dom_values,
        months=months,
        days_of_week=dow_values,
        dom_restricted=dom_restricted,
        dow_restricted=dow_restricted,
        last_day_of_month=dom_specials["last"],
        last_dom_offset=dom_specials["last_offset"],
        nearest_weekday=dom_specials["nearest_weekday"],
        last_dow=dow_specials["last_dow"],
        nth_dow=dow_specials["nth_dow"],
    )


def _parse_field(
    spec: str,
    bounds: tuple[int, int],
    label: str,
    *,
    names: dict[str, int] | None = None,
) -> frozenset[int]:
    """Expand one plain field (``*``, ranges, lists, steps) to a value set."""
    lo, hi = bounds
    if spec in ("*", "?"):
        return frozenset(range(lo, hi + 1))

    values: set[int] = set()
    for item in spec.split(","):
        item = item.strip()
        if not item:
            raise CronParseError(f"Empty entry in {label} field: {spec!r}.")

        step = 1
        had_step = "/" in item
        if had_step:
            item, _, step_str = item.partition("/")
            try:
                step = int(step_str)
            except ValueError:
                raise CronParseError(
                    f"Invalid step {step_str!r} in {label} field."
                ) from None
            if step <= 0:
                raise CronParseError(
                    f"Step must be positive in {label} field: {spec!r}."
                )

        if item in ("*", "?"):
            start, end = lo, hi
        elif "-" in item.lstrip("-"):
            start_str, _, end_str = item.partition("-")
            start = _field_value(start_str, label, bounds, names)
            end = _field_value(end_str, label, bounds, names)
        else:
            start = _field_value(item, label, bounds, names)
            # A bare value with a step (``5/15``) runs to the top of the
            # range; without one it is a single value.
            end = hi if had_step else start

        if start > end:
            # Wrapping range (``FRI-MON``) — expand through the top and
            # around, which is what every mainstream cron does.
            values.update(range(start, hi + 1, step))
            values.update(range(lo, end + 1, step))
        else:
            values.update(range(start, end + 1, step))

    if not values:
        raise CronParseError(f"No values matched in {label} field: {spec!r}.")
    return frozenset(values)


def _field_value(
    token: str,
    label: str,
    bounds: tuple[int, int],
    names: dict[str, int] | None,
) -> int:
    """Resolve one token to an int, accepting month/day names."""
    token = token.strip().upper()
    if names and token in names:
        return names[token]
    try:
        value = int(token)
    except ValueError:
        raise CronParseError(
            f"Invalid value {token!r} in {label} field."
        ) from None
    lo, hi = bounds
    if not (lo <= value <= hi):
        raise CronParseError(
            f"Value {value} out of range {lo}-{hi} in {label} field."
        )
    return value


def _parse_dom_field(spec: str) -> tuple[frozenset[int], dict[str, Any]]:
    """Parse day-of-month, splitting out the ``L`` / ``L-n`` / ``nW`` forms."""
    specials: dict[str, Any] = {
        "last": False,
        "last_offset": None,
        "nearest_weekday": None,
    }
    if spec in ("*", "?"):
        return frozenset(range(1, 32)), specials

    plain: list[str] = []
    for item in spec.split(","):
        token = item.strip().upper()
        if token == "L":
            specials["last"] = True
        elif token.startswith("L-"):
            try:
                specials["last_offset"] = int(token[2:])
            except ValueError:
                raise CronParseError(
                    f"Invalid last-day offset {token!r} in day-of-month field."
                ) from None
        elif token.endswith("W") and token != "W":
            try:
                specials["nearest_weekday"] = int(token[:-1])
            except ValueError:
                raise CronParseError(
                    f"Invalid nearest-weekday {token!r} in day-of-month field."
                ) from None
        else:
            plain.append(token)

    values = (
        _parse_field(",".join(plain), _DOM_RANGE, "day-of-month")
        if plain
        else frozenset()
    )
    return values, specials


def _parse_dow_field(spec: str) -> tuple[frozenset[int], dict[str, Any]]:
    """Parse day-of-week, splitting out the ``nL`` and ``n#k`` forms."""
    specials: dict[str, Any] = {"last_dow": None, "nth_dow": None}
    if spec in ("*", "?"):
        return frozenset(range(0, 7)), specials

    plain: list[str] = []
    for item in spec.split(","):
        token = item.strip().upper()
        if "#" in token:
            dow_str, _, nth_str = token.partition("#")
            dow = _field_value(dow_str, "day-of-week", _DOW_RANGE, _DOW_NAMES)
            try:
                nth = int(nth_str)
            except ValueError:
                raise CronParseError(
                    f"Invalid nth-weekday {token!r} in day-of-week field."
                ) from None
            if not (1 <= nth <= 5):
                raise CronParseError(
                    f"Nth-weekday must be 1-5, got {nth} in day-of-week field."
                )
            specials["nth_dow"] = (dow % 7, nth)
        elif token.endswith("L") and token != "L":
            dow = _field_value(token[:-1], "day-of-week", _DOW_RANGE, _DOW_NAMES)
            specials["last_dow"] = dow % 7
        else:
            plain.append(token)

    if plain:
        raw_values = _parse_field(",".join(plain), _DOW_RANGE, "day-of-week", names=_DOW_NAMES)
        # 7 is an alias for Sunday; normalise so membership tests are simple.
        values = frozenset(v % 7 for v in raw_values)
    else:
        values = frozenset()
    return values, specials


def _parse_duration(text: str) -> float | None:
    """Parse ``90s`` / ``1h30m`` / ``45`` (bare = seconds) to seconds."""
    text = text.strip().lower()
    if not text:
        return None
    try:
        # A bare number is seconds — keeps ``@once+45`` working alongside
        # ``@once+45s``.
        return float(text)
    except ValueError:
        pass
    match = _DURATION_RE.fullmatch(text)
    if not match or not any(match.groupdict().values()):
        return None
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


# --- Date helpers -----------------------------------------------------


def _cron_dow(d: datetime) -> int:
    """Python's Mon=0..Sun=6 converted to cron's Sun=0..Sat=6."""
    return (d.weekday() + 1) % 7


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (datetime(year, month + 1, 1) - timedelta(days=1)).day


def _first_of_next_month(d: datetime) -> datetime:
    if d.month == 12:
        return d.replace(
            year=d.year + 1, month=1, day=1, hour=0, minute=0, second=0
        )
    return d.replace(month=d.month + 1, day=1, hour=0, minute=0, second=0)


def _nearest_weekday_to(year: int, month: int, day: int) -> int:
    """The weekday (Mon-Fri) nearest ``day``, without crossing months.

    This is the ``W`` specifier: ``15W`` means "the weekday closest to
    the 15th". A Saturday shifts back to Friday, a Sunday forward to
    Monday, except at a month boundary where it shifts the other way.
    """
    d = datetime(year, month, day)
    weekday = d.weekday()  # Mon=0 .. Sun=6
    if weekday < 5:
        return day
    last = _last_day_of_month(year, month)
    if weekday == 5:  # Saturday
        return day - 1 if day > 1 else day + 2
    return day + 1 if day < last else day - 2  # Sunday


def _localise(naive: datetime, zone: tzinfo) -> datetime | None:
    """Attach ``zone`` to a naive wall-clock time.

    Returns ``None`` when the time does not exist (spring-forward gap).
    ``fold=0`` means an ambiguous fall-back time resolves to its *first*
    occurrence, which is the documented behaviour.
    """
    aware = naive.replace(tzinfo=zone, fold=0)
    round_tripped = aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_tripped != naive:
        return None
    return aware


def _first_valid_after_gap(naive: datetime, zone: tzinfo) -> datetime | None:
    """Find the instant the clock jumps past a spring-forward gap.

    Scans forward a minute at a time from a wall-clock time that does
    not exist. Gaps run 30-120 minutes and occur twice a year, so the
    scan is bounded and effectively free.
    """
    probe = naive
    for _ in range(_MAX_GAP_SCAN_MINUTES):
        probe = probe + timedelta(minutes=1)
        localised = _localise(probe.replace(second=0), zone)
        if localised is not None:
            return localised
    return None


def _step_of(values: list[int], modulus: int) -> int:
    """Infer the step of an evenly-spaced value set, for ``describe()``."""
    if len(values) < 2:
        return modulus
    return values[1] - values[0]


def _format_duration(seconds: float) -> str:
    """Render a duration the way the macros are written (``1h 30m``)."""
    total = int(seconds)
    if total < 60:
        if float(seconds).is_integer():
            count = int(seconds)
            return f"{count} second" if count == 1 else f"{count} seconds"
        return f"{_trim(seconds)} seconds"
    parts: list[str] = []
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _plural(count: int, noun: str) -> str:
    """``1 second`` / ``30 seconds`` — the summary is user-visible."""
    return noun if count == 1 else f"{count} {noun}s"


def _trim(value: float) -> str:
    """Drop a trailing ``.0`` so ``30.0`` prints as ``30``."""
    return str(int(value)) if float(value).is_integer() else str(value)
