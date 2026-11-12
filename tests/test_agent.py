"""The commands as functions for a program (agent.py), and the MCP server over them."""
import asyncio
import json
import os
import subprocess
import sys

import click
import pytest

from finviz_cli import agent, filters, output
from finviz_cli import cli as cli_mod
from finviz_cli.client import FinvizError
from test_store_cli import FakeClient, bare, runner  # noqa: F401  (fixtures: a fake Finviz, a fixed clock, four saved days)

TOKEN = "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------- one result shape, whatever happens

def test_a_result_has_rows_notes_and_nothing_printed(runner, capsys):
    got = agent.screen("open_chg >= 10", date="2026-11-09", columns=["ticker", "price", "yesterday", "catalyst"])
    assert got["ok"] is True and got["row_count"] == 2 and "error" not in got and "truncated" not in got
    assert {r["ticker"] for r in got["rows"]} == {"RUN", "NEW"} and set(got["rows"][0]) <= {"ticker", "price", "yesterday", "catalyst"}
    assert any("2026-11-09" in n for n in got["notes"])                       # the notes say what the data is
    assert capsys.readouterr() == ("", "")                                    # nothing on stdout or stderr: it is all in the result
    json.dumps(got)                                                           # and it is plain data


def test_empty_fields_are_left_out_and_long_answers_are_cut_with_a_word_about_it(runner):
    everything = agent.screen("", date="2026-11-09", limit=500)
    assert everything["ok"] and everything["row_count"] == len(everything["rows"]) >= 4
    assert all(v is not None and v != "" for row in everything["rows"] for v in row.values())
    two = agent.screen("", date="2026-11-09", limit=2)
    assert len(two["rows"]) == 2 and two["row_count"] == everything["row_count"]
    assert two["truncated"].startswith(f"2 of {everything['row_count']} rows shown (the first)")
    assert agent.screen("", date="2026-11-09", limit=0)["rows"] == everything["rows"][:1]              # at least one, at most the cap
    assert len(agent.screen("", date="2026-11-09", limit=float("inf"))["rows"]) == everything["row_count"]   # not a count either
    assert "raise limit" in two["truncated"]
    # A tool with no limit of its own does not tell the model to raise one.
    cut = agent.call(["screen", "--date=2026-11-09", "--top=0"], 2, fixed=True)
    assert cut["truncated"].endswith("narrow the request") and "raise limit" not in cut["truncated"]
    assert len(agent.screen("", date="2026-11-09", limit="many")["rows"]) == everything["row_count"]   # not a number: the default


def test_the_newest_rows_are_kept_where_a_table_runs_oldest_first(runner):
    # The fake answers with bars out of order (09:30, 17:25, 16:05). The newest are the newest by time, not by position.
    bars = agent.price_bars("huck", "i1", "d1", limit=2)
    assert [r["date"] for r in bars["rows"]] == ["2026-11-10 16:05", "2026-11-10 17:25"] and "(the newest)" in bars["truncated"]
    assert [r["date"] for r in agent.price_bars("huck", "i1", "d1", limit=1)["rows"]] == ["2026-11-10 17:25"]
    every = agent.price_bars("huck", "i1", "d1")
    assert [r["date"] for r in every["rows"]] == ["2026-11-10 09:30", "2026-11-10 16:05", "2026-11-10 17:25"]
    assert every["notes"] == ["HUCK i1 bars: 3 rows, 2026-11-10 09:30 .. 2026-11-10 17:25"]         # the span is earliest to latest too


