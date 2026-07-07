"""Tests for the pre-commit drift hook's security posture (HOOK-1 / S2).

The hook lives at plugins/air/hooks/pre-commit-drift.py (hyphenated → not
importable by name), so we load it by path. Focus: the two security-critical
additions — a secret-free script environment and the out-of-band trust gate on
a repo-provided .air-checks.sh — plus an end-to-end proof that an untrusted but
executable script is NOT run.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK_PATH = Path(__file__).resolve().parents[2] / "hooks" / "pre-commit-drift.py"


def _load():
    spec = importlib.util.spec_from_file_location("air_pre_commit_drift", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load()


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


# --- secret-free environment (S2) ---

def test_build_script_env_excludes_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    for secret in ("ANTHROPIC_API_KEY", "AIR_BOT_TOKEN", "DIMA_PAT",
                   "OPENAI_API_KEY", "AIR_NEW_API_KEY"):
        monkeypatch.setenv(secret, "SECRET-VALUE")
    env = hook._build_script_env()
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["AIR_PLUGIN_ROOT"]  # scripts still get this
    for secret in ("ANTHROPIC_API_KEY", "AIR_BOT_TOKEN", "DIMA_PAT",
                   "OPENAI_API_KEY", "AIR_NEW_API_KEY"):
        assert secret not in env, f"{secret} leaked into the script env"


def test_build_script_env_forwards_locale(monkeypatch):
    monkeypatch.setenv("LC_NUMERIC", "en_US.UTF-8")
    assert hook._build_script_env().get("LC_NUMERIC") == "en_US.UTF-8"


# --- trust gate (HOOK-1) ---

def test_custom_trusted_via_env_allowlist(tmp_path, monkeypatch):
    repo = tmp_path / "repo"; repo.mkdir()
    other = tmp_path / "other"; other.mkdir()
    monkeypatch.setenv("AIR_TRUSTED_CHECKS", os.pathsep.join([str(repo)]))
    assert hook._custom_trusted(str(repo)) is True
    assert hook._custom_trusted(str(other)) is False  # allowlist is exact


def test_custom_trusted_via_git_dir_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("AIR_TRUSTED_CHECKS", raising=False)
    repo = tmp_path / "repo"; repo.mkdir()
    _git_init(repo)
    assert hook._custom_trusted(str(repo)) is False  # no marker yet
    git_dir = subprocess.check_output(
        ["git", "rev-parse", "--absolute-git-dir"], cwd=repo).decode().strip()
    (Path(git_dir) / "air-checks.trusted").write_text("")
    assert hook._custom_trusted(str(repo)) is True


def test_custom_trusted_false_without_any_signal(tmp_path, monkeypatch):
    monkeypatch.delenv("AIR_TRUSTED_CHECKS", raising=False)
    repo = tmp_path / "repo"; repo.mkdir()
    _git_init(repo)
    assert hook._custom_trusted(str(repo)) is False


# --- end-to-end: the money test ---

def _run_hook(repo, drop_trust=True):
    env = os.environ.copy()
    if drop_trust:
        env.pop("AIR_TRUSTED_CHECKS", None)
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}})
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)], input=payload, text=True,
        cwd=str(repo), env=env, capture_output=True)


def test_untrusted_executable_script_is_not_executed(tmp_path):
    """A hostile repo's +x .air-checks.sh must NOT run without an out-of-band
    trust signal — this is the exploit HOOK-1 closes."""
    repo = tmp_path / "repo"; repo.mkdir()
    _git_init(repo)
    canary = repo / "PWNED"
    script = repo / ".air-checks.sh"
    script.write_text(f"#!/bin/bash\ntouch '{canary}'\nexit 0\n")
    script.chmod(0o755)
    r = _run_hook(repo)
    assert not canary.exists(), "untrusted .air-checks.sh executed — trust gate failed"
    # commit is still allowed (built-ins run + pass on a bare repo)
    assert r.returncode == 0
    assert "not trusted" in (r.stderr or "").lower()


def test_trusted_executable_script_runs(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    _git_init(repo)
    ran = repo / "RAN"
    script = repo / ".air-checks.sh"
    script.write_text(f"#!/bin/bash\ntouch '{ran}'\nexit 0\n")
    script.chmod(0o755)
    git_dir = subprocess.check_output(
        ["git", "rev-parse", "--absolute-git-dir"], cwd=repo).decode().strip()
    (Path(git_dir) / "air-checks.trusted").write_text("")
    r = _run_hook(repo)
    assert ran.exists(), "trusted .air-checks.sh did not run"
    assert r.returncode == 0
