"""Durable state for a Hyra project: paths, config, and the SQLite store.

Layout (per research project):
    <project>/.hyra/
        hyra.db            task queue, Research Board, messages, approvals, reviews, evidence
        config.json        resources, approval rules, runner command
        memory/            per-agent hierarchical working memory (markdown)
        books/             project-local Books entries (shared knowledge, pre-sync)
        skills/            skills distilled during retrospectives (pre-publish)
        evidence/<task>/   copies of small evidence files attached on completion
        eb/<name>/         Hyra 1.0-style Experience Banks for score-driven loops
        logs/              runner logs for headless agent sessions

Global library (shared across projects; human-approved syncs only):
    ~/.hyra/library/books/   ~/.hyra/library/skills/
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

HYRA_DIR = ".hyra"

DEFAULT_CONFIG: dict[str, Any] = {
    "project": "",
    "goal": "",
    # Capacity of each resource. Every running task implicitly uses 1 "workers".
    "resources": {"workers": 4, "gpu": 0},
    # A task requesting >= this amount of a resource needs human approval before it can run.
    "approval_rules": {"gpu": 1},
    # Agent harness used by `hyra run` for headless sessions, unless a task sets its own (--harness).
    # Commands in "harness_cmds" override/extend HARNESS_PRESETS. {prompt} and {model} are
    # substituted; the flag right before "{model}" is dropped when no model is set.
    "harness": "claude",
    "harness_cmds": {},
    "max_missions_running": 2,
    "max_attempts": 2,
    "poll_seconds": 30,
    # Hard wall-clock cap per headless session, and how long a session may linger after its task
    # is already done/failed/sleeping before the runner stops it.
    "session_max_minutes": 240,
    "settle_grace_seconds": 90,
    # Optional prefix for Experience Bank evaluation, e.g. ["docker", "run", "--rm", ...].
    "eb_sandbox_prefix": [],
    "evidence_copy_max_bytes": 2_000_000,
}

# Headless invocations per harness. The role protocol is embedded in {prompt}, so no
# harness-specific agent definitions are needed. Permission flags are deliberately autonomous
# (unattended workers can't answer prompts); override per project in config "harness_cmds".
HARNESS_PRESETS: dict[str, list[str]] = {
    # Claude Code: role loaded natively as the subagent definition (~/.claude/agents/hyra-<role>.md,
    # installed by `hyra setup`); if that file is missing, "--agent {agent}" is dropped and the
    # role is embedded in the prompt like for every other harness.
    "claude": ["claude", "-p", "--permission-mode", "auto", "--agent", "{agent}", "--model", "{model}", "{prompt}"],
    "codex": ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check",
              "-m", "{model}", "{prompt}"],
    # Antigravity CLI (Google's successor to Gemini CLI); Go-style flags must precede the prompt.
    "agy": ["agy", "-p", "--dangerously-skip-permissions", "--model", "{model}", "{prompt}"],
    "opencode": ["opencode", "run", "-m", "{model}", "{prompt}"],
    "amp": ["amp", "--dangerously-allow-all", "-x", "{prompt}"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS missions (
    slug TEXT PRIMARY KEY, title TEXT, problem TEXT, status TEXT DEFAULT 'active',
    priority INTEGER DEFAULT 50, conclusion TEXT, wake_at TEXT, wake_reason TEXT,
    created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mission TEXT, kind TEXT, title TEXT, spec TEXT,
    status TEXT, priority INTEGER DEFAULT 50, deps TEXT DEFAULT '[]', resources TEXT DEFAULT '{}',
    model TEXT, worker TEXT, claimed_at TEXT, wake_at TEXT, wake_reason TEXT, summary TEXT,
    review_of INTEGER, review_verdict TEXT, attempts INTEGER DEFAULT 0, pid INTEGER,
    created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS task_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task INTEGER, author TEXT, text TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task INTEGER, kind TEXT, ref TEXT, sha256 TEXT,
    stored_path TEXT, note TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS board (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mission TEXT, task INTEGER, author TEXT, type TEXT,
    text TEXT, refs TEXT DEFAULT '[]', created TEXT);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, recipient TEXT, subject TEXT, body TEXT,
    requires_response INTEGER DEFAULT 0, response TEXT, responded_at TEXT, read_at TEXT,
    created TEXT);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, what TEXT, task INTEGER, ref TEXT,
    status TEXT DEFAULT 'pending', requested_by TEXT, decided_by TEXT, decided_at TEXT, note TEXT,
    created TEXT);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, review_task INTEGER UNIQUE, target_task INTEGER,
    phase TEXT DEFAULT 'blind', blind_notes TEXT, verdict TEXT, notes TEXT,
    created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS agent_state (
    agent TEXT PRIMARY KEY, last_board_id INTEGER DEFAULT 0, last_seen TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, task INTEGER, mission TEXT, pid INTEGER,
    log TEXT, started TEXT, ended TEXT, exit_code INTEGER);
"""


class HyraError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_when(s: str) -> str:
    """Accept '+30m', '+2h', '+1d', or an ISO timestamp; return ISO UTC."""
    s = s.strip()
    if s.startswith("+"):
        unit = s[-1]
        n = float(s[1:-1])
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
                 "h": timedelta(hours=n), "d": timedelta(days=n)}.get(unit)
        if delta is None:
            raise HyraError(f"bad relative time {s!r}; use +30m, +2h, +1d")
        return (datetime.now(timezone.utc) + delta).replace(microsecond=0).isoformat()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise HyraError(f"bad time {s!r}: {e}")
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpret naive times as local
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def is_due(ts: Optional[str]) -> bool:
    return bool(ts) and datetime.fromisoformat(ts) <= datetime.now(timezone.utc)


