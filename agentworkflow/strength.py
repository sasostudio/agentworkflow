"""Difficulty level of a development round (design §17-1 ③ and L0).

The run level (user-approved) and the Master's item levels (v6 `## 항목`) come in as `item_level`;
code can only RAISE it: high-risk paths -> L3, repeated check failures -> L2. Docs-only -> at least L1 (v5).
(v1's strength 0/1/2 dial was folded into this — decision 16.)
"""

from pathlib import PurePosixPath


def _is_high_risk(path, patterns):
    p = path.replace("\\", "/").lower()
    for pat in patterns:
        q = pat.replace("\\", "/").lower()
        if q.endswith("/"):
            if p.startswith(q) or ("/" + q) in ("/" + p):
                return True
        elif p == q or p.endswith("/" + q) or PurePosixPath(p).match(q):
            return True
    return False


DOCS_ONLY = "문서만 변경"


def judge_level(changed_files, cfg, check_fails_total, item_level):
    """Returns (level 1..3, reason). Never lowers item_level.
    v5 (10-01): docs-only is NOT skipped any more (was L0 = no review) — 컷신 실패의 출발점이 「L1 문서만」으로 검수 0회를
    지난 원작 분석 문서였다. A docs-only round is reviewed at least at L1, against its source (request · original)."""
    exts = tuple(e.lower() for e in cfg.get("doc_extensions", []))
    if changed_files and all(f.lower().endswith(exts) for f in changed_files):
        return max(1, item_level or 0), f"{DOCS_ONLY} — 요청·원작과 대조(v5: 문서도 검수)"
    level, why = item_level, [f"항목 L{item_level}"]
    hr = [f for f in changed_files if _is_high_risk(f, cfg.get("high_risk_paths", []))]
    if hr and level < 3:
        level = 3
        why.append("고위험 경로 → L3: " + ", ".join(hr[:5]))
    if check_fails_total >= 2 and level < 2:
        level = 2
        why.append(f"기계검사 실패 누적 {check_fails_total}회 → L2")
    return level, " · ".join(why)
