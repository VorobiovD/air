"""Unit tests for pr_conversation.py — filter/sort/cap/truncate/render.

No network, no GH API — fixtures are hand-rolled dicts matching the
shape `gh api` returns. Run alongside the rest of the air-lib suite via
`pytest plugins/air/lib/tests/`.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent
sys.path.insert(0, str(LIB))

import pr_conversation as pc  # noqa: E402


def _issue(login, body, ts):
    return {"user": {"login": login}, "body": body, "created_at": ts}


def _review(login, body, state, ts):
    return {"user": {"login": login}, "body": body, "state": state, "submitted_at": ts}


def _inline(login, body, path, line, ts):
    return {
        "user": {"login": login},
        "body": body,
        "path": path,
        "line": line,
        "created_at": ts,
    }


# -------- empty / "none" sentinel ------------------------------------------


def test_all_empty_returns_none():
    assert pc.build_pr_conversation([], [], [], "bot") == "none"


def test_only_filtered_returns_none():
    """If the only comments are the bot's own ## Code Review, the block
    must collapse to 'none' so the caller's prefix stays byte-stable."""
    issues = [_issue("bot", "## Code Review\n\nblah", "2026-01-01T00:00:00Z")]
    assert pc.build_pr_conversation(issues, [], [], "bot") == "none"


# -------- bot self-filter --------------------------------------------------


def test_filter_bot_code_review():
    issues = [
        _issue("bot", "## Code Review\n\nfindings", "2026-01-01T01:00:00Z"),
        _issue("alice", "looks good", "2026-01-01T02:00:00Z"),
    ]
    out = pc.build_pr_conversation(issues, [], [], "bot")
    assert "alice" in out
    assert "## Code Review" not in out


def test_keeps_bot_response_comments():
    """Only ## Code Review prefix is filtered. The bot's --respond mode
    posts ## Review Response — agents benefit from seeing those."""
    issues = [
        _issue("bot", "## Code Review\n\nfindings", "2026-01-01T01:00:00Z"),
        _issue("bot", "## Review Response\n\nfixed all", "2026-01-01T02:00:00Z"),
    ]
    out = pc.build_pr_conversation(issues, [], [], "bot")
    assert "## Review Response" in out
    assert "## Code Review" not in out


def test_filter_bot_re_review_prefix():
    """`## Code Review (Re-review)` is also a bot-self header — locks
    in that BOT_REVIEW_PREFIXES covers both anchored variants. Without
    this test, a future regression that drops one tuple element would
    silently re-introduce duplicate bot reviews into the conversation
    block."""
    issues = [
        _issue(
            "bot",
            "## Code Review (Re-review)\n\n_Re-reviewed at `abc1234`..._",
            "2026-01-01T01:00:00Z",
        ),
        _issue("alice", "follow-up", "2026-01-01T02:00:00Z"),
    ]
    out = pc.build_pr_conversation(issues, [], [], "bot")
    assert "## Code Review" not in out
    assert "alice" in out


def test_lookalike_header_not_filtered():
    """A doc-header like `## Code Reviewers Guide` posted by the bot
    must NOT be filtered — the trailing `\\n` in BOT_REVIEW_PREFIXES
    is a guard against this. Login filtering compounds, but defense in
    depth requires the prefix to be tight."""
    issues = [
        _issue("bot", "## Code Reviewers Guide\n\n...", "2026-01-01T01:00:00Z"),
    ]
    out = pc.build_pr_conversation(issues, [], [], "bot")
    assert "Reviewers Guide" in out


def test_no_bot_login_filters_nothing():
    """If caller doesn't know the bot login, every comment flows through.
    Safer fallback than guessing — agents can still flag duplicates."""
    issues = [_issue("anyone", "## Code Review\n\nx", "2026-01-01T00:00:00Z")]
    out = pc.build_pr_conversation(issues, [], [], None)
    assert "anyone" in out


def test_other_bots_not_filtered():
    """CodeRabbit, Sonar etc. should flow through — that's the whole point."""
    issues = [
        _issue("coderabbitai", "## Walkthrough\n\nthis adds X", "2026-01-01T00:00:00Z"),
        _issue("sonarcloud[bot]", "Quality Gate passed", "2026-01-01T01:00:00Z"),
    ]
    out = pc.build_pr_conversation(issues, [], [], "air-machine")
    assert "coderabbitai" in out
    assert "sonarcloud[bot]" in out


