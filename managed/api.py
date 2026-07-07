"""Shared API helpers for the air Managed Agent."""

import os
import sys
import time

import requests

API_BASE = "https://api.anthropic.com/v1"

# Network resilience (audit H2): requests has NO default timeout, so a
# black-holed TCP connection to api.anthropic.com used to hang the whole
# review/setup job until the 95-min workflow kill — the same failure
# github_client._gh_request fixed for GitHub. Bound every call and retry
# transient (connection / 5xx) errors; a persistent error RAISES rather than
# silently returning a partial list (see _paginate).
_API_TIMEOUT = (10, 30)          # (connect, read) seconds
_API_RETRY_ATTEMPTS = 3          # 1 try + 2 retries
_API_RETRY_BACKOFF = 2.0         # seconds, doubles each retry
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
    except (ValueError, KeyError, AttributeError, TypeError):
        # ValueError = non-JSON body; Attribute/TypeError = JSON that isn't the
        # expected {"error": {"message": …}} shape (e.g. a list or a bare
        # string). The error FORMATTER must never itself raise.
        return resp.text[:200]


def _get_with_retry(url: str, params: dict) -> requests.Response:
    """GET with a bounded timeout + retry on transient (connection / 5xx)
    errors. Returns the response for any status < 500 (2xx/3xx/4xx — the
    caller decides what a 4xx means). Raises RuntimeError after exhausting
    retries on a connection error or persistent 5xx — never hangs, never
    silently proceeds on a dead endpoint."""
    last = "unknown error"
    for attempt in range(_API_RETRY_ATTEMPTS):
        try:
            resp = requests.get(
                url, headers=get_headers(), params=params, timeout=_API_TIMEOUT
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            last = f"{type(e).__name__}: {str(e)[:150]}"
        else:
            if resp.status_code < 500:
                return resp
            last = f"HTTP {resp.status_code}: {api_error_message(resp)}"
        if attempt < _API_RETRY_ATTEMPTS - 1:
            time.sleep(_API_RETRY_BACKOFF * (2 ** attempt))
    raise RuntimeError(f"api: GET {url} failed after {_API_RETRY_ATTEMPTS} attempts — {last}")


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
        resp = _get_with_retry(f"{API_BASE}{path}", params)
        if not resp.ok:
            # A persistent 4xx mid-walk (e.g. 401 wrong-workspace key on page 2)
            # must NOT silently return a partial list — that's the exact bug
            # class github_client.PartialPageError closed. Fail loud so the
            # caller (agent sync / env lookup) aborts instead of acting on
            # partial data (spurious "missing agents", duplicate environments).
            raise RuntimeError(
                f"api: {path} returned HTTP {resp.status_code} mid-pagination "
                f"({api_error_message(resp)}) — refusing to act on a partial list"
            )
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
