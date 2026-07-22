# myalgorithm.py
# Competition entry point.
#
# Scoring reminder (see problem statement Sec 3.3): infeasible, time-limit-
# exceeded, and "raised an exception" all score exactly -1 on a hidden
# instance -- identical to never having attempted it. There is no partial
# credit for "the real algorithm almost worked." That makes this wrapper's
# job the single highest-leverage piece of code in the repo: make it
# structurally very hard for algorithm() to ever return something that is
# not a feasible solution.
#
# Strategy:
#   1. Run the real solver (baseline_greedy.greedyalgorithm) with a safety
#      margin subtracted from the given timelimit, then verify whatever it
#      returns with utils.check_feasibility before trusting it -- greedy
#      Algorithm returns *a* dict even when repair did not fully converge,
#      so "it returned something" is not the same as "it returned a
#      feasible solution".
#   2. If the solver raises, or hands back something infeasible, fall back
#      to _emergency_fallback: a small, self-contained placement that is
#      feasible by construction (see its docstring) and does not import
#      baseline_greedy at all, so a bug anywhere in the main solver cannot
#      take the fallback down with it.
#   3. The whole thing is wrapped so algorithm() itself can never raise.


def algorithm(prob_info, timelimit=60):
    """
    This is a template for the custom algorithm.
    The function signature must not be changed or removed, but you can define extra functions or modules that are used in this function.
    The `prob_info` is a dictionary containing the problem information, and `timelimit` is the time limit for the algorithm in seconds.
    The function should return a solution in the format specified in the problem statement.
    Please refer to baseline_greedy.py for an example implementation of a simple greedy algorithm. You can use it as a starting point or reference for your own algorithm.
    """
    import time
    t_start = time.time()

    try:
        solution = _solve(prob_info, timelimit, t_start)
    except Exception as exc:
        print(f"[algorithm] unexpected top-level failure {type(exc).__name__}: {exc} "
              f"-- returning an empty solution as an absolute last resort.")
        solution = {"operations": {}}

    elapsed = time.time() - t_start
    print(f"[algorithm] total elapsed {elapsed:.2f}s / timelimit {timelimit:.1f}s")
    return solution


def _solve(prob_info: dict, timelimit: float, t_start: float) -> dict:
    """
    Try the real solver first (with iterated-greedy restarts -- see below);
    fall back to a guaranteed-feasible placement if it crashes, or if
    nothing it returns actually passes utils.check_feasibility.
    """
    try:
        import baseline_greedy
        from utils import check_feasibility

        # Leave headroom below the true deadline for the feasibility check
        # below and, if needed, the emergency fallback -- both are cheap,
        # but the server's hardware/load may differ from dev, so don't cut
        # this margin too thin.
        #
        # 2026-07-22 (user-proposed, "measured-based dynamic reserve"): the
        # fixed 10% below was tuned watching dev-machine timings on
        # instances up to a few hundred blocks. utils.check_feasibility's
        # own cost scales with instance size (profiling
        # combined_stress_3400, 2026-07-22: ~2.2s for 3400 blocks =~
        # 0.00065s/block on the dev machine) -- a hidden instance large
        # enough, or a grading server slow enough, could make a single
        # check_feasibility call alone eat more than 10% of timelimit,
        # which is exactly what this margin exists to protect against.
        # Reserve whichever is larger: the original fixed 10%, or an
        # instance-size-derived estimate (doubled from the measured
        # dev-machine rate, to hedge against an unverified, possibly
        # slower, server) times a small multiplier for the handful of more
        # check_feasibility-equivalent calls still to come after this point
        # (Phase 2.5/2.6's post-sweep verification, _improve's final
        # confirmation). Purely additive safety via max() -- never smaller
        # than the original 10%, so no behaviour change on any instance
        # where the estimate stays below it (true for all 40 local
        # instances; largest is 300 blocks, estimate ~1.2s vs. a typical
        # local timelimit's 10% floor of several seconds).
        _EST_CHECK_FEASIBILITY_SEC_PER_BLOCK = 0.0013
        _CHECK_FEASIBILITY_RESERVE_MULTIPLIER = 3
        n_blocks = len(prob_info.get("blocks", []))
        estimated_reserve = (_EST_CHECK_FEASIBILITY_SEC_PER_BLOCK * n_blocks
                             * _CHECK_FEASIBILITY_RESERVE_MULTIPLIER)
        reserve = max(timelimit * 0.1, estimated_reserve)
        inner_timelimit = max(1.0, timelimit - reserve)
        best_solution, best_objective, n_attempts = _iterated_greedy(
            prob_info, baseline_greedy, check_feasibility,
            deadline=t_start + inner_timelimit,
        )
        if best_solution is not None:
            return best_solution
        print(f"[algorithm] baseline_greedy returned no feasible solution across "
              f"{n_attempts} restart attempt(s) -- falling back to emergency placement.")
    except Exception as exc:
        print(f"[algorithm] baseline_greedy raised {type(exc).__name__}: {exc} "
              f"-- falling back to emergency placement.")

    return _emergency_fallback(prob_info)


