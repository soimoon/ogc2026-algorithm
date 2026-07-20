"""
Bay floor-space utilization over time, for a given solution -- built to
cheaply validate (or rule out) the NFP investment question before writing
any NFP code: if bays rarely get close to full, tighter packing can't be
the thing standing between the current algorithm and a better objective,
regardless of how much bounding-box waste shape_irregularity.py found.

Utilization at time t in bay j := (sum of layer-0 footprint area of every
block present in bay j at t) / (bay j's area). Layer 0 is used as the
footprint because it's the layer actually resting on the bay floor;
crane/collision feasibility is checked layer-by-layer already (see
utils.check_feasibility), but "how much of the floor is covered" is a
layer-0 question.

Runs the current algorithm (baseline_greedy.greedyalgorithm, via
myalgorithm's safety wrapper) once per instance to get a solution -- this
is a one-shot, moderate-timelimit run per instance, not a repeated sweep,
so it's cheap enough to not meaningfully contend with other background work.

Usage:
    python analysis/bay_utilization.py <instance.json | glob | dir> [...] [--timelimit 30]
"""
import argparse
import glob as globmod
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import myalgorithm
from utils import check_feasibility, _resolve_layers, _poly_from_verts


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


def _layer0_area(block_data: dict, orient_idx: int) -> float:
    shape_entry = block_data["shape"][orient_idx]
    layers = _resolve_layers(shape_entry["layers"])
    if not layers:
        return 0.0
    poly = _poly_from_verts(layers[0])
    return poly.area if poly is not None else 0.0


def analyze_instance(prob_info: dict, timelimit: float) -> None:
    name = prob_info.get("name", "?")
    bays_data = prob_info["bays"]
    blocks_data = prob_info["blocks"]
    n_bays = len(bays_data)

    sol = myalgorithm.algorithm(prob_info, timelimit=timelimit)
    result = check_feasibility(prob_info, sol)
    print(f"\n=== {name}  (bays={n_bays}, blocks={len(blocks_data)})  "
          f"feasible={result['feasible']} ===")
    if not result["feasible"]:
        print("  skipped: solution not feasible, utilization would be meaningless")
        return

    # Reconstruct each block's (bay_id, entry_time, exit_time, layer0 area)
    # by pairing ENTRY/EXIT ops per block_id.
    bay_area = [bd["width"] * bd["height"] for bd in bays_data]
    entry_of: dict[int, tuple] = {}  # block_id -> (bay_id, orient_idx, entry_t)
    intervals: list[tuple] = []  # (bay_id, entry_t, exit_t, area)
    for t_str in sorted(sol["operations"], key=lambda s: int(s)):
        t = int(t_str)
        ops = sol["operations"][t_str]
        for op in ops:
            bid = op["block_id"]
            if op["type"] == "ENTRY":
                entry_of[bid] = (op["bay_id"], op["orient_idx"], t)
            else:
                bay_id, orient_idx, entry_t = entry_of.pop(bid)
                area = _layer0_area(blocks_data[bid], orient_idx)
                intervals.append((bay_id, entry_t, t, area))

    for j in range(n_bays):
        bay_intervals = [iv for iv in intervals if iv[0] == j]
        if not bay_intervals:
            print(f"  bay{j} ({bays_data[j]['width']}x{bays_data[j]['height']}): "
                  f"never used")
            continue
        boundaries = sorted({iv[1] for iv in bay_intervals} | {iv[2] for iv in bay_intervals})
        peak = 0.0
        weighted_sum = 0.0
        total_span = 0.0
        for a, b in zip(boundaries, boundaries[1:]):
            mid_area = sum(area for (_, e, x, area) in bay_intervals if e <= a < x)
            util = mid_area / bay_area[j] if bay_area[j] else 0.0
            peak = max(peak, util)
            weighted_sum += util * (b - a)
            total_span += (b - a)
        avg = weighted_sum / total_span if total_span else 0.0
        print(f"  bay{j} ({bays_data[j]['width']}x{bays_data[j]['height']}): "
              f"peak={peak:.1%}  time-avg={avg:.1%}  "
              f"({len(bay_intervals)} blocks, span={total_span:.0f})")


def run(patterns: list[str], timelimit: float) -> None:
    files = _collect_instance_files(patterns)
    if not files:
        print(f"No instance files matched: {patterns}")
        sys.exit(1)
    print(f"Found {len(files)} instance file(s). timelimit={timelimit}s per instance.")
    for f in files:
        with open(f, encoding="utf-8") as fh:
            prob_info = json.load(fh)
        analyze_instance(prob_info, timelimit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patterns", nargs="+",
                        help="instance JSON file(s), glob pattern(s), or directory(ies)")
    parser.add_argument("--timelimit", type=float, default=30.0,
                        help="wall-clock time limit per instance in seconds (default: %(default)s)")
    args = parser.parse_args()
    run(args.patterns, args.timelimit)
