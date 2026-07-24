"""Unit tests for meta.py — threshold logic, round-trip, edge cases."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LIB = HERE.parent
sys.path.insert(0, str(LIB))

import meta  # noqa: E402


@pytest.fixture
def wiki_dir(tmp_path):
    """Empty directory standing in for a freshly-cloned wiki."""
    return tmp_path


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write(wiki, data):
    (wiki / meta.META_FILENAME).write_text(json.dumps(data))


def _read(wiki):
    return json.loads((wiki / meta.META_FILENAME).read_text())


# -------- read_meta / write_meta ----------------------------------------

def test_read_meta_missing_returns_defaults(wiki_dir):
    out = meta.read_meta(wiki_dir)
    assert out["reviews_since"] == 0
    assert out["last_processed_pr"] == 0
    assert "last_cleanup" in out and "last_check" in out


def test_read_meta_tolerates_unknown_fields(wiki_dir):
    _write(wiki_dir, {
        "last_cleanup": "2026-04-01T00:00:00Z",
        "last_check": "2026-04-01T00:00:00Z",
        "reviews_since": 3,
        "last_processed_pr": 10,
        "future_field": "ignored",
    })
    out = meta.read_meta(wiki_dir)
    assert out["reviews_since"] == 3
    assert "future_field" not in out  # silently dropped


def test_read_meta_tolerates_malformed_json(wiki_dir):
    (wiki_dir / meta.META_FILENAME).write_text("not-json{")
    out = meta.read_meta(wiki_dir)
    assert out["reviews_since"] == 0  # falls back to defaults


def test_roundtrip(wiki_dir):
    original = {
        "last_cleanup": "2026-04-01T00:00:00Z",
        "last_check": "2026-04-02T00:00:00Z",
        "reviews_since": 4,
        "last_processed_pr": 42,
        "last_mirror_render": "2026-04-02T01:00:00Z",
        "learn_claimed_at": "",
    }
    _write(wiki_dir, original)
    m = meta.read_meta(wiki_dir)
    meta.write_meta(wiki_dir, m)
    assert _read(wiki_dir) == original


# -------- should_trigger_learn: all four branches -----------------------

@pytest.fixture
def now():
    return datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)


def test_trigger_count_threshold(now):
    m = {
        "last_cleanup": _iso(now - timedelta(hours=1)),
        "last_check": _iso(now),
        "reviews_since": 15,
        "last_processed_pr": 30,
    }
    trigger, reason = meta.should_trigger_learn(m, now=now)
    assert trigger is True
    assert "reviews_since=15" in reason


def test_reviews_threshold_default_is_15():
    """Unset AIR_LEARN_REVIEWS_THRESHOLD → 15, i.e. byte-identical to pre-knob
    behavior for every caller that doesn't opt in."""
    assert meta.REVIEWS_THRESHOLD == 15


@pytest.mark.parametrize("raw,expected", [("10", 10), ("25", 25), ("1", 1)])
def test_reviews_threshold_env_override(monkeypatch, raw, expected):
    """AIR_LEARN_REVIEWS_THRESHOLD retunes the cadence without a code change."""
    import importlib
    monkeypatch.setenv("AIR_LEARN_REVIEWS_THRESHOLD", raw)
    reloaded = importlib.reload(meta)
    try:
        assert reloaded.REVIEWS_THRESHOLD == expected
        m = {"last_cleanup": _iso(datetime(2026, 4, 24, 11, tzinfo=timezone.utc)),
             "last_check": _iso(datetime(2026, 4, 24, 12, tzinfo=timezone.utc)),
             "reviews_since": expected, "last_processed_pr": 30}
        trigger, reason = reloaded.should_trigger_learn(
            m, now=datetime(2026, 4, 24, 12, tzinfo=timezone.utc))
        assert trigger is True
        assert f">= {expected}" in reason
    finally:
        monkeypatch.delenv("AIR_LEARN_REVIEWS_THRESHOLD", raising=False)
        importlib.reload(meta)   # restore the default for sibling tests


def test_reviews_threshold_env_typo_falls_back_to_default(monkeypatch):
    """A typo'd value must NOT crash the counter step (env.env_int is tolerant)
    and must NOT silently resolve to something surprising — fall back to 15."""
    import importlib
    monkeypatch.setenv("AIR_LEARN_REVIEWS_THRESHOLD", "ten")
    reloaded = importlib.reload(meta)
    try:
        assert reloaded.REVIEWS_THRESHOLD == 15
    finally:
        monkeypatch.delenv("AIR_LEARN_REVIEWS_THRESHOLD", raising=False)
        importlib.reload(meta)


