"""Minimal MCP stdio client for the Roblox Studio MCP server (no external package).

Used by the roblox QA runner (engine code), never exposed to AI roles — the `execute` tool filter in
capabilities.py stays as it is. Measured 09-24 (design §19): a second client connects fine while Studio runs;
execute_luau takes datamodel_type Edit|Client|Server; screen_capture returns base64 JPEG.

The server command lives outside the target (%LOCALAPPDATA%\\Roblox\\mcp.bat) — the engine only EXECUTES it,
never reads or writes files there.
"""

import json
import queue
import subprocess
import threading

DEFAULT_COMMAND = 'cmd.exe /c "cd /d %LOCALAPPDATA%\\Roblox && .\\mcp.bat"'


class StudioError(Exception):
    """Transport-level failure (server did not start, timed out, closed)."""


class StudioClient:
    def __init__(self, command=DEFAULT_COMMAND, timeout=60):
        self.command, self.timeout = command, timeout
        self.proc = None
        self._n = 0
        self._lines = queue.Queue()
        self._stderr = []

    # ---------- lifecycle ----------
    def __enter__(self):
        try:
            self.proc = subprocess.Popen(self.command, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
        except OSError as e:
            raise StudioError(f"MCP 서버 실행 실패: {e}") from e
        threading.Thread(target=self._pump, args=(self.proc.stdout, self._lines), daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()
        self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "agentworkflow-roblox-runner", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def __exit__(self, *a):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            # only the process tree this client started
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        if self.proc:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            for s in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    s.close()
                except (OSError, ValueError):
                    pass

    @staticmethod
    def _pump(stream, q):
        for line in stream:
            q.put(line)
        q.put(None)

    def _pump_err(self):
        for line in self.proc.stderr:
            self._stderr.append(line.decode("utf-8", "replace"))

    # ---------- JSON-RPC ----------
    def _send(self, obj):
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError as e:
            raise StudioError(f"MCP 서버에 쓰기 실패: {e}") from e

    def _request(self, method, params, timeout=None):
        self._n += 1
        mid = self._n
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        wait = timeout or self.timeout
        while True:
            try:
                line = self._lines.get(timeout=wait)
            except queue.Empty:
                raise StudioError(f"MCP 응답 시간 초과 {wait}s ({method})")
            if line is None:
                raise StudioError("MCP 서버 종료 — " + "".join(self._stderr[-5:])[-400:])
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise StudioError(f"MCP 오류({method}): {msg['error']}")
            return msg["result"]

    def call(self, tool, args=None, timeout=None):
        """-> (is_error, content list)."""
        res = self._request("tools/call", {"name": tool, "arguments": args or {}}, timeout)
        return bool(res.get("isError")), res.get("content") or []

    def text(self, tool, args=None, timeout=None):
        """Tool call whose content is text. Raises StudioError when the tool reports an error."""
        err, content = self.call(tool, args, timeout)
        txt = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        if err:
            raise ToolFailed(txt)
        return txt


class ToolFailed(Exception):
    """The tool ran and reported an error (e.g. Luau error, wrong datamodel)."""
