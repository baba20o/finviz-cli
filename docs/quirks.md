# Things Finviz data does that will bite you

Each of these was seen in the data, not read somewhere. Dates are when it was seen.

## Time of day changes what the columns mean

- **Before 09:30 ET the price columns are premarket values.** `Price`, `Change`, `High`,
  `Low` and `Open` describe the premarket session, so `High` in the last pull before the
  open is the premarket high. They reset at the open. `Volume` before the open is the
  premarket's volume so far, and it is the one that does not reset: see "`Volume` is the
  whole day so far" below. (2026-11-10, corrected 2026-11-11)
- **After 16:00 ET the price columns stop following trades, but the close is not final at
  once.** `Price` holds the close; after-hours prices show up only in `After-Hours Close`
  and `After-Hours Change`. For most names the close is in within minutes. For thin names
  it is not. Two pulls on 2026-11-11, at 16:23 and at 17:01, disagreed on `Price` for 557
  of 5,596 names, a tenth of them: by 0.5% at the median and by up to 15%. For the six
  largest differences the later value was the daily bar's close, and all six had traded
  fewer than 20,000 shares. (2026-11-10, 2026-11-11)
- **`Volume` is the whole day so far, not the session.** It is not reset at the open:
  fifteen seconds after the open on 2026-11-11, OMH's `Volume` was already 5.05M, which was
  its premarket. And it goes on counting after the close. Between the same two pulls
  (16:23 and 17:01) `Volume` rose on 3,011 names and fell on none. On the 311 names whose
  `After-Hours Volume` grew by more than 50,000 shares in that time, `Volume` grew by the
  same amount: the ratio of the two increases was 1.00 at the median and at both
  quartiles. Of 186 names with no after-hours volume, `Volume` changed on 3. So `Volume`
  less `After-Hours Volume` is the day up to the close, and on a name that trades after
  hours `Volume`, and what is built on it (`relvol`, `rotation`, `dollar_vol`), grows with
  the hour of the pull. (2026-11-11)
- **So an early pull and a late one are each right about one thing.** A pull just after
  16:05 has the least after-hours trading in its `Volume`, but not yet every close. A
  later one has the closes, and more after-hours trading in its `Volume`. The CLI files a
  pull as it is, from 16:05 on; it does not yet subtract `After-Hours Volume`.
- **A pull between midnight and 04:00 ET describes the previous session.** The CLI files
  it under that session's date.

## Units

- `Average Volume` is in **thousands** in an export (`1008.22` is 1,008,220 shares). The
  CLI converts it to shares. Rows brought in with `finviz history import-lfr` already hold
  shares.
- `Market Cap`, `Shares Float`, `Shares Outstanding`, `Short Interest`, `Income` and
  `Sales` are in millions.
- Percent columns arrive as text with a `%` sign and become plain numbers (12.5).
- The earnings calendar's `Market Cap` is in millions with many decimals; revenue
  estimates and actuals are in millions.

## Holidays

On a market holiday the feed repeats the last session, so a file saved that day is a copy
of the session before (2026-09-07, Labor Day, was one). Two guards:

- `sessions.py` knows the regular NYSE holidays by rule, so those dates are never treated
  as sessions, a pull on one is filed under the session before, and "the session before"
  skips over them.
- An unscheduled closure is caught from the data: a day carrying the same previous closes
  as the saved day before it is dropped, and a pull that looks like that is set aside as
  `_rejected`: it is not filed as a day record and not shown. The day after such a closure
  shows `yesterday = no history`. The check is redone when either of the two files is
  replaced.

## Early closes

Three sessions a year can end at 13:00 ET: the day after Thanksgiving, Christmas Eve and
July 3rd, when they are sessions (checked against NYSE's published 2026 to 2028 calendar).
Everything that depends on the close uses that time: the catalyst windows, when a pull
from before the close stops being reused (13:00), when a pull is filed as the day's record
(13:05 instead of 16:05), and the earliest capture time an imported end-of-day file may
carry. This is by rule. It has not been watched on a real early close yet; the next one is 2026-11-27, and the thing
to confirm is that Finviz's price columns freeze at 13:00 as they do at 16:00.

The calendar rules are today's; they are right from 2022 on (Juneteenth was first observed
that year). A one-off closure is not in them: it is caught from the data, but it still
counts as a session when sessions are counted.

## A pull is checked before it is used

A whole-market pull has to look like one:

- the columns every label is built from are there (a float may be blank, its column not);
- the answer arrived whole: the last line ends, and every row has the header's columns;
- at least 2,000 rows, a price and previous close on 90% of them, and tickers that do not
  repeat (up to 1% may; the repeats are dropped when the file is read);
