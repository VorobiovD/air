"""Offline tests for the MA-independent headless learn (learn_headless.py).

Network-free: the store API + render + counter are faked, and the LLM
`complete` is injected. Exercises the deterministic map/reduce/write
orchestration + the safety guards (size-floor, isolation, race-yield, dry-run).
"""

import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import learn_headless as L  # noqa: E402
import memory_store  # noqa: E402


class FakeStore:
    """In-memory stand-in for the memory_store functions learn uses, with
    update_with matching the real read-modify-write + no-op-skip semantics."""

    def __init__(self, files: dict[str, str], store_id="memstore_x"):
        self.files = dict(files)
        self.store_id = store_id
        self.writes: list[str] = []

    def get_store_id(self, repo, flow="review"):
        return self.store_id

    def list_memories(self, store_id, prefix="/"):
        return {p: {"id": f"mem_{i}", "content_sha256": f"sha_{i}"}
                for i, p in enumerate(self.files)}

    def read_memory(self, store_id, path):
        if path in self.files:
            return self.files[path], f"sha::{self.files[path]}", "mem_x"
        return None

    def update_with(self, store_id, path, fn, default="", must_exist=False):
        current = self.files.get(path)
        if current is None:
            if must_exist:
                return None
            new = fn(default)
            self.files[path] = new
            self.writes.append(path)
            return new
        new = fn(current)
        if new == current:          # real semantics: no-op skips the write
            return new
        self.files[path] = new
        self.writes.append(path)
        return new


@pytest.fixture
def fake(monkeypatch):
    store = FakeStore({
        "/authors/alice.md": "# alice\n- **X** (3x: #1,#2,#3 | last 0 PRs: 0 clean): tends to skip null checks",
        "/authors/bob.md": "# bob\n- **Y** (1x: #9 | new): wide except",
        memory_store.GLOSSARY_PATH: "| `Octane` | worker model | AGENTS.md |",
        memory_store.COMMON_FINDINGS_PATH: "- empty-array guard before implode",
        memory_store.SERVICE_PATTERNS_PATH: "- service: retry policy on the gateway",
        # These two MUST be ignored by the curator (staged, not on the hot path):
        "/review-misc.md": "## Author Patterns\nintro",
        "/meta/air-meta.json": "{}",
    })
    monkeypatch.setattr(L, "memory_store", _store_module(store))
    # Render + counter: record calls, no network.
    calls = {"render": 0, "reset_argv": None}

    def fake_render(store_id, repo, token):
        calls["render"] += 1
        return True
    monkeypatch.setattr(L.render_store_to_wiki, "render_push_and_stamp", fake_render)

    def fake_meta_main(argv):
        calls["reset_argv"] = argv
        return 0
    monkeypatch.setattr(L.meta, "main", fake_meta_main)
    return store, calls


def _store_module(store):
    """A stand-in module object exposing the path constants + the fake fns."""
    m = types.SimpleNamespace(
        AUTHOR_PREFIX=memory_store.AUTHOR_PREFIX,
        GLOSSARY_PATH=memory_store.GLOSSARY_PATH,
        COMMON_FINDINGS_PATH=memory_store.COMMON_FINDINGS_PATH,
        SERVICE_PATTERNS_PATH=memory_store.SERVICE_PATTERNS_PATH,
        PROJECT_PROFILE_PATH=memory_store.PROJECT_PROFILE_PATH,
        get_store_id=store.get_store_id,
        list_memories=store.list_memories,
        read_memory=store.read_memory,
        update_with=store.update_with,
    )
    return m


def _curate_appending(persona, content, *, label=""):
    """A 'real' curation: returns a meaningfully-changed (longer) version."""
    return content + "\n- **curated marker**"


def test_only_curatable_files_are_targeted(fake):
    store, calls = fake
    seen = []

    def complete(persona, content, *, label=""):
        seen.append(label)
        return content + "\n- **m**"
    L.run_headless_learn("o/r", token="t", complete=complete)
    # authors + glossary + common + service — NOT review-misc / meta / history / profile
    assert set(seen) == {"/authors/alice.md", "/authors/bob.md",
                         memory_store.GLOSSARY_PATH, memory_store.COMMON_FINDINGS_PATH,
                         memory_store.SERVICE_PATTERNS_PATH}
    assert "/review-misc.md" not in seen
    assert "/meta/air-meta.json" not in seen


