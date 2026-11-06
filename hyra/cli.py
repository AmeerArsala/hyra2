"""hyra — command-line harness for Hyra 2.0-style long-running agentic research.

Roles: Overall (talks to the human, priorities, approvals) -> Missions (own a research problem,
design experiments, judge results) -> Workers (run experiments, report with evidence) and
Review Workers (blind, independent verification). See `hyra guide` for the protocol.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import books, core, eb
from .store import HyraError, Store, die

GUIDE = Path(__file__).resolve().parent.parent / "claude" / "skills" / "hyra" / "SKILL.md"


def _ids(s: str) -> list[int]:
    return [int(x.strip().lstrip("Tt")) for x in s.split(",") if x.strip()] if s else []


def _tid(s: str) -> int:
    return int(str(s).lstrip("Tt"))


def _res(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k] = float(v) if v else 1.0
    return out


def build() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hyra", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--as", dest="as_agent", help="act as this agent id (overall, mission:<slug>, task:<id>); "
                   "default $HYRA_AGENT or 'overall'")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create .hyra/ in the current directory")
    s.add_argument("--project", default="")
    s.add_argument("--goal", default="")

    sub.add_parser("status", help="dashboard: missions, queue, approvals, mail, board")
    s = sub.add_parser("brief", help="wake-up briefing for an agent (memory, inbox, tasks, new board entries)")
    s.add_argument("agent", nargs="?", default=None)
    s.add_argument("--no-mark", action="store_true", help="don't advance the agent's board cursor")
    sub.add_parser("guide", help="print the Hyra operating guide")

    # missions
    m = sub.add_parser("mission", help="missions: long-lived ownership of one research problem").add_subparsers(dest="sub", required=True)
    s = m.add_parser("new"); s.add_argument("slug"); s.add_argument("--title", required=True)
    s.add_argument("--problem", required=True, help="problem statement incl. success criteria and constraints")
    s.add_argument("--priority", type=int, default=50)
    s = m.add_parser("list"); s.add_argument("--all", action="store_true")
    s = m.add_parser("show"); s.add_argument("slug")
    s = m.add_parser("set"); s.add_argument("slug"); s.add_argument("--status", choices=["active", "paused"])
    s.add_argument("--priority", type=int)
    s = m.add_parser("wake", help="schedule the mission's next wake-up"); s.add_argument("slug")
    s.add_argument("--at", required=True, help="+30m, +2h, +1d or ISO time"); s.add_argument("--reason", required=True)
    s = m.add_parser("close"); s.add_argument("slug"); s.add_argument("--conclusion", required=True)
    s.add_argument("--accept-unreviewed", action="store_true")

    # tasks
    t = sub.add_parser("task", help="the persistent task queue").add_subparsers(dest="sub", required=True)
    s = t.add_parser("add"); s.add_argument("--mission", required=True)
    s.add_argument("--kind", choices=core.TASK_KINDS, default="experiment")
    s.add_argument("--title", required=True); s.add_argument("--spec", required=True,
                                                             help="what to do, what to report, acceptance criteria")
    s.add_argument("--priority", type=int, default=50); s.add_argument("--after", default="", help="dependency ids, e.g. 3,4")
    s.add_argument("--resource", action="append", help="e.g. gpu=2 (repeatable)")
    s.add_argument("--model", help="model for this worker (diversity of perspectives)")
    s.add_argument("--needs-approval", action="store_true")
    s = t.add_parser("list"); s.add_argument("--status"); s.add_argument("--mission")
    s = t.add_parser("show"); s.add_argument("id", type=_tid)
    s = t.add_parser("next", help="claim the highest-priority ready task")
    s.add_argument("--worker", default=None); s.add_argument("--kind", action="append")
    s = t.add_parser("claim"); s.add_argument("id", type=_tid); s.add_argument("--worker", default=None)
    s = t.add_parser("note", help="append to the task's action log"); s.add_argument("id", type=_tid); s.add_argument("text")
    s = t.add_parser("sleep", help="park the task until a time; it is resumed afterwards")
    s.add_argument("id", type=_tid); s.add_argument("--until", required=True); s.add_argument("--reason", required=True)
    s = t.add_parser("complete"); s.add_argument("id", type=_tid); s.add_argument("--summary", required=True)
    s.add_argument("--evidence", action="append", default=[], help="path | commit:REV | url:... | eb:name/sid | task:N (repeatable)")
    s.add_argument("--no-evidence", action="store_true")
    s = t.add_parser("evidence", help="attach more evidence"); s.add_argument("id", type=_tid)
    s.add_argument("refs", nargs="+"); s.add_argument("--note")
    s = t.add_parser("fail"); s.add_argument("id", type=_tid); s.add_argument("--reason", required=True)
    s.add_argument("--requeue", action="store_true")
    s = t.add_parser("cancel"); s.add_argument("id", type=_tid); s.add_argument("--reason", required=True)
    s = t.add_parser("release", help="return a stuck running task to the queue"); s.add_argument("id", type=_tid)

    # board
    b = sub.add_parser("board", help="the Research Board").add_subparsers(dest="sub", required=True)
    s = b.add_parser("post"); s.add_argument("--type", choices=core.BOARD_TYPES, required=True)
    s.add_argument("text"); s.add_argument("--mission"); s.add_argument("--task", type=_tid)
    s.add_argument("--ref", action="append", default=[])
    s = b.add_parser("read"); s.add_argument("--mission"); s.add_argument("--since", type=int, default=0)
    s.add_argument("--type", action="append"); s.add_argument("--limit", type=int, default=30)

    # messages
    g = sub.add_parser("msg", help="messages that require action").add_subparsers(dest="sub", required=True)
    s = g.add_parser("send"); s.add_argument("--to", required=True, help="overall | human | mission:<slug> | task:<id> | mission-workers:<slug>")
    s.add_argument("--subject", required=True); s.add_argument("--body", required=True)
    s.add_argument("--requires-response", action="store_true")
    s = g.add_parser("inbox"); s.add_argument("agent", nargs="?"); s.add_argument("--all", action="store_true")
    s = g.add_parser("reply"); s.add_argument("id", type=lambda v: int(str(v).lstrip("Mm"))); s.add_argument("body")

    # approvals
    a = sub.add_parser("approval", help="human approval gates").add_subparsers(dest="sub", required=True)
    s = a.add_parser("request"); s.add_argument("--kind", choices=["compute", "knowledge-sync", "convention", "other"], required=True)
    s.add_argument("--what", required=True); s.add_argument("--task", type=_tid); s.add_argument("--ref")
    s = a.add_parser("list"); s.add_argument("--all", action="store_true")
    for name in ("grant", "deny"):
        s = a.add_parser(name); s.add_argument("id", type=lambda v: int(str(v).lstrip("Aa")))
        s.add_argument("--by", default="human"); s.add_argument("--note", default="")

    # reviews
    r = sub.add_parser("review", help="independent blind review").add_subparsers(dest="sub", required=True)
    s = r.add_parser("request"); s.add_argument("target", type=_tid); s.add_argument("--focus", default="")
    s.add_argument("--model"); s.add_argument("--priority", type=int)
    s = r.add_parser("evidence"); s.add_argument("id", type=_tid)
    s = r.add_parser("blind"); s.add_argument("id", type=_tid); s.add_argument("--notes", required=True)
    s = r.add_parser("claim"); s.add_argument("id", type=_tid)
    s = r.add_parser("verdict"); s.add_argument("id", type=_tid); s.add_argument("--verdict", choices=core.VERDICTS, required=True)
    s.add_argument("--notes", required=True)

    # memory
    mm = sub.add_parser("memory", help="per-agent hierarchical working memory").add_subparsers(dest="sub", required=True)
    s = mm.add_parser("show"); s.add_argument("agent", nargs="?")
    s = mm.add_parser("path"); s.add_argument("agent", nargs="?")

    # books
    k = sub.add_parser("books", help="shared knowledge base (observation / verified / convention)").add_subparsers(dest="sub", required=True)
    s = k.add_parser("add"); s.add_argument("--title", required=True)
    s.add_argument("--tier", choices=["observation", "convention"], default="observation")
    s.add_argument("--method", required=True); s.add_argument("--rationale", required=True)
    s.add_argument("--applies-when", required=True); s.add_argument("--evidence", action="append", default=[])
    s.add_argument("--tags", default="")
    s = k.add_parser("search"); s.add_argument("query", nargs="?", default=""); s.add_argument("--tier")
    s.add_argument("--limit", type=int, default=10)
    s = k.add_parser("show"); s.add_argument("id")
    s = k.add_parser("promote"); s.add_argument("id"); s.add_argument("--to", choices=["verified", "convention"], required=True)
    s.add_argument("--evidence", action="append", default=[]); s.add_argument("--note", default="")
    s = k.add_parser("revise"); s.add_argument("id"); s.add_argument("--note", required=True)
    s.add_argument("--evidence", action="append", default=[]); s.add_argument("--demote", action="store_true")
    s = k.add_parser("sync", help="copy a project entry to the shared library (needs approval)"); s.add_argument("id")

    # skills
    sk = sub.add_parser("skill", help="distill reusable methods into Claude Code skills").add_subparsers(dest="sub", required=True)
    s = sk.add_parser("new"); s.add_argument("name"); s.add_argument("--description", required=True)
    s.add_argument("--from-books", default="")
    sk.add_parser("list")
    s = sk.add_parser("publish"); s.add_argument("name"); s.add_argument("--claude-skills-dir")

    # experience banks
    e = sub.add_parser("eb", help="Hyra 1.0 score-driven loop (Experience Bank) as a tool").add_subparsers(dest="sub", required=True)
    s = e.add_parser("init"); s.add_argument("name"); s.add_argument("--evaluator", required=True, help="eval.sh or dir containing it")
    s.add_argument("--task", required=True); s.add_argument("--direction", choices=["max", "min"], default="max")
    s.add_argument("--timeout", type=int, default=600)
    s = e.add_parser("submit"); s.add_argument("name"); s.add_argument("solution", help="dir with solve.sh")
    s.add_argument("--parent", action="append", default=[]); s.add_argument("--note", default="")
    s = e.add_parser("top"); s.add_argument("name"); s.add_argument("-n", type=int, default=10); s.add_argument("--all-evaluators", action="store_true")
    s = e.add_parser("inspire"); s.add_argument("name"); s.add_argument("-k", type=int, default=4)
    s = e.add_parser("evaluator", help="install an upgraded evaluator (bilevel loop)"); s.add_argument("name")
    s.add_argument("--new", required=True); s.add_argument("--note", required=True)
    s = e.add_parser("rescore"); s.add_argument("name"); s.add_argument("--top", type=int, default=5)
    s = e.add_parser("list")

    # runner
    s = sub.add_parser("run", help="scheduler: launch headless Mission/Worker sessions and keep them moving")
    s.add_argument("--max-workers", type=int); s.add_argument("--once", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    return p


def main(argv=None) -> None:
    args = build().parse_args(argv)
    if args.as_agent:
        os.environ["HYRA_AGENT"] = args.as_agent
        if args.as_agent.startswith("task:"):
            os.environ["HYRA_TASK"] = args.as_agent[5:]
    try:
        dispatch(args)
    except HyraError as e:
        die(str(e))
    except BrokenPipeError:
        pass


def dispatch(a) -> None:
    if a.cmd == "init":
        st = Store.init(Path.cwd(), a.project or Path.cwd().name, a.goal)
        print(f"initialized Hyra project at {st.dir}\nedit {st.dir/'config.json'} for resources, approval rules and the agent command")
        return
    if a.cmd == "guide":
        print(GUIDE.read_text() if GUIDE.exists() else "guide not found")
        return
    if a.cmd == "books" and a.sub in ("search", "show"):
        try:
            st = Store.open()
        except HyraError:
            st = None
        (books.books_search(st, a.query, a.tier, a.limit) if a.sub == "search" else books.books_show(st, a.id))
        return
    if a.cmd == "skill" and a.sub == "list":
        try:
            st = Store.open()
        except HyraError:
            st = None
        books.skill_list(st)
        return

    st = Store.open()
    agent = core.me()
    c, s = a.cmd, getattr(a, "sub", None)
    if c == "status":
        core.status(st)
    elif c == "brief":
        core.brief(st, a.agent or agent, mark=not a.no_mark)
    elif c == "mission":
        {"new": lambda: core.mission_new(st, a.slug, a.title, a.problem, a.priority),
         "list": lambda: core.mission_list(st, a.all),
         "show": lambda: core.mission_show(st, a.slug),
         "set": lambda: core.mission_set(st, a.slug, a.status, a.priority),
         "wake": lambda: core.mission_wake(st, a.slug, a.at, a.reason),
         "close": lambda: core.mission_close(st, a.slug, a.conclusion, a.accept_unreviewed)}[s]()
    elif c == "task":
        if s == "add":
            core.task_add(st, a.mission, a.kind, a.title, a.spec, a.priority, _ids(a.after), _res(a.resource),
                          a.model, a.needs_approval)
        elif s == "list":
            core.task_list(st, a.status, a.mission)
        elif s == "show":
            core.task_show(st, a.id)
        elif s in ("next", "claim"):
            tid = core.task_claim(st, a.worker or agent, a.id if s == "claim" else None, getattr(a, "kind", None))
            if tid is None:
                print("(nothing ready to claim)")
            else:
                print(f"claimed T{tid}\n")
                core.task_show(st, tid)
        elif s == "note":
            core.task_note(st, a.id, a.text)
        elif s == "sleep":
            core.task_sleep(st, a.id, a.until, a.reason)
        elif s == "complete":
            core.task_complete(st, a.id, a.summary, a.evidence, a.no_evidence)
        elif s == "evidence":
            ids = core.attach_evidence(st, a.id, a.refs, a.note)
            print(f"attached {len(ids)} evidence item(s)")
        elif s == "fail":
            core.task_fail(st, a.id, a.reason, a.requeue)
        elif s == "cancel":
            core.task_cancel(st, a.id, a.reason)
        elif s == "release":
            core.task_release(st, a.id)
    elif c == "board":
        if s == "post":
            core.board_post(st, a.type, a.text, a.mission, a.task, a.ref)
        else:
            core.board_read(st, a.mission, a.since, a.type, a.limit)
    elif c == "msg":
        if s == "send":
            core.msg_send(st, a.to, a.subject, a.body, a.requires_response)
        elif s == "inbox":
            core.msg_inbox(st, a.agent or agent, a.all)
        else:
            core.msg_reply(st, a.id, a.body)
    elif c == "approval":
        if s == "request":
            core.approval_request(st, a.kind, a.what, a.task, a.ref)
        elif s == "list":
            core.approval_list(st, a.all)
        else:
            core.approval_decide(st, a.id, s == "grant", a.by, a.note)
    elif c == "review":
        {"request": lambda: core.review_request(st, a.target, a.focus, a.model, a.priority),
         "evidence": lambda: core.review_evidence(st, a.id),
         "blind": lambda: core.review_blind(st, a.id, a.notes),
         "claim": lambda: core.review_claim(st, a.id),
         "verdict": lambda: core.review_verdict(st, a.id, a.verdict, a.notes)}[s]()
    elif c == "memory":
        who = a.agent or agent
        if s == "show":
            core.memory_show(st, who)
        else:
            p = st.memory_path(who)
            if not p.exists():
                from .store import memory_template
                p.write_text(memory_template(who))
            print(p)
    elif c == "books":
        if s == "add":
            books.books_add(st, a.title, a.tier, a.method, a.rationale, a.applies_when, a.evidence, a.tags)
        elif s == "promote":
            books.books_promote(st, a.id, a.to, a.evidence, a.note)
        elif s == "revise":
            books.books_revise(st, a.id, a.note, a.evidence, a.demote)
        elif s == "sync":
            books.books_sync(st, a.id)
    elif c == "skill":
        if s == "new":
            books.skill_new(st, a.name, a.description, [x for x in a.from_books.split(",") if x])
        elif s == "publish":
            books.skill_publish(st, a.name, a.claude_skills_dir)
    elif c == "eb":
        if s == "init":
            eb.eb_init(st, a.name, a.evaluator, a.task, a.direction, a.timeout)
        elif s == "submit":
            eb.eb_submit(st, a.name, a.solution, a.parent, a.note)
        elif s == "top":
            eb.eb_top(st, a.name, a.n, a.all_evaluators)
        elif s == "inspire":
            eb.eb_inspire(st, a.name, a.k)
        elif s == "evaluator":
            eb.eb_evaluator(st, a.name, a.new, a.note)
        elif s == "rescore":
            eb.eb_rescore(st, a.name, a.top)
        elif s == "list":
            for d in sorted((st.dir / "eb").glob("*/eb.json")):
                cfg = json.loads(d.read_text())
                print(f"{cfg['name']}: {cfg['task'][:80]} (evaluator {cfg['evaluator']}, {cfg['direction']})")
    elif c == "run":
        from .runner import Runner
        Runner(st, a.max_workers, a.dry_run).loop(a.once or a.dry_run)


if __name__ == "__main__":
    main(sys.argv[1:])
