---
name: hyra-retro
description: Hyra project retrospective — distill what a mission or project learned into evidence-backed Books entries (observation / verified / convention), automated checks, and reusable Claude Code skills, so later projects skip the same trial and error. Use when a Hyra mission closes, after a costly failure, or when the user asks to consolidate lessons.
---

# Hyra retrospective (continual learning)

The more expensive each attempt, the more important it is to keep its lessons. The output of a
retro is *tools for next time*: knowledge entries, checks, and skills.

## 1. Gather
- `hyra mission show <slug>` and `hyra board read --mission <slug> --limit 200`
- Review verdicts (especially `narrower`/`refuted`), failed tasks, and the "Ruled out" section of each
  memory file (`hyra memory show mission:<slug>`)
- `hyra books search "<topic>"`: what was already known going in

## 2. Extract candidate lessons
Look for:
- **Troubleshooting methods** that fixed a failed run (training divergence, OOM, numerical mismatch, flaky infra)
- **Validation procedures** that caught real errors (e.g. numerical agreement checks between implementations)
- **Review findings** about how results go wrong. These are candidates for **automated checks**:
  queue a `build` task to add the check to the toolchain (test, assertion, eval guard)
- **Search and design heuristics** that paid off, and ones that consistently wasted compute
- Changes to how the team should work (these are conventions and need a human to sign off)

## 3. Record in Books, at the right tier
- Every entry states the method, the rationale, and **when it applies** (and when it doesn't), with the
  original evidence: `hyra books add --title ... --method ... --rationale ... --applies-when ... --evidence T<id> [--tags]`
- Single-experiment phenomenon → stays an `observation`.
- Reproduced independently (a different task/run) → `hyra books promote <id> --to verified --evidence T<repro>`.
- Affects team behaviour → `--to convention` (creates an approval request for the human).
- New evidence contradicts an entry → `hyra books revise <id> --note ... --evidence ... [--demote]`. Don't
  delete it: what was believed and why it changed is useful too.
- Entries useful beyond this project → `hyra books sync <id>` (approval-gated) into the shared library.

## 4. Distill skills
When several entries form a repeatable procedure (e.g. "training a small scientific model from
scratch", "debugging loss spikes", "verifying a kernel against a reference"):
1. `hyra skill new <name> --description "<when to use it, in one line>" --from-books id1,id2`
2. Edit `.hyra/skills/<name>/SKILL.md` into a procedure another agent can follow cold: when to use it,
   numbered steps, checks and pitfalls, and provenance (Books ids / tasks). Keep it under ~150 lines.
3. `hyra skill publish <name>` → approval request; once granted, re-run to install it into
   `~/.claude/skills` so every future session can use it.
Existing skills that the retro shows to be wrong or incomplete should be revised the same way.
Skills are tools too, and they keep improving through use.

## 5. Close the loop
Post a `retro` board entry summarizing what was added (entries, checks queued, skills), and tell the
human what's waiting on their approval.
