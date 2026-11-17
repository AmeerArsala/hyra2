"""Missions, task queue, Research Board, messages, approvals, reviews, memory, briefings."""
from __future__ import annotations

import hashlib
import re
import sys
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from . import remote
from .store import HyraError, Store, is_due, memory_template, now

TASK_KINDS = ["explore", "experiment", "analysis", "build", "review", "other"]
BOARD_TYPES = ["idea", "finding", "evidence", "decision", "note", "review", "retro"]
VERDICTS = ["supported", "narrower", "refuted", "inconclusive"]
WORKER_ROLES = {"worker", "reviewer", "mission", "context", "proposer", "evaluator", "overseer", "retrospective"}


def me() -> str:
    return os.environ.get("HYRA_AGENT", "overall")


def role() -> str:
    return os.environ.get("HYRA_ROLE", "overall")


def short(s: Optional[str], n: int = 100) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- missions
def mission_new(st: Store, slug: str, title: str, problem: str, priority: int) -> None:
    if st.one("SELECT 1 FROM missions WHERE slug=?", slug):
        raise HyraError(f"mission {slug!r} exists")
    st.x("INSERT INTO missions(slug,title,problem,priority,created,updated) VALUES (?,?,?,?,?,?)",
         slug, title, problem, priority, now(), now())
    mem = st.memory_path(f"mission:{slug}")
    if not mem.exists():
        mem.write_text(memory_template(f"mission:{slug}"))
    st.post(me(), "decision", f"Opened mission '{slug}': {title}", mission=slug)
    print(f"mission {slug} created (priority {priority}). memory: {mem}")


def mission_list(st: Store, show_all: bool) -> None:
    rows = st.q("SELECT * FROM missions " + ("" if show_all else "WHERE status!='closed' ") +
                "ORDER BY priority DESC, created")
    if not rows:
        print("(no missions)")
    for m in rows:
        counts = {r["status"]: r["n"] for r in st.q(
            "SELECT status, COUNT(*) n FROM tasks WHERE mission=? GROUP BY status", m["slug"])}
        c = " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no tasks"
        wake = f" wake@{m['wake_at']}" if m["wake_at"] else ""
        print(f"[{m['status']}] {m['slug']} p{m['priority']}: {m['title']}  ({c}){wake}")


def mission_show(st: Store, slug: str) -> None:
    m = st.mission(slug)
    print(f"# Mission {slug} [{m['status']}] priority {m['priority']}\n{m['title']}\n")
    print(f"## Problem\n{m['problem']}\n")
    if m["conclusion"]:
        print(f"## Conclusion\n{m['conclusion']}\n")
    print("## Tasks")
    for t in st.q("SELECT * FROM tasks WHERE mission=? ORDER BY id", slug):
        rv = f" review={t['review_verdict']}" if t["review_verdict"] else ""
        print(f"  T{t['id']} [{t['status']}] {t['kind']}: {short(t['title'], 80)}{rv}")
    print("\n## Recent board")
    hide_sql, hide_args = board_visible(st)
    for b in st.q("SELECT * FROM board WHERE mission=?" + hide_sql + " ORDER BY id DESC LIMIT 15", slug, *hide_args)[::-1]:
        print(f"  #{b['id']} {b['type']} ({b['author']}): {short(b['text'], 140)}")


def mission_set(st: Store, slug: str, status: Optional[str], priority: Optional[int]) -> None:
    st.mission(slug)
    if status:
        st.x("UPDATE missions SET status=?, updated=? WHERE slug=?", status, now(), slug)
    if priority is not None:
        st.x("UPDATE missions SET priority=?, updated=? WHERE slug=?", priority, now(), slug)
    st.post(me(), "decision", f"Mission {slug}: status={status or '-'} priority={priority if priority is not None else '-'}", mission=slug)
    print("ok")


def mission_wake(st: Store, slug: str, when: Optional[str], reason: str, until_file: Optional[str] = None,
                until_cmd: Optional[str] = None) -> None:
    from .store import parse_when
    st.mission(slug)
    cond = wake_condition(until_file, until_cmd)
    if not when and not cond:
        raise HyraError("give --at <time> and/or --until-file PATH / --until-cmd CMD")
    at = parse_when(when or "+24h")  # the time is the fallback deadline when a condition is given
    st.x("UPDATE missions SET wake_at=?, wake_reason=?, wake_cmd=?, updated=? WHERE slug=?", at, reason, cond, now(), slug)
    print(f"mission {slug} will wake at {at}" + (f" or when `{cond}` succeeds" if cond else "") + f": {reason}")


def mission_close(st: Store, slug: str, conclusion: str, accept_unreviewed: bool) -> None:
    _maybe_backup(st)
    st.mission(slug)
    unreviewed = st.q("""SELECT id, title FROM tasks WHERE mission=? AND status='done'
                         AND kind IN ('experiment','analysis','build') AND review_verdict IS NULL""", slug)
    open_ = st.q("SELECT id FROM tasks WHERE mission=? AND status IN ('queued','running','sleeping','awaiting_approval')", slug)
    if open_:
        raise HyraError(f"mission has open tasks: {', '.join('T%d' % r['id'] for r in open_)} (cancel or finish them)")
    if unreviewed and not accept_unreviewed:
        lst = ", ".join(f"T{r['id']}" for r in unreviewed)
        raise HyraError(f"conclusion rests on unreviewed results ({lst}). Run `hyra review request <task>` "
                        "for the load-bearing ones, or pass --accept-unreviewed and say so in the conclusion.")
    if unreviewed:
        conclusion += "\n\n[NOTE: closed with unreviewed results: " + ", ".join(f"T{r['id']}" for r in unreviewed) + "]"
    loose = [i for i, s_ in idea_states(st, slug) if s_ != "handled"]
    if loose:
        print(f"note: {len(loose)} idea(s) on this mission's board were never tested or resolved "
              f"({', '.join('#%d' % i['id'] for i in loose)}). Record why with a decision `--ref board:<id>`, or carry them "
              "to a new mission.")
    st.x("UPDATE missions SET status='closed', conclusion=?, updated=? WHERE slug=?", conclusion, now(), slug)
    st.post(me(), "decision", f"Closed mission {slug}. Conclusion: {conclusion}", mission=slug)
    print(f"mission {slug} closed. A retrospective is now pending: run the hyra-retro skill, then post "
          f"`hyra board post --type retro --mission {slug} \"...\"`.")


