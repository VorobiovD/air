#!/usr/bin/env python3
"""
Research-preview feature comparison harness.

Every variant performs the SAME net work — review the fixture diff and
verify the findings — so cost/time deltas reflect orchestration choice
and feature use, not workload differences.

The two orthogonal axes:

   architecture  |  no Memory                   |  with Memory
   --------------+-------------------------------+----------------------------
   split         |  reviewer session +           |  same, with wiki in
                 |  verifier session             |  /mnt/memory/wiki
                 |                               |
   multiagent    |  one session, coordinator     |  same, with wiki in
                 |  + reviewer + verifier        |  /mnt/memory/wiki
                 |  sub-agent threads            |
                 |                               |
   solo          |  one session, one agent       |  same, with wiki in
                 |  does both review+verify      |  /mnt/memory/wiki

Plus an Outcomes axis (orthogonal to all the above) for the variants
where the cost-vs-quality tradeoff is most interesting.

Variants exposed via --variant:

  split                Two sessions: reviewer then verifier. Today's
                       architecture, in miniature. The cost floor we
                       compare everything else against.
  split_memory         Same as split, with wiki in memory store.
  multiagent           One session, coordinator delegates to reviewer
                       and verifier sub-agents (callable_agents).
  multiagent_memory    Multiagent + memory.
  solo                 One session, one agent does review+verify in
                       a single context. Tier 3 of the cost plan.
  solo_memory          Solo + memory.
  multiagent_outcomes  Multiagent + Outcomes self-eval loop.
  all                  Multiagent + memory + outcomes stacked.

Direct comparisons the report will show:
  split           vs multiagent           — multiagent feature impact (same memory: none)
  split_memory    vs multiagent_memory    — multiagent feature impact (same memory: yes)
  split           vs split_memory         — memory feature impact (same arch: split)
  multiagent      vs multiagent_memory    — memory feature impact (same arch: multiagent)
  split           vs solo                 — consolidation impact (1 vs 2 agents)
  multiagent      vs multiagent_outcomes  — outcomes impact

Usage:
  python cost_test.py --variant split
  python cost_test.py --variant multiagent
  python cost_test.py --report
  python cost_test.py --cleanup
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests as req

API_BASE = "https://api.anthropic.com/v1"
HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
RESULTS = HERE / "results.jsonl"

PRICING = {
    "claude-opus-4-7":   {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_5m": 6.25, "cache_1h": 10.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_5m": 3.75, "cache_1h": 6.0},
    "claude-haiku-4-5":  {"input": 1.0, "output": 5.0,  "cache_read": 0.10, "cache_5m": 1.25, "cache_1h": 2.0},
}

GA_BETA = "managed-agents-2026-04-01"
RP_BETA = "managed-agents-2026-04-01-research-preview"

TEST_PREFIX = "experiments-cost-test"
DEFAULT_MODEL = "claude-sonnet-4-6"


def headers(beta: str) -> dict:
    """Returns API headers. `beta` selects GA or research-preview value."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY not set")
    return {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": beta,
        "content-type": "application/json",
    }


def cost_of(usage: dict, model: str) -> float:
    """USD cost from a session's usage block."""
    p = PRICING.get(model)
    if not p:
        return 0.0
    cc = usage.get("cache_creation") or {}
    return (
        usage.get("input_tokens", 0) * p["input"]
        + usage.get("output_tokens", 0) * p["output"]
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
        + cc.get("ephemeral_5m_input_tokens", 0) * p["cache_5m"]
        + cc.get("ephemeral_1h_input_tokens", 0) * p["cache_1h"]
    ) / 1_000_000


def aggregate_usage(usages: list[dict]) -> dict:
    """Sum a list of usage dicts into one. Used when a variant runs
    multiple sessions (e.g. split) and we need a single per-variant
    total to compare against single-session variants."""
    total_in = sum(u.get("input_tokens", 0) for u in usages)
    total_out = sum(u.get("output_tokens", 0) for u in usages)
    total_cr = sum(u.get("cache_read_input_tokens", 0) for u in usages)
    total_5m = sum((u.get("cache_creation") or {}).get("ephemeral_5m_input_tokens", 0) for u in usages)
    total_1h = sum((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0) for u in usages)
    return {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_input_tokens": total_cr,
        "cache_creation": {
            "ephemeral_5m_input_tokens": total_5m,
            "ephemeral_1h_input_tokens": total_1h,
        },
    }


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# -------------------- API helpers (raw HTTP) --------------------


def find_or_create_environment(beta: str) -> str:
    """Reuse air-review-env when present; create a bare env otherwise.
    Avoids creating one-per-run."""
    h = headers(beta)
    r = req.get(f"{API_BASE}/environments?limit=100", headers=h)
    r.raise_for_status()
    for env in r.json().get("data", []):
        if env.get("archived_at"):
            continue
        if env["name"] in ("air-review-env", f"{TEST_PREFIX}-env"):
            return env["id"]
    r = req.post(
        f"{API_BASE}/environments",
        headers=h,
        json={"name": f"{TEST_PREFIX}-env", "description": "Cost test harness env"},
    )
    r.raise_for_status()
    return r.json()["id"]


def create_agent(beta: str, name: str, system: str,
                 callable_agents: list[dict] | None = None,
                 model: str = DEFAULT_MODEL) -> dict:
    body = {
        "name": name,
        "model": model,
        "system": system,
        "tools": [{"type": "agent_toolset_20260401"}],
    }
    if callable_agents:
        body["callable_agents"] = callable_agents
    r = req.post(f"{API_BASE}/agents", headers=headers(beta), json=body)
    if not r.ok:
        sys.exit(f"agent create failed: {r.status_code} {r.text[:300]}")
    return r.json()


def archive_agent(beta: str, agent_id: str) -> None:
    req.post(f"{API_BASE}/agents/{agent_id}/archive", headers=headers(beta))


def create_memory_store(beta: str, name: str, description: str) -> str:
    r = req.post(
        f"{API_BASE}/memory_stores",
        headers=headers(beta),
        json={"name": name, "description": description},
    )
    if not r.ok:
        sys.exit(f"memory_store create failed: {r.status_code} {r.text[:300]}")
    return r.json()["id"]


def seed_memory(beta: str, store_id: str, path: str, content: str) -> None:
    r = req.post(
        f"{API_BASE}/memory_stores/{store_id}/memories",
        headers=headers(beta),
        json={"path": path, "content": content},
    )
    if not r.ok:
        sys.exit(f"memory seed failed: {r.status_code} {r.text[:300]}")


def make_wiki_memory_store(beta: str, name_suffix: str) -> str:
    """Create a memory store and seed it with the two wiki fixtures."""
    store_id = create_memory_store(
        beta, f"{TEST_PREFIX}-{name_suffix}",
        "Wiki context (REVIEW.md, PROJECT-PROFILE.md)",
    )
    seed_memory(beta, store_id, "/wiki/REVIEW.md", load_fixture("wiki_REVIEW.md"))
    seed_memory(beta, store_id, "/wiki/PROJECT-PROFILE.md", load_fixture("wiki_PROJECT-PROFILE.md"))
    return store_id


def archive_memory_store(beta: str, store_id: str) -> None:
    req.post(f"{API_BASE}/memory_stores/{store_id}/archive", headers=headers(beta))


def create_session(beta: str, agent_id: str, env_id: str,
                   resources: list | None = None) -> str:
    body = {"agent": agent_id, "environment_id": env_id}
    if resources:
        body["resources"] = resources
    r = req.post(f"{API_BASE}/sessions", headers=headers(beta), json=body)
    if not r.ok:
        sys.exit(f"session create failed: {r.status_code} {r.text[:300]}")
    return r.json()["id"]


def send_define_outcome(beta: str, session_id: str, description: str,
                        rubric_text: str, max_iterations: int = 3) -> None:
    """Send a user.define_outcome event to start the outcome loop. The
    agent begins working immediately — no separate user.message needed."""
    r = req.post(
        f"{API_BASE}/sessions/{session_id}/events",
        headers=headers(beta),
        json={
            "events": [
                {
                    "type": "user.define_outcome",
                    "description": description,
                    "rubric": {"type": "text", "content": rubric_text},
                    "max_iterations": max_iterations,
                }
            ]
        },
    )
    if not r.ok:
        sys.exit(f"define_outcome failed: {r.status_code} {r.text[:300]}")


def send_user_message(beta: str, session_id: str, text: str) -> None:
    r = req.post(
        f"{API_BASE}/sessions/{session_id}/events",
        headers=headers(beta),
        json={"events": [{"type": "user.message", "content": [{"type": "text", "text": text}]}]},
    )
    if not r.ok:
        sys.exit(f"send message failed: {r.status_code} {r.text[:300]}")


def wait_for_idle(beta: str, session_id: str, timeout_s: int = 600) -> dict:
    """Poll session until it has IDLED AFTER doing work. Sessions start
    out idle (waiting for input); accepting that initial idle as "done"
    returns immediately with zero usage. Track that we've seen `running`
    at least once before accepting `idle` as the terminal state."""
    deadline = time.monotonic() + timeout_s
    seen_running = False
    while time.monotonic() < deadline:
        r = req.get(f"{API_BASE}/sessions/{session_id}", headers=headers(beta))
        r.raise_for_status()
        s = r.json()
        status = s.get("status")
        if status == "running":
            seen_running = True
        if status == "idle" and seen_running:
            return s
        if status in ("terminated", "failed"):
            return s
        time.sleep(5)
    sys.exit(f"timed out after {timeout_s}s waiting for session {session_id}")


def fetch_session_output(beta: str, session_id: str, max_chars: int = 2500) -> str:
    """Pull agent.message text content. Truncated to keep results.jsonl small."""
    r = req.get(f"{API_BASE}/sessions/{session_id}/events?limit=200", headers=headers(beta))
    if not r.ok:
        return f"[fetch error {r.status_code}]"
    parts = []
    for ev in r.json().get("data", []):
        if ev.get("type") != "agent.message":
            continue
        for block in ev.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
    full = "".join(parts).strip()
    return full if len(full) <= max_chars else full[:max_chars] + "\n…[truncated]"


# -------------------- prompts --------------------


