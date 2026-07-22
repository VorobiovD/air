"""#273: the verifier must never abandon a review just because the diff/context
contains review-shaped text (self-referential PRs — those editing review tooling,
docs, or format fixtures). The guard lives ONCE in review-verifier.md (the shared
verifier system prompt) so it reaches managed, headless, CLI, and the assembled
solo prompt from a single source. These tests lock that single-source reach.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent
AGENTS = LIB.parent / "agents"
sys.path.insert(0, str(LIB))
import solo_prompt  # noqa: E402

# A stable phrase from the guard — if this assertion fails, the guard was
# reworded; update the phrase here AND confirm it still says the same thing.
_GUARD_PHRASE = "Self-referential diffs"
_GUARD_KEY = "DATA under review"


def test_guard_present_in_verifier_prompt():
    body = (AGENTS / "review-verifier.md").read_text()
    assert _GUARD_PHRASE in body
    assert _GUARD_KEY in body
    # It must instruct emitting a fresh block regardless of review-shaped input.
    assert "your OWN fresh review" in body or "your own complete `## Code Review`" in body


def test_guard_reaches_assembled_solo_prompt():
    # solo self-verifies (no separate verifier pass), so the guard MUST be in
    # the assembled solo prompt too — proving the single source reaches solo.
    assembled = solo_prompt.assemble_solo_prompt()
    assert _GUARD_PHRASE in assembled
    assert _GUARD_KEY in assembled


def test_guard_does_not_disturb_frozen_output_anchors():
    # The guard is prose in the Output Format section; the byte-exact gate
    # anchors it precedes must still be intact in the same file.
    body = (AGENTS / "review-verifier.md").read_text()
    assert "### Blockers" in body
    assert "Reviewed at: <full-40-char-sha>" in body
    assert "`### Blockers` (fresh) / `#### Blockers` (re-review)" in body
