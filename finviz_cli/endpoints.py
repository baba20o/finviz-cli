"""One function per Finviz export endpoint. Parameters and limits: docs/api-reference.md."""
from __future__ import annotations

import csv
import datetime as dt
import math
import re

import pandas as pd

from finviz_cli import columns as C
from finviz_cli import frame
from finviz_cli.client import Client, FinvizError

BAR_TIMEFRAMES = ("i1", "i3", "i5", "i15", "i30", "h", "d", "w", "m")
BAR_RANGES = ("d1", "d5", "m1", "m3", "m6", "ytd", "y1", "y2", "y5", "max")
FILING_GROUPS = ("annual-quarterly-current", "insider-equity", "beneficial-ownership", "exempt-offerings",
                 "registration-statements", "filing-review-correspondence", "sec-orders-notices", "proxy-materials",
                 "tender-offers", "trust-indentures")

EARNINGS_KEYS = {"Date": "date", "Ticker": "ticker", "Company": "company", "Market Cap": "mcap_m",
                 "EPS Estimate": "eps_est", "EPS Actual": "eps_actual", "EPS Surprise": "eps_surprise",
                 "EPS GAAP Estimate": "eps_gaap_est", "EPS GAAP Actual": "eps_gaap_actual",
                 "EPS GAAP Surprise": "eps_gaap_surprise", "Revenue Estimate": "rev_est_m", "Revenue Actual": "rev_actual_m",
                 "Revenue Surprise": "rev_surprise", "1-Day Price Reaction": "reaction_1d"}


def screener_csv(client: Client, filters: str = "", cols: list[int] | None = None, order: str = "",
                 kind: str = "stocks", tickers: list[str] | None = None, view: str = "152") -> str:
    """Raw screener export. `kind`: stocks, etf, or '' for both together."""
    params = {"v": view, "f": filters, "o": order, "c": ",".join(str(i) for i in (cols or C.PRESETS["wide"]))}
    if kind:
        params["type"] = kind
    if tickers:
        params["t"] = ",".join(t.upper() for t in tickers)
    return client.export("screener", params)


def generic(text: str, keys: dict[str, str] | None = None, keep_text: tuple[str, ...] = ()) -> pd.DataFrame:
    """Any export as a table: short column names, numbers where a column is numeric.
    `keep_text` names columns (by their short names) that stay as Finviz wrote them: ones
    whose values mix units ("2.6%", "220K", "108.36") and would otherwise be numbers one
    week and text the next."""
    return _typed_table(*frame.parse_table(text), keys, keep_text)


def _typed_table(headers: list[str], rows: list[dict], keys: dict[str, str] | None = None, keep_text: tuple[str, ...] = ()) -> pd.DataFrame:
    """Rows keyed by Finviz's headers as a table with short names, numbers where a column is
    numeric. A column's type is decided over all the rows given, so rows from several requests
    are typed together, not request by request."""
    df = frame.named_columns(pd.DataFrame(rows) if rows else pd.DataFrame(columns=headers))      # no rows still has its columns
    df = df.rename(columns={h: (keys or {}).get(h) or C.slug(h) for h in df.columns})
    df = df.loc[:, ~df.columns.duplicated()]       # two headers that come to the same name: keep the first
    for col in df.columns:
        if col.endswith(("date", "time", "cik", "ticker")) or col in keep_text:
            continue
        raw = df[col].astype(str).str.replace("%", "", regex=False).str.replace(",", "", regex=False).str.strip()
        filled = raw[raw != ""]
        if not len(filled) or filled.str.match(r"^-?0\d").any():       # an id with leading zeros is not a number
            continue
        # Only when every value is a number: a mostly-numeric column (form "4" next to "4/A")
        # stays text rather than losing the odd ones.
        if pd.to_numeric(filled, errors="coerce").notna().all():
            df[col] = pd.to_numeric(raw, errors="coerce")
    return df


