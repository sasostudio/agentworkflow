"""v6 (10-02 · 설계서 v6 §2 · §5 · §6-2) — every prompt, tool permission and output schema the engine sends.
If an AI was told the wrong thing, read this file.

Two schemas, one per role, never switched inside a session (v6 S-6 — the schema is part of what the session caches):
  BUILD_SCHEMA  builder: first · continue · fix · self-check fix · wiki
  EVAL_SCHEMA   evaluator · evaluator:second · evaluator:devil: E1 · scenario fix (PF/SF) · E2 · second view · rebuttal
Permission rules: paths without a leading slash are relative to the agent cwd (= target). Nobody gets a shell — the
engine runs builds/tests itself (the self-check command and the dev-shot script are the only Bash rules, engine.py).
"""

from pathlib import Path

from .todo import CONTRACT_MARK

NO_SHELL = ["Bash", "PowerShell", "AskUserQuestion", "WebFetch", "WebSearch", "NotebookEdit", "Agent", "Task"]
READ_ONLY = ["Read", "Grep", "Glob"]

# 10-03 정렬안 §3: RUN = "play these scenarios" (was PENDING). The main evaluator's verdict alone picks the next step.
VERDICTS = ("READY", "REJECT", "RUN", "UNSURE")

BUILD_SCHEMA = {
    "type": "object",
    "properties": {
        # C-4 (09-25 run006): paths as a list + a free note — the engine assembles the 완료 근거 (no prose parsing)
        "done": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}},
            "note": {"type": "string"}}, "required": ["id", "files"]}},
        "summary": {"type": "string"},
        # v6 A8: what is left and what comes next — the engine keeps it for the next call and for a lost session
        "remaining": {"type": "string"},
        "write_gate": {"type": "string"},
        "question": {"type": "string"},
        "self_check": {"type": "string"},  # v4 §11: the last self-check line (the engine re-runs it and trusts only that)
    },
    "required": ["done", "summary", "remaining", "write_gate", "question"],
}

_SCENARIO = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "kind": {"type": "string", "enum": ["normal", "failure"]},
        # which TODO items this scenario checks — a record for the reader (10-03: the engine counts nothing from it)
        "item_ids": {"type": "array", "items": {"type": "string"}},
        "filename": {"type": "string"},
        "code": {"type": "string"},
        # v4 §10 (#1): roblox only — steps run in order inside ONE play, each in its own context
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "context": {"type": "string", "enum": ["Server", "Client"]}, "code": {"type": "string"}},
            "required": ["context", "code"]}},
        # v5 (10-01): runners with multi_shot "frames" (qa.CAPS — roblox) capture after the last step
        "frames": {"type": "integer", "minimum": 1, "maximum": 12},
        "interval": {"type": "number", "minimum": 0, "maximum": 10},
        # nothing on screen to capture (the client is gone — Kick·leave · a server-only data check) —
        # the reason; the runner skips the capture and a PASSED stays PASSED, marked "캡처 생략" for the evaluator
        "no_capture": {"type": "string"},
    },
    "required": ["id", "title", "kind", "filename", "code"],
}

# 10-03 정렬안 §3: ONE answer shape for every evaluator call (main · second view · rebuttal). The engine acts on
# `verdict` only and carries `to_builder` / `scenarios` as they are; `issues` · `reason` · `checked` are the record.
EVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "to_builder": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "where": {"type": "string"}, "problem": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["severity", "where", "problem"]}},
        "reason": {"type": "string"},
        # R5: "통과·0건" is only valid with the range actually checked
        "checked": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "scenarios": {"type": "array", "items": _SCENARIO},
        "want_rebuttal": {"type": "boolean"},
        "question": {"type": "string"},
    },
    "required": ["verdict", "to_builder", "issues", "reason", "checked", "scenarios", "want_rebuttal", "question"],
}

# Standard files the engine points each role at (they come in via --add-dir rules_root).
SKILL_FILES = {"developer-mode": "skills/developer-mode.md",
               "audit-inspector": "skills/audit-inspector.md"}

QUESTION_RULE = (
    "- **방향이 갈리거나 요청 명세·TODO로 답할 수 없는 결정**이 필요하면 추측하지 말고, JSON `question` 필드에 "
    "(무엇을 정해야 하나 + 선택지와 추천 1개)를 쓴다(없으면 빈 문자열). 엔진이 멈추고 Master가 답한다. "
    "그 결정에 걸린 부분은 만들지 않는다. 명세·코드를 읽으면 답이 나오는 것은 묻지 않는다.\n"
    "- **필요한 도구가 없어서 막히면**(웹 검색·MCP 조회 등) 우회하지 말고 `question`에 `도구 요청 — 무엇이 · 왜 필요한가 · "
    "없으면 어떻게 되나` 로 요청한다. 도구는 run 시작 때 승인된 것만 쓸 수 있다.\n"
)

