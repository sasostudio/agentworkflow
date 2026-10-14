"""Continuous mode ("campaign") — several runs in a row build one game (design §21).

The admin approves ONE plan (goal · ordered feature list · repo/branch · caps). After that the Master writes and
self-approves each run's request, the engine runs it, and on COMPLETE the Master calls `campaign commit`, which
makes ONE local commit (never push) so the next run starts on a clean tree. What code enforces here:
  - the approved plan is frozen by hash — editing it needs a new admin approval
  - runs go in feature order, only on the campaign branch, only in the campaign repo, within the caps
  - commits happen only for a COMPLETE run of this campaign, never with secrets-looking or check-artifact files
The engine proper (gitutil) stays read-only; the only git writes live in this module, behind those checks.
"""

import datetime as dt
import hashlib
import json
import re
import subprocess
from pathlib import Path

from . import gitutil
from .safety import ENGINE_ROOT, SafetyError, inside_work_root

CAMPAIGNS = ENGINE_ROOT / "campaigns"
_FEATURE = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+(?P<id>F-\d{2,3})\s*(?:\[L(?P<lvl>[1-3])\])?\s*(?P<title>.+?)\s*$", re.M)
_SECRETS = re.compile(r"(\.env$|\.pem$|\.key$|serviceaccount|secret|credential|password|token)", re.I)
COMMIT_TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
PROTECTED = {"main", "master"}


class CampaignError(SafetyError):
    pass


def _sections(text):
    heads = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.M))
    return {m.group(1).strip(): text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)].strip()
            for i, m in enumerate(heads)}


def _field(body, key):
    m = re.search(rf"^\s*[-*]\s*{key}\s*[:：]\s*(.+?)\s*$", body, re.M)
    return m.group(1).strip() if m else None


def parse_plan(text):
    s = _sections(text)
    for need in ("목표", "대상", "기능 목록", "상한"):
        if not s.get(need):
            raise CampaignError(f"캠페인 계획에 `## {need}` 절이 없음")
    repo, branch, commit = (_field(s["대상"], k) for k in ("레포", "브랜치", "커밋"))
    # v6 H6 · N15: `- Wiki: 있음` = the engine keeps a wiki for this game (creates the skeleton on the first run).
    # Part of the approved (hashed) plan — changing it needs a new admin approval.
    wiki_raw = (_field(s["대상"], "Wiki") or "없음").split()[0]
    if wiki_raw not in ("있음", "없음"):
        raise CampaignError(f"`## 대상`의 `- Wiki:` 는 있음 / 없음 (받은 값 {wiki_raw})")
    if not repo or not branch:
        raise CampaignError("`## 대상`에 `- 레포:` 와 `- 브랜치:` 필요")
    if (commit or "").split()[0:1] != ["허용"]:
        raise CampaignError("`## 대상`의 `- 커밋: 허용` 이 없으면 연속 모드 불가(자동 커밋은 사용자가 허용한 레포만)")
    if branch in PROTECTED:
        raise CampaignError(f"캠페인 브랜치가 {branch} — feature 브랜치를 쓴다")
    feats = [{"id": m.group("id"), "level": int(m.group("lvl")) if m.group("lvl") else None,
              "title": m.group("title")} for m in _FEATURE.finditer(s["기능 목록"])]
    if not feats:
        raise CampaignError("`## 기능 목록`에 `- [ ] F-01 [L1] 제목` 형식 항목이 없음")
    ids = [f["id"] for f in feats]
    if len(set(ids)) != len(ids):
        raise CampaignError("기능 ID 중복")
    caps = {}
    for key, name in (("runs", "run"), ("calls", "호출"), ("cost_usd", "비용")):
        v = _field(s["상한"], name)
        if not v or not re.match(r"^\d+(\.\d+)?", v):
            raise CampaignError(f"`## 상한`에 `- {name}: 숫자` 필요")
        caps[key] = float(re.match(r"^\d+(\.\d+)?", v).group(0))
    rp = Path(repo).resolve()
    if not inside_work_root(rp):
        raise CampaignError(f"레포가 작업 범위(AW_WORK_ROOT) 밖: {rp}")
    return {"repo": str(rp), "branch": branch, "features": feats, "caps": caps, "wiki": wiki_raw == "있음"}


