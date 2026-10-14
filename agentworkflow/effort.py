"""v5 D3·D4 · v6 §6-1 — the ONE place that decides a call's effort. If an effort looks wrong, read this.

Difficulty is the Master's: the request's `## 난이도` (run level) and the item levels in `## 항목`.
  작업자(builder)    medium for L1 items · high for L2+ items, the high floor, and a rework after the
                     `rework_high_after`-th evaluation reject of the run (v6: run-wide `eval_rejects` — B2)
  평가자(evaluator)  `levels.evaluate.N.effort` (medium by default, high for L3) · high when the high floor applies
High floor (engine code — the Master can only raise it): visual work (`## 유형` 시각 · 원작 재현) · an item that touches
`high_risk_paths` (save data, security…) · an item that touches `contract_files` (an agreement between modules)."""

from . import todo

ORDER = ("low", "medium", "high", "xhigh", "max")


def raise_to(effort, floor):
    """The higher of two efforts (None counts as medium)."""
    a, b = effort or "medium", floor or "medium"
    return a if ORDER.index(a) >= ORDER.index(b) else b


def _touches(items, patterns):
    pats = [p for p in patterns or [] if p]
    return [i["id"] for i in items or []
            if any(todo._in_target(t, p) or todo._in_target(p, t) for t in (i.get("target_files") or []) for p in pats)]


def floor_reasons(eng, items=None):
    """-> human-readable reasons the high floor applies (empty = it does not). items: the TODO items the call is
    about (None = only the run-wide reasons)."""
    hf = eng.cfg.get("high_floor") or {}
    out = []
    if hf.get("visual", True) and eng.req.get("visual"):
        out.append(f"시각 작업({eng.req.get('kind')})")
    if items:
        if hf.get("high_risk_paths", True):
            hit = _touches(items, eng.cfg.get("high_risk_paths"))
            if hit:
                out.append("고위험 경로(" + ", ".join(hit[:4]) + ")")
        if hf.get("contract_files", True):
            hit = _touches(items, eng.cfg.get("contract_files"))
            if hit:
                out.append("약속 파일(" + ", ".join(hit[:4]) + ")")
    return out


def builder(eng, items, rework=False):
    lvl = max([i.get("level") or eng.run_level for i in items or []] or [eng.run_level])
    hard = rework or lvl >= 2 or bool(floor_reasons(eng, items))
    return "high" if hard else "medium"


def evaluator(eng, base, items=None):
    """base = `levels.evaluate.N.effort`; the floor can only raise it."""
    return raise_to(base, "high") if floor_reasons(eng, items) else (base or "medium")