@pytest.mark.parametrize("make, code_word", [
    (lambda: agent.screen("ticker.to_csv('x.csv') == None", date="2026-11-09"), "not a filter: '.to_csv()' is not available"),
    (lambda: agent.screen("@os.system('x')", date="2026-11-09"), "'@' reaches outside the table"),
    (lambda: agent.screen("nonsense > 1", date="2026-11-09"), "unknown column 'nonsense'"),
    (lambda: agent.screen("", date="yesterday"), "is not a date"),
    (lambda: agent.screen("", date="2020-01-02"), "is not a saved trading day"),
    (lambda: agent.ticker_history([]), "Missing argument"),
    (lambda: agent.price_bars("HUCK", bar="i60"), "'i60' is not one of"),
    (lambda: agent.market_performance("stocks"), "market must be one of futures, forex, crypto"),
    (lambda: agent.market_performance("screen --refresh"), "market must be one of"),
    (lambda: agent.institutions("x", "export"), "kind must be managers or funds"),
    (lambda: agent.ticker_history(["AAA"], days="--help"), "is not a valid integer"),
    (lambda: agent.screen("--refresh", date="2026-11-09"), "not a filter"),
    (lambda: agent.screen("streak", date="2026-11-09"), "needs something that is true or false for each row"),
    (lambda: agent.news([]), "give at least one ticker"),
    (lambda: agent.news(["  ", ""]), "give at least one ticker"),
    (lambda: agent.news(" , "), "give at least one ticker"),
    (lambda: agent.earnings_calendar(sort="size"), "the earnings calendar can be sorted by"),
])
def test_a_failure_is_a_result_that_says_why(runner, capsys, make, code_word):
    got = make()
    assert got["ok"] is False and got["rows"] == [] and code_word in got["error"]
    assert capsys.readouterr() == ("", "") and "Traceback" not in got["error"]


def test_a_finviz_failure_and_an_unexpected_one_are_results_too_and_never_show_the_token(runner, monkeypatch):
    FakeClient.refuse = True
    got = agent.news(["AAPL"])
    assert got == {"ok": False, "rows": [], "row_count": 0, "notes": [], "error": "Finviz refused the token (HTTP 403)"}
    FakeClient.refuse = False

    def broken(*a, **k):
        raise RuntimeError(f"connection reset while sending auth={TOKEN}\nsecond line")

    monkeypatch.setattr(cli_mod.endpoints, "news", broken)
    got = agent.news(["AAPL"])
    assert got["ok"] is False and got["error"].startswith("RuntimeError: connection reset") and TOKEN not in got["error"]
    assert "\n" not in got["error"]


def test_a_pull_that_fails_its_checks_is_a_failure_with_its_reason(runner):
    FakeClient.screener = "Ticker,Price\nONLY,1.00\n"                          # not a whole-market answer
    got = agent.screen("")
    assert got["ok"] is False and "was not usable" in got["error"] and got["rows"] == []


def test_output_capture_is_only_inside_its_block(capsys):
    import pandas as pd
    table = pd.DataFrame([{"a": 1, "b": None}])
    with output.capture() as got:
        output.note("inside  ")
        output.emit(table, "table")
    assert got.rows == [{"a": 1, "b": None}] and got.notes == ["inside"]
    output.note("outside")
    output.emit(table, "ndjson")
    out, err = capsys.readouterr()
    assert out.strip() == '{"a":1,"b":null}' and err.strip() == "outside"


# ---------------------------------------------------------------- each tool asks for the right thing

@pytest.fixture
def asked(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "call", lambda args, limit=50, newest=False, order_by="", fixed=False:
                        calls.append((list(args), limit, newest)) or {"ok": True, "rows": []})
    return calls


