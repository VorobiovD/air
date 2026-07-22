#!/usr/bin/env python3
"""Oversized-diff local fallback: when GitHub's REST diff endpoint 406s on a PR
with >300 files, air computes the diff locally via `git diff base...head`
(three-dot, matching GitHub's PR-diff merge-base semantics) from the checkout it
already has — so a big sync/promote PR gets a (byte-capped) review instead of a
hard exit(1). A real throwaway git repo is the fixture (the fallback shells out
to git; mocking subprocess would test nothing).

Run: python -m pytest managed/test-diff-fallback.py
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import github_client as gc  # noqa: E402
from github_client import local_diff_fallback, fetch_pr_diff, fetch_inter_diff  # noqa: E402


@pytest.fixture(scope="module")
def diverged_repo(tmp_path_factory):
    """merge-base c1; head (feature) changes a.py; base tip changes b.py.
    Three-dot base...head must show ONLY a.py (head's change since the
    merge-base), NOT b.py — that's what distinguishes it from two-dot."""
    repo = tmp_path_factory.mktemp("diff-fallback-repo")

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
            env={"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x",
                 "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x",
                 "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(repo)},
        )

    git("init", "-q", "-b", "main")
    (repo / "a.py").write_text("a1\n")
    (repo / "b.py").write_text("b1\n")
    git("add", "."); git("commit", "-q", "-m", "c1")
    merge_base = git("rev-parse", "HEAD").stdout.strip()
    # feature branch off c1 → change a.py = head
    git("checkout", "-q", "-b", "feature")
    (repo / "a.py").write_text("a1\nA-CHANGED\n")
    git("add", "."); git("commit", "-q", "-m", "head")
    head_sha = git("rev-parse", "HEAD").stdout.strip()
    # base branch moves ahead independently → change b.py = base tip
    git("checkout", "-q", "main")
    (repo / "b.py").write_text("b1\nB-BASE-ONLY\n")
    git("add", "."); git("commit", "-q", "-m", "base")
    base_sha = git("rev-parse", "HEAD").stdout.strip()
    return {"repo": str(repo), "base": base_sha, "head": head_sha, "mb": merge_base}


def test_three_dot_semantics(diverged_repo):
    d = local_diff_fallback(diverged_repo["base"], diverged_repo["head"],
                            checkout_dir=diverged_repo["repo"])
    assert d is not None
    assert "a.py" in d and "A-CHANGED" in d          # head's change IS included
    assert "b.py" not in d and "B-BASE-ONLY" not in d  # base-only change is NOT (three-dot)


def test_env_default_checkout(diverged_repo, monkeypatch):
    monkeypatch.setenv("AIR_TARGET_REPO", diverged_repo["repo"])
    d = local_diff_fallback(diverged_repo["base"], diverged_repo["head"])  # no checkout_dir kwarg
    assert d is not None and "A-CHANGED" in d


def test_missing_checkout_returns_none(diverged_repo, monkeypatch):
    monkeypatch.delenv("AIR_TARGET_REPO", raising=False)
    assert local_diff_fallback(diverged_repo["base"], diverged_repo["head"],
                               checkout_dir="/nonexistent/dir") is None
    # no checkout at all → None (not a crash)
    assert local_diff_fallback(diverged_repo["base"], diverged_repo["head"]) is None


def test_absent_sha_fails_safe_no_fetch(diverged_repo):
    # A valid-looking SHA absent from the checkout: rev-parse fails and we do NOT
    # fetch (persist-credentials is off on the real checkout by design) → None,
    # never a crash and never a wrong diff.
    bogus = "0" * 40
    assert local_diff_fallback(bogus, diverged_repo["head"],
                               checkout_dir=diverged_repo["repo"]) is None


def test_empty_args_returns_none():
    assert local_diff_fallback("", "abc") is None
    assert local_diff_fallback("abc", "") is None


# ---- 406 fallback wiring in the fetchers -----------------------------------

class _Resp:
    def __init__(self, ok, status_code, text="", json_data=None):
        self.ok, self.status_code, self.text, self._j = ok, status_code, text, json_data

    def json(self):
        if self._j is None:
            raise ValueError("no json")
        return self._j


_SAMPLE_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"


def test_fetch_pr_diff_406_uses_local_fallback(monkeypatch):
    monkeypatch.setattr(gc, "_gh_request", lambda *a, **k: _Resp(False, 406, "too many files"))
    monkeypatch.setattr(gc, "_pr_base_head", lambda r, p, t: ("base40", "head40"))
    monkeypatch.setattr(gc, "local_diff_fallback", lambda b, h, **k: _SAMPLE_DIFF)
    out = fetch_pr_diff("o/r", 1, "tok")
    assert out == gc.apply_diff_hygiene(_SAMPLE_DIFF)


def test_fetch_pr_diff_406_fallback_unavailable_still_exits(monkeypatch):
    # 406 but no checkout / git fails → fallback None → keep fail-loud exit(1).
    monkeypatch.setattr(gc, "_gh_request", lambda *a, **k: _Resp(False, 406, "too many files"))
    monkeypatch.setattr(gc, "_pr_base_head", lambda r, p, t: ("base40", "head40"))
    monkeypatch.setattr(gc, "local_diff_fallback", lambda b, h, **k: None)
    with pytest.raises(SystemExit) as ei:
        fetch_pr_diff("o/r", 1, "tok")
    assert ei.value.code == 1