def review_user_message_with_wiki() -> str:
    """Review user message with wiki content INLINED in the prefix —
    matches today's managed/review.py behavior. Used by variants that
    don't have memory."""
    ctx = load_fixture("test_pr_context.txt")
    review_md = load_fixture("wiki_REVIEW.md")
    profile = load_fixture("wiki_PROJECT-PROFILE.md")
    diff = load_fixture("test_pr.diff")
    return f"""{ctx}

<wiki>
== REVIEW.md ==
{review_md}

== PROJECT-PROFILE.md ==
{profile}
</wiki>

<diff>
{diff}
</diff>

Review the diff and produce findings."""


def review_user_message_memory_mode() -> str:
    """Review user message that POINTS to wiki in memory store instead of
    inlining it. Used by memory variants — initial prefix is much smaller,
    agent reads /mnt/memory/wiki/* on demand."""
    ctx = load_fixture("test_pr_context.txt")
    diff = load_fixture("test_pr.diff")
    return f"""{ctx}

Wiki context is mounted at /mnt/memory/wiki/. Read REVIEW.md and
PROJECT-PROFILE.md from there before reviewing.

<diff>
{diff}
</diff>

Review the diff and produce findings."""


def verify_user_message(reviewer_findings: str) -> str:
    """Verifier user message — receives the reviewer's findings + the diff
    and verifies each finding by reading the actual code."""
    diff = load_fixture("test_pr.diff")
    return f"""You verify code review findings.

<diff>
{diff}
</diff>

<findings>
{reviewer_findings}
</findings>

For each finding, score 0-100 confidence (drop <60), classify
CONFIRMED / FALSE POSITIVE / DOWNGRADED. Output the cleaned-up
findings list only. No safety preambles."""


def coordinator_user_message_inline_wiki() -> str:
    """Coordinator user message — drives the multiagent flow. Wiki inlined."""
    msg = review_user_message_with_wiki()
    return msg + (
        "\n\nDelegate to the reviewer agent. When you have its findings, "
        "delegate to the verifier agent with the findings + diff. "
        "Return the verified findings list only."
    )


def coordinator_user_message_memory_mode() -> str:
    """Coordinator user message — multiagent + memory variant."""
    msg = review_user_message_memory_mode()
    return msg + (
        "\n\nDelegate to the reviewer agent. When you have its findings, "
        "delegate to the verifier agent with the findings + diff. "
        "Return the verified findings list only."
    )


def solo_user_message_inline_wiki() -> str:
    """Solo user message — one agent does both review and verify."""
    msg = review_user_message_with_wiki()
    return msg + (
        "\n\nProduce findings, then verify each one yourself by re-reading "
        "the cited line. Drop any finding you can't confidently confirm. "
        "Output the final cleaned-up findings list only."
    )


def solo_user_message_memory_mode() -> str:
    msg = review_user_message_memory_mode()
    return msg + (
        "\n\nProduce findings, then verify each one yourself by re-reading "
        "the cited line. Drop any finding you can't confidently confirm. "
        "Output the final cleaned-up findings list only."
    )


REVIEWER_SYSTEM = """You are a code reviewer. Read the diff and report findings:
bugs, security, error handling, design issues. Severity: blocker/medium/low/nit.
EVERY finding must include file:line. No safety preambles, no 'is not malware'-
style narration. Output structured findings only.
""".strip()

VERIFIER_SYSTEM = """You verify code review findings. The user gives a diff
and a list of findings. For each: read the relevant code, score 0-100
confidence (drop <60), classify CONFIRMED / FALSE POSITIVE / DOWNGRADED.
Output the cleaned findings list only. No safety preambles.
""".strip()

COORDINATOR_SYSTEM = """You coordinate code review work. Delegate the diff
to the reviewer agent. When you receive its findings, delegate to the
verifier with the findings + diff. Return the verifier's final report.
Do not duplicate the reviewer's analysis yourself.
""".strip()

SOLO_SYSTEM = """You are a code reviewer AND verifier in one role.
First produce findings on the diff (bugs, security, design, errors).
Then verify each finding by re-reading the cited line — drop any you
can't confidently confirm. Output the final cleaned-up findings list
only. No safety preambles, no narration about your process.
""".strip()


# -------------------- variants --------------------
# Each variant runs the SAME net work: review the diff + verify findings.
# Differences are purely orchestration / feature use.


def run_split() -> dict:
    """Two separate sessions: reviewer then verifier. Today's split-role
    architecture, in miniature."""
    beta = GA_BETA
    env_id = find_or_create_environment(beta)

    reviewer = create_agent(beta, f"{TEST_PREFIX}-split-reviewer", REVIEWER_SYSTEM)
    verifier = create_agent(beta, f"{TEST_PREFIX}-split-verifier", VERIFIER_SYSTEM)

    # Step 1: reviewer session
    rev_sess = create_session(beta, reviewer["id"], env_id)
    t0 = time.monotonic()
    send_user_message(beta, rev_sess, review_user_message_with_wiki())
    rev_final = wait_for_idle(beta, rev_sess)
    rev_output = fetch_session_output(beta, rev_sess, max_chars=8000)

    # Step 2: verifier session, fed the reviewer's findings
    ver_sess = create_session(beta, verifier["id"], env_id)
    send_user_message(beta, ver_sess, verify_user_message(rev_output))
    ver_final = wait_for_idle(beta, ver_sess)
    wall = time.monotonic() - t0
    ver_output = fetch_session_output(beta, ver_sess, max_chars=2500)

    aggregated = aggregate_usage([rev_final.get("usage") or {}, ver_final.get("usage") or {}])
    active = ((rev_final.get("stats") or {}).get("active_seconds", 0)
            + (ver_final.get("stats") or {}).get("active_seconds", 0))

    return _make_result(
        variant="split",
        session_ids=[rev_sess, ver_sess],
        agent_ids=[reviewer["id"], verifier["id"]],
        store_ids=[],
        beta=beta,
        usage=aggregated,
        model=DEFAULT_MODEL,
        active_seconds=active,
        wall_seconds=wall,
        output_sample=ver_output,
    )


def run_split_memory() -> dict:
    """Split + memory."""
    beta = GA_BETA
    env_id = find_or_create_environment(beta)
    store_id = make_wiki_memory_store(beta, "split-memory-wiki")

    reviewer = create_agent(beta, f"{TEST_PREFIX}-split-mem-reviewer", REVIEWER_SYSTEM)
    verifier = create_agent(beta, f"{TEST_PREFIX}-split-mem-verifier", VERIFIER_SYSTEM)

    mem_resources = [{
        "type": "memory_store",
        "memory_store_id": store_id,
        "access": "read_only",
        "instructions": "Wiki context. Read /mnt/memory/wiki/ files before reviewing.",
    }]

    rev_sess = create_session(beta, reviewer["id"], env_id, resources=mem_resources)
    t0 = time.monotonic()
    send_user_message(beta, rev_sess, review_user_message_memory_mode())
    rev_final = wait_for_idle(beta, rev_sess)
    rev_output = fetch_session_output(beta, rev_sess, max_chars=8000)

    # Verifier doesn't need wiki — only ACCEPTED-PATTERNS / SEVERITY-CALIBRATION
    # which we don't model here. Skip memory on verifier.
    ver_sess = create_session(beta, verifier["id"], env_id)
    send_user_message(beta, ver_sess, verify_user_message(rev_output))
    ver_final = wait_for_idle(beta, ver_sess)
    wall = time.monotonic() - t0
    ver_output = fetch_session_output(beta, ver_sess, max_chars=2500)

    aggregated = aggregate_usage([rev_final.get("usage") or {}, ver_final.get("usage") or {}])
    active = ((rev_final.get("stats") or {}).get("active_seconds", 0)
            + (ver_final.get("stats") or {}).get("active_seconds", 0))

    return _make_result(
        variant="split_memory",
        session_ids=[rev_sess, ver_sess],
        agent_ids=[reviewer["id"], verifier["id"]],
        store_ids=[store_id],
        beta=beta,
        usage=aggregated,
        model=DEFAULT_MODEL,
        active_seconds=active,
        wall_seconds=wall,
        output_sample=ver_output,
    )


def run_multiagent() -> dict:
    """One session, coordinator + reviewer thread + verifier thread."""
    beta = RP_BETA
    env_id = find_or_create_environment(beta)

    reviewer = create_agent(beta, f"{TEST_PREFIX}-mag-reviewer", REVIEWER_SYSTEM)
    verifier = create_agent(beta, f"{TEST_PREFIX}-mag-verifier", VERIFIER_SYSTEM)
    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-mag-coordinator", COORDINATOR_SYSTEM,
        callable_agents=[
            {"type": "agent", "id": reviewer["id"], "version": reviewer["version"]},
            {"type": "agent", "id": verifier["id"], "version": verifier["version"]},
        ],
    )

    sess = create_session(beta, coordinator["id"], env_id)
    t0 = time.monotonic()
    send_user_message(beta, sess, coordinator_user_message_inline_wiki())
    final = wait_for_idle(beta, sess)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=2500)

    # In multiagent, the session-level usage is the parent thread.
    # Sub-agent thread usage may need to be summed separately. Try the
    # simple session.usage first; if it looks low (< 10k), the threads
    # endpoint probably has the real numbers.
    usage = final.get("usage") or {}

    return _make_result(
        variant="multiagent",
        session_ids=[sess],
        agent_ids=[coordinator["id"], reviewer["id"], verifier["id"]],
        store_ids=[],
        beta=beta,
        usage=usage,
        model=DEFAULT_MODEL,
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample,
    )


def run_multiagent_memory() -> dict:
    beta = RP_BETA
    env_id = find_or_create_environment(beta)
    store_id = make_wiki_memory_store(beta, "mag-mem-wiki")

    reviewer = create_agent(beta, f"{TEST_PREFIX}-magmem-reviewer", REVIEWER_SYSTEM)
    verifier = create_agent(beta, f"{TEST_PREFIX}-magmem-verifier", VERIFIER_SYSTEM)
    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-magmem-coordinator", COORDINATOR_SYSTEM,
        callable_agents=[
            {"type": "agent", "id": reviewer["id"], "version": reviewer["version"]},
            {"type": "agent", "id": verifier["id"], "version": verifier["version"]},
        ],
    )

    sess = create_session(
        beta, coordinator["id"], env_id,
        resources=[{
            "type": "memory_store", "memory_store_id": store_id,
            "access": "read_only",
            "instructions": "Wiki context. Read /mnt/memory/wiki/ files when needed.",
        }],
    )
    t0 = time.monotonic()
    send_user_message(beta, sess, coordinator_user_message_memory_mode())
    final = wait_for_idle(beta, sess)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=2500)

    return _make_result(
        variant="multiagent_memory",
        session_ids=[sess],
        agent_ids=[coordinator["id"], reviewer["id"], verifier["id"]],
        store_ids=[store_id],
        beta=beta,
        usage=final.get("usage") or {},
        model=DEFAULT_MODEL,
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample,
    )


