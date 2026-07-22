"""
Empirical A/B/C comparison of Phase-1 priority rules (atc / slack / edd)
across many instances, holding everything else (repair, Phase-3 improve,
timelimit) fixed.

Motivation: the ATC index implemented in baseline_greedy._atc_priority is a
static per-block approximation (t = each block's own release_time, w_j=1)
of the classic dynamic ATC dispatch rule. There's no theoretical guarantee
this approximation beats plain min-slack-first (MST) on this problem, since
the multi-bay/spatial constraints here differ from the single-machine
weighted-tardiness setting ATC was designed for. This script settles it
empirically instead of by argument: same instances, same timelimit, same
Phase 2/3 machinery, only the Phase-1 sort key changes.

Usage:
    python analysis/priority_rule_compare.py <instance.json | glob | dir> [...] [--timelimit 15]
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

RULES = ["edd", "atc", "slack", "regret", "area", "area_slack"]


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

    print(f"Found {len(files)} instance file(s). timelimit={timelimit}s per (instance, rule).")
    print("=" * 100)

    wins = {r: 0 for r in RULES}
    totals = {r: 0.0 for r in RULES}
    n_feasible = {r: 0 for r in RULES}

    for f in files:
        with open(f, encoding="utf-8") as fh:
            prob_info = json.load(fh)

        row = {}
        for rule in RULES:
            t0 = time.time()
            try:
                sol = baseline_greedy.greedyalgorithm(prob_info, timelimit=timelimit,
                                                       priority_rule=rule)
                result = check_feasibility(prob_info, sol)
            except Exception as exc:
                row[rule] = (False, None, time.time() - t0, str(exc))
                continue
            row[rule] = (result["feasible"], result.get("objective"), time.time() - t0, None)

        feasible_objs = {r: v[1] for r, v in row.items() if v[0] and v[1] is not None}
        best_rule = min(feasible_objs, key=feasible_objs.get) if feasible_objs else None
        if best_rule:
            wins[best_rule] += 1

        parts = []
        for rule in RULES:
            ok, obj, elapsed, err = row[rule]
            if err:
                parts.append(f"{rule}=CRASH({err[:30]})")
            elif not ok:
                parts.append(f"{rule}=INFEASIBLE")
            else:
                n_feasible[rule] += 1
                totals[rule] += obj
                tag = " *" if rule == best_rule else ""
                parts.append(f"{rule}={obj:,.0f}{tag}")
        print(f"{f.name:16s}  " + "  ".join(parts))

    print("=" * 100)
    print(f"Wins (lowest objective among feasible rules): {wins}")
    for rule in RULES:
        if n_feasible[rule] > 0:
            print(f"  {rule:6s}: {n_feasible[rule]}/{len(files)} feasible, "
                  f"avg objective (feasible only) = {totals[rule] / n_feasible[rule]:,.0f}")
        else:
            print(f"  {rule:6s}: 0/{len(files)} feasible")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patterns", nargs="+",
                        help="instance JSON file(s), glob pattern(s), or directory(ies)")
    parser.add_argument("--timelimit", type=float, default=15.0,
                        help="wall-clock time limit per (instance, rule) in seconds (default: %(default)s)")
    args = parser.parse_args()
    run(args.patterns, args.timelimit)
