"""Cross-module tests for the single-source frontmatter parser (agent_md).

Locks the dedup: solo_prompt / setup / headless must all parse agents/*.md
through agent_md.split_frontmatter — no second copy of the parser — and proves
headless._persona_model stays consistent with the shared parser after the
refactor. These import setup/headless (managed deps), so they live here; the
pure parse-behavior tests are in plugins/air/lib/tests/test_agent_md.py.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "plugins" / "air" / "lib"))

import agent_md  # noqa: E402
import solo_prompt  # noqa: E402  (light — stdlib only)

AGENTS_DIR = HERE.parent / "plugins" / "air" / "agents"


# ---- single source (no second copy of the parser anywhere) ------------------
def test_solo_prompt_delegates_to_agent_md():
    # solo_prompt.read_prompt must BE agent_md.read_prompt — not a re-copied parser.
    assert solo_prompt.read_prompt is agent_md.read_prompt


def test_setup_delegates_to_agent_md():
    pytest.importorskip("requests")  # setup imports requests
    import setup  # noqa: E402
    assert setup.split_frontmatter is agent_md.split_frontmatter
    assert setup.read_prompt is agent_md.read_prompt
    assert not hasattr(setup, "_split_frontmatter")  # old private copy is gone


# ---- real agent files parse, and headless agrees with the shared parser -----
def test_all_six_agents_parse():
    for name in solo_prompt.SUB_AGENTS:
        fields, body = agent_md.split_frontmatter(AGENTS_DIR / f"{name}.md")
        assert fields.get("name"), f"{name}.md missing name in frontmatter"
        assert body, f"{name}.md has empty body"


def test_persona_model_consistent_with_shared_parser():
    pytest.importorskip("anthropic")  # headless imports the anthropic SDK
    import headless  # noqa: E402
    from setup import MODEL_ALIASES  # noqa: E402
    for name in solo_prompt.SUB_AGENTS:
        path = AGENTS_DIR / f"{name}.md"
        fields, body = agent_md.split_frontmatter(path)
        pbody, model_id, tier = headless._persona_model(f"air-{name}")
        # body + model resolution must match the shared parser exactly
        assert pbody == body
        alias = fields.get("model", "") or "sonnet"
        assert model_id == MODEL_ALIASES.get(alias, MODEL_ALIASES["sonnet"])
        assert tier in headless._TIERS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