def test_below_a_lowered_threshold_still_skips(monkeypatch):
    """Lowering the threshold must not make everything due — 9 < 10 still skips."""
    import importlib
    monkeypatch.setenv("AIR_LEARN_REVIEWS_THRESHOLD", "10")
    reloaded = importlib.reload(meta)
    try:
        n = datetime(2026, 4, 24, 12, tzinfo=timezone.utc)
        m = {"last_cleanup": _iso(n - timedelta(days=1)), "last_check": _iso(n),
             "reviews_since": 9, "last_processed_pr": 30}
        trigger, _ = reloaded.should_trigger_learn(m, now=n)
        assert trigger is False
    finally:
        monkeypatch.delenv("AIR_LEARN_REVIEWS_THRESHOLD", raising=False)
        importlib.reload(meta)


def test_trigger_count_threshold_exceeded(now):
    m = {"last_cleanup": _iso(now), "last_check": _iso(now), "reviews_since": 22, "last_processed_pr": 30}
    trigger, _ = meta.should_trigger_learn(m, now=now)
    assert trigger is True


def test_trigger_date_with_reviews(now):
    m = {
        "last_cleanup": _iso(now - timedelta(days=15)),
        "last_check": _iso(now - timedelta(days=15)),
        "reviews_since": 2,
        "last_processed_pr": 30,
    }
    trigger, reason = meta.should_trigger_learn(m, now=now)
    assert trigger is True
    assert "days_since_cleanup=15" in reason


def test_skip_date_without_reviews(now):
    m = {
        "last_cleanup": _iso(now - timedelta(days=20)),
        "last_check": _iso(now - timedelta(days=20)),
        "reviews_since": 0,
        "last_processed_pr": 30,
    }
    trigger, reason = meta.should_trigger_learn(m, now=now)
    assert trigger is False
    assert "reviews_since=0" in reason


def test_skip_below_all_thresholds(now):
    # 6 days would have fired the old 2-day rule — regression guard that the
    # days backstop no longer triggers on every low-traffic review.
    m = {
        "last_cleanup": _iso(now - timedelta(days=6)),
        "last_check": _iso(now - timedelta(days=6)),
        "reviews_since": 2,
        "last_processed_pr": 30,
    }
    trigger, _ = meta.should_trigger_learn(m, now=now)
    assert trigger is False


def test_trigger_at_exact_boundary_15_reviews(now):
    m = {"last_cleanup": _iso(now), "last_check": _iso(now), "reviews_since": 15, "last_processed_pr": 30}
    trigger, _ = meta.should_trigger_learn(m, now=now)
    assert trigger is True  # >= 15, not > 15


def test_skip_just_below_reviews_boundary(now):
    m = {
        "last_cleanup": _iso(now - timedelta(days=1)),
        "last_check": _iso(now),
        "reviews_since": 14,
        "last_processed_pr": 30,
    }
    trigger, _ = meta.should_trigger_learn(m, now=now)
    assert trigger is False  # 14 < 15, and days below the backstop


def test_trigger_at_exact_boundary_14_days(now):
    m = {
        "last_cleanup": _iso(now - timedelta(days=14, seconds=1)),
        "last_check": _iso(now),
        "reviews_since": 1,
        "last_processed_pr": 30,
    }
    trigger, _ = meta.should_trigger_learn(m, now=now)
    assert trigger is True


# -------- CLI subcommands -----------------------------------------------

def test_cmd_bump_creates_file_from_defaults(wiki_dir):
    rc = meta.main(["bump", "--wiki-dir", str(wiki_dir), "--pr-number", "42"])
    assert rc == 0
    data = _read(wiki_dir)
    assert data["reviews_since"] == 1
    assert data["last_processed_pr"] == 42


def test_cmd_bump_increments(wiki_dir):
    _write(wiki_dir, {
        "last_cleanup": "2026-04-01T00:00:00Z",
        "last_check": "2026-04-01T00:00:00Z",
        "reviews_since": 3,
        "last_processed_pr": 10,
    })
    meta.main(["bump", "--wiki-dir", str(wiki_dir), "--pr-number", "11"])
    data = _read(wiki_dir)
    assert data["reviews_since"] == 4
    assert data["last_processed_pr"] == 11


def test_cmd_bump_doesnt_go_backwards(wiki_dir):
    _write(wiki_dir, {
        "last_cleanup": "2026-04-01T00:00:00Z",
        "last_check": "2026-04-01T00:00:00Z",
        "reviews_since": 3,
        "last_processed_pr": 50,
    })
    # Smaller PR number passed — should keep existing higher one.
    meta.main(["bump", "--wiki-dir", str(wiki_dir), "--pr-number", "5"])
    assert _read(wiki_dir)["last_processed_pr"] == 50