# ---------------------------------------------------------------- tasks
def needs_approval(st: Store, resources: dict) -> list[str]:
    rules = st.config.get("approval_rules", {})
    return [f"{k}={v}" for k, v in resources.items() if k in rules and float(v) >= float(rules[k])]


def task_add(st: Store, mission: str, kind: str, title: str, spec: str, priority: int,
             deps: list[int], resources: dict, model: Optional[str], force_approval: bool,
             review_of: Optional[int] = None, harness: Optional[str] = None) -> int:
    m = st.mission(mission)
    if m["status"] == "closed":
        raise HyraError(f"mission {mission} is closed")
    for d in deps:
        st.task(d)
    if harness:
        st.harness_cmd(harness)  # validate
    gated = needs_approval(st, resources)
    status = "awaiting_approval" if (gated or force_approval) else "queued"
    tid = st.x("""INSERT INTO tasks(mission,kind,title,spec,status,priority,deps,resources,model,harness,review_of,created,updated)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               mission, kind, title, spec, status, priority, json.dumps(deps), json.dumps(resources),
               model, harness, review_of, now(), now())
    if status == "awaiting_approval":
        what = f"Run T{tid} '{title}' with resources {json.dumps(resources)}"
        rid = None
        if remote.enabled(st):
            try:
                rid = remote.approval_create(st, "compute", what, tid, None)
            except HyraError:
                st.set_task(tid, status="cancelled", summary="could not register the approval on the ledger server")
                raise
        aid = st.x("INSERT INTO approvals(kind,what,task,requested_by,created,remote_id) VALUES (?,?,?,?,?,?)",
                   "compute", what, tid, me(), now(), rid)
        print(f"T{tid} queued pending approval A{aid} ({', '.join(gated) or 'requested'})")
    else:
        print(f"T{tid} queued")
    return tid


def usage(st: Store) -> dict[str, float]:
    used: dict[str, float] = {"workers": 0}
    for t in st.q("SELECT resources FROM tasks WHERE status='running'"):
        used["workers"] += 1
        for k, v in json.loads(t["resources"] or "{}").items():
            used[k] = used.get(k, 0) + float(v)
    return used


def fits(st: Store, resources: dict, used: dict) -> bool:
    cap = st.config.get("resources", {})
    if used.get("workers", 0) + 1 > cap.get("workers", 1e9):
        return False
    return all(used.get(k, 0) + float(v) <= float(cap.get(k, 0)) for k, v in resources.items())


def ready(st: Store, kinds: Optional[list[str]] = None) -> list[Any]:
    rows = st.q("""SELECT t.* FROM tasks t JOIN missions m ON m.slug=t.mission
                   WHERE m.status='active' AND (t.status='queued' OR t.status='sleeping')
                   ORDER BY m.priority DESC, t.priority DESC, t.id""")
    out = []
    for t in rows:
        if t["status"] == "sleeping" and not is_due(t["wake_at"]):
            continue
        if kinds and t["kind"] not in kinds:
            continue
        deps = json.loads(t["deps"] or "[]")
        if deps and st.one(f"SELECT COUNT(*) n FROM tasks WHERE id IN ({','.join('?' * len(deps))}) AND status!='done'", *deps)["n"]:
            continue
        out.append(t)
    return out


def verify_gated_tasks(st: Store) -> None:
    """Server mode: a task that needed compute approval only runs if the ledger server says it was granted. The local
    database can't release gated work by itself (an agent editing it would otherwise spend your compute)."""
    sync_approvals(st)
    for a in st.q("SELECT * FROM approvals WHERE kind='compute' AND task IS NOT NULL AND remote_id IS NOT NULL"):
        t = st.one("SELECT status FROM tasks WHERE id=?", a["task"])
        if t and t["status"] == "queued" and remote.approval_get(st, a["remote_id"])["status"] != "granted":
            st.set_task(a["task"], status="awaiting_approval")
            st.note(a["task"], "scheduler", "held: the ledger server has not granted this approval")


def task_claim(st: Store, worker: str, tid: Optional[int] = None, kinds: Optional[list[str]] = None) -> Optional[int]:
    if remote.enabled(st):
        verify_gated_tasks(st)
    check_wake_conditions(st)
    with st.tx():
        used = usage(st)
        cands = ready(st, kinds)
        if tid is not None:
            cands = [t for t in cands if t["id"] == tid]
            if not cands:
                t = st.task(tid)
                raise HyraError(f"T{tid} is not claimable (status={t['status']}, deps or mission not ready)")
        for t in cands:
            if fits(st, json.loads(t["resources"] or "{}"), used):
                resumed = t["status"] == "sleeping"
                st.set_task(t["id"], status="running", worker=worker, claimed_at=now(),
                            attempts=t["attempts"] + 1, wake_at=None)
                st.note(t["id"], worker, "resumed after wake-up" if resumed else "claimed")
                return t["id"]
    return None


def task_list(st: Store, status: Optional[str], mission: Optional[str]) -> None:
    sql, args = "SELECT * FROM tasks WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(status)
    else:
        sql += " AND status NOT IN ('done','failed','cancelled')"
    if mission:
        sql += " AND mission=?"; args.append(mission)
    rows = st.q(sql + " ORDER BY priority DESC, id", *args)
    if not rows:
        print("(no tasks)")
    for t in rows:
        extra = ""
        if t["status"] == "sleeping":
            extra = f" wake@{t['wake_at']} ({short(t['wake_reason'], 40)})"
        if t["worker"] and t["status"] == "running":
            extra = f" by {t['worker']}"
        deps = json.loads(t["deps"] or "[]")
        dep = f" after {','.join('T%d' % d for d in deps)}" if deps else ""
        print(f"T{t['id']} [{t['status']}] p{t['priority']} {t['mission']}/{t['kind']}: {short(t['title'], 70)}{dep}{extra}")


def task_show(st: Store, tid: int) -> None:
    t = st.task(tid)
    rv = st.one("SELECT * FROM reviews WHERE review_task=?", tid)
    print(f"# T{tid} [{t['status']}] {t['kind']} in mission '{t['mission']}' (priority {t['priority']})")
    print(f"title: {t['title']}")
    for k in ["harness", "model", "worker", "claimed_at", "wake_at", "wake_reason", "attempts"]:
        if t[k]:
            print(f"{k}: {t[k]}")
    if t["deps"] != "[]":
        print(f"deps: {t['deps']}")
    if t["resources"] != "{}":
        print(f"resources: {t['resources']}")
    print(f"\n## Spec\n{t['spec']}\n")
    m = st.mission(t["mission"])
    print(f"## Mission problem ({m['slug']})\n{short(m['problem'], 1500)}\n")
    if rv:
        print("## Review protocol\nThis is a BLIND review. Use `hyra review evidence {0}` to see the original problem and evidence.\n"
              "Form and record your own conclusion with `hyra review blind {0} --notes ...` BEFORE `hyra review claim {0}`.\n"
              "Current phase: {1}\n".format(tid, rv["phase"]))
    notes = st.q("SELECT * FROM task_notes WHERE task=? ORDER BY id", tid)
    if notes:
        print("## Notes")
        for n in notes:
            print(f"  [{n['created']}] {n['author']}: {n['text']}")
    if t["status"] in ("done", "failed") and t["summary"] and not _hide_summary_from(st, tid):
        print(f"\n## Summary\n{claim_text(st, t)}")
        _print_evidence(st, tid)
    if t["review_verdict"]:
        print(f"\n## Review verdict: {t['review_verdict']}")
        for r in st.q("SELECT * FROM reviews WHERE target_task=? AND verdict IS NOT NULL", tid):
            print(f"  (T{r['review_task']}) {r['verdict']}: {r['notes']}")


def blinded_target(st: Store) -> Optional[int]:
    """If the current agent is a reviewer still in the blind phase, the task whose claim it must not see yet."""
    cur = os.environ.get("HYRA_TASK")
    if not cur or not cur.isdigit():
        return None
    rv = st.one("SELECT target_task, phase FROM reviews WHERE review_task=?", int(cur))
    return rv["target_task"] if rv and rv["phase"] == "blind" else None


def claim_text(st: Store, t: Any) -> str:
    """A task's claim. In server mode it is sealed on the ledger and fetched with this session's token."""
    return remote.fetch_claim(st, t["id"]) if remote.enabled(st) and t["summary"] == remote.SEALED else (t["summary"] or "")


def _hide_summary_from(st: Store, target: int) -> bool:
    """A reviewer in blind phase for this target must not see the claim via task show."""
    return blinded_target(st) == target


def board_visible(st: Store) -> tuple[str, list]:
    """SQL fragment + args hiding the reviewed task's own Board entries (its finding repeats the claim)
    from a blind reviewer. Everything else on the Board stays visible."""
    t = blinded_target(st)
    return ("", []) if t is None else (" AND (task IS NULL OR task != ?) AND author != ?", [t, f"task:{t}"])


def _print_evidence(st: Store, tid: int) -> None:
    ev = st.q("SELECT * FROM evidence WHERE task=? ORDER BY id", tid)
    if ev:
        print("\n## Evidence")
        for e in ev:
            stored = f" (copy: {e['stored_path']})" if e["stored_path"] else ""
            sha = f" sha256:{e['sha256'][:12]}" if e["sha256"] else ""
            print(f"  E{e['id']} {e['kind']}: {e['ref']}{sha}{stored}" + (f" — {e['note']}" if e["note"] else ""))


def task_note(st: Store, tid: int, text: str) -> None:
    st.task(tid)
    st.note(tid, me(), text)
    print("ok")


def wake_condition(until_file: Optional[str], until_cmd: Optional[str]) -> Optional[str]:
    """A shell condition that is true when the thing being waited for has happened."""
    if until_file and until_cmd:
        raise HyraError("use --until-file or --until-cmd, not both")
    if until_file:
        import shlex
        return f"test -e {shlex.quote(str(Path(until_file).expanduser()))}"
    return until_cmd


def task_sleep(st: Store, tid: int, until: Optional[str], reason: str, until_file: Optional[str] = None,
               until_cmd: Optional[str] = None) -> None:
    """Park a task until a time and/or a condition (event-driven resume, e.g. a training job's output file appears
    or `squeue -j 123 | grep -q .` stops being true). The time is the fallback deadline (default 24h)."""
    from .store import parse_when
    t = st.task(tid)
    if t["status"] not in ("running", "queued"):
        raise HyraError(f"T{tid} is {t['status']}")
    cond = wake_condition(until_file, until_cmd)
    if not until and not cond:
        raise HyraError("give --until <time> and/or --until-file PATH / --until-cmd CMD")
    when = parse_when(until or "+24h")
    st.set_task(tid, status="sleeping", wake_at=when, wake_reason=reason, worker=None, wake_cmd=cond)
    st.note(tid, me(), f"sleeping until {when}" + (f" or until `{cond}` succeeds" if cond else "") + f": {reason}")
    print(f"T{tid} sleeping until {when}" + (f" or until `{cond}` succeeds" if cond else "") +
          ". Record what to check on wake-up in a note/memory before exiting.")


def check_wake_conditions(st: Store) -> list[str]:
    """Evaluate wake conditions of sleeping tasks and waiting missions; when one holds, make it due now.
    Runs outside any DB transaction (a condition may take a few seconds)."""
    woke = []
    rows = [("task", r["id"], r["wake_cmd"]) for r in st.q("SELECT id, wake_cmd FROM tasks WHERE status='sleeping' AND wake_cmd IS NOT NULL")]
    rows += [("mission", r["slug"], r["wake_cmd"]) for r in st.q("SELECT slug, wake_cmd FROM missions WHERE status='active' AND wake_cmd IS NOT NULL")]
    for kind, key, cmd in rows:
        try:
            ok = subprocess.run(["bash", "-c", cmd], cwd=st.root, capture_output=True, timeout=20).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            ok = False
        if not ok:
            continue
        if kind == "task":
            st.set_task(key, wake_at=now(), wake_cmd=None)
            st.note(key, "scheduler", f"wake condition met: {cmd}")
        else:
            st.x("UPDATE missions SET wake_at=?, wake_cmd=NULL, updated=? WHERE slug=?", now(), now(), key)
        woke.append(f"{kind} {key}")
    return woke


def _git_head(cwd: Path) -> Optional[str]:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=10)
        if head.returncode != 0:
            return None
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=10)
        return head.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return None


