"""The only module that talks to Finviz.

Finviz Elite authenticates with a token in the URL, so the full URL must never reach a log,
an error message, a traceback or a file. Everything here reports failures without it, and
`redact` is applied to anything that could carry it.

Limits (docs/api-reference.md): 10 requests per rolling 5 seconds, shared by every export
endpoint on the account, and that includes other programs using the same token. A 429 comes
with Retry-After: 5.

Pacing is coordinated between the processes on this machine through a lock file. Programs on
other machines share the same account budget and cannot be seen from here; against those the
only protection is backing off on a 429.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import math
import os
import random
import re
import threading
import time
import traceback
import urllib.parse
import warnings
from pathlib import Path

import requests

from finviz_cli import config
from finviz_cli.files import Busy, FileLock, Unavailable, write_text_atomic

BASE = "https://elite.finviz.com/export/"
# auth=<token>, also percent-encoded (auth%3d... inside a redirect's return URL), up to the next
# parameter separator in either form.
_AUTH = re.compile(r"(auth(?:=|%(?:25)*3d))(?:(?!%(?:25)*26)[^&\s\"'<>])+", re.IGNORECASE)
_TOKEN_SHAPE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
# (host, everything after it), with or without the scheme: //host/path is a full address too.
# Read inside one value at a time, so a comma is part of the link.
_ABSOLUTE_URL = re.compile(r"(?:https?:)?//([^/\s\"'<>?#]+)([^\s\"'<>]*)", re.IGNORECASE)
_HEX_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_KNOWN_TOKENS: dict[str, re.Pattern] = {}             # token -> the pattern that finds it, however it is encoded
MIN_INTERVAL = 0.75             # seconds between requests: at most 7 in any 5 seconds, leaving room for others
MAX_QUEUE = 600.0               # a slot further than this beyond the moment it was written is not a queue: it is damage
MAX_RESERVATIONS = 6            # times one request will give up a slot and take a new one before it just goes
MAX_HOLD = 31.0                 # the same for a cooldown (a 429 asks for 30 seconds at most here)
CLOCK_SLACK = 2.0               # pacing state "written" later than now by more than this: the clock went back
LOCK_TIMEOUT = 10.0             # the pacing lock is held for microseconds; waiting this long means something is wrong
BUSY = "could not reserve a request slot: another finviz process has held the pacing lock too long"
ENV_FILE = ".env"
TOKEN_NAMES = ("FINVIZ_API_KEY", "FINVIZ_AUTH_TOKEN")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PLAIN_WORDS = re.compile(r"[A-Za-z0-9 .,:;'()_\-]{1,80}")          # no =, &, ?, /, % or markup: not a URL, not a page
_FILE_NAME = re.compile(r'filename=(?:"([A-Za-z0-9._\-]{1,120})"|([A-Za-z0-9._\-]{1,120})(?=\s*(?:;|$)))', re.IGNORECASE)
_NOISY = ("urllib3", "requests", "botocore", "boto3")
_WITHHELD = "<log line withheld>"
_warned: set[str] = set()


class FinvizError(Exception):
    """A failure talking to Finviz. The message never contains the request URL."""


def _warn_once(key: str, message: str) -> None:
    """Say it once per process, on stderr: a degraded mode must not be a silent one."""
    if key not in _warned:
        _warned.add(key)
        warnings.warn(message, RuntimeWarning, stacklevel=3)


def _any_case(hex_pair: str) -> str:
    """A regex for two hex digits written in either letter case (2F, 2f)."""
    return "".join(f"[{c.upper()}{c.lower()}]" if c.isalpha() else c for c in hex_pair)


def _escape_of(text: str) -> str:
    """A regex for `text` percent-escaped: hex digits in either case, and the % itself
    possibly escaped again, any number of times (%2F, %2f, %252F, %25252f)."""
    return "".join("%(?:25)*" + _any_case(f"{byte:02X}") for byte in text.encode("utf-8"))


def _encodings(token: str) -> re.Pattern:
    """One pattern for every way a token gets written inside a URL, an echoed URL, or a URL
    inside a URL. Character by character, each one is either itself (exactly: a dot is a
    dot) or its percent-escape. That holds for letters and digits too, which nothing needs
    to escape but anything may. A space may also be a +, plain or escaped. So a token is
    found whether all of it, some of it or none of it was encoded, once or several times."""
    parts = []
    for ch in token:
        forms = [re.escape(ch), _escape_of(ch)]
        if ch == " ":
            forms += [r"\+", _escape_of("+")]
        parts.append("(?:" + "|".join(forms) + ")")
    return re.compile("".join(parts))


def _remember(token: str) -> None:
    if token and token not in _KNOWN_TOKENS:
        _KNOWN_TOKENS[token] = _encodings(token)


def redact(text: object) -> str:
    out = str(text)
    # Known tokens first: a token holding an & would otherwise be cut short where the auth=
    # pattern stops at the next parameter.
    for pattern in list(_KNOWN_TOKENS.values()):
        out = pattern.sub("<redacted>", out)
    return _TOKEN_SHAPE.sub("<redacted>", _AUTH.sub(r"\1<redacted>", out))


def _echoes(body: str, token: str, path: str = "") -> bool:
    """True when an answer carries the request back: the token itself, or this request's own
    address with its auth parameter (a URL on a Finviz host, or the export path that was
    asked for standing on its own), plain, encoded or encoded twice. A publisher's link is
    judged by its host: one that has an auth parameter of its own, or mentions finviz.com
    somewhere in its query, is their data and is left alone."""
    if token and _encodings(token).search(body):          # the token, however much of it was encoded
        return True
    # The address this request went to, as a relative path. It only counts where it stands on
    # its own, not as the tail of somebody else's address (https://other.example/export/...).
    relative = re.compile(r"(?<![\w./:%-])/export/" + re.escape(path.strip("/")) + r"[^\s\"'<>]*[?&]auth=", re.IGNORECASE) if path else None
    return any(_names_request(cell, relative) for cell in _suspect_cells(body))


def _suspect_cells(body: str) -> list[str]:
    """The pieces of an answer worth a closer look, each one a whole value.

    When the answer is a clean table (it parses strictly and every row is as wide as the
    header) the pieces are its cells, so a quoted link with a comma in it stays whole. Any
    other answer (an error text, a ragged table: just where an unquoted address with commas
    in it would be) is looked at in one piece. Only pieces that could hold an auth parameter,
    plain or percent-encoded, are returned."""
    try:
        rows = list(csv.reader(io.StringIO(body), strict=True))
    except csv.Error:
        rows = []
    table = len(rows) >= 2 and len(rows[0]) >= 2 and all(len(r) == len(rows[0]) for r in rows)
    cells = [cell for row in rows for cell in row] if table else [body]
    return [c for c in cells if "auth" in c.lower() or _HEX_ESCAPE.search(c)]


def _is_finviz(host: str) -> bool:
    host = host.lower().rsplit("@", 1)[-1].split(":")[0]
    return host == "finviz.com" or host.endswith(".finviz.com")


def _names_request(text: str, relative: re.Pattern | None) -> bool:
    """True when one value holds this request's own address with an auth parameter."""
    for _ in range(3):                         # as sent, decoded once, decoded twice
        for host, rest in _ABSOLUTE_URL.findall(text):
            if _is_finviz(host) and re.search(r"[?&]auth=", rest, re.IGNORECASE):
                return True                    # a Finviz address with an auth parameter: the request itself
        # The relative form is looked for outside every full address: whatever a publisher's
        # link carries in its own query (a path like ours included) belongs to that link.
        if relative is not None and relative.search(_ABSOLUTE_URL.sub(" ", text)):
            return True
        # Somebody else's link is done with here, whole. It is taken out before decoding, or a
        # %20 inside it would cut it in two and its tail would be read as text of its own.
        rest = _ABSOLUTE_URL.sub(lambda m: m.group(0) if _is_finviz(m.group(1)) else " ", text)
        decoded = urllib.parse.unquote(rest)
        if decoded == rest:
            return False
        text = decoded
    return False


