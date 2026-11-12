"""Labels and history fields added on top of Finviz's own columns.

The definitions are documented in docs/enrichment.md and are kept stable so that results
stay comparable from one run to the next.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from finviz_cli import sessions

GAP = 5.0           # an open this far from the previous close counts as a gap (percent)
RUN = 10.0           # a move this far above the open counts as a run (percent)
MOVERS = "price >= 1 and dollar_vol >= 1e6 and relvol >= 2 and (high_vs_open >= 10 or chg >= 10 or gap >= 10)"
STREAK_DAYS = 10     # sessions looked back for the streak count
QUIET_SHARES = 50_000  # under this many shares a name counts as quiet that day
SPLIT_RATIO = 1.8    # previous close jumping this much against yesterday's close = reverse split
SPLIT_WORDS = r"reverse (?:stock |share )?split|share consolidation|share combination|consolidation of|ads ratio"
EPS = 1e-9           # slack for comparing two percentages that are equal on paper
NEWS_FORMAT = "%Y-%m-%d %H:%M:%S"
EARN_FORMAT = "%m/%d/%Y %I:%M:%S %p"
SPLIT_COLUMNS = ["date", "di", "ticker", "ratio", "exact", "price_before", "price_after", "float_before", "float_after"]
SPLIT_INPUT = ["ticker", "di", "date", "price", "prev_close", "float_m"]


def add_shape(df: pd.DataFrame) -> pd.DataFrame:
    """opening: gap up / flat open / gap down.  day: ran and held / ran, gave it back / faded / held."""
    g, oc, hvo = df.gap, df.open_chg, df.high_vs_open
    df["opening"] = np.select([g >= GAP, g <= -GAP], ["gap up", "gap down"], default="flat open")
    ran = hvo >= RUN
    if {"price", "open", "high"}.issubset(df.columns):
        # Half the run kept, judged on the prices: close - open against half of high - open.
        # Finviz's own "change from open" is rounded to two decimals, too coarse for this.
        kept_half = (2 * df.price - df.open - df.high) >= -EPS
    else:
        kept_half = oc >= hvo / 2 - EPS
    df["day"] = np.select([ran & kept_half, ran, oc <= -GAP], ["ran and held", "ran, gave it back", "faded"], default="held")
    df["shape"] = df.opening + ", " + df.day
    return df


def add_flags(df: pd.DataFrame) -> pd.DataFrame:
    """mover: the day's activity filter.  close_kind: where a mover finished in its range."""
    df["mover"] = df.eval(MOVERS, engine="python").fillna(False).astype(bool)
    strong = (df.close_pos >= 0.7) & (df.chg >= 10)
    weak = df.close_pos <= 0.3
    df["close_kind"] = np.select([strong, weak], ["strong close", "weak close"], default="in between")
    return df


def add_catalyst(df: pd.DataFrame, day: pd.Timestamp, prev_close: pd.Timestamp, reporters: set[str] | None = None,
                 close: pd.Timestamp | None = None) -> pd.DataFrame:
    """catalyst: what could have set the day off, from what was public before the close.

      earnings     reported between the previous close and this close
      news         the latest headline falls in that same window
      earlier      the latest headline is from the week before the previous close
      after close  the latest headline came after this close: it cannot explain the session,
                   and it hides whatever was published before it (Finviz gives one headline)
      none         nothing in the last week

    `close` is the session's closing time (13:00 on an early-close day; 16:00 when not given).
    `reporters` is the set of tickers on the earnings calendar for that window, used when the
    rows themselves carry no earnings date (days brought in with `history import-lfr`).
    """
    until = close if close is not None else day + pd.Timedelta(hours=16)
    news = pd.to_datetime(df.news_time, format=NEWS_FORMAT, errors="coerce") if "news_time" in df else pd.Series(pd.NaT, index=df.index)
    df["news_age"] = (day - news.dt.normalize()).dt.days
    is_after = news >= until
    is_news = (news >= prev_close) & ~is_after
    is_earlier = (news >= prev_close - pd.Timedelta(days=7)) & (news < prev_close)
    is_earn = pd.Series(False, index=df.index)
    if "earnings" in df:
        earn = pd.to_datetime(df.earnings, format=EARN_FORMAT, errors="coerce")
        df["days_to_earn"] = (earn.dt.normalize() - day).dt.days
        is_earn = (earn >= prev_close) & (earn < until)
    if reporters:
        is_earn = is_earn | df.ticker.isin(reporters)
    df["catalyst"] = np.select([is_earn, is_news, is_earlier, is_after], ["earnings", "news", "earlier", "after close"], default="none")
    if "news" in df:
        # Only a headline that was out by this close: a later one belongs to a later row.
        df["split_headline"] = (df.news.str.contains(SPLIT_WORDS, case=False, regex=True, na=False)
                                & ~is_after & (df.news_age >= 0) & (df.news_age <= 30))
    return df


def same_day(df: pd.DataFrame, day: pd.Timestamp, prev_close: pd.Timestamp, reporters: set[str] | None = None,
             close: pd.Timestamp | None = None) -> pd.DataFrame:
    need = {"gap", "open_chg", "high_vs_open", "close_pos", "chg", "price", "dollar_vol", "relvol"}
    if not need.issubset(df.columns):
        return df
    return add_catalyst(add_flags(add_shape(df)), day, prev_close, reporters, close)


def _previous_session_map(dates: pd.Series) -> dict[str, str]:
    return {d: sessions.previous_session(sessions.as_date(d)).isoformat() for d in dates.unique()}


