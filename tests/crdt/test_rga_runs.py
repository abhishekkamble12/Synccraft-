"""
Run-length ops, validation, compaction and snapshots for the RGA.
"""

import itertools
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from crdt.ids import ROOT, CharId
from crdt.ops import Op
from crdt.rga import RGA, PendingOverflowError


def node_order(rga: RGA) -> list[tuple[str, str, bool]]:
    return [(str(n.char_id), n.char, n.deleted) for n in rga.iter_nodes()]


# ---------------------------------------------------------------------------
# Runs are exactly equivalent to typing one character at a time
# ---------------------------------------------------------------------------


def test_run_insert_equals_char_by_char_typing() -> None:
    by_run = RGA("s")
    by_run.local_insert(0, "hello")
    by_char = RGA("s")
    for i, char in enumerate("hello"):
        by_char.local_insert(i, char)
    assert node_order(by_run) == node_order(by_char)


def test_concurrent_runs_interleave_identically_in_every_delivery_order() -> None:
    base = RGA("base")
    seed_op = base.local_insert(0, "AB")
    sites = [RGA(f"s{i}") for i in range(3)]
    for site in sites:
        site.apply(seed_op)

    ops = [
        sites[0].local_insert(1, "xyz"),
        sites[1].local_insert(1, "12"),
        sites[2].local_delete(0, 2),
        sites[2].local_insert(0, "Q"),
    ]
    outcomes = set()
    for perm in itertools.permutations(ops):
        replica = RGA("r")
        replica.apply(seed_op)
        for op in perm:
            replica.apply(op)
        outcomes.add(tuple(node_order(replica)))
    assert len(outcomes) == 1


def test_multi_char_delete_and_positions() -> None:
    doc = RGA("s")
    doc.local_insert(0, "abcdef")
    op = doc.local_delete(1, 3)
    assert doc.text() == "aef"
    assert len(op) == 3 and len(op.spans) == 1
    assert doc.pos_of_char_id(CharId(5, "s")) == 1  # 'e'
    assert doc.pos_of_char_id(CharId(2, "s")) is None  # deleted 'b'
    with pytest.raises(IndexError):
        doc.local_delete(2, 5)


def test_emoji_are_single_code_points() -> None:
    doc = RGA("s")
    doc.local_insert(0, "a😀b")
    assert doc.visible_len() == 3
    doc.local_delete(1)
    assert doc.text() == "ab"


# ---------------------------------------------------------------------------
# Property: random multi-site sessions with runs converge in any order
# ---------------------------------------------------------------------------

edit = st.tuples(
    st.integers(0, 2),  # site
    st.booleans(),  # delete?
    st.integers(0, 60),  # position seed
    st.text(alphabet="abcdef", min_size=1, max_size=4),  # inserted run
    st.integers(1, 4),  # delete length
    st.integers(0, 3),  # how many peers receive it immediately
)


@settings(max_examples=150, deadline=None)
@given(st.lists(edit, min_size=1, max_size=40), st.randoms(use_true_random=False))
def test_property_runs_converge(edits: list[tuple[int, bool, int, str, int, int]], rnd: random.Random) -> None:
    sites = [RGA(f"s{i}") for i in range(3)]
    log: list[Op] = []
    for site_idx, is_delete, pos_seed, text, del_len, fanout in edits:
        site = sites[site_idx]
        length = site.visible_len()
        if is_delete and length:
            pos = pos_seed % length
            op = site.local_delete(pos, min(del_len, length - pos))
        else:
            op = site.local_insert(pos_seed % (length + 1), text)
        log.append(op)
        for peer in rnd.sample(sites, k=fanout % 4):
            peer.apply(op)

    reference = RGA("ref")
    for op in log:
        reference.apply(op)
    for order in (list(reversed(log)), rnd.sample(log, k=len(log))):
        replica = RGA("x")
        for op in order:
            replica.apply(op)
        assert node_order(replica) == node_order(reference)
    for site in sites:
        for op in log:
            site.apply(op)
        assert site.text() == reference.text()


# ---------------------------------------------------------------------------
# Server-side validation
# ---------------------------------------------------------------------------


def make_server() -> tuple[RGA, RGA]:
    server = RGA("server")
    client = RGA("alice")
    for op in [client.local_insert(0, "hello")]:
        assert server.validate(op) is None
        server.apply(op)
    return server, client


def test_honest_ops_validate() -> None:
    server, client = make_server()
    for op in (client.local_insert(5, " world"), client.local_delete(0, 2)):
        assert server.validate(op) is None
        server.apply(op)
    assert server.text() == client.text()


@pytest.mark.parametrize(
    ("forged", "reason"),
    [
        # Claims another site's ids.
        (Op.create_insert("mallory", CharId(50, "alice"), ROOT, "x"), "char_id_site_mismatch"),
        # Parent never existed: an orphan the server would otherwise buffer forever.
        (Op.create_insert("mallory", CharId(50, "mallory"), CharId(9, "ghost"), "x"), "unknown_parent"),
        # Id not greater than its parent's: breaks RGA's ordering argument.
        (Op.create_insert("mallory", CharId(3, "mallory"), CharId(5, "alice"), "x"), "clock_not_after_parent"),
        # Lamport inconsistent with the run length.
        (
            Op(
                op_id="x",
                site_id="mallory",
                lamport=2,
                type="insert",
                char_id=CharId(60, "mallory"),
                parent_id=ROOT,
                text="ab",
            ),
            "lamport_mismatch",
        ),
        # Deletes a character that does not exist.
        (Op.create_delete("mallory", 70, [(CharId(99, "alice"), 1)]), "unknown_target"),
        (Op.create_insert("mallory", CharId(80, "mallory"), ROOT, "\ud83d"), "bad_text"),
        (Op.create_insert("m" * 65, CharId(80, "m" * 65), ROOT, "x"), "bad_site_id"),
    ],
)
def test_forged_ops_are_rejected(forged: Op, reason: str) -> None:
    server, _ = make_server()
    assert server.validate(forged) == reason


