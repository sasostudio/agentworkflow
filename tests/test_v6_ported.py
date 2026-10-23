"""v6 (10-02) — the lessons of the v4/v5 test files that still apply, ported (ratchet principle: a guard against a real
incident is never dropped with the old engine path). Source file::test named in each docstring.

Not ported (the behaviour no longer exists in v6 — 설계서 v6 §6-2): planner / plan review / design stage · parallel
development and waves · large runs · preplan · seed sessions · "a stage call is a fresh session" (B2 — v6 continues
sessions on purpose) · stage gates · qa-fix · `resume --now` (WAITING_RESET) · `## 실행 방식` · `## 역할·모델`."""

import datetime as dt
import json
import os
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path

from agentworkflow import effort, flow, prompts, qa, roles, studio_capture, studio_shot, todo, waitreset
from agentworkflow.adapters import ClaudeAdapter, FakeAdapter, LimitError, Result, parse_reset
from agentworkflow.config import ConfigError
from agentworkflow.engine import Engine
from agentworkflow.request import RequestError, parse_kind
from agentworkflow.config import DEFAULTS
from agentworkflow.safety import SafetyError
from agentworkflow.state import RunState
from agentworkflow.studio_mcp import StudioClient

import helpers as h
import test_roblox_runner as trr

B, E, DV = "builder", "evaluator", "evaluator:devil"


def make(script, level=1, items=None, extra=h.NO_QA, cfg=None, st=None, repo=None):
    repo = repo or h.make_repo()
    c = h.make_cfg(repo, **(cfg or {}))
    req = h.make_request(level, items, extra)
    st = st or h.make_state(repo)
    fa = script if isinstance(script, FakeAdapter) else FakeAdapter(script)
    slept = []
    e = Engine(c, req, st, fa, fa, sleep=slept.append, out=lambda m: None)
    e.slept = slept
    return e, fa, st, repo


def ok_eval():
    return {E: [h.e_ans("READY")], DV: [h.devil()]}


def timeline(st):
    return (st.dir / "timeline.md").read_text(encoding="utf-8")


def with_rate(fn, five=None, seven=None):
    def run(kw):
        r = fn(kw)
        reset = int((dt.datetime.now() + dt.timedelta(hours=2)).timestamp())
        r.rate = {k: {"utilization": v, "resetsAt": reset} for k, v in (("five_hour", five), ("seven_day", seven))
                  if v is not None}
        return r
    return run


# ================= builder fences (test_engine.Flow · OurRules · test_gaps) =================
class Fences(unittest.TestCase):
    def test_builder_editing_the_todo_text_is_auto_rejected(self):
        """test_engine::test_dev_changing_todo_text_is_auto_rejected_then_escalated — only the Master writes items."""
        def tamper(kw):
            p = next(Path(kw["cwd"]).glob("docs/plan/*.md"))
            p.write_text(p.read_text(encoding="utf-8").replace("hello.txt 작성", "몰래 바꿈"), encoding="utf-8")
        e, fa, st, _ = make({B: [h.b_work(extra=tamper), h.b_work(files={"hello.py": "2\n"}, done=[])],
                             **ok_eval()})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertIn("Master만", [c for c in fa.calls if c["role"] == B][1]["prompt"])  # reported to the builder
        self.assertNotIn("몰래 바꿈", e.plan_path.read_text(encoding="utf-8"))          # and never survives
        self.assertIn("[x] DEV-001", e.plan_path.read_text(encoding="utf-8"))

    def test_check_artifacts_do_not_count_as_outside_targets(self):
        """test_engine::test_check_artifacts_do_not_count_as_outside_targets (e2e run001 __pycache__)."""
        e, fa, st, repo = make({B: [h.b_work()], **ok_eval()},
                               cfg={"check_commands": [f'"{sys.executable}" -c "open(\'built.out\',\'w\').write(\'x\')"']})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertIn("built.out", st.d["check_artifacts"])

    def test_item_without_feature_location_is_refused_before_the_run(self):
        """test_engine::test_plan_without_feature_location_is_auto_rejected — v6 P17: before the run folder exists."""
        r = h.make_request(items=[h.item(location=None)], extra=h.NO_QA)
        with self.assertRaises(RequestError):
            flow.decide(r, h.make_cfg(h.make_repo()))

    def test_level_floor_raises_items_below_the_run_level(self):
        """test_engine::test_run_l2_requires_design_section_and_floors_levels (the floor half — B1 drops the design)."""
        e, fa, st, _ = make({}, level=2, items=[h.item(level=2), h.item("DEV-002", level=2, target="b.py",
                                                                         location="b.py")])
        e.req["sections"]["항목"] = e.req["sections"]["항목"].replace("[L2]", "[L1]", 1)
        e.s_planning()
        self.assertTrue(all(i["level"] == 2 for i in todo.parse(e._read_plan())[0]))
        self.assertIn("레벨 하한 DEV-001 L1→L2", timeline(st))


