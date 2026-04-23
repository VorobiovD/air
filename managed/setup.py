#!/usr/bin/env python3
"""
Bootstrap and sync: creates/updates air review agents + environment.

Fetches the agent list once, then creates or updates each agent.
Called by review.py on every run to keep prompts in sync.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python setup.py
"""

import functools
import os
import sys
from pathlib import Path

import requests

from api import API_BASE, HEADERS, get_headers, api_error_message, list_agents

AGENTS_DIR = Path(__file__).parent.parent / "plugins" / "air" / "agents"
PROMPTS_DIR = Path(__file__).parent / "prompts"

SUB_AGENTS = ["code-reviewer", "simplify", "security-auditor", "git-history-reviewer", "review-verifier"]


MODEL_ALIASES = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}

DEFAULT_OPUS = MODEL_ALIASES["opus"]


@functools.lru_cache(maxsize=None)
def _split_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """Return ({key: value} for scalar frontmatter fields, body_text). Empty dict if no frontmatter.

    Cached per-path so the file is read once per run and warnings (e.g. unclosed
    frontmatter) fire once, not once per consumer (read_prompt / parse_agent_tools /
    parse_agent_model).
    """
    text = path.read_text()
    if not text.startswith("---"):
        return {}, text.strip()
    try:
        end = text.index("---", 3)
    except ValueError:
        print(f"  Warning: {path.name} has unclosed frontmatter")
        return {}, text.strip()
    fields: dict[str, str] = {}
    for line in text[3:end].split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        # Strip inline YAML comments. Note: naive — a legitimate `#` inside a
        # value (e.g. a URL fragment or quoted `#123` reference) will be truncated.
        # Acceptable for the current scalar fields (name, model, tools); use a
        # real YAML parser if quoted values with `#` become needed.
        fields[key.strip()] = value.split("#", 1)[0].strip()
    return fields, text[end + 3:].strip()


def read_prompt(path: Path) -> str:
    """Read a markdown prompt file, stripping YAML frontmatter."""
    _, body = _split_frontmatter(path)
    return body


def parse_agent_tools(path: Path) -> list[str]:
    """Extract tool names from agent frontmatter."""
    fields, _ = _split_frontmatter(path)
    if "tools" not in fields:
        return ["bash", "read", "grep", "glob"]
    return [t.strip().lower() for t in fields["tools"].split(",")]


def parse_agent_model(path: Path, default: str = DEFAULT_OPUS) -> str:
    """Read `model:` from agent frontmatter, resolving aliases to API IDs."""
    fields, _ = _split_frontmatter(path)
    value = fields.get("model", "")
    if not value:
        return default
    return MODEL_ALIASES.get(value, value)


def create_or_update_agent(name: str, system: str, tools: list, existing: dict | None, callable_agents: list | None = None, model: str = DEFAULT_OPUS) -> dict:
    """Update if exists, create if not. Takes pre-fetched existing agent.

    On update, the `model` field is sent so model changes (e.g. from frontmatter
    tiering) propagate to existing managed deployments. If the API rejects a
    model change in-place, we retry without `model` and print a warning so the
    operator knows the agent needs manual re-creation to pick up the new model.
    """

    if existing:
        body = {"model": model, "system": system, "tools": tools, "version": existing["version"]}
        if callable_agents:
            body["callable_agents"] = callable_agents
        resp = requests.post(
            f"{API_BASE}/agents/{existing['id']}",
            headers=get_headers(),
            json=body,
        )
        if resp.ok:
            data = resp.json()
            if existing.get("model") and existing["model"] != model:
                print(f"  {name}: synced → v{data['version']} (model {existing['model']} → {model})")
            else:
                print(f"  {name}: synced → v{data['version']}")
            return data
        # Retry without model ONLY if the primary failure mentions model
        # (API disallows in-place model changes on some endpoints).
        primary_error = api_error_message(resp)
        if existing.get("model") != model and "model" in str(primary_error).lower():
            retry_body = {k: v for k, v in body.items() if k != "model"}
            retry = requests.post(
                f"{API_BASE}/agents/{existing['id']}",
                headers=get_headers(),
                json=retry_body,
            )
            if retry.ok:
                data = retry.json()
                print(
                    f"  {name}: synced prompt → v{data['version']} "
                    f"(model pinned to {existing.get('model', '?')} — delete the agent via the Anthropic console "
                    f"or DELETE /agents/{existing['id']}, then re-run setup.py to re-tier to {model})"
                )
                return data
            # Double-failure: report both errors
            print(
                f"  {name}: sync failed ({resp.status_code}: {primary_error}); "
                f"retry-without-model also failed ({retry.status_code}: {api_error_message(retry)}), "
                f"using v{existing['version']}"
            )
            return existing
        print(f"  {name}: sync failed ({resp.status_code}: {primary_error}), using v{existing['version']}")
        return existing
    else:
        body = {"name": name, "model": model, "system": system, "tools": tools}
        if callable_agents:
            body["callable_agents"] = callable_agents
        resp = requests.post(f"{API_BASE}/agents", headers=get_headers(), json=body)
        if not resp.ok:
            print(f"  {name}: creation failed — {api_error_message(resp)}", file=sys.stderr)
            sys.exit(1)
        data = resp.json()
        print(f"  {name}: created → {data['id']} (v{data['version']}, model={model})")
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
        model = parse_agent_model(prompt_file)
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
            model=model,
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
