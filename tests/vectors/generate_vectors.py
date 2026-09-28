"""
Test vector generator for cross-language CRDT verification (Python <-> JavaScript).

Generates complex multi-replica concurrent editing scenarios and dumps
them as structured JSON vectors for consumption by both pytest and node --test.
"""

import json
from pathlib import Path
import random
from typing import Any

from crdt.ops import Op
from crdt.rga import RGA


VECTORS_DIR = Path(__file__).resolve().parent


def generate_scenario(name: str, num_sites: int, num_ops: int, seed: int) -> dict[str, Any]:
    """Generate a reproducible multi-site editing scenario."""
    rng = random.Random(seed)
    sites = [RGA(site_id=f"site_{i}") for i in range(num_sites)]
    all_ops: list[Op] = []

    for _ in range(num_ops):
        site = rng.choice(sites)
        v_len = site.visible_len()

        if v_len > 0 and rng.random() < 0.25:
            pos = rng.randint(0, v_len - 1)
            op = site.local_delete(pos)
        else:
            pos = rng.randint(0, v_len)
            char = rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
            op = site.local_insert(pos, char)

        all_ops.append(op)

        # Gossip op to random subset of peers
        for peer in rng.sample(sites, k=rng.randint(1, num_sites)):
            peer.apply(op)

    # Deliver all remaining ops to reference replica
    canonical_replica = RGA(site_id="canonical")
    for op in all_ops:
        canonical_replica.apply(op)

    return {
        "name": name,
        "seed": seed,
        "num_sites": num_sites,
        "operations": [op.to_dict() for op in all_ops],
        "expected_text": canonical_replica.text(),
        "expected_len": canonical_replica.visible_len(),
        "expected_snapshot": canonical_replica.to_dict(),
    }


def main() -> None:
    VECTORS_DIR.mkdir(parents=True, exist_ok=True)

    scenarios = [
        generate_scenario("simple_insert_delete", num_sites=2, num_ops=20, seed=101),
        generate_scenario("concurrent_three_sites", num_sites=3, num_ops=50, seed=202),
        generate_scenario("high_contention_five_sites", num_sites=5, num_ops=100, seed=303),
    ]

    for scenario in scenarios:
        file_path = VECTORS_DIR / f"{scenario['name']}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(scenario, f, indent=2)
        print(f"Generated test vector: {file_path}")


if __name__ == "__main__":
    main()
