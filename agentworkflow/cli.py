"""CLI: run / resume / approve / reject / answer / wait-reset / status / unlock / report / campaign / supervise /
dashboard / dashboard-sync / dashboard-deploy.

v6 (10-02 · 설계서 v6 §4): which command works in which stop is the table in §4 — every refusal names the right
command. Every command that continues a run refuses a v5 run first, before reading any setting (N11)."""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from . import capabilities, engine, flow, gitutil, prompts, report, roles, safety, uploader
from .campaign import Campaign, CampaignError, feature_ids
from .adapters import claude_version, make_adapter
from .config import ConfigError, load_config
from .engine import Engine
from .request import RequestError, load_request
from .state import V5_STAGES, RunState, new_counters, new_run_dir


def _out(msg):
    print(msg, flush=True)


def _check_rules(cfg):
    """The working rules, skill files and role definitions every role is pointed at must exist (they are the rules)."""
    missing = [p for p in prompts.skill_paths(cfg["rules_root"]).values() if not Path(p).is_file()]
    missing += roles.missing(cfg["rules_root"], cfg.get("role_definitions", True))
    if missing:
        raise ConfigError("규칙 파일 없음(rules_root 확인): " + ", ".join(missing))


def _adapters(cfg, run_dir):
    """Writes the run's MCP config (servers granted in capabilities only — URL/command, never tokens)."""
    try:
        mcp = capabilities.resolve_mcp(cfg["capabilities"].get("mcp_servers", []))
    except capabilities.CapabilityError as e:
        raise ConfigError(str(e)) from e
    path = Path(run_dir) / "00-request" / "mcp.json"
    path.write_text(json.dumps(mcp, ensure_ascii=False, indent=2), encoding="utf-8")
    return (make_adapter(cfg["providers"]["work"], cfg, path),
            make_adapter(cfg["providers"]["review"], cfg, path))


def _with_campaign(cfg, st_or_name):
    """v6 H6: the campaign plan's `- Wiki: 있음` lets the engine create the wiki skeleton."""
    name = st_or_name if isinstance(st_or_name, str) else ((st_or_name.d.get("campaign") or {}).get("name"))
    if name:
        cfg = dict(cfg)
        cfg["wiki_create"] = bool(Campaign(name).plan().get("wiki"))
    return cfg


def cmd_run(a):
    cfg = load_config(a.config)
    _check_rules(cfg)
    req = load_request(a.request)
    flow.decide(req, cfg)  # P17: a wrong `## 흐름` / `## 항목` is refused before anything is created
    target = Path(cfg["target"]).resolve()
    camp = None
    if a.campaign or a.feature:  # continuous mode (design §21)
        if not (a.campaign and a.feature):
            raise CampaignError("연속 모드는 --campaign 과 --feature 를 함께 준다")
        camp = Campaign(a.campaign)
        camp.check_run(a.feature, target, cfg, run_level=req["run_level"])
        cfg = _with_campaign(cfg, a.campaign)
    safety.preflight(target)
    held = safety.read_lock(target)
    if held:
        raise safety.SafetyError(f"이 프로젝트에 쓰기 실행이 이미 있음(run {held.get('run_id')}) — status 확인 또는 unlock")
    run_dir = new_run_dir(a.title or req["title"])
    shutil.copyfile(a.request, run_dir / "00-request" / "request.md")
    shutil.copyfile(cfg["_path"], run_dir / "00-request" / "engine.json")
    w, r = _adapters(cfg, run_dir)
    safety.acquire_lock(target, run_dir.name)
    counters = new_counters()
    counters["eval_reject_cap"] = cfg["limits"]["max_eval_rejects"]
    st = RunState.create(run_dir, target=str(target), config_path=cfg["_path"],
                         branch=gitutil.current_branch(target), base_head=gitutil.head(target), counters=counters)
    st.d["cli_version"] = claude_version()
    if camp:
        ids = feature_ids(a.feature)
        st.d["campaign"] = {"name": camp.name, "feature": ids[0], "features": ids}
        camp.record_run(a.feature, run_dir, run_dir.name)
    st.save()
    cap = cfg["capabilities"]
    st.timeline(f"run 시작(v6) — 대상 `{target}` 브랜치 `{st.d['branch']}` · Claude Code {st.d['cli_version']} · "
                f"도구 허용: 웹 {cap['web_roles'] or '없음'} / MCP {cap['mcp_servers'] or '없음'} / "
                f"플러그인 {cap['plugins'] or '없음'}")
    _out(f"run: {run_dir}")
    uploader.ensure_running(_out)  # 대시보드 V2: the cloud copy follows the run (never stops it)
    status = Engine(cfg, req, st, w, r, out=_out).run()
    _campaign_status(st, status)
    return 0 if status == "COMPLETE" else 3


