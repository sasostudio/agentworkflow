import datetime as dt
import json
import unittest
from pathlib import Path

from agentworkflow import strength, todo
from agentworkflow.adapters import parse_reset
from agentworkflow.config import DEFAULTS, ConfigError, _merge, validate
from agentworkflow.qa import aggregate, enforce_evidence
from agentworkflow.request import RequestError, load_request

from helpers import ROOT, REQUEST

PLAN = ("# 계획\n\n"
        "- [ ] DEV-001 [L1] 버튼 추가\n  - 대상: src/a.ts\n  - 완료 조건: 버튼 보임\n  - 기능 위치: a.ts render()\n"
        "  - 선행: 없음\n"
        "- [ ] DEV-002 [L2] 문구 수정\n  - 대상: src/b.ts\n  - 완료 조건: 문구 바뀜\n  - 기능 위치: b.ts 상수\n"
        "  - 선행: 없음\n")  # §23-6 S5: `선행:` required
CHECKED_1 = PLAN.replace("- [ ] DEV-001 [L1] 버튼 추가\n", "- [x] DEV-001 [L1] 버튼 추가\n  - 완료 근거: src/a.ts (버튼)\n")


def _req(text, name):
    p = ROOT / name
    p.write_text(text, encoding="utf-8")
    return p