def run_solo() -> dict:
    """One session, ONE agent with a combined review+verify role."""
    beta = GA_BETA
    env_id = find_or_create_environment(beta)
    agent = create_agent(beta, f"{TEST_PREFIX}-solo-reviewer", SOLO_SYSTEM)

    sess = create_session(beta, agent["id"], env_id)
    t0 = time.monotonic()
    send_user_message(beta, sess, solo_user_message_inline_wiki())
    final = wait_for_idle(beta, sess)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=2500)

    return _make_result(
        variant="solo",
        session_ids=[sess],
        agent_ids=[agent["id"]],
        store_ids=[],
        beta=beta,
        usage=final.get("usage") or {},
        model=DEFAULT_MODEL,
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample,
    )


def run_solo_memory() -> dict:
    beta = GA_BETA
    env_id = find_or_create_environment(beta)
    store_id = make_wiki_memory_store(beta, "solo-mem-wiki")

    agent = create_agent(beta, f"{TEST_PREFIX}-solo-mem-reviewer", SOLO_SYSTEM)
    sess = create_session(
        beta, agent["id"], env_id,
        resources=[{
            "type": "memory_store", "memory_store_id": store_id,
            "access": "read_only",
            "instructions": "Wiki context. Read /mnt/memory/wiki/ files before reviewing.",
        }],
    )
    t0 = time.monotonic()
    send_user_message(beta, sess, solo_user_message_memory_mode())
    final = wait_for_idle(beta, sess)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=2500)

    return _make_result(
        variant="solo_memory",
        session_ids=[sess],
        agent_ids=[agent["id"]],
        store_ids=[store_id],
        beta=beta,
        usage=final.get("usage") or {},
        model=DEFAULT_MODEL,
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample,
    )


REVIEW_RUBRIC = """# Code Review Rubric

## Required CONFIRMED findings
The verified findings list must include all three of these (each with file:line):
- SQL injection in `get_user_by_email` (f-string interpolation of `email` into a SQL query)
- Raw password logged via `print()` in the `login` handler
- `debug=True` passed to `app.run` in the `__main__` block

## Format
- Each finding has a severity (blocker / medium / low / nit)
- Each finding cites a file path AND a specific line number
- No safety-narration preamble like "this is not malware" — findings only

## Quality
- No duplicate findings
- No false positives (the verifier should drop low-confidence claims)
"""


def run_multiagent_outcomes() -> dict:
    """Multiagent + Outcomes self-eval. The agent receives a rubric via
    user.define_outcome and iterates until satisfied or max_iterations hit."""
    beta = RP_BETA
    env_id = find_or_create_environment(beta)

    reviewer = create_agent(beta, f"{TEST_PREFIX}-out-reviewer", REVIEWER_SYSTEM)
    verifier = create_agent(beta, f"{TEST_PREFIX}-out-verifier", VERIFIER_SYSTEM)
    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-out-coordinator", COORDINATOR_SYSTEM,
        callable_agents=[
            {"type": "agent", "id": reviewer["id"], "version": reviewer["version"]},
            {"type": "agent", "id": verifier["id"], "version": verifier["version"]},
        ],
    )

    sess = create_session(beta, coordinator["id"], env_id)
    t0 = time.monotonic()
    # Outcomes pattern: user.define_outcome instead of user.message. Agent
    # starts working immediately. Description carries the same diff +
    # wiki + delegation instructions a normal user message would, since
    # there's no separate message event.
    description = (
        coordinator_user_message_inline_wiki()
        + "\n\nProduce a verified findings list satisfying the rubric below."
    )
    send_define_outcome(beta, sess, description, REVIEW_RUBRIC, max_iterations=3)
    final = wait_for_idle(beta, sess)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=2500)

    return _make_result(
        variant="multiagent_outcomes",
        session_ids=[sess],
        agent_ids=[coordinator["id"], reviewer["id"], verifier["id"]],
        store_ids=[],
        beta=beta,
        usage=final.get("usage") or {},
        model=DEFAULT_MODEL,
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample,
    )


def run_multiagent_parallel() -> dict:
    """Tests whether multi-agent truly fans out INDEPENDENT sub-agents
    in parallel. Three reviewer agents (different focus areas: bugs,
    security, style) review the SAME diff. The coordinator's job is
    only to dispatch all three at once and merge their outputs.

    What to look for in the result:
      compute_seconds / wall_seconds  ratio
        ≈ 1.0  →  sub-agents ran SEQUENTIALLY (multi-agent is wall-time-equal-to-cost regression)
        ≈ 3.0  →  sub-agents ran fully concurrent (3 threads in parallel) — the win
        between → partial parallelism

    No verifier in this variant — the question is "do independent
    sub-agents fan out", not "review-verify orchestration".
    """
    beta = RP_BETA
    env_id = find_or_create_environment(beta)

    bugs = create_agent(beta, f"{TEST_PREFIX}-mp-bugs",
                         "You are a code reviewer focused exclusively on LOGIC BUGS, "
                         "error handling, edge cases, and design issues. Skip security "
                         "and style. Output findings only, no preambles. file:line + severity.")
    security = create_agent(beta, f"{TEST_PREFIX}-mp-security",
                             "You are a security auditor. Focus exclusively on injection, "
                             "authentication gaps, data exposure, and credential leakage. "
                             "Skip logic bugs and style. Findings only, no preambles. "
                             "file:line + severity.")
    style = create_agent(beta, f"{TEST_PREFIX}-mp-style",
                         "You are a code-style reviewer. Focus exclusively on readability, "
                         "naming conventions, code-organization smells. Skip security and "
                         "logic bugs. Findings only, no preambles. file:line + severity.")

    parallel_coordinator_system = (
        "You coordinate code review by fanning out to THREE specialist agents IN PARALLEL: "
        "one for bugs, one for security, one for style. Use the agent_toolset to call "
        "ALL THREE at the same time on the SAME diff (do not call them sequentially — "
        "issue all three delegations in a single turn). Wait for all to complete, then "
        "concatenate their findings into one report. Do not duplicate their analysis."
    )

    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-mp-coordinator", parallel_coordinator_system,
        callable_agents=[
            {"type": "agent", "id": bugs["id"], "version": bugs["version"]},
            {"type": "agent", "id": security["id"], "version": security["version"]},
            {"type": "agent", "id": style["id"], "version": style["version"]},
        ],
    )

    sess = create_session(beta, coordinator["id"], env_id)
    t0 = time.monotonic()
    send_user_message(beta, sess, baseline_user_message_for_parallel())
    final = wait_for_idle(beta, sess)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=2500)

    return _make_result(
        variant="multiagent_parallel",
        session_ids=[sess],
        agent_ids=[coordinator["id"], bugs["id"], security["id"], style["id"]],
        store_ids=[],
        beta=beta,
        usage=final.get("usage") or {},
        model=DEFAULT_MODEL,
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample,
    )


def baseline_user_message_for_parallel() -> str:
    """User message variant for the parallel test — emphasizes that the
    SAME diff should be sent to ALL THREE sub-agents at once."""
    ctx = load_fixture("test_pr_context.txt")
    review_md = load_fixture("wiki_REVIEW.md")
    profile = load_fixture("wiki_PROJECT-PROFILE.md")
    diff = load_fixture("test_pr.diff")
    return f"""{ctx}

<wiki>
== REVIEW.md ==
{review_md}

== PROJECT-PROFILE.md ==
{profile}
</wiki>

<diff>
{diff}
</diff>

Delegate the diff above to ALL THREE specialist agents (bugs, security, style)
in PARALLEL — call all three in a single coordination turn. Wait for all three
to return findings, then concatenate them into one report. Do not analyze the
diff yourself."""


def run_multiagent_tiered() -> dict:
    """The user's hypothesis: an Opus coordinator that does context-prep
    once and hands SMALL targeted slices to cheap (Haiku) sub-agents.
    Should save money if (a) the coordinator successfully reduces
    sub-agent context size, and (b) coordinator narration is terse.

    The coordinator slices the diff and the wiki guidance into 3
    focus-specific tasks before delegating. Sub-agents get only their
    slice — not the full diff and wiki — so their cache costs are tiny.
    """
    beta = RP_BETA
    env_id = find_or_create_environment(beta)

    # Sub-agents on Haiku — mechanical pattern-match work
    bugs = create_agent(
        beta, f"{TEST_PREFIX}-mt-bugs",
        "You are a bug detective. The user gives you specific code lines "
        "+ what to check for. Output findings only — no preambles, no "
        "summary. Format: file:line | severity | description.",
        model="claude-haiku-4-5",
    )
    security = create_agent(
        beta, f"{TEST_PREFIX}-mt-security",
        "You are a security auditor. The user gives you specific code "
        "lines + which checks to apply. Output findings only — no "
        "preambles. Format: file:line | severity | description.",
        model="claude-haiku-4-5",
    )
    style = create_agent(
        beta, f"{TEST_PREFIX}-mt-style",
        "You are a code-style reviewer. The user gives you specific "
        "code lines + style rules. Output findings only — no preambles. "
        "Format: file:line | severity | description.",
        model="claude-haiku-4-5",
    )

    tiered_coordinator_system = (
        "You are a code-review coordinator. Your job: load full PR context "
        "(CLAUDE.md, wiki, diff), then dispatch 3 SHORT targeted tasks to "
        "specialist sub-agents — bugs, security, style — IN PARALLEL in "
        "ONE turn. Each task should include ONLY the specific lines that "
        "specialist needs to check, plus 1-2 sentences of guidance. Do NOT "
        "send the full diff or wiki to sub-agents. After all three return, "
        "synthesize into a single findings list. Be terse — no narration "
        "between delegations or before synthesis."
    )

    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-mt-coordinator", tiered_coordinator_system,
        callable_agents=[
            {"type": "agent", "id": bugs["id"], "version": bugs["version"]},
            {"type": "agent", "id": security["id"], "version": security["version"]},
            {"type": "agent", "id": style["id"], "version": style["version"]},
        ],
        model="claude-opus-4-7",  # Opus for the coordinator's slicing decisions
    )

    sess = create_session(beta, coordinator["id"], env_id)
    t0 = time.monotonic()
    send_user_message(beta, sess, baseline_user_message_for_parallel())
    final = wait_for_idle(beta, sess)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=2500)

    # Cost calculation needs to handle MIXED models. The session.usage
    # aggregates ALL token usage but doesn't break out by model. The
    # coordinator (Opus) and sub-agents (Haiku) have different prices.
    # Pull thread usage individually for accurate cost.
    accurate_cost = _mixed_model_cost(beta, sess, [
        ("claude-opus-4-7", coordinator["id"]),
        ("claude-haiku-4-5", bugs["id"]),
        ("claude-haiku-4-5", security["id"]),
        ("claude-haiku-4-5", style["id"]),
    ], fallback_usage=final.get("usage") or {})

    result = _make_result(
        variant="multiagent_tiered",
        session_ids=[sess],
        agent_ids=[coordinator["id"], bugs["id"], security["id"], style["id"]],
        store_ids=[],
        beta=beta,
        usage=final.get("usage") or {},
        model="mixed-opus-haiku",
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample,
    )
    # Override the cost field with the per-model accurate calculation.
    result["cost_usd"] = round(accurate_cost, 4)
    return result


