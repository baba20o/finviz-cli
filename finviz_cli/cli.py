"""finviz: Finviz Elite data with a local history and runner enrichment.

Results go to stdout (a table at a terminal, one JSON object per line when piped); notes about
what was loaded go to stderr. Run `finviz COMMAND --help` for each command.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import click
import pandas as pd

from finviz_cli import __version__, columns as C, config, endpoints, enrich, files, filters, intraday, output, sessions, store
from finviz_cli.client import Client, FinvizError, redact

DEFAULT_SHOW = ["ticker", "price", "chg", "gap", "open_chg", "high_vs_open", "close_pos", "relvol", "vol", "float_m",
                "rotation", "shape", "catalyst", "yesterday", "streak", "news"]
WHOLE_MARKET = ""              # no screener filter: every listed stock, so quiet names are on record too


MAX_AGE_MINUTES = 10           # during a session, a pull older than this is not reused


class Rejected(click.ClickException):
    """A pull or an import that failed its checks. Exit code 3, whichever command met it."""

    exit_code = 3


def session_day(when: dt.datetime) -> str:
    """The session a pull describes: today from 04:00 ET on a trading day, else the session before.

    Finviz shows premarket values from about 04:00; before that, and on weekends and holidays,
    the columns still describe the last session.
    """
    d = when.date()
    if sessions.is_session(d) and when.hour >= 4:
        return d.isoformat()
    return sessions.previous_session(d).isoformat()


def iso_date(_ctx, _param, value):
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise click.BadParameter(f"'{value}' is not a date; use YYYY-MM-DD") from None


PULL_TRIES = 3


def _phase(when: dt.datetime) -> tuple[str, int]:
    """What a pull taken at `when` describes: which session, and how far through it the clock
    is (before the open, in the session, closed but not settled, settled)."""
    session = session_day(when)
    day = dt.date.fromisoformat(session)
    marks = (dt.datetime.combine(day, sessions.OPEN, tzinfo=config.ET),
             dt.datetime.combine(day, sessions.close_time(day), tzinfo=config.ET), config.record_from(day))
    return session, sum(when >= m for m in marks)


def _span(client, asked: dt.datetime, done: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """(when the answered request left, when its answer was complete), Eastern. The client
    records both around the request itself, after pacing and retries; a client that does not
    is taken at the caller's own two looks at the clock."""
    sent, received = getattr(client, "last_sent", None), getattr(client, "last_received", None)
    if sent is None or received is None:
        return asked, done
    return dt.datetime.fromtimestamp(sent, config.ET), dt.datetime.fromtimestamp(received, config.ET)


def pull_today(cols: str | None = None, filters: str = WHOLE_MARKET) -> dict:
    """Pull the screener, keep it, and file it as the day's record when it qualifies."""
    try:
        col_ids = C.resolve(cols)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    # A pull is dated by when its request left, after any queueing: that is the moment the
    # data is from, and it is what orders two pulls whose downloads finish the other way round.
    # If a moment where the columns change meaning (04:00, the open, the close, its settling)
    # fell between the request leaving and the answer being complete, the data could be from
    # either side, so it is asked for again.
    for _ in range(PULL_TRIES):
        client = Client()
        asked = config.now_et()
        text = endpoints.screener_csv(client, filters=filters, cols=col_ids)
        when, answered = _span(client, asked, config.now_et())
        if _phase(when) == _phase(answered):
            break
    else:
        # Every try straddled a boundary (answers that take minutes, a clock being moved). Data
        # of unknown date is not kept as a snapshot and not filed: the command says so and stops.
        raise Rejected(f"{PULL_TRIES} pulls in a row began on one side of a change in the market's state and were answered "
                       f"on the other (the last: {when:%H:%M:%S} to {answered:%H:%M:%S} ET); nothing was saved")
    session = session_day(when)
    whole = filters == WHOLE_MARKET and cols in (None, "wide")
    as_day = whole and session == when.date().isoformat() and config.is_after_close(when)
    saved = store.save_snapshot(text, when, session, whole, as_day)
    return {"path": Path(saved["snapshot"]), "session": session, "pulled": when, "filed": bool(saved["day_file"]),
            "refused": saved["refused"], "usable": saved["usable"], "rows": saved["rows"], "superseded_by": saved["superseded_by"]}


def _filing_note(got: dict) -> str:
    if got["filed"]:
        return "; filed as the day's record"
    if got["superseded_by"]:
        return f"; not filed as the day's record: a later pull is already kept ({got['superseded_by']})"
    return ""


