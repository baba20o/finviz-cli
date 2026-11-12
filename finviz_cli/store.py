"""The local history: one file per trading day, plus every snapshot taken during a day.

  <home>/days/<date>.market.csv   a whole-market export taken after the close (this CLI's own pulls)
  <home>/days/<date>.lfr.json     a separate collector's last snapshot after the close (floats under 100M only)
  <home>/snapshots/<session>/<time>_market.csv     whole-market pulls that passed the checks
  <home>/snapshots/<session>/<time>_custom.csv     pulls with a filter or other columns (kept, never reused)
  <home>/snapshots/<session>/<time>_rejected.csv   whole-market pulls that failed a check (kept to look at, never used)
      <time> is when the request left, Eastern, as YYYYMMDDTHHMMSS and six digits of microseconds
  <home>/earnings.csv             the earnings calendar, as synced
  <home>/cache/                   parsed days and the reverse-split index, rebuilt when their inputs change

When both sources exist for a day the market file wins: it covers every float and carries
earnings dates. Files hold market data only, never the token. Every file is replaced in one
step, and whatever several commands may change at once is changed under one lock. Reading
works on a folder that cannot be written to: the caches are then simply not kept.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
from pathlib import Path

import pandas as pd

from finviz_cli import config, enrich, frame, sessions
from finviz_cli.files import Busy, FileLock, Unavailable, write_bytes_atomic, write_text_atomic

SOURCES = ("market", "lfr")            # order of preference
FLOAT_CAP = {"lfr": 100.0}             # millions of shares a source stops at
CACHE_VERSION = 4
MIN_DAY_ROWS = 2000                    # a whole-market pull has several thousand rows; fewer than this is not one
MIN_COVERAGE = 0.9                     # ...and neither is one with under this share of the rows last filed
MIN_FILLED = 0.9                       # share of rows that must carry a number in a column that always has one
MIN_OVERLAP = 50                       # tickers two days must share before "same previous closes" means anything
STAMP = "%Y%m%dT%H%M%S%f"              # to the microsecond: two pulls in one second must still sort by when they were taken
SECOND_STAMP = "%Y%m%dT%H%M%S"         # names written to the second, by a short-lived earlier version
OLD_STAMP = "%Y%m%dT%H%M"              # snapshots saved before the checks existed
_STAMP_BY_LENGTH = {21: STAMP, 15: SECOND_STAMP, 13: OLD_STAMP}
REQUIRED = ("Ticker", "Price", "Prev Close", "Open", "High", "Low", "Volume", "Change", "Gap", "Change from Open",
            "Relative Volume", "Shares Float")       # the columns every label is built from (a float may be blank, the column not)
ALWAYS_FILLED = (("Price", "a price"), ("Prev Close", "a previous close"))
FILLED_AFTER_CLOSE = (("Open", "an open"), ("High", "a high"), ("Low", "a low"), ("Volume", "a volume"), ("Change", "a change"),
                      ("Gap", "a gap"), ("Change from Open", "a change from the open"), ("Relative Volume", "a relative volume"))
# A time of day followed by a zone: Z, UTC, GMT, or an offset written +05, +0500 or +05:00.
_OFFSET = r"\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*(?:[zZ]|UTC|GMT|[+-]\d{2}(?::?\d{2})?)$"
LEAD = enrich.STREAK_DAYS + 1          # saved days loaded behind a request, enough for `yesterday` and `streak`
_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(market\.csv|lfr\.json)$")
_IMPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


class StoreBusy(Exception):
    """The store's lock could not be taken: held too long by another command, or nowhere to keep it."""


@contextlib.contextmanager
def _store_lock():
    """Everything that changes the store runs under this. Without the lock nothing is written."""
    try:
        with FileLock(config.home() / ".store.lock", 30.0):
            yield
    except Busy:
        raise StoreBusy("another finviz command is writing to the store; try again in a moment") from None
    except Unavailable:
        raise StoreBusy(f"the data folder cannot be written to: {config.home()}") from None


def _is_date(text: str) -> bool:
    try:
        dt.date.fromisoformat(text)
        return True
    except ValueError:
        return False


