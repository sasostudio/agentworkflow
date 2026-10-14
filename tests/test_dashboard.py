"""엔진 대시보드 (10-03 · 엔진_대시보드_설계서 §6 · §8). E1~E3 records the engine writes · the stream parser · the
read-only server's panels and its guards. Fake AI (FakeAdapter) · real git repos · a real local HTTP server."""

import http.client
import json
import os
import sys
import threading
import time
import unittest

from agentworkflow import dashboard as dash, qa
from agentworkflow.adapters import ClaudeAdapter, FakeAdapter, tool_events
from agentworkflow.engine import Engine

import helpers as h

B, E, DV = "builder", "evaluator", "evaluator:devil"


def make(script, st=None):
    repo = h.make_repo()
    st = st or h.make_state(repo)
    fa = FakeAdapter(script)
    e = Engine(h.make_cfg(repo), h.make_request(1, None, h.NO_QA), st, fa, fa, sleep=lambda s: None,
               out=lambda m: None)
    return e, st


def events(st):
    return dash._jsonl(st.dir / "events.jsonl")


def read(p):
    return json.loads(p.read_text(encoding="utf-8"))


# ================= E1 · E2 · E3 (engine records) =================
class Records(unittest.TestCase):
    def test_e1_one_event_per_call_matching_raw(self):
        # the rebuttal's REJECT goes to the main evaluator (E2), which answers READY -> end gate
        e, st = make({B: [h.b_work(cost=0.4)], E: [h.e_ans("READY"), h.e_ans("READY")],
                      DV: [h.devil("REJECT", [("high", "x")])]})
        e.run()
        ev = events(st)
        raws = sorted(p.name for p in (st.dir / "raw").glob("*.response.json"))
        self.assertEqual(len(ev), len(raws))
        self.assertEqual([(x["key"], x["kind"]) for x in ev], [(B, "first"), (E, "E1"), (DV, "DEVIL"), (E, "E2")])
        self.assertEqual([x["verdict"] for x in ev], [None, "READY", "REJECT", "READY"])
        b = ev[0]
        self.assertEqual((b["kind"], b["model_set"], b["cost"], b["resumed"]),
                         ("first", e.models["builder"]["model"], 0.4, False))
        self.assertIn("effort", b)
        self.assertTrue(raws[0].startswith(f"{b['seq']:03d}_"))
        self.assertLessEqual(b["t0"], b["t1"])
        self.assertEqual(set(b["tokens"]), {"in", "cache_read", "cache_write", "out"})

    def test_e2_heartbeat_names_the_running_call_at_once(self):
        seen = []
        repo = h.make_repo()
        st = h.make_state(repo)

        def peek(kw):
            seen.append(read(st.dir / "heartbeat.json"))
            return h.b_work()(kw)
        e, _ = make({B: [peek], E: [h.e_ans("READY")], DV: [h.devil()]}, st=st)
        e.cfg["supervisor"]["heartbeat_sec"] = 3600  # the loop beats once at start, then only the call writes
        e.run()
        call = seen[0]["calls"][0]
        self.assertEqual((call["key"], call["kind"], call["model"]), (B, "first", e.models["builder"]["model"]))
        self.assertIn("effort", call)
        self.assertIsInstance(call["epoch"], float)
        self.assertEqual(read(st.dir / "heartbeat.json")["calls"], [])

    def test_e4_every_usage_reading_lands_at_once_and_e1_keeps_reset_times(self):
        seen = []
        repo = h.make_repo()
        st = h.make_state(repo)
        reset = int(time.time()) + 3600

        def reading(kw):  # the CLI sends readings during the call; the engine writes each one as it comes
            kw["on_rate"]({"five_hour": {"utilization": 0.31, "resetsAt": reset}}, first=False)
            seen.append(read(st.dir / "usage.json"))
            res = h.b_work()(kw)
            res.rate = {"five_hour": {"utilization": 0.33, "resetsAt": reset},
                        "seven_day": {"utilization": 0.5, "resetsAt": reset + 86400}}
            return res
        e, _ = make({B: [reading], E: [h.e_ans("READY")], DV: [h.devil()]}, st=st)
        e.run()
        self.assertEqual((seen[0]["five_hour"]["u"], seen[0]["five_hour"]["reset"]), (0.31, reset))
        ev = events(st)[0]
        self.assertEqual((ev["u5h"], ev["r5h"], ev["u7d"], ev["r7d"]), (0.33, reset, 0.5, reset + 86400))

    def test_e3_tool_uses_go_to_live_json_and_leave_with_the_call(self):
        seen = []
        repo = h.make_repo()
        st = h.make_state(repo)

        def tools(kw):
            kw["on_tool"]({"id": "t1", "name": "Read", "target": "hello.py"})
            seen.append(read(st.dir / "live.json"))
            kw["on_tool"]({"result_of": "t1", "error": False})
            seen.append(read(st.dir / "live.json"))
            return h.b_work()(kw)
        e, _ = make({B: [tools], E: [h.e_ans("READY")], DV: [h.devil()]}, st=st)
        e.run()
        c0 = seen[0]["calls"][0]
        self.assertEqual((c0["key"], c0["kind"], c0["tools"][0]["name"], c0["tools"][0]["target"]),
                         (B, "first", "Read", "hello.py"))
        self.assertNotIn("done", c0["tools"][0])
        self.assertTrue(seen[1]["calls"][0]["tools"][0]["done"])
        self.assertEqual(read(st.dir / "live.json")["calls"], [])


