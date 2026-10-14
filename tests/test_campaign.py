"""Continuous mode / campaign (design §21). Real git, fake AI."""

import json
import unittest
from pathlib import Path

from agentworkflow import gitutil, safety
from agentworkflow.adapters import FakeAdapter
from agentworkflow.campaign import Campaign, CampaignError, _git_write, parse_plan
from agentworkflow.engine import Engine

import helpers as h

PLAN = """# 캠페인: 테스트 게임

## 목표
- 인사 게임 완성

## 대상
- 레포: {repo}
- 브랜치: feature/campaign-t
- 커밋: 허용 (push 안 함)

## 기능 목록
- [ ] F-01 [L1] 인사 출력
- [ ] F-02 [L1] 두 번째 인사

## 상한
- run: {runs}
- 호출: 100
- 비용: 50
"""


def make(runs=10):
    repo = h.make_repo()
    plan = h.ROOT / f"plan_{repo.name}.md"
    plan.write_text(PLAN.format(repo=str(repo).replace("\\", "/"), runs=runs), encoding="utf-8")
    c = Campaign(f"c_{repo.name}", root=h.ROOT / "campaigns")
    c.create(plan)
    return c, repo


def complete_run(c, repo, feature, file="hello.py"):
    """What `run --campaign` does (cli.cmd_run), with the real engine and fake AI."""
    cfg = h.make_cfg(repo)
    c.check_run(feature, repo, cfg)
    st = h.make_state(repo)
    st.d["campaign"] = {"name": c.name, "feature": feature}
    c.record_run(feature, st.dir, st.d["run_id"])
    # v6: build → evaluate → rebuttal → end gate (Master approve) → COMPLETE
    script = {"builder": [h.b_work(files={file: "print(1)\n"})], "evaluator": [h.e_ans("READY")],
              "evaluator:devil": [h.devil()]}
    fake = FakeAdapter(script)
    req = h.make_request(items=[h.item(target=file, location=file)], extra=h.NO_QA)
    status = Engine(cfg, req, st, fake, fake, sleep=lambda s: None, out=lambda m: None).run()
    if status == "AWAITING_APPROVAL":
        from agentworkflow import cli
        cli.approve_gate(st, cfg, repo, "ok")
        status = Engine(cfg, req, st, fake, fake, sleep=lambda s: None, out=lambda m: None).run()
    c.update_status(st.d["run_id"], status)
    return st, status


def set_state(st, **kw):
    p = st.dir / "state.json"
    s = json.loads(p.read_text(encoding="utf-8"))
    s.update(kw)
    p.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


class Plan(unittest.TestCase):
    def test_plan_rules(self):
        ok = PLAN.format(repo="C:/work/x", runs=5)
        self.assertEqual([f["id"] for f in parse_plan(ok)["features"]], ["F-01", "F-02"])
        with self.assertRaises(CampaignError):  # auto-commit only where the admin allowed it
            parse_plan(ok.replace("- 커밋: 허용 (push 안 함)", "- 커밋: 금지"))
        with self.assertRaises(CampaignError):
            parse_plan(ok.replace("feature/campaign-t", "main"))
        saved = safety.WORK_ROOT  # the optional AW_WORK_ROOT fence
        safety.WORK_ROOT = Path("C:/work").resolve()
        try:
            parse_plan(ok)
            with self.assertRaises(CampaignError):  # outside the fence
                parse_plan(ok.replace("C:/work/x", "C:/Windows/x"))
        finally:
            safety.WORK_ROOT = saved
        with self.assertRaises(CampaignError):
            parse_plan(ok.replace("F-02", "F-01"))
        # v6 H6: `- Wiki:` in the approved plan (default 없음 · a typo is refused)
        self.assertFalse(parse_plan(ok)["wiki"])
        self.assertTrue(parse_plan(ok.replace("- 커밋:", "- Wiki: 있음\n- 커밋:"))["wiki"])
        with self.assertRaises(CampaignError):
            parse_plan(ok.replace("- 커밋:", "- Wiki: 켬\n- 커밋:"))


class Lifecycle(unittest.TestCase):
    def test_draft_cannot_run_and_approve_creates_branch(self):
        c, repo = make()
        h.git(repo, "checkout", "-q", "-b", "feature/campaign-t")  # right repo + branch: only DRAFT may block
        with self.assertRaises(CampaignError) as cm:
            c.check_run("F-01", repo, None)
        self.assertIn("DRAFT", str(cm.exception))
        h.git(repo, "checkout", "-q", "feature/t")
        c.approve("사용자 승인 테스트")
        self.assertEqual(gitutil.current_branch(repo), "feature/campaign-t")
        self.assertEqual(c.check_run("F-01", repo, None)["id"], "F-01")

    def test_order_repo_branch_and_frozen_plan(self):
        c, repo = make()
        c.approve("ok")
        with self.assertRaises(CampaignError):  # no skipping
            c.check_run("F-02", repo, None)
        other = h.make_repo()
        with self.assertRaises(CampaignError):  # wrong target
            c.check_run("F-01", other, None)
        h.git(repo, "checkout", "-q", "-b", "feature/elsewhere")
        with self.assertRaises(CampaignError):  # wrong branch
            c.check_run("F-01", repo, None)
        h.git(repo, "checkout", "-q", "feature/campaign-t")
        c.plan_path.write_text(c.plan_path.read_text(encoding="utf-8") + "- [ ] F-03 몰래 추가\n", encoding="utf-8")
        with self.assertRaises(CampaignError):  # plan changed after approval -> admin must re-approve
            c.check_run("F-01", repo, None)

    def test_two_features_in_a_row_with_commits(self):
        c, repo = make()
        c.approve("ok")
        st, status = complete_run(c, repo, "F-01")
        self.assertEqual(status, "COMPLETE")
        head1 = c.commit(st.dir)
        self.assertEqual(gitutil.dirty_paths(repo), [])  # clean tree -> the next run can start
        safety.preflight(repo)
        msg = gitutil.git(repo, "show", "-s", "--format=%B", "HEAD")
        self.assertIn("feat(F-01)", msg)
        self.assertIn("Co-Authored-By", msg)
        st2, status2 = complete_run(c, repo, "F-02", file="bye.py")
        self.assertEqual(status2, "COMPLETE")
        head2 = c.commit(st2.dir)
        self.assertNotEqual(head1, head2)
        d = c.load()
        self.assertEqual(d["status"], "FINISHED")
        self.assertEqual(set(d["done"]), {"F-01", "F-02"})
        self.assertIn("[x] F-02", c.status_text())


