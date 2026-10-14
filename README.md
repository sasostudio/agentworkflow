# AgentWorkflow

**A code-driven engine for long, unattended coding work with Claude Code.** You (or a Claude Code session acting as "Master") write a TODO; the engine drives a **builder** agent and an **evaluator** agent through build → review → QA → final approval, and stops cleanly whenever a human decision is needed.

[한국어 README](README.ko.md)

> **Unofficial.** Not affiliated with or endorsed by Anthropic. It runs the official, unmodified `claude` CLI on your own machine with **your own** Claude login or API key.
> **Windows only** (tested on Windows 11 · PowerShell). **Prompts and rule files are in Korean** — the models handle it fine; English rule files may come later.

```
PLANNING (code) ─ checks the Master's TODO (`## 항목`) → TODO file
BUILD     ─ builder (Sonnet) in ONE session: first call → "continue"… → end checks (out-of-scope · self-check · machine check, no AI)
            → wiki update (same session)
EVALUATE  ─ main evaluator (Opus) in ONE session decides each next step:
            RUN (scenarios → runner → results back to the same session) · REJECT (its text goes to the builder as is)
            · READY → (second view if high-risk / contract files changed) → (fresh-session rebuttal review) → final approval
            · UNSURE → final approval
Final approval (Master) → COMPLETE
```

**Agents do the work and the judging; the engine only runs things unattended**: ordering, passing text through verbatim, running scenarios, counting and capping, fences, and stopping with `HELP` when stuck. It never re-interprets an evaluator's findings.

## Why

Long agent runs fail in boring ways: an agent claims "done" without checking, edits files it was not asked to touch, loops on the same fix, burns the usage limit mid-call, or a crash loses the session. AgentWorkflow puts those failure points in code:

| Failure | What the engine does |
|---|---|
| "Done" without proof | A separate evaluator (fresh context, read-only) must judge; final approval by Master; a fresh-session rebuttal review tries to refute READY |
| Editing outside the task | Every changed file must be a `대상` (target) of some TODO item — otherwise auto-reject |
| Builder rewriting its own TODO | The engine owns the TODO (its copy in the run folder is canonical); the builder only reads it |
| Spinning in place | Two BUILD calls with no new check and no file change = auto-reject; caps on rejects, questions, retries, calls, cost |
| Usage limit hit mid-call | Reads the account's usage windows while a call runs and cuts it at 95 % (5 h) / 97 % (7 d); `resume` continues the same session |
| Crash / PC restart | Write-ahead state, session ids recorded at call start, a supervisor that restarts a dead engine, `resume` after anything |
| Workarounds (`any`, empty `catch`, `@ts-ignore` …) | Diff is scanned and new workaround traces are handed to the evaluator |

## Quality rules (bundled)

Every AI call carries the same rule set that the author uses for their own work — the engine does not rely on your project's `CLAUDE.md`:

| File | Who | What |
|---|---|---|
| `rules/WORKING_RULES.md` | all (system prompt) | work scope · no destructive ops/commits/deploys · **verify before asserting (R1–R12)** · fixed checklist A–H · root-cause-only fixes · side-effect check |
| `rules/roles/builder.md` | builder | role · honest reporting · read-first · stop and ask after 2–3 failed tries |
| `rules/roles/evaluator.md` | evaluator | read-only · self-verification · no rubber-stamping, no nitpicking |
| `rules/skills/developer-mode.md` | builder | assertion gate · runtime-data gate · **write gate W1–W4** (search before writing · no old/new path coexistence · single source · "where do I look if this breaks?") |
| `rules/skills/audit-inspector.md` | evaluator | 8-point code review · checklist A–H · audit the audit tool · no early "0 issues" |

The rules came from real incidents (23 in one day, all from "asserting without opening the file") and were tuned on real projects (Godot and Roblox games).

## Requirements

- Windows 10/11, PowerShell
- Python 3.10+ (no pip packages needed; **Pillow** optional — multi-frame capture sheets)
- [Claude Code](https://code.claude.com/docs) CLI on `PATH`, logged in (`claude`), recent version — tested with 2.1.284 (the engine uses `--json-schema`, `stream-json`, `--include-partial-messages`)
- Clone into a path **without spaces** (e.g. `C:\tools\agentworkflow`) — the builder's self-check command is refused when the engine path has a space
- git (targets must be git repos on a feature branch with a clean tree)
- QA runners (optional): `web` = Node.js + `npm install` in `runners/` + system Chrome · `godot` = Godot 4 · `roblox` = Roblox Studio + Studio MCP + Rojo on `PATH`

## Install

```powershell
# where: any folder you like
git clone https://github.com/sasostudio/agentworkflow.git
cd agentworkflow
[Environment]::SetEnvironmentVariable("AW_HOME", (Get-Location).Path, "User")   # used by the Master command
# optional: only allow targets under one folder
[Environment]::SetEnvironmentVariable("AW_WORK_ROOT", "C:\work", "User")
# optional, web QA runner only
cd runners; npm install; cd ..
```

**Master command for Claude Code** (recommended): copy `claude/commands/agentworkflow.md` to your project's `.claude/commands/` (or `~/.claude/commands/`). Then in a Claude Code session: `/agentworkflow <what to build>`. That session becomes Master: it reads your code, writes `request.md` with the TODO, asks you to approve, runs the engine in the background, and handles approvals, questions and `HELP`.

## Quick start (manual)

1. In your target repo: `git switch -c feature/my-task` and commit or stash everything.
2. Copy an example config: `examples/engine.web.example.json` (or `godot` / `roblox`) → set `target`, `check_commands`, `qa`.
3. Write a request from `examples/request.example.md` and save it under the engine's `requests/` folder (create it; it is git-ignored). The `## 항목` section is the TODO (`대상` = files to change, `완료 조건` = done criteria, `기능 위치` = where the feature lives). Without the Master command, **you** are the Master: you write this TODO by reading your code, and you approve or reject at the end.
4. Run:

