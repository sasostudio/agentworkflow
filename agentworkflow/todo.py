"""TODO ID / level / evidence / ownership rules enforced by a parser, not an LLM (design §7, §17).

Format (v2):
- [ ] DEV-001 [L2] text
  - 대상: src/a.ts, src/b.ts
  - 완료 조건: what the reviewer checks
  - 기능 위치: src/a.ts 의 handleX() — where a bug in this feature would be found (W4 · 09-24)
- [x] DEV-002 [L1] text
  - 대상: src/c.ts
  - 완료 조건: ...
  - 기능 위치: src/c.ts 의 render()
  - 완료 근거: src/c.ts (what changed)
"""

import fnmatch
import re

ITEM = re.compile(r"^(?P<indent>\s*)[-*]\s+\[(?P<mark>[ xX])\]\s+(?P<id>(?:DEV|FIX)-\d{3})(?![\w-])\s*"
                  r"(?:\[L(?P<lvl>[0-3])\]\s*)?(?P<text>.*)$")
ATTR = re.compile(r"^\s+[-*]\s*(?P<key>완료\s*근거|대상|완료\s*조건|기능\s*위치|선행)\s*[:：]\s*(?P<body>.*)$")
DEP_ID = re.compile(r"\b(?:DEV|FIX)-\d{3}\b")
_NO_DEPS = re.compile(r"^(없음|없다|해당\s*없음|none|-|—)\s*[.)]*$", re.I)
EVIDENCE = re.compile(r"^\s+[-*]\s*완료\s*근거\s*[:：]\s*(?P<body>.*)$")
ANY_ID = re.compile(r"\b(?:DEV|FIX)-\d+\b")
DESIGN_HEADING = re.compile(r"^##\s*설계\s*$", re.M)
DESIGN_PARTS = ["파일", "시그니처", "흐름", "경계", "검증"]


def _key(k):
    return {"완료근거": "evidence", "대상": "target", "완료조건": "criteria",
            "기능위치": "location", "선행": "deps_raw"}[re.sub(r"\s", "", k)]


def _deps(raw):
    """`선행:` body -> [ids]; "없음" -> []; None (line missing) -> None."""
    if raw is None:
        return None
    return [] if _NO_DEPS.match(raw.strip()) else DEP_ID.findall(raw)


def parse(text):
    items, errors, seen = [], [], set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = ITEM.match(line)
        if not m:
            if ANY_ID.search(line) and re.match(r"^\s*[-*]\s+\[", line):
                errors.append(f"{i+1}행 형식 오류(ID는 DEV-### / FIX-### 3자리): {line.strip()}")
            continue
        tid = m.group("id")
        if tid in seen:
            errors.append(f"ID 중복: {tid}")
        seen.add(tid)
        attrs = {"evidence": None, "target": None, "criteria": None, "location": None, "deps_raw": None}
        item_indent = _indent(m.group("indent"))
        attr_indent = None  # set by the first attribute line; attributes count only at exactly that indent
        j = i + 1
        while j < len(lines):  # the item's attribute block
            line = lines[j]
            if not line.strip() or ITEM.match(line):  # P1 (§23-3): a nested TODO item ends the parent's block
                break
            ind = _indent(line)
            if ind <= item_indent:
                break
            am = ATTR.match(line)
            if am and (attr_indent is None or ind == attr_indent):
                attr_indent = ind
                attrs[_key(am.group("key"))] = am.group("body").strip()
            elif attr_indent is not None and ind <= attr_indent:
                break  # a non-attribute line at attribute level ends the block (unchanged behaviour)
            # otherwise: deeper sub-list of the attribute above — skip, even if it looks like an attribute
            # (C-1 09-25 run004: `기능 위치:` after a nested ⓐ~ⓖ list was missed · P2: `- 대상: 없음(설명)` inside a
            #  sub-list overwrote the parent's 대상)
            j += 1
        items.append({"id": tid, "checked": m.group("mark") in "xX",
                      "level": int(m.group("lvl")) if m.group("lvl") is not None else None,
                      "text": m.group("text").strip(), **attrs, "deps": _deps(attrs["deps_raw"]),
                      "evidence_files": evidence_files(attrs["evidence"]),
                      "target_files": evidence_files(attrs["target"])})
    return items, errors


