"""`finviz intraday`: each session summed up from minute bars. Offline, like every other test."""
import datetime as dt
import io
import json
import math
import zoneinfo

import pandas as pd
import pytest
from click.testing import CliRunner

from finviz_cli import cli as cli_mod
from finviz_cli import config, intraday
from finviz_cli.cli import cli
from finviz_cli.client import FinvizError

TUE = "2026-11-10"          # a full session
MON = "2026-11-09"
EARLY = "2026-11-27"        # the day after Thanksgiving: closes at 13:00

# One session with all three parts. The premarket high (2.60) is reached twice; the day's
# high (3.50) is reached twice; the first regular bar to pass the premarket high is 10:15.
FULL_DAY = [
    (f"{TUE} 04:05", 2.00, 2.40, 2.00, 2.30, 100),
    (f"{TUE} 08:00", 2.30, 2.60, 2.20, 2.50, 200),
    (f"{TUE} 09:29", 2.50, 2.60, 2.50, 2.55, 50),
    (f"{TUE} 09:30", 2.50, 2.55, 2.45, 2.50, 1000),
    (f"{TUE} 10:15", 2.50, 3.00, 2.50, 2.90, 3000),
    (f"{TUE} 11:00", 2.90, 3.50, 2.80, 3.40, 4000),
    (f"{TUE} 13:00", 3.40, 3.50, 3.00, 3.10, 500),
    (f"{TUE} 15:59", 3.10, 3.20, 3.00, 3.00, 800),
    (f"{TUE} 16:00", 3.00, 3.10, 2.90, 3.10, 70),
    (f"{TUE} 19:59", 3.10, 3.30, 3.10, 3.30, 30),
]


def frame(rows, ticker="RUN"):
    """Bars as endpoints.bars hands them on: our column names, times as 'YYYY-MM-DD HH:MM'."""
    return pd.DataFrame([{"ticker": ticker, "date": when, "open": o, "high": h, "low": l, "close": c, "volume": v}
                         for when, o, h, l, c, v in rows],
                        columns=["ticker", "date", "open", "high", "low", "close", "volume"])


def daily(*rows):
    """Daily bars: (date, volume) or (date, volume, high, low)."""
    return pd.DataFrame([{"ticker": "RUN", "date": d, "open": 1.0, "high": (rest or [1.0])[0], "low": (rest or [0, 1.0])[1],
                          "close": 1.0, "volume": v} for d, v, *rest in rows],
                        columns=["ticker", "date", "open", "high", "low", "close", "volume"])


def only(table):
    assert len(table) == 1
    return table.iloc[0].to_dict()


def empty(value):
    return isinstance(value, float) and math.isnan(value)


# ---------------------------------------------------------------- the summary

def test_a_session_is_read_in_its_three_parts():
    table, left_out = intraday.summarize(frame(FULL_DAY), "run")
    got = only(table)
    assert left_out == 0 and list(table.columns) == intraday.COLUMNS
    assert (got["date"], got["ticker"]) == (TUE, "RUN")
    assert (got["pm_high"], got["pm_high_at"]) == (2.60, "08:00")                     # the first bar to reach it, not the last
    assert (got["open"], got["high"], got["high_at"], got["low"], got["low_at"]) == (2.50, 3.50, "11:00", 2.45, "09:30")
    assert (got["close"], got["last_at"], got["bars"]) == (3.00, "15:59", 5)
    assert (got["high_vs_open"], got["low_vs_open"], got["open_chg"]) == (40.0, -2.0, 20.0)
    assert got["kept"] == 0.5 and got["close_pos"] == round((3.00 - 2.45) / (3.50 - 2.45), 6)
    assert got["pmh_break_at"] == "10:15"
    assert (got["ah_close"], got["ah_chg"]) == (3.30, 10.0)
    assert (got["bar_pm_vol"], got["bar_vol"], got["bar_ah_vol"]) == (350, 9300, 100)
    assert got["live"] is False or got["live"] == False                                 # noqa: E712 (a numpy bool)
    assert empty(got["vol"]) and empty(got["bar_cover"])                                # no daily bars were given


def test_bars_out_of_order_give_the_same_answer():
    straight, _ = intraday.summarize(frame(FULL_DAY), "RUN")
    shuffled, _ = intraday.summarize(frame(FULL_DAY[::-1]), "RUN")
    pd.testing.assert_frame_equal(straight, shuffled)


def test_an_early_close_ends_the_session_at_one_oclock():
    rows = [(f"{EARLY} 09:30", 5.0, 5.2, 4.9, 5.1, 100), (f"{EARLY} 12:59", 5.1, 5.6, 5.1, 5.5, 200),
            (f"{EARLY} 13:00", 5.5, 9.0, 5.5, 8.0, 300), (f"{EARLY} 15:00", 8.0, 8.5, 1.0, 2.0, 400)]
    got = only(intraday.summarize(frame(rows), "RUN")[0])
    assert (got["high"], got["high_at"], got["low"], got["close"], got["last_at"]) == (5.6, "12:59", 4.9, 5.5, "12:59")
    assert (got["bar_vol"], got["bar_ah_vol"], got["ah_close"]) == (300, 700, 2.0)      # what came after 13:00 is after hours
    # The same bars on a full session all belong to it.
    full = only(intraday.summarize(frame([(w.replace(EARLY, TUE), *rest) for w, *rest in rows]), "RUN")[0])
    assert (full["high"], full["low"], full["close"], full["bar_ah_vol"]) == (9.0, 1.0, 2.0, 0)


