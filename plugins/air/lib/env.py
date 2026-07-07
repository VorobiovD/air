#!/usr/bin/env python3
"""Tolerant AIR_* environment-variable parsing + a startup drift report.

Closes two config failure modes the fleet has hit:

  1. A typo'd VALUE crashing the whole job at IMPORT. Several tuning knobs were
     read with a bare `int(os.environ.get(...))` at module load, so a single
     mistyped org variable (e.g. `AIR_DIFF_MAX_BYTES=500k`) raised `ValueError`
     before `main()` ever ran — taking down every review/setup on that repo.
     `env_int` / `env_float` warn once and fall back to the default instead.

  2. A typo'd NAME silently no-op'ing. `AIR_NO_APROVE=1` (missing a P) does
     nothing today with zero signal — the operator thinks advisory mode is on.
     `report_env()` scans the environment for `AIR_*` keys that aren't in the
     known registry and warns, so a mistyped knob is visible in the job log.

`env_bool` also unifies the four hand-rolled truthiness grammars into one, with
token sets chosen to be BYTE-IDENTICAL to the existing `not in ("0","false",
"no")` (default-on kill switch) and `in ("1","true","yes")` (default-off
opt-in) idioms — so routing an existing site through it never changes the gate.

stdlib-only (matches the `plugins/air/lib` no-dependency rule) and imports
nothing from this package, so `verdict.py` / `meta.py` may import it freely
with no cycle. Reports NAMES only — never a variable's VALUE — so it is safe to
run in an environment carrying secrets (`AIR_BOT_TOKEN`, `ANTHROPIC_API_KEY`).
"""

import os
import sys

# Canonical boolean token sets — EXACTLY the tokens already used across the
# codebase. Keep these frozen: env_bool is byte-identical to the old idioms
# only while these match ("0"/"false"/"no" and "1"/"true"/"yes").
_TRUE = ("1", "true", "yes")
_FALSE = ("0", "false", "no")


def _warn(msg: str) -> None:
    print(f"  [env] {msg}", file=sys.stderr)


def _clip(raw: str, n: int = 40) -> str:
    """repr of a value for a warning, length-capped. The numeric/bool knobs are
    definitionally non-secret (timeouts, counts, byte caps, kill switches), but
    cap the echo anyway so a stray long value can't flood the log."""
    r = repr(raw)
    return r if len(r) <= n else r[:n] + "…'"


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Parse an integer knob, warning + falling back to `default` on a
    non-integer value (never raises). Empty/unset → default. `minimum` clamps
    the result (used for counts that must stay >= 1)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        val = default
    else:
        try:
            val = int(raw.strip())
        except ValueError:
            _warn(f"{name}={_clip(raw)} is not an integer — using default {default}")
            val = default
    if minimum is not None and val < minimum:
        return minimum
    return val


def env_float(name: str, default: float) -> float:
    """Parse a float knob, warning + falling back to `default` on a non-number
    value (never raises). Empty/unset → default."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        _warn(f"{name}={_clip(raw)} is not a number — using default {default}")
        return default


def env_bool(name: str, default: bool) -> bool:
    """Parse a kill-switch / feature-flag.

    `default=True`  → a kill switch: enabled unless the value is explicitly
                      falsy ("0"/"false"/"no").
    `default=False` → an opt-in: disabled unless the value is explicitly truthy
                      ("1"/"true"/"yes").

    Byte-identical to the existing `not in _FALSE` (default-on) and `in _TRUE`
    (default-off) idioms for every recognized token; an UNRECOGNIZED value warns
    and falls back to `default` (the old idioms also fell back to default on an
    unrecognized value — just silently). Whitespace + case are normalized, which
    additionally repairs the two sites that used a case-sensitive `in ("1",
    "true")` and so silently ignored `yes`/`TRUE` (the M2 no-op)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    _warn(f"{name}={_clip(raw)} is not a recognized boolean "
          f"(1/true/yes | 0/false/no) — using default {default}")
    return default


# Every AIR_* variable the code legitimately reads (tuning knobs, feature
# flags, infra/secret handles, and internal signals). report_env() warns on any
# AIR_* key set in the environment that is NOT here and NOT under a known
# dynamic prefix — surfacing a mistyped knob name that would otherwise no-op.
KNOWN_AIR_VARS = frozenset({
    # --- tuning knobs (int/float) ---
    "AIR_DIFF_MAX_BYTES", "AIR_HEADLESS_DIFF_CAP", "AIR_HEADLESS_MAX_TURNS",
    "AIR_HEADLESS_TTL_FILES", "AIR_HEADLESS_TTL_BYTES",
    "AIR_STREAM_RETRY_ATTEMPTS", "AIR_STREAM_RETRY_BACKOFF",
    "AIR_LEARN_TIMEOUT_S", "AIR_LEARN_PARALLELISM", "AIR_LEARN_MAX_TOKENS",
    "AIR_LEARN_BATCH_POLL", "AIR_LEARN_BATCH_TIMEOUT", "AIR_LEARN_CALL_TIMEOUT",
    "AIR_LEARN_MIN_KEEP",
    # --- feature flags / kill switches (bool) ---
    "AIR_CATEGORY_FLOOR", "AIR_LEDGER_PIN", "AIR_ORIGIN_ANCHOR",
    "AIR_RELATED_PRS", "AIR_POST_VERIFIER_BODY", "AIR_WIKI_CAP",
    "AIR_HEADLESS_PATTERNS", "AIR_HEADLESS_HISTORY", "AIR_NO_APPROVE",
    "AIR_MULTIAGENT", "AIR_PROMOTE_FASTPATH", "AIR_LEARN_BATCH",
    "AIR_LEARN_CRON_LIVE",
    # --- enums / string knobs ---
    "AIR_REVIEW_MODE", "AIR_REVIEW_FORMAT", "AIR_HEADLESS_CACHE_TTL",
    "AIR_SONNET_INTRO_PRICING", "AIR_MA_COORDINATOR_MODEL", "AIR_LEARN_MODEL",
    "AIR_AGENT_VERSIONS", "AIR_EXPECTED_REVIEWER",
    # --- infra / auth handles (secret VALUES; names are not sensitive) ---
    "AIR_BOT_TOKEN", "AIR_PAT_MAP", "AIR_BOT_LOGINS", "AIR_TARGET_REPO",
    "AIR_PLUGIN_ROOT", "AIR_LIB_DIR", "AIR_TRUSTED_CHECKS",
    "AIR_NEW_API_KEY", "AIR_OLD_API_KEY",
    # --- internal signals set/read within a single run ---
    "AIR_VERDICT_SENTINEL", "AIR_COORDINATOR_WRONG_RUNTIME",
    "AIR_WIKI_PUSH_FAILED",
})

# Families whose exact key varies at runtime: AIR_CTX_0000.. (sandbox context
# blocks) and AIR_WIKI_CAP_<FILE> (per-file bloat-cap overrides).
_KNOWN_PREFIXES = ("AIR_CTX_", "AIR_WIKI_CAP_")


def report_env(log=None) -> list[str]:
    """Warn about every AIR_* key set in the environment that isn't recognized
    (a mistyped knob). NAMES only — never values. Returns the sorted unknown
    list for testing. Cheap: a single pass over os.environ, no I/O."""
    emit = log or _warn
    unknown = sorted(
        k for k in os.environ
        if k.startswith("AIR_")
        and k not in KNOWN_AIR_VARS
        and not any(k.startswith(p) for p in _KNOWN_PREFIXES)
    )
    for k in unknown:
        emit(f"warning: {k} is set but not a recognized AIR_* variable "
             f"— ignored (typo?)")
    return unknown