# ================= stream parser (format measured on run003 raw · 10-03) =================
class Stream(unittest.TestCase):
    USE = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Grep", "input": {"pattern": "id = \"", "path": "a.luau"}}]}})
    RES = json.dumps({"type": "user", "message": {"content": [
        {"tool_use_id": "toolu_1", "type": "tool_result", "content": "26: x", "is_error": True}]}})

    def test_tool_events(self):
        self.assertEqual(tool_events(self.USE), [{"id": "toolu_1", "name": "Grep", "target": "a.luau"}])
        self.assertEqual(tool_events(self.RES), [{"result_of": "toolu_1", "error": True}])
        self.assertEqual(tool_events(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "tool_use"}]}})), [])
        self.assertEqual(tool_events('{"type":"result","result":"tool_use"}'), [])

    def test_stream_passes_tool_lines_as_they_come(self):
        src = ("import sys\nsys.stdin.read()\n"
               f"print({self.USE!r},flush=True)\nprint({self.RES!r},flush=True)\n"
               "print('{\"type\":\"result\",\"result\":\"ok\",\"session_id\":\"S\"}',flush=True)\n")
        got = []
        r, stop, why, sid = ClaudeAdapter._stream([sys.executable, "-c", src], "hi", ".", 5, 30, None, None, None,
                                                  got.append)
        self.assertEqual([g.get("name") or g.get("result_of") for g in got], ["Grep", "toolu_1"])
        self.assertEqual(len(r.stdout.splitlines()), 3)  # the lines are still kept for the result


