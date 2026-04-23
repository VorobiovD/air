#!/usr/bin/env python3
"""
PreToolUse hook: runs drift checks before `git commit`.

Contract:
- Stdin: JSON with {session_id, tool_name, tool_input: {command}}
- Exit 0: allow the tool call
- Exit 2: block the tool call (stderr is shown to Claude)

Behavior:
- Fires only when tool_name == "Bash" AND command starts with `git commit`
  (not `git commit-tree`/`git commit-graph`)
- `--no-verify` on the commit bypasses the check entirely
- Lookup order for what to run:
    1. If `.air-checks.sh` at repo root is executable → run only that
    2. Else if `.air-checks.sh` exists but NOT executable → print nudge,
       run built-ins (give the user zero-config protection even while their
       custom script is half-installed)
    3. Else → run built-ins
- Built-ins live at `$AIR_PLUGIN_ROOT/hooks/builtin-checks.sh`. The hook
  exports `AIR_PLUGIN_ROOT` so user scripts can delegate to built-ins via:
      "$AIR_PLUGIN_ROOT/hooks/builtin-checks.sh" || status=1
"""

import json
import os
import re
import subprocess
import sys


GIT_COMMIT_RE = re.compile(r"(?:^|[\s;&|])git\s+commit(?:\s|$|-m|--)")

# AIR_PLUGIN_ROOT is the plugins/air/ directory (parent of hooks/ which
# contains this script).
AIR_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILTIN_CHECKS = os.path.join(AIR_PLUGIN_ROOT, "hooks", "builtin-checks.sh")


def run_script(path, cwd):
    """Run a shell script; return (rc, stdout, stderr). Timeout at 25s."""
    env = os.environ.copy()
    env["AIR_PLUGIN_ROOT"] = AIR_PLUGIN_ROOT
    try:
        result = subprocess.run(
            ["/bin/bash", path],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
        )
    except subprocess.TimeoutExpired:
        return 1, "", f"air drift-check: {path} timed out after 25s"
    return result.returncode, result.stdout, result.stderr


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

    if "--no-verify" in cmd:
        sys.exit(0)

    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit(0)

    custom = os.path.join(repo_root, ".air-checks.sh")
    custom_exists = os.path.isfile(custom)
    custom_executable = custom_exists and os.access(custom, os.X_OK)

    preamble = []  # non-blocking messages to surface alongside any failure
    if custom_exists and not custom_executable:
        preamble.append(
            f"air drift-check: {custom} is present but not executable. "
            f"`chmod +x .air-checks.sh` to enable it. "
            f"Running built-in auto-detection in the meantime."
        )

    if custom_executable:
        rc, out, err = run_script(custom, cwd=repo_root)
        source = ".air-checks.sh"
    else:
        if not os.path.isfile(BUILTIN_CHECKS):
            # Plugin files missing — silently allow (don't break commits if
            # the plugin is half-installed).
            sys.exit(0)
        rc, out, err = run_script(BUILTIN_CHECKS, cwd=repo_root)
        source = "built-in auto-detection"

    if rc == 0:
        # All clear. Surface the preamble if any (non-blocking info).
        if preamble:
            for msg in preamble:
                print(msg, file=sys.stderr)
        sys.exit(0)

    # Drift detected. Block with combined output.
    for msg in preamble:
        print(msg, file=sys.stderr)
    print(f"air drift-check blocked the commit ({source}):", file=sys.stderr)
    print("", file=sys.stderr)
    if out:
        print(out.rstrip(), file=sys.stderr)
    if err:
        print(err.rstrip(), file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Fix the issues above, or pass --no-verify to bypass this check intentionally.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