# Restarts stop once the remaining budget drops below this floor -- below
# it, a fresh Phase 1+2 pass isn't likely to finish, let alone leave any
# room for Phase 3, so the time is better spent letting the current best
# stand than gambling it on a rushed new attempt.
_RESTART_MIN_BUDGET = 10.0
# Purely a safety cap against a pathological case (e.g. a trivially-easy
# instance where every attempt returns almost instantly) looping far more
# than could plausibly help -- in practice, non-trivial instances consume
# most of their budget in a single attempt (Phase 3 runs until the time
# budget or a stall limit is hit), so this is rarely the binding constraint.
_RESTART_MAX_ATTEMPTS = 5


def _iterated_greedy(prob_info, baseline_greedy, check_feasibility, deadline: float):
    """
    Iterated-greedy diversification: call baseline_greedy.greedyalgorithm
    repeatedly, each time with a different seed (so Phase 3's ALNS explores
    a different sequence of destroy/rebuild decisions -- see
    baseline_greedy.greedyalgorithm's seed docstring) and whatever time
    budget remains, keeping the best feasible result seen.

    Motivation: a single greedyalgorithm() call can plateau (Phase 3 stalls
    out -- see baseline_greedy._improve's stall_limit) well before its time
    budget is exhausted, e.g. observed on prob_5 where the pre-restart code
    left tens of seconds of a 180s budget completely unused once local
    search had nothing left to improve in *that* solution's neighbourhood.
    A fresh construction/ALNS run from a different random seed is a
    different starting basin for local search and can find improvements
    the stalled run couldn't -- this is complementary to Phase 3's
    within-run local search, not a replacement for it.

    Naturally self-limiting: each attempt is given only the time actually
    left (deadline - now), so on instances where a single attempt already
    uses the whole budget, this loop runs exactly once -- identical
    behaviour to before restarts existed.

    Returns (best_solution_or_None, best_objective_or_None, n_attempts).
    """
    import time

    best_solution = None
    best_objective = None
    attempt = 0

    while attempt < _RESTART_MAX_ATTEMPTS:
        remaining = deadline - time.time()
        if remaining < _RESTART_MIN_BUDGET:
            break
        attempt += 1
        seed = attempt - 1  # 0, 1, 2, ... -- deterministic across identical runs
        candidate = baseline_greedy.greedyalgorithm(prob_info, timelimit=remaining, seed=seed)
        result = check_feasibility(prob_info, candidate)
        if not result["feasible"]:
            print(f"[algorithm] restart {attempt} (seed={seed}): infeasible "
                  f"(stage={result['stage']}) -- discarded.")
            continue
        objective = result["objective"]
        if best_objective is None or objective < best_objective:
            print(f"[algorithm] restart {attempt} (seed={seed}): NEW BEST "
                  f"objective={objective:.0f}"
                  + (f" (was {best_objective:.0f})" if best_objective is not None else ""))
            best_solution, best_objective = candidate, objective
        else:
            print(f"[algorithm] restart {attempt} (seed={seed}): "
                  f"objective={objective:.0f} (best stays {best_objective:.0f})")

    return best_solution, best_objective, attempt


