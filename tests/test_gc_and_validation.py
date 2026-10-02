"""
Server-side op validation, site binding, and tombstone garbage collection.
"""

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.utils import timezone

import documents.services as services
from crdt.ids import ROOT, CharId
from crdt.ops import Op
from crdt.rebase import rebase_pending
from crdt.rga import RGA
from documents.models import Collaborator, Document, Operation, SiteSession
from tests.helpers import ws_communicator

# -----------------------------------------------------------------------------
# Validation through the real WebSocket path
# -----------------------------------------------------------------------------


async def _open(doc: Document, user: User):  # type: ignore[no-untyped-def]
    comm = ws_communicator(doc.id, user)
    assert (await comm.connect())[0]
    init = await comm.receive_json_from()
    return comm, init


@pytest.mark.django_db(transaction=True)
async def test_forged_ops_are_rejected_over_websocket() -> None:
    owner = await User.objects.acreate(username="val_owner")
    doc = await Document.objects.acreate(owner=owner)
    comm, init = await _open(doc, owner)
    rga = RGA.from_dict(init["snapshot"], site_id="honest")

    hello = rga.local_insert(0, "hello")
    await comm.send_json_to({"type": "op", "op": hello.to_dict(), "base_seq": 0})
    assert (await comm.receive_json_from())["type"] == "ack"

    async def send(op: Op) -> dict:
        await comm.send_json_to({"type": "op", "op": op.to_dict(), "base_seq": 1})
        return await comm.receive_json_from()

    # Live ops must use the site the socket is bound to.
    other_site = Op.create_insert("someone_else", CharId(50, "someone_else"), ROOT, "x")
    assert (await send(other_site))["reason"] == "site_mismatch"

    # A child id smaller than its parent's breaks RGA's ordering argument.
    low_clock = Op.create_insert("honest", CharId(2, "honest"), CharId(5, "honest"), "x")
    reply = await send(low_clock)
    assert (reply["code"], reply["reason"]) == ("op_rejected", "clock_not_after_parent")

    # The first rejection retires the site: even a valid op from it is refused now,
    # so the site's committed ops stay a prefix of what it sent.
    valid_later = rga.local_insert(5, "!")
    assert (await send(valid_later))["reason"] == "site_retired"

    await comm.disconnect()
    assert await Operation.objects.filter(document=doc).acount() == 1
    session = await SiteSession.objects.aget(document=doc, site_id="honest")
    assert session.retired


@pytest.mark.django_db(transaction=True)
async def test_one_user_cannot_write_under_another_users_site() -> None:
    alice = await User.objects.acreate(username="site_alice")
    mallory = await User.objects.acreate(username="site_mallory")
    doc = await Document.objects.acreate(owner=alice)
    await Collaborator.objects.acreate(document=doc, user=mallory, role="editor")

    a, init = await _open(doc, alice)
    rga = RGA.from_dict(init["snapshot"], site_id="alice_tab")
    await a.send_json_to({"type": "sync", "site_id": "alice_tab", "last_seq": 0, "pending": []})
    await a.receive_json_from()

    # Mallory smuggles an op under alice's site through a sync (no socket binding there).
    forged = rga.local_insert(0, "pwned")
    m, _ = await _open(doc, mallory)
    await m.send_json_to(
        {"type": "sync", "last_seq": 0, "pending": [{"op": forged.to_dict(), "base_seq": 0}]}
    )
    ack = await m.receive_json_from()
    assert ack["rejected"] == [{"op_id": forged.op_id, "reason": "site_owned_by_other_user"}]

    # ... and alice's site was not retired by mallory's attempt.
    honest = rga.local_insert(0, "hi")
    await a.send_json_to({"type": "op", "op": honest.to_dict(), "base_seq": 0})
    assert (await a.receive_json_from())["type"] == "ack"

    await a.disconnect()
    await m.disconnect()


@pytest.mark.django_db
def test_orphan_insert_is_never_persisted_or_buffered() -> None:
    user = User.objects.create(username="orphan")
    doc = Document.objects.create(owner=user)
    orphan = Op.create_insert("m", CharId(9, "m"), CharId(8, "ghost"), "Z")
    assert services.apply_operation(doc.id, orphan, user).error == "unknown_parent"
    assert services.get_or_load_document_rga(doc.id).pending_count == 0
    assert not Operation.objects.filter(document=doc).exists()


# -----------------------------------------------------------------------------
# Garbage collection
# -----------------------------------------------------------------------------


