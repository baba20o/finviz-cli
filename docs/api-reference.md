# Finviz Elite export API: our reference

Written by us from Finviz's API pages (elite.finviz.com/apis, read 2026-11-10) and from
requests made the same day. It is a working reference in our own words, not a copy of
Finviz's documentation. Where something was only read and not tried, it says so.

## The basics

| | |
|---|---|
| Base | `https://elite.finviz.com/export/<endpoint>` |
| Answer | CSV, first line is the header |
| Auth | `auth=<token>` in the query string. The token is shown on the API pages, next to a button that regenerates it. |
| Finding a URL | Every export mirrors a page on the site: take the page URL, put `export/` in front of the path, keep the query string. |
| Old URLs | The `.ashx` forms (`export.ashx`, `quote_export.ashx`, ...) still answer with a 301 to the new path. A client that does not follow redirects gets an empty body. |
| Refusals | A request Finviz cannot serve is answered with HTTP 400 and a few words: `Ticker not found.` for an unknown ticker, `Invalid query` when a required parameter is missing. The CLI passes those words on. |
| File name | Each answer names a file in its `Content-Disposition` header, e.g. `finviz_msft_balanceQuarterly_usd_<date>_<time>.csv`. For statements that name is the only place the currency is given. |

### Rate limit

- 10 requests per rolling 5 seconds, counted per Elite account across every export endpoint.
- Going over returns HTTP 429 with `Retry-After: 5`. Refused requests do not count.
- **The budget is shared by everything that uses the token**: every script, on every
  machine. The client here spaces requests 0.75 s apart (at most 7 in any 5 seconds) across
  every `finviz` process on the machine, through a lock and a slot file in the data folder.
  After a 429 all of them wait out the cooldown, including the command that runs after one
  gave up. Programs on other machines are not coordinated with: there the 429 is the only
  brake.

### Token safety

The token travels in the URL, so a logged URL is a leaked token. `client.py`:

- never puts the URL in an error;
- rewrites every log record from urllib3, requests, botocore and boto3 as it is created:
  the message, the exception text a formatter would append, and the stack text. `auth=` is
  recognised percent-encoded too (`auth%3d...`, as inside a sign-in redirect);
- blanks those libraries' records entirely while the token is being read from AWS, since
  the token is not known yet and could not be recognised in a logged response body;
- refuses a 200 answer that is markup, has an HTML content type, or repeats the request
  (its URL, or the token written plainly, percent-encoded in either letter case, with `+`
  for a space, or encoded twice), so such a body is never printed or saved.

The token is read from `FINVIZ_API_KEY`, in the environment or in a `.env` file (the
current folder, then the data folder), or from the AWS secret named by `FINVIZ_SECRET_ID`
when that is set (its region from `AWS_REGION` or `AWS_DEFAULT_REGION`, in the environment
or the data folder's `.env`, else from AWS's own settings). The name of the secret is taken from the environment or from the data
folder's `.env` only, never from a `.env` that happens to lie in the current folder. Keep
a `.env` out of version control.

## Endpoints

"Tried" means a request on 2026-11-10 or 2026-11-11 returned the columns listed.

### Screener: `/export/screener`

| Parameter | Meaning |
|---|---|
| `type` | `stocks`, `etf`, or left out for both together (the old behaviour) |
| `v` | View number from the screener page. `152` is the custom view and honours `c`. |
| `f` | Filter codes, comma separated, exactly as in the screener URL (`sh_curvol_o50,sh_float_u100`) |
| `o` | Sort key, `-` in front for descending (`-changeopen`, `-relativevolume`) |
| `c` | Column ids, comma separated; the header row comes back in the same order. See [columns.md](columns.md). |
| `t` | Tickers, comma separated, to get just those rows |

Tried. `type=stocks` with no filter returned 5,609 rows; with `f=sh_curvol_o50`, 4,465.
Without `type` the same filter returned 6,708, the extra rows being ETFs. Asking for all
156 column ids with `type=stocks` returns 129 columns: the ETF-only ids are dropped.

Filter codes are not listed on the API page. Build the screen on the site and copy `f`
from the address bar.

### Stock: `/export/stock`

`t=<TICKER>` is required and one ticker per request. `ty` picks the output.

| `ty` | Output | Other parameters | Columns (tried) |
|---|---|---|---|
| left out or `c` | Price history | `p` bar size, `r` range | Date, Open, High, Low, Close, Volume |
| `oc` | Options chain | `e=YYYY-MM-DD` expiry, `s=<strike>`; both optional and exact. The column you filter on is left out of the CSV. | Contract Name, Last Trade, Expiry, Strike, Last Close, Bid, Ask, Change $, Change %, Volume, Open Int., Type, IV, Delta, Gamma, Theta, Vega, Rho |
| `lf` | SEC filings | `f` filing group, `o` sort (`filingDate`, `reportDate`, `form`, `-` for descending) | Filing Date, Report Date, Form, Description, Filing, Document |
| `si` | Short interest history | none | Settlement Date, Short Interest, Shares Float, Avg. Daily Volume, Short Float, Short Ratio |
| `statements` | Financial statements | `s` statement, `so=R` for the Reuters source, `order=newest\|oldest`, `growth` | Period, then one column per period (`TTM`, `FY 2026`, ... or `Q1 2027`, ...): one row per line item, up to eight periods |

