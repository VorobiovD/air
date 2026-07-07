"""Offline tests for the out-of-band scheduled learn driver (learn_cron.py).

Network-free: store listing, counter reads, and run_headless_learn are faked.
Exercises due-detection (the same meta predicate reviews use), the archived /
non-air / locked / filter skips, and dry-run pass-through.
"""

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import learn_cron as C  # noqa: E402
import meta  # noqa: E402 (plugins/air/lib, on path via learn_cron)
import types


def _fake_client():
    ns = types.SimpleNamespace
    return ns(beta=ns(memory_stores=ns(list=lambda **k: None)))


def _meta(reviews_since, locked=False):
    m = meta._default_meta()
    m["reviews_since"] = reviews_since
    m["last_cleanup"] = meta._utc_now_iso()      # recent → only reviews_since drives due-ness
    m["learn_claimed_at"] = meta._utc_now_iso() if locked else ""
    return m


@pytest.fixture
def fleet(monkeypatch):
    # qai-be due(20), qai-fe not-due(3), other due but locked, old archived, non-air store
    stores = [
        {"name": "air-patterns thecvlb/qai-be", "id": "s1"},
        {"name": "air-patterns thecvlb/qai-fe", "id": "s2"},
        {"name": "air-patterns thecvlb/ai-relay", "id": "s3"},      # due but LOCKED
        {"name": "air-patterns thecvlb/retired", "id": "s4", "archived_at": "2026-01-01"},
        {"name": "some-other-store", "id": "s5"},                    # not air-patterns
    ]
    metas = {"s1": _meta(20), "s2": _meta(3), "s3": _meta(20, locked=True), "s4": _meta(99)}
    monkeypatch.setattr(C.memory_store, "client", _fake_client)
    monkeypatch.setattr(C.memory_store, "_paginate", lambda _fn: stores)
    monkeypatch.setattr(C.meta, "_store_find_meta",
                        lambda sid: (metas[sid], "sha", "mem") if sid in metas else None)
    return stores


def test_find_due_filters_archived_nonair_locked_and_notdue(fleet):
    due = C.find_due_repos()
    repos = [r for r, _, _ in due]
    assert repos == ["thecvlb/qai-be"]          # only due + non-archived + air-patterns + unlocked
    # qai-fe not due, ai-relay locked, retired archived, other not-air → all excluded


def test_repos_filter_narrows_scan(fleet):
    due = C.find_due_repos(repos_filter={"thecvlb/qai-fe"})
    assert due == []                            # qai-fe is in-filter but NOT due


def test_store_with_no_counter_is_skipped(monkeypatch):
    monkeypatch.setattr(C.memory_store, "client", _fake_client)
    monkeypatch.setattr(C.memory_store, "_paginate",
                        lambda _fn: [{"name": "air-patterns o/new", "id": "sx"}])
    monkeypatch.setattr(C.meta, "_store_find_meta", lambda sid: None)  # no counter yet
    assert C.find_due_repos() == []


def test_run_invokes_headless_learn_per_due_with_dry_run(fleet, monkeypatch):
    calls = []
    def fake_learn(repo, *, token=None, store_id=None, dry_run=False, log=print):
        calls.append((repo, store_id, dry_run))
        return {"store_id": store_id, "written": [], "dry_run": dry_run}
    monkeypatch.setattr(C.learn_headless, "run_headless_learn", fake_learn)
    out = C.run(dry_run=True)
    assert out["due"] == ["thecvlb/qai-be"] and out["ran"] == ["thecvlb/qai-be"]
    assert calls == [("thecvlb/qai-be", "s1", True)]   # store_id threaded, dry_run passed


def test_run_limit_caps_repos(monkeypatch):
    monkeypatch.setattr(C, "find_due_repos",
                        lambda repos_filter=None, log=print: [("o/a", "s1", "r"), ("o/b", "s2", "r")])
    seen = []
    monkeypatch.setattr(C.learn_headless, "run_headless_learn",
                        lambda repo, **k: seen.append(repo) or {})
    C.run(dry_run=True, limit=1)
    assert seen == ["o/a"]                       # limit=1 → only the first due repo


def test_listing_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(C.memory_store, "client", _fake_client)
    def boom(_fn): raise RuntimeError("api down")
    monkeypatch.setattr(C.memory_store, "_paginate", boom)
    assert C.find_due_repos() == []              # best-effort: a listing error → no learns


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_list_mode_makes_no_model_calls(fleet, monkeypatch):
    # --list enumerates due repos but must NEVER invoke run_headless_learn.
    called = []
    monkeypatch.setattr(C.learn_headless, "run_headless_learn",
                        lambda *a, **k: called.append(1) or {})
    rc = C.main(["--list"])
    assert rc == 0
    assert called == []   # zero model calls — the safe scheduled-trial default


