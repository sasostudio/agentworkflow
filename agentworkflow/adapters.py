"""Provider adapters. Same interface for every provider so codex can be dropped in later (I-9).

run_agent(...) -> Result. Raises LimitError when the provider says the usage limit is hit.
"""

import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

NON_INTERACTIVE_NOTE = (
    "\n\n[엔진 자동 실행] 이 호출은 AgentWorkflow 엔진이 비대화로 실행한다. 사용자에게 질문하지 말고, "
    "모드 표시·인사·요약 장식 없이 지시된 산출물만 만든다. 확인하지 않은 것을 확인한 것처럼 쓰지 않는다.\n"
    "대상 레포의 CLAUDE.md 등 다른 지침 가운데 **사람과의 대화 전용 규칙**(모드·단계 표시·타임스탬프·호칭·말투·"
    "명령어 안내·문서 자동 기록·작업 로그)과 **대화형 절차**(사람에게 묻는 질문 게이트·서브에이전트 호출·여러 라운드 "
    "검수 루프·보고 형식)는 이 호출에 적용하지 않는다 — 그 일은 Master가 run 앞뒤에서 한다. **작업 규칙(확인 전 단정 "
    "금지 R1~R12·문제 해결 원칙·작업 범위·삭제 금지·기능 위치 기준)과 판단 기준·체크리스트(작성 게이트 W1~W4 · 코드 "
    "검수 8항목 · A~H)**는 그대로 적용한다. "
    "방향이 갈려 추측이 필요하면 추측하지 말고 결과 JSON의 `question` 필드로 묻는다(엔진이 Master에게 전달).\n"
)


@dataclass
class Result:
    text: str = ""
    structured: dict = None
    is_error: bool = False
    denials: list = field(default_factory=list)
    cost: float = 0.0
    usage: dict = field(default_factory=dict)
    session_id: str = None
    raw: dict = field(default_factory=dict)
    argv: list = field(default_factory=list)

    model_usage: dict = field(default_factory=dict)
    # D' (09-29): the account's usage windows from the CLI's rate_limit_event — {"five_hour": {"utilization": 0.47,
    # "resetsAt": epoch}, "seven_day": {...}}. Empty when the provider does not report them (fake · older CLI).
    rate: dict = field(default_factory=dict)
    # v6 S-7: why the engine cut the call ("무응답 N초" / "총 상한 N초") — None when it ended by itself
    timed_out: str = None
    # v6 S-5: the CLI could not find the session to resume (`No conversation found` — e.g. the 30-day retention)
    no_session: bool = False
    # v6 H3: False when the CLI printed no result line (cut · crashed) — the call's cost is unknown
    has_result: bool = True

    @property
    def main_model(self):
        """The model that did the work. The CLI also makes small side calls (haiku) — measured 09-23 —
        so pick the entry with the most output tokens, preferring non-haiku."""
        mu = self.model_usage or {}
        if not mu:
            return None
        ranked = sorted(mu.items(), key=lambda kv: ("haiku" in kv[0], -(kv[1] or {}).get("outputTokens", 0)))
        return ranked[0][0]

    @property
    def cache_hit_ratio(self):
        u = self.usage or {}
        read = u.get("cache_read_input_tokens", 0) or 0
        total = read + (u.get("cache_creation_input_tokens", 0) or 0) + (u.get("input_tokens", 0) or 0)
        return (read / total) if total else None


class LimitError(Exception):
    def __init__(self, message, reset_at=None):
        super().__init__(message)
        self.reset_at = reset_at  # datetime or None


# 09-27~29 real texts: "You've hit your session limit · resets 12am" · "You've hit your weekly limit · resets Sep 29, 4pm"
_LIMIT_HINT = re.compile(r"(usage limit|rate limit|limit reached|limit will reset|resets? at|too many requests|429|"
                         r"hit your (?:session |weekly |usage )?limit)", re.I)
_RESET_AMPM = re.compile(r"reset[s]?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
_RESET_24 = re.compile(r"reset[s]?\s*(?:at\s*)?(\d{1,2}):(\d{2})", re.I)
_RESET_EPOCH = re.compile(r"\|(\d{10})\b")
# 09-28/29 (#6): the weekly limit says "resets Sep 29, 4pm (Asia/Seoul)" — a date before the time
_RESET_DATED = re.compile(r"reset[s]?\s*(?:at\s*|on\s*)?([A-Z][a-z]{2})\s+(\d{1,2}),?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
                          re.I)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), 1)}