def test_a_session_with_no_premarket_has_no_premarket_high_to_pass():
    got = only(intraday.summarize(frame(FULL_DAY[3:]), "RUN")[0])
    assert empty(got["pm_high"]) and got["pm_high_at"] == "" and got["pmh_break_at"] == "" and got["bar_pm_vol"] == 0
    assert got["high"] == 3.50


def test_touching_the_premarket_high_is_not_passing_it():
    rows = [(f"{TUE} 08:00", 3.0, 3.5, 3.0, 3.4, 10), (f"{TUE} 09:30", 3.4, 3.5, 3.2, 3.3, 10), (f"{TUE} 10:00", 3.3, 3.4, 3.1, 3.2, 10)]
    got = only(intraday.summarize(frame(rows), "RUN")[0])
    assert got["pm_high"] == 3.5 and got["high"] == 3.5 and got["pmh_break_at"] == ""
    rows.append((f"{TUE} 10:01", 3.2, 3.51, 3.2, 3.5, 10))
    assert only(intraday.summarize(frame(rows), "RUN")[0])["pmh_break_at"] == "10:01"


def test_before_the_open_only_the_premarket_is_there():
    before = dt.datetime(2026, 11, 10, 9, 0)
    got = only(intraday.summarize(frame(FULL_DAY[:3]), "RUN", now=before)[0])
    assert (got["pm_high"], got["pm_high_at"], got["bars"], got["bar_vol"]) == (2.60, "08:00", 0, 0)
    assert all(empty(got[c]) for c in ("open", "high", "low", "close", "kept", "high_vs_open", "ah_close"))
    assert (got["high_at"], got["low_at"], got["last_at"], got["pmh_break_at"]) == ("", "", "", "")
    assert bool(got["live"]) is True


def test_only_the_session_still_trading_is_live():
    two = frame([(f"{MON} 09:30", 1.0, 1.2, 0.9, 1.1, 10), (f"{MON} 15:59", 1.1, 1.3, 1.1, 1.2, 10)] + FULL_DAY[:5])
    table, _ = intraday.summarize(two, "RUN", now=dt.datetime(2026, 11, 10, 10, 16))
    assert table.date.tolist() == [MON, TUE] and table.live.tolist() == [False, True]          # oldest first
    assert table.close.tolist() == [1.2, 2.90] and table.last_at.tolist() == ["15:59", "10:15"]
    after, _ = intraday.summarize(two, "RUN", now=dt.datetime(2026, 11, 10, 16, 0))
    assert after.live.tolist() == [False, False]                                               # at the close it is over
    # A clock with a zone is read in Eastern time: 15:00 UTC is 10:00 in New York that day.
    zoned, _ = intraday.summarize(two, "RUN", now=dt.datetime(2026, 11, 10, 15, 0, tzinfo=zoneinfo.ZoneInfo("UTC")))
    assert zoned.live.tolist() == [False, True]
    assert intraday.summarize(two, "RUN")[0].live.tolist() == [False, False]                    # no clock given: nothing is live


def test_an_early_close_is_over_at_one_oclock():
    rows = frame([(f"{EARLY} 09:30", 5.0, 5.2, 4.9, 5.1, 100)])
    assert intraday.summarize(rows, "RUN", now=dt.datetime(2026, 11, 27, 12, 59))[0].live.tolist() == [True]
    assert intraday.summarize(rows, "RUN", now=dt.datetime(2026, 11, 27, 13, 0))[0].live.tolist() == [False]


def test_a_run_that_never_left_the_open_has_nothing_kept():
    rows = [(f"{TUE} 09:30", 3.0, 3.0, 2.5, 2.6, 10), (f"{TUE} 10:00", 2.6, 2.7, 2.4, 2.5, 10)]
    got = only(intraday.summarize(frame(rows), "RUN")[0])
    assert empty(got["kept"]) and got["high_vs_open"] == 0.0 and got["close_pos"] == round(0.1 / 0.6, 6)
    flat = only(intraday.summarize(frame([(f"{TUE} 09:30", 3.0, 3.0, 3.0, 3.0, 10)]), "RUN")[0])
    assert empty(flat["kept"]) and empty(flat["close_pos"]) and flat["open_chg"] == 0.0


def test_bars_dated_on_days_that_are_not_sessions_are_left_out_and_counted():
    rows = FULL_DAY + [("2026-11-07 10:00", 9.0, 9.9, 9.0, 9.5, 10),       # a Saturday
                       ("2026-11-26 10:00", 9.0, 9.9, 9.0, 9.5, 10),       # Thanksgiving
                       ("2026-11-26 10:01", 9.0, 9.9, 9.0, 9.5, 10)]
    table, left_out = intraday.summarize(frame(rows), "RUN")
    assert table.date.tolist() == [TUE] and left_out == 3