def bars(client: Client, ticker: str, timeframe: str = "d", span: str = "") -> pd.DataFrame:
    if timeframe not in BAR_TIMEFRAMES:
        raise FinvizError(f"timeframe must be one of {', '.join(BAR_TIMEFRAMES)}")
    if span and span not in BAR_RANGES:
        raise FinvizError(f"range must be one of {', '.join(BAR_RANGES)}")
    body = client.export("stock", {"t": ticker.upper(), "p": timeframe, "r": span})
    # Every line Finviz sends ends. A last one that does not was cut short, and what is left of
    # its last number would read as a smaller number that looks perfectly good.
    if not body.endswith(("\n", "\r")):
        raise FinvizError(f"the {ticker.upper()} price history did not arrive whole")
    try:
        df = generic(body)
    except csv.Error:                                   # lines that end some other way, a field too long to be one
        raise FinvizError(f"the {ticker.upper()} price history could not be read as a table") from None
    # Bars always have their dates, rows or not. Anything else is some other answer (an error
    # text reads as a table of one column and no rows) and must not be taken for "no bars".
    if "date" not in df.columns:
        raise FinvizError(f"the {ticker.upper()} price history came back without its Date column")
    if "ticker" in df.columns:                          # bars carry no ticker of their own; ours is put in below
        df = df.drop(columns=["ticker"])
    intraday = timeframe.startswith("i") or timeframe == "h"
    text = df["date"].astype(object).where(df["date"].notna(), "").astype(str).str.strip()      # a missing date is "", never NaN
    if intraday:
        # Finviz writes a 24-hour clock AND an AM/PM suffix ("17:25 PM"). Read the clock, and
        # use the suffix only where it adds something: a 12-hour "04:05 PM" becomes 16:05.
        suffix = text.str.extract(r"(AM|PM)\s*$", expand=False)
        clock = text.str.replace(r"\s*(AM|PM)\s*$", "", regex=True)
        when = pd.to_datetime(clock, format="%m/%d/%Y %H:%M", errors="coerce")
        when = when.fillna(pd.to_datetime(clock, format="%m/%d/%Y %H:%M:%S", errors="coerce"))     # with seconds, where sent
        when = when + pd.to_timedelta(((suffix == "PM") & (when.dt.hour < 12)).astype(int) * 12, unit="h")
        when = when - pd.to_timedelta(((suffix == "AM") & (when.dt.hour == 12)).astype(int) * 12, unit="h")
        out_format = "%Y-%m-%d %H:%M"
    else:
        when = pd.to_datetime(text, format="%m/%d/%Y", errors="coerce")
        out_format = "%Y-%m-%d"
    # A bar without a readable time is not a bar that can be used: say so rather than print a blank.
    if when.isna().any():
        bad = text[when.isna()]
        raise FinvizError(f"could not read the dates in the {ticker.upper()} price history "
                          f"({len(bad)} of {len(text)} rows; first: {bad.iloc[0][:30]!r})")
    df["date"] = when.dt.strftime(out_format)
    df.insert(0, "ticker", ticker.upper())
    return df


def earnings(client: Client, date_from: str, date_to: str = "", sort: str = "") -> pd.DataFrame:
    """Earnings calendar. Finviz allows at most 90 days per request; longer spans are asked for
    in pieces, typed together and, when `sort` is given, ranked together."""
    sort = _sort(sort, EARNINGS_SORTS, "the earnings calendar")
    start = dt.date.fromisoformat(_iso(date_from, "the first day"))
    end = dt.date.fromisoformat(_iso(date_to, "the last day")) if date_to else start
    if end < start:
        raise FinvizError("the last day is before the first")
    parts, columns, every_row = [], None, []
    while start <= end:
        stop = min(start + dt.timedelta(days=89), end)
        params = {"dateFrom": start.isoformat(), "dateTo": stop.isoformat() if stop != start else "", "sort": sort}
        text = client.export("calendar/earnings", params)
        # This answer can replace saved rows, so it is read strictly first: quoting that never
        # closes, or a row that does not have the header's columns, is an answer cut short.
        try:
            headers, raw = frame.parse_table(text, strict=True)
        except csv.Error:
            headers, raw = [], None
        whole = text.endswith(("\n", "\r"))                 # every line Finviz sends ends; a last one that does not was cut
        if raw is None or not whole or any(None in r or any(v is None for v in r.values()) for r in raw):
            raise FinvizError(f"the earnings calendar for {start} .. {stop} did not arrive whole")
        part = generic(text, EARNINGS_KEYS)
        # A calendar always has these two, rows or not. Anything else is some other answer
        # (an error text reads as a one-column table) and must not be taken for "no earnings".
        if "date" not in part.columns or "ticker" not in part.columns:
            raise FinvizError(f"the earnings calendar for {start} .. {stop} came back without its Date and Ticker columns")
        # ...and the dates must be the ones asked for. An answer for some other range (a stale
        # or misrouted one) would otherwise empty this range in the store and fill another.
        # (Dates written some other way are left for the store to read; it enforces the range too.)
        days = pd.to_datetime(part["date"].astype(str).str[:10], format="%Y-%m-%d", errors="coerce").dropna()
        if len(days) and (days.min().date() < start or days.max().date() > stop):
            raise FinvizError(f"the earnings calendar for {start} .. {stop} came back with dates outside that range")
        if columns is not None and headers != columns:
            raise FinvizError(f"the earnings calendar for {start} .. {stop} came back with other columns than the days before it")
        columns = headers
        parts.append(part)
        every_row.extend(raw)
        start = stop + dt.timedelta(days=1)
    if len(parts) < 2:
        return parts[0] if parts else pd.DataFrame()
    # Several requests: the rows are typed together (a piece whose figures are all still to
    # come would otherwise hold text where the others hold numbers) and, when an order was
    # asked for, ranked together: each piece came back in order by itself, and end to end
    # they would be several rankings.
    table = _typed_table(columns, every_row, EARNINGS_KEYS)
    column = EARNINGS_SORT_COLUMNS.get(sort.lstrip("-"))
    if column in table.columns:
        table = table.sort_values(column, ascending=not sort.startswith("-"), kind="stable", na_position="last", ignore_index=True,
                                  key=_ranking(column))
    return table


