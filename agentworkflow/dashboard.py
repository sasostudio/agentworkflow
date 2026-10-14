"""Engine dashboard (엔진_대시보드_설계서 · 10-03). A read-only local web page: Python standard library only, bound to
127.0.0.1, never calls an AI (0 tokens). The browser asks every 2 s; every answer is computed from files the engine
already writes (state.json · heartbeat.json · timeline.md · raw/ · 03-evaluate/) and the E1~E3 records (events.jsonl ·
live.json). Facts only: nothing here judges the work (v6 §0-1 ③) — the one estimate (a call's progress) says so.

Where to look when a panel is wrong:
  the value in the file is wrong       engine._event (E1) · engine._write_heartbeat (E2) · engine._live (E3)
  the value is computed wrong          this file — the panel's function (p_status · p_progress · p_now · p_flow ·
                                       p_calls · p_evals · p_log · p_campaign) · old runs: calls_from_timeline
  it is drawn wrong                    dashboard.html — the panel's draw function (drawStatus … drawCampaign)
"""

import json
import re
import statistics
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import todo
from .campaign import CAMPAIGNS, CampaignError, parse_plan
from .config import DEFAULTS, _merge
from .qa import SHEET, safe_id
from .roles import BUILDER
from .state import AI_LOG

HTML = Path(__file__).with_name("dashboard.html")
# not 8765: the web QA test's managed server binds 127.0.0.1:8765 — a dashboard left open there broke it (10-03)
DEFAULT_PORT = 8799
RUN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}/run\d{3}_[^/\\]+$")
CAMPAIGN_NAME = re.compile(r"^[\w\-가-힣]+$")
_TS = "%Y-%m-%d %H:%M:%S"


# ================= reading files =================
def _json(p, default=None):
    """A file the engine replaces whole (state · heartbeat · live · results). Missing or unreadable -> default."""
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _jsonl(p):
    """events.jsonl: one object per line. A line being written at this moment (no newline yet) is skipped."""
    try:
        text = Path(p).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.split("\n")[:-1]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _epoch(ts):
    try:
        return time.mktime(time.strptime(ts, _TS))
    except (TypeError, ValueError):
        return None


