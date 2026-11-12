"""The commands as functions a program can call: arguments in, rows out.

This is the surface a language model works through (the MCP server in mcp_server.py is a
thin wrapper round it). Three rules shape it:

- It runs the same commands as the command line, so an answer here is the answer there.
- Every result has the same shape: `ok`, `rows`, `row_count`, `notes`, and `error` or
  `truncated` when they apply. A failure is a result that says what went wrong, in words the
  caller can act on, never a traceback.
- It is frugal. Rows are capped, empty fields are left out, and the docstrings below say
  which tool answers which question, because each of them is read by the model on every turn.
"""
from __future__ import annotations

from typing import Literal

import click

from finviz_cli import cli as cli_mod
from finviz_cli import output
from finviz_cli.client import FinvizError

MAX_ROWS = 500

GUIDE = """finviz: Finviz Elite market data, with a local history of trading days.

How to work with it:
- Start with the narrowest tool that answers the question. `screen` filters the whole US stock market on any column;
  `ticker_history`, `intraday`, `news` and the rest answer about named tickers.
- Every result is {ok, rows, row_count, notes}. Read `notes`: they say what day and time the data is from. If `truncated`
  is present, narrow the request rather than raising the limit. If `ok` is false, `error` says why; fix the call, do not retry it unchanged.
- Times are US Eastern. Percentages are plain numbers (12.5 means 12.5%). Volume is in shares; market cap and float are in millions.
- Before 09:30 ET the price columns of `screen` hold premarket values; after 16:00 they hold the close.
- `screen` filters are expressions over column names: `gap >= 5 and relvol > 2`, `news.str.contains('offering', case=False)`.
  Use `list_columns` to find a column's name. A filter can only compare and combine columns; it cannot call anything else.
  Compare like with like: text in quotes (`yesterday == 'mover, weak close'`), numbers as numbers, true/false columns on their
  own (`mover`). Test for a missing value with `.isna()` or `.notna()` (`float_m.notna()`), never by comparing with None.
  A field left out of a row is missing: for text that is `news.isna()` (or `news == ''`), whichever way it came to be empty.
- The labels (`shape`, `mover`, `close_kind`, `catalyst`, `yesterday`, `streak`) describe what happened. They are not forecasts.
- Finviz allows 10 requests per 5 seconds for the whole account. One `screen` call serves many questions; prefer it to a call per ticker.
- Nothing here gives investment advice or places trades.
"""


def call(args: list, limit: int = 50, newest: bool = False, order_by: str = "", fixed: bool = False) -> dict:
    """Run one command and return its rows. `newest` keeps the last rows instead of the first
    when there are more than `limit` (for tables that run oldest first); `order_by` puts the
    rows in that column's order first, so that "the last rows" are the newest whatever order
    they arrived in. `fixed` says the caller cannot raise the limit, so the note about cut
    rows does not suggest it."""
    try:
        limit = max(1, min(int(limit), MAX_ROWS))
    except (TypeError, ValueError, OverflowError):
        limit = 50
    error, code = "", 0
    with output.capture() as got:
        try:
            returned = cli_mod.cli(args=[str(a) for a in args], standalone_mode=False, prog_name="finviz")
            code = returned if isinstance(returned, int) else 0          # a command that ends with ctx.exit(n) comes back as n
        except click.exceptions.Abort:
            code, error = 130, "interrupted"
        except click.ClickException as exc:
            code, error = exc.exit_code, cli_mod._one_line(exc.format_message())
        except FinvizError as exc:
            code, error = 2, cli_mod._one_line(exc)
        except Exception as exc:                                         # never a traceback, and never unredacted
            code, error = 1, f"{type(exc).__name__}: {cli_mod._one_line(exc)}"
    rows = [{k: v for k, v in row.items() if v is not None and v != ""} for row in got.rows]
    if order_by:
        rows.sort(key=lambda row: str(row.get(order_by, "")))            # stable; ISO dates sort as dates
    shown = rows[-limit:] if newest else rows[:limit]
    result: dict = {"ok": code == 0, "rows": shown, "row_count": len(rows), "notes": got.notes}
    if len(shown) < len(rows):
        result["truncated"] = (f"{len(shown)} of {len(rows)} rows shown ({'the newest' if newest else 'the first'}); narrow the request"
                               + ("" if fixed else f", or raise limit (at most {MAX_ROWS})"))
    if error:
        result["error"] = error
    elif code:
        result["error"] = f"the command ended with exit code {code}; see notes"
    return result


