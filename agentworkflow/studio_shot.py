"""v5 D8 — ROBLOX adapter's `dev_shot` (qa.CAPS): the builder looks at the game while it builds. The one
command a developer on a visual run may type (like selfcheck.py): play → run the builder's Luau → capture N frames
(+ sheet) → stop → print the images.

dev_shot interface (what engine._invoke uses — another target's module offers the same names):
    ENV · DRAFT_DIR · command_prefix() · config_for(cfg, target, shots_dir) · rule(references)

Why a script and not the Studio MCP tools themselves (current choice): the engine keeps the same guarantees as its QA runner — one Studio at a time (studio_capture.studio_lock:
parallel workers and QA take turns) · Edit mode first (a running play = someone is using it) · files == Studio
(Rojo) · the play ALWAYS stopped. Whether raw MCP tools can be limited to the play mode is unmeasured.

The Luau comes as a file the builder writes into `.aw-shot/` of the target (its only extra file permission: the
Edit+Write pair on `.aw-shot/**` — measured 10-01 with the engine's own argv). The script MOVES it into the run folder
at once, so the project tree stays clean. (Correction 10-01 23:4x: the first probe said a rule for a folder OUTSIDE
the working folder is denied in every spelling — that probe gave `Write(...)` without its `Edit(...)` pair. With the
pair + `--add-dir`, `C:/…` is allowed too. `.aw-shot` is kept: it works and needs no extra working folder.)

    python <this file> --code .aw-shot/DEV-101_1.luau [--frames 4 --interval 0.5]
"""

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path

ENV = "AW_STUDIOSHOT"
SCRIPT = Path(__file__).resolve().as_posix()
DRAFT_DIR = ".aw-shot"


def command_prefix():
    """What the developer types (and the Bash permission prefix)."""
    return f"python {SCRIPT}"


def config_for(cfg, target, shots_dir):
    """The engine's copy for the AI (engine.json values — the same Studio the QA runner uses)."""
    qa, rb = cfg["qa"], cfg["qa"]["roblox"]
    return {"target": Path(target).as_posix(), "shots_dir": Path(shots_dir).as_posix(),
            "studio_name": rb.get("studio_name"), "mcp_command": rb["mcp_command"],
            "sourcemap_command": rb["sourcemap_command"], "sync_check": rb.get("sync_check", True),
            "sync_wait_sec": rb.get("sync_wait_sec", 15), "ready_sec": rb["play_ready_timeout_sec"],
            "timeout_sec": qa["scenario_timeout_sec"], "lock_wait_sec": rb.get("lock_wait_sec", 900),
            "reset_code": rb.get("reset_code")}  # v6 N14: the builder's looks clean up like the QA plays


def rule(refs):
    """Appended to the builder's prompt on a visual run (Roblox)."""
    cmd = command_prefix()
    return (
        "\n\n## 화면 확인 (시각 작업 · 로블록스)\n"
        f"- 만들면서 게임 화면을 직접 볼 수 있다: `{cmd} --code {DRAFT_DIR}/<파일> [--frames N --interval 초 "
        "--context Server|Client]` (대상 폴더 기준 상대 경로 · 다른 명령·`&&` 연결은 거부된다). 엔진 스크립트가 Studio에서 "
        "플레이를 시작해 그 Luau를 돌리고, 캡처한 뒤 플레이를 멈추고, 이미지 경로를 출력한다 — 그 이미지를 Read로 연다.\n"
        f"- Luau는 먼저 `{DRAFT_DIR}/<항목ID>_<번호>.luau` 로 Write 한다(이 폴더만 추가로 쓸 수 있다 · 게임 파일이 아니다). "
        "QA 시나리오처럼 `return { status = \"PASSED\", note = \"무엇을 띄웠나\" }` 로 끝낸다. 스크립트가 실행 즉시 그 "
        "파일을 run 폴더로 옮기므로 프로젝트에는 남지 않는다.\n"
        "- Studio는 한 번에 하나만 쓴다 — 다른 워커·QA가 쓰고 있으면 스크립트가 기다린다. 필요한 만큼만 찍는다.\n"
        + ("- 원작 근거: " + ", ".join(f"`{r}`" for r in refs) + " — 찍은 화면을 이것과 대조하며 맞춘다.\n" if refs else "")
        + "- Studio에 최신 파일이 반영 안 됐다고 나오면(Rojo) 잠시 뒤 다시 찍는다. 계속이면 `question`으로 알린다.\n"
    )


