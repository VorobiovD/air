"""Shared API helpers for the air Managed Agent."""

import os
import sys

import requests

API_BASE = "https://api.anthropic.com/v1"
HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "managed-agents-2026-04-01",
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


def list_agents() -> dict[str, dict]:
    """Fetch all agents once, return as {name: agent} dict.
    API returns newest first. We iterate oldest→newest so dict overwrites
    keep the newest non-archived agent per name (matches prior behavior)."""
    resp = requests.get(f"{API_BASE}/agents", headers=get_headers())
    if not resp.ok:
        return {}
    result = {}
    for agent in reversed(resp.json().get("data", [])):
        if not agent.get("archived_at"):
            result[agent["name"]] = agent
    return result


def find_environment() -> str | None:
    """Find existing environment by name."""
    resp = requests.get(f"{API_BASE}/environments", headers=get_headers())
    if not resp.ok:
        return None
    for env in resp.json().get("data", []):
        if env["name"] == "air-review-env" and not env.get("archived_at"):
            return env["id"]
    return None