def _symbols(symbols: list[str] | str) -> list[str]:
    """Tickers, however they were handed over: a list, or one string with commas or spaces."""
    items = symbols.replace(",", " ").split() if isinstance(symbols, str) else [str(s) for s in symbols]
    return [s.strip() for s in items if s.strip()]


def _command(name: str, *options: str, values=()) -> list[str]:
    """The arguments of one command: its options, then `--`, then the values the caller gave
    (a filter, tickers, a name to search for). After `--` nothing is read as an option, so a
    value such as "--help" or "--refresh" is only ever a value: text from a model cannot turn
    into a switch, and cannot make the command print its help where the protocol is."""
    values = [str(v) for v in values if str(v).strip()]
    return [*name.split(), *options, *(["--", *values] if values else [])]


def _refused(why: str) -> dict:
    return {"ok": False, "rows": [], "row_count": 0, "notes": [], "error": why}


def _opt(flag: str, value) -> list[str]:
    """`--flag=value` when there is a value. Written as one argument, so a value that starts
    with a dash (a descending sort key) stays a value."""
    return [] if value is None or value == "" else [f"{flag}={value}"]


# ---------------------------------------------------------------- the whole market

def screen(filter: str = "", date: str = "", sort: str = "", ascending: bool = False, columns: list[str] | None = None,
           limit: int = 25) -> dict:
    """Filter every US-listed stock by any expression over the columns. The main tool.

    filter: e.g. "gap >= 5 and relvol > 2 and float_m < 20", "news.str.contains('offering', case=False)",
      "yesterday == 'mover, weak close'". Empty means every stock. Use list_columns to find column names.
      Compare like with like (text in quotes, numbers as numbers); a missing value is tested with
      float_m.isna() or float_m.notna(), not by comparing with None; a field left out of a row is missing (news.isna()).
      A filter that cannot be answered is refused, with the reason.
    date: a saved trading day YYYY-MM-DD; empty means now (premarket values before 09:30 ET, the close after 16:00).
    sort: a column to sort by (default: change from the open, largest first). ascending: smallest first.
    columns: which columns to return (default: a compact set with price, change, gap, volume, float, shape, catalyst).
    limit: rows to return (default 25).
    """
    options = _opt("--date", date) + _opt("--sort", sort) + (["--asc"] if ascending else []) + ["--top=0"] + _opt("--show", ",".join(columns or []))
    return call(_command("screen", *options, values=[filter]), limit)


def movers(date: str = "", limit: int = 50) -> dict:
    """The day's movers (price $1+, $1M+ traded, twice normal volume, a 10% up-move), with how each closed.
    date: a saved trading day YYYY-MM-DD; empty means today's latest data. Strong closers first."""
    return call(_command("movers", *_opt("--date", date)), limit)


def list_columns(contains: str = "") -> dict:
    """The columns a screen filter can name and its results can carry: the name, whether it holds a number, text or
    true/false, whether it is Finviz's own or worked out here, Finviz's header for it, and what it means.
    contains: only columns whose name, header or meaning contains this text (e.g. "float", "split", "short")."""
    found = call(_command("columns", "--screen"), MAX_ROWS)
    if contains.strip() and found["ok"]:
        wanted = contains.strip().lower()
        found["rows"] = [r for r in found["rows"] if any(wanted in str(v).lower() for v in r.values())]
        found["row_count"] = len(found["rows"])
    return found