def today_frame(refresh: bool) -> tuple[pd.DataFrame, str]:
    """Today's whole-market rows: the latest pull if it is still good, else a fresh one.

    A pull goes stale at the moments the columns change meaning: the open (premarket values
    give way to the session's), the close, and five minutes after it when the close has
    settled. In between, one taken before the close had settled goes stale after
    MAX_AGE_MINUTES. A pull taken once it had settled stays good until the next session's
    premarket. A pull that failed its checks is never shown: the command
    stops and says why.
    """
    now = config.now_et()
    session = session_day(now)
    found = None if refresh else store.latest_snapshot(session)
    if found:
        path, pulled = found
        day = dt.date.fromisoformat(session)
        opens = dt.datetime.combine(day, sessions.OPEN, tzinfo=config.ET)
        closes = dt.datetime.combine(day, sessions.close_time(day), tzinfo=config.ET)     # 16:00, or 13:00 on an early close
        settled = config.record_from(day)                                                 # five minutes later: the close is in
        crossed = any(pulled < moment <= now for moment in (opens, closes, settled))
        if pulled < settled and (crossed or (now - pulled) > dt.timedelta(minutes=MAX_AGE_MINUTES)):
            found = None
    if found is None:
        got = pull_today()
        session = got["session"]                    # as the pull itself saw the clock: 04:00 may have passed since `now`
        if not got["usable"]:
            raise Rejected(f"the pull at {got['pulled']:%H:%M} ET was not usable: {got['refused']}. "
                           f"It was kept as {got['path'].name}; nothing was filed. Saved days are still available with --date.")
        output.note(f"pulled {got['rows']} rows at {got['pulled']:%H:%M} ET for the {session} session" + _filing_note(got))
        # Show the newest pull kept for the session. Normally this one; a later one if another
        # command overlapped with this and finished first.
        path, pulled = store.latest_snapshot(session) or (got["path"], got["pulled"])
    df = store.with_history(store.read_snapshot(path, session), session)
    return df, f"{session} session as of {pulled:%Y-%m-%d %H:%M} ET, whole market"


def pick_data(date: str | None, history: bool, since: str | None, until: str | None, refresh: bool) -> tuple[pd.DataFrame, str, int]:
    """Rows for the request: a stack of saved days, one saved day, or today's pull."""
    ranged = history or since or until
    if date and ranged:
        raise click.UsageError("--date picks one day; do not combine it with --history, --since or --until")
    if refresh and (date or ranged):
        raise click.UsageError("--refresh pulls today's data; it does not apply to saved days")
    if since and until and until < since:
        raise click.UsageError("--until is before --since")
    if ranged:
        days = store.trading_days()
        use = [d for d in days if (not since or d >= since) and (not until or d <= until)]
        if not use:
            raise click.ClickException("no saved days in that range (see: finviz history status)")
        return store.history_frame(use), f"{len(use)} saved days, {use[0]} .. {use[-1]}", len(use)
    if date:
        if date not in store.trading_days():
            raise click.ClickException(f"{date} is not a saved trading day (see: finviz history status)")
        df = store.history_frame([date])
        return df, f"{date} after the close" + (f" ({df.source.iloc[0]})" if len(df) else ""), 1
    df, label = today_frame(refresh)
    return df, label, 1


def show_cols(df: pd.DataFrame, show: str | None, default: list[str] | None = None) -> list[str]:
    """Columns to print: the caller's choice, else `default` (missing ones are skipped quietly)."""
    if show == "all":
        return list(df.columns)
    want = [c.strip() for c in show.split(",")] if show else (default or DEFAULT_SHOW)
    missing = [c for c in want if c not in df.columns] if show else []
    if missing:
        raise click.ClickException(f"unknown column(s): {', '.join(missing)}. Available: {', '.join(df.columns)}")
    return [c for c in dict.fromkeys(want) if c in df.columns]


def run_query(df: pd.DataFrame, query: str | None) -> pd.DataFrame:
    if not query:
        return df
    # A filter is parsed, checked and worked out in filters.py: it may compare and combine the
    # table's columns and nothing else, whoever or whatever wrote it. pandas never reads the text.
    try:
        return filters.apply(df, query)
    except filters.FilterError as exc:
        columns = f". Columns: {', '.join(df.columns)}" if "unknown column" in str(exc) else ""
        raise click.ClickException(f"not a filter: {exc}{columns}") from None
    except Exception as exc:
        raise click.ClickException(f"could not run the filter ({type(exc).__name__}: {exc}). Columns: {', '.join(df.columns)}") from None


data_options = [
    click.option("--date", callback=iso_date, help="A saved day, YYYY-MM-DD."),
    click.option("--history", is_flag=True, help="Every saved day."),
    click.option("--since", callback=iso_date, help="Saved days from this date."),
    click.option("--until", callback=iso_date, help="Saved days up to this date."),
    click.option("--refresh", is_flag=True, help="Pull fresh data from Finviz first (today only)."),
]


def with_data_options(fn):
    for opt in reversed(data_options):
        fn = opt(fn)
    return fn


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="finviz")
@click.option("--format", "fmt", type=click.Choice(output.FORMATS), default="auto", help="table, ndjson, json or csv. Default: table at a terminal, ndjson when piped.")
@click.option("--home", type=click.Path(), help="Data folder (default ~/.finviz-cli or $FINVIZ_CLI_HOME).")
@click.pass_context
def cli(ctx, fmt, home):
    """Finviz Elite data with a local history and runner enrichment."""
    if home:
        os.environ["FINVIZ_CLI_HOME"] = str(home)
    ctx.obj = {"fmt": fmt}


# ---------------------------------------------------------------- screening

