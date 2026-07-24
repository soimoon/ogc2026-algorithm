"""
xpress_reinsert.py -- Xpress-assisted exact reinsertion for baseline_greedy's
Phase-3 improvement loop (_improve).

Scope, deliberately narrow: given a small batch of removed blocks (K <= ~5,
see baseline_greedy._improve's k_values) and the fixed context of every
other currently-placed block, choose one (bay, orientation, x, y, entry,
exit) candidate per removed block from a pre-scored candidate list, subject
to no two removed blocks' candidates spatially/temporally conflicting with
each other -- minimizing total candidate cost (_placement_score, the same
scoring function used everywhere else in this codebase).

This is NOT a general geometric packing MIP. Most candidates are generated
by the exact same _candidate_positions / _find_earliest_slot search
baseline_greedy already runs for plain greedy placement, so those add no
new geometry-modeling risk -- each is already individually feasible
against the fixed (non-removed) context on its own. The only thing Xpress
adds is choosing the JOINTLY best combination among the K removed blocks
instead of placing them one at a time greedily, which is a small,
well-scoped independent-set-style assignment problem: binary
y[block, candidate], "exactly one candidate per block", and a "not both" cut
for every pair of candidates (from two different removed blocks) that
would collide in space and time.

2026-07-23 correction: this docstring used to claim every candidate here
is "already individually feasible against the fixed context on its own"
and that "an imperfect conflict model here can only ever cause a proposal
to be (correctly) rejected, never accepted incorrectly" -- both were true
for _find_earliest_slot-derived candidates but NOT for the two supplementary
candidates this module injects on top (_current_position_candidate,
_cross_position_candidate): both are built directly from known position/
timing data without a fresh _find_earliest_slot search, and the
batch-vs-batch pairwise conflict loop below only ever compares candidates
against OTHER BATCH MEMBERS' candidates -- never against the bay's
existing, non-batch occupants. Confirmed via direct reproduction
(prob_39, see notes/algorithm_overview.md) that an injected candidate
could retroactively break an already-scheduled, untouched block's crane
operation and still reach the solver. Both injection sites now
re-validate against the target bay's existing occupants at generation
time (see _cross_candidate_blocked_by_existing) before ever being offered
to the MIP, closing that gap at the source rather than relying solely on
downstream re-validation.

Whatever this proposes is ALSO re-validated by utils.check_feasibility in
most callers before being accepted or rolled back (see _improve's
accept/reject gate) -- but that is now a second line of defense, not the
only thing standing between an imperfect conflict model and an accepted
bad result. reinsert() returns None on any failure (Xpress not
importable/licensed, no candidates, solve infeasible/timed out); callers
must fall back to the existing greedy _place_blocks reinsertion in that
case, never treat this as the only path.

2026-07-24 (user-proposed, following today's cpsat_reinsert.py measurements):
the pairwise conflict-discovery loop below used to be a naive
O(K^2 x candidates^2) double-nested iteration over every block-pair x
candidate-pair combination, regardless of how far apart two candidates
actually are -- explicitly called out as "wholebay's long-documented
scaling ceiling" in this module's own history (see
notes/algorithm_overview.md). cpsat_reinsert.py's 2026-07-23 draft had
already solved the identical sub-problem (grid-bucket candidates by AABB,
only compare pairs sharing a cell) for ITS OWN pairwise loop -- extracted
into baseline_greedy.bucket_candidate_pairs_by_grid() so this module
reuses the same fix instead of duplicating it. This ONLY narrows which
candidate pairs get the expensive _time_overlaps/_bb_overlap/geometry
checks run on them at all -- every check downstream of that narrowing is
byte-for-byte the same as before this change (see
analysis/xpress_reinsert_grid_equivalence.py, which confirms the OLD
naive loop and the NEW grid-narrowed loop produce IDENTICAL conflict
constraint sets on real batches before this was trusted).
"""

from __future__ import annotations

import time

from baseline_greedy import _block_bbox, _placement_score, bucket_candidate_pairs_by_grid
from baseline_greedy import _top_candidates_for_block as _candidates_for_block
from utils import Bay, Block, _bb_overlap, check_collisions, check_entry, check_exit

# 2026-07-22: batches at or below this size get each other's current
# position cross-injected into their candidate pools (see
# _cross_position_candidate) -- see reinsert()'s call site for why this is
# capped (O(K^2) extra cost, not needed for wholebay's much larger K).
CROSS_INJECT_MAX_K = 5
# 2026-07-22 (user-proposed): skip cross-injecting bi into bj's spot when
# bi's own AABB is more than this much larger than bj's in either axis --
# a cheap, deliberately generous pre-filter (not a correctness gate; the
# real polygon check still happens downstream) to avoid paying for a
# candidate/MIP-variable that's almost certainly going to be geometrically
# hopeless anyway (e.g. injecting a large block into a much smaller one's
# footprint). Generous on purpose: false negatives here only cost a missed
# optimization opportunity, never correctness, so err toward not filtering
# out genuinely-plausible swaps.
CROSS_INJECT_SIZE_SLACK = 1.3

