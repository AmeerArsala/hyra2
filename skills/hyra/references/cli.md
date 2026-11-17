# hyra CLI reference

Global: `hyra [--as <agent>] <command>`. If `hyra` isn't on PATH, run `python3 <hyra skill dir>/scripts/hyra`.
Install: `hyra setup --cli` (CLI only) or `hyra setup --toolkit local|global` (skills; see the README; `--uninstall` reverses either). Agent ids are `overall`, `mission:<slug>`, `task:<id>`.
Defaults to `$HYRA_AGENT`, else `overall`. The project is found by walking up from cwd to `.hyra/`,
or from `$HYRA_PROJECT`. The shared library lives at `$HYRA_LIBRARY` (default `~/.hyra/library`).

## Orientation
| command | purpose |
|---|---|
| `hyra init [--project NAME] [--goal "..."]` | create `.hyra/` here |
| `hyra status` | missions, queue, ready tasks, approvals, pending mail, latest board |
| `hyra brief <agent> [--no-mark]` | wake-up briefing: memory, inbox, tasks, board since last brief |
| `hyra guide` | print the Overall skill |

## Missions
`hyra mission new <slug> --title T --problem P [--priority 50]` ·
`mission list [--all]` · `mission show <slug>` · `mission set <slug> [--status active|paused] [--priority N]` ·
`mission wake <slug> [--at +3h|ISO] [--until-file PATH] [--until-cmd CMD] --reason R` · `mission close <slug> --conclusion C [--accept-unreviewed]`

Close is refused while tasks are open, or while done experiment/analysis/build tasks are unreviewed
(unless `--accept-unreviewed`, which gets stamped into the conclusion).

## Task queue
| command | notes |
|---|---|
| `task add --mission M --title T --spec S [--kind explore\|experiment\|analysis\|build\|review\|other] [--priority N] [--after 3,4] [--resource gpu=2] [--model M] [--harness H] [--needs-approval]` | resources at or above `approval_rules` thresholds → `awaiting_approval` |
| `task list [--status S] [--mission M]` / `task show <id>` | |
| `task next [--worker W] [--kind K]` / `task claim <id>` | atomic claim; respects mission priority, task priority, deps, capacity, sleeping |
| `task note <id> "text"` | the action log reviewers read |
| `task sleep <id> [--until +2h] [--until-file PATH] [--until-cmd CMD] --reason R` | parks the task until the time, or as soon as the file exists / the command exits 0 (event-driven; `--until` is then the fallback deadline, default 24h); resumes with its notes |
| `task complete <id> --summary S --evidence PATH\|commit:REV\|url:..\|eb:NAME/SID\|task:N ...` | evidence required (`--no-evidence` is refused for experiment/analysis/build tasks; it exists for explore/other note tasks). Files ≤2MB are hashed and copied to `.hyra/evidence/<id>/`; the git HEAD is recorded automatically |
| `task evidence <id> REF... [--note]` | attach more |
| `task fail <id> --reason R [--requeue]` · `task cancel <id> --reason R` · `task release <id>` | |

Statuses: `queued`, `awaiting_approval`, `running`, `sleeping`, `done`, `failed`, `cancelled`.

## Research Board & messages
`board post --type idea|finding|evidence|decision|note|review|retro "text" [--mission M] [--task N] [--ref R]` ·
`board ideas [--mission M] [--all]` (ideas and whether each is untested / testing / handled, derived from refs and task specs) ·
`board read [--mission M] [--since ID] [--type T] [--limit N]`

Routine updates go on the Board and are read asynchronously. Anything that needs action goes as a message:
`msg send --to overall|human|mission:<slug>|task:<id>|mission-workers:<slug> --subject S --body B [--requires-response]` ·
`msg inbox [agent] [--all]` · `msg reply <id> "text"`

