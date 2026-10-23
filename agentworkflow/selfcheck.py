"""v4 §11 — the dev AI's self-check: the ONE shell command a work role may run.

Measured 09-27 (haiku · dontAsk · allowed `Bash(python check.py:*)`): the allowed command runs; `echo … > file`
and a compound `allowed && echo … > file` are both refused, and no file is written. So a developer is allowed
exactly `python <this file> <files…>`, and the engine then runs the same `run()` itself and trusts only that.

engine.json `self_check`:
  commands     command templates, run WITHOUT a shell (split like a POSIX shell): `{files}` = the files to check as
               separate arguments · `{tmp}` = a fresh folder for this one check (parallel workers never share generated
               files such as a sourcemap). Use forward slashes. A relative program path is taken from the target folder.
  extensions   which files are checked (others are skipped) · timeout_sec per command · max_fixes = how many times the
               engine sends a failing developer back to its own session before the layer check takes over.
The AI's copy of the config comes through the environment variable AW_SELFCHECK (a JSON file in the run folder).
Stdlib only (+ procutil, itself stdlib only): this file also runs as a plain script inside the AI's shell.
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from . import procutil
except ImportError:  # run as a plain script (the AI's shell): its own folder is on sys.path
    import procutil

ENV = "AW_SELFCHECK"
SCRIPT = Path(__file__).resolve().as_posix()


def command_prefix():
    """What the developer types (and the Bash permission prefix)."""
    return f"python {SCRIPT}"


def select(files, extensions, cwd):
    """-> (files to check, refused). Only existing files inside cwd, given as relative paths; never an argument that
    looks like an option (it would reach the tools as a flag)."""
    root = Path(cwd).resolve()
    out, bad = [], []
    for f in files:
        f = str(f or "").strip().replace("\\", "/")
        if f.startswith("./"):
            f = f[2:]
        if not f or f.startswith("-") or Path(f).is_absolute():
            bad.append(f or "(빈 값)")
            continue
        p = (root / f).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            bad.append(f)
            continue
        if not p.is_file():
            bad.append(f)
            continue
        if extensions and not f.lower().endswith(tuple(e.lower() for e in extensions)):
            continue
        if f not in out:
            out.append(f)
    return out, bad


def _argv(template, files, tmp, cwd):
    argv = []
    for tok in shlex.split(template, posix=True):
        if tok == "{files}":
            argv += files
        else:
            argv.append(tok.replace("{tmp}", tmp))
    if argv and ("/" in argv[0] or "\\" in argv[0]) and not Path(argv[0]).is_absolute():
        argv[0] = str((Path(cwd) / argv[0]).resolve())  # Windows resolves a relative program against the parent's dir
    return argv


def run(cfg, files, cwd, tmp_root):
    """Every command runs (a type error and a lint error are worth reporting together). -> (ok, log)."""
    sel, bad = select(files, cfg.get("extensions") or [], cwd)
    lines = ["검사할 수 없는 경로(대상 폴더 안의 실제 파일을 상대 경로로): " + ", ".join(bad)] if bad else []
    if not sel:
        return (not bad), "\n".join(lines + ["검사할 파일 없음 — 자기 검사 " + ("실패" if bad else "통과")])
    Path(tmp_root).mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sc_", dir=str(tmp_root))).as_posix()
    ok = not bad
    for tpl in cfg["commands"]:
        argv = _argv(tpl, sel, tmp, cwd)
        t0 = time.time()
        try:
            r = procutil.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=cfg.get("timeout_sec", 120))
            code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except FileNotFoundError as e:
            code, out = -2, f"명령을 찾지 못함: {e}"
        except subprocess.TimeoutExpired:
            code, out = -1, f"타임아웃 {cfg.get('timeout_sec', 120)}s"
        lines.append(f"$ {' '.join(argv)}\n(exit {code}, {time.time() - t0:.1f}s)\n{out.strip()[-3000:]}")
        ok = ok and code == 0
    lines.append(f"자기 검사 {'통과' if ok else '실패'} — 파일 {len(sel)}개")
    return ok, "\n\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    path = os.environ.get(ENV)
    if not path or not Path(path).is_file():
        print("자기 검사 설정 없음(AW_SELFCHECK) — 엔진 run 안에서만 쓴다")
        return 2
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    ok, log = run(cfg, argv, os.getcwd(), cfg["tmp_root"])
    print(log)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
