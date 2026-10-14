"""Start refusal conditions + one-write-run-per-project lock (design §2)."""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from . import gitutil

# Optional fence: when AW_WORK_ROOT is set, targets and campaign repos must sit under it. Unset = no fence.
WORK_ROOT = Path(os.environ["AW_WORK_ROOT"]).resolve() if os.environ.get("AW_WORK_ROOT") else None
ENGINE_ROOT = Path(__file__).resolve().parent.parent
LOCK_DIR = ENGINE_ROOT / "locks"
PROTECTED_BRANCHES = {"main", "master"}


class SafetyError(Exception):
    pass


def inside_work_root(path):
    if WORK_ROOT is None:
        return True
    try:
        Path(path).resolve().relative_to(WORK_ROOT)
        return True
    except ValueError:
        return False


def preflight(target, allowed_dirty=(), require_clean=True):
    """Raise SafetyError with every violated condition."""
    t = Path(target)
    problems = []
    if not t.is_dir():
        raise SafetyError(f"대상 폴더 없음: {t}")
    if not inside_work_root(t):
        problems.append(f"대상이 작업 범위(AW_WORK_ROOT={WORK_ROOT}) 밖: {t}")
    if not gitutil.is_repo(t):
        problems.append("대상이 git 레포가 아님 (엔진은 git으로 변경을 추적)")
    else:
        br = gitutil.current_branch(t)
        if not br:
            problems.append("detached HEAD — feature 브랜치에서 실행")
        elif br in PROTECTED_BRANCHES:
            problems.append(f"현재 브랜치가 {br} — feature 브랜치를 먼저 만드세요")
        dirty = [] if not require_clean else [p for p in gitutil.dirty_paths(t) if p not in set(allowed_dirty)]
        if dirty:
            problems.append("작업 트리가 깨끗하지 않음(변경 귀속이 흐려짐): " + ", ".join(dirty[:10]))
    if problems:
        raise SafetyError("시작 거부:\n- " + "\n- ".join(problems))


def _lock_path(target, kind="write"):
    """kind="preplan" (§23-8 S4): a separate lock — one preplan per repo, alongside the write run."""
    key = hashlib.sha1(str(Path(target).resolve()).lower().encode()).hexdigest()[:16]
    return LOCK_DIR / (f"{key}.lock" if kind == "write" else f"{kind}_{key}.lock")


def _pid_alive(pid):
    if not pid:
        return False
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                       errors="replace")
    return str(pid) in r.stdout


def read_lock(target, kind="write"):
    p = _lock_path(target, kind)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"corrupt": True}


def acquire_lock(target, run_id, kind="write"):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    p = _lock_path(target, kind)
    info = {"target": str(Path(target).resolve()), "run_id": run_id, "pid": os.getpid(),
            "since": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        cur = read_lock(target, kind) or {}
        alive = _pid_alive(cur.get("pid"))
        raise SafetyError(
            f"이 프로젝트에 {'선기획' if kind != 'write' else '쓰기 실행'}이 이미 있음(run {cur.get('run_id')}, pid {cur.get('pid')}, "
            f"{'실행 중' if alive else '프로세스 없음 — 확인 후 unlock'})")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    return p


def release_lock(target, run_id, kind="write"):
    """Only the owning run may release. The lock file is emptied-by-rename into locks/released/ (no delete)."""
    p = _lock_path(target, kind)
    cur = read_lock(target, kind)
    if not cur or cur.get("run_id") != run_id:
        return False
    rel = LOCK_DIR / "released"
    rel.mkdir(exist_ok=True)
    p.replace(rel / f"{p.stem}_{run_id}_{int(time.time())}.lock")
    return True


def force_unlock(target, kind="write"):
    """Admin command. Refuses while the owning process is alive."""
    cur = read_lock(target, kind)
    if not cur:
        return "잠금 없음"
    if _pid_alive(cur.get("pid")) and cur.get("pid") != os.getpid():
        raise SafetyError(f"잠금 소유 프로세스(pid {cur.get('pid')})가 실행 중 — 해제 거부")
    p = _lock_path(target, kind)
    rel = LOCK_DIR / "released"
    rel.mkdir(exist_ok=True)
    p.replace(rel / f"{p.stem}_forced_{int(time.time())}.lock")
    return f"해제함 (run {cur.get('run_id')})"
