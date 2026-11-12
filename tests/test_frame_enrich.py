import numpy as np
import pandas as pd
import pytest

from conftest import export_csv, row
from finviz_cli import columns as C
from finviz_cli import enrich, frame, sessions, store

DAY = pd.Timestamp("2026-11-10")
PREV = pd.Timestamp("2026-11-09 16:00")


def table(rows, thousands=True):
    df = frame.to_frame(frame.parse_csv(export_csv(rows)), avgvol_in_thousands=thousands)
    df.insert(0, "date", "2026-11-10")
    return df


def test_numbers_percent_signs_and_average_volume_units():
    df = table([row("AAA", 2.00, 2.00, 2.60, 1.90, 2.50, 3_000_000)])
    r = df.iloc[0]
    assert r.chg == pytest.approx(25.0) and r.gap == pytest.approx(0.0) and r.short_float == pytest.approx(5.0)
    assert r.avgvol == pytest.approx(500_000)                       # 500.00 thousand
    assert r.dollar_vol == pytest.approx(7_500_000)
    assert r.rotation == pytest.approx(0.3)                         # 3M shares on a 10M float
    assert r.high_vs_open == 30.0 and r.low_vs_open == -5.0         # exactly: no 30.000000000000004
    assert r.close_pos == pytest.approx((2.5 - 1.9) / (2.6 - 1.9), abs=1e-6)
    assert r.kept == pytest.approx(25.0 / 30.0)
    assert r.from_high == pytest.approx(-3.846154)                  # negative: percent under the day's high
    top = table([row("TOP", 7.00, 7.00, 7.80, 6.95, 7.80, 1_000_000), row("FLAT", 7.00, 7.00, 7.00, 6.95, 6.98, 1_000_000)])
    assert top.kept.iloc[0] == 1.0 and np.isnan(top.kept.iloc[1])   # closed at the high: all of it, never more; no run: no share
    assert isinstance(r.ticker, str) and r.news == ""


def test_average_volume_in_shares_is_left_alone():
    df = table([row("AAA", 2.00, 2.00, 2.60, 1.90, 2.50, 3_000_000)], thousands=False)
    assert df.avgvol.iloc[0] == pytest.approx(500.0)


def test_a_text_column_with_nothing_in_it_is_still_text():
    rows = [{"Ticker": "AAA", "Price": 2.0, "Earnings Date": float("nan"), "News Title": None},
            {"Ticker": "BBB", "Price": 3.0, "Earnings Date": float("nan"), "News Title": None}]       # as an imported file can hold them
    df = frame.to_frame(rows, avgvol_in_thousands=False)
    assert df.earnings.tolist() == ["", ""] and df.news.tolist() == ["", ""] and df.price.tolist() == [2.0, 3.0]


def test_a_table_with_no_rows_keeps_its_columns():
    headers, rows = frame.parse_table("Ticker,Price,Shares Float\n")
    df = frame.to_frame(rows, True, headers)
    assert list(df.columns)[:3] == ["ticker", "price", "float_m"] and df.empty