def _emergency_fallback(prob_info: dict) -> dict:
    """
    Guaranteed-feasible placement used only when the primary solver crashes,
    times out internally, or returns something infeasible.

    Strategy: process blocks in due-date order; each block is placed in the
    first bay (by descending preference) whose bounding box can hold it, at
    the minimum valid integer reference-point position, in a time window
    strictly after every block already scheduled in that bay (bay_free_time).

    Because each bay holds exactly one block at a time under this scheme
    (windows never overlap), every crane operation is trivially
    unobstructed and no spatial collision is possible -- feasibility here
    does not depend on shape, orientation, or any of the more complex
    geometry/search logic elsewhere in the codebase being bug-free.

    Quality is intentionally not a goal: bays are used sequentially rather
    than packed, so this typically produces a working but heavily
    tardy/imbalanced solution. It exists purely as a floor above "-1 for
    exception/timeout/infeasible" on the leaderboard, not as something to
    tune.

    Only imports utils.py (Bay/Block geometry primitives); never imports
    baseline_greedy, so a bug in the main solver's higher-level logic
    cannot also break this fallback.
    """
    import math

    from utils import _resolve_layers, _bounding_box

    bays_data = prob_info["bays"]
    blocks_data = prob_info["blocks"]
    n_bays = len(bays_data)

    bay_free_time = [0] * n_bays  # earliest time each bay is next available

    order = sorted(range(len(blocks_data)), key=lambda i: blocks_data[i]["due_date"])

    assignments = []
    unplaced = []
    for bi in order:
        blk = blocks_data[bi]
        r_time = int(blk["release_time"])
        proc = int(blk["processing_time"])
        prefs = blk["bay_preferences"]

        placed = False
        for bay_id in sorted(range(n_bays), key=lambda j: prefs[j], reverse=True):
            bw = bays_data[bay_id]["width"]
            bh = bays_data[bay_id]["height"]
            for oi, shape_entry in enumerate(blk["shape"]):
                layers = _resolve_layers(shape_entry["layers"])
                if not layers:
                    continue
                all_verts = [v for layer in layers for v in layer]
                lx0, ly0, lx1, ly1 = _bounding_box(all_verts)

                # Valid integer reference-point range: px in [ceil(-lx0), floor(bw-lx1)],
                # py in [ceil(-ly0), floor(bh-ly1)]. The instance format guarantees the
                # reference point (0,0) is itself part of the shape, so lx0<=0<=lx1 and
                # ly0<=0<=ly1 always hold -- ceil(-lx0)/ceil(-ly0) are never negative.
                px_lo = math.ceil(-lx0)
                px_hi = math.floor(bw - lx1)
                py_lo = math.ceil(-ly0)
                py_hi = math.floor(bh - ly1)
                if px_lo > px_hi or py_lo > py_hi:
                    continue  # this orientation cannot fit in this bay at all

                px, py = max(0, px_lo), max(0, py_lo)
                entry = max(r_time, bay_free_time[bay_id])
                exit_t = entry + proc

                assignments.append({
                    "block_id": bi, "bay_id": bay_id,
                    "x": int(px), "y": int(py), "orient_idx": oi,
                    "entry_time": int(entry), "exit_time": int(exit_t),
                })
                bay_free_time[bay_id] = exit_t
                placed = True
                break
            if placed:
                break
        if not placed:
            # No bay/orientation combination fits this block at all -- this
            # means the instance itself has no valid placement for it
            # (malformed instance), which no algorithm could satisfy.
            unplaced.append(bi)

    if unplaced:
        print(f"[algorithm] emergency fallback: {len(unplaced)} block(s) have no valid "
              f"placement in any bay/orientation -- instance appears malformed: {unplaced[:10]}")

    return {"operations": _build_operations(assignments)}


def _build_operations(assignments: list) -> dict:
    """Group a flat assignment list into the {time: [ops]} format, EXIT before ENTRY."""
    by_time: dict = {}
    for a in assignments:
        by_time.setdefault(a["exit_time"], []).append(
            {"type": "EXIT", "block_id": a["block_id"], "bay_id": a["bay_id"]}
        )
        by_time.setdefault(a["entry_time"], []).append(
            {"type": "ENTRY", "block_id": a["block_id"], "bay_id": a["bay_id"],
             "x": a["x"], "y": a["y"], "orient_idx": a["orient_idx"]}
        )

    operations = {}
    for t in sorted(by_time):
        ops = sorted(by_time[t], key=lambda o: (0 if o["type"] == "EXIT" else 1, o["block_id"]))
        operations[str(t)] = ops
    return operations
