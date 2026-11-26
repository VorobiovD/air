"""Data-file classifier — the single place that decides "is this path a data file?".

INERT: nothing imports or calls this module yet. It exists so later changes that
filter data files out of review inputs share ONE definition (list, size rule, caller
globs) instead of growing copies. Importing it has no side effects.

Classes
  * BUILTIN: extensions that are data by nature, regardless of size.
  * AMBIGUOUS: extensions that are usually code/config but can also be a data
    export (`.json`, `.xml`, `.txt`, `.yaml`/`.yml`, `.dat`, `.ndjson`, `.jsonl`,
    `.bak`). They count as data ONLY for a whole-file add/delete of at least
    `AIR_DATA_AMBIGUOUS_MIN_BYTES` (default 100000) — a normal edit to a config file
    is never classified as data.
  * `.sql` is data only when the file name looks like a dump/backup/export
    (migrations and schema files are code).
  * `.bak` is AMBIGUOUS rather than builtin: it is a backup of *something*, often an
    old copy of source; the size gate separates the two.

Caller control
  * `AIR_DATA_GLOBS`       — extra paths always treated as data.
  * `AIR_DATA_ALLOW_GLOBS` — paths never treated as data (wins over everything else).
  Both are comma- or newline-separated fnmatch globs, matched case-insensitively
  against the full path and the basename (`*` crosses `/`).

stdlib-only (the lib/ rule); env parsing reuses the tolerant helpers in env.py.
"""
import fnmatch
import os
import re
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env import env_int  # noqa: E402

BUILTIN_DATA_EXTS = frozenset({
    ".csv", ".tsv", ".xlsx", ".xls", ".parquet", ".avro", ".feather",
    ".pkl", ".pickle", ".db", ".sqlite", ".sqlite3", ".dump",
})

AMBIGUOUS_DATA_EXTS = frozenset({
    ".json", ".xml", ".txt", ".yaml", ".yml", ".dat", ".ndjson", ".jsonl", ".bak",
})

_SQL_DUMP_NAME_RE = re.compile(r"(dump|backup|export)", re.IGNORECASE)

DEFAULT_AMBIGUOUS_MIN_BYTES = 100_000


def parse_globs(raw: str | None) -> tuple[str, ...]:
    """Split a comma/newline separated glob list; blanks and `#` comment lines
    are dropped. Never raises."""
    if not raw:
        return ()
    out = []
    for part in re.split(r"[,\n]", raw):
        part = part.strip()
        if part and not part.startswith("#"):
            out.append(part)
    return tuple(out)


def env_data_globs() -> tuple[str, ...]:
    """Caller-declared extra data globs (`AIR_DATA_GLOBS`)."""
    return parse_globs(os.environ.get("AIR_DATA_GLOBS"))


def env_allow_globs() -> tuple[str, ...]:
    """Caller-declared never-data globs (`AIR_DATA_ALLOW_GLOBS`)."""
    return parse_globs(os.environ.get("AIR_DATA_ALLOW_GLOBS"))


def ambiguous_min_bytes() -> int:
    """Whole-file size from which an ambiguous type counts as data
    (`AIR_DATA_AMBIGUOUS_MIN_BYTES`, default 100000; a typo falls back, never raises)."""
    return env_int("AIR_DATA_AMBIGUOUS_MIN_BYTES", DEFAULT_AMBIGUOUS_MIN_BYTES, minimum=0)


def _glob_hit(path: str, globs: Iterable[str]) -> bool:
    low = path.lower()
    base = os.path.basename(low)
    return any(fnmatch.fnmatch(low, g.lower()) or fnmatch.fnmatch(base, g.lower())
               for g in globs)


def is_data_path(path: str, size: int | None = None, whole_file: bool = False,
                 extra_globs: Iterable[str] = (), allow_globs: Iterable[str] = (),
                 ambiguous_min: int | None = None) -> bool:
    """True if `path` should be treated as a data file.

    `size` / `whole_file` describe the change under test: the byte size of the
    file's content and whether the whole file was added or deleted. They only
    matter for the ambiguous types. `ambiguous_min` overrides the env threshold.
    Order: allow-globs (never data) > extra-globs (always data) > built-in rules.
    """
    if not path:
        return False
    if _glob_hit(path, allow_globs):
        return False
    if _glob_hit(path, extra_globs):
        return True
    base = os.path.basename(path).lower()
    ext = os.path.splitext(base)[1]
    if ext in BUILTIN_DATA_EXTS:
        return True
    if ext == ".sql":
        return bool(_SQL_DUMP_NAME_RE.search(base))
    if ext in AMBIGUOUS_DATA_EXTS:
        if not whole_file or size is None:
            return False
        floor = ambiguous_min_bytes() if ambiguous_min is None else ambiguous_min
        return size >= floor
    return False
