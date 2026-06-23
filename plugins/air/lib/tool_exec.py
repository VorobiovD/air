"""Read-only sandboxed tool executor for the headless (messages-api) review mode.

In the headless mode air owns the agent loop CLIENT-SIDE (see agent_loop.py), so
it must execute the specialists'/verifier's tools itself instead of leaning on the
managed runtime's hosted sandbox. The PR diff is UNTRUSTED (prompt-injection), so
every tool call is validated + path-jailed HERE before it touches the filesystem.

Trust model: the specialists declare `tools: Read, Grep, Glob, Bash` where "Bash is
ONLY for git log / git blame". Review is a READ-ONLY workload — nothing legitimately
writes the checkout, installs packages, or makes network calls. So:

  * Read / Grep / Glob are pure-Python (no shell, no subprocess).
  * Bash is a git read-only dispatcher: shell=False, a frozen verb allowlist, no
    global options before the verb, a deny-flag screen, refspec/path deny-globs,
    and a hardened env (no pager/alias/user-config exec vectors).

Because no write/exec/network verb exists, a malicious diff has no command to
induce — it can only emit jailed read calls. This is STRICTER than the CLI's
general `Bash` and managed's broad sandbox. Residual (same as every air mode
today): a read tool can read a secret that is IN the checkout — mitigated by
reviewing clean clones + the deny-globs below (a blocklist = defense-in-depth,
NOT the primary control; the read-only design is). The deny-glob is PATH-based,
so it screens path/refspec/pathspec reads (literal, magic, and wildcard forms);
it deliberately does NOT try to catch a content read addressed by raw object SHA
(`git show <blob-sha>` / `cat-file -p <sha>`, SHA enumerable via `ls-files -s`) —
that's unscreenable without crippling git, and is an accepted defense-in-depth
gap whose backstop is the clean-clone assumption (no committed secrets).

stdlib-only (the lib/ rule). Secrets (ANTHROPIC_API_KEY / bot PAT / ssh) live in
the orchestrator and are NEVER placed in the git subprocess env (narrow env) —
the same discipline review.py already applies to Codex.
"""
import fnmatch
import os
import re
import subprocess
from pathlib import Path

# Read-only git verbs the verifier/specialists legitimately need. Frozen.
GIT_VERB_ALLOWLIST = frozenset({
    "blame", "log", "show", "diff", "cat-file", "rev-parse", "ls-files", "status",
})

# Flags that can write a file, exec, or reach the network even under a read verb.
# Rejected ANYWHERE in argv (matched whole-token, incl. the `--flag=value` form).
_GIT_DENY_FLAG_RE = re.compile(
    r"^(--output(=.*)?|-[oO].*|--exec-path(=.*)?|--upload-pack(=.*)?|--receive-pack(=.*)?"
    r"|--git-dir(=.*)?|--work-tree(=.*)?|--namespace(=.*)?|--open-files-in-pager(=.*)?"
    # --no-index turns `git diff` into a general two-path file differ that reads
    # ARBITRARY filesystem paths (e.g. /proc/<ppid>/environ → the bot PAT + API
    # key), bypassing the realpath jail entirely. Without it git is repo-confined.
    r"|--no-index)$"
)

# Default-DENY flag allowlist for the read-only git verbs. Blocklisting known-bad
# flags (above) repeatedly lost to unenumerated siblings — `--no-index` was blocked
# but `git blame --contents=<file>` (reads ANY filesystem path) was not, and the
# path screens all sat behind `if not tok.startswith("-")`, so a `-`-prefixed
# path-valued flag skipped every check. So a `-`-prefixed git arg must now be an
# EXACT safe flag, a safe value-glued prefix, or a numeric `-<N>`; everything else —
# path-valued (`--contents`/`--no-index`/`--output`/blame `-S<revs-file>`/`--anchored`),
# config-exec (`--textconv`/`--ext-diff`), or object-enumerating (`cat-file --batch*`) —
# is refused. The legitimate read-only surface (blame/log/show/diff/cat-file/rev-parse/
# ls-files/status with formatting/range flags) is small and fully covered here.
_SAFE_GIT_FLAGS = frozenset({
    "-p", "--patch", "-s", "--no-patch", "--stat", "--shortstat", "--numstat",
    "--summary", "--name-only", "--name-status", "--oneline", "--no-color",
    "--graph", "--decorate", "--no-decorate", "--reverse", "--first-parent",
    "--follow", "-w", "--ignore-all-space", "--ignore-space-change", "--function-context",
    "-M", "-C", "--find-renames", "--find-copies", "--line-porcelain", "--porcelain",
    "--cached", "--staged", "--others", "-t", "-e", "--short", "--verify",
    "--abbrev-ref", "--branch", "--all", "-z",
})
_SAFE_GIT_FLAG_PREFIXES = (
    "--format=", "--pretty=", "--pretty", "--abbrev=", "--abbrev", "--color=",
    "--date=", "--max-count=", "--skip=", "--since=", "--until=", "--after=",
    "--before=", "--unified=", "-U", "-L", "-n", "--decorate=", "--porcelain=",
)
_NUMERIC_FLAG_RE = re.compile(r"^-\d+$")   # `git log -5`