def _ranking(column: str):
    """What a column is ranked by: its text for the date, the ticker and the company, its
    number for everything else. A blank is missing either way, and goes last."""
    def key(values: pd.Series) -> pd.Series:
        text = values.astype(object).where(values.notna(), "").astype(str).str.strip()
        if column in ("date", "ticker", "company"):
            return text.where(text != "", None)
        return pd.to_numeric(text.str.replace("%", "", regex=False).str.replace(",", "", regex=False), errors="coerce")
    return key


def news(client: Client, tickers: list[str] | None = None, feed: str = "stocks", only: str = "", by_source: bool = False) -> pd.DataFrame:
    """Headlines. `only` (news or blogs) and `by_source` belong to the market feed, which is
    the one without tickers."""
    if feed not in NEWS_FEEDS:
        raise FinvizError(f"feed must be one of {', '.join(NEWS_FEEDS)}")
    if (only or by_source) and feed != "market":
        raise FinvizError("news-only, blogs-only and ordering by source are for the market feed")
    if only and only not in NEWS_KINDS:
        raise FinvizError(f"only must be one of {', '.join(NEWS_KINDS)}")
    if tickers and feed == "market":
        raise FinvizError("the market feed is not per ticker; use the stocks, etf or crypto feed")
    view = "2" if by_source else NEWS_FEEDS[feed]
    params = {"v": view, "c": NEWS_KINDS.get(only, ""), "t": ",".join(t.upper() for t in tickers) if tickers else ""}
    return _table(client, "news", params, "the news", need=("title", "date"))


def short_interest(client: Client, ticker: str) -> pd.DataFrame:
    df = _table(client, "stock", {"t": ticker.upper(), "ty": "si"}, f"the {ticker.upper()} short interest", need=("settlement_date", "short_interest"))
    if "ticker" in df.columns:
        df = df.drop(columns=["ticker"])
    df.insert(0, "ticker", ticker.upper())
    return df


def filings(client: Client, ticker: str, group: str = "", order: str = "-filingDate") -> pd.DataFrame:
    if group and group not in FILING_GROUPS:
        raise FinvizError(f"group must be one of {', '.join(FILING_GROUPS)}")
    df = _table(client, "stock", {"t": ticker.upper(), "ty": "lf", "f": group, "o": order}, f"the {ticker.upper()} filings",
                need=("filing_date", "form"))
    if "ticker" in df.columns:
        df = df.drop(columns=["ticker"])
    df.insert(0, "ticker", ticker.upper())
    return df


