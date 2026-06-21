"""Headless (messages-api) review mode — air owns the agent loop CLIENT-SIDE.

The third air execution mode (alongside CLI + managed). Instead of a managed
coordinator session, air orchestrates the review itself: read the agent personas
locally, fan out the specialists as parallel self-hosted Messages-API tool-use
loops (agent_loop.run_agent + the read-only tool_exec sandbox), run the verifier,
then feed its body through the SAME verdict/post tail as managed. No server-side
session → no between-turn scheduling stall.

v1 SCOPE: fresh full reviews + --dry-run. Re-review / promote-fastpath / both-mode
reuse the same ledger machinery and are follow-ups. Requires a local checkout at
the PR head (AIR_TARGET_REPO) — the sandbox reads it; CI's actions/checkout
provides it. Reuses verbatim: prompts.build_pr_context / build_verifier_task,
verdict.py (the gate), github_client (fetch + post). Personas + model tiers come
from plugins/air/agents/*.md frontmatter (all Sonnet today; git-history Haiku).
"""
import asyncio
import os
import sys
import time
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "plugins" / "air" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import anthropic  # noqa: E402

from github_client import (  # noqa: E402
    fetch_pr_metadata, fetch_pr_diff, fetch_bot_login,
    _post_review_comment_with_retry, submit_review_verdict, dismiss_stale_air_verdicts,
)
from prompts import build_pr_context, build_verifier_task  # noqa: E402
from verdict import should_request_changes, _extract_review_body  # noqa: E402 (managed shim)

import agent_loop  # noqa: E402  (plugins/air/lib)
from tool_exec import Sandbox  # noqa: E402

