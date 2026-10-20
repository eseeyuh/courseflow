# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Deterministic deadline parsing and normalisation.

The deadline is read from the exact evidence quote, never from the model:
the model's own date is only checked against it. A quote must state one
full calendar date (day, month and year) and at most one time of day.

    parse_deadline_quote(quote)            -> ParsedDeadline, or DeadlineRejected
    normalise(parsed, institution_timezone) -> NormalisedDeadline

Instants come from ``zoneinfo`` (IANA data from ``tzdata``). A local time
that does not exist (a DST gap) is rejected; one that occurs twice (a DST
overlap) has no single instant and needs review. A date with no time keeps
``precision = date`` and no instant: no time of day is invented. Numeric
dates (12/11/2026) are rejected: day-month order cannot be decided. A zone
the quote names but this module cannot map is never replaced by a default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from app.domain.enums import DeadlinePrecision, TimezoneSource, VerificationStatus
from app.domain.enums import RejectionReason as X
from app.domain.enums import ReviewReason as R


def _names(groups: tuple[tuple[str, ...], ...], start: int) -> dict[str, int]:
    return {name: number for number, names in enumerate(groups, start) for name in names}


_MONTHS = _names(
    (
        ("january", "jan"),
        ("february", "feb"),
        ("march", "mar"),
        ("april", "apr"),
        ("may",),
        ("june", "jun"),
        ("july", "jul"),
        ("august", "aug"),
        ("september", "sep", "sept"),
        ("october", "oct"),
        ("november", "nov"),
        ("december", "dec"),
    ),
    1,
)
_WEEKDAYS = _names(
    (
        ("monday", "mon"),
        ("tuesday", "tue", "tues"),
        ("wednesday", "wed"),
        ("thursday", "thu", "thur", "thurs"),
        ("friday", "fri"),
        ("saturday", "sat"),
        ("sunday", "sun"),
    ),
    0,
)
# Timezone phrases a quote may state, and the IANA zone each one means.
TIMEZONE_PHRASES = {
    "uk time": "Europe/London",
    "london time": "Europe/London",
    "utc": "UTC",
    "gmt": "UTC",
}

_WD = r"(?:\b(?P<wd>[A-Za-z]+)\.?,?\s+)?"
_DMY = re.compile(
    _WD + r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>[A-Za-z]+)\.?,?\s+"
    r"(?P<year>\d{4})\b"
)
_MDY = re.compile(
    _WD + r"\b(?P<month>[A-Za-z]+)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\b"
)
_ISO = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_NUMERIC = re.compile(r"\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b")
_MONTH_YEAR = re.compile(r"\b(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})\b")

# Clock times use a colon; "2.30pm" (a dot) only with am/pm, so that a
# decimal such as 3.50 is never read as a time.
_CLOCKISH = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?![\d:])")
_MERIDIEM = r"\s*(?P<ap>[ap]\.?m\.?)(?![a-z])"
_TIME_24 = re.compile(
    r"(?<![\d:])(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?![\d:])(?:" + _MERIDIEM + r")?",
    re.IGNORECASE,
)
_TIME_12 = re.compile(
    r"(?<![\d.:])(?P<h>1[0-2]|0?[1-9])(?:\.(?P<m>[0-5]\d))?" + _MERIDIEM, re.IGNORECASE
)
_NOON = re.compile(r"\b(?:noon|midday)\b", re.IGNORECASE)
_MIDNIGHT = re.compile(r"\bmidnight\b", re.IGNORECASE)

# A zone named right after a clock time: "14:00 CET", "2:00 pm (Paris time)".
# The am/pm marker is consumed possessively, so it is never read as a zone.
_AFTER_CLOCK = (
    r"(?:\d:\d{2}(?:\s*[aApP]\.?[mM]\.?(?![A-Za-z]))?+|[aApP]\.?[mM]\.?|noon|midday)\s*\(?\s*"
)
_ZONE_WORDS = re.compile(
    _AFTER_CLOCK + r"(?P<zone>[A-Za-z]+(?:\s+[A-Za-z]+)?)\s+time\b", re.IGNORECASE
)
_ZONE_ABBREVIATION = re.compile(
    _AFTER_CLOCK + r"(?P<zone>[A-Z]{2,5}(?:\s*[+-]\s*\d{1,2}(?::?\d{2})?)?)\b(?!\s+time\b)"
)


class DeadlineRejected(ValueError):
    """The quote cannot support a concrete deadline. ``reason`` is a stable code."""

    def __init__(self, reason: X) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ParsedDeadline:
    due_date: date
    due_time: time | None
    weekday_stated: int | None  # Monday = 0, when the quote names one
    timezone: str | None  # IANA, when the quote names a known zone
    review_reasons: tuple[R, ...]  # stated, but not determinate enough to verify


@dataclass(frozen=True, slots=True)
class NormalisedDeadline:
    due_date: date
    due_time: time | None
    timezone: str | None
    timezone_source: TimezoneSource
    precision: DeadlinePrecision
    due_at: datetime | None
    status: VerificationStatus
    review_reasons: tuple[R, ...]


def _date(year: str, month: int, day: str) -> date:
    try:
        return date(int(year), month, int(day))
    except ValueError:
        raise DeadlineRejected(X.IMPOSSIBLE_DATE) from None


def _dates(quote: str) -> set[tuple[date, int | None]]:
    found: set[tuple[date, int | None]] = set()
    for pattern in (_DMY, _MDY):
        for match in pattern.finditer(quote):
            month = _MONTHS.get(match["month"].lower())
            if month is None:
                continue
            # A preceding word that is not a weekday ("on", "by") is ignored.
            weekday = _WEEKDAYS.get(match["wd"].lower()) if match["wd"] else None
            found.add((_date(match["year"], month, match["day"]), weekday))
    for match in _ISO.finditer(quote):
        found.add((_date(match["year"], int(match["month"]), match["day"]), None))
    return found