def _source_files() -> dict[str, tuple[Path, str, str]]:
    """date -> (file, source, fingerprint).

    The fingerprint is the file's size, its modification time, its change time (creation
    time on Windows) and its identity on the volume, so it moves whenever the file is
    replaced by another one, including by a restore that kept the old times. It is not a
    hash of the contents: at 640 MB of days that would cost every command a second.
    """
    out: dict[str, tuple[Path, str, str]] = {}
    d = config.days_dir()
    if not d.exists():
        return out
    found = []
    with os.scandir(d) as it:
        for entry in it:
            m = _DAY_FILE.match(entry.name)
            if not m or not _is_date(m.group(1)):
                continue                                      # anything else in the folder is not a day
            source = "market" if m.group(2) == "market.csv" else "lfr"
            st = os.stat(entry.path)                          # not entry.stat(): that leaves the file's identity at 0 on Windows
            found.append((m.group(1), source, Path(entry.path),
                          f"{source}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}:{st.st_ino}"))
    for day, source, path, mark in sorted(found, key=lambda f: (f[0], f[1] == "market")):    # market overwrites lfr
        out[day] = (path, source, mark)
    return out


def _read_raw(path: Path, source: str) -> pd.DataFrame:
    """A saved day or snapshot as a typed table, one row per ticker. Every stock is kept, with
    or without a float on record: a new listing has none, and it can be the day's biggest mover."""
    if source == "market":
        headers, rows = frame.parse_table(path.read_text(encoding="utf-8"))
        df = frame.to_frame(rows, avgvol_in_thousands=True, headers=headers)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data["rows"] if isinstance(data, dict) else data
        df = frame.to_frame([r for r in rows if isinstance(r, dict)], avgvol_in_thousands=False)
    if "ticker" in df:
        df = df[df.ticker.str.strip() != ""].drop_duplicates("ticker", keep="first")
    if "industry" in df:
        df = df[~df.industry.str.contains("Exchange Traded Fund", na=False)]
    return df.reset_index(drop=True)


def _same_previous_close(a: dict, b: dict) -> bool:
    """True when two days carry the same previous close for nearly every shared ticker: a repeat.

    Only said when they share enough tickers to judge by. One illiquid name that did not
    move is not evidence about a whole session."""
    common = [t for t in a if t in b]
    return len(common) >= MIN_OVERLAP and sum(a[t] == b[t] for t in common) / len(common) > 0.9


def _memo_path() -> Path:
    return config.days_dir() / "closed_days.txt"


