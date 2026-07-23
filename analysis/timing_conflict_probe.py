"""
Quick, cheap probe for "would giving CP-SAT real time-variables (interval
vars + NoOverlap/cumulative) actually help, instead of xpress_reinsert.py's
current fully-static-candidate model?"

Motivation (2026-07-24 discussion): xpress_reinsert.reinsert()'s candidate
pool for a block only ever contains ONE (entry, exit) per (bay, x, y,
orient) -- _top_candidates_for_block calls _find_earliest_slot exactly once
per position, taking the single EARLIEST feasible slot there. So if two
removed blocks' candidates conflict in time at the same (or overlapping)
position, the joint MIP has NO way to express "place bi there too, just
after bj leaves" -- that alternative candidate simply was never generated.
A model where entry time is a real decision variable (CP-SAT interval var +
NoOverlap/cumulative) would resolve exactly this class of conflict for
free, without needing it pre-enumerated.

This script does NOT build or solve any MIP. It reconstructs the exact same
candidate pool + pairwise-conflict predicate xpress_reinsert.reinsert() uses
(same _top_candidates_for_block call, same check_collisions/_crane_conflict
combination), then for every conflicting pair (candidate_i, candidate_j) it
additionally asks: keeping candidate_i's OWN (bay, x, y, orient) fixed,
does _find_earliest_slot find ANY feasible slot for it starting at or after
candidate_j's exit_time, against the ambient (non-batch) bay state? If yes
(in either direction), this specific conflict is a pure "our candidate list
only offered one timing" artifact -- exactly what a time-as-variable model
would resolve automatically. If neither direction works, the conflict is
more fundamentally spatial (no timing shift alone fixes it).

The fraction of "resolvable by re-timing alone" conflicts is a rough,
cheap proxy for how much upside a time-variable CP-SAT reformulation could
plausibly have on THIS instance's actual congestion pattern -- before
spending time building the real thing.

Usage:
    python analysis/timing_conflict_probe.py <instance.json> [--timelimit 30] [--max-per-block 20]
"""
import argparse
import json
import sys
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


