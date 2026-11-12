# finviz-cli

Finviz Elite data from the command line, with a local history of trading days and a set of
computed columns for studying stocks in play. Built for people at a terminal and for
programs and agents reading its output.

This is an independent, unofficial project. It is not affiliated with or endorsed by
Finviz. It needs your own [Finviz Elite](https://finviz.com/elite) subscription and its API
token, and it uses only the export endpoints Finviz documents for Elite subscribers.

```bash
finviz screen "abs(gap) <= 3 and open_chg >= 10 and relvol >= 3"
```

```bash
finviz intraday NVDA --days 3
```

## Install

```bash
pip install git+https://github.com/baba20o/finviz-cli
```

Or from a clone, with the test tools:

```bash
pip install -e ".[dev]"
```

Python 3.10 or newer.

## Token

Your token is shown on the API pages of your Elite account. Put it in a `.env` file
([.env.example](.env.example) is a template):

```
FINVIZ_API_KEY=your-token
```

`finviz` takes the token from the first of these that has one:

1. `FINVIZ_API_KEY` in the environment;
2. a `.env` file in the folder you run the command from;
3. a `.env` file in the data folder (`~/.finviz-cli/.env`), which works from any folder.

The token goes in the request URL, so it is treated as a secret everywhere: the CLI never
writes it to disk, prints it, or puts it in an error message. `finviz doctor` checks that
it works without showing it. Keep your `.env` out of version control; this repository's
`.gitignore` already leaves it out.

If you keep the token in AWS Secrets Manager instead, set `FINVIZ_SECRET_ID` to the
secret's name, and `AWS_REGION` if it is not the one in your AWS settings (both in the
environment or in the data folder's `.env`), and install the `aws` extra, which adds boto3.

## Commands

| Command | What it does |
|---|---|
| `finviz screen "<filter>"` | Any expression over the columns, on today's pull, one saved day (`--date`) or all of them (`--history`) |
| `finviz categories` | Movers grouped by the outline of their day and what set them off |
| `finviz movers` | The day's movers with how each closed: a watchlist for the next session |
| `finviz ticker SYM...` | A ticker across the saved days |
| `finviz intraday SYM...` | Each recent session from minute bars: premarket high, open, high and low with their times, close, how much of the run was kept, when the premarket high was passed |
| `finviz splits` | Reverse splits found in the saved days |
| `finviz snapshot` | Pull the screener now and keep it; from five minutes after the close it becomes the day's record |
| `finviz earnings` | Earnings calendar; `--sync` fills the store so saved days get an earnings catalyst |
| `finviz bars SYM --tf i1 --range d5` | Price history |
| `finviz options SYM`, `statements SYM` | The options chain; a financial statement, with its currency |
| `finviz news`, `short-interest`, `filings`, `insiders` | Headlines, short interest, SEC filings and insider transactions |
| `finviz economic`, `dividends` | The economic and the dividend calendar |
| `finviz groups`, `futures`, `forex`, `crypto` | Sector and industry totals; how futures, currencies and coins have moved |
| `finviz managers`, `funds`, `holdings ID` | Institutional managers and funds, and what one of them holds |
| `finviz export <path> key=value ...` | Any export endpoint without its own command |
| `finviz columns` | Screener columns: Finviz id, our key, header. `--screen` lists the columns a filter can name, with their meaning |
| `finviz history status` / `import-lfr FOLDER` | What is saved; bring in end-of-day files saved by a separate collector (only ones named for their own date and captured after the close) |
| `finviz doctor` | Token, connection and store check |

`finviz COMMAND --help` describes each one.

Output is a table at a terminal and one JSON object per line when piped. `--format
table|ndjson|json|csv` overrides that. Notes about what was loaded go to stderr, so piped
output is only the rows.

A filter is an expression over the column names. Text works too:

```bash
finviz screen "yesterday == 'mover, weak close' and float_m < 20" --date 2026-11-09
```

```bash
finviz screen "news.str.contains('Reverse Stock Split')" --history --show date,ticker,price,chg,news
```

A filter can compare and combine columns, do arithmetic, and call a short list of column
methods (`abs`, `isin`, `between`, `isna`, `.str.contains` and a few more), and it has to
come to true or false for each row. The CLI parses it, checks it and works it out itself;
the text is never handed to pandas or to anything else to evaluate. A filter is data, not
code, whether a person typed it or a model wrote it.

## For agents

The CLI is built to be driven by a language model as much as by a person: stable column
names, one JSON object per line, notes kept apart from rows, exit codes that mean
something, and errors that say what to fix.

`finviz-mcp` serves the same commands as tools over the Model Context Protocol:

```bash
pip install "finviz-cli[mcp] @ git+https://github.com/baba20o/finviz-cli"
```

```json
{"mcpServers": {"finviz": {"command": "finviz-mcp"}}}
```

There are 21 tools, all read-only. Each returns the same shape (`ok`, `rows`, `row_count`,
`notes`, and `error` or `truncated` when they apply), caps its rows, and leaves empty fields
out. A failure comes back as a result that says why, never as a traceback. Values supplied
by the model can never become command switches.

[docs/agent-guide.md](docs/agent-guide.md) is the guide written for the model itself: which tool
answers which question, how to write a filter, and how to read the numbers without being
misled by them.

## What it adds to Finviz's columns

- From one row: dollar volume, float rotation, range, where the price sits in the day's
  range, how much of a run above the open was kept.
- Labels for a finished day: how it opened, what it did, how it closed, whether it was a
  mover, and what set it off (earnings, news, nothing).
- From earlier saved days: what the name did the session before, how many mover days it
  had in the last ten sessions, and how long ago its last reverse split was.
- From minute bars (`finviz intraday`): the premarket high, the times of the high and the
  low, and when the premarket high was passed.

The exact rules are in [docs/enrichment.md](docs/enrichment.md). The labels describe what
happened. They are not trading rules, and nothing here is investment advice.

## The store

`~/.finviz-cli` (or `$FINVIZ_CLI_HOME`) holds one file per trading day, every snapshot,
the earnings calendar and a parse cache. That is market data only; the one thing in the
folder that can be a secret is a `.env` you put there yourself. A day taken by this CLI
covers every listed stock, including the ones with no float on record; a day brought in
with `import-lfr` covers whatever its collector kept. See [docs/quirks.md](docs/quirks.md)
for what that means in practice.

Commands that need today's data reuse the latest whole-market pull when it is still good:
for ten minutes while the session is open, and all evening once it was taken after the
close. A morning pull is never used in place of the close. `--refresh` forces a new one.

Every whole-market pull is checked before it is used (columns, complete rows, row count,
unique tickers, prices, coverage against what is already filed, not a repeat of the day
before). One that passes on a trading day, five minutes or more after the close (16:05 ET,
13:05 on an early close), is filed as that day's record. One that fails is kept as
`<time>_rejected.csv`, is never shown or filed, and the command says why.

Exit codes: 0 fine, 1 an error, 2 a usage error or a Finviz failure, 3 a pull or an import
that was rejected (from any command that met it).

## How it treats Finviz's API

- Finviz allows 10 requests per 5 seconds per account, shared by everything that uses the
  token. The client paces itself at one request per 0.75 s, across every `finviz` process
  on the machine, through a small file in the data folder. After a 429 it waits out the
  time Finviz asks for.
- One whole-market request serves every screening command for as long as it is still
  good, so a session of screening costs a handful of requests, not one per question.
- Errors are one line with the token taken out of anything in them, and with Finviz's own
  reason when it gives one (`Ticker not found.`). `FINVIZ_CLI_DEBUG=1` adds a traceback,
  redacted the same way.

The data belongs to Finviz and its providers. What you may do with it is set by Finviz's
terms for your subscription, not by this project's license.

## Docs

- [docs/agent-guide.md](docs/agent-guide.md): for a language model using the CLI, and for whoever wires it up
- [docs/api-reference.md](docs/api-reference.md): the Finviz export endpoints, in our words, with what was tried
- [docs/columns.md](docs/columns.md): every screener column (generated: `finviz columns --markdown`)
- [docs/enrichment.md](docs/enrichment.md): the columns this CLI adds and their exact rules
- [docs/quirks.md](docs/quirks.md): what the data does that will bite you

## Development

```bash
python -m pytest
```

Tests use made-up rows and a fake client. None of them touches Finviz, the real store, or
your token.

## License

MIT. See [LICENSE](LICENSE).