def _git_flag_allowed(tok: str) -> bool:
    # -L<start>,<end> is a safe line range, BUT git's -L<start>,<end>:<file> and
    # -L:<funcname>:<file> embed a FILE PATH inside the flag value — and a flag's
    # value never reaches the path/deny-glob screens, so `-L1,1:.env` reads the
    # secret. Refuse the path-bearing (colon) form; the normal review usage passes
    # the file as a SEPARATE arg (`git blame -L 1,5 <file>`), which has no colon.
    if tok.startswith("-L"):
        return ":" not in tok
    return (tok in _SAFE_GIT_FLAGS
            or any(tok.startswith(p) for p in _SAFE_GIT_FLAG_PREFIXES)
            or bool(_NUMERIC_FLAG_RE.match(tok)))

# Sensitive basenames/paths a read tool must refuse even though they're in-tree.
# Blocklist (fails open on the unenumerated) — defense-in-depth, not the control.
DENY_GLOBS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "*_rsa", "*_key",
    "*credential*", "*secret*", "*.keystore", ".npmrc", ".pypirc", ".netrc",
    # .git internals — actions/checkout persists the bot PAT into .git/config
    # (extraheader); .git/ also holds hooks/refs/etc a review never reads. fnmatch
    # `*` crosses `/`, so `.git/*` matches every file under .git at any depth, and
    # `*/.git/*` covers a nested/submodule .git. `.gitignore`/`.gitattributes` are
    # NOT matched (no slash after `.git`), so reviewers still read those.
    ".git/*", "*/.git/*",
)


def _deny_glob_match(name: str, rel: str) -> bool:
    """Case-INSENSITIVE deny-glob match (basename AND full rel path). fnmatch's normcase
    is identity on macOS/Linux, so a plain fnmatch is case-SENSITIVE — but a case-
    insensitive checkout FS (macOS/Windows) opens `.ENV` as `.env` while the requested
    spelling `.ENV` sails past a case-sensitive deny-glob. Fold both sides (DENY_GLOBS are
    already lowercase) so a case-variant can't read .env/*.pem/*secret*. May over-refuse a
    legitimately-uppercase name on a case-sensitive FS — safe (a lost read, never a leak)."""
    nl, rl = name.lower(), rel.lower()
    return any(fnmatch.fnmatch(nl, g) or fnmatch.fnmatch(rl, g) for g in DENY_GLOBS)

# Hardened env for the git subprocess: no system/global/user config (kills alias
# + core.pager exec vectors), no pager, no network prompts. Plus a minimal PATH.
_GIT_HARDENED_ENV = {
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "HOME": "/nonexistent",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ALLOW_PROTOCOL": "file",  # local object DB only; no remote fetch protocols
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "LC_ALL": "C",
}

_MAX_OUTPUT = 60_000   # cap any single tool result (chars) — bounds a hostile read
_GIT_TIMEOUT = 30


class ToolError(Exception):
    """A tool call was refused or failed — surfaced to the model as an error
    tool_result so it adapts, never crashing the loop."""


def _split_refspec(arg: str) -> str | None:
    """`<ref>:<path>` (git show/cat-file) → the path component, else None.
    Lets deny-globs screen `git show HEAD:.env`-style in-tree reads."""
    # A bare path has no colon; a windows drive (C:) is not a refspec here.
    if ":" in arg and not arg.startswith("-"):
        ref, _, path = arg.partition(":")
        if path:
            return path
    return None


