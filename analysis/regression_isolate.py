"""
Isolate which of {blocking_chain, z2z3_modes} caused the regression found by
before_after_compare.py (current code vs the actually-submitted zip, 5/6
instances worse, up to 35x). Runs all 4 combinations directly against
baseline_greedy.greedyalgorithm (bypassing myalgorithm's safety margin so
the comparison is apples-to-apples with the same effective timelimit).

Usage:
    python analysis/regression_isolate.py <instance.json | glob | dir> [...] [--timelimit 90]
"""
import argparse
import glob as globmod
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baseline_greedy
from utils import check_feasibility

CONFIGS = [
    ("neither", dict(blocking_chain=False, z2z3_modes=False)),
    ("bc_only", dict(blocking_chain=True, z2z3_modes=False)),
    ("z23_only", dict(blocking_chain=False, z2z3_modes=True)),
    ("both", dict(blocking_chain=True, z2z3_modes=True)),
]


def _collect_instance_files(patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        p = Path(pattern)
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")))
        else:
            files.extend(sorted(Path(m) for m in globmod.glob(pattern)))
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

    print(f"Found {len(files)} instance file(s). timelimit={timelimit}s per (instance, config).")
    print("=" * 100)

    totals = {name: 0.0 for name, _ in CONFIGS}
    n_feasible = {name: 0 for name, _ in CONFIGS}

    for f in files:
        with open(f, encoding="utf-8") as fh:
            prob_info = json.load(fh)

        row = {}
        for name, kwargs in CONFIGS:
            t0 = time.time()
            try:
                sol = baseline_greedy.greedyalgorithm(prob_info, timelimit=timelimit, **kwargs)
                result = check_feasibility(prob_info, sol)
            except Exception as exc:
                row[name] = (False, None, time.time() - t0, str(exc))
                continue
            row[name] = (result["feasible"], result.get("objective"), time.time() - t0, None)

        parts = []
        for name, _ in CONFIGS:
            ok, obj, elapsed, err = row[name]
            if err:
                parts.append(f"{name}=CRASH")
            elif not ok:
                parts.append(f"{name}=INFEASIBLE")
            else:
                n_feasible[name] += 1
                totals[name] += obj
                parts.append(f"{name}={obj:,.0f}")
        print(f"{f.name:16s}  " + "  ".join(parts))

    print("=" * 100)
    for name, _ in CONFIGS:
        if n_feasible[name] > 0:
            print(f"  {name:10s}: {n_feasible[name]}/{len(files)} feasible, "
                  f"avg objective = {totals[name] / n_feasible[name]:,.0f}")
        else:
            print(f"  {name:10s}: 0/{len(files)} feasible")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patterns", nargs="+",
                        help="instance JSON file(s), glob pattern(s), or directory(ies)")
    parser.add_argument("--timelimit", type=float, default=90.0,
                        help="wall-clock time limit per (instance, config) in seconds (default: %(default)s)")
    args = parser.parse_args()
    run(args.patterns, args.timelimit)
