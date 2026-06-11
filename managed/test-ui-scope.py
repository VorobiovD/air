#!/usr/bin/env python3
"""Unit tests for the UI-copy reviewer dispatch gate — review._diff_touches_ui
and review._path_is_ui — plus the solo-prompt inclusion of the UI lens.

Pure functions (no network/API), but importing review.py / setup.py pulls in
anthropic + requests, so run inside the managed venv:

    python managed/test-ui-scope.py

Also works under pytest. Covers: the path/extension allowlist (markup, i18n
catalogs, user-facing docs), the negative cases (backend, CSS-only, air's own
wiki/pattern .md), the mixed case, the diff-header fallback when post_paths is
empty, the fail-open default, and that assemble_solo_prompt() includes the UI
lens (solo applies it too — it self-scopes on non-UI diffs).
"""
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import review  # noqa: E402 — module handle for patching review.memory_store
from review import (  # noqa: E402
    _diff_touches_ui,
    _path_is_ui,
    _path_matches_globs,
    _parse_copy_paths_section,
    _user_facing_copy_globs,
)
from setup import assemble_solo_prompt, SUB_AGENTS  # noqa: E402


@contextmanager
def patched_attr(obj, name, value):
    """Temporarily swap an attribute (e.g. review.memory_store.read_memory)."""
    saved = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, saved)


def _diff_with(*paths):
    """Minimal unified-diff text whose only path signal is the +++ headers."""
    return "\n".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}" for p in paths)


# --- _path_is_ui ------------------------------------------------------------

def test_path_markup_extensions():
    for p in ("src/App.tsx", "web/Button.vue", "ui/Card.jsx", "views/home.svelte",
              "templates/checkout.html", "tmpl/email.hbs", "page.astro"):
        assert _path_is_ui(p), p


def test_path_i18n_catalogs():
    for p in ("locales/en.json", "i18n/fr.yaml", "locale/messages.po",
              "lib/l10n/app_en.arb", "lang/strings.pot"):
        assert _path_is_ui(p), p


def test_path_bare_root_i18n():
    # Bare en.json / messages.json at repo root — no locales/ tree, no
    # separator between stem and extension (the regex must still match).
    for p in ("en.json", "messages.json", "en.yaml", "messages.yml"):
        assert _path_is_ui(p), p


def test_path_user_facing_docs():
    for p in ("help/getting-started.mdx", "content/landing.md", "site/faq/billing.md",
              "docs/help/faq.md"):  # matches because help/ is a user-facing segment
        assert _path_is_ui(p), p


def test_path_mdx_is_markup_regardless_of_dir():
    # `.mdx` is matched as MARKUP via _UI_EXTENSIONS (rendered doc-site page),
    # so it's in scope even under docs/ — distinct from bare docs/**.md below.
    for p in ("docs/guide.mdx", "docs/architecture.mdx"):
        assert _path_is_ui(p), p


def test_path_internal_docs_excluded():
    # Bare docs/**.md is internal eng material (specs/plans/ADRs) — must NOT
    # trigger the copy reviewer. Regression for ai-relay #231 (billing-tool
    # docs/). (.mdx differs — see test_path_mdx_is_markup_regardless_of_dir.)
    for p in ("docs/architecture.md", "docs/specs/design.md", "docs/adr/0001.md",
              "gcp/functions/apis/billing-tool/docs/superpowers/plans/2026-06-07-fix.md"):
        assert not _path_is_ui(p), p


def test_path_negatives():
    for p in ("api/handler.go", "db/migrations/003.sql", "managed/review.py",
              "styles/theme.css", "app/main.scss"):
        assert not _path_is_ui(p), p


def test_path_excludes_air_pattern_files():
    # air's own wiki/pattern markdown must never trigger a UI review.
    for p in ("REVIEW.md", "REVIEW-HISTORY.md", "GLOSSARY.md", "PROJECT-PROFILE.md",
              "ACCEPTED-PATTERNS.md", "SEVERITY-CALIBRATION.md"):
        assert not _path_is_ui(p), p


# --- _diff_touches_ui (post_paths preferred) --------------------------------

def test_scope_from_post_paths_positive():
    assert _diff_touches_ui(["src/App.tsx"], "") is True
    assert _diff_touches_ui(["locales/en.json"], "") is True
    assert _diff_touches_ui(["templates/checkout.html"], "") is True


def test_scope_from_post_paths_negative():
    assert _diff_touches_ui(["api/handler.go", "db/migrations/003.sql"], "") is False
    assert _diff_touches_ui(["styles/theme.css"], "") is False
    assert _diff_touches_ui(["REVIEW.md"], "") is False


def test_scope_mixed_any_ui_triggers():
    assert _diff_touches_ui(["api/handler.go", "src/Modal.tsx"], "") is True


def test_scope_union_catches_ui_beyond_post_paths_cap():
    # post_paths is capped (40 non-UI files) but the diff headers include a UI
    # file — the union of both sources must still trigger. post_paths alone
    # (capped) would miss it.
    backend_paths = [f"svc/file{i}.go" for i in range(40)]
    diff = _diff_with("svc/file0.go", "web/Modal.tsx")
    assert _diff_touches_ui(backend_paths, diff) is True


# --- _diff_touches_ui (union of post_paths + diff-header paths) --------------

