"""Integration: real headless Chrome via runners/web_runner.mjs + managed server. No AI calls."""

import sys
import unittest
from pathlib import Path

from agentworkflow import qa
from agentworkflow.config import DEFAULTS, _merge

import helpers as h

CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
PAGE = "<!doctype html><title>t</title><button id=b onclick=\"document.body.dataset.ok='1'\">인사</button>"

PASS = """export default async function ({ page, baseUrl, shot }) {
  await page.goto(baseUrl);
  await page.click('#b');
  if ((await page.getAttribute('body', 'data-ok')) !== '1') throw new Error('클릭 안 됨');
  await shot('클릭후');
}"""
FAIL = """export default async function ({ page, baseUrl }) {
  await page.goto(baseUrl);
  throw new Error('기대와 다름');
}"""
BLOCK = """export default async function ({ blocked }) { blocked('테스트 계정 없음'); }"""
NOSHOT = """export default async function ({ page, baseUrl }) { await page.goto(baseUrl); }"""


@unittest.skipUnless(CHROME.exists() and (qa.RUNNERS_DIR / "node_modules" / "playwright-core").exists(),
                     "Chrome 또는 playwright-core 없음")
class WebRunner(unittest.TestCase):
    def test_managed_server_and_verdicts(self):
        site = h.ROOT / "site"
        site.mkdir(parents=True, exist_ok=True)
        (site / "index.html").write_text(PAGE, encoding="utf-8")
        cfg = _merge(DEFAULTS, {"qa": {"runner": "web", "environment": "managed", "scenario_timeout_sec": 60,
                                       "managed": {"start": f'"{sys.executable}" -m http.server 8765 --bind 127.0.0.1',
                                                   "ready_url": "http://127.0.0.1:8765/", "ready_timeout_sec": 30}}})
        qdir = h.ROOT / "webqa"
        qdir.mkdir(parents=True, exist_ok=True)
        scen = [{"id": "01_click_flow", "title": "클릭", "kind": "normal", "filename": "s1", "code": PASS},
                {"id": "02_fail_flow", "title": "실패", "kind": "failure", "filename": "s2", "code": FAIL},
                {"id": "03 blocked?", "title": "막힘", "kind": "normal", "filename": "s3", "code": BLOCK},
                {"id": "S4", "title": "캡처없음", "kind": "normal", "filename": "s4", "code": NOSHOT}]
        verdict, results = qa.run_qa(cfg, site, qdir, scen)
        st = {r["id"]: r["status"] for r in results}
        self.assertEqual(st, {"01_click_flow": "PASSED", "02_fail_flow": "FAILED", "03 blocked?": "BLOCKED", "S4": "BLOCKED"},
                         (qdir / "runner.log").read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(verdict, "FAILED")
        self.assertTrue(list((qdir / "evidence" / "01_click_flow").glob("*.png")))


if __name__ == "__main__":
    unittest.main()
