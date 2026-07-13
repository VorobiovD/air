#!/usr/bin/env python3
"""Multi-PAT gate-orphan fix: air's APPROVE must clear a stale CHANGES_REQUESTED
it left under a DIFFERENT rotated bot account, without ever touching a human's
review. Offline — fetch_pr_reviews / dismiss_review / _gh_request are mocked.

Run: python -m pytest managed/test-gate-orphan.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import github_client as gc  # noqa: E402
import review  # noqa: E402
from github_client import AIR_VERDICT_SENTINEL, _is_air_verdict, dismiss_stale_air_verdicts  # noqa: E402


def _rv(rid, login, state, body=""):
    return {"id": rid, "user": {"login": login}, "state": state, "body": body}


# --- identity: only air's own verdicts, never a human's ----------------------

def test_sentinel_identifies_air_any_account():
    # carried by a bot account air's allowlist doesn't even know about
    assert _is_air_verdict(_rv(1, "some-rotated-bot", "CHANGES_REQUESTED",
                               f"Changes requested — blah. {AIR_VERDICT_SENTINEL}"), frozenset())


def test_allowlist_identifies_legacy_presentinel():
    assert _is_air_verdict(_rv(1, "botC", "CHANGES_REQUESTED", "old verdict, no marker"),
                           frozenset({"botC"}))


def test_human_review_never_matches():
    # no sentinel, login not allowlisted — even if the body mentions "changes requested"
    assert not _is_air_verdict(_rv(1, "alice", "CHANGES_REQUESTED",
                                   "I'd request changes — please add a null check"),
                               frozenset({"botA"}))


# --- dismissal behavior ------------------------------------------------------

def _patch(monkeypatch, reviews):
    dismissed = []
    monkeypatch.setattr(gc, "fetch_pr_reviews", lambda r, p, t: reviews)
    monkeypatch.setattr(gc, "dismiss_review", lambda r, p, rid, t, m: dismissed.append(rid) or True)
    return dismissed


def test_dismisses_only_other_account_air_block(monkeypatch):
    reviews = [
        _rv(10, "botB", "CHANGES_REQUESTED", f"Changes requested. {AIR_VERDICT_SENTINEL}"),  # air, OTHER acct → dismiss
        _rv(11, "botA", "CHANGES_REQUESTED", f"Changes requested. {AIR_VERDICT_SENTINEL}"),  # air, CURRENT acct → skip (GitHub auto-supersedes)
        _rv(12, "carol", "CHANGES_REQUESTED", "human block, real finding"),                  # human → never
        _rv(13, "botB", "APPROVED", f"ok {AIR_VERDICT_SENTINEL}"),                            # not CR → skip
        _rv(14, "botB", "COMMENTED", f"note {AIR_VERDICT_SENTINEL}"),                         # not CR → skip
    ]
    dismissed = _patch(monkeypatch, reviews)
    n = dismiss_stale_air_verdicts("o/r", 1, "tok", current_login="botA", bot_logins=frozenset())
    assert n == 1 and dismissed == [10]


def test_legacy_orphan_via_allowlist(monkeypatch):
    reviews = [_rv(20, "botC", "CHANGES_REQUESTED", "pre-sentinel verdict")]
    dismissed = _patch(monkeypatch, reviews)
    n = dismiss_stale_air_verdicts("o/r", 1, "tok", current_login="botA", bot_logins=frozenset({"botC"}))
    assert n == 1 and dismissed == [20]


def test_never_dismisses_human_even_other_account(monkeypatch):
    reviews = [_rv(30, "dave", "CHANGES_REQUESTED", "fix the migration")]
    dismissed = _patch(monkeypatch, reviews)
    n = dismiss_stale_air_verdicts("o/r", 1, "tok", current_login="botA", bot_logins=frozenset({"botC"}))
    assert n == 0 and dismissed == []


def test_fetch_failure_is_nonfatal(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(gc, "fetch_pr_reviews", boom)
    assert dismiss_stale_air_verdicts("o/r", 1, "tok", "botA") == 0


# --- the verdict body carries the sentinel -----------------------------------

def test_submit_verdict_stamps_sentinel(monkeypatch):
    captured = {}

    class _Resp:
        ok = True
    def fake(method, url, *, token, **kw):
        captured.update(kw.get("json", {}))
        return _Resp()
    monkeypatch.setattr(gc, "_gh_request", fake)
    gc.submit_review_verdict("o/r", 1, "tok", "APPROVE", "Approved — 0 blockers.", "abc1234")
    assert AIR_VERDICT_SENTINEL in captured["body"]
    assert captured["event"] == "APPROVE" and captured["commit_id"] == "abc1234"


# --- allowlist sourcing (review._air_bot_logins) -----------------------------

def test_air_bot_logins_from_patmap(monkeypatch):
    monkeypatch.setenv("AIR_PAT_MAP", '{"botA":"A","botB":"B"}')
    monkeypatch.delenv("AIR_BOT_LOGINS", raising=False)
    assert review._air_bot_logins() == frozenset({"botA", "botB"})


def test_air_bot_logins_combines_sources(monkeypatch):
    monkeypatch.setenv("AIR_PAT_MAP", '{"botA":"A"}')
    monkeypatch.setenv("AIR_BOT_LOGINS", "botC, botD")
    assert review._air_bot_logins() == frozenset({"botA", "botC", "botD"})


def test_air_bot_logins_empty_when_unset(monkeypatch):
    monkeypatch.delenv("AIR_PAT_MAP", raising=False)
    monkeypatch.delenv("AIR_BOT_LOGINS", raising=False)
    assert review._air_bot_logins() == frozenset()


def test_air_bot_logins_tolerates_bad_json(monkeypatch):
    monkeypatch.setenv("AIR_PAT_MAP", "not-json")
    monkeypatch.delenv("AIR_BOT_LOGINS", raising=False)
    assert review._air_bot_logins() == frozenset()


# --- AIR_NO_APPROVE: include_own dismisses the current account's OWN block -----
# In advisory mode a clean re-review posts a COMMENT, which does NOT supersede the
# same account's prior CHANGES_REQUESTED — so include_own=True must dismiss it too,
# while still never touching a human's block or the just-posted COMMENT.

def test_include_own_dismisses_current_account_block(monkeypatch):
    reviews = [
        _rv(40, "botA", "CHANGES_REQUESTED", f"prior block. {AIR_VERDICT_SENTINEL}"),  # OWN air block → dismiss (include_own)
        _rv(41, "botB", "CHANGES_REQUESTED", f"other-acct block. {AIR_VERDICT_SENTINEL}"),  # other air acct → dismiss
        _rv(42, "erin", "CHANGES_REQUESTED", "human: fix the auth check"),               # human → never
        _rv(43, "botA", "COMMENTED", f"just-posted advisory note. {AIR_VERDICT_SENTINEL}"),  # our COMMENT → not CR → skip
    ]
    dismissed = _patch(monkeypatch, reviews)
    n = dismiss_stale_air_verdicts("o/r", 1, "tok", current_login="botA",
                                   bot_logins=frozenset(), include_own=True)
    assert n == 2 and dismissed == [40, 41]  # own + other air blocks; NOT the human, NOT the COMMENT


def test_include_own_false_still_skips_current_account(monkeypatch):
    # Default (normal mode): the current account's own CR is left to GitHub to supersede.
    reviews = [_rv(50, "botA", "CHANGES_REQUESTED", f"prior. {AIR_VERDICT_SENTINEL}")]
    dismissed = _patch(monkeypatch, reviews)
    n = dismiss_stale_air_verdicts("o/r", 1, "tok", current_login="botA",
                                   bot_logins=frozenset())  # include_own defaults False
    assert n == 0 and dismissed == []


def test_include_own_never_touches_human(monkeypatch):
    reviews = [_rv(60, "frank", "CHANGES_REQUESTED", "human block, no sentinel")]
    dismissed = _patch(monkeypatch, reviews)
    n = dismiss_stale_air_verdicts("o/r", 1, "tok", current_login="botA",
                                   bot_logins=frozenset(), include_own=True)
    assert n == 0 and dismissed == []


# --- reason-aware dismissal message ------------------------------------------

def _patch_msgs(monkeypatch, reviews):
    """Like _patch but captures (rid, message) so the wording can be asserted."""
    calls = []
    monkeypatch.setattr(gc, "fetch_pr_reviews", lambda r, p, t: reviews)
    monkeypatch.setattr(gc, "dismiss_review", lambda r, p, rid, t, m: calls.append((rid, m)) or True)
    return calls


def test_dismissal_message_is_reason_aware(monkeypatch):
    # Cross-account (PAT rotation OR a CLI verdict posted under a dev's own
    # account) and same-account (advisory include_own) get distinct wording —
    # a same-account dismissal must NOT falsely claim PAT rotation.
    reviews = [
        _rv(70, "botB", "CHANGES_REQUESTED", f"cross-account block. {AIR_VERDICT_SENTINEL}"),
        _rv(71, "botA", "CHANGES_REQUESTED", f"own advisory block. {AIR_VERDICT_SENTINEL}"),
    ]
    calls = _patch_msgs(monkeypatch, reviews)
    n = dismiss_stale_air_verdicts("o/r", 1, "tok", current_login="botA",
                                   bot_logins=frozenset(), include_own=True)
    assert n == 2
    msgs = dict(calls)
    assert "different air-posting account" in msgs[70]
    assert "PAT rotation or a local CLI review" in msgs[70]
    assert "advisory-mode re-review" in msgs[71]
    assert "PAT rotation" not in msgs[71]  # same-account must not claim rotation


# --- resolve_verdict_event / no_approve_enabled (AIR_NO_APPROVE) --------------
from verdict import resolve_verdict_event, no_approve_enabled  # noqa: E402


def test_resolve_verdict_normal_mode(monkeypatch):
    monkeypatch.delenv("AIR_NO_APPROVE", raising=False)
    assert resolve_verdict_event(True) == "REQUEST_CHANGES"
    assert resolve_verdict_event(False) == "APPROVE"
    assert no_approve_enabled() is False


def test_resolve_verdict_no_approve_mode(monkeypatch):
    for val in ("1", "true", "yes"):
        monkeypatch.setenv("AIR_NO_APPROVE", val)
        assert resolve_verdict_event(True) == "REQUEST_CHANGES"   # still blocks on blockers
        assert resolve_verdict_event(False) == "COMMENT"          # never approves
        assert no_approve_enabled() is True
    monkeypatch.setenv("AIR_NO_APPROVE", "0")
    assert resolve_verdict_event(False) == "APPROVE"