@cli.command()
@click.argument("query", required=False)
@with_data_options
@click.option("--tickers", help="Only these tickers, comma separated.")
@click.option("--sort", help="Column to sort by (default: open_chg, or date for several days).")
@click.option("--asc", is_flag=True, help="Sort ascending.")
@click.option("--top", type=click.IntRange(min=0), default=25, show_default=True, help="Rows to print; 0 for all.")
@click.option("--show", help="Columns to print, comma separated, or 'all'.")
@click.pass_context
def screen(ctx, query, date, history, since, until, refresh, tickers, sort, asc, top, show):
    """Filter stocks with any expression over the columns.

    \b
    finviz screen "abs(gap) <= 3 and open_chg >= 10 and relvol >= 3"
    finviz screen "float_m < 5 and gap >= 20" --history --sort open_chg --asc
    finviz screen "yesterday == 'mover, weak close'" --date 2026-11-09
    """
    df, label, n_days = pick_data(date, history, since, until, refresh)
    if tickers:
        df = df[df.ticker.isin([t.strip().upper() for t in tickers.split(",")])]
    out = run_query(df, query)
    output.note(f"data: {label}; {len(df)} rows, {len(out)} match" + (f" on {out.date.nunique()} of {n_days} days" if n_days > 1 else ""))
    by = sort or ("date" if n_days > 1 else "open_chg")           # default: newest day, or biggest move, first
    if sort and sort not in out.columns:
        raise click.ClickException(f"cannot sort by '{sort}'. Columns: {', '.join(out.columns)}")
    if by in out.columns:
        out = out.sort_values(by, ascending=asc)
    cols = show_cols(out, show, (["date"] if n_days > 1 else []) + DEFAULT_SHOW)
    output.emit(out[cols].head(top) if top else out[cols], ctx.obj["fmt"])


@cli.command()
@click.argument("query", required=False)
@with_data_options
@click.option("--top", type=click.IntRange(min=0), default=12, show_default=True, help="Names listed per shape (table output); 0 for all.")
@click.pass_context
def categories(ctx, query, date, history, since, until, refresh, top):
    """Movers grouped by the outline of their day and what set them off.

    Without a filter this uses the standard mover definition (finviz movers --help).
    """
    df, label, n_days = pick_data(date, history, since, until, refresh)
    df = run_query(df, query or enrich.MOVERS)
    output.note(f"data: {label}; {len(df)} names" + (f" over {n_days} days" if n_days > 1 else ""))
    if not {"opening", "day", "catalyst"}.issubset(df.columns):
        raise click.ClickException("this data has no shape labels (the columns they are built from are missing)")
    counts = df.groupby(["opening", "day", "catalyst"]).size().reset_index(name="names")
    counts["per_day"] = (counts.names / n_days).round(2)
    if output.pick(ctx.obj["fmt"]) != "table":
        output.emit(counts, ctx.obj["fmt"])
        return
    if df.empty:
        click.echo("(no rows)")
        return
    table = pd.crosstab(df["shape"], df["catalyst"], margins=True)
    if n_days > 1:
        table["per day"] = (table["All"] / n_days).round(1)
    click.echo(table.to_string())
    cols = [c for c in (["date"] if n_days > 1 else []) + ["ticker", "price", "chg", "gap", "open_chg", "high_vs_open", "close_pos",
            "relvol", "float_m", "rotation", "catalyst", "yesterday", "streak", "news"] if c in df.columns]
    for name, grp in sorted(df.groupby("shape"), key=lambda kv: -len(kv[1])):
        click.echo(f"\n== {name} ({len(grp)}) ==")
        listed = grp.sort_values("high_vs_open" if n_days > 1 else "relvol", ascending=False)[cols]
        output.emit(listed.head(top) if top else listed, "table", 48)          # 0 lists every name, as in `screen`


@cli.command()
@click.option("--date", callback=iso_date, help="A saved day, YYYY-MM-DD (default: today's pull).")
@click.option("--refresh", is_flag=True, help="Pull fresh data from Finviz first.")
@click.option("--show", help="Columns to print, comma separated, or 'all'.")
@click.option("--save", is_flag=True, help="Also write the list to <home>/watchlists/after_<date>.csv.")
@click.pass_context
def movers(ctx, date, refresh, show, save):
    """The day's movers with how each one closed: the watchlist for the next session.

    \b
    A mover: price $1+, $1M+ traded, 2x normal volume, and a 10% up-move somewhere
    (high vs open, close vs previous close, or gap). Strong closers are listed first,
    weak closers last.
    """
    df, label, _ = pick_data(date, False, None, None, refresh)
    if not {"mover", "close_kind", "chg"}.issubset(df.columns):
        raise click.ClickException("this data has no mover labels (the columns they are built from are missing)")
    out = df[df.mover].copy()
    out["_order"] = out.close_kind.map({"strong close": 0, "in between": 1, "weak close": 2})
    out = out.sort_values(["_order", "chg"], ascending=[True, False])
    output.note(f"data: {label}; {len(out)} movers")
    cols = show_cols(out, show, ["ticker", "price", "chg", "open_chg", "close_pos", "relvol", "float_m", "rotation",
                                 "close_kind", "shape", "catalyst", "yesterday", "streak", "days_since_split"])
    cols = [c for c in cols if c != "_order"]
    if save:
        if not len(df):
            raise click.ClickException("there are no rows for that day, so there is no list to save")
        path = config.home() / "watchlists" / f"after_{df.date.iloc[0]}.csv"          # named for the day, movers or not
        files.write_text_atomic(path, out.drop(columns=["_order"]).to_csv(index=False, lineterminator="\n"))
        output.note(f"saved: {path}")
    output.emit(out[cols], ctx.obj["fmt"])


