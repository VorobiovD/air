#!/usr/bin/env python3
"""THE solo-reviewer prompt assembly — shared by the CLI and managed paths.

One agent applying all six review lenses + self-verifying in a single
session. The prompt is assembled from the SAME `agents/*.md` files the
specialists use (frontmatter-stripped, each under a `===== LENS: =====`
delimiter) → zero drift, no seventh prompt file to maintain.

Consumers:
- CLI: `/air:review --solo` runs this file to obtain the prompt for a
  single local subagent (`python3 lib/solo_prompt.py` prints it).
- Managed: `setup.py` imports `assemble_solo_prompt` to create the
  `air-solo-reviewer` agent (`AIR_REVIEW_MODE=solo|both`).

Same anti-drift pattern as `lib/verdict.py`: one implementation, two paths.
"""
import sys
from pathlib import Path

# Lens order is part of the prompt contract (later lenses see earlier
# framing); keep stable. This is also the canonical specialist roster —
# managed/setup.py imports it as its SUB_AGENTS list.
SUB_AGENTS = [
    "code-reviewer",
    "simplify",
    "security-auditor",
    "git-history-reviewer",
    "ui-copy-reviewer",
    "review-verifier",
]

# Default agents dir: this file lives at plugins/air/lib/, the prompts at
# plugins/air/agents/.
AGENTS_DIR = Path(__file__).parent.parent / "agents"

SOLO_PREAMBLE = (
    "You are a thorough code reviewer applying the review lenses below, then "
    "self-verifying your findings (drop false positives / below-60 confidence). "
    "You are reviewing ALONE in a single session — there is no separate verifier "
    "pass, so the verifier lens applies to your OWN findings in real time. Output "
    "exactly the `## Code Review` format the lenses describe, including the "
    "`Reviewed at: <head_sha>` footer.\n"
)


def _split_frontmatter(path: Path) -> tuple[dict, str]:
    """Return ({key: value} for scalar frontmatter fields, body_text).
    Empty dict if no frontmatter."""
    text = path.read_text()
    if not text.startswith("---"):
        return {}, text.strip()
    try:
        end = text.index("---", 3)
    except ValueError:
        print(f"  Warning: {path.name} has unclosed frontmatter", file=sys.stderr)
        return {}, text.strip()
    fields: dict = {}
    for line in text[3:end].split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        # Naive inline-comment strip — fine for the current scalar fields
        # (name, model, tools); use a real YAML parser if quoted values
        # with `#` become needed.
        fields[key.strip()] = value.split("#", 1)[0].strip()
    return fields, text[end + 3:].strip()


def read_prompt(path: Path) -> str:
    """Read a markdown prompt file, stripping YAML frontmatter."""
    _, body = _split_frontmatter(path)
    return body


def assemble_solo_prompt(agents_dir: Path = AGENTS_DIR) -> str:
    """Merge the six specialist prompts into one solo-reviewer system prompt."""
    parts = [SOLO_PREAMBLE]
    for name in SUB_AGENTS:
        body = read_prompt(agents_dir / f"{name}.md")
        parts.append(f"\n\n===== LENS: {name} =====\n{body}")
    return "".join(parts)


if __name__ == "__main__":
    agents_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else AGENTS_DIR
    if not agents_dir.is_dir():
        print(f"error: agents dir not found: {agents_dir}", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(assemble_solo_prompt(agents_dir))
