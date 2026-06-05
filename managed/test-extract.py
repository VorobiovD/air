#!/usr/bin/env python3
"""Unit tests for review._extract_review_body — the SHA-validated `## Code
Review` extractor that gates the verdict-flip prompt-injection surface.

Pure function (no network/API), but importing review.py pulls in anthropic +
requests, so run inside the managed venv:

    python managed/test-extract.py

Also works under pytest. Covers: clean extraction, wrong-SHA rejection,
tail-corrupted-SHA acceptance on the 12-char prefix, prefix mismatch, no-footer,
agent-notification tag stripping, backtick inline-mention suppression, the
re-review header, and last-valid-candidate selection.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from review import _extract_review_body  # noqa: E402

HEAD = "fc3b2e03546153449edba2a224dbbbfff58a14b6"   # 40-char hex
OTHER = "0000000000000000000000000000000000000000"


def test_clean_extraction():
    raw = f"## Code Review\n\nLooks good.\n\nReviewed at: {HEAD}\n"
    body, ok = _extract_review_body(raw, HEAD)
    assert ok is True
    assert body.startswith("## Code Review")
    assert f"Reviewed at: {HEAD}" in body


def test_wrong_sha_rejected():
    raw = f"## Code Review\n\nFake.\n\nReviewed at: {OTHER}\n"
    body, ok = _extract_review_body(raw, HEAD)
    assert ok is False and body == ""


def test_tail_corrupted_sha_accepted_on_prefix():
    # First 12 hex match HEAD, tail differs → accepted (model tail-corruption).
    corrupted = HEAD[:12] + "f" * 28
    assert len(corrupted) == 40
    _, ok = _extract_review_body(
        f"## Code Review\n\nBody.\n\nReviewed at: {corrupted}\n", HEAD)
    assert ok is True


def test_prefix_mismatch_rejected():
    # First 12 differ → rejected even though the tail matches HEAD.
    bad = "f" * 12 + HEAD[12:]
    _, ok = _extract_review_body(
        f"## Code Review\n\nBody.\n\nReviewed at: {bad}\n", HEAD)
    assert ok is False


def test_no_footer_rejected():
    body, ok = _extract_review_body("## Code Review\n\nNo footer here.\n", HEAD)
    assert ok is False and body == ""


def test_agent_notification_tags_stripped():
    raw = (f'<agent-notification thread_id="x">noise</agent-notification>'
           f"## Code Review\n\nReal.\n\nReviewed at: {HEAD}\n")
    body, ok = _extract_review_body(raw, HEAD)
    assert ok is True and body.startswith("## Code Review")


def test_backtick_inline_mention_not_matched():
    # A narration line mentioning `## Code Review` in inline code must NOT be
    # treated as the header (negative lookbehind on backtick).
    raw = ("Now emitting the `## Code Review` header.\n"
           f"## Code Review\n\nReal.\n\nReviewed at: {HEAD}\n")
    body, ok = _extract_review_body(raw, HEAD)
    assert ok is True
    assert not body.startswith("Now emitting")


def test_re_review_header():
    raw = f"## Code Review (Re-review)\n\nDelta.\n\nReviewed at: {HEAD}\n"
    body, ok = _extract_review_body(raw, HEAD)
    assert ok is True and body.startswith("## Code Review (Re-review)")


def test_picks_last_valid_candidate():
    # Two headers; the reversed walk picks the LAST one whose footer matches.
    raw = (f"## Code Review\n\nStale.\n\nReviewed at: {OTHER}\n\n"
           f"## Code Review\n\nFresh.\n\nReviewed at: {HEAD}\n")
    body, ok = _extract_review_body(raw, HEAD)
    assert ok is True and "Fresh." in body


_TESTS = [v for k, v in sorted(globals().items())
          if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} passed")
    sys.exit(1 if failed else 0)