def test_site_ids_must_strictly_increase_even_after_compaction() -> None:
    server, client = make_server()
    delete = client.local_delete(0, 5)
    server.apply(delete)
    assert server.compact() == 5
    # Replaying alice's original ids after their tombstones are gone must still fail.
    replay = Op.create_insert("alice", CharId(1, "alice"), ROOT, "hello")
    assert server.validate(replay) == "clock_not_after_site_history"


def test_pending_buffer_is_bounded() -> None:
    replica = RGA("r", max_pending=3)
    for i in range(3):
        replica.apply(Op.create_insert("s", CharId(10 + i, "s"), CharId(1, "missing"), "x"))
    assert replica.pending_count == 3
    with pytest.raises(PendingOverflowError):
        replica.apply(Op.create_insert("s", CharId(20, "s"), CharId(1, "missing"), "x"))


# ---------------------------------------------------------------------------
# Compaction and snapshots
# ---------------------------------------------------------------------------


def test_compaction_keeps_requested_tombstones_and_positions() -> None:
    doc = RGA("s")
    doc.local_insert(0, "abcdef")
    doc.local_delete(1, 4)  # leaves "af"
    keep = CharId(3, "s")  # 'c'
    assert doc.compact(keep=[keep]) == 3
    assert doc.text() == "af"
    assert doc.total_len() == 3
    assert doc.has(keep)
    # An op still referencing the kept tombstone integrates as before.
    other = Op.create_insert("t", CharId(20, "t"), keep, "Z")
    assert doc.validate(other) is None
    doc.apply(other)
    assert doc.text() == "aZf"


@settings(max_examples=60, deadline=None)
@given(st.lists(edit, min_size=1, max_size=30), st.lists(edit, min_size=1, max_size=15))
def test_property_compaction_at_a_stable_point_is_invisible(
    before: list[tuple[int, bool, int, str, int, int]],
    after: list[tuple[int, bool, int, str, int, int]],
) -> None:
    """
    Once every site has seen every op, tombstones can be dropped: any later op is
    created from a state in which those characters are already deleted, so it can
    never reference them. A compacted replica and an uncompacted one must keep
    agreeing on everything that follows.
    """

    def play(sites: list[RGA], script: list[tuple[int, bool, int, str, int, int]]) -> list[Op]:
        ops = []
        for site_idx, is_delete, pos_seed, text, del_len, _ in script:
            site = sites[site_idx]
            length = site.visible_len()
            if is_delete and length:
                pos = pos_seed % length
                op = site.local_delete(pos, min(del_len, length - pos))
            else:
                op = site.local_insert(pos_seed % (length + 1), text)
            ops.append(op)
        return ops

    sites = [RGA(f"s{i}") for i in range(3)]
    first = play(sites, before)
    full, compacted = RGA("full"), RGA("compacted")
    for replica in [*sites, full, compacted]:
        for op in first:
            replica.apply(op)
    compacted.compact()
    snapshot_copy = RGA.from_dict(compacted.to_dict(), site_id="copy")

    second = play(sites, after)
    for replica in (full, compacted, snapshot_copy):
        for op in second:
            assert replica.validate(op) is None
            replica.apply(op)
    assert compacted.text() == full.text() == snapshot_copy.text()
    assert [n.char_id for n in compacted.iter_nodes()] == [
        n.char_id for n in full.iter_nodes() if not n.deleted or n.char_id in compacted._nodes
    ]


def test_snapshot_round_trip_is_run_length_encoded() -> None:
    doc = RGA("s")
    doc.local_insert(0, "hello world")
    doc.local_delete(5, 1)
    state = doc.to_dict()
    assert state["runs"] == [["1@s", "hello", 0], ["6@s", " ", 1], ["7@s", "world", 0]]
    assert "applied_op_ids" not in state
    restored = RGA.from_dict(state, site_id="t")
    assert node_order(restored) == node_order(doc)
    assert restored.version_vector == {"s": 11}


def test_legacy_snapshot_format_still_loads() -> None:
    legacy = {
        "site_id": "server",
        "clock": 3,
        "applied_op_ids": ["a", "b", "c"],
        "nodes": [
            {"char_id": "1@a", "char": "h", "deleted": False, "parent_id": "0@"},
            {"char_id": "2@a", "char": "i", "deleted": True, "parent_id": "1@a"},
            {"char_id": "3@a", "char": "!", "deleted": False, "parent_id": "2@a"},
        ],
    }
    doc = RGA.from_dict(legacy)
    assert doc.text() == "h!"
    assert doc.version_vector == {"a": 3}
    assert doc.char_id_at(1) == CharId(3, "a")
