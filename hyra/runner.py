"""`hyra run`: a scheduler that keeps research moving outside any single agent session.

Each pass it:
  1. reaps finished sessions (a worker that exits while its task is still 'running' gets the
     task requeued until max_attempts, then failed);
  2. wakes Missions whose wake-up time has come, who have unread mail, or whose tasks changed
     since their last briefing while they have nothing left in flight;
  3. claims ready tasks (priority, dependencies, resource limits, approvals) and launches a
     headless Worker/Reviewer session per task.
Agents sleep by calling `hyra task sleep` / `hyra mission wake` and exiting; the runner resumes them.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .core import fits, ready, task_claim, usage
from .store import Store, is_due, now, skill_dir


WORKER_PROMPT = """You are a Hyra Worker on task T{tid} in the Hyra project at {root}.
Begin with `hyra brief task:{tid}` and follow your Worker protocol.
Before you exit you MUST leave the task in a terminal or parked state with exactly one of:
`hyra task complete {tid} --summary ... --evidence ...`, `hyra task fail {tid} --reason ...`,
or `hyra task sleep {tid} --until ... --reason ...`."""

REVIEWER_PROMPT = """You are a Hyra Review Worker on review task T{tid} in the Hyra project at {root}.
Begin with `hyra brief task:{tid}`, then follow your blind review protocol exactly
(evidence -> blind assessment -> claim -> verdict). Finish with `hyra review verdict {tid} ...`."""

MISSION_PROMPT = """You are the Hyra Mission agent for mission '{slug}' in the Hyra project at {root}.
You were woken because: {reason}.
Begin with `hyra brief mission:{slug}` and follow your Mission protocol: read results, request
reviews for load-bearing claims, update your memory, queue the next tasks, and set your next wake-up
(`hyra mission wake {slug} --at ... --reason ...`) before exiting. Do not run long experiments yourself."""


def role_text(role: str) -> str:
    """The role protocol without its YAML frontmatter (which exists for Claude Code subagents)."""
    text = (skill_dir() / "roles" / f"{role}.md").read_text()
    if text.startswith("---\n"):
        text = text[text.index("\n---\n", 4) + 5:]
    return text.strip()


def compose(header: str, role: str) -> str:
    return f"{header}\n\nThe `hyra` CLI is on your PATH. Your role protocol follows.\n\n{role_text(role)}\n"


def claude_agent_installed(role: str) -> bool:
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return (claude / "agents" / f"hyra-{role}.md").exists()


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cmd(tpl: list[str], model: Optional[str], agent: Optional[str], prompt: str) -> list[str]:
    """Fill a harness template. Optional placeholders ({model}, {agent}) are dropped together with
    the flag in front of them when they have no value."""
    vals = {"{model}": model, "{agent}": agent}
    out = []
    for i, part in enumerate(tpl):
        nxt = tpl[i + 1] if i + 1 < len(tpl) else None
        if nxt in vals and not vals[nxt]:
            continue
        if part in vals and not vals[part]:
            continue
        out.append(part.replace("{model}", model or "").replace("{agent}", agent or "").replace("{prompt}", prompt))
    return out


class Runner:
    def __init__(self, st: Store, max_workers: Optional[int], dry_run: bool,
                 harness: Optional[str] = None, model: Optional[str] = None):
        self.st = st
        self.dry = dry_run
        self.harness = harness
        self.model = model
        self.me = f"runner:{os.getpid()}"
        self.procs: dict[int, tuple[subprocess.Popen, int, Optional[int], Optional[str]]] = {}
        if max_workers is not None:
            st.config.setdefault("resources", {})["workers"] = max_workers

    # -------------------------------------------------------------- launching
    def _launch(self, agent_id: str, role: str, header: str, model: Optional[str], harness: Optional[str],
                task: Optional[int], mission: Optional[str]) -> None:
        harness = self.harness or harness or self.st.config.get("harness", "claude")
        model = self.model or model
        tpl = self.st.harness_cmd(harness)
        # Harnesses with native agent definitions (Claude Code: --agent hyra-<role>) load the role as
        # the session's own system prompt. Everyone else gets the protocol embedded in the prompt.
        native = "{agent}" in tpl and claude_agent_installed(role)
        prompt = header + "\n\nThe `hyra` CLI is on your PATH." if native else compose(header, role)
        cmd = _cmd(tpl, model, f"hyra-{role}" if native else None, prompt)
        if self.dry:
            print(f"[dry-run] would launch {agent_id} via {harness}: "
                  f"{' '.join(c if ' ' not in c else repr(c[:50] + '…') for c in cmd)}")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log = self.st.dir / "logs" / f"{agent_id.replace(':', '_')}-{stamp}.log"
        env = dict(os.environ, HYRA_PROJECT=str(self.st.root), HYRA_AGENT=agent_id, HYRA_ROLE=role,
                   PATH=f"{skill_dir() / 'scripts'}{os.pathsep}{os.environ.get('PATH', '')}")
        if task is not None:
            env["HYRA_TASK"] = str(task)
        fh = open(log, "w")
        p = subprocess.Popen(cmd, cwd=self.st.root, env=env, stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        sid = self.st.x("INSERT INTO sessions(agent,task,mission,pid,log,started) VALUES (?,?,?,?,?,?)",
                        agent_id, task, mission, p.pid, str(log), now())
        if task is not None:
            self.st.set_task(task, pid=p.pid)
        self.procs[p.pid] = (p, sid, task, mission)
        print(f"{now()} launched {agent_id} via {harness} pid {p.pid} log {log}")

    # -------------------------------------------------------------- reaping
    def _overdue(self, sid: int, task: Optional[int]) -> Optional[str]:
        """A session that already settled its task (or ran past the cap) but won't exit, e.g. one
        waiting on a hung background shell. Returns why it should be stopped."""
        started = self.st.one("SELECT started FROM sessions WHERE id=?", sid)["started"]
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds()
        cap = float(self.st.config.get("session_max_minutes", 240)) * 60
        if age > cap:
            return f"session exceeded session_max_minutes ({cap / 60:g})"
        if task is not None:
            t = self.st.task(task)
            if t["status"] != "running" or t["worker"] != self.me:
                since = (datetime.now(timezone.utc) - datetime.fromisoformat(t["updated"])).total_seconds()
                if since > float(self.st.config.get("settle_grace_seconds", 90)):
                    return f"task already {t['status']} {since:.0f}s ago but session still alive"
        return None

    def reap(self) -> None:
        for pid, (p, sid, task, mission) in list(self.procs.items()):
            code = p.poll()
            if code is None:
                why = self._overdue(sid, task)
                if not why:
                    continue
                print(f"{now()} stopping pid {pid}: {why}")
                try:
                    os.killpg(pid, signal.SIGTERM)
                    p.wait(timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                    p.wait()
                code = p.returncode
            del self.procs[pid]
            self.st.x("UPDATE sessions SET ended=?, exit_code=? WHERE id=?", now(), code, sid)
            print(f"{now()} session pid {pid} exited {code}")
            if task is not None:
                self._settle(task, f"worker session exited (code {code}) without completing/sleeping the task")
        # Orphans from crashed runners or killed in-session workers launched by a runner.
        for t in self.st.q("SELECT * FROM tasks WHERE status='running' AND worker LIKE 'runner:%'"):
            if t["pid"] not in self.procs and not _alive(t["pid"]):
                self._settle(t["id"], "worker process no longer alive")

    def _settle(self, tid: int, why: str) -> None:
        t = self.st.task(tid)
        if t["status"] != "running":
            return
        maxa = int(self.st.config.get("max_attempts", 2))
        if t["attempts"] < maxa:
            self.st.set_task(tid, status="queued", worker=None, pid=None)
            self.st.note(tid, self.me, f"{why}; requeued (attempt {t['attempts']}/{maxa})")
        else:
            self.st.set_task(tid, status="failed", worker=None, pid=None, summary=why)
            self.st.note(tid, self.me, f"{why}; giving up after {t['attempts']} attempts")
            self.st.post(self.me, "finding", f"T{tid} FAILED: {why}", mission=t["mission"], task=tid)
            self.st.x("INSERT INTO messages(sender,recipient,subject,body,created) VALUES (?,?,?,?,?)",
                      self.me, f"mission:{t['mission']}", f"T{tid} failed", why, now())

    # -------------------------------------------------------------- scheduling
    def mission_reasons(self) -> list[tuple[str, str]]:
        running = {m for (_, _, _, m) in self.procs.values() if m}
        out = []
        for m in self.st.q("SELECT * FROM missions WHERE status='active' ORDER BY priority DESC"):
            slug = m["slug"]
            if slug in running:
                continue
            agent = f"mission:{slug}"
            state = self.st.one("SELECT * FROM agent_state WHERE agent=?", agent)
            last_seen = state["last_seen"] if state else ""
            mail = self.st.one("""SELECT COUNT(*) n FROM messages WHERE recipient=? AND
                                  (read_at IS NULL OR (requires_response=1 AND response IS NULL))""", agent)["n"]
            inflight = self.st.one("""SELECT COUNT(*) n FROM tasks WHERE mission=? AND status IN
                                      ('queued','running','sleeping')""", slug)["n"]
            changed = self.st.one("SELECT COUNT(*) n FROM tasks WHERE mission=? AND updated > ?", slug, last_seen or "")["n"]
            if not state:
                out.append((slug, "first wake-up: plan the mission"))
            elif is_due(m["wake_at"]):
                out.append((slug, f"scheduled wake-up: {m['wake_reason'] or 'no reason given'}"))
            elif mail:
                out.append((slug, f"{mail} message(s) in your inbox"))
            elif not inflight and changed:
                out.append((slug, "no tasks in flight; results to read or next steps to plan"))
        return out

    def tick(self) -> None:
        self.reap()
        cap = int(self.st.config.get("max_missions_running", 2))
        live_missions = sum(1 for v in self.procs.values() if v[3] and v[2] is None)
        for slug, reason in self.mission_reasons():
            if live_missions >= cap:
                break
            if not self.dry:
                self.st.x("UPDATE missions SET wake_at=NULL WHERE slug=?", slug)
            self._launch(f"mission:{slug}", "mission",
                         MISSION_PROMPT.format(slug=slug, root=self.st.root, reason=reason), None, None, None, slug)
            live_missions += 1
        if self.dry:
            used = usage(self.st)
            for t in ready(self.st):
                res = json.loads(t["resources"] or "{}")
                if fits(self.st, res, used):
                    used["workers"] = used.get("workers", 0) + 1
                    for k, v in res.items():
                        used[k] = used.get(k, 0) + float(v)
                    self._launch_task(t)
            return
        while True:
            tid = task_claim(self.st, self.me)
            if tid is None:
                break
            self._launch_task(self.st.task(tid))

    def _launch_task(self, t) -> None:
        tid = t["id"]
        if t["kind"] == "review":
            self._launch(f"task:{tid}", "reviewer", REVIEWER_PROMPT.format(tid=tid, root=self.st.root),
                         t["model"], t["harness"], tid, None)
        else:
            self._launch(f"task:{tid}", "worker", WORKER_PROMPT.format(tid=tid, root=self.st.root),
                         t["model"], t["harness"], tid, None)

    def run_one(self, tid: int) -> None:
        """Dispatch one specific task to a fresh headless session and wait (e.g. a cross-harness review)."""
        if self.dry:
            self._launch_task(self.st.task(tid))
            return
        task_claim(self.st, self.me, tid)
        self._launch_task(self.st.task(tid))
        while self.procs:
            time.sleep(2)
            self.reap()
        print(f"T{tid} is now {self.st.task(tid)['status']}")

    def loop(self, once: bool) -> None:
        stop = {"flag": False}

        def handle(*_):
            stop["flag"] = True
            print("stopping after current pass (running sessions continue; re-run `hyra run` to keep supervising)")
        signal.signal(signal.SIGINT, handle)
        signal.signal(signal.SIGTERM, handle)
        poll = float(self.st.config.get("poll_seconds", 30))
        while not stop["flag"]:
            self.tick()
            if once:
                if self.dry:
                    return
                while self.procs and not stop["flag"]:
                    time.sleep(2)
                    self.reap()
                return
            for _ in range(int(poll * 2)):
                if stop["flag"]:
                    break
                time.sleep(0.5)
                if any(p.poll() is not None for (p, _, _, _) in self.procs.values()):
                    break