def load_real_pr_fixtures() -> dict:
    """Load real-PR + wiki fixtures for the apples-to-apples production
    comparison. Defaults to PR #38; override via env var
    `EXPERIMENTS_PR_FIXTURE=real_pr40` to pick a different PR fixture
    dir under fixtures/."""
    fixture_subdir = os.environ.get("EXPERIMENTS_PR_FIXTURE", "real_pr38")
    base = FIXTURES / fixture_subdir
    return {
        "diff": (base / "pr.diff").read_text(),
        "meta": json.loads((base / "pr_meta.json").read_text()),
        "review_md": (base / "REVIEW.md").read_text(),
        "project_profile": (base / "PROJECT-PROFILE.md").read_text(),
        "review_history": (base / "REVIEW-HISTORY.md").read_text(),
        "glossary": (base / "GLOSSARY.md").read_text(),
    }


def load_production_agent_prompt(name: str) -> str:
    """Load the EXACT production agent prompt body from
    plugins/air/agents/<name>.md (strip YAML frontmatter)."""
    path = HERE.parent.parent / "plugins" / "air" / "agents" / f"{name}.md"
    text = path.read_text()
    if text.startswith("---"):
        # Frontmatter is `---\nname: ...\n---\n<body>`
        _, _, body = text[4:].partition("\n---\n")
        return body.strip()
    return text


def real_pr_context_block(fix: dict) -> str:
    """Build the PR Context block production sends to specialists, using
    PR #38's metadata + real wiki content."""
    meta = fix["meta"]
    body_short = (meta.get("body", "") or "")[:1500]
    return f"""**PR Context:**
- PR: #{meta['number']} by {meta['author']['login']}
- <pr-title>{meta['title']}</pr-title>
- <pr-body>{body_short}</pr-body>
- Base: {meta['baseRefName']} -> {meta['headRefName']}
- Size: +{meta['additions']}/-{meta['deletions']}, {meta['changedFiles']} files
- HEAD: {meta['headRefOid']}
- Repo: VorobiovD/air

<wiki-REVIEW>
{fix['review_md'][:8000]}
</wiki-REVIEW>

<wiki-PROJECT-PROFILE>
{fix['project_profile'][:8000]}
</wiki-PROJECT-PROFILE>

<wiki-REVIEW-HISTORY>
{fix['review_history'][:4000]}
</wiki-REVIEW-HISTORY>

<wiki-GLOSSARY>
{fix['glossary'][:4000]}
</wiki-GLOSSARY>

<diff>
{fix['diff']}
</diff>

Review the diff. Output findings only — no preambles, no narration.
file:line + severity (blocker/medium/low/nit)."""


def run_one_specialist_session(beta: str, env_id: str, agent_id: str,
                                 model: str, user_msg: str) -> dict:
    """Run one specialist session sync, return its usage + output. Used
    by run_production_clone where we run 4 of these concurrently."""
    sess = create_session(beta, agent_id, env_id)
    t0 = time.monotonic()
    send_user_message(beta, sess, user_msg)
    final = wait_for_idle(beta, sess, timeout_s=900)
    wall = time.monotonic() - t0
    output = fetch_session_output(beta, sess, max_chars=12000)
    return {
        "session_id": sess,
        "usage": final.get("usage") or {},
        "model": model,
        "active_seconds": (final.get("stats") or {}).get("active_seconds", 0),
        "wall_seconds": wall,
        "output": output,
    }


def run_production_clone() -> dict:
    """Replicate production's review architecture on real PR #38:
       - 4 specialists in PARALLEL (code-reviewer, simplify, security,
         git-history)
       - Verifier sequentially after, with all 4 specialists' findings
       Each agent uses production's actual system prompt (from
       plugins/air/agents/<name>.md) and production's actual model.

       This is the apples-to-apples baseline for v2_real.
    """
    import concurrent.futures
    beta = GA_BETA
    env_id = find_or_create_environment(beta)
    fix = load_real_pr_fixtures()
    pr_ctx = real_pr_context_block(fix)

    # Production agent definitions: (name, model, system-prompt-source)
    spec_defs = [
        ("code-reviewer",         "claude-opus-4-7",   "code-reviewer"),
        ("simplify",              "claude-sonnet-4-6", "simplify"),
        ("security-auditor",      "claude-opus-4-7",   "security-auditor"),
        ("git-history-reviewer",  "claude-sonnet-4-6", "git-history-reviewer"),
    ]
    agents = []
    for name, model, prompt_file in spec_defs:
        ag = create_agent(
            beta, f"{TEST_PREFIX}-prod-{name}",
            load_production_agent_prompt(prompt_file),
            model=model,
        )
        agents.append((name, model, ag))

    # Phase 1: 4 specialists in parallel via threads. Each session
    # creation + user.message + wait_for_idle is its own thread.
    t0 = time.monotonic()
    parallel_results: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            ex.submit(run_one_specialist_session, beta, env_id, ag["id"], model, pr_ctx): name
            for (name, model, ag) in agents
        }
        for fut in concurrent.futures.as_completed(futs):
            name = futs[fut]
            parallel_results[name] = fut.result()

    phase1_wall = time.monotonic() - t0

    # Phase 2: verifier reads all 4 specialists' findings + diff, scores
    findings_blob = "\n\n".join(
        f"=== {name} findings ===\n{r['output']}"
        for name, r in parallel_results.items()
    )
    verifier_msg = (
        pr_ctx
        + f"\n\n<all-findings>\n{findings_blob}\n</all-findings>\n\n"
        + "Score each finding 0-100 (drop <60), classify CONFIRMED / "
          "DOWNGRADED / FALSE POSITIVE / PRE-EXISTING. Output the cleaned "
          "findings list grouped by severity."
    )
    verifier_agent = create_agent(
        beta, f"{TEST_PREFIX}-prod-verifier",
        load_production_agent_prompt("review-verifier"),
        model="claude-opus-4-7",
    )
    ver_sess = create_session(beta, verifier_agent["id"], env_id)
    t1 = time.monotonic()
    send_user_message(beta, ver_sess, verifier_msg)
    ver_final = wait_for_idle(beta, ver_sess, timeout_s=900)
    phase2_wall = time.monotonic() - t1
    total_wall = time.monotonic() - t0
    ver_output = fetch_session_output(beta, ver_sess, max_chars=12000)

    # Cost: sum specialists (each at its model) + verifier (Opus).
    total_cost = 0.0
    total_active = 0.0
    for name, r in parallel_results.items():
        total_cost += cost_of(r["usage"], r["model"])
        total_active += r["active_seconds"]
    ver_usage = ver_final.get("usage") or {}
    total_cost += cost_of(ver_usage, "claude-opus-4-7")
    total_active += (ver_final.get("stats") or {}).get("active_seconds", 0)

    # Use the verifier's output as the canonical sample (final review)
    return {
        "variant": "production_clone",
        "session_ids": [r["session_id"] for r in parallel_results.values()] + [ver_sess],
        "agent_ids": [a[2]["id"] for a in agents] + [verifier_agent["id"]],
        "store_ids": [],
        "beta": beta,
        "model": "mixed-prod",
        "input_tokens": sum(r["usage"].get("input_tokens", 0) for r in parallel_results.values()) + ver_usage.get("input_tokens", 0),
        "output_tokens": sum(r["usage"].get("output_tokens", 0) for r in parallel_results.values()) + ver_usage.get("output_tokens", 0),
        "cache_read_tokens": sum(r["usage"].get("cache_read_input_tokens", 0) for r in parallel_results.values()) + ver_usage.get("cache_read_input_tokens", 0),
        "cache_create_5m_tokens": sum((r["usage"].get("cache_creation") or {}).get("ephemeral_5m_input_tokens", 0) for r in parallel_results.values()) + (ver_usage.get("cache_creation") or {}).get("ephemeral_5m_input_tokens", 0),
        "cache_create_1h_tokens": 0,
        "active_seconds": round(total_active, 1),
        "wall_seconds": round(total_wall, 1),
        "cost_usd": round(total_cost, 4),
        "output_sample": ver_output[:2500],
        "phase1_wall_seconds": round(phase1_wall, 1),
        "phase2_wall_seconds": round(phase2_wall, 1),
    }


