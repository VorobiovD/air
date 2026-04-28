"""Shared API helpers for the air Managed Agent."""

import os
import sys

import requests

API_BASE = "https://api.anthropic.com/v1"
# Research-preview header unlocks `callable_agents` (multi-agent), Memory,
# and Outcomes. Phase 1 only uses callable_agents — the air-coordinator
# agent dispatches the 4 specialists + verifier as sub-agents in one
# session instead of the prior 5 separate sessions. Memory and Outcomes
# remain unused but share this header. Anthropic's email (2026-04-25)
# targets a stable May release with breaking changes expected — re-pin
# after stable lands.
HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "managed-agents-2026-04-01-research-preview",
    "content-type": "application/json",
}


def get_headers() -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    return {**HEADERS, "x-api-key": key}


def api_error_message(resp: requests.Response) -> str:
    """Extract error message from API response, handling non-JSON responses."""
    try:
        return resp.json().get("error", {}).get("message", resp.text[:200])
    except (ValueError, KeyError):
        return resp.text[:200]


def _paginate(path: str) -> list[dict]:
    """Fetch all pages of a list endpoint. The /agents and /environments
    endpoints default to 20 items per page and signal more via `next_page`
    (no `has_more` field). Accounts with >20 total agents silently drop
    matches without pagination — e.g. review.py misses `air-simplify` on
    the second page and aborts with a spurious "Missing agents" error.
    """
    all_items: list[dict] = []
    cursor: str | None = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["page"] = cursor
        resp = requests.get(f"{API_BASE}{path}", headers=get_headers(), params=params)
        if not resp.ok:
            break
        body = resp.json()
        all_items.extend(body.get("data", []))
        cursor = body.get("next_page")
        if not cursor:
            break
    return all_items


def list_agents() -> dict[str, dict]:
    """Fetch all agents across pages, return as {name: agent} dict.

    API returns newest first per page. We iterate oldest→newest so dict
    overwrites keep the newest non-archived agent per name.
    """
    all_agents = _paginate("/agents")
    result = {}
    for agent in reversed(all_agents):
        if not agent.get("archived_at"):
            result[agent["name"]] = agent
    return result


def find_environment() -> str | None:
    """Find existing environment by name."""
    for env in _paginate("/environments"):
        if env["name"] == "air-review-env" and not env.get("archived_at"):
            return env["id"]
    return None