class TodoRules(unittest.TestCase):
    def test_parse_level_and_attrs(self):
        items, errs = todo.parse(CHECKED_1)
        self.assertEqual(errs, [])
        a, b = items
        self.assertEqual((a["id"], a["level"], a["target"], a["criteria"], a["evidence"]),
                         ("DEV-001", 1, "src/a.ts", "버튼 보임", "src/a.ts (버튼)"))
        self.assertEqual((b["level"], b["checked"]), (2, False))
        self.assertEqual(todo.parse("- [ ] DEV-003[L3]붙여씀\n")[0][0]["level"], 3)

    def test_nested_sublist_under_attribute_keeps_reading(self):
        # 09-25 run004(로블록스 F-04): `완료 조건:` 아래 더 깊이 들여쓴 하위 목록(ⓐ~ⓖ) 뒤의 `기능 위치:`를
        # 파서가 못 읽어 멀쩡한 기획을 3회 자동반려 → ESCALATED. 하위 목록은 속성 블록의 일부다.
        plan = ("- [ ] DEV-005 [L2] 배치 값\n  - 대상: src/Layout.luau\n  - 완료 조건: 아래 전부\n"
                "    - ⓐ 버튼 높이 ≥ 44\n    - ⓑ 겹침 없음\n  - 기능 위치: Layout.luau AREAS\n  - 선행: 없음\n"
                "- [ ] DEV-006 [L2] 다음 항목\n  - 대상: src/b.luau\n  - 완료 조건: c\n    - 하위\n")
        items, errs = todo.parse(plan)
        self.assertEqual(errs, [])
        self.assertEqual(items[0]["location"], "Layout.luau AREAS")
        self.assertEqual(items[0]["criteria"], "아래 전부")
        self.assertIsNone(items[1]["location"])  # 하위 목록을 건너뛰어도 다음 항목의 속성을 가져오지 않는다
        v = todo.check_plan("", "## 설계\n" + plan, run_level=2)
        self.assertEqual([x for x in v if "DEV-005" in x], [])
        self.assertTrue(any("DEV-006 '기능 위치:'" in x for x in v))

    def test_p1_nested_todo_item_does_not_overwrite_parent(self):
        # T13 (§23-3 C-1 보강 P1): 4칸 들여 중첩된 하위 TODO의 속성이 부모 속성을 덮어쓰면 안 된다
        plan = ("- [ ] DEV-001 [L2] 부모\n  - 대상: src/a.luau\n  - 완료 조건: 부모 조건\n  - 기능 위치: a.luau f()\n"
                "    - [ ] DEV-002 [L2] 자식\n      - 대상: src/b.luau\n      - 완료 조건: 자식 조건\n"
                "      - 기능 위치: b.luau g()\n")
        items, errs = todo.parse(plan)
        self.assertEqual(errs, [])
        by = {i["id"]: i for i in items}
        self.assertEqual(by["DEV-001"]["target"], "src/a.luau")
        self.assertEqual(by["DEV-001"]["location"], "a.luau f()")
        self.assertEqual(by["DEV-002"]["target"], "src/b.luau")

    def test_p2_deeper_attribute_like_line_is_part_of_sublist(self):
        # T14 (§23-3 C-1 보강 P2): 하위 목록 안의 `- 대상: 없음(설명)`이 부모 `대상`을 덮어쓰면 안 된다
        plan = ("- [ ] DEV-001 [L2] 항목\n  - 대상: src/a.luau\n  - 완료 조건: 아래\n"
                "    - 대상: 없음(설명용 예시)\n    - 완료 조건: 하위 설명\n  - 기능 위치: a.luau f()\n")
        it = todo.parse(plan)[0][0]
        self.assertEqual(it["target"], "src/a.luau")
        self.assertEqual(it["criteria"], "아래")
        self.assertEqual(it["location"], "a.luau f()")
        self.assertEqual(it["target_files"], ["src/a.luau"])

    def test_tab_indented_attributes_are_read(self):
        # T15 (§23-3): 탭 들여쓰기도 읽는다(expandtabs(4) 뒤에 잰다)
        plan = "- [ ] DEV-001 [L1] 탭\n\t- 대상: src/a.luau\n\t- 완료 조건: c\n\t\t- 하위\n\t- 기능 위치: a f()\n"
        it = todo.parse(plan)[0][0]
        self.assertEqual((it["target"], it["criteria"], it["location"]), ("src/a.luau", "c", "a f()"))

    def test_parse_errors(self):
        items, errs = todo.parse(PLAN + "- [ ] DEV-1 잘못된 ID\n- [ ] DEV-001 [L1] 중복\n")
        self.assertTrue(any("형식 오류" in e for e in errs))
        self.assertTrue(any("중복" in e for e in errs))

    def test_evidence_paths(self):
        # regression: Makefile-like names were not recognised as paths
        self.assertEqual(todo.evidence_files("Makefile (타깃 추가), src/a.ts, ./b.py"), ["Makefile", "src/a.ts", "b.py"])
        self.assertEqual(todo.evidence_files("그냥 설명 문장"), [])
        # regression (real run002): comma inside the explanation
        self.assertEqual(todo.evidence_files("app.js (클릭 이벤트 리스너 구현, 카운트 증가 및 화면 업데이트)"), ["app.js"])
        self.assertEqual(todo.evidence_files("a.js (x, y), b/c.py (z)"), ["a.js", "b/c.py"])
        self.assertEqual(todo.evidence_files("a.js (작성함(변조), 추가), b.py"), ["a.js", "b.py"])  # nested

    def test_dev_ok(self):
        self.assertEqual(todo.check_dev(PLAN, CHECKED_1, ["src/a.ts"]), [])

    def test_dev_no_evidence(self):
        after = PLAN.replace("- [ ] DEV-001", "- [x] DEV-001")
        self.assertTrue(any("근거 없이는" in v for v in todo.check_dev(PLAN, after, ["src/a.ts"])))

    def test_dev_evidence_not_in_diff(self):
        self.assertTrue(any("실제 변경 목록에 없음" in v for v in todo.check_dev(PLAN, CHECKED_1, ["src/zzz.ts"])))

    def test_dev_cannot_edit_text_level_target_criteria_or_add(self):
        self.assertTrue(todo.check_dev(PLAN, PLAN.replace("버튼 추가", "버튼 두 개 추가"), []))
        self.assertTrue(todo.check_dev(PLAN, PLAN.replace("[L2]", "[L1]"), []))  # v2: level is planner-owned
        self.assertTrue(todo.check_dev(PLAN, PLAN.replace("대상: src/a.ts", "대상: src/x.ts"), []))
        self.assertTrue(todo.check_dev(PLAN, PLAN.replace("버튼 보임", "아무거나"), []))
        self.assertTrue(todo.check_dev(PLAN, PLAN + "- [ ] DEV-003 [L1] 몰래\n  - 대상: a\n  - 완료 조건: b\n", []))

    def test_plan_rules(self):
        self.assertEqual(todo.check_plan("", PLAN, 1), [])
        self.assertTrue(any("체크는 개발 AI만" in v for v in todo.check_plan("", PLAN.replace("[ ] DEV-001", "[x] DEV-001"))))
        self.assertTrue(any("체크 상태" in v for v in todo.check_plan(CHECKED_1, PLAN)))
        self.assertTrue(any("완료된 항목을 삭제" in v for v in
                            todo.check_plan(CHECKED_1, "- [ ] DEV-002 [L2] 문구\n  - 대상: b\n  - 완료 조건: c\n")))

    def test_plan_requires_level_target_criteria(self):
        v = todo.check_plan("", "- [ ] DEV-001 버튼\n", 1)
        self.assertTrue(any("레벨 태그 없음" in x for x in v))
        self.assertTrue(any("'대상:' 줄 없음" in x for x in v))
        self.assertTrue(any("'완료 조건:' 줄 없음" in x for x in v))

    def test_plan_l2_needs_design_section(self):
        self.assertTrue(any("## 설계" in x for x in todo.check_plan("", PLAN, 2)))
        self.assertEqual(todo.check_plan("", PLAN.replace("# 계획\n", "# 계획\n## 설계\n- 파일\n"), 2), [])

    def test_set_level(self):
        t = todo.set_level(CHECKED_1, "DEV-001", 3)
        self.assertIn("- [x] DEV-001 [L3] 버튼 추가", t)
        self.assertIn("완료 근거: src/a.ts", t)
        self.assertEqual(todo.parse(t)[0][0]["level"], 3)

    def test_check_design(self):
        full = "## DEV-002 설계\n" + "".join(f"### {p}\nx\n" for p in todo.DESIGN_PARTS)
        self.assertEqual(todo.check_design(full, ["DEV-002"]), [])
        self.assertTrue(any("없음" in v for v in todo.check_design(full, ["DEV-009"])))
        partial = "## DEV-002\n### 파일\nx\n### 흐름\ny\n## DEV-003\n### 시그니처\n"
        v = todo.check_design(partial, ["DEV-002"])
        self.assertTrue(v and "시그니처" in v[0] and "경계" in v[0])  # section boundary respected

    def test_next_fix(self):
        self.assertEqual(todo.next_fix_id(PLAN), "FIX-001")
        self.assertEqual(todo.next_fix_id(PLAN + "- [ ] FIX-002 [L1] a\n"), "FIX-003")


