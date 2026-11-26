"""Unit tests for data_rule — the (currently inert) data-file classifier."""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import data_rule  # noqa: E402
from data_rule import is_data_path, parse_globs  # noqa: E402


@pytest.mark.parametrize("path", [
    "data/export.csv", "a/b/table.TSV", "report.xlsx", "old.xls", "x.parquet", "x.avro",
    "x.feather", "m.pkl", "m.pickle", "app.db", "app.sqlite", "app.sqlite3", "snapshot.dump",
])
def test_builtin_data_types_are_data_at_any_size(path):
    assert is_data_path(path)
    assert is_data_path(path, size=10, whole_file=False)


@pytest.mark.parametrize("path", ["src/app.py", "README.md", "web/index.tsx", "Makefile", ""])
def test_code_and_docs_are_not_data(path):
    assert not is_data_path(path, size=10_000_000, whole_file=True)


def test_sql_is_data_only_when_it_looks_like_a_dump():
    assert is_data_path("db/prod_dump.sql")
    assert is_data_path("backup-2024.sql")
    assert is_data_path("export/users_export.SQL")
    assert not is_data_path("migrations/0001_create_users.sql")
    assert not is_data_path("schema.sql", size=10_000_000, whole_file=True)


@pytest.mark.parametrize("path", [
    "conf.json", "feed.xml", "notes.txt", "values.yaml", "values.yml", "blob.dat",
    "rows.ndjson", "rows.jsonl", "copy.bak",
])
def test_ambiguous_types_need_a_large_whole_file_change(path, monkeypatch):
    monkeypatch.delenv("AIR_DATA_AMBIGUOUS_MIN_BYTES", raising=False)
    assert not is_data_path(path)                                   # no size info
    assert not is_data_path(path, size=5_000_000, whole_file=False)  # an edit, not add/delete
    assert not is_data_path(path, size=99_999, whole_file=True)      # under the default floor
    assert is_data_path(path, size=100_000, whole_file=True)         # at the default floor


def test_ambiguous_threshold_env_and_override(monkeypatch):
    monkeypatch.setenv("AIR_DATA_AMBIGUOUS_MIN_BYTES", "500")
    assert data_rule.ambiguous_min_bytes() == 500
    assert is_data_path("a.json", size=500, whole_file=True)
    assert not is_data_path("a.json", size=499, whole_file=True)
    assert is_data_path("a.json", size=10, whole_file=True, ambiguous_min=5)
    monkeypatch.setenv("AIR_DATA_AMBIGUOUS_MIN_BYTES", "not-a-number")
    assert data_rule.ambiguous_min_bytes() == data_rule.DEFAULT_AMBIGUOUS_MIN_BYTES


def test_parse_globs_separators_and_comments():
    assert parse_globs("a/*.x, b/**\nc*\n\n  # note\n") == ("a/*.x", "b/**", "c*")
    assert parse_globs("") == () and parse_globs(None) == ()


def test_env_glob_helpers(monkeypatch):
    monkeypatch.setenv("AIR_DATA_GLOBS", "fixtures/*, *.rec")
    monkeypatch.setenv("AIR_DATA_ALLOW_GLOBS", "fixtures/keep.csv")
    assert data_rule.env_data_globs() == ("fixtures/*", "*.rec")
    assert data_rule.env_allow_globs() == ("fixtures/keep.csv",)
    monkeypatch.delenv("AIR_DATA_GLOBS")
    monkeypatch.delenv("AIR_DATA_ALLOW_GLOBS")
    assert data_rule.env_data_globs() == () and data_rule.env_allow_globs() == ()


def test_caller_globs_extra_and_allow():
    extra = ("fixtures/*", "*.rec")
    assert is_data_path("fixtures/a.txt", extra_globs=extra)
    assert is_data_path("deep/x.REC", extra_globs=extra)
    assert not is_data_path("src/app.py", extra_globs=extra)
    # allow wins over extra AND over a built-in type
    assert not is_data_path("fixtures/keep.csv", extra_globs=extra, allow_globs=("fixtures/keep.csv",))
    assert not is_data_path("sample.csv", allow_globs=("sample.csv",))
    assert not is_data_path("tests/sample.csv", allow_globs=("*sample.csv",))


def test_module_is_inert():
    # nothing in the plugin imports data_rule yet; guard against accidental wiring
    lib = HERE.parent
    users = [p.name for p in lib.glob("*.py")
             if p.name != "data_rule.py" and "data_rule" in p.read_text()]
    assert users == []
