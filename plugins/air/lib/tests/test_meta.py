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