_factory_installed = False
_withholding = 0                # >0 while a secret is being fetched: HTTP library records are blanked


def _scrub(record: logging.LogRecord) -> None:
    """Make one HTTP-library log record safe: message, exception text and stack text."""
    if _withholding:
        record.msg, record.args = _WITHHELD, ()
        record.exc_info = record.exc_text = record.stack_info = None
        return
    try:
        record.msg, record.args = redact(record.getMessage()), ()
    except Exception:
        record.msg, record.args = _WITHHELD, ()
    # A formatter appends the exception by itself, from exc_info. Render it here instead, so the
    # text it appends has been through redact (a urllib3 error carries the full URL).
    if record.exc_info:
        try:
            text = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info[0] else ""
        except Exception:
            text = "<exception withheld>"
        record.exc_text, record.exc_info = (redact(text).rstrip("\n") or None), None
    elif record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


def install_log_redaction() -> None:
    """HTTP libraries log full URLs at DEBUG. Rewrite their records as they are created, so the
    token cannot ride along whichever logger or handler they pass through."""
    global _factory_installed
    if _factory_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if record.name.startswith(_NOISY):
            _scrub(record)
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


@contextlib.contextmanager
def _secret_fetch():
    """While the secret is on the wire nothing the HTTP libraries log is passed on. Its value
    is not known yet, so it could not be recognised in a logged response body (botocore's
    parsers log bodies at DEBUG, whatever level their parent logger has)."""
    global _withholding
    install_log_redaction()
    _withholding += 1
    try:
        yield
    finally:
        _withholding -= 1