def insiders(client: Client, ticker: str = "", kind: str = "all", min_value: int | None = None, owner_cik: str = "",
             ten_percent: str = "", sort: str = "") -> pd.DataFrame:
    """Insider transactions. `owner_cik` is one owner's SEC number; `ten_percent` is "only" or
    "exclude" for owners of a tenth of the company."""
    code = INSIDER_KINDS.get(kind)
    if code is None:
        raise FinvizError(f"kind must be one of {', '.join(INSIDER_KINDS)}")
    owner = str(owner_cik).strip()
    if owner and not (owner.isascii() and owner.isdigit()):
        raise FinvizError("an owner's CIK is a number")
    if ten_percent and ten_percent not in TEN_PERCENT:
        raise FinvizError(f"ten_percent must be one of {', '.join(TEN_PERCENT)}")
    params = {"t": ticker.upper(), "tc": code, "tv": min_value, "oc": owner, "or": TEN_PERCENT.get(ten_percent, ""),
              "o": _sort(sort, INSIDER_SORTS, "insider transactions")}
    return _table(client, "insiders", params, "the insider transactions", need=("ticker", "owner", "transaction"))


def groups(client: Client, by: str = "sector", order: str = "") -> pd.DataFrame:
    return _table(client, "groups", {"g": by, "v": "152", "o": order}, f"the {by} groups", need=("name",))


# ---------------------------------------------------------------- the other exports
#
# Each of these is one request and one table. What they share is in _table: the answer must
# have arrived whole and must be the table that was asked for.

NEWS_FEEDS = {"market": "1", "stocks": "3", "etf": "4", "crypto": "5"}
NEWS_KINDS = {"news": "1", "blogs": "2"}
TEN_PERCENT = {"only": "10", "exclude": "-10"}
INSIDER_KINDS = {"all": "7", "buy": "1", "sale": "2", "option": "4"}
INSIDER_SORTS = ("accepted", "ticker", "transactionValue", "transactionDate", "transactionPricePerShare", "transactionShares",
                 "sharesOwnedFollowingTransaction")
EARNINGS_SORT_COLUMNS = {"earningsDate": "date", "ticker": "ticker", "company": "company", "marketCap": "mcap_m", "epsEstimate": "eps_est",
                         "epsActual": "eps_actual", "epsSurprise": "eps_surprise", "epsReportedEstimate": "eps_gaap_est",
                         "epsReportedActual": "eps_gaap_actual", "epsReportedSurprise": "eps_gaap_surprise", "salesEstimate": "rev_est_m",
                         "salesActual": "rev_actual_m", "salesSurprise": "rev_surprise", "oneDayPriceReaction": "reaction_1d"}
EARNINGS_SORTS = tuple(EARNINGS_SORT_COLUMNS)              # Finviz's sort key -> the column it orders, by our name
DIVIDEND_SORTS = ("ticker", "company", "exdate", "ordinary", "special", "yield")
CALENDARS = {"economic": 365, "dividends": 90}          # kind -> the longest range one request may ask for, in days
STATEMENTS = {("income", False): "IA", ("income", True): "IQ", ("balance", False): "BA", ("balance", True): "BQ",
              ("cash", False): "CA", ("cash", True): "CQ"}
GROWTH = ("yoy", "yoy_pct", "qoq", "qoq_pct")
MARKETS = ("futures", "forex", "crypto")
PERFORMANCE_SORTS = {"day": "perfDay", "5min": "perf5min", "hour": "perfHour", "week": "perfWeek", "month": "perfMonth",
                     "mtd": "perfMtd", "quarter": "perfQuarter", "half": "perfHalfYear", "ytd": "perfYtd", "year": "perfYear"}
PLAIN_SORTS = {"ticker": "ticker", "name": "name", "price": "last"}
CRYPTO_QUOTES = ("USD", "USDT", "EUR", "BTC")
PERFORMANCE_KEYS = {"No.": "no", "Ticker": "ticker", "Name": "name", "Price": "price"}
for _label, _key in (("5 Minutes", "perf_5m"), ("1 Hour", "perf_1h"), ("Day", "perf_day"), ("Week", "perf_week"), ("Month", "perf_month"),
                     ("Month To Date", "perf_mtd"), ("Quarter", "perf_quarter"), ("Half Year", "perf_half"),
                     ("Year To Date", "perf_ytd"), ("Year", "perf_year")):
    PERFORMANCE_KEYS[f"Performance ({_label})"] = _key
    PERFORMANCE_KEYS[f"Performance in Pips ({_label})"] = _key