def _read_memo() -> dict[str, tuple[str, str, str]]:
    """date -> (open|closed, the saved day it was compared with, the fingerprints of both files)."""
    p = _memo_path()
    out = {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""      # a damaged memo is no memo, not a crash
    except OSError:
        text = ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = (parts[1], parts[2] if len(parts) > 2 else "-", parts[3] if len(parts) > 3 else "")
    return out


def _write_memo(memo: dict[str, tuple[str, str, str]]) -> None:
    with contextlib.suppress(OSError):                 # a memo is a convenience: a read-only folder still reads
        write_text_atomic(_memo_path(), "".join(f"{d} {' '.join(memo[d])}\n" for d in sorted(memo)))


def trading_days(files: dict | None = None) -> list[str]:
    """Saved days on which the market was open.

    Weekends and the regular holidays are known from the calendar. An unscheduled closure is
    caught from the data: the feed then repeats the last session, so the day carries the same
    previous close as the saved day before it. That check is redone whenever either of the two
    files changes or a different day becomes the one before. Pass `files` (one _source_files
    scan) to judge the same set of files the caller is about to use.
    """
    files = _source_files() if files is None else files
    all_days = [d for d in sorted(files) if sessions.is_session(sessions.as_date(d))]
    memo = _read_memo()
    changed = False
    cache: dict[str, dict] = {}

    def prev_close_map(day: str) -> dict:
        if day not in cache:
            df = _read_raw(*files[day][:2])
            cache[day] = dict(zip(df.ticker, df.prev_close)) if "prev_close" in df else {}
        return cache[day]

    for i, day in enumerate(all_days):
        before = all_days[i - 1] if i else "-"
        mark = files[day][2] + ("/" + files[before][2] if i else "")
        if day in memo and memo[day][1:] == (before, mark):
            continue
        status = "open"
        if i and _same_previous_close(prev_close_map(day), prev_close_map(before)):
            status = "closed"
        memo[day] = (status, before, mark)
        changed = True
    if changed:
        _write_memo({d: memo[d] for d in memo if d in files})
    return [d for d in all_days if memo[d][0] == "open"]


def previous_close(day: str) -> pd.Timestamp:
    """The close of the session before `day`, by the trading calendar (not by which days are saved)."""
    return pd.Timestamp(sessions.close_at(sessions.previous_session(sessions.as_date(day))))


def close_of(day: str) -> pd.Timestamp:
    """When the session on `day` closes: 16:00, or 13:00 on an early-close day."""
    return pd.Timestamp(sessions.close_at(sessions.as_date(day)))


# ---------------------------------------------------------------- earnings calendar

def earnings_path() -> Path:
    return config.home() / "earnings.csv"


def _one_time(text: str) -> pd.Timestamp:
    """One report time, however it is written, as a plain Eastern timestamp (NaT if unreadable)."""
    try:
        when = pd.to_datetime(text, format="mixed")
    except (ValueError, TypeError, OverflowError):
        return pd.NaT
    if when is pd.NaT:
        return pd.NaT
    return when.tz_convert(config.ET).tz_localize(None) if when.tzinfo is not None else when


def _parse_when(text: pd.Series) -> pd.Series:
    """Report times as plain Eastern timestamps, whichever way each one is written.

    A time without a zone is Eastern already (that is how Finviz writes them). One with a
    zone or an offset is brought to Eastern. Rows are placed by position, so the input's
    index can be anything, and anything the fast path does not take is read one by one: a
    calendar that spans a clock change, or mixes the kinds, reads the same as any other.
    """
    values = text.astype(object).where(text.notna(), "").astype(str).str.strip().to_numpy()      # a missing date is "", never NaN
    out = pd.Series(pd.NaT, index=range(len(values)), dtype="datetime64[ns]")
    plain = [i for i, v in enumerate(values) if v and not re.search(_OFFSET, v)]
    if plain:
        try:
            fast = pd.to_datetime(pd.Series([values[i] for i in plain]), format="ISO8601", errors="coerce")
        except (ValueError, TypeError):                          # zones written a way the pattern above did not see
            fast = None
        if fast is not None and str(fast.dtype).startswith("datetime64") and getattr(fast.dt, "tz", None) is None:
            out.iloc[plain] = fast.astype("datetime64[ns]").to_numpy()
    for i in (i for i, v in enumerate(values) if v and pd.isna(out.iloc[i])):
        out.iloc[i] = _one_time(values[i])
    return pd.Series(out.to_numpy(), index=text.index)


def load_earnings() -> pd.DataFrame:
    p = earnings_path()
    if not p.exists():
        return pd.DataFrame(columns=["date", "ticker", "when"])
    df = pd.read_csv(p, dtype=str, keep_default_na=False)       # as text: a ticker called NA is a ticker
    df["when"] = _parse_when(df["date"])
    return df


def save_earnings(new: pd.DataFrame, date_from: str, date_to: str) -> int:
    """Replace the saved calendar for date_from..date_to with `new` (a company that moves its
    report must not keep a row on the old date), and keep everything outside that span.

    `new` has to be a calendar: `date` and `ticker` columns, filled on every row. Anything
    else (an error answer read as a table) is refused before a single saved row is touched.
    """
    if "date" not in new.columns or "ticker" not in new.columns:
        raise ValueError("that is not an earnings calendar: it has no date and ticker columns; the saved calendar was left alone")
    fresh = new.astype(object).where(new.notna(), "").astype(str) if len(new) else new
    if len(fresh) and ((fresh["date"].str.strip() == "") | (fresh["ticker"].str.strip() == "")).any():
        raise ValueError("the earnings calendar has rows without a date or a ticker; the saved calendar was left alone")
    if len(fresh):
        when = _parse_when(fresh["date"])
        if when.isna().any():
            raise ValueError("the earnings calendar has dates that cannot be read; the saved calendar was left alone")
        first, after = pd.Timestamp(date_from), pd.Timestamp(date_to) + pd.Timedelta(days=1)
        if (when < first).any() or (when >= after).any():
            raise ValueError(f"the earnings calendar has dates outside {date_from} .. {date_to}, the range being replaced; "
                             "the saved calendar was left alone")
        # Saved the one way everything here reads them: plain Eastern time, to the minute. What
        # is on disk is then never a mixture of zones, however the rows arrived.
        fresh = fresh.assign(date=when.dt.strftime("%Y-%m-%d %H:%M"))
    with _store_lock():
        old = load_earnings()
        if len(old):
            end = pd.Timestamp(date_to) + pd.Timedelta(days=1)
            old = old[(old.when < pd.Timestamp(date_from)) | (old.when >= end) | old.when.isna()]
        both = pd.concat([old.drop(columns=["when"], errors="ignore"), fresh], ignore_index=True)
        both = both.drop_duplicates(["date", "ticker"], keep="last").sort_values(["date", "ticker"])
        write_text_atomic(earnings_path(), both.to_csv(index=False, lineterminator="\n"))
        return len(both)


def reporters(prev_close: pd.Timestamp, day: str, calendar: pd.DataFrame | None = None) -> set[str]:
    """Tickers on the earnings calendar between the previous close and this day's close."""
    cal = load_earnings() if calendar is None else calendar
    if cal.empty or "date" not in cal or "ticker" not in cal:
        return set()
    when = cal["when"] if "when" in cal else _parse_when(cal["date"])
    return set(cal[(when >= prev_close) & (when < close_of(day))].ticker)


# ---------------------------------------------------------------- days

def _labelled(df: pd.DataFrame, day: str, source: str, who: set[str]) -> pd.DataFrame:
    df.insert(0, "date", day)
    df = enrich.same_day(df, pd.Timestamp(day), previous_close(day), who, close_of(day))
    df["source"] = source
    return df


def _read_pickle(path: Path):
    try:
        return pd.read_pickle(path) if path.exists() else None
    except Exception:
        return None                    # unreadable or half-written by an older version: rebuild


def _write_pickle(path: Path, obj) -> None:
    """Keep a cache if the folder lets us. Not being able to is never an error."""
    buf = io.BytesIO()
    pd.to_pickle(obj, buf)
    with contextlib.suppress(OSError):
        write_bytes_atomic(path, buf.getvalue())


def load_day(day: str, calendar: pd.DataFrame | None = None, files: dict | None = None) -> pd.DataFrame:
    """One saved day, typed and with its same-day labels (shape, catalyst, mover, close_kind).

    The parsed day is cached under the exact inputs that made it: the day's file and the set
    of companies reporting earnings in its window. Change either and it is rebuilt.
    """
    files = _source_files() if files is None else files
    if day not in files:
        raise KeyError(f"{day} is not saved")
    path, source, mark = files[day]
    who = reporters(previous_close(day), day, calendar)
    key = (CACHE_VERSION, mark, hashlib.sha1(",".join(sorted(who)).encode()).hexdigest())
    cache = config.home() / "cache" / f"{day}.{source}.v{CACHE_VERSION}.pkl"
    held = _read_pickle(cache)
    if isinstance(held, dict) and held.get("key") == key:
        return held["df"]
    df = _labelled(_read_raw(path, source), day, source, who)
    _write_pickle(cache, {"key": key, "df": df})
    with contextlib.suppress(OSError):
        for stale in cache.parent.glob(f"{day}.*.pkl"):        # other sources and older versions of this day
            if stale != cache:
                stale.unlink()
    return df


def _di(day: str) -> int:
    return sessions.ordinal(sessions.as_date(day))


def load_days(which: list[str], files: dict | None = None) -> pd.DataFrame:
    """Several days stacked, with `di` = each day's session number (consecutive sessions differ by one)."""
    files = _source_files() if files is None else files
    days = set(trading_days(files))
    calendar = load_earnings()
    parts = [load_day(d, calendar, files).assign(di=_di(d)) for d in which if d in days]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    for flag in ("mover", "split_headline"):             # a day too bare to be labelled leaves blanks: not one there
        if flag in df:
            df[flag] = df[flag].where(df[flag].notna(), False).astype(bool)
    return df


def float_caps(days: list[str], files: dict | None = None) -> dict[int, float]:
    """Session number -> the float (millions) that day's source stops at, where it has a limit."""
    files = _source_files() if files is None else files
    return {_di(d): FLOAT_CAP[files[d][1]] for d in days if d in files and files[d][1] in FLOAT_CAP}


def _slim(day: str, calendar: pd.DataFrame, files: dict) -> pd.DataFrame:
    return load_day(day, calendar, files).assign(di=_di(day)).reindex(columns=enrich.SPLIT_INPUT)


def _stack(parts: list[pd.DataFrame], columns: list[str]) -> pd.DataFrame:
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=columns)


