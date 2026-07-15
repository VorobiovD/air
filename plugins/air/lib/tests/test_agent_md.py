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


# ---- AIR_MODEL_* per-session/client override layer --------------------------
# Keystone invariant: with NO AIR_MODEL* env set, resolve_model_alias returns the
# frontmatter value verbatim, so every consumer behaves exactly as before this
# layer existed (the fleet is unaffected — opt-in only).

def _clear_model_env(mp):
    for v in ("AIR_MODEL_DEFAULT", "AIR_MODEL_CODE_REVIEWER",
              "AIR_MODEL_REVIEW_VERIFIER", "AIR_MODEL_X"):
        mp.delenv(v, raising=False)


def test_no_env_is_inert(monkeypatch):
    _clear_model_env(monkeypatch)
    assert agent_md.model_override("code-reviewer") == ""
    assert agent_md.resolve_model_alias("code-reviewer", "sonnet") == "sonnet"
    assert agent_md.resolve_model_alias("code-reviewer", "haiku") == "haiku"
    assert agent_md.resolve_model_alias("code-reviewer", "") == ""


def test_global_override_beats_frontmatter(monkeypatch):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("AIR_MODEL_DEFAULT", "fable")
    assert agent_md.model_override("code-reviewer") == "fable"
    assert agent_md.resolve_model_alias("code-reviewer", "sonnet") == "fable"


def test_per_agent_beats_global(monkeypatch):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("AIR_MODEL_DEFAULT", "sonnet")
    monkeypatch.setenv("AIR_MODEL_REVIEW_VERIFIER", "fable")
    assert agent_md.model_override("review-verifier") == "fable"   # per-agent wins
    assert agent_md.model_override("code-reviewer") == "sonnet"    # falls to global
    assert agent_md.model_override("air-review-verifier") == "fable"  # air- prefix normalizes


def test_invalid_value_ignored_falls_through(monkeypatch, capsys):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("AIR_MODEL_DEFAULT", "gpt-9")   # not a recognized alias
    assert agent_md.model_override("code-reviewer") == ""            # ignored → fall through
    assert "not a recognized model alias" in capsys.readouterr().err
    monkeypatch.setenv("AIR_MODEL_CODE_REVIEWER", "opus")            # valid per-agent still wins
    assert agent_md.model_override("code-reviewer") == "opus"


def test_value_is_case_insensitive(monkeypatch):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("AIR_MODEL_DEFAULT", "FABLE")
    assert agent_md.model_override("code-reviewer") == "fable"


def test_cli_shim_end_to_end(monkeypatch):
    import os
    import subprocess
    shim = str(HERE.parent / "agent_md.py")
    base = {k: v for k, v in os.environ.items()
            if not k.startswith("AIR_MODEL")}

    def run(env_extra):
        return subprocess.run(
            [sys.executable, shim, "--resolve-model", "code-reviewer"],
            capture_output=True, text=True, env={**base, **env_extra},
        ).stdout.strip()

    assert run({}) == ""                                   # no env → empty (CLI omits model)
    assert run({"AIR_MODEL_DEFAULT": "fable"}) == "fable"  # concrete alias flows through
    assert run({"AIR_MODEL_DEFAULT": "inherit"}) == ""     # inherit not a Task value → dropped
    assert run({"AIR_MODEL_CODE_REVIEWER": "opus",
                "AIR_MODEL_DEFAULT": "sonnet"}) == "opus"  # per-agent wins