# ================= questions (test_gaps) =================
class Questions(unittest.TestCase):
    def test_question_saying_none_does_not_stop(self):
        """test_gaps::test_question_line_saying_none_does_not_stop · test_bold_question_label_saying_none."""
        for q in ("없음", "**없음**", "해당 없음.", "none", "—", '""', "''", "``"):  # '""' = T4 smoke 10-02 23:33
            e, fa, st, _ = make({B: [h.b_work(question=q)], **ok_eval()})
            self.assertEqual(e.run(), "AWAITING_APPROVAL", q)

    def test_wiki_call_question_stops(self):
        """test_gaps::test_wiki_question_stops (G6) — the wiki call is the builder's too."""
        def asks(kw):
            r = h.b_wiki()(kw)
            r.structured["question"] = "기능 문서를 나눌까?"
            return r
        e, fa, st, _ = make({B: [h.b_work(), asks]}, extra="## 흐름\n- QA: 없음\n")
        self.assertEqual(e.run(), "AWAITING_ANSWER")

    def test_denials_reach_the_evaluator_and_the_report(self):
        """test_gaps::test_denials_reach_reviewer_and_report (G5)."""
        def denied(kw):
            r = h.b_work()(kw)
            r.denials = [{"tool_name": "Bash", "tool_input": {"command": "rm x"}}]
            return r
        e, fa, st, repo = make({B: [denied], **ok_eval()})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertIn("권한 거부", [c for c in fa.calls if c["role"] == E][0]["prompt"])
        self.assertIn("rm x", (st.dir / "report.md").read_text(encoding="utf-8"))