def test_scope_diff_fallback_positive():
    assert _diff_touches_ui([], _diff_with("web/Button.vue")) is True


def test_scope_diff_fallback_negative():
    assert _diff_touches_ui([], _diff_with("api/main.go", "go.mod")) is False


def test_scope_fail_open_when_no_paths():
    # No post_paths AND no parseable diff headers → fail OPEN (review it).
    assert _diff_touches_ui([], "") is True
    assert _diff_touches_ui([], "some prose with no diff headers") is True


# --- PROJECT-PROFILE `## User-Facing Copy Paths` opt-in (TUI/.py coverage) ---

_PROFILE_WITH_SECTION = """# Project Profile

## Architecture
A Python TUI.

## User-Facing Copy Paths
- agent-core/agents/*.py
- `**/messages/*.py`

## Applicable Security Checks
Checks: 1, 2, 3
"""

_PROFILE_NO_SECTION = "# Project Profile\n\n## Architecture\nNo copy paths here.\n"


def test_parse_copy_paths_section():
    assert _parse_copy_paths_section(_PROFILE_WITH_SECTION) == [
        "agent-core/agents/*.py", "**/messages/*.py"]  # backticks stripped, stops at next heading


def test_parse_copy_paths_missing_or_empty():
    assert _parse_copy_paths_section(_PROFILE_NO_SECTION) == []
    assert _parse_copy_paths_section("") == []


def test_user_facing_copy_globs_no_store_is_empty():
    # No store → no read, no globs (web-only fallback). Pure, no network.
    assert _user_facing_copy_globs(None) == []


def test_user_facing_copy_globs_reads_and_parses_store():
    # Happy path: store returns profile text → section parsed into globs.
    with patched_attr(review.memory_store, "read_memory",
                      lambda sid, path: (_PROFILE_WITH_SECTION, "sha", "id")):
        assert _user_facing_copy_globs("memstore_x") == [
            "agent-core/agents/*.py", "**/messages/*.py"]


def test_user_facing_copy_globs_read_error_returns_empty():
    # A store read that raises must fail safe to [] (never block a review).
    def _boom_read(sid, path):
        raise RuntimeError("store down")
    with patched_attr(review.memory_store, "read_memory", _boom_read):
        assert _user_facing_copy_globs("memstore_x") == []


def test_path_matches_globs_greedy_and_excludes():
    globs = ["agent-core/agents/*.py", "**/messages/*.py"]
    assert _path_matches_globs("agent-core/agents/clinical/intake.py", globs)  # * greedy across /
    assert _path_matches_globs("src/messages/welcome.py", globs)
    assert not _path_matches_globs("agent-core/handlers/db.py", globs)         # not under a glob
    assert not _path_matches_globs("REVIEW.md", globs)                         # exclude still wins


def test_diff_touches_ui_copy_path_py_in_scope():
    globs = ["agent-core/agents/*.py"]
    assert _diff_touches_ui(["agent-core/agents/intake.py"], "", globs) is True


def test_diff_touches_ui_backend_py_stays_out_of_scope():
    # The $0-backend guarantee: a .py NOT under any declared glob never triggers.
    globs = ["agent-core/agents/*.py"]
    assert _diff_touches_ui(["agent-core/handlers/db.py", "infra/sam.yaml"], "", globs) is False
    # And with no globs at all, plain .py is out.
    assert _diff_touches_ui(["svc/handler.py"], "", ()) is False


def test_diff_touches_ui_web_unaffected_by_globs():
    assert _diff_touches_ui(["src/App.tsx"], "", ()) is True
    assert _diff_touches_ui(["src/App.tsx"], "", ["agent-core/agents/*.py"]) is True


def test_diff_touches_ui_truncated_diff_no_checkout_fails_open():
    # A byte-cap-omitted segment has no `+++ b/` header; with no precomp
    # paths to fall back on, an omitted UI file would be invisible to both
    # signals — fail open.
    from github_client import DIFF_TRUNCATION_MARKER
    diff = (
        "diff --git a/svc/handler.py b/svc/handler.py\n"
        "+++ b/svc/handler.py\n+x = 1\n"
        f"{DIFF_TRUNCATION_MARKER} at 500000 bytes — 2 file(s) omitted: src/App.tsx]\n"
    )
    assert _diff_touches_ui([], diff, ()) is True


def test_diff_touches_ui_truncated_diff_with_checkout_uses_paths():
    # With precomp paths present (uncapped, from git), the path list is
    # authoritative — truncation alone must not force the UI agent.
    from github_client import DIFF_TRUNCATION_MARKER
    diff = (
        "diff --git a/svc/handler.py b/svc/handler.py\n"
        "+++ b/svc/handler.py\n+x = 1\n"
        f"{DIFF_TRUNCATION_MARKER} at 500000 bytes — 1 file(s) omitted: big.py]\n"
    )
    assert _diff_touches_ui(["svc/handler.py", "big.py"], diff, ()) is False


# --- solo includes the UI lens ----------------------------------------------

def test_solo_prompt_includes_ui_lens():
    assert "ui-copy-reviewer" in SUB_AGENTS
    prompt = assemble_solo_prompt()
    assert "ui-copy-reviewer" in prompt  # its lens header is concatenated in


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