def run(instance_path: str, construction_timelimit: float, max_per_block: int) -> None:
    with open(instance_path, encoding="utf-8") as f:
        prob_info = json.load(f)

    blocks_data = prob_info["blocks"]
    bays_data = prob_info["bays"]
    bays = [Bay.from_dict(d, i) for i, d in enumerate(bays_data)]

    w1 = prob_info.get("weights", {}).get("w1", 1.0)
    w2 = prob_info.get("weights", {}).get("w2", 1.0)
    w3 = prob_info.get("weights", {}).get("w3", 1.0)

    print(f"Instance: {prob_info.get('name')}  blocks={len(blocks_data)}  bays={len(bays)}")
    print(f"Building a real starting solution via greedyalgorithm() first (timelimit={construction_timelimit}s) ...")
    sol = baseline_greedy.greedyalgorithm(prob_info, timelimit=construction_timelimit)
    base_result = check_feasibility(prob_info, sol)
    if not base_result["feasible"]:
        print("Starting solution is not feasible -- aborting probe.")
        return
    print(f"Starting objective: {base_result['objective']:,.0f}\n")

    assignments = _build_assignments(sol)

    bay_areas = [bay.width * bay.height for bay in bays]
    avg_area = sum(bay_areas) / len(bays)
    bay_weights = [avg_area / a for a in bay_areas]
    bay_loads = [0.0] * len(bays)
    for a in assignments.values():
        bay_loads[a["bay_id"]] += blocks_data[a["block_id"]]["workload"]
    heaviest_bay = max(range(len(bays)), key=lambda j: bay_weights[j] * bay_loads[j])
    remove_ids = [a["block_id"] for a in assignments.values() if a["bay_id"] == heaviest_bay]
    print(f"Heaviest bay: {heaviest_bay}  ({len(remove_ids)} blocks currently placed there)\n")

    trial_assignments = dict(assignments)
    for bid in remove_ids:
        trial_assignments.pop(bid, None)
    bay_placed, bay_schedule, bay_loads2 = baseline_greedy._rebuild_bay_state(
        trial_assignments, bays, blocks_data
    )

    print(f"Generating candidates for {len(remove_ids)} block(s), max_per_block={max_per_block} ...")
    per_block = {}
    for bi in remove_ids:
        cands = baseline_greedy._top_candidates_for_block(
            bi, blocks_data[bi], bays, bay_placed, bay_schedule, bay_loads2,
            w1, w2, w3, bay_weights, max_per_block, deadline=None,
        )
        if cands:
            per_block[bi] = cands
    print(f"{len(per_block)}/{len(remove_ids)} block(s) have >=1 candidate\n")

    total_conflicts = 0
    resolvable_by_retiming = 0
    identical_position_conflicts = 0

    ids = list(per_block.keys())
    for a in range(len(ids)):
        bi = ids[a]
        for b in range(a + 1, len(ids)):
            bj = ids[b]
            for ci, (_, bay_i, cx_i, cy_i, oi_i, entry_i, exit_i) in enumerate(per_block[bi]):
                for cj, (_, bay_j, cx_j, cy_j, oi_j, entry_j, exit_j) in enumerate(per_block[bj]):
                    if bay_i != bay_j:
                        continue
                    lx0_i, ly0_i, lx1_i, ly1_i = baseline_greedy._block_bbox(blocks_data[bi], oi_i)
                    lx0_j, ly0_j, lx1_j, ly1_j = baseline_greedy._block_bbox(blocks_data[bj], oi_j)
                    bb_i = (cx_i + lx0_i, cy_i + ly0_i, cx_i + lx1_i, cy_i + ly1_i)
                    bb_j = (cx_j + lx0_j, cy_j + ly0_j, cx_j + lx1_j, cy_j + ly1_j)
                    if not _time_overlaps(entry_i, exit_i, entry_j, exit_j) or not _bb_overlap(bb_i, bb_j):
                        continue
                    blk_i = Block(block_id=bi, block_data=blocks_data[bi], x=cx_i, y=cy_i, orient_idx=oi_i)
                    blk_j = Block(block_id=bj, block_data=blocks_data[bj], x=cx_j, y=cy_j, orient_idx=oi_j)
                    if not (check_collisions(bays[bay_i], [blk_i, blk_j])
                            or _crane_conflict(bays[bay_i], blk_i, entry_i, exit_i, blk_j, entry_j, exit_j)):
                        continue

                    # This is a REAL conflict xpress_reinsert.reinsert() would
                    # add a y_i + y_j <= 1 constraint for. Now ask: keeping
                    # EACH candidate's own (bay, x, y, orient) fixed, does a
                    # later feasible slot exist against the ambient
                    # (non-batch) bay state that would clear THIS pair?
                    total_conflicts += 1
                    if (bay_i, cx_i, cy_i, oi_i) == (bay_j, cx_j, cy_j, oi_j):
                        identical_position_conflicts += 1

                    r_time_i = blocks_data[bi]["release_time"]
                    proc_i = blocks_data[bi]["processing_time"]
                    alt_i_entry, _ = baseline_greedy._find_earliest_slot(
                        blk_i, bays[bay_i], bay_placed[bay_i], bay_schedule[bay_i],
                        r_time=max(r_time_i, exit_j), proc=proc_i, deadline=None,
                    )
                    r_time_j = blocks_data[bj]["release_time"]
                    proc_j = blocks_data[bj]["processing_time"]
                    alt_j_entry, _ = baseline_greedy._find_earliest_slot(
                        blk_j, bays[bay_j], bay_placed[bay_j], bay_schedule[bay_j],
                        r_time=max(r_time_j, exit_i), proc=proc_j, deadline=None,
                    )
                    if alt_i_entry is not None or alt_j_entry is not None:
                        resolvable_by_retiming += 1

    print(f"Total confirmed pairwise conflicts (same bay, AABB+time overlap, real "
          f"collision/crane conflict): {total_conflicts}")
    if total_conflicts == 0:
        print("No conflicts found in this batch -- try a larger/more congested bay or lower max_per_block.")
        return
    print(f"  identical (bay,x,y,orient) for both candidates (pure scheduling, no position "
          f"choice involved at all): {identical_position_conflicts} "
          f"({100*identical_position_conflicts/total_conflicts:.1f}%)")
    print(f"  resolvable by re-timing ONE side alone, same position, against ambient state: "
          f"{resolvable_by_retiming} ({100*resolvable_by_retiming/total_conflicts:.1f}%)")
    print(f"  -> {'MEANINGFUL' if resolvable_by_retiming/total_conflicts > 0.2 else 'SMALL'} fraction of "
          f"conflicts look like they're purely a missing-time-diversity artifact of the static "
          f"candidate pool, not genuine spatial packing conflicts.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instance", help="instance JSON file")
    parser.add_argument("--timelimit", type=float, default=30.0,
                        help="timelimit for building the starting solution (default: %(default)s)")
    parser.add_argument("--max-per-block", type=int, default=20,
                        help="candidates per block, same default as xpress_reinsert.reinsert() (default: %(default)s)")
    args = parser.parse_args()
    run(args.instance, args.timelimit, args.max_per_block)