REVIEW_RULES = (
    "당신은 **읽기 전용 평가자**다. 파일을 수정할 수 없고 수정하려 하지도 않는다.\n"
    "- 판단 기준은 평가 기준 `audit-inspector`(§판단 기준 · §코드 검수 8항목 · 고정 체크리스트 A~H 중 해당 항목)와 "
    "작업 규칙 §확인 전 단정 금지다. 봐주지 말고, 근거 없이 트집 잡지도 않는다.\n"
    "- diff와 파일 원문을 직접 열어 확인한 것만 근거로 판정한다. 확인 못 한 것은 추측하지 말고 issue로 적는다.\n"
    "- 요청 명세의 `범위 외`를 건드렸으면 REJECT. high 심각도 issue가 하나라도 있으면 REJECT. **medium·low만 있으면 "
    "REJECT 사유가 아니다** — high가 없을 때의 판정을 내고 `issues`에 남긴다(끝 승인에서 Master가 본다).\n"
    "- `checked`에 **실제로 돌린 항목**을 적는다(예: `A`,`B`,`G`,`8항목-1 근본원인`, 직접 연 파일 경로). 안 돌린 항목은 "
    "적지 않는다(R5).\n"
    "- 결과는 JSON으로만 낸다. 칸: `verdict`(이 지시가 허용한 것 중 하나) · `issues`(지적마다 심각도·위치·문제·고칠 방향 — "
    "기록) · `reason`(판정 이유) · `checked` · `to_builder`(**REJECT를 낼 때만** — 작업자에게 그대로 전해질 글: 무엇이 왜 "
    "틀렸고 무엇을 고칠지 · 그 밖에는 빈 문자열) · `scenarios`(**RUN을 낼 때만** — 이번에 돌릴 시나리오 전부 · 그 밖에는 빈 "
    "배열) · `want_rebuttal`(주 평가자가 READY를 낼 때만 의미 — 그 밖에는 false) · `question`(방향 질문 · 없으면 빈 "
    "문자열).\n"
)


# ---------------- permissions ----------------
def write_rules(folder):
    """Edit+Write pair on one folder, by its ABSOLUTE path: `./**` follows the session's cwd, and a read-only
    `cd X && ls` moves it — every write to the target was then refused (measured: absolute rules keep the target
    writable after a cd and refuse X)."""
    root = Path(folder).resolve().as_posix()
    return [f"Edit({root}/**)", f"Write({root}/**)"]


def perms_build(target):
    """v6 §2: the builder writes anywhere in the target; the engine fences it afterwards (TODO `대상` + wiki_dir)."""
    return READ_ONLY + write_rules(target)


def skill_paths(rules_root):
    root = str(rules_root).replace("\\", "/").rstrip("/")
    return {k: f"{root}/{v}" for k, v in SKILL_FILES.items()}


def shared_context(request_text, plan_rel, wiki_listing, rules_root, pack="work"):
    """The first message of a session (a continued session already has it). Rule packs per side (v4 §11)."""
    sk = skill_paths(rules_root)
    rule = (f"- 작업 기준: `{sk['developer-mode']}` (확인 전 단정 금지 게이트·런타임 데이터 게이트·작성 게이트 W1~W4·"
            "코딩 원칙). **검수는 하지 않는다** — 평가자가 따로 있다. 네 몫은 만들고 자기 검사를 통과시키는 것까지다.\n"
            if pack == "work" else
            f"- 평가 기준: `{sk['audit-inspector']}` (판단 기준·코드 검수 8항목·고정 체크리스트 A~H)\n")
    return (
        "# 공통 맥락 (AgentWorkflow run)\n\n"
        "## 규칙\n"
        "- 작업 규칙(시스템 프롬프트에 이미 들어와 있음 · `WORKING_RULES.md`)을 따른다.\n"
        + rule +
        "- 이 파일을 아직 읽지 않았으면 작업 전에 Read로 읽는다(이미 읽었으면 다시 읽지 않는다).\n\n"
        "## 요청 명세 (00-request/request.md)\n"
        f"{request_text}\n\n"
        f"## TODO 파일\n`{plan_rel}` (대상 프로젝트 기준 상대경로 · Master가 쓴 항목 · 체크는 엔진이 한다)\n\n"
        "## Wiki 계약 문서 (작업 전 관련 문서를 읽는다)\n"
        f"{wiki_listing}\n"
    )


def answers_block(questions):
    """v6 §6-1: the Master's answers go to every builder and evaluator call until the run ends."""
    pairs = [q for q in questions or [] if q.get("a")]
    if not pairs:
        return ""
    return ("\n\n## 이번 run의 방향 질문 답변 (Master — 끝까지 지킨다)\n"
            + "\n".join(f"- Q: {q['q']}\n  A: {q['a']}" for q in pairs) + "\n")


INTERRUPTED = (
    "# ⚠️ 직전 지시가 중간에 끊겼다 (엔진 재개)\n"
    "같은 세션이다. 끊기기 전에 하던 일의 **결과부터 확인**한다 — 마지막에 쓰던 파일을 Read로 열어 어디까지 반영됐는지 "
    "보고, 실행이 잘린 명령(자기 검사 등)은 다시 돌린다. 그다음 아래 지시대로 이어서 한다.\n\n"
)


def session_lost_builder(remaining, done_ids):
    """v6 S-5 · §4 ⑬: the builder's session is gone (e.g. retention) — a new one starts from what the engine kept."""
    return ("# 새 세션 — 이전 작업 세션을 잃었다 (엔진 기록으로 이어서)\n"
            f"- 이미 끝나 체크된 항목: {', '.join(done_ids) or '(없음)'} — 그 코드는 디스크에 있다(Read로 확인).\n"
            f"- 직전 세션이 남긴 「남은 일」: {remaining or '(기록 없음)'}\n"
            "- TODO의 `완료 근거`와 wiki가 지금까지의 근거다. 추측으로 다시 만들지 않는다.\n\n")


