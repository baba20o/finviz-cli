import datetime as dt
import io
import json
import os
import time

import pandas as pd
import pytest
from click.testing import CliRunner

from conftest import export_csv, lfr_json, row
from finviz_cli import cli as cli_mod
from finviz_cli import config, endpoints, output, sessions, store
from finviz_cli.cli import cli, session_day
from finviz_cli.client import FinvizError

ET = config.ET


def mover(t, **extra):
    return row(t, 2.00, 2.00, 2.60, 1.95, 2.50, 3_000_000, **extra)            # strong close


def weak(t):
    return row(t, 2.00, 2.40, 2.80, 2.00, 2.10, 3_000_000)                     # a mover that closed near its low


def calm(t, price=2.00, vol=200_000, **extra):
    return row(t, price, price, price * 1.02, price * 0.99, price * 1.01, vol, **extra)


def quiet(t, p, f="10.00"):
    return row(t, p, p, p * 1.01, p * 0.99, p, 100_000, **{"Shares Float": f})


def at(y, m, d, hh, mm, ss=0):
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=ET)


def seed(home):
    """Thursday and Friday from imported end-of-day files, Monday from a whole-market pull."""
    d = home / "days"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-11-05.lfr.json").write_text(lfr_json("2026-11-05", [mover("RUN"), calm("CALM"), calm("ZED", 3.0)]))
    (d / "2026-11-06.lfr.json").write_text(lfr_json("2026-11-06", [mover("RUN"), weak("WEAK"), calm("CALM", 2.02), calm("ZED", 3.03)]))
    monday = [mover("RUN"), weak("WEAK"), calm("CALM", 2.04), mover("NEW", **{"Earnings Date": "11/9/2026 8:30:00 AM"}),
              row("BIG", 20.0, 20.0, 20.5, 19.8, 20.2, 5_000_000, **{"Shares Float": "250.00"})]
    (d / "2026-11-09.market.csv").write_text(export_csv(monday))


def session_dates(first, count):
    out, d = [], dt.date.fromisoformat(first)
    while len(out) < count:
        if sessions.is_session(d):
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- calendar

def test_trading_calendar():
    closed = {"2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
              "2026-09-07", "2026-11-26", "2026-12-25", "2027-03-26", "2027-06-18", "2027-07-05", "2027-12-24"}
    for day in closed:
        assert not sessions.is_session(dt.date.fromisoformat(day)), day
    for day in ("2026-11-10", "2026-11-27", "2026-12-24", "2027-12-31", "2021-06-18"):   # Juneteenth was not observed before 2022
        assert sessions.is_session(dt.date.fromisoformat(day)), day
    assert sessions.previous_session(dt.date(2026, 9, 8)) == dt.date(2026, 9, 4)
    assert sessions.adjacent("2026-11-06", "2026-11-09") and not sessions.adjacent("2026-11-02", "2026-11-04")


def test_early_closes_and_session_numbers():
    early = [d.isoformat() for y in (2026, 2027, 2028) for d in sessions._year_sessions(y) if sessions.close_time(d) == sessions.EARLY_CLOSE]
    assert early == ["2026-11-27", "2026-12-24", "2027-11-26", "2028-07-03", "2028-11-24"]       # as NYSE publishes them
    n = sessions.ordinal
    assert n(dt.date(2026, 11, 9)) - n(dt.date(2026, 11, 6)) == 1                # across a weekend
    assert n(dt.date(2026, 11, 27)) - n(dt.date(2026, 11, 25)) == 1              # across Thanksgiving
    assert n(dt.date(2027, 1, 4)) - n(dt.date(2026, 12, 31)) == 1                # across a year end
    assert n(dt.date(2026, 11, 14)) == n(dt.date(2026, 11, 13))                  # a Saturday counts as its Friday
    year = sessions._year_sessions(2026)
    assert [n(d) - n(year[0]) for d in year] == list(range(len(year)))           # consecutive sessions differ by one


def test_session_day():
    assert session_day(at(2026, 11, 10, 18, 9)) == "2026-11-10"
    assert session_day(at(2026, 11, 10, 3, 0)) == "2026-11-09"        # before 04:00: still Monday's data
    assert session_day(at(2026, 11, 14, 12, 0)) == "2026-11-13"       # Saturday
    assert session_day(at(2026, 11, 16, 2, 0)) == "2026-11-13"        # early Monday
    assert session_day(at(2026, 11, 26, 17, 0)) == "2026-11-25"       # Thanksgiving
    assert config.is_after_close(at(2026, 11, 10, 16, 5)) and not config.is_after_close(at(2026, 11, 10, 15, 59))
    assert not config.is_after_close(at(2026, 11, 14, 17, 0))


# ---------------------------------------------------------------- store

def test_market_file_wins_and_units_agree(home):
    seed(home)
    assert store.trading_days() == ["2026-11-05", "2026-11-06", "2026-11-09"]
    (home / "days" / "2026-11-09.lfr.json").write_text(lfr_json("2026-11-09", [calm("ONLYLFR")]))
    df = store.load_day("2026-11-09")
    assert set(df.ticker) == {"RUN", "WEAK", "CALM", "NEW", "BIG"} and (df.source == "market").all()
    assert store.load_day("2026-11-06").avgvol.iloc[0] == pytest.approx(500_000) and df.avgvol.iloc[0] == pytest.approx(500_000)


def test_a_stock_with_no_float_on_record_is_kept(home):
    d = home / "days"
    d.mkdir(parents=True)
    rows = [mover("IPO", **{"Shares Float": ""}), calm("CALM"), calm("FUND", **{"Industry": "Exchange Traded Fund"})]
    (d / "2026-11-09.market.csv").write_text(export_csv(rows))
    day = store.load_day("2026-11-09").set_index("ticker")
    assert set(day.index) == {"IPO", "CALM"} and day.mover["IPO"] and pd.isna(day.float_m["IPO"])      # only the fund is left out


def test_other_files_in_the_days_folder_are_not_days(home):
    seed(home)
    d = home / "days"
    for name in ("2026_summa.lfr.json", "20261105.lfr.json", "2026-13-45.lfr.json", "notes.txt", "2026-11-09.market.csv.bak"):
        (d / name).write_text("not a day")
    assert store.trading_days() == ["2026-11-05", "2026-11-06", "2026-11-09"] and store.status()["trading_days"] == 3


def test_a_scheduled_holiday_file_is_ignored_and_its_neighbours_still_connect(home):
    d = home / "days"
    d.mkdir(parents=True)
    (d / "2026-09-04.lfr.json").write_text(lfr_json("2026-09-04", [mover("RUN")]))
    (d / "2026-09-07.lfr.json").write_text(lfr_json("2026-09-07", [mover("RUN")]))       # Labor Day
    (d / "2026-09-08.lfr.json").write_text(lfr_json("2026-09-08", [calm("RUN", 2.5)]))
    assert store.trading_days() == ["2026-09-04", "2026-09-08"]
    df = store.history_frame(["2026-09-08"])
    assert df.yesterday.iloc[0] == "mover, strong close"                                  # Friday is the session before Tuesday
    assert store.status()["closed_days"] == ["2026-09-07"]


def test_an_unscheduled_closure_is_caught_from_the_data_and_rechecked(home):
    d = home / "days"
    d.mkdir(parents=True)
    (d / "2026-11-04.lfr.json").write_text(lfr_json("2026-11-04", [calm("AAA", 2.0), calm("BBB", 3.0)]))
    repeat = [calm("AAA", 2.2), calm("BBB", 3.3)]
    (d / "2026-11-06.lfr.json").write_text(lfr_json("2026-11-06", repeat))
    assert store.trading_days() == ["2026-11-04", "2026-11-06"]                           # nothing to compare Friday with yet
    (d / "2026-11-05.lfr.json").write_text(lfr_json("2026-11-05", repeat))                # Thursday arrives later: Friday repeats it
    assert store.trading_days() == ["2026-11-04", "2026-11-05"]


def test_a_day_judged_closed_is_judged_again_when_its_file_is_replaced(home):
    d = home / "days"
    d.mkdir(parents=True)
    repeat = [calm("AAA", 2.2), calm("BBB", 3.3)]
    (d / "2026-11-05.lfr.json").write_text(lfr_json("2026-11-05", repeat))
    (d / "2026-11-06.lfr.json").write_text(lfr_json("2026-11-06", repeat))                # a bad copy of Thursday
    assert store.trading_days() == ["2026-11-05"]
    (d / "2026-11-06.lfr.json").write_text(lfr_json("2026-11-06", [calm("AAA", 2.3), calm("BBB", 3.4)]))    # the right file, same name
    assert store.trading_days() == ["2026-11-05", "2026-11-06"]
    (d / "2026-11-05.lfr.json").write_text(lfr_json("2026-11-05", [calm("AAA", 2.3), calm("BBB", 3.4)]))    # now Thursday changes under it
    assert store.trading_days() == ["2026-11-05"]
    (d / "2026-11-06.market.csv").write_text(export_csv([calm("AAA", 2.5), calm("BBB", 3.6)]))             # a market file takes over
    assert store.trading_days() == ["2026-11-05", "2026-11-06"]


def test_previous_close_follows_the_calendar_not_the_saved_days(home):
    seed(home)
    assert store.previous_close("2026-11-04") == pd.Timestamp("2026-11-03 16:00")          # 11-03 is not saved
    assert store.previous_close("2026-11-09") == pd.Timestamp("2026-11-06 16:00")
    assert store.previous_close("2026-09-08") == pd.Timestamp("2026-09-04 16:00")
    assert store.previous_close("2026-11-30") == pd.Timestamp("2026-11-27 13:00")          # an early close


def test_history_fields_through_the_store(home):
    seed(home)
    last = store.history_frame(["2026-11-09"]).set_index("ticker")
    assert last.yesterday.to_dict() == {"RUN": "mover, strong close", "WEAK": "mover, weak close", "CALM": "traded, not a mover",
                                        "NEW": "quiet (under 50k shares)", "BIG": "not covered"}
    assert last.streak["RUN"] == 2 and last.streak["WEAK"] == 1 and last.catalyst["NEW"] == "earnings"


def test_history_after_a_gap_in_the_saved_days_is_unknown_not_guessed(home):
    seed(home)
    (home / "days" / "2026-11-11.market.csv").write_text(export_csv([row("RUN", 2.5, 2.5, 3.2, 2.45, 3.1, 3_000_000)]))   # 11-10 is not saved
    df = store.history_frame(["2026-11-11"])
    assert df.yesterday.iloc[0] == "no history" and df.streak.iloc[0] == 3                 # three mover days in the ten sessions before


def test_earnings_calendar_marks_saved_days_and_follows_a_moved_date(home):
    seed(home)
    assert store.load_day("2026-11-06").set_index("ticker").catalyst["CALM"] == "none"
    cal = pd.DataFrame([{"date": "2026-11-05 16:30", "ticker": "CALM"}, {"date": "2026-11-06 16:30", "ticker": "ZED"}])
    assert store.save_earnings(cal, "2026-11-05", "2026-11-06") == 2
    day = store.load_day("2026-11-06").set_index("ticker")                                # the cache notices the new calendar
    assert day.catalyst["CALM"] == "earnings" and day.catalyst["ZED"] == "none"           # ZED reported after that close
    moved = pd.DataFrame([{"date": "2026-11-06 16:30", "ticker": "ZED"}])                 # CALM's report left this span
    assert store.save_earnings(moved, "2026-11-05", "2026-11-06") == 1
    assert set(store.load_earnings().ticker) == {"ZED"}
    assert store.load_day("2026-11-06").set_index("ticker").catalyst["CALM"] == "none"    # and the label follows


