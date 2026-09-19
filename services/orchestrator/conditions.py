"""selection_rules의 조건식 평가기.

조건 언어를 좁게 유지한다. eval()이나 임의 표현식을 쓰지 않는다.
프로파일은 데이터이고, 데이터가 코드를 실행해서는 안 된다.

지원 형태:
    safety_class: "B"                      # 동등 비교
    safety_class: { gte_level: "C" }       # 순서형 비교 (context_schema.ordered 필요)
    procurement:  { contains: "수출-미국" }  # 리스트 포함
    form:         { in: [standalone, web] } # 집합 포함
    ai_applied:   true                     # 불리언
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ContextError(Exception):
    """컨텍스트 스키마 위반. 리졸버가 거부해야 하는 상황."""

    def __init__(self, code: str, detail: str, offending: list[str] | None = None):
        self.code = code
        self.detail = detail
        self.offending = offending or []
        super().__init__(f"{code}: {detail}")


@dataclass
class ContextSchema:
    """합성된 컨텍스트 변수 정의. 각 레이어의 context_schema를 합친 결과."""

    vars: dict[str, dict[str, Any]] = field(default_factory=dict)

    def merge(self, other: dict[str, dict[str, Any]] | None) -> None:
        if not other:
            return
        for name, spec in other.items():
            if name in self.vars:
                # 후순위 레이어가 좁히는 것만 허용한다.
                self.vars[name] = {**self.vars[name], **spec}
            else:
                self.vars[name] = dict(spec)

    def validate(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """컨텍스트를 검증하고 기본값을 채운 새 딕셔너리를 반환한다."""
        resolved: dict[str, Any] = {}
        missing: list[str] = []

        for name, spec in self.vars.items():
            if name in ctx and ctx[name] is not None:
                resolved[name] = ctx[name]
            elif "default" in spec:
                resolved[name] = spec["default"]
            elif spec.get("required"):
                missing.append(name)

        if missing:
            raise ContextError(
                "missing_required_context",
                f"필수 컨텍스트 변수가 없다: {', '.join(sorted(missing))}",
                offending=sorted(missing),
            )

        # 스키마에 없는 변수는 조용히 버리지 않고 알린다.
        unknown = sorted(set(ctx) - set(self.vars))
        if unknown:
            raise ContextError(
                "unknown_context_value",
                f"정의되지 않은 컨텍스트 변수다: {', '.join(unknown)}",
                offending=unknown,
            )

        # 값 검증
        for name, value in resolved.items():
            spec = self.vars[name]
            vtype = spec.get("type")

            if vtype == "enum":
                allowed = spec.get("values", [])
                if value is not None and value not in allowed:
                    raise ContextError(
                        "invalid_context_value",
                        f"{name}의 값 '{value}'는 허용되지 않는다. 허용: {allowed}",
                        offending=[name, str(value)],
                    )

            elif vtype == "bool":
                if not isinstance(value, bool):
                    raise ContextError(
                        "invalid_context_value",
                        f"{name}은 불리언이어야 한다. 받은 값: {value!r}",
                        offending=[name, str(value)],
                    )

            elif vtype == "list":
                if not isinstance(value, list):
                    raise ContextError(
                        "invalid_context_value",
                        f"{name}은 리스트여야 한다. 받은 값: {value!r}",
                        offending=[name, str(value)],
                    )
                allowed = spec.get("values")
                if allowed:
                    bad = [v for v in value if v not in allowed]
                    if bad:
                        raise ContextError(
                            "invalid_context_value",
                            f"{name}에 허용되지 않는 값이 있다: {bad}. 허용: {allowed}",
                            offending=[name] + [str(b) for b in bad],
                        )

        return resolved

    def level_index(self, var: str, value: str) -> int:
        spec = self.vars.get(var)
        if not spec or not spec.get("ordered"):
            raise ContextError(
                "not_ordered",
                f"{var}는 순서형이 아니므로 등급 비교를 쓸 수 없다. "
                f"context_schema에서 ordered: true를 선언하라.",
                offending=[var],
            )
        values = spec.get("values", [])
        if value not in values:
            raise ContextError(
                "invalid_context_value",
                f"{var}의 값 '{value}'가 정의된 등급에 없다: {values}",
                offending=[var, value],
            )
        return values.index(value)


def _eval_one(schema: ContextSchema, var: str, cond: Any, actual: Any) -> bool:
    """단일 변수에 대한 조건 평가."""
    # 스칼라 직접 비교
    if isinstance(cond, (str, bool, int, float)):
        return actual == cond

    # 리스트 → 집합 포함
    if isinstance(cond, list):
        return actual in cond

    if not isinstance(cond, dict):
        raise ContextError("bad_condition", f"{var}의 조건 형식을 해석할 수 없다: {cond!r}")

    for op, operand in cond.items():
        if op == "eq":
            if actual != operand:
                return False
        elif op == "ne":
            if actual == operand:
                return False
        elif op == "in":
            if actual not in operand:
                return False
        elif op == "not_in":
            if actual in operand:
                return False
        elif op == "contains":
            # 리스트 컨텍스트에 특정 값이 들어 있는가
            if not isinstance(actual, (list, tuple, set, str)):
                return False
            if operand not in actual:
                return False
        elif op == "gte_level":
            if actual is None:
                return False
            if schema.level_index(var, actual) < schema.level_index(var, operand):
                return False
        elif op == "lte_level":
            if actual is None:
                return False
            if schema.level_index(var, actual) > schema.level_index(var, operand):
                return False
        elif op == "scheme":
            continue  # 메타 정보. 평가에 관여하지 않는다.
        else:
            raise ContextError("bad_condition", f"{var}에 알 수 없는 연산자다: {op}")

    return True


def evaluate(schema: ContextSchema, condition: dict[str, Any], ctx: dict[str, Any]) -> bool:
    """selection_rule의 if 블록을 평가한다. 모든 항목이 참이어야 발동(AND)."""
    if not condition:
        return False

    for var, cond in condition.items():
        if var not in schema.vars:
            raise ContextError(
                "unknown_rule_variable",
                f"규칙이 정의되지 않은 컨텍스트 변수를 참조한다: {var}",
                offending=[var],
            )
        actual = ctx.get(var, schema.vars[var].get("default"))
        if not _eval_one(schema, var, cond, actual):
            return False

    return True