# 2026-07-24 bugfix: how often (in constraint pairs processed) the main
# conflict-construction loop below re-checks `deadline` -- see that loop's
# own comment for why this exists (bucket_candidate_pairs_by_grid checks
# the deadline while DISCOVERING close pairs, but the loop that actually
# consumes them to build MIP constraints previously had no check at all).
# Same interval baseline_greedy._GRID_DEADLINE_CHECK_INTERVAL uses for the
# analogous inner loop there -- picked for the same reason (frequent enough
# to bound overrun to a few hundred cache-miss Shapely calls, not so
# frequent that time.time() itself becomes measurable overhead).
_CONFLICT_LOOP_DEADLINE_CHECK_INTERVAL = 500

# 2026-07-22 (experiment/same-bay-first branch): toggle for A/B-testing
# whether the same-bay-first fast path (see reinsert()'s call site) is
# trapping blocks in a congested bay by never even generating an
# other-bay candidate once the current bay alone fills max_per_block --
# see notes/ogc2026_p4p6_investigation memory, hypothesis #1. False reverts
# to the original unconditional all-bays scan.
#
# Investigated 2026-07-22: naive block-DUPLICATE stress instances (x4/x8,
# same 250 blocks repeated) gave inconsistent, direction-flipping results
# (OFF ~1% better at x4, ON ~1.7% better at x8) -- these turned out to be a
# poor proxy, since duplicating identical blocks creates an artificial
# preference-clustering that a real large/heterogeneous instance wouldn't
# have to the same degree. A more realistic test (1000 blocks pooled from 5
# DIFFERENT local instances, each keeping its own shape/preferences) showed
# ON consistently better across 4 repeated runs (4-15% margin, direction
# never flipped) -- same-bay-first does NOT look like the P4-6 stagnation's
# culprit after all, and may genuinely help on realistic heterogeneous
# instances. Kept enabled (True) as the default; this toggle is left in
# place as a debug/re-verification knob, not because the investigation is
# still open.
SAME_BAY_FIRST_ENABLED = True


def _time_overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def _current_position_candidate(bi: int, blk_data: dict, current_pos: tuple,
                                bay_loads: list[float], bay_weights: list[float],
                                w1: float, w2: float, w3: float) -> tuple:
    """
    Score a block's CURRENT (bay_id, x, y, orient_idx, entry, exit) as a
    candidate tuple, in the same (score, bay_id, cx, cy, oi, entry, exit_t)
    format _top_candidates_for_block returns -- see reinsert()'s
    guarantee_current_position docstring for why this exists.
    """
    bay_id, cx, cy, oi, entry, exit_t = current_pos
    due = blk_data["due_date"]
    workload = blk_data["workload"]
    prefs = blk_data["bay_preferences"]
    s_max = max(prefs)
    blk_bb = _block_bbox(blk_data, oi)
    tardiness = max(0.0, exit_t - due)
    score = _placement_score(
        tardiness, workload, bay_loads, bay_id,
        s_max - prefs[bay_id], bay_weights, w1, w2, w3,
        top_y=cy + blk_bb[3],
    )
    return (score, bay_id, cx, cy, oi, entry, exit_t)


