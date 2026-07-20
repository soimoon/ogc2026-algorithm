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
import random
import re
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

        # Stage-4+ pre-check: does inserting new_blk here retroactively break
        # any ALREADY-PLACED block's own already-committed crane operation,
        # or collide with one it merely coexists with?
        #
        # 2026-07-20 bugfix: the original version here only checked blocks
        # whose *entire* interval is nested inside [entry, exit_t), and only
        # via steady-state same-level collision (check_collisions) -- never
        # the stricter cross-level crane rule (same-or-higher-level layers).
        # That misses two real cases, both of which check_feasibility WOULD
        # catch later on the full solution (as a Stage-2 or Stage-3
        # violation), meaning they'd only surface after Phase 1, forcing
        # Phase 2 repair to clean them up -- this re-derives the same check
        # at construction time so there's ideally nothing left to repair:
        #   (a) b_other started before new_blk and is still present when
        #       new_blk enters, but *exits* while new_blk is present --
        #       b_other's exit was validated against the world as it looked
        #       when b_other was placed, which didn't include new_blk yet.
        #   (b) b_other's *entry* falls inside new_blk's window (possible
        #       since blocks aren't necessarily placed in entry-time order).
        s4_blocked = False
        for b_other, (a_other, e_other) in zip(placed_in_bay, schedule_in_bay):
            if entry < a_other < exit_t:
                # new_blk is present when b_other enters -- does new_blk
                # obstruct b_other's already-scheduled entry?
                if check_entry(bay, [new_blk], b_other, fast=True):
                    s4_blocked = True
                    break
            if entry < e_other < exit_t:
                # new_blk is present when b_other exits -- does new_blk
                # obstruct b_other's already-scheduled exit?
                if check_exit(bay, [new_blk], b_other, fast=True):
                    s4_blocked = True
                    break
            if (a_other >= entry and e_other <= exit_t
                    and _time_overlaps(entry, exit_t, a_other, e_other)):
                # b_other's whole stay is nested inside new_blk's window --
                # neither check above fires (no boundary of b_other's falls
                # *strictly* inside), so still need the steady-state
                # same-level check for pure coexistence.
                if check_collisions(bay, [new_blk, b_other]):
                    s4_blocked = True
                    break
        if s4_blocked:
            continue

        return entry, exit_t

    return None, None  # no valid time slot for this (position, bay) combination


# -----------------------------------------------------------------------------
# Shared helper: top-K scored candidates for a single block against a given
# bay state. Used by _regret_construct (Phase 1) and xpress_reinsert.py
# (Phase 3) -- both need "not just the single best, but the best few" for a
# block, so this is factored out once instead of duplicated.
# -----------------------------------------------------------------------------

def _top_candidates_for_block(bi: int, blk_data: dict, bays: list[Bay],
                              bay_placed: list[list[Block]],
                              bay_schedule: list[list[tuple[int, int]]],
                              bay_loads: list[float],
                              w1: float, w2: float, w3: float,
                              bay_weights: list[float],
                              max_per_block: int,
                              deadline: float | None) -> list[tuple]:
    """
    Enumerate up to max_per_block (score, bay_id, x, y, orient_idx, entry,
    exit) candidates for block bi against the given bay state, sorted by
    _placement_score ascending (best first). Same search as _place_blocks'
    inner loop, just keeping the top-N instead of only the single best.
    """
    r_time = blk_data["release_time"]
    due = blk_data["due_date"]
    proc = blk_data["processing_time"]
    workload = blk_data["workload"]
    prefs = blk_data["bay_preferences"]
    s_max = max(prefs)

    scored: list[tuple] = []
    for bay_id, bay in enumerate(bays):
        if deadline is not None and time.time() > deadline:
            break
        placed_in_bay = bay_placed[bay_id]
        schedule_in_bay = bay_schedule[bay_id]
        for oi, _ in enumerate(blk_data["shape"]):
            blk_bb = _block_bbox(blk_data, oi)
            candidates = _candidate_positions(bay.width, bay.height, placed_in_bay, blk_bb)
            for cx, cy in candidates:
                if deadline is not None and time.time() > deadline:
                    break
                new_blk = Block(block_id=bi, block_data=blk_data, x=cx, y=cy, orient_idx=oi)
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
                    top_y=cy + blk_bb[3],
                )
                scored.append((score, bay_id, cx, cy, oi, entry, exit_t))

    scored.sort(key=lambda t: t[0])
    return scored[:max_per_block]


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
# Regret-2 dynamic construction (adaptive alternative to static EDD/ATC/slack)
# -----------------------------------------------------------------------------