@pytest.mark.parametrize("make, args, limit, newest", [
    (lambda: agent.screen(), ["screen", "--top=0"], 25, False),
    (lambda: agent.screen(" gap > 5 ", "2026-11-09", "relvol", True, ["ticker", "gap"], 10),
     ["screen", "--date=2026-11-09", "--sort=relvol", "--asc", "--top=0", "--show=ticker,gap", "--", " gap > 5 "], 10, False),
    (lambda: agent.screen("--help"), ["screen", "--top=0", "--", "--help"], 25, False),                    # after --: a value, not a switch
    (lambda: agent.movers("2026-11-09"), ["movers", "--date=2026-11-09"], 50, False),
    (lambda: agent.store_status(), ["history", "status"], 5, False),
    (lambda: agent.ticker_history("aaa, bbb  ccc", 3), ["ticker", "--days=3", "--", "aaa", "bbb", "ccc"], 200, False),
    (lambda: agent.ticker_history(["AAA", "--days=999", "-x", " "]), ["ticker", "--days=10", "--", "AAA", "--days=999", "-x"], 200, False),
    (lambda: agent.ticker_history([]), ["ticker", "--days=10"], 200, False),
    (lambda: agent.intraday(["OMH"], 2, "i5"), ["intraday", "--tf=i5", "--days=2", "--", "OMH"], 200, False),
    (lambda: agent.price_bars("AAPL"), ["bars", "--tf=d", "--range=m1", "--", "AAPL"], 120, True),
    (lambda: agent.news(["AAPL", "MSFT"], 5), ["news", "--", "AAPL", "MSFT"], 5, False),
    (lambda: agent.news(["--feed=market"]), ["news", "--", "--feed=market"], 20, False),                   # a ticker, however it is spelled
    (lambda: agent.market_news("blogs"), ["news", "--feed=market", "--only=blogs"], 30, False),
    (lambda: agent.short_interest("OMH"), ["short-interest", "--", "OMH"], 12, False),
    (lambda: agent.sec_filings("OMH", "registration-statements"), ["filings", "--group=registration-statements", "--", "OMH"], 25, False),
    (lambda: agent.insider_trades(), ["insiders", "--kind=all"], 50, False),
    (lambda: agent.insider_trades("aapl", "buy", 100000, 7), ["insiders", "--kind=buy", "--min-value=100000", "--", "aapl"], 7, False),
    (lambda: agent.options_chain("MSFT", "2026-12-18", 402.5, "put"), ["options", "--expiry=2026-12-18", "--strike=402.5", "--type=put", "--", "MSFT"], 100, False),
    (lambda: agent.options_chain("MSFT"), ["options", "--", "MSFT"], 100, False),
    (lambda: agent.financial_statement("OMH", "balance", True), ["statements", "--statement=balance", "--quarterly", "--", "OMH"], 200, False),
    (lambda: agent.earnings_calendar("2026-11-11", "2026-11-12", "-marketCap"),
     ["earnings", "--from=2026-11-11", "--to=2026-11-12", "--sort=-marketCap"], 100, False),               # a key that starts with a dash stays a value
    (lambda: agent.economic_calendar("2026-11-09"), ["economic", "--from=2026-11-09"], 150, False),
    (lambda: agent.dividend_calendar(), ["dividends", "--sort=-yield"], 100, False),
    (lambda: agent.market_performance("forex", "week"), ["forex", "--sort=week"], 40, False),
    (lambda: agent.sector_groups("industry"), ["groups", "--by=industry"], 60, False),
    (lambda: agent.institutions("berkshire"), ["managers", "--", "berkshire"], 25, False),
    (lambda: agent.institutions("fidelity", "funds"), ["funds", "--", "fidelity"], 25, False),
    (lambda: agent.institution_holdings("1067983", "new"), ["holdings", "--change=new", "--", "1067983"], 50, False),
    (lambda: agent.institution_holdings(1067983), ["holdings", "--", "1067983"], 50, False),                # the id as `institutions` returns it
])
def test_each_tool_asks_for_the_right_thing(asked, make, args, limit, newest):
    make()
    assert asked == [(args, limit, newest)]


def test_a_filter_that_looks_like_an_option_is_not_one(runner):
    """`--help` handed in as a filter must not print the help (on stdout, where the protocol is)."""
    got = agent.screen("--help", date="2026-11-09")
    assert got["ok"] is False and got["rows"] == []


def test_list_columns_can_be_narrowed(runner):
    every = agent.list_columns()
    assert every["ok"] and every["row_count"] == len({r["name"] for r in every["rows"]}) == 75
    floats = agent.list_columns("float")
    assert 0 < floats["row_count"] < 10 and all("float" in json.dumps(r).lower() for r in floats["rows"])


