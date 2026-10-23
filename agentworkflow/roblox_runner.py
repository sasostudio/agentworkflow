"""QA runner `roblox`: the ENGINE (not an AI role) runs QA scenarios in Roblox Studio through Studio MCP.

Scenario = Luau code (from the QA role's JSON). Contract:
    -- context: Server            (first line; Client also allowed; default Server)
    ...code...
    return { status = "PASSED" | "FAILED" | "BLOCKED", note = "무엇을 확인했나" }
The runner wraps it in pcall; a Luau error or a non-table return = FAILED.

Per scenario: play start -> wait for the datamodel -> [reset_code] -> run -> screenshot (.jpg) + console log ->
[reset_code] -> play stop (ALWAYS, also on failure/timeout, so the next scenario starts clean) -> confirm back in Edit.
v6 N14: a play's instances vanish at stop, but what a scenario saved to a DataStore stays (09-26 #13: coins =
5,000,000 left behind broke the next QA). `qa.roblox.reset_code` (Server Luau) runs inside every play before the
scenario and again after it — the second one also when the scenario failed.
Preconditions (never PASS when they fail -> every scenario BLOCKED):
  one Studio matching qa.roblox.studio_name · Studio in Edit mode (a running play = someone is using it) ·
  files == Studio sources for the run's changed scripts (rojo sourcemap; mismatch = Rojo not connected / stale).
The runner never starts or restarts `rojo serve` (restarting forces the user to click Connect again — PoC §3).
"""

import contextlib
import json
import re
import time
from pathlib import Path

from . import gitutil, procutil, studio_capture
from .qa import enforce_evidence
from .studio_mcp import StudioClient, StudioError, ToolFailed

SCRIPT_CLASSES = {"Script", "LocalScript", "ModuleScript"}
_CONTEXT = re.compile(r"^\s*--\s*context\s*:\s*(Server|Client)\b", re.I | re.M)

WRAP = """local __aw_ok, __aw_res = pcall(function()
{code}
end)
if not __aw_ok then return {{status = "FAILED", note = "Luau 오류: " .. tostring(__aw_res)}} end
if type(__aw_res) ~= "table" then
    return {{status = "FAILED", note = "시나리오가 {{status, note}} 테이블을 반환하지 않음"}}
end
return {{status = tostring(__aw_res.status), note = tostring(__aw_res.note or "")}}
"""

# v4 §10 (#1) a step before the last: nil (or a PASSED table) = go on to the next step, FAILED/BLOCKED = stop there
STEP_WRAP = """local __aw_ok, __aw_res = pcall(function()
{code}
end)
if not __aw_ok then return {{status = "FAILED", note = "Luau 오류: " .. tostring(__aw_res)}} end
if __aw_res == nil then return {{status = "CONTINUE", note = ""}} end
if type(__aw_res) ~= "table" then
    return {{status = "FAILED", note = "단계가 nil 도 {{status, note}} 테이블도 아닌 값을 반환함"}}
end
return {{status = tostring(__aw_res.status), note = tostring(__aw_res.note or "")}}
"""

UNIT_WRAP = """local __aw_ok, __aw_res = pcall(function()
{code}
end)
if not __aw_ok then return {{status = "FAILED", note = "단위 테스트 오류: " .. tostring(__aw_res)}} end
if type(__aw_res) ~= "table" or type(__aw_res.run) ~= "number" then
    return {{status = "FAILED", note = "단위 테스트가 {{run, passed, failed}} 를 반환하지 않음"}}
end
local passed = __aw_res.run > 0 and (__aw_res.failed or 0) == 0
return {{status = passed and "PASSED" or "FAILED",
        note = string.format("run=%d passed=%d failed=%d", __aw_res.run, __aw_res.passed or 0, __aw_res.failed or 0)}}
"""


def context_of(code):
    m = _CONTEXT.search(code.split("\n", 3)[0] if code else "")
    return (m.group(1).capitalize() if m else "Server")


def _lua_str(s):
    return json.dumps(s, ensure_ascii=False)  # JSON string escapes are valid Luau escapes for these texts


def _as_list(value, n):
    """execute_luau serializes a Luau array as a JSON OBJECT with keys "1".."n" (measured 09-24:
    `return {"a","b"}` -> {"1":"a","2":"b"}). Missing keys (nil) -> False."""
    if isinstance(value, list):
        return (value + [False] * n)[:n]
    if isinstance(value, dict):
        return [value.get(str(i + 1), False) for i in range(n)]
    return [False] * n


