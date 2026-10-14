"""The ONE place that names the AI roles, their session keys and the rule files they carry. If a role-bound behaviour
is missing on a call (answers block · self-check · dev shot · effort floor · web/MCP grant · role file), start here.

session key            model key   role file (rules_root/)   what it does
builder                builder     roles/builder.md          designs + builds one bundle, then its wiki
evaluator              evaluator   roles/evaluator.md        review + QA scenarios + judgment (one session)
evaluator:second       evaluator   roles/evaluator.md        second view (security · data) on L3/risky runs
evaluator:devil        evaluator   roles/evaluator.md        one fresh rebuttal review right before READY

Every call's system prompt = the working rules (rules_root/WORKING_RULES.md) + the role file (frontmatter dropped,
switchable with `role_definitions`). Both are identical for every call of a role, so they are cached and, after a
compaction, re-applied unchanged (the recorded system prompt is reused until a compaction)."""

from pathlib import Path

BUILDER = "builder"
EVALUATOR = "evaluator"
SECOND = "evaluator:second"
DEVIL = "evaluator:devil"
SESSION_KEYS = (BUILDER, EVALUATOR, SECOND, DEVIL)
EVALUATOR_KEYS = (EVALUATOR, SECOND, DEVIL)


def model_key(session_key):
    """session key -> config `models` / `capabilities` / effort role."""
    return BUILDER if session_key == BUILDER else EVALUATOR


AGENT_FILE = {BUILDER: "roles/builder.md", EVALUATOR: "roles/evaluator.md"}
RULES_FILE = "WORKING_RULES.md"

# A builder that follows a "brief first, then wait for approval" procedure (from the target repo's own instructions)
# ends its call with a plan only. So the override comes BEFORE the definition as well as after it.
ENGINE_HEAD = (
    "[엔진 모드 — 아래 정의서보다 우선 · 끝에 다시 적음] 이 호출은 엔진 안의 작업이다. 다른 지침에 착수 전 "
    "브리핑·승인 대기 같은 대화 절차가 있어도 **하지 않는다** — 지시받은 일을 끝까지 하고, 결과는 사용자 메시지가 "
    "지정한 JSON으로만 낸다.\n\n"
)
ENGINE_MODE = (
    "\n\n[엔진 모드 — 위 역할 정의서보다 우선]\n"
    "- 이 호출은 AgentWorkflow 엔진 안의 작업이다. 역할 정의서의 **역할·품질 기준·자기 검증·금지 사항은 그대로** 따른다.\n"
    "- 결과는 **사용자 메시지가 지정한 JSON 형식으로만** 낸다. 착수 전 브리핑은 TODO(`대상` · `기능 위치`)로 **이미 "
    "Master 승인을 받았다**. 계획·브리핑만 내고 멈추면 엔진이 「진전 없음」으로 반려하고 호출만 버린다 — **지시받은 "
    "작업을 끝까지 한다**. 방향이 갈리거나 막힐 때만 JSON의 `question`(방향 질문)으로 올린다 — 엔진이 Master에게 "
    "전달한다.\n"
    "- 같은 대화가 이어질 수 있다(엔진이 이 세션을 다음 지시에 다시 쓴다). 앞에서 한 일을 기억하되, 파일의 지금 상태는 "
    "늘 Read로 확인한다.\n"
    "- 다른 서브에이전트를 부르지 않는다(Agent·Task 도구 없음). 사람에게 묻는 도구(AskUserQuestion)도 쓰지 않는다.\n"
)


def _body(path):
    t = Path(path).read_text(encoding="utf-8")
    if t.startswith("---"):
        parts = t.split("---", 2)
        if len(parts) == 3:
            t = parts[2]
    return t.strip()


def rules_text(rules_root):
    """The working rules every role carries, or "" when the file is missing (the run start checks it exists)."""
    p = Path(rules_root) / RULES_FILE
    return f"# 작업 규칙 ({RULES_FILE})\n\n" + _body(p) if p.is_file() else ""


def system_text(key, rules_root):
    """The role definition for a session or model key, or "" when there is none (unknown key / file missing — the
    role's prompt still carries its rules; the run start checks the files exist)."""
    name = AGENT_FILE.get(model_key(key) if key in SESSION_KEYS else key)
    if not name:
        return ""
    p = Path(rules_root) / name
    if not p.is_file():
        return ""
    return f"# 역할 정의서 ({name})\n\n" + ENGINE_HEAD + _body(p) + ENGINE_MODE


def missing(rules_root, role_definitions=True):
    """Rule files the roles need but that are not there (checked at run start)."""
    root = Path(rules_root)
    names = {RULES_FILE} | (set(AGENT_FILE.values()) if role_definitions else set())
    return sorted(n for n in names if not (root / n).is_file())