def test_list_columns_lists_what_a_filter_can_name(runner):
    """The guide sends a model here for the names a filter takes. Finviz exports more columns
    than a pull keeps, and the ones worked out here are not Finviz's at all."""
    listed = {r["name"]: r for r in agent.list_columns()["rows"]}
    assert listed["streak"]["from"] == "computed" and listed["streak"]["kind"] == "number" and "10 sessions" in listed["streak"]["meaning"]
    assert listed["mover"]["kind"] == "true/false" and listed["yesterday"]["kind"] == "text"
    assert listed["float_m"] == {"name": "float_m", "kind": "number", "from": "Finviz", "header": "Shares Float", "meaning": "millions of shares"}
    assert agent.list_columns("streak")["row_count"] == 1
    assert agent.list_columns("P/E")["row_count"] == 0 and "p_e" not in listed            # exported by Finviz, not kept by a pull
    table, _, _ = cli_mod.pick_data("2026-11-09", False, None, None, False)              # a saved whole-market day
    assert set(table.columns) <= set(listed), set(table.columns) - set(listed)            # nothing a filter can name is missing from the list
    assert {name for name, r in listed.items() if r["from"] == "computed"} <= set(table.columns)
    kinds = {filters._kind(table[c]) for c in ("price", "streak")} | {filters._kind(table["mover"]), filters._kind(table["shape"])}
    assert kinds == {"a number", "true/false", "text"}
    for name in table.columns:                                                            # and each is the kind the list says it is
        if not table[name].isna().all():
            assert filters._kind(table[name]) == {"number": "a number", "text": "text", "true/false": "true/false"}[listed[name]["kind"]], name
    for name in listed:                                                                   # every name listed is accepted by a filter of its kind
        ask = {"number": f"{name} > 0", "text": f"{name} != 'x'", "true/false": name}[listed[name]["kind"]]
        if name in table.columns:
            assert agent.screen(ask, date="2026-11-09", limit=1)["ok"], ask
    live, _, _ = cli_mod.pick_data(None, False, None, None, False)                        # a pull made now, not a saved day
    assert set(live.columns) <= set(listed), set(live.columns) - set(listed)
    assert set(table.columns) - set(live.columns) == {"di"} and "Saved days only" in listed["di"]["meaning"]      # the one column a live pull lacks, and the list says so
    assert agent.screen("di > 0", date="2026-11-09", limit=1)["ok"] and "unknown column 'di'" in agent.screen("di > 0", limit=1)["error"]
    assert agent.screen("news.str.contains('offering', na=True)", date="2026-11-09")["row_count"] == len(table)   # no headlines that day: all asked for
    assert agent.screen("news.str.contains('offering')", date="2026-11-09")["row_count"] == 0


def test_a_field_left_out_of_a_row_is_missing_to_a_filter(runner):
    """Empty fields are left out of a result. What a model cannot see, .isna() finds."""
    every = agent.screen("", date="2026-11-09", columns=["ticker", "earnings", "news"])
    reporting = agent.screen("earnings.notna()", date="2026-11-09", columns=["ticker", "earnings"])
    silent = agent.screen("earnings.isna()", date="2026-11-09", columns=["ticker", "earnings"])
    assert [r["ticker"] for r in reporting["rows"]] == ["NEW"] and "earnings" in reporting["rows"][0]
    assert silent["row_count"] == every["row_count"] - 1 and all("earnings" not in r for r in silent["rows"])
    assert agent.screen("news.isna()", date="2026-11-09")["row_count"] == every["row_count"] and all("news" not in r for r in every["rows"])
    assert agent.screen("news.notna()", date="2026-11-09")["row_count"] == 0
    assert agent.screen("earnings == ''", date="2026-11-09")["row_count"] == silent["row_count"]


def test_every_tool_says_what_it_is_for():
    names = [t.__name__ for t in agent.TOOLS]
    assert len(names) == len(set(names)) == 21
    for tool in agent.TOOLS:
        assert tool.__doc__ and len(tool.__doc__.strip()) > 40, tool.__name__
        assert tool.__annotations__.get("return") in (dict, "dict"), tool.__name__
    assert "investment advice" in agent.GUIDE and "10 requests per 5 seconds" in agent.GUIDE


# ---------------------------------------------------------------- the MCP server