def _norm(text):
    return (text or "").replace("\r\n", "\n").rstrip("\n")


# ---------------- preconditions ----------------

def pick_studio(client, name):
    txt = client.text("list_roblox_studios")
    studios = json.loads(txt).get("studios") or []
    if name:
        studios = [s for s in studios if name in (s.get("name") or "")]
    if len(studios) != 1:
        raise Blocked(f"대상 Studio를 1개로 특정 못 함(후보 {len(studios)}개, 이름 조건 `{name}`) — 플레이스를 열었나")
    return studios[0]["id"]


def mode(client, sid):
    txt = client.text("get_studio_state", {"studio_id": sid})
    cur = re.search(r"Current Studio Mode:\s*(\w+)", txt)
    avail = re.search(r"Available DataModels:\s*(.+)", txt)
    return (cur.group(1) if cur else "?"), (avail.group(1) if avail else "")


def sourcemap_scripts(command, target):
    r = procutil.run(command, cwd=str(target), shell=True, capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=120)
    if r.returncode != 0:
        raise Blocked(f"rojo sourcemap 실패(exit {r.returncode}): {(r.stderr or r.stdout)[-300:]}")
    tree = json.loads(r.stdout)
    out = {}

    def walk(node, path):
        for ch in node.get("children") or []:
            p = path + [ch["name"]]
            if ch.get("className") in SCRIPT_CLASSES:
                for fp in ch.get("filePaths") or []:
                    if fp.endswith((".luau", ".lua")):
                        out[fp.replace("\\", "/")] = p
            walk(ch, p)
    walk(tree, [])
    return out


def check_sync(client, sid, target, command, wait=15, limit=60):
    """Changed script files (or all mapped scripts when nothing changed) must equal Studio's Source.
    Mismatches are re-checked for up to `wait` seconds: files the developer just wrote may still be on their way
    through Rojo (gap found in the 09-25 simulation — an early check falsely BLOCKED)."""
    mapping = sourcemap_scripts(command, target)
    dirty = [p for p in gitutil.dirty_paths(target) if p in mapping] if gitutil.is_repo(target) else []
    files = dirty or sorted(mapping)[:limit]
    if not files:
        raise Blocked("rojo sourcemap에 스크립트가 없음 — project 파일 확인")
    todo, t0 = files, time.time()
    while True:
        todo = _mismatched(client, sid, target, mapping, todo)
        if not todo or time.time() - t0 >= wait:
            break
        time.sleep(2)
    if todo:
        raise Blocked(f"Studio에 최신 파일이 반영 안 됨({wait}s 재확인 후 · Rojo Connect 필요?): " + ", ".join(todo[:8]))
    return len(files)


# 09-26 (F-10 run003 · §23-12 #12): returning whole sources made Studio answer "[Complex Table]" (not JSON) once a
# chunk got big, and the runner crashed. Studio now returns only `length:hash` of the normalized source (= _sig).
_SIG_LUA = ('local function sig(s) s = string.gsub(s, "\\r\\n", "\\n") s = string.gsub(s, "\\n+$", "") '
            'local h = 0 for i = 1, #s do h = (h * 31 + string.byte(s, i)) % 1000000007 end '
            'return #s .. ":" .. h end ')


def _sig(text):
    b = _norm(text).encode("utf-8")
    h = 0
    for ch in b:
        h = (h * 31 + ch) % 1000000007
    return f"{len(b)}:{h}"


def _mismatched(client, sid, target, mapping, files):
    mismatched = []
    for i in range(0, len(files), 10):
        chunk = files[i:i + 10]
        paths = ", ".join("{" + ", ".join(_lua_str(n) for n in mapping[f]) + "}" for f in chunk)
        code = (_SIG_LUA + "local out = {} for _, p in ipairs({" + paths + "}) do local o = game "
                "for _, n in ipairs(p) do o = o and o:FindFirstChild(n) end "
                "table.insert(out, o and sig(o.Source) or false) end return out")
        raw = client.text("execute_luau", {"studio_id": sid, "datamodel_type": "Edit", "code": code})
        try:
            got = _as_list(json.loads(raw), len(chunk))
        except json.JSONDecodeError:
            raise Blocked(f"Studio 동기화 확인 응답이 JSON이 아님: {raw[:120]!r}")
        for f, sig in zip(chunk, got):
            if sig is False or sig != _sig((Path(target) / f).read_text(encoding="utf-8")):
                mismatched.append(f)
    return mismatched


