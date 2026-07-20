"""
Run the SAME code snapshot on the SAME instance N times in a row, to measure
run-to-run variance (e.g. to tell apart "real regression" from "this
snapshot doesn't fix its RNG seed, so results wander").

Each run is its own subprocess with sys.path pointed at code_dir, matching
before_after_compare.py's isolation pattern.

Usage:
    python analysis/repeat_check.py --dir <dir_with_myalgorithm.py> \
        <instance.json> --timelimit 180 --repeats 5
"""
import argparse
import json
import subprocess
import sys
import time

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


def _run_one(python_exe: str, code_dir: str, instance_path: str, timelimit: float) -> dict:
    script = RUNNER.format(code_dir=code_dir, instance_path=instance_path, timelimit=timelimit)
    proc = subprocess.run([python_exe, "-c", script], capture_output=True, text=True,
                          timeout=timelimit + 60)
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT_JSON:"):
            return json.loads(line[len("RESULT_JSON:"):])
    return {"feasible": False, "objective": None, "error": proc.stderr[-2000:]}


def run(code_dir: str, instance_path: str, timelimit: float, repeats: int) -> None:
    python_exe = sys.executable
    print(f"dir={code_dir}  instance={instance_path}  timelimit={timelimit}s  repeats={repeats}")
    objs = []
    for i in range(repeats):
        t0 = time.time()
        r = _run_one(python_exe, code_dir, instance_path, timelimit)
        elapsed = time.time() - t0
        ok, obj = r["feasible"], r.get("objective")
        if ok and obj is not None:
            objs.append(obj)
            print(f"  run {i+1}/{repeats}: objective={obj:,.0f}  ({elapsed:.1f}s)")
        else:
            print(f"  run {i+1}/{repeats}: INFEASIBLE/ERROR  ({elapsed:.1f}s)  {r.get('error', '')[:200]}")
    if objs:
        print(f"  -> min={min(objs):,.0f}  max={max(objs):,.0f}  "
              f"range={max(objs)-min(objs):,.0f}  ({len(objs)}/{repeats} feasible)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("instance", help="instance JSON file")
    parser.add_argument("--dir", required=True, help="directory containing myalgorithm.py etc.")
    parser.add_argument("--timelimit", type=float, default=60.0)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    run(args.dir, args.instance, args.timelimit, args.repeats)
