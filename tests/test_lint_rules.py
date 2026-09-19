"""profile-lint 규칙 회귀 테스트.

린터 규칙은 정상 프로파일만으로는 검증되지 않는다. 규칙을 지워도 통과하기
때문이다. 각 규칙마다 위반 프로파일을 만들어 실제로 걸리는지 확인한다.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "orchestrator"))

from profile_lint import lint  # noqa: E402

PROFILES = ROOT / "packages" / "profiles"

# (규칙 코드, 설명, 위반 레이어)
CASES: list[tuple[str, str, dict]] = [
    (
        "PL-02",
        "extends 체인 순환",
        {
            "schema_version": "1.0", "id": "XL-cycle-a", "kind": "industry",
            "name": "순환 A", "extends": "XL-cycle-a",
        },
    ),
    (
        "PL-05",
        "C등급 산출물에 생성 프롬프트 연결",
        {
            "schema_version": "1.0", "id": "XL-c-prompt", "kind": "industry",
            "name": "C등급 프롬프트", "status": "draft",
            "artifacts": [
                {"id": "BAD-01", "name": "시험 결과서",
                 "derivable": "C", "prompt": "prompts/x.md"}
            ],
        },
    ),
    (
        "PL-07",
        "오버레이가 id_scheme 설정",
        {
            "schema_version": "1.0", "id": "XL-ov-id", "kind": "overlay",
            "name": "불량 오버레이", "id_scheme": {"requirement": "X-{n:03d}"},
        },
    ),
    (
        "PL-08",
        "추적 규칙에 정의되지 않은 노드 타입",
        {
            "schema_version": "1.0", "id": "XL-bad-node", "kind": "industry",
            "name": "불량 노드", "status": "draft",
            "traceability_rules_add": [
                {"from": "requirement", "to": "ghost_node", "relation": "verifies"}
            ],
        },
    ),
    (
        "PL-09",
        "골든셋 없이 stable",
        {
            "schema_version": "1.0", "id": "XL-no-golden", "kind": "industry",
            "name": "골든셋 없음", "status": "stable",
        },
    ),
    (
        "PL-11",
        "규칙이 정의되지 않은 컨텍스트 변수 참조",
        {
            "schema_version": "1.0", "id": "XL-ghost-var", "kind": "industry",
            "name": "유령 변수", "status": "draft",
            "selection_rules": [
                {"id": "SR-X", "if": {"never_declared": True},
                 "then": {"notes": ["x"]}}
            ],
        },
    ),
    (
        "PL-12",
        "규칙이 없는 산출물 참조",
        {
            "schema_version": "1.0", "id": "XL-ghost-art", "kind": "industry",
            "name": "유령 산출물", "status": "draft",
            "context_schema": {"flag": {"type": "bool", "default": True}},
            "selection_rules": [
                {"id": "SR-X", "if": {"flag": True},
                 "then": {"add_artifacts": ["NO-SUCH"]}}
            ],
        },
    ),
    (
        "PL-13",
        "규칙이 추가하는 표준에 risk_trigger 없음",
        {
            "schema_version": "1.0", "id": "XL-no-trigger", "kind": "industry",
            "name": "발동조건 누락", "status": "draft",
            "context_schema": {"flag": {"type": "bool", "default": True}},
            "standards": [
                {"id": "가상 표준", "enforcement": "L2", "layer": "international",
                 "audit_body": "x", "edition_year": 2020}
            ],
            "selection_rules": [
                {"id": "SR-X", "if": {"flag": True},
                 "then": {"add_standards": ["가상 표준"]}}
            ],
        },
    ),
]


def run_case(code: str, name: str, layer: dict) -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "profiles"
        root.mkdir(parents=True)
        (root / "schema").mkdir()
        shutil.copy(PROFILES / "schema" / "profile.schema.json", root / "schema")
        parent = "overlay" if layer["kind"] == "overlay" else "industry"
        d = root / parent / layer["id"]
        d.mkdir(parents=True)
        with (d / "profile.yaml").open("w", encoding="utf-8") as f:
            yaml.safe_dump(layer, f, allow_unicode=True)

        findings = lint(root)
        codes = {f.rule for f in findings if f.severity == "error"}
        if code not in codes:
            all_codes = sorted({f.rule for f in findings})
            failures.append(f"{code}이 오류로 걸리지 않았다. 검출된 규칙: {all_codes}")
    return failures


def test_clean_pack_has_no_errors() -> list[str]:
    """현재 프로파일 팩에 오류가 없어야 한다 — 위양성 확인."""
    findings = lint(PROFILES)
    errors = [f for f in findings if f.severity == "error"]
    return [f"현재 팩에 오류가 있다: {e}" for e in errors]


def main() -> int:
    total = failed = 0
    for code, name, layer in CASES:
        total += 1
        fails = run_case(code, name, layer)
        if fails:
            failed += 1
            print(f"[FAIL] {code} — {name}")
            for f in fails:
                print(f"        - {f}")
        else:
            print(f"[PASS] {code} — {name}")

    total += 1
    fails = test_clean_pack_has_no_errors()
    if fails:
        failed += 1
        print("[FAIL] 정상 팩에 위양성 없음")
        for f in fails:
            print(f"        - {f}")
    else:
        print("[PASS] 정상 팩에 위양성 없음")

    print(f"\n린터 규칙: {total - failed}/{total} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