def session_lost_evaluator(prev_dir):
    return ("# 새 세션 — 이전 평가 세션을 잃었다 (엔진 기록으로 이어서)\n"
            + (f"- 직전 평가 시도 폴더: `{prev_dir}` — `scenarios.json`·`results_final.json`, 그리고 같은 폴더 위 "
               "`r*_*.json` 판정 기록을 Read로 연다. 이전 회차의 지적·시나리오를 이어받는다.\n\n" if prev_dir else
               "- 이전 평가 기록 없음 — 처음 평가하듯 한다.\n\n"))


# ---------------- builder ----------------
def items_text(items, design_note=""):
    return "\n".join(f"- {i['id']} [L{i['level']}] {i['text']}\n  - 대상: {i['target']}\n  - 완료 조건: {i['criteria']}\n"
                     f"  - 기능 위치: {i['location']}" for i in items) or "(열린 항목 없음)"


_BUILD_RULES = (
    "규칙:\n"
    "- 이 묶음의 **설계부터 구현까지 네가 한다**(설계 단계가 따로 없다). 큰 결정은 코드 주석이 아니라 `summary`에 남긴다.\n"
    "- **각 항목의 `대상` 파일만 고친다.** 대상 밖 파일을 고치면 엔진이 반려한다 — 꼭 필요하면 고치지 말고 `question`으로 "
    "알린다. 코드는 `기능 위치`에 둔다. TODO 파일은 **읽기만** 한다(체크·완료 근거는 엔진이 네 JSON으로 쓴다).\n"
    "- 코드를 쓰기 직전 developer-mode **작성 게이트 W1~W4**를 돌린다. 값·타입·반환·상수는 정의 원문을 열고 쓴다(R1·R7). "
    "우회 금지(try-catch로 묻기·`any`·검사 끄기·빌드 설정 우회) — 근본 원인을 고친다.\n"
    "- 셸이 없다(아래 `자기 검사`·`화면 확인` 명령이 있으면 그것만 예외). 빌드·테스트는 엔진이 돌린다. 삭제가 필요하면 하지 "
    "말고 `summary`에 적는다. 다른 셸 명령·맡지 않은 쓰기는 권한으로 거부된다 — 시도하면 턴만 버린다.\n"
    "- 결과는 JSON: `done`=이번에 끝낸 항목마다 `{id, files, note}` — `files`=그 항목을 위해 **실제로 바꾼 파일 경로 목록**"
    "(경로만) · 이미 체크된 항목의 근거를 고칠 때도 같은 형식 · `summary`=5줄 이내(버그 수정이면 `근본 원인:`, 런타임 데이터에 "
    "기대면 `데이터 가정:`) · `remaining`=**아직 남은 일과 다음에 할 일**(다 끝났으면 `없음`) · `write_gate`=`W1 grep N건 → … "
    "/ W2 … / W3 … / W4 x의 f()에 둠`(신규 코드 0줄이면 `해당없음 — 사유` · 평가자가 코드와 대조한다) · `question`=방향 질문(없으면 "
    "빈 문자열).\n"
    "- 한 번에 다 못 끝내도 된다: 끝낸 항목만 `done`에 넣고 `remaining`에 남긴다. 엔진이 같은 세션으로 「이어서」를 보낸다. "
    "단 끝낸 일도 새 체크도 파일 변경도 없는 답이 2번 이어지면 「진전 없음」으로 반려된다.\n"
    + QUESTION_RULE
)


def build_first(plan_rel, items):
    return (
        "# 역할: 작업자 (v6 — 이 묶음을 설계·구현)\n"
        f"TODO `{plan_rel}` 의 항목을 구현한다. 아래가 이번 묶음 전체다.\n\n"
        f"## 항목\n{items_text(items)}\n\n" + _BUILD_RULES
    )


def build_continue(items, remaining, check_info=""):
    info = (f"\n## 직전 호출 뒤 엔진 기계검사 (참고 — 아직 판정 아님, 남은 항목이 있어서)\n```\n{check_info}\n```\n"
            if check_info else "")
    return (
        "# 이어서 (같은 세션)\n"
        f"남은 항목을 이어서 구현한다. 네가 직전에 남긴 「남은 일」: {remaining or '(없음)'}\n\n"
        f"## 열린 항목\n{items_text(items)}\n" + info +
        "\n같은 규칙·같은 JSON 형식으로 답한다(`done`은 이번에 새로 끝낸 항목만)."
    )


def build_fix(reasons, items):
    lines = "\n".join(f"- [{r['source']}] {r['reason']}" for r in reasons)
    return (
        "# 결함 고치기 (같은 세션)\n"
        "아래 사유를 **전부** 해결한다 — 증상이 아니라 근본 원인을 고친다(작업 규칙 §문제 해결 원칙). 사유가 틀렸다고 "
        "판단되면 고치지 말고 `summary`에 근거(파일:라인)를 적는다 — 평가자가 다시 본다.\n\n"
        f"## 사유\n{lines}\n\n"
        f"## 아직 열린 항목\n{items_text(items)}\n\n"
        "같은 규칙·같은 JSON 형식으로 답한다(이미 체크된 항목을 고쳤으면 `done`에 그 항목과 바꾼 파일을 다시 넣는다)."
    )


