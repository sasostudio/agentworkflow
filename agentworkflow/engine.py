"""v6 state machine (10-02 · 설계서 v6 §1~§5 · 10-03 정렬안). The agents work and judge; the code runs them unattended:
order, carrying their words as they are, running scenarios, counts, fences and stops (HELP to the Master).

    PLANNING (code: the Master's `## 항목`) → BUILD (builder session) → EVALUATE (main evaluator + runner + other eyes)
                                                ↑______ rework (reasons list) ______|      → end gate → COMPLETE

Where to look when something goes wrong:
  a call went to the wrong session / lost its context   _session_for · _call            (§2 · S-1~S-5)
  a run resumed into the wrong step                     s_build (order list) · s_evaluate (eval.phase)  (§3 · §4)
  a reason was lost or sent twice                       _add_rework · _call(delivered)  (§3 rework · P2)
  the evaluation went somewhere unexpected              s_evaluate → _main (the main evaluator's verdict decides)
  cost / usage numbers                                  _call (cost delta) · _note_usage (per key)  (C7 · S-8)
State keys and who clears them: 설계서 v6 §3 (the table is the contract for every key used here).
"""

import datetime as dt
import hashlib
import importlib
import json
import os
import re
import threading
import time
import traceback
from pathlib import Path

from . import bypass_scan, capabilities, checks, effort, flow, gitutil, prompts, qa, report, roles, safety
from . import selfcheck, strength, todo, waitreset, wiki
from .adapters import LimitError, Result
from .roles import BUILDER, DEVIL, EVALUATOR, EVALUATOR_KEYS, SECOND
from .state import V5_STAGES

_NO_QUESTION = re.compile(r"^(없음|없다|없습니다|해당\s*없음|none|n/?a|-|—)\s*[.)]*$", re.I)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Stop(Exception):
    def __init__(self, status, reason, kind=None):
        super().__init__(reason)
        self.status, self.reason, self.kind = status, reason, kind


class Retry(Exception):
    """A transient failure (CLI error answer · no structured output · cut call · runner environment). The run loop runs
    the same step again; after limits.max_retries it is HELP. progressed=True: the cut call changed files or checked
    items — that attempt is not counted (v6 N8) and the next call continues the same session (S-4)."""

    def __init__(self, reason, progressed=False, kind="retry"):
        super().__init__(reason)
        self.progressed, self.kind = progressed, kind


# ================= state helpers shared with cli.py =================
def tree_hash(target, skip=()):
    """One hash of every dirty file of the target (minus `skip`) — "has anything changed?" in one value."""
    snap = {p: h for p, h in gitutil.snapshot(target).items() if p not in set(skip)}
    return hashlib.sha1(json.dumps(snap, sort_keys=True).encode("utf-8")).hexdigest()


def add_rework(st, reason, source):
    """v6 §3 rework: reasons are APPENDED (P11 — a Master reason never hides the evaluator's findings)."""
    rw = st.d.setdefault("rework", [])
    n = max([r["id"] for r in rw] + [st.d.get("rework_seq", 0)]) + 1
    st.d["rework_seq"] = n
    rw.append({"id": n, "reason": reason, "source": source, "t": _now()})
    st.save()
    return n


def raise_cap(st, cfg):
    """v6 N7: every Master intervention gives the evaluation another `max_eval_rejects` rejects."""
    c = st.d["counters"]
    c["eval_reject_cap"] = c.get("eval_rejects", 0) + cfg["limits"]["max_eval_rejects"]
    st.save()


def enter_build(st, target, note):
    """v6 §5-1 step 1 — the ONE way into BUILD (engine and cli): the evaluation in progress is dropped, the evaluator
    sessions' cut calls are forgotten (a new evaluation starts from E1), BUILD's own markers start fresh."""
    st.d.pop("eval", None)
    st.d.pop("build_ok_at", None)
    st.d.pop("gate_pending", None)
    infl = st.d.setdefault("inflight", {})
    for k in EVALUATOR_KEYS:
        infl.pop(k, None)
    st.d["counters"]["no_progress"] = 0
    st.d.pop("build_last_tree", None)
    st.save_snapshot("build_entry", gitutil.snapshot(target), "build")
    st.goto("BUILD", note)


