# For agents: how to use this CLI well

This page is written for a language model that has been given `finviz` as a tool, and for
the engineer wiring it up. It says which tool answers which question, what a result looks
like, and how to read the numbers without being misled by them.

## Two ways in

- **The command line.** `finviz COMMAND ...` prints one JSON object per line when its
  output is piped, and notes about the data on stderr. Exit code 0 is success; 2 is a usage
  error or a Finviz failure; 3 is data that failed its checks.
- **The MCP server.** `finviz-mcp` serves the same commands as tools over stdin and stdout
  (`pip install "finviz-cli[mcp]"`). Every tool only reads.

  ```json
  {"mcpServers": {"finviz": {"command": "finviz-mcp"}}}
  ```

  The token is found the same way as for the command line: `FINVIZ_API_KEY` in the
  environment, or a `.env` file in the data folder (`~/.finviz-cli/.env`).

## What a tool returns

Every tool returns the same shape:

```json
{"ok": true, "rows": [{"ticker": "ABCD", "price": 3.12, "gap": 24.5}], "row_count": 41,
 "notes": ["data: 2026-11-11 session as of 2026-11-11 09:47 ET, whole market; 5609 rows, 41 match"],
 "truncated": "25 of 41 rows shown (the first); narrow the request, or raise limit (at most 500)"}
```

- `rows` are the answer. Fields with no value are left out of a row.
- `notes` say what the data is: which session, as of when. Read them before reasoning about
  the rows, and quote the time when you report a number.
- `truncated` is there only when rows were cut. Narrow the request before raising `limit`.
- When `ok` is false, `rows` is empty and `error` says why in one line. Fix the call. Do not
  send the same call again unchanged.

## Which tool for which question

| The question | The tool |
|---|---|
| Which stocks match some condition right now, or on a past day? | `screen` |
| What moved today, and how did each close? | `movers` |
| What has this ticker done over the last sessions? | `ticker_history` |
| What happened inside the day: when was the high, did it hold? | `intraday` |
| Raw price bars | `price_bars` |
| Why is it moving? | `news`, then `sec_filings` |
| Is it diluting, how much cash does it have? | `financial_statement` (balance, cash), `sec_filings` (registration-statements), `short_interest` (the float over time) |
| Who is selling or buying inside the company? | `insider_trades` |
| Options positioning | `options_chain` (always narrow it: an expiry, a strike or a type) |
| What is scheduled? | `earnings_calendar`, `economic_calendar`, `dividend_calendar` |
| What is the wider market doing? | `market_performance` (futures, forex, crypto), `sector_groups`, `market_news` |
| Who holds what? | `institutions`, then `institution_holdings` |
| What can a filter name, and what does it mean? | `list_columns` |
| How far back does the saved history go? | `store_status` |

Prefer one `screen` call over many per-ticker calls. One whole-market request serves every
`screen` for the next ten minutes, and Finviz allows 10 requests per 5 seconds for the
whole account.

## Writing a filter

A filter is an expression over column names:

```
gap >= 5 and relvol > 2 and float_m < 20
abs(gap) <= 3 and open_chg >= 10
yesterday == 'mover, weak close'
news.str.contains('offering', case=False)
ticker in ['AAPL', 'MSFT']
catalyst == 'earnings' and price.between(1, 20)
```

It may compare columns with values and with each other, combine with `and`, `or`, `not`,
and do arithmetic on numbers. These are all the methods it may call:

- on a column: `abs`, `isin`, `between`, `isna`, `notna` (also `isnull`, `notnull`),
  `round`, `clip`, `fillna`;
- on a column's text, after `.str`: `contains`, `startswith`, `endswith`, `match`,
  `fullmatch`, `lower`, `upper`, `strip`, `len`;
- and the function `abs(...)`.

Nothing else: a filter is data, not code, and anything that reaches outside the table is
refused with a message that says what was wrong.

A filter has to be true or false for each row. Write `streak > 0`, not `streak`; write
`ticker in ['AAPL', 'MSFT']` for a list.

Compare like with like: text with text in quotes (`yesterday == 'mover, weak close'`),
numbers with numbers, and a true/false column with `True` or `False`, or on its own
(`mover`, `not mover`). Comparing text with a number, or a number with `True`, is refused
rather than answered, because the answer would be "no row" or "every row" and look like a
result.

A field that is left out of a row is missing. A missing value is neither above nor below
anything, so a row with no float does not pass `float_m < 20`, and a row with no headline
does not pass `news_time < '2026-11-09'`. It is not equal to anything either, so it does
pass `!=`: `optionable != 'Yes'` keeps the rows where nothing is known about options, and
`optionable.notna() and optionable != 'Yes'` leaves them out. Ask for the missing rows
with `float_m.isna()`, and for the rest with `float_m.notna()`. Comparing with `None` is
refused for the same reason as above.

For text there is one kind of missing, however the cell came to be empty: `news.isna()`
and `news == ''` both find the rows with no headline, and `news.notna()` and `news != ''`
the rows with one. A text test (`contains`, `startswith`, `endswith`, `match`,
`fullmatch`) never passes a row with no text, whatever the pattern, unless asked to:
`news.str.contains('offering', na=True)` is the headlines that mention an offering, and
the rows with no headline. `not news.str.contains('offering')` does include them; add
`news.notna()` to leave them out. The length of no text is 0, so `news.str.len() < 5`
includes them too.

Columns worth knowing (use `list_columns` for the rest):

| Column | Meaning |
|---|---|
| `price`, `open`, `high`, `low`, `prev_close` | prices for the session |
| `chg`, `gap`, `open_chg` | percent change on the day, at the open, and since the open |
| `vol`, `avgvol`, `relvol` | shares traded, the average, and the ratio of the two |
| `float_m`, `mcap_m` | float and market cap, in millions |
| `rotation` | volume divided by float |
| `high_vs_open`, `kept`, `close_pos` | the run above the open, how much of it is left, where the price sits in the range |
| `shape`, `mover`, `close_kind` | labels for a finished day |
| `catalyst`, `news`, `news_age` | what set it off, the latest headline, how old it is |
| `yesterday`, `streak`, `days_since_split` | the session before, mover days in the last ten, sessions since a reverse split |

## Reading the numbers correctly

- **Time of day changes what the columns mean.** Before 09:30 ET, `price`, `high`, `low`
  and `chg` are premarket values. After 16:00 they are the close (for thinly traded names
  the final close can arrive some minutes later). `vol` is the whole day so far at any
  hour: premarket included, and after the close it goes on counting after-hours trading.
- **Percentages are plain numbers.** `gap` of 12.5 means 12.5%.
- **The labels describe, they do not predict.** `shape`, `close_kind` and `mover` are
  only settled after the close; during the session they describe the day so far.
- **`catalyst = none` means no dated headline in the window**, not that nothing happened.
  The screener carries one headline per ticker. Use `news` for the list.
- **`yesterday = no history` or `not covered` means unknown**, not quiet.
- **Minute bars carry part of the volume.** In `intraday`, `vol` is the whole day (from the
  daily bar); the `bar_` columns are a rough guide to where in the day it came.
- **A statement's amounts are in the currency of its `currency` column**, in whole units.
- **Missing is missing.** A field that is absent from a row was not reported. Do not read
  it as zero.

More of this, with the evidence, is in [quirks.md](quirks.md). The exact rule behind every
computed column is in [enrichment.md](enrichment.md).

## What this tool is not

It reads market data. It does not place orders, and nothing it returns is investment
advice. When you report a result, say what the data was and when it was from, and keep
what the data shows apart from what you infer from it.
