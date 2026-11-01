"""Storage mode, chosen from the environment with the same rules as scripts/branches.sh.

    private : DATA_REPO set                 -> JSONL on branches of a separate repository
    public  : PUBLIC_DATA_BRANCH=true       -> JSONL on branches of this repository
    sheets  : SPREADSHEET_URL and GOOGLE_CREDENTIALS_JSON set -> the Google Sheet is the store
    none    : nothing configured            -> local JSONL (branches.sh fails closed in CI)

In private and public mode a configured spreadsheet is only a mirror.
"""

from __future__ import annotations

from collections.abc import Mapping

PRIVATE, PUBLIC, SHEETS, NONE = "private", "public", "sheets", "none"


def storage_mode(env: Mapping[str, str]) -> str:
    def val(name: str) -> str:
        return (env.get(name) or "").strip()

    if val("DATA_REPO"):
        return PRIVATE
    if env.get("PUBLIC_DATA_BRANCH") == "true":  # exactly "true", as in branches.sh
        return PUBLIC
    if val("SPREADSHEET_URL") and val("GOOGLE_CREDENTIALS_JSON"):
        return SHEETS
    return NONE
