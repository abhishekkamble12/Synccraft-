"""
A client whose pending ops reference garbage-collected tombstones must be able to
replay its edits onto the current server state without losing or duplicating text.
"""

from crdt.ops import Op
from crdt.rebase import rebase_pending
from crdt.rga import RGA


def sync(src: RGA, *dsts: RGA, ops: list[Op]) -> None:
    for dst in dsts:
        for op in ops:
            dst.apply(op)


def test_rebase_reanchors_insert_after_collected_parent() -> None:
    server = RGA("server")
    alice = RGA("alice")
    bob = RGA("bob")
    seed = [alice.local_insert(0, "one two three")]
    sync(alice, server, bob, ops=seed)

    # Bob goes offline and types after "two".
    offline = [bob.local_insert(7, "!"), bob.local_insert(8, "?")]
    assert bob.text() == "one two!? three"

    # Meanwhile alice deletes "two" and the server collects the tombstones.
    delete = alice.local_delete(4, 3)
    server.apply(delete)
    server.compact()

    # The server would reject bob's op (unknown parent). Bob rebases.
    assert server.validate(offline[0]) == "unknown_parent"
    fresh = RGA.from_dict(server.to_dict(), site_id="bob")
    rebased = rebase_pending(bob, fresh, offline)
    assert fresh.text() == "one !? three"
    for op in rebased:
        assert server.validate(op) is None
        server.apply(op)
    assert server.text() == fresh.text()


def test_rebase_remaps_chained_inserts_and_drops_redundant_deletes() -> None:
    server = RGA("server")
    alice = RGA("alice")
    bob = RGA("bob")
    sync(alice, server, bob, ops=[alice.local_insert(0, "abc")])

    pending = [
        bob.local_insert(2, "XY"),  # after 'b'
        bob.local_insert(4, "Z"),  # after 'Y', one of bob's own pending chars
        bob.local_delete(0, 1),  # deletes 'a'
    ]
    server.apply(alice.local_delete(0, 2))  # 'a' and 'b' gone
    server.compact()

    fresh = RGA.from_dict(server.to_dict(), site_id="bob")
    rebased = rebase_pending(bob, fresh, pending)
    assert fresh.text() == "XYZc"
    assert [op.type for op in rebased] == ["insert", "insert"]  # delete of 'a' was redundant
    for op in rebased:
        assert server.validate(op) is None
        server.apply(op)
    assert server.text() == "XYZc"


def test_rebase_skips_ops_the_server_already_committed() -> None:
    server = RGA("server")
    bob = RGA("bob")
    committed = bob.local_insert(0, "hi")
    lost_ack = bob.local_insert(2, "!")
    server.apply(committed)
    server.apply(lost_ack)
    fresh = RGA.from_dict(server.to_dict(), site_id="bob")
    assert rebase_pending(bob, fresh, [lost_ack]) == []
    assert fresh.text() == "hi!"


def test_rebase_without_local_history_appends() -> None:
    server = RGA("server")
    alice = RGA("alice")
    server.apply(alice.local_insert(0, "abc"))
    orphan = Op.create_insert("bob", alice.char_id_at(0).__class__(99, "bob"), alice.char_id_at(1), "!")
    server.apply(alice.local_delete(1, 1))
    server.compact()
    fresh = RGA.from_dict(server.to_dict(), site_id="bob")
    rebase_pending(None, fresh, [orphan])
    assert fresh.text() == "ac!"