LIST_SORTS = ("most_popular", "portfolio_value", "num_investments", "new_purchased", "sold_out", "added", "reduced",
              "top_ten_concentration", "turnover", "time_held_top10", "time_held_all")
HOLDING_CHANGES = ("new", "added", "reduced", "closed_out", "no_change")
OPTION_TYPES = ("share", "put", "call")
HOLDING_SORTS = {"managers": ("valueUsd", "issuerTicker", "shares", "percentageOfPortfolio", "changeInShares",
                              "percentChangeFromPrevious", "percOwnership"),
                 "funds": ("valueUsd", "issuerTicker", "issuerName", "assetCategory", "shares", "shareType", "percentageOfPortfolio",
                           "previousPercentageOfPortfolio", "percentChangeFromPrevious", "payOffProfile", "investmentCountry")}
ASSET_CATEGORIES = ("EC", "EP", "DBT", "STIV", "LON", "ABS-MBS", "DCR", "DE", "DIR", "DFE", "DCO", "DO", "RA", "SN", "ABS-APCP",
                    "ABS-CBDO", "ABS-O", "COMM", "RE")
_HOLDER = re.compile(r"(?:[A-Za-z0-9]+-)*([Ss]?\d+)", re.ASCII)      # an id, or the site's slug ending in it


def _table(client: Client, path: str, params: dict, what: str, need: tuple[str, ...] = (), keys: dict[str, str] | None = None,
           keep_text: tuple[str, ...] = ()) -> pd.DataFrame:
    """One export as a table, or an error when it is not the table asked for: cut short (every
    line Finviz sends ends), not readable as CSV, or without the columns it always has."""
    body = client.export(path, params)
    if not body.endswith(("\n", "\r")):
        raise FinvizError(f"{what} did not arrive whole")
    try:
        _, rows = frame.parse_table(body, strict=True)       # quoting that never closes is an answer cut short
        df = generic(body, keys, keep_text)
    except csv.Error:
        raise FinvizError(f"{what} could not be read as a table") from None
    if any(None in r or any(v is None for v in r.values()) for r in rows):          # a row wider or narrower than the header
        raise FinvizError(f"{what} did not arrive whole: a row does not have the header's columns")
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise FinvizError(f"{what} came back without: {', '.join(missing)}")
    return df


def _sort(value: str, allowed: tuple[str, ...], what: str) -> str:
    """A sort key as Finviz takes it: one of `allowed`, with - in front for descending."""
    if value and value.lstrip("-") not in allowed or value.startswith("--"):
        raise FinvizError(f"{what} can be sorted by: {', '.join(allowed)} (put - in front for descending)")
    return value


def _iso(value: str, what: str) -> str:
    try:
        return dt.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise FinvizError(f"{what} must be a date, YYYY-MM-DD") from None


def options(client: Client, ticker: str, expiry: str = "", strike: float | None = None) -> pd.DataFrame:
    """The options chain: every contract, or one expiry, one strike, or both. Finviz leaves
    out the column that was filtered on, so it is put back: every row has expiry and strike."""
    what = f"the {ticker.upper()} options chain"
    if strike is not None:
        try:
            strike = float("nan") if isinstance(strike, bool) else float(strike)       # True is not a strike of 1
        except (TypeError, ValueError):
            strike = float("nan")
        if not math.isfinite(strike) or strike <= 0:
            raise FinvizError("the strike must be a positive number")
        strike = int(strike) if strike.is_integer() else strike
    params = {"t": ticker.upper(), "ty": "oc", "e": _iso(expiry, "the expiry") if expiry else "", "s": strike}
    need = ("contract_name", "type") + (() if expiry else ("expiry",)) + (() if strike is not None else ("strike",))
    df = _table(client, "stock", params, what, need=need)
    if "expiry" in df.columns:                              # Finviz writes 12/18/2026; it leaves here as 2026-12-18
        text = df["expiry"].astype(object).where(df["expiry"].notna(), "").astype(str).str.strip()
        when = pd.to_datetime(text, format="%m/%d/%Y", errors="coerce")
        if when.isna().any():
            raise FinvizError(f"could not read the expiry dates in {what} ({int(when.isna().sum())} of {len(text)} rows)")
        df["expiry"] = when.dt.strftime("%Y-%m-%d")
    else:
        df.insert(2, "expiry", params["e"])
    if "strike" not in df.columns:
        df.insert(3, "strike", float(strike) if strike is not None else float("nan"))
    if "ticker" in df.columns:
        df = df.drop(columns=["ticker"])
    df.insert(0, "ticker", ticker.upper())
    return df