@pytest.mark.parametrize("prev,open_,high,low,price,expected", [
    (2.00, 2.00, 2.60, 1.95, 2.50, "flat open, ran and held"),          # +30% high, kept 25 of 30
    (2.00, 2.00, 2.60, 1.95, 2.10, "flat open, ran, gave it back"),     # kept 5 of 30
    (2.00, 2.00, 2.60, 1.80, 1.85, "flat open, ran, gave it back"),     # ran, then closed under the open
    (2.00, 2.00, 2.10, 1.80, 1.85, "flat open, faded"),
    (2.00, 2.00, 2.10, 1.95, 2.05, "flat open, held"),
    (2.00, 2.30, 2.90, 2.25, 2.80, "gap up, ran and held"),
    (2.00, 2.30, 2.35, 2.00, 2.05, "gap up, faded"),
    (2.00, 2.30, 2.40, 2.25, 2.32, "gap up, held"),
    (2.00, 1.80, 2.20, 1.75, 2.15, "gap down, ran and held"),
    (2.00, 1.80, 2.20, 1.70, 1.82, "gap down, ran, gave it back"),
    (2.00, 2.00, 2.60, 1.95, 2.30, "flat open, ran and held"),          # kept exactly half: 15 of 30
    (2.00, 2.00, 2.60, 1.95, 2.29, "flat open, ran, gave it back"),     # a cent less than half
    (3.00, 3.00, 3.30, 2.95, 3.25, "flat open, ran and held"),          # 3.30 / 3.00 is a 10% run, not 9.99999999999999
    (3.00, 3.00, 3.29, 2.95, 3.25, "flat open, held"),                  # 9.67%: short of a run
    (7.00, 7.00, 8.00, 6.95, 7.50, "flat open, ran and held"),          # half of a 14.2857% run: Finviz says 7.14%, not 7.142857
    (7.00, 7.00, 7.80, 6.95, 7.40, "flat open, ran and held"),          # the same with 5.71% against 5.714286
    (7.00, 7.00, 8.00, 6.95, 7.49, "flat open, ran, gave it back"),     # a cent short of half
])
def test_shape_of_the_day(prev, open_, high, low, price, expected):
    df = enrich.add_shape(table([row("AAA", prev, open_, high, low, price, 1_000_000)]))
    assert df["shape"].iloc[0] == expected


def test_mover_needs_price_dollar_volume_relative_volume_and_a_move():
    rows = [row("MOVE", 2.00, 2.00, 2.60, 1.95, 2.50, 3_000_000),
            row("THIN", 2.00, 2.00, 2.60, 1.95, 2.50, 100_000),                                     # only $250k traded
            row("SLOW", 2.00, 2.00, 2.60, 1.95, 2.50, 3_000_000, **{"Relative Volume": "1.50"}),
            row("PENNY", 0.50, 0.50, 0.70, 0.49, 0.65, 9_000_000),
            row("FLAT", 2.00, 2.00, 2.05, 1.98, 2.02, 3_000_000),
            row("EDGE", 3.00, 3.00, 3.30, 2.95, 3.05, 3_000_000),                                   # a run of exactly 10%
            row("NOFLOAT", 2.00, 2.00, 2.60, 1.95, 2.50, 3_000_000, **{"Shares Float": ""})]         # a mover needs no float
    df = enrich.add_flags(enrich.add_shape(table(rows))).set_index("ticker")
    assert df.mover.to_dict() == {"MOVE": True, "THIN": False, "SLOW": False, "PENNY": False, "FLAT": False, "EDGE": True,
                                  "NOFLOAT": True}
    assert df.close_kind["MOVE"] == "strong close" and np.isnan(df.rotation["NOFLOAT"])


def test_close_kind_weak_and_in_between():
    rows = [row("WEAK", 2.00, 2.40, 2.80, 2.00, 2.10, 3_000_000), row("MID", 2.00, 2.00, 2.60, 2.00, 2.30, 3_000_000)]
    df = enrich.add_flags(enrich.add_shape(table(rows))).set_index("ticker")
    assert df.close_kind.to_dict() == {"WEAK": "weak close", "MID": "in between"}


def test_catalyst_labels():
    rows = [row("EARN", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"Earnings Date": "11/9/2026 4:30:00 PM", "News Time": "2026-11-10 08:00:00"}),
            row("EARNAM", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"Earnings Date": "11/10/2026 8:30:00 AM"}),
            row("OLDEARN", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"Earnings Date": "11/9/2026 8:30:00 AM"}),      # before the previous close
            row("NEWS", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"News Time": "2026-11-09 17:30:00"}),
            row("EARLIER", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"News Time": "2026-11-05 09:00:00"}),
            row("NONE", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"News Time": "2026-09-30 16:42:00"}),
            row("CAL", 2, 2, 2.6, 1.95, 2.5, 3e6)]
    df = enrich.same_day(table(rows), DAY, PREV, reporters={"CAL"}).set_index("ticker")
    assert df.catalyst.to_dict() == {"EARN": "earnings", "EARNAM": "earnings", "OLDEARN": "none", "NEWS": "news",
                                     "EARLIER": "earlier", "NONE": "none", "CAL": "earnings"}
    assert df.news_age["EARLIER"] == 5 and df.days_to_earn["EARN"] == -1


