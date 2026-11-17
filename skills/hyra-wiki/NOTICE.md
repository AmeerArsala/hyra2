# Notice

hyra-wiki is derived from **llm-obsidian-wiki** by Ignat Romanov (https://github.com/ignromanov/llm-obsidian-wiki),
MIT License, copyright (c) 2026 Ignat Romanov (see `LICENSE`). Vendored from upstream commit
`b0530ebd43ad42300a28093204d031a84adb5eb2` (2026-05-28, v0.4.1).

Changes made for portability across agent harnesses:
- Plugin packaging removed (`.claude-plugin/`, `commands/`); `${CLAUDE_PLUGIN_ROOT}` replaced by `$WIKI_SKILL`.
- `agents/*.md` became `roles/*.md` (Claude-only frontmatter `model/color/tools` and `<example>` blocks dropped);
  `skills/*/SKILL.md` became `workflows/*.md`; slash commands replaced by plain scripts.
- Added `scripts/new-vault.sh` and `scripts/templates/_AGENTS.md`: the upstream init workflow called
  `init-vault.sh --name --path`, which the script doesn't accept, and no step created the promised CLAUDE.md.
- Fixed `scripts/promote-draft.sh`: it injected a second `status:` line into promoted pages (and could rewrite
  body lines); it now only edits the frontmatter.
- **Obsidian CLI no longer required.** Seven read-side scripts (search, stats, index, hubs, page-context, section-browse,
  find-unprocessed) shelled out to `obsidian vault=... <cmd>` and swallowed failures, so without the Obsidian desktop
  app running they silently returned empty results. They now use `scripts/lib/shim/obsidian`, a stdlib-Python stand-in
  that reads the markdown on disk (set `WIKI_USE_OBSIDIAN_CLI=1` to prefer a real CLI). Needs `python3`.
- `wiki-stats.sh` accepts `--vault <path>` as documented (it only took a positional path) and no longer prints
  each empty section's count twice (`grep -c ... || echo 0`).
- Docs corrected: index.md lives at the vault root; init no longer promises a CLAUDE.md that nothing created.
- Upstream smoke tests moved to `tests/wiki/` in this repo and retargeted.
