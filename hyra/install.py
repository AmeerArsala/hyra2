"""Installers: the `hyra` CLI, and the skills toolkit (global, or local to one project).

Skills follow the open Agent Skills format (SKILL.md + resources), so one copy works everywhere;
it only has to be discoverable. Global: ~/.agents/skills (Codex and other universal readers) plus
each installed harness's own dir. Local: <project>/.agents/skills, plus <project>/.claude/{skills,agents}
(Claude Code doesn't read .agents/) and <project>/.qwen/skills. Directory tables mirror the `skills`
CLI (vercel-labs/skills), which can also install the skills (`npx skills add`); this adds the CLI,
Claude subagent types, and uninstall.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipapp
import zipfile
from pathlib import Path
from typing import Optional

from . import __version__
from .store import HyraError, skill_dir


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


def _home_dirs():
    home = Path.home()
    return (home, Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")),
            Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")))


def harness_detected(harness: str) -> bool:
    """Is this agent harness installed (config dir or binary present)?"""
    home, cfg, claude = _home_dirs()
    marks = {"claude": [claude], "opencode": [cfg / "opencode"],
             "agy": [home / ".gemini" / "antigravity-cli", home / ".gemini" / "config"],
             "kimi": [Path(os.environ.get("KIMI_CODE_HOME", home / ".kimi-code"))],
             "omp": [home / ".omp"]}
    return any(m.exists() for m in marks.get(harness, [])) or (harness in ("kimi", "omp") and bool(shutil.which(harness)))


def agent_dirs(harness: str, root: Optional[Path] = None) -> list[Path]:
    """Where a harness reads user-defined named agents from: [global dir, project dir]. Role files are plain
    markdown with name/description frontmatter, which every one of these accepts. Not listed: Codex and Pi,
    which have no agent files (their roles run as headless sessions or are adopted in-session).
    Docs: Claude Code (~/.claude/agents), OpenCode (.opencode/agents), Antigravity (.agents/agents, ~/.gemini/config/agents),
    Kimi Code (.agents/agents, ~/.agents/agents), omp (.omp/agents, ~/.omp/agent/agents)."""
    home, cfg, claude = _home_dirs()
    table = {"claude": (claude / "agents", ".claude/agents"),
             "opencode": (cfg / "opencode" / "agents", ".opencode/agents"),
             "agy": (home / ".gemini" / "config" / "agents", ".agents/agents"),
             "kimi": (home / ".agents" / "agents", ".agents/agents"),
             "omp": (home / ".omp" / "agent" / "agents", ".omp/agents")}
    if harness not in table:
        return []
    g, proj = table[harness]
    return [g] + ([root / proj] if root else [])


# Antigravity gives an agent file with no `tools:` list a minimal toolset (no shell, no file writes: found live), so its
# files carry an explicit allowlist of documented tool names. Kimi has different tool names and reads the same project
# directory, so Kimi's files stay plain (all tools).
AGY_TOOLS = ["view_file", "replace_file_content", "grep_search", "run_command", "manage_task", "search_web", "read_url_content"]


def render_agent(text: str, renderer: str) -> str:
    if renderer != "agy" or re.search(r"^tools:", text.split("\n---", 1)[0], re.M):
        return text
    head, sep, rest = text.partition("\n---")
    return head + "\ntools:\n" + "".join(f"  - {t}\n" for t in AGY_TOOLS).rstrip("\n") + sep + rest


def native_agent_available(harness: str, name: str, root: Path) -> bool:
    """Is a usable named agent installed? For Antigravity a file without a tools list would cripple the session, so it
    doesn't count (the runner then embeds the role in the prompt and the default agent keeps all its tools)."""
    for d in reversed(agent_dirs(harness, root)):  # [global, project]: the project's file shadows the global one
        f = d / f"{name}.md"
        if f.exists():
            if harness == "agy" and not re.search(r"^tools:", f.read_text().split("\n---", 1)[0], re.M):
                return False  # a file that shadows ours without tools
            return True
    return False


