"""roblox QA runner (design §19) against a fake Studio MCP server over real stdio — AI calls 0, Studio none."""

import json
import sys
import unittest
from pathlib import Path

from agentworkflow import qa
from agentworkflow.config import ConfigError, DEFAULTS, _merge, validate

import helpers as h

FAKE = Path(__file__).resolve().parent / "fake_studio.py"
SOURCE = "return {}\n"


def setup(scenarios, **state):
    repo = h.make_repo()
    (repo / "src").mkdir()
    (repo / "src" / "Mod.luau").write_text(SOURCE, encoding="utf-8")
    h.git(repo, "add", "-A")
    h.git(repo, "commit", "-q", "-m", "src")
    st = {"studios": [{"id": "s1", "name": "My Place"}], "mode": "Edit", "calls": [],
          "sources": {"ReplicatedStorage/Mod": SOURCE}}
    st.update(state)
    sp = repo.parent / f"{repo.name}_studio.json"
    sp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    smap = {"name": "p", "className": "DataModel", "children": [
        {"name": "ReplicatedStorage", "className": "ReplicatedStorage", "children": [
            {"name": "Mod", "className": "ModuleScript", "filePaths": ["src/Mod.luau"]}]}]}
    mp = repo.parent / f"{repo.name}_sourcemap.json"
    mp.write_text(json.dumps(smap), encoding="utf-8")
    rb = {"studio_name": state.pop("studio_name", None) if "studio_name" in state else None,
          "mcp_command": f'"{sys.executable}" "{FAKE}" "{sp}"',
          "sourcemap_command": f'"{sys.executable}" -c "import sys;sys.stdout.write(open(r\'{mp}\').read())"',
          "play_ready_timeout_sec": 3, "max_wait_sec": 3}  # waits must fit the 5 s scenario timeout
    cfg = h.make_cfg(repo, qa={"runner": "roblox", "scenario_timeout_sec": 5, "roblox": rb})
    qdir = repo.parent / f"{repo.name}_qa"
    qdir.mkdir()
    files = qa.write_scenarios(scenarios, qdir, "roblox")
    return cfg, repo, qdir, files, sp


def scen(sid, code, kind="normal"):
    return {"id": sid, "title": sid, "kind": kind, "filename": sid, "code": code}


def run(scenarios, **state):
    cfg, repo, qdir, files, sp = setup(scenarios, **state)
    res = qa.RUN["roblox"](cfg, repo, qdir, files)
    return {r["id"]: r for r in res}, json.loads(sp.read_text(encoding="utf-8")), qdir


def plays(state):
    return [c["is_start"] for c in state["calls"] if c["tool"] == "start_stop_play"]