def test_earnings_calendar_keeps_odd_tickers_and_odd_dates(home):
    seed(home)
    cal = pd.DataFrame([{"date": "2026-11-05 16:30", "ticker": "NA", "eps_est": 0.5}, {"date": "2026-11-06", "ticker": "NULL", "eps_est": None},
                        {"date": "11/06/2026 8:30:00 AM", "ticker": "ZED", "eps_est": 1.25}])
    assert store.save_earnings(cal, "2026-11-05", "2026-11-06") == 3
    back = store.load_earnings()
    assert set(back.ticker) == {"NA", "NULL", "ZED"} and back.when.notna().all()          # "NA" is a ticker, not a blank
    assert "NA" in store.reporters(store.previous_close("2026-11-06"), "2026-11-06")
    assert "ZED" in store.reporters(store.previous_close("2026-11-06"), "2026-11-06")     # written the other way round, still read
    s = store.status()
    assert (s["earnings_rows"], s["earnings_from"], s["earnings_to"]) == (3, "2026-11-05", "2026-11-06")


def test_something_that_is_not_a_calendar_never_replaces_one(home):
    seed(home)
    cal = pd.DataFrame([{"date": "2026-11-05 16:30", "ticker": "CALM"}])
    store.save_earnings(cal, "2026-11-05", "2026-11-06")
    saved = store.earnings_path().read_text()
    for bad in (pd.DataFrame({"error": []}), pd.DataFrame({"error": ["rate limit"]}), pd.DataFrame([{"date": "", "ticker": "ZED"}]),
                pd.DataFrame([{"date": "2026-11-06 16:30", "ticker": ""}]), pd.DataFrame([{"date": "tomorrow-ish", "ticker": "ZED"}])):
        with pytest.raises(ValueError, match="left alone"):
            store.save_earnings(bad, "2026-11-05", "2026-11-06")
        assert store.earnings_path().read_text() == saved
    none_that_week = pd.DataFrame(columns=["date", "ticker", "company"])         # a real calendar with nothing on it
    assert store.save_earnings(none_that_week, "2026-11-05", "2026-11-06") == 0

    class Answers:
        def __init__(self, body):
            self.body = body

        def export(self, path, params=None):
            return self.body

    with pytest.raises(FinvizError, match="without its Date and Ticker columns"):
        endpoints.earnings(Answers("Error\n"), "2026-11-05", "2026-11-06")
    for cut_short in ('Date,Ticker,Company\n2026-11-06 08:30,AAA,"unfinished\n',            # a quoted field that never closes
                      "Date,Ticker,Company\n2026-11-06 08:30,AAA,Alpha\n2026-11-06 16:30,BB\n",   # a row missing a column
                      "Date,Ticker,Company\n2026-11-06 08:30,AAA,Alpha,extra,fields\n",
                      "Date,Ticker,Com",                                                       # stopped in the header: no line end
                      "Date,Ticker,Company\n2026-11-06 08:30,AAA,Alpha\n2026-11-06 16:30,BBB,Bet"):   # stopped inside the last field
        with pytest.raises(FinvizError, match="did not arrive whole"):
            endpoints.earnings(Answers(cut_short), "2026-11-05", "2026-11-06")
    assert store.earnings_path().read_text().count("\n") == 1                     # still just the header: nothing got in
    assert endpoints.earnings(Answers("Date,Ticker,Company\n"), "2026-11-05", "2026-11-06").empty
    zoned = pd.DataFrame([{"date": "2026-11-06T21:30:00+00:00", "ticker": "ZED"}])                 # 16:30 Eastern, written in UTC
    store.save_earnings(zoned, "2026-11-05", "2026-11-07")
    assert store.load_earnings().when.iloc[0] == pd.Timestamp("2026-11-06 16:30")


def test_a_calendar_saved_in_pieces_across_a_clock_change_still_reads(home):
    """Dates with an offset, saved range by range: -04:00 before the clocks go back, -05:00 after.
    What is kept on disk is plain Eastern time, so the pieces never disagree about the zone."""
    seed(home)
    october = pd.DataFrame([{"date": "2026-10-30T08:30:00-04:00", "ticker": "OCT"}, {"date": "2026-10-30 16:30", "ticker": "PLAIN"}])
    november = pd.DataFrame([{"date": "2026-11-06T08:30:00-05:00", "ticker": "CALM"}, {"date": "2026-11-06T21:30:00Z", "ticker": "ZED"}])
    assert store.save_earnings(october, "2026-10-26", "2026-10-30") == 2
    assert store.save_earnings(november, "2026-11-02", "2026-11-06") == 4
    cal = store.load_earnings().set_index("ticker")
    assert cal.when.to_dict() == {"OCT": pd.Timestamp("2026-10-30 08:30"), "PLAIN": pd.Timestamp("2026-10-30 16:30"),
                                  "CALM": pd.Timestamp("2026-11-06 08:30"), "ZED": pd.Timestamp("2026-11-06 16:30")}
    assert set(cal["date"]) == {"2026-10-30 08:30", "2026-10-30 16:30", "2026-11-06 08:30", "2026-11-06 16:30"}
    day = store.load_day("2026-11-06").set_index("ticker")
    assert day.catalyst["CALM"] == "earnings" and day.catalyst["ZED"] == "none"           # 16:30 is after that close
    assert store.status()["earnings_from"] == "2026-10-30"
    assert store.save_earnings(november.iloc[:1], "2026-11-02", "2026-11-06") == 3        # and it can be saved over again


def test_a_replaced_day_file_is_read_again_at_once(home):
    seed(home)
    assert set(store.load_day("2026-11-09").ticker) == {"RUN", "WEAK", "CALM", "NEW", "BIG"}
    (home / "days" / "2026-11-09.market.csv").write_text(export_csv([mover("ONLY")]))      # no waiting for the clock to tick
    assert set(store.load_day("2026-11-09").ticker) == {"ONLY"}
    assert len(list((home / "cache").glob("2026-11-09.*.pkl"))) == 1


def test_a_swapped_in_file_of_the_same_size_and_time_is_still_noticed(home):
    """A restore that keeps the old size and modification time: only the change time gives it away."""
    seed(home)
    target = home / "days" / "2026-11-09.market.csv"
    text = export_csv([calm("AAA", 2.0), calm("BBB", 3.0)])
    target.write_text(text)
    was = target.stat()
    assert set(store.load_day("2026-11-09").company) == {"Example Inc"} and store.trading_days()[-1] == "2026-11-09"
    marks_before = store._split_state()["marks"]
    time.sleep(0.05)
    fresh = home / "restored.tmp"
    fresh.write_text(text.replace("Example Inc", "Changed Inc"))                          # the same length, other contents
    os.utime(fresh, ns=(was.st_atime_ns, was.st_mtime_ns))
    os.replace(fresh, target)
    now = target.stat()
    assert (now.st_size, now.st_mtime_ns) == (was.st_size, was.st_mtime_ns)               # size and time say nothing changed
    assert set(store.load_day("2026-11-09").company) == {"Changed Inc"}
    assert store._split_state()["marks"] != marks_before                                  # the split index saw it too


def test_a_store_that_cannot_be_written_to_can_still_be_read(home, monkeypatch):
    seed(home)

    def refuse(*a, **k):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(store, "write_text_atomic", refuse)
    monkeypatch.setattr(store, "write_bytes_atomic", refuse)
    assert store.trading_days() == ["2026-11-05", "2026-11-06", "2026-11-09"]
    last = store.history_frame(["2026-11-09"]).set_index("ticker")
    assert last.yesterday["RUN"] == "mover, strong close" and last.streak["RUN"] == 2
    assert store.status()["trading_days"] == 3 and store.split_index()[0].empty
    assert not (home / "cache").exists() and not (home / "days" / "closed_days.txt").exists()      # nothing was kept, nothing failed
    with pytest.raises(PermissionError):
        store.save_snapshot(export_csv([mover("X")]), at(2026, 11, 10, 10, 0), "2026-11-10", False, False)   # writing still says so


def test_a_folder_that_takes_no_files_with_the_real_lock(home, unwritable, monkeypatch):
    """The same, without stand-ins for the store's own functions: the folder refuses new files
    and writes, the way a read-only one does, and everything else is the real code."""
    seed(home)
    unwritable(home)
    assert store.trading_days() == ["2026-11-05", "2026-11-06", "2026-11-09"]
    assert store.history_frame(["2026-11-09"]).set_index("ticker").yesterday["RUN"] == "mover, strong close"
    seeded = {"days", "2026-11-05.lfr.json", "2026-11-06.lfr.json", "2026-11-09.market.csv"}
    assert {p.name for p in home.rglob("*")} == seeded            # no cache folder, no memo, no lock file: nothing new at all
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    pull = export_csv([mover("RUN"), calm("CALM", 2.06), calm("ZED", 3.1), calm("AAA", 4.0), calm("BBB", 5.0)])
    with pytest.raises(store.StoreBusy, match="cannot be written to"):
        store.save_snapshot(pull, at(2026, 11, 10, 16, 30), "2026-11-10", True, True)
    with pytest.raises(store.StoreBusy, match="cannot be written to"):
        store.save_earnings(pd.DataFrame([{"date": "2026-11-05 16:30", "ticker": "CALM"}]), "2026-11-05", "2026-11-06")
    assert not (home / "snapshots").exists() and not store.earnings_path().exists()       # nothing was half-written


def test_damaged_bytes_in_the_memo_are_not_a_crash(home):
    seed(home)
    (home / "days" / "closed_days.txt").write_bytes(b"\xff\xfe\x81 not a memo \x00\n2026-11-05 open\n")
    assert store.trading_days() == ["2026-11-05", "2026-11-06", "2026-11-09"]


def test_a_file_saved_on_a_holiday_is_not_the_day_a_pull_is_compared_with(home, monkeypatch):
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    d = home / "days"
    d.mkdir(parents=True)
    (d / "2026-09-04.lfr.json").write_text(lfr_json("2026-09-04", [calm("AAA", 2.00), calm("BBB", 3.00)]))       # Friday
    rolled = [row("AAA", 2.02, 2.02, 2.02, 2.02, 2.02, 0), row("BBB", 3.03, 3.03, 3.03, 3.03, 3.03, 0)]
    (d / "2026-09-07.lfr.json").write_text(lfr_json("2026-09-07", rolled))                                       # Labor Day: Friday's closes
    tuesday = export_csv([calm("AAA", 2.02), calm("BBB", 3.03), calm("CCC", 4.00)])                             # previous closes: Friday's
    out = store.save_snapshot(tuesday, at(2026, 9, 8, 16, 30), "2026-09-08", True, True)
    assert out["refused"] is None and out["day_file"]             # compared with Friday, the session before, not with the holiday file
    assert store.trading_days() == ["2026-09-04", "2026-09-08"]


def test_a_pull_taken_after_the_close_must_carry_the_day_whenever_it_was_taken(home, monkeypatch):
    seed(home)
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    rows = [mover("RUN"), calm("CALM", 2.06), calm("ZED", 3.1), calm("AAA", 4.0), calm("BBB", 5.0)]
    blank = export_csv([dict(r, **{"Open": "", "High": "", "Low": "", "Change from Open": "", "Gap": ""}) for r in rows])
    assert store.save_snapshot(export_csv(rows), at(2026, 11, 13, 16, 30), "2026-11-13", True, True)["day_file"]     # Friday evening
    for when in (at(2026, 11, 14, 10, 0), at(2026, 11, 16, 3, 58)):         # Saturday; Monday just before premarket: not "the day's record"
        out = store.save_snapshot(blank, when, "2026-11-13", True, False)
        assert not out["usable"] and "have an open" in out["refused"] and out["snapshot"].endswith("_rejected.csv"), when
    assert store.latest_snapshot("2026-11-13")[0].name == "20261113T163000000000_market.csv"                              # the evening pull still serves
    monday = [mover("RUN"), calm("CALM", 2.16), calm("ZED", 3.2), calm("AAA", 4.1), calm("BBB", 5.1)]              # Friday's closes behind them
    premarket = export_csv([dict(r, **{"Open": "", "High": "", "Low": "", "Change from Open": "", "Gap": ""}) for r in monday])
    morning = store.save_snapshot(premarket, at(2026, 11, 16, 8, 0), "2026-11-16", True, False)                     # before Monday's open that is normal
    assert morning["usable"] and morning["refused"] is None