def attach_evidence(st: Store, tid: int, refs: list[str], note: Optional[str] = None) -> list[int]:
    """Evidence refs: a file/dir path, 'commit:<rev>', 'url:<url>', 'task:<id>', 'board:<id>', 'eb:<name>/<sol>'."""
    ids = []
    for ref in refs:
        kind, value, sha, stored = "file", ref, None, None
        if ref.startswith("commit:"):
            kind, value = "commit", ref[7:]
            res = subprocess.run(["git", "rev-parse", "--verify", value + "^{commit}"], capture_output=True, text=True)
            if res.returncode != 0:
                raise HyraError(f"evidence {ref}: not a git commit here")
            value = res.stdout.strip()
        elif ref.split(":", 1)[0] in ("url", "task", "board", "eb", "note"):
            kind, value = ref.split(":", 1)
        else:
            p = Path(ref).expanduser()
            if not p.exists():
                raise HyraError(f"evidence path does not exist: {ref}")
            p = p.resolve()
            value = str(p)
            if p.is_file():
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                sha = h.hexdigest()
                if p.stat().st_size <= st.config.get("evidence_copy_max_bytes", 2_000_000):
                    dest = st.dir / "evidence" / str(tid) / f"{sha[:8]}_{p.name}"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dest)
                    stored = str(dest)
            else:
                kind = "dir"
        ids.append(st.x("INSERT INTO evidence(task,kind,ref,sha256,stored_path,note,created) VALUES (?,?,?,?,?,?,?)",
                        tid, kind, value, sha, stored, note, now()))
    return ids


