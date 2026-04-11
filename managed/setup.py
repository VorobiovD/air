#!/usr/bin/env python3
"""
Bootstrap: creates air review agents + environment via the Anthropic API.

The GitHub Action auto-runs this on first PR. Can also run manually.
Agents are looked up by name — if they exist, they're updated with
the latest prompts. If not, they're created.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python setup.py
"""

import json
import os
import sys
from pathlib import Path

import requests

API_BASE = "https://api.anthropic.com/v1"
AGENTS_DIR = Path(__file__).parent.parent / "plugins" / "air" / "agents"
PROMPTS_DIR = Path(__file__).parent / "prompts"

HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "managed-agents-2026-04-01",
    "content-type": "application/json",
}

SUB_AGENTS = ["code-reviewer", "simplify", "security-auditor", "git-history-reviewer", "review-verifier"]


def get_headers():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    return {**HEADERS, "x-api-key": key}


def read_prompt(path: Path) -> str:
    """Read a markdown prompt file, stripping YAML frontmatter."""
    text = path.read_text()
    if text.startswith("---"):
        try:
            end = text.index("---", 3)
            return text[end + 3:].strip()
        except ValueError:
            print(f"  Warning: {path.name} has unclosed frontmatter")
            return text.strip()
    return text.strip()


def parse_agent_tools(path: Path) -> list[str]:
    """Extract tool names from agent frontmatter."""
    text = path.read_text()
    if not text.startswith("---"):
        return ["bash", "read", "grep", "glob"]
    try:
        end = text.index("---", 3)
        frontmatter = text[3:end]
    except ValueError:
        return ["bash", "read", "grep", "glob"]

    for line in frontmatter.split("\n"):
        if line.strip().startswith("tools:"):
            return [t.strip().lower() for t in line.split(":", 1)[1].strip().split(",")]
    return ["bash", "read", "grep", "glob"]


def find_agent(name: str) -> dict | None:
    """Find an existing agent by name (oldest first)."""
    resp = requests.get(f"{API_BASE}/agents", headers=get_headers())
    if not resp.ok:
        return None
    for agent in resp.json().get("data", []):
        if agent["name"] == name and not agent.get("archived_at"):
            return agent
    return None


def create_or_update_agent(name: str, system: str, tools: list, callable_agents: list | None = None) -> dict:
    """Find agent by name → update if exists, create if not."""
    existing = find_agent(name)

    if existing:
        # Update with latest prompt (version required for optimistic concurrency)
        body = {"system": system, "tools": tools, "version": existing["version"]}
        if callable_agents:
            body["callable_agents"] = callable_agents
        resp = requests.post(
            f"{API_BASE}/agents/{existing['id']}",
            headers=get_headers(),
            json=body,
        )
        if resp.ok:
            data = resp.json()
            print(f"  {name}: updated → v{data['version']}")
            return data
        else:
            err = resp.json().get("error", {}).get("message", resp.text[:200])
            print(f"  {name}: update failed ({resp.status_code}: {err}), using existing v{existing['version']}")
            return existing
    else:
        # Create new
        body = {"name": name, "model": "claude-opus-4-6", "system": system, "tools": tools}
        if callable_agents:
            body["callable_agents"] = callable_agents
        resp = requests.post(f"{API_BASE}/agents", headers=get_headers(), json=body)
        if not resp.ok:
            print(f"  {name}: creation failed — {resp.json().get('error', {}).get('message', resp.text[:200])}", file=sys.stderr)
            sys.exit(1)
        data = resp.json()
        print(f"  {name}: created → {data['id']} (v{data['version']})")
        return data


def find_or_create_environment() -> str:
    """Find existing environment or create one."""
    resp = requests.get(f"{API_BASE}/environments", headers=get_headers())
    if resp.ok:
        for env in resp.json().get("data", []):
            if env["name"] == "air-review-env" and not env.get("archived_at"):
                print(f"  Environment: {env['id']} (existing)")
                return env["id"]

    resp = requests.post(
        f"{API_BASE}/environments",
        headers=get_headers(),
        json={
            "name": "air-review-env",
            "config": {
                "type": "cloud",
                "packages": {"apt": ["gh"]},
                "networking": {"type": "unrestricted"},
            },
        },
    )
    if not resp.ok:
        print(f"  Environment creation failed: {resp.text[:200]}", file=sys.stderr)
        sys.exit(1)
    data = resp.json()
    print(f"  Environment: {data['id']} (created)")
    return data["id"]


def main():
    print("air Managed Agent bootstrap\n")

    # 1. Environment
    print("[1] Environment")
    env_id = find_or_create_environment()

    # 2. Sub-agents
    print("[2] Sub-agents")
    sub_agent_refs = []
    for name in SUB_AGENTS:
        prompt_file = AGENTS_DIR / f"{name}.md"
        if not prompt_file.exists():
            print(f"  {name}: SKIPPED — {prompt_file} not found")
            continue

        system = read_prompt(prompt_file)
        tools = parse_agent_tools(prompt_file)
        tool_configs = [{"name": t, "enabled": True} for t in tools]

        agent = create_or_update_agent(
            name=f"air-{name}",
            system=system,
            tools=[{
                "type": "agent_toolset_20260401",
                "default_config": {"enabled": False},
                "configs": tool_configs,
            }],
        )
        sub_agent_refs.append({"type": "agent", "id": agent["id"], "version": agent["version"]})

    # 3. Orchestrator
    print("[3] Orchestrator")
    orchestrator_prompt = (PROMPTS_DIR / "orchestrator.md").read_text()

    orchestrator = create_or_update_agent(
        name="air-reviewer",
        system=orchestrator_prompt,
        tools=[{"type": "agent_toolset_20260401"}],
        callable_agents=sub_agent_refs,
    )

    print(f"\nDone. Environment: {env_id}, Orchestrator: {orchestrator['id']} (v{orchestrator['version']})")
    print(f"Sub-agents: {len(sub_agent_refs)} registered")


if __name__ == "__main__":
    main()
