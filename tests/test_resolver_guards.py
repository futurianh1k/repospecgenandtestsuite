"""리졸버 방어 가드 테스트.

잘못 작성된 레이어가 합성 단계에서 거부되는지 검증한다.
profile-lint가 CI에서 1차로 걸러내지만, 조직 포크나 런타임 주입 경로로
스키마 검증을 거치지 않은 레이어가 들어올 수 있으므로 리졸버도 방어해야 한다.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "orchestrator"))

from conditions import ContextError  # noqa: E402
from resolver import ProfileError, ProfileStore, resolve  # noqa: E402

PROFILES = ROOT / "packages" / "profiles"

BASE_CTX = {
    "safety_class": "B",
    "form": "standalone",
    "ai_applied": False,
    "network_connected": False,
    "uses_soup": False,
    "procurement": ["민간B2B"],
}

CASES: list[tuple[str, dict, str]] = [
    (
        "오버레이가 id_scheme을 바꾸려 하면 거부",
        {
            "schema_version": "1.0",
            "id": "XT-bad-idscheme",
            "kind": "overlay",
            "name": "불량 오버레이",
            "id_scheme": {"requirement": "HACK-{n:03d}"},
        },
        "id_scheme_mutation_forbidden",
    ),
    (
        "오버레이가 lifecycle을 바꾸려 하면 거부",
        {
            "schema_version": "1.0",
            "id": "XT-bad-lifecycle",
            "kind": "overlay",
            "name": "불량 오버레이",
            "lifecycle": "agile",
        },
        "lifecycle_mutation_forbidden",
    ),
    (
        "산업 레이어가 lifecycle을 직접 설정하려 하면 거부",
        {
            "schema_version": "1.0",
            "id": "XT-bad-industry",
            "kind": "industry",
            "name": "불량 산업 레이어",
            "lifecycle": "hybrid",
        },
        "lifecycle_mutation_forbidden",
    ),
    (
        "규칙이 없는 산출물을 참조하면 거부",
        {
            "schema_version": "1.0",
            "id": "XT-ghost-artifact",
            "kind": "overlay",
            "name": "유령 산출물 참조",
            "context_schema": {"ghost_trigger": {"type": "bool", "default": True}},
            "selection_rules": [
                {
                    "id": "SR-GHOST",
                    "if": {"ghost_trigger": True},
                    "then": {"add_artifacts": ["NO-SUCH-ARTIFACT"]},
                }
            ],
        },
        "unknown_artifact_ref",
    ),
    (
        "규칙이 없는 표준을 참조하면 거부",
        {
            "schema_version": "1.0",
            "id": "XT-ghost-standard",
            "kind": "overlay",
            "name": "유령 표준 참조",
            "context_schema": {"ghost_trigger": {"type": "bool", "default": True}},
            "selection_rules": [
                {
                    "id": "SR-GHOST-STD",
                    "if": {"ghost_trigger": True},
                    "then": {"add_standards": ["존재하지 않는 표준"]},
                }
            ],
        },
        "unknown_standard_ref",
    ),
    (
        "규칙이 정의되지 않은 컨텍스트 변수를 참조하면 거부",
        {
            "schema_version": "1.0",
            "id": "XT-ghost-var",
            "kind": "overlay",
            "name": "유령 변수 참조",
            "selection_rules": [
                {
                    "id": "SR-GHOST-VAR",
                    "if": {"never_declared_var": True},
                    "then": {"notes": ["도달 불가"]},
                }
            ],
        },
        "unknown_rule_variable",
    ),
]


def run_case(name: str, layer: dict, expected_code: str) -> list[str]:
    """임시 프로파일 트리에 불량 레이어를 심고 합성을 시도한다."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "profiles"
        shutil.copytree(PROFILES, root)
        ov_dir = root / "overlay" / layer["id"]
        if layer["kind"] == "industry":
            ov_dir = root / "industry" / layer["id"]
        ov_dir.mkdir(parents=True, exist_ok=True)
        with (ov_dir / "profile.yaml").open("w", encoding="utf-8") as f:
            yaml.safe_dump(layer, f, allow_unicode=True)

        store = ProfileStore(root)
        kwargs = dict(industry="D04-medical", lifecycle="v-model", context=dict(BASE_CTX))
        if layer["kind"] == "industry":
            kwargs["industry"] = layer["id"]
            kwargs["context"] = {}
        else:
            kwargs["overlays"] = [layer["id"]]

        try:
            resolve(store, **kwargs)
        except (ProfileError, ContextError) as exc:
            if exc.code != expected_code:
                failures.append(f"오류 코드 불일치: 기대 {expected_code}, 실제 {exc.code}")
            return failures
        failures.append(f"{expected_code}로 거부되어야 하는데 합성에 성공했다")
    return failures


def test_unknown_context_key_rejected() -> list[str]:
    """스키마에 없는 컨텍스트 키를 사용자가 넘기면 거부한다.

    조용히 무시하면 오타가 통과해 규칙이 발동하지 않고, 사용자는 산출물이
    빠진 사실을 심사 단계에서야 알게 된다.
    """
    failures: list[str] = []
    store = ProfileStore(PROFILES)
    bad = {**BASE_CTX, "saftey_class": "C"}  # 오타
    try:
        resolve(store, "D04-medical", "v-model", bad)
    except (ProfileError, ContextError) as exc:
        if exc.code != "unknown_context_value":
            failures.append(f"오류 코드 불일치: 기대 unknown_context_value, 실제 {exc.code}")
        elif "saftey_class" not in exc.offending:
            failures.append(f"오타 키가 오류에 지목되지 않았다: {exc.offending}")
        return failures
    failures.append("오타 컨텍스트 키가 거부되지 않았다")
    return failures