def task_complete(st: Store, tid: int, summary: str, evidence: list[str], allow_no_evidence: bool) -> None:
    t = st.task(tid)
    if t["status"] not in ("running", "sleeping", "queued"):
        raise HyraError(f"T{tid} is {t['status']}")
    if t["kind"] == "review":
        raise HyraError("review tasks complete via `hyra review verdict`")
    if allow_no_evidence and t["kind"] in ("experiment", "analysis", "build"):
        raise HyraError(f"--no-evidence isn't allowed for {t['kind']} tasks: every conclusion must be backed by code, "
                        "results, logs or traces. (It exists for explore/other planning and note tasks.)")
    if not evidence and not allow_no_evidence:
        raise HyraError("completion requires evidence: --evidence <path|commit:REV|url:...|eb:...> "
                        "(code versions, results, logs, or raw traces). Use --no-evidence only for pure "
                        "planning/notes tasks, and say why in the summary.")
    attach_evidence(st, tid, evidence)
    head = _git_head(Path.cwd())
    if head:
        st.x("INSERT INTO evidence(task,kind,ref,note,created) VALUES (?,?,?,?,?)", tid, "git-head", head,
             f"code version in {Path.cwd()}", now())
    finding_text = f"T{tid} done: {summary}"
    if remote.enabled(st):  # seal the claim on the ledger server first; fail closed if that isn't possible
        remote.seal_claim(st, tid, summary, finding_text)
        summary = remote.SEALED
        finding_text = f"T{tid} done (claim sealed on the ledger server; read it with `hyra task show {tid}`)"
    st.set_task(tid, status="done", summary=summary)
    tested = [f"board:{n}" for n in dict.fromkeys(IDEA_REF.findall(t["spec"] or ""))]  # ideas this task tested
    st.post(me(), "finding" if t["kind"] != "explore" else "idea", finding_text,
            mission=t["mission"], task=tid, refs=evidence + tested)
    print(f"T{tid} done with {len(evidence)} evidence item(s).")


def task_fail(st: Store, tid: int, reason: str, requeue: bool) -> None:
    t = st.task(tid)
    st.set_task(tid, status="queued" if requeue else "failed", summary=reason, worker=None)
    st.note(tid, me(), f"{'requeued' if requeue else 'failed'}: {reason}")
    st.post(me(), "finding", f"T{tid} {'requeued' if requeue else 'FAILED'}: {reason}", mission=t["mission"], task=tid)
    print("ok")


def task_cancel(st: Store, tid: int, reason: str) -> None:
    t = st.task(tid)
    st.set_task(tid, status="cancelled", summary=reason, worker=None)
    st.post(me(), "decision", f"Cancelled T{tid}: {reason}", mission=t["mission"], task=tid)
    print("ok")


def task_release(st: Store, tid: int) -> None:
    st.set_task(tid, status="queued", worker=None)
    st.note(tid, me(), "released back to queue")
    print("ok")


# ---------------------------------------------------------------- board
IDEA_REF = re.compile(r"board:(\d+)")
HANDLED_TYPES = ("finding", "review", "decision", "evidence", "retro")


