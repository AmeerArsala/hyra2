# hyra CLI reference

Global: `hyra [--as <agent>] <command>`. Agent ids are `overall`, `mission:<slug>`, `task:<id>`.
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
`mission wake <slug> --at +3h|ISO --reason R` · `mission close <slug> --conclusion C [--accept-unreviewed]`

Close is refused while tasks are open, or while done experiment/analysis/build tasks are unreviewed
(unless `--accept-unreviewed`, which gets stamped into the conclusion).

## Task queue
| command | notes |
|---|---|
| `task add --mission M --title T --spec S [--kind explore\|experiment\|analysis\|build\|review\|other] [--priority N] [--after 3,4] [--resource gpu=2] [--model sonnet] [--needs-approval]` | resources at or above `approval_rules` thresholds → `awaiting_approval` |
| `task list [--status S] [--mission M]` / `task show <id>` | |
| `task next [--worker W] [--kind K]` / `task claim <id>` | atomic claim; respects mission priority, task priority, deps, capacity, sleeping |
| `task note <id> "text"` | the action log reviewers read |
| `task sleep <id> --until +2h --reason R` | parks the task; it becomes claimable again at that time (resumes with its notes) |
| `task complete <id> --summary S --evidence PATH\|commit:REV\|url:..\|eb:NAME/SID\|task:N ...` | evidence required (`--no-evidence` only for pure planning). Files ≤2MB are hashed and copied to `.hyra/evidence/<id>/`; the git HEAD is recorded automatically |
| `task evidence <id> REF... [--note]` | attach more |
| `task fail <id> --reason R [--requeue]` · `task cancel <id> --reason R` · `task release <id>` | |

Statuses: `queued`, `awaiting_approval`, `running`, `sleeping`, `done`, `failed`, `cancelled`.

## Research Board & messages
`board post --type idea|finding|evidence|decision|note|review|retro "text" [--mission M] [--task N] [--ref R]` ·
`board read [--mission M] [--since ID] [--type T] [--limit N]`

Routine updates go on the Board and are read asynchronously. Anything that needs action goes as a message:
`msg send --to overall|human|mission:<slug>|task:<id>|mission-workers:<slug> --subject S --body B [--requires-response]` ·
`msg inbox [agent] [--all]` · `msg reply <id> "text"`

## Approvals
`approval request --kind compute|knowledge-sync|convention|other --what W [--task N] [--ref R]` ·
`approval list [--all]` · `approval grant|deny <id> [--by human:NAME] [--note N]`.
Agents running with `HYRA_ROLE=worker|reviewer|mission` (headless) cannot grant.

## Blind review
`review request <task> [--focus F] [--model M]` creates a review task.
Then the reviewer runs `review evidence <rid>` (problem + evidence, claim withheld) →
`review blind <rid> --notes` → `review claim <rid>` → `review verdict <rid> --verdict supported|narrower|refuted|inconclusive --notes`.
Verdicts are immutable. The target's combined verdict is the weakest one received. narrower/refuted
verdicts message the Mission and require a response.

## Memory
`memory show [agent]` · `memory path [agent]` (then edit the markdown file). Sections: Current
understanding / Key hypotheses / Ruled out / Next steps.

## Books (knowledge base)
`books add --title T --method M --rationale R --applies-when A --evidence E... [--tier observation|convention] [--tags]`
`books search [query] [--tier]` · `books show <id>` · `books promote <id> --to verified --evidence <independent repro>` ·
`books promote <id> --to convention` (approval) · `books revise <id> --note N [--evidence E] [--demote]` ·
`books sync <id>` (approval; copies to the shared library used by every project)

## Skills
`skill new <name> --description D [--from-books id,id]` → `.hyra/skills/<name>/SKILL.md` draft ·
`skill list` · `skill publish <name>` (approval; copies to the library and symlinks into `~/.claude/skills`)

## Experience Bank (Hyra 1.0 loop)
`eb init NAME --evaluator eval.sh|DIR --task "..." [--direction max|min] [--timeout 600]` ·
`eb submit NAME SOLUTION_DIR [--parent sid] [--note]` · `eb top NAME [-n]` · `eb inspire NAME [-k 4]` ·
`eb evaluator NAME --new eval.sh|DIR --note N` · `eb rescore NAME [--top 5]` · `eb list`

Evaluator contract: `bash eval.sh <solution_dir>` prints `SCORE: <number>` (last wins) and optionally
`METRIC name: value`. Exit code 0 is required. Each run uses a fresh scratch copy (prefix with
`eb_sandbox_prefix` in config for real isolation, e.g. docker).

## Scheduler
`hyra run [--max-workers N] [--once] [--dry-run]`. Config keys: `agent_cmd`, `max_missions_running`,
`max_attempts`, `poll_seconds`, `resources`. Session logs go to `.hyra/logs/`.
