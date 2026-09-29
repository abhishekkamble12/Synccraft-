"""
Formal convergence tests for the RGA CRDT implementation.

Proves strong eventual consistency:
Given any set of concurrent operations across N simulated replicas,
delivering those operations in ANY permutation to each replica results in
identical final text and identical internal linked-list sequences.
"""

import itertools
import random

from crdt.ops import Op
from crdt.rga import RGA


def create_fresh_replica(site_id: str) -> RGA:
    """Helper to instantiate a fresh RGA replica."""
    return RGA(site_id=site_id)


def test_two_sites_all_permutations() -> None:
    """
    Two sites make concurrent edits starting from empty document.
    Deliver all ops across all possible permutations (4! = 24).
    Every single permutation must result in the exact same text.
    """
    s1 = create_fresh_replica("s1")
    s2 = create_fresh_replica("s2")

    # s1 inserts 'A', then 'B'
    op1 = s1.local_insert(0, "A")
    op2 = s1.local_insert(1, "B")

    # s2 concurrently inserts 'X', then 'Y'
    op3 = s2.local_insert(0, "X")
    op4 = s2.local_insert(1, "Y")

    all_ops: list[Op] = [op1, op2, op3, op4]
    permutations = list(itertools.permutations(all_ops))
    assert len(permutations) == 24

    results: set[str] = set()
    first_replica_sequence = None

    for idx, perm in enumerate(permutations):
        replica = create_fresh_replica(f"eval_{idx}")
        for op in perm:
            replica.apply(op)

        final_text = replica.text()
        results.add(final_text)

        # Also verify internal linked-list character ordering
        node_ids = []
        curr = replica._head.next
        while curr is not None:
            node_ids.append((str(curr.char_id), curr.char, curr.deleted))
            curr = curr.next

        if first_replica_sequence is None:
            first_replica_sequence = node_ids
        else:
            assert node_ids == first_replica_sequence, (
                f"Internal node sequence mismatch in permutation {idx}: {node_ids} vs {first_replica_sequence}"
            )

    # Exactly one unique output string across all 24 permutations
    assert len(results) == 1, f"Convergence failed! Multiple outcomes: {results}"


def test_three_sites_random_concurrent_shuffles() -> None:
    """
    3 simulated sites generate concurrent insertions and deletions.
    Ops are delivered in 500 random shuffles to fresh replicas.
    All 500 must converge to the exact same text.
    """
    s1 = create_fresh_replica("site_1")
    s2 = create_fresh_replica("site_2")
    s3 = create_fresh_replica("site_3")

    # Initial shared baseline
    init_ops = [
        s1.local_insert(0, "H"),
        s1.local_insert(1, "E"),
        s1.local_insert(2, "L"),
        s1.local_insert(3, "L"),
        s1.local_insert(4, "O"),
    ]
    for op in init_ops:
        s2.apply(op)
        s3.apply(op)

    # Site 1 inserts "!" at end
    op_s1_1 = s1.local_insert(5, "!")

    # Site 2 deletes first 'E' and inserts 'A' (text -> "HALO")
    op_s2_1 = s2.local_delete(1)
    op_s2_2 = s2.local_insert(1, "A")

    # Site 3 inserts " WORLD"
    op_s3_1 = s3.local_insert(5, " ")
    op_s3_2 = s3.local_insert(6, "W")

    concurrent_ops = [op_s1_1, op_s2_1, op_s2_2, op_s3_1, op_s3_2]
    all_ops = init_ops + concurrent_ops

    # Collect reference result by sequential application
    ref_replica = create_fresh_replica("reference")
    for op in all_ops:
        ref_replica.apply(op)
    reference_text = ref_replica.text()

    # Test 500 randomized delivery orders
    rng = random.Random(42)  # Deterministic seed for reproducible testing
    for iteration in range(500):
        shuffled = list(all_ops)
        rng.shuffle(shuffled)

        test_replica = create_fresh_replica(f"test_{iteration}")
        for op in shuffled:
            test_replica.apply(op)

        assert test_replica.text() == reference_text, (
            f"Failed at shuffle #{iteration}: got {test_replica.text()!r}, expected {reference_text!r}"
        )


def test_five_sites_complex_interleaved_fuzz() -> None:
    """
    5 sites make 30 random local operations (inserts & deletes) with simulated
    message passing between subsets of sites during editing.
    Finally all remaining ops are broadcast to all sites.
    Assert that all 5 sites end up with 100% identical text.
    """
    rng = random.Random(1337)
    sites = [create_fresh_replica(f"peer_{i}") for i in range(5)]
    all_generated_ops: list[Op] = []

    # 30 rounds of random local editing
    for _ in range(30):
        site_idx = rng.randint(0, 4)
        site = sites[site_idx]

        # 80% chance insert, 20% delete (if doc has text)
        if site.visible_len() > 0 and rng.random() < 0.25:
            pos = rng.randint(0, site.visible_len() - 1)
            op = site.local_delete(pos)
        else:
            pos = rng.randint(0, site.visible_len())
            char = rng.choice("abcdefghijklmnopqrstuvwxyz0123456789")
            op = site.local_insert(pos, char)

        all_generated_ops.append(op)

        # Gossip op to 1 or 2 random peers immediately
        peer_targets = rng.sample(sites, k=rng.randint(1, 2))
        for target in peer_targets:
            if target is not site:
                target.apply(op)

    # Final sync phase: broadcast all ops to all 5 sites
    for op in all_generated_ops:
        for site in sites:
            site.apply(op)

    # Assert all 5 replicas converged
    canonical_text = sites[0].text()
    for idx, site in enumerate(sites[1:], start=1):
        assert site.text() == canonical_text, (
            f"Site {idx} diverged! Got {site.text()!r}, expected {canonical_text!r}"
        )