def idea_states(st: Store, mission: Optional[str] = None) -> list[tuple[Any, str]]:
    """Ideas live on the Research Board (type 'idea'). Their state is derived, not stored separately:
      handled  — a finding/review/decision/evidence/retro entry references it (`--ref board:<id>`);
      testing  — not handled, but an unfinished task's spec mentions `board:<id>`;
      untested — neither. Returns [(idea row, state)] oldest first."""
    sql, args = "SELECT * FROM board WHERE type='idea'", []
    if mission:
        sql += " AND mission=?"; args.append(mission)
    ideas = st.q(sql + " ORDER BY id", *args)
    if not ideas:
        return []
    handled: set[int] = set()
    for b in st.q(f"SELECT refs FROM board WHERE type IN ({','.join('?' * len(HANDLED_TYPES))})", *HANDLED_TYPES):
        handled.update(int(m) for r in json.loads(b["refs"] or "[]") for m in IDEA_REF.findall(r))
    testing: set[int] = set()
    for t in st.q("SELECT spec FROM tasks WHERE status IN ('queued','running','sleeping','awaiting_approval')"):
        testing.update(int(m) for m in IDEA_REF.findall(t["spec"] or ""))
    return [(i, "handled" if i["id"] in handled else "testing" if i["id"] in testing else "untested") for i in ideas]


def retros_pending(st: Store) -> list[Any]:
    """Closed missions nobody has run a retrospective for yet (a `retro` Board entry on the mission marks it done).
    This is how the 'improve the tools, not just the answers' loop stays honest: closing a mission isn't the end."""
    return [m for m in st.q("SELECT slug, title FROM missions WHERE status='closed' ORDER BY updated")
            if not st.one("SELECT 1 FROM board WHERE type='retro' AND mission=?", m["slug"])]


def print_pending_retros(st: Store) -> None:
    pend = retros_pending(st)
    if pend:
        print("\n## Retrospectives pending (distill lessons into Books and skills with the hyra-retro skill)")
        for m in pend:
            print(f"  {m['slug']}: {m['title']}   (done = `hyra board post --type retro --mission {m['slug']} \"...\"`)")


def print_open_ideas(st: Store, mission: Optional[str], header: str = "Ideas on the Research Board") -> None:
    open_ = [(i, s) for i, s in idea_states(st, mission) if s != "handled"]
    if not open_:
        return
    print(f"\n## {header} (not yet resolved)")
    for i, state in open_[:12]:
        print(f"  #{i['id']} [{state}] ({i['mission'] or 'no mission'}) {short(i['text'], 150)}")
    if len(open_) > 12:
        print(f"  ... and {len(open_) - 12} more (`hyra board ideas`)")
    print("  Test one: put `board:<id>` in a task spec. Resolve one: post a finding or decision with `--ref board:<id>`.")


def board_post(st: Store, type_: str, text: str, mission: Optional[str], task: Optional[int], refs: list[str]) -> None:
    if mission:
        st.mission(mission)
    for r in refs:
        for bid in IDEA_REF.findall(r):
            if not st.one("SELECT 1 FROM board WHERE id=?", int(bid)):
                raise HyraError(f"--ref {r}: no board entry #{bid}")
    bid = st.post(me(), type_, text, mission=mission, task=task, refs=refs)
    print(f"posted #{bid}")


def board_read(st: Store, mission: Optional[str], since: int, types: Optional[list[str]], limit: int) -> None:
    sql, args = "SELECT * FROM board WHERE id>?", [since]
    if mission:
        sql += " AND (mission=? OR mission IS NULL)"; args.append(mission)
    if types:
        sql += f" AND type IN ({','.join('?' * len(types))})"; args += types
    hide_sql, hide_args = board_visible(st)
    rows = st.q(sql + hide_sql + " ORDER BY id DESC LIMIT ?", *args, *hide_args, limit)[::-1]
    for b in rows:
        refs = json.loads(b["refs"] or "[]")
        r = f"  refs: {', '.join(refs)}" if refs else ""
        where = f"{b['mission'] or '*'}" + (f"/T{b['task']}" if b["task"] else "")
        print(f"#{b['id']} {b['created'][:16]} {b['type']} [{where}] {b['author']}: {b['text']}{r}")
    if not rows:
        print("(no board entries)")


# ---------------------------------------------------------------- messages
def msg_send(st: Store, to: str, subject: str, body: str, requires_response: bool) -> None:
    mid = st.x("INSERT INTO messages(sender,recipient,subject,body,requires_response,created) VALUES (?,?,?,?,?,?)",
               me(), to, subject, body, int(requires_response), now())
    print(f"message M{mid} sent to {to}" + (" (response required)" if requires_response else ""))


def _inbox_rows(st: Store, agent: str, include_read: bool) -> list[Any]:
    targets = [agent]
    if agent.startswith("task:"):
        t = st.one("SELECT mission FROM tasks WHERE id=?", int(agent[5:]))
        if t:
            targets.append(f"mission-workers:{t['mission']}")
    cond = "" if include_read else " AND (read_at IS NULL OR (requires_response=1 AND response IS NULL))"
    return st.q(f"SELECT * FROM messages WHERE recipient IN ({','.join('?' * len(targets))}){cond} ORDER BY id",
                *targets)


def msg_inbox(st: Store, agent: str, include_read: bool) -> None:
    rows = _inbox_rows(st, agent, include_read)
    if not rows:
        print("(inbox empty)")
    for m in rows:
        flag = " [RESPONSE REQUIRED]" if m["requires_response"] and not m["response"] else ""
        print(f"M{m['id']} from {m['sender']} @ {m['created'][:16]}: {m['subject']}{flag}\n    {m['body']}")
        if m["response"]:
            print(f"    -> response: {m['response']}")
        if not m["read_at"]:
            st.x("UPDATE messages SET read_at=? WHERE id=?", now(), m["id"])


def msg_reply(st: Store, mid: int, body: str) -> None:
    m = st.one("SELECT * FROM messages WHERE id=?", mid)
    if not m:
        raise HyraError(f"no message M{mid}")
    st.x("UPDATE messages SET response=?, responded_at=?, read_at=COALESCE(read_at, ?) WHERE id=?", body, now(), now(), mid)
    st.x("INSERT INTO messages(sender,recipient,subject,body,created) VALUES (?,?,?,?,?)",
         me(), m["sender"], f"Re: {m['subject']}", body, now())
    print("ok")


