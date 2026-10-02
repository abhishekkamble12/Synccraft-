"""
Integration tests for WebSocket DocumentConsumer.
"""

import pytest
from django.contrib.auth.models import User

from crdt.ids import ROOT, CharId
from crdt.ops import Op
from documents.models import Document
from tests.helpers import ws_communicator


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_websocket_consumer_connection_and_init() -> None:
    # Create document
    user = await User.objects.acreate(username="test_ws_user")
    doc = await Document.objects.acreate(title="WS Test Doc", owner=user)

    communicator = ws_communicator(doc.id, user)
    connected, subprotocol = await communicator.connect()
    assert connected is True

    # Receive initial document payload
    response = await communicator.receive_json_from()
    assert response["type"] == "init"
    assert response["doc_id"] == str(doc.id)
    assert response["head_seq"] == 0
    assert response["text"] == ""

    await communicator.disconnect()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_websocket_consumer_op_broadcast_between_clients() -> None:
    user = await User.objects.acreate(username="peer_tester")
    doc = await Document.objects.acreate(title="Realtime Collab", owner=user)

    comm1 = ws_communicator(doc.id, user)
    comm2 = ws_communicator(doc.id, user)

    c1_ok, _ = await comm1.connect()
    c2_ok, _ = await comm2.connect()
    assert c1_ok and c2_ok

    # Consume init messages
    await comm1.receive_json_from()
    await comm2.receive_json_from()

    # Client 1 sends an insert op
    cid = CharId(1, "client1")
    op = Op.create_insert("client1", 1, cid, ROOT, "A")

    await comm1.send_json_to(
        {
            "type": "op",
            "op": op.to_dict(),
        }
    )

    # Client 1 receives 'ack'
    ack = await comm1.receive_json_from()
    assert ack["type"] == "ack"
    assert ack["op_id"] == op.op_id
    assert ack["seq"] == 1

    # Client 2 receives broadcasted 'op'
    broadcast = await comm2.receive_json_from()
    assert broadcast["type"] == "ops"
    assert [(i["seq"], i["op"]["char"]) for i in broadcast["ops"]] == [(1, "A")]

    await comm1.disconnect()
    await comm2.disconnect()
