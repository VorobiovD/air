#!/usr/bin/env python3
"""
Small wiki-repo git helpers used by the counter flow.

Stdlib-only. Callers (meta.py writer scripts, managed/review.py) invoke
these to clone + commit + push `.air-meta.json`. Push retries ONCE with
`git pull --rebase` on non-fast-forward so rare concurrent CI reviews
don't lose one side's counter bump.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

META_FILENAME = ".air-meta.json"

# Matches the `https://x-access-token:<TOKEN>@github.com/...` pattern we use
# for wiki auth. Redacts the token before URLs (or git's stderr containing
# them) are logged locally or echoed on dev machines where GH's secret
# redactor doesn't apply.
_TOKEN_URL_RE = re.compile(r"(https?://[^:@/\s]+:)[^@\s]+(@)")


def _redact(s: str) -> str:
    return _TOKEN_URL_RE.sub(r"\1***\2", s or "")


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True,
         timeout: float = 120) -> subprocess.CompletedProcess:
    """Run a git command. Captures stdout+stderr. Raises on non-zero when
    `check` is True.

    Bounded at `timeout` seconds (audit H3): an unbounded wiki clone/pull/push
    to a black-holed remote used to hang the review tail or a learn run until
    the 95-min workflow kill. A timeout is surfaced as a CalledProcessError
    (exit 124) so every caller's existing `except CalledProcessError → return
    False` path handles it — a hung wiki push after a posted review must fail
    the counter/mirror step, never the whole job."""
    try:
        return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise subprocess.CalledProcessError(
            124, cmd, output=(e.output or ""),
            stderr=f"git timed out after {timeout}s",
        ) from e


def clone_wiki(wiki_url: str, dest: Path, depth: int = 1) -> bool:
    """Clone the wiki repo to `dest`. Returns True on success, False if the
    repo doesn't exist yet (new repos have no wiki until something pushes to
    it). Caller should treat False as 'fresh' and skip counter work — the
    first `/air:review` that writes REVIEW.md will create the wiki.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    safe_url = _redact(wiki_url)
    try:
        _run(["git", "clone", "--depth", str(depth), wiki_url, str(dest)])
        return True
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "")
        # Match git's canonical phrase, not a broad substring — "not found"
        # alone would also swallow DNS and transient connectivity errors as
        # if they were fresh-wiki responses.
        if "repository not found" in stderr.lower():
            print(f"  [wiki] {safe_url} doesn't exist yet — skipping counter", file=sys.stderr)
            return False
        # str(CalledProcessError) embeds the cmd list which contains the
        # token-bearing wiki URL — route the whole message through _redact
        # so stderr-empty / signal-killed paths don't leak the PAT on local
        # terminals (where GH's secret redactor isn't present).
        detail = stderr.strip() if stderr else str(e)
        print(f"  [wiki] clone failed: {_redact(detail)}", file=sys.stderr)
        return False


def commit_paths(wiki_dir: Path, paths: list[str], message: str,
                 remove: list[str] | None = None) -> bool:
    """Stage the given paths (relative to wiki_dir, only those that exist),
    optionally `git rm` the `remove` paths (orphan reconciliation), commit if
    there's a delta, push with one rebase-retry. Returns True on success
    (including 'no changes'), False on failure or nothing-to-stage.

    Generalizes the counter push to an arbitrary file list — the
    deterministic store→wiki mirror render (managed/render_store_to_wiki.py)
    stages REVIEW.md, GLOSSARY.md, etc. and removes mirror files whose store
    source was deleted.

    Concurrency (honest): if two CI runs push at the same time, the second
    gets a non-fast-forward error. We pull --rebase and push once more.
    - If the other side touched unrelated files, rebase auto-resolves and
      the retry succeeds.
    - If the other side also mutated the same file, rebase produces a content
      conflict and the except block returns False — that push is dropped.
      For the counter this can leave it off by one; for the mirror it's
      self-healing (the next render re-derives from the store, the source of
      truth). Acceptable until a merge-driver re-applies mathematically.
    """
    wiki_dir = Path(wiki_dir)
    staged_any = False
    try:
        for rel in paths:
            if (wiki_dir / rel).is_file():
                _run(["git", "add", rel], cwd=wiki_dir)
                staged_any = True
        for rel in (remove or []):
            if (wiki_dir / rel).is_file():
                # --ignore-unmatch: an untracked same-named file is a no-op,
                # not a failure (keeps the push best-effort).
                _run(["git", "rm", "--quiet", "--ignore-unmatch", rel], cwd=wiki_dir)
                staged_any = True
        if not staged_any:
            print("  [wiki] no files to commit", file=sys.stderr)
            return False
        # Exit 0 = nothing staged (no delta), exit 1 = something staged.
        diff = _run(["git", "diff", "--cached", "--quiet"], cwd=wiki_dir, check=False)
        if diff.returncode == 0:
            print("  [wiki] no changes to commit — skipping", file=sys.stderr)
            return True
        _run(["git", "commit", "-m", message], cwd=wiki_dir)
    except subprocess.CalledProcessError as e:
        detail = e.stderr.strip() if e.stderr else str(e)
        print(f"  [wiki] commit failed: {_redact(detail)}", file=sys.stderr)
        return False

    # First push attempt.
    try:
        _run(["git", "push"], cwd=wiki_dir)
        return True
    except subprocess.CalledProcessError:
        pass

    # Retry: rebase onto the remote, then push again.
    print("  [wiki] push raced; retrying with pull --rebase", file=sys.stderr)
    try:
        _run(["git", "pull", "--rebase"], cwd=wiki_dir)
        _run(["git", "push"], cwd=wiki_dir)
        return True
    except subprocess.CalledProcessError as e:
        detail = e.stderr.strip() if e.stderr else str(e)
        print(f"  [wiki] push retry failed: {_redact(detail)}", file=sys.stderr)
        return False


def commit_meta(wiki_dir: Path, message: str) -> bool:
    """Stage .air-meta.json, commit if there's a delta, push with one retry.
    Thin wrapper over commit_paths (the counter is one file in the list)."""
    wiki_dir = Path(wiki_dir)
    if not (wiki_dir / META_FILENAME).is_file():
        print(f"  [wiki] no {META_FILENAME} to commit", file=sys.stderr)
        return False
    return commit_paths(wiki_dir, [META_FILENAME], message)


def configure_identity(wiki_dir: Path, name: str, email: str) -> None:
    """Set local (repo-scoped) git identity so commits work on CI runners
    that don't have a global user.name / user.email configured."""
    _run(["git", "config", "user.name", name], cwd=wiki_dir)
    _run(["git", "config", "user.email", email], cwd=wiki_dir)
