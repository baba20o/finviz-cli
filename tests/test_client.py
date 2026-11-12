import logging
import os
import subprocess
import sys
import traceback
import types

import pytest
import requests

from finviz_cli import client
from finviz_cli.client import Client, FinvizError, redact

TOKEN = "11111111-2222-3333-4444-555555555555"
ODD_TOKEN = "plain-secret/with+odd=chars"            # not shaped like a UUID, and changed by URL quoting
BOM = chr(0xFEFF)


class Resp:
    def __init__(self, status=200, text="Ticker,Price\nAAA,1.00\n", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class Session:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


class Clock:
    """A fake wall clock: sleeping moves it forward and is recorded."""

    def __init__(self):
        self.now, self.sleeps = 1_000_000.0, []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch, tmp_path):
    monkeypatch.setenv("FINVIZ_CLI_HOME", str(tmp_path))       # the pacing files must not touch the real store
    c = Clock()
    monkeypatch.setattr(client.time, "time", c.time)
    monkeypatch.setattr(client.time, "sleep", c.sleep)
    monkeypatch.setattr(client.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(Client, "_next", 0.0)
    monkeypatch.setattr(Client, "_hold_until", 0.0)
    monkeypatch.setattr(Client, "_stamp", 0.0)
    monkeypatch.setattr(client, "_warned", set())
    return c


def make(answers, token=TOKEN, **kw):
    s = Session(answers)
    return Client(token=token, session=s, **kw), s


def forget_process_state():
    """What a second process would know: nothing but the shared files."""
    Client._next = Client._hold_until = Client._stamp = 0.0


def leak():
    return requests.ConnectionError(f"HTTPSConnectionPool: /export/screener?auth={TOKEN} timed out")


class Keep(logging.Handler):
    """Collects what a real handler would print, exception text included."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


@pytest.fixture
def listen():
    """Attach a DEBUG handler to a logger for the length of a test."""
    attached = []

    def start(name):
        lg, handler = logging.getLogger(name), Keep()
        attached.append((lg, handler, lg.level))
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
        return lg, handler

    yield start
    for lg, handler, level in attached:
        lg.removeHandler(handler)
        lg.setLevel(level)


# ---------------------------------------------------------------- keeping the token out of sight

def test_redact_hides_the_token_wherever_it_appears():
    assert TOKEN not in redact(f"GET /export/screener?v=152&auth={TOKEN}&f=x")
    assert TOKEN not in redact(f"token {TOKEN} leaked")
    assert redact("https://x/y?auth=abc123&z=1") == "https://x/y?auth=<redacted>&z=1"
    Client(token="not-shaped-like-a-uuid-9f8e7d")               # a token of any shape is known once used
    assert "9f8e7d" not in redact("it was not-shaped-like-a-uuid-9f8e7d all along")


def test_redact_reads_a_percent_encoded_url_too():
    # A sign-in redirect carries the original request, encoded, in its return address.
    encoded = "/login.ashx?ReturnUrl=%2fexport%2fscreener%3fv%3d152%26auth%3dabc123XYZ%26f%3dx"
    assert redact(encoded) == "/login.ashx?ReturnUrl=%2fexport%2fscreener%3fv%3d152%26auth%3d<redacted>%26f%3dx"
    assert "abc123" not in redact("next=%252Fexport%253Fauth%253Dabc123XYZ%2526z%253D1")       # encoded twice
    Client(token=ODD_TOKEN)
    assert "plain-secret" not in redact("ReturnUrl=x%3Fauth%3Dplain-secret%2Fwith%2Bodd%3Dchars")
    assert "plain-secret" not in redact("somewhere plain-secret%2Fwith%2Bodd%3Dchars appears bare")


def test_redact_knows_a_token_in_every_encoding_and_whole(monkeypatch):
    amp = "part1&part2 with space"                             # an & inside the token, where the auth= pattern would stop
    Client(token=amp)
    Client(token=ODD_TOKEN)
    for written in ("auth=part1%26part2+with+space&f=x", "auth=part1%26part2%20with%20space", "auth%3dpart1%2526part2%2520with%2520space",
                    "seen bare: part1%26part2+with+space", "lowercase escapes: plain-secret%2fwith%2bodd%3dchars",
                    "twice: plain-secret%252fwith%252bodd%253dchars", "twice: plain-secret%252Fwith%252Bodd%253Dchars"):
        shown = redact(written)
        assert "part2" not in shown and "odd" not in shown and "<redacted>" in shown, written
    Client(token="example/part+tail=end")
    mixed = "example%2Fpart%2btail%3Dend"                      # escapes in both letter cases at once, no auth= before it
    assert redact(f"seen: {mixed}") == "seen: <redacted>" and redact(f"twice: example%252Fpart%252btail%253Dend") == "twice: <redacted>"
    assert redact("Example/Part+Tail=End stays") == "Example/Part+Tail=End stays"     # the token's own letters match exactly


def test_a_mixed_case_escape_is_redacted_in_exception_and_stack_text(listen):
    Client(token="example/part+tail=end")
    mixed = "example%2Fpart%2btail%3Dend"
    lg, handler = listen("urllib3.connectionpool")
    try:
        raise requests.ConnectionError(f"broken while sending {mixed}")
    except requests.ConnectionError:
        lg.warning("retrying %s", mixed, exc_info=True)
    lg.handle(lg.makeRecord(lg.name, logging.ERROR, "pool.py", 1, "while sending", (), None, sinfo=f"Stack:\n  {mixed}"))
    assert len(handler.lines) == 2 and "example" not in "\n".join(handler.lines)


def test_a_secret_in_a_records_stack_text_is_redacted(listen):
    Client(token=TOKEN)
    lg, handler = listen("urllib3.connectionpool")
    stack = f"Stack (most recent call last):\n  File \"pool.py\", line 1\n    GET /export/screener?v=152&auth={TOKEN}"
    lg.handle(lg.makeRecord(lg.name, logging.ERROR, "pool.py", 1, "while sending", (), None, sinfo=stack))
    assert len(handler.lines) == 1 and TOKEN not in handler.lines[0] and "auth=<redacted>" in handler.lines[0]


def test_export_sends_the_token_but_never_reports_it():
    c, s = make([Resp()])
    assert c.export("screener", {"v": "152", "f": ""}).startswith("Ticker")
    url, params = s.calls[0]
    assert url == "https://elite.finviz.com/export/screener"
    assert params == {"v": "152", "auth": TOKEN}          # empty parameters are dropped


def test_a_network_error_never_carries_the_url_into_the_message_or_traceback():
    c, s = make([leak(), leak(), leak(), leak()])
    with pytest.raises(FinvizError) as e:
        c.export("screener", {})
    shown = "".join(traceback.format_exception(e.value))
    assert TOKEN not in shown and "auth=" not in shown and "ConnectionError" in str(e.value)
    assert e.value.__context__ is None and len(s.calls) == 4


def test_an_interrupt_during_a_pause_does_not_drag_the_url_along(clock, monkeypatch):
    def interrupted(seconds):
        raise KeyboardInterrupt
    c, _ = make([leak(), Resp()])
    monkeypatch.setattr(client.time, "sleep", interrupted)
    with pytest.raises(KeyboardInterrupt) as e:
        c.export("screener", {})
    assert TOKEN not in "".join(traceback.format_exception(e.value))


def test_the_token_cannot_be_passed_as_a_parameter():
    c, s = make([Resp()])
    with pytest.raises(FinvizError):
        c.export("screener", {"auth": "x"})
    assert not s.calls


def test_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("FINVIZ_API_KEY", " abc-env-token ")
    assert client.load_token() == "abc-env-token"
    assert "abc-env-token" not in redact("seen: abc-env-token")           # known from the moment it is loaded


def test_token_comes_from_a_dot_env_file(monkeypatch, tmp_path, nothing_of_the_developers_own):
    here, home = nothing_of_the_developers_own, tmp_path                  # the current folder, the data folder
    with pytest.raises(FinvizError, match="no Finviz token: put FINVIZ_API_KEY in the environment or in a .env file"):
        client.load_token()                                               # nowhere: and AWS is not even tried
    (home / ".env").write_text("FINVIZ_API_KEY=from-the-data-folder\n", "utf-8")
    assert client.load_token() == "from-the-data-folder"
    assert "from-the-data-folder" not in redact("seen: from-the-data-folder")          # redacted like any other
    (here / ".env").write_text("FINVIZ_API_KEY=from-this-folder\n", "utf-8")
    assert client.load_token() == "from-this-folder"                      # the current folder comes before the data folder
    monkeypatch.setenv("FINVIZ_API_KEY", "from-the-environment")
    assert client.load_token() == "from-the-environment"                  # and the environment before both
    monkeypatch.setenv("FINVIZ_API_KEY", "   ")
    assert client.load_token() == "from-this-folder"                      # a blank one is no token
    assert "FINVIZ_API_KEY" not in os.environ or os.environ["FINVIZ_API_KEY"] == "   "  # the file was read, not loaded into the process


@pytest.mark.parametrize("text, token", [
    ("FINVIZ_API_KEY=plain\n", "plain"),
    ("FINVIZ_API_KEY = spaced \n", "spaced"),
    ('FINVIZ_API_KEY="double quoted"\n', "double quoted"),
    ("FINVIZ_API_KEY='single # kept'\n", "single # kept"),
    ("export FINVIZ_API_KEY=exported\n", "exported"),
    ("# a comment\n\nOTHER=1\nFINVIZ_API_KEY=after-a-comment  # the token\n", "after-a-comment"),
    ("FINVIZ_API_KEY=has#no-space\n", "has#no-space"),
    ("FINVIZ_API_KEY=first\nFINVIZ_API_KEY=last\n", "last"),
    ("FINVIZ_API_KEY=crlf\r\nOTHER=2\r\n", "crlf"),
    (chr(0xFEFF) + "FINVIZ_API_KEY=after-a-byte-order-mark\n", "after-a-byte-order-mark"),
    ("FINVIZ_AUTH_TOKEN=older-name\n", "older-name"),
    ("FINVIZ_API_KEY=with=equals==\n", "with=equals=="),
])
def test_the_plain_forms_of_a_dot_env_line_are_read(tmp_path, text, token):
    (tmp_path / ".env").write_bytes(text.encode("utf-8"))
    assert client.load_token() == token


@pytest.mark.parametrize("text", ["", "FINVIZ_API_KEY=\n", "FINVIZ_API_KEY=''\n", "#FINVIZ_API_KEY=commented-out\n",
                                  "FINVIZ_API_KEY\n", "=nameless\n", "MY FINVIZ_API_KEY=two words\n", "OTHER_KEY=1\n"])
def test_a_dot_env_with_no_token_in_it_is_no_token(tmp_path, text):
    (tmp_path / ".env").write_text(text, "utf-8")
    with pytest.raises(FinvizError, match="no Finviz token"):
        client.load_token()


def test_a_dot_env_that_cannot_be_read_is_passed_over(tmp_path, nothing_of_the_developers_own):
    (tmp_path / ".env").write_bytes(b"FINVIZ_API_KEY=\xff\xfe\xfd not text\n")        # not UTF-8
    (nothing_of_the_developers_own / ".env").mkdir()                                  # a folder where the file would be
    with pytest.raises(FinvizError, match="no Finviz token"):
        client.load_token()


def test_the_secrets_name_is_never_taken_from_the_current_folder(monkeypatch, tmp_path, nothing_of_the_developers_own):
    """A .env that merely lies in the folder a command is run from must not be able to name
    which secret of the account is fetched and sent to Finviz as the token."""
    asked = []

    class Secrets:
        def get_secret_value(self, SecretId):
            asked.append(SecretId)
            return {"SecretString": '{"FINVIZ_API_KEY": "from-aws"}'}

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: Secrets()))
    (nothing_of_the_developers_own / ".env").write_text("FINVIZ_SECRET_ID=someone/elses/secret\n", "utf-8")
    with pytest.raises(FinvizError, match="no Finviz token"):
        client.load_token()
    assert asked == []                                                    # nothing was fetched
    (tmp_path / ".env").write_text("FINVIZ_SECRET_ID=mine/in-the-data-folder\n", "utf-8")
    assert client.load_token() == "from-aws" and asked == ["mine/in-the-data-folder"]
    monkeypatch.setenv("FINVIZ_SECRET_ID", "mine/in-the-environment")
    assert client.load_token() == "from-aws" and asked[-1] == "mine/in-the-environment"


def test_a_secret_is_only_looked_for_when_it_is_named(monkeypatch):
    def no_aws(*a, **k):
        raise AssertionError("AWS was asked although no secret was named")

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=no_aws))
    with pytest.raises(FinvizError, match="no Finviz token"):
        client.load_token()
    monkeypatch.setenv("FINVIZ_SECRET_ID", "named")
    monkeypatch.setitem(sys.modules, "boto3", None)                       # named, but boto3 is not installed
    with pytest.raises(FinvizError, match="FINVIZ_SECRET_ID is set, but boto3 is not installed"):
        client.load_token()


def test_http_library_logs_are_redacted_on_any_child_logger(listen):
    client.install_log_redaction()
    client.install_log_redaction()                          # installing twice wraps the factory once
    seen = []
    for name in ("urllib3.connectionpool", "urllib3.util.retry", "requests.adapters"):
        lg, handler = listen(name)
        lg.debug("GET /export/screener?v=152&auth=%s HTTP/1.1", TOKEN)
        seen += handler.lines
    assert len(seen) == 3 and all(TOKEN not in line and "auth=<redacted>" in line for line in seen)
    other = logging.getLogger("somewhere.else").makeRecord("somewhere.else", logging.INFO, "f", 1, "left %s", ("alone",), None)
    assert other.getMessage() == "left alone"


def test_an_exception_attached_to_a_log_record_is_redacted_as_well(listen):
    # A formatter appends the exception by itself; that text must have been through redact.
    Client(token=TOKEN)
    lg, handler = listen("urllib3.connectionpool")
    try:
        raise requests.ConnectionError(f"Max retries exceeded with url: /export/screener?v=152&auth={TOKEN}")
    except requests.ConnectionError as exc:
        lg.warning("Retrying after connection broken", exc_info=True)
        lg.error("giving up", exc_info=exc)
        lg.error("where it happened", stack_info=True)
    shown = "\n".join(handler.lines)
    assert len(handler.lines) == 3 and TOKEN not in shown
    assert shown.count("ConnectionError") >= 2 and "auth=<redacted>" in shown          # the exception is still reported


def test_nothing_is_logged_about_the_secret_while_it_is_fetched(monkeypatch, listen):
    """botocore's parsers log response bodies at DEBUG. The token is not known yet at that
    moment, so it cannot be recognised: those records are blanked instead."""
    body = '{"SecretString": "{\\"FINVIZ_API_KEY\\": \\"' + ODD_TOKEN + '\\"}"}'

    class Secrets:
        def get_secret_value(self, SecretId):
            logging.getLogger("botocore.parsers").debug("Response body:\n%s", body)
            logging.getLogger("urllib3.connectionpool").debug("POST / HTTP/1.1 200 %s", body)
            return {"SecretString": '{"FINVIZ_API_KEY": "' + ODD_TOKEN + '"}'}

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: Secrets()))
    monkeypatch.setenv("FINVIZ_SECRET_ID", "a/secret")
    client._KNOWN_TOKENS.clear()
    _, parsers = listen("botocore.parsers")                # a child logger set to DEBUG by the host program
    _, pool = listen("urllib3.connectionpool")
    assert client.load_token() == ODD_TOKEN
    shown = "\n".join(parsers.lines + pool.lines)
    assert len(parsers.lines) == 1 and len(pool.lines) == 1 and "plain-secret" not in shown and "withheld" in shown
    assert "plain-secret" not in redact(f"later: {ODD_TOKEN}")
    logging.getLogger("botocore.parsers").debug("after the fetch %s", "logging is back")
    assert "logging is back" in parsers.lines[-1]


def test_a_secret_that_cannot_be_read_is_reported_without_details(monkeypatch):
    class Secrets:
        def get_secret_value(self, SecretId):
            raise RuntimeError(f"AccessDenied for {SecretId} with key {TOKEN}")

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: Secrets()))
    monkeypatch.setenv("FINVIZ_SECRET_ID", "a/secret")
    with pytest.raises(FinvizError) as e:
        client.load_token()
    assert "RuntimeError" in str(e.value) and TOKEN not in str(e.value) and e.value.__context__ is None
    assert "a/secret" not in str(e.value)                                 # nor the secret's name


# ---------------------------------------------------------------- what counts as an answer

def test_things_that_are_not_csv_are_errors():
    pages = ["<!DOCTYPE html><html><body>Sign in</body></html>", BOM + "<html><body>Sign in</body></html>",
             "<!-- served by the edge -->\n<!DOCTYPE html><html></html>", "<?xml version='1.0'?><error/>",
             " " * 500 + "\n<html><body>late markup</body></html>", "<h1>Too many requests</h1>"]
    for body in pages:
        c, _ = make([Resp(200, body)])
        with pytest.raises(FinvizError, match="web page"):
            c.export("screener", {})
    c, _ = make([Resp(200, "Ticker,Price\nAAA,1\n", {"Content-Type": "text/html; charset=utf-8"})])
    with pytest.raises(FinvizError, match="web page"):
        c.export("screener", {})
    c, _ = make([Resp(200, "  \n")])
    with pytest.raises(FinvizError, match="returned nothing"):
        c.export("screener", {})
    c, _ = make([Resp(200, BOM + "Ticker,Price\nAAA,1\n", {"Content-Type": "text/csv"})])
    assert c.export("screener", {}) == "Ticker,Price\nAAA,1\n"


def test_an_answer_that_repeats_the_token_is_withheld():
    spaced = "two words/and+more"
    cases = [(TOKEN, f"/export/screener?v=152&auth={TOKEN}"), (ODD_TOKEN, f"called with {ODD_TOKEN}"),
             (ODD_TOKEN, "token plain-secret%2Fwith%2Bodd%3Dchars"), (ODD_TOKEN, "token plain-secret%2fwith%2bodd%3dchars"),     # lowercase escapes
             (ODD_TOKEN, "token plain-secret%252fwith%252bodd%253dchars"),                                                       # encoded twice
             (spaced, "token two+words%2Fand%2Bmore"), (spaced, "token two%20words%2fand%2bmore"),                               # a space as + or %20
             (TOKEN, "https://elite.finviz.com/export/screener?v=152&auth=someone-elses"),                                       # any echoed request
             (TOKEN, "ReturnUrl=%2fexport%2fscreener%3fv%3d152%26auth%3dsomething")]
    for token, echoed in cases:
        c, _ = make([Resp(200, f"Error,Detail\nbad request,{echoed}\n")], token=token)
        with pytest.raises(FinvizError, match="repeats the request's token") as e:
            c.export("screener", {})
        assert token not in str(e.value) and echoed not in str(e.value), echoed
    harmless = "Ticker,News Title\nAAA,Author=unknown says 12.5% of authors disagree\n"       # the word, not a URL parameter
    assert make([Resp(200, harmless)])[0].export("news", {}) == harmless
    # A publisher's own link may carry an auth parameter. That is their data, not our request.
    links = ("Ticker,News URL\nAAA,https://publisher.example/article?id=7&auth=public-link-signature\n"
             "BBB,https://export.example/feed?auth=abc\nCCC,https://news.example/redirect?to=%2Fstory%3Fauth%3Dxyz\n"
             "DDD,https://data.example/export/prices?auth=their-own-key\n"
             "EEE,https://publisher.example/story?source=finviz.com&auth=public-signature\n"       # mentions Finviz, is not Finviz
             "FFF,https://data.example/export/screener?auth=their-own-key\n"                       # the same path, on their host
             "GGG,https://notfinviz.com/x?auth=1\nHHH,https://finviz.com.evil.example/x?auth=1\n"
             "III,https://publisher.example/share?path=/export/screener&auth=sig\n"                # our path, inside their query
             "JJJ,https://publisher.example/redirect?to=%2Fexport%2Fscreener%3Fauth%3Dsig\n"
             'KKK,"https://publisher.example/article?related=one,/export/screener&auth=public-signature"\n'   # a comma inside their link
             "LLL,https://publisher.example/article?related=one%2C/export/screener&auth=public-signature\n"
             'MMM,"A headline, with commas, and https://publisher.example/a?x=1,2&auth=s in it"\n')
    assert make([Resp(200, links)])[0].export("screener", {}) == links
    after_a_link = 'Error,Detail\nbad request,"see https://publisher.example/a?x=1 and /export/screener?v=152&auth=x"\n'
    with pytest.raises(FinvizError, match="repeats the request's token"):            # their link does not hide ours beside it
        make([Resp(200, after_a_link)])[0].export("screener", {})
    # An encoded space inside their link must not cut it in two once decoded, leaving its tail
    # to be read as if it stood alone.
    spaced = ("Ticker,News URL\nAAA,https://publisher.example/share?title=Top%20Stocks&path=/export/screener&auth=pubsig\n"
              "BBB,https://publisher.example/share?title=Top%20Stocks&url=https%3A%2F%2Ffinviz.com%2Fquote.ashx%3Ft%3DAAA&auth=pubsig\n"
              "CCC,https://publisher.example/a?q=%22quoted%22%3Cb%3E&next=/export/screener&auth=s\n"
              "DDD,//publisher.example/share?path=/export/screener&auth=public-signature\n"       # a full address without its scheme
              "EEE,//cdn.publisher.example/x?u=finviz.com&auth=1\n")
    assert make([Resp(200, spaced)])[0].export("screener", {}) == spaced
    with pytest.raises(FinvizError, match="repeats the request's token"):            # the same form, on a Finviz host, is ours
        make([Resp(200, "Error,Detail\nbad request,//elite.finviz.com/export/screener?v=152&auth=x\n")])[0].export("screener", {})
    # An answer that is not a clean table is read in one piece: the commas in our own address
    # (c=1,65) must not scatter it across "cells".
    for ragged in ("Bad request: https://elite.finviz.com/export/screener?v=152&c=1,65&auth=111111\n",
                   "Error,Detail\nbad,https://elite.finviz.com/export/screener?v=152&c=1,65&auth=other\n",
                   "Error,Detail\nbad,/export/screener?v=152&c=1,65,66&auth=other\n"):
        with pytest.raises(FinvizError, match="repeats the request's token"):
            make([Resp(200, ragged)])[0].export("screener", {})
    for ours in ("https://elite.finviz.com/export/screener?v=152&auth=x", "https://FINVIZ.com/quote.ashx?t=A&auth=x",
                 "see /export/screener?v=152&auth=x", "next=%2Fexport%2Fscreener%3Fauth%3Dx", "https://user@elite.finviz.com:443/export/news?auth=x"):
        c, _ = make([Resp(200, f"Error,Detail\nbad request,{ours}\n")])
        with pytest.raises(FinvizError, match="repeats the request's token"):
            c.export("screener", {})


def test_token_matching_is_exact_about_the_token_and_loose_about_its_encoding():
    Client(token="abc/def")
    assert redact("bare a%62c%2Fdef here") == "bare <redacted> here"            # even a letter may arrive escaped
    assert redact("%61%62%63%2f%64%65%66") == "<redacted>" and redact("abc%252Fdef") == "<redacted>"
    Client(token="two words")
    for written in ("two words", "two+words", "two%20words", "two%2Bwords", "two%252Bwords"):     # a space, as forms write it, once or twice
        assert redact(f"[{written}]") == "[<redacted>]", written
    Client(token="secret.part")
    assert redact("secretXpart and secret-part stay") == "secretXpart and secret-part stay"       # a dot is a dot, not "any character"
    assert redact("secret.part and secret%2Epart go") == "<redacted> and <redacted> go"
    c, _ = make([Resp(200, "Ticker,Note\nAAA,secretXpart\n")], token="secret.part")
    assert "AAA" in c.export("screener", {})
    c, _ = make([Resp(200, "Error,Detail\nbad,a%62c%2Fdef\n")], token="abc/def")
    with pytest.raises(FinvizError, match="repeats the request's token"):
        c.export("screener", {})


def test_refused_token_and_other_statuses():
    c, _ = make([Resp(403, "")])
    with pytest.raises(FinvizError, match="refused"):
        c.export("screener", {})
    c, _ = make([Resp(404, "")])
    with pytest.raises(FinvizError, match="HTTP 404"):
        c.export("nothing", {})


def test_a_bug_in_the_call_is_not_retried():
    c, s = make([TypeError("bad argument")])
    with pytest.raises(FinvizError, match="could not be made"):
        c.export("screener", {})
    assert len(s.calls) == 1


def test_a_server_error_is_retried_with_a_growing_pause(clock):
    c, s = make([Resp(503, ""), Resp(502, ""), Resp()])
    assert "AAA" in c.export("screener", {})
    assert len(s.calls) == 3 and 3.0 in clock.sleeps and 6.0 in clock.sleeps
    c, s = make([Resp(500, "")] * 4)
    with pytest.raises(FinvizError, match="HTTP 500"):
        c.export("screener", {})
    assert len(s.calls) == 4


# ---------------------------------------------------------------- pacing

def test_rate_limit_holds_the_next_request_back(clock):
    c, s = make([Resp(429, "", {"Retry-After": "5"}), Resp()])
    assert "AAA" in c.export("screener", {})
    assert len(s.calls) == 2 and sum(clock.sleeps) == pytest.approx(5.0)


def test_giving_up_on_rate_limits_still_leaves_the_cooldown_for_the_next_command(clock):
    c, s = make([Resp(429, "")] * 4)
    with pytest.raises(FinvizError, match="rate limited"):
        c.export("screener", {})
    assert len(s.calls) == 4 and len(clock.sleeps) == 3            # three pauses between four tries, none after the last
    forget_process_state()                                         # the next command is a new process
    clock.sleeps.clear()
    nxt, _ = make([Resp()])
    nxt.export("screener", {})
    assert sum(clock.sleeps) == pytest.approx(5.0)                 # it sits out the cooldown the last 429 asked for


def test_requests_are_spaced_and_the_spacing_is_shared(clock, tmp_path):
    c, s = make([Resp(), Resp(), Resp()], min_interval=0.75)
    c.export("screener", {})
    c.export("screener", {})
    assert clock.sleeps == [pytest.approx(0.75)]
    forget_process_state()                                         # a second process: only the file knows
    other, _ = make([Resp()], min_interval=0.75)
    other.export("screener", {})
    assert clock.sleeps[-1] == pytest.approx(0.75) and (tmp_path / ".next_request").exists()


def test_a_cooldown_is_never_shortened_by_a_later_shorter_one(clock, tmp_path):
    c, _ = make([Resp()])
    c._hold(20.0)                                                  # one process was told to wait 20 seconds
    forget_process_state()
    other, _ = make([Resp()])
    other._hold(2.0)                                               # another is told 2 seconds a moment later
    forget_process_state()
    third, _ = make([Resp()])
    third.export("screener", {})
    assert sum(clock.sleeps) == pytest.approx(20.0)


def test_a_cooldown_published_while_a_request_waits_is_honoured(clock, monkeypatch, tmp_path):
    c, s = make([Resp(), Resp()], min_interval=0.75)
    c.export("screener", {})
    real_sleep, published = clock.sleep, []

    def sleep(seconds):
        real_sleep(seconds)
        if not published:                                          # meanwhile another process hit a 429
            published.append(clock.now + 5.0)
            (tmp_path / ".next_request").write_text(f"{published[0]:.6f} {published[0]:.6f}")

    monkeypatch.setattr(client.time, "sleep", sleep)
    c.export("screener", {})
    assert len(s.calls) == 2 and clock.now >= published[0]         # it did not send inside the cooldown
    assert clock.sleeps == [pytest.approx(0.75), pytest.approx(5.0)]


def test_a_stale_pacing_file_cannot_stall_a_request(clock, tmp_path):
    (tmp_path / ".next_request").write_text(f"{clock.now + 86400} {clock.now + 86400}")
    c, _ = make([Resp()])
    c.export("screener", {})
    assert clock.sleeps == []
    forget_process_state()
    (tmp_path / ".next_request").write_text("not a number")        # unreadable: treated as no constraint
    make([Resp()])[0].export("screener", {})
    assert clock.sleeps == []


def test_pacing_state_that_is_not_a_finite_number_is_ignored(clock, tmp_path):
    for text in ("nan 0 0", "0 nan 0", "inf inf inf", "1e400 0 0", ""):
        forget_process_state()
        (tmp_path / ".next_request").write_text(text)
        make([Resp()])[0].export("screener", {})               # returns: no endless loop on a deadline that never compares
    assert clock.sleeps == []
    c, s = make([Resp(429, "", {"Retry-After": "nan"}), Resp(429, "", {"Retry-After": "inf"}), Resp()])
    forget_process_state()
    (tmp_path / ".next_request").unlink()
    c.export("screener", {})
    assert len(s.calls) == 3 and clock.sleeps == [pytest.approx(5.0), pytest.approx(5.0)]       # the usual cooldown, not none


def test_a_long_queue_is_kept_but_a_damaged_or_future_one_is_not(clock, tmp_path):
    pace = tmp_path / ".next_request"
    pace.write_text(f"{clock.now + 90:.6f} 0 {clock.now:.6f}")           # 120 requests are queued by other processes
    make([Resp()])[0].export("screener", {})
    assert clock.sleeps == [pytest.approx(90.0)]                         # it joins the queue instead of jumping it
    forget_process_state()
    clock.sleeps.clear()
    pace.write_text(f"{clock.now + 5000:.6f} 0 {clock.now:.6f}")         # further than any queue reaches: damage
    make([Resp()])[0].export("screener", {})
    assert clock.sleeps == []
    forget_process_state()
    pace.write_text(f"{clock.now + 3600:.6f} {clock.now + 3600:.6f} {clock.now + 3590:.6f}")    # stamped in the future: the clock went back
    make([Resp()])[0].export("screener", {})
    assert clock.sleeps == []


def test_a_request_that_wakes_long_after_its_slot_takes_a_new_one(clock, monkeypatch):
    c, s = make([Resp(), Resp()], min_interval=0.75)
    c.export("screener", {})
    real_sleep, overslept = clock.sleep, []

    def sleep(seconds):
        real_sleep(seconds)
        if not overslept:                                      # the machine was suspended: it comes back 30 seconds late
            overslept.append(True)
            clock.now += 30.0

    taken = []
    reserve = c._reserve
    monkeypatch.setattr(c, "_reserve", lambda: taken.append(reserve()) or taken[-1])
    monkeypatch.setattr(client.time, "sleep", sleep)
    c.export("screener", {})
    assert len(taken) == 2 and taken[1][0] == pytest.approx(clock.now) and len(s.calls) == 2   # the stale slot was given up


def test_a_slow_disk_is_not_mistaken_for_oversleeping(clock, monkeypatch):
    """Publishing a reservation takes a second on this disk, longer than the spacing itself.
    That must not look like waking late, or the request would reserve again for ever."""
    def slow_write(path, text):
        clock.now += 1.0
        path.write_text(text)

    monkeypatch.setattr(client, "write_text_atomic", slow_write)
    c, s = make([Resp(), Resp()], min_interval=0.75)
    taken = []
    reserve = c._reserve
    monkeypatch.setattr(c, "_reserve", lambda: taken.append(reserve()) or taken[-1])
    c.export("screener", {})
    c.export("screener", {})
    assert len(s.calls) == 2 and len(taken) == 2               # one reservation each


def test_a_request_gives_up_slots_only_so_many_times(clock, monkeypatch):
    c, s = make([Resp()], min_interval=0.75)
    monkeypatch.setattr(c, "_cooldown", lambda: clock.now + 5.0)        # every look finds a cooldown just published
    taken = []
    reserve = c._reserve
    monkeypatch.setattr(c, "_reserve", lambda: taken.append(reserve()) or taken[-1])
    c.export("screener", {})
    assert len(taken) == client.MAX_RESERVATIONS and len(s.calls) == 1  # then it goes, and a 429 would answer


def test_without_a_writable_folder_pacing_stays_in_the_process_and_says_so(clock, monkeypatch):
    class NoLock:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise client.Unavailable("read-only")

    def no_write(*a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(client, "FileLock", NoLock)
    monkeypatch.setattr(client, "write_text_atomic", no_write)
    c, s = make([Resp(), Resp(), Resp()], min_interval=0.75)
    with pytest.warns(RuntimeWarning, match="not shared with other finviz processes") as caught:
        c.export("screener", {})
        c.export("screener", {})
        c.export("screener", {})
    assert len(s.calls) == 3 and clock.sleeps == [pytest.approx(0.75), pytest.approx(0.75)]     # still spaced, inside this process
    assert len({str(w.message) for w in caught}) == len(caught) <= 2                            # each thing said once


def test_the_real_lock_on_a_folder_that_takes_no_files(clock, tmp_path, unwritable, monkeypatch):
    """No stand-in for the lock here: the folder itself refuses, as a read-only one does, and
    FileLock has to work that out for itself (os.access would say yes on Windows)."""
    import time as real_time

    from finviz_cli.files import FileLock, Unavailable
    unwritable(tmp_path)
    started = real_time.monotonic()
    with pytest.raises(Unavailable):
        with FileLock(tmp_path / ".pace.lock", timeout=5.0):
            pytest.fail("entered a lock in a folder that takes no files")
    assert real_time.monotonic() - started < 2.0                # said at once, not after waiting out the timeout
    c, s = make([Resp(), Resp()], min_interval=0.75)
    with pytest.warns(RuntimeWarning, match="not shared with other finviz processes"):
        c.export("screener", {})
        c.export("screener", {})
    assert len(s.calls) == 2 and clock.sleeps == [pytest.approx(0.75)]      # the requests went out, spaced in this process
    assert not (tmp_path / ".next_request").exists() and not (tmp_path / ".pace.lock").exists()


def test_a_lock_file_this_user_may_not_write_is_unavailable_not_busy(tmp_path):
    import stat
    import time as real_time

    from finviz_cli.files import FileLock, Unavailable
    path = tmp_path / "x.lock"
    path.write_text("")
    os.chmod(path, stat.S_IREAD)                               # read-only: someone else's, or locked down
    try:
        started = real_time.monotonic()
        with pytest.raises(Unavailable):
            with FileLock(path, timeout=5.0):
                pytest.fail("entered a lock it could not open")
        assert real_time.monotonic() - started < 2.0
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def test_the_client_records_when_the_answered_request_left(clock):
    c, s = make([Resp(429, "", {"Retry-After": "5"}), Resp()])
    began = clock.now
    c.export("screener", {})
    assert c.last_sent == pytest.approx(began + 5.0) and c.last_received >= c.last_sent        # the second request, after the cooldown
    c2, _ = make([Resp(403, "")])
    with pytest.raises(FinvizError):
        c2.export("screener", {})
    assert c2.last_sent is None and c2.last_received is None                                    # nothing was answered
    c3, _ = make([Resp(200, "<html>Sign in</html>")])
    with pytest.raises(FinvizError, match="web page"):
        c3.export("screener", {})
    assert c3.last_sent is None and c3.last_received is None                                    # an answer, but not data
    c4, _ = make([Resp(), Resp(403, "")])
    c4.export("screener", {})
    assert c4.last_sent is not None
    with pytest.raises(FinvizError):
        c4.export("screener", {})
    assert c4.last_sent is None and c4.last_received is None                                    # the earlier call's times do not linger
    c5, s5 = make([Resp()])
    c5.export("screener", {})
    with pytest.raises(FinvizError, match="not as a parameter"):
        c5.export("screener", {"auth": "x"})                                                    # refused before any request is made
    assert c5.last_sent is None and c5.last_received is None and len(s5.calls) == 1             # cleared all the same


def test_it_is_the_folder_probe_that_says_a_lock_cannot_exist(tmp_path, monkeypatch):
    """The lock file will not open, with an error that is not a refusal (a read-only volume
    says EROFS; Windows says write-protected). Whether a lock can ever exist here is then
    decided by trying to make a file in the folder: if that works, keep waiting."""
    import builtins
    import errno

    from finviz_cli import files
    from finviz_cli.files import Busy, FileLock, Unavailable

    def read_only_volume(path, mode="r", *a, **k):
        if str(path).endswith("x.lock"):
            exc = OSError(errno.EROFS, "Read-only file system", str(path))
            exc.winerror = 19
            raise exc
        return builtins.open(path, mode, *a, **k)

    def no_probe(*a, **k):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(files, "open", read_only_volume, raising=False)
    with pytest.raises(Busy):                                    # the folder takes files: not hopeless, so it waits its time out
        with FileLock(tmp_path / "x.lock", timeout=0.3):
            pytest.fail("entered")
    assert not list(tmp_path.glob(".probe.*"))                   # and the probe cleans up after itself
    monkeypatch.setattr(files.tempfile, "mkstemp", no_probe)
    with pytest.raises(Unavailable):                             # the folder takes none: hopeless, said at once
        with FileLock(tmp_path / "x.lock", timeout=5.0):
            pytest.fail("entered")


def test_a_refused_lock_file_and_a_busy_one_are_told_apart(tmp_path, monkeypatch):
    """Windows reports both as PermissionError. Its own code says which: 5 is access denied
    (an ACL: waiting will not help), 32 is a sharing violation (someone has it: wait)."""
    import builtins
    import time as real_time

    from finviz_cli import files
    from finviz_cli.files import Busy, FileLock, Unavailable

    def refusing(code):
        def guarded(path, mode="r", *a, **k):
            if str(path).endswith("x.lock"):
                exc = PermissionError(13, "Permission denied", str(path))
                exc.winerror = code
                raise exc
            return builtins.open(path, mode, *a, **k)
        return guarded

    monkeypatch.setattr(files, "open", refusing(5), raising=False)
    started = real_time.monotonic()
    with pytest.raises(Unavailable):
        with FileLock(tmp_path / "x.lock", timeout=5.0):
            pytest.fail("entered")
    assert real_time.monotonic() - started < 2.0                 # at once
    monkeypatch.setattr(files, "open", refusing(32), raising=False)
    started = real_time.monotonic()
    with pytest.raises(Busy):
        with FileLock(tmp_path / "x.lock", timeout=0.3):
            pytest.fail("entered")
    assert real_time.monotonic() - started >= 0.3                # it kept trying for as long as it was given


def test_damaged_bytes_in_the_pacing_file_are_not_a_crash(clock, tmp_path):
    (tmp_path / ".next_request").write_bytes(b"\xff\xfe\x81 garbage \x00")
    make([Resp()])[0].export("screener", {})
    assert clock.sleeps == []


def test_a_token_is_found_however_much_of_it_was_encoded():
    slashed = "part1/part2&part3"
    Client(token=slashed)
    for written in ("auth=part1/part2%26part3&v=1", "auth=part1%2Fpart2&part3", "bare part1/part2%2526part3 here",
                    "auth%3Dpart1%252Fpart2%2526part3"):
        shown = redact(written)
        assert "part1" not in shown and "part2" not in shown and "part3" not in shown, (written, shown)
    plus = "part1/part2+part3"
    for echoed in ("part1%2Fpart2+part3", "part1/part2%2Bpart3", "part1%2fpart2%2bpart3", plus):
        c, _ = make([Resp(200, f"Error,Detail\nbad,{echoed}\n")], token=plus)
        with pytest.raises(FinvizError, match="repeats the request's token"):
            c.export("screener", {})
    c, _ = make([Resp(200, "Ticker,Note\nAAA,part1 part2 part3\n")], token=plus)      # the words, not the token
    assert "AAA" in c.export("screener", {})


def test_a_lock_is_never_reported_as_held_when_it_is_not(tmp_path):
    from finviz_cli.files import Busy, FileLock, Unavailable
    path = tmp_path / "x.lock"
    with FileLock(path):
        with pytest.raises(Busy):
            with FileLock(path, timeout=0.2):                  # another holder, even inside one process
                pytest.fail("entered a lock that someone else holds")
    with FileLock(path, timeout=0.2):                          # released: free again
        pass
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    with pytest.raises(Unavailable):
        with FileLock(blocker / "nested" / "x.lock", timeout=0.2):       # nowhere to put a lock
            pytest.fail("entered a lock that cannot exist")


def test_processes_never_take_the_same_slot(tmp_path):
    """Three real processes reserving at once: every slot is taken once and they are spaced."""
    code = ("from finviz_cli.client import Client\n"
            "c = Client(token='t', min_interval=0.2)\n"
            "print(' '.join(repr(c._reserve()[0]) for _ in range(8)))\n")
    # The children run somewhere empty (no .env of anyone's beside them) and import this same
    # package, wherever it was imported from here.
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(client.__file__)))
    env = {**os.environ, "FINVIZ_CLI_HOME": str(tmp_path), "PYTHONPATH": package_root}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for _ in range(3)]
    slots = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        slots += [float(x) for x in out.split()]
    slots.sort()
    gaps = [b - a for a, b in zip(slots, slots[1:])]
    assert len(slots) == 24 and min(gaps) >= 0.2 - 1e-4, gaps


# ---------------------------------------------------------------- what Finviz says when it refuses, and the name of what it sends

@pytest.mark.parametrize("status, body, shown", [
    (400, "Ticker not found.", ": Ticker not found."),
    (400, " Invalid query\n", ": Invalid query"),
    (400, BOM + "Ticker not found", ": Ticker not found"),
    (404, "Not found", ": Not found"),
])
def test_finvizs_own_reason_for_a_refusal_is_passed_on(status, body, shown):
    c, _ = make([Resp(status, body)])
    with pytest.raises(FinvizError) as e:
        c.export("stock", {"t": "ZZZZ"})
    assert str(e.value) == f"Finviz answered HTTP {status} for /export/stock" + shown


@pytest.mark.parametrize("body", [
    "", "   ", "<html><body>Bad request</body></html>", "Invalid query: /export/stock?t=A&auth=" + TOKEN,
    "No such thing: " + TOKEN,                                   # plain characters, but it is the token
    "no such id 99999999-8888-7777-6666-555555555555",           # shaped like a token, whoever's it is
    "see https://elite.finviz.com/export/stock", "line one\nline two", "x" * 81, "bad = value", "100% wrong", "tab\there",
    "caf" + chr(233) + " closed",
])
def test_a_reason_that_is_not_a_few_plain_words_is_left_out(body):
    c, _ = make([Resp(400, body)])
    with pytest.raises(FinvizError) as e:
        c.export("stock", {"t": "ZZZZ"})
    assert str(e.value) == "Finviz answered HTTP 400 for /export/stock" and TOKEN not in str(e.value)


def test_a_refused_token_and_a_server_error_say_what_they_always_said():
    c, _ = make([Resp(403, "Forbidden")])
    with pytest.raises(FinvizError, match=r"^Finviz refused the token \(HTTP 403\)$"):
        c.export("screener", {})
    c, _ = make([Resp(503, "Service Unavailable")] * 4)
    with pytest.raises(FinvizError, match=r"gave up on /export/screener after 4 tries: Finviz error \(HTTP 503\)$"):
        c.export("screener", {})


def test_the_name_of_the_file_finviz_sends_is_kept_when_it_is_a_plain_name():
    name = "finviz_msft_balanceQuarterly_usd_11-11-2026_13-44-28.csv"

    def named(value):
        return Resp(200, headers={"Content-Disposition": value})

    c, _ = make([named(f"attachment; filename={name}; filename*=UTF-8''{name}"), Resp(200), Resp(400, "Ticker not found"),
                 named(f'attachment; filename="{name}"'), named('attachment; filename="two words.csv"'),
                 named(f"attachment; filename=finviz_{TOKEN}.csv"), named("attachment; filename=../../somewhere/else.csv"),
                 named("inline"), named(f"attachment; FILENAME={name}")])
    c.export("stock", {"t": "MSFT"})
    assert c.last_filename == name
    c.export("stock", {"t": "MSFT"})
    assert c.last_filename == ""                                 # no name this time: nothing is left over from the call before
    c.last_filename = "left over"
    with pytest.raises(FinvizError):
        c.export("stock", {"t": "ZZZZ"})
    assert c.last_filename == ""                                 # nor after a failure
    for expected in (name, "", "", "", "", name):                # quoted; with a space; carrying the token; a path; none; any case
        c.export("stock", {"t": "MSFT"})
        assert c.last_filename == expected


# ---------------------------------------------------------------- a .env line with quotes and a comment, and where AWS is asked

@pytest.mark.parametrize("text, token", [
    ('FINVIZ_API_KEY="my-token" # Finviz Elite\n', "my-token"),
    ("FINVIZ_API_KEY='my-token'   # Finviz Elite\n", "my-token"),
    ('FINVIZ_API_KEY="part1 # part2" # the comment\n', "part1 # part2"),
    ('FINVIZ_API_KEY="has \'both\' kinds"\n', "has 'both' kinds"),
    ('FINVIZ_API_KEY="closed"#no space\n', "closed"),
])
def test_a_quoted_value_may_be_followed_by_a_comment(tmp_path, text, token):
    (tmp_path / ".env").write_text(text, "utf-8")
    assert client.load_token() == token


@pytest.mark.parametrize("text", [
    'FINVIZ_API_KEY="" # leave blank to use the other file\n',       # blank is blank, quoted or not
    "FINVIZ_API_KEY= # leave blank to use the other file\n",         # ... and the comment after it is not the token
    "FINVIZ_API_KEY=#no space either\n", "FINVIZ_API_KEY=\t# a tab before it\n",
    'FINVIZ_API_KEY=an-earlier-line\nFINVIZ_API_KEY="never closed\n',  # a broken later line does not leave the earlier one standing
    'FINVIZ_API_KEY="never closed\n', "FINVIZ_API_KEY='never closed # really\n",
    'FINVIZ_API_KEY="closed" and more\n', 'FINVIZ_API_KEY="a""b"\n', 'FINVIZ_API_KEY="\n', "FINVIZ_API_KEY='\n",
])
def test_a_quoted_value_that_is_blank_or_not_cleanly_closed_is_no_token(tmp_path, nothing_of_the_developers_own, text):
    (nothing_of_the_developers_own / ".env").write_text(text, "utf-8")
    with pytest.raises(FinvizError, match="no Finviz token"):
        client.load_token()
    (tmp_path / ".env").write_text("FINVIZ_API_KEY=in-the-data-folder\n", "utf-8")
    assert client.load_token() == "in-the-data-folder"               # and it does not stand in the way of the next place


def test_a_file_is_only_opened_when_nothing_before_it_had_a_token(monkeypatch, tmp_path, nothing_of_the_developers_own):
    opened = []
    real = client._env_file
    monkeypatch.setattr(client, "_env_file", lambda path: opened.append(path.parent) or real(path))
    (nothing_of_the_developers_own / ".env").write_text("FINVIZ_API_KEY=in-this-folder\n", "utf-8")
    (tmp_path / ".env").write_text("FINVIZ_API_KEY=in-the-data-folder\n", "utf-8")
    monkeypatch.setenv("FINVIZ_API_KEY", "in-the-environment")
    assert client.load_token() == "in-the-environment" and opened == []
    monkeypatch.delenv("FINVIZ_API_KEY")
    assert client.load_token() == "in-this-folder" and opened == [nothing_of_the_developers_own]
    (nothing_of_the_developers_own / ".env").unlink()
    assert client.load_token() == "in-the-data-folder" and opened[1:] == [nothing_of_the_developers_own, tmp_path]


class NoRegionError(Exception):
    """Named as botocore's own."""


def aws(monkeypatch, fail_without_region=False, fail_always=None):
    """A stand-in for boto3 that records the region each client was made for."""
    made = []

    class Secrets:
        def get_secret_value(self, SecretId):
            return {"SecretString": "from-aws"}

    def make_client(service, **kwargs):
        made.append(kwargs.get("region_name", "(AWS's own settings)"))
        if fail_always:
            raise fail_always
        if fail_without_region and "region_name" not in kwargs:
            raise NoRegionError("You must specify a region.")
        return Secrets()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=make_client))
    return made


