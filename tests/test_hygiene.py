"""Repository hygiene and workflow invariants."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "media_embed_tracer"
WORKFLOWS = ROOT / ".github" / "workflows"


def test_license_is_mit_with_contributors_line():
    text = (ROOT / "LICENSE").read_text()
    assert text.startswith("MIT License") and "media-embed-tracer contributors" in text


def test_dependencies_pinned_and_no_atproto():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    deps = [*project["dependencies"], *(d for group in project["optional-dependencies"].values() for d in group)]
    assert all(re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][A-Za-z0-9.]*", d) for d in deps), deps
    assert not any(d.startswith(("atproto", "python-dotenv")) for d in deps)


def test_gitignore_does_not_block_json_and_ignores_venv():
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert "*.json" not in lines and ".venv/" in lines


@pytest.mark.parametrize("dead", ["write_embed", "is_duplicate", "fetch_html_cached", "dotenv", "USER_ENTERED",
                                  "Chrome/", "auth/drive"])
def test_no_dead_or_unsafe_code(dead):
    for path in SRC.rglob("*.py"):
        assert dead not in path.read_text(), f"{dead} in {path}"


def workflows():
    return {p.name: p.read_text() for p in WORKFLOWS.glob("*.yml")}


def test_all_workflows_have_concurrency_and_pinned_actions():
    for name, text in workflows().items():
        assert "\nconcurrency:" in text, name
        for ref in re.findall(r"uses:\s*(\S+)", text):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{name}: {ref} not pinned to a SHA"


# v1 file names are kept so the Actions history and enabled state carry over.
SCRAPE_WORKFLOWS = ("scrape.yml", "scrape-localized.yml")


SCRAPE_GROUP = ("group: ${{ (vars.DATA_REPO != '' || vars.PUBLIC_DATA_BRANCH == 'true') && 'scrape-branches' "
                "|| format('scrape-{0}', github.workflow) }}")


def test_scrape_workflows_share_group_in_branch_modes_and_commit_before_anything_else():
    """Branch modes share the branches, so one group across both workflows; otherwise
    (sheets mode) a group per workflow, so one instance never cancels the other's queued run."""
    for wf in SCRAPE_WORKFLOWS:
        text = workflows()[wf]
        assert SCRAPE_GROUP in text and "cancel-in-progress: false" in text
        assert "BLUESKY_HANDLE:" in text and "BLUESKY_APP_PASSWORD:" in text
        assert "export" not in text.split("Run scraper")[1]   # exports are weekly, never per run
        assert text.index("commit-data") < text.index("commit-state")


def test_branch_script_never_amends_data():
    text = (ROOT / "scripts" / "branches.sh").read_text()
    data_part = text.split("commit-data)")[1].split(";;")[0]
    assert "--amend" not in data_part and "--force" not in data_part


def test_keepalive_uses_enable_endpoint_with_actions_write():
    text = workflows()["keepalive.yml"]
    assert "actions: write" in text and "--method PUT" in text and "/enable" in text
    # Works on its own (no reliance on data commits) and covers every scheduled workflow.
    assert "contents: write" not in text and "git push" not in text
    for name, wf in workflows().items():
        if "schedule:" in wf:
            assert name in text, name


def run_blocks(text: str) -> list[str]:
    """Every `run:` script: the inline form plus the indented block of `run: |`."""
    blocks, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("run:"):
            continue
        block, indent = [stripped], len(line) - len(stripped)
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            block.append(nxt)
        blocks.append("\n".join(block))
    return blocks


def test_scrape_workflows_check_storage_mode_before_scraping():
    for wf in SCRAPE_WORKFLOWS:
        text = workflows()[wf]
        assert "DATA_REPO: ${{ vars.DATA_REPO }}" in text
        assert "PUBLIC_DATA_BRANCH: ${{ vars.PUBLIC_DATA_BRANCH }}" in text
        # The fail-closed check is the first step after the code checkout.
        assert text.index("scripts/branches.sh mode") < text.index("pip install")
        assert text.index("scripts/branches.sh mode") < text.index("python -m media_embed_tracer run")
        # Commit steps only run when the branches were checked out (skipped if the check failed).
        assert text.count("steps.branches.outcome == 'success'") == 1
        assert "if: always() && steps.commit_data.outcome == 'success'" in text