def timeline(rd):
    """timeline.md -> [(epoch, "YYYY-MM-DD HH:MM:SS", text)] — a line without a time stamp continues the one above."""
    try:
        lines = (Path(rd) / "timeline.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        m = re.match(r"^- `(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})` (.*)$", ln)
        if m:
            out.append([_epoch(m.group(1)), m.group(1), m.group(2)])
        elif out and ln.strip():
            out[-1][2] += "\n" + ln.strip()
    return [tuple(x) for x in out]


def limits_of(rd):
    """The run's limits as the engine used them: defaults + the engine.json copied into the run at its start."""
    raw = _json(Path(rd) / "00-request" / "engine.json", {}) or {}
    cfg = _merge(DEFAULTS, raw if isinstance(raw, dict) else {})
    return cfg["limits"], cfg["supervisor"]


def resolve_run(ai_log, run_id):
    """"YYYY-MM-DD/runNNN_title" -> the run folder inside ai_log, or None (anything else is refused)."""
    if not run_id or not RUN_ID.match(run_id):
        return None
    root = Path(ai_log).resolve()
    rd = (root / run_id).resolve()
    try:
        rd.relative_to(root)
    except ValueError:
        return None
    return rd if (rd / "state.json").is_file() else None


def inside(base, rel):
    """A file under `base` named by a relative path from the browser, or None (absolute · `..` · outside · missing)."""
    if not rel or rel.startswith(("/", "\\")) or ":" in rel:
        return None
    base = Path(base).resolve()
    p = (base / rel).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        return None
    return p if p.is_file() else None


# ================= calls: E1 records, or the timeline of an older run =================
# v6: 호출 `key`·kind model/effort … · 소요 N초 · 이어쓰기 — v5 and older lines have no `·kind` / 소요 / 이어쓰기
# (measured 10-03 on the 09-23 · 09-29 · 10-02 runs), so each field is read on its own
_CALL_LINE = re.compile(r"^호출 `(?P<key>[^`]+)`(?:·(?P<kind>\S+))? (?P<model>[^/\s]+)/(?P<effort>\S+)"
                        r"(?: → 실제 \*\*(?P<actual>[^*]+)\*\*)?(?P<rest>.*)$", re.S)
_VERDICT_LINE = re.compile(r"^판정 \*\*(?P<v>[A-Z_]+)\*\* \((?P<name>[A-Z0-9_]+)\)")


def _num_in(rx, text, scale=1.0):
    m = re.search(rx, text)
    return float(m.group(1)) * scale if m else None


def calls_from_timeline(tl, n_raw):
    """Runs from before E1 (no events.jsonl): the same facts read back from the timeline's call lines. seq (the raw
    file number) is given only when the line count matches the raw file count — otherwise it is not known."""
    calls = []
    for ep, _ts, text in tl:
        m = _CALL_LINE.match(text)
        if m:
            rest = m.group("rest") or ""
            sec = _num_in(r" · 소요 (\d+)초", rest)
            res = re.search(r" · (이어쓰기|새 세션)", rest)
            calls.append({
                "seq": None, "t0": (ep - sec) if (ep and sec is not None) else ep, "t1": ep, "sec": sec,
                "key": m.group("key"), "kind": m.group("kind") or m.group("key"), "model_set": m.group("model"),
                "model": m.group("actual") or m.group("model"),
                "effort": None if m.group("effort") == "None" else m.group("effort"),
                "resumed": (res.group(1) == "이어쓰기") if res else None,
                "cost": _num_in(r" · \$([\d.]+)", rest), "tokens": None,
                "cache_hit": _num_in(r"캐시 적중 (\d+)%", rest, 0.01),
                "u5h": _num_in(r" 5h (\d+)%", rest, 0.01), "u7d": _num_in(r" 7d (\d+)%", rest, 0.01),
                "verdict": None, "error": "끊김" in rest, "cut": None, "from_timeline": True})
            continue
        v = _VERDICT_LINE.match(text)
        if v:
            for c in reversed(calls):
                if c["kind"] == v.group("name") and c["verdict"] is None:
                    c["verdict"] = v.group("v")
                    break
    if len(calls) == n_raw:
        for i, c in enumerate(calls, 1):
            c["seq"] = i
    return calls


def run_calls(rd, tl):
    """-> (calls, source) — "events" (E1) · "timeline" (a run from before E1) · "none" (no call has ended yet — a new
    run's first call is still running: it is not an old run)."""
    ev = _jsonl(Path(rd) / "events.jsonl")
    if ev:
        return ev, "events"
    calls = calls_from_timeline(tl, len(list((Path(rd) / "raw").glob("*.response.json"))))
    return calls, ("timeline" if calls else "none")


def raw_file(rd, seq):
    if not seq:
        return None
    hit = sorted((Path(rd) / "raw").glob(f"{int(seq):03d}_*.response.json"))
    return f"raw/{hit[0].name}" if hit else None


# ================= panels =================
def p_status(st, hb, now, stall_sec):
    """① Is it running · stopped · why · what the Master does next (the commands are the ones cli.py accepts)."""
    status = st.get("status")
    age = (now - hb["epoch"]) if isinstance(hb, dict) and isinstance(hb.get("epoch"), (int, float)) else None
    stalled = status == "RUNNING" and (age is None or age > stall_sec)
    qs = st.get("questions") or []
    open_q = qs[-1]["q"] if qs and not qs[-1].get("a") else None
    kind = st.get("help_kind")
    todo_ = None
    if stalled:
        todo_ = (f"heartbeat {('기록 없음' if age is None else f'{age:.0f}초 없음')} — 엔진 프로세스가 죽었을 수 있음"
                 f"(감시자 기준 {stall_sec:.0f}초) · Master: status 확인 뒤 resume")
    elif status == "AWAITING_APPROVAL":
        todo_ = "끝 승인 대기 — Master: approve (후속 항목이면 --follow-up) 또는 reject --reason"
    elif status == "AWAITING_ANSWER" or (status == "HELP" and kind == "questions" and open_q):
        todo_ = "방향 질문 — Master: answer --text"
    elif status == "HELP" and kind == "eval_cap":
        todo_ = "평가 반려 상한 — Master: reject --reason(방향) 또는 포기 (resume 불가)"
    elif status == "HELP":
        todo_ = "Master 판단 필요 — 원인 해결 뒤 resume, 또는 reject --reason"
    elif status == "PAUSED_USAGE":
        pu = st.get("paused_usage") or {}
        todo_ = f"사용량 일시중지 — 리셋 {pu.get('reset', '?')} 뒤 재개 · Master: wait-reset --run (백그라운드)"
    gate = st.get("gate_pending") or {}
    return {"status": status, "stalled": stalled, "stage": st.get("stage"), "eval_no": st.get("eval_no"),
            "hb_age": age, "pid": (hb or {}).get("pid"), "reason": st.get("pause_reason"), "help_kind": kind,
            "todo": todo_, "question": open_q, "unsure": gate.get("unsure"),
            "branch": st.get("branch"), "target": st.get("target"), "run_level": st.get("run_level"),
            "flow_note": st.get("flow_note")}


# §4 run work rate: a position on the way, not a judgment. EVALUATE 55~95 by the step the evaluation is at.
_EVAL_POS = {"MAIN": 55, "RUN": 65, "MAIN_LOOKED": 75, "SECOND": 82, "DEVIL": 88}
# the step's own progress fills the way to the next position (rate_fill · the page draws it)
_EVAL_NEXT = {"MAIN": 65, "RUN": 75, "MAIN_LOOKED": 82, "SECOND": 88, "DEVIL": 95}


def _eval_phase(ev):
    ev = ev or {}
    phase = ev.get("phase") or "MAIN"
    return "MAIN_LOOKED" if phase == "MAIN" and ev.get("looked") else phase


def work_rate(stage, status, ev, checked, total):
    if stage == "COMPLETE" or status == "COMPLETE":
        return 100.0
    if status == "AWAITING_APPROVAL":
        return 95.0
    if stage == "PLANNING":
        return 5.0
    if stage == "BUILD":
        return 5.0 + (50.0 * checked / total if total else 0.0)
    if stage == "EVALUATE":
        return float(_EVAL_POS.get(_eval_phase(ev), 55))
    return 0.0


def _mtime(p):
    try:
        return p.stat().st_mtime
    except (OSError, ValueError):
        return 0.0


def _countable(target, items):
    """The open items' target files that can be counted: a plain relative path inside the repo. A glob, a folder, an
    absolute / UNC / `..` path names no one file — left out (a folder's own mtime does not move when a file in it does)."""
    out = set()
    for i in items:
        if i.get("checked"):
            continue
        for f in i.get("target_files") or []:
            pf = Path(f)
            if (any(c in f for c in "*?[") or f.endswith(("/", "\\")) or pf.is_absolute() or pf.drive
                    or f.startswith(("//", "\\\\")) or ".." in pf.parts or (target / pf).is_dir()):
                continue
            out.add(f)
    return sorted(out)


def _build_since(st, tl):
    """When this BUILD began (the newest `**… → BUILD**` timeline line; a run without one: its creation)."""
    for ep, _ts, text in reversed(tl or []):
        if ep and re.match(r"^\*\*[A-Z_]+ → BUILD\*\*", text):
            return ep
    return _epoch(st.get("created"))


def rate_fill(st, items, hb_calls, runner, build_since=None):
    """How far the current step has got on the way from work_rate to `next` -> None or {"next", "kind", ...}:
    "files" = the open items' target files changed since this BUILD began (a fact — changed, not done; counted from the
    BUILD's start, not the call's, so it holds between calls instead of dropping back) · "runner" = scenarios ended ·
    "estimate" = the running call's elapsed ÷ its usual time — the page takes that number from ③ (now_calls), where it
    moves every second. The page stops short of next."""
    if st.get("status") != "RUNNING":
        return None
    call = (list(hb_calls or []) or [None])[-1]
    stage = st.get("stage")
    if stage == "BUILD":
        target = Path(st["target"]) if st.get("target") else None
        files = _countable(target, items) if target else []
        if files and build_since:
            done = sum(1 for f in files if _mtime(target / f) >= build_since)
            return {"next": 55.0, "kind": "files", "done": done, "total": len(files)}
        if call and call.get("key") == BUILDER:
            return {"next": 55.0, "kind": "estimate", "key": call.get("key"), "call_kind": call.get("kind")}
        return None
    if stage == "EVALUATE":
        phase = _eval_phase(st.get("eval"))
        nxt = float(_EVAL_NEXT.get(phase, 65))
        if phase == "RUN":
            return ({"next": nxt, "kind": "runner", "done": runner["done"], "total": runner["total"]}
                    if runner and runner.get("total") else None)
        if call:
            return {"next": nxt, "kind": "estimate", "key": call.get("key"), "call_kind": call.get("kind")}
    return None


def p_progress(st, rd, lim, tl, now, hb_calls=(), runner=None):
    """② KPI tiles: work rate (§4 · + the step's own progress: rate_fill) · items · evaluation · rejects · scenario
    runs · calls · cost · elapsed."""
    plan = ""
    try:
        plan = (Path(rd) / "01-planning" / "todo.md").read_text(encoding="utf-8")
    except OSError:
        pass
    items = todo.parse(plan)[0] if plan else []
    checked = sum(1 for i in items if i["checked"])
    c = st.get("counters") or {}
    ev = st.get("eval") or {}
    start = _epoch(st.get("created"))
    end = now if st.get("status") == "RUNNING" else (tl[-1][0] if tl else None)
    return {"rate": round(work_rate(st.get("stage"), st.get("status"), ev, checked, len(items)), 1),
            "fill": rate_fill(st, items, hb_calls, runner, _build_since(st, tl)),
            "items": {"checked": checked, "total": len(items),
                      "list": [{"id": i["id"], "checked": i["checked"], "text": i.get("text", "")[:120]}
                               for i in items]},
            "eval_no": st.get("eval_no") or 0, "eval_phase": ev.get("phase"),
            "rejects": {"n": c.get("eval_rejects", 0), "cap": c.get("eval_reject_cap") or lim["max_eval_rejects"]},
            "runs": {"n": ev.get("runs", 0), "cap": lim["max_eval_runs"]},
            "calls": {"n": c.get("calls", 0), "cap": lim["max_calls_per_run"]},
            "cost": {"usd": c.get("cost_usd", 0.0), "unknown": c.get("cost_unknown", 0),
                     "cap": lim.get("max_cost_usd_per_run")},
            "self_fix": {"n": c.get("self_fix", 0), "cap": lim["max_self_fix"]},
            "retries": st.get("retries") or {},
            "elapsed": (end - start) if (start and end) else None}


def _median_sec(calls, key, kind):
    xs = [c["sec"] for c in calls if c.get("key") == key and c.get("kind") == kind and c.get("sec")]
    return statistics.median(xs) if xs else None


_RECENT = {}  # events.jsonl of another run -> (mtime, [(key, kind, sec)]) — read again only when the file changed


def _recent_secs(ai_log, skip, now, days=14):
    out = []
    for p in Path(ai_log).glob("*/run*/events.jsonl"):
        if p.parent == skip:
            continue
        m = _mtime(p)
        if now - m > days * 86400:
            continue
        hit = _RECENT.get(p)
        if not hit or hit[0] != m:
            hit = (m, [(c.get("key"), c.get("kind"), c["sec"]) for c in _jsonl(p)
                       if isinstance(c, dict) and isinstance(c.get("sec"), (int, float)) and c["sec"] > 0])
            _RECENT[p] = hit
        out += hit[1]
    return out


def _typical(calls, key, kind, rd, now):
    """The usual time of a call of this session and kind: this run's finished ones, else the last 14 days of runs
    (a run's first call — the long builder one — has none of its own yet) -> (seconds | None, "run" | "recent" | None)."""
    m = _median_sec(calls, key, kind)
    if m:
        return m, "run"
    rd = Path(rd)
    xs = [s for k, kd, s in _recent_secs(rd.parent.parent, rd, now) if k == key and kd == kind]
    return (statistics.median(xs), "recent") if xs else (None, None)


def p_now(st, hb, alive, live, calls, rd, now):
    """③ The calls running now (heartbeat — E2) with the tools they are using (live.json — E3). Progress is an
    ESTIMATE: elapsed ÷ the median of finished calls of the same session and kind (this run, else recent runs)."""
    out = []
    lv = (live or {}).get("calls") or []
    for c in ((hb or {}).get("calls") or []):
        t0 = c.get("epoch")
        el = (now - t0) if t0 else None
        typ, src = _typical(calls, c.get("key"), c.get("kind"), rd, now)
        tools = next((x.get("tools") for x in lv if x.get("key") == c.get("key") and x.get("kind") == c.get("kind")
                      and (not t0 or (x.get("tools") or [{}])[-1].get("epoch", 0) >= t0 - 1)), [])
        out.append({"key": c.get("key"), "kind": c.get("kind"), "since": c.get("since"), "model": c.get("model"),
                    "effort": c.get("effort"), "resumed": c.get("resumed"), "elapsed": el, "typical": typ,
                    "typical_src": src,
                    "estimate": (el / typ) if (el is not None and typ) else None, "tools": tools or []})
    runner = None
    ev = st.get("eval") or {}
    if alive and st.get("stage") == "EVALUATE" and ev.get("phase") == "RUN" and ev.get("attempt_dir"):
        ad = Path(ev["attempt_dir"])
        part = _json(ad / "partial_results.json", []) or []
        scen = _json(ad / "scenarios.json", []) or []
        runner = {"attempt": ad.name, "done": len(part) if isinstance(part, list) else 0,
                  "total": len(scen) if isinstance(scen, list) else 0,
                  "last": [{"id": r.get("id"), "status": r.get("status")} for r in (part or [])[-3:]
                           if isinstance(r, dict)]}
    return {"calls": out, "runner": runner, "alive": alive}


def p_flow(st, hb_calls, alive):
    """④ Where the run is on the v6 state machine (§1) and inside the current step — from history · eval · eval_log."""
    stage, status = st.get("stage"), st.get("status")
    hist = st.get("history") or []
    cnt = lambda a, b: sum(1 for h in hist if h.get("from") == a and h.get("to") == b)  # noqa: E731
    ev = st.get("eval") or {}
    eno = st.get("eval_no") or 0
    cur = [x for x in (st.get("eval_log") or []) if x.get("eval_no") == eno]
    last = lambda names: next((x["verdict"] for x in reversed(cur) if x.get("phase") in names), None)  # noqa: E731
    keys = [c.get("key") for c in hb_calls or []]
    kinds = [c.get("kind") for c in hb_calls or []]
    top = {"PLAN": stage == "PLANNING" and "active" or "done", "BUILD": None, "EVAL": None, "GATE": None, "DONE": None}
    if stage == "BUILD":
        top["BUILD"] = "active"
        top["EVAL"] = "done" if cnt("BUILD", "EVALUATE") else None
    elif stage == "EVALUATE":
        top["BUILD"] = "done"
        top["EVAL"] = "done" if status == "AWAITING_APPROVAL" else "active"
        top["GATE"] = "wait" if status == "AWAITING_APPROVAL" else None
    elif stage == "COMPLETE":
        top.update(BUILD="done", EVAL="done", GATE="done", DONE="complete")
    inner_kind = "BUILD" if stage in ("PLANNING", "BUILD") else "EVAL"
    inner_cur = None
    if stage == "BUILD":
        inner_cur = "WIKI" if "wiki" in kinds else ("W" if "builder" in keys else ("CHK" if alive else None))
    elif stage == "EVALUATE" and status != "AWAITING_APPROVAL":
        by_key = {"evaluator": "M", "evaluator:second": "S", "evaluator:devil": "D"}
        inner_cur = next((by_key[k] for k in keys if k in by_key), None) or \
            {"MAIN": "M", "RUN": "R", "SECOND": "S", "DEVIL": "D"}.get(ev.get("phase") or "MAIN")
    elif stage in ("EVALUATE", "COMPLETE"):
        inner_cur = "A"
    return {"stage": stage, "status": status, "alive": alive, "top": top, "current_top":
            {"PLANNING": "PLAN", "BUILD": "BUILD", "EVALUATE": "GATE" if status == "AWAITING_APPROVAL" else "EVAL",
             "COMPLETE": "DONE"}.get(stage),
            "edges": {"PB": cnt("PLANNING", "BUILD"), "BE": cnt("BUILD", "EVALUATE"), "EB": cnt("EVALUATE", "BUILD"),
                      "EG": 1 if (status == "AWAITING_APPROVAL" or stage == "COMPLETE") else 0,
                      "GD": 1 if stage == "COMPLETE" else 0},
            "came_back": bool(hist) and hist[-1].get("from") == "EVALUATE" and hist[-1].get("to") == "BUILD",
            "inner": {"kind": inner_kind, "current": inner_cur, "eval_no": eno,
                      "labels": {"M": last(("E1", "E2")), "S": last(("SECOND",)), "D": last(("DEVIL",)),
                                 "R": f"실행 {ev.get('runs', 0)}회" if ev.get("runs") else None,
                                 "A": "통과" if status in ("AWAITING_APPROVAL", "COMPLETE") else None},
                      "done": sorted({{"E1": "M", "E2": "M", "SECOND": "S", "DEVIL": "D"}.get(x.get("phase"), "")
                                      for x in cur} - {""})}}


def _attempts(rd):
    """03-evaluate/e{n}_a{m} in order -> [(path, results, verdict or None while it is running)]."""
    out = []
    for ad in (Path(rd) / "03-evaluate").glob("e*_a*"):
        m = re.match(r"^e(\d+)_a(\d+)$", ad.name)
        if not (m and ad.is_dir()):
            continue
        fin = _json(ad / "results_final.json")
        res = (fin or {}).get("results") if isinstance(fin, dict) else None
        if res is None:
            part = _json(ad / "partial_results.json", [])
            res = part if isinstance(part, list) else []
        out.append(((int(m.group(1)), int(m.group(2))), ad, res,
                    (fin or {}).get("verdict") if isinstance(fin, dict) else None))
    return [x[1:] for x in sorted(out, key=lambda x: x[0])]


def p_calls(rd, calls, hb_calls, st, now):
    """⑤ Gantt bars: finished calls (E1 or timeline) · running calls (heartbeat) · scenario runs (attempt folders)."""
    bars = []
    for c in calls:
        b = {k: c.get(k) for k in ("seq", "t0", "t1", "sec", "key", "kind", "model", "model_set", "effort", "cost",
                                   "cache_hit", "tokens", "verdict", "error", "cut", "resumed", "u5h", "u7d",
                                   "denials")}
        b["raw"] = raw_file(rd, c.get("seq"))
        bars.append(b)
    for c in hb_calls or []:
        if c.get("epoch"):
            bars.append({"key": c.get("key"), "kind": c.get("kind"), "t0": c["epoch"], "t1": now, "running": True,
                         "model": c.get("model"), "effort": c.get("effort"), "sec": now - c["epoch"]})
    ev = st.get("eval") or {}
    for ad, res, verdict in _attempts(rd):
        man = ad / "scenarios.json"
        fin = ad / "results_final.json"
        if not man.is_file():
            continue
        t0 = man.stat().st_mtime
        running = not fin.is_file() and ev.get("attempt_dir") and Path(ev["attempt_dir"]).name == ad.name
        if not fin.is_file() and not running:
            continue  # an interrupted attempt — its end is not known
        t1 = fin.stat().st_mtime if fin.is_file() else now
        bars.append({"key": "runner", "kind": ad.name, "t0": t0, "t1": t1, "sec": t1 - t0, "running": bool(running),
                     "verdict": verdict, "n": len(res),
                     "failed": sum(1 for r in res if isinstance(r, dict) and r.get("status") != "PASSED")})
    t_start = _epoch(st.get("created"))
    return {"bars": bars, "t_start": t_start, "now": now}


def p_evals(st, rd):
    """⑥ Verdict history (eval_log by evaluation) · scenario × attempt grid (results · evidence)."""
    hist = {}
    for x in st.get("eval_log") or []:
        f = f"03-evaluate/r{x.get('round')}_{x.get('phase')}.json"
        hist.setdefault(x.get("eval_no"), []).append({"phase": x.get("phase"), "verdict": x.get("verdict"),
                                                      "round": x.get("round"),
                                                      "file": f if (Path(rd) / f).is_file() else None})
    attempts, rows, order = [], {}, []
    for ad, res, verdict in _attempts(rd):
        attempts.append({"name": ad.name, "verdict": verdict, "running": verdict is None})
        for r in res:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            rid = r["id"]
            if rid not in rows:
                rows[rid] = {"id": rid, "title": r.get("title", ""), "kind": r.get("kind", ""), "cells": {}}
                order.append(rid)
            edir = f"03-evaluate/{ad.name}/evidence/{safe_id(rid)}"
            names = r.get("evidence") or []
            names = [n for n in names if n == SHEET] or names
            ev_files = [f"{edir}/{n}" for n in names if (Path(rd) / edir / n).is_file()]
            rows[rid]["cells"][ad.name] = {"status": r.get("status"), "note": str(r.get("note") or "")[:400],
                                           "env": bool(r.get("env")), "evidence": ev_files,
                                           "log": next((f"03-evaluate/{ad.name}/{p.name}" for p in
                                                        sorted((ad).glob(f"log_{safe_id(rid)}.txt"))), None)}
    return {"history": [{"eval_no": k, "steps": v} for k, v in sorted(hist.items(), key=lambda kv: kv[0] or 0)],
            "approvals": st.get("approvals") or [], "attempts": attempts, "rows": [rows[i] for i in order]}


_LOG_TYPES = (("stage", re.compile(r"^\*\*[A-Z_]+ → [A-Z_]+\*\*")), ("call", re.compile(r"^호출 `")),
              ("verdict", re.compile(r"^판정 \*\*")),
              # the engine's own reject lines (engine.py timeline calls) and the Master's (cli note "Master 반려 — …")
              ("reject", re.compile(r"^\*\*(평가 반려|자동반려|기계검사 실패)\*\*|^Master 반려")),
              ("stop", re.compile(r"^상태 \*\*|^감시자")), ("retry", re.compile(r"^\*\*(재시도|이어서)\*\*")))


def p_log(tl, n=300):
    """⑦ The timeline as it is (last n lines), each with a display type for the filter boxes."""
    out = []
    for _ep, ts, text in tl[-n:]:
        typ = next((t for t, rx in _LOG_TYPES if rx.search(text)), "other")
        out.append({"t": ts, "text": text[:1200], "type": typ})
    return out


def p_campaign(st, campaigns):
    """⑧ The campaign this run belongs to: features ✓ + commit (campaign.json `done`), the run's own features."""
    c = st.get("campaign") or {}
    name = c.get("name")
    if not name or not CAMPAIGN_NAME.match(name):
        return None
    d = _json(Path(campaigns) / name / "campaign.json")
    if not isinstance(d, dict):
        return None
    feats, plan_err = [], None
    try:
        feats = parse_plan((Path(campaigns) / name / "campaign.md").read_text(encoding="utf-8"))["features"]
    except (OSError, CampaignError) as e:
        plan_err = str(e)[:200]
    done = d.get("done") or {}
    if not feats:
        feats = [{"id": k, "title": "", "level": None} for k in done]
    mine = set(c.get("features") or [])
    return {"name": name, "status": d.get("status"), "runs": len(d.get("runs") or []), "plan_error": plan_err,
            "features": [{**f, "commit": done.get(f["id"]), "this_run": f["id"] in mine} for f in feats],
            "done": len(done), "total": len(feats), "log": (d.get("log") or [])[-6:]}


# ================= account usage (one value for the whole engine — not per run) =================
def account_usage(ai_log=AI_LOG, now=None, days=7):
    """① 5h / 7d: the newest usage reading of ANY run touched in the last `days` — usage.json (E4 · every reading,
    also mid-call) or the last call that carried one (events.jsonl / timeline for older runs). The dashboard never asks
    Claude, so between engine calls this is the last reading: the page shows its age and hides a window whose reset
    time passed (no reset time recorded -> older than the window's length = reset). -> {t, u5h, r5h, u7d, r7d, run} or
    None."""
    now = now or time.time()
    best = None
    for sp in Path(ai_log).glob("*/run*/state.json"):
        rd = sp.parent
        if now - sp.stat().st_mtime > days * 86400:
            continue
        cands = []
        u = _json(rd / "usage.json")
        if isinstance(u, dict) and isinstance(u.get("t"), (int, float)):
            fh, sd = u.get("five_hour") or {}, u.get("seven_day") or {}
            cands.append({"t": u["t"], "u5h": fh.get("u"), "r5h": fh.get("reset"), "u7d": sd.get("u"),
                          "r7d": sd.get("reset")})
        calls, _src = run_calls(rd, timeline(rd))
        c = next((c for c in reversed(calls) if c.get("u5h") is not None or c.get("u7d") is not None), None)
        if c and c.get("t1"):
            cands.append({"t": c["t1"], "u5h": c.get("u5h"), "r5h": c.get("r5h"), "u7d": c.get("u7d"),
                          "r7d": c.get("r7d")})
        for x in cands:
            if best is None or x["t"] > best["t"]:
                best = {**x, "run": f"{rd.parent.name}/{rd.name}"}
    return best


# ================= the whole page / the run list =================
def run_payload(rd, campaigns=CAMPAIGNS, now=None):
    now = now or time.time()
    rd = Path(rd)
    st = _json(rd / "state.json", {}) or {}
    hb = _json(rd / "heartbeat.json")
    live = _json(rd / "live.json")
    lim, sup = limits_of(rd)
    tl = timeline(rd)
    calls, source = run_calls(rd, tl)
    status = p_status(st, hb, now, float(sup["stall_sec"]))
    alive = st.get("status") == "RUNNING" and not status["stalled"]
    hb_calls = ((hb or {}).get("calls") or []) if alive else []
    now_calls = p_now(st, {"calls": hb_calls}, alive, live, calls, rd, now)
    return {"id": f"{rd.parent.name}/{rd.name}", "now": now, "source": source,
            "status": status, "progress": p_progress(st, rd, lim, tl, now, hb_calls, now_calls["runner"]),
            "now_calls": now_calls,
            "flow": p_flow(st, hb_calls, alive), "calls": p_calls(rd, calls, hb_calls, st, now),
            "evals": p_evals(st, rd), "log": p_log(tl), "campaign": p_campaign(st, campaigns)}


def list_runs(ai_log=AI_LOG, now=None, limit=80):
    """⑧ run list, newest first: status · calls · cost · alive."""
    now = now or time.time()
    out = []
    for sp in Path(ai_log).glob("*/run*/state.json"):
        rd = sp.parent
        if not RUN_ID.match(f"{rd.parent.name}/{rd.name}"):
            continue
        st = _json(sp, {}) or {}
        hb = _json(rd / "heartbeat.json")
        c = st.get("counters") or {}
        age = (now - hb["epoch"]) if isinstance(hb, dict) and isinstance(hb.get("epoch"), (int, float)) else None
        out.append({"id": f"{rd.parent.name}/{rd.name}", "created": st.get("created"), "status": st.get("status"),
                    "stage": st.get("stage"), "calls": c.get("calls", 0), "cost": c.get("cost_usd", 0.0),
                    "campaign": (st.get("campaign") or {}).get("name"),
                    "feature": ",".join((st.get("campaign") or {}).get("features") or []) or None,
                    "alive": st.get("status") == "RUNNING" and age is not None and age <= limits_of(rd)[1]["stall_sec"],
                    "mtime": sp.stat().st_mtime})
    out.sort(key=lambda r: (r["created"] or "", r["id"]), reverse=True)
    return out[:limit]


# ================= HTTP (127.0.0.1 only · GET only · read only) =================
_TYPES = {".json": "application/json; charset=utf-8", ".jsonl": "text/plain; charset=utf-8",
          ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
          ".luau": "text/plain; charset=utf-8", ".lua": "text/plain; charset=utf-8", ".py": "text/plain; charset=utf-8",
          ".js": "text/plain; charset=utf-8", ".mjs": "text/plain; charset=utf-8", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


class Server(ThreadingHTTPServer):
    """http.server allows SO_REUSEADDR — on Windows that lets a second server bind a port another one is already
    listening on, so two dashboards would share 8799 without an error (found 10-03 by a mutation test)."""
    allow_reuse_address = False


def make_handler(ai_log=AI_LOG, campaigns=CAMPAIGNS, port=DEFAULT_PORT):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet: the page asks every 2 s
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)) or body is None:
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            # another site in the browser must not read these files through a DNS name pointing here
            if self.headers.get("Host") not in hosts:
                return self._send(403, {"error": "host"})
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in ("/", "/index.html"):
                return self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/runs":
                return self._send(200, list_runs(ai_log))
            if u.path == "/api/usage":
                return self._send(200, account_usage(ai_log))
            if u.path == "/api/run":
                rd = resolve_run(ai_log, q.get("id"))
                return self._send(200, run_payload(rd, campaigns)) if rd else self._send(404, {"error": "run"})
            if u.path == "/api/file":
                base = resolve_run(ai_log, q.get("run")) if q.get("run") else (
                    Path(campaigns) / q["campaign"] if CAMPAIGN_NAME.match(q.get("campaign") or "") else None)
                p = inside(base, q.get("path")) if base else None
                if not p:
                    return self._send(404, {"error": "file"})
                return self._send(200, p.read_bytes(), _TYPES.get(p.suffix.lower(), "application/octet-stream"))
            return self._send(404, {"error": "path"})

    return Handler


def serve(port=DEFAULT_PORT, open_browser=True, out=print, ai_log=AI_LOG, campaigns=CAMPAIGNS):
    try:
        httpd = Server(("127.0.0.1", port), make_handler(ai_log, campaigns, port))
    except OSError as e:
        out(f"오류: 127.0.0.1:{port} 를 열 수 없음({e}) — 다른 포트: dashboard --port 8800")
        return 2
    url = f"http://127.0.0.1:{port}/"
    out(f"대시보드: {url}  (읽기 전용 · 이 PC에서만 · 끝내기 Ctrl+C)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0

