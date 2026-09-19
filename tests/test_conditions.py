"""조건 평가기 단위 테스트.

골든셋은 D04 규칙 조합을 통해서만 평가기를 간접 검증하므로,
D04에 우연히 나타나지 않는 연산자 의미(등급 비교 등)는 여기서 직접 검증한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "orchestrator"))

from conditions import ContextError, ContextSchema, evaluate  # noqa: E402


def make_schema() -> ContextSchema:
    s = ContextSchema()
    s.merge({
        "safety_class": {"type": "enum", "values": ["A", "B", "C"],
                         "ordered": True, "required": True},
        "unordered_grade": {"type": "enum", "values": ["x", "y"], "default": "x"},
        "procurement": {"type": "list",
                        "values": ["민간B2B", "수출-미국", "수출-EU"],
                        "default": ["민간B2B"]},
        "flag": {"type": "bool", "default": False},
    })
    return s


CASES: list[tuple[str, callable]] = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("gte_level: C 조건은 C에서만 참")
def t1(s):
    cond = {"safety_class": {"gte_level": "C"}}
    assert evaluate(s, cond, {"safety_class": "C"}) is True
    assert evaluate(s, cond, {"safety_class": "B"}) is False
    assert evaluate(s, cond, {"safety_class": "A"}) is False


@case("gte_level: B 조건은 B와 C에서 참 — 동등비교와 구별된다")
def t2(s):
    cond = {"safety_class": {"gte_level": "B"}}
    assert evaluate(s, cond, {"safety_class": "C"}) is True, "C >= B여야 한다"
    assert evaluate(s, cond, {"safety_class": "B"}) is True
    assert evaluate(s, cond, {"safety_class": "A"}) is False


@case("lte_level: B 조건은 A와 B에서 참")
def t3(s):
    cond = {"safety_class": {"lte_level": "B"}}
    assert evaluate(s, cond, {"safety_class": "A"}) is True
    assert evaluate(s, cond, {"safety_class": "B"}) is True
    assert evaluate(s, cond, {"safety_class": "C"}) is False


@case("순서형이 아닌 변수에 등급 비교를 쓰면 거부")
def t4(s):
    try:
        evaluate(s, {"unordered_grade": {"gte_level": "y"}}, {"unordered_grade": "x"})
    except ContextError as e:
        assert e.code == "not_ordered", e.code
        return
    raise AssertionError("거부되어야 한다")


@case("contains: 리스트 컨텍스트 포함 검사")
def t5(s):
    cond = {"procurement": {"contains": "수출-미국"}}
    assert evaluate(s, cond, {"procurement": ["민간B2B", "수출-미국"]}) is True
    assert evaluate(s, cond, {"procurement": ["민간B2B"]}) is False
    assert evaluate(s, cond, {"procurement": []}) is False


@case("복수 조건은 AND로 결합된다")
def t6(s):
    cond = {"safety_class": {"gte_level": "B"}, "flag": True}
    assert evaluate(s, cond, {"safety_class": "C", "flag": True}) is True
    assert evaluate(s, cond, {"safety_class": "C", "flag": False}) is False
    assert evaluate(s, cond, {"safety_class": "A", "flag": True}) is False


@case("정의되지 않은 변수를 참조하면 거부")
def t7(s):
    try:
        evaluate(s, {"ghost_var": True}, {})
    except ContextError as e:
        assert e.code == "unknown_rule_variable", e.code
        return
    raise AssertionError("거부되어야 한다")


@case("필수 변수 누락 시 검증 단계에서 거부")
def t8(s):
    try:
        s.validate({"flag": True})
    except ContextError as e:
        assert e.code == "missing_required_context", e.code
        assert "safety_class" in e.offending
        return
    raise AssertionError("거부되어야 한다")


@case("enum 범위 밖 값은 거부")
def t9(s):
    try:
        s.validate({"safety_class": "D"})
    except ContextError as e:
        assert e.code == "invalid_context_value", e.code
        return
    raise AssertionError("거부되어야 한다")


@case("리스트 변수에 허용되지 않는 값이 있으면 거부")
def t10(s):
    try:
        s.validate({"safety_class": "A", "procurement": ["수출-일본"]})
    except ContextError as e:
        assert e.code == "invalid_context_value", e.code
        return
    raise AssertionError("거부되어야 한다")


@case("불리언 변수에 문자열을 넣으면 거부")
def t11(s):
    try:
        s.validate({"safety_class": "A", "flag": "true"})
    except ContextError as e:
        assert e.code == "invalid_context_value", e.code
        return
    raise AssertionError("거부되어야 한다")


@case("기본값이 채워진다")
def t12(s):
    resolved = s.validate({"safety_class": "B"})
    assert resolved["flag"] is False
    assert resolved["procurement"] == ["민간B2B"]


def main() -> int:
    failures = 0
    for name, fn in CASES:
        schema = make_schema()
        try:
            fn(schema)
            print(f"[PASS] {name}")
        except AssertionError as exc:
            failures += 1
            print(f"[FAIL] {name}\n        - {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {name}\n        - 예상치 못한 예외: {type(exc).__name__}: {exc}")

    print(f"\n조건 평가기: {len(CASES) - failures}/{len(CASES)} 통과")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
