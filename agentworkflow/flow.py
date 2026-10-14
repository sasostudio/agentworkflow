"""v6 §1 · §6-2 (10-02) — the shape of a run. If a run skipped or ran a part unexpectedly, read this file.

The TODO is always the Master's `## 항목` (there is no planner AI in v6). request.md (optional):
    ## 흐름
    - QA: 있음 | 없음       (default 있음 — the evaluator writes scenarios and the runner plays them)
    - Wiki: 있음 | 없음     (default 있음 — the builder updates the wiki at the end of BUILD, in its own session)

    ## 항목                  (required — the TODO in the engine's format, see todo.py)
    - [ ] DEV-001 [L1] 할 일
      - 대상: src/a.luau
      - 완료 조건: 무엇을 보면 끝났나
      - 기능 위치: src/a.luau 의 f()
      - 선행: 없음

The one minimum the engine keeps whatever the Master wrote (named in the timeline): `## 유형` 시각 / 원작 재현 → QA on
(the evaluator compares captures). v6 B1: the v5 rule "L2+ needs a `## 설계` section" is not applied — the builder
designs inside its own work.
"""

import re

from . import todo
from .request import RequestError

_LINE = re.compile(r"^\s*[-*]?\s*(QA|Wiki)\s*[:：]\s*(\S+)", re.M | re.I)
_ON = {"있음": True, "켬": True, "on": True, "없음": False, "끔": False, "off": False}
_STALE = re.compile(r"^\s*[-*]?\s*흐름\s*[:：]", re.M)


def parse(body):
    """`## 흐름` body -> {"qa"?, "wiki"?}. Unknown values and the v5 `흐름:` line are errors (a typo must not pick)."""
    if _STALE.search(body or ""):
        raise RequestError("`## 흐름`의 `흐름: 단순/설계` 줄은 v6에서 폐지 — `QA:`·`Wiki:`만 쓴다")
    out = {}
    for key, val in _LINE.findall(body or ""):
        v = val.strip().rstrip(".,")
        if v.lower() not in _ON and v not in _ON:
            raise RequestError(f"`## 흐름` {key}: {v} — 있음 / 없음")
        out[key.lower()] = _ON.get(v, _ON.get(v.lower()))
    return out


def master_items(req):
    """The Master's TODO from `## 항목` -> parsed items (todo.parse)."""
    return todo.parse(req["sections"].get("항목", ""))


def master_plan_text(req):
    return "# 계획\nMaster 작성 TODO (v6 — `## 항목`)\n\n" + req["sections"].get("항목", "").strip() + "\n"


def item_problems(req, cfg):
    """Everything wrong with `## 항목` that the Master must fix before the run starts (v6 P17: checked before the
    run folder exists, so a wrong request never leaves a run that repeats the same error on every resume)."""
    items, errs = master_items(req)
    v = errs + todo.check_plan("", master_plan_text(req), req["run_level"], require_design=False)
    v += todo.contract_problems([{"id": i["id"], "text": i["text"], "targets": i["target_files"]} for i in items],
                                cfg.get("contract_files"))
    return v


def decide(req, cfg):
    """-> {"qa", "wiki", "forced": [reasons]}. Raises RequestError for a request the Master must fix first."""
    raw = parse(req["sections"].get("흐름", ""))
    f = {"qa": raw.get("qa", True), "wiki": raw.get("wiki", True), "forced": []}
    if req.get("visual") and not f["qa"]:
        f["qa"] = True
        f["forced"].append(f"시각 작업({req.get('kind')}) → QA 켬(캡처 대조 필수)")
    v = item_problems(req, cfg)
    if v:
        raise RequestError("`## 항목` 오류:\n- " + "\n- ".join(v))
    return f


def note(f):
    return (f"QA {'켬' if f['qa'] else '끔'} · Wiki {'켬' if f['wiki'] else '끔'}"
            + ("".join(f" · ⚠️{r}" for r in f["forced"]) if f["forced"] else ""))
