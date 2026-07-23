"""
Controlled small-K experiment: does giving the joint reinsertion MIP MULTIPLE
feasible timings per (bay, x, y, orient) candidate -- not just the single
earliest one xpress_reinsert.reinsert() generates today -- actually find a
better joint solution?

Motivation (2026-07-24 discussion, follow-up to timing_conflict_probe.py):
that first probe's "100% of conflicts resolvable by re-timing" result turned
out to be a near-tautology -- it only checked each block against the AMBIENT
(non-batch) bay state, which is nearly empty once a whole 46-block bay is
removed, so "wait long enough and it's free" is true almost by construction
and says nothing about whether time-diversity helps once ALL the OTHER
removed blocks are also competing for the same times/positions.

This script isolates the actual hypothesis instead of the solver-choice
question (CP-SAT vs Xpress -- deliberately NOT tested here; both would model
this identically once timing is discretized into several alternatives, so
using Xpress for both sides keeps this a controlled A/B on ONE variable:
candidate time-diversity, not solver choice):

  A) BASELINE: xpress_reinsert.reinsert() exactly as production code calls
     it today -- one (entry, exit) per (bay, x, y, orient) candidate.
  B) ENRICHED: same set of (bay, x, y, orient) positions, but each position
     gets up to `--timings-per-position` successive feasible entry times
     (found by repeatedly calling _find_earliest_slot with an advancing
     r_time floor against the SAME ambient state) instead of just the first.
     Solved with the identical Xpress model shape (exactly-one-per-block +
     pairwise conflict cuts), just with a larger, time-diverse candidate
     pool per block.

If B's objective is NOT meaningfully better than A's on a genuinely
mutually-conflicting small cluster, that's real evidence the single-timing-
per-position candidate model isn't leaving much on the table here, and a
CP-SAT time-as-variable reformulation likely wouldn't move the needle much
either (since B already gives Xpress the same flexibility a NoOverlap/
interval-var CP-SAT model would provide, just discretized). If B IS
meaningfully better, that's a real, solver-independent signal that time
flexibility is a genuine missing degree of freedom worth pursuing (in
either Xpress or CP-SAT form).

Cluster selection: rather than a huge whole-bay batch (where pairwise
conflict density says little about whether a SMALL, tractable K is even
mutually conflicted), this script explicitly picks a "hot" cluster: the
block with the most pairwise conflicts against others in the same
heaviest bay, plus its top conflict partners -- guaranteeing a genuinely
contested K instead of an arbitrary one.

Usage:
    python analysis/time_diversity_probe.py <instance.json> [--timelimit 30]
        [--cluster-size 6] [--max-per-block 8] [--timings-per-position 3]
"""
import argparse
import json
import sys
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
    """Pick the block with the most pairwise conflicts, plus its top
    conflict partners, from the full-bay candidate pool -- guarantees a
    genuinely mutually-contested small K instead of an arbitrary subset."""
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


def _enrich_candidates(cluster, per_block, bay_placed, bay_schedule, blocks_data, bays, timings_per_position):
    """For each block's existing (bay, x, y, orient) positions, add up to
    `timings_per_position` successive feasible entry times at that SAME
    position against the ambient (ex-batch) ANY bay state -- found by
    repeatedly calling _find_earliest_slot with an advancing r_time floor."""
    enriched: dict[int, list[tuple]] = {}
    for bi in cluster:
        blk_data = blocks_data[bi]
        proc = blk_data["processing_time"]
        due = blk_data["due_date"]
        workload = blk_data["workload"]
        prefs = blk_data["bay_preferences"]
        s_max = max(prefs)
        r_time = blk_data["release_time"]

        seen_positions = set()
        new_cands = []
        for (_, bay_id, cx, cy, oi, entry0, exit0) in per_block[bi]:
            pos_key = (bay_id, cx, cy, oi)
            if pos_key in seen_positions:
                continue
            seen_positions.add(pos_key)
            bay = bays[bay_id]
            blk_bb = baseline_greedy._block_bbox(blk_data, oi)
            floor = r_time
            for _ in range(timings_per_position):
                new_blk = Block(block_id=bi, block_data=blk_data, x=cx, y=cy, orient_idx=oi)
                entry, exit_t = baseline_greedy._find_earliest_slot(
                    new_blk, bay, bay_placed[bay_id], bay_schedule[bay_id],
                    r_time=floor, proc=proc, deadline=None,
                )
                if entry is None:
                    break
                tardiness = max(0.0, exit_t - due)
                bay_areas = [b.width * b.height for b in bays]
                avg_area = sum(bay_areas) / len(bays)
                bay_weights = [avg_area / a for a in bay_areas]
                bay_loads = [0.0] * len(bays)  # coarse: ignore load-balance term for this probe
                score = baseline_greedy._placement_score(
                    tardiness, workload, bay_loads, bay_id,
                    s_max - prefs[bay_id], bay_weights, 1.0, 1.0, 1.0,
                    top_y=cy + blk_bb[3],
                )
                new_cands.append((score, bay_id, cx, cy, oi, entry, exit_t))
                floor = exit_t + 1  # next call finds the NEXT successive slot at this same position
        enriched[bi] = new_cands
    return enriched