def _campaign_status(st, status):
    c = st.d.get("campaign")
    if c:
        Campaign(c["name"]).update_status(st.d["run_id"], status)


def refuse_v5(run_dir):
    """N11 · B9: a v5 run cannot continue under v6 — refused before any setting is read."""
    p = Path(run_dir) / "state.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    if d.get("stage") in V5_STAGES or "stage_round" in d:
        raise safety.SafetyError(f"v5 run(단계 {d.get('stage')}) — v6 엔진으로 이어갈 수 없다. 사용자 결정으로 포기 처리"
                                 "(timeline 기록 · 잠금 해제 `unlock`) 뒤 새 run으로")


def _continue(run_dir, config=None, note=None, mutate=None):
    """Shared by resume / approve / reject / answer: reload, re-lock, optionally change state, run (§4 공통)."""
    refuse_v5(run_dir)
    st = RunState.load(run_dir)
    if st.d["status"] == "COMPLETE":
        _out("이미 COMPLETE")
        return 0
    cfg = _with_campaign(load_config(config or st.d["config_path"]), st)
    _check_rules(cfg)
    req = load_request(run_dir / "00-request" / "request.md")
    target = Path(cfg["target"]).resolve()
    safety.preflight(target, require_clean=False)
    lock = safety.read_lock(target)
    if lock and lock.get("run_id") != st.d["run_id"]:
        raise safety.SafetyError(f"다른 run({lock.get('run_id')})이 이 프로젝트를 잠그고 있음")
    prev, reason = st.d["status"], st.d.get("pause_reason") or ""
    if st.d.get("campaign"):
        camp = Campaign(st.d["campaign"]["name"])
        camp.ensure_active()
        if gitutil.current_branch(target) != camp.plan()["branch"]:
            raise CampaignError(f"캠페인 브랜치 {camp.plan()['branch']} 가 아님 — 재개 거부")
    if mutate:
        mutate(st, cfg, target)
    if lock:
        safety.force_unlock(target)
    safety.acquire_lock(target, st.d["run_id"])
    reset_on_resume(st)
    st.d["cli_version"] = claude_version()
    st.timeline(f"{note or 'resume'} — 직전 {prev}: {reason[:200]} (자동반려·기계검사·무진전 카운터 초기화) · "
                f"Claude Code {st.d['cli_version']}")
    st.save()
    w, r = _adapters(cfg, run_dir)
    uploader.ensure_running(_out)  # 대시보드 V2: the cloud copy follows the run (never stops it)
    status = Engine(cfg, req, st, w, r, out=_out).run()
    _campaign_status(st, status)
    return 0 if status == "COMPLETE" else 3