def test_the_region_comes_from_the_environment_then_the_data_folder_then_awss_own_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("FINVIZ_SECRET_ID", "a/secret")
    made = aws(monkeypatch)
    assert client.load_token() == "from-aws" and made == ["(AWS's own settings)"]         # nothing named: left to AWS's settings
    (tmp_path / ".env").write_text("AWS_DEFAULT_REGION=eu-central-1\nAWS_REGION=eu-west-1\n", "utf-8")
    client.load_token()
    assert made[-1] == "eu-west-1"                                                        # the data folder's .env
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    client.load_token()
    assert made[-1] == "ap-south-1"                                                       # the environment before it
    monkeypatch.setenv("AWS_REGION", "  us-west-2 ")
    client.load_token()
    assert made[-1] == "us-west-2"


def test_with_no_region_anywhere_awss_first_region_is_tried(monkeypatch):
    monkeypatch.setenv("FINVIZ_SECRET_ID", "a/secret")
    made = aws(monkeypatch, fail_without_region=True)
    assert client.load_token() == "from-aws" and made == ["(AWS's own settings)", "us-east-1"]
    made = aws(monkeypatch, fail_always=RuntimeError(f"could not start with key {TOKEN}"))
    with pytest.raises(FinvizError) as e:
        client.load_token()
    assert "RuntimeError" in str(e.value) and TOKEN not in str(e.value) and e.value.__context__ is None and len(made) == 1
    monkeypatch.setenv("AWS_REGION", "eu-west-1")                                         # a region that was named is not second-guessed
    made = aws(monkeypatch, fail_always=NoRegionError("still none"))
    with pytest.raises(FinvizError, match="NoRegionError"):
        client.load_token()
    assert made == ["eu-west-1"]


def test_a_blank_secret_name_in_the_environment_does_not_hide_the_one_in_the_data_folder(monkeypatch, tmp_path):
    aws(monkeypatch)
    monkeypatch.setenv("FINVIZ_SECRET_ID", "   ")
    with pytest.raises(FinvizError, match="no Finviz token"):
        client.load_token()
    (tmp_path / ".env").write_text("FINVIZ_SECRET_ID = 'mine/in the data folder'  # quoted, with a comment\n", "utf-8")
    assert client.load_token() == "from-aws"


def test_a_comment_after_a_blank_setting_is_never_sent_anywhere(monkeypatch, tmp_path):
    """`NAME= # a note` in the data folder's .env: the note is not a secret's name or a region."""
    made = aws(monkeypatch)
    (tmp_path / ".env").write_text("FINVIZ_SECRET_ID= # kept in the environment on this machine\nAWS_REGION= # the default\n", "utf-8")
    with pytest.raises(FinvizError, match="no Finviz token"):
        client.load_token()
    assert made == []                                                   # AWS was not asked for a secret named after a comment
    monkeypatch.setenv("FINVIZ_SECRET_ID", "a/secret")
    assert client.load_token() == "from-aws" and made == ["(AWS's own settings)"]         # nor a region named after one
