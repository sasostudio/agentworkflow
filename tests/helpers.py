"""Test fixtures. Temp repos live under <engine>/.testtmp/<this run>/.

Cleanup: at process exit the tests delete ONLY the folder this
process created (marked with .created_by_pid). Nothing else under .testtmp/ is touched.
Set AW_KEEP_TESTTMP=1 to keep it for debugging."""

import atexit
import itertools
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from agentworkflow import waitreset
from agentworkflow.adapters import Result
from agentworkflow.config import DEFAULTS, _merge, validate
from agentworkflow.state import RunState, new_counters, new_run_dir

# v5: the usage-pause fallback is a REAL detached process in production — a test must never start one. (10-01: the first
# v5 test run started 13 of them; after the reset they would have resumed test runs through the real Claude adapter,
# and their open log files kept the test folder from being cleaned up.) Every Engine reads this at construction.
FALLBACKS = []


def _fake_spawn_fallback(run_dir, at, reset_epoch):
    FALLBACKS.append({"run_dir": str(run_dir), "at": at, "reset_epoch": reset_epoch})
    return True


waitreset.spawn_fallback = _fake_spawn_fallback

TESTTMP = Path(__file__).resolve().parent.parent / ".testtmp"
ROOT = TESTTMP / f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
ROOT.mkdir(parents=True)
(ROOT / ".created_by_pid").write_text(str(os.getpid()), encoding="utf-8")
_n = itertools.count(1)


def _force_writable(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)  # git object files are read-only on Windows
    func(path)


def _cleanup_own_root():
    if os.environ.get("AW_KEEP_TESTTMP") == "1":
        return
    marker = ROOT / ".created_by_pid"
    # guard: only our own folder, directly under .testtmp, created by this very process
    if not (ROOT.parent == TESTTMP and marker.is_file()
            and marker.read_text(encoding="utf-8").strip() == str(os.getpid())):
        return
    # delete everything EXCEPT the marker first; the marker goes last so a half-finished cleanup
    # (a file briefly locked by a watcher/AV on Windows) is still recognisably ours (09-24: 2 orphans)
    for attempt in range(5):
        try:
            for child in ROOT.iterdir():
                if child == marker:
                    continue
                if child.is_dir():
                    shutil.rmtree(child, onerror=_force_writable)
                else:
                    _force_writable(os.remove, child, None)
            marker.unlink()
            ROOT.rmdir()
            return
        except OSError:
            time.sleep(0.5)


atexit.register(_cleanup_own_root)

# tests must not touch the engine's real locks/ folder
from agentworkflow import safety as _safety  # noqa: E402
_safety.LOCK_DIR = ROOT / "locks"

REQUEST = """# 테스트 작업

## 목표
- hello.txt 에 인사말이 있다

## 구현 범위
- hello.txt 작성

## 범위 외
- 다른 파일

## 주의
- 없음

## QA 시나리오 힌트
- 정상: 인사말 확인

## 난이도
L{level}
근거: 테스트

## 항목
{items}
{extra}"""

QA_RUNNER = r'''
import json, sys, pathlib
manifest, ev, res = map(pathlib.Path, sys.argv[1:4])
out = []
for m in json.loads(manifest.read_text(encoding="utf-8")):
    code = pathlib.Path(m["path"]).read_text(encoding="utf-8")
    # AFTERFIX: fails until the QA_FIX developer wrote "hi2" into hello.py (a rework cycle re-runs the same scenario)
    fixed = "AFTERFIX" in code and pathlib.Path("hello.py").is_file() and "hi2" in pathlib.Path("hello.py").read_text()
    st = "PASSED" if ("PASS" in code or fixed) else ("BLOCKED" if "BLOCK" in code else "FAILED")
    evid = []
    if "EVIDENCE" in code:
        (ev / m["evidence_dir"]).mkdir(parents=True, exist_ok=True)
        (ev / m["evidence_dir"] / "shot.png").write_bytes(b"png")
        evid = ["shot.png"]
    out.append({"id": m["id"], "status": st, "note": "fake", "evidence": evid})
res.write_text(json.dumps(out), encoding="utf-8")
'''


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def make_repo(branch="feature/t"):
    repo = ROOT / f"repo{next(_n)}"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# t\n", encoding="utf-8")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    if branch != "main":
        git(repo, "checkout", "-q", "-b", branch)
    return repo


