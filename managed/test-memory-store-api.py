"""Regression guards for the Managed-Agents Memories API call shape (2026-07-23).

The SDK/API changed three things at once and made EVERY store read crash
(reviews ran pattern-blind; headless learn crashed):
  1. `memories.list` dropped `order_by`  → TypeError on the kwarg
  2. `depth` is now bounded 0-1           → `depth=20` 400s
  3. `path_prefix` must be directory-shaped `^(/([^/\x00]+/)*)?$` → a full file
     path like `/authors/foo.md` 400s; list its dir `/authors/` and match exactly

These mock-based tests lock OUR call shape (no order_by/depth; dir-shaped
path_prefix); the live fix was verified against a real store the same day.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))              # managed/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "air" / "lib"))
import memory_store as ms  # noqa: E402


class _Page:
    def __init__(self, data): self._data = data
    def model_dump(self): return {"data": self._data, "next_page": None}


class _RecMemories:
    """Records every .list/.create/.update call; .list returns `data`."""
    def __init__(self, calls, data=None):
        self.calls = calls; self._data = data or []
    def list(self, store_id, **kw):
        self.calls.append(("list", store_id, kw)); return _Page(self._data)
    def retrieve(self, memory_id, **kw):
        self.calls.append(("retrieve", memory_id, kw))
        return types.SimpleNamespace(content="body", content_sha256="sha", id=memory_id)
    def create(self, store_id, **kw):
        self.calls.append(("create", store_id, kw))
    def update(self, memory_id, **kw):
        self.calls.append(("update", memory_id, kw))


def _install(monkeypatch, data=None):
    calls = []
    mem = _RecMemories(calls, data)
    fake = types.SimpleNamespace(
        beta=types.SimpleNamespace(memory_stores=types.SimpleNamespace(memories=mem)))
    monkeypatch.setattr(ms, "client", lambda: fake)
    return calls


def test_list_memories_passes_no_order_by_no_depth(monkeypatch):
    calls = _install(monkeypatch)
    ms.list_memories("store1", "/")
    kind, store, kw = calls[0]
    assert kind == "list" and store == "store1"
    assert "order_by" not in kw, "order_by removed from the API — must not be sent"
    assert "depth" not in kw, "depth now bounded 0-1 — don't send the old depth=20"
    assert kw == {"path_prefix": "/"}


def test_read_memory_lists_by_directory_prefix_not_full_path(monkeypatch):
    # The bug: read_memory passed the FULL file path as path_prefix → 400.
    calls = _install(monkeypatch, data=[])   # empty → absent → returns None (no retrieve)
    assert ms.read_memory("s", "/authors/VorobiovD.md") is None
    list_calls = [c for c in calls if c[0] == "list"]
    assert list_calls[0][2]["path_prefix"] == "/authors/"   # dir-shaped, not the file path


def test_read_memory_matches_exact_path_then_retrieves(monkeypatch):
    data = [{"type": "memory_metadata", "path": "/authors/VorobiovD.md",
             "id": "mem_1", "content_sha256": "sha1"}]
    calls = _install(monkeypatch, data=data)
    got = ms.read_memory("s", "/authors/VorobiovD.md")
    assert got == ("body", "sha", "mem_1")
    assert any(c[0] == "retrieve" and c[1] == "mem_1" for c in calls)


def test_write_memory_lists_by_dir_prefix_then_creates(monkeypatch):
    calls = _install(monkeypatch, data=[])   # absent → create path
    ms.write_memory("s", "/meta/air-meta.json", "content")
    assert calls[0][0] == "list" and calls[0][2]["path_prefix"] == "/meta/"
    create = [c for c in calls if c[0] == "create"]
    assert create and create[0][2]["path"] == "/meta/air-meta.json"


@pytest.mark.parametrize("path,expected", [
    ("/glossary.md", "/"),
    ("/authors/VorobiovD.md", "/authors/"),
    ("/meta/air-meta.json", "/meta/"),
    ("/archive/glossary-overflow-1.md", "/archive/"),
])
def test_dir_prefix(path, expected):
    assert ms._dir_prefix(path) == expected


def test_sdk_signature_conformance():
    # Binds to the REAL SDK: order_by must NOT be a param (would re-break),
    # path_prefix MUST be. Catches a future SDK change before it hits prod.
    anthropic = pytest.importorskip("anthropic")
    import inspect
    c = anthropic.Anthropic(api_key="x")
    params = inspect.signature(c.beta.memory_stores.memories.list).parameters
    assert "order_by" not in params, "SDK re-added order_by? re-audit the call sites"
    assert "path_prefix" in params
    assert "depth" in params  # still a param, just bounded 0-1 (we don't send it)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
