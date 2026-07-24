"""
Historical measurement (2026-07-24, see notes/algorithm_overview.md #81): how
often baseline_greedy._find_earliest_slot's Stage-4+ pre-check missed the
narrow edge case identified during code review, BEFORE that gap was closed:

    a_other == entry  AND  e_other > exit_t

(an already-scheduled block b_other that starts at the EXACT same instant as
the candidate new_blk's entry, but stays in the bay longer than new_blk's own
exit) was not covered by any of the three branches the Stage-4+ loop had at
the time (proven analytically: for a genuinely time-overlapping b_other, this
is the ONLY uncovered combination -- every other combination is either caught
by the main Stage-2 present_at_entry loop earlier in the function, or by one
of the three original Stage-4+ branches).

Result (40 local instances, train + train-set2, 20s each): the combination
was exposed 4,672 times across 1,529,727 _find_earliest_slot calls (~0.3%),
but check_collisions never once found a real overlap in any of them --
construction-time exposure was real but empirically harmless at local scale.
#81 closed the gap anyway (the fix is a fourth branch, same check_collisions
pattern as the existing nested-case branch, essentially free).

This script intentionally FREEZES a byte-for-byte copy of the PRE-#81
function body (the buggy version) rather than importing the real
baseline_greedy._find_earliest_slot, so it keeps measuring the original gap
for the record even after future edits to the real function -- it is NOT a
regression test and will not reflect any changes made to _find_earliest_slot
after 2026-07-24. Monkeypatches baseline_greedy._find_earliest_slot for the
duration of the run only; does not modify any tracked file.

Usage:
    python analysis/measure_stage4_gap.py [timelimit_sec]
"""
import bisect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402
from utils import check_entry, check_exit, check_collisions, _bb_overlap, check_feasibility  # noqa: E402
import baseline_greedy  # noqa: E402
import myalgorithm  # noqa: E402

_time_overlaps = baseline_greedy._time_overlaps

STATS = {"exposure": 0, "leaked_collision": 0, "calls": 0}
# per-instance breakdown, reset by the driver loop between instances
CUR = {"exposure": 0, "leaked_collision": 0}


def _find_earliest_slot_pre_81(new_blk, bay, placed_in_bay, schedule_in_bay,
                               r_time, proc, deadline=None):
    """Frozen copy of _find_earliest_slot as it existed immediately before
    #81's fourth Stage-4+ branch was added -- see module docstring."""
    STATS["calls"] += 1
    if not bay.contains_block(new_blk):
        return None, None

    new_bbox = new_blk.bounding_rect()
    relevant = [
        (b, sched) for b, sched in zip(placed_in_bay, schedule_in_bay)
        if _bb_overlap(new_bbox, b.bounding_rect())
    ]
    relevant_blocks = [b for b, _ in relevant]
    relevant_schedule = [sched for _, sched in relevant]
    _sched_by_id = {b.block_id: sched for b, sched in zip(relevant_blocks, relevant_schedule)}

    candidate_entries = sorted({r_time} | {e for _, e in relevant_schedule if e > r_time})

    _conflict_cache = {}

    n_candidates = len(candidate_entries)
    idx = 0
    while idx < n_candidates:
        if deadline is not None and time.time() > deadline:
            return None, None

        entry = candidate_entries[idx]
        exit_t = entry + proc

        present_at_entry = [
            b for b, (a, e) in zip(relevant_blocks, relevant_schedule)
            if a < entry < e
        ]
        known_blocker = next(
            (b for b in present_at_entry if _conflict_cache.get(b.block_id) is True), None
        )
        if known_blocker is not None:
            e_blocker = _sched_by_id[known_blocker.block_id][1]
            idx = bisect.bisect_left(candidate_entries, e_blocker, idx + 1)
            continue
        untested = [b for b in present_at_entry if b.block_id not in _conflict_cache]
        entry_obs = check_entry(bay, untested, new_blk, fast=True) if untested else []
        if entry_obs:
            blocker = entry_obs[0].existing_block
            if blocker.block_id == new_blk.block_id:
                return None, None
            _conflict_cache[blocker.block_id] = True
            e_blocker = _sched_by_id[blocker.block_id][1]
            idx = bisect.bisect_left(candidate_entries, e_blocker, idx + 1)
            continue
        for b in untested:
            _conflict_cache[b.block_id] = False

        present_at_exit_others = [
            b for b, (a, e) in zip(relevant_blocks, relevant_schedule)
            if a < exit_t < e
        ]
        known_blocker = next(
            (b for b in present_at_exit_others if _conflict_cache.get(b.block_id) is True), None
        )
        if known_blocker is not None:
            e_blocker = _sched_by_id[known_blocker.block_id][1]
            idx = bisect.bisect_left(candidate_entries, e_blocker - proc, idx + 1)
            continue
        untested = [b for b in present_at_exit_others if b.block_id not in _conflict_cache]
        exit_obs = check_exit(bay, [new_blk] + untested, new_blk, fast=True) if untested else []
        if exit_obs:
            blocker = exit_obs[0].existing_block
            _conflict_cache[blocker.block_id] = True
            e_blocker = _sched_by_id[blocker.block_id][1]
            idx = bisect.bisect_left(candidate_entries, e_blocker - proc, idx + 1)
            continue
        for b in untested:
            _conflict_cache[b.block_id] = False

        s4_blocked = False
        for b_other, (a_other, e_other) in zip(relevant_blocks, relevant_schedule):
            if entry < a_other < exit_t:
                if check_entry(bay, [new_blk], b_other, fast=True):
                    s4_blocked = True
                    break
            if entry < e_other < exit_t:
                if check_exit(bay, [new_blk], b_other, fast=True):
                    s4_blocked = True
                    break
            if (a_other >= entry and e_other <= exit_t
                    and _time_overlaps(entry, exit_t, a_other, e_other)):
                if check_collisions(bay, [new_blk, b_other]):
                    s4_blocked = True
                    break

            # --- DIAGNOSTIC ONLY (does not affect s4_blocked/behavior) ---
            # exact combination #81's fourth branch now covers for real
            if (a_other == entry and e_other > exit_t
                    and _time_overlaps(entry, exit_t, a_other, e_other)):
                CUR["exposure"] += 1
                STATS["exposure"] += 1
                if check_collisions(bay, [new_blk, b_other]):
                    CUR["leaked_collision"] += 1
                    STATS["leaked_collision"] += 1
            # --- end diagnostic ---

        if s4_blocked:
            idx += 1
            continue

        return entry, exit_t

    return None, None