class Roblox(unittest.TestCase):
    def test_pass_fail_error_and_play_always_stopped(self):
        r, st, qdir = run([scen("ok", "-- MARK_PASS"), scen("bad", "-- MARK_FAIL", "failure"),
                           scen("err", "-- MARK_ERROR", "failure"), scen("nt", "-- MARK_NOTABLE", "failure")])
        self.assertEqual(r["ok"]["status"], "PASSED")
        self.assertEqual(r["ok"]["evidence"], ["01.jpg"])
        self.assertTrue((qdir / "evidence" / "ok" / "01.jpg").read_bytes().startswith(b"\xff\xd8"))
        self.assertEqual(r["bad"]["status"], "FAILED")  # control: a wrong scenario is detected
        self.assertEqual(r["err"]["status"], "FAILED")
        self.assertIn("실행 오류", r["err"]["note"])
        self.assertEqual(r["nt"]["status"], "FAILED")
        self.assertEqual(plays(st), [True, False] * 4)  # every start has its stop
        self.assertEqual(st["mode"], "Edit")
        self.assertTrue((qdir / "log_ok.txt").is_file())

    def test_no_capture_skips_the_capture_and_keeps_the_passed(self):
        """Kick ends the client — nothing to capture; the data check passed but was demoted
        twice. A declared `no_capture` is not captured and stays PASSED, marked as a fact; undeclared ones still fall."""
        kick = dict(scen("kick", "-- MARK_PASS"), no_capture="Kick으로 클라이언트가 끊김 — 서버 데이터로 판정")
        r, st, qdir = run([kick, scen("ok", "-- MARK_PASS")])
        self.assertEqual(r["kick"]["status"], "PASSED")
        self.assertIn("캡처 생략", r["kick"]["note"])
        self.assertEqual(r["kick"]["evidence"], [])
        caps = [c for c in st["calls"] if c["tool"] == "screen_capture"]
        self.assertEqual(len(caps), 1)  # only "ok" was captured
        self.assertEqual(r["ok"]["evidence"], ["01.jpg"])
        res = qa.enforce_evidence([{"id": "x", "status": "PASSED", "note": "", "evidence": []}], qdir / "evidence")
        self.assertEqual(res[0]["status"], "BLOCKED")  # without the declaration the old rule holds

    def test_timeout_is_failed_and_play_is_stopped(self):
        r, st, _ = run([scen("hang", "-- MARK_HANG")])
        self.assertEqual(r["hang"]["status"], "FAILED")
        self.assertIn("타임아웃", r["hang"]["note"])
        self.assertEqual(plays(st), [True, False])
        self.assertEqual(st["mode"], "Edit")

    def test_no_studio_blocks_everything(self):
        r, st, _ = run([scen("ok", "-- MARK_PASS")], studios=[])
        self.assertEqual(r["ok"]["status"], "BLOCKED")
        self.assertEqual(plays(st), [])

    def test_two_studios_need_a_name(self):
        two = [{"id": "s1", "name": "My Place"}, {"id": "s2", "name": "Other"}]
        r, _, _ = run([scen("ok", "-- MARK_PASS")], studios=two)
        self.assertEqual(r["ok"]["status"], "BLOCKED")
        r, _, _ = run([scen("ok", "-- MARK_PASS")], studios=two, studio_name="My Place")
        self.assertEqual(r["ok"]["status"], "PASSED")

    def test_studio_already_playing_is_not_touched(self):
        r, st, _ = run([scen("ok", "-- MARK_PASS")], mode="Play")
        self.assertEqual(r["ok"]["status"], "BLOCKED")
        self.assertIn("Edit 모드가 아님", r["ok"]["note"])
        self.assertEqual(plays(st), [])  # someone may be playing — the runner must not stop it

    def test_stale_studio_source_blocks(self):
        r, st, _ = run([scen("ok", "-- MARK_PASS")], sources={"ReplicatedStorage/Mod": "return {old = true}\n"})
        self.assertEqual(r["ok"]["status"], "BLOCKED")
        self.assertIn("Rojo", r["ok"]["note"])
        self.assertEqual(plays(st), [])

    def test_sync_lag_is_retried_not_blocked(self):
        # gap (09-25 simulation): the developer just wrote files; Rojo needs a moment to push them into Studio
        r, st, _ = run([scen("ok", "-- MARK_PASS")], sync_lag_calls=2,
                       sources_lagging={"ReplicatedStorage/Mod": "stale\n"},
                       sources_after_lag={"ReplicatedStorage/Mod": SOURCE})
        self.assertEqual(r["ok"]["status"], "PASSED")

    def test_big_sources_do_not_break_the_sync_check(self):
        # 09-26 F-10 run003: 10 big scripts in one chunk → Studio answered "[Complex Table]" → JSONDecodeError crash
        big = "return {\n" + "  x = 1,\n" * 2000 + "}\n"
        cfg, repo, qdir, files, sp = setup([scen("ok", "-- MARK_PASS")], complex_table_over=2000,
                                           sources={"ReplicatedStorage/Mod": big})
        (repo / "src" / "Mod.luau").write_text(big, encoding="utf-8")
        res = {x["id"]: x for x in qa.RUN["roblox"](cfg, repo, qdir, files)}
        self.assertEqual(res["ok"]["status"], "PASSED")

    def test_play_that_cannot_stop_blocks_the_rest(self):
        r, st, _ = run([scen("a", "-- MARK_PASS"), scen("b", "-- MARK_PASS")], stop_fails=True)
        self.assertIn("정지 확인 실패", r["a"]["note"])
        self.assertEqual(r["b"]["status"], "BLOCKED")
        self.assertEqual(plays(st).count(True), 1)  # b never started

    def test_client_context_line(self):
        r, st, _ = run([scen("c", "-- context: Client\n-- MARK_PASS")])
        self.assertEqual(r["c"]["status"], "PASSED")
        ctx = [c["datamodel_type"] for c in st["calls"] if c["tool"] == "execute_luau" and c["datamodel_type"] != "Edit"]
        self.assertEqual(ctx, ["Client"])

    def test_steps_run_in_order_inside_one_play(self):
        """v4 §10 (#1): Server prepares → Client acts → the last step judges — one play, contexts switched."""
        s = {**scen("m", ""), "steps": [{"context": "Server", "code": "-- setup"},
                                        {"context": "Client", "code": "-- MARK_PASS"}]}
        r, st, qdir = run([s])
        self.assertEqual(r["m"]["status"], "PASSED")
        ctx = [c["datamodel_type"] for c in st["calls"] if c["tool"] == "execute_luau" and c["datamodel_type"] != "Edit"]
        self.assertEqual(ctx, ["Server", "Client"])
        self.assertEqual(plays(st), [True, False])  # ONE play for both steps, stopped at the end
        body = next((qdir / "scenarios").glob("*m.luau")).read_text(encoding="utf-8")
        self.assertIn("step 2/2 · context: Client", body)

    def test_a_failing_step_stops_the_scenario(self):
        s = {**scen("m", ""), "steps": [{"context": "Server", "code": "-- MARK_FAIL"},
                                        {"context": "Client", "code": "-- MARK_PASS"}]}
        r, st, _ = run([s])
        self.assertEqual(r["m"]["status"], "FAILED")
        self.assertIn("[단계 1/2 Server]", r["m"]["note"])
        ctx = [c["datamodel_type"] for c in st["calls"] if c["tool"] == "execute_luau" and c["datamodel_type"] != "Edit"]
        self.assertEqual(ctx, ["Server"])
        self.assertFalse(r["m"]["env"])  # the scenario ran: whatever it found is not the environment's

    def test_nothing_ran_is_flagged_env(self):
        r, _, _ = run([scen("ok", "-- MARK_PASS")], studios=[])
        self.assertTrue(r["ok"]["env"])
        r, _, _ = run([scen("a", "-- MARK_PASS"), scen("b", "-- MARK_PASS")], stop_fails=True)
        self.assertTrue(r["b"]["env"])

    def test_results_are_written_one_by_one_and_a_resume_plays_only_what_is_left(self):
        """#7 (09-28 10:39): the PC went off at 36/75 and every judgment was lost."""
        cfg, repo, qdir, files, sp = setup([scen("a", "-- MARK_PASS"), scen("b", "-- MARK_FAIL", "failure")])
        res = qa.RUN["roblox"](cfg, repo, qdir, files)
        partial = json.loads((qdir / "partial_results.json").read_text(encoding="utf-8"))
        self.assertEqual([r["id"] for r in partial], ["a", "b"])  # on disk after each scenario
        before = len([c for c in json.loads(sp.read_text(encoding="utf-8"))["calls"] if c["tool"] == "execute_luau"])
        prior = {r["id"]: r for r in res if r["id"] == "a"}
        res2 = qa.RUN["roblox"](cfg, repo, qdir, files, prior=prior)
        after = len([c for c in json.loads(sp.read_text(encoding="utf-8"))["calls"] if c["tool"] == "execute_luau"])
        self.assertEqual({r["id"]: r["status"] for r in res2}, {"a": "PASSED", "b": "FAILED"})
        self.assertEqual(after - before, 1 + 1)  # the sync check + only "b" played

    def test_unit_test_runs_first(self):
        cfg, repo, qdir, files, sp = setup([scen("ok", "-- MARK_PASS")])
        cfg["qa"]["roblox"]["unit_test_code"] = "return UNIT_BAD"
        res = qa.RUN["roblox"](cfg, repo, qdir, files)
        self.assertEqual([r["id"] for r in res], ["UNIT", "ok"])
        self.assertEqual(res[0]["status"], "FAILED")
        self.assertIn("failed=1", res[0]["note"])

    def test_config_defaults_and_scenario_contract(self):
        cfg = _merge(DEFAULTS, {"target": "x", "qa": {"runner": "roblox"}})
        validate(cfg)
        self.assertIn("mcp.bat", cfg["qa"]["roblox"]["mcp_command"])
        self.assertIn("sourcemap", cfg["qa"]["roblox"]["sourcemap_command"])
        with self.assertRaises(ConfigError):
            validate(_merge(DEFAULTS, {"target": "x", "qa": {"runner": "roblox", "roblox": {"play_ready_timeout_sec": 0}}}))
        from agentworkflow import prompts
        self.assertIn("status", prompts.RUNNER_CONTRACT["roblox"])
        # AI roles still never get execute_luau (the runner is engine code)
        with self.assertRaises(ConfigError):
            validate(_merge(DEFAULTS, {"target": "x", "capabilities": {
                "mcp_servers": ["Roblox_Studio"], "mcp_tools": {"qa": ["mcp__Roblox_Studio__execute_luau"]}}}))