class Blocked(Exception):
    pass


# ---------------- play control ----------------

def _wait_mode(client, sid, want, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        cur, avail = mode(client, sid)
        if want == "Edit" and cur == "Edit":
            return True
        if want != "Edit" and want in avail:
            return True
        time.sleep(1)
    return False


def _stop_play(client, sid, timeout):
    try:
        client.text("start_stop_play", {"studio_id": sid, "is_start": False})
    except (ToolFailed, StudioError):
        pass
    return _wait_mode(client, sid, "Edit", timeout)


def steps_of(f, raw):
    """-> [(context, wrapped code)] — one entry for a plain scenario, one per step for a v4 multi-context one."""
    steps = f.get("steps")
    if not steps:
        return [(context_of(raw), WRAP.format(code=raw))]
    last = len(steps) - 1
    return [(str(st.get("context") or "Server").capitalize(),
             (WRAP if i == last else STEP_WRAP).format(code=st.get("code") or "")) for i, st in enumerate(steps)]


def _reset(client, sid, code, when):
    """v6 N14: the configured clean-up inside the running play. -> "" or a note for the result."""
    if not code:
        return ""
    try:
        client.text("execute_luau", {"studio_id": sid, "datamodel_type": "Server", "code": code}, timeout=60)
        return ""
    except (ToolFailed, StudioError) as e:
        return f" ⚠️정리 코드 실패({when}): {str(e)[-200:]}"


def run_one(client, sid, steps, timeout, ready_timeout, edir, sid_name, log, n_frames=1, interval=0.0,
            reset_code=None, capture=True):
    """steps: [(context, code)] run in order inside ONE play (v4 §10 #1). Returns dict(status, note, evidence, env) —
    env=True only when nothing of the scenario ran (the play or its datamodel never came up). Play is always stopped
    before returning. n_frames/interval: the captures after the last step (studio_capture.frames)."""
    evidence, note = [], ""
    # mutated in place so `finally` can annotate it
    res = {"status": "BLOCKED", "note": "", "evidence": evidence, "env": True}
    server_up = False
    try:
        try:
            client.text("start_stop_play", {"studio_id": sid, "is_start": True})
        except (ToolFailed, StudioError) as e:
            res["note"] = f"플레이 시작 실패: {e}"
            return res
        deadline = time.time() + timeout
        status = "CONTINUE"
        for i, (ctx, code) in enumerate(steps, 1):
            tag = f"[단계 {i}/{len(steps)} {ctx}] " if len(steps) > 1 else ""
            if not _wait_mode(client, sid, ctx, ready_timeout):
                if i == 1:
                    res["note"] = f"플레이 시작 후 {ctx} 준비 안 됨({ready_timeout}s)"
                    return res
                status, note = "FAILED", f"{tag}{ctx} 준비 안 됨({ready_timeout}s)"
                break
            if not server_up and reset_code:  # before the scenario: the Server datamodel must be up
                if ctx == "Server" or _wait_mode(client, sid, "Server", ready_timeout):
                    server_up = True
                    note += _reset(client, sid, reset_code, "전")
            res["env"] = False  # scenario code runs from here on — what happens now is not the environment's
            try:
                out = json.loads(client.text("execute_luau", {"studio_id": sid, "datamodel_type": ctx, "code": code},
                                             timeout=max(5, deadline - time.time())))
                status, note = out.get("status"), tag + str(out.get("note", ""))
            except ToolFailed as e:
                status, note = "FAILED", f"{tag}실행 오류: {str(e)[-400:]}"
            except StudioError as e:
                status, note = "FAILED", f"{tag}타임아웃·통신 오류: {e}"
            except json.JSONDecodeError:
                status, note = "FAILED", f"{tag}반환값을 JSON으로 읽지 못함"
            if i == len(steps) or status not in ("CONTINUE", "PASSED"):
                break
        if status == "CONTINUE":
            status, note = "FAILED", "마지막 단계가 {status, note} 테이블을 돌려주지 않음"
        # v5 (10-01): a scenario may ask for N frames `interval` s apart (what only shows over time — a cutscene);
        # qa.add_sheets puts them side by side afterwards (every runner). 1 frame = v4's single 01.jpg.
        if capture:  # 10-03: a scenario that declared `no_capture` (the client is gone — Kick·leave) is not captured
            names, cap_note = studio_capture.frames(client, sid, edir, sid_name, n_frames, interval)
            evidence.extend(names)
            note += cap_note
        try:
            log.write_text(client.text("get_console_output", {"studio_id": sid}), encoding="utf-8")
        except (ToolFailed, StudioError) as e:
            log.write_text(f"(콘솔 읽기 실패: {e})", encoding="utf-8")
        res.update(status=status, note=note)
        return res
    finally:
        if server_up:  # after the scenario — whatever it returned
            res["note"] += _reset(client, sid, reset_code, "후")
        if not _stop_play(client, sid, ready_timeout):
            res["note"] += " ⚠️플레이 정지 확인 실패"
            res["stop_failed"] = True


def _write_partial(qa_dir, results):
    """#7 (09-29): every finished scenario is on disk at once — a stop mid-run (PC off at 36/75) keeps them."""
    p = qa_dir / "partial_results.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def run(cfg, target, qa_dir, files, client_factory=None, prior=None):
    """prior: {id: result} of an interrupted attempt of the same scenarios for the same code (the engine checked
    that) — those ran already and are taken as they are; only the rest is played."""
    prior = prior or {}
    qa = cfg["qa"]
    rb = qa["roblox"]
    evidence = qa_dir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    timeout, ready = qa["scenario_timeout_sec"], rb["play_ready_timeout_sec"]
    plan = list(files)
    if rb.get("unit_test_code"):
        plan.insert(0, {"id": "UNIT", "title": "단위 테스트(engine.json qa.roblox.unit_test_code)", "kind": "normal",
                        "evidence_dir": "UNIT", "_unit": True})

    def blocked_all(msg):  # nothing ran: the environment's (v4 §10 ENV -> RETRY)
        return enforce_evidence([{"id": f["id"], "title": f["title"], "kind": f["kind"], "status": "BLOCKED",
                                  "note": msg, "evidence": [], "env": True} for f in plan], evidence)

    factory = client_factory or (lambda: StudioClient(rb["mcp_command"], timeout=max(60, timeout)))
    notes = []
    # v5: builders may drive the same Studio (studio_shot) — take turns. Tests (client_factory) use no lock file.
    hold = (studio_capture.studio_lock(f"QA {Path(qa_dir).name}", rb.get("lock_wait_sec", 900))
            if client_factory is None else contextlib.nullcontext())
    try:
        with hold, factory() as client:
            sid = pick_studio(client, rb.get("studio_name"))
            cur, _ = mode(client, sid)
            if cur != "Edit":
                return blocked_all(f"Studio가 Edit 모드가 아님({cur}) — 누가 플레이 중이면 끝난 뒤 다시")
            if rb.get("sync_check", True):
                n = check_sync(client, sid, target, rb["sourcemap_command"], rb.get("sync_wait_sec", 15))
                notes.append(f"동기화 확인 {n}개 파일")
            results, stuck = [], None
            for f in plan:
                if f["id"] in prior:  # ran before the interruption — same scenario, same code
                    results.append(prior[f["id"]])
                    continue
                if stuck:  # a play that could not be stopped would contaminate every later scenario
                    results.append({"id": f["id"], "title": f["title"], "kind": f["kind"], "status": "BLOCKED",
                                    "note": f"앞 시나리오({stuck}) 뒤 플레이 정지 실패 — 실행 안 함", "evidence": [],
                                    "env": True})
                    continue
                if f.get("_unit"):
                    seq = [("Server", UNIT_WRAP.format(code=rb["unit_test_code"]))]
                else:
                    seq = steps_of(f, Path(f["path"]).read_text(encoding="utf-8"))
                r = run_one(client, sid, seq, timeout, ready, evidence / f["evidence_dir"], f["evidence_dir"],
                            qa_dir / f"log_{f['evidence_dir']}.txt", f.get("frames") or 1, f.get("interval") or 0,
                            reset_code=rb.get("reset_code"), capture=not f.get("no_capture"))
                if r.pop("stop_failed", False):
                    stuck = f["id"]
                results.append({"id": f["id"], "title": f["title"], "kind": f["kind"], **r})
                _write_partial(qa_dir, results)
    except (Blocked, studio_capture.Busy) as e:
        return blocked_all(str(e))
    except (StudioError, ToolFailed) as e:
        return blocked_all(f"Studio MCP 연결·호출 실패: {e}")
    (qa_dir / "runner.log").write_text("\n".join(notes), encoding="utf-8")
    return enforce_evidence(results, evidence, plan)