def test_the_whole_days_volume_comes_from_the_daily_bar():
    two = frame([(f"{MON} 09:30", 1.0, 1.2, 0.9, 1.1, 600)] + FULL_DAY)
    table, _ = intraday.summarize(two, "RUN", daily=daily((TUE, 19500), ("2026-11-06", 5)))
    got = table.set_index("date")
    assert got.vol[TUE] == 19500 and got.bar_cover[TUE] == 0.5                 # 350 + 9300 + 100 of 19,500
    assert empty(got.vol[MON]) and empty(got.bar_cover[MON])                   # a day the daily bars do not have
    none, _ = intraday.summarize(frame(FULL_DAY), "RUN", daily=daily((TUE, 0)))
    assert only(none)["vol"] == 0 and empty(only(none)["bar_cover"])           # nothing to divide by
    for unusable in (pd.DataFrame(), pd.DataFrame({"date": [TUE]}), daily((TUE, "n/a")), daily((None, 5)), daily((TUE, -5))):
        assert empty(only(intraday.summarize(frame(FULL_DAY), "RUN", daily=unusable)[0])["vol"])


def test_the_daily_bars_own_high_and_low_stand_beside_the_minute_bars():
    """The two are different feeds: on a thin day the minute bars reach further than the
    official high and low. Both are shown; the summary itself is made from the minute bars."""
    got = only(intraday.summarize(frame(FULL_DAY), "RUN", daily=daily((TUE, 19500, 3.40, 2.47)))[0])
    assert (got["high"], got["day_high"], got["low"], got["day_low"]) == (3.50, 3.40, 2.45, 2.47)
    assert got["high_vs_open"] == 40.0                                         # still from the minute bars
    for high, low in ((0, 2.47), (-1, 2.47), ("n/a", 2.47), (None, 2.47)):     # a figure that makes no sense is left out, the other kept
        got = only(intraday.summarize(frame(FULL_DAY), "RUN", daily=daily((TUE, 19500, high, low)))[0])
        assert empty(got["day_high"]) and got["day_low"] == 2.47 and got["vol"] == 19500
    assert empty(only(intraday.summarize(frame(FULL_DAY), "RUN")[0])["day_high"])


def test_nullable_columns_are_read_like_any_other():
    """pandas' own missing value (in an Int64 or Float64 column) is a missing value, not a crash."""
    bars = frame(FULL_DAY).astype({"open": "Float64", "high": "Float64", "low": "Float64", "close": "Float64", "volume": "Int64"})
    figures = pd.DataFrame({"date": pd.array([TUE, MON, None], dtype="string"),
                            "volume": pd.array([19500, None, 7], dtype="Int64"),
                            "high": pd.array([None, 3.0, 3.0], dtype="Float64"), "low": pd.array([2.47, None, 1.0], dtype="Float64")})
    got = only(intraday.summarize(bars, "RUN", daily=figures)[0])
    assert (got["high"], got["kept"], got["bar_vol"]) == (3.50, 0.5, 9300)
    assert got["vol"] == 19500 and got["bar_cover"] == 0.5 and empty(got["day_high"]) and got["day_low"] == 2.47
    bars.at[3, "close"] = pd.NA
    with pytest.raises(FinvizError, match="1 of 10 RUN bars cannot be used"):
        intraday.summarize(bars, "RUN")
    bars = frame(FULL_DAY).astype({"volume": "Int64"})
    bars.at[0, "volume"] = pd.NA
    with pytest.raises(FinvizError, match="1 of 10 RUN bars cannot be used"):
        intraday.summarize(bars, "RUN")


@pytest.mark.parametrize("day, last_regular, first_after", [(TUE, "15:5%d", "16:00"), (EARLY, "12:5%d", "13:00")])
@pytest.mark.parametrize("width", [3, 5])
def test_three_and_five_minute_bars_split_at_the_same_moments(width, day, last_regular, first_after):
    """Bars as wide as Finviz's own: the last premarket bar starts `width` minutes before
    09:30, the last regular one `width` minutes before the close."""
    rows = [(f"{day} 09:{30 - width:02d}", 4.0, 9.0, 4.0, 4.5, 10),              # premarket: its 9.00 is not the session's high
            (f"{day} 09:30", 5.0, 5.5, 4.9, 5.2, 20),
            (f"{day} {last_regular % (10 - width)}", 5.2, 6.0, 5.1, 5.8, 30),
            (f"{day} {first_after}", 5.8, 8.0, 1.0, 7.0, 40)]                    # after hours: neither its 8.00 nor its 1.00 counts
    got = only(intraday.summarize(frame(rows), "RUN")[0])
    assert (got["pm_high"], got["pm_high_at"]) == (9.0, f"09:{30 - width:02d}")
    assert (got["open"], got["high"], got["low"], got["close"], got["bars"]) == (5.0, 6.0, 4.9, 5.8, 2)
    assert (got["high_at"], got["last_at"], got["ah_close"]) == (last_regular % (10 - width), last_regular % (10 - width), 7.0)
    assert (got["bar_pm_vol"], got["bar_vol"], got["bar_ah_vol"]) == (10, 50, 40) and got["pmh_break_at"] == ""


