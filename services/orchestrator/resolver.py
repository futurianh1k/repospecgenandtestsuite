"""프로파일 리졸버.

레이어를 순서대로 합성한 뒤 selection_rules를 평가해 최종 프로파일을 만든다.

합성 원칙:
  - 집합 필드(standards, gates, lint_rules)는 strictest-wins
  - 단일값 필드(id_scheme, output_default)는 last-wins
  - lifecycle은 lifecycle 레이어만 설정 가능

_provenance를 남겨 "어느 레이어가 이 요구를 만들었는가"를 역추적할 수 있게 한다.
감리·심사 대응에 필요하다.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from conditions import ContextError, ContextSchema, evaluate

ENFORCEMENT_RANK = {"L1": 4, "L2": 3, "L3": 2, "L4": 1}
LAYER_KINDS = ["base", "lifecycle", "industry", "overlay", "org", "project"]

SINGLE_VALUE_FIELDS = [
    "id_scheme",
    "output_default",
    "derivable_policy",
    "evidence_policy",
    "risk_scheme",
]


class ProfileError(Exception):
    def __init__(self, code: str, detail: str, offending: list[str] | None = None):
        self.code = code
        self.detail = detail
        self.offending = offending or []
        super().__init__(f"{code}: {detail}")


# ---------------------------------------------------------------------------
# 로딩
# ---------------------------------------------------------------------------

class ProfileStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._cache: dict[str, dict[str, Any]] = {}

    def load(self, rel: str) -> dict[str, Any]:
        if rel in self._cache:
            return copy.deepcopy(self._cache[rel])
        path = self.root / rel / "profile.yaml"
        if not path.exists():
            raise ProfileError("layer_not_found", f"레이어를 찾을 수 없다: {rel}", [rel])
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        self._cache[rel] = data
        return copy.deepcopy(data)

    def find_layer(self, layer_id: str) -> str:
        """레이어 id로 상대 경로를 찾는다."""
        if layer_id == "_base":
            return "_base"
        for parent in ("lifecycle", "industry", "overlay", "org"):
            candidate = self.root / parent / layer_id
            if (candidate / "profile.yaml").exists():
                return f"{parent}/{layer_id}"
        # 오버레이는 X1 같은 짧은 코드로도 참조된다
        for parent in ("overlay", "industry"):
            base = self.root / parent
            if base.exists():
                for child in sorted(base.iterdir()):
                    if child.name.startswith(layer_id + "-") and (child / "profile.yaml").exists():
                        return f"{parent}/{child.name}"
        raise ProfileError("layer_not_found", f"레이어를 찾을 수 없다: {layer_id}", [layer_id])


# ---------------------------------------------------------------------------
# 병합 헬퍼
# ---------------------------------------------------------------------------

def _union_standards(acc: list[dict], incoming: list[dict]) -> list[dict]:
    index = {s["id"]: s for s in acc}
    for item in incoming:
        sid = item["id"]
        # 발동 조건 없이 선언된 표준은 무조건 적용된다. 이 사실을 병합 과정에
        # 보존해야, 다른 레이어의 규칙이 같은 표준을 조건부로 참조하더라도
        # 무조건 선언이 덮이지 않는다. (공공 프로파일의 개인정보 보호법처럼
        # 산업 레이어는 조건부로, 횡단 오버레이는 무조건으로 선언하는 경우)
        unconditional = not item.get("risk_trigger")
        if sid not in index:
            entry = dict(item)
            entry["_unconditional"] = unconditional
            # 조건 없이 선언됐을 때의 강제성. 조건이 발동하지 않으면 이 값이 적용된다.
            # 예: KISA 개발보안 가이드는 평시 L3(권고)이나 보안약점 진단 대상이면
            # L2(계약 필수)로 올라간다. 둘을 구분하지 않으면 진단 대상이 아닌
            # 사업에도 L2가 찍혀 준거 목록이 과장된다.
            entry["_unconditional_enforcement"] = item["enforcement"] if unconditional else None
            index[sid] = entry
            continue
        current = index[sid]
        # strictest-wins: enforcement 최댓값
        if ENFORCEMENT_RANK[item["enforcement"]] > ENFORCEMENT_RANK[current["enforcement"]]:
            merged = {**current, **item}
        else:
            merged = {**item, **current}
            merged["enforcement"] = current["enforcement"]
        merged["_unconditional"] = bool(current.get("_unconditional")) or unconditional
        candidates = [
            e for e in (current.get("_unconditional_enforcement"),
                        item["enforcement"] if unconditional else None)
            if e
        ]
        merged["_unconditional_enforcement"] = (
            max(candidates, key=lambda e: ENFORCEMENT_RANK[e]) if candidates else None
        )
        # overlays는 합집합
        ov = sorted(set(current.get("overlays", [])) | set(item.get("overlays", [])))
        if ov:
            merged["overlays"] = ov
        index[sid] = merged
    return list(index.values())


def _union_artifacts(acc: list[dict], incoming: list[dict]) -> list[dict]:
    index = {a["id"]: a for a in acc}
    for item in incoming:
        aid = item["id"]
        index[aid] = {**index.get(aid, {}), **item}
    return list(index.values())


def _stricter_gate(a: dict, b: dict) -> dict:
    op = a.get("op", "gte")
    if op != b.get("op", "gte"):
        # 연산자가 다르면 차단성이 높은 쪽을 택한다
        return a if a.get("blocking", True) else b
    if op == "gte":
        return a if a["threshold"] >= b["threshold"] else b
    if op == "lte":
        return a if a["threshold"] <= b["threshold"] else b
    return a


def _union_gates(acc: list[dict], incoming: list[dict]) -> list[dict]:
    index = {g["name"]: g for g in acc}
    for item in incoming:
        name = item["name"]
        index[name] = _stricter_gate(index[name], item) if name in index else dict(item)
    return list(index.values())


SEVERITY_RANK = {"error": 3, "warning": 2, "info": 1}


def _union_lint(acc: list[dict], incoming: list[dict]) -> list[dict]:
    index = {r["id"]: r for r in acc}
    for item in incoming:
        rid = item["id"]
        if rid in index:
            cur = index[rid]
            keep = cur if SEVERITY_RANK[cur["severity"]] >= SEVERITY_RANK[item["severity"]] else item
            index[rid] = {**item, **cur, "severity": keep["severity"]}
        else:
            index[rid] = dict(item)
    return list(index.values())


def _trace_key(r: dict) -> tuple:
    return (r["from"], r["to"], r["relation"])


def _union_traces(acc: list[dict], incoming: list[dict]) -> list[dict]:
    index = {_trace_key(r): dict(r) for r in acc}
    for item in incoming:
        key = _trace_key(item)
        if key in index:
            cur = index[key]
            cur["required"] = bool(cur.get("required")) or bool(item.get("required"))
            cur["bidirectional"] = bool(cur.get("bidirectional")) or bool(item.get("bidirectional"))
        else:
            index[key] = dict(item)
    return list(index.values())


def _merge_layer(acc: dict[str, Any], layer: dict[str, Any]) -> dict[str, Any]:
    kind = layer.get("kind")

    # lifecycle 보호: lifecycle 레이어만 생명주기를 설정할 수 있다
    if "lifecycle" in layer and kind != "lifecycle":
        raise ProfileError(
            "lifecycle_mutation_forbidden",
            f"{layer['id']}({kind})는 lifecycle을 설정할 수 없다. lifecycle 레이어에서만 가능하다.",
            [layer["id"]],
        )
    if kind == "lifecycle":
        acc["lifecycle"] = layer["lifecycle"]

    # 오버레이는 id_scheme을 바꿀 수 없다 (PL-07)
    if kind == "overlay" and "id_scheme" in layer:
        raise ProfileError(
            "id_scheme_mutation_forbidden",
            f"오버레이 {layer['id']}는 id_scheme을 변경할 수 없다.",
            [layer["id"]],
        )

    acc["standards"] = _union_standards(
        acc.get("standards", []),
        layer.get("standards", []) + layer.get("standards_add", []),
    )
    acc["artifacts"] = _union_artifacts(
        acc.get("artifacts", []),
        layer.get("artifacts", []) + layer.get("artifacts_add", []),
    )
    acc["quality_gates"] = _union_gates(
        acc.get("quality_gates", []),
        layer.get("quality_gates", []) + layer.get("quality_gates_add", []),
    )
    acc["lint_rules"] = _union_lint(
        acc.get("lint_rules", []),
        layer.get("lint_rules", []) + layer.get("lint_rules_add", []),
    )
    acc["traceability_rules"] = _union_traces(
        acc.get("traceability_rules", []),
        layer.get("traceability_rules", []) + layer.get("traceability_rules_add", []),
    )

    # id_scheme은 병합(키 단위 last-wins), 나머지 단일값은 통째 교체
    if "id_scheme" in layer:
        acc["id_scheme"] = {**acc.get("id_scheme", {}), **layer["id_scheme"]}
    for key in SINGLE_VALUE_FIELDS:
        if key == "id_scheme":
            continue
        if key in layer:
            acc[key] = layer[key]

    for key in ("selection_rules", "conflict_rules", "minimum_set",
                "lint_includes", "output_formats", "default_overlays"):
        if key in layer:
            existing = acc.get(key, [])
            if key in ("selection_rules",):
                seen = {r["id"] for r in existing}
                existing = existing + [r for r in layer[key] if r["id"] not in seen]
            else:
                existing = existing + [v for v in layer[key] if v not in existing]
            acc[key] = existing

    for key in ("name", "description", "tier", "status", "ksic", "ncs_ref",
                "audit_bodies", "lifecycle_options", "lifecycle_default"):
        if key in layer and kind in ("industry", "org", "project"):
            acc[key] = layer[key]

    return acc


# ---------------------------------------------------------------------------
# 규칙 적용
# ---------------------------------------------------------------------------

def _collect_effect(intents: dict[str, Any], effect: dict[str, Any], rule_id: str) -> None:
    """1단계: 규칙 효과를 의도(intent)로 수집만 한다. 아직 적용하지 않는다.

    규칙을 만나는 즉시 적용하면 결과가 규칙 순서에 좌우된다. 완화가 필수화보다
    먼저 평가되면 충돌 감지 자체가 일어나지 않아, 조직 포크가 규칙을 추가하는
    순간 결과가 뒤집힌다. 전부 수집한 뒤 우선순위로 해소해야 순서 독립성이 선다.
    """
    for aid in effect.get("add_artifacts", []):
        intents["add_artifacts"].setdefault(aid, []).append(rule_id)
    for aid in effect.get("relax_artifacts", []):
        intents["relax_artifacts"].setdefault(aid, []).append(rule_id)
    for aid in effect.get("drop_artifacts", []):
        intents["drop_artifacts"].setdefault(aid, []).append(rule_id)
    for sid in effect.get("add_standards", []):
        intents["add_standards"].setdefault(sid, []).append(rule_id)
    for ov in effect.get("require_overlays", []):
        intents["require_overlays"].setdefault(ov, []).append(rule_id)
    for gate in effect.get("add_gates", []):
        intents["add_gates"].append((rule_id, gate))
    for tr in effect.get("add_trace_rules", []):
        intents["add_trace_rules"].append((rule_id, tr))
    for note in effect.get("notes", []):
        intents["notes"].append((rule_id, note))


def _resolve_intents(
    resolved: dict[str, Any],
    intents: dict[str, Any],
    all_artifacts: dict[str, dict],
) -> None:
    """2단계: 수집된 의도를 우선순위에 따라 해소한다.

    산출물 상태 우선순위 — 엄격한 쪽이 이긴다:
        필수화(add) > 제거(drop) > 완화(relax)

    add가 drop을 이기는 이유: 규제가 요구한 산출물을 편의 규칙이 지울 수 없다.
    drop이 relax를 이기는 이유: 적용 범위 밖이라 제거된 산출물은 선택항목으로
    남을 이유가 없다.
    """
    trace = resolved["_rule_trace"]
    index = resolved["_artifact_index"]

    adds = intents["add_artifacts"]
    relaxes = intents["relax_artifacts"]
    drops = intents["drop_artifacts"]

    for aid, rules in sorted(adds.items()):
        if aid not in all_artifacts:
            raise ProfileError(
                "unknown_artifact_ref",
                f"규칙 {rules[0]}가 정의되지 않은 산출물을 참조한다: {aid}",
                [rules[0], aid],
            )
        art = dict(all_artifacts[aid])
        art["required"] = True
        art["activated_by"] = rules
        index[aid] = art
        for rid in rules:
            trace.append({"rule": rid, "action": "add_artifact", "target": aid})

    for aid, rules in sorted(drops.items()):
        if aid in adds:
            for rid in rules:
                trace.append({
                    "rule": rid, "action": "drop_skipped", "target": aid,
                    "reason": f"{', '.join(adds[aid])}가 필수화했다",
                })
            continue
        index.pop(aid, None)
        for rid in rules:
            trace.append({"rule": rid, "action": "drop_artifact", "target": aid})

    for aid, rules in sorted(relaxes.items()):
        if aid in adds:
            for rid in rules:
                trace.append({
                    "rule": rid, "action": "relax_skipped", "target": aid,
                    "reason": f"{', '.join(adds[aid])}가 필수화했다",
                })
            continue
        if aid in drops:
            for rid in rules:
                trace.append({
                    "rule": rid, "action": "relax_skipped", "target": aid,
                    "reason": f"{', '.join(drops[aid])}가 제거했다",
                })
            continue
        if aid in index:
            index[aid]["required"] = False
            index[aid]["relaxed_by"] = rules
            for rid in rules:
                trace.append({"rule": rid, "action": "relax_artifact", "target": aid})
        else:
            # 조건부 산출물이 활성화되지 않은 상태에서의 완화는 무의미하다.
            # 오류는 아니지만 규칙 설계 점검 신호이므로 흔적을 남긴다.
            for rid in rules:
                trace.append({
                    "rule": rid, "action": "relax_noop", "target": aid,
                    "reason": "해당 산출물이 활성화되지 않았다",
                })

    for sid, rules in sorted(intents["add_standards"].items()):
        resolved["_activated_standards"].add(sid)
        for rid in rules:
            trace.append({"rule": rid, "action": "add_standard", "target": sid})

    for ov, rules in sorted(intents["require_overlays"].items()):
        resolved["_required_overlays"].add(ov)
        for rid in rules:
            trace.append({"rule": rid, "action": "require_overlay", "target": ov})

    if intents["add_gates"]:
        resolved["quality_gates"] = _union_gates(
            resolved["quality_gates"], [g for _, g in intents["add_gates"]]
        )
        for rid, gate in intents["add_gates"]:
            trace.append({"rule": rid, "action": "add_gate", "target": gate["name"]})

    if intents["add_trace_rules"]:
        resolved["traceability_rules"] = _union_traces(
            resolved["traceability_rules"], [t for _, t in intents["add_trace_rules"]]
        )
        for rid, tr in intents["add_trace_rules"]:
            trace.append({
                "rule": rid, "action": "add_trace_rule",
                "target": f"{tr['from']}->{tr['to']}:{tr['relation']}",
            })

    for rid, note in intents["notes"]:
        resolved["notes"].append({"rule": rid, "note": note})


def _new_intents() -> dict[str, Any]:
    return {
        "add_artifacts": {},
        "relax_artifacts": {},
        "drop_artifacts": {},
        "add_standards": {},
        "require_overlays": {},
        "add_gates": [],
        "add_trace_rules": [],
        "notes": [],
    }


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def resolve(
    store: ProfileStore,
    industry: str,
    lifecycle: str,
    context: dict[str, Any] | None = None,
    overlays: list[str] | None = None,
    org: str | None = None,
    project_overrides: dict[str, Any] | None = None,
    auto_overlay: bool = True,
) -> dict[str, Any]:
    context = dict(context or {})
    explicit_overlays = list(overlays or [])

    # --- 1단계: 베이스 레이어 합성 -------------------------------------------
    layers: list[dict[str, Any]] = [store.load("_base")]
    layers.append(store.load(store.find_layer(lifecycle)))
    industry_layer = store.load(store.find_layer(industry))
    layers.append(industry_layer)

    # 생명주기 허용 여부 검사
    allowed_lc = industry_layer.get("lifecycle_options")
    if allowed_lc and lifecycle not in allowed_lc:
        raise ProfileError(
            "lifecycle_not_allowed",
            f"{industry}는 생명주기 '{lifecycle}'를 허용하지 않는다. 허용: {allowed_lc}",
            [lifecycle, industry],
        )

    # 기본 오버레이 + 명시 오버레이
    overlay_ids = list(dict.fromkeys(
        industry_layer.get("default_overlays", []) + explicit_overlays
    ))

    schema = ContextSchema()
    for layer in layers:
        schema.merge(layer.get("context_schema"))

    # 오버레이를 붙이기 전에 컨텍스트를 검증한다.
    # 필수 변수 누락은 여기서 잡혀야 한다.
    resolved_ctx = schema.validate(context)

    # --- 2단계: 선택 규칙 1차 평가로 필요한 오버레이를 확정 --------------------
    rule_source: list[dict[str, Any]] = []
    for layer in layers:
        rule_source.extend(layer.get("selection_rules", []))

    if auto_overlay:
        for rule in rule_source:
            try:
                fired = evaluate(schema, rule["if"], resolved_ctx)
            except ContextError:
                fired = False
            effect = rule.get("then") if fired else rule.get("else")
            if effect:
                overlay_ids.extend(effect.get("require_overlays", []))
        overlay_ids = list(dict.fromkeys(overlay_ids))

    # --- 3단계: 오버레이 레이어 합성 ------------------------------------------
    for ov in sorted(overlay_ids):
        layers.append(store.load(store.find_layer(ov)))
    if org:
        layers.append(store.load(store.find_layer(org)))
    if project_overrides:
        layers.append({**project_overrides, "kind": "project", "id": "project_override"})

    schema = ContextSchema()
    for layer in layers:
        schema.merge(layer.get("context_schema"))
    resolved_ctx = schema.validate(context)

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = _merge_layer(merged, layer)

    # --- 4단계: 선택 규칙 최종 평가 -------------------------------------------
    all_artifacts = {a["id"]: a for a in merged.get("artifacts", [])}
    # 조건부 산출물은 규칙이 활성화할 때까지 제외한다
    artifact_index = {
        aid: dict(a) for aid, a in all_artifacts.items() if not a.get("conditional")
    }

    resolved: dict[str, Any] = {
        **merged,
        "_artifact_index": artifact_index,
        "_activated_standards": set(),
        "_required_overlays": set(overlay_ids),
        "_rule_trace": [],
        "notes": [],
        "fired_rules": [],
    }

    # 1단계: 전 규칙을 평가해 의도만 수집한다 (순서 독립성 확보)
    intents = _new_intents()
    for rule in merged.get("selection_rules", []):
        fired = evaluate(schema, rule["if"], resolved_ctx)
        if fired:
            resolved["fired_rules"].append(rule["id"])
            if rule.get("then"):
                _collect_effect(intents, rule["then"], rule["id"])
        elif rule.get("else"):
            resolved["fired_rules"].append(rule["id"] + "_ELSE")
            _collect_effect(intents, rule["else"], rule["id"] + "_ELSE")

    # 2단계: 우선순위로 해소한다
    _resolve_intents(resolved, intents, all_artifacts)

    # --- 5단계: 정리 ---------------------------------------------------------
    resolved["artifacts"] = sorted(resolved.pop("_artifact_index").values(), key=lambda a: a["id"])

    activated = resolved.pop("_activated_standards")
    known_std = {s["id"] for s in resolved["standards"]}
    unknown_std = sorted(activated - known_std)
    if unknown_std:
        raise ProfileError(
            "unknown_standard_ref",
            f"규칙이 정의되지 않은 표준을 참조한다: {', '.join(unknown_std)}",
            unknown_std,
        )

    # 조건부 표준 필터링.
    # 어떤 규칙의 add_standards에 등장하는 표준은 그 규칙이 발동해야 적용된다.
    # 규칙 집합 자체가 무엇이 조건부인지 선언하므로 별도 필드가 필요 없다.
    # 필터링하지 않으면 미국에 수출하지 않는 프로젝트의 작업지시서에도
    # 21 CFR Part 11이 남아, 준거 목록이 실제 의무를 반영하지 못한다.
    conditional_std: set[str] = set()
    for rule in merged.get("selection_rules", []):
        for branch in ("then", "else"):
            eff = rule.get(branch) or {}
            conditional_std.update(eff.get("add_standards", []))

    applied, deferred = [], []
    for std in resolved["standards"]:
        sid = std["id"]
        is_activated = sid in activated
        std["rule_activated"] = is_activated
        unconditional = std.pop("_unconditional", False)
        uncond_enf = std.pop("_unconditional_enforcement", None)
        is_conditional = sid in conditional_std and not unconditional

        if is_conditional and not is_activated:
            std["deferred_reason"] = "적용 조건 미충족"
            deferred.append(std)
            continue

        # 조건부 상향분이 발동하지 않았으면 무조건 선언분의 강제성으로 되돌린다
        if (sid in conditional_std and not is_activated and uncond_enf
                and uncond_enf != std["enforcement"]):
            std["enforcement_if_triggered"] = std["enforcement"]
            std["enforcement"] = uncond_enf
        applied.append(std)

    resolved["standards"] = applied
    resolved["deferred_standards"] = deferred

    resolved["required_overlays"] = sorted(resolved.pop("_required_overlays"))
    resolved["rule_trace"] = resolved.pop("_rule_trace")
    resolved["context"] = resolved_ctx
    resolved["_provenance"] = [f"{l.get('kind')}:{l.get('id')}" for l in layers]
    resolved.pop("selection_rules", None)
    resolved.pop("context_schema", None)

    # 오버레이가 규칙으로 요구됐는데 실제로 합성되지 않았으면 경고한다
    missing_ov = [
        ov for ov in resolved["required_overlays"]
        if not any(p.endswith(f":{ov}") or f":{ov}-" in p for p in resolved["_provenance"])
    ]
    if missing_ov:
        resolved["warnings"] = [
            {"code": "overlay_not_applied", "overlays": missing_ov,
             "detail": "규칙이 요구한 오버레이가 합성되지 않았다. auto_overlay를 확인하라."}
        ]

    return resolved