def _env_file(path: Path) -> dict[str, str]:
    """The NAME=value lines of a .env file. Nothing when it is not there or cannot be read.

    Only the plain form is read: an optional `export`, a name, `=`, and a value with or
    without quotes around it. `#` starts a comment on a line of its own, or after an
    unquoted value when a space comes before it. Nothing is expanded, and nothing is put
    into the process's environment: the caller asks for the names it wants.
    """
    try:
        text = path.read_text("utf-8-sig")
    except (OSError, UnicodeError):
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[7:].lstrip()
        name, equals, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not equals or not _ENV_NAME.fullmatch(name):                # blank, a comment, or not an assignment
            continue
        if value[:1] in ('"', "'"):
            # Quoted: what is between the quotes, whatever it holds. After the closing quote
            # there may be a comment and nothing else; a quote that never closes, or more
            # text after it, is not a value to guess at.
            end = value.find(value[0], 1)
            rest = value[end + 1:].lstrip() if end != -1 else None
            if end == -1 or (rest and not rest.startswith("#")):
                found.pop(name, None)                       # nor does an earlier line for the same name stand in for it
                continue
            value = value[1:end]
        elif value.startswith("#"):
            value = ""                                      # `NAME= # a comment`: nothing was given, and the comment is not it
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0].strip()
        found[name] = value
    return found


def _current_folder() -> Path:
    return Path.cwd()


def _first_set(*values: str | None) -> str:
    """The first of these that is more than blanks, without the blanks around it."""
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def _folder_env() -> dict[str, str]:
    try:
        return _env_file(_current_folder() / ENV_FILE)
    except OSError:                                         # the current folder is gone
        return {}