def _regret_construct(
    blocks_data: list[dict],
    bays: list[Bay],
    bay_placed: list[list[Block]],
    bay_schedule: list[list[tuple[int, int]]],
    bay_loads: list[float],
    w1: float, w2: float, w3: float,
    t_start: float,
    deadline: float,
    regret_k: int = 2,
    log_interval: int = 0,
) -> tuple[dict[int, dict], list[int]]:
    """
    Regret-K insertion: at each step, place the still-unplaced block whose
    gap between its best and (regret_k-1)-th-best candidate score is
    largest -- i.e. the block that stands to lose the most by NOT claiming
    its best slot right now -- instead of a fixed EDD/ATC/slack order. A
    block with fewer than regret_k feasible candidates gets infinite regret
    (this may be its only remaining chance).

    Static-order construction (EDD/ATC/slack) always visits blocks in the
    same fixed order regardless of how contested the space/time each one
    actually needs turns out to be. Regret insertion adapts: a block with
    plenty of good options can wait, a block about to lose its only decent
    option gets placed first.

    Cost and time management: each round re-evaluates every still-unplaced
    block's top candidates against the CURRENT bay state (since committing
    one block changes everyone else's candidates), so this is O(rounds) x
    O(remaining blocks) candidate searches -- O(n^2) in the worst case,
    versus O(n) for static insertion. Not affordable to run to completion
    on large instances within typical timelimits. `deadline` bounds this
    function specifically (independent of the caller's own Phase-1
    deadline): once passed, it returns immediately with whatever it placed
    so far, plus the ids of blocks still unplaced. The caller is expected
    to finish those remaining blocks via the cheap static _place_blocks
    pass, exactly mirroring Phase 1's existing forced-fallback pattern.
    In effect: spend the expensive adaptive budget on the highest-impact
    EARLY decisions (claiming contested space/time first, while the fewest
    blocks are placed and re-evaluation is cheapest), fall back to cheap
    static placement once the remaining set makes full re-evaluation too
    slow to keep affording.

    Returns
    -------
    (result, remaining_ids): result is dict[block_id -> assignment] for
    blocks placed adaptively; remaining_ids (sorted) is what the caller
    must still place by some other (cheaper) means.
    """
    unplaced = set(range(len(blocks_data)))
    result: dict[int, dict] = {}
    n_total = len(blocks_data)

    bay_areas = [bay.width * bay.height for bay in bays]
    avg_area = sum(bay_areas) / len(bays)
    bay_weights = [avg_area / a for a in bay_areas]

    round_idx = 0
    while unplaced:
        if time.time() > deadline:
            break

        best: tuple | None = None  # (regret, block_id, candidate_tuple)
        for bi in unplaced:
            cands = _top_candidates_for_block(
                bi, blocks_data[bi], bays, bay_placed, bay_schedule, bay_loads,
                w1, w2, w3, bay_weights, regret_k, deadline,
            )
            if cands:
                regret = float("inf") if len(cands) < regret_k else (cands[-1][0] - cands[0][0])
                if best is None or regret > best[0]:
                    best = (regret, bi, cands[0])
            if time.time() > deadline:
                break

        if best is None:
            break  # nothing evaluable before deadline -- caller finishes the rest

        _, bi, chosen = best
        score, bay_id, cx, cy, oi, entry, exit_t = chosen
        blk = Block(block_id=bi, block_data=blocks_data[bi], x=cx, y=cy, orient_idx=oi)
        bay_placed[bay_id].append(blk)
        bay_schedule[bay_id].append((entry, exit_t))
        bay_loads[bay_id] += blocks_data[bi]["workload"]
        result[bi] = {
            "block_id": bi, "bay_id": bay_id,
            "x": int(round(cx)), "y": int(round(cy)), "orient_idx": oi,
            "entry_time": int(round(entry)), "exit_time": int(round(exit_t)),
        }
        unplaced.discard(bi)
        round_idx += 1

        if log_interval and round_idx % log_interval == 0:
            elapsed = time.time() - t_start
            print(f"[Greedy]   regret-construct {round_idx}/{n_total} placed, "
                  f"{len(unplaced)} remaining  elapsed={elapsed:.1f}s")

    return result, sorted(unplaced)


# -----------------------------------------------------------------------------
# Main algorithm
# -----------------------------------------------------------------------------

