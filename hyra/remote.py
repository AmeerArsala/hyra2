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
        raise HyraError(f"ledger server refused ({e.code}): {msg}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HyraError(f"ledger server unreachable at {cfg['url']}: {e}. Failing closed (nothing was recorded).")


def human_token() -> str:
    tok = os.environ.get("HYRA_HUMAN_TOKEN")
    if tok:
        return tok
    if sys.stdin.isatty():
        return getpass.getpass("human token: ")
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
    tok = os.environ.get("HYRA_RUNNER_TOKEN")
    if not tok:
        raise HyraError("server mode: the scheduler needs HYRA_RUNNER_TOKEN (printed when `hyra server serve` first ran)")
    return call(st, "POST", "/v1/token", {"role": role, "scope": scope}, token=tok)["token"]


def mint_for_human(st: Store, role: str, scope: str = "") -> str:
    return call(st, "POST", "/v1/token", {"role": role, "scope": scope}, token=human_token())["token"]
