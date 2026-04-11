#!/usr/bin/env python3
"""
Trigger an air review via Managed Agent.

The repo is mounted via github_repository resource for clone/push auth.
GH_TOKEN is passed in the session message for gh CLI API calls (PR comments,
review verdicts). This is a bot PAT with minimal permissions.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    export AIR_BOT_TOKEN=ghp_...
    python review.py myorg/myrepo 123
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests as req

from api import list_agents, find_environment


def get_pr_branch(repo: str, pr_number: int, token: str) -> str:
    """Get the PR's head branch name."""
    resp = req.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    if not resp.ok:
        print(f"Error fetching PR: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        sys.exit(1)
    return resp.json()["head"]["ref"]


def sync_agents():
    """Run setup.py to create/update agents with latest prompts."""
    print("[1] Syncing agents with latest prompts...")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "setup.py")],
        env=os.environ,
    )
    if result.returncode != 0:
        print("Error: agent sync failed.", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Trigger an air review for a PR")
    parser.add_argument("repo", help="owner/repo (e.g., myorg/myrepo)")
    parser.add_argument("pr_number", type=int, help="PR number to review")
    parser.add_argument("--mode", choices=["auto", "fresh", "re-review"], default="auto")
    parser.add_argument("--poll", action="store_true", help="Poll instead of streaming (default: stream)")
    args = parser.parse_args()

    bot_token = os.environ.get("AIR_BOT_TOKEN", "")
    if not bot_token:
        print("Error: AIR_BOT_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    # Step 1: Sync agents (creates if missing, updates prompts if changed)
    sync_agents()

    # Find orchestrator and environment
    agents = list_agents()
    orchestrator = agents.get("air-reviewer")
    env_id = find_environment()

    if not orchestrator or not env_id:
        print("Error: agents not found after sync.", file=sys.stderr)
        sys.exit(1)

    print(f"  Orchestrator: {orchestrator['id']} (v{orchestrator['version']})")

    # Step 2: Get PR branch
    pr_branch = get_pr_branch(args.repo, args.pr_number, bot_token)

    # Step 3: Create session with repo mounted
    print(f"[2] Creating session for PR #{args.pr_number} on {args.repo}...")

    from anthropic import Anthropic
    client = Anthropic()

    session = client.beta.sessions.create(
        agent=orchestrator["id"],
        environment_id=env_id,
        title=f"Review PR #{args.pr_number} on {args.repo}",
        resources=[{
            "type": "github_repository",
            "url": f"https://github.com/{args.repo}",
            "authorization_token": bot_token,
            "checkout": {"type": "branch", "name": pr_branch},
            "mount_path": "/workspace/repo",
        }],
    )
    print(f"  Session: {session.id}")

    # Step 4: Send review task
    # GH_TOKEN in message for gh CLI (comments, PR data, review verdicts).
    # Clone/push auth handled by github_repository resource.
    task = (
        f"Review PR #{args.pr_number} on {args.repo}.\n"
        f"REPO={args.repo}\n"
        f"PR_NUMBER={args.pr_number}\n"
        f"GH_TOKEN={bot_token}\n"
        f"PLATFORM=github\n"
        f"MODE={args.mode}\n\n"
        f"The repo is pre-cloned at /workspace/repo with branch '{pr_branch}' checked out.\n"
        f"Set GH_TOKEN as env var for gh CLI. Git push auth is pre-configured.\n"
        f"Execute the full review pipeline."
    )

    print("[3] Sending review task...")
    client.beta.sessions.events.send(
        session.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": task}]}],
    )

    if args.poll:
        poll_session(client, session.id)
    else:
        stream_session(client, session.id)


def stream_session(client, session_id: str):
    """Stream events in real-time with 30-minute timeout."""
    signal.signal(signal.SIGALRM, lambda *_: (print("\nTimed out (30 min)."), sys.exit(1)))
    signal.alarm(1800)

    print("[4] Streaming...\n")
    threads_active = 0

    with client.beta.sessions.events.stream(session_id) as stream:
        for event in stream:
            t = event.type if hasattr(event, "type") else ""

            if t == "agent.message":
                for block in event.content:
                    if hasattr(block, "text"):
                        print(block.text, end="", flush=True)
            elif t == "agent.tool_use":
                name = getattr(event, "name", "?")
                print(f"\n  [tool] {name}", flush=True)
            elif t == "session.thread_created":
                threads_active += 1
                print(f"\n  [sub-agent spawned] ({threads_active} active)", flush=True)
            elif t == "session.thread_idle":
                threads_active = max(0, threads_active - 1)
                print(f"\n  [sub-agent done] ({threads_active} active)", flush=True)
            elif t == "session.status_idle":
                print("\n\nReview complete.")
                break
            elif t == "session.error":
                print(f"\n  [session error — continuing]", flush=True)


def poll_session(client, session_id: str):
    """Poll for completion."""
    print("[4] Polling (review takes ~5-15 min)...")
    time.sleep(30)

    for i in range(150):
        s = client.beta.sessions.retrieve(session_id)

        if s.status == "idle":
            events = client.beta.sessions.events.list(session_id, limit=5, order="desc")
            has_work = any(e.type in ("agent.message", "agent.tool_use") for e in events.data)
            if has_work:
                print("\nReview complete.")
                return
            time.sleep(5)
            continue
        elif s.status == "terminated":
            print("\nSession terminated.", file=sys.stderr)
            sys.exit(1)

        print(f"  [{(i+1)*10 + 30}s] {s.status}...", flush=True)
        time.sleep(10)
    else:
        print("\nTimed out (25 min).", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