def feature_ids(feature):
    """v4 §16-17: one run (a Master "bundle") may cover several consecutive features — `--feature F-11,F-12,F-13`.
    -> [ids] in the given order (a plain `F-11` is a one-item list)."""
    ids = [x.strip() for x in str(feature or "").split(",") if x.strip()]
    if not ids:
        raise CampaignError("--feature 가 비어 있음")
    if len(set(ids)) != len(ids):
        raise CampaignError(f"--feature 에 같은 기능이 두 번: {feature}")
    return ids


def _run_ids(entry):
    return entry.get("features") or [entry["feature"]]


def _hash(text):
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Campaign:
    def __init__(self, name, root=None):
        self.dir = Path(root or CAMPAIGNS) / name
        self.name = name
        self.plan_path = self.dir / "campaign.md"
        self.ledger_path = self.dir / "campaign.json"

    # ---------- persistence ----------
    def exists(self):
        return self.ledger_path.is_file()

    def load(self):
        if not self.exists():
            raise CampaignError(f"캠페인 없음: {self.name}")
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))

    def save(self, d):
        self.ledger_path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    def log(self, d, line):
        d.setdefault("log", []).append(f"{_now()} {line}")

    # ---------- lifecycle ----------
    def create(self, plan_file):
        if self.exists():
            raise CampaignError(f"이미 있는 캠페인: {self.name}")
        text = Path(plan_file).read_text(encoding="utf-8")
        parse_plan(text)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.plan_path.write_text(text, encoding="utf-8")
        d = {"name": self.name, "status": "DRAFT", "approved_hash": None, "runs": [], "done": {}}
        self.log(d, "생성(DRAFT) — 사용자 승인 전에는 run 불가")
        self.save(d)
        return d

    def plan(self):
        return parse_plan(self.plan_path.read_text(encoding="utf-8"))

    def approve(self, note):
        """Admin approval (the Master runs this ONLY after the admin said OK). Freezes the plan and puts the repo
        on the campaign branch (creates it from the current HEAD if missing; needs a clean tree)."""
        d = self.load()
        p = self.plan()
        repo, branch = Path(p["repo"]), p["branch"]
        if not gitutil.is_repo(repo):
            raise CampaignError(f"git 레포가 아님: {repo}")
        cur = gitutil.current_branch(repo)
        if cur != branch:
            if gitutil.dirty_paths(repo):
                raise CampaignError("브랜치를 바꾸려면 작업 트리가 깨끗해야 함: " + ", ".join(gitutil.dirty_paths(repo)[:8]))
            exists = bool(gitutil.git(repo, "branch", "--list", branch).strip())
            _git_write(repo, "checkout", branch) if exists else _git_write(repo, "checkout", "-b", branch)
        d["approved_hash"] = _hash(self.plan_path.read_text(encoding="utf-8"))
        d["status"] = "ACTIVE"
        self.log(d, f"사용자 승인 → ACTIVE · 브랜치 {branch} · {note}")
        self.save(d)
        return d

    def ensure_active(self, d=None):
        d = d or self.load()
        if d["status"] != "ACTIVE":
            raise CampaignError(f"캠페인 상태 {d['status']} — 사용자 승인(ACTIVE) 뒤에만 run")
        if _hash(self.plan_path.read_text(encoding="utf-8")) != d["approved_hash"]:
            raise CampaignError("승인 뒤 캠페인 계획이 바뀜 — 사용자 재승인(`campaign approve`) 필요")
        return d

    def next_feature(self, d=None):
        d = d or self.load()
        return next((f for f in self.plan()["features"] if f["id"] not in d["done"]), None)

    def totals(self, d=None):
        d = d or self.load()
        calls = cost = 0
        for r in d["runs"]:
            st = Path(r["run_dir"]) / "state.json"
            if st.is_file():
                c = json.loads(st.read_text(encoding="utf-8"))["counters"]
                calls += c.get("calls", 0)
                cost += c.get("cost_usd", 0.0)
        return {"runs": len(d["runs"]), "calls": calls, "cost_usd": round(cost, 4)}

    # ---------- gates ----------
    def check_run(self, feature_id, target, config_cfg, run_level=None):
        """Called by `run --campaign`: order, repo, branch, caps, difficulty floor. Raises CampaignError.
        feature_id may list several consecutive features (a bundle — they must be the next ones, in plan order)."""
        ids = feature_ids(feature_id)
        d = self.ensure_active()
        p = self.plan()
        if Path(target).resolve() != Path(p["repo"]):
            raise CampaignError(f"engine.json target이 캠페인 레포가 아님: {target} ≠ {p['repo']}")
        cur = gitutil.current_branch(Path(p["repo"]))
        if cur != p["branch"]:
            raise CampaignError(f"현재 브랜치 {cur} — 캠페인 브랜치 {p['branch']} 에서만 run")
        left = [f for f in p["features"] if f["id"] not in d["done"]]
        if not left:
            raise CampaignError("남은 기능 없음 — 캠페인 완료")
        want = left[:len(ids)]
        if [f["id"] for f in want] != ids:
            raise CampaignError(f"다음 기능은 {', '.join(f['id'] for f in want)} (계획 순서) — 받은 값 {', '.join(ids)}: "
                                "순서를 건너뛸 수 없음(바꾸려면 계획 수정+재승인)")
        open_runs = [r for r in d["runs"] if set(_run_ids(r)) & set(ids)
                     and r.get("status") not in ("COMMITTED", "ABANDONED")]
        if open_runs:
            raise CampaignError(f"{', '.join(ids)} 의 run이 아직 열려 있음({open_runs[-1]['run_id']}) — 이어서 하거나 "
                                "사용자 판단(`campaign abandon`)")
        for nxt in want:
            if nxt["level"] and run_level is not None and run_level < nxt["level"]:
                # C9: the Master self-approves requests, but not below what the admin approved in the plan
                raise CampaignError(f"{nxt['id']} 는 계획상 L{nxt['level']} — request 난이도 L{run_level} 로 낮출 수 없음")
        t = self.totals(d)
        caps = p["caps"]
        if t["runs"] >= caps["runs"] or t["calls"] >= caps["calls"] or t["cost_usd"] >= caps["cost_usd"]:
            raise CampaignError(f"캠페인 상한 도달 {t} / {caps} — 사용자 판단")
        return want[0]  # the bundle's first feature (the others are in the ledger entry's `features`)

    def record_run(self, feature_id, run_dir, run_id):
        ids = feature_ids(feature_id)
        d = self.load()
        d["runs"].append({"feature": ids[0], "features": ids, "run_id": run_id, "run_dir": str(run_dir),
                          "status": "RUNNING", "started": _now()})
        self.log(d, f"{', '.join(ids)} run 시작 {run_id}")
        self.save(d)

    def abandon(self, run_id, reason):
        """Admin decision: give up this run of the feature (C2 · 09-25). The engine never discards the work —
        cleaning the tree (stash/revert) is the admin's call."""
        d = self.load()
        entry = next((r for r in d["runs"] if r["run_id"] == run_id), None)
        if not entry or entry.get("status") == "COMMITTED":
            raise CampaignError(f"포기할 수 있는 run이 아님: {run_id}")
        entry.update(status="ABANDONED", reason=reason, abandoned=_now())
        self.log(d, f"{entry['feature']} run 포기 {run_id} — {reason}")
        self.save(d)

    def update_status(self, run_id, status):
        d = self.load()
        for r in d["runs"]:
            if r["run_id"] == run_id and r.get("status") not in ("COMMITTED", "ABANDONED"):
                r["status"] = status
        self.save(d)

    def commit(self, run_dir):
        """One local commit for a COMPLETE run of this campaign. Never pushes."""
        d = self.ensure_active()
        p = self.plan()
        st = json.loads((Path(run_dir) / "state.json").read_text(encoding="utf-8"))
        entry = next((r for r in d["runs"] if r["run_id"] == st["run_id"]), None)
        if not entry:
            raise CampaignError(f"이 캠페인의 run이 아님: {st['run_id']}")
        if entry.get("status") == "COMMITTED":
            raise CampaignError("이미 커밋한 run")
        if st["status"] != "COMPLETE":
            raise CampaignError(f"run 상태 {st['status']} — COMPLETE만 커밋")
        repo = Path(p["repo"])
        if Path(st["target"]).resolve() != repo:
            raise CampaignError("run 대상이 캠페인 레포가 아님")
        if gitutil.current_branch(repo) != p["branch"]:
            raise CampaignError(f"브랜치가 {p['branch']} 가 아님")
        dirty = gitutil.dirty_paths(repo)
        if not dirty:
            raise CampaignError("커밋할 변경이 없음")
        # C12 (09-25): only what the reviewed run left behind — nothing edited after COMPLETE
        final = st.get("final_snapshot")
        if final is None:
            raise CampaignError("run에 완료 시점 스냅샷이 없음 — 검수된 상태를 확인할 수 없어 커밋 거부")
        now = gitutil.snapshot(repo)
        drift = sorted({p for p in set(now) | set(final) if now.get(p, "_") != final.get(p, "_")})
        if drift:
            raise CampaignError("run 완료 뒤 바뀐 파일(검수 안 받음) — 커밋 거부: " + ", ".join(drift[:8]))
        secrets = [f for f in dirty if _SECRETS.search(Path(f).name)]
        if secrets:
            raise CampaignError("비밀처럼 보이는 파일이 변경 목록에 있음 — 사용자 확인: " + ", ".join(secrets))
        artifacts = [f for f in dirty if f in set(st.get("check_artifacts") or [])]
        if artifacts:
            raise CampaignError("기계검사 산출물이 git에 잡힘 — .gitignore 필요(사용자 확인): " + ", ".join(artifacts[:8]))
        fids = _run_ids(entry)
        feats = [f for f in p["features"] if f["id"] in fids]
        scope = fids[0] if len(fids) == 1 else f"{fids[0]}..{fids[-1]}"
        msg = (f"feat({scope}): {' · '.join(f['title'] for f in feats)}\n\n"
               f"AgentWorkflow campaign `{self.name}` · run `{st['run_id']}` · 호출 {st['counters']['calls']}회\n\n"
               f"{COMMIT_TRAILER}")
        _git_write(repo, "add", "-A")
        _git_write(repo, "commit", "-q", "-m", msg)
        head = gitutil.head(repo)
        entry.update(status="COMMITTED", commit=head, committed=_now())
        for fid in fids:
            d["done"][fid] = head
        self.log(d, f"{', '.join(fids)} 커밋 {head[:10]} ({st['run_id']})")
        if self.next_feature(d) is None:
            d["status"] = "FINISHED"
            self.log(d, "모든 기능 완료 → FINISHED (main 병합·push는 사용자)")
        self.save(d)
        return head

    def status_text(self):
        d = self.load()
        p = self.plan()
        t = self.totals(d)
        nxt = self.next_feature(d)
        lines = [f"캠페인 {self.name} · {d['status']} · 레포 {p['repo']} · 브랜치 {p['branch']}",
                 f"사용 {t} / 상한 {p['caps']}",
                 "다음 기능: " + (f"{nxt['id']} {nxt['title']}" if nxt else "없음")]
        for f in p["features"]:
            mark = "x" if f["id"] in d["done"] else " "
            lines.append(f"- [{mark}] {f['id']} {f['title']}" + (f" ({d['done'][f['id']][:10]})" if mark == "x" else ""))
        return "\n".join(lines)


_GIT_WRITE = {"add", "commit", "checkout"}


def _git_write(repo, *args):
    """The ONLY git writes in the engine. No push / reset / clean / branch -D exist on purpose."""
    if (args[0] not in _GIT_WRITE or any(a in ("--force", "-f", "--hard", "-B", "--amend") for a in args)
            or (args[0] == "checkout" and ("--" in args or "." in args))):  # `checkout -- f` discards changes
        raise CampaignError(f"허용 안 된 git 쓰기: {' '.join(args)}")
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise CampaignError(f"git {' '.join(args[:2])} 실패: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout
