"""v4 §6 — supervisor keeps a run alive (09-26 중간 평가: ReDoS 34분 먹통 · 예외 종료 후 state RUNNING 잔존 · 사람이
깨어 있어야만 진행). Engine side: top-level exception -> HELP, heartbeat file, per-role call timeouts."""
import json
import sys
import time
import unittest
from pathlib import Path

from agentworkflow import supervisor
from agentworkflow.adapters import FakeAdapter, Result
from agentworkflow.engine import Engine

import helpers as h

FAKE = Path(__file__).resolve().parent / "fake_engine.py"


def run_dir(script):
    d = h.ROOT / f"sup{next(h._n)}"
    d.mkdir(parents=True)
    (d / "script.json").write_text(json.dumps(script), encoding="utf-8")
    return d


def sup(d, first=("run",), **kw):
    argv = list(first) + (["--dir", str(d)] if first[0] == "run" else ["--run", str(d)])
    opts = dict(stall_sec=2.0, poll_sec=0.2, restart_window_sec=60, max_restarts=3)
    opts.update(kw)
    return supervisor.Supervisor(argv, launcher=lambda a: [sys.executable, str(FAKE), *a], out=lambda m: None, **opts)


def starts(d):
    return int((d / "starts.txt").read_text())


def status(d):
    return json.loads((d / "state.json").read_text(encoding="utf-8"))["status"]


class Supervise(unittest.TestCase):
    def test_complete_exits_zero_without_restart(self):
        d = run_dir(["complete"])
        self.assertEqual(sup(d).run(), 0)
        self.assertEqual(starts(d), 1)

    def test_crash_is_resumed_automatically(self):
        # #12 (09-26): engine died with state RUNNING and nobody noticed — now the supervisor resumes it
        d = run_dir(["crash", "complete"])
        self.assertEqual(sup(d).run(), 0)
        self.assertEqual(starts(d), 2)
        self.assertIn("감시자", (d / "timeline.md").read_text(encoding="utf-8"))

    def test_stall_is_killed_and_resumed(self):
        # #11 (09-26): a frozen main thread (ReDoS) stopped writing heartbeats for 34 minutes
        d = run_dir(["stall", "complete"])
        t0 = time.monotonic()
        self.assertEqual(sup(d).run(), 0)
        self.assertEqual(starts(d), 2)
        self.assertLess(time.monotonic() - t0, 30)

    def test_repeated_restarts_become_help(self):
        d = run_dir(["crash"])
        self.assertEqual(sup(d, max_restarts=3).run(), 3)
        self.assertEqual(status(d), "HELP")
        self.assertEqual(starts(d), 3)

    def test_decision_states_go_to_master_without_restart(self):
        for act in ("help", "awaiting"):
            d = run_dir([act])
            self.assertEqual(sup(d).run(), 3)
            self.assertEqual(starts(d), 1)

    def test_a_stage_move_does_not_stop_or_restart_the_run(self):
        """v6: the preplan notice is gone — a stage move inside a run is nobody's business but the engine's."""
        d = run_dir(["qa", "complete"])
        lines = []
        s = sup(d)
        s.out = lines.append
        self.assertEqual(s.run(), 0)
        self.assertEqual(starts(d), 1)
        self.assertFalse(any("알림" in x for x in lines))
        self.assertFalse((d / "notice.json").exists())

    def test_resume_argv_is_used_for_restarts(self):
        d = run_dir(["crash", "complete"])
        seen = []
        s = sup(d)
        s.launcher = lambda a: (seen.append(list(a)), [sys.executable, str(FAKE), *a])[1]
        s.run()
        self.assertEqual(seen[1][:3], ["resume", "--run", str(d)])

    def test_failed_start_without_run_dir_returns_child_code(self):
        s = supervisor.Supervisor(["run", "--config", "x"], launcher=lambda a: [sys.executable, "-c", "import sys; sys.exit(2)"],
                                  out=lambda m: None, stall_sec=2.0, poll_sec=0.2)
        self.assertEqual(s.run(), 2)


class EngineSide(unittest.TestCase):
    def test_unexpected_exception_becomes_help_not_running(self):
        repo = h.make_repo()
        boom = lambda kw: (_ for _ in ()).throw(RuntimeError("boom"))  # noqa: E731
        cfg = h.make_cfg(repo)
        st = h.make_state(repo)
        fake = FakeAdapter([boom])
        e = Engine(cfg, h.make_request(1), st, fake, fake, sleep=lambda s: None, out=lambda m: None)
        self.assertEqual(e.run(), "HELP")
        self.assertIn("RuntimeError", st.d["pause_reason"])

    def test_heartbeat_file_is_written_during_a_run(self):
        repo = h.make_repo()
        cfg = h.make_cfg(repo)
        cfg["supervisor"]["heartbeat_sec"] = 0.05
        st = h.make_state(repo)

        def slow(kw):  # an error that stops at once (unknown model -> HELP, no retry), after a few heartbeats
            time.sleep(0.3)
            return Result(text="does not support this model", is_error=True)
        fake = FakeAdapter([slow])
        e = Engine(cfg, h.make_request(1), st, fake, fake, sleep=lambda s: None, out=lambda m: None)
        e.run()
        hb = json.loads((st.dir / "heartbeat.json").read_text(encoding="utf-8"))
        self.assertIn("epoch", hb)
        self.assertIn("pid", hb)

    def test_call_limits_go_to_the_adapter(self):
        """v6 S-7 · D1: every call carries the idle and total limits (the adapter's watcher thread cuts the call)."""
        repo = h.make_repo()
        cfg = h.make_cfg(repo, limits={"idle_timeout_sec": 120, "call_total_sec": 3600})
        fake = FakeAdapter({"builder": [lambda kw: Result(text="does not support this model", is_error=True)]})
        Engine(cfg, h.make_request(1), h.make_state(repo), fake, fake, sleep=lambda s: None, out=lambda m: None).run()
        self.assertEqual((fake.calls[0]["idle_timeout"], fake.calls[0]["total_timeout"]), (120, 3600))


if __name__ == "__main__":
    unittest.main()
