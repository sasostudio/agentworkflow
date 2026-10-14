"""v4 §9 (R-A) — AI↔engine contracts are JSON; documents are written by the engine; tool output is read only from
known diagnostic shapes (survey of every check log of a real Roblox project), with bounded work per line (§23-12 #7 · #11)."""
import random
import re
import time
import unittest
from pathlib import Path

from agentworkflow import checks, prompts, todo

import helpers as h


def items(*pairs):
    return h.items_json("".join(pairs))


class RenderPlan(unittest.TestCase):
    def test_checked_items_keep_their_mark_and_evidence_across_a_replan(self):
        prior = todo.render_plan({"summary": "s", "design": "", "items": items(h.item("DEV-001"), h.item("DEV-002"))})
        prior = todo.mark_done(prior, "DEV-001", "hello.py (작성)")
        again = todo.render_plan({"summary": "s2", "design": "", "items": items(h.item("DEV-001"), h.item("DEV-002"))},
                                 prior)
        parsed = {i["id"]: i for i in todo.parse(again)[0]}
        self.assertTrue(parsed["DEV-001"]["checked"])
        self.assertEqual(parsed["DEV-001"]["evidence"], "hello.py (작성)")
        self.assertFalse(parsed["DEV-002"]["checked"])

    def test_empty_fields_are_left_out_so_check_plan_names_them(self):
        bad = {"id": "DEV-001", "level": 1, "text": "t", "targets": [], "criteria": "", "location": "", "deps": []}
        v = todo.check_plan("", todo.render_plan({"summary": "s", "design": "", "items": [bad]}), run_level=1)
        self.assertTrue(any("'대상:' 줄 없음" in x for x in v))
        self.assertTrue(any("'완료 조건:' 줄 없음" in x for x in v))
        self.assertTrue(any("'기능 위치:' 줄 없음" in x for x in v))

    def test_json_id_problems(self):
        v = todo.json_item_problems([{"id": "DEV-1"}, {"id": "DEV-002"}, {"id": "DEV-002"}])
        self.assertTrue(any("형식 오류" in x for x in v))
        self.assertTrue(any("ID 중복" in x for x in v))
        self.assertTrue(todo.json_item_problems([{"id": "DEV-003"}], "FIX"))  # QA_FIX must add FIX items

    def test_mark_done_on_a_checked_item_replaces_its_evidence(self):
        text = todo.mark_done(todo.render_plan({"summary": "s", "design": "", "items": items(h.item())}),
                              "DEV-001", "a.py (1차)")
        text = todo.mark_done(text, "DEV-001", "hello.py (고침)")
        self.assertEqual(text.count("완료 근거:"), 1)
        self.assertEqual(todo.parse(text)[0][0]["evidence"], "hello.py (고침)")

    def test_render_design_replaces_same_id_and_keeps_others(self):
        d1 = todo.render_design([h.design_json("DEV-001"), h.design_json("DEV-002")])
        d2 = todo.render_design([dict(h.design_json("DEV-002"), flow="새 흐름")], d1)
        self.assertEqual(todo.check_design(d2, ["DEV-001", "DEV-002"]), [])
        self.assertEqual(d2.count("## DEV-002"), 1)
        self.assertIn("새 흐름", d2)