def test_report_times_with_any_kind_of_zone_are_read(home):
    seed(home)
    odd = pd.DataFrame({"date": ["2026-11-06T21:30:00+00", "2026-11-06 08:30:00-05", "2026-11-06 13:30 UTC", "2026-11-06T13:30:00.000Z",
                                 "2026-11-06", "11/06/2026 8:30:00 AM"],
                        "ticker": ["HOURONLY", "MINUS5", "NAMED", "MILLIS", "DATEONLY", "USSTYLE"]}, index=[7, 7, 7, 3, 3, 0])     # an index with repeats
    assert store.save_earnings(odd, "2026-11-05", "2026-11-07") == 6
    when = store.load_earnings().set_index("ticker").when.to_dict()
    assert when == {"HOURONLY": pd.Timestamp("2026-11-06 16:30"), "MINUS5": pd.Timestamp("2026-11-06 08:30"), "NAMED": pd.Timestamp("2026-11-06 08:30"),
                    "MILLIS": pd.Timestamp("2026-11-06 08:30"), "DATEONLY": pd.Timestamp("2026-11-06 00:00"), "USSTYLE": pd.Timestamp("2026-11-06 08:30")}
    # Zones written without colons slip past the pattern that sorts zoned from plain. They must
    # still be read, or at worst refused: never a crash, and never a saved row that cannot be read back.
    compact = pd.DataFrame({"date": ["2026-11-06T133000Z", "2026-11-06T083000-0500", "2026-11-06 08:30", "2026-11-06T13Z"],
                            "ticker": ["BASICZ", "BASICOFF", "PLAIN", "HOURZ"]})
    assert store.save_earnings(compact, "2026-11-05", "2026-11-07") == 4
    back = store.load_earnings().set_index("ticker")
    assert back.when.to_dict() == {"BASICZ": pd.Timestamp("2026-11-06 08:30"), "BASICOFF": pd.Timestamp("2026-11-06 08:30"),
                                   "PLAIN": pd.Timestamp("2026-11-06 08:30"), "HOURZ": pd.Timestamp("2026-11-06 08:00")}
    assert set(back["date"]) == {"2026-11-06 08:30", "2026-11-06 08:00"}          # on disk: plain Eastern, whatever came in
    with pytest.raises(ValueError, match="left alone"):
        store.save_earnings(pd.DataFrame({"date": ["garbage"], "ticker": ["X"]}), "2026-11-05", "2026-11-07")
    assert len(store.load_earnings()) == 4
    gaps = pd.DataFrame({"date": ["2026-11-06 16:30", None, float("nan")], "ticker": ["ZED", "NODATE", "NANDATE"]})      # a calendar handed in with holes
    assert store._parse_when(gaps["date"]).isna().tolist() == [False, True, True]
    assert store.reporters(store.previous_close("2026-11-09"), "2026-11-09", gaps) == {"ZED"}                            # a missing date matches no window


def test_one_unchanged_name_does_not_make_a_day_a_repeat(home, monkeypatch):
    monkeypatch.setattr(store, "MIN_OVERLAP", 50)                     # as shipped
    d = home / "days"
    d.mkdir(parents=True)
    (d / "2026-11-05.lfr.json").write_text(lfr_json("2026-11-05", [calm("STILL", 2.0)]))
    (d / "2026-11-06.lfr.json").write_text(lfr_json("2026-11-06", [calm("STILL", 2.0), calm("MOVED", 3.0)]))
    assert store.trading_days() == ["2026-11-05", "2026-11-06"]       # one shared ticker is not evidence about a session
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    pull = export_csv([calm("STILL", 2.0), calm("OTHER", 5.0), calm("THIRD", 6.0)])
    out = store.save_snapshot(pull, at(2026, 11, 9, 16, 30), "2026-11-09", True, True)
    assert out["refused"] is None and out["day_file"]


def test_snapshot_naming_filing_and_refusals(home, monkeypatch):
    seed(home)
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    good = export_csv([mover("RUN"), calm("CALM", 2.06), calm("ZED", 3.1), calm("AAA", 4.0), calm("BBB", 5.0)])
    out = store.save_snapshot(good, at(2026, 11, 10, 16, 30, 5), "2026-11-10", True, True)
    assert out["snapshot"].endswith("20261110T163005000000_market.csv") and out["day_file"].endswith("2026-11-10.market.csv")
    assert out["usable"] and out["rows"] == 5 and out["refused"] is None
    again = store.save_snapshot(good, at(2026, 11, 10, 16, 30, 5), "2026-11-10", True, True)
    assert again["snapshot"].endswith("20261110T163005000000-2_market.csv")                          # the same second keeps both
    store.save_snapshot(export_csv([mover("X")]), at(2026, 11, 10, 16, 40), "2026-11-10", False, False)
    path, pulled = store.latest_snapshot("2026-11-10")
    assert path.name == "20261110T163005000000-2_market.csv" and pulled == at(2026, 11, 10, 16, 30, 5)    # the filtered pull is never reused
    thin = store.save_snapshot(export_csv([mover("RUN")]), at(2026, 11, 10, 17, 0), "2026-11-10", True, True)
    assert thin["day_file"] is None and "too few" in thin["refused"] and not thin["usable"]
    assert thin["snapshot"].endswith("20261110T170000000000_rejected.csv")
    assert store.latest_snapshot("2026-11-10")[0].name == "20261110T163005000000-2_market.csv"       # a rejected pull is never the latest
    assert (home / "days" / "2026-11-10.market.csv").read_text() == good                        # the good record survives
    repeat = store.save_snapshot(good, at(2026, 11, 11, 16, 30), "2026-11-11", True, True)
    assert repeat["day_file"] is None and "same previous closes" in repeat["refused"] and repeat["snapshot"].endswith("_rejected.csv")
    assert store.latest_snapshot("2026-11-11") is None


def test_an_older_snapshot_name_is_read_but_only_after_it_is_checked(home, monkeypatch):
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    folder = home / "snapshots" / "2026-11-10"
    folder.mkdir(parents=True)
    sound = export_csv([mover("RUN"), calm("CALM")])
    (folder / "20261110T1837_market.csv").write_text(sound)
    (folder / "20261110T183659000000_market.csv").write_text(sound)
    path, pulled = store.latest_snapshot("2026-11-10")
    assert path.name == "20261110T1837_market.csv" and pulled == at(2026, 11, 10, 18, 37)        # ordered by time, not by spelling
    # Names without seconds were written before the checks existed. A thin one left from then
    # must not be shown just because its name says "market".
    (folder / "20261110T1900_market.csv").write_text(export_csv([mover("ALONE")]))
    path, pulled = store.latest_snapshot("2026-11-10")
    assert path.name == "20261110T1837_market.csv"
    assert (folder / "20261110T1900_rejected.csv").exists() and not (folder / "20261110T1900_market.csv").exists()
    (folder / "20261110T1900_market.csv").write_text(export_csv([mover("AGAIN")]))       # the same name turns up again (a restore)
    assert store.latest_snapshot("2026-11-10")[0].name == "20261110T1837_market.csv"
    assert not (folder / "20261110T1900_market.csv").exists()                            # set aside again, over the earlier reject
    assert "AGAIN" in (folder / "20261110T1900_rejected.csv").read_text()


def test_what_is_not_the_whole_market_is_never_filed_as_a_day(home, monkeypatch):
    seed(home)
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 3)
    rows = [mover("RUN"), calm("CALM", 2.06), calm("ZED", 3.1), calm("FOUR", 4.0), calm("FIVE", 5.0)]
    good = export_csv(rows)
    assert store.save_snapshot(good, at(2026, 11, 10, 16, 30), "2026-11-10", True, True)["day_file"]
    page = "<!-- error -->\n<html>\n" + "<p>sign in</p>\n" * 3000 + "</html>\n"                   # plenty of lines, no table
    no_cols = "Ticker,Price\n" + "".join(f"T{i},1.00\n" for i in range(50))
    no_float = export_csv([{k: v for k, v in r.items() if k != "Shares Float"} for r in rows])     # the whole column gone
    twins = export_csv([mover("RUN"), mover("RUN"), mover("RUN"), calm("CALM", 2.06), calm("FIVE", 5.0)])
    unpriced = export_csv([calm(f"T{i}", **{"Price": ""}) for i in range(5)])
    wrapped = export_csv([calm("ONE", **{"News Title": "line\n" * 500})])                          # one row, hundreds of lines
    cut_mid_row = good[:good.rindex("FIVE") + 12]                                                  # the answer stopped arriving
    cut_at_a_row_end = good.rstrip("\n")
    too_wide = good + "SIX," + ",".join(["1"] * 40) + "\n"                                         # more fields than the header
    unopened = export_csv([calm(f"T{i}", **{"Open": "", "High": "", "Low": ""}) for i in range(5)])
    no_relvol = export_csv([dict(r, **{"Relative Volume": ""}) for r in rows])                     # nothing could be a mover
    width = len(good.splitlines()[0].split(","))
    cut_in_quotes = good + "SIX," + ",".join(["1"] * (width - 2)) + ',"a headline that\n'          # stopped inside a quoted last field
    cases = [(page, "columns are missing"), (no_cols, "columns are missing"), (no_float, "columns are missing: Shares Float"),
             (twins, "tickers repeat"), (unpriced, "have a price"), (wrapped, "too few"), (cut_mid_row, "cut off"),
             (cut_at_a_row_end, "cut off"), (too_wide, "do not have the header's columns"), (unopened, "have an open"),
             (no_relvol, "have a relative volume"), (cut_in_quotes, "cannot be read as a table")]
    for n, (text, why) in enumerate(cases):
        out = store.save_snapshot(text, at(2026, 11, 10, 17, n), "2026-11-10", True, True)
        assert out["day_file"] is None and why in out["refused"] and out["snapshot"].endswith("_rejected.csv"), (why, out["refused"])
    morning = store.save_snapshot(unopened, at(2026, 11, 11, 8, 0), "2026-11-11", True, False)     # before the open that is how it looks
    assert morning["usable"] and morning["refused"] is None
    small = export_csv(rows[:3])
    out = store.save_snapshot(small, at(2026, 11, 11, 16, 30), "2026-11-11", True, True)           # 3 rows against 5 the day before
    assert out["day_file"] is None and "only 3 rows against 5 on 2026-11-10" in out["refused"]
    out = store.save_snapshot(small, at(2026, 11, 10, 18, 0), "2026-11-10", True, True)            # ...and against the same day's record
    assert out["day_file"] is None and "only 3 rows against 5 on 2026-11-10" in out["refused"]
    assert (home / "days" / "2026-11-10.market.csv").read_text() == good                           # nothing touched the record
    assert store.latest_snapshot("2026-11-10")[0].name == "20261110T163000000000_market.csv"


def test_an_older_pull_that_finishes_late_does_not_replace_the_newer_record(home, monkeypatch):
    seed(home)
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    earlier = export_csv([mover("RUN"), calm("CALM", 2.06), calm("ZED", 3.1), calm("AAA", 4.0), calm("OLD", 5.0)])
    later = export_csv([mover("RUN"), calm("CALM", 2.06), calm("ZED", 3.1), calm("AAA", 4.0), calm("NEW", 5.0)])
    first = store.save_snapshot(later, at(2026, 11, 10, 16, 6), "2026-11-10", True, True)          # asked second, answered first
    second = store.save_snapshot(earlier, at(2026, 11, 10, 16, 5), "2026-11-10", True, True)       # asked first, held up
    assert first["day_file"] and second["day_file"] is None and second["refused"] is None and second["usable"]
    assert second["superseded_by"] == "20261110T160600000000_market.csv"
    assert (home / "days" / "2026-11-10.market.csv").read_text() == later                          # the record did not go back in time
    assert store.latest_snapshot("2026-11-10")[0].name == "20261110T160600000000_market.csv"
    assert (home / "snapshots" / "2026-11-10" / "20261110T160500000000_market.csv").exists()             # both pulls are kept
    third = store.save_snapshot(earlier, at(2026, 11, 10, 16, 7), "2026-11-10", True, True)        # a later one does replace it
    assert third["day_file"] and (home / "days" / "2026-11-10.market.csv").read_text() == earlier


