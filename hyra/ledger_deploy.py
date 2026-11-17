"""`hyra server up|down|where`: put the ledger server on any tailnet device you can ssh into, without thinking about it.

The ledger is one SQLite file (`~/.hyra-server/ledger.db`) behind a small stdlib HTTP process, so "deploying" it is: copy one
self-contained file (`hyra.pyz`), run it as a service, and make it reachable on the tailnet. This module does that over ssh:

  hyra server up                      # picks a Linux/macOS device on your tailnet that you can ssh into (never this one)
  hyra server up nitez@APOTHEOSIS     # or name it
  hyra server down [--purge --yes]    # stop it over ssh (keeps the data unless --purge)
  hyra server where                   # what's deployed

Service manager on the target: systemd --user (Linux), launchd (macOS), else a plain background process. Exposure: `tailscale
serve` (HTTPS with a real certificate, server bound to 127.0.0.1) when allowed, otherwise bound to the tailnet address (plain
HTTP inside the WireGuard tunnel). ssh uses BatchMode and strict host-key checking: an unseen host fails with a message telling
you to `ssh` there once, rather than being trusted silently.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipapp
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .store import HyraError, skill_dir

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=yes"]
DEFAULT_PORT = 7421
MAC_TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def state_path() -> Path:
    return Path.home() / ".hyra" / "ledger.json"


def load_state() -> dict:
    p = state_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_state(s: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    os.chmod(p, 0o600)


def unit_name() -> str:
    return os.environ.get("HYRA_LEDGER_UNIT", "hyra-ledger")


def launchd_label() -> str:
    return os.environ.get("HYRA_LEDGER_LABEL", "com.hyra.ledger")


# ------------------------------------------------------------------------------------------------ transport
class Host:
    """Where commands run: `user@host` over ssh, or this machine (target None; used for tests and single-machine setups)."""

    def __init__(self, target: Optional[str]):
        self.target = target

    @property
    def label(self) -> str:
        return self.target or "this machine"

    def run(self, script: str, timeout: int = 90) -> tuple[int, str, str]:
        cmd = ["ssh", *SSH_OPTS, self.target, "bash", "-s"] if self.target else ["bash", "-s"]
        try:
            r = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", "timed out"
        return r.returncode, r.stdout, r.stderr.strip()

    def put(self, data: bytes, dest_in_home: str) -> None:
        """Write bytes to $HOME/<dest_in_home> atomically (ssh stdin, so no scp needed)."""
        q = shlex.quote(dest_in_home)
        script = f'mkdir -p "$(dirname "$HOME"/{q})" && cat > "$HOME"/{q}.new && mv "$HOME"/{q}.new "$HOME"/{q}'
        cmd = ["ssh", *SSH_OPTS, self.target, script] if self.target else ["bash", "-c", script]
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=120)
        if r.returncode != 0:
            raise HyraError(f"could not copy to {self.label}: {r.stderr.decode(errors='replace').strip()[:200]}")


def ssh_failure_hint(err: str, target: str) -> str:
    if "Host key verification failed" in err or "REMOTE HOST IDENTIFICATION" in err:
        return f"unknown or changed host key: run `ssh {target}` once yourself to trust it, then retry"
    if "Permission denied" in err:
        return f"ssh refused the login for {target} (wrong user? try `hyra config set --user ledger_hosts '[\"user@{target.split('@')[-1]}\"]'`)"
    if "timed out" in err.lower() or "Connection refused" in err:
        return f"{target} didn't answer on ssh (offline, or ssh not enabled)"
    return err.splitlines()[-1][:160] if err else "unknown ssh error"


# ------------------------------------------------------------------------------------------------ discovery
def tailscale_bin() -> Optional[str]:
    override = os.environ.get("HYRA_TAILSCALE_BIN")  # tests and unusual installs
    if override is not None:
        return override if override and Path(override).exists() else None
    return shutil.which("tailscale") or (MAC_TAILSCALE if Path(MAC_TAILSCALE).exists() else None)


def tailnet_status() -> dict:
    ts = tailscale_bin()
    if not ts:
        raise HyraError("tailscale CLI not found on this machine (needed to discover tailnet devices; or pass user@host)")
    r = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise HyraError(f"`tailscale status` failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def candidates(status: dict, preferred: list[str], any_owner: bool = False) -> list[dict]:
    """Tailnet devices that could run the ledger: online, Linux or macOS, not this device, owned by you. Your preferred
    `ledger_hosts` (user@host) first, then Linux (servers) before macOS (laptops), alphabetically."""
    me = status.get("Self", {})
    pref = {p.split("@")[-1].lower(): p for p in preferred}
    out = []
    for p in status.get("Peer", {}).values():
        if p.get("ID") == me.get("ID") or not p.get("Online") or p.get("OS") not in ("linux", "macOS"):
            continue
        if not any_owner and p.get("UserID") != me.get("UserID"):
            continue
        host = p.get("HostName", "")
        out.append({"host": host, "dns": (p.get("DNSName") or "").rstrip("."), "os": p["OS"], "ip": (p.get("TailscaleIPs") or [""])[0],
                    "target": pref.get(host.lower())})
    out.sort(key=lambda c: (c["target"] is None, c["os"] != "linux", c["host"].lower()))
    return out


def ssh_user_for(host: str) -> str:
    """The user ssh would use for this host (honours ~/.ssh/config), else your local user name."""
    r = subprocess.run(["ssh", "-G", host], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("user "):
            return line.split(None, 1)[1]
    return os.environ.get("USER", "root")


def probe_ssh(target: str) -> tuple[bool, str]:
    rc, out, err = Host(target).run('uname -s; command -v python3 >/dev/null && python3 -c "import sys; assert sys.version_info >= (3, 9)" && echo py-ok', timeout=15)
    if rc != 0:
        return False, ssh_failure_hint(err, target)
    return ("py-ok" in out), ("" if "py-ok" in out else f"{target} has no python3 >= 3.9")


def auto_select(preferred: list[str], any_owner: bool = False, say=print) -> tuple[str, dict]:
    cands = candidates(tailnet_status(), preferred, any_owner)
    if not cands:
        raise HyraError("no other online Linux/macOS device owned by you on the tailnet; pass user@host explicitly")
    targets = [(c, c["target"] or f"{ssh_user_for(c['dns'] or c['host'])}@{c['dns'] or c['host']}") for c in cands]
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda ct: probe_ssh(ct[1]), targets))
    for (c, t), (ok, why) in zip(targets, results):
        say(f"  {'ok  ' if ok else 'skip'} {t:34} {c['os']:6} {'' if ok else why}")
    for (c, t), (ok, _) in zip(targets, results):
        if ok:
            return t, c
    raise HyraError("none of your tailnet devices accepted an ssh login. Pass user@host, or list yours once: "
                    "`hyra config set --user ledger_hosts '[\"nitez@APOTHEOSIS\", \"isagi@fedora\"]'`")


# ------------------------------------------------------------------------------------------------ the bundle
def bundle_bytes() -> bytes:
    from .install import bundle_bytes as _bundle  # the running version of hyra, as one file
    return _bundle()


# ------------------------------------------------------------------------------------------------ remote facts and services
PROBE = r'''
echo "os=$(uname -s)"; echo "home=$HOME"; echo "user=$(id -un)"
echo "python=$(command -v python3 || true)"
echo "pyok=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 9)))' 2>/dev/null || echo 0)"
echo "systemd=$(systemctl --user show-environment >/dev/null 2>&1 && echo 1 || echo 0)"
echo "linger=$(loginctl show-user "$(id -un)" -p Linger 2>/dev/null | cut -d= -f2)"
TS=$(command -v tailscale || ls /Applications/Tailscale.app/Contents/MacOS/Tailscale 2>/dev/null | head -1 || true)
echo "tailscale=$TS"
[ -n "$TS" ] && echo "tsself=$("$TS" status --json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin)["Self"]; print(d["DNSName"].rstrip("."), d["TailscaleIPs"][0])' 2>/dev/null)"
'''


def facts(host: Host) -> dict:
    rc, out, err = host.run(PROBE, timeout=30)
    if rc != 0:
        raise HyraError(f"can't reach {host.label}: {ssh_failure_hint(err, host.label)}")
    f = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    if f.get("pyok") != "1":
        raise HyraError(f"{host.label} needs python3 >= 3.9")
    return f


def health_script(port: int, tries: int = 40, bind: str = "127.0.0.1") -> str:
    """Poll the ledger where it actually listens (a ledger bound to the tailnet address does not answer on 127.0.0.1)."""
    return (f'for i in $(seq 1 {tries}); do python3 -c "import urllib.request;urllib.request.urlopen(\'http://{bind}:{port}/v1/health\',timeout=1)" '
            f'>/dev/null 2>&1 && exit 0; sleep 0.25; done; exit 1')


def serve_args(f: dict, bind: str, port: int, rotate: bool) -> list[str]:
    a = [f"{f['home']}/.hyra-server/hyra.pyz", "server", "serve", "--data", f"{f['home']}/.hyra-server", "--host", bind, "--port", str(port),
         "--no-print-tokens"]
    return a + (["--rotate-tokens"] if rotate else [])


def systemd_unit(exec_argv: list[str]) -> str:
    return ("[Unit]\nDescription=Hyra ledger server\nAfter=network-online.target\n\n[Service]\n"
            f"ExecStart={' '.join(shlex.quote(x) for x in exec_argv)}\nRestart=on-failure\nUMask=0077\nNoNewPrivileges=yes\n\n"
            "[Install]\nWantedBy=default.target\n")


def launchd_plist(label: str, exec_argv: list[str], data: str) -> str:
    argv = "".join(f"    <string>{x}</string>\n" for x in exec_argv)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>\n'
            f'  <key>Label</key><string>{label}</string>\n  <key>ProgramArguments</key><array>\n{argv}  </array>\n'
            '  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n'
            f'  <key>StandardOutPath</key><string>{data}/ledger.log</string>\n  <key>StandardErrorPath</key><string>{data}/ledger.log</string>\n'
            '  <key>Umask</key><integer>63</integer>\n</dict></plist>\n')


def install_service(host: Host, f: dict, bind: str, port: int, rotate: bool, use_service: bool) -> str:
    args = serve_args(f, bind, port, rotate)
    py = f["python"]
    data = f"{f['home']}/.hyra-server"
    if use_service and f.get("systemd") == "1":
        name = unit_name()
        unit = systemd_unit([py, *args])
        script = (f'set -e\nmkdir -p "$HOME/.config/systemd/user" {shlex.quote(data)}; chmod 700 {shlex.quote(data)}\n'
                  f'cat > "$HOME/.config/systemd/user/{name}.service" <<\'UNIT\'\n{unit}UNIT\n'
                  f'systemctl --user daemon-reload\nsystemctl --user enable {name} >/dev/null 2>&1\nsystemctl --user restart {name}\n'
                  + (f'[ "$(loginctl show-user "$(id -un)" -p Linger 2>/dev/null | cut -d= -f2)" = yes ] || loginctl enable-linger "$(id -un)" 2>/dev/null || true\n'))
        mode = "systemd"
    elif use_service and f.get("os") == "Darwin":
        label = launchd_label()
        plist = launchd_plist(label, [py, *args], data)
        script = (f'set -e\nmkdir -p "$HOME/Library/LaunchAgents" {shlex.quote(data)}; chmod 700 {shlex.quote(data)}\n'
                  f'cat > "$HOME/Library/LaunchAgents/{label}.plist" <<\'PLIST\'\n{plist}PLIST\n'
                  f'launchctl bootout "gui/$(id -u)/{label}" 2>/dev/null || true\n'
                  f'launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/{label}.plist"\n')
        mode = "launchd"
    else:  # no service manager (or --no-service): a plain background process; it won't survive a reboot
        cmd = " ".join(shlex.quote(x) for x in [py, *args])
        script = (f'set -e\numask 077; mkdir -p {shlex.quote(data)}; chmod 700 {shlex.quote(data)}\n'
                  f'[ -f {shlex.quote(data)}/ledger.pid ] && kill "$(cat {shlex.quote(data)}/ledger.pid)" 2>/dev/null || true\nsleep 0.3\n'
                  f'nohup {cmd} >> {shlex.quote(data)}/ledger.log 2>&1 < /dev/null &\necho $! > {shlex.quote(data)}/ledger.pid\n')
        mode = "process"
    rc, out, err = host.run(script, timeout=60)
    if rc != 0:
        raise HyraError(f"couldn't start the ledger service on {host.label} ({mode}): {err[:240]}")
    return mode


def stop_service(host: Host, mode: str, f: dict) -> None:
    data = f"{f['home']}/.hyra-server"
    if mode == "systemd":
        name = unit_name()
        host.run(f'systemctl --user disable --now {name} 2>/dev/null; rm -f "$HOME/.config/systemd/user/{name}.service"; systemctl --user daemon-reload')
    elif mode == "launchd":
        label = launchd_label()
        host.run(f'launchctl bootout "gui/$(id -u)/{label}" 2>/dev/null; rm -f "$HOME/Library/LaunchAgents/{label}.plist"')
    else:
        host.run(f'[ -f {shlex.quote(data)}/ledger.pid ] && kill "$(cat {shlex.quote(data)}/ledger.pid)" 2>/dev/null; rm -f {shlex.quote(data)}/ledger.pid; true')


def http_ok(url: str) -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(url.rstrip("/") + "/v1/health", timeout=6)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------------------------------------ up / down / where
def up(target: Optional[str], *, auto: bool, port: int, use_service: bool, rotate: bool, expose: str, dry_run: bool,
       preferred: list[str], any_owner: bool, say=print) -> dict:
    state = load_state()
    local = target == "local"
    if local:
        target_s: Optional[str] = None
    elif target:
        target_s = target
    elif state.get("target") and not auto:
        target_s = state["target"] if state["target"] != "local" else None
        local = state["target"] == "local"
        say(f"using the ledger host you set up before: {state['target']}")
    else:
        say("looking for a device on your tailnet that accepts ssh (never this one):")
        target_s, cand = auto_select(preferred, any_owner, say)
        say(f"chosen: {target_s}")
    host = Host(target_s)
    if dry_run:
        say(f"[dry-run] would deploy the ledger to {host.label} on port {port}; nothing changed")
        return {}
    f = facts(host)
    say(f"{host.label}: {f['os']}, python {f['python']}, {'systemd --user' if f.get('systemd') == '1' else 'launchd' if f['os'] == 'Darwin' else 'no service manager'}"
        f", tailscale {'found' if f.get('tailscale') else 'not found'}")
    # a free port (or the ledger's own)
    rc, out, _ = host.run('for p in $(seq {0} {1}); do python3 - "$p" <<\'PY\'\nimport socket, sys, urllib.request\np = int(sys.argv[1])\n'
                          's = socket.socket(); s.settimeout(0.5)\nif s.connect_ex(("127.0.0.1", p)) != 0:\n    print(p); sys.exit(0)\ntry:\n'
                          '    urllib.request.urlopen(f"http://127.0.0.1:{{p}}/v1/health", timeout=1); print(p); sys.exit(0)\nexcept Exception:\n    sys.exit(1)\n'
                          'PY\n[ $? -eq 0 ] && break; done'.format(port, port + 5))
    ports = [l for l in out.split() if l.isdigit()]
    if not ports:
        raise HyraError(f"ports {port}-{port + 5} are all busy on {host.label}")
    port = int(ports[-1])
    host.put(bundle_bytes(), ".hyra-server/hyra.pyz")
    ts, dns_ip = f.get("tailscale", ""), (f.get("tsself") or "").split()
    dns, ip = (dns_ip + ["", ""])[:2]
    if expose == "auto":
        expose = "serve" if (ts and dns and not local) else "none" if local else "bind" if ip else "none"
    # 1) bind to loopback and publish with `tailscale serve` (HTTPS, real certificate); 2) else bind to the tailnet address
    plan = [("serve", "127.0.0.1")] if expose in ("serve",) else []
    plan += [("bind", ip)] if expose in ("serve", "bind") and ip else []
    plan = plan or [("none", "127.0.0.1")]
    mode, url, used = "", "", ""
    for how, bind in plan:
        mode = install_service(host, f, bind, port, rotate and how == plan[0][0], use_service)
        rc, _, err = host.run(health_script(port, bind=bind))
        if rc != 0:
            raise HyraError(f"the ledger didn't start on {host.label} (look at ~/.hyra-server/ledger.log or `journalctl --user -u {unit_name()}`)")
        if how == "serve":
            rc, _, err = host.run(f'"{ts}" serve --bg --https={port} http://127.0.0.1:{port}', timeout=40)
            cand_url = f"https://{dns}:{port}"
            if rc == 0 and http_ok(cand_url):
                url, used = cand_url, "tailscale serve (HTTPS)"
                break
            why = " ".join((err or "not reachable from here").split())
            hint = (" For HTTPS later, run once on that machine: `sudo tailscale set --operator=$USER`, then `hyra server up` again."
                    if "denied" in why.lower() or "operator" in why.lower() else "")
            say(f"tailscale serve isn't available there ({why[:70]}); using the tailnet address instead (plain HTTP inside the encrypted tunnel, fine).{hint}")
            host.run(f'"{ts}" serve --https={port} off 2>/dev/null; true')
        elif how == "bind":
            cand_url = f"http://{ip}:{port}"
            if http_ok(cand_url):
                url, used = cand_url, "bound to the tailnet address (plain HTTP inside the WireGuard tunnel)"
                break
        else:
            url, used = (f"http://127.0.0.1:{port}" if local else f"http://{host.target.split('@')[-1]}:{port}"), "loopback only"
            break
    if not url:
        raise HyraError(f"started, but {host.label} isn't reachable from this machine on the tailnet; see {state_path()}")
    # first-run tokens: read once over ssh, then delete them from the server
    rc, out, _ = host.run(f'F="$HOME/.hyra-server/initial-tokens.txt"; [ -f "$F" ] && cat "$F" && rm -f "$F" || true')
    toks = dict(l.split("=", 1) for l in out.split() if "=" in l)
    where_to = "local" if local else target_s
    # re-running `up` against the same machine keeps everything else we know (backup repo, last backup, cached tokens...)
    new = dict(state) if state.get("target") == where_to else {}
    new.pop("stopped", None)
    new.update({"target": where_to, "url": url, "port": port, "mode": mode, "expose": used, "home": f["home"],
                "tailscale": ts, "python": f["python"], "os": f["os"]})
    if toks.get("HYRA_HUMAN_TOKEN"):  # first run, or --rotate-tokens: new tokens; the cached short-lived one is now invalid
        new.update(human_token=toks["HYRA_HUMAN_TOKEN"], runner_token=toks["HYRA_RUNNER_TOKEN"])
        new.pop("overall_token", None); new.pop("overall_expires", None)
    if "human_token" not in new:
        raise HyraError(f"the ledger on {host.label} already has tokens that aren't saved on this machine. Run `hyra server up --rotate-tokens` to mint new ones")
    save_state(new)
    say(f"ledger is up: {url}  ({used}; {mode} on {host.label})")
    say(f"saved connection and tokens to {state_path()} (mode 600). Stop it with `hyra server down`.")
    return new


def down(purge: bool, yes: bool, say=print) -> None:
    s = load_state()
    if not s:
        raise HyraError("no ledger deployed from this machine (nothing in ~/.hyra/ledger.json)")
    host = Host(None if s["target"] == "local" else s["target"])
    f = {"home": s["home"]}
    if s.get("expose", "").startswith("tailscale") and s.get("tailscale"):
        host.run(f'"{s["tailscale"]}" serve --https={s["port"]} off 2>/dev/null; true')
    stop_service(host, s["mode"], f)
    say(f"stopped the ledger on {host.label}")
    if purge:
        if not yes:
            raise HyraError("--purge deletes the ledger database (all sealed claims, approvals, audit trail) on the server: add --yes to confirm")
        host.run(f'rm -rf {shlex.quote(s["home"] + "/.hyra-server")}')
        state_path().unlink(missing_ok=True)
        say("deleted the ledger data and the saved connection")
    else:
        s["stopped"] = True
        save_state(s)
        say("data kept on the server; `hyra server up` starts it again with the same tokens")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise HyraError(f"git {' '.join(args)} failed in {repo}: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def user_config() -> dict:
    p = Path.home() / ".hyra" / "config.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def default_backup_repo() -> Path:
    return Path.home() / ".hyra" / "ledger-backup"


def _age_seconds(iso: Optional[str]) -> float:
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() if iso else float("inf")


def maybe_backup(cfg: dict) -> None:
    """Start a background ledger backup if one is due. Cheap and safe to call from commands you run all the time: it returns at
    once, backs off for 10 minutes after any attempt (so a failing remote isn't hammered), and never raises. Needs a deployed
    ledger and a backup remote/repo; every `ledger_backup_every_hours` (default 6; 0 = off)."""
    try:
        s = load_state()
        hours = cfg.get("ledger_backup_every_hours")
        hours = 6.0 if hours is None else float(hours)
        if not s.get("url") or s.get("stopped") or hours <= 0:
            return
        if not (s.get("backup_repo") or cfg.get("ledger_backup_remote") or user_config().get("ledger_backup_remote")):
            return
        if _age_seconds(s.get("last_backup")) < hours * 3600 or _age_seconds(s.get("last_backup_attempt")) < 600:
            return
        s["last_backup_attempt"] = datetime_now_iso()
        save_state(s)
        log = open(Path.home() / ".hyra" / "ledger-backup.log", "a")
        subprocess.Popen([sys.executable, str(skill_dir() / "scripts" / "hyra"), "server", "backup", "--quiet"],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except Exception:
        pass


def backup(repo: Optional[str], init: Optional[str], remote: Optional[str], push: bool, say=print, quiet: bool = False) -> None:
    """Snapshot the ledger (claims, reviews, approvals, audit; NOT tokens) into a git repo and push it.
    The snapshot is taken on the server with SQLite's online backup (consistent while it runs), as plain SQL text so git diffs and
    compresses it. The repo holds your research conclusions: keep it PRIVATE."""
    s = load_state()
    if not s:
        raise HyraError("no ledger deployed from this machine (`hyra server up`)")
    import fcntl
    lock = open(Path.home() / ".hyra" / "ledger-backup.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # another backup is running right now
    cfg_remote = user_config().get("ledger_backup_remote")
    if not (init or repo or s.get("backup_repo")) and cfg_remote:  # the remote is configured but the local repo isn't set up yet
        init, remote = str(default_backup_repo()), cfg_remote
    if init:
        path = Path(init).expanduser()
        if not path.exists():
            if remote and subprocess.run(["git", "ls-remote", remote], capture_output=True).returncode == 0 and \
                    subprocess.run(["git", "ls-remote", remote, "HEAD"], capture_output=True, text=True).stdout.strip():
                subprocess.run(["git", "clone", "-q", remote, str(path)], check=True)
            else:
                path.mkdir(parents=True)
                subprocess.run(["git", "init", "-q", str(path)], check=True)
        if remote:
            if "origin" not in _git(path, "remote", check=False).stdout.split():
                _git(path, "remote", "add", "origin", remote)
            elif _git(path, "remote", "get-url", "origin", check=False).stdout.strip() != remote:  # you changed the remote
                _git(path, "remote", "set-url", "origin", remote)
        (path / "README.md").write_text("# hyra ledger backup\n\nSnapshots of a hyra ledger server (`hyra server backup`): sealed claims, reviews, approvals, "
                                        "audit trail, as SQL. **Keep this repository private.** Restore with `hyra server restore --from <this dir>`.\n", encoding="utf-8")
        s["backup_repo"] = str(path.resolve())
        save_state(s)
    path = Path(repo or s.get("backup_repo") or "").expanduser()
    if not str(path) or str(path) == "." or not (path / ".git").exists():
        raise HyraError("no backup repo yet: `hyra server backup --init ~/hyra-ledger-backup --remote git@github.com:you/hyra-ledger-backup.git` (a PRIVATE repo)")
    host = Host(None if s["target"] == "local" else s["target"])
    data = f"{s['home']}/.hyra-server"
    rc, out, err = host.run(f'{shlex.quote(s["python"])} {shlex.quote(data)}/hyra.pyz server dump --data {shlex.quote(data)}', timeout=180)
    if rc != 0 or "CREATE TABLE" not in out:
        raise HyraError(f"couldn't snapshot the ledger on {host.label}: {err[:200] or 'empty output'}")
    (path / "ledger.sql").write_text(out, encoding="utf-8")
    _git(path, "add", "ledger.sql", "README.md")
    if _git(path, "diff", "--cached", "--quiet", check=False).returncode == 0:
        if not quiet:
            say("ledger backup: no changes since the last snapshot")
    else:
        from datetime import datetime, timezone
        n = out.count("INSERT INTO \"claims\"")
        _git(path, "-c", "user.name=hyra", "-c", "user.email=hyra@localhost", "commit", "-q", "-m",
             f"ledger backup {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}Z ({n} sealed claims)")
        say(f"ledger backup committed in {path}")
    s["backup_repo"] = str(path.resolve())
    pushed_ok = True
    if push and "origin" in _git(path, "remote", check=False).stdout.split():
        r = _git(path, "push", "-q", "-u", "origin", "HEAD", check=False)
        pushed_ok = r.returncode == 0
        say("pushed to the remote" if pushed_ok else f"WARNING: committed locally but the push failed: {(r.stderr or r.stdout).strip()[:200]} "
                                                      "(it will be retried; fix the remote or your git credentials, then `hyra server backup`)")
    elif push:
        say("note: this backup repo has no remote, so it is only on this machine (`git -C <repo> remote add origin <private repo>`)")
    if pushed_ok:  # a snapshot that never left this machine doesn't count as a backup: keep retrying
        s["last_backup"] = datetime_now_iso()
    save_state(s)


def datetime_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def restore(src: str, yes: bool, say=print) -> None:
    """Replace the ledger's data on its server with a backup. New human/runner tokens are minted and saved (the old ones don't
    survive: tokens are deliberately not part of a backup). Projects keep their ids, so they reconnect to their sealed claims."""
    s = load_state()
    if not s:
        raise HyraError("no ledger deployed from this machine: `hyra server up` first (a fresh one is fine), then restore into it")
    p = Path(src).expanduser()
    sql = p / "ledger.sql" if p.is_dir() else p
    if not sql.exists():
        raise HyraError(f"no ledger.sql at {sql}")
    if not yes:
        raise HyraError(f"this REPLACES the ledger data on {s['target']} with {sql}; the old database is kept aside on the server. Add --yes")
    host = Host(None if s["target"] == "local" else s["target"])
    data = f"{s['home']}/.hyra-server"
    stop_service(host, s["mode"], {"home": s["home"]})
    host.put(sql.read_bytes(), ".hyra-server/restore.sql")
    rc, out, err = host.run(f'{shlex.quote(s["python"])} {shlex.quote(data)}/hyra.pyz server load --data {shlex.quote(data)} --file {shlex.quote(data)}/restore.sql --force; '
                            f'rm -f {shlex.quote(data)}/restore.sql', timeout=180)
    if rc != 0:
        raise HyraError(f"restore failed on {host.label}: {(err or out)[:240]}")
    say(out.strip().splitlines()[-1] if out.strip() else "restored")
    for k in ("human_token", "runner_token", "stopped"):
        s.pop(k, None)
    save_state(s)
    up(s["target"], auto=False, port=s["port"], use_service=s["mode"] != "process", rotate=False, expose="auto" if s["target"] != "local" else "none",
       dry_run=False, preferred=[], any_owner=False, say=say)


def where() -> None:
    s = load_state()
    if not s:
        print("no ledger deployed from this machine (`hyra server up`)")
        return
    print(f"target: {s['target']}  url: {s['url']}  ({s.get('expose')}; {s['mode']})" + ("  [stopped]" if s.get("stopped") else ""))
    print(f"healthy: {http_ok(s['url'])}   saved tokens: {'human, runner' if s.get('human_token') else 'none'}  ({state_path()})")
    try:
        import urllib.request
        req = urllib.request.Request(s["url"].rstrip("/") + "/v1/stats", headers={"Authorization": f"Bearer {s.get('runner_token', '')}"})
        st = json.loads(urllib.request.urlopen(req, timeout=6).read())
        print(f"size: {st['db_bytes'] / 1024:.0f} KB   projects: {st['projects']}   sealed claims: {st['claims']}   reviews: {st['reviews']}   "
              f"approvals: {st['approvals']}   live tokens: {st['tokens_live']}   audit rows: {st['audit_rows']}")
    except Exception:
        pass