# ================= retry · budgets (test_signals) =================
class Signals(unittest.TestCase):
    def test_missing_structured_output_is_retried(self):
        """test_signals::test_missing_structured_output_is_retried."""
        e, fa, st, _ = make({B: [lambda kw: Result(text="no json"), h.b_work()], **ok_eval()})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(e.slept, [30])

    def test_transient_error_is_retried_then_help_after_the_budget(self):
        """test_signals::test_transient_call_error_is_retried_then_continues · test_retry_budget_is_per_stage."""
        err = lambda kw: Result(text="API Error 529 overloaded", is_error=True)  # noqa: E731
        e, fa, st, _ = make({B: [err] * 4})
        self.assertEqual(e.run(), "HELP")
        self.assertEqual(e.slept, [30, 120, 300])
        self.assertEqual(st.d["help_kind"], "retries")

    def test_auto_rejects_do_not_use_up_the_evaluation_budget(self):
        """test_signals::test_rule_violations_do_not_use_up_the_review_budget — self_fix ≠ eval_rejects."""
        e, fa, st, _ = make({B: [h.b_work(gate=""), h.b_work(files={"hello.py": "2\n"}, done=[])], **ok_eval()})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(st.d["counters"]["eval_rejects"], 0)

    def test_rework_with_no_change_goes_back_to_evaluation(self):
        """test_engine::test_rework_round_without_changes_goes_to_review — the builder may dispute a finding."""
        rej = h.e_ans("REJECT", issues=[("high", "잘못된 지적")])
        dispute = h.b_work(files={}, done=[], remaining="없음")
        e, fa, st, _ = make({B: [h.b_work(), dispute], E: [rej, h.e_ans("READY")], DV: [h.devil()]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(st.d["eval_no"], 2)


# ================= evaluation inputs (test_engine · test_parallel) =================
class EvalInputs(unittest.TestCase):
    def test_bypass_traces_reach_the_evaluator_once(self):
        """test_engine::test_bypass_traces_go_to_reviewer_once (code-health axis L)."""
        e, fa, st, repo = make({B: [h.b_work(files={"hello.py": "try:\n    x()\nexcept Exception:\n    pass\n"}),
                                    h.b_work(files={"hello.py": "y = 1\n"}, done=[])],
                                E: [h.e_ans("REJECT", issues=[("high", "x")]), h.e_ans("READY")],
                                DV: [h.devil()]})
        e.run()
        p1, p2 = [c["prompt"] for c in fa.calls if c["role"] == E]
        self.assertIn("우회 흔적", p1)
        self.assertNotIn("새로 생긴 우회 흔적", p2)

    def test_files_past_the_diff_limit_are_named(self):
        """test_parallel::DiffCoverage — a file the diff could not carry is named so the evaluator opens it."""
        big = {"hello.py": "x = 1\n" * 4000}
        e, fa, st, _ = make({B: [h.b_work(files=big)], **ok_eval()}, cfg={"limits": {"review_diff_chars": 500}})
        e.run()
        self.assertIn("diff에 없음(길이 한도로 잘림)", [c for c in fa.calls if c["role"] == E][0]["prompt"])

    def test_every_call_points_at_its_skill_file(self):
        """test_engine::test_every_role_is_pointed_at_the_skill_files."""
        e, fa, st, _ = make({B: [h.b_work()], **ok_eval()})
        e.run()
        sk = {c["role"]: c["prompt"] for c in fa.calls}
        self.assertIn("skills/developer-mode.md", sk[B])
        self.assertIn("skills/audit-inspector.md", sk[E])
        self.assertIn("skills/audit-inspector.md", sk[DV])

    def test_work_and_verify_sides_use_their_own_adapters(self):
        """test_engine::test_work_and_verify_sides_use_separate_providers."""
        repo = h.make_repo()
        w, r = FakeAdapter({B: [h.b_work()]}), FakeAdapter(ok_eval())
        e = Engine(h.make_cfg(repo), h.make_request(extra=h.NO_QA), h.make_state(repo), w, r, out=lambda m: None)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual([c["role"] for c in w.calls], [B])
        self.assertEqual([c["role"] for c in r.calls], [E, DV])

    def test_capabilities_grant_web_per_role(self):
        """test_engine::test_capabilities_grant_tools_per_role."""
        e, fa, st, _ = make({B: [h.b_work()], **ok_eval()}, cfg={"capabilities": {"web_roles": ["evaluator"]}})
        e.run()
        by = {c["role"]: c for c in fa.calls}
        self.assertIn("WebSearch", by[E]["allowed_tools"])
        self.assertNotIn("WebSearch", by[E]["disallowed_tools"])
        self.assertIn("WebSearch", by[B]["disallowed_tools"])

    def test_doc_only_bundle_is_evaluated_against_its_source_but_not_played(self):
        """test_engine::test_doc_only_change_is_reviewed_against_its_source_but_not_played (v5 컷신 사고)."""
        e, fa, st, _ = make({B: [h.b_work(files={"docs/spec.md": "# 명세\n"})], **ok_eval()},
                            items=[h.item(target="docs/spec.md", location="docs/spec.md")], extra=h.NO_WIKI)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertFalse((st.dir / "03-evaluate").glob("e*_a*").__next__ if False else
                         list((st.dir / "03-evaluate").glob("e*_a*")))
        self.assertIn("시나리오 없음", [c for c in fa.calls if c["role"] == E][0]["prompt"])

    def test_failure_flows_are_asked_for_in_the_instructions(self):
        """test_engine::test_qa_without_failure_flow_is_auto_rejected (e2e run002: 3 normal-only scenarios). 10-03 정렬안
        A6: no engine floor any more — the instruction asks for failure flows; the rebuttal checks coverage."""
        p = prompts.eval_main({}, {"runner": "web", "environment": "external", "base_url": None, "cap": 20, "carry": ""})
        self.assertIn("실패 흐름", p)
        self.assertIn("완료 조건마다", p)
        self.assertIn("완료 조건마다", prompts.eval_devil({}, [], [], ""))

    def test_passed_without_evidence_is_shown_to_the_evaluator_as_a_fact(self):
        """test_engine::test_qa_passed_without_evidence_is_a_scenario_defect_fixed_by_the_qa_session (P6) → 10-03: the
        runner's demotion is a fact; the evaluator reads it in its results (no engine re-classification)."""
        r = [{"id": "S", "status": "BLOCKED", "note": "증거(캡처) 없는 PASSED → BLOCKED 강등. ok", "kind": "normal",
              "title": "S"}]
        block = prompts.results_block("d", r, "- S → BLOCKED", "(없음)", [])
        self.assertIn("증거(캡처) 없는 PASSED", block)
        self.assertIn("shot()", block)

    def test_report_shows_eval_rounds_cost_and_unknowns(self):
        """test_engine::test_report_rounds_are_not_shifted → v6 report (real cost · unknown count · eval log)."""
        e, fa, st, _ = make({B: [h.cut(), h.b_work()], **ok_eval()})
        e.run()
        rep = (st.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("비용 미상 호출 1회", rep)
        self.assertIn("| 1 |", rep)
        self.assertIn("끝 승인 대기", rep)


# ================= effort (test_v5::Effort · test_stage3) =================
def stub(level=1, visual=False, **cfg):
    base = {"high_floor": {"visual": True, "high_risk_paths": True, "contract_files": True},
            "high_risk_paths": ["src/Data/"], "contract_files": ["shared/Api.luau"]}
    base.update(cfg)
    return types.SimpleNamespace(cfg=base, req={"visual": visual, "kind": "시각" if visual else None}, run_level=level)


def it(target, level=1):
    return {"id": "DEV-001", "level": level, "target_files": [target]}


class Effort(unittest.TestCase):
    def test_table(self):
        self.assertEqual(effort.builder(stub(), [it("a.luau")]), "medium")
        self.assertEqual(effort.builder(stub(), [it("a.luau", 2)]), "high")
        self.assertEqual(effort.builder(stub(), [it("a.luau")], rework=True), "high")
        self.assertEqual(effort.builder(stub(), [it("src/Data/Save.luau")]), "high")    # high-risk path
        self.assertEqual(effort.builder(stub(), [it("shared/Api.luau")]), "high")       # contract file
        self.assertEqual(effort.builder(stub(visual=True), [it("a.luau")]), "high")
        self.assertEqual(effort.evaluator(stub(), "medium", [it("a.luau")]), "medium")
        self.assertEqual(effort.evaluator(stub(), "medium", [it("shared/Api.luau")]), "high")
        self.assertEqual(effort.evaluator(stub(visual=True), "medium"), "high")

    def test_the_floor_only_raises(self):
        self.assertEqual(effort.raise_to("xhigh", "high"), "xhigh")
        self.assertEqual(effort.raise_to(None, "high"), "high")
        self.assertEqual(effort.evaluator(stub(visual=True), "xhigh"), "xhigh")
        off = stub(visual=True, high_floor={"visual": False, "high_risk_paths": False, "contract_files": False})
        self.assertEqual(effort.builder(off, [it("src/Data/Save.luau")]), "medium")

    def test_high_risk_path_in_the_diff_brings_the_second_view(self):
        """test_engine::test_high_risk_path_raises_l1_item_to_dual_review (N18)."""
        e, fa, st, _ = make({B: [h.b_work(files={"src/Data/Save.py": "x\n"})], E: [h.e_ans("READY")],
                             "evaluator:second": [h.e_ans("READY", sid="S2")], DV: [h.devil()]},
                            items=[h.item(target="src/Data/Save.py", location="src/Data/Save.py")],
                            cfg={"high_risk_paths": ["src/Data/"]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        roles_ = [c["role"] for c in fa.calls]
        self.assertIn("evaluator:second", roles_)
        self.assertEqual([c["effort"] for c in fa.calls if c["role"] == B], ["high"])


# ================= contract files (test_v5::Contract) =================
class Contract(unittest.TestCase):
    def test_unmarked_item_is_refused_marked_one_runs_at_high_with_the_rule(self):
        repo = h.make_repo()
        cfg = h.make_cfg(repo, contract_files=["hello.py"])
        with self.assertRaises(RequestError):
            flow.decide(h.make_request(extra=h.NO_QA), cfg)
        # a contract file in the diff also brings the second view (N18)
        e, fa, st, _ = make({B: [h.b_work()], "evaluator:second": [h.e_ans("READY", sid="S2")], **ok_eval()},
                            items=[h.item(text="[약속 변경] 인사 함수에 인자 추가")], cfg={"contract_files": ["hello.py"]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        b = [c for c in fa.calls if c["role"] == B][0]
        self.assertIn("## 약속 파일", b["prompt"])
        self.assertEqual(b["effort"], "high")

    def test_problems(self):
        items = [{"id": "DEV-001", "text": "x", "targets": ["src/shared/"]},
                 {"id": "DEV-002", "text": "[약속 변경] y", "targets": ["src/shared/Api.luau"]},
                 {"id": "DEV-003", "text": "z", "targets": ["src/other.luau"]}]
        v = todo.contract_problems(items, ["src/shared/Api.luau"])
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0].startswith("DEV-001"))
        self.assertEqual(todo.contract_problems(items, []), [])


# ================= role definitions (test_v5::Roles) =================
class Roles(unittest.TestCase):
    def test_role_text_is_the_role_file_plus_the_engine_mode(self):
        root = DEFAULTS["rules_root"]
        self.assertEqual(roles.missing(root), [])
        self.assertIn("확인 전 단정 금지", roles.rules_text(root))
        t = roles.system_text("builder", root)
        self.assertTrue(t.startswith("# 역할 정의서 (roles/builder.md)"))
        self.assertLess(t.index("[엔진 모드 — 아래 정의서보다 우선"), t.index("## "))
        self.assertIn("착수 전", roles.ENGINE_HEAD)
        self.assertIn("「진전 없음」으로 반려", roles.ENGINE_MODE)
        self.assertNotIn("\n---\nname:", t)
        for key in ("evaluator", "evaluator:second", "evaluator:devil"):
            self.assertIn("roles/evaluator.md", roles.system_text(key, root))
        self.assertEqual(roles.system_text("nobody", root), "")

    def test_every_call_carries_its_role_and_can_be_switched_off(self):
        e, fa, st, _ = make({B: [h.b_work()], **ok_eval()})
        e.run()
        want = {B: "builder", E: "evaluator", DV: "evaluator"}
        for c in fa.calls:
            self.assertIn(f"roles/{want[c['role']]}.md", c["append_system"], c["role"])
            self.assertIn("# 작업 규칙 (WORKING_RULES.md)", c["append_system"], c["role"])
        e, fa, st, _ = make({B: [h.b_work()], **ok_eval()}, cfg={"role_definitions": False})
        e.run()  # the role file goes, the working rules stay
        self.assertTrue(all("# 역할 정의서" not in c["append_system"] and "# 작업 규칙" in c["append_system"]
                            for c in fa.calls))


# ================= evidence sheets · frames · runner caps (test_v5::Sheets) =================
def png(p, color="red", size=(64, 40)):
    from PIL import Image
    Image.new("RGB", size, color).save(p)


class Sheets(unittest.TestCase):
    def test_sheet_from_two_or_more_captures(self):
        from PIL import Image
        d = h.make_repo() / "ev"
        d.mkdir()
        for n, c in (("01.png", "red"), ("02.png", "green"), ("03.png", "blue")):
            png(d / n, c)
        self.assertEqual(qa.make_sheet(d, ["01.png", "02.png", "03.png"]), qa.SHEET)
        with Image.open(d / qa.SHEET) as im:
            self.assertEqual(im.size, (480 * 3, 300))
        self.assertIsNone(qa.make_sheet(d, ["01.png"]))
        (d / "02.png").write_bytes(b"not an image")
        self.assertEqual(qa.make_sheet(d, ["01.png", "02.png", "03.png"]), qa.SHEET)

    def test_caps(self):
        self.assertEqual(qa.caps("roblox"), {"multi_shot": "frames", "dev_shot": "studio_shot"})
        self.assertIsNone(qa.caps("web")["dev_shot"])
        self.assertEqual(qa.caps("nonexistent"), qa.CAPS["none"])
        self.assertEqual(set(qa.CAPS) - {"none"}, set(qa.RUN))

    def test_roblox_frames_and_manifest(self):
        s = dict(trr.scen("ok", "-- MARK_PASS"), frames=3, interval=0)
        r, state, qdir = trr.run([s, trr.scen("one", "-- MARK_PASS")])
        self.assertEqual(r["ok"]["evidence"], ["01.jpg", "02.jpg", "03.jpg"])
        self.assertEqual(r["one"]["evidence"], ["01.jpg"])
        d = h.make_repo() / "q"
        d.mkdir()
        files = qa.write_scenarios([dict(trr.scen("a", "x"), frames=4, interval=0.5, item_ids=["DEV-001"])], d, "roblox")
        self.assertEqual((files[0]["frames"], files[0]["interval"], files[0]["item_ids"]), (4, 0.5, ["DEV-001"]))

    def test_unreported_scenario_is_the_environments(self):
        """N10: the command runner's results.json leaves a scenario out → BLOCKED with env (never a code defect)."""
        d = h.make_repo() / "q2"
        (d / "evidence").mkdir(parents=True)
        (d / "results.json").write_text("[]", encoding="utf-8")
        got = qa._read_results(d / "results.json", [{"id": "S", "title": "S", "kind": "normal"}], d / "evidence")
        self.assertEqual((got[0]["status"], got[0].get("env")), ("BLOCKED", True))


# ================= the builder's own look (test_v5::DevShot) =================
def roblox_engine(visual=True, **over):
    repo = h.make_repo()
    cfg = h.make_cfg(repo, qa={"runner": "roblox"}, **over)
    extra = h.NO_WIKI + ("## 유형\n시각\n" if visual else "")
    fa = FakeAdapter({B: [h.b_work(files={"hello.py": "1\n"})] * 2, E: [h.e_ans("RUN")] * 2})
    st = h.make_state(repo)
    return Engine(cfg, h.make_request(extra=extra), st, fa, fa, sleep=lambda s: None, out=lambda m: None), fa, repo, st


class DevShot(unittest.TestCase):
    def test_only_on_a_visual_run_of_a_runner_that_offers_it(self):
        self.assertIs(roblox_engine()[0]._dev_shot(), studio_shot)
        self.assertIsNone(roblox_engine(visual=False)[0]._dev_shot())
        self.assertIsNone(roblox_engine(dev_shot=False)[0]._dev_shot())

    def test_the_builder_gets_one_command_and_one_draft_folder_evaluators_do_not(self):
        e, fa, repo, st = roblox_engine()
        allowed, env, extra = e._builder_tools()
        self.assertIn(f"Bash({studio_shot.command_prefix()}:*)", allowed)
        drafts = (repo / ".aw-shot").resolve().as_posix()  # absolute: `./` follows the session's cwd
        self.assertIn(f"Write({drafts}/**)", allowed)
        self.assertIn(f"Edit({drafts}/**)", allowed)       # measured 10-01: Write alone is denied
        self.assertIn("## 화면 확인", extra)
        cfgf = json.loads(Path(env[studio_shot.ENV]).read_text(encoding="utf-8"))
        self.assertEqual(cfgf["target"], repo.resolve().as_posix())
        self.assertIn("reset_code", cfgf)                 # v6 N14: the builder's looks clean up too
        e.s_planning()
        e._call(E, "p", kind="E1", schema=None, allowed=["Read"])
        kw = fa.calls[-1]
        self.assertFalse(any("studio_shot" in t or ".aw-shot" in t for t in kw["allowed_tools"]))

    def test_another_target_carries_nothing_of_roblox(self):
        """v5 §9: only the tools the work needs — a Godot run gets no Roblox tool, word or MCP."""
        repo = h.make_repo()
        cfg = h.make_cfg(repo, qa={"runner": "godot", "godot_exe": "godot.exe"})
        fa = FakeAdapter({B: [h.b_work()], E: [h.e_ans("READY")]})
        e = Engine(cfg, h.make_request(extra="## 유형\n시각\n"), h.make_state(repo), fa, fa, sleep=lambda s: None,
                   out=lambda m: None)
        self.assertIsNone(e._dev_shot())
        e.s_planning()
        allowed, env, extra = e._builder_tools()
        e._call(B, prompts_first(e), kind="first", schema=None, allowed=allowed, env=env, first_extra=extra)
        e._call(E, "p", kind="E1", schema=None, allowed=["Read"])
        for kw in fa.calls:
            text = kw["prompt"] + " ".join(kw["allowed_tools"]) + json.dumps(kw.get("env") or {})
            for word in ("studio_shot", ".aw-shot", "Rojo", "Luau", "mcp__"):
                self.assertNotIn(word, text, (kw["role"], word))

    def test_unrun_drafts_are_moved_out_of_the_project(self):
        e, fa, repo, st = roblox_engine()
        (repo / ".aw-shot").mkdir()
        (repo / ".aw-shot" / "DEV-001_1.luau").write_text("return {}", encoding="utf-8")
        e._sweep_shot_drafts()
        self.assertEqual(list((repo / ".aw-shot").iterdir()), [])
        self.assertEqual(len(list((st.dir / "dev-shots" / "leftover").iterdir())), 1)

    def test_take_only_from_the_draft_folder_and_move_it(self):
        repo = h.make_repo()
        c = {"target": str(repo), "shots_dir": str(repo.parent / f"{repo.name}_shots")}
        (repo / "src.luau").write_text("x", encoding="utf-8")
        moved, err = studio_shot._take("src.luau", c)
        self.assertIsNone(moved)
        (repo / ".aw-shot").mkdir()
        (repo / ".aw-shot" / "DEV-001_1.luau").write_text("return {}", encoding="utf-8")
        moved, err = studio_shot._take(".aw-shot/DEV-001_1.luau", c)
        self.assertIsNone(err)
        self.assertTrue(moved.is_file())

    def test_shoot_plays_captures_resets_and_always_stops(self):
        cfg, repo, qdir, files, sp = trr.setup([])
        cfg["qa"]["roblox"]["reset_code"] = "-- RESET_MARK\nreturn nil"
        c = studio_shot.config_for(cfg, repo, repo.parent / f"{repo.name}_shots")
        code = Path(c["shots_dir"]) / "001_x" / "code.luau"
        code.parent.mkdir(parents=True)
        code.write_text("-- MARK_PASS\nreturn { status = \"PASSED\", note = \"x\" }", encoding="utf-8")
        rc, text = studio_shot.shoot(c, code, frames=2, client_factory=lambda: StudioClient(c["mcp_command"], timeout=60))
        self.assertEqual(rc, 0, text)
        state = json.loads(sp.read_text(encoding="utf-8"))
        self.assertEqual(trr.plays(state), [True, False])
        self.assertEqual(sum(1 for x in state["calls"] if x.get("code_head", "").startswith("-- RESET_MARK")), 2)


def prompts_first(e):
    from agentworkflow import prompts
    return prompts.build_first(e.plan_rel, todo.parse(e._read_plan())[0])


class StudioLock(unittest.TestCase):
    def test_one_at_a_time_and_a_dead_owner_is_taken_over(self):
        d = h.make_repo()
        p = d / "studio.lock"
        with studio_capture.studio_lock("a", path=p):
            with self.assertRaises(studio_capture.Busy):
                with studio_capture.studio_lock("b", wait_sec=0, poll=0.01, path=p):
                    pass
        self.assertFalse(p.exists())
        self.assertTrue((d / "studio.lock.done").is_file())
        p.write_text(json.dumps({"pid": 999999, "who": "죽은 워커"}), encoding="utf-8")
        with studio_capture.studio_lock("c", wait_sec=5, path=p):
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["who"], "c")


# ================= usage pause · stream (test_v5::NotifyPause · test_resume_usage) =================
class UsagePause(unittest.TestCase):
    def test_below_the_thresholds_nothing_happens(self):
        e, fa, st, _ = make({B: [with_rate(h.b_work(), 0.5, 0.8)], **ok_eval()})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertIn("5h 50%", timeline(st))

    def test_five_hour_pause_starts_the_fallback_and_a_resume_redoes_nothing(self):
        h.FALLBACKS.clear()
        e, fa, st, repo = make({B: [with_rate(h.b_work(), five=0.96)], **ok_eval()})
        self.assertEqual(e.run(), "PAUSED_USAGE")
        self.assertEqual([c["role"] for c in fa.calls], [B])      # stopped BEFORE the next call
        self.assertEqual(len(h.FALLBACKS), 1)
        self.assertIn(f'wait-reset --run "{st.dir}"', st.d["pause_reason"])
        self.assertIn("[x] DEV-001", e._read_plan())               # the call's work is kept
        e2 = Engine(e.cfg, e.req, RunState.load(st.dir), fa, fa, out=lambda m: None)
        self.assertEqual(e2.run(), "AWAITING_APPROVAL")
        self.assertEqual([c["role"] for c in fa.calls].count(B), 1)

    def test_seven_day_window_stops_for_the_master(self):
        e, fa, st, _ = make({B: [with_rate(h.b_work(), seven=0.98)]})
        self.assertEqual(e.run(), "HELP")
        self.assertEqual(st.d["help_kind"], "usage7d")
        self.assertEqual(len(fa.calls), 1)

    def test_a_stale_reading_never_stops_a_resumed_run(self):
        repo = h.make_repo()
        st = h.make_state(repo)
        reset = int((dt.datetime.now() + dt.timedelta(days=2)).timestamp())
        st.d["usage"] = {"windows": {"seven_day": {"utilization": 0.99, "resetsAt": reset}}}
        st.save()
        e, fa, st, _ = make({B: [h.b_work()], **ok_eval()}, st=st, repo=repo)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")

    def test_a_window_past_its_reset_is_ignored(self):
        e, fa, st, _ = make({})
        past = int((dt.datetime.now() - dt.timedelta(minutes=5)).timestamp())
        st.d["usage"] = {"windows": {"five_hour": {"utilization": 0.99, "resetsAt": past}}}
        e._usage_gate(B)

    def test_first_reading_cancels_the_call_before_its_work(self):
        reset = int((dt.datetime.now() + dt.timedelta(hours=1)).timestamp())
        hot = {"five_hour": {"utilization": 0.96, "resetsAt": reset}}

        def first(kw):
            got = kw["on_rate"](hot, first=True)
            return got if got is not None else h.b_work()(kw)
        e, fa, st, _ = make({B: [first]})
        self.assertEqual(e.run(), "PAUSED_USAGE")
        self.assertIn("호출 시작 때 판정", st.d["pause_reason"])
        self.assertNotIn("[x] DEV-001", e._read_plan())

    @staticmethod
    def _cut_mid_work(window, u, seen):
        """A long builder call (run002: one call for a 9-item bundle, 25 min+): it has a session and has done part of
        the work when a usage reading arrives. seen gets what on_rate returned."""
        later = dt.timedelta(hours=1) if window == "five_hour" else dt.timedelta(days=2)
        reset = int((dt.datetime.now() + later).timestamp())

        def fn(kw):
            kw["on_init"]("SB")
            (Path(kw["cwd"]) / "hello.py").write_text("x = 1\n", encoding="utf-8")  # half of the item
            got = kw["on_rate"]({window: {"utilization": u, "resetsAt": reset}}, first=False)
            seen.append(got)
            return got if got is not None else h.b_work()(kw)
        return fn

    @staticmethod
    def _resumed(seen):
        def fn(kw):
            seen.append((kw.get("resume"), kw["prompt"].startswith(prompts.INTERRUPTED)))
            return h.b_work()(kw)
        return fn

    def test_run002_weekly_threshold_mid_call_stops_the_call_and_resume_continues_it(self):
        """10-06 run002: weekly 98% (threshold 97%) arrived mid-call and the call ran on — the engine only stopped
        before the NEXT call. Now the reading cuts the call at once; the session is kept for a resume (account swap)."""
        seen, back = [], []
        e, fa, st, repo = make({B: [self._cut_mid_work("seven_day", 0.98, seen), self._resumed(back)], **ok_eval()})
        self.assertEqual(e.run(), "HELP")
        self.assertEqual(st.d["help_kind"], "usage7d")
        self.assertIn("호출 중 판정", st.d["pause_reason"])
        self.assertIsNotNone(seen[0])                                  # the reading stopped the call
        self.assertNotIn("[x] DEV-001", e._read_plan())                # cut before it finished
        self.assertEqual(st.d["inflight"][B]["sid"], "SB")             # what a resume continues
        c = st.d["counters"]
        self.assertEqual((c["calls"], c.get("cost_unknown")), (1, 1))  # P16: the cut call counts, cost unknown
        self.assertIn("사용량 정지", timeline(st))
        self.assertEqual((repo / "hello.py").read_text(encoding="utf-8"), "x = 1\n")  # the work so far stays
        e2 = Engine(e.cfg, e.req, RunState.load(st.dir), fa, fa, out=lambda m: None)
        self.assertEqual(e2.run(), "AWAITING_APPROVAL")                # a new account: no old reading blocks it
        self.assertEqual(back, [("SB", True)])                         # same session, told it was cut

    def test_five_hour_threshold_mid_call_pauses_with_the_fallback(self):
        h.FALLBACKS.clear()
        seen = []
        e, fa, st, _ = make({B: [self._cut_mid_work("five_hour", 0.95, seen)]})
        self.assertEqual(e.run(), "PAUSED_USAGE")
        self.assertIn("호출 중 판정", st.d["pause_reason"])
        self.assertEqual(len(h.FALLBACKS), 1)                          # resumes itself after the reset
        self.assertEqual(st.d["inflight"][B]["sid"], "SB")

    def test_mid_call_below_the_threshold_lets_the_call_finish(self):
        """Mid-call the threshold itself decides (the per-call estimate is for starting a call, not for cutting one)."""
        seen = []
        e, fa, st, _ = make({B: [self._cut_mid_work("seven_day", 0.965, seen)], **ok_eval()})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(seen, [None])

    def test_wait_reset_and_the_fallback(self):
        st = h.make_state(h.make_repo())
        with self.assertRaises(SafetyError):
            waitreset.run(st.dir, out=lambda m: None)
        st.d["status"] = "PAUSED_USAGE"
        st.d["paused_usage"] = {"reset_epoch": time.time() - 120}
        st.save()
        out = []
        self.assertEqual(waitreset.run(st.dir, out=out.append, sleep=lambda s: None), 0)
        self.assertTrue(any("RESUME_READY" in m for m in out))

    def test_the_fallback_writes_its_log_whatever_the_console_encoding(self):
        """10-06~10-10: 3 of 11 fallback logs ended in UnicodeEncodeError 'cp949' — its stdout is the log FILE, so
        Python used the system encoding unless the parent shell had set UTF-8, and 「—」 is not in cp949."""
        st = h.make_state(h.make_repo())                  # RUNNING — not the pause that started it: the exit line
        log = st.dir / "waitreset.log"
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        env["PYTHONIOENCODING"] = "cp949"                 # what a shell without UTF-8 gives the fallback
        with open(log, "w", encoding="utf-8") as f:
            rc = subprocess.run([sys.executable, "-m", "agentworkflow.waitreset", "--fallback", str(st.dir),
                                 str(time.time() - 1), "123"], cwd=str(waitreset.PKG_ROOT), stdout=f, stderr=f,
                                env=env, timeout=60).returncode
        text = log.read_bytes().decode("utf-8", errors="replace")   # before the fix the file held cp949 bytes
        self.assertEqual(rc, 0, text)
        self.assertIn("대비책 종료 — 이미 재개됨", text)


class Stream(unittest.TestCase):
    RATE = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 0.47, "resetsAt": 1790527200},
        "seven_day": {"utilization": 0.8, "resetsAt": 1790665200}}}}
    OK = {"type": "result", "result": "ok", "structured_output": {"a": 1}, "is_error": False,
          "permission_denials": [], "total_cost_usd": 0.5, "usage": {}, "session_id": "s", "modelUsage": {}}

    def fake_cli(self, events, after_rate_sec=0.0):
        d = h.make_repo()
        (d / "events.json").write_text(json.dumps(events), encoding="utf-8")
        (d / "cli.py").write_text(
            "import json, sys, time, pathlib\nsys.stdin.read()\nd = pathlib.Path(sys.argv[1])\n"
            "for ev in json.loads((d / 'events.json').read_text(encoding='utf-8')):\n"
            "    print(json.dumps(ev), flush=True)\n"
            f"    if ev.get('type') == 'rate_limit_event': time.sleep({after_rate_sec})\n"
            "(d / 'finished').write_text('1')\n", encoding="utf-8")
        a = ClaudeAdapter(isolation=[])
        a.build_argv = lambda **kw: [sys.executable, str(d / "cli.py"), str(d)]
        return a, d

    def test_dated_weekly_reset(self):
        now = dt.datetime(2026, 9, 28, 2, 16)
        self.assertEqual(parse_reset("You've hit your weekly limit · resets Sep 29, 4pm (Asia/Seoul)", now),
                         dt.datetime(2026, 9, 29, 16, 0))

    def test_result_usage_rejected_reset_and_large_prompt(self):
        a, d = self.fake_cli([self.RATE, {"type": "assistant", "message": {}}, self.OK])
        res = a.run_agent(prompt="p" * 200000, cwd=".", model="m")
        self.assertEqual(res.structured, {"a": 1})
        self.assertEqual(res.rate["five_hour"]["utilization"], 0.47)
        argv = ClaudeAdapter(isolation=[]).build_argv(model="m", effort=None, allowed_tools=None,
                                                      disallowed_tools=None, permission_mode=None, add_dirs=None,
                                                      schema=None)
        self.assertIn("--include-partial-messages", argv)       # v6 D1 · T9
        limit = [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1790665200}},
                 {"type": "result", "result": "You've hit your weekly limit", "is_error": True, "session_id": "s"}]
        a, d = self.fake_cli(limit)
        with self.assertRaises(LimitError):
            a.run_agent(prompt="p", cwd=".", model="m")

    def test_on_rate_can_stop_the_call_before_its_work(self):
        a, d = self.fake_cli([self.RATE, self.OK], after_rate_sec=30)
        t0 = time.monotonic()
        with self.assertRaises(LimitError):
            a.run_agent(prompt="p", cwd=".", model="m", on_rate=lambda r, first=True: LimitError("x", None))
        self.assertLess(time.monotonic() - t0, 20)
        self.assertFalse((d / "finished").exists())

    def test_a_stop_kills_the_whole_process_tree(self):
        """run002: a cut call must not leave what the CLI started (a test run, a build) running on. Measured 10-06:
        Popen.kill() on Windows ends the parent only — its child lives on."""
        d = h.make_repo()
        (d / "cli.py").write_text(
            "import json, subprocess, sys, time, pathlib\nsys.stdin.read()\nd = pathlib.Path(sys.argv[1])\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "(d / 'child.pid').write_text(str(c.pid))\n"
            f"print(json.dumps({json.dumps(self.RATE)}), flush=True)\n"
            "time.sleep(60)\n", encoding="utf-8")
        a = ClaudeAdapter(isolation=[])
        a.build_argv = lambda **kw: [sys.executable, str(d / "cli.py"), str(d)]
        t0 = time.monotonic()
        with self.assertRaises(LimitError):
            a.run_agent(prompt="p", cwd=".", model="m", on_rate=lambda r, first=True: LimitError("x", None))
        # before the fix the stop itself waited 60 s: the live child held the pipes open (measured 10-06)
        self.assertLess(time.monotonic() - t0, 20)
        child = int((d / "child.pid").read_text())
        alive = str(child) in subprocess.run(["tasklist", "/FI", f"PID eq {child}"], capture_output=True,
                                             text=True).stdout
        if alive:
            subprocess.run(["taskkill", "/PID", str(child), "/F"], capture_output=True)
        self.assertFalse(alive)

    def test_idle_cut_returns_an_error_result_with_no_result_line(self):
        a, d = self.fake_cli([self.RATE, self.OK], after_rate_sec=30)
        res = a.run_agent(prompt="p", cwd=".", model="m", idle_timeout=2)
        self.assertTrue(res.is_error)
        self.assertFalse(res.has_result)
        self.assertEqual(res.timed_out, "무응답 2초")

    def test_lost_session_is_recognised(self):
        a, d = self.fake_cli([{"type": "result", "result": "No conversation found with session ID: x",
                               "is_error": True, "session_id": "x"}])
        res = a.run_agent(prompt="p", cwd=".", model="m", resume="x")
        self.assertTrue(res.no_session)


