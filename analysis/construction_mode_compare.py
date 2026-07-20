"""
Empirical A/B comparison of Phase-1 construction_mode ("serial" vs "batched")
across many instances, holding everything else (priority_rule=edd, repair,
Phase 2.5/3, timelimit, seed) fixed.

"batched" (see baseline_greedy._place_blocks_batched) commits Phase 1 in
batches of PHASE1_BATCH_SIZE via xpress_reinsert.reinsert() (joint MIP)
instead of one block at a time (Serial SGS) -- this settles empirically
whether that actually reduces repair burden / improves the final objective,
rather than by argument.

Usage:
    python analysis/construction_mode_compare.py <instance.json | glob | dir> [...] [--timelimit 90]
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

MODES = ["serial", "batched"]


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

    print(f"Found {len(files)} instance file(s). timelimit={timelimit}s per (instance, mode).")
    print("=" * 100)

    wins = {m: 0 for m in MODES}
    totals = {m: 0.0 for m in MODES}
    n_feasible = {m: 0 for m in MODES}

    for f in files:
        with open(f, encoding="utf-8") as fh:
            prob_info = json.load(fh)

        row = {}
        for mode in MODES:
            t0 = time.time()
            try:
                sol = baseline_greedy.greedyalgorithm(prob_info, timelimit=timelimit,
                                                       construction_mode=mode)
                result = check_feasibility(prob_info, sol)
            except Exception as exc:
                row[mode] = (False, None, time.time() - t0, str(exc))
                continue
            row[mode] = (result["feasible"], result.get("objective"), time.time() - t0, None)

        feasible_objs = {m: v[1] for m, v in row.items() if v[0] and v[1] is not None}
        best_mode = min(feasible_objs, key=feasible_objs.get) if feasible_objs else None
        if best_mode:
            wins[best_mode] += 1

        parts = []
        for mode in MODES:
            ok, obj, elapsed, err = row[mode]
            if err:
                parts.append(f"{mode}=CRASH({err[:30]})")
            elif not ok:
                parts.append(f"{mode}=INFEASIBLE")
            else:
                n_feasible[mode] += 1
                totals[mode] += obj
                tag = " *" if mode == best_mode else ""
                parts.append(f"{mode}={obj:,.0f}{tag} ({elapsed:.1f}s)")
        print(f"{f.name:16s}  " + "  ".join(parts))

    print("=" * 100)
    print(f"Wins (lowest objective among feasible modes): {wins}")
    for mode in MODES:
        if n_feasible[mode] > 0:
            print(f"  {mode:8s}: {n_feasible[mode]}/{len(files)} feasible, "
                  f"avg objective (feasible only) = {totals[mode] / n_feasible[mode]:,.0f}")
        else:
            print(f"  {mode:8s}: 0/{len(files)} feasible")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patterns", nargs="+",
                        help="instance JSON file(s), glob pattern(s), or directory(ies)")
    parser.add_argument("--timelimit", type=float, default=90.0,
                        help="wall-clock time limit per (instance, mode) in seconds (default: %(default)s)")
    args = parser.parse_args()
    run(args.patterns, args.timelimit)