def store_status() -> dict:
    """Which trading days are saved locally (the range `screen` can reach back with `date`, and that `ticker_history` reads)."""
    return call(_command("history status"), 5)


# ---------------------------------------------------------------- named tickers

def ticker_history(symbols: list[str], days: int = 10) -> dict:
    """One or more tickers across the saved trading days, newest first: price, change, gap, volume, float rotation,
    the day's shape, catalyst, what it did the session before, mover days in the last ten, sessions since a reverse split."""
    return call(_command("ticker", f"--days={days}", values=_symbols(symbols)), 200, fixed=True)


def intraday(symbols: list[str], days: int = 5, bar: Literal["i1", "i3", "i5"] = "i1") -> dict:
    """What happened inside each recent session, from minute bars: premarket high and its time, open, high and low with
    their times, close, how much of the run above the open was kept, when the premarket high was passed, the day's volume.
    days: sessions per ticker, newest first (one-minute bars reach about ten sessions back). Two requests per ticker."""
    return call(_command("intraday", f"--tf={bar}", f"--days={days}", values=_symbols(symbols)), 200, fixed=True)


def price_bars(symbol: str, bar: Literal["i1", "i3", "i5", "i15", "i30", "h", "d", "w", "m"] = "d",
               range: Literal["d1", "d5", "m1", "m3", "m6", "ytd", "y1", "y2", "y5", "max"] = "m1", limit: int = 120) -> dict:
    """Price history for one ticker: date, open, high, low, close, volume. Returns the newest `limit` bars, oldest first.
    bar: i1..i30 minutes (i1, i3, i5 include premarket and after hours), h hourly, d daily, w weekly, m monthly."""
    return call(_command("bars", f"--tf={bar}", f"--range={range}", values=[symbol]), limit, newest=True, order_by="date")


def news(symbols: list[str], limit: int = 20) -> dict:
    """Recent headlines for the given tickers, newest first: title, source, time, link. Finviz gives 100 rows at most
    for the whole list, so ask for few tickers at a time."""
    chosen = _symbols(symbols)
    if not chosen:                                               # with no ticker the command would fetch the whole feed
        return _refused("give at least one ticker (market_news is the feed for the whole market)")
    return call(_command("news", values=chosen), limit)


def market_news(only: Literal["", "news", "blogs"] = "", limit: int = 30) -> dict:
    """General market headlines, newest first. only: "news" or "blogs" to leave the other out."""
    return call(_command("news", "--feed=market", *_opt("--only", only)), limit)


def short_interest(symbol: str, limit: int = 12) -> dict:
    """Short interest by settlement date, newest first, with the float and average volume on each date."""
    return call(_command("short-interest", values=[symbol]), limit)


def sec_filings(symbol: str, group: str = "", limit: int = 25) -> dict:
    """SEC filings for one ticker, newest first: dates, form, description, links.
    group: one of annual-quarterly-current, insider-equity, beneficial-ownership, exempt-offerings,
    registration-statements (offerings), proxy-materials, tender-offers; empty for all."""
    return call(_command("filings", *_opt("--group", group), values=[symbol]), limit)


def insider_trades(symbol: str = "", kind: Literal["all", "buy", "sale", "option"] = "all", min_value: int = 0,
                   limit: int = 50) -> dict:
    """Insider transactions, newest first. symbol: one ticker; empty means every ticker over the last 7 days.
    min_value: smallest transaction in dollars."""
    return call(_command("insiders", f"--kind={kind}", *(_opt("--min-value", min_value) if min_value else []), values=[symbol]), limit)


def options_chain(symbol: str, expiry: str = "", strike: float = 0, type: Literal["", "call", "put"] = "", limit: int = 100) -> dict:
    """The options chain of one ticker: bid, ask, volume, open interest, implied volatility (1.0 is 100%) and Greeks.
    A whole chain is thousands of rows: give expiry (YYYY-MM-DD), strike or type to narrow it."""
    options = _opt("--expiry", expiry) + (_opt("--strike", strike) if strike else []) + _opt("--type", type)
    return call(_command("options", *options, values=[symbol]), limit)