def our_skills(only: Optional[list[str]] = None) -> list[Path]:
    found = sorted(p for p in skill_dir().parent.iterdir()
                   if p.name.startswith("hyra") and (p / "SKILL.md").exists())
    if only:
        bad = set(only) - {p.name for p in found}
        if bad:
            raise HyraError(f"unknown skill(s): {', '.join(sorted(bad))} (have: {', '.join(p.name for p in found)})")
        found = [p for p in found if p.name in set(only) | {"hyra"}]  # companions need the core skill
    return found


def role_agents(skills: list[Path]) -> list[tuple[Path, str]]:
    """Role files (<skill>/roles/*.md) with the agent name from their frontmatter. They double as
    Claude Code subagent definitions (hyra-worker, wiki-advisor, ...)."""
    out = []
    for sk in skills:
        for r in sorted(f for f in (sk / "roles").glob("*.md") if not f.name.startswith(".")) if (sk / "roles").is_dir() else []:
            m = re.search(r"^name:\s*(\S+)\s*$", r.read_text(encoding="utf-8").split("\n---", 1)[0], re.M)
            out.append((r, m.group(1) if m else f"{sk.name}-{r.stem}"))
    return out


def _ours(dest: Path) -> bool:
    """True if dest is something this installer may replace/remove."""
    if dest.is_symlink():
        return True
    if dest.is_file():
        return dest.stem in {n for _, n in role_agents(our_skills())}
    return dest.is_dir() and (dest / "SKILL.md").exists() and dest.name.startswith("hyra")


def _copy(src: Path, dest: Path, out: list[str]) -> None:
    if dest.exists() or dest.is_symlink():
        if not _ours(dest):
            out.append(f"skip    {dest} exists and isn't ours")
            return
        dest.unlink() if (dest.is_symlink() or dest.is_file()) else shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        shutil.copy2(src, dest)
    out.append(f"copied  {dest}")


def _remove(dest: Path, out: list[str]) -> None:
    if (dest.exists() or dest.is_symlink()) and _ours(dest):
        dest.unlink() if (dest.is_symlink() or dest.is_file()) else shutil.rmtree(dest)
        out.append(f"removed {dest}")


# ---------------------------------------------------------------------------------------------- the global CLI
# The `hyra` command is the one thing that is installed machine-wide, whatever way the skills were installed (per project
# or globally): a single self-contained copy in ~/.hyra/cli/hyra, linked from ~/.local/bin/hyra. Deleting a repo can't break
# it, and a repo's skills can be older or newer than it, which is what the version check below is for.
def cli_home() -> Path:
    return Path(os.environ.get("HYRA_CLI_HOME") or Path.home() / ".hyra" / "cli")


def parse_version(text: Optional[str]) -> Optional[tuple]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def running_pyz() -> Optional[Path]:
    """The bundle file this process is running from, if it is one (the global CLI, or a skill folder's hyra.pyz)."""
    for parent in Path(__file__).resolve().parents:
        if parent.is_file() and zipfile.is_zipfile(parent):
            return parent
    return None


