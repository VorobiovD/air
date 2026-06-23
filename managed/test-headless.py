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


# ---- P1 context parity: patterns_dir branch + stage_patterns ----------------

def test_build_pr_context_patterns_dir_branch():
    from prompts import build_pr_context
    meta = {"number": 5, "user": {"login": "alice"}, "title": "T", "body": "b",
            "base": {"ref": "main"}, "head": {"ref": "feat", "sha": "abc123"},
            "additions": 1, "deletions": 0, "changed_files": 1, "commits": 1}
    # Default (managed) paths must NOT mention the headless staged dir — additive only.
    assert ".air-patterns" not in build_pr_context(meta, "o/r")
    assert ".air-patterns" not in build_pr_context(meta, "o/r", store_mounted=True)
    pat = build_pr_context(meta, "o/r", patterns_dir=".air-patterns")
    # Points at the staged dir, tells the agent to Glob+Read it, names the author file,
    # and does NOT send it to the (non-existent) managed mount paths.
    assert ".air-patterns" in pat and "Glob" in pat and "author-patterns.md" in pat
    assert "/workspace/wiki" not in pat and "/mnt/memory/" not in pat


def test_stage_patterns_store_path(tmp_path, monkeypatch):
    monkeypatch.delenv("AIR_HEADLESS_PATTERNS", raising=False)
    monkeypatch.setattr(headless.memory_store, "get_store_id",
                        lambda repo, flow="review": "store_x")
    data = {
        "/authors/alice.md": "alice patterns",
        headless.memory_store.GLOSSARY_PATH: "glossary body",
        headless.memory_store.ACCEPTED_PATTERNS_PATH: "accepted body",
    }
    monkeypatch.setattr(headless.memory_store, "read_memory",
                        lambda sid, path: (data[path], "sha", "id") if path in data else None)
    rel, abs_, src = headless.stage_patterns("o/r", "alice", str(tmp_path), "secret-tok")
    assert rel == ".air-patterns"
    d = tmp_path / ".air-patterns"
    assert (d / "author-patterns.md").read_text() == "alice patterns"
    assert (d / "glossary.md").read_text() == "glossary body"
    assert (d / "accepted-patterns.md").read_text() == "accepted body"
    assert "store_x" in src
    # Only files that exist are staged (service/common/severity/project-profile absent here).
    assert not (d / "service-patterns.md").exists()


def test_stage_patterns_wiki_path_no_token_leak(tmp_path, monkeypatch):
    monkeypatch.delenv("AIR_HEADLESS_PATTERNS", raising=False)
    monkeypatch.setattr(headless.memory_store, "get_store_id",
                        lambda repo, flow="review": None)

    def fake_clone(argv, **kw):
        dest = argv[-1]  # `git clone --depth 1 <url> <dest>`
        with open(__import__("os").path.join(dest, "REVIEW.md"), "w") as fh:
            fh.write("review patterns")
        with open(__import__("os").path.join(dest, "GLOSSARY.md"), "w") as fh:
            fh.write("glossary body")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(headless.subprocess, "run", fake_clone)
    rel, abs_, src = headless.stage_patterns("o/r", "alice", str(tmp_path), "secret-tok")
    assert rel == ".air-patterns"
    d = tmp_path / ".air-patterns"
    assert (d / "review-patterns.md").read_text() == "review patterns"
    assert (d / "glossary.md").read_text() == "glossary body"
    assert src.startswith("wiki")
    # The clone URL carries the bot token; it must never surface in the return value.
    assert "secret-tok" not in src


def test_stage_patterns_clone_fail_degrades(tmp_path, monkeypatch):
    monkeypatch.delenv("AIR_HEADLESS_PATTERNS", raising=False)
    monkeypatch.setattr(headless.memory_store, "get_store_id",
                        lambda repo, flow="review": None)
    monkeypatch.setattr(headless.subprocess, "run",
                        lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"err"))
    rel, abs_, src = headless.stage_patterns("o/r", "alice", str(tmp_path), "tok")
    assert rel is None and abs_ is None  # pattern-blind, never raises
    assert not (tmp_path / ".air-patterns").exists()  # no empty dir left behind


