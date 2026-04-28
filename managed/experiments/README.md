# Cost-test harness — research-preview feature comparison

Compares managed-agents review configurations on **the same fixed work** so cost / time deltas reflect orchestration and feature use, not workload differences.

The work in every variant: **review the fixture diff and verify the findings.** The fixture is a small Python auth handler with three deliberate bugs (SQL injection, raw-password log, `debug=True`) — every variant should catch all three.

## The 2x2 grid (plus orthogonal axes)

|                 | no Memory                            | with Memory                          |
|-----------------|--------------------------------------|--------------------------------------|
| **split**       | `split` — 2 sessions: reviewer then verifier (today's architecture, miniature) | `split_memory` |
| **multiagent**  | `multiagent` — 1 session, coordinator + reviewer + verifier sub-agents | `multiagent_memory` |
| **solo**        | `solo` — 1 session, 1 agent does both review+verify | `solo_memory` |

Plus the Outcomes axis:
- `multiagent_outcomes` — multiagent + Outcomes self-eval loop
- `all` — multiagent + memory + outcomes (full stack)

8 variants total. Each does the **same review+verify work**, so direct comparisons are valid.

## What each comparison answers

| Pair | Answers |
|---|---|
| `split` vs `multiagent` | Does the multiagent feature reduce cost vs running separate sessions? Same memory state (none). |
| `split_memory` vs `multiagent_memory` | Same question with memory enabled. |
| `split` vs `split_memory` | Does Memory reduce cost? Architecture held constant (split). |
| `multiagent` vs `multiagent_memory` | Same Memory question, on the multiagent architecture. |
| `split` vs `solo` | Does collapsing reviewer+verifier into one agent save money? |
| `multiagent` vs `multiagent_outcomes` | Outcomes cost+quality impact. |
| `split` vs `all` | Full preview-feature stack vs today's architecture — the headline. |

## Cost estimate per variant

| variant | sessions | est. cost |
|---|---|---|
| split | 2 | ~$0.80 |
| split_memory | 2 | ~$0.65 |
| solo | 1 | ~$0.60 |
| solo_memory | 1 | ~$0.50 |
| multiagent | 1 (3 threads) | ~$1.50 |
| multiagent_memory | 1 (3 threads) | ~$1.30 |
| multiagent_outcomes | 1 (3 threads) | ~$2.00 |
| all | 1 (3 threads) | ~$1.80 |

**Total to run all 8: ~$9-10.** You can also run just the headline pair (`split` + `all`) for ~$3.

## Run

```bash
cd /Users/dima.v/Documents/GitHub/air

# Run any subset — order doesn't matter, results.jsonl appends.
python3 managed/experiments/cost_test.py --variant split
python3 managed/experiments/cost_test.py --variant split_memory
python3 managed/experiments/cost_test.py --variant solo
python3 managed/experiments/cost_test.py --variant solo_memory
python3 managed/experiments/cost_test.py --variant multiagent
python3 managed/experiments/cost_test.py --variant multiagent_memory
python3 managed/experiments/cost_test.py --variant multiagent_outcomes
python3 managed/experiments/cost_test.py --variant all

# Compare
python3 managed/experiments/cost_test.py --report

# Clean up test agents + memory stores
python3 managed/experiments/cost_test.py --cleanup
```

## Sample report output

```
variant                  cost     in    out  cache_rd     cc_5m   wall  compute
----------------------------------------------------------------------------------
split                  $0.420   180  3,200    45,300    12,400    65s     58s
split_memory           $0.312   180  3,400    23,100     4,800    72s     65s
solo                   $0.385   180  4,100    52,000    14,200    78s     71s
multiagent             $0.890   240  5,800   140,400    18,500    92s     85s
multiagent_memory      $0.620   240  5,500    78,200     9,200    98s     90s
multiagent_outcomes    $1.380   320  9,400   210,000    24,000   135s    122s
all                    $1.150   320  8,800   165,000    18,000   140s    125s

Direct comparisons:
  split            → multiagent          $0.420 → $0.890  (+0.470 = +112%)  multiagent feature impact (no memory)
  split_memory     → multiagent_memory   $0.312 → $0.620  (+0.308 = +98.7%) multiagent feature impact (with memory)
  split            → split_memory        $0.420 → $0.312  (-0.108 = -25.7%) memory feature impact (split arch)
  multiagent       → multiagent_memory   $0.890 → $0.620  (-0.270 = -30.3%) memory feature impact (multiagent arch)
  split            → solo                $0.420 → $0.385  (-0.035 = -8.3%)  consolidation impact (1 agent vs 2)
  multiagent       → multiagent_outcomes $0.890 → $1.380  (+0.490 = +55.1%) outcomes feature impact
  split            → all                 $0.420 → $1.150  (+0.730 = +173%)  full stack vs current arch
```

(numbers are illustrative — actual results from the run will vary)

## Caveats

1. **Outcomes API shape is speculative.** The Outcomes docs page returns 404 for non-preview accounts. The harness sends `body["outcome"] = {"description": "..."}` based on blog descriptions. If the API rejects it, the error tells us the correct shape — adjust and re-run.
2. **Multiagent usage may be reported per-thread.** The harness reads `session.usage` for the parent. If sub-agent thread tokens aren't aggregated there, multiagent costs will look artificially low. Cross-check against the dashboard after the run.
3. **Raw HTTP, not the new SDK.** The same `managed-agents-2026-04-01-research-preview` beta header should unlock the same surface; install the research-preview SDK only if specific endpoint shapes don't work via raw HTTP.
4. **Solo's review+verify in one role is a quality risk** — the same agent that produces findings is asked to filter false positives. Watch for "found 5 things, kept all 5" behavior in the output sample. That's why the cost plan keeps verifier separate even in Tier 3.

## Files

```
managed/experiments/
├── cost_test.py              # main harness
├── fixtures/
│   ├── test_pr.diff          # 80-line auth handler with 3 deliberate bugs
│   ├── test_pr_context.txt   # PR Context block
│   ├── wiki_REVIEW.md        # synthetic 30-line wiki
│   └── wiki_PROJECT-PROFILE.md
├── results.jsonl             # appended results, one per variant run (gitignored)
├── .gitignore
└── README.md
```
