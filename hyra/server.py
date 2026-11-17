"""`hyra serve`: an optional ledger server that turns two CLI rules into real boundaries.

Without it, "a blind reviewer can't see the claim" and "agents can't grant approvals" are rules inside a CLI that runs as the
same OS user as the agents, so a determined agent could open `.hyra/hyra.db`. In server mode the secrets live on another
machine (e.g. a tailnet device) and each agent session holds only its own scoped token:

  * worker claims are SEALED on the server (the project's local database never contains them);
  * the blind-review state machine runs on the server: a reviewer's token cannot fetch the claim until it has submitted its
    own blind assessment, and verdicts are immutable;
  * approvals live on the server and only the human token can decide them; the scheduler asks the server before running
    anything that needed approval, so editing the local database doesn't release gated work.

Tokens (sent as `Authorization: Bearer ...`): `human` (decides approvals, mints tokens, reads everything), `runner` (the scheduler:
mints session tokens, can't read claims or decide approvals) and per-session tokens minted for each agent: worker (scoped to a
task), reviewer (scoped to a review task), mission, overall, retrospective, and non-privileged roles (context/proposer/evaluator).

Threat model, plainly: this stops an agent from reading sealed claims or granting approvals by touching files, and from doing so
without a credential. It does not stop an agent that goes hunting for credentials in its parent processes, and the worker's raw
session log (`.hyra/logs/`) still exists locally. Stdlib only; use it over a private network (a tailnet is WireGuard-encrypted).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

PRIVILEGED_READERS = {"human", "mission", "overall", "retrospective"}   # may read any task's claim (they are not blinded)
SESSION_ROLES = {"worker", "reviewer", "mission", "overall", "retrospective", "context", "proposer", "evaluator"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, role TEXT, project TEXT, scope TEXT, created TEXT, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS claims (project TEXT, task INTEGER, summary TEXT, finding TEXT, author TEXT, created TEXT, PRIMARY KEY (project, task));
CREATE TABLE IF NOT EXISTS reviews (project TEXT, review_task INTEGER, target_task INTEGER, phase TEXT DEFAULT 'blind',
    blind_notes TEXT, verdict TEXT, notes TEXT, created TEXT, PRIMARY KEY (project, review_task));
CREATE TABLE IF NOT EXISTS approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, kind TEXT, what TEXT, task INTEGER, ref TEXT,
    status TEXT DEFAULT 'pending', requested_by TEXT, decided_by TEXT, decided_at TEXT, note TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS audit (ts TEXT, project TEXT, role TEXT, action TEXT, detail TEXT);
"""


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Ledger:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "ledger.db"
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        os.chmod(self.path, 0o600)

    def q(self, sql, *a):
        with self.lock:
            return self.db.execute(sql, a).fetchall()

    def one(self, sql, *a):
        with self.lock:
            return self.db.execute(sql, a).fetchone()

    def x(self, sql, *a) -> int:
        with self.lock:
            return self.db.execute(sql, a).lastrowid

    def mint(self, role: str, project: str = "*", scope: str = "") -> str:
        tok = f"hyra_{role}_{secrets.token_hex(24)}"
        self.x("INSERT INTO tokens(hash,role,project,scope,created) VALUES (?,?,?,?,?)", _hash(tok), role, project, scope, _now())
        return tok

    def whoami(self, token: Optional[str]):
        return self.one("SELECT * FROM tokens WHERE hash=? AND revoked=0", _hash(token)) if token else None

    def audit(self, who, project, action, detail=""):
        self.x("INSERT INTO audit VALUES (?,?,?,?,?)", _now(), project, who["role"] if who else "?", action, detail)


class Denied(Exception):
    def __init__(self, msg: str, code: int = 403):
        super().__init__(msg)
        self.code = code