def reset_on_resume(st):
    """§3 「재개 때」: per-stage counters start over; eval_rejects and round never do. 10-03: the scenario runs of the
    evaluation in progress too (an `eval_runs` HELP resumes at the stopped run, after the Master fixed the cause)."""
    c = st.d["counters"]
    c["self_fix"] = 0
    c["check_fails"] = 0
    c["no_progress"] = 0
    st.d.pop("retries", None)
    if st.d.get("eval"):
        st.d["eval"]["runs"] = 0


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def can_answer(st):
    """A question is waiting: AWAITING_ANSWER, or HELP because of the question limit with the last one unanswered."""
    qs = st.d.get("questions") or []
    if st.d["status"] == "AWAITING_ANSWER":
        return True
    return st.d["status"] == "HELP" and bool(qs) and not qs[-1].get("a") and st.d.get("help_kind") == "questions"


def cmd_resume(a):
    run_dir = Path(a.run).resolve()
    refuse_v5(run_dir)
    st = RunState.load(run_dir)
    if st.d["status"] == "AWAITING_APPROVAL":
        raise safety.SafetyError("끝 승인 대기 — resume 대신 approve 또는 reject --reason")
    if can_answer(st):
        raise safety.SafetyError("답을 기다리는 방향 질문이 있음 — resume 대신 answer --text")
    if st.d["status"] == "HELP" and st.d.get("help_kind") == "eval_cap":  # §4 ⑧
        raise safety.SafetyError("평가 반려 상한 — 같은 반려가 반복된다. resume 대신 reject --reason 으로 방향을 주거나 포기")
    return _continue(run_dir, a.config)


def _expect(st, *statuses):
    if st.d["status"] not in statuses:
        raise safety.SafetyError(f"현재 상태가 {st.d['status']} — 이 명령은 {' / '.join(statuses)} 일 때만")


def add_follow_ups(st, targets, texts, after=None):
    """One new item per file (`--follow-up` + `--follow-up-text`, repeatable in pairs — P14)."""
    targets, texts = list(targets or []), list(texts or [])
    if len(targets) != len(texts) or any(not (t or "").strip() for t in texts):
        raise safety.SafetyError("--follow-up 마다 --follow-up-text(할 일)가 하나씩 필요")
    for t, x in zip(targets, texts):
        st.d.setdefault("follow_ups", []).append({"target": t.strip().replace("\\", "/"), "text": x.strip(),
                                                  "after": after})
    return bool(targets)


def approve_gate(st, cfg, target, note, follow_up=None, follow_up_text=None, after=None):
    """§4 ⑩⑪ · P10 · P12: approve the end gate. The tree must still be the one the gate was opened on."""
    gate = st.d.get("gate_pending") or {}
    now = engine.tree_hash(target, {f"{cfg['plan_dir'].rstrip('/')}/{st.d['run_id']}.md"}
                           | set(st.d.get("check_artifacts") or []))
    if gate.get("snap") and gate["snap"] != now:
        raise safety.SafetyError("끝 승인 뒤 작업 트리가 바뀜(평가 안 받은 변경) — 승인 거부. 바꾼 것을 되돌리거나 "
                                 "reject --reason 으로 작업자에게 보낸다")
    st.d.setdefault("approvals", []).append({"t": _now(), "decision": "승인", "gate": "END", "note": note or "",
                                             "unsure": gate.get("unsure")})
    if gate.get("unsure"):  # P10: the Master was the second pair of eyes — this tree is the accepted one
        st.save_snapshot("accepted", gitutil.snapshot(target), "Master 승인(불확실)")
    st.d.pop("gate_pending", None)
    if add_follow_ups(st, follow_up, follow_up_text, after):
        engine.raise_cap(st, cfg)  # N7: a Master intervention
        st.save()
        return  # the engine adds the items and goes back to BUILD (Engine._apply_follow_ups)
    st.goto("COMPLETE", "Master 끝 승인")


def cmd_approve(a):
    run_dir = Path(a.run).resolve()
    refuse_v5(run_dir)
    _expect(RunState.load(run_dir), "AWAITING_APPROVAL")
    return _continue(run_dir, a.config, note="Master 승인", mutate=lambda st, cfg, target: approve_gate(
        st, cfg, target, a.note, a.follow_up, a.follow_up_text, a.after))


