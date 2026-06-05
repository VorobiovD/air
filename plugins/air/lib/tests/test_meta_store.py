"""meta.py memory-store backend — exercised against a stateful fake API."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LIB = HERE.parent
sys.path.insert(0, str(LIB))

import meta  # noqa: E402


class FakeStoreAPI:
    """In-memory stand-in for the 4 REST shapes _store_api issues."""

    def __init__(self):
        self.content: str | None = None
        self.sha = "sha-0"
        self.mem_id = "mem_fake"

    def __call__(self, method, path, body=None):
        if method == "GET" and "memories?" in path:
            if self.content is None:
                return {"data": []}
            # live API shape: list entries carry type "memory_metadata"
            return {"data": [{"type": "memory_metadata",
                              "path": meta.STORE_META_PATH,
                              "id": self.mem_id}]}
        if method == "GET" and self.mem_id in path:
            return {"content": self.content, "content_sha256": self.sha,
                    "id": self.mem_id}
        if method == "POST" and path.endswith("/memories"):
            assert self.content is None, "create on existing memory"
            self.content = body["content"]
            self.sha = "sha-1"
            return {"id": self.mem_id, "content_sha256": self.sha}
        if method == "POST" and self.mem_id in path:
            assert body["precondition"]["content_sha256"] == self.sha
            self.content = body["content"]
            self.sha = f"sha-{self.sha[-1]}x"
            return {"id": self.mem_id, "content_sha256": self.sha}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def fake(monkeypatch):
    api = FakeStoreAPI()
    monkeypatch.setattr(meta, "_store_api", api)
    return api


def test_store_bump_creates_then_increments(fake):
    rc = meta.main(["bump", "--store-id", "memstore_x", "--pr-number", "7"])
    assert rc == 0
    data = json.loads(fake.content)
    assert data["reviews_since"] == 1
    assert data["last_processed_pr"] == 7

    meta.main(["bump", "--store-id", "memstore_x", "--pr-number", "9"])
    data = json.loads(fake.content)
    assert data["reviews_since"] == 2
    assert data["last_processed_pr"] == 9


def test_store_check_triggers_at_threshold(fake):
    seed = meta._default_meta()
    seed["reviews_since"] = meta.REVIEWS_THRESHOLD
    fake.content = json.dumps(seed)
    rc = meta.main(["check", "--store-id", "memstore_x"])
    assert rc == 1


def test_store_reset_zeroes_counter(fake):
    seed = meta._default_meta()
    seed["reviews_since"] = 8
    fake.content = json.dumps(seed)
    meta.main(["reset", "--store-id", "memstore_x", "--pr-number", "50"])
    data = json.loads(fake.content)
    assert data["reviews_since"] == 0
    assert data["last_processed_pr"] == 50


def test_store_bump_failure_never_blocks(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(meta, "_store_api", boom)
    rc = meta.main(["bump", "--store-id", "memstore_x", "--pr-number", "1"])
    assert rc == 0  # warn + proceed; review flow must not fail on plumbing


def test_backend_arg_required():
    with pytest.raises(SystemExit):
        meta.main(["bump", "--pr-number", "1"])


# --- mirror-render throttle ------------------------------------------------

_NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


def test_mirror_due_within_window():
    m = {"last_mirror_render": (_NOW - timedelta(minutes=30)).isoformat()}
    due, _ = meta._mirror_due(m, now=_NOW)
    assert due is False


def test_mirror_due_elapsed():
    m = {"last_mirror_render": (_NOW - timedelta(hours=2)).isoformat()}
    due, _ = meta._mirror_due(m, now=_NOW)
    assert due is True


def test_mirror_due_never_rendered():
    due, reason = meta._mirror_due({"last_mirror_render": ""}, now=_NOW)
    assert due is True and "never" in reason


def test_mirror_due_unparseable_is_due():
    due, _ = meta._mirror_due({"last_mirror_render": "garbage"}, now=_NOW)
    assert due is True


def test_cmd_mirror_due_never_exits_1(fake):
    # Empty store → defaults (last_mirror_render="") → render due.
    assert meta.main(["mirror-due", "--store-id", "memstore_x"]) == 1


def test_cmd_mirror_due_within_window_exits_0(fake):
    seed = meta._default_meta()
    seed["last_mirror_render"] = meta._utc_now_iso()   # just rendered
    fake.content = json.dumps(seed)
    assert meta.main(["mirror-due", "--store-id", "memstore_x"]) == 0


def test_cmd_mirror_due_elapsed_exits_1(fake):
    seed = meta._default_meta()
    seed["last_mirror_render"] = "2020-01-01T00:00:00Z"   # ancient
    fake.content = json.dumps(seed)
    assert meta.main(["mirror-due", "--store-id", "memstore_x"]) == 1


def test_cmd_mirror_due_store_error_skips(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(meta, "_store_api", boom)
    # On store error a render would hit the same dead store — skip (exit 0), don't render.
    assert meta.main(["mirror-due", "--store-id", "memstore_x"]) == 0


def test_cmd_mirror_rendered_stamps(fake):
    assert meta.main(["mirror-rendered", "--store-id", "memstore_x"]) == 0
    stamped = json.loads(fake.content)["last_mirror_render"]
    assert stamped and meta._parse_iso(stamped)   # non-empty + parseable


def test_mirror_rendered_then_due_is_within_window(fake):
    # End-to-end: stamping resets the throttle, so the next due-check is a no-op.
    meta.main(["mirror-rendered", "--store-id", "memstore_x"])
    assert meta.main(["mirror-due", "--store-id", "memstore_x"]) == 0