def build_wiki(wiki_dir, changed):
    return (
        "# Wiki 갱신 (같은 세션 · 이번 묶음 마지막)\n"
        f"이번 묶음의 변경점으로 `{wiki_dir}/` 계약 문서를 갱신한다 — 네가 만든 코드를 네가 설명한다.\n"
        f"- 이번 묶음 변경 파일: {changed}\n"
        f"- 구성: `index.md` · `features/` · `architecture.md` · `decisions.md` · `operations.md` · `log.md`\n"
        "- 코드를 Read로 확인한 사실만 쓴다. `log.md` 에 이번 묶음 1줄 추가.\n"
        "- wiki는 **기능 계약**(이 기능은 이렇게 동작한다)만 담는다. 버전·기한·진행 트랙 같은 프로젝트 상태는 쓰지 않는다 "
        "(상태 정본은 프로젝트 현황서 — Master가 run 뒤에 기록).\n"
        "- 같은 문단은 한 줄에 이어 쓰고 문장 중간에서 줄을 끊지 않는다.\n"
        f"- `{wiki_dir}/` 밖 파일은 수정하지 않는다(엔진이 반려).\n"
        "- 결과는 같은 JSON 형식: `done`=빈 배열 · `summary`=무엇을 갱신했나 3줄 이내 · `remaining`=`없음` · "
        "`write_gate`=`해당없음 — 문서` · `question`=방향 질문(없으면 빈 문자열)."
    )


def self_check_rule(cmd):
    """v4 §11: appended to the builder's first call when engine.json `self_check` is on."""
    return (
        "\n\n## 제출 전 자기 검사 (필수)\n"
        f"- 셸 명령은 **이것 하나만** 허용된다: `{cmd} <네가 바꾼 파일 경로들>` (대상 폴더 기준 상대 경로, 공백으로 구분). "
        "다른 명령·`&&` 연결은 거부된다.\n"
        "- 코드를 다 쓴 뒤 돌려서 **통과(exit 0)할 때까지** 근본 원인을 고친다(우회 금지: 검사 끄기·`any`·무시 주석). "
        "결과 JSON `self_check`에 마지막 실행의 마지막 줄을 적는다.\n"
        "- 통과하면 끝이다 — **스스로 검수하지 않는다**(평가는 별도). 엔진이 제출 뒤 같은 검사를 다시 돌려 확인한다.\n"
    )


def self_check_fix(files, log):
    return (
        "# 자기 검사 실패 — 이어서 고친다 (같은 세션)\n"
        f"제출 뒤 엔진이 네 파일({', '.join(files)})에 자기 검사를 다시 돌렸더니 실패했다. 로그가 가리키는 오류의 "
        "**근본 원인**을 고치고, 자기 검사를 다시 돌려 통과시킨 뒤 같은 JSON 형식으로 답한다(`done`은 빈 배열 · "
        "`summary`에 `근본 원인:` + 고친 곳 · `remaining`은 그대로).\n\n"
        f"## 자기 검사 로그(엔진 실행)\n```\n{log[-4000:]}\n```\n"
    )


def contract_rule(files):
    """v5 D5: an agreement between modules is never broken by one item alone."""
    return (
        "\n\n## 약속 파일 (모듈 사이 약속)\n"
        "- 아래 파일은 여러 기능이 기대는 **약속**이다: " + ", ".join(f"`{f}`" for f in files) + "\n"
        f"- `{CONTRACT_MARK}` 표시가 있는 항목만 이 파일을 바꿀 수 있다. 가능하면 바꾸지 말고 **확장**(새 필드·새 함수 "
        "추가)한다. 기존 호출부 영향은 grep 전수로 확인한다(R2).\n"
    )


def original_rule(kind, refs):
    """v5 D2 → v6: no planner — the builder reads the original scene by scene before building it."""
    return (
        f"\n\n## {kind} — 원작을 먼저 본다\n"
        + ("- 원작 근거를 **Read로 직접 연다**: " + ", ".join(f"`{r}`" for r in refs) + "\n" if refs else "")
        + "- 장면(화면·연출 단위)마다 무엇이 보이고 언제 바뀌나(구도·크기·위치·색·순서·타이밍)를 먼저 정리하고, 그대로 "
        "만든다. 본 것만(R1) — 열지 못한 근거는 `summary`에 `미확인`.\n"
    )


