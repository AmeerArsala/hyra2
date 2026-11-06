# Hyra 2.0: research harness (Python) + Agent Skills

A working implementation of the research harness described in Tencent Hunyuan's
[Hyra 2.0: The Next Step for Auto Research](https://hy.tencent.ai/research/hyra2) (Nov 1, 2026).
Tencent hasn't released its own agent code. This reimplements the published design in two layers:

1. **`hyra/`: the harness, in Python** (~2.1k lines, stdlib only). It holds the durable research
   state and enforces the rules: persistent task queue, Research Board, messages, approvals, blind
   review, evidence, per-agent memory, tiered knowledge, the Hyra 1.0 Experience Bank loop, and a
   scheduler that drives headless agent sessions. See the [code tour](#code-tour).
2. **`skills/`: the agent layer**, as standard [Agent Skills](https://agentskills.io) (`SKILL.md`)
   plus role protocols. They teach an agent to play Overall / Mission / Worker / Reviewer on top of the
   harness. They work in Claude Code (natively, see [Claude Code specifics](#claude-code-specifics)),
   Codex, Antigravity (IDE + `agy` CLI), OpenCode, Cursor, Amp, Qwen Code, Copilot, Goose and Droid.

> Hyra 2.0's thesis: move from "lottery-style sampling and repeated problem-solving" to **improving
> tools**: models, workflows, verification methods, and skills that every later task can reuse.

## Install

```bash
./install.sh      # = hyra setup: links skills into ~/.agents/skills + every installed harness's skills dir,
                  #   hyra-* subagent types into ~/.claude/agents (if Claude Code exists), `hyra` into ~/.local/bin
./uninstall.sh    # removes only those symlinks
uv tool install -e .     # optional: install the CLI as a Python package instead (or: pip install -e .)
python3 -m unittest discover -s tests   # end-to-end tests (Python ≥3.9, no dependencies)
```

`skills/hyra/scripts/hyra` is a thin launcher. It finds the `hyra/` package by walking up from its
real location, or falls back to an installed `hyra` package. So the skill works from a checkout
(symlinked) or with the package installed.

## Use

In any harness, from the repo you're researching in, ask for the `hyra` skill
(Claude Code: `/hyra <goal>`; elsewhere: "use the hyra skill to …" or the harness's skill syntax):

```
/hyra train a protein inverse-folding model that beats ProteinMPNN on scTM at >10x speed
```

The agent takes the **Overall** role. It aligns the goal with you, runs `hyra init`, opens Missions, and then either:
- **In-session mode:** plays each Mission itself and gives each task a fresh context. It uses the
  harness's subagents if it has them (Claude Code gets dedicated `hyra-worker` / `hyra-reviewer`
  types); otherwise `hyra run --task <id> [--harness …]` hands the task to a headless agent CLI.
- **Daemon mode:** you run `hyra run` (in tmux or a terminal). This scheduler launches headless
  sessions by priority, dependencies, resource limits, approvals and wake-up times, and requeues
  workers that die or hang. Preview with `hyra run --dry-run`.

**Mixing harnesses.** Every headless session gets its role protocol (natively for Claude, in the prompt elsewhere), so any agent CLI
can play any role, and harnesses can be mixed per task. For example, review Claude's results with Codex:
`hyra review request 12 --harness codex`. Built-in presets: `claude`, `codex`, `agy` (Antigravity), `opencode`,
`amp`. Add or override them under `harness_cmds` in `.hyra/config.json` (see `skills/hyra/references/cli.md`).

You stay in the loop for approvals (`hyra approval list`). Large compute, team conventions, and syncs
to the shared knowledge library all need your explicit yes.

## How the paper maps to this repo

| Hyra 2.0 design element | Implementation |
|---|---|
| **Overall** talks with researchers, manages priorities, coordinates resources and key decisions | `hyra` skill (`skills/hyra/SKILL.md`), mission priorities, `hyra approval` |
| **Mission** owns a research problem long-term: directions, experiments, organizing Workers, owning conclusions | `roles/mission.md`, `hyra mission …` (close is refused while results are unreviewed) |
| **Workers** read code, run experiments, analyze failures, propose ideas, challenge the Mission; different models for different perspectives | `roles/worker.md`; `hyra msg send --to mission:…`; `task add --model … --harness …` (different models *and* vendors) |
| **Persistent task queue**; scheduler launches by priority, dependencies, resource limits; waiting tasks don't block others | SQLite queue in `.hyra/hyra.db`; `task add --after/--resource`; atomic `task next`; `hyra run` |
| Agents **schedule their next wake-up** and resume when an experiment finishes | `task sleep --until`, `mission wake --at`; the runner resumes them |
| **Human approval** for substantial compute or shared-knowledge sync; authorized work continues | `approval_rules` in config → `awaiting_approval`; `books sync` / `skill publish` gated; agents can't grant |
| **Research Board** for ideas, findings, evidence, decisions, read asynchronously; **messages** for things that need action, with explicit responses | `hyra board`, `hyra msg … --requires-response`, `hyra brief` (per-agent read cursor) |
| Completion must **attach evidence**: code versions, results, logs, raw traces | `task complete` requires `--evidence`; files are hashed and copied, git HEAD recorded; `task note` = action trace |
| **Review Worker** gets the original problem and evidence *first*, to avoid anchoring on the Worker's conclusion; narrower findings kept faithfully | `roles/reviewer.md`; `review evidence → blind → claim → verdict` (enforced order, immutable, weakest verdict wins) |
| Separate **project state / agent working memory / reusable knowledge** | queue + Board / `.hyra/memory/<agent>.md` (understanding, hypotheses, ruled-out, next steps) / Books |
| **Books**: method + rationale + applicability + original evidence; tiers **Observation / Verified / Convention**; revised as evidence emerges | `hyra books add/promote/revise/sync`; verified needs independent reproduction; convention needs approval |
| **Retrospectives** distill transferable methods into **skills** that keep being refined | `hyra-retro` skill; `hyra skill new/publish` → linked into every installed harness (available in all future sessions) |
| Review flaws become **automated checks** in the toolchain | Mission and retro protocols queue `build` tasks for them |
| Hyra 1.0 loop (Context Agent, Experience Bank, Proposal Agents, `solve.sh`, sandboxed scoring, bilevel evaluator evolution) | `hyra eb …` + `hyra-loop` skill |
| HyProver: conjectures → proof Workers → independent fresh-context review → reviewed lemmas as premises | `hyra-prover` skill |

## Layout

```
hyra/                         the harness (Python package, CLI entry point hyra.cli:main)
skills/hyra/SKILL.md          Overall role + entry point (harness-neutral)
skills/hyra/roles/*.md        Mission / Worker / Reviewer protocols (also Claude Code subagent definitions)
skills/hyra/references/cli.md full CLI reference, harness presets
skills/hyra/scripts/hyra      launcher used by agents (finds hyra/)
skills/hyra-loop/ hyra-prover/ hyra-retro/   companion skills
tests/test_e2e.py             end-to-end tests
pyproject.toml  install.sh  uninstall.sh
```

Per-project state lives in `<project>/.hyra/`. The shared library is `~/.hyra/library/` (override
with `HYRA_LIBRARY`).

## Code tour

| module | lines | what it does |
|---|---|---|
| [`hyra/store.py`](hyra/store.py) | ~300 | Project discovery (`.hyra/`), config defaults, **harness presets**, the SQLite schema (missions, tasks, task_notes, evidence, board, messages, approvals, reviews, agent_state, sessions), WAL + `BEGIN IMMEDIATE` transactions so many agents can share one queue, memory-file template. |
| [`hyra/core.py`](hyra/core.py) | ~650 | The Hyra 2.0 mechanics. Missions (close refused while results are unreviewed); the **task queue** (priority → deps → approval gate → resource capacity → sleeping/wake-up; atomic claim); **evidence** (hashing, copying, git HEAD); the Board; messages that require a response; **approvals** (agents can't grant); **blind review** (evidence → blind notes → claim → immutable verdict, weakest verdict wins, refutations message the Mission); per-agent **briefings** with a read cursor. |
| [`hyra/books.py`](hyra/books.py) | ~230 | **Books** knowledge base: entries need method/rationale/applicability/evidence; observation → verified (independent reproduction required) → convention (human approval); revise/demote; approval-gated sync to the shared library; **skill distillation** and publishing. |
| [`hyra/eb.py`](hyra/eb.py) | ~210 | **Hyra 1.0 loop as a tool.** Experience Bank of `solve.sh` solutions scored by a versioned `eval.sh` in fresh scratch dirs; lineage; `inspire` bundles (diverse strong + failures + stagnation signal); evaluator upgrades + rescoring (the bilevel loop). |
| [`hyra/runner.py`](hyra/runner.py) | ~290 | **Scheduler** (`hyra run`). Wakes Missions (first run, due time, mail, results with nothing in flight); claims tasks and launches headless sessions per harness; Claude gets `--agent hyra-<role>`, other harnesses get the role embedded; reaps dead/hung/lingering sessions and requeues or fails tasks; `--task` single dispatch. |
| [`hyra/install.py`](hyra/install.py) | ~90 | `hyra setup`: the per-harness global skills-dir table; symlinks skills, Claude subagents and the CLI; uninstall. |
| [`hyra/cli.py`](hyra/cli.py) | ~340 | argparse wiring for every command (`hyra --help`). |

## Claude Code specifics

Claude Code gets the Claude-native path everywhere:
- **Skills** in `~/.claude/skills/` (`/hyra`, `/hyra-loop`, `/hyra-prover`, `/hyra-retro`).
- **Subagent types** `hyra-mission`, `hyra-worker`, `hyra-reviewer` in `~/.claude/agents/`. The role
  files carry Claude subagent frontmatter, so in-session the Overall dispatches tasks with the Agent tool
  (`subagent_type: "hyra-worker"`, `run_in_background: true`, `model` per task).
- Claude subagents can't spawn subagents, so in-session the main session plays Overall *and* each
  Mission, and only Workers/Reviewers are subagents. For unattended multi-day runs, `hyra run`
  launches every role as its own top-level `claude -p --agent hyra-<role>` session (the role is the
  system prompt) with `--permission-mode auto`.
- Other harnesses read the same role files (frontmatter stripped, embedded in the prompt), so behaviour stays the same across them.

## Config (`.hyra/config.json`)

- `harness`: default agent CLI for headless sessions (`claude`, `codex`, `agy`, `opencode`, `amp`, or one you define in `harness_cmds`).
- `resources`: capacities, e.g. `{"workers": 4, "gpu": 8}`. Each running task uses 1 worker plus whatever it requests.
- `approval_rules`: e.g. `{"gpu": 1}` means any task requesting ≥1 GPU needs human approval.
- `session_max_minutes` (240), `settle_grace_seconds` (90): stop sessions that hang or linger after their task is settled.
- `max_missions_running`, `max_attempts`, `poll_seconds`, `eb_sandbox_prefix` (e.g. a `docker run …` prefix for real isolation).

## Limits (honest)

- Resource accounting covers tasks Hyra launches. Cluster jobs a worker submits are tracked only
  through the notes it records and the sleep/wake cycle.
- Review blinding is enforced by the CLI's ordering (the claim can't be read until a blind assessment
  is recorded, and `task show` hides it from the reviewer). An agent that deliberately reads the database could still get around it.
- Experience Bank evaluation runs in a fresh scratch copy, not a security sandbox, unless you set `eb_sandbox_prefix`.
- Approval enforcement relies on the agent role (`HYRA_ROLE`) for headless sessions, and on instructions in-session.
- Headless presets use autonomous permission flags (Claude `auto`, Codex `workspace-write` sandbox, agy `--dangerously-skip-permissions`,
  Amp `--dangerously-allow-all`), because unattended agents can't answer prompts. Tighten them in `harness_cmds` if needed.
- Verification status (2026-11-06):

  | harness | skills discovered | headless `hyra run` worker |
  |---|---|---|
  | Claude Code | ✅ 4 skills + 3 `hyra-*` subagents | ✅ `--agent hyra-worker` and embedded-role modes |
  | Codex | ✅ (in model-visible prompt) | ✅ sandboxed `workspace-write`, task completed with evidence |
  | Antigravity CLI (`agy`) | ✅ | not yet run |
  | OpenCode (Aeon `sven`) | ✅ (`opencode debug skill`) | not yet run |
  | Cursor, Qwen, Amp, Copilot, Goose, Droid | linked only | preset untested (Amp) / none |