def test_data_repo_token_only_reaches_branch_steps():
    for wf in SCRAPE_WORKFLOWS:
        text = workflows()[wf]
        steps = text.split("\n      - ")
        for needle in ("secrets.DATA_REPO_TOKEN", "github.token"):
            with_token = [s for s in steps if needle in s]
            assert len(with_token) == 4, needle
            assert all("scripts/branches.sh" in s for s in with_token)
        scraper = next(s for s in steps if "media_embed_tracer run" in s)
        assert "DATA_REPO_TOKEN" not in scraper and "GITHUB_TOKEN" not in scraper and "github.token" not in scraper
        # No push-capable token persisted in .git/config for the scraper step to use.
        checkout = next(s for s in steps if "actions/checkout@" in s)
        assert "persist-credentials: false" in checkout


def test_secrets_never_interpolated_into_run_scripts():
    for name, text in workflows().items():
        for block in run_blocks(text):
            assert "${{ secrets." not in block and "DATA_REPO_TOKEN" not in block, (name, block)
            assert "set -x" not in block


def test_branch_script_masks_token_and_keeps_it_out_of_git_config():
    text = (ROOT / "scripts" / "branches.sh").read_text()
    assert 'echo "::add-mask::$token"' in text and 'echo "::add-mask::$header"' in text
    assert text.index("::add-mask::") < text.index("GIT_CONFIG_VALUE_1=")
    assert "set -x" not in text and "git config http" not in text and "extraheader" not in text.split("ensure_remote()")[1]
    assert "x-access-token:" not in text.split("printf")[0]   # never placed in a remote URL


def test_export_runs_only_in_public_branch_mode():
    text = workflows()["export.yml"]
    assert "if: vars.PUBLIC_DATA_BRANCH == 'true' && vars.DATA_REPO == ''" in text
    assert "DATA_REPO_TOKEN" not in text


def test_no_deployment_identifiers_in_repo():
    """Only generic placeholders: no real handles, sheet ids or emails in tracked text."""
    pattern = re.compile(r"docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]{30,}|[\w.+-]+@(?!users\.noreply|example)"
                         r"[\w-]+\.(?:com|no|org|net)\b")
    for path in list(ROOT.glob("*.md")) + list(SRC.rglob("*.py")) + list(WORKFLOWS.glob("*.yml")) + list(
            (ROOT / "config").glob("*")):
        assert not pattern.search(path.read_text()), path


def test_readme_documents_every_env_var_status_and_exit_code():
    readme = (ROOT / "README.md").read_text()
    code = (SRC / "config.py").read_text() + (SRC / "cli.py").read_text()
    names = set(re.findall(r'env(?:iron)?\.get\("([A-Z0-9_]+)"', code)) | set(re.findall(r'_num\(env, "([A-Z0-9_]+)"', code))
    assert len(names) > 25
    missing = sorted(n for n in names if f"`{n}`" not in readme)
    assert not missing, missing
    from media_embed_tracer import pipeline, repost
    for status in repost.STATUSES:
        assert f"`{status}`" in readme
    for code_ in (pipeline.EXIT_OK, pipeline.EXIT_CONFIG, pipeline.EXIT_STORE, pipeline.EXIT_LOGIN,
                  pipeline.EXIT_FETCH, pipeline.EXIT_BLUESKY):
        assert f"| {code_} |" in readme


def test_scrape_workflows_cap_run_time_below_schedule_offset():
    for wf in SCRAPE_WORKFLOWS:
        text = workflows()[wf]
        assert 'MAX_RUN_MINUTES: "10"' in text


def test_no_contact_url_or_placeholder_in_source():
    for p in SRC.rglob("*.py"):
        text = p.read_text()
        assert "your-username" not in text and "PROJECT_URL" not in text, p.name