def run_tiered_v2_real() -> dict:
    """v2 tiered architecture on the real PR #38: Sonnet coordinator +
    4 Haiku specialist sub-agents + 1 Sonnet verifier sub-agent. Strict
    2-turn coordinator protocol like multiagent_tiered_v2."""
    beta = RP_BETA
    env_id = find_or_create_environment(beta)
    fix = load_real_pr_fixtures()
    pr_ctx = real_pr_context_block(fix)

    bugs = create_agent(
        beta, f"{TEST_PREFIX}-mtr-bugs",
        "You are a bug detective. The user gives you the PR Context + "
        "diff. Focus on logic bugs, error handling, edge cases, design "
        "issues. Output findings only — no preambles. Format: "
        "file:line | severity | description.",
        model="claude-haiku-4-5",
    )
    security = create_agent(
        beta, f"{TEST_PREFIX}-mtr-security",
        "You are a security auditor. Apply the 31-item security checklist "
        "from PROJECT-PROFILE.md's 'Applicable Security Checks' section. "
        "Output findings only — no preambles. Format: file:line | severity "
        "| description.",
        model="claude-haiku-4-5",
    )
    simplify = create_agent(
        beta, f"{TEST_PREFIX}-mtr-simplify",
        "You are a simplify reviewer. Three dimensions: Code Reuse, Code "
        "Quality, Efficiency. Look for duplication, dead code, "
        "over-complication. Output findings only — no preambles. Format: "
        "file:line | severity | description.",
        model="claude-haiku-4-5",
    )
    git_history = create_agent(
        beta, f"{TEST_PREFIX}-mtr-git-history",
        "You are a git-history reviewer. Use blame and churn signals from "
        "the PR Context. Look for stale assumptions, recurring problems, "
        "absent authors. Output findings only — no preambles. Format: "
        "file:line | severity | description.",
        model="claude-haiku-4-5",
    )
    verifier = create_agent(
        beta, f"{TEST_PREFIX}-mtr-verifier",
        "You verify code review findings. The user gives you the diff + "
        "specialist findings. Score each 0-100, drop <60, classify "
        "CONFIRMED / DOWNGRADED / FALSE POSITIVE. Output cleaned list "
        "grouped by severity. No preambles.",
        model="claude-sonnet-4-6",
    )

    coordinator_system = (
        "You are a code-review coordinator. STRICT PROTOCOL — only 3 turns:\n"
        "\n"
        "TURN 1: Issue all 4 specialist delegations (bugs, security, "
        "simplify, git-history) in PARALLEL — all 4 tool calls in ONE "
        "response. Each delegation: send the relevant code slices + 1-2 "
        "sentences of guidance for that specialist's focus. Do NOT send "
        "the full diff or wiki to specialists. NO commentary between calls.\n"
        "\n"
        "TURN 2: When all 4 specialists return, delegate to the verifier "
        "with all findings + the diff. ONE call. NO narration about your "
        "process.\n"
        "\n"
        "TURN 3: Output the verifier's final list verbatim. NO restatement, "
        "NO commentary.\n"
        "\n"
        "Total: 3 of YOUR turns. No more. No 'Two down, one to go' updates."
    )

    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-mtr-coordinator", coordinator_system,
        callable_agents=[
            {"type": "agent", "id": bugs["id"], "version": bugs["version"]},
            {"type": "agent", "id": security["id"], "version": security["version"]},
            {"type": "agent", "id": simplify["id"], "version": simplify["version"]},
            {"type": "agent", "id": git_history["id"], "version": git_history["version"]},
            {"type": "agent", "id": verifier["id"], "version": verifier["version"]},
        ],
        model="claude-sonnet-4-6",
    )

    sess = create_session(beta, coordinator["id"], env_id)
    t0 = time.monotonic()
    send_user_message(beta, sess, pr_ctx)
    final = wait_for_idle(beta, sess, timeout_s=900)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=12000)

    accurate_cost = _mixed_model_cost(beta, sess, [
        ("claude-sonnet-4-6", coordinator["id"]),
        ("claude-haiku-4-5", bugs["id"]),
        ("claude-haiku-4-5", security["id"]),
        ("claude-haiku-4-5", simplify["id"]),
        ("claude-haiku-4-5", git_history["id"]),
        ("claude-sonnet-4-6", verifier["id"]),
    ], fallback_usage=final.get("usage") or {})

    result = _make_result(
        variant="tiered_v2_real",
        session_ids=[sess],
        agent_ids=[coordinator["id"], bugs["id"], security["id"],
                   simplify["id"], git_history["id"], verifier["id"]],
        store_ids=[],
        beta=beta,
        usage=final.get("usage") or {},
        model="mixed-sonnet-haiku",
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample[:2500],
    )
    result["cost_usd"] = round(accurate_cost, 4)
    return result


def run_tiered_v2_real_prodprompts() -> dict:
    """Ablation 1: same as tiered_v2_real (Sonnet coord + 4 Haiku
    specialists + Sonnet verifier) but the sub-agents use the FULL
    production prompts loaded from plugins/air/agents/*.md.

    Isolates: did the savings come from Haiku, or from the
    stripped-down sub-agent prompts? If this run cost is close to
    tiered_v2_real, prompt-stripping wasn't the savings driver."""
    beta = RP_BETA
    env_id = find_or_create_environment(beta)
    fix = load_real_pr_fixtures()
    pr_ctx = real_pr_context_block(fix)

    bugs = create_agent(beta, f"{TEST_PREFIX}-mtrp-bugs",
                         load_production_agent_prompt("code-reviewer"),
                         model="claude-haiku-4-5")
    security = create_agent(beta, f"{TEST_PREFIX}-mtrp-security",
                             load_production_agent_prompt("security-auditor"),
                             model="claude-haiku-4-5")
    simplify = create_agent(beta, f"{TEST_PREFIX}-mtrp-simplify",
                             load_production_agent_prompt("simplify"),
                             model="claude-haiku-4-5")
    git_history = create_agent(beta, f"{TEST_PREFIX}-mtrp-git-history",
                                load_production_agent_prompt("git-history-reviewer"),
                                model="claude-haiku-4-5")
    verifier = create_agent(beta, f"{TEST_PREFIX}-mtrp-verifier",
                             load_production_agent_prompt("review-verifier"),
                             model="claude-sonnet-4-6")

    coordinator_system = (
        "You are a code-review coordinator. STRICT PROTOCOL — only 3 turns:\n"
        "\n"
        "TURN 1: Issue all 4 specialist delegations (code-reviewer, "
        "simplify, security-auditor, git-history-reviewer) in PARALLEL "
        "in ONE response. Each delegation receives the full PR Context "
        "+ diff (do NOT slice — these specialists' prompts assume full "
        "context). NO commentary between calls.\n"
        "\n"
        "TURN 2: When all 4 return, delegate to the verifier with all "
        "findings + the diff. ONE call. NO narration about your process.\n"
        "\n"
        "TURN 3: Output the verifier's final list verbatim. NO commentary.\n"
        "\n"
        "Total: 3 of YOUR turns. No more."
    )

    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-mtrp-coordinator", coordinator_system,
        callable_agents=[
            {"type": "agent", "id": bugs["id"], "version": bugs["version"]},
            {"type": "agent", "id": security["id"], "version": security["version"]},
            {"type": "agent", "id": simplify["id"], "version": simplify["version"]},
            {"type": "agent", "id": git_history["id"], "version": git_history["version"]},
            {"type": "agent", "id": verifier["id"], "version": verifier["version"]},
        ],
        model="claude-sonnet-4-6",
    )

    sess = create_session(beta, coordinator["id"], env_id)
    t0 = time.monotonic()
    send_user_message(beta, sess, pr_ctx)
    final = wait_for_idle(beta, sess, timeout_s=900)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=12000)

    accurate_cost = _mixed_model_cost(beta, sess, [
        ("claude-sonnet-4-6", coordinator["id"]),
        ("claude-haiku-4-5", bugs["id"]),
        ("claude-haiku-4-5", security["id"]),
        ("claude-haiku-4-5", simplify["id"]),
        ("claude-haiku-4-5", git_history["id"]),
        ("claude-sonnet-4-6", verifier["id"]),
    ], fallback_usage=final.get("usage") or {})

    result = _make_result(
        variant="tiered_v2_real_prodprompts",
        session_ids=[sess],
        agent_ids=[coordinator["id"], bugs["id"], security["id"],
                   simplify["id"], git_history["id"], verifier["id"]],
        store_ids=[], beta=beta,
        usage=final.get("usage") or {},
        model="mixed-sonnet-haiku-prodprompts",
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample[:2500],
    )
    result["cost_usd"] = round(accurate_cost, 4)
    return result


