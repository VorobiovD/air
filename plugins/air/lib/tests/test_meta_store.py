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


# --- claim: atomic bump + learn lock on the store backend ------------------

def test_store_claim_below_threshold_bumps_no_lock(fake):
    seed = meta._default_meta()
    seed["reviews_since"] = 3
    fake.content = json.dumps(seed)
    rc = meta.main(["claim", "--store-id", "memstore_x", "--pr-number", "11"])
    data = json.loads(fake.content)
    assert rc == 0
    assert data["reviews_since"] == 4
    assert not data.get("learn_claimed_at")


def test_store_claim_claims_at_threshold(fake):
    seed = meta._default_meta()
    seed["reviews_since"] = meta.REVIEWS_THRESHOLD - 1   # 14 → 15 crosses
    fake.content = json.dumps(seed)
    rc = meta.main(["claim", "--store-id", "memstore_x", "--pr-number", "11"])
    data = json.loads(fake.content)
    assert rc == 1
    assert data["reviews_since"] == meta.REVIEWS_THRESHOLD
    assert data["learn_claimed_at"]   # lock acquired in the same write


def test_store_claim_skips_when_lock_live(fake):
    seed = meta._default_meta()
    seed["reviews_since"] = 20
    seed["learn_claimed_at"] = meta._utc_now_iso()   # learn in flight
    fake.content = json.dumps(seed)
    rc = meta.main(["claim", "--store-id", "memstore_x", "--pr-number", "11"])
    data = json.loads(fake.content)
    assert rc == 0                       # lock held → no second learn
    assert data["reviews_since"] == 21   # review still counts
    assert data["learn_claimed_at"] == seed["learn_claimed_at"]  # untouched


def test_store_reset_clears_lock(fake):
    seed = meta._default_meta()
    seed["reviews_since"] = 16
    seed["learn_claimed_at"] = meta._utc_now_iso()
    fake.content = json.dumps(seed)
    meta.main(["reset", "--store-id", "memstore_x", "--pr-number", "50"])
    data = json.loads(fake.content)
    assert data["reviews_since"] == 0
    assert data["learn_claimed_at"] == ""


# --- M3: out-of-band (cron) lock claim/release -----------------------------

def test_claim_learn_lock_wins_when_free(fake):
    seed = meta._default_meta()
    seed["reviews_since"] = 20            # due, no lock
    fake.content = json.dumps(seed)
    assert meta.claim_learn_lock("memstore_x") is True
    data = json.loads(fake.content)
    assert data["learn_claimed_at"]      # lock acquired
    assert data["reviews_since"] == 20   # NOT bumped — a cron run isn't a review


def test_claim_learn_lock_loses_when_live(fake):
    seed = meta._default_meta()
    seed["reviews_since"] = 20
    seed["learn_claimed_at"] = meta._utc_now_iso()   # a review/other cron holds it
    fake.content = json.dumps(seed)
    before = fake.content
    assert meta.claim_learn_lock("memstore_x") is False
    assert fake.content == before        # no write on a lost claim


def test_claim_learn_lock_none_when_no_counter(fake):
    fake.content = None                  # store exists but no counter yet
    assert meta.claim_learn_lock("memstore_x") is False


def test_release_learn_lock_clears(fake):
    seed = meta._default_meta()
    seed["reviews_since"] = 20
    seed["learn_claimed_at"] = meta._utc_now_iso()
    fake.content = json.dumps(seed)
    meta.release_learn_lock("memstore_x")
    data = json.loads(fake.content)
    assert data["learn_claimed_at"] == ""
    assert data["reviews_since"] == 20   # release touches only the lock


# --- read-author: store-backed author-pattern read (CLI Fix 1) -------------
# The ai-relay 2026-06-27 bug: the store→wiki render emits per-author blocks
# under a heading the CLI's `### <login>` grep missed, so a dominant author
# read as "new author". The CLI now reads /authors/<login>.md from the store.
# Exit codes are the contract the CLI branches on: 0=found, 3=new, 2=unknown.

class FakeAuthorAPI:
    """Fake for the find-store scan + author-memory read sequence.

    `store_name` None → no store for the repo (find returns nothing).
    `author_content` None → author file absent.
    """

    def __init__(self, store_name="air-patterns owner/repo",
                 author_path="/authors/alice.md", author_content="# alice\n- **X** (3x)"):
        self.store_name = store_name
        self.author_path = author_path
        self.author_content = author_content
        self.store_id = "memstore_z"
        self.mem_id = "mem_author"

    def __call__(self, method, path, body=None):
        if method == "GET" and path == "/memory_stores":
            data = []
            if self.store_name is not None:
                data = [{"name": self.store_name, "id": self.store_id}]
            return {"data": data}
        if method == "GET" and "/memories?path_prefix=" in path:
            if self.author_content is None:
                return {"data": []}
            return {"data": [{"type": "memory_metadata",
                              "path": self.author_path, "id": self.mem_id}]}
        if method == "GET" and self.mem_id in path:
            return {"content": self.author_content,
                    "content_sha256": "sha-a", "id": self.mem_id}
        raise AssertionError(f"unexpected call {method} {path}")