def make_cfg(repo, **over):
    runner = repo.parent / "qa_runner.py"
    if not runner.exists():
        runner.write_text(QA_RUNNER, encoding="utf-8")
    # v6: the engine.json keys of removed parts are refused (config.check_removed) — tests use v6 keys only
    base = {"target": str(repo),
            "check_commands": [f'"{sys.executable}" -c "import pathlib,sys; p=pathlib.Path(\'FAIL\'); sys.exit(1 if p.exists() and p.read_text().strip()==\'fail\' else 0)"'],
            "qa": {"runner": "command", "environment": "external",
                   "command": f'"{sys.executable}" "{runner}" "{{manifest}}" "{{evidence}}" "{{results}}"'},
            "wiki_create": True}
    cfg = _merge(_merge(DEFAULTS, base), over)
    validate(cfg)
    cfg["_path"] = str(repo / "engine.json")
    return cfg


def make_request(level=1, items=None, extra="", path=False):
    """v6: `## 항목` is required — items: item() strings (default one hello.py item). extra: more sections (e.g.
    `## 흐름\n- QA: 없음`). path=True -> (request, path) for the CLI tests."""
    from agentworkflow.request import load_request
    p = ROOT / f"request{next(_n)}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(items if items is not None else [item(level=level)])
    p.write_text(REQUEST.replace("{level}", str(level)).replace("{items}", body).replace("{extra}", extra),
                 encoding="utf-8")
    return (load_request(p), p) if path else load_request(p)


def make_state(repo, cap=3):
    run_dir = new_run_dir("test", root=ROOT / "ai-log")
    c = new_counters()
    c["eval_reject_cap"] = cap
    return RunState.create(run_dir, target=str(repo), counters=c)


# ---------- scripted agent behaviours ----------

def verdict(v="READY", issues=None, level_changes=None):
    return lambda kw: Result(text="ok", structured={"verdict": v, "issues": issues or [], "reason": v,
                                                    "checked": ["A", "B", "G"],
                                                    "level_changes": level_changes or []})


def item(tid="DEV-001", level=1, text="hello.txt 작성", target="hello.py", criteria="인사말 출력",
         location="hello.py 한 파일(기능 1개)", deps="없음"):
    # deps: §23-6 S5 `선행:` line is required — None leaves it out (for the negative tests)
    loc = f"  - 기능 위치: {location}\n" if location else ""
    dep = f"  - 선행: {deps}\n" if deps is not None else ""
    return f"- [ ] {tid} [L{level}] {text}\n  - 대상: {target}\n  - 완료 조건: {criteria}\n" + loc + dep


GATE = "작성게이트: W1 grep 0건 → 신규 / W2 해당없음 / W3 통과 / W4 hello.py 한 곳"


def items_json(md):
    """v4 §9 fixture bridge: item() markdown (the readable way these tests describe a TODO) -> the planner's JSON
    items. A missing attribute becomes an empty field (the engine then leaves the line out and check_plan names it)."""
    from agentworkflow import todo
    out = []
    for i in todo.parse(md)[0]:
        it = {"id": i["id"], "text": i["text"], "targets": i["target_files"], "criteria": i["criteria"] or "",
              "location": i["location"] or ""}
        if i["level"] is not None:
            it["level"] = i["level"]
        if i["deps"] is not None:
            it["deps"] = i["deps"]
        out.append(it)
    return out


def plan_writer(items=None, design=False, raw=None, designs=None, question=""):
    """The planner's v4 JSON (PLAN_SCHEMA). items: item() strings · raw: a whole markdown TODO body (for broken
    plans) · design=True: the `## 설계` text required at run level >= 2 · designs: L3 5-part designs (merged mode)."""
    items = items if items is not None else [item()]

    def fn(kw):
        md = raw if raw is not None else "".join(items)
        return Result(text="planned", structured={
            "summary": "접근 (조사: hello.py:1)", "design": "- 파일: hello.py\n- 흐름: print" if design else "",
            "items": items_json(md), "designs": list(designs or []), "question": question})
    return fn


def design_json(tid, skip=()):
    parts = {"files": "파일", "signature": "시그니처", "flow": "흐름", "boundary": "경계", "verify": "검증"}
    return {"id": tid, "title": "설계", **{k: ("" if v in skip else "내용") for k, v in parts.items()}}


def designer(ids=("DEV-001",), skip=()):
    """The designer's v4 JSON (DESIGN_SCHEMA): the five parts per id (parts named in `skip` left empty)."""
    return lambda kw: Result(text="designed", structured={"designs": [design_json(i, skip) for i in ids],
                                                          "question": ""})