baseline_greedy._find_earliest_slot = _find_earliest_slot_pre_81


def _collect_instance_files():
    repo_parent = Path(__file__).resolve().parent.parent.parent
    patterns = [
        repo_parent / "training_instances_20260531-CWCx_z9X" / "train" / "*.json",
        repo_parent / "train-set2-UXyrUSG6" / "train" / "*.json",
    ]
    files = []
    for p in patterns:
        files.extend(sorted(str(f) for f in Path(p.parent).glob(p.name)))
    return files


def main():
    timelimit = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    files = _collect_instance_files()
    print(f"Found {len(files)} instance file(s). timelimit={timelimit}s per instance.")
    print("=" * 100)

    total_exposure = 0
    total_leaked = 0
    per_instance_leaks = []

    for f in files:
        with open(f, encoding="utf-8") as fh:
            prob_info = json.load(fh)
        n_blocks = len(prob_info["blocks"])
        n_bays = len(prob_info["bays"])

        CUR["exposure"] = 0
        CUR["leaked_collision"] = 0
        calls_before = STATS["calls"]

        t0 = time.time()
        solution = myalgorithm.algorithm(prob_info, timelimit=timelimit)
        elapsed = time.time() - t0
        result = check_feasibility(prob_info, solution)

        calls = STATS["calls"] - calls_before
        exposure = CUR["exposure"]
        leaked = CUR["leaked_collision"]
        total_exposure += exposure
        total_leaked += leaked
        if leaked:
            per_instance_leaks.append((Path(f).name, leaked, exposure))

        feas = "feasible" if result["feasible"] else f"INFEASIBLE stage={result['stage']}"
        print(f"{Path(f).name:16s} bays={n_bays:2d} blocks={n_blocks:4d}  "
              f"slot_calls={calls:7d}  exposure={exposure:5d}  leaked_collision={leaked:5d}  "
              f"{feas}  elapsed={elapsed:.1f}s")

    print("=" * 100)
    print(f"TOTAL across {len(files)} instances: _find_earliest_slot calls={STATS['calls']}  "
          f"exposure={total_exposure}  leaked_collision={total_leaked}")
    if per_instance_leaks:
        print("Instances with a real leaked collision:")
        for name, leaked, exposure in per_instance_leaks:
            print(f"  {name}: leaked_collision={leaked} (exposure={exposure})")
    else:
        print("No real leaked collisions observed on any local instance.")


if __name__ == "__main__":
    main()