def run_multiagent_codex_full() -> dict:
    """End-to-end test of coordinator-runs-codex pattern. Validates the
    Phase 1 architecture for codex specifically:
      - Coordinator container has @openai/codex pre-installed
      - OPENAI_API_KEY passes via user message (mirrors GH_TOKEN pattern
        in managed/learn.py)
      - Coordinator dispatches codex (Bash) + 4 Claude specialists IN
        PARALLEL in TURN 1
      - Verifier in TURN 2 gets all 5 sources

    Wiki update is NOT included in this variant — that's a separate test.
    """
    if not os.environ.get('OPENAI_API_KEY'):
        sys.exit("OPENAI_API_KEY not set — required for codex test")
    if not os.environ.get('AIR_BOT_TOKEN'):
        sys.exit("AIR_BOT_TOKEN not set — required for github_repository mount")

    beta = RP_BETA
    # Use the test env that has @openai/codex pre-installed
    CODEX_ENV_ID = os.environ.get('CODEX_TEST_ENV_ID', 'env_01V9K6psdXb4ysvePGwiVGHq')

    fix = load_real_pr_fixtures()
    pr_ctx = real_pr_context_block(fix)
    bot_token = os.environ['AIR_BOT_TOKEN']
    openai_key = os.environ['OPENAI_API_KEY']

    # Production-prompt sub-agents (matching managed_production)
    code_reviewer = create_agent(beta, f"{TEST_PREFIX}-cdx-code",
                                  load_production_agent_prompt("code-reviewer"),
                                  model="claude-opus-4-7")
    simplify = create_agent(beta, f"{TEST_PREFIX}-cdx-simplify",
                             load_production_agent_prompt("simplify"),
                             model="claude-sonnet-4-6")
    security = create_agent(beta, f"{TEST_PREFIX}-cdx-security",
                             load_production_agent_prompt("security-auditor"),
                             model="claude-opus-4-7")
    git_history = create_agent(beta, f"{TEST_PREFIX}-cdx-git-history",
                                load_production_agent_prompt("git-history-reviewer"),
                                model="claude-sonnet-4-6")
    verifier = create_agent(beta, f"{TEST_PREFIX}-cdx-verifier",
                             load_production_agent_prompt("review-verifier"),
                             model="claude-opus-4-7")

    # Parallel-TURN-1 prompt — restored for Opus test. Opus is better at
    # following multi-tool parallel-dispatch instructions than Sonnet.
    coordinator_system = (
        "You are a code-review coordinator. STRICT PROTOCOL — only 3 turns:\n"
        "\n"
        "The user message contains an OPENAI_API_KEY line. Codex CLI requires "
        "explicit login (it doesn't read the env var).\n"
        "\n"
        "TURN 1 (FIRST RESPONSE) — issue ALL 5 tool calls IN ONE RESPONSE, in "
        "parallel. Do NOT issue them across multiple turns. Do NOT wait for "
        "any to complete before issuing the others. The runtime executes "
        "tool_use blocks concurrently when they arrive in one response.\n"
        "\n"
        "  TOOL 1 — Bash (chained codex setup + run):\n"
        "    `printf '%s' '<OPENAI_API_KEY-value>' | codex login --with-api-key > /dev/null 2>&1 \\\n"
        "     && cd /workspace/repo \\\n"
        "     && git fetch origin main 2>/dev/null \\\n"
        "     && BASE_SHA=$(git rev-parse origin/main) \\\n"
        "     && codex review --base \"$BASE_SHA\" 2>&1 | head -800`\n"
        "\n"
        "  TOOL 2 — delegate to code-reviewer (full PR Context + diff)\n"
        "  TOOL 3 — delegate to simplify (same)\n"
        "  TOOL 4 — delegate to security-auditor (same)\n"
        "  TOOL 5 — delegate to git-history-reviewer (same)\n"
        "\n"
        "All 5 in your FIRST response. NO commentary between them. The bash "
        "will block ~5 min, but the 4 specialists run concurrently in their "
        "own threads, so total wall time is max(bash, specialists) ≈ 5 min.\n"
        "\n"
        "Critical: the runtime supports multiple tool_use blocks in one "
        "assistant response. USE this — issue all 5 dispatches together.\n"
        "\n"
        "If codex's bash returns error/empty, note as `(codex unavailable: "
        "<reason>)` for TURN 2.\n"
        "\n"
        "TURN 2: When all 5 returned, delegate to verifier with all findings "
        "+ codex output + diff. ONE call. NO narration.\n"
        "\n"
        "TURN 3 (final): Output verifier's findings list VERBATIM.\n"
        "\n"
        "Total: 3 of YOUR turns."
    )

    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-cdx-coordinator", coordinator_system,
        callable_agents=[
            {"type": "agent", "id": code_reviewer["id"], "version": code_reviewer["version"]},
            {"type": "agent", "id": simplify["id"], "version": simplify["version"]},
            {"type": "agent", "id": security["id"], "version": security["version"]},
            {"type": "agent", "id": git_history["id"], "version": git_history["version"]},
            {"type": "agent", "id": verifier["id"], "version": verifier["version"]},
        ],
        model="claude-opus-4-7",  # Opus for better multi-tool parallel dispatch
    )

    # Resolve PR #40 base SHA so codex can `--base <sha>`. The fixture
    # has headRefOid; merge-base with main needs a git operation.
    # Simpler: use 'origin/main' as base (codex resolves the ref).
    head_sha = fix['meta']['headRefOid']
    base_ref = "origin/main"  # let codex resolve; merge-base computed inside codex

    # Mount the air repo + wiki via github_repository resource (with
    # bot_token for auth — same pattern as managed/review.py)
    resources = [
        {
            "type": "github_repository",
            "url": "https://github.com/VorobiovD/air",
            "authorization_token": bot_token,
            "checkout": {"type": "commit", "sha": head_sha},
            "mount_path": "/workspace/repo",
        },
    ]

    sess_r = req.post(
        f"{API_BASE}/sessions",
        headers=headers(beta),
        json={
            "agent": coordinator["id"],
            "environment_id": CODEX_ENV_ID,
            "resources": resources,
        },
    )
    if not sess_r.ok:
        sys.exit(f"session create failed: {sess_r.status_code} {sess_r.text[:400]}")
    sess = sess_r.json()["id"]

    user_msg = (
        f"OPENAI_API_KEY={openai_key}\n"
        f"BASE_SHA={base_ref}\n"
        f"\n"
        f"{pr_ctx}\n"
        f"\n"
        "Coordinate the code review per your protocol. Run codex AND delegate "
        "the 4 specialists in TURN 1 — all in parallel."
    )

    t0 = time.monotonic()
    send_user_message(beta, sess, user_msg)
    final = wait_for_idle(beta, sess, timeout_s=900)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=12000)

    accurate_cost = _mixed_model_cost(beta, sess, [
        ("claude-opus-4-7", coordinator["id"]),  # Opus coord
        ("claude-opus-4-7", code_reviewer["id"]),
        ("claude-sonnet-4-6", simplify["id"]),
        ("claude-opus-4-7", security["id"]),
        ("claude-sonnet-4-6", git_history["id"]),
        ("claude-opus-4-7", verifier["id"]),
    ], fallback_usage=final.get("usage") or {})

    result = _make_result(
        variant="multiagent_codex_full",
        session_ids=[sess],
        agent_ids=[coordinator["id"], code_reviewer["id"], simplify["id"],
                   security["id"], git_history["id"], verifier["id"]],
        store_ids=[], beta=beta,
        usage=final.get("usage") or {},
        model="opus-coord-prod-codex",
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample[:2500],
    )
    result["cost_usd"] = round(accurate_cost, 4)
    return result


def run_multiagent_production() -> dict:
    """Ablation 2: production architecture moved into a SINGLE multiagent
    session, but otherwise unchanged. Same models, same prompts, same
    full context as production_clone — just orchestrated as
    coordinator + sub-agent threads in one session.

    Isolates: does the multi-agent architecture itself save money even
    with current models, prompts, and context? If this run costs about
    the same as production_clone, the multi-agent shape isn't the lever
    — Haiku is."""
    beta = RP_BETA
    env_id = find_or_create_environment(beta)
    fix = load_real_pr_fixtures()
    pr_ctx = real_pr_context_block(fix)

    # Production specialists with their production models.
    code_reviewer = create_agent(beta, f"{TEST_PREFIX}-mp-code",
                                  load_production_agent_prompt("code-reviewer"),
                                  model="claude-opus-4-7")
    simplify = create_agent(beta, f"{TEST_PREFIX}-mp-simplify",
                             load_production_agent_prompt("simplify"),
                             model="claude-sonnet-4-6")
    security = create_agent(beta, f"{TEST_PREFIX}-mp-security",
                             load_production_agent_prompt("security-auditor"),
                             model="claude-opus-4-7")
    git_history = create_agent(beta, f"{TEST_PREFIX}-mp-git-history",
                                load_production_agent_prompt("git-history-reviewer"),
                                model="claude-sonnet-4-6")
    verifier = create_agent(beta, f"{TEST_PREFIX}-mp-verifier",
                             load_production_agent_prompt("review-verifier"),
                             model="claude-opus-4-7")

    coordinator_system = (
        "You are a code-review coordinator. STRICT PROTOCOL — only 3 turns:\n"
        "\n"
        "TURN 1: Issue all 4 specialist delegations (code-reviewer, "
        "simplify, security-auditor, git-history-reviewer) in PARALLEL "
        "in ONE response. Each delegation receives the full PR Context "
        "+ diff. NO commentary between calls.\n"
        "\n"
        "TURN 2: When all 4 return, delegate to the verifier with all "
        "findings + the diff. ONE call. NO narration about your process.\n"
        "\n"
        "TURN 3: Output the verifier's final list verbatim. NO commentary.\n"
        "\n"
        "Total: 3 of YOUR turns. No more."
    )

    # Sonnet coordinator — Opus would be more expensive without adding
    # value (this coordinator's job is just dispatch + verbatim passthrough).
    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-mp-coordinator", coordinator_system,
        callable_agents=[
            {"type": "agent", "id": code_reviewer["id"], "version": code_reviewer["version"]},
            {"type": "agent", "id": simplify["id"], "version": simplify["version"]},
            {"type": "agent", "id": security["id"], "version": security["version"]},
            {"type": "agent", "id": git_history["id"], "version": git_history["version"]},
            {"type": "agent", "id": verifier["id"], "version": verifier["version"]},
        ],
        model="claude-sonnet-4-6",
    )

    sess = create_session(beta, coordinator["id"], env_id)
    t0 = time.monotonic()
    send_user_message(beta, sess, pr_ctx)
    final = wait_for_idle(beta, sess, timeout_s=900)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=12000)

    # Mixed-model cost: Sonnet coord + Opus code-reviewer + Sonnet simplify
    # + Opus security + Sonnet git-history + Opus verifier
    accurate_cost = _mixed_model_cost(beta, sess, [
        ("claude-sonnet-4-6", coordinator["id"]),
        ("claude-opus-4-7", code_reviewer["id"]),
        ("claude-sonnet-4-6", simplify["id"]),
        ("claude-opus-4-7", security["id"]),
        ("claude-sonnet-4-6", git_history["id"]),
        ("claude-opus-4-7", verifier["id"]),
    ], fallback_usage=final.get("usage") or {})

    result = _make_result(
        variant="multiagent_production",
        session_ids=[sess],
        agent_ids=[coordinator["id"], code_reviewer["id"], simplify["id"],
                   security["id"], git_history["id"], verifier["id"]],
        store_ids=[], beta=beta,
        usage=final.get("usage") or {},
        model="mixed-prod-multiagent",
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample[:2500],
    )
    result["cost_usd"] = round(accurate_cost, 4)
    return result