def test_no_bars_is_an_empty_table_with_its_columns_and_their_types():
    table, left_out = intraday.summarize(frame([]), "RUN")
    assert len(table) == 0 and list(table.columns) == intraday.COLUMNS and left_out == 0
    full, _ = intraday.summarize(frame(FULL_DAY), "RUN")
    assert table.dtypes.astype(str).to_dict() == full.dtypes.astype(str).to_dict()         # the same types, rows or not
    assert len(table[~table.live]) == 0 and list(table[~table.live].columns) == intraday.COLUMNS
    joined = pd.concat([table, full], ignore_index=True)
    assert joined.dtypes.astype(str).to_dict() == full.dtypes.astype(str).to_dict() and joined.high.tolist() == [3.50]
    only_a_saturday, left_out = intraday.summarize(frame([("2026-11-07 10:00", 9.0, 9.9, 9.0, 9.5, 10)]), "RUN")
    assert len(only_a_saturday) == 0 and left_out == 1
    assert only_a_saturday.dtypes.astype(str).to_dict() == full.dtypes.astype(str).to_dict()


# Row 4 rises (open 2.50, close 2.90), row 6 falls (open 3.40, close 3.10): a high or a low
# between the two ends of such a bar contradicts only one of them.
@pytest.mark.parametrize("row, change", [
    (3, {"open": 0.0, "low": 0.0}),                      # a price of nothing
    (3, {"low": -1.0}),
    (3, {"volume": -5}),
    (4, {"high": 2.70}),                                 # a high over the bar's open but under its close
    (6, {"high": 3.20}),                                 # ... and over its close but under its open
    (4, {"low": 2.70}),                                  # a low under the bar's close but over its open
    (6, {"low": 3.20}),                                  # ... and under its open but over its close
    (3, {"close": float("nan")}),
    (3, {"close": "n/a"}),
    (3, {"volume": None}),
    (3, {"close": True}),                                # True is not a price of 1.00
    (3, {"volume": dt.datetime(2026, 11, 10)}),
    (3, {"close": 2 + 3j}),
    (3, {"high": 1e308}),                                # beyond any price
    (3, {"low": 1e-300, "open": 1e-300}),
    (3, {"volume": 1e308}),                              # two of these would add up to infinity
    (3, {"date": "sometime"}),
    (3, {"date": None}),
    (3, {"date": f"{TUE} 09:30:15"}),                    # not the form endpoints.bars writes
])
def test_a_bar_that_cannot_be_used_stops_the_summary(row, change):
    bars = frame(FULL_DAY).astype(object)
    for col, value in change.items():
        bars.at[row, col] = value
    with pytest.raises(FinvizError, match="1 of 10 RUN bars cannot be used"):
        intraday.summarize(bars, "RUN")


@pytest.mark.parametrize("kind", ["bool", "datetime64[ns]", "timedelta64[ns]", "complex128"])
def test_a_column_of_a_type_that_is_no_number_is_not_read_as_one(kind):
    bars = frame(FULL_DAY)
    bars["volume"] = pd.Series([1] * len(bars)).astype(kind)
    with pytest.raises(FinvizError, match="10 of 10 RUN bars cannot be used"):
        intraday.summarize(bars, "RUN")
    figures = daily((TUE, 19500))
    figures["volume"] = pd.Series([1]).astype(kind)
    assert empty(only(intraday.summarize(frame(FULL_DAY), "RUN", daily=figures)[0])["vol"])


def test_bars_without_their_columns_are_refused_rows_or_not():
    with pytest.raises(FinvizError, match="the RUN bars came back without: low, volume"):
        intraday.summarize(frame(FULL_DAY).drop(columns=["low", "volume"]), "RUN")
    # With no rows too: an error text reads as a table of one column and no rows, and that
    # is not "no bars".
    with pytest.raises(FinvizError, match="the RUN bars came back without: low, volume"):
        intraday.summarize(frame([]).drop(columns=["low", "volume"]), "RUN")
    with pytest.raises(FinvizError, match="the RUN bars came back without: date, open, high, low, close, volume"):
        intraday.summarize(pd.DataFrame({"error": []}), "RUN")
    with pytest.raises(FinvizError, match="came back without"):
        intraday.summarize(pd.DataFrame(), "RUN")


def test_columns_named_twice_are_refused_and_wrong_arguments_say_so():
    twice = frame(FULL_DAY)
    twice.insert(3, "high", twice.pop("high"), allow_duplicates=True)
    twice.insert(4, "high", 9.9, allow_duplicates=True)
    with pytest.raises(FinvizError, match="the RUN bars do not have one plain name per column"):
        intraday.summarize(twice, "RUN")
    levels = frame(FULL_DAY)
    levels.columns = pd.MultiIndex.from_product([levels.columns, ["RUN"]])
    with pytest.raises(FinvizError, match="one plain name per column"):
        intraday.summarize(levels, "RUN")
    for bars, ticker, now in ((None, "RUN", None), (frame(FULL_DAY), None, None), (frame(FULL_DAY), "RUN", dt.date(2026, 11, 10))):
        with pytest.raises(TypeError, match="must be"):
            intraday.summarize(bars, ticker, now=now)


