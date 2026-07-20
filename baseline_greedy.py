"""
baseline_greedy.py -- EDD + Best-Fit Greedy Algorithm with Post-Hoc Repair

===============================================================================
ALGORITHM OVERVIEW
===============================================================================

Phase 1 -- Aggressive greedy placement (EDD order):
  Blocks are sorted by Earliest Due Date (ties broken by Shortest Processing
  Time).  For each block, every (bay, orientation, position, time-slot)
  combination is scored; the cheapest is committed.  Crane-path feasibility
  (check_entry / check_exit) is verified against the current bay state, so
  most Phase-1 placements are already crane-feasible.

Phase 2 -- Iterative repair:
  check_feasibility is called on the Phase-1 solution.  Violating blocks are
  re-placed in EDD order.  Two modes are supported (repair_mode parameter):

  * "greedy" (default)
      Violating blocks are removed from the current solution and re-placed
      using the same full Phase-1 search (best bay + position + time-slot).
      State (bay_placed / bay_schedule / bay_loads) is reconstructed from
      the non-violating assignments before each pass.
      Cycle detection: if a block reappears in a second repair pass it is
      added to forced_ids, which bypasses search and uses _force_place
      (empty-bay window at (0,0)) to guarantee termination.
      Time guard: blocks whose turn comes after 90% of timelimit are also
      sent to _force_place to ensure all blocks are assigned before timeout.

  * "simple"
      Each violating block keeps its current (bay, x, y, orient) and is only
      pushed to the next empty-bay window (bay completely empty for the full
      processing duration).  Stage-4 (spatial collision) violations are also
      reset to position (0,0).

===============================================================================
SOLUTION DICT FORMAT
===============================================================================

{
    "operations": {
        "<time_int>": [           # integer time-point as string key
            {
                "type":       "EXIT",   # crane removes block from bay
                "block_id":   int,
                "bay_id":     int,
            },
            {
                "type":       "ENTRY",  # crane places block into bay
                "block_id":   int,
                "bay_id":     int,
                "x":          int,      # bottom-left x the reference point within the bay
                "y":          int,      # bottom-left y the reference point within the bay
                "orient_idx": int,      # index into block["shape"] list
            },
            ...
        ],
        ...
    }
}

At each time-point, EXIT operations always precede ENTRY operations.
Within the same type, operations are ordered so that each is feasible given
the bay state after all preceding operations at that time have completed.
entry_time = int(t_str) for ENTRY ops; exit_time = int(t_str) for EXIT ops.

Feasibility checking and objective computation: utils.check_feasibility(prob_info, solution).
"""

import math
import time
from utils import Bay, Block, check_entry, check_exit, check_collisions, _resolve_layers, _bounding_box


# -----------------------------------------------------------------------------
# Helpers: block bounding box (anchored, per orientation)
# -----------------------------------------------------------------------------

def _block_bbox(block_data: dict, orient_idx: int) -> tuple[float, float, float, float]:
    """Bounding box of a block in local coordinates relative to the reference
    point (first vertex of first layer = (0, 0)).  Returns (min_x, min_y, max_x, max_y)."""
    raw_layers = block_data["shape"][orient_idx]["layers"]
    layers = _resolve_layers(raw_layers)
    if not layers:
        return (0.0, 0.0, 1.0, 1.0)
    all_verts = [v for l in layers for v in l]
    return _bounding_box(all_verts)


# -----------------------------------------------------------------------------
# Helper: time interval overlap check
# -----------------------------------------------------------------------------

def _time_overlaps(a_entry: int, a_exit: int,
                   b_entry: int, b_exit: int) -> bool:
    """True if intervals [a_entry, a_exit) and [b_entry, b_exit) overlap."""
    return a_entry < b_exit and b_entry < a_exit


# -----------------------------------------------------------------------------
# ATC (Apparent Tardiness Cost) priority -- adapted for Phase-1 static ordering
# -----------------------------------------------------------------------------

def _atc_priority(blk_data: dict, p_avg: float, k: float) -> float:
    """
    Adapted Apparent Tardiness Cost index (Vepsalainen & Morton 1987).

    Classic ATC recomputes I_j(t) = (w_j/p_j) * exp(-max(d_j-p_j-t,0)/(k*p_avg))
    dynamically at every dispatch decision, using the shared machine's current
    free time as t. Phase 1 here is a single static sort feeding sequential
    greedy insertion (not a re-evaluated dispatch loop), so t is approximated
    once per block by its own release_time -- the earliest moment it could
    possibly be considered, which is this problem's natural analogue of "not
    yet on the machine's clock".

    w_j (per-job importance) has no analogue here -- Z1 sums tardiness
    unweighted across blocks -- so w_j=1 for every block, reducing the rule to
    a slack-modulated SPT (shortest processing time) index. Higher index =
    higher priority = placed earlier in Phase 1's insertion order. k is the
    lookahead parameter: large k approaches pure SPT (1/p_j), small k
    approaches "least slack first" (near-EDD-like urgency).
    """
    p = max(1.0, float(blk_data["processing_time"]))
    slack = max(0.0, blk_data["due_date"] - blk_data["release_time"] - blk_data["processing_time"])
    return (1.0 / p) * math.exp(-slack / max(1e-9, k * p_avg))


# -----------------------------------------------------------------------------
# Helper: candidate position generation (bottom-left corner based)
# -----------------------------------------------------------------------------

