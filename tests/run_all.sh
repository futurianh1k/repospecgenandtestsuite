#!/usr/bin/env bash
# 전체 검증 파이프라인. CI에서 이 스크립트를 실행한다.
set -uo pipefail
cd "$(dirname "$0")/.."

FAIL=0
run () {
  echo "── $1 ──"
  shift
  "$@" || FAIL=1
  echo
}

run "지침 파일 동기화"              python3 tests/test_docs_sync.py
run "프로파일 팩 검증 (PL-01~PL-13)" python3 services/orchestrator/profile_lint.py packages/profiles
run "린터 규칙 회귀 테스트"          python3 tests/test_lint_rules.py
run "조건 평가기 단위 테스트"        python3 tests/test_conditions.py
run "리졸버 방어 가드"               python3 tests/test_resolver_guards.py
run "골든셋 조건 분기 (전 프로파일)" python3 tests/run_goldenset.py

if [ "$FAIL" -ne 0 ]; then
  echo "검증 실패"
  exit 1
fi
echo "전체 통과"
