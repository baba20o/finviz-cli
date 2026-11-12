"""Printing: a table for a person at a terminal, one JSON object per line for a program."""
from __future__ import annotations

import contextlib
import contextvars
import json
import math
import sys

import pandas as pd

FORMATS = ("auto", "table", "ndjson", "json", "csv")
BIG_COUNTS = ("vol", "avgvol", "ah_vol", "dollar_vol", "bar_pm_vol", "bar_vol", "bar_ah_vol")
PRICES = ("price", "open", "high", "low", "close", "prev_close", "pm_high", "ah_close", "day_high", "day_low")
FINE = ("iv", "delta", "gamma", "theta", "vega", "rho")       # small by nature: an option's gamma of 0.0008 is not 0.00


def pick(fmt: str) -> str:
    if fmt != "auto":
        return fmt
    return "table" if sys.stdout.isatty() else "ndjson"


def _clean(value):
    """A JSON-safe value: missing becomes null, numpy scalars become Python numbers."""
    if value is None or value is pd.NaT:
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 6) if abs(value) >= 1 else float(f"{value:.6g}")   # keep small numbers' digits
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat(sep=" ")
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def records(df: pd.DataFrame) -> list[dict]:
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict("records")]


def _is_text(series: pd.Series) -> bool:
    return not (pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series))


class Captured:
    """What a command would have printed: its rows, and its notes."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.notes: list[str] = []


_sink: contextvars.ContextVar[Captured | None] = contextvars.ContextVar("finviz_output_sink", default=None)


@contextlib.contextmanager
def capture():
    """Inside this block emit() and note() hand what they have to the caller instead of
    printing it. For a program that runs a command and wants the rows themselves, such as
    the MCP server, whose stdout carries its protocol and must hold nothing else. It is kept
    per thread and per task, so two calls at once do not see each other's rows."""
    got = Captured()
    token = _sink.set(got)
    try:
        yield got
    finally:
        _sink.reset(token)


def emit(df: pd.DataFrame, fmt: str = "auto", wide_text: int = 60) -> None:
    got = _sink.get()
    if got is not None:
        got.rows.extend(records(df))
        return
    fmt = pick(fmt)
    out = sys.stdout
    if fmt == "ndjson":
        for row in records(df):
            out.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
    elif fmt == "json":
        out.write(json.dumps(records(df), indent=1, default=str) + "\n")
    elif fmt == "csv":
        out.write(df.to_csv(index=False, lineterminator="\n"))
    else:
        if df.empty:
            out.write("(no rows)\n")
            return
        show = df.copy()
        rename = {}
        for col in list(show.columns):
            if col in BIG_COUNTS and pd.api.types.is_numeric_dtype(show[col]):
                # Under a thousand the count itself: 350 shares are not "0k".
                show[col] = show[col].map(lambda v: "" if pd.isna(v) else (f"{v / 1e6:.1f}M" if abs(v) >= 1e6 else
                                                                         f"{v / 1e3:.0f}k" if abs(v) >= 1e3 else f"{v:.0f}")).astype(object)
            elif _is_text(show[col]):
                text = show[col].astype(object).where(show[col].notna(), "").astype(str).str.slice(0, wide_text)
                width = max(int(text.str.len().max() or 0), len(str(col)))
                show[col] = text.str.ljust(width).astype(object)        # text reads left to right
                rename[col] = str(col).ljust(width)
        show = show.rename(columns=rename)
        with pd.option_context("display.width", 320, "display.max_colwidth", wide_text + 2, "display.max_rows", 2000):
            out.write(show.round(_places(show)).to_string(index=False) + "\n")


def _places(table: pd.DataFrame) -> dict:
    """Decimals for each number column of a table view: two, except that a price column
    holding prices under a dollar keeps four, and under a cent six. 0.0042 is not 0.00."""
    places = {}
    for col in table.columns:
        if not pd.api.types.is_float_dtype(table[col]):
            continue
        places[col] = 4 if col in FINE else 2
        if col in PRICES:
            sizes = table[col].abs()
            smallest = sizes[sizes > 0].min()
            if smallest < 0.01:
                places[col] = 6
            elif smallest < 1:
                places[col] = 4
    return places


def note(message: str) -> None:
    """Context lines (what data, how many rows) go to stderr so piped output stays clean."""
    got = _sink.get()
    if got is not None:
        got.notes.append(message.rstrip())
        return
    sys.stderr.write(message.rstrip() + "\n")
