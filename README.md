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

Three scripts, each doing one thing. All take `--uninstall`.

```bash
./install-cli.sh               # just the `hyra` command (symlink in ~/.local/bin)
./install-toolkit-local.sh     # skills for ONE project (run from it, or --project DIR); copied, so the repo is self-contained
./install-toolkit-global.sh    # skills for every agent harness on this machine; symlinked, edits go live
#  toolkit options: --skills hyra,hyra-retro   --mode copy|link   --no-cli   (local: --project DIR --all-harnesses)
python3 -m unittest discover -s tests   # end-to-end tests (Python ≥3.9, no dependencies)
```

- **Local** writes `<project>/.agents/skills/` (Codex, Antigravity, OpenCode, Cursor, Amp, ...), plus
  `.claude/skills/` + `.claude/agents/` if Claude Code is installed (it doesn't read `.agents/`) and `.qwen/skills/`
  if Qwen is. Commit them so teammates get the same toolkit.
- **Global** writes `~/.agents/skills` and each installed harness's own dir, plus Claude's `hyra-*` agent types.
- Both toolkits install the CLI if `hyra` isn't already on PATH (never replace an existing one); `--no-cli` skips that.
- The skills call `hyra`, so the CLI must exist on the machine. Alternatively `uv tool install -e .` / `pip install -e .`.
- [`npx skills add`](https://skills.sh) can also install just the skill folders; it doesn't install the CLI or Claude's agent types.

`skills/hyra/scripts/hyra` is a launcher that works from three layouts: a source checkout (development), a self-contained
bundle `hyra.pyz` next to it (so a skills-only install needs nothing else), or an installed `hyra` package. After changing anything
under `hyra/`, run `python3 scripts/build_bundle.py`; a test fails if the bundle is stale.

### On a fresh repo or machine (e.g. via skills.sh)

`npx skills add <owner>/<repo>` (all skills) copies the skill folders into the repo (project-level by default; `-g` for one global
copy), and **that's all you do**. skills.sh has no post-install hook, so the skill handles the rest itself at the start of a session:
it runs `setup --cli --check`. **The CLI is the one global piece, whichever way the skills were installed**: a single self-contained
copy in `~/.hyra/cli/`, linked from `~/.local/bin/hyra`, so deleting a repo never breaks it and every repo uses the same one.
If the skills carry a newer version than the global one (say, an older hyra from your other computers), the agent tells you and **asks
before upgrading**; it never downgrades a newer global hyra, and installing skills never upgrades behind your back. The only
requirement is Python 3.9+. Everything else is optional:

| | what | how |
|---|---|---|
| 1 | an agent CLI installed and logged in, for unattended runs (`hyra run`) | `hyra doctor` lists which work; `hyra config set harness <name>` |
| 2 | per research repo | `hyra init` (the agent does this when you ask for research) |
| 3 | a ledger server, for hard blind-review and approval boundaries | `hyra server up` (it finds a tailnet device and deploys it for you; see below) |
| 4 | phone pings | [`docs/self-hosting.md`](docs/self-hosting.md) |
| 5 | wiki web capture | `defuddle-cli`, `trafilatura`, `yt-dlp` (the wiki scripts need `bash`, `git`, `python3`) |
| 6 | "named agents" | **skip this.** Some tools (Claude Code, OpenCode, Antigravity, omp) let you save helper agents as files so a chat can say "use hyra-worker". `hyra setup --toolkit global --agents-only` writes those files. Without them hyra pastes the same instructions into each prompt, which works everywhere and is what was tested most |

Run **`hyra doctor`** anywhere: it checks Python, the global CLI and its version, the skills in every harness's folder, which agent CLIs actually run
(not just exist on PATH), the scoring sandbox, and, inside a project, config problems, the ledger server and tokens, and says what to do.
**`hyra config show|set|unset|validate`** manages the layers (built-in defaults < `~/.hyra/config.json` < `<project>/.hyra/config.json`) and
catches typos (`overal_agent` is rejected with "did you mean `overall_agent`?"). `hyra init` writes only the project's name (and the ledger connection), so
your machine-wide settings aren't silently overridden.

## Quick start

The whole flow is three commands: `cd my-repo`, add the skills, `hyra init`; then talk to your agent. Details: **[docs/guide.md](docs/guide.md)**.

## Use

In any harness, from the repo you're researching in, ask for the `hyra` skill
(Claude Code: `/hyra <goal>`; elsewhere: "use the hyra skill to …" or the harness's skill syntax):

```
/hyra train a protein inverse-folding model that beats ProteinMPNN on scTM at >10x speed
```

The agent takes the **Overall** role. It aligns the goal with you, runs `hyra init`, opens Missions, and then starts the
**daemon** (`hyra run`, in the background or in tmux): a scheduler that launches each Mission, Worker and Reviewer as its *own*
headless session, by priority, dependencies, resource limits, approvals and wake-up times (including condition wake-ups, like
"when this training job's output file appears"), and that requeues workers that die or hang. Preview with `hyra run --dry-run`;
run one Mission in its own session with `hyra run --mission <slug>`. The daemon also runs an **unattended Overall**
(wakes when mail, failed tasks, a stalled mission, or an approval waiting >2h needs the human-facing layer; reprioritizes, unblocks,
messages you, never grants approvals; `notify_cmd` can ping your phone) and an **automatic retrospective** for every closed mission.

A light **in-session mode** also exists for small jobs: the agent plays the Mission itself and hands each task to a
fresh subagent (harnesses with agent files) or `hyra run --task <id>`. It's the less faithful path (Overall and Mission share a
context), so the skill tells the agent to say so.

**Mixing harnesses.** Every headless session gets its role protocol (natively for Claude, in the prompt elsewhere), so any agent CLI
can play any role, and harnesses can be mixed per task. For example, review Claude's results with Codex:
`hyra review request 12 --harness codex`. Built-in presets: `claude`, `codex`, `agy` (Antigravity), `kimi`, `omp`, `pi`,
`opencode`, `amp`. Add or override them under `harness_cmds` in `.hyra/config.json` (see `skills/hyra/references/cli.md`).

You stay in the loop for approvals (`hyra approval list`). Large compute, team conventions, and syncs
to the shared knowledge library all need your explicit yes.

## How the paper maps to this repo

| Hyra 2.0 design element | Implementation |
|---|---|
| **Overall** talks with researchers, manages priorities, coordinates resources and key decisions | interactive: the `hyra` skill; unattended: the `hyra-overall` role, woken by the daemon on mail / failed tasks / stalled missions / approvals waiting, adjusting `mission set --priority`, nudging missions, messaging the human; `hyra approval` stays human-only |
| **Mission** owns a research problem long-term: directions, experiments, organizing Workers, owning conclusions | `roles/mission.md`, `hyra mission …` (close is refused while results are unreviewed) |
| **Workers** read code, run experiments, analyze failures, propose ideas, challenge the Mission; different models for different perspectives | `roles/worker.md`; `hyra msg send --to mission:…`; `task add --model … --harness …` (different models *and* vendors) |
| **Persistent task queue**; scheduler launches by priority, dependencies, resource limits; waiting tasks don't block others | SQLite queue in `.hyra/hyra.db`; `task add --after/--resource`; atomic `task next`; `hyra run` |
| Agents **schedule their next wake-up** and resume when an experiment finishes or a checkpoint is reached | `task sleep --until-file/--until-cmd`, `mission wake --until-file/--until-cmd` (time is the fallback deadline); the runner evaluates conditions each tick, and also wakes a Mission whenever one of its tasks finishes |
| **Human approval** for substantial compute or shared-knowledge sync; authorized work continues | `approval_rules` in config → `awaiting_approval`; `books sync` / `skill publish` gated; agents can't grant |
| **Research Board** for ideas, findings, evidence, decisions, read asynchronously; **messages** for things that need action, with explicit responses | `hyra board`, `hyra msg … --requires-response`, `hyra brief` (per-agent read cursor) |
| Completion must **attach evidence**: code versions, results, logs, raw traces | `task complete` requires `--evidence`; files are hashed and copied, git HEAD recorded; `task note` = action trace |
| **Review Worker** gets the original problem and evidence *first*, to avoid anchoring on the Worker's conclusion; narrower findings kept faithfully | `roles/reviewer.md`; `review evidence → blind → claim → verdict` (enforced order, immutable, weakest verdict wins) |
| Separate **project state / agent hierarchical memory / reusable knowledge** | queue + Board / `.hyra/memory/<agent>/` (a short level-0 `index.md` plus level-1 nodes under understanding, hypotheses, ruled-out, next; briefings show the index and a tree, nodes open on demand) / Books |
| **Books**: method + rationale + applicability + original evidence; tiers **Observation / Verified / Convention**; revised as evidence emerges | `hyra books add/promote/revise/sync`; verified needs independent reproduction; convention needs approval |
| **Retrospectives** distill transferable methods into **skills** that keep being refined | `hyra-retro` skill; `hyra skill new/publish` → linked into every installed harness; closing a mission makes the daemon launch a retrospective session (`hyra-retrospective`, up to 2 tries; `auto_retro`) that records Books entries, checks and skill drafts and posts the `retro` entry; until then it shows as *pending* in every Overall briefing |
| Review flaws become **automated checks** in the toolchain | a `narrower`/`refuted` verdict automatically queues a `build` task to turn the flaw into a check that fails on the flawed version and passes on a correct one (`auto_check_tasks`) |
| Hyra 1.0 loop (Context Agent, Experience Bank, Proposal Agents, `solve.sh`, sandboxed scoring, asynchronous pipeline, bilevel evaluator evolution, initial evaluator from the task description) | `hyra eb loop`: a Context Agent keeps an inspiration queue stocked, up to N Proposal Agents (the semaphore) drain it in parallel, each submission is scored in a sandbox (macOS `sandbox-exec`, Linux `bwrap`, rootless Podman or Docker: no network, no writes outside the scratch dir, project state hidden), and a budget (submissions / minutes / target score / patience / STOP file) ends it; `--rounds N` lets an Evaluator Agent upgrade the evaluator between rounds, `--create-evaluator` writes the first one from the task |
| HyProver: conjectures → proof Workers → independent fresh-context review → reviewed lemmas as premises | `hyra-prover` skill |

## The wiki: `hyra-wiki` (optional skill)

[`skills/hyra-wiki/`](skills/hyra-wiki/) is a harness-neutral port of Ignat Romanov's MIT-licensed
[llm-obsidian-wiki](https://github.com/ignromanov/llm-obsidian-wiki) (Karpathy's LLM Wiki pattern): an Obsidian-compatible
markdown vault that agents compile and maintain, with four roles (researcher, scribe, advisor, curator), cited typed pages,
confidence and supersession. Upstream is a Claude Code plugin; this version needs no plugin system, slash commands, or Obsidian
app, so it works wherever `.agents/skills` does (Codex, Antigravity, OpenCode, Pi/omp, Cursor, Amp, Claude Code, ...).
See [`skills/hyra-wiki/NOTICE.md`](skills/hyra-wiki/NOTICE.md) for exactly what changed and which upstream bugs were fixed.

**Together with Hyra:** wiki = outside knowledge with sources; Hyra Books = lessons from our own experiments with evidence.
`hyra wiki setup --vault <path>` lets missions search the wiki (read-only); `hyra wiki export <books-id>` sends verified
lessons into `wiki/_drafts/` (approval-gated) for the wiki's curator to promote. Hyra workers never write to the vault.
Install it with the rest, or alone: `./install-toolkit-global.sh --skills hyra,hyra-wiki`. Code: [`hyra/wiki.py`](hyra/wiki.py).

## Layout

```
hyra/                         the harness (Python package, CLI entry point hyra.cli:main)
skills/hyra/SKILL.md          Overall role + entry point (harness-neutral)
skills/hyra/roles/*.md        Mission / Worker / Reviewer protocols (also Claude Code subagent definitions)
skills/hyra/references/cli.md full CLI reference, harness presets
skills/hyra/scripts/hyra      launcher used by agents (finds hyra/)
skills/hyra-loop/ hyra-prover/ hyra-retro/   companion skills
skills/hyra-wiki/             optional LLM-wiki skill (roles/, workflows/, scripts/, references/, NOTICE.md, LICENSE)
tests/test_e2e.py             end-to-end tests
tests/wiki/                   upstream wiki smoke tests (init, migrate), run from test_e2e
pyproject.toml  install-cli.sh  install-toolkit-local.sh  install-toolkit-global.sh
scripts/build_bundle.py       rebuilds skills/hyra/scripts/hyra.pyz from hyra/
docs/self-hosting.md          ledger server + ntfy on a tailnet device
```

Per-project state lives in `<project>/.hyra/`. The shared library is `~/.hyra/library/` (override
with `HYRA_LIBRARY`).

## Code tour

| module | lines | what it does |
|---|---|---|
| [`hyra/store.py`](hyra/store.py) | ~300 | Project discovery (`.hyra/`), config defaults, **harness presets**, the SQLite schema (missions, tasks, task_notes, evidence, board, messages, approvals, reviews, agent_state, sessions), WAL + `BEGIN IMMEDIATE` transactions so many agents can share one queue, memory-file template. |
| [`hyra/core.py`](hyra/core.py) | ~650 | The Hyra 2.0 mechanics. Missions (close refused while results are unreviewed); the **task queue** (priority → deps → approval gate → resource capacity → sleeping/wake-up; atomic claim); **evidence** (hashing, copying, git HEAD); the Board; messages that require a response; **approvals** (agents can't grant); **blind review** (evidence → blind notes → claim → immutable verdict, weakest verdict wins, refutations message the Mission); per-agent **briefings** with a read cursor. |
| [`hyra/books.py`](hyra/books.py) | ~230 | **Books** knowledge base: entries need method/rationale/applicability/evidence; observation → verified (independent reproduction required) → convention (human approval); revise/demote; approval-gated sync to the shared library; **skill distillation** and publishing. |
| [`hyra/eb.py`](hyra/eb.py) | ~380 | **Experience Bank.** `solve.sh` solutions scored by a versioned `eval.sh`; **sandboxed** evaluation (OS sandbox, sanitized environment, no credentials); atomic ids and locked appends for parallel agents; lineage; `inspire` bundles; the **inspiration queue** (atomic take); evaluator upgrades + rescoring. |
| [`hyra/ebloop.py`](hyra/ebloop.py) | ~190 | **The autonomous Hyra 1.0 pipeline** (`hyra eb loop`): Context Agent → queue → Proposal Agents (semaphore) → scoring, with budgets, drain, bilevel evaluator rounds and evaluator creation. |
| [`hyra/server.py`](hyra/server.py), [`hyra/remote.py`](hyra/remote.py) | ~300 | **Optional ledger server** (sealed claims, server-side blind review, human-only approvals, scoped tokens, audit trail) and its client. Inactive unless a project runs `hyra server connect`. |
| [`hyra/runner.py`](hyra/runner.py) | ~420 | **Scheduler** (`hyra run`). Wakes Missions (first run, due time or condition, mail, any task finished), the unattended Overall, and retrospectives for closed missions; notifies the human; claims tasks and launches headless sessions per harness; harnesses with agent files get `--agent <role>`, the rest get the role embedded; reaps dead/hung/lingering sessions and requeues or fails tasks; `--task` / `--mission` single dispatch. |
| [`hyra/install.py`](hyra/install.py) | ~230 | `hyra setup`: CLI install, and the toolkit install (global: per-harness dirs, linked; local: project dirs, copied), named agents for every harness that reads agent files, uninstall. |
| [`hyra/wiki.py`](hyra/wiki.py) | ~140 | Optional bridge to a wiki vault: read-only `search`/`show`, approval-gated `export` of verified Books as `wiki/_drafts/` pages in the wiki's schema. |
| [`hyra/doctor.py`](hyra/doctor.py) | ~220 | `hyra doctor` (what's installed, what actually runs, what's misconfigured) and `hyra config` (layered config with validation). |
| [`hyra/cli.py`](hyra/cli.py) | ~340 | argparse wiring for every command (`hyra --help`). |

## Per-harness details

Skills (`SKILL.md`) are discovered from `.agents/skills` by nearly everything; Claude Code additionally uses `.claude/skills`.
**Named agents** (so a harness can dispatch `hyra-worker`, `wiki-advisor`, ... by name) use markdown files in each harness's own directory.
The role files are plain `name` + `description` frontmatter and a prompt body, which all of these accept unchanged
(no `model` or `tools` keys: Antigravity silently skips a file with an invalid `model`):

| harness | skills | named agent files (global / project) | headless preset |
|---|---|---|---|
| Claude Code | `~/.claude/skills` | `~/.claude/agents` / `.claude/agents` | `claude -p --agent <role>` |
| OpenCode | `~/.agents/skills` | `~/.config/opencode/agents` / `.opencode/agents` | `opencode run --agent <role>` |
| Antigravity CLI (`agy`) | `~/.gemini/antigravity-cli/skills` | `~/.gemini/config/agents` / `.agents/agents` | `agy --mode auto --agent <role> -p=<prompt>` |
| Kimi Code | `~/.agents/skills` | `~/.agents/agents` / `.agents/agents` | `kimi --yolo --agent <role> -p <prompt>` |
| oh-my-pi (`omp`) | `~/.agents/skills` (its `agents` provider) | `~/.omp/agent/agents` / `.omp/agents` | `omp -p <prompt>` |
| Pi | `~/.agents/skills` | none (by design) | `pi -p <prompt>` |
| Codex | `~/.agents/skills` | none | `codex exec --sandbox workspace-write` |

**Do the agent files matter?** Only as a convenience. They let an interactive session dispatch a role by name, and a native
`--agent` launch loads the role as the session's system prompt (cleaner and faster on Claude). Everything works without them:
when a harness has no agent files (Codex, Pi) or they aren't installed, `hyra run` embeds the same role protocol in the prompt,
which is how all three roles were verified live on Codex.

Claude subagents can't spawn subagents, so an *interactive* Claude session can't host a separate Mission; use the daemon
(`hyra run`) or `hyra run --mission <slug>`, which start each role as its own top-level session.

## Server mode (optional hardening)

The ledger is **one SQLite file behind a small stdlib HTTP process** (`hyra server serve`), so putting it somewhere is just: copy one
self-contained file, run it as a service, reach it over the tailnet. `hyra server up` does all of that over ssh, on any Linux or macOS
device on your tailnet; you don't have to think about the machine:

```bash
hyra server up                    # picks a device for you (never this one), deploys, saves the connection, connects this project
hyra server up nitez@APOTHEOSIS  # or name one (the username can differ per machine)
hyra server where                 # what's deployed, and is it healthy
hyra server down                  # stop it over ssh (data kept);  --purge --yes also deletes it
hyra config set --user ledger_hosts '["noctezeta@blend","nitez@APOTHEOSIS","isagi@fedora"]'   # once: your machines and usernames
```
**Auto-selection:** your `ledger_hosts` first, then online Linux devices owned by you (before macOS laptops), then macOS; phones, Windows,
offline machines, devices shared by other tailnet users, and this machine are skipped. Each candidate is probed over ssh (`BatchMode`,
strict host keys, so an unseen host is skipped with "run `ssh user@host` once to trust it", never trusted silently).
`hyra server up --dry-run` shows the choice without changing anything. **On the target:** `systemd --user` (Linux) or launchd (macOS)
keeps it running, else a background process; it's published with `tailscale serve` (HTTPS, real certificate) when allowed, otherwise
bound to the tailnet address. First-run tokens are read over ssh once, deleted from the server, and saved in `~/.hyra/ledger.json`
(mode 600): the **runner** token is used only by the scheduler (no exported variable needed, and it's stripped from every agent session)
and the **human** token only from a real terminal. No container is needed; an always-on Linux machine is the better host (a Mac
LaunchAgent only runs while you're logged in).

What it enforces once a project is connected:
- **Worker claims are sealed on the server.** The project's database holds only a marker (checked down to the WAL file).
- **Blind review is enforced by the server.** A reviewer's token can't fetch the claim until it has recorded its blind assessment;
  verdicts are immutable; the server keeps an audit trail (seal, read, blind, reveal, verdict).
- **Approvals are decided only with the human token.** The scheduler asks the server before running anything that needed approval,
  so editing the local database can't release gated work.
- **Each agent session gets its own scoped token**, minted by the scheduler. It fails closed: if the server is unreachable, completing a
  task is refused.

Tested live over a tailnet (omp worker and omp blind reviewer on this Mac, ledger on a Linux box): the audit trail showed the seal, then
the reviewer's blind assessment, *then* the claim reveal; an early read was refused through the CLI and the raw API. Codex needs network
access to reach the ledger, which the runner grants it only when a ledger is configured. For the deployer itself: auto-selection was run
(dry run) against a real tailnet, the full up/where/down lifecycle runs on one machine, and the launchd service was started and removed
for real; the **systemd and `tailscale serve` steps on a remote machine are written and unit-tested but have not been run for real yet**.
**What it doesn't stop:** an agent that hunts for credentials (the saved tokens file, parent processes), and the worker's raw session log
(`.hyra/logs/`) and execution notes, which stay local and can contain its conclusion. It makes the secrets unreachable by touching the
project's files, not by a determined attacker. It's not needed for agents that follow instructions; it's defense in depth.
**Why it has to be another machine.** Your agents run on this computer as you, so anything stored here, even in a hidden folder, is readable
by them: the sealed claims, the approval ledger and the human token included. On a different machine those sit behind a network
boundary: an agent needs a credential to reach them, not just a file path. That is what closes the blind-review gap (an agent can't
open a file to read the claim) and the approval gap. A project stays tied to its ledger (its sealed claims live only there), so use the
same one over time; many projects can share it. **New repos connect automatically:** `hyra init` connects to the ledger you deployed
(`hyra init --no-ledger` for a purely local project; if the ledger is unreachable it says so and the project is local).

**What it serves:** not files. It answers a small JSON API over HTTP (health, tokens, claims, reviews, approvals, stats) and applies the rules
(who may read what, in which order). The only state is one SQLite file on the server; clients never download it.
**Housekeeping (automatic):** session tokens expire after 24 hours (`hyra server token --ttl-hours N` for a longer interactive session; the
scheduler's per-agent tokens are minted fresh each session); every 6 hours the server drops long-expired tokens and audit rows older than 180
days (`--audit-days`); it keeps no request log. **Never deleted automatically:** sealed claims, reviews and approvals, which are your research
record and tiny (on the order of a few KB per task). `hyra server where` shows the size and counts.

**Best practices.**
- **Bring it up once, not per repo.** One ledger serves all your projects (each is partitioned by its own id). Run `hyra server up` once per
  tailnet; every `hyra init` after that connects the new repo by itself. Re-run `hyra server up` only to upgrade it after updating hyra
  (it copies the new bundle, restarts, and keeps the data and tokens) or to move it.
- **Use it where the guarantees matter**: unattended multi-day runs, real compute approvals, results others will rely on. For quick or
  exploratory repos, local mode (no ledger) is simpler and fine.
- **Host it on an always-on Linux machine** you can ssh into; list your machines once in `ledger_hosts`.
- **Stay on the tailnet while a connected project is active.** If the ledger is unreachable, completing a task is refused (it fails closed,
  nothing is recorded). Don't disconnect a project that has sealed claims: their text lives only on the server.
- **Back it up to a git repo** (it's the only copy of the sealed claims). Make a **private** repo, then once:
  `hyra server backup --init ~/hyra-ledger-backup --remote git@github.com:you/hyra-ledger-backup.git`, and afterwards just
  `hyra server backup` (commit + push; "no changes" if nothing happened). To make `hyra run` do it for you:
  `hyra config set --user ledger_backup_every_hours 6`. The snapshot is taken on the server with SQLite's online backup (consistent
  while it runs; copying the live file isn't), written as SQL text so git can diff and compress it, and **leaves out the tokens**.
  The repo contains your research conclusions in plain text, so keep it private.
- **Restore** (new machine, lost disk): `hyra server up` (an empty ledger is fine), then `hyra server restore --from ~/hyra-ledger-backup --yes`.
  It replaces the data (the old database is kept aside on the server), mints **new** tokens and saves them; projects keep their ids and
  read their sealed claims again. Moving the ledger to another host is the same: `hyra server down`, `hyra server up user@newhost`, restore.
- **If a token leaks:** `hyra server up --rotate-tokens`. Treat `~/.hyra/ledger.json` like an ssh key.

To deploy to a specific machine: `hyra server up noctezeta@blend` (that's the whole command; run it in your terminal, or `! hyra server up noctezeta@blend` inside an agent session).
Manual setup instead of `hyra server up`: [`docs/self-hosting.md`](docs/self-hosting.md).

## Phone notifications

Step-by-step self-hosting of ntfy on a tailnet device (so the text never leaves it): [`docs/self-hosting.md`](docs/self-hosting.md).

`notify_cmd` in `.hyra/config.json` is a shell command the daemon runs whenever a message for you arrives (`hyra msg send --to human`),
with the text in `$HYRA_NOTIFY_TEXT`. It sends nothing by itself, so you choose the channel. Nothing is configured by default.
```jsonc
// ntfy.sh: free push; install the ntfy app, subscribe to your topic. The topic name is the only secret, and the text passes through their server.
"notify_cmd": "curl -s -d \"$HYRA_NOTIFY_TEXT\" https://ntfy.sh/CHOOSE-A-LONG-RANDOM-TOPIC"
// Private variant: run your own ntfy on a tailnet device and point the app at it (the phone must be on the tailnet).
"notify_cmd": "curl -s -d \"$HYRA_NOTIFY_TEXT\" http://blend:8080/hyra"
// iMessage via the Messages app on your Mac (signed in to iMessage; use your own number):
"notify_cmd": "osascript -e 'tell application \"Messages\" to send (system attribute \"HYRA_NOTIFY_TEXT\") to buddy \"+15551234567\" of (1st service whose service type = iMessage)'"
// Mac-only banner (not your phone): "osascript -e 'display notification (system attribute \"HYRA_NOTIFY_TEXT\") with title \"hyra\"'"
```

## Config (`.hyra/config.json`)

- `harness`: default agent CLI for headless sessions (`claude`, `codex`, `agy`, `opencode`, `amp`, or one you define in `harness_cmds`).
- `resources`: capacities, e.g. `{"workers": 4, "gpu": 8}`. Each running task uses 1 worker plus whatever it requests.
- `approval_rules`: e.g. `{"gpu": 1}` means any task requesting ≥1 GPU needs human approval.
- `session_max_minutes` (240), `settle_grace_seconds` (90): stop sessions that hang or linger after their task is settled.
- `harness_models`: default model per harness, e.g. `{"agy": "gemini-4-hydrogen-medium", "opencode": "provider/model"}`. Put machine-wide choices in `~/.hyra/config.json`; the project file overrides it key by key.
- `overall_agent` (true), `overall_min_gap_minutes` (60), `overall_stall_hours` (6), `auto_retro` (true), `auto_check_tasks` (true), `notify_cmd` (a shell command run with `$HYRA_NOTIFY_TEXT` when a message for the human arrives), `approvals_require_tty` (false: if true, `approval grant|deny` only works from a real terminal, which an agent's shell tool is not).
- `max_missions_running`, `max_attempts`, `poll_seconds`.
- Experience Bank: `eb_sandbox` (`auto` | `podman` | `docker` | `none`), `eb_sandbox_image` (a local image with bash for the container modes), `eb_sandbox_prefix` (full override), `eb_env_passthrough` (env vars a sandboxed evaluator may see), `eb_drain_seconds`.

## Limits (honest)

- **By default, blinding and approvals are CLI rules, not OS boundaries.** The claim can't be read through the CLI until a blind
  assessment is recorded (a blind reviewer's briefing, Board reads and mission view hide the reviewed task's entries), and
  headless sessions carry a non-human role that refuses `approval grant`. An agent that deliberately opens `.hyra/hyra.db` can
  still see everything, and an interactive agent could run `approval grant` without asking you. Two ways to harden, both opt-in:
  `approvals_require_tty: true` (grants need a real terminal), and **server mode** below, which moves both rules onto another machine.
- Headless presets use autonomous permission flags, because unattended agents can't answer prompts (Claude `auto`, Codex
  `workspace-write` sandbox, agy `--mode auto`, Kimi `--yolo`, omp/Pi their default approval mode, Amp `--dangerously-allow-all`).
  Tighten them in `harness_cmds` if needed.
- The evaluation sandbox needs `sandbox-exec` (macOS), `bwrap` (Linux), or Podman/Docker; otherwise it falls back to a scratch
  copy and says so. Verified with an evaluator that tries to escape: macOS `sandbox-exec`, Linux `bwrap`, rootless Podman and Docker
  (on that machine `docker` was Podman's emulation) all blocked writes outside the scratch dir, network, and reading project state.
- Resource accounting covers tasks Hyra launches. External cluster jobs are tracked through a condition command you give
  (`--until-cmd`), not through any scheduler integration.
- Verification status (2026-11-17):

  | harness | skills discovered | agent files | roles run headless (`hyra run`) |
  |---|---|---|---|
  | Claude Code | ✅ live | ✅ live | ✅ Worker, both `--agent` and embedded |
  | Codex | ✅ live | n/a | ✅ **Worker, blind Reviewer, Mission**, all live |
  | Antigravity (`agy`) | ✅ live | ✅ live (needs the explicit `tools` list we generate) | ✅ Worker, native `--agent`, Gemini 4 Hydrogen |
  | OpenCode | ✅ live | ✅ validated with `opencode debug agent` | ✅ Worker, native `--agent`, free `opencode/big-pickle` model |
  | oh-my-pi (`omp`) | ✅ live (5 skills) | ✅ live (all 12 agents listed) | ✅ Worker and blind Reviewer, also against the ledger server |
  | Pi | per its docs (omp is its fork and shares the shape) | none by design | per its docs; not installed here |
  | Kimi Code | per its docs | per its docs | per its docs; **not installed here, not tested (you skipped it)** |
  | Cursor, Qwen, Amp, Copilot, Goose, Droid | skills linked only | not supported | untested |

  The whole test suite (31 tests, including a live ledger server) also passes on Linux with Python 3.14.

- Live runs found and fixed real defects: the Board leaked a worker's claim to a blind reviewer (found by a Codex reviewer), and
  parallel Proposal Agents were handed the same solution id (found by the autonomous-loop run), an Antigravity agent file without a
  `tools` list had no shell (found by running it), and rootless Podman failed when I stripped `HOME`/`XDG_RUNTIME_DIR` (found on Linux).