def _indent(s):
    """Leading-whitespace width with tabs as 4 columns (§23-3: tab-indented plans must parse too)."""
    s = s.expandtabs(4)
    return len(s) - len(s.lstrip())


def evidence_files(body):
    if not body:
        return []
    out = []
    # drop (explanations) BEFORE splitting — they often contain commas (e2e run002 false reject)
    # peel innermost pairs repeatedly so nested "(a(b))" also goes (09-24 found by a regression test)
    while True:
        peeled = re.sub(r"[(（][^()（）]*[)）]", "", body)
        if peeled == body:
            break
        body = peeled
    for part in re.split(r"[,，;]", body):
        part = part.strip().strip("`'\"")
        if part and not re.search(r"\s", part):  # a path token (Makefile, src/a.ts); prose has spaces
            p = part.replace("\\", "/")
            out.append(p[2:] if p.startswith("./") else p)
    return out


def _normalize_for_dev(text):
    """Strip what the developer IS allowed to change: checkbox marks and evidence lines.
    Level tags, 대상, 완료 조건 and text stay — only the Master (and the engine's level rules) change them."""
    out = []
    for line in text.splitlines():
        if EVIDENCE.match(line):
            continue
        m = ITEM.match(line)
        if m:
            tag = f"[L{m.group('lvl')}] " if m.group("lvl") is not None else ""
            line = f"{m.group('indent')}- [ ] {m.group('id')} {tag}{m.group('text').strip()}"
        out.append(line.rstrip())
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _file_matches(ev_file, changed):
    ev_file = ev_file.lower()
    for c in changed:
        c = c.lower()
        if c == ev_file or c.endswith("/" + ev_file) or ev_file.endswith("/" + c):
            return True
    return False


def check_dev(before, after, changed_files):
    """Rules for a BUILD call (v6 — the TODO is the Master's, the engine writes the checks). Returns list of
    violation strings (empty = pass)."""
    v = []
    items, errs = parse(after)
    v += errs
    if _normalize_for_dev(before) != _normalize_for_dev(after):
        v.append("작업 중에 TODO 문구·ID·레벨·대상·완료 조건·항목 구성이 바뀜 — 항목은 Master만 바꾼다 "
                 "(체크·완료 근거는 엔진이 결과 JSON으로 쓴다 · 대상이 더 필요하면 `question`)")
    for it in items:
        if not it["checked"]:
            continue
        if not it["evidence"]:
            v.append(f"{it['id']} 체크됐지만 '완료 근거' 없음 — 근거 없이는 체크할 수 없다")
            continue
        if not it["evidence_files"]:
            v.append(f"{it['id']} 완료 근거에 파일 경로가 없음")
            continue
        missing = [f for f in it["evidence_files"] if not _file_matches(f, changed_files)]
        if missing:
            v.append(f"{it['id']} 완료 근거 파일이 실제 변경 목록에 없음: {', '.join(missing)}")
    return v