- at least 90% of the rows already filed for the same session, and of the last filed day;
- not a repeat of the saved day before (judged only when the two share 50 or more tickers);
- if it was taken after the session's close had settled (that evening, overnight, at the
  weekend; filed as the day's record or not), the day itself on 90% of rows: open, high,
  low, volume, change, gap, change from open and relative volume.

Rows are counted as table rows, not lines of text, and quoting is read strictly, so an
error page with thousands of lines does not pass and neither does an answer that stops
inside a quoted field. A pull that fails is saved as `<time>_rejected.csv` to look at. It
is never shown, never reused and never filed, and the command that made it says why and
exits with code 3.

What the checks cannot do is prove nothing is missing. An answer cut exactly at the end of
a row, with its line end, and still within 10% of the last filed day's size, looks whole.
The connection layer normally catches a short answer before it gets here.

A pull that passes is filed as the day's record only if it is the latest one kept for the
session. Two commands can overlap and finish out of order; the older answer is kept as a
snapshot but does not replace the newer record. A pull is dated by the moment its request
left (after any waiting in the queue), to the microsecond, and snapshots are named by
that: it is when the data is from, whichever download finishes first.

If the clock passes 04:00, the open, the close or the close's settling between the request
leaving and its answer being complete, it is not clear which side the data is from, so the
pull is made again. After three such pulls in a row the command stops with exit code 3 and
saves nothing.

Snapshots saved before these checks existed have no seconds in their name. One of those is
judged by the same rules the first time it would be reused, and renamed `_rejected` if it
fails.

The client also refuses an answer that is not data before it gets that far: markup of any
kind, an HTML content type, an empty body, or text that repeats the request. That means
the token, matched character by character so that any part of it may be percent-encoded
(once or more, in either letter case, a space as `+`), or the request's own address with
its `auth` parameter: a URL on a Finviz host, or the export path standing on its own. A
link in the data (written with `http://`, `https://` or just `//`) is judged by its host,
so someone else's link with an `auth` parameter of its own, or one that merely mentions
finviz.com or carries a path like ours in its query, is left alone. A token written in some
other transformation altogether (base64, say) would not be recognised.

## A pull goes stale at the open, at the close, and when the close has settled

Before 09:30 the price columns are premarket values, and they reset at the open. So a pull
taken at 09:29 is not reused at 09:31, however fresh it is. The same at the close (16:00,
or 13:00 on an early close): a pull from 15:56 is not shown at 16:02. And again five
minutes later, when late prints are in and a pull can be filed as the day's record.
Between those moments a pull is reused for ten minutes; one taken after the close has
settled is reused until the next session's premarket.

A pull taken after a session's close has settled must have that day in it (open, high,
low, volume, change, gap, change from open, relative volume on 90% of rows), whenever it
was taken: that evening, overnight, or over the weekend.

## A changed file is noticed by its size, times and identity, not its contents

