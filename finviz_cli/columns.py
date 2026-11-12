"""Screener columns: Finviz's ids and headers, and the short keys this CLI uses for them.

The id list was read from Finviz's own parameter table and checked against a live export on
2026-11-10 (docs/columns.md is generated from this file with `finviz columns --markdown`).
Ids 100-124, 151 and 152 describe ETFs and are left out of a `type=stocks` export.
"""
from __future__ import annotations

import re

# index = Finviz column id
HEADERS = [
    "No.", "Ticker", "Company", "Sector", "Industry", "Country", "Market Cap", "P/E", "Forward P/E", "PEG",
    "P/S", "P/B", "P/Cash", "P/Free Cash Flow", "Dividend Yield", "Payout Ratio", "EPS (ttm)",
    "EPS Growth This Year", "EPS Growth Next Year", "EPS Growth Past 5 Years", "EPS Growth Next 5 Years",
    "Sales Growth Past 5 Years", "EPS Growth Quarter Over Quarter", "Sales Growth Quarter Over Quarter",
    "Shares Outstanding", "Shares Float", "Insider Ownership", "Insider Transactions", "Institutional Ownership",
    "Institutional Transactions", "Short Float", "Short Ratio", "Return on Assets", "Return on Equity",
    "Return on Invested Capital", "Current Ratio", "Quick Ratio", "LT Debt/Equity", "Total Debt/Equity",
    "Gross Margin", "Operating Margin", "Profit Margin", "Performance (Week)", "Performance (Month)",
    "Performance (Quarter)", "Performance (Half Year)", "Performance (Year)", "Performance (YTD)", "Beta",
    "Average True Range", "Volatility (Week)", "Volatility (Month)", "20-Day Simple Moving Average",
    "50-Day Simple Moving Average", "200-Day Simple Moving Average", "50-Day High", "50-Day Low", "52-Week High",
    "52-Week Low", "Relative Strength Index (14)", "Change from Open", "Gap", "Analyst Recom", "Average Volume",
    "Relative Volume", "Price", "Change", "Volume", "Earnings Date", "Target Price", "IPO Date",
    "After-Hours Close", "After-Hours Change", "Book/sh", "Cash/sh", "Dividend", "Employees", "EPS Next Q",
    "Income", "Index", "Optionable", "Prev Close", "Sales", "Shortable", "Short Interest", "Float %", "Open",
    "High", "Low", "Trades", "Performance (1 Minute)", "Performance (2 Minutes)", "Performance (3 Minutes)",
    "Performance (5 Minutes)", "Performance (10 Minutes)", "Performance (15 Minutes)", "Performance (30 Minutes)",
    "Performance (1 Hour)", "Performance (2 Hours)", "Performance (4 Hours)", "Asset Type", "ETF Type", "Region",
    "Single Category", "Sector/Theme", "Tags", "Active/Passive", "Net Expense Ratio", "Total Holdings",
    "Assets Under Management", "Net Asset Value", "Net Asset Value %", "Net Flows (1 Month)",
    "Net Flows % (1 Month)", "Net Flows (3 Month)", "Net Flows % (3 Month)", "Net Flows (YTD)", "Net Flows % (YTD)",
    "Net Flows (1 Year)", "Net Flows % (1 Year)", "Return 1 Year", "Return 3 Year", "Return 5 Year",
    "Return 10 Year", "Return Since Inception", "All-Time High", "All-Time Low", "EPS Surprise",
    "Revenue Surprise", "Exchange", "Dividend TTM", "Dividend Ex Date", "EPS Year Over Year TTM",
    "Sales Year Over Year TTM", "52-Week Range", "News Time", "News URL", "News Title", "Performance (3 Years)",
    "Performance (5 Years)", "Performance (10 Years)", "After-Hours Volume", "EPS Growth Past 3 Years",
    "Sales Growth Past 3 Years", "Enterprise Value", "EV/EBITDA", "EV/Sales", "Dividend Growth 1 Year",
    "Dividend Growth 3 Years", "Dividend Growth 5 Years", "Daily Digest", "Security Type", "Tracked Index",
    "Candlestick", "Dilution Risk", "Performance (MTD)",
]

ETF_ONLY = set(range(100, 125)) | {151, 152}

