#!/usr/bin/env python3
"""THE diff-hygiene contract — generated/vendored stubbing + global size cap.

One implementation, every path (the verdict.py / agent_md.py / meta.py anti-drift
pattern). Stubs minified bundles, sourcemaps, snapshots, dist/vendor/node_modules
segments, and lockfiles whose same-dir manifest also changed; then enforces a
byte cap with explicit in-diff markers (nothing dropped silently). Agents never
see the stubbed bodies — it's PURE cost (and a load-bearing parity guarantee for
the re-review ledger, whose UNCHANGED determination must see the same diff shape
on the CLI and managed).

Consumers:
- managed: `github_client.py` re-exports these (fetch_pr_diff/fetch_inter_diff
  apply them inside the fetchers); review.py uses count_diff_changed_lines.
- CLI: `/air:review` pipes its diff files through `python3 lib/diff_hygiene.py
  --diff-file <path>` (the analogue of the managed in-fetcher hygiene).

stdlib-only (re, os, sys).
"""
import os
import re
import sys

DIFF_MAX_BYTES = int(os.environ.get("AIR_DIFF_MAX_BYTES", "500000"))
# Cap-marker line prefix. review.py keys codex-skip off it: a truncated
# re-review delta must NOT skip codex (real changes may live in the
# omitted tail, and codex reads the git tree, not this diff). Detection is
# LINE-START anchored at the consumer — diff body lines always start with
# `+`/`-`/space, so PR content cannot forge a line beginning with this.
DIFF_TRUNCATION_MARKER = "[air: diff truncated"

# Lockfile → the manifest whose same-directory change justifies stubbing.
_LOCKFILE_MANIFESTS = {
    "package-lock.json": "package.json",
    "yarn.lock": "package.json",
    "pnpm-lock.yaml": "package.json",
    "bun.lock": "package.json",
    "bun.lockb": "package.json",
    "composer.lock": "composer.json",
    "Cargo.lock": "Cargo.toml",
    "poetry.lock": "pyproject.toml",
    "uv.lock": "pyproject.toml",
    "go.sum": "go.mod",
    "Gemfile.lock": "Gemfile",
}
_GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", ".snap")
# Whole-segment match only (`dist` matches `pkg/dist/x.js`, not
# `src/distance.py`). `build/` is deliberately absent — it collides with
# committed source in too many layouts.
_GENERATED_SEGMENTS = {"dist", "node_modules", "__snapshots__", "vendor"}


def _is_generated_path(path: str) -> bool:
    if not path:
        return False
    basename = path.rsplit("/", 1)[-1]
    if basename in _LOCKFILE_MANIFESTS:
        return True
    if basename.endswith(_GENERATED_SUFFIXES):
        return True
    return any(seg in _GENERATED_SEGMENTS for seg in path.split("/")[:-1])


def _segment_path(segment: str) -> str:
    """The b/-side path from a `diff --git a/x b/x` header (rename-safe)."""
    header = segment.splitlines()[0] if segment else ""
    return header.rsplit(" b/", 1)[-1] if " b/" in header else ""


def count_diff_changed_lines(diff: str) -> int:
    """Count added/removed lines in a unified diff (excl. +++/--- headers).

    The one shared sizing metric: promote overlap, codex-skip, and hygiene
    stub counts all use this definition (review.py re-exports it)."""
    n = 0
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            n += 1
    return n


def _stub_decision(path: str, changed_paths: set) -> bool:
    """Should this generated-classified path actually be stubbed?

    Lockfiles: only when the paired manifest in the SAME directory also
    changed — dependency-bump noise gets stubbed, a lockfile-only change
    (the supply-chain evasion shape) stays fully reviewable. All other
    generated-classified paths are stubbed unconditionally."""
    basename = path.rsplit("/", 1)[-1]
    manifest = _LOCKFILE_MANIFESTS.get(basename)
    if manifest is None:
        return True
    prefix = path[: -len(basename)]  # "" at root, "dir/" otherwise
    return f"{prefix}{manifest}" in changed_paths


def _should_stub(path: str, changed_paths: set) -> bool:
    """The complete stubbing decision — classification AND lockfile pairing.

    Callers outside apply_diff_hygiene must use this, not bare
    `_is_generated_path` (which says "stubbing candidate", not "stub it":
    lockfiles classify as generated but only stub when their same-dir
    manifest also changed)."""
    return _is_generated_path(path) and _stub_decision(path, changed_paths)