class Levels(unittest.TestCase):
    cfg = {"high_risk_paths": ["firestore.rules", "src/payment/"], "doc_extensions": [".md"]}

    def test_judge_level(self):
        j = strength.judge_level
        self.assertEqual(j(["README.md"], self.cfg, 0, 3)[0], 3)          # v5: docs only is reviewed, never lowered
        self.assertEqual(j(["README.md"], self.cfg, 0, 0)[0], 1)          # v5: docs only -> at least L1 (was L0)
        self.assertTrue(j(["README.md"], self.cfg, 0, 1)[1].startswith(strength.DOCS_ONLY))
        self.assertEqual(j(["src/a.ts"], self.cfg, 0, 1)[0], 1)            # item level kept
        self.assertEqual(j(["src/a.ts"], self.cfg, 0, 2)[0], 2)
        self.assertEqual(j(["app/firestore.rules"], self.cfg, 0, 1)[0], 3)  # high-risk -> L3
        self.assertEqual(j(["src/payment/pay.ts"], self.cfg, 0, 2)[0], 3)
        self.assertEqual(j(["src/a.ts"], self.cfg, 2, 1)[0], 2)            # repeated check fails -> L2
        self.assertEqual(j(["src/a.ts"], self.cfg, 5, 3)[0], 3)            # never lowers


class Qa(unittest.TestCase):
    def test_roblox_qa_prompt_warns_server_context_has_its_own_module_instances(self):
        # C-3 (09-25 run005 F-05): QA wrote Server scenarios that `require`d the game's service modules to read live
        # state -> the runner's Server context has its own module instances (measured: client roll OK, required
        # SessionService saw no session) -> 5 of 8 scenarios BLOCKED. The prompt must say how to observe live state.
        from agentworkflow import prompts
        p = prompts.eval_main({}, {"runner": "roblox", "environment": "external", "base_url": None, "cap": 20,
                                   "carry": ""})
        self.assertIn("모듈 인스턴스", p)
        self.assertIn("Remote", p)
        self.assertIn("DataStore에 저장한 값은 남는다", p)  # v6 N14: the old "정지하면 사라진다" was wrong


    def test_aggregate_order(self):
        self.assertEqual(aggregate([{"status": "PASSED"}, {"status": "BLOCKED"}]), "BLOCKED")
        self.assertEqual(aggregate([{"status": "BLOCKED"}, {"status": "FAILED"}]), "FAILED")
        self.assertEqual(aggregate([{"status": "PASSED"}]), "PASSED")
        self.assertEqual(aggregate([]), "BLOCKED")

    def test_passed_without_evidence_is_blocked(self):
        r = enforce_evidence([{"id": "S1", "status": "PASSED", "evidence": ["nope.png"]}], ROOT / "none")
        self.assertEqual(r[0]["status"], "BLOCKED")


