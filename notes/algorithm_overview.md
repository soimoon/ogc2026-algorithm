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