# Short keys for the columns we work with; everything else gets a slug of its header.
KEYS = {
    0: "no", 1: "ticker", 2: "company", 3: "sector", 4: "industry", 5: "country", 6: "mcap_m",
    24: "shares_out_m", 25: "float_m", 30: "short_float", 31: "short_ratio", 42: "perf_week", 43: "perf_month",
    44: "perf_quarter", 45: "perf_half", 46: "perf_year", 47: "perf_ytd", 48: "beta", 49: "atr",
    50: "volatility_week", 51: "volatility_month", 52: "sma20", 53: "sma50", 54: "sma200", 55: "high_50d",
    56: "low_50d", 57: "high_52w", 58: "low_52w", 59: "rsi", 60: "open_chg", 61: "gap", 62: "analyst_recom",
    63: "avgvol", 64: "relvol", 65: "price", 66: "chg", 67: "vol", 68: "earnings", 69: "target_price",
    70: "ipo_date", 71: "ah_close", 72: "ah_chg", 76: "employees", 78: "income_m", 79: "index",
    80: "optionable", 81: "prev_close", 82: "sales_m", 83: "shortable", 84: "short_interest_m", 85: "float_pct",
    86: "open", 87: "high", 88: "low", 89: "trades", 90: "perf_1m", 91: "perf_2m", 92: "perf_3m", 93: "perf_5m",
    94: "perf_10m", 95: "perf_15m", 96: "perf_30m", 97: "perf_1h", 98: "perf_2h", 99: "perf_4h",
    125: "ath_dist", 126: "atl_dist", 127: "eps_surprise", 128: "rev_surprise", 129: "exchange",
    134: "range_52w", 135: "news_time", 136: "news_url", 137: "news", 141: "ah_vol", 150: "daily_digest",
    153: "candlestick", 154: "dilution_risk", 155: "perf_mtd",
}

TEXT_IDS = {1, 2, 3, 4, 5, 68, 70, 79, 80, 83, 100, 101, 102, 103, 104, 105, 106, 129, 131, 134, 135, 136, 137,
            150, 151, 152, 153}

# What each number means, for the keys where it is not obvious.
UNITS = {
    "mcap_m": "millions of dollars", "shares_out_m": "millions of shares", "float_m": "millions of shares",
    "short_interest_m": "millions of shares", "income_m": "millions of dollars", "sales_m": "millions of dollars",
    "avgvol": "shares (Finviz sends thousands; converted on load)", "vol": "shares", "ah_vol": "shares",
    "chg": "percent vs previous close", "open_chg": "percent vs today's open", "gap": "percent, open vs previous close",
    "short_float": "percent of float", "float_pct": "percent of shares outstanding",
    "ath_dist": "percent below the all-time high", "atl_dist": "percent above the all-time low",
    "relvol": "today's volume / average volume", "atr": "dollars", "trades": "number of trades today",
    "dilution_risk": "Finviz score, higher is riskier",
}

PRESETS = {
    # the set the first saved days were taken with
    "core": [1, 2, 3, 4, 5, 6, 25, 30, 49, 60, 61, 63, 64, 67, 81, 86, 87, 88, 65, 66, 68, 135, 137],
    # what a daily snapshot keeps: core plus what history was missing
    "wide": [1, 2, 3, 4, 5, 129, 6, 24, 25, 85, 30, 31, 84, 49, 60, 61, 63, 64, 67, 89, 81, 86, 87, 88, 65, 66,
             71, 72, 141, 68, 127, 128, 70, 80, 83, 154, 125, 57, 58, 52, 53, 54, 59, 42, 43, 135, 136, 137, 150],
    # a live scan: price, volume and the short-horizon performance columns
    "intraday": [1, 25, 63, 64, 67, 89, 81, 86, 87, 88, 65, 66, 60, 61, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99,
                 135, 137],
}
PRESETS["all"] = [i for i in range(len(HEADERS)) if i not in ETF_ONLY]