# ================= server panels =================
class Panels(unittest.TestCase):
    def test_timeline_calls_in_v6_v5_and_older_formats(self):
        lines = [(1000.0, "", "호출 `builder`·first claude-sonnet-5-5/high · $1.1197 · 캐시 적중 91% · 소요 145초 · "
                              "새 세션 · 5h 39% · 7d 43%"),
                 (1100.0, "", "호출 `evaluator`·E1 claude-opus-5-5/high → 실제 **claude-haiku-4-5** ⚠️설정과 다름 · "
                              "비용 미상 · 소요 20초 · 이어쓰기"),
                 (1100.0, "", "판정 **REJECT** (E1) — 이유"),
                 (1200.0, "", "호출 `developer` claude-haiku-4-5-20251001/high · $0.1678 · 캐시 적중 64% · 소요 49초 · "
                              "5h 68% · 7d 98%"),
                 (1300.0, "", "호출 `seed` haiku/low · $0.0128 · 캐시 적중 81%")]
        c = dash.calls_from_timeline(lines, 4)
        self.assertEqual([x["seq"] for x in c], [1, 2, 3, 4])
        self.assertEqual((c[0]["t0"], c[0]["cost"], c[0]["cache_hit"], c[0]["u5h"], c[0]["resumed"]),
                         (855.0, 1.1197, 0.91, 0.39, False))
        self.assertEqual((c[1]["model"], c[1]["model_set"], c[1]["cost"], c[1]["verdict"], c[1]["resumed"]),
                         ("claude-haiku-4-5", "claude-opus-5-5", None, "REJECT", True))
        self.assertEqual((c[2]["kind"], c[2]["u7d"], c[2]["resumed"]), ("developer", 0.98, None))
        self.assertEqual((c[3]["sec"], c[3]["t0"]), (None, 1300.0))
        self.assertIsNone(dash.calls_from_timeline(lines, 5)[0]["seq"])  # counts differ -> raw link unknown

    def test_work_rate_positions(self):
        self.assertEqual(dash.work_rate("PLANNING", "RUNNING", None, 0, 2), 5)
        self.assertEqual(dash.work_rate("BUILD", "RUNNING", None, 1, 2), 30)
        self.assertEqual(dash.work_rate("EVALUATE", "RUNNING", {"phase": "MAIN"}, 2, 2), 55)
        self.assertEqual(dash.work_rate("EVALUATE", "RUNNING", {"phase": "MAIN", "looked": True}, 2, 2), 75)
        self.assertEqual(dash.work_rate("EVALUATE", "AWAITING_APPROVAL", None, 2, 2), 95)
        self.assertEqual(dash.work_rate("COMPLETE", "COMPLETE", None, 2, 2), 100)

    def test_build_fills_by_the_target_files_the_running_call_changed(self):
        """The rate used to stand at one value for a whole builder call (items are checked only when it ends)."""
        repo = h.make_repo()
        st = h.make_state(repo)
        req = h.make_request(1, [h.item(target="a.py, b.py")], h.NO_QA)
        (repo / "b.py").write_text("old\n", encoding="utf-8")             # there before this BUILD: not counted
        old = time.time() - 120
        os.utime(repo / "b.py", (old, old))
        seen = []

        def build(kw):
            kw["on_init"]("SB")
            (repo / "a.py").write_text("x = 1\n", encoding="utf-8")       # half of the item's files so far
            seen.append(dash.run_payload(st.dir)["progress"])
            return h.b_work(files={"a.py": "x = 1\n", "b.py": "y = 2\n"})(kw)
        fa = FakeAdapter({B: [build], E: [h.e_ans("READY")], DV: [h.devil()]})
        Engine(h.make_cfg(repo), req, st, fa, fa, sleep=lambda s: None, out=lambda m: None).run()
        p = seen[0]
        self.assertEqual(p["rate"], 5.0)                                   # the position itself is unchanged
        self.assertEqual(p["fill"], {"next": 55.0, "kind": "files", "done": 1, "total": 2})

    def test_evaluate_fills_by_the_running_calls_estimate_from_recent_runs(self):
        """The first evaluator call of a run has no finished call of its kind in this run — recent runs give one."""
        repo = h.make_repo()
        st = h.make_state(repo)
        other = st.dir.parent / "run999_dash-typical-source"
        other.mkdir(exist_ok=True)
        (other / "events.jsonl").write_text(json.dumps({"key": E, "kind": "E1", "sec": 120.0}) + "\n",
                                            encoding="utf-8")
        seen = []

        def evaluate(kw):
            seen.append(dash.run_payload(st.dir))
            return h.e_ans("READY")(kw)
        e, _ = make({B: [h.b_work()], E: [evaluate], DV: [h.devil()]}, st=st)
        e.run()
        p = seen[0]
        self.assertEqual((p["progress"]["rate"], p["progress"]["fill"]["kind"], p["progress"]["fill"]["key"]),
                         (55.0, "estimate", E))
        call = next(c for c in p["now_calls"]["calls"] if c["key"] == E)
        self.assertEqual(call["typical_src"], "recent")
        self.assertIsNotNone(call["estimate"])

    def test_build_fill_holds_between_calls_and_skips_what_names_no_file(self):
        """Review 10-09: counted from the call's start, the rate dropped back when a call ended. A glob / folder /
        absolute / `..` target names no one file: left out — with nothing left, the builder call's estimate instead."""
        repo = h.make_repo()
        (repo / "src").mkdir(exist_ok=True)
        (repo / "a.py").write_text("x\n", encoding="utf-8")
        st = {"stage": "BUILD", "status": "RUNNING", "target": str(repo)}
        since = time.time() - 60
        items = [{"checked": False, "target_files": ["a.py", "b.py", "src/*.py", "src", "../x.py", "C:/abs.py"]}]
        self.assertEqual(dash.rate_fill(st, items, [], None, since),                       # no call running
                         {"next": 55.0, "kind": "files", "done": 1, "total": 2})
        only_odd = [{"checked": False, "target_files": ["src/*.py", "src"]}]
        self.assertEqual(dash.rate_fill(st, only_odd, [{"key": B, "kind": "first", "epoch": since}], None, since),
                         {"next": 55.0, "kind": "estimate", "key": B, "call_kind": "first"})
        self.assertIsNone(dash.rate_fill(st, only_odd, [], None, since))

    def test_typical_is_this_runs_median_else_other_runs_never_its_own(self):
        root = h.ROOT / f"typical_{time.time_ns()}" / "ai-log"
        me, other = root / "2026-10-09" / "run001_me", root / "2026-10-09" / "run002_other"
        for d, sec in ((me, 999.0), (other, 120.0)):
            d.mkdir(parents=True)
            (d / "events.jsonl").write_text(json.dumps({"key": E, "kind": "E1", "sec": sec}) + "\nnot json\n[1]\n",
                                            encoding="utf-8")
        now = time.time()
        self.assertEqual(dash._typical([], E, "E1", me, now), (120.0, "recent"))   # its own file is not "recent"
        self.assertEqual(dash._typical([{"key": E, "kind": "E1", "sec": 30.0}], E, "E1", me, now), (30.0, "run"))
        self.assertEqual(dash._typical([], B, "first", me, now), (None, None))

    def test_runner_step_fills_by_scenarios_ended_and_a_stop_fills_nothing(self):
        st = {"stage": "EVALUATE", "status": "RUNNING", "eval": {"phase": "RUN"}}
        self.assertEqual(dash.rate_fill(st, [], [], {"done": 3, "total": 4}),
                         {"next": 75.0, "kind": "runner", "done": 3, "total": 4})
        self.assertIsNone(dash.rate_fill({**st, "status": "HELP"}, [], [], {"done": 3, "total": 4}))
        self.assertIsNone(dash.rate_fill({"stage": "BUILD", "status": "RUNNING"}, [], [], None))  # no call running

    def test_payload_of_an_engine_run(self):
        e, st = make({B: [h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]})
        e.run()
        p = dash.run_payload(st.dir)
        self.assertEqual(p["source"], "events")
        self.assertEqual(p["status"]["status"], "AWAITING_APPROVAL")
        self.assertIn("approve", p["status"]["todo"])
        self.assertEqual((p["progress"]["rate"], p["progress"]["items"]["checked"]), (95.0, 1))
        self.assertEqual((p["flow"]["current_top"], p["flow"]["top"]["GATE"]), ("GATE", "wait"))
        bars = [b for b in p["calls"]["bars"] if b["key"] != "runner"]
        self.assertEqual([b["key"] for b in bars], [B, E, DV])
        self.assertTrue(all(b["raw"] and (st.dir / b["raw"]).is_file() for b in bars))
        self.assertEqual(p["now_calls"]["calls"], [])
        self.assertEqual([x["type"] for x in p["log"] if x["text"].startswith("호출")], ["call"] * 3)

    def test_stalled_running_run_says_so(self):
        e, st = make({})
        st.d["status"] = "RUNNING"
        st.save()
        (st.dir / "heartbeat.json").write_text(json.dumps({"epoch": time.time() - 999, "calls": [
            {"key": B, "kind": "first", "epoch": time.time() - 999}]}), encoding="utf-8")
        p = dash.run_payload(st.dir)
        self.assertEqual(p["source"], "none")  # no call ended yet — not shown as an old (pre-E1) run
        self.assertTrue(p["status"]["stalled"])
        self.assertIn("resume", p["status"]["todo"])
        self.assertEqual(p["now_calls"]["calls"], [])  # a dead process's last call is not shown as running

    def test_account_usage_is_the_newest_reading_of_any_run(self):
        root = h.ROOT / f"usage_{time.time_ns()}"
        now = time.time()
        for name, t, u in (("run001_a", now - 600, 0.2), ("run002_b", now - 60, 0.7)):
            rd = root / "2026-10-04" / name
            rd.mkdir(parents=True)
            (rd / "state.json").write_text("{}", encoding="utf-8")
            (rd / "usage.json").write_text(json.dumps({"t": t, "five_hour": {"u": u, "reset": now + 99}}),
                                           encoding="utf-8")
        u = dash.account_usage(root, now)
        self.assertEqual((u["run"], u["u5h"], u["r5h"]), ("2026-10-04/run002_b", 0.7, now + 99))
        self.assertIsNone(dash.account_usage(h.ROOT / "no_runs_here", now))
        self.assertNotIn("usage", dash.p_status({"status": "COMPLETE"}, None, now, 120))  # one source: account_usage

    def test_log_types_do_not_take_a_note_for_a_reject(self):
        tl = [(0, "2026-10-03 15:08:15", "Master 승인 — 직전 … (자동반려·기계검사·무진전 카운터 초기화)"),
              (0, "2026-10-03 15:08:16", "**자동반려** (1/3) — 엔진 규칙 위반"),
              (0, "2026-10-03 15:08:17", "Master 반려 — 직전 HELP")]
        self.assertEqual([x["type"] for x in dash.p_log(tl)], ["other", "reject", "reject"])

    def test_failures_text_has_no_classification_line(self):
        self.assertNotIn("QA 분류", qa.failures_text([{"id": "S1", "kind": "normal", "title": "t", "status": "FAILED",
                                                       "note": "n", "class": "X"}]))