def test_two_pulls_in_one_second_are_still_filed_in_the_order_they_were_taken(home, monkeypatch):
    seed(home)
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    a = export_csv([mover("RUN"), calm("CALM", 2.06), calm("ZED", 3.1), calm("AAA", 4.0), calm("FIRST", 5.0)])
    b = export_csv([mover("RUN"), calm("CALM", 2.06), calm("ZED", 3.1), calm("AAA", 4.0), calm("SECOND", 5.0)])
    taken_a = dt.datetime(2026, 11, 10, 16, 5, 0, 100_000, tzinfo=ET)
    taken_b = dt.datetime(2026, 11, 10, 16, 5, 0, 800_000, tzinfo=ET)
    saved_b = store.save_snapshot(b, taken_b, "2026-11-10", True, True)           # taken later, saved first
    saved_a = store.save_snapshot(a, taken_a, "2026-11-10", True, True)
    assert saved_b["day_file"] and saved_a["day_file"] is None and saved_a["superseded_by"] == "20261110T160500800000_market.csv"
    assert (home / "days" / "2026-11-10.market.csv").read_text() == b
    assert store.latest_snapshot("2026-11-10") == (home / "snapshots" / "2026-11-10" / "20261110T160500800000_market.csv", taken_b)
    assert (home / "snapshots" / "2026-11-10" / "20261110T160500100000_market.csv").exists()


def test_snapshot_names_written_to_the_second_are_still_read_in_order(home, monkeypatch):
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    folder = home / "snapshots" / "2026-11-10"
    folder.mkdir(parents=True)
    for name in ("20261110T163000_market.csv", "20261110T163000500000_market.csv", "20261110T163001_market.csv"):
        (folder / name).write_text(export_csv([mover("RUN"), calm("CALM")]))
    path, when = store.latest_snapshot("2026-11-10")
    assert path.name == "20261110T163001_market.csv" and when == at(2026, 11, 10, 16, 30, 1)
    (folder / "20261110T163001_market.csv").unlink()
    assert store.latest_snapshot("2026-11-10")[0].name == "20261110T163000500000_market.csv"     # half a second after 16:30:00


def test_a_day_judged_closed_is_no_yardstick_for_coverage(home, monkeypatch):
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    d = home / "days"
    d.mkdir(parents=True)
    whole = [calm(f"T{i}", 2.0 + i) for i in range(10)]
    (d / "2026-11-05.market.csv").write_text(export_csv(whole))
    (d / "2026-11-06.market.csv").write_text(export_csv(whole[:5]))               # a partial repeat of Thursday: judged closed
    assert store.trading_days() == ["2026-11-05"]
    half = export_csv([calm(f"T{i}", 2.5 + i) for i in range(5)])                 # Monday: 5 rows, as many as the bad file
    out = store.save_snapshot(half, at(2026, 11, 9, 16, 30), "2026-11-09", True, True)
    assert out["day_file"] is None and "only 5 rows against 10 on 2026-11-05" in out["refused"]


def test_an_older_snapshot_is_judged_by_every_rule_a_new_pull_is(home, monkeypatch):
    seed(home)
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    rows = [mover("RUN"), calm("CALM", 2.06), calm("ZED", 3.1), calm("AAA", 4.0), calm("BBB", 5.0)]
    assert store.save_snapshot(export_csv(rows), at(2026, 11, 10, 16, 30), "2026-11-10", True, True)["day_file"]
    folder = home / "snapshots" / "2026-11-10"
    (folder / "20261110T1700_market.csv").write_text(export_csv(rows[:3]))                         # 3 rows against the 5 filed
    blank = [dict(r, **{"Open": "", "High": "", "Low": ""}) for r in rows]
    (folder / "20261110T1800_market.csv").write_text(export_csv(blank))                            # an evening pull with no day in it
    (folder / "20261110T1900_market.csv").write_text((home / "days" / "2026-11-09.market.csv").read_text())    # yesterday again
    path, _ = store.latest_snapshot("2026-11-10")
    assert path.name == "20261110T163000000000_market.csv"
    assert sorted(p.name for p in folder.glob("*_rejected.csv")) == ["20261110T1700_rejected.csv", "20261110T1800_rejected.csv",
                                                                    "20261110T1900_rejected.csv"]


def test_the_first_day_ever_filed_cannot_be_overwritten_by_a_part_of_it(home, monkeypatch):
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 3)                     # an empty store: no earlier day to compare with
    whole = export_csv([calm(f"T{i}", 2.0 + i) for i in range(10)])
    part = export_csv([calm(f"T{i}", 2.0 + i) for i in range(5)])
    assert store.save_snapshot(whole, at(2026, 11, 10, 16, 30), "2026-11-10", True, True)["day_file"]
    out = store.save_snapshot(part, at(2026, 11, 10, 17, 0), "2026-11-10", True, True)
    assert out["day_file"] is None and "only 5 rows against 10 on 2026-11-10" in out["refused"]
    assert (home / "days" / "2026-11-10.market.csv").read_text() == whole


def test_import_lfr_takes_only_end_of_day_files_named_for_their_date(home, tmp_path):
    src = tmp_path / "eod"
    src.mkdir()
    (src / "2026-11-05.json").write_text(lfr_json("2026-11-05", [mover("RUN")]))
    assert store.import_lfr(src) == {"added": 1, "unchanged": 0, "rejected": []}
    assert store.import_lfr(src) == {"added": 0, "unchanged": 1, "rejected": []}
    rows = json.loads(lfr_json("2026-11-06", [calm("AAA")]))["rows"]                                          # one complete stock row
    fund = [dict(rows[0], Industry="Exchange Traded Fund")]

    def put(name, **payload):
        (src / name).write_text(json.dumps(payload))

    put("2026-11-06.json", date="2026-11-06", et=600, rows=rows)                                              # 10:00
    put("2026-11-09.json", date="2026-11-06", et=970, rows=rows)                                              # says another day
    put("2026-11-10.json", date="2026-11-10", rows=rows)                                                      # no capture time
    (src / "2026-11-11.json").write_text(json.dumps(rows))                                                    # a bare list
    (src / "2026-11-12.json").write_text("{not json")
    (src / "2026-11-13.json").write_text('{"date": "2026-11-13", "et": NaN, "rows": [{"Ticker": "AAA"}]}')    # json.loads takes NaN
    (src / "2026-11-16.json").write_text('{"date": "2026-11-16", "et": -Infinity, "rows": [{"Ticker": "AAA"}]}')
    put("2026-11-17.json", date="2026-11-17", et=5000, rows=rows)                                             # not a time of day
    put("2026-11-18.json", date="2026-11-18", et=970, rows=[None])
    put("2026-11-19.json", date="2026-11-19", et=970, rows=[{"Ticker": "AAA", "Price": 2.0}])                 # a row, but not a stock row
    put("2026-11-20.json", date="2026-11-20", et=970, rows=fund)
    put("2026-11-23.json", date="2026-11-23", et=970, rows=[dict(rows[0], Price=None)])
    put("2026-11-24.json", date="2026-11-24", et=970, rows=[dict(rows[0], Ticker="nan")])                     # what a missing ticker turns into
    (src / "2026-11-25.json").write_text(json.dumps({"date": "2026-11-25", "et": 970, "rows": [dict(rows[0], Ticker=None)]}).replace("null", "NaN"))
    put("2026-11-27.json", date="2026-11-27", et=779, rows=rows)                                              # 12:59 on an early close
    (src / "2026-11-05_0935.json").write_text(lfr_json("2026-11-05", [calm("MORNING")]))                      # would overwrite 11-05
    (src / "2026_summary.json").write_text("{}")
    got = store.import_lfr(src)
    assert got["added"] == 0 and got["unchanged"] == 1
    assert dict(got["rejected"]) == {
        "2026-11-06.json": "captured at 10:00 ET, before the close", "2026-11-09.json": "it says it is for '2026-11-06'",
        "2026-11-10.json": "it does not say when it was captured", "2026-11-11.json": "no rows",
        "2026-11-12.json": "not readable as JSON", "2026-11-13.json": "it does not say when it was captured",
        "2026-11-16.json": "it does not say when it was captured", "2026-11-17.json": "it does not say when it was captured",
        "2026-11-18.json": "1 of 1 rows are not complete stock rows", "2026-11-19.json": "1 of 1 rows are not complete stock rows",
        "2026-11-20.json": "it holds no stocks, only funds", "2026-11-23.json": "1 of 1 rows have no ticker or no price",
        "2026-11-24.json": "1 of 1 rows have no ticker or no price", "2026-11-25.json": "1 of 1 rows have no ticker or no price",
        "2026-11-27.json": "captured at 12:59 ET, before the close",
        "2026-11-05_0935.json": "the name is not <date>.json", "2026_summary.json": "the name is not <date>.json"}
    assert store.trading_days() == ["2026-11-05"] and set(store.load_day("2026-11-05").ticker) == {"RUN"}
    put("2026-11-27.json", date="2026-11-27", et=785, rows=rows)                                              # 13:05: after that day's close
    assert store.import_lfr(src)["added"] == 1 and "2026-11-27" in store.trading_days()


def test_import_lfr_notices_a_correction_of_the_same_size(home, tmp_path):
    src = tmp_path / "eod"
    src.mkdir()
    first = lfr_json("2026-11-05", [mover("AAA")])
    second = first.replace('"Price": 2.5', '"Price": 2.6')                                 # same length, different number
    assert len(first) == len(second) and first != second
    (src / "2026-11-05.json").write_text(first)
    assert store.import_lfr(src)["added"] == 1
    (src / "2026-11-05.json").write_text(second)
    assert store.import_lfr(src)["added"] == 1
    assert json.loads((home / "days" / "2026-11-05.lfr.json").read_text())["rows"][0]["Price"] == 2.6


# ---------------------------------------------------------------- the split clock

def split_store(home):
    """Sixteen sessions. SPLT splits on the second. GAPD is last seen on the first and comes
    back on the fifteenth with its float cut to a twentieth: a split somewhere in that gap."""
    days = session_dates("2026-10-19", 16)
    d = home / "days"
    d.mkdir(parents=True, exist_ok=True)

    def step(t, start, i, f="10.00"):
        """A name drifting up a cent a day, so no day repeats the one before."""
        prev, price = round(start + 0.01 * (i - 1), 2), round(start + 0.01 * i, 2)
        return row(t, prev, prev, round(price * 1.01, 4), round(prev * 0.99, 4), price, 100_000, **{"Shares Float": f})

    for i, day in enumerate(days):
        rows = [step("NORM", 5.00, i), step("OTHER", 7.00, i), step("THIRD", 9.00, i)]
        rows.append(quiet("SPLT", 0.30) if i == 0 else step("SPLT", 3.00, i, "1.00") if i > 1 else
                    row("SPLT", 3.00, 3.00, 3.10, 2.90, 3.01, 100_000, **{"Shares Float": "1.00"}))
        if i == 0:
            rows.append(quiet("GAPD", 0.20))
        if i >= 14:
            rows.append(row("GAPD", 4.00, 4.00, 4.10, 3.90, 4.00, 100_000, **{"Shares Float": "0.50"}) if i == 14 else step("GAPD", 4.00, 1, "0.50"))
        if i in (3, 12):
            rows.append(mover("HOT"))                              # a mover 12 sessions before the last day, and one 3 before
        elif i > 12:
            rows.append(step("HOT", 2.50, i - 12))
        (d / f"{day}.lfr.json").write_text(lfr_json(day, rows))
    assert store.trading_days() == days
    return days