# --- M3: the cron claims the anti-storm lock before curating -----------------

def test_run_live_claims_lock_then_learns(fleet, monkeypatch):
    claimed, learned = [], []
    monkeypatch.setattr(C.meta, "claim_learn_lock", lambda sid: claimed.append(sid) or True)
    monkeypatch.setattr(C.meta, "release_learn_lock", lambda sid: pytest.fail("must not release on clean reset"))
    monkeypatch.setattr(C.learn_headless, "run_headless_learn",
                        lambda repo, **k: learned.append(repo) or {"reset": True})
    C.run(dry_run=False)
    assert claimed == ["s1"] and learned == ["thecvlb/qai-be"]


def test_run_skips_repo_when_lock_lost(fleet, monkeypatch):
    learned = []
    monkeypatch.setattr(C.meta, "claim_learn_lock", lambda sid: False)   # lost the race
    monkeypatch.setattr(C.learn_headless, "run_headless_learn",
                        lambda repo, **k: learned.append(repo) or {})
    out = C.run(dry_run=False)
    assert learned == []                                  # never curated (lock lost)
    assert out["due"] == ["thecvlb/qai-be"]               # still detected as due


def test_run_releases_lock_on_learn_error(fleet, monkeypatch):
    released = []
    monkeypatch.setattr(C.meta, "claim_learn_lock", lambda sid: True)
    monkeypatch.setattr(C.meta, "release_learn_lock", lambda sid: released.append(sid))
    def boom(repo, **k):
        raise RuntimeError("model outage")
    monkeypatch.setattr(C.learn_headless, "run_headless_learn", boom)
    C.run(dry_run=False)
    assert released == ["s1"]                             # errored → lock freed for re-arm


def test_run_releases_lock_on_degraded_no_reset(fleet, monkeypatch):
    released = []
    monkeypatch.setattr(C.meta, "claim_learn_lock", lambda sid: True)
    monkeypatch.setattr(C.meta, "release_learn_lock", lambda sid: released.append(sid))
    # A degraded run returns reset=False (re-arm intent) → the cron frees the lock.
    monkeypatch.setattr(C.learn_headless, "run_headless_learn",
                        lambda repo, **k: {"reset": False})
    C.run(dry_run=False)
    assert released == ["s1"]


def test_run_keeps_lock_on_clean_reset(fleet, monkeypatch):
    # reset=True means run_headless_learn already cleared the lock → no extra release.
    monkeypatch.setattr(C.meta, "claim_learn_lock", lambda sid: True)
    monkeypatch.setattr(C.meta, "release_learn_lock",
                        lambda sid: pytest.fail("must not double-release after a clean reset"))
    monkeypatch.setattr(C.learn_headless, "run_headless_learn",
                        lambda repo, **k: {"reset": True})
    C.run(dry_run=False)


def test_dry_run_never_touches_lock(fleet, monkeypatch):
    # dry-run writes nothing — it must not claim or release the store lock either.
    monkeypatch.setattr(C.meta, "claim_learn_lock",
                        lambda sid: pytest.fail("dry-run must not claim the lock"))
    monkeypatch.setattr(C.meta, "release_learn_lock",
                        lambda sid: pytest.fail("dry-run must not release the lock"))
    monkeypatch.setattr(C.learn_headless, "run_headless_learn",
                        lambda repo, **k: {"dry_run": True})
    C.run(dry_run=True)


def test_claim_error_skips_only_that_repo(monkeypatch):
    # #1 isolation: a store error in claim_learn_lock must skip ONLY that repo,
    # not abort the rest of the scheduled run (matches find_due_repos isolation).
    monkeypatch.setattr(C, "find_due_repos",
                        lambda repos_filter=None, log=print: [("o/a", "sa", "r"), ("o/b", "sb", "r")])
    def claim(sid):
        if sid == "sa":
            raise RuntimeError("store blip")
        return True
    learned = []
    monkeypatch.setattr(C.meta, "claim_learn_lock", claim)
    monkeypatch.setattr(C.meta, "release_learn_lock", lambda sid: None)
    monkeypatch.setattr(C.learn_headless, "run_headless_learn",
                        lambda repo, **k: learned.append(repo) or {"reset": True})
    out = C.run(dry_run=False)                 # must NOT raise
    assert learned == ["o/b"]                  # o/a skipped on claim error, o/b still learned
    assert out["ran"] == ["o/a", "o/b"]        # both recorded (o/a as an error)