def test_daily_figures_of_an_odd_shape_are_left_out_not_a_crash():
    twice = pd.DataFrame([[TUE, TUE, 19500, 7]], columns=["date", "date", "volume", "volume"])          # the first of each is read
    assert only(intraday.summarize(frame(FULL_DAY), "RUN", daily=twice)[0])["vol"] == 19500
    levels = daily((TUE, 19500))
    levels.columns = pd.MultiIndex.from_product([levels.columns, ["RUN"]])
    for odd in (levels, daily((TUE, 19500))["volume"], "not a table", 7):
        assert empty(only(intraday.summarize(frame(FULL_DAY), "RUN", daily=odd)[0])["vol"])
    upside_down = only(intraday.summarize(frame(FULL_DAY), "RUN", daily=daily((TUE, 19500, 2.00, 3.00)))[0])
    assert empty(upside_down["day_high"]) and empty(upside_down["day_low"]) and upside_down["vol"] == 19500   # a high under its own low
    padded = daily((f" {TUE} ", 19500))
    assert only(intraday.summarize(frame(FULL_DAY), "RUN", daily=padded)[0])["vol"] == 19500


def test_two_bars_for_one_minute_are_refused():
    """Which of the two is the open, and whose volume counts? Neither answer is safe."""
    doubled = FULL_DAY + [(f"{TUE} 09:30", 2.80, 2.90, 2.75, 2.85, 1000)]
    for rows in (doubled, doubled[::-1]):
        with pytest.raises(FinvizError, match="2 of 11 RUN bars cannot be used.*two bars carry the same time"):
            intraday.summarize(frame(rows), "RUN")


def test_figures_no_float_can_hold_are_not_figures():
    bars = frame(FULL_DAY).astype(object)
    bars.at[0, "volume"] = 10 ** 400                                            # a whole number too long for any float
    with pytest.raises(FinvizError, match="1 of 10 RUN bars cannot be used"):
        intraday.summarize(bars, "RUN")
    mixed = frame(FULL_DAY)
    mixed["volume"] = pd.Categorical([True] + [10] * 9, categories=pd.Index([True, 10], dtype=object))
    with pytest.raises(FinvizError, match="1 of 10 RUN bars cannot be used"):   # True among the categories is still not a count
        intraday.summarize(mixed, "RUN")
    counted = frame(FULL_DAY)
    counted["volume"] = pd.Categorical([10] * 10)
    assert only(intraday.summarize(counted, "RUN")[0])["bar_vol"] == 50          # numbers in a category column are numbers


def test_only_rows_dated_as_daily_bars_are_read_as_daily_figures():
    # Minute bars handed in as the daily ones: every row has a time of day, so none is read.
    got = only(intraday.summarize(frame(FULL_DAY), "RUN", daily=frame(FULL_DAY))[0])
    assert empty(got["vol"]) and empty(got["day_high"]) and empty(got["bar_cover"])
    # Two rows for one day: the first is taken whole, not a figure from each.
    two = pd.DataFrame([{"date": TUE, "volume": 19500, "high": 3.40, "low": 2.47},
                        {"date": TUE, "volume": None, "high": 2.60, "low": None},
                        {"date": TUE, "volume": 7, "high": 9.0, "low": 8.0}])
    got = only(intraday.summarize(frame(FULL_DAY), "RUN", daily=two)[0])
    assert (got["vol"], got["day_high"], got["day_low"]) == (19500, 3.40, 2.47)
    first_is_empty = pd.DataFrame([{"date": TUE, "volume": None, "high": None, "low": None}, {"date": TUE, "volume": 7, "high": 9.0, "low": 8.0}])
    got = only(intraday.summarize(frame(FULL_DAY), "RUN", daily=first_is_empty)[0])
    assert empty(got["vol"]) and empty(got["day_high"]) and empty(got["day_low"])


def test_a_low_reached_twice_is_timed_at_the_first_bar():
    rows = [(f"{TUE} 09:30", 3.0, 3.1, 2.9, 3.0, 10), (f"{TUE} 10:00", 3.0, 3.2, 2.8, 2.9, 10),
            (f"{TUE} 11:00", 2.9, 3.0, 2.8, 2.9, 10), (f"{TUE} 12:00", 2.9, 3.2, 2.9, 3.1, 10)]
    got = only(intraday.summarize(frame(rows), "RUN")[0])
    assert (got["low"], got["low_at"], got["high"], got["high_at"]) == (2.8, "10:00", 3.2, "10:00")


# ---------------------------------------------------------------- the command

def finviz_bars(rows):
    """The same bars as Finviz sends them: US dates, a 24-hour clock and a stray AM/PM."""
    out = ["Date,Open,High,Low,Close,Volume"]
    for when, o, h, l, c, v in rows:
        t = dt.datetime.strptime(when, "%Y-%m-%d %H:%M")
        out.append(f"{t:%m/%d/%Y %H:%M} {'AM' if t.hour < 12 else 'PM'},{o},{h},{l},{c},{v}")
    return "\n".join(out) + "\n"


def finviz_daily(*rows):
    return "Date,Open,High,Low,Close,Volume\n" + "".join(f"{dt.date.fromisoformat(d):%m/%d/%Y},1,1,1,1,{v}\n" for d, v in rows)


