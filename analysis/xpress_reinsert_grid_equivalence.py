"""
OLD (naive O(K^2 x candidates^2)) vs NEW (grid-bucketed via
baseline_greedy.bucket_candidate_pairs_by_grid) pairwise-conflict-set
equivalence check for xpress_reinsert.reinsert()'s 2026-07-24 rewrite.

Reproduces the OLD pairwise loop verbatim (as it was before this session's
rewrite) as a standalone function here, and compares its output -- the
exact SET of (block, candidate) pairs judged to conflict -- against the
NEW grid-narrowed code path, on real candidate pools pulled from real
congested batches at several K. This mirrors the same methodology used for
the 2026-07-23 AABB pre-filter change (see notes/algorithm_overview.md):
an OLD/NEW SET comparison, not just "does the final objective look
similar" -- a silent narrowing bug would show up here even if it happened
to not matter for one particular batch's optimal solution.

Usage:
    python analysis/xpress_reinsert_grid_equivalence.py <instance.json> [...]
        [--timelimit 30] [--k-values 6,20,46]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baseline_greedy
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


def _old_conflict_set(per_block, blocks_data, bays, bb):
    """Verbatim reproduction of the PRE-2026-07-24 pairwise loop."""
    conflicts = set()
    ids = list(per_block.keys())
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            bi, bj = ids[a], ids[b]
            for ci, (_, bay_i, cx_i, cy_i, oi_i, entry_i, exit_i) in enumerate(per_block[bi]):
                for cj, (_, bay_j, cx_j, cy_j, oi_j, entry_j, exit_j) in enumerate(per_block[bj]):
                    if (bay_i != bay_j or not _time_overlaps(entry_i, exit_i, entry_j, exit_j)
                            or not _bb_overlap(bb[bi][ci], bb[bj][cj])):
                        continue
                    blk_i = Block(block_id=bi, block_data=blocks_data[bi],
                                 x=cx_i, y=cy_i, orient_idx=oi_i)
                    blk_j = Block(block_id=bj, block_data=blocks_data[bj],
                                 x=cx_j, y=cy_j, orient_idx=oi_j)
                    if (check_collisions(bays[bay_i], [blk_i, blk_j])
                            or _crane_conflict(bays[bay_i], blk_i, entry_i, exit_i,
                                               blk_j, entry_j, exit_j)):
                        pair = frozenset({(bi, ci), (bj, cj)})
                        conflicts.add(pair)
    return conflicts


def _new_conflict_set(per_block, blocks_data, bays, bb):
    """Mirrors the NEW (2026-07-24) grid-bucketed pairwise loop in
    xpress_reinsert.reinsert() exactly."""
    conflicts = set()
    by_bay = {}
    for bi, cands in per_block.items():
        for ci in range(len(cands)):
            bay_id = cands[ci][1]
            by_bay.setdefault(bay_id, []).append(((bi, ci), bb[bi][ci]))

    for bay_id, entries in by_bay.items():
        close_pairs = baseline_greedy.bucket_candidate_pairs_by_grid(entries)
        for (bi, ci), (bj, cj) in close_pairs:
            if bi == bj:
                continue
            _, bay_i, cx_i, cy_i, oi_i, entry_i, exit_i = per_block[bi][ci]
            _, bay_j, cx_j, cy_j, oi_j, entry_j, exit_j = per_block[bj][cj]
            if not _time_overlaps(entry_i, exit_i, entry_j, exit_j):
                continue
            blk_i = Block(block_id=bi, block_data=blocks_data[bi], x=cx_i, y=cy_i, orient_idx=oi_i)
            blk_j = Block(block_id=bj, block_data=blocks_data[bj], x=cx_j, y=cy_j, orient_idx=oi_j)
            if (check_collisions(bays[bay_i], [blk_i, blk_j])
                    or _crane_conflict(bays[bay_i], blk_i, entry_i, exit_i, blk_j, entry_j, exit_j)):
                conflicts.add(frozenset({(bi, ci), (bj, cj)}))
    return conflicts


def _check_batch(label, per_block, blocks_data, bays):
    bb = {}
    for bi, cands in per_block.items():
        blk_bbs = []
        for (_, _, cx, cy, oi, _, _) in cands:
            lx0, ly0, lx1, ly1 = baseline_greedy._block_bbox(blocks_data[bi], oi)
            blk_bbs.append((cx + lx0, cy + ly0, cx + lx1, cy + ly1))
        bb[bi] = blk_bbs

    t0 = time.time()
    old_set = _old_conflict_set(per_block, blocks_data, bays, bb)
    t_old = time.time() - t0

    t0 = time.time()
    new_set = _new_conflict_set(per_block, blocks_data, bays, bb)
    t_new = time.time() - t0

    n_cands = sum(len(v) for v in per_block.values())
    match = old_set == new_set
    print(f"  [{label}] K={len(per_block)} total_candidates={n_cands} "
          f"old_conflicts={len(old_set)} ({t_old:.3f}s)  new_conflicts={len(new_set)} ({t_new:.3f}s)  "
          f"{'MATCH' if match else 'MISMATCH !!!'}")
    if not match:
        only_old = old_set - new_set
        only_new = new_set - old_set
        print(f"    only in OLD (missed by new): {list(only_old)[:5]}")
        print(f"    only in NEW (spurious): {list(only_new)[:5]}")
    return match


def run(instance_path: str, construction_timelimit: float, k_values: list[int]) -> None:
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

    trial_assignments = dict(assignments)
    for bid in all_bay_ids:
        trial_assignments.pop(bid, None)
    bay_placed, bay_schedule, bay_loads2 = baseline_greedy._rebuild_bay_state(
        trial_assignments, bays, blocks_data
    )

    all_matched = True
    for k in k_values:
        batch = all_bay_ids[:k]
        if len(batch) < 2:
            continue
        per_block = {}
        for bi in batch:
            cands = baseline_greedy._top_candidates_for_block(
                bi, blocks_data[bi], bays, bay_placed, bay_schedule, bay_loads2,
                w1, w2, w3, bay_weights, max_per_block=20, deadline=None,
            )
            if cands:
                per_block[bi] = cands
        if len(per_block) < 2:
            continue
        matched = _check_batch(f"K={k}", per_block, blocks_data, bays)
        all_matched = all_matched and matched
    print(f"  ALL MATCHED: {all_matched}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instances", nargs="+", help="instance JSON file(s)")
    parser.add_argument("--timelimit", type=float, default=30.0)
    parser.add_argument("--k-values", type=str, default="6,20,46")
    args = parser.parse_args()
    k_values = [int(x) for x in args.k_values.split(",")]
    for inst in args.instances:
        run(inst, args.timelimit, k_values)