@cli.command()
@click.argument("symbols", nargs=-1, required=True)
@click.option("--days", type=click.IntRange(min=1), default=10, show_default=True, help="Saved sessions to show.")
@click.option("--show", help="Columns to print, comma separated, or 'all'.")
@click.pass_context
def ticker(ctx, symbols, days, show):
    """One or more tickers across the saved days, newest first."""
    all_days = store.trading_days()
    if not all_days:
        raise click.ClickException("no saved days yet (see: finviz history status)")
    df = store.history_frame(all_days[-days:])
    df = df[df.ticker.isin([s.upper() for s in symbols])].sort_values(["ticker", "date"], ascending=[True, False])
    output.note(f"data: last {days} saved days to {all_days[-1]}; {len(df)} rows. A missing day means under 50k shares or outside that day's source.")
    cols = show_cols(df, show, ["date", "ticker", "price", "chg", "gap", "open_chg", "high_vs_open", "relvol", "vol", "float_m",
                                "rotation", "shape", "catalyst", "yesterday", "streak", "days_since_split", "news"])
    output.emit(df[cols], ctx.obj["fmt"])


@cli.command()
@click.option("--days", type=click.IntRange(min=1), default=20, show_default=True, help="Sessions to look back from the last saved day.")
@click.pass_context
def splits(ctx, days):
    """Reverse splits found in the saved days, newest first."""
    all_days = store.trading_days()
    if not all_days:
        raise click.ClickException("no saved days yet (see: finviz history status)")
    last = sessions.ordinal(sessions.as_date(all_days[-1]))
    ev = store.split_index()[0]
    ev = ev[ev.di > last - days].sort_values(["di", "ticker"], ascending=[False, True])
    output.note(f"data: the {days} sessions to {all_days[-1]}; {len(ev)} reverse splits. exact=false means the day is only known to within a gap in the data.")
    ev = ev.assign(ratio=ev.ratio.astype(float).round(1)).drop(columns=["di"])
    output.emit(ev, ctx.obj["fmt"])


# ---------------------------------------------------------------- pulling and storing

@cli.command()
@click.option("--columns", "cols", help="Preset (wide, core, intraday, all) or ids/keys, comma separated. Default: wide.")
@click.option("--filters", default=WHOLE_MARKET, help="Finviz filter codes, e.g. sh_curvol_o50,sh_float_u100. Default: none (every stock).")
@click.pass_context
def snapshot(ctx, cols, filters):
    """Pull the screener now and keep it.

    A whole-market pull with the default columns, taken on a trading day five minutes or more
    after its close (16:05 ET, 13:05 on an early close), is also filed as that day's record.
    Exits 3 when the pull failed its checks.
    """
    got = pull_today(cols, filters)
    output.note(f"saved {got['rows']} rows for the {got['session']} session: {got['path']}" + _filing_note(got)
                + (f"; REJECTED, it will not be used or filed: {got['refused']}" if got["refused"] else ""))
    output.emit(pd.DataFrame([{"session": got["session"], "pulled_at": f"{got['pulled']:%Y-%m-%d %H:%M}", "rows": got["rows"],
                               "snapshot": str(got["path"]), "filed_as_day": got["filed"], "rejected_because": got["refused"] or ""}]), ctx.obj["fmt"], 100)
    if got["refused"]:
        ctx.exit(3)


@cli.group()
def history():
    """The saved days."""


@history.command("status")
@click.pass_context
def history_status(ctx):
    """How many days are saved, from which source."""
    s = store.status()
    output.emit(pd.DataFrame([{**s, "by_source": ", ".join(f"{k} {v}" for k, v in s["by_source"].items()),
                               "closed_days": ", ".join(s["closed_days"])}]), ctx.obj["fmt"], 100)


@history.command("import-lfr")
@click.argument("folder", type=click.Path(exists=True, file_okay=False))
@click.pass_context
def history_import_lfr(ctx, folder):
    """Copy end-of-day files saved by a separate collector (<date>.json) from FOLDER into the store.

    A file is taken only when it is named for its own date, was captured at or after that
    session's close, and holds complete stock rows. Exits 3 when any file was rejected.
    """
    got = store.import_lfr(Path(folder))
    days = store.trading_days()
    output.note(f"added {got['added']}, unchanged {got['unchanged']}, rejected {len(got['rejected'])}; {len(days)} trading days saved"
                + (f", {days[0]} .. {days[-1]}" if days else ""))
    for name, why in got["rejected"]:
        output.note(f"rejected {name}: {why}")
    output.emit(pd.DataFrame([{"added": got["added"], "unchanged": got["unchanged"], "rejected": len(got["rejected"]),
                               "trading_days": len(days), "rejected_files": "; ".join(f"{n}: {w}" for n, w in got["rejected"])}]),
                ctx.obj["fmt"], 100)
    if got["rejected"]:
        ctx.exit(3)


# ---------------------------------------------------------------- other Finviz exports

@cli.command()
@click.argument("symbol")
@click.option("--tf", default="d", show_default=True, type=click.Choice(endpoints.BAR_TIMEFRAMES), help="Bar size: i1..i30 minutes, h, d, w, m.")
@click.option("--range", "span", default="", type=click.Choice(("",) + endpoints.BAR_RANGES), help="How far back: d1, d5, m1, m3, m6, ytd, y1, y2, y5, max.")
@click.pass_context
def bars(ctx, symbol, tf, span):
    """Price history for one ticker."""
    df = endpoints.bars(Client(), symbol, tf, span)
    # The span is the earliest and the latest bar, not the first and the last row: nothing promises their order.
    output.note(f"{symbol.upper()} {tf} bars: {len(df)} rows" + (f", {df.date.min()} .. {df.date.max()}" if len(df) else ""))
    output.emit(df, ctx.obj["fmt"])


DAILY_RANGE = "m3"             # the daily bars asked for beside the minute bars: further back than any of those reach


