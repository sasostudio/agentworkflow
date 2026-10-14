"""v6 (10-02 · 설계서 v6 §9 T8) — one test per cell of §3 (key lifecycle) and §4 (stop x resume), plus §5's rule rows
and the P5~P20 checklist. Fake AI (FakeAdapter keyed by session key) · real git repos · real QA command runner."""

import argparse
import json
import sys
import time
import unittest
from pathlib import Path

from agentworkflow import cli, engine as eng_mod, prompts, safety
from agentworkflow.adapters import ClaudeAdapter, FakeAdapter, Result
from agentworkflow.config import ConfigError, DEFAULTS, _merge, check_removed, validate
from agentworkflow.engine import Engine
from agentworkflow.request import RequestError, load_request

import helpers as h

B, E, S2, DV = "builder", "evaluator", "evaluator:second", "evaluator:devil"


def make(script, level=1, items=None, extra=h.NO_QA, cfg=None, st=None, repo=None):
    repo = repo or h.make_repo()
    c = h.make_cfg(repo, **(cfg or {}))
    req = h.make_request(level, items, extra)
    st = st or h.make_state(repo)
    fa = FakeAdapter(script)
    sleeps = []
    e = Engine(c, req, st, fa, fa, sleep=sleeps.append, out=lambda m: None)
    e.sleeps = sleeps
    return e, fa, st, repo


def calls(fa, key=None):
    return [c for c in fa.calls if key is None or c["role"] == key]


def kinds(st):
    """(session key, kind) of every recorded call, in order."""
    out = []
    for p in sorted((st.dir / "raw").glob("*.response.json")):
        x = json.loads(p.read_text(encoding="utf-8"))
        out.append((p.stem.split("_", 2)[2].replace(".response", "").replace("-", ":"), x["extra"].get("kind")))
    return out


def plan_text(e):
    return e.plan_path.read_text(encoding="utf-8")


def two_items():
    return [h.item("DEV-001", target="a.py", location="a.py"), h.item("DEV-002", target="b.py", location="b.py")]


