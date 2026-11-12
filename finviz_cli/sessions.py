"""The NYSE trading calendar, by rule: which dates are sessions, and when each one closes.

Used to decide which session a pull describes, what "the session before" is, whether two
saved days are really neighbours, and how many sessions lie between two dates. Covers the
regular full-day holidays and the regular 13:00 closes; one-off closures (a storm, a day of
mourning) are not known here and are caught from the data instead (store.trading_days).

The rules are today's rules. They are right from 2022 on (the first year Juneteenth was
observed) and are not a record of older calendars.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache


def _easter(year: int) -> dt.date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: dt.date) -> dt.date | None:
    """A holiday on Sunday is kept on Monday; on Saturday, the Friday before."""
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def holidays(year: int) -> frozenset[dt.date]:
    days = {
        _nth_weekday(year, 2, 0, 3),                    # Presidents' Day
        _easter(year) - dt.timedelta(days=2),           # Good Friday
        _last_weekday(year, 5, 0),                      # Memorial Day
        _nth_weekday(year, 9, 0, 1),                    # Labor Day
        _nth_weekday(year, 11, 3, 4),                   # Thanksgiving
    }
    if year >= 1998:
        days.add(_nth_weekday(year, 1, 0, 3))           # Martin Luther King Jr. Day
    fixed_days = [dt.date(year, 7, 4), dt.date(year, 12, 25)]
    if year >= 2022:
        fixed_days.append(dt.date(year, 6, 19))         # Juneteenth, first observed in 2022
    for fixed in fixed_days:
        days.add(_observed(fixed))
    new_year = dt.date(year, 1, 1)
    if new_year.weekday() == 6:
        days.add(new_year + dt.timedelta(days=1))
    elif new_year.weekday() < 5:                        # on a Saturday it is not observed at all
        days.add(new_year)
    return frozenset(days)


def is_session(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def previous_session(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while not is_session(d):
        d -= dt.timedelta(days=1)
    return d


OPEN = dt.time(9, 30)
REGULAR_CLOSE = dt.time(16, 0)
EARLY_CLOSE = dt.time(13, 0)
FIRST_YEAR = 1990                                       # session numbers count from here


def close_time(d: dt.date) -> dt.time:
    """When the regular session ends: 13:00 on the day after Thanksgiving, on Christmas Eve
    and on July 3rd when those are sessions, else 16:00."""
    if is_session(d) and (d == _nth_weekday(d.year, 11, 3, 4) + dt.timedelta(days=1) or (d.month, d.day) in ((12, 24), (7, 3))):
        return EARLY_CLOSE
    return REGULAR_CLOSE


def close_at(d: dt.date) -> dt.datetime:
    """The moment the session on `d` closes, as a naive Eastern time."""
    return dt.datetime.combine(d, close_time(d))


@lru_cache(maxsize=None)
def _year_sessions(year: int) -> tuple[dt.date, ...]:
    d, out = dt.date(year, 1, 1), []
    while d.year == year:
        if is_session(d):
            out.append(d)
        d += dt.timedelta(days=1)
    return tuple(out)


@lru_cache(maxsize=None)
def _sessions_before(year: int) -> int:
    return 0 if year <= FIRST_YEAR else _sessions_before(year - 1) + len(_year_sessions(year - 1))


@lru_cache(maxsize=None)
def ordinal(d: dt.date) -> int:
    """A running session number: consecutive sessions differ by exactly one, whatever lies
    between them. A date that is not a session gets the number of the session before it."""
    if d.year < FIRST_YEAR:
        raise ValueError(f"{d} is before the calendar starts ({FIRST_YEAR})")
    return _sessions_before(d.year) + sum(1 for s in _year_sessions(d.year) if s <= d) - 1


def as_date(day: str | dt.date) -> dt.date:
    return day if isinstance(day, dt.date) else dt.date.fromisoformat(day)


def adjacent(earlier: str | dt.date, later: str | dt.date) -> bool:
    """True when `earlier` is the session right before `later`."""
    return previous_session(as_date(later)) == as_date(earlier)