def load_token() -> str:
    """The token, from the first place that has one: FINVIZ_API_KEY in the environment, a
    .env file in the current folder, a .env file in the data folder, and last the AWS secret
    named by FINVIZ_SECRET_ID when that is set."""
    home: dict[str, str] = {}
    for place in ("environment", "folder", "home"):         # each file is opened only if nothing before it had a token
        if place == "home":
            home = _env_file(config.home() / ENV_FILE)
        source = os.environ if place == "environment" else _folder_env() if place == "folder" else home
        for name in TOKEN_NAMES:
            value = (source.get(name) or "").strip()
            if value:
                _remember(value)
                return value
    # The secret's name comes from the environment or from the data folder's own .env, never
    # from a .env that merely happens to be in the current folder: whatever that one named
    # would be fetched from the account and sent out as the token.
    secret_id = _first_set(os.environ.get("FINVIZ_SECRET_ID"), home.get("FINVIZ_SECRET_ID"))
    if not secret_id:
        raise FinvizError("no Finviz token: put FINVIZ_API_KEY in the environment or in a .env file "
                          "(in the current folder, or in the data folder)")
    try:
        import boto3
    except ImportError:
        raise FinvizError("FINVIZ_SECRET_ID is set, but boto3 is not installed to read the secret from AWS") from None
    region = _first_set(os.environ.get("AWS_REGION"), os.environ.get("AWS_DEFAULT_REGION"),
                        home.get("AWS_REGION"), home.get("AWS_DEFAULT_REGION"))
    failure = None
    raw = ""
    with _secret_fetch():
        # No exception is raised from inside an except block here: the one caught could carry
        # the secret, and would ride along as the context of the next.
        secrets, unplaced = None, False
        try:
            secrets = boto3.client("secretsmanager", **({"region_name": region} if region else {}))
        except Exception as exc:
            unplaced = not region and type(exc).__name__ == "NoRegionError"
            failure = None if unplaced else type(exc).__name__
        if unplaced:                                        # no region named anywhere, AWS's own settings included
            try:
                secrets = boto3.client("secretsmanager", region_name="us-east-1")
            except Exception as exc:
                failure = type(exc).__name__
        if secrets is not None and not failure:
            try:
                raw = secrets.get_secret_value(SecretId=secret_id)["SecretString"]
            except Exception as exc:
                failure = type(exc).__name__
    if failure:
        raise FinvizError(f"no Finviz token: reading the AWS secret named by FINVIZ_SECRET_ID failed ({failure})")
    token = ""
    try:
        data = json.loads(raw)
    except ValueError:
        token = raw.strip()
    else:
        if isinstance(data, dict):
            for key in ("FINVIZ_API_KEY", "FINVIZ_AUTH_TOKEN", "token", "auth"):
                if data.get(key):
                    token = str(data[key]).strip()
                    break
    if not token:
        raise FinvizError("the AWS secret does not hold a FINVIZ_API_KEY value")
    _remember(token)
    return token