def _type_and_delete(doc: Document, user: User, site: str, text: str) -> RGA:
    rga = RGA(site)
    services.apply_operations(doc.id, [rga.local_insert(0, text)], user=user)
    services.apply_operations(doc.id, [rga.local_delete(0, len(text))], user=user)
    return rga


@pytest.mark.django_db
def test_compaction_waits_for_the_slowest_live_session() -> None:
    user = User.objects.create(username="gc_wait")
    doc = Document.objects.create(owner=user)
    _type_and_delete(doc, user, "w", "x" * 200)  # seq 1 insert, seq 2 delete

    services.record_site_ack(doc.id, "fast", user, 2)
    services.record_site_ack(doc.id, "slow", user, 1)  # has not seen the delete yet
    held_back = services.compact_document(doc.id, min_ops=1)
    assert held_back is not None
    assert (held_back.gc_seq, held_back.tombstones_removed) == (1, 0)

    services.record_site_ack(doc.id, "slow", user, 2)
    result = services.compact_document(doc.id, min_ops=1)
    assert result is not None
    assert (result.gc_seq, result.tombstones_removed, result.nodes_after) == (2, 200, 0)
    doc.refresh_from_db()
    assert doc.gc_seq == 2


@pytest.mark.django_db
def test_expired_sessions_stop_holding_back_gc(settings: object) -> None:
    settings.SITE_SESSION_TTL_SEC = 60  # type: ignore[attr-defined]
    user = User.objects.create(username="gc_ttl")
    doc = Document.objects.create(owner=user)
    _type_and_delete(doc, user, "w", "abc")
    services.record_site_ack(doc.id, "gone", user, 0)
    assert services.compact_document(doc.id, min_ops=1) is None

    SiteSession.objects.filter(site_id="gone").update(
        last_seen=timezone.now() - timedelta(seconds=120)
    )
    assert services.compact_document(doc.id, min_ops=1) is not None


@pytest.mark.django_db
def test_compaction_keeps_tombstones_named_by_later_ops_and_rejects_stale_ones() -> None:
    user = User.objects.create(username="gc_keep")
    doc = Document.objects.create(owner=user)
    alice, bob = RGA("alice"), RGA("bob")
    seed = alice.local_insert(0, "abcdef")
    services.apply_operation(doc.id, seed, user)
    bob.apply(seed)

    # Bob (based on seq 1) inserts after 'c' concurrently with alice deleting "bcd".
    services.apply_operation(doc.id, alice.local_delete(1, 3), user, base_seq=1)  # seq 2
    late = bob.local_insert(3, "!")
    services.apply_operation(doc.id, late, user, base_seq=1)  # seq 3, parent 'c' is a tombstone
    assert services.get_document_state(doc.id).text == "a!ef"

    services.record_site_ack(doc.id, "alice", user, 2)
    result = services.compact_document(doc.id, min_ops=1)
    assert result is not None and result.gc_seq == 2
    assert result.tombstones_removed == 2  # 'b' and 'd'; 'c' is still named by seq 3

    # The compacted snapshot plus the log reproduces every state from scratch.
    services.clear_replica_cache()
    assert services.get_document_state(doc.id).text == "a!ef"
    assert services.reconstruct_state_at_seq(doc.id, 3)[1] == "a!ef"

    # An op based on a state older than the GC point is refused as stale.
    stale = bob.local_insert(0, "?")
    assert services.apply_operation(doc.id, stale, user, base_seq=1).error == "stale"


@pytest.mark.django_db
def test_other_process_reloads_compacted_state() -> None:
    user = User.objects.create(username="gc_multi")
    doc = Document.objects.create(owner=user)
    _type_and_delete(doc, user, "w", "y" * 50)
    assert services.get_or_load_document_rga(doc.id).total_len() == 50  # warm cache

    # "Another node" compacts: simulate by compacting then restoring the stale cache.
    stale_replica = services._replicas[doc.id]
    services.record_site_ack(doc.id, "s", user, 2)
    assert services.compact_document(doc.id, min_ops=1) is not None
    services._replicas[doc.id] = stale_replica

    assert services.get_or_load_document_rga(doc.id).total_len() == 0