def reject_to_build(st, cfg, target, reason):
    """§4 ⑤⑧⑨⑩⑪: the Master's reason → rework (appended — P11) · cap raised (N7) · back to BUILD."""
    gate = st.d["status"] == "AWAITING_APPROVAL"
    st.d.setdefault("approvals", []).append({"t": _now(), "decision": "반려", "gate": "END" if gate else "HELP",
                                             "note": reason, "help_kind": st.d.get("help_kind")})
    engine.add_rework(st, f"Master 반려 ({'끝 승인' if gate else 'HELP'}):\n{reason}",
                      "master_gate" if gate else "master_help")
    # v6 §6-1 H9 (implemented 10-03): the same words reach the evaluator — a reason about the scenarios is its to act on
    st.d.setdefault("master_notes", []).append({"t": _now(), "reason": reason})
    engine.raise_cap(st, cfg)
    st.d.pop("help_kind", None)
    engine.enter_build(st, target, "Master 반려 → 작업자")


def cmd_reject(a):
    run_dir = Path(a.run).resolve()
    refuse_v5(run_dir)
    st = RunState.load(run_dir)
    _expect(st, "AWAITING_APPROVAL", "HELP")
    if can_answer(st):
        raise safety.SafetyError("답을 기다리는 방향 질문이 있음 — reject 대신 answer --text")
    return _continue(run_dir, a.config, note="Master 반려",
                     mutate=lambda st, cfg, target: reject_to_build(st, cfg, target, a.reason))


def cmd_wait_reset(a):
    """After PAUSED_USAGE the Master runs this in the background — it ends at the reset (+60 s), which wakes the
    Master. (The fallback that resumes without the Master was started by the engine as it paused.)"""
    from . import waitreset
    return waitreset.run(Path(a.run).resolve(), out=_out)


def answer_question(st, cfg, target, text, follow_up=None, follow_up_text=None, after=None):
    """§4 ④⑦: the answer goes into `questions` (every later call carries it). Follow-up items = a Master intervention."""
    q = (st.d.get("questions") or [{}])[-1]
    q["a"], q["a_t"] = text, _now()
    st.d.pop("help_kind", None)
    if add_follow_ups(st, follow_up, follow_up_text, after):
        engine.raise_cap(st, cfg)
    st.save()


def cmd_answer(a):
    run_dir = Path(a.run).resolve()
    refuse_v5(run_dir)
    if not can_answer(RunState.load(run_dir)):
        raise safety.SafetyError("답을 기다리는 방향 질문이 없음")
    return _continue(run_dir, a.config, note="Master 답변", mutate=lambda st, cfg, target: answer_question(
        st, cfg, target, a.text, a.follow_up, a.follow_up_text, a.after))


def cmd_campaign(a):
    c = Campaign(a.name)
    if a.action == "create":
        c.create(a.plan)
        _out(f"생성: {c.dir} (DRAFT) — 사용자 승인 뒤 `campaign approve`")
    elif a.action == "approve":
        if not a.note:
            raise CampaignError("--note 에 사용자 승인 근거(언제·무엇을)를 적는다")
        c.approve(a.note)
        _out(c.status_text())
    elif a.action == "status":
        _out(c.status_text())
    elif a.action == "abandon":
        if not (a.run and a.note):
            raise CampaignError("abandon 은 --run 과 --note(사용자 결정 근거) 필요")
        c.abandon(RunState.load(Path(a.run).resolve()).d["run_id"], a.note)
        _out("포기 기록 — 작업 트리 정리(stash 등)는 사용자가 한다\n" + c.status_text())
    elif a.action == "commit":
        if not a.run:
            raise CampaignError("--run 필요")
        head = c.commit(Path(a.run).resolve())
        _out(f"커밋 {head[:10]} (push 안 함)\n" + c.status_text())
    return 0


