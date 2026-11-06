---
name: hyra-prover
description: HyProver-style workflow for proofs in pure mathematics on the Hyra harness — literature search, problem reformulation, intermediate conjectures, proof Workers, and independent fresh-context review, with reviewed lemmas accumulated in shared research notes as premises. Use when the goal is to prove (or disprove) a mathematical statement.
---

> Requires the `hyra` skill (its CLI: `hyra`, or `python3 <hyra skill dir>/scripts/hyra`).

# HyProver workflow

Organize mathematical exploration the way a careful mathematician does. Spend tokens on promising,
well-posed sub-goals rather than spreading attempts everywhere. Only lemmas that pass an independent
review become premises for further work.

## Setup
- One mission per target theorem: `hyra mission new <slug> --title "<statement>" --problem "<precise statement, definitions, known results, what counts as done (informal proof / Lean)>"`.
- Research notes live at `.hyra/prover/<slug>/notes.md` (create it). Keep these sections:
  - **Target** (exact statement and equivalent reformulations)
  - **Established**: reviewed lemmas only. Each has an id `L<n>`, a statement, the proof file
    `.hyra/prover/<slug>/lemmas/L<n>.md`, its review task, and its dependencies.
  - **Open conjectures**: id `C<n>`, statement, why we believe it, what it would unlock, attempts so far
  - **Refuted / dead ends**, with counterexamples or the reason
  - **Literature**: references, the exact results used, and how they apply
  - **Leads**: reformulations and connections to other fields worth pursuing

## Loop (Mission role)
1. **Explore** (`--kind explore` tasks): search the literature, check known results and special
   cases, compute small examples, and look for reformulations that connect the problem to other
   branches of mathematics where stronger tools exist. Output: leads and candidate intermediate conjectures.
2. **Choose** the next sub-goals: conjectures that are plausible (checked on examples), decisive
   (they unlock the target or a big piece of it), and tractable. Record why in a `decision` board post.
3. **Prove** (`--kind experiment` tasks, one conjecture each): a proof Worker writes a complete,
   self-contained proof to `lemmas/L<n>.md` using only Established lemmas and cited literature, then
   completes the task with that file as evidence. A counterexample or a gap is a valid result:
   record it under Refuted, or refine the conjecture.
4. **Review in a fresh context**: `hyra review request <task> --focus "check every step; reconstruct any step marked obvious"`,
   ideally with a different `--model`. Only `supported` moves a lemma to **Established**. With
   `narrower`, the weaker statement goes in instead.
5. Repeat. Assemble the final proof from Established lemmas, then request a whole-proof review.
   For important results, also queue a formalization task (Lean/Isabelle) if feasible, and get a
   human mathematician to check before claiming a solution.

## Discipline
- Never let an unreviewed lemma become a premise, even if it looks obvious.
- Track lemma dependencies, so that if one is refuted you know what falls with it.
- Proof techniques that work (or fail) across problems go into Books (`--tags math,<area>`).