def test_the_split_clock_is_the_same_whatever_range_is_asked_for(home):
    days = split_store(home)
    assert len(days) > store.LEAD + 2                                 # the splits lie outside what one day's request loads
    one = store.history_frame([days[-1]]).set_index("ticker")
    wide = store.history_frame(days)
    wide = wide[wide.date == days[-1]].set_index("ticker")
    clock = {"NORM": -1.0, "OTHER": -1.0, "THIRD": -1.0, "SPLT": 14.0, "GAPD": 1.0, "HOT": -1.0}  # -1: never split
    assert one.days_since_split.fillna(-1).to_dict() == wide.days_since_split.fillna(-1).to_dict() == clock
    assert one.split_ratio["SPLT"] == pytest.approx(10.0) and one.split_ratio["GAPD"] == pytest.approx(20.0)
    assert one.streak.to_dict() == wide.streak.to_dict() and one.yesterday.to_dict() == wide.yesterday.to_dict()
    assert one.streak["HOT"] == 1                                     # the mover day 3 sessions back, not the one 12 back
    day_after_run = store.history_frame([days[13]]).set_index("ticker")
    assert day_after_run.streak["HOT"] == 2 and day_after_run.yesterday["HOT"] == "mover, strong close"     # both are within ten there
    events = store.split_index()[0].set_index("ticker")
    assert events.loc["SPLT", "exact"] and events.loc["SPLT", "date"] == days[1]
    assert not events.loc["GAPD", "exact"] and events.loc["GAPD", "date"] == days[14]


def test_the_split_index_grows_with_new_days_and_is_rebuilt_when_an_old_day_changes(home):
    days = split_store(home)
    first = store.split_index()[0]
    assert set(first.ticker) == {"SPLT", "GAPD"}
    nxt = session_dates("2026-11-10", 1)[0]
    (home / "days" / f"{nxt}.lfr.json").write_text(lfr_json(nxt, [row("NORM", 50.0, 50.0, 50.5, 49.5, 50.0, 100_000, **{"Shares Float": "1.00"})]))
    grown = store.split_index()[0]
    assert set(grown.ticker) == {"SPLT", "GAPD", "NORM"} and grown.set_index("ticker").loc["NORM", "exact"]       # 5.00 -> 50.00, the day after
    extended = store._split_state()
    (home / "cache" / f"splits.v{store.CACHE_VERSION}.pkl").unlink()
    rebuilt = store._split_state()

    def whole(frame, keys):
        """Every column of an index table, in one fixed order, as plain values."""
        return frame.sort_values(keys).reset_index(drop=True).astype(object).where(frame.sort_values(keys).reset_index(drop=True).notna(), None).values.tolist()

    for part, keys in (("events", ["ticker", "di"]), ("last", ["ticker"]), ("prev", ["ticker"])):
        assert list(extended[part].columns) == list(rebuilt[part].columns), part
        assert whole(extended[part], keys) == whole(rebuilt[part], keys), part         # extended a day at a time == built in one pass
    assert extended["marks"] == rebuilt["marks"] and set(extended["prev"].ticker) >= {"SPLT", "GAPD", "NORM"}
    assert extended["prev"].set_index("ticker").date["NORM"] == days[-1] and extended["last"].set_index("ticker").date["NORM"] == nxt
    (home / "days" / f"{days[1]}.lfr.json").write_text(lfr_json(days[1], [quiet("NORM", 5.00), quiet("SPLT", 0.30)]))  # day 2 corrected: no split yet
    changed = store.split_index()[0].set_index("ticker")
    assert changed.loc["SPLT", "date"] == days[2]                     # the split is now seen a day later


def test_a_live_pull_shows_todays_split_against_the_last_saved_row(home):
    days = split_store(home)
    today = session_dates("2026-11-10", 1)[0]
    live = [quiet("SPLT", 3.00, "1.00"), row("NORM", 50.0, 50.0, 50.5, 49.5, 50.0, 100_000, **{"Shares Float": "1.00"}),
            row("BACK", 9.0, 9.0, 9.1, 8.9, 9.0, 100_000)]
    path = home / "live.csv"
    path.write_text(export_csv(live))
    got = store.with_history(store.read_snapshot(path, today), today).set_index("ticker")
    assert got.days_since_split.fillna(-1).to_dict() == {"SPLT": 15.0, "NORM": 0.0, "BACK": -1.0}     # NORM split today; BACK is new
    assert got.split_ratio["NORM"] == pytest.approx(50.0 / 5.15) and got.yesterday["NORM"] == "traded, not a mover"
    assert "di" not in got.columns
    assert set(store.split_index()[0].ticker) == {"SPLT", "GAPD"}             # looking at a live pull adds nothing to the index


def test_a_live_pull_is_judged_on_its_own_rows_whatever_is_filed_for_that_session(home):
    """Friday is filed from an imported file, which did not list X that day. A whole-market pull
    for Friday does list it, split. The rows shown are the ones the split clock is read from."""
    d = home / "days"
    d.mkdir(parents=True)
    (d / "2026-11-05.lfr.json").write_text(lfr_json("2026-11-05", [quiet("X", 0.30), quiet("Y", 5.00)]))
    (d / "2026-11-06.lfr.json").write_text(lfr_json("2026-11-06", [quiet("Y", 5.05), quiet("Z", 9.00)]))      # no X on Friday
    live = home / "live.csv"
    live.write_text(export_csv([row("X", 3.00, 3.00, 3.10, 2.90, 3.00, 100_000, **{"Shares Float": "1.00"}), quiet("Y", 5.05)]))
    got = store.with_history(store.read_snapshot(live, "2026-11-06"), "2026-11-06").set_index("ticker")
    assert got.days_since_split.fillna(-1).to_dict() == {"X": 0.0, "Y": -1.0} and got.split_ratio["X"] == pytest.approx(10.0)
    assert store.split_index()[0].empty                               # the filed days themselves show no split
    # The other way round: the filed Friday shows a split that the live rows do not.
    (d / "2026-11-06.lfr.json").write_text(lfr_json("2026-11-06", [row("Y", 50.0, 50.0, 50.5, 49.5, 50.0, 100_000, **{"Shares Float": "1.00"})]))
    assert set(store.split_index()[0].ticker) == {"Y"}
    got = store.with_history(store.read_snapshot(live, "2026-11-06"), "2026-11-06").set_index("ticker")
    assert got.days_since_split.fillna(-1).to_dict() == {"X": 0.0, "Y": -1.0}


def test_a_live_pull_for_an_older_session_is_compared_with_the_rows_before_it(home):
    d = home / "days"
    d.mkdir(parents=True)
    (d / "2026-11-04.lfr.json").write_text(lfr_json("2026-11-04", [quiet("X", 0.30), quiet("Y", 5.00)]))
    (d / "2026-11-06.lfr.json").write_text(lfr_json("2026-11-06", [quiet("X", 3.05, "1.00"), quiet("Y", 5.10)]))   # a later day is saved
    live = home / "live.csv"
    live.write_text(export_csv([row("X", 3.00, 3.00, 3.10, 2.90, 3.00, 100_000, **{"Shares Float": "1.00"}), quiet("Y", 5.05)]))
    got = store.with_history(store.read_snapshot(live, "2026-11-05"), "2026-11-05").set_index("ticker")       # Thursday is not filed
    assert got.days_since_split.fillna(-1).to_dict() == {"X": 0.0, "Y": -1.0}
    assert got.yesterday.to_dict() == {"X": "traded, not a mover", "Y": "traded, not a mover"}


def test_history_fields_are_there_even_with_nothing_saved(home):
    path = home / "live.csv"
    path.write_text(export_csv([mover("RUN"), calm("CALM")]))
    got = store.with_history(store.read_snapshot(path, "2026-11-10"), "2026-11-10").set_index("ticker")
    assert got.yesterday.to_dict() == {"RUN": "no history", "CALM": "no history"} and got.streak.tolist() == [0, 0]
    assert got.days_since_split.isna().all() and got.split_ratio.isna().all()


# ---------------------------------------------------------------- command line

class FakeClient:
    calls = []
    refuse = False
    screener = None
    calendar = None

    def export(self, path, params=None):
        if FakeClient.refuse:
            raise FinvizError("Finviz refused the token (HTTP 403)")
        FakeClient.calls.append((path, dict(params or {})))
        if path == "calendar/earnings":
            return FakeClient.calendar or "Date,Ticker,Company,Market Cap,EPS Estimate,EPS Actual\n2026-11-09 08:30,NEW,New Co,10.5,0.1,0.2\n"
        if path == "stock" and (params or {}).get("ty") == "lf":
            return "Filing Date,Report Date,Form,Description,Filing,Document\n2026-11-06,2026-09-30,10-Q,Quarterly report,https://www.sec.gov/f,https://www.sec.gov/d\n"
        if path == "stock" and (params or {}).get("ty"):
            return "Settlement Date,Short Interest,Short Float\n2026-10-30,6207615,15.68\n"
        if path == "stock":
            return "Date,Open,High,Low,Close,Volume\n11/10/2026 09:30 AM,2.9,3.0,2.8,2.95,1000\n11/10/2026 17:25 PM,3.4,3.4,3.4,3.4,80\n11/10/2026 04:05 PM,3.4,3.4,3.4,3.4,80\n"
        if path == "news":
            return '"Title","Source","Date","Url","Category","Ticker"\n"A headline","Wire",2026-11-10 13:08:17,"https://example.com/a",Stock,AAPL\n'
        if path == "insiders":
            return ("Ticker,Owner,Owner CIK,Relationship,Date,Transaction,Cost,#Shares,Value ($),#Shares Total,SEC Form 4,SEC Form 4 Link\n"
                    "HUCK,Some Owner,1228754,Director,11/04/2026,Buy,2.5,1000,2500,9000,11/06/2026 16:15,http://www.sec.gov/x\n")
        if path == "groups":
            return "Name,Value\nalpha,1\nbeta,2\n"
        return FakeClient.screener or export_csv([mover("LIVE"), mover("RUN"), weak("WEAK"), calm("CALM", 2.05), calm("ZED", 3.06)])


class Now:
    value = at(2026, 11, 10, 16, 30)


@pytest.fixture
def bare(home, monkeypatch):
    """The command line with a fake Finviz and a fixed clock, on an empty store."""
    FakeClient.calls, FakeClient.refuse, FakeClient.screener, FakeClient.calendar = [], False, None, None
    Now.value = at(2026, 11, 10, 16, 30)
    monkeypatch.setattr(cli_mod, "Client", FakeClient)
    monkeypatch.setattr(config, "now_et", lambda: Now.value)
    monkeypatch.setattr(store, "MIN_DAY_ROWS", 2)
    return CliRunner()


@pytest.fixture
def runner(bare, home):
    seed(home)
    return bare


def lines(result):
    return [json.loads(x) for x in result.stdout.splitlines() if x.startswith("{")]


def document(result, first="["):
    """What a command printed from its first line starting with `first`: the notes come before it."""
    out = result.stdout.splitlines()
    start = next(i for i, x in enumerate(out) if x.startswith(first))
    return "\n".join(out[start:])


def test_screen_a_saved_day(runner):
    r = runner.invoke(cli, ["screen", "open_chg >= 10", "--date", "2026-11-09", "--format", "ndjson"])
    assert r.exit_code == 0, r.output
    got = {x["ticker"]: x for x in lines(r)}
    assert set(got) == {"RUN", "NEW"} and got["RUN"]["yesterday"] == "mover, strong close" and got["NEW"]["catalyst"] == "earnings"
    assert not FakeClient.calls                                   # a saved day needs no request


def test_history_fields_do_not_depend_on_the_range_asked_for(bare, home):
    days = split_store(home)
    fields = ("yesterday", "streak", "days_since_split", "split_ratio")
    for ticker in ("SPLT", "GAPD", "NORM"):
        by_date = lines(bare.invoke(cli, ["screen", f"ticker == '{ticker}'", "--date", days[-1], "--format", "ndjson", "--show", "all"]))[0]
        by_since = lines(bare.invoke(cli, ["screen", f"ticker == '{ticker}'", "--since", days[-1], "--format", "ndjson", "--show", "all"]))[0]
        whole = [x for x in lines(bare.invoke(cli, ["screen", f"ticker == '{ticker}'", "--history", "--format", "ndjson", "--show", "all",
                                                    "--top", "0"])) if x["date"] == days[-1]][0]
        assert [by_date[f] for f in fields] == [by_since[f] for f in fields] == [whole[f] for f in fields], ticker
    assert by_date["days_since_split"] is None                    # NORM never split
    r = bare.invoke(cli, ["ticker", "SPLT", "--days", "1", "--format", "ndjson"])
    assert lines(r)[0]["days_since_split"] == 14


