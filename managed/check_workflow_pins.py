#!/usr/bin/env python3
"""Structural check that a pinned caller's SHA pins air's CODE, not just the YAML.

Both reusable workflows (`managed-review.yml`, `air-learn-cron.yml`) check out
air's own Python separately from the caller's `uses:` line. That checkout must
run the commit the workflow file was loaded from (`job.workflow_sha`), with
`air_ref` as an empty-default override; otherwise a caller pinned to a SHA
freezes only the YAML while the engine floats on `main`.

The first version of this guard (`.air-checks.sh` Check L) searched for
strings anywhere in the file, comments included, and an audit defeated it four
ways: an unquoted `default: main`, `github.workflow_sha` in place of
`job.workflow_sha`, `ref: main` with the expected expression kept in a comment,
and a resolver that assigns `ref="main"`. This module asserts structure
instead, with comments stripped, and it EXECUTES the resolver step to check
what ref it actually produces.

Stdlib only (runs in the drift-check job with no dependencies). Usage:
    python3 managed/check_workflow_pins.py .github/workflows/managed-review.yml ...
Exit 1 with one line per violation.
"""
import os
import re
import subprocess
import sys
import tempfile

RESOLVER_ID = "air_code"
RESOLVED_REF = "${{ steps.air_code.outputs.ref }}"
EXPECTED_ENV = {
    "OVERRIDE": "${{ inputs.air_ref }}",
    "WF_SHA": "${{ job.workflow_sha }}",
}
AIR_REPO = "VorobiovD/air"
SAMPLE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _strip_comment(line: str) -> str:
    """Drop a YAML comment. A full-line comment becomes empty; a trailing
    ` # ...` is cut unless the `#` sits inside quotes. Block-scalar (`run: |`)
    bodies are shell, where the same rule holds for these workflows."""
    if line.lstrip().startswith("#"):
        return ""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and i > 0 and line[i - 1] in " \t":
            return line[:i].rstrip()
    return line.rstrip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _steps(lines: list) -> list:
    """Split comment-stripped lines into step blocks: each starts at a `- `
    list item and runs until the next line at the same or a lower indent."""
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^(\s*)- \S", ln)
        if m:
            ind = len(m.group(1))
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() and _indent(nxt) <= ind:
                    break
                j += 1
            out.append(lines[i:j])
            i = j
        else:
            i += 1
    return out


def _value(block: list, key: str):
    """Value of `key:` anywhere in a step block (first match), or None."""
    for ln in block:
        m = re.match(r"^\s*(?:- )?" + re.escape(key) + r":\s*(.*)$", ln)
        if m:
            return m.group(1).strip()
    return None


def _run_body(block: list):
    """The dedented body of the step's `run: |` block scalar, or None."""
    for k, ln in enumerate(block):
        m = re.match(r"^(\s*)(?:- )?run:\s*\|\s*$", ln)
        if m:
            base = len(m.group(1))
            body = [b for b in block[k + 1:] if not b.strip() or _indent(b) > base]
            nonblank = [b for b in body if b.strip()]
            if not nonblank:
                return ""
            cut = min(_indent(b) for b in nonblank)
            return "\n".join(b[cut:] for b in body) + "\n"
    return None


def _env(block: list) -> dict:
    env, inside, ind = {}, False, 0
    for ln in block:
        if re.match(r"^\s*env:\s*$", ln):
            inside, ind = True, _indent(ln)
            continue
        if inside:
            if ln.strip() and _indent(ln) <= ind:
                break
            m = re.match(r"^\s*([A-Z_][A-Z0-9_]*):\s*(.*)$", ln)
            if m:
                env[m.group(1)] = m.group(2).strip()
    return env


def _air_ref_default(lines: list):
    """The `default:` of the `air_ref` workflow_call input, comment-stripped.
    Returns (found, value)."""
    for k, ln in enumerate(lines):
        if re.match(r"^\s*air_ref:\s*$", ln):
            ind = _indent(ln)
            for nxt in lines[k + 1:]:
                if nxt.strip() and _indent(nxt) <= ind:
                    break
                m = re.match(r"^\s*default:\s*(.*)$", nxt)
                if m:
                    return True, m.group(1).strip()
            return True, None
    return False, None


def _resolve(body: str, override: str, wf_sha: str):
    """Execute the resolver script; return the `ref=` it writes, or None."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out")
        open(out, "w").close()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GITHUB_OUTPUT": out,
               "OVERRIDE": override, "WF_SHA": wf_sha, "WF_REF": ""}
        r = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return None
        # GitHub reads $GITHUB_OUTPUT top to bottom and the LAST assignment of a
        # name wins, so a script that writes the right ref and then overwrites it
        # must be judged by the final value, not the first.
        refs = [ln[len("ref="):] for ln in open(out).read().splitlines() if ln.startswith("ref=")]
        return refs[-1] if refs else None
    return None


def check(text: str, name: str = "<workflow>") -> list:
    errors = []
    lines = [_strip_comment(ln) for ln in text.splitlines()]
    steps = _steps(lines)

    found, default = _air_ref_default(lines)
    if not found:
        errors.append(f"{name}: no `air_ref` input — callers lose the explicit override")
    elif default not in ("''", '""'):
        errors.append(f"{name}: `air_ref` must default to an empty string (got {default!r}) — "
                      "a non-empty default overrides job.workflow_sha and un-pins every caller")

    resolvers = [b for b in steps if _value(b, "id") == RESOLVER_ID]
    if len(resolvers) != 1:
        errors.append(f"{name}: expected exactly one step with `id: {RESOLVER_ID}`, found {len(resolvers)}")
    else:
        block = resolvers[0]
        env = _env(block)
        for key, want in EXPECTED_ENV.items():
            if env.get(key) != want:
                errors.append(f"{name}: resolver env {key} must be `{want}` (got {env.get(key)!r})")
        body = _run_body(block)
        if body is None:
            errors.append(f"{name}: resolver step has no `run: |` script")
        else:
            cases = (
                ("an explicit air_ref override wins", "feat/x", SAMPLE_SHA, "feat/x"),
                ("no override → the workflow's own commit", "", SAMPLE_SHA, SAMPLE_SHA),
                ("no override and no workflow commit → explicit main", "", "", "main"),
            )
            for label, override, sha, want in cases:
                got = _resolve(body, override, sha)
                if got != want:
                    errors.append(f"{name}: resolver produced {got!r} where {label} requires {want!r}")

    air_checkouts = [b for b in steps
                     if (_value(b, "uses") or "").startswith("actions/checkout@")
                     and _value(b, "repository") == AIR_REPO]
    if not air_checkouts:
        errors.append(f"{name}: no `actions/checkout` step for {AIR_REPO} found")
    for b in air_checkouts:
        ref = _value(b, "ref")
        if ref != RESOLVED_REF:
            errors.append(f"{name}: the {AIR_REPO} checkout must use `ref: {RESOLVED_REF}` (got {ref!r})")
    return errors


def main(argv: list) -> int:
    paths = argv[1:]
    if not paths:
        print("usage: check_workflow_pins.py <workflow.yml> ...", file=sys.stderr)
        return 2
    errors = []
    for p in paths:
        with open(p) as f:
            errors += check(f.read(), p)
    for e in errors:
        print(e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