def _candidate_positions(bay_w: float, bay_h: float,
                         placed_blocks: list[Block],
                         blk_bb: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """
    Return integer (x, y) reference-point candidate positions for a new block
    using the "bottom-left fill" heuristic.

    blk_bb = (local_min_x, local_min_y, local_max_x, local_max_y) in local
    coordinates (reference point = first vertex of first layer = (0, 0)).
    A placement (x, y) is valid iff the block's world bbox stays within the bay:
      x + blk_bb[0] >= 0,  y + blk_bb[1] >= 0
      x + blk_bb[2] <= bay_w,  y + blk_bb[3] <= bay_h
    Candidates are sorted by (x, y) so the search visits left-most / bottom-most
    positions first.
    """
    lx0, ly0, lx1, ly1 = blk_bb
    # Smallest valid integer reference-point position (block's left/bottom edge at bay wall)
    xs = {max(0, math.ceil(-lx0))}
    ys = {max(0, math.ceil(-ly0))}
    for b in placed_blocks:
        bb = b.bounding_rect()
        # Reference-point x/y such that new block's left/bottom edge touches the
        # right/top edge of this placed block
        xs.add(math.ceil(bb[2] - lx0))
        ys.add(math.ceil(bb[3] - ly0))

    candidates = []
    for x in sorted(xs):
        for y in sorted(ys):
            if x + lx1 <= bay_w + 1e-6 and y + ly1 <= bay_h + 1e-6:
                candidates.append((int(x), int(y)))
    return candidates


# -----------------------------------------------------------------------------
# Placement score (lower is better)
# -----------------------------------------------------------------------------

def _placement_score(tardiness: float, workload: float,
                     bay_loads: list[float], bay_id: int,
                     pref_penalty: float,
                     bay_weights: list[float],
                     w1: float, w2: float, w3: float,
                     top_y: float = 0.0, w4: float = 1e-4) -> float:
    """
    Composite score for placing a block in bay_id (lower is better).

      w1 * tardiness    -- total tardiness: max(0, exit_time - due_date).

      w2 * new_obj2     -- approximation of normalized load-balance penalty.
                          new_obj2 = max_j |u[bay_id]*new_load - u[j]*load_j|
                          where u_j = avg_bay_area / (W_j * H_j).

      w3 * pref_penalty -- preference penalty: S_i_max - S_i_bay_id.
                          0 when placed in most-preferred bay.

      w4 * top_y        -- tie-breaking: lower top edge -> tighter packing.
    """
    new_load = bay_loads[bay_id] + workload
    new_obj2 = max(
        (abs(bay_weights[bay_id] * new_load - bay_weights[j] * bay_loads[j])
         for j in range(len(bay_loads)) if j != bay_id),
        default=0.0
    )
    return w1 * tardiness + w2 * new_obj2 + w3 * pref_penalty + w4 * top_y


# -----------------------------------------------------------------------------
# Earliest feasible entry slot (aggressive -- allows time overlap)
# -----------------------------------------------------------------------------

def _find_earliest_slot(new_blk: Block,
                        bay: Bay,
                        placed_in_bay: list[Block],
                        schedule_in_bay: list[tuple[int, int]],
                        r_time: int,
                        proc: int,
                        deadline: float | None = None) -> tuple[int | None, int | None]:
    """
    Return the earliest (entry, exit_t) time slot >= r_time at which new_blk
    can be crane-placed into bay without violating Stage-2 (entry) or Stage-3
    (exit) feasibility.  Returns (None, None) if no candidate entry passes
    both checks -- this means the (position, bay) combination is infeasible for
    any time and the caller should try a different position.

    deadline : optional absolute time.time() budget.  candidate_entries is
      bounded by len(schedule_in_bay) (every already-placed block's exit
      time), which can be large in a densely-scheduled bay; each candidate
      does several check_entry/check_exit/check_collisions calls, so a
      single call to this function can itself take a non-trivial slice of
      wall time on a big instance. Checked once per candidate_entries
      iteration so a slow bay can't make one caller-level deadline check
      (in _place_blocks) miss its budget by an unbounded amount. Once past
      deadline, returns (None, None) immediately -- same as "no slot found"
      -- rather than silently returning a stale/partial answer.

    -- Candidate enumeration ----------------------------------------------------
    Candidates = {r_time} | {exit_time of every already-placed block in bay}.

    -- Feasibility checks (mirror of check_feasibility Stages 2 & 3) -----------
    Stage-2 (crane entry): the crane path must not be blocked at entry_time.
      present_at_entry = blocks b_k with  a_k <= entry < e_k
      check_entry(bay, present_at_entry, new_blk, fast=True) returns True if
      ANY block in present_at_entry obstructs the crane path; fast=True exits
      on the first obstruction to avoid unnecessary Shapely work.

    Stage-3 (crane exit): the crane path must not be blocked at exit_time.
      present_at_exit  = [new_blk] + blocks b_k with  a_k < exit_t < e_k
      (new_blk itself is included because it will be present during its own exit)
      check_exit(bay, present_at_exit, new_blk, fast=True) returns True if
      ANY block in present_at_exit obstructs the crane exit path.

    Stage-4 (interior-interval): blocks whose interval is strictly inside
      [entry, exit_t) -- i.e. entry < a_k AND e_k < exit_t -- are invisible
      to the Stage-2 and Stage-3 boundary checks above.  They are present
      during new_blk's stay but not at its entry or exit moment.  A per-pair
      spatial collision check is run for these blocks to avoid producing
      Stage-4 violations that the repair loop cannot detect at placement time.
    """
    candidate_entries = sorted({r_time} | {e for _, e in schedule_in_bay if e > r_time})

    for entry_candidate in candidate_entries:
        if deadline is not None and time.time() > deadline:
            return None, None

        entry  = max(r_time, entry_candidate)
        exit_t = entry + proc

        # Stage-2: blocks already present when new_blk arrives.
        # Mirrors check_feasibility: a_k < entry < e_k  (strict lower bound --
        # blocks entering at the same moment are handled by Stage-5 ordering).
        present_at_entry = [
            b for b, (a, e) in zip(placed_in_bay, schedule_in_bay)
            if a < entry < e
        ]
        if check_entry(bay, present_at_entry, new_blk, fast=True):
            continue  # crane path blocked at entry -> try next exit boundary

        # Stage-3: blocks still present when new_blk departs.
        # Mirrors check_feasibility: a_k < exit_t < e_k  (strict both ends).
        present_at_exit = [new_blk] + [
            b for b, (a, e) in zip(placed_in_bay, schedule_in_bay)
            if a < exit_t < e
        ]
        if check_exit(bay, present_at_exit, new_blk, fast=True):
            continue  # crane path blocked at exit -> try next exit boundary

        # Stage-4 pre-check: blocks that co-exist with new_blk during (entry, exit_t)
        # but are absent at both boundary moments, so Stage-2 and Stage-3 above
        # don't cover them.  A block b_other falls into this gap when:
        #   a_other >= entry  (not caught by Stage-2: a_k < entry is false)
        #   e_other <= exit_t (not caught by Stage-3: e_k > exit_t is false)
        # AND its interval actually overlaps [entry, exit_t).
        # Note: e_other == exit_t means b_other departs exactly when new_blk does;
        # check_feasibility treats them as co-present during their shared [a,e) so
        # a spatial collision is still a violation -- include it here.
        s4_blocked = False
        for b_other, (a_other, e_other) in zip(placed_in_bay, schedule_in_bay):
            if a_other < entry or e_other > exit_t:
                continue  # covered by Stage-2 (a_other < entry) or Stage-3 (e_other > exit_t)
            if not _time_overlaps(entry, exit_t, a_other, e_other):
                continue  # disjoint in time
            if check_collisions(bay, [new_blk, b_other]):
                s4_blocked = True
                break
        if s4_blocked:
            continue

        return entry, exit_t

    return None, None  # no valid time slot for this (position, bay) combination


# -----------------------------------------------------------------------------
# Guaranteed-feasible entry: empty-bay window
# -----------------------------------------------------------------------------

def _empty_bay_entry(schedule_in_bay: list[tuple[int, int]],
                     r_time: int, proc: int) -> int:
    """
    Return the earliest entry time >= r_time such that the bay is completely
    empty for the entire window [entry, entry + proc).

    This guarantees crane-path feasibility: when the bay is empty at both
    entry_time and exit_time, check_entry and check_exit trivially pass
    (no blocks present means no polygon obstructions).

    Algorithm -- iterative push:
      Start with entry = r_time.  Scan all existing slots (a_k, e_k).  If
      [entry, entry+proc) overlaps any slot, advance entry to e_k (the end of
      that slot) so the window no longer overlaps it.  Repeat until no
      overlaps remain.

    Convergence guarantee:
      Each iteration advances entry by at least the distance to the next
      slot endpoint.  Because the number of slots is finite, the loop
      terminates after at most len(schedule_in_bay) passes.
    """
    entry = int(r_time)
    changed = True
    while changed:
        changed = False
        exit_t = entry + proc
        for a, e in schedule_in_bay:
            if _time_overlaps(entry, exit_t, a, e):
                entry = max(entry, e)  # push past the overlapping slot
                changed = True
    return entry


# -----------------------------------------------------------------------------
# Main algorithm
# -----------------------------------------------------------------------------

def greedyalgorithm(prob_info: dict, timelimit: float,
                    repair_mode: str = "greedy",
                    priority_rule: str = "edd",
                    atc_k: float = 2.0) -> dict:
    """
    ATC/EDD + Best-Fit Greedy algorithm with post-hoc feasibility repair and
    an anytime tardiness-improvement pass.

    Parameters
    ----------
    prob_info     : instance JSON dict with keys "name", "bays", "blocks", "weights"
    timelimit     : wall-clock time limit in seconds
    repair_mode   : "greedy" (default) or "simple" -- see module docstring for details
    priority_rule : "edd" (default), "slack" (min-slack-first / MST), or "atc"
                    -- Phase-1 insertion order. See _atc_priority for the ATC
                    adaptation used here.

                    Empirically (analysis/priority_rule_compare.py, all 40
                    train instances, 15s/instance): plain EDD won on 21/40
                    instances (avg objective 484M) vs slack 11/40 (505M) vs
                    ATC 8/40 (522M) -- i.e. the simplest rule beat both more
                    "principled" alternatives here, likely because
                    _placement_score already folds w1*tardiness into every
                    candidate's score at commit time, so insertion order
                    matters less than the ATC/MST theory (developed for
                    single-machine scheduling, not this multi-bay spatial
                    setting) would suggest. "atc"/"slack" are kept available
                    for further experimentation, not because either beat EDD.
    atc_k         : ATC lookahead parameter (only used when priority_rule="atc").
                    Larger = closer to pure SPT, smaller = closer to min-slack-first.

    Returns
    -------
    solution dict in the format described in the module docstring

    Phase 1 -- greedy placement:
        Blocks sorted by priority_rule (ATC index descending, or (due_date,
        processing_time) for EDD).  For each block, every (bay, orientation,
        candidate position) is tried; _find_earliest_slot computes the
        earliest crane-feasible time slot.  The combination minimising
        _placement_score is committed.  bay_placed, bay_schedule, and
        bay_loads are updated incrementally.

    Phase 2 -- Repair (see _repair and module docstring for details):
        Calls _repair which runs up to max_passes rounds of
        check_feasibility -> re-place violating blocks, worst-tardiness-first.

    Phase 3 -- Improve (see _improve): once Phase 2 reaches a feasible
        solution, spends any leftover time budget trying to reduce Z1 by
        removing and re-inserting the currently most-tardy blocks. _repair
        only ever touches blocks that are spatially/crane infeasible; a
        block can be fully feasible and still sit at a large, avoidable
        tardiness with nothing in Phases 1-2 ever revisiting it. Phase 3
        exists specifically to use whatever time Phases 1-2 didn't need
        (which, empirically, is often several seconds on mid-sized train
        instances) on exactly that gap.
    """
    t_start = time.time()

    bays_data   = prob_info["bays"]
    blocks_data = prob_info["blocks"]
    n_bays      = len(bays_data)
    n_blocks    = len(blocks_data)

    w1 = prob_info.get("weights", {}).get("w1", 1.0)
    w2 = prob_info.get("weights", {}).get("w2", 1.0)
    w3 = prob_info.get("weights", {}).get("w3", 1.0)

    print(f"[Greedy] Instance : {prob_info.get('name', '?')}")
    print(f"[Greedy] Bays     : {n_bays}  |  Blocks : {n_blocks}  |  Timelimit : {timelimit:.1f}s")
    print(f"[Greedy] Weights  : w1={w1}  w2={w2}  w3={w3}")
    print(f"[Greedy] {'-' * 56}")

    bays = [Bay.from_dict(d, i) for i, d in enumerate(bays_data)]
    for i, b in enumerate(bays):
        print(f"[Greedy]   bay[{i}]  {b.width}x{b.height}")

    # -- Instance validity check: every block must have at least one valid -----
    # integer (x, y) position in at least one bay and orientation.
    # If not, the problem instance itself is malformed -- abort immediately.
    invalid_blocks = []
    for bi, blk_data in enumerate(blocks_data):
        placeable = False
        for bay in bays:
            for oi in range(len(blk_data["shape"])):
                bb = _block_bbox(blk_data, oi)
                lx0, ly0, lx1, ly1 = bb
                if (math.ceil(-lx0) <= math.floor(bay.width  - lx1) and
                        math.ceil(-ly0) <= math.floor(bay.height - ly1)):
                    placeable = True
                    break
            if placeable:
                break
        if not placeable:
            invalid_blocks.append(bi)
    if invalid_blocks:
        print(f"[Greedy] ERROR: {len(invalid_blocks)} block(s) cannot be placed at any integer "
              f"position in any bay -- malformed instance.")
        for bi in invalid_blocks:
            blk_data = blocks_data[bi]
            for bay in bays:
                for oi in range(len(blk_data["shape"])):
                    bb = _block_bbox(blk_data, oi)
                    lx0, ly0, lx1, ly1 = bb
                    bw, bh = lx1 - lx0, ly1 - ly0
                    print(f"[Greedy]   block {bi} oi={oi} bay{bay.id}({bay.width}x{bay.height}): "
                          f"bw={bw:.4f} bh={bh:.4f} "
                          f"px=[{math.ceil(-lx0)},{math.floor(bay.width-lx1)}] "
                          f"py=[{math.ceil(-ly0)},{math.floor(bay.height-ly1)}]")
        raise ValueError(
            f"Malformed instance '{prob_info.get('name', '?')}': "
            f"block(s) {invalid_blocks} have no valid integer placement in any bay."
        )

    # -- Phase 1: aggressive greedy --------------------------------------------
    def _slack(i: int) -> float:
        b = blocks_data[i]
        return max(0.0, b["due_date"] - b["release_time"] - b["processing_time"])

    if priority_rule == "atc":
        p_avg = sum(b["processing_time"] for b in blocks_data) / max(1, n_blocks)
        sorted_indices = sorted(
            range(n_blocks),
            key=lambda i: (-_atc_priority(blocks_data[i], p_avg, atc_k), blocks_data[i]["due_date"])
        )
        rule_label = f"ATC(k={atc_k})"
    elif priority_rule == "slack":
        sorted_indices = sorted(
            range(n_blocks),
            key=lambda i: (_slack(i), blocks_data[i]["due_date"])
        )
        rule_label = "MST(min-slack)"
    else:
        sorted_indices = sorted(
            range(n_blocks),
            key=lambda i: (blocks_data[i]["due_date"], blocks_data[i]["processing_time"])
        )
        rule_label = "EDD"
    print(f"[Greedy] {'-' * 56}")
    print(f"[Greedy] Phase 1 : {rule_label} greedy placement ...")

    bay_placed:   list[list[Block]]             = [[] for _ in range(n_bays)]
    bay_schedule: list[list[tuple[int, int]]]   = [[] for _ in range(n_bays)]
    bay_loads:    list[float]                   = [0.0] * n_bays

    # Phase 1 gets at most 50% of the total timelimit for full search. This
    # was originally 75%, but Phase 1's per-block search has no "good enough,
    # stop" early exit -- it always explores every candidate for the best
    # score -- so in practice it happily consumes its *entire* allotment on
    # every instance, not just large/hard ones. With Phase 3 now existing to
    # spend leftover time on tardiness specifically, Phase 1 no longer needs
    # (or should get) the lion's share of the budget: a decent-not-perfect
    # Phase 1 construction plus more Phase 3 rounds empirically beats a
    # maximally-searched Phase 1 with almost no Phase 3 left.
    phase1_deadline = t_start + timelimit * 0.5
    assignments = _place_blocks(
        sorted_indices, blocks_data, bays,
        bay_placed, bay_schedule, bay_loads,
        w1, w2, w3, forced_ids=set(),
        t_start=t_start, log_interval=max(1, n_blocks // 10),
        deadline=phase1_deadline,
    )

    elapsed_p1 = time.time() - t_start
    loads_str = "  ".join(f"bay{i}={round(bay_loads[i])}" for i in range(n_bays))
    print(f"[Greedy] Phase 1 done  |  placed={len(assignments)}  {loads_str}  "
          f"elapsed={elapsed_p1:.2f}s")

    # -- Phase 2: repair infeasible assignments --------------------------------
    print(f"[Greedy] {'-' * 56}")
    print(f"[Greedy] Phase 2 : repair  mode={repair_mode}")
    sol = {"operations": _build_operations(list(assignments.values()))}
    assignments = _repair(prob_info, sol, assignments, bays, blocks_data,
                          w1, w2, w3, t_start, timelimit,
                          repair_mode=repair_mode)

    # -- Phase 3: improve feasible-but-tardy assignments with leftover time ---
    print(f"[Greedy] {'-' * 56}")
    print("[Greedy] Phase 3 : improve (worst-tardiness LNS) ...")
    assignments = _improve(prob_info, assignments, bays, blocks_data,
                           w1, w2, w3, t_start, timelimit, atc_k=atc_k)

    elapsed_total = time.time() - t_start
    final_sol = {"operations": _build_operations(list(assignments.values()))}

    from utils import check_feasibility
    final_result = check_feasibility(prob_info, final_sol)
    print(f"[Greedy] {'-' * 56}")
    print(f"[Greedy] Done  |  assigned={len(assignments)}/{n_blocks}  "
          f"elapsed={elapsed_total:.2f}s")
    if final_result["feasible"]:
        print(f"[Greedy] Objective : {final_result['objective']:.0f}  "
              f"(obj1={final_result['obj1']:.1f}  "
              f"obj2={final_result['obj2']:.1f}  "
              f"obj3={final_result['obj3']:.1f})")
    else:
        print(f"[Greedy] INFEASIBLE stage={final_result['stage']}")
        for v in final_result["violations"][:5]:
            print(f"[Greedy]   {v}")

    return final_sol


# -----------------------------------------------------------------------------
# Force-place helper (no feasibility check -- used as phase-1 last resort)
# -----------------------------------------------------------------------------

def _force_place(bi: int,
                 blocks_data: list[dict],
                 bays: list[Bay],
                 bay_schedule: list[list[tuple[int, int]]],
                 prefs: list[float]) -> tuple:
    """
    Fallback placement: place block bi at the minimum valid position in the
    highest-preference bay whose dimensions accommodate the block, using
    an empty-bay entry window.

    When called:
      * _place_blocks found no feasible (position, bay, time-slot) combination
        during Phase-1 search (should be rare for well-formed instances).
      * bi is in forced_ids during repair -- the block has appeared in two or
        more consecutive repair passes, indicating a crane-path cycle.  Forcing
        it to an empty-bay window breaks the cycle by guaranteeing that both
        check_entry and check_exit trivially pass (bay is empty).

    Why minimum-valid position with empty-bay window is always feasible:
      _empty_bay_entry returns a time interval [entry, exit_t) during which no
      other block occupies the bay.  With the bay empty at both entry_time and
      exit_time, check_entry/check_exit have no polygon obstructions to report,
      so Stage-2 and Stage-3 always pass regardless of block shape or position.
      The minimum-valid position (max(0, ceil(-lx0)), max(0, ceil(-ly0)))
      ensures the block's bounding box starts at the bay's lower-left corner,
      so the bay boundary check also passes.

    Orientation selection: the first orientation whose footprint fits within
    the bay is used.  If no orientation fits (degenerate instance), the
    preferred bay with orientation 0 is used as an absolute last resort.

    Position selection: the minimum valid reference-point position is used,
    i.e. (max(0, ceil(-lx0)), max(0, ceil(-ly0))) derived from the block's
    local bounding box.  This ensures the block's world bounding box starts at
    the bay's lower-left corner regardless of which vertex is the reference
    point.  Placing at (0, 0) would be wrong when lx0 < 0 or ly0 < 0.
    """
    blk_data = blocks_data[bi]
    r_time   = blk_data["release_time"]
    proc     = blk_data["processing_time"]
    n_bays   = len(bays)

    for bay_id in sorted(range(n_bays), key=lambda j: prefs[j], reverse=True):
        bay = bays[bay_id]
        for oi in range(len(blk_data["shape"])):
            bb = _block_bbox(blk_data, oi)
            lx0, ly0, lx1, ly1 = bb
            # Block translates all layers by (px - ref_x, py - ref_y).
            # With ref point guaranteed (0,0) by the instance generator:
            #   world_xmin = lx0 + px >= 0   =>  px >= ceil(-lx0)
            #   world_xmax = lx1 + px <= W   =>  px <= floor(W - lx1)
            #   world_ymin = ly0 + py >= 0   =>  py >= ceil(-ly0)
            #   world_ymax = ly1 + py <= H   =>  py <= floor(H - ly1)
            # A valid integer px exists iff ceil(-lx0) <= floor(W - lx1).
            px_lo = math.ceil(-lx0)
            px_hi = math.floor(bay.width  - lx1)
            py_lo = math.ceil(-ly0)
            py_hi = math.floor(bay.height - ly1)
            if px_lo > px_hi or py_lo > py_hi:
                continue  # no valid integer position for this orientation
            px = max(0, px_lo)
            py = max(0, py_lo)
            entry = _empty_bay_entry(bay_schedule[bay_id], r_time, proc)
            return (bay_id, px, py, oi, entry, entry + proc)

    # This path should never be reached: greedyalgorithm() checks at startup that
    # every block has at least one valid integer position and raises ValueError for
    # malformed instances before any placement begins.
    raise RuntimeError(
        f"_force_place: block {bi} has no valid integer position in any bay "
        f"-- instance validation should have caught this."
    )


# -----------------------------------------------------------------------------
# Shared greedy placement kernel (used by Phase 1 and _repair)
# -----------------------------------------------------------------------------

def _place_blocks(
    block_ids: list[int],
    blocks_data: list[dict],
    bays: list[Bay],
    bay_placed: list[list[Block]],
    bay_schedule: list[list[tuple[int, int]]],
    bay_loads: list[float],
    w1: float, w2: float, w3: float,
    forced_ids: set[int],
    prev_assignments: dict[int, dict] | None = None,
    t_start: float | None = None,
    log_interval: int = 0,
    deadline: float | None = None,
) -> dict[int, dict]:
    """
    Shared placement kernel used by both Phase 1 and _repair (greedy mode).

    For each block in block_ids, finds the best (bay, x, y, orient, entry_time)
    by minimising _placement_score, then commits it to bay_placed /
    bay_schedule / bay_loads.  Returns a dict mapping block_id -> assignment.

    Search order:
      1. Repair fast-path (only when prev_assignments is provided):
         Try the block's previous (bay, x, y, orient) with _find_earliest_slot.
         If that position is still crane-feasible, record it as the initial
         best candidate.  This avoids re-solving the position search for blocks
         that only need a time adjustment.
      2. Full search (Phase-1 style):
         Iterate bays in decreasing preference order, then orientations, then
         candidate positions from _candidate_positions.  For each (bay, orient,
         pos), call _find_earliest_slot to get the earliest crane-feasible slot.
         Keep the (bay, orient, pos, slot) with the lowest _placement_score.
      3. Forced path (forced_ids or no feasible combination found):
         Blocks in forced_ids skip steps 1-2 entirely and go straight to
         _force_place.  If the full search in step 2 found nothing, _force_place
         is used as a fallback and n_fallback is incremented.

    Parameters
    ----------
    block_ids        : ordered list of block indices to place (EDD order)
    blocks_data      : raw block data list from prob_info
    bays             : Bay objects (width, height, polygon)
    bay_placed       : mutable per-bay lists of placed Block objects (updated in-place)
    bay_schedule     : mutable per-bay lists of (entry_time, exit_time) (updated in-place)
    bay_loads        : mutable per-bay cumulative workload floats (updated in-place)
    w1, w2, w3       : objective weights
    forced_ids       : block ids to bypass search and use _force_place directly
    prev_assignments : previous assignment dict (repair mode fast-path)
    t_start          : wall-clock start time (for log timestamps)
    log_interval     : print a progress line every N blocks (0 = silent)
    deadline         : absolute time.time() budget for the full O(bays x
                       orientations x positions) search in step 2.  Once
                       exceeded, every remaining block in block_ids is routed
                       straight to the forced path (step 3), the same
                       guaranteed-O(bays x orientations) _force_place fallback
                       used for cycle-breaking.  Without this, a single very
                       large instance could exhaust the entire timelimit
                       inside this search before Phase 2 (which does have its
                       own 90%/98% time guards) ever runs, risking a hard
                       time-limit-exceeded failure with nothing to show for it.
                       None (default) disables the check.

    Returns
    -------
    dict[block_id -> assignment dict] for all blocks in block_ids
    """
    n_bays  = len(bays)
    n_total = len(block_ids)
    result: dict[int, dict] = {}
    n_forced = n_fallback = 0

    # Bay weights for normalized obj2: u_j = avg_area / (W_j * H_j)
    _bay_areas   = [bay.width * bay.height for bay in bays]
    _avg_area    = sum(_bay_areas) / n_bays
    bay_weights  = [_avg_area / a for a in _bay_areas]

    for rank, bi in enumerate(block_ids):
        blk_data = blocks_data[bi]
        r_time   = blk_data["release_time"]
        due      = blk_data["due_date"]
        proc     = blk_data["processing_time"]
        workload = blk_data["workload"]
        prefs    = blk_data["bay_preferences"]
        s_max    = max(prefs)
        n_orient = len(blk_data["shape"])

        best_score     = float("inf")
        best_placement = None
        past_deadline  = deadline is not None and time.time() > deadline
        used_forced    = bi in forced_ids or past_deadline

        if not used_forced:
            # -- Repair fast-path: try previous (bay, x, y, orient) first -----
            if prev_assignments and bi in prev_assignments:
                pa = prev_assignments[bi]
                pb_id = pa["bay_id"]
                px, py, poi = int(pa["x"]), int(pa["y"]), pa["orient_idx"]
                prev_blk = Block(block_id=bi, block_data=blk_data,
                                 x=px, y=py, orient_idx=poi)
                if bays[pb_id].contains_block(prev_blk):
                    entry, exit_t = _find_earliest_slot(
                        prev_blk, bays[pb_id],
                        bay_placed[pb_id], bay_schedule[pb_id],
                        r_time, proc, deadline=deadline,
                    )
                    if entry is not None:
                        tardiness = max(0.0, exit_t - due)
                        p_bb = _block_bbox(blk_data, poi)
                        best_score     = _placement_score(
                            tardiness, workload, bay_loads, pb_id,
                            s_max - prefs[pb_id], bay_weights, w1, w2, w3,
                            top_y=py + p_bb[3]
                        )
                        best_placement = (pb_id, px, py, poi, entry, exit_t)

            # -- Full search (Phase-1 style) -----------------------------------
            # deadline is only checked once per block (above, via past_deadline)
            # before this search starts. That is not fine-grained enough: a
            # single block with many candidate positions in a densely-packed
            # bay can itself take multiple seconds inside this triple loop
            # (each _find_earliest_slot call walks every already-placed block
            # in the bay). Re-check deadline periodically inside the candidate
            # loop too, so one slow block cannot by itself blow past deadline
            # by more than a handful of candidate evaluations.
            deadline_hit = False
            bay_order = sorted(range(n_bays), key=lambda j: prefs[j], reverse=True)
            for bay_id in bay_order:
                if deadline_hit:
                    break
                bay             = bays[bay_id]
                placed_in_bay   = bay_placed[bay_id]
                schedule_in_bay = bay_schedule[bay_id]

                for oi in range(n_orient):
                    if deadline_hit:
                        break
                    blk_bb = _block_bbox(blk_data, oi)
                    lx0_oi, ly0_oi, lx1_oi, ly1_oi = blk_bb
                    # Require a valid integer reference-point position to exist:
                    #   px in [ceil(-lx0), floor(W - lx1)]
                    #   py in [ceil(-ly0), floor(H - ly1)]
                    # If either range is empty there is no integer placement.
                    if (math.ceil(-lx0_oi) > math.floor(bay.width  - lx1_oi) or
                            math.ceil(-ly0_oi) > math.floor(bay.height - ly1_oi)):
                        continue

                    active_in_bay = [
                        b for b, (a_k, e_k) in zip(placed_in_bay, schedule_in_bay)
                        if e_k > r_time
                    ]
                    candidates = _candidate_positions(
                        bay.width, bay.height, active_in_bay, blk_bb
                    )
                    for cx, cy in candidates:
                        if deadline is not None and time.time() > deadline:
                            deadline_hit = True
                            break

                        new_blk = Block(block_id=bi, block_data=blk_data,
                                        x=cx, y=cy, orient_idx=oi)
                        if not bay.contains_block(new_blk):
                            continue

                        entry, exit_t = _find_earliest_slot(
                            new_blk, bay, placed_in_bay, schedule_in_bay,
                            r_time, proc, deadline=deadline,
                        )
                        if entry is None:
                            continue

                        tardiness = max(0.0, exit_t - due)
                        score = _placement_score(
                            tardiness, workload, bay_loads, bay_id,
                            s_max - prefs[bay_id], bay_weights, w1, w2, w3,
                            top_y=cy + blk_bb[3]
                        )
                        if score < best_score:
                            best_score     = score
                            best_placement = (bay_id, cx, cy, oi, entry, exit_t)

        if best_placement is None:
            best_placement = _force_place(bi, blocks_data, bays, bay_schedule, prefs)
            n_fallback += 1

        if used_forced:
            n_forced += 1

        bay_id, cx, cy, oi, entry, exit_t = best_placement
        final_blk = Block(block_id=bi, block_data=blk_data, x=cx, y=cy, orient_idx=oi)
        bay_placed[bay_id].append(final_blk)
        bay_schedule[bay_id].append((entry, exit_t))
        bay_loads[bay_id] += workload

        result[bi] = {
            "block_id":   bi,
            "bay_id":     bay_id,
            "x":          int(round(cx)),
            "y":          int(round(cy)),
            "orient_idx": oi,
            "entry_time": int(round(entry)),
            "exit_time":  int(round(exit_t)),
        }

        if log_interval > 0 and t_start is not None:
            n_done = rank + 1
            if n_done % log_interval == 0 or n_done == n_total:
                elapsed = time.time() - t_start
                loads_str = " ".join(f"b{i}={round(bay_loads[i])}" for i in range(n_bays))
                flag = " [forced]" if used_forced else (" [fallback]" if best_score == float("inf") else "")
                print(f"[Greedy]   {n_done:4d}/{n_total}"
                      f"  block{bi:<4d} -> bay{bay_id} ({cx},{cy}) oi={oi}"
                      f"  t=[{int(round(entry))},{int(round(exit_t))})"
                      f"  loads=[{loads_str}]"
                      f"  fallback={n_fallback}{flag}"
                      f"  {elapsed:.1f}s")

    return result


# -----------------------------------------------------------------------------
# Shared helper: rebuild per-bay state from a flat assignments dict
# -----------------------------------------------------------------------------

def _rebuild_bay_state(
    assignments: dict[int, dict],
    bays: list[Bay],
    blocks_data: list[dict],
) -> tuple[list[list[Block]], list[list[tuple[int, int]]], list[float]]:
    """
    Rebuild (bay_placed, bay_schedule, bay_loads) from a flat assignments dict.

    Shared by _repair (greedy mode) and _improve: both remove a subset of
    blocks from assignments and need an accurate view of what's still
    occupying each bay before re-searching placements for the removed ones.
    """
    n_bays = len(bays)
    bay_placed:   list[list[Block]]           = [[] for _ in range(n_bays)]
    bay_schedule: list[list[tuple[int, int]]] = [[] for _ in range(n_bays)]
    bay_loads:    list[float]                 = [0.0] * n_bays
    for a in assignments.values():
        bid_a  = a["block_id"]
        bay_id = a["bay_id"]
        blk = Block(block_id=bid_a, block_data=blocks_data[bid_a],
                    x=int(a["x"]), y=int(a["y"]), orient_idx=a["orient_idx"])
        bay_placed[bay_id].append(blk)
        bay_schedule[bay_id].append((a["entry_time"], a["exit_time"]))
        bay_loads[bay_id] += blocks_data[bid_a]["workload"]
    return bay_placed, bay_schedule, bay_loads


# -----------------------------------------------------------------------------
# Phase 3: improve feasible-but-tardy assignments using leftover time budget
# -----------------------------------------------------------------------------

def _improve(prob_info: dict,
            assignments: dict[int, dict],
            bays: list[Bay],
            blocks_data: list[dict],
            w1: float, w2: float, w3: float,
            t_start: float,
            timelimit: float,
            atc_k: float = 2.0,
            k_values: tuple[int, ...] = (1, 2, 3, 5),
            stall_limit: int | None = None) -> dict[int, dict]:
    """
    Large-neighborhood-search-style improvement pass for an already FEASIBLE
    solution, targeting Z1 (total tardiness).

    _repair only ever touches blocks that are spatially/crane infeasible; a
    solution can be fully feasible and still have large tardiness sitting on
    the table with nothing in Phases 1-2 ever revisiting it. This pass
    targets exactly that gap: each round, remove the current top-K
    most-tardy blocks, re-insert them via the same _place_blocks search used
    everywhere else, and keep the result only if the *actual* objective
    (re-verified with check_feasibility, not assumed from a delta estimate)
    improved -- otherwise the round is discarded and best_assignments is
    unchanged.

    Only ever called on a solution that is already feasible; if the incoming
    solution is not feasible this returns it unchanged, so it can never be
    blamed for masking a Phase 2 failure. Runs until timelimit*0.99 or no
    positive-tardiness block remains, so it is safe to let it simply run out
    the clock -- every accepted round is independently verified, so an
    interruption at any point still returns a solution at least as good as
    what Phase 2 produced.

    k_values are cycled round-robin (1, 2, 3, 5, 1, 2, ...): a round that
    can't find a better placement by moving a single worst block gets a
    chance with a larger removal set on the next round, which can unblock
    chains a single relocate cannot, without paying the cost of a large
    removal every round.

    Parameters
    ----------
    stall_limit : stop early after this many consecutive non-improving
                  rounds (default 3 * len(k_values)). Purely a wall-clock
                  courtesy for local testing -- returning early vs. running
                  to the deadline doesn't affect the score either way, since
                  the leaderboard only sees the final returned solution.
    """
    from utils import check_feasibility

    if stall_limit is None:
        stall_limit = 3 * len(k_values)

    def _build(a: dict[int, dict]) -> dict:
        return {"operations": _build_operations(list(a.values()))}

    base_result = check_feasibility(prob_info, _build(assignments))
    if not base_result["feasible"]:
        print("[Greedy] Improve: skipped (incoming solution is not feasible)")
        return assignments

    best_assignments = dict(assignments)
    best_obj = base_result["objective"]
    p_avg = sum(b["processing_time"] for b in blocks_data) / max(1, len(blocks_data))
    deadline = t_start + timelimit * 0.99

    round_idx = 0
    stalled = 0
    while time.time() < deadline:
        tardy = sorted(
            (
                (bid, a["exit_time"] - blocks_data[bid]["due_date"])
                for bid, a in best_assignments.items()
                if a["exit_time"] - blocks_data[bid]["due_date"] > 0
            ),
            key=lambda t: -t[1],
        )
        if not tardy:
            print(f"[Greedy] Improve: Z1=0, nothing left to improve  round={round_idx}")
            break

        k = k_values[round_idx % len(k_values)]
        remove_ids = [bid for bid, _ in tardy[:k]]

        trial_assignments = dict(best_assignments)
        for bid in remove_ids:
            trial_assignments.pop(bid, None)

        bay_placed, bay_schedule, bay_loads = _rebuild_bay_state(
            trial_assignments, bays, blocks_data
        )
        # Try an exact joint reinsertion of the K removed blocks via Xpress
        # first (see xpress_reinsert.py) -- it can find combinations plain
        # greedy can't (deciding all K at once instead of one at a time).
        # reinsert() returns None on any failure (Xpress unavailable, no
        # candidates, solve timeout/infeasible), which is a routine, expected
        # outcome here, not an error -- always fall back to the same greedy
        # _place_blocks search used everywhere else in this codebase.
        # k=1 has nothing to jointly optimize against (no pairwise conflicts
        # possible with a single block), so skip the MIP overhead entirely.
        partial = None
        if k > 1:
            try:
                import xpress_reinsert
                partial = xpress_reinsert.reinsert(
                    remove_ids, blocks_data, bays,
                    bay_placed, bay_schedule, bay_loads,
                    w1, w2, w3, deadline,
                )
            except Exception:
                partial = None

        used_xpress = partial is not None
        if partial is None:
            order = sorted(remove_ids, key=lambda b: -_atc_priority(blocks_data[b], p_avg, atc_k))
            partial = _place_blocks(
                order, blocks_data, bays,
                bay_placed, bay_schedule, bay_loads,
                w1, w2, w3, forced_ids=set(),
                prev_assignments=best_assignments,
                deadline=deadline,
            )
        trial_assignments.update(partial)

        trial_result = check_feasibility(prob_info, _build(trial_assignments))
        round_idx += 1
        if trial_result["feasible"] and trial_result["objective"] < best_obj - 1e-6:
            gain = best_obj - trial_result["objective"]
            best_assignments = trial_assignments
            best_obj = trial_result["objective"]
            stalled = 0
            elapsed = time.time() - t_start
            solver_tag = "xpress" if used_xpress else "greedy"
            print(f"[Greedy] Improve round {round_idx}: k={k} removed={remove_ids} "
                  f"via={solver_tag}  obj -{gain:.0f} -> {best_obj:.0f}  elapsed={elapsed:.1f}s")
        else:
            stalled += 1
            solver_tag = "xpress" if used_xpress else "greedy"
            print(f"[Greedy] Improve round {round_idx}: k={k} via={solver_tag}  "
                  f"no gain (feasible={trial_result['feasible']})  stalled={stalled}")
            if stalled >= stall_limit:
                print(f"[Greedy] Improve: no gain for {stalled} rounds, stopping early  "
                      f"round={round_idx}")
                break

    return best_assignments


# -----------------------------------------------------------------------------
# Phase 2: repair infeasible blocks
# -----------------------------------------------------------------------------

def _repair(prob_info: dict,
            sol: dict,
            assignments: dict[int, dict],
            bays: list[Bay],
            blocks_data: list[dict],
            w1: float, w2: float, w3: float,
            t_start: float,
            timelimit: float,
            max_passes: int = 10,
            repair_mode: str = "greedy") -> dict[int, dict]:
    """
    Iteratively detect infeasible blocks and repair them.

    Runs up to max_passes rounds of: check_feasibility -> collect violating
    block ids -> re-place them.  Stops early if the solution becomes feasible
    or 98% of timelimit is consumed.

    -- repair_mode="greedy" (default) ------------------------------------------
    Violating blocks are removed from assignments and re-placed using the full
    Phase-1 search (all bays, orientations, positions, time-slots).  The state
    arrays (bay_placed, bay_schedule, bay_loads) are reconstructed from the
    remaining non-violating assignments before each block is re-placed, so the
    search sees the current bay state.

    Cycle detection:
      repaired_counts[bid] tracks how many repair passes have touched block bid.
      If bid appears in a second pass (count > 1) it is added to forced_ids.
      Blocks in forced_ids skip search and go straight to _force_place (empty-
      bay window), which is structurally guaranteed to produce a crane-feasible
      placement.  This breaks cycles where two blocks keep displacing each other.

    Time guard (90% threshold):
      For each block in to_repair, if wall-clock time > 90% of timelimit before
      its turn, it is added to forced_ids.  This ensures all blocks are assigned
      before timeout rather than leaving some unassigned (Stage-1 failure).

    -- repair_mode="simple" -----------------------------------------------------
    Each violating block keeps its current (bay, x, y, orient) and is only
    pushed to the next empty-bay time window via _empty_bay_entry.  Stage-4
    violations (spatial collision) are also reset to position (0, 0).
    Faster than greedy mode, but cannot improve spatial placement quality.

    Parameters
    ----------
    prob_info   : instance JSON dict
    sol         : current solution dict (operations format)
    assignments : current assignment dict (block_id -> assignment dict)
    bays        : Bay objects
    blocks_data : raw block data from prob_info
    w1,w2,w3    : objective weights
    t_start     : wall-clock start time
    timelimit   : total wall-clock time limit
    max_passes  : maximum number of repair iterations
    repair_mode : "greedy" or "simple"

    Returns
    -------
    Updated assignments dict (all blocks assigned)
    """
    from utils import check_feasibility

    repaired_counts: dict[int, int] = {}
    forced_ids:      set[int]       = set()

    for pass_idx in range(max_passes):
        # Capped at 80% (not 98%) so Phase 3 (_improve) is structurally
        # guaranteed a real slice of the budget instead of only getting
        # whatever repair happens not to use -- repair already exits early
        # via the feasible-break below whenever it converges sooner anyway.
        if time.time() - t_start > timelimit * 0.80:
            break

        result = check_feasibility(prob_info, sol)
        if result["feasible"]:
            break

        viols = result["violations"]
        elapsed_r = time.time() - t_start
        print(f"[Greedy] Repair pass {pass_idx+1}: {len(viols)} violation(s)  "
              f"stage={result['stage']}  elapsed={elapsed_r:.1f}s")

        # -- Parse block ids from violation messages ---------------------------
        # Each violation string contains "block <id>" somewhere in the text.
        # Deduplicate while preserving first-occurrence order.
        to_repair: list[int] = []
        seen: set[int] = set()
        for v in viols:
            try:
                bid = int(v.split("block ")[1].split()[0])
                if bid not in seen:
                    seen.add(bid)
                    to_repair.append(bid)
            except (IndexError, ValueError):
                pass

        if not to_repair:
            break

        # Re-place worst-current-tardiness-first so the biggest Z1 offenders
        # claim the best remaining slots. assignments[b] still holds each
        # block's pre-repair entry/exit at this point (removal happens later,
        # per branch below), so "current tardiness" is well-defined here.
        # Blocks that are infeasible but not tardy (e.g. a pure spatial/crane
        # violation with tardiness=0) fall back to EDD ordering, since
        # tardiness alone can't distinguish them.
        def _current_tardiness(b: int) -> float:
            a = assignments.get(b)
            if a is None:
                return 0.0
            return max(0.0, a["exit_time"] - blocks_data[b]["due_date"])

        to_repair.sort(key=lambda b: (-_current_tardiness(b),
                                      blocks_data[b]["due_date"],
                                      blocks_data[b]["processing_time"]))
        n_repl = len(to_repair)

        if repair_mode == "simple":
            # -- Simple mode: adjust only the time window, keep position/orient -
            # Rebuild the per-bay time schedule from all current assignments so
            # that _empty_bay_entry can find a gap with no other blocks present.
            n_bays = len(bays)
            bay_schedule: list[list[tuple[int, int]]] = [[] for _ in range(n_bays)]
            for a in assignments.values():
                bay_schedule[a["bay_id"]].append((a["entry_time"], a["exit_time"]))

            for ri, bid in enumerate(to_repair):
                a      = assignments[bid]
                bay_id = a["bay_id"]
                r_time = blocks_data[bid]["release_time"]
                proc   = blocks_data[bid]["processing_time"]

                # Remove the block's current slot before searching for a new one
                old_slot = (a["entry_time"], a["exit_time"])
                if old_slot in bay_schedule[bay_id]:
                    bay_schedule[bay_id].remove(old_slot)

                entry  = _empty_bay_entry(bay_schedule[bay_id], r_time, proc)
                exit_t = entry + proc

                # Stage-4 (spatial collision): also reset position to (0,0)
                # to eliminate any spatial overlap with other blocks
                x, y, oi = a["x"], a["y"], a["orient_idx"]
                if result["stage"] == 4:
                    x, y = 0, 0

                assignments[bid] = dict(a, x=x, y=y, orient_idx=oi,
                                        entry_time=int(round(entry)),
                                        exit_time=int(round(exit_t)))
                bay_schedule[bay_id].append((entry, exit_t))

                prev_t = f"[{a['entry_time']},{a['exit_time']})"
                new_t  = f"[{entry},{exit_t})"
                tag    = "[s4->(0,0)]" if result["stage"] == 4 else "[time]"
                elapsed_ri = time.time() - t_start
                print(f"[Greedy]   repair {ri+1:3d}/{n_repl}"
                      f"  block{bid:<4d} {tag}"
                      f"  bay{bay_id} ({int(x)},{int(y)})"
                      f"  {prev_t} -> {new_t}"
                      f"  elapsed={elapsed_ri:.1f}s")

        else:
            # -- Greedy mode: full Phase-1 re-search for violating blocks ------
            # Mark repeat offenders as forced before touching assignments, so
            # the flag is active when _place_blocks processes them below.
            for bid in to_repair:
                repaired_counts[bid] = repaired_counts.get(bid, 0) + 1
                if repaired_counts[bid] > 1:
                    forced_ids.add(bid)

            # Remove violating blocks from assignments so the state reconstruction
            # below does not include their (now invalid) positions/slots.
            for bid in to_repair:
                assignments.pop(bid, None)

            # Reconstruct bay_placed / bay_schedule / bay_loads from the
            # remaining valid assignments.  This gives _place_blocks an accurate
            # view of which positions and time-slots are already occupied.
            bay_placed, bay_schedule2, bay_loads = _rebuild_bay_state(
                assignments, bays, blocks_data
            )

            for ri, bi in enumerate(to_repair):
                # Time guard: switch to forced path once 75% of timelimit is
                # used (was 90% -- tightened alongside the 80% pass-loop cap
                # above so repair reliably hands time back to Phase 3).
                # Without this, a slow repair search could exhaust the
                # timelimit before all blocks are placed, causing Stage-1
                # (assignment) failures.
                if time.time() - t_start > timelimit * 0.75:
                    forced_ids.add(bi)
                prev_a  = assignments.get(bi)
                partial = _place_blocks(
                    [bi], blocks_data, bays,
                    bay_placed, bay_schedule2, bay_loads,
                    w1, w2, w3, forced_ids,
                    prev_assignments=assignments,
                    deadline=t_start + timelimit * 0.80,
                )
                assignments.update(partial)
                new_a       = partial[bi]
                is_forced   = bi in forced_ids
                changed_bay = prev_a and prev_a["bay_id"] != new_a["bay_id"]
                changed_pos = prev_a and (prev_a["x"] != new_a["x"]
                                          or prev_a["y"] != new_a["y"])
                tag = ("[forced]" if is_forced
                       else "[bay]" if changed_bay
                       else "[pos]" if changed_pos
                       else "[time]")
                prev_t = (f"[{int(prev_a['entry_time'])},{int(prev_a['exit_time'])})"
                          if prev_a else "N/A")
                new_t  = f"[{int(new_a['entry_time'])},{int(new_a['exit_time'])})"
                elapsed_ri = time.time() - t_start
                print(f"[Greedy]   repair {ri+1:3d}/{n_repl}"
                      f"  block{bi:<4d} {tag}"
                      f"  bay{new_a['bay_id']} ({int(new_a['x'])},{int(new_a['y'])})"
                      f"  {prev_t} -> {new_t}"
                      f"  elapsed={elapsed_ri:.1f}s")

        sol = {"operations": _build_operations(list(assignments.values()))}

    result = check_feasibility(prob_info, sol)
    status = "feasible" if result["feasible"] else f"INFEASIBLE stage={result['stage']}"
    obj    = f"obj={result['objective']:.0f}" if result["feasible"] else ""
    forced_note = f"  forced={len(forced_ids)}" if forced_ids else ""
    elapsed_done = time.time() - t_start
    print(f"[Greedy] Repair done  |  {status}  {obj}{forced_note}  elapsed={elapsed_done:.1f}s")

    return assignments


# -----------------------------------------------------------------------------
# Build operations dict from assignments
# -----------------------------------------------------------------------------

def _build_operations(assignments: list[dict]) -> dict:
    """
    Build the "operations" dict from a flat list of assignment dicts.

    Groups operations by integer time-point into buckets, sorts each bucket
    so that EXIT operations precede ENTRY operations at the same time, and
    within each type sorts by block_id for deterministic ordering.

    Bucket tuple format: (sort_key, type_str, block_id, bay_id, x, y, orient_idx)
      sort_key = 0 for EXIT  -> sorts before
      sort_key = 1 for ENTRY -> sorts after
    The sort key ensures EXIT-before-ENTRY without explicit type-string comparison.

    The returned dict maps str(time_int) -> list of operation dicts, ordered as
    required by check_feasibility (EXIT ops first within each time-point).
    """
    buckets: dict[int, list[tuple]] = {}
    for a in assignments:
        t_entry = int(a["entry_time"])
        t_exit  = int(a["exit_time"])
        bid     = a["block_id"]
        bay     = a["bay_id"]
        buckets.setdefault(t_exit,  []).append((0, "EXIT",  bid, bay, None,    None,    None))
        buckets.setdefault(t_entry, []).append((1, "ENTRY", bid, bay, a["x"], a["y"], a["orient_idx"]))

    operations: dict[str, list[dict]] = {}
    for t in sorted(buckets):
        ops = sorted(buckets[t], key=lambda x: (x[0], x[2]))
        result = []
        for _, kind, bid, bay, x, y, orient_idx in ops:
            op: dict = {"type": kind, "block_id": bid, "bay_id": bay}
            if kind == "ENTRY":
                op["x"] = x
                op["y"] = y
                op["orient_idx"] = orient_idx
            result.append(op)
        operations[str(t)] = result
    return operations


# -----------------------------------------------------------------------------
# CLI run
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json
    import pathlib
    from collections import defaultdict
    from utils import check_feasibility

    parser = argparse.ArgumentParser(description="EDD greedy algorithm smoke test")
    parser.add_argument("instance", help="path to instance JSON file")
    parser.add_argument("--timelimit", type=float, default=60.0,
                        help="wall-clock time limit in seconds (default: %(default)s)")
    parser.add_argument("--repair", choices=["greedy", "simple"], default="greedy",
                        help="repair mode (default: %(default)s)")
    args = parser.parse_args()

    inst_file = pathlib.Path(args.instance)

    with open(inst_file) as f:
        prob_info = json.load(f)

    t0  = time.time()
    sol = greedyalgorithm(prob_info, timelimit=args.timelimit, repair_mode=args.repair)
    elapsed = time.time() - t0

    result = check_feasibility(prob_info, sol)

    n_assigned = sum(1 for ops in sol["operations"].values()
                     for op in ops if op["type"] == "ENTRY")
    print(f"Instance : {prob_info['name']}")
    print(f"Elapsed  : {elapsed:.3f}s")
    print(f"Assigned : {n_assigned} / {len(prob_info['blocks'])} blocks")
    print(f"Feasible : {result['feasible']}  (stage={result['stage']})")
    if result["feasible"]:
        print(f"Objective: {result['objective']:.2f}  "
              f"(obj1={result['obj1']:.1f}, obj2={result['obj2']:.1f}, obj3={result['obj3']:.1f})")
    else:
        for v in result["violations"][:10]:
            print(f"  VIOLATION: {v}")
