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
        best_solution, best_objective, n_attempts = _iterated_greedy_tiered(
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

# 2026-07-22 (user-proposed): which Phase-1 priority_rule each restart
# attempt uses, cycled by attempt index (not just varying `seed` on a fixed
# rule, as before). Motivation: EDD doesn't always win locally (see
# baseline_greedy.greedyalgorithm's priority_rule docstring) -- on the
# instances it loses, a structurally different construction (not just a
# differently-shuffled EDD order) may give Phase 3 a genuinely better basin
# to improve on.
#
# Weighted heavily toward EDD (3 of 5 slots), not an even split, based on a
# fresh 40-instance re-measurement the same day: EDD now wins 29-33/40
# (stronger than the older 21/40 figure -- today's other fixes seem to have
# widened EDD's lead, likely because the downstream repair/improve machinery
# increasingly compensates for construction-order choices either way).
# "area"/"area_slack" win only 5-6/40 each, and when they LOSE they often
# lose catastrophically (10x-800x worse than EDD on several instances) --
# since _iterated_greedy only ever keeps the strictly-better result, a bad
# attempt is harmless to the final answer, but IS a wasted restart slot.
# EDD gets attempts 1-3 unconditionally (so an instance that only fits 1-3
# restarts -- likely large/slow instances, the ones already stuck across
# submissions -- sees IDENTICAL behaviour to before this change); the
# diversified rules only get tried on attempts 4-5, i.e. only when there's
# genuine spare restart budget left over, which is exactly when trying
# something with a lower average win rate costs nothing to attempt.
_PRIORITY_RULE_CYCLE = ["edd", "edd", "edd", "area_slack", "area"]


def _iterated_greedy(prob_info, baseline_greedy, check_feasibility, deadline: float):
    """
    Iterated-greedy diversification: call baseline_greedy.greedyalgorithm
    repeatedly, each time with a different seed AND (2026-07-22) a different
    Phase-1 priority_rule (see _PRIORITY_RULE_CYCLE above), with whatever
    time budget remains, keeping the best feasible result seen.

    Motivation: a single greedyalgorithm() call can plateau (Phase 3 stalls
    out -- see baseline_greedy._improve's stall_limit) well before its time
    budget is exhausted, e.g. observed on prob_5 where the pre-restart code
    left tens of seconds of a 180s budget completely unused once local
    search had nothing left to improve in *that* solution's neighbourhood.
    A fresh construction/ALNS run from a different random seed is a
    different starting basin for local search and can find improvements
    the stalled run couldn't -- this is complementary to Phase 3's
    within-run local search, not a replacement for it. Varying the priority
    rule too (not just the seed) goes further: a different seed still
    perturbs the SAME EDD-based order, while a different rule gives Phase 3
    a structurally different construction to improve on.

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
        rule = _PRIORITY_RULE_CYCLE[(attempt - 1) % len(_PRIORITY_RULE_CYCLE)]
        candidate = baseline_greedy.greedyalgorithm(prob_info, timelimit=remaining, seed=seed,
                                                     priority_rule=rule)
        result = check_feasibility(prob_info, candidate)
        if not result["feasible"]:
            print(f"[algorithm] restart {attempt} (seed={seed}, rule={rule}): infeasible "
                  f"(stage={result['stage']}) -- discarded.")
            continue
        objective = result["objective"]
        if best_objective is None or objective < best_objective:
            print(f"[algorithm] restart {attempt} (seed={seed}, rule={rule}): NEW BEST "
                  f"objective={objective:.0f}"
                  + (f" (was {best_objective:.0f})" if best_objective is not None else ""))
            best_solution, best_objective = candidate, objective
        else:
            print(f"[algorithm] restart {attempt} (seed={seed}, rule={rule}): "
                  f"objective={objective:.0f} (best stays {best_objective:.0f})")

    return best_solution, best_objective, attempt


# -----------------------------------------------------------------------------
# Parallel restart dispatch (2026-07-24, user-proposed)
# -----------------------------------------------------------------------------
#
# _iterated_greedy above runs its restart attempts SEQUENTIALLY, one process,
# one CPU core -- despite the competition explicitly allowing up to 4 CPU
# cores at eval time (problem statement resource limits). On a large/slow
# instance, a single greedyalgorithm() call's own Phase 1 + Repair can
# already consume most of the time budget, so _RESTART_MIN_BUDGET's floor
# check means the sequential loop above only ever completes ONE attempt --
# directly confirmed on synthetic large-scale stress instances
# (combined_stress_3400/x16: every repeated run landed on "attempt 1" only,
# see notes/ogc2026_p4p6_investigation.md). That is exactly the instance
# scale where the hidden P4-6 problem instances are suspected to live.
#
# _iterated_greedy_parallel below runs the SAME kind of diversified attempts
# (see _PARALLEL_ATTEMPT_PLAN) CONCURRENTLY as separate OS processes instead
# of sequentially -- so a large instance now always gets every planned
# attempt within the same wall-clock budget that used to buy it only one.
# An earlier design (splitting a single greedyalgorithm() run's Phase 3
# across parallel workers, sharing one Phase-1/Repair result) was considered
# and rejected during design discussion: it stops paying off in exactly the
# worst-case regime (a single Phase 1 + Repair pass alone exceeding the
# whole budget), since there would be nothing left to parallelize. Running
# whole independent greedyalgorithm() attempts avoids that failure mode --
# every worker contributes its own construction attempt regardless of how
# far it gets.
#
# Design constraints this had to satisfy (see the code-review discussion
# this session):
#   - Every worker still searches the WHOLE multi-bay solution (not a
#     bay-partitioned subset) so cross-bay rebalance/preference moves stay
#     available in every attempt -- a bay-decomposed design was considered
#     and rejected specifically because it would structurally block exactly
#     the moves Z2/Z3 depend on (confirmed relevant: P3/P6 lean on w3).
#   - A result is never trusted without utils.check_feasibility, exactly
#     like _iterated_greedy's own sequential attempts -- greedyalgorithm()
#     can return a dict even when repair did not fully converge (see this
#     module's own top-of-file docstring).
#   - multiprocessing's "fork" start method is preferred where available
#     (Linux/Mac -- includes the actual Ubuntu grading server), falling
#     back to "spawn" only where fork isn't supported (Windows dev
#     machine). This was NOT the original choice -- spawn was tried first,
#     for what seemed like good reasons (no silently-inherited parent
#     state: open Xpress handles, partially-consumed RNG, an already-
#     applied monkeypatch). Directly disproved during this session's own
#     testing: spawn re-imports and re-executes the __main__ module in
#     every child to reconstruct enough state to unpickle the target
#     function -- if the ACTUAL top-level caller of algorithm() (an
#     external, unknown, unguarded grading harness script, or even just a
#     local test script, as directly reproduced here) does not itself sit
#     behind `if __name__ == "__main__":`, spawning a child re-runs that
#     caller's own top-level code inside the child, which calls
#     algorithm() again, which spawns MORE children -- observed directly,
#     not hypothetical (a local smoke test without the guard produced a
#     genuine recursive re-entry, only saved from a full process-bomb by
#     _RESTART_MIN_BUDGET/_PARALLEL_MIN_BUDGET's own floor checks
#     eventually stopping each generation). fork has no equivalent risk:
#     a forked child is a copy-on-write duplicate of the CURRENT process
#     at the exact point of the call, continuing straight into the target
#     function -- it never re-executes __main__ at all, so this whole bug
#     class cannot occur regardless of how the real, unknown, unauditable
#     grading harness is structured. The specific "inherited state" risks
#     spawn was chosen to avoid don't actually apply to this design in
#     practice: nothing opens an Xpress handle or consumes shared RNG
#     state in the parent before workers are spawned (see
#     _iterated_greedy_parallel below -- dispatch happens immediately),
#     and Block.bounding_rect's monkeypatch is idempotent to inherit.
#   - As a second, independent backstop against the same failure class
#     (belt-and-suspenders, not a substitute for the fork fix above --
#     multiprocessing.current_process() reflects THIS interpreter's own
#     role, not why it was started): _iterated_greedy_parallel refuses to
#     spawn at all when it detects it is already running inside a
#     non-main process, immediately falling back to sequential instead.
#   - Every worker process is explicitly terminated and joined in a
#     `finally` block, unconditionally, regardless of how collection exits
#     (normal completion, a slow/hung worker, or an unexpected exception) --
#     see notes/feedback_check_processes_before_tests: a single lingering
#     process once contaminated this same repo's local timing measurements
#     with ~25% noise; on the actual grading server there is no way to
#     inspect or clean up anything after the fact, so this cannot be left
#     to chance.
#   - Any failure at any layer (process-pool spawn itself, a worker's own
#     greedyalgorithm() call, an IPC/pickling error) falls back to the
#     existing sequential _iterated_greedy with whatever budget remains --
#     this feature can only ever add candidates to choose from, never
#     become a NEW way for algorithm() to return something worse than
#     today's floor.

import os as _os  # noqa: E402 -- module-level import used by the constants/helpers below

# 2026-07-24: 4, matching the competition's <=4 CPU core cap. History: an
# earlier A/B against this feature's first cut at 4 workers surfaced a real
# contention cost (parallel losing to plain sequential on 13/40 local
# instances, a couple severely) even on an 18-core dev machine, leading to a
# "N-1 core rule" experiment at 3 workers. Re-tested with a proper repeated
# (2x) trial on the 10-instance canary set: 3-worker vs 4-worker came back
# genuinely mixed (2 vs 3 wins, 2 ties across 7 completed instances, most
# margins low-confidence/overlapping) -- no clear winner either way, so the
# original contention concern did not reproduce as a decisive effect once
# measured properly. Given that, and that this whole feature's actual
# value driver (parallel-vs-sequential, not 3-vs-4-workers specifically)
# already showed a clear net-positive fair A/B (24 wins/13 losses/3 ties
# across all 40 local instances), the tie-break went to using the full
# competition-allowed core budget rather than leaving one idle on an
# unconfirmed contention hypothesis. Two safety gaps this decision
# specifically depended on closing first (both fixed the same day, see
# their own comments): the quality-floor gap in _iterated_greedy_tiered
# (a slow-server scenario could previously skip Phase 3 entirely, worse
# than plain sequential -- fixed to always attempt one local chain
# regardless of remaining budget) and the _PARALLEL_COLLECT_GRACE
# proportional cap (bounds worst-case total elapsed regardless of
# timelimit size). Peak aggregate memory measured at ~226MB for a 3-process
# run on the largest local instance (250 blocks) -- trivial next to the
# competition's 16GB cap, though unconfirmed at whatever scale the actual
# hidden P4-6 instances turn out to be.
_PARALLEL_MAX_WORKERS = 4

# Same recipe already validated for _iterated_greedy's sequential restart
# cycle (_PRIORITY_RULE_CYCLE above) -- EDD gets the majority of slots
# (locally the strongest single rule, wins 29-33/40 instances), one slot
# goes to area_slack (the stronger of the two non-EDD rules on the
# instances EDD does lose -- see priority_rule's own docstring in
# baseline_greedy.greedyalgorithm). Every slot also gets a distinct seed
# even where the rule repeats (EDD seed=0 vs seed=1), since
# baseline_greedy._perturb_construction_order makes seed alone a genuine
# source of construction diversity, not just a different Phase-3 RNG
# stream. Unlike the sequential cycle above, there is no "only reached with
# spare budget" rationale here -- every slot runs concurrently on its own
# core for the same wall-clock cost, so there is no opportunity-cost reason
# to weight this any more conservatively toward EDD than the already-
# validated sequential recipe already does.
#
# 2026-07-24: restored to 4 slots alongside _PARALLEL_MAX_WORKERS going
# back to 4 (see that constant's comment for the full history -- was
# briefly trimmed to 3 during the "N-1 core rule" experiment, which did not
# hold up under a proper repeated-trial re-test). 3xEDD + 1xarea_slack,
# matching this feature's original mix.
_PARALLEL_ATTEMPT_PLAN = [
    (0, "edd"),
    (1, "edd"),
    (2, "edd"),
    (3, "area_slack"),
]

# Below this much remaining wall-clock budget, process-spawn overhead
# (spawn re-imports this module plus baseline_greedy/xpress_reinsert/
# shapely fresh in every child) is not worth paying -- run the existing
# sequential _iterated_greedy instead, identical to before this feature
# existed. Same order of magnitude as _RESTART_MIN_BUDGET above.
_PARALLEL_MIN_BUDGET = 5.0

# Subtracted from `remaining` before it is handed to each worker as ITS OWN
# `timelimit` -- a worker's internal deadline is anchored to time.time()
# captured AFTER spawn completes (baseline_greedy.greedyalgorithm's own
# t_start), not to the parent's dispatch-time clock reading, so without
# this buffer every worker would silently run slightly PAST the intended
# absolute deadline by however long spawn+dispatch actually took (observed
# on the order of 100-300ms locally). A fixed, generous buffer bounds this
# overshoot risk directly; the cost (very slightly less search time per
# worker) is negligible next to the risk it removes (eating into the
# reserve _solve() already computed for the feasibility check + emergency
# fallback that come after this call).
_PARALLEL_SPAWN_OVERHEAD_BUFFER = 2.0

# Extra wall-clock slack, beyond the shared deadline, given to the parent
# for collecting worker results over IPC before it gives up waiting and
# terminates whatever is still running -- workers stop their OWN search at
# their own (buffer-reduced) deadline regardless, this only covers
# process/queue overhead on top of that.
#
# 2026-07-24 (safety audit, before trusting this feature at all): a FIXED
# 5.0s grace is fine for realistic competition timelimits (problem
# statement: "minutes to ~30min"), but is unsound in general -- _solve()'s
# own `reserve` (the margin it computes for check_feasibility +
# emergency_fallback AFTER this function returns) is typically
# timelimit*0.1, and nothing here ever told _solve() that THIS function
# could also eat up to 5s of extra time on top of its own deadline in the
# abnormal case (a hung/abnormally slow worker actually consuming the full
# grace). For any timelimit below 50s, a flat 5.0s grace is LARGER than
# that 10% reserve, meaning a hung worker could push this function's return
# past the timelimit _solve() was actually given -- exactly the TLE (-1
# score) failure mode this whole codebase's design philosophy exists to
# prevent (see this module's own top-of-file docstring), self-inflicted by
# this feature. Local testing never surfaced it because workers always
# behaved normally (finished at/under their own worker_timelimit, well
# before `deadline` even arrived, so the grace was never actually consumed)
# -- this is a LATENT risk, not something reproduced, but "only manifests
# in the abnormal case" describes exactly the case a safety margin exists
# for. Capped here as min(this, remaining*0.1) at the point of use (see
# collect_deadline below) -- same proportional-with-a-ceiling shape as
# _solve()'s own reserve, bounding worst-case total elapsed to within the
# original timelimit regardless of how small timelimit is.
_PARALLEL_COLLECT_GRACE = 5.0


def _parallel_attempt_worker(prob_info: dict, timelimit: float, seed: int,
                             priority_rule: str) -> tuple:
    """
    Runs exactly one baseline_greedy.greedyalgorithm() attempt end-to-end
    (Phase 1 through Phase 3, completely unmodified) and verifies the
    result with utils.check_feasibility before returning -- identical
    per-attempt logic to _iterated_greedy's own sequential loop, just
    callable from a worker process.

    Returns (seed, priority_rule, solution_or_None, objective_or_None).
    solution/objective are None whenever the attempt was infeasible or
    raised -- never propagates an exception to its caller, matching every
    other "this path failed, the caller must treat it as routine and move
    on" contract in this codebase (xpress_reinsert.reinsert(), etc.).
    """
    try:
        import baseline_greedy as _bg
        from utils import check_feasibility as _cf
        solution = _bg.greedyalgorithm(
            prob_info, timelimit=timelimit, seed=seed, priority_rule=priority_rule,
        )
        result = _cf(prob_info, solution)
        if not result["feasible"]:
            return (seed, priority_rule, None, None)
        return (seed, priority_rule, solution, result["objective"])
    except Exception as exc:
        print(f"[algorithm] parallel attempt (seed={seed}, rule={priority_rule}) raised "
              f"{type(exc).__name__}: {exc}")
        return (seed, priority_rule, None, None)


def _parallel_attempt_entry(result_queue, prob_info: dict, timelimit: float,
                            seed: int, priority_rule: str) -> None:
    """
    Module-level (picklable, required by multiprocessing's spawn start
    method) process entry point -- runs _parallel_attempt_worker and
    reports its result back over result_queue, since a raw
    multiprocessing.Process has no direct return value.

    _parallel_attempt_worker already catches every exception it can raise
    internally, but this wraps it again anyway (belt-and-suspenders: a
    worker process that fails to even report back would otherwise strand
    the parent waiting on the queue until its own collection deadline) and
    also guards the queue.put() call itself, in case that specific step
    ever fails -- if put() fails, the parent's collection loop simply times
    out waiting for this slot instead of raising anywhere.
    """
    try:
        result = _parallel_attempt_worker(prob_info, timelimit, seed, priority_rule)
    except Exception:
        result = (seed, priority_rule, None, None)
    try:
        result_queue.put(result)
    except Exception:
        pass


def _iterated_greedy_parallel(prob_info, baseline_greedy, check_feasibility, deadline: float):
    """
    Parallel replacement for _iterated_greedy: runs the attempts in
    _PARALLEL_ATTEMPT_PLAN CONCURRENTLY as separate OS processes instead of
    sequentially, keeping the best feasible result. See the module-level
    comment block above this section for the full design rationale.

    Falls back to the existing sequential _iterated_greedy (unmodified,
    same `baseline_greedy`/`check_feasibility` already imported by the
    caller) whenever: there isn't enough remaining budget to bother
    (_PARALLEL_MIN_BUDGET), only one usable core is available, or process-
    pool creation itself fails for any reason. Provably no worse than
    _iterated_greedy in every other case either: same one-attempt-per-slot
    structure, same "keep strictly best feasible" selection, same
    check_feasibility-gated trust in every result.

    Returns (best_solution_or_None, best_objective_or_None, n_attempts) --
    identical contract to _iterated_greedy, so _solve() needs no other
    changes beyond calling this instead.
    """
    import time
    import multiprocessing as mp

    def _fallback():
        return _iterated_greedy(prob_info, baseline_greedy, check_feasibility, deadline)

    # Circuit breaker (see the design-rationale comment block above): never
    # spawn from inside a process that is not the original main process.
    # Belt-and-suspenders against runaway recursive spawning regardless of
    # cause -- fork (chosen below specifically to avoid this) removes the
    # mechanism that could trigger it in the first place, but this check
    # costs nothing and catches it unconditionally either way.
    if mp.current_process().name != "MainProcess":
        return _fallback()

    remaining = deadline - time.time()
    if remaining < _PARALLEL_MIN_BUDGET:
        return _fallback()

    n_cores = _os.cpu_count() or 1
    n_workers = max(1, min(_PARALLEL_MAX_WORKERS, n_cores, len(_PARALLEL_ATTEMPT_PLAN)))
    if n_workers <= 1:
        return _fallback()

    plan = _PARALLEL_ATTEMPT_PLAN[:n_workers]
    worker_timelimit = max(1.0, remaining - _PARALLEL_SPAWN_OVERHEAD_BUFFER)

    # Prefer fork (no __main__ re-execution risk -- see the design-rationale
    # comment above _parallel_attempt_worker); only fall back to spawn where
    # fork isn't available at all (Windows). forkserver is deliberately not
    # used here either: it also re-imports __main__ once, at server startup,
    # to determine what the server process needs to preload, so it shares
    # (a smaller-window version of) the same risk spawn has.
    available = mp.get_all_start_methods()
    start_method = "fork" if "fork" in available else "spawn"

    procs = []
    result_queue = None
    try:
        ctx = mp.get_context(start_method)
        result_queue = ctx.Queue()
        for seed, rule in plan:
            p = ctx.Process(
                target=_parallel_attempt_entry,
                args=(result_queue, prob_info, worker_timelimit, seed, rule),
                daemon=True,
            )
            p.start()
            procs.append(p)
    except Exception as exc:
        print(f"[algorithm] parallel restart: process spawn failed ({type(exc).__name__}: "
              f"{exc}) -- falling back to sequential _iterated_greedy")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        return _fallback()

    print(f"[algorithm] parallel restart: {len(procs)} attempt(s) dispatched across separate "
          f"processes (start_method={start_method}, {n_cores} core(s) detected), "
          f"timelimit={worker_timelimit:.1f}s each")

    best_solution = None
    best_objective = None
    n_attempts = 0
    # min(fixed cap, 10% of what was left at dispatch time) -- see
    # _PARALLEL_COLLECT_GRACE's own comment for why an uncapped flat grace
    # is unsound for small timelimits. `remaining` was computed above
    # (deadline - time.time(), before dispatch overhead), so this is a
    # slight overestimate of "10% of what's actually left now", which only
    # makes the cap slightly MORE conservative, never less.
    collect_grace = min(_PARALLEL_COLLECT_GRACE, remaining * 0.1)
    collect_deadline = deadline + collect_grace
    n_collected = 0
    try:
        while n_collected < len(procs):
            remaining_wait = collect_deadline - time.time()
            if remaining_wait <= 0:
                print(f"[algorithm] parallel restart: collection deadline hit with "
                      f"{len(procs) - n_collected} attempt(s) still outstanding")
                break
            try:
                w_seed, w_rule, solution, objective = result_queue.get(timeout=remaining_wait)
            except Exception:
                print(f"[algorithm] parallel restart: collection timed out with "
                      f"{len(procs) - n_collected} attempt(s) still outstanding")
                break
            n_collected += 1
            n_attempts += 1
            if solution is None:
                print(f"[algorithm] parallel attempt (seed={w_seed}, rule={w_rule}): "
                      f"infeasible or failed -- discarded")
                continue
            if best_objective is None or objective < best_objective:
                print(f"[algorithm] parallel attempt (seed={w_seed}, rule={w_rule}): NEW BEST "
                      f"objective={objective:.0f}"
                      + (f" (was {best_objective:.0f})" if best_objective is not None else ""))
                best_solution, best_objective = solution, objective
            else:
                print(f"[algorithm] parallel attempt (seed={w_seed}, rule={w_rule}): "
                      f"objective={objective:.0f} (best stays {best_objective:.0f})")
    finally:
        # Explicit, unconditional cleanup no matter how the block above
        # exited (normal completion, a timed-out/hung worker, or an
        # unexpected exception) -- never leave a worker process alive past
        # this function's return. See this section's design-rationale
        # comment above for why this cannot be left to chance.
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=2.0)
        if result_queue is not None:
            try:
                result_queue.close()
                result_queue.join_thread()
            except Exception:
                pass

    # 2026-07-24 (safety audit): an earlier version of this function tried
    # one more sequential _iterated_greedy attempt here whenever
    # n_attempts < len(procs) (some worker never reported back). Removed --
    # proved dead code, not just unlikely: n_attempts can only be less than
    # len(procs) after the while-loop above exits via one of its two
    # `break`s, both of which only fire once time.time() >= collect_deadline
    # (> the original `deadline`). _iterated_greedy's own very first check
    # is `remaining = deadline - time.time(); if remaining < _RESTART_MIN_BUDGET:
    # break` -- with deadline already in the past, remaining is negative,
    # so that call always returned (None, None, 0) immediately without
    # attempting any real work. Harmless (a no-op call, not a correctness
    # bug), but the removed comment claimed it "spends genuinely remaining
    # budget", which was never true -- kept as a note here rather than
    # silently deleting the history, per this file's own logging philosophy.

    return best_solution, best_objective, n_attempts


# -----------------------------------------------------------------------------
# Tiered parallel restart: shared construction, parallel Phase 3 (2026-07-24)
# -----------------------------------------------------------------------------
#
# _iterated_greedy_parallel above has every worker redo Phase 1 + Repair +
# 2.5/2.6 construction independently before its own Phase 3. Locally, EDD
# (the default priority_rule) wins the plurality of instances (29-33/40 --
# see _PRIORITY_RULE_CYCLE's own docstring), so most of those parallel
# constructions converge close to the same place -- redundant CPU spent on
# (mostly) the same work, especially costly on exactly the large/congested
# instances (suspected P4-6 profile) where construction alone can consume
# most of the budget, leaving little of anything to fan out afterward.
#
# _iterated_greedy_tiered does the ONE deterministic EDD/seed=0 construction
# ONCE (baseline_greedy.greedyalgorithm(..., return_pre_improve_state=True)
# stops right before its own Phase 3 call and hands back everything needed
# to run it separately), then fans every other available core out into
# Phase 3 (_improve) from that SAME shared, already-feasible base, each with
# a different seed. This spends the whole parallel budget on the part of
# the pipeline that benefits from more independent attempts, instead of
# repeating the part that mostly doesn't.
#
# Falls back to _iterated_greedy_parallel (N independent full pipelines) if
# the shared construction itself fails or doesn't reach a feasible base --
# without a valid shared starting point there is nothing to fan out from,
# and N independent attempts (each with its own from-scratch construction)
# is exactly what that function already does. Every other failure mode
# (process spawn, a worker's own _improve call, IPC) degrades the same way
# -- one fewer candidate in the pool, never a crash or a TLE.


def _tiered_phase3_worker(pre_state: dict, seed: int) -> tuple:
    """
    Runs baseline_greedy._improve() starting from a SHARED, already-verified
    Phase 1 + Repair(+2.5/2.6) base (pre_state, from greedyalgorithm's
    return_pre_improve_state=True), with this worker's own seed driving its
    ALNS operator selection/annealing draws -- see
    _iterated_greedy_tiered's module-level comment for the full rationale.

    geometry_cache is deliberately NOT shared from pre_state here -- each
    worker gets a fresh {} instead. pre_state's own geometry_cache (built
    during the single shared construction pass) is still a valid starting
    point in principle, but multiprocessing already pickles a fresh COPY of
    it into each worker's own process regardless (no actual cross-process
    sharing occurs either way), so reusing it wouldn't save anything -- a
    fresh dict keeps each worker's cache scoped to exactly its own
    exploration, matching every other geometry_cache usage's own safety
    argument (never let a cache outlive/cross a single logical run).

    Verifies the result with check_feasibility before returning, exactly
    like _parallel_attempt_worker -- _improve() only ever runs on top of an
    already-feasible base and is internally self-verifying, but this is the
    same "never trust an unverified result across a process boundary"
    discipline used everywhere else in this module.

    Returns (seed, solution_or_None, objective_or_None); never raises.
    """
    try:
        import baseline_greedy as _bg
        from utils import check_feasibility as _cf
        assignments = _bg._improve(
            pre_state["prob_info"], dict(pre_state["assignments"]), pre_state["bays"],
            pre_state["blocks_data"], pre_state["w1"], pre_state["w2"], pre_state["w3"],
            pre_state["t_start"], pre_state["timelimit"],
            atc_k=pre_state["atc_k"], annealing=pre_state["annealing"],
            z2z3_modes=pre_state["z2z3_modes"], seed=seed,
            max_per_block=pre_state["max_per_block"],
            z1_lower_bound=pre_state["z1_lower_bound"], z23_relax=pre_state["z23_relax"],
            use_maxrects=pre_state["use_maxrects"], known_result=pre_state["known_result"],
            geometry_cache={},
        )
        solution = {"operations": _bg._build_operations(list(assignments.values()))}
        result = _cf(pre_state["prob_info"], solution)
        if not result["feasible"]:
            return (seed, None, None)
        return (seed, solution, result["objective"])
    except Exception as exc:
        print(f"[algorithm] tiered Phase-3 worker (seed={seed}) raised "
              f"{type(exc).__name__}: {exc}")
        return (seed, None, None)


def _assignments_from_solution(solution: dict) -> dict[int, dict]:
    """
    Reverse of baseline_greedy._build_operations -- reconstructs a flat
    assignments dict (block_id -> {block_id, bay_id, x, y, orient_idx,
    entry_time, exit_time}) from a wrapped {"operations": {time_str: [op,
    ...]}} solution.

    Needed for island-model migration only: each round's winning candidate
    is tracked as a solution dict (matching this module's own final return
    contract, and reusing _tiered_phase3_worker's existing tested return
    shape unchanged), but the NEXT round's pre_state["assignments"] needs
    the raw per-block dict baseline_greedy._improve() itself operates on --
    converting once per round is cheap (single pass over the operations
    dict) compared to the Phase-3 search it feeds into.
    """
    out: dict[int, dict] = {}
    for t_str, ops in solution["operations"].items():
        t = int(t_str)
        for op in ops:
            bid = op["block_id"]
            entry = out.setdefault(bid, {"block_id": bid, "bay_id": op["bay_id"]})
            if op["type"] == "ENTRY":
                entry["x"] = op["x"]
                entry["y"] = op["y"]
                entry["orient_idx"] = op["orient_idx"]
                entry["entry_time"] = t
            else:
                entry["exit_time"] = t
    return out


def _tiered_phase3_entry(result_queue, pre_state: dict, seed: int) -> None:
    """Module-level (picklable) process entry point -- see
    _parallel_attempt_entry, same wrapping rationale."""
    try:
        result = _tiered_phase3_worker(pre_state, seed)
    except Exception:
        result = (seed, None, None)
    try:
        result_queue.put(result)
    except Exception:
        pass


# 2026-07-24 (island-model migration, user-proposed "try idea #2"): every
# Phase-3 chain up to this point ran to completion with zero communication
# between workers -- a worker that drew an unlucky sequence of ALNS moves
# (or just started from a weaker basin, in the N-independent-pipeline
# fallback) had no way to benefit from another worker's progress until the
# very end, when only the single best result gets kept and everything else
# is thrown away. Splitting each worker's budget into _ISLAND_N_SEGMENTS
# rounds and having every worker's NEXT round start from the GLOBAL best
# found so far (not its own -- full migration, not partial) reallocates
# the whole parallel budget toward whatever the swarm's best basin turns
# out to be, each round, instead of some cores potentially spending their
# entire budget on a basin nothing ever rescues them from.
#
# Deliberately reuses the exact SAME dispatch/collect/cleanup machinery as
# the original single-shot design (_tiered_phase3_round below is the same
# logic _iterated_greedy_tiered used to run once, inline, now callable
# multiple times) rather than inventing live inter-process communication
# (a shared Manager/pipe mid-run) -- each "round" is its own independent
# spawn-collect-cleanup cycle, exactly like today's proven single round,
# just repeated with an evolving starting point. This trades some
# additional process-spawn overhead (paid _ISLAND_N_SEGMENTS times instead
# of once) for migration -- only engaged when there is enough total budget
# to make that trade worthwhile (see _ISLAND_MIN_SEGMENT_BUDGET below);
# below that, degrades to exactly one round covering the whole remaining
# budget, identical to the pre-migration behaviour.
#
# Provably no worse than the non-segmented version: the global best is
# tracked via a plain min() across every candidate from every round (same
# pattern as the original single-round collection), so it can only ever
# stay the same or improve round over round -- a "wasted" round (every
# worker's segment fails to beat the incoming global best) just means the
# next round starts from the same place, not a regression.
_ISLAND_N_SEGMENTS = 3
# Below this per-segment budget, process-spawn overhead (paid once per
# segment, not once total) starts to dominate -- see
# _PARALLEL_SPAWN_OVERHEAD_BUFFER's own docstring for the same tradeoff at
# the single-round level. Segmenting is only attempted when
# remaining_after_construct / _ISLAND_N_SEGMENTS would stay at or above
# this floor; otherwise _iterated_greedy_tiered runs exactly one
# unsegmented round instead, identical to before this feature existed.
_ISLAND_MIN_SEGMENT_BUDGET = 10.0

# 2026-07-24 (hybrid fix, after an A/B against the actual 9th submission's
# code showed this whole tiered/island feature LOSING on prob_1 (8,041 ->
# 68,633, 8.5x worse) and prob_18 (174,788 -> 194,062, ~11% worse), both
# deterministic across repeats): every Phase-3 worker above only ever
# refines the ONE shared EDD/seed=0 construction. ALNS moves (_improve's
# operators) are local, feasible-preserving edits -- they cannot reproduce
# what a DIFFERENT Phase-1 construction reaches, since that changes the
# initial placement/order globally, not incrementally.
#
# First attempt at a fix reserved one worker slot for a single alternate-
# construction attempt with a different priority_rule (area_slack). Empirically
# verified NOT to fix either regression: probing the 9th submission's own
# sequential _iterated_greedy directly on both instances showed the winning
# basin comes from priority_rule="edd" with seed=1 (the SECOND restart
# attempt, same rule as the shared base) -- NOT a different rule at all:
#   prob_1:  restart 1 (seed=0, edd)=68,633   restart 2 (seed=1, edd)=8,041 (best)   restart 3 (seed=2, edd)=90,809
#   prob_18: restart 1 (seed=0, edd)=194,062  restart 2 (seed=1, edd)=174,788 (best) restart 3 (seed=2, edd)=197,235
# i.e. Phase-1 EDD construction is NOT seed-invariant (tie-breaking among
# same-due-date blocks alone produces an 8.5x objective swing here) -- the
# tiered design's whole premise that "EDD converges to basically the same
# place regardless of seed, so sharing one seed=0 base is safe" is simply
# wrong for these instances. A single area_slack attempt can't recover a
# same-rule-different-seed basin, and area_slack itself is unreliable
# besides -- on prob_18 it scored 66,041,060 (vs the 174,788 that won).
#
# Fix: instead of one fixed (seed, rule) attempt, the alt slot runs the
# EXISTING _iterated_greedy restart loop (unmodified -- cycles
# _PRIORITY_RULE_CYCLE with an increasing seed each attempt, same as the old
# purely-sequential path) inside its own process for whatever's left of the
# shared deadline. On both regressed instances the old sequential path only
# ever completed 3-4 full attempts in a 90s budget before finding its best
# -- giving one full core to that exact same loop (_alt_restart_entry below)
# reproduces it directly, instead of trying to approximate it with a single
# shot at a different rule.


def _alt_restart_entry(result_queue, prob_info: dict, deadline: float) -> None:
    """
    Module-level (picklable) process entry point for the alt-construction
    slot -- see the module-level comment above for why this exists and why
    it runs the full _iterated_greedy restart loop rather than one attempt.

    Passed the SAME absolute `deadline` _iterated_greedy_tiered itself uses
    (not a relative timelimit computed in the parent before spawn) --
    _iterated_greedy reads time.time() itself on every iteration, inside
    this child process, so it already anchors correctly to the true wall
    clock without needing a spawn-overhead buffer subtracted in the parent
    first (unlike _parallel_attempt_worker's worker_timelimit, which IS a
    relative value handed to greedyalgorithm's own t_start-anchored clock).

    Returns (solution_or_None, objective_or_None, n_attempts) on the queue;
    never raises.
    """
    try:
        import baseline_greedy as _bg
        from utils import check_feasibility as _cf
        solution, objective, n_attempts = _iterated_greedy(prob_info, _bg, _cf, deadline)
    except Exception as exc:
        print(f"[algorithm] tiered: alt restart-loop raised {type(exc).__name__}: {exc}")
        solution, objective, n_attempts = None, None, 0
    try:
        result_queue.put((solution, objective, n_attempts))
    except Exception:
        pass


# 2026-07-24 (weight-adaptive alt slot, after re-verifying the restart-loop
# fix above against the 9th submission): prob_1/prob_14/prob_18/prob_40 (w3
# share of w1+w2+w3 all under 2%) all tied or improved with the restart-loop
# alt slot -- prob_1 and prob_18 in particular now match the 9th submission
# EXACTLY (8,041 and 174,788 respectively), confirming the seed=1/edd basin
# diagnosis. But prob_32 and prob_34 -- the two local instances presumed
# closest to P3/P6 (see canary_quality_check.py's own CANARY_SET), both with
# a w3 share an order of magnitude higher (13-15%) than every other canary
# instance -- regressed under the SAME restart-loop alt slot (prob_34 by as
# much as ~31% in one run, well outside that instance's previously observed
# noise band of a few percent).
#
# Mechanically this tracks: the restart-loop's _PRIORITY_RULE_CYCLE is
# mostly "edd" (3 of 5 slots), which searches for a better Z1(tardiness)
# basin specifically -- exactly what a w1-dominant instance needs more of,
# and largely wasted work on a w3-elevated instance where the ALNS
# preference/balance operators inside tiered/island Phase-3 (which the alt
# slot's core is NOT spent on) matter more. Separately, the ORIGINAL single-
# attempt alt slot (priority_rule="area_slack", one shot, no restart loop --
# see this function's own history above) had already turned prob_32 from a
# regression into a win before the restart-loop replaced it -- evidence that
# area_slack specifically (not edd-cycling) is the useful alternate basin
# for this w3-elevated profile, not just "more attempts of anything".
#
# Rather than pick one alt-slot strategy for every instance, use whichever
# one the evidence above actually supports for that instance's own weight
# profile: the edd-focused restart loop for w1-dominant instances (recovers
# prob_1/prob_18 exactly), the single area_slack attempt for w3-elevated
# ones (already a proven win on prob_32, and only ~0.4%/noise-level on
# prob_34 -- versus the restart loop's ~31% regression there).
_TIERED_ALT_W3_SHARE_THRESHOLD = 0.05
_TIERED_ALT_SINGLE_SEED = 5000
_TIERED_ALT_SINGLE_RULE = "area_slack"


def _tiered_phase3_round(pre_state: dict, n_workers: int, deadline: float,
                         seed_base: int) -> list[tuple[int, dict, float]]:
    """
    Runs ONE round of Phase-3 exploration: the main process's own chain
    (seed=seed_base) plus n_workers additional spawned processes (seeds
    seed_base+1..seed_base+n_workers), all starting from pre_state's
    CURRENT assignments/known_result/timelimit, for whatever time remains
    until `deadline`. Returns every candidate collected this round as
    (seed, solution, objective) tuples -- may be shorter than n_workers+1
    if some worker failed, timed out, or never reported back; may even be
    empty if the main chain itself raised AND every spawned worker failed
    (caller must handle an empty list, e.g. by falling back to pre_state's
    own known_result as a still-valid floor).

    Identical dispatch/collect/cleanup logic to the single-round design
    this replaces (fork-preferred, proportional collect_grace, explicit
    unconditional process cleanup in a finally block) -- see
    _iterated_greedy_tiered's own module-level comment for why. Extracted
    into its own function specifically so _iterated_greedy_tiered can call
    it multiple times (island-model migration) without duplicating this
    logic per round.

    Never raises -- every internal failure mode (spawn failure, a worker
    exception, IPC issues) degrades to "fewer candidates in the returned
    list", matching every other "this path failed, treat it as routine"
    contract in this module.
    """
    import time
    import multiprocessing as mp

    candidates: list[tuple[int, dict, float]] = []
    remaining = deadline - time.time()
    pre_state["timelimit"] = max(0.0, remaining)
    pre_state["t_start"] = time.time()

    try:
        main_seed, main_sol, main_obj = _tiered_phase3_worker(pre_state, seed=seed_base)
        if main_sol is not None:
            candidates.append((main_seed, main_sol, main_obj))
    except Exception as exc:
        print(f"[algorithm] tiered round: main chain (seed={seed_base}) raised "
              f"{type(exc).__name__}: {exc}")

    if n_workers <= 0 or remaining < _PARALLEL_MIN_BUDGET:
        return candidates

    available = mp.get_all_start_methods()
    start_method = "fork" if "fork" in available else "spawn"

    procs = []
    result_queue = None
    try:
        ctx = mp.get_context(start_method)
        result_queue = ctx.Queue()
        for i in range(n_workers):
            worker_seed = seed_base + 1 + i
            p = ctx.Process(
                target=_tiered_phase3_entry,
                args=(result_queue, pre_state, worker_seed),
                daemon=True,
            )
            p.start()
            procs.append(p)
    except Exception as exc:
        print(f"[algorithm] tiered round: worker spawn failed ({type(exc).__name__}: {exc})")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        return candidates

    collect_grace = min(_PARALLEL_COLLECT_GRACE, remaining * 0.1)
    collect_deadline = deadline + collect_grace
    n_collected = 0
    try:
        while n_collected < len(procs):
            remaining_wait = collect_deadline - time.time()
            if remaining_wait <= 0:
                print(f"[algorithm] tiered round: collection deadline hit with "
                      f"{len(procs) - n_collected} worker(s) still outstanding")
                break
            try:
                w_seed, w_sol, w_obj = result_queue.get(timeout=remaining_wait)
            except Exception:
                print(f"[algorithm] tiered round: collection timed out with "
                      f"{len(procs) - n_collected} worker(s) still outstanding")
                break
            n_collected += 1
            if w_sol is None:
                print(f"[algorithm] tiered round: worker (seed={w_seed}) infeasible/failed -- discarded")
                continue
            candidates.append((w_seed, w_sol, w_obj))
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=2.0)
        if result_queue is not None:
            try:
                result_queue.close()
                result_queue.join_thread()
            except Exception:
                pass

    return candidates


def _iterated_greedy_tiered(prob_info, baseline_greedy, check_feasibility, deadline: float):
    """
    Tiered parallel restart: one shared EDD/seed=0 construction (Phase 1
    through Repair/2.5/2.6, run once in this process), then Phase 3
    (_improve) fanned out across most of the remaining cores from that same
    base -- EXCEPT one core, reserved for an independent alt-construction
    restart loop (_alt_restart_entry) that explores different seeds/rules
    from scratch, since the shared base's own seed/rule choice can itself
    be a bad basin _improve can never fully escape. See this section's
    module-level comment (above _alt_restart_entry) for the full rationale.

    Falls back to _iterated_greedy_parallel whenever: there isn't enough
    budget to bother, only one usable core is available, the shared
    construction itself raises or doesn't reach a feasible base, or
    process-pool creation fails. Returns (best_solution_or_None,
    best_objective_or_None, n_attempts) -- identical contract to
    _iterated_greedy/_iterated_greedy_parallel, so _solve() needs no other
    changes beyond calling this instead.
    """
    import time
    import multiprocessing as mp

    def _fallback(alt_proc=None, alt_queue=None):
        # alt_proc/alt_queue are only ever non-None after Step 0 below has
        # already dispatched the alternate-construction attempt -- clean it
        # up here too so no path through this function can ever leave it
        # running past return, matching every other process-cleanup
        # discipline in this module.
        if alt_proc is not None:
            try:
                if alt_proc.is_alive():
                    alt_proc.terminate()
                alt_proc.join(timeout=2.0)
            except Exception:
                pass
        if alt_queue is not None:
            try:
                alt_queue.close()
                alt_queue.join_thread()
            except Exception:
                pass
        return _iterated_greedy_parallel(prob_info, baseline_greedy, check_feasibility, deadline)

    if mp.current_process().name != "MainProcess":
        return _fallback()

    remaining = deadline - time.time()
    if remaining < _PARALLEL_MIN_BUDGET:
        return _fallback()

    n_cores = _os.cpu_count() or 1
    n_slots = max(1, min(_PARALLEL_MAX_WORKERS, n_cores))
    if n_slots <= 1:
        return _fallback()

    # -- Step 0: dispatch the independent alternate-construction attempt
    # CONCURRENTLY with the shared construction below -- see the module-
    # level comment above _alt_restart_entry for the restart-loop's own
    # rationale, and the comment above _TIERED_ALT_W3_SHARE_THRESHOLD for
    # why the STRATEGY itself is picked per-instance from the weight
    # profile (edd-focused restart loop for w1-dominant instances, a
    # single area_slack attempt for w3-elevated ones). Passed this
    # function's own absolute `deadline` directly in the restart-loop case
    # (see _alt_restart_entry's own docstring for why no spawn-overhead
    # buffer is needed there); the single-attempt case still needs one,
    # matching _parallel_attempt_worker's usage elsewhere. Never blocks: if
    # spawn fails, alt_proc/alt_queue stay None and everything below
    # degrades to exactly the pre-fix behaviour (all n_slots-1 remaining
    # slots go to tiered/island refinement).
    alt_proc = None
    alt_queue = None
    weights = prob_info.get("weights", {})
    w1_plus = weights.get("w1", 0) + weights.get("w2", 0) + weights.get("w3", 0)
    w3_share = (weights.get("w3", 0) / w1_plus) if w1_plus > 0 else 0.0
    alt_mode = "restart_loop" if w3_share < _TIERED_ALT_W3_SHARE_THRESHOLD else "single_attempt"
    try:
        alt_available = mp.get_all_start_methods()
        alt_start_method = "fork" if "fork" in alt_available else "spawn"
        alt_ctx = mp.get_context(alt_start_method)
        alt_queue = alt_ctx.Queue()
        if alt_mode == "restart_loop":
            alt_proc = alt_ctx.Process(
                target=_alt_restart_entry,
                args=(alt_queue, prob_info, deadline),
                daemon=True,
            )
        else:
            alt_worker_timelimit = max(1.0, remaining - _PARALLEL_SPAWN_OVERHEAD_BUFFER)
            alt_proc = alt_ctx.Process(
                target=_parallel_attempt_entry,
                args=(alt_queue, prob_info, alt_worker_timelimit,
                      _TIERED_ALT_SINGLE_SEED, _TIERED_ALT_SINGLE_RULE),
                daemon=True,
            )
        alt_proc.start()
        print(f"[algorithm] tiered: alt slot mode={alt_mode} (w3_share={w3_share:.2%}, "
              f"threshold={_TIERED_ALT_W3_SHARE_THRESHOLD:.0%})")
    except Exception as exc:
        print(f"[algorithm] tiered: alternate-construction spawn failed "
              f"({type(exc).__name__}: {exc}) -- proceeding without it")
        alt_proc = None
        alt_queue = None

    # -- Step 1: shared construction, ONCE, in this (main) process. --------
    try:
        pre_state = baseline_greedy.greedyalgorithm(
            prob_info, timelimit=remaining, seed=0, priority_rule="edd",
            return_pre_improve_state=True,
        )
    except Exception as exc:
        print(f"[algorithm] tiered: shared construction raised {type(exc).__name__}: "
              f"{exc} -- falling back to N-independent-pipeline parallel restart")
        return _fallback(alt_proc, alt_queue)

    pre_state["prob_info"] = prob_info
    base_result = pre_state.get("known_result")
    if base_result is None:
        # known_result (last_verified_result inside greedyalgorithm) can be
        # None even when assignments IS actually feasible -- e.g. _repair's
        # own final force-place guarantee mutated assignments after its
        # last verified check, deliberately without re-verifying (see
        # _repair's own comment on why: that recheck would cost as much as
        # the guarantee itself, ~19s on a large instance, for confirmation
        # Phase 2.5/2.6/3 would give for free moments later anyway).
        # Exactly this force-place path fires more often on large/congested
        # instances -- precisely the ones this whole tiered feature exists
        # to help -- so treating "unverified" as "infeasible" here would
        # make it fall back to the less-efficient N-independent path
        # disproportionately often on its own best-case instances. One
        # fresh check costs the same as _improve's own internal fallback
        # would have paid anyway (known_result=None there triggers the
        # identical recompute) -- see _improve's own known_result docstring.
        base_sol = {"operations": baseline_greedy._build_operations(
            list(pre_state["assignments"].values()))}
        base_result = check_feasibility(prob_info, base_sol)
        pre_state["known_result"] = base_result
    if not base_result.get("feasible"):
        print(f"[algorithm] tiered: shared construction did not reach a feasible base "
              f"-- falling back to N-independent-pipeline parallel restart")
        return _fallback(alt_proc, alt_queue)

    base_sol = {"operations": baseline_greedy._build_operations(
        list(pre_state["assignments"].values()))}
    global_best_seed: int = 0
    global_best_sol: dict = base_sol
    global_best_obj: float = base_result["objective"]
    n_attempts_total = 1  # the shared base itself always counts as one

    remaining_after_construct = deadline - time.time()
    # additional spawned workers beyond the main process's own chain each
    # round -- one slot is already reserved for the alternate-construction
    # attempt dispatched in Step 0 above (if it's actually running).
    n_reserved_for_alt = 1 if alt_proc is not None else 0
    n_workers = max(0, n_slots - 1 - n_reserved_for_alt)

    # Island-model migration (see the module-level comment above
    # _tiered_phase3_round) only when there's enough total budget that
    # EVERY segment would still clear _ISLAND_MIN_SEGMENT_BUDGET --
    # otherwise n_segments=1 runs exactly one round covering the whole
    # remaining budget, byte-for-byte the same as this feature's
    # pre-migration behaviour (including the "always try the local chain
    # even with almost no time left" safety property, since a single round
    # with n_workers possibly still >0 but remaining < _PARALLEL_MIN_BUDGET
    # already degrades to local-chain-only inside _tiered_phase3_round).
    if n_workers > 0 and remaining_after_construct / _ISLAND_N_SEGMENTS >= _ISLAND_MIN_SEGMENT_BUDGET:
        n_segments = _ISLAND_N_SEGMENTS
    else:
        n_segments = 1

    available = mp.get_all_start_methods()
    start_method = "fork" if "fork" in available else "spawn"
    print(f"[algorithm] tiered: shared EDD construction done "
          f"({remaining - remaining_after_construct:.1f}s), {n_workers} additional worker(s) "
          f"per round x {n_segments} round(s) (start_method={start_method}, "
          f"{n_cores} core(s) detected), {remaining_after_construct:.1f}s total Phase-3 budget")

    for segment_idx in range(n_segments):
        segment_now = time.time()
        if segment_now >= deadline:
            break
        # Split whatever's ACTUALLY left (not a pre-computed fixed share)
        # evenly across the REMAINING segments -- self-correcting if an
        # earlier segment ran short or long, same spirit as
        # _PARALLEL_SPAWN_OVERHEAD_BUFFER's "measure, don't assume" pattern.
        segments_left = n_segments - segment_idx
        segment_deadline = deadline if segments_left <= 1 else \
            segment_now + (deadline - segment_now) / segments_left

        # Migrate: every round starts from the GLOBAL best found so far
        # (full migration, not each worker's own previous result), so a
        # worker that drew an unlucky ALNS sequence last round gets a fresh
        # shot from the swarm's actual best instead of continuing to dig
        # wherever it ended up. pre_state is this function's own local
        # dict (built once from greedyalgorithm's return_pre_improve_state,
        # never shared with any other caller) -- mutating its
        # assignments/known_result between rounds is exactly the mechanism
        # that propagates migration into the next round's dispatch.
        if segment_idx > 0:
            pre_state["assignments"] = _assignments_from_solution(global_best_sol)
            # known_result=None (not a fabricated dict) is deliberate --
            # _improve() needs feasible/objective/obj1 (for its own
            # z1_lower_bound gating) and possibly more later; None makes it
            # recompute check_feasibility fresh internally (its own
            # documented fallback for "unknown starting state"), which is
            # the same cost check_feasibility_incremental's own shadow-
            # validated contract already accepts elsewhere in this
            # codebase -- correct and safe, just not free, unlike trying to
            # hand-construct a partial result dict that could KeyError or
            # silently carry a stale/wrong obj1.
            pre_state["known_result"] = None

        round_candidates = _tiered_phase3_round(
            pre_state, n_workers, segment_deadline, seed_base=1000 * segment_idx,
        )
        n_attempts_total += len(round_candidates)
        for cand_seed, cand_sol, cand_obj in round_candidates:
            if cand_obj < global_best_obj - 1e-6:
                global_best_seed, global_best_sol, global_best_obj = cand_seed, cand_sol, cand_obj
        print(f"[algorithm] tiered: round {segment_idx + 1}/{n_segments} collected "
              f"{len(round_candidates)} candidate(s), global best so far: "
              f"seed={global_best_seed} objective={global_best_obj:.0f}")

    print(f"[algorithm] tiered: {n_attempts_total} Phase-3 attempt(s) total across "
          f"{n_segments} round(s), best from seed={global_best_seed} objective={global_best_obj:.0f}")

    # -- Step N: collect the alternate-construction attempt dispatched back
    # in Step 0 (whichever alt_mode was chosen) -- it has been running
    # concurrently this whole time against the SAME absolute deadline this
    # whole function got, so it should already be at or near done. Same
    # proportional-with-a-ceiling grace as every other collection point in
    # this module (see _PARALLEL_COLLECT_GRACE's own docstring) -- bounds
    # worst-case added wall-clock even if it's somehow still running.
    #
    # The two alt_mode targets return different tuple shapes on the queue
    # (_alt_restart_entry: (solution, objective, n_attempts);
    # _parallel_attempt_entry: (seed, priority_rule, solution, objective),
    # matching _iterated_greedy_parallel's own usage) -- normalized to a
    # common (alt_sol, alt_obj, alt_n_attempts, alt_label) shape here so the
    # merge/print logic below doesn't need to care which one ran.
    if alt_proc is not None:
        alt_grace = min(_PARALLEL_COLLECT_GRACE, remaining * 0.1)
        alt_wait = max(0.0, (deadline + alt_grace) - time.time())
        try:
            if alt_mode == "restart_loop":
                alt_sol, alt_obj, alt_n_attempts = alt_queue.get(timeout=alt_wait)
                alt_label = "alt restart-loop"
            else:
                alt_seed, alt_rule, alt_sol, alt_obj = alt_queue.get(timeout=alt_wait)
                alt_n_attempts = 1
                alt_label = f"alt single-attempt (seed={alt_seed}, rule={alt_rule})"
            n_attempts_total += alt_n_attempts
            if alt_sol is None:
                print(f"[algorithm] tiered: {alt_label} ({alt_n_attempts} attempt(s)) "
                      f"found nothing feasible -- discarded")
            elif alt_obj < global_best_obj - 1e-6:
                print(f"[algorithm] tiered: {alt_label} ({alt_n_attempts} attempt(s)) "
                      f"objective={alt_obj:.0f} -- NEW global best (was {global_best_obj:.0f})")
                global_best_sol, global_best_obj = alt_sol, alt_obj
            else:
                print(f"[algorithm] tiered: {alt_label} ({alt_n_attempts} attempt(s)) "
                      f"objective={alt_obj:.0f} (global best stays {global_best_obj:.0f})")
        except Exception:
            print(f"[algorithm] tiered: alt slot collection timed out -- discarded")
        finally:
            try:
                if alt_proc.is_alive():
                    alt_proc.terminate()
                alt_proc.join(timeout=2.0)
            except Exception:
                pass
            try:
                alt_queue.close()
                alt_queue.join_thread()
            except Exception:
                pass

    return global_best_sol, global_best_obj, n_attempts_total


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