def run_parallel_sessions_haiku() -> dict:
    """Ablation 3: production-style parallel sessions architecture
    (4 specialists in their own sessions + 1 verifier session) but ALL
    specialists run on Haiku. Verifier on Sonnet (matches
    tiered_v2_real_prodprompts).

    Isolates: does the multi-agent architecture provide ANY savings
    beyond the Haiku swap? If this run is comparable to
    tiered_v2_real_prodprompts, multi-agent contributed ~zero — Haiku
    is the whole story."""
    import concurrent.futures
    beta = GA_BETA
    env_id = find_or_create_environment(beta)
    fix = load_real_pr_fixtures()
    pr_ctx = real_pr_context_block(fix)

    # Same production prompts and shape as production_clone — only
    # difference is models.
    spec_defs = [
        ("code-reviewer",        "claude-haiku-4-5",  "code-reviewer"),
        ("simplify",             "claude-haiku-4-5",  "simplify"),
        ("security-auditor",     "claude-haiku-4-5",  "security-auditor"),
        ("git-history-reviewer", "claude-haiku-4-5",  "git-history-reviewer"),
    ]
    agents = []
    for name, model, prompt_file in spec_defs:
        ag = create_agent(
            beta, f"{TEST_PREFIX}-psh-{name}",
            load_production_agent_prompt(prompt_file),
            model=model,
        )
        agents.append((name, model, ag))

    t0 = time.monotonic()
    parallel_results: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            ex.submit(run_one_specialist_session, beta, env_id, ag["id"], model, pr_ctx): name
            for (name, model, ag) in agents
        }
        for fut in concurrent.futures.as_completed(futs):
            name = futs[fut]
            parallel_results[name] = fut.result()
    phase1_wall = time.monotonic() - t0

    # Verifier on Sonnet (same as tiered_v2_real_prodprompts)
    findings_blob = "\n\n".join(
        f"=== {name} findings ===\n{r['output']}"
        for name, r in parallel_results.items()
    )
    verifier_msg = (
        pr_ctx
        + f"\n\n<all-findings>\n{findings_blob}\n</all-findings>\n\n"
        + "Score each finding 0-100 (drop <60), classify CONFIRMED / "
          "DOWNGRADED / FALSE POSITIVE / PRE-EXISTING. Output cleaned "
          "list grouped by severity."
    )
    verifier_agent = create_agent(
        beta, f"{TEST_PREFIX}-psh-verifier",
        load_production_agent_prompt("review-verifier"),
        model="claude-sonnet-4-6",
    )
    ver_sess = create_session(beta, verifier_agent["id"], env_id)
    t1 = time.monotonic()
    send_user_message(beta, ver_sess, verifier_msg)
    ver_final = wait_for_idle(beta, ver_sess, timeout_s=900)
    phase2_wall = time.monotonic() - t1
    total_wall = time.monotonic() - t0
    ver_output = fetch_session_output(beta, ver_sess, max_chars=12000)

    total_cost = 0.0
    total_active = 0.0
    for name, r in parallel_results.items():
        total_cost += cost_of(r["usage"], r["model"])
        total_active += r["active_seconds"]
    ver_usage = ver_final.get("usage") or {}
    total_cost += cost_of(ver_usage, "claude-sonnet-4-6")
    total_active += (ver_final.get("stats") or {}).get("active_seconds", 0)

    return {
        "variant": "parallel_sessions_haiku",
        "session_ids": [r["session_id"] for r in parallel_results.values()] + [ver_sess],
        "agent_ids": [a[2]["id"] for a in agents] + [verifier_agent["id"]],
        "store_ids": [],
        "beta": beta,
        "model": "haiku-specialists-sonnet-verifier",
        "input_tokens": sum(r["usage"].get("input_tokens", 0) for r in parallel_results.values()) + ver_usage.get("input_tokens", 0),
        "output_tokens": sum(r["usage"].get("output_tokens", 0) for r in parallel_results.values()) + ver_usage.get("output_tokens", 0),
        "cache_read_tokens": sum(r["usage"].get("cache_read_input_tokens", 0) for r in parallel_results.values()) + ver_usage.get("cache_read_input_tokens", 0),
        "cache_create_5m_tokens": sum((r["usage"].get("cache_creation") or {}).get("ephemeral_5m_input_tokens", 0) for r in parallel_results.values()) + (ver_usage.get("cache_creation") or {}).get("ephemeral_5m_input_tokens", 0),
        "cache_create_1h_tokens": 0,
        "active_seconds": round(total_active, 1),
        "wall_seconds": round(total_wall, 1),
        "cost_usd": round(total_cost, 4),
        "output_sample": ver_output[:2500],
        "phase1_wall_seconds": round(phase1_wall, 1),
        "phase2_wall_seconds": round(phase2_wall, 1),
    }


def run_multiagent_production_2turn() -> dict:
    """Ablation 4: multi-agent with current models + current prompts +
    STRICT 2-TURN protocol. The coordinator absorbs the verifier role
    (no separate verifier sub-agent) — TURN 1 dispatches 4 specialists,
    TURN 2 synthesizes + filters + scores.

    Isolates: does the 2-turn protocol (vs my multiagent_production's
    3-turn protocol with separate verifier) save additional money?
    Coordinator becomes Opus (matches production's verifier role)."""
    beta = RP_BETA
    env_id = find_or_create_environment(beta)
    fix = load_real_pr_fixtures()
    pr_ctx = real_pr_context_block(fix)

    code_reviewer = create_agent(beta, f"{TEST_PREFIX}-mp2-code",
                                  load_production_agent_prompt("code-reviewer"),
                                  model="claude-opus-4-7")
    simplify = create_agent(beta, f"{TEST_PREFIX}-mp2-simplify",
                             load_production_agent_prompt("simplify"),
                             model="claude-sonnet-4-6")
    security = create_agent(beta, f"{TEST_PREFIX}-mp2-security",
                             load_production_agent_prompt("security-auditor"),
                             model="claude-opus-4-7")
    git_history = create_agent(beta, f"{TEST_PREFIX}-mp2-git-history",
                                load_production_agent_prompt("git-history-reviewer"),
                                model="claude-sonnet-4-6")

    # Coordinator system prompt: 2-turn protocol where TURN 2 = verifier role.
    # Reuse the production verifier's prompt body so the synthesis step
    # applies the same scoring rubric production uses.
    verifier_body = load_production_agent_prompt("review-verifier")
    coordinator_system = (
        "You are a code-review coordinator. STRICT PROTOCOL — only 2 turns:\n"
        "\n"
        "TURN 1: Issue all 4 specialist delegations (code-reviewer, "
        "simplify, security-auditor, git-history-reviewer) in PARALLEL "
        "in ONE response. Each delegation receives the full PR Context "
        "+ diff. NO commentary between calls.\n"
        "\n"
        "TURN 2 (after all 4 return): YOU now perform the verifier role. "
        "Apply the verifier rubric below to all collected findings. Score "
        "each 0-100 confidence (drop <60), classify CONFIRMED / DOWNGRADED "
        "/ FALSE POSITIVE / PRE-EXISTING / IMPROVEMENT. Output the cleaned "
        "findings list grouped by severity. NO process narration.\n"
        "\n"
        "VERIFIER RUBRIC (apply in TURN 2):\n"
        "---\n"
        f"{verifier_body[:6000]}\n"
        "---\n"
        "\n"
        "Total: 2 of YOUR turns. No more."
    )

    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-mp2-coordinator", coordinator_system,
        callable_agents=[
            {"type": "agent", "id": code_reviewer["id"], "version": code_reviewer["version"]},
            {"type": "agent", "id": simplify["id"], "version": simplify["version"]},
            {"type": "agent", "id": security["id"], "version": security["version"]},
            {"type": "agent", "id": git_history["id"], "version": git_history["version"]},
        ],
        model="claude-opus-4-7",  # Opus coordinator — does verifier-grade synthesis in TURN 2
    )

    sess = create_session(beta, coordinator["id"], env_id)
    t0 = time.monotonic()
    send_user_message(beta, sess, pr_ctx)
    final = wait_for_idle(beta, sess, timeout_s=900)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=12000)

    accurate_cost = _mixed_model_cost(beta, sess, [
        ("claude-opus-4-7", coordinator["id"]),
        ("claude-opus-4-7", code_reviewer["id"]),
        ("claude-sonnet-4-6", simplify["id"]),
        ("claude-opus-4-7", security["id"]),
        ("claude-sonnet-4-6", git_history["id"]),
    ], fallback_usage=final.get("usage") or {})

    result = _make_result(
        variant="multiagent_production_2turn",
        session_ids=[sess],
        agent_ids=[coordinator["id"], code_reviewer["id"], simplify["id"],
                   security["id"], git_history["id"]],
        store_ids=[], beta=beta,
        usage=final.get("usage") or {},
        model="opus-coord-prod-specialists-2turn",
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample[:2500],
    )
    result["cost_usd"] = round(accurate_cost, 4)
    return result


def run_multiagent_tiered_v2() -> dict:
    """v2 of the tiered hypothesis: Sonnet coordinator (not Opus) +
    terser system prompt that mandates split-and-merge in 2 turns total.

    What v1 (Opus, 8+ turns) showed: the cost driver was the coordinator
    replaying its 30k context across 8 sequential delegations. v2 fixes
    both axes — cheaper coordinator and forced 2-turn protocol.

    Same 3 Haiku sub-agents (bugs, security, style)."""
    beta = RP_BETA
    env_id = find_or_create_environment(beta)

    bugs = create_agent(
        beta, f"{TEST_PREFIX}-mt2-bugs",
        "You are a bug detective. The user gives you specific code lines "
        "+ what to check. Output findings only — no preambles. Format: "
        "file:line | severity | description.",
        model="claude-haiku-4-5",
    )
    security = create_agent(
        beta, f"{TEST_PREFIX}-mt2-security",
        "You are a security auditor. The user gives you code + checks to "
        "apply. Output findings only — no preambles. Format: file:line | "
        "severity | description.",
        model="claude-haiku-4-5",
    )
    style = create_agent(
        beta, f"{TEST_PREFIX}-mt2-style",
        "You are a code-style reviewer. The user gives you code + style "
        "rules. Output findings only — no preambles. Format: file:line | "
        "severity | description.",
        model="claude-haiku-4-5",
    )

    # The system prompt mandates the 2-turn protocol explicitly. Each
    # rule is its own line so the coordinator can't elide one in
    # paraphrasing. This is the difference between $0.32 coordinator
    # cost and (hopefully) <$0.10.
    v2_coordinator_system = (
        "You are a code-review coordinator. STRICT PROTOCOL — only 2 turns:\n"
        "\n"
        "TURN 1 (your first response): In ONE message, issue all 3 sub-agent "
        "delegations in parallel using the agent_toolset. NO narration, NO "
        "explanation, NO 'I will now delegate' commentary. Just the 3 tool "
        "calls back-to-back. Each delegation includes only the relevant code "
        "slice for that specialist (bugs gets logic-prone lines, security "
        "gets auth/SQL/data-flow lines, style gets formatting). Sub-agents "
        "must NOT receive the full diff — only their slice.\n"
        "\n"
        "TURN 2 (after all 3 return): Output the merged findings list. NO "
        "process narration ('Two down, one to go'). NO restatement of what "
        "each agent found. Just a single de-duplicated findings list grouped "
        "by severity (Blockers / Medium / Low / Nit).\n"
        "\n"
        "Total: 2 of YOUR turns. No more."
    )

    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-mt2-coordinator", v2_coordinator_system,
        callable_agents=[
            {"type": "agent", "id": bugs["id"], "version": bugs["version"]},
            {"type": "agent", "id": security["id"], "version": security["version"]},
            {"type": "agent", "id": style["id"], "version": style["version"]},
        ],
        model="claude-sonnet-4-6",  # Sonnet coordinator, not Opus
    )

    sess = create_session(beta, coordinator["id"], env_id)
    t0 = time.monotonic()
    send_user_message(beta, sess, baseline_user_message_for_parallel())
    final = wait_for_idle(beta, sess)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=2500)

    accurate_cost = _mixed_model_cost(beta, sess, [
        ("claude-sonnet-4-6", coordinator["id"]),
        ("claude-haiku-4-5", bugs["id"]),
        ("claude-haiku-4-5", security["id"]),
        ("claude-haiku-4-5", style["id"]),
    ], fallback_usage=final.get("usage") or {})

    result = _make_result(
        variant="multiagent_tiered_v2",
        session_ids=[sess],
        agent_ids=[coordinator["id"], bugs["id"], security["id"], style["id"]],
        store_ids=[],
        beta=beta,
        usage=final.get("usage") or {},
        model="mixed-sonnet-haiku",
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample,
    )
    result["cost_usd"] = round(accurate_cost, 4)
    return result


