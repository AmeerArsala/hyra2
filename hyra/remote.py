"""Client for the optional ledger server (hyra/server.py). Inactive unless the project's config has a `server` entry.

Config (`.hyra/config.json`):  "server": {"url": "http://blend:7421", "project": "<uuid>"}   (written by `hyra server connect`)
Credentials come only from the environment of the process that needs them, never from the project directory:
  HYRA_TOKEN         this agent session's scoped token (the scheduler mints one per session)
  HYRA_RUNNER_TOKEN  the scheduler's token (mints session tokens); stripped from every agent session's environment
  HYRA_HUMAN_TOKEN   the human's token, or typed at a terminal prompt; used only to decide approvals and mint tokens
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

from .store import HyraError, Store

SEALED = "[sealed on the ledger server]"


def enabled(st: Store) -> bool:
    return bool((st.config.get("server") or {}).get("url"))


def _cfg(st: Store) -> dict:
    cfg = st.config.get("server") or {}
    if not cfg.get("url") or not cfg.get("project"):
        raise HyraError("ledger server not configured: `hyra server connect --url URL`")
    return cfg


def call(st: Store, method: str, path: str, body: Optional[dict] = None, token: Optional[str] = None) -> Any:
    cfg = _cfg(st)
    tok = token or os.environ.get("HYRA_TOKEN") or os.environ.get("HYRA_RUNNER_TOKEN") or os.environ.get("HYRA_HUMAN_TOKEN")
    if not tok and sys.stdin.isatty():  # you, at a terminal, reading approvals
        tok = _saved().get("human_token")
    if not tok:
        tok = _ambient_overall_token(cfg)  # the interactive agent (Overall) with no token of its own
    req = urllib.request.Request(cfg["url"].rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok or ''}", "X-Hyra-Project": cfg["project"],
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", "")
        except Exception:
            msg = ""
        hint = ""
        if e.code == 401 and not tok:
            hint = " (this shell has no ledger token: start your agent with `hyra session <agent command>`, e.g. `hyra session claude`)"
        raise HyraError(f"ledger server refused ({e.code}): {msg}{hint}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HyraError(f"ledger server unreachable at {cfg['url']}: {e}. Failing closed (nothing was recorded).")


def _saved() -> dict:
    """Connection and tokens written by `hyra server up` (~/.hyra/ledger.json, mode 600)."""
    try:
        from .ledger_deploy import load_state
        return load_state()
    except Exception:
        return {}


def _ambient_overall_token(cfg: dict) -> Optional[str]:
    """The interactive agent is Overall; with no token in its environment it uses a short-lived `overall` token minted from the
    saved runner token (cached in ~/.hyra/ledger.json). Agent sessions launched by the scheduler always carry their own scoped
    token and a role in HYRA_ROLE, so they never get here: a worker or reviewer without a token is refused, not upgraded."""
    if os.environ.get("HYRA_ROLE") not in (None, "", "overall"):
        return None
    try:
        from .ledger_deploy import _age_seconds, datetime_now_iso, load_state, save_state, state_path
        from datetime import datetime, timedelta, timezone
        s = load_state()
        if not s.get("runner_token") or s.get("url", "").rstrip("/") != cfg["url"].rstrip("/"):
            return None
        if s.get("overall_token") and _age_seconds(s.get("overall_expires")) < -1800:  # more than 30 min of life left
            return s["overall_token"]
        req = urllib.request.Request(cfg["url"].rstrip("/") + "/v1/token", method="POST",
                                     data=json.dumps({"role": "overall", "ttl_hours": 4}).encode(),
                                     headers={"Authorization": f"Bearer {s['runner_token']}", "X-Hyra-Project": cfg["project"],
                                              "Content-Type": "application/json"})
        tok = json.loads(urllib.request.urlopen(req, timeout=15).read())["token"]
        s["overall_token"] = tok
        s["overall_expires"] = (datetime.now(timezone.utc) + timedelta(hours=4)).replace(microsecond=0).isoformat()
        save_state(s)
        return tok
    except Exception:
        return None


def runner_token() -> Optional[str]:
    """The scheduler's token. Only the scheduler process uses the saved copy (and strips it from every agent session)."""
    return os.environ.get("HYRA_RUNNER_TOKEN") or _saved().get("runner_token")


def human_token() -> str:
    tok = os.environ.get("HYRA_HUMAN_TOKEN")
    if tok:
        return tok
    if sys.stdin.isatty():  # a real terminal: you. An agent's shell tool is never a tty, so it can't use the saved copy this way.
        saved = _saved().get("human_token")
        return saved or getpass.getpass("human token: ")
    raise HyraError("deciding approvals needs the human token: run in a terminal (you'll be prompted) or set HYRA_HUMAN_TOKEN")


# ---- claims -------------------------------------------------------------------------------------------------------
def seal_claim(st: Store, tid: int, summary: str, finding: str) -> None:
    call(st, "POST", f"/v1/claims/{tid}", {"summary": summary, "finding": finding})


def fetch_claim(st: Store, tid: int) -> str:
    try:
        return call(st, "GET", f"/v1/claims/{tid}")["summary"]
    except HyraError as e:
        return f"[claim is sealed on the ledger server and this session can't read it: {e}]"


# ---- reviews ------------------------------------------------------------------------------------------------------
def register_review(st: Store, rid: int, target: int) -> None:
    call(st, "POST", "/v1/reviews", {"review_task": rid, "target_task": target})


def review_blind(st: Store, rid: int, notes: str) -> None:
    call(st, "POST", f"/v1/reviews/{rid}/blind", {"notes": notes})


def review_claim(st: Store, rid: int) -> dict:
    return call(st, "GET", f"/v1/reviews/{rid}/claim")


def review_verdict(st: Store, rid: int, verdict: str, notes: str) -> None:
    call(st, "POST", f"/v1/reviews/{rid}/verdict", {"verdict": verdict, "notes": notes})


# ---- approvals ----------------------------------------------------------------------------------------------------
def approval_create(st: Store, kind: str, what: str, task: Optional[int], ref: Optional[str]) -> int:
    return call(st, "POST", "/v1/approvals", {"kind": kind, "what": what, "task": task, "ref": ref})["id"]


def approval_get(st: Store, remote_id: int) -> dict:
    return call(st, "GET", f"/v1/approvals/{remote_id}")


def approval_granted(st: Store, kind: str, ref: str) -> bool:
    rows = call(st, "GET", f"/v1/approvals?status=granted&kind={kind}&ref={ref}")["approvals"]
    return bool(rows)


def approval_decide(st: Store, remote_id: int, grant: bool, by: str, note: str) -> None:
    call(st, "POST", f"/v1/approvals/{remote_id}/decide", {"decision": "grant" if grant else "deny", "by": by, "note": note},
         token=human_token())


# ---- sessions -----------------------------------------------------------------------------------------------------
def mint_session_token(st: Store, role: str, scope: str = "") -> str:
    tok = runner_token()
    if not tok:
        raise HyraError("server mode: the scheduler needs the runner token (set HYRA_RUNNER_TOKEN, or `hyra server up` saves it)")
    return call(st, "POST", "/v1/token", {"role": role, "scope": scope}, token=tok)["token"]


def mint_for_human(st: Store, role: str, scope: str = "", ttl_hours: Optional[float] = None) -> str:
    body: dict = {"role": role, "scope": scope}
    if ttl_hours is not None:
        body["ttl_hours"] = ttl_hours
    return call(st, "POST", "/v1/token", body, token=human_token())["token"]
