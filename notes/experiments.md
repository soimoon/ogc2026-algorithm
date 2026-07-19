실험 기록 남기기.  
아래는 예시.  
## 2026-07-19
- baseline_greedy 그대로 복사, alg_tester로 스모크 테스트 통과 확인
- 관찰: bay_layout에서 블록이 시간축으로만 재사용, 공간 활용도 낮음

## 2026-07-2x
- _candidate_positions를 bounding_rect 대신 실제 폴리곤(check_collisions) 기반으로 교체 시도
- train prob_1~5 기준 objective 변화: ...
