"""
Compute a cheap, valid theoretical lower bound on Z1 (total tardiness) for
one or more instances, ignoring every spatial/crane/bay-capacity constraint.

For each block, the best case is starting exactly at release_time and
finishing in exactly processing_time with zero waiting for bay space or
crane conflicts -- no algorithm can ever do better than that for a single
block in isolation, so summing this per-block floor over all blocks gives a
valid (if loose) lower bound on the achievable Z1 for the whole instance:

    Z1_lower_bound = sum(max(0, release_time + processing_time - due_date)
                          for each block)

This is O(n) per instance -- just JSON + arithmetic, no geometry/Xpress/
search of any kind -- so it's safe to run alongside other CPU-heavy work.

Use: distinguishing "this instance is just inherently tight/tardy no matter
what" from "our algorithm is leaving real Z1 improvement on the table" when
interpreting objective numbers (e.g. the ongoing 1st-vs-2nd submission
regression hunt in notes/algorithm_overview.md).

Usage:
    python analysis/lower_bound.py <instance.json | glob | dir> [...]
"""
import argparse
import glob as globmod
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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


def lower_bound_z1(prob_info: dict) -> tuple[float, int, int]:
    """Returns (Z1_lower_bound, n_blocks_with_positive_floor, n_blocks)."""
    total = 0.0
    n_tight = 0
    blocks = prob_info["blocks"]
    for b in blocks:
        floor = max(0.0, b["release_time"] + b["processing_time"] - b["due_date"])
        if floor > 0:
            n_tight += 1
        total += floor
    return total, n_tight, len(blocks)


def run(patterns: list[str]) -> None:
    files = _collect_instance_files(patterns)
    if not files:
        print(f"No instance files matched: {patterns}")
        sys.exit(1)

    print(f"Found {len(files)} instance file(s).")
    print(f"{'instance':16s} {'n_blocks':>8s} {'w1':>10s} "
          f"{'Z1_lower_bound':>15s} {'w1*LB':>15s} {'n_tight':>8s}")
    print("=" * 80)

    rows = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            prob_info = json.load(fh)
        lb, n_tight, n_blocks = lower_bound_z1(prob_info)
        w1 = prob_info.get("weights", {}).get("w1", 1.0)
        rows.append((f.name, n_blocks, w1, lb, n_tight))
        print(f"{f.name:16s} {n_blocks:8d} {w1:10.0f} "
              f"{lb:15,.0f} {w1 * lb:15,.0f} {n_tight:8d}")

    print("=" * 80)
    n_zero = sum(1 for r in rows if r[3] == 0)
    print(f"{n_zero}/{len(rows)} instances have Z1_lower_bound == 0 "
          f"(zero tardiness is theoretically achievable -- any observed Z1 > 0 "
          f"there is purely a placement/scheduling inefficiency, not an "
          f"inherent property of the instance).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patterns", nargs="+",
                        help="instance JSON file(s), glob pattern(s), or directory(ies)")
    args = parser.parse_args()
    run(args.patterns)