Price history: `p` accepted `i1`, `i3`, `i5`, `i15`, `i30`, `h`, `d`, `w`, `m` when tried. A bar
size it does not know is answered with daily bars, without an error; the CLI only sends
the ones listed. Daily bars went back about ten years, also with `r=max`; monthly bars to
1986. `r` is one of `d1, d5, m1, m3, m6, ytd, y1,
y2, y5, max`; left out, it gives everything the bar size has. One-, three- and five-minute
bars include the premarket and after hours (04:00 to 20:00 ET); fifteen- and thirty-minute
bars are the regular session only. One- and three-minute bars went back ten sessions,
five- and fifteen-minute bars fifteen, thirty-minute bars about 200 (2026-11-11). An
unknown ticker is answered with HTTP 400. The minute bars are not the same feed as the
daily bar: see [quirks.md](quirks.md), "Minute bars are a different feed from the daily bar".

Filing groups for `f`: `annual-quarterly-current`, `insider-equity`,
`beneficial-ownership`, `exempt-offerings`, `registration-statements`,
`filing-review-correspondence`, `sec-orders-notices`, `proxy-materials`, `tender-offers`,
`trust-indentures`.

Statements for `s`: `IA`, `IQ` income statement (annual, quarterly); `BA`, `BQ` balance
sheet; `CA`, `CQ` cash flow. `growth` takes `yoy`, `yoy_pct`, `qoq`, `qoq_pct`, comma
separated; each adds a row under every line item. Amounts and share counts are whole
units; per-share figures and ratios have two decimals. The first rows are `Period End
Date` and `Period Length`. A company that reports twice a year has half-years where
quarters are asked for. Tried.

Options: `IV` is a decimal (0.44 is 44%), `Type` is `put` or `call`, `Expiry` is
`MM/DD/YYYY` (the CLI writes it `YYYY-MM-DD`), and a field with no quote is empty.

### Calendars: `/export/calendar/<kind>`

| Kind | Parameters | Notes |
|---|---|---|
| `earnings` | `dateFrom` (required), `dateTo`, `sort` | One day without `dateTo`; at most 90 days per request. Tried: Date (with time), Ticker, Company, Market Cap, EPS Estimate, EPS Actual, EPS Surprise, the same three for GAAP EPS, Revenue Estimate, Revenue Actual, Revenue Surprise, 1-Day Price Reaction. |
| `economic` | `dateFrom` (required), `dateTo` | `dateFrom` snaps to that week's Monday; one week without `dateTo`; at most 365 days. Tried: Event, Date, Time, Impact (a number), For, Actual, Expected, Prior. |
| `dividends` | `dateFrom` (required), `dateTo`, `sort` | One day without `dateTo`; at most 90 days. `sort`: `ticker` (default), `company`, `exdate`, `ordinary`, `special`, `yield`. Tried: Ticker, Company, Ex-Date, Amount, Special, Dividend Est. Yield. |

The earnings calendar reaches into the past, which is how saved days get an `earnings`
catalyst (`finviz earnings --sync`). Its `sort` takes `earningsDate` (default), `ticker`,
`company`, `marketCap`, `epsEstimate`, `epsActual`, `epsSurprise`, the three
`epsReported...` for GAAP, `salesEstimate`, `salesActual`, `salesSurprise`,
`oneDayPriceReaction`; `-` in front for descending.

### News: `/export/news`

| Parameter | Meaning |
|---|---|
| `v` | `1` market news by time, `2` by source, `3` stock news, `4` ETF news, `5` crypto |
| `c` | With the market views: `1` news only, `2` blogs only |
| `t` | Tickers, comma separated (with `v=3`, `4` or `5`) |
| `pid` | A saved portfolio's id, for its news |

Tried with `v=3&t=...`: Title, Source, Date, Url, Category, Ticker; 100 rows, which is the
full recent headline list for the tickers, not just the latest one. The market views
(`v=1`, `v=2`) have no Ticker column.

### Insiders: `/export/insiders`

| Parameter | Meaning |
|---|---|
| `t` | Ticker |
| `tc` | `1` buys, `2` sales, `4` option exercises, `7` all (default) |
| `or` | `10` ten-percent owners only, `-10` to exclude them |
| `tv` | Smallest transaction value in dollars |
| `oc` | The owner's SEC CIK |
| `o` | Sort: `accepted` (default), `ticker`, `transactionValue`, `transactionDate`, `transactionPricePerShare`, `transactionShares`, `sharesOwnedFollowingTransaction`; `-` for descending |