@cli.command("intraday")
@click.argument("symbols", nargs=-1, required=True)
@click.option("--tf", default="i1", show_default=True, type=click.Choice(intraday.TIMEFRAMES),
              help="Bar size. One- and three-minute bars reach about ten sessions back, five-minute bars about fifteen.")
@click.option("--days", type=click.IntRange(min=1), help="Only the newest sessions, this many per ticker (default: every session the bars hold).")
@click.option("--show", help="Columns to print, comma separated, or 'all'.")
@click.pass_context
def intraday_cmd(ctx, symbols, tf, days, show):
    """Each session of one or more tickers, read from minute bars, newest first.

    The premarket high, the open, the high and the low with their times, the close, how much
    of the run was kept, and when the premarket high was passed. Times are Eastern. `vol` is
    the whole day's volume from the daily bar: the minute bars carry only part of it.
    """
    wanted = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
    if not wanted:                                    # an argument was given, but it was blank (an empty shell variable, say)
        raise click.UsageError("no ticker given")
    cols = show_cols(pd.DataFrame(columns=intraday.COLUMNS), show, intraday.DEFAULT_SHOW)     # a column that is not there is said before anything is asked for
    client = Client()
    parts = []
    for sym in wanted:
        try:
            asked = config.now_et()
            minute_bars = endpoints.bars(client, sym, tf)
            # Whether a session is still trading is judged as of when these bars were asked
            # for, not as of whenever the second request comes back.
            as_of = _span(client, asked, config.now_et())[0]
            daily = endpoints.bars(client, sym, "d", DAILY_RANGE)
        except FinvizError as exc:
            raise FinvizError(f"{sym}: {exc}") from None
        table, left_out = intraday.summarize(minute_bars, sym, daily, as_of)
        output.note(f"{sym} {tf} bars: {len(minute_bars)} rows"
                    + (f", {minute_bars.date.min()} .. {minute_bars.date.max()}" if len(minute_bars) else "")
                    + f"; {len(table)} sessions"
                    + (f"; {left_out} bars dated on days that are not sessions were left out" if left_out else ""))
        for row in table[table.live].itertuples():
            output.note(f"{sym}: the {row.date} session is still trading; its close is the last price, at {row.last_at} ET"
                        if row.bars else f"{sym}: the {row.date} session has not traded yet; only its premarket is shown")
        parts.append((table.tail(days) if days else table).iloc[::-1])
    parts = [p for p in parts if len(p)]
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=intraday.COLUMNS)
    output.note("vol is the whole day from the daily bar. The minute bars carry only part of the volume (bar_cover is how much; "
                "less of it before the open than in the session), so the bar_ columns are a rough guide to where in the day "
                "it came, not a count of how much there was.")
    output.emit(df[cols], ctx.obj["fmt"])


@cli.command()
@click.option("--from", "date_from", callback=iso_date, help="First day, YYYY-MM-DD (default: today).")
@click.option("--to", "date_to", callback=iso_date, help="Last day, YYYY-MM-DD.")
@click.option("--save", is_flag=True, help="Merge the rows into the store, so saved days get an 'earnings' catalyst.")
@click.option("--sync", is_flag=True, help="Fill the store from the session before the first saved day to two weeks ahead.")
@click.option("--sort", default="", help=f"Order: {', '.join(endpoints.EARNINGS_SORTS)}. Write --sort=-marketCap for descending.")
@click.pass_context
def earnings(ctx, date_from, date_to, save, sync, sort):
    """The earnings calendar, with estimates, actuals, surprises and the one-day reaction."""
    today = config.now_et().date()
    if sync:
        days = store.trading_days()
        # From the session BEFORE the first saved day: a day's earnings window opens at the
        # previous close, so a report that evening belongs to the first saved day.
        first = store.previous_close(days[0]).date() if days else today - dt.timedelta(days=90)
        date_from = date_from or first.isoformat()
        date_to = date_to or (today + dt.timedelta(days=14)).isoformat()
        save = True
    date_from = date_from or today.isoformat()
    date_to = date_to or date_from
    if date_to < date_from:
        raise click.UsageError("--to is before --from")
    df = endpoints.earnings(Client(), date_from, date_to, sort)
    output.note(f"earnings {date_from} .. {date_to}: {len(df)} rows")
    total = None
    if save:
        total = store.save_earnings(df, date_from, date_to)
        output.note(f"store now holds {total} earnings rows")
    if sync:                                   # a sync reports what it did; the rows themselves are in the store
        output.emit(pd.DataFrame([{"from": date_from, "to": date_to, "rows_pulled": len(df), "rows_in_store": total}]), ctx.obj["fmt"])
    else:
        output.emit(df, ctx.obj["fmt"])


@cli.command()
@click.argument("symbols", nargs=-1)
@click.option("--feed", default="stocks", show_default=True, type=click.Choice(list(endpoints.NEWS_FEEDS)))
@click.option("--only", type=click.Choice(list(endpoints.NEWS_KINDS)), help="Market feed: news only, or blogs only.")
@click.option("--by-source", is_flag=True, help="Market feed: ordered by source instead of by time.")
@click.pass_context
def news(ctx, symbols, feed, only, by_source):
    """Recent headlines, for the given tickers or the whole feed (100 rows at most)."""
    output.emit(endpoints.news(Client(), list(symbols), feed, only or "", by_source), ctx.obj["fmt"], 90)


