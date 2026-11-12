"""Shared test helpers. No test touches the network or the real data folder."""
import csv
import io
import json

import pytest

from finviz_cli import columns as C

BASE = {"Company": "Example Inc", "Sector": "Technology", "Industry": "Software - Application", "Country": "USA",
        "Market Cap": "100.00", "Shares Float": "10.00", "Short Float": "5.00%", "Average True Range": "0.20",
        "Average Volume": "500.00", "Relative Volume": "3.00", "Earnings Date": "", "News Time": "", "News Title": ""}


def row(ticker, prev_close, open_, high, low, price, volume, **extra):
    """One screener row as Finviz would send it (strings, percents with a % sign)."""
    r = dict(BASE)
    r.update({"Ticker": ticker, "Prev Close": f"{prev_close}", "Open": f"{open_}", "High": f"{high}", "Low": f"{low}",
              "Price": f"{price}", "Volume": f"{volume}",
              "Change": f"{(price / prev_close - 1) * 100:.2f}%", "Gap": f"{(open_ / prev_close - 1) * 100:.2f}%",
              "Change from Open": f"{(price / open_ - 1) * 100:.2f}%"})
    r.update(extra)
    return r


def export_csv(rows):
    out = io.StringIO()
    fields = list(rows[0].keys())
    w = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def lfr_json(day, rows):
    """The same rows in the shape of an imported end-of-day file: numbers as numbers, average volume in shares."""
    fixed = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if k in C.HEADERS and C.key_for_header(k) not in C.TEXT_KEYS:
                try:
                    d[k] = float(str(v).replace("%", "").replace(",", ""))
                except ValueError:
                    d[k] = None
            else:
                d[k] = v
        d["Average Volume"] = d["Average Volume"] * 1000
        fixed.append(d)
    return json.dumps({"date": day, "et": 970, "rows": fixed})


@pytest.fixture
def unwritable(monkeypatch):
    """Make a folder behave as one that takes no new files and no writes, on any platform.

    The real code runs against it (FileLock's own probe included): only the two calls that
    would create or open a file for writing under that folder are made to fail, the way the
    operating system fails them. Returns a function taking the folder to protect.
    """
    import builtins
    import io
    import os
    import pathlib
    import tempfile

    def protect(folder):
        root = pathlib.Path(os.path.abspath(str(folder)))
        real_open, real_mkstemp, real_mkdir, real_replace = builtins.open, tempfile.mkstemp, pathlib.Path.mkdir, os.replace

        def inside(path):
            return pathlib.Path(os.path.abspath(str(path))).is_relative_to(root)

        def refuse(path):
            return PermissionError(13, "Permission denied", str(path))

        def guarded_open(path, mode="r", *a, **k):
            if isinstance(path, (str, os.PathLike)) and inside(path) and any(c in mode for c in "wax+"):
                raise refuse(path)
            return real_open(path, mode, *a, **k)

        def guarded_mkstemp(*a, dir=None, **k):
            if dir is not None and inside(dir):
                raise refuse(dir)
            return real_mkstemp(*a, dir=dir, **k)

        def guarded_mkdir(self, *a, **k):
            if inside(self) and not self.exists():              # an existing folder is fine, as it is for the real call
                raise refuse(self)
            return real_mkdir(self, *a, **k)

        def guarded_replace(src, dst, *a, **k):
            if inside(dst):
                raise refuse(dst)
            return real_replace(src, dst, *a, **k)

        def guarded_rename(src, dst, *a, **k):
            if inside(src) or inside(dst):
                raise refuse(dst)
            return real_rename(src, dst, *a, **k)

        def guarded_unlink(path, *a, **k):
            if inside(path):
                raise refuse(path)
            return real_unlink(path, *a, **k)

        real_rename, real_unlink = os.rename, os.unlink
        # Every door a write can come through, wherever in the package it is made from: plain
        # open and io.open (which pathlib uses), temporary files, new folders, and moving,
        # replacing or deleting a file.
        monkeypatch.setattr(builtins, "open", guarded_open)
        monkeypatch.setattr(io, "open", guarded_open)
        monkeypatch.setattr(tempfile, "mkstemp", guarded_mkstemp)
        monkeypatch.setattr(pathlib.Path, "mkdir", guarded_mkdir)
        monkeypatch.setattr(os, "replace", guarded_replace)
        monkeypatch.setattr(os, "rename", guarded_rename)
        monkeypatch.setattr(os, "unlink", guarded_unlink)

    return protect


@pytest.fixture(autouse=True)
def nothing_of_the_developers_own(monkeypatch, tmp_path_factory):
    """No test sees the token of whoever runs it: not from the environment, not from a .env
    in the folder the tests are run from, not from the real data folder. Returns the empty
    folder that stands in for the current one."""
    from finviz_cli import client
    for name in ("FINVIZ_API_KEY", "FINVIZ_AUTH_TOKEN", "FINVIZ_SECRET_ID", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FINVIZ_CLI_HOME", str(tmp_path_factory.mktemp("home")))       # a test's own fixture may point it elsewhere
    here = tmp_path_factory.mktemp("here")
    monkeypatch.setattr(client, "_current_folder", lambda: here)

    def no_network(self, request, **kwargs):
        raise AssertionError(f"a test tried to reach the network ({request.method} {request.url.split('?')[0]})")

    import requests
    monkeypatch.setattr(requests.Session, "send", no_network)
    return here


@pytest.fixture
def home(tmp_path, monkeypatch):
    from finviz_cli import store
    monkeypatch.setenv("FINVIZ_CLI_HOME", str(tmp_path))
    monkeypatch.delenv("FINVIZ_API_KEY", raising=False)
    monkeypatch.setattr(store, "MIN_OVERLAP", 1)        # the test days hold a handful of tickers, not thousands
    return tmp_path
