# Enrichment: the columns this CLI adds

Finviz's own columns are listed in [columns.md](columns.md). Everything below is computed
here. Percent values are plain numbers (12.5 means 12.5%).

## From one row

| Column | Meaning |
|---|---|
| `dollar_vol` | price x volume |
| `rotation` | volume / float: how many times the float traded |
| `range_pct` | (high - low) / low |
| `high_vs_open`, `low_vs_open` | how far the high and the low were from the open |
| `close_pos` | where the price sits in the day's range: 0 at the low, 1 at the high |
| `from_high` | percent from the day's high: 0 at the high, negative below it (-20 is 20% under) |
| `kept` | share of the run above the open still there at the close, from the prices: 1 is a close at the high, below 0 is under the open; empty when the high never passed the open |
| `atr_pct` | ATR as a percent of price |

## Labels for the day

| Column | Values | Rule |
|---|---|---|
| `opening` | `gap up`, `flat open`, `gap down` | open 5%+ above, within 5% of, or 5%+ below the previous close |
| `day` | `ran and held` | high 10%+ above the open and the close kept at least half of it |
| | `ran, gave it back` | high 10%+ above the open and the close kept less than half, or went under the open |
| | `faded` | never ran 10%, closed 5%+ below the open |
| | `held` | none of the above |
| `shape` | the two joined, e.g. `flat open, ran and held` | |
| `mover` | true / false | price $1+, $1M+ traded, 2x normal volume, and a 10% up-move somewhere (high vs open, close vs previous close, or gap) |
| `close_kind` | `strong close` | closed up 10%+ on the day and in the top 30% of its range |
| | `weak close` | closed in the bottom 30% of its range |
| | `in between` | anything else |
| `catalyst` | `earnings` | reported between the previous close and this close (row's earnings date, or the saved earnings calendar) |
| | `news` | latest headline is dated between the previous close and this close |
| | `earlier` | latest headline is from the week before the previous close |
| | `after close` | latest headline came after this close. It cannot explain the session, and it hides whatever came before it, because Finviz gives one headline per name |
| | `none` | nothing in that window |
| `news_age` | days | since the latest headline; 0 is that day, negative is a later day |
| `days_to_earn` | days | to the row's earnings date; negative is days since. Whole-market rows only. |
| `split_headline` | true / false | the latest headline, out by this close and at most 30 days old, mentions a reverse split or share consolidation |

The shapes are only known after the close. They describe a finished day.

"This close" is 16:00 ET, or 13:00 on an early-close session (the day after Thanksgiving,
Christmas Eve and July 3rd, when those are sessions).

Percentages worked out here are rounded to six decimals before they are compared, so a
high of 3.30 on an open of 3.00 is a 10% run. "Kept half of the run" and `kept` are worked
out from the prices themselves (close minus open against high minus open), because
Finviz's own change-from-open is rounded to two decimals: on an open of 7.00, a high of
8.00 and a close of 7.50 it says 7.14%, and half of the run is 7.142857%.

`split_headline` only counts a headline that was out by this close.

## From earlier sessions

These need saved days (`finviz history status`).

| Column | Meaning |
|---|---|
| `yesterday` | What the name did the session before: `mover, strong close`, `mover, in between`, `mover, weak close`, `traded, not a mover`, `quiet (under 50k shares)`, `not covered` (that day's source stopped at a smaller float, or this name has no float on record), `no history` (that session is not saved) |
| `streak` | mover days in the 10 sessions before this one |
| `days_since_split` | sessions since the name's last reverse split; 0 is the first session at the new share count; empty if none was seen |
| `split_ratio` | size of that split: 10 means 1-for-10 |

Sessions are counted on the trading calendar, not on which days happen to be saved:

- "The session before" is the calendar's. If it is not in the store, `yesterday` is
  `no history`.
- `streak` looks at the 10 calendar sessions before the row. A session missing from the
  store simply contributes nothing, so a gap never pulls in mover days from a month ago.
- `days_since_split` is the number of calendar sessions since the split.

The split clock is read from one index built over every saved day, so a row shows the same
`days_since_split` and `split_ratio` whether it was asked for by date, by range or with
`--history`. A split is found by comparing a name's row with its own row before it, however
far back that is. `finviz splits` lists the same index.

A stock with no float on record (a new listing, often) is kept, with `float_m` and
`rotation` empty. It can still be a mover.

## Inside the day

`finviz intraday SYM...` reads Finviz's minute bars (1, 3 or 5 minutes) and gives one row
per session. Times are Eastern and are the minute a bar starts. The regular session is
09:30 up to the close (16:00, or 13:00 on an early close); every bar before 09:30 is
premarket and every bar from the close on is after hours.

| Column | Meaning |
|---|---|
| `pm_high`, `pm_high_at` | highest premarket price, and the first bar that reached it; empty when nothing traded before the open |
| `open` | the open of the first bar of the regular session |
| `high`, `high_at`, `low`, `low_at` | the session's high and low, and the first bar that reached each |
| `close`, `last_at` | the close of the last bar of the regular session, and when that bar began. While the session is trading it is the last price so far |
| `high_vs_open`, `low_vs_open`, `open_chg` | the high, the low and the close against the open, in percent |
| `kept`, `close_pos` | as under "From one row", from these prices |
| `pmh_break_at` | the first regular-session bar whose high was above the premarket high; empty when none was, or when there was no premarket |
| `ah_close`, `ah_chg` | the last price after the close, and that against the close |
| `vol` | the whole day's volume, from the daily bar |
| `day_high`, `day_low` | the daily bar's own high and low |
| `bar_pm_vol`, `bar_vol`, `bar_ah_vol` | the minute bars' volume before the open, in the session and after the close |
| `bar_cover` | those three together as a share of `vol` |
| `bars` | how many bars the regular session has |
| `live` | true while the session has not closed |

What to know when reading it:

- The prices and times come from the minute bars; `vol`, `day_high` and `day_low` come from
  the daily bar, which is a different feed. The minute bars hold only part of the day's
  volume (`bar_cover`, usually a half to three-quarters), and less of it before the open
  than in the session. So the `bar_` columns are a rough guide to where in the day the
  volume came, with the premarket looking smaller than it was, and not a count of how much
  there was. On a thin day the minute bars also reach a little beyond the daily bar's high
  and low. Numbers for both are in [quirks.md](quirks.md), "Minute bars are a different
  feed from the daily bar".
- A session with no bars has no row. Bars dated on a day that is not a session are left
  out, and the command says how many.
- A bar with a missing time, price or volume, with a figure no price or volume can be, or
  with prices that contradict each other (a high under its own open or close), stops the
  command rather than being skipped: the bar skipped could be the day's high. So do two
  bars carrying the same time. The daily bar's figures are the other way round: one that
  cannot be read is left empty.
- `live` is judged as of the moment the minute bars were asked for.
- With 3- or 5-minute bars a time is the start of a bar that long.
- One-minute and three-minute bars reach ten sessions back, five-minute bars fifteen.
