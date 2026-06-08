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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from review import _diff_touches_ui, _path_is_ui  # noqa: E402
from setup import assemble_solo_prompt, SUB_AGENTS  # noqa: E402


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
    for p in ("docs/help/faq.md", "help/getting-started.mdx", "content/landing.md"):
        assert _path_is_ui(p), p


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