def find_splits(history: pd.DataFrame) -> pd.DataFrame:
    """Reverse splits visible in a run of saved days.

    exact: the ticker was in the data for the session right before (by the trading calendar),
    so the split day is known. Otherwise sessions are missing in between; the jump must
    then come with the float shrinking by the same threshold, and the date is only known
    to within that gap.
    `history` needs: ticker, di (session number), date, price, prev_close, float_m. A split
    is only seen when the ticker's row before it is in `history` too, however far back.
    """
    if history.empty:
        return pd.DataFrame(columns=SPLIT_COLUMNS)
    h = history.sort_values(["ticker", "di"])
    g = h.groupby("ticker", sort=False)
    p_prev, date_prev, f_prev = g.price.shift(1), g.date.shift(1), g.float_m.shift(1)
    ratio = (h.prev_close / p_prev).where((p_prev > 0) & (h.prev_close > 0))      # a zero price is bad data, not a split
    shrink = (f_prev / h.float_m).where((f_prev > 0) & (h.float_m > 0))
    # "The session before" is decided by the calendar, not by which days happen to be saved:
    # with a day missing from the store, an ordinary two-day move must not read as a split.
    neighbours = date_prev == h.date.map(_previous_session_map(h.date))
    exact = neighbours & (ratio >= SPLIT_RATIO)
    approx = ~neighbours & date_prev.notna() & (ratio >= SPLIT_RATIO) & (shrink >= SPLIT_RATIO)
    ev = pd.DataFrame({"date": h.date, "di": h.di, "ticker": h.ticker, "ratio": ratio, "exact": exact, "price_before": p_prev,
                       "price_after": h.prev_close, "float_before": f_prev, "float_after": h.float_m})[exact | approx]
    return ev.reset_index(drop=True)[SPLIT_COLUMNS]


def add_history(df: pd.DataFrame, float_cap_by_di: dict[int, float] | None = None, splits: pd.DataFrame | None = None) -> pd.DataFrame:
    """Fields that need earlier sessions, for every row of a stack of days at once.

    `df` holds same-day enriched rows with `di` = the session number of each row's date
    (sessions.ordinal), so `di - 1` is always the session before, saved or not. Adds:
      yesterday         what the name did the session before: 'quiet (under 50k shares)' when it
                        was missing from that day's data, 'traded, not a mover', or 'mover, ...'
                        with how it closed. 'not covered' when that day's source stopped at a
                        float below this name's, or this name's float is unknown
                        (`float_cap_by_di`, millions of shares), and 'no history' when the
                        previous session is not in the stack.
      streak            mover days in the STREAK_DAYS sessions before this one
      days_since_split  sessions since the name's last reverse split (0 = first session after it)
      split_ratio       size of that split (10 = 1-for-10)
    `splits` are the reverse splits to count from (store.split_index: every saved day, so the
    answer does not depend on which days are in `df`). Without it they are looked for in `df`.
    """
    df = df.sort_values(["ticker", "di"]).reset_index(drop=True)
    for column in ("float_m", "vol", "close_kind"):               # a bare source may lack one: unknown, not a crash
        if column not in df:
            df[column] = np.nan
    df["mover"] = df["mover"].fillna(False).astype(bool)
    y = df[["ticker", "di", "mover", "close_kind", "vol"]].drop_duplicates(["ticker", "di"]).copy()
    y["di"] += 1
    m = df.merge(y, on=["ticker", "di"], how="left", suffixes=("", "_y"))
    in_feed = m.mover_y.notna() & ~(m.vol_y < QUIET_SHARES)       # a whole-market day lists quiet names too
    mover_y = m.mover_y.where(m.mover_y.notna(), False).astype(bool)
    cap = m.di.map(lambda d: (float_cap_by_di or {}).get(d - 1, np.inf)).astype(float)
    # Missing from a source that only lists small floats: quiet only if this name is known to be small.
    outside = (m.float_m >= cap) | (m.float_m.isna() & np.isfinite(cap))
    # Yesterday is only known when the session before this one is in the stack.
    date_of = df.drop_duplicates("di").set_index("di").date.to_dict()
    known = m.di.map(lambda d: date_of.get(d - 1)) == m.date.map(_previous_session_map(m.date))
    absent = m.mover_y.isna()                       # no row at all that day; a row with little volume is "quiet", whatever its float
    df["yesterday"] = np.select(
        [(~known).to_numpy(), (absent & outside).to_numpy(), (~in_feed).to_numpy(), (~mover_y).to_numpy(),
         (m.close_kind_y == "strong close").to_numpy(), (m.close_kind_y == "weak close").to_numpy()],
        ["no history", "not covered", "quiet (under 50k shares)", "traded, not a mover", "mover, strong close", "mover, weak close"],
        default="mover, in between")

    ev = find_splits(df) if splits is None else splits
    mover_days = {t: np.unique(g.to_numpy()) for t, g in df[df.mover].groupby("ticker").di}      # a session counts once
    split_days = {t: (g.di.to_numpy(), g.ratio.to_numpy()) for t, g in ev.sort_values("di").groupby("ticker")}
    streak = np.zeros(len(df), dtype=int)
    since = np.full(len(df), np.nan)
    ratio = np.full(len(df), np.nan)
    for ticker, rows in df.groupby("ticker", sort=False).indices.items():
        di = df.di.to_numpy()[rows]
        arr = mover_days.get(ticker)
        if arr is not None:
            streak[rows] = np.searchsorted(arr, di, "left") - np.searchsorted(arr, di - STREAK_DAYS, "left")
        sp = split_days.get(ticker)
        if sp is not None:
            k = np.searchsorted(sp[0], di, "right") - 1
            ok = k >= 0
            since[rows[ok]] = di[ok] - sp[0][k[ok]]
            ratio[rows[ok]] = sp[1][k[ok]]
    df["streak"], df["days_since_split"], df["split_ratio"] = streak, since, ratio
    return df