MONDAY = [(f"{MON} 09:30", 1.0, 1.2, 0.9, 1.1, 600), (f"{MON} 15:59", 1.1, 1.3, 1.1, 1.2, 400)]


class Feed:
    answers = {}
    calls = []

    def export(self, path, params=None):
        params = dict(params or {})
        Feed.calls.append((path, params))
        key = (params.get("t"), params.get("p"))
        if path != "stock" or key not in Feed.answers:
            raise FinvizError("Finviz answered HTTP 400 for /export/stock")
        return Feed.answers[key]


class Clock:
    now = dt.datetime(2026, 11, 10, 18, 0, tzinfo=config.ET)


@pytest.fixture
def run(home, monkeypatch):
    Feed.calls = []
    Feed.answers = {("RUN", "i1"): finviz_bars(MONDAY + FULL_DAY), ("RUN", "d"): finviz_daily((MON, 2000), (TUE, 19500)),
                    ("FADE", "i1"): finviz_bars([(f"{TUE} 09:30", 4.0, 4.0, 3.0, 3.2, 900)]), ("FADE", "d"): finviz_daily((TUE, 1800)),
                    ("RUN", "i5"): finviz_bars(FULL_DAY[:5]), ("QUIET", "i1"): "Date,Open,High,Low,Close,Volume\n",
                    ("QUIET", "d"): "Date,Open,High,Low,Close,Volume\n"}
    Clock.now = dt.datetime(2026, 11, 10, 18, 0, tzinfo=config.ET)
    monkeypatch.setattr(cli_mod, "Client", Feed)
    monkeypatch.setattr(config, "now_et", lambda: Clock.now)
    runner = CliRunner()
    return lambda *args: runner.invoke(cli, ["intraday", *args])


def rows_of(result):
    return [json.loads(x) for x in result.stdout.splitlines() if x.startswith("{")]


def test_the_command_shows_each_session_newest_first(run):
    r = run("run", "--format", "ndjson")
    assert r.exit_code == 0, r.output
    got = rows_of(r)
    assert [x["date"] for x in got] == [TUE, MON] and list(got[0]) == intraday.DEFAULT_SHOW
    assert (got[0]["pm_high"], got[0]["high_at"], got[0]["kept"], got[0]["pmh_break_at"], got[0]["vol"]) == (2.6, "11:00", 0.5, "10:15", 19500)
    assert got[1]["pm_high"] is None and got[1]["pm_high_at"] == "" and got[1]["vol"] == 2000
    assert Feed.calls == [("stock", {"t": "RUN", "p": "i1", "r": ""}), ("stock", {"t": "RUN", "p": "d", "r": "m3"})]
    assert "RUN i1 bars: 12 rows, 2026-11-09 09:30 .. 2026-11-10 19:59; 2 sessions" in r.output
    assert "still trading" not in r.output                                                # it is 18:00: both sessions are over


def test_days_and_show_narrow_what_is_printed(run):
    r = run("RUN", "--days", "1", "--show", "date,kept,bar_cover,live", "--format", "ndjson")
    assert rows_of(r) == [{"date": TUE, "kept": 0.5, "bar_cover": 0.5, "live": False}]
    everything = rows_of(run("RUN", "--show", "all", "--format", "ndjson"))
    assert list(everything[0]) == intraday.COLUMNS and everything[1]["bar_cover"] == 0.5 and everything[1]["bars"] == 2
    r = run("RUN", "--show", "date,nonsense")
    assert r.exit_code != 0 and "unknown column(s): nonsense" in r.output
    assert run("RUN", "--days", "0").exit_code == 2


def test_several_tickers_keep_the_order_they_were_asked_in(run):
    r = run("run", "fade", "RUN", "--format", "ndjson")
    assert r.exit_code == 0, r.output
    assert [(x["ticker"], x["date"]) for x in rows_of(r)] == [("RUN", TUE), ("RUN", MON), ("FADE", TUE)]
    assert [c[1]["t"] for c in Feed.calls] == ["RUN", "RUN", "FADE", "FADE"]              # asked for twice, fetched once
    assert rows_of(r)[2]["kept"] is None and rows_of(r)[2]["open_chg"] == -20.0


def test_a_session_still_trading_is_said_to_be(run):
    Clock.now = dt.datetime(2026, 11, 10, 10, 20, tzinfo=config.ET)
    r = run("RUN", "--tf", "i5", "--show", "date,close,last_at,live", "--format", "ndjson")
    assert rows_of(r) == [{"date": TUE, "close": 2.9, "last_at": "10:15", "live": True}]
    assert "RUN: the 2026-11-10 session is still trading; its close is the last price, at 10:15 ET" in r.output
    assert Feed.calls[0] == ("stock", {"t": "RUN", "p": "i5", "r": ""})
    Feed.answers[("RUN", "i5")] = finviz_bars(FULL_DAY[:3])
    Clock.now = dt.datetime(2026, 11, 10, 9, 29, tzinfo=config.ET)
    r = run("RUN", "--tf", "i5", "--show", "date,pm_high,open,live", "--format", "ndjson")
    assert rows_of(r) == [{"date": TUE, "pm_high": 2.6, "open": None, "live": True}]
    assert "RUN: the 2026-11-10 session has not traded yet; only its premarket is shown" in r.output