def _cell(value):
    """A statement cell: a whole number, a number, or the text as it came (a date, a label)."""
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    plain = text.replace(",", "")
    if re.fullmatch(r"[+-]?\d+", plain, re.ASCII):
        return int(plain)                                   # exactly, also beyond what a float holds
    try:
        number = float(plain)
    except ValueError:
        return text
    return number if math.isfinite(number) else text


def statements(client: Client, ticker: str, statement: str = "income", quarterly: bool = False, reuters: bool = False,
               oldest_first: bool = False, growth: tuple[str, ...] = ()) -> pd.DataFrame:
    """A financial statement: one row per line item, one column per period (eight of them).

    Amounts are in full units of the statement's currency. Finviz says which currency only in
    the name of the file it sends, so it is read from there into a `currency` column (empty
    when the name does not carry one).
    """
    code = STATEMENTS.get((statement, bool(quarterly)))
    if code is None:
        raise FinvizError("statement must be one of income, balance, cash")
    bad = [g for g in growth if g not in GROWTH]
    if bad:
        raise FinvizError(f"growth rows are: {', '.join(GROWTH)}")
    params = {"t": ticker.upper(), "ty": "statements", "s": code, "so": "R" if reuters else "",
              "order": "oldest" if oldest_first else "", "growth": ",".join(growth)}
    body = client.export("stock", params)
    what = f"the {ticker.upper()} {statement} statement"
    if not body.endswith(("\n", "\r")):
        raise FinvizError(f"{what} did not arrive whole")
    try:
        headers, rows = frame.parse_table(body, strict=True)
    except csv.Error:
        raise FinvizError(f"{what} could not be read as a table") from None
    if not headers or headers[0] != "Period" or len(headers) < 2 or len(set(headers)) != len(headers):
        raise FinvizError(f"{what} came back without its Period column and the periods beside it")
    if any(None in r or any(v is None for v in r.values()) for r in rows):
        raise FinvizError(f"{what} did not arrive whole")
    names = ["item"] + [C.slug(h) for h in headers[1:]]
    if len(set(names)) != len(names):
        raise FinvizError(f"{what} names a period twice")
    table = pd.DataFrame([[str(r[headers[0]]).strip()] + [_cell(r[h]) for h in headers[1:]] for r in rows], columns=names, dtype=object)
    name = (getattr(client, "last_filename", "") or "").lower()
    found = re.fullmatch(r"finviz_[a-z0-9.\-]+_[a-z]+_([a-z]{3})_[0-9_\-]+\.csv", name)
    table.insert(0, "ticker", ticker.upper())
    table.insert(1, "currency", found.group(1).upper() if found else "")
    return table


def calendar(client: Client, kind: str, date_from: str, date_to: str = "", sort: str = "") -> pd.DataFrame:
    """The economic or the dividend calendar. (The earnings calendar has its own function.)

    Economic: Finviz moves the first day back to that week's Monday, and gives the whole week
    when no last day is named. Dividends: one day when no last day is named.
    """
    if kind not in CALENDARS:
        raise FinvizError(f"calendar must be one of {', '.join(CALENDARS)}")
    start = dt.date.fromisoformat(_iso(date_from, "the first day"))
    end = dt.date.fromisoformat(_iso(date_to, "the last day")) if date_to else None
    if end is not None and end < start:
        raise FinvizError("the last day is before the first")
    # Finviz does not refuse a longer range: it stops at that many days after the first and
    # says nothing. So it is refused here.
    if end is not None and (end - start).days > CALENDARS[kind]:
        raise FinvizError(f"the {kind} calendar reaches at most {CALENDARS[kind]} days past the first day in one request")
    if kind == "economic" and sort:
        raise FinvizError("the economic calendar has no sort order")
    # A last day that was named is always sent: without one the economic calendar gives the
    # whole week, so leaving out a last day equal to the first would turn a day into a week.
    params = {"dateFrom": start.isoformat(), "dateTo": end.isoformat() if end else "",
              "sort": _sort(sort, DIVIDEND_SORTS, "the dividend calendar") if kind == "dividends" else ""}
    need = ("event", "date") if kind == "economic" else ("ticker", "ex_date")
    # An economic figure is a percent, a count in thousands, an index level or a dollar sum,
    # release by release: those columns are text, always.
    keep = ("for", "actual", "expected", "prior") if kind == "economic" else ()
    return _table(client, f"calendar/{kind}", params, f"the {kind} calendar", need=need, keep_text=keep)