def test_splits_command_reads_the_same_index(bare, home):
    days = split_store(home)
    recent = lines(bare.invoke(cli, ["splits", "--days", "5", "--format", "ndjson"]))
    assert [(x["ticker"], x["date"], x["exact"]) for x in recent] == [("GAPD", days[14], False)]
    every = lines(bare.invoke(cli, ["splits", "--days", "30", "--format", "ndjson"]))
    assert [(x["ticker"], x["ratio"]) for x in every] == [("GAPD", 20.0), ("SPLT", 10.0)]


def test_history_sorting_and_bad_input(runner):
    r = runner.invoke(cli, ["screen", "mover", "--history", "--format", "ndjson", "--top", "0"])
    assert r.exit_code == 0 and [x["date"] for x in lines(r)][0] == "2026-11-09" and len(lines(r)) == 6
    r = runner.invoke(cli, ["screen", "mover", "--history", "--sort", "date", "--asc", "--format", "ndjson", "--top", "0"])
    assert [x["date"] for x in lines(r)][0] == "2026-11-05"
    assert "not a filter: unknown column 'nonsense_column'" in runner.invoke(cli, ["screen", "nonsense_column > 1", "--date", "2026-11-09"]).output
    r = runner.invoke(cli, ["screen", "ticker.to_csv('written_by_a_filter.csv') == None", "--date", "2026-11-09"])
    assert r.exit_code != 0 and "not a filter: '.to_csv()' is not available" in r.output                 # a filter is data, not code
    r = runner.invoke(cli, ["screen", "ticker > 1", "--date", "2026-11-09"])                                 # text against a number
    assert r.exit_code != 0 and "not a filter: a comparison compares text with a number: compare like with like" in r.output
    r = runner.invoke(cli, ["screen", "float_m != None", "--date", "2026-11-09"])                           # true of every row, to pandas
    assert r.exit_code != 0 and "not a filter: missing values are tested with .isna() and .notna()" in r.output
    assert runner.invoke(cli, ["screen", "--date", "2026-11-09", "--history"]).exit_code == 2
    assert runner.invoke(cli, ["screen", "--date", "2026-11-09", "--refresh"]).exit_code == 2
    assert "not a date" in runner.invoke(cli, ["screen", "--date", "11/09/2026"]).output
    assert "cannot sort" in runner.invoke(cli, ["screen", "--date", "2026-11-09", "--sort", "nope"]).output


def test_numbers_that_make_no_sense_are_usage_errors(runner):
    for args in (["screen", "--date", "2026-11-09", "--top", "-5"], ["ticker", "RUN", "--days", "0"], ["splits", "--days", "0"],
                 ["insiders", "HUCK", "--min-value", "-500"], ["categories", "--date", "2026-11-09", "--top", "-1"],
                 ["screen", "--since", "2026-11-10", "--until", "2026-11-01"], ["export", "screener", "v=152", "--raw", "--format", "json"],
                 ["columns", "--markdown", "--format", "json"]):
        r = runner.invoke(cli, args)
        assert r.exit_code == 2, (args, r.output)
    assert not FakeClient.calls                                   # refused before anything was asked of Finviz


def test_after_the_close_one_pull_serves_every_command_and_becomes_the_day(runner, home):
    r = runner.invoke(cli, ["screen", "--format", "ndjson"])
    assert r.exit_code == 0, r.output
    got = {x["ticker"]: x for x in lines(r)}
    assert set(got) == {"LIVE", "RUN", "WEAK", "CALM", "ZED"}
    assert got["RUN"]["yesterday"] == "mover, strong close" and got["RUN"]["streak"] == 3     # history reaches the live rows
    assert got["LIVE"]["yesterday"] == "quiet (under 50k shares)"
    assert FakeClient.calls[0][0] == "screener" and FakeClient.calls[0][1]["type"] == "stocks" and not FakeClient.calls[0][1]["f"]
    assert (home / "days" / "2026-11-10.market.csv").exists()
    Now.value = at(2026, 11, 10, 19, 0)
    m = runner.invoke(cli, ["movers", "--format", "ndjson"])
    assert [x["ticker"] for x in lines(m)][-1] == "WEAK" and {x["ticker"] for x in lines(m)} == {"LIVE", "RUN", "WEAK"}
    assert len(FakeClient.calls) == 1                             # the after-close pull is reused all evening


def test_a_pull_that_fails_its_checks_is_never_shown_or_reused(runner, home):
    assert runner.invoke(cli, ["screen", "--format", "ndjson"]).exit_code == 0             # 16:30: a good pull, filed
    good = (home / "days" / "2026-11-10.market.csv").read_text()
    Now.value = at(2026, 11, 10, 17, 0)
    FakeClient.screener = export_csv([mover("ALONE")])                                     # Finviz answers with one row
    r = runner.invoke(cli, ["screen", "--refresh", "--format", "ndjson"])
    assert r.exit_code == 3 and "was not usable" in r.output and "too few" in r.output and not lines(r)       # 3: a rejected pull
    assert runner.invoke(cli, ["movers", "--refresh"]).exit_code == 3 and runner.invoke(cli, ["categories", "--refresh"]).exit_code == 3
    assert (home / "days" / "2026-11-10.market.csv").read_text() == good
    Now.value = at(2026, 11, 10, 17, 5)
    m = runner.invoke(cli, ["movers", "--format", "ndjson"])
    assert {x["ticker"] for x in lines(m)} == {"LIVE", "RUN", "WEAK"}                      # the 16:30 pull, not the bad one
    assert len(FakeClient.calls) == 4                                                      # one good pull, three refused ones
    s = runner.invoke(cli, ["snapshot", "--format", "ndjson"])
    info = lines(s)[0]
    assert s.exit_code == 3 and info["snapshot"].endswith("_rejected.csv") and "too few" in info["rejected_because"] and not info["filed_as_day"]


def test_a_pull_during_the_session_goes_stale(runner, home):
    Now.value = at(2026, 11, 10, 10, 0)
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    Now.value = at(2026, 11, 10, 10, 5)
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    assert len(FakeClient.calls) == 1                             # five minutes old: reused
    Now.value = at(2026, 11, 10, 10, 20)
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    assert len(FakeClient.calls) == 2                             # twenty minutes old: pulled again
    assert not (home / "days" / "2026-11-10.market.csv").exists()
    Now.value = at(2026, 11, 10, 16, 30)
    runner.invoke(cli, ["movers", "--format", "ndjson"])
    assert len(FakeClient.calls) == 3 and (home / "days" / "2026-11-10.market.csv").exists()  # a morning pull never stands in for the close


def test_a_pull_from_before_the_close_is_not_shown_once_the_market_has_closed(runner, home):
    Now.value = at(2026, 11, 10, 15, 56)
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    Now.value = at(2026, 11, 10, 16, 2)                          # six minutes old, but the closing prices are in now
    runner.invoke(cli, ["movers", "--format", "ndjson"])
    assert len(FakeClient.calls) == 2 and not (home / "days" / "2026-11-10.market.csv").exists()     # pulled again; too early to file
    Now.value = at(2026, 11, 10, 16, 4)
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    assert len(FakeClient.calls) == 2                            # the 16:02 pull serves until the close has settled
    Now.value = at(2026, 11, 10, 16, 6)
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    assert len(FakeClient.calls) == 3 and (home / "days" / "2026-11-10.market.csv").exists()         # settled: pulled once more and filed


def clock_that_reads(monkeypatch, *moments):
    """Each look at the clock gives the next moment; the last one from then on."""
    left = list(moments)
    monkeypatch.setattr(config, "now_et", lambda: left.pop(0) if len(left) > 1 else left[0])


def test_an_answer_that_straddles_the_rollover_is_asked_for_again(runner, home, monkeypatch):
    # The command starts at 03:59:59. Pacing holds the request; the answer arrives after 04:00,
    # when Finviz has moved on to Tuesday's premarket. Whose data is it? Ask again and know.
    clock_that_reads(monkeypatch, at(2026, 11, 10, 3, 59, 59), at(2026, 11, 10, 3, 59, 59), at(2026, 11, 10, 4, 0, 1),
                     at(2026, 11, 10, 4, 0, 2), at(2026, 11, 10, 4, 0, 3))
    df, label = cli_mod.today_frame(False)
    assert len(FakeClient.calls) == 2 and label.startswith("2026-11-10 session as of 2026-11-10 04:00")
    assert [p.name for p in (home / "snapshots").iterdir()] == ["2026-11-10"]            # nothing was filed under Monday


def test_an_answer_that_straddles_the_close_is_asked_for_again(runner, home, monkeypatch):
    clock_that_reads(monkeypatch, at(2026, 11, 10, 15, 59, 58), at(2026, 11, 10, 15, 59, 58), at(2026, 11, 10, 16, 0, 2),
                     at(2026, 11, 10, 16, 0, 3), at(2026, 11, 10, 16, 0, 4))
    cli_mod.today_frame(False)
    assert len(FakeClient.calls) == 2
    assert [p.name for p in (home / "snapshots" / "2026-11-10").iterdir()] == ["20261110T160003000000_market.csv"]   # dated when it was asked for
    clock_that_reads(monkeypatch, at(2026, 11, 10, 10, 0, 0), at(2026, 11, 10, 10, 0, 9))                           # nine seconds, no boundary
    FakeClient.calls = []
    cli_mod.pull_today()
    assert len(FakeClient.calls) == 1


class TimedClient(FakeClient):
    """A client that, like the real one, records when the answered request left and when the
    answer was complete. The command's own clock says something else: the queue came first."""
    script = []

    def export(self, path, params=None):
        sent, received, body = TimedClient.script.pop(0)
        self.last_sent, self.last_received = sent.timestamp(), received.timestamp()
        FakeClient.screener = body
        return super().export(path, params)


def test_a_pull_is_dated_by_when_its_request_left_not_by_the_commands_clock(runner, home, monkeypatch):
    monkeypatch.setattr(cli_mod, "Client", TimedClient)
    Now.value = at(2026, 11, 10, 10, 0, 0)                           # the command started here and then waited its turn
    TimedClient.script = [(at(2026, 11, 10, 10, 0, 30), at(2026, 11, 10, 10, 0, 31), None)]
    got = cli_mod.pull_today()
    assert got["path"].name == "20261110T100030000000_market.csv" and got["pulled"] == at(2026, 11, 10, 10, 0, 30)
    # The request left before the close, the answer came after it: asked again, whatever the command's clock says.
    TimedClient.script = [(at(2026, 11, 10, 15, 59, 59), at(2026, 11, 10, 16, 0, 2), None), (at(2026, 11, 10, 16, 0, 3), at(2026, 11, 10, 16, 0, 4), None)]
    FakeClient.calls = []
    got = cli_mod.pull_today()
    assert len(FakeClient.calls) == 2 and got["path"].name == "20261110T160003000000_market.csv"


def test_a_pull_that_is_ambiguous_three_times_running_is_not_kept(runner, home, monkeypatch):
    monkeypatch.setattr(cli_mod, "Client", TimedClient)
    Now.value = at(2026, 11, 10, 16, 10)
    straddles = [(at(2026, 11, 10, 9, 29), at(2026, 11, 10, 9, 31), None), (at(2026, 11, 10, 9, 31), at(2026, 11, 10, 16, 1), None),
                 (at(2026, 11, 10, 16, 1), at(2026, 11, 10, 16, 6), None)]             # the open, the close, the settling
    TimedClient.script = list(straddles)
    r = runner.invoke(cli, ["snapshot", "--format", "ndjson"])
    assert r.exit_code == 3 and "nothing was saved" in r.output and len(FakeClient.calls) == 3 and not lines(r)
    assert not (home / "snapshots").exists() and not (home / "days" / "2026-11-10.market.csv").exists()
    TimedClient.script = list(straddles)
    assert runner.invoke(cli, ["screen"]).exit_code == 3                               # the same from a command that only wanted to look