def test_a_headline_after_the_close_does_not_explain_the_session():
    split = "Example Inc announces 1-for-10 reverse stock split"
    rows = [row("INDAY", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"News Time": "2026-11-10 15:59:00"}),
            row("LATE", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"News Time": "2026-11-10 16:30:00"}),               # after the close
            row("NEXTDAY", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"News Time": "2026-11-11 07:00:00", "News Title": split}),
            row("SPLITNEWS", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"News Time": "2026-11-06 09:00:00", "News Title": split}),
            row("SPLITTODAY", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"News Time": "2026-11-10 09:00:00", "News Title": split}),
            row("SPLITLATE", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"News Time": "2026-11-10 16:30:00", "News Title": split}),     # same day, after the close
            row("LATEEARN", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"Earnings Date": "11/10/2026 4:30:00 PM", "News Time": "2026-11-10 16:31:00"})]
    df = enrich.same_day(table(rows), DAY, PREV).set_index("ticker")
    assert df.catalyst.to_dict() == {"INDAY": "news", "LATE": "after close", "NEXTDAY": "after close", "SPLITNEWS": "earlier",
                                     "SPLITTODAY": "news", "SPLITLATE": "after close",
                                     "LATEEARN": "after close"}                # that report belongs to the next session
    assert df.split_headline.to_dict() == {"INDAY": False, "LATE": False, "NEXTDAY": False, "SPLITNEWS": True, "SPLITTODAY": True,
                                           "SPLITLATE": False, "LATEEARN": False}


def test_an_early_close_moves_the_cutoff():
    friday, monday = "2026-11-27", "2026-11-30"                     # the day after Thanksgiving closes at 13:00
    assert store.close_of(friday) == pd.Timestamp("2026-11-27 13:00") and store.previous_close(monday) == pd.Timestamp("2026-11-27 13:00")
    assert store.close_of("2026-12-24") == pd.Timestamp("2026-12-24 13:00") and store.close_of(monday) == pd.Timestamp("2026-11-30 16:00")
    rows = [row("RPT", 2, 2, 2.6, 1.95, 2.5, 3e6, **{"Earnings Date": "11/27/2026 2:00:00 PM", "News Time": "2026-11-27 14:00:00"})]

    def label(day):
        df = frame.to_frame(frame.parse_csv(export_csv(rows)), True)
        df.insert(0, "date", day)
        return enrich.same_day(df, pd.Timestamp(day), store.previous_close(day), None, store.close_of(day)).catalyst.iloc[0]

    assert label(friday) == "after close" and label(monday) == "earnings"       # 14:00 is after Friday's close


def di(date):
    return sessions.ordinal(sessions.as_date(date))


def stack(days):
    """days: list of (date, rows). Returns the same-day enriched stack with di, as the store builds it."""
    parts = []
    for date, rows in days:
        df = frame.to_frame(frame.parse_csv(export_csv(rows)), True)
        df.insert(0, "date", date)
        df = enrich.same_day(df, pd.Timestamp(date), store.previous_close(date), None, store.close_of(date))
        parts.append(df.assign(di=di(date)))
    return pd.concat(parts, ignore_index=True)


def quiet(t, p, f="10.00"):
    return row(t, p, p, p * 1.01, p * 0.99, p, 100_000, **{"Shares Float": f})


