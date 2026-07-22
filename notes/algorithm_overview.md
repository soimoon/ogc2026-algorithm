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

35. **연산자 희석 문제 확정 및 근본 수정 -- ALNS 라운드 루프를 "패스당 1회 보장" 방식에서 진짜 가중치 복원추출로 재작성 (2026-07-21)**
    34번 가설이 3차 제출 실채점(P1: 13,310→15,740, +18.3%)에서 로컬과 동일한 패턴으로 재현되면서 확정됨 — 로컬 전용 현상이 아니라 실제 숨겨진 인스턴스에서도 나타나는 실재 문제로 격상. 코드를 다시 보니 `_improve`의 룰렛휠은 겉보기와 달리 **"한 패스(pass) 안에서 활성 연산자 전부가 정확히 1번씩 보장되어 시도"**되는 구조였음(`tried_this_round` 집합으로 소진 관리, 가중치는 그 패스 안의 순서만 정함) — EMA 가중치가 "얼마나 자주 뽑히는지"에는 전혀 영향을 못 주고 있었음. Z1=0이라 활성 연산자가 4개(preference/balance/random/wholebay)뿐인 소규모 인스턴스에서, prob_1의 전체 라운드 수가 10~11개(≈패스 2~3번)뿐이라 wholebay(이 유형엔 거의 항상 무의미)가 preference/balance와 1:1 비율로 기회를 나눠 가지면서, 2차 제출본이 찾았던 연속 성공 체인(round4→round6)을 찾을 기회 자체가 줄어들었던 것.
    **수정**: "패스당 1회 보장" 이중 while 루프를 제거하고, 매 라운드 `operator_names` 전체에 대해 **순수 가중치 기반 복원추출**(고전적 ALNS 룰렛휠, Ropke & Pisinger 방식)로 교체 — 성과 좋은 연산자는 EMA 가중치가 오르면서 실제로 더 자주 재선택되고, 성과 없는 연산자는 자연히 선택 비중이 줄어듦(완전 배제는 아님, 언제든 회복 가능). `stall_limit` 기본값도 "패스" 단위에서 "개별 시도" 단위로 바뀐 것에 맞춰 `len(operator_names)`를 곱해 재조정(패턴/기존 인내심 규모 보존).
    **검증**: 격리 유닛테스트 불필요(로직이 표준 `random.choices` 호출로 단순화됨) — prob_1 직접 실행(180초)에서 **round 5(`preference k=8`)가 66,281→41,183(gain=25,098)을 한 번에 찾아 2차 제출본 값을 정확히 재현**. 40개 전체 로버스트니스(20초) 40/40 통과.

36. **35번 구현 중 2번째 버그 발견/수정 -- "더 이상 개선할 거 없음" 조기종료 조건의 결함 (2026-07-21)**
    35번을 3차 제출본 대비 광범위 비교(before/after)로 검증하던 중, prob_1이 **정확히 Phase 2(repair) 직후 값(68,633)에서 완전히 멈춘** 이상 결과 발견(Phase 3가 아예 아무 개선도 못 찾음) — 직접 검증(41,183)과 모순. 원인: "더 이상 개선할 거 없음"을 판단하려고 넣은 `consecutive_no_candidates` 카운터가 **연산자 종류 구분 없이 "연속으로 빈 후보가 나온 횟수"만** 셌음 — 복원추출 특성상 가중치가 조금만 쏠려도 **같은 연산자가 연속으로 여러 번 뽑힐 수 있는데**, 그 연산자가 마침 일시적으로 후보가 없으면(예: preference penalty>0인 블록이 아직 하나도 없을 때) 다른 연산자는 한 번도 안 뽑혀보고도 카운터가 임계값에 도달해서 Phase 3 전체가 조기 종료됨. **수정**: 단순 카운터 대신 `empty_operators_seen`(set)으로 "마지막 성공 이후 개별적으로 빈 후보를 반환한 연산자 종류"를 추적 — `len(empty_operators_seen) >= len(operator_names)`가 될 때만(즉 현재 활성 연산자 전부가 각각 한 번씩은 확인됐을 때만) "진짜로 없음"으로 판단하도록 수정. 뽑기 순서/가중치 편향과 무관하게 항상 올바른 판정.
    **검증**: prob_1을 `myalgorithm` 경유로 3회 반복 실행 — **3회 전부 정확히 41,183**(분산 0), 68,633 고착 재현 없음 확인. 40개 전체 로버스트니스(20초) 재검증 40/40 통과. 3차 제출본 대비 광범위 비교(8개 인스턴스, 180초, "before"는 재사용): 4승 2무 2소폭패(+1.9~2.7%, 타이밍 노이즈 범위로 추정) — 순이익 확인.
    **교훈**: "조기 종료/정지 조건" 같은 부수적 로직도 가중치 기반 복원추출 환경에서는 직관과 다르게 동작할 수 있음 — 이번에도 실행 기반 광범위 비교가 아니었으면 발견 못 했을 버그.

37. **증분(incremental) feasibility 검증기 신설 -- `feature/incremental-feasibility` 브랜치, 아직 shadow 모드 (2026-07-21)**
    `check_feasibility()`의 Stage 2~4가 사실상 전부 O(bay_size²)(블록마다 같은 bay의 다른 블록 전부를 스캔/전체 쌍을 순회)라는 걸 확인 — `_improve`의 한 라운드는 보통 K(1~20)개만 바꾸는데, 안 바뀐 블록끼리의 쌍까지 매번 처음부터 재검증하는 게 탐색 라운드 수를 근본적으로 제한하는 병목이라는 가설(사용자 제기)을 코드로 확인. `utils.check_feasibility_incremental(prob_info, base_assignments, new_assignments, changed_ids)` 신설 — "현재 상태는 이미 feasible"이라는 불변조건을 활용해서, Stage 1은 바뀐 블록만, Stage 2~4는 "바뀐 블록 × 같은 bay의 (바뀐 블록의 옛/새 시간창과 겹치는) 블록"만 재검사(O(K×n)). Stage 5(순차 재생)는 영향받은 bay만 스코프를 좁혀서 재생하되, "같은 시각 EXIT가 ENTRY보다 먼저"라는 순서 규칙은 (원본처럼 임의의 외부 입력을 검증하는 게 아니라) 이 함수 자체의 정렬로 보장 — 그래서 `_improve` 내부 전용이고 최종 제출 검증에는 절대 쓰면 안 됨(그건 항상 전체 `check_feasibility`로).
    **안전장치(shadow validation)**: infeasible을 feasible로 잘못 판정하면 -1점(코드베이스 최고 위험 지점)이라, 처음부터 증분 검증만 믿지 않음 — `_improve`의 모든 라운드에서 전체 `check_feasibility`(기존, 결정권자)와 증분 검증기를 **둘 다** 돌려서 feasible/stage/objective를 비교하고, 어긋나면 `*** INCREMENTAL-CHECK MISMATCH ***`로 크게 로그를 찍되 실제 accept/reject 결정은 항상 전체 검증 결과만 따름. 이 단계에서는 오히려 계산량이 늘어 더 느림 — 실제 속도 이득은 증분 검증만 신뢰하고 전체 검증을 떼어낸 뒤에나 나옴.
    **검증**: (a) 격리 스크립트로 prob_1/prob_40에서 실제 ALNS 스타일 K블록 제거+재삽입 시나리오 50건(feasible 40건 + 의도적으로 충돌시킨 infeasible 10건, Stage 1/2/4 각각 겨냥) 비교 — **50/50 일치**. (b) 실전 파이프라인에 배선 후 40개 전체 로버스트니스(30초)에서 라운드 107건 shadow 비교 — **107/107 일치**, 40/40 feasible·크래시 0·시간초과 0 그대로 유지. 누적 157건 무결점.
    **다음 단계**: main에는 아직 안 합침(위험한 구조 변경이라 검증된 main과 분리, `feature/incremental-feasibility` 브랜치에만 커밋). 더 오래(180초)/더 많은 인스턴스로 shadow 비교 건수를 계속 쌓아서 확신이 설 때 전체 검증을 떼어내는 전환을 하기로 함 — 사용자와 합의.

38. **증분 검증기 컷오버 -- "거절은 증분만, 채택은 전체 재검증으로 확인" 하이브리드로 전환 (2026-07-21)**
    37번의 shadow 검증이 157/157 무결점으로 쌓인 뒤, `_improve`의 매 라운드 전체 재검증을 떼어내고 실제 속도 이득을 실현. 다만 완전히 증분 검증만 믿는 대신: **거절될 라운드는 증분 검증만 사용**(current_assignments를 안 건드리니 틀려도 기회 하나 놓치는 것뿐, 위험 없음), **채택하는 순간(NEW BEST든 워크 수락이든)만 전체 `check_feasibility`로 재확인**하는 확인 게이트를 둠 — current_assignments는 이후 모든 증분 검증이 "이미 feasible하다"고 믿는 기준점이 되므로, 여기서 잘못되면 오류가 다음 라운드들로 계속 전파됨. 확인 게이트에서 전체 검증과 어긋나면 `*** INCREMENTAL-CHECK MISMATCH at accept gate ***`로 로그 찍고 그 라운드를 안전하게 거절.

39. **candidate 스캔 상한선 도입 -- 4번의 실패 끝에 "bay×orientation 완전 독립 예산 + AABB 순위 매기기"로 확정 (2026-07-21)**
    `xpress_reinsert.reinsert()` 프로파일링 결과 후보생성(`_top_candidates_for_block`)이 라운드 시간의 99%+, 그중에서도 `_find_earliest_slot`이 99.9%를 차지함을 확인(K=2~8 배치가 25~38초, MIP solve 자체는 <0.02초). `_candidate_positions`가 블록 하나당 최대 ~3,756개 위치를 내고 전부 `_find_earliest_slot`으로 검사하는 게 원인. `CANDIDATE_SCAN_CAP` 도입 시도 중 **4번 연속 실패**하며 원인을 하나씩 규명:
    - (a) cap + "이상적 후보(entry=r_time) 찾으면 조기종료": prob_1 obj1이 0→17로 악화. `_placement_score`가 "이 위치가 미래 배치에 미치는 영향"을 못 보는데 조기종료로 비교 폭을 더 좁힌 게 원인.
    - (b) 조기종료 빼고 cap만(전역 예산, 블록 전체 공유): obj1은 안 깨지지만 Repair obj가 68,633→1,666,716로 폭발. 원인: 1순위 선호 bay가 혼잡해서 예산을 혼자 다 써버리면 2순위 bay는 아예 한 번도 못 봄(2순위가 텅 비어서 즉시 배치 가능했을 수도 있는데).
    - (c) bay별 독립 예산 + AABB 하한선 기반 순위 매기기(`_rank_candidates_by_earliest_bound`, 이미 배치된 블록과의 bbox 겹침으로 "이 위치가 최소 언제부터 가능한지" 싸게 추정해서 정렬) 도입: 격리 진단(`diagnose_ranking.py`)으로 순위 매기기 자체는 10/10 샘플에서 정확함을 확인했는데도 여전히 실패 — 이번엔 bay 하나 안에서 **orientation끼리** 예산을 나눠 쓰다가 같은 방식으로 서로를 굶기는 문제 재발(굶기는 문제가 bay 레벨에서 orientation 레벨로 옮겨갔을 뿐).
    - (d) **최종**: bay×orientation 조합마다 **완전히 독립적인** 예산(다른 무엇과도 공유 안 함) + AABB 순위 매기기. 이제 어떤 조합도 서로를 굶길 수 없음. `CANDIDATE_SCAN_CAP=50`으로 검증: prob_1 Repair obj=68,633(정답과 정확히 일치), Phase 3도 정상적으로 개선 라운드를 찾음.
    **교훈**: "후보 개수를 줄이는" 접근은 4번 다 사람이 놓친 미묘한 부작용이 있었음 — 다음 40번 항목의 손실 없는(exact) 최적화가 더 안전하고 효과도 컸음.

