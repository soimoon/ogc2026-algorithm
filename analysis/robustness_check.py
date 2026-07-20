"""
Smoke-test myalgorithm.algorithm() across many instance files at once.

Motivation: the evaluation server scores infeasible / time-limit-exceeded /
"raised an exception" identically as -1 (problem statement Sec 3.3), so the
single highest-leverage thing to verify before spending one of the limited
(12h-cooldown) submission slots is that the entry point never does any of
those three things, across the full range of instance sizes we have locally.

This script does NOT judge solution quality (objective value) -- it only
checks the floor: did it crash, did it blow the time budget, is the result
actually feasible per utils.check_feasibility. Objective is printed for
feasible runs purely for eyeballing, not as a pass/fail criterion.

Usage:
    python analysis/robustness_check.py <instance.json | glob | dir> [...] [--timelimit 30]

Examples:
    python analysis/robustness_check.py "../training_instances_20260531-CWCx_z9X/train/*.json"
    python analysis/robustness_check.py ../train-set2-UXyrUSG6/train --timelimit 60
"""
import argparse
import glob as globmod
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import myalgorithm
from utils import check_feasibility


def _collect_instance_files(patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        p = Path(pattern)
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")))
        else:
            files.extend(sorted(Path(m) for m in globmod.glob(pattern)))
    # de-duplicate while preserving order
    seen = set()
    unique = []
    for f in files:
        rp = f.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(f)
    return unique


def run(patterns: list[str], timelimit: float) -> None:
    files = _collect_instance_files(patterns)
    if not files:
        print(f"No instance files matched: {patterns}")
        sys.exit(1)

    print(f"Found {len(files)} instance file(s). timelimit={timelimit}s per instance.")
    print("=" * 100)

    n_ok = n_infeasible = n_exception = n_over_budget = 0
    worst_over_budget = 0.0

    for f in files:
        with open(f, encoding="utf-8") as fh:
            prob_info = json.load(fh)

        n_blocks = len(prob_info["blocks"])
        n_bays = len(prob_info["bays"])
        label = f"{f.name:16s} bays={n_bays:2d} blocks={n_blocks:4d}"

        t0 = time.time()
        try:
            solution = myalgorithm.algorithm(prob_info, timelimit=timelimit)
            elapsed = time.time() - t0
            result = check_feasibility(prob_info, solution)
        except Exception as exc:
            elapsed = time.time() - t0
            n_exception += 1
            print(f"{label}  CRASHED  {type(exc).__name__}: {exc}  elapsed={elapsed:.1f}s")
            continue

        over_budget = elapsed > timelimit * 1.05  # 5% tolerance for wrapper overhead
        if over_budget:
            n_over_budget += 1
            worst_over_budget = max(worst_over_budget, elapsed - timelimit)

        if result["feasible"]:
            n_ok += 1
            status = "FEASIBLE  "
            extra = f"obj={result['objective']:.0f}"
        else:
            n_infeasible += 1
            status = "INFEASIBLE"
            extra = f"stage={result['stage']}  {result['violations'][:1]}"

        budget_flag = "  [OVER BUDGET]" if over_budget else ""
        print(f"{label}  {status}  elapsed={elapsed:6.1f}s  {extra}{budget_flag}")

    print("=" * 100)
    print(f"Summary: {n_ok}/{len(files)} feasible, {n_infeasible} infeasible, "
          f"{n_exception} crashed, {n_over_budget} over time budget "
          f"(worst overrun: {worst_over_budget:.1f}s)")
    if n_infeasible or n_exception or n_over_budget:
        print("^ any of these on a hidden instance scores -1 on the leaderboard "
              "(problem statement Sec 3.3) -- treat as must-fix before submitting.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patterns", nargs="+",
                        help="instance JSON file(s), glob pattern(s), or directory(ies)")
    parser.add_argument("--timelimit", type=float, default=30.0,
                        help="wall-clock time limit per instance in seconds (default: %(default)s)")
    args = parser.parse_args()
    run(args.patterns, args.timelimit)