class Request(unittest.TestCase):
    def test_level_required_and_parsed(self):
        r = load_request(_req(REQUEST.replace("{level}", "2"), "req_l2.md"))
        self.assertEqual(r["run_level"], 2)
        with self.assertRaises(RequestError):
            load_request(_req(REQUEST.replace("## 난이도\nL{level}\n근거: 테스트\n", ""), "req_nolv.md"))
        with self.assertRaises(RequestError):
            load_request(_req(REQUEST.replace("L{level}", "어려움"), "req_badlv.md"))

    def test_old_strength_section_refused(self):
        with self.assertRaises(RequestError):
            load_request(_req(REQUEST.replace("{level}", "1") + "\n## 강도\n2\n", "req_old.md"))

    def test_missing_section(self):
        with self.assertRaises(RequestError):
            load_request(_req(REQUEST.replace("{level}", "1").replace("## 범위 외\n- 다른 파일\n", ""), "req_bad.md"))

    def test_example_request_is_valid(self):
        r = load_request(Path(__file__).resolve().parent.parent / "examples" / "request.example.md")
        self.assertIn(r["run_level"], (1, 2, 3))
        self.assertTrue(r["sections"]["항목"])  # v6: the Master's TODO

    def test_models_section_refused(self):
        # v6 A3: the Master does not pick models (09-29) — a request that still has the section is refused
        with self.assertRaises(RequestError):
            load_request(_req(REQUEST.replace("{level}", "1") + "\n## 역할·모델\n- developer: claude-opus-5-5 / high\n",
                              "req_models.md"))


class Config(unittest.TestCase):
    def test_defaults_decision_19_20(self):
        # v6 §2: exact ids, never aliases — builder Sonnet 5.5 · evaluator (and its second/devil sessions) Opus 5.5
        self.assertEqual(set(DEFAULTS["models"]), {"builder", "evaluator"})
        self.assertEqual(DEFAULTS["models"]["builder"]["model"], "claude-sonnet-5-5")
        self.assertEqual(DEFAULTS["models"]["evaluator"]["model"], "claude-opus-5-5")
        ev = DEFAULTS["levels"]["evaluate"]
        self.assertEqual([ev[k]["effort"] for k in "123"], ["medium", "medium", "high"])
        # 10-03 정렬안: no scenario floor (min_failure gone) · the second view follows high-risk/contract changes, not L3
        self.assertFalse(any("min_failure" in ev[k] for k in "123"))
        self.assertEqual([ev[k]["second"] for k in "123"], [False, False, False])
        self.assertEqual(DEFAULTS["limits"]["max_eval_runs"], 3)  # decision 10-03
        self.assertEqual((DEFAULTS["limits"]["idle_timeout_sec"], DEFAULTS["limits"]["call_total_sec"]), (300, 10800))

    def test_v1_review_keys_refused(self):
        cfg = _merge(DEFAULTS, {"target": "x", "models": {"review_1": {"model": "claude-opus-5-5"}}})
        with self.assertRaises(ConfigError):
            validate(cfg)

    def test_bad_effort_refused(self):
        cfg = _merge(DEFAULTS, {"target": "x", "levels": {"plan_review": {"2": "ultra"}}})
        with self.assertRaises(ConfigError):
            validate(cfg)


