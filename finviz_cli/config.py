"""Where things live, and the market clock."""
from __future__ import annotations

import datetime as dt
import os
import zoneinfo
from pathlib import Path

from finviz_cli import sessions

ET = zoneinfo.ZoneInfo("America/New_York")
SETTLE_MINUTES = 5              # late prints keep arriving for a few minutes after the close


def home() -> Path:
    """Data folder: $FINVIZ_CLI_HOME, else ~/.finviz-cli. Market data, and a .env if its owner puts one there."""
    return Path(os.environ.get("FINVIZ_CLI_HOME") or Path.home() / ".finviz-cli")


def snapshots_dir() -> Path:
    return home() / "snapshots"


def days_dir() -> Path:
    return home() / "days"


def now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def close_minute(day: dt.date) -> int:
    """Minutes after midnight ET at which the session on `day` closes (960, or 780 on an early close)."""
    t = sessions.close_time(day)
    return t.hour * 60 + t.minute


def record_from(day: dt.date) -> dt.datetime:
    """From this moment a pull carries the session's close: its closing time plus a few minutes."""
    return dt.datetime.combine(day, sessions.close_time(day), tzinfo=ET) + dt.timedelta(minutes=SETTLE_MINUTES)


def is_after_close(when: dt.datetime) -> bool:
    """True when `when` falls on a session and that session's close has settled."""
    return sessions.is_session(when.date()) and when >= record_from(when.date())