40. **`_find_earliest_slot` 공간 사전 필터링 -- 손실 없는(exact) 최적화, 39번보다 안전하고 효과적 (2026-07-21, 사용자 제안)**
    사용자가 "블록과 물리적으로 무관한 먼 블록의 exit_time까지 시간 후보에 다 넣고 있는 거 아니냐"고 지적 — 코드 확인 결과 정확했음: `candidate_entries`가 bay 안 **모든** 블록의 exit_time을 위치와 무관하게 다 모으고 있었음. new_blk의 (x,y,orient_idx)는 함수 호출 내내 고정이므로, "이 기존 블록이 new_blk와 공간적으로 겹칠 수 있는가"(AABB 비교, `check_entry`/`check_exit`가 내부적으로 이미 하는 것과 동일한 판정)는 매 반복마다 다시 계산할 필요 없이 **함수 시작 시 딱 한 번만 계산해서 재사용** 가능. 겹칠 수 없는 블록은 애초에 어느 시점에도 이 위치를 막을 수 없으므로 결과를 전혀 바꾸지 않는(=39번과 달리 손실 없는) 최적화. 같이 확인한 나머지 3개 아이디어: **AABB 1차 선별은 이미 `check_entry`/`check_exit`에 있었음**(utils.py `_bb_overlap`), **조기종료도 이미 있었음**(`_find_earliest_slot`이 첫 유효 후보에서 바로 `return`), **위치 후보 축소는 부분 구현**(모서리 기반이긴 한데 서로 다른 블록의 x/y를 교차곱해서 M×N으로 불어남 — 다음 후보 과제로 남김).
    **검증**: prob_1 Phase 1: 35.9s→15.3s, candidates=: 5~12초→1~4초로 추가 개선, obj1=0 유지. prob_40: 안전하게 유지되지만 이 인스턴스는 효과가 작음(크고 혼잡해서 대부분 블록이 실제로 공간적으로 관련 있음 -- 후보 개수 자체(교차곱 문제)가 더 큰 병목일 가능성).

41. **stall 조건을 라운드 횟수에서 시간 비율로 전환 -- iterated-greedy 재시작이 처음으로 제대로 발동, 극적인 품질 개선 발견 (2026-07-21)**
    39/40번으로 라운드가 빨라지면서, 고정 라운드 수 기반이던 `stall_limit`(개선 없이 N라운드 지나면 조기 종료)이 **시간이 남았는데도 너무 일찍 멈추는** 리스크를 사용자가 지적. `STALL_TIME_FRAC=0.25`로 전환 -- "마지막 개선 이후 Phase 3 전체 예산의 25%가 지나도록 안 나아지면 멈춤" 방식으로, 라운드가 얼마나 빠르든 자동으로 맞춰짐(`stall_limit` 파라미터/계산은 이제 아무도 안 써서 완전히 제거).
    **효과가 예상보다 훨씬 컸음**: `myalgorithm._iterated_greedy`(다른 seed로 재시작, 이미 구현은 되어 있었지만 "Phase 3가 거의 항상 전체 예산을 다 써서 재시작 기회가 실질적으로 없다"고 오늘 초반에 확인했던 그 재시작 기능)가 **이제 실제로 여러 번(최대 5회) 발동**함을 확인. prob_1에서 restart 2(seed=1)가 68,633→8,041로 개선(같은 attempt 1 결과 대비 -88%) -- 서로 다른 construction 순서 섞기(`_perturb_construction_order`)가 완전히 다른 지역 최적점을 찾아낸 것.
    **광범위 검증(대표 8개 인스턴스, 3차 제출본 대비, 180초)**: **8전 8승**, 그중 다수가 자릿수 단위 개선 -- prob_9 -98.5%(67배), prob_20 -98.7%(77배), prob_31 -98.9%(88배), prob_23 -88.9%(9배), prob_40 -71.9%(3.6배), prob_1 -86.8%, prob_17 -19.2%, prob_5 -2.8%. 40개 전체 로버스트니스(20초)도 40/40 feasible·크래시 0·시간초과 0 유지.
    **결론**: 오늘 하루의 변경들이 사슬처럼 이어진 결과 -- 후보생성 속도 개선(39/40번) → 라운드가 빨라짐 + 시간 기준으로 적절히 멈춤(41번) → 재시작이 실제로 여러 번 발동 → 서로 다른 construction 기반을 실제로 탐색 → 훨씬 나은 해 발견. 이 중 하나만 있었다면 이 정도 효과는 안 나왔을 것.

42. **위험 사고 방지: `check_feasibility_incremental`이 잘못 `utils.py`에 들어가 있었던 것을 발견/수정 -- 4차 제출 패키징 직전 (2026-07-21)**
    37번에서 증분 검증기를 만들 때 위치 선정을 잘못해서 `utils.py`에 추가해버렸음. 그런데 `utils.py`는 **채점 서버가 매 실행마다 자기 원본으로 덮어쓰는 파일**(제출 zip에 넣어도 무시됨, 원본은 `baseline/utils.py`에서 확인 가능) -- 부정행위 방지를 위한 장치로, 지금까지의 모든 제출 zip 구성(`myalgorithm.py`+`baseline_greedy.py`+`xpress_reinsert.py`, `utils.py` 미포함)이 우연히 이 구조와 맞아떨어졌던 것뿐이었음. 4차 제출을 패키징하려던 직전에 사용자가 직접 발견 -- 그대로 제출했다면 실채점 시 `from utils import check_feasibility_incremental`이 `ImportError`를 내고, 모든 인스턴스가 `myalgorithm._emergency_fallback`으로 떨어져 사실상 전멸했을 것(오늘 로컬 검증은 전부 로컬의 온전한 `utils.py`를 그대로 썼기 때문에 이 버그를 전혀 못 잡아냈음).
    **정당성 확인**: 이 최적화 자체는 부정행위 방지 장치가 막으려는 종류의 꼼수가 아님 -- (a) 매 채택(accept) 시점마다 원본 `check_feasibility` 전체 검증이 여전히 게이트로 걸려 있고(38번), (b) 최종 반환 솔루션은 항상 `myalgorithm.py`의 verify-before-return 단계에서 원본 `check_feasibility`로 재검증되며, (c) 하네스도 독립적으로 최종 결과를 재검증함. 즉 "내부 탐색 루프를 더 싸게 재검증하는 방법"일 뿐 검증 기준 자체를 바꾸지 않음 -- 다만 이 정당성과 무관하게, **파일 위치 자체가 채점 서버 제약을 어겼으므로 반드시 수정이 필요했음**.
    **수정**: `check_feasibility_incremental` 함수 전체를 `baseline_greedy.py`로 이전(`_improve` 바로 앞, `_select_removal_candidates` 뒤에 배치) -- 이 파일은 매 제출 zip에 항상 포함되는 파일. `utils.py`는 `C:\Users\nympe\Downloads\baseline-latest-oxXm57Lz\ogc2026\baseline\utils.py`(원본)과 `diff`로 완전히 동일함을 확인할 때까지 복원(1440줄, 끝에 빈 줄 1개 포함). `_improve` 내부의 `from utils import check_feasibility, check_feasibility_incremental`을 `from utils import check_feasibility`로 수정(이제 같은 파일 안의 함수라 import 불필요).
    **검증**: `ast.parse` 문법 체크 통과(양쪽 파일), `import baseline_greedy, utils, myalgorithm, xpress_reinsert`로 실제 로드 확인 + `hasattr` 로 함수가 정확히 `baseline_greedy`에만 있고 `utils`엔 없음을 확인. prob_1 스모크 테스트(60초, `myalgorithm.algorithm` 경유, 재시작 포함) -- feasible, obj=68633, `INCREMENTAL-CHECK MISMATCH` 로그 없음, 크래시 없음 -- 이전(버그 있던 상태)과 동일한 동작 확인(로컬은 두 파일 어디에 함수가 있든 코드베이스 합은 동일하므로 당연한 결과지만, import 경로 자체가 깨지지 않았음을 재확인).
    **교훈**: 채점 서버가 특정 파일을 덮어쓴다는 제약은 코드로는 전혀 드러나지 않는 암묵적 규칙이라, "새 함수를 어디에 넣을지" 결정할 때마다 매번 의식적으로 확인해야 함 -- 다음에 새 유틸 함수를 추가할 때는 항상 `myalgorithm.py`/`baseline_greedy.py`/`xpress_reinsert.py` 중 하나에 넣을 것, `utils.py`는 읽기 전용으로 취급.

43. **P4/P5/P6이 4번 제출 내내 큰 이유 진단 -- 합성 스트레스 테스트(prob_40을 2x/4x/8x로 블록 복제)로 Phase 1이 예산의 고정 비율(~60%)을 항상 소진한다는 걸 확인 (2026-07-22)**
    실채점 P4/P5/P6이 매번 수백만~수천만대에서 완만하게만 개선되는 패턴(P1/P3처럼 롤러코스터를 타지 않음)을 근거로, "이 세 문제는 Phase 1+2(구성+repair)만으로 이미 timelimit 대부분을 써버려서 Phase 3(ALNS, 최근 개선이 거의 다 집중된 곳)가 사실상 못 도는 크거나 혼잡한 인스턴스"라는 가설을 세움. 검증을 위해 로컬 최대 인스턴스(`prob_40`, 250블록)의 블록 리스트를 그대로 2배/4배/8배 복제해 같은 bay 용량에 넣은 합성 인스턴스를 생성(`bays`는 그대로 두고 `blocks`만 반복 -- 물리적 혼잡도를 인위적으로 극단화).
    **결과 (`myalgorithm.algorithm` 경유, 60초/300초 둘 다)**: 500블록에서 Phase 1이 예산의 60%(32.5s/54s), 1000블록에서도 60%(34.4s/54s 60초 기준, 162s/270s 300초 기준)를 소진 -- **절대 시간을 5배(60s->300s) 줘도 이 비율이 그대로**였음. 즉 "시간이 부족해서"가 아니라 구조적: `phase1_deadline = t_start + timelimit*0.5`가 인스턴스 난이도와 무관한 고정 비율이기 때문. 그 시점에 실제로 탐색-배치된 건 블록의 10~20%뿐이고 나머지는 전부 강제배치(`fallback` 82~90%)로 떨어짐 -- Phase 3는 라운드당 비용까지 폭증(1000블록에서 라운드 하나에 60초+)해서 5배 시간을 줘도 3~4라운드밖에 못 돎.
    **다음 우선순위로 이어짐**: 이 진단이 아래 44/45번 시도의 근거가 됨.

44. **`_candidate_positions`를 O(m²) 전체 교차곱에서 O(m) corner/extreme-point로 교체 시도 -- 실패, 되돌림 (2026-07-22, 사용자 제안)**
    `_candidate_positions`가 "블록마다 오른쪽 x / 위쪽 y를 모아 전체 교차곱"(O(m²), m=이미 배치된 블록 수)을 만드는 걸 확인 -- `#40`에서 이미 "미해결 TODO"로 남겨둔 문제. 사용자 제안대로 블록 하나당 자기 자신의 (오른쪽 x, 위쪽 y, 오른쪽-위쪽 코너) 3개 점만 내도록(O(m)) 재작성.
    **결과**: `prob_1` 회귀 검증(60초, seed 0/1 동일 조건 `git stash`로 정확 비교) -- objective 68,633->212,775, obj1 0->4로 **명백한 악화**. 원인 추정: 진짜 extreme-point/skyline 이론에서는 "서로 다른 두 블록의 모서리를 조합한 섞인 코너"도 실제 스카이라인이 꺾이는 지점일 수 있는데, 블록별 독립 코너만 내는 방식은 이런 정당한 섞인 코너까지 다 잘라버림. `#39`의 "4번의 실패" 패턴이 다섯 번째로 재현된 셈.
    **되돌림**: 기존 O(m²) 교차곱 유지, 시도와 실패 원인을 함수 docstring에 문서화. **진짜 고치려면** 배치된 블록들이 만드는 전체 윤곽선(skyline)을 추적/갱신하는 정석 알고리즘이 필요 -- 블록별 독립 계산으로는 부족함이 확인됨. 다음 시도 시 이 문서 참고.

