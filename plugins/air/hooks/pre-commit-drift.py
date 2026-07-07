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
import shlex
import subprocess
import sys

# AIR_PLUGIN_ROOT is the plugins/air/ directory (parent of hooks/ which
# contains this script).
AIR_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILTIN_CHECKS = os.path.join(AIR_PLUGIN_ROOT, "hooks", "builtin-checks.sh")


# Environment passed to drift-check scripts. DENY-BY-DEFAULT: neither the
# built-ins nor a repo-provided `.air-checks.sh` may see the session's secrets
# (ANTHROPIC_API_KEY, *_PAT, AIR_BOT_TOKEN, OPENAI_API_KEY, …). We forward only
# the infra vars git/python3/grep/sed/find need, plus AIR_PLUGIN_ROOT. This is
# the security fix for the "a cloned hostile repo exfiltrates secrets on the
# first commit" class: even if an untrusted script somehow runs, its
# environment holds nothing worth stealing. Paired with `_custom_trusted`
# below (which stops an untrusted custom script from running at all), and with
# the fact that the built-ins are air's own code shipped inside the plugin.
_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "TMPDIR",
    "TEMP", "TMP", "TERM", "TERMINFO", "TZ", "LANG", "LANGUAGE", "LC_ALL",
    "LC_CTYPE", "LC_MESSAGES", "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_EXEC_PATH",
    "SYSTEMROOT", "PATHEXT",  # Windows: git/python need these
)


def _build_script_env():
    """A minimal, secret-free environment for drift-check scripts."""
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    # Any other locale var the allowlist didn't name (LC_* / LC_NUMERIC / …).
    for k, v in os.environ.items():
        if k.startswith("LC_"):
            env[k] = v
    env["AIR_PLUGIN_ROOT"] = AIR_PLUGIN_ROOT
    return env


def run_script(path, cwd):
    """Run a shell script; return (rc, stdout, stderr). Timeout at 25s."""
    env = _build_script_env()
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


# Shell operators that separate sub-commands. shlex with `punctuation_chars`
# returns each operator as its own token even when tight-packed like `a;b`.
_SUBCOMMAND_SEPARATORS = {";", "&&", "||", "|", "&", "\n"}
# Common command wrappers that precede the real command. `env VAR=x git commit`
# and `sudo git commit` should route to the same path as bare `git commit`.
_WRAPPERS = {"sudo", "nohup", "exec", "time", "env"}
# Two-token git meta-flags: `git --git-dir /foo commit` consumes the next arg.
_GIT_TWO_TOKEN_FLAGS = {"--git-dir", "--work-tree", "--namespace", "-C", "-c"}


