"""
Integration test suite for offline editing, reconnection, duplicate resilience,
and crash recovery (Core Requirements 03 & 07).
"""

import pytest
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User

from config.asgi import application
from crdt.ids import CharId, ROOT
from crdt.ops import Op
from crdt.rga import RGA
from documents.models import Document, Operation
import documents.services as services


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_disconnect_mid_edit_no_duplicate_or_loss() -> None:
    """
    Requirement 07: Handle a client disconnect/reconnect mid-edit without data loss or duplication.
    """
    user = await User.objects.acreate(username="tester_dup")
    doc = await Document.objects.acreate(title="Disconnect Test", owner=user)

    comm1 = WebsocketCommunicator(application, f"/ws/docs/{doc.id}/")
    await comm1.connect()
    await comm1.receive_json_from()  # Consume init

    # Client generates 2 operations
    cid1 = CharId(1, "client_a")
    cid2 = CharId(2, "client_a")
    op1 = Op.create_insert("client_a", 1, cid1, ROOT, "H")
    op2 = Op.create_insert("client_a", 2, cid2, cid1, "i")

    # Send op1 and receive ack
    await comm1.send_json_to({"type": "op", "op": op1.to_dict()})
    ack1 = await comm1.receive_json_from()
    assert ack1["type"] == "ack"
    assert ack1["seq"] == 1

    # Send op2, but immediately disconnect before processing ack (simulating network drop)
    await comm1.send_json_to({"type": "op", "op": op2.to_dict()})
    await comm1.disconnect()

    # Reconnect fresh communicator (client resumes session)
    comm2 = WebsocketCommunicator(application, f"/ws/docs/{doc.id}/")
    await comm2.connect()
    init_msg = await comm2.receive_json_from()
    assert init_msg["type"] == "init"

    # Client resends unacknowledged op2 along with last_seq=1 via sync message
    await comm2.send_json_to(
        {
            "type": "sync",
            "last_seq": 1,
            "pending": [op2.to_dict()],
        }
    )

    sync_ack = await comm2.receive_json_from()
    assert sync_ack["type"] == "sync_ack"
    assert op2.op_id in sync_ack["acked"]

    # Assert database state: exactly 2 operations exist, no duplicates
    count = await Operation.objects.filter(document_id=doc.id).acount()
    assert count == 2

    # Assert final document text is exactly "Hi"
    rga = services.get_or_load_document_rga(doc.id)
    assert rga.text() == "Hi"

    await comm2.disconnect()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_offline_concurrent_editing_merges_on_reconnect() -> None:
    """
    Requirement 03: Edits made offline must merge correctly once a client reconnects,
    without overwriting others' changes.
    """
    user = await User.objects.acreate(username="tester_offline")
    doc = await Document.objects.acreate(title="Offline Merge Test", owner=user)

    # Initial state: type "BASE" on server
    comm_online = WebsocketCommunicator(application, f"/ws/docs/{doc.id}/")
    await comm_online.connect()
    await comm_online.receive_json_from()

    chars = ["B", "A", "S", "E"]
    parent = ROOT
    for idx, c in enumerate(chars, start=1):
        cid = CharId(idx, "initial")
        op = Op.create_insert("initial", idx, cid, parent, c)
        await comm_online.send_json_to({"type": "op", "op": op.to_dict()})
        await comm_online.receive_json_from()  # ack
        parent = cid

    # Client B starts offline with copy of "BASE" (last_seq = 4)
    client_b_rga = RGA(site_id="client_b")
    for idx, c in enumerate(chars, start=1):
        client_b_rga.local_insert(idx - 1, c)

    # --- Concurrent Edits While Client B is Offline ---
    # 1. Client A (online) inserts "!" at the end (pos 4 -> "BASE!")
    cid_a = CharId(10, "client_a")
    parent_e = CharId(4, "initial")
    op_online = Op.create_insert("client_a", 10, cid_a, parent_e, "!")
    await comm_online.send_json_to({"type": "op", "op": op_online.to_dict()})
    await comm_online.receive_json_from()  # ack

    # 2. Client B (offline) locally inserts "SUPER " at the start (pos 0)
    # and deletes 'E'
    op_b1 = client_b_rga.local_insert(0, "S")
    op_b2 = client_b_rga.local_insert(1, "U")

    # --- Client B Reconnects and Synchronizes ---
    comm_b = WebsocketCommunicator(application, f"/ws/docs/{doc.id}/")
    await comm_b.connect()
    await comm_b.receive_json_from()  # init

    # Client B sends sync with last_seq=4 and its offline pending ops
    await comm_b.send_json_to(
        {
            "type": "sync",
            "last_seq": 4,
            "pending": [op_b1.to_dict(), op_b2.to_dict()],
        }
    )

    sync_ack_b = await comm_b.receive_json_from()
    assert sync_ack_b["type"] == "sync_ack"
    assert op_b1.op_id in sync_ack_b["acked"]
    assert op_b2.op_id in sync_ack_b["acked"]

    # Client A receives the broadcasted offline ops from Client B
    b_broadcast_1 = await comm_online.receive_json_from()
    assert b_broadcast_1["type"] == "op"

    # Server state convergence
    server_rga = services.get_or_load_document_rga(doc.id)
    server_text = server_rga.text()

    # Apply missed ops to Client B
    for missed in sync_ack_b["missed"]:
        client_b_rga.apply(Op.from_dict(missed["op"]))

    # Assert both replicas and server converged to identical text with zero data loss
    assert client_b_rga.text() == server_text
    assert "SU" in server_text
    assert "BASE" in server_text
    assert "!" in server_text

    await comm_online.disconnect()
    await comm_b.disconnect()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_server_crash_recovery_from_database() -> None:
    """
    Simulates a total server crash / process restart mid-session.
    Asserts state is 100% reconstructed from PostgreSQL operation logs.
    """
    user = await User.objects.acreate(username="crash_tester")
    doc = await Document.objects.acreate(title="Crash Recovery Doc", owner=user)

    comm = WebsocketCommunicator(application, f"/ws/docs/{doc.id}/")
    await comm.connect()
    await comm.receive_json_from()

    # Write 10 characters
    expected_str = "CRASHPROOF"
    parent = ROOT
    for i, ch in enumerate(expected_str, start=1):
        cid = CharId(i, "node1")
        op = Op.create_insert("node1", i, cid, parent, ch)
        await comm.send_json_to({"type": "op", "op": op.to_dict()})
        await comm.receive_json_from()
        parent = cid

    await comm.disconnect()

    # --- SIMULATE CRASH: Clear all in-memory caches ---
    services._doc_cache.clear()
    services._doc_locks.clear()

    # Reconnect and load document from scratch
    reconstructed_rga = services.get_or_load_document_rga(doc.id)
    assert reconstructed_rga.text() == expected_str
    assert reconstructed_rga.visible_len() == len(expected_str)
