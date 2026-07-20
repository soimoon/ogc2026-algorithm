"""
Quantify how irregular block shapes are, relative to their axis-aligned
bounding boxes, for a given problem instance.

Motivation: baseline_greedy.py's _candidate_positions() only generates
candidate reference points from bounding-rect touch points (bay walls and
already-placed blocks' bounding_rect() edges) -- it never considers
positions where two irregular (non-convex) polygons could interlock while
their bounding boxes still overlap. This script checks how much headroom
that leaves on the table: if polygon_area / bbox_area is close to 1 for
most blocks, they're near-rectangular and NFP-style candidate generation
wouldn't help much. If it's well below 1, a meaningful amount of each
block's bounding box is empty space that a smarter placement could exploit.

Usage:
    python analysis/shape_irregularity.py <instance.json>
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import _resolve_layers, _bounding_box, _poly_from_verts


def analyze(instance_path: str) -> None:
    with open(instance_path, "r", encoding="utf-8") as f:
        instance = json.load(f)

    blocks = instance["blocks"]
    ratios = []  # (block_id, orient_idx, layer_idx, ratio, poly_area, bbox_area)

    for bi, blk in enumerate(blocks):
        for shape_entry in blk["shape"]:
            oi = shape_entry["orientation"]
            layers = _resolve_layers(shape_entry["layers"])
            for li, layer in enumerate(layers):
                poly = _poly_from_verts(layer)
                if poly is None:
                    continue
                poly_area = poly.area
                bbox = _bounding_box(layer)
                bbox_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                if bbox_area <= 0:
                    continue
                ratios.append((bi, oi, li, poly_area / bbox_area, poly_area, bbox_area))

    ratio_values = sorted(r[3] for r in ratios)
    n = len(ratio_values)

    print(f"Instance: {instance.get('name', instance_path)}")
    print(f"Total blocks: {len(blocks)}")
    print(f"Total (block, orientation, layer) samples: {n}")
    print(f"Average ratio (polygon area / bbox area): {sum(ratio_values) / n:.4f}")
    print(f"Median ratio: {ratio_values[n // 2]:.4f}")
    print(f"Min ratio: {ratio_values[0]:.4f}")
    print(f"Max ratio: {ratio_values[-1]:.4f}")

    buckets = {"0.9-1.0": 0, "0.8-0.9": 0, "0.7-0.8": 0, "0.6-0.7": 0, "0.5-0.6": 0, "<0.5": 0}
    for r in ratio_values:
        if r >= 0.9:
            buckets["0.9-1.0"] += 1
        elif r >= 0.8:
            buckets["0.8-0.9"] += 1
        elif r >= 0.7:
            buckets["0.7-0.8"] += 1
        elif r >= 0.6:
            buckets["0.6-0.7"] += 1
        elif r >= 0.5:
            buckets["0.5-0.6"] += 1
        else:
            buckets["<0.5"] += 1

    print("\nDistribution:")
    for k, v in buckets.items():
        print(f"  {k}: {v} ({100 * v / n:.1f}%)")

    print("\n10 most irregular (lowest ratio) samples: (block_id, orient_idx, layer_idx, ratio, poly_area, bbox_area)")
    for bi, oi, li, ratio, pa, ba in sorted(ratios, key=lambda x: x[3])[:10]:
        print(f"  block {bi} orient {oi} layer {li}: ratio={ratio:.4f} "
              f"(poly_area={pa:.2f}, bbox_area={ba:.2f})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python analysis/shape_irregularity.py <instance.json>")
        sys.exit(1)
    analyze(sys.argv[1])