def skill_dir() -> Path:
    """The `hyra` skill folder (SKILL.md, roles/, scripts/hyra). In a checkout it sits next to this
    package; for an installed package, fall back to wherever the skill was installed."""
    home = Path.home()
    cands = [Path(__file__).resolve().parent.parent / "skills" / "hyra",
             Path(os.environ.get("HYRA_SKILL_DIR", "/nonexistent")),
             home / ".agents" / "skills" / "hyra",
             Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")) / "skills" / "hyra"]
    for c in cands:
        if (c / "roles" / "worker.md").exists():
            return c.resolve()
    raise HyraError("can't find the hyra skill folder (roles/*.md); set HYRA_SKILL_DIR")


def library_root() -> Path:
    return Path(os.environ.get("HYRA_LIBRARY", Path.home() / ".hyra" / "library"))


def find_root(start: Optional[Path] = None) -> Path:
    env = os.environ.get("HYRA_PROJECT")
    if env:
        p = Path(env).expanduser().resolve()
        if (p / HYRA_DIR).is_dir():
            return p
        raise HyraError(f"HYRA_PROJECT={env} has no {HYRA_DIR}/ directory")
    cur = (start or Path.cwd()).resolve()
    for p in [cur, *cur.parents]:
        if (p / HYRA_DIR).is_dir():
            return p
    raise HyraError("not inside a Hyra project (no .hyra/ found). Run `hyra init` first.")


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.dir = root / HYRA_DIR
        self.db = sqlite3.connect(str(self.dir / "hyra.db"), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(tasks)")}
        if "harness" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN harness TEXT")
        self.config = self._load_config()

    def harness_cmd(self, name: str) -> list[str]:
        cmds = dict(HARNESS_PRESETS, **self.config.get("harness_cmds", {}))
        if name not in cmds:
            raise HyraError(f"unknown harness {name!r}; known: {', '.join(sorted(cmds))} "
                            "(add one under \"harness_cmds\" in .hyra/config.json)")
        return cmds[name]

    # ---- construction -------------------------------------------------
    @classmethod
    def init(cls, root: Path, project: str, goal: str) -> "Store":
        d = root / HYRA_DIR
        for sub in ["memory", "books", "skills", "evidence", "eb", "logs"]:
            (d / sub).mkdir(parents=True, exist_ok=True)
        cfg_path = d / "config.json"
        if not cfg_path.exists():
            cfg = dict(DEFAULT_CONFIG, project=project, goal=goal)
            cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
        st = cls(root)
        mem = st.memory_path("overall")
        if not mem.exists():
            mem.write_text(memory_template("overall"))
        return st

    @classmethod
    def open(cls) -> "Store":
        return cls(find_root())

    def _load_config(self) -> dict[str, Any]:
        p = self.dir / "config.json"
        cfg = dict(DEFAULT_CONFIG)
        if p.exists():
            cfg.update(json.loads(p.read_text()))
        return cfg

    # ---- helpers --------------------------------------------------------
    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, *args: Any) -> Optional[sqlite3.Row]:
        return self.db.execute(sql, args).fetchone()

    def x(self, sql: str, *args: Any) -> int:
        cur = self.db.execute(sql, args)
        return cur.lastrowid

    def tx(self):
        """BEGIN IMMEDIATE transaction context so claims are atomic across processes."""
        return _Tx(self.db)

    def task(self, tid: int) -> sqlite3.Row:
        row = self.one("SELECT * FROM tasks WHERE id=?", tid)
        if not row:
            raise HyraError(f"no task {tid}")
        return row

    def mission(self, slug: str) -> sqlite3.Row:
        row = self.one("SELECT * FROM missions WHERE slug=?", slug)
        if not row:
            raise HyraError(f"no mission {slug!r}")
        return row

    def memory_path(self, agent: str) -> Path:
        safe = agent.replace(":", "__").replace("/", "_")
        return self.dir / "memory" / f"{safe}.md"

    def post(self, author: str, type_: str, text: str, mission: Optional[str] = None,
             task: Optional[int] = None, refs: Optional[list] = None) -> int:
        return self.x("INSERT INTO board(mission,task,author,type,text,refs,created) VALUES (?,?,?,?,?,?,?)",
                      mission, task, author, type_, text, json.dumps(refs or []), now())

    def note(self, tid: int, author: str, text: str) -> None:
        self.x("INSERT INTO task_notes(task,author,text,created) VALUES (?,?,?,?)", tid, author, text, now())

    def set_task(self, tid: int, **fields: Any) -> None:
        fields["updated"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE tasks SET {cols} WHERE id=?", *fields.values(), tid)


class _Tx:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def __enter__(self):
        for _ in range(100):
            try:
                self.db.execute("BEGIN IMMEDIATE")
                return self
            except sqlite3.OperationalError:
                time.sleep(0.1)
        raise HyraError("database busy")

    def __exit__(self, exc_type, *_):
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")


def memory_template(agent: str) -> str:
    return f"""# Working memory: {agent}

Hierarchical, self-maintained. Keep it short; rewrite rather than append.
Promote anything reusable beyond this project to Books (`hyra books add`).

## Current understanding
- (what is true right now, with pointers to evidence: task ids, board ids, paths)

## Key hypotheses
- (open bets; for each: why we believe it, what would falsify it, which task tests it)

## Ruled out
- (approaches tried and rejected, with the evidence that killed them, so nobody retries them blindly)

## Next steps
- (ordered; what you will do on next wake-up)
"""


def die(msg: str) -> None:
    print(f"hyra: error: {msg}", file=sys.stderr)
    sys.exit(1)
