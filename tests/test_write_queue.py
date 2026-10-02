"""
Tests for per-document group commit (documents/write_queue.py).
"""

import asyncio

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import User

import documents.services as services
from crdt.ids import ROOT, CharId
from crdt.ops import Op
from crdt.rga import RGA
from documents.models import Document, Operation
from documents.write_queue import submit_op
from tests.helpers import ws_communicator


@pytest.mark.django_db(transaction=True)
async def test_concurrent_submissions_are_committed_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await User.objects.acreate(username="batcher")
    doc = await Document.objects.acreate(owner=user)

    batch_sizes: list[int] = []
    real_apply = services.apply_operations

    def recording_apply(doc_id, ops, user=None, users=None):  # type: ignore[no-untyped-def]
        batch_sizes.append(len(ops))
        return real_apply(doc_id, ops, user=user, users=users)

    monkeypatch.setattr("documents.write_queue.apply_operations", recording_apply)

    ops = [Op.create_insert(f"s{i}", 1, CharId(1, f"s{i}"), ROOT, "x") for i in range(40)]
    futures = [submit_op(doc.id, op, user, f"chan{i}") for i, op in enumerate(ops)]
    results = await asyncio.gather(*futures)

    assert sorted(seq for seq, _ in results) == list(range(1, 41))
    assert all(new for _, new in results)
    assert sum(batch_sizes) == 40
    assert len(batch_sizes) < 40, "ops submitted together should share transactions"


@pytest.mark.django_db(transaction=True)
async def test_bad_op_in_batch_only_fails_itself() -> None:
    user = await User.objects.acreate(username="isolate")
    doc = await Document.objects.acreate(owner=user)
    other = await Document.objects.acreate(owner=user)
    taken = Op.create_insert("o", 1, CharId(1, "o"), ROOT, "z")
    await sync_to_async(services.apply_operation)(other.id, taken)

    good1 = Op.create_insert("a", 1, CharId(1, "a"), ROOT, "A")
    clash = Op.create_insert("b", 1, CharId(1, "b"), ROOT, "B", op_id=taken.op_id)
    good2 = Op.create_insert("c", 1, CharId(1, "c"), ROOT, "C")

    results = await asyncio.gather(
        submit_op(doc.id, good1, user, "x"),
        submit_op(doc.id, clash, user, "y"),
        submit_op(doc.id, good2, user, "z"),
        return_exceptions=True,
    )

    assert isinstance(results[1], Exception)
    assert {r[0] for r in (results[0], results[2])} == {1, 2}  # type: ignore[index]
    assert await Operation.objects.filter(document=doc).acount() == 2


@pytest.mark.django_db(transaction=True)
async def test_many_sockets_typing_concurrently_converge() -> None:
    owner = await User.objects.acreate(username="swarm_owner")
    doc = await Document.objects.acreate(owner=owner)

    clients = []
    for i in range(6):
        comm = ws_communicator(doc.id, owner)
        assert (await comm.connect())[0]
        init = await comm.receive_json_from()
        clients.append((comm, RGA.from_dict(init["snapshot"], site_id=f"site{i}")))

    ops_per_client = 15
    for _ in range(ops_per_client):
        for comm, rga in clients:
            op = rga.local_insert(rga.visible_len(), "k")
            await comm.send_json_to({"type": "op", "op": op.to_dict()})

    total = ops_per_client * len(clients)
    for comm, rga in clients:
        acked, seen = 0, 0
        while acked < ops_per_client or seen < total - ops_per_client:
            msg = await comm.receive_json_from(timeout=10)
            if msg["type"] == "ack":
                acked += 1
            elif msg["type"] == "ops":
                for item in msg["ops"]:
                    rga.apply(Op.from_dict(item["op"]))
                    seen += 1
        await comm.disconnect()

    server = await sync_to_async(services.get_document_state)(doc.id)
    assert server.seq == total
    assert {rga.text() for _, rga in clients} == {server.text}
    assert len(server.text) == total
