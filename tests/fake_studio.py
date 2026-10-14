"""Fake Roblox Studio MCP server (stdio JSON-RPC) for the roblox runner tests. AI calls = 0, Studio = none.

State (studios, mode, sources, failure switches) lives in a JSON file named by argv[1] so a test can set it up
and read what happened: every tool call is appended to state["calls"].
Markers in scenario code pick the behaviour: MARK_PASS / MARK_FAIL / MARK_ERROR / MARK_HANG / MARK_NOTABLE.
"""
import base64
import json
import re
import sys
import time

STATE = sys.argv[1]


def load():
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def save(s):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)


def _sig(src):  # same signature the runner computes in Luau: normalized length + polynomial hash
    b = src.replace("\r\n", "\n").rstrip("\n").encode("utf-8")
    h = 0
    for ch in b:
        h = (h * 31 + ch) % 1000000007
    return f"{len(b)}:{h}"


def text(t, err=False):
    return {"content": [{"type": "text", "text": t}], "isError": err}


def handle(name, a):
    s = load()
    s["calls"].append({"tool": name, **{k: v for k, v in a.items() if k != "code"},
                       "code_head": (a.get("code") or "")[:40]})
    save(s)
    if name == "list_roblox_studios":
        return text(json.dumps({"studios": s["studios"]}))
    if name == "get_studio_state":
        avail = "Edit" if s["mode"] == "Edit" else "Edit, Client, Server"
        return text(f"- Current Studio Mode: {s['mode']}\n- Available DataModels: {avail}\n")
    if name == "start_stop_play":
        if a["is_start"]:
            s["mode"] = "Play"
        elif not s.get("stop_fails"):
            s["mode"] = "Edit"
        save(s)
        return text("ok")
    if name == "execute_luau":
        code = a["code"]
        if a["datamodel_type"] != "Edit" and s["mode"] == "Edit":
            return text(f"{a['datamodel_type']} datamodel is not available in Edit mode", True)
        if "o.Source" in code and s.get("sync_lag_calls"):  # Rojo still pushing: stale for N more checks
            s["sync_lag_calls"] -= 1
            if s["sync_lag_calls"] == 0:
                s["sources"] = s["sources_after_lag"]
            save(s)
            s = load() if s["sync_lag_calls"] == 0 else {**s, "sources": s["sources_lagging"]}
        if "o.Source" in code:  # sync check: return sources (or their signatures) in the order of the requested paths
            paths = re.findall(r"\{((?:\"[^\"]*\"(?:, )?)+)\}", code)
            out = {}
            for i, p in enumerate(paths, 1):
                key = "/".join(json.loads("[" + p + "]"))
                src = s["sources"].get(key, False)
                out[str(i)] = _sig(src) if (src is not False and "sig(o.Source)" in code) else src
            # the real server answers "[Complex Table]" instead of JSON when the returned table is too big (09-26 F-10)
            if s.get("complex_table_over") and len(json.dumps(out)) > s["complex_table_over"]:
                return text("[Complex Table]")
            # like the real server: a Luau array comes back as {"1": .., "2": ..} (measured 09-24)
            return text(json.dumps(out, ensure_ascii=False))
        if "MARK_HANG" in code:
            time.sleep(30)
        if "MARK_ERROR" in code:
            return text("AssistantCommand:1: boom", True)
        if "MARK_NOTABLE" in code:
            return text(json.dumps({"status": "FAILED", "note": "시나리오가 {status, note} 테이블을 반환하지 않음"}))
        if "UNIT_OK" in code:
            return text(json.dumps({"status": "PASSED", "note": "run=5 passed=5 failed=0"}))
        if "UNIT_BAD" in code:
            return text(json.dumps({"status": "FAILED", "note": "run=6 passed=5 failed=1"}))
        if "MARK_FAIL" in code:
            return text(json.dumps({"status": "FAILED", "note": "기대와 다름"}))
        return text(json.dumps({"status": "PASSED", "note": "ok"}))
    if name == "screen_capture":
        return {"content": [{"type": "image", "mimeType": "image/jpeg",
                             "data": base64.b64encode(b"\xff\xd8\xff\xe0fakejpeg").decode()}], "isError": False}
    if name == "get_console_output":
        return text("console log")
    return text("unknown tool", True)


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    elif msg["method"] == "tools/call":
        res = handle(msg["params"]["name"], msg["params"].get("arguments") or {})
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}, ensure_ascii=False) + "\n")
    sys.stdout.flush()