def bundle_bytes() -> bytes:
    """This exact version of hyra as one self-contained file: the running bundle, or one built from the source tree."""
    rp = running_pyz()
    if rp:
        return rp.read_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(Path(__file__).parent, Path(tmp) / "hyra", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        out = Path(tmp) / "b.pyz"
        zipapp.create_archive(tmp, out, interpreter="/usr/bin/env python3", main="hyra.cli:main", compressed=True)
        return out.read_bytes()


def current_cli_dir() -> Optional[Path]:
    """A directory holding an executable `hyra` of the SAME version as this process, for agent sessions' PATH (so a session
    never runs a different version than the scheduler that launched it)."""
    env = os.environ.get("HYRA_CLI_DIR")
    if env:
        return Path(env)
    rp = running_pyz()
    return rp.parent if rp and (rp.parent / "hyra").exists() else None


def installed_cli(bindir: Optional[Path] = None) -> dict:
    """The `hyra` on PATH; failing that, our own global copy (installed, but its bin dir isn't on PATH yet)."""
    path = shutil.which("hyra")
    on_path = bool(path)
    if not path:
        for cand in ([bindir / "hyra"] if bindir else []) + [cli_home() / "hyra"]:
            if cand.exists():
                path = str(cand)
                break
    if not path:
        return {"path": None, "version": None, "on_path": False}
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        return {"path": path, "version": parse_version(r.stdout + r.stderr) if r.returncode == 0 else None, "on_path": on_path}
    except (OSError, subprocess.TimeoutExpired):
        return {"path": path, "version": None, "on_path": on_path}


def cli_state(inst: dict) -> str:
    mine, have = parse_version(__version__), inst["version"]
    if not inst["path"]:
        return "missing"
    if have is None:
        return "upgrade"  # an older hyra that can't even report its version
    return "ok" if have == mine else "upgrade" if have < mine else "newer"


def install_cli(bindir: Path, uninstall: bool, only_if_missing: bool, check: bool = False, force: bool = False) -> None:
    """Put this version of hyra at ~/.hyra/cli/hyra and link it from <bindir>/hyra. `check` only reports; an explicit install
    upgrades an older or unknown one (the command is the consent) but won't downgrade a newer one without --force."""
    home, dest = cli_home(), bindir / "hyra"
    inst = installed_cli(bindir)
    state = cli_state(inst)
    have = ".".join(map(str, inst["version"])) if inst["version"] else "an older version with no version number"
    msgs = {"missing": "missing: `hyra` is not on this machine yet",
            "ok": f"ok ({__version__} at {inst['path']})",
            "upgrade": f"upgrade-available: installed {have} at {inst['path']}; these skills carry {__version__}",
            "newer": f"newer: installed {have} at {inst['path']} is newer than these skills ({__version__}); update the skills instead"}
    # an install that is just a link into some repo's skill folder: right version today, breaks when that repo moves or is deleted
    repo_linked = state == "ok" and dest.is_symlink() and home.resolve() not in Path(os.path.realpath(dest)).parents
    if check:
        print(f"hyra-cli: {msgs[state]}" + (f"; but {dest} is a link into a repo, so it breaks if that repo moves: run `setup --cli` to make it independent" if repo_linked else ""))
        return
    out: list[str] = []
    if uninstall:
        if dest.is_symlink() and home.resolve() in Path(os.path.realpath(dest)).parents:
            dest.unlink()
            out.append(f"removed {dest}")
        for f in ("hyra", "VERSION"):
            if (home / f).exists():
                (home / f).unlink()
                out.append(f"removed {home / f}")
    elif only_if_missing and state != "missing":
        out.append(f"cli     {msgs[state]}" + ("; run `hyra setup --cli` to upgrade (ask the user first)" if state == "upgrade" else ""))
    elif state == "ok" and not repo_linked:
        out.append(f"cli     already up to date: {msgs['ok']}" + ("" if inst.get("on_path", True) else f"\nnote: {bindir} is not on your PATH yet"))
    elif state == "newer" and not force:
        out.append(f"cli     not downgrading: {msgs['newer']} (or pass --force)")
    else:
        home.mkdir(parents=True, exist_ok=True)
        tmp = home / "hyra.new"
        tmp.write_bytes(bundle_bytes())
        os.chmod(tmp, 0o755)
        os.replace(tmp, home / "hyra")
        (home / "VERSION").write_text(__version__ + "\n")
        bindir.mkdir(parents=True, exist_ok=True)
        # replace a link, or a regular file only when it IS the older hyra we just identified (or --force); never some other tool
        old_is_here = bool(inst["path"]) and Path(inst["path"]) == dest
        if dest.is_symlink() or (dest.exists() and (force or old_is_here)):
            dest.unlink()
        if dest.exists():
            out.append(f"skip    {dest} exists and is not a symlink (--force to replace)")
        else:
            dest.symlink_to(home / "hyra")
        out.append(f"installed hyra {__version__} at {home / 'hyra'} (linked from {dest})" + (f"; it replaced {have}" if state == "upgrade" else
                   "; it was a link into a repo, now independent of any repo" if repo_linked else ""))
        first = shutil.which("hyra")
        if str(bindir) not in os.environ.get("PATH", "").split(os.pathsep):
            out.append(f"note: add {bindir} to PATH")
        elif first and os.path.realpath(first) != os.path.realpath(home / "hyra"):
            out.append(f"WARNING: another hyra earlier on PATH ({first}) still shadows this one; remove it or put {bindir} first")
    print("\n".join(out) or "nothing to do")


def install_toolkit(scope: str, project: Optional[str], skills: Optional[list[str]], copy: Optional[bool],
                    all_harnesses: bool, uninstall: bool, agents_only: bool = False) -> None:
    """scope 'global': ~/.agents + each installed harness's dir (symlinks by default).
    scope 'local': inside one project, copied by default so the repo is self-contained/committable."""
    home = Path.home()
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
    harnesses = [h for h in ("claude", "opencode", "agy", "kimi", "omp") if all_harnesses or harness_detected(h)]
    root: Optional[Path] = None
    if scope == "local":
        root = Path(project or Path.cwd()).expanduser().resolve()
        if not root.is_dir():
            raise HyraError(f"no such project directory: {root}")
        targets = [root / ".agents" / "skills"]
        if "claude" in harnesses:
            targets.append(root / ".claude" / "skills")  # Claude Code doesn't read .agents/
        if all_harnesses or (home / ".qwen").exists():
            targets.append(root / ".qwen" / "skills")
        copy = True if copy is None else copy
    else:
        targets = skill_dirs()
        copy = False if copy is None else copy
    renderers: dict[Path, str] = {}  # agent dir -> "plain" | "agy". Antigravity and Kimi share .agents/agents locally;
    for h in harnesses:              # Kimi's plain files win there, and Antigravity falls back to prompt-embedded roles.
        d = agent_dirs(h, root)[-1]  # project dir for local, global dir for global
        if h == "agy" and d in renderers:
            continue
        renderers[d] = "agy" if h == "agy" else "plain"
    chosen = our_skills(skills)
    pairs: list[tuple[Path, Path]] = [] if agents_only else [(s, d / s.name) for s in chosen for d in targets]
    # Named agent types for every harness that reads markdown agent files (see agent_dirs). The role files work
    # as-is. Codex and Pi have none: there the roles run as headless sessions (`hyra run`) or are adopted in-session.
    plain_pairs = [(r, d / f"{name}.md") for d, kind in renderers.items() if kind == "plain" for r, name in role_agents(chosen)]
    agy_pairs = [(r, d / f"{name}.md") for d, kind in renderers.items() if kind == "agy" for r, name in role_agents(chosen)]
    pairs += plain_pairs
    out: list[str] = []
    for src, dest in agy_pairs:  # generated files (explicit tools list), never symlinks
        if uninstall:
            _remove(dest, out)
        else:
            if dest.is_symlink():
                dest.unlink()
            if dest.exists() and not _ours(dest):
                out.append(f"skip    {dest} exists and isn't ours")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(render_agent(src.read_text(), "agy"))
            out.append(f"wrote   {dest} (Antigravity: explicit tools list)")
    for src, dest in pairs:
        if uninstall:
            _remove(dest, out)
        elif copy:
            _copy(src, dest, out)
        else:
            _link(src, dest, out)
    print("\n".join(out) or "nothing to do")
    if not uninstall and scope == "global":
        (home / ".hyra" / "library" / "books").mkdir(parents=True, exist_ok=True)
        (home / ".hyra" / "library" / "skills").mkdir(parents=True, exist_ok=True)


def setup(args) -> None:
    home = Path.home()
    bindir = Path(args.bin_dir or os.environ.get("HYRA_BIN_DIR", home / ".local" / "bin")).expanduser()
    skills = [x for x in (args.skills or "").split(",") if x] or None
    if args.toolkit:
        install_toolkit(args.toolkit, args.project, skills, {"copy": True, "link": False}.get(args.mode),
                        args.all_harnesses, args.uninstall, getattr(args, "agents_only", False))
        if not args.uninstall:
            if not args.no_cli:  # skills call `hyra`, so make sure it exists (never replaces an existing one)
                install_cli(bindir, False, only_if_missing=True)
            print("done. Ask your agent to use the hyra skill (Claude Code: /hyra <goal>).")
    else:
        install_cli(bindir, args.uninstall, only_if_missing=False, check=getattr(args, "check", False), force=getattr(args, "force", False))
