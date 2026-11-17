"""`hyra doctor` and `hyra config`: see what's set up, what isn't, and what's misconfigured.

Config layers (later wins; dict-valued keys merge key by key):
  built-in defaults  <  ~/.hyra/config.json (your machine-wide choices)  <  <project>/.hyra/config.json
Unknown keys are silently ignored by the rest of hyra, so `hyra config validate` (and doctor) flag them, with a did-you-mean.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from . import install, remote
from .store import DEFAULT_CONFIG, HARNESS_PRESETS, HyraError, Store, find_root, skill_dir

KNOWN_KEYS = set(DEFAULT_CONFIG) | {"server"}


def user_config_path() -> Path:
    return Path.home() / ".hyra" / "config.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def problems(cfg: dict) -> list[str]:
    out = []
    for k, v in cfg.items():
        if k not in KNOWN_KEYS:
            near = difflib.get_close_matches(k, KNOWN_KEYS, n=1)
            out.append(f"unknown key {k!r}" + (f" (did you mean {near[0]!r}?)" if near else "") + ": it is ignored")
            continue
        d = DEFAULT_CONFIG.get(k)
        if d is None:
            continue
        ok = isinstance(v, dict) if isinstance(d, dict) else isinstance(v, list) if isinstance(d, list) else \
            isinstance(v, bool) if isinstance(d, bool) else isinstance(v, (int, float)) and not isinstance(v, bool) if isinstance(d, (int, float)) \
            else isinstance(v, str) if isinstance(d, str) else True
        if not ok:
            out.append(f"{k!r} should be a {type(d).__name__}, got {type(v).__name__}")
    for h, cmd in (cfg.get("harness_cmds") or {}).items():
        if not isinstance(cmd, list) or "{prompt}" not in cmd:
            out.append(f"harness_cmds[{h!r}] must be a list containing \"{{prompt}}\"")
    return out


# ------------------------------------------------------------------------------------------------ hyra config
def config_show(root: Optional[Path]) -> None:
    user = _read(user_config_path())
    proj = _read(root / ".hyra" / "config.json") if root else {}
    print(f"# defaults < {user_config_path()}{'' if user_config_path().exists() else ' (absent)'}"
          + (f" < {root / '.hyra' / 'config.json'}" if root else "  (not in a project)"))
    keys = list(DEFAULT_CONFIG) + [k for k in {**user, **proj} if k not in DEFAULT_CONFIG]
    for k in keys:
        src = "project" if k in proj else "user" if k in user else "default"
        val = proj[k] if k in proj else user[k] if k in user else DEFAULT_CONFIG.get(k)
        if isinstance(val, dict) and src != "default" and DEFAULT_CONFIG.get(k):
            val = {**DEFAULT_CONFIG[k], **user.get(k, {}), **proj.get(k, {})}
        print(f"{k:28} [{src:7}] {json.dumps(val)[:110]}")


def _target(root: Optional[Path], user: bool) -> Path:
    if user:
        user_config_path().parent.mkdir(parents=True, exist_ok=True)
        return user_config_path()
    if not root:
        raise HyraError("not inside a Hyra project: run `hyra init`, or use --user for machine-wide config")
    return root / ".hyra" / "config.json"


def config_set(root: Optional[Path], key: str, value: str, user: bool, force: bool) -> None:
    top = key.split(".")[0]
    if top not in KNOWN_KEYS and not force:
        near = difflib.get_close_matches(top, KNOWN_KEYS, n=1)
        raise HyraError(f"unknown key {top!r}" + (f" (did you mean {near[0]!r}?)" if near else "") + "; --force to set it anyway")
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    path = _target(root, user)
    cfg = _read(path)
    node = cfg
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = parsed
    bad = problems({top: cfg[top]})
    if bad and not force:
        raise HyraError("; ".join(bad))
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"{key} = {json.dumps(parsed)}   ({path})")


def config_unset(root: Optional[Path], key: str, user: bool) -> None:
    path = _target(root, user)
    cfg = _read(path)
    node, parts = cfg, key.split(".")
    for p in parts[:-1]:
        node = node.get(p, {})
    if parts[-1] not in node:
        raise HyraError(f"{key} is not set in {path}")
    del node[parts[-1]]
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"unset {key} ({path})")


def config_validate(root: Optional[Path]) -> None:
    bad = 0
    for label, p in (("user", user_config_path()), ("project", root / ".hyra" / "config.json" if root else None)):
        if p and p.exists():
            for msg in problems(_read(p)):
                print(f"{label}: {msg}")
                bad += 1
    print("config is valid" if not bad else f"{bad} problem(s)")


# ------------------------------------------------------------------------------------------------ hyra doctor
def doctor() -> None:
    notes: list[str] = []

    def ok(m, fix=""): print(f"  ok    {m}")
    def warn(m, fix=""):
        print(f"  WARN  {m}")
        if fix:
            print(f"        -> {fix}")
        notes.append(m)
    def info(m, fix=""): print(f"  info  {m}")

    print("Environment")
    (ok if sys.version_info >= (3, 9) else warn)(f"python {sys.version.split()[0]} (needs 3.9+)")
    cli = shutil.which("hyra")
    try:
        launcher = skill_dir() / "scripts" / "hyra"
    except HyraError:
        launcher = None
    if cli:
        ok(f"`hyra` is on PATH: {cli}")
    else:
        warn("`hyra` is not on PATH (skills call it by that name)",
             f"python3 {launcher} setup --cli" if launcher else "run `setup --cli` from the hyra skill's scripts/hyra")
    try:
        mine = install.our_skills()
        names = [p.name for p in mine]
        ok(f"skills found next to the CLI: {', '.join(names)}")
    except HyraError as e:
        mine, names = [], []
        warn(f"can't find the hyra skill folder: {e}")

    print("\nAgent harnesses (what `hyra run` can launch)")
    from concurrent.futures import ThreadPoolExecutor
    import subprocess

    def probe(item):
        h, cmd = item
        path = shutil.which(cmd[0])
        if not path:
            return h, None, "not installed"
        try:  # on PATH is not enough: wrappers/shims exist for tools that aren't actually installed
            r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL)
            text = (r.stdout + r.stderr).strip()
            if r.returncode != 0 or "unavailable" in text.lower() or "unable to start" in text.lower():
                return h, None, f"found at {path} but doesn't run ({text.splitlines()[0][:60] if text else 'no output'})"
            return h, path, path
        except (subprocess.TimeoutExpired, OSError) as e:
            return h, None, f"found at {path} but doesn't run ({type(e).__name__})"
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(probe, HARNESS_PRESETS.items()))
    found = {h: path for h, path, _ in results}
    for h, path, msg in results:
        (ok if path else info)(f"{h:9} {msg}")
    root: Optional[Path]
    try:
        root = find_root()
    except HyraError:
        root = None
    st = Store(root) if root else None
    default_h = (st.config.get("harness") if st else None) or "claude"
    if not found.get(default_h) and default_h in HARNESS_PRESETS and (st is None or not st.config.get("harness_cmds", {}).get(default_h)):
        warn(f"the default harness {default_h!r} isn't installed", "`hyra config set harness <name>` to one that is listed above")

    print("\nSkills and agent files")
    for d in install.skill_dirs():
        have = [n for n in names if (d / n / "SKILL.md").exists()]
        (ok if len(have) == len(names) and names else info)(f"{str(d).replace(str(Path.home()), '~'):40} {len(have)}/{len(names)} skills")
    for h in ("claude", "opencode", "agy", "kimi", "omp"):
        if install.harness_detected(h):
            have = install.native_agent_available(h, "hyra-worker", root or Path.cwd())
            (ok if have else info)(f"{h:9} named agents: {'installed' if have else 'not installed (optional; roles are embedded in the prompt instead)'}")
    if not any(install.native_agent_available(h, "hyra-worker", root or Path.cwd()) for h in ("claude", "opencode", "agy", "kimi", "omp")):
        info("to register the optional named agents: `hyra setup --toolkit global --agents-only`")

    print("\nScoring sandbox (hyra eb)")
    if st:
        from .eb import sandbox_command
        mode, _ = sandbox_command(st, Path.cwd())
        (ok if mode != "none" else warn)(f"evaluations run under: {mode}" if mode != "none" else
                                         "no OS sandbox available: evaluations run in a scratch copy only",
                                         "" if mode != "none" else "install bubblewrap/podman, or set eb_sandbox")

    print("\nProject")
    if not root:
        info("not inside a Hyra project (run `hyra init` in your research repo)")
    else:
        ok(f"project: {root}")
        for label, p in (("user", user_config_path()), ("project", root / ".hyra" / "config.json")):
            for msg in problems(_read(p)):
                warn(f"{label} config: {msg}", "`hyra config validate`")
        cfg = st.config
        info(f"harness {cfg.get('harness')}; unattended Overall {'on' if cfg.get('overall_agent') else 'off'}; auto retro "
             f"{'on' if cfg.get('auto_retro') else 'off'}; approvals_require_tty {cfg.get('approvals_require_tty')}")
        if cfg.get("notify_cmd"):
            ok("notify_cmd is set")
        else:
            info("notify_cmd is not set (no phone pings; see the README's recipes)")
        wv = (cfg.get("wiki") or {}).get("vault") or os.environ.get("HYRA_WIKI_VAULT")
        if wv:
            (ok if (Path(wv) / "wiki").is_dir() else warn)(f"wiki vault {wv}")
        if remote.enabled(st):
            try:
                remote.call(st, "GET", "/v1/health")
                ok(f"ledger server reachable: {cfg['server']['url']}")
            except HyraError as e:
                warn(f"ledger server: {e}")
            have = [n for n in ("HYRA_TOKEN", "HYRA_RUNNER_TOKEN", "HYRA_HUMAN_TOKEN") if os.environ.get(n)]
            info(f"server tokens in this shell: {', '.join(have) or 'none'} (the scheduler needs HYRA_RUNNER_TOKEN; "
                 "approvals need the human token, typed at the prompt)")
            from . import ledger_deploy as ld
            ls = ld.load_state()
            hours = cfg.get("ledger_backup_every_hours")
            if ls.get("backup_repo") or cfg.get("ledger_backup_remote"):
                last = ls.get("last_backup") or "never"
                (ok if ls.get("last_backup") else warn)(f"ledger backup every {6 if hours is None else hours}h to {cfg.get('ledger_backup_remote') or ls.get('backup_repo')}; last: {last}")
            else:
                warn("the ledger has no backup remote: sealed claims exist only on the server",
                     "`hyra config set --user ledger_backup_remote <private git URL>`")
        else:
            info("ledger server: not configured (local mode; see `hyra server`)")

    print()
    print("Everything required is in place." if not notes else f"{len(notes)} thing(s) to look at (marked WARN).")
