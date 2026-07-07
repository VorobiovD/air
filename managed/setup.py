#!/usr/bin/env python3
"""
Bootstrap and sync: creates/updates air review agents + environment.

Fetches the agent list once, then creates or updates each agent — except
agents pinned via AIR_AGENT_VERSIONS, which skip prompt sync entirely and
resolve to their pinned {id, version} (see parse_agent_pins).
Called by review.py on every run.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python setup.py
"""

import json
import os
import sys
from pathlib import Path

import requests

from api import API_BASE, HEADERS, get_headers, api_error_message, list_agents

AGENTS_DIR = Path(__file__).parent.parent / "plugins" / "air" / "agents"
PROMPTS_DIR = Path(__file__).parent / "prompts"

# The solo prompt assembly and the canonical specialist roster live in the
# shared plugin lib (the CLI's `/air:review --solo` consumes the same
# assembly — one implementation, two paths, like lib/verdict.py).
_AIR_LIB_DIR = Path(__file__).resolve().parent.parent / "plugins" / "air" / "lib"
if str(_AIR_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_AIR_LIB_DIR))
from solo_prompt import SUB_AGENTS, assemble_solo_prompt  # noqa: E402,F401
from agent_md import read_prompt, split_frontmatter  # noqa: E402,F401
import env  # noqa: E402  (plugins/air/lib — tolerant env parsing)

# Agent names accepted in AIR_AGENT_VERSIONS pins (the review roster).
# air-learner is deliberately NOT pinnable — learn is wiki maintenance,
# low regression risk, and always tracks the latest prompt.
PINNABLE_AGENTS = [f"air-{n}" for n in SUB_AGENTS] + ["air-coordinator"]

# The API's named agent-toolset config vocabulary. Sub-agent delegation is
# NOT in it — it's an unnamed capability riding default_config, which is
# why coordinators default-ENABLE and explicitly disable each named tool
# outside their frontmatter allowlist. If Anthropic extends the vocabulary,
# add the new name here: an unlisted named tool gets no explicit disable
# and is silently granted to the coordinator under the enabled default.
NAMED_TOOL_VOCABULARY = (
    "bash", "edit", "glob", "grep", "read", "web_fetch", "web_search", "write",
)


def build_coordinator_tool_configs(allowed: set) -> list[dict]:
    """Explicit enable/disable for every named tool: enabled iff in the
    frontmatter allowlist. Paired with default_config {"enabled": True} so
    the unnamed delegation capability stays alive (see the coordinator
    blocks in main())."""
    return [{"name": t, "enabled": t in allowed} for t in NAMED_TOOL_VOCABULARY]