def apply_diff_hygiene(diff: str, *, max_bytes: int | None = None) -> str:
    """Stub generated-file segments, then enforce the global size cap.

    Both transformations leave an explicit in-diff marker (and a stdout
    decision-log line), so reviewers — human and agent — can always see
    what was omitted. Nothing is dropped silently.
    """
    if not diff:
        return diff
    budget = DIFF_MAX_BYTES if max_bytes is None else max_bytes
    segments = re.split(r"(?m)^(?=diff --git )", diff)
    paths = [_segment_path(s) for s in segments]
    changed_paths = {
        p for s, p in zip(segments, paths) if s.startswith("diff --git ")
    }
    kept: list = []
    kept_paths: list = []
    for seg, path in zip(segments, paths):
        if not seg.startswith("diff --git ") or not _should_stub(path, changed_paths):
            kept.append(seg)
            kept_paths.append(path)
            continue
        n = count_diff_changed_lines(seg)
        header = seg.splitlines()[0]
        kept.append(
            f"{header}\n[air: {path}: {n} changed lines omitted "
            f"(generated/vendored)]\n"
        )
        kept_paths.append(path)
        print(f"  diff hygiene: stubbed {path} ({n} changed lines)")
    result = "".join(kept)
    if len(result.encode("utf-8", errors="replace")) <= budget:
        return result

    def _marker(show_paths: list, n_omitted: int) -> str:
        # Paths are tail-truncated to 60 chars so 5 of them can't blow the
        # budget the marker exists to enforce.
        shown = ", ".join(p[-60:] for p in show_paths)
        extra = n_omitted - len(show_paths)
        suffix = f", … +{extra} more" if extra > 0 else ""
        named = f": {shown}{suffix}" if show_paths else ""
        return (
            f"{DIFF_TRUNCATION_MARKER} at {budget} bytes — "
            f"{n_omitted} file(s) omitted{named}]\n"
        )

    # Greedy first-fit at file boundaries: a single oversized segment is
    # omitted on its own — it must not drag down the (possibly small)
    # segments after it. The selection reserves room for the largest
    # marker we could emit (5 fully-truncated paths), then the marker
    # shrinks its shown-path list until the whole result fits. Guarantee:
    # output ≤ budget whenever budget ≥ the path-less marker (~80 bytes);
    # below that floor the marker is emitted anyway — visibility beats a
    # degenerate cap.
    capped: list = []
    omitted: list = []
    used = 0
    reserve = len(_marker(["x" * 60] * 5, len(kept)).encode("utf-8", errors="replace"))
    limit = max(0, budget - reserve)
    for seg, path in zip(kept, kept_paths):
        size = len(seg.encode("utf-8", errors="replace"))
        if used + size <= limit:
            capped.append(seg)
            used += size
        else:
            omitted.append(path or "(preamble)")
    # A cap-omitted LOCKFILE gets its own loud marker: the stub gate
    # deliberately kept it whole (lockfile-only = the supply-chain attack
    # shape), so silently folding it into the generic count would blind
    # the security lens to exactly the shape the lockfile exception
    # protects. The dedicated line tells the checklist to flag the gap.
    lockfile_markers = "".join(
        f"[air: LOCKFILE {p[-60:]} omitted by the size cap — supply-chain "
        f"review incomplete; fetch its diff manually]\n"
        for p in omitted
        if p.rsplit("/", 1)[-1] in _LOCKFILE_MANIFESTS
    )
    used += len(lockfile_markers.encode("utf-8", errors="replace"))
    for n_shown in (5, 4, 3, 2, 1, 0):
        marker = _marker(omitted[:n_shown], len(omitted))
        if used + len(marker.encode("utf-8", errors="replace")) <= budget:
            break
    capped.append(lockfile_markers)
    capped.append(marker)
    print(
        f"  [warn] diff hygiene: {len(omitted)} file segment(s) over the "
        f"{budget}-byte cap omitted: {', '.join(omitted[:5])}"
        f"{'' if len(omitted) <= 5 else f', … +{len(omitted) - 5} more'}",
        file=sys.stderr,
    )
    return "".join(capped)


def _main(argv: list) -> int:
    """Thin CLI: hygiene a diff file IN PLACE (the CLI review path's analogue of
    the managed in-fetcher hygiene). Decision-log lines go to stdout/stderr; the
    hygiene'd diff overwrites the file so the bash caller needs no capture."""
    import argparse
    ap = argparse.ArgumentParser(description="Apply air diff-hygiene to a diff file in place.")
    ap.add_argument("--diff-file", required=True, help="path to a unified-diff file to hygiene in place")
    ap.add_argument("--max-bytes", type=int, default=None, help="byte cap (default AIR_DIFF_MAX_BYTES/500000)")
    args = ap.parse_args(argv)
    try:
        with open(args.diff_file, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as e:
        print(f"diff_hygiene: cannot read {args.diff_file}: {e}", file=sys.stderr)
        return 1
    cleaned = apply_diff_hygiene(raw, max_bytes=args.max_bytes)
    with open(args.diff_file, "w", encoding="utf-8") as fh:
        fh.write(cleaned)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
