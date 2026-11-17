"""`hyra run`: a scheduler that keeps research moving outside any single agent session.

Each pass it:
  1. reaps finished sessions (a worker that exits while its task is still 'running' gets the
     task requeued until max_attempts, then failed);
  2. wakes Missions whose wake-up time has come, who have unread mail, or one of whose tasks
     finished (done/failed) since their last briefing;
  3. claims ready tasks (priority, dependencies, resource limits, approvals) and launches a
     headless Worker/Reviewer session per task.
Agents sleep by calling `hyra task sleep` / `hyra mission wake` and exiting; the runner resumes them.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .core import check_wake_conditions as core_check, fits, ready, retros_pending, task_claim, usage
from . import remote
from .install import native_agent_available
from .store import HyraError, Store, is_due, now, skill_dir


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


OVERALL_PROMPT = """You are Overall for the Hyra project at {root}, running unattended (no human is watching this session).
You were woken because: {reason}.
Begin with `hyra brief overall` and follow your Overall protocol: look after the missions, answer your mail, and message the
human only for what needs them. Never grant or deny approvals."""

RETRO_PROMPT = """You are the Hyra retrospective agent for the closed mission '{slug}' in the Hyra project at {root}.
The full procedure is the hyra-retro skill: read {skill} first and follow it. Begin with `hyra mission show {slug}`.
Finish by posting `hyra board post --type retro --mission {slug} "..."`."""


def find_role(role: str) -> Path:
    """Role files live in <skill>/roles/<role>.md across the hyra skills (hyra: worker/mission/reviewer,
    hyra-loop: context/proposer/evaluator, hyra-wiki: ...)."""
    for roles in sorted(skill_dir().parent.glob("*/roles")):
        if (roles / f"{role}.md").exists():
            return roles / f"{role}.md"
    raise HyraError(f"no role file {role}.md in any installed hyra skill")


def role_agent_name(role: str) -> str:
    m = re.search(r"^name:\s*(\S+)\s*$", find_role(role).read_text(encoding="utf-8").split("\n---", 1)[0], re.M)
    return m.group(1) if m else f"hyra-{role}"


def role_text(role: str) -> str:
    """The role protocol without its YAML frontmatter (which exists for agent-file harnesses)."""
    text = find_role(role).read_text(encoding="utf-8")
    if text.startswith("---\n"):
        text = text[text.index("\n---\n", 4) + 5:]
    return text.strip()


def compose(header: str, role: str) -> str:
    return f"{header}\n\nThe `hyra` CLI is on your PATH. Your role protocol follows.\n\n{role_text(role)}\n"


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
        self.agent_of: dict[int, str] = {}  # pid -> agent id of every live session
        if max_workers is not None:
            st.config.setdefault("resources", {})["workers"] = max_workers
        if remote.enabled(st) and not dry_run and not os.environ.get("HYRA_RUNNER_TOKEN"):
            raise HyraError("server mode: start the scheduler with HYRA_RUNNER_TOKEN set (it mints each agent's scoped token)")

    # -------------------------------------------------------------- launching
    def _launch(self, agent_id: str, role: str, header: str, model: Optional[str], harness: Optional[str],
                task: Optional[int], mission: Optional[str], extra_env: Optional[dict] = None) -> Optional[subprocess.Popen]:
        harness = self.harness or harness or self.st.config.get("harness", "claude")
        model = self.model or model or (self.st.config.get("harness_models") or {}).get(harness)
        tpl = self.st.harness_cmd(harness)
        # Harnesses with native agent definitions (Claude Code: --agent hyra-<role>) load the role as
        # the session's own system prompt. Everyone else gets the protocol embedded in the prompt.
        agent_name = role_agent_name(role)
        native = "{agent}" in tpl and native_agent_available(harness, agent_name, self.st.root)
        prompt = header + "\n\nThe `hyra` CLI is on your PATH." if native else compose(header, role)
        if remote.enabled(self.st) and harness == "codex" and "--sandbox" in tpl and not any("network_access" in c for c in tpl):
            # Codex's workspace-write sandbox has no network, so it couldn't reach the ledger (found live: completion failed
            # closed). Allow network only when a ledger is configured.
            tpl = tpl[:2] + ["-c", "sandbox_workspace_write.network_access=true"] + tpl[2:]
        cmd = _cmd(tpl, model, agent_name if native else None, prompt)
        if self.dry:
            print(f"[dry-run] would launch {agent_id} via {harness}: "
                  f"{' '.join(c if ' ' not in c else repr(c[:50] + '…') for c in cmd)}")
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log = self.st.dir / "logs" / f"{agent_id.replace(':', '_')}-{stamp}.log"
        env = dict(os.environ, HYRA_PROJECT=str(self.st.root), HYRA_AGENT=agent_id, HYRA_ROLE=role,
                   PATH=f"{skill_dir() / 'scripts'}{os.pathsep}{os.environ.get('PATH', '')}")
        if task is not None:
            env["HYRA_TASK"] = str(task)
        env.update(extra_env or {})
        # Privileged credentials never reach an agent session; each gets only its own scoped token (server mode).
        for secret in ("HYRA_RUNNER_TOKEN", "HYRA_HUMAN_TOKEN", "HYRA_TOKEN"):
            env.pop(secret, None)
        if remote.enabled(self.st):
            srole = {"overseer": "overall"}.get(role, role)
            scope = str(task) if role in ("worker", "reviewer") and task is not None else ""
            env["HYRA_TOKEN"] = remote.mint_session_token(self.st, srole, scope)
        fh = open(log, "w")
        p = subprocess.Popen(cmd, cwd=self.st.root, env=env, stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        sid = self.st.x("INSERT INTO sessions(agent,task,mission,pid,log,started) VALUES (?,?,?,?,?,?)",
                        agent_id, task, mission, p.pid, str(log), now())
        if task is not None:
            self.st.set_task(task, pid=p.pid)
        self.procs[p.pid] = (p, sid, task, mission)
        self.agent_of[p.pid] = agent_id
        print(f"{now()} launched {agent_id} via {harness} pid {p.pid} log {log}")
        return p

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
            self.agent_of.pop(pid, None)
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
            finished = self.st.one("""SELECT COUNT(*) n FROM tasks WHERE mission=? AND status IN ('done','failed')
                                      AND updated > ?""", slug, last_seen or "")["n"]
            if not state:
                out.append((slug, "first wake-up: plan the mission"))
            elif is_due(m["wake_at"]):
                out.append((slug, f"scheduled wake-up: {m['wake_reason'] or 'no reason given'}"))
            elif mail:
                out.append((slug, f"{mail} message(s) in your inbox"))
            elif finished:  # an experiment finished: resume work (the spec's event-driven wake-up)
                out.append((slug, f"{finished} task(s) finished since you last looked: judge the results"))
        return out

    # -------------------------------------------------------------- Overall, retrospectives, notifications
    def _running(self, agent_id: str) -> bool:
        return agent_id in self.agent_of.values()

    def _recent_session(self, agent: str, minutes: float) -> bool:
        row = self.st.one("SELECT started FROM sessions WHERE agent=? ORDER BY id DESC LIMIT 1", agent)
        if not row:
            return False
        return (datetime.now(timezone.utc) - datetime.fromisoformat(row["started"])).total_seconds() < minutes * 60

    def overall_reason(self) -> Optional[str]:
        cfg = self.st.config
        if not cfg.get("overall_agent", True) or self._running("overall"):
            return None
        if self._recent_session("overall", float(cfg.get("overall_min_gap_minutes", 60))):
            return None
        state = self.st.one("SELECT last_seen FROM agent_state WHERE agent='overall'")
        last = state["last_seen"] if state and state["last_seen"] else ""
        why = []
        mail = self.st.one("""SELECT COUNT(*) n FROM messages WHERE recipient='overall' AND
                              (read_at IS NULL OR (requires_response=1 AND response IS NULL))""")["n"]
        if mail:
            why.append(f"{mail} message(s) for you")
        failed = self.st.one("SELECT COUNT(*) n FROM tasks WHERE status='failed' AND updated > ?", last)["n"]
        if failed:
            why.append(f"{failed} task(s) failed")
        waiting = self.st.one("""SELECT COUNT(*) n FROM approvals WHERE status='pending' AND created < ?""",
                              (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat())["n"]
        if waiting:
            why.append(f"{waiting} approval(s) waiting over 2h for the human")
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=float(cfg.get("overall_stall_hours", 6)))).replace(microsecond=0).isoformat()
        for m in self.st.q("SELECT slug, updated, wake_at FROM missions WHERE status='active' AND updated < ?", cutoff):
            busy = self.st.one("SELECT COUNT(*) n FROM tasks WHERE mission=? AND status IN ('queued','running','sleeping','awaiting_approval')", m["slug"])["n"]
            woken = self.st.one("SELECT 1 FROM agent_state WHERE agent=?", f"mission:{m['slug']}")
            if not busy and not m["wake_at"] and woken:
                why.append(f"mission '{m['slug']}' looks stalled (idle since {m['updated'][:16]})")
        return "; ".join(why) or None

    def retro_slugs(self) -> list[str]:
        if not self.st.config.get("auto_retro", True):
            return []
        out = []
        for slug in [m["slug"] for m in retros_pending(self.st)]:
            tries = self.st.one("SELECT COUNT(*) n FROM sessions WHERE agent=?", f"retro:{slug}")["n"]
            if tries < 2 and not self._running(f"retro:{slug}"):
                out.append(slug)
        return out

    def notify_humans(self) -> None:
        cmd = self.st.config.get("notify_cmd")
        if not cmd or self.dry:
            return
        state = self.st.one("SELECT last_board_id FROM agent_state WHERE agent='notify'")
        last = state["last_board_id"] if state else 0
        for m in self.st.q("SELECT * FROM messages WHERE recipient='human' AND id>? ORDER BY id", last):
            subprocess.run(["bash", "-c", cmd], env=dict(os.environ, HYRA_NOTIFY_TEXT=f"{m['subject']}: {m['body']}"),
                           capture_output=True, timeout=30)
            self.st.x("INSERT INTO agent_state(agent,last_board_id,last_seen) VALUES ('notify',?,?) "
                      "ON CONFLICT(agent) DO UPDATE SET last_board_id=excluded.last_board_id, last_seen=excluded.last_seen", m["id"], now())

    def run_overall(self, reason: str = "run on demand") -> None:
        self._launch("overall", "overseer", OVERALL_PROMPT.format(root=self.st.root, reason=reason), None, None, None, None)
        while self.procs and not self.dry:
            time.sleep(2)
            self.reap()

    def tick(self) -> None:
        self.reap()
        for w in core_check(self.st):
            print(f"{now()} wake condition met for {w}")
        cap = int(self.st.config.get("max_missions_running", 2))
        live_missions = sum(1 for v in self.procs.values() if v[3] and v[2] is None)
        for slug, reason in self.mission_reasons():
            if live_missions >= cap:
                break
            if not self.dry:
                self.st.x("UPDATE missions SET wake_at=NULL, wake_cmd=NULL WHERE slug=?", slug)
            self._launch(f"mission:{slug}", "mission",
                         MISSION_PROMPT.format(slug=slug, root=self.st.root, reason=reason), None, None, None, slug)
            live_missions += 1
        self.notify_humans()
        reason = self.overall_reason()
        if reason:
            self._launch("overall", "overseer", OVERALL_PROMPT.format(root=self.st.root, reason=reason), None, None, None, None)
        for slug in self.retro_slugs():
            skill = skill_dir().parent / "hyra-retro" / "SKILL.md"
            self._launch(f"retro:{slug}", "retrospective", RETRO_PROMPT.format(slug=slug, root=self.st.root, skill=skill),
                         None, None, None, None)
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

    def run_mission(self, slug: str, reason: str = "run on demand") -> None:
        """Run one Mission in its own fresh top-level headless session and wait for it (so Overall and Mission are
        separate agents even when Overall is an interactive session that can't spawn nested agents)."""
        self.st.mission(slug)
        self._launch(f"mission:{slug}", "mission", MISSION_PROMPT.format(slug=slug, root=self.st.root, reason=reason),
                     None, None, None, slug)
        while self.procs and not self.dry:
            time.sleep(2)
            self.reap()
        print(f"mission {slug} session ended")

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
