"""
Feasibility/benefit probe for "bay-independent exact scheduling": is it even
tractable to jointly re-optimize ALL of one bay's currently-placed blocks in
a single Xpress MIP (not just a small K<=12 batch), given a generous time
budget? This does NOT wire anything into the main pipeline -- it just
measures whether xpress_reinsert.reinsert() can complete on a whole-bay-size
K, and whether doing so actually improves the objective, before deciding if
building this into _improve is worth the effort.

Usage:
    python analysis/bay_mip_probe.py <instance.json> [--timelimit 90] [--mip-budget 300] [--max-per-block 8]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baseline_greedy
import xpress_reinsert
from utils import check_feasibility


def run(instance_path: str, construction_timelimit: float, mip_budget: float, max_per_block: int) -> None:
    with open(instance_path, encoding="utf-8") as f:
        prob_info = json.load(f)

    blocks_data = prob_info["blocks"]
    bays_data = prob_info["bays"]
    from utils import Bay
    bays = [Bay.from_dict(d, i) for i, d in enumerate(bays_data)]

    w1 = prob_info.get("weights", {}).get("w1", 1.0)
    w2 = prob_info.get("weights", {}).get("w2", 1.0)
    w3 = prob_info.get("weights", {}).get("w3", 1.0)

    print(f"Instance: {prob_info.get('name')}  blocks={len(blocks_data)}  bays={len(bays)}")
    print("Building a real starting solution via greedyalgorithm() first "
          f"(timelimit={construction_timelimit}s) ...")
    sol = baseline_greedy.greedyalgorithm(prob_info, timelimit=construction_timelimit)
    base_result = check_feasibility(prob_info, sol)
    if not base_result["feasible"]:
        print("Starting solution is not feasible -- aborting probe.")
        return
    print(f"Starting objective: {base_result['objective']:,.0f} "
          f"(obj1={base_result['obj1']:.0f} obj2={base_result['obj2']:.0f} obj3={base_result['obj3']:.0f})")

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

    bay_placed, bay_schedule, bay_loads = baseline_greedy._rebuild_bay_state(
        assignments, bays, blocks_data
    )

    # Weighted by bay area (u_j = avg_area / area_j), matching Z2's own
    # definition and _select_removal_candidates' "balance" mode -- a raw
    # workload max would pick a bay that's merely large, not necessarily
    # the one actually contributing most to Z2's imbalance term.
    bay_areas = [bay.width * bay.height for bay in bays]
    avg_area = sum(bay_areas) / len(bays)
    bay_weights = [avg_area / a for a in bay_areas]
    heaviest_bay = max(range(len(bays)), key=lambda j: bay_weights[j] * bay_loads[j])
    remove_ids = [a["block_id"] for a in assignments.values() if a["bay_id"] == heaviest_bay]
    print(f"\nHeaviest bay (by weighted load): {heaviest_bay}  "
          f"(load={bay_loads[heaviest_bay]:.0f}, weight={bay_weights[heaviest_bay]:.2f}, "
          f"weighted={bay_weights[heaviest_bay] * bay_loads[heaviest_bay]:.0f}, "
          f"{len(remove_ids)} blocks currently placed there)")

    # Remove them from the bay state so reinsert() generates fresh candidates.
    trial_assignments = dict(assignments)
    for bid in remove_ids:
        trial_assignments.pop(bid, None)
    bay_placed2, bay_schedule2, bay_loads2 = baseline_greedy._rebuild_bay_state(
        trial_assignments, bays, blocks_data
    )

    current_positions = {
        bid: (assignments[bid]["bay_id"], assignments[bid]["x"], assignments[bid]["y"],
             assignments[bid]["orient_idx"], assignments[bid]["entry_time"], assignments[bid]["exit_time"])
        for bid in remove_ids
    }

    print(f"Calling xpress_reinsert.reinsert() with K={len(remove_ids)}, "
          f"max_per_block={max_per_block}, budget={mip_budget}s, "
          f"current_positions guaranteed ...")
    t0 = time.time()
    partial = xpress_reinsert.reinsert(
        remove_ids, blocks_data, bays,
        bay_placed2, bay_schedule2, bay_loads2,
        w1, w2, w3, deadline=time.time() + mip_budget,
        max_per_block=max_per_block, solve_time_limit=int(mip_budget),
        restrict_bay_id=heaviest_bay,
        current_positions=current_positions,
    )
    elapsed = time.time() - t0
    if partial is None:
        print(f"Result: FAILED (returned None) after {elapsed:.1f}s -- "
              f"Xpress unavailable, no candidates, or solve infeasible/timed out.")
        return

    n_moved = sum(
        1 for bid in remove_ids
        if (partial[bid]["x"], partial[bid]["y"], partial[bid]["orient_idx"],
            partial[bid]["entry_time"], partial[bid]["exit_time"])
        != (assignments[bid]["x"], assignments[bid]["y"], assignments[bid]["orient_idx"],
            assignments[bid]["entry_time"], assignments[bid]["exit_time"])
    )
    print(f"  {n_moved}/{len(remove_ids)} block(s) actually moved from their starting position "
          f"({'NOT just a no-op' if n_moved > 0 else 'a no-op -- solver kept everyone in place'})")

    trial_assignments.update(partial)
    trial_sol = {"operations": baseline_greedy._build_operations(list(trial_assignments.values()))}
    trial_result = check_feasibility(prob_info, trial_sol)
    print(f"Result: SUCCESS after {elapsed:.1f}s")
    print(f"  feasible={trial_result['feasible']}")
    if trial_result["feasible"]:
        print(f"  new objective: {trial_result['objective']:,.0f} "
              f"(obj1={trial_result['obj1']:.0f} obj2={trial_result['obj2']:.0f} obj3={trial_result['obj3']:.0f})")
        print(f"  vs starting:   {base_result['objective']:,.0f} "
              f"(obj1={base_result['obj1']:.0f} obj2={base_result['obj2']:.0f} obj3={base_result['obj3']:.0f})")
        delta = trial_result["objective"] - base_result["objective"]
        print(f"  delta: {delta:+,.0f} ({'IMPROVED' if delta < 0 else 'WORSE' if delta > 0 else 'no change'})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instance", help="instance JSON file")
    parser.add_argument("--timelimit", type=float, default=90.0,
                        help="timelimit for building the starting solution (default: %(default)s)")
    parser.add_argument("--mip-budget", type=float, default=300.0,
                        help="wall-clock budget given to the whole-bay MIP solve (default: %(default)s)")
    parser.add_argument("--max-per-block", type=int, default=8,
                        help="candidates per block for the MIP (default: %(default)s)")
    args = parser.parse_args()
    run(args.instance, args.timelimit, args.mip_budget, args.max_per_block)
