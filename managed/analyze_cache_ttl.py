#!/usr/bin/env python3
"""Reprice a headless run for 5m vs 1h cache TTL from its per-turn `[turn]` telemetry.

WHY this is exact: a 1h run's reads are all warm (the 1h TTL never expires within a
run), so its captured `[turn] … cr=…` profile is the TRUE cache-read mass. To get the
5m cost for the SAME review we reprice deterministically:
  - cache WRITES: 5m bills 1.25x base input, 1h bills 2x (same tokens written).
  - cache READS: a turn whose gap > 300s would MISS the 5m cache (TTL refreshes on each
    read, so > 5min since the prior read = expired) — its `cr` tokens become a re-write
    at 1.25x instead of a read at 0.1x. Turns within 300s stay 0.1x reads.
So `cache-miss %` = the fraction of cache-read tokens on turns with gap > 300s, and the
5m-vs-1h delta is the exact $ difference for that review.

Best run at 1h TTL (AIR_HEADLESS_CACHE_TTL=1h) so all reads are warm; the auto-TTL
already routes heavy PRs (the ones where misses can happen) to 1h, so harvesting their
CI/dry-run logs feeds this directly. A 5m run's expiry re-writes are already baked into
its `cw`, so its 1h-equivalent is only a lower bound (flagged below).

Usage:  analyze_cache_ttl.py <run.log> [<run2.log> ...]
        (reads the headless `[turn]` lines; ANSI/GHA prefixes are tolerated)
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "air" / "lib"))
import agent_loop  # noqa: E402  (single source of $/MTok rates + write multipliers)

_TURN = re.compile(
    r"\[turn\]\s+(\S+)\s+t=(\d+)\s+tc=(\d+)\s+gap=([\d.]+)s\s+"
    r"in=(\d+)\s+out=(\d+)\s+cw=(\d+)\s+cr=(\d+)")
_TTL = re.compile(r"cache TTL:\s+(\w+)")
# per-agent cost line carries the ACTUAL tier used: `[cost] <label> <tier> in=…`
# (the `[cost] TOTAL in=…` aggregate has no tier between the label and `in=`, so it's skipped)
_COST = re.compile(r"\[cost\]\s+(\S+)\s+(haiku|sonnet|opus)\s+in=")
_COMPLETE = re.compile(r"\[headless\] complete in ([\d.]+)s")
_HAIKU_LABELS = {"git-history-reviewer"}   # the only Haiku-tier lens; rest are Sonnet
_EXPIRY_S = 300.0
_FIVE_MIN_MULT = agent_loop.cache_write_mult("5m")   # 1.25
_ONE_HR_MULT = agent_loop.cache_write_mult("1h")     # 2.0


def _tier(label: str, tier_map: dict) -> str:
    # Prefer the tier the run ITSELF reported (the [cost] lines), so pricing stays
    # correct when code-reviewer/security-auditor revert from the temporary Sonnet
    # tier (#169) back to Opus. Fall back to the label snapshot for partial logs.
    return tier_map.get(label) or ("haiku" if label in _HAIKU_LABELS else "sonnet")


def analyze(path: str) -> dict | None:
    turns, ttl, wall, tier_map = [], "?", None, {}
    with open(path, errors="replace") as fh:
        for line in fh:
            m = _TURN.search(line)
            if m:
                turns.append((m.group(1), float(m.group(4)),
                              int(m.group(5)), int(m.group(6)), int(m.group(7)), int(m.group(8))))
            cst = _COST.search(line)
            if cst:
                tier_map[cst.group(1)] = cst.group(2)   # label -> actual tier this run used
            t = _TTL.search(line)
            if t:
                ttl = t.group(1)
            c = _COMPLETE.search(line)
            if c:
                wall = float(c.group(1))
    if not turns:
        return None
    c1h = c5m = 0.0
    cr_total = cr_miss = miss_turns = 0
    for label, gap, inp, out, cw, cr in turns:
        pin, pout = agent_loop.price_for_tier(_tier(label, tier_map))
        base = (inp * pin + out * pout) / 1e6
        c1h += base + (cw * pin * _ONE_HR_MULT + cr * pin * 0.1) / 1e6
        miss = gap > _EXPIRY_S and cr > 0          # 5m would have expired → re-write the read
        read_rate = pin * _FIVE_MIN_MULT if miss else pin * 0.1
        c5m += base + (cw * pin * _FIVE_MIN_MULT + cr * read_rate) / 1e6
        cr_total += cr
        if miss:
            cr_miss += cr
            miss_turns += 1
    return {"path": Path(path).name, "ttl": ttl, "wall": wall, "turns": len(turns),
            "c1h": c1h, "c5m": c5m, "delta": c1h - c5m,
            "miss_turns": miss_turns, "cr_total": cr_total, "cr_miss": cr_miss,
            "miss_pct": (100.0 * cr_miss / cr_total) if cr_total else 0.0}


def main(argv: list[str]) -> int:
    rows = [r for r in (analyze(p) for p in argv) if r]
    if not rows:
        print("no [turn] telemetry found — run headless with the per-turn instrumentation "
              "(agent_loop emits `[turn] … gap=… cw=… cr=…`).", file=sys.stderr)
        return 1
    print(f"{'run':28} {'ttl':3} {'turns':>5} {'miss%':>6} {'miss/turns':>10} "
          f"{'$1h':>7} {'$5m':>7} {'5m saves':>9}")
    for r in rows:
        warn = "" if r["ttl"] == "1h" else "  (5m run: 1h cost is a lower bound — expiry re-writes already in cw)"
        mt = f"{r['miss_turns']}/{r['turns']}"
        print(f"{r['path'][:28]:28} {r['ttl']:3} {r['turns']:>5} {r['miss_pct']:>5.1f}% "
              f"{mt:>10} ${r['c1h']:>6.2f} ${r['c5m']:>6.2f} ${r['delta']:>7.2f}{warn}")
    if len(rows) > 1:
        s1h = sum(r["c1h"] for r in rows); s5m = sum(r["c5m"] for r in rows)
        print(f"\nAGGREGATE: $1h={s1h:.2f}  $5m={s5m:.2f}  5m saves ${s1h - s5m:.2f} "
              f"({100 * (s1h - s5m) / s1h:.0f}%) across {len(rows)} runs")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