AGENTS_DIR = _LIB.parent / "agents"
MODEL_ALIASES = {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5"}
SPECIALISTS = ["air-code-reviewer", "air-simplify", "air-security-auditor", "air-git-history-reviewer"]
UI_SPECIALIST = "air-ui-copy-reviewer"
VERIFIER = "air-review-verifier"
_DIFF_CAP = int(os.environ.get("AIR_HEADLESS_DIFF_CAP", "120000"))  # chars; v1 guard
                             # (managed has apply_diff_hygiene — a follow-up). Tunable so a
                             # big-PR run can match the diff the managed coordinator saw.
_TIER = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"}


def _persona_model(agent: str) -> tuple[str, str, str]:
    """(persona_body, model_id, tier) from plugins/air/agents/<short>.md frontmatter."""
    short = agent.replace("air-", "")
    text = (AGENTS_DIR / f"{short}.md").read_text()
    body, alias = text, "sonnet"
    end = text.index("---", 3) if text.startswith("---") and "---" in text[3:] else -1
    if end != -1:
        for line in text[3:end].splitlines():
            if line.strip().startswith("model:"):
                alias = line.split(":", 1)[1].split("#", 1)[0].strip()
        body = text[end + 3:].strip()
    return body, MODEL_ALIASES.get(alias, MODEL_ALIASES["sonnet"]), _TIER.get(alias, "sonnet")


# Each agent loop turn is a full model round-trip; serializing one tool per turn
# is the dominant cost/latency driver on big PRs (the A/B's 50-86 tool-call
# specialists). This directive pushes the model to fan out independent reads —
# it does not change WHAT gets read, only that it's batched. Shared across the
# specialist + verifier tasks (NOT the personas, which managed also uses).
_BATCH_DIRECTIVE = (
    " TOOL EFFICIENCY: when you need several files or independent searches, issue them as "
    "MULTIPLE parallel tool calls in a SINGLE response — do not read one-per-turn. Serialize "
    "only when a call genuinely depends on a prior result. This materially cuts review latency."
)


def _specialist_task(agent: str) -> str:
    return (
        "Review THIS PR through your lens (your system prompt defines it). The PR Context + "
        "`<diff>` are provided above. Use your Read / Grep / Bash(git blame/log) tools to verify "
        "against the actual source at the changed lines BEFORE reporting — the diff alone is not "
        "enough context. Emit your findings in exactly the format your lens specifies. Be concise."
        + _BATCH_DIRECTIVE
    )


async def run_headless_review(args, bot_token: str) -> dict:
    api_key = os.environ["ANTHROPIC_API_KEY"]
    checkout = os.environ.get("AIR_TARGET_REPO") or os.getcwd()
    client = anthropic.Anthropic(api_key=api_key, max_retries=6)
    sandbox = Sandbox(checkout)
    floor = os.environ.get("AIR_CATEGORY_FLOOR", "1").strip().lower() not in ("0", "false", "no")

    # ---- PREP (reused helpers) -------------------------------------------
    print(f"[headless] fetching PR #{args.pr_number} on {args.repo} …")
    meta = fetch_pr_metadata(args.repo, args.pr_number, bot_token)
    head_sha = meta["head"]["sha"]
    diff = fetch_pr_diff(args.repo, args.pr_number, bot_token)
    if len(diff) > _DIFF_CAP:
        diff = diff[:_DIFF_CAP] + f"\n[air: diff truncated at {_DIFF_CAP} chars — v1 guard]\n"
    pr_context = (build_pr_context(meta, args.repo, mode="full")
                  + f"\n\n<diff>\n{diff}\n</diff>\n")

    # Per-agent turn budget scales with PR size: a big multi-file PR needs more
    # read/blame round-trips than a small one. A fixed cap that's fine for a
    # 4-file PR starves a 30+-file one mid-investigation (the agent hits the cap
    # before emitting findings; the verifier then never sees them — observed in
    # A/B testing: two specialists hit a 45-turn cap and produced nothing).
    n_files = diff.count("\ndiff --git ") + (1 if diff.startswith("diff --git ") else 0)
    turn_budget = int(os.environ.get("AIR_HEADLESS_MAX_TURNS")
                      or min(150, 45 + 3 * max(n_files, 1)))
    print(f"[headless] turn budget: {turn_budget} ({n_files} changed files)")

    # v1: the 4 core specialists. The UI/copy lens (conditional on user-facing
    # diffs) is a v1.1 dispatch follow-up — mirror review.py:_diff_touches_ui.
    in_scope = list(SPECIALISTS)

    # ---- SPECIALISTS (parallel self-hosted loops) ------------------------
    print(f"[headless] running {len(in_scope)} specialists in parallel (self-hosted loops)…")
    t0 = time.monotonic()

    def _run_specialist(agent: str):
        persona, model, tier = _persona_model(agent)
        r = agent_loop.run_agent(
            client, model=model, persona=persona, pr_context=pr_context,
            task=_specialist_task(agent), sandbox=sandbox, effort="high",
            label=agent.replace("air-", ""), max_turns=turn_budget)
        r["agent"], r["tier"] = agent, tier
        return r

    settled = await asyncio.gather(
        *[asyncio.to_thread(_run_specialist, a) for a in in_scope],
        return_exceptions=True)
    specialist_results = {}
    for agent, res in zip(in_scope, settled):
        if isinstance(res, Exception):
            print(f"  [warn] {agent} failed: {type(res).__name__}: {res} — degrading", file=sys.stderr)
            specialist_results[agent] = None
        else:
            specialist_results[agent] = res

    # ---- VERIFIER --------------------------------------------------------
    findings_block = []
    missing_blocker_lens = []
    for agent in in_scope:
        r = specialist_results.get(agent)
        short = agent.replace("air-", "")
        if r and r.get("text"):
            findings_block.append(f"===== Findings from {agent} =====\n{r['text']}")
        else:
            findings_block.append(f"===== {agent} =====\n(specialist did not complete — unavailable)")
            if agent in ("air-security-auditor", "air-code-reviewer"):
                missing_blocker_lens.append(agent)

    verifier_task = build_verifier_task("full", args.repo, head_sha, None, "")
    verifier_input = (
        "Specialist findings to verify (verify each against source per your system prompt; "
        "drop FALSE POSITIVE / below-threshold; emit [sec:<token>] tags on confirmed exposures):\n\n"
        + "\n\n".join(findings_block) + "\n\n" + verifier_task + _BATCH_DIRECTIVE)
    vpersona, vmodel, vtier = _persona_model(VERIFIER)
    print("[headless] running verifier (self-hosted loop)…")
    vres = await asyncio.to_thread(
        agent_loop.run_agent, client, **{
            "model": vmodel, "persona": vpersona, "pr_context": pr_context,
            "task": verifier_input, "sandbox": sandbox, "effort": "high", "label": "verifier",
            "max_turns": turn_budget})
    review_body_raw = vres["text"]
    wall = time.monotonic() - t0

    # ---- DETERMINISTIC TAIL (reused verbatim) ----------------------------
    review_body, extracted = _extract_review_body(review_body_raw, head_sha)
    cost = (agent_loop.usage_cost(vres["usage"], vtier)
            + sum(agent_loop.usage_cost(r["usage"], r["tier"])
                  for r in specialist_results.values() if r))
    print(f"\n[headless] complete in {wall:.1f}s  cost≈${cost:.2f}  verifier_extracted={extracted}")

    if not extracted:
        print("[headless] verifier produced no usable ## Code Review block — failing the run", file=sys.stderr)
        return {"ok": False, "reason": "no review body", "wall": wall, "cost": cost}

    rc, reason = should_request_changes(review_body, floor_exposures=floor)
    # Fail closed if a blocker-class lens didn't run (headless partial-failure policy).
    if not rc and missing_blocker_lens:
        rc, reason = True, f"blocker-class lens did not complete: {', '.join(missing_blocker_lens)}"
        print(f"  [gate] {reason} — failing closed", file=sys.stderr)
    verdict = "REQUEST_CHANGES" if rc else "APPROVE"

    if getattr(args, "dry_run", False):
        print(f"\n===== DRY RUN — verdict: {verdict} ({reason or 'clean'}) =====\n")
        print(review_body)
        return {"ok": True, "verdict": verdict, "reason": reason, "body": review_body,
                "wall": wall, "cost": cost, "dry_run": True,
                "specialists": {a: (r["tool_calls"] if r else None) for a, r in specialist_results.items()}}

    _post_review_comment_with_retry(args.repo, args.pr_number, review_body, bot_token)
    if meta.get("state") == "open":
        # commit_id pins the verdict to the SHA we reviewed (not the PR's current
        # head). Both are required args — omitting them crashed the post path
        # (only --dry-run, which returns above, was tested). fetch_bot_login is a
        # blocking requests.get, so off-thread it to keep the event loop free.
        bot_login = await asyncio.to_thread(fetch_bot_login, bot_token)
        submit_review_verdict(args.repo, args.pr_number, bot_token,
                              event=verdict, body=reason or "", commit_id=head_sha)
        # Gate-orphan dismissal needs OUR login to skip our own just-posted verdict.
        # If we can't resolve it, SKIP dismissal — calling with current_login=None
        # makes the skip-self guard falsy and dismisses the verdict we just posted,
        # silently un-gating a REQUEST_CHANGES (the dogfood-caught gate-safety bug).
        if bot_login:
            dismiss_stale_air_verdicts(args.repo, args.pr_number, bot_token, bot_login)
        else:
            print("  [warn] bot login unresolved — skipping stale-verdict dismissal "
                  "(won't risk clearing our own verdict)", file=sys.stderr)
    return {"ok": True, "verdict": verdict, "reason": reason, "wall": wall, "cost": cost}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Headless (messages-api) air review — v1 fresh full review")
    p.add_argument("repo"); p.add_argument("pr_number", type=int)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    token = os.environ["AIR_BOT_TOKEN"]
    out = asyncio.run(run_headless_review(a, token))
    sys.exit(0 if out.get("ok") else 1)