# ---------------- evaluator ----------------
RUNNER_CONTRACT = {
    "web": (
        "시나리오 1개 = ES 모듈 파일 1개(`.mjs`). 형식:\n"
        "```js\nexport default async function ({ page, baseUrl, shot, log, blocked }) {\n"
        "  await page.goto(baseUrl);\n  // 조작…\n  await shot('01-시작');   // 캡처 = 증거. PASSED 에는 최소 1장 필수\n"
        "  // 기대와 다르면 throw new Error('무엇이 기대와 달랐나')  → FAILED\n"
        "  // 환경·데이터 때문에 실행 불가면 blocked('사유')        → BLOCKED\n}\n```\n"
        "page 는 Playwright Page(모바일 뷰포트). 외부 import 금지.\n"
        "- **시간에 따라 보이는 것(v5)**: 애니메이션·전환은 `await page.waitForTimeout(ms)` 사이에 `shot('01')`·`shot('02')`… "
        "여러 번 찍는다 — 엔진이 2장 이상이면 한 장의 시트(`sheet.jpg`, 찍은 순서대로 번호)로 묶는다."
    ),
    "godot": (
        "시나리오 1개 = `extends SceneTree` GDScript 파일 1개(`.gd`). 창을 띄워 실행된다(헤드리스 아님).\n"
        "- 캡처: `var img := root.get_viewport().get_texture().get_image(); img.save_png(OS.get_environment(\"AW_EVIDENCE_DIR\").path_join(\"01.png\"))`\n"
        "- 끝에서 결과 JSON을 `OS.get_environment(\"AW_RESULT_FILE\")` 에 쓴다: "
        "`{\"status\":\"PASSED|FAILED|BLOCKED\",\"note\":\"…\",\"evidence\":[\"01.png\"]}` 후 `quit()`.\n"
        "- 씬은 `load(\"res://…\")` 로 불러오고, 프레임은 `await process_frame` 으로 기다린다.\n"
        "- **시간에 따라 보이는 것(v5)**: 기다리며(`await create_timer(초).timeout`) `01.png`·`02.png`… 여러 장 저장하고 "
        "`evidence`에 순서대로 적는다 — 엔진이 2장 이상이면 한 장의 시트(`sheet.jpg`)로 묶는다."
    ),
    "command": "설정된 QA 명령이 시나리오 폴더를 실행하고 results.json 을 만든다. 시나리오 파일 형식은 명령 규약을 따른다.",
    "roblox": (
        "시나리오 1개 = Luau 코드 1개(`.luau`). **엔진이** Roblox Studio에서 플레이를 시작해 실행하고, 끝나면 스크린샷·콘솔을 "
        "남기고 플레이를 정지한다(시나리오마다 새 플레이). 형식:\n"
        "```lua\n-- context: Server          -- 첫 줄. Client도 가능(생략 = Server)\n"
        "local Players = game:GetService(\"Players\")\n-- 확인…(task.wait 가능)\n"
        "if 기대와_다르면 then return { status = \"FAILED\", note = \"무엇이 기대와 달랐나\" } end\n"
        "return { status = \"PASSED\", note = \"무엇을 확인했나\" }\n```\n"
        "- 반드시 `{status, note}` 테이블을 return 한다(`PASSED`/`FAILED`/`BLOCKED`). Luau 오류·다른 반환 = FAILED.\n"
        "- 환경·데이터 때문에 확인 불가면 `BLOCKED` + 사유. 키보드·마우스 입력 시뮬레이션은 아직 없다 — 상태·값·인스턴스를 "
        "코드로 읽어 판정한다.\n"
        "- **여러 컨텍스트(v4)**: Server에서 준비 → Client에서 조작 → 다시 관찰처럼 컨텍스트가 바뀌면 `code` 대신 "
        "`steps`=[{context, code}, …]를 쓴다(`code`는 빈 문자열). 엔진이 **한 플레이 안에서 순서대로** 실행한다. 앞 단계는 "
        "`nil`을 돌려주면 다음 단계로 가고(`FAILED`/`BLOCKED` 테이블이면 거기서 끝), **마지막 단계**가 `{status, note}`를 "
        "돌려준다. 단계 사이 값은 `workspace:SetAttribute(\"aw_x\", v)`로 넘긴다(Server → Client로 복제된다 — Client에서 "
        "Server 쪽으로는 복제되지 않는다). 복제·반영을 기다릴 땐 `task.wait(1)` 정도를 넣는다.\n"
        "- **대기 합계**: 시나리오 하나의 `task.wait(N)` 숫자 합이 엔진 상한(기본 60초)을 넘으면 실행 전에 반려된다.\n"
        "- **시간에 따라 보이는 것(v5)**: 컷신·애니메이션처럼 흘러가야 보이는 것은 시나리오가 그것을 **시작시키고 바로** "
        "return 하게 쓰고, 시나리오에 `frames`(2~12장)·`interval`(초)을 준다 — 엔진이 return 직후부터 그 간격으로 캡처하고 "
        "한 장의 시트(`sheet.jpg`, 번호 붙은 격자)로 묶는다. 안 주면 캡처 1장(`01.jpg`).\n"
        # v6 N14 (09-26 #13: a scenario left coins = 5,000,000 in the Studio DataStore and broke the next QA)
        "- 인스턴스·변수 같은 **게임 상태는 정지하면 사라지지만, DataStore에 저장한 값은 남는다**(Studio API 접근이 켜진 "
        "플레이스). 엔진은 설정된 정리 코드(`qa.roblox.reset_code`)를 플레이마다 전·후에 돌려 테스트 키를 지운다 — 시나리오는 "
        "정리 코드가 지우는 테스트 키·테스트 사용자로만 저장하고, 실제 키를 바꾸지 않는다. 파일·플레이스 저장은 하지 않는다.\n"
        "- ⚠️ **시나리오 코드는 게임 스크립트와 모듈 인스턴스가 따로다**(09-25 실측: 클라이언트 롤은 성공했는데 Server "
        "시나리오에서 `require`한 세션 모듈은 세션을 못 봄). 게임 스크립트의 `ModuleScript`를 `require`하면 **새 사본**이 "
        "나오므로 살아 있는 게임 상태(세션·인벤토리·장착·롤 수)를 읽거나 바꾸는 데 쓰지 않는다. 살아 있는 상태는 "
        "`-- context: Client`에서 **Remote로 조작하고 응답·GUI로 관찰**하거나, 게임이 인스턴스에 노출한 값(Attribute·"
        "Value 객체·leaderstats·GUI 문구)으로 읽는다. 서비스 모듈 `require`는 그 모듈의 순수 함수·새로 만든 객체를 "
        "시험할 때만 쓴다."
    ),
}


