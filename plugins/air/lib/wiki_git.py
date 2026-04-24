#!/usr/bin/env python3
"""
Small wiki-repo git helpers used by the counter flow.

Stdlib-only. Callers (meta.py writer scripts, managed/review.py) invoke
these to clone + commit + push `.air-meta.json`. Push retries ONCE with
`git pull --rebase` on non-fast-forward so rare concurrent CI reviews
don't lose one side's counter bump.
"""

import os
import subprocess
import sys
from pathlib import Path

META_FILENAME = ".air-meta.json"


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command. Captures stdout+stderr. Raises on non-zero when `check` is True."""
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def clone_wiki(wiki_url: str, dest: Path, depth: int = 1) -> bool:
    """Clone the wiki repo to `dest`. Returns True on success, False if the
    repo doesn't exist yet (new repos have no wiki until something pushes to
    it). Caller should treat False as 'fresh' and skip counter work — the
    first `/air:review` that writes REVIEW.md will create the wiki.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(["git", "clone", "--depth", str(depth), wiki_url, str(dest)])
        return True
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").lower()
        if "repository not found" in stderr or "not found" in stderr:
            print(f"  [wiki] {wiki_url} doesn't exist yet — skipping counter", file=sys.stderr)
            return False
        print(f"  [wiki] clone failed: {e.stderr.strip() if e.stderr else e}", file=sys.stderr)
        return False


def commit_meta(wiki_dir: Path, message: str) -> bool:
    """Stage .air-meta.json, commit if there's a delta, push with one retry.
    Returns True on success (including 'no changes'), False on failure.

    Concurrency: if two CI runs push at the same time, the second gets a
    non-fast-forward error. We pull --rebase and push once more; typically
    succeeds because the only contested file is .air-meta.json and the
    counter semantics are commutative (+1 + +1 = +2, regardless of order).
    """
    wiki_dir = Path(wiki_dir)
    meta_path = wiki_dir / META_FILENAME
    if not meta_path.is_file():
        print(f"  [wiki] no {META_FILENAME} to commit", file=sys.stderr)
        return False

    try:
        _run(["git", "add", META_FILENAME], cwd=wiki_dir)
        # Exit 0 = nothing staged, exit 1 = something staged. Either is fine.
        diff = _run(["git", "diff", "--cached", "--quiet"], cwd=wiki_dir, check=False)
        if diff.returncode == 0:
            print(f"  [wiki] {META_FILENAME} unchanged — skipping commit", file=sys.stderr)
            return True
        _run(["git", "commit", "-m", message], cwd=wiki_dir)
    except subprocess.CalledProcessError as e:
        print(f"  [wiki] commit failed: {e.stderr.strip() if e.stderr else e}", file=sys.stderr)
        return False

    # First push attempt.
    try:
        _run(["git", "push"], cwd=wiki_dir)
        return True
    except subprocess.CalledProcessError:
        pass

    # Retry: rebase onto the remote, then push again. On rebase conflict in
    # .air-meta.json we give up — the next review's bump will write a fresh
    # state based on whatever won the race.
    print(f"  [wiki] push raced; retrying with pull --rebase", file=sys.stderr)
    try:
        _run(["git", "pull", "--rebase"], cwd=wiki_dir)
        _run(["git", "push"], cwd=wiki_dir)
        return True
    except subprocess.CalledProcessError as e:
        print(f"  [wiki] push retry failed: {e.stderr.strip() if e.stderr else e}", file=sys.stderr)
        return False


def configure_identity(wiki_dir: Path, name: str, email: str) -> None:
    """Set local (repo-scoped) git identity so commits work on CI runners
    that don't have a global user.name / user.email configured."""
    _run(["git", "config", "user.name", name], cwd=wiki_dir)
    _run(["git", "config", "user.email", email], cwd=wiki_dir)
