"""Machine checks (build/test) run by the ENGINE, never by an agent (design §5-2)."""

import os
import re
import subprocess
import time
from pathlib import Path


def run_checks(commands, cwd, timeout_sec, log_path=None):
    """Run each shell command in order; stop at first failure. Returns (ok, summary_text)."""
    lines = []
    ok = True
    for cmd in commands:
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=str(cwd), shell=True, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout_sec)
            code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired as e:
            code, out = -1, f"타임아웃 {timeout_sec}s\n{e.stdout or ''}"
        dt = time.time() - t0
        lines.append(f"$ {cmd}\n(exit {code}, {dt:.1f}s)\n{out}")
        if code != 0:
            ok = False
            break
    text = "\n\n".join(lines) if lines else "(기계검사 명령 없음)"
    if log_path:
        log_path.write_text(text, encoding="utf-8")
    return ok, text


# Path tokens: split on separators first, then test each token whole — no nested quantifier over the line.
# (09-26: the first version `(?:[\\/]?[\w.\-]+)+\.\w{1,6}` backtracked exponentially on a luau-lsp line and hung
# the engine 34 min in F-10 run003 — §23-12 #11.) Handles `src\a.luau:`, `src/c.luau:1:2`, `C:\...\b.luau [game/..](3,4)`.
_SPLIT = re.compile(r"[\s\[\]()\"',;]+")
_LINECOL = re.compile(r"(?::\d+)+:?$")
_FILE = re.compile(r"(?:[A-Za-z]:)?[^:*?<>|]*\.\w{1,6}")


def _path_tokens(line):
    for tok in _SPLIT.split(line):
        tok = _LINECOL.sub("", tok.strip("┌─│")).rstrip(":")
        if tok and _FILE.fullmatch(tok):
            yield tok


# v4 §9 (R-A): a line names a failing file only in one of the shapes our tools really print (survey of every check
# log of a real Roblox project) — StyLua `Diff in <path>:` · selene `┌─ <path>:l:c` (under `error[..]`/`warning[..]`) · luau-lsp
# `<path> [game/..](l,c): TypeError: …` / `<path>(l,c): …Error`. Anything else (e.g. luau-lsp's
# "Created sourcemap at sourcemap.json" — §23-12 #7) is ignored. Plain substring tests + one anchored short regex.
_LSP_POS = re.compile(r"\(\d{1,7},\d{1,7}\): ")
_MAX_LINE = 4000  # bound the work per line (§23-12 #11): a diagnostic's path is always near the start


def _diagnostic(line):
    s = line.lstrip()
    if s.startswith("Diff in ") or s.startswith("┌─ "):
        return True
    return bool(_LSP_POS.search(line)) and ("Error" in line or "error" in line or "Warning" in line)


def error_files(log, root):
    """C-5 (09-25): files a failed check log points at, as repo-relative posix paths that exist under `root`.
    Only diagnostic lines count (v4 §9 — tool output is read by shape, not by guessing path-looking words)."""
    base = Path(root).resolve()
    found = []
    for line in log.splitlines():
        line = line[:_MAX_LINE]
        if line.startswith("$ ") or not _diagnostic(line):
            continue
        for tok in _path_tokens(line):
            p = Path(tok)
            if p.is_absolute():
                try:
                    rel = Path(os.path.relpath(p, base))
                except ValueError:  # another drive
                    continue
                if rel.parts and rel.parts[0] == "..":
                    continue
            else:
                rel = p
            if (base / rel).is_file():
                s = rel.as_posix()
                if s not in found:
                    found.append(s)
    return sorted(found)


def tail(text, n=4000):
    return text if len(text) <= n else "...(앞부분 생략)\n" + text[-n:]
