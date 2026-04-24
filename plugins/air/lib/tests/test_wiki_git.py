"""Unit tests for wiki_git.py — commit flow + retry behavior.

Tests use a pair of local bare-repo + clone directories to simulate the
wiki remote, so no network needed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LIB = HERE.parent
sys.path.insert(0, str(LIB))

import wiki_git  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def fake_remote(tmp_path):
    """Bare repo acting as the wiki remote."""
    remote = tmp_path / "wiki.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=master")
    return remote


@pytest.fixture
def wiki_clone(tmp_path, fake_remote):
    """Clone of the bare remote with an initial commit + identity."""
    clone = tmp_path / "wiki"
    subprocess.run(["git", "clone", str(fake_remote), str(clone)], check=True, capture_output=True)
    wiki_git.configure_identity(clone, "test-bot", "test@example.com")
    # Seed the wiki with an initial commit so we have a branch to push to.
    (clone / "Home.md").write_text("# Wiki\n")
    _git(clone, "add", "Home.md")
    _git(clone, "commit", "-m", "initial")
    _git(clone, "push", "origin", "master")
    return clone


def _write_meta(wiki, reviews_since=1):
    (wiki / wiki_git.META_FILENAME).write_text(
        json.dumps({"reviews_since": reviews_since, "last_cleanup": "2026-04-01T00:00:00Z",
                    "last_check": "2026-04-01T00:00:00Z", "last_processed_pr": 1}, indent=2) + "\n"
    )


def test_commit_meta_returns_false_when_no_file(wiki_clone):
    ok = wiki_git.commit_meta(wiki_clone, "test")
    assert ok is False


def test_commit_meta_happy_path(wiki_clone, fake_remote):
    _write_meta(wiki_clone, reviews_since=1)
    ok = wiki_git.commit_meta(wiki_clone, "bump counter")
    assert ok is True
    # Confirm the remote advanced: fresh clone should have the file.
    tmp = wiki_clone.parent / "verify"
    subprocess.run(["git", "clone", str(fake_remote), str(tmp)], check=True, capture_output=True)
    assert (tmp / wiki_git.META_FILENAME).is_file()


def test_commit_meta_noop_when_unchanged(wiki_clone):
    """Running commit_meta twice in a row with the same file should not fail
    and should not create an empty commit."""
    _write_meta(wiki_clone, reviews_since=1)
    assert wiki_git.commit_meta(wiki_clone, "first") is True
    # Second call with identical content: nothing staged, returns True.
    assert wiki_git.commit_meta(wiki_clone, "second") is True


def test_commit_meta_retries_on_concurrent_push(tmp_path, fake_remote, wiki_clone):
    """Simulate the race: a second clone pushes first; our clone's push fails
    non-fast-forward; retry with pull --rebase succeeds."""
    # Second clone: lands a new commit on master first.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(fake_remote), str(other)], check=True, capture_output=True)
    wiki_git.configure_identity(other, "other-bot", "other@example.com")
    (other / "unrelated.md").write_text("# other\n")
    _git(other, "add", "unrelated.md")
    _git(other, "commit", "-m", "other side wrote first")
    _git(other, "push", "origin", "master")

    # Now our clone is stale. Try to push: first attempt should fail,
    # retry with pull --rebase should succeed.
    _write_meta(wiki_clone, reviews_since=7)
    ok = wiki_git.commit_meta(wiki_clone, "bump after race")
    assert ok is True

    # Final remote state: both commits landed.
    verify = tmp_path / "verify"
    subprocess.run(["git", "clone", str(fake_remote), str(verify)], check=True, capture_output=True)
    assert (verify / "unrelated.md").is_file()
    assert (verify / wiki_git.META_FILENAME).is_file()
    assert json.loads((verify / wiki_git.META_FILENAME).read_text())["reviews_since"] == 7


def test_clone_wiki_missing_repo(tmp_path):
    """If the wiki URL 404s, clone_wiki returns False rather than raising."""
    dest = tmp_path / "nope"
    # Using a local path that doesn't exist triggers git's "not found" error.
    ok = wiki_git.clone_wiki(str(tmp_path / "does-not-exist.git"), dest)
    assert ok is False
    assert not dest.exists()
