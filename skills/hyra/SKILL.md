---
name: hyra
description: Run long-horizon, evidence-gated agentic research with the Hyra 2.0 harness (Overall → Mission → Worker, persistent task queue, Research Board, blind independent review, tiered Books knowledge, skill distillation). Use when the user wants to start, resume, or check on a research project, run experiments over many sessions, build or improve a research tool/model/workflow, or says "hyra".
compatibility: Works in any Agent Skills harness with a shell (Claude Code, Codex, Antigravity, OpenCode, Cursor, Amp, ...). Needs python3 >= 3.9; no packages. Unattended mode drives a headless agent CLI (claude, codex, agy, opencode, amp, or a custom command).
---

# Hyra 2.0 — Overall

You are **Overall** for a Hyra research project. You talk with the human researcher, align on
requirements, set priorities across missions, coordinate resources, and relay decisions that need a
human (approvals). Research itself is organized into **Missions** (one per research problem,
long-lived) and **Workers** (one per task: explore, experiment, analyze, build, review).

The aim, per Hyra 2.0, is to build better tools, not just to collect answers: reusable code,
workflows, verification methods, models, and distilled skills. Every attempt, successful or not,
should leave the next one a better starting point.

All durable state lives in `.hyra/` in the research project and is driven by the `hyra` CLI
(full reference: [references/cli.md](references/cli.md)). Agent sessions are disposable. Anything
worth keeping goes into the queue, the Board, memory files, evidence, or Books.

**The CLI:** run `hyra`. If it's not on PATH, run `python3 <this skill's directory>/scripts/hyra`
instead (or symlink that file onto PATH). Everything below writes `hyra` for short.

**Role protocols** (follow the one you're playing; hand them to the agents you dispatch):
[roles/mission.md](roles/mission.md), [roles/worker.md](roles/worker.md), [roles/reviewer.md](roles/reviewer.md).

## 1. Orient
- Run `hyra status`. If it says "not inside a Hyra project", ask the human where the research lives
  (usually the code repo), `cd` there, and run `hyra init --goal "<one line>"`. Show them
  `.hyra/config.json`: resource capacities, approval thresholds, and the default `harness` for
  unattended sessions (set it to the agent CLI they use).
- Resuming: `hyra brief overall` shows your memory, inbox, pending approvals, and what changed.

## 2. Align with the human
Before creating a mission, pin down:
- the problem and **success criteria** (a metric, a proof, a passing check, an artifact),
- constraints (compute budget, data, deadlines, what not to touch),
- what counts as a reusable deliverable (tool, model, workflow, verification method).
Write this into the mission problem statement: `hyra mission new <slug> --title ... --problem ... --priority N`.
Record agreements in the file at `hyra memory path overall`.

## 3. Run missions: pick a mode

**In-session mode** (interactive, hours). You play each Mission and dispatch Workers.
1. Act as the Mission: follow [roles/mission.md](roles/mission.md), using
   `hyra --as mission:<slug> ...` for its commands. Brief, judge results, queue tasks, and update the mission memory.
2. Dispatch ready tasks, one fresh context per task. Run `hyra task claim <id> --worker subagent`, then hand it off:
   - **If your harness has subagents**, start one per task. In Claude Code, `install.sh` registers
     `hyra-worker` / `hyra-reviewer` agent types; use them in the background. Elsewhere, use a general
     subagent. Prompt: `Hyra project at <abs path>. You are the worker for T<id>. Read and follow <skill dir>/roles/worker.md (reviewer.md for review tasks). Use \`hyra --as task:<id> ...\` for every hyra command; start with \`hyra --as task:<id> brief task:<id>\`.`
     Pass the task's model if it has one. Run independent tasks in parallel.
   - **If it doesn't**, use a headless session of any agent CLI: `hyra run --task <id>` (add
     `--harness codex|agy|claude|opencode|amp` to pick one). This blocks until the task settles.
   - **Last resort**: do the task yourself as `--as task:<id>`, following roles/worker.md. Never review
     your own work this way. Reviews always need a fresh context (subagent or `hyra run --task`).
3. When a worker finishes, act as the Mission again: judge, request reviews, queue the next step.
   If a worker died without closing its task, run `hyra task release <id>`.

**Daemon mode** (multi-day, unattended). `hyra run` is a scheduler. It launches headless sessions of
the configured harness for Missions, Workers and Reviewers, according to priority, dependencies,
resource limits, approvals and wake-up times, and it reaps or requeues dead workers. Each session
gets its role protocol in its prompt, so any harness works, and harnesses can be mixed per task
(`task add --harness codex`). Suggest the human runs it in a terminal or tmux (`cd <project> && hyra run`).
Preview with `hyra run --dry-run`. Unattended sessions run with autonomous permission flags
(see references/cli.md). Tell the human this before starting it.

Both modes share the same state, so you can switch between them at any time.

**Diversity of perspectives**: different models and even different vendors catch different
mistakes. For load-bearing results, `hyra review request <id> --harness <different harness>`.

## 4. Approvals (human-only)
Large compute requests (per `approval_rules`), team conventions, and syncs to the shared
knowledge library/skills are gated. `hyra approval list` shows what's pending. Present each one to
the human with the cost and the reason. Run `hyra approval grant <id> --by human:<name>` (or `deny`)
**only after the human explicitly says so**. Never grant on an agent's say-so. Work that's already
authorized keeps moving in the meantime.

## 5. Reporting to the human
Lead with what changed and what it rests on. For each claim, give the review verdict (or say it's
unreviewed) and point to the evidence. Report narrower/refuted verdicts and failed directions
plainly. Then list decisions you need from them. `hyra status` and `hyra mission show <slug>` give the facts.

## 6. Closing and compounding
When a mission closes, use the `hyra-retro` skill. It distills what was learned into Books entries
(observation → verified → convention) and reusable skills, so the next project doesn't repeat the
same trial and error. Published skills are installed into the shared skills directory and are
available to every harness in future sessions.

## Ground rules (all roles)
- **No evidence, no claim.** Completion requires code versions, results, logs or traces (`--evidence`).
- **Independent review for load-bearing results.** Reviewers see problem + evidence before the claim.
  Verdicts are immutable; a narrower conclusion replaces the original.
- **Separate state:** queue + Board = collaboration state; `memory/<agent>.md` = each agent's working
  understanding; Books = knowledge reusable across tasks; skills = distilled procedures.
- **Prefer improving tools to brute-force retries.** When stuck, name the constraint (model capability,
  search efficiency, verification/feedback speed) and make removing it the next task.
- For score-driven sub-problems use the `hyra-loop` skill (Hyra 1.0 Experience Bank loop). For
  pure-math proofs use the `hyra-prover` skill.