## Approvals
`approval request --kind compute|knowledge-sync|convention|other --what W [--task N] [--ref R]` ·
`approval list [--all]` · `approval grant|deny <id> [--by human:NAME] [--note N]`.
Agents running with `HYRA_ROLE=worker|reviewer|mission` (headless) cannot grant.

## Blind review
`review request <task> [--focus F] [--model M] [--harness H]` creates a review task (a different harness = an independent vendor's perspective).
Then the reviewer runs `review evidence <rid>` (problem + evidence, claim withheld) →
`review blind <rid> --notes` → `review claim <rid>` → `review verdict <rid> --verdict supported|narrower|refuted|inconclusive --notes`.
Verdicts are immutable. The target's combined verdict is the weakest one received. narrower/refuted
verdicts message the Mission and require a response.

## Memory (hierarchical)
Level 0 is `memory/<agent>/index.md` (`memory path [agent]`; keep it ~25 lines: understanding, hypotheses, ruled out, next steps).
Level 1 are nodes: `memory note <understanding|hypotheses|ruled-out|next> "<title>" --text T [--summary S] [--agent A]` (rewrites if it exists) ·
`memory tree [agent]` · `memory show [agent] [--node PATH]`. Briefings show the index plus the node tree; open nodes on demand.

## Books (knowledge base)
`books add --title T --method M --rationale R --applies-when A --evidence E... [--tier observation|convention] [--tags]`
`books search [query] [--tier]` · `books show <id>` · `books promote <id> --to verified --evidence <independent repro>` ·
`books promote <id> --to convention` (approval) · `books revise <id> --note N [--evidence E] [--demote]` ·
`books sync <id>` (approval; copies to the shared library used by every project)

## Skills
`skill new <name> --description D [--from-books id,id]` → `.hyra/skills/<name>/SKILL.md` draft ·
`skill list` · `skill publish <name>` (approval; copies to `~/.hyra/library/skills` and links it into every installed harness's skills dir)

## Wiki bridge (optional, llm-obsidian-wiki vault)
`wiki setup --vault PATH [--cluster NAME]` (once per project; or env `HYRA_WIKI_VAULT`) ·
`wiki search QUERY [--limit N] [--all] [--drafts]` (read-only; skips deprecated/superseded unless `--all`) ·
`wiki show PAGE` · `wiki export <books-id> [--type concept|decision|comparison|synthesis]` (verified/convention
only; approval; writes `wiki/_drafts/<slug>.md` only, never overwrites or touches `raw/`).

## Experience Bank (Hyra 1.0 loop)
`eb loop NAME [--proposers N] [--max-submissions N] [--minutes M] [--target S] [--patience N] [--rounds R] [--harness H] [--dry-run] [--create-evaluator --task T --direction D]` (the autonomous pipeline) ·
`eb status NAME` · `eb inspiration add NAME --text T [--ref sid] [--file F]` / `take NAME` / `list NAME [--all]` ·
`eb init NAME --evaluator eval.sh|DIR --task "..." [--direction max|min] [--timeout 600]` ·
`eb submit NAME SOLUTION_DIR [--parent sid] [--note]` · `eb top NAME [-n]` · `eb inspire NAME [-k 4]` ·
`eb evaluator NAME --new eval.sh|DIR --note N` · `eb rescore NAME [--top 5]` · `eb list`

Evaluator contract: `bash eval.sh <solution_dir>` prints `SCORE: <number>` (last wins) and optionally
`METRIC name: value`. Exit code 0 is required. Each run uses a fresh scratch copy in an OS sandbox when available
(`eb_sandbox`: `auto` = macOS `sandbox-exec` / Linux `bwrap`, `docker`, `none`; `eb_sandbox_prefix` overrides), with a sanitized environment.

## Scheduler and harnesses
`hyra run [--max-workers N] [--once] [--dry-run] [--harness H] [--model M]` runs the scheduler loop.
`hyra run --task <id> [--harness H]` / `hyra run --mission <slug>` run one task / one Mission in its own fresh headless session and wait.
`hyra run --overall` runs one unattended Overall session now.

Each headless session gets its role protocol (`roles/*.md`) embedded in the prompt, with `hyra`
on PATH and `HYRA_PROJECT/HYRA_AGENT/HYRA_ROLE/HYRA_TASK` set, so any agent CLI can play any role.
The harness comes from the task's `--harness`, falling back to config `harness` (default `claude`).

| harness | command (`{model}` / `{agent}` flags dropped if unset) |
|---|---|
| `claude` | `claude -p --permission-mode auto --agent hyra-<role> --model {model} {prompt}` (role loaded natively from `~/.claude/agents`; embedded in the prompt if not installed) |
| `codex` | `codex exec --sandbox workspace-write --skip-git-repo-check -m {model} {prompt}` (no agent files: role embedded; sandbox allows no network) |
| `agy` | `agy --mode auto --agent <role> --model {model} -p={prompt}` (Antigravity CLI; `-p` must carry its prompt as `-p=...`; `--mode auto` isn't available on every account, then add `--dangerously-skip-permissions` via `harness_cmds`) |
| `kimi` | `kimi --yolo --agent <role> -m {model} -p {prompt}` (Kimi Code; `--yolo` = ask only for risky actions, `--auto` = never ask) |
| `omp` | `omp -p --model {model} {prompt}` (oh-my-pi; `-p` is boolean, prompt positional; default approval mode is yolo) |
| `pi` | `pi -p --model {model} {prompt}` (Pi; same shape as omp) |
| `opencode` | `opencode run --agent <role> -m {model} {prompt}` (model as provider/model) |
| `amp` | `amp --dangerously-allow-all -x {prompt}` |

`{agent}` flags are used only when that harness has the role's agent file installed (see the README's per-harness table); otherwise the role protocol is embedded in the prompt.
`harness_models` in config sets a default model per harness.

Override or add harnesses in `.hyra/config.json`, e.g.
`"harness_cmds": {"codex": ["codex", "exec", "--sandbox", "danger-full-access", "-m", "{model}", "{prompt}"]}`.
Unattended behaviour: `overall_agent`, `overall_min_gap_minutes`, `overall_stall_hours`, `auto_retro`, `auto_check_tasks`,
`notify_cmd`, `approvals_require_tty`. Machine-wide defaults (e.g. `harness_models`) can live in `~/.hyra/config.json`.
Other config keys: `max_missions_running`, `max_attempts`, `poll_seconds`, `session_max_minutes`,
`settle_grace_seconds`, `resources`. Session logs go to `.hyra/logs/`.

## Ledger server (optional hardening; see the README)
`server serve [--data DIR] [--host H] [--port P]` (on the server machine; prints the human and runner tokens once) ·
`server connect --url URL [--project ID]` (per project) · `server status` · `server token --role overall|mission|worker|reviewer|retrospective [--scope S]`
(needs the human token, from `HYRA_HUMAN_TOKEN` or a prompt; for an interactive session, export the result as `HYRA_TOKEN`).
In server mode: `task complete` seals the claim on the server (and fails if it can't); `review blind|claim|verdict` and `approval grant|deny`
are decided by the server; `hyra run` needs `HYRA_RUNNER_TOKEN` and gives each agent its own scoped `HYRA_TOKEN`.

## Config and diagnostics
`doctor` (works anywhere: Python, CLI on PATH, skills per harness, agent CLIs that actually run, sandbox, and inside a project config problems, wiki, ledger, tokens) ·
`config show` (effective values with their source) · `config set KEY VALUE [--user] [--force]` (JSON or text; dotted keys like `harness_models.opencode`; typos and wrong types refused) ·
`config unset KEY [--user]` · `config validate` · `config path`. Layers: defaults < `~/.hyra/config.json` < `<project>/.hyra/config.json`.
`setup --toolkit global|local --agents-only` registers only the named agent files. `server serve ... --no-print-tokens | --rotate-tokens`.
