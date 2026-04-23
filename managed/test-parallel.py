#!/usr/bin/env python3
"""
Parallel sub-agent smoke test.

Creates (or reuses) 4 trivial sleep agents + an orchestrator that calls all 4,
measures wall-clock time, and decides whether Anthropic's Managed Agents API
actually dispatches callable_agent invocations in parallel or serializes them.

Each sub-agent sleeps SLEEP_SECONDS seconds then returns.
- Parallel dispatch  → wall-clock ≈ SLEEP_SECONDS + orchestration overhead
- Sequential dispatch → wall-clock ≈ 4 * SLEEP_SECONDS + overhead

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python test-parallel.py [sleep_seconds]   # default 10

Cleanup: the test agents persist in your account. Delete manually or via:
    python test-parallel.py --cleanup
"""

import os
import sys
import time

import requests as req

from setup import MODEL_ALIASES

API_BASE = "https://api.anthropic.com/v1"
HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "managed-agents-2026-04-01",
    "content-type": "application/json",
}

SUB_AGENT_NAMES = [f"air-test-parallel-sub{i}" for i in range(1, 5)]
ORCHESTRATOR_NAME = "air-test-parallel-orchestrator"


def get_headers():
    return {**HEADERS, "x-api-key": os.environ["ANTHROPIC_API_KEY"]}


def list_agents() -> dict:
    resp = req.get(f"{API_BASE}/agents", headers=get_headers())
    resp.raise_for_status()
    return {a["name"]: a for a in resp.json().get("data", []) if not a.get("archived_at")}


def delete_agent(agent_id: str) -> None:
    req.delete(f"{API_BASE}/agents/{agent_id}", headers=get_headers())


def cleanup():
    existing = list_agents()
    for name in SUB_AGENT_NAMES + [ORCHESTRATOR_NAME]:
        if name in existing:
            delete_agent(existing[name]["id"])
            print(f"  deleted {name}")
    print("cleanup done.")


def create_or_reuse_sub_agent(name: str, sleep_seconds: int, existing: dict) -> dict:
    if name in existing:
        print(f"  reusing {name} → {existing[name]['id']}")
        return existing[name]
    body = {
        "name": name,
        "model": MODEL_ALIASES["haiku"],  # cheapest; this agent does almost nothing
        "system": (
            f"You are test sub-agent {name}. When called, record the current time as START, "
            f"then run exactly one bash command: `sleep {sleep_seconds}`. After it completes, "
            f"record the time as END. Return a single line of the form: "
            f"`{name}: START=<unix-ts> END=<unix-ts>`. Do nothing else."
        ),
        "tools": [{"type": "agent_toolset_20260401", "configs": [{"name": "bash", "enabled": True}]}],
    }
    resp = req.post(f"{API_BASE}/agents", headers=get_headers(), json=body)
    resp.raise_for_status()
    data = resp.json()
    print(f"  created {name} → {data['id']}")
    return data


def create_or_reuse_orchestrator(sub_refs: list[dict], existing: dict) -> dict:
    system = (
        "You are a test orchestrator. You have 4 callable sub-agents: "
        f"{', '.join(SUB_AGENT_NAMES)}. Your only job: send a trivial message "
        "('go') to ALL FOUR sub-agents SIMULTANEOUSLY (in a single turn — do NOT "
        "wait for one to return before calling the next). Wait for all four to "
        "return. Concatenate their responses into a single final message. Do not "
        "do any work yourself."
    )
    if ORCHESTRATOR_NAME in existing:
        # Update the callable_agents in case sub-agent versions changed.
        agent = existing[ORCHESTRATOR_NAME]
        body = {
            "system": system,
            "tools": [{"type": "agent_toolset_20260401"}],
            "version": agent["version"],
            "callable_agents": sub_refs,
        }
        resp = req.post(f"{API_BASE}/agents/{agent['id']}", headers=get_headers(), json=body)
        if resp.ok:
            print(f"  reusing {ORCHESTRATOR_NAME} → {agent['id']} (synced)")
            return resp.json()
        # fall through to recreate
        delete_agent(agent["id"])

    body = {
        "name": ORCHESTRATOR_NAME,
        "model": MODEL_ALIASES["sonnet"],
        "system": system,
        "tools": [{"type": "agent_toolset_20260401"}],
        "callable_agents": sub_refs,
    }
    resp = req.post(f"{API_BASE}/agents", headers=get_headers(), json=body)
    resp.raise_for_status()
    data = resp.json()
    print(f"  created {ORCHESTRATOR_NAME} → {data['id']}")
    return data