def _plan_path(kw):
    import re
    m = re.search(r"`(docs/plan/[^`]+\.md)`", kw["prompt"])
    return Path(kw["cwd"]) / m.group(1)


def fix_adder(fid="FIX-001", text="(QA S1) 고치기", level=1):
    """The QA-fix planner's v4 JSON (FIX_SCHEMA)."""
    return lambda kw: Result(text="fix added", structured={"items": items_json(item(fid, level, text)),
                                                           "question": ""})


def developer(file="hello.py", body="print(1)\n", check_ids=None, evidence=None, tamper=False, summary=None):
    """The single developer's v4 JSON (DEV_SCHEMA): writes `file`, reports every unchecked item (or `check_ids`) as
    done with `files` = evidence. tamper=True edits the TODO text itself (a dev must not). summary: old prose
    summary — its `작성게이트:` / `방향 질문:` lines become the JSON fields (absent gate line = empty write_gate)."""
    def fn(kw):
        import re
        cwd = Path(kw["cwd"])
        p = _plan_path(kw)
        (cwd / file).write_text(body, encoding="utf-8")
        text = p.read_text(encoding="utf-8")
        ids = check_ids if check_ids is not None else re.findall(r"- \[ \] ((?:DEV|FIX)-\d{3})", text)
        if tamper:
            p.write_text(text.replace("작성", "작성함(변조)"), encoding="utf-8")
        files = [x.strip() for x in (evidence or file).split(",")]
        prose = "dev done\n" + (GATE if summary is None else summary)
        return work(prose, done=[{"id": i, "files": files, "note": "작성"} for i in ids])
    return fn


def developer_each(files_by_id):
    """A single developer finishing several items, each with its own file (v4 §9: files ∩ that item's 대상)."""
    def fn(kw):
        for f in files_by_id.values():
            (Path(kw["cwd"]) / f).write_text("print(1)\n", encoding="utf-8")
        return work("dev done\n" + GATE, done=[{"id": i, "files": [f], "note": "작성"} for i, f in files_by_id.items()])
    return fn


def work(text, done=None):
    """v4 §9 fixture bridge: an old prose answer -> the work-role JSON. `작성게이트:` / `방향 질문:` lines become
    `write_gate` / `question` (the raw value is kept, so "없음" still exercises the engine's no-question rule)."""
    import re
    gate = q = ""
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]?\s*\**\s*(작성\s*게이트|방향\s*질문)\s*\**\s*[:：]\s*\**\s*(.*)$", line)
        if m:
            if m.group(1).startswith("작성"):
                gate = m.group(2).strip()
            else:
                q = m.group(2).strip()
    return Result(text=text, structured={"done": list(done or []), "summary": text, "write_gate": gate,
                                         "question": q})


def qa_scen(code="PASS EVIDENCE", with_failure_flow=True, n=1):
    # ids with "_" on purpose (regression: runners must not parse ids from file names)
    sc = [{"id": f"0{k}_main_flow", "title": "인사말", "kind": "normal", "filename": f"s{k}",
           "code": code if k == 1 else "PASS EVIDENCE"} for k in range(1, n + 1)]
    if with_failure_flow:
        sc += [{"id": f"1{k}_edge_flow", "title": "빈 입력", "kind": "failure", "filename": f"f{k}",
                "code": "PASS EVIDENCE"} for k in range(1, n + 1)]
    return lambda kw: Result(text="qa", structured={"scenarios": sc})


def wiki_writer():
    def fn(kw):
        p = Path(kw["cwd"]) / "docs" / "wiki" / "log.md"
        p.write_text(p.read_text(encoding="utf-8") + "- run 기록\n", encoding="utf-8")
        return Result(text="wiki", structured={"summary": "log.md 1줄", "question": ""})
    return fn


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


# ================= v6 fixtures (10-02) — builder · evaluator answers in BUILD_SCHEMA / EVAL_SCHEMA =================
NO_QA = "## 흐름\n- QA: 없음\n- Wiki: 없음\n"
NO_WIKI = "## 흐름\n- Wiki: 없음\n"


def _session(kw, sid, cost):
    """Like the real CLI: the init line names the session first (on_init), the result line repeats it with the
    session's cumulative cost."""
    if kw.get("on_init"):
        kw["on_init"](kw.get("resume") or sid)
    return kw.get("resume") or sid


