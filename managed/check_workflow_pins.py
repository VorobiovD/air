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


def _jobs(lines: list) -> list:
    """[(job_name, [step_block, ...]), ...] in file order. Steps are the items of
    each job's own `steps:` list only (split at that list's dash indent), so a
    nested list item can never be mistaken for a step."""
    out, i, n = [], 0, len(lines)
    while i < n and not re.match(r"^jobs:\s*$", lines[i]):
        i += 1
    i += 1
    while i < n:
        m = re.match(r"^(\s+)([A-Za-z0-9_-]+):\s*$", lines[i])
        if not (m and lines[i].strip()):
            i += 1
            continue
        job_ind, job = len(m.group(1)), m.group(2)
        j = i + 1
        while j < n and not (lines[j].strip() and _indent(lines[j]) <= job_ind):
            j += 1
        body = lines[i + 1:j]
        steps = []
        for k, ln in enumerate(body):
            if re.match(r"^\s*steps:\s*$", ln):
                items = body[k + 1:]
                dash = next((_indent(x) for x in items if re.match(r"^\s*- ", x)), None)
                cur = None
                for x in items:
                    if x.strip() and _indent(x) < (dash or 0):
                        break
                    if dash is not None and re.match(r"^\s*- ", x) and _indent(x) == dash:
                        if cur is not None:
                            steps.append(cur)
                        cur = [x]
                    elif cur is not None:
                        cur.append(x)
                if cur is not None:
                    steps.append(cur)
                break
        out.append((job, steps))
        i = j
    return out


def _fields(block: list) -> dict:
    """The step's OWN top-level keys → raw value (`''` for a key that opens a
    nested mapping or block scalar). Keys inside `with:`/`env:` are not here."""
    dash = _indent(block[0])
    key_ind = dash + 2
    out = {}
    first = re.match(r"^\s*- ([A-Za-z0-9_-]+):\s*(.*)$", block[0])
    if first:
        out[first.group(1)] = first.group(2).strip()
    for ln in block[1:]:
        if ln.strip() and _indent(ln) == key_ind:
            m = re.match(r"^\s*([A-Za-z0-9_-]+):\s*(.*)$", ln)
            if m:
                out[m.group(1)] = m.group(2).strip()
    return out


def _child_map(block: list, key: str) -> dict:
    """Direct children of the step's top-level `key:` mapping (e.g. `with`, `env`)."""
    key_ind = _indent(block[0]) + 2
    out, inside, child_ind = {}, False, None
    for ln in block:
        if not inside:
            if _indent(ln) == key_ind and re.match(r"^\s*" + re.escape(key) + r":\s*$", ln):
                inside = True
            continue
        if ln.strip() and _indent(ln) <= key_ind:
            break
        if not ln.strip():
            continue
        if child_ind is None:
            child_ind = _indent(ln)
        if _indent(ln) == child_ind:
            m = re.match(r"^\s*([A-Za-z0-9_-]+):\s*(.*)$", ln)
            if m:
                out[m.group(1)] = m.group(2).strip()
    return out


def _run_body(block: list):
    """The dedented body of the step's top-level `run: |` block scalar, or None."""
    key_ind = _indent(block[0]) + 2
    for k, ln in enumerate(block):
        m = re.match(r"^(\s*)(- )?run:\s*\|\s*$", ln)
        if m and (_indent(ln) == key_ind or (k == 0 and m.group(2))):
            base = _indent(ln) if k else key_ind
            body = [b for b in block[k + 1:] if not b.strip() or _indent(b) > base]
            nonblank = [b for b in body if b.strip()]
            if not nonblank:
                return ""
            cut = min(_indent(b) for b in nonblank)
            return "\n".join(b[cut:] for b in body) + "\n"
    return None


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


def _parse_outputs(text: str):
    """Parse a $GITHUB_OUTPUT file the way the runner does: `name=value` lines and
    `name<<DELIM` … `DELIM` blocks, in order, last write wins. Returns a dict, or
    None if the file is malformed (an unterminated block, an unparsable line) —
    a format the guard cannot judge must fail, not pass."""
    out, lines, i = {}, text.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        if ln == "":
            i += 1
            continue
        # The runner's rule: if `=` appears before any `<<`, it's name=value;
        # otherwise a `<<` makes it a delimited multi-line block.
        eq_at, hd_at = ln.find("="), ln.find("<<")
        eq = re.match(r"^([^=]+)=(.*)$", ln) if eq_at >= 0 and (hd_at < 0 or eq_at < hd_at) else None
        hd = re.match(r"^(.+?)<<(.+)$", ln) if eq is None and hd_at >= 0 else None
        if hd:
            name, delim = hd.group(1), hd.group(2)
            j = i + 1
            while j < len(lines) and lines[j] != delim:
                j += 1
            if j >= len(lines):
                return None
            out[name] = "\n".join(lines[i + 1:j])
            i = j + 1
        elif eq:
            out[eq.group(1)] = eq.group(2)
            i += 1
        else:
            return None
    return out


def _resolve(body: str, override: str, wf_sha: str):
    """Execute the resolver script; return the effective `ref` output, or None."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out")
        open(out, "w").close()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GITHUB_OUTPUT": out,
               "OVERRIDE": override, "WF_SHA": wf_sha, "WF_REF": ""}
        r = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return None
        parsed = _parse_outputs(open(out).read())
        return None if parsed is None else parsed.get("ref")


def check(text: str, name: str = "<workflow>") -> list:
    errors = []
    lines = [_strip_comment(ln) for ln in text.splitlines()]
    jobs = _jobs(lines)

    found, default = _air_ref_default(lines)
    if not found:
        errors.append(f"{name}: no `air_ref` input — callers lose the explicit override")
    elif default not in ("''", '""'):
        errors.append(f"{name}: `air_ref` must default to an empty string (got {default!r}) — "
                      "a non-empty default overrides job.workflow_sha and un-pins every caller")

    resolvers = [(job, idx, b) for job, steps in jobs for idx, b in enumerate(steps)
                 if _fields(b).get("id") == RESOLVER_ID]
    if len(resolvers) != 1:
        errors.append(f"{name}: expected exactly one step with `id: {RESOLVER_ID}`, found {len(resolvers)}")
    else:
        r_job, r_idx, block = resolvers[0]
        f = _fields(block)
        # A skipped or allowed-to-fail resolver leaves the ref empty, and an empty
        # cross-repo checkout floats on air's default branch — the bug this guards.
        for key in ("if", "continue-on-error"):
            if key in f:
                errors.append(f"{name}: the resolver step must always run — remove its `{key}:`")
        env = _child_map(block, "env")
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

    checkouts = [(job, idx, b) for job, steps in jobs for idx, b in enumerate(steps)
                 if _fields(b).get("uses", "").startswith("actions/checkout@")
                 and _child_map(b, "with").get("repository") == AIR_REPO]
    if not checkouts:
        errors.append(f"{name}: no `actions/checkout` step for {AIR_REPO} found")
    for job, idx, b in checkouts:
        ref = _child_map(b, "with").get("ref")
        if ref != RESOLVED_REF:
            errors.append(f"{name}: the {AIR_REPO} checkout's `with.ref` must be `{RESOLVED_REF}` (got {ref!r})")
        if len(resolvers) == 1 and (job != resolvers[0][0] or idx < resolvers[0][1]):
            errors.append(f"{name}: the {AIR_REPO} checkout in job `{job}` must come after the "
                          f"`{RESOLVER_ID}` resolver in the same job")
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
