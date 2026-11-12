"""Turn Finviz rows into a typed table with the same column names everywhere."""
from __future__ import annotations

import csv
import io

import numpy as np
import pandas as pd

from finviz_cli import columns as C


def parse_table(text: str, strict: bool = False) -> tuple[list[str], list[dict]]:
    """(headers, rows). The headers are kept apart so a table with no rows still has columns.
    `strict` raises csv.Error on broken quoting (a quoted field that never closes) instead of
    making the best of it."""
    reader = csv.DictReader(io.StringIO(text), strict=strict)
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def parse_csv(text: str) -> list[dict]:
    return parse_table(text)[1]


def named_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Without the nameless column a row with more fields than the header leaves behind."""
    keep = [isinstance(c, str) for c in df.columns]
    return df if all(keep) else df.loc[:, keep]


def to_frame(rows: list[dict], avgvol_in_thousands: bool, headers: list[str] | None = None) -> pd.DataFrame:
    """Rows keyed by Finviz headers -> DataFrame keyed by our short names, numbers as numbers.

    Percent columns come as '12.5%' and become 12.5. Average volume arrives in thousands from
    the export and in shares from a collector's saved rows; it leaves here in shares.
    `headers` gives the columns when there are no rows.
    """
    df = named_columns(pd.DataFrame(rows) if rows else pd.DataFrame(columns=list(headers or [])))
    df = df.rename(columns={h: C.key_for_header(h) for h in df.columns})
    df = df.loc[:, ~df.columns.duplicated()]
    for col in df.columns:
        if col in C.TEXT_KEYS:
            df[col] = df[col].astype(object).where(df[col].notna(), "").astype(str).replace({"None": "", "nan": ""})
        else:
            raw = df[col].astype(str).str.replace("%", "", regex=False).str.replace(",", "", regex=False)
            df[col] = pd.to_numeric(raw, errors="coerce")
    if avgvol_in_thousands and "avgvol" in df:
        df["avgvol"] = df["avgvol"] * 1000.0
    return derive(df)


def _has(df: pd.DataFrame, *cols: str) -> bool:
    return all(c in df for c in cols)


PLACES = 6        # percentages are rounded, so 2.60 / 2.00 is a 30% run and not 30.000000000000004


def derive(df: pd.DataFrame) -> pd.DataFrame:
    """Columns computed from one row: no history needed. See docs/enrichment.md."""
    if _has(df, "price", "vol"):
        df["dollar_vol"] = df.price * df.vol
    if _has(df, "vol", "float_m"):
        df["rotation"] = df.vol / (df.float_m * 1e6)
    if _has(df, "high", "low", "open", "price"):
        df["range_pct"] = ((df.high / df.low - 1) * 100).round(PLACES)
        df["close_pos"] = ((df.price - df.low) / (df.high - df.low)).where(df.high > df.low).round(PLACES)
        df["from_high"] = ((df.price / df.high - 1) * 100).round(PLACES)
        df["high_vs_open"] = ((df.high / df.open - 1) * 100).round(PLACES)
        df["low_vs_open"] = ((df.low / df.open - 1) * 100).round(PLACES)
        # From the prices, not from Finviz's two-decimal percentages: those are too coarse to
        # say how much of a run was kept.
        df["kept"] = ((df.price - df.open) / (df.high - df.open)).where(df.high > df.open).round(PLACES)
    if _has(df, "atr", "price"):
        df["atr_pct"] = df.atr / df.price * 100
    return df.replace([np.inf, -np.inf], np.nan)