class Engine:
    def __init__(self, cfg, req, state, work_adapter, review_adapter=None, sleep=time.sleep, out=print):
        self.cfg, self.req, self.st = cfg, req, state
        self.adapters = {BUILDER: work_adapter, EVALUATOR: review_adapter or work_adapter}
        self.sleep, self.out = sleep, out
        self.target = Path(cfg["target"]).resolve()
        self.models = cfg["models"]
        self.run_level = req["run_level"]
        self.plan_rel = f"{cfg['plan_dir'].rstrip('/')}/{state.d['run_id']}.md"
        self.plan_path = self.target / self.plan_rel
        self.wdir = cfg["wiki_dir"].rstrip("/")
        self.lim = dict(cfg["limits"])
        self.flow = flow.decide(req, cfg)
        self._hb_lock = threading.Lock()
        self._hb_stop = threading.Event()
        self._calls_now = {}          # call id -> {"key", "kind", "since", "epoch", "model", "effort"} — heartbeat
        self._call_ids = iter(range(1, 1 << 30))
        self._hb_write_lock = threading.Lock()
        self._live_lock = threading.Lock()
        self._live_tools = {}         # call id -> the latest tool uses of that call (dashboard E3 · live.json)
        self.spawn_fallback = waitreset.spawn_fallback

    # ================= main loop =================
    def run(self):
        d = self.st.d
        if d["stage"] in V5_STAGES:  # v6 N11 — cli refuses first; this is the second line
            raise RuntimeError(f"v5 run(단계 {d['stage']}) — v6 엔진으로 이어갈 수 없다. 포기 처리 필요")
        d["status"] = "RUNNING"
        d.setdefault("run_level", self.run_level)
        d["counters"].setdefault("eval_reject_cap", None)
        if d["counters"]["eval_reject_cap"] is None:
            d["counters"]["eval_reject_cap"] = self.lim["max_eval_rejects"]
        for k in ("windows", "last"):  # a reading from before this start may be another account's or stale
            (d.get("usage") or {}).pop(k, None)
        d.pop("paused_usage", None)
        d.pop("help_kind", None)
        infl = d.setdefault("inflight", {})
        infl.pop(DEVIL, None)  # §3 inflight ②: the rebuttal is always a new session
        for k in [k for k, v in infl.items() if not (v or {}).get("sid")]:
            infl.pop(k)  # §3 inflight ⑤: died before the session existed = an ordinary new call
        self.st.save()
        self._note_flow()
        handlers = {"PLANNING": self.s_planning, "BUILD": self.s_build, "EVALUATE": self.s_evaluate,
                    "COMPLETE": self.s_complete}
        hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        hb.start()
        try:
            self._apply_follow_ups()
            while d["status"] == "RUNNING":
                stage = d["stage"]
                try:
                    handlers[stage]()
                except LimitError as e:
                    self._wait_reset(e)
                    continue
                except Retry as r:
                    self._retry(stage, r)
                    continue
                if d["stage"] != stage:
                    d.pop("retries", None)
        except Stop as s:
            if s.kind:
                d["help_kind"] = s.kind
            self.st.set_status(s.status, s.reason)
            self.out(f"[{d['run_id']}] 정지 {s.status}: {s.reason}")
        except Exception as e:  # noqa: BLE001 — v4 §6: never leave state RUNNING behind a dead process
            d["help_traceback"] = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-1500:]
            d["help_kind"] = "exception"
            self.st.set_status("HELP", f"엔진 예외 {type(e).__name__}: {str(e)[:300]} · 단계 {d.get('stage')}")
            self.out(f"[{d['run_id']}] 정지 HELP: {type(e).__name__}: {e}")
        finally:
            self._hb_stop.set()
            hb.join(timeout=5)
            self._write_heartbeat()
        path = report.write(self.st, self.target)
        if d["status"] == "COMPLETE":
            safety.release_lock(self.target, d["run_id"])
        self.out(f"보고서: {path}")
        return d["status"]

    def _note_flow(self):
        note = flow.note(self.flow) + f" · 평가 반려 상한 {self.st.d['counters']['eval_reject_cap']}"
        if self.st.d.get("flow_note") != note:
            self.st.d["flow_note"] = note
            self.st.timeline(note)

    def _help(self, kind, reason):
        return Stop("HELP", reason, kind)

    def _retry(self, stage, r):
        if r.progressed:  # v6 N8: the cut call did work — continue it at once, not counted
            self.st.timeline(f"**이어서** — 끊긴 호출이 진전을 냄(재시도에 안 셈): {str(r)[:200]}")
            return
        rt = self.st.d.setdefault("retries", {})
        n = rt.get(stage, 0) + 1
        rt[stage] = n
        self.st.save()
        cap = self.lim["max_retries"]
        if n > cap:
            raise self._help("env" if r.kind == "env" else "retries",
                             f"재시도 {cap}회 뒤에도 실패 · 단계 {stage}: {str(r)[:400]}")
        backoff = self.lim["retry_backoff_sec"]
        wait = backoff[min(n, len(backoff)) - 1]
        self.st.timeline(f"**재시도** ({n}/{cap}) {wait}초 뒤 — {str(r)[:300]}")
        self.out(f"재시도 {n}/{cap} — {wait}초 대기: {str(r)[:120]}")
        self.sleep(wait)

    # ================= heartbeat (v4 §6) · live records for the dashboard (대시보드 설계서 §6 E1~E3) =================
    @staticmethod
    def _write_display(p, body):
        """heartbeat.json / live.json: replaced whole (readers never see half a file). These are records for watchers —
        a file held by a scanner for longer than the retries skips one write; the next write carries the same facts."""
        tmp = p.with_name(f"{p.name}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            for _ in range(10):
                try:
                    tmp.replace(p)
                    return
                except PermissionError:
                    time.sleep(0.05)
        except OSError:
            pass

    def _write_heartbeat(self):
        """E2: also written at once when a call starts or ends (not only every heartbeat_sec)."""
        with self._hb_write_lock:  # read + write in one turn: an older picture never lands after a newer one
            with self._hb_lock:
                calls = [dict(v) for v in self._calls_now.values()]
            hb = {"epoch": time.time(), "t": _now(), "pid": os.getpid(), "stage": self.st.d.get("stage"),
                  "status": self.st.d.get("status"), "calls": calls}
            self._write_display(self.st.dir / "heartbeat.json", hb)

    def _live(self, cid, key, kind, tool):
        """E3: the latest tool uses of every running call -> live.json. tool = {"id", "name", "target"} (a use) ·
        {"result_of", "error"} (its result) · None (the call ended — its entry goes)."""
        with self._live_lock:
            if tool is None:
                if self._live_tools.pop(cid, None) is None:
                    return
            elif "result_of" in tool:
                for x in (self._live_tools.get(cid) or {}).get("tools", []):
                    if x.get("id") == tool["result_of"]:
                        x["done"], x["error"] = True, bool(tool.get("error"))
            else:
                e = self._live_tools.setdefault(cid, {"key": key, "kind": kind, "tools": []})
                e["tools"].append({**tool, "t": time.strftime("%H:%M:%S"), "epoch": time.time()})
                del e["tools"][:-8]
            body = {"epoch": time.time(), "calls": [{**v, "tools": [dict(x) for x in v["tools"]]}
                                                    for v in self._live_tools.values()]}
            self._write_display(self.st.dir / "live.json", body)

    def _write_usage(self, rate):
        """E4: every account usage reading as it arrives (the first ~2 s into a call, then about every +1% — also
        mid-call) -> usage.json. The dashboard shows the newest one; it never asks Claude itself (0 tokens)."""
        if rate:
            self._write_display(self.st.dir / "usage.json", {"t": time.time(), **{
                w: {"u": v.get("utilization"), "reset": v.get("resetsAt")} for w, v in rate.items()}})

    def _heartbeat_loop(self):
        every = float(self.cfg.get("supervisor", {}).get("heartbeat_sec", 15))
        while not self._hb_stop.is_set():
            self._write_heartbeat()
            self._hb_stop.wait(every)

    # ================= sessions and calls (§2 · §7 S-1~S-9) =================
    def _session_for(self, key):
        """S-1: the one place that decides. A recorded session is continued whatever its age (context first); the
        window only says whether the cache is likely cold. The rebuttal is always new (D2)."""
        if key == DEVIL:
            return None
        e = (self.st.d.get("work_sessions") or {}).get(key) or {}
        return e.get("sid")

    def _shared(self, pack):
        return prompts.shared_context(self.req["text"], self.plan_rel, wiki.listing(self.target, self.wdir),
                                      self.cfg["rules_root"], pack)

    def _budget(self, key):
        self._usage_gate(key)
        c = self.st.d["counters"]
        if c["calls"] >= self.lim["max_calls_per_run"]:
            raise self._help("budget", f"호출 예산 소진 ({c['calls']}/{self.lim['max_calls_per_run']})")
        cap = self.lim.get("max_cost_usd_per_run")
        if cap is not None and c["cost_usd"] >= cap:
            raise self._help("budget", f"비용 지표 상한 도달 (${c['cost_usd']:.2f}/${cap})")

    def _call(self, key, body, *, kind, schema, allowed, effort=None, delivered=(), env=None, first_extra="",
              lost_note=""):
        """One AI call on session `key`. Writes inflight first (§3 · S-3), stores the result, clears what it delivered
        (P2). -> Result with structured output, or raises Retry / Stop / LimitError."""
        self._budget(key)
        st = self.st
        sid = self._session_for(key)
        infl = st.d.setdefault("inflight", {})
        cut = infl.get(key) if sid and (infl.get(key) or {}).get("sid") == sid else None
        mk = roles.model_key(key)
        pack = "work" if mk == BUILDER else "verify"
        prompt = (prompts.INTERRUPTED if cut else "") + body
        if not sid:  # a new session carries the shared context and the once-per-session rules
            prompt = self._shared(pack) + "\n\n---\n\n" + lost_note + prompt + first_extra
        prompt += prompts.answers_block(st.d.get("questions"))
        infl[key] = {"sid": sid, "kind": kind, "started": _now(), "delivered": list(delivered)}
        st.save()
        m = dict(self.models[mk])
        if effort:
            m["effort"] = effort
        allowed = list(allowed)
        extra = capabilities.extra_tools(self.cfg["capabilities"], mk)
        if any(a.startswith("Bash(") for a in allowed):
            extra = extra + ["Bash"]
        snap0 = tree_hash(self.target, self._fence_skip())
        checked0 = self._checked_ids()

        def on_init(new_sid):  # S-3: the session id is known before any work — a cut call can be continued
            from .state import _SAVE_LOCK
            with _SAVE_LOCK:
                infl[key]["sid"] = new_sid
                ws = st.d.setdefault("work_sessions", {})
                prev = ws.get(key) or {}
                if prev.get("sid") != new_sid:
                    ws[key] = {"sid": new_sid, "t": time.time(), "last_cost": 0.0}
                st.save()

        cid = next(self._call_ids)
        wall0 = time.time()
        with self._hb_lock:
            self._calls_now[cid] = {"key": key, "kind": kind, "since": time.strftime("%H:%M:%S"), "epoch": wall0,
                                    "model": m["model"], "effort": m.get("effort"), "resumed": bool(sid)}
        self._write_heartbeat()  # E2: the dashboard sees a call the moment it starts (not up to 15 s later)
        t0 = time.monotonic()
        try:
            res = self.adapters[mk].run_agent(
                role=key, prompt=prompt, cwd=self.target, model=m["model"], effort=m.get("effort"),
                allowed_tools=allowed + [t for t in extra if t != "Bash"],
                disallowed_tools=[t for t in prompts.NO_SHELL if t not in extra],
                permission_mode="dontAsk", add_dirs=None, schema=schema, resume=sid, persist=True,
                idle_timeout=self.lim["idle_timeout_sec"], total_timeout=self.lim["call_total_sec"],
                env=env, on_rate=self._rate_check, on_init=on_init, append_system=self._role_text(key),
                on_tool=lambda t: self._live(cid, key, kind, t))
        except (Stop, LimitError) as why:  # the usage threshold (or the provider's limit) ended the call
            self._record_cut(key, kind, m, prompt, why, wall0, time.monotonic() - t0, bool(sid), delivered)
            raise
        finally:
            with self._hb_lock:
                self._calls_now.pop(cid, None)
            self._live(cid, key, kind, None)
            self._write_heartbeat()
        elapsed = time.monotonic() - t0
        if res.no_session and sid:  # S-5 · §4 ⑬: only this key starts over
            seq = st.record_call(key, st.d["stage"], prompt, res, extra={"kind": kind, "cost_unknown": True,
                                                                         "no_session": True})
            self._event(seq, key, kind, m, res, None, wall0, elapsed, bool(sid))
            (st.d.get("work_sessions") or {}).pop(key, None)
            infl.pop(key, None)
            st.save()
            st.timeline(f"⚠️세션 사라짐 `{key}` ({sid[:8]}) — 그 키만 새 세션으로(엔진 기록으로 이어서)")
            return self._call(key, body, kind=kind, schema=schema, allowed=allowed, effort=effort,
                              delivered=delivered, env=env, first_extra=first_extra,
                              lost_note=self._lost_note(key))
        # C7 · S-9: the CLI reports a continued session's WHOLE spend — this call's share is the difference
        ws = (st.d.get("work_sessions") or {}).get(key) or {}
        unknown = bool(res.timed_out) or not getattr(res, "has_result", True)
        real = None
        if not unknown:
            last = float(ws.get("last_cost") or 0.0) if ws.get("sid") == res.session_id else 0.0
            real = res.cost - last if res.cost >= last else res.cost
        seq = st.record_call(key, st.d["stage"], prompt, res,
                             extra={"kind": kind, "resumed": bool(sid), "real_cost": real, "cost_unknown": unknown,
                                    "effort": m.get("effort"), "delivered": list(delivered)})
        if res.session_id and not unknown:
            ws = st.d.setdefault("work_sessions", {}).setdefault(key, {})
            ws.update(sid=res.session_id, t=time.time(), last_cost=res.cost)
        self._note_usage(res, key)
        self._log_call(key, kind, m, res, real, elapsed, bool(sid))
        self._event(seq, key, kind, m, res, real, wall0, elapsed, bool(sid))
        if res.denials:
            st.d.setdefault("denials", []).extend(
                {"stage": st.d["stage"], "round": st.d["round"], "role": key, "tool": d.get("tool_name"),
                 "input": json.dumps(d.get("tool_input") or {}, ensure_ascii=False)[:160]} for d in res.denials)
        if res.is_error or (schema and not isinstance(res.structured, dict)):
            st.save()  # inflight stays: the next call of this key says "중단됐다" (S-4) when the session exists
            if res.is_error and "does not support this model" in (res.text or ""):
                raise self._help("model", f"{key} 호출 오류: {res.text[:300]} — `claude update` 후 resume")
            progressed = (tree_hash(self.target, self._fence_skip()) != snap0
                          or self._checked_ids() != checked0)
            why = res.timed_out or ("구조화 출력 없음" if not res.is_error else res.text[:300])
            raise Retry(f"{key} 호출 실패 — {why}", progressed=progressed and key == BUILDER)
        infl.pop(key, None)
        if delivered:  # P2: only the reasons this call carried are done
            st.d["rework"] = [r for r in st.d.get("rework") or [] if r["id"] not in set(delivered)]
        st.save()
        return res

    def _record_cut(self, key, kind, m, prompt, why, wall0, elapsed, resumed, delivered):
        """P16: a call cut for usage counts like any cut call — no result, cost unknown (a continued session's next call
        carries it in its cumulative cost). inflight stays as it is: the resume continues that session (S-4)."""
        sid = ((self.st.d.get("inflight") or {}).get(key) or {}).get("sid")
        res = Result(text=f"호출 끊음 — {why}", is_error=True, timed_out="사용량 정지", session_id=sid, has_result=False)
        seq = self.st.record_call(key, self.st.d["stage"], prompt, res,
                                  extra={"kind": kind, "resumed": resumed, "cost_unknown": True, "cut_by_usage": True,
                                         "effort": m.get("effort"), "delivered": list(delivered)})
        self._log_call(key, kind, m, res, None, elapsed, resumed)
        self._event(seq, key, kind, m, res, None, wall0, elapsed, resumed)

    def _lost_note(self, key):
        if key == BUILDER:
            done = [i["id"] for i in todo.parse(self._read_plan())[0] if i["checked"]]
            return prompts.session_lost_builder(self.st.d.get("remaining"), done)
        prev = self._latest_attempt()
        return prompts.session_lost_evaluator(prev.as_posix() if prev else None)

    def _role_text(self, key):
        """System prompt = the working rules (always) + the role definition (switchable)."""
        root = self.cfg["rules_root"]
        parts = [roles.rules_text(root),
                 roles.system_text(key, root) if self.cfg.get("role_definitions", True) else ""]
        return "\n\n".join(p for p in parts if p) or None

    def _log_call(self, key, kind, m, res, real, elapsed, resumed):
        c = self.st.d["counters"]
        hit = res.cache_hit_ratio
        actual = res.main_model
        self.out(f"[run {self.st.d['run_id'][:6]} · {self.st.d['stage']} r{self.st.d['round']} · {key}/{kind} · "
                 f"{m['model']}/{m.get('effort')} · 호출 {c['calls']}/{self.lim['max_calls_per_run']}]")
        self.st.timeline(
            f"호출 `{key}`·{kind} {m['model']}/{m.get('effort')}"
            + (f" → 실제 **{actual}** ⚠️설정과 다름" if actual and actual != m["model"] else "")
            + (f" · ${real:.4f}" if real is not None else " · 비용 미상")
            + (f" · 캐시 적중 {hit:.0%}" if hit is not None else "")
            + (f" · 권한거부 {len(res.denials)}" if res.denials else "")
            + f" · 소요 {elapsed:.0f}초" + (" · 이어쓰기" if resumed else " · 새 세션")
            + (f" · ⚠️끊김({res.timed_out})" if res.timed_out else "")
            + "".join(f" · {lbl} {w['utilization']:.0%}" for k2, lbl in (("five_hour", "5h"), ("seven_day", "7d"))
                      for w in [(res.rate or {}).get(k2)] if w))

    def _event(self, seq, key, kind, m, res, real, wall0, elapsed, resumed):
        """E1: one structured line per call in events.jsonl — the facts of the timeline line above, as values (the
        dashboard never parses prose). seq = the raw/ file number. Records only: nothing here decides anything."""
        u, rate = res.usage or {}, res.rate or {}
        s = res.structured if isinstance(res.structured, dict) else {}
        rec = {"seq": seq, "t0": round(wall0, 3), "t1": round(wall0 + elapsed, 3), "sec": round(elapsed, 1),
               "key": key, "kind": kind, "stage": self.st.d.get("stage"), "round": self.st.d.get("round"),
               "eval_no": self.st.d.get("eval_no"), "model_set": m["model"], "model": res.main_model,
               "effort": m.get("effort"), "resumed": resumed, "cost": real,
               "tokens": {"in": u.get("input_tokens") or 0, "cache_read": u.get("cache_read_input_tokens") or 0,
                          "cache_write": u.get("cache_creation_input_tokens") or 0,
                          "out": u.get("output_tokens") or 0},
               "cache_hit": res.cache_hit_ratio, "u5h": (rate.get("five_hour") or {}).get("utilization"),
               "u7d": (rate.get("seven_day") or {}).get("utilization"),
               "r5h": (rate.get("five_hour") or {}).get("resetsAt"), "r7d": (rate.get("seven_day") or {}).get("resetsAt"),
               "verdict": s.get("verdict"),
               "error": bool(res.is_error), "cut": res.timed_out, "denials": len(res.denials or [])}
        with (self.st.dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    @staticmethod
    def _question_of(res):
        s = res.structured if isinstance(getattr(res, "structured", None), dict) else {}
        # T4 smoke (10-02 23:33): Haiku answered `""` (two quote marks) for "no question" — quotes are not a question
        q = str(s.get("question") or "").strip(" *_\n\"'`“”‘’")
        return "" if (not q or _NO_QUESTION.match(q)) else q

    def _raise_question(self, key, q):
        qs = self.st.d.setdefault("questions", [])
        qs.append({"t": _now(), "stage": self.st.d["stage"], "role": key, "q": q})
        self.st.save()
        self.st.timeline(f"**방향 질문** ({key}) — {q[:300]}")
        if len(qs) > self.lim["max_questions"]:
            raise self._help("questions", f"방향 질문 {len(qs)}회 — 상한 {self.lim['max_questions']} 초과. 마지막: {q[:300]}")
        raise Stop("AWAITING_ANSWER", f"{key}: {q}")

    # ================= files and fences =================
    def _own_plan_path(self):
        return self.st.dir / "01-planning" / "todo.md"

    def _read_plan(self):
        """The TODO as the ENGINE last wrote it (its own copy in the run folder). The file in the target is what the
        builder reads — it may have touched it; the engine never takes that as the truth (10-02 test: a tampered TODO
        became the next call's "before" and the change stuck)."""
        own = self._own_plan_path()
        if own.is_file():
            return own.read_text(encoding="utf-8")
        return self.plan_path.read_text(encoding="utf-8") if self.plan_path.is_file() else ""

    def _read_target_plan(self):
        return self.plan_path.read_text(encoding="utf-8") if self.plan_path.is_file() else ""

    def _write_plan(self, text):
        self.plan_path.parent.mkdir(parents=True, exist_ok=True)
        self.plan_path.write_text(text, encoding="utf-8")
        self._own_plan_path().parent.mkdir(parents=True, exist_ok=True)
        self._own_plan_path().write_text(text, encoding="utf-8")

    def _checked_ids(self):
        return {i["id"] for i in todo.parse(self._read_plan())[0] if i["checked"]}

    def _fence_skip(self):
        """Files that are never the builder's: the TODO (engine-written) and what the engine's own checks produced."""
        return {self.plan_rel} | set(self.st.d.get("check_artifacts") or [])

    def _tree(self):
        return tree_hash(self.target, self._fence_skip())

    def _code_tree(self):
        """P8: the code without the wiki — "the wiki is up to date" means it was written for THIS code."""
        skip = self._fence_skip() | {p for p in gitutil.dirty_paths(self.target) if p.startswith(self.wdir + "/")}
        return tree_hash(self.target, skip)

    def _wiki_on(self):
        if not self.flow["wiki"]:
            return False
        if wiki.exists(self.target, self.wdir):
            return True
        if self.cfg.get("wiki_create"):
            made = wiki.create_skeleton(self.target, self.wdir)
            # the skeleton is the engine's, not the builder's update: it counts as present at BUILD entry (N15)
            entry = self.st.snapshot("build_entry") or {}
            entry.update({p: gitutil.file_hash(self.target, p) for p in made})
            self.st.save_snapshot("build_entry", entry, "build")
            self.st.timeline("wiki 뼈대 생성: " + ", ".join(made))
            return True
        if "wiki_skip_noted" not in self.st.d:
            self.st.d["wiki_skip_noted"] = True
            self.st.d["skipped"].append(f"WIKI — `{self.wdir}/` 없음(기존 프로젝트 · 캠페인 계획 `- Wiki: 있음`이면 생성)")
            self.st.save()
        return False

    def _run_changed(self):
        return [p for p in gitutil.dirty_paths(self.target) if p not in self._fence_skip()]

    # ================= PLANNING (code only — v6 §1) =================
    def s_planning(self):
        v = flow.item_problems(self.req, self.cfg)
        if v:  # cli checked before the run folder existed — only a request changed after the start lands here
            raise self._help("plan", "`## 항목` 오류:\n- " + "\n- ".join(v))
        text = flow.master_plan_text(self.req)
        items = todo.parse(text)[0]
        for it in items:  # the run-level floor (the Master's own number may only be raised)
            if it["level"] is not None and it["level"] < self.run_level:
                text = todo.set_level(text, it["id"], self.run_level)
                self.st.timeline(f"레벨 하한 {it['id']} L{it['level']}→L{self.run_level} (run 난이도)")
        self._write_plan(text)
        (self.st.dir / "01-planning" / "plan.md").write_text(text, encoding="utf-8")
        enter_build(self.st, self.target, f"Master 작성 TODO {len(items)}개")

    # ================= BUILD (§5-1) =================
    def s_build(self):
        rework = list(self.st.d.get("rework") or [])
        open_items = todo.unchecked(self._read_plan())
        cut = (self.st.d.get("inflight") or {}).get(BUILDER)
        if rework:
            return self._build_call("fix", open_items, rework)
        if cut and cut.get("kind") == "wiki":
            return self._wiki_call()
        if open_items or cut:
            has = bool(self._session_for(BUILDER))
            return self._build_call("continue" if has else "first", open_items, [])
        if self._tree() != self.st.d.get("build_ok_at"):
            return self._end_checks()
        if self._wiki_on() and self._code_tree() != self.st.d.get("wiki_done_at"):
            return self._wiki_step()
        self._enter_evaluate()

    def _build_call(self, kind, open_items, rework):
        d = self.st.dir / "02-build"
        rnd = self._next_round()
        plan_before = self._read_plan()
        tree0 = self._tree()
        if kind == "fix":
            body = prompts.build_fix(rework, open_items)
        elif kind == "continue":
            body = prompts.build_continue(open_items, self.st.d.get("remaining"), self.st.d.get("check_info") or "")
        else:
            body = prompts.build_first(self.plan_rel, open_items)
        allowed, env, first_extra = self._builder_tools()
        eff = effort.builder(self, open_items or todo.parse(plan_before)[0],
                             rework=kind == "fix" and self.st.d["counters"].get("eval_rejects", 0)
                             >= self.lim["rework_high_after"])
        res = self._call(BUILDER, body, kind=kind, schema=prompts.BUILD_SCHEMA, allowed=allowed, effort=eff,
                         delivered=[r["id"] for r in rework], env=env, first_extra=first_extra)
        self._sweep_shot_drafts()
        s = res.structured
        (d / f"r{rnd}_build.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        self.st.d.pop("check_info", None)
        # §5-1 step 4: ① done first (work finished before a question is kept) → ② question → ③ fences → ④ self-check
        run_changed = self._run_changed()
        # the builder may only READ the TODO: compare its file with the engine's copy, then write the engine's version
        # (with the new checks) back over it — a touched TODO is reported and never survives
        v = todo.check_dev(plan_before, self._read_target_plan(), [])
        v = [x for x in v if "Master만" in x]
        plan_now, v_done = self._apply_done(plan_before, s.get("done"), run_changed)
        v += v_done
        self._write_plan(plan_now)
        self.st.d["remaining"] = str(s.get("remaining") or "").strip()
        self.st.d["last_summary"] = (str(s.get("summary") or "").strip() + "\n작성게이트: "
                                     + str(s.get("write_gate") or "").strip())
        self.st.save()
        q = self._question_of(res)
        if q:
            self._raise_question(BUILDER, q)
        v += todo.check_dev(plan_before, plan_now, run_changed)  # 완료 근거 files really changed
        v += self._outside(run_changed, plan_now)
        # (10-03 정렬안 N1: an empty `write_gate` is no longer the engine's to reject — the evaluator checks the claim)
        # §5-1 step 5 (N3 · P15): no new check and the same tree as after the last BUILD call
        c = self.st.d["counters"]
        tree1 = self._tree()
        newly = {i["id"] for i in todo.parse(plan_now)[0] if i["checked"]} - \
                {i["id"] for i in todo.parse(plan_before)[0] if i["checked"]}
        if not newly and tree1 == tree0 and tree1 == self.st.d.get("build_last_tree", tree0):
            c["no_progress"] = c.get("no_progress", 0) + 1
            if c["no_progress"] >= self.lim["no_progress_max"]:
                v.append(f"진전 없음 — 새 체크도 파일 변경도 없는 호출 {c['no_progress']}번 연속")
        else:
            c["no_progress"] = 0
        self.st.d["build_last_tree"] = tree1
        self.st.save()
        if v:
            return self._auto_reject(v)
        self._self_check_loop(d, f"r{rnd}")
        if todo.unchecked(plan_now):  # B8: a middle call's machine check is information only
            ok, log = self._run_checks(d / f"r{rnd}_check_info.log")
            if not ok:
                self.st.d["check_info"] = checks.tail(log, 2000)
                self.st.save()
            return
        return self._end_checks()

    def _builder_tools(self):
        allowed = prompts.perms_build(self.target)
        env, extra = None, ""
        if self.cfg.get("contract_files"):
            extra += prompts.contract_rule(self.cfg["contract_files"])
        if self.req.get("kind") == "원작 재현":
            extra += prompts.original_rule(self.req["kind"], self.req.get("references") or [])
        if self._sc():
            allowed.append(f"Bash({selfcheck.command_prefix()}:*)")
            extra += prompts.self_check_rule(selfcheck.command_prefix())
            env = {selfcheck.ENV: self._self_check_env()}
        shot = self._dev_shot()
        if shot:
            allowed += [f"Bash({shot.command_prefix()}:*)"] + prompts.write_rules(self.target / shot.DRAFT_DIR)
            extra += shot.rule(self.req.get("references") or [])
            env = {**(env or {}), shot.ENV: self._dev_shot_env(shot)}
        return allowed, env, extra

    def _outside(self, run_changed, plan_text):
        """§5-1 step 3 · step 8: every changed file must be some item's `대상` (wiki_dir is always allowed when the wiki
        is on — the builder writes it in the same session)."""
        wiki_on = self.flow["wiki"] and (wiki.exists(self.target, self.wdir) or bool(self.cfg.get("wiki_create")))
        files = [p for p in run_changed if not (wiki_on and p.startswith(self.wdir + "/"))]
        out = todo.outside_targets(files, plan_text)
        return (["TODO `대상:` 밖 파일을 수정함: " + ", ".join(out[:10])
                 + " — 필요하면 고치지 말고 `question`으로 알린다(대상 추가는 Master만)"] if out else [])

    def _auto_reject(self, violations, source="self_fix"):
        c = self.st.d["counters"]
        c["self_fix"] = c.get("self_fix", 0) + 1
        reason = "엔진 규칙 위반:\n- " + "\n- ".join(violations)
        add_rework(self.st, reason, source)
        self.st.timeline(f"**자동반려** ({c['self_fix']}/{self.lim['max_self_fix']}) — {reason[:300]}")
        if c["self_fix"] >= self.lim["max_self_fix"]:
            kind = "no_progress" if any(x.startswith("진전 없음") for x in violations) else "self_fix"
            raise self._help(kind, f"{self.st.d['stage']} 자동반려 {c['self_fix']}회 — Master 판단 필요. "
                                   f"마지막 사유: {reason[:400]}")

    def _end_checks(self):
        """§5-1 step 6 · P1: the checks that make a BUILD finished, with no AI. Pass → build_ok_at = this tree."""
        d = self.st.dir / "02-build"
        rnd = self.st.d["round"]
        plan = self._read_plan()
        v = self._outside(self._run_changed(), plan) + todo.check_dev(plan, plan, self._run_changed())
        if v:
            return self._auto_reject(v)
        if self._sc():
            files = [p for p in self._run_changed() if not p.startswith(self.wdir + "/")]
            ok, log = selfcheck.run(self._sc(), files, self.target, self.st.dir / "selfcheck-tmp")
            (d / f"r{rnd}_end_selfcheck.log").write_text(log, encoding="utf-8")
            if not ok:
                return self._auto_reject(["자기 검사 실패(엔진 실행) — 로그 끝:\n" + checks.tail(log, 1500)])
        ok, log = self._run_checks(d / f"r{rnd}_check.log")
        c = self.st.d["counters"]
        if not ok:
            c["check_fails"] = c.get("check_fails", 0) + 1
            c["check_fails_total"] = c.get("check_fails_total", 0) + 1
            add_rework(self.st, "기계검사(빌드·테스트) 실패 — 로그를 보고 근본 원인을 고칠 것:\n```\n"
                       + checks.tail(log, 3000) + "\n```", "check")
            self.st.timeline(f"**기계검사 실패** ({c['check_fails']}/{self.lim['max_check_fails']})")
            if c["check_fails"] >= self.lim["max_check_fails"]:
                raise self._help("check", f"BUILD 기계검사 실패 {c['check_fails']}회 — Master 판단 필요")
            return
        self.st.d["last_check_log"] = checks.tail(log, 2000)
        self.st.d["build_ok_at"] = self._tree()
        self.st.save()
        self.st.timeline("BUILD 끝 검사 통과(대상 밖·자기 검사·기계검사)")

    def _wiki_step(self):
        """§5-1 step 7 · P8. A wiki rework that the builder already fixed is checked without a new call."""
        if self.st.d.get("wiki_tried") and self._wiki_problems() == []:
            return self._wiki_done()
        return self._wiki_call()

    def _wiki_call(self):
        changed = [p for p in self._run_changed() if not p.startswith(self.wdir + "/")]
        allowed, env, first_extra = self._builder_tools()
        self._next_round()
        self.st.save_snapshot("wiki_base", gitutil.snapshot(self.target), "wiki")
        res = self._call(BUILDER, prompts.build_wiki(self.wdir, ", ".join(changed) or "(없음)"), kind="wiki",
                         schema=prompts.BUILD_SCHEMA, allowed=allowed, env=env, first_extra=first_extra)
        self.st.d["wiki_tried"] = True
        self.st.save()
        q = self._question_of(res)
        if q:
            self._raise_question(BUILDER, q)
        v = self._wiki_problems()
        if v:
            return self._auto_reject(v)
        self._wiki_done()

    def _wiki_problems(self):
        """The fence only: nothing outside the wiki since the last end check. Whether the wiki needed an update is the
        evaluator's to judge (10-03 정렬안 A5 — the engine's "code changed, wiki unchanged" reject is gone)."""
        v = []
        if self._tree() != self.st.d.get("build_ok_at"):
            stray = [p for p in gitutil.changed_since(self.target, self.st.snapshot("wiki_base") or {})
                     if not p.startswith(self.wdir + "/") and p not in self._fence_skip()]
            if stray:
                v.append("wiki 갱신 중 wiki 밖 파일을 수정함: " + ", ".join(stray[:10]))
        return v

    def _wiki_done(self):
        self.st.d["wiki_done_at"] = self._code_tree()
        self.st.d["build_ok_at"] = self._tree()  # the wiki is part of the finished tree
        self.st.d.pop("wiki_tried", None)
        self.st.save()
        self.st.timeline("wiki 갱신 확인")

    def _enter_evaluate(self):
        self.st.d["eval_no"] = self.st.d.get("eval_no", 0) + 1
        self.st.d.pop("remaining", None)
        self.st.goto("EVALUATE", f"평가 {self.st.d['eval_no']}회차 — 열린 항목 0 · 끝 검사 통과")

    def _next_round(self):
        self.st.d["round"] = self.st.d.get("round", 0) + 1
        self.st.save()
        return self.st.d["round"]

    # ---------- BUILD helpers kept from v5 ----------
    def _apply_done(self, plan, done, changed):
        """The engine writes [x] + 완료 근거 from the builder's `done`: id exists · files are paths · each file really
        changed in this run · inside the item's 대상. -> (new plan text, violations)."""
        items = {i["id"]: i for i in todo.parse(plan)[0]}
        v = []
        for dn in done or []:
            tid = dn.get("id")
            it = items.get(tid)
            if not it:
                v.append(f"`done`에 없는 항목 id: {tid}")
                continue
            files, problems = todo.clean_files(dn.get("files"))
            bad = [f for f in files if not todo._file_matches(f, changed)
                   or not any(todo._in_target(f, t) for t in it["target_files"])]
            if problems or bad:
                v.append(f"{tid} files 가 실제 변경(이번 run 변경 ∩ 대상)이 아님: "
                         + "; ".join(problems + [f"{f} — 변경·대상 밖" for f in bad]))
                continue
            plan = todo.mark_done(plan, tid, todo.evidence_line(files, dn.get("note")))
        return plan, v

    def _run_checks(self, log_path):
        pre = set(gitutil.dirty_paths(self.target))
        ok, log = checks.run_checks(self.cfg["check_commands"], self.target, self.cfg["check_timeout_sec"], log_path)
        made = sorted(set(gitutil.dirty_paths(self.target)) - pre)
        if made:  # files the checks themselves created (only NEW dirt — never a file the builder touched)
            self.st.d["check_artifacts"] = sorted(set(self.st.d.get("check_artifacts", [])) | set(made))
            self.st.save()
            self.st.timeline("기계검사가 만든 파일(대상 밖 검사에서 제외): " + ", ".join(made[:10]))
        return ok, log

    def _sc(self):
        sc = self.cfg.get("self_check") or {}
        return sc if sc.get("commands") else None

    def _self_check_env(self):
        p = self.st.dir / "00-request" / "selfcheck.json"
        body = json.dumps({**self._sc(), "tmp_root": (self.st.dir / "selfcheck-tmp").as_posix()}, ensure_ascii=False)
        if not p.is_file() or p.read_text(encoding="utf-8") != body:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        return str(p)

    def _self_check_loop(self, d, tag):
        """v4 §11: the engine re-runs the self-check on the builder's files; a failure goes back to the same session
        (at most self_check.max_fixes). What still fails is left to the end checks."""
        sc = self._sc()
        if not sc:
            return
        files = [p for p in self._run_changed() if not p.startswith(self.wdir + "/")]
        ok, log = selfcheck.run(sc, files, self.target, self.st.dir / "selfcheck-tmp")
        (d / f"{tag}_selfcheck.log").write_text(log, encoding="utf-8")
        for attempt in range(1, sc["max_fixes"] + 1):
            if ok:
                return
            allowed, env, _ = self._builder_tools()
            res = self._call(BUILDER, prompts.self_check_fix(files, log), kind="self_check_fix",
                             schema=prompts.BUILD_SCHEMA, allowed=allowed, env=env)
            q = self._question_of(res)
            if q:
                self._raise_question(BUILDER, q)
            ok, log = selfcheck.run(sc, files, self.target, self.st.dir / "selfcheck-tmp")
            (d / f"{tag}_selfcheck_fix{attempt}.log").write_text(log, encoding="utf-8")
            self.st.timeline(f"자기 검사 이어서 수정 ({attempt}/{sc['max_fixes']}) — {'통과' if ok else '실패'}")

    def _dev_shot(self):
        """D8: the target's dev_shot module (qa.CAPS) on a visual run, else None."""
        name = qa.caps((self.cfg.get("qa") or {}).get("runner")).get("dev_shot")
        if not (name and self.cfg.get("dev_shot", True) and self.req.get("visual")):
            return None
        return importlib.import_module(f".{name}", __package__)

    def _dev_shot_env(self, shot):
        p = self.st.dir / "00-request" / "devshot.json"
        body = json.dumps(shot.config_for(self.cfg, self.target, self.st.dir / "dev-shots"), ensure_ascii=False)
        if not p.is_file() or p.read_text(encoding="utf-8") != body:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        return str(p)

    def _sweep_shot_drafts(self):
        shot = self._dev_shot()
        drafts = self.target / shot.DRAFT_DIR if shot else None
        if drafts is None or not drafts.is_dir():
            return
        left = [p for p in drafts.rglob("*") if p.is_file()]
        if not left:
            return
        dest = self.st.dir / "dev-shots" / "leftover"
        dest.mkdir(parents=True, exist_ok=True)
        for p in left:
            os.replace(p, dest / f"{time.strftime('%H%M%S')}_{p.name}")
        self.st.timeline(f"화면 확인 초안 {len(left)}개를 실행 안 한 채 남김 — run 폴더로 옮김")

    # ================= EVALUATE (§5-2 · 10-03 정렬안 §3) =================
    # The MAIN evaluator (one session) decides every step: READY · REJECT (+ to_builder) · RUN (+ scenarios) · UNSURE.
    # The engine carries its words as they are, plays its scenarios, shows it what the other eyes (second view ·
    # rebuttal) said, and counts. It never sorts, re-classifies or routes the evaluator's findings (정렬안 §0 ③).
    _EVAL_STEPS = ("MAIN", "RUN", "SECOND", "DEVIL")

    def s_evaluate(self):
        ev = self.st.d.get("eval")
        tree = self._tree()
        if ev and (ev.get("snap") != tree or ev.get("phase") not in self._EVAL_STEPS):
            # §3 eval: the code moved under a half-done evaluation (or a record from before 10-03) — start again
            self.st.timeline("평가 기록이 지금 트리와 다름(또는 옛 형식) — 주 평가자 첫 평가부터 다시")
            self.st.d.pop("eval", None)
            for k in EVALUATOR_KEYS:
                (self.st.d.get("inflight") or {}).pop(k, None)
            ev = None
        if not ev:
            ev = {"phase": "MAIN", "attempt_dir": None, "snap": tree, "verdicts": {}, "looked": False, "inbox": [],
                  "runs": 0}
            self.st.d["eval"] = ev
            self.st.save()
        {"MAIN": self._main, "RUN": self._run_scenarios, "SECOND": self._second, "DEVIL": self._devil}[ev["phase"]](ev)

    def _set_phase(self, ev, phase, **kw):
        ev["phase"] = phase
        ev.update(kw)
        self.st.d["eval"] = ev
        self.st.save()

    def _inbox(self, ev, text):
        """Something for the main evaluator to see on its next call (results · the other eyes · a fact check)."""
        ev.setdefault("inbox", []).append(text)
        self._set_phase(ev, "MAIN")

    # ---------- what the evaluator looks at ----------
    def _review_ctx(self):
        base = self.st.snapshot("accepted")
        since = "직전 통과" if base is not None else "run 시작"
        skip = self._fence_skip()
        subject = [p for p in (gitutil.changed_since(self.target, base) if base is not None
                               else gitutil.dirty_paths(self.target)) if p not in skip]
        diff, omitted = gitutil.diff_with_omitted(self.target, subject, self.lim["review_diff_chars"])
        seen = set(self.st.d.get("bypass_seen") or [])
        new_bp = [f for f in bypass_scan.scan(gitutil.diff_with_omitted(self.target, subject, None)[0])
                  if "|".join(f) not in seen]
        if new_bp:
            self.st.d["bypass_seen"] = sorted(seen | {"|".join(f) for f in new_bp})
            self.st.d.setdefault("bypass_findings", []).extend(
                {"round": self.st.d["round"], "path": p, "kind": k, "line": t} for p, k, t in new_bp)
            self.st.save()
        denied = "\n".join(f"- `{x['tool']}` {x['input']}" for x in self.st.d.get("denials") or []
                           if x.get("role") == BUILDER)
        return {"changed": ", ".join(subject) or "(없음)", "diff": diff, "omitted": omitted, "subject": subject,
                "check_log": self.st.d.get("last_check_log"), "summary": self.st.d.get("last_summary"),
                "bypass": bypass_scan.as_text(new_bp), "denied": denied, "since": since,
                "kind": self.req.get("kind"), "refs": self.req.get("references") or []}

    def _eval_level(self, subject):
        items = todo.parse(self._read_plan())[0]
        item_level = max([i["level"] or self.run_level for i in items] or [self.run_level])
        return strength.judge_level(subject, self.cfg, self.st.d["counters"].get("check_fails_total", 0), item_level)

    def _code_items(self):
        exts = tuple(e.lower() for e in self.cfg["doc_extensions"])
        return [i for i in todo.parse(self._read_plan())[0]
                if i["target_files"] and not all(t.lower().endswith(exts) for t in i["target_files"])]

    def _qa_on(self):
        if self.cfg["qa"]["runner"] == "none" or not self.flow["qa"]:
            return False
        return bool(self._code_items())  # a docs-only bundle has nothing to play (judged from the source in E1)

    def _scenario_cap(self):
        """① a bound, not a judgment (정렬안 A6): at most max(qa.max_scenarios, code items x 3), never above 100. How
        many scenarios are ENOUGH is the evaluator's call — the engine sets no floor."""
        return min(100, max(self.cfg["qa"]["max_scenarios"], len(self._code_items()) * 3))

    def _qa_info(self):
        qc = self.cfg["qa"]
        base = qc.get("base_url") or (qc["managed"].get("ready_url") if qc["environment"] == "managed" else None)
        return {"runner": qc["runner"], "environment": qc["environment"], "base_url": base,
                "cap": self._scenario_cap(), "carry": self._carry_text()}

    def _latest_attempt(self):
        """P3 · N13: the run's most recently FINISHED attempt (any evaluation round) — the carry-over source."""
        root = self.st.dir / "03-evaluate"
        done = [p for p in root.glob("e*_a*") if (p / "results_final.json").is_file()]
        return max(done, key=lambda p: p.stat().st_mtime) if done else None

    def _carry_text(self):
        """Facts only (정렬안 N3): the latest finished attempt's scenarios and how each one ended."""
        last = self._latest_attempt()
        scen = self._scenarios_from(last) if last else None
        if not scen:
            return ""
        res = {r["id"]: r for r in self._results(last)}
        lines = [f"- `{s['id']}` [{s['kind']}] {s['title']} · 지난 결과 {(res.get(s['id']) or {}).get('status', '?')}"
                 for s in scen]
        return f"시도 폴더 `{last.as_posix()}` (`scenarios/` 에 코드 · `results_final.json`)\n" + "\n".join(lines)

    def _eval_effort(self, items=None):
        base = self.cfg["levels"]["evaluate"][str(self.run_level)]["effort"]
        return effort.evaluator(self, base, items)

    def _record_verdict(self, ev, name, v, d):
        self.st.d.setdefault("eval_log", []).append({"eval_no": self.st.d.get("eval_no"), "round": self.st.d["round"],
                                                      "phase": name, "verdict": v.get("verdict")})
        (d / f"r{self.st.d['round']}_{name}.json").write_text(json.dumps(v, ensure_ascii=False, indent=2),
                                                              encoding="utf-8")
        ev.setdefault("verdicts", {})[name] = v.get("verdict")
        self.st.save()
        self.st.timeline(f"판정 **{v.get('verdict')}** ({name}) — {str(v.get('reason') or '')[:200]}"
                         + (f" · 돌린 범위 {', '.join(map(str, v['checked']))[:120]}" if v.get("checked")
                            else " · ⚠️돌린 범위(checked) 없음"))

    @staticmethod
    def _issues_text(v):
        out = [f"- [{i.get('severity')}] {i.get('where')}: {i.get('problem')}"
               + (f" → {i['fix']}" if i.get("fix") else "") for i in v.get("issues") or []]
        return (v.get("reason") or "") + ("\n" + "\n".join(out) if out else "")

    # ---------- the main evaluator ----------
    def _main(self, ev):
        d = self.st.dir / "03-evaluate"
        qa_on = self._qa_on()
        if not ev.get("looked"):  # this evaluation's first look: the code since the last pass
            ctx = self._review_ctx()
            lvl, why = self._eval_level(ctx["subject"])
            self.st.timeline(f"평가 레벨 **L{lvl}** — {why} · 변경 {len(ctx['subject'])}개")
            ev["level"] = lvl
            wiki_on = bool(self.flow["wiki"] and wiki.exists(self.target, self.wdir))  # read only — no skeleton here
            body = prompts.eval_main(ctx, self._qa_info() if qa_on else None, self.st.d.get("master_notes"), wiki_on,
                                     again=(self.st.d.get("eval_no") or 1) > 1)
            kind = "E1"
        else:  # what came back for it: the runner's results · the other eyes · a fact check · the Master's answer
            body = prompts.eval_followup(ev.get("inbox") or [], qa_on)
            kind = "E2"
        self._next_round()
        res = self._call(EVALUATOR, body, kind=kind, schema=prompts.EVAL_SCHEMA, allowed=prompts.READ_ONLY,
                         effort=self._eval_effort(todo.parse(self._read_plan())[0]))
        v = res.structured
        ev["looked"], ev["inbox"] = True, []
        self._record_verdict(ev, kind, v, d)
        q = self._question_of(res)
        if q:
            self._raise_question(EVALUATOR, q)
        verdict = v.get("verdict")
        if verdict == "REJECT":  # its words go to the builder as they are
            return self._eval_reject([("eval", str(v.get("to_builder") or "").strip() or self._issues_text(v))])
        if verdict == "UNSURE":
            ev["unsure"] = str(v.get("reason") or "").strip() or "(사유 없음)"
            return self._gate(ev)
        if verdict == "RUN":
            return self._ask_run(ev, v, qa_on)
        ev["want_rebuttal"] = bool(v.get("want_rebuttal"))
        self._after_ready(ev)

    def _ask_run(self, ev, v, qa_on):
        """RUN: play these scenarios (none given = the same ones again). Only facts are checked before playing; a problem
        goes back to the evaluator as information. Decision 10-03: at most limits.max_eval_runs per evaluation → HELP."""
        scen = v.get("scenarios") or ev.get("scenarios") or []
        ev["runs"] = ev.get("runs", 0) + 1
        cap = self.lim["max_eval_runs"]
        if ev["runs"] > cap:  # stop AT this run: a resume (counter back to 0 — cli.reset_on_resume) plays it
            self._set_phase(ev, "RUN" if (qa_on and scen) else "MAIN", attempt_dir=None, scenarios=scen)
            raise self._help("eval_runs", f"한 평가에서 시나리오 실행 요청 {ev['runs']}회(상한 {cap}) — Master 판단 필요 "
                                          "(환경이면 복구 뒤 resume = 이 실행부터 · 방향이면 reject --reason)")
        if not qa_on:
            return self._inbox(ev, prompts.NO_QA_NOTE)
        try:
            problems = qa.precheck(scen, self.cfg, self.target, self._scenario_cap()) if scen else ["시나리오가 없음"]
        except qa.SyntaxToolError as e:
            raise self._help("config", f"QA 설정 오류 — {e}")
        if problems:
            self.st.timeline("시나리오 실행 전 검사 — " + "; ".join(problems)[:300])
            return self._inbox(ev, prompts.precheck_note(problems))
        ev["scenarios"] = scen
        self._set_phase(ev, "RUN", attempt_dir=None)

    def _after_ready(self, ev):
        """READY from the main evaluator: the other eyes still needed, then the end gate."""
        if not ev.get("second_done") and self._need_second():
            return self._set_phase(ev, "SECOND")
        if not ev.get("devil_done") and self._need_devil(ev):
            return self._set_phase(ev, "DEVIL")
        self._accept(ev, "평가 READY")

    def _accept(self, ev, by):
        self.st.save_snapshot("accepted", gitutil.snapshot(self.target), by)
        self._gate(ev)

    def _risk_touched_since(self, snap_name):
        """High-risk paths or contract files changed since that snapshot (none yet: since the last pass / run start)."""
        base = self.st.snapshot(snap_name)
        if base is None:
            base = self.st.snapshot("accepted")
        changed = gitutil.changed_since(self.target, base) if base is not None else gitutil.dirty_paths(self.target)
        pats = list(self.cfg.get("contract_files") or []) + list(self.cfg.get("high_risk_paths") or [])
        return [p for p in changed if p not in self._fence_skip()
                and any(todo._in_target(p, t) or todo._in_target(t, p) for t in pats)]

    def _need_second(self):
        """정렬안 B9: the second view (security · data) only when such a file changed since it last looked — or when the
        engine.json sets levels.evaluate.N.second for this level. An L3 alone no longer calls it."""
        if self.cfg["levels"]["evaluate"][str(self.run_level)].get("second"):
            return True
        return bool(self._risk_touched_since("second"))

    def _need_devil(self, ev):
        """D2 + decision 10-03 (나): the run's first pass always; after that only when the main evaluator asks for it
        (`want_rebuttal`) or a high-risk/contract file changed since the last rebuttal. At most once per evaluation."""
        if not self.st.d.get("devil_runs"):
            return True
        return bool(ev.get("want_rebuttal")) or bool(self._risk_touched_since("devil"))

    # ---------- scenarios: run ----------
    def _run_scenarios(self, ev):
        root = self.st.dir / "03-evaluate"
        prior = None
        adir = Path(ev["attempt_dir"]) if ev.get("attempt_dir") else None
        if adir is not None and adir.is_dir() and not (adir / "results_final.json").is_file():
            prior = self._played_before(adir)  # #7: an interrupted run of the same scenarios on the same code
        else:
            n = len(list(root.glob(f"e{self.st.d['eval_no']}_a*"))) + 1
            adir = root / f"e{self.st.d['eval_no']}_a{n}"
            adir.mkdir(parents=True, exist_ok=True)
            self._set_phase(ev, "RUN", attempt_dir=str(adir))
        try:
            verdict, results = qa.run_qa(self.cfg, self.target, adir, ev["scenarios"], prior=prior)
        except RuntimeError as e:  # a managed server that did not start = the environment's
            raise Retry(f"QA 환경: {e}", kind="env")
        self.st.timeline(f"시나리오 실행 `{adir.name}` **{verdict}** — "
                         + ", ".join(f"{r['id']}:{r['status']}" for r in results)[:400])
        failed = [r for r in results if r["status"] != "PASSED"]
        if failed and all(r.get("env") for r in failed):  # the runner itself could not play them — a fact (①)
            self._set_phase(ev, "RUN", attempt_dir=None)  # the retry plays them again in a new attempt
            raise Retry("시나리오 환경 실패 — 실행 환경 확인 필요:\n" + qa.failures_text(failed), kind="env")
        if verdict == "PASSED" and not self.req.get("visual"):  # all passed by its own checks
            self.st.timeline("전부 통과(시각 작업 아님) — 결과 판정 호출 생략")
            ev["passed_unseen"] = True
            return self._after_ready(ev)
        self._inbox(ev, self._results_block(adir, results))

    def _results_block(self, adir, results):
        failed = [r for r in results if r["status"] != "PASSED"]
        logs = "\n".join(f"- `{p.as_posix()}`" for p in sorted(adir.glob("*.txt")) + sorted(adir.glob("*.log"))) or "(없음)"
        visual = self.req.get("kind") if self.req.get("visual") else None
        return prompts.results_block(adir.as_posix(), results, qa.failures_text(failed), logs,
                                     self._shots(adir, results) if visual else [], visual,
                                     self.req.get("references") or [])

    def _played_before(self, adir):
        if not (adir / "partial_results.json").is_file():
            return None
        try:
            done = json.loads((adir / "partial_results.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        keep = {r["id"]: r for r in done if isinstance(r, dict) and r.get("id") and not r.get("env")}
        if keep:
            self.st.timeline(f"시나리오 이어서 실행 — `{adir.name}`에서 이미 돈 {len(keep)}개는 그 결과 사용")
        return {"dir": str(adir), "results": keep} if keep else None

    @staticmethod
    def _scenarios_from(attempt):
        try:
            manifest = json.loads((attempt / "scenarios.json").read_text(encoding="utf-8"))
            return [{"id": m["id"], "title": m["title"], "kind": m["kind"], "item_ids": m.get("item_ids") or [],
                     "filename": Path(m["path"]).stem,
                     "code": "" if m.get("steps") else Path(m["path"]).read_text(encoding="utf-8"),
                     **({"steps": m["steps"]} if m.get("steps") else {}),
                     **{k: m[k] for k in ("frames", "interval", "no_capture") if m.get(k)}} for m in manifest] or None
        except (OSError, ValueError, KeyError):
            return None

    @staticmethod
    def _results(adir):
        try:
            return json.loads((Path(adir) / "results_final.json").read_text(encoding="utf-8"))["results"]
        except (OSError, ValueError, KeyError):
            return []

    def _shots(self, adir, results):
        shots = []
        for r in results:
            edir = Path(adir) / "evidence" / qa.safe_id(r["id"])
            names = r.get("evidence") or []
            names = [n for n in names if n == qa.SHEET] or names
            paths = [(edir / n).as_posix() for n in names if (edir / n).is_file()]
            if paths:
                shots.append((r["id"], r.get("title") or "", paths))
        return shots

    # ---------- the other eyes: second view · rebuttal (D2) — what they say goes to the main evaluator ----------
    def _second(self, ev):
        d = self.st.dir / "03-evaluate"
        self._next_round()
        res = self._call(SECOND, prompts.eval_second(self._review_ctx()), kind="SECOND", schema=prompts.EVAL_SCHEMA,
                         allowed=prompts.READ_ONLY, effort=effort.raise_to(self._eval_effort(), "high"))
        v = res.structured
        self._record_verdict(ev, "SECOND", v, d)
        self.st.save_snapshot("second", gitutil.snapshot(self.target), "2번째 관점")
        ev["second_done"] = True
        q = self._question_of(res)
        if q:
            self._raise_question(SECOND, q)
        if v.get("verdict") == "READY":  # nothing for the main evaluator to weigh (its notes stay in the record)
            return self._after_ready(ev)
        self._inbox(ev, prompts.other_eyes_block("2번째 관점(보안·데이터 · 새 세션)", v))

    def _devil(self, ev):
        d = self.st.dir / "03-evaluate"
        ctx = self._review_ctx()
        adir = Path(ev["attempt_dir"]) if ev.get("attempt_dir") else None
        scen_files, shots, results_text = [], [], ""
        if adir is not None and adir.is_dir():
            scen_files = sorted(p.as_posix() for p in (adir / "scenarios").glob("*"))
            results = self._results(adir)
            results_text = "\n".join(f"- {r['id']} → {r['status']}: {r.get('note', '')}" for r in results)
            by_id = {s["id"]: s for s in ev.get("scenarios") or []}
            for sid, title, paths in self._shots(adir, results):
                s = by_id.get(sid) or {}
                when = (f"마지막 단계 return 직후 {s.get('frames')}장 · {s.get('interval')}초 간격" if s.get("frames")
                        else "시나리오가 shot()을 부른 순간(코드 참조)")
                shots.append((sid, title, when, paths))
        self._next_round()
        res = self._call(DEVIL, prompts.eval_devil(ctx, scen_files, shots, results_text), kind="DEVIL",
                         schema=prompts.EVAL_SCHEMA, allowed=prompts.READ_ONLY,
                         effort=effort.raise_to(self._eval_effort(), "high"))
        v = res.structured
        self._record_verdict(ev, "DEVIL", v, d)
        self.st.d["devil_runs"] = self.st.d.get("devil_runs", 0) + 1
        self.st.save_snapshot("devil", gitutil.snapshot(self.target), "반박 검토")
        ev["devil_done"] = True
        q = self._question_of(res)
        if q:
            self._raise_question(DEVIL, q)
        if v.get("verdict") == "READY":
            return self._accept(ev, "반박 검토 READY")
        text = prompts.other_eyes_block("반박 검토(새 세션)", v)
        if ev.pop("passed_unseen", None) and adir is not None:  # it never saw these results itself (all passed)
            text += "\n" + self._results_block(adir, self._results(adir))
        self._inbox(ev, text)

    def _eval_reject(self, parts):
        """Any rejection from the evaluation side: reasons → rework, one count per round (P20), back to BUILD."""
        c = self.st.d["counters"]
        c["eval_rejects"] = c.get("eval_rejects", 0) + 1
        for source, text in parts:
            add_rework(self.st, f"[평가 {self.st.d.get('eval_no')}회차 · {source}]\n{text}", source)
        cap = c.get("eval_reject_cap") or self.lim["max_eval_rejects"]
        self.st.timeline(f"**평가 반려** ({c['eval_rejects']}/{cap}) — "
                         + " | ".join(t[:150] for _, t in parts))
        enter_build(self.st, self.target, f"평가 반려 {c['eval_rejects']}회 → 작업자")
        if c["eval_rejects"] >= cap:
            raise self._help("eval_cap", f"평가 반려 {c['eval_rejects']}회(상한 {cap}) — 같은 반려가 반복된다. "
                                         "Master: `reject --reason`으로 방향을 주거나 포기")

    def _gate(self, ev):
        """The end gate (always — v6 decision 3). The stage stays EVALUATE; approve → COMPLETE (cli)."""
        unsure = ev.get("unsure")
        self.st.d["gate_pending"] = {"t": _now(), "snap": self._tree(), "unsure": unsure,
                                     "eval_no": self.st.d.get("eval_no"), "attempt_dir": ev.get("attempt_dir")}
        self.st.d.pop("eval", None)
        self.st.save()
        raise Stop("AWAITING_APPROVAL",
                   (f"⚠️평가자 판단 불확실 — Master가 직접 본다: {str(unsure)[:300]} · " if unsure else
                    "평가 통과(READY) · ") + "끝 승인 대기 — approve (후속 항목이 있으면 --follow-up) 또는 reject --reason")

    def s_complete(self):
        self.st.d["final_snapshot"] = gitutil.snapshot(self.target)
        self.st.set_status("COMPLETE")

    # ================= follow-up items (Master — approve/answer --follow-up) =================
    def _apply_follow_ups(self):
        ups = self.st.d.get("follow_ups") or []
        if not ups:
            return
        plan = self._read_plan()
        ids = {i["id"] for i in todo.parse(plan)[0]}
        new = []
        for u in ups:
            fid = todo.next_fix_id(plan + "".join(todo.item_md(x) for x in new))
            it = {"id": fid, "level": self.run_level, "text": f"(후속 · Master) {u['text']}", "targets": [u["target"]],
                  "criteria": u["text"], "location": u["target"], "deps": [u["after"]] if u.get("after") else []}
            bad = todo.json_item_problems([it], "FIX") + ([f"선행 {u['after']} 없음"] if u.get("after")
                                                           and u["after"] not in ids else [])
            bad += todo.contract_problems([it], self.cfg.get("contract_files"))
            if bad or not str(u.get("target") or "").strip():
                self.st.d["follow_ups"] = []  # P7: never re-apply a wrong input on the next resume
                self.st.save()
                raise self._help("follow_up", "후속 항목 입력 오류: " + "; ".join(bad or ["대상 파일 없음"]))
            new.append(it)
        self._write_plan(todo.append_items(plan, new))
        self.st.d["follow_ups"] = []
        self.st.timeline("후속 항목 추가(Master) — " + ", ".join(f"{i['id']} {i['targets'][0]}" for i in new))
        if self.st.d["stage"] != "BUILD":
            enter_build(self.st, self.target, "후속 항목 추가")
        self.st.save()

    # ================= account usage (S-8) =================
    def _note_usage(self, res, key):
        """The reading arrives ~2 s after a call STARTS: the use of the calls that started from one reading shows in
        the next reading. That rise is put on the key that made the last call (last_call_key — N12/B3)."""
        rate = getattr(res, "rate", None) or {}
        u = self.st.d.setdefault("usage", {})
        if not rate:
            u["last_call_key"] = key
            return
        last = u.setdefault("last", {})
        pc = self.lim["usage_pause"]
        for w, now in rate.items():
            prev = last.get(w)
            same = bool(prev) and prev["resetsAt"] == now.get("resetsAt")
            if same and now["utilization"] > prev["u"]:
                who = u.get("last_call_key") or key
                hist = u.setdefault("per_call_hist", {}).setdefault(who, {}).setdefault(w, [])
                hist.append(now["utilization"] - prev["u"])
                del hist[:-5]
                est = max(hist)
                if w == "five_hour":
                    est = min(est, pc["per_call_cap"])
                u.setdefault("per_call", {}).setdefault(who, {})[w] = est
            last[w] = {"u": now["utilization"], "resetsAt": now.get("resetsAt")}
        u["windows"] = rate
        u["last_call_key"] = key
        self.st.save()

    def _usage_verdict(self, windows, key, ahead=True):
        """ahead: before a call starts — the reading plus this call's expected use. Mid-call (ahead=False) the reading
        already holds the call's use so far, and only the threshold itself counts."""
        u = self.st.d.get("usage") or {}
        pause = self.lim["usage_pause"]
        for w, label in (("five_hour", "5시간"), ("seven_day", "주간")):
            x = (windows or {}).get(w)
            if not x or not x.get("resetsAt") or int(x["resetsAt"]) <= time.time():
                continue
            step = (((u.get("per_call") or {}).get(key) or {}).get(w) or pause["per_call_default"]) if ahead else 0.0
            if x["utilization"] + step < pause[w]:
                continue
            reset = dt.datetime.fromtimestamp(int(x["resetsAt"]))
            return w, (f"{label} 사용량 {x['utilization']:.0%}" + (f" + 이번 호출({key}) 예상 {step:.0%}" if ahead else "")
                       + f" ≥ {pause[w]:.0%} — 리셋 {reset:%m-%d %H:%M}"), reset
        return None

    def _usage_stop(self, w, why, reset, when=""):
        if w == "seven_day":
            return Stop("HELP", f"사용량 일시중지{when} · {why} · 계정 교체 뒤 resume, 아니면 리셋 뒤 resume", "usage7d")
        return LimitError(f"사용량 일시중지{when} · {why}", reset)

    def _usage_gate(self, key):
        v = self._usage_verdict((self.st.d.get("usage") or {}).get("windows"), key)
        if not v:
            return
        e = self._usage_stop(*v)
        if isinstance(e, Stop):
            raise e
        ((self.st.d.get("usage") or {}).get("windows") or {}).pop(v[0], None)
        self._wait_reset(e)

    def _rate_check(self, rate, first=True):
        """Every usage reading of a call (any thread — never touches state). At the threshold the call is cut at once,
        whatever it is doing (measured: one builder call ran 25 min+ past the weekly 97% because a mid-call
        reading only marked "stop before the next call"). The cut call keeps its session (inflight) — the resume
        continues it, told it was cut (S-3 · S-4)."""
        self._write_usage(rate)
        with self._hb_lock:
            key = next((c["key"] for c in self._calls_now.values()), BUILDER)
        if first:
            v = self._usage_verdict(rate, key)
            return self._usage_stop(*v, when=" · 호출 시작 때 판정(작업 전 취소)") if v else None
        v = self._usage_verdict(rate, key, ahead=False)
        return self._usage_stop(*v, when=" · 호출 중 판정(호출 즉시 끊음 · resume이 같은 세션으로 이어감)") if v else None

    def _wait_reset(self, e):
        """P18: one way only — the engine stops (PAUSED_USAGE) and its fallback resumes after the reset."""
        if e.reset_at is None:
            raise self._help("usage", f"사용량 한도 — 리셋 시각을 읽지 못함: {e}")
        wait = (e.reset_at - dt.datetime.now()).total_seconds() + 60
        if wait > self.lim["max_reset_wait_sec"]:
            raise self._help("usage", f"사용량 한도 — 리셋 {e.reset_at:%m-%d %H:%M} 까지 대기가 상한 초과")
        reset = e.reset_at
        self.st.d["paused_usage"] = {"reset_epoch": reset.timestamp(), "reset": f"{reset:%Y-%m-%d %H:%M}",
                                     "reason": str(e)[:300], "t": _now()}
        self.st.save()
        fb = float(self.lim["usage_pause"].get("fallback_min", 10))
        started = self.spawn_fallback(self.st.dir, reset.timestamp() + 60 + fb * 60, reset.timestamp())
        self.st.timeline(f"**사용량 일시중지** — 리셋 {reset:%m-%d %H:%M} (+60초) 뒤 재개 예정 · "
                         + (f"Master가 {fb:.0f}분 안에 재개 안 하면 엔진 대비책이 재개" if started
                            else "⚠️엔진 대비책을 띄우지 못함 — Master가 재개해야 함") + f" · {str(e)[:200]}")
        raise Stop("PAUSED_USAGE", f"리셋 {reset:%m-%d %H:%M} 뒤 재개 — Master: 사용자에게 알리고 "
                                   f"`wait-reset --run \"{self.st.dir}\"` 를 백그라운드로 · 원인: {str(e)[:200]}")
