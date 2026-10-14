"""엔진 대시보드 V2 (10-04 · 설계서 §10): the uploader and the site build. A fake Supabase (REST + Storage) runs on
127.0.0.1 — nothing leaves the PC, no window opens, no real uploader process is started."""

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from agentworkflow import dashboard_site, uploader
from agentworkflow.adapters import FakeAdapter
from agentworkflow.engine import Engine

import helpers as h

B, E, DV = "builder", "evaluator", "evaluator:devil"
SECRET = "sb_secret_TESTONLY_never_publish"


class FakeSupabase:
    """Just the calls uploader.Client makes. fail=True -> every call answers 500."""

    def __init__(self):
        self.rows, self.objects, self.fail, self.uploads = {}, {}, False, 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body=b""):
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _route(self, method):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if fake.fail:
                    return self._reply(500, {"message": "down"})
                if self.headers.get("apikey") != SECRET:
                    return self._reply(401, {"message": "key"})
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path == "/rest/v1/aw_runs" and method == "POST":
                    row = json.loads(body)
                    fake.rows[row["id"]] = {**fake.rows.get(row["id"], {}), **row}
                    return self._reply(201)
                if u.path == "/rest/v1/aw_runs" and method == "GET":
                    lt = q["updated_at"][0][3:]
                    return self._reply(200, [{"id": r["id"]} for r in fake.rows.values() if r["updated_at"] < lt])
                if u.path == "/rest/v1/aw_runs" and method == "DELETE":
                    fake.rows.pop(unquote(q["id"][0][3:]), None)
                    return self._reply(204)
                pre = "/storage/v1/object/aw-files/"
                if u.path.startswith(pre) and method == "POST":
                    fake.objects[u.path[len(pre):]] = body
                    fake.uploads += 1
                    return self._reply(200, {"Key": u.path})
                if u.path == "/storage/v1/object/list/aw-files":
                    prefix = json.loads(body)["prefix"]
                    return self._reply(200, [{"name": k[len(prefix) + 1:]} for k in fake.objects
                                             if k.startswith(prefix + "/")])
                if u.path == "/storage/v1/object/aw-files" and method == "DELETE":
                    for k in json.loads(body)["prefixes"]:
                        fake.objects.pop(k, None)
                    return self._reply(200, [])
                return self._reply(404, {"message": u.path})

            def do_GET(self):  # noqa: N802
                self._route("GET")

            def do_POST(self):  # noqa: N802
                self._route("POST")

            def do_DELETE(self):  # noqa: N802
                self._route("DELETE")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.client = uploader.Client(f"http://127.0.0.1:{self.srv.server_address[1]}", SECRET)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def engine_run():
    """A real run folder (fake AI) with raw files and verdicts — the uploader's input."""
    repo = h.make_repo()
    st = h.make_state(repo)
    fa = FakeAdapter({B: [h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]})
    Engine(h.make_cfg(repo), h.make_request(1, None, h.NO_QA), st, fa, fa, sleep=lambda s: None,
           out=lambda m: None).run()
    return st