# ================= §1 · §5 happy paths =================
class Flow(unittest.TestCase):
    def test_qa_off_wiki_off_build_eval_devil_gate_approve(self):
        e, fa, st, repo = make({B: [h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual([c["role"] for c in fa.calls], [B, E, DV])
        self.assertIn("[x] DEV-001", plan_text(e))
        self.assertEqual(st.d["stage"], "EVALUATE")
        self.assertEqual(st.d["eval_no"], 1)
        self.assertIsNotNone(st.snapshot("accepted"))  # devil READY
        self.assertNotIn("eval", st.d)  # §3 eval: deleted at the gate
        cfg = h.make_cfg(repo)
        cli.approve_gate(st, cfg, repo, "ok")
        self.assertEqual(st.d["stage"], "COMPLETE")
        e2 = Engine(cfg, e.req, st, fa, fa, out=lambda m: None)
        self.assertEqual(e2.run(), "COMPLETE")

    def test_qa_and_wiki_on_all_pass_skips_e2(self):
        e, fa, st, repo = make({B: [h.b_work(), h.b_wiki()], E: [h.e_ans("RUN", h.scen_set())],
                                DV: [h.devil()]}, extra="")
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(kinds(st), [(B, "first"), (B, "wiki"), (E, "E1"), (DV, "DEVIL")])
        self.assertIn("묶음 기록", (repo / "docs/wiki/log.md").read_text(encoding="utf-8"))
        res = json.loads(next((st.dir / "03-evaluate").glob("e1_a1/results_final.json")).read_text(encoding="utf-8"))
        self.assertEqual(res["verdict"], "PASSED")
        man = json.loads((st.dir / "03-evaluate/e1_a1/scenarios.json").read_text(encoding="utf-8"))
        self.assertEqual(man[0]["item_ids"], ["DEV-001"])  # N13: item_ids travel with the scenario

    def test_sessions_continue_per_key_devil_always_new(self):
        e, fa, st, _ = make({B: [h.b_work(done=["DEV-001"], files={"a.py": "1\n"}, remaining="DEV-002"),
                                 h.b_work(done=["DEV-002"], files={"b.py": "2\n"})],
                             E: [h.e_ans("READY")], DV: [h.devil()]}, items=two_items())
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        b = calls(fa, B)
        self.assertIsNone(b[0]["resume"])
        self.assertEqual(b[1]["resume"], "SB")  # S-1: same session, continued
        self.assertIn("# 이어서", b[1]["prompt"])
        self.assertNotIn("# 공통 맥락", b[1]["prompt"])  # a continued session already has it
        self.assertIsNone(calls(fa, DV)[0]["resume"])
        self.assertTrue(all(c["persist"] for c in fa.calls))  # S-2

    def test_schema_fixed_per_role(self):
        e, fa, st, _ = make({B: [h.b_work(), h.b_wiki()], E: [h.e_ans("RUN", h.scen_set())], DV: [h.devil()]},
                            extra="")
        e.run()
        self.assertTrue(all(c["schema"] is prompts.BUILD_SCHEMA for c in calls(fa, B)))  # S-6 incl. the wiki call
        self.assertTrue(all(c["schema"] is prompts.EVAL_SCHEMA for c in calls(fa, E) + calls(fa, DV)))
        self.assertTrue(all("Agent" in c["disallowed_tools"] for c in fa.calls))  # D3 · S-11


# ================= cost (C7 · S-9 · P16) =================
class Cost(unittest.TestCase):
    def test_cost_is_the_delta_of_a_continued_session(self):
        e, fa, st, _ = make({B: [h.b_work(done=["DEV-001"], files={"a.py": "1\n"}, cost=0.10),
                                 h.b_work(done=["DEV-002"], files={"b.py": "2\n"}, cost=0.25)],
                             E: [h.e_ans("READY", cost=0.30)], DV: [h.devil()]}, items=two_items())
        e.run()
        raw = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((st.dir / "raw").glob("*.response.json"))]
        self.assertAlmostEqual(raw[1]["cost_usd"], 0.15)
        self.assertAlmostEqual(raw[1]["cost_cum"], 0.25)
        self.assertAlmostEqual(st.d["counters"]["cost_usd"], 0.25 + 0.30 + 0.20)

    def test_cut_call_counts_as_a_call_with_unknown_cost(self):
        e, fa, st, _ = make({B: [h.cut(), h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]})
        e.run()
        c = st.d["counters"]
        self.assertEqual(c["cost_unknown"], 1)
        self.assertEqual(c["calls"], 4)


# ================= §5-1 BUILD =================
class Build(unittest.TestCase):
    def test_done_is_applied_before_the_question_stops(self):
        e, fa, st, _ = make({B: [h.b_work(question="A냐 B냐? 추천 A")]})
        self.assertEqual(e.run(), "AWAITING_ANSWER")
        self.assertIn("[x] DEV-001", plan_text(e))  # §5-1 step 4 ①②

    def test_middle_call_check_failure_is_information_only(self):
        e, fa, st, repo = make({B: [h.b_work(done=["DEV-001"], files={"a.py": "1\n", "FAIL": "fail"}),
                                    h.b_work(done=["DEV-002"], files={"b.py": "2\n", "FAIL": "ok"})],
                                E: [h.e_ans("READY")], DV: [h.devil()]},
                               items=[h.item("DEV-001", target="a.py, FAIL", location="a.py"),
                                      h.item("DEV-002", target="b.py, FAIL", location="b.py")])
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(st.d["counters"]["check_fails"], 0)  # B8
        self.assertIn("엔진 기계검사 (참고", calls(fa, B)[1]["prompt"])

    def test_last_call_check_failure_is_rework_check(self):
        e, fa, st, repo = make({B: [h.b_work(files={"hello.py": "1\n", "FAIL": "fail"}),
                                    h.b_work(files={"FAIL": "ok"}, done=[])],
                                E: [h.e_ans("READY")], DV: [h.devil()]},
                               items=[h.item(target="hello.py, FAIL")])
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(kinds(st)[:2], [(B, "first"), (B, "fix")])
        self.assertIn("기계검사", calls(fa, B)[1]["prompt"])
        self.assertEqual(st.d["counters"]["check_fails_total"], 1)

    def test_check_fail_cap_is_help(self):
        bad = h.b_work(files={"hello.py": "1\n", "FAIL": "fail"})
        e, fa, st, _ = make({B: [bad, h.b_work(files={"hello.py": "2\n", "FAIL": "fail"}, done=[]),
                                 h.b_work(files={"hello.py": "3\n", "FAIL": "fail"}, done=[])]},
                            items=[h.item(target="hello.py, FAIL")])
        self.assertEqual(e.run(), "HELP")
        self.assertEqual(st.d["help_kind"], "check")

    def test_outside_target_auto_reject_then_fix_carries_the_reason_and_clears_it(self):
        def undo(kw):
            (Path(kw["cwd"]) / "other.py").unlink()
        e, fa, st, _ = make({B: [h.b_work(files={"hello.py": "1\n", "other.py": "x\n"}),
                                 h.b_work(files={"hello.py": "2\n"}, done=["DEV-001"], extra=undo)],
                             E: [h.e_ans("READY")], DV: [h.devil()]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(st.d["rework"], [])  # P2: cleared after the call that carried it
        fix = calls(fa, B)[1]
        self.assertIn("other.py", fix["prompt"])
        delivered = json.loads(sorted((st.dir / "raw").glob("*builder.response.json"))[1]
                               .read_text(encoding="utf-8"))["extra"]["delivered"]
        self.assertEqual(len(delivered), 1)

    def test_no_progress_twice_is_an_auto_reject(self):
        idle = lambda: h.b_work(files={}, done=[], remaining="생각 중")  # noqa: E731
        e, fa, st, _ = make({B: [idle(), idle(), idle(), idle()]})
        self.assertEqual(e.run(), "HELP")  # N3: 2nd idle call = reject · 3 rejects = HELP
        self.assertEqual(st.d["help_kind"], "no_progress")
        self.assertEqual(len(calls(fa, B)), 4)

    def test_missing_write_gate_is_not_the_engines_to_reject(self):
        """정렬안 N1: the evaluator checks the write-gate claim against the code — the engine does not bounce it."""
        e, fa, st, _ = make({B: [h.b_work(gate="")], E: [h.e_ans("READY")], DV: [h.devil()]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual([k for _, k in kinds(st)], ["first", "E1", "DEVIL"])
        self.assertIn("작성게이트", calls(fa, E)[0]["prompt"])

    def test_end_checks_run_without_ai_when_tree_differs_from_build_ok_at(self):
        # P1: a resume after a crash between the last call and the end checks
        e, fa, st, repo = make({E: [h.e_ans("READY")], DV: [h.devil()]})
        from agentworkflow import todo
        e.s_planning()
        (repo / "hello.py").write_text("print(1)\n", encoding="utf-8")
        e._write_plan(todo.mark_done(plan_text(e), "DEV-001", todo.evidence_line(["hello.py"], "작성")))
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(calls(fa, B), [])
        self.assertIn("BUILD 끝 검사 통과", (st.dir / "timeline.md").read_text(encoding="utf-8"))

    def test_wiki_unchanged_is_the_evaluators_to_judge(self):
        """정렬안 A5: run003 spent a call on "code changed, wiki unchanged" after a one-line comment fix. Whether the
        wiki needed it is now judged in the evaluation (the wiki line is in its instructions)."""
        e, fa, st, _ = make({B: [h.b_work(), h.b_wiki(write=False)],
                             E: [h.e_ans("READY")], DV: [h.devil()]}, extra="## 흐름\n- QA: 없음\n")
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual([k for _, k in kinds(st)], ["first", "wiki", "E1", "DEVIL"])
        self.assertIn("wiki 계약 문서", calls(fa, E)[0]["prompt"])

    def test_wiki_call_writing_outside_the_wiki_is_rejected(self):
        e, fa, st, _ = make({B: [h.b_work(), h.b_wiki(stray="hello2.py")]}, extra="## 흐름\n- QA: 없음\n",
                            cfg={"limits": {"max_self_fix": 1}})
        self.assertEqual(e.run(), "HELP")
        self.assertTrue(any("wiki 밖" in r["reason"] for r in st.d["rework"]))


# ================= §3 inflight · S-3 · S-4 · S-5 · N8 =================
class Inflight(unittest.TestCase):
    def test_cut_with_progress_continues_the_same_session_uncounted(self):
        e, fa, st, _ = make({B: [h.cut(write={"hello.py": "half\n"}), h.b_work()],
                             E: [h.e_ans("READY")], DV: [h.devil()]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        second = calls(fa, B)[1]
        self.assertEqual(second["resume"], "SB")
        self.assertTrue(second["prompt"].startswith(prompts.INTERRUPTED))  # S-4
        self.assertEqual(e.sleeps, [])  # N8: not a counted retry

    def test_cut_without_progress_is_a_counted_retry_with_backoff(self):
        e, fa, st, _ = make({B: [h.cut(), h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(e.sleeps, [30])
        self.assertTrue(calls(fa, B)[1]["prompt"].startswith(prompts.INTERRUPTED))

    def test_inflight_without_sid_is_dropped_at_start(self):
        repo = h.make_repo()
        st = h.make_state(repo)
        st.d["inflight"] = {B: {"sid": None, "kind": "first", "started": "x", "delivered": []}}
        st.save()
        e, fa, st, _ = make({B: [h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]}, st=st, repo=repo)
        e.run()
        self.assertFalse(calls(fa, B)[0]["prompt"].startswith(prompts.INTERRUPTED))

    def test_devil_inflight_is_always_dropped(self):
        repo = h.make_repo()
        st = h.make_state(repo)
        st.d["inflight"] = {DV: {"sid": "old", "kind": "DEVIL", "started": "x", "delivered": []}}
        st.d.setdefault("work_sessions", {})[DV] = {"sid": "old", "t": 0, "last_cost": 0}
        st.save()
        e, fa, st, _ = make({B: [h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]}, st=st, repo=repo)
        e.run()
        self.assertIsNone(calls(fa, DV)[0]["resume"])

    def test_lost_session_starts_that_key_over_from_the_record(self):
        e, fa, st, _ = make({B: [h.b_work(done=["DEV-001"], files={"a.py": "1\n"}, remaining="b.py 남음"), h.gone(),
                                 h.b_work(done=["DEV-002"], files={"b.py": "2\n"}, sid="SB2")],
                             E: [h.e_ans("READY")], DV: [h.devil()]}, items=two_items())
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        third = calls(fa, B)[2]
        self.assertIsNone(third["resume"])
        self.assertIn("이전 작업 세션을 잃었다", third["prompt"])
        self.assertIn("b.py 남음", third["prompt"])
        self.assertEqual(st.d["work_sessions"][B]["sid"], "SB2")

    def test_init_line_records_the_session_before_any_result(self):
        e, fa, st, _ = make({B: [h.cut(sid="EARLY"), h.cut(sid="EARLY")]}, cfg={"limits": {"max_retries": 1}})
        e.run()
        self.assertEqual(st.d["work_sessions"][B]["sid"], "EARLY")  # S-3
        self.assertEqual(st.d["inflight"][B]["sid"], "EARLY")


# ================= §5-2 EVALUATE (10-03 정렬안 §3 — the main evaluator decides, the engine carries) =================
class Evaluate(unittest.TestCase):
    def test_reject_carries_to_builder_as_is_and_clears_eval(self):
        e, fa, st, _ = make({B: [h.b_work(), h.b_work(files={"hello.py": "fixed\n"}, done=[])],
                             E: [h.e_ans("REJECT", issues=[("high", "경계값 누락")],
                                         to_builder="hello.py:1 경계값 0을 처리하지 않음 — 0이면 빈 문자열을 낸다"),
                                 h.e_ans("READY")],
                             DV: [h.devil()]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(st.d["counters"]["eval_rejects"], 1)
        self.assertEqual(st.d["eval_no"], 2)
        fix = calls(fa, B)[1]
        self.assertIn("hello.py:1 경계값 0을 처리하지 않음 — 0이면 빈 문자열을 낸다", fix["prompt"])  # word for word
        self.assertEqual(calls(fa, E)[1]["resume"], "SE")  # the evaluator session continues across rounds
        self.assertIn("다시 평가", calls(fa, E)[1]["prompt"])  # it decides what to look at again
        self.assertEqual(st.d["rework"], [])

    def test_reject_without_to_builder_falls_back_to_its_issues(self):
        e, fa, st, _ = make({B: [h.b_work(), h.b_work(files={"hello.py": "x\n"}, done=[])],
                             E: [h.e_ans("REJECT", issues=[("high", "경계값 누락")]), h.e_ans("READY")],
                             DV: [h.devil()]})
        e.run()
        self.assertIn("경계값 누락", calls(fa, B)[1]["prompt"])

    def test_rework_runs_high_after_the_second_reject(self):
        rej = lambda: h.e_ans("REJECT", issues=[("high", "x")])  # noqa: E731
        e, fa, st, _ = make({B: [h.b_work(), h.b_work(files={"hello.py": "a\n"}, done=[]),
                                 h.b_work(files={"hello.py": "b\n"}, done=[])],
                             E: [rej(), rej(), h.e_ans("READY")], DV: [h.devil()]}, cfg={"limits": {"max_eval_rejects": 5}})
        e.run()
        self.assertEqual(calls(fa, B)[1]["effort"], "medium")
        self.assertEqual(calls(fa, B)[2]["effort"], "high")  # D4 · eval_rejects >= rework_high_after

    def test_eval_reject_cap_is_help_and_resume_is_refused(self):
        rej = lambda: h.e_ans("REJECT", issues=[("high", "x")])  # noqa: E731
        e, fa, st, repo = make({B: [h.b_work(), h.b_work(files={"hello.py": "a\n"}, done=[]),
                                    h.b_work(files={"hello.py": "b\n"}, done=[])],
                                E: [rej(), rej(), rej()]})
        self.assertEqual(e.run(), "HELP")
        self.assertEqual(st.d["help_kind"], "eval_cap")
        self.assertEqual(st.d["stage"], "BUILD")
        with self.assertRaises(safety.SafetyError):
            cli.cmd_resume(argparse.Namespace(run=str(st.dir), config=None))
        cli.reject_to_build(st, h.make_cfg(repo), repo, "방향: 이렇게")
        self.assertEqual(st.d["counters"]["eval_reject_cap"], 6)  # N7
        self.assertEqual(st.d["rework"][-1]["source"], "master_help")

    def test_master_reason_reaches_the_evaluator_too(self):
        """§6-1 H9 (implemented 10-03): run003's resume plan needs the Master's words at the evaluator, not only at the
        builder (a reason about the scenarios is the evaluator's to act on)."""
        rej = lambda: h.e_ans("REJECT", issues=[("high", "x")])  # noqa: E731
        e, fa, st, repo = make({B: [h.b_work(), h.b_work(files={"hello.py": "a\n"}, done=[]),
                                    h.b_work(files={"hello.py": "b\n"}, done=[])],
                                E: [rej(), rej(), rej()]})
        self.assertEqual(e.run(), "HELP")
        cfg = h.make_cfg(repo)
        cli.reject_to_build(st, cfg, repo, "코드 결함 없음 — 다음 플레이 시작값을 확인하는 시나리오를 보강")
        fa.script[B] = [h.b_work(files={"hello.py": "c\n"}, done=[])]
        fa.script[E] = [h.e_ans("READY")]
        fa.script[DV] = [h.devil()]
        self.assertEqual(Engine(cfg, e.req, st, fa, fa, out=lambda m: None).run(), "AWAITING_APPROVAL")
        p = calls(fa, E)[-1]["prompt"]
        self.assertIn("Master 반려 사유", p)
        self.assertIn("다음 플레이 시작값을 확인하는 시나리오를 보강", p)

    def test_eval_restarts_from_the_first_look_when_the_tree_moved(self):
        e, fa, st, repo = make({B: [h.b_work()], E: [h.e_ans("READY"), h.e_ans("READY")], DV: [h.devil()]})
        e.s_planning()
        while st.d["stage"] != "EVALUATE":
            e.s_build()
        st.d["eval"] = {"phase": "DEVIL", "attempt_dir": None, "snap": "old", "verdicts": {}}
        st.save()
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(st.d["eval_no"], 1)  # §3 eval_no: only BUILD→EVALUATE raises it
        self.assertEqual(calls(fa, E)[0]["role"], E)

    def test_an_old_phase_record_starts_over(self):
        e, fa, st, repo = make({B: [h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]})
        e.s_planning()
        while st.d["stage"] != "EVALUATE":
            e.s_build()
        st.d["eval"] = {"phase": "PRECHECK", "attempt_dir": None, "snap": e._tree(), "verdicts": {}}  # before 10-03
        st.save()
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(len(calls(fa, E)), 1)

    def test_eval_resumes_at_its_phase_when_the_tree_is_the_same(self):
        e, fa, st, repo = make({B: [h.b_work()], DV: [h.devil()]})
        e.s_planning()
        while st.d["stage"] != "EVALUATE":
            e.s_build()
        st.d["eval"] = {"phase": "DEVIL", "attempt_dir": None, "snap": e._tree(), "verdicts": {"E1": "READY"}}
        st.save()
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(calls(fa, E), [])

    def test_second_view_only_when_a_high_risk_file_changed_and_its_findings_go_to_the_main_evaluator(self):
        """정렬안 B9 + A3: the second view is called for a high-risk/contract change (not for L3 alone) and what it finds
        goes to the main evaluator, which decides — never straight to the builder."""
        e, fa, st, _ = make({B: [h.b_work(), h.b_work(files={"hello.py": "x\n"}, done=[])],
                             E: [h.e_ans("READY"), h.e_ans("REJECT", to_builder="입력 검증 추가(2번째 관점 지적 맞음)"),
                                 h.e_ans("READY")],
                             S2: [h.e_ans("REJECT", issues=[("high", "입력 검증 없음")], sid="S2"),
                                  h.e_ans("READY", sid="S2")],
                             DV: [h.devil()]}, cfg={"high_risk_paths": ["hello.py"]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(st.d["counters"]["eval_rejects"], 1)
        to_main = calls(fa, E)[1]["prompt"]
        self.assertIn("2번째 관점", to_main)
        self.assertIn("입력 검증 없음", to_main)
        self.assertIn("입력 검증 추가(2번째 관점 지적 맞음)", calls(fa, B)[1]["prompt"])
        self.assertNotIn("입력 검증 없음", calls(fa, B)[1]["prompt"])  # the engine did not route the second view's words
        self.assertEqual(calls(fa, S2)[1]["resume"], "S2")  # hello.py changed since it looked → it looks again

    def test_l3_alone_does_not_call_the_second_view(self):
        e, fa, st, _ = make({B: [h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]}, level=3)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(calls(fa, S2), [])

    def test_unsure_goes_to_the_gate_without_other_eyes(self):
        e, fa, st, _ = make({B: [h.b_work()], E: [h.e_ans("UNSURE", reason="화면 확인 필요")]}, level=3)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(calls(fa, DV), [])
        self.assertEqual(st.d["gate_pending"]["unsure"], "화면 확인 필요")

    def test_the_evaluator_runs_then_sees_the_results_in_its_session_then_decides(self):
        e, fa, st, _ = make({B: [h.b_work()], E: [h.e_ans("RUN", h.scen_set(code="FAILME")),
                                                  h.e_ans("UNSURE", reason="두 번 플레이해야 보임")]},
                            extra=h.NO_WIKI)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertTrue(any((st.dir / "03-evaluate").glob("e1_a1/results_final.json")))
        second = calls(fa, E)[1]
        self.assertEqual(second["resume"], "SE")
        self.assertIn("네 시나리오 실행 결과", second["prompt"])
        self.assertIn("FAILED", second["prompt"])
        self.assertEqual(calls(fa, DV), [])
        self.assertEqual(st.d["gate_pending"]["unsure"], "두 번 플레이해야 보임")

    def test_no_scenario_floor_the_evaluator_decides_how_many(self):
        only_fail = [h.scenario("F1", kind="failure")]
        e, fa, st, _ = make({B: [h.b_work()], E: [h.e_ans("RUN", only_fail)], DV: [h.devil()]}, extra=h.NO_WIKI)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual([k for _, k in kinds(st)], ["first", "E1", "DEVIL"])

    def test_a_precheck_problem_goes_back_as_information(self):
        bad = [h.scenario("S1", code="   ")]
        e, fa, st, _ = make({B: [h.b_work()], E: [h.e_ans("RUN", bad), h.e_ans("RUN", h.scen_set())],
                             DV: [h.devil()]}, extra=h.NO_WIKI)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual([k for _, k in kinds(st)], ["first", "E1", "E2", "DEVIL"])
        self.assertIn("실행 전 검사", calls(fa, E)[1]["prompt"])
        self.assertIn("비어 있는 코드", calls(fa, E)[1]["prompt"])

    def test_scenario_cap_is_a_bound_only(self):
        items = [h.item(f"DEV-{i:03d}", target=f"m{i}.py", location=f"m{i}.py") for i in range(1, 41)]
        e2, *_ = make({}, items=items, level=3)
        e2.s_planning()
        self.assertEqual(e2._scenario_cap(), 100)
        e1, *_ = make({})
        e1.s_planning()
        self.assertEqual(e1._scenario_cap(), max(e1.cfg["qa"]["max_scenarios"], 3))
        self.assertFalse(hasattr(e1, "_scenario_limits"))  # 정렬안 A6: no floor

    def test_the_evaluator_fixes_its_own_scenario_and_runs_again(self):
        e, fa, st, _ = make({B: [h.b_work()],
                             E: [h.e_ans("RUN", h.scen_set(code="BROKEN")), h.e_ans("RUN", h.scen_set())],
                             DV: [h.devil()]}, extra=h.NO_WIKI)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual([k for _, k in kinds(st)], ["first", "E1", "E2", "DEVIL"])
        self.assertTrue((st.dir / "03-evaluate/e1_a2/results_final.json").is_file())
        self.assertEqual(st.d["counters"]["eval_rejects"], 0)

    def test_an_environment_claim_is_not_overruled_repeats_end_in_help(self):
        """정렬안 §4: the engine no longer turns "environment" into "code defect" (`_cross_check`). The evaluator asks
        to play again (RUN, no scenarios = the same ones); repeating hits the RUN cap → HELP to the Master."""
        again = lambda: h.e_ans("RUN", [], reason="Studio 쪽 문제로 보임 — 다시 돌림")  # noqa: E731
        e, fa, st, _ = make({B: [h.b_work()], E: [h.e_ans("RUN", h.scen_set(code="FAILME")), again(), again(),
                                                  again()]}, extra=h.NO_WIKI)
        self.assertEqual(e.run(), "HELP")
        self.assertEqual(st.d["help_kind"], "eval_runs")
        self.assertEqual(st.d["counters"].get("eval_rejects", 0), 0)
        self.assertEqual(len(list((st.dir / "03-evaluate").glob("e1_a*"))), 3)  # decision 10-03: 3 runs
        # the Master fixed the environment and resumes: the counter starts over and the stopped run is played first
        cli.reset_on_resume(st)
        self.assertEqual(st.d["eval"]["runs"], 0)
        fa.script[E] = [h.e_ans("READY")]
        fa.script[DV] = [h.devil()]
        self.assertEqual(Engine(e.cfg, e.req, st, fa, fa, out=lambda m: None).run(), "AWAITING_APPROVAL")
        self.assertEqual(len(list((st.dir / "03-evaluate").glob("e1_a*"))), 4)

    def test_no_capture_scenario_passes_through_the_engine_and_is_carried(self):
        """10-03: a declared `no_capture` survives the command runner too (all runners share the evidence rule), the
        result says 캡처 생략, and the next evaluation's carry-over keeps the declaration."""
        nc = dict(h.scenario("N_kick", code="PASS"), no_capture="퇴장 뒤 데이터만 확인")
        e, fa, st, _ = make({B: [h.b_work()], E: [h.e_ans("RUN", [nc])], DV: [h.devil()]}, extra=h.NO_WIKI)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        res = json.loads((st.dir / "03-evaluate/e1_a1/results_final.json").read_text(encoding="utf-8"))["results"]
        self.assertEqual(res[0]["status"], "PASSED")
        self.assertIn("캡처 생략", res[0]["note"])
        self.assertEqual(e._scenarios_from(st.dir / "03-evaluate/e1_a1")[0]["no_capture"], "퇴장 뒤 데이터만 확인")
        self.assertIn("no_capture", prompts.eval_main({}, {"runner": "web", "environment": "external", "base_url": None,
                                                           "cap": 6, "carry": ""}))

    def test_runner_flagged_environment_retries_from_run_then_help(self):
        e, fa, st, _ = make({B: [h.b_work()], E: [h.e_ans("RUN", h.scen_set(code="BLOCK"))]},
                            extra=h.NO_WIKI, cfg={"qa": {"command": "exit 0"}})
        self.assertEqual(e.run(), "HELP")
        self.assertEqual(st.d["help_kind"], "env")  # §4 ⑨ — the runner's own fact, retried
        self.assertEqual(len(list((st.dir / "03-evaluate").glob("e1_a*"))), 4)  # every retry = a new attempt

    def test_run003_replay_rebuttal_findings_go_to_the_main_evaluator(self):
        """run003 (10-03): the rebuttal named a missing scenario (completion condition 3) next to a low code note. The
        engine sent both to the builder, who cannot write scenarios → 3 rejects → HELP. Now both go to the main
        evaluator with the results it never saw (all passed), and it adds the scenario — no rework, no reject."""
        more = h.scen_set() + [h.scenario("N_next_play", code="PASS EVIDENCE")]
        e, fa, st, _ = make({B: [h.b_work()],
                             E: [h.e_ans("RUN", h.scen_set()), h.e_ans("RUN", more)],
                             DV: [h.devil("REJECT", issues=[("medium", "완료 조건 3을 확인하는 시나리오가 없다"),
                                                            ("low", "주석 낡음")])]}, extra=h.NO_WIKI)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual([k for _, k in kinds(st)], ["first", "E1", "DEVIL", "E2"])
        self.assertEqual(len(calls(fa, B)), 1)
        self.assertEqual(st.d["counters"].get("eval_rejects", 0), 0)
        to_main = calls(fa, E)[1]["prompt"]
        self.assertIn("반박 검토", to_main)
        self.assertIn("완료 조건 3을 확인하는 시나리오가 없다", to_main)
        self.assertIn("네 시나리오 실행 결과", to_main)  # the results it skipped (all passed) come along
        self.assertTrue((st.dir / "03-evaluate/e1_a2/results_final.json").is_file())

    def test_a_rebuttal_code_finding_reaches_the_builder_through_the_main_evaluator(self):
        e, fa, st, _ = make({B: [h.b_work(), h.b_work(files={"hello.py": "x\n"}, done=[])],
                             E: [h.e_ans("READY"), h.e_ans("REJECT", to_builder="완료 조건 2 구현 — hello.py에 추가"),
                                 h.e_ans("READY")],
                             DV: [h.devil("REJECT", issues=[("high", "완료 조건 2 빠짐")])]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertEqual(st.d["counters"]["eval_rejects"], 1)
        self.assertIn("완료 조건 2 구현 — hello.py에 추가", calls(fa, B)[1]["prompt"])
        self.assertEqual(len(calls(fa, DV)), 1)  # 결정 10-03 (나): the second pass did not ask for it again

    def test_rebuttal_again_only_when_the_evaluator_asks(self):
        e, fa, st, repo = make({B: [h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        cfg = h.make_cfg(repo)
        for want, n_devil in ((False, 1), (True, 2)):
            cli.reject_to_build(st, cfg, repo, "다시")
            fa.script[B] = [h.b_work(files={"hello.py": f"{want}\n"}, done=[])]
            fa.script[E] = [h.e_ans("READY", want_rebuttal=want)]
            fa.script[DV] = [h.devil()]
            self.assertEqual(Engine(cfg, e.req, st, fa, fa, out=lambda m: None).run(), "AWAITING_APPROVAL")
            self.assertEqual(len(calls(fa, DV)), n_devil, want)

    def test_one_answer_shape_and_the_medium_low_rule(self):
        """run003 ②: v5's "medium·low만이면 통과" was lost in v6 — the rebuttal rejected on medium/low. And T4's prompt
        contradiction cannot come back: one schema, its fields described once (REVIEW_RULES), in every prompt."""
        self.assertIn("medium·low만 있으면", prompts.REVIEW_RULES)
        self.assertNotIn("READY로 두고", prompts.REVIEW_RULES)
        self.assertEqual(set(prompts.EVAL_SCHEMA["properties"]),
                         {"verdict", "to_builder", "issues", "reason", "checked", "scenarios", "want_rebuttal",
                          "question"})
        self.assertNotIn("kind", prompts.EVAL_SCHEMA["properties"]["issues"]["items"]["properties"])
        ctx = {"changed": "a", "diff": "", "omitted": [], "check_log": "", "summary": "", "bypass": "", "denied": "",
               "since": "x"}
        qinfo = {"runner": "web", "environment": "external", "base_url": None, "cap": 6, "carry": ""}
        ps = {"main": prompts.eval_main(ctx, qinfo), "main_noqa": prompts.eval_main(ctx, None),
              "followup": prompts.eval_followup(["x"], True), "second": prompts.eval_second(ctx),
              "devil": prompts.eval_devil(ctx, [], [], "")}
        for name, p in ps.items():
            self.assertIn(prompts.REVIEW_RULES, p, name)
            for gone in ("PENDING", "`classes`", "## 결과 칸"):
                self.assertNotIn(gone, p, (name, gone))

    def test_next_evaluation_reviews_only_since_the_accepted_snapshot(self):
        e, fa, st, repo = make({B: [h.b_work(), h.b_work(files={"extra.py": "1\n"})],
                                E: [h.e_ans("READY"), h.e_ans("READY")], DV: [h.devil(), h.devil()]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        cfg = h.make_cfg(repo)
        cli.approve_gate(st, cfg, repo, "ok", ["extra.py"], ["추가 기능"])
        e2 = Engine(cfg, e.req, st, fa, fa, out=lambda m: None)
        self.assertEqual(e2.run(), "AWAITING_APPROVAL")
        p = calls(fa, E)[1]["prompt"]
        self.assertIn("직전 통과", p)
        self.assertIn("extra.py", p.split("## diff", 1)[0])
        self.assertNotIn("hello.py", p.split("## 평가 대상", 1)[1].split("##", 1)[0])

    def test_carry_over_names_the_last_finished_attempt(self):
        e, fa, st, _ = make({B: [h.b_work(), h.b_work(files={"hello.py": "x\n"}, done=[])],
                             E: [h.e_ans("RUN", h.scen_set()), h.e_ans("REJECT", to_builder="x 고칠 것"),
                                 h.e_ans("RUN", h.scen_set())],
                             DV: [h.devil("REJECT", issues=[("high", "x")])]}, extra=h.NO_WIKI)
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        self.assertIn("e1_a1", calls(fa, E)[2]["prompt"])  # P3: carried over from the latest finished attempt


# ================= §4 Master commands =================
class Master(unittest.TestCase):
    def _gate(self):
        e, fa, st, repo = make({B: [h.b_work()], E: [h.e_ans("READY")], DV: [h.devil()]})
        self.assertEqual(e.run(), "AWAITING_APPROVAL")
        return e, fa, st, repo, h.make_cfg(repo)

    def test_approve_refused_when_the_tree_changed_after_the_gate(self):
        e, fa, st, repo, cfg = self._gate()
        (repo / "hello.py").write_text("changed later\n", encoding="utf-8")
        with self.assertRaises(safety.SafetyError):
            cli.approve_gate(st, cfg, repo, "ok")  # P12

    def test_unsure_approve_records_the_accepted_snapshot(self):
        e, fa, st, repo = make({B: [h.b_work()], E: [h.e_ans("UNSURE", reason="?")]})
        e.run()
        st.drop_snapshot("accepted")
        cli.approve_gate(st, h.make_cfg(repo), repo, "봤음")
        self.assertIsNotNone(st.snapshot("accepted"))  # P10

    def test_gate_reject_appends_to_rework_raises_cap_goes_build(self):
        e, fa, st, repo, cfg = self._gate()
        eng_mod.add_rework(st, "평가자 지적", "eval")
        cli.reject_to_build(st, cfg, repo, "버튼 위치 다름")
        self.assertEqual([r["source"] for r in st.d["rework"]], ["eval", "master_gate"])  # P11
        self.assertEqual(st.d["stage"], "BUILD")
        self.assertEqual(st.d["counters"]["eval_reject_cap"], 3)
        self.assertNotIn("gate_pending", st.d)

    def test_follow_up_adds_an_item_and_builds_it(self):
        e, fa, st, repo, cfg = self._gate()
        cli.approve_gate(st, cfg, repo, "", ["more.py"], ["more 기능"])
        fa.script[B] = [h.b_work(files={"more.py": "1\n"})]
        fa.script[E] = [h.e_ans("READY")]
        fa.script[DV] = [h.devil()]
        self.assertEqual(Engine(cfg, e.req, st, fa, fa, out=lambda m: None).run(), "AWAITING_APPROVAL")
        self.assertIn("[x] FIX-001", plan_text(e))

    def test_bad_follow_up_is_help_and_not_reapplied(self):
        e, fa, st, repo, cfg = self._gate()
        st.d["follow_ups"] = [{"target": "x.py", "text": "t", "after": "DEV-999"}]
        st.d["gate_pending"] = None
        st.d["status"] = "RUNNING"
        st.save()
        self.assertEqual(Engine(cfg, e.req, st, fa, fa, out=lambda m: None).run(), "HELP")
        self.assertEqual(st.d["help_kind"], "follow_up")
        self.assertEqual(st.d["follow_ups"], [])  # P7

    def test_answer_reaches_builder_and_evaluator(self):
        e, fa, st, repo = make({B: [h.b_work(question="A냐 B냐")]})
        self.assertEqual(e.run(), "AWAITING_ANSWER")
        cfg = h.make_cfg(repo)
        cli.answer_question(st, cfg, repo, "A로")
        fa.script[E] = [h.e_ans("READY")]
        fa.script[DV] = [h.devil()]
        self.assertEqual(Engine(cfg, e.req, st, fa, fa, out=lambda m: None).run(), "AWAITING_APPROVAL")
        self.assertIn("A: A로", calls(fa, E)[0]["prompt"])
        self.assertIn("A: A로", calls(fa, DV)[0]["prompt"])

    def test_v5_run_is_refused_first(self):
        repo = h.make_repo()
        st = h.make_state(repo)
        st.d["stage"] = "DEVELOPMENT"
        st.save()
        with self.assertRaises(safety.SafetyError):
            cli.refuse_v5(st.dir)
        with self.assertRaises(RuntimeError):  # the engine's own second line
            Engine(h.make_cfg(repo), h.make_request(), st, FakeAdapter({}), out=lambda m: None).run()

    def test_question_cap_is_help_and_can_still_be_answered(self):
        q = lambda: h.b_work(files={}, done=[], question="?")  # noqa: E731
        e, fa, st, repo = make({B: [q()]}, cfg={"limits": {"max_questions": 1}})
        e.run()
        cfg = e.cfg
        cli.answer_question(st, cfg, repo, "a")
        fa.script[B] = [q()]
        Engine(cfg, e.req, st, fa, fa, out=lambda m: None).run()
        self.assertEqual(st.d["status"], "HELP")
        self.assertTrue(cli.can_answer(st))


# ================= usage (S-8 · P18) =================
class Usage(unittest.TestCase):
    def test_rise_goes_to_the_last_calls_key_and_five_hour_is_capped(self):
        e, fa, st, _ = make({})
        reset = int(time.time()) + 3600
        rate = lambda u: Result(rate={"five_hour": {"utilization": u, "resetsAt": reset}})  # noqa: E731
        e._note_usage(rate(0.10), B)
        e._note_usage(rate(0.40), E)  # the 30% rise was the builder's call
        per = st.d["usage"]["per_call"]
        self.assertEqual(per[B]["five_hour"], 0.10)  # per_call_cap
        self.assertNotIn(E, per)

    def test_threshold_pauses_with_a_fallback(self):
        e, fa, st, _ = make({B: [h.b_work()]})
        st.d["usage"] = {"windows": {"five_hour": {"utilization": 0.99, "resetsAt": time.time() + 600}}}
        st.save()
        h.FALLBACKS.clear()
        with self.assertRaises(eng_mod.Stop) as cm:
            e._usage_gate(B)
        self.assertEqual(cm.exception.status, "PAUSED_USAGE")
        self.assertEqual(len(h.FALLBACKS), 1)


# ================= config · request (§6-3 · A3) =================
class Config(unittest.TestCase):
    def test_removed_keys_are_refused_with_a_hint(self):
        for raw in ({"use_seed": False}, {"models": {"developer": {"model": "x"}}},
                    {"limits": {"usage_pause": {"mode": "wait"}}}, {"limits": {"parallel_dev": True}},
                    {"levels": {"dev_review": {}}}, {"master_gates": []}):
            with self.assertRaises(ConfigError):
                check_removed(raw)

    def test_idle_must_not_exceed_total(self):
        cfg = _merge(DEFAULTS, {"target": "x", "limits": {"idle_timeout_sec": 900, "call_total_sec": 600}})
        with self.assertRaises(ConfigError):
            validate(cfg)

    def test_request_removed_sections_and_old_flow_line(self):
        _, p = h.make_request(path=True)
        txt = p.read_text(encoding="utf-8")
        p.write_text(txt + "\n## 실행 방식\n- 병렬\n", encoding="utf-8")
        with self.assertRaises(RequestError):
            load_request(p)
        _, p = h.make_request(path=True, extra="## 흐름\n- 흐름: 단순\n")
        e, *_ = make({}, extra="")
        from agentworkflow import flow
        with self.assertRaises(RequestError):
            flow.decide(load_request(p), e.cfg)


# ================= adapter (S-3 · S-7 · T9) =================
class Adapter(unittest.TestCase):
    def _run(self, src, idle, total, seen=None):
        return ClaudeAdapter._stream([sys.executable, "-c", src], "hi", ".", idle, total, None, None,
                                     (seen.append if seen is not None else None))

    def test_init_callback_and_partial_lines_not_kept(self):
        src = ("import json,sys,time\nsys.stdin.read()\n"
               "print(json.dumps({'type':'system','subtype':'init','session_id':'S1'}),flush=True)\n"
               "for i in range(3):\n print(json.dumps({'type':'stream_event'}),flush=True); time.sleep(0.2)\n"
               "print(json.dumps({'type':'result','result':'ok','session_id':'S1'}),flush=True)\n")
        seen = []
        r, stop, why, sid = self._run(src, 5, 30, seen)
        self.assertEqual((seen, sid, why), (["S1"], "S1", None))
        self.assertEqual(len(r.stdout.splitlines()), 2)

    def test_idle_cut_keeps_the_session_id(self):
        src = ("import json,sys,time\nsys.stdin.read()\n"
               "print(json.dumps({'type':'system','subtype':'init','session_id':'S2'}),flush=True)\ntime.sleep(30)\n")
        r, stop, why, sid = self._run(src, 2, 60)
        self.assertIsNone(r)
        self.assertEqual((why, sid), ("무응답 2초", "S2"))

    def test_total_cut_while_lines_keep_coming(self):
        src = ("import json,sys,time\nsys.stdin.read()\n"
               "while True:\n print(json.dumps({'type':'stream_event'}),flush=True); time.sleep(0.3)\n")
        r, stop, why, sid = self._run(src, 2, 3)
        self.assertEqual(why, "총 상한 3초")


# ================= roblox reset_code (N14) =================
class RobloxReset(unittest.TestCase):
    def test_reset_runs_before_and_after_each_play_even_on_failure(self):
        import test_roblox_runner as tr
        cfg, repo, qdir, files, sp = tr.setup([tr.scen("ok", "-- MARK_PASS"), tr.scen("bad", "-- MARK_FAIL")])
        cfg["qa"]["roblox"]["reset_code"] = "-- RESET_MARK\nreturn nil"
        from agentworkflow import qa
        qa.RUN["roblox"](cfg, repo, qdir, files)
        st = json.loads(sp.read_text(encoding="utf-8"))
        seq = [("play" if c.get("is_start") else "stop") if c["tool"] == "start_stop_play"
               else ("reset" if c.get("code_head", "").startswith("-- RESET_MARK") else "run")
               for c in st["calls"] if c["tool"] in ("start_stop_play", "execute_luau")
               and c.get("datamodel_type") != "Edit"]
        self.assertEqual(seq, ["play", "reset", "run", "reset", "stop"] * 2)


if __name__ == "__main__":
    unittest.main()
