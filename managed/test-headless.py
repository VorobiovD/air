"""Network-free unit tests for the headless (messages-api) mode.

Locks the regressions the dogfood review flagged on code paths the dry-run A/B
never exercised: the post-path call signatures, the CI exit-code propagation on
failure, and frontmatter-parse robustness.
Imports review.py / headless.py, so it needs the managed deps (CI installs them).
"""
import asyncio
import inspect
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "plugins" / "air" / "lib"))

import github_client  # noqa: E402
import headless  # noqa: E402
import review  # noqa: E402


def test_post_path_signatures_bind():
    # Blocker #2: the post path crashed with TypeError because required args were
    # omitted. Bind the EXACT call shapes headless uses — a missing required arg
    # (commit_id / current_login) raises TypeError here, catching the drift.
    inspect.signature(github_client.submit_review_verdict).bind(
        "o/r", 1, "tok", event="APPROVE", body="", commit_id="deadbeef")
    inspect.signature(github_client.dismiss_stale_air_verdicts).bind(
        "o/r", 1, "tok", "bot-login")
    inspect.signature(github_client._post_review_comment_with_retry).bind(
        "o/r", 1, "body", "tok")


def test_persona_model_survives_malformed_frontmatter(tmp_path, monkeypatch):
    # Blocker-low #11: an unterminated `---` fence must not crash _persona_model.
    monkeypatch.setattr(headless, "AGENTS_DIR", tmp_path)
    (tmp_path / "weird.md").write_text("---\nmodel: sonnet\n(no closing fence)\n")
    body, model, tier = headless._persona_model("air-weird")
    assert model and tier  # sane defaults, no ValueError


def _args():
    return types.SimpleNamespace(mode="messages-api", repo="o/r", pr_number=1, dry_run=True)


def test_messages_api_dispatch_exits_nonzero_on_failure(monkeypatch):
    # Blocker #3: a failed/empty headless review (ok=False) must FAIL the job, not
    # exit 0 (which would green a review that never ran). review.py imports
    # run_headless_review lazily, so patching the module attr is honored.
    monkeypatch.setenv("AIR_BOT_TOKEN", "x")

    async def fake_fail(args, token):
        return {"ok": False, "reason": "no review body"}

    monkeypatch.setattr(headless, "run_headless_review", fake_fail)
    with pytest.raises(SystemExit) as e:
        asyncio.run(review.run_review(_args()))
    assert e.value.code == 1


def test_blocker_lens_incomplete_detects_truncation():
    # The dogfood gate-bypass: a max_turns-truncated security lens has truthy trailing
    # text, so it must NOT read as a completed lens — fail closed instead.
    inc = headless._blocker_lens_incomplete
    assert inc("air-security-auditor", {"text": "partial findings…", "stop": "max_turns"}) is True
    assert inc("air-code-reviewer", {"text": "findings", "stop": "end_turn"}) is False
    assert inc("air-code-reviewer", None) is True
    assert inc("air-security-auditor", {"text": "", "stop": "end_turn"}) is True
    # a non-blocker lens never fail-closes (simplify/git-history can degrade)
    assert inc("air-simplify", {"text": "x", "stop": "max_turns"}) is False


def test_messages_api_dispatch_returns_cleanly_on_success(monkeypatch):
    monkeypatch.setenv("AIR_BOT_TOKEN", "x")

    async def fake_ok(args, token):
        return {"ok": True, "verdict": "APPROVE"}

    monkeypatch.setattr(headless, "run_headless_review", fake_ok)
    asyncio.run(review.run_review(_args()))  # returns, no SystemExit, no managed setup