@cli.command("short-interest")
@click.argument("symbol")
@click.pass_context
def short_interest(ctx, symbol):
    """Short interest by settlement date, with the float on each date."""
    output.emit(endpoints.short_interest(Client(), symbol), ctx.obj["fmt"])


@cli.command()
@click.argument("symbol")
@click.option("--group", default="", type=click.Choice(("",) + endpoints.FILING_GROUPS), help="Kind of filing.")
@click.pass_context
def filings(ctx, symbol, group):
    """SEC filings for one ticker, newest first."""
    output.emit(endpoints.filings(Client(), symbol, group), ctx.obj["fmt"], 70)


@cli.command()
@click.argument("symbol", required=False, default="")
@click.option("--kind", default="all", show_default=True, type=click.Choice(["all", "buy", "sale", "option"]))
@click.option("--min-value", type=click.IntRange(min=0), help="Smallest transaction, in dollars.")
@click.option("--owner-cik", default="", help="Only this owner, by SEC CIK number.")
@click.option("--ten-percent", type=click.Choice(list(endpoints.TEN_PERCENT)), help="Only owners of 10% or more, or everyone but them.")
@click.option("--sort", default="", help=f"Order: {', '.join(endpoints.INSIDER_SORTS)}. Write --sort=-transactionValue for descending.")
@click.pass_context
def insiders(ctx, symbol, kind, min_value, owner_cik, ten_percent, sort):
    """Insider transactions, 200 rows at most. Without a ticker or an owner Finviz returns the last 7 days."""
    output.emit(endpoints.insiders(Client(), symbol, kind, min_value, owner_cik, ten_percent or "", sort), ctx.obj["fmt"], 40)


@cli.command()
@click.option("--by", default="sector", show_default=True, help="sector, industry, country, capitalization, or an industry group.")
@click.pass_context
def groups(ctx, by):
    """Sector or industry totals."""
    output.emit(endpoints.groups(Client(), by), ctx.obj["fmt"])


@cli.command()
@click.argument("symbol")
@click.option("--expiry", callback=iso_date, help="Only this expiry, YYYY-MM-DD.")
@click.option("--strike", type=click.FloatRange(min=0, min_open=True), help="Only this strike.")
@click.option("--type", "kind", type=click.Choice(["call", "put"]), help="Only calls, or only puts.")
@click.option("--show", help="Columns to print, comma separated, or 'all'.")
@click.pass_context
def options(ctx, symbol, expiry, strike, kind, show):
    """The options chain of one ticker: each contract's bid, ask, volume, open interest,
    implied volatility (1.0 is 100%) and Greeks."""
    df = endpoints.options(Client(), symbol, expiry or "", strike)
    if kind:
        df = df[df["type"].astype(str).str.lower() == kind]
    output.note(f"{symbol.upper()} options: {len(df)} contracts"
                + (f", {df.expiry.nunique()} expiries {df.expiry.min()} .. {df.expiry.max()}" if len(df) else ""))
    output.emit(df[show_cols(df, show, list(df.columns))], ctx.obj["fmt"], 30)


@cli.command()
@click.argument("symbol")
@click.option("--statement", default="income", show_default=True, type=click.Choice(["income", "balance", "cash"]),
              help="Income statement, balance sheet or cash flow.")
@click.option("--quarterly", is_flag=True, help="Quarters instead of years.")
@click.option("--reuters", is_flag=True, help="Reuters as the source instead of FactSet.")
@click.option("--oldest-first", is_flag=True, help="Oldest period first.")
@click.option("--growth", default="", help=f"Rows to add under each line, comma separated: {', '.join(endpoints.GROWTH)}.")
@click.pass_context
def statements(ctx, symbol, statement, quarterly, reuters, oldest_first, growth):
    """A financial statement of one ticker: one row per line item, one column per period.

    Amounts are whole units of the currency in the `currency` column.
    """
    rows = tuple(g.strip() for g in growth.split(",") if g.strip())
    df = endpoints.statements(Client(), symbol, statement, quarterly, reuters, oldest_first, rows)
    currency = df["currency"].iloc[0] if len(df) else ""
    output.note(f"{symbol.upper()} {statement} statement, {'quarterly' if quarterly else 'annual'}: {len(df)} lines, "
                f"{len(df.columns) - 3} periods" + (f", amounts in {currency}" if currency else "; Finviz did not say which currency"))
    output.emit(df, ctx.obj["fmt"], 50)


@cli.command()
@click.option("--from", "date_from", callback=iso_date, help="A day of the first week, YYYY-MM-DD (default: today). Finviz starts at that week's Monday.")
@click.option("--to", "date_to", callback=iso_date, help="Last day (default: the end of that week). At most 365 days.")
@click.pass_context
def economic(ctx, date_from, date_to):
    """The economic calendar: releases and speeches with their time, impact, and the actual,
    expected and prior figures."""
    date_from = date_from or config.now_et().date().isoformat()
    df = endpoints.calendar(Client(), "economic", date_from, date_to or "")
    # The rows say what came back: Finviz starts at the Monday of the first day's week, whatever day was asked for.
    span = f"{df['date'].min()} .. {df['date'].max()}" if len(df) else (f"{date_from} .. {date_to}" if date_to else f"the week of {date_from}")
    output.note(f"economic calendar {span}: {len(df)} events (Finviz starts at the Monday of the first day's week)")
    output.emit(df, ctx.obj["fmt"], 60)