def make_handler(ledger: Ledger):
    class H(BaseHTTPRequestHandler):
        server_version = "hyra-ledger/1"

        def log_message(self, *a):  # quiet; the audit table is the record
            pass

        # ---- plumbing
        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _who(self):
            auth = self.headers.get("Authorization", "")
            who = ledger.whoami(auth[7:] if auth.startswith("Bearer ") else None)
            if not who:
                raise Denied("missing or invalid token", 401)
            return who

        def _project(self, who) -> str:
            proj = self.headers.get("X-Hyra-Project", "")
            if not proj:
                raise Denied("X-Hyra-Project header required", 400)
            if who["project"] not in ("*", proj):
                raise Denied("token is for another project")
            return proj

        def _route(self, method: str):
            path = self.path.split("?")[0].rstrip("/")
            query = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&")) if "?" in self.path else {}
            if path == "/v1/health":
                return 200, {"ok": True}
            who = self._who()
            proj = self._project(who)
            role, scope = who["role"], who["scope"]
            body = self._body() if method == "POST" else {}

            if path == "/v1/token" and method == "POST":
                if role not in ("human", "runner"):
                    raise Denied("only the human or the runner token can mint tokens")
                want = body.get("role", "")
                if want not in SESSION_ROLES | {"runner"} or (want == "runner" and role != "human"):
                    raise Denied(f"cannot mint role {want!r}")
                return 200, {"token": ledger.mint(want, proj, str(body.get("scope", "")))}

            m = re.fullmatch(r"/v1/claims/(\d+)", path)
            if m:
                task = int(m.group(1))
                if method == "POST":  # a worker seals its own task's claim (a mission/human may too)
                    if not (role in ("human", "mission", "overall") or (role == "worker" and scope == str(task))):
                        raise Denied("only the task's worker can seal its claim")
                    ledger.x("INSERT OR REPLACE INTO claims VALUES (?,?,?,?,?,?)", proj, task, body.get("summary", ""),
                             body.get("finding", ""), role, _now())
                    ledger.audit(who, proj, "seal", f"T{task}")
                    return 200, {"sealed": task}
                if not (role in PRIVILEGED_READERS or (role == "worker" and scope == str(task))):
                    raise Denied("this token cannot read claims")
                row = ledger.one("SELECT summary, finding FROM claims WHERE project=? AND task=?", proj, task)
                if not row:
                    raise Denied("no sealed claim for that task", 404)
                ledger.audit(who, proj, "read-claim", f"T{task}")
                return 200, {"summary": row["summary"], "finding": row["finding"]}

            if path == "/v1/reviews" and method == "POST":
                if role not in ("human", "runner", "mission", "overall"):
                    raise Denied("this token cannot register reviews")
                ledger.x("INSERT OR IGNORE INTO reviews(project,review_task,target_task,created) VALUES (?,?,?,?)",
                         proj, int(body["review_task"]), int(body["target_task"]), _now())
                return 200, {"registered": body["review_task"]}

            m = re.fullmatch(r"/v1/reviews/(\d+)/(blind|claim|verdict|status)", path)
            if m:
                rid, action = int(m.group(1)), m.group(2)
                if not (role == "human" or (role == "reviewer" and scope == str(rid))):
                    raise Denied("only the review's own reviewer token may use this")
                rv = ledger.one("SELECT * FROM reviews WHERE project=? AND review_task=?", proj, rid)
                if not rv:
                    raise Denied("review not registered", 404)
                if action == "status":
                    return 200, {"phase": rv["phase"], "verdict": rv["verdict"]}
                if action == "blind":
                    if rv["phase"] != "blind":
                        raise Denied("blind assessment already recorded", 409)
                    ledger.x("UPDATE reviews SET blind_notes=?, phase='revealed' WHERE project=? AND review_task=?",
                             body.get("notes", ""), proj, rid)
                    ledger.audit(who, proj, "blind", f"review T{rid}")
                    return 200, {"phase": "revealed"}
                if action == "claim":
                    if rv["phase"] == "blind":
                        raise Denied("record your blind assessment first")  # <- the whole point
                    c = ledger.one("SELECT summary, finding FROM claims WHERE project=? AND task=?", proj, rv["target_task"])
                    ledger.audit(who, proj, "reveal-claim", f"review T{rid} -> T{rv['target_task']}")
                    return 200, {"summary": c["summary"] if c else "(no sealed claim)", "blind_notes": rv["blind_notes"]}
                if action == "verdict" and method == "POST":
                    if rv["phase"] == "blind":
                        raise Denied("record a blind assessment before the verdict")
                    if rv["verdict"]:
                        raise Denied("verdict already recorded; verdicts are immutable", 409)
                    ledger.x("UPDATE reviews SET verdict=?, notes=?, phase='final' WHERE project=? AND review_task=?",
                             body["verdict"], body.get("notes", ""), proj, rid)
                    ledger.audit(who, proj, "verdict", f"review T{rid}: {body['verdict']}")
                    return 200, {"verdict": body["verdict"]}

            if path == "/v1/approvals" and method == "POST":
                if role == "runner":
                    raise Denied("the runner token cannot request approvals")
                aid = ledger.x("INSERT INTO approvals(project,kind,what,task,ref,requested_by,created) VALUES (?,?,?,?,?,?,?)",
                               proj, body.get("kind", "other"), body.get("what", ""), body.get("task"), body.get("ref"), role, _now())
                ledger.audit(who, proj, "approval-request", f"A{aid} {body.get('kind')}")
                return 200, {"id": aid}
            if path == "/v1/approvals" and method == "GET":
                rows = ledger.q("SELECT * FROM approvals WHERE project=?" + (" AND status=?" if "status" in query else "") +
                                (" AND kind=?" if "kind" in query else "") + (" AND ref=?" if "ref" in query else "") + " ORDER BY id",
                                proj, *[query[k] for k in ("status", "kind", "ref") if k in query])
                return 200, {"approvals": [dict(r) for r in rows]}
            m = re.fullmatch(r"/v1/approvals/(\d+)(/decide)?", path)
            if m:
                aid = int(m.group(1))
                row = ledger.one("SELECT * FROM approvals WHERE project=? AND id=?", proj, aid)
                if not row:
                    raise Denied("no such approval", 404)
                if not m.group(2):
                    return 200, dict(row)
                if role != "human":
                    raise Denied("only the human token can decide approvals")
                if row["status"] != "pending":
                    raise Denied(f"already {row['status']}", 409)
                status = "granted" if body.get("decision") == "grant" else "denied"
                ledger.x("UPDATE approvals SET status=?, decided_by=?, decided_at=?, note=? WHERE id=?",
                         status, body.get("by", "human"), _now(), body.get("note", ""), aid)
                ledger.audit(who, proj, f"approval-{status}", f"A{aid}")
                return 200, {"id": aid, "status": status}
            raise Denied("not found", 404)

        def _handle(self, method: str):
            try:
                code, obj = self._route(method)
            except Denied as e:
                code, obj = e.code, {"error": str(e)}
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                code, obj = 400, {"error": f"bad request: {e}"}
            self._send(code, obj)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return H


