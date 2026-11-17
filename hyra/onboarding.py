"""The small amount of setup around `hyra init` and `hyra server up`.

`hyra init` never asks anything and never deploys anything: it creates `.hyra/`, writes your machine-wide config the first time,
and connects the project to your ledger server if you already have one. `hyra server up` (optional, once per machine) ends by
asking for a private git remote to back the ledger up to.
"""
from __future__ import annotations

import json
import sys
import uuid
from typing import Optional

from . import doctor, ledger_deploy as ld
from .store import HyraError, Store

USER_DEFAULTS = {"ledger_hosts": [], "ledger_backup_remote": "", "ledger_backup_every_hours": 6}


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _save_user_cfg(key: str, value) -> None:
    path = doctor.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = doctor._read(path)
    cfg[key] = value
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def connect_project(st: Store, url: str, say=print) -> None:
    cfg_path = st.dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["server"] = {"url": url.rstrip("/"), "project": (cfg.get("server") or {}).get("project") or uuid.uuid4().hex}
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    st.config["server"] = cfg["server"]
    say(f"connected to your ledger server ({url})")


def init_run(st: Store, no_ledger: bool, say=print) -> None:
    ucfg = doctor.user_config_path()
    if not ucfg.exists():  # visible, editable machine-wide settings (secrets never go here: tokens live in ledger.json)
        ucfg.parent.mkdir(parents=True, exist_ok=True)
        ucfg.write_text(json.dumps(USER_DEFAULTS, indent=2) + "\n", encoding="utf-8")
    if no_ledger:
        return
    saved = ld.load_state()
    if saved.get("url") and not saved.get("stopped"):
        if ld.http_ok(saved["url"]):
            connect_project(st, saved["url"], say)
        else:
            say(f"note: your ledger ({saved['url']}) isn't reachable right now, so this project is local; `hyra server connect` when it is")
    else:
        say("optional: `hyra server up` sets up a hardened ledger server (once per machine); new projects then connect to it automatically")


def after_up(backup_remote: Optional[str], say=print) -> None:
    """The last step of every `hyra server up`: the git remote for the ledger's backups, then a backup that pushes to it.
    In a terminal it always asks, showing the saved remote (Enter keeps it), so re-running `up` is how you fix or change it.
    Not in a terminal (an agent): it uses the saved remote, or tells you what's missing."""
    ucfg = doctor.user_config_path()
    have = (doctor._read(ucfg).get("ledger_backup_remote") or "") if ucfg.exists() else ""
    if backup_remote is None:
        if _tty():
            shown = f"[{have}; Enter keeps it]" if have else "[blank to skip]"
            try:
                backup_remote = input(f"Git remote to back the ledger up to (a PRIVATE repo URL) {shown}: ").strip() or have
            except EOFError:
                backup_remote = have
        else:
            backup_remote = have
            if not have:
                say("backup: no remote set. Give a PRIVATE git repo URL: `hyra config set --user ledger_backup_remote <url>` "
                    "(until then the sealed claims exist only on the ledger machine)")
    if backup_remote and backup_remote != "none":
        if backup_remote != have:
            _save_user_cfg("ledger_backup_remote", backup_remote)
        try:
            ld.backup(None, str(ld.default_backup_repo()), backup_remote, True, say=say)
            say("backups run by themselves every 6 hours from now on")
        except HyraError as e:
            say(f"saved the backup remote, but the backup failed: {e}")