def _time(quote: str) -> tuple[time | None, tuple[R, ...]]:
    times: list[time] = []
    for match in _TIME_24.finditer(quote):
        hour, minute = int(match["h"]), int(match["m"])
        if match["ap"] is not None:
            if not 1 <= hour <= 12:
                raise DeadlineRejected(X.IMPOSSIBLE_TIME)
            hour = hour % 12 + (12 if match["ap"].lower().startswith("p") else 0)
        times.append(time(hour, minute))
    if len(_CLOCKISH.findall(quote)) != len(times):
        raise DeadlineRejected(X.IMPOSSIBLE_TIME)  # e.g. 24:00 or 14:75
    for match in _TIME_12.finditer(quote):
        hour = int(match["h"]) % 12 + (12 if match["ap"].lower().startswith("p") else 0)
        times.append(time(hour, int(match["m"] or 0)))
    times += [time(12, 0) for _ in _NOON.finditer(quote)]
    if len(set(times)) > 1:
        raise DeadlineRejected(X.MULTIPLE_TIMES)
    if _MIDNIGHT.search(quote):
        if times:
            raise DeadlineRejected(X.MULTIPLE_TIMES)
        return None, (R.AMBIGUOUS_TIME,)  # the start or the end of the day?
    return (times[0] if times else None), ()


def _timezone(quote: str) -> tuple[str | None, tuple[R, ...]]:
    lowered = quote.lower()
    zones = {
        zone
        for phrase, zone in TIMEZONE_PHRASES.items()
        if re.search(rf"\b{re.escape(phrase)}\b", lowered)
    }
    named = [f"{m['zone']} time" for m in _ZONE_WORDS.finditer(quote)]
    named += [m["zone"] for m in _ZONE_ABBREVIATION.finditer(quote)]
    if any(zone.lower() not in TIMEZONE_PHRASES for zone in named):
        return None, (R.UNRECOGNISED_TIMEZONE,)
    if len(zones) > 1:
        return None, (R.CONFLICTING_TIMEZONE,)
    return (zones.pop() if zones else None), ()


def parse_deadline_quote(quote: str) -> ParsedDeadline:
    """The one deadline an exact evidence quote states, or DeadlineRejected."""
    if _NUMERIC.search(quote):
        raise DeadlineRejected(X.NUMERIC_DATE)
    dates = _dates(quote)
    if not dates:
        months = [m for m in _MONTH_YEAR.finditer(quote) if m["month"].lower() in _MONTHS]
        raise DeadlineRejected(X.MONTH_ONLY if months else X.NO_DATE)
    if len({value for value, _ in dates}) > 1:
        raise DeadlineRejected(X.MULTIPLE_DATES)
    due_date = next(iter(dates))[0]
    weekdays = {weekday for _, weekday in dates if weekday is not None}
    due_time, time_reasons = _time(quote)
    zone, zone_reasons = _timezone(quote)
    reasons = time_reasons + zone_reasons
    if weekdays and weekdays != {due_date.weekday()}:
        reasons += (R.WEEKDAY_MISMATCH,)
    weekday = weekdays.pop() if len(weekdays) == 1 else None
    return ParsedDeadline(due_date, due_time, weekday, zone, reasons)


def normalise(parsed: ParsedDeadline, institution_timezone: str | None) -> NormalisedDeadline:
    """Precision, timezone provenance, the instant and the status, deterministically."""
    reasons: list[R] = list(parsed.review_reasons)
    zone_unusable = {R.UNRECOGNISED_TIMEZONE, R.CONFLICTING_TIMEZONE} & set(reasons)
    if parsed.timezone is not None:
        zone, source = parsed.timezone, TimezoneSource.EXPLICIT_SOURCE
    elif institution_timezone is not None and not zone_unusable:
        zone, source = institution_timezone, TimezoneSource.INSTITUTION_DEFAULT
    else:
        zone, source = None, TimezoneSource.UNKNOWN
    precision = DeadlinePrecision.DATE if parsed.due_time is None else DeadlinePrecision.DATETIME
    due_at = None
    if parsed.due_time is not None:
        if zone is None:
            if not zone_unusable:
                reasons.append(R.MISSING_TIMEZONE)
        else:
            due_at = _instant(parsed.due_date, parsed.due_time, zone)
            if due_at is None:
                reasons.append(R.AMBIGUOUS_LOCAL_TIME)  # occurs twice (DST overlap)
    return NormalisedDeadline(
        due_date=parsed.due_date,
        due_time=parsed.due_time,
        timezone=zone,
        timezone_source=source,
        precision=precision,
        due_at=due_at,
        status=VerificationStatus.NEEDS_REVIEW if reasons else VerificationStatus.VERIFIED,
        review_reasons=tuple(reasons),
    )


def _instant(day: date, at: time, zone: str) -> datetime | None:
    """The UTC instant; None when the local time occurs twice; DeadlineRejected
    when it never occurs."""
    tz = ZoneInfo(zone)
    local = datetime.combine(day, at)
    first, second = local.replace(tzinfo=tz, fold=0), local.replace(tzinfo=tz, fold=1)
    if first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != local:
        raise DeadlineRejected(X.LOCAL_TIME_NONEXISTENT)  # skipped by a DST gap
    if first.utcoffset() != second.utcoffset():
        return None
    return first.astimezone(UTC)
