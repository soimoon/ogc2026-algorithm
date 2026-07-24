# 알고리즘 오버뷰 v9 -- 9차 제출 준비 시작 시점 스냅샷 (2026-07-24 기준)

`algorithm_overview.md`가 "지금 코드가 왜 이렇게 생겼는지"(시간순 변경 이력 + 실패한 시도까지 전부)를 기록한
문서라면, 이 파일은 그 역사를 걷어내고 **"지금 이 순간 코드가 실제로 무엇을 하는가"**만 한 번에 훑을 수 있게
정리한 것입니다. 기술보고서의 "알고리즘 설계" 섹션 초안으로 바로 활용 가능하도록 작성했습니다.

**버전 관리 방식 변경 (2026-07-24)**: 이전에는 `algorithm_overview2.md` 하나를 계속 덮어써서 "현재 상태"만
유지했습니다. 오늘부터 제출 주기가 빨라져서(8차까지 제출 완료, 9차 준비 시작) 제출 사이 코드 변화가 커질 수
있으므로, **매 제출 준비 사이클마다 새 버전 파일(`algorithm_overview_v{N}.md`)을 만들어 이전 스냅샷을
그대로 보존**하는 방식으로 전환합니다. 이 파일이 그 첫 버전(v9, 9차 제출 준비용)입니다. 이전 스냅샷은
`algorithm_overview2.md`(8차 제출 시점, commit 4b3c706 기준)로 남겨두고 더 이상 갱신하지 않습니다. 이력
전체를 담는 `algorithm_overview.md`의 날짜별 append 방식은 그대로 유지됩니다 -- 이 규칙은 바뀌지 않습니다.

