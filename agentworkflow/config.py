"""engine.json loading. JSON because Python 3.10 has no tomllib (design §15).

v6 (10-02 · 설계서 v6 §6-2 · §6-3): two AI roles (builder · evaluator), one end gate (always), the v5/v4 keys of
removed parts are refused with a migration hint instead of being silently ignored."""

import copy
import json
from pathlib import Path

from . import capabilities, selfcheck
from .safety import ENGINE_ROOT

DEFAULTS = {
    "target": None,
    "check_commands": [],
    "check_timeout_sec": 1800,
    "qa": {
        "runner": "none",          # none | command | web | godot | roblox
        "environment": "external",  # managed | isolated | external
        "base_url": None,
        "managed": {"start": None, "ready_url": None, "ready_timeout_sec": 90},
        "command": None,            # runner=command: shell command with {scenarios} {evidence} {results} {target} {base_url}
        "godot_exe": None,          # runner=godot
        "scenario_timeout_sec": 300,
        # v6 §5-2: the cap is max(this, code items x 3), at most 100
        "max_scenarios": 20,
        "syntax_command": None,     # parses one scenario's code from stdin, exit 0 = valid (e.g. `stylua.exe -`)
        # runner=roblox (09-24, design §19): the ENGINE drives Studio MCP; AI roles never get execute_luau
        "roblox": {
            "studio_name": None,        # substring of the Studio instance name (required when several are open)
            "mcp_command": None,        # default: studio_mcp.DEFAULT_COMMAND (%LOCALAPPDATA%\Roblox\mcp.bat)
            "sourcemap_command": None,  # default: `rojo sourcemap default.project.json` (rojo on PATH)
            "sync_check": True,         # files == Studio Source for changed scripts, else BLOCKED
            "sync_wait_sec": 15,        # re-check a mismatch this long (Rojo may still be pushing fresh edits)
            "play_ready_timeout_sec": 30,
            "unit_test_code": None,     # optional Server Luau returning {run, passed, failed}, run before scenarios
            "max_wait_sec": 60,         # v4 §10: sum of task.wait(N) literals in one scenario (#8: 150 s of waits)
            # v6 N14 (#13 · 09-26: a QA scenario left coins = 5,000,000 in the Studio DataStore and broke the next QA):
            # Server Luau the ENGINE runs before and after every play (scenarios and the builder's looks) to clear
            # test keys — runs even when the scenario failed
            "reset_code": None,
        },
    },
    "plan_dir": "docs/plan",
    "wiki_dir": "docs/wiki",
    # v6 H6: a campaign plan `- Wiki: 있음` makes the engine create the skeleton; an ordinary run writes the wiki only
    # when the folder exists. (cli sets this from the campaign plan — not an engine.json key.)
    "wiki_create": False,
    "doc_extensions": [".md", ".txt"],
    "high_risk_paths": [],
    # v5 D5 (10-01): the agreements between modules. An item may touch one only when its text carries `[약속 변경]`.
    "contract_files": [],
    # v5 D4: Master judges difficulty; these force high whatever the level says (the engine only raises)
    "high_floor": {"visual": True, "high_risk_paths": True, "contract_files": True},
    "limits": {
        "max_calls_per_run": 90,    # v4 §12: measured max 35 (10~14 items) x ~2.5 for big runs
        "max_questions": 3,         # direction questions per run (AWAITING_ANSWER) before HELP
        "max_self_fix": 3,          # rule/format/no-progress/scenario-fix violations per stage (3rd = HELP)
        "max_check_fails": 3,       # machine-check failures of the LAST BUILD call (v6 B8 — middle calls do not count)
        "max_retries": 3,           # transient failures per stage before HELP
        "retry_backoff_sec": [30, 120, 300],
        # v6 B2·N7: run-wide evaluation rejects (not reset by stage moves or resumes); every Master intervention
        # raises the cap to rejects + this
        "max_eval_rejects": 3,
        # decision 10-03: scenario runs the main evaluator may ask for in ONE evaluation (RUN) — over it = HELP
        "max_eval_runs": 3,
        "no_progress_max": 2,       # v6 N3: BUILD calls in a row with no new check and the same tree
        "rework_high_after": 2,     # the builder's rework after the 2nd evaluation reject runs at high (v5 D4)
        # v6 D1 + T9 (10-02 18:08 measured): with --include-partial-messages the longest silence while Sonnet wrote a
        # 58 KB file was 3.8 s (without it 105 s); tools report every 30 s (S5). So a call silent this long is stuck.
        "idle_timeout_sec": 300,
        "call_total_sec": 10800,    # the last line of defence for one call (3 h)
        # display only (v6 S-1): a session idle longer than this re-reads its history uncached (measured 20 min hit ·
        # 65 min miss — C2). The run still continues the session (context first).
        "continue_window_sec": 3000,
        # D' · v5 notify: at 95% (5 h) the engine stops (PAUSED_USAGE) so the Master is told; the
        # fallback resumes fallback_min after the reset. 7-day 97% stops for the user (HELP).
        # v6 S-8: per_call estimates are kept per session key; the 5-hour estimate is capped (per_call_cap)
        "usage_pause": {"five_hour": 0.95, "seven_day": 0.97, "per_call_default": 0.01, "per_call_cap": 0.10,
                        "fallback_min": 10},
        "review_diff_chars": 60000,  # diff sent to the evaluator is cut here; cut files are named + re-asked
        "max_cost_usd_per_run": None,
        "max_reset_wait_sec": 6 * 3600,
    },
    # v4 §11: the builder's self-check (selfcheck.py) — off while `commands` is empty. Templates run without a
    # shell: {files} = the files to check · {tmp} = a fresh folder per check (see selfcheck.py)
    "self_check": {"commands": [], "extensions": [], "timeout_sec": 120, "max_fixes": 2},
    # v4 §6: the supervisor's watch values (the engine writes heartbeat.json every heartbeat_sec from a side thread)
    "supervisor": {"heartbeat_sec": 15, "stall_sec": 120, "poll_sec": 5, "restart_window_sec": 3600, "max_restarts": 3},
    # exact model IDs, never aliases: an alias silently follows the CLI version (09-23: "opus" resolved to Opus 5).
    # v6 §2: builder = Sonnet 5.5 · evaluator (and its second-view / devil sessions) = Opus 5.5
    "models": {
        "builder": {"model": "claude-sonnet-5-5", "effort": "medium"},
        "evaluator": {"model": "claude-opus-5-5", "effort": "medium"},
    },
    # difficulty level -> evaluation strength. Keys are "1".."3".
    #   effort = the evaluator's base effort (the high floor can only raise it)
    #   second = force the second-view evaluator (security · data) on every evaluation of this level. Off by default:
    #            10-03 정렬안 B9 — it is called when a high-risk path or a contract file changed, not for L3 alone.
    #   (min_failure — removed 10-03 정렬안 A6: how many scenarios are enough is the evaluator's call; ignored if set)
    "levels": {
        "evaluate": {"1": {"effort": "medium", "second": False},
                     "2": {"effort": "medium", "second": False},
                     "3": {"effort": "high", "second": False}},
    },
    "providers": {
        "work": "claude",
        "review": "claude",
        "claude_args": [],          # extra CLI args for every claude call (isolation args come from capabilities)
    },
    # The rule files (WORKING_RULES.md · roles/ · skills/ — roles.py, prompts.SKILL_FILES). Sub-agents read them via
    # --add-dir, and nothing else from the user's settings. Web / MCP / plugins are granted per run.
    "rules_root": str(ENGINE_ROOT / "rules"),
    # each role's definition (roles/*.md) is appended to the system prompt after the working rules
    "role_definitions": True,
    # v5 D8: on a visual run the builder may look at the running result itself — when the runner's adapter offers it
    "dev_shot": True,
    "capabilities": copy.deepcopy(capabilities.DEFAULT),
}

