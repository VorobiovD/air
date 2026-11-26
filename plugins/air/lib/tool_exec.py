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
NOT the primary control; the read-only design is).

Git output contract (a stricter one for the headless sandbox):
  * patch output is available only with a limited set of formatting options;
  * protected paths are detected robustly in the OUTPUT itself (git_paths.py), not
    only in the arguments;
  * every operand of a content read is either a path that passed the path screens or
    an object whose type was verified as a non-blob; anything else is refused;
  * protected content is omitted from `git show`/`git diff` while the commit's own
    identity is kept; git's own commit selection is never re-implemented: a request
    that selects commits by walking history is passed to git unchanged, or refused
    when it would need filtering (with a pointer to one-commit-at-a-time `git show`);
    `git log` is never path-limited, so a log whose patch would show a protected
    path is refused as a whole;
  * a git result cut by the output cap says so, and a multi-commit `git show`
    reports how many of the requested commits it shows (Read and Grep results are
    still cut at the cap without a note);
  * every git subprocess of one tool call shares a single time budget.
An inert data-file classifier is staged alongside for a later change.

stdlib-only (the lib/ rule). Secrets (ANTHROPIC_API_KEY / bot PAT / ssh) live in
the orchestrator and are NEVER placed in the git subprocess env (narrow env) —
the same discipline review.py already applies to Codex.
"""
import fnmatch
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_paths  # noqa: E402

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

def _exclude_pathspecs() -> list[str]:
    """git exclude pathspecs mirroring DENY_GLOBS (case-insensitive, `:(glob)`
    semantics where `**/` also matches zero directories). `show`/`diff` patch
    output gets these appended when it would touch a protected path; the output
    scan remains the backstop."""
    specs: list[str] = []
    for g in DENY_GLOBS:
        if g.startswith("*/"):
            continue  # nested form of a rule already covered with its `**/` prefix
        if g.endswith("/*"):
            specs.append(f":(exclude,icase,glob)**/{g[:-2]}/**")
        else:
            specs.append(f":(exclude,icase,glob)**/{g}")
            specs.append(f":(exclude,icase,glob)**/{g}/**")  # a directory so named
    return specs


_PATCH_VERBS = frozenset({"show", "diff"})        # patch output is their default
# Flags that choose the OUTPUT FORMAT; dropped from the rename pre-pass so its
# `--name-status` output is the only format git prints.
_FORMAT_FLAG_RE = re.compile(
    r"^(-p|-u|-s|-z|--patch.*|-U\d*|--unified.*|--stat.*|--numstat|--shortstat|--summary"
    r"|--raw|--name-only|--name-status|--check|--dirstat.*|--compact-summary|--no-patch"
    r"|--format.*|--pretty.*|--oneline|--abbrev-commit|--graph|--color.*|--no-color)$")
# Commit-count flags: appending exclude pathspecs would change WHICH commit `-1` selects
# (history simplification), so these calls skip the git-side exclusion and rely on the
# output scan (+ the rename pre-pass is irrelevant: the scan sees every header).
_COUNT_FLAG_RE = re.compile(r"^(-\d+|-n\d*|--max-count(=.*)?)$")
_NAME_STATUS_RE = re.compile(r"^[ACDMRTUXB]\d*$")
_OBJECT_VERBS = frozenset({"cat-file", "show", "diff", "log"})
_RANGE_SPLIT_RE = re.compile(r"\.\.\.?")
# Flags that choose a NON-patch diff format (stat/name listings or no diff at all). A
# `show`/`diff` carrying one of these (and no explicit patch flag) prints no patch.
_DIFF_FORMAT_FLAG_RE = re.compile(
    r"^(-s|--no-patch|--stat(=.*)?|--shortstat|--numstat|--summary|--name-only|--name-status)$")
# Flags dropped from the metadata-only header call (everything that selects a diff
# output); header-shaping flags (`--format=`, `--oneline`, `--no-color`...) are kept.
_NON_HEADER_FLAG_RE = re.compile(
    r"^(-p|-u|-s|--patch.*|-U\d*|--unified.*|--stat(=.*)?|--numstat|--shortstat|--summary"
    r"|--raw|--name-only|--name-status|--check|--dirstat.*|--compact-summary|--no-patch)$")
_REV_SUFFIX_RE = re.compile(r"(\^[!@]|\^-\d*)$")   # `X^!`, `X^@`, `X^-N` select commits
_OBJ_TYPES = frozenset({"blob", "tree", "commit", "tag"})
_NOT_AN_OBJECT = "not-an-object"
_OMIT_NOTE = "[air: content of protected paths omitted]"
# Flags that shape WHICH commits a `show` walks (count, order, date window, branch scope).
_WALK_FLAG_RE = re.compile(
    r"^(--first-parent|--reverse|--all|-\d+|-n.*|--max-count(=.*)?|--skip(=.*)?"
    r"|--(since|until|after|before)(=.*)?)$")
_WALK_REFUSAL = ("refused: this request walks history (a range or a walk option) and what "
                 "it selects changes protected paths. Show one commit at a time "
                 "(`git show <commit>`) or add a pathspec for the code you want.")


def _is_patch_flag(tok: str) -> bool:
    return (tok in ("-p", "--patch") or tok.startswith("-U")
            or tok.startswith("--unified") or tok.startswith("-L"))


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
_TOOL_BUDGET_S = 30    # ONE time budget shared by every git subprocess of a tool call
_clock = time.monotonic


def _cap(text: str, notes: list[str] | None = None) -> str:
    """Apply the output cap. Notes (omissions, coverage) always survive; when the cap
    cuts the text, a visible truncation note is added too."""
    notes = list(notes or [])
    tail = "\n".join(notes)
    if len(text) + (len(tail) + 1 if tail else 0) <= _MAX_OUTPUT:
        return text.rstrip("\n") + "\n" + tail if tail else text
    notes.insert(0, f"[air: output truncated at {_MAX_OUTPUT} characters]")
    tail = "\n".join(notes)
    return text[:max(0, _MAX_OUTPUT - len(tail) - 1)].rstrip("\n") + "\n" + tail


class ToolError(Exception):
    """A tool call was refused or failed — surfaced to the model as an error
    tool_result so it adapts, never crashing the loop."""


class _Budget:
    """Wall-clock allowance for one tool call; every subprocess draws on it."""

    def __init__(self, total: float | None = None):
        self.total = _TOOL_BUDGET_S if total is None else total
        self.deadline = _clock() + self.total

    def remaining(self) -> float:
        left = self.deadline - _clock()
        if left <= 0:
            raise ToolError(f"refused: git call exceeded its {self.total:g}s time budget")
        return left


def _split_refspec(arg: str) -> str | None:
    """`<ref>:<path>` (git show/cat-file) → the path component, else None.
    Lets deny-globs screen `git show HEAD:.env`-style in-tree reads.

    Splits where git does: at the first ':' that is not inside a `{...}` group
    (git's own revision parser skips braces, e.g. `<rev>^{/text}`), so the path
    screened is the path git will actually read."""
    if arg.startswith("-") or ":" not in arg:
        return None
    depth = 0
    for i, ch in enumerate(arg):
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        elif ch == ":" and not depth:
            path = arg[i + 1:]
            return path or None
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

    def _screen_refspec_path(self, arg: str) -> str | None:
        """Deny-glob the path side of a `<ref>:<path>` git arg (object-DB read,
        so realpath-jail doesn't apply — but the basename screen still must).
        Returns the screened path, or None when the arg yields no path to screen."""
        path = _split_refspec(arg)
        if path is not None:
            base = os.path.basename(path)
            self._deny_glob_check(path, base)
        return path

    def _git(self, args: list[str], budget: _Budget) -> "subprocess.CompletedProcess":
        """Run git (hardened env, shell=False) inside the shared per-call budget.
        A timeout or a spawn/decode failure raises ToolError, so a caller can never
        read a failed check as an answer."""
        timeout = budget.remaining()
        try:
            return subprocess.run(
                ["git", "--no-pager", *args], cwd=str(self.root), env=_GIT_HARDENED_ENV,
                shell=False, capture_output=True, text=True, errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"git timed out (shared {budget.total:g}s time budget)")
        except Exception as e:
            raise ToolError(f"refused: git could not be run ({type(e).__name__})")

    @staticmethod
    def _rev_sides(tok: str) -> list[str]:
        """Revision operands of a token: split a `A..B` / `A...B` range, drop a
        leading `^` (exclusion), a trailing `^!`/`^@`/`^-N` and empty sides."""
        sides = []
        for p in _RANGE_SPLIT_RE.split(tok):
            p = _REV_SUFFIX_RE.sub("", p.lstrip("^"))
            if p:
                sides.append(p)
        return sides

    def _lookup_type(self, spelling: str, budget: _Budget) -> str:
        """Object type of `spelling`: a VERIFIED type, `_NOT_AN_OBJECT` when git
        answers that the name is not an object, else ToolError (could not verify)."""
        proc = self._git(["cat-file", "-t", spelling], budget)
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out in _OBJ_TYPES:
            return out
        if proc.returncode == 128 and "Not a valid object name" in (proc.stderr or ""):
            return _NOT_AN_OBJECT
        raise ToolError(f"refused: could not verify the object type of `{spelling}`")

    def _object_kind(self, side: str, budget: _Budget) -> str:
        """Verified type of a revision operand, a tag peeled to its target."""
        kind = self._lookup_type(side, budget)
        if kind == "tag":
            kind = self._lookup_type(f"{side}^{{}}", budget)
            if kind == _NOT_AN_OBJECT:
                raise ToolError(f"refused: could not verify the object type of `{side}`")
        return kind

    def _is_tree_path(self, tok: str) -> bool:
        """`tok` names an existing path inside the checkout that passes the jail."""
        if not os.path.lexists(self.root / tok):
            return False
        self._jail(tok)   # symlink escape / deny-glob -> ToolError
        return True

    def _verify_operands(self, verb: str, toks: list[str], budget: _Budget,
                         screened: set[str], type_form: bool = False) -> bool:
        """Every checked operand must be (a) a token whose path was extracted and
        screened (`screened`, produced by the screening step), or (b) a verified
        NON-blob object, or (show/diff/log only, a lone operand) an existing in-tree
        path that passed the path screens; a blob, an unknown name or a failed lookup
        refuses the call. Having a ':' in it earns an operand nothing. Returns True iff
        every operand is a verified revision (so `-- <pathspecs>` may follow it)."""
        all_revs = True
        for i, tok in enumerate(toks):
            if tok in screened:
                if verb != "cat-file" and ".." in tok:
                    # A range operator in a token that also carries a `<rev>:<path>` leaves
                    # its other side with no screen of either kind; keep them separate.
                    raise ToolError(f"refused: `{tok}` combines range syntax with "
                                    "`<rev>:<path>`; pass them as separate arguments")
                all_revs = False      # `<rev>:<path>` whose path passed the screens
                continue
            if type_form and i == 0 and tok in _OBJ_TYPES:
                all_revs = False      # `git cat-file <type> <object>`: the type word
                continue
            sides = self._rev_sides(tok)
            if not sides:
                raise ToolError(f"refused: could not verify `{tok}` as a revision or path")
            kinds = [self._object_kind(side, budget) for side in sides]
            if "blob" in kinds:
                raise ToolError("refused: raw blob reads by object id are not allowed "
                                "(use `<rev>:<path>`)")
            if _NOT_AN_OBJECT not in kinds:
                continue
            if (verb != "cat-file" and len(sides) == 1
                    and not _RANGE_SPLIT_RE.search(tok) and self._is_tree_path(tok)):
                all_revs = False
                continue
            raise ToolError(f"refused: `{tok}` is neither a verifiable revision nor an "
                            "existing path in the checkout")
        return all_revs

    def _scan_changed_paths(self, verb: str, argv_tail: list[str],
                            budget: _Budget) -> tuple[bool, bool]:
        """Pre-pass for the git-side exclusion. Lists every path the requested
        revisions change and returns (touches_protected_path, rename_pair_denied).
        With a deny-globbed side excluded, a rename/copy pair loses its pairing and the
        other side prints as a whole-file add/delete carrying the bytes, hence the
        pair test. Raises ToolError when the pre-pass could not run (fail closed)."""
        flags: list[str] = []
        toks: list[str] = []
        prev = ""
        for a in argv_tail:
            if a.startswith("-"):
                if not _FORMAT_FLAG_RE.match(a):
                    flags.append(a)
            elif prev == "-n" and a.isdigit():
                flags.append(a)     # `-n 5`: the count belongs to the flag
            else:
                toks.append(a)
            prev = a
        pre = [verb, *flags, "-M", "-C", "-C", "--name-status", "-z"]
        if verb in ("log", "show"):
            pre.append("--pretty=format:")
        proc = self._git([*pre, *toks], budget)
        if proc.returncode != 0:
            raise ToolError("refused: could not verify rename/copy pairs for this git call")
        fields = [f for f in proc.stdout.split("\0") if f != ""]
        touches = any(_deny_glob_match(os.path.basename(f), f) for f in fields)
        pair_denied = False
        i = 0
        while i < len(fields):
            st = fields[i]
            i += 1
            if not _NAME_STATUS_RE.match(st):
                continue  # separator noise; resync on the next status token
            n = 2 if st[0] in "RC" else 1
            paths = fields[i:i + n]
            i += n
            if n == 2 and any(_deny_glob_match(os.path.basename(p), p) for p in paths):
                pair_denied = True
        return touches, pair_denied

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
        budget = _Budget()
        has_graph = has_patch_flag = has_diff_format = False
        has_dashdash = False
        flags: list[str] = []
        rev_toks: list[str] = []   # non-flag tokens BEFORE any `--`
        screened: set[str] = set()  # tokens whose `<ref>:<path>` path was extracted + screened
        all_toks: list[str] = []   # every non-flag token (cat-file reads objects after `--` too)
        prev = ""
        for tok in argv[2:]:
            if tok == "--":
                has_dashdash = True
                prev = tok
                continue  # pathspec separator — neither a flag nor a path
            if tok.startswith("-"):
                # FLAG: default-deny (see _git_flag_allowed). The deny-regex is kept as
                # an explicit, documented blocklist of the worst flags; the allowlist is
                # the backstop that closes unenumerated path-valued/exec siblings.
                if _GIT_DENY_FLAG_RE.match(tok) or not _git_flag_allowed(tok):
                    raise ToolError(f"git flag not allowed (read-only allowlist): {tok}")
                # Colour escapes are woven into diff headers; only `--color=never` (or
                # `--no-color`) keeps the output scan reading plain text.
                if tok.startswith("--color") and tok != "--color=never":
                    raise ToolError(f"git flag not allowed (only --color=never): {tok}")
                flags.append(tok)
                has_graph = has_graph or tok == "--graph"
                has_patch_flag = has_patch_flag or _is_patch_flag(tok)
                has_diff_format = has_diff_format or bool(_DIFF_FORMAT_FLAG_RE.match(tok))
                prev = tok
                continue
            if prev == "-n" and tok.isdigit():
                prev = tok
                continue  # `-n 5`: the count is the flag's value, not a revision
            prev = tok
            all_toks.append(tok)
            if not has_dashdash:
                rev_toks.append(tok)
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
            if self._screen_refspec_path(tok):  # `git show HEAD:.env` → refused
                screened.add(tok)
            # A wildcard pathspec (`.e??`, `*.key`) expands git-side to match files the
            # literal token never equals, so the deny-glob below can't see what it reads.
            if any(c in tok for c in "*?["):
                raise ToolError(f"git wildcard pathspec not allowed: {tok}")
            # Plain literal pathspec (`git log -p -- .env`): refs/SHAs/ranges never match
            # a sensitive deny-glob, so this only ever refuses an actual deny-globbed path.
            self._deny_glob_check(tok, os.path.basename(tok))
        # Does this call print a PATCH? `show`/`diff` do unless a stat/name-listing/
        # no-patch format was chosen; `log` only with an explicit patch flag. An explicit
        # patch flag always wins (the strict reading).
        patch_output = (verb in _PATCH_VERBS or verb == "log") and (
            has_patch_flag or (verb in _PATCH_VERBS and not has_diff_format))
        # `--graph` prefixes every line (`| diff --git …`) next to patch output.
        if has_graph and patch_output:
            raise ToolError("git flag not allowed with patch output: --graph")
        # Object checks (three-way, see the module docstring): a blob named by SHA or by a
        # tag peeling to one has no path for the deny-globs to screen; an operand that
        # cannot be verified as a non-blob object or an existing path is refused too.
        # `git cat-file -p -- <blob>` still prints the blob, so for cat-file every
        # non-flag token counts, not just those before `--`.
        all_revs = False
        if verb in _OBJECT_VERBS:
            type_form = verb == "cat-file" and not any(f in ("-p", "-t", "-s", "-e") for f in flags)
            all_revs = self._verify_operands(
                verb, all_toks if verb == "cat-file" else rev_toks, budget, screened,
                type_form)
        # Protected content in patch output. git's own commit selection is never
        # re-implemented. `diff` gets git-side exclusions, and only when a requested change
        # touches a protected path (so an empty or all-harmless request is never rewritten).
        # `show` of an explicit list of single revisions is filtered one revision at a time
        # (each commit keeps its own header). A `show` that WALKS history (a range, `^X`,
        # `X^@`, or a count/order/date/branch-scope flag) goes to git unchanged unless a
        # selected change touches a protected path, in which case it is refused with a
        # pointer to one-commit-at-a-time `show`. `log` is never path-limited, so it relies
        # on the output scan below and is refused whole when its patch would show one.
        no_follow = not any(a == "--follow" or a.startswith("-L") for a in argv[2:])
        show_filter = (verb == "show" and patch_output and not has_dashdash and all_revs
                       and not any(":" in t for t in rev_toks) and no_follow)
        can_exclude = (verb == "diff" and patch_output and not has_dashdash and all_revs
                       and not any(":" in t for t in rev_toks) and no_follow
                       and not any(_COUNT_FLAG_RE.match(a) for a in argv[2:]))
        if show_filter:
            walks = (any(_WALK_FLAG_RE.match(f) for f in flags)
                     or any(_RANGE_SPLIT_RE.search(t) or t.startswith("^")
                            or _REV_SUFFIX_RE.search(t) for t in rev_toks))
            if walks:
                touches, pair_denied = self._scan_changed_paths(verb, argv[2:], budget)
                if touches or pair_denied:
                    raise ToolError(_WALK_REFUSAL)
                # nothing protected is selected: git's own result is returned as it is
            else:
                # One revision at a time: each keeps its own header (a metadata-only
                # call), then its own filtered patch, then the note when something was
                # omitted. Output over the cap is cut at a commit boundary, and says so.
                flag_args = [a for a in argv[2:] if a.startswith("-")]
                revs: list = self._dedupe_revs(rev_toks, budget) or [None]  # none: git's default
                room = _MAX_OUTPUT - 300                 # leaves space for the notes
                body, shown, omitted = "", 0, False
                for rev in revs:
                    part, om = self._show_one(flag_args, [rev] if rev else [], budget)
                    sep = "\n" if shown else ""          # git's blank line between commits
                    if shown and len(body) + len(sep) + len(part) > room:
                        break
                    body += sep + part
                    omitted = omitted or om
                    shown += 1
                    if len(body) > room:
                        break
                if git_paths.any_path(body, lambda p: _deny_glob_match(os.path.basename(p), p)):
                    raise ToolError("refused: git output would expose a deny-globbed file's content")
                budget.remaining()
                notes = [_OMIT_NOTE] if omitted else []
                if shown < len(revs):
                    notes.append(f"[air: output limit reached: showing {shown} of "
                                 f"{len(revs)} requested commits]")
                return _cap(body or "(no output)", notes)
        excl: list[str] = []
        if can_exclude:
            touches, pair_denied = self._scan_changed_paths(verb, argv[2:], budget)
            if pair_denied:
                raise ToolError("refused: git output would expose a deny-globbed file's content")
            if touches:
                excl = ["--", ".", *_exclude_pathspecs()]
        proc = self._git([verb, *argv[2:], *excl], budget)
        body = proc.stdout if proc.returncode == 0 else (proc.stdout + proc.stderr)
        # Output scan (the backstop): refuse the output if any diff-header form names a
        # protected path (see git_paths.py).
        if git_paths.any_path(body or "", lambda p: _deny_glob_match(os.path.basename(p), p)):
            if verb == "log" and patch_output:
                raise ToolError(
                    "refused: this git log output would include a protected path. Use "
                    "`git show <commit>` (headers kept, protected content omitted) or add "
                    "a pathspec for the code you want.")
            raise ToolError("refused: git output would expose a deny-globbed file's content")
        budget.remaining()   # a call that outran its budget is refused, not returned late
        return _cap(body or "(no output)", [_OMIT_NOTE] if excl else None)

    def _dedupe_revs(self, revs: list[str], budget: _Budget) -> list[str]:
        """Drop repeated revisions the way git does: by the object they name, not by
        spelling (two names for one commit are shown once). Order is kept; a name that
        does not resolve is kept as given so git reports it."""
        seen: set[str] = set()
        out: list[str] = []
        for rev in revs:
            proc = self._git(["rev-parse", "--verify", "--quiet", f"{rev}^{{object}}"], budget)
            key = proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else rev
            if key not in seen:
                seen.add(key)
                out.append(rev)
        return out

    def _show_one(self, flag_args: list[str], rev_arg: list[str],
                  budget: _Budget) -> tuple[str, bool]:
        """`git show` for ONE revision with protected content omitted. Returns
        (text, omitted): the header and filtered patch, and whether anything was left
        out (the caller adds the note, outside any cut of the body). A plain git error
        is returned as text."""
        touches, pair_denied = self._scan_changed_paths("show", [*flag_args, *rev_arg], budget)
        if pair_denied:
            raise ToolError("refused: git output would expose a deny-globbed file's content")
        raw = self._lookup_type(rev_arg[0], budget) if (touches and rev_arg) else ""
        kind = self._object_kind(rev_arg[0], budget) if raw == "tag" else raw
        if kind == "tree":
            touches = False       # a tree listing has no header/patch split: print it once
        if not touches:
            proc = self._git(["show", *flag_args, *rev_arg], budget)
            return (proc.stdout if proc.returncode == 0 else (proc.stdout + proc.stderr)), False
        hdr_args = ["show", "-s",
                    *[a for a in flag_args if not _NON_HEADER_FLAG_RE.match(a)], *rev_arg]
        hdr = self._git(hdr_args, budget)
        if hdr.returncode != 0:
            return hdr.stdout + hdr.stderr, False
        # An annotated tag prints its own header too: take the patch from its commit so
        # the header appears once.
        patch_rev = [f"{rev_arg[0]}^{{commit}}"] if raw == "tag" and kind == "commit" else rev_arg
        proc = self._git(["show", *flag_args, "--format=", *patch_rev,
                          "--", ".", *_exclude_pathspecs()], budget)
        if proc.returncode != 0:
            return proc.stdout + proc.stderr, False
        out = hdr.stdout.rstrip("\n") + "\n"
        if proc.stdout.strip():
            out += "\n" + proc.stdout.rstrip("\n") + "\n"
        return out, True

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