def parse_reset(text, now=None):
    now = now or dt.datetime.now()
    m = _RESET_EPOCH.search(text or "")
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1)))
    m = _RESET_DATED.search(text or "")
    if m and m.group(1).lower() in _MONTHS:
        h, mi = int(m.group(3)) % 12, int(m.group(4) or 0)
        if m.group(5).lower() == "pm":
            h += 12
        try:
            t = now.replace(month=_MONTHS[m.group(1).lower()], day=int(m.group(2)), hour=h, minute=mi, second=0,
                            microsecond=0)
        except ValueError:
            return None
        return t if t > now else t.replace(year=t.year + 1)
    h = mi = None
    m = _RESET_AMPM.search(text or "")
    if m:
        h, mi = int(m.group(1)) % 12, int(m.group(2) or 0)
        if m.group(3).lower() == "pm":
            h += 12
    else:
        m = _RESET_24.search(text or "")
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
    if h is None or h > 23 or mi > 59:
        return None
    t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return t


def looks_like_limit(res_json, text):
    if res_json.get("api_error_status") == 429:
        return True
    return bool(res_json.get("is_error")) and bool(_LIMIT_HINT.search(text or ""))


class ClaudeAdapter:
    name = "claude"

    def __init__(self, exe="claude", extra_args=None, isolation=None):
        self.exe = exe
        # isolation = capabilities.isolation_args(...). Not `--restricted`: it also drops the project's CLAUDE.md,
        # web and MCP. Now: user settings are never loaded (their global allow rules leaked MCP tools past
        # --allowedTools — measured), hooks off, plugins off unless granted, the rule files via --add-dir (the working
        # rules and role file also go into the system prompt), web/MCP/plugins only as granted per run.
        self.extra_args = list(extra_args or [])
        self.isolation = list(isolation or [])

    def build_argv(self, *, model, effort, allowed_tools, disallowed_tools, permission_mode, add_dirs,
                   schema, resume=None, fork=False, persist=False, append_system=None, partial=True):
        # stream-json (+ --verbose, which it requires): the same result object as the last line, plus a
        # rate_limit_event with the account's usage windows (measured 09-28/29) — json output has no usage at all
        # v6 D1·T9 (10-02): --include-partial-messages keeps lines coming while the model writes (longest silence
        # 3.8 s measured) — the idle watch needs that; _stream does not keep those lines
        argv = [self.exe, "-p", *self.isolation, *self.extra_args, "--output-format", "stream-json", "--verbose",
                *(["--include-partial-messages"] if partial else []), "--model", model]
        if effort:
            argv += ["--effort", effort]
        if permission_mode:
            argv += ["--permission-mode", permission_mode]
        if allowed_tools is not None:
            argv += ["--allowedTools", ",".join(allowed_tools)]
        if disallowed_tools:
            argv += ["--disallowedTools", ",".join(disallowed_tools)]
        for d in add_dirs or []:
            argv += ["--add-dir", str(d)]
        if schema:
            argv += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        if resume:
            argv += ["--resume", resume]
            if fork:
                argv += ["--fork-session"]
        if not persist:
            argv += ["--no-session-persistence"]
        argv += ["--append-system-prompt", (append_system or "") + NON_INTERACTIVE_NOTE]
        return argv

    @staticmethod
    def _kill_tree(p):
        """Cut a call with everything it started. Popen.kill() on Windows ends the CLI only — its children (a test,
        a build) live on and hold the pipes, so the stop itself waited for them (measured 10-06 · run002)."""
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        if p.poll() is None:
            p.kill()

    @staticmethod
    def _stream(argv, prompt, cwd, idle_timeout, total_timeout, env, on_rate, on_init=None, on_tool=None):
        """Run the CLI reading stdout as it comes. -> (CompletedProcess | None when stopped/cut, stop exception | None,
        why-cut | None, session id from the init line | None).
        v6 S-7: a watcher thread cuts the call after `idle_timeout` s without a line or `total_timeout` s in all (the
        reading loop blocks on the next line, so it cannot time anything itself — C4).
        v6 S-3: the init line's session id goes to on_init at once — a call cut later still leaves its id.
        Partial-message lines (`stream_event`) only refresh the clock; they are not kept (T9: ~20x the output).
        Dashboard E3: each tool use (an `assistant` line) and tool result (a `user` line) goes to on_tool as it comes."""
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             cwd=str(cwd), text=True, encoding="utf-8", errors="replace",
                             env={**os.environ, **env} if env else None)
        err, cut, last = [], [], [time.monotonic()]
        begun = last[0]
        done = threading.Event()

        def feed():
            try:
                p.stdin.write(prompt)
                p.stdin.close()
            except (OSError, ValueError):
                pass  # the process ended (or was stopped) before reading all of it

        def watch():
            while not done.wait(1.0):
                now = time.monotonic()
                why = (f"무응답 {idle_timeout:.0f}초" if now - last[0] > idle_timeout else
                       f"총 상한 {total_timeout:.0f}초" if now - begun > total_timeout else None)
                if why:
                    cut.append(why)
                    ClaudeAdapter._kill_tree(p)
                    return

        side = [threading.Thread(target=feed, daemon=True),
                threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True),
                threading.Thread(target=watch, daemon=True)]
        for th in side:
            th.start()
        lines, stop, n_rate, sid = [], None, 0, None
        try:
            for line in p.stdout:
                last[0] = time.monotonic()
                if '"stream_event"' in line[:40]:
                    continue
                lines.append(line)
                if on_tool:
                    for t in tool_events(line):
                        on_tool(t)
                if sid is None and '"init"' in line and '"session_id"' in line:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        ev = None
                    if isinstance(ev, dict) and ev.get("subtype") == "init" and ev.get("session_id"):
                        sid = ev["session_id"]
                        if on_init:
                            on_init(sid)
                if on_rate and stop is None and '"rate_limit_event"' in line:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # v5 (10-01): EVERY reading goes to the engine — they come about every +1% during a call (bundle 3
                    # measured up to 7 in one call). first=True only for the reading that arrives before any work.
                    n_rate += 1
                    stop = on_rate(_rate_of(ev)[0], first=n_rate == 1) if isinstance(ev, dict) else None
                    if stop is not None:
                        ClaudeAdapter._kill_tree(p)
                        break
            p.wait()
        finally:
            done.set()
        for th in side[:2]:
            th.join(5)
        for f in (p.stdout, p.stderr):
            f.close()
        if stop is not None:
            return None, stop, None, sid
        if cut:
            return None, None, cut[0], sid
        return (subprocess.CompletedProcess(argv, p.returncode, stdout="".join(lines), stderr="".join(err)),
                None, None, sid)

    def run_agent(self, *, prompt, cwd, model, effort=None, allowed_tools=None, disallowed_tools=None,
                  permission_mode="dontAsk", add_dirs=None, schema=None, resume=None, fork=False,
                  persist=False, idle_timeout=300, total_timeout=10800, append_system=None, role=None, env=None,
                  on_rate=None, on_init=None, on_tool=None):
        """on_rate(rate, first) -> Exception|None: called for every usage reading of this call (the first arrives ~2 s
        in, before any work — measured 09-30 · later ones about every +1% — measured 10-01). An exception stops the call
        there (process tree killed) and is raised — the engine returns one at its usage thresholds, also mid-call.
        on_init(session_id): called as soon as the CLI names the session (v6 S-3 — before any work).
        on_tool(event): every tool use / tool result as it happens (dashboard E3 — tool_events)."""
        argv = self.build_argv(model=model, effort=effort, allowed_tools=allowed_tools,
                               disallowed_tools=disallowed_tools, permission_mode=permission_mode,
                               add_dirs=add_dirs, schema=schema, resume=resume, fork=fork, persist=persist,
                               append_system=append_system)
        r, stop, why, sid = self._stream(argv, prompt, cwd, idle_timeout, total_timeout, env, on_rate, on_init,
                                         on_tool)
        if stop is not None:
            raise stop
        if r is None:
            return Result(text=f"호출 끊음 — {why}", is_error=True, argv=argv, timed_out=why, session_id=sid,
                          has_result=False)
        out = (r.stdout or "").strip()
        j, rate, rejected_at = {}, {}, None
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "rate_limit_event":
                rate, rej = _rate_of(ev)
                rejected_at = rej or rejected_at
            elif ev.get("type") == "result" or ("result" in ev and "session_id" in ev):
                j = ev
        text = j.get("result") or (r.stderr or "") or out[-2000:]
        # the rate event saying "rejected" on an error result is a limit too (the text is only a second opinion)
        if looks_like_limit(j, text + (r.stderr or "")) or (rejected_at and j.get("is_error")):
            raise LimitError(text[:500], rejected_at or parse_reset(text + " " + (r.stderr or "")))
        no_session = "No conversation found" in f"{text}{r.stderr or ''}"
        return Result(
            text=text if isinstance(text, str) else json.dumps(text, ensure_ascii=False),
            structured=j.get("structured_output"),
            is_error=bool(j.get("is_error")) or (not j) or r.returncode != 0 or no_session, no_session=no_session,
            has_result=bool(j),
            denials=j.get("permission_denials") or [],
            cost=float(j.get("total_cost_usd") or 0.0),
            usage=j.get("usage") or {},
            session_id=j.get("session_id") or sid,
            model_usage=j.get("modelUsage") or {},
            raw={"stdout": out[-200000:], "stderr": (r.stderr or "")[-20000:], "returncode": r.returncode},
            argv=argv, rate=rate,
        )