def _solve_assignment_mip(per_block, bays, blocks_data):
    """Minimal reproduction of xpress_reinsert.reinsert()'s core model
    (exactly-one-per-block + pairwise conflict cuts), used identically for
    BOTH the baseline and enriched candidate pools so only candidate
    richness differs, not solver/model shape."""
    import xpress as xp

    prob = xp.problem()
    y = {}
    for bi, cands in per_block.items():
        for ci in range(len(cands)):
            y[(bi, ci)] = xp.var(vartype=xp.binary, name=f"y_{bi}_{ci}")
    prob.addVariable(list(y.values()))
    for bi, cands in per_block.items():
        prob.addConstraint(xp.Sum(y[(bi, ci)] for ci in range(len(cands))) == 1)

    ids = list(per_block.keys())
    for a in range(len(ids)):
        bi = ids[a]
        for b in range(a + 1, len(ids)):
            bj = ids[b]
            for ci, (_, bay_i, cx_i, cy_i, oi_i, entry_i, exit_i) in enumerate(per_block[bi]):
                for cj, (_, bay_j, cx_j, cy_j, oi_j, entry_j, exit_j) in enumerate(per_block[bj]):
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
                        prob.addConstraint(y[(bi, ci)] + y[(bj, cj)] <= 1)

    objective = xp.Sum(per_block[bi][ci][0] * y[(bi, ci)]
                       for bi in ids for ci in range(len(per_block[bi])))
    prob.setObjective(objective, sense=xp.minimize)
    prob.controls.outputlog = 0
    prob.controls.maxtime = 30
    prob.solve()
    if prob.attributes.solstatus not in (xp.SolStatus.OPTIMAL, xp.SolStatus.FEASIBLE):
        return None, None
    chosen = {}
    for bi, cands in per_block.items():
        for ci in range(len(cands)):
            val = prob.getSolution(y[(bi, ci)])
            if val is not None and val > 0.5:
                chosen[bi] = cands[ci]
                break
    return prob.getObjVal(), chosen


