# 알고리즘 개요: 베이스라인 흐름 → 변경 이력 → 추가 개선 아이디어

`notes/experiments.md`가 시간순 실험 로그라면, 이 파일은 "지금 코드가 왜 이렇게 생겼는지"를 한 번에 이해할 수 있게 정리한 문서입니다. 기술보고서 초안으로도 그대로 재활용 가능하도록 작성했습니다.

---

## 1. 원래 베이스라인(`baseline_greedy.py`) 동작 흐름

대회 측이 제공한 베이스라인은 **"일단 빠르게 다 채우고(Phase 1), 문제 생기면 나중에 고친다(Phase 2)"** 구조입니다. 두 단계로 나뉩니다.

### Phase 1 — EDD 그리디 배치

1. 모든 블록을 **마감일(due_date) 빠른 순**으로 정렬합니다 (EDD = Earliest Due Date). 이 순서가 곧 "배치 시도 순서"이고, 한 번 배치를 확정하면 절대 다시 건드리지 않습니다(그리디).
2. 순서대로 블록 하나씩 꺼내서, **가능한 모든 (bay, 방향, 위치) 조합**을 다 훑어봅니다.
   - 위치 후보(`_candidate_positions`)는 "바운딩 박스 기준 bottom-left" 방식으로 만듭니다. 이미 놓인 블록들의 바운딩 박스 우측/상단 끝에 딱 붙는 지점들을 후보로 삼는 식이라, 실제 다각형 모양(오목한 부분 등)은 고려하지 않습니다.
   - 각 후보 위치마다 `_find_earliest_slot`으로 "이 위치에 놓을 수 있는 가장 이른 시각"을 찾습니다. 이때 crane으로 넣고 뺄 때 다른 블록에 막히지 않는지(Stage-2/3 검사)까지 확인합니다.
3. 후보들 중 `_placement_score`(= `w1×지연 + w2×부하불균형 + w3×선호도페널티`, 실제 그 인스턴스의 가중치를 그대로 사용) 값이 가장 낮은 조합을 **즉시 확정**합니다.
4. 정말 아무 데도 못 들어가면 `_force_place`로 강제 배치(빈 bay 시간대에 밀어넣기)합니다.

이 단계가 끝나면 100개 블록이면 100개 다 배치는 되지만, crane 순서 제약 같은 걸 완벽히 못 지킨 경우가 남아있을 수 있습니다.

### Phase 2 — Repair (사후 수리)

1. `check_feasibility`로 전체를 검사해서 위반 사항을 찾습니다.
2. 위반된 블록들만 골라서(원래는 due_date 순으로) 다시 배치를 시도합니다. 두 가지 모드:
   - **greedy 모드**(기본값): 위반 블록을 빼고 상태를 재구성한 뒤, Phase 1과 똑같은 전체 탐색으로 다시 자리를 찾습니다. 같은 블록이 2번 이상 계속 위반되면(무한루프 방지) 강제 배치로 전환합니다.
   - **simple 모드**: 위치는 그대로 두고 시간대만 밀어서 빈 구간을 찾습니다. 더 빠르지만 배치 품질 개선은 없습니다.
3. 최대 10번 반복하고, feasible해지면 즉시 종료합니다.

### 원래 구조의 한계 (오늘 작업의 출발점)

- **시간 예산 관리가 허술함**: Phase 1엔 아예 시간 제한 체크가 없었고(큰 인스턴스에서 Phase 1이 전체 시간을 다 써버릴 위험), Phase 2만 90%/98% 기준으로 관리했습니다.
- **`greedyalgorithm()`이 infeasible해도 그냥 반환**: repair가 다 못 끝내도 "INFEASIBLE"이라고 로그만 찍고 결과는 그대로 돌려줍니다. 이걸 그대로 제출하면 채점 서버에서 자동 -1점입니다.
- **"feasible하지만 많이 늦은" 블록을 고치는 단계가 아예 없음**: Phase 2는 물리적으로 위반된(충돌/crane 제약 위반) 블록만 건드리고, 시간표상 feasible하지만 지연이 큰 블록은 Phase 1에서 확정된 그대로 끝까지 갑니다.
- **배치 순서가 항상 EDD 고정**: 다른 우선순위 규칙을 실험해볼 여지가 코드에 없었습니다.

---

## 2. 변경 이력

날짜별로 아래에 계속 이어서 추가. 새 변경사항이 생기면 맨 아래에 `### YYYY-MM-DD` 섹션을 새로 붙여서 그날 바꾼 것들을 순서대로 정리.

### 2026-07-20

1. **`myalgorithm.py` 전면 재작성 — 안전 래퍼 추가** (`_solve`, `_emergency_fallback`)
   `baseline_greedy.greedyalgorithm()`을 부르고 나서 **직접 `check_feasibility`로 재검증**합니다. 예외가 나거나 infeasible한 결과가 나오면, `baseline_greedy`를 아예 import하지 않는 완전히 독립적인 `_emergency_fallback`(블록을 bay별로 한 번에 하나씩만 순차 배치 — 구조적으로 항상 feasible)으로 대체합니다. `algorithm()` 자체도 try/except로 한 번 더 감싸서 어떤 경우에도 예외가 밖으로 새지 않게 했습니다.

2. **`baseline_greedy.py` Phase 1에 시간 데드라인 추가**
   `_place_blocks`에 `deadline` 파라미터를 추가해서, 정해진 시각을 넘기면 남은 블록은 바로 강제배치로 넘어가게 했습니다. (처음엔 블록 단위로만 체크했는데, 테스트 중 **블록 하나의 탐색 자체가 몇 초씩 걸려서 데드라인을 넘기는 문제**를 발견 → `_find_earliest_slot`과 후보 위치 반복문 내부에도 세분화된 체크를 추가.)

3. **Repair 단계 정렬 기준 변경**: `to_repair`를 EDD 순 대신 **현재 tardiness(지연) 큰 순**으로 정렬하도록 변경. 가장 심하게 늦은 블록이 좋은 자리를 먼저 가져가도록 했습니다.

4. **Phase 1 우선순위 규칙 3종 구현 + 실측 비교**
   `priority_rule` 파라미터로 `edd`/`slack`(min-slack-first)/`atc`(Apparent Tardiness Cost, release_time을 t로 근사) 세 가지를 다 만들고, 40개 train 인스턴스에 동일 조건(15초)으로 A/B/C 테스트했습니다. 결과: **EDD가 21승(평균 obj 484M), slack 11승(505M), ATC 8승(522M)** — 이론상 더 정교해 보였던 ATC/slack이 실제로는 원래 있던 단순 EDD를 못 이겼습니다. 그래서 **기본값은 EDD로 유지**했고, 나머지 둘은 옵션으로만 남겨뒀습니다.

5. **Phase 3(`_improve`) 신설 — feasible하지만 지연 큰 블록 개선**
   Phase 2가 끝난 뒤, feasible한 해에서 tardiness가 큰 블록 K개(1→2→3→5 순환)를 빼서 다시 넣어보고, **실제 objective가 개선될 때만 채택**(아니면 롤백)하는 LNS(large neighborhood search) 스타일 반복을 추가했습니다. 남는 시간을 다 쓸 때까지(또는 개선이 더 없을 때까지) 반복합니다.

6. **Phase 1/2/3 시간 배분 재조정**: 원래 Phase 1이 75%, repair가 98%까지 쓸 수 있었는데, Phase 1은 시간이 있으면 있는 대로 다 쓰는 구조라 실제로 Phase 3에 시간이 거의 안 남는 문제를 발견 → **Phase 1은 50%, repair는 80%까지**로 낮춰서 Phase 3에 확실히 시간을 남기도록 재조정.

