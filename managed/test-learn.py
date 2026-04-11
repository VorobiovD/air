#!/usr/bin/env python3
"""
Test wiki learn cycle: clone wiki, update REVIEW.md, push.
Verifies the auth fix for wiki push.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    export AIR_BOT_TOKEN=ghp_...
    python test-learn.py VorobiovD/air
"""

import os
import sys

from api import list_agents, find_environment


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else "VorobiovD/air"
    bot_token = os.environ.get("AIR_BOT_TOKEN", "")

    if not bot_token:
        print("Error: AIR_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    # Find test agent
    agents = list_agents()
    test_agent = agents.get("air-test")
    env_id = find_environment()

    if not test_agent or not env_id:
        print("Error: run test-session.py first to create test agent + environment", file=sys.stderr)
        sys.exit(1)

    print(f"[1] Creating learn session for {repo}...")
    from anthropic import Anthropic
    client = Anthropic()

    session = client.beta.sessions.create(
        agent=test_agent["id"],
        environment_id=env_id,
        title=f"Learn test — {repo}",
        resources=[{
            "type": "github_repository",
            "url": f"https://github.com/{repo}",
            "authorization_token": bot_token,
            "checkout": {"type": "branch", "name": "main"},
            "mount_path": "/workspace/repo",
        }],
    )
    print(f"  Session: {session.id}")

    task = f"""Run these commands to test the wiki learn cycle:

1. Set auth:
```bash
export GH_TOKEN="{bot_token}"
```

2. Clone wiki with auth:
```bash
git clone --depth 1 "https://x-access-token:$GH_TOKEN@github.com/{repo}.wiki.git" /workspace/wiki 2>&1
ls /workspace/wiki/*.md
```

3. Read current REVIEW.md:
```bash
cat /workspace/wiki/REVIEW.md | head -20
```

4. Add a test line and push:
```bash
cd /workspace/wiki
echo "" >> REVIEW.md
echo "## Test Entry" >> REVIEW.md
echo "- Test pattern from managed agent learn cycle ($(date +%Y-%m-%d %H:%M))" >> REVIEW.md
git add REVIEW.md
git -c user.name="air-machine" -c user.email="air@bot" -c commit.gpgsign=false commit -m "learn: test wiki push from managed agent"
git push 2>&1
echo "WIKI PUSH RESULT: $?"
```

5. Revert the test:
```bash
cd /workspace/wiki
git revert --no-edit HEAD
git push 2>&1
echo "REVERT RESULT: $?"
```

Report PASS/FAIL for each step.
"""

    print("[2] Running learn test...\n")
    client.beta.sessions.events.send(
        session.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": task}]}],
    )

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
                print(f"\n  [error — continuing]", flush=True)


if __name__ == "__main__":
    main()