def slug(header: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", header.lower().replace("%", " pct ").replace("/", " ")).strip("_")
    return s or "col"


def key_for_id(col_id: int) -> str:
    return KEYS.get(col_id) or slug(HEADERS[col_id])


_HEADER_TO_ID = {h: i for i, h in enumerate(HEADERS)}


def key_for_header(header: str) -> str:
    col_id = _HEADER_TO_ID.get(header)
    return key_for_id(col_id) if col_id is not None else slug(header)


TEXT_KEYS = {key_for_id(i) for i in TEXT_IDS}


def resolve(spec: str | None) -> list[int]:
    """'wide', 'core', 'all', or a comma list of ids and keys such as '1,65,relvol,float_m'."""
    if not spec:
        return PRESETS["wide"]
    if spec in PRESETS:
        return PRESETS[spec]
    by_key = {key_for_id(i): i for i in range(len(HEADERS))}
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit() and int(part) < len(HEADERS):
            out.append(int(part))
        elif part in by_key:
            out.append(by_key[part])
        else:
            raise ValueError(f"unknown column '{part}' (run: finviz columns)")
    if 1 not in out:
        out.insert(0, 1)                       # every row needs its ticker
    return out


def table() -> list[dict]:
    return [{"id": i, "key": key_for_id(i), "header": h, "kind": "text" if i in TEXT_IDS else "number",
             "etf_only": i in ETF_ONLY, "unit": UNITS.get(key_for_id(i), "")} for i, h in enumerate(HEADERS)]


# What a screening table holds beside Finviz's own columns: name -> (kind, what it is).
# docs/enrichment.md has the rules in full.
COMPUTED = {
    "date": ("text", "the trading day of the row, YYYY-MM-DD"),
    "source": ("text", "where the day's rows came from: market (a pull of every listed stock) or lfr (an imported end-of-day file)"),
    "di": ("number", "the session's number on the trading calendar; consecutive sessions differ by one. Saved days only: a live pull has no di"),
    "dollar_vol": ("number", "price x volume, in dollars"),
    "rotation": ("number", "volume / float: how many times the float traded"),
    "range_pct": ("number", "(high - low) / low, percent"),
    "high_vs_open": ("number", "the day's high against the open, percent"),
    "low_vs_open": ("number", "the day's low against the open, percent"),
    "close_pos": ("number", "where the price sits in the day's range: 0 at the low, 1 at the high"),
    "from_high": ("number", "percent from the day's high: 0 at the high, -20 is 20% under it"),
    "kept": ("number", "share of the run above the open still there at the close: 1 is a close at the high, below 0 is under the open"),
    "atr_pct": ("number", "ATR as a percent of price"),
    "opening": ("text", "gap up, flat open or gap down: the open 5%+ above, within 5% of, or 5%+ below the previous close"),
    "day": ("text", "what the session did after the open: 'ran and held', 'ran, gave it back', 'faded' or 'held'"),
    "shape": ("text", "opening and day joined, e.g. 'flat open, ran and held'"),
    "mover": ("true/false", "price $1+, $1M+ traded, twice normal volume, and a 10% up-move (high vs open, change, or gap)"),
    "close_kind": ("text", "strong close, weak close or in between"),
    "catalyst": ("text", "what was out between the previous close and this one: earnings, news, earlier, after close or none"),
    "news_age": ("number", "days since the latest headline; 0 is that day, negative is a later day"),
    "days_to_earn": ("number", "days to the row's earnings date; negative is days since"),
    "split_headline": ("true/false", "the latest headline, at most 30 days old, mentions a reverse split or share consolidation"),
    "yesterday": ("text", "the session before: 'mover, strong close', 'mover, in between', 'mover, weak close', 'traded, not a mover', "
                          "'quiet (under 50k shares)', 'not covered' or 'no history'"),
    "streak": ("number", "mover days in the 10 sessions before this one"),
    "days_since_split": ("number", "sessions since the name's last reverse split; empty if none was seen"),
    "split_ratio": ("number", "size of that split: 10 means 1-for-10"),
}


def screen_table() -> list[dict]:
    """The columns of the table `finviz screen` filters: Finviz's own that a pull keeps, then the ones added here."""
    own = [{"name": key_for_id(i), "kind": "text" if i in TEXT_IDS else "number", "from": "Finviz", "header": HEADERS[i],
            "meaning": UNITS.get(key_for_id(i), "")} for i in [0, *PRESETS["wide"]]]
    return own + [{"name": name, "kind": kind, "from": "computed", "header": "", "meaning": meaning} for name, (kind, meaning) in COMPUTED.items()]
