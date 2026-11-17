"""End-to-end test of the hyra CLI in a temp project. Run: python3 -m unittest discover -s tests"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import textwrap
import unittest
from pathlib import Path

HYRA = str(Path(__file__).resolve().parent.parent / "skills" / "hyra" / "scripts" / "hyra")


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()
        self.env = dict(os.environ, HYRA_LIBRARY=str(Path(self.tmp.name) / "lib"),
                        HYRA_SKILL_DIRS=os.pathsep.join(str(Path(self.tmp.name) / d) for d in ("agents-skills", "claude-skills")))
        self.env["CLAUDE_CONFIG_DIR"] = str(Path(self.tmp.name) / "claude-home")  # no hyra agents installed
        fake = Path(self.tmp.name) / "fakehome"  # tests must never see (or touch) the real home's installed agents
        fake.mkdir()
        self.env.update(HOME=str(fake), XDG_CONFIG_HOME=str(fake / ".config"), KIMI_CODE_HOME=str(fake / ".kimi-code"),
                        HYRA_TAILSCALE_BIN="")  # tests never look at (or probe) the real tailnet
        for k in ("HYRA_AGENT", "HYRA_ROLE", "HYRA_TASK", "HYRA_PROJECT"):
            self.env.pop(k, None)

    def tearDown(self):
        for fn in reversed(getattr(self, "pre_cleanup", [])):  # things that need the temp dirs to still exist (stop servers, ...)
            try:
                fn()
            except Exception:
                pass
        self.tmp.cleanup()

    def h(self, *args, ok=True, env=None):
        p = subprocess.run([sys.executable, HYRA, *args], cwd=self.root, capture_output=True, text=True,
                           env=dict(self.env, **(env or {})))
        if ok is None:
            return p.stdout + p.stderr
        if ok and p.returncode != 0:
            self.fail(f"hyra {' '.join(args)} failed:\n{p.stdout}\n{p.stderr}")
        if not ok:
            self.assertNotEqual(p.returncode, 0, f"expected failure: hyra {' '.join(args)}\n{p.stdout}")
        return p.stdout + p.stderr

    def test_full_lifecycle(self):
        self.h("init", "--goal", "test goal")
        cfg = json.loads((self.root / ".hyra/config.json").read_text())
        cfg["resources"] = {"workers": 2, "gpu": 2}
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.h("mission", "new", "m1", "--title", "Find x", "--problem", "Make x small. Success: x<1.")

        # queue: dependency + approval gating + resource limits
        self.assertIn("T1 queued", self.h("task", "add", "--mission", "m1", "--title", "baseline", "--spec", "run baseline"))
        self.assertIn("pending approval A1", self.h("task", "add", "--mission", "m1", "--title", "big run",
                                                    "--spec", "train", "--resource", "gpu=2", "--after", "1"))
        out = self.h("task", "next", "--worker", "w1")
        self.assertIn("claimed T1", out)
        self.assertIn("nothing ready", self.h("task", "next", "--worker", "w2"))  # T2 awaits approval + dep

        # workers cannot grant approvals
        self.h("approval", "grant", "1", ok=False, env={"HYRA_ROLE": "worker"})

        # completion requires evidence
        self.h("task", "complete", "1", "--summary", "x=3", ok=False)
        self.h("task", "complete", "1", "--summary", "x=3", "--no-evidence", ok=False)  # experiments can't opt out
        res = self.root / "result.json"
        res.write_text('{"x": 3}')
        self.h("task", "complete", "1", "--summary", "baseline x=3", "--evidence", str(res))
        self.assertTrue(any((self.root / ".hyra/evidence/1").iterdir()))

        self.h("approval", "grant", "1", "--by", "human:test")
        self.assertIn("claimed T2", self.h("task", "next", "--worker", "w1"))
        self.h("task", "sleep", "2", "--until", "+1h", "--reason", "waiting for training")
        self.assertIn("nothing ready", self.h("task", "next"))

        # blind review
        self.h("review", "request", "1")
        rid = "3"
        self.h("review", "claim", rid, ok=False)  # blind phase blocks the claim
        # Found by a live Codex reviewer: the worker's claim also sits on the Research Board (finding entry).
        # A blind reviewer's briefing, board reads and mission view must not leak it.
        blind = {"HYRA_TASK": rid, "HYRA_ROLE": "reviewer"}
        for cmd in (("brief", f"task:{rid}"), ("board", "read"), ("mission", "show", "m1"), ("status",)):
            self.assertNotIn("baseline x=3", self.h(*cmd, env=blind), cmd)
        self.assertIn("baseline x=3", self.h("board", "read"))  # everyone else still sees it
        ev = self.h("review", "evidence", rid)
        self.assertIn("result.json", ev)
        self.assertNotIn("baseline x=3", ev)
        shown = self.h("task", "show", "1", env={"HYRA_TASK": rid})
        self.assertNotIn("baseline x=3", shown)
        self.h("review", "blind", rid, "--notes", "reran: x=3 confirmed, but only on seed 0")
        self.assertIn("baseline x=3", self.h("review", "claim", rid))
        self.assertIn("baseline x=3", self.h("board", "read", env=blind))  # revealed after the blind assessment
        self.h("review", "verdict", rid, "--verdict", "narrower", "--notes", "holds for seed 0 only")
        self.h("review", "verdict", rid, "--verdict", "supported", "--notes", "x", ok=False)  # immutable
        self.assertIn("review=narrower", self.h("mission", "show", "m1"))
        self.assertIn("RESPONSE REQUIRED", self.h("brief", "mission:m1"))

        # board + messages
        self.h("board", "post", "--type", "idea", "--mission", "m1", "try lower lr")
        self.h("msg", "send", "--to", "overall", "--subject", "need data", "--body", "pls", "--requires-response")
        self.assertIn("need data", self.h("status"))
        self.assertIn("try lower lr", self.h("board", "read", "--mission", "m1"))

        # mission close blocked while tasks open
        self.h("mission", "close", "m1", "--conclusion", "done", ok=False)
        self.h("task", "cancel", "2", "--reason", "superseded")
        self.h("mission", "close", "m1", "--conclusion", "x=3 baseline (seed 0)", ok=False)  # the narrower review queued a check task
        self.assertIn("Add an automated check for the flaw found reviewing T1", self.h("task", "list"))
        self.h("task", "cancel", "4", "--reason", "one-off flaw")
        self.h("mission", "close", "m1", "--conclusion", "x=3 baseline (seed 0)")

        # books: tiers, evidence, independence, approval-gated convention and sync
        self.h("books", "add", "--title", "Warmup fixes divergence", "--method", "m", "--rationale", "r",
               "--applies-when", "a", ok=False)  # needs evidence
        self.h("books", "add", "--title", "Warmup fixes divergence", "--method", "use 1k warmup steps",
               "--rationale", "loss spikes at step 200", "--applies-when", "AdamW, lr>1e-3", "--evidence", "T1")
        self.h("books", "promote", "warmup-fixes-divergence", "--to", "verified", "--evidence", "T1", ok=False)
        self.h("books", "promote", "warmup-fixes-divergence", "--to", "verified", "--evidence", "T9")
        self.assertIn("[verified]", self.h("books", "search", "warmup"))
        out = self.h("books", "sync", "warmup-fixes-divergence")
        self.assertIn("approval A", out)
        aid = out.split("approval A")[1].split()[0]
        self.h("approval", "grant", aid)
        self.h("books", "sync", "warmup-fixes-divergence")
        self.assertTrue((Path(self.env["HYRA_LIBRARY"]) / "books/warmup-fixes-divergence.md").exists())

        # skills
        self.h("skill", "new", "lr-warmup", "--description", "Fix early divergence", "--from-books", "warmup-fixes-divergence")
        self.h("skill", "publish", "lr-warmup", ok=False)  # template not filled in
        sk = self.root / ".hyra/skills/lr-warmup/SKILL.md"
        sk.write_text(sk.read_text().replace("\n1.\n", "\n1. Add 1k warmup steps.\n"))
        out = self.h("skill", "publish", "lr-warmup")
        aid = out.split("approval A")[1].split()[0]
        self.h("approval", "grant", aid)
        self.h("skill", "publish", "lr-warmup")
        for d in ("agents-skills", "claude-skills"):  # linked into every harness dir
            self.assertTrue((Path(self.tmp.name) / d / "lr-warmup/SKILL.md").exists())

    def test_experience_bank(self):
        self.h("init")
        ev = self.root / "eval.sh"
        ev.write_text(textwrap.dedent("""\
            set -e
            out=$(bash "$1/solve.sh")
            echo "METRIC raw: $out"
            echo "SCORE: $out"
        """))
        self.h("eb", "init", "toy", "--evaluator", str(ev), "--task", "maximize the number", "--direction", "max")
        for i, val in enumerate(["3", "7", "oops"]):
            d = self.root / f"sol{i}"
            d.mkdir()
            (d / "solve.sh").write_text(f"echo {val}\n" if val != "oops" else "exit 3\n")
            out = self.h("eb", "submit", "toy", str(d), "--note", f"try {val}")
        self.assertIn("INVALID", out)
        top = self.h("eb", "top", "toy")
        self.assertLess(top.index("s0002"), top.index("s0001"))
        self.assertIn("_eval.log", self.h("eb", "inspire", "toy"))
        ev2 = self.root / "eval2.sh"
        ev2.write_text('out=$(bash "$1/solve.sh"); echo "SCORE: $((out * 10))"\n')
        self.h("eb", "evaluator", "toy", "--new", str(ev2), "--note", "scaled")
        self.assertIn("v2 70", self.h("eb", "rescore", "toy", "--top", "5"))

    def test_runner_dry_run(self):
        self.h("init")
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("task", "add", "--mission", "m1", "--title", "a", "--spec", "s", "--model", "sonnet")
        self.h("task", "add", "--mission", "m1", "--title", "b", "--spec", "s", "--harness", "codex")
        self.h("task", "add", "--mission", "m1", "--title", "c", "--spec", "s", "--harness", "nope", ok=False)
        out = self.h("run", "--dry-run")
        self.assertIn("would launch mission:m1 via claude", out)
        self.assertIn("would launch task:1 via claude: claude -p --permission-mode auto --model sonnet", out)
        self.assertIn("would launch task:2 via codex: codex exec", out)
        self.assertNotIn("-m '", out.split("task:2")[1])  # no model -> -m flag dropped
        self.assertIn("via agy: agy --mode auto", self.h("run", "--dry-run", "--task", "2", "--harness", "agy"))
        self.assertNotIn("--agent", out)  # Claude subagents not installed -> role embedded in prompt instead
        # With the Claude Code agent definitions installed, claude sessions load the role natively.
        agents = Path(self.env["CLAUDE_CONFIG_DIR"]) / "agents"
        agents.mkdir(parents=True)
        for r in ("mission", "worker", "reviewer"):
            (agents / f"hyra-{r}.md").write_text("---\nname: x\n---\n")
        out = self.h("run", "--dry-run")
        self.assertIn("claude -p --permission-mode auto --agent hyra-worker --model sonnet", out)
        self.assertIn("--agent hyra-mission", out)
        self.assertNotIn("--agent", out.split("task:2")[1])  # codex has no native agents
        # kimi/agy/opencode/omp presets (exact flags per their docs); native --agent only when the agent file exists
        for h, needle in (("kimi", "kimi --yolo -p"), ("agy", "agy --mode auto '-p="), ("opencode", "opencode run '"),
                          ("omp", "omp -p")):
            self.assertIn(needle, self.h("run", "--dry-run", "--task", "2", "--harness", h), h)
        fake_home = Path(self.env["HOME"])
        (fake_home / ".agents/agents").mkdir(parents=True)
        (fake_home / ".agents/agents/hyra-worker.md").write_text("---\nname: hyra-worker\n---\n")
        self.assertIn("kimi --yolo --agent hyra-worker -p", self.h("run", "--dry-run", "--task", "2", "--harness", "kimi"))
        self.assertIn("kimi --yolo --agent hyra-worker -m x -p",
                      self.h("run", "--dry-run", "--task", "2", "--harness", "kimi", "--model", "x"))
        self.assertIn("[queued]", self.h("task", "list"))  # dry run claims nothing

    def test_mission_wakes_when_a_task_finishes_even_with_others_in_flight(self):
        self.h("init")
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("task", "add", "--mission", "m1", "--title", "a", "--spec", "s")
        self.h("task", "add", "--mission", "m1", "--title", "b", "--spec", "s")
        self.h("brief", "mission:m1", env={"HYRA_ROLE": "mission"})  # the mission has looked; nothing new yet
        self.assertNotIn("would launch mission:m1", self.h("run", "--dry-run", "--max-workers", "0"))
        self.h("task", "next", "--worker", "w")
        f = self.root / "r.txt"; f.write_text("ok")
        import time; time.sleep(1.2)  # timestamps have 1s resolution
        self.h("task", "complete", "1", "--summary", "done", "--evidence", str(f))  # T2 is still queued (in flight)
        out = self.h("run", "--dry-run", "--max-workers", "0")
        self.assertIn("would launch mission:m1", out)

    def test_antigravity_agent_files_carry_an_explicit_tools_list(self):
        """Live finding: an Antigravity agent file with no tools list gets no shell and no file writes."""
        home, env = self._home()
        self.h("setup", "--toolkit", "global", env=env)
        agy = (home / ".gemini/config/agents/hyra-worker.md").read_text()
        self.assertIn("tools:\n  - view_file", agy)
        self.assertIn("  - run_command", agy)
        self.assertFalse((home / ".gemini/config/agents/hyra-worker.md").is_symlink())  # generated, not linked
        self.assertNotIn("tools:", (home / ".agents/agents/hyra-worker.md").read_text().split("\n---", 1)[0])  # Kimi: plain
        self.assertIn("tools:", agy.split("\n---", 1)[0])
        # a project-local agent file without tools doesn't count as native for agy (the run falls back to the embedded role)
        self.h("init")
        d = self.root / ".agents/agents"; d.mkdir(parents=True)
        (d / "hyra-worker.md").write_text("---\nname: hyra-worker\ndescription: x\n---\nbody\n")
        self.h("mission", "new", "m", "--title", "t", "--problem", "p")
        self.h("task", "add", "--mission", "m", "--title", "a", "--spec", "s")
        out = self.h("run", "--dry-run", "--task", "1", "--harness", "agy", env=env)
        self.assertNotIn("--agent", out)

    def test_user_level_config_applies_to_every_project(self):
        (Path(self.env["HOME"]) / ".hyra").mkdir()
        (Path(self.env["HOME"]) / ".hyra/config.json").write_text(json.dumps({"harness_models": {"agy": "gemini-4-hydrogen-medium"}}))
        self.h("init")
        cfg = json.loads((self.root / ".hyra/config.json").read_text())
        cfg["harness_models"] = {"opencode": "opencode/big-pickle"}  # project adds a key; the user-level one survives
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.h("mission", "new", "m", "--title", "t", "--problem", "p")
        self.h("task", "add", "--mission", "m", "--title", "a", "--spec", "s")
        self.assertIn("--model gemini-4-hydrogen-medium", self.h("run", "--dry-run", "--task", "1", "--harness", "agy"))
        self.assertIn("-m opencode/big-pickle", self.h("run", "--dry-run", "--task", "1", "--harness", "opencode"))

    def test_memory_is_hierarchical(self):
        """Spec: each agent keeps hierarchical memory (understanding, hypotheses, ruled-out, next steps)."""
        self.h("init")
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        idx = Path(self.h("memory", "path", "mission:m1").strip())
        self.assertEqual(idx.name, "index.md")
        self.assertEqual(idx.parent.name, "mission__m1")
        self.h("memory", "note", "hypotheses", "LR warmup", "--text", "Spikes at step 200. Falsifier: spike persists.",
               "--summary", "warmup fixes the spike", "--agent", "mission:m1")
        self.h("memory", "note", "ruled-out", "Bigger batch", "--text", "Did not help (T4).", "--agent", "mission:m1")
        self.h("memory", "note", "nonsense", "x", "--text", "y", ok=False)
        tree = self.h("memory", "tree", "mission:m1")
        self.assertIn("hypotheses/lr-warmup.md: LR warmup — warmup fixes the spike", tree)
        brief = self.h("brief", "mission:m1", env={"HYRA_ROLE": "mission"})
        self.assertIn("level 0", brief)
        self.assertIn("hypotheses/lr-warmup.md", brief)            # progressive disclosure: titles in the brief,
        self.assertNotIn("Falsifier: spike persists", brief)        # ...detail only on demand
        self.assertIn("Falsifier: spike persists", self.h("memory", "show", "mission:m1", "--node", "hypotheses/lr-warmup.md"))
        self.h("memory", "show", "mission:m1", "--node", "../../hyra.db", ok=False)  # can't escape the memory dir
        # a flat memory file from an older version migrates into the hierarchy
        legacy = self.root / ".hyra/memory/mission__old.md"
        legacy.write_text("# old memory\n")
        self.h("mission", "new", "old", "--title", "t", "--problem", "p")
        self.assertIn("# old memory", self.h("memory", "show", "mission:old"))
        self.assertFalse(legacy.exists())

    def test_closed_missions_get_a_pending_retrospective(self):
        self.h("init")
        self.h("mission", "new", "m1", "--title", "Fast fib", "--problem", "p")
        self.assertNotIn("Retrospectives pending", self.h("status"))
        self.h("mission", "close", "m1", "--conclusion", "done")
        for cmd in (("status",), ("brief", "overall")):
            self.assertIn("m1: Fast fib", self.h(*cmd))
        self.h("board", "post", "--type", "retro", "--mission", "m1", "Added 2 Books entries and 1 skill.")
        self.assertNotIn("Retrospectives pending", self.h("status"))

    def test_event_driven_wake(self):
        """Spec: agents resume 'when an experiment finishes or a checkpoint is reached', not only on a clock."""
        self.h("init")
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("task", "add", "--mission", "m1", "--title", "train", "--spec", "s")
        self.h("task", "next", "--worker", "w")
        done = self.root / "job.done"
        self.h("task", "sleep", "1", "--reason", "training", ok=False)  # needs a time and/or a condition
        self.h("task", "sleep", "1", "--until", "+1d", "--until-file", str(done), "--until-cmd", "true", "--reason", "x", ok=False)
        self.h("task", "sleep", "1", "--until-file", str(done), "--reason", "waiting for the training job")
        self.assertIn("nothing ready", self.h("task", "next"))      # condition false: stays parked
        done.write_text("finished")                                  # the 'experiment' finishes
        self.assertIn("claimed T1", self.h("task", "next"))          # ...and it resumes, long before the deadline
        self.assertIn("wake condition met", self.h("task", "show", "1"))
        # missions too: a condition command, with the runner noticing it
        flag = self.root / "ckpt"
        self.h("mission", "wake", "m1", "--until-cmd", f"test -e {flag}", "--reason", "checkpoint 1000")
        self.h("brief", "mission:m1", env={"HYRA_ROLE": "mission"})
        self.assertNotIn("would launch mission:m1", self.h("run", "--dry-run", "--max-workers", "0"))
        flag.write_text("x")
        out = self.h("run", "--dry-run", "--max-workers", "0")
        self.assertIn("wake condition met for mission m1", out)
        self.assertIn("would launch mission:m1", out)

    FAKE_AGENTS = r'''
role="$HYRA_ROLE"; name="$HYRA_EB"
case "$role" in
  context)  for i in 1 2; do hyra eb inspiration add "$name" --text "try a bigger number ($i)"; done ;;
  proposer) out=$(hyra eb inspiration take "$name"); echo "$out" | grep -q "queue empty" && exit 0
            n=$(( $(ls .hyra/eb/$name/solutions | wc -l) * 10 + 5 )); d=$(mktemp -d)
            echo "echo $n" > "$d/solve.sh"; hyra eb submit "$name" "$d" --note "auto $n" ;;
  evaluator) path=$(printf '%s' "$0" | sed -n 's/.*Write the evaluator to \([^ ]*\) .*/\1/p')
            [ -n "$path" ] && printf 'out=$(bash "$1/solve.sh"); echo "SCORE: $out"\n' > "$path/eval.sh" ;;
esac
'''

    def _loop_project(self):
        self.h("init")
        cfg = json.loads((self.root / ".hyra/config.json").read_text())
        cfg.update(harness="fake", harness_cmds={"fake": ["bash", "-c", self.FAKE_AGENTS, "{prompt}"]}, eb_drain_seconds=30)
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))

    def test_eb_loop_autonomous_pipeline(self):
        """Hyra 1.0: Context Agent stocks a queue, parallel Proposal Agents drain it, sandboxed scoring, budget stops it."""
        self._loop_project()
        ev = self.root / "eval.sh"
        ev.write_text('out=$(bash "$1/solve.sh"); echo "SCORE: $out"\n')
        self.h("eb", "init", "toy", "--evaluator", str(ev), "--task", "maximize the number")
        out = self.h("eb", "loop", "toy", "--proposers", "3", "--max-submissions", "6", "--poll", "0.1")
        self.assertIn("submission budget spent", out)
        results = [json.loads(l) for l in (self.root / ".hyra/eb/toy/results.jsonl").read_text().splitlines()]
        self.assertGreaterEqual(len(results), 4)
        self.assertTrue(all(r["sandbox"] for r in results))
        self.assertEqual(len({r["id"] for r in results}), len(results))     # parallel agents never collide on ids
        self.assertIn("best under v1", out)
        # every agent session must succeed: a crashed Proposal Agent is a lost submission (this once happened when
        # parallel agents were handed the same solution id)
        for log in (self.root / ".hyra/logs").glob("eb_toy_*.log"):
            self.assertNotIn("Traceback", log.read_text(), log.name)
        # no inspiration is ever handed to two agents
        taken = list((self.root / ".hyra/eb/toy/claimed").glob("insp-*.json"))
        self.assertEqual(len(taken), len({p.name for p in taken}))
        # a second run stops at a target score
        out = self.h("eb", "loop", "toy", "--proposers", "1", "--max-submissions", "20", "--target", "10", "--poll", "0.2")
        self.assertIn("target reached", out)
        # STOP file and dry run
        (self.root / ".hyra/eb/toy/STOP").write_text("")
        self.assertIn("stopped", self.h("eb", "loop", "toy", "--poll", "0.2"))
        (self.root / ".hyra/eb/toy/STOP").unlink()
        self.assertIn("would launch eb:toy:context", self.h("eb", "loop", "toy", "--dry-run"))

    def test_eb_loop_creates_the_evaluator_from_the_task(self):
        self._loop_project()
        self.h("eb", "loop", "fresh", "--poll", "0.2", ok=False)  # no bank, not asked to create one
        out = self.h("eb", "loop", "fresh", "--create-evaluator", "--task", "maximize the number", "--max-submissions", "2",
                     "--proposers", "1", "--poll", "0.2")
        self.assertTrue((self.root / ".hyra/eb/fresh/evaluators/v1/eval.sh").exists())
        self.assertIn("submission budget spent", out)

    def test_eb_evaluation_is_sandboxed(self):
        if sys.platform != "darwin" and not shutil.which("bwrap"):
            self.skipTest("no sandbox-exec/bwrap on this machine")
        self.h("init")
        outside = Path(self.tmp.name) / "ESCAPED"
        ev = self.root / "eval.sh"
        ev.write_text(f'''echo hi > "{outside}" 2>/dev/null
echo "METRIC wrote_outside: $([ -e "{outside}" ] && echo yes || echo no)"
python3 -c "import socket;s=socket.socket();s.settimeout(2);s.connect(('1.1.1.1',53))" 2>/dev/null && echo "METRIC net: yes" || echo "METRIC net: no"
echo "METRIC secret: ${{HYRA_TEST_SECRET:-no}}"
echo "SCORE: 1"\n''')
        sol = self.root / "sol"; sol.mkdir(); (sol / "solve.sh").write_text("echo 1\n")
        self.h("eb", "init", "t", "--evaluator", str(ev), "--task", "probe")
        self.h("eb", "submit", "t", str(sol), env={"HYRA_TEST_SECRET": "leaked"})
        r = json.loads((self.root / ".hyra/eb/t/results.jsonl").read_text().splitlines()[-1])
        self.assertIn(r["sandbox"], ("sandbox-exec", "bwrap"))
        self.assertEqual(r["metrics"], {"wrote_outside": "no", "net": "no", "secret": "no"})
        self.assertFalse(outside.exists())

    def test_container_sandbox_commands_are_locked_down(self):
        """The Linux/container paths can't run on every machine, so check the exact command they would use."""
        sys.path.insert(0, str(Path(HYRA).resolve().parents[3]))
        from unittest import mock
        from hyra import eb
        from hyra.store import Store
        self.h("init")
        st = Store(self.root)
        with mock.patch.object(eb.shutil, "which", side_effect=lambda c: f"/usr/bin/{c}"):
            for engine in ("podman", "docker"):
                st.config.update(eb_sandbox=engine, eb_sandbox_image="img:1")
                mode, cmd = eb.sandbox_command(st, Path(self.tmp.name))
                self.assertEqual((mode, cmd[:2]), (engine, [engine, "run"]))
                for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "no-new-privileges"):
                    self.assertIn(flag, cmd)
                self.assertEqual(cmd[-1], "img:1")
                self.assertEqual("--userns=keep-id" in cmd, engine == "podman")
            with mock.patch.object(eb.sys, "platform", "linux"):
                st.config.update(eb_sandbox="auto")
                mode, cmd = eb.sandbox_command(st, Path(self.tmp.name))
                self.assertEqual(mode, "bwrap")
                for flag in ("--unshare-net", "--unshare-pid", "--ro-bind", "--die-with-parent"):
                    self.assertIn(flag, cmd)
                self.assertEqual(cmd[cmd.index("--tmpfs") + 1], str(st.dir.resolve()))  # project state hidden
            st.config.update(eb_sandbox="none")
            self.assertEqual(eb.sandbox_command(st, Path(self.tmp.name))[0], "none")

    def test_run_one_mission_in_its_own_session(self):
        self.h("init")
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.assertIn("would launch mission:m1", self.h("run", "--mission", "m1", "--dry-run"))
        self.h("run", "--mission", "nope", "--dry-run", ok=False)


    OVERALL_AGENTS = r'''
case "$HYRA_ROLE" in
  overseer)      hyra brief overall >/dev/null
                 if hyra approval grant 1 >/dev/null 2>&1; then echo granted > overseer_result; else echo refused > overseer_result; fi
                 hyra msg send --to human --subject "approval waiting" --body "A1 needs you; I recommend yes" ;;
  retrospective) hyra board post --type retro --mission "${HYRA_AGENT#retro:}" "retro done: 1 Books entry" ;;
esac
'''

    def _fake_cfg(self, **extra):
        self.h("init")
        cfg = json.loads((self.root / ".hyra/config.json").read_text())
        cfg.update(harness="fake", harness_cmds={"fake": ["bash", "-c", self.OVERALL_AGENTS, "{prompt}"]}, **extra)
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))

    def test_unattended_overall_wakes_only_when_needed_and_cannot_grant(self):
        """Spec: Overall manages priorities and talks to humans. In daemon mode nobody opens a session, so it runs itself."""
        self._fake_cfg()
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.assertNotIn("would launch overall", self.h("run", "--dry-run"))     # nothing needs it
        self.h("msg", "send", "--to", "overall", "--subject", "stuck", "--body", "T3 keeps failing")
        self.assertIn("would launch overall", self.h("run", "--dry-run"))
        self.h("run", "--overall")
        self.assertEqual((self.root / "overseer_result").read_text().strip(), "refused")  # cannot grant approvals
        self.assertIn("approval waiting", self.h("msg", "inbox", "human"))
        self.h("brief", "overall")                                                # reads the mail
        self.assertNotIn("would launch overall", self.h("run", "--dry-run"))     # min gap + nothing new
        cfg = json.loads((self.root / ".hyra/config.json").read_text()); cfg["overall_agent"] = False
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.h("msg", "send", "--to", "overall", "--subject", "again", "--body", "x")
        self.assertNotIn("would launch overall", self.h("run", "--dry-run"))     # can be turned off

    def test_stalled_mission_and_failed_tasks_wake_overall(self):
        self._fake_cfg(overall_stall_hours=0.0001)
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("brief", "mission:m1", env={"HYRA_ROLE": "mission"})              # the mission has been woken once...
        time.sleep(1.2)                                                           # ...and then nothing happens
        self.assertIn("would launch overall", self.h("run", "--dry-run", "--max-workers", "0"))  # stalled mission is the only reason

    def test_notify_cmd_fires_for_messages_to_the_human(self):
        self._fake_cfg(notify_cmd='echo "$HYRA_NOTIFY_TEXT" >> notified.txt', overall_agent=False)
        self.h("msg", "send", "--to", "human", "--subject", "conclusion ready", "--body", "see mission m1")
        self.h("run", "--once", "--max-workers", "0")
        self.assertIn("conclusion ready: see mission m1", (self.root / "notified.txt").read_text())
        self.h("run", "--once", "--max-workers", "0")                             # not notified twice
        self.assertEqual((self.root / "notified.txt").read_text().count("conclusion ready"), 1)

    def test_closed_missions_get_an_automatic_retrospective(self):
        self._fake_cfg(overall_agent=False)
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("mission", "close", "m1", "--conclusion", "done")
        self.assertIn("would launch retro:m1", self.h("run", "--dry-run"))
        self.h("run", "--once", "--max-workers", "0")
        self.assertNotIn("Retrospectives pending", self.h("status"))
        self.assertNotIn("would launch retro:m1", self.h("run", "--dry-run"))
        cfg = json.loads((self.root / ".hyra/config.json").read_text()); cfg["auto_retro"] = False
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.h("mission", "new", "m2", "--title", "t", "--problem", "p")
        self.h("mission", "close", "m2", "--conclusion", "done")
        self.assertNotIn("would launch retro:m2", self.h("run", "--dry-run"))

    def test_review_flaws_become_automated_check_tasks(self):
        """Spec: 'Flaws found during review can become automated checks in the toolchain'. Now a queued task, not advice."""
        self.h("init")
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        f = self.root / "r.txt"; f.write_text("x")
        for n in (1, 2):
            self.h("task", "add", "--mission", "m1", "--title", f"exp{n}", "--spec", "s")
            self.h("task", "next", "--worker", "w")
            self.h("task", "complete", str(n), "--summary", "claim", "--evidence", str(f))
        self.h("review", "request", "1")                                      # T3
        self.h("review", "blind", "3", "--notes", "my view", env={"HYRA_TASK": "3", "HYRA_ROLE": "reviewer"})
        self.h("review", "verdict", "3", "--verdict", "supported", "--notes", "fine", env={"HYRA_TASK": "3", "HYRA_ROLE": "reviewer"})
        self.assertNotIn("automated check", self.h("task", "list"))              # supported -> no flaw -> no task
        self.h("review", "request", "2")                                      # T6 (T4 was the next free id)
        rid = [l for l in self.h("task", "list").splitlines() if "Review T2" in l][0].split()[0].lstrip("T")
        env = {"HYRA_TASK": rid, "HYRA_ROLE": "reviewer"}
        self.h("review", "blind", rid, "--notes", "my view", env=env)
        out = self.h("review", "verdict", rid, "--verdict", "refuted", "--notes", "the baseline was run on a different seed", env=env)
        listing = self.h("task", "list")
        self.assertIn("build: Add an automated check for the flaw found reviewing T2", listing)
        spec = self.h("task", "show", [l for l in listing.splitlines() if "automated check" in l][0].split()[0].lstrip("T"))
        self.assertIn("different seed", spec)
        self.assertIn("fails on the flawed version", spec)

    def test_approvals_can_require_a_real_terminal(self):
        self.h("init")
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("task", "add", "--mission", "m1", "--title", "big", "--spec", "s", "--needs-approval")
        self.h("approval", "grant", "1")                                       # default: allowed (chat approval flow)
        self.h("task", "add", "--mission", "m1", "--title", "big2", "--spec", "s", "--needs-approval")
        cfg = json.loads((self.root / ".hyra/config.json").read_text()); cfg["approvals_require_tty"] = True
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.assertIn("real terminal", self.h("approval", "grant", "2", ok=False))  # a subprocess shell isn't a tty


    def _start_ledger(self):
        import socket, urllib.request
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        data = Path(self.tmp.name) / "ledger"
        self.ledger = subprocess.Popen([sys.executable, HYRA, "server", "serve", "--data", str(data), "--port", str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env)
        self.addCleanup(lambda: (self.ledger.terminate(), self.ledger.wait()))
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        toks = dict(l.split("=", 1) for l in (data / "initial-tokens.txt").read_text().split())
        self.url = f"http://127.0.0.1:{port}"
        return toks["HYRA_HUMAN_TOKEN"], toks["HYRA_RUNNER_TOKEN"]

    def _http(self, method, path, token, body=None):
        import urllib.request, urllib.error
        req = urllib.request.Request(self.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}", "X-Hyra-Project": self.project})
        try:
            return 200, json.loads(urllib.request.urlopen(req, timeout=5).read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_server_mode_makes_blinding_and_approvals_real_boundaries(self):
        human, runner = self._start_ledger()
        self.h("init")
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.assertIn("project connected", self.h("server", "connect", "--url", self.url))
        cfg = json.loads((self.root / ".hyra/config.json").read_text())
        cfg["resources"] = {"workers": 4, "gpu": 2}
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.project = cfg["server"]["project"]
        H = {"HYRA_HUMAN_TOKEN": human}
        tok = lambda role, scope="": self.h("server", "token", "--role", role, "--scope", scope, env=H).strip()
        mission, worker1 = tok("mission"), tok("worker", "1")

        # 1. completing a task seals the claim ON THE SERVER; the project's database never holds it
        self.h("task", "add", "--mission", "m1", "--title", "exp", "--spec", "s")
        self.h("task", "next", "--worker", "w", env={"HYRA_RUNNER_TOKEN": runner})
        f = self.root / "r.txt"; f.write_text("data")
        SECRET = "CLAIM-the-secret-conclusion-42"
        self.h("task", "complete", "1", "--summary", SECRET, "--evidence", str(f), env={"HYRA_TOKEN": worker1})
        raw = b"".join(p.read_bytes() for p in (self.root / ".hyra").glob("hyra.db*"))
        self.assertNotIn(SECRET.encode(), raw)                                   # not in the db, not in the WAL
        self.assertNotIn("CLAIM-the-secret", self.h("board", "read"))
        self.assertIn(SECRET, self.h("task", "show", "1", env={"HYRA_TOKEN": mission}))   # a mission can read it

        # 2. a reviewer can't read the claim before its blind assessment, however it asks
        self.h("review", "request", "1", env={"HYRA_TOKEN": mission})
        rid = "2"
        reviewer = tok("reviewer", rid)
        R = {"HYRA_TOKEN": reviewer, "HYRA_TASK": rid, "HYRA_ROLE": "reviewer"}
        self.assertIn("blind assessment first", self.h("review", "claim", rid, ok=False, env=R))
        self.assertEqual(self._http("GET", "/v1/claims/1", reviewer)[0], 403)         # direct API: refused
        self.assertEqual(self._http("GET", "/v1/reviews/2/claim", reviewer)[0], 403)
        self.assertEqual(self._http("GET", "/v1/claims/1", runner)[0], 403)           # even the scheduler can't read claims
        self.assertNotIn(SECRET, self.h("task", "show", "1", env=R))
        self.h("review", "blind", rid, "--notes", "my own view", env=R)
        self.assertIn(SECRET, self.h("review", "claim", rid, env=R))                  # revealed only now
        self.h("review", "verdict", rid, "--verdict", "supported", "--notes", "ok", env=R)
        self.h("review", "verdict", rid, "--verdict", "refuted", "--notes", "x", ok=False, env=R)   # immutable
        other = tok("reviewer", "99")
        self.assertEqual(self._http("GET", "/v1/reviews/2/claim", other)[0], 403)     # another review's token is useless here

        # 3. approvals: decided only with the human token, on the server
        self.h("task", "add", "--mission", "m1", "--title", "gpu", "--spec", "s", "--resource", "gpu=1", env={"HYRA_TOKEN": mission})
        self.assertIn("A1", self.h("approval", "list"))
        self.h("approval", "grant", "1", ok=False, env={"HYRA_TOKEN": worker1})           # an agent has no human token
        self.assertEqual(self._http("POST", "/v1/approvals/1/decide", worker1, {"decision": "grant"})[0], 403)
        self.assertEqual(self._http("POST", "/v1/approvals/1/decide", runner, {"decision": "grant"})[0], 403)
        # tampering with the local database does not release gated work
        db = __import__("sqlite3").connect(self.root / ".hyra/hyra.db")
        db.execute("UPDATE approvals SET status='granted'"); db.execute("UPDATE tasks SET status='queued' WHERE title='gpu'"); db.commit(); db.close()
        self.assertIn("nothing ready", self.h("task", "next", "--worker", "w", env={"HYRA_RUNNER_TOKEN": runner}))
        self.assertIn("awaiting_approval", self.h("task", "show", "3"))
        db = __import__("sqlite3").connect(self.root / ".hyra/hyra.db")
        db.execute("UPDATE approvals SET status='pending'"); db.commit(); db.close()
        self.h("approval", "grant", "1", env=H)                                     # the human, with their token
        self.assertIn("claimed T3", self.h("task", "next", "--worker", "w", env={"HYRA_RUNNER_TOKEN": runner}))

        # 4. fails closed when the server is down: nothing is recorded locally
        self.h("task", "add", "--mission", "m1", "--title", "late", "--spec", "s")
        self.h("task", "next", "--worker", "w", env={"HYRA_RUNNER_TOKEN": runner})
        self.ledger.terminate(); self.ledger.wait()
        out = self.h("task", "complete", "4", "--summary", "x", "--evidence", str(f), ok=False, env={"HYRA_TOKEN": worker1})
        self.assertIn("unreachable", out)
        self.assertIn("[running]", self.h("task", "list").split("T4")[1])

    def test_ledger_token_hygiene(self):
        data = Path(self.tmp.name) / "ledger2"
        def serve(*flags):  # start, wait for the tokens line, stop
            p = subprocess.Popen([sys.executable, HYRA, "server", "serve", "--data", str(data), "--port", "0", *flags],
                                 stdout=subprocess.PIPE, text=True, env=self.env)
            lines = []
            for line in p.stdout:  # wait for the real "listening" line instead of guessing a delay (a loaded machine is slower)
                lines.append(line)
                if "listening" in line:
                    break
            p.terminate(); p.wait()
            return "".join(lines)
        out = serve("--no-print-tokens")
        toks = (data / "initial-tokens.txt").read_text()
        human1 = [l for l in toks.split() if l.startswith("HYRA_HUMAN_TOKEN=")][0].split("=")[1]
        self.assertNotIn(human1, out)                                           # not printed, so not in a service journal
        self.assertEqual(oct((data / "initial-tokens.txt").stat().st_mode & 0o777), "0o600")
        self.assertNotIn("First run", serve("--no-print-tokens"))               # a restart doesn't mint anything
        serve("--no-print-tokens", "--rotate-tokens")
        human2 = [l for l in (data / "initial-tokens.txt").read_text().split() if l.startswith("HYRA_HUMAN_TOKEN=")][0].split("=")[1]
        self.assertNotEqual(human1, human2)
        import sqlite3
        db = sqlite3.connect(data / "ledger.db")
        import hashlib
        h = lambda t: hashlib.sha256(t.encode()).hexdigest()
        self.assertEqual(db.execute("select revoked from tokens where hash=?", (h(human1),)).fetchone()[0], 1)
        self.assertEqual(db.execute("select revoked from tokens where hash=?", (h(human2),)).fetchone()[0], 0)

    def test_ledger_cleans_up_after_itself(self):
        """Session tokens expire, housekeeping prunes dead tokens and old audit rows, and research data is never auto-deleted."""
        sys.path.insert(0, str(Path(HYRA).resolve().parents[3]))
        from hyra.server import Ledger
        import sqlite3
        led = Ledger(Path(self.tmp.name) / "l")
        worker = led.mint("worker", "p", "1", ttl_hours=24)
        dead = led.mint("worker", "p", "2", ttl_hours=-48)                   # expired two days ago
        forever = led.mint("human")
        self.assertIsNotNone(led.whoami(worker)); self.assertIsNone(led.whoami(dead)); self.assertIsNotNone(led.whoami(forever))
        self.assertIsNotNone(led.one("SELECT expires FROM tokens WHERE hash=?", __import__("hashlib").sha256(worker.encode()).hexdigest())["expires"])
        self.assertIsNone(led.one("SELECT expires FROM tokens WHERE hash=?", __import__("hashlib").sha256(forever.encode()).hexdigest())["expires"])
        led.x("INSERT INTO audit VALUES (?,?,?,?,?)", "2020-01-01T00:00:00+00:00", "p", "worker", "seal", "T1")     # ancient
        led.x("INSERT INTO audit VALUES (?,?,?,?,?)", "2999-01-01T00:00:00+00:00", "p", "worker", "seal", "T2")     # recent
        led.x("INSERT INTO claims VALUES (?,?,?,?,?,?)", "p", 1, "the research record", "f", "worker", "2020-01-01T00:00:00+00:00")
        led.x("INSERT INTO approvals(project,kind,what,status,created) VALUES ('p','compute','x','granted','2020-01-01T00:00:00+00:00')")
        before = led.stats()
        led.prune(audit_days=180)
        after = led.stats()
        self.assertEqual(after["tokens_total"], before["tokens_total"] - 1)     # only the long-dead token went
        self.assertEqual(after["audit_rows"], before["audit_rows"] - 1)         # only the ancient audit row went
        self.assertEqual((after["claims"], after["approvals"]), (1, 1))         # the research record is untouched
        self.assertIsNone(led.whoami(dead)); self.assertIsNotNone(led.whoami(worker))
        # ledgers created before expiry existed are upgraded in place
        old = sqlite3.connect(Path(self.tmp.name) / "old.db")
        old.executescript("CREATE TABLE tokens (hash TEXT PRIMARY KEY, role TEXT, project TEXT, scope TEXT, created TEXT, revoked INTEGER DEFAULT 0);")
        old.commit(); old.close()
        Path(self.tmp.name, "oldl").mkdir(); (Path(self.tmp.name) / "old.db").rename(Path(self.tmp.name) / "oldl" / "ledger.db")
        Ledger(Path(self.tmp.name) / "oldl")
        cols = [r[1] for r in sqlite3.connect(Path(self.tmp.name) / "oldl" / "ledger.db").execute("PRAGMA table_info(tokens)")]
        self.assertIn("expires", cols)

    def _wait_for(self, fn, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.2)
        return None

    def test_init_generates_config_and_never_deploys_from_an_agent_shell(self):
        home = Path(self.env["HOME"])
        out = self.h("init")                                                          # no terminal: nothing is asked or deployed
        ucfg = home / ".hyra/config.json"
        self.assertTrue(ucfg.exists())                                                  # generated silently
        cfg = json.loads(ucfg.read_text())
        self.assertEqual((cfg["ledger_backup_every_hours"], cfg["ledger_backup_remote"], cfg["ledger_hosts"]), (6, "", []))
        self.assertNotIn("server", json.loads((self.root / ".hyra/config.json").read_text()))
        self.assertIn("optional: `hyra server up`", out)                                # one line, no questions
        self.assertRegex(self.h("config", "show"), r"ledger_backup_every_hours\s+\[user\s*\] 6")
        cfg["ledger_hosts"] = ["me@box"]; ucfg.write_text(json.dumps(cfg))
        p2 = Path(self.tmp.name) / "again"; p2.mkdir()
        subprocess.run([sys.executable, HYRA, "init"], cwd=p2, capture_output=True, env=self.env)
        self.assertEqual(json.loads(ucfg.read_text())["ledger_hosts"], ["me@box"])    # a second init never overwrites your settings

    def test_home_directory_is_never_mistaken_for_a_project(self):
        """~/.hyra (machine-wide settings) looks like a project's .hyra/. Found live: `hyra server up` from a folder under the home
        directory treated the home directory as the project, crashed, and left a stray database in ~/.hyra."""
        self.pre_cleanup = [lambda: self.h("server", "down", "--purge", "--yes", ok=None)]
        home = Path(self.env["HOME"])
        (home / ".hyra").mkdir(exist_ok=True); (home / ".hyra/config.json").write_text("{}")
        work = home / "Projects" / "not-a-project"; work.mkdir(parents=True)
        run = lambda *a: subprocess.run([sys.executable, HYRA, *a], cwd=work, capture_output=True, text=True, env=self.env)
        r = run("status")
        self.assertNotEqual(r.returncode, 0); self.assertIn("not inside a Hyra project", r.stderr)
        self.assertIn("not inside a Hyra project", run("doctor").stdout)
        r = subprocess.run([sys.executable, HYRA, "init"], cwd=home, capture_output=True, text=True, env=self.env)
        self.assertNotEqual(r.returncode, 0); self.assertIn("home directory", r.stderr)         # and init refuses to make it one
        r = run("server", "up", "--local", "--no-service", "--expose", "none")                 # the exact command that crashed
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(sorted(p.name for p in (home / ".hyra").glob("hyra.db*")), [])         # no stray database, ever

    def test_server_up_sets_up_backups_and_init_connects(self):
        import socket
        self.pre_cleanup = [lambda: self.h("server", "down", "--purge", "--yes", ok=None)]
        bare = Path(self.tmp.name) / "remote.git"; subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        out = self.h("server", "up", "--local", "--no-service", "--expose", "none", "--backup-remote", str(bare))
        self.assertIn("ledger is up", out); self.assertIn("backups now run by themselves", out)
        self.assertIn("connected to your ledger server", self.h("init"))                # init needs no flags to connect
        self.assertEqual(json.loads((Path(self.env["HOME"]) / ".hyra/config.json").read_text())["ledger_backup_remote"], str(bare))
        clone = Path(self.tmp.name) / "c"; subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        self.assertTrue((clone / "ledger.sql").exists())                               # the first snapshot already reached the remote
        self.assertNotIn("TABLE tokens", (clone / "ledger.sql").read_text())

    def test_server_up_asks_for_the_backup_remote_in_a_real_terminal(self):
        import pty
        self.pre_cleanup = [lambda: self.h("server", "down", "--purge", "--yes", ok=None)]
        bare = Path(self.tmp.name) / "remote.git"; subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        import select
        m, sl = pty.openpty()
        p = subprocess.Popen([sys.executable, HYRA, "server", "up", "--local", "--no-service", "--expose", "none"], cwd=self.root,
                             stdin=sl, stdout=sl, stderr=sl, env=self.env)
        out, answered, end = "", False, time.time() + 60
        while time.time() < end:  # behave like a person: read until the question appears, then type the answer
            if select.select([m], [], [], 0.2)[0]:
                try:
                    out += os.read(m, 4096).decode(errors="replace")
                except OSError:
                    break
            if not answered and "blank to skip" in out:
                os.write(m, (str(bare) + "\n").encode()); answered = True
            if p.poll() is not None and not select.select([m], [], [], 0.3)[0]:
                break
        p.wait(timeout=30); os.close(sl); os.close(m)
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("Git remote to back the ledger up to", out)
        self.assertEqual(json.loads((Path(self.env["HOME"]) / ".hyra/config.json").read_text())["ledger_backup_remote"], str(bare))
        proj = Path(self.tmp.name) / "p-after"; proj.mkdir()                              # and later projects connect with a bare `init`
        r = subprocess.run([sys.executable, HYRA, "init"], cwd=proj, capture_output=True, text=True, env=self.env)
        self.assertIn("connected to your ledger server", r.stdout)
        self.assertIn("server", json.loads((proj / ".hyra/config.json").read_text()))

    def test_interactive_agent_needs_no_token_ceremony_but_sessions_are_not_upgraded(self):
        self.pre_cleanup = [lambda: self.h("server", "down", "--purge", "--yes", ok=None)]
        self.h("server", "up", "--local", "--no-service", "--expose", "none")
        self.h("init")
        sf = Path(self.env["HOME"]) / ".hyra/ledger.json"
        st = json.loads(sf.read_text())
        f = self.root / "r.txt"; f.write_text("x")
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        for _ in range(2):
            self.h("task", "add", "--mission", "m1", "--title", "a", "--spec", "s")
        self.h("task", "next", "--worker", "w", env={"HYRA_RUNNER_TOKEN": st["runner_token"]})
        self.h("task", "next", "--worker", "w", env={"HYRA_RUNNER_TOKEN": st["runner_token"]})
        # a scheduler-launched worker/reviewer that somehow has no token is refused, never quietly upgraded to Overall
        out = self.h("task", "complete", "2", "--summary", "x", "--evidence", str(f), ok=False, env={"HYRA_ROLE": "worker", "HYRA_TASK": "2"})
        self.assertIn("hyra session", out)
        self.assertNotIn("overall_token", json.loads(sf.read_text()))                       # nothing was minted for it
        # the interactive agent (default role) just works: a short-lived overall token is minted behind the scenes and cached
        self.h("task", "complete", "1", "--summary", "interactive claim", "--evidence", str(f))
        cached = json.loads(sf.read_text())
        self.assertTrue(cached["overall_token"].startswith("hyra_overall_"))
        self.assertIn("interactive claim", self.h("task", "show", "1"))                      # and can read it back
        self.h("task", "complete", "2", "--summary", "second", "--evidence", str(f))
        self.assertEqual(json.loads(sf.read_text())["overall_token"], cached["overall_token"])   # reused, not re-minted each command
        # `hyra session <agent>` is still there for anyone who wants an explicit token
        shown = self.h("session", "--", "sh", "-c", 'echo "tok=$(echo $HYRA_TOKEN | cut -d_ -f2) human=${HYRA_HUMAN_TOKEN:-absent} runner=${HYRA_RUNNER_TOKEN:-absent}"',
                       env={"HYRA_HUMAN_TOKEN": st["human_token"]})
        self.assertIn("tok=overall human=absent runner=absent", shown)
        self.h("session", ok=False)

    def test_routine_overall_commands_trigger_a_due_backup(self):
        self.pre_cleanup = [lambda: self.h("server", "down", "--purge", "--yes", ok=None)]
        bare = Path(self.tmp.name) / "remote.git"; subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        self.h("server", "up", "--local", "--no-service", "--expose", "none", "--backup-remote", str(bare))
        self.h("init")
        sf = Path(self.env["HOME"]) / ".hyra/ledger.json"
        for how in (("status",), ("brief", "overall")):                                  # no scheduler running: just using the agent
            s = json.loads(sf.read_text()); s["last_backup"] = "2020-01-01T00:00:00+00:00"; s.pop("last_backup_attempt", None)
            sf.write_text(json.dumps(s))
            self.h(*how)
            self.assertTrue(self._wait_for(lambda: json.loads(sf.read_text()).get("last_backup", "") > "2026-01-01"), how)
        s = json.loads(sf.read_text()); s["last_backup"] = "2020-01-01T00:00:00+00:00"; sf.write_text(json.dumps(s))
        self.h("status")                                                                # attempted a moment ago: backs off, doesn't hammer
        time.sleep(1.5)
        self.assertEqual(json.loads(sf.read_text())["last_backup"], "2020-01-01T00:00:00+00:00")
        cfgp = Path(self.env["HOME"]) / ".hyra/config.json"                            # and it can be switched off
        c = json.loads(cfgp.read_text()); c["ledger_backup_every_hours"] = 0; cfgp.write_text(json.dumps(c))
        s = json.loads(sf.read_text()); s.pop("last_backup_attempt", None); sf.write_text(json.dumps(s))
        self.h("status"); time.sleep(1.5)
        self.assertEqual(json.loads(sf.read_text())["last_backup"], "2020-01-01T00:00:00+00:00")

    def test_ledger_dump_and_restore_roundtrip(self):
        sys.path.insert(0, str(Path(HYRA).resolve().parents[3]))
        from hyra.server import Ledger, dump_sql, restore_sql
        d1 = Path(self.tmp.name) / "l1"
        led = Ledger(d1)
        led.mint("human"); led.mint("worker", "p", "1", ttl_hours=24)
        led.x("INSERT INTO claims VALUES (?,?,?,?,?,?)", "p", 1, "the sealed conclusion", "f", "worker", "2026-01-01T00:00:00+00:00")
        led.x("INSERT INTO approvals(project,kind,what,status,created) VALUES ('p','compute','run it','granted','2026-01-01T00:00:00+00:00')")
        sql = dump_sql(str(d1))
        self.assertIn("the sealed conclusion", sql)
        self.assertNotIn("hyra_", sql); self.assertNotIn('"tokens"', sql); self.assertNotIn("TABLE tokens", sql)   # no tokens, not even hashes
        f = Path(self.tmp.name) / "dump.sql"; f.write_text(sql)
        d2 = Path(self.tmp.name) / "l2"
        self.assertEqual(restore_sql(str(d2), str(f)), "restored 1 claims, 0 reviews, 1 approvals")
        led2 = Ledger(d2)
        self.assertEqual(led2.one("SELECT summary FROM claims")[0], "the sealed conclusion")
        self.assertEqual(led2.one("SELECT COUNT(*) FROM tokens")[0], 0)            # fresh tokens will be minted on first serve
        with self.assertRaises(SystemExit):
            restore_sql(str(d2), str(f))                                           # won't silently overwrite a ledger with data
        restore_sql(str(d2), str(f), force=True)
        self.assertTrue(list(d2.glob("ledger.db.before-restore-*")))              # ...and the old one is kept aside

    def test_ledger_backup_restore_cycle_and_init_autoconnect(self):
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        self.pre_cleanup = [lambda: self.h("server", "down", "--purge", "--yes", ok=None)]
        home = Path(self.env["HOME"])
        # 1. init auto-connects a project to a ledger you already deployed
        self.h("init")
        self.assertNotIn("server", json.loads((self.root / ".hyra/config.json").read_text()))   # no ledger yet: local project
        self.h("server", "up", "--local", "--no-service", "--expose", "none", "--port", str(port))
        proj2 = Path(self.tmp.name) / "proj2"; proj2.mkdir()
        run2 = lambda *a, ok=True: subprocess.run([sys.executable, HYRA, *a], cwd=proj2, capture_output=True, text=True, env=self.env)
        out = run2("init")
        self.assertIn("connected to your ledger server", out.stdout)
        c2 = json.loads((proj2 / ".hyra/config.json").read_text())["server"]
        self.assertEqual(c2["url"], f"http://127.0.0.1:{port}"); self.assertEqual(len(c2["project"]), 32)
        proj3 = Path(self.tmp.name) / "proj3"; proj3.mkdir()
        out3 = subprocess.run([sys.executable, HYRA, "init", "--no-ledger"], cwd=proj3, capture_output=True, text=True, env=self.env)
        self.assertNotIn("server", json.loads((proj3 / ".hyra/config.json").read_text()))            # opt-out
        # 2. a sealed claim in that project, then back the ledger up into a git repo with a (local, bare) remote
        st = json.loads((home / ".hyra/ledger.json").read_text())
        H = {"HYRA_HUMAN_TOKEN": st["human_token"]}
        tok = lambda role, scope="": subprocess.run([sys.executable, HYRA, "server", "token", "--role", role, "--scope", scope], cwd=proj2,
                                                     capture_output=True, text=True, env=dict(self.env, **H)).stdout.strip()
        e = lambda **k: dict(self.env, **k)
        r = lambda *a, env=None: subprocess.run([sys.executable, HYRA, *a], cwd=proj2, capture_output=True, text=True, env=env or self.env)
        r("mission", "new", "m1", "--title", "t", "--problem", "p"); r("task", "add", "--mission", "m1", "--title", "a", "--spec", "s")
        r("task", "next", "--worker", "w", env=e(HYRA_RUNNER_TOKEN=st["runner_token"]))
        (proj2 / "r.txt").write_text("x")
        done = r("task", "complete", "1", "--summary", "UNIQUE-SEALED-CONCLUSION-77", "--evidence", str(proj2 / "r.txt"), env=e(HYRA_TOKEN=tok("worker", "1")))
        self.assertEqual(done.returncode, 0, done.stderr)
        bare = Path(self.tmp.name) / "remote.git"; subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        repo = Path(self.tmp.name) / "backup-repo"
        self.assertIn("no backup repo yet", self.h("server", "backup", ok=False))
        out = self.h("server", "backup", "--init", str(repo), "--remote", str(bare))
        self.assertIn("committed", out); self.assertIn("pushed", out)
        sql = (repo / "ledger.sql").read_text()
        self.assertIn("UNIQUE-SEALED-CONCLUSION-77", sql)                                 # the claims are in the backup...
        self.assertNotIn(st["human_token"], sql); self.assertNotIn(st["runner_token"], sql); self.assertNotIn("TABLE tokens", sql)   # ...the tokens are not
        clone = Path(self.tmp.name) / "verify-clone"; subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        self.assertIn("UNIQUE-SEALED-CONCLUSION-77", (clone / "ledger.sql").read_text())    # it really reached the remote
        self.assertIn("no changes", self.h("server", "backup"))                              # idempotent
        # 3. the scheduler backs up on its own when asked to
        u = Path(self.env["HOME"]) / ".hyra/config.json"
        self.h("config", "set", "--user", "ledger_backup_every_hours", "1")
        sf = home / ".hyra/ledger.json"
        stt = json.loads(sf.read_text()); stt["last_backup"] = "2020-01-01T00:00:00+00:00"; sf.write_text(json.dumps(stt))   # long overdue
        self.h("run", "--once", "--max-workers", "0")
        fired = self._wait_for(lambda: json.loads(sf.read_text()).get("last_backup", "") > "2026-01-01" and json.loads(sf.read_text())["last_backup"])
        self.assertTrue(fired, "the scheduler never took a snapshot")                       # it did, in the background
        self.h("run", "--once", "--max-workers", "0")
        time.sleep(1.5)
        self.assertEqual(json.loads(sf.read_text())["last_backup"], fired)                   # and not again within the hour
        # 4. disaster recovery: the ledger is destroyed, redeployed empty, and restored from the git repo
        self.h("server", "down", "--purge", "--yes")
        self.assertFalse((home / ".hyra-server").exists())
        self.h("server", "up", "--local", "--no-service", "--expose", "none", "--port", str(port))
        self.assertIn("--yes", self.h("server", "restore", "--from", str(repo), ok=False))
        out = self.h("server", "restore", "--from", str(repo), "--yes")
        self.assertIn("restored 1 claims", out)
        st2 = json.loads((home / ".hyra/ledger.json").read_text())
        self.assertNotEqual(st2["human_token"], st["human_token"])                           # new tokens after a restore
        mission2 = subprocess.run([sys.executable, HYRA, "server", "token", "--role", "mission"], cwd=proj2, capture_output=True, text=True,
                                  env=e(HYRA_HUMAN_TOKEN=st2["human_token"])).stdout.strip()
        shown = r("task", "show", "1", env=e(HYRA_TOKEN=mission2)).stdout
        self.assertIn("UNIQUE-SEALED-CONCLUSION-77", shown)                                  # the project still reads its sealed claim


    def test_scheduler_mints_scoped_tokens_and_strips_privileged_ones(self):
        human, runner = self._start_ledger()
        script = (r'''echo "runner=${HYRA_RUNNER_TOKEN:-absent} human=${HYRA_HUMAN_TOKEN:-absent}" > envcheck
echo ok > r.txt; hyra task complete $HYRA_TASK --summary "SEALED-BY-SESSION" --evidence r.txt''')
        self.h("init")
        cfg = json.loads((self.root / ".hyra/config.json").read_text())
        cfg.update(harness="fake", harness_cmds={"fake": ["bash", "-c", script, "{prompt}"]}, max_missions_running=0, overall_agent=False)
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.h("server", "connect", "--url", self.url)
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("task", "add", "--mission", "m1", "--title", "a", "--spec", "s")
        self.h("run", "--once", env={"HYRA_RUNNER_TOKEN": runner, "HYRA_HUMAN_TOKEN": human})
        self.assertEqual((self.root / "envcheck").read_text().strip(), "runner=absent human=absent")
        self.assertIn("[done]", self.h("task", "show", "1"))                 # the minted worker token sealed its own claim
        self.assertNotIn("SEALED-BY-SESSION", b"".join(p.read_bytes() for p in (self.root / ".hyra").glob("hyra.db*")).decode("latin1"))
        self.h("run", "--once", ok=False)                                     # the scheduler needs its token in server mode
        # Codex's sandbox has no network, so in server mode it is given network access (to reach the ledger) and only then
        self.h("task", "add", "--mission", "m1", "--title", "b", "--spec", "s")
        self.assertIn("network_access=true", self.h("run", "--dry-run", "--task", "2", "--harness", "codex", env={"HYRA_RUNNER_TOKEN": runner}))


    def _ld(self):
        sys.path.insert(0, str(Path(HYRA).resolve().parents[3]))
        from hyra import ledger_deploy
        return ledger_deploy

    def test_ledger_auto_selection_matches_the_shape_of_a_real_tailnet(self):
        ld = self._ld()
        me = {"ID": "n1", "UserID": 1, "HostName": "macbook", "OS": "macOS"}
        peers = {f"n{i}": {"ID": f"n{i}", "UserID": u, "HostName": h, "DNSName": f"{h}.tail.ts.net.", "OS": os_, "Online": on,
                           "TailscaleIPs": [f"100.0.0.{i}"]}
                 for i, (h, os_, on, u) in enumerate([("APOTHEOSIS", "linux", True, 1), ("phone", "android", True, 1),
                                                       ("offline-box", "linux", False, 1), ("friends-box", "linux", True, 2),
                                                       ("zed-mac", "macOS", True, 1), ("blend", "linux", True, 1),
                                                       ("windows-pc", "windows", True, 1), ("iphone", "iOS", True, 1)], start=2)}
        names = lambda c: [x["host"] for x in c]
        got = ld.candidates({"Self": me, "Peer": peers}, [])
        self.assertEqual(names(got), ["APOTHEOSIS", "blend", "zed-mac"])        # Linux before macOS; no phones/windows/offline/other owners/self
        self.assertEqual(names(ld.candidates({"Self": me, "Peer": peers}, [], any_owner=True))[:3], ["APOTHEOSIS", "blend", "friends-box"])
        got = ld.candidates({"Self": me, "Peer": peers}, ["isagi@zed-mac", "nitez@blend"])  # your own list wins, with your usernames
        self.assertEqual(names(got)[:2], ["blend", "zed-mac"])
        self.assertEqual([c["target"] for c in got][:2], ["nitez@blend", "isagi@zed-mac"])
        self.assertIn("ssh nitez@box", ld.ssh_failure_hint("Host key verification failed.", "nitez@box"))
        self.assertIn("wrong user", ld.ssh_failure_hint("Permission denied (publickey)", "nitez@box"))

    def test_service_definitions(self):
        ld = self._ld()
        unit = ld.systemd_unit(["/usr/bin/python3", "/home/u/.hyra-server/hyra.pyz", "server", "serve", "--host", "127.0.0.1"])
        for needle in ("ExecStart=/usr/bin/python3 /home/u/.hyra-server/hyra.pyz server serve", "Restart=on-failure", "UMask=0077",
                       "NoNewPrivileges=yes", "WantedBy=default.target"):
            self.assertIn(needle, unit)
        plist = ld.launchd_plist("com.hyra.ledger", ["/usr/bin/python3", "/Users/u/.hyra-server/hyra.pyz", "server", "serve"], "/Users/u/.hyra-server")
        for needle in ("<key>Label</key><string>com.hyra.ledger</string>", "<key>KeepAlive</key><true/>", "<string>serve</string>", "ledger.log"):
            self.assertIn(needle, plist)
        import plistlib; plistlib.loads(plist.encode())                           # valid property list

    def test_ledger_deploy_lifecycle_on_this_machine(self):
        """`hyra server up/where/down` end to end (here: 'local' target, plain process, loopback), then a sealed claim via saved tokens."""
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        self.pre_cleanup = [lambda: self.h("server", "down", "--purge", "--yes", ok=None)]
        self.h("init")
        self.h("server", "up", "--dry-run", "--local")
        out = self.h("server", "up", "--local", "--no-service", "--expose", "none", "--port", str(port))
        self.assertIn("ledger is up", out)
        self.assertIn("connected this project", out)                              # inside a project, it connects it
        state_file = Path(self.env["HOME"]) / ".hyra/ledger.json"
        self.assertEqual(oct(state_file.stat().st_mode & 0o777), "0o600")
        st = json.loads(state_file.read_text())
        self.assertTrue(st["human_token"].startswith("hyra_human_") and st["runner_token"].startswith("hyra_runner_"))
        self.assertFalse((Path(self.env["HOME"]) / ".hyra-server/initial-tokens.txt").exists())   # token file removed from the server
        self.assertIn("healthy: True", self.h("server", "where"))
        self.assertIn("up", self.h("server", "up", "--local", "--no-service", "--expose", "none", "--port", str(port)))   # idempotent
        self.assertEqual(json.loads(state_file.read_text())["human_token"], st["human_token"])    # same tokens after a re-run
        # the scheduler needs no exported token: it uses the saved runner token, and strips tokens from the agents it launches
        script = 'echo ok > r.txt; hyra task complete $HYRA_TASK --summary "SEALED-VIA-SAVED-TOKEN" --evidence r.txt'
        cfg = json.loads((self.root / ".hyra/config.json").read_text())
        cfg.update(harness="fake", harness_cmds={"fake": ["bash", "-c", script, "{prompt}"]}, max_missions_running=0, overall_agent=False, auto_retro=False)
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("task", "add", "--mission", "m1", "--title", "a", "--spec", "s")
        self.h("run", "--once")
        self.assertIn("[done]", self.h("task", "show", "1"))
        raw = b"".join(p.read_bytes() for p in (self.root / ".hyra").glob("hyra.db*"))
        self.assertNotIn(b"SEALED-VIA-SAVED-TOKEN", raw)
        # the saved HUMAN token is only usable from a real terminal: an agent's shell (pipes) can't use it
        mission = self.h("server", "token", "--role", "mission", env={"HYRA_HUMAN_TOKEN": st["human_token"]}).strip()
        cfg = json.loads((self.root / ".hyra/config.json").read_text()); cfg["resources"] = {"workers": 4, "gpu": 2}
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.h("task", "add", "--mission", "m1", "--title", "gpu", "--spec", "s", "--resource", "gpu=1", env={"HYRA_TOKEN": mission})
        self.assertNotIn("granted", self.h("approval", "grant", "1", ok=False))   # pipes: no token, refused
        import pty, os as _os
        m, sl = pty.openpty()
        p = subprocess.Popen([sys.executable, HYRA, "approval", "grant", "1"], cwd=self.root, stdin=sl, stdout=sl, stderr=sl, env=self.env)
        p.wait(timeout=30); _os.close(sl)
        self.assertEqual(p.returncode, 0)                                         # a terminal: the saved token is used
        # down keeps data; --purge needs --yes
        self.assertIn("data kept", self.h("server", "down"))
        self.assertIn("healthy: False", self.h("server", "where"))
        self.assertIn("--yes", self.h("server", "down", "--purge", ok=False))
        self.h("server", "up", "--local", "--no-service", "--expose", "none", "--port", str(port))
        self.assertEqual(json.loads(state_file.read_text())["human_token"], st["human_token"])    # data (and tokens) survived
        self.assertIn("deleted", self.h("server", "down", "--purge", "--yes"))
        self.assertFalse(state_file.exists())
        self.assertFalse((Path(self.env["HOME"]) / ".hyra-server").exists())

    def test_ledger_as_a_real_os_service(self):
        """Gated: starts a real launchd/systemd user service on THIS machine (unique name, removed afterwards)."""
        if not os.environ.get("HYRA_TEST_SERVICE"):
            self.skipTest("set HYRA_TEST_SERVICE=1 to start (and remove) a real launchd/systemd user service")
        if sys.platform != "darwin" and not shutil.which("systemctl"):
            self.skipTest("no launchd/systemd here")
        import socket, uuid
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        tag = uuid.uuid4().hex[:8]
        env = {"HYRA_LEDGER_LABEL": f"com.hyra.ledger.test{tag}", "HYRA_LEDGER_UNIT": f"hyra-ledger-test{tag}"}
        self.pre_cleanup = [lambda: self.h("server", "down", "--purge", "--yes", ok=None, env=env)]
        out = self.h("server", "up", "--local", "--expose", "none", "--port", str(port), env=env)
        self.assertRegex(out, r"launchd|systemd")
        self.assertIn("healthy: True", self.h("server", "where", env=env))


    def test_runner_requeues_dead_worker(self):
        self.h("init")
        cfg = json.loads((self.root / ".hyra/config.json").read_text())
        cfg["harness"] = "fake"
        cfg["harness_cmds"] = {"fake": ["true", "{prompt}"]}  # a "worker" that exits immediately
        cfg["max_attempts"] = 2
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("task", "add", "--mission", "m1", "--title", "a", "--spec", "s")
        self.h("run", "--once")
        self.assertIn("requeued", self.h("task", "show", "1"))
        self.h("run", "--once")
        self.assertIn("[failed]", self.h("task", "show", "1"))

    def test_runner_stops_lingering_session(self):
        self.h("init")
        cfg = json.loads((self.root / ".hyra/config.json").read_text())
        script = (f'"{sys.executable}" "{HYRA}" task complete $HYRA_TASK --summary ok --no-evidence; '
                  'sleep 600 # {prompt}')
        cfg.update(harness="fake", harness_cmds={"fake": ["bash", "-c", script]}, settle_grace_seconds=1,
                   poll_seconds=1, max_missions_running=0)
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("task", "add", "--mission", "m1", "--kind", "other", "--title", "a", "--spec", "s")
        out = self.h("run", "--once")
        self.assertIn("stopping pid", out)
        self.assertIn("[done]", self.h("task", "show", "1"))

    def test_any_harness_gets_role_protocol_and_cli(self):
        """A generic harness receives the full role protocol in its prompt and finds `hyra` on PATH."""
        self.h("init")
        cfg = json.loads((self.root / ".hyra/config.json").read_text())
        script = 'printf "%s" "$0" > prompt.txt; hyra task complete $HYRA_TASK --summary ok --evidence prompt.txt'
        cfg.update(harness_cmds={"generic": ["bash", "-c", script, "{prompt}"]}, max_missions_running=0)
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("task", "add", "--mission", "m1", "--title", "a", "--spec", "s", "--harness", "generic")
        self.assertIn("T1 is now done", self.h("run", "--task", "1"))
        prompt = (self.root / "prompt.txt").read_text()
        self.assertIn("Hyra Worker on task T1", prompt)
        self.assertIn("## Integrity rules", prompt)  # worker role body embedded
        self.assertNotIn("name: hyra-worker", prompt)  # Claude frontmatter stripped
        self.h("review", "request", "1", "--harness", "generic")
        self.assertIn("harness: generic", self.h("task", "show", "2"))

    def _home(self):
        home = Path(self.tmp.name) / "home"
        for d in (".claude", ".gemini/antigravity-cli", ".config/opencode", ".kimi-code", ".omp"):
            (home / d).mkdir(parents=True)
        env = {"HOME": str(home), "HYRA_SKILL_DIRS": "", "XDG_CONFIG_HOME": str(home / ".config"),
               "CLAUDE_CONFIG_DIR": str(home / ".claude"), "HYRA_BIN_DIR": str(home / "bin"), "KIMI_CODE_HOME": str(home / ".kimi-code"),
               "PATH": "/usr/bin:/bin"}  # no hyra on PATH, so the toolkit must install the CLI
        return home, env

    def test_skills_only_install_has_a_working_cli(self):
        """What `npx skills add` gives you: the skill folders and nothing else. The bundled hyra.pyz must make them work."""
        sys.path.insert(0, str(Path(HYRA).resolve().parents[3] / "scripts"))
        import build_bundle
        recorded = (Path(HYRA).parent / "hyra.pyz.sha256").read_text().strip()
        self.assertEqual(recorded, build_bundle.source_hash(), "hyra.pyz is stale: run `python3 scripts/build_bundle.py`")
        copy = Path(self.tmp.name) / "skills-copy"
        shutil.copytree(Path(HYRA).resolve().parents[2], copy, ignore=shutil.ignore_patterns("__pycache__"))  # skills/ only
        launcher = copy / "hyra" / "scripts" / "hyra"
        run = lambda *a, **kw: subprocess.run([sys.executable, str(launcher), *a], cwd=self.root, capture_output=True, text=True,
                                              env=dict(self.env, **kw.get("env", {})))
        r0 = run("init"); self.assertEqual(r0.returncode, 0, r0.stderr)
        self.assertEqual(run("mission", "new", "m", "--title", "t", "--problem", "p").returncode, 0)
        self.assertIn("T1 queued", run("task", "add", "--mission", "m", "--title", "a", "--spec", "s").stdout)
        self.assertIn("Overall", run("guide").stdout)                       # finds SKILL.md next to the bundle
        # roles and sibling skills resolve from the copied skill folders (no source checkout around)
        out = run("run", "--dry-run", "--task", "1", "--harness", "codex")
        self.assertIn("would launch task:1 via codex", out.stdout, out.stderr)
        self.assertNotIn("Traceback", out.stderr)
        # and `setup --cli` from the bundle puts a working `hyra` on PATH, with the toolkit installable from the copy
        home = Path(self.tmp.name) / "home2"; (home / ".claude").mkdir(parents=True)
        env = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"), "HYRA_BIN_DIR": str(home / "bin")}
        self.assertEqual(run("setup", "--cli", env=env).returncode, 0)
        r = subprocess.run([str(home / "bin" / "hyra"), "--help"], capture_output=True, text=True, env=dict(self.env, **env))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(run("setup", "--toolkit", "global", "--agents-only", env=env).returncode, 0)
        self.assertTrue((home / ".claude/agents/hyra-worker.md").exists())
        self.assertFalse((home / ".agents/skills").exists())                # agents-only: skills untouched

    def test_config_and_doctor(self):
        self.assertIn("not inside a Hyra project", self.h("doctor"))
        self.h("init")
        self.assertIn("harness", self.h("config", "show"))
        self.h("config", "set", "overal_agent", "false", ok=False)                   # typo -> refused, with a suggestion
        self.assertIn("overall_agent", self.h("config", "set", "overal_agent", "false", ok=False))
        self.h("config", "set", "overall_agent", "false")
        self.h("config", "set", "harness_models.opencode", "opencode/big-pickle")     # dotted keys reach into dicts
        self.h("config", "set", "overall_min_gap_minutes", "30")
        self.h("config", "set", "overall_min_gap_minutes", "soon", ok=False)          # wrong type refused
        cfg = json.loads((self.root / ".hyra/config.json").read_text())
        self.assertEqual((cfg["overall_agent"], cfg["harness_models"], cfg["overall_min_gap_minutes"]),
                         (False, {"opencode": "opencode/big-pickle"}, 30))
        self.h("config", "set", "--user", "approvals_require_tty", "true")           # machine-wide, in the (fake) home
        self.assertRegex(self.h("config", "show"), r"approvals_require_tty\s+\[user")
        self.assertRegex(self.h("config", "show"), r"overall_agent\s+\[project")
        cfg["bogus_key"] = 1
        (self.root / ".hyra/config.json").write_text(json.dumps(cfg))
        self.assertIn("unknown key 'bogus_key'", self.h("config", "validate"))
        out = self.h("doctor")
        self.assertIn("unknown key 'bogus_key'", out)                                # doctor surfaces it
        self.assertIn("evaluations run under", out)
        self.h("config", "unset", "bogus_key")
        self.assertIn("config is valid", self.h("config", "validate"))
        self.assertIn("harness_models", self.h("config", "unset", "harness_models.opencode") and self.h("config", "show"))

    def test_install_cli_only(self):
        home, env = self._home()
        self.h("setup", "--cli", env=env)
        self.assertTrue((home / "bin/hyra").is_symlink())
        self.assertFalse((home / ".claude/skills").exists())  # skills are not touched
        self.h("setup", "--cli", "--uninstall", env=env)
        self.assertFalse((home / "bin/hyra").exists())

    def test_install_toolkit_global(self):
        home, env = self._home()
        self.h("setup", "--toolkit", "global", env=env)
        for d in (".agents/skills", ".claude/skills", ".gemini/antigravity-cli/skills", ".config/opencode/skills"):
            for sk in ("hyra", "hyra-loop", "hyra-prover", "hyra-retro"):
                self.assertTrue((home / d / sk / "SKILL.md").exists(), f"{d}/{sk}")
        self.assertTrue((home / ".claude/skills/hyra").is_symlink())
        self.assertFalse((home / ".cursor").exists())  # harnesses that aren't installed are left alone
        self.assertTrue((home / ".claude/agents/hyra-worker.md").exists())
        self.assertTrue((home / ".claude/agents/wiki-advisor.md").exists())  # roles named by their frontmatter
        # every harness that reads markdown agent files gets the role files, in its own documented location
        for d in (".claude/agents", ".config/opencode/agents", ".gemini/config/agents", ".agents/agents", ".omp/agent/agents"):
            self.assertTrue((home / d / "hyra-worker.md").exists(), d)
            self.assertTrue((home / d / "wiki-scribe.md").exists(), d)
        self.assertFalse((home / ".codex").exists())  # Codex has no agent files: nothing invented
        self.assertTrue((home / ".agents/skills/hyra-wiki/SKILL.md").exists())
        self.assertTrue((home / "bin/hyra").exists())  # CLI installed because it was missing
        self.h("setup", "--toolkit", "global", "--uninstall", env=env)
        self.assertFalse((home / ".gemini/antigravity-cli/skills/hyra").exists())
        self.assertTrue((home / "bin/hyra").exists())  # uninstalling the toolkit leaves the CLI alone

    def test_install_toolkit_local(self):
        home, env = self._home()
        proj = self.root
        self.h("setup", "--toolkit", "local", "--skills", "hyra,hyra-retro", env=env)
        for d in (".agents/skills", ".claude/skills"):
            self.assertTrue((proj / d / "hyra/SKILL.md").exists())
            self.assertFalse((proj / d / "hyra").is_symlink())  # copied, so the repo is self-contained
            self.assertTrue((proj / d / "hyra-retro/SKILL.md").exists())
            self.assertFalse((proj / d / "hyra-loop").exists())  # --skills subset
        self.assertTrue((proj / ".claude/agents/hyra-worker.md").exists())
        self.assertFalse((proj / ".claude/agents/wiki-advisor.md").exists())  # hyra-wiki not selected
        for d in (".opencode/agents", ".agents/agents", ".omp/agents"):  # agy + kimi share .agents/agents
            self.assertTrue((proj / d / "hyra-reviewer.md").exists(), d)
        self.assertFalse((proj / ".qwen").exists())
        self.assertFalse((home / ".claude/skills").exists())  # nothing global
        self.assertTrue((home / "bin/hyra").exists())
        # a project-local install is found by the CLI even if the checkout isn't around
        (proj / "unrelated.txt").write_text("keep")
        self.h("setup", "--toolkit", "local", "--uninstall", env=env)  # idempotent re-run + removal
        self.assertFalse((proj / ".agents/skills/hyra").exists())
        self.assertFalse((proj / ".claude/agents/hyra-worker.md").exists())
        self.assertEqual((proj / "unrelated.txt").read_text(), "keep")
        self.h("setup", "--toolkit", "local", "--skills", "nope", env=env, ok=False)

    def test_wiki_bridge(self):
        vault = Path(self.tmp.name) / "vault"
        for d in ("wiki/concepts", "wiki/_drafts", "raw"):
            (vault / d).mkdir(parents=True)
        (vault / "wiki/concepts/flash-attention.md").write_text(
            '---\ntitle: "Flash Attention"\ntype: concept\nstatus: active\nconfidence: high\n'
            'tldr: "IO-aware exact attention"\ntags: [attention]\n---\nTiling avoids HBM reads.\n')
        (vault / "wiki/concepts/old-attention.md").write_text(
            '---\ntitle: "Old attention"\ntype: concept\nstatus: superseded\ntldr: "attention"\n---\nattention\n')
        (vault / "wiki/index.md").write_text("attention attention attention\n")
        self.h("init")
        self.h("wiki", "search", "attention", ok=False)  # no vault configured yet
        self.h("wiki", "setup", "--vault", str(Path(self.tmp.name) / "nope"), ok=False)
        self.h("wiki", "setup", "--vault", str(vault), "--cluster", "research")
        out = self.h("wiki", "search", "attention")
        self.assertIn("concepts/flash-attention.md", out)
        self.assertNotIn("old-attention", out)       # superseded pages skipped
        self.assertNotIn("index.md", out)
        self.assertIn("old-attention", self.h("wiki", "search", "attention", "--all"))
        self.assertIn("Tiling avoids", self.h("wiki", "show", "flash-attention"))
        self.assertIn("Wiki vault:", self.h("brief", "overall"))
        self.assertNotIn("Wiki vault:", self.h("brief", "overall", env={"HYRA_ROLE": "reviewer"}))

        # export: only verified/convention, approval-gated, drafts only, never overwrites
        self.h("books", "add", "--title", "Warmup fixes divergence", "--method", "Use 1k warmup steps. Then decay.",
               "--rationale", "loss spikes at step 200", "--applies-when", "AdamW, lr>1e-3", "--evidence", "T1")
        self.h("wiki", "export", "warmup-fixes-divergence", ok=False)  # still an observation
        self.h("books", "promote", "warmup-fixes-divergence", "--to", "verified", "--evidence", "T9")
        out = self.h("wiki", "export", "warmup-fixes-divergence")
        self.assertIn("approval A", out)
        self.assertEqual(list((vault / "wiki/_drafts").iterdir()), [])  # nothing written before approval
        self.h("approval", "grant", out.split("approval A")[1].split()[0])
        self.h("wiki", "export", "warmup-fixes-divergence")
        d = (vault / "wiki/_drafts/warmup-fixes-divergence.md").read_text()
        for needle in ("type: concept", "status: draft", 'confidence: medium', 'cluster: "research"',
                       "hyra-verified", "hyra-evidence:T1", "## Counter-Arguments & Gaps", "Use 1k warmup steps."):
            self.assertIn(needle, d)
        self.assertEqual(sorted(p.name for p in (vault / "wiki").iterdir()), ["_drafts", "concepts", "index.md"])
        self.assertEqual(list((vault / "raw").iterdir()), [])
        (vault / "wiki/concepts/warmup-fixes-divergence.md").write_text("---\ntitle: x\n---\n")
        self.h("wiki", "export", "warmup-fixes-divergence", ok=False)  # wiki already has the page


    def test_ideas_live_on_the_board(self):
        """Ideas need no separate object: their state is derived from board refs and task specs."""
        self.h("init")
        self.h("board", "post", "--type", "idea", "Theory: warmup fixes the spike. Falsifier: spike persists with 1k warmup.")
        self.assertIn("[untested] (no mission)", self.h("board", "ideas"))
        self.assertIn("Ideas on the Research Board", self.h("brief", "overall"))
        self.assertIn("[untested]", self.h("status"))
        self.h("board", "post", "--type", "finding", "x", "--ref", "board:99", ok=False)  # dangling ref rejected
        self.h("mission", "new", "m1", "--title", "t", "--problem", "p")
        self.h("board", "post", "--type", "idea", "second idea", "--mission", "m1")
        self.h("task", "add", "--mission", "m1", "--title", "test it", "--spec", "Test the theory in board:1.")
        ideas = self.h("board", "ideas")
        self.assertIn("#1 [testing]", ideas)
        self.assertIn("#3 [untested]", ideas)
        self.assertNotIn("Ideas on the Research Board", self.h("brief", "task:1", env={"HYRA_ROLE": "worker"}))
        # completing the task auto-links its finding to the idea it tested -> handled
        self.h("task", "next", "--worker", "w")
        f = self.root / "r.txt"; f.write_text("spike gone")
        self.h("task", "complete", "1", "--summary", "spike gone with warmup", "--evidence", str(f))
        self.assertNotIn("#1 ", self.h("board", "ideas"))
        self.assertIn("#1 [handled]", self.h("board", "ideas", "--all"))
        # a decision can drop an idea without testing it
        self.h("board", "post", "--type", "decision", "dropping: same as idea 1", "--mission", "m1", "--ref", "board:3")
        self.assertIn("no open ideas", self.h("board", "ideas"))
        # closing warns about loose ideas
        self.h("board", "post", "--type", "idea", "third", "--mission", "m1")
        self.h("review", "request", "1")
        self.h("task", "cancel", "2", "--reason", "skip review")
        self.assertIn("never tested or resolved", self.h("mission", "close", "m1", "--conclusion", "c", "--accept-unreviewed"))


    def test_hyra_wiki_skill_is_harness_neutral(self):
        import re
        sk = Path(HYRA).resolve().parents[2] / "hyra-wiki"
        for f in [sk / "SKILL.md", *sk.glob("roles/*.md"), *sk.glob("workflows/*.md")]:
            text = f.read_text()
            self.assertNotIn("CLAUDE_PLUGIN_ROOT", text, f.name)
            self.assertNotRegex(text, r"AskUserQuestion|WebFetch|/wiki:init", f.name)
            for rel in re.findall(r"\$WIKI_SKILL/((?:scripts|workflows|roles)/[\w./-]+\.(?:sh|md))", text):
                self.assertTrue((sk / rel).exists(), f"{f.name} references missing {rel}")
        for f in sk.glob("roles/*.md"):  # no Claude-only frontmatter keys
            fm = f.read_text().split("\n---", 1)[0]
            self.assertNotRegex(fm, r"(?m)^(model|color|tools):", f.name)
        fm = (sk / "SKILL.md").read_text().split("\n---", 1)[0]
        self.assertIn("name: hyra-wiki", fm)
        # the documented one-step scaffold works, is not re-runnable, and seeds instructions for every harness
        vault = Path(self.tmp.name) / "vault"
        scripts = sk / "scripts"
        run = lambda *a: subprocess.run(["bash", str(scripts / a[0]), *a[1:]], capture_output=True, text=True)
        r = run("new-vault.sh", "--name", "demo", "--path", str(vault))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("hyra-wiki", (vault / "AGENTS.md").read_text())
        self.assertEqual((vault / "CLAUDE.md").read_text().strip(), "@AGENTS.md")
        self.assertIn("vault_path:", (vault / "wiki.config.md").read_text())
        self.assertNotEqual(run("new-vault.sh", "--name", "demo", "--path", str(vault)).returncode, 0)
        self.assertEqual(run("wiki-stats.sh", "--vault", str(vault)).returncode, 0)
        # hyra's bridge reads a page the wiki's own scripts created
        self.assertEqual(run("create-page.sh", "concept", "Flash Attention", str(vault)).returncode, 0)
        self.h("init")
        self.h("wiki", "setup", "--vault", str(vault))
        self.assertIn("flash-attention", self.h("wiki", "search", "flash attention"))
        # upstream smoke tests (init, migrate, migrate edge cases) still pass at the new location
        for t in sorted((Path(HYRA).resolve().parents[3] / "tests" / "wiki").glob("smoke-*.sh")):
            p = subprocess.run(["bash", str(t)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, f"{t.name}\n{p.stdout[-400:]}{p.stderr[-400:]}")


if __name__ == "__main__":
    unittest.main()
