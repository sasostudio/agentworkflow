"""Git READ-ONLY helpers. The engine never commits/pushes/resets (design §2)."""

import hashlib
import subprocess
from pathlib import Path

_ALLOWED = {"rev-parse", "status", "diff", "ls-files", "branch", "show"}  # v6: check-ignore went with the preplan


def git(target, *args, check=True):
    if args[0] not in _ALLOWED:
        raise RuntimeError(f"엔진은 git 읽기 명령만 사용: {args[0]}")
    r = subprocess.run(["git", "-C", str(target), "-c", "core.quotepath=false", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {r.stderr.strip()}")
    return r.stdout


def is_repo(target):
    try:
        return git(target, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except RuntimeError:
        return False


def repo_root(target):
    return Path(git(target, "rev-parse", "--show-toplevel").strip())


def current_branch(target):
    return git(target, "branch", "--show-current").strip()


def head(target):
    return git(target, "rev-parse", "HEAD", check=False).strip()


def dirty_paths(target):
    """Paths (relative to target) that differ from HEAD, incl. untracked (not ignored)."""
    out = git(target, "status", "--porcelain", "--untracked-files=all", "--", ".")
    paths = []
    for line in out.splitlines():
        if not line.strip():
            continue
        p = line[3:]
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        paths.append(p.strip().strip('"'))
    root = repo_root(target)
    rel_prefix = Path(target).resolve().relative_to(root.resolve()) if Path(target).resolve() != root.resolve() else None
    if rel_prefix:
        pre = rel_prefix.as_posix() + "/"
        paths = [p[len(pre):] if p.startswith(pre) else p for p in paths]
    return sorted(set(paths))


def _hash(path):
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None


def snapshot(target):
    """{relpath: sha1|None} for every currently-dirty path. Used to find per-round changes."""
    t = Path(target)
    return {p: _hash(t / p) for p in dirty_paths(target)}


def changed_since(target, snap):
    now = snapshot(target)
    changed = [p for p, h in now.items() if snap.get(p, "__absent__") != h]
    # a path dirty before and clean now also changed (reverted to HEAD)
    changed += [p for p in snap if p not in now]
    return sorted(set(changed))


def file_hash(target, relpath):
    return _hash(Path(target) / relpath)


def diff_text(target, paths, max_chars=60000):
    """Unified diff vs HEAD for tracked paths + full text for untracked ones (truncated)."""
    return diff_with_omitted(target, paths, max_chars)[0]


def diff_with_omitted(target, paths, max_chars=60000):
    """-> (diff text, paths whose diff did not fully fit under max_chars). max_chars=None = no cut.
    §23-6 (4) 09-25: F-05 sent 60,000 of 137,938 chars — 14 of 33 files never reached the reviewer unnamed."""
    t = Path(target)
    tracked = set(git(target, "ls-files", "--", *paths).splitlines()) if paths else set()
    pieces = []  # (path, text) in the old order: tracked (sorted) first, then untracked in given order
    for p in sorted(tracked):
        pieces.append((p, git(target, "diff", "HEAD", "--", p, check=False).rstrip("\n")))
    for p in paths:
        if p in tracked:
            continue
        f = t / p
        if f.is_file():
            try:
                body = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                body = "(바이너리 파일)"
            pieces.append((p, f"--- /dev/null\n+++ b/{p}  (새 파일)\n{body}"))
        else:
            pieces.append((p, f"--- a/{p}\n+++ /dev/null  (삭제됨)"))
    pieces = [(p, s) for p, s in pieces if s]  # a tracked path with no diff adds nothing (as before)
    text = "\n".join(s for _, s in pieces)
    if max_chars is None or len(text) <= max_chars:
        return text, []
    omitted, pos = [], 0
    for p, s in pieces:
        end = pos + len(s)
        if end > max_chars:
            omitted.append(p)
        pos = end + 1
    return text[:max_chars] + f"\n... (잘림: 전체 {len(text)}자 — 파일은 Read로 직접 확인)", omitted
