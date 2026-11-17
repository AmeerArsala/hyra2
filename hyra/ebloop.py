"""`hyra eb loop`: the Hyra 1.0 loop as an autonomous pipeline.

  Context Agent --(inspirations)--> queue --(take)--> Proposal Agents --(submit)--> isolated scoring --> Experience Bank
        ^                                                                                                    |
        +---------------------------------------- reads results, stocks the queue ------------------------------+

An asynchronous producer-consumer pipeline: a Context Agent session keeps the inspiration queue stocked, up to
`proposers` Proposal Agent sessions (the semaphore) drain it, and every submission is scored in a sandbox. It runs until
a budget (submissions / minutes), a target score, stagnation, or a STOP file ends it. Bilevel: when the inner loop ends,
an Evaluator Agent may upgrade the evaluator and the loop starts again under it. For a task with no evaluator, an Evaluator
Agent first writes one from the task description. All agents are headless sessions of any harness (`--harness`).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from . import eb
from .runner import Runner
from .store import HyraError, Store, now

CONTEXT_PROMPT = """You are the Hyra Context Agent for Experience Bank '{name}' in the Hyra project at {root}.
Task being optimized: {task}
Currently {queued} inspiration(s) are waiting. Queue {want} new, diverse ones now, then exit. Begin with
`hyra eb top {name}` and `hyra eb inspire {name}`."""

PROPOSER_PROMPT = """You are a Hyra Proposal Agent for Experience Bank '{name}' in the Hyra project at {root}.
Task being optimized: {task}
Take ONE inspiration (`hyra eb inspiration take {name}`), write and submit one better solution, then exit."""

EVAL_UPGRADE_PROMPT = """You are the Hyra Evaluator Agent (Mode B: upgrade) for Experience Bank '{name}' in the Hyra project at {root}.
Task being optimized: {task}
The inner loop just ended ({why}). Inspect the bank and decide whether the evaluator should change; follow your role protocol."""

EVAL_CREATE_PROMPT = """You are the Hyra Evaluator Agent (Mode A: create the initial evaluator) in the Hyra project at {root}.
Task to be optimized: {task}
Direction: {direction} the score. Write the evaluator to {path} (a directory containing eval.sh), test it, then exit."""


class EbLoop:
    def __init__(self, st: Store, name: str, *, proposers: int = 3, max_submissions: int = 30, minutes: float = 60,
                 target: Optional[float] = None, patience: int = 0, queue_low: int = 2, per_round: int = 4,
                 harness: Optional[str] = None, model: Optional[str] = None, rounds: int = 1,
                 poll: float = 3.0, dry: bool = False):
        self.st, self.name, self.dry = st, name, dry
        self.proposers, self.max_sub, self.minutes = proposers, max_submissions, minutes
        self.target, self.patience, self.queue_low, self.per_round = target, patience, queue_low, per_round
        self.rounds, self.poll = rounds, poll
        self.runner = Runner(st, None, dry, harness, model)
        self.live: dict[int, str] = {}  # pid -> role of the loop's own sessions
        self.root = st.root

    # ---------------------------------------------------------------- helpers
    def _bank(self):
        d = eb._root(self.st, self.name)
        return d, eb._cfg(d)

    def _results(self, d, cfg):
        return [r for r in eb._results(d) if r["evaluator"] == cfg["evaluator"]]

    def _spawn(self, role: str, header: str) -> None:
        p = self.runner._launch(f"eb:{self.name}:{role}", role, header, None, None, None, None,
                                extra_env={"HYRA_EB": self.name})
        if p:
            self.live[p.pid] = role

    def _alive(self, role: Optional[str] = None) -> int:
        self.runner.reap()
        dead = [pid for pid in self.live if pid not in self.runner.procs]
        for pid in dead:
            del self.live[pid]
        return sum(1 for r in self.live.values() if role is None or r == role)

    def _met(self, score: float, cfg: dict) -> bool:
        return self.target is not None and (score >= self.target if cfg["direction"] == "max" else score <= self.target)

    # ---------------------------------------------------------------- the inner loop
    def inner(self) -> str:
        d, cfg = self._bank()
        base = len(self._results(d, cfg))
        started = time.time()
        idle = 0
        while True:
            d, cfg = self._bank()
            res = self._results(d, cfg)
            new = len(res) - base
            best = eb._best(d, cfg)
            queued, proposing, context = eb.queue_count(d), self._alive("proposer"), self._alive("context")
            if best and self._met(best["score"], cfg):
                why = f"target reached ({best['id']} = {best['score']:g})"
            elif new >= self.max_sub:
                why = f"submission budget spent ({new}/{self.max_sub})"
            elif (time.time() - started) / 60 >= self.minutes:
                why = f"time budget spent ({self.minutes:g} min)"
            elif (d / "STOP").exists():
                why = "stopped (STOP file)"
            elif self.patience and self._since_improvement(res, cfg) >= self.patience:
                why = f"stagnated ({self.patience} submissions without improvement)"
            else:
                why = ""
            if why:
                self._drain()
                return why
            if self.dry:
                self._plan(queued)
                return "dry run"
            # producer: keep the queue stocked
            if queued < self.queue_low and not context:
                self._spawn("context", CONTEXT_PROMPT.format(name=self.name, root=self.root, task=cfg["task"],
                                                             queued=queued, want=self.per_round))
            # consumers, capped by the semaphore and by what the budget still allows
            room = self.proposers - proposing
            affordable = self.max_sub - new - proposing
            for _ in range(max(0, min(room, queued, affordable))):
                self._spawn("proposer", PROPOSER_PROMPT.format(name=self.name, root=self.root, task=cfg["task"]))
                queued -= 1
            if not self.live and eb.queue_count(d) == 0:
                idle += 1
                if idle >= 3:
                    self._drain()
                    return "no inspirations were produced (Context Agent made none)"
            else:
                idle = 0
            time.sleep(self.poll)

    def _since_improvement(self, res: list, cfg: dict) -> int:
        better = eb._better(cfg)
        best, since = None, 0
        for r in res:
            if r["score"] is not None and (best is None or better(r["score"], best)):
                best, since = r["score"], 0
            else:
                since += 1
        return since

    def _plan(self, queued: int) -> None:
        d, cfg = self._bank()
        print(f"[dry-run] bank {self.name}: {len(self._results(d, cfg))} submissions, {queued} queued; would run "
              f"1 Context Agent (queue < {self.queue_low}) and up to {self.proposers} Proposal Agents; budget "
              f"{self.max_sub} submissions / {self.minutes:g} min" + (f" / target {self.target:g}" if self.target is not None else ""))
        self._spawn("context", CONTEXT_PROMPT.format(name=self.name, root=self.root, task=cfg["task"], queued=queued, want=self.per_round))
        self._spawn("proposer", PROPOSER_PROMPT.format(name=self.name, root=self.root, task=cfg["task"]))

    def _drain(self) -> None:
        """Budget reached: let in-flight Proposal Agents finish (their submissions still count), stop the rest."""
        deadline = time.time() + float(self.st.config.get("eb_drain_seconds", 300))
        while self._alive() and time.time() < deadline and not self.dry:
            time.sleep(self.poll)

    # ---------------------------------------------------------------- bilevel + creation
    def create_evaluator(self, task: str, direction: str, timeout: int) -> None:
        d = self.st.dir / "eb" / f"{self.name}.pending"
        d.mkdir(parents=True, exist_ok=True)
        self._spawn("evaluator", EVAL_CREATE_PROMPT.format(root=self.root, task=task, direction=direction, path=d))
        while self._alive():
            time.sleep(self.poll)
        if not (d / "eval.sh").exists():
            raise HyraError(f"the Evaluator Agent did not write {d / 'eval.sh'}")
        eb.eb_init(self.st, self.name, str(d), task, direction, timeout)

    def run(self) -> None:
        for rnd in range(1, self.rounds + 1):
            d, cfg = self._bank()
            print(f"{now()} round {rnd}/{self.rounds}: evaluator {cfg['evaluator']}")
            why = self.inner()
            print(f"{now()} inner loop ended: {why}")
            eb.eb_status(self.st, self.name)
            if self.dry or rnd == self.rounds or why.startswith(("target", "stopped")):
                break
            before = cfg["evaluator"]
            self._spawn("evaluator", EVAL_UPGRADE_PROMPT.format(name=self.name, root=self.root, task=cfg["task"], why=why))
            while self._alive():
                time.sleep(self.poll)
            if eb._cfg(d)["evaluator"] == before:
                print(f"{now()} the Evaluator Agent left the evaluator unchanged; stopping")
                break
        d, cfg = self._bank()
        best = eb._best(d, cfg)
        print(f"best under {cfg['evaluator']}: " + (f"{best['id']} score {best['score']:g} -> {d / 'solutions' / best['id']}" if best else "none"))
