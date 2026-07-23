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
SMALLER set (8 instances) at a LONGER, more realistic budget (default
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import myalgorithm
from utils import check_feasibility

REPO_ROOT = Path(__file__).resolve().parent.parent
CANARY_HISTORY_PATH = Path(__file__).resolve().parent / "canary_history.jsonl"

_INSTANCE_SEARCH_DIRS = [
    REPO_ROOT.parent / "training_instances_20260531-CWCx_z9X" / "train",
    REPO_ROOT.parent / "train-set2-UXyrUSG6" / "train",
]

# 2026-07-24 (user-approved set). "target"/"confidence"/"note" are exactly
# the reasoning from notes/experiments.md's cross-referencing session --
# update THERE first if the hypothesis changes, then mirror here.
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
    {"instance": "prob_40", "target": "P4",    "confidence": "low",
     "note": "로컬 최혼잡 인스턴스 -- P4 '구조적 정체'가 혼잡도 관련이라는 최선 추측, 미확정"},
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
            print(f"  {row['timestamp']}  commit={row['commit']:<8}  timelimit={row['timelimit']:.0f}s  "
                 f"{status}  obj={obj}  {row.get('label', '')}")


def run(timelimit: float, label: str) -> None:
    commit = _git_commit()
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    history = _load_history()
    by_instance: dict[str, list[dict]] = {}
    for row in history:
        by_instance.setdefault(row["instance"], []).append(row)

    print(f"Canary quality check -- commit={commit}  timelimit={timelimit:.0f}s  label={label!r}")
    print(f"History file: {CANARY_HISTORY_PATH}\n")

    any_regression = False
    for entry in CANARY_SET:
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
            "timestamp": timestamp, "commit": commit, "instance": name,
            "target": entry["target"], "confidence": entry["confidence"],
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
    args = parser.parse_args()
    if args.history:
        _print_history_table(_load_history())
    else:
        run(args.timelimit, args.label)
