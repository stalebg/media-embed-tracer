#!/usr/bin/env bash
# Git-scraping branches used by the scrape workflows.
#
#   data  : the dataset. Append-only monthly partitions + posts.jsonl.
#           Normal fast-forward commits only (never amended or force-pushed).
#   state : fast-changing scraper state (seen articles, feed health, resolve queue).
#           Always exactly one commit, force-pushed each run (no history to keep).
#
#   scripts/branches.sh mode                  validate the storage mode (fails closed); writes
#                                             mode=<private|public|sheets> to $GITHUB_OUTPUT
#   scripts/branches.sh checkout              worktrees ./data-branch and ./state-branch
#   scripts/branches.sh commit-data <label>   commit + push data (rebase + retry if behind)
#   scripts/branches.sh commit-state <label>  replace the state branch with one commit
#
# Storage modes (chosen from the environment, which the workflows fill from repository
# variables and secrets):
#
#   private : DATA_REPO=owner/name (+ DATA_REPO_TOKEN). The branches live in that separate
#             (private) repository, reached through the remote "store". Wins if both are set.
#             DATA_REPO may also be a full URL (e.g. file:///tmp/x.git for tests); URLs
#             with embedded credentials are refused.
#   public  : PUBLIC_DATA_BRANCH=true. The branches live in this repository (origin),
#             authenticated with GITHUB_TOKEN (the workflows check out the code with
#             persist-credentials: false, so the scraper step holds no push token).
#   sheets  : neither of the above, but SPREADSHEET_URL and GOOGLE_CREDENTIALS_JSON are set
#             (the workflows pass only whether each is set, not the values). The Google
#             Sheet holds state and data; there are no branches: checkout and commit-* do
#             nothing, and the workflows skip those steps.
#   none    : every command exits 1 with an ::error:: before touching anything.
#
# Tokens are never written to .git/config or the command line: each is passed to git as an
# Authorization header through GIT_CONFIG_* environment variables, scoped to the remote's
# URL, and masked in the Actions log.
#
# In these modes both scrape workflows share one concurrency group, so these never run in
# parallel (sheets mode: one group per workflow).
set -euo pipefail

DATA_BRANCH="${DATA_BRANCH:-data}"
STATE_BRANCH="${STATE_BRANCH:-state}"
STORE_REMOTE="store"

die() {
  echo "::error::$*"
  exit 1
}

# use_token <url> <token>: send the token as an Authorization header to <url> only.
use_token() {
  local url="$1" token="$2" header
  header="$(printf 'x-access-token:%s' "$token" | base64 | tr -d '\n')"
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    echo "::add-mask::$token"
    echo "::add-mask::$header"
  fi
  # The empty value first resets any Authorization header configured elsewhere (e.g. by
  # actions/checkout for github.com), so only this token is sent.
  export GIT_CONFIG_COUNT=2
  export GIT_CONFIG_KEY_0="http.$url.extraheader" GIT_CONFIG_VALUE_0=""
  export GIT_CONFIG_KEY_1="http.$url.extraheader" GIT_CONFIG_VALUE_1="AUTHORIZATION: basic $header"
}