def _cross_position_candidate(bi: int, blk_data: dict, my_orient_idx: int,
                              other_pos: tuple, bay: Bay, bay_loads: list[float],
                              bay_weights: list[float],
                              w1: float, w2: float, w3: float) -> tuple | None:
    """
    2026-07-22 (user-proposed). Score block bi occupying a DIFFERENT block's
    current (bay_id, x, y) -- bi's own orientation/shape/processing_time,
    the other block's position and entry time as a starting guess -- as a
    candidate tuple, same format as _current_position_candidate.

    Motivation: on a congested instance, Z1 tardiness caused by two blocks
    mutually blocking each other's spot ("deadlock") can only be resolved by
    literally swapping their positions -- but each block's own candidate
    list is independently ranked and capped (max_per_block, usually 20), so
    the OTHER block's exact spot may simply never be ranked highly enough to
    survive the cap on its own merits, even though "swap" specifically
    selects these two blocks BECAUSE they're blocking each other. Without
    that spot in the pool, the joint MIP structurally cannot express the
    swap at all, no matter how obviously good it would be. Injecting it
    explicitly guarantees the solver always gets to at least CONSIDER the
    literal swap, whether or not the AABB-lower-bound ranking would have
    surfaced it.

    Deliberately cheap (no _find_earliest_slot search) -- like every other
    candidate in this codebase, this is just a suggestion; the pairwise
    conflict check still screens it against the rest of this batch, and the
    caller re-validates the whole trial with the real check_feasibility
    before ever accepting it. Uses bi's OWN orientation (not the other
    block's) since a different block's shape at that orientation index may
    not even correspond to a compatible orientation for bi -- and the other
    block's entry time as a starting guess (the natural "swap" hypothesis:
    each block takes over where the other one was, when it was there),
    combined with bi's own processing_time for the exit.

    2026-07-23 bugfix (found while comparing against a CP-SAT prototype):
    the other block's reference-point (x, y) is only guaranteed valid for
    ITS OWN bbox, not for bi's bbox at my_orient_idx -- if that orientation's
    local bbox has a negative min corner (reference point not at the
    shape's own bottom-left, which real instances do have), reusing the
    other block's (x, y) as-is can place bi's footprint partly outside the
    bay entirely. Harmless in production today (the caller's downstream
    check_feasibility would reject any batch this ends up in, per this
    module's whole safety design), but wasteful -- a single invalid
    candidate can drag an otherwise-good batch's chosen combination into a
    guaranteed rejection. Bounds-checked explicitly now; returns None
    (skip injecting) rather than a candidate that could never be valid.
    """
    other_bay_id, other_cx, other_cy, _other_oi, other_entry, _other_exit = other_pos
    r_time = blk_data["release_time"]
    due = blk_data["due_date"]
    proc = blk_data["processing_time"]
    workload = blk_data["workload"]
    prefs = blk_data["bay_preferences"]
    s_max = max(prefs)
    blk_bb = _block_bbox(blk_data, my_orient_idx)

    lx0, ly0, lx1, ly1 = blk_bb
    if (other_cx + lx0 < 0 or other_cy + ly0 < 0
            or other_cx + lx1 > bay.width or other_cy + ly1 > bay.height):
        return None

    entry = max(r_time, other_entry)
    exit_t = entry + proc
    tardiness = max(0.0, exit_t - due)
    score = _placement_score(
        tardiness, workload, bay_loads, other_bay_id,
        s_max - prefs[other_bay_id], bay_weights, w1, w2, w3,
        top_y=other_cy + blk_bb[3],
    )
    return (score, other_bay_id, other_cx, other_cy, my_orient_idx, entry, exit_t)


def _crane_conflict(bay: Bay,
                    blk_i: Block, entry_i: float, exit_i: float,
                    blk_j: Block, entry_j: float, exit_j: float) -> bool:
    """
    True if placing blk_i and blk_j together in the same bay would violate
    the crane operation constraint (Stage 2/3 in utils.check_feasibility) at
    any of the four moments where one could be present during the other's
    crane operation. Presence windows mirror check_feasibility exactly:
    "a_other < t < e_other" (strict both ends).

    2026-07-20 bugfix: the pairwise conflict check in reinsert() below used
    to call check_collisions() only -- steady-state spatial overlap. That
    misses the case where two batch members' bounding footprints never
    overlap in space at the same instant, but one's crane path (entering or
    leaving) is swept-through-obstructed by a layer of the other at the
    moment of that operation, which is a *different, stricter* rule (same-
    or-higher-level layers, not just same-level). A batch that "solved"
    without this check could still be rejected by check_feasibility right
    after -- observed in testing as the identical block pair reappearing in
    violations on the very next repair pass, cascading into forced
    placement of unrelated blocks. See notes/algorithm_overview.md.

    Blocks whose intervals are fully nested (neither is present at the
    other's entry/exit boundary) don't need this check -- any steady-state
    collision between them is already caught by check_collisions().
    """
    if entry_j < entry_i < exit_j and check_entry(bay, [blk_j], blk_i, fast=True):
        return True
    if entry_i < entry_j < exit_i and check_entry(bay, [blk_i], blk_j, fast=True):
        return True
    if entry_j < exit_i < exit_j and check_exit(bay, [blk_j], blk_i, fast=True):
        return True
    if entry_i < exit_j < exit_i and check_exit(bay, [blk_i], blk_j, fast=True):
        return True
    return False


def _crane_conflict_from_facts(entry_i: float, exit_i: float, entry_j: float, exit_j: float,
                               i_blocks_j: bool, j_blocks_i: bool) -> bool:
    """
    Pure time-comparison restatement of _crane_conflict's four boundary
    conditions, given the two ALREADY-KNOWN (position-only, time-
    independent) geometry facts -- no Shapely calls here at all.

    2026-07-24 (user-proposed): check_exit(bay,[B],X)'s geometry is
    IDENTICAL to check_entry(bay,[B],X)'s (see _crane_conflict's own
    docstring/this module's history -- "the j >= k rule applies in both
    directions"), so conditions 1&3 both only ever need j_blocks_i
    (= check_entry(bay,[blk_j],blk_i)) and conditions 2&4 both only ever
    need i_blocks_j (= check_entry(bay,[blk_i],blk_j)) -- exactly the two
    facts _cached_geometry_facts() computes once and reuses across every
    round of an ALNS run that happens to re-propose the same two
    positions. Kept as a separate function (not inlined into the caller)
    so it stays trivially unit-testable/comparable against the original
    Shapely-calling `_crane_conflict` above.
    """
    if entry_j < entry_i < exit_j and j_blocks_i:
        return True
    if entry_i < entry_j < exit_i and i_blocks_j:
        return True
    if entry_j < exit_i < exit_j and j_blocks_i:
        return True
    if entry_i < exit_j < exit_i and i_blocks_j:
        return True
    return False


