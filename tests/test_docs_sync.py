"""지침 파일 동기화 검사.

AGENTS.md가 단일 원본이고, CLAUDE.md는 이를 가져오며,
.github/copilot-instructions.md는 핵심만 압축한 사본이다.

사본이 원본과 어긋나면 지침 자체가 거짓말이 된다. 도구마다 다른 규칙을
읽으면 같은 저장소에서 서로 다른 결론이 나온다. 핵심 불변조건이 세 곳에
모두 살아 있는지 기계로 확인한다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

AGENTS = ROOT / "AGENTS.md"
CLAUDE = ROOT / "CLAUDE.md"
COPILOT = ROOT / ".github" / "copilot-instructions.md"
PROFILE_INSTR = ROOT / ".github" / "instructions" / "profile-yaml.instructions.md"

# 세 파일 모두에 살아 있어야 하는 불변조건.
# (라벨, 각 파일에서 찾을 정규식)
INVARIANTS: list[tuple[str, str]] = [
    ("C등급 프롬프트 금지", r"derivable[:\s`]*C.*프롬프트|C등급.*프롬프트|`derivable: C`"),
    ("PL-05 언급", r"PL-05"),
    ("증거 앵커 필수", r"증거 앵커|앵커 없는|evidence.*앵커"),
    ("추정 금지 변수", r"safety_class"),
    ("표준 원문 복제 금지", r"원문.*복제|원문 텍스트|조항 번호 참조"),
    ("준거와 인증 구분", r"준거.*인증|인증 주장"),
    ("strictest-wins", r"strictest-wins"),
    ("2단계 규칙 적용", r"2단계"),
    ("효과 우선순위", r"`?add`? *>.*`?drop`? *>.*`?relax`?"),
    ("조건부 필터링", r"조건부.*(제외|필터)"),
    ("PL-13 발동조건", r"PL-13"),
    ("변이 테스트", r"변이|망가뜨"),
    ("골든셋 필수", r"골든셋"),
    ("한국어 문체", r"한국어"),
]

# AGENTS.md에만 있어야 하는 것 — copilot은 짧게 유지한다
COPILOT_MAX_LINES = 90


def read(p: Path) -> str:
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def check_invariants() -> list[str]:
    failures: list[str] = []
    texts = {
        "AGENTS.md": read(AGENTS),
        "CLAUDE.md": read(CLAUDE),
        ".github/copilot-instructions.md": read(COPILOT),
    }

    # CLAUDE.md는 AGENTS.md를 가져오므로 합쳐서 본다
    if "@AGENTS.md" in texts["CLAUDE.md"]:
        texts["CLAUDE.md"] = texts["CLAUDE.md"] + "\n" + texts["AGENTS.md"]

    for label, pattern in INVARIANTS:
        rx = re.compile(pattern, re.IGNORECASE | re.DOTALL)
        missing = [name for name, body in texts.items() if not rx.search(body)]
        if missing:
            failures.append(f"'{label}'이 빠진 파일: {', '.join(missing)}")
    return failures


def check_claude_imports() -> list[str]:
    body = read(CLAUDE)
    if "@AGENTS.md" not in body:
        return ["CLAUDE.md가 @AGENTS.md를 가져오지 않는다. 지침이 갈라진다."]
    return []


def check_copilot_length() -> list[str]:
    body = read(COPILOT)
    lines = [l for l in body.splitlines() if l.strip()]
    if len(lines) > COPILOT_MAX_LINES:
        return [
            f"copilot-instructions.md가 {len(lines)}줄이다 "
            f"(상한 {COPILOT_MAX_LINES}). 매 요청에 주입되므로 길면 희석된다. "
            f"세부는 AGENTS.md로 옮겨라."
        ]
    return []


def check_copilot_points_to_source() -> list[str]:
    body = read(COPILOT)
    if "AGENTS.md" not in body:
        return ["copilot-instructions.md가 AGENTS.md를 가리키지 않는다."]
    return []


# 문서에 나오는 저장소 경로를 추출한다. 고정 목록을 쓰면 새로 추가된
# 오타를 놓치므로, 본문에서 직접 뽑아 실재 여부를 확인한다.
PATH_RX = re.compile(
    r"(?:^|[\s`(\"'])"
    r"((?:tests|services|packages|\.github|\.vscode)/[A-Za-z0-9_./\-*]+"
    r"\.(?:py|sh|ya?ml|json|md))"
)

# 와일드카드가 든 경로는 glob으로 확인한다
def _path_ok(rel: str) -> bool:
    if "*" in rel:
        return any(ROOT.glob(rel))
    return (ROOT / rel).exists()


def check_referenced_paths_exist() -> list[str]:
    """지침이 언급하는 경로가 실제로 존재하는지 확인한다."""
    failures: list[str] = []
    seen: set[str] = set()
    for name, path in [
        ("AGENTS.md", AGENTS),
        ("CLAUDE.md", CLAUDE),
        (".github/copilot-instructions.md", COPILOT),
        (".github/instructions/profile-yaml.instructions.md", PROFILE_INSTR),
    ]:
        body = read(path)
        for rel in PATH_RX.findall(body):
            key = f"{name}::{rel}"
            if key in seen:
                continue
            seen.add(key)
            if not _path_ok(rel):
                failures.append(f"{name}이 없는 경로를 언급한다: {rel}")
    return failures


def check_vscode_schema_binding() -> list[str]:
    """VS Code 스키마 바인딩이 실제 스키마 파일을 가리키는지 확인한다."""
    import json
    p = ROOT / ".vscode" / "settings.json"
    if not p.exists():
        return [".vscode/settings.json이 없다"]
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f".vscode/settings.json 파싱 실패: {exc}"]

    schemas = cfg.get("yaml.schemas", {})
    if not schemas:
        return ["yaml.schemas 설정이 없다. 편집 중 인라인 검증을 받지 못한다."]

    failures = []
    for schema_path in schemas:
        target = ROOT / schema_path.lstrip("./")
        if not target.exists():
            failures.append(f"yaml.schemas가 없는 스키마를 가리킨다: {schema_path}")
    return failures


def check_pl_rules_documented() -> list[str]:
    """린터가 구현한 PL 규칙이 단일 원본(AGENTS.md)에 있는지 확인한다.

    경로 한정 지침(profile-yaml.instructions.md)에만 있으면, resolver.py를
    고치는 에이전트는 그 규칙을 보지 못한다. 원본에 있어야 한다.
    """
    lint_src = read(ROOT / "services" / "orchestrator" / "profile_lint.py")
    implemented = set(re.findall(r'Finding\("(PL-\d+)"', lint_src))
    agents = read(AGENTS)
    missing = sorted(r for r in implemented if r not in agents)
    if missing:
        return [f"구현됐으나 AGENTS.md에 없는 린터 규칙: {', '.join(missing)}"]

    # 반대 방향 — 문서에만 있고 구현되지 않은 규칙
    documented = set(re.findall(r"\bPL-\d+\b", agents))
    phantom = sorted(documented - implemented)
    if phantom:
        return [f"AGENTS.md에 있으나 구현되지 않은 린터 규칙: {', '.join(phantom)}"]
    return []


CHECKS = [
    ("핵심 불변조건이 세 파일에 모두 있다", check_invariants),
    ("CLAUDE.md가 AGENTS.md를 가져온다", check_claude_imports),
    ("copilot 지침이 짧게 유지된다", check_copilot_length),
    ("copilot 지침이 원본을 가리킨다", check_copilot_points_to_source),
    ("지침이 언급하는 경로가 실재한다", check_referenced_paths_exist),
    ("VS Code 스키마 바인딩이 유효하다", check_vscode_schema_binding),
    ("구현된 린터 규칙이 문서화되어 있다", check_pl_rules_documented),
]


def main() -> int:
    failed = 0
    for name, fn in CHECKS:
        fails = fn()
        if fails:
            failed += 1
            print(f"[FAIL] {name}")
            for f in fails:
                print(f"        - {f}")
        else:
            print(f"[PASS] {name}")

    print(f"\n지침 동기화: {len(CHECKS) - failed}/{len(CHECKS)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
