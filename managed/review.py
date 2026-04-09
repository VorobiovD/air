#!/usr/bin/env python3
"""
Trigger an air review session for a specific PR.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python review.py myorg/myrepo 123
    python review.py myorg/myrepo 123 --mode re-review
    python review.py myorg/myrepo 123 --platform gitlab
"""

import argparse
import json
import sys
from pathlib import Path

from anthropic import Anthropic

CONFIG_PATH = Path(__file__).parent / "config.json"


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
    parser.add_argument("--platform", choices=["github", "gitlab"], default="github")
    parser.add_argument("--stream", action="store_true", default=True, help="Stream events (default)")
    parser.add_argument("--poll", action="store_true", help="Poll instead of streaming")
    args = parser.parse_args()

    config = load_config()
    client = Anthropic()

    # Create session
    session_kwargs = {
        "agent": config["orchestrator"]["id"],
        "environment_id": config["environment_id"],
        "title": f"Review PR #{args.pr_number} on {args.repo}",
    }
    if config.get("vault_id"):
        session_kwargs["vault_ids"] = [config["vault_id"]]

    print(f"Creating session for PR #{args.pr_number} on {args.repo}...")
    session = client.beta.sessions.create(**session_kwargs)
    print(f"Session: {session.id}")

    # Build the review task message
    task = (
        f"Review PR #{args.pr_number} on {args.repo}.\n"
        f"REPO={args.repo}\n"
        f"PR_NUMBER={args.pr_number}\n"
        f"PLATFORM={args.platform}\n"
        f"MODE={args.mode}\n\n"
        f"Execute the full review pipeline. Post the review as a PR comment. "
        f"Push learned patterns to the wiki."
    )

    if args.poll:
        run_poll(client, session.id, task)
    else:
        run_stream(client, session.id, task)


def run_stream(client: Anthropic, session_id: str, task: str):
    """Send the task, then stream events in real-time."""
    print("Sending review task...")

    # Send the user message first
    client.beta.sessions.events.send(
        session_id,
        events=[{
            "type": "user.message",
            "content": [{"type": "text", "text": task}],
        }],
    )

    print("Streaming review...\n")

    # Then subscribe to the event stream
    with client.beta.sessions.events.stream(session_id) as stream:
        for event in stream:
            etype = event.type if hasattr(event, "type") else str(type(event))

            if etype == "agent.message":
                for block in event.content:
                    if hasattr(block, "text"):
                        print(block.text, end="", flush=True)
            elif etype == "agent.tool_use":
                tool_name = getattr(getattr(event, "tool", None), "name", "unknown")
                print(f"\n  [tool] {tool_name}", flush=True)
            elif etype == "agent.thinking":
                pass  # silent
            elif etype == "session.idle":
                print("\n\nReview complete.")
                break
            elif etype == "session.error":
                print(f"\n\nSession error: {event}", file=sys.stderr)
                sys.exit(1)
            elif etype == "session.thread_created":
                print(f"\n  [sub-agent spawned]", flush=True)
            elif etype == "session.thread_idle":
                print(f"\n  [sub-agent finished]", flush=True)


def run_poll(client: Anthropic, session_id: str, task: str):
    """Send the task and poll for completion."""
    import time

    # Send the review task
    client.beta.sessions.events.send(
        session_id,
        events=[{
            "type": "user.message",
            "content": [{"type": "text", "text": task}],
        }],
    )

    print("Review started. Polling for completion...")

    while True:
        session = client.beta.sessions.retrieve(session_id)
        status = session.status

        if status == "idle":
            print("Review complete.")
            # Fetch final events
            events = client.beta.sessions.events.list(session_id)
            for event in events.data:
                if event.type == "agent.message":
                    for block in event.content:
                        if hasattr(block, "text"):
                            print(block.text)
            break
        elif status == "terminated":
            print(f"Session terminated: {session}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"  Status: {status}...", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
