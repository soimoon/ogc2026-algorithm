"""
cpsat_reinsert.py -- CP-SAT-based exact reinsertion, a prototype alternative
to xpress_reinsert.py's Xpress MIP for baseline_greedy's Phase-3 improvement
loop (_improve) and the wholebay large-scale ruin-and-recreate operator.

Motivation (2026-07-23, user-proposed): xpress_reinsert.reinsert() encodes
"no two removed blocks' chosen candidates may conflict" as one explicit
`y_i + y_j <= 1` linear constraint per conflicting (candidate, candidate)
pair -- an O(K^2 x candidates^2) constraint count that is wholebay's
long-documented scaling ceiling.

2026-07-23/24, first two drafts (kept for history, both superseded --
see the 2026-07-24 rewrite below for what the module actually does now):
  draft 1 tried clustering conflicts into CP-SAT NoOverlap groups over
  per-bay spatial clusters, hoping to collapse many pairwise constraints
  into one disjunctive-scheduling constraint -- dropped because verifying
  a cluster's conflict graph is actually COMPLETE (a precondition for that
  NoOverlap grouping being correct) is itself O(cluster_size^2), pure
  overhead whenever the cluster ISN'T complete.
  draft 2 mirrored xpress_reinsert.reinsert() almost exactly: same plain
  pairwise `y_i + y_j <= 1` cuts, same FIXED (entry, exit) per candidate
  (computed once by _find_earliest_slot against the non-batch context,
  never revisited). That draft's own docstring already flagged the real
  limitation this rewrite fixes: "candidates carry a FIXED (entry, exit)
  ... CP-SAT never gets to slide a candidate's timing to resolve a
  conflict, only to accept-or-reject whole static candidates."

2026-07-24 REWRITE (user-proposed, following a direct measurement --
see analysis/time_diversity_probe.py): that measurement compared the SAME
Xpress solver on the SAME small (K=6) genuinely-contested cluster, once
with draft 2's one-fixed-timing-per-position candidates and once with a
few extra timing alternatives added at the SAME positions -- objective
dropped 228,348 -> 879 (prob_9, -99.6%) and 36,933 -> 1,641 (prob_40,
-95.6%) on two different real instances just from adding timing
diversity, with the identical solver and identical position candidates.
At max_per_block=8 the fixed-timing model was even fully INFEASIBLE on
one cluster where the timing-diverse version solved it cleanly. That
confirms the missing degree of freedom is real and large -- but
enumerating extra discrete timings per position multiplies the candidate
count (and therefore the O(candidates^2) pairwise-conflict cost) exactly
the way wholebay's K~100-170 batches can't afford.

This is precisely the case for switching from discrete timing candidates
to genuine CP-SAT interval variables: entry_time is now a real IntVar per
(block, candidate-position), wrapped in an OptionalFixedSizeIntervalVar
gated by that candidate's presence Boolean, and conflicting positions are
resolved with `AddNoOverlap` on a PER-PAIR basis (one 2-interval NoOverlap
call per confirmed-conflicting (candidate, candidate) or (candidate,
ambient-occupant) pair -- never a shared multi-interval NoOverlap group,
which would incorrectly force mutual exclusion between candidates that
never actually conflict with each other). Spatial candidate generation is
completely unchanged (still `_top_candidates_for_block`, same
max_per_block, same position/orientation search) -- ONLY the timing
dimension is now continuous, so the candidate count driving pairwise-cost
stays exactly what it was before this rewrite; there is no more
timing-diversity-driven candidate blowup to worry about.

Why a plain per-pair NoOverlap is a SAFE, sufficient replacement for the
old (entry,exit)-anchored `check_collisions`/crane-conflict check (not
just a convenient simplification): check_entry/check_exit's own geometry
(see utils.py) tests, for a fixed pair of positions, whether ANY layer
pair (k, j>=k) overlaps -- entirely independent of *when* either block is
scheduled. The four boundary conditions the OLD `_crane_conflict` tested
(entry_j inside i's window, entry_i inside j's window, exit_j inside i's
window, exit_i inside j's window) can ALGEBRAICALLY only be satisfied
when the two intervals genuinely overlap in time (each condition requires
one interval's boundary to fall STRICTLY inside the other's open window,
which is impossible once the intervals are fully time-disjoint); the
"one fully nested inside the other" case that trips check_collisions
instead of a boundary condition also requires actual time overlap by
definition. So: if two candidate POSITIONS are geometrically incompatible
in the sense that check_collisions or check_entry (either direction)
would ever flag them, the exact condition under which check_feasibility
could ever reject the pair is "their active windows overlap in time, even
partially" -- precisely what `AddNoOverlap` between their two intervals
already forbids, nothing more and nothing less. If the positions are
geometrically COMPATIBLE (neither test ever flags them), no constraint is
added at all and the schedule may freely overlap them in time. This
predicate is computed ONCE per (candidate, candidate) or (candidate,
ambient-occupant) pair, purely from their fixed positions -- exactly the
same static geometry work the old pairwise loop already paid for, just
consumed differently (a NoOverlap instead of a `y_i+y_j<=1` cut).

A second, previously-separate correctness gap this rewrite also removes
structurally rather than patching: earlier revisions needed a dedicated
_cross_candidate_blocked_by_existing() screen for the injected
_current_position_candidate ONLY, because every OTHER candidate carried a
pre-verified fixed (entry, exit) that was already known-safe against
ambient occupants by construction, while the injected one wasn't. Now
that NO candidate carries a fixed (entry, exit) at all, every candidate --
injected or search-derived -- gets the SAME NoOverlap-against-ambient-
occupants treatment uniformly (see the "candidate vs ambient" pass in
reinsert() below). There is no more special case to forget to protect.

Scope, same discipline as xpress_reinsert.py: spatial candidates are
generated by the exact same _top_candidates_for_block search
baseline_greedy already runs elsewhere, so this module adds no new
geometry-modeling risk. Whatever this proposes is re-validated by
utils.check_feasibility in the caller before being accepted -- an
imperfect conflict model here can only ever cause a proposal to be
(correctly) rejected, never accepted incorrectly.

Status: correctness-focused prototype, still NOT wired into
baseline_greedy.py/myalgorithm.py. Deliberately mirrors only
xpress_reinsert.reinsert()'s CORE -- same-bay-first fast path and
cross-position (swap) injection are NOT ported yet; add once this core is
validated against xpress_reinsert on real scenarios (see
analysis/cpsat_interval_vs_xpress_probe.py).
"""