7. **`xpress_reinsert.py` 신설 — Xpress MIP로 K블록 동시 재배치**
   Phase 3에서 K개(2개 이상)를 뺐다가 다시 넣을 때, 그리디로 한 개씩 넣는 대신 **Xpress로 K개를 동시에 최적 조합**하도록 함. 각 블록의 후보 위치는 기존 `_candidate_positions`/`_find_earliest_slot`을 그대로 재사용해서 상위 N개만 뽑고(기하학적 로직 재발명 없음), 후보끼리 겹치면 안 된다는 제약만 새로 추가한 작은 MIP(독립집합 형태)입니다. K=1은 조합 최적화할 게 없어서 그리디 유지, Xpress가 없거나 실패하면 자동으로 그리디 재배치로 폴백합니다.

8. **`_rebuild_bay_state` 공용 함수로 정리**: repair와 Phase 3가 똑같이 쓰던 "assignments에서 bay 상태 재구성" 코드가 중복돼 있던 걸 함수 하나로 뽑아서 공유하도록 정리.

9. **`_top_candidates_for_block` 공용 함수로 정리 + Regret-2 dynamic construction 추가 → 실측 후 폐기**
   `xpress_reinsert.py`에만 있던 "블록 하나의 top-N 후보 뽑기" 로직을 `baseline_greedy.py`로 옮겨서 공유하도록 정리. 이걸 재사용해서 `priority_rule="regret"` 신설 — 정적 순서(EDD/slack/ATC) 대신, 매 스텝마다 "지금 최선 후보 vs 차선 후보" 점수 차이(regret)가 가장 큰 블록부터 배치. 다만 매 라운드 남은 블록 전체를 재평가해야 해서 O(n²)에 가까워, 전체 예산의 일부만 적응형으로 쓰고 나머지는 EDD로 폴백하는 하이브리드로 구현.
   **실측 결과(180초, 인스턴스 10개 중 4개 완료 시점에 결론 남): EDD가 압도적으로 승리** — prob_1 edd=795,089 vs regret=22,116,862 / prob_5 edd=133,539 vs regret=3,911,670 / prob_8 edd=11,252 vs regret=27,701,420 / prob_10 edd=200,567 vs regret=50,800,236. 최대 2,500배 가까이 나빴음. 300블록 인스턴스에서는 regret이 예산을 다 쓰고도 300개 중 16개밖에 처리 못 하고 나머지 284개를 급하게 EDD로 몰아넣는 것도 확인됨 — 매 라운드 "남은 블록 전체 재평가" 비용이 예상보다 훨씬 커서 실용적이지 않음. **`priority_rule` 기본값은 계속 `edd`로 유지, `regret`은 폐기(코드는 남겨두되 사용 안 함).**

10. **Repair 단계를 blocking-chain-aware로 확장**
    `utils.check_feasibility`의 위반 메시지가 "block 33 exit obstructed by block 96"처럼 **누가 누구를 막고 있는지**를 이미 텍스트에 담고 있다는 걸 활용. 기존엔 위반 메시지에서 첫 번째 block id(피해자)만 뽑아서 그 블록만 재배치했는데, 이제 **정규식으로 두 번째 block id(가해자/blocker)도 같이 뽑아서 repair 대상에 포함**시킴. blocker가 slack이 있으면 그쪽을 살짝 옮겨서 피해자가 원래(더 좋은) 자리를 지킬 수 있게 됨. 여러 단계로 이어지는 연쇄(A를 막는 B, B를 막는 C)는 기존의 다중 pass 반복 구조에서 자연스럽게 커버됨(별도 그래프 추적 코드 없이).

11. **blocking-chain repair가 되려 objective를 크게 악화시키는 회귀(regression) 발견 → 3단계로 근본 원인 추적/수정**
    1차 제출본보다 오히려 훨씬 나쁜 결과가 나오는 걸 발견해서 3개 레이어의 버그를 순서대로 찾아냄: (a) 순차적으로 제거→재삽입하면 피해자가 blocker의 원래 자리를 먼저 차지해버리는 **순서 문제** → `xpress_reinsert.py` 신설해서 K개를 그리디 순차가 아니라 **Xpress로 동시에** 배치하도록 우선 시도. (b) 그런데도 INFEASIBLE이 반복 → `xpress_reinsert.py`의 배치 쌍 충돌 검사가 `check_collisions`(같은 레벨 정적 겹침)만 하고 있었고, **크레인 진입/이탈 시 상위 레벨 스침(cross-level)** 룰을 빠뜨리고 있었음 → `_crane_conflict()` 신설(문제9의 4가지 경계 케이스 체크)로 수정. (c) 그래도 joint 재배치 성공률이 거의 0% → 후보를 블록별로 독립적으로 top-8만 뽑다 보니, 애초에 경합 중인 블록들의 후보가 서로 겹치는 곳에 몰려서 조합 자체가 안 나오는 경우가 많았음 → `max_per_block` 8→20으로 확대.

12. **construction 단계(`_find_earliest_slot`) Stage-4 사전검사의 구조적 허점 발견/수정**
    Phase 1이 새 블록을 넣을 때 "이미 배치된 블록의 이미 확정된 entry/exit을 새 블록이 나중에 가로막지는 않는지"를 제대로 검사하지 않고 있었음(완전히 중첩된 케이스만 약하게 걸러내는 `check_collisions` 호출 하나뿐). 이미 배치된 블록마다 entry-시점 침범/exit-시점 침범/steady-state 겹침 3가지를 각각 정확히 검사하도록 재작성. 이게 바로 "원래 pristine baseline조차 매번 repair pass가 필요했던" 근본 원인이었을 가능성이 높음 — 수정 후 prob_1은 repair pass 0회로 통과(이전엔 항상 1~2회 이상).

13. **Repair(Phase 2)에 verify-then-commit 안전장치 추가**
    Phase 3(`_improve`)는 원래도 "시도 → `check_feasibility`로 검증 → 개선 안 되면 롤백" 구조였는데, Phase 2(repair)는 그런 검증 없이 joint/sequential 재배치 결과를 바로 반영하고 있었음. `base_assignments`(시도 전 스냅샷)를 남기고, 시도 결과를 `trial_assignments`에 쓴 뒤 **`check_feasibility` 1회로 검증 → 위반이 줄었을 때만 커밋, 아니면 `base_assignments`에서 강제배치(`_force_place`, 구조적으로 항상 feasible)로 폴백**하도록 변경. 실제로 left-justify 스윕 결과가 전체적으로는 infeasible해지는 케이스를 이 안전장치가 정상적으로 잡아내고 롤백하는 것도 확인함.

14. **Phase 2.5 신설 — Left Justification (`_left_justify`)**
    RCPSP schedule justification 기법 중 Left justification 적용: repair 이후, 각 bay 안에서 entry 시각 오름차순으로 블록을 순회하며 **자기 bay/자리/방향은 그대로 두고 entry만 더 당길 수 있는지**(`_find_earliest_slot` 재사용) 확인해서 당김. Phase 1은 "자기 자신의 tardiness가 0이면" 더 일찍 넣을 유인이 없어서 bay가 실제로는 비어있는데도 블록이 필요 이상 늦게 들어가는 경우가 있었음(`analysis/bay_utilization.py`로 확인, 피크 점유율 1~51%). 전체 스윕 결과를 `check_feasibility` 1회로 검증해서 개선 안 되거나 infeasible해지면 통째로 롤백하므로 절대 악화시키지 않음.