45. **`xpress_reinsert.reinsert()`에 same-bay-first fast path 추가 -- 부분 성공, 유지 (2026-07-22)**
    `reinsert()`의 일반(비-wholebay) 호출 경로가 매번 전 bay x 전 orientation을 스캔하는 걸 확인(docstring에 스스로 "no restrict_bay_id for this general call path" 명시) -- 43번 스트레스 테스트에서 블록 2~3개 재배치에 candidate 생성만 6초 이상 걸리는 걸 실측. `current_positions`가 있으면 그 블록의 **현재 bay만** 먼저 검색해서 `max_per_block`(20) quota를 채우면 다른 bay 스캔을 생략하고, quota를 못 채우면(그 bay가 희박함) 기존처럼 전 bay 검색으로 폴백하는 2단계 방식 도입. 이 최적화가 실제로 발동하려면 호출부가 `current_positions`를 넘겨야 하는데, Phase 2 repair(`_repair`, blocking_chain joint reinsert)와 Phase 3 일반 LNS(`_improve`, tardy/swap/preference/random 모드) 둘 다 이 파라미터를 아예 안 넘기고 있었음 -- 두 호출부에 각각 제거 직전 `assignments`/`current_assignments`에서 위치를 스냅샷해서 연결.
    **검증**: `prob_1`(60초) 회귀 없음(objective 68,633/obj1=0 그대로, 여러 차례 재확인). 500블록 스트레스 테스트에서 제거된 블록이 혼잡하지 않은 bay에 있던 라운드는 candidate 생성이 3s대->0.25s로 극적으로 빨라짐; 하필 가장 혼잡한 bay에 있던 라운드는 6.5s->3.8s(~40% 감소)에 그침 -- 혼잡한 bay 자체를 스캔하는 비용(그 bay 점유 수에 비례)은 이 수정으로 해소 안 됨, 44번(스카이라인) 영역이 그 근본 해법.
    **최종 목적함수 영향**: 500/1000블록 스트레스 테스트 모두에서 원본 대비 objective가 노이즈 수준 차이(±0.3% 이내)에 그침 -- 이 수정 단독으로는 P4~6급 인스턴스의 대세를 바꾸지 못함, 다만 안전한 순수 효율 개선이라 유지.

46. **Phase 1 deadline: 속도 기반 조기 포기 시도 -- 역효과 확인, 되돌림 (2026-07-22)**
    43번 진단(Phase 1이 인스턴스 난이도 무관하게 고정 비율만 소진)에 대한 대응으로, Phase 1 초반 배치 속도를 측정해 "이 속도면 deadline까지 못 끝난다"고 판단되면(투사 완료 시간이 남은 예산의 2배 초과) 곧바로 나머지 블록을 강제배치로 넘기고 절약한 시간을 Phase 2/3에 넘기는 조기 포기 로직을 `_place_blocks`에 추가.
    **결과**: 500블록 스트레스 테스트(60초)에서 Phase 1이 실제로 32.5s->9.6s로 확 빨라졌지만, 최종 objective는 오히려 757M->886M로 **악화**. 원인: 조기 포기로 실탐색 배치된 블록이 88->49개로 줄었고, 이 품질 손실이 Phase 3가 추가로 번 시간(~23초, 라운드 1개 분량)으로 회복하기엔 너무 컸음 -- Phase 3 라운드 자체도 혼잡한 인스턴스에선 비싸서(45번 참고) "시간을 더 준다"는 게 생각보다 힘이 약함.
    **되돌림**: 기존 고정 비율(`phase1_deadline = timelimit*0.5`, `phase1_hard_deadline = timelimit*0.6`, `near_done_frac=0.05`) 그대로 유지, 시도와 실패 원인을 `_place_blocks` docstring 근처에 문서화. **교훈**: Phase 1/Phase 3 사이에서 시간을 재배분하는 접근은, 근본 병목(혼잡한 bay 하나의 후보 스캔 비용, 44/45번)을 안 고치는 한 어느 방향으로 재배분해도 순이익이 나기 어려움 -- 다음에 시도한다면 시간 재배분보다 근본 비용 절감(44번의 정석 skyline 알고리즘, 또는 `_find_earliest_slot`/`_rank_candidates_by_earliest_bound`의 O(bay 점유수) 스캔을 공간 인덱스로 대체하는 것)을 먼저 검토할 것.

47. **MaxRects(여유 사각형 추적) 1차 시도 -- 합성 랜덤 테스트는 통과했으나 실전에서 두 가지 다른 실패 모드 발견, 전부 되돌림 (2026-07-22)**
    44번(corner-only)의 실패 이후, "블록별 독립 코너"가 아니라 "실제 빈 공간(free space) 자체"를 추적하는 MaxRects 방식을 시도. 개념: bay를 "채워진 부분"이 아니라 "블록이 들어갈 만큼 큰 최대 여유 사각형들의 목록"으로 추적하고, 각 여유 사각형의 좌하단 코너를 후보로 삼음 -- 서로 다른 두 블록의 모서리가 조합되는 경우도 빈 공간 계산에 자동으로 반영되므로 44번이 놓쳤던 "섞인 코너" 문제를 원천적으로 피함.
    **1단계 검증(합성)**: 기존 O(m²) 교차곱과 정확히 같은 최선의 배치를 찾는지, 랜덤 사각형 패킹 시나리오 32,000+회(참조점 오프셋 lx0/ly0<0 케이스 포함 37,000+회)로 비교 -- **0건 불일치**. 정확성 자체는 확실히 검증됨.
    **2단계 실전 테스트에서 발견된 문제 (a) -- 산개 배치 시 성능 폭주**: 순수 랜덤(비인접) 사각형 배치로 벤치마크하니 free-rectangle 리스트가 기하급수적으로 불어남(m=200에서 4.9초, m=800에서는 사실상 무한 루프급). 원인: 실제 bay 점유 이력은 블록들이 서로 다른 시간대에 같은 자리를 썼다 나가는 식이라 공간적으로 "산개"되어 있을 수 있는데, 이런 입력에서 MaxRects의 사각형 분할이 계속 작은 조각들을 양산함.
    **2단계 실전 테스트에서 발견된 문제 (b) -- Phase 3 후보 다양성 붕괴**: 성능 문제를 일단 제쳐두고 prob_1(60초)에 전체(양쪽 호출부) 적용해보니 objective가 587,937(obj1=18)로 알려진 정답(68,633, obj1=0)보다 8.5배 나쁨. 자세히 보니 raw Phase 1+2 구성 품질은 오히려 더 좋아졌는데(원시 objective ~15M -> 60만대), Phase 3(ALNS, `xpress_reinsert.reinsert()`의 조인트 재배치)가 그 이후로 거의 개선을 못 함 -- 로그에 `n_candidates=[(91, 1), (28, 1)]`처럼 블록당 후보가 1개뿐인 경우가 반복적으로 나타남. **원인**: `_top_candidates_for_block`(xpress_reinsert 경로)의 조인트 MIP는 여러 블록을 동시에 재배치하며 서로 충돌하지 않는 조합을 찾아야 하므로 블록마다 "다양한 대안"이 필요한데, MaxRects는 설계상 "중복 없는 최선의 한 자리"만 주므로 대안이 하나뿐이면 다른 블록과 충돌 시 그냥 막힘 -- Phase 1의 단일 블록 최적 배치에는 정확히 맞는 특성이 조인트 재배치에는 정반대로 작용.
    **시도한 완화책과 결과**:
    - Phase 1(`_place_blocks`, 직접 호출)에만 국한하고 xpress_reinsert는 기존 교차곱 유지 -- prob_1 raw 구성은 개선됐지만(15M -> 60만대) 최종 objective는 여전히 회귀(68,633 -> 291,336, "시작점에 민감한" prob_1의 특성이 다시 발목 잡음), 게다가 500블록 스트레스 테스트에서 Phase 1이 32.5초 그대로(**속도 개선 전혀 없음**) -- 이땐 아직 sliver-pruning(48번) 전이라 문제 (a)가 그대로 남아있었음.
    - xpress_reinsert 쪽에 "Top-K 여유 사각형 + 격자 오프셋(jitter) 다양성 주입"(사용자 제안) 시도 -- 처음엔 top-K를 면적 기준으로 골랐다가 합성 테스트에서 3,034/10,000건 불일치 발견(면적이 아니라 실제 배치 우선순위 기준인 (y,x) 오름차순으로 골라야 함, 수정 후 0/20,000 불일치로 재검증). 그런데도 실전에서는 여전히 `n_candidates=1` 그대로 -- 계측해보니 실제로는 **후보가 0개**였고(로그의 "1"은 별도 안전장치인 "현재 위치 폴백"이었음), **근본 원인**: `_top_candidates_for_block`은 그 bay에 이력상 배치됐던 모든 블록(`placed_in_bay`, 시간 필터링 안 함)을 통째로 넘기는데, MaxRects는 "그 블록들이 전부 동시에 영구 점유 중"이라고 가정하므로 시간대별로 자리를 돌려썼을 뿐인 블록들까지 전부 겹쳐 보여서 빈 공간이 하나도 안 남는 것으로 계산됨. `_place_blocks`가 쓰는 `active_in_bay`(그 블록의 release_time 이후 아직 안 나간 블록만 필터링)와 달리 이 호출부는 이 필터링이 없어서, 파라미터 튜닝으로는 못 고치는 **구조적 불일치**로 확인. (제대로 고치려면 xpress_reinsert가 배치 내 각 블록의 개별 r_time 기준으로 그때그때 다시 필터링하도록 구조를 바꿔야 함 -- 오늘 범위 밖.)
    **결론**: xpress_reinsert 경로는 MaxRects 전면 적용 불가로 확정, 기존 교차곱 유지. Phase 1 전용 적용은 48번에서 sliver-pruning과 함께 재시도.

