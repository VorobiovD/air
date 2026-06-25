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
import sys
from pathlib import Path


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