15. **Phase 3(`_improve`)를 제대로 된 ALNS(Adaptive Large Neighborhood Search)로 재작성**
    기존엔 K를 1→2→3→5로 고정 순환하고, Z2/Z3 타겟 모드는 실패 시 escalate/prune하는 임시방편이었음. 이번에 세 가지를 한 번에 반영: (a) **K 값 범위 확대**(1,2,3,5,8,12,n/10,n/5를 n/3 이하로 캡) — 기존엔 최대 K=5뿐이라 탐색 이웃이 좁았음. (b) **destroy 연산자(tardy/preference/balance/random)를 지수이동평균 가중치 기반 룰렛휠로 확률적 선택** — "새로 최고 기록 갱신=보상 3.0, 개선 없이 수락=0.5, 거부=0.0"으로 매 라운드 가중치 업데이트, 잘 먹히는 연산자가 자연스럽게 더 자주 선택되도록 함(기존의 임시방편 escalate/prune 로직을 완전히 대체). (c) **random destroy 연산자 추가** — tardy/preference/balance 셋 다 "특정 기준으로 나쁜 블록"만 노려서 로컬 최적에 갇히기 쉬우므로, 무작위로 블록을 뽑는 네 번째 연산자로 다양성 확보. K>12는 여전히 그리디만 사용(Xpress 조합 폭발 방지).

16. **`_improve`의 "NEW BEST" 로그 포맷 수정 (표시만 수정, 계산 로직엔 문제 없었음)**
    `f"NEW BEST obj -{gain:.0f} -> {best_obj:.0f}"`가 개선폭(gain, 항상 양수)에 마이너스 부호를 붙여 찍다 보니 마치 "이전 objective가 음수였다"처럼 보였음(예: `obj -2222874 -> 181068061`). 실제 계산은 맞았음(직전 Phase 로그의 obj와 gain을 대조해서 확인). `f"NEW BEST obj {prev_best:.0f} -> {best_obj:.0f} (gain={gain:.0f})"`로 명확하게 수정.

17. **ALNS 연산자 선택용 RNG seed를 고정값(0)으로 변경 — 재현성 확보**
    `_improve`의 `rng = random.Random(seed)`가 `seed=None`(unseeded) 기본값이라, **같은 인스턴스·같은 timelimit이라도 실행할 때마다 다른 objective**가 나오는 걸 발견(prob_23, 180초: 재실행마다 55.6M~80.6M로 요동). `greedyalgorithm()`에 `seed: int | None = 0` 파라미터를 추가해 기본값을 고정. **주의**: 이 0이라는 값은 로컬 40개 인스턴스에서 성능 비교로 고른 게 아니라 임의로 정한 상수 — "제일 잘 나오는 seed를 찾아서 고정"하면 그 자체가 로컬 데이터에 대한 하이퍼파라미터 오버피팅이 되므로 의도적으로 비교 없이 고정함. (여러 seed가 비정상적으로 나쁜 값은 없는지 스팟체크하는 건 제출과 무관한 후속 작업으로 남겨둠.)

**2026-07-20 최종 검증**: 위 11~17번 변경사항을 모두 반영한 코드로 40개 train 인스턴스 전체 로버스트니스 체크(90초 예산) → **40/40 feasible, infeasible/crash/timeout 0건**. 1차 제출본(`submissions/submission_20260720_0516.zip`) 대비 대표 인스턴스 8개(prob_1,5,9,17,20,23,31,40, 180초 예산) 품질 비교 → **6승 1무 1패**(prob_23의 1패는 재실행 결과 55.6M vs 80.6M로 크게 요동해서 실제 regression이 아니라 위 17번 seed 미고정 이슈로 판명, seed 고정 후 재현 가능). seed 고정 + 로그 포맷 수정 후 다시 40개 전체 스모크 테스트(20초 예산) → 40/40 feasible 재확인. `submissions/submission_20260720_1937.zip`으로 아카이브.

18. **Phase 1을 배치 기반 준-Parallel-SGS로 재작성 시도 → 실측 후 폐기 (2026-07-20)**
    "비가역적 그리디(Serial SGS)가 Phase 2 repair가 항상 필요해지는 구조적 원인"이라는 문제의식에서, `_place_blocks_batched` 신설: EDD 정렬된 블록을 `PHASE1_BATCH_SIZE`(6, 이후 4로도 실험)개씩 묶어서 `xpress_reinsert.reinsert()`(Phase 3가 K블록 동시 재삽입에 쓰던 바로 그 조인트 MIP, "새 블록 삽입"에도 동일하게 재사용 가능함을 확인)로 한 번에 배치, MIP 실패 시에만 기존 `_place_blocks`로 개별 폴백. `construction_mode="batched"/"serial"` 스위치로 A/B 비교.
    **실측 결과(90초 예산, prob_1/9/17/23): 4개 인스턴스 전부 serial이 승리, 심하면 455배(prob_1) 나쁨.** 원인: 배치당 MIP(후보 생성 + O(batch²×candidates²) 충돌 제약 + solve)가 블록 1개당 순수 그리디보다 2~3배 느려서, `phase1_deadline`(예산 50%) 안에 다 못 끝내고 나머지가 `_force_place`(마감시한 무시하는 조잡한 배치)로 떨어짐 — **인스턴스가 클수록 강제배치 비율이 커지는 구조**(prob_1 16~22%, prob_9 55%, prob_17 약 70%). `batch_size`를 6→4, `max_per_block`을 20→8로 줄여서 배치를 더 싸게 만드는 시도도 해봤으나 **오히려 더 나빠짐**(prob_9 194.7M→254.9M, prob_17 147K→276.9M) — 배치 개수가 늘면서 배치당 고정 오버헤드(모델 빌드 자체)가 총비용을 더 키운 것으로 보임. 배치 크기를 줄이는 방향으로는 해결 안 됨을 확인, **이 접근 자체를 폐기**. `construction_mode` 기본값 `"serial"`로 되돌림(`_place_blocks_batched`/`xpress_reinsert.reinsert()`의 신규 삽입 재사용 코드는 남겨두되 비활성 — 나중에 근본적으로 다른 배치 전략(예: 후보 수를 극도로 줄이거나, 전체가 아니라 실제로 경합하는 블록에만 선택적으로 적용)이 떠오르면 재검토 가능).
    **교훈**: "구조적으로 더 나은 결정"이 "그 결정에 드는 시간 비용"을 못 이기면 오히려 손해라는 걸 재확인 — Phase 3의 조인트 재삽입(K≤12, 배치 수가 적음)에서는 이 오버헤드가 감당 가능했지만, Phase 1처럼 n_blocks/batch_size로 배치 수 자체가 수십 개인 곳에 그대로 옮기면 누적 비용이 지배적이 됨.

19. **timelimit 기반 Xpress 후보 수(`max_per_block`) 자동 분기 추가 (2026-07-20, 미검증)**
    2차 제출 실제 채점에서 P3만 1차 대비 4.6배 악화된 걸 계기로 로컬 40개에서 원인 추적 중, "1차 vs 2차"를 30초로 스크리닝했더니 상당수가 2차 패배로 나왔다가, 그 중 격차가 컸던 4개(prob_2/4/8/21)를 90초로 재확인하니 **4개 중 3개는 격차가 사라지거나 역전**됨(prob_21만 1.34배로 유지, P3의 4.6배와는 격차가 너무 커서 같은 원인인지 불확실 — 조사 계속 진행 중). 이 패턴 자체는 "2차가 1차보다 무거운 로직(후보 수 20개, verify-rollback, left-justify, 확장 ALNS)을 쓰다 보니 예산이 짧으면 라운드 수가 줄어 손해, 넉넉하면 이득"이라는 가설을 뒷받침함.
    이 중 가장 근거가 확실한 레버(`xpress_reinsert.reinsert()`의 `max_per_block`, 오늘 batched Phase 1 실험에서도 O(K²×candidates²) 비용의 핵심 변수로 확인됨)만 우선 반영: `greedyalgorithm()`에서 `timelimit < 90`이면 `_repair`/`_improve`가 Xpress 호출 시 `max_per_block=8`(기존 20 대신)을 쓰도록 분기. `SHORT_TIMELIMIT_THRESHOLD`/두 후보 수 상수만 바꾸면 되는 단일 지점이라 되돌리기 쉬움.
    **주의: 아직 실행 검증 전.** 40개 전체 재검증(90초, P3 원인 추적용)이 CPU를 쓰고 있어서 실제 before/after 비교는 미룸 — 코드만 작성해둔 상태, 다음 세션/여유 생기면 반드시 검증 필요.