def _cached_geometry_facts(cache: dict | None, bay_id: int, bay: Bay,
                           blk_i: Block, key_i: tuple, blk_j: Block, key_j: tuple
                           ) -> tuple[bool, bool, bool]:
    """
    Return (same_level_collision, i_blocks_j_entry, j_blocks_i_entry) for
    this exact pair of FIXED positions -- a pure function of (bay_id, key_i,
    key_j) alone (see this module's 2026-07-24 docstring section for the
    algebraic argument), so safe to memoize across every reinsert() call
    for the lifetime of a single `cache` dict.

    key_i/key_j : (block_id, x, y, orient_idx) -- already computed by the
        caller from its own loop variables, no extra work here. Used only
        as a cache key, never reinterpreted.

    cache=None disables memoization entirely (always recompute) -- the
    safe default for any caller that hasn't opted in (see reinsert()'s
    geometry_cache parameter docstring for why this must never be a bare
    module-level global: a cache that outlives one greedyalgorithm() call
    would silently answer for the WRONG problem instance in any process
    that solves more than one, e.g. analysis/robustness_check.py).

    Cache key is normalized (smaller key first) so (i, j) and (j, i) share
    one entry; check_collisions is symmetric already, and the two
    directional check_entry facts are stored/swapped consistently with
    that normalization.
    """
    if key_i <= key_j:
        a_key, b_key, blk_a, blk_b, swapped = key_i, key_j, blk_i, blk_j, False
    else:
        a_key, b_key, blk_a, blk_b, swapped = key_j, key_i, blk_j, blk_i, True

    if cache is None:
        same_level = bool(check_collisions(bay, [blk_a, blk_b]))
        a_blocks_b = bool(check_entry(bay, [blk_a], blk_b, fast=True))
        b_blocks_a = bool(check_entry(bay, [blk_b], blk_a, fast=True))
    else:
        cache_key = (bay_id, a_key, b_key)
        facts = cache.get(cache_key)
        if facts is None:
            same_level = bool(check_collisions(bay, [blk_a, blk_b]))
            a_blocks_b = bool(check_entry(bay, [blk_a], blk_b, fast=True))
            b_blocks_a = bool(check_entry(bay, [blk_b], blk_a, fast=True))
            facts = (same_level, a_blocks_b, b_blocks_a)
            cache[cache_key] = facts
        else:
            same_level, a_blocks_b, b_blocks_a = facts

    if not swapped:
        return same_level, a_blocks_b, b_blocks_a  # (same_level, i_blocks_j, j_blocks_i)
    return same_level, b_blocks_a, a_blocks_b  # i,j were swapped relative to a,b


