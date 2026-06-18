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