20. **19번의 짧은-timelimit `max_per_block` 축소를 검증 전에 되돌림 (2026-07-20)**
    "짧으면 후보 수를 줄이자"는 게 근거 있어 보였지만 다시 보니 **직접적인 증거가 없었음** — 확실한 건 "90초로 늘리면 해소된다"는 것뿐이지, "20→8로 줄이면 90초 없이도 해소된다"는 건 검증한 적이 없었음. 게다가 바로 그날 18번 항목(batched Phase 1)에서 **같은 레버(max_per_block 20→8)를 실제로 줄여봤더니 오히려 더 나빠졌던** 직접적인 반례가 있음(맥락은 다르지만 — 거기선 배치 수가 수십 개라 고정 오버헤드 누적이 문제였고, 여기(`_repair`/`_improve`)는 호출 횟수가 훨씬 적어서 같은 정도로 나쁠지는 불확실하지만, 근거 없이 밀어붙일 이유는 없음). 짧은-timelimit 쪽은 이미 **2차 제출 실제 채점으로 검증된 상태**(1차 대비 6문제 중 4개 개선)라 근거 없는 변경으로 건드릴 필요가 없다고 판단, `max_per_block=20`으로 원복. 남은 노력은 **아직 한 번도 실험 안 해본 긴 timelimit(600초+) 쪽 — batched/MIP 확장이 거기선 실제로 도움되는지**에 집중하기로 함.

21. **preference/balance 연산자에 epsilon-constraint 스타일 완화 수락 기준 추가 (2026-07-20, 미검증)**
    세션 초반 "가중합 스칼라화는 w1이 크면 w2/w3 개선이 묻힌다"는 이론적 우려를 오늘 로그로 직접 수치화함 — 지금까지의 모든 로그를 grep해서 연산자별 수락률을 세어보니 **tardy 62.9%(151/240), random 57.0%(138/242) vs balance 32.8%(20/61), preference 15.8%(9/57)**로, Z2/Z3를 노리는 연산자가 절반 이하 확률로만 채택되고 있음이 확인됨 — 이론이 맞았음.
    대응: `_improve`의 라운드 수락 기준을 `preference`/`balance` 모드일 때만 완화. `annealing=True`일 때 이미 있던 "가끔 나빠지는 것도 받아들이는" walk/best 분리 패턴(`current_obj`=탐색 중인 상태, 가끔 나빠질 수 있음 / `best_obj`=지금까지 본 것 중 진짜 최선, 반환값, 절대 안 나빠짐)을 그대로 재사용 — `annealing=False`(기본값)여도 이 두 연산자에 한해서만 `delta <= Z23_RELAX_FRAC * max(1, current_obj)`(현재 objective의 1% 이내 악화)면 수락하되, `current_obj`가 `best_obj`의 105%(`Z23_MAX_DRIFT_FRAC`)를 넘지 않도록 상한도 걸어서 여러 라운드에 걸쳐 walk가 무한정 나빠지는 걸 방지함. `tardy`/`random`은 그대로 엄격한 hill-climbing 유지(이미 수락률이 높아서 안 건드림). **채점 기준 자체가 가중합이라 "Z2/Z3만 본 답"을 최종 제출할 수 없다는 점을 존중** — 완화는 오직 walk(current) 단계에만 적용되고, 실제 반환값(best)은 여전히 순수 가중합 기준으로 한 번도 나빠진 적 없는 것만 나옴.
    **주의: 아직 실행 검증 전** (P3 원인 추적용 40개 재검증이 CPU를 쓰고 있어서 미룸). `Z23_RELAX_FRAC=0.01`/`Z23_MAX_DRIFT_FRAC=0.05` 둘 다 근거 없이 정한 placeholder 값 — 검증 계획: (a) 이 변경 전/후로 preference/balance 수락률이 실제로 오르는지 로그로 재확인, (b) before/after objective 비교로 순수 이득인지 확인, (c) 혹시 `current_obj`가 계속 105% 상한 근처에 붙어서 유의미한 탈출을 못 하면 `Z23_RELAX_FRAC`을 키우거나 낮추는 재튜닝 필요.

22. **`balance` 연산자에 Z2 전용 "직접 재배치" 이동 추가 (2026-07-20, 미검증)**
    Z2는 Z1/Z3처럼 블록별 sum이 아니라 **bay 쌍 중 가중부하 차이가 최대인 쌍을 뽑는 max/병목 함수**라서, "가장 무거운 bay에서 블록을 빼서 범용 재삽입 검색에 맡기는" 기존 `balance` 모드는 재삽입된 블록이 정확히 그 병목 쌍(가장 가벼운 bay)으로 가리라는 보장이 없었음(`_placement_score`가 tardiness/preference까지 섞어서 판단하므로). 신설한 `_try_rebalance_move()`가 **가장 가벼운(가중부하 기준) bay를 직접 계산해서, 제거된 블록들을 그 bay에만 강제로 순차 배치 시도**(기존 `_candidate_positions`/`_find_earliest_slot`을 그대로 재사용 — 새 지오메트리 로직 없음, 대상 bay 하나로 범위만 제한). 실패(그 bay에 자리 없음)하면 기존 Xpress/그리디 체인으로 자연스럽게 폴백(`xpress_reinsert.reinsert()`의 `None` 계약과 동일 패턴). `mode=="balance"`일 때만 적용, 성공 시 로그에 `via=direct-rebalance`로 표시.
    **정적 검토 중 실제 버그 하나 발견/수정**: `target_bay_id` 계산에 `n_bays`를 썼는데 `_improve` 스코프엔 그 변수가 없어서(`len(bays)`가 맞는 표현) `NameError`가 날 뻔했음 — `import ast`/`inspect`로 실행 없이 문법+와이어링만 확인하는 과정에서 잡아냄. 실행 기반 검증이 아직 전혀 안 됐다는 걸 다시 한번 상기시켜주는 사례.
    **기대치**: Z2를 실제로 겨냥하는 메커니즘은 맞지만, 관측된 인스턴스들의 w2가 4~10 정도로 w1(667~29,630) 대비 작아서 Z2 자체가 크게 줄어도 합산 objective에 대한 기여는 제한적일 수 있음 — "손해는 안 보지만 체감 개선은 미지수"로 기대치를 잡음(기존 ALNS weight EMA가 안 먹히면 자연히 덜 선택되므로 다운사이드는 제한적).
    **주의: 아직 실행 검증 전.** 검증 계획: (a) `via=direct-rebalance` 성공률이 기존 balance 모드 대비 실제로 오르는지, (b) Z2(obj2) 성분이 눈에 띄게 줄어드는지, (c) 그게 합산 objective에도 유의미하게 반영되는지 3단계로 확인 필요.

