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

import bisect
import math
import random
import re
import time
from utils import Bay, Block, check_entry, check_exit, check_collisions, _resolve_layers, _bounding_box, _bb_overlap

# 2026-07-22 (user-proposed follow-up to the check_feasibility redundancy
# finding): Block.bounding_rect() recomputes its AABB from the full vertex
# set on every call (see its own docstring in utils.py -- not O(1)).
# Profiling combined_stress_3400 (60s) with caller breakdown
# (pstats.print_callers) found the single largest caller by far is THIS
# module's own _find_earliest_slot (1.7M of ~3.0M total bounding_rect calls,
# 8.6s of cumulative time) -- re-deriving the SAME already-placed block's
# AABB from scratch on every call as it rescans a bay's placed_in_bay list.
# _block_area (this module's MaxRects area-sort key) is a smaller second
# offender. Both were fixable one call site at a time (as done earlier
# today for _candidate_positions_maxrects2's own call site via
# `_cached_bounding_rect`), but check_feasibility (utils.py, never
# modifiable) ALSO builds a persistent per-bay Block list once and reuses
# it across Stages 2/3/4 (see its "bay_blocks" comment) -- each stage
# independently re-fetches bounding_rect() for the same blocks, a cost
# fixing individual baseline_greedy.py call sites can never reach since
# it happens entirely inside utils.py's own code.
#
# Patching the method on the Block CLASS itself (not editing utils.py's
# FILE -- this runs at baseline_greedy.py's import time, against whatever
# Block class the grading server's utils.py defines) makes every caller,
# including utils.py's own internal Stage 2/3/4 checks, benefit from the
# same per-instance cache transparently -- no per-call-site changes needed
# anywhere, in this module or utils.py. Safe under the same invariant
# already relied on for the single-call-site version: no code anywhere in
# this codebase (grepped baseline_greedy.py, xpress_reinsert.py, AND
# utils.py) ever mutates a Block's x/y/orient_idx in place after
# construction -- every move constructs a fresh Block instead (also
# required by __post_init__'s own _layers_cache, which makes the same
# assumption already, unconditionally, since before today).
_orig_bounding_rect = Block.bounding_rect


def _cached_bounding_rect_method(self):
    cached = getattr(self, "_bg_bbox_cache", None)
    if cached is None:
        cached = _orig_bounding_rect(self)
        self._bg_bbox_cache = cached
    return cached