def cmd_status(a):
    st = RunState.load(Path(a.run).resolve())
    d = st.d
    c = d["counters"]
    _out(f"{d['run_id']} · {d['status']} · {d['stage']} r{d.get('round', d.get('stage_round'))} · 호출 {c['calls']} · "
         f"${c['cost_usd']:.4f}" + (f" (+미상 {c['cost_unknown']}회)" if c.get("cost_unknown") else "")
         + f" · 평가 반려 {c.get('eval_rejects', 0)}/{c.get('eval_reject_cap')}")
    if d.get("pause_reason"):
        _out(f"사유: {d['pause_reason']}" + (f" ({d['help_kind']})" if d.get("help_kind") else ""))
    rep = st.dir / "report.md"
    if rep.exists():
        _out(f"보고서: {rep}")
    return 0


def cmd_unlock(a):
    cfg = load_config(a.config)
    _out(safety.force_unlock(Path(cfg["target"]).resolve()))
    return 0


def cmd_report(a):
    st = RunState.load(Path(a.run).resolve())
    _out(str(report.write(st, st.d["target"])))
    return 0


def cmd_supervise(a):
    """v4 §6: run any engine command under the supervisor (auto-resume on crash / stall; exits only when someone is
    needed). Usage: `python -m agentworkflow supervise run --config … --request …` (or resume/approve/answer …)."""
    from .config import DEFAULTS
    from .supervisor import Supervisor
    rest = [x for x in a.rest if x != "--"]
    if not rest:
        raise safety.SafetyError("supervise 뒤에 감시할 엔진 명령(run/resume/approve/answer/…)을 쓴다")
    sv = dict(DEFAULTS["supervisor"])
    if "--config" in rest:
        try:
            sv.update(load_config(rest[rest.index("--config") + 1]).get("supervisor") or {})
        except (ConfigError, IndexError):
            pass
    return Supervisor(rest, out=_out, stall_sec=sv["stall_sec"], poll_sec=sv["poll_sec"],
                      restart_window_sec=sv["restart_window_sec"], max_restarts=int(sv["max_restarts"])).run()


def cmd_dashboard(a):
    """대시보드 설계서 §7: read-only local web page over ai-log/ and campaigns/ (127.0.0.1 only · no AI call)."""
    from . import dashboard
    return dashboard.serve(a.port or dashboard.DEFAULT_PORT, open_browser=not a.no_browser, out=_out)


def cmd_dashboard_sync(a):
    """대시보드 설계서 §10: the uploader process (run/resume/approve/reject/answer start it — rarely by hand)."""
    return uploader.main()


