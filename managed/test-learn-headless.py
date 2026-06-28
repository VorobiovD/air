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
    # authors + glossary + common-findings — NOT review-misc / meta / history / profile
    assert set(seen) == {"/authors/alice.md", "/authors/bob.md",
                         memory_store.GLOSSARY_PATH, memory_store.COMMON_FINDINGS_PATH}
    assert "/review-misc.md" not in seen
    assert "/meta/air-meta.json" not in seen


def test_changed_files_written_rendered_reset(fake):
    store, calls = fake
    out = L.run_headless_learn("o/r", token="t", complete=_curate_appending)
    assert set(out["written"]) == {"/authors/alice.md", "/authors/bob.md",
                                   memory_store.GLOSSARY_PATH, memory_store.COMMON_FINDINGS_PATH}
    assert calls["render"] == 1
    assert calls["reset_argv"][:2] == ["reset", "--store-id"]
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