23. **`analysis/lower_bound.py` 신설 — Z1 이론적 하한선 계산 (2026-07-20)**
    각 블록을 `release_time`에 바로 시작해서 `processing_time`만에 끝낸다고 가정(공간/crane 제약 전부 무시)했을 때의 지연을 합산 — 어떤 알고리즘도 이보다 좋을 수 없는 진짜 하한선. O(n), JSON만 읽으면 되고 실제 배치/Xpress 전혀 안 씀. **로컬 40개 인스턴스 전부 하한선 0으로 확인됨** — 즉 이론적으로 전부 지연 0이 가능한 인스턴스들이고, 지금까지 관측된 모든 Z1>0은 순수하게 알고리즘/스케줄링 비효율 때문이지 인스턴스 자체의 한계가 아님. P3 등 회귀 원인 추적 시 "인스턴스가 원래 안 좋다"는 가능성을 배제하고 알고리즘 쪽에 집중해도 된다는 근거로 사용.

24. **`_top_candidates_for_block`에 dominance/다양성 기반 후보 필터링 추가 (2026-07-20, 미검증)**
    기존엔 `_placement_score` 상위 max_per_block개를 그냥 뽑았는데, 같은 bay·비슷한 시간대의 근접 중복 후보들이 슬롯을 낭비할 수 있음(`xpress_reinsert.reinsert()`의 조인트 MIP 입장에선 후보들이 서로 다른 bay/시간대여야 실제로 충돌 회피 대안이 됨). `(bay_id, entry_time // processing_time)` 버킷 기준으로, 버킷당 최고점 후보를 먼저 채우고(다양성 확보) 남는 슬롯은 점수 순으로 채우는 방식으로 교체. 최고점 후보는 항상 버킷 1번으로 제일 먼저 뽑히므로 기존 최선 후보를 놓치는 일은 없음 — 2번째~N번째 후보의 "다양성"만 개선하는 변경. 격리된 가짜 데이터로 로직 자체는 검증함(최고점 유지, 근접 중복 스킵 확인) — 다만 실제 인스턴스에서 MIP 성공률/objective가 개선되는지는 아직 실행 검증 전.
    **검증 계획**: before/after로 (a) `xpress_reinsert.reinsert()`의 조인트 성공률(`joint_ok`), (b) 최종 objective 비교.

25. **Z1 하한선을 `_improve`에 실제로 연결 — 도달 시 `tardy` 연산자 완전 배제 (2026-07-20, 미검증)**
    23번의 `analysis/lower_bound.py` 계산식을 `greedyalgorithm()` 시작 시 그대로 계산해서 `[Greedy] Weights` 로그 줄에 같이 출력하고, `_improve(..., z1_lower_bound=...)`로 전달. `_improve`는 `best_obj1`(현재 최선 해의 Z1 성분, `check_feasibility` 결과의 `obj1`)을 추적하다가 하한선에 도달하면(`best_obj1 <= z1_lower_bound + 1e-6`) **`tardy`를 `operator_names`에서 완전히 제거**(가중치를 낮추는 게 아니라 아예 못 뽑히게) — 초기화 시점과 매 "NEW BEST" 갱신 시점 둘 다에서 체크. 하한선이 이미 증명된 값이라 이건 추측이 아니라 수학적으로 안전한 가지치기.
    안전성 확인: `operator_names`가 완전히 비어도(예: `z2z3_modes=False`인데 Z1이 이미 하한선) 크래시 안 남 — 안쪽 `while len(tried_this_round) < len(operator_names):`가 길이 0일 때 자연스럽게 0회 반복되고, `any_candidates=False`로 "더 개선할 거 없음" 경로로 정상 종료됨(코드 직접 추적으로 확인, 실행 테스트는 아님).
    **주의: 아직 실행 검증 전.** 검증 계획: (a) `tardy` 배제 로그가 실제로 찍히는지, (b) 배제 이후 라운드 수/다른 연산자 선택 빈도가 늘어나는지, (c) 그게 objective에 실제로 도움되는지(로컬 40개 전부 하한선 0이라 "이미 obj1=0인 인스턴스"에서 특히 눈에 띄어야 함 — 오늘 로그에서 이미 여러 번 obj1=0.0 확인됨, 예: prob_1/5/17).

26. **batched Phase 1을 긴 timelimit(600초)에서 재검증 → 여전히 실패, 확정 폐기 (2026-07-20)**
    18번에서 90초 예산으로 실패했던 게 "시간이 부족해서"일 뿐일 수 있다는 가설을 검증하려고, prob_17(300블록)을 600초(90초의 6.7배)로 serial vs batched 재비교. **결과: batched가 여전히 647배 나쁨**(serial=142,780 obj1=0.0 완벽 vs batched=92,369,807 obj1=9513). Phase 1 로그 확인 결과, 300초까지 배정된 Phase1 예산을 다 써도 75개 배치 중 49개까지만 처리하고 **나머지 40%(120블록)는 여전히 강제배치로 떨어짐** — 즉 "시간을 늘리면 해결된다"는 가설은 명확히 반증됨. 배치당 처리비용이 순수 그리디보다 근본적으로 비싸고, 인스턴스가 혼잡해질수록 그 격차가 더 벌어지는 것으로 보임(선형으로 시간을 늘려도 못 따라잡음). **construction_mode="batched" 및 관련 배치 인프라(`_place_blocks_batched`)는 이제 완전히 폐기 결론 — 코드는 남기되(이미 `"serial"`이 기본값) 더 이상 재시도할 근거 없음.**

27. **Bay 독립 전체-MIP 재배치 probe → 후보 생성 단계에서부터 실패, 폐기 (2026-07-20)**
    "bay별로 feasibility가 독립이니 bay 하나 전체를 조인트 MIP로 정확히 재최적화하면 어떨까"라는 아이디어를 `analysis/bay_mip_probe.py`로 실측. prob_17의 가장 무거운 bay(K=96블록)에 `xpress_reinsert.reinsert()`를 300초 예산으로 직접 호출. **결과: MIP 풀이는 시작도 못 하고 실패** — 로그에 "block 130에서 후보 생성 중 deadline 도달"이라고 찍힘. 96개 블록 각각의 후보 생성(지오메트리 탐색) 단계만으로 300초를 다 써버림, 실제 MIP 최적화(O(K²×후보수²) 충돌제약)는 아예 진입도 못 함. 18번(batched Phase 1)과 **같은 근본 원인**(조인트 MIP 인프라가 큰 K에서 후보생성부터 못 버팀)으로 확정. **결론: 지금 있는 조인트 MIP 도구는 K≤12 정도의 소규모 전용, 대규모 재구성엔 부적합 — bay 독립 방향도 폐기.**

28. **Right Justification 구현 — Left와 번갈아 적용 (2026-07-20)**
    `_find_earliest_slot`을 거울처럼 뒤집은 `_find_latest_slot`(마감 쪽에서부터 최대한 늦은 feasible 슬롯 탐색, 같은 Stage-2/3/4 체크 재사용) 신설. `_right_justify`는 `_left_justify`의 거울 버전인데, **왼쪽과 달리 개별적으로 "더 나은지"를 따지지 않고 그냥 각 bay의 현재 오른쪽 끝(`bay_right_edge`)까지 늦은 순서(늦은 entry부터)로 밀어붙임** — RCPSP 문헌에서 L-R-L 교대 압축이 단일 방향 압축의 local optimum을 벗어나는 표준 기법이라는 데 근거. 오른쪽으로 미는 것 자체는 개별 블록의 tardiness를 늘릴 수 있어서(의도된 것 — 다른 배치 구조를 만들어보는 탐색 단계), **반드시 뒤이어 `_left_justify`를 한 번 더 돌리고, 그 최종 2-패스 결과 전체를 `check_feasibility` 한 번으로 검증해서 원래(right-justify 시작 전)보다 나쁘면 통째로 버림** — 안 그러면 안전하지 않음. `greedyalgorithm()`에 Phase 2.6으로 삽입, `right_justify: bool = True` 파라미터로 토글 가능.
    실행 검증: prob_1(개선 없이 안전하게 버려짐 확인), prob_23(실제 개선: `moved 75+100 block(s) obj 160213346 -> 158315086`) 확인 — 최소 크래시 없이 동작하고 실제 이득 사례도 있음을 확인.