def test_changed_files_written_rendered_reset(fake):
    store, calls = fake
    out = L.run_headless_learn("o/r", token="t", complete=_curate_appending)
    assert set(out["written"]) == {"/authors/alice.md", "/authors/bob.md",
                                   memory_store.GLOSSARY_PATH, memory_store.COMMON_FINDINGS_PATH,
                                   memory_store.SERVICE_PATTERNS_PATH}
    assert calls["render"] == 1
    assert calls["reset_argv"][:2] == ["reset", "--store-id"]
    assert out["reset"] is True
    assert "**curated marker**" in store.files["/authors/alice.md"]


def test_noop_curation_skips_write(fake):
    store, calls = fake

    def identity(persona, content, *, label=""):
        return content  # unchanged → must not write
    out = L.run_headless_learn("o/r", token="t", complete=identity)
    assert out["written"] == []
    assert store.writes == []
    assert calls["render"] == 0          # nothing written → no render
    assert calls["reset_argv"] is not None  # counter still reset (cadence)


def test_size_floor_refuses_collapsed_curation(fake):
    store, calls = fake
    before = dict(store.files)

    def collapse(persona, content, *, label=""):
        return "x"  # catastrophic shrink → must be REFUSED
    out = L.run_headless_learn("o/r", token="t", complete=collapse)
    assert out["written"] == []
    assert store.files == before  # nothing mutated


def test_one_flaky_file_does_not_abort_the_run(fake):
    store, calls = fake

    def flaky(persona, content, *, label=""):
        if label == "/authors/alice.md":
            raise RuntimeError("model blip")
        return content + "\n- **ok**"
    out = L.run_headless_learn("o/r", token="t", complete=flaky)
    # alice failed (isolated); the other three still curated + written
    assert "/authors/alice.md" not in out["written"]
    assert "/authors/bob.md" in out["written"]
    assert memory_store.GLOSSARY_PATH in out["written"]


def test_dry_run_writes_nothing(fake):
    store, calls = fake
    before = dict(store.files)
    out = L.run_headless_learn("o/r", token="t", complete=_curate_appending, dry_run=True)
    assert out["dry_run"] is True
    assert out["written"] == []
    assert store.writes == []
    assert store.files == before
    assert calls["render"] == 0
    assert calls["reset_argv"] is None   # dry-run touches nothing


def test_race_yields_instead_of_clobbering(fake, monkeypatch):
    store, calls = fake

    # Simulate a concurrent per-review strengthen landing AFTER the MAP read but
    # BEFORE the write (the only window that matters): inject it just as
    # update_with is about to read-modify-write alice.md. The race-aware fn
    # must YIELD (return current unchanged), never clobber the strengthen.
    orig_update = store.update_with
    STRONG = "# alice\nSTRENGTHENED BY A CONCURRENT REVIEW"
    fired = {"done": False}

    def racing_update(store_id, path, fn, default="", must_exist=False):
        if path == "/authors/alice.md" and not fired["done"]:
            fired["done"] = True
            store.files[path] = STRONG  # concurrent writer won the line first
        return orig_update(store_id, path, fn, default=default, must_exist=must_exist)
    store.update_with = racing_update
    monkeypatch.setattr(L, "memory_store", _store_module(store))

    out = L.run_headless_learn("o/r", token="t", complete=_curate_appending)
    # alice changed under us → curation yielded; the strengthen survives intact
    assert store.files["/authors/alice.md"] == STRONG
    assert "**curated marker**" not in store.files["/authors/alice.md"]
    assert "/authors/alice.md" not in out["written"]   # driver counts it as yielded
    assert "/authors/bob.md" in out["written"]          # the rest still curated


def test_no_store_repo_skips(monkeypatch):
    store = FakeStore({}, store_id=None)
    monkeypatch.setattr(L, "memory_store", _store_module(store))
    out = L.run_headless_learn("o/r", token="t", complete=_curate_appending)
    assert out["skipped"] == "no-store"
    assert out["curated"] == []


def test_all_curations_failed_does_not_reset(fake):
    # A total model outage (every map-call raises) must NOT consume the cadence —
    # otherwise the next learn waits a full interval with nothing curated.
    store, calls = fake

    def all_fail(persona, content, *, label=""):
        raise RuntimeError("model outage")
    out = L.run_headless_learn("o/r", token="t", complete=all_fail)
    assert out["written"] == []
    assert out["failures"] > 0
    assert out["reset"] is False
    assert calls["reset_argv"] is None     # counter NOT reset → re-arms next review
    assert calls["render"] == 0


