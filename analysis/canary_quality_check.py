"""
canary_quality_check.py -- quality-regression check on a small, curated
"canary" subset of the 40 local instances, each picked because it's
believed to structurally resemble one of the 6 hidden grading instances
(P1-P6) -- see notes/experiments.md's 2026-07-24 entry for the full
reasoning (submission-history cross-referenced against each local
instance's actual w1/w2/w3/block-count/bay-count).

Distinct from analysis/robustness_check.py (2026-07-24 discussion,
user-proposed split): robustness_check.py runs ALL 40 local instances at a
SHORT budget (~20s) to catch crashes/infeasible/TLE before every submission
-- cheap, run every time, feasibility-only. This script runs a much
SMALLER set (10 instances) at a LONGER, more realistic budget (default
120s, enough for ALNS to actually converge) and, critically, PERSISTS every
run's result to a JSON-Lines history file (CANARY_HISTORY_PATH) so quality
regressions -- not just infeasibility -- are visible across code changes
over time, instead of a one-off before/after comparison that gets thrown
away after the session ends.

Runs through myalgorithm.algorithm() (the actual competition entry point,
including _iterated_greedy restarts and the emergency-fallback safety net)
rather than calling baseline_greedy.greedyalgorithm() directly, so this
measures what a real submission would actually produce.

IMPORTANT CAVEAT (never forget this): P1-P6 are hidden grading instances,
NOT the same files as prob_1-40 -- every "target" annotation below is a
STRUCTURAL/BEHAVIORAL similarity guess (weight profile, block/bay count,
or reproduced-regression pattern), never a claim of identity. Confidence
is annotated per entry; "low" entries are best-effort placeholders, not
validated matches -- treat their results as directional signal only, not
proof either way about the corresponding hidden problem.

Usage:
    python analysis/canary_quality_check.py [--timelimit 120] [--label "text"]
    python analysis/canary_quality_check.py --history     # print history table only, no run
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CANARY_HISTORY_PATH = Path(__file__).resolve().parent / "canary_history.jsonl"

_INSTANCE_SEARCH_DIRS = [
    REPO_ROOT.parent / "training_instances_20260531-CWCx_z9X" / "train",
    REPO_ROOT.parent / "train-set2-UXyrUSG6" / "train",
]

# 2026-07-24 (user-approved set). "target"/"confidence"/"note" are exactly
# the reasoning from notes/experiments.md's cross-referencing session --
# update THERE first if the hypothesis changes, then mirror here.
#
# 2026-07-24 (later, +2 during the parallel-restart worker-count
# investigation): 8 -> 10, picked by scanning all 40 local instances'
# w1/w2/w3/block/bay profiles for anything that strengthens or diversifies
# the existing 8, not just to pad the count.
#   - prob_36: near-exact weight-profile AND size match to prob_40
#     (w1=667, w2=1, w3=13, 250 blocks, 4 bays -- identical on every one of
#     those axes). prob_40 was previously the SOLE P4 candidate at "low"
#     confidence (a congestion guess, not a weight-profile match like every
#     other entry here) -- having a second instance land in the exact same
#     rare corner of weight-space (w1=667 is the lowest in the entire local
#     set, shared by only prob_25/36/40) turns that into an actual cluster
#     match, the same style of evidence prob_32/34's P3/P6 pairing already
#     uses. Raises P4 confidence from "low" to "medium".
#   - prob_20: weak justification, included for coverage rather than a
#     confident P-target guess -- the ONLY 5-bay instance in the entire
#     local 40 (every other local instance has 2-4); already noted as
#     structurally "이질적" when P5's own grouping explicitly excluded it.
#     No hidden instance has been hypothesized to be 5-bay specifically,
#     but the canary set otherwise has zero representation of that regime.
#     Kept at "low" confidence deliberately -- treat its results as
#     structural-diversity coverage for the worker-count experiment, not
#     as a P-target signal.
CANARY_SET = [
    {"instance": "prob_1",  "target": "P1",    "confidence": "medium",
     "note": "구성 다양화 민감성(문서화됨) + 5→6차 P3/P6 회귀 재현 3종 중 하나"},
    {"instance": "prob_14", "target": "P3",    "confidence": "high",
     "note": "5→6차 회귀 재현 (이진탐색으로 확정)"},
    {"instance": "prob_34", "target": "P3/P6", "confidence": "high",
     "note": "회귀 재현 + w1=3333(낮음)/w3=533(높음) 가중치 계열"},
    {"instance": "prob_32", "target": "P3/P6", "confidence": "medium",
     "note": "w1=3333/w3=600, prob_34와 같은 가중치 계열(대체 신호)"},
    {"instance": "prob_17", "target": "P5",    "confidence": "medium-high",
     "note": "300블록, bay=4, w1=9,697 (prob_18/19와 같은 하위그룹)"},
    {"instance": "prob_18", "target": "P5",    "confidence": "medium-high",
     "note": "300블록, bay=4, w1=13,333 (prob_17/19와 같은 하위그룹)"},
    {"instance": "prob_19", "target": "P5",    "confidence": "medium-high",
     "note": "300블록, bay=4, w1=10,667 (prob_20은 w1=26,667/bay=5로 이질적이라 제외)"},
    {"instance": "prob_40", "target": "P4",    "confidence": "medium",
     "note": "로컬 최혼잡 인스턴스 + prob_36과 완전 동일한 가중치/규모 클러스터 "
             "(w1=667, w2=1, w3=13, 250블록/4bay) -- 원래 단독 low-confidence 추측이었으나 "
             "짝이 생겨 medium으로 상향"},
    {"instance": "prob_36", "target": "P4",    "confidence": "medium",
     "note": "prob_40과 완전 동일 가중치/규모 클러스터 (w1=667, w2=1, w3=13, 250블록/4bay) "
             "-- 로컬 40개 중 w1=667은 prob_25/36/40 셋뿐인 희귀 구간"},
    {"instance": "prob_20", "target": "미분류", "confidence": "low",
     "note": "로컬 40개 중 유일한 5-bay 인스턴스 (나머지는 전부 2~4bay) -- P-타겟 추측이 아니라 "
             "구조적 다양성 확보용. 병렬 워커수 실험에서 미대표 구조 커버 목적으로 추가"},
]


def _find_instance_path(name: str) -> Path:
    for d in _INSTANCE_SEARCH_DIRS:
        p = d / f"{name}.json"
        if p.exists():
            return p
    raise FileNotFoundError(f"Canary instance {name}.json not found in {_INSTANCE_SEARCH_DIRS}")


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _load_history() -> list[dict]:
    if not CANARY_HISTORY_PATH.exists():
        return []
    rows = []
    with open(CANARY_HISTORY_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _append_history(row: dict) -> None:
    with open(CANARY_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _print_history_table(history: list[dict]) -> None:
    by_instance: dict[str, list[dict]] = {}
    for row in history:
        by_instance.setdefault(row["instance"], []).append(row)
    for entry in CANARY_SET:
        name = entry["instance"]
        rows = by_instance.get(name, [])
        print(f"\n=== {name}  (target={entry['target']}, confidence={entry['confidence']}) ===")
        if not rows:
            print("  (no history yet)")
            continue
        for row in rows[-10:]:
            status = "FEASIBLE" if row["feasible"] else "INFEASIBLE"
            obj = f"{row['objective']:,.0f}" if row["objective"] is not None else "N/A"
            ver = row.get("code_version", "working-tree")
            print(f"  {row['timestamp']}  code={ver:<28}  commit={row['commit']:<8}  "
                 f"timelimit={row['timelimit']:.0f}s  {status}  obj={obj}  {row.get('label', '')}")


def run(timelimit: float, label: str, code_dir: str | None, only: str | None = None) -> None:
    # 2026-07-24 (user-proposed): optionally test an OLD code snapshot (e.g.
    # submissions/submission_20260723_1250, the 7th round's actual submitted
    # code) against the same canary set, for a local-only stand-in comparison
    # while waiting for a round's real eval-server score to come back.
    # Prepending code_dir to sys.path makes `import myalgorithm` (which
    # itself does `import baseline_greedy` / `import xpress_reinsert`)
    # resolve to THAT directory's copies instead of the repo root's current
    # working-tree versions -- utils.py is deliberately NOT shipped in
    # submission snapshots (it's server-provided/never modified), so it
    # still resolves to the repo root's copy either way, exactly matching
    # how the grading server actually runs old submissions.
    if code_dir:
        code_path = str(Path(code_dir).resolve())
        sys.path.insert(0, code_path)
        version_label = Path(code_dir).name
    else:
        sys.path.insert(0, str(REPO_ROOT))
        version_label = "working-tree"
    sys.path.insert(0, str(REPO_ROOT))  # utils.py always from repo root

    for mod in ("myalgorithm", "baseline_greedy", "xpress_reinsert", "cpsat_reinsert"):
        sys.modules.pop(mod, None)  # never trust a stale import from a prior code_dir in-process
    import myalgorithm
    from utils import check_feasibility

    commit = _git_commit()
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    history = _load_history()
    by_instance: dict[str, list[dict]] = {}
    for row in history:
        by_instance.setdefault(row["instance"], []).append(row)

    print(f"Canary quality check -- code_version={version_label}  commit={commit}  "
          f"timelimit={timelimit:.0f}s  label={label!r}")
    print(f"History file: {CANARY_HISTORY_PATH}\n")

    any_regression = False
    active_set = [e for e in CANARY_SET if e["instance"] == only] if only else CANARY_SET
    for entry in active_set:
        name = entry["instance"]
        path = _find_instance_path(name)
        with open(path, encoding="utf-8") as f:
            prob_info = json.load(f)

        t0 = time.time()
        solution = myalgorithm.algorithm(prob_info, timelimit=timelimit)
        elapsed = time.time() - t0
        result = check_feasibility(prob_info, solution)
        feasible = result["feasible"]
        objective = result["objective"] if feasible else None

        row = {
            "timestamp": timestamp, "commit": commit, "code_version": version_label,
            "instance": name, "target": entry["target"], "confidence": entry["confidence"],
            "timelimit": timelimit, "elapsed": elapsed,
            "feasible": feasible, "objective": objective, "label": label,
        }
        _append_history(row)

        prior_rows = by_instance.get(name, [])
        prior = prior_rows[-1] if prior_rows else None
        status = "FEASIBLE" if feasible else f"INFEASIBLE(stage={result['stage']})"
        obj_str = f"{objective:,.0f}" if objective is not None else "N/A"
        print(f"[{name}] (target={entry['target']}, confidence={entry['confidence']})  "
              f"{status}  obj={obj_str}  elapsed={elapsed:.1f}s")

        if not feasible:
            print(f"  !!! INFEASIBLE -- must-fix before submitting (see problem statement Sec 3.3)")
            any_regression = True
        elif prior is not None and prior["feasible"] and prior["objective"] is not None:
            delta = objective - prior["objective"]
            pct = 100 * delta / prior["objective"] if prior["objective"] else 0.0
            if delta > 1e-6:
                print(f"  ^ REGRESSION vs previous run ({prior['timestamp']}, commit={prior['commit']}, "
                      f"timelimit={prior['timelimit']:.0f}s): {prior['objective']:,.0f} -> {objective:,.0f} "
                      f"({pct:+.1f}%)")
                any_regression = True
            elif delta < -1e-6:
                print(f"  ^ improved vs previous run ({prior['timestamp']}, commit={prior['commit']}): "
                      f"{prior['objective']:,.0f} -> {objective:,.0f} ({pct:+.1f}%)")
            else:
                print(f"  ^ same as previous run ({prior['timestamp']}, commit={prior['commit']})")
        else:
            print("  (no prior history for this instance -- first run)")

    print(f"\n{'!!! ONE OR MORE REGRESSIONS/INFEASIBLE -- review before submitting' if any_regression else 'No regressions detected vs previous recorded run.'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timelimit", type=float, default=120.0,
                        help="wall-clock seconds per canary instance (default: %(default)s)")
    parser.add_argument("--label", type=str, default="",
                        help="free-text tag for this run (e.g. 'before grid filter')")
    parser.add_argument("--history", action="store_true",
                        help="print recorded history for all canary instances and exit (no run)")
    parser.add_argument("--code-dir", type=str, default=None,
                        help="run an OLD code snapshot instead of the working tree, e.g. "
                             "submissions/submission_20260723_1250 (must contain myalgorithm.py/"
                             "baseline_greedy.py/xpress_reinsert.py; utils.py always comes from "
                             "the repo root, matching how the grading server treats submissions)")
    parser.add_argument("--only", type=str, default=None,
                        help="run just one canary instance by name (e.g. prob_34) -- for fast "
                             "bisection iterations instead of the full 8-instance set")
    args = parser.parse_args()
    if args.history:
        _print_history_table(_load_history())
    else:
        run(args.timelimit, args.label, args.code_dir, args.only)