def check_plan(before, after, run_level=1, require_design=True):
    """Rules for a TODO written or extended by the Master (before may be '' on the first plan).
    require_design=False (v6 B1): the builder designs inside its own work — no `## 설계` section is required."""
    v = []
    new_items, errs = parse(after)
    v += errs
    if not new_items:
        v.append("TODO 항목이 하나도 없음 (- [ ] DEV-001 [L1] ... 형식)")
    old = {i["id"]: i for i in parse(before)[0]} if before else {}
    new = {i["id"]: i for i in new_items}
    for tid, it in new.items():
        if it["level"] is None:
            v.append(f"{tid} 레벨 태그 없음 — `{tid} [L1]` 처럼 L0~L3 를 붙인다")
        if not it["target"]:
            v.append(f"{tid} '대상:' 줄 없음 (바꿀·만들 파일)")
        if not it["criteria"]:
            v.append(f"{tid} '완료 조건:' 줄 없음 (검수가 판정할 기준)")
        if not it["location"]:
            v.append(f"{tid} '기능 위치:' 줄 없음 — 이 기능을 어느 파일·함수에 두나(착수 전 브리핑 ③ · W4)")
        if it["deps"] is None:  # §23-6 S5: required, "없음" written out
            v.append(f"{tid} '선행:' 줄 없음 — 다른 항목이 만드는 것을 쓰면 그 ID, 없으면 `선행: 없음`")
        else:
            if tid in it["deps"]:
                v.append(f"{tid} 선행에 자기 자신")
            unknown = [x for x in it["deps"] if x not in new]
            if unknown:
                v.append(f"{tid} 선행에 없는 ID: {', '.join(unknown)}")
        prev = old.get(tid)
        if prev is None:
            if it["checked"]:
                v.append(f"{tid} 새 항목이 체크된 상태 — 체크는 개발 AI만 한다")
        else:
            if prev["checked"] != it["checked"]:
                v.append(f"{tid} 체크 상태를 항목 추가 때 바꿈 — 체크는 엔진이 작업자 결과로 한다")
            if prev["evidence"] != it["evidence"]:
                v.append(f"{tid} 완료 근거를 항목 추가 때 바꿈")
    for tid, prev in old.items():
        if tid not in new and prev["checked"]:
            v.append(f"{tid} 완료된 항목을 삭제함")
    if require_design and run_level >= 2 and not DESIGN_HEADING.search(after):
        v.append(f"run 난이도 L{run_level} — 기획 문서에 `## 설계` 절 필요(파일·시그니처·흐름·경계)")
    cyc = _dep_cycle({tid: [d for d in (it["deps"] or []) if d in new and d != tid] for tid, it in new.items()})
    if cyc:
        v.append("선행 순환: " + " → ".join(cyc))
    return v


def _dep_cycle(graph):
    """-> one dependency cycle as [id, ..., id] or []."""
    state, stack = {}, []

    def visit(n):
        state[n] = 1
        stack.append(n)
        for m in graph.get(n, []):
            if state.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if not state.get(m):
                found = visit(m)
                if found:
                    return found
        state[n] = 2
        stack.pop()
        return []
    for n in graph:
        if not state.get(n):
            found = visit(n)
            if found:
                return found
    return []


def clean_files(files):
    """C-4 (09-25 run006) · 엔진 세션 보완 ②: a worker's `files` list -> (normalised paths, problems).
    Backslash → '/', surrounding space/quotes/backticks stripped, duplicates dropped; empty list, empty entries and
    entries with whitespace (prose, not a path) are problems."""
    out, problems = [], []
    if not isinstance(files, list) or not files:
        return [], ["files 가 비어 있음 — 바꾼 파일 경로를 목록으로"]
    for f in files:
        p = str(f or "").strip().strip("`'\"").strip().replace("\\", "/")
        if p.startswith("./"):
            p = p[2:]
        if not p:
            problems.append("빈 경로")
        elif re.search(r"\s", p):
            problems.append(f"경로에 공백(설명은 note에): {p[:60]}")
        elif p not in out:
            out.append(p)
    return out, problems


def evidence_line(files, note):
    """C-4 · 보완 ①: the engine writes the 완료 근거 itself — paths first, then the note with every character the
    evidence parser treats as structure (parentheses, commas, semicolons) removed, so an unbalanced '(' in a long
    note can never swallow a path again."""
    body = ", ".join(files)
    clean = re.sub(r"[()（）,，;；]", " ", note or "")
    clean = re.sub(r"\s+", " ", clean).strip()[:200]
    return body + (f" ({clean})" if clean else "")