# which input field names what a tool works on — the first one present is shown (dashboard E3)
_TOOL_TARGET = ("file_path", "notebook_path", "path", "pattern", "command", "url", "query", "description", "prompt")


def tool_events(line):
    """One stream-json line -> its tool events (format measured 10-03 on run003 raw: an `assistant` line carries
    content[{type: tool_use, id, name, input}] · a `user` line carries content[{type: tool_result, tool_use_id,
    is_error}]). -> [{"id", "name", "target"}] / [{"result_of", "error"}] · [] for any other line."""
    head = line[:40]
    if not (('"assistant"' in head and '"tool_use"' in line) or ('"user"' in head and '"tool_result"' in line)):
        return []
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return []
    content = ((ev.get("message") or {}).get("content") if isinstance(ev, dict) else None) or []
    out = []
    for c in content if isinstance(content, list) else []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "tool_use":
            inp = c.get("input") if isinstance(c.get("input"), dict) else {}
            target = next((str(inp[k]) for k in _TOOL_TARGET if inp.get(k)), "")
            out.append({"id": c.get("id"), "name": c.get("name"), "target": " ".join(target.split())[:160]})
        elif c.get("type") == "tool_result":
            out.append({"result_of": c.get("tool_use_id"), "error": bool(c.get("is_error"))})
    return out