from __future__ import annotations

import time

from baseline_greedy import _block_bbox, _placement_score, bucket_candidate_pairs_by_grid
from baseline_greedy import _top_candidates_for_block as _candidates_for_block
from utils import Bay, Block, check_collisions, check_entry


def _static_conflict(bay: Bay, blk_a: Block, blk_b: Block) -> bool:
    """
    True if placing blk_a and blk_b at their fixed (bay, x, y, orient)
    positions could EVER violate feasibility, for SOME relative timing --
    a pure function of position, independent of when either is scheduled.

    Covers exactly what utils.check_feasibility's Stage 2/3/4 machinery
    checks between two blocks (see this module's docstring for the
    algebraic argument that these are the only ways two positions can ever
    conflict, and that they can only fire when the two intervals overlap
    in time): same-level steady-state collision (check_collisions), or a
    crane entry sweep obstruction in either direction (check_entry -- see
    utils.check_exit's own docstring: "the j >= k rule applies in both
    directions", so check_exit's predicate is identical to check_entry's
    and doesn't need a separate call).
    """
    return (check_collisions(bay, [blk_a, blk_b])
            or check_entry(bay, [blk_b], blk_a, fast=True)
            or check_entry(bay, [blk_a], blk_b, fast=True))


def _position_candidates(bi: int, blk_data: dict, bays: list[Bay],
                         bay_placed: list[list[Block]],
                         bay_schedule: list[list[tuple[int, int]]],
                         bay_loads: list[float],
                         w1: float, w2: float, w3: float,
                         bay_weights: list[float],
                         max_per_block: int,
                         deadline: float | None,
                         restrict_bay_id: int | None,
                         current_positions: dict[int, tuple] | None) -> list[tuple]:
    """
    Position-only candidates for block bi: (static_score, bay_id, x, y,
    orient_idx) -- deliberately no (entry, exit) here, unlike
    xpress_reinsert's candidates, since timing is now a free CP-SAT
    variable (see module docstring). static_score is _placement_score
    with tardiness forced to 0.0, i.e. exactly the w2 (balance) + w3
    (preference) + tie-break terms that DON'T depend on the block's
    eventual scheduled time -- w1*tardiness is added to the objective
    separately in reinsert() as a genuine function of the chosen interval's
    end time.

    Reuses _top_candidates_for_block for the actual position search (same
    geometry, same max_per_block/restrict_bay_id contract as
    xpress_reinsert.py) -- only the FIXED (entry, exit) it also returns is
    thrown away and replaced by the recomputed tardiness-free score.
    """
    raw = _candidates_for_block(
        bi, blk_data, bays, bay_placed, bay_schedule, bay_loads,
        w1, w2, w3, bay_weights, max_per_block, deadline,
        restrict_bay_id=restrict_bay_id,
    )
    prefs = blk_data["bay_preferences"]
    s_max = max(prefs)
    workload = blk_data["workload"]

    def _static_score(bay_id: int, cx: float, cy: float, oi: int) -> float:
        blk_bb = _block_bbox(blk_data, oi)
        return _placement_score(
            0.0, workload, bay_loads, bay_id, s_max - prefs[bay_id],
            bay_weights, w1, w2, w3, top_y=cy + blk_bb[3],
        )

    seen = set()
    cands: list[tuple] = []
    for (_score, bay_id, cx, cy, oi, _entry, _exit) in raw:
        key = (bay_id, cx, cy, oi)
        if key in seen:
            continue
        seen.add(key)
        cands.append((_static_score(bay_id, cx, cy, oi), bay_id, cx, cy, oi))

    if current_positions is not None and bi in current_positions:
        bay_id, cx, cy, oi, _entry, _exit = current_positions[bi]
        key = (bay_id, cx, cy, oi)
        if key not in seen:
            cands.append((_static_score(bay_id, cx, cy, oi), bay_id, cx, cy, oi))

    return cands


