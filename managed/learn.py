#!/usr/bin/env python3
"""
Trigger wiki cleanup + history regeneration via Managed Agent.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    export AIR_BOT_TOKEN=ghp_...
    python learn.py myorg/myrepo
    python learn.py myorg/myrepo --history-only
    python learn.py myorg/myrepo --refresh-profile
"""

import argparse
import os
import signal
import sys
import time
from pathlib import Path

import requests as req

from api import API_BASE, get_headers, list_agents, find_environment, api_error_message


def sync_learn_agent():
    """Create or update the learn orchestrator agent."""
    agents = list_agents()
    existing = agents.get("air-learner")
    prompt = (Path(__file__).parent / "prompts" / "learn-orchestrator.md").read_text()

    if existing:
        resp = req.post(
            f"{API_BASE}/agents/{existing['id']}",
            headers=get_headers(),
            json={"system": prompt, "tools": [{"type": "agent_toolset_20260401"}], "version": existing["version"]},
        )
        if resp.ok:
            data = resp.json()
            print(f"  air-learner: synced → v{data['version']}")
            return data
        else:
            print(f"  air-learner: sync failed, using v{existing['version']}")
            return existing
    else:
        resp = req.post(
            f"{API_BASE}/agents",
            headers=get_headers(),
            json={
                "name": "air-learner",
                "model": "claude-opus-4-6",
                "system": prompt,
                "tools": [{"type": "agent_toolset_20260401"}],
            },
        )
        if not resp.ok:
            print(f"Error creating learn agent: {api_error_message(resp)}", file=sys.stderr)
            sys.exit(1)
        data = resp.json()
        print(f"  air-learner: created → {data['id']} (v{data['version']})")
        return data


def main():
    parser = argparse.ArgumentParser(description="Trigger wiki cleanup + history regeneration")
    parser.add_argument("repo", help="owner/repo")
    parser.add_argument("--history-only", action="store_true", help="Only regenerate REVIEW-HISTORY.md")
    parser.add_argument("--refresh-profile", action="store_true", help="Re-run full project scan")
    parser.add_argument("--poll", action="store_true", help="Poll instead of streaming")
    args = parser.parse_args()

    bot_token = os.environ.get("AIR_BOT_TOKEN", "")
    if not bot_token:
        print("Error: AIR_BOT_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    # Sync learn agent
    print("[1] Syncing learn agent...")
    agent = sync_learn_agent()
    env_id = find_environment()

    if not env_id:
        print("Error: environment not found. Run setup.py first.", file=sys.stderr)
        sys.exit(1)

    # Determine mode
    mode = "full"
    if args.history_only:
        mode = "history-only"
    elif args.refresh_profile:
        mode = "refresh-profile"

    # Create session
    print(f"[2] Creating learn session for {args.repo} (mode: {mode})...")
    from anthropic import Anthropic
    client = Anthropic()

    session = client.beta.sessions.create(
        agent=agent["id"],
        environment_id=env_id,
        title=f"Learn — {args.repo}",
        resources=[{
            "type": "github_repository",
            "url": f"https://github.com/{args.repo}",
            "authorization_token": bot_token,
            "checkout": {"type": "branch", "name": "main"},
            "mount_path": "/workspace/repo",
        }],
    )
    print(f"  Session: {session.id}")

    # Send task
    task = (
        f"Run wiki cleanup for {args.repo}.\n"
        f"REPO={args.repo}\n"
        f"GH_TOKEN={bot_token}\n"
        f"MODE={mode}\n\n"
        f"The repo is at /workspace/repo. Set GH_TOKEN as env var.\n"
        f"Execute the full learn pipeline."
    )

    print("[3] Running learn...\n")
    client.beta.sessions.events.send(
        session.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": task}]}],
    )

    if args.poll:
        poll(client, session.id)
    else:
        stream(client, session.id)


def stream(client, session_id: str):
    signal.signal(signal.SIGALRM, lambda *_: (print("\nTimed out."), sys.exit(1)))
    signal.alarm(900)  # 15 min

    with client.beta.sessions.events.stream(session_id) as s:
        for event in s:
            t = event.type if hasattr(event, "type") else ""
            if t == "agent.message":
                for block in event.content:
                    if hasattr(block, "text"):
                        print(block.text, end="", flush=True)
            elif t == "agent.tool_use":
                print(f"\n  [tool] {getattr(event, 'name', '?')}", flush=True)
            elif t == "session.status_idle":
                print("\n\nDone.")
                break
            elif t == "session.error":
                print(f"\n  [error — continuing]", flush=True)


def poll(client, session_id: str):
    print("Polling...")
    time.sleep(15)
    for i in range(60):  # 10 min
        s = client.beta.sessions.retrieve(session_id)
        if s.status == "idle":
            print("\nDone.")
            return
        elif s.status == "terminated":
            print("\nTerminated.", file=sys.stderr)
            sys.exit(1)
        print(f"  [{(i+1)*10 + 15}s] {s.status}...", flush=True)
        time.sleep(10)
    print("\nTimed out.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
