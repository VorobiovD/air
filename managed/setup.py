#!/usr/bin/env python3
"""
Bootstrap and sync: creates/updates air review agents + environment.

Fetches the agent list once, then creates or updates each agent.
Called by review.py on every run to keep prompts in sync.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python setup.py
"""

import os
import sys
from pathlib import Path

import requests

from api import API_BASE, HEADERS, get_headers, api_error_message, list_agents

AGENTS_DIR = Path(__file__).parent.parent / "plugins" / "air" / "agents"
PROMPTS_DIR = Path(__file__).parent / "prompts"

SUB_AGENTS = ["code-reviewer", "simplify", "security-auditor", "git-history-reviewer", "review-verifier"]


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


def create_or_update_agent(name: str, system: str, tools: list, existing: dict | None, callable_agents: list | None = None) -> dict:
    """Update if exists, create if not. Takes pre-fetched existing agent."""

    if existing:
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
            print(f"  {name}: synced → v{data['version']}")
            return data
        else:
            print(f"  {name}: sync failed ({resp.status_code}: {api_error_message(resp)}), using v{existing['version']}")
            return existing
    else:
        body = {"name": name, "model": "claude-opus-4-6", "system": system, "tools": tools}
        if callable_agents:
            body["callable_agents"] = callable_agents
        resp = requests.post(f"{API_BASE}/agents", headers=get_headers(), json=body)
        if not resp.ok:
            print(f"  {name}: creation failed — {api_error_message(resp)}", file=sys.stderr)
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
                print(f"  Environment: {env['id']}")
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
        print(f"  Environment creation failed: {api_error_message(resp)}", file=sys.stderr)
        sys.exit(1)
    data = resp.json()
    print(f"  Environment: {data['id']} (created)")
    return data["id"]


def main():
    print("Syncing air agents...\n")

    # 1. Environment
    print("[1] Environment")
    env_id = find_or_create_environment()

    # 2. Fetch all agents once (fix N+1)
    print("[2] Fetching agent list...")
    agents_by_name = list_agents()

    # 3. Sub-agents
    print("[3] Sub-agents")
    sub_agent_refs = []
    for name in SUB_AGENTS:
        prompt_file = AGENTS_DIR / f"{name}.md"
        if not prompt_file.exists():
            print(f"  air-{name}: SKIPPED — {prompt_file} not found")
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
            existing=agents_by_name.get(f"air-{name}"),
        )
        sub_agent_refs.append({"type": "agent", "id": agent["id"], "version": agent["version"]})

    # 4. Orchestrator
    print("[4] Orchestrator")
    orchestrator_prompt = (PROMPTS_DIR / "orchestrator.md").read_text()

    orchestrator = create_or_update_agent(
        name="air-reviewer",
        system=orchestrator_prompt,
        tools=[{"type": "agent_toolset_20260401"}],
        existing=agents_by_name.get("air-reviewer"),
        callable_agents=sub_agent_refs,
    )

    print(f"\nDone. Orchestrator: {orchestrator['id']} (v{orchestrator['version']}), {len(sub_agent_refs)} sub-agents")


if __name__ == "__main__":
    main()