def run(sleep_seconds: int):
    print(f"[1] Setting up {len(SUB_AGENT_NAMES)} sub-agents (each sleeps {sleep_seconds}s)")
    existing = list_agents()
    subs = [create_or_reuse_sub_agent(n, sleep_seconds, existing) for n in SUB_AGENT_NAMES]
    sub_refs = [
        {
            "type": "agent",
            "id": s["id"],
            "version": s["version"],
            "permission_policy": {"type": "always_allow"},
        }
        for s in subs
    ]

    print(f"\n[2] Setting up orchestrator with {len(sub_refs)} callable_agents")
    existing = list_agents()  # re-fetch in case sub creation changed state
    orch = create_or_reuse_orchestrator(sub_refs, existing)

    print("\n[3] Finding environment")
    resp = req.get(f"{API_BASE}/environments", headers=get_headers())
    env_id = None
    for e in resp.json().get("data", []):
        if e["name"] == "air-review-env" and not e.get("archived_at"):
            env_id = e["id"]
            break
    if not env_id:
        print("  Error: no environment found. Run `python setup.py` first.", file=sys.stderr)
        sys.exit(1)
    print(f"  env: {env_id}")

    print("\n[4] Creating session + triggering orchestrator")
    from anthropic import Anthropic
    client = Anthropic()
    session = client.beta.sessions.create(
        agent=orch["id"],
        environment_id=env_id,
        title="air parallel sub-agent smoke test",
    )
    print(f"  session: {session.id}")

    t0 = time.monotonic()
    client.beta.sessions.events.send(
        session.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": "Run all 4 sub-agents now."}]}],
    )

    # Stream events; track sub-agent call timestamps.
    sub_call_starts: dict[str, float] = {}
    sub_call_ends: dict[str, float] = {}
    print("\n[5] Streaming events...")
    with client.beta.sessions.events.stream(session.id) as stream:
        for event in stream:
            t = event.type if hasattr(event, "type") else ""
            now = time.monotonic() - t0
            if t == "agent.tool_use":
                name = getattr(event, "name", "?")
                if name in SUB_AGENT_NAMES:
                    sub_call_starts[name] = now
                    print(f"  [{now:6.2f}s] → {name} dispatched")
            elif t == "agent.tool_result":
                name = getattr(event, "name", "?")
                if name in SUB_AGENT_NAMES:
                    sub_call_ends[name] = now
                    print(f"  [{now:6.2f}s] ← {name} returned")
            elif t == "session.status_idle":
                wall = time.monotonic() - t0
                print(f"\n[6] Session complete in {wall:.2f}s")
                break
            elif t == "session.error":
                print(f"  [error: {getattr(event, 'error', {})}]", file=sys.stderr)
                break

    wall = time.monotonic() - t0

    print("\n--- Verdict ---")
    print(f"wall-clock: {wall:.2f}s")
    print(f"per-agent sleep: {sleep_seconds}s")
    print(f"expected parallel: ~{sleep_seconds + 5}s (sleep + overhead)")
    print(f"expected serial:   ~{sleep_seconds * 4 + 5}s")

    if sub_call_starts:
        starts = sorted(sub_call_starts.values())
        print(f"\nsub-agent dispatch spread: {starts[-1] - starts[0]:.2f}s (first → last)")
        if len(sub_call_starts) >= 2 and starts[-1] - starts[0] < sleep_seconds * 0.5:
            print(f"→ PARALLEL (all {len(sub_call_starts)} dispatched within < half a sleep window)")
        else:
            print(f"→ SERIAL (dispatches are staggered by more than half a sleep window)")

    if wall < sleep_seconds * 2:
        print(f"→ wall-clock confirms PARALLEL execution")
    elif wall > sleep_seconds * 3:
        print(f"→ wall-clock suggests SERIAL execution")
    else:
        print(f"→ wall-clock ambiguous — inspect dispatch spread above")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--cleanup":
        cleanup()
        return
    sleep_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    run(sleep_seconds)


if __name__ == "__main__":
    main()
