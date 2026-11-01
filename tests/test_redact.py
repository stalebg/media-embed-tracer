"""Log redaction for public Actions logs (private-store mode)."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from media_embed_tracer import redact
from media_embed_tracer.redact import RedactingFormatter, RedactingStream, private

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "media_embed_tracer"

URL = "https://news.example.com/2026/10/story-about-something?id=7"
AT = "at://did:plc:abc123xyz/app.bsky.feed.post/3kq2abcd"
DID = "did:plc:zz9fooqux"
TITLE = "Minister quits after leaked chat"
SECRETS = (URL, AT, DID, TITLE, "alice.bsky.social", "@bob.example.org", "news.example.com")


def record(msg: str, *args) -> logging.LogRecord:
    return logging.LogRecord("media_embed_tracer.pipeline", logging.WARNING, __file__, 1, msg, args, None)


def fmt(redacting: bool, rec: logging.LogRecord) -> str:
    return RedactingFormatter("%(levelname)s %(name)s: %(message)s", redact=redacting).format(rec)


def sample() -> logging.LogRecord:
    return record("fetch failed %s (%s) quoting %s by %s, title %s; handles %s %s; feed %s: %d HTTP %d",
                  private(URL), URL, AT, DID, private(TITLE), "alice.bsky.social", "@bob.example.org",
                  "Example Feed", 3, 503)


def test_private_mode_redacts_urls_uris_dids_handles_and_titles():
    out = fmt(True, sample())
    for secret in SECRETS:
        assert secret not in out, secret
    # Allowed: feed names, counts, status codes, platform/level/logger names.
    assert "Example Feed" in out and ": 3 HTTP 503" in out and "WARNING media_embed_tracer.pipeline" in out


def test_public_mode_leaves_records_unchanged():
    out = fmt(False, sample())
    for secret in SECRETS[:-1]:
        assert secret in out, secret


def test_traceback_and_mapping_args_are_redacted():
    try:
        raise ValueError(f"bad response from {URL}")
    except ValueError:
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "%(t)s failed", ({"t": private(TITLE)},),
                                sys.exc_info())
    out = fmt(True, rec)
    assert URL not in out and TITLE not in out and "ValueError" in out


def test_stream_wrapper_catches_prints_and_annotations():
    buf = io.StringIO()
    stream = RedactingStream(buf)
    print(f"::warning::new post {AT} in {URL} by {DID}", file=stream)
    assert buf.getvalue().startswith("::warning::new post <at-uri> in <url> by <did>")


def test_requests_error_text_is_redacted():
    err = ("ConnectionError: HTTPSConnectionPool(host='news.example.com', port=443): Max retries exceeded "
           "with url: /2026/10/story-about-something (Caused by NameResolutionError)")
    out = redact.redact_text(err)
    assert "news.example.com" not in out and "story-about-something" not in out
    assert out.startswith("ConnectionError: HTTPSConnectionPool(host='<host>'") and "NameResolutionError" in out


def test_exact_urllib3_retry_warning_is_redacted():
    line = ("Retrying (Retry(total=1, connect=None, read=None, redirect=None, status=None)) after connection "
            "broken by 'ReadTimeoutError(\"HTTPSConnectionPool(host='nrk.no', port=443): Read timed out. "
            "(read timeout=20)\")': /nyheter/hemmelig-sak-123")
    rec = logging.LogRecord("urllib3.connectionpool", logging.WARNING, __file__, 1,
                            "Retrying (%r) after connection broken by '%r': %s",
                            ("Retry(total=1, connect=None, read=None, redirect=None, status=None)",
                             'ReadTimeoutError("HTTPSConnectionPool(host=\'nrk.no\', port=443): Read timed out.")',
                             "/nyheter/hemmelig-sak-123"), None)
    for out in (redact.redact_text(line), fmt(True, rec)):
        assert "hemmelig-sak-123" not in out and "nrk.no" not in out, out
        assert "Retrying" in out and "ReadTimeoutError" in out


@pytest.mark.parametrize("text", [
    "GET /xrpc/com.atproto.identity.resolveHandle?handle=journalist.no HTTP/1.1",
    "app.bsky.actor.getProfile?actor=reporter.example.org",
    "MissingSchema: Invalid URL 'nrk.no/sak/1': No scheme supplied",
    "quoted by @journalist.no today",
])
def test_custom_domain_handles_and_schemeless_urls(text):
    out = redact.redact_text(text)
    for secret in ("journalist.no", "reporter.example.org", "nrk.no/sak/1", "sak/1"):
        assert secret not in out, out


@pytest.mark.parametrize(("error", "shown"), [
    ("MissingSchema: Invalid URL 'nrk.no/sak/1': No scheme supplied", "MissingSchema"),
    ("HTTP 404", "HTTP 404"),
    ("not HTML: application/pdf", "not HTML"),
    ("parse error: mismatched tag near 'Secret title'", "parse error"),
    ("parse error: ValueError", "parse error: ValueError"),
    ("Minister quits after leaked chat", "error"),
])
def test_error_texts_keep_only_class_or_label(error, shown):
    out = fmt(True, record("fetch failed (status %s): %s", 404, redact.private_error(error)))
    assert out.endswith(f"(status 404): {shown}"), out
    assert fmt(False, record("%s", redact.private_error(error))).endswith(error)


def test_sheets_mirror_error_shows_only_class_name():
    class APIError(Exception):
        pass

    problem = redact.private_error(APIError("cell value 'Minister quits' rejected"), "mirror sheets: ")
    assert fmt(True, record("problem: %s", problem)).endswith("problem: mirror sheets: APIError")
    assert "Minister quits" in fmt(False, record("problem: %s", problem))
    # Exception objects passed straight to the logger are reduced to their class too.
    assert fmt(True, record("store save failed: %s", APIError("title X"))).endswith("store save failed: APIError")


def test_install_silences_urllib3_retries_only_in_redact_mode():
    urllib3_log = logging.getLogger("urllib3")
    old_level, old_handlers = urllib3_log.level, logging.getLogger().handlers[:]
    old_streams = sys.stdout, sys.stderr
    try:
        urllib3_log.setLevel(logging.NOTSET)
        redact.install({})
        assert urllib3_log.level == logging.NOTSET
        redact.install({"LOG_REDACT": "true"})
        assert urllib3_log.level == logging.ERROR
    finally:
        sys.stdout, sys.stderr = old_streams
        urllib3_log.setLevel(old_level)
        logging.getLogger().handlers[:] = old_handlers


@pytest.mark.parametrize(("env", "expected"), [
    ({}, False), ({"LOG_REDACT": "true"}, True), ({"LOG_REDACT": "false"}, False),
    ({"DATA_REPO": "owner/embed-data"}, True), ({"PUBLIC_DATA_BRANCH": "true"}, False),
])
def test_enabled(env, expected):
    assert redact.enabled(env) is expected


def test_cli_process_output_is_redacted_end_to_end(tmp_path):
    """A real subprocess in redact mode: logs, print() and an uncaught traceback are all scrubbed."""
    code = (
        "import logging, os, sys\n"
        "from media_embed_tracer import redact\n"
        "redact.install(os.environ)\n"
        f"logging.getLogger('t').warning('new post in %s', {URL!r})\n"
        f"print('found', {AT!r})\n"
        f"raise RuntimeError('boom ' + {DID!r})\n"
    )
    env = {"PATH": "/usr/bin:/bin", "LOG_REDACT": "true"}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=tmp_path)
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "RuntimeError" in out and "new post in" in out
    for secret in (URL, AT, DID):
        assert secret not in out
    env.pop("LOG_REDACT")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert URL in r.stdout and AT in r.stdout


def test_nothing_writes_the_public_step_summary_or_logs_titles():
    workflows = "".join(p.read_text() for p in (ROOT / ".github" / "workflows").glob("*.yml"))
    sources = {p: p.read_text() for p in SRC.rglob("*.py")}
    assert "GITHUB_STEP_SUMMARY" not in workflows and not any("GITHUB_STEP_SUMMARY" in t for t in sources.values())
    for path, text in sources.items():
        for call in __import__("re").findall(r"log\.\w+\((.*)", text):
            assert "title" not in call, (path, call)


REDACT_EXPR = "LOG_REDACT: ${{ (vars.DATA_REPO != '' || vars.PUBLIC_DATA_BRANCH != 'true') && 'true' || 'false' }}"


@pytest.mark.parametrize(("data_repo", "public", "expected"), [
    ("owner/embed-data", "", True),       # private store
    ("owner/embed-data", "true", True),   # private wins
    ("", "true", False),                  # public branches: the data is public anyway
    ("", "", True),                       # sheets mode (or nothing configured)
    ("", "false", True),
])
def test_scrape_workflows_turn_redaction_on_unless_public_branch_mode(data_repo, public, expected):
    for wf in ("scrape.yml", "scrape-localized.yml"):
        text = (ROOT / ".github" / "workflows" / wf).read_text()
        assert REDACT_EXPR in text
    # The same expression, evaluated the way Actions does for these variable values.
    assert (((data_repo != "" or public != "true") and "true") or "false") == ("true" if expected else "false")


def test_redaction_on_in_sheets_mode():
    sheets = {"SPREADSHEET_URL": "sheet-id-placeholder", "GOOGLE_CREDENTIALS_JSON": "{}"}
    assert redact.enabled(sheets)
    assert redact.enabled({**sheets, "LOG_REDACT": "false"})        # cannot be switched off in sheets mode
    assert not redact.enabled({**sheets, "PUBLIC_DATA_BRANCH": "true"})   # public mode: sheet is a mirror
    assert redact.enabled({**sheets, "DATA_REPO": "owner/embed-data"})
    assert not redact.enabled({"SPREADSHEET_URL": "sheet-id-placeholder"})  # half-configured: no sheets mode
