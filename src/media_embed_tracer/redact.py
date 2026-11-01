"""Log redaction for public Actions logs.

When the dataset lives in a private store (a private repository or the Google Sheet), the
Actions logs of a public repository must not show the findings. In redact mode
(``LOG_REDACT=true``, ``DATA_REPO`` set, or sheets storage mode) every
log line and everything written to stdout/stderr (prints, tracebacks, annotations) has
URLs, ``at://`` URIs, DIDs, handles and e-mail addresses replaced. Values wrapped in
:func:`private` (titles, handles, ids) are replaced whole. Counts, feed names, platform
names, status codes and error classes are left alone. Exceptions that set ``log_safe``
(StoreError(..., safe=True): messages built by this package without findings) are shown in
full; the output is still pattern-scrubbed.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from typing import TextIO

from .mode import PRIVATE, SHEETS, storage_mode

REDACTED = "<redacted>"

_PATTERNS = [
    (re.compile(r"\bat://\S+"), "<at-uri>"),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE), "<url>"),
    (re.compile(r"\bdid:[a-z0-9]+:[A-Za-z0-9._:%-]+"), "<did>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"(?<![\w.@/])@[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]"), "@<handle>"),
    (re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.bsky\.social\b", re.IGNORECASE), "<handle>"),
    (re.compile(r"\bwww\.[\w-]+(?:\.[\w-]+)+/\S*", re.IGNORECASE), "<url>"),
    # urllib3/requests errors: HTTPSConnectionPool(host='x', port=443): ... (Caused by ...) url: /path
    (re.compile(r"host=\\?'[^'\\]*\\?'"), "host='<host>'"),   # also repr-escaped host=\'x\'
    (re.compile(r"\burl: /\S*"), "url: <path>"),
    # urllib3 retry warnings: "Retrying (...) after connection broken by '...': /path"
    (re.compile(r"': /\S*"), "': <path>"),
    # query values that name accounts or links (e.g. resolveHandle?handle=x.example)
    (re.compile(r"\b(handle|actor|uri|url|q|subject)=[^&\s'\"]+", re.IGNORECASE), r"\1=<redacted>"),
    # scheme-less host + path, e.g. 'nrk.no/sak/1'
    (re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}/\S*", re.IGNORECASE), "<url>"),
]

# Error texts that carry no findings and may be shown as they are.
_SAFE_ERROR = re.compile(r"HTTP \d{3}|response too large|deadline exceeded|\d+/\d+ \w+(?: \w+)* failed"
                         r"|parse error: [A-Za-z_]\w*")
_ERROR_CLASS = re.compile(r"([A-Za-z_][\w.]*(?:Error|Exception|Timeout|Warning)|[A-Z]\w+)(?=:|$)")
_ERROR_LABEL = re.compile(r"(not HTML|parse error|could not read own recent posts)\b")


class Private(str):
    """Marks a log argument (title, handle, id, URL) that redact mode replaces entirely."""


def private(value: object) -> Private:
    return Private("" if value is None else value)


class PrivateError(Private):
    """An error text that may echo URLs, titles or cell values. Redact mode shows only
    ``prefix`` plus the error class (or a fixed label such as ``HTTP 404``), unless the
    error was marked safe (``keep``)."""

    prefix: str = ""
    keep: bool = False


def is_safe(error: object) -> bool:
    """True for an exception whose message was built without findings (``log_safe``)."""
    return isinstance(error, BaseException) and getattr(error, "log_safe", False) is True


def private_error(error: object, prefix: str = "") -> PrivateError:
    text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    out = PrivateError(prefix + text)
    out.prefix = prefix
    out.keep = is_safe(error)
    return out


def error_summary(text: str) -> str:
    """The shareable part of an error text: its class name or a fixed label."""
    text = text.strip()
    if _SAFE_ERROR.fullmatch(text):
        return text
    if m := _ERROR_LABEL.match(text):
        return m.group(1)
    if m := _ERROR_CLASS.match(text):
        return m.group(1)
    return "error"


def enabled(env: Mapping[str, str]) -> bool:
    if env.get("LOG_REDACT", "").strip().lower() == "true":
        return True
    return storage_mode(env) in (PRIVATE, SHEETS)


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _scrub(arg: object) -> object:
    if isinstance(arg, PrivateError):
        return str(arg) if arg.keep else arg.prefix + error_summary(arg[len(arg.prefix):])
    if isinstance(arg, Private):
        return REDACTED
    if isinstance(arg, BaseException):
        return str(arg) if is_safe(arg) else type(arg).__name__
    return arg


class RedactingFormatter(logging.Formatter):
    """Formats normally; in redact mode drops Private args and scrubs the whole output."""

    def __init__(self, fmt: str | None = None, *, redact: bool) -> None:
        super().__init__(fmt)
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        if not self.redact:
            return super().format(record)
        record = logging.makeLogRecord(record.__dict__)
        if isinstance(record.args, Mapping):
            record.args = {k: _scrub(v) for k, v in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(_scrub(a) for a in record.args)
        if isinstance(record.msg, Private):
            record.msg = _scrub(record.msg)
        return redact_text(super().format(record))


class RedactingStream:
    """Text stream wrapper that scrubs every write: catches print(), tracebacks, annotations."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        self._stream.write(redact_text(text))
        return len(text)

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def install(env: Mapping[str, str], level: int = logging.INFO) -> bool:
    """Configure root logging (UTC times) and, in redact mode, wrap stdout/stderr."""
    redact = enabled(env)
    if redact:
        if not isinstance(sys.stdout, RedactingStream):
            sys.stdout = RedactingStream(sys.stdout)
        if not isinstance(sys.stderr, RedactingStream):
            sys.stderr = RedactingStream(sys.stderr)
    handler = logging.StreamHandler(sys.stdout)
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", redact=redact)
    formatter.converter = __import__("time").gmtime  # log times in UTC
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.captureWarnings(True)
    if redact:
        # urllib3's retry warnings carry request paths and query strings.
        logging.getLogger("urllib3").setLevel(logging.ERROR)
    return redact