def test_the_server_offers_every_tool_as_read_only(runner):
    pytest.importorskip("mcp")
    from finviz_cli import mcp_server
    server = mcp_server.build()
    tools = asyncio.run(server.list_tools())
    assert [t.name for t in tools] == [t.__name__ for t in agent.TOOLS]
    screen = next(t for t in tools if t.name == "screen")
    schema = getattr(screen, "inputSchema", None) or getattr(screen, "input_schema", None)
    assert set(schema["properties"]) == {"filter", "date", "sort", "ascending", "columns", "limit"} and not schema.get("required")
    assert "Filter every US-listed stock" in screen.description
    hints = [getattr(t, "annotations", None) for t in tools]
    assert all(h is None or (getattr(h, "readOnlyHint", getattr(h, "read_only_hint", None)) is True) for h in hints)
    answer = payload(asyncio.run(server.call_tool("screen", {"filter": "open_chg >= 10", "date": "2026-11-09", "columns": ["ticker"]})))
    assert answer["ok"] is True and answer["row_count"] == 2 and {r["ticker"] for r in answer["rows"]} == {"RUN", "NEW"}
    refused = payload(asyncio.run(server.call_tool("screen", {"filter": "ticker.str.contains('A', True, 128)", "date": "2026-11-09"})))
    assert refused["ok"] is False and refused["rows"] == [] and "takes at most 1 value by position" in refused["error"]


def payload(answer):
    """The tool's own result out of whatever the SDK wraps it in: a dict with ok and rows."""
    if isinstance(answer, dict):
        if "ok" in answer and "rows" in answer:
            return answer
        for value in answer.values():
            found = payload(value)
            if found:
                return found
    elif isinstance(answer, (list, tuple)):
        for item in answer:
            found = payload(item)
            if found:
                return found
    else:
        for name in ("structured_content", "structuredContent", "content"):
            found = payload(getattr(answer, name, None)) if getattr(answer, name, None) is not None else None
            if found:
                return found
        text = getattr(answer, "text", None)
        if isinstance(text, str) and text.lstrip().startswith("{"):
            return payload(json.loads(text))
    return None


def test_an_id_from_one_tool_goes_into_the_next_as_it_came(runner, monkeypatch):
    """`institutions` returns a manager's investor_id as a number. `institution_holdings` must
    take it as that, through the SDK's own check of the arguments."""
    pytest.importorskip("mcp")
    from finviz_cli import mcp_server

    class Holders:
        def export(self, path, params=None):
            if path == "managers":
                return ("Name,Portfolio Manager,Investor ID,Report Date,Portfolio Value\n"
                        '"BIG FUND LLP","Some One",1067983,2026-06-30,299253556246\n')
            assert path == "managers/1067983/holdings", path
            return "Ticker,Name,Type,Shares Held,Market Value\nAAA,Example Corp,Share,100,2500\n"

    monkeypatch.setattr(cli_mod, "Client", Holders)
    server = mcp_server.build()
    listed = payload(asyncio.run(server.call_tool("institutions", {"search": "big"})))
    investor_id = listed["rows"][0]["investor_id"]
    assert investor_id == 1067983 and isinstance(investor_id, int)
    held = payload(asyncio.run(server.call_tool("institution_holdings", {"investor_id": investor_id})))
    assert held["ok"] is True and held["rows"] == [{"ticker": "AAA", "name": "Example Corp", "type": "Share", "shares_held": 100, "market_value": 2500}]
    as_text = payload(asyncio.run(server.call_tool("institution_holdings", {"investor_id": "1067983"})))
    assert as_text["rows"] == held["rows"]
    assert agent.institution_holdings(True)["ok"] is False                                  # true is not an id