```powershell
# where: the engine folder
cd $env:AW_HOME
mkdir requests -Force | Out-Null
python -m agentworkflow supervise run --config "C:\work\my-app\engine.json" --request "requests\my-task.md"
```

**The engine parses these Korean headings and field names — keep them as they are** (the text under them can be in any language):

| Heading / field | Meaning |
|---|---|
| `## 목표` · `## 구현 범위` · `## 범위 외` · `## 주의` | goal · in scope · out of scope · cautions |
| `## QA 시나리오 힌트` · `## 난이도` (L1–L3) | QA scenario hints · difficulty |
| `## 항목` (required) | the TODO: `- [ ] DEV-001 [L1] task` with `대상:` (files to change) · `완료 조건:` (done criteria) · `기능 위치:` (where the feature lives) · `선행:` (depends on, or `없음` = none) |
| `## 흐름` (optional) | `- QA: 있음/없음` · `- Wiki: 있음/없음` (on/off, both on by default) |
| `## 유형` · `## 원작 근거` (optional) | `시각` (visual) / `원작 재현` (recreate an original) · reference image paths |

Exit codes: `0` complete · `3` stopped (HELP · AWAITING_APPROVAL · AWAITING_ANSWER · PAUSED_USAGE) · `2` refused to start.

## Commands

```powershell
# where: the engine folder ($env:AW_HOME)
python -m agentworkflow supervise run     --config "engine.json" --request "request.md"
python -m agentworkflow status            --run "ai-log\DATE\runNNN_slug"
python -m agentworkflow supervise resume  --run "…"            # after a crash, HELP, or usage pause
python -m agentworkflow supervise approve --run "…" --note "why" [--follow-up FILE --follow-up-text "task" [--after DEV-001]]
python -m agentworkflow supervise reject  --run "…" --reason "what to change"
python -m agentworkflow supervise answer  --run "…" --text "answer" [--follow-up FILE --follow-up-text "task"]
python -m agentworkflow wait-reset        --run "…"            # PAUSED_USAGE: returns at reset + 60 s
python -m agentworkflow report            --run "…"
python -m agentworkflow unlock            --config "engine.json"  # only when no process owns the lock
python -m agentworkflow dashboard         [--port 8799]        # local read-only dashboard, 0 tokens
python -m agentworkflow campaign …                            # continuous mode (see the Master command)
```

## QA runners

| runner | how |
|---|---|
| `web` | `runners/web_runner.mjs` (playwright-core + system Chrome) · `managed` = the engine starts your dev server and stops only that process tree |
| `godot` | `godot --path TARGET -s scenario.gd` per scenario |
| `command` | your command writes `results.json` |
| `roblox` | the engine drives Studio via MCP: `reset_code` → scenario → capture → `reset_code` → stop |
| `none` | no scenarios |

The evaluator writes the scenarios; the engine only checks syntax, wait totals and caps, then runs them and hands results back.

## Configuration notes

- `models`: `builder` (default `claude-sonnet-5-5`) and `evaluator` (default `claude-opus-5-5`). Use exact model IDs, not aliases.
- `capabilities` (per run, all off by default): `web_roles`, `mcp_servers`, `mcp_tools`, `plugins`. Submit/send/execute-type MCP tools are refused.
- Sub-agents never load your user settings (`--setting-sources project,local`, hooks off, `--strict-mcp-config`): global allow rules were measured to leak MCP tools past `--allowedTools`.
- `rules_root` (default `rules/`): point it at your own copy if you want to change the rules.
- `AW_WORK_ROOT` (env, optional): refuse targets outside this folder.

## Dashboard

`python -m agentworkflow dashboard` → `http://127.0.0.1:8799` (this PC only, read-only, no AI calls). Shows status, progress, the current call, a flow graph, the call timeline (click = raw output), verdicts and scenario captures.

Optional cloud copy (Supabase + Vercel): `supabase/dashboard_schema.sql` (**edit the admin email first**) + `.env.dashboard` (git-ignored) + `python -m agentworkflow dashboard-deploy`. Without `.env.dashboard` the uploader stays off.

## Usage limits & terms

- Runs consume **your** Claude plan usage (or API credits). Anthropic's Pro/Max limits assume ordinary, individual use — see [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance).
- The engine reads usage windows from the CLI stream and stops at 95 % (5 h) / 97 % (7 d). It never touches your credentials.
- Do not use this to offer Claude access to other people through your account.

## Tests

```powershell
# where: the engine folder
python -m unittest discover -s tests
```

253 tests, no AI calls (fake adapter, fake Studio). Only `test_web_runner` launches headless Chrome. Temp repos go to `.testtmp/` and each test process removes only its own folder.

## Known limitations

- **Do not let the PC sleep while a run is going** — nothing (engine, fallback resume, Master) runs during sleep. Screen lock is fine.
- The second view and the rebuttal review run one after another, not in parallel.
- Roblox QA captures: Studio MCP resets the camera after `execute_luau`, so camera-dependent views do not show in captures.
- Rules make good behaviour much more likely, not certain — only countable things are enforced in code.
- Code comments refer to internal design notes (`design §N`, `v6 §N`) that are not part of this repo.

## License

MIT — see [LICENSE](LICENSE).