VALID_RUNNERS = {"none", "command", "web", "godot", "roblox"}
MODEL_ROLES = ("builder", "evaluator")
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
VALID_ENVS = {"managed", "isolated", "external"}

# v6 §6-3 (B9): keys of parts v6 removed — refused with what to do, never silently ignored
REMOVED_KEYS = {
    "master_gates": "삭제 — v6는 run 끝 승인 1번이 항상 있다(설정으로 못 끔)",
    "require_plan_approval": "삭제 — PLANNING은 Master가 쓴 `## 항목`이라 승인 멈춤이 없다",
    "create_wiki": "삭제 — 캠페인 계획 `- Wiki: 있음` 또는 wiki 폴더가 있으면 켜진다",
    "use_seed": "삭제 — seed는 v5에서 폐지(D6)",
    "large_run": "삭제 — 대형 run 폐지(v6 결정 3)",
}
REMOVED_LIMITS = {
    "parallel_dev": "병렬 개발 폐지(v6 D-1)", "max_parallel_devs": "병렬 개발 폐지", "max_items_per_worker": "병렬 개발 폐지",
    "parallel_reviewers": "2인 패널은 2번째 관점 평가자로(levels.evaluate.N.second)",
    "merge_l3_design": "설계 단계 폐지 — 작업자가 설계까지", "max_session_uses": "세션은 run 동안 이어 쓴다(v6 S-1)",
    "max_qa_cycles": "평가 회차는 limits.max_eval_rejects", "max_review_rounds": "limits.max_eval_rejects",
    "call_timeout_sec": "limits.idle_timeout_sec + limits.call_total_sec(v6 D1)",
    "call_timeout_by_role": "limits.idle_timeout_sec + limits.call_total_sec(v6 D1)",
}
OLD_MODEL_KEYS = {"planner", "designer", "developer", "wiki", "qa", "review"}
OLD_LEVEL_KEYS = {"plan_review", "design_review", "dev_review", "qa", "wiki_review"}


