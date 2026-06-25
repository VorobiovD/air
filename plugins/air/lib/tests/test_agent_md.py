"""Pure parse-behavior tests for agent_md.split_frontmatter / read_prompt.

These have NO managed/ deps (agent_md is stdlib-only), so they live here with the
other lib-module suites (run by air-lib-tests.yml via `pytest tests/`). The
cross-module delegation/identity tests that import setup/headless stay in
managed/test-agent-md.py.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import agent_md  # noqa: E402


def test_normal_frontmatter(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("---\nname: x\nmodel: opus\ntools: Read, Grep\n---\nBODY here\n")
    fields, body = agent_md.split_frontmatter(p)
    assert fields == {"name": "x", "model": "opus", "tools": "Read, Grep"}
    assert body == "BODY here"


def test_no_frontmatter(tmp_path):
    p = tmp_path / "b.md"
    p.write_text("just a body, no fence\n")
    fields, body = agent_md.split_frontmatter(p)
    assert fields == {}
    assert body == "just a body, no fence"


def test_unclosed_frontmatter_warns_to_stderr(tmp_path, capsys):
    p = tmp_path / "c.md"
    p.write_text("---\nmodel: sonnet\n(no closing fence)\n")
    fields, body = agent_md.split_frontmatter(p)
    assert fields == {}                       # unparseable → empty (fail-safe)
    assert "unclosed frontmatter" in capsys.readouterr().err  # stderr, not stdout
    assert body  # body preserved, no crash


def test_inline_comment_stripped(tmp_path):
    p = tmp_path / "d.md"
    p.write_text("---\nmodel: sonnet  # temporary\n---\nbody\n")
    fields, _ = agent_md.split_frontmatter(p)
    assert fields["model"] == "sonnet"


def test_blank_and_comment_lines_skipped(tmp_path):
    p = tmp_path / "e.md"
    p.write_text("---\n\n# a yaml comment\nname: y\n---\nbody\n")
    fields, _ = agent_md.split_frontmatter(p)
    assert fields == {"name": "y"}


def test_read_prompt_strips_frontmatter(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("---\nname: z\n---\nthe prompt body\n")
    assert agent_md.read_prompt(p) == "the prompt body"
