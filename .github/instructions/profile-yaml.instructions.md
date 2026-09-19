---
applyTo: "packages/profiles/**/*.yaml"
---

# 프로파일 YAML 작성 지침

`packages/profiles/schema/profile.schema.json`이 스키마다. VS Code의 YAML
확장이 `.vscode/settings.json` 설정으로 이 스키마를 붙여 주므로, 자동완성과
인라인 오류 표시가 편집 중에 나온다.

## 레이어 종류와 권한

| kind | 설정 가능 | 금지 |
|---|---|---|
| `base` | 전부 | — |
| `lifecycle` | `lifecycle`, `id_scheme`, 복원 정책 | 산업·규제 내용 |
| `industry` | 표준, 산출물, 선택 규칙, `context_schema` | `lifecycle` 직접 설정 |
| `overlay` | `*_add` 필드, `activation` | `lifecycle`, `id_scheme` |

`*_add` 접미어가 붙은 필드(`standards_add`, `artifacts_add` 등)는
기존 레이어에 **더하는** 것이고, 접미어 없는 필드는 그 레이어의 선언이다.
오버레이는 `_add` 계열만 쓴다.

## 산출물

```yaml
- id: MD-TST-01          # 프로파일 내 고유. 접두어로 단계를 알 수 있게
  name: 소프트웨어 단위 검증 기록
  phase: 시험
  required: false        # 조건부면 false로 두고 규칙이 켠다
  derivable: B           # A 결정론적 | B 추론+검수 | C 골격만
  conditional: true      # 규칙이 활성화해야 결과에 포함된다
  prompt: prompts/x.md   # derivable C에는 절대 붙이지 않는다 (PL-05)
  standard_refs: ["IEC 62304:2006+AMD1:2015"]
```

`conditional: true`인 산출물은 어떤 규칙의 `add_artifacts`에 반드시 등장해야
한다. 그렇지 않으면 영원히 활성화되지 않는다.

## 표준

```yaml
- id: "식약처 고시 제2025-23호"    # 판본·호수를 id에 포함한다
  name_ko: "디지털의료제품 분류 및 등급 지정 등에 관한 규정"
  issuer: "식품의약품안전처"
  layer: notice                    # law|notice|guideline|international|...
  enforcement: L1                  # L1 법적의무 > L2 인증필수 > L3 관행 > L4 권고
  legal_basis:                     # L1·L2는 필수 (또는 note에 "확인 필요")
    law: "디지털의료제품법"
    ministry: "식품의약품안전처"
    effective: "2025-04-07"
  risk_trigger:                    # 규칙이 추가하는 표준은 필수 (PL-13)
    condition: "ai_applied == true"
  status: current                  # superseded도 지우지 말고 표기만 바꾼다
  confidence: high                 # high|medium|low — 정직하게
  note: "최신 개정 확인 필요"       # 불확실하면 반드시 남긴다
```

폐지·대체 표준도 삭제하지 않는다. 레거시 시스템 문서화에 필요하다.
`status: superseded`로 표기하고 `relations.superseded_by`에 대체 표준을 적는다.

## 선택 규칙

```yaml
- id: SR-AI                        # SR- 접두어
  desc: "AI 적용 시 전용 가이드라인이 발동된다"
  standard_ref: "..."              # 이 규칙의 근거 표준
  if:
    ai_applied: true               # context_schema에 선언된 변수만 (PL-11)
    safety_class: { gte_level: "C" }   # 순서형은 ordered: true 필요
    procurement: { contains: "수출-미국" }
  then:
    add_standards: [...]           # 존재하는 표준만 (PL-12)
    add_artifacts: [...]           # 존재하는 산출물만 (PL-12)
    require_overlays: [X1]
    notes: ["사람이 읽을 주의사항"]
  else:                            # 선택. 없으면 조건 미충족 시 무효과
    add_standards: [...]
```

조건 연산자는 `eq`, `ne`, `in`, `not_in`, `contains`, `gte_level`, `lte_level`뿐이다.
임의 표현식은 지원하지 않는다. 프로파일은 데이터이고 데이터가 코드를
실행해서는 안 된다.

### 효과 우선순위

`add` > `drop` > `relax`. 규칙 작성 순서와 무관하게 적용된다.
`relax`로 산출물을 줄이려는 규칙이 있어도, 다른 규칙이 필수화했으면 남는다.

## 컨텍스트 변수

```yaml
context_schema:
  safety_class:
    type: enum
    values: ["A", "B", "C"]   # 낮은 → 높은 오름차순
    ordered: true             # gte_level/lte_level 쓰려면 필수
    required: true            # 없으면 합성 거부
  ai_applied:
    type: bool
    default: false
```

선택 규칙이 참조하는 변수는 **전부** 여기 선언되어야 한다.
선언 없이 참조하면 PL-11이 막고, 리졸버도 `unknown_rule_variable`로 거부한다.

## 골든셋

프로파일을 고치면 `evals/goldenset.yaml`도 고친다.

```yaml
resolution_cases:
  - id: RC-01
    name: "무엇을 검증하는지"
    context: { ... }
    expect:
      fired_rules: [SR-X]
      artifacts_include: [A-01]     # 필수로 포함
      artifacts_exclude: [A-02]     # 목록에 아예 없음
      artifacts_optional: [A-03]    # 포함되나 required=false
      standards_absent: ["..."]     # 준거 목록에서 제외
      standards_enforcement: { "표준명": "L2" }
      rule_trace_include: [{ rule: SR-X, action: add_artifact, target: A-01 }]
```

새 규칙에는 **발동 케이스와 미발동 케이스를 둘 다** 넣는다.
발동만 시험하면 조건이 항상 참이 되는 버그를 놓친다.

## 흔한 실수

| 증상 | 원인 |
|---|---|
| 규칙이 발동하는데 산출물이 안 나옴 | `conditional: true`인데 `add_artifacts`에 없음 |
| 조건 미충족인데 표준이 남음 | `risk_trigger` 미선언 (PL-13) |
| 강제성이 예상보다 높음 | 다른 레이어가 같은 표준을 더 높게 선언 (strictest-wins) |
| 합성이 `unknown_rule_variable`로 거부 | `context_schema` 선언 누락 |
| `status: stable`인데 린트 오류 | `evals/goldenset.yaml` 없음 (PL-09) |
