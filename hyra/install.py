"""Install Hyra's skills into every agent harness on this machine (and publish distilled skills).

Skills follow the open Agent Skills format (SKILL.md + resources), so one copy works everywhere;
it only has to be discoverable. ~/.agents/skills is the shared location (read by Codex and others);
harnesses with their own global directory get a symlink there too, if that harness is installed.
Directory table mirrors the `skills` CLI (vercel-labs/skills).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .store import skill_dir


def skill_dirs() -> list[Path]:
    env = os.environ.get("HYRA_SKILL_DIRS")
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p]
    home = Path.home()
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
    # (marker that the harness is installed, its global skills dir)
    table = [
        (home / ".agents", home / ".agents" / "skills"),          # shared: Codex, and other universal readers
        (claude, claude / "skills"),                              # Claude Code
        (home / ".gemini" / "antigravity", home / ".gemini" / "antigravity" / "skills"),          # Antigravity
        (home / ".gemini" / "antigravity-cli", home / ".gemini" / "antigravity-cli" / "skills"),  # Antigravity CLI (agy)
        (home / ".cursor", home / ".cursor" / "skills"),          # Cursor
        (cfg / "opencode", cfg / "opencode" / "skills"),          # OpenCode
        (cfg / "amp", cfg / "agents" / "skills"),                 # Amp
        (home / ".qwen", home / ".qwen" / "skills"),              # Qwen Code
        (home / ".copilot", home / ".copilot" / "skills"),        # GitHub Copilot
        (cfg / "goose", cfg / "goose" / "skills"),                # Goose
        (home / ".factory", home / ".factory" / "skills"),        # Droid
    ]
    return [d for marker, d in table if d == home / ".agents" / "skills" or marker.exists()]


def _link(src: Path, dest: Path, out: list[str]) -> None:
    if dest.is_symlink() or not dest.exists():
        if dest.is_symlink():
            if Path(os.readlink(dest)) == src:
                out.append(f"ok      {dest}")
                return
            dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(src)
        out.append(f"linked  {dest} -> {src}")
    else:
        out.append(f"skip    {dest} exists and is not a symlink")


def link_skill(src: Path, name: Optional[str] = None) -> list[str]:
    out: list[str] = []
    for d in skill_dirs():
        _link(src, d / (name or src.name), out)
    return out


def our_skills() -> list[Path]:
    return sorted(p for p in skill_dir().parent.iterdir()
                  if p.name.startswith("hyra") and (p / "SKILL.md").exists())


def setup(bin_dir: Optional[str], uninstall: bool) -> None:
    home = Path.home()
    bindir = Path(bin_dir or os.environ.get("HYRA_BIN_DIR", home / ".local" / "bin")).expanduser()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
    cli = skill_dir() / "scripts" / "hyra"
    pairs: list[tuple[Path, Path]] = [(cli, bindir / "hyra")]
    for s in our_skills():
        pairs += [(s, d / s.name) for d in skill_dirs()]
    if claude.exists():  # Claude Code subagent types; the role files double as agent definitions
        pairs += [(r, claude / "agents" / f"hyra-{r.stem}.md") for r in sorted((skill_dir() / "roles").glob("*.md"))]
    out: list[str] = []
    for src, dest in pairs:
        if uninstall:
            if dest.is_symlink() and Path(os.readlink(dest)) == src:
                dest.unlink()
                out.append(f"removed {dest}")
        else:
            _link(src, dest, out)
    print("\n".join(out) or "nothing to do")
    if not uninstall:
        (home / ".hyra" / "library" / "books").mkdir(parents=True, exist_ok=True)
        (home / ".hyra" / "library" / "skills").mkdir(parents=True, exist_ok=True)
        if str(bindir) not in os.environ.get("PATH", "").split(os.pathsep):
            print(f"note: add {bindir} to PATH (or call {cli} directly)")
        print("done. Ask your agent to use the hyra skill (Claude Code: /hyra <goal>).")