def b_work(files=None, done=None, remaining="없음", question="", gate=GATE, sid="SB", cost=0.1, extra=None):
    """The builder's v6 answer. files: {path: body} written now · done: item ids reported done with all `files`
    (None = every open item of the TODO) · extra: callable(kw) run before answering (e.g. write a stray file)."""
    files = {"hello.py": "print(1)\n"} if files is None else files

    def fn(kw):
        import re
        sess = _session(kw, sid, cost)
        cwd = Path(kw["cwd"])
        for f, body in files.items():
            (cwd / f).parent.mkdir(parents=True, exist_ok=True)
            (cwd / f).write_text(body, encoding="utf-8")
        if extra:
            extra(kw)
        ids = done
        if ids is None:
            plans = list(cwd.glob("docs/plan/*.md"))  # the run's own TODO = the newest (a campaign repo keeps older ones)
            plan = max(plans, key=lambda p: p.stat().st_mtime_ns) if plans else None
            ids = re.findall(r"- \[ \] ((?:DEV|FIX)-\d{3})", plan.read_text(encoding="utf-8")) if plan else []
        return Result(text="built", session_id=sess, cost=cost, structured={
            "done": [{"id": i, "files": list(files), "note": "작성"} for i in ids], "summary": "작업 요약",
            "remaining": remaining, "write_gate": gate, "question": question})
    return fn


def b_wiki(sid="SB", cost=0.2, write=True, stray=None):
    def fn(kw):
        sess = _session(kw, sid, cost)
        cwd = Path(kw["cwd"])
        if write:
            p = cwd / "docs" / "wiki" / "log.md"
            p.write_text(p.read_text(encoding="utf-8") + "- 묶음 기록\n", encoding="utf-8")
        if stray:
            (cwd / stray).write_text("x\n", encoding="utf-8")
        return Result(text="wiki", session_id=sess, cost=cost, structured={
            "done": [], "summary": "log.md 1줄", "remaining": "없음", "write_gate": "해당없음 — 문서", "question": ""})
    return fn


def scenario(sid="S1", items=("DEV-001",), kind="normal", code="PASS EVIDENCE"):
    return {"id": sid, "title": sid, "kind": kind, "item_ids": list(items), "filename": sid, "code": code}


def scen_set(code="PASS EVIDENCE", items=("DEV-001",), n_fail=1):
    return ([scenario(f"N_{i}", (i,), "normal", code) for i in items]
            + [scenario(f"F_{k}", (items[0],), "failure", "PASS EVIDENCE") for k in range(1, n_fail + 1)])


def e_ans(verdict="RUN", scenarios=None, issues=None, question="", reason="", sid="SE", cost=0.3, to_builder="",
          want_rebuttal=False):
    """Any evaluator-side answer (main · second view · rebuttal) in the one EVAL_SCHEMA shape (10-03 정렬안 §3).
    issues: [(severity, problem)] · reason: defaults to the verdict."""
    def fn(kw):
        sess = _session(kw, sid, cost) if sid else None
        iss = [{"severity": sv, "where": "hello.py", "problem": p, "fix": "고침"} for sv, p in (issues or [])]
        return Result(text="eval", session_id=sess, cost=cost, structured={
            "verdict": verdict, "to_builder": to_builder, "issues": iss, "reason": reason or verdict,
            "checked": ["A", "B", "G"], "scenarios": list(scenarios if scenarios is not None else []),
            "want_rebuttal": want_rebuttal, "question": question})
    return fn


def devil(verdict="READY", issues=None, reason=""):
    """The rebuttal is always a new session — a fresh id every time (like the real CLI)."""
    ids = itertools.count(1)
    return lambda kw: e_ans(verdict, issues=issues, reason=reason, sid=f"SD{next(ids)}", cost=0.2)(kw)


def cut(sid="SB", write=None):
    """A call the watcher cut: the init line came (sid known), no result line. write: {path: body} done before."""
    def fn(kw):
        sess = _session(kw, sid, 0)
        for f, body in (write or {}).items():
            (Path(kw["cwd"]) / f).write_text(body, encoding="utf-8")
        return Result(text="호출 끊음 — 무응답 300초", is_error=True, timed_out="무응답 300초", session_id=sess,
                      has_result=False)
    return fn


def gone():
    """`--resume` of a session the CLI no longer has (S-5)."""
    return lambda kw: Result(text="No conversation found with session ID", is_error=True, no_session=True)
