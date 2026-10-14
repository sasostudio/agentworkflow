"""docs/wiki contract documents (design §9). Never overwrites existing files."""

from pathlib import Path

SKELETON = {
    "index.md": "# Wiki 색인\n\n- [architecture](architecture.md)\n- [decisions](decisions.md)\n- [operations](operations.md)\n- [log](log.md)\n- features/\n",
    "architecture.md": "# 아키텍처\n\n(코드에서 확인한 구조만 적는다)\n",
    "decisions.md": "# 결정 기록\n\n| 날짜 | 결정 | 근거 |\n|---|---|---|\n",
    "operations.md": "# 운영\n\n(빌드·배포·실행 절차)\n",
    "log.md": "# 변경 로그\n\n",
    "features/README.md": "# 기능 문서\n\n기능 1개 = 파일 1개.\n",
}


def exists(target, wiki_dir):
    return (Path(target) / wiki_dir).is_dir()


def create_skeleton(target, wiki_dir):
    base = Path(target) / wiki_dir
    made = []
    for rel, body in SKELETON.items():
        p = base / rel
        if p.exists():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        made.append(f"{wiki_dir}/{rel}")
    return made


def listing(target, wiki_dir, index_chars=3000):
    base = Path(target) / wiki_dir
    if not base.is_dir():
        return "(이 프로젝트에는 wiki가 없다)"
    files = sorted(p.relative_to(target).as_posix() for p in base.rglob("*.md"))
    idx = base / "index.md"
    head = idx.read_text(encoding="utf-8")[:index_chars] if idx.is_file() else ""
    return "파일:\n" + "\n".join(f"- `{f}`" for f in files) + (f"\n\nindex.md:\n{head}" if head else "")