29. **`_improve`에서 KeyError('tardy') 버그 발견/수정 (2026-07-20)**
    28번 검증 중 prob_5/prob_1 조합에서 `KeyError: 'tardy'`로 `baseline_greedy`가 죽어서 emergency fallback으로 떨어지는 걸 발견(원인 재현 스크립트로 전체 traceback 확보). 원인: `tardy` 라운드가 마침 Z1을 하한선까지 도달시키는 바로 그 라운드일 때, "NEW BEST" 로그를 찍기 *전에* 25번의 tardy-제거 로직이 먼저 `op_weight.pop("tardy", ...)`을 실행해버려서, 그 다음 줄의 로그 문자열이 `op_weight[mode]`(mode="tardy")를 읽으려다 KeyError. **수정: 로그 출력을 먼저 하고 tardy 제거를 그 다음으로 순서만 바꿈.** 수정 후 동일 재현 시나리오(prob_1/5/17/23, 재시작 래퍼 조건 그대로) 재검증 완료.
    **교훈**: "실행 안 해보고 정적 검토만으로 검증했다"고 표시해둔 항목들(21/22/24/25)이 오늘 실제로 두 개(24번 n_bays 오타, 25번 이 KeyError)의 진짜 버그를 담고 있었음 — 정적 검토가 일부는 잡아냈지만(24번은 그때 잡음) 전부는 아니었음. 남은 21/22도 아직 실행 검증 전이라는 걸 재상기.

30. **Z1 긴급도 기반 `tardy`/`swap` 연산자 가중치 부스트 추가 (2026-07-20, 미검증)**
    w1이 압도적이고 Z1 하한선이 로컬 40개 전부 0이라는 오늘의 핵심 발견을 살려서, Z1이 아직 하한선에 안 닿았으면(`best_obj1 > z1_lower_bound`) 룰렛휠에서 `tardy`/`swap`의 선택 가중치에 `Z1_URGENCY_BOOST=3.0`을 곱해 더 자주 뽑히게 함(EMA로 추적되는 `op_weight` 자체는 안 건드림, 선택 확률만 일시적으로 부스트). Z1이 하한선에 닿으면 25번 로직이 `tardy`/`swap` 둘 다 완전히 제거하므로 이 부스트는 자연히 무의미해짐 — 서로 자연스럽게 맞물리는 짝 로직. **주의: 아직 실행 검증 전.**

31. **Pairwise Swap 연산자(`mode="swap"`) 신설 — Z1 교착(deadlock) 상황 정면 겨냥 (2026-07-20, 실행은 격리 테스트만)**
    "큰 폭 개선을 하려면 Z1을 더 잡아야 한다"는 오늘의 결론에 따라 세션 초반부터 아이디어 목록에 있던 pairwise swap을 구현. 기존 `tardy` 모드는 지연 블록을 독립적으로 빼서 범용 재삽입 검색에 맡기는데, **두 블록이 서로의 좋은 자리를 막고 있는 교착 상황**은 이 방식으로 잘 안 풀림(하나만 빼면 그 블로커가 여전히 그 자리에 있어서 똑같은 결과가 나올 수 있음). `_select_removal_candidates`에 `"swap"` 모드 신설: 가장 지연이 심한 블록부터, **그 블록의 이상적(제약 없는) 시간창 `[release_time, release_time+processing_time)`과 같은 bay에서 겹치는 다른 블록**을 "블로커"로 찾아서 그 둘을 페어로 반환 — `best_assignments`/`blocks_data`만으로 계산되는 가벼운 로직이라 새 컨텍스트(bay 상태 등) 안 필요함. `operator_names`에 `tardy` 바로 옆에 추가, Z1 하한선 도달 시 `tardy`와 함께 자동 제거됨(30번 부스트 대상에도 포함).
    **부수 수정**: `_improve`의 Xpress 조인트 시도 조건을 `1 < k <= JOINT_MAX_K`(그 라운드에 배정된 명목 k값 기준)에서 `1 < len(remove_ids) <= JOINT_MAX_K`(실제로 반환된 블록 수 기준)로 변경 — 안 그러면 swap이 2개를 반환해도 그 라운드의 k값이 우연히 1이면 조인트 MIP을 건너뛰고 순차 재삽입으로 떨어져서, "맞바꾸기"의 핵심 의미(두 후보를 동시에 고려)가 사라질 뻔했음.
    격리 테스트로 로직 검증(가짜 데이터: 블로커 정확히 찾음, 지연 블록 없으면 빈 리스트 반환 확인) — **실제 인스턴스 실행 테스트는 아직 안 함**, 백그라운드 비교 작업 방해 안 하려고 미룸.

### 2026-07-21

32. **Whole-bay reinsertion을 독립 "Phase 4"에서 Phase 3의 ALNS 연산자(`mode="wholebay"`)로 재통합, 옛 Phase 4 코드 삭제**
    27번(bay 독립 전체-MIP)이 `restrict_bay_id`+`current_positions` 두 가지 수정(같은 세션에서 완료) 이후 실제로 작동함을 `analysis/bay_mip_probe.py`가 검증(prob_40 K=170, ~4초 만에 완료, 순수 Z1 개선 -504,919)한 뒤, 이걸 먼저 "Phase 3 끝나고 남는 시간에만 도는 별도 Phase 4"로 실제 파이프라인(`greedyalgorithm()`)에 붙였었음. 그런데 `phase4_deadline = t_start + timelimit * 0.99`가 Phase 3 자신의 데드라인과 완전히 같은 값이었고, Phase 3는 (오늘까지 관측된 바로는) 거의 항상 자기 데드라인까지 뭔가 계속 시도할 게 남아있어서 스스로 멈추는 일이 없음 — 그 결과 **Phase 4가 남는 시간을 받는 일 자체가 실질적으로 발생하지 않음**을 prob_40(400초)/prob_17(300초) 두 조건 모두에서 확인(`[Greedy] Phase 4` 로그 줄이 한 번도 안 찍힘, TLE는 없었음).
    이 결과를 받고 "정말 목적함수를 개선하려면 이 로직을 Phase 3 안에 넣어야 한다"는 방향으로 전환. `_select_removal_candidates`에 `"wholebay"` 모드 신설(`balance`처럼 `bay_weights[j]*load[j]`가 가장 큰 bay를 고르되, k를 무시하고 그 bay의 블록 전부를 반환) + `_improve`의 라운드 루프에 `mode == "wholebay"`일 때 `xpress_reinsert.reinsert(..., restrict_bay_id=target_bay_id, current_positions=...)`를 직접 호출하는 분기 추가, `operator_names`에 편입(`z2z3_modes` 여부와 무관하게 항상 포함 — 검증된 효과가 순수 Z1 개선이라 `tardy`/`swap`과 같은 카테고리로 취급). 다만 라운드당 비용이 whole-bay MIP 한 번(초 단위)이라 `tardy`/`swap`이 받는 `Z1_URGENCY_BOOST`(룰렛휠 확률 3배)는 **의도적으로 주지 않음** — EMA 가중치가 자연스럽게 효과를 반영하도록 둠. Z1 하한선 도달 시 `tardy`/`swap`처럼 강제 제외하지도 않음(whole-bay 재삽입은 `_placement_score`가 Z2/Z3까지 함께 고려하므로 Z1이 0이어도 여전히 유효할 수 있음).
    이제 쓸모없어진 옛 Phase 4 코드 전부 삭제: `greedyalgorithm()`의 Phase 4 블록, `_whole_bay_reinsert()` 함수, `whole_bay_reinsert` 파라미터, `WHOLE_BAY_MIN_BUDGET` 상수(`WHOLE_BAY_MAX_PER_BLOCK`은 새 wholebay 연산자가 그대로 재사용하므로 유지).
    **실행 검증**: `ast.parse` 문법 체크 통과, `inspect.getsource`로 `"wholebay"`가 `_improve`/`_select_removal_candidates` 양쪽에 들어갔는지와 `_whole_bay_reinsert`/`whole_bay_reinsert` 파라미터가 완전히 제거됐는지 확인. 실제 인스턴스 실행: prob_17(90초) — `mode=wholebay` 라운드가 1회 뽑혀 `via=wholebay-xpress` 경로로 정상 동작(이번엔 개선 없이 rejected, 크래시 없음), feasible 유지. prob_40(180초) — 이번 실행에선 라운드가 3개뿐이라 `wholebay`가 룰렛휠에서 뽑히진 않았지만(다른 3개 연산자만 시도됨) 에러 없이 feasible 유지(250/250 배치, obj1=204761). **아직 안 된 것**: `wholebay`가 실제로 뽑혔을 때 objective가 개선되는 실측 사례는 이번 라운드에서 못 봄(prob_17에서 뽑힌 1건은 rejected) — 다음 검증에서 더 긴 예산/더 큰 인스턴스로 재확인 필요.

