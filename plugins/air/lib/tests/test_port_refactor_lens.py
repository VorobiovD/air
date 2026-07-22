"""#276: refactor/port PRs that claim "no behavior change" have silently shipped
behavioral regressions (dropped throws, swallowed-input coercions, weakened
assertions) that the review under-recalled. The port/refactor behavioral-diff
lens lives in code-reviewer.md (the correctness specialist) and — because
solo_prompt assembles code-reviewer — reaches the solo path too. Lock that reach.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent
AGENTS = LIB.parent / "agents"
sys.path.insert(0, str(LIB))
import solo_prompt  # noqa: E402

_LENS_PHRASE = "verify the behavioral diff"
_CLAIM_PHRASE = '"no behavior change"'


def test_lens_present_in_code_reviewer():
    body = (AGENTS / "code-reviewer.md").read_text()
    assert _LENS_PHRASE in body
    assert _CLAIM_PHRASE in body
    # The four motivating failure classes must each be named.
    assert "Dropped throws" in body
    assert "Swallowed-input coercions" in body or "coerces it away" in body
    assert "Weakened assertions" in body
    assert "verification target" in body  # "no behavior change" is a target, not a relaxation


def test_lens_reaches_assembled_solo_prompt():
    assembled = solo_prompt.assemble_solo_prompt()
    assert _LENS_PHRASE in assembled
    assert _CLAIM_PHRASE in assembled