def serve(data_dir: str, host: str, port: int, quiet_tokens: bool = False, rotate: bool = False) -> None:
    """quiet_tokens: write the tokens to <data>/initial-tokens.txt (0600) but don't print them (use under systemd, whose
    journal would otherwise keep them). rotate: revoke every human/runner token and mint fresh ones (session tokens stay)."""
    d = Path(data_dir).expanduser()
    ledger = Ledger(d)
    first = not ledger.one("SELECT 1 FROM tokens WHERE role='human' AND revoked=0")
    if first or rotate:
        if rotate:
            ledger.x("UPDATE tokens SET revoked=1 WHERE role IN ('human','runner')")
        human, runner = ledger.mint("human"), ledger.mint("runner")
        f = d / "initial-tokens.txt"
        f.write_text(f"HYRA_HUMAN_TOKEN={human}\nHYRA_RUNNER_TOKEN={runner}\n")
        os.chmod(f, 0o600)
        if quiet_tokens:
            print(f"{'Rotated' if rotate else 'First run'}: new human and runner tokens written to {f} (not printed). "
                  "Copy them somewhere safe, then delete the file.")
        else:
            print(f"{'Tokens rotated' if rotate else 'First run'}. Save these now (also in {f}, readable only by you; delete it afterwards):")
            print(f"  human  token (decides approvals; keep it to yourself): {human}")
            print(f"  runner token (for the scheduler; `hyra run` reads HYRA_RUNNER_TOKEN): {runner}")
    httpd = ThreadingHTTPServer((host, port), make_handler(ledger))
    print(f"hyra ledger listening on http://{host}:{port}  (data: {d})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
