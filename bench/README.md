# The benchmark

A paired experiment that measures what delegating implementation to a
subagent is actually worth, in tokens and dollars. The headline result and
the numbers live in the repository README under "What it is worth,
measured"; this file is the method, so that someone who distrusts the
number can check how it was produced or rerun it.

## What is being compared

One variable, held everything else fixed.

| Held fixed | Varied |
|---|---|
| the task, the repository, the tool set, the effort level, the turn cap, the model that orchestrates | whether implementation is delegated to a subagent |

- **inline arm**: one Fable 5 agent with read, bash, write and edit. It
  implements and verifies by itself.
- **delegated arm**: a Fable 5 orchestrator with read, bash and a
  `delegate_implementation` tool, but *no write access*. It briefs an Opus
  5 implementer that works in its own fresh context, then verifies the
  result itself.

Success is a passing test suite, checked by the harness after the agent
stops, not by asking the agent. That matters: without it, an arm that
gives up early looks like an arm that was efficient.

## What is recorded

Every response's `usage` is read and charged at published per-token rates,
including the cache multipliers (writes at 1.25x, reads at 0.1x). Nothing
is estimated. Per run:

| Field | Meaning |
|---|---|
| `orchestrator.cost` | dollars spent on the expensive model. This is the headline measure |
| `orchestrator.ctx_peak` | largest single-request context on the orchestrator |
| `orchestrator.ctx_cumulative` | sum of context across the orchestrator's requests, which is what a growing window actually costs |
| `orchestrator.out_tokens` | output tokens on the expensive model, thinking included |
| `implementer.*` | the same fields for the subagent |
| `total_cost` | both models together |
| `success` | did the suite pass |
| `aborted` | set when a cost gate stopped the run |

`ctx_cumulative` is the honest context metric, not `ctx_peak`. Context is
re-sent on every request, so a window that is large for ten turns costs
ten times, and peak alone hides that.

## Cost gates

The harness refuses to spend more than it was told to. `PER_RUN_CAP`
aborts a single run, `GLOBAL_CAP` aborts the benchmark, and both raise
rather than warn. An aborted run is still recorded, with `aborted` set and
`success` measured, so an abort is visible as an abort instead of quietly
becoming a data point. In the reported set no run hit either gate.

## Running it

```bash
pip install anthropic
export ANTHROPIC_API_KEY=...      # per-token billing, no subscription credits
python3 bench/bench.py feature duration
```

Arguments are task names. Each named task runs in both arms. Results
append to `bench/results.json`, so repeated invocations accumulate
repetitions rather than overwriting them. Throwaway repositories are
created under `~/.bench-work` and rebuilt fresh for every run, so the arms
always start from identical state.

The key is read from the environment by the SDK and never written to disk
by the harness.

## Tasks

| Name | Shape | In the reported 16 runs |
|---|---|---|
| `duration` | write one new module against 11 failing tests | yes, 4 runs per arm |
| `feature` | write three new modules against 27 failing tests | yes, 4 runs per arm |
| `ledger` | fix five bugs in one existing module | no, defined but never run |
| `pipeline` | find and fix one bug in a 16-file package | no, defined but never run |

`ledger` and `pipeline` are included because the bug-finding shape is the
obvious next thing to measure, but they are not part of any reported
result and no claim rests on them.

## What this design cannot tell you

- **It is not Claude Code.** The loop, the tool set and the prompts imitate
  the real harness; they are not it. The ratio between the arms should
  transfer better than the absolute numbers, but that assumption is
  untested. Running the same tasks through real Claude Code sessions
  billed to two separate API keys, and comparing per-key spend in the
  Console, would close this gap.
- **The tasks are synthetic and small.** Contexts stayed under 5000
  tokens. The mechanism being measured scales with the number of
  implementer turns, which was three here. Larger, messier work should
  favor delegation more, but that is a prediction, not a finding.
- **One effort level.** Everything ran at `medium`. Effort strongly affects
  thinking volume, and thinking is billed as output, so a sweep across
  `low` through `xhigh` would likely move the numbers.
- **Four runs per cell.** Enough to see that the large-task effect sits
  outside the observed spread, not enough for a confidence interval.
- **One repository shape, one language, one test runner.**

## Reading the raw data

`results.json` holds every run, plus the per-model token totals and the
configuration that produced them. A quick summary:

```bash
python3 - <<'EOF'
import json, statistics as st
runs = json.load(open("bench/results.json"))["runs"]
for task in sorted({r["task"] for r in runs}):
    for arm in ("inline", "delegated"):
        cell = [r for r in runs if r["task"] == task and r["arm"] == arm]
        if not cell:
            continue
        cost = [r["orchestrator"]["cost"] for r in cell]
        print("%-9s %-10s n=%d  expensive-model $%.4f (%.4f to %.4f)  ok=%d/%d"
              % (task, arm, len(cell), st.mean(cost), min(cost), max(cost),
                 sum(r["success"] for r in cell), len(cell)))
EOF
```
