"""
Validation for xpress_reinsert.reinsert()'s 2026-07-24 geometry_cache
parameter: (A) correctness -- repeated calls on the SAME candidate batch
must return IDENTICAL results whether or not a shared cache is used across
them (a cache is a pure memoization of a provably time-independent fact,
so it must never change what gets returned, only how fast); (B) a rough
real-world speed signal -- run baseline_greedy._improve() with vs without
geometry_cache on the same starting solution/seed and compare wall time and
rounds achieved (NOT expected to reach bit-identical objectives, since a
cheaper per-round cost lets more ALNS rounds fit in the same time budget,
changing the exploration path -- that's the intended effect, not a bug).

Usage:
    python analysis/geometry_cache_probe.py <instance.json> [...]
        [--timelimit 30] [--cluster-size 6] [--improve-timelimit 60]
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baseline_greedy
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


def _experiment_a(cluster, blocks_data, bays, bay_placed, bay_schedule, bay_loads,
                  w1, w2, w3, current_positions, n_repeats=5):
    print(f"--- Experiment A: correctness (repeat same K={len(cluster)} batch {n_repeats}x) ---")

    no_cache_results = []
    t0 = time.time()
    for _ in range(n_repeats):
        r = xpress_reinsert.reinsert(
            cluster, blocks_data, bays, bay_placed, bay_schedule, bay_loads,
            w1, w2, w3, deadline=None, max_per_block=20, solve_time_limit=10,
            current_positions=current_positions, geometry_cache=None,
        )
        no_cache_results.append(r)
    t_no_cache = time.time() - t0

    shared_cache: dict = {}
    cache_results = []
    t0 = time.time()
    for _ in range(n_repeats):
        r = xpress_reinsert.reinsert(
            cluster, blocks_data, bays, bay_placed, bay_schedule, bay_loads,
            w1, w2, w3, deadline=None, max_per_block=20, solve_time_limit=10,
            current_positions=current_positions, geometry_cache=shared_cache,
        )
        cache_results.append(r)
    t_cache = time.time() - t0

    all_none = all(r is None for r in no_cache_results + cache_results)
    if all_none:
        print("  (all repeats returned None -- batch infeasible for this call shape, skipping)")
        return
    match = all(r == no_cache_results[0] for r in no_cache_results) and \
        all(r == no_cache_results[0] for r in cache_results)
    print(f"  no_cache: {n_repeats}x in {t_no_cache:.3f}s")
    print(f"  cache:    {n_repeats}x in {t_cache:.3f}s (cache entries: {len(shared_cache)})")
    print(f"  IDENTICAL across all {2*n_repeats} calls: {match}")
    if not match:
        print("  !!! MISMATCH -- geometry_cache changed the result, this is a real bug")


def _experiment_b(prob_info, sol, assignments, bays, blocks_data, w1, w2, w3,
                  t_start_placeholder, improve_timelimit, seed=0):
    print(f"--- Experiment B: real _improve() run, with vs without geometry_cache "
          f"(timelimit={improve_timelimit}s, seed={seed}) ---")
    base_result = check_feasibility(prob_info, sol)

    for label, cache in (("without cache", None), ("with cache", {})):
        t0 = time.time()
        result = baseline_greedy._improve(
            prob_info, dict(assignments), bays, blocks_data,
            w1, w2, w3, t0, improve_timelimit, seed=seed,
            known_result=base_result, geometry_cache=cache,
        )
        elapsed = time.time() - t0
        final_sol = {"operations": baseline_greedy._build_operations(list(result.values()))}
        final = check_feasibility(prob_info, final_sol)
        cache_size = len(cache) if cache is not None else None
        print(f"  [{label}] elapsed={elapsed:.1f}s  feasible={final['feasible']}  "
              f"objective={final.get('objective')}  cache_entries={cache_size}")


def run(instance_path: str, construction_timelimit: float, cluster_size: int,
       improve_timelimit: float) -> None:
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
        print("Starting solution is not feasible -- aborting.")
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

    cluster_trial = dict(assignments)
    for bid in all_bay_ids:
        cluster_trial.pop(bid, None)
    bay_placed, bay_schedule, bay_loads2 = baseline_greedy._rebuild_bay_state(
        cluster_trial, bays, blocks_data
    )
    per_block_full = {}
    for bi in all_bay_ids:
        cands = baseline_greedy._top_candidates_for_block(
            bi, blocks_data[bi], bays, bay_placed, bay_schedule, bay_loads2,
            w1, w2, w3, bay_weights, max_per_block=20, deadline=None,
        )
        if cands:
            per_block_full[bi] = cands
    cluster = _find_hot_cluster(all_bay_ids, per_block_full, bays, blocks_data, cluster_size)
    if len(cluster) >= 2:
        cluster_trial2 = dict(assignments)
        for bid in cluster:
            cluster_trial2.pop(bid, None)
        bay_placed_c, bay_schedule_c, bay_loads_c = baseline_greedy._rebuild_bay_state(
            cluster_trial2, bays, blocks_data
        )
        current_positions = {
            bid: (assignments[bid]["bay_id"], assignments[bid]["x"], assignments[bid]["y"],
                 assignments[bid]["orient_idx"], assignments[bid]["entry_time"], assignments[bid]["exit_time"])
            for bid in cluster
        }
        _experiment_a(cluster, blocks_data, bays, bay_placed_c, bay_schedule_c, bay_loads_c,
                     w1, w2, w3, current_positions)
    else:
        print("Could not find a hot cluster for Experiment A -- skipping.")

    _experiment_b(prob_info, sol, assignments, bays, blocks_data, w1, w2, w3,
                 None, improve_timelimit)
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instances", nargs="+", help="instance JSON file(s)")
    parser.add_argument("--timelimit", type=float, default=30.0)
    parser.add_argument("--cluster-size", type=int, default=6)
    parser.add_argument("--improve-timelimit", type=float, default=60.0)
    args = parser.parse_args()
    for inst in args.instances:
        run(inst, args.timelimit, args.cluster_size, args.improve_timelimit)
