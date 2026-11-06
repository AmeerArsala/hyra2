---
name: hyra-loop
description: Hyra 1.0 recursive improvement loop for performance-driven problems with a computable score (speedups, bounds/constructions, benchmark metrics, heuristics). Maintains an Experience Bank of scored solutions, assembles inspiration bundles, proposes improved solutions in parallel, and upgrades the evaluator when it becomes the bottleneck (bilevel loop). Use when a task or sub-problem can be scored automatically.
---

> Requires the `hyra` skill (its CLI: `hyra`, or `python3 <hyra skill dir>/scripts/hyra`).

# Hyra loop (Experience Bank)

A minimalist loop for problems with clear evaluation criteria: keep proposing, scoring and learning
until the target is hit or the budget runs out. In Hyra 2.0 this is a **tool a Worker or Mission uses**
for a score-driven sub-problem. It isn't the whole research process.

## Setup (once per problem)
1. Write the evaluator `eval.sh`. It's called as `bash eval.sh <solution_dir>` from a fresh scratch directory.
   - It runs `<solution_dir>/solve.sh` (or imports its outputs), checks validity **strictly**, and
     prints `SCORE: <number>`. Optional diagnostics: `METRIC name: value`.
   - Invalid outputs must exit non-zero or print no score. Never give partial credit for broken solutions.
   - Make it deterministic (fixed seeds, fixed test set) and as fast as you can. Evaluator speed
     bounds the loop's speed.
   - Hold out what you can. Agents will exploit any gap (reward hacking): unchecked constraints,
     timing noise, cached answers, reading the test data.
2. `hyra eb init <name> --evaluator eval.sh --task "<precise task + constraints>" --direction max|min --timeout 600`
3. Submit a baseline: a dir with `solve.sh`, then `hyra eb submit <name> <dir> --note "baseline"`.

## Inner loop (repeat)
- **Context step:** `hyra eb inspire <name> -k 4` gives the strongest diverse solutions, recent
  failures with logs, and a stagnation signal. Read the actual code and logs. Synthesize 2–4
  *different* inspirations: a refinement of the best, a combination of two lineages, a fix for
  a recurring failure, and a bold new direction.
- **Proposal step:** for each inspiration, write a new solution dir (copy the parent, then change it)
  and `hyra eb submit <name> <dir> --parent <sid> --note "<idea in one line>"`. To go in parallel,
  dispatch several workers (subagents, or tasks run via `hyra run`), each with a different inspiration and the same bank name.
- Record in a task note or memory which ideas helped and which didn't, so the next context step
  doesn't repeat dead ends.
- Stop when the target is met, the budget is spent, or several rounds bring no improvement. Then
  consider the outer loop.

## Outer loop (upgrade the evaluator)
When the inner loop ends, look at what the bank shows about the evaluator:
- Solutions that score well but are wrong or degenerate → close the loophole (reward-hacking fix).
- Evaluation too slow → make it faster without changing what it measures.
- Scores bunched near the ceiling or uninformative → add finer-grained feedback or more headroom
  (harder instances, stronger opponents taken from the bank itself).
Install it with `hyra eb evaluator <name> --new eval_v2.sh --note "<what changed and why>"`, then
`hyra eb rescore <name> --top 5`, and continue the inner loop under the new evaluator.

## Reporting
The best solution is evidence: `hyra task complete <id> --summary "best <score> under evaluator vN (<what it measures>)" --evidence eb:<name>/<sid> --evidence .hyra/eb/<name>/solutions/<sid>`.
State which evaluator version produced the score, and request an independent review if the score is
load-bearing. A reviewer should re-run the evaluator and audit it for loopholes. If the evaluator
or a trick is reusable, record it in Books.
