"""Engine dashboard V2 — the uploader (엔진_대시보드_설계서 §10 · 10-04). Pushes what the local dashboard computes
(dashboard.run_payload) and the files the page can open to Supabase, so the Vercel site shows the engine from anywhere.
Python standard library only.

It is a SEPARATE process: the CLI only tries to start it when a run starts or continues (ensure_running). A network
or Supabase failure is logged here and never reaches the engine. With nothing alive for IDLE_EXIT_SEC it stops itself.

Where to look when something is wrong:
  nothing arrives in the cloud        ensure_running (started? `.dashboard-sync/uploader.log`) · sync_run · Client
  a value is wrong                     dashboard.py (the same function the PC page uses) — not here
  a file does not open on the site     upload_files (which files · their keys in payload["files"])
  old runs stay                        cleanup
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import dashboard
from .safety import ENGINE_ROOT
from .state import AI_LOG

ENV_FILE = ENGINE_ROOT / ".env.dashboard"
SYNC_DIR = ENGINE_ROOT / ".dashboard-sync"
BUCKET = "aw-files"
PERIOD_SEC = 3            # how often the uploader looks
KEEP_DAYS = 7             # a run not changed for 7 days leaves the cloud (the PC keeps it)
IDLE_EXIT_SEC = 600       # nothing alive for 10 minutes -> the uploader stops itself
CLEANUP_EVERY_SEC = 3600
MAX_FILE = 50 * 1024 * 1024  # Supabase free plan: 50 MB per file
# the files the page can open (dashboard.html: raw bar · verdict chip · results · scenario log · evidence)
FILE_GLOBS = ("raw/*.response.json", "03-evaluate/r*_*.json", "03-evaluate/e*_a*/results_final.json",
              "03-evaluate/e*_a*/log_*.txt", "03-evaluate/e*_a*/evidence/**/*")
# what changes on every look without anything happening — left out when deciding "did the run change?"
# (the site adds the time passed since payload["now"] itself)
_VOLATILE = {"now", "hb_age", "elapsed", "estimate"}


class ConfigMissing(Exception):
    pass


def load_env(path=None):
    """KEY=value lines (# comments). -> {"url", "publishable", "secret"}; ConfigMissing when url/secret are empty."""
    path = path or ENV_FILE
    env = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise ConfigMissing(f"{path} 없음") from e
    for ln in lines:
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip().strip("\"'")
    cfg = {"url": env.get("SUPABASE_URL", "").rstrip("/"), "publishable": env.get("SUPABASE_PUBLISHABLE_KEY", ""),
           "secret": env.get("SUPABASE_SECRET_KEY", "")}
    if not cfg["url"] or not cfg["secret"]:
        raise ConfigMissing(f"{path}: SUPABASE_URL / SUPABASE_SECRET_KEY 비어 있음")
    return cfg


# ================= Supabase over plain HTTPS =================
class Client:
    def __init__(self, url, secret, timeout=20):
        self.url, self.key, self.timeout = url.rstrip("/"), secret, timeout

    def _call(self, method, path, body=None, headers=None):
        h = {"apikey": self.key, "Authorization": "Bearer " + self.key, **(headers or {})}
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path.split('?')[0]} → {e.code} {e.read()[:200]!r}") from None
        return json.loads(data) if data[:1] in (b"[", b"{") else None

    def upsert_run(self, row):
        self._call("POST", "/rest/v1/aw_runs", row,
                   {"Prefer": "resolution=merge-duplicates,return=minimal"})

    def upload(self, key, data, ctype):
        self._call("POST", f"/storage/v1/object/{BUCKET}/{key}", data,
                   {"Content-Type": ctype, "x-upsert": "true"})

    def old_runs(self, before_iso):
        q = urllib.parse.quote(before_iso, safe="")
        return [r["id"] for r in self._call("GET", f"/rest/v1/aw_runs?select=id&updated_at=lt.{q}") or []]

    def delete_run(self, run_id):
        prefix = run_key(run_id)
        names = [o["name"] for o in self._call("POST", f"/storage/v1/object/list/{BUCKET}",
                                               {"prefix": prefix, "limit": 1000}) or [] if o.get("name")]
        if names:
            self._call("DELETE", f"/storage/v1/object/{BUCKET}", {"prefixes": [f"{prefix}/{n}" for n in names]})
        self._call("DELETE", "/rest/v1/aw_runs?id=eq." + urllib.parse.quote(run_id, safe=""))


# ================= what goes up =================
def run_key(run_id):
    """Storage keys must be plain ASCII — run folders carry Korean titles, so a run is a short hash."""
    return hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:16]