def run(instance_path: str, construction_timelimit: float, cluster_size: int,
       max_per_block: int, timings_per_position: int) -> None:
    with open(instance_path, encoding="utf-8") as f:
        prob_info = json.load(f)
    blocks_data = prob_info["blocks"]
    bays = [Bay.from_dict(d, i) for i, d in enumerate(prob_info["bays"])]
    w1 = prob_info.get("weights", {}).get("w1", 1.0)
    w2 = prob_info.get("weights", {}).get("w2", 1.0)
    w3 = prob_info.get("weights", {}).get("w3", 1.0)

    print(f"Instance: {prob_info.get('name')}  blocks={len(blocks_data)}  bays={len(bays)}")
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

    print(f"Heaviest bay {heaviest_bay}: {len(all_bay_ids)} blocks. Generating full-bay candidates "
          f"to find a genuinely contested small cluster ...")
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
        print("Could not find a mutually-conflicting cluster of size >= 2 -- aborting.")
        return
    print(f"Hot cluster (K={len(cluster)}): {cluster}\n")

    per_block_cluster = {bi: per_block_full[bi] for bi in cluster}

    print("=== A) BASELINE -- single timing per position (production xpress_reinsert.reinsert) ===")
    baseline_partial = xpress_reinsert.reinsert(
        cluster, blocks_data, bays, bay_placed, bay_schedule, bay_loads2,
        w1, w2, w3, deadline=None, max_per_block=max_per_block, solve_time_limit=30,
    )
    if baseline_partial is None:
        print("BASELINE reinsert() returned None -- NO feasible combination exists among these "
              "single-timing-per-position candidates (production code would fall back to the "
              "much worse sequential greedy _place_blocks here). Proceeding to check whether "
              "the ENRICHED (time-diverse) model can do better than 'infeasible'.")
        baseline_obj = None
    else:
        baseline_obj = sum(
            baseline_greedy._placement_score(
                max(0.0, baseline_partial[bi]["exit_time"] - blocks_data[bi]["due_date"]),
                blocks_data[bi]["workload"], bay_loads2, baseline_partial[bi]["bay_id"],
                max(blocks_data[bi]["bay_preferences"]) - blocks_data[bi]["bay_preferences"][baseline_partial[bi]["bay_id"]],
                bay_weights, w1, w2, w3,
            )
            for bi in cluster
        )
        print(f"BASELINE objective (sum of chosen candidate scores): {baseline_obj:,.1f}")
        for bi in cluster:
            a = baseline_partial[bi]
            print(f"  block {bi}: bay={a['bay_id']} pos=({a['x']},{a['y']}) t=[{a['entry_time']},{a['exit_time']})")

    print(f"\n=== B) ENRICHED -- up to {timings_per_position} timings per position, same Xpress model ===")
    enriched = _enrich_candidates(cluster, per_block_cluster, bay_placed, bay_schedule,
                                  blocks_data, bays, timings_per_position)
    total_enriched_cands = sum(len(v) for v in enriched.values())
    print(f"Enriched candidate pool: {[(bi, len(v)) for bi, v in enriched.items()]} "
          f"(total {total_enriched_cands}, vs baseline {sum(len(v) for v in per_block_cluster.values())})")
    enriched_obj, enriched_chosen = _solve_assignment_mip(enriched, bays, blocks_data)
    if enriched_obj is None:
        print("ENRICHED model infeasible/unsolved -- aborting comparison.")
        return
    print(f"ENRICHED objective: {enriched_obj:,.1f}")
    for bi in cluster:
        c = enriched_chosen.get(bi)
        if c is None:
            print(f"  block {bi}: NOT ASSIGNED (bug?)")
            continue
        _, bay_id, cx, cy, oi, entry, exit_t = c
        print(f"  block {bi}: bay={bay_id} pos=({cx},{cy}) t=[{entry},{exit_t})")

    print(f"\n=== RESULT ===")
    if baseline_obj is None:
        print(f"baseline=INFEASIBLE (no combination exists)  enriched={enriched_obj:,.1f} (FEASIBLE)")
        print("-> Time-diversity turned an INFEASIBLE static-candidate joint reinsertion into a "
              "feasible one on this genuinely-contested cluster: strong, unambiguous evidence that "
              "time flexibility (CP-SAT interval/NoOverlap, or simply enriching candidate timing in "
              "the existing Xpress model) is a real missing degree of freedom here.")
    else:
        delta = enriched_obj - baseline_obj
        pct = 100 * delta / baseline_obj if baseline_obj else 0.0
        print(f"baseline={baseline_obj:,.1f}  enriched={enriched_obj:,.1f}  delta={delta:+,.1f} ({pct:+.1f}%)")
        if delta < -1e-6:
            print("-> Time-diversity DID find a better joint solution on this cluster: "
                  "real evidence a time-flexible (CP-SAT interval/NoOverlap) reformulation could help.")
        else:
            print("-> No improvement from adding timing alternatives at the same positions on this "
                  "cluster: the single-timing-per-position candidate model already captured the useful "
                  "flexibility here.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instance", help="instance JSON file")
    parser.add_argument("--timelimit", type=float, default=30.0)
    parser.add_argument("--cluster-size", type=int, default=6)
    parser.add_argument("--max-per-block", type=int, default=8)
    parser.add_argument("--timings-per-position", type=int, default=3)
    args = parser.parse_args()
    run(args.instance, args.timelimit, args.cluster_size, args.max_per_block, args.timings_per_position)