def greedyalgorithm(prob_info: dict, timelimit: float,
                    repair_mode: str = "greedy",
                    priority_rule: str = "edd",
                    atc_k: float = 2.0,
                    annealing: bool = False,
                    blocking_chain: bool = True,
                    z2z3_modes: bool = True,
                    left_justify: bool = True,
                    seed: int | None = 0) -> dict:
    """
    ATC/EDD + Best-Fit Greedy algorithm with post-hoc feasibility repair and
    an anytime tardiness-improvement pass.

    Parameters
    ----------
    prob_info     : instance JSON dict with keys "name", "bays", "blocks", "weights"
    timelimit     : wall-clock time limit in seconds
    repair_mode   : "greedy" (default) or "simple" -- see module docstring for details
    priority_rule : "edd" (default), "slack" (min-slack-first / MST), "atc",
                    or "regret" -- Phase-1 insertion order/construction. See
                    _atc_priority for the ATC adaptation and
                    _regret_construct for the regret-based dynamic
                    construction used here.

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
                    "regret" is a genuinely different (dynamic, not just a
                    different static sort key) construction, added 2026-07-20
                    -- not yet benchmarked against EDD the same way; treat as
                    experimental until analysis/priority_rule_compare.py is
                    re-run with it included.
    atc_k         : ATC lookahead parameter (only used when priority_rule="atc").
                    Larger = closer to pure SPT, smaller = closer to min-slack-first.
    annealing     : False (default) -- Phase 3 stays strict hill-climbing.
                    True -- Phase 3 uses simulated annealing (see _improve's
                    docstring). Experimental as of 2026-07-20, not yet
                    benchmarked against the default.
    left_justify  : True (default) -- run Phase 2.5 (_left_justify) between
                    repair and improve: pull blocks earlier within their own
                    bay/position/orientation wherever a strictly earlier
                    feasible entry exists. Verified with one check_feasibility
                    call on the whole swept result; discarded (pre-sweep
                    state kept) if that check fails or doesn't improve the
                    objective, so this can never make the result worse.
                    Added 2026-07-20 to address bays sitting mostly idle
                    (see analysis/bay_utilization.py) despite real tardiness
                    -- Phase 1 has no incentive to enter a block earlier than
                    strictly necessary once its own tardiness is already 0.
    seed          : RNG seed passed through to _improve's ALNS operator
                    selection (fixed at 0 by default, was unseeded/None
                    before 2026-07-20). Unseeded runs were observed to give
                    materially different objectives on repeat runs of the
                    *same* instance/timelimit (e.g. prob_23 at 180s: 55.6M vs
                    80.6M across two runs) -- fixing the seed makes local
                    testing/comparisons reproducible and removes that
                    variance from the actual submission. Deliberately a fixed
                    constant, not tuned/selected by which value scores best
                    on the local train instances (that would just be
                    overfitting a hyperparameter to local data).

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

    if priority_rule == "regret":
        print(f"[Greedy] {'-' * 56}")
        print("[Greedy] Phase 1 : Regret-2 dynamic construction ...")
        # 2026-07-20 bugfix: this used to give the adaptive loop 70% of
        # Phase 1's budget, leaving only 30% for the EDD fallback. On
        # instances the adaptive loop can't finish (the common case for
        # n>=100, see _regret_construct's O(n^2) cost note), that starved
        # the fallback badly -- e.g. a 300-block instance where regret only
        # placed 16 blocks left the fallback ~30% of Phase 1's budget to
        # place the other 284, which wasn't enough; blocks that then hit
        # THEIR OWN deadline inside _place_blocks got dumped into
        # _force_place (ignores due dates entirely), producing objectives
        # up to ~2500x worse than plain EDD in testing. Flipped the split:
        # adaptive loop now gets at most 25%, fallback gets the rest, and
        # the fallback call is now logged so this is visible instead of
        # silently happening again.
        regret_deadline = t_start + (phase1_deadline - t_start) * 0.25
        assignments, remaining_ids = _regret_construct(
            blocks_data, bays, bay_placed, bay_schedule, bay_loads,
            w1, w2, w3, t_start, regret_deadline,
            log_interval=max(1, n_blocks // 10),
        )
        if remaining_ids:
            print(f"[Greedy] Phase 1 : {len(remaining_ids)} block(s) left after regret "
                  f"budget, falling back to EDD static placement ...")
            fallback_order = sorted(
                remaining_ids,
                key=lambda i: (blocks_data[i]["due_date"], blocks_data[i]["processing_time"])
            )
            rest = _place_blocks(
                fallback_order, blocks_data, bays,
                bay_placed, bay_schedule, bay_loads,
                w1, w2, w3, forced_ids=set(),
                t_start=t_start, log_interval=max(1, len(fallback_order) // 10),
                deadline=phase1_deadline,
            )
            assignments.update(rest)
    else:
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
                          repair_mode=repair_mode, blocking_chain=blocking_chain)

    # -- Phase 2.5: left-justify (pull blocks earlier where possible) ---------
    if left_justify:
        print(f"[Greedy] {'-' * 56}")
        print("[Greedy] Phase 2.5 : left-justify ...")
        from utils import check_feasibility as _cf_lj
        pre_sol = {"operations": _build_operations(list(assignments.values()))}
        pre_result = _cf_lj(prob_info, pre_sol)
        if pre_result["feasible"]:
            justified, n_moved = _left_justify(
                assignments, bays, blocks_data, deadline=t_start + timelimit * 0.85,
            )
            justified_sol = {"operations": _build_operations(list(justified.values()))}
            justified_result = _cf_lj(prob_info, justified_sol)
            if justified_result["feasible"] and justified_result["objective"] <= pre_result["objective"] + 1e-6:
                gain = pre_result["objective"] - justified_result["objective"]
                assignments = justified
                print(f"[Greedy] Left-justify: moved {n_moved} block(s)  "
                      f"obj {pre_result['objective']:.0f} -> {justified_result['objective']:.0f} "
                      f"(-{gain:.0f})")
            else:
                print(f"[Greedy] Left-justify: swept result not better/feasible "
                      f"(feasible={justified_result['feasible']}), keeping pre-sweep state "
                      f"({n_moved} candidate move(s) discarded)")
        else:
            print("[Greedy] Left-justify: skipped (pre-sweep state not feasible)")

    # -- Phase 3: improve feasible-but-tardy assignments with leftover time ---
    print(f"[Greedy] {'-' * 56}")
    print("[Greedy] Phase 3 : improve (worst-tardiness LNS) ...")
    assignments = _improve(prob_info, assignments, bays, blocks_data,
                           w1, w2, w3, t_start, timelimit, atc_k=atc_k,
                           annealing=annealing, z2z3_modes=z2z3_modes, seed=seed)

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

def _select_removal_candidates(
    best_assignments: dict[int, dict],
    blocks_data: list[dict],
    bay_weights: list[float],
    k: int,
    mode: str,
    rng: "random.Random | None" = None,
) -> list[int]:
    """
    Pick up to k block ids for _improve to remove+reinsert this round.

    "tardy"      -- worst current tardiness first (drives Z1).
    "preference" -- worst current preference penalty first (drives Z3):
                    blocks sitting furthest from their most-preferred bay.
    "balance"    -- blocks currently in the single most (weighted-)loaded
                    bay, largest workload first (drives Z2). There's no
                    dedicated swap operator here: removing a block from the
                    heaviest bay and letting the existing reinsertion search
                    reconsider it is enough to reduce Z2 on its own, since
                    _placement_score already penalises adding to an
                    already-heavy bay -- it will naturally tend to land
                    somewhere lighter if that's better.
    "random"     -- uniform random sample of currently-placed blocks, no
                    scoring at all. Added 2026-07-20 alongside ALNS-style
                    adaptive operator weighting: the three scored modes above
                    are all deterministic/greedy in what they target, which
                    can get stuck reworking the same kind of neighborhood
                    repeatedly. A pure-random destroy operator gives the
                    search a genuinely different, unbiased neighborhood each
                    time -- classic ALNS diversification.

    Added 2026-07-20 so Phase 3 can target Z2/Z3 too, not just Z1 -- before
    this, blocks that were fully feasible but sitting in a suboptimal
    bay for balance/preference reasons (rather than being late) were never
    reconsidered by anything in this codebase.
    """
    if mode == "random":
        all_ids = list(best_assignments.keys())
        if not all_ids:
            return []
        kk = min(k, len(all_ids))
        if rng is not None:
            return rng.sample(all_ids, kk)
        return all_ids[:kk]
    if mode == "tardy":
        scored = [
            (bid, a["exit_time"] - blocks_data[bid]["due_date"])
            for bid, a in best_assignments.items()
            if a["exit_time"] - blocks_data[bid]["due_date"] > 0
        ]
        scored.sort(key=lambda t: -t[1])
        return [bid for bid, _ in scored[:k]]

    if mode == "preference":
        scored = []
        for bid, a in best_assignments.items():
            prefs = blocks_data[bid]["bay_preferences"]
            penalty = max(prefs) - prefs[a["bay_id"]]
            if penalty > 0:
                scored.append((bid, penalty))
        scored.sort(key=lambda t: -t[1])
        return [bid for bid, _ in scored[:k]]

    if mode == "balance":
        loads: dict[int, float] = {}
        for bid, a in best_assignments.items():
            loads[a["bay_id"]] = loads.get(a["bay_id"], 0.0) + blocks_data[bid]["workload"]
        if not loads:
            return []
        busiest = max(loads, key=lambda j: bay_weights[j] * loads[j])
        scored = [
            (bid, blocks_data[bid]["workload"])
            for bid, a in best_assignments.items() if a["bay_id"] == busiest
        ]
        scored.sort(key=lambda t: -t[1])
        return [bid for bid, _ in scored[:k]]

    return []


def _improve(prob_info: dict,
            assignments: dict[int, dict],
            bays: list[Bay],
            blocks_data: list[dict],
            w1: float, w2: float, w3: float,
            t_start: float,
            timelimit: float,
            atc_k: float = 2.0,
            k_values: tuple[int, ...] | None = None,
            stall_limit: int | None = None,
            annealing: bool = False,
            initial_temp_frac: float = 0.01,
            cooling_rate: float = 0.98,
            seed: int | None = None,
            z2z3_modes: bool = True) -> dict[int, dict]:
    """
    Large-neighborhood-search-style improvement pass for an already FEASIBLE
    solution, targeting all three objective components (not just Z1).

    _repair only ever touches blocks that are spatially/crane infeasible; a
    solution can be fully feasible and still have large tardiness -- or a
    lopsided bay load, or blocks stuck far from their preferred bay -- with
    nothing in Phases 1-2 ever revisiting it. This pass targets exactly that
    gap: each round, remove a small set of blocks (see
    _select_removal_candidates for how they're chosen -- worst tardiness
    most rounds, worst preference-penalty or the most-loaded bay's blocks on
    others) and re-insert them via the same _place_blocks/xpress_reinsert
    search used everywhere else.

    Only ever called on a solution that is already feasible; if the incoming
    solution is not feasible this returns it unchanged, so it can never be
    blamed for masking a Phase 2 failure. Runs until timelimit*0.99 or
    nothing left to improve, so it is safe to let it simply run out the
    clock -- best_assignments is tracked separately from the walk and is
    always the best *feasible* solution seen, independently re-verified with
    check_feasibility every round (never assumed from a delta estimate), so
    an interruption at any point still returns a solution at least as good
    as what Phase 2 produced.

    annealing=False (default) -- hill-climbing: a round is only ever kept if
    it strictly improves the objective; every rejected round leaves the walk
    exactly where it was, so "current" and "best" never diverge. This is the
    behaviour that's been tested throughout the session so far.

    annealing=True -- simulated annealing: a worse round can still be
    *walked to* (not just discarded) with probability
    exp(-(new_obj - current_obj) / T), T cooling geometrically each round
    from initial_temp_frac * starting objective. This lets the search escape
    local optima a strict hill-climb cannot (e.g. two blocks that would
    together improve things but neither improves alone). best_assignments
    is still only ever updated by a *strictly better feasible* round,
    regardless of what the walk does -- so a bad wander can never make the
    final returned solution worse than what Phase 2 produced. Only ever
    walks to FEASIBLE trials; an infeasible trial is always rejected
    outright, independent of temperature. Experimental as of 2026-07-20 --
    not yet benchmarked against annealing=False.

    k_values are cycled round-robin. None (default) resolves to a spread from
    small (1, 2, 3, 5, 8, 12) up to large (n/10, n/5) removal sizes, capped
    at n/3 -- added 2026-07-20 alongside ALNS operator selection, since a
    single relocate or a 5-block swap can't unblock a structural issue that
    needs a bigger reshuffle, but a large-K round only bothers with Xpress's
    joint reinsertion up to JOINT_MAX_K=12 (its pairwise conflict check is
    O(K^2 x max_per_block^2), too expensive beyond that -- larger K falls
    straight back to the same sequential greedy reinsertion used everywhere
    else).

    Operator selection (2026-07-20): each destroy mode ("tardy",
    "preference", "balance", "random" -- see _select_removal_candidates) has
    an adaptive weight, initialised equal. Each round, one not-yet-tried
    operator is drawn by weighted random choice (roulette wheel); its weight
    is then nudged toward a reward (new best > merely accepted > rejected)
    via an exponential moving average. This is standard ALNS-style adaptive
    operator selection: operators that keep paying off get picked more
    often, ones that stop paying off fade out but never to exactly zero, so
    they can still recover if the landscape changes later in the run.

    Parameters
    ----------
    stall_limit : stop early after this many consecutive rounds with no new
                  *best* (default 3 * len(k_values)). Purely a wall-clock
                  courtesy for local testing -- returning early vs. running
                  to the deadline doesn't affect the score either way, since
                  the leaderboard only sees the final returned solution.
    seed        : RNG seed for the annealing accept/reject draw and the
                  operator-selection/random-destroy draws. None (default)
                  means an unseeded, naturally varying run each time.
    """
    from utils import check_feasibility

    # Larger destroy sizes (2026-07-20): a single relocate or a 5-block swap
    # can't unblock a structural issue that needs a bigger reshuffle. Mix in
    # much larger removal sizes (up to n/3) alongside the original small
    # ones, capped so a single round never removes more than a third of the
    # instance. Resolved here (before stall_limit, which depends on it).
    if k_values is None:
        n = len(blocks_data)
        cap = max(1, n // 3)
        k_values = tuple(sorted({
            k for k in (1, 2, 3, 5, 8, 12, max(1, n // 10), max(1, n // 5))
            if k <= cap
        }))

    if stall_limit is None:
        stall_limit = 3 * len(k_values)

    def _build(a: dict[int, dict]) -> dict:
        return {"operations": _build_operations(list(a.values()))}

    base_result = check_feasibility(prob_info, _build(assignments))
    if not base_result["feasible"]:
        print("[Greedy] Improve: skipped (incoming solution is not feasible)")
        return assignments

    rng = random.Random(seed)
    current_assignments = dict(assignments)
    current_obj = base_result["objective"]
    best_assignments = dict(assignments)
    best_obj = current_obj
    t0 = initial_temp_frac * max(1.0, current_obj)

    p_avg = sum(b["processing_time"] for b in blocks_data) / max(1, len(blocks_data))
    bay_areas = [bay.width * bay.height for bay in bays]
    avg_area = sum(bay_areas) / len(bays)
    bay_weights = [avg_area / a for a in bay_areas]
    deadline = t_start + timelimit * 0.99

    round_idx = 0
    stalled = 0
    # 2026-07-20: modes are now tried in escalating order WITHIN a single
    # round -- "tardy" first always, and "preference"/"balance" only get a
    # turn if tardy's own trial this round didn't yield an accepted move.
    # Previously modes were picked by a fixed round_idx%N schedule regardless
    # of whether tardy was still succeeding, which meant a scheduled
    # preference/balance round could displace a round that would otherwise
    # have gone to a *productive* tardy attempt -- pure opportunity cost,
    # since Phase 3's accept-only-if-better rule can't undo the lost time.
    # Measured effect: on some instances this made the final objective worse
    # than z2z3_modes=False despite never accepting a worse solution (see
    # notes/algorithm_overview.md). Escalating only on failure means every
    # instance where "tardy" alone would have kept succeeding behaves
    # identically to z2z3_modes=False (zero divergence, zero opportunity
    # cost) -- preference/balance only get to spend time that a stalled
    # tardy attempt would otherwise have spent uselessly anyway.
    # 2026-07-20: proper ALNS-style adaptive operator selection, replacing
    # the earlier fixed escalation+pruning scheme. Each operator (destroy
    # mode) has a weight; each round, one operator is picked by
    # weighted-random draw (roulette wheel) among those not yet tried THIS
    # round, and its weight is updated by an exponential moving average
    # toward a reward that reflects how well it did (new best > merely
    # accepted > rejected). This generalises the old "escalate on failure,
    # prune after 4 empty tries" logic into a smooth, continuously-adapting
    # version of the same idea: operators that keep paying off get picked
    # more often, operators that stop paying off fade out (but never to
    # exactly zero, so they can still recover if the landscape changes).
    operator_names = ("tardy", "preference", "balance", "random") if z2z3_modes else ("tardy",)
    op_weight: dict[str, float] = {name: 1.0 for name in operator_names}
    WEIGHT_DECAY = 0.8       # fraction of old weight kept each update
    REWARD_NEW_BEST = 3.0
    REWARD_ACCEPTED = 0.5    # accepted (e.g. an annealing walk) but not a new best
    REWARD_REJECTED = 0.0

    # Joint Xpress reinsertion is O(K^2 x max_per_block^2) in the pairwise
    # conflict check -- fine for the small K's, prohibitively expensive for
    # the large ones just added above. Skip straight to sequential greedy
    # reinsertion once K exceeds this.
    JOINT_MAX_K = 12

    while time.time() < deadline:
        round_accepted = False
        any_candidates = False
        tried_this_round: set[str] = set()

        while len(tried_this_round) < len(operator_names):
            if time.time() >= deadline:
                break

            remaining_ops = [n for n in operator_names if n not in tried_this_round]
            weights = [op_weight[n] for n in remaining_ops]
            mode = rng.choices(remaining_ops, weights=weights, k=1)[0]
            tried_this_round.add(mode)

            k = k_values[round_idx % len(k_values)]
            remove_ids = _select_removal_candidates(
                current_assignments, blocks_data, bay_weights, k, mode, rng
            )
            if not remove_ids:
                continue  # this operator has nothing to offer right now -- try another
            any_candidates = True

            trial_assignments = dict(current_assignments)
            for bid in remove_ids:
                trial_assignments.pop(bid, None)

            bay_placed, bay_schedule, bay_loads = _rebuild_bay_state(
                trial_assignments, bays, blocks_data
            )
            # Try an exact joint reinsertion of the K removed blocks via Xpress
            # first (see xpress_reinsert.py) -- it can find combinations plain
            # greedy can't (deciding all K at once instead of one at a time).
            # reinsert() returns None on any failure (Xpress unavailable, no
            # candidates, solve timeout/infeasible, or K too large -- see
            # JOINT_MAX_K), which is a routine, expected outcome here, not an
            # error -- always fall back to the same greedy _place_blocks
            # search used everywhere else in this codebase. k=1 has nothing
            # to jointly optimize against (no pairwise conflicts possible
            # with a single block), so skip the MIP overhead entirely.
            partial = None
            if 1 < k <= JOINT_MAX_K:
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
                    prev_assignments=current_assignments,
                    deadline=deadline,
                )
            trial_assignments.update(partial)

            trial_result = check_feasibility(prob_info, _build(trial_assignments))
            round_idx += 1
            solver_tag = "xpress" if used_xpress else "greedy"

            if not trial_result["feasible"]:
                op_weight[mode] = WEIGHT_DECAY * op_weight[mode] + (1 - WEIGHT_DECAY) * REWARD_REJECTED
                print(f"[Greedy] Improve round {round_idx}: mode={mode} k={k} via={solver_tag}  "
                      f"infeasible, rejected  w={op_weight[mode]:.2f}")
                continue

            delta = trial_result["objective"] - current_obj
            if annealing:
                temperature = t0 * (cooling_rate ** round_idx)
                accept = delta < 0 or (temperature > 1e-9 and rng.random() < math.exp(-delta / temperature))
            else:
                temperature = 0.0
                accept = delta < -1e-6

            if not accept:
                op_weight[mode] = WEIGHT_DECAY * op_weight[mode] + (1 - WEIGHT_DECAY) * REWARD_REJECTED
                print(f"[Greedy] Improve round {round_idx}: mode={mode} k={k} via={solver_tag}  "
                      f"rejected obj={trial_result['objective']:.0f} (T={temperature:.3g})  "
                      f"w={op_weight[mode]:.2f}")
                continue

            current_assignments = trial_assignments
            current_obj = trial_result["objective"]
            round_accepted = True
            if current_obj < best_obj - 1e-6:
                gain = best_obj - current_obj
                prev_best = best_obj
                best_assignments = current_assignments
                best_obj = current_obj
                stalled = 0
                op_weight[mode] = WEIGHT_DECAY * op_weight[mode] + (1 - WEIGHT_DECAY) * REWARD_NEW_BEST
                elapsed = time.time() - t_start
                print(f"[Greedy] Improve round {round_idx}: mode={mode} k={k} removed={remove_ids} "
                      f"via={solver_tag}  NEW BEST obj {prev_best:.0f} -> {best_obj:.0f} "
                      f"(gain={gain:.0f})  "
                      f"w={op_weight[mode]:.2f}  elapsed={elapsed:.1f}s")
            else:
                op_weight[mode] = WEIGHT_DECAY * op_weight[mode] + (1 - WEIGHT_DECAY) * REWARD_ACCEPTED
                print(f"[Greedy] Improve round {round_idx}: mode={mode} k={k} via={solver_tag}  "
                      f"walked to worse obj={current_obj:.0f} (T={temperature:.3g})  "
                      f"w={op_weight[mode]:.2f}  best still {best_obj:.0f}")
            break  # this round succeeded -- don't also try the remaining operators

        if not any_candidates:
            print(f"[Greedy] Improve: nothing left to improve (Z1/Z2/Z3 all settled)  round={round_idx}")
            break

        if not round_accepted:
            stalled += 1
            if stalled >= stall_limit:
                print(f"[Greedy] Improve: no new best for {stalled} rounds, stopping early  "
                      f"round={round_idx}")
                break

    return best_assignments


# -----------------------------------------------------------------------------
# Phase 2.5: left-justify (RCPSP-style schedule compaction)
# -----------------------------------------------------------------------------

def _left_justify(
    assignments: dict[int, dict],
    bays: list[Bay],
    blocks_data: list[dict],
    deadline: float | None,
) -> tuple[dict[int, dict], int]:
    """
    Pull every block as early as possible within its own bay, position, and
    orientation -- classical RCPSP "left justification" (Valls et al.),
    adapted to this problem's due-date-driven objective instead of a
    makespan.

    Motivation (2026-07-20): Phase 1's construction has no incentive to
    enter a block earlier than strictly necessary once its own tardiness is
    already 0 -- _placement_score rewards low tardiness, bay balance, and
    preference, never "entered early". A block can end up sitting in its bay
    later than it needs to purely as an artifact of construction order,
    needlessly keeping that bay occupied and pushing back whoever needs it
    next -- this is exactly what the bay-utilization analysis surfaced
    (bays mostly idle, yet real tardiness still occurring). This sweep finds
    and removes exactly that kind of avoidable slack.

    For each block, bay-by-bay and earliest-current-entry first (so an
    earlier block's own compaction can free room for the next one within the
    same sweep), keeps its bay/position/orientation fixed and asks
    _find_earliest_slot whether a strictly earlier feasible entry exists
    given every OTHER currently-placed block's schedule in that bay. If so,
    adopts it.

    _find_earliest_slot's crane-feasibility checks gate each individual
    move, but they are one-directional (is THIS block's crane path clear --
    not "does moving it also keep every neighbor's path clear"). The caller
    is expected to verify the whole swept result with one check_feasibility
    call rather than one per move (far cheaper, and this function itself
    never touches shared state so a caller can always discard the result and
    keep the original assignments unchanged if verification fails).

    Returns (new_assignments, n_moved). Never mutates the input assignments.
    """
    trial = dict(assignments)
    n_moved = 0

    for bay_id, bay in enumerate(bays):
        bay_block_ids = sorted(
            (bid for bid, a in trial.items() if a["bay_id"] == bay_id),
            key=lambda bid: trial[bid]["entry_time"],
        )
        for bid in bay_block_ids:
            if deadline is not None and time.time() > deadline:
                return trial, n_moved

            a = trial[bid]
            blk_data = blocks_data[bid]
            r_time = blk_data["release_time"]
            proc = blk_data["processing_time"]

            others = [o for o in bay_block_ids if o != bid]
            placed_others = [
                Block(block_id=o, block_data=blocks_data[o],
                     x=int(trial[o]["x"]), y=int(trial[o]["y"]),
                     orient_idx=trial[o]["orient_idx"])
                for o in others
            ]
            schedule_others = [(trial[o]["entry_time"], trial[o]["exit_time"]) for o in others]

            this_blk = Block(block_id=bid, block_data=blk_data,
                             x=int(a["x"]), y=int(a["y"]), orient_idx=a["orient_idx"])
            new_entry, new_exit = _find_earliest_slot(
                this_blk, bay, placed_others, schedule_others, r_time, proc,
                deadline=deadline,
            )
            if new_entry is not None and new_entry < a["entry_time"]:
                trial[bid] = dict(a, entry_time=int(new_entry), exit_time=int(new_exit))
                n_moved += 1

    return trial, n_moved


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
            repair_mode: str = "greedy",
            blocking_chain: bool = True) -> dict[int, dict]:
    """
    Iteratively detect infeasible blocks and repair them.

    Runs up to max_passes rounds of: check_feasibility -> collect violating
    block ids -> re-place them.  Stops early if the solution becomes feasible
    or 98% of timelimit is consumed.

    Blocking-chain aware (2026-07-20): violation messages from utils.py name
    not just the violating block but, for obstruction/collision violations,
    the other block actually in the way ("block 33 exit obstructed by block
    96"). Both ids get pulled into the repair set, not just the victim, so a
    blocker with slack can be nudged aside instead of always relocating the
    victim to a worse spot. See the parsing block below for details.

    Verified, never-worse repair (2026-07-20, greedy mode only): each pass's
    attempt (joint Xpress or sequential) is built into a throwaway
    trial_assignments and only committed if check_feasibility confirms the
    violation count actually went down (or reached zero). If not, the whole
    to_repair batch is force-placed instead (_force_place is structurally
    crane-feasible by construction), guaranteeing forward progress every
    pass instead of risking a silently-worse state carrying into the next
    one. This mirrors _improve's accept-only-if-better discipline, which
    this phase never had before.

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

        # -- Parse block ids from violation messages (blocking-chain aware) ----
        # Each violation string mentions the violating block ("block <id>")
        # first. Obstruction/collision violations (Stage2/3/4/5 "obstructed
        # by block <id>" / "and block <id> collide") also name the OTHER
        # block actually in the way -- e.g. "block 33 exit obstructed by
        # block 96" means block 96 is the reason block 33 can't leave.
        #
        # The plain victim-only version of this (just re-place block 33)
        # keeps re-searching for a new home for the victim every pass, even
        # when the cheaper fix is to nudge the *blocker* (block 96) out of
        # the way instead -- which may have plenty of slack and let the
        # victim keep its original, better slot. So: pull in blocker ids
        # too, not just victims, and let them compete for re-placement in
        # the same pass via the existing _place_blocks search + scoring
        # (which already prefers low-cost moves, and via prev_assignments
        # will just leave a blocker where it was if moving isn't needed).
        # Multi-hop chains (C blocks B blocks A) fall out naturally across
        # repair passes: if freeing B this pass reveals B itself needs to
        # move C, that shows up as a new/changed violation next pass.
        to_repair: list[int] = []
        seen: set[int] = set()
        for v in viols:
            ids = [int(x) for x in re.findall(r"block (\d+)", v)]
            if not ids:
                continue
            # blocking_chain=False: only ever the victim (ids[0]), matching
            # pre-2026-07-20 behaviour -- kept as an A/B toggle after this
            # change was found to regress the objective in testing (see
            # notes/algorithm_overview.md).
            keep = ids if blocking_chain else ids[:1]
            for bid in keep:
                if bid not in seen:
                    seen.add(bid)
                    to_repair.append(bid)

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
            # assignments (and the state just rebuilt from it) now represents
            # "everything except to_repair" -- the guaranteed-safe baseline
            # this pass starts from. Keep a copy so a failed attempt below
            # can be discarded without corrupting it (see verify step).
            base_assignments = dict(assignments)

            # Try an exact joint reinsertion of the whole to_repair batch via
            # Xpress first when blocking_chain pulled in more than just the
            # victim. 2026-07-20: sequential one-at-a-time reinsertion below
            # has an ordering hazard -- whichever block is processed first
            # (usually the highest-tardiness victim, since to_repair is
            # tardiness-sorted) can accidentally grab a blocker's old spot
            # before the blocker gets a chance to reclaim it, since ALL of
            # to_repair was removed from the state up front. The displaced
            # blocker then lands somewhere new, which can disturb other,
            # previously-fine blocks -- this caused a measured 3-35x
            # objective regression in testing (see notes/algorithm_overview.md).
            # Reusing the same joint-optimization machinery Phase 3 already
            # uses (xpress_reinsert) sidesteps the ordering hazard entirely:
            # every to_repair member's candidates are considered together
            # with proper conflict constraints, not one at a time. Falls back
            # to the existing sequential loop (unchanged) if Xpress is
            # unavailable, any member has zero candidates, or the solve
            # fails -- same safety net used everywhere else Xpress appears.
            joint_partial = None
            if blocking_chain and len(to_repair) > 1:
                try:
                    import xpress_reinsert
                    joint_partial = xpress_reinsert.reinsert(
                        to_repair, blocks_data, bays,
                        bay_placed, bay_schedule2, bay_loads,
                        w1, w2, w3, t_start + timelimit * 0.80,
                    )
                except Exception as _dbg_exc:
                    import traceback
                    print(f"[Greedy] DEBUG repair joint reinsert raised: {_dbg_exc!r}")
                    traceback.print_exc()
                    joint_partial = None
                print(f"[Greedy] DEBUG repair joint reinsert for batch={to_repair} "
                      f"-> {'SUCCESS' if joint_partial is not None else 'None (falling back to sequential)'}")

            if joint_partial is not None:
                trial_assignments = dict(base_assignments)
                trial_assignments.update(joint_partial)
                elapsed_j = time.time() - t_start
                print(f"[Greedy]   repair batch of {n_repl} via=xpress-joint  "
                      f"blocks={to_repair}  elapsed={elapsed_j:.1f}s")
            else:
                trial_assignments = dict(base_assignments)
                for ri, bi in enumerate(to_repair):
                    # Time guard: switch to forced path once 75% of timelimit is
                    # used (was 90% -- tightened alongside the 80% pass-loop cap
                    # above so repair reliably hands time back to Phase 3).
                    # Without this, a slow repair search could exhaust the
                    # timelimit before all blocks are placed, causing Stage-1
                    # (assignment) failures.
                    if time.time() - t_start > timelimit * 0.75:
                        forced_ids.add(bi)
                    prev_a  = trial_assignments.get(bi)
                    partial = _place_blocks(
                        [bi], blocks_data, bays,
                        bay_placed, bay_schedule2, bay_loads,
                        w1, w2, w3, forced_ids,
                        prev_assignments=trial_assignments,
                        deadline=t_start + timelimit * 0.80,
                    )
                    trial_assignments.update(partial)
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

            # -- Verify: did this pass actually reduce violations? --------------
            # 2026-07-20: repair used to commit whatever an attempt produced
            # with no check that it actually helped -- unlike Phase 3, which
            # only ever keeps a round when check_feasibility confirms it's
            # better. An unverified repair attempt (joint or sequential) could
            # silently leave as many or more violations than before (e.g. the
            # crane-constraint gap in xpress_reinsert, fixed separately, or a
            # sequential ordering hazard), and that state would then carry
            # into the next pass and potentially cascade onto unrelated
            # blocks. Mirror Phase 3's discipline: verify, and if this pass
            # didn't actually reduce the violation count, fall back to
            # force-placing the whole to_repair batch instead of keeping the
            # unverified attempt. _force_place is structurally guaranteed
            # crane-feasible (empty-bay window), so this always makes real
            # progress on THESE violations -- placement quality suffers, but
            # the pass can never silently make things worse.
            trial_sol = {"operations": _build_operations(list(trial_assignments.values()))}
            trial_check = check_feasibility(prob_info, trial_sol)
            trial_viol_count = 0 if trial_check["feasible"] else len(trial_check["violations"])

            if trial_check["feasible"] or trial_viol_count < len(viols):
                assignments = trial_assignments
                sol = trial_sol
                elapsed_v = time.time() - t_start
                print(f"[Greedy] Repair pass {pass_idx+1} verified: violations "
                      f"{len(viols)} -> {trial_viol_count}  elapsed={elapsed_v:.1f}s")
            else:
                for bid in to_repair:
                    forced_ids.add(bid)
                bay_placed_f, bay_schedule_f, bay_loads_f = _rebuild_bay_state(
                    base_assignments, bays, blocks_data
                )
                forced_partial = _place_blocks(
                    to_repair, blocks_data, bays,
                    bay_placed_f, bay_schedule_f, bay_loads_f,
                    w1, w2, w3, forced_ids,
                    deadline=t_start + timelimit * 0.80,
                )
                assignments = dict(base_assignments)
                assignments.update(forced_partial)
                sol = {"operations": _build_operations(list(assignments.values()))}
                elapsed_v = time.time() - t_start
                print(f"[Greedy] Repair pass {pass_idx+1}: attempt didn't reduce violations "
                      f"({len(viols)} -> {trial_viol_count}), force-placed batch instead  "
                      f"elapsed={elapsed_v:.1f}s")
            continue

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
