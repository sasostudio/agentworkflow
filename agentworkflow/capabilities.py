"""What a sub-agent may use beyond Read/Edit: web, MCP tools, plugins — chosen per run (09-24 decision).

Why this module exists (measured):
- `--restricted` blocks plugin side effects but ALSO removes the project's CLAUDE.md, web and MCP.
- Loading user settings brings user plugins back (one wrote its own memory file into the target and caused 3
  auto-rejects) AND merges the user's global allow rules into the engine's allowlist: an MCP tool NOT in
  --allowedTools was called under dontAsk.
- So: user settings are never loaded (`--setting-sources project,local`), hooks are off, bkit is off, the rule
  files come in via `--add-dir rules_root` (the working rules and role file also go into the system prompt), and
  web/MCP/plugins are granted per run from engine.json `capabilities`. Default = nothing extra.
"""

import json
import re
import subprocess

# v6 §6-1: the two AI roles. A v5 name here is refused by validate() (never silently ignored — A3)
ROLES = ("builder", "evaluator")
WEB_TOOLS = ["WebSearch", "WebFetch"]
REFUSED_PLUGINS = ("bkit",)  # run001: wrote docs/.bkit-memory.json into the target -> 3 auto-rejects
# Irreversible / paid actions are refused even if engine.json lists them. Matched on the tool's own name
# (after `mcp__server__`). Read-type tools (list/get/stats/…) pass.
_IRREVERSIBLE = re.compile(
    r"(submit|send|create|update|delete|remove|cancel|rollback|upload|push|publish|deploy|modify|change|"
    r"complete|execute|invoke|generate|upscale|dubbing|write|insert|edit|set(_|$)|^set|_set\b|refresh|save|"
    r"import|confirm|connect|participate|rename|select_workspace)", re.I)
_MCP_TOOL = re.compile(r"^mcp__(?P<server>[\w.-]+?)__(?P<tool>[\w.-]+)$")

DEFAULT = {"web_roles": [], "mcp_servers": [], "mcp_tools": {}, "plugins": []}


class CapabilityError(Exception):
    pass


def validate(cap):
    """Raise CapabilityError on anything unsafe or malformed. `cap` is engine.json `capabilities`."""
    bad_roles = [r for r in cap.get("web_roles", []) if r not in ROLES]
    if bad_roles:
        raise CapabilityError(f"capabilities.web_roles 에 모르는 역할: {bad_roles} (가능: {list(ROLES)})")
    servers = set(cap.get("mcp_servers", []))
    for role, tools in (cap.get("mcp_tools") or {}).items():
        if role not in ROLES:
            raise CapabilityError(f"capabilities.mcp_tools 에 모르는 역할: {role}")
        for t in tools:
            m = _MCP_TOOL.match(t)
            if not m:
                raise CapabilityError(f"MCP 도구 이름은 mcp__서버__도구 형식: {t}")
            if m.group("server") not in servers:
                raise CapabilityError(f"{t} — 서버 `{m.group('server')}` 가 capabilities.mcp_servers 에 없음")
            if _IRREVERSIBLE.search(m.group("tool")):
                raise CapabilityError(f"{t} — 되돌릴 수 없는/비용이 드는 도구는 엔진에서 허용하지 않음 (조회용만)")
    for p in cap.get("plugins", []):
        if any(w in p.lower() for w in REFUSED_PLUGINS):
            raise CapabilityError(f"plugins 에 {p} — bkit 은 대상 폴더에 파일을 써서 금지(run001)")


def extra_tools(cap, role):
    """Tools granted to `role` on top of its base permissions."""
    out = list(WEB_TOOLS) if role in cap.get("web_roles", []) else []
    return out + list((cap.get("mcp_tools") or {}).get(role, []))


def settings_json(cap):
    plugins = {p: True for p in cap.get("plugins", [])}
    plugins.setdefault("bkit@bkit-marketplace", False)  # belt and braces: user settings are not loaded anyway
    return json.dumps({"disableAllHooks": True, "enabledPlugins": plugins}, ensure_ascii=False)


def isolation_args(cap, rules_root, mcp_config_path):
    """CLI args every claude call gets (replaces the old `--restricted`)."""
    return ["--setting-sources", "project,local",
            "--settings", settings_json(cap),
            "--add-dir", str(rules_root),
            "--mcp-config", str(mcp_config_path), "--strict-mcp-config"]


def resolve_mcp(names, exe="claude"):
    """Build an --mcp-config dict from servers registered in Claude Code. Only the transport + URL/command
    are copied; OAuth tokens stay in Claude's own store (so nothing secret is written to the run folder).
    Servers that need environment variables are refused (their values could be secrets)."""
    servers = {}
    for name in names:
        r = subprocess.run([exe, "mcp", "get", name], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        out = r.stdout or ""
        if r.returncode != 0 or "Type:" not in out:
            raise CapabilityError(f"MCP 서버 `{name}` 를 찾지 못함 (claude mcp get 실패)")
        typ = re.search(r"Type:\s*(\S+)", out).group(1).lower()
        if re.search(r"^\s*Environment:", out, re.M):
            raise CapabilityError(f"MCP 서버 `{name}` 는 환경변수가 필요 — 값이 비밀일 수 있어 엔진에서 미지원")
        if typ in ("http", "sse"):
            servers[name] = {"type": typ, "url": re.search(r"URL:\s*(\S+)", out).group(1)}
        elif typ == "stdio":
            cmd = re.search(r"Command:\s*(.+)", out).group(1).strip()
            args = re.search(r"Args:\s*(.*)", out)
            servers[name] = {"type": "stdio", "command": cmd,
                             "args": args.group(1).split() if args and args.group(1).strip() else []}
        else:
            raise CapabilityError(f"MCP 서버 `{name}` 형식 `{typ}` 미지원")
    return {"mcpServers": servers}
