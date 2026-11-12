"""What happened inside each session, read from minute bars. Rules: docs/enrichment.md, "Inside the day"."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from finviz_cli import config, sessions
from finviz_cli.client import FinvizError
from finviz_cli.frame import PLACES

# Bar sizes that carry the hours around the session (04:00 to 20:00 ET). Finviz's 15- and
# 30-minute bars hold the regular session only, so they cannot say what the premarket did.
TIMEFRAMES = ("i1", "i3", "i5")
NEEDED = ("date", "open", "high", "low", "close", "volume")
OPEN_MINUTE = sessions.OPEN.hour * 60 + sessions.OPEN.minute

COLUMNS = ["date", "ticker", "pm_high", "pm_high_at", "open", "high", "high_at", "low", "low_at", "close", "last_at",
           "high_vs_open", "low_vs_open", "open_chg", "kept", "close_pos", "pmh_break_at", "ah_close", "ah_chg",
           "vol", "day_high", "day_low", "bar_cover", "bar_pm_vol", "bar_vol", "bar_ah_vol", "bars", "live"]
DEFAULT_SHOW = ["date", "ticker", "pm_high", "pm_high_at", "open", "high", "high_at", "low", "low_at", "close",
                "high_vs_open", "open_chg", "kept", "pmh_break_at", "vol"]
TIMES = ("pm_high_at", "high_at", "low_at", "last_at", "pmh_break_at")
TEXT = ("date", "ticker") + TIMES
COUNTS = ("bar_pm_vol", "bar_vol", "bar_ah_vol", "bars")

# What a price or a volume can be. Anything outside is a damaged figure, and keeping the
# sums and the ratios made from them inside what a float can hold is what these are for.
MIN_PRICE, MAX_PRICE = 1e-6, 1e7
MAX_BAR_VOLUME, MAX_DAY_VOLUME = 1e12, 1e15
DAILY = {"vol": ("volume", 0.0, MAX_DAY_VOLUME), "day_high": ("high", MIN_PRICE, MAX_PRICE),
         "day_low": ("low", MIN_PRICE, MAX_PRICE)}                    # ours: (the daily bar's, least, most)


def _typed(table: pd.DataFrame) -> pd.DataFrame:
    """Every column in its own type, rows or not: a table with no rows joins to one with rows."""
    kinds = {c: "str" if c in TEXT else "int64" if c in COUNTS else "bool" if c == "live" else "float64" for c in COLUMNS}
    return table.astype(kinds)


def _number_or_nothing(value):
    """Numbers and text pass (text is read as a number further on, or not at all). True, a
    date, a complex number: none of those is a price."""
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating, str)):
        return None
    if isinstance(value, int):
        try:
            return float(value)
        except OverflowError:                              # a whole number too long for any float is no figure either
            return None
    return value


def _floats(values: pd.Series | pd.DataFrame) -> np.ndarray:
    """Plain floats, whatever the column's type. Text that is no number, a missing value of
    any kind (None, NaN, pandas' own NA in a nullable column) and a value of a type that is
    no number at all (a boolean, a date, a duration, a complex number) come out as NaN."""
    table = values if isinstance(values, pd.DataFrame) else values.to_frame()
    out = np.full(table.shape, np.nan)
    for i in range(table.shape[1]):
        col = table.iloc[:, i]
        if isinstance(col.dtype, pd.CategoricalDtype):         # judged by what the categories are, one value at a time
            col = col.astype(object)
        kind = col.dtype
        if (pd.api.types.is_bool_dtype(kind) or pd.api.types.is_datetime64_any_dtype(kind)
                or pd.api.types.is_timedelta64_dtype(kind) or pd.api.types.is_complex_dtype(kind)):
            continue
        if kind == object:
            col = col.map(_number_or_nothing)
        out[:, i] = pd.to_numeric(col, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    return out if isinstance(values, pd.DataFrame) else out[:, 0]


def _text(values: pd.Series) -> pd.Series:
    return values.astype(object).where(values.notna(), "").astype(str)


def _require_columns(bars: pd.DataFrame, ticker: str) -> None:
    """Bars have these six columns, rows or not. An answer that is something else (an error
    text reads as a table of one column and no rows) must not pass for "no bars"."""
    if isinstance(bars.columns, pd.MultiIndex) or bars.columns.duplicated().any():
        raise FinvizError(f"the {ticker} bars do not have one plain name per column")
    missing = [c for c in NEEDED if c not in bars.columns]
    if missing:
        raise FinvizError(f"the {ticker} bars came back without: {', '.join(missing)}")


def _checked(bars: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """The bars with a real time on each, oldest first. A bar that cannot be used is an error:
    leaving it out could leave out the day's high."""
    df = bars[list(NEEDED)].reset_index(drop=True)
    numbers = _floats(df[list(NEEDED[1:])])
    when = pd.to_datetime(_text(df["date"]), format="%Y-%m-%d %H:%M", errors="coerce")
    o, h, l, c, v = (numbers[:, i] for i in range(5))
    with np.errstate(invalid="ignore"):                    # a missing value fails the first test; the others are then moot
        bad = (when.isna().to_numpy() | ~np.isfinite(numbers).all(axis=1) | (l < MIN_PRICE) | (h > MAX_PRICE)
               | (v < 0) | (v > MAX_BAR_VOLUME) | (h < np.maximum(o, c)) | (l > np.minimum(o, c)))
    # Two bars for one minute cannot both be right: which is the open, and whose volume counts?
    bad |= (when.notna() & when.duplicated(keep=False)).to_numpy()
    if bad.any():
        first = str(df["date"][bad].iloc[0])[:30]
        raise FinvizError(f"{int(bad.sum())} of {len(df)} {ticker} bars cannot be used: a time, a price or a volume is missing "
                          f"or impossible, the prices contradict each other, or two bars carry the same time (first: {first!r})")
    df[list(NEEDED[1:])] = numbers
    df["when"] = when
    return df.sort_values("when", kind="stable").reset_index(drop=True)


def _daily_figures(daily: pd.DataFrame | None) -> dict[str, dict[str, float]]:
    """Day -> what the daily bar says of it: volume, high, low. A figure that cannot be read
    is simply not there: these stand beside the summary, they are not what it is made from."""
    if not isinstance(daily, pd.DataFrame) or isinstance(daily.columns, pd.MultiIndex) or not len(daily):
        return {}
    daily = daily.loc[:, ~daily.columns.duplicated()]
    if "date" not in daily.columns:
        return {}
    # One row per day, and only rows dated as a daily bar is: a date and nothing more. Rows
    # with a time of day are some other kind of bar, and of two rows for one day the first
    # is taken whole rather than a figure from each.
    days = _text(daily["date"]).str.strip()
    use = (days.str.fullmatch(r"\d{4}-\d{2}-\d{2}") & ~days.duplicated()).to_numpy()
    days = days[use].tolist()
    found: dict[str, dict[str, float]] = {}
    for ours, (theirs, least, most) in DAILY.items():
        if theirs not in daily.columns:
            continue
        for day, value in zip(days, _floats(daily[theirs])[use]):
            if np.isfinite(value) and least <= value <= most:
                found.setdefault(day, {})[ours] = float(value)
    for figures in found.values():
        if figures.get("day_high", np.inf) < figures.get("day_low", -np.inf):     # a high under its own low: neither is to be trusted
            del figures["day_high"], figures["day_low"]
    return found


def _at(part: pd.DataFrame, position: int) -> str:
    return part["when"].iloc[position].strftime("%H:%M")


def _pct(value: float, base: float) -> float:
    return round((value / base - 1) * 100, PLACES)


def _session_row(day: dt.date, ticker: str, pre: pd.DataFrame, reg: pd.DataFrame, post: pd.DataFrame) -> dict:
    row = dict.fromkeys(COLUMNS, np.nan)
    row.update({k: "" for k in TIMES})
    row.update({"date": day.isoformat(), "ticker": ticker, "bars": len(reg),
                "bar_pm_vol": int(round(pre["volume"].sum())), "bar_vol": int(round(reg["volume"].sum())),
                "bar_ah_vol": int(round(post["volume"].sum()))})
    if len(pre):
        highs = pre["high"].to_numpy()
        row["pm_high"], row["pm_high_at"] = float(highs.max()), _at(pre, int(highs.argmax()))      # the first bar to reach it
    if len(reg):
        highs, lows = reg["high"].to_numpy(), reg["low"].to_numpy()
        o, h, l, c = float(reg["open"].iloc[0]), float(highs.max()), float(lows.min()), float(reg["close"].iloc[-1])
        row.update({"open": o, "high": h, "high_at": _at(reg, int(highs.argmax())), "low": l, "low_at": _at(reg, int(lows.argmin())),
                    "close": c, "last_at": _at(reg, -1), "high_vs_open": _pct(h, o), "low_vs_open": _pct(l, o),
                    "open_chg": _pct(c, o)})
        # The same two measures the screen has, from the prices themselves (docs/enrichment.md).
        if h > o:
            row["kept"] = round((c - o) / (h - o), PLACES)
        if h > l:
            row["close_pos"] = round((c - l) / (h - l), PLACES)
        if len(pre):
            above = np.flatnonzero(highs > row["pm_high"])
            if len(above):
                row["pmh_break_at"] = _at(reg, int(above[0]))
    if len(post):
        row["ah_close"] = float(post["close"].iloc[-1])
        if len(reg):
            row["ah_chg"] = _pct(row["ah_close"], row["close"])
    return row


def summarize(bars: pd.DataFrame, ticker: str, daily: pd.DataFrame | None = None,
              now: dt.datetime | None = None) -> tuple[pd.DataFrame, int]:
    """(one row per session, oldest first; bars left out because their date is not a session).

    `bars` is what endpoints.bars returns for a minute timeframe: times are Eastern, each bar
    stamped with the minute it starts. Before 09:30 is the premarket, from the session's close
    on is after hours (13:00 on an early close). `daily` gives the whole day's volume and the
    daily bar's own high and low, where it has the day. `now` (Eastern, or carrying its zone)
    is the moment the bars are from: a session that had not closed by then is marked live, and
    its close is only the last price.
    """
    if not isinstance(bars, pd.DataFrame):
        raise TypeError("bars must be a DataFrame, as endpoints.bars returns")
    if not isinstance(ticker, str):
        raise TypeError("ticker must be text")
    if now is not None and not isinstance(now, dt.datetime):
        raise TypeError("now must be a datetime (a date has no time of day to compare with the close)")
    ticker = ticker.upper()
    _require_columns(bars, ticker)
    if not len(bars):
        return _typed(pd.DataFrame(columns=COLUMNS)), 0
    df = _checked(bars, ticker)
    if now is not None and now.tzinfo is not None:
        now = now.astimezone(config.ET).replace(tzinfo=None)
    day_figures = _daily_figures(daily)
    minute = (df["when"].dt.hour * 60 + df["when"].dt.minute).to_numpy()
    days = df["when"].dt.date.to_numpy()
    rows, left_out = [], 0
    for day in sorted(set(days)):
        mine = days == day
        if not sessions.is_session(day):
            left_out += int(mine.sum())
            continue
        part, at = df[mine], minute[mine]
        closes = config.close_minute(day)
        row = _session_row(day, ticker, part[at < OPEN_MINUTE], part[(at >= OPEN_MINUTE) & (at < closes)], part[at >= closes])
        row["live"] = bool(now is not None and now < sessions.close_at(day))
        row.update(day_figures.get(row["date"], {}))
        if row["vol"] > 0:                                    # not when it is missing, and not when it is nothing
            row["bar_cover"] = round((row["bar_pm_vol"] + row["bar_vol"] + row["bar_ah_vol"]) / row["vol"], PLACES)
        rows.append(row)
    return _typed(pd.DataFrame(rows, columns=COLUMNS)), left_out