48. **sliver-pruning(사용자 제안)으로 MaxRects 성능 문제 해결 -- Phase 1 전용 적용으로 x2/x4 스트레스 테스트에서 실측 개선 확인 (2026-07-22)**
    47번(b)의 성능 폭주 원인은 free-rectangle 분할 시 아주 작은("sliver") 조각들이 계속 쌓이는 것 -- 이번에 요청 중인 블록의 (bw, bh)보다 폭이나 높이가 작은 조각은 **생성되는 즉시 버리는** 최적화를 도입(사용자 제안). 분할은 부모보다 절대 커질 수 없으므로, 이번 호출에서 이미 너무 작은 조각은 이후 분할을 거쳐도 영원히 너무 작을 수밖에 없음 -- 이번 호출 결과에 한해 완전히 무손실.
    **성능**: 합성 벤치마크(산개 배치, m=200)에서 4.9초 -> 0.22초로 **22배 개선**, m=800에서도 0.27초로 안정적. 20,000회 합성 재검증에서도 0건 불일치로 정확성 재확인.
    **최종 배치**: `_candidate_positions_maxrects2`(sliver-pruning만 적용, 여유 사각형당 코너 1개)를 `_place_blocks`(Phase 1 직접 호출부, `active_in_bay` 사용)에만 연결. `_top_candidates_for_block`(xpress_reinsert 경로)는 47번 결론대로 기존 `_candidate_positions`(O(m²) 교차곱) 그대로 유지.
    **실측 결과 (60초 기준)**:
    ```
    prob_1 (100블록, 소규모):     objective 68,633 -> 291,336  (회귀, prob_1 특유의 시작점 민감성)
    x2 스트레스 (500블록, 혼잡):  Phase1 32.5s->7.6s, objective 755M -> 423M   (-44%, 대폭 개선)
    x4 스트레스 (1000블록, 혼잡): Phase1 34.4s->28.6s, objective 3,552M -> 2,717M (-23.5%, 개선)
    ```
    목표였던 대형/혼잡 인스턴스(P4/P5/P6과 유사한 성격)에서는 확실한 개선이지만, prob_1처럼 작고 시작점에 민감한 인스턴스는 여전히 손해를 봄 -- 순이익 여부는 40개 로컬 인스턴스 전체(60초, 품질 비교)에 대한 광범위 검증 결과로 판단 필요. **아직 남은 과제**: xpress_reinsert 경로(#47(b))는 여전히 O(m²) 교차곱에 묶여 있어 혼잡한 bay에서 조인트 재배치 후보 생성이 비쌈.

49. **`_improve`의 Z1-하한 도달 시 tardy/swap 영구 제거 버그 수정 (사용자 발견, 2026-07-22)**
    `best_obj1 <= z1_lower_bound`가 되면 `operator_names.remove("tardy")`/`.remove("swap")`으로 **영구** 제거하던 기존 로직에서, 사용자가 논리적 결함을 지적: `best_obj1`은 Z1 단독이 아니라 **현재 최선의 가중합(w1*Z1+w2*Z2+w3*Z3)에 대응하는 Z1 값**을 추적하므로, 이후 라운드가 "Z1을 조금 희생하고 Z2/Z3를 크게 개선"하는 트레이드오프로 새 최선을 찾으면 `best_obj1`이 하한 위로 다시 올라갈 수 있음 -- 그런데 이 시점엔 이미 tardy/swap이 `operator_names`에서 영구히 사라진 뒤라, Z1이 다시 나빠져도 그걸 고칠 유일한 연산자들을 영영 다시 못 씀. 코드 확인 결과 실제로 `best_obj1 = trial_result["obj1"]`이 "새 최선" 갱신마다 매번 덮어써지는 걸 확인, 이론적으로 재현 가능한 버그임을 확정.
    **수정**: 영구 제거(`operator_names.remove()`/`op_weight.pop()`) 대신, 매 라운드 룰렛휠 가중치 계산 시점(`best_obj1 <= z1_lower_bound + 1e-6`)에 tardy/swap 가중치를 **그 라운드에 한해서만** 0으로 만드는 방식으로 교체 -- `operator_names`에는 항상 남아있고, 이후 라운드에 `best_obj1`이 하한 위로 올라가면 즉시 (부스트된) 정상 가중치로 다시 뽑힐 수 있음. 부수 효과: `empty_operators_seen`(#36의 "더 이상 개선 없음" 조기종료 판정)이 `len(operator_names)`(고정) 대신 **그 라운드의 실제 활성 연산자 수**(`active_operator_names`)와 비교하도록 같이 수정 -- 안 그러면 tardy/swap이 가중치 0이라 절대 뽑히지 않는 동안 `empty_operators_seen`에 절대 추가되지 않아 조기종료 판정 자체가 영영 발동 못 하는 2차 버그가 생길 뻔했음(발견 즉시 같이 수정).
    **검증**: import/구문 확인, prob_1 스모크 테스트(크래시 없음, feasible 유지) 확인. 실제 "Z1 재악화 후 회복" 시나리오가 트리거되는 경우는 드물 것으로 예상(w1이 보통 압도적으로 크기 때문)이라 로컬 40개 세트에서 수치 차이가 눈에 띄지 않을 수 있음 -- 다음 40개 전체 검증 때 자연스럽게 같이 반영해서 회귀 없는지 확인할 것.

50. **xpress_reinsert 경로에 배치-단위 시간 필터링 + MaxRects 재도전 (사용자 제안) -- 부분 성공했으나 순이익 없어 되돌림 (2026-07-22)**
    47(b)의 근본 원인(`_top_candidates_for_block`이 시간 미필터링 전체 이력을 넘겨서 MaxRects가 빈 공간을 못 찾음)에 대한 직접 대응: 조인트 재배치 배치(K개 블록) 내 **최소 r_time**을 기준으로 `e_k > 최소 r_time`인 블록만 남기고 필터링 후 MaxRects에 전달하는 로직을 `_top_candidates_for_block`(신규 `min_batch_r_time` 파라미터)과 `xpress_reinsert.reinsert()`에 추가. 배치 전체에 안전한 이유: 최솟값 기준 필터링은 배치의 어떤 멤버 기준으로 봐도 "과소 필터링"이 될 수 없음(더 많이 남기는 방향으로만 치우침).
    **결과**: 의도대로 후보 다양성이 일부 회복됨(`n_candidates` 1개 -> 블록에 따라 2~21개, containment 가지치기 없는 diverse(Top-K+jitter, #47(b)에서 만들어둔 버전) 조합 시 더 크게 회복). 하지만 **최종 결과는 개선되지 않음**: prob_1은 그대로 회귀(582,246, obj1=18)였고, 로그를 보니 21.8초 만에 "개선할 게 없음"으로 조기종료 -- 후보 부족이 아니라 Phase 1 MaxRects가 만든 시작점 자체가 Phase 3 연산자들이 잘 못 빠져나오는 구조라는 걸 재확인. 오히려 500블록 스트레스 테스트에서는 **더 나빠짐**: 423M(Phase 1 전용, 12라운드) -> 463M(이 조합, 7라운드) -- 목표였던 혼잡 인스턴스에서도 순손실.
    **되돌림**: `xpress_reinsert.py`와 `_top_candidates_for_block`의 `min_batch_r_time` 관련 코드 전부 제거, `_top_candidates_for_block`은 항상 기존 `_candidate_positions`(O(m²) 교차곱)만 쓰도록 복원. **교훈**: 근본 원인 진단(시간 미필터링)은 정확했고 수정도 의도대로 작동했지만("증상"은 고쳤음), 그게 실제 목적함수 개선으로 이어지지는 않았음 -- Phase 3의 진짜 병목은 후보 다양성이 아니라 다른 곳(연산자 자체의 지역 탐색 능력, 또는 Phase 1 MaxRects 구성이 만드는 지역 최적점의 구조)에 있을 가능성.

51. **최종 해법: 블록 개수(N) 기준 적응형 Phase 1 엔진 스위칭 (사용자 제안) (2026-07-22)**
    48/50번까지의 결론: MaxRects는 대형/혼잡 인스턴스(500~1000블록)에 확실히 좋고 소형/민감 인스턴스(prob_1, 100블록)에는 나쁨 -- 어느 한쪽에 억지로 맞추기보다, **인스턴스 크기로 미리 분기**하는 게 유일하게 검증된 두 극단 모두에서 안전한 선택. `greedyalgorithm` 진입 시 `n_blocks`로 한 번만 결정(`use_maxrects = n_blocks >= MAXRECTS_MIN_BLOCKS`)하고, `_place_blocks`/`_place_blocks_batched`/`_repair`/`_improve` 전체에 새 `use_maxrects` 파라미터로 일관되게 threading(각 함수 내부의 `_place_blocks` 재귀/위임 호출에도 전달) -- 한 인스턴스 안에서 Phase 1 구성과 Phase 2 repair가 같은 엔진을 쓰도록 보장.
    **임계값**: `MAXRECTS_MIN_BLOCKS = 300`. 검증된 데이터는 딱 두 지점뿐(N=100 회귀, N=500/1000 대폭 개선) -- 로컬 학습 인스턴스가 최대 N=300(prob_20)까지밖에 없어 그 이상 구간은 로컬로 검증 불가능. 300은 로컬로 검증 가능한 범위(~300)의 상한선이자, 미검증 구간(300~500)에서는 안전한(기존 방식) 쪽으로 보수적으로 잡은 값 -- 다음 40개 전체 검증에서 prob_20(N=300, 경계값)이 실제로 어느 쪽이 나은지 데이터가 쌓이면 재조정할 것.
    **검증**: prob_1(N=100, 60초) 정확히 68,633(obj1=0)으로 복원 확인 -- 기존 방식과 100% 동일 동작. x2 스트레스(N=500, 60초) Phase 1 7.56s, objective 427.7M -- 48번에서 검증한 423M과 오차범위 내로 일치, MaxRects 이득 그대로 유지. 두 극단 모두 의도대로 작동함을 확인.
    **다음 단계**: 40개 로컬 인스턴스 전체(60초) 재검증을 다음 세션으로 이월(사용자 합의) -- prob_1~19/21~40(전부 N<300)은 기존 방식으로 돌아가 회귀 없이 원래 좋았던 값을 회복할 것으로 예상, prob_20(N=300)만 MaxRects 경로를 타므로 그 결과가 임계값 재조정의 핵심 데이터가 될 것. 밀집도(density) 기반 보조 기준은 이번엔 보류 -- 블록 개수만으로 우선 검증.


52. **prob_1의 "진짜 최선"은 68,633이 아니라 8,041 -- 60초 비교가 만든 착시 확인, STALL_TIME_FRAC에 절대시간 상한 추가 (사용자 제안) (2026-07-22)**
    사용자가 4차 제출 전 로컬 검증(#41)에서 prob_1이 8,041까지 나온 기록을 떠올리며 오늘 계속 68,633을 "정답"처럼 취급한 게 맞는지 질문 -- 실제로 180초로 재검증하니 `restart 2(seed=1)`가 정확히 8,041을 재현함(60초로는 재현 안 됨). 원인: `_iterated_greedy`는 남은 예산을 다음 재시작에 그대로 넘기는 구조라, 같은 "restart 2, seed=1"이라도 총예산이 클수록 그 재시작이 받는 시간이 커짐 -- 60초 예산에서는 8,041로 이어지는 탐색 경로를 끝까지 못 밟음. **오늘 세션 내내 60초로 비교한 수치들(#48/#51의 prob_1 68,633 등)은 서로 비교하는 용도로는 유효하지만, "prob_1의 최선"으로 취급하면 안 됨** -- 실제 채점 서버 timelimit(몇 분~30분)에서는 더 낮은 값이 나올 수 있음.
    이어서 사용자가 지목한 구조적 문제: `STALL_TIME_FRAC=0.25`가 Phase 3 남은 예산의 **비율**로만 정체 판정 대기시간을 정하므로, 60초에서는 대기시간이 ~13.5초로 적당하지만 히든 인스턴스가 600초를 받으면 대기시간이 135초까지 늘어남 -- ALNS가 일찍 수렴해버리면 135초 동안 무의미한 룰렛휠만 돌리다 재시작하게 돼서, 정확히 8,041을 찾은 것과 같은 종류의 재시작 기회를 오히려 줄임.
    **수정**: `stall_threshold = min(STALL_TIME_FRAC * (deadline - phase3_loop_start), STALL_TIME_CAP)`, `STALL_TIME_CAP = 15.0` -- 세 곳의 중복된 정체 판정 조건문을 이 값 하나로 통일. 작은 예산(비율 값이 이미 15초 미만)은 기존과 동일 동작, 큰 예산에서만 절대 상한이 걸림.
    **검증**: prob_1 60초 -- 68,633 그대로(무변화 확인, 13.5<15라 원래도 캡에 안 걸림). prob_1 300초 -- 이전(캡 없음, 180초 기준) 4회였던 재시작이 **5회 전부** 돎(`_RESTART_MAX_ATTEMPTS` 상한까지 도달), restart 2가 8,041 재발견 + restart 4/5가 추가로 다른 basin(113,874 / 53,790) 탐색 -- 의도대로 재시작 빈도가 늘어남 확인. x2 스트레스(500블록, 60초) -- 427.7M 그대로(무변화, 이 실행에서는 애초에 정체가 발동할 만큼 라운드가 정체되지 않아 캡이 트리거 안 됨) -- 혼잡 인스턴스에서 부작용 없음도 확인.
    **참고로 확인한 것**: 사용자가 별도로 제안한 "O(m²) 교차곱의 상수를 블록별 X_max/Y_max 하나씩만 써서 줄이자"는 아이디어는 코드 확인 결과 **이미 그렇게 구현되어 있었음**(`Block.bounding_rect()`가 애초에 레이어 전체를 포괄하는 단일 AABB 하나만 반환, `_candidate_positions`도 블록당 X_max/Y_max 하나씩만 xs/ys에 추가) -- 상수는 이미 최소치라 추가 여지 없음, m² 크기 자체를 줄이려면 여전히 MaxRects류의 다른 알고리즘이 필요.

53. **STALL_TIME_CAP에 최소 라운드 수 AND 조건 추가 -- 절대시간 상한만으로는 위험하다는 지적 (사용자 제안) (2026-07-22)**
    52번의 `STALL_TIME_CAP=15.0`이 큰 예산에서의 낭비는 막지만, 반대로 **히든 인스턴스가 로컬 테스트보다 훨씬 혼잡하면 라운드 하나가 15초를 넘게 걸릴 수 있다**는 위험을 사용자가 지적 -- 실제로 오늘 500/1000블록 합성 스트레스 테스트에서 라운드 하나가 수십 초 걸리는 걸 이미 관측한 바 있음(예: x4 스트레스에서 K=100 배치 라운드). 이 경우 시간 캡 단독으로는 "느린 라운드 1개"를 "정체"로 오판해서 조기 재시작을 유발할 위험이 있음.
    **수정**: `rounds_since_last_improve`(매 라운드 시도마다 +1, 새 최선 갱신 시에만 0으로 리셋 -- 빈 후보/기각/워크 모두 카운트)를 새로 추적하고, 정체 판정을 `시간 조건 AND 최소 라운드 수(MIN_ROUNDS_SINCE_IMPROVE=5) 조건`으로 변경(기존 `stalled` 카운터는 "워크 스텝은 카운트 안 함"이라는 별개 목적이 있어 재사용하지 않고 새 변수 도입). 즉 15초가 지났어도 그 사이 시도한 라운드가 5번 미만이면 아직 재시작하지 않음 -- 라운드가 비싼 인스턴스일수록 자연히 더 오래 버팀.
    **검증**: prob_1 300초 -- 8,041 재발견 그대로, 재시작 5회 전부 유지("no new best for N rounds" 로그가 전부 N≥5로 조건 충족 확인). x2 스트레스 60초 -- 427.7M 그대로 무변화.

54. **xpress_reinsert: swap 데드락 해소를 위한 "상대방 현재 좌표" 후보 강제 주입 + AABB 크기 사전 필터 (사용자 제안) (2026-07-22)**
    사용자 문제 제기: 혼잡한 인스턴스의 Z1 지연 상당수는 "서로 자리를 막고 있는" 두 블록의 진짜 맞교환(swap)으로만 풀리는데, 각 블록의 후보 리스트는 독립적으로 랭킹·컷오프(`max_per_block`, 보통 20)되므로 "상대방의 정확한 현재 좌표"가 그 컷오프 안에 안 들어오면 -- swap이 바로 그 두 블록을 "서로를 막고 있어서" 골랐다는 사실과 무관하게 -- 조인트 MIP는 그 교환 조합 자체를 표현할 방법이 없음.
    **1차 구현**: `_cross_position_candidate()` 신설 -- 블록 bi가 배치 내 다른 블록 bj의 현재 (bay_id, x, y)를 bi 자신의 orientation/processing_time으로 점유하는 후보를 강제 추가. `_find_earliest_slot` 같은 정밀 탐색 없이 저렴하게(entry=max(r_time, bj의 기존 entry), exit=entry+bi의 proc) 생성 -- 이 코드베이스의 "후보는 제안일 뿐, 진짜 검증은 항상 다운스트림(pairwise conflict + 최종 check_feasibility)에서" 원칙 그대로 적용해 안전함. wholebay(K가 수십~백 단위)는 이미 자기 몫의 fallback(`_current_position_candidate`)이 있고 O(K²) 비용이 부담스러워 `CROSS_INJECT_MAX_K=5` 이하 배치에만 적용(swap은 항상 정확히 K=2).
    **1차 검증 결과 -- 순손실**: prob_1 60초는 무변화(68,633)했지만, x2 스트레스(60초)가 427.7M -> 436.9M로 소폭 악화, prob_1 300초는 재시작이 5회 -> 4회로 줄고 restart 4 결과도 나빠짐(113,874 -> 6,821,738) -- 매 라운드 O(K) 후보/MIP 변수를 추가로 만드는 상시 비용이, 드물게만 필요한 이득보다 커 보임.
    **2차 수정(사용자 제안) -- AABB 크기 사전 필터**: bi를 bj의 자리에 주입하기 전에, `_block_bbox`로 얻은 두 블록의 폭/높이를 비교해 `bi가 bj보다 CROSS_INJECT_SIZE_SLACK(=1.3)배 이상 크면 애초에 후보 생성 자체를 건너뜀` -- 명백히 안 맞을 조합에 대한 후보/MIP 변수 생성 비용 자체를 없앰. 정확성 게이트가 아니라 순수 비용 절감용이라 여유(slack)를 넉넉하게 잡음(false negative가 나도 최적화 기회 하나를 놓칠 뿐, 틀린 답이 나올 위험은 없음).
    **2차 검증**: x2 스트레스(60초) 430.2M -- 원래 기준(427.7M)에 근접, 라운드 수도 12개로 회복(1차 시도의 8개보다 훨씬 나음). prob_1 300초 -- 재시작 5회, 8,041 재발견, restart 4/5 중간값(113,874 / 53,790)까지 오늘 이전 검증과 **완전히 동일** -- 오버헤드가 사실상 해소됨.
    **남은 불확실성**: swap 데드락이 실제로 이 주입 덕분에 풀리는 케이스가 로컬 테스트에서 명확히 관측되진 않음(현재까지의 테스트는 "회귀 없음"만 확인, "이걸로 뭔가 새로 풀렸다"는 직접 증거는 아직 없음) -- 혼잡도가 더 높은 인스턴스(x4 이상, 혹은 실제 P4~6)에서 효과가 나타날 가능성.

55. **`_force_place`를 "bay 전체가 빔" 대신 "AABB 겹치는 블록만 나가면 됨"으로 경량화 (사용자 제안) (2026-07-22)**
    사용자 문제 제기: P4~6 같은 히든 인스턴스는 로컬 40개보다 훨씬 혼잡할 수 있는데, Phase 1 deadline(`timelimit*0.5`)이 `time.time()` 기반이라 채점 서버의 CPU 성능/부하에 따라 "몇 개 블록이 제대로 탐색되고 몇 개가 강제배치로 떨어지는지"가 로컬과 다르게 나올 수 있음 -- 이때 강제배치(`_force_place`)로 떨어지는 블록이 많을수록 그 품질이 전체 결과를 크게 좌우함.
    **기존 문제**: `_force_place`가 쓰던 `_empty_bay_entry`는 그 bay가 **완전히 빌 때까지** 기다림(due_date 무시, 공간적으로 전혀 무관한 블록까지 다 나갈 때까지 대기) -- 코드 자체는 랜덤이 아니라 결정론적이지만, 품질이 과도하게 보수적이라 강제배치된 블록마다 불필요하게 큰 지연을 만듦.
    **수정**: `_aabb_gap_entry` 신설 -- 새 블록의 world AABB와 **실제로 겹치는** 블록들의 (기존 `_bb_overlap` 재사용) 최대 `exit_time` 직후에만 진입하도록 완화. **크레인 제약까지 안전한 이유**: AABB가 안 겹치는 두 블록은 실제 폴리곤(AABB의 부분집합)도 절대 겹칠 수 없으므로, "AABB가 겹치는 블록이 전부 나간 뒤"에 들어가면 그 순간 충돌 가능한 블록이 하나도 없음 -- `check_entry`/`check_exit`의 same-or-higher-level 검사까지 자동으로 통과. 기존의 "완전히 빔" 보장과 동일한 안전성 논증을 좁혀서 재사용한 것뿐이라 정확성 손실 없음. `_force_place`에 `bay_placed` 파라미터 추가(AABB 조회용), 유일한 호출부(`_place_blocks`, deadline 강제배치·repair 사이클 차단 양쪽 다 재사용) 업데이트.
    **검증**: prob_1(60초) 68,633 무변화(회귀 없음). x2 스트레스(60초) 433.4M, x4 스트레스(60초) 2,713M -- 둘 다 기존 기준(427~430M / 2,717M)과 노이즈 범위 내로 거의 동일, 극적인 개선은 아직 안 보임. 추정 원인: 오늘 스트레스 테스트가 특정 bay(bay2)를 인위적으로 2~4배 과밀하게 만들어서, 그 bay 안에서는 "AABB 겹침만 기다림"과 "전체가 빔"의 실질적 차이가 크지 않을 수 있음(이미 거의 항상 뭔가와 겹칠 만큼 빽빽함) -- 실제 P4~6이 이 정도로 극단적이지 않고 "그냥 크기만 큰" 경우라면 더 뚜렷한 효과가 있을 수 있음. 로직 자체는 증명 가능하게 안전하므로 극적 개선이 안 보여도 유지.

56. **ALNS 룰렛휠 가중치 붕괴(Weight Collapse) 버그 수정 -- 하한선 도입 (사용자 발견) (2026-07-22)**
    사용자 문제 제기: `op_weight[mode] = WEIGHT_DECAY*w + (1-WEIGHT_DECAY)*REWARD`(`WEIGHT_DECAY=0.8`)에서 어떤 연산자가 계속 거절(REWARD_REJECTED=0.0)만 당하면 `w_{n+1}=0.8*w_n`인 순수 기하급수 감소이고 하한이 전혀 없음 -- 계산해보면 연속 거절 약 3,339회면 `0.8^n`이 float64 최소 양수값 아래로 내려가 **실제로 0.0에 언더플로우**됨. 만약 모든 연산자가 동시에 이 상태에 도달하면 `random.choices(operator_names, weights=[0,0,...])`가 `ValueError`를 던지는데, 이건 "Algorithm raised an exception"으로 채점되어 **-1점**(크래시와 동일 취급). 코드 주석에 이미 적혀있던 설계 의도("성과 없는 연산자는... 완전 배제는 아님, 언제든 회복 가능")를 실제 수식이 보장하지 못하고 있었던 진짜 버그.
    **수정**: `MIN_WEIGHT=0.01` 도입, 5곳의 모든 `op_weight[mode] = ...` 갱신에 `max(MIN_WEIGHT, ...)`로 하한 적용(거절뿐 아니라 new-best/accepted 갱신에도 방어적으로 동일 적용). 히든 인스턴스가 특이해서 수백~수천 라운드 연속 실패하는 극단적 상황에서도 크래시 없이 항상 회복 가능하도록 보장.
    **검증**: prob_1(60초) 무변화 확인(정상적인 실행에서는 애초에 이 하한에 걸릴 만큼 극단적인 연속 거절이 안 생겨서 동작 변화 없음 -- 이 수정은 "정상 케이스 개선"이 아니라 "극단적 엣지케이스에서의 크래시 방지"가 목적).

57. **Phase 1 MaxRects: 블록을 면적 내림차순으로 넣어 파편화 감소 + `active_in_bay`/정렬을 orientation 루프 밖으로 끌어올림 (사용자 제안 + 성능 버그 수정) (2026-07-22)**
    사용자 제안(persistent MaxRects 논의 중 파생): `_candidate_positions_maxrects2`가 블록을 처음부터 재생(replay)할 때, 큰 블록부터 먼저 넣으면 여유 공간이 초반에 몇 개의 큰 덩어리로 나뉘고, 이후 작은 블록들은 대부분 그 덩어리 하나에 통째로 들어가서(다른 여유 사각형들과는 애초에 안 겹쳐 overlap 체크에서 바로 스킵됨) 파편(fragment) 수 자체가 줄어듦 -- 최종 결과는 삽입 순서와 무관(#47/#48에서 이미 증명)하지만 **중간 파편 수는 순서에 따라 달라짐**, 이게 매 호출의 실제 비용을 좌우함. `active_in_bay`를 면적 내림차순으로 정렬해서 전달하도록 변경.
    **부수적으로 발견한 성능 버그**: `active_in_bay` 계산과 신규 정렬 로직이 orientation 루프 **안쪽**에 있었는데, 이 필터(`e_k > r_time`)와 정렬 모두 orientation과 무관해서 매번 똑같은 리스트를 orientation 수(최대 8)만큼 중복 계산/재정렬하고 있었음. 게다가 정렬 키가 `b.bounding_rect()`를 블록 하나당 4번씩 호출(`bounding_rect()`는 O(1)이 아니라 매번 전체 정점 집합에서 재계산 -- `utils.py` docstring 확인). bay 레벨로 끌어올리고(orientation 루프 밖, bay당 1회만) `_block_area()` 헬퍼로 `bounding_rect()` 호출을 블록당 1회로 줄임.
    **함께 도입한 `MIN_WIDTH`/`MIN_HEIGHT`**: persistent MaxRects(다음 세션 예정) 논의에서 나온 아이디어 -- `_place_blocks` 진입 시 그 호출이 다룰 블록들의 전 orientation 중 최소 폭/높이를 한 번만 계산해서 sliver-pruning에 추가 전달. **현재(from-scratch 재계산) 구조에서는 사실상 no-op**(이번 호출 블록 자신의 크기가 항상 전역 최솟값보다 크거나 같아서 기존 기준이 이미 더 타이트함) -- 그래도 나중에 상태가 persistent로 바뀌면 그때는 "한 번 버린 조각은 못 돌아온다"는 이유로 전역 기준이 필수가 되므로 미리 배선해둠.
    **검증 (60초 기준)**: 처음 정렬만 추가했을 때는 x2가 421.6M로 개선됐지만 x4는 오히려 2,750M로 악화(정렬 자체의 중복 호출 비용이 커진 인스턴스일수록 더 부각됨) -- orientation-loop 밖으로 끌어올리고 `bounding_rect()` 중복 호출을 없앤 뒤 재검증하니:
    ```
    x2 (500블록): Phase1 12.12s->5.12s, objective 421.6M -> 412.4M  (오늘 최고 기록)
    x4 (1000블록): Phase1 32.61s->20.58s, objective 2,750M -> 2,704M (원래 기준 2,713~2,717M보다도 개선)
    prob_1 (100블록, N<300이라 이 경로 자체를 안 탐): 8,041 무변화
    ```
    오늘 도입한 Phase 1 MaxRects(#51) 대비 추가로 명확한 순이익 확인.

58. **Phase 1 "deadline 절벽" 버그 발견/수정 (사용자 발견, 2026-07-22) -- prob_10 27배 회귀의 진짜 원인**
    43~57번 변경사항 전체(692f3cb 커밋 시점)를 4차 제출본과 40개 로컬 인스턴스(60초)로 A/B 비교(`analysis/before_after_compare.py`)한 결과 **23승 14패 3무**로 깨끗한 승리가 아니었음. 그중 `prob_10`이 196,955 -> 5,413,025로 **~27배 악화**된 게 가장 심각했는데, 블록 수 확인 결과 14개 회귀 인스턴스 전부 300블록 미만이라 **MaxRects(#51)와는 무관**함이 먼저 확인됨.
    **근본 원인 추적**: `xpress_reinsert`의 same-bay-first fast path(#45)를 임시로 꺼봐도 회귀가 안 고쳐짐(오히려 110,439,304로 더 나쁨) -- 범인이 아님. 대신 같은 코드·같은 `seed=0`로 prob_10을 반복 실행하니 objective가 200,567/5,413,025/14,639,014로 **자릿수 단위로 요동**침을 발견. Phase 1 진행 로그를 rank별로 대조한 결과, **rank 140까지는 세 번 다 완전히 동일한 배치**(블록/좌표/시간 전부 일치)였는데, 이후부터 wall-clock elapsed가 실행마다 8초 가까이 벌어지면서(같은 코드, 같은 입력, 순수 머신 타이밍 노이즈) 느린 실행은 `phase1_deadline`(=`timelimit*0.5`)을 rank~140-160 사이에서 넘겨버렸고, 그 순간부터 나머지 전체(200개 중 40개, 20%)가 **탐색을 한 번도 안 해보고 그냥 `_force_place`로 강제배치**됨.
    코드 확인 결과 이 전환이 완전한 이진(binary) 스위치였음: `used_forced = bi in forced_ids or past_deadline` (`baseline_greedy.py`, `_place_blocks`) -- `time.time()`이 deadline을 넘는 순간부터 이후 모든 블록이 탐색 0회로 강제배치행. 기존에 있던 `hard_deadline`/`near_done_frac=0.05` 구제책(#(2026-07-20) 항목)은 "남은 블록이 전체의 5% 이하"일 때만 발동해서, 이번처럼 20%가 한꺼번에 걸리는 경우는 구제 못 함.
    **수정**: `deadline`을 넘는 순간 "탐색 0 vs 탐색 풀full"이라는 이진 전환 대신, `deadline`~`hard_deadline` 사이를 "저비용이지만 0은 아닌 탐색" 구간으로 재정의. 새 상수 `OVERTIME_SCAN_CAP=5`(기존 `CANDIDATE_SCAN_CAP=50`보다 훨씬 작음)와 이 구간에서 최우선순위 bay 1개로만 `bay_order`를 제한하는 로직을 추가(O(m²) 후보 생성 자체가 bay당 1회씩이라 scan_budget만 줄여선 비용이 안 줄었을 것). `near_done_frac`(남은 블록 비율 기준 게이트)은 완전히 제거하고, 대신 **남은 블록 수와 무관하게** deadline을 넘긴 모든 블록이 이 축소 탐색을 거치도록 일반화(`hard_deadline`이 `None`인 호출부는 기존과 100% 동일하게 즉시 강제배치, 하위호환).
    **`_repair`(Phase 2)에도 같은 절벽이 있었음**: `_place_blocks` 호출부 6곳 감사 결과, Phase 1 메인 경로 1곳만 `hard_deadline`을 받고 있었음. 그중 `_repair`의 순차 반복 루프(활성 기본 경로, 모든 인스턴스가 거침)는 자체적으로 `if time.time()-t_start > timelimit*0.75: forced_ids.add(bi)`라는 **더 이른** 수동 강제배치 트리거를 갖고 있어서, 여기에 `hard_deadline=timelimit*0.85`만 추가해선 무용지물(`forced_ids` 멤버십이 `_place_blocks`의 자체 deadline 판단보다 먼저 체크됨) -- 0.75 임계값 자체를 0.85로 올려서 새 `hard_deadline`과 정합시킴. `regret`/`batched` 우선순위 경로(`:1422`, `:2216`)도 같은 결함이 있으나 둘 다 폐기된 비활성 경로라 이번엔 보류.
    **검증**: (a) prob_10을 fix 후 3회 반복 -> 199,013으로 전부 수렴(이전엔 200,567~14,639,014로 요동, `before`=196,955와 사실상 동률). (b) prob_39(250블록, `to_repair` 162/250이 overtime 구간에 걸림)로 `_repair` fix 확인 -> fix 전 312,642,282에서 fix 후 64,998,998로 **~5배 개선**, feasible 유지. (c) 회귀/개선/동률 섞인 11개 대표 인스턴스로 재비교 -> **10승 1패**(prob_1만 패, 아래 참고). (d) 40개 전체 20초 초단기 예산 로버스트 테스트 -> **40/40 feasible, 크래시/infeasible/시간초과 0건** (`analysis/robustness_check.py`).
    **prob_1의 "회귀" 재확인**: 11개 재비교에서 `prob_1`이 68,633 -> 580,714로 악화된 것처럼 보였으나, 같은 fix된 코드로 8회 반복 실행한 결과 **restart 1(seed=0)은 8번 모두 정확히 68,633으로 완전히 결정론적**이었고, 최종값은 68,633(2회, restart 2가 개선 못 함) / 8,041(3회, `_iterated_greedy`가 유지하는 "최선만 채택" 로직 덕분에 이게 바로 그 유명한 "prob_1의 진짜 최선") / 12,551 / 15,969(2회)로 **68,633보다 나빠진 적이 단 한 번도 없음** -- 580,714는 11개 배치 실행 중 우연히 그 순간 머신에 걸린 다른 부하 때문으로 추정(격리 재현 8회 중 재현 안 됨), 진짜 회귀가 아니라 노이즈로 결론.
    **남은 리스크**: `_improve`(Phase 3)의 greedy 폴백(K≤12, xpress 실패시만 발동)도 `hard_deadline` 없이 `_place_blocks`를 호출하지만, 이미 Phase 3 자체 예산 끝자락이라 여기 확장하면 오히려 전체 timelimit을 넘길 위험(TLE)이 있어 일부러 손 안 댐 -- 리스크로만 남겨둠. `regret`/`batched` 경로도 마찬가지로 미해결.

59. **5차 제출 실채점 회귀(P1/P3 악화, P4~6 고착) 조사 중 발견한 5개의 추가 "#58과 같은 종류" 병목 전부 수정 + emergency fallback 위험 직접 실증/차단 (2026-07-22)**
    5차 제출(위 #58 포함)이 실채점에서 P2만 그대로고 나머지는 전부 악화/무변화(P4=10.09M/P5=21.27M/P6=52.27M, 4차와 거의 동일)로 나오자, 사용자가 5가지 가설(same-bay-first의 bay 탈출 불가, Phase1+2가 예산을 다 먹음, `JOINT_MAX_K` 조인트 재배치 비용, 위 둘의 공통 근본원인, wholebay 후보생성 비용)을 제기. 검증을 위해 `prob_40`을 x4/x8/x16(1000/2000/4000블록)으로 복제한 합성 스트레스 인스턴스로 실측한 결과, **#58과 똑같은 "unbounded O(m²)/O(N) 스캔" 패턴이 최소 4곳 더 있었음**(전부 오늘 세션 안에서 순차 발견/수정):
    - **`_force_place`의 `_aabb_gap_entry`**: AABB 겹치는 블록의 `max(exit_time)`을 구하려고 매번 전체를 훑고 반복(`while changed`)하던 걸, exit_time 내림차순 정렬 + 첫 매치에서 즉시 break(사용자 제안)로 교체 -- 정렬 리스트는 `bisect.insort`로 증분 유지(단, `_place_blocks`가 `hard_deadline`을 진짜로 넘긴 "이후로는 전부 강제배치 확정" 구간에서만 안전하게 캐시 재사용 -- 그 전에는 탐색 경로가 언제든 같은 bay에 새 블록을 추가할 수 있어 캐시가 stale해질 위험이 있음). x16에서 Phase 1이 575s->75s(9.6배 초과->1.26배 초과).
    - **`_top_candidates_for_block`(xpress_reinsert 후보 생성)**: `_candidate_positions`에 `deadline`(2000쌍마다 체크, 넘으면 지금까지 만든 후보만 반환)과 `max_source_blocks`(사용자 제안 -- bay 블록이 많으면 면적 상위 `XPRESS_CANDIDATE_MAX_SOURCE_BLOCKS=50`개만 x/y 좌표 제공, 교차곱 구조 자체는 유지해서 #44에서 실패했던 "블록별 자기 코너만" 방식과 다름)를 추가. x4에서 후보생성 3.284s->1.158s(~2.8배) 실측.
    - **`_try_rebalance_move`**: `_candidate_positions`/`_find_earliest_slot` 호출에 캡이 아예 없었던 걸 발견(디버그 타이밍 계측으로 확인) -- 같은 `max_source_blocks`/`CANDIDATE_SCAN_CAP` 패턴 적용.
    - **`_left_justify`/`_right_justify`(Phase 2.5/2.6)**: `deadline`이 `t_start` 기준 고정 비율(85%/87%)이라, Repair가 자기 몫(80%)을 일찍 끝내면(예: 26.8s) 그 여유(126s!)를 통째로 물려받는 버그 -- x4에서 이 두 함수가 ~116초를 먹어서 Phase 3 라운드가 4개뿐이었음. `_justify_deadline()` 신설(사용자 제안): 매 호출 직전 `time.time()` 기준으로 "남은 예산의 5%, 최대 10초"만 배정(`JUSTIFY_MAX_FRAC_OF_REMAINING`/`JUSTIFY_ABS_CAP_SECONDS`) -- 기존 85%/87% 상한은 그대로 유지(안 넘음). 수정 후 116초->16초, Phase 3 라운드 4개->14개, objective 2.6% 추가 개선.
    - **`_improve`의 중복 `check_feasibility`**: Phase 3 진입 시 매번 전체를 처음부터 재검증하는데, 이게 4000블록 규모에서 ~19초(utils.py 소유라 수정 불가) -- Left/Right-justify가 이미 직전에 계산해둔 결과를 버리고 있었던 것 뿐이라, `check_feasibility_incremental`(diff 방식) 대신 **"이미 계산된, 현재 상태와 정확히 일치하는 결과를 그대로 재사용"**(diff 불필요, 상태가 그 사이 전혀 안 바뀜)하는 `known_result` 파라미터 추가. `greedyalgorithm`이 Left/Right-justify 블록마다 `last_verified_result`를 갱신(커밋됐으면 그 결과, 안 됐으면 pre-sweep 결과 -- 항상 `assignments`와 정확히 일치)해서 `_improve`에 전달.
    - x8(2000블록)은 위 조합만으로 60s 예산 안에 완전히 들어옴(56.2s). x16(4000블록)은 77.6s까지 줄었지만 Phase 3 자체가 0라운드(진입 시 `check_feasibility`가 ~19초를 다 먹음, utils.py라 못 고침) -- 2000~4000블록 사이 어딘가에 남은 한계선이 있음.

    **emergency fallback 위험 직접 실증 및 차단**: 위 조사 중 x16을 3회 반복하니 **2/3이 Repair의 INFEASIBLE 반환 -> restart 재시도 시간 부족 -> `myalgorithm._emergency_fallback`으로 낙하**하는 걸 확인(같은 코드, 순수 타이밍 노이즈). `_emergency_fallback`은 완전 결정론적이라 코드가 바뀌어도 값이 절대 안 바뀌는데, **실제 5차 제출 결과에서 P5가 4차 대비 21,268,024로 완전히 동일**했던 것(P4/P6는 매번 다름)이 이 메커니즘과 정확히 들어맞아, 실채점에서 이미 벌어지고 있을 가능성으로 격상.
    **수정**(`_repair`, 전부 사용자 설계): (a) 메인 루프 종료 조건을 80%->78%로 살짝 앞당겨서, 이 최종 보장 단계 전용으로 **2%를 확실히 예비**해둠(진행 중이던 pass 자체는 여전히 80%까지 쓸 수 있음). (b) 루프 종료 직후 이미 계산돼 있던 `result`(중복 호출 없음, 두 번째 `check_feasibility`는 생략 -- 4000블록 규모면 이것도 ~19초라 오히려 TLE를 만들 수 있어서, 검증은 바로 다음 Phase 2.5/2.6/3이 알아서 함)가 infeasible이면, 위반 블록 전부를 **"뽑아내고(assignments에서 제거) 밀어넣기(`_rebuild_bay_state`로 클린 상태 재구성 후 `_force_place`)"**(사용자 지정, 유령 충돌 방지) + **한 번에 하나씩 순차 처리**(사용자 지정, `_aabb_gap_entry`가 매번 갱신된 상태를 봐야 서로 안 겹침) 방식으로 강제 배치. (c) 이미 timelimit의 95%를 넘겼으면 이 단계 자체를 스킵(사용자 지적 -- 여기서 TLE를 새로 만들면 안 하느니만 못함).
    **검증**: 인위적으로 100블록을 전부 같은 좌표에 몰아넣고(423개 위반) `t_start`를 조작해 85% 지점에서 강제 트리거 -> 최종 보장이 발동해서 100개 전부 force-place, 별도 재검증 스크립트로 feasible=True 확인. 97% 지점(스킵 임계값 이후)에서는 정상적으로 스킵되고 infeasible 반환도 확인. x16을 이 fix까지 전부 반영한 상태로 10회 반복 -> **10/10 feasible, emergency fallback 0회**(이전 3회 중 2회 대비). 40개 로컬 인스턴스 20초 로버스트 테스트 3회(각 fix 적용 단계마다) -> 매번 40/40 feasible.

    **아직 안 건드린 것**: same-bay-first(가설 1)는 오늘 전혀 손 안 댐 -- 이건 "라운드가 도는가"가 아니라 "라운드 안에서 뭘 탐색하는가"의 문제라 완전히 별개, 다음 세션 과제로 이월. wholebay의 MIP 자체 pairwise 제약 구성 비용(O(K²×후보수²), K가 170+ 가능)도 후보 생성 캡의 간접 혜택만 받았을 뿐 별도 미해결.

60. **same-bay-first(가설 1) 검증 완료 -- P4~6 고착의 원인 아님으로 결론, 기본값 ON 유지 (2026-07-22)**
    `xpress_reinsert.py`에 `SAME_BAY_FIRST_ENABLED` 토글을 추가해 A/B 테스트. **블록 복제 스트레스 인스턴스(x4/x8, 같은 250블록 반복)에서는 결과가 배율에 따라 뒤집혔음**(x4: OFF가 ~1% 나음, x8: ON이 ~1.7% 나음) -- 재현성은 확인했지만(각 조건 2회씩 완전히 동일한 값), 방향 자체가 일관되지 않아 신뢰할 수 없는 신호로 판단. 원인: 복제는 같은 블록의 선호도/형상이 그대로 반복돼서 **인위적인 bay 쏠림**을 만듦 -- 진짜 크고 다양한 인스턴스의 대리 지표로 부적합.
    **개선된 방법**: 서로 다른 5개(이후 14개, 3400블록) 로컬 인스턴스를 하나로 합쳐(각자의 형상/선호도/시간 유지, bay 레이아웃과 가중치만 기준 인스턴스에서 통일) 이질적인 대형 스트레스 인스턴스를 생성. 이 조건에서는 **ON이 4회 반복 전부에서 일관되게 나음**(4~15% 격차, 방향이 한 번도 안 뒤집힘). 추가로 same-bay-first의 원래 도입 근거(전체 bay 스캔이 비쌈)가 오늘 candidate 생성 캡(#59, `max_source_blocks=50`) 도입으로 이미 상당 부분 약해졌다는 점도 확인(스캔 비용이 이제 인스턴스 크기와 무관하게 고정 상수).
    **결론**: same-bay-first는 P4~6 고착의 유력 용의자에서 제외. 기본값 `True`(ON) 유지, 토글은 재검증용으로 남겨둠.

61. **`_improve`의 매 accept마다 하는 전체 `check_feasibility` 재확인을 N번마다 1회로 배치 (사용자 제안, 2026-07-22)**
    Phase 3 진입 직후 매 accept(새 최선 또는 담금질/완화 워크스텝)마다 강제로 전체 `check_feasibility`를 다시 부르던 것을, 오늘 하루 쌓인 섀도 검증 데이터(다양한 인스턴스/스케일/연산자에 걸친 accept 624건, 불일치 0건)를 근거로 **`CONFIRM_CHECK_INTERVAL=10`번마다 1회 + 반환 직전 무조건 1회**로 완화. 그 사이는 `check_feasibility_incremental`(diff 기반)을 그대로 신뢰. 어긋나면 **그 사이의 accept를 전부 버리고 마지막 확인된 체크포인트로 롤백 후 즉시 종료**(어떤 라운드가 문제였는지 특정하지 않고 통째로 되돌리는 게 더 안전하고 단순).
    **안전성 논증**: 체크포인트는 항상 전체 검증을 통과한 상태만 저장하므로, 최종 반환값은 배치 여부와 무관하게 "Phase 3 시작 시점보다 나빠진 적 없고 항상 전체 검증을 통과한 상태"라는 보장이 그대로 유지됨. 유일한 이론적 단점은 안전성이 아니라 기회비용(드물게 어긋나면 즉시 잡을 때보다 최대 9라운드분 더 버림).
    **결함 주입 검증**: `check_feasibility_incremental`이 거짓 objective를 보고하도록 몽키패치해서 강제로 MISMATCH 유발 -> 반환 직전 무조건 검증이 정확히 잡아내고 마지막 정직한 체크포인트(200,567)로 완전히 롤백하는 것을 직접 확인.
    **실측 효과(3400블록 이질적 인스턴스, 300초, before=매번확인 vs after=배치)**: after가 라운드 하나 더 완주(대형 wholebay, K~700+, gain 823M 추가)해서 objective가 124,500,588,192 -> 124,116,249,147로 개선 -- 다만 12라운드까지는 두 버전이 구조적으로 거의 동일했다가 13라운드 이후 갈라진 것이라, 타이밍 노이즈의 영향도 배제 못 함(재현성 추가 확인은 다음 세션 과제로 남김).
    **검증**: prob_1/x4/x16/combined_3400(3회)에서 MISMATCH 0건. 40개 로컬 인스턴스 20초 로버스트 테스트(이 fix 반영 후) -> 40/40 feasible, 0 크래시/infeasible/시간초과.

62. **NFP 도입 전 프로파일링으로 전제 검증 -- Shapely 비용 미미, 실제 병목은 check_feasibility 중복 호출 + bounding_rect 재계산 (사용자 제안, 2026-07-22)**
    NFP(No-Fit Polygon) 도입에 앞서 "Shapely 폴리곤 교차 검사가 실제로 비용의 큰 비중을 차지하는지" 먼저 프로파일링(`cProfile`, combined_stress_3400, 60초)으로 검증. 결과: `check_feasibility`(9회 호출) 19.9s(33%), 우리 코드의 `_candidate_positions_maxrects2`(순수 AABB 산술, Shapely 미사용) 17.0s(28%), 반면 Shapely 자체 연산(intersection/is_valid/polygon 생성 전부 합산) 은 ~4-5s(~7-8%)에 불과. `check_entry`/`check_exit`/`check_collisions`의 자체 실행시간도 1.5% 미만 -- 비용 대부분이 이미 있는 AABB 프리필터(`bounding_rect`/`_bounding_box`, 440만 호출, 13.4s)에서 나옴. **결론: NFP는 이미 작은 비중(7-8%)을 노리는 것이라 투자 대비 효과가 낮음, 보류.**
    사용자가 "check_feasibility가 그렇게 비싼 게 utils.py 자체의 본질적 한계인지, 아니면 우리 코드가 비효율적으로 여러 번 부르는 건지"를 재질문 -- 호출 지점 9곳 전수 추적 결과, **Phase 2.5(left-justify)의 `pre_result`와 Phase 2.6(right-justify)의 `pre_rj_result` 2곳이 순수 중복**이었음: 바로 직전 단계(`_repair` 또는 Phase 2.5)가 이미 계산해서 `last_verified_result`에 들고 있는 것과 정확히 동일한 내용을 처음부터 다시 계산하고 있었음(Phase 2.6은 `last_verified_result` 변수가 바로 옆에 있는데도 아예 참조하지 않고 있었음 -- 단순 누락).
    **수정**: (a) `_repair()`가 `assignments`만이 아니라 `(assignments, verified_result)`를 반환하도록 변경 -- `verified_result`는 반환 시점의 `result`가 feasible이었을 때만 채워짐(최종 guarantee 강제배치가 발동해 `assignments`를 추가로 건드린 경우는 `None`, 그 경우 호출부가 기존처럼 재계산). (b) Phase 2.5가 이 값을 `pre_result`로 재사용(없으면 기존처럼 재계산). (c) Phase 2.6이 `last_verified_result`(Phase 2.5 산출물, 없으면 `_repair` 산출물)를 `pre_rj_result`로 재사용. (d) `_candidate_positions_maxrects2`가 매 호출마다 이미 배치된 블록들의 `bounding_rect()`를 처음부터 재계산하던 것을, Block 인스턴스에 결과를 직접 캐싱하는 `_cached_bounding_rect()` 헬퍼로 대체 -- Block의 x/y/orient_idx가 이 코드베이스 전체에서 한 번도 in-place로 mutate되지 않는다는 것(항상 새 Block 생성, grep으로 확인 + `__post_init__`의 `_layers_cache`도 동일 전제)에 기반한 안전한 캐싱. `utils.py`는 건드리지 않음(Block에 `__slots__`가 없어 외부에서 속성 추가 가능).
    **검증**: combined_stress_3400(60초) 재프로파일링 -> `check_feasibility` 9회->7회(예측대로 정확히 2회 감소), `bounding_rect`/`_bounding_box` 440만->324만 호출(~26% 감소, 전체 예상보다는 작음 -- 알고리즘이 시간 적응형이라 절약된 시간이 다시 재투자되면서 실행 경로 자체가 달라짐, 오늘 계속 나온 타이밍 노이즈 효과), 전체 60.0s->57.9s(~3.5%). 40개 로컬 인스턴스 20초 로버스트 테스트 -> 40/40 feasible, 0 크래시.

63. **시간 기반 아키텍처 안정성 재검토 1단계: 남은 이진 cliff 전수조사 -- 새로운 cliff 없음 확인 (사용자 지시, 2026-07-22)**
    "채점 서버는 1회만 채점하는데 타이밍 노이즈로 품질이 흔들리는 게 근본적으로 괜찮은가"라는 사용자 문제 제기 이후, `baseline_greedy.py`/`xpress_reinsert.py`/`myalgorithm.py`의 `time.time() > deadline` 패턴 18곳 전수 확인. 결과: 대부분 오늘 이미 고친 graceful-degradation/safe-truncation 패턴(호출부가 부분 결과를 안전하게 재검증 후 채택/폐기), 2곳(`_regret_construct`, `_place_blocks_batched`)은 `priority_rule="regret"`/`construction_mode="batched"`가 실제로 한 번도 선택되지 않는 죽은 코드, `xpress_reinsert.py`의 두 곳은 실패 시 안전한 폴백 경로로 전환. 유일하게 남은 건 `_improve`의 greedy 폴백(`hard_deadline` 미적용)인데, K가 작고(보통 ≤12) Phase 3의 "개선 안 되면 버림" 로직 덕에 최악의 경우가 "라운드 하나 낭비"뿐이라 기존의 의도적 방치가 여전히 타당함. **결론: 새로운 이진 cliff는 발견되지 않음.**

64. **시간 기반 아키텍처 안정성 재검토 2단계: `_place_blocks`의 scan budget을 이진 전환에서 연속 램프로 (사용자 제안, 2026-07-22)**
    `_place_blocks`의 overtime 메커니즘(#58)이 여전히 `scan_budget = OVERTIME_SCAN_CAP if in_overtime else CANDIDATE_SCAN_CAP`라는 **두 값짜리 이진 전환**이었음 -- `deadline` 넘기 직전 블록은 여전히 CANDIDATE_SCAN_CAP(50) 전체를, 넘긴 직후 블록은 갑자기 OVERTIME_SCAN_CAP(5)만 받아서 "같은 cliff가 한 단계 아래에서 재발"하는 구조. 사용자 제안: `time_left_ratio = (hard_deadline - now) / (hard_deadline - phase_start)` 기반으로 `scan_budget`을 `base_cap`에서 `floor_cap`까지 선형으로 연속 감소시키는 `_dynamic_scan_cap()` 헬퍼 신설. 별도의 처리량 벤치마크 없이도 "느린 기계일수록 블록당 경과시간이 더 빨리 누적 -> ratio가 더 빨리 줄어듦"이라는 방식으로 자연스럽게 기계 속도에 맞춰짐(자가보정 효과를 부작용으로 얻음).
    **검증**: x16(4000블록) 스트레스 인스턴스 3회 반복 -> 전부 feasible, elapsed 63.2~63.8s(60s 예산 대비 일관된 오버런, 이전 그래프에이션 수정 이후 수준과 동등하거나 더 좋음). 40개 로컬 인스턴스 20초 로버스트 테스트 -> 40/40 feasible, 0 크래시. `experiment/dynamic-scan-cap` 브랜치에서 main으로 merge.

65. **시간 기반 아키텍처 안정성 재검토 3단계: `myalgorithm._solve`의 최종 안전마진을 인스턴스 크기 기반 측정치로 (사용자 제안, 2026-07-22)**
    `inner_timelimit = timelimit * 0.9`(check_feasibility 최종 검증 + emergency fallback을 위한 고정 10% 예약)가 몇백 블록짜리 로컬 인스턴스 기준으로 튜닝된 값이었는데, `check_feasibility` 자체 비용이 인스턴스 크기에 비례한다는 걸 오늘(#62) 프로파일링으로 이미 확인함(combined_stress_3400: ~2.2s/3400블록 =~ 0.00065s/블록). 숨겨진 P4-6이 충분히 크거나 서버가 느리면 `check_feasibility` 단 한 번 호출이 10% 예약분을 넘길 수 있음 -- 정확히 이 마진이 지키려는 것. **수정**: `reserve = max(timelimit*0.1, 0.0013(=측정치 2배, 서버 속도 불확실성 헤지) * n_blocks * 3(=이후 남은 check_feasibility급 호출 수))`로, 기존 고정 10%보다 작아지는 일은 없는 순수 가산 방식(`max()`). 로컬 40개 인스턴스(최대 300블록)에서는 추정치가 항상 고정 10% 아래라 동작 변화 없음, 3400+블록 규모에서는 추정치가 지배적이 됨.
    **범위 결정**: `_repair`의 78%/95% 임계값에는 같은 공식을 적용하지 않음 -- 그 마진은 `check_feasibility`가 아니라 최종 guarantee 단계의 강제배치(`_force_place`) 비용을 위한 것이라 다른 연산이고, `_force_place`는 이미 O(1)에 가깝게 고쳐져 있어(오늘 #59) 진짜 변수는 "몇 개가 강제배치될지"인데 이건 사전에 계측 불가능 -- 근거 없이 같은 숫자를 끌어다 쓰면 실제보다 엄밀한 것처럼 보이는 것뿐이라 판단, 손대지 않음.
    **검증**: x16/combined_stress_3400 각 3회 반복 -> 전부 feasible이고, elapsed가 이전(60s 예산에서 63~64s로 초과하던 수준)보다 **명확히 개선**되어 53.9~55.4s(x16)/51.1~51.9s(combined_3400)로 예산 안에 안전하게 들어옴 -- 더 큰 예약분 덕에 실제 마진이 커진 것으로 해석. objective는 이전 수준과 동등(품질 손실 미미). 40개 로컬 인스턴스 20초 로버스트 테스트 -> 40/40 feasible, 0 크래시. `experiment/measured-safety-margin` 브랜치에서 main으로 merge.

66. **`Block.bounding_rect()` 전역 캐싱 -- 클래스 메서드 자체를 패치해서 check_feasibility 내부 재계산까지 제거 (사용자 제안, 2026-07-22)**
    3가지 시간기반 안정성 항목을 마치고, 사용자가 "check_feasibility 9번 호출이 utils.py 본질적 비용이 아니라 우리 코드 비효율이었다"는 점을 짚으며 전체 프로파일을 다시 훑어 가장 심각한 병목을 찾자고 제안. `pstats.print_callers('bounding_rect')`로 호출자 breakdown을 뜬 결과, 남은 306만 회 `bounding_rect` 호출 중 **170만 회(56%)가 `_find_earliest_slot` 단독**에서 나옴(이미 배치된 `placed_in_bay`를 매 호출 재스캔하며 AABB를 재계산 -- 오늘 앞서 `_candidate_positions_maxrects2`에만 적용했던 캐싱 누락 사례가 여기도 동일하게 있었음), `_block_area`(MaxRects 정렬 키)도 2위 기여자.
    **더 큰 발견**: `check_feasibility`(utils.py) 자체도 bay별 Block 리스트를 한 번만 만들어 Stage 2/3/4 전체에서 재사용하는데(코드 주석에 명시), 각 스테이지가 독립적으로 같은 블록의 `bounding_rect()`를 또 호출 -- 이건 utils.py **내부** 코드라 개별 호출부 캐싱(오늘 오전에 한 방식)으로는 절대 닿을 수 없는 비용이었음.
    **수정**: `_cached_bounding_rect()` 래퍼(개별 호출부용)를 폐기하고, 대신 `baseline_greedy.py` import 시점에 `Block.bounding_rect` **메서드 자체**를 패치(`Block.bounding_rect = _cached_bounding_rect_method`) -- utils.py 파일은 전혀 안 건드리고(서버가 새로 덮어쓸 그 파일 자체는 그대로), 런타임에 이미 로드된 클래스 객체에 인스턴스별 캐싱 속성을 얹는 방식. 이렇게 하면 `check_feasibility`를 포함해 Block 인스턴스를 다루는 **모든** 코드가 자동으로 캐싱 혜택을 받음(호출부를 하나하나 고칠 필요 없음). 안전성 근거는 기존과 동일(Block의 x/y/orient_idx가 baseline_greedy.py/xpress_reinsert.py**뿐 아니라 utils.py 자체에서도** 생성 후 in-place mutate되지 않음을 grep으로 재확인).
    **검증**: combined_stress_3400 재프로파일링 -> `bounding_rect` 호출 304만->30.6만(~90% 감소), 상위 30위 밖으로 밀려남. x16/combined_3400 각 3회 반복 -> 전부 feasible, combined_3400 objective가 이전(143.7억~149.9억) 대비 124억~128억으로 개선(절약된 시간이 Phase 3에 재투자된 것으로 추정). 40개 로컬 인스턴스 20초 로버스트 -> 40/40 feasible, 0 크래시. `experiment/global-bbox-cache` 브랜치에서 main으로 merge.
    **남은 최대 병목 (미해결, 성격이 다름)**: `_candidate_positions_maxrects2`(MaxRects 후보생성) 자체가 매 호출마다 `placed_blocks` 전체를 처음부터 재생하며 O(m x freelist^2) sliver-pruning/containment dedup을 수행 -- 이건 캐싱으로 없앨 수 있는 "이미 아는 답 재계산"이 아니라, 매 호출이 실제로 다른 입력(다른 블록)에 대한 진짜 새 계산이라 발생하는 **알고리즘 자체의 복잡도**. 코드 내 `min_width`/`min_height` 파라미터 주석에 이미 진짜 해법(free-rect 리스트를 호출 간 persistent하게 유지)이 언급돼 있으나, sliver-pruning 임계값을 "이번 호출 블록 크기" 대신 "앞으로 올 수 있는 모든 블록의 전역 최솟값"으로 바꿔야 하는 정확성 문제가 있어 미룬 상태. 오늘 고친 것들보다 범위가 크고 리스크가 높은 별도 프로젝트로 남겨둠.

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