@pytest.mark.django_db(transaction=True)
async def test_stale_client_rebases_over_websocket_without_losing_edits() -> None:
    """
    Bob goes offline, alice deletes the word bob was typing after, the server
    collects it, bob comes back: his op is rejected as stale, he reloads, rebases
    onto a new site and his text lands next to where it was.
    """
    owner = await User.objects.acreate(username="rebase_owner")
    doc = await Document.objects.acreate(owner=owner)

    alice_ws, init = await _open(doc, owner)
    alice = RGA.from_dict(init["snapshot"], site_id="alice")
    seed = alice.local_insert(0, "one two three")
    await alice_ws.send_json_to({"type": "op", "op": seed.to_dict(), "base_seq": 0})
    await alice_ws.receive_json_from()

    bob = RGA("bob")
    bob.apply(seed)
    offline = bob.local_insert(7, "!")  # after "two", based on seq 1

    delete = alice.local_delete(4, 3)
    await alice_ws.send_json_to({"type": "op", "op": delete.to_dict(), "base_seq": 1})
    await alice_ws.receive_json_from()
    await alice_ws.send_json_to({"type": "sync", "site_id": "alice", "last_seq": 2, "pending": []})
    await alice_ws.receive_json_from()
    await sync_to_async(services.compact_document)(doc.id, 1)

    bob_ws, _ = await _open(doc, owner)
    await bob_ws.send_json_to(
        {
            "type": "sync",
            "site_id": "bob",
            "last_seq": 1,
            "pending": [{"op": offline.to_dict(), "base_seq": 1}],
        }
    )
    ack = await bob_ws.receive_json_from()
    assert ack["rejected"] == [{"op_id": offline.op_id, "reason": "stale"}]
    assert ack["gc_seq"] == 2

    await bob_ws.send_json_to({"type": "resync"})
    fresh_init = await bob_ws.receive_json_from()
    fresh = RGA.from_dict(fresh_init["snapshot"], site_id="bob-2")
    rebased = rebase_pending(bob, fresh, [offline])
    await bob_ws.send_json_to(
        {
            "type": "sync",
            "site_id": "bob-2",
            "last_seq": fresh_init["head_seq"],
            "pending": [{"op": op.to_dict(), "base_seq": fresh_init["head_seq"]} for op in rebased],
        }
    )
    final = await bob_ws.receive_json_from()
    assert final["rejected"] == []

    server_text = (await sync_to_async(services.get_document_state)(doc.id)).text
    assert server_text == fresh.text() == "one ! three"
    await alice_ws.disconnect()
    await bob_ws.disconnect()


# -----------------------------------------------------------------------------
# Heartbeats
# -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_heartbeats_are_buffered_then_flushed_in_one_write(settings: object) -> None:
    settings.ACK_FLUSH_INTERVAL_SEC = 3600  # type: ignore[attr-defined]
    user = User.objects.create(username="hb_user")
    doc = Document.objects.create(owner=user)
    services.flush_site_acks(doc.id)  # start the interval now

    assert services.heartbeat(doc.id, "tab1", user, 7) == 0
    assert services.heartbeat(doc.id, "tab1", user, 9) == 0
    assert not SiteSession.objects.filter(document=doc).exists(), "buffered, not written"

    # Compaction always flushes first, so GC never decides on a stale buffer.
    services.compact_document(doc.id, min_ops=10**9)
    session = SiteSession.objects.get(document=doc, site_id="tab1")
    assert (session.acked_seq, session.user_id) == (9, user.pk)


@pytest.mark.django_db
def test_buffered_heartbeat_cannot_hijack_another_users_site(settings: object) -> None:
    settings.ACK_FLUSH_INTERVAL_SEC = 0  # type: ignore[attr-defined]
    alice = User.objects.create(username="hb_alice")
    mallory = User.objects.create(username="hb_mallory")
    doc = Document.objects.create(owner=alice)
    services.record_site_ack(doc.id, "alice_tab", alice, 5)
    services.heartbeat(doc.id, "alice_tab", mallory, 0)
    assert SiteSession.objects.get(site_id="alice_tab").acked_seq == 5


@pytest.mark.django_db(transaction=True)
async def test_heartbeat_reply_tells_a_client_the_head() -> None:
    owner = await User.objects.acreate(username="hb_ws")
    doc = await Document.objects.acreate(owner=owner)
    rga = RGA("writer")
    await sync_to_async(services.apply_operations)(doc.id, [rga.local_insert(0, "abc")])

    comm, _ = await _open(doc, owner)
    await comm.send_json_to({"type": "stable", "seq": 0})
    assert await comm.receive_json_from() == {"type": "head", "seq": 1}
    await comm.disconnect()