# Sets MODE, REMOTE and (private mode) STORE_URL. Exits 1 when no mode is configured.
# Order: DATA_REPO, then PUBLIC_DATA_BRANCH=true, then the spreadsheet (as in mode.py).
resolve_mode() {
  local data_repo="${DATA_REPO:-}" token="${DATA_REPO_TOKEN:-}"
  if [[ -n "$data_repo" ]]; then
    MODE=private
    REMOTE="$STORE_REMOTE"
    case "$data_repo" in
      *://*@*) die "DATA_REPO must not contain credentials; put the token in the DATA_REPO_TOKEN secret." ;;
      *://*) STORE_URL="$data_repo" ;;
      *)
        [[ "$data_repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
          || die "DATA_REPO must look like owner/name (e.g. owner/embed-data)."
        STORE_URL="https://github.com/$data_repo.git"
        ;;
    esac
    if [[ -n "$token" ]]; then
      use_token "$STORE_URL" "$token"
    elif [[ "$STORE_URL" == http* ]]; then
      die "DATA_REPO is set but DATA_REPO_TOKEN is empty. Add a fine-grained token with Contents: read and write on $data_repo."
    fi
  elif [[ -n "$token" ]]; then
    die "DATA_REPO_TOKEN is set but DATA_REPO is empty. Set DATA_REPO=owner/name."
  elif [[ "${PUBLIC_DATA_BRANCH:-}" == "true" ]]; then
    MODE=public
    REMOTE=origin
    local origin_url
    origin_url="$(git remote get-url origin)"
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
      use_token "$origin_url" "$GITHUB_TOKEN"
    elif [[ "${GITHUB_ACTIONS:-}" == "true" && "$origin_url" == http* ]]; then
      die "public-branch mode needs GITHUB_TOKEN in the branch steps (checkout uses persist-credentials: false)."
    fi
  elif [[ -n "${SPREADSHEET_URL:-}" && -n "${GOOGLE_CREDENTIALS_JSON:-}" ]]; then
    MODE=sheets
    REMOTE=""
  else
    die "No data store configured; nothing was scraped. Set SPREADSHEET_URL + GOOGLE_CREDENTIALS_JSON (sheets), DATA_REPO + DATA_REPO_TOKEN (private repo), or PUBLIC_DATA_BRANCH=true."
  fi
}

ensure_remote() {
  [[ "$MODE" == private ]] || return 0
  if git remote get-url "$STORE_REMOTE" >/dev/null 2>&1; then
    git remote set-url "$STORE_REMOTE" "$STORE_URL"
  else
    git remote add "$STORE_REMOTE" "$STORE_URL"
  fi
}

identity() {
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
}

checkout_branch() {
  local branch="$1" dir="$2" rc=0
  git ls-remote --exit-code --heads "$REMOTE" "$branch" >/dev/null 2>&1 || rc=$?
  if [[ $rc -eq 0 ]]; then
    git fetch -q --depth=1 "$REMOTE" "+refs/heads/$branch:refs/remotes/$REMOTE/$branch"
    git worktree add -q -B "$branch" "$dir" "$REMOTE/$branch"
  elif [[ $rc -eq 2 ]]; then
    # Branch does not exist yet (also: a brand-new empty repository).
    git worktree add -q --detach "$dir"
    (cd "$dir" && git checkout -q --orphan "$branch" && git rm -rq --cached --ignore-unmatch . && git clean -fdxq)
    echo "new orphan branch $branch on $REMOTE"
  else
    # Unreachable or unauthorised: never start from empty state (it could re-quote posts).
    die "cannot read branch $branch from remote $REMOTE (git ls-remote exit $rc). Check DATA_REPO and DATA_REPO_TOKEN."
  fi
}

cmd="${1:-}"
case "$cmd" in
  mode|checkout|commit-data|commit-state) resolve_mode; ensure_remote ;;
esac

if [[ "${MODE:-}" == sheets && "$cmd" != mode ]]; then
  echo "sheets mode: no branches, nothing to $cmd"
  exit 0
fi

case "$cmd" in
  mode)
    if [[ "$MODE" == sheets ]]; then
      echo "storage mode: sheets (state and data in the Google Sheet; no branches)"
    else
      echo "storage mode: $MODE (remote $REMOTE)"
    fi
    if [[ -n "${GITHUB_OUTPUT:-}" ]]; then echo "mode=$MODE" >> "$GITHUB_OUTPUT"; fi
    ;;
  checkout)
    checkout_branch "$DATA_BRANCH" data-branch
    checkout_branch "$STATE_BRANCH" state-branch
    ;;
  commit-data)
    cd data-branch
    identity
    git add -A
    if git diff --cached --quiet; then echo "no data changes"; exit 0; fi
    git commit -q -m "data: ${2:-update} $(date -u +%FT%TZ)"
    for attempt in 1 2 3; do
      if git push -q "$REMOTE" "HEAD:$DATA_BRANCH"; then echo "data pushed to $REMOTE"; exit 0; fi
      echo "push rejected (attempt $attempt); rebasing" >&2
      git pull -q --rebase "$REMOTE" "$DATA_BRANCH"
    done
    exit 1
    ;;
  commit-state)
    cd state-branch
    identity
    git checkout -q --orphan "state-tmp-$$"
    git add -A
    git commit -q --allow-empty -m "state: ${2:-update} $(date -u +%FT%TZ)"
    git push -q --force "$REMOTE" "HEAD:$STATE_BRANCH"
    echo "state pushed to $REMOTE"
    ;;
  *)
    echo "usage: $0 mode | checkout | commit-data <label> | commit-state <label>" >&2
    exit 2
    ;;
esac
