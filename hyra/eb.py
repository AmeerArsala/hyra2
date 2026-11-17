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
import os
import re
import shutil
import subprocess
import tempfile
import sys
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


def sandbox_command(st: Store, work: Path) -> tuple[str, list[str]]:
    """How to isolate one evaluation. A fresh scratch copy is always used; on top of that (config `eb_sandbox`):
      auto (default): macOS sandbox-exec (no network, no writes outside the scratch dir, project .hyra unreadable) or
                      Linux bubblewrap (read-only root, no network, project .hyra hidden); otherwise falls back to
                      'none' with a warning;
      podman | docker: a throwaway container: no network, read-only root, all capabilities dropped, only the scratch dir
                      writable, image from `eb_sandbox_image` (must contain bash; never pulled implicitly by `auto`);
      none: scratch copy only.
    `eb_sandbox_prefix` (a command list) overrides everything."""
    custom = st.config.get("eb_sandbox_prefix")
    if custom:
        return "custom", list(custom)
    mode = st.config.get("eb_sandbox", "auto")
    w = str(work.resolve())
    if mode in ("auto", "sandbox-exec") and sys.platform == "darwin" and shutil.which("sandbox-exec"):
        profile = ('(version 1)(allow default)(deny network*)(deny file-write*)'
                   f'(allow file-write* (subpath "{w}") (subpath "/dev"))'
                   f'(deny file-read* (subpath "{st.dir.resolve()}"))')
        return "sandbox-exec", ["sandbox-exec", "-p", profile]
    if mode in ("auto", "bwrap") and sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap", ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", str(st.dir.resolve()),
                         "--bind", w, w, "--unshare-net", "--unshare-pid", "--unshare-ipc", "--new-session",
                         "--die-with-parent", "--chdir", w]
    if mode in ("podman", "docker") and shutil.which(mode):
        image = st.config.get("eb_sandbox_image") or st.config.get("eb_docker_image") or "python:3.12-slim"
        userns = ["--userns=keep-id"] if mode == "podman" else []  # rootless podman: files stay owned by the caller
        return mode, [mode, "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
                      "--security-opt", "no-new-privileges", "--security-opt", "label=disable", *userns,
                      "-v", f"{w}:{w}", "-w", w, "-e", f"HOME={w}", "-e", f"TMPDIR={w}", "-e", "PYTHONDONTWRITEBYTECODE=1", image]
    return "none", []


_WARNED: set[str] = set()


def _run_eval(st: Store, d: Path, cfg: dict, sol_dir: Path, ev: str) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"hyra-eb-{cfg['name']}-") as tmp:
        work = Path(tmp).resolve()
        shutil.copytree(sol_dir, work / "solution")
        shutil.copytree(d / "evaluators" / ev, work / "evaluator")
        mode, prefix = sandbox_command(st, work)
        if mode == "none" and "none" not in _WARNED:
            _WARNED.add("none")
            print("warning: evaluation runs in a scratch copy only (no network/filesystem sandbox available; "
                  "set eb_sandbox to podman or docker, or eb_sandbox_prefix)", file=sys.stderr)
        # Solutions are untrusted code: no API keys or credentials from the parent environment.
        if mode in ("podman", "docker"):
            # The engine process needs the caller's HOME / XDG_RUNTIME_DIR / DOCKER_HOST to find its (rootless) storage and
            # socket; found live on Linux: stripping them made rootless podman fall back to root-only paths and fail. The
            # evaluator inside the container only sees the explicit -e flags in the command, so no credentials leak.
            env = dict(os.environ)
        else:
            keep = ["PATH", "LANG", "LC_ALL", "TERM", *st.config.get("eb_env_passthrough", [])]
            env = {k: os.environ[k] for k in keep if k in os.environ}
            env.update(HOME=str(work), TMPDIR=str(work), PYTHONDONTWRITEBYTECODE="1")
        cmd = prefix + ["bash", str(work / "evaluator" / "eval.sh"), str(work / "solution")]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=cfg["timeout"], env=env)
            out, code = p.stdout + ("\n[stderr]\n" + p.stderr if p.stderr else ""), p.returncode
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            out += f"\n[timeout after {cfg['timeout']}s]"
            code = -9
        dur = round(time.time() - t0, 2)
    scores = SCORE_RE.findall(out)
    score = float(scores[-1]) if scores and code == 0 else None
    metrics = {k: v for k, v in METRIC_RE.findall(out)}
    return {"score": score, "exit": code, "seconds": dur, "metrics": metrics, "sandbox": mode, "log": out}


def _alloc_solution(d: Path) -> tuple[str, Path]:
    """Claim the next solution id atomically (exclusive mkdir), so parallel Proposal Agents never collide."""
    sols = d / "solutions"
    sols.mkdir(exist_ok=True)
    n = len(list(sols.iterdir())) + 1
    while True:
        sid = f"s{n:04d}"
        try:
            (sols / sid).mkdir()
            return sid, sols / sid
        except FileExistsError:
            n += 1