이 문서는 커밋 c48fd0e + 2026-07-24 세션 전체(안전 버그 수정 2건, `xpress_reinsert.py` 그리드 필터 + 전역
기하 캐시, `cpsat_reinsert.py` interval-var 재작성 -- 전부 §9.1) 반영 시점 기준입니다. 이후 바뀐 게 있으면
**다음 제출 준비 사이클에서 새 버전 파일을 만들 것** (같은 파일을 계속 고치지 말 것 -- 하단 "이 문서 갱신
규칙" 참고).

---

## 0. 파일별 역할

| 파일 | 역할 |
|---|---|
| `myalgorithm.py` | 대회 채점 서버가 호출하는 진입점. 재시작 루프 + 크래시/타임아웃/infeasible에 대한 최후 안전망. |
| `baseline_greedy.py` | 실제 알고리즘 본체 (Phase 1~3, 전부). |
| `xpress_reinsert.py` | Phase 2/3에서 여러 블록을 동시에 재배치할 때 쓰는 소규모 Xpress MIP. |
| `cpsat_reinsert.py` | **프로토타입, 아직 파이프라인에 미배선.** `xpress_reinsert.py`의 CP-SAT 대체 실험판 -- §7.5 참고. |
| `utils.py` | **채점 서버가 매번 원본으로 덮어씀** -- 절대 수정 금지, 여기 있는 `check_feasibility`가 유일한 정답 판정 기준. |

---

## 1. 전체 파이프라인 한눈에

```
myalgorithm.algorithm(prob_info, timelimit)
  └─ _solve()
       └─ _iterated_greedy()  -- 남은 시간이 있는 한 최대 5회 재시작
            └─ baseline_greedy.greedyalgorithm(prob_info, timelimit=남은시간, seed=0,1,2,...,
                                                priority_rule=시도별 순환값)
                 ├─ Phase 1   : 초기 배치 (construction)
                 ├─ Phase 2   : repair (infeasible 블록 재배치)
                 ├─ Phase 2.5 : left-justify
                 ├─ Phase 2.6 : right-justify + re-left-justify
                 └─ Phase 3   : improve (ALNS 기반 지연/불균형/선호도 개선)
            └─ check_feasibility로 검증, feasible + 지금까지 최선이면 채택
       └─ 전부 실패하거나 infeasible이면 -> _emergency_fallback() (구조적으로 항상 feasible)
  └─ 무슨 일이 있어도 algorithm() 자체는 예외를 밖으로 던지지 않음 (try/except 최외곽 한 겹 더)
```

**코드 수준의 안전망**: 문제 명세 3.3절상 infeasible/TLE/크래시는 전부 동일하게 -1점이므로, 모든 개선 단계
(Phase 2/2.5/2.6/3)는 "시도 → 검증 → 개선 안 되면 롤백" 패턴을 예외 없이 따름 -- 어떤 단계가 중간에
시간 초과로 끊겨도 그 시점까지의 best는 항상 feasible. 이 안전망 자체는 계속 유지.

**개발 전략(2026-07-22부터)**: 실험은 브랜치에서 과감하게(구조적으로 다른 접근, 리스크 있는 시도 포함) 하고,
실제로 검증된 것만 `main`에 올려 제출하는 방식. "시도→검증→롤백" 안전망과
[[feedback_empirical_verification]]의 "바꾸기 전엔 항상 A/B 테스트" 원칙은 그대로 유지.

---

## 2. `myalgorithm.py` -- 진입점과 재시작

### `_iterated_greedy` (반복 그리디 재시작)
- 남은 예산(`deadline - now`)이 `_RESTART_MIN_BUDGET=10.0`초 미만이면 중단, 최대 `_RESTART_MAX_ATTEMPTS=5`회.
- 매 시도마다 `seed=attempt-1` (0, 1, 2, ...) **및 `priority_rule`을 `_PRIORITY_RULE_CYCLE`에서 순환
  선택**해서 `greedyalgorithm`을 다시 호출.
- **2026-07-22 변경 (v2 스냅샷 이후 업데이트, v2에는 반영 안 돼 있었음)**: `_PRIORITY_RULE_CYCLE =
  ["edd", "edd", "edd", "area_slack", "area"]` -- 시도 1~3은 무조건 EDD(기존과 동일 동작 보장), 시도 4~5에서만
  `area_slack`/`area`(§3.1 참고, 2026-07-22 신설된 space-packing 기반 규칙)로 다양화. EDD가 로컬 40개 기준
  29-33/40으로 여전히 우세하지만, 나머지 인스턴스에서 Phase 3가 구조적으로 다른 시작점을 탐색해볼 수 있게
  하려는 의도. 재시작 예산이 3회 이하인(보통 크고 느린) 인스턴스는 이 변경 이전과 동일하게 동작.
- **이전 시도보다 objective가 개선될 때만 채택** (`if objective < best_objective`) -- 재시작이 아무리 나빠도
  최종 결과가 이전 최선보다 나빠지는 일은 수학적으로 없음.
- `timelimit * 0.9`만 내부 예산으로 쓰고 나머지는 `check_feasibility` 검증 + 비상 폴백 여유로 남겨둠 --
  정확히는 `max(timelimit*0.1, 인스턴스크기 기반 추정치)`로 예약(2026-07-22, 큰 인스턴스에서 `check_feasibility`
  자체 비용이 커질 수 있음을 대비한 동적 예약).

### `_emergency_fallback` (최후의 보루)
- `baseline_greedy`를 아예 import하지 않는 완전히 독립적인 코드. 블록을 due_date 순으로, bay마다 **한 번에
  하나씩만 순차 배치**(겹치는 시간대가 아예 없도록) -- 구조적으로 항상 feasible. 품질은 매우 나쁘지만
  "메인 솔버가 통째로 죽어도 -1은 피한다"는 목적 하나만 있음.

---

## 3. Phase 1 -- 초기 배치 (construction)

### 3.1 우선순위 규칙 (`priority_rule`)
기본값 **`"edd"`** (Earliest Due Date, `(due_date, processing_time)` 오름차순). 대안:

- `"atc"` (Apparent Tardiness Cost), `"slack"`(min-slack-first) -- 40개 로컬 인스턴스 초기 실측에서 EDD가
  21/40으로 우세(평균 484M vs slack 505M vs ATC 522M), 계속 옵션으로만 유지.
- `"regret"` -- 매 스텝 동적 재평가(O(n²)), 대형 인스턴스에서 EDD 대비 최대 2500배까지 패배 -- **사실상 폐기**,
  코드는 남아있지만 사용 안 함.
- **`"area"`/`"area_slack"` (2026-07-22 신설, v2 스냅샷에 누락돼 있던 부분)**: `area`는 순수 공간-패킹 우선
  (바운딩박스 면적 내림차순, due_date는 타이브레이커만), `area_slack`은 slack-ascending과 area-descending의
  순위합(rank-sum) 블렌드. EDD 단독으로 최선이 아닌 나머지 인스턴스에서 Phase 3에 구조적으로 다른 시작
  basin을 주려는 목적으로 도입 -- **standalone 기본값으로 쓰라는 게 아니라, `myalgorithm._iterated_greedy`의
  재시작 사이클(시도 4~5)에 섞어 넣기 위한 용도** (§2 참고). 40개 로컬 재측정 기준 EDD가 29-33/40으로
  더 강해졌고(다운스트림 repair/improve 로직이 개선되면서 construction 순서 선택의 영향력 자체가 줄어든
  것으로 추정), area/area_slack은 각 5-6/40에서만 승리하며 질 때는 종종 10~800배 크게 짐 -- 다만
  `_iterated_greedy`가 "개선될 때만 채택"이라 나쁜 시도는 반환값에 무해, 재시작 슬롯 하나를 소모할 뿐.

### 3.2 후보 위치 생성 -- 적응형 엔진 전환
- **N < 300블록**: `_candidate_positions` -- O(m²) 전체 교차곱(m = 그 시점까지 배치된 블록 수). 정확하지만
  크고 혼잡한 인스턴스에서 느림.
- **N ≥ 300블록** (`MAXRECTS_MIN_BLOCKS=300`): `_candidate_positions_maxrects2` -- MaxRects + sliver-pruning
  + 면적 내림차순 삽입. 500/1000블록 합성 스트레스 테스트에서 objective -44%/-23.5% 확인. 300~500 구간은
  아직 미검증.
- `xpress_reinsert.py`/`cpsat_reinsert.py`의 조인트 재배치 경로는 **항상 O(m²) 교차곱만 사용** (MaxRects는
  후보 다양성 붕괴 문제로 부적합함이 확인됨).
- 후보가 많으면(`CANDIDATE_SCAN_CAP=50` 초과) `_rank_candidates_by_earliest_bound`로 "가장 빨리 놓을 수 있는"
  기준 상위 몇 개만 남기고 스캔.

### 3.3 시간 슬롯 탐색 -- `_find_earliest_slot`
후보 (bay, orientation, x, y)가 정해지면, 그 자리에 놓을 수 있는 **가장 이른** crane-feasible 진입/이탈 시각을
찾음. Stage-2(진입)/3(이탈)/4(사전검사: 새 블록이 이미 배치된 블록의 확정 진입/이탈을 나중에 가로막지 않는지)를
모두 검사. 공간적으로 무관한(AABB 안 겹치는) 블록은 탐색에서 제외.

### 3.4 배치 확정 -- `_placement_score`
`w1×지연 + w2×정규화된_부하불균형 + w3×선호도페널티`(+미세한 top_y 타이브레이커) 최소 조합을 즉시 확정
(비가역적, Serial SGS).

### 3.5 시간 예산 관리
- `phase1_deadline`/`hard_deadline` = timelimit의 (인스턴스 크기별) 일정 비율. `deadline`~`hard_deadline`
  구간(overtime)에서는 `OVERTIME_SCAN_CAP=5` + 최우선순위 bay 1개로만 축소된 저비용 탐색을 계속 수행.
- `hard_deadline` 넘긴 블록만 `_force_place`로 감.

### 3.6 강제 배치 -- `_force_place`
`_aabb_gap_entry`로 새 블록의 world AABB와 실제로 겹치는 블록들만 나가면 되는 시각을 계산 -- AABB가 안
겹치는 두 블록은 어떤 layer에서도 절대 충돌할 수 없다는 증명에 기반해 crane-safe. `sorted_cache`가 주어지면
O(1)-근처 증분 조회, 없으면 O(현재 배치 수) 선형 스캔 -- 둘 다 수학적으로 같은 값을 반환(§9.1 참고, 이
캐시를 빠뜨렸던 두 지점을 오늘 수정함).

### 3.7 재시작 다양성 -- `_perturb_construction_order`
`seed=0`이면 완전히 무변화. `seed≥1`이면 `_CONSTRUCTION_SHUFFLE_WINDOW=5` 크기 슬라이딩 윈도우 안에서만
살짝 섞음.

### 3.8 비활성 상태인 대안 경로
- `construction_mode="batched"`: 배치당 오버헤드가 순수 그리디보다 커서 항상 패배(최대 647배) -- **폐기**.
- `priority_rule="regret"`: O(n²) 비용 때문에 EDD에 최대 2500배까지 패배 -- **폐기**.

---

## 4. Phase 2 -- Repair (`_repair`)

Phase 1 이후 남을 수 있는 crane/공간 제약 위반을 고침. 최대 10 패스, `timelimit*0.78`을 넘으면 중단.

1. `check_feasibility`로 위반 목록을 받음. **Blocking-chain aware**: 피해자뿐 아니라 가해자(blocker) id도
   같이 뽑아 재배치 대상에 포함.
2. 현재 tardiness가 큰 순으로 정렬(동률이면 EDD).
3. **repair_mode="greedy"(기본값)**:
   - 두 블록 이상이면 먼저 `xpress_reinsert.reinsert()`로 동시 재배치 시도.
   - 실패하면 한 블록씩 순차 그리디 재배치(`_place_blocks`, overtime 완화 적용, `deadline=0.80`/
     `hard_deadline=0.85`).
   - **verify-then-commit**: 결과를 바로 반영하지 않고, `check_feasibility`로 위반이 실제로 줄었을 때만
     커밋. 아니면 `to_repair` 전체를 `_force_place`로 폴백.
     **2026-07-24 수정**: 이 force-place 폴백 호출이 `hard_deadline`을 안 넘겨서 `_force_place`의
     `sorted_cache` 최적화가 한 번도 안 켜지던 문제를 고침 (§9.1 참고) -- `to_repair` 전원이 이미
     `forced_ids`에 들어간 상태라 처음부터 캐시를 켜도 안전.
   - **사이클 감지**: 같은 블록이 2회 이상 연속 위반되면 `forced_ids`에 추가돼 탐색 없이 강제배치로 감.
4. **최종 feasibility 보장(final guarantee)**: 루프가 78% 시간컷/max_passes로 끝났는데도 위반이 남아있으면,
   남은 위반 블록 전부를 `_force_place`로 밀어넣어 무조건 feasible하게 반환.
   **2026-07-24 수정**: 이 루프도 `sorted_cache` 없이 매 블록마다 O(현재까지 배치 수) 선형 스캔을 하고
   있었음 -- Phase 1 overtime 구간에서 이미 한 번 발견/수정됐던 "자기강화 O(n²) 스파이럴"과 동일한 패턴이
   여기 남아있던 것. 로컬 40개 인스턴스에선 이 단계까지 남는 위반이 적어 체감되지 않았지만, 아직 못 본 크고
   혼잡한 히든 인스턴스에서 이 단계까지 수백~수천 개가 밀리면 TLE로 이어질 수 있는 잠재 위험이었음 (§9.1).

---

## 5. Phase 2.5/2.6 -- Justification (RCPSP 압축)

- **`_left_justify`**: 각 bay 안에서 진입시각 오름차순으로, 자기 bay/위치/방향은 그대로 두고 진입만 더 당길
  수 있으면 당김. 전체 스윕을 `check_feasibility` 1회로 검증 후 개선 안 되면 통째로 버림.
- **`_right_justify` + 재-`_left_justify`**: L-R-L 교대 압축. 오른쪽으로 미는 건 개별 블록의 지연을 일부러
  늘릴 수 있음(의도적 탐색 단계) -- 반드시 뒤이어 left-justify + 전체 검증 필요.

---

## 6. Phase 3 -- Improve (`_improve`, ALNS)

"파괴(destroy) → 재삽입(repair) → 개선됐으면 채택" 구조의 Large Neighborhood Search. Z1(지연)/Z2(부하불균형)/
Z3(선호도)를 동시에 개선.

### 6.1 파괴 연산자
| 연산자 | 겨냥하는 목표 | 방식 |
|---|---|---|
| `tardy` | Z1 | 현재 지연이 가장 큰 블록 K개 |
| `swap` | Z1 (deadlock 해소) | 최악 지연 블록 + 그 블록의 "이상적 시간대"를 같은 bay에서 점유 중인 블록, 정확히 2개 |
| `wholebay` | Z1 (구조적) | 가장 무거운 bay의 블록 전부, 그 bay로 candidate 생성 제한 |
| `preference` | Z3 | 선호도 페널티가 가장 큰 블록 K개 |
| `balance` | Z2 | 가장 무거운(가중부하) bay에서 workload 큰 순 K개 |
| `random` | 다양성 | 무작위 K개 |

`z2z3_modes=False`면 `preference`/`balance`/`random`은 빠지고 `tardy`/`swap`/`wholebay`만 남음.

### 6.2 재삽입 경로 (연산자별 우선순위)
1. `mode=="balance"` → `_try_rebalance_move`로 가장 가벼운 bay에 직접 강제 배치 시도.
2. `mode=="wholebay"` → `xpress_reinsert.reinsert(restrict_bay_id=그 bay, max_per_block=40)`.
3. 그 외 (또는 위가 실패) & `1 < len(remove_ids) <= JOINT_MAX_K=12` → `xpress_reinsert.reinsert()`로 조인트
   재배치.
4. 전부 실패/해당없음 → 순차 그리디 `_place_blocks`(ATC 우선순위 정렬, `prev_assignments`로 "제자리 유지" 시도
   포함) -- 여기는 아직 `hard_deadline` 없음(§9 알려진 한계, 의도적으로 안 건드림).

### 6.3 채택 기준
- 기본(hill-climbing): 엄격하게 개선될 때만 채택.
- `annealing=True`(실험적): `exp(-Δ/T)` 확률로 워스닝도 수락, `best`는 여전히 strictly-better일 때만 갱신.
- `z23_relax=True`(기본값, `preference`/`balance` 한정): `current_obj` 기준 최대 1% 악화 허용,
  `best_obj`의 105% 상한. 남은 시간 15초 미만이면 완화 끔. `best_assignments`는 이 완화와 무관하게 항상
  순수 가중합 최선만 유지.
- **배치(batched) 확정 검증 (2026-07-22 도입)**: 매 accept마다 전체 `check_feasibility`를 부르는 대신,
  `check_feasibility_incremental`(변경된 블록만 재검증)로 매 라운드 스크리닝하고, `CONFIRM_CHECK_INTERVAL=10`
  accept마다 + 함수 반환 직전 1회 **무조건** 전체 `check_feasibility`로 확인. 불일치 발견 시 마지막 확인된
  체크포인트로 전부 롤백 후 조기 종료 -- 624개 표본 shadow-validation에서 불일치 0건이었지만, 진짜 괴리가
  나더라도 최대 10 accept 안에 잡히고 절대 미검증 상태를 반환하지 않도록 설계됨.

### 6.4 연산자 가중치 (ALNS 룰렛휠)
매 라운드 `random.choices`로 가중 랜덤 선택, 결과에 따라 지수이동평균(`WEIGHT_DECAY=0.8`)으로 갱신, 보상은
새최선 3.0 / 수락 0.5 / 거부 0.0. `MIN_WEIGHT=0.01` 하한(언더플로우 방지). Z1이 이론적 하한에 도달하면
`tardy`/`swap`은 그 라운드만 가중치 0(영구 제거 아님). `tardy`/`swap`은 Z1 헤드룸이 있는 동안
`Z1_URGENCY_BOOST=3.0`배 부스트.

### 6.5 K값과 정지 조건
`k_values`는 (1,2,3,5,8,12,n/10,n/5)를 n/3 이하로 캡. `stall_threshold = min(0.25×남은예산, 15.0초)` +
`MIN_ROUNDS_SINCE_IMPROVE=5` 라운드 둘 다 만족해야 조기 종료(시간 조건 단독 아님, 느린 라운드 1개를 정체로
오판 방지).

---

## 7. `xpress_reinsert.py` -- 조인트 재배치 MIP

- **범위**: 소규모 배치(K≲12, `wholebay`는 예외적으로 최대 백여 개)를 대상으로, 후보 위치는
  `_top_candidates_for_block`(O(m²) 교차곱)을 재사용. Xpress는 "K개를 서로 안 겹치게 어떻게 조합하는 게
  총점이 최소인가"라는 독립집합형 배정 문제만 풂.
- **충돌 판정**: 같은 레벨 정적 겹침(`check_collisions`) **그리고** crane 진입/이탈 상위 레벨 스침
  (`_crane_conflict`) 둘 다 검사.
- **후보 다양성 보강 장치 세 가지**:
  1. `_current_position_candidate`: 각 블록의 현재 위치를 후보에 강제 포함.
  2. `_cross_position_candidate` (K≤`CROSS_INJECT_MAX_K=5`): 배치 내 다른 블록의 현재 자리도 후보로 주입
     (진짜 맞교환 탐색용).
  3. same-bay-first fast path: 블록의 현재 bay만 먼저 검색해서 `max_per_block` 쿼터가 채워지면 다른 bay
     스캔 생략.
- **2026-07-23/24 안전 수정 (완료)**: 위 두 주입 후보(`_current_position_candidate`/
  `_cross_position_candidate`) 모두 원래 "배치(batch) 멤버끼리만" 충돌 검사하고 **배치에 속하지 않는 기존
  점유 블록과는 검사 안 하는** 구조적 결함이 있었음 -- `_repair`의 blocking_chain 경로(to_repair 자체가 이미
  위반 중)에서 실제로 기존 정상 블록을 소급 파손시키는 사례가 재현됨(prob_39). `_cross_candidate_blocked_by_
  existing()`을 두 주입 지점 모두에 적용해 수정 완료, `equiv_cpsat_vs_xpress.py` 재검증에서 INFEASIBLE-
  MISMATCH 2→0 확인.
- **2026-07-24 성능 개선 1 -- 그리드 기반 쌍별 충돌 사전필터**: 기존 쌍별 루프가 `O(K²×candidates²)` 순수
  반복문(멀리 있는 쌍까지 전부 순회 후 `_time_overlaps`/`_bb_overlap`으로 걸러냄)이었던 걸,
  `baseline_greedy.bucket_candidate_pairs_by_grid()`(median footprint 크기 기준 그리드 셀 버킷팅, 셀
  공유하는 쌍만 비교)로 후보 쌍 자체를 좁히도록 교체. **OLD/NEW 쌍별 충돌 집합 완전 일치 검증
  통과**(`analysis/xpress_reinsert_grid_equivalence.py`, K=6/20/46/100). **성능은 기대만큼은 아니었음**:
  K=100에서만 1.8배 개선(199.5s→111.7s), K≤46에서는 거의 차이 없거나 오히려 소폭 느림(K=46: 35.8s→41.5s) --
  실측해보니 이 문제의 후보들은 실제로 공간적으로 많이 뭉쳐 있어서(경합 중인 bay라서) 그리드가 걸러낼
  "먼 쌍" 자체가 적었음. 그래도 정확성엔 문제없고 큰 K에서 손해는 없어 반영 유지.
- **2026-07-24 성능 개선 2 -- 전역(런 전체) 기하 사실 캐시**: 위 그리드 필터로도 못 줄인 진짜 병목("가까운
  쌍 자체가 많아서 매번 Shapely를 다시 부름")을 겨냥. 핵심 통찰(사용자 제안): `check_collisions`/
  `check_entry` 결과는 **오직 두 블록의 고정된 (bay, x, y, orient) 위치만의 함수**이고 시간이나 다른
  블록 상태와 무관 -- `_crane_conflict`가 시간을 쓰는 부분은 "4개 경계 조건 중 어느 걸 적용할지 고르는
  것"뿐, Shapely 호출 자체의 답은 항상 같음. `_cached_geometry_facts()`가 `(bay_id, block_i위치,
  block_j위치)`로 정규화한 키로 `(same_level_collision, i_blocks_j, j_blocks_i)` 세 불리언을 캐싱,
  `_crane_conflict_from_facts()`가 이 캐시된 사실 + 이번 라운드 실제 시간만으로 Shapely 호출 없이 순수
  비교. **캐시는 `greedyalgorithm()` 호출마다 새로 만들어서 `_repair`/`_improve`를 거쳐 명시적으로
  전달**(모듈 전역 dict 절대 아님 -- `analysis/robustness_check.py`처럼 한 프로세스에서 여러 인스턴스를
  도는 호출자에서 인스턴스 간 캐시 오염이 구조적으로 불가능하도록). **검증 결과**: 같은 배치 5회 반복
  호출이 캐시 유무와 무관하게 **완전히 동일한 결과**(정확성 확인), 속도는 캐시로 **1.85배**(6.72s→3.63s).
  실제 `_improve()` 60초급 실행에서도 같은 objective에 도달하면서 **1.84배 빠름**(18.8s→10.2s, cache
  entries=261) -- 오늘 세 가지 성능 시도(CP-SAT 재작성, 그리드 필터, 이 캐시) 중 **가장 확실한 순이득**.
- **2026-07-24 버그 수정 -- 쌍별 충돌 제약 구성 루프의 deadline 미체크**: 9차 패키징 전 40개 로컬
  로버스트니스 재검증 중 `prob_6`(150블록)이 20초 예산에서 36.1초를 씀(+16.1초 초과) -- TIMING 로그로
  추적하니 그리드 필터로 근접쌍(`close_pairs`)을 찾는 단계(`bucket_candidate_pairs_by_grid`)는 deadline을
  주기적으로 체크하지만, 그 결과를 실제로 소비해서 `y[i]+y[j]<=1` 제약을 만드는 후속 루프(line 664 부근,
  `geometry_cache` 미스 시 실제 Shapely 호출 발생)에는 체크가 전혀 없었음 -- 후보가 한 bay에 조밀하게
  뭉쳐(경합 bay) `close_pairs`가 매우 커지는 경우(그리드 필터 자체의 알려진 약점, 위 항목 참조) 이 루프가
  deadline과 무관하게 끝까지 실행됨. `_CONFLICT_LOOP_DEADLINE_CHECK_INTERVAL=500`(그리드 헬퍼와 동일 주기)
  으로 이 루프에도 체크 추가, 걸리면 기존 계약대로 `None` 반환(호출자는 이미 순차 재삽입 폴백 보유).
  **검증**: prob_6 단독 18.0s(초과 0), 40개 전체 재실행 40/40 feasible·0 시간 초과, 새 체크가 실제로
  5회 발동(prob_6 하나만의 우연이 아니라 여러 인스턴스에서 재현되는 실제 패턴이었음).

### 7.5 `cpsat_reinsert.py` -- CP-SAT 대체 (interval-var + NoOverlap, 미배선)

- **2026-07-24 전면 재작성**: 기존 초안(정적 timing 후보 + 순수 pairwise `y_i+y_j<=1`, xpress_reinsert와
  본질적으로 동일한 모델)을 폐기하고, **entry_time을 진짜 CP-SAT `OptionalFixedSizeIntervalVar`로 만들어
  `AddNoOverlap`으로 푸는** 모델로 재작성. 위치 후보는 여전히 `_top_candidates_for_block` 재사용(이산,
  신규 지오메트리 로직 없음) -- 오직 시간 축만 연속 변수화.
- **핵심 근거**: 두 후보 위치가 정적으로(시간 무관하게) 충돌 가능한지는 `_static_conflict()`
  (`check_collisions` 또는 `check_entry` 양방향) 하나로 판정 가능하고, 이 판정이 True인 두 인터벌은
  **시간이 조금이라도 겹치면 반드시 실제 충돌**이라는 게 대수적으로 확인됨(`_crane_conflict`의 4개 경계
  조건이 전부 "시간이 겹칠 때만" 발동 가능) -- 즉 페어당 `AddNoOverlap([interval_i, interval_j])` 하나면
  충분히 안전. 기존 점유(ambient) 블록도 고정 interval로 만들어 같은 방식으로 처리.
- **실측 결과 (2026-07-24, `analysis/cpsat_interval_vs_xpress_probe.py` / `cpsat_wholebay_probe.py`)**:
  - **정확성**: 합성 sanity test 2건 + 실제 인스턴스 비교 전부 `check_feasibility` 통과, xpress 대비
    objective 동일하거나 근소한 차이.
  - **소규모 K(=6, ambient 블록 실제로 남겨둔 현실적 시나리오)**: xpress와 **사실상 동일**(prob_9: 완전
    동일, prob_40: +0.0%) -- 시간 유연성이 실제로는 별 도움 안 됨(이미 다른 블록들이 공간을 채우고 있어
    타이밍 여유가 없음).
  - **wholebay 스케일(K=46, bay 전체를 실제로 비움)**: objective는 xpress와 **완전히 동일**했지만 cpsat이
    **25배 느림**(1.2s vs 31.1s, 그중 9.7s가 NoOverlap 제약 39,275개 생성, 21.3s가 solve) -- 이 문제의
    진짜 어려운 부분(위치 조합)은 여전히 순수 이산 assignment-with-conflicts 구조라 Xpress의 LP
    relaxation/clique cut이 유리한 영역이고, CP-SAT의 NoOverlap은 시간 축에만 도움을 줌.
  - **K=165**: 둘 다 시간 예산 내 실패(xpress는 candidate 생성 단계에서, cpsat은 처음엔 grid-cell 안쪽
    반복문 안에 deadline 체크가 없어서 179초까지 초과 -- **버그 발견/수정**: cell 단위 체크를 셀 안쪽
    이중루프에도 추가.
  - **초기(비현실적) 실험의 정정**: 애초에 이 CP-SAT 방향을 검토하게 만든 "timing 후보 3개 추가만으로
    -95~99%" 결과(`analysis/time_diversity_probe.py`)는 알고 보니 **bay 전체를 실제로 비운(ambient 없음)
    비현실적 세팅**에서 나온 것 -- 실제 소규모 배치(다른 블록들이 남아있는 현실적 상황)에서는 재현 안 됨.
- **결론 (2026-07-24 시점)**: 모델 자체는 정확하고 재사용 가능하지만, **테스트한 두 시나리오(소규모 K,
  wholebay K=46) 어디서도 xpress 대비 실질적 이득을 못 보였고, wholebay에서는 오히려 훨씬 느림** --
  당장 프로덕션에 배선할 근거는 아직 없음. 코드는 남겨두되(모델이 정확하다는 것 자체는 검증됨) 파이프라인
  연결은 보류. `same-bay-first`/`_cross_position_candidate`는 여전히 미이식.
- **현재 상태**: `myalgorithm.py`/`baseline_greedy.py` 어디서도 import되지 않음(완전히 비활성, 제출 zip에도
  미포함 -- 애초에 프로덕션 경로에서 안 쓰이므로 포함해도 무해하지만 관례상 3개 파일만 패키징).

---

## 8. 핵심 상수 요약

| 상수 | 값 | 위치/의미 |
|---|---|---|
| `MAXRECTS_MIN_BLOCKS` | 300 | 이 블록 수 이상이면 Phase 1이 MaxRects 후보 생성 사용 |
| `CANDIDATE_SCAN_CAP` | 50 | 평상시 (bay, orientation)당 후보 스캔 상한 |
| `OVERTIME_SCAN_CAP` | 5 | deadline~hard_deadline 구간의 축소 스캔 상한 |
| `phase1_deadline` / `hard_deadline` | timelimit의 (크기별) 20-50% / 30-60% | Phase 1 시간 예산 |
| repair의 deadline / hard_deadline | timelimit의 80% / 85% | Phase 2 시간 예산 |
| `xpress_max_per_block` | 20 (wholebay는 `WHOLE_BAY_MAX_PER_BLOCK=40`) | 조인트 MIP 블록당 후보 수 |
| `JOINT_MAX_K` | 12 | 이 초과면 Xpress 대신 순차 그리디 |
| `STALL_TIME_CAP` / `MIN_ROUNDS_SINCE_IMPROVE` | 15.0초 / 5회 | Phase 3 정체 판정 |
| `CONFIRM_CHECK_INTERVAL` | 10 | Phase 3 배치 확정 검증 주기(accept 수 기준) |
| `MIN_WEIGHT` | 0.01 | ALNS 연산자 가중치 하한(언더플로우 방지) |
| `Z1_URGENCY_BOOST` | 3.0 | tardy/swap 룰렛휠 확률 부스트 |
| `_RESTART_MAX_ATTEMPTS` / `_RESTART_MIN_BUDGET` | 5회 / 10.0초 | 반복 그리디 재시작 |
| `_PRIORITY_RULE_CYCLE` | [edd,edd,edd,area_slack,area] | 재시작 시도별 Phase-1 규칙 순환 |
| `seed` (기본값) | 0 | 재현성 기준선 (하이퍼파라미터 튜닝 아님) |
| `geometry_cache` | `greedyalgorithm()`이 매 호출마다 생성 | `xpress_reinsert.reinsert()`의 쌍별 기하 판정 캐시, 인스턴스 간 절대 공유 안 됨 |
| `_GRID_DEADLINE_CHECK_INTERVAL` | 500 | `bucket_candidate_pairs_by_grid`의 셀 안쪽 루프 deadline 체크 주기 |
| `_CONFLICT_LOOP_DEADLINE_CHECK_INTERVAL` | 500 | `xpress_reinsert.reinsert()`의 제약 구성(소비) 루프 deadline 체크 주기 |

---

## 9. 알려진 한계 / 잔여 리스크 (현재 시점)

### 9.1 오늘(2026-07-24) 수정/추가 완료된 항목
1. **`cpsat_reinsert.py`의 존재-블록 안전성 결함** -- xpress_reinsert.py의 f0eee17 수정이 이식 안 돼 있던
   것을 발견/수정 (§7.5). 미배선 상태라 실채점 영향은 없었음.
2. **`_repair`의 두 강제배치(force-place) 경로가 `sorted_cache` 없이 O(n²)-류 스캔을 하던 문제** -- (a)
   "verify 실패 시 to_repair 전체 강제배치" 폴백, (b) 루프 끝의 "최종 feasibility 보장" 강제배치 루프. 둘 다
   Phase 1 overtime 구간에서 이미 한 번 발견/수정됐던 것과 동일한 패턴이 남아있던 것 (§4).
3. **`xpress_reinsert.py` 쌍별 루프 그리드 사전필터** -- 정확성 검증 완료, 성능은 K=100에서만 유의미(§7).
4. **`xpress_reinsert.py` 전역 기하 사실 캐시** -- 정확성/성능 둘 다 확실히 검증(§7), 오늘 세 성능 시도 중
   가장 확실한 순이득.
5. **`cpsat_reinsert.py` interval-var + NoOverlap 전면 재작성** -- 모델은 정확하지만 실측 이득 없음(§7.5),
   프로덕션 배선은 보류.
6. **40개 로컬 로버스트니스 최종 재검증(그리드 필터 + 기하 캐시 반영 후)**: 40/40 feasible, 0 crash. 첫
   실행에서 나왔던 "2건 시간 초과"는 그 자리에서 CPU 경합/노이즈로 판단했었으나, **이 판단은 정정 필요** --
   9차 패키징 직전 재검증에서 실제로 `prob_6`이 재현 가능한 시간 초과(+16.1초)를 보였고, 원인은 노이즈가
   아니라 §7의 실제 deadline-체크 누락 버그였음(항목 7 참조). 그 버그 수정 후 재검증은 40/40 feasible,
   **0 시간 초과**(새 deadline 체크가 5회 실제 발동한 상태에서).
7. **`xpress_reinsert.py` 쌍별 충돌 제약 구성 루프의 deadline 미체크 수정** -- 항목 6에서 발견, §7에 상세
   기록. 9차 제출 전 마지막 수정사항.

### 9.2 아직 남은 한계
1. **`_improve`의 그리디 폴백(K≤12, Xpress 실패 시)은 여전히 `hard_deadline` 없음** -- Phase 1/Repair와 같은
   절벽이 이론상 있지만, 이미 Phase 3 예산 끝자락이라 확장 시 오히려 TLE 위험이 있어 의도적으로 안 건드림.
2. `regret`/`construction_mode="batched"` 경로도 같은 결함이 있으나 둘 다 폐기된 비활성 경로.
3. **결과가 근본적으로 벽시계 시간 의존적**: 재시작 횟수, Phase 1/2/3 간 시간 배분, overtime 판정 전부
   `time.time()` 기반이라, 채점 서버(다른 CPU/부하)에서 로컬과 다른 결과가 나올 수 있음 -- 오늘도 동시
   실행 중인 다른 프로세스가 로버스트니스 결과에 노이즈를 만드는 걸 직접 관측함.
4. `MAXRECTS_MIN_BLOCKS=300` 임계값은 N=100/500/1000 세 지점만으로 정한 것 -- 300~500 구간 미검증.
5. `cpsat_reinsert.py`는 오늘 실측 결과 xpress 대비 이득이 없어(wholebay에서 25배 느림) 프로덕션 배선
   근거가 아직 없음 -- 코드는 정확성이 검증된 채로 남겨두되, 사용하지 않기로 함(§7.5).
6. **아직 검증 안 된, 사용자 제안 2번 아이디어**: K 규모에 반비례하는 동적 후보 할당(`max_per_block`을
   K가 커질수록 줄여서 총 후보 풀을 상수로 유지) -- idea 1(기하 캐시)과 달리 후보 다양성을 줄이는 것이라
   품질 트레이드오프가 있음, 별도 검증 필요, 아직 미착수.
7. **P3 회귀(`b621af5`, 5→6차) 서브원인 여전히 미확정**: 이번 세션에 6개 서브변경 전부(이전 세션 3개 +
   오늘 새로 발견한 `_try_rebalance_move` 캡 포함 3개) 개별 테스트 완료했지만 전부 무죄 -- 복합효과이거나
   원 이진탐색 앵커 자체가 노이즈에 오염됐을 가능성. `ogc2026_p4p6_investigation` 메모리 참조.
8. **오늘의 성능 최적화(그리드 필터, 기하 캐시)는 전부 "Phase 3 라운드를 더 싸게 만드는" 방향**이지, P4-P6가
   여전히 objective 10~52M대에 갇혀있는 근본 원인을 직접 겨냥하진 않음 -- 라운드가 빨라진 게 실제로 큰/혼잡한
   인스턴스의 objective 개선으로 이어지는지는 별도로 확인 필요.

---

## 이 문서 갱신 규칙

- **매 제출 준비 사이클마다 새 버전 파일을 만들 것** (`algorithm_overview_v{N}.md`, N은 다음 제출 회차 번호)
  -- 이전 버전 파일은 그대로 보존(덮어쓰지 않음), 새 사이클 시작 시 그 시점의 "현재 상태"를 새 파일에 처음부터
  다시 정리. 한 사이클 안에서 구조가 계속 바뀌면 같은 버전 파일을 직접 고쳐 쓰되, 사이클이 끝나고 다음 제출
  준비가 시작되면 새 파일로 넘어갈 것.
- 날짜별 변경 이력(성공/실패한 시도 전부)은 계속 `algorithm_overview.md`에 append -- 이 규칙은 바뀌지 않음.
- 오래된 버전 파일들은 삭제하지 말 것 -- 제출별로 코드가 실제로 어떻게 달랐는지 추적하는 히스토리 역할.