# ---------------------------------------------------------------- approvals
def approval_request(st: Store, kind: str, what: str, task: Optional[int], ref: Optional[str]) -> int:
    rid = remote.approval_create(st, kind, what, task, ref) if remote.enabled(st) else None
    aid = st.x("INSERT INTO approvals(kind,what,task,ref,requested_by,created,remote_id) VALUES (?,?,?,?,?,?,?)",
               kind, what, task, ref, me(), now(), rid)
    st.x("INSERT INTO messages(sender,recipient,subject,body,requires_response,created) VALUES (?,?,?,?,?,?)",
         me(), "overall", f"Approval A{aid} needed ({kind})", what, 0, now())
    print(f"approval A{aid} requested; a human must grant it (`hyra approval grant {aid}`)")
    return aid


def sync_approvals(st: Store) -> None:
    """Server mode: the ledger is the authority. Apply decisions made there (e.g. from another device) to the local mirror."""
    if not remote.enabled(st):
        return
    try:
        for a in st.q("SELECT * FROM approvals WHERE status='pending' AND remote_id IS NOT NULL"):
            r = remote.approval_get(st, a["remote_id"])
            if r["status"] != "pending":
                _apply_decision(st, a, r["status"], r.get("decided_by") or "human", r.get("note") or "")
    except HyraError as e:  # listing must still work without a token or while the server is away (shows the local mirror)
        print(f"warning: approvals not synced with the ledger ({e})", file=sys.stderr)


def approval_list(st: Store, show_all: bool) -> None:
    sync_approvals(st)
    rows = st.q("SELECT * FROM approvals " + ("" if show_all else "WHERE status='pending' ") + "ORDER BY id")
    if not rows:
        print("(no pending approvals)")
    for a in rows:
        t = f" T{a['task']}" if a["task"] else ""
        d = f" by {a['decided_by']}" if a["decided_by"] else ""
        print(f"A{a['id']} [{a['status']}{d}] {a['kind']}{t}: {a['what']} (requested by {a['requested_by']})")


def approval_decide(st: Store, aid: int, grant: bool, by: str, note: str) -> None:
    _maybe_backup(st)
    if role() in WORKER_ROLES:
        raise HyraError("agents cannot decide approvals; only the human (via Overall) can")
    if st.config.get("approvals_require_tty") and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise HyraError("approvals_require_tty is on: run this in a real terminal yourself (an agent's shell tool isn't one)")
    a = st.one("SELECT * FROM approvals WHERE id=?", aid)
    if not a:
        raise HyraError(f"no approval A{aid}")
    if a["status"] != "pending":
        raise HyraError(f"A{aid} already {a['status']}")
    if remote.enabled(st):  # the decision is made on the ledger, with the human's token, not in the local file
        if a["remote_id"] is None:
            raise HyraError(f"A{aid} was never registered on the ledger server")
        remote.approval_decide(st, a["remote_id"], grant, by, note)
    _apply_decision(st, a, "granted" if grant else "denied", by, note)
    print(f"A{aid} {'granted' if grant else 'denied'}")


def _apply_decision(st: Store, a: Any, status: str, by: str, note: str) -> None:
    aid = a["id"]
    st.x("UPDATE approvals SET status=?, decided_by=?, decided_at=?, note=? WHERE id=?", status, by, now(), note, aid)
    if a["task"] and a["kind"] == "compute":
        t = st.task(a["task"])
        if t["status"] == "awaiting_approval":
            st.set_task(a["task"], status="queued" if status == "granted" else "cancelled",
                        summary=None if status == "granted" else f"approval denied: {note}")
    st.post(by, "decision", f"Approval A{aid} {status}: {a['what']}" + (f" — {note}" if note else ""), task=a["task"])


def approval_granted(st: Store, kind: str, ref: str) -> bool:
    if remote.enabled(st):
        return remote.approval_granted(st, kind, ref)
    return bool(st.one("SELECT 1 FROM approvals WHERE kind=? AND ref=? AND status='granted'", kind, ref))


# ---------------------------------------------------------------- reviews
def review_request(st: Store, target: int, focus: str, model: Optional[str], priority: Optional[int],
                   harness: Optional[str] = None) -> None:
    t = st.task(target)
    if t["status"] != "done":
        raise HyraError(f"T{target} is {t['status']}; review completed work")
    spec = (f"Independently verify the result of T{target} ('{t['title']}').\n"
            f"You are deliberately NOT shown the original worker's conclusion first. Protocol:\n"
            f"  1. `hyra review evidence <this task>`: read the original problem and the raw evidence.\n"
            f"  2. Re-derive/re-run what you can. Record your own conclusion: `hyra review blind <this task> --notes ...`.\n"
            f"  3. Only then `hyra review claim <this task>` to see what the worker claimed.\n"
            f"  4. `hyra review verdict <this task> --verdict supported|narrower|refuted|inconclusive --notes ...`.\n"
            f"A verdict of 'narrower' means the evidence supports only a weaker claim: state that claim exactly.")
    if focus:
        spec += f"\n\nReview focus requested by {me()}: {focus}"
    rid = task_add(st, t["mission"], "review", f"Review T{target}: {t['title']}", spec,
                   priority if priority is not None else t["priority"] + 5, [], {}, model, False, review_of=target,
                   harness=harness)
    st.x("INSERT INTO reviews(review_task,target_task,created,updated) VALUES (?,?,?,?)", rid, target, now(), now())
    if remote.enabled(st):
        remote.register_review(st, rid, target)


def _review(st: Store, rid: int) -> Any:
    r = st.one("SELECT * FROM reviews WHERE review_task=?", rid)
    if not r:
        raise HyraError(f"T{rid} is not a review task")
    return r


