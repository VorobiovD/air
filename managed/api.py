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

# The GA dialect (what the Python SDK sends). The research-preview dialect
# accepts a `multiagent` roster on agent CREATE but silently drops it on
# UPDATE — and its GET never renders the field, so the drop is invisible
# (verified 2026-06-11 against api.anthropic.com: RP update → roster gone,
# GA update → roster persists; RP GET shows null either way). Any request
# that carries `multiagent` must use this header.
GA_BETA = "managed-agents-2026-04-01"


def get_headers(*, ga: bool = False) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    headers = {**HEADERS, "x-api-key": key}
    if ga:
        headers["anthropic-beta"] = GA_BETA
    return headers


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

    CANONICAL CURSOR CONTRACT (all managed-agents list endpoints: agents,
    environments, session events, memory stores, memories): pages carry an
    opaque `next_page` token, consumed as the `page` request param. There
    is NO `has_more` / `last_id` / `starting_after` surface — probing those
    silently single-pages the walk (that exact bug shipped independently in
    three places before this note). Client-side mirrors of this contract:
    `memory_store._paginate` (SDK, sync) and
    `session_runner._list_events_paged` (SDK, async).
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