def test_a_slow_download_of_older_data_does_not_replace_a_newer_record(runner, home, monkeypatch):
    """A's request leaves first and takes four seconds to arrive. B's leaves 0.8 seconds later
    and is back in one. B is filed; when A finally lands it is the older data, and stays out."""
    monkeypatch.setattr(cli_mod, "Client", TimedClient)
    micro = lambda s, us: dt.datetime(2026, 11, 10, 16, 5, s, us, tzinfo=ET)
    older = export_csv([mover("LIVE"), mover("RUN"), weak("WEAK"), calm("CALM", 2.05), calm("OLDER", 3.06)])
    newer = export_csv([mover("LIVE"), mover("RUN"), weak("WEAK"), calm("CALM", 2.05), calm("NEWER", 3.06)])
    Now.value = at(2026, 11, 10, 16, 5, 5)
    TimedClient.script = [(micro(0, 900_000), micro(1, 800_000), newer), (micro(0, 100_000), micro(4, 100_000), older)]
    b = cli_mod.pull_today()                                         # B finishes, and is saved, first
    a = cli_mod.pull_today()
    assert b["filed"] and not a["filed"] and a["superseded_by"] == "20261110T160500900000_market.csv"
    assert "NEWER" in (home / "days" / "2026-11-10.market.csv").read_text()
    assert store.latest_snapshot("2026-11-10")[0].name == "20261110T160500900000_market.csv"


def test_movers_says_so_when_a_day_has_nothing_to_label(runner, home):
    (home / "days" / "2026-11-04.lfr.json").write_text(json.dumps({"date": "2026-11-04", "et": 970, "rows": [{"Ticker": "AAA", "Price": 2.0}]}))
    r = runner.invoke(cli, ["movers", "--date", "2026-11-04"])       # a file from before imports were checked, say
    assert r.exit_code == 1 and "no mover labels" in r.output and "Traceback" not in r.output


def test_a_premarket_pull_is_not_carried_across_the_open(runner, home):
    Now.value = at(2026, 11, 10, 9, 29)
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    Now.value = at(2026, 11, 10, 9, 31)                          # two minutes old, but its columns were premarket values
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    assert len(FakeClient.calls) == 2
    Now.value = at(2026, 11, 10, 9, 36)
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    assert len(FakeClient.calls) == 2                            # inside the session the ten minutes apply again


def test_an_early_close_ends_the_session_at_one(runner, home):
    assert config.is_after_close(at(2026, 11, 27, 13, 5)) and not config.is_after_close(at(2026, 11, 27, 13, 4))
    assert not config.is_after_close(at(2026, 11, 26, 17, 0))                    # Thanksgiving: no session to be after
    Now.value = at(2026, 11, 27, 12, 59)
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    assert len(FakeClient.calls) == 1 and not (home / "days" / "2026-11-27.market.csv").exists()
    Now.value = at(2026, 11, 27, 13, 6)                                          # seven minutes on, but the session is over
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    assert len(FakeClient.calls) == 2 and (home / "days" / "2026-11-27.market.csv").exists()
    Now.value = at(2026, 11, 27, 15, 30)
    runner.invoke(cli, ["movers", "--format", "ndjson"])
    assert len(FakeClient.calls) == 2                                            # the after-close pull serves the afternoon


def test_a_filtered_snapshot_is_kept_apart(runner, home):
    Now.value = at(2026, 11, 10, 10, 0)
    r = runner.invoke(cli, ["snapshot", "--filters", "sh_float_u5", "--format", "ndjson"])
    assert r.exit_code == 0 and lines(r)[0]["snapshot"].endswith("_custom.csv") and FakeClient.calls[0][1]["f"] == "sh_float_u5"
    runner.invoke(cli, ["screen", "--format", "ndjson"])
    assert len(FakeClient.calls) == 2 and not FakeClient.calls[1][1]["f"]          # screen made its own whole-market pull
    assert "unknown column" in runner.invoke(cli, ["snapshot", "--columns", "bogus"]).output


def test_a_weekend_pull_belongs_to_friday_but_is_not_the_day_record(runner, home):
    Now.value = at(2026, 11, 13, 15, 0)
    runner.invoke(cli, ["snapshot"])
    Now.value = at(2026, 11, 14, 10, 0)                           # Saturday
    r = runner.invoke(cli, ["snapshot", "--format", "ndjson"])
    info = lines(r)[0]
    assert r.exit_code == 0 and info["session"] == "2026-11-13" and info["filed_as_day"] is False
    path, pulled = store.latest_snapshot("2026-11-13")
    assert path.name == "20261114T100000000000_market.csv" and pulled.day == 14        # it sorts after Friday's own pulls
    assert not (home / "days" / "2026-11-13.market.csv").exists()


def test_a_first_pull_on_an_empty_store_has_every_history_field(bare, home):
    r = bare.invoke(cli, ["screen", "yesterday == 'no history'", "--format", "ndjson", "--show", "ticker,yesterday,streak,days_since_split"])
    assert r.exit_code == 0, r.output
    assert {x["ticker"]: (x["yesterday"], x["streak"], x["days_since_split"]) for x in lines(r)} == {
        t: ("no history", 0, None) for t in ("LIVE", "RUN", "WEAK", "CALM", "ZED")}


def test_categories_counts_and_copes_with_nothing_to_count(runner):
    r = runner.invoke(cli, ["categories", "--date", "2026-11-09", "--format", "ndjson"])
    assert r.exit_code == 0 and sum(x["names"] for x in lines(r)) == 3                    # RUN, WEAK and NEW are movers
    for extra in (["--date", "2026-11-09"], ["--history"]):
        for fmt in ("table", "ndjson", "json"):
            r = runner.invoke(cli, ["categories", "price > 99999", *extra, "--format", fmt])
            assert r.exit_code == 0, (extra, fmt, r.output)
    assert "(no rows)" in runner.invoke(cli, ["categories", "price > 99999", "--history", "--format", "table"]).output
    assert "flat open, ran and held" in runner.invoke(cli, ["categories", "--history", "--format", "table"]).output
    one = runner.invoke(cli, ["categories", "--history", "--format", "table", "--top", "1"]).output
    every = runner.invoke(cli, ["categories", "--history", "--format", "table", "--top", "0"]).output
    assert "(no rows)" not in every and every.count("2026-11-") > one.count("2026-11-")         # 0 lists every name, not none


def test_movers_order_and_saved_list(runner, home):
    r = runner.invoke(cli, ["movers", "--date", "2026-11-09", "--save", "--format", "ndjson"])
    assert r.exit_code == 0, r.output
    names = [x["ticker"] for x in lines(r)]
    assert set(names[:2]) == {"RUN", "NEW"} and names[2] == "WEAK"               # strong closers first, the weak closer last
    assert (home / "watchlists" / "after_2026-11-09.csv").exists()
    (home / "days" / "2026-11-04.lfr.json").write_text(lfr_json("2026-11-04", [calm("CALM", 1.9), calm("ZED", 2.9)]))
    r = runner.invoke(cli, ["movers", "--date", "2026-11-04", "--save", "--format", "ndjson"])
    saved = home / "watchlists" / "after_2026-11-04.csv"                          # a day without movers is still that day
    assert r.exit_code == 0 and not lines(r) and saved.exists() and not (home / "watchlists" / "after_none.csv").exists()
    header = saved.read_text().splitlines()
    assert len(header) == 1 and {"date", "ticker", "close_kind"} <= set(header[0].split(","))


def test_ticker_history_status_and_import_from_the_command_line(runner, home, tmp_path):
    r = runner.invoke(cli, ["ticker", "run", "--days", "2", "--format", "ndjson"])
    assert r.exit_code == 0 and [x["date"] for x in lines(r)] == ["2026-11-09", "2026-11-06"]
    s = lines(runner.invoke(cli, ["history", "status", "--format", "ndjson"]))[0]
    assert s["trading_days"] == 3 and s["by_source"] == "lfr 2, market 1"
    src = tmp_path / "eod"
    src.mkdir()
    (src / "2026-11-04.json").write_text(lfr_json("2026-11-04", [calm("CALM", 1.9)]))
    r = runner.invoke(cli, ["history", "import-lfr", str(src)])
    assert r.exit_code == 0 and "added 1, unchanged 0, rejected 0; 4 trading days saved" in r.output
    (src / "2026-11-03.json").write_text(json.dumps({"date": "2026-11-03", "et": 600, "rows": [{"Ticker": "AAA"}]}))
    r = runner.invoke(cli, ["history", "import-lfr", str(src), "--format", "ndjson"])
    assert r.exit_code == 3 and "rejected 2026-11-03.json: captured at 10:00 ET, before the close" in r.output
    assert lines(r) == [{"added": 0, "unchanged": 1, "rejected": 1, "trading_days": 4,
                         "rejected_files": "2026-11-03.json: captured at 10:00 ET, before the close"}]      # a result on stdout too


def test_json_and_csv_output_from_a_command(runner):
    r = runner.invoke(cli, ["screen", "mover", "--date", "2026-11-09", "--format", "json", "--show", "ticker,price,days_since_split"])
    rows = json.loads(document(r))
    assert r.exit_code == 0 and {x["ticker"] for x in rows} == {"RUN", "WEAK", "NEW"} and rows[0]["days_since_split"] is None
    r = runner.invoke(cli, ["screen", "mover", "--date", "2026-11-09", "--format", "csv", "--show", "ticker,price"])
    assert document(r, "ticker").splitlines()[0] == "ticker,price" and len(document(r, "ticker").splitlines()) == 4
    r = runner.invoke(cli, ["screen", "price > 99999", "--date", "2026-11-09", "--format", "csv", "--show", "ticker,price"])
    assert r.exit_code == 0 and document(r, "ticker").splitlines() == ["ticker,price"]       # no rows still has its header


def test_bars_normalises_finviz_timestamps(runner):
    r = runner.invoke(cli, ["bars", "huck", "--tf", "i1", "--range", "d1", "--format", "ndjson"])
    assert r.exit_code == 0, r.output
    assert [x["date"] for x in lines(r)] == ["2026-11-10 09:30", "2026-11-10 17:25", "2026-11-10 16:05"]
    assert FakeClient.calls[0] == ("stock", {"t": "HUCK", "p": "i1", "r": "d1"})


def test_bar_dates_must_all_be_readable():
    class Feed:
        def __init__(self, body):
            self.body = body

        def export(self, path, params=None):
            return "Date,Open,High,Low,Close,Volume\n" + self.body

    with pytest.raises(FinvizError, match="could not read the dates"):
        endpoints.bars(Feed("yesterday,1,1,1,1,1\n"), "AAA", "i1")
    with pytest.raises(FinvizError, match="1 of 2 rows; first: 'soon'"):       # one bad row is not waved through as a blank
        endpoints.bars(Feed("11/10/2026 09:30 AM,1,1,1,1,1\nsoon,1,1,1,1,1\n"), "AAA", "i1")
    with pytest.raises(FinvizError, match="could not read the dates"):
        endpoints.bars(Feed("11/10/2026,1,1,1,1,1\nlater,1,1,1,1,1\n"), "AAA", "d")
    df = endpoints.bars(Feed("11/10/2026 09:30:15,1,1,1,1,1\n11/10/2026 04:05:00 PM,1,1,1,1,1\n"), "AAA", "i1")
    assert df.date.tolist() == ["2026-11-10 09:30", "2026-11-10 16:05"]         # seconds, where Finviz sends them

    class Shifted:                                                               # the date is not the first column, and one row is short
        def export(self, path, params=None):
            return "Open,Date\n1,11/10/2026 09:30 AM\n2\n"

    with pytest.raises(FinvizError, match="1 of 2 rows; first: ''"):             # a clear error, not a TypeError on a missing value
        endpoints.bars(Shifted(), "AAA", "i1")