def reinsert(remove_ids: list[int],
            blocks_data: list[dict],
            bays: list[Bay],
            bay_placed: list[list[Block]],
            bay_schedule: list[list[tuple[int, int]]],
            bay_loads: list[float],
            w1: float, w2: float, w3: float,
            deadline: float | None,
            max_per_block: int = 20,
            solve_time_limit: int = 3,
            restrict_bay_id: int | None = None,
            current_positions: dict[int, tuple] | None = None) -> dict[int, dict] | None:
    """
    CP-SAT interval-variable reinsertion -- see this module's docstring for
    the full 2026-07-24 rewrite rationale. Contract mirrors
    xpress_reinsert.reinsert() exactly (same parameters, same
    {block_id: assignment_dict} return format, same None-on-any-failure
    fallback contract) so a caller can swap between the two without any
    other change.

    Unlike xpress_reinsert.reinsert(), each returned assignment's
    entry_time is chosen freely by the solver (subject to release_time and
    every real crane/collision constraint against both the rest of this
    batch and the bay's existing occupants) -- NOT anchored to whatever
    _find_earliest_slot happened to find first against the non-batch
    context alone.
    """
    try:
        from ortools.sat.python import cp_model
    except Exception:
        return None

    try:
        bay_areas = [bay.width * bay.height for bay in bays]
        avg_area = sum(bay_areas) / len(bays)
        bay_weights = [avg_area / a for a in bay_areas]

        _t_cand0 = time.time()
        per_block: dict[int, list[tuple]] = {}
        for bi in remove_ids:
            if deadline is not None and time.time() > deadline:
                print(f"[cpsat_reinsert] DEBUG bail: deadline hit before candidates for block {bi}")
                return None
            cands = _position_candidates(
                bi, blocks_data[bi], bays, bay_placed, bay_schedule, bay_loads,
                w1, w2, w3, bay_weights, max_per_block, deadline,
                restrict_bay_id, current_positions,
            )
            if not cands:
                print(f"[cpsat_reinsert] DEBUG bail: block {bi} has 0 candidates")
                return None
            per_block[bi] = cands
        _t_cand1 = time.time()

        # World AABB per candidate, precomputed once (same optimization as
        # xpress_reinsert.py, proven equivalent to check_collisions/
        # check_entry's own internal AABB pre-filter).
        bb: dict[tuple[int, int], tuple[float, float, float, float]] = {}
        for bi, cands in per_block.items():
            for ci, (_, _bay_id, cx, cy, oi) in enumerate(cands):
                lx0, ly0, lx1, ly1 = _block_bbox(blocks_data[bi], oi)
                bb[(bi, ci)] = (cx + lx0, cy + ly0, cx + lx1, cy + ly1)

        # A generous, finite time horizon for the interval vars -- only
        # needs to never bind the optimal solution, not be tight. Worst
        # case: every removed block ends up scheduled sequentially, one
        # after another, starting only after the latest thing already
        # committed in ANY touched bay.
        touched_bays = {bay_id for cands in per_block.values() for (_, bay_id, _, _, _) in cands}
        max_ambient_exit = max(
            (e for bay_id in touched_bays for (_, e) in bay_schedule[bay_id]), default=0
        )
        max_due = max((blocks_data[bi]["due_date"] for bi in per_block), default=0)
        sum_proc = sum(int(blocks_data[bi]["processing_time"]) for bi in per_block)
        horizon = int(max(max_ambient_exit, max_due)) + sum_proc + 1

        model = cp_model.CpModel()

        y: dict[tuple[int, int], "cp_model.IntVar"] = {}
        starts: dict[tuple[int, int], "cp_model.IntVar"] = {}
        intervals: dict[tuple[int, int], "cp_model.IntervalVar"] = {}
        block_end: dict[int, "cp_model.IntVar"] = {}
        tardiness: dict[int, "cp_model.IntVar"] = {}

        for bi, cands in per_block.items():
            r_time = int(blocks_data[bi]["release_time"])
            proc = int(blocks_data[bi]["processing_time"])
            due = int(blocks_data[bi]["due_date"])
            for ci in range(len(cands)):
                y[(bi, ci)] = model.NewBoolVar(f"y_{bi}_{ci}")
                starts[(bi, ci)] = model.NewIntVar(r_time, horizon, f"s_{bi}_{ci}")
                intervals[(bi, ci)] = model.NewOptionalFixedSizeIntervalVar(
                    starts[(bi, ci)], proc, y[(bi, ci)], f"iv_{bi}_{ci}"
                )
            model.AddExactlyOne(y[(bi, ci)] for ci in range(len(cands)))

            # Channel whichever candidate is chosen into a single per-block
            # end-time var, so tardiness can be expressed once per block
            # instead of once per candidate.
            block_end[bi] = model.NewIntVar(0, horizon, f"end_{bi}")
            for ci in range(len(cands)):
                model.Add(block_end[bi] == starts[(bi, ci)] + proc).OnlyEnforceIf(y[(bi, ci)])
            tardiness[bi] = model.NewIntVar(0, horizon, f"tardy_{bi}")
            model.AddMaxEquality(tardiness[bi], [block_end[bi] - due, 0])

            # Solver hint: if this block's current position/timing survived
            # into its own candidate list (see _position_candidates), seed
            # it as a starting point -- purely a search hint, never a
            # correctness requirement (AddHint is advisory only).
            if current_positions is not None and bi in current_positions:
                cur_bay_id, cur_cx, cur_cy, cur_oi, cur_entry, _cur_exit = current_positions[bi]
                for ci, (_, bay_id, cx, cy, oi) in enumerate(cands):
                    if (bay_id, cx, cy, oi) == (cur_bay_id, cur_cx, cur_cy, cur_oi):
                        model.AddHint(y[(bi, ci)], 1)
                        model.AddHint(starts[(bi, ci)], int(cur_entry))
                        break

        # Group candidates (and, per bay, the bay's existing non-batch
        # occupants) by grid cell for an O(n) (not O(n^2)) AABB-close
        # lookup -- same technique as this module's earlier drafts, now
        # applied uniformly to candidate-vs-candidate AND candidate-vs-
        # ambient pairs (see module docstring: there is no longer a
        # separate, easy-to-forget path for injected candidates).
        _t_pairs0 = time.time()
        ambient_interval_cache: dict[tuple[int, int], "cp_model.IntervalVar"] = {}

        def _ambient_interval(bay_id: int, idx: int) -> "cp_model.IntervalVar":
            key = (bay_id, idx)
            iv = ambient_interval_cache.get(key)
            if iv is None:
                a, e = bay_schedule[bay_id][idx]
                iv = model.NewIntervalVar(int(a), int(e - a), int(e), f"amb_{bay_id}_{idx}")
                ambient_interval_cache[key] = iv
            return iv

        # 2026-07-24: grid-bucketing extracted into baseline_greedy.
        # bucket_candidate_pairs_by_grid() (shared with xpress_reinsert.py's
        # own pairwise loop, which got the identical fix the same day) --
        # this module keeps its "cand"/"amb" key tagging (the helper itself
        # is key-agnostic) so the dispatch logic below is unchanged.
        by_bay_entries: dict[int, list[tuple]] = {}
        for bi, cands in per_block.items():
            for ci, (_, bay_id, _cx, _cy, _oi) in enumerate(cands):
                by_bay_entries.setdefault(bay_id, []).append(
                    (("cand", bi, ci), bb[(bi, ci)])
                )
        for bay_id in list(by_bay_entries):
            for idx in range(len(bay_placed[bay_id])):
                by_bay_entries[bay_id].append(
                    (("amb", bay_id, idx), bay_placed[bay_id][idx].bounding_rect())
                )

        n_noverlap = 0
        for bay_id, entries in by_bay_entries.items():
            if deadline is not None and time.time() > deadline:
                print(f"[cpsat_reinsert] DEBUG bail: deadline hit before pairwise "
                      f"construction (bay={bay_id})")
                return None

            close_pairs = bucket_candidate_pairs_by_grid(entries, deadline=deadline)
            if close_pairs is None:
                print(f"[cpsat_reinsert] DEBUG bail: deadline hit during pairwise "
                      f"construction (bay={bay_id})")
                return None

            for key_a, key_b in close_pairs:
                kind_a, *rest_a = key_a
                kind_b, *rest_b = key_b
                if kind_a == "amb" and kind_b == "amb":
                    continue  # both fixed/already-known-feasible with each other

                if kind_a == "cand" and kind_b == "cand":
                    bi_a, ci_a = rest_a
                    bi_b, ci_b = rest_b
                    if bi_a == bi_b:
                        continue  # same block's own candidates already mutually exclusive
                    _, bay_a, cx_a, cy_a, oi_a = per_block[bi_a][ci_a]
                    _, bay_b, cx_b, cy_b, oi_b = per_block[bi_b][ci_b]
                    blk_a = Block(block_id=bi_a, block_data=blocks_data[bi_a],
                                 x=cx_a, y=cy_a, orient_idx=oi_a)
                    blk_b = Block(block_id=bi_b, block_data=blocks_data[bi_b],
                                 x=cx_b, y=cy_b, orient_idx=oi_b)
                    if _static_conflict(bays[bay_id], blk_a, blk_b):
                        model.AddNoOverlap([intervals[(bi_a, ci_a)], intervals[(bi_b, ci_b)]])
                        n_noverlap += 1
                else:
                    # one "cand", one "amb" -- normalize order
                    if kind_a == "amb":
                        (bi_c, ci_c), (_bay_amb, idx_amb) = rest_b, rest_a
                    else:
                        (bi_c, ci_c), (_bay_amb, idx_amb) = rest_a, rest_b
                    _, bay_c, cx_c, cy_c, oi_c = per_block[bi_c][ci_c]
                    blk_c = Block(block_id=bi_c, block_data=blocks_data[bi_c],
                                 x=cx_c, y=cy_c, orient_idx=oi_c)
                    amb_blk = bay_placed[bay_id][idx_amb]
                    if _static_conflict(bays[bay_id], blk_c, amb_blk):
                        model.AddNoOverlap([intervals[(bi_c, ci_c)], _ambient_interval(bay_id, idx_amb)])
                        n_noverlap += 1
        _t_pairs1 = time.time()

        model.Minimize(
            sum(int(round(per_block[bi][ci][0] * 1000)) * y[(bi, ci)]
                for bi in per_block for ci in range(len(per_block[bi])))
            + int(round(w1 * 1000)) * sum(tardiness[bi] for bi in per_block)
        )

        solver = cp_model.CpSolver()
        remaining = solve_time_limit
        if deadline is not None:
            remaining = max(1, min(solve_time_limit, deadline - time.time()))
            if remaining <= 0:
                print("[cpsat_reinsert] DEBUG bail: no time left before solve")
                return None
        solver.parameters.max_time_in_seconds = float(remaining)
        solver.parameters.num_search_workers = 1

        _t_solve0 = time.time()
        status = solver.Solve(model)
        _t_solve1 = time.time()

        print(f"[cpsat_reinsert] TIMING batch_size={len(remove_ids)} "
              f"candidates={_t_cand1-_t_cand0:.3f}s pairs={_t_pairs1-_t_pairs0:.3f}s "
              f"(noverlap_constraints={n_noverlap}) solve={_t_solve1-_t_solve0:.3f}s "
              f"total={_t_solve1-_t_cand0:.3f}s")
        print(f"[cpsat_reinsert] DEBUG status={solver.StatusName(status)} "
              f"batch={remove_ids} n_candidates={[(bi, len(c)) for bi, c in per_block.items()]}")

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None

        result: dict[int, dict] = {}
        for bi, cands in per_block.items():
            chosen_ci = None
            for ci in range(len(cands)):
                if solver.Value(y[(bi, ci)]) > 0:
                    chosen_ci = ci
                    break
            if chosen_ci is None:
                print(f"[cpsat_reinsert] DEBUG bail: no chosen candidate extracted for block {bi}")
                return None
            _, bay_id, cx, cy, oi = cands[chosen_ci]
            entry = solver.Value(starts[(bi, chosen_ci)])
            proc = int(blocks_data[bi]["processing_time"])
            result[bi] = {
                "block_id": bi, "bay_id": bay_id,
                "x": int(round(cx)), "y": int(round(cy)), "orient_idx": oi,
                "entry_time": int(entry), "exit_time": int(entry + proc),
            }
        return result
    except Exception as _dbg_exc:
        import traceback
        print(f"[cpsat_reinsert] DEBUG raised: {_dbg_exc!r}")
        traceback.print_exc()
        return None