The store decides that a day's file changed from its size, modification time, change time
(creation time on Windows, the inode's change time elsewhere) and its identity on the
volume (inode or NTFS file ID). That catches a file being replaced by another, including
by a restore that put the old times back. It is not a hash of the contents: reading 640 MB
of days on every command would cost a second each time. What it cannot see is the same
file rewritten in place with contents of exactly the same length and its times then set
back. If that ever happens, delete `<home>/cache` and `<home>/days/closed_days.txt`; both
are rebuilt.

## An earnings report belongs to the session whose window it falls in

A session's earnings window runs from the previous session's close to its own. A report on
Wednesday at 16:30 is Thursday's catalyst. So `finviz earnings --sync` starts at the
session before the first saved day, not at that day.

Saving a calendar replaces the saved rows for the dates that were asked for. So the answer
is checked before anything is replaced: it must have arrived whole (strict quoting, every
row the header's width, a final line end), carry its Date and Ticker columns, and hold
only dates inside the range asked for. Otherwise the saved calendar is left as it was.

## Earnings times are kept as plain Eastern time

Whatever form a report time arrives in (Finviz writes Eastern time without a zone; a time
with a UTC offset is converted), the calendar on disk holds `YYYY-MM-DD HH:MM` in Eastern
time. Pieces saved on either side of a clock change therefore read as one calendar.

## Blank floats

About 3% of the whole-market rows have no `Shares Float` (176 of 5,609 on 2026-11-10: new
listings, shells, some ADRs). They are kept: on that day 88 of them traded 50k+ shares.

## Gaps in the saved days

"The session before" comes from the calendar, not from which days are saved. When that
session is missing from the store, the next day's `yesterday` is
`no history` rather than a guess from an older day, and a price jump across the gap only
counts as a reverse split if the float shrank with it.

## Reverse splits

- On the first session after a reverse split, `Prev Close` is already at the new share
  count while yesterday's saved `Price` is at the old one. A jump of 1.8x or more between
  the two is a split, and the float nearly always shrinks the same day. (2026-11-10)
- Finviz's saved prices are as traded on the day. Price bars from a broker are usually
  adjusted for later splits, so the two disagree by the split factor for older dates.
- Names do split twice within weeks (CDT: 1-for-24 on 2026-09-29, 1-for-21 on 2026-11-06).

## Intraday bar timestamps

Intraday price history writes a 24-hour clock **and** an AM/PM suffix: `11/10/2026 17:25 PM`.
The CLI drops the suffix. Daily bars are `MM/DD/YYYY`. If any bar's time cannot be read the
command fails and says how many, rather than printing bars with a blank time.

An answer with no Date column is refused: it is some other answer (an error text reads as
a table of one column and no rows), not "no bars". So is one whose last line does not end:
it was cut short, and what is left of its last number would read as a smaller one.

## Minute bars are a different feed from the daily bar

Measured on 2026-11-11 over 80 finished ticker-days: eight names, from SPY and AAPL down
to floats under 3M, for the ten sessions 2026-10-28 to 2026-11-10.

- **Which bars have the hours around the session.** One-, three- and five-minute bars run
  from 04:00 to 20:00. Fifteen- and thirty-minute bars hold 09:30 to 16:00 only. That is
  why `finviz intraday` offers only the first three.
- **How far back.** With no range given, one- and three-minute bars start at 04:00 ten
  sessions before today, five- and fifteen-minute bars fifteen sessions before, and
  thirty-minute bars about 200 sessions before. The window is whole days.
- **Volume: the minute bars hold part of it.** All of a day's minute bars, 04:00 to 20:00,
  added up to between 0.48 and 0.88 of the daily bar's volume, 0.65 at the median. The
  daily bar agrees with the screener's `Volume`. Before the open the share was lower
  still: OMH's premarket on 2026-11-11 was 2.08M in the bars and 5.05M on the screener. So
  the share is not even across the day, and the bars give only a rough picture of where in
  the day the volume came (the premarket's part looks smaller than it was). How much there
  was is read from the screener or the daily bar.
- **Prices: the open and the close agree, the high and the low nearly.** The first regular
  bar's open and the last one's close equalled the daily bar's open and close on all 80
  days. The high differed on 31 and the low on 22, by 0.7% and 0.3% at the median. The
  large differences, up to 8%, were all on thin days (a few thousand to 90,000 shares),
  where the minute bars reach beyond the daily bar's high and low: they seem to include
  prints the official high and low leave out, odd lots most likely. On days of a million
  shares or more the two were within 1.2%.

## The latest headline is only the latest

The screener's `News Time` and `News Title` hold one headline per ticker, the newest. A
name with heavy news loses the earlier catalyst. Use `finviz news TICKER` for the full
recent list.

It keeps updating after the close while the price columns stay frozen, so a day record
pulled in the evening carries after-hours headlines. Those rows get `catalyst = after close`
instead of `news`. The later the pull, the more of them there are; a pull at 16:05 has the
fewest.

## The rate limit is per account, the pacing is per machine

Finviz allows 10 requests per rolling 5 seconds for the whole account. Every `finviz`
process that uses the same data folder shares a lock and a slot file there (`.pace.lock`,
`.next_request`), so they queue behind each other at 0.75 seconds apart and all sit out a
429's cooldown together. Anything else using the same token cannot be seen from here:
another machine, another user on this one, or a process pointed at a different data
folder. Against those the only
protection is backing off when Finviz answers 429.

Limits, all deliberate:

- A queue is kept up to ten minutes long (about 800 requests). A slot further ahead than
  that is taken for a damaged file and ignored, so a queue that really did grow past ten
  minutes would be jumped.
- If the data folder cannot be written to, or the lock file is there but not writable by
  this user, there is nowhere to keep the lock. That is found out at once, by trying to
  make a file there and by the reason the system gives for refusing the lock file (access
  denied is final; a file in use elsewhere is waited for). Requests are then paced inside each process only, and a
  warning says so (once for the lock, once for the slot file if that cannot be written
  either). Commands that write to the store fail and say the folder cannot be written to.
- A request can leave in the instant after another process published a cooldown. Closing
  that would mean holding the lock for the whole request, which would queue every process
  behind the slowest answer. The cost is one more 429, handled like any other.
- A request gives up its slot and takes a new one at most six times (when it wakes late,
  or a cooldown lands while it waits). After that it goes.

## What the history does not have

- A day brought in with `finviz history import-lfr` holds only what its collector kept: in
  the files this was written for, floats under 100M and names that traded 50k+ shares. A
  name missing from such a day was either quiet or too big; the CLI says `not covered`
  when it cannot tell.
- Those days carry no earnings date. The earnings calendar fills that in.

## Old URLs

`export.ashx` and friends redirect to the new paths. Python's `requests` follows the
redirect; `curl` without `-L` returns nothing.
