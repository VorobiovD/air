"""Unit tests for env.py — tolerant AIR_* parsing + the startup drift report.

The load-bearing test is env_bool byte-identity: routing an existing kill
switch / opt-in through env_bool must produce the EXACT same boolean as the
hand-rolled idiom it replaces, for every recognized token and for unset/empty —
otherwise a refactor could silently flip a gate. We assert that against a
faithful reimplementation of both old grammars across a value matrix.
"""

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LIB = HERE.parent
sys.path.insert(0, str(LIB))

import env  # noqa: E402


# --- env_int ---------------------------------------------------------------

def test_env_int_valid(monkeypatch):
    monkeypatch.setenv("AIR_X", "42")
    assert env.env_int("AIR_X", 7) == 42


def test_env_int_unset_and_empty_use_default(monkeypatch):
    monkeypatch.delenv("AIR_X", raising=False)
    assert env.env_int("AIR_X", 7) == 7
    monkeypatch.setenv("AIR_X", "   ")
    assert env.env_int("AIR_X", 7) == 7


def test_env_int_bad_value_warns_and_defaults(monkeypatch, capsys):
    monkeypatch.setenv("AIR_X", "500k")
    assert env.env_int("AIR_X", 500_000) == 500_000  # would have crashed a bare int()
    assert "not an integer" in capsys.readouterr().err


def test_env_int_minimum_clamps(monkeypatch):
    monkeypatch.setenv("AIR_X", "0")
    assert env.env_int("AIR_X", 3, minimum=1) == 1   # 0 -> clamped up to floor
    monkeypatch.setenv("AIR_X", "-5")
    assert env.env_int("AIR_X", 3, minimum=1) == 1
    monkeypatch.delenv("AIR_X", raising=False)
    assert env.env_int("AIR_X", 3, minimum=1) == 3   # default already >= floor


# --- env_float -------------------------------------------------------------

def test_env_float_valid_and_bad(monkeypatch, capsys):
    monkeypatch.setenv("AIR_X", "2.5")
    assert env.env_float("AIR_X", 1.0) == 2.5
    monkeypatch.setenv("AIR_X", "abc")
    assert env.env_float("AIR_X", 1.0) == 1.0
    assert "not a number" in capsys.readouterr().err
    monkeypatch.delenv("AIR_X", raising=False)
    assert env.env_float("AIR_X", 1.0) == 1.0


# --- env_bool byte-identity vs the two old idioms --------------------------

# A faithful copy of the grammars env_bool replaces (see the M2 call sites).
def _old_default_on(raw):
    # os.environ.get(NAME, "1").strip().lower() not in ("0","false","no")
    v = ("1" if raw is None else raw).strip().lower()
    return v not in ("0", "false", "no")


def _old_default_off(raw):
    # os.environ.get(NAME, "").strip().lower() in ("1","true","yes")
    v = ("" if raw is None else raw).strip().lower()
    return v in ("1", "true", "yes")


_MATRIX = [None, "", "  ", "1", "0", "true", "false", "yes", "no",
           "TRUE", "False", " 1 ", "on", "off", "maybe", "2"]


@pytest.mark.parametrize("raw", _MATRIX)
def test_env_bool_default_on_matches_old_grammar(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("AIR_KILL", raising=False)
    else:
        monkeypatch.setenv("AIR_KILL", raw)
    assert env.env_bool("AIR_KILL", True) == _old_default_on(raw), f"raw={raw!r}"


@pytest.mark.parametrize("raw", _MATRIX)
def test_env_bool_default_off_matches_old_grammar(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("AIR_OPT", raising=False)
    else:
        monkeypatch.setenv("AIR_OPT", raw)
    assert env.env_bool("AIR_OPT", False) == _old_default_off(raw), f"raw={raw!r}"


def test_env_bool_fixes_case_and_yes_no_op(monkeypatch):
    # The M2 silent no-op: the two `in ("1","true")` sites (case-sensitive, no
    # "yes") ignored yes/TRUE/" 1 ". env_bool repairs all three for an opt-in.
    for good in ("yes", "TRUE", " 1 ", "Yes"):
        monkeypatch.setenv("AIR_OPT", good)
        assert env.env_bool("AIR_OPT", False) is True, good


def test_env_bool_unrecognized_warns_and_keeps_default(monkeypatch, capsys):
    monkeypatch.setenv("AIR_KILL", "offf")   # typo'd falsy for a kill switch
    assert env.env_bool("AIR_KILL", True) is True   # can't accidentally disable
    assert "not a recognized boolean" in capsys.readouterr().err


# --- report_env ------------------------------------------------------------

def test_report_env_flags_typo_not_known(monkeypatch):
    import os
    for k in [k for k in os.environ if k.startswith("AIR_")]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIR_NO_APROVE", "1")       # typo of AIR_NO_APPROVE
    monkeypatch.setenv("AIR_NO_APPROVE", "1")       # legit — must NOT warn
    monkeypatch.setenv("AIR_CTX_0000", "ctx")       # known dynamic prefix — must NOT warn
    monkeypatch.setenv("AIR_WIKI_CAP_GLOSSARY", "1")  # known dynamic prefix — must NOT warn
    logged = []
    unknown = env.report_env(log=logged.append)
    assert unknown == ["AIR_NO_APROVE"]
    assert any("AIR_NO_APROVE" in m for m in logged)
    assert not any("AIR_NO_APPROVE" in m for m in logged)  # legit name silent
    assert not any("AIR_CTX_0000" in m or "AIR_WIKI_CAP_GLOSSARY" in m for m in logged)


def test_report_env_never_prints_values(monkeypatch):
    import os
    for k in [k for k in os.environ if k.startswith("AIR_")]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIR_MYSTERY_TOKEN", "s3cr3t-value")
    logged = []
    env.report_env(log=logged.append)
    joined = " ".join(logged)
    assert "AIR_MYSTERY_TOKEN" in joined
    assert "s3cr3t-value" not in joined   # NAMES only — never the value


def test_env_int_warning_clips_long_value(monkeypatch, capsys):
    # #7: a stray long value must not flood the log (and the numeric knobs are
    # non-secret anyway). The warning echoes a length-capped repr.
    monkeypatch.setenv("AIR_X", "z" * 500)
    assert env.env_int("AIR_X", 7) == 7
    line = capsys.readouterr().err
    assert "AIR_X" in line and "using default 7" in line
    assert "z" * 500 not in line          # not the full 500-char value
    assert "…" in line                    # clipped