def test_only_bars_that_carry_the_premarket_are_offered(run):
    r = run("RUN", "--tf", "i15")
    assert r.exit_code == 2 and "'i15' is not one of 'i1', 'i3', 'i5'" in r.output and Feed.calls == []


def test_a_ticker_finviz_does_not_know_is_named_in_the_error(run, capsys):
    r = run("RUN", "nope")
    assert r.exit_code != 0 and isinstance(r.exception, FinvizError) and str(r.exception).startswith("NOPE: Finviz answered HTTP 400")
    assert rows_of(r) == []                                                               # nothing half-printed
    with pytest.raises(SystemExit) as stop:                                               # and through the real entry point: one line, exit 2
        cli_mod.main(["intraday", "nope"])
    assert stop.value.code == 2 and capsys.readouterr().err.strip() == "error: NOPE: Finviz answered HTTP 400 for /export/stock"


def test_a_blank_ticker_is_a_usage_error_before_anything_is_asked_for(run):
    for blank in ("", "   "):
        r = run(blank, "--format", "json")
        assert r.exit_code == 2 and "no ticker given" in r.output and Feed.calls == []
    assert run().exit_code == 2                                                          # and so is none at all
    r = run(" run ", "", "--days", "1", "--format", "ndjson")                             # blanks beside a real one are just dropped
    assert [x["ticker"] for x in rows_of(r)] == ["RUN"]


def test_a_ticker_with_no_bars_prints_no_rows(run):
    r = run("quiet", "--format", "csv")
    assert r.exit_code == 0, r.output
    assert "QUIET i1 bars: 0 rows; 0 sessions" in r.output and ",".join(intraday.DEFAULT_SHOW) in r.output
    r = run("quiet", "run", "--days", "1", "--format", "ndjson")
    assert [x["ticker"] for x in rows_of(r)] == ["RUN"]


def test_the_table_view_prints_times_and_big_counts(run):
    r = run("RUN", "--show", "date,high_at,vol,bar_vol,bar_pm_vol,bar_ah_vol", "--format", "table")
    assert r.exit_code == 0, r.output
    line = next(x for x in r.output.splitlines() if x.startswith(TUE))
    assert line.split() == [TUE, "11:00", "20k", "9k", "350", "100"]                      # 19,500 and 9,300 shares; small counts as they are
    monday = next(x for x in r.output.splitlines() if x.startswith(MON))
    assert monday.split() == [MON, "15:59", "2k", "1k", "0", "0"]


def test_the_table_view_keeps_the_digits_of_small_prices(run):
    """Two decimals would print a stock at four tenths of a cent as 0.00."""
    Feed.answers[("PENNY", "i1")] = finviz_bars([(f"{TUE} 09:30", 0.0040, 0.0045, 0.0035, 0.0042, 100)])
    Feed.answers[("PENNY", "d")] = finviz_daily((TUE, 100))
    Feed.answers[("CENTS", "i1")] = finviz_bars([(f"{TUE} 09:30", 0.42, 0.4567, 0.40, 0.44, 100)])
    Feed.answers[("CENTS", "d")] = finviz_daily((TUE, 100))
    r = run("PENNY", "--show", "date,open,high,low,close,high_vs_open", "--format", "table")
    assert next(x for x in r.output.splitlines() if x.startswith(TUE)).split() == [TUE, "0.004", "0.0045", "0.0035", "0.0042", "12.5"]
    r = run("CENTS", "--show", "date,open,high,low,close", "--format", "table")
    assert next(x for x in r.output.splitlines() if x.startswith(TUE)).split() == [TUE, "0.42", "0.4567", "0.4", "0.44"]
    r = run("RUN", "--days", "1", "--show", "date,open,high,kept", "--format", "table")       # from a dollar up: two, as before
    assert next(x for x in r.output.splitlines() if x.startswith(TUE)).split() == [TUE, "2.5", "3.5", "0.5"]


@pytest.mark.parametrize("fmt", ["ndjson", "json", "csv", "table"])
def test_rows_go_to_stdout_and_notes_to_stderr_in_every_format(run, capsys, fmt):
    """Through the real entry point, where the two streams are apart: what a program reads
    from stdout is the rows and nothing else."""
    with pytest.raises(SystemExit) as stop:
        cli_mod.main(["intraday", "RUN", "--show", "date,ticker,high,high_at,kept,live", "--format", fmt])
    out, err = capsys.readouterr()
    assert stop.value.code == 0
    assert "RUN i1 bars: 12 rows" in err and "vol is the whole day" in err and "bars:" not in out and "vol is" not in out
    first, second = {"date": TUE, "ticker": "RUN", "high": 3.5, "high_at": "11:00", "kept": 0.5, "live": False}, \
                    {"date": MON, "ticker": "RUN", "high": 1.3, "high_at": "15:59", "kept": 2 / 3, "live": False}
    if fmt == "ndjson":
        got = [json.loads(x) for x in out.splitlines()]
    elif fmt == "json":
        got = json.loads(out)
    elif fmt == "csv":
        got = pd.read_csv(io.StringIO(out)).to_dict("records")
    else:
        assert [x.split() for x in out.splitlines()] == [["date", "ticker", "high", "high_at", "kept", "live"],
                                                         [TUE, "RUN", "3.5", "11:00", "0.50", "False"],
                                                         [MON, "RUN", "1.3", "15:59", "0.67", "False"]]
        return
    assert len(got) == 2
    for row, want in zip(got, (first, second)):
        assert {k: v for k, v in row.items() if k != "kept"} == {k: v for k, v in want.items() if k != "kept"}
        assert row["kept"] == pytest.approx(want["kept"], abs=1e-6)


