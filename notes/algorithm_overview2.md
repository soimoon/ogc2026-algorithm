# 알고리즘 오버뷰 2 -- 현재 코드가 실제로 돌아가는 흐름 (2026-07-22 기준 스냅샷)

`algorithm_overview.md`가 "지금 코드가 왜 이렇게 생겼는지"(시간순 변경 이력 + 실패한 시도까지 전부)를 기록한 문서라면,
이 문서는 그 역사를 걷어내고 **"지금 이 순간 코드가 실제로 무엇을 하는가"**만 한 번에 훑을 수 있게 정리한 것입니다.
기술보고서의 "알고리즘 설계" 섹션 초안으로 바로 활용 가능하도록 작성했습니다. 커밋 `4b3c706` 시점 기준이며,
이후 바뀐 게 있으면 이 문서도 같이 갱신해야 합니다 (하단 "이 문서 갱신 규칙" 참고).

---

## 0. 세 파일의 역할

| 파일 | 역할 |
|---|---|
| `myalgorithm.py` | 대회 채점 서버가 호출하는 진입점. 재시작 루프 + 크래시/타임아웃/infeasible에 대한 최후 안전망. |
| `baseline_greedy.py` | 실제 알고리즘 본체 (Phase 1~3, 전부). |
| `xpress_reinsert.py` | Phase 2/3에서 여러 블록을 동시에 재배치할 때 쓰는 소규모 Xpress MIP. |
| `utils.py` | **채점 서버가 매번 원본으로 덮어씀** -- 절대 수정 금지, 여기 있는 `check_feasibility`가 유일한 정답 판정 기준. |

---

## 1. 전체 파이프라인 한눈에

