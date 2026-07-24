"""
_stats_compare.py -- shared repeated-trial statistics helpers for this
repo's analysis/ comparison scripts (before_after_compare.py, repeat_check.py,
and any ad-hoc A/B script).

Built 2026-07-24 after single-trial A/Bs gave CONTRADICTORY verdicts twice
in one session (4-worker-vs-sequential and 3-worker-vs-sequential
parallel-restart comparisons landed on opposite conclusions -- see
notes/algorithm_overview.md's parallel-restart entries): this whole
codebase is wall-clock-adaptive by design, so a single trial per condition
is not enough to tell "this change genuinely helps" apart from "this run
happened to get lucky/unlucky". Comparisons should report a distribution
(repeated trials) with a verdict that accounts for spread, not a bare
point comparison.

Deliberately pure-stdlib (statistics module only, no numpy/scipy) -- this
is a small internal tool, not worth adding a dependency-availability risk
for.

Usage pattern (see before_after_compare.py's --repeats flag for a full
example):
    values_a = [run_condition_a() for _ in range(n_repeats)]
    values_b = [run_condition_b() for _ in range(n_repeats)]
    print_summary_table("A", values_a, "B", values_b)
"""
import statistics as _st


def summarize(values: list) -> dict:
    """
    min/max/median/mean/stdev/n for a list of objective values from
    repeated trials. `values` may contain None entries (infeasible/failed
    trials) -- these are excluded from the statistics but counted in `n`
    so the caller can see the feasible/total ratio.

    Returns n_feasible=0 (all stat fields None) if every trial failed --
    callers must check this before doing arithmetic on the result.
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": len(values), "n_feasible": 0, "median": None, "mean": None,
                "min": None, "max": None, "stdev": None}
    return {
        "n": len(values),
        "n_feasible": len(vals),
        "median": _st.median(vals),
        "mean": _st.mean(vals),
        "min": min(vals),
        "max": max(vals),
        "stdev": _st.stdev(vals) if len(vals) > 1 else 0.0,
    }


def compare_verdict(name_a: str, values_a: list, name_b: str, values_b: list) -> str:
    """
    Compare two repeated-trial result sets by MEDIAN (robust to a single
    outlier run dominating the comparison, unlike mean or a single-trial
    point comparison). Flags when the two conditions' [min, max] ranges
    overlap -- overlapping ranges mean a single additional trial from
    either side could plausibly have landed past the other's result, so
    the verdict is reported as low-confidence in that case rather than a
    flat "X wins". This is a lightweight heuristic (not a formal
    significance test -- see module docstring for why no scipy), meant to
    stop an obviously-noise-dominated comparison from being read as a firm
    conclusion, not to replace real statistical rigor for a high-stakes
    decision.
    """
    sa, sb = summarize(values_a), summarize(values_b)
    if sa["n_feasible"] == 0 and sb["n_feasible"] == 0:
        return "both all-infeasible -- no comparison possible"
    if sa["n_feasible"] == 0:
        return f"{name_b} wins ({name_a} always infeasible)"
    if sb["n_feasible"] == 0:
        return f"{name_a} wins ({name_b} always infeasible)"

    overlap = sa["min"] <= sb["max"] and sb["min"] <= sa["max"]
    if sa["median"] < sb["median"] * 0.999:
        winner = name_a
    elif sb["median"] < sa["median"] * 0.999:
        winner = name_b
    else:
        return "tie (medians within noise)"
    confidence = ("LOW -- ranges overlap, could flip with more trials" if overlap
                 else "higher -- ranges don't overlap")
    return f"{winner} better by median (confidence: {confidence})"


def format_summary(values: list) -> str:
    s = summarize(values)
    if s["n_feasible"] == 0:
        return f"ALL INFEASIBLE (0/{s['n']})"
    return (f"median={s['median']:,.0f}  mean={s['mean']:,.0f}  "
           f"[{s['min']:,.0f}, {s['max']:,.0f}]  stdev={s['stdev']:,.0f}  "
           f"(n={s['n_feasible']}/{s['n']} feasible)")


def print_summary_table(name_a: str, values_a: list, name_b: str, values_b: list) -> None:
    width = max(len(name_a), len(name_b), 10)
    print(f"  {name_a:{width}s}  {format_summary(values_a)}")
    print(f"  {name_b:{width}s}  {format_summary(values_b)}")
    print(f"  -> {compare_verdict(name_a, values_a, name_b, values_b)}")
