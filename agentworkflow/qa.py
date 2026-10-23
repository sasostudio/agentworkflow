"""Independent QA runners + verdict aggregation (design §8).

Verdict per scenario: PASSED | FAILED | BLOCKED.  Overall = FAILED > BLOCKED > PASSED.
A PASSED scenario with no evidence file on disk is demoted to BLOCKED by code.
"""

import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from . import procutil
from .safety import ENGINE_ROOT

RUNNERS_DIR = ENGINE_ROOT / "runners"
EXT = {"web": ".mjs", "godot": ".gd", "roblox": ".luau"}
ORDER = {"FAILED": 3, "BLOCKED": 2, "PASSED": 1}


def aggregate(results):
    if not results:
        return "BLOCKED"
    return max((r["status"] for r in results), key=lambda s: ORDER.get(s, 3))


def enforce_evidence(results, evidence_root, files=()):
    """A PASSED with no capture on disk is demoted to BLOCKED — unless the scenario declared `no_capture` (measured:
    Kick ends the client, nothing to capture; its data check passed twice and was demoted twice). Then it
    stays PASSED and the note says so as a fact; whether the reason holds is the evaluator's to judge."""
    skip = {f["id"]: f["no_capture"] for f in files or () if f.get("no_capture")}
    for r in results:
        if r.get("status") not in ORDER:
            r["note"] = f"알 수 없는 상태 {r.get('status')!r} → FAILED. " + (r.get("note") or "")
            r["status"] = "FAILED"
        if r["status"] != "PASSED":
            continue
        ev = [e for e in (r.get("evidence") or []) if _exists(e, evidence_root, r.get("id"))]
        if not ev and r.get("id") in skip:
            r["note"] = f"캡처 생략(시나리오 선언: {skip[r['id']]}). " + (r.get("note") or "")
        elif not ev:
            r["status"] = "BLOCKED"
            r["note"] = "증거(캡처) 없는 PASSED → BLOCKED 강등. " + (r.get("note") or "")
    return results


def safe_id(sid):
    """Folder/file-safe form of a scenario id. Runners get ids from scenarios.json, never from file names."""
    return re.sub(r"[^\w\-]+", "_", str(sid or ""))[:60] or "S"


def _exists(e, root, sid):
    p = Path(e)
    if p.is_absolute():
        return p.is_file()
    return (Path(root) / safe_id(sid) / e).is_file() or (Path(root) / e).is_file()


def _safe_name(index, sid, ext):
    return f"{index:02d}_{safe_id(sid)}{ext}"


def write_scenarios(scenarios, qa_dir, runner):
    sdir = qa_dir / "scenarios"
    sdir.mkdir(parents=True, exist_ok=True)
    out = []
    for n, s in enumerate(scenarios, 1):
        ext = EXT.get(runner, Path(s.get("filename", "")).suffix or ".txt")
        path = sdir / _safe_name(n, s["id"], ext)
        steps = s.get("steps") or None
        body = s["code"] if not steps else "\n".join(
            f"-- ===== step {i}/{len(steps)} · context: {st.get('context')} =====\n{st.get('code', '')}"
            for i, st in enumerate(steps, 1))
        path.write_text(body, encoding="utf-8")
        cap = {k: s[k] for k in ("frames", "interval", "no_capture") if s.get(k)}  # v5 frames · 10-03 no_capture
        # v6 N13: item_ids travel with the scenario (the next evaluation carries them over from scenarios.json)
        out.append({**{k: s[k] for k in ("id", "title", "kind")}, "item_ids": list(s.get("item_ids") or []),
                    "path": str(path),
                    "evidence_dir": safe_id(s["id"]), **({"steps": steps} if steps else {}), **cap})
    (qa_dir / "scenarios.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ---------------- managed environment ----------------

class ManagedServer:
    def __init__(self, start_cmd, ready_url, cwd, timeout, log_path):
        self.start_cmd, self.ready_url, self.cwd, self.timeout = start_cmd, ready_url, cwd, timeout
        self.log_path = log_path
        self.proc = None
        self.log = None

    def __enter__(self):
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(self.start_cmd, cwd=str(self.cwd), shell=True, stdout=self.log,
                                     stderr=subprocess.STDOUT)
        t0 = time.time()
        while time.time() - t0 < self.timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"managed 서버가 바로 종료됨 (exit {self.proc.returncode})")
            try:
                with urllib.request.urlopen(self.ready_url, timeout=3):
                    return self
            except Exception:
                time.sleep(1.5)
        self.__exit__(None, None, None)
        raise RuntimeError(f"managed 서버 준비 시간 초과: {self.ready_url}")

    def __exit__(self, *a):
        # stop ONLY the process tree this engine started
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        if self.proc:
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        if self.log:
            self.log.close()


