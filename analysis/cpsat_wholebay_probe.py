"""
Wholebay-scale A/B: production xpress_reinsert.reinsert() vs the rewritten
cpsat_reinsert.reinsert(), both with restrict_bay_id set and the ENTIRE
heaviest bay's blocks as remove_ids -- exactly mirroring
baseline_greedy._select_removal_candidates(mode="wholebay") /
_improve's wholebay operator, the actual production scenario where the
2026-07-24 time_diversity_probe.py's dramatic (~95%+) improvement is
realistic (whole bay emptied simultaneously, no ambient anchor blocks left
in that bay at all -- unlike a small-K removal from an otherwise-intact
bay, which analysis/cpsat_interval_vs_xpress_probe.py just showed gets
basically NO benefit from free timing).

Usage:
    python analysis/cpsat_wholebay_probe.py <instance.json> [...]
        [--timelimit 30] [--max-per-block 40] [--solve-time-limit 60]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baseline_greedy
import cpsat_reinsert
import xpress_reinsert
from utils import Bay, check_feasibility


def _build_assignments(sol: dict) -> dict:
    assignments = {}
    for day_ops in sol["operations"].values():
        for op in day_ops:
            if op["type"] == "ENTRY":
                assignments[op["block_id"]] = {"block_id": op["block_id"], "bay_id": op["bay_id"],
                                               "x": op["x"], "y": op["y"], "orient_idx": op["orient_idx"]}
    for day, day_ops in sol["operations"].items():
        for op in day_ops:
            bid = op["block_id"]
            if bid in assignments:
                if op["type"] == "ENTRY":
                    assignments[bid]["entry_time"] = int(day)
                elif op["type"] == "EXIT":
                    assignments[bid]["exit_time"] = int(day)
    return assignments


def run(instance_path: str, construction_timelimit: float, max_per_block: int, solve_time_limit: float) -> None:
    with open(instance_path, encoding="utf-8") as f:
        prob_info = json.load(f)
    blocks_data = prob_info["blocks"]
    bays = [Bay.from_dict(d, i) for i, d in enumerate(prob_info["bays"])]
    w1 = prob_info.get("weights", {}).get("w1", 1.0)
    w2 = prob_info.get("weights", {}).get("w2", 1.0)
    w3 = prob_info.get("weights", {}).get("w3", 1.0)

    print(f"=== {prob_info.get('name')}  blocks={len(blocks_data)}  bays={len(bays)} ===")
    sol = baseline_greedy.greedyalgorithm(prob_info, timelimit=construction_timelimit)
    base_result = check_feasibility(prob_info, sol)
    if not base_result["feasible"]:
        print("Starting solution is not feasible -- aborting probe.")
        return
    print(f"Starting objective: {base_result['objective']:,.0f}")
    assignments = _build_assignments(sol)

    bay_areas = [bay.width * bay.height for bay in bays]
    avg_area = sum(bay_areas) / len(bays)
    bay_weights = [avg_area / a for a in bay_areas]
    bay_loads = [0.0] * len(bays)
    for a in assignments.values():
        bay_loads[a["bay_id"]] += blocks_data[a["block_id"]]["workload"]
    heaviest_bay = max(range(len(bays)), key=lambda j: bay_weights[j] * bay_loads[j])
    remove_ids = [a["block_id"] for a in assignments.values() if a["bay_id"] == heaviest_bay]
    print(f"Heaviest bay: {heaviest_bay}  K={len(remove_ids)}")

    trial_assignments = dict(assignments)
    for bid in remove_ids:
        trial_assignments.pop(bid, None)
    bay_placed, bay_schedule, bay_loads2 = baseline_greedy._rebuild_bay_state(
        trial_assignments, bays, blocks_data
    )

    current_positions = {
        bid: (assignments[bid]["bay_id"], assignments[bid]["x"], assignments[bid]["y"],
             assignments[bid]["orient_idx"], assignments[bid]["entry_time"], assignments[bid]["exit_time"])
        for bid in remove_ids
    }

    results = {}
    for label, module in (("xpress", xpress_reinsert), ("cpsat", cpsat_reinsert)):
        t0 = time.time()
        partial = module.reinsert(
            remove_ids, blocks_data, bays, bay_placed, bay_schedule, bay_loads2,
            w1, w2, w3, deadline=time.time() + solve_time_limit + 30,
            max_per_block=max_per_block, solve_time_limit=solve_time_limit,
            restrict_bay_id=heaviest_bay, current_positions=current_positions,
        )
        elapsed = time.time() - t0
        if partial is None:
            print(f"[{label}] returned None ({elapsed:.1f}s) -- infeasible/unavailable/timed out.")
            results[label] = None
            continue
        full_trial = dict(trial_assignments)
        full_trial.update(partial)
        full_sol = {"operations": baseline_greedy._build_operations(list(full_trial.values()))}
        check = check_feasibility(prob_info, full_sol)
        results[label] = check
        status = "FEASIBLE" if check["feasible"] else f"INFEASIBLE(stage={check['stage']})"
        obj_str = f"obj={check['objective']:,.1f}" if check["feasible"] else ""
        print(f"[{label}] {status}  {obj_str}  ({elapsed:.1f}s)")
        if not check["feasible"]:
            print(f"  !!! violations: {check['violations'][:3]}")

    if results.get("xpress") and results.get("cpsat") and results["xpress"]["feasible"] and results["cpsat"]["feasible"]:
        delta = results["cpsat"]["objective"] - results["xpress"]["objective"]
        pct = 100 * delta / results["xpress"]["objective"] if results["xpress"]["objective"] else 0.0
        vs_start = 100 * (results["xpress"]["objective"] - base_result["objective"]) / base_result["objective"] if base_result["objective"] else 0.0
        print(f"RESULT: start={base_result['objective']:,.1f}  xpress={results['xpress']['objective']:,.1f}  "
              f"cpsat={results['cpsat']['objective']:,.1f}  delta(cpsat-xpress)={delta:+,.1f} ({pct:+.1f}%)")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instances", nargs="+", help="instance JSON file(s)")
    parser.add_argument("--timelimit", type=float, default=30.0)
    parser.add_argument("--max-per-block", type=int, default=40)
    parser.add_argument("--solve-time-limit", type=float, default=60.0)
    args = parser.parse_args()
    for inst in args.instances:
        run(inst, args.timelimit, args.max_per_block, args.solve_time_limit)