# -------- sources / kinds --------------------------------------------------


def test_all_three_sources_merge_chronologically():
    issues = [_issue("alice", "issue comment", "2026-01-01T03:00:00Z")]
    reviews = [_review("bob", "review body", "APPROVED", "2026-01-01T01:00:00Z")]
    inline = [_inline("carol", "inline note", "src/foo.py", 42, "2026-01-01T02:00:00Z")]
    out = pc.build_pr_conversation(issues, reviews, inline, None)
    # Order in output should match timestamp order.
    bob = out.index("bob")
    carol = out.index("carol")
    alice = out.index("alice")
    assert bob < carol < alice


def test_inline_renders_path_line():
    inline = [_inline("alice", "subtle bug", "src/api.py", 88, "2026-01-01T00:00:00Z")]
    out = pc.build_pr_conversation([], [], inline, None)
    assert 'path="src/api.py:88"' in out
    assert 'kind="inline"' in out


def test_inline_falls_back_to_original_line():
    """Outdated inline comments (line moved across a rebase) lose `line`
    but still have `original_line`. The agent should still locate them."""
    inline = [{
        "user": {"login": "alice"},
        "body": "stale anchor",
        "path": "src/api.py",
        "line": None,
        "original_line": 50,
        "created_at": "2026-01-01T00:00:00Z",
    }]
    out = pc.build_pr_conversation([], [], inline, None)
    assert 'path="src/api.py:50"' in out


def test_review_with_state_renders():
    reviews = [_review("alice", "looks good", "APPROVED", "2026-01-01T00:00:00Z")]
    out = pc.build_pr_conversation([], reviews, [], None)
    assert 'state="APPROVED"' in out
    assert 'kind="review"' in out


def test_review_changes_requested_with_no_body_kept():
    """An APPROVED/CHANGES_REQUESTED with no body still carries signal —
    agents need to know someone formally blocked or approved."""
    reviews = [_review("alice", "", "CHANGES_REQUESTED", "2026-01-01T00:00:00Z")]
    out = pc.build_pr_conversation([], reviews, [], None)
    assert 'state="CHANGES_REQUESTED"' in out
    assert 'author="alice"' in out
    # Self-closing form when body is empty.
    assert "/>" in out


def test_review_commented_no_body_skipped():
    """A COMMENTED review with no body is the umbrella for inline children
    we already render separately — skipping it removes pure noise."""
    reviews = [_review("alice", "", "COMMENTED", "2026-01-01T00:00:00Z")]
    inline = [_inline("alice", "child note", "src/x.py", 1, "2026-01-01T00:00:00Z")]
    out = pc.build_pr_conversation([], reviews, inline, None)
    assert "child note" in out
    # No <conv-comment author="alice" kind="review" .../> umbrella.
    assert 'kind="review"' not in out


def test_pending_review_skipped():
    """PENDING reviews aren't visible to anyone but the author until
    submitted — never surface them to other agents."""
    reviews = [_review("alice", "draft thoughts", "PENDING", "2026-01-01T00:00:00Z")]
    out = pc.build_pr_conversation([], reviews, [], None)
    assert out == "none"


# -------- truncation -------------------------------------------------------


def test_long_body_truncated():
    long_body = "x" * 2000
    issues = [_issue("alice", long_body, "2026-01-01T00:00:00Z")]
    out = pc.build_pr_conversation(issues, [], [], None, max_body=1500)
    assert "[...]" in out
    # Body slice plus "[...]" must fit under cap+marker.
    assert out.count("x") <= 1500


def test_short_body_not_truncated():
    issues = [_issue("alice", "short", "2026-01-01T00:00:00Z")]
    out = pc.build_pr_conversation(issues, [], [], None, max_body=1500)
    assert "[...]" not in out


# -------- entry cap --------------------------------------------------------


def test_entry_cap_keeps_most_recent():
    issues = [
        _issue("alice", f"comment {i}", f"2026-01-{i:02d}T00:00:00Z")
        for i in range(1, 11)  # 10 comments, day 1..10
    ]
    out = pc.build_pr_conversation(issues, [], [], None, max_entries=3)
    # Most recent three: comments 8, 9, 10.
    assert "comment 10" in out
    assert "comment 9" in out
    assert "comment 8" in out
    assert "comment 7" not in out