def _rate_of(ev):
    """A rate_limit_event -> ({window: {utilization, resetsAt}}, the reset as a datetime when status is rejected)."""
    info = ev.get("rate_limit_info") or {}
    rate = {w: {"utilization": float(v.get("utilization") or 0), "resetsAt": v.get("resetsAt")}
            for w, v in (info.get("unifiedWindows") or {}).items() if isinstance(v, dict)}
    rej = None
    if info.get("status") == "rejected" and info.get("resetsAt"):
        rej = dt.datetime.fromtimestamp(int(info["resetsAt"]))
    return rate, rej


class ScriptExhausted(BaseException):
    """A test script ran out. BaseException on purpose: the engine's v4 top-level handler turns every Exception into
    HELP, which would let a test that runs past its script "pass" by accident (found in stage 2, 09-26)."""


class FakeAdapter:
    """Scripted adapter for tests: AI calls = 0.

    script: list of callables(call_dict) -> Result|Exception, consumed in order;
    or a dict role -> list of callables.
    """
    name = "fake"

    def __init__(self, script):
        self.script = script
        self.calls = []
        self._lock = threading.Lock()  # §23: parallel calls pop the script concurrently

    def run_agent(self, **kw):
        role = kw.get("role")
        with self._lock:  # only the bookkeeping is locked — fn runs outside so parallel fakes can meet (Barrier)
            self.calls.append(kw)
            if isinstance(self.script, dict):
                queue = self.script.get(role)
                if not queue:
                    raise ScriptExhausted(f"FakeAdapter: {role} 스크립트 소진")
                fn = queue.pop(0)
            else:
                if not self.script:
                    raise ScriptExhausted("FakeAdapter: 스크립트 소진")
                fn = self.script.pop(0)
        out = fn(kw)
        if isinstance(out, Exception):
            raise out
        out.argv = out.argv or ["fake", role or "?"]
        return out


def claude_version(exe="claude"):
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        return (r.stdout or r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(확인 실패: {e})"


def make_adapter(name, cfg=None, mcp_config_path=None):
    """mcp_config_path: the run's 00-request/mcp.json (written by the CLI from capabilities.mcp_servers)."""
    if name == "claude":
        from . import capabilities
        if not mcp_config_path:  # never run a claude call without isolation (bkit / user allow rules)
            raise ValueError("claude 어댑터는 mcp_config_path(격리 설정)가 필요")
        cfg = cfg or {}
        iso = capabilities.isolation_args(cfg["capabilities"], cfg["rules_root"], mcp_config_path)
        return ClaudeAdapter(extra_args=(cfg.get("providers") or {}).get("claude_args"), isolation=iso)
    if name == "codex":
        raise NotImplementedError("codex 어댑터는 I-9 (Codex 설치·결제 후)")
    raise ValueError(f"알 수 없는 provider: {name}")