class ConfigError(Exception):
    pass


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def check_removed(raw):
    """v6 §6-3: a v5 engine.json key -> ConfigError naming the v6 way (B9)."""
    bad = [f"`{k}` — {why}" for k, why in REMOVED_KEYS.items() if k in raw]
    bad += [f"`limits.{k}` — {why}" for k, why in REMOVED_LIMITS.items() if k in (raw.get("limits") or {})]
    if isinstance((raw.get("limits") or {}).get("usage_pause"), dict) and "mode" in raw["limits"]["usage_pause"]:
        bad.append("`limits.usage_pause.mode` — v6는 notify만(WAITING_RESET 폐지 · P18)")
    old_m = sorted(OLD_MODEL_KEYS & set(raw.get("models") or {}))
    if old_m:
        bad.append(f"`models.{'/'.join(old_m)}` — v6 역할은 builder(← developer) · evaluator(← review)")
    old_l = sorted(OLD_LEVEL_KEYS & set(raw.get("levels") or {}))
    if old_l:
        bad.append(f"`levels.{'/'.join(old_l)}` — v6는 levels.evaluate 하나")
    if "fallback_order" in (raw.get("providers") or {}):
        bad.append("`providers.fallback_order` — codex 제외(v6 결정 1)")
    if bad:
        raise ConfigError("v5 설정 키 — v6로 옮겨야 함(설계서 v6 §6-3):\n- " + "\n- ".join(bad))


def load_config(path):
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"설정 파일 없음: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"설정 JSON 오류: {e}") from e
    check_removed(raw)
    cfg = _merge(DEFAULTS, raw)
    validate(cfg)
    cfg["_path"] = str(p.resolve())
    return cfg


def _num(v, low, strict=True):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and (v > low if strict else v >= low)


