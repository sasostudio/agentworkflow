"""v5 (10-01) — ROBLOX adapter part: what its QA runner and the builder's studio_shot share — one Studio at a time · N
frames. (The sheet that puts the frames together is common to every runner: qa.make_sheet.)

If two things drive Studio at once (a QA run and a builder's shot, or two builders) one stops the other's play.
`studio_lock` makes them take turns: a lock FILE (any process — builders run as their own `claude` processes),
held while one play runs. A lock whose process is gone is taken over.

frames(): screen captures after a scenario's last step, `interval` seconds apart — for what only shows over time
(a cutscene, an animation).
"""

import base64
import contextlib
import json
import os
import time
from pathlib import Path

from .safety import _pid_alive
from .studio_mcp import StudioError

LOCK = Path(__file__).resolve().parent.parent / ".studio.lock"
MAX_FRAMES = 12


class Busy(Exception):
    """Studio stayed taken by another run/worker for the whole wait."""


@contextlib.contextmanager
def studio_lock(who, wait_sec=600, poll=2.0, path=None):
    """Hold Studio for one play. who: shown to whoever waits (e.g. "QA run 2026-10-01_x", "worker DEV-101")."""
    p = Path(path or LOCK)
    t0 = time.time()
    while True:
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "who": who, "t": time.strftime("%Y-%m-%d %H:%M:%S")}, f,
                          ensure_ascii=False)
            break
        except FileExistsError:
            try:
                held = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                held = {}
            if held.get("pid") and not _pid_alive(held["pid"]):
                with contextlib.suppress(OSError):
                    os.replace(p, p.with_suffix(".lock.stale"))  # its owner is gone — take over (kept for a look)
                continue
            if time.time() - t0 >= wait_sec:
                raise Busy(f"Studio 사용 중({held.get('who', '?')} · {held.get('t', '?')}) — {wait_sec}s 기다림")
            time.sleep(poll)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.replace(p, p.with_suffix(".lock.done"))  # release = move away (one atomic step, no delete)


def frames(client, sid, edir, tag, n=1, interval=0.0, first_name="01"):
    """Capture n frames into edir, `interval` s apart. -> (file names, note). n=1 keeps v4's single `01.jpg`."""
    n = max(1, min(int(n or 1), MAX_FRAMES))
    names, note = [], ""
    for k in range(n):
        if k:
            time.sleep(max(0.0, float(interval or 0)))
        try:
            err, content = client.call("screen_capture", {"studio_id": sid, "capture_id": f"AW_{tag}_{k + 1}"})
        except StudioError as e:
            note += f" (스크린샷 {k + 1}/{n} 실패: {e})"
            continue
        img = next((c for c in content if c.get("type") == "image"), None)
        if not img or err:
            note += f" (스크린샷 {k + 1}/{n} 실패)"
            continue
        ext = ".png" if "png" in (img.get("mimeType") or "") else ".jpg"
        name = (first_name if n == 1 else f"{k + 1:02d}") + ext
        edir.mkdir(parents=True, exist_ok=True)
        (edir / name).write_bytes(base64.b64decode(img["data"]))
        names.append(name)
    return names, note