def test_overflow_chunked_file_is_skipped(fake):
    # A primary memory split into /archive overflow chunks must NOT be curated
    # (curating it alone could drop the marker and orphan the chunks).
    store, calls = fake
    store.files["/authors/alice.md"] = (
        "<!-- older content: see /archive/alice-overflow-1.md -->\n"
        "# alice\n- **X** (3x: #1 | last 0 PRs: 0 clean): skips null checks")
    seen = []

    def complete(persona, content, *, label=""):
        seen.append(label)
        return content + "\n- **m**"
    out = L.run_headless_learn("o/r", token="t", complete=complete)
    assert "/authors/alice.md" not in seen          # never curated
    assert "/authors/alice.md" not in out["written"]
    assert out["skipped_chunked"] == 1
    assert "older content: see" in store.files["/authors/alice.md"]  # untouched


def test_fidelity_refuses_dropping_a_pattern_or_lowering_a_count(fake):
    store, calls = fake

    def lossy(persona, content, *, label=""):
        if label == "/authors/alice.md":
            # drops pattern X entirely
            return "# alice\n- **Z** (1x: #5 | new): something else"
        if label == "/authors/bob.md":
            # lowers Y's count 1 -> 0 (well, rewrites to 0x — a count regression)
            return "# bob\n- **Y** (0x: | new): wide except"
        return content + "\n- **ok**"
    out = L.run_headless_learn("o/r", token="t", complete=lossy)
    assert "/authors/alice.md" not in out["written"]   # dropped-pattern → refused
    assert "/authors/bob.md" not in out["written"]      # lowered-count → refused
    assert store.files["/authors/alice.md"].startswith("# alice\n- **X**")  # original kept


def test_fidelity_refuses_dropping_a_glossary_term(fake):
    store, calls = fake
    store.files[memory_store.GLOSSARY_PATH] = "| `Octane` | a | s |\n| `Pennant` | b | s |"

    def drop_term(persona, content, *, label=""):
        if label == memory_store.GLOSSARY_PATH:
            return "| `Octane` | a | s |"   # dropped Pennant
        return content + "\n- **ok**"
    out = L.run_headless_learn("o/r", token="t", complete=drop_term)
    assert memory_store.GLOSSARY_PATH not in out["written"]
    assert "Pennant" in store.files[memory_store.GLOSSARY_PATH]


# --- pure-function guards ---------------------------------------------------

def test_fidelity_allows_legit_dedup_and_count_increase():
    orig = "# a\n- **X** (3x: #1,#2,#3 | last 0): t\n- **Y** (1x: #9): u"
    # merge narrows refs + bumps X's count; keeps both patterns
    cur = "# a\n- **X** (4x: #1,#2,#3,#4 | last 0): t\n- **Y** (1x: #9): u"
    assert L._fidelity_violation("/authors/a.md", orig, cur) is None


def test_fidelity_findings_file_allows_merge():
    # findings files may drop/merge entries — byte-floor only, no structural check
    orig = "- a\n- b\n- c"
    assert L._fidelity_violation(memory_store.COMMON_FINDINGS_PATH, orig, "- a") is None


def test_is_chunked_detects_overflow_marker():
    assert L._is_chunked("<!-- older content: see /archive/x-overflow-2.md -->\nbody")
    assert not L._is_chunked("# normal file\n- **X** (1x): t")


def test_default_complete_raises_on_max_tokens(monkeypatch):
    # A curation that hits max_tokens must raise (so _curate_one isolates it),
    # never return a half-formed file that could pass the size floor.
    # _default_complete STREAMS (required by the SDK at high max_tokens), so the
    # fake models messages.stream() as a context manager with get_final_message().
    class _Block:
        type = "text"
        text = "truncated..."

    class _Msg:
        stop_reason = "max_tokens"
        content = [_Block()]

    class _FakeStream:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get_final_message(self):
            return _Msg()

    class _FakeClient:
        class messages:
            @staticmethod
            def stream(**kw):
                return _FakeStream()
    monkeypatch.setattr(L, "_client", _FakeClient())
    with pytest.raises(ValueError, match="max_tokens"):
        L._default_complete("persona", "x" * 1000, label="/glossary.md")