def test_earnings_sync_fills_the_store_and_reports_what_it_did(runner, home):
    r = runner.invoke(cli, ["earnings", "--sync", "--format", "json"])
    assert r.exit_code == 0, r.output
    assert json.loads(document(r)) == [{"from": "2026-11-04", "to": "2026-11-24", "rows_pulled": 1, "rows_in_store": 1}]
    assert FakeClient.calls[0][1]["dateFrom"] == "2026-11-04" and store.status()["earnings_rows"] == 1      # the session before the first saved day
    assert runner.invoke(cli, ["earnings", "--from", "2026-11-10", "--to", "2026-11-01"]).exit_code == 2
    FakeClient.calls = []
    runner.invoke(cli, ["earnings", "--sync", "--from", "2026-11-06"])
    assert FakeClient.calls[0][1]["dateFrom"] == "2026-11-06"                                                # a start that was asked for is kept


def test_a_sync_reaches_back_to_the_evening_before_the_first_saved_day(runner, home):
    """Thursday is the first saved day. CALM reported on Wednesday after the close: that is
    Thursday's catalyst, and a sync that started on Thursday would never fetch it."""
    assert store.load_day("2026-11-05").set_index("ticker").catalyst["CALM"] == "none"
    FakeClient.calendar = "Date,Ticker,Company\n2026-11-04 16:30,CALM,Calm Co\n2026-11-04 08:30,ZED,Zed Co\n"
    r = runner.invoke(cli, ["earnings", "--sync"])
    assert r.exit_code == 0, r.output
    day = store.load_day("2026-11-05").set_index("ticker")
    assert day.catalyst["CALM"] == "earnings" and day.catalyst["ZED"] == "none"      # ZED reported before Wednesday's close: Wednesday's news
    for name in ("2026-11-05.lfr.json", "2026-11-06.lfr.json"):                      # now a history that starts on a Monday
        (home / "days" / name).unlink()
    FakeClient.calls = []
    runner.invoke(cli, ["earnings", "--sync"])
    assert FakeClient.calls[0][1]["dateFrom"] == "2026-11-06"                         # Friday, not Sunday and not Monday


def test_an_earnings_answer_that_did_not_arrive_whole_leaves_the_calendar_as_it_was(runner, home):
    runner.invoke(cli, ["earnings", "--sync"])
    saved = store.earnings_path().read_text()
    assert "NEW" in saved
    for broken in ('Date,Ticker,Company\n2026-11-09 08:30,NEW,"unfinished\n', "Date,Ticker,Company\n2026-11-09 08:30,NE", "Error\n"):
        FakeClient.calendar = broken
        r = runner.invoke(cli, ["earnings", "--sync", "--format", "ndjson"])
        assert r.exit_code != 0 and not lines(r), broken
        assert store.earnings_path().read_text() == saved                             # the whole way through the command: untouched


def test_a_calendar_for_other_dates_than_asked_for_replaces_nothing(runner, home):
    good = pd.DataFrame([{"date": "2026-11-05 16:30", "ticker": "CALM"}, {"date": "2026-11-06 08:30", "ticker": "ZED"}])
    store.save_earnings(good, "2026-11-05", "2026-11-06")
    saved = store.earnings_path().read_text()
    FakeClient.calendar = "Date,Ticker,Company\n2026-10-30 08:30,OTHER,Other Co\n"           # well-formed, and for another week
    r = runner.invoke(cli, ["earnings", "--from", "2026-11-05", "--to", "2026-11-06", "--save", "--format", "ndjson"])
    assert r.exit_code != 0 and isinstance(r.exception, FinvizError) and "outside that range" in str(r.exception) and not lines(r)
    assert store.earnings_path().read_text() == saved
    stale = pd.DataFrame([{"date": "2026-10-30 08:30", "ticker": "OTHER"}])                   # the store's own guard, without the endpoint in front
    late = pd.DataFrame([{"date": "2026-11-07 00:00", "ticker": "NEXT"}])                     # one minute past the range is past it
    for outside in (stale, late):
        with pytest.raises(ValueError, match="outside 2026-11-05 .. 2026-11-06"):
            store.save_earnings(outside, "2026-11-05", "2026-11-06")
        assert store.earnings_path().read_text() == saved
    edge = pd.DataFrame([{"date": "2026-11-05 00:00", "ticker": "FIRST"}, {"date": "2026-11-06 23:59", "ticker": "LAST"}])
    assert store.save_earnings(edge, "2026-11-05", "2026-11-06") == 2                         # both ends of the range are inside it


def test_a_long_earnings_span_is_asked_for_in_pieces(runner):
    FakeClient.calendar = "Date,Ticker,Company\n"                    # nothing reported: a calendar all the same
    endpoints.earnings(FakeClient(), "2026-01-01", "2026-06-30")
    asked = [(p["dateFrom"], p["dateTo"]) for _, p in FakeClient.calls]
    assert asked == [("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-29"), ("2026-06-30", "")]        # 90 days at most each


def test_the_other_exports_ask_for_the_right_thing(runner):
    for args, call in ((["news", "aapl", "msft"], ("news", {"v": "3", "c": "", "t": "AAPL,MSFT"})),      # (the client drops what is empty)
                       (["short-interest", "huck"], ("stock", {"t": "HUCK", "ty": "si"})),
                       (["filings", "huck", "--group", "insider-equity"], ("stock", {"t": "HUCK", "ty": "lf", "f": "insider-equity", "o": "-filingDate"})),
                       (["insiders", "huck", "--kind", "buy", "--min-value", "100000"],
                        ("insiders", {"t": "HUCK", "tc": "1", "tv": 100000, "oc": "", "or": "", "o": ""})),
                       (["groups", "--by", "industry"], ("groups", {"g": "industry", "v": "152", "o": ""})),
                       (["export", "calendar/economic", "dateFrom=2026-11-09"], ("calendar/economic", {"dateFrom": "2026-11-09"}))):
        FakeClient.calls = []
        r = runner.invoke(cli, [*args, "--format", "ndjson"])
        assert r.exit_code == 0 and lines(r), (args, r.output)
        assert FakeClient.calls == [call], args


def test_columns_markdown_and_export_parameters(runner):
    r = runner.invoke(cli, ["columns", "--markdown"])
    assert r.exit_code == 0 and "| 25 | `float_m` | Shares Float |" in r.output
    r = runner.invoke(cli, ["export", "screener", "novalue"])
    assert r.exit_code != 0 and "key=value" in r.output
    r = runner.invoke(cli, ["export", "groups", "g=sector", "--raw"])
    assert r.exit_code == 0 and r.stdout == "Name,Value\nalpha,1\nbeta,2\n"


def test_main_reports_failures_as_one_line_and_an_exit_code(runner, capsys, monkeypatch):
    FakeClient.refuse = True
    with pytest.raises(SystemExit) as e:
        cli_mod.main(["doctor", "--format", "ndjson"])
    assert e.value.code == 2 and '"ok":false' in capsys.readouterr().out
    secret = "99999999-8888-7777-6666-555555555555"

    def boom(*a, **k):
        raise RuntimeError(f"unexpected, with https://elite.finviz.com/export/screener?auth={secret}")
    monkeypatch.setattr(store, "status", boom)
    with pytest.raises(SystemExit) as e:
        cli_mod.main(["history", "status"])
    err = capsys.readouterr().err
    assert e.value.code == 1 and err.startswith("error: RuntimeError") and secret not in err and "Traceback" not in err
    assert err.count("\n") == 1 and "auth=<redacted>" in err                   # one physical line, the token gone
    for argv in (["screen", "--date", "2026-11-09\nsecond line\nthird"], ["screen", "price >\n\n1e99 and", "--date", "2026-11-09"]):
        with pytest.raises(SystemExit) as e:
            cli_mod.main(argv)                                                 # a line break in an argument stays out of the error
        err = capsys.readouterr().err
        assert e.value.code != 0 and err.startswith("error: ") and err.count("\n") == 1, err
    with pytest.raises(SystemExit) as e:
        cli_mod.main(["columns", "--preset", "core", "--format", "ndjson"])
    assert e.value.code == 0


# ---------------------------------------------------------------- tables and JSON

def test_generic_export_keeps_what_is_not_a_number():
    df = endpoints.generic("Settlement Date,Short Interest,Short Float\n2026-10-30,6207615,15.68\n2026-10-15,5943336,15.02\n")
    assert list(df.columns) == ["settlement_date", "short_interest", "short_float"]
    assert df.short_interest.sum() == 12150951 and df.settlement_date.iloc[0] == "2026-10-30"
    forms = endpoints.generic("Form,Owner Id,Value\n4,000123,10\n4/A,000456,\n4,000789,12.5\n")
    assert forms.form.tolist() == ["4", "4/A", "4"] and forms.owner_id.tolist() == ["000123", "000456", "000789"]
    assert forms.value.iloc[0] == 10 and pd.isna(forms.value.iloc[1])
    empty = endpoints.generic("Settlement Date,Short Interest\n")
    assert list(empty.columns) == ["settlement_date", "short_interest"] and empty.empty      # headers survive an empty answer
    odd = endpoints.generic("Ticker,Price,price\nAAA,1.5,2.5\nBBB,3,4,5,6\n")               # two headers with one name, a row too long
    assert list(odd.columns) == ["ticker", "price"] and odd.ticker.tolist() == ["AAA", "BBB"] and odd.price.tolist() == [1.5, 3.0]


def test_json_output_is_clean():
    df = pd.DataFrame({"ticker": ["AAA"], "n": pd.Series([3], dtype="int64"), "x": [float("nan")], "tiny": [0.00000123],
                       "big": [1234567.891234567], "when": [pd.NaT], "flag": [True]})
    rec = output.records(df)[0]
    assert rec == {"ticker": "AAA", "n": 3, "x": None, "tiny": 0.00000123, "big": 1234567.891235, "when": None, "flag": True}
    json.dumps(rec)


def test_table_output_trims_and_left_aligns_text(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(output.sys, "stdout", buf)
    df = pd.DataFrame({"ticker": ["AAA", "B"], "vol": [8_247_663.0, 182_000.0], "news": ["x" * 100, "short"]})
    output.emit(df, "table", wide_text=20)
    text = buf.getvalue().splitlines()
    assert "x" * 21 not in buf.getvalue() and "8.2M" in text[1] and "182k" in text[2]
    assert text[2].split()[0] == "B" and text[2].index("B") == text[1].index("AAA")      # text columns start at the same place


def test_a_filter_that_breaks_in_the_working_out_is_an_error_in_one_line(runner, monkeypatch):
    """Whatever goes wrong inside pandas while a filter is worked out is reported, not thrown."""
    def broken(df, expression):
        raise OverflowError("int too big to convert")
    monkeypatch.setattr("finviz_cli.filters.apply", broken)
    r = runner.invoke(cli, ["screen", "price > 1", "--date", "2026-11-09"])
    assert r.exit_code != 0 and "could not run the filter (OverflowError: int too big to convert)" in r.output and "Traceback" not in r.output


def test_a_flag_is_a_flag_on_every_day_of_a_stack(home):
    """A day too bare to be labelled has no mover and no split headline: false, not a gap that turns the column into objects."""
    seed(home)
    days = store.load_days(["2026-11-05", "2026-11-06", "2026-11-09"])
    assert days["mover"].dtype == bool and days["split_headline"].dtype == bool
    stacked = pd.concat([days[days.date == "2026-11-09"], days[days.date == "2026-11-05"].drop(columns=["mover", "split_headline"])], ignore_index=True)
    assert stacked["split_headline"].isna().any()                                 # what a day without the column does to a stack


def test_columns_screen_lists_the_table_a_filter_works_on(runner):
    r = runner.invoke(cli, ["columns", "--screen", "--format", "ndjson"])
    names = [x["name"] for x in lines(r)]
    assert r.exit_code == 0 and len(names) == len(set(names)) == 75 and {"streak", "yesterday", "float_m", "date"} <= set(names)
    assert "--screen stands alone" in runner.invoke(cli, ["columns", "--screen", "--preset", "wide"]).output
    assert "--screen stands alone" in runner.invoke(cli, ["columns", "--screen", "--markdown"]).output