33. **prob_1 회귀 원인 규명 -- candidate diversity 필터를 슬롯-비율 방식에서 점수손실 기반 방식으로 재설계 (2026-07-21)**
    `before_submission2`(=`submission-20260720-1937` 태그, 실채점 받은 2차 제출본과 완전 동일 — diff로 확인)를 진짜 baseline으로 놓고 prob_1을 재조사한 결과(그 전까지 "1차 제출본"이라고 착각하고 있던 폴더가 실은 2차 제출본이었음 — non-determinism도 seed 미고정이 아니라 아래 cold-start성 타이밍 민감성 때문이었던 것으로 정정), `blocking_chain`/`z2z3_modes`/`left_justify`/`right_justify`/`z23_relax`(21번 항목, 이번에 토글로 만들어서 직접 A/B — 무관함 확인) 전부 무관함을 하나씩 제거법으로 확인. 2차 제출본 자체를 직접 재실행해서 Phase 3 라운드 로그를 나란히 비교한 결과, **완전히 동일한 5블록 제거 세트([27,46,93,80,91], mode=preference k=5)에 대해 2차 제출본은 조인트 MIP으로 -7,928 개선을 찾았는데 현재 코드는 같은 시도가 오히려 147,978로 악화되어 rejected**됨 — 후보 생성(`_top_candidates_for_block`) 자체가 달라졌다는 확실한 신호.
    범인은 24번 항목의 candidate diversity 필터(`CANDIDATE_DIVERSITY_FILL_FRAC`, 슬롯의 하위 40%를 무조건 다양성 후보로 교체)였음 — `CANDIDATE_DIVERSITY_FILL_FRAC=0.0`(필터 완전 비활성화)으로 몽키패치해서 재현: round 4가 2차 제출본과 **정확히 동일하게** 68,633→60,705(gain=7,928, 같은 블록 세트)를 찾아냄. 이 필터는 애초에 24번 항목에서 이미 한 번 prob_40을 회귀시켜서 "슬롯의 100%→40%만 다양성 할당"으로 완화됐던 이력이 있는데, 40%도 prob_1 같은 케이스엔 여전히 과했던 것.
    **수정**: 슬롯 비율 기반 설계를 폐기하고, `CANDIDATE_DIVERSITY_MAX_DEGRADE_FRAC=0.1`로 **점수 손실 폭 기반** 설계로 교체(`Z23_RELAX_FRAC`와 같은 패턴) — 기본은 항상 순수 top-max_per_block(점수 기준, 아무것도 버리지 않음)이고, 어떤 후보를 다양성 후보로 바꿔치기하는 건 (a) 같은 버킷에 여분의 중복이 있고(그 버킷의 유일한 대표를 절대 안 지움) (b) 교체할 후보의 점수가 **top-N 자체의 점수 범위(최악-최선)의 10% 이내**일 때만 허용. 구현 중 실제 버그 하나 발견/즉시 수정: 처음엔 "top-N의 범위"가 아니라 "전체 무제한 후보 풀의 범위"를 기준으로 삼았다가, 먼 bay의 나쁜 후보 하나 때문에 budget이 오히려 더 헐거워져서 필터가 더 공격적으로 동작하는 역효과 발생(prob_1 재실행 시 183,104로 더 악화됨, 실행 검증 중 즉시 발견) — top-N 자체의 범위로 기준을 좁혀서 수정, 재검증 통과.
    **검증**: (a) 격리 유닛테스트(가짜 데이터, `analysis/` 밖에 임시 스크립트로) — 저렴한 다양성 후보는 포함되고 비싼 이상치는 안 딸려오는지 확인. (b) prob_1 실제 실행(180초) — round 4가 2차 제출본/필터-완전비활성화 두 경우와 **정확히 동일**한 68,633→60,705를 찾음. **남은 격차(60,705 vs 2차 제출본의 41,183)는 별개 원인**(연산자 희석 -- 아래 참고)이라 이 수정의 범위 밖. (c) **아직 안 됨**: 이 필터가 원래 겨냥했던 prob_40에서 재차 회귀하지 않는지는 다음 광범위 비교에서 확인 예정.

34. **연산자 희석 가설 (2026-07-21, 미해결 -- 광범위 비교 후 판단 예정)**
    33번 수정 후에도 prob_1은 60,705에서 멈추고 2차 제출본의 41,183까지는 못 감. 2차 제출본 로그: round 4(68,633→60,705) 이후 round 5(`random k=8`)→59,809, round 6(`preference k=10`, 다른 5블록 조합)→41,183(gain=18,626)로 이어짐. 현재 코드는 round 4 성공 후 가중치 갱신 결과가 달라서(오늘 추가된 `wholebay`/`swap` 연산자가 룰렛휠에 같이 경쟁하고 있음) round 5~10이 2차 제출본과 다른 순서로 흘러가 그 결정적 round 6 조합을 못 찾음. **가설**: Z1=0이라 Phase 3의 유일한 개선 경로가 preference/balance뿐인 소규모 인스턴스에서, 오늘 늘어난 연산자 수(tardy/swap/preference/balance/random/wholebay)가 한정된 라운드 예산을 분산시켜 특정 연산자가 "몰아서" 여러 번 시도할 기회를 줄이는 것으로 추정. **아직 확정 아님** — 인스턴스 하나(prob_1)만으로 성급하게 분기/구조 변경을 하지 말고, 광범위 비교로 이 패턴이 여러 인스턴스에서 재현되는지 먼저 확인하기로 사용자와 합의. 재현되면 인스턴스 속성(크기, Z1=0 여부) 기반 분기보다는, 가능하면 여기서도 메커니즘 기반 조정(예: 연산자 수가 아니라 "라운드가 몇 번이나 남았는지"에 비례해서 룰렛휠 탐색폭을 조절하는 등)을 우선 검토하기로 함.

---

## 3. 시간이 있다면 더 해볼 수 있는 것들