def review_evidence(st: Store, rid: int) -> None:
    r = _review(st, rid)
    t = st.task(r["target_task"])
    m = st.mission(t["mission"])
    print(f"# Evidence for T{t['id']} (worker's claim withheld until you record a blind assessment)\n")
    print(f"## Mission problem\n{m['problem']}\n\n## Task spec given to the worker\n{t['spec']}")
    _print_evidence(st, t["id"])
    notes = st.q("SELECT * FROM task_notes WHERE task=? AND text NOT LIKE 'claimed%' ORDER BY id", t["id"])
    if notes:
        print("\n## Execution notes (actions log; may contain interpretation, weigh accordingly)")
        for n in notes:
            print(f"  [{n['created'][:16]}] {n['author']}: {n['text']}")


def review_blind(st: Store, rid: int, notes: str) -> None:
    r = _review(st, rid)
    if r["phase"] != "blind":
        raise HyraError("blind assessment already recorded")
    if remote.enabled(st):
        remote.review_blind(st, rid, notes)
    st.x("UPDATE reviews SET blind_notes=?, phase='revealed', updated=? WHERE review_task=?", notes, now(), rid)
    st.note(rid, me(), f"blind assessment: {notes}")
    print("blind assessment recorded; you may now run `hyra review claim`.")


def review_claim(st: Store, rid: int) -> None:
    r = _review(st, rid)
    if r["phase"] == "blind" and not remote.enabled(st):
        raise HyraError("record your independent assessment first: `hyra review blind <task> --notes ...`")
    t = st.task(r["target_task"])
    if remote.enabled(st):  # the server decides; it won't release the claim before the blind assessment
        print(f"# Worker's claim for T{t['id']}\n{remote.review_claim(st, rid)['summary']}\n\n# Your blind assessment\n{r['blind_notes']}")
        return
    print(f"# Worker's claim for T{t['id']}\n{t['summary']}\n\n# Your blind assessment\n{r['blind_notes']}")


def review_verdict(st: Store, rid: int, verdict: str, notes: str) -> None:
    r = _review(st, rid)
    if r["phase"] == "blind":
        raise HyraError("record a blind assessment before the verdict")
    if r["verdict"]:
        raise HyraError("verdict already recorded; verdicts are immutable (request a new review instead)")
    if remote.enabled(st):
        remote.review_verdict(st, rid, verdict, notes)
    st.x("UPDATE reviews SET verdict=?, notes=?, phase='final', updated=? WHERE review_task=?", verdict, notes, now(), rid)
    target = st.task(r["target_task"])
    prior = target["review_verdict"]
    # The weakest verdict wins: one refutation is not erased by a later 'supported'.
    rank = {v: i for i, v in enumerate(["supported", "inconclusive", "narrower", "refuted"])}
    combined = verdict if not prior or rank[verdict] > rank.get(prior, -1) else prior
    st.set_task(target["id"], review_verdict=combined)
    st.set_task(rid, status="done", summary=f"{verdict}: {notes}")
    st.post(me(), "review", f"Review of T{target['id']}: {verdict.upper()} — {notes}",
            mission=target["mission"], task=target["id"])
    if verdict in ("narrower", "refuted") and st.config.get("auto_check_tasks", True) \
            and st.mission(target["mission"])["status"] != "closed":
        # Spec: "flaws found during review can become automated checks in the toolchain". Make that a queued task, not a hope.
        cid = task_add(st, target["mission"], "build", f"Add an automated check for the flaw found reviewing T{target['id']}",
                       f"A review of T{target['id']} ({target['title']}) returned '{verdict}':\n\n{notes}\n\n"
                       "Turn this flaw into an automated check in the toolchain (a test, assertion, evaluation guard, lint, or a "
                       "validation step in the pipeline) that fails on the flawed version and passes on a correct one, so it is "
                       "caught automatically next time. Attach the check and a run showing it fail then pass as evidence. If an "
                       "automated check is genuinely impossible, record why as a Books observation (`hyra books add`) and complete "
                       "the task citing it. The Mission may cancel this task if the flaw was one-off.",
                       priority=max(10, target["priority"] - 10), deps=[], resources={}, model=None, force_approval=False)
        st.post("scheduler", "decision", f"Queued T{cid}: automated check for the flaw found in the review of T{target['id']}",
                mission=target["mission"], task=cid)
    st.x("INSERT INTO messages(sender,recipient,subject,body,requires_response,created) VALUES (?,?,?,?,?,?)",
         me(), f"mission:{target['mission']}", f"Review verdict for T{target['id']}: {verdict}", notes,
         int(verdict in ("refuted", "narrower")), now())
    print(f"verdict recorded: {verdict}")


# ---------------------------------------------------------------- memory & briefings
def _memory_index(st: Store, agent: str) -> Path:
    p = st.memory_path(agent)
    if not p.exists():
        p.write_text(memory_template(agent))
    return p


def _memory_nodes(st: Store, agent: str) -> list[tuple[str, str, str]]:
    """(relative path, title, summary) for every level-1 node, grouped by section order."""
    from .store import MEMORY_SECTIONS
    d, out = st.memory_dir(agent), []
    for sec in MEMORY_SECTIONS:
        for f in sorted((d / sec).glob("*.md")) if (d / sec).is_dir() else []:
            lines = f.read_text().splitlines()
            title = lines[0].lstrip("# ").strip() if lines else f.stem
            summ = next((l[8:].strip() for l in lines[1:6] if l.startswith("summary:")), "")
            out.append((f"{sec}/{f.name}", title, summ))
    return out


def memory_tree(st: Store, agent: str, limit: int = 40) -> None:
    nodes = _memory_nodes(st, agent)
    if not nodes:
        print("(no detail nodes yet: `hyra memory note <section> \"<title>\" --text ...`)")
    for rel, title, summ in nodes[:limit]:
        print(f"  {rel}: {title}" + (f" — {summ}" if summ else ""))
    if len(nodes) > limit:
        print(f"  ... {len(nodes) - limit} more")


