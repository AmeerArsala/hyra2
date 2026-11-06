# Hyra 2.0 for Claude Code

A working implementation of the research harness described in Tencent Hunyuan's
[Hyra 2.0: The Next Step for Auto Research](https://hy.tencent.ai/research/hyra2) (Nov 1, 2026),
built for Claude Code. Tencent hasn't released its own agent code. This reimplements the published
design: Claude Code agents/skills for the roles, plus a small stdlib-only Python CLI (`hyra`) for the
durable state that keeps research going across sessions, days, and parallel agents.

> Hyra 2.0's thesis: move from "lottery-style sampling and repeated problem-solving" to **improving
> tools**: models, workflows, verification methods, and skills that every later task can reuse.

## Install

```bash
./install.sh      # symlinks: ~/.local/bin/hyra, ~/.claude/agents/hyra-*.md, ~/.claude/skills/hyra*
./uninstall.sh    # removes only those symlinks
python3 -m unittest discover -s tests   # end-to-end tests (Python ≥3.9, no dependencies)
```

## Use

In any Claude Code session, from the repo you're researching in:

```
/hyra train a protein inverse-folding model that beats ProteinMPNN on scTM at >10x speed
/hyra status
/hyra resume
```

Claude takes the **Overall** role. It aligns the goal with you, runs `hyra init`, opens Missions, and
then either:
- **In-session mode:** plays each Mission itself and dispatches `hyra-worker` / `hyra-reviewer`
  subagents in the background, or
- **Daemon mode:** you run `hyra run` (in tmux or a terminal). This scheduler launches headless
  `claude -p --agent hyra-…` sessions by priority, dependencies, resource limits, approvals and
  wake-up times, and requeues workers that die. Preview with `hyra run --dry-run`.

You stay in the loop for approvals (`hyra approval list`). Large compute, team conventions, and syncs
to the shared knowledge library all need your explicit yes.

## How the paper maps to this repo

| Hyra 2.0 design element | Implementation |
|---|---|
| **Overall** talks with researchers, manages priorities, coordinates resources and key decisions | `/hyra` skill (`claude/skills/hyra/SKILL.md`), mission priorities, `hyra approval` |
| **Mission** owns a research problem long-term: directions, experiments, organizing Workers, owning conclusions | `hyra-mission` agent, `hyra mission …` (close is refused while results are unreviewed) |
| **Workers** read code, run experiments, analyze failures, propose ideas, challenge the Mission; different models for different perspectives | `hyra-worker` agent; `hyra msg send --to mission:…`; `task add --model …` |
| **Persistent task queue**; scheduler launches by priority, dependencies, resource limits; waiting tasks don't block others | SQLite queue in `.hyra/hyra.db`; `task add --after/--resource`; atomic `task next`; `hyra run` |
| Agents **schedule their next wake-up** and resume when an experiment finishes | `task sleep --until`, `mission wake --at`; the runner resumes them |
| **Human approval** for substantial compute or shared-knowledge sync; authorized work continues | `approval_rules` in config → `awaiting_approval`; `books sync` / `skill publish` gated; agents can't grant |
| **Research Board** for ideas, findings, evidence, decisions, read asynchronously; **messages** for things that need action, with explicit responses | `hyra board`, `hyra msg … --requires-response`, `hyra brief` (per-agent read cursor) |
| Completion must **attach evidence**: code versions, results, logs, raw traces | `task complete` requires `--evidence`; files are hashed and copied, git HEAD recorded; `task note` = action trace |
| **Review Worker** gets the original problem and evidence *first*, to avoid anchoring on the Worker's conclusion; narrower findings kept faithfully | `hyra-reviewer` agent; `review evidence → blind → claim → verdict` (enforced order, immutable, weakest verdict wins) |
| Separate **project state / agent working memory / reusable knowledge** | queue + Board / `.hyra/memory/<agent>.md` (understanding, hypotheses, ruled-out, next steps) / Books |
| **Books**: method + rationale + applicability + original evidence; tiers **Observation / Verified / Convention**; revised as evidence emerges | `hyra books add/promote/revise/sync`; verified needs independent reproduction; convention needs approval |
| **Retrospectives** distill transferable methods into **skills** that keep being refined | `hyra-retro` skill; `hyra skill new/publish` → `~/.claude/skills` (available in every future session) |
| Review flaws become **automated checks** in the toolchain | Mission and retro protocols queue `build` tasks for them |
| Hyra 1.0 loop (Context Agent, Experience Bank, Proposal Agents, `solve.sh`, sandboxed scoring, bilevel evaluator evolution) | `hyra eb …` + `hyra-loop` skill |
| HyProver: conjectures → proof Workers → independent fresh-context review → reviewed lemmas as premises | `hyra-prover` skill |

## Layout

```
bin/hyra                  CLI launcher
hyra/store.py             paths, config, SQLite schema
hyra/core.py              missions, queue, board, messages, approvals, blind review, memory, briefings
hyra/books.py             Books knowledge base + skill distillation/publishing
hyra/eb.py                Experience Bank (Hyra 1.0 loop)
hyra/runner.py            `hyra run` scheduler for headless sessions
claude/agents/            hyra-mission, hyra-worker, hyra-reviewer
claude/skills/            hyra (Overall + CLI reference), hyra-loop, hyra-prover, hyra-retro
tests/test_e2e.py         end-to-end tests
```

Per-project state lives in `<project>/.hyra/`. The shared library is `~/.hyra/library/` (override
with `HYRA_LIBRARY`).

## Config (`.hyra/config.json`)

- `resources`: capacities, e.g. `{"workers": 4, "gpu": 8}`. Each running task uses 1 worker plus whatever it requests.
- `approval_rules`: e.g. `{"gpu": 1}` means any task requesting ≥1 GPU needs human approval.
- `agent_cmd`: the headless session command. The default is `claude -p --permission-mode auto --agent {agent} [--model {model}] {prompt}`.
  Change the permission mode here if you want stricter or looser autonomy for unattended workers.
- `max_missions_running`, `max_attempts`, `poll_seconds`, `eb_sandbox_prefix` (e.g. a `docker run …` prefix for real isolation).

## Limits (honest)

- Resource accounting covers tasks Hyra launches. Cluster jobs a worker submits are tracked only
  through the notes it records and the sleep/wake cycle.
- Review blinding is enforced by the CLI's ordering (the claim can't be read until a blind assessment
  is recorded, and `task show` hides it from the reviewer). An agent that deliberately reads the database could still get around it.
- Experience Bank evaluation runs in a fresh scratch copy, not a security sandbox, unless you set `eb_sandbox_prefix`.
- Approval enforcement relies on the agent role (`HYRA_ROLE`) for headless sessions, and on instructions in-session.