@cli.command()
@click.option("--from", "date_from", callback=iso_date, help="First ex-date, YYYY-MM-DD (default: today).")
@click.option("--to", "date_to", callback=iso_date, help="Last ex-date (default: the same day). At most 90 days.")
@click.option("--sort", default="", help=f"Order: {', '.join(endpoints.DIVIDEND_SORTS)}. Write --sort=-yield for descending.")
@click.pass_context
def dividends(ctx, date_from, date_to, sort):
    """The dividend calendar by ex-date: amount, special dividend and estimated yield."""
    date_from = date_from or config.now_et().date().isoformat()
    df = endpoints.calendar(Client(), "dividends", date_from, date_to or "", sort)
    output.note(f"dividends {date_from} .. {date_to or date_from}: {len(df)} rows")
    output.emit(df, ctx.obj["fmt"], 50)


MARKET_SORTS = click.Choice([*endpoints.PERFORMANCE_SORTS, *endpoints.PLAIN_SORTS])
MARKET_SORT_HELP = "A timeframe (best first), or ticker, name, price."


def _market(ctx, market: str, sort: str, reverse: bool, pips: bool = False, quote: str = "") -> None:
    df = endpoints.performance(Client(), market, sort, reverse, pips, quote)
    output.note(f"{market}: {len(df)} rows, " + ("in pips" if pips else "percent change")
                + (f", quoted in {quote.upper()}" if quote else ""))
    output.emit(df, ctx.obj["fmt"], 40)


@cli.command()
@click.option("--sort", default="day", show_default=True, type=MARKET_SORTS, help=MARKET_SORT_HELP)
@click.option("--reverse", is_flag=True, help="Turn the order round.")
@click.pass_context
def futures(ctx, sort, reverse):
    """Futures: each contract's change from five minutes to a year."""
    _market(ctx, "futures", sort, reverse)


@cli.command()
@click.option("--sort", default="day", show_default=True, type=MARKET_SORTS, help=MARKET_SORT_HELP)
@click.option("--reverse", is_flag=True, help="Turn the order round.")
@click.option("--pips", is_flag=True, help="Changes in pips instead of percent.")
@click.pass_context
def forex(ctx, sort, reverse, pips):
    """Currency pairs: each pair's change from five minutes to a year."""
    _market(ctx, "forex", sort, reverse, pips=pips)


@cli.command()
@click.option("--sort", default="day", show_default=True, type=MARKET_SORTS, help=MARKET_SORT_HELP)
@click.option("--reverse", is_flag=True, help="Turn the order round.")
@click.option("--quote", default="", type=click.Choice(["", *endpoints.CRYPTO_QUOTES], case_sensitive=False), help="Price them in this currency (default USD).")
@click.pass_context
def crypto(ctx, sort, reverse, quote):
    """Crypto: each coin's change from five minutes to a year."""
    _market(ctx, "crypto", sort, reverse, quote=quote)


LIST_SORT_HELP = f"Order: {', '.join(endpoints.LIST_SORTS)}. Write --sort=-portfolio_value for descending."


@cli.command()
@click.argument("search", required=False, default="")
@click.option("--sort", default="", help=LIST_SORT_HELP)
@click.pass_context
def managers(ctx, search, sort):
    """Institutional managers (13F filers), 500 at most: narrow it with part of a name.

    The `investor_id` column is what `finviz holdings` takes.
    """
    df = endpoints.holders(Client(), "managers", search, sort)
    output.note(f"managers{f' matching {search!r}' if search else ''}: {len(df)} rows" + (" (the most Finviz gives; narrow the search)" if len(df) >= 500 else ""))
    output.emit(df, ctx.obj["fmt"], 40)


@cli.command()
@click.argument("search", required=False, default="")
@click.option("--sort", default="", help=LIST_SORT_HELP)
@click.pass_context
def funds(ctx, search, sort):
    """Funds, 500 at most: narrow it with part of a fund or series name.

    The `investor_id` column (S and digits) is what `finviz holdings` takes.
    """
    df = endpoints.holders(Client(), "funds", search, sort)
    output.note(f"funds{f' matching {search!r}' if search else ''}: {len(df)} rows" + (" (the most Finviz gives; narrow the search)" if len(df) >= 500 else ""))
    output.emit(df, ctx.obj["fmt"], 40)


@cli.command()
@click.argument("holder")
@click.option("--report-date", callback=iso_date, help="The quarter-end report to read, YYYY-MM-DD (default: the latest).")
@click.option("--change", type=click.Choice(endpoints.HOLDING_CHANGES), help="Only positions that are new, added to, reduced, closed out or unchanged.")
@click.option("--option-type", type=click.Choice(endpoints.OPTION_TYPES), help="A manager's shares, puts or calls only.")
@click.option("--asset-category", default="", help="A fund's holdings of one SEC asset category, e.g. EC (common equity), DBT (debt).")
@click.option("--sort", default="", help="Finviz's sort key, e.g. --sort=-valueUsd (the default), issuerTicker, shares, percentageOfPortfolio.")
@click.pass_context
def holdings(ctx, holder, report_date, change, option_type, asset_category, sort):
    """What one manager or one fund holds.

    HOLDER is an investor id from `finviz managers` (digits), a series id from `finviz funds`
    (S and digits), or the name-and-id slug from the site's address bar.
    """
    df = endpoints.holdings(Client(), holder, report_date or "", change or "", option_type or "", asset_category, sort)
    output.note(f"holdings of {holder.strip()}: {len(df)} positions")
    output.emit(df, ctx.obj["fmt"], 40)