def mark_done(text, tid, evidence):
    """§23-6 S5 / v4 §9: the ENGINE checks an item (no AI edits the TODO file). `[x]` + a `완료 근거:` line right
    under the item. An item that is already checked gets its 완료 근거 line replaced (a rework correcting evidence)."""
    out, lines = [], text.splitlines(keepends=True)
    in_item, replaced = False, False
    for line in lines:
        bare = line.rstrip("\r\n")
        m = ITEM.match(bare)
        if m:
            in_item = m.group("id") == tid
        if m and in_item:
            nl = line[len(bare):] or "\n"
            ind = m.group("indent")
            out.append(line.replace("[ ]", "[x]", 1))
            out.append(f"{ind}  - 완료 근거: {evidence}{nl}")
            replaced = True
            continue
        if in_item and replaced and EVIDENCE.match(bare):
            continue  # the old 완료 근거 line of this item — replaced by the one written above
        out.append(line)
    return "".join(out)


# ---------- v4 §9: JSON -> documents (the engine writes them; no AI edits a TODO / design file) ----------
_ID = re.compile(r"^(?:DEV|FIX)-\d{3}$")


def item_md(it, checked=False, evidence=None):
    """One TODO item from its JSON. Empty fields are left out on purpose: check_plan then reports them exactly as
    before (the rule checks stay in one place)."""
    lvl = it.get("level")
    head = f"- [{'x' if checked else ' '}] {it.get('id', '')}" + (f" [L{lvl}]" if isinstance(lvl, int) else "")
    # #3 (09-27~28, 3 times): a planner put line breaks / a list into `text` — the attribute lines below it were then
    # read as part of the text and the item looked field-less. One item = one text line, always.
    lines = [f"{head} {' '.join(str(it.get('text') or '').split())}"]
    if evidence:
        lines.append(f"  - 완료 근거: {evidence}")
    targets = ", ".join(str(t).strip() for t in (it.get("targets") or []) if str(t).strip())
    for key, val in (("대상", targets), ("완료 조건", it.get("criteria")), ("기능 위치", it.get("location"))):
        if str(val or "").strip():
            lines.append(f"  - {key}: {' '.join(str(val).split())}")
    deps = it.get("deps")
    if isinstance(deps, list):
        lines.append("  - 선행: " + (", ".join(str(d).strip() for d in deps) if deps else "없음"))
    return "\n".join(lines) + "\n"


def json_item_problems(items, prefix=None):
    """What JSON can get wrong that the md checks would not name clearly: id format and duplicates.
    prefix None = DEV-### or FIX-### (a replan may carry FIX items); "FIX" for follow-up items (approve/answer --follow-up · --items)."""
    v, seen = [], set()
    for it in items:
        tid = str(it.get("id") or "")
        if not _ID.match(tid) or (prefix and not tid.startswith(prefix)):
            v.append(f"형식 오류(ID는 {(prefix or 'DEV') + '-###'} 3자리): {tid or '(빈 id)'}")
        if tid in seen:
            v.append(f"ID 중복: {tid}")
        seen.add(tid)
    return v


CONTRACT_MARK = "[약속 변경]"


def contract_problems(items, contract_files):
    """v5 D5: an item may touch an agreement between modules (engine.json `contract_files`) only when
    its text says `[약속 변경]` — so the change is visible to the reviewer and the Master before anyone builds it."""
    pats = [p for p in contract_files or [] if p]
    v = []
    for it in items:
        hit = [t for t in it.get("targets") or [] for p in pats if _in_target(t, p) or _in_target(p, t)]
        if hit and CONTRACT_MARK not in str(it.get("text") or ""):
            v.append(f"{it.get('id')} 이 약속 파일({', '.join(sorted(set(hit))[:3])})을 건드리는데 text에 "
                     f"`{CONTRACT_MARK}` 표시 없음 — 바꿀 거면 표시하고 무엇을 바꾸나 적는다(가능하면 확장)")
    return v