def test_stage_patterns_timeout_does_not_leak_token(tmp_path, monkeypatch, capsys):
    # Finding #1: a clone timeout raises TimeoutExpired whose __str__ expands the
    # full argv (token-bearing URL). The broad except must scrub it via _redact —
    # CI masks secrets but a local --dry-run does not.
    monkeypatch.delenv("AIR_HEADLESS_PATTERNS", raising=False)
    monkeypatch.setattr(headless.memory_store, "get_store_id",
                        lambda repo, flow="review": None)

    def fake_timeout(argv, **kw):
        raise headless.subprocess.TimeoutExpired(argv, 90)

    monkeypatch.setattr(headless.subprocess, "run", fake_timeout)
    rel, abs_, src = headless.stage_patterns("o/r", "alice", str(tmp_path), "ghp_SECRET123")
    assert rel is None  # degrades pattern-blind, never raises
    err = capsys.readouterr().err
    assert "ghp_SECRET123" not in err  # token scrubbed from the warn line
    assert "pattern staging failed" in err  # failure still logged (redacted)
    assert not (tmp_path / ".air-patterns").exists()


def test_stage_patterns_disabled_killswitch(tmp_path, monkeypatch):
    monkeypatch.setenv("AIR_HEADLESS_PATTERNS", "0")
    rel, abs_, src = headless.stage_patterns("o/r", "alice", str(tmp_path), "tok")
    assert rel is None and "disabled" in src


def test_run_headless_review_dry_run_orchestration(tmp_path, monkeypatch):
    # Integration: exercise the FULL dry-run path (the parallel bot_login/precomp/
    # stage gather + the conversation gather + context build + specialist/verifier
    # fan-out + gate), so a gather/unpacking regression is caught without paid CI.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("AIR_TARGET_REPO", str(tmp_path))  # real dir for the Sandbox
    head = "a" * 40  # _extract_review_body's footer regex requires a full 40-char hex SHA
    meta = {"number": 1, "user": {"login": "alice"}, "title": "T", "body": "b",
            "base": {"ref": "main"}, "head": {"ref": "feat", "sha": head},
            "additions": 1, "deletions": 0, "changed_files": 1, "commits": 1,
            "state": "open"}
    monkeypatch.setattr(headless, "fetch_pr_metadata", lambda *a, **k: meta)
    monkeypatch.setattr(headless, "fetch_pr_diff", lambda *a, **k:
                        "diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-x\n+y\n")
    monkeypatch.setattr(headless, "fetch_bot_login", lambda *a, **k: "air-bot")
    monkeypatch.setattr(headless, "fetch_issue_comments", lambda *a, **k: [])
    monkeypatch.setattr(headless, "fetch_pr_reviews", lambda *a, **k: [])
    monkeypatch.setattr(headless, "fetch_pr_review_comments", lambda *a, **k: [])
    # No real store/wiki/git: stage degrades pattern-blind, precomp returns empties.
    monkeypatch.setattr(headless, "stage_patterns", lambda *a, **k: (None, None, "mock"))
    # headless does `from review import compute_*` lazily, which reads the review
    # module's attrs at call time — so patch the review module itself.
    monkeypatch.setattr(review, "compute_file_statuses", lambda *a, **k: ("", []))
    for fn in ("compute_blame_summaries", "compute_churn_data", "compute_diff_check_warnings"):
        monkeypatch.setattr(review, fn, lambda *a, **k: "")

    def fake_run_agent(client, **kw):
        body = ("## Code Review\n\nLooks good.\n\n### Strengths\n- clean\n\n"
                f"No blockers.\n\nReviewed at: {head}\n") if kw.get("label") == "verifier" \
            else "## Code Review\n\nNo issues from this lens.\n"
        return {"text": body, "usage": {}, "turns": 1, "tool_calls": 0,
                "wall_s": 0.0, "stop": "end_turn"}

    monkeypatch.setattr(headless.agent_loop, "run_agent", fake_run_agent)
    args = types.SimpleNamespace(repo="o/r", pr_number=1, dry_run=True, closed=False)
    out = asyncio.run(headless.run_headless_review(args, "tok"))
    assert out["ok"] is True
    assert out["verdict"] == "APPROVE"  # clean verifier body, no conflict markers
    assert out.get("dry_run") is True


def test_stage_patterns_empty_store_cleans_up(tmp_path, monkeypatch):
    # Store exists but holds none of the pattern files -> no dir left, pattern-blind.
    monkeypatch.delenv("AIR_HEADLESS_PATTERNS", raising=False)
    monkeypatch.setattr(headless.memory_store, "get_store_id",
                        lambda repo, flow="review": "store_x")
    monkeypatch.setattr(headless.memory_store, "read_memory", lambda sid, path: None)
    rel, abs_, src = headless.stage_patterns("o/r", "alice", str(tmp_path), "tok")
    assert rel is None and "no pattern files" in src
    assert not (tmp_path / ".air-patterns").exists()
