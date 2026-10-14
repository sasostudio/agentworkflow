"""v4 §6 — the supervisor keeps one engine run alive so nobody has to watch it.

09-26 mid-review: an engine froze for 34 minutes (ReDoS in the main thread) and another died leaving state RUNNING —
both waited for a human. The supervisor runs the engine CLI as a child and:
  - child exited while the run state still says it was working (RUNNING)                  -> `resume` it
  - heartbeat file older than `stall_sec` while the child is alive (frozen main thread)     -> kill the tree, resume
  - `max_restarts` restarts inside `restart_window_sec`                                    -> state HELP, stop
It exits (= the Master's background task finishes, which notifies the Master session) only when the run reached a
state that needs someone: COMPLETE · HELP · a Master decision (AWAITING_*) · PAUSED_USAGE.
(v6: the preplan notice is gone with the preplan — 설계서 v6 §6-2.)
If supervision misbehaves, this module is the one place to read."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

WORKING = {"RUNNING"}       # the process is gone but the run was mid-work -> resume
DONE_OK = {"COMPLETE"}


def _engine_cmd(argv):
    return [sys.executable, "-m", "agentworkflow", *argv]


def _kill_tree(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def _read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Supervisor:
    def __init__(self, argv, *, launcher=None, out=print, stall_sec=120.0, poll_sec=5.0,
                 restart_window_sec=3600.0, max_restarts=3, kill=_kill_tree, clock=time.monotonic):
        self.argv = list(argv)
        self.launcher = launcher or _engine_cmd
        self.out = out
        self.stall_sec, self.poll_sec = stall_sec, poll_sec
        self.window, self.max_restarts = restart_window_sec, max_restarts
        self.kill, self.clock = kill, clock
        self.run_dir = self._dir_from_argv(self.argv)
        self.restarts = []  # clock() of each restart

    # ---------- helpers ----------
    @staticmethod
    def _dir_from_argv(argv):
        if "--run" in argv:
            return Path(argv[argv.index("--run") + 1]).resolve()
        return None

    def _pump(self, proc):
        """Echo the child's output; learn the run folder from the CLI's `run: …` line."""
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            self.out(line)
            for key in ("run: ",):
                if line.startswith(key) and self.run_dir is None:
                    self.run_dir = Path(line[len(key):].strip()).resolve()

    def _state(self):
        return _read_json(self.run_dir / "state.json") if self.run_dir else None

    def _heartbeat_age(self, started, started_wall):
        hb = _read_json(self.run_dir / "heartbeat.json") if self.run_dir else None
        last = hb.get("epoch") if isinstance(hb, dict) else None
        # a beat from before THIS child started belongs to the previous (killed/crashed) process — ignore it, or a
        # fresh child is judged frozen before it has written its first beat (found by test_stall_is_killed_and_resumed)
        if not isinstance(last, (int, float)) or last < started_wall:
            return self.clock() - started  # none yet: count from the child's start
        return max(0.0, time.time() - last)

    def _note(self, line):
        self.out(f"[감시자] {line}")
        if self.run_dir and self.run_dir.is_dir():
            with (self.run_dir / "timeline.md").open("a", encoding="utf-8") as f:
                f.write(f"- `{_now()}` 감시자: {line}\n")

    def _mark_help(self, reason):
        p = self.run_dir / "state.json"
        d = _read_json(p) or {}
        d.update(status="HELP", pause_reason=reason)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        self._note(f"상태 **HELP** — {reason}")

    # ---------- one child ----------
    def _start(self, argv):
        proc = subprocess.Popen(self.launcher(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        t = threading.Thread(target=self._pump, args=(proc,), daemon=True)
        t.start()
        return proc, t

    def _watch(self, proc):
        """-> ("exit", code) or ("stall", age)."""
        started, started_wall = self.clock(), time.time()
        while True:
            code = proc.poll()
            if code is not None:
                return "exit", code
            if self.run_dir is not None:
                age = self._heartbeat_age(started, started_wall)
                if age > self.stall_sec:
                    return "stall", age
            time.sleep(self.poll_sec)

    # ---------- main loop ----------
    def run(self):
        argv = self.argv
        while True:
            proc, pump = self._start(argv)
            kind, val = self._watch(proc)
            if kind == "stall":
                self._note(f"heartbeat {val:.0f}초 멈춤 — 엔진 프로세스 트리 종료 후 재개")
                self.kill(proc.pid)
                proc.wait(timeout=30)
            pump.join(timeout=5)
            # 10-02 log audit: the pipe was never closed — one handle per child, every restart. Closed only once the
            # reader thread is done (a still-reading thread would hit a closed file)
            if proc.stdout and not pump.is_alive():
                proc.stdout.close()
            st = self._state()
            if st is None:  # never got as far as a run folder (config error etc.)
                return val if kind == "exit" else 3
            status = st.get("status")
            if kind == "exit" and status not in WORKING:
                self.out(f"[감시자] 종료 — {status}: {st.get('pause_reason') or ''}"[:400])
                return 0 if status in DONE_OK else 3
            now = self.clock()
            self.restarts = [t for t in self.restarts if now - t < self.window] + [now]
            if len(self.restarts) >= self.max_restarts:
                cause = "멈춤" if kind == "stall" else f"종료 코드 {val}"
                self._mark_help(f"감시자 재시작 {len(self.restarts)}회({self.window / 60:.0f}분 안) — 같은 지점 반복 실패 · "
                                f"단계 {st.get('stage')} · 마지막 원인: {cause}")
                return 3
            if kind == "exit":
                self._note(f"엔진 종료(코드 {val}) · 상태 {status} → 자동 재개 ({len(self.restarts)}/{self.max_restarts})")
            argv = ["resume", "--run", str(self.run_dir)]