### Phase 1 (초기 배치)
1. ~~NFP(no-fit polygon) 기반 후보 생성~~ → **검증 완료 (2026-07-20), 투자 가치 없음으로 결론.** `analysis/bay_utilization.py`로 prob_1/20/31의 bay별 시간에 따른 점유율(layer-0 footprint area / bay area)을 측정한 결과, **peak 점유율이 최대 50.9%, 대부분 10~40%대, 시간평균은 대부분 한 자리수~10%대**로 나타남 — bay 공간이 거의 항상 남아도는 상태. `shape_irregularity.py`가 확인한 "블록 형상이 바운딩박스의 40% 이상을 낭비한다"는 사실은 맞지만, 애초에 공간이 병목이 아니라서 그 낭비가 실제 배치 실패/지연으로 이어지지 않음. **NFP는 보류/폐기, 병목은 공간이 아니라 시간(스케줄링) 쪽이라는 게 재확인됨.**
2. **ATC lookahead 파라미터(k) 튜닝 또는 인스턴스별 동적 선택**: **진행 순서 합의됨(2026-07-20)** — (a) 넉넉한 예산(10~15분)에서 regret/ATC가 EDD를 실제로 역전하는지 먼저 검증 → (b) 역전 신호가 있으면, `timelimit`을 고정 임계값과 비교하는 대신 **런타임에 짧게(1초 정도) 실측해서 블록당 처리 속도를 추정하고, 그 속도로 전체 timelimit 안에 커버 가능한 비율을 계산해 규칙을 선택**하는 방식으로 구현(bay 밀집도 등에 따라 블록당 비용이 인스턴스마다 달라서 고정 공식보다 실측 기반이 안전함). (a) 결과가 안 좋으면 이 항목 자체를 보류.
3. ~~Regret-insertion 방식으로 교체~~ → **최종 결론: 폐기 (2026-07-20).** 예산 버그(적응형 단계가 폴백을 굶겨서 대량 강제배치로 이어짐) 수정 후 깨끗한 조건(120초, 6개 인스턴스)에서 EDD/ATC/slack과 재비교. 격차는 크게 좁혀졌지만(전엔 최대 2,500배 나빴는데 이번엔 feasible한 경우 13~25% 차이), **6개 중 2개가 여전히 INFEASIBLE, feasible했던 4개에서도 EDD를 한 번도 못 이김.** 안정성 문제까지 있어 더 파고들 가치가 낮다고 판단, 코드는 남겨두되 사용 안 함. `priority_rule` 기본값 EDD 최종 확정.

### Phase 2 (repair)
4. **Pairwise swap 기반 repair**: 지금은 위반 블록을 빼서 다시 넣기만 함. 두 블록을 맞바꾸는 방식도 추가하면 지금 못 푸는 사이클을 풀 수 있을 가능성.
5. ~~Blocking-chain 추적~~ → **구현 완료 (2026-07-20).** `check_feasibility` 위반 메시지에서 피해자+blocker id를 둘 다 뽑아서 repair 대상에 포함. 다만 이건 "위반을 해소하는" 목적만 커버하고, "위반은 없지만 더 나아질 수 있는" 경우는 못 잡음 → 그건 Phase 3의 14번 항목으로 별도 커버(아래).

### Phase 3 (improve / Xpress)
6. **K=1에도 상황에 따라 Xpress 활용**: 지금은 K=1이면 무조건 그리디인데, 후보가 많은 밀집 bay에서는 단일 블록이라도 MIP로 미세 최적화할 여지가 있을 수 있음(다만 이득이 크지 않을 가능성 높음, 우선순위 낮음).
7. **Pairwise swap move를 Phase 3에도 추가**: 지금은 "빼고 다시 넣기"만 있음(K≥2일 땐 Xpress가 조인트로 풀어서 사실상 맞교환도 탐색 범위에 포함되긴 함). 완전히 일반적인 "아무 두 블록이나 바꿔서 개선" 탐색은 아직 없음.
8. **`max_per_block`(현재 8) / `k_values`(현재 1,2,3,5) / `stall_limit` 튜닝**: 인스턴스 크기별로 다르게 가져가면 더 나을 수 있음. 지금은 전 인스턴스 공통값.
9. ~~Simulated annealing으로 확장~~ → **구현 완료 (2026-07-20), 실험적.** `_improve(..., annealing=True)`로 켤 수 있음 — 매 라운드 `current`(walk 상태)와 `best`(지금까지 최선, 항상 feasible)를 분리해서 추적하고, 개선 안 되는 이동도 `exp(-Δ/T)` 확률로 받아들이도록 함(T는 objective 크기에 비례해서 시작, 라운드마다 기하급수적으로 냉각). `best`는 strictly-better일 때만 갱신되므로 최종 반환값이 hill-climbing보다 나빠질 일은 없음. 아직 `annealing=False`(기본값) 대비 실측 비교 안 함.
10. **Gurobi 라이선스 받으면 Xpress와 비교**: 지금은 Xpress만 붙여놨음. 둘 다 붙여서 어느 쪽이 이 크기의 서브문제(K≤5)에서 더 빠른지/좋은지 비교해볼 수 있음(다만 이 정도 규모에선 둘 다 거의 즉시 풀릴 가능성이 높아 차이가 작을 것으로 예상).

### 전체 구조 / 검증
11. **Iterated greedy (재시작)**: tie-break을 랜덤화해서 전체를 여러 번 돌리고 best만 채택. 지금 구조 위에 거의 그대로 얹을 수 있음.
12. **더 크거나 극단적인 인스턴스로 스트레스 테스트**: 지금 검증은 로컬 train 40개(100~300블록) 기준. 히든 인스턴스가 이보다 크거나 특이한 weight 비율일 경우까지 커버되는지는 추가 확인 필요.
13. **서브프로세스 격리 + 하드 킬 방식 검토**: 지금은 cooperative time-check(중간중간 시간 체크)라서, 쪼갤 수 없는 단일 작업(예: `check_feasibility` 한 번 호출)이 아주 느려지면 이론상 몇 초 정도는 timelimit을 넘길 수 있음. 완전히 막으려면 별도 프로세스로 실행하고 강제 종료하는 방식이 필요한데, Windows(개발)/Linux(서버) 환경 차이 때문에 리스크도 같이 커져서 지금은 보류.
14. ~~Z2/Z3까지 포함한 joint 최적화~~ → **구현 완료 (2026-07-20).** `_improve`의 블록 선택 기준을 tardy 외에 preference penalty(Z3), 가장 부하 큰 bay(Z2)로도 주기적으로 순환하도록 확장(`_select_removal_candidates`). sanity test만 통과, 실제 개선 효과는 아직 별도 검증 안 함.

### 검증 관련 교훈 (2026-07-20)
15. **짧은 예산(15~20초)으로 규칙/구조를 비교하면 결과가 오염될 수 있음.** 실제로 원래 "EDD가 ATC/slack을 이긴다"는 결론도 15초·구예산(Phase1 75%) 기준이었는데, 그 조건에서 200블록 인스턴스가 절반 가까이 강제배치(`_force_place`)로 밀려나 있었던 게 뒤늦게 발견됨 — 강제배치는 순서 철학과 무관하게 결과를 깎아먹으므로, 비교 자체가 "어떤 순서가 나은가"가 아니라 "어떤 순서가 시간부족에 덜 취약한가"를 재고 있었을 위험이 있음. **앞으로 우선순위/구조 비교 실험은 최소 2분 이상, 가능하면 실제 대회 시간대(몇 분~30분)에 가깝게 잡고, fallback/forced 카운트를 항상 로그로 남겨서 오염 여부를 바로 확인할 것.**
