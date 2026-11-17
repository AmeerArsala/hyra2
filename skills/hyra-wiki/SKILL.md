---
name: hyra-wiki
description: Karpathy-style LLM wiki (an Obsidian-compatible markdown vault an agent compiles and maintains: raw sources, typed cited pages, confidence, supersession) for any agent harness. Four roles: researcher (web to cited pages), scribe (verbatim capture of URL/PDF/text), advisor (answers only from the wiki, with [[wikilinks]]), curator (audit, fix, migrate). Use for "research X into the wiki", "save this URL/PDF", "what does the wiki say about X", "clean up the wiki", or to scaffold a vault. Optional companion to hyra; vendored from MIT-licensed llm-obsidian-wiki.
compatibility: Any Agent Skills harness with a shell (Claude Code, Codex, Antigravity, OpenCode, Pi/omp, Cursor, Amp, ...). Needs bash, git, python3. Optional capture tools - defuddle-cli (npm), trafilatura (uv/pip), yt-dlp, a PDF-to-text tool.
---

# hyra-wiki: an LLM-maintained knowledge vault

Karpathy's LLM Wiki pattern: *compile knowledge once into interlinked, cited markdown, then query the
compiled wiki*, rather than re-deriving from raw documents every time. The vault is plain markdown in git
(open it in Obsidian if you like). This skill is the portable port of
[llm-obsidian-wiki](https://github.com/ignromanov/llm-obsidian-wiki) (MIT, see `NOTICE.md`): no plugin
system, no slash commands, no harness-specific tools.

## Paths
`WIKI_SKILL` = the directory containing this `SKILL.md`. Set it before running anything:
`WIKI_SKILL="<that directory>"`. Scripts are `"$WIKI_SKILL/scripts/<name>.sh"`; workflows are
`"$WIKI_SKILL/workflows/<name>.md"`; roles are `"$WIKI_SKILL/roles/<role>.md"`.

The **vault** is a separate directory (default: ask; remember it in `$HYRA_WIKI_VAULT` or the project's
`.hyra/config.json`, or find it via `wiki.config.md`). Every script takes `--vault <path>`.

## Roles (pick by what the user asked; adopt one per task)

| User says... | Role file | Workflow(s) it follows | Writes to |
|---|---|---|---|
| "research X", "investigate Y", "write up Z" | `roles/researcher.md` | research, capture | `raw/` (append), new `wiki/` pages |
| "save this URL / PDF / GitHub link / text" | `roles/scribe.md` | capture | `raw/`, `wiki/sources/` |
| "what did we decide about X", "what does the wiki say" | `roles/advisor.md` | answer | `wiki/synthesis/` only |
| "clean up / audit / fix / migrate the wiki" | `roles/curator.md` | audit, maintain, migrate | `wiki/` (never `raw/`) |
| "create a wiki / new vault" | none | init | new vault |
| "wiki status" | none | status | nothing |

How to run a role: **read its role file, then the workflows it names, and follow them.** Roles are
personas with write boundaries; workflows are step-by-step playbooks that call the scripts.
- If your harness has subagents, run each role in a fresh one (Claude Code: the installer registers
  `wiki-researcher`, `wiki-advisor`, `wiki-curator`, `wiki-scribe` agent types). Otherwise adopt the role yourself.
- Roles never call each other. If one finds work outside its lane (e.g. the advisor finds a gap), it ends with
  a note ("researcher should investigate X") and the user decides. Run roles sequentially, not concurrently.
- Boundaries are enforced by these instructions, not by the harness, so follow them. In particular: only
  scribe/researcher write `raw/` (append-only); advisor reads `wiki/` only and says "not in the wiki" instead of guessing;
  every typed page cites sources.

## Quick start
```bash
WIKI_SKILL=<this directory>
"$WIKI_SKILL/scripts/new-vault.sh" --name my-wiki --path ~/wikis/my-wiki   # scaffold (AGENTS.md + CLAUDE.md included)
"$WIKI_SKILL/scripts/wiki-stats.sh" --vault ~/wikis/my-wiki                 # metrics
"$WIKI_SKILL/scripts/wiki-search.sh" --vault ~/wikis/my-wiki --query "auth"  # search + TLDR triage
```
Then talk naturally: "research nextjs auth options", "save this URL: ...", "what did we decide about auth?".

## With hyra (optional)
If the project also uses the `hyra` skill: `hyra wiki setup --vault <path>` lets missions search the wiki
(read-only), and `hyra wiki export <books-id>` sends verified lessons from experiments into `wiki/_drafts/`
(approval-gated). The **curator** then promotes drafts with `scripts/promote-draft.sh`. Wiki = outside knowledge with sources;
hyra Books = lessons from our own experiments with evidence. Hyra workers never write the vault.

## Reference
`references/architecture.md` (layers, contracts, frontmatter schema), `references/skills.md` and `agents.md`
(upstream docs), `references/migration-*.md`, `references/CHANGELOG.md`, `references/upstream-README.md`
(original plugin docs; install and slash-command parts don't apply here).

No Obsidian app is needed: read-side scripts use a bundled filesystem shim (`scripts/lib/shim/obsidian`); set `WIKI_USE_OBSIDIAN_CLI=1` to prefer a real Obsidian CLI.
Tools: `scripts/lib/check-deps.sh` reports missing optional capture tools. Web capture is local-only by default
(defuddle, then trafilatura); the cloud fallback (r.jina.ai) is opt-in.