@cli.command("export")
@click.argument("path")
@click.argument("params", nargs=-1)
@click.option("--raw", is_flag=True, help="Print the CSV exactly as Finviz sent it.")
@click.pass_context
def export_cmd(ctx, path, params, raw):
    """Any export endpoint by path, for the ones without a command of their own.

    \b
    finviz export screener type=etf v=152 c=1,65,66
    finviz export stock t=MSFT ty=oc
    finviz export calendar/economic dateFrom=2026-11-09
    """
    pairs = {}
    for item in params:
        if "=" not in item:
            raise click.ClickException(f"parameters look like key=value, got '{item}'")
        k, v = item.split("=", 1)
        pairs[k] = v
    if raw and ctx.obj["fmt"] != "auto":
        raise click.UsageError("--raw prints Finviz's own CSV; do not combine it with --format")
    text = Client().export(path, pairs)
    if raw:
        sys.stdout.write(text)
    else:
        output.emit(endpoints.generic(text), ctx.obj["fmt"])


# ---------------------------------------------------------------- reference

@cli.command("columns")
@click.option("--preset", type=click.Choice(sorted(C.PRESETS)), help="Only the columns of a preset.")
@click.option("--markdown", is_flag=True, help="Print the table as Markdown (this is how docs/columns.md is made).")
@click.option("--screen", "screening", is_flag=True,
              help="The columns `finviz screen` filters and returns: Finviz's own that a pull keeps, and the ones this CLI adds.")
@click.pass_context
def columns_cmd(ctx, preset, markdown, screening):
    """Screener columns: Finviz id, this CLI's key, Finviz's header.

    Finviz exports more columns than a pull keeps. --screen lists the ones a filter can
    name: what each is called, whether it holds a number, text or true/false, and what it means.
    """
    if screening:
        if preset or markdown:
            raise click.UsageError("--screen stands alone; it is not combined with --preset or --markdown")
        output.emit(pd.DataFrame(C.screen_table()), ctx.obj["fmt"], 110)
        return
    rows = [r for r in C.table() if not preset or r["id"] in C.PRESETS[preset]]
    if markdown and ctx.obj["fmt"] != "auto":
        raise click.UsageError("--markdown is a format of its own; do not combine it with --format")
    if markdown:
        click.echo("| id | key | Finviz header | kind | ETF only | unit |")
        click.echo("|---|---|---|---|---|---|")
        for r in rows:
            click.echo(f"| {r['id']} | `{r['key']}` | {r['header']} | {r['kind']} | {'yes' if r['etf_only'] else ''} | {r['unit']} |")
        return
    output.emit(pd.DataFrame(rows), ctx.obj["fmt"])


@cli.command()
@click.pass_context
def doctor(ctx):
    """Check the token, the connection and the store. Prints no secrets."""
    checks = []
    try:
        client = Client()
        text = endpoints.screener_csv(client, cols=[1, 65], tickers=["AAPL"])
        ok = text.strip().splitlines()[0].startswith("Ticker") or "Ticker" in text[:60]
        checks.append({"check": "token and connection", "ok": bool(ok), "detail": "screener export answered" if ok else "unexpected answer"})
    except FinvizError as exc:
        checks.append({"check": "token and connection", "ok": False, "detail": redact(exc)})
    s = store.status()
    checks.append({"check": "store", "ok": s["trading_days"] > 0, "detail": f"{s['trading_days']} trading days in {s['home']}"})
    checks.append({"check": "earnings calendar", "ok": s["earnings_rows"] > 0, "detail": f"{s['earnings_rows']} rows"})
    output.emit(pd.DataFrame(checks), ctx.obj["fmt"], 90)
    if not all(c["ok"] for c in checks[:1]):
        ctx.exit(2)


def _set_format(ctx, _param, value):
    if value:
        ctx.ensure_object(dict)["fmt"] = value
    return value


def _add_format_option(group: click.Group) -> None:
    """--format works after the command name as well as before it."""
    for cmd in group.commands.values():
        if isinstance(cmd, click.Group):
            _add_format_option(cmd)
        else:
            cmd.params.append(click.Option(["--format", "fmt_local"], type=click.Choice(output.FORMATS), callback=_set_format,
                                           expose_value=False, help="table, ndjson, json or csv."))


_add_format_option(cli)


def _one_line(text: object) -> str:
    """An error as one physical line, with the token taken out: whatever the message holds
    (an argument with a line break in it, a library's multi-line text) is folded flat."""
    return " ".join(redact(text).split())


def main(argv: list[str] | None = None) -> None:
    """Entry point. Every failure leaves as one redacted line and a non-zero exit code."""
    code = 0
    try:
        code = cli(args=argv, standalone_mode=False)
    except click.exceptions.Abort:
        sys.exit(130)
    except click.exceptions.Exit as exc:
        sys.exit(exc.exit_code)
    except click.ClickException as exc:
        click.echo(f"error: {_one_line(exc.format_message())}", err=True)
        sys.exit(exc.exit_code)
    except FinvizError as exc:
        click.echo(f"error: {_one_line(exc)}", err=True)
        sys.exit(2)
    except Exception as exc:              # never a raw traceback: it is not redacted
        click.echo(f"error: {type(exc).__name__}: {_one_line(exc)}", err=True)
        if os.environ.get("FINVIZ_CLI_DEBUG"):
            import traceback
            click.echo(redact(traceback.format_exc()), err=True)
        sys.exit(1)
    sys.exit(code if isinstance(code, int) else 0)


if __name__ == "__main__":
    main()
