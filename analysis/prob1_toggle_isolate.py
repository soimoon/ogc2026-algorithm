"""
2026-07-21: isolate which post-1st-submission feature is responsible for the
prob_1 regression (steady-state ~41,183 on the 1st-submission code vs
~66,281 on current code, confirmed via analysis/repeat_check.py -- not
noise). Toggles one flag off at a time from the current all-True default and
reruns prob_1, to see which single change (or combination) recovers most of
the gap. seed is left fixed (0) throughout -- only structural flags vary.

Usage:
    python analysis/prob1_toggle_isolate.py <instance.json> [--timelimit 180]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baseline_greedy
from utils import check_feasibility

CONFIGS = [
    ("all_true (current default)", dict()),
    ("blocking_chain=False", dict(blocking_chain=False)),
    ("z2z3_modes=False", dict(z2z3_modes=False)),
    ("left_justify=False", dict(left_justify=False)),
    ("right_justify=False", dict(right_justify=False)),
    ("bc+z23+lj+rj all False", dict(blocking_chain=False, z2z3_modes=False,
                                    left_justify=False, right_justify=False)),
]


def run(instance_path: str, timelimit: float) -> None:
    with open(instance_path, encoding="utf-8") as f:
        prob_info = json.load(f)

    print(f"Instance: {instance_path}  timelimit={timelimit}s per config")
    print("=" * 100)
    for name, kwargs in CONFIGS:
        t0 = time.time()
        try:
            sol = baseline_greedy.greedyalgorithm(prob_info, timelimit=timelimit, **kwargs)
            result = check_feasibility(prob_info, sol)
        except Exception as exc:
            print(f"{name:32s}  CRASH: {exc}")
            continue
        elapsed = time.time() - t0
        if result["feasible"]:
            print(f"{name:32s}  obj={result['objective']:>14,.0f}  "
                  f"(obj1={result['obj1']:.0f} obj2={result['obj2']:.0f} obj3={result['obj3']:.0f})  "
                  f"({elapsed:.1f}s)")
        else:
            print(f"{name:32s}  INFEASIBLE  ({elapsed:.1f}s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instance", help="instance JSON file")
    parser.add_argument("--timelimit", type=float, default=180.0)
    args = parser.parse_args()
    run(args.instance, args.timelimit)