def test_over_stdio_nothing_but_the_protocol_is_on_stdout(tmp_path):
    """A real exchange with the server as its own process: every line it writes to stdout is
    a JSON-RPC message, including after a tool has run and after one has failed."""
    pytest.importorskip("mcp")
    import queue
    import threading

    package_root = os.path.dirname(os.path.dirname(os.path.abspath(agent.__file__)))
    env = {**os.environ, "FINVIZ_CLI_HOME": str(tmp_path), "PYTHONPATH": package_root, "PYTHONIOENCODING": "utf-8"}
    for name in ("FINVIZ_API_KEY", "FINVIZ_AUTH_TOKEN", "FINVIZ_SECRET_ID"):
        env.pop(name, None)
    proc = subprocess.Popen([sys.executable, "-m", "finviz_cli.mcp_server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env, cwd=str(tmp_path))
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout] and None, daemon=True).start()
    errors = []
    threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()
    seen = []

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def answer_to(request_id):
        while True:
            try:
                line = lines.get(timeout=60)
            except queue.Empty:
                pytest.fail(f"no answer to request {request_id}; stderr: {''.join(errors)[-1500:]}")
            if not line.strip():
                continue
            seen.append(line)
            message = json.loads(line)                                         # anything that is not JSON fails here
            assert message.get("jsonrpc") == "2.0", line[:200]
            if message.get("id") == request_id:
                return message

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        assert answer_to(1)["result"]["serverInfo"]["name"] == "finviz"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert len(answer_to(2)["result"]["tools"]) == 21
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "store_status", "arguments": {}}})
        status = json.dumps(answer_to(3)["result"]).replace('\\"', '"')
        assert '"ok": true' in status and "row_count" in status
        # A filter that is spelled like a switch: a value all the same, and no help text on stdout.
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "screen", "arguments": {"filter": "--help", "date": "2026-11-09"}}})
        refused = json.dumps(answer_to(4)["result"]).replace('\\"', '"')
        assert '"ok": false' in refused and "Usage:" not in refused
        # No token in this process: the failure comes back as a result, in words.
        send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "news", "arguments": {"symbols": ["AAPL"]}}})
        assert "no Finviz token" in json.dumps(answer_to(5)["result"])
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    assert len(seen) >= 5 and not any("Usage:" in line for line in seen)


def test_a_stray_print_inside_a_tool_does_not_reach_the_protocol(tmp_path):
    """Whatever a tool or a library under it prints, stdout stays the protocol's. Shown with a
    server that has one extra tool whose only job is to print."""
    pytest.importorskip("mcp")
    import queue
    import threading

    script = tmp_path / "noisy_server.py"
    script.write_text(
        "import sys\n"
        "from finviz_cli import agent, mcp_server\n"
        "def noisy() -> dict:\n"
        "    'Prints, as a careless library might.'\n"
        "    print('STRAY TEXT ON STDOUT')\n"
        "    sys.stdout.write('more stray text\\n')\n"
        "    sys.stdout.flush()\n"
        "    return {'ok': True, 'rows': [], 'row_count': 0, 'notes': []}\n"
        "agent.TOOLS = (*agent.TOOLS, noisy)\n"
        "sys.argv = sys.argv[:1]\n"
        "mcp_server.main()\n", "utf-8")
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(agent.__file__)))
    env = {**os.environ, "FINVIZ_CLI_HOME": str(tmp_path), "PYTHONPATH": package_root, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", env=env, cwd=str(tmp_path))
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout] and None, daemon=True).start()
    errors = []
    threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()
    seen = []

    def exchange(message, request_id=None):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        while request_id is not None:
            try:
                line = lines.get(timeout=60)
            except queue.Empty:
                pytest.fail(f"no answer to request {request_id}; stderr: {''.join(errors)[-1500:]}")
            if line.strip():
                seen.append(line)
                message = json.loads(line)                                     # stray text would fail here
                if message.get("id") == request_id:
                    return message

    try:
        exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}, 1)
        exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert len(exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 2)["result"]["tools"]) == 22
        assert "row_count" in json.dumps(exchange({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "noisy", "arguments": {}}}, 3))
        assert len(exchange({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, 4)["result"]["tools"]) == 22      # still talking
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    assert not any("STRAY" in line or "more stray" in line for line in seen)
    for _ in range(50):                                                        # the reader thread may still be draining stderr
        if "STRAY TEXT ON STDOUT" in "".join(errors):
            break
        threading.Event().wait(0.1)
    assert "STRAY TEXT ON STDOUT" in "".join(errors) and "more stray text" in "".join(errors)       # it went to stderr instead
