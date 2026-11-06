"""Experience Bank: the Hyra 1.0 loop as a reusable tool for score-driven sub-problems.

A loop has a versioned evaluator and a bank of solutions. Each solution is a directory whose
entry point is solve.sh. `submit` copies the solution into a fresh scratch directory, runs the
current evaluator against it, parses the score, and records score + logs + lineage in the bank.
Agents play the Context Agent (`inspire` assembles inspiration bundles from the bank) and the
Proposal Agents (write new solution dirs and submit them). When the inner loop stalls, upgrade
the evaluator (bilevel loop) and `rescore` the top solutions under it.

Evaluator contract: <evaluator_dir>/eval.sh is run as `bash eval.sh <abs solution dir>` with cwd at
the scratch dir. It must print a line `SCORE: <number>` (last one wins). Optional extra lines
`METRIC <name>: <value>` are recorded too. Non-zero exit or no score = invalid submission.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from .core import me
from .store import HyraError, Store, now

SCORE_RE = re.compile(r"^SCORE:\s*([-+0-9.eE]+|nan|inf)\s*$", re.M)
METRIC_RE = re.compile(r"^METRIC\s+([\w.-]+):\s*(.+?)\s*$", re.M)


def _root(st: Store, name: str) -> Path:
    d = st.dir / "eb" / name
    if not (d / "eb.json").exists():
        raise HyraError(f"no experience bank {name!r} (create with `hyra eb init`)")
    return d


def _cfg(d: Path) -> dict:
    return json.loads((d / "eb.json").read_text())


def _save(d: Path, cfg: dict) -> None:
    (d / "eb.json").write_text(json.dumps(cfg, indent=2) + "\n")


def _results(d: Path) -> list[dict]:
    p = d / "results.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def _better(cfg: dict):
    return (lambda a, b: a > b) if cfg["direction"] == "max" else (lambda a, b: a < b)


def eb_init(st: Store, name: str, evaluator: str, task_desc: str, direction: str, timeout: int) -> None:
    d = st.dir / "eb" / name
    if (d / "eb.json").exists():
        raise HyraError(f"bank {name} exists")
    src = Path(evaluator).expanduser().resolve()
    if not src.exists():
        raise HyraError(f"evaluator not found: {evaluator}")
    v1 = d / "evaluators" / "v1"
    v1.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, v1)
    else:
        v1.mkdir()
        shutil.copy2(src, v1 / "eval.sh")
    if not (v1 / "eval.sh").exists():
        raise HyraError("evaluator dir must contain eval.sh")
    (d / "solutions").mkdir(parents=True, exist_ok=True)
    _save(d, {"name": name, "task": task_desc, "direction": direction, "timeout": timeout,
              "evaluator": "v1", "evaluators": {"v1": {"created": now(), "note": "initial"}}, "next_id": 1})
    print(f"experience bank {name} at {d} (evaluator v1, {direction}imize score)")


def _run_eval(st: Store, d: Path, cfg: dict, sol_dir: Path, ev: str) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"hyra-eb-{cfg['name']}-") as tmp:
        work = Path(tmp)
        shutil.copytree(sol_dir, work / "solution")
        shutil.copytree(d / "evaluators" / ev, work / "evaluator")
        cmd = list(st.config.get("eb_sandbox_prefix") or []) + ["bash", str(work / "evaluator" / "eval.sh"), str(work / "solution")]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=cfg["timeout"])
            out, code = p.stdout + ("\n[stderr]\n" + p.stderr if p.stderr else ""), p.returncode
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            out += f"\n[timeout after {cfg['timeout']}s]"
            code = -9
        dur = round(time.time() - t0, 2)
    scores = SCORE_RE.findall(out)
    score = float(scores[-1]) if scores and code == 0 else None
    metrics = {k: v for k, v in METRIC_RE.findall(out)}
    return {"score": score, "exit": code, "seconds": dur, "metrics": metrics, "log": out}


def eb_submit(st: Store, name: str, solution: str, parents: list[str], note: str) -> None:
    d = _root(st, name)
    cfg = _cfg(d)
    src = Path(solution).expanduser().resolve()
    if not (src / "solve.sh").exists():
        raise HyraError("a solution is a directory with solve.sh as its entry point")
    sid = f"s{cfg['next_id']:04d}"
    cfg["next_id"] += 1
    _save(d, cfg)
    dest = d / "solutions" / sid
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    r = _run_eval(st, d, cfg, dest, cfg["evaluator"])
    (dest / "_eval.log").write_text(r.pop("log"))
    rec = {"id": sid, "evaluator": cfg["evaluator"], "parents": parents, "note": note, "author": me(),
           "created": now(), **r}
    with open(d / "results.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    best = _best(d, cfg)
    status = "INVALID" if r["score"] is None else f"score {r['score']:g}"
    is_best = best and best["id"] == sid
    print(f"{sid}: {status} (exit {r['exit']}, {r['seconds']}s) under {cfg['evaluator']}"
          + ("  ** new best **" if is_best else f"  (best: {best['id']} {best['score']:g})" if best else ""))
    print(f"log: {dest/'_eval.log'}")


def _best(d: Path, cfg: dict, ev: Optional[str] = None) -> Optional[dict]:
    better = _better(cfg)
    best = None
    for r in _results(d):
        if r["evaluator"] != (ev or cfg["evaluator"]) or r["score"] is None:
            continue
        if best is None or better(r["score"], best["score"]):
            best = r
    return best


def eb_top(st: Store, name: str, n: int, all_evals: bool) -> None:
    d = _root(st, name)
    cfg = _cfg(d)
    rows = [r for r in _results(d) if r["score"] is not None and (all_evals or r["evaluator"] == cfg["evaluator"])]
    rows.sort(key=lambda r: r["score"], reverse=cfg["direction"] == "max")
    total = len(_results(d))
    print(f"# {name}: {cfg['task']}\nevaluator {cfg['evaluator']}, {cfg['direction']}imize; {total} submissions")
    for r in rows[:n]:
        par = f" <- {','.join(r['parents'])}" if r["parents"] else ""
        print(f"  {r['id']} [{r['evaluator']}] {r['score']:g}  {r['seconds']}s{par}  {r['note'][:80]}")


def eb_inspire(st: Store, name: str, k: int) -> None:
    """Context-Agent helper: a diverse bundle of strong solutions, recent failures, and lineage."""
    d = _root(st, name)
    cfg = _cfg(d)
    res = _results(d)
    cur = [r for r in res if r["evaluator"] == cfg["evaluator"]]
    good = sorted([r for r in cur if r["score"] is not None], key=lambda r: r["score"], reverse=cfg["direction"] == "max")
    bad = [r for r in cur if r["score"] is None][-3:]
    picked, seen_parents = [], set()
    for r in good:  # prefer distinct lineages for diversity
        root = r["parents"][0] if r["parents"] else r["id"]
        if root in seen_parents and len(picked) < k // 2:
            continue
        seen_parents.add(root)
        picked.append(r)
        if len(picked) >= k:
            break
    print(f"# Inspiration bundle for '{name}'\nTask: {cfg['task']}\nEvaluator: {cfg['evaluator']} "
          f"({d/'evaluators'/cfg['evaluator']}), {cfg['direction']}imize\n")
    print("## Strong, diverse solutions (read their code; combine or mutate ideas)")
    for r in picked:
        print(f"- {r['id']} score {r['score']:g}: {d/'solutions'/r['id']}  note: {r['note']}  metrics: {r['metrics']}")
    if bad:
        print("\n## Recent invalid submissions (read logs to avoid repeating the failure)")
        for r in bad:
            print(f"- {r['id']} exit {r['exit']}: {d/'solutions'/r['id']/'_eval.log'}  note: {r['note']}")
    if len(good) >= 4:
        last = [r["score"] for r in cur[-6:] if r["score"] is not None]
        best = good[0]["score"]
        if last and best not in last:
            print("\n## Signal: the last submissions did not improve the best. Consider a different direction, "
                  "or (if the evaluator is the bottleneck) upgrading it — see `hyra eb evaluator`.")


def eb_evaluator(st: Store, name: str, new: str, note: str) -> None:
    d = _root(st, name)
    cfg = _cfg(d)
    src = Path(new).expanduser().resolve()
    ver = f"v{len(cfg['evaluators']) + 1}"
    dest = d / "evaluators" / ver
    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        dest.mkdir(parents=True)
        shutil.copy2(src, dest / "eval.sh")
    if not (dest / "eval.sh").exists():
        raise HyraError("evaluator dir must contain eval.sh")
    cfg["evaluators"][ver] = {"created": now(), "note": note}
    cfg["evaluator"] = ver
    _save(d, cfg)
    print(f"evaluator {ver} is now current ({note}). Rescore strong solutions: `hyra eb rescore {name} --top 5`")


def eb_rescore(st: Store, name: str, top: int) -> None:
    d = _root(st, name)
    cfg = _cfg(d)
    prev = [r for r in _results(d) if r["score"] is not None and r["evaluator"] != cfg["evaluator"]]
    prev.sort(key=lambda r: r["score"], reverse=cfg["direction"] == "max")
    done = {r["id"] for r in _results(d) if r["evaluator"] == cfg["evaluator"]}
    for r in [p for p in prev if p["id"] not in done][:top]:
        res = _run_eval(st, d, cfg, d / "solutions" / r["id"], cfg["evaluator"])
        (d / "solutions" / r["id"] / f"_eval_{cfg['evaluator']}.log").write_text(res.pop("log"))
        rec = {"id": r["id"], "evaluator": cfg["evaluator"], "parents": r["parents"], "note": f"rescore: {r['note']}",
               "author": me(), "created": now(), **res}
        with open(d / "results.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        s = "INVALID" if res["score"] is None else f"{res['score']:g}"
        print(f"{r['id']}: {r['evaluator']} {r['score']:g} -> {cfg['evaluator']} {s}")