class Prompts(unittest.TestCase):
    def test_no_unfilled_placeholders(self):
        # 09-26 stage 4: a lost f-prefix sent the developer a literal "{todo_lines}" (caught by test_w5)
        # v6: every builder and evaluator prompt
        it = [{"id": "DEV-001", "level": 1, "text": "t", "target": "a.py", "criteria": "c", "location": "a.py"}]
        ctx = {"changed": "a.py", "diff": "+x", "omitted": ["b.py"], "check_log": "ok", "summary": "s",
               "bypass": "", "denied": "", "since": "run 시작", "kind": "시각", "refs": ["ref.png"]}
        qinfo = {"runner": "web", "environment": "external", "base_url": "http://x", "cap": 20, "carry": "- `S1` …"}
        res = [{"id": "S1", "status": "FAILED", "note": "n"}]
        notes = [{"t": "10-03", "reason": "R"}]
        rendered = [prompts.build_first("p.md", it), prompts.build_continue(it, "남음", "LOG"),
                    prompts.build_fix([{"source": "eval", "reason": "R"}], it), prompts.build_wiki("docs/wiki", "a.py"),
                    prompts.self_check_fix(["a.py"], "LOG"), prompts.eval_main(ctx, qinfo, notes, True, True),
                    prompts.eval_main(ctx, None), prompts.eval_second(ctx), prompts.precheck_note(["p"]),
                    prompts.eval_followup([prompts.results_block("d", res, "- S1 fail", "(없음)", [("S1", "t", ["a.png"])],
                                                                 "시각", ["ref.png"]),
                                           prompts.other_eyes_block("반박 검토", {"verdict": "REJECT", "issues": []})],
                                          True),
                    prompts.eval_followup([], False),
                    prompts.eval_devil(ctx, ["s.mjs"], [("S1", "t", "끝", ["a.png"])], "- S1 → PASSED"),
                    prompts.session_lost_builder("남음", ["DEV-001"]), prompts.session_lost_evaluator("x")]
        for text in rendered:
            self.assertEqual(re.findall(r"\{[a-z_]+\}", text), [], text[:120])

    def test_evaluator_is_read_only_and_nobody_gets_a_shell_or_subagents(self):
        self.assertFalse([t for t in prompts.READ_ONLY if t.startswith(("Edit(", "Write("))])
        for t in ("Bash", "Agent", "Task", "AskUserQuestion"):
            self.assertIn(t, prompts.NO_SHELL)  # v6 D3 · S-11


class ErrorFiles(unittest.TestCase):
    def setUp(self):
        self.root = h.make_repo()
        for f in ("src/shared/A.luau", "src/tests/Cases/B_Test.luau", "src/server/C.luau", "sourcemap.json"):
            p = Path(self.root) / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x\n", encoding="utf-8")

    def test_the_three_real_tool_shapes(self):
        abs_c = str(Path(self.root) / "src" / "server" / "C.luau")
        log = ("== StyLua\nDiff in src\\shared\\A.luau:\n1 1  | x\n"
               "== selene\nwarning[unused_variable]: y is defined, but never used\n   ┌─ src\\tests\\Cases\\B_Test.luau:3:7\n"
               "== luau-lsp\n" + abs_c + " [game/ServerScriptService/Server/C](12,3): TypeError: Expected this to be 'A'\n")
        self.assertEqual(checks.error_files(log, self.root),
                         ["src/server/C.luau", "src/shared/A.luau", "src/tests/Cases/B_Test.luau"])

    def test_info_lines_are_not_failures(self):
        # §23-12 #7 (F-09): "Created sourcemap at sourcemap.json" was read as a failing file -> wrong hand-off
        log = ("[INFO] Loading definitions file: @roblox - C:\\x\\globalTypes.None.d.luau\nCreated sourcemap at sourcemap.json\n"
               "Results:\n0 errors\n== failed checks: 1\n")
        self.assertEqual(checks.error_files(log, self.root), [])

    def test_bounded_time_on_hostile_lines(self):
        # §23-12 #11 (F-10): one luau-lsp line hung the engine 34 min (exponential backtracking)
        rnd = random.Random(7)
        alphabet = "abcXYZ019_-.\\/:[](),;' ┌─│"
        lines = ["c:\\Users\\" + "a\\" * 400 + "x" * 3000 + " [game/" + "b/" * 500 + "](1,2): TypeError: " + "q" * 5000]
        lines += ["".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 6000))) for _ in range(200)]
        t0 = time.monotonic()
        for line in lines:
            checks.error_files(line, self.root)
        self.assertLess(time.monotonic() - t0, 5.0)


if __name__ == "__main__":
    unittest.main()
