#!/usr/bin/env python3
"""
PreToolUse hook: runs the repo's `.air-checks.sh` before `git commit`.

Contract:
- Stdin: JSON with {session_id, tool_name, tool_input: {command}}
- Exit 0: allow the tool call
- Exit 2: block the tool call (stderr is shown to Claude)

Behavior:
- Fires only when tool_name == "Bash" AND command starts with `git commit`
  (not `git commit-tree`, `git commit-graph`, etc. — only the commit verb)
- Does nothing unless `.air-checks.sh` exists at the repo root (opt-in)
- Runs the script from the repo root; if it exits non-zero, the commit
  is blocked and the script's output is shown

Skip with --no-verify on the git commit call if needed.
"""

import json
import os
import re
import subprocess
import sys


GIT_COMMIT_RE = re.compile(r"(?:^|[\s;&|])git\s+commit(?:\s|$|-m|--)")


def main():
    try:
        data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    cmd = (data.get("tool_input") or {}).get("command", "")
    if not GIT_COMMIT_RE.search(cmd):
        sys.exit(0)

    # If the user is intentionally bypassing hooks, honor that.
    if "--no-verify" in cmd:
        sys.exit(0)

    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit(0)

    script = os.path.join(repo_root, ".air-checks.sh")
    if not os.path.isfile(script):
        sys.exit(0)

    if not os.access(script, os.X_OK):
        print(
            f"air drift-check: {script} exists but is not executable. "
            f"Run `chmod +x {script}` or delete it.",
            file=sys.stderr,
        )
        sys.exit(2)

    result = subprocess.run(
        [script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=25,
    )

    if result.returncode == 0:
        sys.exit(0)

    # Blocked. Surface the script's output so Claude sees what failed.
    print("air drift-check blocked the commit:", file=sys.stderr)
    print("", file=sys.stderr)
    if result.stdout:
        print(result.stdout.rstrip(), file=sys.stderr)
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Fix the issues above, or pass --no-verify to bypass this check intentionally.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