def _take(code_arg, c):
    """The draft -> the run folder (moved, not copied). -> (moved path, error)."""
    target = Path(c["target"]).resolve()
    p = (target / code_arg).resolve() if not Path(code_arg).is_absolute() else Path(code_arg).resolve()
    drafts = (target / DRAFT_DIR).resolve()
    if drafts not in p.parents:
        return None, f"--code 는 `{DRAFT_DIR}/` 안의 파일만(받은 것: {code_arg})"
    if not p.is_file():
        return None, f"파일 없음: {code_arg} — 먼저 `{DRAFT_DIR}/<항목ID>_<번호>.luau` 로 Write"
    shots = Path(c["shots_dir"])
    shots.mkdir(parents=True, exist_ok=True)
    n = len([d for d in shots.iterdir() if d.is_dir()]) + 1
    dest = shots / f"{n:03d}_{p.stem}"
    dest.mkdir(parents=True, exist_ok=True)
    moved = dest / "code.luau"
    os.replace(p, moved)
    return moved, None


def shoot(c, code_file, frames=1, interval=0.0, context=None, client_factory=None):
    """-> (exit code, report text). Everything the AI needs is in the text (images as absolute paths)."""
    from . import roblox_runner as rr
    from . import studio_capture
    from .studio_mcp import StudioClient, StudioError, ToolFailed
    code = Path(code_file).read_text(encoding="utf-8")
    if context:
        code = f"-- context: {context}\n" + code
    edir = Path(code_file).parent
    factory = client_factory or (lambda: StudioClient(c["mcp_command"], timeout=max(60, c["timeout_sec"])))
    hold = (studio_capture.studio_lock(f"builder shot {edir.name}", c["lock_wait_sec"])
            if client_factory is None else contextlib.nullcontext())
    try:
        with hold, factory() as client:
            sid = rr.pick_studio(client, c.get("studio_name"))
            cur, _ = rr.mode(client, sid)
            if cur != "Edit":
                return 1, f"Studio가 Edit 모드가 아님({cur}) — 다른 쪽이 플레이 중. 잠시 뒤 다시"
            if c.get("sync_check", True):
                rr.check_sync(client, sid, c["target"], c["sourcemap_command"], c.get("sync_wait_sec", 15))
            r = rr.run_one(client, sid, rr.steps_of({}, code), c["timeout_sec"], c["ready_sec"], edir, "shot",
                           edir / "console.txt", frames, interval, reset_code=c.get("reset_code"))
    except (rr.Blocked, studio_capture.Busy) as e:
        return 1, f"찍지 못함: {e}"
    except (StudioError, ToolFailed) as e:
        return 1, f"Studio MCP 연결·호출 실패: {e}"
    from .qa import SHEET, make_sheet  # the sheet every runner's captures get
    sh = make_sheet(edir, r["evidence"])
    imgs = [(edir / n).as_posix() for n in r["evidence"] + ([sh] if sh else [])]
    sheet = [p for p in imgs if p.endswith(SHEET)]
    lines = [f"결과: {r['status']} — {r['note'] or '-'}",
             "이미지(Read로 연다): " + (", ".join(sheet + [p for p in imgs if p not in sheet]) or "(없음)"),
             f"콘솔: {(edir / 'console.txt').as_posix()}"]
    return (0 if imgs else 1), "\n".join(lines)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    path = os.environ.get(ENV)
    if not path or not Path(path).is_file():
        print("화면 확인 설정 없음(AW_STUDIOSHOT) — 엔진 run 안에서만 쓴다")
        return 2
    c = json.loads(Path(path).read_text(encoding="utf-8"))
    ap = argparse.ArgumentParser(prog="studio_shot")
    ap.add_argument("--code", required=True)
    ap.add_argument("--frames", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.0)
    ap.add_argument("--context", choices=["Server", "Client"])
    a = ap.parse_args(argv)
    moved, err = _take(a.code, c)
    if err:
        print(err)
        return 2
    t0 = time.time()
    rc, text = shoot(c, moved, a.frames, a.interval, a.context)
    print(text + f"\n({time.time() - t0:.0f}초)")
    return rc


if __name__ == "__main__":
    if __package__ in (None, ""):  # run as a file (the permission prefix is `python <file>`): import as a package
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from agentworkflow.studio_shot import main as _main
        sys.exit(_main())
    sys.exit(main())