class Precheck(unittest.TestCase):
    """v4 §10 (#8): what can be known before a scenario runs — the scenario's own problem (back to the QA AI)."""

    def cfg(self, **qa_over):
        repo = h.make_repo()
        syn = f'"{sys.executable}" -c "import sys; sys.exit(3 if chr(83)+\'YNTAX_ERR\' in sys.stdin.read() else 0)"'
        return h.make_cfg(repo, qa={"runner": "roblox", "scenario_timeout_sec": 90, "syntax_command": syn,
                                    "roblox": {"max_wait_sec": 60}, **qa_over}), repo

    def test_waits_return_syntax_and_steps(self):
        cfg, repo = self.cfg()
        ok = scen("ok", "task.wait(20)\nreturn {status='PASSED', note='x'}")
        self.assertEqual(qa.precheck([ok], cfg, repo), [])
        slow = scen("slow", "task.wait(50) wait(20)\nreturn {status='PASSED'}")
        noret = scen("noret", "local x = 1")
        bad = {**scen("bad", ""), "steps": [{"context": "Server", "code": "return nil"},
                                            {"context": "Client", "code": "SYNTAX_ERR return {}"}]}
        v = qa.precheck([slow, noret, bad], cfg, repo)
        self.assertTrue(any("slow" in x and "70초" in x for x in v), v)
        self.assertTrue(any("noret" in x and "return" in x for x in v), v)
        self.assertTrue(any("bad 단계 2 문법 오류" in x for x in v), v)
        self.assertFalse(any("bad 단계 1" in x for x in v), v)

    def test_a_syntax_command_that_cannot_run_is_a_config_error_not_a_scenario_defect(self):
        cfg, repo = self.cfg(syntax_command="../no/such/tool.exe -")
        with self.assertRaises(qa.SyntaxToolError):
            qa.precheck([scen("ok", "return {status='PASSED'}")], cfg, repo)

    def test_steps_only_for_roblox_and_count_and_duplicates(self):
        repo = h.make_repo()
        cfg = h.make_cfg(repo)  # command runner
        s = {**scen("a", ""), "steps": [{"context": "Server", "code": "x"}]}
        v = qa.precheck([s, scen("a", "PASS")], cfg, repo, max_n=1)
        self.assertTrue(any("roblox 러너만" in x for x in v), v)
        self.assertTrue(any("최대 1개" in x for x in v), v)
        self.assertTrue(any("id 중복: a" in x for x in v), v)


if __name__ == "__main__":
    unittest.main()