# ---------------- runners ----------------

def run_web(cfg, target, qa_dir, files):
    qa = cfg["qa"]
    evidence = qa_dir / "evidence"
    results_file = qa_dir / "results.json"
    base_url = qa.get("base_url") or qa["managed"].get("ready_url")
    cmd = ["node", str(RUNNERS_DIR / "web_runner.mjs"), "--manifest", str(qa_dir / "scenarios.json"),
           "--evidence", str(evidence), "--results", str(results_file), "--base-url", base_url,
           "--timeout", str(qa["scenario_timeout_sec"])]

    def _go():
        r = procutil.run(cmd, cwd=str(RUNNERS_DIR), capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=qa["scenario_timeout_sec"] * max(1, len(files)) + 120)
        (qa_dir / "runner.log").write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
        return r

    if qa["environment"] == "managed":
        with ManagedServer(qa["managed"]["start"], qa["managed"]["ready_url"], target,
                           qa["managed"]["ready_timeout_sec"], qa_dir / "server.log"):
            _go()
    else:
        _go()
    return _read_results(results_file, files, evidence)


def run_godot(cfg, target, qa_dir, files):
    qa = cfg["qa"]
    evidence = qa_dir / "evidence"
    results = []
    for f in files:
        edir = evidence / f["evidence_dir"]
        edir.mkdir(parents=True, exist_ok=True)
        rfile = qa_dir / f"result_{f['evidence_dir']}.json"
        env = dict(os.environ, AW_EVIDENCE_DIR=str(edir), AW_RESULT_FILE=str(rfile))
        cmd = [qa["godot_exe"], "--path", str(target), "-s", f["path"]]
        try:
            r = procutil.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=qa["scenario_timeout_sec"])
            log = (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            log = f"타임아웃 {qa['scenario_timeout_sec']}s"
        (qa_dir / f"log_{f['evidence_dir']}.txt").write_text(log, encoding="utf-8")
        if rfile.is_file():
            try:
                j = json.loads(rfile.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                j = {"status": "FAILED", "note": "결과 JSON 파싱 실패"}
        else:
            j = {"status": "FAILED", "note": "결과 파일 없음(스크립트 오류·타임아웃) — 로그: " + log[-600:]}
        results.append({"id": f["id"], "title": f["title"], "kind": f["kind"], "status": j.get("status"),
                        "note": j.get("note", ""), "evidence": j.get("evidence", [])})
    return enforce_evidence(results, evidence, files)


def run_command(cfg, target, qa_dir, files):
    qa = cfg["qa"]
    evidence = qa_dir / "evidence"
    evidence.mkdir(exist_ok=True)
    results_file = qa_dir / "results.json"
    cmd = qa["command"].format(scenarios=qa_dir / "scenarios", manifest=qa_dir / "scenarios.json",
                               evidence=evidence, results=results_file, target=target,
                               base_url=qa.get("base_url") or "")
    r = procutil.run(cmd, cwd=str(target), shell=True, capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=qa["scenario_timeout_sec"] * max(1, len(files)) + 60)
    (qa_dir / "runner.log").write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
    return _read_results(results_file, files, evidence)


def _read_results(results_file, files, evidence):
    if not results_file.is_file():
        return [{"id": f["id"], "title": f["title"], "kind": f["kind"], "status": "BLOCKED", "env": True,
                 "note": "러너가 results.json 을 만들지 않음 — runner.log 확인", "evidence": []} for f in files]
    got = {r.get("id"): r for r in json.loads(results_file.read_text(encoding="utf-8"))}
    out = []
    for f in files:
        # v6 N10: a scenario the runner never reported is the environment's (nothing of it is known to have run)
        r = got.get(f["id"]) or {"status": "BLOCKED", "note": "결과 누락", "env": True}
        out.append({"id": f["id"], "title": f["title"], "kind": f["kind"], "status": r.get("status"),
                    "note": r.get("note", ""), "evidence": r.get("evidence", []),
                    **({"env": True} if r.get("env") else {})})
    return enforce_evidence(out, evidence, files)


def run_roblox(cfg, target, qa_dir, files, prior=None):
    from . import roblox_runner  # lazy: roblox_runner imports enforce_evidence from here
    return roblox_runner.run(cfg, target, qa_dir, files, prior=prior)


RUN = {"web": run_web, "godot": run_godot, "command": run_command, "roblox": run_roblox}

# v5: the engine is for every kind of work — what differs per target is declared HERE, per runner, and
# the engine core only asks. If a target lacks something, look here first.
#   multi_shot: how a scenario captures what only shows over time —
#               "frames" = scenario fields `frames`/`interval`, the runner captures (roblox) ·
#               "scenario" = the scenario calls its own capture several times (web `shot()` · godot `save_png`) ·
#               None = no images (command: whatever its own runner writes)
#   dev_shot:   the module that lets the BUILDER look at the running result while it builds (None = not offered for
#               this target yet — the builder works from code and QA still captures)
CAPS = {
    "none": {"multi_shot": None, "dev_shot": None},
    "command": {"multi_shot": None, "dev_shot": None},
    "web": {"multi_shot": "scenario", "dev_shot": None},
    "godot": {"multi_shot": "scenario", "dev_shot": None},
    "roblox": {"multi_shot": "frames", "dev_shot": "studio_shot"},
}


def caps(runner):
    return CAPS.get(runner, CAPS["none"])


# ---------------- evidence sheet (every runner) ----------------
IMAGE_EXT = (".png", ".jpg", ".jpeg")
SHEET = "sheet.jpg"


def make_sheet(edir, names, width=480):
    """v5: several captures of one scenario side by side in ONE image (3 per row, numbered in capture order), so the
    visual review opens one picture (measured 10-01: one sheet ≈ 1.7k tokens) instead of N files.
    -> the sheet's name, or None when there is nothing to put together (<2 images) or Pillow is missing."""
    names = [n for n in names if n.lower().endswith(IMAGE_EXT) and n != SHEET and (Path(edir) / n).is_file()]
    if len(names) < 2:
        return None
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    ims = []
    for nm in names:
        try:  # a broken capture must not take the QA run down with it — it is just left out of the sheet
            with Image.open(Path(edir) / nm) as im:
                im = im.convert("RGB")
                ims.append(im.resize((width, max(1, round(im.height * width / im.width)))))
        except (OSError, ValueError):  # PIL.UnidentifiedImageError is an OSError
            continue
    if len(ims) < 2:
        return None
    cols = min(3, len(ims))
    rows = (len(ims) + cols - 1) // cols
    cell_h = max(im.height for im in ims)
    out = Image.new("RGB", (cols * width, rows * cell_h), "black")
    draw = ImageDraw.Draw(out)
    for i, im in enumerate(ims):
        x, y = (i % cols) * width, (i // cols) * cell_h
        out.paste(im, (x, y))
        draw.rectangle([x, y, x + 34, y + 20], fill="black")
        draw.text((x + 6, y + 4), f"{i + 1:02d}", fill="yellow")
    out.save(Path(edir) / SHEET, quality=85)
    return SHEET


def add_sheets(results, evidence_root):
    """Every scenario with 2+ captures gets a sheet in its evidence (whatever runner made them)."""
    for r in results:
        ev = list(r.get("evidence") or [])
        if SHEET in ev:
            continue
        edir = Path(evidence_root) / safe_id(r.get("id"))
        sh = make_sheet(edir, ev)
        if sh:
            r["evidence"] = ev + [sh]
        elif sum(1 for n in ev if n.lower().endswith(IMAGE_EXT)) >= 2:
            r["note"] = (r.get("note") or "") + " (시트 못 만듦 — Pillow 없음 또는 파일 위치 다름, 캡처 파일은 있음)"
    return results


def run_qa(cfg, target, qa_dir, scenarios, prior=None):
    """prior (roblox only): {"dir": the interrupted attempt folder, "results": {id: result}} — its evidence is copied
    in and those scenarios are not played again (#7, 09-29)."""
    runner = cfg["qa"]["runner"]
    files = write_scenarios(scenarios, qa_dir, runner)
    if prior and prior.get("results") and runner == "roblox":
        import shutil
        for r in prior["results"].values():
            src = Path(prior["dir"]) / "evidence" / safe_id(r["id"])
            if src.is_dir():
                shutil.copytree(src, qa_dir / "evidence" / safe_id(r["id"]), dirs_exist_ok=True)
        results = RUN[runner](cfg, target, qa_dir, files, prior=prior["results"])
    else:
        results = RUN[runner](cfg, target, qa_dir, files)
    add_sheets(results, qa_dir / "evidence")  # v5: one sheet per multi-capture scenario, for every runner
    verdict = aggregate(results)
    (qa_dir / "results_final.json").write_text(json.dumps({"verdict": verdict, "results": results},
                                                          ensure_ascii=False, indent=2), encoding="utf-8")
    return verdict, results


def failures_text(results):
    return "\n".join(f"- {r['id']} [{r['kind']}] {r['title']} → {r['status']}: {r.get('note', '')}"
                      for r in results if r["status"] != "PASSED")


# v4 §10 (#8): what can be known about a scenario BEFORE it runs. A problem here is the scenario's own (the QA AI fixes
# it), never a code defect. Linear regex only (§23-12 #11): a number inside `wait(` … `)`.
_WAIT = re.compile(r"\b(?:task\.)?wait\s*\(\s*(\d+(?:\.\d+)?)\s*\)")
_RETURN = re.compile(r"\breturn\b")


class SyntaxToolError(Exception):
    """The configured syntax command cannot even parse a valid chunk — a config problem, not the scenarios'."""


def precheck(scenarios, cfg, target, max_n=None):
    """-> problems (empty = run them). Raises SyntaxToolError when qa.syntax_command itself does not work (measured
    09-27: under cmd.exe a forward-slash relative program path does not run — every scenario would have looked broken)."""
    q, v = cfg["qa"], []
    if q.get("syntax_command"):
        ok, out = _parses(q["syntax_command"], "return nil", target)
        if not ok:
            raise SyntaxToolError(f"qa.syntax_command 가 올바른 코드(`return nil`)도 통과시키지 못함: {out[:200]}")
    if max_n and len(scenarios) > max_n:
        v.append(f"시나리오 {len(scenarios)}개 — 최대 {max_n}개")
    ids = [str(s.get("id")) for s in scenarios]
    v += [f"시나리오 id 중복: {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    for s in scenarios:
        sid, steps = s.get("id"), s.get("steps") or []
        if steps and q["runner"] != "roblox":
            v.append(f"{sid} `steps`는 roblox 러너만 — `code` 하나로 쓴다")
        codes = [str(st.get("code") or "") for st in steps] if steps else [str(s.get("code") or "")]
        if not all(c.strip() for c in codes):
            v.append(f"{sid} 비어 있는 코드" + (" 단계" if steps else ""))
            continue
        if q["runner"] == "roblox":
            waits = sum(float(x) for c in codes for x in _WAIT.findall(c))
            if waits > q["roblox"]["max_wait_sec"]:
                v.append(f"{sid} `task.wait` 합계 {waits:g}초 — 시나리오당 최대 {q['roblox']['max_wait_sec']}초")
            if not _RETURN.search(codes[-1]):
                v.append(f"{sid} 마지막 코드에 `return` 없음 — `{{status, note}}` 테이블을 돌려준다")
        if q.get("syntax_command"):
            for n, c in enumerate(codes, 1):
                ok, out = _parses(q["syntax_command"], c, target)
                if not ok:
                    v.append(f"{sid}" + (f" 단계 {n}" if steps else "") + f" 문법 오류: {out}")
    return v


def _parses(command, code, target):
    try:
        r = procutil.run(command, cwd=str(target), shell=True, input=code, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30)
    except subprocess.TimeoutExpired:
        return False, "문법 검사 타임아웃"
    return r.returncode == 0, " ".join(((r.stderr or "") + (r.stdout or "") if r.returncode else "").split())[-300:]
