#!/usr/bin/env python3
"""THE agents/*.md frontmatter parser — one implementation, every consumer.

A plain markdown prompt file (`agents/<name>.md`) opens with a YAML
frontmatter fence carrying scalar fields (`name`, `model`, `tools`, `speed`)
followed by the prompt body. Three call sites need to read it:

- `solo_prompt.assemble_solo_prompt` — strips frontmatter, joins the 6 bodies.
- `setup.parse_agent_{tools,model,speed}` — reads the scalar fields to sync agents.
- `headless._persona_model` — reads `model:` + returns the body.

This module is the single source so the parser can't drift between them (the
same anti-drift contract as `verdict.py` / `solo_prompt.py`). If Anthropic ever
extends the frontmatter vocabulary, only this file changes.

stdlib-only, network-free. Importable from `managed/` (setup.py / headless.py
put `plugins/air/lib` on sys.path) and runnable in-place from the lib dir.
"""
import functools
import os
import sys
from pathlib import Path

# Model aliases the per-session/client override layer ACCEPTS. This layer only
# decides WHICH alias wins (env vs frontmatter); the alias→API-ID mapping stays
# in managed/setup.py's MODEL_ALIASES (this module can't import it — managed
# imports the lib, not vice versa). Kept a superset of MODEL_ALIASES' keys plus
# `fable` (CLI/subscription only — the managed API path is org-restricted, where
# it degrades to sonnet) and `inherit` (session model on the CLI; no session
# server-side → sonnet). An UNRECOGNIZED env value is ignored so a typo can
# never silently select a phantom/absent model.
_OVERRIDE_ALIASES = ("opus", "sonnet", "haiku", "fable", "inherit")


@functools.lru_cache(maxsize=None)
def split_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """Return ({key: value} for scalar frontmatter fields, body_text).

    Empty dict if there is no frontmatter. Cached per-path so each file is read
    once per run and a warning (e.g. unclosed frontmatter) fires once, not once
    per consumer. The warning goes to STDERR on purpose: `solo_prompt` writes
    the assembled prompt to STDOUT, so a stdout warning would corrupt it.
    """
    text = path.read_text()
    if not text.startswith("---"):
        return {}, text.strip()
    try:
        end = text.index("---", 3)
    except ValueError:
        print(f"  Warning: {path.name} has unclosed frontmatter", file=sys.stderr)
        return {}, text.strip()
    fields: dict[str, str] = {}
    for line in text[3:end].split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        # Strip inline YAML comments. Naive — a legitimate `#` inside a value
        # (a URL fragment, a quoted `#123`) is truncated. Acceptable for the
        # current scalar fields (name, model, tools, speed); use a real YAML
        # parser if quoted values with `#` become needed.
        fields[key.strip()] = value.split("#", 1)[0].strip()
    return fields, text[end + 3:].strip()


def read_prompt(path: Path) -> str:
    """Read a markdown prompt file, stripping YAML frontmatter."""
    _, body = split_frontmatter(path)
    return body


def _env_key(short_name: str) -> str:
    """`review-verifier` → `AIR_MODEL_REVIEW_VERIFIER` (the `air-` prefix, if any,
    is dropped first so the key matches the agent's short name)."""
    return "AIR_MODEL_" + short_name.replace("air-", "").replace("-", "_").upper()


def model_override(short_name: str) -> str:
    """Per-session/client model override from the environment, or "" if none.

    Precedence (highest first): AIR_MODEL_<AGENT> then AIR_MODEL_DEFAULT. Returns
    a recognized alias (see _OVERRIDE_ALIASES) or "". An UNRECOGNIZED value is
    ignored (with a one-line stderr warning) and the next level applies, so a
    typo can't select a phantom model. This is the ONLY place the env layer is
    read — so with no AIR_MODEL* set it returns "" and every caller behaves
    exactly as before the override layer existed (the fleet is unaffected)."""
    for var in (_env_key(short_name), "AIR_MODEL_DEFAULT"):
        val = os.environ.get(var, "").strip().lower()
        if not val:
            continue
        if val in _OVERRIDE_ALIASES:
            return val
        print(f"  Warning: {var}={val!r} is not a recognized model alias "
              f"({', '.join(_OVERRIDE_ALIASES)}) — ignoring", file=sys.stderr)
    return ""


def resolve_model_alias(short_name: str, frontmatter_model: str = "") -> str:
    """The model ALIAS an agent should run at: the env override if set, else the
    committed frontmatter value. Returns an alias string (or "" if neither is
    set — the caller then applies its own default).

    WITH NO AIR_MODEL* ENV SET this returns exactly `frontmatter_model`, so
    managed/headless resolution is byte-identical to pre-override behavior. The
    alias→API-ID mapping stays in the caller (managed MODEL_ALIASES); the CLI
    passes the alias straight to Claude Code's Task `model:` (which takes
    aliases). `inherit` means "session model" — honored natively by the CLI;
    server-side (managed/headless) there is no session, so the caller maps it to
    its default."""
    return model_override(short_name) or frontmatter_model


if __name__ == "__main__":  # CLI shim for review.md (the plugin can only call lib/)
    import argparse
    ap = argparse.ArgumentParser(description="air agent-md helper")
    ap.add_argument("--resolve-model", metavar="SHORT",
                    help="print the ENV model override for <SHORT> (empty if none set) — "
                         "the CLI passes it to the Task spawn only when non-empty")
    ap.add_argument("--agents-dir",
                    default=str(Path(__file__).resolve().parents[1] / "agents"))
    args = ap.parse_args()
    if args.resolve_model:
        # Only the ENV override is printed (not the frontmatter fallback): the CLI
        # omits `model:` when this is empty, so no-env spawns stay byte-identical.
        # `inherit` (session model) is dropped to empty — Claude Code's Task
        # `model:` takes a concrete tier, not `inherit`; to override the CLI tier
        # use a concrete alias (fable/opus/…). `inherit` stays meaningful only in
        # frontmatter (CLI-native) and server-side (managed/headless → sonnet).
        ov = model_override(args.resolve_model)
        print("" if ov == "inherit" else ov)