def test_cmd_check_returns_1_on_trigger(wiki_dir):
    _write(wiki_dir, {
        "last_cleanup": "2026-04-01T00:00:00Z",
        "last_check": "2026-04-01T00:00:00Z",
        "reviews_since": 15,
        "last_processed_pr": 30,
    })
    rc = meta.main(["check", "--wiki-dir", str(wiki_dir)])
    assert rc == 1


def test_cmd_check_returns_0_on_skip(wiki_dir):
    _write(wiki_dir, {
        "last_cleanup": datetime.now(timezone.utc).isoformat(),
        "last_check": datetime.now(timezone.utc).isoformat(),
        "reviews_since": 1,
        "last_processed_pr": 30,
    })
    rc = meta.main(["check", "--wiki-dir", str(wiki_dir)])
    assert rc == 0


def test_cmd_check_bumps_last_check_on_skip_with_zero_reviews(wiki_dir):
    """Date passed but 0 reviews → skip + advance last_check so we don't
    re-evaluate on every review."""
    old_check = "2026-04-01T00:00:00Z"
    _write(wiki_dir, {
        "last_cleanup": old_check,
        "last_check": old_check,
        "reviews_since": 0,
        "last_processed_pr": 30,
    })
    rc = meta.main(["check", "--wiki-dir", str(wiki_dir)])
    assert rc == 0
    new_check = _read(wiki_dir)["last_check"]
    assert new_check != old_check  # bumped to now
    # reviews_since stays at 0; last_cleanup stays at old value.
    assert _read(wiki_dir)["reviews_since"] == 0
    assert _read(wiki_dir)["last_cleanup"] == old_check