def render_plan(plan, prior=""):
    """`PLAN_SCHEMA` -> TODO markdown. Items already checked in `prior` keep their [x] and 완료 근거."""
    done = {i["id"]: i for i in parse(prior)[0] if i["checked"]} if prior else {}
    body = "# 계획\n" + str(plan.get("summary") or "").strip() + "\n\n"
    if str(plan.get("design") or "").strip():
        body += "## 설계\n" + str(plan["design"]).strip() + "\n\n"
    for it in plan.get("items") or []:
        d = done.get(it.get("id"))
        body += item_md(it, checked=bool(d), evidence=d["evidence"] if d else None)
    return body


def append_items(text, items):
    """Follow-up / added items (FIX-###) go after the existing TODO."""
    return text.rstrip("\n") + "\n" + "".join(item_md(it) for it in items)


def render_design(designs, prior=""):
    """`DESIGN_SCHEMA.designs` -> design markdown; sections of ids not in `designs` are kept from `prior`."""
    new_ids = {str(d.get("id")) for d in designs}
    kept = [s for s in re.split(r"(?m)^(?=##\s+(?!#))", prior or "") if s.strip()
            and not any(re.match(rf"^##\s*{re.escape(i)}\b", s) for i in new_ids)]
    parts = {"파일": "files", "시그니처": "signature", "흐름": "flow", "경계": "boundary", "검증": "verify"}
    out = []
    for d in designs:
        sec = f"## {d.get('id')} {str(d.get('title') or '').strip()}".rstrip() + "\n"
        for kor, key in parts.items():
            if str(d.get(key) or "").strip():  # an empty part is left out -> check_design names it
                sec += f"### {kor}\n{str(d[key]).strip()}\n"
        out.append(sec)
    return "".join(s if s.endswith("\n") else s + "\n" for s in kept) + "".join(out)


def set_level(text, tid, level):
    """Rewrite one item's [Lx] tag (engine-side: floor clamp and reviewer level changes)."""
    out = []
    for line in text.splitlines(keepends=True):
        m = ITEM.match(line.rstrip("\r\n"))
        if m and m.group("id") == tid:
            nl = line[len(line.rstrip("\r\n")):]
            line = (f"{m.group('indent')}- [{m.group('mark')}] {tid} [L{level}] {m.group('text').strip()}" + nl)
        out.append(line)
    return "".join(out)


def check_design(text, ids):
    """Every L3 item needs `## DEV-00N` with the five `### ` parts (design §17-4)."""
    v = []
    for tid in ids:
        m = re.search(rf"^##\s*{re.escape(tid)}\b.*$", text, re.M)
        if not m:
            v.append(f"{tid} 설계 절(`## {tid}`) 없음")
            continue
        nxt = re.search(r"^##\s+(?!#)", text[m.end():], re.M)
        body = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():]
        missing = [p for p in DESIGN_PARTS if not re.search(rf"^###\s*{p}", body, re.M)]
        if missing:
            v.append(f"{tid} 설계에 빠진 부분: " + ", ".join(f"### {p}" for p in missing))
    return v


def unchecked(text):
    return [i for i in parse(text)[0] if not i["checked"]]


def next_fix_id(text):
    nums = [int(i["id"][4:]) for i in parse(text)[0] if i["id"].startswith("FIX-")]
    return f"FIX-{(max(nums) + 1) if nums else 1:03d}"


def _in_target(path, target):
    """target token: a file, a folder ending with '/', or a glob."""
    p, t = path.lower().replace("\\", "/"), target.lower().replace("\\", "/")
    if t.endswith("/"):
        return p.startswith(t) or ("/" + t) in ("/" + p)
    if any(ch in t for ch in "*?["):
        return fnmatch.fnmatch(p, t)
    return _file_matches(t, [p])


def outside_targets(changed_files, text):
    """Changed files not covered by any item's `대상:` (the engine-side edit-gate, decision 3 · 09-24)."""
    targets = [t for it in parse(text)[0] for t in it["target_files"]]
    return [f for f in changed_files if not any(_in_target(f, t) for t in targets)]
