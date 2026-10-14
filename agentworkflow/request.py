"""00-request/request.md — required sections checked by code (design §6)."""

import re
from pathlib import Path

REQUIRED = ["목표", "구현 범위", "범위 외", "주의", "QA 시나리오 힌트", "난이도", "항목"]
# v6 §6-2: `## 항목` = the Master's TODO in the engine format (always — there is no planner AI) · `## 흐름` = only
# `QA:` / `Wiki:` (flow.py). Sections of removed parts are refused (A3) — a request must not silently lose them.
REMOVED = {"실행 방식": "병렬·순차·대형 폐지(v6) — 묶음을 나누는 것은 Master", "역할·모델": "Master는 모델을 정하지 않는다(09-29)",
           "강도": "v2에서 폐지 — `## 난이도`에 L1~L3"}
# v5 (10-01): `## 유형` 시각 / 원작 재현 = visual work — high floor · the builder may look at the running result (when
# the runner's adapter offers it — qa.CAPS) · the evaluator compares captures with
# `## 원작 근거` (frame sheets / images, one path per line). 원작 재현 needs 원작 근거.
# `## 난이도` = run level L1~L3 (design §17-1). Master proposes, the user approves it at the gate.

_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.M)


class RequestError(Exception):
    pass


def parse_sections(text):
    heads = list(_HEADING.finditer(text))
    out = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out[m.group(1).strip()] = text[m.end():end].strip()
    return out


def load_request(path):
    p = Path(path)
    if not p.is_file():
        raise RequestError(f"request.md 없음: {p}")
    text = p.read_text(encoding="utf-8")
    sections = parse_sections(text)
    gone = [f"`## {k}` — {why}" for k, why in REMOVED.items() if k in sections]
    if gone:
        raise RequestError("v6에서 없앤 절:\n- " + "\n- ".join(gone))
    missing = [s for s in REQUIRED if not sections.get(s)]
    if missing:
        raise RequestError("request.md 필수 절 누락/빈 절: " + ", ".join(f"## {s}" for s in missing))
    m = re.search(r"^\s*[-*]?\s*L([1-3])(?!\d)", sections["난이도"], re.M)  # the line that starts with L1..L3
    if not m:
        raise RequestError("`## 난이도` 첫 줄에 L1 / L2 / L3 중 하나가 필요 (설계서 §17-1 기준표)")
    title = re.search(r"^#\s+(.+)$", text, re.M)
    kind, refs = parse_kind(sections.get("유형", ""), sections.get("원작 근거", ""))
    return {"text": text, "sections": sections, "visual": kind is not None, "kind": kind, "references": refs,
            "run_level": int(m.group(1)), "title": title.group(1).strip() if title else p.stem}


KINDS = ("원작 재현", "시각")
_TICKED = re.compile(r"`([^`]+)`")


def parse_kind(kind_body, refs_body):
    """-> (kind | None, [reference paths]). kind = the first of KINDS named in `## 유형`; other text = not visual."""
    kind = next((k for k in KINDS if k in (kind_body or "")), None)
    refs = []
    for line in (refs_body or "").splitlines():  # a `ticked` path wins; else the line's first word after "- "
        ticked = _TICKED.findall(line)
        words = ticked or line.strip().lstrip("-*").split()[:1]
        refs += [s.strip() for s in words if s.strip() and ("/" in s or "\\" in s or "." in s)]
    if kind == "원작 재현" and not refs:
        raise RequestError("`## 유형` 원작 재현 이면 `## 원작 근거`에 원작 프레임(시트·이미지) 경로가 한 줄 이상 필요")
    return kind, refs
