"""
Compare two separate code snapshots (e.g. the actually-submitted zip vs the
current working tree) on the same instances, to measure whether changes made
since submission (blocking-chain-aware repair, Z2/Z3-aware Phase 3 selection,
etc.) actually improve the objective -- not just "doesn't crash".

Each version is run in its own subprocess with sys.path pointed at its own
directory, so the two versions' same-named modules (myalgorithm,
baseline_greedy, xpress_reinsert) never collide via Python's module cache
the way they would in a single process.

2026-07-24 (--repeats, user-proposed "option A" after two single-trial A/Bs
this session gave CONTRADICTORY verdicts on the same question -- see
notes/algorithm_overview.md's parallel-restart worker-count entries): this
codebase is wall-clock-adaptive by design, so a single (before, after) pair
per instance can be dominated by run-to-run noise rather than a real
difference. --repeats N (default 1, unchanged single-trial behaviour) runs
each (instance, version) cell N times and reports median/spread via
_stats_compare.py instead of a bare point comparison.

Usage:
    python analysis/before_after_compare.py \
        --before <dir_with_myalgorithm.py> --after <dir_with_myalgorithm.py> \
        <instance.json | glob | dir> [...] [--timelimit 60] [--repeats 3]
"""
import argparse
import glob as globmod
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _stats_compare import compare_verdict, format_summary, print_summary_table  # noqa: E402

RUNNER = r"""
import sys, json
sys.path.insert(0, {code_dir!r})
import myalgorithm
from utils import check_feasibility

with open({instance_path!r}, encoding="utf-8") as f:
    prob_info = json.load(f)

sol = myalgorithm.algorithm(prob_info, timelimit={timelimit})
result = check_feasibility(prob_info, sol)
print("RESULT_JSON:" + json.dumps({{
    "feasible": result["feasible"],
    "objective": result.get("objective"),
}}))
"""


def _collect_instance_files(patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        p = Path(pattern)
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")))
        else:
            files.extend(sorted(Path(m) for m in globmod.glob(pattern)))
    seen = set()
    unique = []
    for f in files:
        rp = f.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(f)
    return unique


def _run_one(python_exe: str, code_dir: str, instance_path: str, timelimit: float) -> dict:
    script = RUNNER.format(code_dir=code_dir, instance_path=instance_path, timelimit=timelimit)
    proc = subprocess.run([python_exe, "-c", script], capture_output=True, text=True,
                          timeout=timelimit + 60)
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT_JSON:"):
            return json.loads(line[len("RESULT_JSON:"):])
    return {"feasible": False, "objective": None, "error": proc.stderr[-2000:]}


def run(before_dir: str, after_dir: str, patterns: list[str], timelimit: float,
       repeats: int = 1) -> None:
    files = _collect_instance_files(patterns)
    if not files:
        print(f"No instance files matched: {patterns}")
        sys.exit(1)

    python_exe = sys.executable
    print(f"Found {len(files)} instance file(s). timelimit={timelimit}s per (instance, version). "
          f"repeats={repeats}")
    print(f"before = {before_dir}")
    print(f"after  = {after_dir}")
    print("=" * 100)

    if repeats <= 1:
        # Unchanged single-trial path (exact pre-2026-07-24 behaviour) --
        # no behaviour change for any existing caller that doesn't pass
        # --repeats.
        before_wins = after_wins = ties = 0
        for f in files:
            t0 = time.time()
            before = _run_one(python_exe, before_dir, str(f), timelimit)
            t1 = time.time()
            after = _run_one(python_exe, after_dir, str(f), timelimit)
            t2 = time.time()

            b_ok, b_obj = before["feasible"], before.get("objective")
            a_ok, a_obj = after["feasible"], after.get("objective")

            b_str = f"{b_obj:,.0f}" if b_ok and b_obj is not None else "INFEASIBLE"
            a_str = f"{a_obj:,.0f}" if a_ok and a_obj is not None else "INFEASIBLE"

            tag = ""
            if b_ok and a_ok and b_obj is not None and a_obj is not None:
                if a_obj < b_obj - 1e-6:
                    after_wins += 1
                    tag = "  <- after better"
                elif b_obj < a_obj - 1e-6:
                    before_wins += 1
                    tag = "  <- BEFORE better (regression!)"
                else:
                    ties += 1
                    tag = "  (tie)"
            print(f"{f.name:16s}  before={b_str:>16s} ({t1-t0:5.1f}s)  "
                  f"after={a_str:>16s} ({t2-t1:5.1f}s){tag}")

        print("=" * 100)
        print(f"after better: {after_wins}   before better (regression): {before_wins}   tie: {ties}")
        return

    # Repeated-trial path: N runs per (instance, version), verdict by
    # median + range-overlap check (see _stats_compare.compare_verdict).
    before_wins = after_wins = ties = 0
    for f in files:
        print(f"\n{f.name}")
        before_objs: list = []
        after_objs: list = []
        for rep in range(repeats):
            t0 = time.time()
            before = _run_one(python_exe, before_dir, str(f), timelimit)
            t1 = time.time()
            after = _run_one(python_exe, after_dir, str(f), timelimit)
            t2 = time.time()
            b_obj = before.get("objective") if before["feasible"] else None
            a_obj = after.get("objective") if after["feasible"] else None
            before_objs.append(b_obj)
            after_objs.append(a_obj)
            print(f"  rep {rep+1}/{repeats}: before={b_obj!r} ({t1-t0:5.1f}s)  "
                  f"after={a_obj!r} ({t2-t1:5.1f}s)")

        print_summary_table("before", before_objs, "after", after_objs)
        verdict = compare_verdict("before", before_objs, "after", after_objs)
        if verdict.startswith("after"):
            after_wins += 1
        elif verdict.startswith("before"):
            before_wins += 1
        else:
            ties += 1

    print("=" * 100)
    print(f"By-instance verdict: after better={after_wins}   "
          f"before better (regression)={before_wins}   tie/inconclusive={ties}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patterns", nargs="+",
                        help="instance JSON file(s), glob pattern(s), or directory(ies)")
    parser.add_argument("--before", required=True, help="directory containing the 'before' myalgorithm.py etc.")
    parser.add_argument("--after", required=True, help="directory containing the 'after' myalgorithm.py etc.")
    parser.add_argument("--timelimit", type=float, default=60.0,
                        help="wall-clock time limit per (instance, version) in seconds (default: %(default)s)")
    parser.add_argument("--repeats", type=int, default=1,
                        help="repeat each (instance, version) cell this many times and compare "
                             "by median/spread instead of a single point (default: %(default)s, "
                             "i.e. unchanged single-trial behaviour)")
    args = parser.parse_args()
    run(args.before, args.after, args.patterns, args.timelimit, repeats=args.repeats)
