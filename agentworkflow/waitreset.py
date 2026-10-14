"""v5 — the usage pause, after the engine stopped itself (PAUSED_USAGE):

  engine at 95% → PAUSED_USAGE · the engine starts its own fallback (spawn_fallback) · its supervise task ends
  → the Master is woken, tells the user when it resumes, and runs `wait-reset --run X` in the background
  → that ends at the reset + 60 s → the Master is woken again, checks the run and resumes it (`supervise resume`).
  If the Master does not (it hit the limit itself, the window was closed…), the fallback resumes the run itself
  `usage_pause.fallback_min` minutes after that — only if the run is still in the SAME pause.

The fallback is started by the ENGINE, not by `wait-reset`: it must work even when the Master never answers.
The reset time is Anthropic's own (`resetsAt` of the usage reading that stopped the engine), not a guess.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

from .safety import SafetyError
from .state import RunState

PKG_ROOT = Path(__file__).resolve().parent.parent
# Windows: the fallback must outlive the engine and its supervisor (their console and, if any, their job)
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
_BREAKAWAY = 0x01000000              # CREATE_BREAKAWAY_FROM_JOB


def _pause(run_dir):
    st = RunState.load(run_dir)
    return st, st.d.get("paused_usage") or {}


def run(run_dir, out=print, sleep=time.sleep):
    """`wait-reset`: the Master's waker — ends at the reset + 60 s (the background task ending wakes the Master)."""
    st, pu = _pause(run_dir)
    if st.d["status"] != "PAUSED_USAGE" or not pu.get("reset_epoch"):
        raise SafetyError(f"wait-reset 은 PAUSED_USAGE 에서만 (현재 {st.d['status']})")
    wake = float(pu["reset_epoch"]) + 60
    out(f"사용량 리셋 {time.strftime('%m-%d %H:%M', time.localtime(wake - 60))} (+60초)까지 대기")
    while time.time() < wake:
        sleep(min(300.0, max(1.0, wake - time.time())))
    out(f"RESUME_READY — 리셋 지남. Master: `status --run {run_dir}` 로 상태 확인 → `supervise resume --run {run_dir}`")
    return 0


def spawn_fallback(run_dir, at, reset_epoch):
    """Start the detached fallback (called by the engine as it pauses). -> True when the process was started."""
    argv = [sys.executable, "-m", "agentworkflow.waitreset", "--fallback", str(run_dir), str(at), str(reset_epoch)]
    with open(Path(run_dir) / "waitreset.log", "a", encoding="utf-8") as log:
        for flags in (_DETACHED | _BREAKAWAY, _DETACHED):  # a job that forbids breakaway refuses the first (OSError)
            try:
                subprocess.Popen(argv, cwd=str(PKG_ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 creationflags=flags, close_fds=True)
                return True
            except OSError:
                continue
        log.write(f"[{time.strftime('%H:%M:%S')}] 대비책 프로세스를 띄우지 못함 — Master가 직접 재개해야 함\n")
    return False


def fallback_main(run_dir, at, reset_epoch):
    """The detached fallback: at `at`, resume the run if it is still in the pause that started it."""
    run_dir = Path(run_dir)
    while time.time() < at:
        time.sleep(min(300.0, max(1.0, at - time.time())))
    st, pu = _pause(run_dir)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if st.d["status"] != "PAUSED_USAGE" or str(pu.get("reset_epoch")) != str(reset_epoch):
        print(f"[{stamp}] 대비책 종료 — 이미 재개됨/다른 상태({st.d['status']})", flush=True)
        return 0
    st.timeline("사용량 리셋 뒤 Master 재개 없음 → 엔진 대비책이 스스로 재개(`supervise resume`)")
    print(f"[{stamp}] Master 재개 없음 → supervise resume", flush=True)
    return subprocess.call([sys.executable, "-m", "agentworkflow", "supervise", "resume", "--run", str(run_dir)],
                           cwd=str(PKG_ROOT))


if __name__ == "__main__":
    # its stdout/stderr are waitreset.log: without this Python writes the system encoding (cp949 here) unless the
    # parent shell set UTF-8, and 「—」 is not in cp949 — 3 of 11 logs ended in UnicodeEncodeError (10-06~10-10)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if len(sys.argv) == 5 and sys.argv[1] == "--fallback":
        sys.exit(fallback_main(sys.argv[2], float(sys.argv[3]), sys.argv[4]))
    print(json.dumps({"usage": "python -m agentworkflow wait-reset --run <run 폴더>"}, ensure_ascii=False))
    sys.exit(2)