def _cross_candidate_blocked_by_existing(bay: Bay, blk: Block, entry: float, exit_t: float,
                                         existing_blocks: list[Block],
                                         existing_schedule: list[tuple[int, int]]) -> bool:
    """
    True if placing blk at (entry, exit_t) in this bay would conflict with
    any of the bay's EXISTING (already-placed, non-batch) occupants -- a
    steady-state same-level collision, or a crane entry/exit obstruction in
    either direction.

    2026-07-23 bugfix companion (found while comparing against a CP-SAT
    prototype). _cross_position_candidate, unlike every other candidate
    source in this module, is built directly from another block's
    position/timing without ever searching against the bay's actual
    current occupants -- the batch-vs-batch pairwise conflict loop in
    reinsert() below only ever compares candidates against OTHER BATCH
    MEMBERS' candidates (ids x ids), never against blocks that are staying
    put. A cross-injected candidate that happens to overlap one of those
    untouched blocks used to sail through with nothing stopping it, unlike
    _find_earliest_slot-derived candidates (always searched against
    bay_placed directly) or _current_position_candidate (provably safe --
    it re-proposes the exact (position, entry, exit) already proven
    feasible in the ORIGINAL, strictly-more-crowded bay state, before any
    of this batch's OTHER members were removed; removing blocks can only
    remove obstructions, never add one, so reusing a fact already proven
    in a superset of the current context can never break in the subset).

    Confirmed by direct reproduction (prob_39, seed=2, bay 2, block 19
    cross-injected into block 63's old slot): the raw candidate had 3 real
    entry obstructions and 2 real exit obstructions against blocks never
    involved in the batch at all -- exactly the kind of retroactive
    breakage equiv_cpsat_vs_xpress.py's INFEASIBLE-MISMATCH cases were
    surfacing. Mirrors the same check_collisions/_crane_conflict
    combination already used for the batch-pairwise loop just below, only
    checked against the bay's OTHER (non-batch) occupants instead of other
    batch candidates.
    """
    for other, (a, e) in zip(existing_blocks, existing_schedule):
        if not _time_overlaps(entry, exit_t, a, e):
            continue
        if check_collisions(bay, [blk, other]) or _crane_conflict(bay, blk, entry, exit_t, other, a, e):
            return True
    return False


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
            current_positions: dict[int, tuple] | None = None,
            geometry_cache: dict | None = None) -> dict[int, dict] | None:
    """
    Jointly reinsert remove_ids via a small Xpress MIP.

    Despite the name, remove_ids need not have ever been placed before --
    candidates are generated purely from the *other* blocks already present
    in bay_placed/bay_schedule/bay_loads (see _top_candidates_for_block),
    so this same function also serves as the joint-batch *insertion*
    primitive for baseline_greedy._place_blocks_batched (Phase 1's
    construction_mode="batched"), not just Phase 2/3 reinsertion.

    Returns {block_id: assignment_dict} on success, or None if Xpress is
    unavailable, any block has zero feasible candidates, the MIP finds no
    feasible/optimal solution, or anything else goes wrong. None is a
    routine, expected outcome (not an error) -- callers must fall back to
    the existing greedy _place_blocks reinsertion whenever this returns None.

    max_per_block was 8 originally; measured fallback rate in that config was
    ~0% success on repair's blocking-chain batches (2026-07-20; see
    notes/algorithm_overview.md). Likely cause: candidates are ranked
    independently per block by _placement_score, with no awareness of the
    OTHER batch member -- and a blocking-chain batch exists specifically
    because the bay is contested there, so both blocks' top few candidates
    tend to cluster in the same desirable spots and collide with each other,
    even when a perfectly good joint arrangement exists further down each
    block's own candidate list. Raised to 20 to give the solver more room to
    find it; still small enough that the O(K^2 x max_per_block^2) pairwise
    conflict-check cost stays cheap for the K<~6 batches seen in practice.

    restrict_bay_id : optional (2026-07-20). Passed straight through to
        _top_candidates_for_block -- when set, candidate generation only
        searches that one bay per block instead of every bay, which is the
        actual fix for bay-scoped joint reinsertion (e.g. "re-optimize this
        whole bay's blocks together") not being able to even finish
        generating candidates for large K (see analysis/bay_mip_probe.py --
        the K=96 probe was searching all 4 bays per block, most of them
        far more crowded than the target one, which was the real cost, not
        the MIP solve). None (default) preserves existing all-bays
        behaviour for every other caller unchanged.
    current_positions : optional (2026-07-20), {block_id: (bay_id, x, y,
        orient_idx, entry, exit_t)}. When a block_id has an entry here, its
        CURRENT position is scored (via _placement_score, same formula as
        every other candidate) and appended to that block's candidate list
        if not already present -- guarantees at least one jointly-feasible
        combination always exists whenever every block's current position is
        provided (e.g. re-optimizing a whole bay's currently-placed blocks
        together: "everyone stays exactly where they are" trivially has zero
        pairwise conflicts, since that's how they were already arranged).
        This does NOT bias the solver toward keeping blocks in place -- it's
        still just one candidate among many, competing on the same score, so
        anything that's actually a real improvement and doesn't conflict
        with other picks is still preferred. It only prevents the solve from
        going INFEASIBLE when a real combination is known to already exist
        but didn't happen to make it into any block's independently-ranked
        top max_per_block (2026-07-20: observed on analysis/bay_mip_probe.py
        -- a 52-block whole-bay reinsertion came back solstatus=3/infeasible
        even at max_per_block=40, because several blocks only had 2-8
        geometrically valid candidates in that bay at all, and evidently
        none of the independently-top-ranked combinations happened to be
        mutually compatible). None (default) -- no guarantee added, existing
        behaviour unchanged for every other caller.
    geometry_cache : optional (2026-07-24, user-proposed), a plain dict used
        to memoize _cached_geometry_facts() lookups across MULTIPLE calls to
        this function -- see that function's docstring for why this is
        provably safe to reuse (a pure function of fixed position pairs,
        independent of timing or any other block's state). None (default,
        used by every caller that hasn't opted in -- e.g. every analysis/
        script) disables memoization entirely, identical to before this
        parameter existed. Callers that DO opt in (baseline_greedy.
        greedyalgorithm(), threaded through _repair/_improve) MUST create a
        fresh dict once per greedyalgorithm() call and never let it outlive
        that call or leak into a different problem instance's solve --
        bay_id/block_id integers alone don't disambiguate between two
        different instances that happen to reuse the same ids, so a stale
        cache from a PREVIOUS instance would silently answer for the wrong
        geometry in any process that solves more than one (e.g.
        analysis/robustness_check.py's multi-instance loop). Never make this
        a bare module-level global for exactly that reason.
    """
    try:
        import xpress as xp
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
                print(f"[xpress_reinsert] DEBUG bail: deadline hit before candidates for block {bi} "
                      f"(remaining={remove_ids})")
                return None
            # 2026-07-22: same-bay-first fast path. The general (non-wholebay)
            # call path here always searched every bay x orientation for
            # every block, even though most repair batches exist because the
            # block's OWN current bay is contested (see this function's
            # docstring on why max_per_block was raised) -- on a congested
            # bay this full all-bays scan measured as the dominant cost of a
            # repair round (6+ seconds for a 2-3 block batch, see
            # notes/algorithm_overview.md). If the block's current bay alone
            # already yields a full max_per_block quota of candidates (via
            # the same AABB-lower-bound ranking used everywhere else), that
            # bay is not sparse -- scanning the other bays too is very
            # unlikely to change the outcome, so skip it. If the current bay
            # comes up short (sparse/contested-out), fall through to the
            # unrestricted full search exactly as before -- this never
            # narrows the search in the case that actually needs it (a block
            # whose current bay genuinely has no good options left), only in
            # the case where its current bay already has plenty.
            cands = None
            if SAME_BAY_FIRST_ENABLED and restrict_bay_id is None and current_positions is not None and bi in current_positions:
                own_bay = current_positions[bi][0]
                same_bay_cands = _candidates_for_block(
                    bi, blocks_data[bi], bays, bay_placed, bay_schedule, bay_loads,
                    w1, w2, w3, bay_weights, max_per_block, deadline,
                    restrict_bay_id=own_bay,
                )
                if len(same_bay_cands) >= max_per_block:
                    cands = same_bay_cands
            if cands is None:
                cands = _candidates_for_block(
                    bi, blocks_data[bi], bays, bay_placed, bay_schedule, bay_loads,
                    w1, w2, w3, bay_weights, max_per_block, deadline,
                    restrict_bay_id=restrict_bay_id,
                )
            if current_positions is not None and bi in current_positions:
                current_cand = _current_position_candidate(
                    bi, blocks_data[bi], current_positions[bi],
                    bay_loads, bay_weights, w1, w2, w3,
                )
                # 2026-07-23 bugfix: this candidate is only guaranteed safe
                # against the bay's EXISTING (non-batch) occupants when it
                # was already proven feasible in the fuller, pre-removal
                # state -- true for _improve's LNS operators (which only
                # ever remove currently-FEASIBLE blocks) but explicitly
                # FALSE for _repair's blocking_chain joint-reinsert path,
                # whose whole to_repair batch is, by definition, currently
                # VIOLATING (see baseline_greedy.py's repair_current_positions
                # comment -- it only reasons about batch-internal conflicts,
                # via the pairwise loop below, never against non-batch
                # blocks). Reusing the same existing-block check just added
                # for _cross_position_candidate closes that gap uniformly:
                # a no-op (always passes) whenever the safety proof already
                # holds, the actual missing safety net whenever it doesn't.
                (_, cc_bay_id, cc_cx, cc_cy, cc_oi,
                 cc_entry, cc_exit) = current_cand
                cc_blk = Block(block_id=bi, block_data=blocks_data[bi],
                               x=cc_cx, y=cc_cy, orient_idx=cc_oi)
                if not _cross_candidate_blocked_by_existing(
                        bays[cc_bay_id], cc_blk, cc_entry, cc_exit,
                        bay_placed[cc_bay_id], bay_schedule[cc_bay_id]):
                    already_present = any(
                        c[1:] == current_cand[1:] for c in cands
                    )
                    if not already_present:
                        cands = cands + [current_cand]
            # 2026-07-22 (user-proposed): also inject each OTHER batch
            # member's current position as a candidate for bi -- see
            # _cross_position_candidate's docstring. Gated to small batches
            # (CROSS_INJECT_MAX_K) since this is O(K) extra candidates per
            # block, O(K^2) for the whole batch -- fine for "swap" (always
            # exactly K=2) and other small-k operators, but would add
            # needless cost to wholebay's much larger batches (K up to
            # ~170), which already has its own current_positions-based
            # fallback guarantee (_current_position_candidate above) and
            # doesn't need this cross-injection to make progress.
            if (current_positions is not None and bi in current_positions
                    and len(remove_ids) <= CROSS_INJECT_MAX_K):
                my_orient_idx = current_positions[bi][3]
                my_bb = _block_bbox(blocks_data[bi], my_orient_idx)
                my_w, my_h = my_bb[2] - my_bb[0], my_bb[3] - my_bb[1]
                for bj in remove_ids:
                    if bj == bi or bj not in current_positions:
                        continue
                    # cheap AABB size pre-filter (user-proposed) -- skip
                    # injecting bi into bj's spot when bi's own footprint is
                    # clearly too big to plausibly fit there, before paying
                    # for the candidate/MIP-variable at all.
                    other_orient_idx = current_positions[bj][3]
                    other_bb = _block_bbox(blocks_data[bj], other_orient_idx)
                    other_w = other_bb[2] - other_bb[0]
                    other_h = other_bb[3] - other_bb[1]
                    if (my_w > other_w * CROSS_INJECT_SIZE_SLACK
                            or my_h > other_h * CROSS_INJECT_SIZE_SLACK):
                        continue
                    cross_cand = _cross_position_candidate(
                        bi, blocks_data[bi], my_orient_idx, current_positions[bj],
                        bays[current_positions[bj][0]], bay_loads, bay_weights, w1, w2, w3,
                    )
                    if cross_cand is None:
                        continue  # bi's shape at this orientation doesn't fit bj's spot
                    (_, cand_bay_id, cand_cx, cand_cy, cand_oi,
                     cand_entry, cand_exit) = cross_cand
                    cand_blk = Block(block_id=bi, block_data=blocks_data[bi],
                                     x=cand_cx, y=cand_cy, orient_idx=cand_oi)
                    if _cross_candidate_blocked_by_existing(
                            bays[cand_bay_id], cand_blk, cand_entry, cand_exit,
                            bay_placed[cand_bay_id], bay_schedule[cand_bay_id]):
                        continue  # would retroactively break/collide with a non-batch block
                    already_present = any(
                        c[1:] == cross_cand[1:] for c in cands
                    )
                    if not already_present:
                        cands = cands + [cross_cand]
            if not cands:
                print(f"[xpress_reinsert] DEBUG bail: block {bi} has 0 candidates "
                      f"(batch={remove_ids})")
                return None  # this block has no feasible slot at all right now
            per_block[bi] = cands
        _t_cand1 = time.time()

        prob = xp.problem()
        y: dict[tuple[int, int], "xp.var"] = {}
        for bi, cands in per_block.items():
            for ci in range(len(cands)):
                y[(bi, ci)] = xp.var(vartype=xp.binary, name=f"y_{bi}_{ci}")
        prob.addVariable(list(y.values()))

        for bi, cands in per_block.items():
            prob.addConstraint(xp.Sum(y[(bi, ci)] for ci in range(len(cands))) == 1)

        # 2026-07-23 (user-proposed): world-space AABB per candidate,
        # precomputed once here rather than inside the O(K^2 x candidates^2)
        # pair loop below. check_collisions/check_entry/check_exit each
        # already have their OWN internal AABB pre-filter (see their
        # docstrings), so this doesn't unlock any new Shapely-skipping those
        # don't already do -- what it skips is everything OUTSIDE that: two
        # full Block() constructions (shape/layer resolution) plus up to
        # three function-call layers (_crane_conflict's own four check_entry/
        # check_exit calls) for every candidate pair whose footprints can
        # never possibly conflict regardless of timing. Candidates spread
        # across a large bay routinely have many same-bay, time-overlapping
        # pairs that are nowhere near each other spatially -- exactly the gap
        # this closes. bb[bi][ci] = (x0, y0, x1, y1) in world coordinates.
        bb: dict[int, list[tuple[float, float, float, float]]] = {}
        for bi, cands in per_block.items():
            blk_bbs = []
            for (_, _, cx, cy, oi, _, _) in cands:
                lx0, ly0, lx1, ly1 = _block_bbox(blocks_data[bi], oi)
                blk_bbs.append((cx + lx0, cy + ly0, cx + lx1, cy + ly1))
            bb[bi] = blk_bbs

        # 2026-07-24: candidates grouped by bay, then narrowed to AABB-close
        # pairs via the shared grid helper (see module docstring) instead of
        # an unconditional O(K^2 x candidates^2) iteration over every
        # block-pair x candidate-pair combination. Grouping by bay first
        # means every returned pair is automatically same-bay -- no more
        # explicit `bay_i != bay_j` check needed. Everything AFTER the
        # narrowing (same-block skip, _time_overlaps, the real
        # check_collisions/_crane_conflict check, the constraint itself) is
        # byte-for-byte identical to before this change.
        ids = list(per_block.keys())
        by_bay: dict[int, list[tuple[tuple[int, int], tuple[float, float, float, float]]]] = {}
        for bi, cands in per_block.items():
            for ci in range(len(cands)):
                bay_id = cands[ci][1]
                by_bay.setdefault(bay_id, []).append(((bi, ci), bb[bi][ci]))

        for bay_id, entries in by_bay.items():
            if deadline is not None and time.time() > deadline:
                return None
            close_pairs = bucket_candidate_pairs_by_grid(entries, deadline=deadline)
            if close_pairs is None:
                print(f"[xpress_reinsert] DEBUG bail: deadline hit during pairwise "
                      f"construction (bay={bay_id})")
                return None
            for _pair_idx, ((bi, ci), (bj, cj)) in enumerate(close_pairs):
                # 2026-07-24 bugfix: bucket_candidate_pairs_by_grid checks
                # `deadline` while DISCOVERING close_pairs, but this loop --
                # which actually consumes them, including geometry_cache
                # misses that trigger fresh Shapely check_collisions/
                # check_entry calls -- previously had no check at all.
                # Confirmed via analysis/robustness_check.py: a single
                # congested bay (dense candidate cluster defeats the grid
                # filter's "skip far pairs" premise, see module docstring)
                # produced a close_pairs list whose consumption alone took
                # 22s past this reinsert() call's deadline (prob_6, K=19
                # batch). Same interval/rationale as the grid helper's own
                # periodic check.
                if (deadline is not None
                        and _pair_idx % _CONFLICT_LOOP_DEADLINE_CHECK_INTERVAL == 0
                        and time.time() > deadline):
                    print(f"[xpress_reinsert] DEBUG bail: deadline hit during conflict-"
                          f"constraint construction (bay={bay_id}, {_pair_idx}/"
                          f"{len(close_pairs)} pairs processed)")
                    return None
                if bi == bj:
                    continue  # same block's own candidates already mutually exclusive
                _, bay_i, cx_i, cy_i, oi_i, entry_i, exit_i = per_block[bi][ci]
                _, bay_j, cx_j, cy_j, oi_j, entry_j, exit_j = per_block[bj][cj]
                if not _time_overlaps(entry_i, exit_i, entry_j, exit_j):
                    continue
                blk_i = Block(block_id=bi, block_data=blocks_data[bi],
                             x=cx_i, y=cy_i, orient_idx=oi_i)
                blk_j = Block(block_id=bj, block_data=blocks_data[bj],
                             x=cx_j, y=cy_j, orient_idx=oi_j)
                # 2026-07-24: same two reasons a (candidate_i, candidate_j)
                # pair can't coexist as before (steady-state same-level
                # collision, or a crane entry sweep obstruction in either
                # direction) -- but the underlying Shapely-calling facts are
                # now looked up through geometry_cache (see
                # _cached_geometry_facts' docstring: both facts are a pure
                # function of the two FIXED positions, independent of entry_i/
                # exit_i/entry_j/exit_j, so memoizing them across every
                # reinsert() call in one greedyalgorithm() run is provably
                # safe). _crane_conflict_from_facts is a pure time-comparison
                # restatement of the original _crane_conflict, given those
                # cached facts -- no new Shapely calls anywhere in this branch.
                same_level, i_blocks_j, j_blocks_i = _cached_geometry_facts(
                    geometry_cache, bay_i, bays[bay_i],
                    blk_i, (bi, cx_i, cy_i, oi_i), blk_j, (bj, cx_j, cy_j, oi_j),
                )
                if same_level or _crane_conflict_from_facts(
                        entry_i, exit_i, entry_j, exit_j, i_blocks_j, j_blocks_i):
                    prob.addConstraint(y[(bi, ci)] + y[(bj, cj)] <= 1)

        _t_pairs1 = time.time()

        objective = xp.Sum(
            per_block[bi][ci][0] * y[(bi, ci)]
            for bi in ids for ci in range(len(per_block[bi]))
        )
        prob.setObjective(objective, sense=xp.minimize)

        remaining = solve_time_limit
        if deadline is not None:
            remaining = max(1, min(solve_time_limit, int(deadline - time.time())))
            if remaining <= 0:
                print(f"[xpress_reinsert] DEBUG bail: no time left before solve (batch={remove_ids})")
                return None
        # prob.controls.maxtime is an integer Xpress control -- int() guards
        # against a caller passing a float solve_time_limit (maxtime would
        # otherwise raise xpress.ModelError deep inside solve(), which
        # looks like a solver failure rather than a caller type mismatch).
        prob.controls.maxtime = int(remaining)
        prob.controls.outputlog = 0
        _t_solve0 = time.time()
        prob.solve()
        _t_solve1 = time.time()

        # 2026-07-21: timing breakdown -- added to find out whether candidate
        # generation (all-bays search per block, no restrict_bay_id for this
        # general call path unlike wholebay's), the O(K^2 x candidates^2)
        # pairwise conflict construction, or the MIP solve itself dominates
        # a given call's cost, before deciding where to optimize next.
        print(f"[xpress_reinsert] TIMING batch_size={len(remove_ids)} "
              f"candidates={_t_cand1-_t_cand0:.3f}s pairs={_t_pairs1-_t_cand1:.3f}s "
              f"solve={_t_solve1-_t_solve0:.3f}s total={_t_solve1-_t_cand0:.3f}s")
        print(f"[xpress_reinsert] DEBUG solstatus={prob.attributes.solstatus} "
              f"batch={remove_ids} n_candidates={[(bi, len(c)) for bi, c in per_block.items()]}")
        if prob.attributes.solstatus not in (xp.SolStatus.OPTIMAL, xp.SolStatus.FEASIBLE):
            return None

        result: dict[int, dict] = {}
        for bi, cands in per_block.items():
            chosen = None
            for ci in range(len(cands)):
                val = prob.getSolution(y[(bi, ci)])
                if val is not None and val > 0.5:
                    chosen = cands[ci]
                    break
            if chosen is None:
                print(f"[xpress_reinsert] DEBUG bail: no chosen candidate extracted for block {bi}")
                return None
            _, bay_id, cx, cy, oi, entry, exit_t = chosen
            result[bi] = {
                "block_id": bi, "bay_id": bay_id,
                "x": int(round(cx)), "y": int(round(cy)), "orient_idx": oi,
                "entry_time": int(round(entry)), "exit_time": int(round(exit_t)),
            }
        return result
    except Exception as _dbg_exc:
        import traceback
        print(f"[xpress_reinsert] DEBUG raised: {_dbg_exc!r}")
        traceback.print_exc()
        return None
