"""Two engine defects found in real runs.

1. Time limits that did not cut: a machine check hit its 1800 s limit but returned after 9886.6 s. On Windows
   subprocess.run kills only the direct child at the limit, then waits with NO limit for the pipes — and the
   grandchild (powershell → Godot · Godot_console.exe → Godot) still held them. procutil.run cuts the tree.
2. Writes refused after a `cd`: the builder's `Edit(./**)`·`Write(./**)` follow the session's cwd, and a read-only
   `cd X && ls` moves it (X inside --add-dir) — every write to the target was then refused ("mode"). Measured:
   absolute rules keep the target writable after a cd and refuse X."""

import subprocess
import sys
import time
import unittest
from pathlib import Path

from agentworkflow import checks, procutil, prompts, selfcheck
from agentworkflow.config import ConfigError, DEFAULTS, _merge, validate

import helpers as h

PY = sys.executable.replace("\\", "/")
# a parent that waits on a long grandchild, like run_tests.ps1 waits on Godot
GRAND = ("import subprocess, sys, pathlib\n"
         "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
         "pathlib.Path(sys.argv[1], 'grandchild.pid').write_text(str(c.pid))\n"
         "c.wait()\n")
TOOLS = h.ROOT / "procutil_tools"
TOOLS.mkdir(parents=True, exist_ok=True)
(TOOLS / "grand.py").write_text(GRAND, encoding="utf-8")
GRAND_PY = (TOOLS / "grand.py").as_posix()
CUT_WITHIN = 20  # before the fix each of these took the grandchild's full 60 s


def _wait_pid(folder):
    p = Path(folder) / "grandchild.pid"
    for _ in range(100):
        if p.is_file() and p.read_text().strip():
            return int(p.read_text())
        time.sleep(0.1)
    return None


def _alive(pid):
    # bytes: with no match tasklist prints a localized (cp949) notice that a UTF-8 decode would lose
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True).stdout
    if str(pid).encode() in out:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)  # never leave it behind
        return True
    return False


class TimeLimitCutsTheTree(unittest.TestCase):
    def test_machine_check_returns_at_its_limit(self):
        d = h.make_repo()
        t0 = time.monotonic()
        ok, log = checks.run_checks([f'"{PY}" "{GRAND_PY}" "{d.as_posix()}"'], d, 2)
        self.assertLess(time.monotonic() - t0, CUT_WITHIN)
        self.assertFalse(ok)
        self.assertIn("(exit -1", log)
        self.assertIn("타임아웃 2s", log)
        pid = _wait_pid(d)
        self.assertIsNotNone(pid)
        self.assertFalse(_alive(pid))

    def test_self_check_returns_at_its_limit(self):
        d = h.make_repo()
        (d / "good.py").write_text("ok", encoding="utf-8")
        tmp = h.ROOT / "procutil_sc"
        cfg = {"commands": [f"{PY} {GRAND_PY} {{tmp}}"], "extensions": [".py"], "timeout_sec": 2, "max_fixes": 1}
        t0 = time.monotonic()
        ok, log = selfcheck.run(cfg, ["good.py"], d, tmp)
        self.assertLess(time.monotonic() - t0, CUT_WITHIN)
        self.assertFalse(ok)
        self.assertIn("타임아웃 2s", log)
        pid = _wait_pid(next(tmp.glob("sc_*")))
        self.assertIsNotNone(pid)
        self.assertFalse(_alive(pid))

    def test_run_keeps_subprocess_run_contract(self):
        """callers (qa · roblox_runner · checks · selfcheck) rely on the same result and the same exception."""
        r = procutil.run([PY, "-c", "import sys; d = sys.stdin.read(); print(d.upper()); "
                                    "print('err', file=sys.stderr); sys.exit(3)"],
                         input="abc", capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual((r.returncode, r.stdout.strip(), r.stderr.strip()), (3, "ABC", "err"))
        d = h.make_repo()
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            procutil.run(f'"{PY}" "{GRAND_PY}" "{d.as_posix()}"', shell=True, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=2)
        self.assertLess(time.monotonic() - t0, CUT_WITHIN)
        self.assertFalse(_alive(_wait_pid(d)))


class BuilderWritesFollowTheTarget(unittest.TestCase):
    def test_write_rules_name_the_target_not_the_cwd(self):
        target = h.make_repo().resolve()
        rules = prompts.perms_build(target)
        self.assertIn(f"Edit({target.as_posix()}/**)", rules)
        self.assertIn(f"Write({target.as_posix()}/**)", rules)
        self.assertFalse([r for r in rules if "./" in r])

    def test_a_target_path_with_a_comma_is_refused(self):
        """--allowedTools is one comma-joined argument — a comma would split the target's write rule."""
        cfg = _merge(DEFAULTS, {"target": "C:/work/a,b"})
        with self.assertRaises(ConfigError):
            validate(cfg)


if __name__ == "__main__":
    unittest.main()