class Client:
    """Throttled, token-safe access to the Finviz export endpoints. Returns CSV text."""

    _lock = threading.Lock()
    _next = 0.0                     # earliest wall-clock time the next request slot is free
    _hold_until = 0.0               # no request before this time (set by a 429)
    _stamp = 0.0                    # when this process last set those two

    def __init__(self, token: str | None = None, session: requests.Session | None = None,
                 min_interval: float = MIN_INTERVAL, timeout: float = 90.0, retries: int = 4):
        self._token = token
        if token:
            _remember(token)
        self.session = session or requests.Session()
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self.last_sent: float | None = None        # when the request that was answered left (after any queueing)
        self.last_received: float | None = None    # when its answer was complete
        self.last_filename = ""                     # the file name Finviz gave that answer, when it gave one
        install_log_redaction()

    def _auth(self) -> str:
        if not self._token:
            self._token = load_token()
        return self._token

    # Pacing is shared between processes through two small files: `.next_request` holds the
    # next free slot, the cooldown and the time they were written; `.pace.lock` makes reading
    # and updating them one step.
    @staticmethod
    def _pace_file():
        return config.home() / ".next_request"

    @staticmethod
    def _lock_file():
        return config.home() / ".pace.lock"

    def _read_shared(self) -> tuple[float, float, float | None] | None:
        """(next free slot, cooldown end, when written) as last saved by any process, or None
        when there is nothing usable: no file yet, or contents that are not finite numbers."""
        try:
            parts = self._pace_file().read_text(encoding="utf-8", errors="replace").split()      # damage reads as text that is not a number
        except FileNotFoundError:
            return None
        except OSError:
            _warn_once("pace-read", "the pacing file cannot be read: request pacing is not shared with other finviz processes")
            return None
        try:
            values = [float(x) for x in parts[:3]]
        except ValueError:
            return None
        if not values or not all(math.isfinite(v) for v in values):       # "nan" parses as a float, and would never compare
            return None
        return values[0], (values[1] if len(values) > 1 else 0.0), (values[2] if len(values) > 2 else None)

    def _write_shared(self, nxt: float, hold: float, now: float) -> None:
        try:
            write_text_atomic(self._pace_file(), f"{nxt:.6f} {hold:.6f} {now:.6f}")       # in one step: never an empty file
        except OSError:
            _warn_once("pace-write", "the pacing file cannot be written: request pacing is not shared with other finviz processes")

    @staticmethod
    def _believable(nxt: float, hold: float, written: float | None, now: float) -> tuple[float, float]:
        """Deadlines as far as they can be trusted. A long queue is real and is kept: what is
        dropped is state stamped in the future (the clock went back) and deadlines further
        past their own stamp than any queue or cooldown reaches (damage)."""
        written = now if written is None else written            # an older file carries no stamp: judge it from now
        if written > now + CLOCK_SLACK:
            return 0.0, 0.0
        return (nxt if nxt - written <= MAX_QUEUE else 0.0), (hold if hold - written <= MAX_HOLD else 0.0)

    def _state(self, now: float) -> tuple[float, float]:
        """This process's view merged with the shared one. Call with both locks held."""
        nxt, hold = self._believable(Client._next, Client._hold_until, Client._stamp, now)
        shared = self._read_shared()
        if shared:
            s_nxt, s_hold = self._believable(*shared, now)
            nxt, hold = max(nxt, s_nxt), max(hold, s_hold)
        return nxt, hold

    def _publish(self, nxt: float, hold: float, now: float) -> None:
        Client._next, Client._hold_until, Client._stamp = nxt, hold, now
        self._write_shared(nxt, hold, now)

    @contextlib.contextmanager
    def _locked(self):
        """Both locks: this process's threads, then the other processes on this machine."""
        with Client._lock:
            lock = FileLock(self._lock_file(), LOCK_TIMEOUT)
            try:
                lock.__enter__()
            except Busy:
                raise FinvizError(BUSY) from None
            except Unavailable:
                # Nowhere to keep a lock. The request still goes out, paced inside this process
                # only, and says so: other finviz processes cannot be queued behind.
                _warn_once("pace-lock", "the data folder is not writable: request pacing is not shared with other finviz processes")
                yield
                return
            try:
                yield
            finally:
                lock.__exit__(None, None, None)

    def _reserve(self) -> tuple[float, float]:
        """Take the next free request slot on this machine. Returns (the time the slot starts,
        the time the reservation was finished: publishing it can itself take a moment)."""
        with self._locked():
            now = time.time()
            nxt, hold = self._state(now)
            slot = max(nxt, hold, now)
            self._publish(slot + self.min_interval, hold, now)
            return slot, time.time()

    def _cooldown(self) -> float:
        with self._locked():
            return self._state(time.time())[1]

    def _wait(self) -> None:
        """Sleep until this request's own slot. The slot is reserved first, so two processes
        can never wake for the same one. Afterwards the slot is given up and a new one taken
        if a cooldown was published meanwhile, or if this process woke so late that the slot
        is no longer its own (a suspended machine): spacing is kept either way.

        One thing is not closed: another process can publish a cooldown in the instant between
        this check and the request leaving. Closing it would mean holding the lock for the
        whole request, queueing every process behind the slowest one. That request draws one
        more 429 at worst, which is handled like any other.
        """
        for attempt in range(MAX_RESERVATIONS):
            slot, ready = self._reserve()
            delay = slot - time.time()
            if delay > 0:
                time.sleep(delay)
            # Late is measured from when this process could first have gone: the slot, or the
            # end of the reservation if a slow disk made that later. Slow storage is not lateness.
            late = time.time() - max(slot, ready)
            if late > max(self.min_interval, 0.05) and attempt < MAX_RESERVATIONS - 1:
                continue
            if self._cooldown() <= slot + 1e-6:      # no cooldown was published after this slot was taken
                return
        # Out of tries (cooldown after cooldown from other processes): go, and let a 429 say no.

    def _hold(self, seconds: float) -> None:
        """Push everyone's next request back, as after a 429. Never shortens a longer cooldown."""
        with self._locked():
            now = time.time()
            nxt, hold = self._state(now)
            hold = max(hold, now + seconds)
            self._publish(max(nxt, hold), hold, now)

    def export(self, path: str, params: dict | None = None) -> str:
        """GET /export/<path> and return the CSV body. `params` must not include the token."""
        self.last_sent = self.last_received = None        # first of all: they describe this call, or nothing
        self.last_filename = ""
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        if any(k.lower() == "auth" for k in clean):
            raise FinvizError("pass the token through the environment, not as a parameter")
        name = f"/export/{path.lstrip('/')}"
        token = self._auth()
        last = "no attempt made"
        for attempt in range(self.retries):
            final = attempt == self.retries - 1
            self._wait()
            failure = None
            r = None
            sent = time.time()
            try:
                r = self.session.get(BASE + path.lstrip("/"), params={**clean, "auth": token}, timeout=self.timeout)
            except requests.RequestException as exc:      # its text carries the URL: keep only the type
                failure = f"request failed ({type(exc).__name__})"
            except Exception as exc:
                failure = f"request could not be made ({type(exc).__name__})"
                final = True
            # Nothing below runs inside the except block, so an interrupt during a pause
            # cannot chain the original exception (and its URL) into a traceback.
            if failure:
                last = failure
                if final:
                    break
                time.sleep(2 * (attempt + 1) + random.uniform(0, 0.25))
                continue
            if r.status_code == 429:
                try:
                    pause = float(r.headers.get("Retry-After", 5))
                except (TypeError, ValueError):
                    pause = 5.0
                if not math.isfinite(pause):              # "nan" and "inf" are floats too
                    pause = 5.0
                last = "rate limited (HTTP 429)"
                # Published even when this was the last try: the next command, here or in
                # another process, must still sit out the cooldown.
                self._hold(min(max(pause, 1.0), 30.0) + random.uniform(0, 0.25))
                if final:
                    break
                continue
            if r.status_code in (500, 502, 503, 504):
                last = f"Finviz error (HTTP {r.status_code})"
                if final:
                    break
                time.sleep(3 * (attempt + 1) + random.uniform(0, 0.25))
                continue
            if r.status_code in (401, 403):
                raise FinvizError(f"Finviz refused the token (HTTP {r.status_code})")
            if r.status_code != 200:
                reason = self._reason(r, name, token)
                raise FinvizError(f"Finviz answered HTTP {r.status_code} for {name}" + (f": {reason}" if reason else ""))
            received = time.time()
            body = self._checked_body(r, name, token)
            self.last_sent, self.last_received = sent, received       # only once the answer is known to be data
            self.last_filename = self._filename(r, token)
            return body
        raise FinvizError(f"gave up on {name} after {self.retries} tries: {last}")

    @staticmethod
    def _reason(r, name: str, token: str) -> str:
        """Finviz's own few words for a refusal ("Ticker not found"), when they are safe to
        repeat: one short line of plain words, which therefore cannot be a page, a query
        string or the request coming back. Anything else is left out."""
        try:
            text = str(r.text or "").lstrip(chr(0xFEFF)).strip()
        except Exception:
            return ""
        if not _PLAIN_WORDS.fullmatch(text) or _echoes(text, token, name.removeprefix("/export/")):
            return ""
        return text if redact(text) == text else ""

    @staticmethod
    def _filename(r, token: str) -> str:
        """The name Finviz gave the file, from Content-Disposition. Some exports say things
        only there (a statement's currency). A plain name or nothing."""
        header = str((getattr(r, "headers", None) or {}).get("Content-Disposition", ""))
        found = _FILE_NAME.search(header)
        name = (found.group(1) or found.group(2)) if found else ""
        return name if name and token not in name and redact(name) == name else ""

    @staticmethod
    def _checked_body(r, name: str, token: str) -> str:
        """The body of a 200 answer, or an error when it is not data that is safe to hand on."""
        body = r.text.lstrip(chr(0xFEFF))             # a byte-order mark
        if not body.strip():
            raise FinvizError(f"{name} returned nothing")
        kind = str((getattr(r, "headers", None) or {}).get("Content-Type", "")).lower()
        # No export starts with markup: a page, a comment before a page, or XML is an error answer.
        if "html" in kind or body.lstrip()[:1] == "<":
            raise FinvizError(f"{name} returned a web page, not CSV: the token was rejected or the parameters are not valid")
        # An answer that repeats the request (an error text, a diagnostic) would carry the token
        # to stdout or into a saved file. It is never data, however the URL or token is encoded.
        if _echoes(body, token, name.removeprefix("/export/")):
            raise FinvizError(f"{name} returned text that repeats the request's token; it was withheld")
        return body