def validate(cfg):
    check_removed(cfg)  # DEFAULTS carry none of these keys — any one present came from the caller's config
    if not cfg.get("target"):
        raise ConfigError("target 누락")
    if "," in str(cfg["target"]):  # the builder's write rule names the target inside the comma-joined --allowedTools
        raise ConfigError("target 경로에 쉼표(,)를 쓸 수 없다 — 작업자 쓰기 허용 규칙이 쪼개진다")
    qa = cfg["qa"]
    if qa["runner"] not in VALID_RUNNERS:
        raise ConfigError(f"qa.runner 는 {sorted(VALID_RUNNERS)} 중 하나")
    if qa["environment"] not in VALID_ENVS:
        raise ConfigError(f"qa.environment 는 {sorted(VALID_ENVS)} 중 하나")
    if qa["runner"] == "command" and not qa.get("command"):
        raise ConfigError("qa.runner=command 이면 qa.command 필요")
    if qa["runner"] == "godot" and not qa.get("godot_exe"):
        raise ConfigError("qa.runner=godot 이면 qa.godot_exe 필요")
    if not isinstance(qa.get("max_scenarios"), int) or isinstance(qa.get("max_scenarios"), bool) or qa["max_scenarios"] < 1:
        raise ConfigError("qa.max_scenarios 는 1 이상의 정수")
    if qa["runner"] == "roblox":
        _fill_roblox(qa["roblox"])
        mw = qa["roblox"].get("max_wait_sec")
        if not _num(mw, 0) or not mw < qa["scenario_timeout_sec"]:
            raise ConfigError("qa.roblox.max_wait_sec 는 0보다 크고 scenario_timeout_sec 보다 작아야 함")
        rc = qa["roblox"].get("reset_code")
        if rc is not None and not (isinstance(rc, str) and rc.strip()):
            raise ConfigError("qa.roblox.reset_code 는 Server Luau 문자열(없으면 null)")
    if qa["runner"] == "web" and qa["environment"] != "managed" and not qa.get("base_url"):
        raise ConfigError("qa.runner=web 이면 qa.base_url 필요 (managed 는 managed.ready_url 사용)")
    if qa["environment"] == "managed" and qa["runner"] == "web":
        m = qa["managed"]
        if not m.get("start") or not m.get("ready_url"):
            raise ConfigError("qa.environment=managed 이면 managed.start / managed.ready_url 필요")
    for role in MODEL_ROLES:
        if role not in cfg["models"]:
            raise ConfigError(f"models.{role} 누락")
    unknown = sorted(set(cfg["models"]) - set(MODEL_ROLES))
    if unknown:  # a typo or an old role name must not be silently ignored (v6 B9)
        raise ConfigError(f"models 에 모르는 역할 {unknown} — v6 역할은 {list(MODEL_ROLES)}")
    check_levels(cfg["levels"])
    if any("--restricted" in str(a) for a in cfg["providers"].get("claude_args") or []):
        raise ConfigError("providers.claude_args 의 --restricted 는 쓰지 않는다 — 대상 레포의 CLAUDE.md·웹·MCP가 빠진다")
    try:
        capabilities.validate(cfg["capabilities"])
    except capabilities.CapabilityError as e:
        raise ConfigError(str(e)) from e
    lim = cfg["limits"]
    rb = lim.get("retry_backoff_sec")
    if not (isinstance(rb, list) and rb and all(_num(x, 0, strict=False) for x in rb)):
        raise ConfigError("limits.retry_backoff_sec 는 0 이상의 수 목록")
    for k in ("max_check_fails", "max_calls_per_run", "max_questions", "max_self_fix", "max_retries",
              "max_eval_rejects", "max_eval_runs", "no_progress_max", "rework_high_after"):
        v = lim.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ConfigError(f"limits.{k} 는 1 이상의 정수")
    for k, low in (("idle_timeout_sec", 10), ("call_total_sec", 60)):
        if not _num(lim.get(k), low, strict=False):
            raise ConfigError(f"limits.{k} 는 {low} 이상의 수(초)")
    if lim["call_total_sec"] < lim["idle_timeout_sec"]:
        raise ConfigError("limits.call_total_sec 는 idle_timeout_sec 보다 커야 함")
    v = lim.get("review_diff_chars")
    if not isinstance(v, int) or isinstance(v, bool) or v < 100:
        raise ConfigError("limits.review_diff_chars 는 100 이상의 정수")
    if not _num(lim.get("continue_window_sec"), 0, strict=False):
        raise ConfigError("limits.continue_window_sec 는 0 이상의 수")
    sc = cfg["self_check"]
    if not (isinstance(sc.get("commands"), list) and all(isinstance(c, str) and c.strip() for c in sc["commands"])):
        raise ConfigError("self_check.commands 는 명령 문자열 목록")
    if sc["commands"] and not any("{files}" in c for c in sc["commands"]):
        raise ConfigError("self_check.commands 중 하나는 `{files}`(검사할 파일)를 받아야 함")
    if sc["commands"] and " " in selfcheck.SCRIPT:
        raise ConfigError(f"자기 검사 스크립트 경로에 공백이 있음 — 작업자의 허용 명령 접두어가 깨진다: {selfcheck.SCRIPT}")
    for k in ("timeout_sec", "max_fixes"):
        if not isinstance(sc.get(k), int) or isinstance(sc.get(k), bool) or sc[k] < (1 if k == "timeout_sec" else 0):
            raise ConfigError(f"self_check.{k} 는 정수(timeout_sec ≥ 1 · max_fixes ≥ 0)")
    if not isinstance(sc.get("extensions"), list):
        raise ConfigError("self_check.extensions 는 목록")
    up = lim.get("usage_pause") or {}
    for k in ("five_hour", "seven_day", "per_call_default", "per_call_cap"):
        if not _num(up.get(k), 0) or up[k] > 1:
            raise ConfigError(f"limits.usage_pause.{k} 는 0보다 크고 1 이하")
    if not _num(up.get("fallback_min"), 0, strict=False):
        raise ConfigError("limits.usage_pause.fallback_min 은 0 이상의 수(분)")
    if not isinstance(cfg.get("contract_files"), list) or not all(isinstance(x, str) and x.strip()
                                                                   for x in cfg["contract_files"]):
        raise ConfigError("contract_files 는 경로 문자열 목록")
    hf = cfg.get("high_floor") or {}
    if set(hf) - {"visual", "high_risk_paths", "contract_files"} or not all(isinstance(v, bool) for v in hf.values()):
        raise ConfigError("high_floor 는 visual · high_risk_paths · contract_files 의 true/false")
    for k in ("role_definitions", "dev_shot", "wiki_create"):
        if not isinstance(cfg.get(k), bool):
            raise ConfigError(f"{k} 는 true/false")
    for k in ("heartbeat_sec", "stall_sec", "poll_sec", "restart_window_sec", "max_restarts"):
        if not _num(cfg["supervisor"].get(k), 0):
            raise ConfigError(f"supervisor.{k} 는 양수")
    if cfg["supervisor"]["stall_sec"] <= cfg["supervisor"]["heartbeat_sec"] * 2:
        raise ConfigError("supervisor.stall_sec 는 heartbeat_sec 의 2배보다 커야 함(정상 heartbeat를 멈춤으로 오판)")


def _fill_roblox(rb):
    from .studio_mcp import DEFAULT_COMMAND
    rb["mcp_command"] = rb.get("mcp_command") or DEFAULT_COMMAND
    rb["sourcemap_command"] = rb.get("sourcemap_command") or "rojo sourcemap default.project.json"
    if not isinstance(rb.get("play_ready_timeout_sec"), int) or rb["play_ready_timeout_sec"] < 1:
        raise ConfigError("qa.roblox.play_ready_timeout_sec 는 1 이상의 정수")


def check_levels(levels):
    ev = levels.get("evaluate") or {}
    for lv in ("1", "2", "3"):
        row = ev.get(lv)
        if not isinstance(row, dict):
            raise ConfigError(f"levels.evaluate.{lv} 누락")
        if row.get("effort") not in EFFORTS:
            raise ConfigError(f"levels.evaluate.{lv}.effort = {row.get('effort')!r} — {sorted(EFFORTS)} 중 하나")
        if not isinstance(row.get("second"), bool):
            raise ConfigError(f"levels.evaluate.{lv}.second 는 true/false")
