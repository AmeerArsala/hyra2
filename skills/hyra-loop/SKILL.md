---
name: hyra-loop
description: "Hyra 1.0 recursive improvement loop for performance-driven problems with a computable score (speedups, bounds/constructions, benchmark metrics, heuristics). Runs an autonomous pipeline: a Context Agent keeps a queue of inspirations stocked, parallel Proposal Agents turn them into scored solutions in an isolated sandbox, and an Evaluator Agent can write the first evaluator and upgrade it (bilevel loop). Use when a task or sub-problem can be scored automatically."
---

> Requires the `hyra` skill. Its CLI is `hyra`; if that is not on PATH yet, do the hyra skill's start-of-session check (`setup --cli --check`).

# Hyra loop (Experience Bank)

A minimalist loop for problems with clear evaluation criteria: keep proposing, scoring and learning until the target is hit or
the budget runs out. In Hyra 2.0 this is a **tool a Worker or Mission uses** for a score-driven sub-problem, not the whole
research process.

```
Context Agent --inspirations--> queue --take--> Proposal Agents (N in parallel) --submit--> sandboxed scoring --> Experience Bank
      ^                                                                                                     |
      +------------------------------------- reads results, restocks the queue ------------------------------+
   outer loop: Evaluator Agent inspects the bank between rounds and upgrades the evaluator (closes reward hacks, speeds it up,
               adds finer feedback or headroom); with no evaluator yet, it first writes one from the task description.
```

## Autonomous mode (preferred): `hyra eb loop`
```bash
hyra eb loop NAME --proposers 3 --max-submissions 40 --minutes 90 --target 0.95 --patience 12 --rounds 2 [--harness codex]
```
It launches the Context Agent, Proposal Agents and Evaluator Agent as headless sessions (roles `hyra-context`, `hyra-proposer`,
`hyra-evaluator`; any harness, and you may mix them with `--harness`), keeps at most `--proposers` Proposal Agents running, and stops
at the first of: submission budget, time budget, target score, `--patience` submissions without improvement, or a `STOP` file in
`.hyra/eb/NAME/`. In-flight proposers finish (their submissions count). `--rounds N` (>1) is the bilevel loop: after each round an
Evaluator Agent may install an upgraded evaluator and the next round runs under it. `hyra eb status NAME` shows progress;
`hyra eb loop NAME --dry-run` shows the plan.

### Setup
1. If you have an evaluator: write `eval.sh` (below) and `hyra eb init NAME --evaluator eval.sh --task "<precise task + constraints>" --direction max|min`.
   If you don't: `hyra eb loop NAME --create-evaluator --task "<precise task>" --direction max` and an Evaluator Agent writes it, tests it
   against a trivial and a cheating solution, and the loop starts.
2. Seed a baseline (a dir with `solve.sh`, `hyra eb submit NAME <dir> --note baseline`) so the first inspirations have something to build on.

### The evaluator contract
`bash eval.sh <solution_dir>` runs `<solution_dir>/solve.sh`, checks validity **strictly**, and prints `SCORE: <number>` (optionally
`METRIC name: value`). It runs from a fresh scratch copy **inside an OS sandbox when one is available** (macOS `sandbox-exec`, Linux
`bwrap`, or Docker via `eb_sandbox: docker`): no network, no writes outside the scratch dir, the project's `.hyra` unreadable, and no
credentials in the environment. Every record notes which sandbox ran; if none exists the run says so, so don't trust it with hostile code.
Make it deterministic, fast, and hard to game (agents will exploit any gap: unchecked constraints, timing noise, reading the test data).
Invalid output must exit non-zero or print no score.

## Manual mode
For a quick sub-problem you can drive it yourself: `hyra eb inspire NAME` (strong solutions, failures with log paths, a stagnation
signal), write solution dirs, `hyra eb submit NAME <dir> --parent <sid> --note "<idea>"`, `hyra eb top NAME`. Upgrade the evaluator with
`hyra eb evaluator NAME --new eval_v2.sh --note "<why>"` then `hyra eb rescore NAME --top 8`. The queue commands are
`hyra eb inspiration add|take|list` (what the Context and Proposal Agents use).

## Reporting
The best solution is evidence: `hyra task complete <id> --summary "best <score> under evaluator vN (<what it measures>)" --evidence eb:<name>/<sid> --evidence .hyra/eb/<name>/solutions/<sid>`.
State which evaluator version and sandbox produced the score, and request an independent review if the score is load-bearing; the
reviewer should re-run the evaluator and audit it for loopholes. If the evaluator or a trick is reusable, record it in Books.
