#!/usr/bin/env python3
"""Unit tests for the PR6′ multiagent migration plumbing (AIR_MULTIAGENT=1):
ThreadTracker's dual-runtime accounting (incl. the TURN-0 first-dispatch
gate and unattributed-event warning), the run-random heredoc sentinel that
makes the TURN-0 workspace writes injection-proof, the required-agents
gate, and the flag/constant wiring.

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
    t = ThreadTracker(multiagent_primary=review.COORDINATOR_MA_AGENT)
    t.on_event("session.thread_created", "air-code-reviewer")
    assert t.open_count == 1
    t.on_event("session.thread_status_idle", "air-code-reviewer")
    assert t.open_count == 0


def test_ma_primary_thread_excluded():
    # The coordinator's own thread idles BETWEEN its turns and re-runs; it
    # must never count as an open sub-agent thread.
    t = ThreadTracker(multiagent_primary=review.COORDINATOR_MA_AGENT)
    t.on_event("session.thread_created", review.COORDINATOR_MA_AGENT)
    t.on_event("session.thread_status_running", review.COORDINATOR_MA_AGENT)
    assert t.open_count == 0
    t.on_event("session.thread_status_idle", review.COORDINATOR_MA_AGENT)
    assert t.open_count == 0


def test_ma_rerun_reopens_thread():
    # A roster thread can idle and then RUN AGAIN on a coordinator
    # follow-up — running must re-open it (a +/- counter would drift).
    t = ThreadTracker(multiagent_primary=review.COORDINATOR_MA_AGENT)
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
    t = ThreadTracker(multiagent_primary=review.COORDINATOR_MA_AGENT)
    t.on_event("session.thread_created", "air-code-reviewer")
    t.on_event("session.thread_created", "air-simplify")
    t.on_event("session.thread_status_idle", "air-simplify")
    t.on_event("session.thread_status_idle", "air-simplify")
    assert t.open_count == 1


def test_ma_terminated_closes_thread():
    t = ThreadTracker(multiagent_primary=review.COORDINATOR_MA_AGENT)
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
    assert review.COORDINATOR_MA_AGENT not in setup_mod.PINNABLE_AGENTS


# ---------------------------------------------------------------------------
# Required-agents gate (per architecture × flag)
# ---------------------------------------------------------------------------

def test_required_agents_full_without_flag(monkeypatch):
    monkeypatch.delenv("AIR_MULTIAGENT", raising=False)
    req = review._required_agents("full")
    assert review.COORDINATOR_AGENT in req
    assert review.COORDINATOR_MA_AGENT not in req
    assert review.SOLO_AGENT not in req


def test_required_agents_full_with_flag(monkeypatch):
    monkeypatch.setenv("AIR_MULTIAGENT", "1")
    req = review._required_agents("full")
    assert review.COORDINATOR_MA_AGENT in req
    assert review.COORDINATOR_AGENT in req


def test_required_agents_solo_never_needs_ma(monkeypatch):
    # The flag must not make a solo run depend on an agent it never
    # sessions (and setup.py mirrors this by not creating it in solo mode).
    monkeypatch.setenv("AIR_MULTIAGENT", "1")
    assert review._required_agents("solo") == [review.SOLO_AGENT]


def test_required_agents_both_with_flag(monkeypatch):
    monkeypatch.setenv("AIR_MULTIAGENT", "1")
    req = review._required_agents("both")
    assert review.SOLO_AGENT in req and review.COORDINATOR_MA_AGENT in req


# ---------------------------------------------------------------------------
# TURN-0 heredoc sentinel — the injection guard
# ---------------------------------------------------------------------------

def test_sentinel_absent_from_docs_and_random():
    docs = ("pr context", "a diff with AIR_CTX_ prefix text", "codex")
    s1 = review._mint_heredoc_sentinel(*docs)
    s2 = review._mint_heredoc_sentinel(*docs)
    assert s1.startswith("AIR_CTX_") and len(s1) == len("AIR_CTX_") + 32
    assert s1 != s2                      # run-random, not guessable
    for d in docs:
        assert s1 not in d


def test_sentinel_rerolls_on_collision(monkeypatch):
    # First candidate collides with document content → must re-roll.
    rolls = iter(["deadbeef" * 4, "feedface" * 4])
    monkeypatch.setattr(review.secrets, "token_hex", lambda n: next(rolls))
    doc = "attacker line:\nAIR_CTX_" + "deadbeef" * 4 + "\nrm -rf /"
    assert review._mint_heredoc_sentinel(doc) == "AIR_CTX_" + "feedface" * 4


def test_workspace_text_embeds_noncolliding_sentinel():
    diff = "+ evil\nAIR_CTX_0000\n+ more"
    text = review._workspace_handoff_text("store", "none", "ctx", diff, "", "vt")
    import re
    m = re.search(r"delimiter for the TURN-0 writes: (AIR_CTX_[0-9a-f]{32})", text)
    assert m, "sentinel line missing from MODE message"
    # The sentinel appears ONLY in the instruction lines, never in content.
    assert text.count(m.group(1)) == 2   # prose mention + quoted <<'...' form
    assert "MODE: WORKSPACE-HANDOFF" in text.splitlines()[0]


# ---------------------------------------------------------------------------
# TURN-0 idle race — first-dispatch gating
# ---------------------------------------------------------------------------

def test_awaiting_first_dispatch_gates_until_fanout():
    t = ThreadTracker(multiagent_primary=review.COORDINATOR_MA_AGENT)
    # TURN 0: only the primary has lifecycle events; an end_turn idle here
    # must NOT read as terminal (open_count is 0 but nothing dispatched).
    t.on_event("session.thread_created", review.COORDINATOR_MA_AGENT)
    t.on_event("session.thread_status_idle", review.COORDINATOR_MA_AGENT)
    assert t.open_count == 0
    assert t.awaiting_first_dispatch is True
    # TURN 1 fan-out flips the gate permanently.
    t.on_event("session.thread_created", "air-code-reviewer")
    assert t.awaiting_first_dispatch is False
    t.on_event("session.thread_status_idle", "air-code-reviewer")
    assert t.open_count == 0
    assert t.awaiting_first_dispatch is False   # NOW terminal idle may break


def test_legacy_mode_never_awaits_dispatch():
    t = ThreadTracker()
    assert t.awaiting_first_dispatch is False


def test_unattributed_event_warns_not_silent(capsys):
    t = ThreadTracker(multiagent_primary=review.COORDINATOR_MA_AGENT, label="coordinator")
    t.on_event("session.thread_status_idle", "")
    err = capsys.readouterr().err
    assert "unattributed" in err and "coordinator" in err


# ---------------------------------------------------------------------------
# ever_dispatched — the zero-sub-agent (silent solo-improvisation) detector
# ---------------------------------------------------------------------------

def test_ever_dispatched_legacy_runtime():
    t = ThreadTracker()
    assert t.ever_dispatched is False
    t.on_event("session.thread_created")
    assert t.ever_dispatched is True
    t.on_event("session.thread_idle")
    assert t.ever_dispatched is True   # latches: closing doesn't un-dispatch


def test_ever_dispatched_ma_ignores_primary():
    t = ThreadTracker(multiagent_primary=review.COORDINATOR_MA_AGENT)
    t.on_event("session.thread_status_running", review.COORDINATOR_MA_AGENT)
    assert t.ever_dispatched is False  # the coordinator's own thread is not a dispatch
    t.on_event("session.thread_status_running", "air-code-reviewer")
    assert t.ever_dispatched is True


# ---------------------------------------------------------------------------
# run_session(require_dispatch=True) — fail loud when no sub-agent ever ran.
# Scripted through a fake client on the legacy runtime: a clean end_turn
# idle with zero thread events is byte-for-byte the production callable_agents
# failure shape (create_agent denied → coordinator improvises → "success").
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from session_runner import SpecialistSessionError, run_session  # noqa: E402


def _fake_client(events, create_calls=None):
    """Minimal AsyncAnthropic stand-in for run_session's happy SSE path.
    Pass a list as `create_calls` to capture sessions.create kwargs."""
    class _Stream:
        def __init__(self, evs):
            self._it = iter(evs)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def __aiter__(self):
            return self
        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

    async def _create(**kwargs):
        if create_calls is not None:
            create_calls.append(kwargs)
        return SimpleNamespace(id="sesn_test")

    async def _send(sid, events):
        return None

    async def _stream(sid):
        return _Stream(events)

    sessions = SimpleNamespace(
        create=_create,
        events=SimpleNamespace(send=_send, stream=_stream),
    )
    return SimpleNamespace(beta=SimpleNamespace(sessions=sessions))


def _msg(text, eid):
    return SimpleNamespace(
        id=eid, type="agent.message",
        content=[SimpleNamespace(text=text)],
    )


def _idle(eid, stop="end_turn"):
    return SimpleNamespace(
        id=eid, type="session.status_idle",
        stop_reason=SimpleNamespace(type=stop),
    )


def _thread(eid, etype):
    return SimpleNamespace(id=eid, type=etype, agent_name="air-code-reviewer")


def _run(events, **kwargs):
    client = _fake_client(events)
    return asyncio.run(run_session(
        client, "agent_x", 1, "env_x", "o/r",
        {"type": "branch", "name": "main"}, "tok", "go", "coordinator",
        **kwargs,
    ))


def test_require_dispatch_fails_zero_thread_session():
    events = [_msg("## Code Review\nimprovised", "e1"), _idle("e2")]
    with pytest.raises(SpecialistSessionError) as exc:
        _run(events, require_dispatch=True)
    assert "without dispatching" in str(exc.value)


def test_require_dispatch_passes_with_fanout():
    events = [
        _thread("e1", "session.thread_created"),
        _thread("e2", "session.thread_idle"),
        _msg("## Code Review\nreal", "e3"),
        _idle("e4"),
    ]
    assert "real" in _run(events, require_dispatch=True)


def test_no_require_dispatch_keeps_solo_sessions_working():
    # Solo / learn / codex sessions legitimately never dispatch sub-agents.
    events = [_msg("solo output", "e1"), _idle("e2")]
    assert _run(events) == "solo output"


# ---------------------------------------------------------------------------
# Session attribution metadata (C8)
# ---------------------------------------------------------------------------

from session_runner import build_session_metadata  # noqa: E402


def test_session_metadata_strings_and_empties(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    meta = build_session_metadata("o/r", 42, kind="review-coordinator")
    assert meta == {"repo": "o/r", "pr": "42", "kind": "review-coordinator", "ci_run": "12345"}
    assert all(isinstance(v, str) for v in meta.values())
    # Absent context never serializes as '' (the API stores what it's given).
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    assert build_session_metadata("o/r") == {"repo": "o/r"}


def test_run_session_passes_metadata_to_create():
    calls = []
    events = [_msg("out", "e1"), _idle("e2")]
    client = _fake_client(events, create_calls=calls)
    asyncio.run(run_session(
        client, "agent_x", 1, "env_x", "o/r",
        {"type": "branch", "name": "main"}, "tok", "go", "coordinator",
        metadata={"repo": "o/r", "pr": "7"},
    ))
    assert calls[0]["metadata"] == {"repo": "o/r", "pr": "7"}
    # And omitted entirely when not provided — never an empty dict.
    calls.clear()
    client = _fake_client(events, create_calls=calls)
    asyncio.run(run_session(
        client, "agent_x", 1, "env_x", "o/r",
        {"type": "branch", "name": "main"}, "tok", "go", "coordinator",
    ))
    assert "metadata" not in calls[0]


# ---------------------------------------------------------------------------
# setup.py multiagent persistence — GA dialect + roster verification
# ---------------------------------------------------------------------------

import api  # noqa: E402
import setup as setup_mod  # noqa: E402


def test_get_headers_ga_swaps_beta_header(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rp = api.get_headers()  # default: research-preview dialect
    assert rp["anthropic-beta"] == "managed-agents-2026-04-01-research-preview"
    assert api.get_headers(ga=True)["anthropic-beta"] == "managed-agents-2026-04-01"


def test_verify_roster_aborts_when_dropped(capsys):
    requested = {"type": "coordinator", "agents": [{"type": "agent", "id": "a", "version": 1}]}
    with pytest.raises(SystemExit):
        setup_mod._verify_multiagent_roster("air-coordinator-ma", {"version": 4}, requested)
    assert "dropped the multiagent roster" in capsys.readouterr().err


def test_verify_roster_passes_when_persisted_or_unrequested():
    roster = {"type": "coordinator", "agents": [{"type": "agent", "id": "a", "version": 1}]}
    setup_mod._verify_multiagent_roster("x", {"version": 5, "multiagent": roster}, roster)
    setup_mod._verify_multiagent_roster("x", {"version": 5}, None)  # specialists never request one


def test_coordinator_tool_configs_enable_disable_matrix():
    """Every named tool gets an EXPLICIT config: enabled iff allowlisted.
    An implicit (missing) entry would inherit the enabled default — the
    same silent-grant shape the delegation fix exists for, in reverse."""
    allowed = {"bash", "read", "grep", "glob"}
    configs = setup_mod.build_coordinator_tool_configs(allowed)
    assert {c["name"] for c in configs} == set(setup_mod.NAMED_TOOL_VOCABULARY)
    state = {c["name"]: c["enabled"] for c in configs}
    assert all(state[t] for t in allowed)
    assert not any(state[t] for t in set(setup_mod.NAMED_TOOL_VOCABULARY) - allowed)


def test_solo_prompt_resolves_to_the_shared_lib():
    """setup.py's solo assembly IS plugins/air/lib/solo_prompt.py — the same
    prompt the CLI's --solo flow runs. One implementation, two paths (the
    lib/verdict.py pattern); a managed-local copy would silently drift."""
    import inspect
    src = inspect.getsourcefile(setup_mod.assemble_solo_prompt)
    assert src and src.endswith("plugins/air/lib/solo_prompt.py")
    prompt = setup_mod.assemble_solo_prompt()
    assert prompt.startswith("You are a thorough code reviewer")
    for lens in setup_mod.SUB_AGENTS:
        assert f"===== LENS: {lens} =====" in prompt


def test_sdk_page_cursor_still_exposes_next_page():
    """Guard against mock-contract drift: the fakes in these suites model
    the page as {data, next_page}. If an SDK upgrade renames the cursor
    field, this fails loudly instead of letting every drain silently
    single-page again (the exact bug class fixed three times on PR #152)."""
    from anthropic.pagination import AsyncPageCursor, SyncPageCursor
    assert "next_page" in SyncPageCursor.model_fields
    assert "next_page" in AsyncPageCursor.model_fields


def test_coordinator_prompt_has_wrong_runtime_guard():
    """The coordinator is a managed-runtime DELEGATOR (callable_agents), not a
    reviewer. Invoked as a LOCAL Claude Code subagent — no callable_agents, no
    MODE line, no embedded PR Context+diff — it used to silently confabulate a
    full review AND fake the tool-call transcript (tool_uses:0), even narrating
    a wiki push of invented "author patterns". `require_dispatch=True` catches
    this in the managed runtime; this prompt-level guard is its analog for the
    local-misinvocation path. Keep it.
    """
    text = (Path(__file__).parents[1] / "plugins/air/agents/coordinator.md").read_text()
    assert "AIR_COORDINATOR_WRONG_RUNTIME" in text
    assert "delegator, never a reviewer" in text
    assert "NEVER fabricate findings" in text
    # Pin the load-bearing STOP semantics too — not just the labels. Keeping the
    # sentinel while dropping "Do NOT proceed"/"STOP" would re-open the
    # emit-then-confabulate path the guard exists to close.
    assert "Do NOT proceed" in text and "STOP" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# Opt-in tiered MA coordinator (AIR_MA_COORDINATOR_MODEL) — routes to a
# SEPARATE air-coordinator-ma-<alias> agent so a per-repo opt-in never mutates
# the shared Sonnet coordinator. The coordinator only relays the verifier's
# review verbatim (validated relay-safe), so a cheaper tier is gate-consistent.
# ---------------------------------------------------------------------------

def test_ma_coordinator_default_unset():
    assert review._ma_coordinator_name("") == "air-coordinator-ma"
    assert review._ma_coordinator_name(None) == "air-coordinator-ma"


def test_ma_coordinator_sonnet_is_default_agent():
    # sonnet == the default tier → no separate agent, route to the standard one
    assert review._ma_coordinator_name("sonnet") == "air-coordinator-ma"


def test_ma_coordinator_haiku_routes_to_tiered_agent():
    assert review._ma_coordinator_name("haiku") == "air-coordinator-ma-haiku"
    assert review._ma_coordinator_name(" Haiku ") == "air-coordinator-ma-haiku"  # trimmed/lowercased


def test_ma_coordinator_opus_routes_to_tiered_agent():
    assert review._ma_coordinator_name("opus") == "air-coordinator-ma-opus"


def test_ma_coordinator_unknown_alias_fails_safe_to_default():
    # an unknown value must NOT route to a non-existent agent — fall back
    assert review._ma_coordinator_name("gpt5") == "air-coordinator-ma"
    assert review._ma_coordinator_name("haiku-4-5-20251001") == "air-coordinator-ma"


# ---------------------------------------------------------------------------
# Direct-post (AIR_POST_VERIFIER_BODY): recover findings the coordinator's relay
# dropped by posting the VERIFIER's delivered body. Selection MUST be safe by
# construction — a specialist's (unverified) body, which also carries a
# `## Code Review` header, can NEVER be selected; anything ambiguous falls back
# to the coordinator relay.
# ---------------------------------------------------------------------------
_SHA = "a" * 40
# Coordinator relayed 2 of the verifier's 3 findings (the drop) + the footer.
_COORD = (
    "Excellent. Synthesizing for TURN 3.\n\n## Code Review\n\nsummary\n\n"
    "### Blockers\n\n**1. alpha auth bypass**\n\n[l] — x\n\n"
    "### Medium\n\n**2. beta pii leak**\n\n[l] — y\n\n### Strengths\n\n- ok\n\n"
    f"Reviewed at: {_SHA}\n"
)
# Verifier delivered all 3 findings, verifier-template sections, NO footer.
_VERIFIER = (
    "Review complete. Here are the findings.\n\n## Code Review\n\nsummary\n\n"
    "### Blockers\n\n**1. alpha auth bypass**\n\n[l] — x\n\n"
    "### Medium\n\n**2. beta pii leak**\n\n[l] — y\n\n"
    "### Low\n\n**3. gamma dropped nit**\n\n[l] — z\n\n### Strengths\n\n- ok\n"
)
# A specialist that ALSO emits `## Code Review` but with its own (different)
# findings and no verifier-only sections — must never be selected.
_SPECIALIST = (
    "## Code Review — PR by dev\n\nReview complete.\n\n"
    "### Medium\n\n**1. delta unrelated thing**\n\n### Nit\n\n**2. epsilon**\n"
)


def test_direct_post_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("AIR_POST_VERIFIER_BODY", raising=False)
    assert review._post_verifier_body_enabled() is False
    for v in ("1", "true", "YES"):
        monkeypatch.setenv("AIR_POST_VERIFIER_BODY", v)
        assert review._post_verifier_body_enabled() is True
    for v in ("0", "false", ""):
        monkeypatch.setenv("AIR_POST_VERIFIER_BODY", v)
        assert review._post_verifier_body_enabled() is False


def test_direct_recovers_dropped_findings_from_verifier_body():
    cap = {"received_reviews": [_SPECIALIST, _VERIFIER]}
    src, status = review._select_review_source(_COORD, cap, _SHA, "full")
    assert status.startswith("direct")
    # the posted body is the verifier's (3 findings, incl. the dropped gamma) +
    # a synthesized footer that re-validates against head_sha
    assert "gamma dropped nit" in src
    assert review._extract_review_body(src, _SHA)[1] is True
    assert len(review._finding_titles(src)) == 3


def test_direct_never_selects_a_specialist_body():
    # Only a specialist was delivered (no verifier-only sections / its titles
    # don't cover the relayed findings) → must fall back, never post it.
    cap = {"received_reviews": [_SPECIALIST]}
    src, status = review._select_review_source(_COORD, cap, _SHA, "full")
    assert src == _COORD and "delta unrelated" not in src
    assert status == "no-verifier-match"


def test_direct_falls_back_when_no_candidates():
    src, status = review._select_review_source(_COORD, {"received_reviews": []}, _SHA, "full")
    assert src == _COORD and status == "no candidates captured"


def test_direct_off_when_capture_disabled():
    src, status = review._select_review_source(_COORD, None, _SHA, "full")
    assert status == "off" and src == _COORD


def test_direct_not_applied_to_solo():
    cap = {"received_reviews": [_VERIFIER]}
    src, status = review._select_review_source(_COORD, cap, _SHA, "solo")
    assert status == "off" and src == _COORD


def test_select_verifier_body_requires_coverage_and_count():
    coord_body = review._extract_review_body(_COORD, _SHA)[0]
    # a verifier-shaped body whose findings DON'T cover the relayed ones (wrong
    # body) is rejected on coverage
    wrong = (
        "## Code Review\n\n### Blockers\n\n**1. totally other one**\n\n"
        "### Strengths\n\n- ok\n"
    )
    _, status = review._select_verifier_body([wrong], coord_body, _SHA)
    assert status == "no-verifier-match"
    # the real verifier body (covers relayed, >= findings) is selected
    body, status = review._select_verifier_body([_VERIFIER], coord_body, _SHA)
    assert status == "direct" and "gamma" in body


def test_append_review_footer_makes_body_sha_valid():
    finalized = review._append_review_footer(_VERIFIER, _SHA)
    assert review._extract_review_body(finalized, _SHA)[1] is True


def test_finding_titles_parses_numbered_lines():
    titles = review._finding_titles(_VERIFIER)
    assert len(titles) == 3
    assert any("alpha" in t for t in titles)
