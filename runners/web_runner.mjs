// Web QA runner — executes QA-authored scenario modules with playwright-core + system Chrome.
// Contract (design §8): each scenario = `export default async function ({ page, baseUrl, shot, log, blocked })`.
//   throw        -> FAILED
//   blocked(msg) -> BLOCKED
//   return       -> PASSED (the engine demotes PASSED without a screenshot to BLOCKED)
// Input: --manifest scenarios.json [{id, path, evidence_dir}] written by the engine.
// Output: --results JSON array [{id, status, note, evidence: [file]}], screenshots under --evidence/<evidence_dir>/.
import { chromium } from 'playwright-core';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith('--') ? [...acc, [a.slice(2), all[i + 1]]] : acc), []),
);
const CHROME = process.env.AW_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const timeoutMs = Number(args.timeout || 300) * 1000;

class Blocked extends Error {}

// ids come from the engine's manifest (scenarios.json) — never parsed from file names
// (e2e run002: ids like "01_initial_state" were cut at the first "_" -> every result "missing")
const manifest = JSON.parse(fs.readFileSync(args.manifest, 'utf-8'));
const results = [];
const browser = await chromium.launch({ executablePath: CHROME, headless: true });
try {
  for (const entry of manifest) {
    const id = entry.id;
    const edir = path.join(args.evidence, entry.evidence_dir);
    fs.mkdirSync(edir, { recursive: true });
    const evidence = [];
    const logs = [];
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
    const page = await ctx.newPage();
    page.on('pageerror', (e) => logs.push(`pageerror: ${e.message}`));
    page.on('console', (m) => { if (m.type() === 'error') logs.push(`console.error: ${m.text()}`); });
    const shot = async (name) => {
      const file = `${String(evidence.length + 1).padStart(2, '0')}-${String(name).replace(/[^\w가-힣-]+/g, '_')}.png`;
      await page.screenshot({ path: path.join(edir, file), fullPage: false });
      evidence.push(file);
      return file;
    };
    let status = 'PASSED';
    let note = '';
    let timer;
    try {
      const mod = await import(pathToFileURL(entry.path).href);
      await Promise.race([
        mod.default({ page, baseUrl: args['base-url'], shot, log: (m) => logs.push(String(m)), blocked: (m) => { throw new Blocked(m); } }),
        new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`시나리오 타임아웃 ${timeoutMs / 1000}s`)), timeoutMs); }),
      ]);
    } catch (e) {
      status = e instanceof Blocked ? 'BLOCKED' : 'FAILED';
      note = e?.message || String(e);
      try { await shot('failure'); } catch {}
    } finally {
      clearTimeout(timer);
    }
    if (logs.length) note += (note ? ' | ' : '') + logs.slice(0, 5).join(' / ');
    results.push({ id, status, note, evidence });
    await ctx.close();
    console.log(`${id} ${status} ${note}`);
  }
} finally {
  await browser.close();
}
fs.writeFileSync(args.results, JSON.stringify(results, null, 2));