class Uploader(unittest.TestCase):
    def setUp(self):
        self.sb = FakeSupabase()

    def tearDown(self):
        self.sb.close()

    def test_row_and_files_go_up_then_only_on_change(self):
        st = engine_run()
        rid = f"{st.dir.parent.name}/{st.dir.name}"
        mem, now = {}, time.time()
        self.assertTrue(uploader.sync_run(self.sb.client, st.dir, mem, now))
        row = self.sb.rows[rid]
        self.assertEqual((row["status"], row["calls"]), ("AWAITING_APPROVAL", 3))
        files = row["payload"]["files"]
        raws = {f"raw/{p.name}" for p in (st.dir / "raw").glob("*.response.json")}
        self.assertTrue(raws and raws <= set(files))  # every raw bar the page can open is in the cloud
        self.assertTrue(all(files[r] in self.sb.objects for r in raws))
        self.assertTrue(all(k.isascii() for k in files.values()))
        n = self.sb.uploads
        self.assertFalse(uploader.sync_run(self.sb.client, st.dir, mem, now + 10))  # nothing happened
        self.assertEqual(self.sb.uploads, n)  # no file sent twice
        (st.dir / "heartbeat.json").write_text(json.dumps({"epoch": now + 20, "calls": []}), encoding="utf-8")
        self.assertTrue(uploader.sync_run(self.sb.client, st.dir, mem, now + 20))  # a new heartbeat = alive signal

    def test_what_changes_by_itself_does_not_count(self):
        a = {"now": 1, "status": {"hb_age": 3, "x": 1}, "progress": {"elapsed": 5},
             "now_calls": {"calls": [{"key": "b", "elapsed": 7, "estimate": 0.5}]},
             "calls": {"now": 1, "bars": [{"key": "b", "sec": 3}, {"key": "b", "running": True, "t1": 9}]}}
        b = json.loads(json.dumps(a))
        b["now"], b["status"]["hb_age"], b["progress"]["elapsed"] = 99, 50, 60
        b["now_calls"]["calls"][0].update(elapsed=70, estimate=0.9)
        b["calls"]["now"], b["calls"]["bars"][1]["t1"] = 99, 99
        self.assertEqual(uploader.payload_hash(a), uploader.payload_hash(b))
        b["status"]["x"] = 2
        self.assertNotEqual(uploader.payload_hash(a), uploader.payload_hash(b))

    def test_cleanup_removes_week_old_runs_only(self):
        c = self.sb.client
        old, new = "2026-09-01/run001_old", "2026-10-04/run001_new"
        c.upsert_run({"id": old, "payload": {}, "updated_at": "2026-09-20T00:00:00Z"})
        c.upsert_run({"id": new, "payload": {}, "updated_at": "2026-10-04T00:00:00Z"})
        c.upload(uploader.file_key(old, "raw/a.json"), b"x", "application/json")
        c.upload(uploader.file_key(new, "raw/a.json"), b"y", "application/json")
        now = time.mktime(time.strptime("2026-10-04 12:00:00", "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(uploader.cleanup(c, {}, now), [old])
        self.assertEqual(set(self.sb.rows), {new})
        self.assertEqual(list(self.sb.objects), [uploader.file_key(new, "raw/a.json")])

    def test_usage_is_one_small_row_sent_only_when_it_changes(self):
        root = h.ROOT / f"usage_up_{time.time_ns()}"
        rd = root / "2026-10-04" / "run001_u"
        rd.mkdir(parents=True)
        (rd / "state.json").write_text("{}", encoding="utf-8")
        now = time.time()
        (rd / "usage.json").write_text(json.dumps({"t": now, "five_hour": {"u": 0.4, "reset": now + 99}}),
                                       encoding="utf-8")
        mem = {}
        self.assertTrue(uploader.sync_usage(self.sb.client, mem, now, root))
        self.assertEqual(self.sb.rows[uploader.USAGE_ID]["payload"]["u5h"], 0.4)
        self.assertFalse(uploader.sync_usage(self.sb.client, mem, now + 3, root))  # same reading: nothing sent

    def test_only_runs_of_the_last_week_are_looked_at(self):
        st = engine_run()
        week_ago = time.time() - 8 * 86400
        for p in (st.dir / "state.json", st.dir / "heartbeat.json", st.dir / "timeline.md"):
            os.utime(p, (week_ago, week_ago))
        self.assertNotIn(st.dir, [rd for rd, _t in uploader.recent_runs(st.dir.parent.parent)])

    def test_a_down_cloud_never_stops_the_loop(self):
        st = engine_run()
        self.sb.fail = True
        saved = uploader.SYNC_DIR
        uploader.SYNC_DIR = h.ROOT / f"sync_{time.time_ns()}"
        try:
            self.assertEqual(uploader.loop(self.sb.client, st.dir.parent.parent, once=True), 0)
        finally:
            uploader.SYNC_DIR = saved

    def test_off_without_config(self):
        saved = uploader.ENV_FILE
        uploader.ENV_FILE = h.ROOT / "no_such.env"
        try:
            self.assertEqual(uploader.ensure_running(out=lambda m: None), "off")
        finally:
            uploader.ENV_FILE = saved


class Site(unittest.TestCase):
    def test_build_publishes_public_values_only(self):
        d = h.ROOT / f"site_{time.time_ns()}"
        cfg = {"url": "https://x.supabase.co", "publishable": "sb_publishable_ok", "secret": SECRET}
        dashboard_site.build(cfg, site_dir=d)
        self.assertEqual(json.loads((d / "config.json").read_text(encoding="utf-8")),
                         {"url": "https://x.supabase.co", "key": "sb_publishable_ok"})
        self.assertIn("CLOUD", (d / "index.html").read_text(encoding="utf-8"))
        self.assertFalse(any(SECRET in p.read_text(encoding="utf-8") for p in d.iterdir()))

    def test_build_refuses_a_secret_in_the_site(self):
        cfg = {"url": "https://x.supabase.co", "publishable": SECRET, "secret": SECRET}  # pasted into the wrong line
        with self.assertRaises(dashboard_site.SiteError):
            dashboard_site.build(cfg, site_dir=h.ROOT / f"site_{time.time_ns()}")
        with self.assertRaises(dashboard_site.SiteError):
            dashboard_site.build({**cfg, "publishable": ""}, site_dir=h.ROOT / f"site_{time.time_ns()}")


if __name__ == "__main__":
    unittest.main()