def coverage_note(omitted):
    """§23-6 (4): files the diff could not carry — named so the evaluator opens them."""
    if not omitted:
        return ""
    return ("## ⚠️ 아래 파일은 diff에 없음(길이 한도로 잘림) — 전부 Read로 직접 열어 평가할 것\n"
            + "\n".join(f"- `{p}`" for p in omitted)
            + "\n판정 `checked`에 이 파일들의 **경로**를 적는다(열어 본 것만).\n\n")


def _review_block(ctx):
    """The code under evaluation — shared by E1 · second view · rebuttal. ctx keys: changed · diff · omitted · check_log ·
    summary · bypass · denied · since."""
    bp = (f"## 새로 생긴 우회 흔적 (엔진 grep — code-health 축 L)\n{ctx['bypass']}\n"
          "정당한 이유(외부 라이브러리 타입 등)가 코드·요약에 없으면 high.\n\n") if ctx.get("bypass") else ""
    dn = (f"## 작업자 호출의 권한 거부 (허용 안 된 도구·경로를 시도함)\n{ctx['denied']}\n"
          "막힌 일 때문에 결과가 빠졌거나 다른 길로 우회했는지 본다. 빠졌는데 요약에 안 밝혔으면 high.\n\n"
          ) if ctx.get("denied") else ""
    return (
        # T4 smoke (10-02 23:33): the evaluator rejected the TODO's `완료 근거` line as the builder's out-of-scope edit
        "## TODO 파일은 평가 대상이 아니다\n공통 맥락의 TODO 파일에 있는 체크 표시(`[x]`)와 `완료 근거` 줄은 **엔진이** 작업자의 "
        "결과 JSON으로 쓴 것이다 — 작업자의 수정이 아니고, 범위 외·대상 밖 판정에 넣지 않는다(작업자가 TODO를 고치면 엔진이 "
        "막는다). 아래 「평가 대상」 목록과 diff에도 TODO 파일은 빠져 있다.\n\n"
        f"## 평가 대상 = {ctx.get('since') or 'run 시작'} 이후 바뀐 것\n{ctx.get('changed') or '(없음)'}\n\n"
        f"## 작업자 요약 (단서일 뿐 — 근거는 직접 읽은 파일)\n{ctx.get('summary') or '(없음)'}\n\n"
        + bp + dn +
        f"## 엔진 기계검사 (통과)\n```\n{ctx.get('check_log') or '(설정된 검사 없음)'}\n```\n\n"
        + coverage_note(ctx.get("omitted")) +
        f"## diff\n```diff\n{ctx.get('diff') or ''}\n```\n\n"
    )


def _original_block(kind, refs):
    if not kind:
        return ""
    return (f"## {kind} — 화면이 결과물이다\n"
            + ("- 원작 근거(기준)를 **Read로 직접 연다**: " + ", ".join(f"`{r}`" for r in refs) + "\n" if refs else
               "- 원작 근거 없음 — 요청 명세의 화면·연출 서술이 기준이다.\n")
            + "- 구도·색·크기·위치·순서·타이밍을 항목별로 대조한다. 원작에 없는 것·빠진 장면은 high.\n\n")


def master_block(notes):
    """v6 §6-1 H9 (implemented 10-03): the Master's reject reasons reach the evaluator too, until the run ends."""
    if not notes:
        return ""
    return ("## Master 반려 사유 (이번 run — 작업자에게도 같은 글이 갔다)\n"
            + "\n".join(f"- [{n.get('t', '')}] {n.get('reason', '')}" for n in notes) + "\n"
            "- 사유가 평가 쪽(시나리오·확인 범위)을 가리키면 네가 반영한다.\n\n")


_MAIN_VERDICTS_QA = (
    "## 판정 (네가 정한다 — 엔진은 네 판정대로 움직이기만 한다)\n"
    "- `RUN` + `scenarios`: 실제로 돌려 봐야 아는 것이 있으면(보통 이것). 엔진이 실행 전에 문법·대기 합계·개수만 확인하고 "
    "돌린 뒤 결과를 **이 세션으로** 돌려준다.\n"
    "- `REJECT` + `to_builder`: 코드만 봐도 high 결함 — 작업자에게 그대로 전해진다.\n"
    "- `READY`: 실행 없이도 완료 조건이 전부 확인되는 경우만(문서·주석만 바뀐 경우 등) — `reason`에 이유.\n"
    "- `UNSURE`: 시나리오로도 코드로도 확인할 수 없는 것이 있으면 — `reason`에 무엇을 Master가 직접 봐야 하나.\n"
)
_MAIN_VERDICTS_NO_QA = (
    "## 판정 (네가 정한다)\n"
    "- `REJECT` + `to_builder`(high 결함) · `READY` · `UNSURE`(`reason`에 Master가 볼 것). 이 run은 QA가 꺼져 있어 "
    "`RUN`은 쓸 수 없다.\n"
)
_COVERAGE = (
    "- **완료 조건마다** 무엇으로 확인하는지 정한다 — 어느 시나리오인가, 또는 코드로 확인했나. `reason`에 조건별로 1줄씩. "
    "러너로 확인할 수 없는 조건(예: 플레이를 두 번 이어야 보이는 것)은 억지로 넘기지 말고 `UNSURE`로 Master에게 올린다.\n"
)
_REBUTTAL = (
    "- `want_rebuttal`: READY를 낼 때, 이번 변경이 **반박 검토(새 세션 · 1회 약 $1~1.5)를 다시** 받아야 하면 true — 지난 "
    "반박 검토 뒤 기능 동작이 바뀌었거나 네 확신이 낮을 때. 문서·주석만 고쳤으면 false. (run의 첫 통과에는 엔진이 항상 "
    "부른다.)\n"
)


