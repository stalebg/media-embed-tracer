"""Storage modes of scripts/branches.sh, run against local bare repositories (no network)."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "branches.sh"

pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("bash")), reason="needs git and bash")

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}
MODE_VARS = ("DATA_REPO", "DATA_REPO_TOKEN", "PUBLIC_DATA_BRANCH", "GITHUB_ACTIONS", "GIT_CONFIG_COUNT",
             "GITHUB_TOKEN", "SPREADSHEET_URL", "GOOGLE_CREDENTIALS_JSON", "GITHUB_OUTPUT")
SHEETS = {"SPREADSHEET_URL": "set", "GOOGLE_CREDENTIALS_JSON": "set"}


def git(*args: str, cwd: Path) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def branches(bare: Path) -> set[str]:
    out = git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=bare)
    return set(out.split())


@pytest.fixture
def repos(tmp_path):
    """A 'public' origin with one code commit, an empty 'private' store, and a clone of origin."""
    origin, private = tmp_path / "origin.git", tmp_path / "private.git"
    for bare in (origin, private):
        git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("init", "-q", "-b", "main", str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("code\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "code", cwd=seed)
    git("push", "-q", origin.as_uri(), "main", cwd=seed)

    def clone(name: str) -> Path:
        work = tmp_path / name
        git("clone", "-q", origin.as_uri(), str(work), cwd=tmp_path)
        return work

    return origin, private, clone


def run(work: Path, *args: str, **mode: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in MODE_VARS} | GIT_ENV | mode
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=work, env=env, capture_output=True, text=True)


def full_run(work: Path, payload: str, **mode: str) -> str:
    out = ""
    r = run(work, "checkout", **mode)
    assert r.returncode == 0, r.stdout + r.stderr
    out += r.stdout
    (work / "data-branch" / "international").mkdir(parents=True, exist_ok=True)
    with open(work / "data-branch" / "international" / "rows.jsonl", "a") as fh:
        fh.write(payload + "\n")
    (work / "state-branch" / "seen.jsonl").write_text(payload + "\n")
    for cmd in ("commit-data", "commit-state"):
        r = run(work, cmd, "test", **mode)
        assert r.returncode == 0, r.stdout + r.stderr
        out += r.stdout + r.stderr
    return out


def test_neither_mode_fails_closed_and_touches_nothing(repos):
    origin, private, clone = repos
    work = clone("w")
    for cmd in ("mode", "checkout", "commit-data", "commit-state"):
        r = run(work, cmd)
        assert r.returncode == 1
        assert "::error::" in r.stdout and "DATA_REPO" in r.stdout and "PUBLIC_DATA_BRANCH=true" in r.stdout
        assert "SPREADSHEET_URL" in r.stdout and "GOOGLE_CREDENTIALS_JSON" in r.stdout
    assert not (work / "data-branch").exists() and not (work / "state-branch").exists()
    assert branches(origin) == {"main"} and branches(private) == set()


@pytest.mark.parametrize("value", ["false", "1", "TRUE"])
def test_public_mode_needs_exactly_true(repos, value):
    _, _, clone = repos
    assert run(clone("w"), "mode", PUBLIC_DATA_BRANCH=value).returncode == 1


def test_token_without_repo_refuses_public_fallback(repos):
    origin, _, clone = repos
    r = run(clone("w"), "checkout", DATA_REPO_TOKEN="tok", PUBLIC_DATA_BRANCH="true")
    assert r.returncode == 1 and "::error::" in r.stdout
    assert branches(origin) == {"main"}


def test_github_repo_without_token_fails(repos):
    _, _, clone = repos
    r = run(clone("w"), "mode", DATA_REPO="owner/embed-data")
    assert r.returncode == 1 and "DATA_REPO_TOKEN" in r.stdout


@pytest.mark.parametrize("bad", ["not a repo", "https://x-access-token:tok@github.com/owner/embed-data.git"])
def test_bad_data_repo_values_fail(repos, bad):
    _, _, clone = repos
    r = run(clone("w"), "mode", DATA_REPO=bad, DATA_REPO_TOKEN="tok")
    assert r.returncode == 1 and "::error::" in r.stdout
    assert "tok@" not in r.stdout


def test_private_mode_pushes_only_to_store_and_survives_reruns(repos):
    origin, private, clone = repos
    mode = {"DATA_REPO": private.as_uri()}
    out = full_run(clone("run1"), "first", **mode)
    assert "new orphan branch data on store" in out   # empty repository, no branches yet
    assert branches(private) == {"data", "state"}
    assert branches(origin) == {"main"}               # nothing published in the code repo

    work = clone("run2")
    full_run(work, "second", **mode)
    data_log = git("log", "--format=%s", "data", cwd=private).splitlines()
    assert len(data_log) == 2                         # fast-forward history kept
    assert len(git("log", "--format=%s", "state", cwd=private).splitlines()) == 1   # state: one commit
    rows = git("show", "data:international/rows.jsonl", cwd=private)
    assert rows == "first\nsecond\n"
    assert git("remote", "get-url", "store", cwd=work).strip() == private.as_uri()
    assert branches(origin) == {"main"}


def test_private_wins_over_public(repos):
    origin, private, clone = repos
    full_run(clone("w"), "x", DATA_REPO=private.as_uri(), PUBLIC_DATA_BRANCH="true")
    assert branches(private) == {"data", "state"} and branches(origin) == {"main"}


def test_public_mode_pushes_to_origin(repos):
    origin, private, clone = repos
    full_run(clone("w"), "x", PUBLIC_DATA_BRANCH="true")
    assert branches(origin) == {"main", "data", "state"} and branches(private) == set()


def test_unreachable_store_fails_instead_of_starting_empty(repos, tmp_path):
    _, _, clone = repos
    work = clone("w")
    r = run(work, "checkout", DATA_REPO=(tmp_path / "missing.git").as_uri())
    assert r.returncode == 1 and "cannot read branch data" in r.stdout
    assert not (work / "data-branch").exists()


def test_token_is_masked_and_never_stored(repos):
    _, private, clone = repos
    token = "github_pat_SECRET123"
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    work = clone("w")
    # file:// ignores HTTP headers, so the whole token path runs locally.
    out = full_run(work, "x", DATA_REPO=private.as_uri(), DATA_REPO_TOKEN=token, GITHUB_ACTIONS="true")
    lines = out.splitlines()
    assert f"::add-mask::{token}" in lines and f"::add-mask::{header}" in lines
    assert not [ln for ln in lines if (token in ln or header in ln) and not ln.startswith("::add-mask::")]
    config = (work / ".git" / "config").read_text()
    assert token not in config and header not in config
    assert branches(private) == {"data", "state"}
    # Outside Actions nothing about the token is printed at all.
    r = run(work, "mode", DATA_REPO=private.as_uri(), DATA_REPO_TOKEN=token)
    assert r.returncode == 0 and token not in r.stdout + r.stderr and header not in r.stdout


def test_github_store_url_and_header_scope(repos):
    """The token path for a real GitHub repository: remote URL and header key, without network."""
    _, _, clone = repos
    work = clone("w")
    r = run(work, "mode", DATA_REPO="owner/embed-data", DATA_REPO_TOKEN="tok")
    assert r.returncode == 0 and "storage mode: private (remote store)" in r.stdout
    assert git("remote", "get-url", "store", cwd=work).strip() == "https://github.com/owner/embed-data.git"
    assert git("remote", "get-url", "origin", cwd=work).strip().endswith("origin.git")
    script = SCRIPT.read_text()
    assert 'GIT_CONFIG_KEY_1="http.$url.extraheader"' in script and 'use_token "$STORE_URL"' in script
    assert 'GIT_CONFIG_VALUE_0=""' in script   # resets any other header for github.com


def test_public_mode_authenticates_origin_with_github_token(repos):
    origin, _, clone = repos
    token = "ghs_PUBLICTOKEN42"
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    work = clone("w")
    out = full_run(work, "x", PUBLIC_DATA_BRANCH="true", GITHUB_TOKEN=token, GITHUB_ACTIONS="true")
    lines = out.splitlines()
    assert f"::add-mask::{token}" in lines and f"::add-mask::{header}" in lines
    assert not [ln for ln in lines if (token in ln or header in ln) and not ln.startswith("::add-mask::")]
    assert token not in (work / ".git" / "config").read_text()
    assert branches(origin) == {"main", "data", "state"}


def test_public_mode_on_actions_without_token_fails_for_https_origin(repos):
    _, _, clone = repos
    work = clone("w")
    git("remote", "set-url", "origin", "https://github.com/owner/code.git", cwd=work)
    r = run(work, "mode", PUBLIC_DATA_BRANCH="true", GITHUB_ACTIONS="true")
    assert r.returncode == 1 and "GITHUB_TOKEN" in r.stdout


# --- sheets mode --------------------------------------------------------------------

def test_sheets_mode_selected_and_reported_to_the_workflow(repos, tmp_path):
    origin, private, clone = repos
    work = clone("w")
    out_file = tmp_path / "github_output"
    r = run(work, "mode", GITHUB_OUTPUT=str(out_file), **SHEETS)
    assert r.returncode == 0 and "storage mode: sheets" in r.stdout
    assert out_file.read_text() == "mode=sheets\n"
    # If called anyway, the branch commands do nothing: no worktrees, no remote, no push.
    for cmd in ("checkout", "commit-data", "commit-state"):
        r = run(work, cmd, "test", **SHEETS)
        assert r.returncode == 0 and "sheets mode" in r.stdout
    assert not (work / "data-branch").exists() and not (work / "state-branch").exists()
    assert "store" not in git("remote", cwd=work).split()
    assert branches(origin) == {"main"} and branches(private) == set()


@pytest.mark.parametrize(("mode", "expected"), [
    ({"DATA_REPO": "owner/embed-data", "DATA_REPO_TOKEN": "tok"}, "private"),
    ({"PUBLIC_DATA_BRANCH": "true"}, "public"),
])
def test_branch_modes_win_over_sheets(repos, tmp_path, mode, expected):
    _, _, clone = repos
    out_file = tmp_path / "github_output"
    r = run(clone("w"), "mode", GITHUB_OUTPUT=str(out_file), **SHEETS, **mode)
    assert r.returncode == 0 and out_file.read_text() == f"mode={expected}\n"


@pytest.mark.parametrize("half", [{"SPREADSHEET_URL": "set"}, {"GOOGLE_CREDENTIALS_JSON": "set"}])
def test_half_configured_sheet_fails_closed(repos, half):
    _, _, clone = repos
    r = run(clone("w"), "mode", **half)
    assert r.returncode == 1 and "::error::" in r.stdout


def test_token_without_repo_is_refused_even_with_a_sheet(repos):
    _, _, clone = repos
    r = run(clone("w"), "mode", DATA_REPO_TOKEN="tok", **SHEETS)
    assert r.returncode == 1 and "DATA_REPO" in r.stdout


@pytest.mark.parametrize("env", [
    {}, {"DATA_REPO": "owner/x"}, {"PUBLIC_DATA_BRANCH": "true"}, {"PUBLIC_DATA_BRANCH": "TRUE"},
    {"SPREADSHEET_URL": "set"}, {"SPREADSHEET_URL": "set", "GOOGLE_CREDENTIALS_JSON": "set"},
    {"SPREADSHEET_URL": "set", "GOOGLE_CREDENTIALS_JSON": "set", "PUBLIC_DATA_BRANCH": "true"},
    {"SPREADSHEET_URL": "set", "GOOGLE_CREDENTIALS_JSON": "set", "DATA_REPO": "owner/x"},
])
def test_python_and_shell_agree_on_the_mode(repos, tmp_path, env):
    from media_embed_tracer.mode import storage_mode

    _, _, clone = repos
    out_file = tmp_path / "github_output"
    shell_env = dict(env)
    if "DATA_REPO" in env:
        shell_env["DATA_REPO_TOKEN"] = "tok"
    r = run(clone("w"), "mode", GITHUB_OUTPUT=str(out_file), **shell_env)
    shell = out_file.read_text().strip().removeprefix("mode=") if r.returncode == 0 else "none"
    assert shell == storage_mode(env)


def _steps(text: str) -> dict[str, str]:
    out = {}
    for step in text.split("\n      - ")[1:]:
        first = step.splitlines()[0]
        out[first.removeprefix("name: ").strip()] = step
    return out


@pytest.mark.parametrize("wf", ["scrape.yml", "scrape-localized.yml"])
def test_workflows_skip_branch_steps_in_sheets_mode(wf):
    text = (ROOT / ".github" / "workflows" / wf).read_text()
    steps = _steps(text)
    check = steps["Check data store"]
    assert "id: store" in check and "run: scripts/branches.sh mode" in check
    prefix = "LOCALIZED_" if "localized" in wf else ""
    # Only whether the secrets are set reaches the mode check, never their values.
    assert f"SPREADSHEET_URL: ${{{{ secrets.{prefix}SPREADSHEET_URL != '' && 'set' || '' }}}}" in check
    assert "GOOGLE_CREDENTIALS_JSON: ${{ secrets.GOOGLE_CREDENTIALS_JSON != '' && 'set' || '' }}" in check
    checkout = steps["Check out data and state branches"]
    assert "if: steps.store.outputs.mode != 'sheets'" in checkout
    # The commit steps depend on the checkout (skipped => skipped) and on each other.
    assert "if: always() && steps.branches.outcome == 'success'" in steps["Commit data"]
    assert "if: always() && steps.commit_data.outcome == 'success'" in steps["Commit state"]
    # The scraper still gets the sheet (its store in sheets mode), and the mode variables.
    scraper = steps["Run scraper"]
    assert f"SPREADSHEET_URL: ${{{{ secrets.{prefix}SPREADSHEET_URL }}}}" in scraper
    assert "GOOGLE_CREDENTIALS_JSON: ${{ secrets.GOOGLE_CREDENTIALS_JSON }}" in scraper
    assert "DATA_REPO: ${{ vars.DATA_REPO }}" in text and "PUBLIC_DATA_BRANCH: ${{ vars.PUBLIC_DATA_BRANCH }}" in text


@pytest.mark.parametrize(("mode", "ran"), [
    ("sheets", ["Check data store", "Run scraper"]),
    ("private", ["Check data store", "Check out data and state branches", "Run scraper", "Commit data",
                 "Commit state"]),
])
def test_workflow_step_conditions_simulated(mode, ran):
    """Evaluate the three step conditions for one mode, the way Actions would."""
    text = (ROOT / ".github" / "workflows" / "scrape.yml").read_text()
    steps = _steps(text)
    outcome = {}

    def cond(step: str) -> bool:
        body = steps[step]
        if "if: steps.store.outputs.mode != 'sheets'" in body:
            return mode != "sheets"
        if "steps.branches.outcome == 'success'" in body:
            return outcome.get("branches") == "success"
        if "steps.commit_data.outcome == 'success'" in body:
            return outcome.get("commit_data") == "success"
        return True

    done = []
    for name, step_id in (("Check data store", "store"), ("Check out data and state branches", "branches"),
                          ("Run scraper", None), ("Commit data", "commit_data"), ("Commit state", None)):
        if cond(name):
            done.append(name)
            if step_id:
                outcome[step_id] = "success"
        elif step_id:
            outcome[step_id] = "skipped"
    assert done == ran


@pytest.mark.parametrize(("wf", "name"), [("scrape.yml", "international"), ("scrape-localized.yml", "localized")])
def test_workflows_name_their_instance_for_the_sheet_marker(wf, name):
    scraper = _steps((ROOT / ".github" / "workflows" / wf).read_text())["Run scraper"]
    assert f"INSTANCE_NAME: {name}" in scraper
