#!/usr/bin/env python3
"""
Trigger an air review via Managed Agent with multi-agent orchestration.

The orchestrator agent spawns 4 reviewer sub-agents in parallel threads,
collects findings, runs verification, and posts the review — all within
one session, mirroring the CLI plugin architecture.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python review.py myorg/myrepo 123 --app-auth
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from anthropic import Anthropic

CONFIG_PATH = Path(__file__).parent / "config.json"


def generate_app_token(config: dict) -> str:
    """Generate a short-lived GitHub App installation token (1 hour expiry)."""
    try:
        import jwt
        import requests as req
    except ImportError:
        print("Error: PyJWT, cryptography, requests required. pip install PyJWT cryptography requests", file=sys.stderr)
        sys.exit(1)

    app = config.get("github_app", {})
    app_id = app.get("app_id") or os.environ.get("APP_ID", "")
    install_id = app.get("installation_id") or os.environ.get("INSTALLATION_ID", "")
    key_path = app.get("private_key_path") or os.environ.get("APP_PRIVATE_KEY_PATH", "")

    if not all([app_id, install_id, key_path]):
        print("Error: GitHub App auth requires app_id, installation_id, private_key_path.", file=sys.stderr)
        sys.exit(1)

    pem = Path(key_path).expanduser().read_text()
    now = int(time.time())
    jwt_token = jwt.encode({"iat": now - 60, "exp": now + 600, "iss": str(app_id)}, pem, algorithm="RS256")

    resp = req.post(
        f"https://api.github.com/app/installations/{install_id}/access_tokens",
        headers={"Authorization": f"Bearer {jwt_token}", "Accept": "application/vnd.github+json"},
    )
    if not resp.ok:
        print(f"Error: GitHub API returned {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        sys.exit(1)
    data = resp.json()
    if "token" not in data:
        print(f"Error: unexpected response: {data}", file=sys.stderr)
        sys.exit(1)

    print(f"  Token ready (expires {data['expires_at']}, posts as air-reviewer[bot])")
    return data["token"]


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print("Error: config.json not found. Run setup.py first.", file=sys.stderr)
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text())


def main():
    parser = argparse.ArgumentParser(description="Trigger an air review for a PR")
    parser.add_argument("repo", help="owner/repo (e.g., myorg/myrepo)")
    parser.add_argument("pr_number", type=int, help="PR number to review")
    parser.add_argument("--mode", choices=["auto", "fresh", "re-review"], default="auto")
    parser.add_argument("--gh-token", help="GitHub token (or set GH_TOKEN env var, or use --app-auth)")
    parser.add_argument("--app-auth", action="store_true", help="Generate token from GitHub App")
    parser.add_argument("--poll", action="store_true", help="Poll instead of streaming (default: stream)")
    args = parser.parse_args()

    config = load_config()
    client = Anthropic()

    # Step 1: Resolve GitHub token
    gh_token = args.gh_token or os.environ.get("GH_TOKEN", "")
    if not gh_token and args.app_auth:
        print("[1] Generating GitHub App token...")
        gh_token = generate_app_token(config)
    if not gh_token:
        print("Error: No GitHub token. Use --app-auth, --gh-token, or set GH_TOKEN.", file=sys.stderr)
        sys.exit(1)

    # Step 2: Create session
    print(f"[2] Creating session for PR #{args.pr_number} on {args.repo}...")
    session_kwargs = {
        "agent": config["orchestrator"]["id"],
        "environment_id": config["environment_id"],
        "title": f"Review PR #{args.pr_number} on {args.repo}",
    }
    if config.get("vault_id"):
        session_kwargs["vault_ids"] = [config["vault_id"]]

    session = client.beta.sessions.create(**session_kwargs)
    print(f"  Session: {session.id}")

    # Step 3: Send the review task
    task = (
        f"Review PR #{args.pr_number} on {args.repo}.\n"
        f"REPO={args.repo}\n"
        f"PR_NUMBER={args.pr_number}\n"
        f"GH_TOKEN={gh_token}\n"
        f"MODE={args.mode}\n\n"
        f"Execute the full review pipeline:\n"
        f"1. Setup auth and clone the repo\n"
        f"2. Fetch PR data and load wiki context\n"
        f"3. Delegate to ALL 4 reviewer sub-agents in PARALLEL\n"
        f"4. Collect findings, run verification via the verifier sub-agent\n"
        f"5. Post the consolidated review as a PR comment\n"
        f"6. Push learned patterns to the wiki\n"
    )

    print("[3] Sending review task...")
    client.beta.sessions.events.send(
        session.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": task}]}],
    )

    # Step 4: Monitor
    if args.poll:
        poll_session(client, session.id)
    else:
        stream_session(client, session.id)


def stream_session(client: Anthropic, session_id: str):
    """Stream events in real-time with 30-minute timeout."""
    import signal

    def timeout_handler(signum, frame):
        print("\n\nStream timed out after 30 minutes.", file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(1800)  # 30 minutes

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
            elif t == "agent.mcp_tool_use":
                name = getattr(event, "name", "?")
                print(f"\n  [mcp] {name}", flush=True)
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
                print(f"\n\nSession error: {event}", file=sys.stderr)
                sys.exit(1)


def poll_session(client: Anthropic, session_id: str):
    """Poll for completion with initial delay."""
    print("[4] Polling (review takes ~5-15 min)...")

    # Wait 30s before first poll to let session start
    time.sleep(30)

    for i in range(150):  # ~25 min max
        s = client.beta.sessions.retrieve(session_id)
        status = s.status

        if status == "idle":
            # Check if work was actually done (not just initial idle)
            events = client.beta.sessions.events.list(session_id, limit=5, order="desc")
            has_work = any(e.type in ("agent.message", "agent.tool_use", "agent.mcp_tool_use") for e in events.data)
            if has_work:
                print("\nReview complete.")
                # Print final messages
                all_events = client.beta.sessions.events.list(session_id, limit=100, order="desc")
                for event in reversed(all_events.data):
                    if event.type == "agent.message":
                        for block in event.content:
                            if hasattr(block, "text") and block.text.strip():
                                print(block.text)
                break
            else:
                # Session idle but no work yet — wait more
                time.sleep(5)
                continue
        elif status == "terminated":
            print(f"\nSession terminated.", file=sys.stderr)
            sys.exit(1)
        else:
            elapsed = (i + 1) * 10 + 30
            print(f"  [{elapsed}s] {status}...", flush=True)
            time.sleep(10)
    else:
        print("\nTimed out after 25 min.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