def eval_main(ctx, qa, master_notes=None, wiki_on=False, again=False):
    """The main evaluator's first look in an evaluation. qa: None (QA off) or {runner, environment, base_url, cap,
    carry}. again: a later evaluation (the same session — it decides what to look at and play again)."""
    if qa:
        carry = qa.get("carry") or ""
        task = (
            "## 시나리오 (QA 켜짐 — 네가 쓰고 엔진 러너가 실행한다)\n"
            f"- 실행 환경: {qa['environment']}" + (f", 주소 `{qa['base_url']}`" if qa.get("base_url") else "") + "\n"
            "- 몇 개가 충분한지는 네가 정한다 — 정상 흐름과 실패 흐름으로 완료 조건을 **실제 사용자가 하듯** 확인한다. "
            f"엔진 상한은 **{qa['cap']}개**(넘으면 실행 전에 돌려보낸다). 셀렉터·씬 경로는 코드를 Read로 확인한다.\n"
            "- 파일은 만들지 않는다 — 시나리오 코드를 JSON `scenarios`로 낸다(`item_ids`는 기록용 · 어느 항목을 보는지).\n"
            "- 캡처는 PASSED의 증거다(없으면 엔진이 BLOCKED로 강등). **찍을 화면이 없는 시나리오**(Kick·퇴장으로 클라이언트가 "
            "사라짐 · 서버 데이터만 확인)만 `no_capture`에 이유를 적는다 — 캡처를 건너뛰고 결과에 「캡처 생략」으로 표시된다. "
            "화면이 결과인 확인(시각·UI)에는 쓰지 않는다.\n"
            + (f"\n### 지난 시나리오와 결과 (사실 — 다시 쓸지 고칠지는 네가 정한다)\n{carry}\n"
               "- 엔진은 **네가 이번에 낸 시나리오만** 돌린다. 다시 돌릴 것은 다시 낸다.\n" if carry else "")
            + f"\n### 시나리오 규약 ({qa['runner']})\n{RUNNER_CONTRACT[qa['runner']]}\n\n"
        )
        verdicts = _MAIN_VERDICTS_QA
    else:
        task = "## 시나리오 없음 (이 run은 QA 꺼짐) — `scenarios`는 빈 배열\n\n"
        verdicts = _MAIN_VERDICTS_NO_QA
    head = ("# 역할: 주 평가자 — 다시 평가 (같은 세션)\n"
            "작업자가 네 지적(또는 Master 사유)을 반영했다. 아래 변경을 보고, **무엇을 다시 볼지·다시 돌릴지 네가 정한다** "
            "(이번 변경과 상관없는 확인을 의식처럼 반복하지 않는다).\n" if again else
            "# 역할: 주 평가자 (코드 평가 + 시나리오 + 판정)\n")
    return (
        head +
        "작업자가 끝낸 묶음을 평가한다. **실행으로는 안 보이는 것**(보안·우회 코드·구조·`기능 위치`·단일 소스·다른 기능 상태를 "
        "몰래 건드림·각 항목의 '완료 조건'이 코드에 빠짐없이 구현됐는지)은 코드로, **실제로 돌려 봐야 아는 것**은 시나리오로 "
        "본다. audit-inspector **코드 검수 8항목**을 전부 본다. 작업자 요약의 `작성게이트:`·`근본 원인:` 주장이 코드와 맞는지 "
        "대조한다(거짓·과장이면 high).\n"
        + ("- wiki가 켜진 run이다: 이번 변경이 wiki 계약 문서(`log.md` 포함)에 사실대로 반영됐는지도 본다.\n" if wiki_on else "")
        + "(ID·형식·대상 밖 파일·기계검사는 엔진이 이미 코드로 검사했다 — 너는 내용이 맞는지 본다.)\n"
        "- 네가 이 평가의 **주 평가자**다: 실행 결과, 2번째 관점·반박 검토(다른 새 세션)의 지적도 이 세션으로 온다. 무엇을 "
        "작업자에게 보내고 무엇을 네가 고칠지는 네가 정한다.\n\n"
        + master_block(master_notes)
        + _review_block(ctx) + _original_block(ctx.get("kind"), ctx.get("refs")) + task + verdicts + _COVERAGE
        + _REBUTTAL + "\n" + REVIEW_RULES + QUESTION_RULE
    )


NO_QA_NOTE = ("## 엔진 안내 (사실)\n이 run은 QA가 꺼져 있어 시나리오를 돌릴 수 없다. `REJECT` · `READY` · `UNSURE` 중에서 "
              "판정한다.\n")


def precheck_note(problems):
    return ("## 시나리오 실행 전 검사에서 걸림 (엔진 · 사실)\n- " + "\n- ".join(problems)
            + "\n고친 시나리오 **전체**를 `RUN` + `scenarios`로 다시 낸다(엔진은 낸 것만 돌린다).\n")


