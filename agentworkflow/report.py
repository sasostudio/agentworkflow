"""report.md — what the user reads to decide commit / resume / limit tuning (design §13)."""

import json
from pathlib import Path

from . import gitutil

NEXT = {
    "COMPLETE": "diff를 확인하고 사용자가 직접 커밋한다(엔진은 커밋하지 않는다). "
                "연속 모드 run이면 Master가 diff 확인 후 `campaign commit`(로컬 커밋, push 없음).",
    "HELP": "Master가 근본 원인을 찾아 조치한다(엔진 결함으로 보이면 근본 원인·재현을 사용자에게 보고). 조치 뒤 "
            "`supervise resume` · 작업자에게 방향을 줄 일이면 `reject --reason`(평가 반려 상한이면 이것만) · "
            "방향 질문 상한이면 `answer`.",
    "RUNNING": "실행 중(또는 비정상 종료). 프로세스가 없으면 `resume`.",
    # v6 decision 3: one end gate, always
    "AWAITING_APPROVAL": "끝 승인 — Master가 결과를 요청 의도와 대조한다(평가 판정·반박 검토·캡처 · 불확실 표시가 있으면 "
                         "그 곳을 직접 본다) → `approve`(고칠 파일이 있으면 `--follow-up 파일 --follow-up-text 할일`) 또는 "
                         "`reject --reason`(작업자에게). 사용자 판단 항목(보안·범위·명세·되돌릴 수 없는 일)이면 사용자에게 먼저.",
    "PAUSED_USAGE": "사용량 상한에서 엔진이 스스로 멈췄다. Master: 사용자에게 멈춤과 재개 예정 시각을 알리고 "
                    "`wait-reset --run …` 을 백그라운드로 → 끝나면(리셋+60초) 상태 확인 뒤 `supervise resume`. "
                    "Master가 재개하지 않으면 엔진 대비책이 fallback_min 뒤 스스로 재개한다.",
    "AWAITING_ANSWER": "작업자·평가자의 방향 질문에 Master가 답한다 → `answer --text`. 명세·코드로 답이 안 나오면 사용자에게 묻는다.",
}


def write(state, target):
    d = state.d
    c = d["counters"]
    lines = [
        f"# 실행 보고서 — {d['run_id']}",
        "",
        f"- 상태: **{d['status']}**" + (f" — {d['pause_reason']}" if d.get("pause_reason") else ""),
        f"- 현재 단계: {d['stage']} (라운드 {d.get('round', 0)}) · run 난이도 L{d.get('run_level', '?')}"
        + (f" · HELP 종류 `{d['help_kind']}`" if d.get("help_kind") else ""),
        f"- 대상: `{d.get('target')}` · 브랜치 `{d.get('branch')}` · 시작 HEAD `{(d.get('base_head') or '')[:10]}`",
        f"- AI 호출 {c['calls']}회 · 비용 지표 ${c['cost_usd']:.4f} (이 run 호출들의 실제 몫 — 정가 환산값, 구독 실사용량과 "
        "다를 수 있음)" + (f" · 비용 미상 호출 {c['cost_unknown']}회(끊긴 호출 — 그 비용은 같은 세션의 다음 호출에 합쳐짐)"
                          if c.get("cost_unknown") else ""),
        f"- 평가 {d.get('eval_no', 0)}회차 · 평가 반려 {c.get('eval_rejects', 0)}/{c.get('eval_reject_cap')} · "
        f"누적 기계검사 실패 {c.get('check_fails_total', 0)}",
        "",
        "## 다음 행동",
        NEXT.get(d["status"], ""),
        "",
        "## 단계 이력",
    ]
    for h in d["history"]:
        lines.append(f"- {h['t']} {h['from']} → {h['to']} {h.get('note', '')}")
    if d.get("skipped"):
        lines += ["", "## 건너뛴 단계"] + [f"- {s}" for s in d["skipped"]]
    if d.get("eval_log"):
        lines += ["", "## 평가 판정 기록", "| 회차 | 라운드 | 단계 | 판정 |", "|:-:|:-:|---|---|"]
        lines += [f"| {e.get('eval_no')} | {e.get('round')} | {e.get('phase')} | {e.get('verdict')} |" for e in d["eval_log"]]
    if d.get("rework"):
        lines += ["", "## 작업자에게 보낼 사유 (아직 안 보냄)"]
        lines += [f"- [{r['source']}] {r['reason'][:300]}" for r in d["rework"]]
    if d.get("approvals"):
        lines += ["", "## 승인 (Master)"] + [f"- {a['t']} [{a.get('gate', 'PLANNING')}] **{a['decision']}** "
                                            f"{a.get('note', '')}" for a in d["approvals"]]
    gp = d.get("gate_pending")
    if gp:
        lines += ["", f"## 끝 승인 대기 — 평가 {gp.get('eval_no')}회차 ({gp.get('t')})"]
        if gp.get("attempt_dir"):
            lines.append(f"- 마지막 시나리오 시도: `{gp['attempt_dir']}`")
        if gp.get("unsure"):
            lines.append(f"- ⚠️ 평가자 판단 불확실 — Master가 직접 볼 곳: {gp['unsure']}")
    if d.get("paused_usage"):
        pu = d["paused_usage"]
        lines += ["", "## 사용량 일시중지", f"- 리셋 {pu.get('reset')} (+60초) 뒤 재개 · {pu.get('reason', '')}"]
    if d.get("questions"):
        lines += ["", "## 방향 질문 · 답변"]
        for q in d["questions"]:
            lines.append(f"- {q['t']} [{q['stage']} · {q['role']}] Q: {q['q']}")
            if q.get("a"):
                lines.append(f"  - A ({q.get('a_t', '')}): {q['a']}")
    if d.get("denials"):
        lines += ["", "## 권한 거부 (허용 안 된 도구·경로 시도)"]
        lines += [f"- {x['stage']} r{x['round']} `{x['role']}` → `{x['tool']}` {x['input']}" for x in d["denials"]]
    if d.get("bypass_findings"):
        lines += ["", "## 우회 흔적 (축 L — 평가자에게 전달된 것)"]
        lines += [f"- r{b['round']} `{b['path']}` · {b['kind']}: `{b['line']}`" for b in d["bypass_findings"]]
    qa_final = sorted(Path(state.dir, "03-evaluate").glob("e*_a*/results_final.json"), key=lambda p: p.stat().st_mtime)
    if qa_final:
        j = json.loads(qa_final[-1].read_text(encoding="utf-8"))
        lines += ["", f"## 마지막 시나리오 실행 ({qa_final[-1].parent.name}) — {j['verdict']}"]
        lines += [f"- {r['id']} {r['title']} → **{r['status']}** {r.get('note', '')}" for r in j["results"]]
    try:
        changed = gitutil.dirty_paths(target)
    except Exception as e:  # report must still be written
        changed = [f"(git 조회 실패: {e})"]
    lines += ["", f"## 변경 파일 ({len(changed)})"] + [f"- `{p}`" for p in changed]
    lines += ["", "원문 전체: `raw/` · 시각순 판정 이력: `timeline.md`"]
    (state.dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return state.dir / "report.md"
