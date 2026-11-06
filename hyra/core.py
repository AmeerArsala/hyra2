"""Missions, task queue, Research Board, messages, approvals, reviews, memory, briefings."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from .store import HyraError, Store, is_due, memory_template, now

TASK_KINDS = ["explore", "experiment", "analysis", "build", "review", "other"]
BOARD_TYPES = ["idea", "finding", "evidence", "decision", "note", "review", "retro"]
VERDICTS = ["supported", "narrower", "refuted", "inconclusive"]
WORKER_ROLES = {"worker", "reviewer", "mission"}


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
    for b in st.q("SELECT * FROM board WHERE mission=? ORDER BY id DESC LIMIT 15", slug)[::-1]:
        print(f"  #{b['id']} {b['type']} ({b['author']}): {short(b['text'], 140)}")


def mission_set(st: Store, slug: str, status: Optional[str], priority: Optional[int]) -> None:
    st.mission(slug)
    if status:
        st.x("UPDATE missions SET status=?, updated=? WHERE slug=?", status, now(), slug)
    if priority is not None:
        st.x("UPDATE missions SET priority=?, updated=? WHERE slug=?", priority, now(), slug)
    st.post(me(), "decision", f"Mission {slug}: status={status or '-'} priority={priority if priority is not None else '-'}", mission=slug)
    print("ok")


def mission_wake(st: Store, slug: str, when: str, reason: str) -> None:
    from .store import parse_when
    st.mission(slug)
    st.x("UPDATE missions SET wake_at=?, wake_reason=?, updated=? WHERE slug=?", parse_when(when), reason, now(), slug)
    print(f"mission {slug} will wake at {parse_when(when)}: {reason}")


def mission_close(st: Store, slug: str, conclusion: str, accept_unreviewed: bool) -> None:
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
    st.x("UPDATE missions SET status='closed', conclusion=?, updated=? WHERE slug=?", conclusion, now(), slug)
    st.post(me(), "decision", f"Closed mission {slug}. Conclusion: {conclusion}", mission=slug)
    print(f"mission {slug} closed. Consider a retrospective: distill Books/skills (see hyra-retro skill).")


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
        aid = st.x("INSERT INTO approvals(kind,what,task,requested_by,created) VALUES (?,?,?,?,?)",
                   "compute", what, tid, me(), now())
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


def task_claim(st: Store, worker: str, tid: Optional[int] = None, kinds: Optional[list[str]] = None) -> Optional[int]:
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
        print(f"\n## Summary\n{t['summary']}")
        _print_evidence(st, tid)
    if t["review_verdict"]:
        print(f"\n## Review verdict: {t['review_verdict']}")
        for r in st.q("SELECT * FROM reviews WHERE target_task=? AND verdict IS NOT NULL", tid):
            print(f"  (T{r['review_task']}) {r['verdict']}: {r['notes']}")


def _hide_summary_from(st: Store, target: int) -> bool:
    """A reviewer in blind phase for this target must not see the claim via task show."""
    cur = os.environ.get("HYRA_TASK")
    if not cur or not cur.isdigit():
        return False
    rv = st.one("SELECT phase FROM reviews WHERE review_task=? AND target_task=?", int(cur), target)
    return bool(rv and rv["phase"] == "blind")


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


def task_sleep(st: Store, tid: int, until: str, reason: str) -> None:
    from .store import parse_when
    t = st.task(tid)
    if t["status"] not in ("running", "queued"):
        raise HyraError(f"T{tid} is {t['status']}")
    when = parse_when(until)
    st.set_task(tid, status="sleeping", wake_at=when, wake_reason=reason, worker=None)
    st.note(tid, me(), f"sleeping until {when}: {reason}")
    print(f"T{tid} sleeping until {when}. Record what to check on wake-up in a note/memory before exiting.")


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
    if not evidence and not allow_no_evidence:
        raise HyraError("completion requires evidence: --evidence <path|commit:REV|url:...|eb:...> "
                        "(code versions, results, logs, or raw traces). Use --no-evidence only for pure "
                        "planning/notes tasks, and say why in the summary.")
    attach_evidence(st, tid, evidence)
    head = _git_head(Path.cwd())
    if head:
        st.x("INSERT INTO evidence(task,kind,ref,note,created) VALUES (?,?,?,?,?)", tid, "git-head", head,
             f"code version in {Path.cwd()}", now())
    st.set_task(tid, status="done", summary=summary)
    st.post(me(), "finding" if t["kind"] != "explore" else "idea", f"T{tid} done: {summary}",
            mission=t["mission"], task=tid, refs=evidence)
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
def board_post(st: Store, type_: str, text: str, mission: Optional[str], task: Optional[int], refs: list[str]) -> None:
    if mission:
        st.mission(mission)
    bid = st.post(me(), type_, text, mission=mission, task=task, refs=refs)
    print(f"posted #{bid}")


def board_read(st: Store, mission: Optional[str], since: int, types: Optional[list[str]], limit: int) -> None:
    sql, args = "SELECT * FROM board WHERE id>?", [since]
    if mission:
        sql += " AND (mission=? OR mission IS NULL)"; args.append(mission)
    if types:
        sql += f" AND type IN ({','.join('?' * len(types))})"; args += types
    rows = st.q(sql + " ORDER BY id DESC LIMIT ?", *args, limit)[::-1]
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
    aid = st.x("INSERT INTO approvals(kind,what,task,ref,requested_by,created) VALUES (?,?,?,?,?,?)",
               kind, what, task, ref, me(), now())
    st.x("INSERT INTO messages(sender,recipient,subject,body,requires_response,created) VALUES (?,?,?,?,?,?)",
         me(), "overall", f"Approval A{aid} needed ({kind})", what, 0, now())
    print(f"approval A{aid} requested; a human must grant it (`hyra approval grant {aid}`)")
    return aid


def approval_list(st: Store, show_all: bool) -> None:
    rows = st.q("SELECT * FROM approvals " + ("" if show_all else "WHERE status='pending' ") + "ORDER BY id")
    if not rows:
        print("(no pending approvals)")
    for a in rows:
        t = f" T{a['task']}" if a["task"] else ""
        d = f" by {a['decided_by']}" if a["decided_by"] else ""
        print(f"A{a['id']} [{a['status']}{d}] {a['kind']}{t}: {a['what']} (requested by {a['requested_by']})")


def approval_decide(st: Store, aid: int, grant: bool, by: str, note: str) -> None:
    if role() in WORKER_ROLES:
        raise HyraError("agents cannot decide approvals; only the human (via Overall) can")
    a = st.one("SELECT * FROM approvals WHERE id=?", aid)
    if not a:
        raise HyraError(f"no approval A{aid}")
    if a["status"] != "pending":
        raise HyraError(f"A{aid} already {a['status']}")
    status = "granted" if grant else "denied"
    st.x("UPDATE approvals SET status=?, decided_by=?, decided_at=?, note=? WHERE id=?", status, by, now(), note, aid)
    if a["task"] and a["kind"] == "compute":
        t = st.task(a["task"])
        if t["status"] == "awaiting_approval":
            st.set_task(a["task"], status="queued" if grant else "cancelled",
                        summary=None if grant else f"approval denied: {note}")
    st.post(by, "decision", f"Approval A{aid} {status}: {a['what']}" + (f" — {note}" if note else ""),
            task=a["task"])
    print(f"A{aid} {status}")


def approval_granted(st: Store, kind: str, ref: str) -> bool:
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
    st.x("UPDATE reviews SET blind_notes=?, phase='revealed', updated=? WHERE review_task=?", notes, now(), rid)
    st.note(rid, me(), f"blind assessment: {notes}")
    print("blind assessment recorded; you may now run `hyra review claim`.")


def review_claim(st: Store, rid: int) -> None:
    r = _review(st, rid)
    if r["phase"] == "blind":
        raise HyraError("record your independent assessment first: `hyra review blind <task> --notes ...`")
    t = st.task(r["target_task"])
    print(f"# Worker's claim for T{t['id']}\n{t['summary']}\n\n# Your blind assessment\n{r['blind_notes']}")


def review_verdict(st: Store, rid: int, verdict: str, notes: str) -> None:
    r = _review(st, rid)
    if r["phase"] == "blind":
        raise HyraError("record a blind assessment before the verdict")
    if r["verdict"]:
        raise HyraError("verdict already recorded; verdicts are immutable (request a new review instead)")
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
    st.x("INSERT INTO messages(sender,recipient,subject,body,requires_response,created) VALUES (?,?,?,?,?,?)",
         me(), f"mission:{target['mission']}", f"Review verdict for T{target['id']}: {verdict}", notes,
         int(verdict in ("refuted", "narrower")), now())
    print(f"verdict recorded: {verdict}")


# ---------------------------------------------------------------- memory & briefings
def memory_show(st: Store, agent: str) -> None:
    p = st.memory_path(agent)
    if not p.exists():
        p.write_text(memory_template(agent))
    print(f"(memory file: {p})\n")
    print(p.read_text())


def brief(st: Store, agent: str, mark: bool = True) -> None:
    """Everything an agent needs on wake-up, without replaying a long session."""
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
    mem = st.memory_path(agent)
    if mem.exists():
        print(f"## Your working memory ({mem})\n{mem.read_text()}")
    print("## Inbox")
    msg_inbox(st, agent, include_read=False)
    if agent == "overall":
        print("\n## Pending approvals")
        approval_list(st, False)
    if agent.startswith("mission:"):
        print("\n## Your tasks")
        task_list(st, None, mission)
        recent = st.q("SELECT * FROM tasks WHERE mission=? AND status IN ('done','failed') AND updated > COALESCE(?, '') ORDER BY id",
                      mission, state["last_seen"] if state else None)
        if recent:
            print("\n## Finished since your last briefing")
            for t in recent:
                rv = f" [review: {t['review_verdict']}]" if t["review_verdict"] else ""
                print(f"  T{t['id']} {t['status']} {t['kind']}: {short(t['title'], 60)}{rv}\n    {short(t['summary'], 400)}")
    print(f"\n## Research Board since #{last}")
    board_read(st, mission, last, None, 40)
    if mark:
        top = st.one("SELECT MAX(id) m FROM board")["m"] or 0
        st.x("INSERT INTO agent_state(agent,last_board_id,last_seen) VALUES (?,?,?) "
             "ON CONFLICT(agent) DO UPDATE SET last_board_id=excluded.last_board_id, last_seen=excluded.last_seen",
             agent, top, now())


def status(st: Store) -> None:
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
    print("\n## Latest board")
    board_read(st, None, 0, None, 8)