def _tokenize(cmd: str) -> list[str]:
    """shlex-tokenize, respecting quotes AND producing operator tokens for `;`, `&&`, `||`, `|`, `&` even tight-packed."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars="|&;<>")
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return cmd.split()


def _split_subcommands(cmd: str) -> list[list[str]]:
    """Split a Bash command into argv lists per sub-command."""
    subs: list[list[str]] = []
    current: list[str] = []
    for t in _tokenize(cmd):
        if t in _SUBCOMMAND_SEPARATORS:
            if current:
                subs.append(current)
                current = []
        else:
            current.append(t)
    if current:
        subs.append(current)
    return subs


def _argv_is_git_commit(argv: list[str]) -> bool:
    """True if argv invokes `git commit` (not commit-tree/commit-graph)."""
    if not argv:
        return False
    i = 0
    # Strip env-var prefix assignments like `GIT_AUTHOR_NAME=x git commit`.
    while i < len(argv) and "=" in argv[i] and not argv[i].startswith("-") and "/" not in argv[i].split("=", 1)[0]:
        i += 1
    # Strip common wrappers: `sudo git commit`, `env git commit`, `nohup git commit`, etc.
    while i < len(argv) and argv[i] in _WRAPPERS:
        i += 1
        # env-style VAR=val assignments after `env`.
        while i < len(argv) and "=" in argv[i] and not argv[i].startswith("-"):
            i += 1
    if i >= len(argv):
        return False
    leader = argv[i]
    if not (leader == "git" or leader.endswith("/git")):
        return False
    # Scan past git's own flags to find the subcommand.
    j = i + 1
    while j < len(argv):
        tok = argv[j]
        # Single-token forms: `--git-dir=/foo`, `-c key=val`, generic `-X`.
        if any(tok.startswith(f + "=") for f in ("--git-dir", "--work-tree", "--namespace")):
            j += 1
            continue
        # Two-token forms: `--git-dir /foo`, `-C /path`, etc.
        if tok in _GIT_TWO_TOKEN_FLAGS:
            j += 2
            continue
        if tok.startswith("-"):
            j += 1
            continue
        return tok == "commit"
    return False


def _is_git_commit(cmd: str) -> bool:
    """Return True if any sub-command in `cmd` runs `git commit`."""
    return any(_argv_is_git_commit(argv) for argv in _split_subcommands(cmd))


def _has_no_verify(cmd: str) -> bool:
    """Return True if `--no-verify` appears as a bare argv token in any sub-command
    that runs `git commit` (not inside a quoted message body)."""
    for argv in _split_subcommands(cmd):
        if _argv_is_git_commit(argv) and "--no-verify" in argv:
            return True
    return False


def _absolute_git_dir(repo_root):
    """Absolute path to this repo's git dir, or None."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _custom_trusted(repo_root):
    """Whether a repo-provided `.air-checks.sh` is trusted to execute.

    The executable bit is NOT trust — git preserves it across clones, so a
    hostile repo can ship `.air-checks.sh` mode 755 and, without this gate,
    have it run (with the user's environment) on the first Claude-driven
    commit. Trust therefore requires an OUT-OF-BAND signal the repo's author
    cannot set:
      1. a marker file `<git-dir>/air-checks.trusted` — the git dir is not part
         of the cloned/pulled tree, so a hostile repo cannot inject it; the
         user creates it once, after reviewing the script; or
      2. the repo root listed in `$AIR_TRUSTED_CHECKS` (os.pathsep-separated
         absolute paths) — for non-interactive/allowlist setups.
    """
    allow = os.environ.get("AIR_TRUSTED_CHECKS", "")
    if allow:
        want = os.path.realpath(repo_root)
        for p in allow.split(os.pathsep):
            if p and os.path.realpath(p) == want:
                return True
    git_dir = _absolute_git_dir(repo_root)
    if git_dir and os.path.isfile(os.path.join(git_dir, "air-checks.trusted")):
        return True
    return False


def main():
    try:
        data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    cmd = (data.get("tool_input") or {}).get("command", "")
    if not _is_git_commit(cmd):
        sys.exit(0)

    if _has_no_verify(cmd):
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
    # Trust is separate from the executable bit (an attacker's repo can ship a
    # +x script): a repo-provided script runs only when explicitly trusted.
    custom_trusted = custom_executable and _custom_trusted(repo_root)

    preamble = []  # non-blocking messages to surface alongside any failure
    if custom_exists and not custom_executable:
        preamble.append(
            f"air drift-check: {custom} is present but not executable. "
            f"`chmod +x .air-checks.sh` to enable it (you'll also be prompted "
            f"to trust it). Running built-in auto-detection in the meantime."
        )
    elif custom_executable and not custom_trusted:
        preamble.append(
            f"air drift-check: {custom} is executable but NOT trusted for this "
            f"repo, so it was skipped — a repo-provided script is never run "
            f"automatically (it could contain anything). Review it, then run "
            f'`touch "$(git rev-parse --absolute-git-dir)/air-checks.trusted"` '
            f"to enable it (or set AIR_TRUSTED_CHECKS). Running built-in "
            f"auto-detection in the meantime."
        )

    if custom_trusted:
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
        # Built-ins passed. Emit any preamble to stderr for transcript visibility
        # (Ctrl-R). Per Claude Code PreToolUse contract, stderr on exit 0 is
        # surfaced in the transcript but NOT injected into Claude's context —
        # acceptable for a nudge-style message. Blocking would be too aggressive
        # since built-ins did pass cleanly.
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
