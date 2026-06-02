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
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests as req

from api import API_BASE, get_headers, list_agents, find_environment, api_error_message
from setup import MODEL_ALIASES, create_or_update_agent


def sync_learn_agent():
    """Create or update the learn orchestrator agent.

    Delegates to setup.create_or_update_agent so the model field propagates on
    update (same retry-without-model fallback as sub-agents) and there's one
    source of truth for the update body shape.
    """
    agents = list_agents()
    prompt = (Path(__file__).parent / "prompts" / "learn-orchestrator.md").read_text()
    return create_or_update_agent(
        name="air-learner",
        system=prompt,
        tools=[{"type": "agent_toolset_20260401"}],
        existing=agents.get("air-learner"),
        # Sonnet, not Opus: wiki cleanup is structured dedup/reorg work, and
        # the learner fires as a review epilogue — it ran on 4 of the 5
        # runs preceding the 2026-05-22 credit exhaustion. If the API
        # refuses the in-place model change, create_or_update_agent retries
        # without `model` and prints the remediation (archive air-learner
        # via console or POST /agents/{id}/archive — the API has no DELETE
        # route for agents, verified 2026-06-02 — then re-run to re-create).
        model=MODEL_ALIASES["sonnet"],
    )


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

    # Reset the shared `/air:learn` trigger counter on the wiki so the next
    # review sees a clean `reviews_since: 0` and the cadence restarts.
    # Best-effort — never fails the overall learn run.
    try:
        _reset_learn_counter(args.repo, bot_token)
    except Exception as e:
        print(f"  [warn] counter reset failed: {e}", file=sys.stderr)


def _reset_learn_counter(repo: str, bot_token: str) -> None:
    """Clone the wiki, call `meta.py reset`, push. Mirrors the update path
    in managed/review.py::_update_learn_counter but calls `reset` instead
    of `bump`+`check`."""
    air_root = Path(__file__).resolve().parent.parent
    lib_dir = air_root / "plugins" / "air" / "lib"
    meta_script = lib_dir / "meta.py"
    if not meta_script.is_file():
        print(f"  [warn] meta.py not found at {meta_script}", file=sys.stderr)
        return
    sys.path.insert(0, str(lib_dir))
    import wiki_git  # type: ignore

    wiki_url = f"https://x-access-token:{bot_token}@github.com/{repo}.wiki.git"
    with tempfile.TemporaryDirectory(prefix="air-wiki-learn-") as tmp:
        wiki_dir = Path(tmp) / "wiki"
        if not wiki_git.clone_wiki(wiki_url, wiki_dir):
            return
        wiki_git.configure_identity(wiki_dir, "air-machine", "air-machine@users.noreply.github.com")
        result = subprocess.run(
            [sys.executable, str(meta_script), "reset", "--wiki-dir", str(wiki_dir),
             "--pr-number", "0"],
            capture_output=True, text=True,
        )
        sys.stderr.write(result.stderr)
        if result.returncode != 0:
            return
        wiki_git.commit_meta(wiki_dir, "meta: reset counter after /air:learn")


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