```
myalgorithm.algorithm(prob_info, timelimit)
  └─ _solve()
       └─ _iterated_greedy()  -- 남은 시간이 있는 한 최대 5회 재시작
            └─ baseline_greedy.greedyalgorithm(prob_info, timelimit=남은시간, seed=0,1,2,...)
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
(Phase 2/2.5/2.6/3)는 "시도 → 검증 → 개선 안 되면 원래 상태로 롤백" 패턴을 예외 없이 따름 -- 어떤 단계가 중간에
시간 초과로 끊겨도 그 시점까지의 best는 항상 feasible. 이 안전망 자체는 계속 유지(feasibility를 잃으면 그 즉시
-1이라 협상의 여지가 없음).

**개발 전략(2026-07-22 수정)**: 다만 "품질보다 안전 먼저"라는 태도를 알고리즘 설계 자체의 보수성으로까지 확대
적용하진 않기로 함 -- 5차 제출이 로컬 검증을 전부 통과한 안전한 변경들로만 구성됐는데도 실채점에서는 P1/P3가
오히려 악화되고 P4/P5/P6은 여전히 수천만 단위에 그대로 갇혀 있었음(선두권과 점수 격차가 큰 상황에서, "로컬에서
안 나빠짐"을 확인하며 조금씩 개선하는 접근 자체가 이 격차를 좁히기엔 부족하다는 뜻). 그래서 앞으로는 **실험은
브랜치에서 과감하게**(구조적으로 다른 접근, 리스크 있는 시도 포함) 하고, 실제로 검증된 것만 `main`에 올려
제출하는 방식으로 전환. 위에서 설명한 "시도→검증→롤백" 안전망과 [[feedback_empirical_verification]]의
"바꾸기 전엔 항상 A/B 테스트" 원칙은 그대로 유지 -- 바뀐 건 "얼마나 과감한 걸 시도해볼지"와 "그걸 어디서(브랜치
vs main) 시도할지"임.

---

## 2. `myalgorithm.py` -- 진입점과 재시작

### `_iterated_greedy` (반복 그리디 재시작)
- 남은 예산(`deadline - now`)이 `_RESTART_MIN_BUDGET=10.0`초 미만이면 중단, 최대 `_RESTART_MAX_ATTEMPTS=5`회.
- 매 시도마다 `seed=attempt-1` (0, 1, 2, ...)로 `greedyalgorithm`을 다시 호출 -- seed=0은 완전히 결정론적인
  "기본" 시도, seed≥1은 Phase 1 블록 순서를 살짝 섞고(`_perturb_construction_order`) Phase 3 ALNS의 난수도 다르게 소비.
- **이전 시도보다 objective가 개선될 때만 채택** (`if objective < best_objective`) -- 즉 재시작이 아무리 나빠도
  최종 결과가 이전 최선보다 나빠지는 일은 수학적으로 없음. 각 시도는 남은 시간을 통째로 받으므로, 첫 시도가
  일찍 정체되면 재시작이 그 남은 시간을 다른 basin 탐색에 씀.
- `timelimit * 0.9`만 내부 예산으로 쓰고 나머지 10%는 `check_feasibility` 검증 + 비상 폴백용 여유로 남겨둠.

### `_emergency_fallback` (최후의 보루)
- `baseline_greedy`를 아예 import하지 않는 완전히 독립적인 코드. 블록을 due_date 순으로, bay마다 **한 번에
  하나씩만 순차 배치**(겹치는 시간대가 아예 없도록) -- 구조적으로 항상 feasible. 품질은 매우 나쁘지만
  "메인 솔버가 통째로 죽어도 -1은 피한다"는 목적 하나만 있음.

---

## 3. Phase 1 -- 초기 배치 (construction)

### 3.1 우선순위 규칙 (`priority_rule`)
기본값 **`"edd"`** (Earliest Due Date, `(due_date, processing_time)` 오름차순). 대안으로 `"atc"`(Apparent
Tardiness Cost), `"slack"`(min-slack-first), `"regret"`(매 스텝 동적 재평가)이 구현돼 있지만, 40개 로컬 인스턴스
실측(`analysis/priority_rule_compare.py`) 결과 **EDD가 21/40 인스턴스에서 최선**(평균 objective 484M, slack
505M, ATC 522M)이라 계속 기본값으로 유지. `regret`은 O(n²) 비용 때문에 대형 인스턴스에서 실패해 사실상 미사용.

### 3.2 후보 위치 생성 -- 적응형 엔진 전환
- **N < 300블록**: `_candidate_positions` -- 이미 배치된 블록들의 (오른쪽 x, 위쪽 y) 좌표를 전부 모아 만드는
  **O(m²) 전체 교차곱** (m = 그 시점까지 배치된 블록 수). 정확하지만 크고 혼잡한 인스턴스에서 느림.
- **N ≥ 300블록** (`MAXRECTS_MIN_BLOCKS=300`): `_candidate_positions_maxrects2` -- **MaxRects**(여유 사각형
  추적) + sliver-pruning(요청 블록보다 작은 조각은 즉시 버림) + 면적 내림차순 삽입(파편 수 최소화). 500/1000블록
  합성 스트레스 테스트에서 objective -44%/-23.5% 확인. 이 임계값(300)은 로컬 검증 가능한 상한(prob_20=300블록)
  기준으로 보수적으로 잡은 값 -- 300~500 구간은 아직 미검증.
- 두 엔진 모두 최종 도달 가능한 빈 공간 집합은 동일(증명됨)하지만 후보 **다양성**이 달라서, `xpress_reinsert.py`의
  조인트 재배치 경로는 **항상 O(m²) 교차곱만 사용**(MaxRects는 "빈 공간이 안 남아 후보가 1개로 붕괴"하는 문제가
  있어 그쪽엔 부적합함이 확인됨).
- 후보가 많으면(`CANDIDATE_SCAN_CAP=50` 초과) `_rank_candidates_by_earliest_bound`로 "가장 빨리 놓을 수 있는"
  기준 상위 몇 개만 남기고 스캔 -- bay/orientation마다 독립적인 예산(공유하면 한쪽이 굶는 버그가 있었음).

### 3.3 시간 슬롯 탐색 -- `_find_earliest_slot`
후보 (bay, orientation, x, y)가 정해지면, 그 자리에 놓을 수 있는 **가장 이른** crane-feasible 진입/이탈 시각을
찾음. `check_feasibility`의 Stage 2(진입)/3(이탈)을 그대로 미러링하고, 추가로 **Stage-4 사전검사**(새 블록이
이미 배치된 다른 블록의 이미 확정된 진입/이탈을 나중에 가로막지 않는지)까지 검사 -- 이게 빠지면 "일단 배치하고
나중에 repair가 항상 고쳐야 하는" 구조가 됨. 공간적으로 무관한(AABB 안 겹치는) 블록은 이 탐색에서 아예 제외
(spatial pre-filter, O(m²) 비용의 실질적 상수 절감).

### 3.4 배치 확정 -- `_placement_score`
후보 조합마다 `w1×지연 + w2×정규화된_부하불균형 + w3×선호도페널티`(+미세한 top_y 타이브레이커)를 계산해서
**가장 낮은 값을 즉시 확정**(비가역적, Serial SGS). 실제 인스턴스별 `w1/w2/w3`를 그대로 사용.

### 3.5 시간 예산 관리 (오늘 수정된 부분)
- `phase1_deadline = timelimit*0.5`, `phase1_hard_deadline = timelimit*0.6`.
- **2026-07-22 이전**: `deadline`을 넘는 순간부터 남은 블록 전부가 탐색 0회로 강제배치 -- 이진 절벽이라 몇 초의
  머신 타이밍 노이즈만으로 결과가 자릿수 단위로 요동칠 수 있었음(`prob_10` 27배 회귀로 발견).
- **현재**: `deadline`~`hard_deadline` 구간(`overtime`)에서는 **`OVERTIME_SCAN_CAP=5`**(기존 50 대신)로 축소하고
  **최우선순위 bay 1개로만** 제한한 저비용 탐색을 여전히 수행 -- 탐색을 완전히 0으로 만들지 않음. `hard_deadline`도
  넘긴 블록만 `_force_place`로 감. 남은 블록 비율과 무관하게 전부 이 완화를 받음(예전엔 남은 5% 이하일 때만 적용).

### 3.6 강제 배치 -- `_force_place`
전부 실패했거나 deadline을 완전히 넘긴 블록의 최후 수단. **`_aabb_gap_entry`**로 새 블록의 world AABB와 실제로
겹치는 블록들만 나가면 되는 시각을 계산(전체 bay가 빌 때까지 기다리던 예전 `_empty_bay_entry`보다 훨씬 덜
보수적) -- AABB가 안 겹치는 두 블록은 어떤 layer에서도 절대 충돌할 수 없다는 증명에 기반해 여전히 crane-safe.

### 3.7 재시작 다양성 -- `_perturb_construction_order`
`seed=0`이면 완전히 무변화(재현성 기준선). `seed≥1`이면 `_CONSTRUCTION_SHUFFLE_WINDOW=5` 크기 슬라이딩 윈도우
안에서만 살짝 섞음 -- EDD 순서 철학은 유지하면서 재시작마다 실제로 다른 구체적 배치 순서를 만들어냄.

### 3.8 비활성 상태인 대안 경로
- `construction_mode="batched"` (`_place_blocks_batched`, 배치 단위로 Xpress 조인트 배치): 500/1000블록
  스트레스 테스트에서 배치당 오버헤드가 순수 그리디보다 커서 항상 패배(최대 647배) -- **폐기, 기본값은
  `"serial"`**. 코드는 남아있지만 안 씀.
- `priority_rule="regret"`: O(n²) 비용 때문에 대형 인스턴스에서 EDD에 최대 2500배까지 패배 -- **폐기**.

---

## 4. Phase 2 -- Repair (`_repair`)

Phase 1 이후 남을 수 있는 crane/공간 제약 위반을 고침. 최대 10 패스, `timelimit*0.80`을 넘으면 중단(Phase 3에
시간을 남기기 위해 90%가 아니라 80%로 낮춤).

1. `check_feasibility`로 위반 목록을 받음. **Blocking-chain aware**: 위반 메시지("block 33 exit obstructed by
   block 96")에서 피해자뿐 아니라 **가해자(blocker) id도 같이 뽑아** 재배치 대상에 포함 -- blocker가 여유가
   있으면 살짝 옮겨서 피해자가 원래(더 좋은) 자리를 지킬 수 있게 함.
2. 현재 tardiness가 큰 순으로 정렬(동률이면 EDD).
3. **repair_mode="greedy"(기본값)**:
   - 두 블록 이상이면 먼저 `xpress_reinsert.reinsert()`로 **동시에** 재배치 시도(순차 배치의 "먼저 뺀 블록이
     나중 블록 자리를 가로챈다" 순서 문제를 원천 차단).
   - 실패하면 한 블록씩 순차 그리디 재배치(`_place_blocks`) -- **Phase 1과 동일한 overtime 완화**를 오늘
     새로 적용(`deadline=timelimit*0.80`, `hard_deadline=timelimit*0.85`). 자체 보유하던 "75% 지나면 무조건
     강제배치" 사전 트리거도 85%로 올려서 새 hard_deadline과 정합시킴(그렇지 않으면 hard_deadline이 도달하기도
     전에 이 사전 트리거가 먼저 발동해 무용지물이 됨 -- 오늘 같이 발견/수정).
   - **verify-then-commit**: 이 패스의 결과를 바로 반영하지 않고, `check_feasibility`로 위반이 실제로 줄었을
     때만 커밋. 아니면 원래 상태에서 `to_repair` 전체를 `_force_place`(구조적으로 항상 feasible)로 폴백 --
     매 패스가 절대 이전보다 나빠지지 않음을 보장.
   - **사이클 감지**: 같은 블록이 2회 이상 연속 위반되면 `forced_ids`에 추가돼 탐색 없이 강제배치로 감(무한
     루프 방지).
4. **repair_mode="simple"**: 위치는 그대로 두고 시간대만 빈 곳으로 밈. 더 빠르지만 품질 개선은 없음(현재
   기본값은 아님).

---

## 5. Phase 2.5/2.6 -- Justification (RCPSP 압축)

- **`_left_justify`**: 각 bay 안에서 진입시각 오름차순으로, 자기 bay/위치/방향은 그대로 두고 진입만 더 당길
  수 있으면 당김. Phase 1은 "자기 지연이 0이면" 더 일찍 넣을 유인이 없어서 bay가 비어있어도 블록이 필요
  이상 늦게 들어가는 경우가 있었음(`analysis/bay_utilization.py`로 확인, 피크 점유율 1~51%). 개별 이동이
  전부 "더 이르거나 같음"만 채택하는 안전한 압축이라 전체 스윕도 `check_feasibility` 1회로 검증 후 개선 안
  되면 통째로 버림.
- **`_right_justify` + 재-`_left_justify`**: RCPSP 문헌의 L-R-L 교대 압축 기법 -- 오른쪽으로 미는 건 개별
  블록의 지연을 일부러 늘릴 수 있음(다른 배치 구조를 탐색하려는 의도적 단계), 그래서 **반드시 뒤이어
  left-justify를 한 번 더 돌리고 전체 결과를 통째로 검증**해야 안전함. 개선 없거나 infeasible이면 폐기.

---

## 6. Phase 3 -- Improve (`_improve`, ALNS)

이미 feasible한 해를 대상으로, 남은 시간 전부를 써서 Z1(지연)/Z2(부하불균형)/Z3(선호도)를 동시에 개선.
"파괴(destroy) → 재삽입(repair) → 개선됐으면 채택" 구조의 Large Neighborhood Search.

### 6.1 파괴 연산자 (`_select_removal_candidates`)
| 연산자 | 겨냥하는 목표 | 방식 |
|---|---|---|
| `tardy` | Z1 | 현재 지연이 가장 큰 블록 K개 |
| `swap` | Z1 (deadlock 해소) | 최악 지연 블록 + 그 블록의 "이상적 시간대"를 같은 bay에서 점유 중인 블록, 정확히 2개 |
| `wholebay` | Z1 (구조적) | 가장 무거운 bay의 블록 **전부**(K가 100+ 될 수 있음), 그 bay 하나로 candidate 생성을 제한해서 tractable하게 만듦 |
| `preference` | Z3 | 선호도 페널티가 가장 큰 블록 K개 |
| `balance` | Z2 | 가장 무거운(가중부하) bay에서 workload 큰 순 K개 |
| `random` | 다양성 | 무작위 K개 (국소 최적 탈출용) |

`z2z3_modes=False`면 `preference`/`balance`/`random`은 빠지고 `tardy`/`swap`/`wholebay`만 남음.

### 6.2 재삽입 경로 (연산자별 우선순위)
1. `mode=="balance"` → **`_try_rebalance_move`**로 가장 가벼운 bay에 직접 강제 배치 시도(Z2는 max/병목
   함수라 일반 재삽입이 정확히 그 bay를 고른다는 보장이 없어서 전용 로직).
2. `mode=="wholebay"` → `xpress_reinsert.reinsert(restrict_bay_id=그 bay, max_per_block=40)`로 bay 하나
   전체를 조인트 재최적화.
3. 그 외 (또는 위가 실패) & `1 < len(remove_ids) <= JOINT_MAX_K=12` → `xpress_reinsert.reinsert()`로 조인트
   재배치(swap의 K=2도 항상 이 경로).
4. 전부 실패/해당없음 → 순차 그리디 `_place_blocks`(ATC 우선순위로 정렬, `prev_assignments`로 "제자리
   유지" 시도 포함) -- **여기는 아직 `hard_deadline` 없음** (아래 8절 참고).

### 6.3 채택 기준
- 기본(hill-climbing): **엄격하게 개선될 때만** 채택 (`delta < -1e-6`).
- `annealing=True`(실험적, 기본 아님): `exp(-Δ/T)` 확률로 워스닝도 수락(모의 담금질), `best`는 여전히
  strictly-better일 때만 갱신되므로 최종 반환값은 절대 나빠지지 않음.
- `z23_relax=True`(기본값, `preference`/`balance` 한정): 이 두 연산자는 채택률이 매우 낮았음(15.8%/32.8% vs
  tardy 62.9%) -- `w1`이 보통 압도적이라 Z1이 조금이라도 나빠지면 거의 항상 기각되기 때문. `current_obj`(워크
  상태) 기준 최대 1%(`Z23_RELAX_FRAC`)까지 악화를 허용하되 `best_obj`의 105%(`Z23_MAX_DRIFT_FRAC`)를 못 넘게
  상한. 남은 시간이 15초(`Z23_MIN_REMAINING_FOR_RELAX`) 미만이면 회복할 시간이 부족하니 완화 자체를 끔.
  **`best_assignments`(실제 반환값)는 이 완화와 무관하게 한 번도 나빠진 적 없는 순수 가중합 최선만 유지.**
- 매 accept 직전 **`check_feasibility` 전체 재검증**(내부적으로는 빠른 `check_feasibility_incremental`로
  매 라운드 스크리닝하다가, 실제로 채택하는 순간에만 전체 재검증 -- Stage 1~5를 처음부터 다시 계산하는
  대신 이번 라운드에 변경된 블록/bay만 좁혀서 검사).

### 6.4 연산자 가중치 (ALNS 룰렛휠)
- 매 라운드 `random.choices`로 가중 랜덤 선택(복원추출). 결과(새 최선/수락/거부)에 따라 지수이동평균으로
  가중치 갱신: `WEIGHT_DECAY=0.8`, 보상은 새최선 3.0 / 수락 0.5 / 거부 0.0.
- **`MIN_WEIGHT=0.01` 하한**: 이 하한이 없으면 계속 거부만 당하는 연산자가 기하급수적으로 감소하다 float64
  최소값 아래로 언더플로우(약 3,339회 연속 거부)해서 전부 0이 되면 `random.choices`가 크래시(-1점) --
  오늘이 아니라 어제 발견/수정된 버그지만 현재도 유효.
- **Z1 하한 도달 시 `tardy`/`swap` 처리**: `analysis/lower_bound.py`가 계산한 Z1의 이론적 하한(release_time+
  processing_time-due_date의 합, 공간/crane 무시)에 도달하면 그 라운드에 한해서만 두 연산자 가중치를 0으로 --
  **영구 제거는 아님**(어제 있었던 버그: 영구 제거하면 Z2/Z3 트레이드오프로 나중에 Z1이 다시 나빠져도 못 고침).
- **`tardy`/`swap` urgency boost**: Z1이 하한에 도달하지 않은 동안, 이 두 연산자의 룰렛휠 확률만
  `Z1_URGENCY_BOOST=3.0`배 부스트(EMA 가중치 자체는 안 건드림) -- w1이 보통 압도적이라 Z1 개선이 우선이라는
  판단.

### 6.5 K값과 정지 조건
- `k_values`는 (1,2,3,5,8,12,n/10,n/5)를 n/3 이하로 캡, 라운드마다 순환.
- **정체 감지**: `stall_threshold = min(0.25 × 남은예산, 15.0초)` 동안, 그리고 **최소 5라운드
  (`MIN_ROUNDS_SINCE_IMPROVE`) 이상 시도**했는데도 새 최선이 없으면 조기 종료 -- 시간 조건 단독이 아니라
  라운드 수 조건도 AND로 걸어서, 혼잡한 인스턴스에서 라운드 하나가 15초를 넘게 걸리는 경우(500/1000블록
  스트레스 테스트에서 실측됨) "느린 라운드 1개"를 "정체"로 오판해 조기 재시작하는 걸 막음.
- 모든 활성 연산자가 이번 라운드에 후보를 못 낸 걸 확인하면(`empty_operators_seen`) "더 개선할 거 없음"으로
  종료.

---

## 7. `xpress_reinsert.py` -- 조인트 재배치 MIP

- **범위**: 소규모 배치(K≲12, `wholebay`는 예외적으로 최대 백여 개)를 대상으로, 각 블록의 후보 위치는
  `baseline_greedy`의 기존 탐색(`_top_candidates_for_block`, O(m²) 교차곱 기반)을 그대로 재사용 -- 새 지오메트리
  로직 없음. Xpress가 하는 일은 오직 "이 K개를 서로 안 겹치게 어떻게 조합하는 게 총점이 최소인가"라는
  독립집합형 배정 문제 하나뿐: `y[block, candidate]` 이진변수, "블록당 후보 정확히 1개" 제약, 그리고 시공간이
  겹치는 후보쌍마다 "동시 선택 불가" 컷.
- **충돌 판정**: 같은 레벨 정적 겹침(`check_collisions`) **그리고** crane 진입/이탈 시 상위 레벨 스침
  (`_crane_conflict`, 문제 명세의 4가지 경계 케이스) 둘 다 검사 -- 하나만 빠뜨렸다가 실제 회귀를 낸 적 있음.
- **안전망**: 이게 뭘 제안하든 호출자(`_improve`/`_repair`)가 `check_feasibility`로 재검증 후에만 받아들이므로,
  여기의 불완전한 충돌 모델은 "잘못된 제안을 (정당하게) 거부당하는" 것 이상의 사고를 낼 수 없음. 실패/불가/
  Xpress 미설치 시 전부 `None` 반환, 호출자는 항상 그리디 폴백으로 이어짐.
- **후보 다양성 보강 장치 세 가지** (전부 "그냥 제안 하나 추가"일 뿐, 정확성 게이트 아님):
  1. `current_positions` → `_current_position_candidate`: 각 블록의 **현재 위치**를 후보에 강제 포함 --
     "전부 제자리 유지"가 항상 하나의 유효한 해임을 보장(특히 wholebay에서 필수).
  2. `current_positions` (K≤`CROSS_INJECT_MAX_K=5`) → `_cross_position_candidate`: 배치 내 **다른 블록의
     현재 자리**도 후보로 주입 -- 서로 자리를 막고 있는 두 블록의 진짜 맞교환(swap)을 조인트 MIP가 최소한
     "고려라도" 할 수 있게 함. `CROSS_INJECT_SIZE_SLACK=1.3`배 이상 크기 차이 나면 사전 필터로 건너뜀(비용
     절감용, 정확성과 무관).
  3. **same-bay-first fast path**: 블록의 현재 bay만 먼저 검색해서 `max_per_block` 쿼터가 채워지면 다른
     bay 스캔을 생략(혼잡한 bay에서 후보 생성 자체가 라운드 비용의 대부분을 차지했던 걸 완화).

---

## 8. 핵심 상수 요약

| 상수 | 값 | 위치/의미 |
|---|---|---|
| `MAXRECTS_MIN_BLOCKS` | 300 | 이 블록 수 이상이면 Phase 1이 MaxRects 후보 생성 사용 |
| `CANDIDATE_SCAN_CAP` | 50 | 평상시 (bay, orientation)당 후보 스캔 상한 |
| `OVERTIME_SCAN_CAP` | 5 | deadline~hard_deadline 구간의 축소 스캔 상한 |
| `phase1_deadline` / `hard_deadline` | timelimit의 50% / 60% | Phase 1 시간 예산 |
| repair의 deadline / hard_deadline | timelimit의 80% / 85% | Phase 2 시간 예산 |
| `xpress_max_per_block` | 20 (wholebay는 `WHOLE_BAY_MAX_PER_BLOCK=40`) | 조인트 MIP 블록당 후보 수 |
| `JOINT_MAX_K` | 12 | 이 초과면 Xpress 대신 순차 그리디 |
| `STALL_TIME_CAP` / `MIN_ROUNDS_SINCE_IMPROVE` | 15.0초 / 5회 | Phase 3 정체 판정 |
| `MIN_WEIGHT` | 0.01 | ALNS 연산자 가중치 하한(언더플로우 방지) |
| `Z1_URGENCY_BOOST` | 3.0 | tardy/swap 룰렛휠 확률 부스트 |
| `_RESTART_MAX_ATTEMPTS` / `_RESTART_MIN_BUDGET` | 5회 / 10.0초 | 반복 그리디 재시작 |
| `seed` (기본값) | 0 | 재현성 기준선 (하이퍼파라미터 튜닝 아님, 임의 고정값) |

---

## 9. 알려진 한계 / 잔여 리스크 (현재 시점)

1. **`_improve`의 그리디 폴백(K≤12, Xpress 실패 시)은 여전히 `hard_deadline` 없음** -- Phase 1/Repair와 같은
   절벽이 이론상 있지만, 이미 Phase 3 예산 끝자락이라 확장 시 오히려 TLE 위험이 있어 의도적으로 안 건드림.
2. `regret`/`construction_mode="batched"` 경로도 같은 결함이 있으나 둘 다 폐기된 비활성 경로.
3. **결과가 근본적으로 벽시계 시간 의존적**: 재시작 횟수, Phase 1/2/3 간 시간 배분, `_place_blocks`/`_repair`의
   overtime 판정 전부 `time.time()` 기반이라, 채점 서버(다른 CPU/부하)에서 로컬과 다른 결과가 나올 수 있음 --
   구조적으로 완전히 해소된 적은 없고, 오늘 작업으로 "노이즈가 결과에 미치는 영향의 크기"만 크게 줄인 상태.
4. `MAXRECTS_MIN_BLOCKS=300` 임계값은 N=100/500/1000 세 지점만으로 정한 것 -- 300~500 구간(정확히는 대회
   히든 인스턴스 크기 전반)은 미검증.
5. `STALL_TIME_FRAC`/재시작 예산 등 시간 비율 기반 로직 전체를 실제 4코어 CPU 제한 조건에서 재검증한 적 없음.

---

## 이 문서 갱신 규칙

`algorithm_overview.md`처럼 매 변경마다 새 항목을 추가하는 게 아니라, **이 문서는 "현재 상태"를 나타내는
스냅샷**입니다. 알고리즘 구조가 바뀌면(새 Phase, 새 연산자, 핵심 상수 변경 등) 해당 섹션을 직접 고쳐 쓸 것 --
낡은 내용을 이력으로 남기고 싶으면 그건 `algorithm_overview.md`에 적고, 여기는 항상 "지금 맞는 설명"만 유지.