class OurRules(unittest.TestCase):
    """09-24 (design §18)."""

    def _cfg(self, **over):
        cfg = _merge(DEFAULTS, {"target": "x", **over})
        validate(cfg)
        return cfg

    def test_irreversible_mcp_tools_refused(self):
        for tool in ("review_submit", "bundle_upload", "push_send_scheduled", "event_act_type_set",
                     "iaa_placement_group_create", "generate_image", "execute_luau"):
            with self.assertRaises(ConfigError, msg=tool):
                self._cfg(capabilities={"mcp_servers": ["s"], "mcp_tools": {"evaluator": [f"mcp__s__{tool}"]}})
        for tool in ("notice_list", "notice_get", "bundle_list", "dashboard_dau", "iaa_settlement_summary_v2"):
            self._cfg(capabilities={"mcp_servers": ["s"], "mcp_tools": {"evaluator": [f"mcp__s__{tool}"]}})

    def test_capability_shape_checked(self):
        with self.assertRaises(ConfigError):  # server not declared
            self._cfg(capabilities={"mcp_tools": {"evaluator": ["mcp__s__notice_list"]}})
        with self.assertRaises(ConfigError):  # unknown role
            self._cfg(capabilities={"web_roles": ["boss"]})
        with self.assertRaises(ConfigError):  # bkit polluted the target in run001
            self._cfg(capabilities={"plugins": ["bkit@bkit-marketplace"]})
        self._cfg(capabilities={"plugins": ["frontend-design@claude-plugins-official"], "web_roles": ["evaluator"]})

    def test_restricted_refused(self):
        with self.assertRaises(ConfigError):
            self._cfg(providers={"claude_args": ["--restricted"]})

    def test_outside_targets(self):
        plan = "- [ ] DEV-001 [L1] a\n  - 대상: src/a.ts, src/ui/, t/*_test.py (신규)\n  - 완료 조건: c\n  - 기능 위치: a\n"
        self.assertEqual(todo.outside_targets(["src/a.ts", "src/ui/b.tsx", "t/c_test.py", "x.py"], plan), ["x.py"])

    def test_bypass_scan_only_added_lines(self):
        from agentworkflow import bypass_scan
        diff = ("--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1 +1,2 @@\n const old: any = 1;\n"
                "+try { f() } catch (e) {}\n--- /dev/null\n+++ b/n.py  (새 파일)\ndef f():\n    x = 1  # type: ignore\n")
        kinds = {(p, k) for p, k, _ in bypass_scan.scan(diff)}
        self.assertIn(("src/a.ts", "빈 catch"), kinds)
        self.assertNotIn(("src/a.ts", "any 타입"), kinds)  # context line, not added
        self.assertIn(("n.py", "타입 검사 끄기"), kinds)  # indented body of a new file is scanned
        self.assertEqual(bypass_scan.scan("--- a/README.md\n+++ b/README.md\n+TODO later\n"), [])

    def test_skill_files_exist_under_rules_root(self):
        from agentworkflow import prompts
        for p in prompts.skill_paths(DEFAULTS["rules_root"]).values():
            self.assertTrue(Path(p).is_file(), p)


class Adapter(unittest.TestCase):
    def test_main_model_skips_cli_side_calls(self):
        from agentworkflow.adapters import Result
        r = Result(model_usage={"claude-haiku-4-5-20251001": {"outputTokens": 900},
                                "claude-opus-5-5": {"outputTokens": 40}})
        self.assertEqual(r.main_model, "claude-opus-5-5")  # measured 09-23: haiku side calls appear in modelUsage
        self.assertEqual(Result(model_usage={"claude-haiku-4-5-20251001": {"outputTokens": 5}}).main_model,
                         "claude-haiku-4-5-20251001")

    def test_parse_reset(self):
        now = dt.datetime(2026, 9, 23, 20, 0)
        self.assertEqual(parse_reset("Usage limit reached. resets 11pm", now), dt.datetime(2026, 9, 23, 23, 0))
        self.assertEqual(parse_reset("limit resets at 09:30", now), dt.datetime(2026, 9, 24, 9, 30))
        self.assertIsNone(parse_reset("something else", now))

    def test_argv_reviewer_is_read_only_and_forks(self):
        from agentworkflow.adapters import make_adapter
        cfg = _merge(DEFAULTS, {"target": "x"})
        ad = make_adapter("claude", cfg, ROOT / "mcp.json")
        argv = ad.build_argv(model="claude-opus-5-5", effort="xhigh", allowed_tools=["Read", "Grep"],
                             disallowed_tools=["Bash"], permission_mode="dontAsk", add_dirs=None,
                             schema={"type": "object"}, resume="sid", fork=True)
        self.assertIn("--fork-session", argv)
        self.assertNotIn("--restricted", argv)  # it also drops the project CLAUDE.md, web, MCP
        # user settings never load (bkit + global allow rules leaked an MCP tool past --allowedTools — probe D2)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")
        st = json.loads(argv[argv.index("--settings") + 1])
        self.assertTrue(st["disableAllHooks"])
        self.assertFalse(st["enabledPlugins"]["bkit@bkit-marketplace"])
        self.assertEqual(Path(argv[argv.index("--add-dir") + 1]), Path(DEFAULTS["rules_root"]))
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Grep")
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--json-schema", argv)


if __name__ == "__main__":
    unittest.main()