Without a ticker or owner filter only the last 7 days come back. 200 rows at most.
Tried with a ticker: Ticker, Owner, Owner CIK, Relationship, Date, Transaction, Cost,
#Shares, Value ($), #Shares Total, SEC Form 4, SEC Form 4 Link.

### Groups: `/export/groups`

`g` is the grouping (`sector`, `industry`, ...), `v=152` with `c` for columns, `o` to sort.
Group column ids are their own list: 0 No., 1 Name, 2 Market Cap, 3 P/E, 4 Forward P/E,
5 PEG, 6 P/S, 7 P/B, 8 P/C, 9 P/Free Cash Flow, 10 Dividend Yield, 11-13 EPS and sales
growth, 14 Float Short, 15-20 performance (week to year-to-date), 21 Analyst Recom,
22 Average Volume, 23 Relative Volume, 24 Change, 25 Volume, 26 Stocks, 27-36 debt,
return, liquidity and margin ratios, 37-38 three-year growth, 39 Enterprise Value,
40 EV/EBITDA, 41 EV/Sales, 42 Employees. Tried with `g=sector`.

### Portfolio: `/export/portfolio`

`pid=<portfolio id>`, `o` to sort, `c` for columns (0 Ticker, 1 Company, 2 Price,
3 Change %, 4 Volume, 5 Transaction, 6 Date, 7 Shares, 8 Cost, 9 Market Value, 10 Gain $,
11 Gain %, 12 Change $). Not tried: no portfolio is set up.

### Managers and funds: `/export/managers`, `/export/funds`

- List: `search=<part of a name>`, `sort` (`-most_popular` default, `-portfolio_value`,
  `-num_investments`, `-new_purchased`, `-sold_out`, `-added`, `-reduced`,
  `-top_ten_concentration`, `-turnover`, `-time_held_top10`, `-time_held_all`).
- Holdings: `/export/managers/<investor id>/holdings` or `/export/funds/<series id>/holdings`,
  with `reportEndDate=YYYY-MM-DD`, `transactionType` (`new`, `added`, `reduced`,
  `closed_out`, `no_change`), `sort` (`-valueUsd` default, `issuerTicker`, `shares`,
  `percentageOfPortfolio`, `changeInShares`, `percentChangeFromPrevious`, `percOwnership`).
  Managers also take `optionType` (`share`, `put`, `call`); funds take `assetCategory`
  (SEC N-PORT codes such as `EC` equity-common, `DBT` debt) and more sort keys
  (`issuerName`, `assetCategory`, `shareType`, `previousPercentageOfPortfolio`,
  `payOffProfile`, `investmentCountry`).

Tried. A list has 500 rows at most: Name, Portfolio Manager (or Series Name), Investor ID,
Report Date, Portfolio Value, # Investments, New Purchased, Sold Out, Added, Reduced, Top
10 Concentration (%), Turnover (%), Time Held Top 10, Time Held All. There is no slug
column: holdings are asked for by the Investor ID (digits for a manager, `S` and digits
for a fund), or by the name-and-id slug in the site's address bar. Holdings are not
capped. A manager's: Ticker, Name, Type, Sector, Industry, Shares Held, Market Value, %
Portfolio, Prev. % Portfolio, Change in Shares, % Change, % Ownership, Avg. Price, Q 1st
Owned.

### Futures, forex, crypto: `/export/futures/performance`, `/export/forex/performance`, `/export/crypto/performance`

`sort` takes a performance column (`-perfDayPct` default, `-perf5minPct`, `-perfHourPct`,
`-perfWeekPct`, `-perfMonthPct`, `-perfMtdPct`, `-perfQuarterPct`, `-perfHalfYearPct`,
`-perfYtdPct`, `-perfYearPct`) or `ticker`, `name`, `-last`. Forex also takes `unit=pips`, and
then every sort key ends in `Pips` instead of `Pct`. Crypto takes `c=USD|USDT|EUR|BTC` for
the currency prices are quoted in. Tried: No., Ticker, Name, Price, and Performance over
5 minutes, 1 hour, day, week, month, month to date, quarter, half year, year to date and
year (`Performance in Pips (...)` with `unit=pips`).

## What the CLI covers

`screen`, `snapshot`, `categories`, `movers`, `ticker`, `splits` use the screener.
`bars`, `intraday`, `options`, `statements`, `short-interest`, `filings` use the stock
endpoint. `earnings`, `economic`, `dividends` are the calendars; `news`, `insiders`,
`groups`, `futures`, `forex`, `crypto`, `managers`, `funds` and `holdings` have their own
commands. The portfolio export has none (it was not tried: it needs a saved portfolio);
it and anything else are reachable with `finviz export <path> key=value ...`.