def parse_agent_pins() -> dict[str, int]:
    """Parse the AIR_AGENT_VERSIONS env var (JSON map agent-name → version).

    Empty/unset → {} (everything floats — the air repo's own posture).
    Work repos pass a blessed set, e.g.
    `{"air-code-reviewer": 12, ..., "air-coordinator": 9}`, published in
    the release notes; they bump deliberately instead of riding main.

    Malformed input fails LOUDLY (exit 1): a typo'd pin silently floating
    would defeat the entire point of pinning, so unparseable JSON, unknown
    agent names, and non-integer versions all abort the run before any
    sync or session spend happens.
    """
    raw = os.environ.get("AIR_AGENT_VERSIONS", "").strip()
    if not raw:
        return {}
    try:
        pins = json.loads(raw)
    except ValueError as e:
        print(f"Error: AIR_AGENT_VERSIONS is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(pins, dict):
        print("Error: AIR_AGENT_VERSIONS must be a JSON object (agent name → version).", file=sys.stderr)
        sys.exit(1)
    bad_keys = [k for k in pins if k not in PINNABLE_AGENTS]
    if bad_keys:
        print(
            f"Error: AIR_AGENT_VERSIONS has unknown agent name(s): {bad_keys}. "
            f"Pinnable: {PINNABLE_AGENTS}.",
            file=sys.stderr,
        )
        sys.exit(1)
    bad_vals = {k: v for k, v in pins.items() if not isinstance(v, int) or isinstance(v, bool) or v < 1}
    if bad_vals:
        print(
            f"Error: AIR_AGENT_VERSIONS versions must be positive integers, got: {bad_vals}.",
            file=sys.stderr,
        )
        sys.exit(1)
    # A pinned coordinator dispatches whatever sub-agent versions its pinned
    # revision recorded — pinning it without pinning every specialist gives a
    # roster the caller can't see or control. Specialists-without-coordinator
    # is fine (the floating coordinator's roster is rebuilt from the pinned
    # specialist versions each sync).
    if "air-coordinator" in pins:
        unpinned = [f"air-{n}" for n in SUB_AGENTS if f"air-{n}" not in pins]
        if unpinned:
            print(
                f"Error: air-coordinator is pinned but {unpinned} are not — "
                f"pin the whole blessed set from one release (a pinned "
                f"coordinator's sub-agent roster is fixed at its pinned "
                f"version; partial pins skew it silently).",
                file=sys.stderr,
            )
            sys.exit(1)
    return pins


MODEL_ALIASES = {
    "opus": "claude-opus-4-8",
    # Sonnet 5 (2026-06-30). Flipped from claude-sonnet-4-6 after a 7-PR A/B: 5 was
    # ~2x faster, more thorough (found a superset of 4.6's findings every time), and
    # correct on both gate divergences (caught a real sec blocker 4.6 missed; did NOT
    # over-gate where 4.6 did). One lever — every `model: sonnet` agent + learn follow
    # this. Rollback = revert this line. Pricing tier stays "sonnet" ($3/$15; $2/$10
    # intro through 2026-08-31), so cost telemetry is unchanged.
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}

DEFAULT_OPUS = MODEL_ALIASES["opus"]


def parse_agent_tools(path: Path) -> list[str]:
    """Extract tool names from agent frontmatter."""
    fields, _ = split_frontmatter(path)
    if "tools" not in fields:
        return ["bash", "read", "grep", "glob"]
    return [t.strip().lower() for t in fields["tools"].split(",")]


def parse_agent_model(path: Path, default: str = DEFAULT_OPUS) -> str:
    """Read `model:` from agent frontmatter, resolving aliases to API IDs."""
    fields, _ = split_frontmatter(path)
    value = fields.get("model", "")
    if not value:
        return default
    return MODEL_ALIASES.get(value, value)


def parse_agent_speed(path: Path) -> str | None:
    """Return the `speed:` frontmatter value (e.g. "fast"), or None if absent."""
    fields, _ = split_frontmatter(path)
    return fields.get("speed", "") or None


def _normalize_model_field(value) -> dict | None:
    """Return the canonical object form of a model field for comparison.

    The API accepts both scalar (`"claude-opus-4-8"`) and object form
    (`{"id": ..., "speed": ...}`) on send, and historically returns either form
    on read depending on how the agent was last synced. Normalize both shapes
    to the object form so existing-vs-new diffs compare like-for-like.
    """
    if not value:
        return None
    if isinstance(value, dict):
        return value
    return {"id": value, "speed": "standard"}


def _verify_multiagent_roster(name: str, data: dict, requested: dict | None) -> None:
    """Abort the sync if a requested multiagent roster didn't persist.

    A coordinator that lost its roster doesn't error at review time — the
    runtime simply never offers the delegation tool, and the coordinator
    improvises an unverified single-agent review that LOOKS like a normal
    success (observed 2026-06-11 on an enforcing workspace runtime). Failing the sync
    here is the only cheap place to catch it.
    """
    if requested and not data.get("multiagent"):
        print(
            f"  {name}: API accepted the sync but dropped the multiagent "
            f"roster (v{data.get('version')}) — a roster-less coordinator "
            f"silently improvises solo reviews. Aborting.",
            file=sys.stderr,
        )
        sys.exit(1)


def create_or_update_agent(
    name: str,
    system: str,
    tools: list,
    existing: dict | None,
    callable_agents: list | None = None,
    multiagent: dict | None = None,
    model: str = DEFAULT_OPUS,
    speed: str | None = None,
) -> dict:
    """Update if exists, create if not. Takes pre-fetched existing agent.

    On update, the `model` field is sent so model changes (e.g. from frontmatter
    tiering) propagate to existing managed deployments. If the API rejects a
    model change in-place, we retry without `model` and print a warning so the
    operator knows the agent needs manual re-creation to pick up the new model.

    When `speed` is set (e.g. "fast"), the model field is sent in object form
    `{"id": model, "speed": speed}`; otherwise scalar string. The API stores
    object form either way (with default `speed=standard` when omitted), so the
    existing-vs-new comparison normalizes both sides to object form before
    printing the diff.
    """
    model_field = {"id": model, "speed": speed} if speed else model
    sent_normalized = _normalize_model_field(model_field)
    existing_normalized = _normalize_model_field(existing.get("model") if existing else None)
    # `multiagent` only exists in the GA dialect: the research-preview update
    # endpoint silently drops the roster (and its GET renders the field as
    # null even when set, hiding the loss). A roster-less coordinator still
    # "works" — it improvises an unverified solo review with no error — so
    # the wrong header here is a silent-degradation bug, not a crash.
    headers = get_headers(ga=multiagent is not None)

    if existing:
        body = {"model": model_field, "system": system, "tools": tools, "version": existing["version"]}
        if callable_agents:
            body["callable_agents"] = callable_agents
        if multiagent:
            body["multiagent"] = multiagent
        resp = requests.post(
            f"{API_BASE}/agents/{existing['id']}",
            headers=headers,
            json=body,
        )
        if resp.ok:
            data = resp.json()
            _verify_multiagent_roster(name, data, multiagent)
            if existing_normalized and existing_normalized != sent_normalized:
                print(f"  {name}: synced → v{data['version']} (model {existing['model']} → {sent_normalized})")
            else:
                print(f"  {name}: synced → v{data['version']}")
            return data
        # Retry without model ONLY if the primary failure mentions model
        # (API disallows in-place model changes on some endpoints).
        primary_error = api_error_message(resp)
        if existing_normalized != sent_normalized and "model" in str(primary_error).lower():
            retry_body = {k: v for k, v in body.items() if k != "model"}
            retry = requests.post(
                f"{API_BASE}/agents/{existing['id']}",
                headers=headers,
                json=retry_body,
            )
            if retry.ok:
                data = retry.json()
                _verify_multiagent_roster(name, data, multiagent)
                print(
                    f"  {name}: synced prompt → v{data['version']} "
                    f"(model pinned to {existing.get('model', '?')} — archive the agent via the Anthropic console "
                    f"or POST /agents/{existing['id']}/archive, then re-run setup.py to re-tier to {sent_normalized})"
                )
                return data
            # Double-failure: report both errors
            print(
                f"  {name}: sync failed ({resp.status_code}: {primary_error}); "
                f"retry-without-model also failed ({retry.status_code}: {api_error_message(retry)}), "
                f"using v{existing['version']}"
            )
            return existing
        print(f"  {name}: sync failed ({resp.status_code}: {primary_error}), using v{existing['version']}")
        return existing
    else:
        body = {"name": name, "model": model_field, "system": system, "tools": tools}
        if callable_agents:
            body["callable_agents"] = callable_agents
        if multiagent:
            body["multiagent"] = multiagent
        resp = requests.post(f"{API_BASE}/agents", headers=headers, json=body)
        if not resp.ok:
            print(f"  {name}: creation failed — {api_error_message(resp)}", file=sys.stderr)
            sys.exit(1)
        data = resp.json()
        _verify_multiagent_roster(name, data, multiagent)
        print(f"  {name}: created → {data['id']} (v{data['version']}, model={sent_normalized})")
        return data


def find_or_create_environment() -> str:
    """Find existing environment or create one."""
    resp = requests.get(f"{API_BASE}/environments", headers=get_headers())
    if resp.ok:
        for env in resp.json().get("data", []):
            if env["name"] == "air-review-env" and not env.get("archived_at"):
                print(f"  Environment: {env['id']}")
                return env["id"]

    resp = requests.post(
        f"{API_BASE}/environments",
        headers=get_headers(),
        json={
            "name": "air-review-env",
            "config": {
                "type": "cloud",
                "packages": {"apt": ["gh"]},
                "networking": {"type": "unrestricted"},
            },
        },
    )
    if not resp.ok:
        print(f"  Environment creation failed: {api_error_message(resp)}", file=sys.stderr)
        sys.exit(1)
    data = resp.json()
    print(f"  Environment: {data['id']} (created)")
    return data["id"]


def _pinned_entry(full_name: str, pin: int, agents_by_name: dict) -> dict:
    """Resolve a pinned agent to a slim {id, version} roster entry.

    Pinning skips prompt sync entirely — the existing agent's current
    config stays untouched and sessions/rosters reference the pinned
    version. A pin on an agent that doesn't exist yet is an error (there
    is nothing to pin; float once to create it, then pin).
    """
    existing = agents_by_name.get(full_name)
    if not existing:
        print(
            f"Error: {full_name} is pinned to v{pin} but no such agent exists "
            f"in this workspace — remove the pin (float once to create), then re-pin.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"  {full_name}: pinned → v{pin} (prompt sync skipped)")
    return {"id": existing["id"], "version": pin}


def main():
    # Line-buffer piped stdout (same rationale as review.py): sync output
    # is flushed to the same CI stream as review.py (subprocess.run with
    # inherited stdout, no PIPE) — block buffering reorders it against
    # stderr and loses it on truncation.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    print("Syncing air agents...\n")
    # Surface a mistyped AIR_* knob NAME early (secret-safe — NAMES only).
    env.report_env()

    pins = parse_agent_pins()
    if pins:
        print(f"Version pins active for {len(pins)} agent(s): {sorted(pins)}\n")

    # 1. Environment
    print("[1] Environment")
    env_id = find_or_create_environment()

    # 2. Fetch all agents once (fix N+1)
    print("[2] Fetching agent list...")
    agents_by_name = list_agents()

    # 3. Specialist agents. They become `callable_agents` of the
    # coordinator below — the coordinator dispatches them as sub-agents
    # within a single session, replacing the prior client-side
    # asyncio.gather over 5 separate sessions.
    print("[3] Specialist agents")
    synced: dict[str, dict] = {}
    for name in SUB_AGENTS:
        full_name = f"air-{name}"
        if full_name in pins:
            synced[name] = _pinned_entry(full_name, pins[full_name], agents_by_name)
            continue

        prompt_file = AGENTS_DIR / f"{name}.md"
        if not prompt_file.exists():
            print(f"  air-{name}: SKIPPED — {prompt_file} not found")
            continue

        system = read_prompt(prompt_file)
        tools = parse_agent_tools(prompt_file)
        model = parse_agent_model(prompt_file)
        speed = parse_agent_speed(prompt_file)
        tool_configs = [{"name": t, "enabled": True} for t in tools]

        agent = create_or_update_agent(
            name=f"air-{name}",
            system=system,
            tools=[{
                "type": "agent_toolset_20260401",
                "default_config": {"enabled": False},
                "configs": tool_configs,
            }],
            existing=agents_by_name.get(f"air-{name}"),
            model=model,
            speed=speed,
        )
        synced[name] = agent

    # 4. Coordinator agent. Multi-agent orchestrator that calls all 5
    # specialists as sub-agents via callable_agents. Skipped if
    # coordinator.md is absent (lets older trees still sync specialists
    # without erroring).
    coordinator_file = AGENTS_DIR / "coordinator.md"
    coordinator: dict | None = None
    # Shared by steps 4 and 4b (same prompt file, same model/tools — only
    # the delegation primitive differs). Parsed once.
    coord_system = coord_model = coord_speed = None
    coord_tool_configs: list[dict] = []
    if coordinator_file.exists():
        coord_system = read_prompt(coordinator_file)
        coord_model = parse_agent_model(coordinator_file, default=MODEL_ALIASES["sonnet"])
        # Coordinator is Sonnet today (no fast-mode), but accept the
        # `speed:` field for forward-compatibility if Anthropic adds fast
        # mode to Sonnet or we re-tier the coordinator to Opus later.
        coord_speed = parse_agent_speed(coordinator_file)
        # COORDINATORS INVERT the specialists' allowlist construction.
        # Sub-agent delegation is an UNNAMED toolset capability ("create_
        # agent" — not in the configs[].name vocabulary: bash, edit, glob,
        # grep, read, web_fetch, web_search, write), so it inherits
        # default_config. The specialists' default-deny shape silently
        # disabled it; older workspace runtime builds didn't enforce
        # that, newer builds DO ("Permission to use create_agent has
        # been denied", 2026-06-11 — the coordinator then improvised an
        # unverified solo review). Construction: default-ENABLE, then
        # explicitly disable everything NOT in the frontmatter allowlist.
        # Same effective named-tool surface as before (bash/read/grep/glob
        # on, edit/write/web off), delegation rides the enabled default.
        coord_tool_configs = build_coordinator_tool_configs(
            set(parse_agent_tools(coordinator_file))
        )
    if "air-coordinator" in pins:
        # Pinned coordinator: its callable_agents roster is whatever that
        # version recorded at sync time — pin a coordinator version whose
        # roster matches the pinned specialists (i.e. pin the whole blessed
        # set from one release, not a mix).
        print("[4] Coordinator agent (multi-agent dispatcher)")
        coordinator = _pinned_entry("air-coordinator", pins["air-coordinator"], agents_by_name)
    elif coordinator_file.exists():
        print("[4] Coordinator agent (multi-agent dispatcher)")
        if len(synced) < len(SUB_AGENTS):
            print(
                f"  air-coordinator: SKIPPED — only {len(synced)}/{len(SUB_AGENTS)} "
                f"sub-agents synced; coordinator needs all 5 to declare callable_agents",
                file=sys.stderr,
            )
        else:
            callable_agents = [
                {"type": "agent", "id": synced[n]["id"], "version": synced[n]["version"]}
                for n in SUB_AGENTS
            ]
            coordinator = create_or_update_agent(
                name="air-coordinator",
                system=coord_system,
                tools=[{
                    "type": "agent_toolset_20260401",
                    # Default-ENABLE so unnamed delegation works; named
                    # tools outside the frontmatter allowlist are disabled
                    # explicitly in coord_tool_configs (see above).
                    "default_config": {"enabled": True},
                    "configs": coord_tool_configs,
                }],
                existing=agents_by_name.get("air-coordinator"),
                callable_agents=callable_agents,
                model=coord_model,
                speed=coord_speed,
            )

    # 4b. Multiagent-roster coordinator — the PR6′ migration path, created
    # ONLY when the run opts in via AIR_MULTIAGENT=1 AND the architecture
    # actually uses a coordinator (solo mode never does — without the mode
    # guard, a transient create failure here could sys.exit a solo review
    # that has no use for this agent). Same posture as the solo agent: a
    # default run never touches it. Same prompt/tools/model as
    # air-coordinator; only the delegation primitive differs — a GA
    # `multiagent` roster whose /workspace is SHARED across threads
    # (probes 1-4, 2026-06-10/11), which is what enables
    # MODE: WORKSPACE-HANDOFF. Deliberately NOT pinnable (pin the
    # specialists + air-coordinator; the MA agent is rebuilt each sync).
    ma_mode = (
        env.env_bool("AIR_MULTIAGENT", False)
        and os.environ.get("AIR_REVIEW_MODE", "full") != "solo"
    )
    if not ma_mode:
        print("[4b] Multiagent coordinator — skipped (AIR_MULTIAGENT unset or solo mode)")
    elif not coordinator_file.exists() or len(synced) < len(SUB_AGENTS):
        print(
            "[4b] Multiagent coordinator — SKIPPED (needs coordinator.md + "
            "all specialists synced)",
            file=sys.stderr,
        )
    else:
        print("[4b] Multiagent coordinator (GA roster, shared workspace)")
        create_or_update_agent(
            name="air-coordinator-ma",
            system=coord_system,
            tools=[{
                "type": "agent_toolset_20260401",
                # Default-ENABLE — same rationale as step 4 (unnamed
                # delegation rides the default; allowlist via disables).
                "default_config": {"enabled": True},
                "configs": coord_tool_configs,
            }],
            existing=agents_by_name.get("air-coordinator-ma"),
            multiagent={
                "type": "coordinator",
                "agents": [
                    {"type": "agent", "id": synced[n]["id"], "version": synced[n]["version"]}
                    for n in SUB_AGENTS
                ],
            },
            model=coord_model,
            speed=coord_speed,
        )

        # 4c. Opt-in cheaper/faster MA coordinator tier (AIR_MA_COORDINATOR_MODEL).
        # The MA coordinator only delegates + relays the verifier's review
        # VERBATIM — it does NOT synthesize (coordinator.md TURN 3 Part A) — so a
        # cheaper model is relay-safe (validated 2026-06-19: 0 findings dropped,
        # gate verdict unchanged) and cuts the idle-wake latency. Built as a
        # SEPARATE agent (air-coordinator-ma-<alias>) so a per-repo opt-in never
        # mutates the shared Sonnet air-coordinator-ma other callers route to.
        ma_tier = os.environ.get("AIR_MA_COORDINATOR_MODEL", "").strip().lower()
        if ma_tier in MODEL_ALIASES and MODEL_ALIASES[ma_tier] != coord_model:
            tier_name = f"air-coordinator-ma-{ma_tier}"
            print(f"[4c] Tiered MA coordinator: {tier_name} (model={ma_tier})")
            create_or_update_agent(
                name=tier_name,
                system=coord_system,
                tools=[{
                    "type": "agent_toolset_20260401",
                    "default_config": {"enabled": True},
                    "configs": coord_tool_configs,
                }],
                existing=agents_by_name.get(tier_name),
                multiagent={
                    "type": "coordinator",
                    "agents": [
                        {"type": "agent", "id": synced[n]["id"], "version": synced[n]["version"]}
                        for n in SUB_AGENTS
                    ],
                },
                model=MODEL_ALIASES[ma_tier],
            )

    # 5. Solo reviewer agent. One agent applying all 6 lenses + self-verify in
    # a single session — the opt-in AIR_REVIEW_MODE=solo path in review.py.
    # Its prompt is assembled from the same specialist .md files (zero drift),
    # so it is deliberately NOT in PINNABLE_AGENTS (pin the specialists).
    #
    # Synced ONLY when the run actually uses it (AIR_REVIEW_MODE=solo;
    # review.py passes the resolved mode through sync_agents). A full-only run
    # never creates it — so an at-capacity workspace or a transient create
    # failure can't abort a default review that never touches the solo agent.
    solo_mode = os.environ.get("AIR_REVIEW_MODE", "full") == "solo"
    solo: dict | None = None
    if not solo_mode:
        print("[5] Solo reviewer agent — skipped (review_mode=full)")
    elif (missing_md := [n for n in SUB_AGENTS if not (AGENTS_DIR / f"{n}.md").exists()]):
        print(f"[5] Solo reviewer agent — SKIPPED (missing prompt files: {missing_md})", file=sys.stderr)
    else:
        print("[5] Solo reviewer agent (single-session, all lenses)")
        solo = create_or_update_agent(
            name="air-solo-reviewer",
            system=assemble_solo_prompt(),
            tools=[{
                "type": "agent_toolset_20260401",
                "default_config": {"enabled": False},
                "configs": [{"name": t, "enabled": True} for t in ["bash", "read", "grep", "glob"]],
            }],
            existing=agents_by_name.get("air-solo-reviewer"),
            model=DEFAULT_OPUS,
            speed="fast",
        )

    coord_status = "+ coordinator" if coordinator else "(coordinator absent)"
    solo_status = "+ solo" if solo else ("(solo not needed)" if not solo_mode else "(solo absent)")
    print(f"\nDone. {len(synced)} specialist agents synced {coord_status} {solo_status}.")


if __name__ == "__main__":
    main()
