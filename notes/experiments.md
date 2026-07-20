실험 기록 남기기.  
아래는 예시.  
## 2026-07-19
- baseline_greedy 그대로 복사, alg_tester로 스모크 테스트 통과 확인
- 관찰: bay_layout에서 블록이 시간축으로만 재사용, 공간 활용도 낮음

## 2026-07-2x
- _candidate_positions를 bounding_rect 대신 실제 폴리곤(check_collisions) 기반으로 교체 시도
- train prob_1~5 기준 objective 변화: ...

## 2026-07-19
- baseline_greedy로 prob_1(100 blocks, 2 bays) 실행, alg_tester Algorithm Output 로그 확인
  - Phase 1: 전 구간 `fallback=0` (100/100 블록 모두 탐색으로 자리를 찾음, 완전 배치 실패 없음)
  - Phase 2 repair: 대부분 `[time]`(시간만 조정), `[forced]` 1건(block65, 2회 연속 위반으로 사이클 방지용 강제배치) -- `best_score==inf`로 인한 진짜 fallback은 없었음
  - 결론: 이 인스턴스에서는 "공간이 없어서 못 들어가는" 수준의 실패는 발생하지 않음
- `analysis/shape_irregularity.py` 작성: 각 (block, orientation, layer)의 실제 다각형 면적 / bounding_rect 면적 비율 계산
  - `python analysis/shape_irregularity.py <instance.json>` 로 실행
  - prob_1 결과: 평균 비율 0.577, 중앙값 0.526, 최솟값 0.175 (block 25)
  - 분포: 43.5%가 비율 0.5 미만 (바운딩박스의 절반도 안 채움), 0.9 이상은 7.1%뿐
- 해석: `_candidate_positions`가 실패(fallback)로 이어지진 않지만, 블록 형상 자체는 평균적으로 바운딩박스의 40% 이상을 낭비하고 있음
  -> "실행 가능성" 문제는 아니지만 "패킹 품질/목적함수" 개선 여지는 충분해 보임 -> NFP(no-fit polygon) 스타일 후보 생성 시도해볼 가치 있음
- 다음: 위 `_candidate_positions` 교체 실험(바로 위 항목)을 이 근거를 바탕으로 진행