def _last_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """Each ticker's latest row. The sort is stable and on both keys, so it is the same row
    whether the index was built in one pass or extended a day at a time."""
    if rows.empty:
        return pd.DataFrame(columns=enrich.SPLIT_INPUT)
    return rows.sort_values(["ticker", "di"], kind="mergesort").groupby("ticker", sort=False).tail(1).reset_index(drop=True)


def _split_state(files: dict | None = None) -> dict:
    """The reverse-split index over ALL saved days: {"events", "last", "prev", "days"}.

      events  every reverse split found
      last    each ticker's last saved row
      prev    each ticker's last saved row before the final saved day

    A split is recognised by comparing a row with the same ticker's row before it, which may
    be many sessions back. Looking only at the days a command asked for would therefore give
    a different answer for the same row; this index is the one answer. It is cached, extended
    when days are added at the end, and rebuilt when an earlier day changes or goes.
    """
    files = _source_files() if files is None else files
    days = trading_days(files)
    marks = [(d, files[d][2]) for d in days]
    cache = config.home() / "cache" / f"splits.v{CACHE_VERSION}.pkl"
    held = _read_pickle(cache)
    events = pd.DataFrame(columns=enrich.SPLIT_COLUMNS)
    last = pd.DataFrame(columns=enrich.SPLIT_INPUT)
    done = 0
    if isinstance(held, dict) and held.get("version") == CACHE_VERSION:
        if held["marks"] == marks:
            return held
        if held["marks"] and held["marks"] == marks[:len(held["marks"])]:
            events, last, done = held["events"], held["last"], len(held["marks"])
    prev = last
    if days[done:]:
        calendar = load_earnings()
        rows = _stack([last] + [_slim(d, calendar, files) for d in days[done:]], enrich.SPLIT_INPUT)
        events = _stack([events, enrich.find_splits(rows)], enrich.SPLIT_COLUMNS)      # the carried rows come first: no event of their own
        last = _last_rows(rows)
        prev = _last_rows(rows[rows.di < _di(days[-1])])
    state = {"version": CACHE_VERSION, "marks": marks, "events": events, "last": last, "prev": prev, "days": days}
    _write_pickle(cache, state)
    return state