def test_reverse_split_exact_and_across_a_gap():
    days = [("2026-11-04", [quiet("SPLT", 0.30), quiet("GAPD", 0.20), quiet("NORM", 5.00)]),
            ("2026-11-05", [quiet("SPLT", 0.30), quiet("NORM", 5.00)]),
            ("2026-11-06", [row("SPLT", 3.00, 3.00, 3.10, 2.90, 3.00, 100_000, **{"Shares Float": "1.00"}), quiet("NORM", 5.00)]),
            ("2026-11-09", [row("GAPD", 4.00, 4.00, 4.10, 3.90, 4.00, 100_000, **{"Shares Float": "0.50"}),
                            row("NORM", 5.00, 5.00, 9.90, 5.00, 9.80, 100_000)])]   # NORM doubles on its own: not a split
    ev = enrich.find_splits(stack(days)).set_index("ticker")
    assert set(ev.index) == {"SPLT", "GAPD"}
    assert ev.loc["SPLT", "exact"] and ev.loc["SPLT", "ratio"] == pytest.approx(10.0) and ev.loc["SPLT", "date"] == "2026-11-06"
    assert not ev.loc["GAPD", "exact"] and ev.loc["GAPD", "ratio"] == pytest.approx(20.0)


def test_a_two_day_move_across_a_missing_day_is_not_a_split():
    # 11-03 is not saved. JUMP closed 1.00 on 11-02 and shows a previous close of 1.90 on 11-04: it
    # simply rose on the missing day. REAL shows the same jump with its float cut to a tenth.
    days = [("2026-11-02", [quiet("JUMP", 1.00), quiet("REAL", 1.00)]),
            ("2026-11-04", [row("JUMP", 1.90, 1.90, 1.95, 1.85, 1.90, 100_000),
                            row("REAL", 10.0, 10.0, 10.2, 9.8, 10.0, 100_000, **{"Shares Float": "1.00"})])]
    ev = enrich.find_splits(stack(days))
    assert ev.ticker.tolist() == ["REAL"] and not ev.exact.iloc[0]
    hist = enrich.add_history(stack(days))
    assert (hist[hist.date == "2026-11-04"].yesterday == "no history").all()       # the session before is not in the data


def test_a_zero_price_or_float_is_bad_data_not_a_split():
    zero = row("ZERO", 1.00, 1.00, 1.00, 1.00, 1.00, 100_000)
    zero.update({"Price": "0", "Change": "0%", "Change from Open": "0%"})
    days = [("2026-11-04", [zero, quiet("NOFLOAT", 1.00)]),
            ("2026-11-06", [quiet("ZERO", 5.00), quiet("NOFLOAT", 3.00, "0")])]       # a gap, so the float has to confirm
    assert enrich.find_splits(stack(days)).empty                      # 5.00 / 0 and 10 / 0 are not ratios


def test_history_fields():
    mover = lambda t: row(t, 2.00, 2.00, 2.60, 1.95, 2.50, 3_000_000)          # strong close
    weak = lambda t: row(t, 2.00, 2.40, 2.80, 2.00, 2.10, 3_000_000)
    calm = lambda t, v=200_000, **kw: row(t, 2.00, 2.00, 2.05, 1.98, 2.02, v, **kw)
    big = row("BIG", 20.0, 20.0, 20.5, 19.8, 20.2, 5_000_000, **{"Shares Float": "250.00"})
    days = [("2026-11-05", [mover("RUN"), calm("CALM"), calm("THIN", 10_000)]),
            ("2026-11-06", [mover("RUN"), weak("WEAK"), calm("CALM"), calm("THIN", 10_000)]),
            ("2026-11-09", [mover("RUN"), calm("WEAK"), calm("CALM"), calm("NEW"), calm("THIN"), big,
                            calm("NOFLOAT", **{"Shares Float": ""})])]
    df = enrich.add_history(stack(days), float_cap_by_di={di("2026-11-06"): 100.0})
    last = df[df.date == "2026-11-09"].set_index("ticker")
    assert last.yesterday.to_dict() == {"RUN": "mover, strong close", "WEAK": "mover, weak close", "CALM": "traded, not a mover",
                                        "NEW": "quiet (under 50k shares)", "THIN": "quiet (under 50k shares)", "BIG": "not covered",
                                        "NOFLOAT": "not covered"}        # with no float on record it cannot be called quiet
    assert last.streak.to_dict()["RUN"] == 2 and last.streak.to_dict()["WEAK"] == 1 and last.streak.to_dict()["CALM"] == 0
    assert (df[df.date == "2026-11-05"].yesterday == "no history").all()
    assert np.isnan(last.days_since_split["RUN"])


