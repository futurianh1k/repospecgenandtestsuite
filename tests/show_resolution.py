"""시나리오별 해소 결과 비교 출력.

골든셋은 통과/실패만 알려준다. 실제로 무엇이 달라지는지 눈으로 봐야
규칙 설계가 의도대로인지 판단할 수 있다.

사용: python3 tests/show_resolution.py [D01-public-kr | D04-medical]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "orchestrator"))

from resolver import ProfileStore, resolve  # noqa: E402

PROFILES = ROOT / "packages" / "profiles"

SETS = {
    "D04-medical": {
        "base": {
            "safety_class": "A", "form": "standalone", "ai_applied": False,
            "generative_ai": False, "network_connected": False, "uses_soup": False,
            "is_dtx": False, "xr_applied": False, "procurement": ["민간B2B"],
        },
        "scenarios": [
            ("① Class A 독립형 최소", {}),
            ("② Class C 내장형", {"safety_class": "C", "form": "embedded"}),
            ("③ Class B AI+망", {"safety_class": "B", "ai_applied": True,
                                 "network_connected": True}),
            ("④ 생성형AI DTx 수출", {"safety_class": "C", "ai_applied": True,
                                     "generative_ai": True, "network_connected": True,
                                     "uses_soup": True, "is_dtx": True,
                                     "procurement": ["민간B2B", "수출-미국", "수출-EU"]}),
        ],
    },
    "D01-public-kr": {
        "base": {
            "project_scale": "소규모", "project_type": "구축",
            "audit_required": False, "pmo_delegated": False,
            "security_diagnosis": False, "personal_data": False,
            "pia_required": False, "db_included": True, "public_data": False,
            "egov_framework": False, "fp_estimation": False,
            "cloud_deployment": False, "data_migration": False,
        },
        "scenarios": [
            ("① 소규모 감리없음", {}),
            ("② 중규모 표준FW", {"project_scale": "중규모", "egov_framework": True}),
            ("③ 대규모 감리+보안", {"project_scale": "대규모", "audit_required": True,
                                    "security_diagnosis": True, "personal_data": True,
                                    "fp_estimation": True}),
            ("④ 대규모 전부", {"project_scale": "대규모", "audit_required": True,
                               "security_diagnosis": True, "personal_data": True,
                               "pia_required": True, "public_data": True,
                               "egov_framework": True, "fp_estimation": True,
                               "cloud_deployment": True, "data_migration": True}),
            ("⑤ 유지관리", {"project_scale": "중규모", "project_type": "유지관리",
                            "db_included": False}),
        ],
    },
}

ROWS = [
    ("필수 산출물", lambda r: sum(1 for a in r["artifacts"] if a.get("required"))),
    ("선택 산출물", lambda r: sum(1 for a in r["artifacts"] if not a.get("required"))),
    ("적용 표준", lambda r: len(r["standards"])),
    ("  L1 법적의무", lambda r: sum(1 for s in r["standards"] if s["enforcement"] == "L1")),
    ("  L2 인증필수", lambda r: sum(1 for s in r["standards"] if s["enforcement"] == "L2")),
    ("보류 표준", lambda r: len(r.get("deferred_standards", []))),
    ("품질 게이트", lambda r: len(r["quality_gates"])),
    ("추적 규칙", lambda r: len(r["traceability_rules"])),
    ("린터 규칙", lambda r: len(r["lint_rules"])),
    ("발동 규칙", lambda r: len(r["fired_rules"])),
    ("요구 오버레이", lambda r: len(r["required_overlays"])),
]


def show(profile_id: str, spec: dict, store: ProfileStore) -> None:
    results = []
    for label, delta in spec["scenarios"]:
        r = resolve(store, profile_id, "v-model", {**spec["base"], **delta})
        results.append((label, r))

    width = 14 + 15 * len(results)
    print(f"\n{'=' * width}")
    print(f"{profile_id} — 시나리오별 해소 결과")
    print("=" * width)
    print(f"{'항목':<14}" + "".join(f"{lbl[:14]:>15}" for lbl, _ in results))
    print("-" * width)
    for name, fn in ROWS:
        print(f"{name:<14}" + "".join(f"{fn(r):>15}" for _, r in results))

    print()
    for label, r in results:
        req = sorted(a["id"] for a in r["artifacts"] if a.get("required"))
        opt = sorted(a["id"] for a in r["artifacts"] if not a.get("required"))
        print(f"[{label}]")
        print(f"  발동 규칙 : {', '.join(r['fired_rules']) or '없음'}")
        print(f"  오버레이  : {', '.join(r['required_overlays']) or '없음'}")
        print(f"  필수 산출물({len(req)}): {', '.join(req)}")
        if opt:
            print(f"  선택 산출물({len(opt)}): {', '.join(opt)}")
        shifted = [s for s in r["standards"] if s.get("enforcement_if_triggered")]
        if shifted:
            for s in shifted:
                print(f"  강제성 유동 : {s['id']} — 현재 {s['enforcement']}, "
                      f"조건 발동 시 {s['enforcement_if_triggered']}")
        if r["notes"]:
            for n in r["notes"]:
                print(f"  [{n['rule']}] {n['note']}")
        print()


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else None
    store = ProfileStore(PROFILES)
    for pid, spec in SETS.items():
        if target and pid != target:
            continue
        show(pid, spec, store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
