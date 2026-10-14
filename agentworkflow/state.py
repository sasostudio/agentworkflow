"""Run directory, state.json (write-ahead), raw/ originals, timeline.md (design §13).

v6 §3: the key lifecycle table in 설계서 v6 is the contract for every key here — who writes it, who clears it, what a
resume does with it. `round` counts for the whole run (A7: evidence files r{n}_* are never overwritten).
`eval_rejects` survives stage moves and resumes (B2)."""

import json
import re
import threading
import time
from pathlib import Path

from .safety import ENGINE_ROOT

AI_LOG = ENGINE_ROOT / "ai-log"
STAGE_DIRS = ["00-request", "01-planning", "02-build", "03-evaluate", "raw"]
V5_STAGES = ("DESIGN", "DEVELOPMENT", "QA", "QA_FIX", "WIKI")
# v6 N5: E1 and the second view may record a session id from two threads — one lock, one temp file per thread
_SAVE_LOCK = threading.RLock()


def slugify(title):
    s = re.sub(r"[^\w가-힣]+", "-", title).strip("-").lower()
    return (s or "run")[:40]


def new_run_dir(title, root=None):
    root = Path(root) if root else AI_LOG
    day = time.strftime("%Y-%m-%d")
    base = root / day
    base.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for d in base.iterdir() if (m := re.match(r"run(\d+)_", d.name))]
    n = (max(nums) + 1) if nums else 1
    run_dir = base / f"run{n:03d}_{slugify(title)}"
    run_dir.mkdir()
    for d in STAGE_DIRS:
        (run_dir / d).mkdir()
    return run_dir


class RunState:
    def __init__(self, run_dir, data):
        self.dir = Path(run_dir)
        self.d = data

    # ---------- persistence ----------
    @classmethod
    def create(cls, run_dir, **fields):
        data = {
            "run_id": Path(run_dir).name,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            # v6: RUNNING | AWAITING_APPROVAL | AWAITING_ANSWER | HELP | PAUSED_USAGE | COMPLETE
            "status": "RUNNING",
            "stage": "PLANNING",
            "round": 0,
            "pause_reason": None,
            "counters": new_counters(),
            "work_sessions": {},
            "inflight": {},
            "rework": [],
            "eval_no": 0,
            "history": [],
            "skipped": [],
            "base_head": None,
            "call_seq": 0,
        }
        data.update(fields)
        st = cls(run_dir, data)
        st.save()
        return st

    @classmethod
    def load(cls, run_dir):
        p = Path(run_dir) / "state.json"
        return cls(run_dir, json.loads(p.read_text(encoding="utf-8")))

    def save(self):
        with _SAVE_LOCK:
            p = self.dir / "state.json"
            tmp = p.with_name(f"state.json.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=2), encoding="utf-8")
            # ai-log lives inside the Obsidian vault (coding\); file watchers/AV can hold state.json for a moment
            # -> WinError 5 on replace (seen in tests). Bounded retry, then re-raise.
            for attempt in range(20):
                try:
                    tmp.replace(p)
                    return
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.1)

    # ---------- transitions ----------
    def goto(self, stage, note=""):
        prev = self.d["stage"]
        self.d["stage"] = stage
        if prev != stage:  # per-stage budgets only — eval_rejects and round are run-wide (v6 B2 · A7)
            c = self.d["counters"]
            c["self_fix"] = 0
            c["check_fails"] = 0
        self.d["history"].append({"t": _now(), "from": prev, "to": stage, "note": note})
        self.save()
        self.timeline(f"**{prev} → {stage}** {note}")

    def set_status(self, status, reason=None):
        self.d["status"] = status
        self.d["pause_reason"] = reason
        self.save()
        self.timeline(f"상태 **{status}**" + (f" — {reason}" if reason else ""))

    # ---------- snapshots (v4 §8) ----------
    def save_snapshot(self, name, files, by):
        """Named file-hash snapshot ({relpath: sha1|None}), written to state.json at once (write-ahead). Facts such as
        "what is still unreviewed" are computed from these instead of being accumulated in lists."""
        self.d.setdefault("snapshots", {})[name] = {"t": _now(), "by": by, "files": dict(files)}
        self.save()

    def snapshot(self, name):
        s = (self.d.get("snapshots") or {}).get(name)
        return None if s is None else dict(s["files"])

    def drop_snapshot(self, name):
        if (self.d.get("snapshots") or {}).pop(name, None) is not None:
            self.save()

    # ---------- records ----------
    def timeline(self, line):
        p = self.dir / "timeline.md"
        if not p.exists():
            p.write_text(f"# timeline — {self.d['run_id']}\n\n", encoding="utf-8")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"- `{_now()}` {line}\n")

    def record_call(self, role, stage, prompt, result, extra=None):
        self.d["call_seq"] += 1
        n = self.d["call_seq"]
        # v6: session keys carry ":" (evaluator:devil) — on Windows that names an NTFS stream, not a file (10-02 test)
        base = self.dir / "raw" / f"{n:03d}_{stage}_{role.replace(':', '-')}"
        # the last argv item is the appended system text (long) — keep only its first line, so the record shows which
        # role definition the call carried (v5 · 10-02 smoke: it was not visible at all)
        argv = [str(a) for a in (result.argv or [])]
        # which rule files this call carried: the system prompt's own section headings (working rules · role file)
        heads = [ln.strip() for ln in (argv[-1] if argv else "").splitlines()
                 if ln.startswith(("# 작업 규칙 (", "# 역할 정의서 ("))]
        base.with_suffix(".prompt.md").write_text(
            "```\n" + " ".join(argv[:-1]) + (f"\n[덧붙인 시스템 프롬프트] {' · '.join(heads)[:200]}" if heads else "")
            + "\n```\n\n" + prompt, encoding="utf-8")
        body = {"text": result.text, "structured": result.structured, "is_error": result.is_error,
                # v6 C7: the CLI reports a resumed/forked session's WHOLE spend — the engine passes this call's own
                # share as extra.real_cost; cost_cum keeps the CLI value
                "denials": result.denials, "cost_usd": (extra or {}).get("real_cost", result.cost),
                "cost_cum": result.cost, "usage": result.usage,
                "session_id": result.session_id, "raw": result.raw, "extra": extra or {}}
        base.with_suffix(".response.json").write_text(json.dumps(body, ensure_ascii=False, indent=2),
                                                        encoding="utf-8")
        c = self.d["counters"]
        c["calls"] += 1  # v6 P16: a call that left no result counts too
        if (extra or {}).get("cost_unknown"):
            c["cost_unknown"] = c.get("cost_unknown", 0) + 1
        else:
            c["cost_usd"] = round(c["cost_usd"] + ((extra or {}).get("real_cost", result.cost) or 0.0), 6)
        self.save()
        return n


def new_counters():
    """v6 §3 counters. calls · cost_usd (real — v6 C7) · cost_unknown (calls with no result line — H3) · self_fix ·
    check_fails (per stage) · check_fails_total (strength input) · no_progress (N3) · eval_rejects + eval_reject_cap
    (run-wide — B2 · N7; the cap is set from limits at run start)."""
    return {"calls": 0, "cost_usd": 0.0, "cost_unknown": 0, "self_fix": 0, "check_fails": 0, "check_fails_total": 0,
            "no_progress": 0, "eval_rejects": 0, "eval_reject_cap": None}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")