# ================= request kinds (test_v5::ConfigAndRequest) =================
class Request(unittest.TestCase):
    def test_kind_and_references(self):
        self.assertEqual(parse_kind("", ""), (None, []))
        self.assertEqual(parse_kind("시각 연출", ""), ("시각", []))
        self.assertEqual(parse_kind("원작 재현 — 컷신", "- `자료/rec2/sheet_03.jpg` 0:36~0:46\n- C:\\x\\y.png"),
                         ("원작 재현", ["자료/rec2/sheet_03.jpg", "C:\\x\\y.png"]))
        with self.assertRaises(RequestError):
            parse_kind("원작 재현", "")

    def test_visual_forces_qa_on(self):
        r = h.make_request(extra="## 흐름\n- QA: 없음\n\n## 유형\n시각\n")
        f = flow.decide(r, h.make_cfg(h.make_repo()))
        self.assertTrue(f["qa"])
        self.assertTrue(f["forced"])

    def test_config_checks(self):
        repo = h.make_repo()
        for over in ({"limits": {"usage_pause": {"fallback_min": -1}}}, {"contract_files": [""]},
                     {"high_floor": {"visual": "yes"}}, {"dev_shot": "yes"}, {"role_definitions": 1},
                     {"qa": {"runner": "roblox", "roblox": {"reset_code": ""}}}):
            with self.assertRaises(ConfigError, msg=over):
                h.make_cfg(repo, **over)


if __name__ == "__main__":
    unittest.main()