def _append_result(d: Path, rec: dict) -> None:
    import fcntl
    with open(d / "results.jsonl", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # concurrent submitters must not interleave records
        f.write(json.dumps(rec) + "\n")
        f.flush()


def eb_submit(st: Store, name: str, solution: str, parents: list[str], note: str) -> None:
    d = _root(st, name)
    cfg = _cfg(d)
    src = Path(solution).expanduser().resolve()
    if not (src / "solve.sh").exists():
        raise HyraError("a solution is a directory with solve.sh as its entry point")
    sid, dest = _alloc_solution(d)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"), dirs_exist_ok=True)
    r = _run_eval(st, d, cfg, dest, cfg["evaluator"])
    (dest / "_eval.log").write_text(r.pop("log"))
    rec = {"id": sid, "evaluator": cfg["evaluator"], "parents": parents, "note": note, "author": me(),
           "created": now(), **r}
    _append_result(d, rec)
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
        _append_result(d, rec)
        s = "INVALID" if res["score"] is None else f"{res['score']:g}"
        print(f"{r['id']}: {r['evaluator']} {r['score']:g} -> {cfg['evaluator']} {s}")


# ---------------------------------------------------------------- inspiration queue (Context Agent -> Proposal Agents)
def _qdirs(d: Path) -> tuple[Path, Path]:
    q, c = d / "queue", d / "claimed"
    q.mkdir(exist_ok=True)
    c.mkdir(exist_ok=True)
    return q, c


def queue_count(d: Path) -> int:
    return len(list(_qdirs(d)[0].glob("insp-*.json")))


def insp_add(st: Store, name: str, text: str, refs: list[str], files: list[str]) -> None:
    """A Context Agent adds an inspiration: a self-contained bundle of context (text + solutions + files)."""
    d = _root(st, name)
    q, c = _qdirs(d)
    known = {r["id"] for r in _results(d)}
    bad = [r for r in refs if r not in known]
    if bad:
        raise HyraError(f"unknown solution id(s): {', '.join(bad)}")
    for _ in range(50):  # unique id even with concurrent writers
        n = len(list(q.glob("insp-*.json"))) + len(list(c.glob("insp-*.json"))) + 1
        path = q / f"insp-{n:04d}.json"
        try:
            with open(path, "x") as f:
                json.dump({"id": path.stem, "created": now(), "author": me(), "text": text, "refs": refs,
                           "files": [str(Path(x).expanduser().resolve()) for x in files]}, f, indent=2)
            print(f"queued {path.stem} ({queue_count(d)} waiting)")
            return
        except FileExistsError:
            continue
    raise HyraError("could not allocate an inspiration id")


def insp_take(st: Store, name: str) -> None:
    """A Proposal Agent atomically claims one inspiration (rename is atomic: no two agents get the same one)."""
    d = _root(st, name)
    cfg = _cfg(d)
    q, c = _qdirs(d)
    for f in sorted(q.glob("insp-*.json")):
        try:
            os.rename(f, c / f.name)
        except OSError:
            continue  # someone else took it
        b = json.loads((c / f.name).read_text())
        by_id = {r["id"]: r for r in _results(d) if r["evaluator"] == cfg["evaluator"]}
        print(f"# Inspiration {b['id']} for bank '{name}'\n\nTask: {cfg['task']}\n"
              f"Evaluator ({cfg['evaluator']}, {cfg['direction']}imize): {d / 'evaluators' / cfg['evaluator'] / 'eval.sh'}\n\n{b['text']}\n")
        for sid in b["refs"]:
            r = by_id.get(sid)
            sc = "invalid" if not r or r["score"] is None else f"{r['score']:g}"
            print(f"- {sid}: score {sc}, code {d / 'solutions' / sid}, log {d / 'solutions' / sid / '_eval.log'}"
                  + (f", note: {r['note']}" if r else ""))
        for x in b["files"]:
            print(f"- file: {x}")
        print(f"\nWrite a solution dir with solve.sh, then: hyra eb submit {name} <dir> --parent <sid> --note \"...\"")
        return
    print("(queue empty)")


def insp_list(st: Store, name: str, show_all: bool) -> None:
    d = _root(st, name)
    q, c = _qdirs(d)
    for label, folder in (("queued", q), ("claimed", c)):
        if label == "claimed" and not show_all:
            continue
        for f in sorted(folder.glob("insp-*.json")):
            b = json.loads(f.read_text())
            print(f"{b['id']} [{label}] {b['text'][:110].replace(chr(10), ' ')}")


def eb_status(st: Store, name: str) -> None:
    d = _root(st, name)
    cfg = _cfg(d)
    res = [r for r in _results(d) if r["evaluator"] == cfg["evaluator"]]
    best = _best(d, cfg)
    print(f"{name}: evaluator {cfg['evaluator']} ({cfg['direction']}imize); {len(res)} submissions under it "
          f"({len(_results(d))} total); best {best['id'] + ' ' + format(best['score'], 'g') if best else 'none'}; "
          f"queue {queue_count(d)} waiting")