def test_an_answer_that_is_not_bars_is_an_error_not_an_empty_result(run):
    for body, why in (("Invalid parameter\n", "came back without its Date column"),          # one line of something else
                      ("Date,Open,High,Low,Close,Volume\n11/10/2026 09:30 AM,2.5,2.55,2.45,2.5,10", "did not arrive whole")):
        Feed.answers[("RUN", "i1")] = body
        r = run("RUN", "--format", "ndjson")
        assert r.exit_code != 0 and isinstance(r.exception, FinvizError) and why in str(r.exception), r.output
        assert str(r.exception).startswith("RUN: ") and rows_of(r) == []
    Feed.answers[("RUN", "i1")] = finviz_bars(FULL_DAY)
    Feed.answers[("RUN", "d")] = "Service unavailable\n"                                     # the daily bars, the same
    r = run("RUN", "--format", "ndjson")
    assert r.exit_code != 0 and "came back without its Date column" in str(r.exception)


def test_live_is_judged_as_of_when_the_minute_bars_were_asked_for(run, monkeypatch):
    """The second request can come back after the close (pacing, a retry). The minute bars
    were asked for before it, so the session they show had not closed."""
    moments = iter([dt.datetime(2026, 11, 10, 15, 59, 58, tzinfo=config.ET)])
    monkeypatch.setattr(config, "now_et", lambda: next(moments, dt.datetime(2026, 11, 10, 16, 0, 30, tzinfo=config.ET)))
    r = run("RUN", "--days", "1", "--show", "date,live", "--format", "ndjson")
    assert rows_of(r) == [{"date": TUE, "live": True}] and "still trading" in r.output


class Timed(Feed):
    """A client that says when each request left and was answered, as the real one does."""
    sent = {}                     # bar size -> the moment that request left

    def export(self, path, params=None):
        body = super().export(path, params)
        self.last_sent = Timed.sent[(params or {}).get("p")].timestamp()
        self.last_received = self.last_sent + 1.0
        return body


@pytest.mark.parametrize("minute_bars_left, live", [((15, 59, 59), True),      # asked for before the close: the session was open
                                                    ((16, 0, 2), False)])     # held up in the queue until after it: it was over
def test_live_goes_by_when_the_minute_bar_request_left_not_the_daily_one(run, monkeypatch, minute_bars_left, live):
    monkeypatch.setattr(cli_mod, "Client", Timed)
    Timed.sent = {"i1": dt.datetime(2026, 11, 10, *minute_bars_left, tzinfo=config.ET),
                  "d": dt.datetime(2026, 11, 10, 16, 0, 5, tzinfo=config.ET)}
    Clock.now = dt.datetime(2026, 11, 10, 15, 59, 58, tzinfo=config.ET)       # the command's own look at the clock is neither
    r = run("RUN", "--days", "1", "--show", "date,live", "--format", "ndjson")
    assert rows_of(r) == [{"date": TUE, "live": live}], r.output


def test_bars_that_cannot_be_read_as_a_table_or_bring_their_own_ticker(run):
    Feed.answers[("RUN", "i1")] = "Date,Open,High,Low,Close,Volume\r11/10/2026 09:30 AM,2.5,2.55,2.45,2.5,10\r"      # lines ending some other way
    r = run("RUN", "--format", "ndjson")
    assert isinstance(r.exception, FinvizError) and "could not be read as a table" in str(r.exception)
    Feed.answers[("RUN", "i1")] = "Ticker,Date,Open,High,Low,Close,Volume\nOTHER,11/10/2026 09:30 AM,2.5,2.55,2.45,2.5,10\n"
    r = run("RUN", "--days", "1", "--show", "date,ticker,open", "--format", "ndjson")
    assert r.exit_code == 0 and rows_of(r) == [{"date": TUE, "ticker": "RUN", "open": 2.5}], r.output


def test_a_column_that_is_not_there_is_said_before_anything_is_asked_for(run):
    r = run("RUN", "FADE", "--show", "date,nonsense")
    assert r.exit_code != 0 and "unknown column(s): nonsense" in r.output and Feed.calls == []


def test_bars_on_a_day_that_is_no_session_are_mentioned(run):
    Feed.answers[("RUN", "i1")] = finviz_bars(FULL_DAY + [("2026-11-07 10:00", 9.0, 9.9, 9.0, 9.5, 10)])
    r = run("RUN", "--format", "ndjson")
    assert [x["date"] for x in rows_of(r)] == [TUE]
    assert "1 sessions; 1 bars dated on days that are not sessions were left out" in r.output