# ================= HTTP: 127.0.0.1 · Host check · files only inside the run =================
class Http(unittest.TestCase):
    def test_guards(self):
        e, st = make({B: [h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]})
        e.run()
        ai_log = st.dir.parent.parent
        rid = f"{st.dir.parent.name}/{st.dir.name}"
        srv = dash.Server(("127.0.0.1", 0), dash.make_handler(ai_log, ai_log, 0))
        port = srv.server_address[1]
        srv.RequestHandlerClass = dash.make_handler(ai_log, ai_log, port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def get(path, host=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
            r = c.getresponse()
            return r.status, r.read()
        try:
            self.assertEqual(get("/")[0], 200)
            self.assertEqual(get("/api/runs", host="evil.example")[0], 403)
            _, body = get("/api/runs")
            self.assertIn(rid, [r["id"] for r in json.loads(body)])
            self.assertEqual(get(f"/api/run?id={rid}")[0], 200)
            self.assertEqual(get("/api/usage")[0], 200)
            self.assertEqual(get("/api/run?id=../x")[0], 404)
            self.assertEqual(get(f"/api/file?run={rid}&path=state.json")[0], 200)
            (ai_log.parent / "outside.txt").write_text("secret", encoding="utf-8")  # a real file the run must not serve
            for bad in ("../../../outside.txt", "..%2F..%2F..%2Foutside.txt", str(ai_log.parent / "outside.txt"),
                        "C:/Windows/win.ini", "/etc/passwd"):
                self.assertEqual(get(f"/api/file?run={rid}&path={bad}")[0], 404, bad)
        finally:
            srv.shutdown()
            srv.server_close()


# ================= the local server's port =================
def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Port(unittest.TestCase):
    def test_two_dashboards_cannot_share_a_port(self):
        port = free_port()
        with dash.Server(("127.0.0.1", port), dash.make_handler(h.ROOT, h.ROOT, port)):
            with self.assertRaises(OSError):  # Windows SO_REUSEADDR would let this bind (allow_reuse_address)
                dash.Server(("127.0.0.1", port), dash.make_handler(h.ROOT, h.ROOT, port))

if __name__ == "__main__":
    unittest.main()