def test_overlay_order_independence_with_conflict() -> list[str]:
    """단일값 필드가 충돌하는 오버레이에서도 입력 순서가 결과를 바꾸지 않는다.

    현재 X1·X2·X5는 단일값 필드를 건드리지 않아 충돌이 없다. 충돌이 실제로
    생기는 상황을 만들어야 정렬이 의미를 갖는지 확인할 수 있다.
    """
    failures: list[str] = []
    overlays = [
        {
            "schema_version": "1.0", "id": "XT-alpha", "kind": "overlay",
            "name": "알파", "output_default": "pdf",
            "quality_gates_add": [
                {"name": "shared_gate", "metric": "m", "threshold": 0.60, "op": "gte"}
            ],
        },
        {
            "schema_version": "1.0", "id": "XT-omega", "kind": "overlay",
            "name": "오메가", "output_default": "xlsx",
            "quality_gates_add": [
                {"name": "shared_gate", "metric": "m", "threshold": 0.95, "op": "gte"}
            ],
        },
    ]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "profiles"
        shutil.copytree(PROFILES, root)
        for layer in overlays:
            d = root / "overlay" / layer["id"]
            d.mkdir(parents=True, exist_ok=True)
            with (d / "profile.yaml").open("w", encoding="utf-8") as f:
                yaml.safe_dump(layer, f, allow_unicode=True)

        store = ProfileStore(root)
        a = resolve(store, "D04-medical", "v-model", dict(BASE_CTX),
                    overlays=["XT-alpha", "XT-omega"])
        b = resolve(store, "D04-medical", "v-model", dict(BASE_CTX),
                    overlays=["XT-omega", "XT-alpha"])

        if a["output_default"] != b["output_default"]:
            failures.append(
                f"단일값 필드가 입력 순서에 좌우된다: "
                f"{a['output_default']} vs {b['output_default']}"
            )
        ga = next(g for g in a["quality_gates"] if g["name"] == "shared_gate")
        gb = next(g for g in b["quality_gates"] if g["name"] == "shared_gate")
        if ga["threshold"] != gb["threshold"]:
            failures.append(f"게이트 임계치가 순서에 좌우된다: {ga} vs {gb}")
        if ga["threshold"] != 0.95:
            failures.append(f"게이트가 엄격한 쪽(0.95)이 아니다: {ga['threshold']}")
    return failures


def test_provenance_recorded() -> list[str]:
    """합성 이력이 남는지 — 감리 대응의 전제 조건."""
    failures: list[str] = []
    store = ProfileStore(PROFILES)
    r = resolve(store, "D04-medical", "v-model", dict(BASE_CTX))
    prov = r.get("_provenance", [])
    for expected in ("base:_base", "lifecycle:v-model", "industry:D04-medical"):
        if expected not in prov:
            failures.append(f"_provenance에 {expected}가 없다: {prov}")
    if not r.get("rule_trace"):
        failures.append("rule_trace가 비어 있다 — 규칙 적용 이력을 역추적할 수 없다")
    return failures


def test_determinism() -> list[str]:
    """오버레이 입력 순서가 달라도 결과가 같아야 한다."""
    failures: list[str] = []
    store = ProfileStore(PROFILES)
    ctx = {**BASE_CTX, "ai_applied": True, "network_connected": True, "uses_soup": True}

    a = resolve(store, "D04-medical", "v-model", dict(ctx), overlays=["X1", "X2", "X5"])
    b = resolve(store, "D04-medical", "v-model", dict(ctx), overlays=["X5", "X1", "X2"])

    for key, label in [
        ("artifacts", "산출물"),
        ("standards", "표준"),
        ("quality_gates", "게이트"),
        ("lint_rules", "린터 규칙"),
    ]:
        ka = sorted(x.get("id") or x.get("name") for x in a[key])
        kb = sorted(x.get("id") or x.get("name") for x in b[key])
        if ka != kb:
            failures.append(f"{label} 집합이 오버레이 순서에 따라 달라진다")

    if sorted(a["fired_rules"]) != sorted(b["fired_rules"]):
        failures.append("발동 규칙이 오버레이 순서에 따라 달라진다")
    return failures


def main() -> int:
    total = failed = 0

    for name, layer, code in CASES:
        total += 1
        fails = run_case(name, layer, code)
        if fails:
            failed += 1
            print(f"[FAIL] {name}")
            for f in fails:
                print(f"        - {f}")
        else:
            print(f"[PASS] {name}")

    for fn, name in [
        (test_unknown_context_key_rejected, "스키마에 없는 컨텍스트 키는 거부된다"),
        (test_provenance_recorded, "합성 이력(_provenance, rule_trace)이 기록된다"),
        (test_determinism, "오버레이 입력 순서와 무관하게 결과가 동일하다"),
        (test_overlay_order_independence_with_conflict,
         "단일값 필드가 충돌해도 오버레이 순서가 결과를 바꾸지 않는다"),
    ]:
        total += 1
        fails = fn()
        if fails:
            failed += 1
            print(f"[FAIL] {name}")
            for f in fails:
                print(f"        - {f}")
        else:
            print(f"[PASS] {name}")

    print(f"\n리졸버 가드: {total - failed}/{total} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