def test_read_author_found_prints_content(monkeypatch, capsys):
    monkeypatch.setattr(meta, "_store_api", FakeAuthorAPI())
    rc = meta.main(["read-author", "--repo", "owner/repo", "--login", "alice"])
    assert rc == meta.READ_AUTHOR_FOUND  # 0
    assert "# alice" in capsys.readouterr().out


def test_read_author_absent_is_new_author(monkeypatch):
    # Store exists, but this author has no file → genuinely new author (exit 3).
    monkeypatch.setattr(meta, "_store_api", FakeAuthorAPI(author_content=None))
    rc = meta.main(["read-author", "--repo", "owner/repo", "--login", "bob"])
    assert rc == meta.READ_AUTHOR_ABSENT  # 3


def test_read_author_empty_file_is_new_author(monkeypatch):
    monkeypatch.setattr(meta, "_store_api", FakeAuthorAPI(author_content="   \n"))
    rc = meta.main(["read-author", "--repo", "owner/repo", "--login", "alice"])
    assert rc == meta.READ_AUTHOR_ABSENT  # 3 — present but no patterns


def test_read_author_no_store_is_unknown(monkeypatch):
    # No store for this repo (legacy, or local key sees the wrong workspace) →
    # UNKNOWN (exit 2), NOT "new author" — the CLI must not misreport.
    monkeypatch.setattr(meta, "_store_api", FakeAuthorAPI(store_name=None))
    rc = meta.main(["read-author", "--repo", "owner/repo", "--login", "alice"])
    assert rc == meta.READ_AUTHOR_UNKNOWN  # 2


def test_read_author_allows_bot_login(monkeypatch, capsys):
    # GitHub App authors carry a `[bot]` suffix — must reach their store file.
    api = FakeAuthorAPI(author_path="/authors/dependabot[bot].md",
                        author_content="# dependabot[bot]\n- **deps** (2x)")
    monkeypatch.setattr(meta, "_store_api", api)
    rc = meta.main(["read-author", "--repo", "owner/repo", "--login", "dependabot[bot]"])
    assert rc == meta.READ_AUTHOR_FOUND
    assert "dependabot" in capsys.readouterr().out


def test_read_author_rejects_trailing_hyphen_login(monkeypatch):
    # GitHub disallows a trailing hyphen; the regex must reject it (→ UNKNOWN),
    # not treat it as a valid-but-absent author (which would read as exit 3).
    called = {"n": 0}

    def spy(*a, **k):
        called["n"] += 1
        return {"data": []}
    monkeypatch.setattr(meta, "_store_api", spy)
    rc = meta.main(["read-author", "--repo", "owner/repo", "--login", "alice-"])
    assert rc == meta.READ_AUTHOR_UNKNOWN
    assert called["n"] == 0


def test_read_author_rejects_injection_login(monkeypatch):
    # An injection-y login must be rejected BEFORE any API call (returns UNKNOWN).
    called = {"n": 0}

    def spy(*a, **k):
        called["n"] += 1
        return {"data": []}
    monkeypatch.setattr(meta, "_store_api", spy)
    rc = meta.main(["read-author", "--repo", "owner/repo", "--login", "bob&admin=1"])
    assert rc == meta.READ_AUTHOR_UNKNOWN
    assert called["n"] == 0   # rejected before touching the API


def test_read_author_store_unreachable_is_unknown(monkeypatch):
    # The ai-relay failure mode: the local ANTHROPIC_API_KEY can't reach the
    # store (wrong workspace / revoked) → transport error → UNKNOWN (exit 2).
    def boom(*a, **k):
        raise RuntimeError("401 invalid x-api-key")
    monkeypatch.setattr(meta, "_store_api", boom)
    rc = meta.main(["read-author", "--repo", "owner/repo", "--login", "alice"])
    assert rc == meta.READ_AUTHOR_UNKNOWN  # 2 — never claim "new author" on failure


def test_read_author_no_backend_arg_required():
    # read-author resolves the store from --repo; it must NOT demand --wiki-dir/--store-id.
    import io
    import contextlib
    # Missing --login should still be an argparse error, but --repo alone must
    # pass the backend guard (regression for the main() exemption).
    with contextlib.redirect_stderr(io.StringIO()):
        with pytest.raises(SystemExit):
            meta.main(["read-author", "--repo", "owner/repo"])  # no --login