def split_index() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(every reverse split found across all saved days, each ticker's last saved row)."""
    state = _split_state()
    return state["events"], state["last"]


def _last_before(day: str, state: dict, files: dict) -> pd.DataFrame:
    """Each ticker's last saved row strictly before `day`: the row a split on `day` shows against."""
    days = state["days"]
    if not days or day > days[-1]:
        return state["last"]
    if day == days[-1]:
        return state["prev"]
    calendar = load_earnings()                             # an older session: rare, so worked out on the spot
    return _last_rows(_stack([_slim(d, calendar, files) for d in days if d < day], enrich.SPLIT_INPUT))


def history_frame(which: list[str], lead: int = LEAD) -> pd.DataFrame:
    """The days in `which`, with their history fields (yesterday, streak, split clock).

    `lead` earlier saved days are loaded behind the first one, which is all `yesterday` and
    `streak` can reach; the split clock comes from the index over every saved day. So the
    fields do not depend on how wide a range was asked for.
    """
    files = _source_files()
    days = trading_days(files)
    known = set(days)
    wanted = [d for d in which if d in known]
    if not wanted:
        return pd.DataFrame()
    first = days.index(min(wanted))
    span = days[max(0, first - lead):days.index(max(wanted)) + 1]
    df = load_days(span, files)
    if df.empty or "mover" not in df:
        return df[df.date.isin(wanted)].reset_index(drop=True)
    out = enrich.add_history(df, float_caps(days, files), _split_state(files)["events"])
    return out[out.date.isin(wanted)].reset_index(drop=True)


def with_history(today: pd.DataFrame, day: str, lookback: int = LEAD) -> pd.DataFrame:
    """Add the history fields to `today`: rows for `day` from a live pull.

    The fields are always added. With nothing saved before `day` they read 'no history', 0
    and blank, the same as a saved day with nothing before it. The split clock for `day`
    itself is worked out from these rows against each name's last saved row before `day`,
    whatever the store already holds for that session: the rows shown are the ones judged.
    """
    if "mover" not in today:
        return today
    files = _source_files()
    days = trading_days(files)
    di_today = _di(day)
    live = today.assign(di=di_today)
    earlier = [d for d in days if d < day][-lookback:]
    both = _stack([load_days(earlier, files), live], list(live.columns)) if earlier else live
    state = _split_state(files)
    before = _last_before(day, state, files)
    fresh = enrich.find_splits(_stack([before, live.reindex(columns=enrich.SPLIT_INPUT)], enrich.SPLIT_INPUT))
    events = _stack([state["events"][state["events"].di < di_today], fresh[fresh.di == di_today]], enrich.SPLIT_COLUMNS)
    out = enrich.add_history(both, float_caps(days, files), events)
    return out[out.di == di_today].drop(columns=["di"]).reset_index(drop=True)


# ---------------------------------------------------------------- writing

def _number(text) -> float | None:
    try:
        value = float(str(text).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _prev_closes(rows: list[dict]) -> dict:
    out = {}
    for r in rows:
        value = _number(r.get("Prev Close"))
        if value is not None and r.get("Ticker"):
            out[r["Ticker"]] = value
    return out


def _count_rows(text: str) -> int:
    try:
        return len(frame.parse_table(text)[1])
    except csv.Error:
        return 0


def _unfilled(rows: list[dict], columns) -> str | None:
    for header, name in columns:
        filled = sum(1 for r in rows if _number(r.get(header)) is not None and (header != "Price" or _number(r.get(header)) > 0))
        if filled < MIN_FILLED * len(rows):
            return f"only {filled} of {len(rows)} rows have {name}"
    return None


def inspect_market(text: str) -> tuple[int, list[dict], str | None]:
    """(rows, parsed rows, what is wrong) for a pull that should be the whole market.

    Looked at as a table, not as lines of text: an error page with enough lines is not a day,
    and neither is an answer that stopped arriving part of the way through.
    """
    try:
        headers, rows = frame.parse_table(text, strict=True)      # broken quoting is a broken answer
    except csv.Error:
        return 0, [], "it cannot be read as a table"
    missing = [h for h in REQUIRED if h not in headers]
    if missing:
        return len(rows), rows, "columns are missing: " + ", ".join(missing)
    if not text.endswith(("\n", "\r")):
        return len(rows), rows, "the last line is cut off: the answer did not arrive whole"
    ragged = sum(1 for r in rows if None in r or any(v is None for v in r.values()))
    if ragged:
        return len(rows), rows, f"{ragged} of {len(rows)} rows do not have the header's columns"
    named = [r["Ticker"].strip() for r in rows if (r.get("Ticker") or "").strip()]
    if len(named) < MIN_DAY_ROWS:
        return len(named), rows, f"only {len(named)} rows, too few for the whole market"
    if len(set(named)) < 0.99 * len(named):
        return len(named), rows, f"tickers repeat ({len(named) - len(set(named))} duplicates)"
    return len(named), rows, _unfilled(rows, ALWAYS_FILLED)


def _market_days(files: dict, upto: str) -> list[tuple[str, Path]]:
    """Whole-market day files to measure a new pull against: the same session's, and the last
    one before it. Real sessions only: a file judged to be a closed day's repeat is no yardstick."""
    days = [d for d in trading_days(files) if files[d][1] == "market" and d <= upto]
    keep = [d for d in days if d == upto] + [d for d in days if d < upto][-1:]
    return [(d, files[d][0]) for d in keep]


def judge_market(text: str, session: str, after_close: bool) -> tuple[int, str | None]:
    """(rows, what is wrong) for a whole-market pull of `session`, by every rule there is:
    the table itself (inspect_market), no collapse in coverage against what is already filed
    for this session or the last one, not a repeat of the saved day before (a closure or a
    stale feed) and, for a pull taken after the close, the day's own columns filled in.
    New pulls and older snapshots about to be reused go through this same door."""
    rows, parsed, problem = inspect_market(text)
    if problem:
        return rows, problem
    files = _source_files()
    for day, path in _market_days(files, session):
        had = _count_rows(path.read_text(encoding="utf-8"))
        if rows < MIN_COVERAGE * had:
            return rows, f"only {rows} rows against {had} on {day}: part of the market is missing"
    earlier = [d for d in trading_days(files) if d < session]         # real sessions only: a file saved on a holiday is not one
    if earlier:
        before = _read_raw(*files[earlier[-1]][:2])
        if "prev_close" in before and _same_previous_close(_prev_closes(parsed), dict(zip(before.ticker, before.prev_close))):
            return rows, f"same previous closes as {earlier[-1]}: the market was closed or the feed has not moved on"
    return rows, (_unfilled(parsed, FILLED_AFTER_CLOSE) if after_close else None)


def _carries_the_close(session: str, pulled_at: dt.datetime) -> bool:
    """True when a pull for `session` was taken after that session's close had settled: the
    same evening, overnight, or on the weekend after. Its day columns must then be filled in."""
    return _is_date(session) and pulled_at >= config.record_from(sessions.as_date(session))


def save_snapshot(text: str, pulled_at: dt.datetime, session: str, whole_market: bool, as_day: bool) -> dict:
    """Keep a pull under the session it describes, named by when it was really taken.

    A whole-market pull is judged first (judge_market). One that fails is kept as `_rejected`
    and never used. `as_day` also files a pull that passed as that session's end-of-day
    record, unless a later pull is already kept for the session: commands can overlap and
    finish out of order, and the day's record must not go back in time. Both pulls are kept.
    The checks, the name and the writes happen under the store's lock, as one step.
    Returns {"snapshot", "day_file", "refused", "usable", "rows", "superseded_by"}.
    """
    folder = config.snapshots_dir() / session
    with _store_lock():
        kind, problem, newer = "custom", None, None
        if not whole_market:
            rows = _count_rows(text)
        else:
            rows, problem = judge_market(text, session, as_day or _carries_the_close(session, pulled_at))
            kind = "rejected" if problem else "market"
            if as_day and not problem:
                latest = latest_snapshot(session)
                if latest and latest[1] > pulled_at:               # compared to the microsecond, as the names are written
                    newer = latest[0].name
        stamp, n = f"{pulled_at:{STAMP}}", 1
        snap = folder / f"{stamp}_{kind}.csv"
        while snap.exists():                                       # two pulls in one second keep both
            n += 1
            snap = folder / f"{stamp}-{n}_{kind}.csv"
        write_text_atomic(snap, text)
        out = {"snapshot": str(snap), "day_file": None, "refused": problem, "usable": whole_market and not problem, "rows": rows,
               "superseded_by": newer}
        if as_day and not problem and not newer:
            target = config.days_dir() / f"{session}.market.csv"
            write_text_atomic(target, text)
            out["day_file"] = str(target)
        return out


def _stamp_time(name: str) -> tuple[dt.datetime, int, bool] | None:
    """(when the pull was taken, its number within that second, whether the name is the old
    kind) from a snapshot's file name."""
    stamp, _, number = name.split("_")[0].partition("-")
    fmt = _STAMP_BY_LENGTH.get(len(stamp))                 # by length: strptime would happily read 1837 as 18:03:07
    if not fmt or not stamp.replace("T", "").isdigit():
        return None
    try:
        return dt.datetime.strptime(stamp, fmt).replace(tzinfo=config.ET), int(number or 1), fmt == OLD_STAMP
    except ValueError:
        return None


def latest_snapshot(session: str) -> tuple[Path, dt.datetime] | None:
    """The newest whole-market pull that passed its checks, with the time it was taken (ET).

    A name with seconds in it was written after the checks. An older name was not, so such a
    file is judged now, by the same rules as a new pull; one that fails is renamed `_rejected`
    and the one before it is tried.
    """
    folder = config.snapshots_dir() / session
    found = [(key, p) for p in (folder.glob("*_market.csv") if folder.exists() else []) if (key := _stamp_time(p.name))]
    for (when, _, old), path in sorted(found, key=lambda f: f[0][:2], reverse=True):
        if old:
            try:
                problem = judge_market(path.read_text(encoding="utf-8"), session, _carries_the_close(session, when))[1]
            except (OSError, UnicodeDecodeError):
                problem = "it cannot be read"
            if problem:
                with contextlib.suppress(OSError):         # replace, not rename: on Windows a rename onto an existing name fails
                    os.replace(path, path.with_name(path.name.replace("_market.csv", "_rejected.csv")))
                continue
        return path, when
    return None


def read_snapshot(path: Path, day: str) -> pd.DataFrame:
    return _labelled(_read_raw(path, "market"), day, "snapshot", reporters(previous_close(day), day))


def _check_import(name: str, raw: bytes) -> str | None:
    """Why a collector's end-of-day file cannot become a day, or None when it can."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "not readable as JSON"
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list) or not data["rows"]:
        return "no rows"
    day = name[:10]
    if data.get("date") != day:
        return f"it says it is for {data.get('date')!r}"
    minute = data.get("et")
    if isinstance(minute, bool) or not isinstance(minute, (int, float)) or not math.isfinite(minute) or not 0 <= minute < 1440:
        return "it does not say when it was captured"
    if minute < config.close_minute(sessions.as_date(day)):
        return f"captured at {int(minute) // 60:02d}:{int(minute) % 60:02d} ET, before the close"
    rows = data["rows"]
    whole = [r for r in rows if isinstance(r, dict) and all(h in r for h in REQUIRED)]
    if len(whole) < 0.99 * len(rows):
        return f"{len(rows) - len(whole)} of {len(rows)} rows are not complete stock rows"
    usable = [r for r in whole if isinstance(r.get("Ticker"), str) and r["Ticker"].strip() not in ("", "nan", "None")
              and (_number(r.get("Price")) or 0) > 0]              # the names a ticker turns into when it was never one
    if len(usable) < 0.99 * len(rows):
        return f"{len(rows) - len(usable)} of {len(rows)} rows have no ticker or no price"
    if all("Exchange Traded Fund" in str(r.get("Industry") or "") for r in usable):
        return "it holds no stocks, only funds"
    return None


def import_lfr(folder: Path) -> dict:
    """Copy a separate collector's end-of-day files (<date>.json)
    into the store. A file becomes a day only when it is named for its own date, says it was
    captured after that session's close, and holds complete stock rows.
    Returns {"added", "unchanged", "rejected": [(name, why)]}."""
    out = {"added": 0, "unchanged": 0, "rejected": []}
    for p in sorted(Path(folder).glob("20*.json")):
        if not _IMPORT_NAME.match(p.name) or not _is_date(p.name[:10]):
            out["rejected"].append((p.name, "the name is not <date>.json"))
            continue
        raw = p.read_bytes()
        problem = _check_import(p.name, raw)
        if problem:
            out["rejected"].append((p.name, problem))
            continue
        target = config.days_dir() / f"{p.name[:10]}.lfr.json"
        with _store_lock():
            if target.exists() and target.read_bytes() == raw:
                out["unchanged"] += 1
                continue
            write_bytes_atomic(target, raw)
        out["added"] += 1
    return out


def status() -> dict:
    files = _source_files()
    days = trading_days(files)
    by_source: dict[str, int] = {}
    for d in days:
        by_source[files[d][1]] = by_source.get(files[d][1], 0) + 1
    cal = load_earnings()
    when = cal["when"].dropna()
    not_sessions = sorted(d for d in files if d not in days)
    return {"home": str(config.home()), "trading_days": len(days), "first": days[0] if days else None,
            "last": days[-1] if days else None, "by_source": by_source, "closed_days": not_sessions,
            "earnings_rows": int(len(cal)), "earnings_from": f"{when.min():%Y-%m-%d}" if len(when) else None,
            "earnings_to": f"{when.max():%Y-%m-%d}" if len(when) else None}
