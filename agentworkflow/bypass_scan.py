"""code-health axis L (bypass traces) on the lines a development round ADDED (decision 4 · 09-24).

The engine only finds; it does not judge. Findings go to the dev reviewer ("reject unless justified")
and to report.md. Auto-reject was refused: a justified `any` (untyped library) would loop forever.
Only `+` lines of the round's diff are scanned, so old code never produces noise.
"""

import re
from pathlib import PurePosixPath

_COMMON = [("미루기 표시", re.compile(r"\b(TODO|FIXME|HACK|XXX)\b"))]
_BY_EXT = {
    (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"): [
        ("빈 catch", re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*\}")),
        ("any 타입", re.compile(r"(:\s*any\b|\bas\s+any\b|<any>)")),
        ("타입 검사 끄기", re.compile(r"@ts-(ignore|expect-error|nocheck)")),
        ("린터 끄기", re.compile(r"eslint-disable|oxlint-disable")),
    ],
    (".py",): [
        ("예외 삼키기", re.compile(r"except[^:\n]*:\s*(pass|\.\.\.)\s*$", re.M)),
        ("맨 except", re.compile(r"except\s*:")),
        ("타입 검사 끄기", re.compile(r"#\s*type:\s*ignore")),
        ("린터 끄기", re.compile(r"#\s*noqa")),
    ],
    (".gd",): [("경고 끄기", re.compile(r"@warning_ignore"))],
    (".cs", ".java", ".kt"): [
        ("빈 catch", re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*\}")),
        ("경고 끄기", re.compile(r"#pragma\s+warning\s+disable|@Suppress(Warnings)?\b")),
    ],
}


def _rules(path):
    ext = PurePosixPath(path).suffix.lower()
    for exts, rules in _BY_EXT.items():
        if ext in exts:
            return rules + _COMMON
    return []  # docs / unknown types: not scanned


def added_blocks(diff):
    """{path: added text} from a unified diff (+ lines only; new files shown in full by gitutil.diff_text)."""
    out, cur, whole = {}, None, False
    for line in diff.splitlines():
        if line.startswith("diff --git") or line.startswith("--- "):
            cur, whole = None, False
            continue
        if line.startswith("+++ "):
            name = line[4:].split("  (")[0].strip()
            cur = name[2:] if name.startswith("b/") else (None if name == "/dev/null" else name)
            whole = "(새 파일)" in line  # gitutil.diff_text prints untracked files' whole body without '+'
            if cur:
                out.setdefault(cur, [])
            continue
        if not cur:
            continue
        if whole:
            out[cur].append(line)
        elif line.startswith("+"):
            out[cur].append(line[1:])
    return {p: "\n".join(v) for p, v in out.items()}


def scan(diff):
    """-> list of (path, kind, line text)."""
    found = []
    for path, text in added_blocks(diff).items():
        for kind, rx in _rules(path):
            for m in rx.finditer(text):
                start = text.rfind("\n", 0, m.start()) + 1
                end = text.find("\n", m.end())
                snippet = " ⏎ ".join(s.strip() for s in text[start: end if end != -1 else None].splitlines())
                found.append((path, kind, snippet[:160]))
    return found


def as_text(found):
    return "\n".join(f"- `{p}` · {k}: `{t}`" for p, k, t in found)