def cmd_dashboard_deploy(a):
    """대시보드 설계서 §10: publish the site (dashboard.html + public config) to Vercel."""
    from . import dashboard_site
    try:
        return dashboard_site.deploy(out=_out)
    except (dashboard_site.SiteError, uploader.ConfigMissing) as e:
        _out(f"오류: {e}")
        return 2


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="agentworkflow")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--request", required=True)
    r.add_argument("--title")
    r.add_argument("--campaign", help="연속 모드 캠페인 이름(설계서 §21)")
    r.add_argument("--feature", help="이번 run이 맡는 캠페인 기능 ID(F-01…) — 순서대로만 · 묶음은 쉼표(F-11,F-12)")
    r.set_defaults(fn=cmd_run)
    cp = sub.add_parser("campaign", help="연속 모드: create / approve / status / commit / abandon")
    cp.add_argument("action", choices=["create", "approve", "status", "commit", "abandon"])
    cp.add_argument("--name", required=True)
    cp.add_argument("--plan", help="create: 캠페인 계획 md")
    cp.add_argument("--note", help="approve/abandon: 사용자 결정 근거")
    cp.add_argument("--run", help="commit/abandon: run 폴더")
    cp.set_defaults(fn=cmd_campaign)
    s = sub.add_parser("resume", help="RUNNING(죽음) · HELP(평가 반려 상한 제외) · PAUSED_USAGE 에서 이어서")
    s.add_argument("--run", required=True)
    s.add_argument("--config", help="기본: run 시작 때의 설정 경로(수정된 limits 반영됨)")
    s.set_defaults(fn=cmd_resume)
    ap = sub.add_parser("approve", help="AWAITING_APPROVAL(끝 승인): 승인 → COMPLETE · --follow-up 이면 항목 추가 → 작업자")
    ap.add_argument("--run", required=True)
    ap.add_argument("--note", help="승인 메모(보고서에 남음)")
    ap.add_argument("--follow-up", dest="follow_up", action="append",
                    help="승인하며 고칠 파일 하나(새 FIX 항목) — 여러 번 가능, --follow-up-text 와 짝")
    ap.add_argument("--follow-up-text", dest="follow_up_text", action="append", help="그 항목이 할 일(완료 조건)")
    ap.add_argument("--after", help="후속 항목들의 선행 항목 ID")
    ap.add_argument("--config")
    ap.set_defaults(fn=cmd_approve)
    rj = sub.add_parser("reject", help="AWAITING_APPROVAL · HELP: 사유를 작업자에게(rework) → BUILD")
    rj.add_argument("--run", required=True)
    rj.add_argument("--reason", required=True)
    rj.add_argument("--config")
    rj.set_defaults(fn=cmd_reject)
    an = sub.add_parser("answer", help="AWAITING_ANSWER: 방향 질문에 답하고 이어서 실행")
    an.add_argument("--run", required=True)
    an.add_argument("--text", required=True)
    an.add_argument("--follow-up", dest="follow_up", action="append",
                    help="후속 항목: 이 파일 하나를 고치는 새 항목(FIX-###)을 더한다 — 여러 번 가능")
    an.add_argument("--follow-up-text", dest="follow_up_text", action="append", help="후속 항목이 할 일(완료 조건)")
    an.add_argument("--after", help="후속 항목의 선행 항목 ID(예: DEV-702)")
    an.add_argument("--config")
    an.set_defaults(fn=cmd_answer)
    wr = sub.add_parser("wait-reset", help="PAUSED_USAGE: 리셋(+60초)까지 기다렸다 끝남(=Master 깨움)")
    wr.add_argument("--run", required=True)
    wr.set_defaults(fn=cmd_wait_reset)
    t = sub.add_parser("status")
    t.add_argument("--run", required=True)
    t.set_defaults(fn=cmd_status)
    u = sub.add_parser("unlock")
    u.add_argument("--config", required=True)
    u.set_defaults(fn=cmd_unlock)
    q = sub.add_parser("report")
    q.add_argument("--run", required=True)
    q.set_defaults(fn=cmd_report)
    sv = sub.add_parser("supervise", help="v4 §6: 엔진 명령을 감시자 아래에서 실행(죽거나 멈추면 자동 재개)")
    sv.add_argument("rest", nargs=argparse.REMAINDER)
    sv.set_defaults(fn=cmd_supervise)
    db = sub.add_parser("dashboard", help="엔진 대시보드 — 읽기 전용 · 이 PC(127.0.0.1)에서만 · 토큰 0")
    db.add_argument("--port", type=int, help="기본 8799")
    db.add_argument("--no-browser", dest="no_browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    db.set_defaults(fn=cmd_dashboard)
    ds = sub.add_parser("dashboard-sync", help="클라우드 대시보드 업로더(run 시작·재개 때 자동으로 뜸 · 할 일 없으면 10분 뒤 종료)")
    ds.set_defaults(fn=cmd_dashboard_sync)
    dd = sub.add_parser("dashboard-deploy", help="클라우드 대시보드 사이트를 Vercel에 배포(.env.dashboard 필요)")
    dd.set_defaults(fn=cmd_dashboard_deploy)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (ConfigError, RequestError, safety.SafetyError) as e:
        _out(f"오류: {e}")
        return 2
