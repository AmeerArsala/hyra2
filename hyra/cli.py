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

from . import __version__, books, core, eb, wiki
from .store import HyraError, Store, die, find_root, skill_dir



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
    p.add_argument("--version", action="version", version=f"hyra {__version__}")
    p.add_argument("--as", dest="as_agent", help="act as this agent id (overall, mission:<slug>, task:<id>); "
                   "default $HYRA_AGENT or 'overall'")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="install the CLI (--cli) or the skills toolkit (--toolkit local|global)")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--cli", action="store_true", help="put `hyra` on PATH")
    g.add_argument("--toolkit", choices=["local", "global"],
                   help="skills for agent harnesses: local = this project (copied), global = every harness on this machine (linked)")
    s.add_argument("--project", help="local toolkit: project dir (default: cwd)")
    s.add_argument("--skills", help="comma list, e.g. hyra,hyra-retro (default: all; hyra is always included)")
    s.add_argument("--mode", choices=["copy", "link"], help="default: copy for local, link for global")
    s.add_argument("--all-harnesses", action="store_true", help="local: also create .claude/ and .qwen/ even if not detected")
    s.add_argument("--no-cli", action="store_true", help="toolkit: don't install the CLI when it's missing")
    s.add_argument("--check", action="store_true", help="--cli: only report whether the global hyra is missing / ok / older / newer than these skills")
    s.add_argument("--force", action="store_true", help="--cli: also replace a newer global hyra or a regular file in the way")
    s.add_argument("--agents-only", action="store_true", help="toolkit: only register the named agent files (hyra-worker, ...) for the harnesses that read them; leave skills alone (use after `npx skills add`)")
    s.add_argument("--bin-dir"); s.add_argument("--uninstall", action="store_true")
    s = sub.add_parser("init", help="create .hyra/ in the current directory")
    s.add_argument("--no-ledger", action="store_true", help="a purely local project: don't connect it to your ledger server")
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
    s.add_argument("--at", help="+30m, +2h, +1d or ISO time (fallback deadline if a condition is given; default 24h)")
    s.add_argument("--until-file", help="wake as soon as this path exists"); s.add_argument("--until-cmd", help="wake as soon as this shell command exits 0")
    s.add_argument("--reason", required=True)
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
    s.add_argument("--harness", help="agent harness for headless runs: claude, codex, agy, opencode, amp, or custom")
    s.add_argument("--needs-approval", action="store_true")
    s = t.add_parser("list"); s.add_argument("--status"); s.add_argument("--mission")
    s = t.add_parser("show"); s.add_argument("id", type=_tid)
    s = t.add_parser("next", help="claim the highest-priority ready task")
    s.add_argument("--worker", default=None); s.add_argument("--kind", action="append")
    s = t.add_parser("claim"); s.add_argument("id", type=_tid); s.add_argument("--worker", default=None)
    s = t.add_parser("note", help="append to the task's action log"); s.add_argument("id", type=_tid); s.add_argument("text")
    s = t.add_parser("sleep", help="park the task until a time; it is resumed afterwards")
    s.add_argument("id", type=_tid); s.add_argument("--until", help="+2h or ISO time (fallback deadline if a condition is given; default 24h)")
    s.add_argument("--until-file", help="resume as soon as this path exists (e.g. a job's output)")
    s.add_argument("--until-cmd", help="resume as soon as this shell command exits 0 (e.g. a finished-job check)")
    s.add_argument("--reason", required=True)
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
    s = b.add_parser("ideas", help="ideas on the board and whether each is untested / testing / handled")
    s.add_argument("--mission"); s.add_argument("--all", action="store_true", help="include handled ideas")
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
    s.add_argument("--harness", help="e.g. review Claude's work with codex for an independent perspective")
    s = r.add_parser("evidence"); s.add_argument("id", type=_tid)
    s = r.add_parser("blind"); s.add_argument("id", type=_tid); s.add_argument("--notes", required=True)
    s = r.add_parser("claim"); s.add_argument("id", type=_tid)
    s = r.add_parser("verdict"); s.add_argument("id", type=_tid); s.add_argument("--verdict", choices=core.VERDICTS, required=True)
    s.add_argument("--notes", required=True)

    # memory
    mm = sub.add_parser("memory", help="per-agent hierarchical working memory").add_subparsers(dest="sub", required=True)
    s = mm.add_parser("show", help="level 0 + the tree of nodes, or one node with --node"); s.add_argument("agent", nargs="?")
    s.add_argument("--node", help="relative path from `hyra memory tree`, e.g. hypotheses/lr-warmup.md")
    s = mm.add_parser("tree", help="list level-1 nodes with their one-line summaries"); s.add_argument("agent", nargs="?")
    s = mm.add_parser("note", help="create or rewrite a level-1 node"); s.add_argument("section", choices=["understanding", "hypotheses", "ruled-out", "next"])
    s.add_argument("title"); s.add_argument("--text", required=True); s.add_argument("--summary", default=""); s.add_argument("--agent")
    s = mm.add_parser("path", help="path of the level-0 index to edit"); s.add_argument("agent", nargs="?")

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
    sk = sub.add_parser("skill", help="distill reusable methods into Agent Skills (SKILL.md)").add_subparsers(dest="sub", required=True)
    s = sk.add_parser("new"); s.add_argument("name"); s.add_argument("--description", required=True)
    s.add_argument("--from-books", default="")
    sk.add_parser("list")
    s = sk.add_parser("publish"); s.add_argument("name")

    s = sub.add_parser("session", help="start your coding agent with a ledger token: `hyra session claude` (server mode)")
    s.add_argument("--role", default="overall", choices=["overall", "mission"]); s.add_argument("--ttl-hours", type=float, default=12)
    s.add_argument("command", nargs=argparse.REMAINDER, help="the agent command, e.g. claude, codex, omp")

    # config and diagnostics
    cf = sub.add_parser("config", help="show / set / validate configuration (defaults < ~/.hyra/config.json < project)").add_subparsers(dest="sub", required=True)
    cf.add_parser("show"); cf.add_parser("validate"); cf.add_parser("path")
    s = cf.add_parser("set"); s.add_argument("key", help="e.g. harness, overall_agent, harness_models.opencode"); s.add_argument("value", help="JSON or plain text")
    s.add_argument("--user", action="store_true", help="write ~/.hyra/config.json (machine-wide) instead of the project"); s.add_argument("--force", action="store_true")
    s = cf.add_parser("unset"); s.add_argument("key"); s.add_argument("--user", action="store_true")
    sub.add_parser("doctor", help="check what's installed and configured, and what to do next")

    # ledger server
    sv = sub.add_parser("server", help="optional ledger server: sealed claims, server-side blind review, human-only approvals").add_subparsers(dest="sub", required=True)
    s = sv.add_parser("serve", help="run the ledger (on another machine, e.g. a tailnet device)")
    s.add_argument("--data", default="~/.hyra-server"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=7421)
    s.add_argument("--no-print-tokens", action="store_true", help="write first-run tokens to <data>/initial-tokens.txt only (for services: keeps them out of the journal)")
    s.add_argument("--rotate-tokens", action="store_true", help="revoke the human and runner tokens and mint new ones, then serve")
    s.add_argument("--audit-days", type=float, default=180, help="keep the audit trail this long (expired tokens are pruned automatically; claims never are)")
    s = sv.add_parser("up", help="deploy the ledger to a tailnet device over ssh (auto-picks one) and save the connection")
    s.add_argument("target", nargs="?", help="user@host; omitted = the one you used before, else auto-select")
    s.add_argument("--auto", action="store_true", help="re-run auto-selection even if a host was used before")
    s.add_argument("--local", action="store_true", help="deploy on THIS machine (single-machine setups and tests; agents here share your user)")
    s.add_argument("--port", type=int, default=7421); s.add_argument("--no-service", action="store_true", help="plain background process instead of systemd/launchd")
    s.add_argument("--expose", choices=["auto", "serve", "bind", "none"], default="auto", help="serve = tailscale serve HTTPS; bind = tailnet address; none = loopback only")
    s.add_argument("--rotate-tokens", action="store_true"); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--backup-remote", metavar="URL", help="private git repo for ledger backups (otherwise asked in a terminal); `none` to skip")
    s.add_argument("--any-owner", action="store_true", help="auto-select may also pick devices shared with you by other tailnet users")
    s = sv.add_parser("down", help="stop the ledger over ssh (data kept unless --purge)")
    s.add_argument("--purge", action="store_true", help="also delete the database on the server"); s.add_argument("--yes", action="store_true")
    sv.add_parser("where", help="what is deployed, and is it healthy")
    s = sv.add_parser("backup", help="snapshot the ledger (no tokens) into a git repo and push it")
    s.add_argument("--repo", help="path of the backup git repo (remembered after the first time)")
    s.add_argument("--init", metavar="PATH", help="create/clone the backup repo at PATH first"); s.add_argument("--remote", help="with --init: its git remote (use a PRIVATE repo)")
    s.add_argument("--no-push", action="store_true"); s.add_argument("--quiet", action="store_true")
    s = sv.add_parser("restore", help="replace the ledger's data with a backup (new tokens are minted)")
    s.add_argument("--from", dest="src", required=True, help="a ledger.sql file, or a backup repo directory"); s.add_argument("--yes", action="store_true")
    s = sv.add_parser("dump", help="(on the server) print a token-free SQL snapshot"); s.add_argument("--data", default="~/.hyra-server")
    s = sv.add_parser("load", help="(on the server) build the ledger database from a dump"); s.add_argument("--data", default="~/.hyra-server")
    s.add_argument("--file", required=True); s.add_argument("--force", action="store_true")
    s = sv.add_parser("connect", help="point this project at a ledger (default: the one `server up` deployed)"); s.add_argument("--url"); s.add_argument("--project")
    sv.add_parser("status")
    s = sv.add_parser("token", help="mint a token for an interactive session (needs the human token)")
    s.add_argument("--role", required=True, choices=["overall", "mission", "worker", "reviewer", "retrospective"]); s.add_argument("--scope", default="")
    s.add_argument("--ttl-hours", type=float, help="token lifetime (default 24h)")

    # wiki bridge
    w = sub.add_parser("wiki", help="optional bridge to an llm-obsidian-wiki (Karpathy LLM wiki) vault").add_subparsers(dest="sub", required=True)
    s = w.add_parser("setup"); s.add_argument("--vault", required=True); s.add_argument("--cluster", default="hyra")
    s = w.add_parser("search", help="read-only search of the vault's wiki/"); s.add_argument("query")
    s.add_argument("--limit", type=int, default=8); s.add_argument("--all", action="store_true", help="include deprecated/superseded")
    s.add_argument("--drafts", action="store_true", help="include wiki/_drafts")
    s = w.add_parser("show"); s.add_argument("page")
    s = w.add_parser("export", help="verified/convention Books entry -> wiki/_drafts (needs approval)")
    s.add_argument("id"); s.add_argument("--type", choices=["concept", "decision", "comparison", "synthesis"], default="concept")

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
    s = e.add_parser("status"); s.add_argument("name")
    ins = e.add_parser("inspiration", help="the queue between the Context Agent and Proposal Agents").add_subparsers(dest="isub", required=True)
    s = ins.add_parser("add"); s.add_argument("name"); s.add_argument("--text", required=True)
    s.add_argument("--ref", action="append", default=[], help="solution id (repeatable)"); s.add_argument("--file", action="append", default=[])
    s = ins.add_parser("take"); s.add_argument("name")
    s = ins.add_parser("list"); s.add_argument("name"); s.add_argument("--all", action="store_true")
    s = e.add_parser("loop", help="run the autonomous Hyra 1.0 pipeline: Context Agent + parallel Proposal Agents + sandboxed scoring")
    s.add_argument("name"); s.add_argument("--proposers", type=int, default=3, help="max concurrent Proposal Agents (the semaphore)")
    s.add_argument("--max-submissions", type=int, default=30); s.add_argument("--minutes", type=float, default=60)
    s.add_argument("--target", type=float, help="stop when the best score reaches this")
    s.add_argument("--patience", type=int, default=0, help="stop after this many submissions without improvement")
    s.add_argument("--queue-low", type=int, default=2); s.add_argument("--per-round", type=int, default=4, help="inspirations the Context Agent adds per wake-up")
    s.add_argument("--rounds", type=int, default=1, help=">1 = bilevel: an Evaluator Agent may upgrade the evaluator between rounds")
    s.add_argument("--harness"); s.add_argument("--model"); s.add_argument("--poll", type=float, default=3.0)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--create-evaluator", action="store_true", help="bank doesn't exist yet: an Evaluator Agent writes eval.sh from --task")
    s.add_argument("--task", help="with --create-evaluator"); s.add_argument("--direction", choices=["max", "min"], default="max")
    s.add_argument("--timeout", type=int, default=600)

    # runner
    s = sub.add_parser("run", help="scheduler: launch headless Mission/Worker sessions and keep them moving")
    s.add_argument("--max-workers", type=int); s.add_argument("--once", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--task", type=_tid, help="launch just this task in a fresh headless session and wait for it")
    s.add_argument("--mission", help="launch just this Mission in its own fresh headless session and wait for it")
    s.add_argument("--overall", action="store_true", help="run one unattended Overall session now and wait for it")
    s.add_argument("--harness", help="override the harness for this run"); s.add_argument("--model")
    return p


def upgrade_notice() -> None:
    """If the skills this command found carry a newer hyra than the one running, say so once (stderr; agents ask the user)."""
    try:
        from .install import parse_version
        sk = skill_dir()
        v = (sk / "scripts" / "hyra.pyz.version").read_text().strip()
        if parse_version(v) and parse_version(v) > parse_version(__version__):
            print(f"hyra: these skills carry hyra {v} but this is {__version__}. Ask the user, then upgrade: "
                  f"python3 {sk}/scripts/hyra setup --cli", file=sys.stderr)
    except Exception:
        pass


def main(argv=None) -> None:
    args = build().parse_args(argv)
    if args.cmd != "setup":
        upgrade_notice()
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


def load_saved_url() -> str:
    from .ledger_deploy import load_state
    url = load_state().get("url")
    if not url:
        raise HyraError("no --url given and no ledger deployed from this machine: run `hyra server up`")
    return url


def dispatch(a) -> None:
    if a.cmd == "init":
        st = Store.init(Path.cwd(), a.project or Path.cwd().name, a.goal)
        print(f"initialized Hyra project at {st.dir}\nsee `hyra config show` for what applies; `hyra config set <key> <value>` to change it (resources, approval rules, harness, ...)")
        from . import onboarding
        onboarding.init_run(st, a.no_ledger)
        return
    if a.cmd == "setup":
        from .install import setup
        setup(a)
        return
    if a.cmd == "guide":
        print((skill_dir() / "SKILL.md").read_text())
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

    if a.cmd == "session":
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        if not cmd:
            raise HyraError("usage: hyra session [--role overall] [--ttl-hours 12] <agent command>   e.g. hyra session claude")
        from . import remote
        env = {k: v for k, v in os.environ.items() if k not in ("HYRA_HUMAN_TOKEN", "HYRA_RUNNER_TOKEN")}
        try:
            st0 = Store(find_root())
        except HyraError:
            st0 = None
        if st0 and remote.enabled(st0):
            env["HYRA_TOKEN"] = remote.mint_for_human(st0, a.role, "", a.ttl_hours)
            print(f"hyra: starting {cmd[0]} with a {a.role} ledger token valid for {a.ttl_hours:g}h", file=sys.stderr)
        os.execvpe(cmd[0], cmd, env)
    if a.cmd in ("doctor", "config"):  # work with or without a project
        from . import doctor as dr
        try:
            root = find_root()
        except HyraError:
            root = None
        if a.cmd == "doctor":
            dr.doctor()
        elif a.sub == "show":
            dr.config_show(root)
        elif a.sub == "set":
            dr.config_set(root, a.key, a.value, a.user, a.force)
        elif a.sub == "unset":
            dr.config_unset(root, a.key, a.user)
        elif a.sub == "validate":
            dr.config_validate(root)
        else:
            print(f"user:    {dr.user_config_path()}\nproject: {root / '.hyra' / 'config.json' if root else '(not in a project)'}")
        return
    if a.cmd == "server" and a.sub in ("dump", "load"):
        from .server import dump_sql, restore_sql
        if a.sub == "dump":
            sys.stdout.write(dump_sql(a.data))
        else:
            print(restore_sql(a.data, a.file, a.force))
        return
    if a.cmd == "server" and a.sub in ("backup", "restore"):
        from . import ledger_deploy as ld
        if a.sub == "backup":
            ld.backup(a.repo, a.init, a.remote, not a.no_push, quiet=a.quiet)
        else:
            ld.restore(a.src, a.yes)
        return
    if a.cmd == "server" and a.sub in ("down", "where"):
        from . import ledger_deploy as ld
        ld.down(a.purge, a.yes) if a.sub == "down" else ld.where()
        return
    if a.cmd == "server" and a.sub == "up":
        from . import ledger_deploy as ld
        from . import doctor as dr
        pref = list(dr._read(dr.user_config_path()).get("ledger_hosts", []))
        try:
            proot = find_root()
            pref += dr._read(proot / ".hyra" / "config.json").get("ledger_hosts", [])
        except HyraError:
            proot = None
        res = ld.up("local" if a.local else a.target, auto=a.auto, port=a.port, use_service=not a.no_service, rotate=a.rotate_tokens,
                    expose=a.expose, dry_run=a.dry_run, preferred=pref, any_owner=a.any_owner)
        if res and not a.dry_run:
            from . import onboarding
            onboarding.after_up(a.backup_remote)
        if res and proot and not a.dry_run:
            st0 = Store(proot)
            cfg_path = st0.dir / "config.json"
            cfg = json.loads(cfg_path.read_text())
            import uuid
            cfg["server"] = {"url": res["url"], "project": (cfg.get("server") or {}).get("project") or uuid.uuid4().hex}
            cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
            print(f"connected this project ({proot.name}) to the ledger")
        return
    if a.cmd == "server" and a.sub == "serve":
        from .server import serve
        serve(a.data, a.host, a.port, a.no_print_tokens, a.rotate_tokens, a.audit_days)
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
         "wake": lambda: core.mission_wake(st, a.slug, a.at, a.reason, a.until_file, a.until_cmd),
         "close": lambda: core.mission_close(st, a.slug, a.conclusion, a.accept_unreviewed)}[s]()
    elif c == "task":
        if s == "add":
            core.task_add(st, a.mission, a.kind, a.title, a.spec, a.priority, _ids(a.after), _res(a.resource),
                          a.model, a.needs_approval, harness=a.harness)
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
            core.task_sleep(st, a.id, a.until, a.reason, a.until_file, a.until_cmd)
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
        elif s == "ideas":
            rows = [(i, st_) for i, st_ in core.idea_states(st, a.mission) if a.all or st_ != "handled"]
            for i, st_ in rows:
                print(f"#{i['id']} [{st_}] ({i['mission'] or 'no mission'}) {i['author']}: {i['text']}")
            if not rows:
                print("(no open ideas)")
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
        {"request": lambda: core.review_request(st, a.target, a.focus, a.model, a.priority, a.harness),
         "evidence": lambda: core.review_evidence(st, a.id),
         "blind": lambda: core.review_blind(st, a.id, a.notes),
         "claim": lambda: core.review_claim(st, a.id),
         "verdict": lambda: core.review_verdict(st, a.id, a.verdict, a.notes)}[s]()
    elif c == "memory":
        who = a.agent or agent
        if s == "show":
            core.memory_show(st, who, a.node)
        elif s == "tree":
            core.memory_tree(st, who)
        elif s == "note":
            core.memory_note(st, who, a.section, a.title, a.text, a.summary)
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
    elif c == "server":
        from . import remote
        if s == "connect":
            import uuid
            cfg_path = st.dir / "config.json"
            cfg = json.loads(cfg_path.read_text())
            url = a.url or load_saved_url()
            cfg["server"] = {"url": url.rstrip("/"), "project": a.project or (cfg.get("server") or {}).get("project") or uuid.uuid4().hex}
            cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
            st.config["server"] = cfg["server"]
            print(f"project connected to {cfg['server']['url']} as project {cfg['server']['project']}: {remote.call(st, 'GET', '/v1/health')}")
            print("From now on: claims are sealed on the server, blind review and approvals are enforced there. "
                  "Start the scheduler with HYRA_RUNNER_TOKEN set; decide approvals with the human token.")
        elif s == "status":
            print(f"server: {st.config.get('server') or 'not configured (local mode)'}")
            if remote.enabled(st):
                print(f"health: {remote.call(st, 'GET', '/v1/health')}")
        elif s == "token":
            print(remote.mint_for_human(st, a.role, a.scope, a.ttl_hours))
    elif c == "wiki":
        if s == "setup":
            wiki.setup(st, a.vault, a.cluster)
        elif s == "search":
            wiki.search(st, a.query, a.limit, a.all, a.drafts)
        elif s == "show":
            wiki.show(st, a.page)
        elif s == "export":
            wiki.export(st, a.id, a.type)
    elif c == "skill":
        if s == "new":
            books.skill_new(st, a.name, a.description, [x for x in a.from_books.split(",") if x])
        elif s == "publish":
            books.skill_publish(st, a.name)
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
        elif s == "status":
            eb.eb_status(st, a.name)
        elif s == "inspiration":
            {"add": lambda: eb.insp_add(st, a.name, a.text, a.ref, a.file), "take": lambda: eb.insp_take(st, a.name),
             "list": lambda: eb.insp_list(st, a.name, a.all)}[a.isub]()
        elif s == "loop":
            from .ebloop import EbLoop
            lp = EbLoop(st, a.name, proposers=a.proposers, max_submissions=a.max_submissions, minutes=a.minutes,
                        target=a.target, patience=a.patience, queue_low=a.queue_low, per_round=a.per_round,
                        harness=a.harness, model=a.model, rounds=a.rounds, poll=a.poll, dry=a.dry_run)
            if not (st.dir / "eb" / a.name / "eb.json").exists():
                if not (a.create_evaluator and a.task):
                    raise HyraError(f"no bank {a.name!r}: `hyra eb init` it, or pass --create-evaluator --task \"...\"")
                lp.create_evaluator(a.task, a.direction, a.timeout)
            lp.run()
        elif s == "list":
            for d in sorted((st.dir / "eb").glob("*/eb.json")):
                cfg = json.loads(d.read_text())
                print(f"{cfg['name']}: {cfg['task'][:80]} (evaluator {cfg['evaluator']}, {cfg['direction']})")
    elif c == "run":
        from .runner import Runner
        r = Runner(st, a.max_workers, a.dry_run, a.harness, a.model)
        r.run_one(a.task) if a.task else r.run_mission(a.mission) if a.mission else r.run_overall() if a.overall else r.loop(a.once or a.dry_run)


if __name__ == "__main__":
    main(sys.argv[1:])
