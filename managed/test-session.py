#!/usr/bin/env python3
"""
Quick test: creates a minimal session that verifies all operations:
- Repo mounted and accessible
- gh CLI authenticated
- Can read files, run git blame, git log
- Can post a PR comment
- Can clone and push wiki
Then cleans up the test comment.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    export AIR_BOT_TOKEN=ghp_...
    python test-session.py VorobiovD/air 14
"""

import json
import os
import sys
import time

import requests as req

API_BASE = "https://api.anthropic.com/v1"
HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "managed-agents-2026-04-01",
    "content-type": "application/json",
}


def get_headers():
    return {**HEADERS, "x-api-key": os.environ["ANTHROPIC_API_KEY"]}


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else "VorobiovD/air"
    pr = sys.argv[2] if len(sys.argv) > 2 else "14"
    bot_token = os.environ.get("AIR_BOT_TOKEN", "")

    if not bot_token:
        print("Error: AIR_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    # Get PR branch
    print(f"[1] Fetching PR #{pr} branch...")
    resp = req.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr}",
        headers={"Authorization": f"Bearer {bot_token}"},
    )
    branch = resp.json()["head"]["ref"]
    print(f"  Branch: {branch}")

    # Find or create a simple test agent
    print("[2] Finding test agent...")
    resp = req.get(f"{API_BASE}/agents", headers=get_headers())
    test_agent = None
    for a in resp.json().get("data", []):
        if a["name"] == "air-test" and not a.get("archived_at"):
            test_agent = a
            break

    if not test_agent:
        print("  Creating test agent...")
        resp = req.post(f"{API_BASE}/agents", headers=get_headers(), json={
            "name": "air-test",
            "model": "claude-sonnet-4-6",
            "system": "You are a test agent. Execute the commands the user gives you. Report results concisely.",
            "tools": [{"type": "agent_toolset_20260401"}],
        })
        test_agent = resp.json()
        print(f"  Created: {test_agent['id']}")
    else:
        print(f"  Found: {test_agent['id']}")

    # Find environment
    resp = req.get(f"{API_BASE}/environments", headers=get_headers())
    env_id = None
    for e in resp.json().get("data", []):
        if e["name"] == "air-review-env" and not e.get("archived_at"):
            env_id = e["id"]
            break
    if not env_id:
        print("Error: no environment found. Run setup.py first.", file=sys.stderr)
        sys.exit(1)

    # Create session with repo mounted
    print("[3] Creating session...")
    from anthropic import Anthropic
    client = Anthropic()

    session = client.beta.sessions.create(
        agent=test_agent["id"],
        environment_id=env_id,
        title="air test session",
        resources=[{
            "type": "github_repository",
            "url": f"https://github.com/{repo}",
            "authorization_token": bot_token,
            "checkout": {"type": "branch", "name": branch},
            "mount_path": "/workspace/repo",
        }],
    )
    print(f"  Session: {session.id}")

    # Send test commands
    print("[4] Running tests...\n")
    task = f"""Run these commands one by one and report PASS/FAIL for each:

1. REPO ACCESS: `ls /workspace/repo/CLAUDE.md && echo PASS || echo FAIL`
2. GH AUTH: `export GH_TOKEN="{bot_token}" && gh auth status 2>&1 && echo PASS || echo FAIL`
3. GH PR VIEW: `export GH_TOKEN="{bot_token}" && gh pr view {pr} --repo {repo} --json title --jq .title && echo PASS || echo FAIL`
4. GIT BLAME: `cd /workspace/repo && git blame CLAUDE.md | head -3 && echo PASS || echo FAIL`
5. GIT LOG: `cd /workspace/repo && git log --oneline -3 && echo PASS || echo FAIL`
6. POST COMMENT: `export GH_TOKEN="{bot_token}" && gh pr comment {pr} --repo {repo} --body "air test — this comment will be deleted" && echo PASS || echo FAIL`
7. WIKI CLONE: `git clone https://x-access-token:{bot_token}@github.com/{repo}.wiki.git /workspace/wiki 2>&1 && echo PASS || echo FAIL`
8. WIKI PUSH: `cd /workspace/wiki && echo "test" >> .air-test && git add .air-test && git -c commit.gpgsign=false -c user.name="air-machine" -c user.email="air@test" commit -m "test" && git push 2>&1 && echo PASS || echo FAIL`
9. WIKI CLEANUP: `cd /workspace/wiki && git rm .air-test && git -c commit.gpgsign=false -c user.name="air-machine" -c user.email="air@test" commit -m "cleanup" && git push 2>&1 && echo PASS || echo FAIL`

After all tests, print a summary table:
| Test | Result |
|---|---|
| ... | PASS/FAIL |

Then delete the test comment from step 6:
`export GH_TOKEN="{bot_token}" && gh api repos/{repo}/issues/{pr}/comments --jq '.[-1].id' | xargs -I {{}} gh api repos/{repo}/issues/comments/{{}} -X DELETE`
"""

    client.beta.sessions.events.send(
        session.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": task}]}],
    )

    # Stream results
    with client.beta.sessions.events.stream(session.id) as stream:
        for event in stream:
            t = event.type if hasattr(event, "type") else ""
            if t == "agent.message":
                for block in event.content:
                    if hasattr(block, "text"):
                        print(block.text, end="", flush=True)
            elif t == "agent.tool_use":
                name = getattr(event, "name", "?")
                print(f"\n  [tool] {name}", flush=True)
            elif t == "session.status_idle":
                print("\n\nDone.")
                break
            elif t == "session.error":
                err = getattr(event, "error", {})
                print(f"\n  [error: {err}]", flush=True)


if __name__ == "__main__":
    main()