def _mixed_model_cost(beta: str, session_id: str,
                      agents_by_model: list[tuple[str, str]],
                      fallback_usage: dict) -> float:
    """Compute cost when the multi-agent session has multiple models.
    Walks the threads endpoint and tallies usage per thread (each thread
    runs one specific agent → one specific model). If thread usage isn't
    available, falls back to summing session usage at the COORDINATOR's
    model price (overestimate, but conservative)."""
    h = headers(beta)
    r = req.get(f"{API_BASE}/sessions/{session_id}/threads", headers=h)
    if not r.ok:
        # Couldn't get threads — overestimate using coordinator (most expensive) model
        coord_model = agents_by_model[0][0]
        return cost_of(fallback_usage, coord_model)

    agent_to_model = dict((aid, model) for (model, aid) in agents_by_model)
    total = 0.0
    threads = r.json().get("data", [])
    for t in threads:
        agent_id = (t.get("agent") or {}).get("id") or t.get("agent_id")
        model = agent_to_model.get(agent_id, agents_by_model[0][0])
        thread_usage = t.get("usage") or {}
        total += cost_of(thread_usage, model)
    if total == 0:
        # Threads endpoint returned but had no usage — fall back
        coord_model = agents_by_model[0][0]
        total = cost_of(fallback_usage, coord_model)
    return total


def run_all() -> dict:
    """Multiagent + memory + outcomes — full preview-feature stack."""
    beta = RP_BETA
    env_id = find_or_create_environment(beta)
    store_id = make_wiki_memory_store(beta, "all-wiki")

    reviewer = create_agent(beta, f"{TEST_PREFIX}-all-reviewer", REVIEWER_SYSTEM)
    verifier = create_agent(beta, f"{TEST_PREFIX}-all-verifier", VERIFIER_SYSTEM)
    coordinator = create_agent(
        beta, f"{TEST_PREFIX}-all-coordinator", COORDINATOR_SYSTEM,
        callable_agents=[
            {"type": "agent", "id": reviewer["id"], "version": reviewer["version"]},
            {"type": "agent", "id": verifier["id"], "version": verifier["version"]},
        ],
    )

    sess = create_session(
        beta, coordinator["id"], env_id,
        resources=[{
            "type": "memory_store", "memory_store_id": store_id,
            "access": "read_only",
            "instructions": "Wiki context. Read /mnt/memory/wiki/ files when needed.",
        }],
    )
    t0 = time.monotonic()
    description = (
        coordinator_user_message_memory_mode()
        + "\n\nProduce a verified findings list satisfying the rubric below."
    )
    send_define_outcome(beta, sess, description, REVIEW_RUBRIC, max_iterations=3)
    final = wait_for_idle(beta, sess)
    wall = time.monotonic() - t0
    output_sample = fetch_session_output(beta, sess, max_chars=2500)

    return _make_result(
        variant="all",
        session_ids=[sess],
        agent_ids=[coordinator["id"], reviewer["id"], verifier["id"]],
        store_ids=[store_id],
        beta=beta,
        usage=final.get("usage") or {},
        model=DEFAULT_MODEL,
        active_seconds=(final.get("stats") or {}).get("active_seconds", 0),
        wall_seconds=wall,
        output_sample=output_sample,
    )


def _make_result(*, variant: str, session_ids: list[str], agent_ids: list[str],
                 store_ids: list[str], beta: str, usage: dict, model: str,
                 active_seconds: float, wall_seconds: float,
                 output_sample: str) -> dict:
    cc = usage.get("cache_creation") or {}
    return {
        "variant": variant,
        "session_ids": session_ids,
        "agent_ids": agent_ids,
        "store_ids": store_ids,
        "beta": beta,
        "model": model,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_create_5m_tokens": cc.get("ephemeral_5m_input_tokens", 0),
        "cache_create_1h_tokens": cc.get("ephemeral_1h_input_tokens", 0),
        "active_seconds": round(active_seconds, 1),
        "wall_seconds": round(wall_seconds, 1),
        "cost_usd": round(cost_of(usage, model), 4),
        "output_sample": output_sample,
    }


VARIANTS = {
    "split": run_split,
    "split_memory": run_split_memory,
    "multiagent": run_multiagent,
    "multiagent_memory": run_multiagent_memory,
    "multiagent_parallel": run_multiagent_parallel,
    "multiagent_tiered": run_multiagent_tiered,
    "multiagent_tiered_v2": run_multiagent_tiered_v2,
    "production_clone": run_production_clone,
    "tiered_v2_real": run_tiered_v2_real,
    "tiered_v2_real_prodprompts": run_tiered_v2_real_prodprompts,
    "multiagent_production": run_multiagent_production,
    "parallel_sessions_haiku": run_parallel_sessions_haiku,
    "multiagent_production_2turn": run_multiagent_production_2turn,
    "multiagent_codex_full": run_multiagent_codex_full,
    "solo": run_solo,
    "solo_memory": run_solo_memory,
    "multiagent_outcomes": run_multiagent_outcomes,
    "all": run_all,
}


def append_result(result: dict) -> None:
    with open(RESULTS, "a") as f:
        f.write(json.dumps(result) + "\n")


def cmd_report() -> None:
    if not RESULTS.is_file():
        print(f"No results yet at {RESULTS}. Run a variant first."); return
    rows = [json.loads(line) for line in RESULTS.read_text().splitlines() if line.strip()]
    if not rows:
        print("Empty results file."); return

    # Most recent run of each variant
    by_variant = {}
    for r in rows:
        by_variant[r["variant"]] = r

    print(f"\n{'variant':<20} {'cost':>8} {'in':>6} {'out':>7} {'cache_rd':>10} {'cc_5m':>9} {'wall':>6} {'compute':>8}")
    print("-" * 100)
    order = ["split", "split_memory", "solo", "solo_memory", "multiagent",
             "multiagent_memory", "multiagent_parallel", "multiagent_tiered",
             "multiagent_tiered_v2", "multiagent_outcomes", "all",
             "production_clone", "tiered_v2_real",
             "tiered_v2_real_prodprompts", "multiagent_production",
             "parallel_sessions_haiku", "multiagent_production_2turn"]
    for name in order:
        if name not in by_variant:
            continue
        r = by_variant[name]
        print(f"{name:<20} ${r['cost_usd']:>6.3f} "
              f"{r['input_tokens']:>6,} {r['output_tokens']:>7,} "
              f"{r['cache_read_tokens']:>10,} {r['cache_create_5m_tokens']:>9,} "
              f"{r['wall_seconds']:>5.0f}s {r['active_seconds']:>6.0f}s")

    print("\nDirect comparisons (each does the same review+verify work):")
    pairs = [
        ("split", "multiagent",          "multiagent feature impact (no memory)"),
        ("split_memory", "multiagent_memory", "multiagent feature impact (with memory)"),
        ("split", "split_memory",        "memory feature impact (split arch)"),
        ("multiagent", "multiagent_memory", "memory feature impact (multiagent arch)"),
        ("split", "solo",                "consolidation impact (1 agent vs 2)"),
        ("multiagent", "multiagent_outcomes", "outcomes feature impact"),
        ("split", "all",                  "full stack vs current arch"),
    ]
    for a, b, label in pairs:
        if a in by_variant and b in by_variant:
            ca = by_variant[a]["cost_usd"]
            cb = by_variant[b]["cost_usd"]
            delta = cb - ca
            pct = (delta / ca) * 100 if ca else 0
            print(f"  {a:<22} → {b:<22}  ${ca:>6.3f} → ${cb:>6.3f}  ({delta:+.3f} = {pct:+.1f}%)  {label}")


def cmd_cleanup() -> None:
    """Archive every test agent + memory_store. Sessions auto-archive."""
    for beta in (GA_BETA, RP_BETA):
        try:
            r = req.get(f"{API_BASE}/agents?limit=100", headers=headers(beta))
            for a in r.json().get("data", []):
                if not a.get("archived_at") and TEST_PREFIX in a.get("name", ""):
                    archive_agent(beta, a["id"])
                    print(f"  archived agent {a['name']} ({a['id']})")
        except Exception as e:
            print(f"  agent cleanup error on {beta}: {e}", file=sys.stderr)
        try:
            r = req.get(f"{API_BASE}/memory_stores?limit=100", headers=headers(beta))
            for s in r.json().get("data", []):
                if not s.get("archived_at") and TEST_PREFIX in s.get("name", ""):
                    archive_memory_store(beta, s["id"])
                    print(f"  archived store {s['name']} ({s['id']})")
        except Exception as e:
            print(f"  store cleanup error on {beta}: {e}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description="Cost-test harness for managed-agents preview features")
    p.add_argument("--variant", choices=list(VARIANTS.keys()), help="Which variant to run")
    p.add_argument("--report", action="store_true", help="Print comparison table from results.jsonl")
    p.add_argument("--cleanup", action="store_true", help="Archive test agents and memory stores")
    args = p.parse_args()

    if args.report:
        cmd_report(); return
    if args.cleanup:
        cmd_cleanup(); return
    if not args.variant:
        p.error("--variant is required (or pass --report / --cleanup)")

    print(f"Running variant: {args.variant}")
    result = VARIANTS[args.variant]()
    append_result(result)
    print(f"\nDone. cost=${result['cost_usd']:.4f}  "
          f"in={result['input_tokens']:,}  out={result['output_tokens']:,}  "
          f"cache_rd={result['cache_read_tokens']:,}  "
          f"wall={result['wall_seconds']:.0f}s  "
          f"compute={result['active_seconds']:.0f}s")
    print(f"Results appended to {RESULTS}")
    print(f"\nOutput sample (first 2500 chars):\n{result['output_sample']}")


if __name__ == "__main__":
    main()
