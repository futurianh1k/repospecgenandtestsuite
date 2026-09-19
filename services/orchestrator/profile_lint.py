"""프로파일 팩 검증기 (PL-01 ~ PL-10).

CI에서 강제한다. PL-05와 PL-09가 제품 신뢰성의 마지노선이다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

try:
    from jsonschema import Draft202012Validator
    HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    HAS_JSONSCHEMA = False


TRACE_NODES = {
    "stakeholder_req", "system_req", "software_req", "requirement",
    "story", "acceptance_criterion", "component", "architecture",
    "detail_design", "testcase", "unit_test", "integration_test",
    "system_test", "acceptance_test", "code_symbol", "commit",
    "dataset", "model_artifact", "risk_item", "soup_item",
    "threat", "security_control", "usability_spec",
    # 공공 사업 — 과업대비표와 대가산정 축
    "rfp_requirement", "deliverable", "change_request",
    "function_point", "weakness_item",
}


class Finding:
    def __init__(self, rule: str, layer: str, message: str, severity: str = "error"):
        self.rule = rule
        self.layer = layer
        self.message = message
        self.severity = severity

    def __str__(self) -> str:
        mark = "ERROR" if self.severity == "error" else "WARN "
        return f"[{mark}] {self.rule} ({self.layer}): {self.message}"


def _iter_layers(root: Path):
    for path in sorted(root.rglob("profile.yaml")):
        with path.open(encoding="utf-8") as f:
            yield path, yaml.safe_load(f)


def lint(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    schema_path = root / "schema" / "profile.schema.json"

    validator = None
    if HAS_JSONSCHEMA and schema_path.exists():
        with schema_path.open(encoding="utf-8") as f:
            validator = Draft202012Validator(json.load(f))

    layers: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path, data in _iter_layers(root):
        lid = data.get("id", path.parent.name)
        layers[lid] = (path, data)

    for lid, (path, data) in layers.items():
        rel = str(path.relative_to(root))
        kind = data.get("kind")

        # --- PL-01: 스키마 통과 ---
        if validator:
            for err in validator.iter_errors(data):
                loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
                findings.append(Finding("PL-01", rel, f"{loc}: {err.message}"))

        # --- PL-02: extends 순환 ---
        seen, cur = {lid}, data.get("extends")
        while cur:
            if cur in seen:
                findings.append(Finding("PL-02", rel, f"extends 체인에 순환이 있다: {cur}"))
                break
            seen.add(cur)
            nxt = layers.get(cur)
            cur = nxt[1].get("extends") if nxt else None

        all_arts = data.get("artifacts", []) + data.get("artifacts_add", [])
        art_ids = {a["id"] for a in all_arts}

        for art in all_arts:
            # --- PL-03: ref 대상 존재 ---
            if art.get("ref"):
                ref_file = path.parent / "artifacts" / f"{art['ref']}.yaml"
                if not ref_file.exists():
                    findings.append(
                        Finding("PL-03", rel,
                                f"산출물 {art['id']}의 ref '{art['ref']}'에 해당하는 정의 파일이 없다",
                                "warning")
                    )
            # --- PL-04: 템플릿·프롬프트 파일 존재 ---
            for key in ("template", "prompt"):
                if art.get(key) and not (path.parent / art[key]).exists():
                    findings.append(
                        Finding("PL-04", rel,
                                f"산출물 {art['id']}의 {key} 파일이 없다: {art[key]}", "warning")
                    )
            # --- PL-05: C등급에 생성 프롬프트 금지 (마지노선) ---
            if art.get("derivable") == "C" and art.get("prompt"):
                findings.append(
                    Finding("PL-05", rel,
                            f"산출물 {art['id']}는 derivable C인데 생성 프롬프트가 연결되어 있다. "
                            f"AI가 검증 결과서를 지어내게 된다.")
                )

        # --- PL-06: 강제성 근거 ---
        # 국내 규범(law/notice/directive/manual)은 법적 근거가 있어야 한다.
        # 국제표준·외국법규의 L2는 법적 근거가 아니라 심사 주체로 강제성이 성립한다.
        domestic_layers = {"law", "notice", "directive", "manual", "manual_annex", "guideline"}
        for std in data.get("standards", []) + data.get("standards_add", []):
            enforcement = std.get("enforcement")
            layer_type = std.get("layer")
            note = std.get("note") or ""
            if enforcement in ("L1", "L2"):
                if layer_type in domestic_layers:
                    if not std.get("legal_basis") and "확인 필요" not in note:
                        findings.append(
                            Finding("PL-06", rel,
                                    f"표준 {std['id']}는 국내 {enforcement} 규범인데 legal_basis가 없다. "
                                    f"근거를 기재하거나 note에 '확인 필요'를 남겨라.")
                        )
                elif not std.get("audit_body") and not std.get("legal_basis"):
                    findings.append(
                        Finding("PL-06", rel,
                                f"표준 {std['id']}는 {enforcement}인데 legal_basis도 audit_body도 없다. "
                                f"강제성의 근거를 밝혀라.", "warning")
                    )
            # --- PL-10: 판본 표기 ---
            # 국내 법령은 수시 개정되므로 연도 대신 legal_basis.effective로 갈음한다.
            has_edition = std.get("edition_year") or (std.get("legal_basis") or {}).get("effective")
            if not has_edition and "확인 필요" not in note:
                findings.append(
                    Finding("PL-10", rel,
                            f"표준 {std['id']}에 판본·개정연도 또는 시행일이 없다", "warning")
                )

        # --- PL-07: 오버레이의 금지 필드 ---
        if kind == "overlay":
            for forbidden in ("lifecycle", "id_scheme"):
                if forbidden in data:
                    findings.append(
                        Finding("PL-07", rel, f"오버레이가 {forbidden}를 설정할 수 없다")
                    )

        # --- PL-08: 추적 규칙 노드 타입 ---
        for tr in data.get("traceability_rules", []) + data.get("traceability_rules_add", []):
            for side in ("from", "to"):
                if tr.get(side) not in TRACE_NODES:
                    findings.append(
                        Finding("PL-08", rel,
                                f"추적 규칙의 {side} 노드 타입이 정의에 없다: {tr.get(side)}")
                    )

        # --- PL-09: 골든셋 없으면 stable 금지 ---
        if kind == "industry":
            gs = path.parent / "evals" / "goldenset.yaml"
            if data.get("status") == "stable" and not gs.exists():
                findings.append(
                    Finding("PL-09", rel,
                            "골든셋(evals/goldenset.yaml)이 없는데 status가 stable이다")
                )

        # --- 선택 규칙 정합성 ---
        ctx_vars = set(data.get("context_schema", {}))
        # 상속 체인의 컨텍스트 변수까지 모은다.
        # PL-02가 순환을 보고해도 이 순회는 계속되므로 자체 가드가 필요하다.
        cur, walked = data.get("extends"), {lid}
        while cur and cur in layers and cur not in walked:
            walked.add(cur)
            ctx_vars |= set(layers[cur][1].get("context_schema", {}))
            cur = layers[cur][1].get("extends")
        if kind == "industry":
            for base_id in ("_base",):
                if base_id in layers:
                    ctx_vars |= set(layers[base_id][1].get("context_schema", {}))

        all_stds = data.get("standards", []) + data.get("standards_add", [])
        std_ids = {s["id"] for s in all_stds}

        # --- PL-13: 규칙이 추가하는 표준은 발동 조건을 선언해야 한다 ---
        # 선언하지 않으면 리졸버가 '무조건 적용'으로 보아, 조건이 맞지 않는
        # 프로젝트의 준거 목록에도 남는다.
        rule_added: set[str] = set()
        for rule in data.get("selection_rules", []):
            for branch in ("then", "else"):
                rule_added.update((rule.get(branch) or {}).get("add_standards", []))
        std_by_id = {s["id"]: s for s in all_stds}
        for sid in sorted(rule_added):
            std = std_by_id.get(sid)
            if std is not None and not std.get("risk_trigger"):
                findings.append(
                    Finding("PL-13", rel,
                            f"표준 {sid}는 규칙이 추가하는데 risk_trigger가 없다. "
                            f"조건을 선언하지 않으면 조건 미충족 프로젝트에도 적용된다.")
                )

        for rule in data.get("selection_rules", []):
            for var in rule.get("if", {}):
                if var not in ctx_vars:
                    findings.append(
                        Finding("PL-11", rel,
                                f"규칙 {rule['id']}가 정의되지 않은 컨텍스트 변수를 참조한다: {var}")
                    )
            for branch in ("then", "else"):
                eff = rule.get(branch) or {}
                for aid in eff.get("add_artifacts", []) + eff.get("relax_artifacts", []):
                    if aid not in art_ids:
                        findings.append(
                            Finding("PL-12", rel,
                                    f"규칙 {rule['id']}.{branch}가 없는 산출물을 참조한다: {aid}")
                        )
                for sid in eff.get("add_standards", []):
                    if sid not in std_ids:
                        findings.append(
                            Finding("PL-12", rel,
                                    f"규칙 {rule['id']}.{branch}가 없는 표준을 참조한다: {sid}")
                        )

    return findings


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "packages/profiles")
    findings = lint(root)
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    for f in findings:
        print(f)

    print(f"\nprofile-lint: 오류 {len(errors)}건, 경고 {len(warnings)}건")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
