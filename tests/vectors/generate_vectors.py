"""
Test vector generator for cross-language CRDT verification (Python <-> JavaScript).

Each scenario is a reproducible multi-site editing session (run inserts, span
deletes, partial gossip). The Python implementation computes the expected final
node order and snapshot; tests/vectors/test_rga_js.mjs replays the same ops through
static/js/rga.js and must match exactly. `legacy_*.json` hold ops in the pre-RLE
single-character format, which both implementations must still read.

    python -m tests.vectors.generate_vectors
"""

import json
import random
from pathlib import Path
from typing import Any

from crdt.ops import Op
from crdt.rebase import rebase_pending
from crdt.rga import RGA

VECTORS_DIR = Path(__file__).resolve().parent
# Includes astral-plane characters: the unit is the code point in both languages.
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_é😀🚀"


def node_order(rga: RGA) -> list[list[Any]]:
    return [[str(n.char_id), n.char, n.deleted] for n in rga.iter_nodes()]


def generate_scenario(name: str, num_sites: int, num_ops: int, seed: int) -> dict[str, Any]:
    """Generate a reproducible multi-site editing scenario."""
    rng = random.Random(seed)
    sites = [RGA(site_id=f"site_{i}") for i in range(num_sites)]
    all_ops: list[Op] = []

    for _ in range(num_ops):
        site = rng.choice(sites)
        v_len = site.visible_len()
        if v_len > 0 and rng.random() < 0.3:
            pos = rng.randint(0, v_len - 1)
            op = site.local_delete(pos, rng.randint(1, min(4, v_len - pos)))
        else:
            text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 5)))
            op = site.local_insert(rng.randint(0, v_len), text)
        all_ops.append(op)

        # Gossip op to random subset of peers
        for peer in rng.sample(sites, k=rng.randint(1, num_sites)):
            peer.apply(op)

    canonical = RGA(site_id="canonical")
    for op in all_ops:
        canonical.apply(op)

    return {
        "name": name,
        "seed": seed,
        "num_sites": num_sites,
        "operations": [op.to_dict() for op in all_ops],
        "expected_text": canonical.text(),
        "expected_len": canonical.visible_len(),
        "expected_order": node_order(canonical),
        "expected_runs": canonical.to_dict()["runs"],
        "expected_vv": canonical.version_vector,
    }


def generate_rebase_scenario(seed: int) -> dict[str, Any]:
    """A stale client's pending ops replayed onto a compacted server state."""
    rng = random.Random(seed)
    server, alice, bob = RGA("server"), RGA("alice"), RGA("bob")
    shared = [alice.local_insert(0, "the quick brown fox jumps")]
    for op in shared:
        server.apply(op)
        bob.apply(op)

    pending: list[Op] = []
    for _ in range(6):
        length = bob.visible_len()
        if length and rng.random() < 0.3:
            pos = rng.randint(0, length - 1)
            pending.append(bob.local_delete(pos, rng.randint(1, min(3, length - pos))))
        else:
            pending.append(bob.local_insert(rng.randint(0, length), rng.choice(["!", "XY", "😀"])))

    server_ops = [alice.local_delete(4, 12), alice.local_insert(4, "lazy ")]
    for op in server_ops:
        server.apply(op)
    server.compact()

    fresh = RGA.from_dict(server.to_dict(), site_id="bob-2")
    rebase_pending(bob, fresh, pending)
    return {
        "name": "rebase_after_gc",
        "kind": "rebase",
        "shared": [op.to_dict() for op in shared],
        "pending": [op.to_dict() for op in pending],
        "server_snapshot": server.to_dict(),
        "old_site": "bob",
        "old_clock": bob.clock.value,
        "new_site": "bob-2",
        "expected_text": fresh.text(),
        "expected_order": node_order(fresh),
    }


def main() -> None:
    scenarios = [
        generate_scenario("simple_insert_delete", num_sites=2, num_ops=20, seed=101),
        generate_scenario("concurrent_three_sites", num_sites=3, num_ops=60, seed=202),
        generate_scenario("high_contention_five_sites", num_sites=5, num_ops=150, seed=303),
        generate_rebase_scenario(seed=404),
    ]
    for scenario in scenarios:
        file_path = VECTORS_DIR / f"{scenario['name']}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(scenario, f, indent=1, ensure_ascii=False)
        print(f"Generated test vector: {file_path}")


if __name__ == "__main__":
    main()