class Sandbox:
    """Read-only tool execution jailed to a single checkout root."""

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ToolError(f"checkout root is not a directory: {root}")

    # ---- path jail -------------------------------------------------------
    def _deny_glob_check(self, rel: str, name: str) -> None:
        if _deny_glob_match(name, rel):
            raise ToolError("refused: sensitive path matches a deny-glob")

    def _jail(self, p: str) -> Path:
        """Resolve `p` (relative to root, or absolute) and assert it stays inside
        root after symlink resolution; then deny-glob screen. Blocks `../`,
        absolute escapes, and symlink escapes."""
        cand = (Path(p) if os.path.isabs(p) else self.root / p).resolve()
        try:
            rel = cand.relative_to(self.root)
        except ValueError:
            raise ToolError(f"refused: path outside the checkout ({p})")
        self._deny_glob_check(str(rel), cand.name)
        return cand

    def _screen_refspec_path(self, arg: str) -> None:
        """Deny-glob the path side of a `<ref>:<path>` git arg (object-DB read,
        so realpath-jail doesn't apply — but the basename screen still must)."""
        path = _split_refspec(arg)
        if path is not None:
            base = os.path.basename(path)
            self._deny_glob_check(path, base)

    # ---- tools -----------------------------------------------------------
    def read(self, file_path: str, offset: int | None = None, limit: int | None = None) -> str:
        f = self._jail(file_path)
        if not f.is_file():
            raise ToolError(f"not a file: {file_path}")
        lines = f.read_text(errors="replace").splitlines()
        start = max(0, (offset or 1) - 1)
        end = start + limit if limit else len(lines)
        out = "\n".join(f"{i+1}\t{ln}" for i, ln in enumerate(lines[start:end], start=start))
        return out[:_MAX_OUTPUT]

    def glob(self, pattern: str, path: str | None = None) -> str:
        base = self._jail(path) if path else self.root
        if not base.is_dir():
            raise ToolError(f"not a directory: {path}")
        hits = []
        for hit in sorted(base.glob(pattern)):
            try:
                rel = hit.resolve().relative_to(self.root)
            except ValueError:
                continue  # globbed outside the jail (symlink) — skip
            if _deny_glob_match(hit.name, str(rel)):
                continue  # don't surface deny-globbed paths (parity with read/grep)
            hits.append(str(rel))
        return ("\n".join(hits) or "(no matches)")[:_MAX_OUTPUT]

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None,
             ignore_case: bool = False) -> str:
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            raise ToolError(f"bad regex: {e}")
        base = self._jail(path) if path else self.root
        roots = [base] if base.is_file() else sorted(base.rglob(glob or "*"))
        out = []
        for f in roots:
            if not f.is_file():
                continue
            try:
                rel = f.resolve().relative_to(self.root)
            except ValueError:
                continue
            # Check the full relative path, not just the basename — fnmatch lets `*`
            # cross `/`, so `*secret*` matches `secrets/token.txt` whose basename
            # ("token.txt") is innocuous. Without the rel check grep could read a file
            # under a deny-globbed DIRECTORY that read()/glob() (which check rel) refuse.
            if _deny_glob_match(f.name, str(rel)):
                continue
            try:
                for n, ln in enumerate(f.read_text(errors="replace").splitlines(), 1):
                    if rx.search(ln):
                        out.append(f"{f.relative_to(self.root)}:{n}:{ln}")
                        if len("\n".join(out)) > _MAX_OUTPUT:
                            return "\n".join(out)[:_MAX_OUTPUT] + "\n[truncated]"
            except OSError:
                continue
        return ("\n".join(out) or "(no matches)")[:_MAX_OUTPUT]

    def bash(self, command: str) -> str:
        """git read-only dispatcher. shell=False — the command is shlex-split and
        executed as an argv with NO shell, so metacharacters are inert."""
        import shlex
        try:
            argv = shlex.split(command)
        except ValueError as e:
            raise ToolError(f"unparseable command: {e}")
        if not argv or argv[0] != "git":
            raise ToolError("Bash is restricted to read-only `git` commands (blame/log/show/diff/...)")
        # No global options before the verb (`git -c core.pager=… …`, `git -C …`).
        if len(argv) >= 2 and argv[1].startswith("-"):
            raise ToolError(f"git global option not allowed before the verb: {argv[1]}")
        if len(argv) < 2:
            raise ToolError("no git verb given")
        verb = argv[1]
        if verb not in GIT_VERB_ALLOWLIST:
            raise ToolError(f"git verb not allowed (read-only allowlist): {verb}")
        for tok in argv[2:]:
            if tok == "--":
                continue  # pathspec separator — neither a flag nor a path
            if tok.startswith("-"):
                # FLAG: default-deny (see _git_flag_allowed). The deny-regex is kept as
                # an explicit, documented blocklist of the worst flags; the allowlist is
                # the backstop that closes unenumerated path-valued/exec siblings.
                if _GIT_DENY_FLAG_RE.match(tok) or not _git_flag_allowed(tok):
                    raise ToolError(f"git flag not allowed (read-only allowlist): {tok}")
                continue
            # NON-flag token: a ref, commit range, or pathspec. Screen all path shapes.
            # Leading ':' magic (`:(glob).e??` / `:.env` staged) defeats literal-text
            # deny-globbing; refuse the whole class (plain `ref:path` colon-in-middle ok).
            if tok.startswith(":"):
                raise ToolError(f"git pathspec/refspec magic (leading ':') not allowed: {tok}")
            # Path traversal (`../x`, `a/../../etc`) escapes the checkout; absolute paths
            # name files outside it. Commit ranges (`A..B`, `A...B`) never contain '/..'.
            if tok.startswith("../") or "/.." in tok:
                raise ToolError(f"path traversal not allowed in a git arg: {tok}")
            if tok.startswith("/"):
                raise ToolError(f"absolute path not allowed in a git arg: {tok}")
            self._screen_refspec_path(tok)  # `git show HEAD:.env` → refused
            # A wildcard pathspec (`.e??`, `*.key`) expands git-side to match files the
            # literal token never equals, so the deny-glob below can't see what it reads.
            if any(c in tok for c in "*?["):
                raise ToolError(f"git wildcard pathspec not allowed: {tok}")
            # Plain literal pathspec (`git log -p -- .env`): refs/SHAs/ranges never match
            # a sensitive deny-glob, so this only ever refuses an actual deny-globbed path.
            self._deny_glob_check(tok, os.path.basename(tok))
        run_argv = ["git", "--no-pager", verb, *argv[2:]]
        try:
            proc = subprocess.run(
                run_argv, cwd=str(self.root), env=_GIT_HARDENED_ENV,
                shell=False, capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"git timed out after {_GIT_TIMEOUT}s")
        body = proc.stdout if proc.returncode == 0 else (proc.stdout + proc.stderr)
        # Output-scan (defense-in-depth): a path-LESS content dump (`git log -p`,
        # `git show <commit>`) carries no path token for the deny-glob to screen, so a
        # COMMITTED deny-globbed file's content would leak through the diff. Refuse the
        # output if any diff header names a deny-globbed path. Clean-clone repos (no such
        # committed file) are unaffected; this only ever fires on a real committed secret.
        for m in re.finditer(r"(?m)^diff --git a/(\S+) b/(\S+)", body or ""):
            for p in (m.group(1), m.group(2)):
                if _deny_glob_match(os.path.basename(p), p):
                    raise ToolError("refused: git output would expose a deny-globbed file's content")
        return (body or "(no output)")[:_MAX_OUTPUT]

    # ---- dispatch --------------------------------------------------------
    def dispatch(self, name: str, tool_input: dict) -> tuple[str, bool]:
        """Route a model tool_use to the jailed implementation. Returns
        (result_text, is_error); never raises into the loop."""
        try:
            if name == "Read":
                return self.read(tool_input["file_path"], tool_input.get("offset"), tool_input.get("limit")), False
            if name == "Glob":
                return self.glob(tool_input["pattern"], tool_input.get("path")), False
            if name == "Grep":
                return self.grep(tool_input["pattern"], tool_input.get("path"),
                                 tool_input.get("glob"), bool(tool_input.get("ignore_case"))), False
            if name == "Bash":
                return self.bash(tool_input["command"]), False
            return f"[tool refused] unknown tool: {name}", True
        except KeyError as e:
            return f"[tool refused] missing required arg: {e}", True
        except ToolError as e:
            return f"[tool refused] {e}", True
        except Exception as e:  # never crash the loop on a tool fault
            return f"[tool error] {type(e).__name__}: {e}", True


# Anthropic tool-schema definitions matching the agents' declared tools
# (Read/Grep/Glob/Bash). Passed to messages.create(tools=...) by agent_loop.py.
TOOL_SCHEMAS = [
    {"name": "Read", "description": "Read a file from the checkout (read-only).",
     "input_schema": {"type": "object", "properties": {
         "file_path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
         "required": ["file_path"]}},
    {"name": "Grep", "description": "Regex search files in the checkout (read-only).",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "path": {"type": "string"},
         "glob": {"type": "string"}, "ignore_case": {"type": "boolean"}},
         "required": ["pattern"]}},
    {"name": "Glob", "description": "Glob for file paths in the checkout (read-only).",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "path": {"type": "string"}},
         "required": ["pattern"]}},
    {"name": "Bash", "description": "Run a READ-ONLY git command only (git blame/log/show/diff/...). No other commands.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
         "required": ["command"]}},
]
