"""Network-free unit tests for the headless (messages-api) mode.

Locks the regressions the dogfood review flagged on code paths the dry-run A/B
never exercised: the post-path call signatures, the CI exit-code propagation on
failure, and frontmatter-parse robustness.
Imports review.py / headless.py, so it needs the managed deps (CI installs them).
"""
import asyncio
import inspect
import os
import sys
import types
from pathlib import Path

import pytest
import requests

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


def _mock_store(monkeypatch, listing, bodies):
    """Wire memory_store for the list-once + retrieve-by-id staging path.
    `listing` maps path -> id; `bodies` maps id -> content."""
    monkeypatch.setattr(headless.memory_store, "get_store_id",
                        lambda repo, flow="review": "store_x")
    monkeypatch.setattr(headless.memory_store, "list_memories",
                        lambda sid, prefix="/": {p: {"id": i, "content_sha256": "s"}
                                                 for p, i in listing.items()})

    class _Mem:
        def retrieve(self, mem_id, memory_store_id=None):
            return types.SimpleNamespace(content=bodies[mem_id])

    class _Client:
        beta = types.SimpleNamespace(
            memory_stores=types.SimpleNamespace(memories=_Mem()))

    monkeypatch.setattr(headless.memory_store, "client", lambda: _Client())


def test_stage_patterns_store_path(tmp_path, monkeypatch):
    monkeypatch.delenv("AIR_HEADLESS_PATTERNS", raising=False)
    _mock_store(monkeypatch, listing={
        "/authors/alice.md": "a1",
        headless.memory_store.GLOSSARY_PATH: "g1",
        headless.memory_store.ACCEPTED_PATTERNS_PATH: "ac1",
    }, bodies={"a1": "alice patterns", "g1": "glossary body", "ac1": "accepted body"})
    rel, abs_, src = headless.stage_patterns("o/r", "alice", str(tmp_path), "secret-tok")
    assert rel == ".air-patterns"
    d = tmp_path / ".air-patterns"
    assert (d / "author-patterns.md").read_text() == "alice patterns"
    assert (d / "glossary.md").read_text() == "glossary body"
    assert (d / "accepted-patterns.md").read_text() == "accepted body"
    assert "store_x" in src
    # Only files present in the listing are staged (service/common/severity/profile absent).
    assert not (d / "service-patterns.md").exists()


def test_stage_patterns_wiki_path_no_token_leak(tmp_path, monkeypatch):
    monkeypatch.delenv("AIR_HEADLESS_PATTERNS", raising=False)
    monkeypatch.setattr(headless.memory_store, "get_store_id",
                        lambda repo, flow="review": None)

    def fake_clone(argv, **kw):
        dest = argv[-1]  # `git clone --depth 1 <url> <dest>`
        with open(os.path.join(dest, "REVIEW.md"), "w") as fh:
            fh.write("review patterns")
        with open(os.path.join(dest, "GLOSSARY.md"), "w") as fh:
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
    # Hermetic: no store (wiki backend, mode stays full — empty issue comments mean
    # no prior detected anyway), stage degrades pattern-blind, precomp empties.
    monkeypatch.setattr(headless.memory_store, "get_store_id", lambda *a, **k: None)
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
    _mock_store(monkeypatch, listing={}, bodies={})  # empty store: list returns nothing
    rel, abs_, src = headless.stage_patterns("o/r", "alice", str(tmp_path), "tok")
    assert rel is None and "no pattern files" in src
    assert not (tmp_path / ".air-patterns").exists()


# ---- P2 re-review path -------------------------------------------------------

def _prior_comment(prior_sha):
    """A bot-authored prior `## Code Review` with a `Reviewed at:` footer — what
    find_prior_review matches and extract_reviewed_at_sha parses. No findings, so the
    real build_carry_forward_ledger returns [] (pin skipped) unless a test mocks it."""
    return {"id": 11, "user": {"login": "air-bot"},
            "body": f"## Code Review\n\nNo issues.\n\nReviewed at: {prior_sha}\n"}


