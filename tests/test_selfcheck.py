"""v4 §11 — the developer's self-check: one allowed command (selfcheck.py), re-run by the engine, a failure goes back
to the developer's own session (at most self_check.max_fixes), what still fails is left to the machine check."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from agentworkflow import selfcheck
from agentworkflow.adapters import FakeAdapter, Result
from agentworkflow.config import ConfigError
from agentworkflow.engine import Engine

import helpers as h

PY = sys.executable.replace("\\", "/")
CHECKER = "import sys, pathlib\nbad = [f for f in sys.argv[1:] if 'BAD' in pathlib.Path(f).read_text()]\n" \
          "print('checked', sys.argv[1:], 'bad', bad)\nsys.exit(1 if bad else 0)\n"
TMPCHECK = "import sys, pathlib\np = pathlib.Path(sys.argv[1]) / 'made.txt'\nassert not p.exists()\np.write_text('x')\n"


# the checker scripts live OUTSIDE the target repo (a file the dev did not own would be an out-of-target change)
TOOLS = h.ROOT / "selfcheck_tools"
TOOLS.mkdir(parents=True, exist_ok=True)
(TOOLS / "checker.py").write_text(CHECKER, encoding="utf-8")
(TOOLS / "tmpcheck.py").write_text(TMPCHECK, encoding="utf-8")
CHK, TMPCHK = (TOOLS / "checker.py").as_posix(), (TOOLS / "tmpcheck.py").as_posix()


def repo_with_checker():
    return h.make_repo()


def sc_cfg(**over):
    return {"commands": [f"{PY} {CHK} {{files}}"], "extensions": [".py"], "timeout_sec": 60, "max_fixes": 2,
            **over}


class Runner(unittest.TestCase):
    def test_files_extensions_and_refusals(self):
        repo = repo_with_checker()
        (repo / "good.py").write_text("ok", encoding="utf-8")
        (repo / "bad.py").write_text("BAD", encoding="utf-8")
        (repo / "notes.md").write_text("BAD", encoding="utf-8")
        tmp = repo.parent / f"sc_{repo.name}"
        ok, log = selfcheck.run(sc_cfg(), ["good.py", "notes.md"], repo, tmp)
        self.assertTrue(ok, log)  # .md is not in extensions -> skipped
        ok, log = selfcheck.run(sc_cfg(), ["good.py", "bad.py"], repo, tmp)
        self.assertFalse(ok)
        self.assertIn("bad.py", log)
        for refused in ("--version", "../outside.py", "missing.py", str(repo / "good.py")):
            ok, log = selfcheck.run(sc_cfg(), [refused], repo, tmp)
            self.assertFalse(ok, refused)
            self.assertIn("검사할 수 없는 경로", log)
        self.assertEqual(selfcheck.run(sc_cfg(), [], repo, tmp)[0], True)

    def test_tmp_is_fresh_for_every_check(self):
        """Parallel workers must never share generated files (e.g. a sourcemap) — {tmp} is new each run."""
        repo = repo_with_checker()
        (repo / "good.py").write_text("ok", encoding="utf-8")
        cfg = sc_cfg(commands=[f"{PY} {TMPCHK} {{tmp}}", f"{PY} {CHK} {{files}}"])
        tmp = repo.parent / f"sc2_{repo.name}"
        self.assertTrue(selfcheck.run(cfg, ["good.py"], repo, tmp)[0])
        self.assertTrue(selfcheck.run(cfg, ["good.py"], repo, tmp)[0])  # tmpcheck asserts made.txt is not there yet

    def test_script_reads_its_config_from_the_environment(self):
        repo = repo_with_checker()
        (repo / "bad.py").write_text("BAD", encoding="utf-8")
        cfgp = repo.parent / f"sc_{repo.name}.json"
        cfgp.write_text(json.dumps({**sc_cfg(), "tmp_root": (repo.parent / f"t_{repo.name}").as_posix()}),
                        encoding="utf-8")
        env = {**os.environ, selfcheck.ENV: str(cfgp), "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run([sys.executable, selfcheck.SCRIPT, "bad.py"], cwd=repo, env=env, capture_output=True,
                           text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 1)
        self.assertIn("자기 검사 실패", r.stdout)
        env.pop(selfcheck.ENV)
        r = subprocess.run([sys.executable, selfcheck.SCRIPT, "bad.py"], cwd=repo, env=env, capture_output=True,
                           text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 2)  # outside an engine run it refuses (never a silent pass)

    def test_config_rules(self):
        repo = repo_with_checker()
        with self.assertRaises(ConfigError):
            h.make_cfg(repo, self_check={"commands": ["stylua --check src"]})  # no {files}
        with self.assertRaises(ConfigError):
            h.make_cfg(repo, self_check={"max_fixes": -1})


def engine(repo, script, limits=None, max_fixes=2):
    cfg = h.make_cfg(repo, self_check=sc_cfg(max_fixes=max_fixes), limits=limits or {})
    fake = FakeAdapter(script)
    req = h.make_request(1, extra=h.NO_QA)
    return Engine(cfg, req, h.make_state(repo), fake, fake, sleep=lambda s: None, out=lambda m: None), fake


def dev_writes(bodies, sid="dev-s"):
    """The builder: its n-th call writes bodies[n] into hello.py (only the first call reports the item done)."""
    n = {"i": 0}

    def fn(kw):
        i = n["i"]
        n["i"] += 1
        if kw.get("on_init"):
            kw["on_init"](kw.get("resume") or sid)
        Path(kw["cwd"], "hello.py").write_text(bodies[i], encoding="utf-8")
        done = [] if i else [{"id": "DEV-001", "files": ["hello.py"], "note": "작성"}]
        return Result(text="d", session_id=kw.get("resume") or sid, structured={
            "done": done, "summary": "구현", "remaining": "없음", "write_gate": "W1 grep 0건 / W2 / W3 / W4 hello.py",
            "question": ""})
    return [fn] * len(bodies)


def rest():
    return {"evaluator": [h.e_ans("READY")], "evaluator:devil": [h.devil()]}


def devs(fake):
    return [c for c in fake.calls if c["role"] == "builder"]


class Engine1(unittest.TestCase):
    def test_builder_fixes_in_its_own_session_then_goes_to_evaluation(self):
        repo = repo_with_checker()
        e, fake = engine(repo, {"builder": dev_writes(["BAD\n", "print(1)\n"]), **rest()})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        d = devs(fake)
        self.assertEqual(len(d), 2)
        self.assertEqual(d[1]["resume"], "dev-s")  # the same session continues
        self.assertIn("자기 검사 실패", d[1]["prompt"])
        rule = f"Bash({selfcheck.command_prefix()}:*)"
        self.assertIn(rule, d[0]["allowed_tools"])
        self.assertNotIn("Bash", d[0]["disallowed_tools"])
        self.assertIn("PowerShell", d[0]["disallowed_tools"])  # every other shell stays closed
        self.assertTrue(Path(d[0]["env"][selfcheck.ENV]).is_file())
        self.assertIn("자기 검사 (필수", d[0]["prompt"])
        self.assertIn("자기 검사 이어서 수정 (1/2) — 통과", (e.st.dir / "timeline.md").read_text(encoding="utf-8"))

    def test_evaluators_never_get_the_command(self):
        repo = repo_with_checker()
        e, fake = engine(repo, {"builder": dev_writes(["print(1)\n"]), **rest()})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        for c in fake.calls:
            if c["role"] != "builder":
                self.assertFalse(any(str(a).startswith("Bash(") for a in c["allowed_tools"] or []), c["role"])
                self.assertIn("Bash", c["disallowed_tools"])

    def test_still_failing_after_max_fixes_is_an_auto_reject_at_the_end_checks(self):
        # v6 §5-1 step 6 (P1): the end checks run the self-check too — BUILD does not finish on a failing file
        repo = repo_with_checker()
        e, fake = engine(repo, {"builder": dev_writes(["BAD\n", "BAD\n", "BAD\n", "print(1)\n"]), **rest()},
                         max_fixes=2)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(len(devs(fake)), 4)
        self.assertIn("자기 검사 실패(엔진 실행)", devs(fake)[3]["prompt"])


if __name__ == "__main__":
    unittest.main()
