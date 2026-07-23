"""
A/B correctness + quality comparison: production xpress_reinsert.reinsert()
(fixed-timing-per-position candidates) vs the 2026-07-24 rewritten
cpsat_reinsert.reinsert() (interval vars + NoOverlap, free timing).

Reuses the same "build a real solution -> find the heaviest bay -> find a
genuinely mutually-conflicting hot cluster" pattern as
analysis/time_diversity_probe.py, since that script already confirmed the
underlying opportunity (time flexibility) is real and large on such
clusters. This script instead runs the ACTUAL production/prototype
reinsert() functions (not a hand-rolled enriched-candidate stand-in) and,
critically, re-verifies EVERY result against utils.check_feasibility on the
FULL solution (not just trusting either module's own internal conflict
model) before comparing objectives -- an infeasible result from either side
is reported as a hard failure, not silently ignored.

Usage:
    python analysis/cpsat_interval_vs_xpress_probe.py <instance.json> [...]
        [--timelimit 30] [--cluster-size 6] [--max-per-block 20]
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baseline_greedy
import cpsat_reinsert
import xpress_reinsert
from utils import Bay, Block, _bb_overlap, check_collisions, check_feasibility
from xpress_reinsert import _crane_conflict, _time_overlaps


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


def _find_hot_cluster(remove_ids, per_block, bays, blocks_data, cluster_size):
    conflict_count = Counter()
    partner_count: dict[int, Counter] = {bi: Counter() for bi in remove_ids}
    ids = list(per_block.keys())
    for a in range(len(ids)):
        bi = ids[a]
        for b in range(a + 1, len(ids)):
            bj = ids[b]
            found = False
            for (_, bay_i, cx_i, cy_i, oi_i, entry_i, exit_i) in per_block[bi]:
                if found:
                    break
                for (_, bay_j, cx_j, cy_j, oi_j, entry_j, exit_j) in per_block[bj]:
                    if bay_i != bay_j or not _time_overlaps(entry_i, exit_i, entry_j, exit_j):
                        continue
                    lx0_i, ly0_i, lx1_i, ly1_i = baseline_greedy._block_bbox(blocks_data[bi], oi_i)
                    lx0_j, ly0_j, lx1_j, ly1_j = baseline_greedy._block_bbox(blocks_data[bj], oi_j)
                    bb_i = (cx_i + lx0_i, cy_i + ly0_i, cx_i + lx1_i, cy_i + ly1_i)
                    bb_j = (cx_j + lx0_j, cy_j + ly0_j, cx_j + lx1_j, cy_j + ly1_j)
                    if not _bb_overlap(bb_i, bb_j):
                        continue
                    blk_i = Block(block_id=bi, block_data=blocks_data[bi], x=cx_i, y=cy_i, orient_idx=oi_i)
                    blk_j = Block(block_id=bj, block_data=blocks_data[bj], x=cx_j, y=cy_j, orient_idx=oi_j)
                    if (check_collisions(bays[bay_i], [blk_i, blk_j])
                            or _crane_conflict(bays[bay_i], blk_i, entry_i, exit_i, blk_j, entry_j, exit_j)):
                        conflict_count[bi] += 1
                        conflict_count[bj] += 1
                        partner_count[bi][bj] += 1
                        partner_count[bj][bi] += 1
                        found = True
                        break
    if not conflict_count:
        return []
    hot_bi = conflict_count.most_common(1)[0][0]
    cluster = [hot_bi]
    for bj, _ in partner_count[hot_bi].most_common():
        if len(cluster) >= cluster_size:
            break
        if bj not in cluster:
            cluster.append(bj)
    return cluster


def _score_solution(prob_info, blocks_data, assignments) -> dict:
    sol = {"operations": baseline_greedy._build_operations(list(assignments.values()))}
    return check_feasibility(prob_info, sol)


def run(instance_path: str, construction_timelimit: float, cluster_size: int, max_per_block: int) -> None:
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
    assignments = _build_assignments(sol)

    bay_areas = [bay.width * bay.height for bay in bays]
    avg_area = sum(bay_areas) / len(bays)
    bay_weights = [avg_area / a for a in bay_areas]
    bay_loads = [0.0] * len(bays)
    for a in assignments.values():
        bay_loads[a["bay_id"]] += blocks_data[a["block_id"]]["workload"]
    heaviest_bay = max(range(len(bays)), key=lambda j: bay_weights[j] * bay_loads[j])
    all_bay_ids = [a["block_id"] for a in assignments.values() if a["bay_id"] == heaviest_bay]

    trial_assignments = dict(assignments)
    for bid in all_bay_ids:
        trial_assignments.pop(bid, None)
    bay_placed, bay_schedule, bay_loads2 = baseline_greedy._rebuild_bay_state(
        trial_assignments, bays, blocks_data
    )

    per_block_full = {}
    for bi in all_bay_ids:
        cands = baseline_greedy._top_candidates_for_block(
            bi, blocks_data[bi], bays, bay_placed, bay_schedule, bay_loads2,
            w1, w2, w3, bay_weights, max_per_block, deadline=None,
        )
        if cands:
            per_block_full[bi] = cands

    cluster = _find_hot_cluster(all_bay_ids, per_block_full, bays, blocks_data, cluster_size)
    if len(cluster) < 2:
        print("Could not find a mutually-conflicting cluster of size >= 2 -- skipping instance.")
        return
    print(f"Heaviest bay {heaviest_bay}, hot cluster (K={len(cluster)}): {cluster}")

    # IMPORTANT: only remove the cluster itself from the ambient state, NOT
    # the whole heaviest bay -- the rest of that bay's blocks (all_bay_ids
    # minus cluster) must stay as real ambient occupants both for the
    # reinsert() calls below AND for the final full-solution feasibility
    # check, exactly matching how production code actually calls reinsert()
    # (only the removed batch is absent from bay_placed/bay_schedule).
    cluster_trial = dict(assignments)
    for bid in cluster:
        cluster_trial.pop(bid, None)
    bay_placed_c, bay_schedule_c, bay_loads_c = baseline_greedy._rebuild_bay_state(
        cluster_trial, bays, blocks_data
    )

    current_positions = {
        bid: (assignments[bid]["bay_id"], assignments[bid]["x"], assignments[bid]["y"],
             assignments[bid]["orient_idx"], assignments[bid]["entry_time"], assignments[bid]["exit_time"])
        for bid in cluster
    }

    results = {}
    for label, module in (("xpress", xpress_reinsert), ("cpsat", cpsat_reinsert)):
        t0 = time.time()
        partial = module.reinsert(
            cluster, blocks_data, bays, bay_placed_c, bay_schedule_c, bay_loads_c,
            w1, w2, w3, deadline=time.time() + 30, max_per_block=max_per_block,
            solve_time_limit=20, current_positions=current_positions,
        )
        elapsed = time.time() - t0
        if partial is None:
            print(f"[{label}] returned None ({elapsed:.1f}s) -- infeasible/unavailable/timed out.")
            results[label] = None
            continue
        full_trial = dict(cluster_trial)
        full_trial.update(partial)
        check = _score_solution(prob_info, blocks_data, full_trial)
        results[label] = check
        status = "FEASIBLE" if check["feasible"] else f"INFEASIBLE(stage={check['stage']})"
        obj_str = f"obj={check['objective']:,.1f}" if check["feasible"] else ""
        print(f"[{label}] {status}  {obj_str}  ({elapsed:.1f}s)")
        if not check["feasible"]:
            print(f"  !!! {label} produced a result that fails full check_feasibility -- "
                  f"first violations: {check['violations'][:3]}")

    if results.get("xpress") and results.get("cpsat"):
        if results["xpress"]["feasible"] and results["cpsat"]["feasible"]:
            delta = results["cpsat"]["objective"] - results["xpress"]["objective"]
            pct = 100 * delta / results["xpress"]["objective"] if results["xpress"]["objective"] else 0.0
            print(f"RESULT: xpress={results['xpress']['objective']:,.1f}  "
                  f"cpsat={results['cpsat']['objective']:,.1f}  delta={delta:+,.1f} ({pct:+.1f}%)")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instances", nargs="+", help="instance JSON file(s)")
    parser.add_argument("--timelimit", type=float, default=30.0)
    parser.add_argument("--cluster-size", type=int, default=6)
    parser.add_argument("--max-per-block", type=int, default=20)
    args = parser.parse_args()
    for inst in args.instances:
        run(inst, args.timelimit, args.cluster_size, args.max_per_block)