def _rereview_run(monkeypatch, tmp_path, *, comments, inter_diff=None, inter_exc=None,
                  head="a" * 40, fresh=False, ledger=None, pin=None,
                  ledger_pin_env=None, ledger_calls=None, full_diff=None, codex=None,
                  no_codex=False):
    """Drive run_headless_review (dry-run) for re-review-path tests. Returns
    (result, calls) where calls counts which diff fetcher fired. inter_diff is the
    fetch_inter_diff RETURN (str / "" / None); inter_exc, if set, makes it RAISE.
    ledger_pin_env sets AIR_LEDGER_PIN (else deleted → enabled). ledger_calls, if a
    list, spies build_carry_forward_ledger (records each call, returns ledger or [])."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("AIR_TARGET_REPO", str(tmp_path))
    if ledger_pin_env is None:
        monkeypatch.delenv("AIR_LEDGER_PIN", raising=False)  # default → pin enabled
    else:
        monkeypatch.setenv("AIR_LEDGER_PIN", ledger_pin_env)
    meta = {"number": 7, "user": {"login": "alice"}, "title": "T", "body": "b",
            "base": {"ref": "main"}, "head": {"ref": "feat", "sha": head},
            "additions": 1, "deletions": 0, "changed_files": 1, "commits": 2,
            "state": "open"}
    calls = {"inter": 0, "full": 0}
    monkeypatch.setattr(headless, "fetch_pr_metadata", lambda *a, **k: meta)
    monkeypatch.setattr(headless, "fetch_bot_login", lambda *a, **k: "air-bot")
    monkeypatch.setattr(headless, "fetch_issue_comments", lambda *a, **k: comments)
    monkeypatch.setattr(headless, "fetch_pr_reviews", lambda *a, **k: [])
    monkeypatch.setattr(headless, "fetch_pr_review_comments", lambda *a, **k: [])
    monkeypatch.setattr(headless.memory_store, "get_store_id", lambda *a, **k: None)
    monkeypatch.setattr(headless, "stage_patterns", lambda *a, **k: (None, None, "mock"))
    monkeypatch.setattr(review, "compute_file_statuses", lambda *a, **k: ("", []))
    for fn in ("compute_blame_summaries", "compute_churn_data", "compute_diff_check_warnings"):
        monkeypatch.setattr(review, fn, lambda *a, **k: "")

    def _full(*a, **k):
        calls["full"] += 1
        return full_diff or "diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-x\n+y\n"

    def _inter(*a, **k):
        calls["inter"] += 1
        if inter_exc is not None:
            raise inter_exc
        return inter_diff

    monkeypatch.setattr(headless, "fetch_pr_diff", _full)
    monkeypatch.setattr(headless, "fetch_inter_diff", _inter)
    if ledger_calls is not None:  # spy: detect whether the ledger is built at all
        def _spy_ledger(*a, **k):
            ledger_calls.append(True)
            return ledger or []
        monkeypatch.setattr(headless, "build_carry_forward_ledger", _spy_ledger)
    elif ledger is not None:
        monkeypatch.setattr(headless, "build_carry_forward_ledger", lambda *a, **k: ledger)
    if pin is not None:
        monkeypatch.setattr(headless, "pin_and_resurrect", pin)

    # Codex: when `codex` text is given, satisfy the gate (binary + key + base sha)
    # and mock the session to return that text. Else leave it disabled.
    if codex is not None:
        meta["base"]["sha"] = "c" * 40
        monkeypatch.setenv("OPENAI_API_KEY", "sk-codex")
        monkeypatch.setattr(headless.shutil, "which", lambda name: "/usr/bin/codex")

        async def _fake_codex(repo, base):
            return codex
        # run_codex_session is lazy-imported from review inside the function, so
        # patch the review module attr (like compute_*), not a headless attr.
        monkeypatch.setattr(review, "run_codex_session", _fake_codex)
    else:
        monkeypatch.setattr(headless.shutil, "which", lambda name: None)  # no codex binary

    def fake_run_agent(client, **kw):
        if kw.get("label") == "verifier":
            calls["verifier_task"] = kw.get("task", "")
            body = f"## Code Review\n\nClean.\n\nNo blockers.\n\nReviewed at: {head}\n"
        else:
            body = "## Code Review\n\nlens.\n"
        return {"text": body, "usage": {}, "turns": 1, "tool_calls": 0, "wall_s": 0.0, "stop": "end_turn"}

    monkeypatch.setattr(headless.agent_loop, "run_agent", fake_run_agent)
    args = types.SimpleNamespace(repo="o/r", pr_number=7, dry_run=True, closed=False, fresh=fresh,
                                 no_codex=no_codex)
    return asyncio.run(headless.run_headless_review(args, "tok")), calls


def test_rereview_uses_inter_diff(tmp_path, monkeypatch):
    out, calls = _rereview_run(monkeypatch, tmp_path, comments=[_prior_comment("b" * 40)],
                               inter_diff="diff --git a/f b/f\n@@ -1 +1 @@\n-a\n+b\n")
    assert out["ok"] and out["verdict"] in ("APPROVE", "REQUEST_CHANGES")
    assert calls["inter"] == 1 and calls["full"] == 0  # re-review reviewed the inter-diff


def test_rereview_inter_none_falls_back_to_full(tmp_path, monkeypatch):
    out, calls = _rereview_run(monkeypatch, tmp_path, comments=[_prior_comment("b" * 40)],
                               inter_diff=None)
    assert out["ok"] and calls["inter"] == 1 and calls["full"] == 1  # None → full review


def test_rereview_empty_inter_skips(tmp_path, monkeypatch):
    out, calls = _rereview_run(monkeypatch, tmp_path, comments=[_prior_comment("b" * 40)],
                               inter_diff="")
    assert out["ok"] and out["verdict"] is None and "no changes" in out["reason"]
    assert calls["full"] == 0  # empty successful compare → skip, no full fetch


def test_rereview_inter_raises_falls_back_to_full(tmp_path, monkeypatch):
    # fetch_inter_diff RAISES on retry exhaustion (not None) — the except branch must
    # catch RequestException and fall back to a full review (the return-None branch is
    # covered separately; this exercises the distinct raise path).
    out, calls = _rereview_run(monkeypatch, tmp_path, comments=[_prior_comment("b" * 40)],
                               inter_exc=requests.exceptions.ConnectionError("timeout"))
    assert out["ok"] and calls["inter"] == 1 and calls["full"] == 1  # raise → full


def test_rereview_ledger_pin_disabled_by_killswitch(tmp_path, monkeypatch):
    # AIR_LEDGER_PIN=0 → neither the ledger build nor pin_and_resurrect runs.
    ledger_calls, pin_calls = [], []

    def spy_pin(body, ledger):
        pin_calls.append(True)
        return body, []

    out, _ = _rereview_run(monkeypatch, tmp_path, comments=[_prior_comment("b" * 40)],
                           inter_diff="diff --git a/f b/f\n@@ -1 +1 @@\n-a\n+b\n",
                           ledger_pin_env="0", ledger_calls=ledger_calls, pin=spy_pin)
    assert ledger_calls == [] and pin_calls == [] and out["ok"]  # kill-switch gates both


# ---- P3 UI/copy lens dispatch ------------------------------------------------

_UI_DIFF = ("diff --git a/src/Button.tsx b/src/Button.tsx\n"
           "--- a/src/Button.tsx\n+++ b/src/Button.tsx\n@@ -1 +1 @@\n-old copy\n+new copy\n")
_BACKEND_DIFF = ("diff --git a/managed/api.py b/managed/api.py\n"
                "--- a/managed/api.py\n+++ b/managed/api.py\n@@ -1 +1 @@\n-x\n+y\n")


def test_ui_lens_dispatched_on_user_facing_diff(tmp_path, monkeypatch):
    # A user-facing (.tsx) diff → the conditional UI/copy reviewer joins the fan-out.
    out, _ = _rereview_run(monkeypatch, tmp_path, comments=[], full_diff=_UI_DIFF)
    assert "air-ui-copy-reviewer" in out["specialists"]


def test_ui_lens_skipped_on_backend_diff(tmp_path, monkeypatch):
    # Backend-only (.py) diff, no store-declared copy globs → UI lens not dispatched
    # ($0 added); only the 4 core specialists run.
    out, _ = _rereview_run(monkeypatch, tmp_path, comments=[], full_diff=_BACKEND_DIFF)
    assert "air-ui-copy-reviewer" not in out["specialists"]
    assert set(out["specialists"]) == {
        "air-code-reviewer", "air-simplify", "air-security-auditor", "air-git-history-reviewer"}


# ---- P3 Codex external second-opinion ----------------------------------------

def test_codex_findings_folded_into_verifier(tmp_path, monkeypatch):
    # When codex is set up (binary + key + base sha), its findings are folded into
    # the verifier's input as an untrusted-wrapped external-opinion block.
    out, calls = _rereview_run(monkeypatch, tmp_path, comments=[],
                               codex="CODEX-FLAG: unchecked array index at f.py:12")
    vt = calls["verifier_task"]
    assert "external second opinion" in vt
    assert "CODEX-FLAG: unchecked array index" in vt
    assert "<untrusted-tool-output>" in vt  # framed untrusted like the specialists
    assert out["ok"]


def test_codex_absent_when_not_configured(tmp_path, monkeypatch):
    # No codex binary (default) → no codex block in the verifier input.
    out, calls = _rereview_run(monkeypatch, tmp_path, comments=[])
    assert "external second opinion" not in calls["verifier_task"] and out["ok"]


def test_codex_disabled_by_no_codex_flag(tmp_path, monkeypatch):
    # --no-codex wins even when the binary + key + base sha are all present.
    out, calls = _rereview_run(monkeypatch, tmp_path, comments=[],
                               codex="should-not-appear", no_codex=True)
    assert "external second opinion" not in calls["verifier_task"]
    assert "should-not-appear" not in calls["verifier_task"] and out["ok"]


# ---- P4 AIR_EXPECTED_REVIEWER identity assertion -----------------------------

def test_expected_reviewer_mismatch_fails_at_zero_cost(tmp_path, monkeypatch):
    # Wrong token owner (bot_login is "air-bot") → refuse to run, before any agent.
    monkeypatch.setenv("AIR_EXPECTED_REVIEWER", "someone-else")
    out, _ = _rereview_run(monkeypatch, tmp_path, comments=[])
    assert out["ok"] is False and "identity mismatch" in out["reason"]
    assert out["cost"] == 0.0  # failed pre-spend


def test_expected_reviewer_match_proceeds(tmp_path, monkeypatch):
    # Matching reviewer → review proceeds normally.
    monkeypatch.setenv("AIR_EXPECTED_REVIEWER", "air-bot")
    out, _ = _rereview_run(monkeypatch, tmp_path, comments=[])
    assert out["ok"] is True and out["verdict"] == "APPROVE"


def test_already_reviewed_at_head_skips(tmp_path, monkeypatch):
    out, calls = _rereview_run(monkeypatch, tmp_path, comments=[_prior_comment("a" * 40)],
                               inter_diff="x", head="a" * 40)
    assert out["ok"] and out["verdict"] is None and "already reviewed" in out["reason"]
    assert calls["inter"] == 0 and calls["full"] == 0  # returned before diff selection


def test_fresh_flag_forces_full_despite_prior(tmp_path, monkeypatch):
    out, calls = _rereview_run(monkeypatch, tmp_path, comments=[_prior_comment("b" * 40)],
                               inter_diff="x", fresh=True)
    assert out["ok"] and calls["inter"] == 0 and calls["full"] == 1  # --fresh ignores the prior


def test_rereview_ledger_pin_wired(tmp_path, monkeypatch):
    seen = {}

    def fake_pin(body, ledger):
        seen["ledger"] = ledger
        return body + "\n[pinned]", ["[pin] test applied"]

    # Ledger entries carry the attrs build_verifier_task renders (num/prior_severity/change).
    mock_ledger = [types.SimpleNamespace(num=1, prior_severity="blocker", change="UNCHANGED")]
    out, _ = _rereview_run(monkeypatch, tmp_path, comments=[_prior_comment("b" * 40)],
                           inter_diff="diff --git a/f b/f\n@@ -1 +1 @@\n-a\n+b\n",
                           ledger=mock_ledger, pin=fake_pin)
    # Re-review built a (non-empty) ledger and threaded the SAME list into pin_and_resurrect.
    assert seen.get("ledger") is mock_ledger and out["ok"]


# ---- P4 gate completeness: backfill a missing verdict on the at-head skip ----

def test_athead_skip_backfills_missing_verdict(tmp_path, monkeypatch):
    # The at-head skip must repair an orphaned-comment state (comment posted, verdict
    # lost to a kill between the two POSTs) by calling _backfill_verdict_if_missing with
    # exactly the values review.py passes. Spying on review._backfill_verdict_if_missing
    # (the lazy-import source) also validates the import edit — a typo'd name would raise
    # AttributeError here / ImportError on the lazy import.
    calls = {}

    def spy(args, head_sha, prior, *, bot_login, pr_state, pr_author, token):
        calls["kw"] = dict(head_sha=head_sha, prior_id=prior.get("id"), bot_login=bot_login,
                           pr_state=pr_state, pr_author=pr_author, token=token)

    monkeypatch.setattr(review, "_backfill_verdict_if_missing", spy)
    out, _ = _rereview_run(monkeypatch, tmp_path, comments=[_prior_comment("a" * 40)],
                           head="a" * 40)  # prior_sha == head → at-head skip fires
    assert out["ok"] and out["verdict"] is None and "already reviewed" in out["reason"]
    assert calls["kw"] == dict(head_sha="a" * 40, prior_id=11, bot_login="air-bot",
                               pr_state="open", pr_author="alice", token="tok")


def test_backfill_not_called_on_normal_rereview(tmp_path, monkeypatch):
    # Scope guard: backfill fires ONLY on the at-head skip, never on a real re-review
    # delta (prior_sha != head_sha → the review proceeds normally and posts a verdict).
    n = {"backfill": 0}

    def _count_backfill(*a, **k):
        n["backfill"] += 1

    monkeypatch.setattr(review, "_backfill_verdict_if_missing", _count_backfill)
    out, calls = _rereview_run(monkeypatch, tmp_path, comments=[_prior_comment("b" * 40)],
                               inter_diff="diff --git a/f b/f\n@@ -1 +1 @@\n-a\n+b\n",
                               head="a" * 40)
    assert out["ok"] and calls["inter"] == 1 and n["backfill"] == 0


def test_usage_telemetry_reports_per_agent_and_cache_ratio():
    # The cost-telemetry helper must surface per-agent tokens + the aggregate
    # cache-read ratio (the number that answers "is the 1h cache giving cross-agent
    # reuse?"). cache-read% = cr / (cr + cw + in).
    rows = [
        ("code-reviewer", "sonnet", {"input_tokens": 1000, "output_tokens": 500,
                                     "cache_creation_input_tokens": 2000,
                                     "cache_read_input_tokens": 7000}),
        ("verifier", "sonnet", {"input_tokens": 0, "output_tokens": 200,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 9000}),
    ]
    lines = []
    headless._log_usage_telemetry(rows, log=lines.append)
    assert any("code-reviewer" in l and "cr=" in l for l in lines)   # per-agent line
    agg = [l for l in lines if "TOTAL" in l][0]
    # cr=16000, cw=2000, in=1000 → 16000/19000 = 84%
    assert "cache-read 84%" in agg


def test_usage_telemetry_handles_zero_and_empty():
    # No tokens at all (e.g. an all-failed run) must not ZeroDivisionError.
    lines = []
    headless._log_usage_telemetry([("x", "haiku", {})], log=lines.append)
    assert any("cache-read 0%" in l for l in lines)


def test_usage_telemetry_guards_none_token_fields():
    # The SDK can report a token field as None (present, not absent); a bare
    # f"{None:>7}" raises TypeError under an alignment spec and would crash the
    # telemetry before the complete line. Guard with `or 0`.
    lines = []
    headless._log_usage_telemetry(
        [("x", "sonnet", {"input_tokens": None, "output_tokens": 5,
                          "cache_creation_input_tokens": None,
                          "cache_read_input_tokens": 90})],
        log=lines.append)
    assert any("[cost] x" in l for l in lines)            # row printed, no TypeError
    assert any("cache-read" in l for l in lines)          # aggregate printed


# ---- auto cache-TTL selection + write-multiplier pricing ----

def test_choose_cache_ttl_auto_and_override(monkeypatch):
    monkeypatch.delenv("AIR_HEADLESS_CACHE_TTL", raising=False)
    small = "diff --git a/f b/f\n@@ -1 +1 @@\n-a\n+b\n"
    assert headless._choose_cache_ttl(3, small) == "5m"        # small PR → cheap 5m
    assert headless._choose_cache_ttl(25, small) == "1h"       # many files → safe 1h
    assert headless._choose_cache_ttl(2, "x" * 200000) == "1h"  # big diff → 1h
    monkeypatch.setenv("AIR_HEADLESS_CACHE_TTL", "1h")
    assert headless._choose_cache_ttl(2, small) == "1h"        # override forces 1h
    monkeypatch.setenv("AIR_HEADLESS_CACHE_TTL", "5m")
    assert headless._choose_cache_ttl(50, "x" * 999999) == "5m"  # override beats heavy


def test_usage_cost_write_mult():
    al = headless.agent_loop
    u = {"cache_creation_input_tokens": 1_000_000}  # 1M cache-write tokens, sonnet
    assert abs(al.usage_cost(u, "sonnet", 2.0) - 6.0) < 1e-6    # 1h: 2x * $3/MTok
    assert abs(al.usage_cost(u, "sonnet", 1.25) - 3.75) < 1e-6  # 5m: 1.25x * $3/MTok
    assert al.cache_write_mult("5m") == 1.25 and al.cache_write_mult("1h") == 2.0
    assert al.cache_write_mult("weird") == 2.0                  # unknown → safe default


def test_analyze_cache_ttl_reprices_and_flags_misses(tmp_path):
    import analyze_cache_ttl as az
    log = tmp_path / "run.log"
    log.write_text(
        "[headless] cache TTL: 1h\n"
        "  [turn] code-reviewer t=1 tc=2 gap=20.0s in=100 out=0 cw=50000 cr=0\n"
        "  [turn] code-reviewer t=2 tc=0 gap=400.0s in=0 out=0 cw=0 cr=100000\n"
        "[headless] complete in 60.0s\n")
    r = az.analyze(str(log))
    assert r["turns"] == 2 and r["miss_turns"] == 1     # the 400s-gap read would expire on 5m
    assert r["miss_pct"] == 100.0
    # miss re-write (1.25x) >> warm read (0.1x), so on this miss-heavy run 5m costs MORE
    assert r["c1h"] < r["c5m"]