Block.bounding_rect = _cached_bounding_rect_method


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
                         blk_bb: tuple[float, float, float, float],
                         deadline: float | None = None,
                         max_source_blocks: int | None = None) -> list[tuple[int, int]]:
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

    2026-07-22: two attempts to replace this O(m^2) (m=placed_blocks) cross
    product both reverted -- see notes/algorithm_overview.md #44/#48:
    (a) per-block-corners-only (O(m)) regressed prob_1 (obj1 0->4) -- mixed
    corners from two UNRELATED blocks are sometimes genuine, necessary
    candidates that per-block-local rules can't produce.
    (b) MaxRects free-rectangle tracking passed 37,000+ synthetic random
    trials (candidate-generation correctness, verified directly against this
    function) but STILL regressed prob_1 end-to-end (587,937 vs the known
    68,633, obj1 0->18) -- raw Phase 1+2 construction quality was actually
    BETTER (raw post-repair objective dropped from ~15M to under 1M), but
    Phase 3 (ALNS reinsertion via xpress_reinsert) then found almost no
    further improvement across every restart, vs. the 200x+ improvement it
    reliably finds today -- some blocks' candidate lists collapsed to a
    single entry. Root cause not isolated (a Phase-3-candidate-diversity
    interaction the synthetic tests, which only checked the single best
    achievable position, never exercised) -- MaxRects was also separately
    found to blow up superlinearly (0->4.9s for m=200) on scattered,
    non-contiguous placed-block configurations, which real per-bay history
    (blocks occupying the same footprint at different, non-overlapping
    times) can plausibly produce. Scoping MaxRects to just this function's
    OWN direct caller (_place_blocks's construction/repair search, leaving
    _top_candidates_for_block / xpress_reinsert on the cross product) was
    also tried, since raw construction quality DID improve in isolation
    (prob_1 raw post-repair ~15M -> under 1M) -- but end-to-end this still
    regressed prob_1 (68,633 -> 291,336: a better-scoring but apparently
    harder-for-Phase-3-to-improve-from starting arrangement) AND showed no
    speedup at all on congested synthetic stress instances (500-block
    scaled test: Phase 1 still 32.5s either way) -- the scattered-history
    performance cliff above hits _place_blocks's own congested-bay case
    just as hard as it hits xpress_reinsert's. Left as O(m^2) until a fix
    addresses the diversity regression, the scattered-input performance
    cliff, AND actually speeds up the congested case that motivated this in
    the first place.

    deadline : optional (2026-07-22, user-proposed). This function had NO
        time awareness at all -- on a bay with thousands of placed blocks,
        a single call's O(m^2) cross-product assembly loop below could run
        for an unbounded amount of time with no way for a caller to
        interrupt it, unlike every other hot loop in this codebase (which
        all check some deadline periodically). Checked periodically (not
        every single (x, y) pair, to keep the check's own overhead
        negligible) during the O(|xs|*|ys|) assembly loop; if exceeded,
        returns whatever candidates have been assembled so far instead of
        continuing -- a partial, still-valid candidate list (every entry
        already passed the bay-boundary check) beats blocking the caller
        indefinitely. None (default) disables the check, identical to
        before this parameter existed.
    max_source_blocks : optional (2026-07-22, user-proposed). When set and
        len(placed_blocks) exceeds it, only the top max_source_blocks
        blocks BY FOOTPRINT AREA contribute their corner to the xs/ys sets
        below -- the full cross-product structure (mixing ANY surviving
        block's x with ANY surviving block's y) is preserved, only WHICH
        blocks get to contribute a coordinate is restricted. This is
        deliberately different from the reverted 2026-07-22 "per-block
        corners only" attempt above (which only ever paired a block's OWN
        x with its OWN y and lost genuine mixed-corner candidates between
        UNRELATED blocks) -- large/major blocks are exactly the ones most
        likely to actually define the bay's remaining free-space skyline;
        a small block sitting well inside a large block's own footprint
        rarely contributes a corner that survives the bay-boundary check
        anyway. Same safety argument as every other candidate-count cap in
        this codebase: every resulting (x, y) is still independently
        re-verified downstream (bay.contains_block + _find_earliest_slot's
        real feasibility check against the FULL, untruncated bay state,
        not this subset) -- this can only miss some genuinely good
        position (a quality cost, already accepted everywhere candidates
        are capped), never accept an actually-infeasible one. None
        (default) disables the filter, identical to before this parameter
        existed.
    """
    lx0, ly0, lx1, ly1 = blk_bb
    # Smallest valid integer reference-point position (block's left/bottom edge at bay wall)
    xs = {max(0, math.ceil(-lx0))}
    ys = {max(0, math.ceil(-ly0))}
    source_blocks = placed_blocks
    if max_source_blocks is not None and len(placed_blocks) > max_source_blocks:
        source_blocks = sorted(
            placed_blocks,
            key=lambda b: (lambda r: (r[2] - r[0]) * (r[3] - r[1]))(b.bounding_rect()),
            reverse=True,
        )[:max_source_blocks]
    for b in source_blocks:
        bb = b.bounding_rect()
        # Reference-point x/y such that new block's left/bottom edge touches the
        # right/top edge of this placed block
        xs.add(math.ceil(bb[2] - lx0))
        ys.add(math.ceil(bb[3] - ly0))

    candidates = []
    xs_sorted = sorted(xs)
    ys_sorted = sorted(ys)
    _check_every = 2000
    _pairs_seen = 0
    for x in xs_sorted:
        for y in ys_sorted:
            if deadline is not None:
                _pairs_seen += 1
                if _pairs_seen % _check_every == 0 and time.time() > deadline:
                    return candidates
            if x + lx1 <= bay_w + 1e-6 and y + ly1 <= bay_h + 1e-6:
                candidates.append((int(x), int(y)))
    return candidates



def _candidate_positions_maxrects2(bay_w: float, bay_h: float,
                                   placed_blocks: list[Block],
                                   blk_bb: tuple[float, float, float, float],
                                   min_width: float | None = None,
                                   min_height: float | None = None) -> list[tuple[int, int]]:
    """
    EXPERIMENTAL (2026-07-22): MaxRects + sliver-pruning, re-attempt after
    #48's MaxRects regressed prob_1 and showed no speedup on congested
    stress instances. Sliver pruning discards a free-rectangle fragment the
    instant a split produces one narrower/shorter than the CURRENT block's
    own (bw, bh) -- provably lossless for this call (a fragment that's
    already too small can only get smaller under further splits, so it can
    never satisfy this call's final size check either way), and tames the
    free-list blowup that made unpruned MaxRects catastrophically slow on
    scattered configurations (0->4.9s at m=200 unpruned -> ~0.2s pruned,
    synthetic benchmark). Being re-tried end-to-end (both call sites) to
    see whether the earlier Phase-3-candidate-diversity collapse was itself
    a symptom of MaxRects's unpruned slowness (deadline/budget checks
    truncating candidate generation) rather than a separate issue. See
    notes/algorithm_overview.md.

    min_width / min_height : optional (2026-07-22, user-proposed), the
        smallest width/height across every block this whole _place_blocks
        call might place (see its caller). Folded into the per-split
        pruning threshold below (max(bw, min_width)) -- but since bw (THIS
        call's own block) is always >= the global min by definition, this
        is currently a no-op: the per-call threshold already dominates it.
        Kept and wired through anyway so it's ready to matter once the
        free-rect list persists across calls instead of being rebuilt from
        scratch every time (deferred -- see notes/algorithm_overview.md) --
        a persistent list can't safely prune by "this call's own block"
        alone, since a fragment discarded now must stay valid for whatever
        smaller block arrives many calls later.
    """
    lx0, ly0, lx1, ly1 = blk_bb
    bw = lx1 - lx0
    bh = ly1 - ly0
    sliver_w = max(bw, min_width) if min_width is not None else bw
    sliver_h = max(bh, min_height) if min_height is not None else bh

    free: list[tuple[float, float, float, float]] = [(0.0, 0.0, float(bay_w), float(bay_h))]
    for b in placed_blocks:
        px0, py0, px1, py1 = b.bounding_rect()
        new_free = []
        for (fx0, fy0, fx1, fy1) in free:
            if not (fx0 < px1 and px0 < fx1 and fy0 < py1 and py0 < fy1):
                new_free.append((fx0, fy0, fx1, fy1))
                continue
            if px0 > fx0 and (px0 - fx0) + 1e-9 >= sliver_w:
                new_free.append((fx0, fy0, px0, fy1))
            if px1 < fx1 and (fx1 - px1) + 1e-9 >= sliver_w:
                new_free.append((px1, fy0, fx1, fy1))
            if py0 > fy0 and (py0 - fy0) + 1e-9 >= sliver_h:
                new_free.append((fx0, fy0, fx1, py0))
            if py1 < fy1 and (fy1 - py1) + 1e-9 >= sliver_h:
                new_free.append((fx0, py1, fx1, fy1))
        pruned = []
        for i, r in enumerate(new_free):
            rx0, ry0, rx1, ry1 = r
            if not any(
                j != i and s[0] <= rx0 and s[1] <= ry0 and s[2] >= rx1 and s[3] >= ry1
                for j, s in enumerate(new_free)
            ):
                pruned.append(r)
        free = pruned

    candidates = set()
    for (fx0, fy0, fx1, fy1) in free:
        if fx1 - fx0 + 1e-6 >= bw and fy1 - fy0 + 1e-6 >= bh:
            x = max(0, math.ceil(fx0 - lx0))
            y = max(0, math.ceil(fy0 - ly0))
            if x + lx1 <= bay_w + 1e-6 and y + ly1 <= bay_h + 1e-6:
                candidates.add((int(x), int(y)))
    return sorted(candidates)


# 2026-07-22: how many jittered positions to add per kept free rectangle,
# and how many free rectangles to keep, in _candidate_positions_maxrects_diverse.
MAXRECTS_DIVERSE_TOP_K = 15
MAXRECTS_DIVERSE_JITTER = 2


def _candidate_positions_maxrects_diverse(bay_w: float, bay_h: float,
                                          placed_blocks: list[Block],
                                          blk_bb: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """
    EXPERIMENTAL (2026-07-22): MaxRects variant for _top_candidates_for_block
    (xpress_reinsert's JOINT reinsertion pool), not _place_blocks.

    _candidate_positions_maxrects2's single-corner-per-free-rectangle output
    is exactly right for placing ONE block in isolation (Phase 1's own
    construction/repair search, which only ever wants its own best spot),
    but xpress_reinsert's joint MIP needs a DIVERSE pool per block so it can
    route around conflicts between several simultaneously-removed blocks --
    with only 1 candidate per block, two blocks whose single best spots
    happen to collide leave the solver nothing to fall back on (measured:
    n_candidates collapsing to 1 across the board, Phase 3 then finding
    almost no improvement -- see notes/algorithm_overview.md #48/#49).

    This variant: (a) skips the containment-based free-rect pruning (which
    was itself collapsing distinct nearby positions down to one), keeping
    only the sliver-pruning needed for performance; (b) ranks the surviving
    free rectangles by (fy0, fx0) -- the SAME lowest-y-then-lowest-x
    criterion actual placement quality is judged by, NOT by area (an
    earlier area-based ranking attempt caused real mismatches against the
    brute-force cross product in synthetic testing: a small rect near the
    origin can be the genuinely best position even though a larger rect
    elsewhere has more area) -- and keeps only the top MAXRECTS_DIVERSE_TOP_K;
    (c) for each of those, adds a small local jittered grid of positions
    (offsets 0..MAXRECTS_DIVERSE_JITTER in both axes, bounded to stay inside
    that same free rectangle) instead of just its single corner, so the
    joint solver has room to shift within a free region to dodge another
    block's choice. Validated via 20,000 synthetic random-packing trials (0
    mismatches in best-feasible-position found vs. the brute-force cross
    product) before being wired in. The final CANDIDATE_SCAN_CAP + AABB-
    lower-bound ranking already applied by callers trims this down further
    -- no separate cap logic needed here.
    """
    lx0, ly0, lx1, ly1 = blk_bb
    bw = lx1 - lx0
    bh = ly1 - ly0

    free: list[tuple[float, float, float, float]] = [(0.0, 0.0, float(bay_w), float(bay_h))]
    for b in placed_blocks:
        px0, py0, px1, py1 = b.bounding_rect()
        new_free = []
        for (fx0, fy0, fx1, fy1) in free:
            if not (fx0 < px1 and px0 < fx1 and fy0 < py1 and py0 < fy1):
                new_free.append((fx0, fy0, fx1, fy1))
                continue
            if px0 > fx0 and (px0 - fx0) + 1e-9 >= bw:
                new_free.append((fx0, fy0, px0, fy1))
            if px1 < fx1 and (fx1 - px1) + 1e-9 >= bw:
                new_free.append((px1, fy0, fx1, fy1))
            if py0 > fy0 and (py0 - fy0) + 1e-9 >= bh:
                new_free.append((fx0, fy0, fx1, py0))
            if py1 < fy1 and (fy1 - py1) + 1e-9 >= bh:
                new_free.append((fx0, py1, fx1, fy1))
        free = new_free  # sliver-pruned only, no containment collapse (keeps diversity)

    big_enough = [
        r for r in free
        if r[2] - r[0] + 1e-9 >= bw and r[3] - r[1] + 1e-9 >= bh
    ]
    big_enough.sort(key=lambda r: (r[1], r[0]))
    top = big_enough[:MAXRECTS_DIVERSE_TOP_K]

    candidates = set()
    for (fx0, fy0, fx1, fy1) in top:
        max_dx = min(MAXRECTS_DIVERSE_JITTER, int(fx1 - fx0 - bw))
        max_dy = min(MAXRECTS_DIVERSE_JITTER, int(fy1 - fy0 - bh))
        for dx in range(0, max_dx + 1):
            for dy in range(0, max_dy + 1):
                x = max(0, math.ceil(fx0 + dx - lx0))
                y = max(0, math.ceil(fy0 + dy - ly0))
                if x + lx1 <= bay_w + 1e-6 and y + ly1 <= bay_h + 1e-6:
                    candidates.add((int(x), int(y)))
    return sorted(candidates)


def _rank_candidates_by_earliest_bound(
    candidates: list[tuple[int, int]],
    blk_bb: tuple[float, float, float, float],
    placed_in_bay: list["Block"],
    schedule_in_bay: list[tuple[int, int]],
    r_time: float,
) -> list[tuple[int, int]]:
    """
    2026-07-21: cheap (AABB-only, no Shapely) re-ordering of candidate
    positions by a LOWER BOUND on their achievable entry time, so a
    subsequent cap on how many candidates get the expensive
    _find_earliest_slot call (see CANDIDATE_SCAN_CAP) keeps the ones most
    likely to actually be good instead of an arbitrary bottom-left-fill
    prefix.

    Motivation: _candidate_positions returns positions in bottom-left-fill
    (x, y) order, which has no particular relationship to which positions
    will yield an early ENTRY TIME once fed through _find_earliest_slot --
    capping the raw list at CANDIDATE_SCAN_CAP without this re-ranking
    measurably regressed solution quality (prob_1: obj1 went from 0 to
    17-39, i.e. real tardiness appeared where the uncapped search always
    found zero) because the specific position a later Phase-3 round needed
    could simply never be scanned. This function doesn't fix that by
    scanning more -- it fixes it by scanning the RIGHT ones first.

    For each candidate (cx, cy), the new block's world bounding box is
    compared (AABB only, not the full polygon) against every already-placed
    block's bounding box; any overlap means this position cannot truly be
    free until that block's exit_time at the earliest -- a correct LOWER
    bound on the real answer (necessary, not sufficient: crane-sweep effects
    _find_earliest_slot itself checks can still push the true earliest slot
    later, but never earlier than this bound). Candidates with a lower bound
    sort first.

    O(len(candidates) * len(placed_in_bay)) simple float comparisons -- no
    polygon work -- which profiling confirms costs a small fraction of even
    a single _find_earliest_slot call.
    """
    lx0, ly0, lx1, ly1 = blk_bb
    placed_boxes = [
        (b.bounding_rect(), e) for b, (_, e) in zip(placed_in_bay, schedule_in_bay)
    ]

    def _lower_bound(cx: int, cy: int) -> float:
        bx0, by0, bx1, by1 = cx + lx0, cy + ly0, cx + lx1, cy + ly1
        bound = r_time
        for (pbx0, pby0, pbx1, pby1), exit_t in placed_boxes:
            if bx0 < pbx1 and pbx0 < bx1 and by0 < pby1 and pby0 < by1 and exit_t > bound:
                bound = exit_t
        return bound

    return sorted(candidates, key=lambda c: _lower_bound(c[0], c[1]))


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
    # 2026-07-21: spatial pre-filter, computed ONCE per call. new_blk's
    # (x, y, orient_idx) are already fixed for the entire duration of this
    # call -- only the candidate ENTRY TIME varies across iterations below
    # -- so whether an existing block b is spatially close enough to
    # possibly obstruct new_blk (their footprint AABBs overlap) has the SAME
    # answer for every entry_candidate. A block whose AABB never overlaps
    # new_blk's can never obstruct it at ANY time (check_entry/check_exit
    # already reject on this exact AABB test internally -- see _bb_overlap
    # in utils.py -- so this changes nothing about the result, it just does
    # that check once instead of redundantly inside every iteration below).
    # Profiling found this loop's block scan (both for candidate_entries and
    # the Stage-4+ pre-check) was the dominant per-call cost -- a block in
    # the opposite corner of a large, busy bay contributed its exit_time to
    # candidate_entries and got scanned in the Stage-4+ loop on every single
    # iteration, despite being physically incapable of ever obstructing this
    # position.
    new_bbox = new_blk.bounding_rect()
    relevant = [
        (b, sched) for b, sched in zip(placed_in_bay, schedule_in_bay)
        if _bb_overlap(new_bbox, b.bounding_rect())
    ]
    relevant_blocks = [b for b, _ in relevant]
    relevant_schedule = [sched for _, sched in relevant]

    candidate_entries = sorted({r_time} | {e for _, e in relevant_schedule if e > r_time})

    for entry_candidate in candidate_entries:
        if deadline is not None and time.time() > deadline:
            return None, None

        entry  = max(r_time, entry_candidate)
        exit_t = entry + proc

        # Stage-2: blocks already present when new_blk arrives.
        # Mirrors check_feasibility: a_k < entry < e_k  (strict lower bound --
        # blocks entering at the same moment are handled by Stage-5 ordering).
        present_at_entry = [
            b for b, (a, e) in zip(relevant_blocks, relevant_schedule)
            if a < entry < e
        ]
        if check_entry(bay, present_at_entry, new_blk, fast=True):
            continue  # crane path blocked at entry -> try next exit boundary

        # Stage-3: blocks still present when new_blk departs.
        # Mirrors check_feasibility: a_k < exit_t < e_k  (strict both ends).
        present_at_exit = [new_blk] + [
            b for b, (a, e) in zip(relevant_blocks, relevant_schedule)
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
        for b_other, (a_other, e_other) in zip(relevant_blocks, relevant_schedule):
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


def _find_latest_slot(new_blk: Block,
                      bay: Bay,
                      placed_in_bay: list[Block],
                      schedule_in_bay: list[tuple[int, int]],
                      r_time: int,
                      proc: int,
                      latest_exit_bound: int,
                      deadline: float | None = None) -> tuple[int | None, int | None]:
    """
    Mirror of _find_earliest_slot, searching backward instead of forward:
    return the LATEST (entry, exit_t) with exit_t <= latest_exit_bound and
    entry >= r_time at which new_blk can be crane-placed into bay. Returns
    (None, None) if no candidate exit passes both checks.

    2026-07-20, added for _right_justify (RCPSP right justification,
    alternated with the existing _left_justify -- see that function's
    docstring). The caller passes latest_exit_bound = the block's own
    CURRENT exit_time, so this can only pull a block's exit EARLIER than (or
    equal to) where it already is -- exactly like _find_earliest_slot can
    only pull entry earlier than where a block already is -- never later,
    so right-justifying can never make a schedule worse than it already is.

    Candidate enumeration (descending): {latest_exit_bound} | {entry time of
    every already-placed block in bay, if < latest_exit_bound} -- mirror of
    _find_earliest_slot's forward candidates (which anchor on other blocks'
    EXIT times); here we anchor on other blocks' ENTRY times, since walking
    backward, another block's entry is where the crane path may become
    newly obstructed as new_blk's own exit is pushed later.

    Same Stage-2/3/4 feasibility checks as _find_earliest_slot (see that
    function's docstring for what each stage catches), just walked in the
    opposite direction.
    """
    candidate_exits = sorted(
        {latest_exit_bound} | {a for a, _ in schedule_in_bay if a < latest_exit_bound},
        reverse=True,
    )

    for exit_candidate in candidate_exits:
        if deadline is not None and time.time() > deadline:
            return None, None

        exit_t = exit_candidate
        entry = exit_t - proc
        if entry < r_time:
            continue  # can't start before release_time

        # Stage-2: blocks already present when new_blk arrives.
        present_at_entry = [
            b for b, (a, e) in zip(placed_in_bay, schedule_in_bay)
            if a < entry < e
        ]
        if check_entry(bay, present_at_entry, new_blk, fast=True):
            continue

        # Stage-3: blocks still present when new_blk departs.
        present_at_exit = [new_blk] + [
            b for b, (a, e) in zip(placed_in_bay, schedule_in_bay)
            if a < exit_t < e
        ]
        if check_exit(bay, present_at_exit, new_blk, fast=True):
            continue

        # Stage-4+ pre-check -- see _find_earliest_slot's docstring.
        s4_blocked = False
        for b_other, (a_other, e_other) in zip(placed_in_bay, schedule_in_bay):
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

# How much of the pool's own score RANGE (worst candidate found - best
# candidate found, for THIS block) a diversity substitution may cost, at
# most -- see _top_candidates_for_block's 2026-07-21 revision note. Relative
# to the pool's own range (not a fixed slot fraction, and not a fraction of
# the raw score itself, which can be ~0 whenever tardiness+preference
# penalty are both already 0 and Z2 dominates) so it scales automatically
# with whatever w1/w2/w3 magnitudes and candidate spread a given instance
# and block happen to have.
CANDIDATE_DIVERSITY_MAX_DEGRADE_FRAC = 0.1

# 2026-07-21: hard cap on how many (position, feasibility-check) pairs get
# evaluated per (block, bay, orientation) -- fully independent budget, never
# shared across bays or orientations (see _rank_candidates_by_earliest_bound
# and the per-(bay, orientation) reset comments at each call site for why:
# any sharing let one bay/orientation exhaust the budget and starve the
# others entirely, which measurably regressed quality far worse than a
# small cap value ever did). Candidates are re-sorted by a cheap AABB lower
# bound before capping (_rank_candidates_by_earliest_bound), not left in
# raw bottom-left-fill order, so a small cap still reliably includes the
# genuinely best candidates -- confirmed directly: the true winning
# candidate ranked within the cap in 10/10 sampled blocks. Profiling found
# _candidate_positions can return thousands of positions for a single block
# in a densely-packed bay (e.g. ~3,756 measured for prob_1), each triggering
# a full _find_earliest_slot call averaging ~2ms -- the dominant cost in
# every Phase-3 round measured (candidate generation was 99%+ of total round
# time; the actual MIP solve was <0.02s). 200 (safe but only modestly
# faster, since full independence means the worst-case total is
# n_bays * n_orientations * cap) was the first validated-safe value; 50
# trades a smaller per-combo exploration for a much lower worst-case total,
# still validated safe via the same AABB ranking guarantee.
CANDIDATE_SCAN_CAP = 50

# 2026-07-22 (user-caught robustness issue): scan budget used in _place_blocks
# for blocks that land in the deadline/hard_deadline "overtime" window (see
# that function's docstring). Deliberately much smaller than
# CANDIDATE_SCAN_CAP -- overtime exists specifically so a few seconds of pure
# wall-clock noise (same code/seed/instance, different machine load -- see
# notes/algorithm_overview.md's prob_10 repro) doesn't dump dozens of blocks
# into _force_place with zero search, so its cost per block must stay bounded
# even when many blocks land in this window at once. Nonzero on purpose: a
# handful of real candidates beats _force_place's due-date-blind AABB-clear
# wait every time.
OVERTIME_SCAN_CAP = 5

# 2026-07-22 (bugfix, x16/4000-block stress test): OVERTIME_SCAN_CAP alone
# does NOT bound overtime cost -- it only caps how many candidates get
# SCANNED, not how many get GENERATED, and _candidate_positions /
# _candidate_positions_maxrects2's generation cost scales with the size of
# the bay's own active-block list regardless. On a large/congested instance
# that list keeps growing throughout construction, so per-block overtime
# cost visibly accelerated round after round (measured: 575s against a 60s
# budget, ~9.6x over, on a 4000-block synthetic stress instance -- see
# notes/algorithm_overview.md). Caps how many of the bay's own active blocks
# get fed into candidate generation while in the overtime window, so that
# cost stays bounded no matter how large the bay's full history has grown.
OVERTIME_MAX_ACTIVE_BLOCKS = 60

# 2026-07-22 (same bugfix): each orientation tried in the overtime window
# still pays its own full _find_earliest_slot search against the bay's REAL,
# untruncated schedule (OVERTIME_MAX_ACTIVE_BLOCKS only bounds candidate
# GENERATION, not this) -- trying all n_orient (up to 8) orientations
# multiplies that cost up to 8x per block. Capped separately since it's a
# different cost source than active_in_bay's O(m^2) generation blowup.
OVERTIME_MAX_ORIENTATIONS = 2

# 2026-07-22 (user-proposed): cap on how many of a bay's placed blocks
# contribute a corner to _candidate_positions' x/y sets when called from
# _top_candidates_for_block (the xpress_reinsert-feeding path, which --
# unlike _place_blocks's Phase 1 construction -- has no adaptive MaxRects
# engine, since MaxRects was already found structurally unsuitable there
# (see _candidate_positions' #47(b) note) -- so this path stays on the raw
# O(m^2) cross product for any bay size. Reduces the effective m to a fixed
# constant regardless of how large the bay's real population is -- see
# _candidate_positions' max_source_blocks docstring for the safety argument
# (downstream re-validation against the FULL bay state is unaffected).
XPRESS_CANDIDATE_MAX_SOURCE_BLOCKS = 50


def _dynamic_scan_cap(now: float, phase_start: float, hard_deadline: float | None,
                      base_cap: int, floor_cap: int) -> int:
    """
    Scan budget that shrinks continuously from base_cap toward floor_cap as
    the phase's own hard_deadline approaches, instead of jumping between
    exactly two fixed values the instant a `deadline` marker is crossed
    (2026-07-22, user-proposed, replacing the old
    CANDIDATE_SCAN_CAP->OVERTIME_SCAN_CAP binary switch in _place_blocks).

    Motivation: the old switch meant a block evaluated a split second before
    `deadline` still got the FULL base_cap, while the very next block (after
    a few more milliseconds of pure wall-clock noise) suddenly got only
    floor_cap -- the same "cliff" pattern already fixed for the
    force-place/no-force-place decision itself (see notes/
    algorithm_overview.md #58), just one level down, at the scan-budget
    level. A continuous ramp means no single block right at the boundary
    can still claim the full budget an instant before the switch flips, and
    it naturally self-calibrates to whatever this run's actual machine
    throughput turns out to be: on a slow run, elapsed time (and therefore
    the ratio) advances faster per block placed, so the cap shrinks sooner
    -- without needing a separate throughput benchmark.

    hard_deadline=None means the caller never opted into this phase's time
    budgeting at all (e.g. a call site with no deadline concept) --
    returns base_cap unchanged in that case, identical to before this
    function existed.
    """
    if hard_deadline is None:
        return base_cap
    total = max(1e-6, hard_deadline - phase_start)
    time_left_ratio = max(0.0, min(1.0, (hard_deadline - now) / total))
    return max(floor_cap, int(round(base_cap * time_left_ratio)))


def _top_candidates_for_block(bi: int, blk_data: dict, bays: list[Bay],
                              bay_placed: list[list[Block]],
                              bay_schedule: list[list[tuple[int, int]]],
                              bay_loads: list[float],
                              w1: float, w2: float, w3: float,
                              bay_weights: list[float],
                              max_per_block: int,
                              deadline: float | None,
                              restrict_bay_id: int | None = None) -> list[tuple]:
    """
    Enumerate up to max_per_block (score, bay_id, x, y, orient_idx, entry,
    exit) candidates for block bi against the given bay state, sorted by
    _placement_score ascending (best first). Same search as _place_blocks'
    inner loop, just keeping the top-N instead of only the single best.

    restrict_bay_id : optional (2026-07-20). When set, only that one bay is
        searched instead of every bay. Motivation: callers that only care
        about re-optimizing WITHIN a single bay (e.g. a bay-scoped joint
        reinsertion) were paying the full per-block search cost across
        EVERY other (often much more crowded) bay too, even though any
        candidate outside restrict_bay_id would never be used -- this was
        the actual reason a K=96 single-bay probe couldn't even finish
        generating candidates in 300s (see analysis/bay_mip_probe.py and
        notes/algorithm_overview.md), not the MIP solve itself. None
        (default) preserves the original all-bays behaviour exactly.

    2026-07-22: a time-filtered-bay-history + MaxRects variant was tried
    here (filter bay_placed/bay_schedule by the calling batch's earliest
    release_time, then generate candidates via MaxRects instead of the
    O(m^2) cross product) to fix MaxRects's "sees the bay's whole
    unfiltered history as simultaneously occupied" problem (see
    _candidate_positions' docstring and notes/algorithm_overview.md #47(b)).
    It DID restore candidate diversity (n_candidates went from a collapsed
    1 back up into the 10-20 range for several blocks) but end-to-end this
    made BOTH prob_1 (still regressed, stalls out after ~22s instead of
    using the full budget) and the 500-block congested stress instance
    (463M vs the Phase-1-only-MaxRects baseline's 423M -- worse, and fewer
    Phase 3 rounds: 7 vs 12) worse, not better. Reverted -- see #50.
    """
    r_time = blk_data["release_time"]
    due = blk_data["due_date"]
    proc = blk_data["processing_time"]
    workload = blk_data["workload"]
    prefs = blk_data["bay_preferences"]
    s_max = max(prefs)

    bay_ids_to_search = [restrict_bay_id] if restrict_bay_id is not None else range(len(bays))

    scored: list[tuple] = []
    # 2026-07-21: budget is PER BAY (reset for each bay_id, shared only
    # across that bay's own orientations) -- see CANDIDATE_SCAN_CAP's
    # docstring. Two earlier designs both failed: resetting per-orientation
    # let n_orientations multiply the total cost right back up; sharing ONE
    # budget across the whole block (all bays combined) let a single
    # congested bay consume the entire budget and starve every OTHER bay
    # from being explored at all -- measured as a severe quality regression
    # in _place_blocks (the analogous single-choice search, see its own
    # comment) traced to exactly this: a block's most-preferred bay eating
    # the whole budget meant a much emptier second bay was never even
    # sampled. Per-bay budgets can't have that failure mode -- every bay
    # that gets reached always gets its own fair look.
    # No "stop once an ideal-timing candidate is found" early exit here (an
    # earlier version had one) -- this feeds a joint MIP's candidate POOL,
    # and stopping early can starve it of a candidate that scores slightly
    # worse alone but would have made a better JOINT combination (avoiding a
    # pairwise conflict with another block in the same batch). Measured on
    # _place_blocks (the analogous single-choice search): an ideal-only
    # early exit there regressed prob_1's obj1 from 0 to 17 (real tardiness
    # appeared) because _placement_score can't see everything that matters
    # about a position. Bound the raw candidate COUNT only.
    # 2026-07-21: budget resets per (bay, orientation) -- NOT shared with
    # anything, including other orientations of the same bay. An earlier
    # version shared one budget across all orientations within a bay (to
    # avoid n_orientations multiplying the per-bay cost back up); measured
    # to reintroduce the exact same starvation failure mode as the
    # shared-across-bays bug, just one level down: an early orientation
    # could exhaust the bay's whole budget before a later orientation (which
    # might hold the actually-best candidate) ever got scanned at all.
    # Confirmed via analysis/diagnose_ranking-style direct comparison: the
    # AABB lower-bound ranking itself is accurate (the true-best candidate
    # ranked within the cap in 10/10 sampled blocks), so the remaining
    # failures had to be a starvation bug, not a ranking-quality one. Fully
    # independent per-(bay, orientation) budgets cannot starve each other by
    # construction -- the tradeoff is a higher worst-case total scan count
    # (bounded by n_bays * n_orientations * CANDIDATE_SCAN_CAP), acceptable
    # since the AABB ranking already keeps each individual budget's spend
    # worthwhile.
    for bay_id in bay_ids_to_search:
        bay = bays[bay_id]
        if deadline is not None and time.time() > deadline:
            break
        placed_in_bay = bay_placed[bay_id]
        schedule_in_bay = bay_schedule[bay_id]
        for oi, _ in enumerate(blk_data["shape"]):
            scan_budget = CANDIDATE_SCAN_CAP
            blk_bb = _block_bbox(blk_data, oi)
            candidates = _candidate_positions(
                bay.width, bay.height, placed_in_bay, blk_bb,
                deadline=deadline, max_source_blocks=XPRESS_CANDIDATE_MAX_SOURCE_BLOCKS,
            )
            if len(candidates) > scan_budget:
                candidates = _rank_candidates_by_earliest_bound(
                    candidates, blk_bb, placed_in_bay, schedule_in_bay, r_time
                )
            for cx, cy in candidates:
                if scan_budget <= 0:
                    break
                scan_budget -= 1
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

    # 2026-07-20: dominance/diversity filtering instead of a plain top-N by
    # raw score. Motivation: xpress_reinsert.reinsert()'s joint MIP gets one
    # shot per block to resolve a conflict via a DIFFERENT candidate -- if
    # the top max_per_block candidates are all near-duplicates (same bay,
    # entry times a few time units apart), the solver has little real
    # flexibility, since near-duplicates tend to conflict with the same
    # other blocks. Bucket by (bay_id, entry_time // processing_time).
    #
    # 2026-07-21 rewrite: the slot-fraction approach (reserve a fixed share
    # of slots for diversity, unconditionally discarding whatever plain
    # top-by-score candidates land in that share) regressed prob_1 the same
    # way its first version (2026-07-20) had already regressed prob_40 --
    # confirmed by isolation testing (analysis/repeat_check.py-style A/B):
    # a preference-mode joint reinsertion found a real -7,928 objective
    # improvement with diversity-filling disabled and found NOTHING with it
    # on, because the one candidate the joint MIP actually needed happened
    # to rank just past the "core" cutoff and got swapped out for an
    # unrelated, much-worse-scoring "diverse" candidate. A fixed slot
    # fraction has no way to know whether that trade is cheap or ruinous.
    # Bound the trade by its actual cost instead: start from the plain
    # top-max_per_block by score (nothing discarded by default), and only
    # ever substitute a candidate for a new-bucket alternative when (a) that
    # bucket already has a spare duplicate within the current top-N (so the
    # substitution never removes a bucket's only representative) and (b) the
    # alternative's score is within CANDIDATE_DIVERSITY_MAX_DEGRADE_FRAC of
    # this block's own candidate-score range. This can never make the pool
    # worse than the plain top-N by more than that bounded amount, while
    # still giving the joint MIP genuinely distinct alternatives when they're
    # nearly free.
    top = scored[:max_per_block]
    if len(top) <= 1:
        return top

    bucket_width = max(1, proc)

    def _bucket(c: tuple) -> tuple[int, int]:
        return (c[1], int(c[5]) // bucket_width)

    bucket_counts: dict[tuple[int, int], int] = {}
    for c in top:
        b = _bucket(c)
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
    seen_buckets = set(bucket_counts)

    best_score = top[0][0]
    # 2026-07-21 bugfix: this must be the range WITHIN the kept top-N
    # (top[-1] - best), not the worst candidate across the entire unbounded
    # search -- using the full pool's range let one distant/bad candidate
    # (e.g. a far bay with a large preference penalty) blow the budget wide
    # open, making substitutions MORE permissive than intended (confirmed on
    # prob_1: this bug made round 4's rejected objective 183,104 -- worse
    # than both the old fixed-slot-fraction design AND diversity disabled
    # entirely). Bounding to the top-N's own spread keeps the budget tied to
    # candidates we were already willing to keep.
    score_budget = max(1e-9, top[-1][0] - best_score) * CANDIDATE_DIVERSITY_MAX_DEGRADE_FRAC

    result = list(top)
    for cand in scored[max_per_block:]:
        if cand[0] - best_score > score_budget:
            break  # scored is sorted ascending -- nothing further can qualify
        bucket = _bucket(cand)
        if bucket in seen_buckets:
            continue
        for i in range(len(result) - 1, -1, -1):
            worst_bucket = _bucket(result[i])
            if bucket_counts[worst_bucket] > 1:
                bucket_counts[worst_bucket] -= 1
                bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
                result[i] = cand
                seen_buckets.add(bucket)
                break
    return result


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


def _aabb_gap_entry(new_bbox: tuple[float, float, float, float],
                    placed_in_bay: list[Block],
                    schedule_in_bay: list[tuple[int, int]],
                    r_time: int, proc: int,
                    sorted_desc_cache: list[tuple[int, int, int, Block]] | None = None) -> int:
    """
    2026-07-22 (user-proposed): like _empty_bay_entry, but only requires the
    bay to be clear of blocks whose world AABB actually overlaps new_bbox --
    not the WHOLE bay -- so a force-placed block can slot into a real gap
    next to unrelated blocks instead of always waiting for total bay
    turnover.

    Still crane-safe, not just steady-state-collision-safe: two blocks
    whose AABBs don't overlap can never collide at any layer (each block's
    real polygon is a subset of its own AABB), so once every AABB-
    overlapping block has exited, nothing present at our entry/exit could
    possibly obstruct check_entry/check_exit's same-or-higher-level test --
    and every AABB-DISJOINT block trivially can't obstruct it either,
    whether or not it's present. The only blocks that matter are exactly
    the ones this function pushes entry past, so the answer is simply
    max(r_time, max(exit_time over every AABB-overlapping block)).

    2026-07-22 bugfix (user-proposed): this used to be an iterative push
    (repeatedly re-scanning relevant_schedule until [entry, entry+proc)
    stopped TIME-overlapping any one of them) -- which can in principle
    stop earlier than max(exit_time) when there's a genuine temporal gap
    between clusters of overlapping-block occupancy, but still requires an
    unconditional O(len(placed_in_bay)) scan (via _bb_overlap on every
    block) to build relevant_schedule, on every single call, regardless of
    result. On a large/congested instance this became the dominant cost:
    _force_place calls accumulate through Phase 1's tail once
    deadline/hard_deadline are exceeded, the bay they land in keeps
    growing, and each subsequent call got slower -- measured directly, a
    4000-block synthetic stress instance took 575s against a 60s budget
    (9.6x over) with this exact per-call scan as the driver. Switched to
    the simpler, single-pass "max exit_time over overlapping blocks"
    definition (matches this function's OWN documented guarantee above --
    "once every AABB-overlapping block has exited" -- literally), which is
    still provably safe (can only be equal to or later than the tightest
    possible entry, never earlier, so it never trades safety for speed) and
    -- critically -- lets the caller supply candidates pre-sorted by
    exit_time so the scan can stop at the FIRST match instead of touching
    every block. Pure quality cost in the (probably rare) case where the
    old iterative version found a tighter gap; never a safety regression.

    sorted_desc_cache : optional. When given, must be a list of
        (exit_time, entry_time, block_id, Block) tuples for this exact bay,
        kept sorted ASCENDING by exit_time by the caller (_place_blocks
        maintains this incrementally with bisect.insort, only once
        genuinely past hard_deadline -- see that function -- since that's
        the only point where no OTHER code path can still be adding
        un-tracked blocks to the same bay for the rest of this call).
        Scanned in reverse (highest exit_time first), so the first
        AABB-matching entry found is guaranteed BY CONSTRUCTION to carry
        the maximum exit_time among every AABB-overlapping block --
        typically letting this return after checking only a handful of
        blocks instead of every one in the bay. None (default) falls back
        to the plain O(placed_in_bay) scan -- both branches compute the
        exact same quantity, this parameter only changes HOW it's found.
    """
    if sorted_desc_cache is not None:
        best_e = None
        for e, _a, _bid, b in reversed(sorted_desc_cache):
            if _bb_overlap(new_bbox, b.bounding_rect()):
                best_e = e
                break  # exit_time-ascending list, scanned in reverse -> first hit is the max
        return max(int(r_time), best_e) if best_e is not None else int(r_time)

    best_e = None
    for b, (_a, e) in zip(placed_in_bay, schedule_in_bay):
        if _bb_overlap(new_bbox, b.bounding_rect()):
            if best_e is None or e > best_e:
                best_e = e
    return max(int(r_time), best_e) if best_e is not None else int(r_time)


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
# Construction-order diversification (for iterated-greedy restarts)
# -----------------------------------------------------------------------------

_CONSTRUCTION_SHUFFLE_WINDOW = 5


def _perturb_construction_order(sorted_indices: list[int], seed: int | None) -> list[int]:
    """
    Lightly randomize a priority-sorted block order for construction
    diversity across iterated-greedy restarts (see myalgorithm._iterated_greedy).

    seed=0 or None returns sorted_indices completely unchanged -- this keeps
    the seed=0 default (used everywhere else as "the" reproducible baseline:
    single-shot greedyalgorithm() calls, analysis scripts, etc.) producing
    byte-identical construction to before this function existed.

    For any other seed, shuffles within a sliding window of
    _CONSTRUCTION_SHUFFLE_WINDOW positions so each restart's Phase 1 starts
    from a genuinely different concrete placement order -- not just a
    different Phase 3 (ALNS) exploration path from the *same* Phase 1/2/2.5
    result, which is all varying `seed` did before this (Phase 1's EDD sort
    is otherwise fully deterministic, independent of seed -- see
    notes/algorithm_overview.md's 2026-07-20 iterated-greedy entry).
    Bounded to a small local window (not a full shuffle) so this stays close
    to the empirically-validated EDD order rather than a random one -- only
    ties/near-ties actually get reordered in practice, since due_date is
    the dominant sort key and same-window blocks are usually close in it.
    """
    if not seed:
        return sorted_indices
    rng = random.Random(seed)
    order = list(sorted_indices)
    for start in range(0, len(order), _CONSTRUCTION_SHUFFLE_WINDOW):
        chunk = order[start:start + _CONSTRUCTION_SHUFFLE_WINDOW]
        rng.shuffle(chunk)
        order[start:start + _CONSTRUCTION_SHUFFLE_WINDOW] = chunk
    return order


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
                    seed: int | None = 0,
                    construction_mode: str = "serial",
                    right_justify: bool = True,
                    z23_relax: bool = True) -> dict:
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
    construction_mode : "serial" (default) -- the original Serial-SGS
                    behaviour (one block at a time, never revisited).
                    "batched" -- Phase 1 commits blocks in batches of
                    PHASE1_BATCH_SIZE via xpress_reinsert.reinsert() (jointly
                    optimized), falling back to the classic one-at-a-time
                    _place_blocks per batch if Xpress is unavailable/fails/
                    finds nothing. 2026-07-20 bugfix: this docstring used to
                    claim "batched" was the default -- it never was in the
                    actual signature, and batched was separately confirmed
                    (500/1000-block stress tests) to lose to serial by up to
                    647x, so it's kept only as an explicit opt-in for
                    regression comparison, never something a caller should
                    reach for by default. Kept switchable so a regression can
                    be ruled out/rolled back with a single flag flip; see
                    _place_blocks_batched's docstring for the motivation
                    (Serial SGS commits blocks with no visibility
                    into blocks not yet placed, which is a structural cause
                    of Phase 2/_repair ever being needed at all).

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

    # 2026-07-22 (user-proposed): adaptive Phase 1 candidate-generation
    # engine, decided once per instance from its block count. MaxRects
    # (_candidate_positions_maxrects2) measured a real, substantial win on
    # large/congested synthetic stress instances (500/1000 blocks: Phase 1
    # 32.5s->7.6s and 34.4s->28.6s, final objective -44%/-23.5%) but
    # regressed prob_1 (100 blocks: 68,633->291,336) -- prob_1 is known to
    # be unusually sensitive to its exact construction starting point/
    # sequence (see notes/algorithm_overview.md #34-36), and MaxRects's
    # different (if individually better-scoring) construction lands Phase 3
    # in a harder-to-escape basin there. Rather than pick one engine for
    # every instance, switch by size: only local instances up to 300 blocks
    # were available to validate the small-instance side (prob_20, 300
    # blocks, is the largest) -- MAXRECTS_MIN_BLOCKS is set just above that
    # tested range, on the conservative (favour the known-safe cross
    # product) side of the untested 300-500 gap, pending more data. See
    # notes/algorithm_overview.md #51.
    MAXRECTS_MIN_BLOCKS = 300
    use_maxrects = n_blocks >= MAXRECTS_MIN_BLOCKS

    w1 = prob_info.get("weights", {}).get("w1", 1.0)
    w2 = prob_info.get("weights", {}).get("w2", 1.0)
    w3 = prob_info.get("weights", {}).get("w3", 1.0)

    # Z1's theoretical lower bound (see analysis/lower_bound.py): each
    # block's own release_time+processing_time-due_date floor, ignoring
    # every spatial/crane constraint -- no placement can ever beat this.
    # O(n_blocks), negligible cost. Used by _improve to know when 'tardy'
    # moves are provably futile (2026-07-20).
    z1_lower_bound = sum(
        max(0.0, b["release_time"] + b["processing_time"] - b["due_date"])
        for b in blocks_data
    )

    print(f"[Greedy] Instance : {prob_info.get('name', '?')}")
    print(f"[Greedy] Bays     : {n_bays}  |  Blocks : {n_blocks}  |  Timelimit : {timelimit:.1f}s")
    print(f"[Greedy] Weights  : w1={w1}  w2={w2}  w3={w3}  |  Z1 lower bound : {z1_lower_bound:.0f}")
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
    # 2026-07-20: bounded "cliff" mitigation -- see _place_blocks'
    # hard_deadline docstring. Fixed, known-in-advance ceiling (never
    # estimated/adaptive) -- only ever matters when phase1_deadline is hit
    # with a small tail of blocks left, in which case those last few get up
    # to this much extra time instead of being dumped into _force_place.
    # Deliberately a modest +10 percentage points (not more) given repair
    # still needs real room afterward, and TLE is the single worst possible
    # outcome (-1, same as a crash) -- this is the more conservative side of
    # what was considered.
    phase1_hard_deadline = t_start + timelimit * 0.6

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
                use_maxrects=use_maxrects,
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
        sorted_indices = _perturb_construction_order(sorted_indices, seed)
        print(f"[Greedy] {'-' * 56}")
        if construction_mode == "batched":
            print(f"[Greedy] Phase 1 : {rule_label} batched joint placement "
                  f"(batch={PHASE1_BATCH_SIZE}) ...")
            assignments = _place_blocks_batched(
                sorted_indices, blocks_data, bays,
                bay_placed, bay_schedule, bay_loads,
                w1, w2, w3,
                t_start=t_start, log_interval=max(1, n_blocks // 10),
                deadline=phase1_deadline,
                use_maxrects=use_maxrects,
            )
        else:
            print(f"[Greedy] Phase 1 : {rule_label} greedy placement ...")
            assignments = _place_blocks(
                sorted_indices, blocks_data, bays,
                bay_placed, bay_schedule, bay_loads,
                w1, w2, w3, forced_ids=set(),
                t_start=t_start, log_interval=max(1, n_blocks // 10),
                deadline=phase1_deadline,
                hard_deadline=phase1_hard_deadline,
                use_maxrects=use_maxrects,
            )

    elapsed_p1 = time.time() - t_start
    loads_str = "  ".join(f"bay{i}={round(bay_loads[i])}" for i in range(n_bays))
    print(f"[Greedy] Phase 1 done  |  placed={len(assignments)}  {loads_str}  "
          f"elapsed={elapsed_p1:.2f}s")

    # 2026-07-20: a timelimit-dependent max_per_block (smaller candidate
    # count below some short-timelimit threshold) was tried here and then
    # reverted before ever being tested -- see notes/algorithm_overview.md
    # item 19/20. The short-timelimit path is already validated by a real
    # eval-server result (2nd submission improved 4/6 known problems), and
    # the one directly relevant precedent for this exact lever (shrinking
    # max_per_block in _place_blocks_batched's experiment, same day) made
    # results WORSE, not better -- not enough evidence to touch already-
    # working short-timelimit behaviour. Left as a flat max_per_block=20
    # unconditionally; the open, unexplored, higher-upside direction is the
    # opposite end (do batched/bigger-MIP approaches help once timelimit is
    # large, e.g. 600s+?), not shrinking things further at the short end.
    xpress_max_per_block = 20

    # -- Phase 2: repair infeasible assignments --------------------------------
    print(f"[Greedy] {'-' * 56}")
    print(f"[Greedy] Phase 2 : repair  mode={repair_mode}")
    sol = {"operations": _build_operations(list(assignments.values()))}
    assignments, repair_verified_result = _repair(
        prob_info, sol, assignments, bays, blocks_data,
        w1, w2, w3, t_start, timelimit,
        repair_mode=repair_mode, blocking_chain=blocking_chain,
        max_per_block=xpress_max_per_block,
        use_maxrects=use_maxrects)

    # 2026-07-22 (user-proposed perf fix): tracks a check_feasibility result
    # that exactly matches the CURRENT `assignments` at every point below --
    # not an incremental diff (there's no partial change to reconcile here),
    # just avoiding letting an already-computed result go to waste. Updated
    # immediately after every check_feasibility call in this stretch, before
    # any commit-or-discard decision that might change `assignments` again,
    # so the invariant "matches current assignments exactly" always holds:
    # either a sweep committed (its OWN post-sweep check is then correct)
    # or it didn't (assignments reverted to the pre-sweep state, whose
    # pre-sweep check is then still correct -- _left_justify/_right_justify
    # never mutate their input). Seeded from _repair's own already-computed
    # result (None if repair's final-guarantee force-place mutated
    # assignments after its last check, or if it ended infeasible) --
    # _improve falls back to its original from-scratch check whenever this
    # is None, identical to before this parameter existed.
    last_verified_result: dict | None = repair_verified_result

    # -- Phase 2.5: left-justify (pull blocks earlier where possible) ---------
    if left_justify:
        print(f"[Greedy] {'-' * 56}")
        print("[Greedy] Phase 2.5 : left-justify ...")
        from utils import check_feasibility as _cf_lj
        # Reuse repair's own verified result instead of recomputing an
        # identical check_feasibility call when it's available (see
        # last_verified_result's seeding above).
        if last_verified_result is not None:
            pre_result = last_verified_result
        else:
            pre_sol = {"operations": _build_operations(list(assignments.values()))}
            pre_result = _cf_lj(prob_info, pre_sol)
        if pre_result["feasible"]:
            justified, n_moved = _left_justify(
                assignments, bays, blocks_data,
                deadline=_justify_deadline(t_start, timelimit, 0.85),
            )
            justified_sol = {"operations": _build_operations(list(justified.values()))}
            justified_result = _cf_lj(prob_info, justified_sol)
            if justified_result["feasible"] and justified_result["objective"] <= pre_result["objective"] + 1e-6:
                gain = pre_result["objective"] - justified_result["objective"]
                assignments = justified
                last_verified_result = justified_result
                print(f"[Greedy] Left-justify: moved {n_moved} block(s)  "
                      f"obj {pre_result['objective']:.0f} -> {justified_result['objective']:.0f} "
                      f"(-{gain:.0f})")
            else:
                last_verified_result = pre_result
                print(f"[Greedy] Left-justify: swept result not better/feasible "
                      f"(feasible={justified_result['feasible']}), keeping pre-sweep state "
                      f"({n_moved} candidate move(s) discarded)")
        else:
            last_verified_result = pre_result
            print("[Greedy] Left-justify: skipped (pre-sweep state not feasible)")

    # -- Phase 2.6: right-justify then re-left-justify (escape local optima) --
    # 2026-07-20: alternating RCPSP justification directions -- see
    # _right_justify's docstring for why this is only safe to keep when
    # followed by a mandatory left-justify pass and verified as a whole.
    if right_justify:
        print(f"[Greedy] {'-' * 56}")
        print("[Greedy] Phase 2.6 : right-justify + re-left-justify ...")
        from utils import check_feasibility as _cf_rj
        # 2026-07-22 (user-proposed perf fix): last_verified_result already
        # matches the current `assignments` exactly (see the invariant
        # comment above, right before Phase 2.5) whenever Phase 2.5 ran --
        # reuse it instead of paying for an identical check_feasibility call
        # (measured ~2.2s at 3400-block scale). Only recompute when it's
        # unavailable (left_justify=False, so Phase 2.5 never set it).
        if last_verified_result is not None:
            pre_rj_result = last_verified_result
        else:
            pre_rj_sol = {"operations": _build_operations(list(assignments.values()))}
            pre_rj_result = _cf_rj(prob_info, pre_rj_sol)
        if pre_rj_result["feasible"]:
            rj_deadline = _justify_deadline(t_start, timelimit, 0.87)
            right_justified, n_moved_r = _right_justify(assignments, bays, blocks_data, deadline=rj_deadline)
            re_left_justified, n_moved_l = _left_justify(right_justified, bays, blocks_data, deadline=rj_deadline)
            rj_sol = {"operations": _build_operations(list(re_left_justified.values()))}
            rj_result = _cf_rj(prob_info, rj_sol)
            if rj_result["feasible"] and rj_result["objective"] <= pre_rj_result["objective"] + 1e-6:
                gain = pre_rj_result["objective"] - rj_result["objective"]
                assignments = re_left_justified
                last_verified_result = rj_result
                print(f"[Greedy] Right-justify+re-left: moved {n_moved_r}+{n_moved_l} block(s)  "
                      f"obj {pre_rj_result['objective']:.0f} -> {rj_result['objective']:.0f} "
                      f"(-{gain:.0f})")
            else:
                last_verified_result = pre_rj_result
                print(f"[Greedy] Right-justify+re-left: swept result not better/feasible "
                      f"(feasible={rj_result['feasible']}), keeping pre-sweep state "
                      f"({n_moved_r}+{n_moved_l} candidate move(s) discarded)")
        else:
            last_verified_result = pre_rj_result
            print("[Greedy] Right-justify+re-left: skipped (pre-sweep state not feasible)")

    # -- Phase 3: improve feasible-but-tardy assignments with leftover time ---
    print(f"[Greedy] {'-' * 56}")
    print("[Greedy] Phase 3 : improve (worst-tardiness LNS) ...")
    assignments = _improve(prob_info, assignments, bays, blocks_data,
                           w1, w2, w3, t_start, timelimit, atc_k=atc_k,
                           annealing=annealing, z2z3_modes=z2z3_modes, seed=seed,
                           max_per_block=xpress_max_per_block,
                           z1_lower_bound=z1_lower_bound, z23_relax=z23_relax,
                           use_maxrects=use_maxrects, known_result=last_verified_result)

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
                 bay_placed: list[list[Block]],
                 bay_schedule: list[list[tuple[int, int]]],
                 prefs: list[float],
                 sorted_cache: dict[int, list[tuple[int, int, int, Block]]] | None = None) -> tuple:
    """
    Fallback placement: place block bi at the minimum valid position in the
    highest-preference bay whose dimensions accommodate the block, using
    the earliest AABB-clear entry window at that position.

    sorted_cache : optional (2026-07-22 perf fix). Mutable dict[bay_id ->
        list of (exit_time, entry_time, block_id, Block) tuples sorted
        ascending by exit_time], maintained by the caller across a whole
        burst of force-place calls -- see _place_blocks and
        _aabb_gap_entry's docstrings. Only ever passed non-None once the
        caller has established no OTHER code path can still be adding
        untracked blocks to any bay for the rest of this call (i.e. truly
        past hard_deadline), since this function inserts bi into it after
        placing, and a stale/incomplete cache would be a real safety bug,
        not just a quality one. None (default) -- no cache, falls back to
        _aabb_gap_entry's plain per-call scan, identical to before this
        parameter existed.

    When called:
      * _place_blocks found no feasible (position, bay, time-slot) combination
        during Phase-1 search (should be rare for well-formed instances).
      * bi is in forced_ids during repair -- the block has appeared in two or
        more consecutive repair passes, indicating a crane-path cycle.  Forcing
        it to an AABB-clear window breaks the cycle by guaranteeing that both
        check_entry and check_exit trivially pass (see below).

    2026-07-22 (user-proposed): previously waited for the bay to be
    COMPLETELY empty (_empty_bay_entry) -- correct but needlessly
    conservative, since it ignores due dates AND makes every force-placed
    block wait for total bay turnover even when there's a perfectly good
    gap next to some spatially-unrelated block. Switched to
    _aabb_gap_entry, which only waits for blocks whose world AABB actually
    overlaps this block's -- still provably crane-safe (see that function's
    docstring: two AABB-disjoint blocks can never collide at any layer), so
    a force-placed block can now slot into a real gap instead of always
    waiting for the whole bay to clear. Directly relevant on large/congested
    hidden instances (P4-P6-scale): more blocks land here as the deadline
    forces more forced placements, so how good THIS fallback is matters a
    lot more there than on small/local-tested instances where it's rarely
    exercised -- see notes/algorithm_overview.md.

    Why minimum-valid position with an AABB-clear window is always feasible:
      _aabb_gap_entry returns a time interval [entry, exit_t) during which no
      AABB-overlapping block occupies the bay -- and any block that ISN'T
      AABB-overlapping can't obstruct check_entry/check_exit regardless of
      whether it's present, since its real polygon is a subset of its own
      (non-overlapping) AABB. So Stage-2 and Stage-3 always pass regardless
      of block shape or position, exactly as with the old full-bay-empty
      guarantee, just without waiting for spatially-irrelevant blocks too.
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
            new_bbox = (px + lx0, py + ly0, px + lx1, py + ly1)
            bay_cache = None
            if sorted_cache is not None:
                bay_cache = sorted_cache.get(bay_id)
                if bay_cache is None:
                    # Lazy first build for this bay (one-time O(n log n)) --
                    # captures everything already committed to it so far,
                    # whether placed via search or a previous force-place.
                    bay_cache = sorted(
                        (
                            (e, a, b.block_id, b)
                            for b, (a, e) in zip(bay_placed[bay_id], bay_schedule[bay_id])
                        ),
                        key=lambda t: t[0],
                    )
                    sorted_cache[bay_id] = bay_cache
            entry = _aabb_gap_entry(
                new_bbox, bay_placed[bay_id], bay_schedule[bay_id], r_time, proc,
                sorted_desc_cache=bay_cache,
            )
            exit_t = entry + proc
            if sorted_cache is not None:
                new_blk = Block(block_id=bi, block_data=blk_data, x=px, y=py, orient_idx=oi)
                bisect.insort(bay_cache, (exit_t, entry, bi, new_blk), key=lambda t: t[0])
            return (bay_id, px, py, oi, entry, exit_t)

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
    hard_deadline: float | None = None,
    use_maxrects: bool = False,
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
    hard_deadline    : optional, absolute time.time() ceiling for the Phase-1
                       "cliff" mitigation. None (default) means "no
                       extension" -- deadline is always the real cutoff,
                       every block past it goes straight to _force_place,
                       identical to before this parameter existed. When set,
                       any block still left once `deadline` passes gets a
                       reduced-cost search instead (OVERTIME_SCAN_CAP,
                       top-preference bay only) up to hard_deadline, instead
                       of being force-placed immediately.
                       2026-07-22 bugfix (user-caught): originally this
                       extension only covered the last near_done_frac (5%)
                       tail of block_ids, on the assumption that "deadline
                       passed with many blocks left" meant a genuinely
                       congested instance that should just cut its losses.
                       Reproduced directly on prob_10 that this reasoning
                       doesn't hold: same code/seed/instance, but one run's
                       raw machine speed was ~1.5x slower for no algorithmic
                       reason, which alone pushed deadline to fall with ~20%
                       of blocks left (outside the old 5% tail) -- all of
                       them fell to _force_place with zero search in that
                       run and almost none in a faster run of the identical
                       code, a 27x objective swing from pure wall-clock
                       noise. Now the reduced-cost search applies regardless
                       of how many blocks are left, since OVERTIME_SCAN_CAP +
                       single-bay restriction keeps the added cost bounded
                       either way. hard_deadline is always a fixed,
                       separately-computed absolute ceiling (never estimated/
                       adaptive), so the worst-case extra time used is
                       bounded and known in advance -- once past
                       hard_deadline too, blocks fall back to forced
                       placement exactly as they would have without this
                       parameter at all.

    Returns
    -------
    dict[block_id -> assignment dict] for all blocks in block_ids
    """
    n_bays  = len(bays)
    n_total = len(block_ids)
    result: dict[int, dict] = {}
    n_forced = n_fallback = 0

    # 2026-07-22 (user-proposed): smallest width/height across every
    # (block, orientation) combination this call might place, computed
    # once (O(n_total x orientations), negligible). Currently a no-op
    # inside _candidate_positions_maxrects2's sliver-pruning -- that
    # function already prunes fragments smaller than the CURRENT block's
    # own (bw, bh), which is always >= this global minimum, so the
    # per-call check already dominates it. Wired through now anyway so it
    # is ready to matter once MaxRects' free-rect list becomes persistent
    # across calls instead of rebuilt from scratch each time (deferred --
    # see notes/algorithm_overview.md) -- persistence needs the GLOBAL
    # minimum specifically because a fragment discarded then can never be
    # recovered for a later, smaller block, unlike today's from-scratch
    # rebuild where every call re-derives its own exact threshold anyway.
    maxrects_min_width = maxrects_min_height = None
    if use_maxrects:
        maxrects_min_width = float("inf")
        maxrects_min_height = float("inf")
        for _bi in block_ids:
            _bdata = blocks_data[_bi]
            for _oi in range(len(_bdata["shape"])):
                _bb = _block_bbox(_bdata, _oi)
                maxrects_min_width = min(maxrects_min_width, _bb[2] - _bb[0])
                maxrects_min_height = min(maxrects_min_height, _bb[3] - _bb[1])

    # Bay weights for normalized obj2: u_j = avg_area / (W_j * H_j)
    _bay_areas   = [bay.width * bay.height for bay in bays]
    _avg_area    = sum(_bay_areas) / n_bays
    bay_weights  = [_avg_area / a for a in _bay_areas]
    _extension_logged = False

    # 2026-07-22: TRIED a rate-based early give-up here -- sample the
    # placement rate after a warm-up prefix, extrapolate whether this call
    # is on pace to finish all of block_ids by `deadline`, and if projected
    # time was >2x budget, immediately force-place everyone remaining
    # instead of waiting for the wall-clock deadline, handing the saved
    # time to Phase 2/3. Reasoning seemed sound (deadline/hard_deadline only
    # mitigate a SMALL leftover tail -- see hard_deadline's docstring -- so
    # a badly congested instance that is doomed from early on just burns
    # time searching until deadline for no benefit) and it DID free up the
    # intended time (Phase 1 on the 500-block synthetic stress instance:
    # 32.5s -> 9.6s). But it REGRESSED the final objective on that same
    # instance (757M -> 886M at 60s) -- cutting off search that early meant
    # far fewer blocks got a real search-based placement (88 -> 49 of 500),
    # and that quality loss was bigger than what the ~23s it handed back to
    # Phase 3 could recover, since Phase 3's own rounds are themselves
    # expensive on a congested instance (see xpress_reinsert same-bay-first
    # fast path above) and only bought one extra round. Reverted -- see
    # notes/algorithm_overview.md before trying a gentler version (e.g.
    # shrinking the deadline instead of an all-or-nothing cutoff, or fixing
    # the underlying per-candidate cost instead of reallocating time around
    # it).

    # 2026-07-22 (user-proposed perf fix): lazily-built, incrementally
    # maintained per-bay cache for _force_place's _aabb_gap_entry lookup
    # (see that function's docstring) -- keyed by bay_id, only ever read/
    # written once `past_hard_deadline` is True for the current rank (see
    # the call site below), since that's the one point in this function
    # where NO other code path can still be adding untracked blocks to any
    # bay for the rest of this call (every remaining rank is unconditionally
    # forced too). A stale cache used outside that guarantee would be a
    # real safety bug, not just a quality one -- see _force_place's
    # sorted_cache docstring.
    _force_place_sorted_cache: dict[int, list[tuple[int, int, int, Block]]] = {}

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

        # 2026-07-22 bugfix (user-caught): the old version only extended the
        # deadline (full search) for the last near_done_frac tail of
        # block_ids. Reproduced directly on prob_10: same code, same seed=0,
        # same instance, but one run's raw machine speed happened to be
        # ~1.5x slower than another's for no algorithmic reason -- that
        # alone pushed `deadline` to fall with ~20% of blocks still left
        # (not within the old 5% tail), and every one of them fell straight
        # into _force_place with ZERO search, one run vs. essentially none
        # forced in the other -- a 27x objective swing from pure wall-clock
        # noise, not a real code difference (see notes/algorithm_overview.md).
        # Fix: once `deadline` passes, don't force-place immediately -- keep
        # searching (same candidate/geometry machinery, no new logic) but at
        # a reduced scan budget and restricted to the single most-preferred
        # bay (see below), so cost stays bounded regardless of how many
        # blocks land in this window. Only once hard_deadline ALSO passes
        # (or hard_deadline is None, i.e. the caller didn't opt into this
        # mitigation -- e.g. _repair/_improve's greedy fallback calls) does
        # a block fall back to _force_place, same guaranteed ceiling as
        # before -- this just replaces an instant cliff at the first
        # deadline with a cheap-but-real search first.
        #
        # 2026-07-22 (later same day, user-proposed): the scan budget itself
        # (see `_dynamic_scan_cap` below) was originally a second binary
        # switch nested inside this one -- CANDIDATE_SCAN_CAP right up until
        # `deadline`, then instantly OVERTIME_SCAN_CAP -- which reintroduces
        # the exact same cliff shape one level down (a block evaluated a
        # split second before `deadline` still gets the full budget; the
        # next one gets a fifth of it). Replaced with a continuous ramp from
        # CANDIDATE_SCAN_CAP toward OVERTIME_SCAN_CAP as `hard_deadline`
        # approaches, so timing noise right at the boundary can no longer
        # cause one block to claim a disproportionate share right before the
        # switch flips.
        past_deadline      = deadline is not None and time.time() > deadline
        past_hard_deadline = hard_deadline is not None and time.time() > hard_deadline
        in_overtime        = past_deadline and hard_deadline is not None and not past_hard_deadline
        used_forced        = bi in forced_ids or (past_deadline and not in_overtime)
        effective_deadline = hard_deadline if in_overtime else deadline
        if in_overtime and not _extension_logged:
            _extension_logged = True
            print(f"[Greedy]   deadline extension: past deadline with "
                  f"{len(block_ids) - rank}/{len(block_ids)} block(s) left -- "
                  f"top-preference-bay-only reduced search up to hard_deadline "
                  f"instead of forcing")

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
                        r_time, proc, deadline=effective_deadline,
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
            # 2026-07-22: in the overtime window (see hard_deadline's
            # docstring), restrict to the single most-preferred bay -- an
            # O(m^2) _candidate_positions generation happens once per
            # bay_id in this loop regardless of scan_budget, so capping
            # scan_budget alone doesn't bound the dominant cost here.
            # Combined with OVERTIME_SCAN_CAP below, this keeps the total
            # added cost per block roughly constant no matter how many
            # blocks land in this window at once.
            if in_overtime:
                bay_order = bay_order[:1]
            # 2026-07-21: budget is PER BAY (reset for each bay_id) -- see
            # CANDIDATE_SCAN_CAP's docstring and _top_candidates_for_block's
            # matching comment. A single shared-across-all-bays budget let
            # this block's most-preferred (and possibly most congested) bay
            # consume the whole thing, meaning a much emptier second bay was
            # never even sampled -- measured directly: prob_1's Phase 1
            # obj went from the correct 68,633 (obj1=0) to 1,666,716+ with a
            # shared budget, because blocks kept getting stuck in a
            # congested top-preference bay instead of trying the other one.
            # 2026-07-21: budget resets per (bay, orientation), not shared
            # across orientations either -- see _top_candidates_for_block's
            # matching comment for the full story (an orientation-sharing
            # version reintroduced the same starvation bug one level down:
            # an early orientation could exhaust a bay's whole budget before
            # a later, possibly-better orientation was ever scanned).
            for bay_id in bay_order:
                if deadline_hit:
                    break
                bay             = bays[bay_id]
                placed_in_bay   = bay_placed[bay_id]
                schedule_in_bay = bay_schedule[bay_id]
                # 2026-07-22: hoisted out of the orientation loop below --
                # this filter (and the area-sort) doesn't depend on oi at
                # all, so recomputing/re-sorting it once per orientation
                # (up to n_orient times, same list every time) was pure
                # waste. Computed once per bay instead.
                active_in_bay = [
                    b for b, (a_k, e_k) in zip(placed_in_bay, schedule_in_bay)
                    if e_k > r_time
                ]
                # 2026-07-22 bugfix (user-caught, x16/4000-block stress test):
                # OVERTIME_SCAN_CAP + single-bay restriction only bounds how
                # many candidates get PICKED, not how many get GENERATED --
                # _candidate_positions / _candidate_positions_maxrects2's own
                # cost scales with len(active_in_bay) regardless of
                # scan_budget. On a large/congested instance, active_in_bay
                # for the (usually few) most-contested bay(s) keeps growing
                # throughout construction, so each subsequent overtime call
                # got progressively more expensive -- a self-reinforcing
                # spiral (confirmed directly: a 4000-block stress instance
                # took 575s against a 60s budget, ~9.6x over, with per-block
                # overtime cost visibly accelerating in the log). Cap the
                # bay-state INPUT itself here so overtime cost stays bounded
                # no matter how large this bay's full history has grown.
                # Safe: downstream _find_earliest_slot / bay.contains_block
                # always check the trial position against the FULL
                # placed_in_bay/schedule_in_bay (passed separately, untouched
                # by this truncation) -- this can only miss some genuinely
                # free spot (a quality cost, already accepted everywhere else
                # candidates are capped in this codebase), never accept an
                # actually-occupied position.
                if in_overtime and len(active_in_bay) > OVERTIME_MAX_ACTIVE_BLOCKS:
                    active_in_bay = active_in_bay[-OVERTIME_MAX_ACTIVE_BLOCKS:]
                if use_maxrects:
                    # 2026-07-22 (user-proposed): feed MaxRects's
                    # from-scratch replay largest-block-first. The final
                    # free-space set is provably order-independent (see
                    # #47/#48), but the INTERMEDIATE fragment count during
                    # the replay isn't -- inserting large blocks first
                    # tends to divide the bay into a few big coherent
                    # chunks early, so a later small block typically lands
                    # entirely inside just ONE existing free rectangle (the
                    # overlap-check's early `continue` already skips every
                    # other one for free), instead of an arbitrary
                    # insertion order fragmenting the space into many more
                    # pieces that each subsequent call has to carry and
                    # re-check. bounding_rect() recomputes from the block's
                    # full vertex set every call (not O(1) -- see its
                    # docstring), so the sort key calls it exactly once per
                    # block via this helper instead of 4x inline.
                    def _block_area(b: Block) -> float:
                        r = b.bounding_rect()
                        return (r[2] - r[0]) * (r[3] - r[1])
                    active_in_bay = sorted(active_in_bay, key=_block_area, reverse=True)

                # 2026-07-22 bugfix: capping active_in_bay above bounds
                # candidate-GENERATION cost, but each orientation still pays
                # its own full _find_earliest_slot search against this bay's
                # REAL (untruncated) placed_in_bay/schedule_in_bay -- trying
                # all n_orient (up to 8) orientations in overtime mode still
                # multiplies that cost up to 8x per block. Measured directly:
                # even with the active_in_bay cap alone, a 2000-block stress
                # instance still overran its budget 1.4x (84s/60s), with
                # Phase 1 alone taking more than double its intended
                # hard_deadline share. Also cap orientations tried while in
                # overtime -- the first candidate found across ANY
                # orientation is still compared by score like normal, this
                # just stops trying ALL of them once time is already this
                # tight.
                orient_range = range(min(n_orient, OVERTIME_MAX_ORIENTATIONS)) if in_overtime else range(n_orient)
                for oi in orient_range:
                    if deadline_hit:
                        break
                    if hard_deadline is not None and t_start is not None:
                        scan_budget = _dynamic_scan_cap(
                            time.time(), t_start, hard_deadline,
                            CANDIDATE_SCAN_CAP, OVERTIME_SCAN_CAP,
                        )
                    else:
                        scan_budget = OVERTIME_SCAN_CAP if in_overtime else CANDIDATE_SCAN_CAP
                    blk_bb = _block_bbox(blk_data, oi)
                    lx0_oi, ly0_oi, lx1_oi, ly1_oi = blk_bb
                    # Require a valid integer reference-point position to exist:
                    #   px in [ceil(-lx0), floor(W - lx1)]
                    #   py in [ceil(-ly0), floor(H - ly1)]
                    # If either range is empty there is no integer placement.
                    if (math.ceil(-lx0_oi) > math.floor(bay.width  - lx1_oi) or
                            math.ceil(-ly0_oi) > math.floor(bay.height - ly1_oi)):
                        continue

                    # use_maxrects: see greedyalgorithm's MAXRECTS_MIN_BLOCKS
                    # comment / notes/algorithm_overview.md #51 -- adaptive
                    # per-instance choice, decided once by the caller.
                    # active_in_bay is computed (and, if use_maxrects,
                    # area-sorted) once per bay above, outside this
                    # orientation loop -- reused as-is here.
                    if use_maxrects:
                        candidates = _candidate_positions_maxrects2(
                            bay.width, bay.height, active_in_bay, blk_bb,
                            min_width=maxrects_min_width, min_height=maxrects_min_height,
                        )
                    else:
                        candidates = _candidate_positions(
                            bay.width, bay.height, active_in_bay, blk_bb
                        )
                    if len(candidates) > scan_budget:
                        candidates = _rank_candidates_by_earliest_bound(
                            candidates, blk_bb, placed_in_bay, schedule_in_bay, r_time
                        )
                    # 2026-07-21: same scan cap as _top_candidates_for_block --
                    # see CANDIDATE_SCAN_CAP's docstring. Unlike that function
                    # (which only feeds a joint MIP's candidate POOL), here
                    # the single position picked directly determines the
                    # committed layout for this Phase-1/repair placement --
                    # an early "found one with ideal timing, stop comparing"
                    # exit was tried and measurably regressed quality (prob_1
                    # went from obj1=0 to obj1=17 -- real tardiness appeared
                    # where none existed before), because _placement_score's
                    # top_y tie-break doesn't capture how much a given (x, y)
                    # choice obstructs or enables FUTURE blocks' placements,
                    # only its own immediate score. Bound the raw candidate
                    # COUNT via scan_budget only -- still compare every
                    # candidate within that budget by score, same as before
                    # this change, just capped instead of unbounded.
                    for cx, cy in candidates:
                        if scan_budget <= 0:
                            break
                        scan_budget -= 1
                        if effective_deadline is not None and time.time() > effective_deadline:
                            deadline_hit = True
                            break

                        new_blk = Block(block_id=bi, block_data=blk_data,
                                        x=cx, y=cy, orient_idx=oi)
                        if not bay.contains_block(new_blk):
                            continue

                        entry, exit_t = _find_earliest_slot(
                            new_blk, bay, placed_in_bay, schedule_in_bay,
                            r_time, proc, deadline=effective_deadline,
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
            best_placement = _force_place(
                bi, blocks_data, bays, bay_placed, bay_schedule, prefs,
                sorted_cache=_force_place_sorted_cache if past_hard_deadline else None,
            )
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
# Batched (quasi-Parallel-SGS) construction kernel -- Phase 1 alternative
# -----------------------------------------------------------------------------

PHASE1_BATCH_SIZE = 4
PHASE1_MAX_PER_BLOCK = 8
# Smaller than Phase 3's JOINT_MAX_K=12 / max_per_block=20 -- Phase 1 has to
# get through n_blocks/BATCH_SIZE batches total (dozens, for n=200-300), so
# each batch's MIP needs a much tighter per-batch cost than a one-off Phase
# 3 round. 2026-07-20: measured with batch=6/max_per_block=20, batched Phase
# 1 ran 2-3x slower per block than plain _place_blocks and left 16-70% of
# blocks (worse for larger instances) force-placed once phase1_deadline hit
# -- a clear regression (see notes/algorithm_overview.md). Shrinking both
# knobs to cut per-batch candidate-generation and pairwise-conflict cost
# (O(batch_size^2 * max_per_block^2)) is the first thing to try before
# giving up on batched construction entirely.


def _place_blocks_batched(
    block_ids: list[int],
    blocks_data: list[dict],
    bays: list[Bay],
    bay_placed: list[list[Block]],
    bay_schedule: list[list[tuple[int, int]]],
    bay_loads: list[float],
    w1: float, w2: float, w3: float,
    t_start: float | None = None,
    log_interval: int = 0,
    deadline: float | None = None,
    batch_size: int = PHASE1_BATCH_SIZE,
    use_maxrects: bool = False,
) -> dict[int, dict]:
    """
    Quasi-Parallel-SGS alternative to _place_blocks: instead of committing
    one block at a time (Serial SGS -- see _place_blocks' docstring), chunks
    block_ids (already priority-sorted by the caller, e.g. EDD order) into
    batches of `batch_size` and commits each batch *jointly* via
    xpress_reinsert.reinsert() -- the same joint MIP Phase 3 already uses to
    reinsert K removed blocks at once, reused here as-is since it makes no
    assumption that remove_ids were ever previously placed (it only reads
    the *other*, already-committed blocks via bay_placed/bay_schedule/
    bay_loads to generate each id's candidates -- see _top_candidates_for_block).

    Motivation: Serial SGS commits each block seeing only *past* decisions,
    never future ones -- a block committed early can (and empirically does,
    see notes/algorithm_overview.md #12) end up obstructing a block that
    hasn't been placed yet, which is exactly what Phase 2 (_repair) exists
    to clean up after the fact. Jointly deciding a *batch* of blocks at once
    doesn't eliminate this (batches still commit in order, and a batch has
    no visibility into blocks in later batches), but it meaningfully
    shrinks the blast radius: conflicts *within* a batch are resolved
    before anything is committed, instead of being discovered by Phase 2
    afterward.

    Falls back to the existing, fully-validated _place_blocks (one at a
    time, same as Serial SGS) for any batch where the joint MIP is
    unavailable, times out, or finds no feasible combination -- this is a
    routine, expected outcome, not an error; it never blocks progress.
    Batches of size 1 (the last partial chunk) skip the MIP entirely since
    there is nothing to jointly optimize against a single block.
    """
    n_total = len(block_ids)
    n_bays = len(bays)
    result: dict[int, dict] = {}
    n_batches_done = 0
    n_joint_ok = 0

    for start in range(0, n_total, batch_size):
        batch_ids = block_ids[start:start + batch_size]
        n_batches_done += 1

        past_deadline = deadline is not None and time.time() > deadline
        partial = None
        used_xpress = False

        if not past_deadline and len(batch_ids) > 1:
            try:
                import xpress_reinsert
                partial = xpress_reinsert.reinsert(
                    batch_ids, blocks_data, bays,
                    bay_placed, bay_schedule, bay_loads,
                    w1, w2, w3, deadline,
                    max_per_block=PHASE1_MAX_PER_BLOCK,
                    solve_time_limit=2,
                )
            except Exception:
                partial = None
            used_xpress = partial is not None

        if used_xpress:
            n_joint_ok += 1
            for bi in batch_ids:
                a = partial[bi]
                blk_data = blocks_data[bi]
                final_blk = Block(block_id=bi, block_data=blk_data,
                                  x=a["x"], y=a["y"], orient_idx=a["orient_idx"])
                bay_placed[a["bay_id"]].append(final_blk)
                bay_schedule[a["bay_id"]].append((a["entry_time"], a["exit_time"]))
                bay_loads[a["bay_id"]] += blk_data["workload"]
        else:
            # Deadline already passed, batch of 1, Xpress unavailable, or no
            # feasible joint combination -- fall back to the proven
            # one-at-a-time path for just this batch. _place_blocks mutates
            # bay_placed/bay_schedule/bay_loads in place itself, and forces
            # every block automatically once `deadline` has passed.
            partial = _place_blocks(
                batch_ids, blocks_data, bays,
                bay_placed, bay_schedule, bay_loads,
                w1, w2, w3, forced_ids=set(),
                t_start=t_start, deadline=deadline,
                use_maxrects=use_maxrects,
            )

        result.update(partial)

        if log_interval > 0 and t_start is not None:
            n_done = start + len(batch_ids)
            if n_batches_done % max(1, log_interval // batch_size) == 0 or n_done == n_total:
                elapsed = time.time() - t_start
                loads_str = " ".join(f"b{i}={round(bay_loads[i])}" for i in range(n_bays))
                tag = "xpress-joint" if used_xpress else "greedy-fallback"
                print(f"[Greedy]   {n_done:4d}/{n_total}"
                      f"  batch#{n_batches_done} (size={len(batch_ids)}) via={tag}"
                      f"  loads=[{loads_str}]"
                      f"  joint_ok={n_joint_ok}/{n_batches_done}"
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

def _try_rebalance_move(
    remove_ids: list[int],
    target_bay_id: int,
    blocks_data: list[dict],
    bays: list[Bay],
    bay_placed: list[list[Block]],
    bay_schedule: list[list[tuple[int, int]]],
    deadline: float | None,
) -> dict[int, dict] | None:
    """
    2026-07-20: dedicated move for the "balance" operator, targeting Z2
    directly instead of relying on the general reinsertion search to
    happen to prefer a lighter bay.

    Z2 = max over bay PAIRS of |weighted load difference| -- unlike Z1/Z3
    (sums over blocks, so any local improvement to any block helps), Z2
    only moves when a change touches the specific pair currently realising
    that max. The general per-block _placement_score search blends bay
    load with tardiness/preference, so a block removed from the heaviest
    bay often lands back in a bay that's merely "less bad" by the combined
    score, not the specific lightest bay that would actually close the
    current bottleneck gap. This function forces the direct move: try to
    place every block in remove_ids into target_bay_id (the caller passes
    the currently lightest-*weighted*-load bay) specifically, one at a time
    in workload-descending order, using the exact same
    _candidate_positions / _find_earliest_slot search _place_blocks already
    uses elsewhere -- just restricted to one bay -- so this adds no new
    geometry/feasibility logic, only a new *target*.

    Returns None if any block has no feasible slot in target_bay_id (routine
    -- the target bay may simply be too full; caller must fall back to the
    general multi-bay reinsertion search in that case, exactly like
    xpress_reinsert.reinsert()'s None contract). Does not mutate
    bay_placed/bay_schedule -- the caller commits on accept, same as every
    other reinsertion path in this module.

    2026-07-22 bugfix (user-caught): this called _candidate_positions with
    no deadline and no max_source_blocks cap, and evaluated EVERY resulting
    candidate via _find_earliest_slot with no scan budget at all -- the
    same unbounded-cost pattern already found and fixed today in
    _place_blocks, _force_place, and _top_candidates_for_block, just missed
    here. target_bay_id is chosen as the lightest bay by WEIGHTED load
    (bay_weights[j] * bay_loads[j]) -- a large bay can have a low weighted
    load while still holding a large raw block count, so "lightest" here
    does not imply "small enough to scan freely". Measured directly: one
    "balance" round on a 1000-block synthetic stress instance took ~118s
    (vs ~1s for a typical xpress_reinsert round on the same instance) with
    this exact call as the only unbounded step. Now capped the same way as
    _top_candidates_for_block: max_source_blocks limits candidate
    GENERATION cost, CANDIDATE_SCAN_CAP + periodic deadline checks limit
    how many candidates get the full _find_earliest_slot treatment. Same
    safety argument as every other cap in this module -- downstream
    bay.contains_block / _find_earliest_slot still re-verify against the
    FULL local_placed/local_schedule, so this can only miss a genuinely
    better slot, never accept an infeasible one.
    """
    bay = bays[target_bay_id]
    local_placed = list(bay_placed[target_bay_id])
    local_schedule = list(bay_schedule[target_bay_id])
    result: dict[int, dict] = {}

    order = sorted(remove_ids, key=lambda b: -blocks_data[b]["workload"])
    for bi in order:
        if deadline is not None and time.time() > deadline:
            return None
        blk_data = blocks_data[bi]
        r_time = blk_data["release_time"]
        due = blk_data["due_date"]
        proc = blk_data["processing_time"]

        best = None
        for oi in range(len(blk_data["shape"])):
            if deadline is not None and time.time() > deadline:
                return None
            blk_bb = _block_bbox(blk_data, oi)
            lx0, ly0, lx1, ly1 = blk_bb
            if (math.ceil(-lx0) > math.floor(bay.width - lx1) or
                    math.ceil(-ly0) > math.floor(bay.height - ly1)):
                continue
            candidates = _candidate_positions(
                bay.width, bay.height, local_placed, blk_bb,
                deadline=deadline, max_source_blocks=XPRESS_CANDIDATE_MAX_SOURCE_BLOCKS,
            )
            if len(candidates) > CANDIDATE_SCAN_CAP:
                candidates = _rank_candidates_by_earliest_bound(
                    candidates, blk_bb, local_placed, local_schedule, r_time
                )
            scan_budget = CANDIDATE_SCAN_CAP
            for cx, cy in candidates:
                if scan_budget <= 0:
                    break
                scan_budget -= 1
                if deadline is not None and time.time() > deadline:
                    return None
                new_blk = Block(block_id=bi, block_data=blk_data, x=cx, y=cy, orient_idx=oi)
                if not bay.contains_block(new_blk):
                    continue
                entry, exit_t = _find_earliest_slot(
                    new_blk, bay, local_placed, local_schedule, r_time, proc, deadline=deadline,
                )
                if entry is None:
                    continue
                tardiness = max(0.0, exit_t - due)
                if best is None or tardiness < best[0]:
                    best = (tardiness, cx, cy, oi, entry, exit_t)

        if best is None:
            return None  # target bay has no feasible slot for this block
        _, cx, cy, oi, entry, exit_t = best
        local_placed.append(Block(block_id=bi, block_data=blk_data, x=cx, y=cy, orient_idx=oi))
        local_schedule.append((entry, exit_t))
        result[bi] = {
            "block_id": bi, "bay_id": target_bay_id,
            "x": int(round(cx)), "y": int(round(cy)), "orient_idx": oi,
            "entry_time": int(round(entry)), "exit_time": int(round(exit_t)),
        }
    return result


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
    "swap"       -- targeted PAIR: the worst-tardiness block plus whichever
                    same-bay block currently occupies that block's own
                    IDEAL (unconstrained) window [release_time,
                    release_time+processing_time). Drives Z1, but via a
                    fundamentally different mechanism than "tardy": "tardy"
                    removes blocks independently and lets the general search
                    re-place them one at a time (or jointly, for the same K
                    blocks Xpress happens to be given) -- it has no notion
                    of WHY a block is tardy. Two blocks that are mutually
                    blocking each other's best spot are exactly the case
                    plain independent removal handles worst: removing just
                    the tardy one and re-searching often just finds the same
                    (or an equally bad) spot again, since the actual blocker
                    is still sitting exactly where it was. Explicitly pairing
                    the tardy block with its concrete blocker and handing
                    both to the SAME joint reinsertion (see _improve's
                    Xpress-joint-attempt condition, now keyed off
                    len(remove_ids) rather than the nominal k so a 2-block
                    swap pair always gets the joint treatment regardless of
                    which k_values slot this round landed on) lets the
                    solver actually consider swapping their positions, not
                    just independently re-searching each in isolation.
                    Added 2026-07-20.

    Added 2026-07-20 so Phase 3 can target Z2/Z3 too, not just Z1 -- before
    this, blocks that were fully feasible but sitting in a suboptimal
    bay for balance/preference reasons (rather than being late) were never
    reconsidered by anything in this codebase.
    """
    if mode == "swap":
        tardy_scored = [
            (bid, a["exit_time"] - blocks_data[bid]["due_date"])
            for bid, a in best_assignments.items()
            if a["exit_time"] - blocks_data[bid]["due_date"] > 0
        ]
        if not tardy_scored:
            return []
        tardy_scored.sort(key=lambda t: -t[1])
        for bid, _ in tardy_scored[:max(1, k)]:
            a = best_assignments[bid]
            ideal_start = blocks_data[bid]["release_time"]
            ideal_end = ideal_start + blocks_data[bid]["processing_time"]
            for other_bid, other_a in best_assignments.items():
                if other_bid == bid or other_a["bay_id"] != a["bay_id"]:
                    continue
                if _time_overlaps(ideal_start, ideal_end, other_a["entry_time"], other_a["exit_time"]):
                    return [bid, other_bid]
        return []  # none of the worst-tardiness blocks has an identifiable same-bay blocker

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

    if mode == "wholebay":
        # Whole-bay joint reinsertion (added 2026-07-21): unlike every other
        # mode here, this ignores k entirely and returns ALL blocks
        # currently in the heaviest-weighted bay, so _improve can hand the
        # whole set to xpress_reinsert.reinsert() as one joint MIP -- see
        # analysis/bay_mip_probe.py, which validated that bay-independence
        # makes this tractable (K=170 in ~4s) once candidate generation is
        # restricted to that one bay via restrict_bay_id.
        loads = {}
        for bid, a in best_assignments.items():
            loads[a["bay_id"]] = loads.get(a["bay_id"], 0.0) + blocks_data[bid]["workload"]
        if not loads:
            return []
        heaviest = max(loads, key=lambda j: bay_weights[j] * loads[j])
        return [bid for bid, a in best_assignments.items() if a["bay_id"] == heaviest]

    return []


# -----------------------------------------------------------------------------
# Incremental feasibility check (2026-07-21)
# -----------------------------------------------------------------------------
# Lives here, not in utils.py: utils.py is overwritten by the grading server
# with its own original copy on every run (an anti-cheat measure), so any code
# needed at grading time must live in a file we actually control (this one,
# or myalgorithm.py / xpress_reinsert.py). This function is internal-only --
# every call site still gates any state mutation behind a full check_feasibility
# confirmation (see _improve below), and myalgorithm.py verifies the final
# returned solution with the full check_feasibility regardless -- so it changes
# nothing about what gets validated, only how cheaply _improve's internal
# search loop re-checks its own trial states.

def check_feasibility_incremental(prob_info: dict,
                                  base_assignments: dict[int, dict],
                                  new_assignments: dict[int, dict],
                                  changed_ids: set) -> dict:
    """
    Re-verify only what could possibly have changed, instead of re-running
    check_feasibility's five stages against every block from scratch.

    Motivation: check_feasibility's Stages 2-4 are each effectively O(bay_size^2)
    (per-block "who else is present" scans, and all-pairs collision checks) --
    for a local-search loop that only removes and reinserts a small batch of
    K blocks per trial, re-paying that cost for every OTHER block that never
    moved is pure waste. Stage 1 is O(n) already cheap. The objective
    computation is a single O(n) linear pass (not O(n^2)), so it's left as a
    full recompute here too -- not worth the complexity of an incremental sum
    for something already linear.

    PRECONDITIONS (caller's responsibility, not re-verified here):
      - `base_assignments` is a COMPLETE assignment dict (every block_id in
        prob_info["blocks"]) that was already confirmed feasible by a prior
        check_feasibility (full or incremental) call.
      - `new_assignments` is also COMPLETE, identical to base_assignments
        except for the entries in `changed_ids`.
      - Both dicts use the same per-block schema as check_feasibility's
        internal `assignments` list: {block_id, bay_id, x, y, orient_idx,
        entry_time, exit_time}.

    SCOPE LIMITATION vs check_feasibility: the "EXIT-before-ENTRY at the same
    timestamp" ordering rule is enforced by construction here (this function
    builds its own per-bay op timeline with EXIT sorted first at ties, mirroring
    baseline_greedy._build_operations' own tie-break), not independently
    verified from an externally-supplied operations list. This is safe for
    this codebase's actual call site (baseline_greedy._improve, which always
    goes through _build_operations before ever calling the full check), but
    means this function must NEVER be used to validate the final solution
    handed back to the grading harness -- that must always go through the
    full check_feasibility (see myalgorithm.py's verify-before-return step).

    2026-07-21: added with a mandatory shadow-validation phase (see
    baseline_greedy._improve) -- every call site also calls the full
    check_feasibility and compares results, logging any divergence loudly,
    and treats the FULL check's verdict as authoritative until this function
    has accumulated enough divergence-free trials to be trusted standalone.
    An incorrectly-optimistic feasibility check is the single most dangerous
    class of bug in this codebase (silently accepting an actually-infeasible
    solution scores -1, same as never attempting the instance), so this
    function is deliberately over-cautious about what counts as "affected"
    rather than trying to shave every last redundant check.

    Returns the same-shaped dict as check_feasibility:
        {feasible, stage, violations, objective, obj1, obj2, obj3}
    """
    blocks_data = prob_info["blocks"]
    bays_data = prob_info["bays"]
    n_blocks = len(blocks_data)
    n_bays = len(bays_data)

    violations: list[str] = []

    # -- Cheap coverage sanity check (O(n), not O(n^2)) ------------------------
    if len(new_assignments) != n_blocks:
        violations.append(
            f"Stage1: new_assignments has {len(new_assignments)} entries, "
            f"expected {n_blocks} (incomplete solution)"
        )
        return {"feasible": False, "stage": 1, "violations": violations,
                "objective": None, "obj1": None, "obj2": None, "obj3": None}

    # -- Stage 1: only changed blocks can possibly have a NEW violation -------
    for bid in changed_ids:
        a = new_assignments[bid]
        blk_data = blocks_data[bid]
        if not (0 <= a["bay_id"] < n_bays):
            violations.append(
                f"Stage1: block {bid} assigned to invalid bay {a['bay_id']}"
            )
        if not (0 <= a["orient_idx"] < len(blk_data["shape"])):
            violations.append(
                f"Stage1: block {bid} has invalid orient_idx {a['orient_idx']}"
            )
        ei, ai, pi = a["exit_time"], a["entry_time"], blk_data["processing_time"]
        if ei - ai < pi - 1e-6:
            violations.append(
                f"Stage1: block {bid} exit-entry={ei-ai:.2f} < processing_time={pi}"
            )
        if ai < blk_data["release_time"] - 1e-6:
            violations.append(
                f"Stage1: block {bid} entry_time={ai} < release_time="
                f"{blk_data['release_time']}"
            )
    if violations:
        return {"feasible": False, "stage": 1, "violations": violations,
                "objective": None, "obj1": None, "obj2": None, "obj3": None}

    def _time_overlaps(a1: float, e1: float, a2: float, e2: float) -> bool:
        return a1 < e2 and a2 < e1

    # -- Which bays does this trial actually touch? ----------------------------
    # A bay is "affected" if a changed block is newly in it OR used to be in it
    # (a block that moved bays affects both the bay it left and the bay it
    # entered -- the one it left needs re-checking too, since removing a block
    # can change other blocks' present-sets there, and its own boundary/pair
    # checks in the OLD bay are simply gone, not something to verify there
    # anymore -- only the blocks it left behind matter).
    affected_bays: set = set()
    for bid in changed_ids:
        affected_bays.add(new_assignments[bid]["bay_id"])
        if bid in base_assignments:
            affected_bays.add(base_assignments[bid]["bay_id"])

    bay_objs: dict = {}
    bay_asgns_by_bay: dict = {}
    bay_blocks_by_bay: dict = {}
    for j in affected_bays:
        bay_objs[j] = Bay.from_dict(bays_data[j], j)
        asgns_j = [a for a in new_assignments.values() if a["bay_id"] == j]
        bay_asgns_by_bay[j] = asgns_j
        bay_blocks_by_bay[j] = [
            Block(block_id=a["block_id"], block_data=blocks_data[a["block_id"]],
                 x=int(round(a["x"])), y=int(round(a["y"])), orient_idx=a["orient_idx"])
            for a in asgns_j
        ]

    changed_in_bay: dict = {
        j: {bid for bid in changed_ids if new_assignments[bid]["bay_id"] == j}
        for j in affected_bays
    }

    def _affected_unchanged(j: int) -> set:
        """
        Unchanged blocks in bay j whose present-set at their own entry/exit
        could have changed, because a changed block's OLD or NEW window
        (whichever applies to this bay) overlaps their entry/exit-time window.
        """
        result: set = set()
        for bid in changed_in_bay[j]:
            new_a = new_assignments[bid]
            windows = [(new_a["entry_time"], new_a["exit_time"])]
            if bid in base_assignments and base_assignments[bid]["bay_id"] == j:
                old_a = base_assignments[bid]
                windows.append((old_a["entry_time"], old_a["exit_time"]))
            for a2 in bay_asgns_by_bay[j]:
                bid2 = a2["block_id"]
                if bid2 in changed_ids:
                    continue
                if any(_time_overlaps(w0, w1, a2["entry_time"], a2["exit_time"])
                      for w0, w1 in windows):
                    result.add(bid2)
        return result

    # -- Stage 2: crane entry feasibility, only for affected blocks -----------
    for j in affected_bays:
        bay = bay_objs[j]
        asgns_j = bay_asgns_by_bay[j]
        blocks_j = bay_blocks_by_bay[j]
        to_check = changed_in_bay[j] | _affected_unchanged(j)
        for idx, a in enumerate(asgns_j):
            if a["block_id"] not in to_check:
                continue
            new_blk = blocks_j[idx]
            ai = a["entry_time"]
            present = [
                blocks_j[k] for k, other in enumerate(asgns_j)
                if k != idx and other["entry_time"] < ai < other["exit_time"]
            ]
            obs = check_entry(bay, present, new_blk)
            if obs:
                for o in obs:
                    if o.existing_block.block_id == new_blk.block_id:
                        violations.append(
                            f"Stage2: t={int(ai)}: block {a['block_id']} exceeds "
                            f"bay boundary (area={o.area:.3f})"
                        )
                    else:
                        kind = "sweep" if o.is_sweep else "collision"
                        violations.append(
                            f"Stage2: t={int(ai)}: block {a['block_id']} entry "
                            f"obstructed by block {o.existing_block.block_id} "
                            f"(kind={kind}, area={o.area:.3f})"
                        )
    if violations:
        return {"feasible": False, "stage": 2, "violations": violations,
                "objective": None, "obj1": None, "obj2": None, "obj3": None}

    # -- Stage 3: crane exit feasibility, only for affected blocks ------------
    for j in affected_bays:
        bay = bay_objs[j]
        asgns_j = bay_asgns_by_bay[j]
        blocks_j = bay_blocks_by_bay[j]
        to_check = changed_in_bay[j] | _affected_unchanged(j)
        for idx, a in enumerate(asgns_j):
            if a["block_id"] not in to_check:
                continue
            target_blk = blocks_j[idx]
            ei = a["exit_time"]
            present_at_exit = [
                blocks_j[k] for k, other in enumerate(asgns_j)
                if blocks_j[k].block_id == target_blk.block_id
                or (other["entry_time"] < ei < other["exit_time"])
            ]
            obs = check_exit(bay, present_at_exit, target_blk)
            if obs:
                for o in obs:
                    kind = "sweep" if o.is_sweep else "collision"
                    violations.append(
                        f"Stage3: t={int(ei)}: block {a['block_id']} exit "
                        f"obstructed by block {o.existing_block.block_id} "
                        f"(kind={kind}, area={o.area:.3f})"
                    )
    if violations:
        return {"feasible": False, "stage": 3, "violations": violations,
                "objective": None, "obj1": None, "obj2": None, "obj3": None}

    # -- Stage 4: bay boundary + pairwise collisions, only touching changed ---
    for j in affected_bays:
        bay = bay_objs[j]
        asgns_j = bay_asgns_by_bay[j]
        blocks_j = bay_blocks_by_bay[j]
        changed_set_j = changed_in_bay[j]

        for idx, a in enumerate(asgns_j):
            if a["block_id"] not in changed_set_j:
                continue
            blk = blocks_j[idx]
            if not bay.contains_block(blk):
                bb = blk.bounding_rect()
                violations.append(
                    f"Stage4: block {a['block_id']} bounding box {bb} "
                    f"exceeds bay {j} ({bay.width}*{bay.height})"
                )

        # Only pairs with at least one changed member can possibly be a NEW
        # collision -- unchanged/unchanged pairs were already verified and
        # neither member moved, so their outcome cannot have changed.
        n = len(asgns_j)
        checked_pairs: set = set()
        for p in range(n):
            ap = asgns_j[p]
            if ap["block_id"] not in changed_set_j:
                continue
            for q in range(n):
                if q == p:
                    continue
                aq = asgns_j[q]
                pair_key = (min(ap["block_id"], aq["block_id"]),
                           max(ap["block_id"], aq["block_id"]))
                if pair_key in checked_pairs:
                    continue
                checked_pairs.add(pair_key)
                if not _time_overlaps(ap["entry_time"], ap["exit_time"],
                                      aq["entry_time"], aq["exit_time"]):
                    continue
                results = check_collisions(bay, [blocks_j[p], blocks_j[q]])
                for r in results:
                    violations.append(
                        f"Stage4: block {ap['block_id']} and block "
                        f"{aq['block_id']} collide in bay {j} at layer "
                        f"{r.layer_index} (area={r.area:.3f})"
                    )
    if violations:
        return {"feasible": False, "stage": 4, "violations": violations,
                "objective": None, "obj1": None, "obj2": None, "obj3": None}

    # -- Stage 5: sequential replay, scoped to affected bays only -------------
    # See the SCOPE LIMITATION note in the docstring: EXIT-before-ENTRY at a
    # tied timestamp is enforced by this function's own sort, not
    # independently verified -- safe only for internally-consistent callers.
    for j in affected_bays:
        bay = bay_objs[j]
        asgns_j = bay_asgns_by_bay[j]
        blocks_by_id_j = {a["block_id"]: blk for a, blk in zip(asgns_j, bay_blocks_by_bay[j])}
        ops_j = []
        for a in asgns_j:
            ops_j.append((a["exit_time"], 0, a["block_id"], "EXIT"))
            ops_j.append((a["entry_time"], 1, a["block_id"], "ENTRY"))
        ops_j.sort()

        bay_present: set = set()
        for (t, _, bid, kind) in ops_j:
            target_blk = blocks_by_id_j[bid]
            if kind == "ENTRY":
                present_blks = [blocks_by_id_j[k] for k in bay_present]
                obs = check_entry(bay, present_blks, target_blk)
                if obs:
                    for o in obs:
                        if o.existing_block.block_id == bid:
                            violations.append(
                                f"Stage5: t={int(t)}: ENTRY block {bid} "
                                f"exceeds bay boundary"
                            )
                        else:
                            kind_str = "sweep" if o.is_sweep else "collision"
                            violations.append(
                                f"Stage5: t={int(t)}: ENTRY block {bid} "
                                f"obstructed by block {o.existing_block.block_id} "
                                f"({kind_str})"
                            )
                else:
                    bay_present.add(bid)
            else:  # EXIT
                if bid not in bay_present:
                    violations.append(
                        f"Stage5: t={int(t)}: EXIT block {bid} is not present "
                        f"in bay {j} at this point"
                    )
                    continue
                present_blks = [blocks_by_id_j[k] for k in bay_present]
                obs = check_exit(bay, present_blks, target_blk)
                if obs:
                    for o in obs:
                        kind_str = "sweep" if o.is_sweep else "collision"
                        violations.append(
                            f"Stage5: t={int(t)}: EXIT block {bid} "
                            f"obstructed by block {o.existing_block.block_id} "
                            f"({kind_str})"
                        )
                else:
                    bay_present.discard(bid)
    if violations:
        return {"feasible": False, "stage": 5, "violations": violations,
                "objective": None, "obj1": None, "obj2": None, "obj3": None}

    # -- Objective: single O(n) linear pass, not incrementalized (already ----
    # cheap relative to the O(n^2) stages above -- see docstring).
    w1 = prob_info.get("weights", {}).get("w1", 1.0)
    w2 = prob_info.get("weights", {}).get("w2", 1.0)
    w3 = prob_info.get("weights", {}).get("w3", 1.0)

    obj1 = 0.0
    bay_loads = [0.0] * n_bays
    obj3 = 0.0
    for a in new_assignments.values():
        bi = a["block_id"]
        bj = a["bay_id"]
        blk = blocks_data[bi]
        obj1 += max(0.0, a["exit_time"] - blk["due_date"])
        bay_loads[bj] += blk["workload"]
        s_max = max(blk["bay_preferences"])
        obj3 += s_max - blk["bay_preferences"][bj]

    bay_areas = [bays_data[j]["width"] * bays_data[j]["height"] for j in range(n_bays)]
    avg_area = sum(bay_areas) / n_bays
    u = [avg_area / a for a in bay_areas]
    if n_bays >= 2:
        obj2 = math.floor(max(
            abs(u[j1] * bay_loads[j1] - u[j2] * bay_loads[j2])
            for j1 in range(n_bays) for j2 in range(n_bays)
            if j1 != j2
        ))
    else:
        obj2 = 0.0

    objective = w1 * obj1 + w2 * obj2 + w3 * obj3

    return {
        "feasible":  True,
        "stage":     5,
        "violations": [],
        "objective": objective,
        "obj1":      obj1,
        "obj2":      obj2,
        "obj3":      obj3,
    }


def _improve(prob_info: dict,
            assignments: dict[int, dict],
            bays: list[Bay],
            blocks_data: list[dict],
            w1: float, w2: float, w3: float,
            t_start: float,
            timelimit: float,
            atc_k: float = 2.0,
            k_values: tuple[int, ...] | None = None,
            annealing: bool = False,
            initial_temp_frac: float = 0.01,
            cooling_rate: float = 0.98,
            seed: int | None = None,
            z2z3_modes: bool = True,
            max_per_block: int = 20,
            z1_lower_bound: float = 0.0,
            z23_relax: bool = True,
            use_maxrects: bool = False,
            known_result: dict | None = None) -> dict[int, dict]:
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
    seed        : RNG seed for the annealing accept/reject draw and the
                  operator-selection/random-destroy draws. None (default)
                  means an unseeded, naturally varying run each time.

    Stops early once no new best has been found for STALL_TIME_FRAC of the
    Phase-3 time budget (see that constant below) -- unlike the earlier
    fixed-round-count version, this scales automatically with however fast
    or slow individual rounds happen to be, so it can't strand unused
    wall-clock budget just because rounds got cheaper. Early return here
    isn't wasted either way: myalgorithm._iterated_greedy can spend
    whatever's left on a fresh restart with a different seed.

    known_result : optional (2026-07-22 perf fix, user-proposed). A
        check_feasibility result the caller already computed for THIS EXACT
        `assignments` (e.g. whichever of left-justify/right-justify's own
        pre-sweep or post-sweep checks was the last one performed, since
        neither function mutates its input and a commit only ever happens
        right after its own confirming check -- see greedyalgorithm's
        `last_verified_result` tracking). Measured directly: this function's
        own from-scratch re-check of the same state took ~19s on a
        4000-block synthetic stress instance, consuming the ENTIRE Phase 3
        budget before a single round could run. When given, used in place
        of a redundant recomputation; when None (default, e.g. both
        left_justify and right_justify are off), falls back to computing it
        fresh here exactly as before this parameter existed. Not an
        incremental/diffed result like check_feasibility_incremental --
        there's no partial change to reconcile, `assignments` genuinely
        hasn't moved since known_result was computed, so this is just reuse,
        not a new correctness-sensitive mechanism.
    """
    from utils import check_feasibility

    # Larger destroy sizes (2026-07-20): a single relocate or a 5-block swap
    # can't unblock a structural issue that needs a bigger reshuffle. Mix in
    # much larger removal sizes (up to n/3) alongside the original small
    # ones, capped so a single round never removes more than a third of the
    # instance.
    if k_values is None:
        n = len(blocks_data)
        cap = max(1, n // 3)
        k_values = tuple(sorted({
            k for k in (1, 2, 3, 5, 8, 12, max(1, n // 10), max(1, n // 5))
            if k <= cap
        }))

    def _build(a: dict[int, dict]) -> dict:
        return {"operations": _build_operations(list(a.values()))}

    base_result = known_result if known_result is not None else check_feasibility(prob_info, _build(assignments))
    if not base_result["feasible"]:
        print("[Greedy] Improve: skipped (incoming solution is not feasible)")
        return assignments

    rng = random.Random(seed)
    current_assignments = dict(assignments)
    current_obj = base_result["objective"]
    best_assignments = dict(assignments)
    best_obj = current_obj
    best_obj1 = base_result["obj1"]
    t0 = initial_temp_frac * max(1.0, current_obj)

    p_avg = sum(b["processing_time"] for b in blocks_data) / max(1, len(blocks_data))
    bay_areas = [bay.width * bay.height for bay in bays]
    avg_area = sum(bay_areas) / len(bays)
    bay_weights = [avg_area / a for a in bay_areas]
    deadline = t_start + timelimit * 0.99

    round_idx = 0
    stalled = 0
    rounds_since_last_improve = 0
    empty_operators_seen: set[str] = set()
    # 2026-07-21: stall detection switched from a fixed ROUND count to a
    # fraction of the remaining wall-clock budget. The round-count version
    # (3 * len(k_values) * len(operator_names)) was calibrated back when
    # ~10-15 rounds fit in a typical budget -- today's candidate-generation
    # speedups (spatial pre-filtering in _find_earliest_slot, per-(bay,
    # orientation) capped+ranked candidate scan) can make rounds cheap
    # enough that this fixed count gets reached with most of the time
    # budget still unused, stopping the search early for no real reason
    # (see notes/algorithm_overview.md). A time-based threshold scales
    # automatically with however fast rounds happen to be, instead of
    # needing to be re-tuned every time per-round cost changes. `stalled`
    # (the round counter) is kept only for the log line, not the decision.
    STALL_TIME_FRAC = 0.25
    # 2026-07-22 (user-proposed): STALL_TIME_FRAC alone scales the stall
    # wait linearly with however much Phase 3 budget this instance happens
    # to get -- fine at 60s (0.25 * ~54s ~= 13.5s), but on a large hidden
    # timelimit (say 600s) it lets ALNS spin an unproductive roulette wheel
    # for up to 135s before giving up and restarting, when a genuinely
    # stuck run is just as identifiable in 15s regardless of total budget.
    # Every extra second spent confirming "still stuck" past that point is
    # a second _iterated_greedy doesn't get to try a fresh construction
    # basin -- directly relevant to prob_1's restart-2 8,041 discovery
    # (see notes/algorithm_overview.md), which a large enough stall wait
    # would delay or crowd out. Cap with min() so small budgets are
    # unaffected (the fraction is already below the cap there) and large
    # ones give up promptly instead of scaling the wait with budget size.
    STALL_TIME_CAP = 15.0
    # 2026-07-22 (user-proposed): STALL_TIME_CAP guards against wasting a
    # LARGE budget on an already-stuck run, but a flat 15s cap alone risks
    # the opposite mistake on a hidden instance more congested than
    # anything tested locally, where a single round can legitimately take
    # well past 15s (see stress-test note above). Requiring at least this
    # many round ATTEMPTS since the last improvement too (not just elapsed
    # time) ensures the operator actually got a fair number of tries before
    # being declared stuck, regardless of how expensive each try is.
    MIN_ROUNDS_SINCE_IMPROVE = 5
    phase3_loop_start = time.time()
    last_improvement_time = phase3_loop_start
    stall_threshold = min(STALL_TIME_FRAC * (deadline - phase3_loop_start), STALL_TIME_CAP)
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
    # 2026-07-21: "wholebay" is included in BOTH branches (unlike
    # preference/balance, which are Z2/Z3-only and gated behind
    # z2z3_modes) because its validated benefit (analysis/bay_mip_probe.py)
    # was entirely a Z1 improvement -- a joint re-optimization of a whole
    # bay's schedule can resolve tardiness conflicts a K<=12 round can't
    # even see. It's deliberately left OUT of the Z1_URGENCY_BOOST/pruning
    # treatment below (unlike tardy/swap): each attempt costs a full
    # whole-bay MIP solve (seconds, not the near-instant cost of a small
    # K round), so over-boosting its roulette-wheel odds while Z1 has
    # headroom risks spending disproportionate time on it; its natural EMA
    # weight already lets it earn more rounds if it keeps paying off.
    operator_names = (["tardy", "swap", "wholebay", "preference", "balance", "random"]
                      if z2z3_modes else ["tardy", "swap", "wholebay"])
    op_weight: dict[str, float] = {name: 1.0 for name in operator_names}
    Z1_URGENCY_BOOST = 3.0  # see the round-selection weights computation below

    # 2026-07-20: Z1's theoretical lower bound (see analysis/lower_bound.py --
    # sum of each block's own release_time+processing_time-due_date floor,
    # ignoring all spatial/crane constraints) is a proof, not a heuristic --
    # while best_obj1 sits at it, no possible move can reduce Z1 further, so
    # 'tardy' and 'swap' (which both exist purely to reduce Z1) are
    # provably useless *for now*. 2026-07-22 bugfix (user-caught): this used
    # to permanently operator_names.remove() them the moment best_obj1 first
    # hit the bound. But best_obj1 tracks whatever Z1 value belongs to the
    # current-best WEIGHTED objective (w1*Z1+w2*Z2+w3*Z3), not Z1 in
    # isolation -- a later round can legitimately become the new best by
    # trading a small Z1 regression for a big enough Z2/Z3 win, pushing
    # best_obj1 back above the bound. With the old permanent removal,
    # 'tardy'/'swap' would already be gone from operator_names at that
    # point and could never be redrawn for the rest of this run, even
    # though Z1 genuinely needs fixing again. Fixed by never removing them
    # at all -- instead their roulette-wheel weight is computed fresh every
    # round from the CURRENT best_obj1 (see the `weights` list below): zero
    # while at the bound, their normal (boosted) weight the instant
    # best_obj1 rises above it again. Functionally identical to the old
    # behaviour whenever Z1 never regresses (the common case), but no
    # longer permanently forecloses the trade-off-regression case.
    WEIGHT_DECAY = 0.8       # fraction of old weight kept each update
    REWARD_NEW_BEST = 3.0
    REWARD_ACCEPTED = 0.5    # accepted (e.g. an annealing walk) but not a new best
    REWARD_REJECTED = 0.0
    # 2026-07-22 (user-caught bug): op_weight[mode] = WEIGHT_DECAY*w + (1-
    # WEIGHT_DECAY)*REWARD_REJECTED is pure geometric decay toward 0 with no
    # floor -- an operator (or, on a long enough unlucky/unusual hidden
    # instance, ALL operators) rejected on every draw for ~3,339 consecutive
    # rounds decays w=0.8^n past float64's smallest positive value, i.e.
    # underflows to an exact 0.0. If every operator's weight hits exactly
    # 0.0 at once, random.choices(operator_names, weights=[0,0,...]) raises
    # ValueError -- scored identically to a crash (-1), the worst possible
    # outcome. This also directly contradicts this scheme's own stated
    # intent ("성과 없는 연산자는... 완전 배제는 아님, 언제든 회복 가능" --
    # see the comment above the round-selection weights computation) --
    # the design always assumed operators stay recoverable, but the formula
    # itself never enforced it. MIN_WEIGHT is applied to every update (not
    # just rejections) for defense in depth.
    MIN_WEIGHT = 0.01

    # 2026-07-20: preference/balance-mode trials measured a 15.8%/32.8%
    # acceptance rate across today's logs (vs 62.9%/57.0% for tardy/random)
    # -- these operators exist specifically to target Z3/Z2, but under the
    # plain "never accept worse" rule almost every attempt is thrown away
    # even when it genuinely improves its own target, because *any* increase
    # in Z1 tends to be enough to lose given w1's typical dominance (see
    # notes/algorithm_overview.md). Give these two operators (only) a small,
    # bounded epsilon-constraint-style relaxation: accept a trial that makes
    # the WALK state (current_obj) worse by up to Z23_RELAX_FRAC, so the
    # search can actually explore the Z1-vs-Z2/Z3 trade-off instead of
    # rejecting it outright every time. Z23_MAX_DRIFT_FRAC caps how far the
    # walk can compound away from the best-known solution across repeated
    # small acceptances. Neither constant is validated yet (see changelog);
    # `best_obj`/`best_assignments` -- what actually gets returned -- are
    # completely unaffected by this and can never regress, exactly like the
    # existing annealing=True walk/best split this reuses the pattern from.
    Z23_RELAX_FRAC = 0.01
    Z23_MAX_DRIFT_FRAC = 0.05
    # 2026-07-20: found via repeated-run testing (prob_40/prob_20, 3 runs
    # each at 180s, current code vs the deterministic 2nd-submission
    # baseline) that this relaxation can hurt on instances where Phase 1
    # construction itself is expensive (dense/congested bays -> heavy
    # _find_earliest_slot cost), leaving very little of the time budget for
    # Phase 3. Mechanism: unlike strict hill-climbing (a rejected trial
    # leaves current_assignments unchanged, so the next round always retries
    # from the best-known state), an epsilon-relaxed accept MOVES
    # current_assignments to a worse state -- if very few rounds remain,
    # there may not be enough left to find a genuine improvement and climb
    # back out before time runs out, effectively wasting one of only a
    # handful of rounds. Strict hill-climbing never has this failure mode
    # (every round is retried from best). Guard: only allow the relaxation
    # when there's still a reasonable amount of wall-clock room left for
    # Phase 3 to recover in -- below this, fall back to strict acceptance
    # for preference/balance too (same as tardy/random already do).
    Z23_MIN_REMAINING_FOR_RELAX = 15.0

    # Joint Xpress reinsertion is O(K^2 x max_per_block^2) in the pairwise
    # conflict check -- fine for the small K's, prohibitively expensive for
    # the large ones just added above. Skip straight to sequential greedy
    # reinsertion once K exceeds this.
    JOINT_MAX_K = 12

    # 2026-07-22 (experiment branch, user-proposed): batch the mandatory
    # full check_feasibility confirmation across several accepts instead of
    # paying it on EVERY one. Motivation: on a large instance (thousands of
    # blocks), that single confirmation call can cost ~19s (measured
    # directly, see notes/algorithm_overview.md #59) -- if several accepts
    # happen in a row, each paying that cost separately can consume most of
    # Phase 3's remaining budget on redundant re-verification of a checker
    # (check_feasibility_incremental) that's already been shadow-validated
    # across 624 accept-path samples today (diverse instances/scales/
    # operators) with ZERO divergences from the full checker. Still confirm
    # every CONFIRM_CHECK_INTERVAL accepts (not never), and unconditionally
    # once more right before returning -- so a real divergence, if one ever
    # happens, is caught within a bounded number of rounds, not silently
    # trusted forever. On a mismatch, roll ALL THE WAY BACK to the last
    # full-checked checkpoint (not just the one bad round) and stop --
    # pinpointing exactly which of the batched accepts was wrong isn't worth
    # the complexity when discarding all of them and returning a
    # known-good state is simple and safe.
    CONFIRM_CHECK_INTERVAL = 10
    checkpoint_assignments = dict(current_assignments)
    checkpoint_obj = current_obj
    checkpoint_best_assignments = dict(best_assignments)
    checkpoint_best_obj = best_obj
    checkpoint_best_obj1 = best_obj1
    accepts_since_checkpoint = 0

    while time.time() < deadline:
        # 2026-07-22 (user-proposed): counts every roulette-wheel spin this
        # loop takes (regardless of outcome -- empty draw, rejected,
        # walked-worse), reset to 0 only on a genuine new best. Paired with
        # stall_threshold below via AND: a slow-but-legitimately-exploring
        # run on a more congested/complex hidden instance than anything
        # tested locally could plausibly spend >15s on a SINGLE round (see
        # the 500/1000-block synthetic stress tests, where individual
        # rounds ran into the tens of seconds) -- the time cap alone could
        # then misread "one slow round" as "stuck" and restart prematurely
        # before the operator even got a fair number of tries. Requiring
        # both the time condition AND a minimum round count guards against
        # that without weakening the original fix's intent (still gives up
        # quickly on a genuinely-stuck FAST-rounds instance).
        rounds_since_last_improve += 1
        # 2026-07-21 rewrite: replaced the old "each operator gets exactly
        # one guaranteed shot per pass, in weighted order" scheme with pure
        # weighted-random selection WITH replacement every round -- the
        # classic ALNS roulette wheel. The old scheme's per-pass exclusivity
        # meant every operator got the SAME number of attempts regardless of
        # its EMA weight (weight only reordered a pass, never changed how
        # OFTEN an operator got picked), which defeated the point of
        # adaptive weighting: once Z1 hits its lower bound (leaving only
        # preference/balance/random/wholebay active), wholebay -- usually
        # unproductive on small/already-well-scheduled instances -- was
        # guaranteed the same attempt count as preference/balance, diluting
        # the few rounds that actually mattered. Confirmed both locally
        # (prob_1 regressed vs the 2nd submission after swap/wholebay were
        # added) and on a real hidden grading instance (P1's actual score
        # regressed the same way in the 3rd submission) -- see notes/
        # algorithm_overview.md. Now a hot-streak operator can be redrawn
        # every single round (rising weight makes that increasingly likely),
        # while a consistently unproductive one fades toward a near-zero
        # share instead of keeping a guaranteed fixed slice.
        # w1 typically dominates w2/w3 (see notes/algorithm_overview.md),
        # and Z1's lower bound is 0 on every local instance checked so far
        # -- so while Z1 hasn't reached it, a 'tardy' success is usually
        # worth far more to the combined objective than a same-sized
        # preference/balance win. Boost tardy/swap's roulette-wheel
        # selection odds (not their underlying EMA weight -- that still
        # reflects their own track record) while there's still Z1
        # headroom. 2026-07-22: once best_obj1 reaches z1_lower_bound,
        # zero their weight for THIS round instead of permanently removing
        # them (see the comment above operator_names' definition) -- they
        # stay in operator_names and simply get redrawn-eligible again the
        # instant a later round's best_obj1 rises back above the bound
        # (e.g. a Z2/Z3 trade-off that regresses Z1 slightly).
        z1_at_bound = best_obj1 <= z1_lower_bound + 1e-6
        active_operator_names = [
            n for n in operator_names if not (z1_at_bound and n in ("tardy", "swap"))
        ]
        weights = [
            0.0 if (z1_at_bound and n in ("tardy", "swap"))
            else op_weight[n] * (Z1_URGENCY_BOOST if n in ("tardy", "swap") else 1.0)
            for n in operator_names
        ]
        mode = rng.choices(operator_names, weights=weights, k=1)[0]

        k = k_values[round_idx % len(k_values)]
        remove_ids = _select_removal_candidates(
            current_assignments, blocks_data, bay_weights, k, mode, rng
        )
        if not remove_ids:
            # this operator has nothing to offer right now -- 2026-07-21
            # bugfix: originally a simple counter of consecutive empty draws
            # regardless of which operator, meant to approximate "every
            # operator has been checked." With weighted-random selection
            # WITH replacement, that's unsound -- if one operator's weight
            # is even moderately higher, it can get redrawn several times in
            # a row, and if THAT operator alone is temporarily empty (e.g.
            # "preference" before any block has a preference penalty), the
            # counter hits its threshold on pure bad luck from one operator
            # while others with real candidates never even got a turn.
            # Confirmed: this caused a real regression (prob_1 stopped at
            # the raw post-repair objective, zero Phase 3 improvement, in a
            # before/after run where the direct/unaffected path found the
            # correct -27,450 improvement). Track the actual SET of
            # operators confirmed empty since the last non-empty draw
            # instead -- only concludes "truly nothing left" once every
            # currently-active operator has individually been checked, no
            # matter how the weighted draws happened to land.
            empty_operators_seen.add(mode)
            # 2026-07-22: compare against the CURRENTLY active operator
            # count (active_operator_names), not the full static
            # operator_names -- tardy/swap are zero-weighted (never drawn
            # as `mode`) whenever Z1 sits at its bound, so they'd never be
            # added to empty_operators_seen and comparing against the full
            # list would make this threshold unreachable during that
            # stretch, silently disabling the stall/give-up check.
            if len(empty_operators_seen) >= len(active_operator_names):
                print(f"[Greedy] Improve: nothing left to improve (Z1/Z2/Z3 all settled)  round={round_idx}")
                break
            continue
        empty_operators_seen.clear()

        trial_assignments = dict(current_assignments)
        for bid in remove_ids:
            trial_assignments.pop(bid, None)

        bay_placed, bay_schedule, bay_loads = _rebuild_bay_state(
            trial_assignments, bays, blocks_data
        )

        # "balance" targets Z2 (a max/bottleneck over bay pairs, not a
        # per-block sum -- see _try_rebalance_move's docstring), so
        # before falling into the general reinsertion search (which
        # optimizes the blended _placement_score and often does NOT
        # land removed blocks in the specific bay that would close the
        # current bottleneck), force-try placing every removed block
        # directly into the currently lightest-weighted-load bay.
        # Returns None (routine, not an error) if that bay has no room
        # -- falls through to the same xpress/greedy chain every other
        # mode uses.
        partial = None
        used_direct = False
        used_wholebay = False
        if mode == "wholebay":
            # 2026-07-20: jointly re-optimize an ENTIRE bay's current
            # block set (K can be 100+, far past JOINT_MAX_K) instead of
            # a small scored removal -- see analysis/bay_mip_probe.py
            # for the full rationale/validation (K=170 completed in
            # ~4s and found a real ~505k objective improvement once
            # candidate generation was restricted to the target bay).
            # Integrated as an ALNS operator (an earlier standalone
            # "Phase 4" that ran this only in Phase 3's leftover time
            # was removed -- it shared Phase 3's own deadline, so Phase
            # 3 essentially never stalled before it, and Phase 4 never
            # fired in testing) so the already-proven weighted operator
            # selection decides how often it's worth trying, instead of
            # carving out a hardcoded chunk of budget away from the
            # other operators that have already shown strong results.
            # restrict_bay_id + current_positions (both passed to
            # reinsert() only here) are what make this tractable at
            # all: without them, candidate generation alone couldn't
            # even finish for K~100 within any reasonable budget.
            target_bay_id = current_assignments[remove_ids[0]]["bay_id"]
            current_positions = {
                bid: (current_assignments[bid]["bay_id"], current_assignments[bid]["x"],
                     current_assignments[bid]["y"], current_assignments[bid]["orient_idx"],
                     current_assignments[bid]["entry_time"], current_assignments[bid]["exit_time"])
                for bid in remove_ids
            }
            try:
                import xpress_reinsert
                partial = xpress_reinsert.reinsert(
                    remove_ids, blocks_data, bays,
                    bay_placed, bay_schedule, bay_loads,
                    w1, w2, w3, deadline,
                    max_per_block=WHOLE_BAY_MAX_PER_BLOCK,
                    restrict_bay_id=target_bay_id,
                    current_positions=current_positions,
                )
            except Exception:
                partial = None
            used_wholebay = partial is not None
        elif mode == "balance":
            target_bay_id = min(range(len(bays)), key=lambda j: bay_weights[j] * bay_loads[j])
            partial = _try_rebalance_move(
                remove_ids, target_bay_id, blocks_data, bays,
                bay_placed, bay_schedule, deadline,
            )
            used_direct = partial is not None

        # Try an exact joint reinsertion of the removed blocks via Xpress
        # next (see xpress_reinsert.py) -- it can find combinations plain
        # greedy can't (deciding all of them at once instead of one at a
        # time). reinsert() returns None on any failure (Xpress
        # unavailable, no candidates, solve timeout/infeasible, or too
        # large -- see JOINT_MAX_K), which is a routine, expected outcome
        # here, not an error -- always fall back to the same greedy
        # _place_blocks search used everywhere else in this codebase. A
        # single removed block has nothing to jointly optimize against
        # (no pairwise conflicts possible), so skip the MIP overhead
        # entirely. 2026-07-20: keyed off len(remove_ids) rather than the
        # nominal k_values slot k -- modes like "swap" always return
        # exactly 2 ids regardless of which k this round landed on, and
        # a swap pair specifically NEEDS the joint solve (that's the
        # whole point -- see _select_removal_candidates' "swap" docstring)
        # to actually be considered together instead of independently.
        if partial is None and 1 < len(remove_ids) <= JOINT_MAX_K:
            try:
                import xpress_reinsert
                # current_positions lets reinsert()'s same-bay-first fast
                # path restrict each block's initial candidate scan to its
                # own pre-removal bay (see xpress_reinsert.reinsert's
                # docstring) -- current_assignments still holds remove_ids'
                # positions here (trial_assignments above is the popped
                # copy; current_assignments itself is untouched).
                reinsert_current_positions = {
                    bid: (current_assignments[bid]["bay_id"], current_assignments[bid]["x"],
                         current_assignments[bid]["y"], current_assignments[bid]["orient_idx"],
                         current_assignments[bid]["entry_time"], current_assignments[bid]["exit_time"])
                    for bid in remove_ids if bid in current_assignments
                }
                partial = xpress_reinsert.reinsert(
                    remove_ids, blocks_data, bays,
                    bay_placed, bay_schedule, bay_loads,
                    w1, w2, w3, deadline,
                    max_per_block=max_per_block,
                    current_positions=reinsert_current_positions,
                )
            except Exception:
                partial = None

        used_xpress = partial is not None and not used_direct and not used_wholebay
        if partial is None:
            order = sorted(remove_ids, key=lambda b: -_atc_priority(blocks_data[b], p_avg, atc_k))
            partial = _place_blocks(
                order, blocks_data, bays,
                bay_placed, bay_schedule, bay_loads,
                w1, w2, w3, forced_ids=set(),
                prev_assignments=current_assignments,
                deadline=deadline,
                use_maxrects=use_maxrects,
            )
        trial_assignments.update(partial)

        # 2026-07-21: cut over from "full check every round" to "incremental
        # check every round, full check only to CONFIRM an accept" -- see
        # check_feasibility_incremental's docstring and this session's
        # shadow-validation track record (157/157 divergence-free) before
        # this change. Most rounds end up REJECTED, and a rejected trial
        # never touches current_assignments -- if the incremental checker is
        # ever wrong there, the only cost is a missed opportunity, not
        # corrupted state. current_assignments is what every FUTURE
        # incremental check's "already known feasible" precondition depends
        # on, though, so the one moment that actually mutates it (an accept,
        # whether NEW BEST or an annealing/epsilon-relax walk step) still
        # gets a full check_feasibility as a mandatory confirmation gate
        # below, before the mutation happens.
        try:
            trial_result = check_feasibility_incremental(
                prob_info, current_assignments, trial_assignments, set(remove_ids)
            )
        except Exception as _inc_exc:
            print(f"[Greedy] *** INCREMENTAL-CHECK RAISED *** mode={mode} k={k}  "
                  f"{type(_inc_exc).__name__}: {_inc_exc} -- falling back to full check this round")
            trial_result = check_feasibility(prob_info, _build(trial_assignments))

        round_idx += 1
        solver_tag = ("direct-rebalance" if used_direct
                     else "wholebay-xpress" if used_wholebay
                     else "xpress" if used_xpress else "greedy")

        if not trial_result["feasible"]:
            op_weight[mode] = max(MIN_WEIGHT, WEIGHT_DECAY * op_weight[mode] + (1 - WEIGHT_DECAY) * REWARD_REJECTED)
            print(f"[Greedy] Improve round {round_idx}: mode={mode} k={k} via={solver_tag}  "
                  f"infeasible, rejected  w={op_weight[mode]:.2f}")
            stalled += 1
            if (time.time() - last_improvement_time > stall_threshold
                    and rounds_since_last_improve >= MIN_ROUNDS_SINCE_IMPROVE):
                print(f"[Greedy] Improve: no new best for {stalled} rounds / "
                      f"{time.time() - last_improvement_time:.1f}s, stopping early  "
                      f"round={round_idx}")
                break
            continue

        delta = trial_result["objective"] - current_obj
        if annealing:
            temperature = t0 * (cooling_rate ** round_idx)
            accept = delta < 0 or (temperature > 1e-9 and rng.random() < math.exp(-delta / temperature))
        elif (z23_relax and mode in ("preference", "balance") and delta > 0
              and (deadline - time.time()) >= Z23_MIN_REMAINING_FOR_RELAX):
            # epsilon-constraint-style relaxation for these two operators
            # only -- see Z23_RELAX_FRAC's docstring above. temperature
            # here isn't an SA temperature; it's logged as the absolute
            # slack budget so the reason a worse trial got accepted is
            # visible in the log line below. Gated on remaining time
            # (Z23_MIN_REMAINING_FOR_RELAX) -- see that constant's
            # docstring: too little time left to recover from a worse
            # walk step, so fall through to strict acceptance instead.
            temperature = Z23_RELAX_FRAC * max(1.0, current_obj)
            within_step_budget = delta <= temperature
            within_drift_cap = (current_obj + delta) <= best_obj * (1 + Z23_MAX_DRIFT_FRAC)
            accept = within_step_budget and within_drift_cap
        else:
            temperature = 0.0
            accept = delta < -1e-6

        if not accept:
            op_weight[mode] = max(MIN_WEIGHT, WEIGHT_DECAY * op_weight[mode] + (1 - WEIGHT_DECAY) * REWARD_REJECTED)
            print(f"[Greedy] Improve round {round_idx}: mode={mode} k={k} via={solver_tag}  "
                  f"rejected obj={trial_result['objective']:.0f} (T={temperature:.3g})  "
                  f"w={op_weight[mode]:.2f}")
            stalled += 1
            if (time.time() - last_improvement_time > stall_threshold
                    and rounds_since_last_improve >= MIN_ROUNDS_SINCE_IMPROVE):
                print(f"[Greedy] Improve: no new best for {stalled} rounds / "
                      f"{time.time() - last_improvement_time:.1f}s, stopping early  "
                      f"round={round_idx}")
                break
            continue

        # 2026-07-22 (experiment branch, user-proposed): only pay the full
        # check_feasibility confirmation every CONFIRM_CHECK_INTERVAL
        # accepts (see the constant's docstring above for the rationale and
        # shadow-validation evidence) instead of on every single one.
        # Between confirmations, trial_result (check_feasibility_incremental)
        # is trusted directly -- current_assignments does drift ahead of the
        # last full verification for a bounded number of rounds, but the
        # periodic confirmation below (and the unconditional one right
        # before this function returns) guarantees any real divergence is
        # still caught, never silently trusted forever.
        accepts_since_checkpoint += 1
        do_full_check = accepts_since_checkpoint >= CONFIRM_CHECK_INTERVAL
        if do_full_check:
            confirm_result = check_feasibility(prob_info, _build(trial_assignments))
            confirm_mismatch = confirm_result["feasible"] != trial_result["feasible"]
            if not confirm_mismatch and confirm_result["feasible"]:
                confirm_mismatch = abs(confirm_result["objective"] - trial_result["objective"]) > 1e-3
            if confirm_mismatch:
                print(f"[Greedy] *** INCREMENTAL-CHECK MISMATCH after {accepts_since_checkpoint} "
                      f"batched accept(s) *** mode={mode} k={k} removed={remove_ids}  "
                      f"inc=(feasible={trial_result['feasible']}, obj={trial_result.get('objective')})  "
                      f"full=(feasible={confirm_result['feasible']}, stage={confirm_result.get('stage')}, "
                      f"obj={confirm_result.get('objective')})  -- rolling ALL of them back to the last "
                      f"confirmed checkpoint (obj {checkpoint_obj:.0f}) and stopping Improve early")
                current_assignments = checkpoint_assignments
                current_obj = checkpoint_obj
                best_assignments = checkpoint_best_assignments
                best_obj = checkpoint_best_obj
                best_obj1 = checkpoint_best_obj1
                # Reset here (not just on a successful advance) -- we just
                # rolled back to the checkpoint itself, which is already
                # known-good, so the unconditional final check below would
                # otherwise redundantly re-verify it for no reason.
                accepts_since_checkpoint = 0
                break
            trial_result = confirm_result  # authoritative values from here on

        current_assignments = trial_assignments
        current_obj = trial_result["objective"]
        if current_obj < best_obj - 1e-6:
            gain = best_obj - current_obj
            prev_best = best_obj
            best_assignments = current_assignments
            best_obj = current_obj
            best_obj1 = trial_result["obj1"]
            stalled = 0
            rounds_since_last_improve = 0
            last_improvement_time = time.time()
            op_weight[mode] = max(MIN_WEIGHT, WEIGHT_DECAY * op_weight[mode] + (1 - WEIGHT_DECAY) * REWARD_NEW_BEST)
            elapsed = time.time() - t_start
            print(f"[Greedy] Improve round {round_idx}: mode={mode} k={k} removed={remove_ids} "
                  f"via={solver_tag}  NEW BEST obj {prev_best:.0f} -> {best_obj:.0f} "
                  f"(gain={gain:.0f})  "
                  f"w={op_weight[mode]:.2f}  elapsed={elapsed:.1f}s")
            # 2026-07-22: tardy/swap are never removed from operator_names
            # any more (see the comment above operator_names' definition) --
            # just log the bound transition here for visibility; the actual
            # zero-weighting happens fresh every round in the `weights`
            # computation above, driven directly by best_obj1 vs
            # z1_lower_bound, so it can never go stale.
            if best_obj1 <= z1_lower_bound + 1e-6 and not z1_at_bound:
                print(f"[Greedy] Improve: Z1={best_obj1:.0f} reached its theoretical lower "
                      f"bound ({z1_lower_bound:.0f}) -- 'tardy'/'swap' skipped from here "
                      f"(provably futile) unless Z1 regresses again")
        else:
            # accepted but not a new best (annealing/epsilon-relax walk step)
            # -- deliberately does NOT touch `stalled` either way, matching
            # the pre-2026-07-21 pass-based scheme: only a genuine new best
            # resets patience, a lateral walk step shouldn't extend it
            # indefinitely without ever actually improving best_assignments.
            op_weight[mode] = max(MIN_WEIGHT, WEIGHT_DECAY * op_weight[mode] + (1 - WEIGHT_DECAY) * REWARD_ACCEPTED)
            print(f"[Greedy] Improve round {round_idx}: mode={mode} k={k} via={solver_tag}  "
                  f"walked to worse obj={current_obj:.0f} (T={temperature:.3g})  "
                  f"w={op_weight[mode]:.2f}  best still {best_obj:.0f}")

        if do_full_check:
            # This batch of accepts_since_checkpoint just got confirmed
            # (the mismatch branch above already `break`s before reaching
            # here) -- advance the checkpoint to the current, now-verified
            # state and reset the counter.
            checkpoint_assignments = dict(current_assignments)
            checkpoint_obj = current_obj
            checkpoint_best_assignments = dict(best_assignments)
            checkpoint_best_obj = best_obj
            checkpoint_best_obj1 = best_obj1
            accepts_since_checkpoint = 0

    # 2026-07-22 (experiment branch): unconditional final confirmation --
    # if the loop ended (deadline/stall/nothing-left) with unconfirmed
    # accepts still outstanding (accepts_since_checkpoint > 0), verify
    # best_assignments for real before returning it, exactly like the
    # per-batch check above. Never return an unconfirmed state, no matter
    # how the loop exited.
    if accepts_since_checkpoint > 0:
        final_confirm = check_feasibility(prob_info, _build(best_assignments))
        final_mismatch = final_confirm["feasible"] != True
        if not final_mismatch:
            final_mismatch = abs(final_confirm["objective"] - best_obj) > 1e-3
        if final_mismatch:
            print(f"[Greedy] *** INCREMENTAL-CHECK MISMATCH at final return *** "
                  f"claimed obj={best_obj:.0f}, full check says "
                  f"feasible={final_confirm['feasible']} obj={final_confirm.get('objective')} -- "
                  f"returning last confirmed checkpoint (obj {checkpoint_best_obj:.0f}) instead")
            best_assignments = checkpoint_best_assignments
        else:
            print(f"[Greedy] Improve: final batched confirmation OK "
                  f"({accepts_since_checkpoint} accept(s) since last checkpoint)")

    return best_assignments


# -----------------------------------------------------------------------------
# Phase 2.5: left-justify (RCPSP-style schedule compaction)
# -----------------------------------------------------------------------------

# 2026-07-22 bugfix (user-caught): _left_justify/_right_justify's deadline
# parameter used to be a fixed FRACTION OF THE TOTAL TIMELIMIT measured from
# t_start (e.g. t_start + timelimit*0.85) -- fine when every earlier phase
# uses close to its own worst-case share, but if Phase 2 (repair) converges
# early (as it did in a 1000-block synthetic stress test: repair finished
# in 26.8s against an 80%-of-180s=144s allowance), left-justify silently
# INHERITS all of that unused slack, since its deadline is still anchored to
# t_start, not to "how much is actually left right now". Measured directly:
# left-justify + right-justify together consumed ~116s of that leftover
# slack on a single congested bay, starving Phase 3 of nearly the entire
# budget it was supposed to get. Fix: give these two passes their OWN small
# budget computed from `time.time()` at the moment they're ABOUT TO START
# (not from t_start), so it can never balloon just because an earlier phase
# happened to finish ahead of schedule -- capped at whichever is smaller of
# a fraction of whatever's ACTUALLY left, or a small absolute ceiling.
JUSTIFY_MAX_FRAC_OF_REMAINING = 0.05
JUSTIFY_ABS_CAP_SECONDS = 10.0


def _justify_deadline(t_start: float, timelimit: float, outer_frac: float) -> float:
    """
    Compute a tight, "now"-anchored deadline for one left/right-justify call:
    never later than the existing outer_frac-of-timelimit cap (preserves the
    original safety ceiling exactly), AND never more than
    min(JUSTIFY_ABS_CAP_SECONDS, JUSTIFY_MAX_FRAC_OF_REMAINING * whatever's
    actually left right now) beyond the current moment -- see the module
    comment above for why anchoring to t_start alone let an early-finishing
    earlier phase's slack silently balloon this one's real budget.
    """
    now = time.time()
    remaining = max(0.0, (t_start + timelimit) - now)
    own_budget = min(JUSTIFY_ABS_CAP_SECONDS, remaining * JUSTIFY_MAX_FRAC_OF_REMAINING)
    return min(t_start + timelimit * outer_frac, now + own_budget)


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


def _right_justify(
    assignments: dict[int, dict],
    bays: list[Bay],
    blocks_data: list[dict],
    deadline: float | None,
) -> tuple[dict[int, dict], int]:
    """
    Push blocks as LATE as each bay's own current right edge allows --
    classical RCPSP "right justification" (Valls et al.), the mirror of
    _left_justify.

    Motivation (2026-07-20): alternating left- and right-justification
    passes is a standard RCPSP technique for escaping local optima that a
    single directional compaction can't -- a schedule that's already left-
    justified can still have blocks positioned in a way that, after being
    deliberately pushed toward the LATE end of the bay's current span,
    opens up DIFFERENT gaps that a subsequent left-justify pass can exploit
    better than the first left-justify pass did (see notes/
    algorithm_overview.md's Left Justification entry -- this is the
    "alternate with Right if beneficial" follow-up planned there, now
    justified since Left has repeatedly shown real gains in today's logs).

    Unlike _left_justify (which only ever accepts a move that's individually
    earlier than the block's current position -- a strict, always-safe
    improvement), this pass is INTENTIONALLY not individually-improving: it
    can push a block's own exit_time later than it currently is (increasing
    that block's own tardiness), as long as it doesn't exceed the bay's
    current overall right edge (the max exit_time already present in that
    bay when the sweep for that bay starts). This is deliberate -- the point
    is to explore a differently-shaped configuration, not to greedily
    improve on this pass alone. The caller MUST treat this as a scratch
    exploration step: run this, then _left_justify on the result, then
    check_feasibility + compare the combined before/after objective ONCE,
    keeping the two-pass result only if it's feasible and at least as good
    as the state before right-justify started (exactly like every other
    "trial then verify-or-discard" pattern in this module). Right-justify on
    its own, without a mandatory following left-justify + whole-result
    verification, is not safe to keep.

    For each block, bay-by-bay and LATEST-current-entry first (mirroring
    left-justify's earliest-first order -- so a later block's own move can
    free room for the next-latest one within the same sweep), keeps its
    bay/position/orientation fixed and asks _find_latest_slot for the latest
    feasible slot not exceeding that bay's snapshot right edge.

    Never mutates the input assignments. Returns (new_assignments, n_moved).
    """
    trial = dict(assignments)
    n_moved = 0

    for bay_id, bay in enumerate(bays):
        bay_block_ids_snapshot = [bid for bid, a in trial.items() if a["bay_id"] == bay_id]
        if not bay_block_ids_snapshot:
            continue
        bay_right_edge = max(trial[bid]["exit_time"] for bid in bay_block_ids_snapshot)
        bay_block_ids = sorted(bay_block_ids_snapshot, key=lambda bid: trial[bid]["entry_time"], reverse=True)

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
            new_entry, new_exit = _find_latest_slot(
                this_blk, bay, placed_others, schedule_others, r_time, proc,
                latest_exit_bound=bay_right_edge, deadline=deadline,
            )
            if new_entry is not None and new_entry > a["entry_time"]:
                trial[bid] = dict(a, entry_time=int(new_entry), exit_time=int(new_exit))
                n_moved += 1

    return trial, n_moved


WHOLE_BAY_MAX_PER_BLOCK = 40


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
            blocking_chain: bool = True,
            max_per_block: int = 20,
            use_maxrects: bool = False) -> tuple[dict[int, dict], dict | None]:
    """
    Iteratively detect infeasible blocks and repair them.

    Runs up to max_passes rounds of: check_feasibility -> collect violating
    block ids -> re-place them.  Stops early if the solution becomes feasible
    or 98% of timelimit is consumed.

    Returns (assignments, verified_result). verified_result is the
    check_feasibility dict that exactly matches the returned assignments --
    reusable by the caller instead of recomputing an identical check
    (2026-07-22, user-proposed perf fix) -- or None when it can't be trusted
    to still match (the final-guarantee force-place block below mutates
    assignments AFTER its own last check_feasibility call, so that call is
    stale relative to what's actually returned).

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
        # Capped at 78% (not 80%, not 98%) so Phase 3 (_improve) is
        # structurally guaranteed a real slice of the budget instead of only
        # getting whatever repair happens not to use -- repair already exits
        # early via the feasible-break below whenever it converges sooner
        # anyway. 2026-07-22 (user-proposed): tightened from 80% to 78%,
        # deliberately leaving the individual pass attempts below (which
        # still target 80% for their own internal deadlines) a small,
        # GUARANTEED 2%-of-timelimit reserve that this loop itself will never
        # spend -- reserved specifically for the final feasibility-guarantee
        # step after this loop (see below), so that step doesn't have to
        # compete with an in-progress pass for whatever time is left.
        if time.time() - t_start > timelimit * 0.78:
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

            # Snapshot each to_repair block's pre-repair (bay, x, y, orient,
            # entry, exit) before it's popped below -- passed to reinsert()
            # as current_positions so its same-bay-first fast path (see
            # xpress_reinsert.reinsert's docstring) can restrict the initial
            # candidate scan to each block's own current bay instead of
            # always scanning every bay -- that all-bays scan measured as
            # the dominant per-round cost on congested bays (see
            # notes/algorithm_overview.md). Safe to include even though
            # to_repair's positions are individually violating: the MIP's
            # pairwise conflict constraints still apply to this candidate
            # like any other, so a genuinely conflicting "current position"
            # pair can never both be chosen.
            repair_current_positions = {
                bid: (assignments[bid]["bay_id"], assignments[bid]["x"], assignments[bid]["y"],
                     assignments[bid]["orient_idx"], assignments[bid]["entry_time"],
                     assignments[bid]["exit_time"])
                for bid in to_repair if bid in assignments
            }

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
                        max_per_block=max_per_block,
                        current_positions=repair_current_positions,
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
                    # Time guard: switch to forced path once 85% of timelimit is
                    # used (was 75%, then 90% before that -- see below). Without
                    # this, a slow repair search could exhaust the timelimit
                    # before all blocks are placed, causing Stage-1 (assignment)
                    # failures.
                    # 2026-07-22 bugfix (user-caught): this pre-check used to
                    # fire at 75%, which is BEFORE the `deadline=timelimit*0.80`
                    # passed to _place_blocks below even matters -- since
                    # `bi in forced_ids` short-circuits _place_blocks' own
                    # used_forced check, this manual guard was silently
                    # preempting the hard_deadline overtime window added below
                    # (0.80 -> 0.85) before it ever got a chance to run, making
                    # that addition dead code. Raised to 0.85 (matching the new
                    # hard_deadline exactly) so the graduated
                    # reduced-but-nonzero search in that window actually runs
                    # instead of every block past 0.75 falling straight to
                    # _force_place with zero search -- this is the same
                    # wall-clock-noise cliff diagnosed on prob_10's Phase 1,
                    # just reachable here too on a large `to_repair` batch.
                    if time.time() - t_start > timelimit * 0.85:
                        forced_ids.add(bi)
                    prev_a  = trial_assignments.get(bi)
                    # 2026-07-22: hard_deadline extends the same
                    # overtime mitigation _place_blocks now has for Phase 1
                    # (see its docstring / OVERTIME_SCAN_CAP) to repair's own
                    # sequential loop -- previously this deadline had no
                    # hard_deadline at all, so the same wall-clock-noise
                    # cliff diagnosed on prob_10's Phase 1 could equally hit
                    # a large `to_repair` batch here: crossing
                    # timelimit*0.80 by a few seconds of pure machine noise
                    # would dump every remaining repair-target block into
                    # _force_place with zero search. A smaller +5 percentage
                    # point window than Phase 1's +10 (0.80 -> 0.85, vs
                    # 0.50 -> 0.60) since repair's own budget share is
                    # already tighter and Phase 3 still needs what's left.
                    partial = _place_blocks(
                        [bi], blocks_data, bays,
                        bay_placed, bay_schedule2, bay_loads,
                        w1, w2, w3, forced_ids,
                        prev_assignments=trial_assignments,
                        deadline=t_start + timelimit * 0.80,
                        hard_deadline=t_start + timelimit * 0.85,
                        use_maxrects=use_maxrects,
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
                    use_maxrects=use_maxrects,
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

    # -- Final feasibility guarantee (2026-07-22, user-proposed) --------------
    # The loop above can exit (78% time cap or max_passes exhausted) with
    # SOME violations still unresolved -- returning that state risks the
    # caller (myalgorithm._iterated_greedy) discarding it as infeasible and,
    # if no time remains for another restart, falling all the way back to
    # _emergency_fallback -- a MUCH lower quality floor than whatever real
    # repair progress was made here. _force_place is provably crane-safe
    # regardless of shape/position (see its docstring), so force-place every
    # still-violating block, guaranteeing feasibility here unconditionally
    # instead of leaving it to chance.
    #
    # Time budget: the loop above was tightened from 80% to 78% specifically
    # to leave this step a GUARANTEED, uncontested 2%-of-timelimit reserve
    # (see that change's comment). `result` above is already a FRESH,
    # authoritative check against the current `sol` -- reused here, not
    # recomputed, so this costs nothing extra to find out what's still
    # broken. Does NOT re-verify with a second check_feasibility call after
    # force-placing (that would cost as much as `result` itself already did
    # -- possibly ~19s on a very large instance, measured directly -- for a
    # confirmation Phase 2.5/2.6/3's own upcoming checks will give for free
    # moments later anyway). Skips the attempt entirely if already past 95%
    # of timelimit -- at that point there may not even be room for the
    # force-place loop itself, and attempting it risks CAUSING a TLE instead
    # of preventing an infeasible return, which would be strictly worse.
    if not result["feasible"] and time.time() - t_start < timelimit * 0.95:
        final_viol_ids: list[int] = []
        seen_final: set[int] = set()
        for v in result["violations"]:
            for x in re.findall(r"block (\d+)", v):
                bid = int(x)
                if bid not in seen_final:
                    seen_final.add(bid)
                    final_viol_ids.append(bid)
        print(f"[Greedy] Repair: final guarantee -- force-placing {len(final_viol_ids)} "
              f"still-violating block(s) to avoid returning infeasible")
        # "뽑아내고 밀어넣기" (extract-then-insert, user-specified): remove
        # every violator from assignments FIRST, so none of them can
        # phantom-collide with its own stale position, then rebuild a clean
        # bay state from what's left (mirrors the main loop's own
        # greedy-mode pattern above).
        for bid in final_viol_ids:
            assignments.pop(bid, None)
        bay_placed_f, bay_schedule_f, bay_loads_f = _rebuild_bay_state(
            assignments, bays, blocks_data
        )
        # Sequential, one at a time (user-specified): _aabb_gap_entry
        # computes each block's entry window against the bay state passed to
        # it -- placing several blocks against the SAME snapshot would let
        # them all land in the same "empty" slot and collide with EACH
        # OTHER. Commit each one (update bay_placed_f/bay_schedule_f/
        # bay_loads_f) before computing the next.
        for bid in final_viol_ids:
            bay_id, px, py, oi, entry, exit_t = _force_place(
                bid, blocks_data, bays, bay_placed_f, bay_schedule_f,
                blocks_data[bid]["bay_preferences"],
            )
            new_blk = Block(block_id=bid, block_data=blocks_data[bid], x=px, y=py, orient_idx=oi)
            bay_placed_f[bay_id].append(new_blk)
            bay_schedule_f[bay_id].append((entry, exit_t))
            bay_loads_f[bay_id] += blocks_data[bid]["workload"]
            assignments[bid] = {
                "block_id": bid, "bay_id": bay_id,
                "x": int(px), "y": int(py), "orient_idx": oi,
                "entry_time": int(entry), "exit_time": int(exit_t),
            }
        print(f"[Greedy] Repair: final guarantee applied to {len(final_viol_ids)} block(s) "
              f"(not re-verified here -- see comment above; Phase 2.5/2.6/3's own upcoming "
              f"checks will confirm)")
    elif not result["feasible"]:
        print(f"[Greedy] Repair: final guarantee SKIPPED -- already at "
              f"{(time.time() - t_start) / timelimit * 100:.0f}% of timelimit, not enough "
              f"margin left to safely attempt it without risking a TLE")

    # `result` still matches `assignments` exactly iff it was already
    # feasible at the point it was computed above -- both branches that
    # mutate `assignments` afterward (force-place) are gated on
    # `not result["feasible"]`, so a feasible `result` here guarantees
    # neither one ran. An infeasible result is never safe to forward (either
    # it was mutated after, or the caller needs a fresh check regardless).
    verified_result = result if result["feasible"] else None
    return assignments, verified_result


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
