#!/usr/bin/env python3
"""
One-time setup: creates the air-reviewer agent, environment, vault,
and sub-agents via the Anthropic Managed Agents API.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python setup.py [--github-token ghp_...]

Outputs a config.json with all resource IDs for use by review.py and
the GitHub Action workflow.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic

BETA_HEADER = "managed-agents-2026-04-01"
AGENTS_DIR = Path(__file__).parent.parent / "plugins" / "air" / "agents"
PROMPTS_DIR = Path(__file__).parent / "prompts"
CONFIG_PATH = Path(__file__).parent / "config.json"


def read_prompt(path: Path) -> str:
    """Read a markdown prompt file, stripping YAML frontmatter."""
    text = path.read_text()
    if text.startswith("---"):
        try:
            end = text.index("---", 3)
            return text[end + 3:].strip()
        except ValueError:
            print(f"  Warning: {path.name} has unclosed frontmatter, using full content")
            return text.strip()
    return text.strip()


def create_environment(client: Anthropic) -> str:
    """Create the sandbox environment with gh CLI pre-installed."""
    env = client.beta.environments.create(
        name="air-review-env",
        config={
            "type": "cloud",
            "packages": {"apt": ["gh"]},
            "networking": {"type": "unrestricted"},
        },
    )
    print(f"  Environment: {env.id}")
    return env.id


def create_vault(client: Anthropic, github_token: str | None) -> str | None:
    """Create a vault and store the GitHub PAT."""
    if not github_token:
        print("  Vault: skipped (no --github-token)")
        return None

    vault = client.beta.vaults.create(
        display_name="air-reviewer",
        metadata={"purpose": "code-review"},
    )

    client.beta.vaults.credentials.create(
        vault.id,
        display_name="GitHub PAT",
        auth={
            "type": "static_bearer",
            "mcp_server_url": "https://mcp.github.com/mcp",
            "token": github_token,
        },
    )
    print(f"  Vault: {vault.id} (GitHub PAT stored)")
    return vault.id


def parse_agent_tools(path: Path) -> list[str]:
    """Extract tool names from agent frontmatter (e.g., 'tools: Read, Grep, Glob, Bash')."""
    text = path.read_text()
    if not text.startswith("---"):
        return ["bash", "read", "grep", "glob"]  # default
    try:
        end = text.index("---", 3)
        frontmatter = text[3:end]
    except ValueError:
        return ["bash", "read", "grep", "glob"]

    for line in frontmatter.split("\n"):
        if line.strip().startswith("tools:"):
            tools_str = line.split(":", 1)[1].strip()
            return [t.strip().lower() for t in tools_str.split(",")]
    return ["bash", "read", "grep", "glob"]


def create_sub_agent(client: Anthropic, name: str, prompt_file: Path) -> dict:
    """Create a sub-agent from an agent markdown file, respecting its declared tools."""
    system = read_prompt(prompt_file)
    declared_tools = parse_agent_tools(prompt_file)

    tool_configs = [{"name": t, "enabled": True} for t in declared_tools]

    agent = client.beta.agents.create(
        name=f"air-{name}",
        model="claude-opus-4-6",
        system=system,
        tools=[{
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": False},
            "configs": tool_configs,
        }],
    )
    print(f"  Agent {name}: {agent.id} (v{agent.version}) tools={declared_tools}")
    return {"id": agent.id, "version": agent.version}


def create_orchestrator(sub_agents: dict) -> dict:
    """Create the orchestrator agent with callable_agents via raw API.
    The SDK doesn't support callable_agents yet, so we use requests directly."""
    import requests as req

    system = (PROMPTS_DIR / "orchestrator.md").read_text()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    callable_agents = [
        {"type": "agent", "id": info["id"], "version": info["version"]}
        for info in sub_agents.values()
    ]

    body = {
        "name": "air-reviewer",
        "model": "claude-opus-4-6",
        "system": system,
        "tools": [{"type": "agent_toolset_20260401"}],
        "callable_agents": callable_agents,
    }

    resp = req.post(
        "https://api.anthropic.com/v1/agents",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "managed-agents-2026-04-01",
            "content-type": "application/json",
        },
        json=body,
    )
    data = resp.json()

    if "error" in data:
        print(f"  callable_agents failed: {data['error']['message']}")
        print("  Falling back to standalone mode...")
        del body["callable_agents"]
        resp = req.post(
            "https://api.anthropic.com/v1/agents",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "managed-agents-2026-04-01",
                "content-type": "application/json",
            },
            json=body,
        )
        data = resp.json()
        mode = "standalone"
    else:
        mode = "multi-agent"

    print(f"  Orchestrator: {data['id']} (v{data['version']}) [{mode}]")
    return {"id": data["id"], "version": data["version"], "mode": mode}


def main():
    parser = argparse.ArgumentParser(description="Set up air Managed Agent resources")
    parser.add_argument("--github-token", help="GitHub PAT for vault (optional, can set GH_TOKEN at session level)")
    args = parser.parse_args()

    client = Anthropic()

    print("Creating air Managed Agent resources...\n")

    # 1. Environment
    print("[1/4] Environment")
    env_id = create_environment(client)

    # 2. Vault
    print("[2/4] Vault")
    vault_id = create_vault(client, args.github_token)

    # 3. Sub-agents (4 reviewers + 1 verifier)
    print("[3/4] Sub-agents")
    agent_files = {
        "code-reviewer": AGENTS_DIR / "code-reviewer.md",
        "simplify": AGENTS_DIR / "simplify.md",
        "security-auditor": AGENTS_DIR / "security-auditor.md",
        "git-history-reviewer": AGENTS_DIR / "git-history-reviewer.md",
        "review-verifier": AGENTS_DIR / "review-verifier.md",
    }

    sub_agents = {}
    for name, path in agent_files.items():
        if not path.exists():
            print(f"  WARNING: {path} not found, skipping")
            continue
        sub_agents[name] = create_sub_agent(client, name, path)

    # 4. Orchestrator
    print("[4/4] Orchestrator")
    orchestrator = create_orchestrator(sub_agents)

    # Save config
    config = {
        "environment_id": env_id,
        "vault_id": vault_id,
        "orchestrator": orchestrator,
        "sub_agents": sub_agents,
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    print(f"\nConfig saved to {CONFIG_PATH}")
    print("Use these IDs in review.py and the GitHub Action workflow.")


if __name__ == "__main__":
    main()
