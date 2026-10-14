"""State model — derive "what changed / what is reviewed" from snapshots and trees instead of lists and flags.
v6 (10-02) keeps the two regressions this file guarded in v4/v5, on the v6 path:
  §23-12 #6: a question after every item was checked must still go through the end checks and the evaluation
  write-ahead: a call stopped by the usage limit after writing files must not lose that work on resume (S-4)."""
import datetime as dt
import unittest
from pathlib import Path

from agentworkflow import cli
from agentworkflow.adapters import FakeAdapter, LimitError
from agentworkflow.engine import Engine
from agentworkflow.state import RunState

import helpers as h


def make(repo, script, state=None):
    cfg = h.make_cfg(repo)
    st = state or h.make_state(repo)
    fake = FakeAdapter(script)
    return Engine(cfg, h.make_request(1, extra=h.NO_QA), st, fake, fake, sleep=lambda s: None,
                  out=lambda m: None), fake, st


class StateModel(unittest.TestCase):
    def test_question_after_all_items_checked_still_gets_checks_and_evaluation(self):
        repo = h.make_repo()
        e, fake, st = make(repo, {"builder": [h.b_work(question="로그 형식을 정해 줘")]})
        self.assertEqual(e.run(), "AWAITING_ANSWER")
        self.assertIn("[x] DEV-001", e.plan_path.read_text(encoding="utf-8"))
        st2 = RunState.load(st.dir)
        cli.answer_question(st2, e.cfg, repo, "JSON 한 줄")
        e2, fake2, _ = make(repo, {"evaluator": [h.e_ans("READY")], "evaluator:devil": [h.devil()]}, state=st2)
        self.assertEqual(e2.run(), "AWAITING_APPROVAL")
        self.assertIn("BUILD 끝 검사 통과", (st.dir / "timeline.md").read_text(encoding="utf-8"))
        self.assertEqual([c["role"] for c in fake2.calls], ["evaluator", "evaluator:devil"])

    def test_limit_after_writing_keeps_the_work_and_continues_the_session(self):
        repo = h.make_repo()
        first = {"n": 0}

        def writes_then_limit(kw):
            first["n"] += 1
            kw["on_init"]("SB")
            Path(kw["cwd"], "hello.py").write_text("print(1)\n", encoding="utf-8")
            return LimitError("hit", reset_at=dt.datetime.now() + dt.timedelta(minutes=1))
        e, fake, st = make(repo, {"builder": [writes_then_limit]})
        self.assertEqual(e.run(), "PAUSED_USAGE")
        self.assertEqual(st.d["inflight"]["builder"]["sid"], "SB")  # S-3: the session is known
        st2 = RunState.load(st.dir)
        # the resumed call finds hello.py already written (same content) and reports it as this item's file
        e2, fake2, _ = make(repo, {"builder": [h.b_work(files={"hello.py": "print(1)\n"})], "evaluator": [h.e_ans("READY")],
                                   "evaluator:devil": [h.devil()]}, state=st2)
        self.assertEqual(e2.run(), "AWAITING_APPROVAL")
        b = fake2.calls[0]
        self.assertEqual(b["resume"], "SB")
        self.assertIn("중간에 끊겼다", b["prompt"])  # S-4
        self.assertIn("[x] DEV-001", e2.plan_path.read_text(encoding="utf-8"))  # the earlier write counts


if __name__ == "__main__":
    unittest.main()
