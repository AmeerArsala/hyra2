"""End-to-end test of the hyra CLI in a temp project. Run: python3 -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import tempfile
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
        for k in ("HYRA_AGENT", "HYRA_ROLE", "HYRA_TASK", "HYRA_PROJECT"):
            self.env.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()

    def h(self, *args, ok=True, env=None):
        p = subprocess.run([sys.executable, HYRA, *args], cwd=self.root, capture_output=True, text=True,
                           env=dict(self.env, **(env or {})))
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
        ev = self.h("review", "evidence", rid)
        self.assertIn("result.json", ev)
        self.assertNotIn("baseline x=3", ev)
        shown = self.h("task", "show", "1", env={"HYRA_TASK": rid})
        self.assertNotIn("baseline x=3", shown)
        self.h("review", "blind", rid, "--notes", "reran: x=3 confirmed, but only on seed 0")
        self.assertIn("baseline x=3", self.h("review", "claim", rid))
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
        self.assertIn("via agy: agy -p --dangerously-skip-permissions", self.h("run", "--dry-run", "--task", "2", "--harness", "agy"))
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
        self.assertIn("[queued]", self.h("task", "list"))  # dry run claims nothing

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
        self.h("task", "add", "--mission", "m1", "--title", "a", "--spec", "s")
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

    def test_setup_links_every_harness(self):
        home = Path(self.tmp.name) / "home"
        for d in (".claude", ".gemini/antigravity-cli", ".config/opencode"):
            (home / d).mkdir(parents=True)
        env = {"HOME": str(home), "HYRA_SKILL_DIRS": "", "XDG_CONFIG_HOME": str(home / ".config"),
               "CLAUDE_CONFIG_DIR": str(home / ".claude"), "HYRA_BIN_DIR": str(home / "bin")}
        self.h("setup", env=env)
        for d in (".agents/skills", ".claude/skills", ".gemini/antigravity-cli/skills", ".config/opencode/skills"):
            for sk in ("hyra", "hyra-loop", "hyra-prover", "hyra-retro"):
                self.assertTrue((home / d / sk / "SKILL.md").exists(), f"{d}/{sk}")
        self.assertFalse((home / ".cursor").exists())  # harnesses that aren't installed are left alone
        self.assertTrue((home / ".claude/agents/hyra-worker.md").exists())
        self.assertTrue((home / "bin/hyra").exists())
        self.h("setup", "--uninstall", env=env)
        self.assertFalse((home / ".gemini/antigravity-cli/skills/hyra").exists())
        self.assertFalse((home / "bin/hyra").exists())


if __name__ == "__main__":
    unittest.main()
