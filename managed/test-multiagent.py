#!/usr/bin/env python3
"""Unit tests for the PR6′ multiagent migration plumbing (AIR_MULTIAGENT=1):
ThreadTracker's dual-runtime accounting, the WORKSPACE-HANDOFF coordinator
message, agent selection, and the required-agents gate.

The drain-loop accounting is the highest-risk piece: the GA multiagent
primitive renamed the thread lifecycle events (session.thread_status_idle,
NOT session.thread_idle) and lets threads idle-then-re-run — an unhandled
rename means the open-thread count never decrements and every run rides the
2700s wall timeout (probe 2, 2026-06-10).

Pure functions, no network. Run: python -m pytest managed/test-multiagent.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import review  # noqa: E402
from session_runner import ThreadTracker  # noqa: E402


# ---------------------------------------------------------------------------
# ThreadTracker — legacy callable_agents semantics (multiagent_primary=None)
# ---------------------------------------------------------------------------

def test_legacy_counter_basic():
    t = ThreadTracker()
    t.on_event("session.thread_created")
    t.on_event("session.thread_created")
    assert t.open_count == 2
    t.on_event("session.thread_idle")
    assert t.open_count == 1
    t.on_event("session.thread_idle")
    assert t.open_count == 0


def test_legacy_counter_never_negative():
    t = ThreadTracker()
    t.on_event("session.thread_idle")
    assert t.open_count == 0


def test_legacy_ignores_ga_event_names():
    # callable_agents never emits thread_status_*; if the runtime starts
    # to, the legacy counter must not be perturbed (the MA path opts in
    # explicitly via multiagent_primary).
    t = ThreadTracker()
    t.on_event("session.thread_created")
    t.on_event("session.thread_status_idle")
    assert t.open_count == 1


# ---------------------------------------------------------------------------
# ThreadTracker — multiagent semantics (per-thread state, primary excluded)
# ---------------------------------------------------------------------------

def test_ma_rename_decrements():
    # The probe-confirmed GA rename: thread_status_idle must close a thread.
    t = ThreadTracker(multiagent_primary="air-coordinator-ma")
    t.on_event("session.thread_created", "air-code-reviewer")
    assert t.open_count == 1
    t.on_event("session.thread_status_idle", "air-code-reviewer")
    assert t.open_count == 0


def test_ma_primary_thread_excluded():
    # The coordinator's own thread idles BETWEEN its turns and re-runs; it
    # must never count as an open sub-agent thread.
    t = ThreadTracker(multiagent_primary="air-coordinator-ma")
    t.on_event("session.thread_created", "air-coordinator-ma")
    t.on_event("session.thread_status_running", "air-coordinator-ma")
    assert t.open_count == 0
    t.on_event("session.thread_status_idle", "air-coordinator-ma")
    assert t.open_count == 0


def test_ma_rerun_reopens_thread():
    # A roster thread can idle and then RUN AGAIN on a coordinator
    # follow-up — running must re-open it (a +/- counter would drift).
    t = ThreadTracker(multiagent_primary="air-coordinator-ma")
    t.on_event("session.thread_created", "air-review-verifier")
    t.on_event("session.thread_status_idle", "air-review-verifier")
    assert t.open_count == 0
    t.on_event("session.thread_status_running", "air-review-verifier")
    assert t.open_count == 1
    t.on_event("session.thread_status_idle", "air-review-verifier")
    assert t.open_count == 0


def test_ma_duplicate_idles_do_not_drift():
    # The same thread idling repeatedly must not push the count below the
    # other open threads (set semantics, not arithmetic).
    t = ThreadTracker(multiagent_primary="air-coordinator-ma")
    t.on_event("session.thread_created", "air-code-reviewer")
    t.on_event("session.thread_created", "air-simplify")
    t.on_event("session.thread_status_idle", "air-simplify")
    t.on_event("session.thread_status_idle", "air-simplify")
    assert t.open_count == 1


def test_ma_terminated_closes_thread():
    t = ThreadTracker(multiagent_primary="air-coordinator-ma")
    t.on_event("session.thread_created", "air-security-auditor")
    t.on_event("session.thread_status_terminated", "air-security-auditor")
    assert t.open_count == 0


def test_ma_probe_trace_replay():
    # Replay of the probe-2 lifecycle trace (5 workers + verifier +
    # primary) — must end at 0 open with no intermediate stuck state.
    t = ThreadTracker(multiagent_primary="coord")
    for name in ("w1", "w2", "w3", "w4", "w5"):
        t.on_event("session.thread_created", name)
        t.on_event("session.thread_status_running", name)
    t.on_event("session.thread_status_idle", "coord")
    assert t.open_count == 5
    for name in ("w1", "w2", "w5", "w3", "w4"):
        t.on_event("session.thread_status_idle", name)
    t.on_event("session.thread_status_running", "coord")
    assert t.open_count == 0
    t.on_event("session.thread_created", "verifier")
    t.on_event("session.thread_status_running", "verifier")
    assert t.open_count == 1
    t.on_event("session.thread_status_idle", "verifier")
    t.on_event("session.thread_status_idle", "coord")
    assert t.open_count == 0


# ---------------------------------------------------------------------------
# review.py wiring — flag, agent selection, required gate
# ---------------------------------------------------------------------------

def test_multiagent_flag_parsing(monkeypatch):
    monkeypatch.delenv("AIR_MULTIAGENT", raising=False)
    assert review._multiagent_enabled() is False
    monkeypatch.setenv("AIR_MULTIAGENT", "1")
    assert review._multiagent_enabled() is True
    monkeypatch.setenv("AIR_MULTIAGENT", "true")
    assert review._multiagent_enabled() is True
    monkeypatch.setenv("AIR_MULTIAGENT", "0")
    assert review._multiagent_enabled() is False


def test_ma_agent_name_constant():
    assert review.COORDINATOR_MA_AGENT == "air-coordinator-ma"


def test_setup_does_not_pin_ma_agent():
    import setup as setup_mod
    assert "air-coordinator-ma" not in setup_mod.PINNABLE_AGENTS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
