"""골든셋 러너 — 리졸버 조건 분기 회귀 검증.

resolution_cases만 실행한다. extraction_cases는 기준 저장소가 준비되면 활성화한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "orchestrator"))

from conditions import ContextError  # noqa: E402
from resolver import ProfileError, ProfileStore, resolve  # noqa: E402

PROFILES = ROOT / "packages" / "profiles"


class Result:
    def __init__(self, case_id: str, name: str):
        self.case_id = case_id
        self.name = name
        self.failures: list[str] = []

    @property
    def passed(self) -> bool:
        return not self.failures

    def fail(self, msg: str) -> None:
        self.failures.append(msg)


def _check_error(
    case: dict, expect: dict, result: Result, store: ProfileStore, default_industry: str
) -> None:
    try:
        resolve(
            store,
            industry=case.get("industry", default_industry),
            lifecycle=case.get("lifecycle", "v-model"),
            context=case.get("context", {}),
        )
    except (ProfileError, ContextError) as exc:
        if exc.code != expect["error"]:
            result.fail(f"오류 코드 불일치: 기대 {expect['error']}, 실제 {exc.code}")
        for token in expect.get("error_detail_contains", []):
            joined = exc.detail + " " + " ".join(exc.offending)
            if token not in joined:
                result.fail(f"오류 상세에 '{token}'이 없다: {joined}")
        return
    result.fail(f"오류 {expect['error']}가 발생해야 하는데 성공했다")


def run_case(case: dict, store: ProfileStore, default_industry: str) -> Result:
    result = Result(case["id"], case["name"])
    expect = case["expect"]

    if "error" in expect:
        _check_error(case, expect, result, store, default_industry)
        return result

    try:
        r = resolve(
            store,
            industry=case.get("industry", default_industry),
            lifecycle=case.get("lifecycle", "v-model"),
            context=case.get("context", {}),
            project_overrides=case.get("project_overrides"),
        )
    except (ProfileError, ContextError) as exc:
        result.fail(f"예상치 못한 오류: {exc.code} — {exc.detail}")
        return result

    fired = set(r["fired_rules"])
    artifact_ids = {a["id"] for a in r["artifacts"]}
    required_ids = {a["id"] for a in r["artifacts"] if a.get("required")}
    standard_ids = {s["id"] for s in r["standards"]}
    gate_names = {g["name"] for g in r["quality_gates"]}
    trace_keys = {f"{t['from']}->{t['to']}:{t['relation']}" for t in r["traceability_rules"]}

    for rid in expect.get("fired_rules", []):
        if rid not in fired:
            result.fail(f"규칙 {rid}가 발동하지 않았다. 발동: {sorted(fired)}")

    for aid in expect.get("artifacts_include", []):
        if aid not in required_ids:
            state = "미포함" if aid not in artifact_ids else "포함되었으나 required=false"
            result.fail(f"산출물 {aid}가 필수여야 하는데 {state}")

    # 제외는 '필수가 아님'이 아니라 '목록에 아예 없음'을 뜻한다.
    # 활성화되지 않은 조건부 산출물이 선택항목으로 남으면 작업지시서가 흐려진다.
    for aid in expect.get("artifacts_exclude", []):
        if aid in artifact_ids:
            art = next(a for a in r["artifacts"] if a["id"] == aid)
            state = "필수로" if art.get("required") else "선택항목으로"
            result.fail(f"산출물 {aid}가 목록에서 제외되어야 하는데 {state} 포함되었다")

    # 완화된 산출물은 목록에 남되 required=false여야 한다
    for aid in expect.get("artifacts_optional", []):
        if aid not in artifact_ids:
            result.fail(f"산출물 {aid}가 선택항목으로 남아야 하는데 목록에 없다")
        else:
            art = next(a for a in r["artifacts"] if a["id"] == aid)
            if art.get("required"):
                result.fail(f"산출물 {aid}가 선택항목이어야 하는데 필수다")

    for sid in expect.get("standards_include", []):
        if sid not in standard_ids:
            result.fail(f"표준 '{sid}'가 없다")

    for sid in expect.get("standards_exclude", []):
        activated = {s["id"] for s in r["standards"] if s.get("rule_activated")}
        if sid in activated:
            result.fail(f"표준 '{sid}'가 활성화되지 않아야 하는데 규칙으로 활성화되었다")

    # 준거 목록에서 완전히 빠져야 하는 표준
    for sid in expect.get("standards_absent", []):
        if sid in standard_ids:
            result.fail(f"표준 '{sid}'가 준거 목록에서 빠져야 하는데 포함되었다")

    # 조건 미충족으로 보류 목록에 들어가야 하는 표준
    deferred_ids = {s["id"] for s in r.get("deferred_standards", [])}
    for sid in expect.get("standards_deferred", []):
        if sid not in deferred_ids:
            result.fail(f"표준 '{sid}'가 보류 목록에 없다")

    for gname in expect.get("gates_include", []):
        if gname not in gate_names:
            result.fail(f"게이트 {gname}이 없다")

    std_by_id = {s["id"]: s for s in r["standards"]}
    for sid, level in (expect.get("standards_enforcement") or {}).items():
        if sid not in std_by_id:
            result.fail(f"표준 '{sid}'가 없어 강제성을 확인할 수 없다")
        elif std_by_id[sid]["enforcement"] != level:
            result.fail(
                f"표준 '{sid}'의 강제성이 {level}이어야 하는데 "
                f"{std_by_id[sid]['enforcement']}다"
            )

    for gname, threshold in (expect.get("gate_thresholds") or {}).items():
        gate = next((g for g in r["quality_gates"] if g["name"] == gname), None)
        if gate is None:
            result.fail(f"게이트 {gname}이 없어 임계치를 확인할 수 없다")
        elif abs(gate["threshold"] - threshold) > 1e-9:
            result.fail(
                f"게이트 {gname}의 임계치가 {threshold}여야 하는데 {gate['threshold']}다"
            )

    for ov in expect.get("overlays_required", []):
        if ov not in r["required_overlays"]:
            result.fail(f"오버레이 {ov}가 요구되지 않았다. 요구: {r['required_overlays']}")

    if "overlays_required" in expect and not expect["overlays_required"]:
        extra = [o for o in r["required_overlays"] if o not in ("X2", "X5")]
        if extra:
            result.fail(f"규칙으로 추가 요구된 오버레이가 없어야 하는데 있다: {extra}")

    for key in expect.get("trace_relations_include", []):
        if key not in trace_keys:
            result.fail(f"추적 규칙 {key}가 없다")

    for entry in expect.get("rule_trace_include", []):
        hit = any(
            t.get("rule") == entry["rule"]
            and t.get("action") == entry["action"]
            and t.get("target") == entry["target"]
            for t in r["rule_trace"]
        )
        if not hit:
            result.fail(
                f"규칙 추적에 {entry['rule']}/{entry['action']}/{entry['target']}가 없다"
            )

    for entry in expect.get("rule_trace_absent", []):
        hit = any(
            t.get("rule") == entry["rule"]
            and t.get("action") == entry["action"]
            and t.get("target") == entry["target"]
            for t in r["rule_trace"]
        )
        if hit:
            result.fail(
                f"규칙 추적에 {entry['rule']}/{entry['action']}/{entry['target']}가 "
                f"없어야 하는데 있다"
            )

    for token in expect.get("notes_contain", []):
        if not any(token in n["note"] for n in r["notes"]):
            result.fail(f"규칙 주의사항에 '{token}'이 없다")

    if r.get("warnings"):
        for w in r["warnings"]:
            result.fail(f"리졸버 경고: {w}")

    return result


def main() -> int:
    """인자로 프로파일 id를 주면 그것만, 없으면 골든셋이 있는 전 프로파일을 돈다."""
    target = sys.argv[1] if len(sys.argv) > 1 else None
    gs_paths = sorted((PROFILES / "industry").glob("*/evals/goldenset.yaml"))
    if target:
        gs_paths = [p for p in gs_paths if p.parents[1].name == target]
        if not gs_paths:
            print(f"골든셋을 찾을 수 없다: {target}")
            return 1

    store = ProfileStore(PROFILES)
    total_pass = total = total_pending = 0

    for gs_path in gs_paths:
        profile_id = gs_path.parents[1].name
        with gs_path.open(encoding="utf-8") as f:
            gs = yaml.safe_load(f)

        cases = gs.get("resolution_cases", [])
        results = [run_case(c, store, profile_id) for c in cases]

        print(f"── {profile_id} ── {gs.get('name', '')}")
        for res in results:
            mark = "PASS" if res.passed else "FAIL"
            print(f"[{mark}] {res.case_id}  {res.name}")
            for failure in res.failures:
                print(f"        - {failure}")

        passed = sum(1 for r in results if r.passed)
        pending = len([c for c in gs.get("extraction_cases", [])
                       if c.get("status") == "pending_fixture"])
        print(f"   → {passed}/{len(results)} 통과, 추출 케이스 {pending}건 보류\n")
        total_pass += passed
        total += len(results)
        total_pending += pending

    print(f"조건 분기 합계: {total_pass}/{total} 통과, "
          f"추출 케이스 {total_pending}건 보류(기준 저장소 대기)")
    return 0 if total_pass == total else 1


if __name__ == "__main__":
    sys.exit(main())