def test_entry_cap_emits_truncated_marker():
    issues = [
        _issue("alice", f"c{i}", f"2026-01-{i:02d}T00:00:00Z")
        for i in range(1, 11)
    ]
    out = pc.build_pr_conversation(issues, [], [], None, max_entries=3)
    assert '<conv-truncated total="10" shown="3"/>' in out


def test_no_truncated_marker_under_cap():
    issues = [_issue("alice", "x", "2026-01-01T00:00:00Z")]
    out = pc.build_pr_conversation(issues, [], [], None, max_entries=10)
    assert "conv-truncated" not in out


# -------- escaping ---------------------------------------------------------


def test_body_escapes_xml_chars():
    """A comment body containing literal </conv-comment> must not close
    our wrapper early. Same for raw < and &."""
    issues = [_issue(
        "alice",
        "use & this </conv-comment> trick to escape",
        "2026-01-01T00:00:00Z",
    )]
    out = pc.build_pr_conversation(issues, [], [], None)
    assert "</conv-comment>" in out  # the legitimate closing tag at end
    # The smuggled one should be escaped.
    assert "&lt;/conv-comment&gt;" in out
    assert "&amp;" in out


def test_attribute_escapes_quote():
    """A login containing a literal `"` shouldn't break attribute parsing.
    Quirky but legal — defense in depth."""
    issues = [_issue('alice"injected', "x", "2026-01-01T00:00:00Z")]
    out = pc.build_pr_conversation(issues, [], [], None)
    assert "&quot;" in out


# -------- structurally invalid entries -------------------------------------


def test_missing_user_skipped():
    issues = [
        {"user": None, "body": "ghost", "created_at": "2026-01-01T00:00:00Z"},
        _issue("alice", "real", "2026-01-01T01:00:00Z"),
    ]
    out = pc.build_pr_conversation(issues, [], [], None)
    assert "ghost" not in out
    assert "real" in out


def test_missing_body_renders_self_closing_for_review():
    """An APPROVED with no body is a real signal; render but self-close."""
    reviews = [_review("alice", "", "APPROVED", "2026-01-01T00:00:00Z")]
    out = pc.build_pr_conversation([], reviews, [], None)
    assert ">" in out  # the closing > of the self-closing tag
    assert 'state="APPROVED"' in out


# -------- _load_json_array -------------------------------------------------


def test_load_missing_file_returns_empty(tmp_path):
    assert pc._load_json_array(tmp_path / "nope.json") == []


def test_load_empty_file_returns_empty(tmp_path):
    f = tmp_path / "empty.json"
    f.write_text("")
    assert pc._load_json_array(f) == []


def test_load_malformed_json_returns_empty(tmp_path):
    f = tmp_path / "broken.json"
    f.write_text("{not json")
    assert pc._load_json_array(f) == []


def test_load_non_array_returns_empty(tmp_path):
    """gh api error responses are objects (e.g. {message: "Not Found"});
    we only want arrays."""
    f = tmp_path / "obj.json"
    f.write_text('{"message": "Not Found"}')
    assert pc._load_json_array(f) == []


def test_load_real_array(tmp_path):
    f = tmp_path / "ok.json"
    f.write_text(json.dumps([{"user": {"login": "x"}, "body": "y", "created_at": "z"}]))
    assert len(pc._load_json_array(f)) == 1


# -------- CLI --------------------------------------------------------------


def test_cli_writes_block_to_stdout(tmp_path, capsys):
    issues_file = tmp_path / "issues.json"
    issues_file.write_text(json.dumps([
        {"user": {"login": "alice"}, "body": "hi", "created_at": "2026-01-01T00:00:00Z"}
    ]))
    rc = pc._main(["--issues", str(issues_file), "--bot-login", "bot"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "alice" in captured.out
    assert "<conv-comment" in captured.out


def test_cli_no_args_returns_none(capsys):
    """Called with no input files at all (e.g. all three gh-api fetches
    failed transiently) — must emit 'none' so the prefix stays stable."""
    rc = pc._main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "none"