def memory_note(st: Store, agent: str, section: str, title: str, text: str, summary: str) -> None:
    from .store import MEMORY_SECTIONS
    if section not in MEMORY_SECTIONS:
        raise HyraError(f"section must be one of: {', '.join(MEMORY_SECTIONS)}")
    _memory_index(st, agent)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "node"
    path = st.memory_dir(agent) / section / f"{slug}.md"
    path.parent.mkdir(exist_ok=True)
    verb = "updated" if path.exists() else "created"
    path.write_text(f"# {title}\nsummary: {summary or short(text, 100)}\nupdated: {now()} by {me()}\n\n{text}\n")
    print(f"{verb} {section}/{path.name} (level 1). Point to it from the index (`hyra memory path`) with one line.")


def memory_show(st: Store, agent: str, node: Optional[str] = None) -> None:
    d = st.memory_dir(agent)
    if node:
        p = (d / node).resolve()
        if d.resolve() not in p.parents or not p.is_file():
            raise HyraError(f"no memory node {node!r} (see `hyra memory tree`)")
        print(p.read_text())
        return
    print(f"(level 0: {_memory_index(st, agent)})\n")
    print(_memory_index(st, agent).read_text())
    print("## Detail nodes (level 1)")
    memory_tree(st, agent)


def brief(st: Store, agent: str, mark: bool = True) -> None:
    """Everything an agent needs on wake-up, without replaying a long session."""
    if agent == "overall":
        _maybe_backup(st)
    state = st.one("SELECT * FROM agent_state WHERE agent=?", agent)
    last = state["last_board_id"] if state else 0
    print(f"# Briefing for {agent} @ {now()}\n")
    cfg = st.config
    if cfg.get("goal"):
        print(f"Project goal: {cfg['goal']}\n")
    mission = None
    if agent.startswith("mission:"):
        mission = agent.split(":", 1)[1]
        m = st.mission(mission)
        print(f"## Your mission: {mission} [{m['status']}] — {m['title']}\n{m['problem']}\n")
        if m["wake_reason"]:
            print(f"Wake reason: {m['wake_reason']}\n")
    elif agent.startswith("task:"):
        task_show(st, int(agent.split(":", 1)[1]))
        print()
        mission = st.task(int(agent.split(":", 1)[1]))["mission"]
    from .wiki import vault_path
    wv = vault_path(st)
    if wv and os.environ.get("HYRA_ROLE") != "reviewer":  # reviewers verify from evidence, not background reading
        print(f"Wiki vault: {wv} (background knowledge; read-only for you: `hyra wiki search \"...\"`)\n")
    mem = st.memory_path(agent)
    if mem.exists():
        print(f"## Your working memory, level 0 ({mem})\n{mem.read_text()}")
        print("## Detail nodes (level 1; open with `hyra memory show --node <path>`)")
        memory_tree(st, agent)
        print()
    print("## Inbox")
    msg_inbox(st, agent, include_read=False)
    if agent == "overall":
        print("\n## Pending approvals")
        approval_list(st, False)
        print_pending_retros(st)
    if agent.startswith("mission:"):
        print("\n## Your tasks")
        task_list(st, None, mission)
        recent = st.q("SELECT * FROM tasks WHERE mission=? AND status IN ('done','failed') AND updated > COALESCE(?, '') ORDER BY id",
                      mission, state["last_seen"] if state else None)
        if recent:
            print("\n## Finished since your last briefing")
            for t in recent:
                rv = f" [review: {t['review_verdict']}]" if t["review_verdict"] else ""
                print(f"  T{t['id']} {t['status']} {t['kind']}: {short(t['title'], 60)}{rv}\n    {short(claim_text(st, t), 400)}")
    if agent == "overall" or agent.startswith("mission:"):  # workers get their task, not the idea backlog
        print_open_ideas(st, mission)
    print(f"\n## Research Board since #{last}")
    board_read(st, mission, last, None, 40)
    if mark:
        top = st.one("SELECT MAX(id) m FROM board")["m"] or 0
        st.x("INSERT INTO agent_state(agent,last_board_id,last_seen) VALUES (?,?,?) "
             "ON CONFLICT(agent) DO UPDATE SET last_board_id=excluded.last_board_id, last_seen=excluded.last_seen",
             agent, top, now())


def _maybe_backup(st: Store) -> None:
    from . import ledger_deploy
    ledger_deploy.maybe_backup(st.config)


def status(st: Store) -> None:
    _maybe_backup(st)
    cfg = st.config
    print(f"# Hyra project: {cfg.get('project') or st.root.name}  ({st.root})")
    if cfg.get("goal"):
        print(f"goal: {cfg['goal']}")
    used = usage(st)
    cap = cfg.get("resources", {})
    print("resources: " + ", ".join(f"{k} {used.get(k, 0):g}/{v}" for k, v in cap.items()))
    print("\n## Missions")
    mission_list(st, False)
    print("\n## Queue")
    task_list(st, None, None)
    r = ready(st)
    print(f"ready to run now: {', '.join('T%d' % t['id'] for t in r) or 'none'}")
    due = st.q("SELECT slug, wake_reason FROM missions WHERE status='active' AND wake_at IS NOT NULL AND wake_at <= ?", now())
    if due:
        print("missions due to wake: " + ", ".join(f"{d['slug']} ({short(d['wake_reason'], 40)})" for d in due))
    print("\n## Pending approvals")
    approval_list(st, False)
    pend = st.q("SELECT * FROM messages WHERE requires_response=1 AND response IS NULL ORDER BY id")
    if pend:
        print("\n## Messages awaiting response")
        for m in pend:
            print(f"  M{m['id']} {m['sender']} -> {m['recipient']}: {m['subject']}")
    unread = st.q("SELECT COUNT(*) n FROM messages WHERE recipient IN ('overall','human') AND read_at IS NULL")[0]["n"]
    if unread:
        print(f"\nunread messages for overall/human: {unread} (`hyra msg inbox overall`)")
    print_open_ideas(st, None)
    print_pending_retros(st)
    print("\n## Latest board")
    board_read(st, None, 0, None, 8)