def test_fetch_pr_diff_non_406_error_exits_without_fallback(monkeypatch):
    # A 403 (auth) must NOT trigger the local fallback — stays fail-loud.
    called = {"fallback": False}
    monkeypatch.setattr(gc, "_gh_request", lambda *a, **k: _Resp(False, 403, "forbidden"))
    monkeypatch.setattr(gc, "local_diff_fallback",
                        lambda *a, **k: called.__setitem__("fallback", True) or _SAMPLE_DIFF)
    with pytest.raises(SystemExit) as ei:
        fetch_pr_diff("o/r", 1, "tok")
    assert ei.value.code == 1 and called["fallback"] is False


def test_fetch_inter_diff_406_uses_local_fallback(monkeypatch):
    monkeypatch.setattr(gc, "_gh_request", lambda *a, **k: _Resp(False, 406, "too many files"))
    monkeypatch.setattr(gc, "local_diff_fallback", lambda b, h, **k: _SAMPLE_DIFF)
    out = fetch_inter_diff("o/r", "base40", "head40", "tok")
    assert out == gc.apply_diff_hygiene(_SAMPLE_DIFF)


def test_fetch_inter_diff_non_406_returns_none(monkeypatch):
    # 404 (force-push GC'd base) keeps the None contract → caller full-review.
    monkeypatch.setattr(gc, "_gh_request", lambda *a, **k: _Resp(False, 404, "not found"))
    monkeypatch.setattr(gc, "local_diff_fallback", lambda *a, **k: pytest.fail("must not call on 404"))
    assert fetch_inter_diff("o/r", "base40", "head40", "tok") is None


def _raise_request(*a, **k):
    raise gc.req.RequestException("connection exhausted")


def test_pr_base_head_degrades_on_raised_request(monkeypatch):
    # _gh_request RE-RAISES on retry exhaustion; the extra GET must degrade to
    # None (→ clean exit(1)), never propagate an uncaught traceback.
    monkeypatch.setattr(gc, "_gh_request", _raise_request)
    assert gc._pr_base_head("o/r", 1, "tok") is None


def test_fetch_pr_diff_406_base_head_none_exits(monkeypatch):
    # 406 but the base/head lookup failed (raised/None) → no fallback → exit(1).
    monkeypatch.setattr(gc, "_gh_request", lambda *a, **k: _Resp(False, 406, "too many files"))
    monkeypatch.setattr(gc, "_pr_base_head", lambda r, p, t: None)
    with pytest.raises(SystemExit) as ei:
        fetch_pr_diff("o/r", 1, "tok")
    assert ei.value.code == 1


# --- re-review inter-diff scope (only_files) + fetch_pr_changed_files --------

# A 2-file compare diff: the PR's own file + a base-branch-merged noise file.
_SCOPED_DIFF = (
    "diff --git a/src/pr.py b/src/pr.py\n--- a/src/pr.py\n+++ b/src/pr.py\n@@ -1 +1 @@\n-x\n+y\n"
    "diff --git a/docs/merged.md b/docs/merged.md\n--- a/docs/merged.md\n+++ b/docs/merged.md\n@@ -0,0 +1 @@\n+noise\n"
)


def test_inter_diff_only_files_filters_before_hygiene(monkeypatch):
    monkeypatch.setattr(gc, "_gh_request", lambda *a, **k: _Resp(True, 200, _SCOPED_DIFF))
    out = fetch_inter_diff("o/r", "base40", "head40", "tok", only_files={"src/pr.py"})
    assert "src/pr.py" in out
    assert "docs/merged.md" not in out          # merged-in noise dropped


def test_inter_diff_only_files_none_is_unfiltered(monkeypatch):
    # Default (promote/origin callers) → byte-identical to no-filter behavior.
    monkeypatch.setattr(gc, "_gh_request", lambda *a, **k: _Resp(True, 200, _SCOPED_DIFF))
    out = fetch_inter_diff("o/r", "base40", "head40", "tok")
    assert out == gc.apply_diff_hygiene(_SCOPED_DIFF)
    assert "docs/merged.md" in out


def test_inter_diff_only_files_applies_on_406_local_fallback(monkeypatch):
    monkeypatch.setattr(gc, "_gh_request", lambda *a, **k: _Resp(False, 406, "too many files"))
    monkeypatch.setattr(gc, "local_diff_fallback", lambda b, h, **k: _SCOPED_DIFF)
    out = fetch_inter_diff("o/r", "b", "h", "tok", only_files={"src/pr.py"})
    assert "src/pr.py" in out and "docs/merged.md" not in out


def test_fetch_pr_changed_files_collects_paths_and_renames(monkeypatch):
    files = [{"filename": "src/a.py"},
             {"filename": "src/new.py", "previous_filename": "src/old.py"}]
    monkeypatch.setattr(gc, "_github_paginate", lambda *a, **k: files)
    got = gc.fetch_pr_changed_files("o/r", 1, "tok")
    assert got == {"src/a.py", "src/new.py", "src/old.py"}


def test_fetch_pr_changed_files_error_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise gc.PartialPageError("mid-walk fail")
    monkeypatch.setattr(gc, "_github_paginate", _boom)
    assert gc.fetch_pr_changed_files("o/r", 1, "tok") is None   # fail-open


def test_fetch_pr_changed_files_oversized_returns_none(monkeypatch):
    huge = [{"filename": f"f{i}.py"} for i in range(gc._PR_FILES_MAX_PAGES * 100)]
    monkeypatch.setattr(gc, "_github_paginate", lambda *a, **k: huge)
    assert gc.fetch_pr_changed_files("o/r", 1, "tok") is None   # oversized → don't filter