def test_a_name_that_was_listed_but_barely_traded_was_quiet_not_uncovered():
    calm = lambda t, v, **kw: row(t, 2.00, 2.00, 2.05, 1.98, 2.02, v, **kw)
    days = [("2026-11-06", [calm("THIN", 10_000), calm("BIGTHIN", 10_000)]),
            ("2026-11-09", [calm("THIN", 200_000, **{"Shares Float": ""}), calm("BIGTHIN", 200_000, **{"Shares Float": "250.00"}),
                            calm("GONE", 200_000, **{"Shares Float": "250.00"})])]
    last = enrich.add_history(stack(days), float_cap_by_di={di("2026-11-06"): 100.0}).query("date == '2026-11-09'").set_index("ticker")
    # THIN and BIGTHIN were in Friday's data with 10,000 shares: that is known, whatever their float says today.
    assert last.yesterday.to_dict() == {"THIN": "quiet (under 50k shares)", "BIGTHIN": "quiet (under 50k shares)", "GONE": "not covered"}


def test_split_clock_counts_sessions():
    days = [("2026-11-04", [quiet("SPLT", 0.30)]), ("2026-11-05", [row("SPLT", 3.00, 3.00, 3.10, 2.90, 3.00, 100_000)]),
            ("2026-11-06", [quiet("SPLT", 3.00)]), ("2026-11-09", [quiet("SPLT", 3.00)])]
    df = enrich.add_history(stack(days)).sort_values("di")
    assert df.days_since_split.tolist()[1:] == [0.0, 1.0, 2.0] and np.isnan(df.days_since_split.iloc[0])
    assert df.split_ratio.iloc[-1] == pytest.approx(10.0)


def test_streak_and_split_clock_count_sessions_not_saved_days():
    mover = lambda t: row(t, 2.00, 2.00, 2.60, 1.95, 2.50, 3_000_000)
    # Nothing is saved between 10-01 and 11-09: 27 sessions apart, but neighbours in the store.
    days = [("2026-09-30", [quiet("SPLT", 0.30), mover("RUN")]),
            ("2026-10-01", [row("SPLT", 3.00, 3.00, 3.10, 2.90, 3.00, 100_000), mover("RUN")]),
            ("2026-11-09", [quiet("SPLT", 3.00), mover("RUN")])]
    last = enrich.add_history(stack(days)).query("date == '2026-11-09'").set_index("ticker")
    assert last.streak["RUN"] == 0                                    # its mover days are a month old
    assert last.days_since_split["SPLT"] == di("2026-11-09") - di("2026-10-01") == 27
    assert last.yesterday["RUN"] == "no history"


def test_splits_can_be_handed_in_instead_of_searched_for():
    days = [("2026-11-06", [quiet("SPLT", 3.00)]), ("2026-11-09", [quiet("SPLT", 3.00)])]
    known = pd.DataFrame([{"date": "2026-10-01", "di": di("2026-10-01"), "ticker": "SPLT", "ratio": 10.0, "exact": True}])
    df = enrich.add_history(stack(days), splits=known).sort_values("di")
    assert df.days_since_split.tolist() == [di("2026-11-06") - di("2026-10-01"), di("2026-11-09") - di("2026-10-01")]
    assert enrich.add_history(stack(days)).days_since_split.isna().all()         # the two days alone do not show it


def test_column_presets_and_keys():
    assert C.key_for_header("Shares Float") == "float_m" and C.key_for_header("Change from Open") == "open_chg"
    assert C.key_for_header("Something New (%)") == "something_new_pct"
    assert C.resolve("price,relvol")[0] == 1                           # the ticker is always included
    assert C.resolve("1,65") == [1, 65] and C.resolve(None) == C.PRESETS["wide"]
    assert not set(C.PRESETS["all"]) & C.ETF_ONLY and len(C.HEADERS) == 156
    with pytest.raises(ValueError):
        C.resolve("not_a_column")