def results_block(attempt_dir, results, failures, logs, shots, visual_kind=None, refs=()):
    """The runner's results for the main evaluator. shots: [(scenario id, title, [image paths])] (visual run)."""
    summary = "\n".join(f"- `{r['id']}` → {r['status']}: {r.get('note', '')}" for r in results)
    lines = "\n".join(f"- `{sid}` {title}: " + ", ".join(f"`{p}`" for p in paths) for sid, title, paths in shots)
    vis = (f"\n### 시각 판정 ({visual_kind}) — 캡처를 Read로 직접 연다(`sheet.jpg` = 시간 순 프레임 격자)\n"
           + ("- 원작 근거를 먼저 연다: " + ", ".join(f"`{r}`" for r in refs) + "\n" if refs else "")
           + "- 화면이 요청·원작대로 보이는지 판정한다.\n"
           f"{lines or '(캡처 없음)'}\n") if visual_kind else ""
    return (f"## 네 시나리오 실행 결과 (러너 · 시도 폴더 `{attempt_dir}`)\n{summary}\n"
            + (f"\n### 통과 못 한 것\n{failures}\n" if failures else "")
            + "- `증거(캡처) 없는 PASSED → BLOCKED 강등`은 엔진이 붙인 사실이다(시나리오가 `shot()`을 안 불렀다). "
            "환경 때문에 실행 자체가 안 된 것으로 보이면 같은 시나리오로 다시 `RUN`(빈 `scenarios` = 직전 것 그대로) — "
            "반복되면 엔진 상한에서 Master에게 간다.\n"
            f"\n### 실행 기록(필요하면 Read)\n{logs}\n" + vis)


def other_eyes_block(name, v):
    """What a second pair of eyes (its own new session) said — for the main evaluator to weigh. Not routed by the engine."""
    iss = "\n".join(f"- [{i.get('severity')}] {i.get('where')}: {i.get('problem')}"
                    + (f" → {i['fix']}" if i.get("fix") else "") for i in v.get("issues") or [])
    return (f"## {name}의 판정: {v.get('verdict')}\n{v.get('reason') or ''}\n" + (f"{iss}\n" if iss else "")
            + "- 이 지적이 맞는지 **네가** 판단한다: 코드 결함이면 `REJECT` + `to_builder` · 네 시나리오·확인 범위 문제면 "
            "고쳐서 `RUN` · 맞지 않으면 근거(파일:라인)를 `reason`에 적고 `READY` · 판단이 안 서면 `UNSURE`.\n")


def eval_followup(inbox, qa_on):
    """The main evaluator's next call in the same session: what came back since its last answer."""
    body = "\n".join(inbox) if inbox else ("## 방향 질문에 대한 Master 답\n이 지시 끝의 「방향 질문 답변」을 반영해 판정을 "
                                         "다시 낸다.\n")
    return ("# 이어서 — 주 평가자 (같은 세션)\n" + body + "\n"
            + (_MAIN_VERDICTS_QA if qa_on else _MAIN_VERDICTS_NO_QA) + _COVERAGE + _REBUTTAL + "\n" + REVIEW_RULES)


def eval_second(ctx):
    return (
        "# 역할: 평가자 — 2번째 관점 (보안·데이터 · 새 세션)\n"
        "주 평가자가 같은 묶음을 통과로 봤다. 너는 **보안·데이터 관점만** 따로 본다: 권한·인증·입력 검증·시크릿 노출 · 데이터 "
        "구조·저장 형식·마이그레이션 안전성 · 동시성 · 약속 파일(모듈 사이 계약)의 기존 호출부 영향(grep 전수).\n"
        "- 판정: high 결함이 있으면 `REJECT` · 없으면 `READY` · 판단이 정말 안 서면 `UNSURE`(`reason`에 이유). 네 지적은 "
        "주 평가자에게 간다(작업자에게 바로 가지 않는다) — `to_builder`는 빈 문자열.\n\n"
        + _review_block(ctx) + REVIEW_RULES
    )


def eval_devil(ctx, scen_files, shots, results_text):
    """The rebuttal review (v6 D2): a NEW session tries to refute READY right before the end gate."""
    lines = "\n".join(f"- `{sid}` {title} (찍은 시점: {when}): " + ", ".join(f"`{p}`" for p in paths)
                      for sid, title, when, paths in shots)
    return (
        "# 역할: 평가자 — 반박 검토 (새 눈)\n"
        "다른 평가자가 이 묶음을 **통과(READY)** 로 판정했다. 너는 처음 보는 사람으로서 그 통과를 **반박해 본다**: 완료 조건이 "
        "정말 다 구현됐나 · 시나리오가 **완료 조건마다** 그것을 정말 확인했나(빠진 조건 · 통과하도록 느슨하게 쓴 기대값 · 확인 "
        "안 한 값 · 캡처 시점이 틀려 아무것도 증명 못 하는 캡처 · 「캡처 생략」의 이유가 맞나 — 화면으로 봐야 할 것을 생략하지 "
        "않았나) · 범위 외를 건드렸나 · 보안·데이터.\n"
        "- 반박할 근거가 없으면 `READY`. 있으면 `REJECT` + `issues`(무엇이 왜 틀렸나 — 코드인지 시나리오인지도 `problem`에 "
        "적는다). 판단이 정말 안 서면 `UNSURE`. 네 판정은 주 평가자에게 간다 — `to_builder`는 빈 문자열.\n\n"
        + _review_block(ctx) + _original_block(ctx.get("kind"), ctx.get("refs")) +
        (f"## 시나리오 파일\n" + "\n".join(f"- `{p}`" for p in scen_files) + "\n\n" if scen_files else
         "## 시나리오 없음 (QA 꺼짐 또는 문서만)\n\n")
        + (f"## 실행 결과\n{results_text}\n\n" if results_text else "")
        + (f"## 캡처\n{lines}\n\n" if lines else "") + REVIEW_RULES
    )