def test_cmd_check_doesnt_bump_on_below_threshold(wiki_dir):
    """Below both thresholds → skip, last_check NOT bumped (still useful as
    the last 'we actually evaluated' timestamp for the zero-review case,
    but for not-yet-stale state it's a no-op)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    _write(wiki_dir, {
        "last_cleanup": now_iso,
        "last_check": now_iso,
        "reviews_since": 1,
        "last_processed_pr": 30,
    })
    meta.main(["check", "--wiki-dir", str(wiki_dir)])
    # Nothing mutated.
    data = _read(wiki_dir)
    assert data["last_check"] == now_iso
    assert data["reviews_since"] == 1


def test_cmd_reset_zeros_counter_and_advances_cleanup(wiki_dir):
    _write(wiki_dir, {
        "last_cleanup": "2026-04-01T00:00:00Z",
        "last_check": "2026-04-01T00:00:00Z",
        "reviews_since": 8,
        "last_processed_pr": 10,
    })
    rc = meta.main(["reset", "--wiki-dir", str(wiki_dir), "--pr-number", "50"])
    assert rc == 0
    data = _read(wiki_dir)
    assert data["reviews_since"] == 0
    assert data["last_cleanup"] != "2026-04-01T00:00:00Z"
    assert data["last_processed_pr"] == 50


# -------- end-to-end via subprocess (confirms shebang + argparse) -------

def test_script_is_directly_invocable(wiki_dir):
    """Matches how review.md will invoke: `python3 "$LIB/meta.py" bump ...`"""
    script = LIB / "meta.py"
    result = subprocess.run(
        [sys.executable, str(script), "bump", "--wiki-dir", str(wiki_dir), "--pr-number", "1"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert _read(wiki_dir)["reviews_since"] == 1


# -------- claim: atomic bump + learn-slot lock (anti-storm) --------------

def test_cmd_claim_bumps_and_skips_below_threshold(wiki_dir):
    _write(wiki_dir, {
        "last_cleanup": _iso(datetime.now(timezone.utc)),
        "last_check": _iso(datetime.now(timezone.utc)),
        "reviews_since": 3, "last_processed_pr": 10,
    })
    rc = meta.main(["claim", "--wiki-dir", str(wiki_dir), "--pr-number", "11"])
    data = _read(wiki_dir)
    assert rc == 0                       # below threshold → no claim
    assert data["reviews_since"] == 4    # but still counted
    assert not data.get("learn_claimed_at")  # no lock


def test_cmd_claim_claims_on_crossing_threshold(wiki_dir):
    _write(wiki_dir, {
        "last_cleanup": _iso(datetime.now(timezone.utc)),
        "last_check": _iso(datetime.now(timezone.utc)),
        "reviews_since": 14, "last_processed_pr": 10,
    })
    rc = meta.main(["claim", "--wiki-dir", str(wiki_dir), "--pr-number", "11"])
    data = _read(wiki_dir)
    assert rc == 1                       # 14→15 crosses → CLAIMED
    assert data["reviews_since"] == 15
    assert data["learn_claimed_at"]      # lock acquired


def test_cmd_claim_skips_when_lock_live(wiki_dir):
    # Threshold met AND a learn is already in flight (fresh lock) → skip,
    # but still count this review. This is the anti-storm guarantee.
    now = datetime.now(timezone.utc)
    _write(wiki_dir, {
        "last_cleanup": _iso(now), "last_check": _iso(now),
        "reviews_since": 20, "last_processed_pr": 10,
        "learn_claimed_at": _iso(now),   # someone is learning right now
    })
    rc = meta.main(["claim", "--wiki-dir", str(wiki_dir), "--pr-number", "11"])
    data = _read(wiki_dir)
    assert rc == 0                       # lock held → no second learn
    assert data["reviews_since"] == 21   # review still counts
    assert data["learn_claimed_at"] == _iso(now)  # lock untouched


def test_cmd_claim_reclaims_when_lock_stale(wiki_dir):
    # A learn that died without resetting leaves a stale lock; past the TTL the
    # next review re-claims (self-healing — never wedged).
    now = datetime.now(timezone.utc)
    stale = now - timedelta(minutes=meta.LEARN_LOCK_TTL_MIN + 5)
    _write(wiki_dir, {
        "last_cleanup": _iso(now), "last_check": _iso(now),
        "reviews_since": 20, "last_processed_pr": 10,
        "learn_claimed_at": _iso(stale),
    })
    rc = meta.main(["claim", "--wiki-dir", str(wiki_dir), "--pr-number", "11"])
    data = _read(wiki_dir)
    assert rc == 1                                   # stale lock → re-claim
    assert _iso(stale) != data["learn_claimed_at"]   # lock refreshed


def test_cmd_reset_clears_the_learn_lock(wiki_dir):
    now = datetime.now(timezone.utc)
    _write(wiki_dir, {
        "last_cleanup": "2026-04-01T00:00:00Z", "last_check": "2026-04-01T00:00:00Z",
        "reviews_since": 16, "last_processed_pr": 10,
        "learn_claimed_at": _iso(now),
    })
    meta.main(["reset", "--wiki-dir", str(wiki_dir), "--pr-number", "50"])
    data = _read(wiki_dir)
    assert data["reviews_since"] == 0
    assert data["learn_claimed_at"] == ""   # lock released


def test_learn_lock_live_states():
    now = datetime.now(timezone.utc)
    assert meta._learn_lock_live({"learn_claimed_at": ""}) is False
    assert meta._learn_lock_live({}) is False
    assert meta._learn_lock_live({"learn_claimed_at": "garbage"}) is False
    assert meta._learn_lock_live({"learn_claimed_at": _iso(now)}) is True
    old = now - timedelta(minutes=meta.LEARN_LOCK_TTL_MIN + 1)
    assert meta._learn_lock_live({"learn_claimed_at": _iso(old)}) is False


def test_claim_then_reset_then_claim_cycle(wiki_dir):
    # Full cadence: cross → claim (lock) → reset (clear) → next review bumps
    # from 0 and does NOT re-fire while below threshold.
    now = datetime.now(timezone.utc)
    _write(wiki_dir, {
        "last_cleanup": _iso(now), "last_check": _iso(now),
        "reviews_since": 14, "last_processed_pr": 10,
    })
    assert meta.main(["claim", "--wiki-dir", str(wiki_dir), "--pr-number", "11"]) == 1
    assert meta.main(["reset", "--wiki-dir", str(wiki_dir), "--pr-number", "11"]) == 0
    assert _read(wiki_dir)["reviews_since"] == 0
    assert meta.main(["claim", "--wiki-dir", str(wiki_dir), "--pr-number", "12"]) == 0
    assert _read(wiki_dir)["reviews_since"] == 1


def test_learn_lock_ttl_exceeds_learn_runtime():
    """H1: the anti-storm lock TTL must exceed the learn runtime, or the lock
    ages out mid-learn and a concurrent review re-fires it (the storm). The old
    flat 20-min TTL sat below the 25-min AIR_LEARN_TIMEOUT_S. Checked in a fresh
    process per env so the import-time constant reflects that env."""
    def ttl(timeout_env):
        e = dict(os.environ)
        e.pop("AIR_LEARN_TIMEOUT_S", None)
        if timeout_env is not None:
            e["AIR_LEARN_TIMEOUT_S"] = timeout_env
        out = subprocess.run(
            [sys.executable, "-c", "import meta; print(meta.LEARN_LOCK_TTL_MIN)"],
            cwd=str(LIB), env=e, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return int(out.stdout.strip())

    # default (1500s / 25min) → floored at 40min; the TTL in seconds exceeds it
    assert ttl(None) == 40
    assert ttl(None) * 60 >= 1500
    # a longer configured timeout scales the TTL above it (+10min margin)
    assert ttl("3000") == 60                 # 3000//60 + 10
    assert ttl("3000") * 60 >= 3000
    # a garbage timeout must NOT crash the import → falls back to 1500 → 40
    assert ttl("nope") == 40