def performance(client: Client, market: str, sort: str = "day", reverse: bool = False, pips: bool = False,
                quote: str = "") -> pd.DataFrame:
    """Futures, forex or crypto: every instrument's change over each timeframe.

    `sort` is a timeframe (day, 5min, hour, week, month, mtd, quarter, half, ytd, year), best
    first, or ticker, name, price; `reverse` turns the order round. `pips` is for forex,
    `quote` (USD, USDT, EUR, BTC) for crypto.
    """
    if market not in MARKETS:
        raise FinvizError(f"market must be one of {', '.join(MARKETS)}")
    if pips and market != "forex":
        raise FinvizError("pips are for forex")
    if quote and (market != "crypto" or quote.upper() not in CRYPTO_QUOTES):
        raise FinvizError(f"a quote currency is for crypto, one of {', '.join(CRYPTO_QUOTES)}")
    if sort in PERFORMANCE_SORTS:
        key = PERFORMANCE_SORTS[sort] + ("Pips" if pips else "Pct")
        descending = not reverse                              # the best first, unless turned round
    elif sort in PLAIN_SORTS:
        key = PLAIN_SORTS[sort]
        descending = (sort == "price") != reverse             # names from A, prices from the top
    else:
        raise FinvizError(f"sort must be one of {', '.join([*PERFORMANCE_SORTS, *PLAIN_SORTS])}")
    params = {"sort": ("-" if descending else "") + key, "unit": "pips" if pips else "", "c": quote.upper()}
    return _table(client, f"{market}/performance", params, f"the {market} table", need=("ticker", "price"), keys=PERFORMANCE_KEYS)


def holders(client: Client, kind: str, search: str = "", sort: str = "") -> pd.DataFrame:
    """Institutional managers or funds, 500 at most: narrow it with part of a name."""
    if kind not in HOLDING_SORTS:
        raise FinvizError("kind must be managers or funds")
    params = {"search": search, "sort": _sort(sort, LIST_SORTS, f"the list of {kind}")}
    return _table(client, kind, params, f"the list of {kind}", need=("name", "investor_id"))


def holdings(client: Client, holder: str, report_date: str = "", change: str = "", option_type: str = "",
             asset_category: str = "", sort: str = "") -> pd.DataFrame:
    """What one manager or one fund holds. `holder` is an investor id (digits: a manager), a
    series id (S and digits: a fund), or the name-and-id slug the site uses for either."""
    # The id may arrive as a number: a manager's investor_id is one in the table `holders` returns.
    holder = "" if holder is None or isinstance(holder, bool) else str(holder).strip()
    found = _HOLDER.fullmatch(holder)
    if not found:
        raise FinvizError("give an investor id (digits), a fund's series id (S and digits), or the site's slug ending in one")
    kind = "funds" if found.group(1).upper().startswith("S") else "managers"
    if change and change not in HOLDING_CHANGES:
        raise FinvizError(f"change must be one of {', '.join(HOLDING_CHANGES)}")
    if option_type and (kind != "managers" or option_type not in OPTION_TYPES):
        raise FinvizError(f"an option type is for a manager's holdings, one of {', '.join(OPTION_TYPES)}")
    if asset_category and (kind != "funds" or asset_category.upper() not in ASSET_CATEGORIES):
        raise FinvizError(f"an asset category is for a fund's holdings, one of {', '.join(ASSET_CATEGORIES)}")
    params = {"reportEndDate": _iso(report_date, "the report date") if report_date else "", "transactionType": change,
              "optionType": option_type, "assetCategory": asset_category.upper(),
              "sort": _sort(sort, HOLDING_SORTS[kind], f"a {kind[:-1]}'s holdings")}
    return _table(client, f"{kind}/{holder}/holdings", params, f"the holdings of {holder}", need=("ticker", "name"))