def test_main_exit_code_signals_total_failure(monkeypatch):
    # main() returns non-zero only on a total outage (failures>0, nothing
    # written) so review.py surfaces the visible `[warn] … exited N` line.
    monkeypatch.setattr(L, "run_headless_learn",
                        lambda *a, **k: {"failures": 3, "written": []})
    assert L.main(["o/r"]) == 1
    monkeypatch.setattr(L, "run_headless_learn",
                        lambda *a, **k: {"failures": 0, "written": ["/glossary.md"]})
    assert L.main(["o/r"]) == 0
    # all-refused (failures=0, nothing written) is a clean exit 0 — guard worked
    monkeypatch.setattr(L, "run_headless_learn",
                        lambda *a, **k: {"failures": 0, "written": []})
    assert L.main(["o/r"]) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# --- REVIEW-HISTORY (KAIROS) regen (Phase-1b) --------------------------------

_PR_BODIES = [
    {"pr": 10, "body": "## Code Review\n\n### Blockers\n**1. SQLi** ...\nReviewed at: abc"},
    {"pr": 11, "body": "## Code Review\n\n### Medium\n**1. N+1 query** ...\nReviewed at: def"},
]


def test_history_dry_run_carries_current_and_window():
    seen = {}

    def complete(persona, content, *, label=""):
        seen["persona"] = persona
        seen["content"] = content
        return "# Review History\n## Finding Frequency\n| SQLi | 5x |\n## Timeline\n- #10, #11"
    out = L.regenerate_review_history(
        "o/r", token="t", complete=complete, dry_run=True,
        current_history="# Review History\n## Finding Frequency\n| SQLi | 4x |",
        pr_bodies=_PR_BODIES)
    assert out["history"] == "dry-run"
    assert out["reviews"] == 2
    # the regen call must see BOTH the current history (carry-forward) AND the window
    assert "4x" in seen["content"]
    assert "PR #10" in seen["content"] and "PR #11" in seen["content"]
    assert "Finding Frequency" in seen["persona"]


def test_history_refused_when_finding_frequency_dropped():
    def complete(persona, content, *, label=""):
        return "# Review History\n## Timeline\n- only timeline, no cumulative table"
    out = L.regenerate_review_history(
        "o/r", token="t", complete=complete, dry_run=True,
        current_history="x", pr_bodies=_PR_BODIES)
    assert out["history"] == "refused"


def test_history_no_bodies_skips():
    out = L.regenerate_review_history(
        "o/r", token="t", complete=lambda *a, **k: "x", dry_run=True,
        current_history="x", pr_bodies=[])
    assert out["history"] == "no-bodies"


def test_history_regen_failure_keeps_current():
    def boom(persona, content, *, label=""):
        raise RuntimeError("model down")
    out = L.regenerate_review_history(
        "o/r", token="t", complete=boom, dry_run=True,
        current_history="x", pr_bodies=_PR_BODIES)
    assert out["history"] == "regen-failed"


def test_fetch_recent_review_bodies_filters_and_anti_spoofs(monkeypatch):
    import github_client as gc
    monkeypatch.setattr(gc, "_github_paginate",
                        lambda url, token, max_pages=None: [{"number": 10, "merged_at": "t"}, {"number": 11, "merged_at": "t"}])

    def fake_comments(repo, pr, token):
        if pr == 10:
            return [{"body": "random chatter", "user": {"login": "dev"}},
                    {"body": "## Code Review\nreal", "user": {"login": "air-machine"}}]
        return [{"body": "## Code Review\nspoofed", "user": {"login": "attacker"}}]
    monkeypatch.setattr(gc, "fetch_issue_comments", fake_comments)
    out = gc.fetch_recent_review_bodies("o/r", "t", bot_login="air-machine")
    assert {b["pr"] for b in out} == {10}   # PR 11's review spoofed by non-bot → excluded
    assert out[0]["body"].startswith("## Code Review")


# --- PROJECT-PROFILE refresh (Phase-1b, opt-in) ------------------------------