def financial_statement(symbol: str, statement: Literal["income", "balance", "cash"] = "income", quarterly: bool = False) -> dict:
    """A financial statement: one row per line item, one column per period (up to eight), in whole units of the
    currency named in each row's `currency`. quarterly: quarters instead of years."""
    return call(_command("statements", f"--statement={statement}", *(["--quarterly"] if quarterly else []), values=[symbol]), 200, fixed=True)


# ---------------------------------------------------------------- calendars and markets

def earnings_calendar(date_from: str = "", date_to: str = "", sort: str = "", limit: int = 100) -> dict:
    """Companies reporting earnings, with estimates, actuals, surprises and the one-day reaction.
    date_from, date_to: YYYY-MM-DD (default: today). sort: Finviz's key, e.g. "-marketCap", "epsSurprise", "earningsDate"."""
    return call(_command("earnings", *_opt("--from", date_from), *_opt("--to", date_to), *_opt("--sort", sort)), limit)


def economic_calendar(date_from: str = "", date_to: str = "", limit: int = 150) -> dict:
    """Economic releases and central-bank speeches: time, impact (1 to 3), and the actual, expected and prior figures
    as Finviz writes them ("2.6%", "220K"). Without date_to: the whole week of date_from (default: this week).
    Rows start at the Monday of the first week whatever day is asked for."""
    return call(_command("economic", *_opt("--from", date_from), *_opt("--to", date_to)), limit)


def dividend_calendar(date_from: str = "", date_to: str = "", limit: int = 100) -> dict:
    """Stocks going ex-dividend: amount, special dividend, estimated yield. date_from, date_to: YYYY-MM-DD (default: today)."""
    return call(_command("dividends", *_opt("--from", date_from), *_opt("--to", date_to), "--sort=-yield"), limit)


def market_performance(market: Literal["futures", "forex", "crypto"], sort: str = "day", limit: int = 40) -> dict:
    """How futures, currency pairs or coins have moved, from five minutes to a year, best first.
    sort: day, 5min, hour, week, month, mtd, quarter, half, ytd, year, or ticker, name, price."""
    if market not in ("futures", "forex", "crypto"):             # the market is the command itself: only these three
        return _refused("market must be one of futures, forex, crypto")
    return call(_command(market, f"--sort={sort}"), limit)


def sector_groups(by: Literal["sector", "industry", "country", "capitalization"] = "sector", limit: int = 60) -> dict:
    """Totals by sector, industry, country or size: market cap, valuation, change and volume of each group."""
    return call(_command("groups", f"--by={by}"), limit)


def institutions(search: str, kind: Literal["managers", "funds"] = "managers", limit: int = 25) -> dict:
    """Find institutional managers (13F filers) or funds by part of their name. The `investor_id` of a row is what
    institution_holdings takes."""
    if kind not in ("managers", "funds"):                        # likewise
        return _refused("kind must be managers or funds")
    return call(_command(kind, values=[search]), limit)


def institution_holdings(investor_id: str | int, change: Literal["", "new", "added", "reduced", "closed_out", "no_change"] = "",
                         limit: int = 50) -> dict:
    """What one manager or fund holds, largest positions first. investor_id: from `institutions` (digits for a manager,
    S and digits for a fund). change: only positions that are new, added to, reduced, closed out or unchanged."""
    if isinstance(investor_id, bool):
        return _refused("investor_id is the id from `institutions`: digits for a manager, S and digits for a fund")
    return call(_command("holdings", *_opt("--change", change), values=[investor_id]), limit)        # a number or text, as the list gave it


TOOLS = (screen, movers, list_columns, store_status, ticker_history, intraday, price_bars, news, market_news, short_interest,
         sec_filings, insider_trades, options_chain, financial_statement, earnings_calendar, economic_calendar, dividend_calendar,
         market_performance, sector_groups, institutions, institution_holdings)