class Bundle(unittest.TestCase):
    """v4 §16-17: one run may cover several consecutive features (a Master bundle)."""

    def test_bundle_must_be_the_next_features_in_order_and_commits_all_of_them(self):
        c, repo = make()
        c.approve("ok")
        for bad in ("F-02", "F-02,F-01", "F-01,F-01", "F-01,F-03"):
            with self.assertRaises(CampaignError, msg=bad):
                c.check_run(bad, repo, None)
        self.assertEqual(c.check_run("F-01,F-02", repo, None)["id"], "F-01")
        st, status = complete_run(c, repo, "F-01,F-02")
        self.assertEqual(status, "COMPLETE")
        self.assertEqual(c.load()["runs"][-1]["features"], ["F-01", "F-02"])
        with self.assertRaises(CampaignError):  # the open bundle blocks each of its features
            c.check_run("F-01", repo, None)
        c.commit(st.dir)
        d = c.load()
        self.assertEqual(set(d["done"]), {"F-01", "F-02"})
        self.assertEqual(d["status"], "FINISHED")
        msg = gitutil.git(repo, "show", "-s", "--format=%B", "HEAD")
        self.assertIn("feat(F-01..F-02): 인사 출력 · 두 번째 인사", msg)


class CommitGuards(unittest.TestCase):
    def test_only_complete_runs(self):
        c, repo = make()
        c.approve("ok")
        st, _ = complete_run(c, repo, "F-01")
        set_state(st, status="ESCALATED")
        with self.assertRaises(CampaignError):
            c.commit(st.dir)

    def test_secret_and_artifact_files_block_commit(self):
        c, repo = make()
        c.approve("ok")
        st, _ = complete_run(c, repo, "F-01")
        (repo / "serviceAccountKey.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(CampaignError):
            c.commit(st.dir)
        (repo / "serviceAccountKey.json").unlink()
        set_state(st, check_artifacts=["build.out"])
        (repo / "build.out").write_text("x", encoding="utf-8")
        with self.assertRaises(CampaignError):
            c.commit(st.dir)

    def test_open_run_blocks_a_second_run_of_the_same_feature(self):
        c, repo = make()
        c.approve("ok")
        complete_run(c, repo, "F-01")  # COMPLETE but not committed yet
        with self.assertRaises(CampaignError):
            c.check_run("F-01", repo, None)

    def test_caps(self):
        c, repo = make(runs=1)
        c.approve("ok")
        st, _ = complete_run(c, repo, "F-01")
        c.commit(st.dir)
        with self.assertRaises(CampaignError):  # 1 run used of 1
            c.check_run("F-02", repo, None)

    def test_git_writes_are_limited(self):
        repo = h.make_repo()
        for args in (("push",), ("reset", "--hard"), ("checkout", "--", "app.py"), ("clean", "-fd"),
                     ("commit", "--amend", "-m", "x"), ("checkout", "-B", "x")):
            with self.assertRaises(CampaignError, msg=args):
                _git_write(repo, *args)


class CampaignGaps(unittest.TestCase):
    """Continuous-mode simulation (09-25). Written before the fixes."""

    # C12 — a file changed AFTER the run finished was never reviewed -> must not ride along in the commit
    def test_change_after_complete_blocks_commit(self):
        c, repo = make()
        c.approve("ok")
        st, _ = complete_run(c, repo, "F-01")
        (repo / "hello.py").write_text("print('검수 안 받은 수정')\n", encoding="utf-8")
        with self.assertRaises(CampaignError) as cm:
            c.commit(st.dir)
        self.assertIn("검수", str(cm.exception))

    # C2 — an abandoned feature run (admin decision) must not jam the campaign forever
    def test_abandoned_run_frees_the_feature(self):
        c, repo = make()
        c.approve("ok")
        st, _ = complete_run(c, repo, "F-01")
        set_state(st, status="ESCALATED")
        with self.assertRaises(CampaignError):
            c.check_run("F-01", repo, None)  # open run
        c.abandon(st.d["run_id"], "사용자: 방향 바꿔 다시")
        h.git(repo, "stash", "-u")  # the admin cleans the tree (the engine never discards work)
        self.assertEqual(c.check_run("F-01", repo, None)["id"], "F-01")

    # C9 — the Master self-approves requests, but not below the difficulty the admin approved in the plan
    def test_request_level_not_below_plan_level(self):
        c, repo = make()
        c.plan_path.write_text(c.plan_path.read_text(encoding="utf-8").replace("F-01 [L1]", "F-01 [L3]"),
                               encoding="utf-8")
        c.approve("ok")
        with self.assertRaises(CampaignError):
            c.check_run("F-01", repo, None, run_level=1)
        self.assertEqual(c.check_run("F-01", repo, None, run_level=3)["id"], "F-01")


if __name__ == "__main__":
    unittest.main()