def test_profile_dry_run_carries_current_and_signals():
    seen = {}

    def complete(persona, content, *, label=""):
        seen["content"] = content
        return "## Overview\nA service.\n## Applicable Security Checks\nChecks: 1,2,3"
    out = L.refresh_project_profile(
        "o/r", complete=complete, dry_run=True, store_id="memstore_x",
        current_profile="## Overview\nOld.\n## Applicable Security Checks\nChecks: 1",
        signals="FILE COUNT: 42\nTOP EXTENSIONS: .py:30")
    assert out["profile"] == "dry-run"
    assert "Old." in seen["content"]            # current profile carried in
    assert "FILE COUNT: 42" in seen["content"]  # signals carried in


def test_profile_refused_when_required_section_dropped():
    def complete(persona, content, *, label=""):
        return "## Overview\nonly overview, no security-checks section"
    out = L.refresh_project_profile(
        "o/r", complete=complete, dry_run=True, store_id="memstore_x",
        current_profile="x", signals="y")
    assert out["profile"] == "refused"


def test_gather_repo_signals_real_checkout():
    # Run against air's own checkout — deterministic, no network.
    sig = L._gather_repo_signals(".")
    assert "FILE COUNT:" in sig and "TOP EXTENSIONS:" in sig
    assert ".py" in sig   # air has Python


def test_fetch_recent_review_bodies_matches_re_reviews(monkeypatch):
    # A "## Code Review (Re-review)" body must be matched too (canonical prefix
    # set), else multi-round PRs are silently dropped from the history.
    import github_client as gc
    monkeypatch.setattr(gc, "_github_paginate",
                        lambda url, token, max_pages=None: [{"number": 20, "merged_at": "t"}])
    monkeypatch.setattr(gc, "fetch_issue_comments", lambda repo, pr, token: [
        {"body": "## Code Review (Re-review)\nround 2 ...", "user": {"login": "air-machine"}}])
    out = gc.fetch_recent_review_bodies("o/r", "t")  # no bot_login → prefix is the signal
    assert {b["pr"] for b in out} == {20}
    assert "Re-review" in out[0]["body"]


# --- Phase-2 Batch API ------------------------------------------------------

def test_apply_guards_matrix():
    lg = lambda *a, **k: None
    A = "/authors/a.md"
    orig = "# a\n- **X** (3x: #1): t"
    assert L._apply_guards(A, orig, "", lg) == (None, "failed")          # empty
    assert L._apply_guards(A, orig, "x", lg) == (None, "refused")        # size floor
    assert L._apply_guards(A, orig, "# a\n- **Z** (1x): u", lg)[1] == "refused"  # dropped X
    assert L._apply_guards(A, orig, orig, lg) == (None, "noop")          # unchanged
    good = orig + "\n- **Y** (1x: #2): more"
    assert L._apply_guards(A, orig, good, lg) == (good, "ok")            # valid change


def test_batch_curate_applies_guards_and_isolates(monkeypatch):
    lg = lambda *a, **k: None
    pending = [("/authors/a.md", "# a\n- **X** (3x: #1): tends to skip"),
               (memory_store.GLOSSARY_PATH, "| `T` | def | s |"),
               (memory_store.COMMON_FINDINGS_PATH, "- finding one")]

    def fake_submit(items, log):
        return {"/authors/a.md": "# a\n- **X** (3x: #1): tends to skip\n- **NEW** (1x): n",  # ok
                memory_store.GLOSSARY_PATH: "| `OTHER` | x | s |",   # dropped term T → refused
                memory_store.COMMON_FINDINGS_PATH: None}              # request failed
    monkeypatch.setattr(L, "_submit_batch", fake_submit)
    out = L._batch_curate(pending, lg)
    assert out["/authors/a.md"][2] == "ok"
    assert out[memory_store.GLOSSARY_PATH][2] == "refused"
    assert out[memory_store.COMMON_FINDINGS_PATH][2] == "failed"


def test_run_headless_uses_batch_when_enabled(fake, monkeypatch):
    store, calls = fake
    monkeypatch.setattr(L, "_BATCH_ENABLED", True)

    def fake_submit(items, log):
        # one batch call for ALL files; return a real change per file
        return {p: c + "\n- **batch marker**" for p, persona, c in items}
    monkeypatch.setattr(L, "_submit_batch", fake_submit)
    # no `complete` injected → complete is _default_complete → batch path eligible
    out = L.run_headless_learn("o/r", token="t")
    assert "/authors/alice.md" in out["written"]
    assert "**batch marker**" in store.files["/authors/alice.md"]
    assert calls["render"] == 1 and calls["reset_argv"] is not None