def file_key(run_id, rel):
    ext = Path(rel).suffix.lower()
    return f"{run_key(run_id)}/{hashlib.sha1(rel.encode('utf-8')).hexdigest()[:20]}{ext}"


def stable(obj):
    """The payload without what changes on its own (_VOLATILE) — equal hashes = nothing happened."""
    if isinstance(obj, dict):
        return {k: stable(v) for k, v in obj.items() if k not in _VOLATILE}
    if isinstance(obj, list):
        return [stable(v) for v in obj if not (isinstance(v, dict) and v.get("running"))]
    return obj


def payload_hash(payload):
    return hashlib.sha1(json.dumps(stable(payload), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def recent_runs(ai_log=AI_LOG, now=None, days=KEEP_DAYS):
    """Run folders touched in the last `days` (the newest of state · heartbeat · timeline)."""
    now = now or time.time()
    out = []
    for sp in Path(ai_log).glob("*/run*/state.json"):
        rd = sp.parent
        if not dashboard.RUN_ID.match(f"{rd.parent.name}/{rd.name}"):
            continue
        t = max((p.stat().st_mtime for p in (sp, rd / "heartbeat.json", rd / "timeline.md") if p.exists()), default=0)
        if now - t <= days * 86400:
            out.append((rd, t))
    return out


def upload_files(client, rd, run_id, sent):
    """New or changed files the page can open -> Storage. sent: {rel: [mtime, size]} (updated in place).
    -> {rel: key} for every file now in the cloud."""
    keys = {}
    for pat in FILE_GLOBS:
        for p in sorted(rd.glob(pat)):
            if not p.is_file():
                continue
            rel = p.relative_to(rd).as_posix()
            st = p.stat()
            if st.st_size > MAX_FILE:
                continue
            sig = [round(st.st_mtime, 3), st.st_size]
            if sent.get(rel) != sig:
                client.upload(file_key(run_id, rel), p.read_bytes(),
                              dashboard._TYPES.get(p.suffix.lower(), "application/octet-stream"))
                sent[rel] = sig
            keys[rel] = file_key(run_id, rel)
    return keys


def sync_run(client, rd, mem, now, campaigns=dashboard.CAMPAIGNS):
    """One run: files first (so the payload never points at a missing file), then the row when something changed.
    mem = this run's memory {"hash", "hb", "files"}. -> True when the row was sent."""
    run_id = f"{rd.parent.name}/{rd.name}"
    files = upload_files(client, rd, run_id, mem.setdefault("files", {}))
    payload = dashboard.run_payload(rd, campaigns, now=now)
    payload["files"] = files
    h = payload_hash(payload)
    hb = dashboard._json(rd / "heartbeat.json") or {}
    hb_epoch = hb.get("epoch") if isinstance(hb.get("epoch"), (int, float)) else None
    if h == mem.get("hash") and hb_epoch == mem.get("hb"):
        return False
    st = dashboard._json(rd / "state.json", {}) or {}
    c = st.get("counters") or {}
    camp = st.get("campaign") or {}
    client.upsert_run({"id": run_id, "created": st.get("created"), "status": st.get("status"),
                       "stage": st.get("stage"), "calls": c.get("calls", 0), "cost": c.get("cost_usd", 0.0),
                       "campaign": camp.get("name"), "feature": ",".join(camp.get("features") or []) or None,
                       "hb_epoch": hb_epoch, "payload": payload,
                       "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))})
    mem["hash"], mem["hb"] = h, hb_epoch
    return True


USAGE_ID = "__usage__"  # one extra row: the account usage (dashboard.account_usage) — the site reads it every refresh


def sync_usage(client, mem, now, ai_log=AI_LOG):
    """The newest account usage reading -> its own small row when it changed (not inside every run's payload, or a
    new reading would re-send every run). -> True when sent."""
    u = dashboard.account_usage(ai_log, now)
    if not u or u == mem.get("usage"):
        return False
    client.upsert_run({"id": USAGE_ID, "status": "USAGE", "payload": u,
                       "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))})
    mem["usage"] = u
    return True


def cleanup(client, mem_runs, now):
    """Runs whose row was not updated for KEEP_DAYS leave the cloud (row + files)."""
    before = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - KEEP_DAYS * 86400))
    gone = client.old_runs(before)
    for run_id in gone:
        client.delete_run(run_id)
        mem_runs.pop(run_id, None)
    return gone


def any_alive(runs, now):
    for rd, _t in runs:
        st = dashboard._json(rd / "state.json", {}) or {}
        hb = dashboard._json(rd / "heartbeat.json") or {}
        if st.get("status") == "RUNNING" and isinstance(hb.get("epoch"), (int, float)) and now - hb["epoch"] < 120:
            return True
    return False


def _uploader_alive(pid):
    """A pid can be reused by another program — alive only when it is a python process (tasklist image name)."""
    if not pid:
        return False
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True,
                       errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return any(line.lower().startswith('"python') and f'"{pid}"' in line for line in r.stdout.splitlines())


def _read_pid():
    try:
        return int((SYNC_DIR / "uploader.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


# ================= the loop =================
def _load_mem():
    try:
        return json.loads((SYNC_DIR / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"runs": {}, "cleanup": 0}


def _save_mem(mem):
    SYNC_DIR.mkdir(exist_ok=True)
    tmp = SYNC_DIR / "state.json.tmp"
    tmp.write_text(json.dumps(mem, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SYNC_DIR / "state.json")


def _log(line):
    print(f"[{time.strftime('%m-%d %H:%M:%S')}] {line}", flush=True)


def loop(client, ai_log=AI_LOG, campaigns=dashboard.CAMPAIGNS, sleep=time.sleep, clock=time.time, once=False):
    """Look every PERIOD_SEC until nothing has been alive for IDLE_EXIT_SEC. A failure skips that run this time."""
    mem = _load_mem()
    last_alive = clock()
    while True:
        now = clock()
        runs = recent_runs(ai_log, now)
        for rd, _t in runs:
            run_id = f"{rd.parent.name}/{rd.name}"
            try:
                if sync_run(client, rd, mem["runs"].setdefault(run_id, {}), now, campaigns):
                    _log(f"올림 {run_id}")
            except (RuntimeError, OSError, ValueError) as e:  # network · Supabase · a file mid-write: next look
                _log(f"실패 {run_id}: {e}")
        try:
            sync_usage(client, mem, now, ai_log)
        except (RuntimeError, OSError, ValueError) as e:
            _log(f"사용량 실패: {e}")
        if now - mem.get("cleanup", 0) >= CLEANUP_EVERY_SEC:
            try:
                gone = cleanup(client, mem["runs"], now)
                mem["cleanup"] = now
                if gone:
                    _log(f"7일 지나 삭제 {len(gone)}개: {', '.join(gone)[:300]}")
            except (RuntimeError, OSError, ValueError) as e:
                _log(f"정리 실패: {e}")
        _save_mem(mem)
        if any_alive(runs, now):
            last_alive = now
        if once or now - last_alive >= IDLE_EXIT_SEC:
            return 0
        sleep(PERIOD_SEC)


def main():
    """`dashboard-sync` — the uploader process (normally started by ensure_running)."""
    try:
        cfg = load_env()
    except ConfigMissing as e:
        _log(f"업로더 꺼짐 — {e}")
        return 2
    SYNC_DIR.mkdir(exist_ok=True)
    other = _read_pid()
    if other and other != os.getpid() and _uploader_alive(other):  # two starts at once: one uploader is enough
        _log(f"업로더가 이미 돈다(pid {other}) — 이 프로세스는 끝냄")
        return 0
    lock = SYNC_DIR / "uploader.pid"
    lock.write_text(str(os.getpid()), encoding="utf-8")
    _log("업로더 시작")
    try:
        return loop(Client(cfg["url"], cfg["secret"]))
    finally:
        if lock.is_file() and lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink()
        _log("업로더 끝")


# ================= started by the CLI (run · resume · approve · reject · answer) =================
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — no console window
_BREAKAWAY = 0x01000000              # CREATE_BREAKAWAY_FROM_JOB (outlives a supervisor's job)


def ensure_running(out=print):
    """Start the uploader unless it is already running or the cloud is not set up. Never raises — the run goes on
    whatever happens here. -> "running" | "started" | "off" | "failed"."""
    try:
        load_env()
    except ConfigMissing:
        return "off"
    if _uploader_alive(_read_pid()):
        return "running"
    SYNC_DIR.mkdir(exist_ok=True)
    argv = [sys.executable, "-m", "agentworkflow", "dashboard-sync"]
    with open(SYNC_DIR / "uploader.log", "a", encoding="utf-8") as log:
        for flags in (_DETACHED | _BREAKAWAY, _DETACHED):  # a job that forbids breakaway refuses the first (OSError)
            try:
                subprocess.Popen(argv, cwd=str(ENGINE_ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 creationflags=flags, close_fds=True)
                out("대시보드 업로더 시작(클라우드 대시보드 갱신)")
                return "started"
            except OSError:
                continue
    out("⚠️대시보드 업로더를 띄우지 못함 — run은 그대로 진행(클라우드 대시보드만 갱신 안 됨)")
    return "failed"
