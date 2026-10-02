"""
Regression tests for write-path concurrency, multi-node cache coherence, and access control.
"""

import threading

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.test import Client

import documents.services as services
from crdt.ids import ROOT, CharId
from crdt.ops import Op
from crdt.rga import RGA
from documents.models import AIJob, Collaborator, Document, Operation
from tests.helpers import ws_communicator


def _insert(site: str, clock: int, parent: CharId, ch: str) -> Op:
    return Op.create_insert(site, CharId(clock, site), parent, ch)


# -----------------------------------------------------------------------------
# Write path
# -----------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_apply_operation_completes_without_deadlock() -> None:
    """apply_operation once re-acquired a non-reentrant lock and hung forever."""
    user = User.objects.create(username="deadlock")
    doc = Document.objects.create(owner=user)
    outcome: list[object] = []

    def write() -> None:
        try:
            outcome.append(services.apply_operation(doc.id, _insert("s", 1, ROOT, "x")))
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            outcome.append(exc)

    t = threading.Thread(target=write, daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "apply_operation deadlocked"
    assert outcome == [services.OpResult(1, True)]


@pytest.mark.django_db(transaction=True)
def test_concurrent_writers_get_unique_contiguous_seqs() -> None:
    user = User.objects.create(username="racer")
    doc = Document.objects.create(owner=user)
    errors: list[Exception] = []

    def writer(site: str) -> None:
        try:
            parent = ROOT
            for clock in range(1, 26):
                op = _insert(site, clock, parent, "a")
                services.apply_operation(doc.id, op)
                parent = op.char_id
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors
    seqs = sorted(Operation.objects.filter(document=doc).values_list("server_seq", flat=True))
    assert seqs == list(range(1, 101))
    assert services.get_document_state(doc.id).text == "a" * 100


@pytest.mark.django_db
def test_replica_catches_up_with_writes_from_another_process() -> None:
    """
    Simulate a second server node: it writes rows directly to the shared log.
    This node's cached replica must replay them before serving reads or writes.
    """
    user = User.objects.create(username="multinode")
    doc = Document.objects.create(owner=user)

    op1 = _insert("node1", 1, ROOT, "A")
    services.apply_operation(doc.id, op1)
    assert services.get_document_state(doc.id).text == "A"  # warm this node's cache

    # "Node 2" persists seq 2 without touching this process's cache.
    op2 = _insert("node2", 2, op1.char_id, "B")
    Operation.objects.create(
        document=doc,
        server_seq=2,
        op_id=op2.op_id,
        site_id=op2.site_id,
        lamport=op2.lamport,
        type=op2.type,
        payload=op2.to_dict(),
    )
    Document.objects.filter(id=doc.id).update(head_seq=2)

    # Reads see node 2's write ...
    state = services.get_document_state(doc.id)
    assert (state.text, state.seq) == ("AB", 2)

    # ... and so do writes, including snapshots taken by this node.
    op3 = _insert("node1", 3, op2.char_id, "C")
    assert services.apply_operation(doc.id, op3) == services.OpResult(3, True)
    services.clear_replica_cache()
    assert services.get_document_state(doc.id).text == "ABC"


@pytest.mark.django_db
def test_snapshot_written_every_interval_matches_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "SNAPSHOT_INTERVAL", 5)
    user = User.objects.create(username="snap")
    doc = Document.objects.create(owner=user)
    ops, parent = [], ROOT
    for i, ch in enumerate("snapshotting", start=1):
        op = _insert("s", i, parent, ch)
        ops.append(op)
        parent = op.char_id
    services.apply_operations(doc.id, ops)

    assert list(doc.snapshots.order_by("server_seq").values_list("server_seq", flat=True)) == [
        5,
        10,
    ]
    services.clear_replica_cache()  # force load from snapshot 10 + replay 11..12
    assert services.get_document_state(doc.id).text == "snapshotting"
    assert services.reconstruct_state_at_seq(doc.id, 7)[1] == "snapsho"


@pytest.mark.django_db
def test_op_id_reused_from_another_document_is_rejected_not_fatal() -> None:
    """It once failed the whole batch with an IntegrityError; now only that op is refused."""
    user = User.objects.create(username="poison")
    doc = Document.objects.create(owner=user)
    other = Document.objects.create(owner=user)
    services.apply_operation(other.id, _insert("s", 1, ROOT, "z"), user)
    clash = Operation.objects.get(document=other).op_id

    good = _insert("s", 1, ROOT, "A")
    bad = Op.create_insert("s", CharId(2, "s"), good.char_id, "B", op_id=clash)
    results = services.apply_operations(doc.id, [good, bad])

    assert [r.error for r in results] == [None, "duplicate_op_id"]
    assert services.get_document_state(doc.id).text == "A"
    assert Operation.objects.filter(document=doc).count() == 1


@pytest.mark.django_db
def test_failed_transaction_does_not_poison_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User.objects.create(username="poison2")
    doc = Document.objects.create(owner=user)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(Operation.objects, "bulk_create", boom)
    with pytest.raises(RuntimeError):
        services.apply_operations(doc.id, [_insert("s", 1, ROOT, "A")])
    monkeypatch.undo()

    assert services.get_document_state(doc.id).text == ""
    assert Operation.objects.filter(document=doc).count() == 0


@pytest.mark.django_db
def test_revert_ops_are_minimal_and_exact() -> None:
    user = User.objects.create(username="revert_min")
    doc = Document.objects.create(owner=user)
    working = RGA(site_id="typist")
    services.apply_operations(doc.id, [working.local_insert(i, c) for i, c in enumerate("abcdef")])
    target_seq = 6
    services.apply_operations(
        doc.id,
        [working.local_delete(2), working.local_insert(2, "X"), working.local_insert(0, "_")],
    )
    assert services.get_document_state(doc.id).text == "_abXdef"

    revert, _ = services.generate_revert_operations(doc.id, target_seq, site_id="rv")
    assert len(revert) == 3  # delete "_", delete "X", re-insert "c"
    services.apply_operations(doc.id, revert)
    assert services.get_document_state(doc.id).text == "abcdef"


# -----------------------------------------------------------------------------
# Access control (HTTP)
# -----------------------------------------------------------------------------


@pytest.fixture
def shared_doc(db: None) -> dict[str, object]:
    owner = User.objects.create_user(username="owner", password="pw")
    viewer = User.objects.create_user(username="viewer", password="pw")
    outsider = User.objects.create_user(username="outsider", password="pw")
    doc = Document.objects.create(title="Private", owner=owner)
    Collaborator.objects.create(document=doc, user=viewer, role="viewer")
    services.apply_operation(doc.id, _insert("s", 1, ROOT, "A"), owner)
    return {"owner": owner, "viewer": viewer, "outsider": outsider, "doc": doc}


@pytest.mark.parametrize(
    "path",
    ["/docs/{id}/", "/api/docs/{id}/history/", "/api/docs/{id}/at/1/", "/docs/{id}/share/"],
)
def test_outsider_cannot_read_document(shared_doc: dict, path: str) -> None:
    client = Client()
    client.force_login(shared_doc["outsider"])
    assert client.get(path.format(id=shared_doc["doc"].id)).status_code == 404


def test_viewer_can_read_but_not_revert(shared_doc: dict) -> None:
    client = Client()
    client.force_login(shared_doc["viewer"])
    doc_id = shared_doc["doc"].id
    assert client.get(f"/api/docs/{doc_id}/history/").status_code == 200
    assert client.post(f"/api/docs/{doc_id}/revert/0/").status_code == 403
    assert services.get_document_state(doc_id).text == "A"


def test_outsider_cannot_revert(shared_doc: dict) -> None:
    client = Client()
    client.force_login(shared_doc["outsider"])
    assert client.post(f"/api/docs/{shared_doc['doc'].id}/revert/0/").status_code == 404


# -----------------------------------------------------------------------------
# Access control (WebSocket)
# -----------------------------------------------------------------------------


async def _connect(doc: Document, user: User | None, origin: bytes = b"http://localhost") -> bool:
    comm = ws_communicator(doc.id, user, origin=origin)
    connected, _ = await comm.connect()
    await comm.disconnect()
    return bool(connected)


@pytest.mark.django_db(transaction=True)
async def test_websocket_rejects_anonymous_and_outsiders() -> None:
    owner = await User.objects.acreate(username="ws_owner")
    outsider = await User.objects.acreate(username="ws_outsider")
    doc = await Document.objects.acreate(owner=owner)

    assert await _connect(doc, owner) is True
    assert await _connect(doc, None) is False
    assert await _connect(doc, outsider) is False


@pytest.mark.django_db(transaction=True)
async def test_websocket_rejects_cross_site_origin() -> None:
    owner = await User.objects.acreate(username="ws_csrf")
    doc = await Document.objects.acreate(owner=owner)
    assert await _connect(doc, owner, origin=b"https://evil.example") is False


@pytest.mark.django_db(transaction=True)
async def test_viewer_websocket_cannot_write() -> None:
    owner = await User.objects.acreate(username="ws_owner2")
    viewer = await User.objects.acreate(username="ws_viewer")
    doc = await Document.objects.acreate(owner=owner)
    await Collaborator.objects.acreate(document=doc, user=viewer, role="viewer")

    comm = ws_communicator(doc.id, viewer)
    assert (await comm.connect())[0]
    init = await comm.receive_json_from()
    assert init["role"] == "viewer"

    op = _insert("v", 1, ROOT, "x")
    await comm.send_json_to({"type": "op", "op": op.to_dict()})
    assert (await comm.receive_json_from())["code"] == "forbidden"

    # Pending ops smuggled through a sync are ignored too.
    await comm.send_json_to({"type": "sync", "last_seq": 0, "pending": [op.to_dict()]})
    assert (await comm.receive_json_from())["type"] == "sync_ack"
    await comm.disconnect()

    assert await Operation.objects.filter(document=doc).acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_ai_cancel_is_scoped_to_the_connected_document() -> None:
    owner = await User.objects.acreate(username="ws_cancel")
    victim_owner = await User.objects.acreate(username="victim")
    doc = await Document.objects.acreate(owner=owner)
    other_doc = await Document.objects.acreate(owner=victim_owner)
    other_job = await AIJob.objects.acreate(document=other_doc, user=victim_owner, kind="rewrite")

    comm = ws_communicator(doc.id, owner)
    await comm.connect()
    await comm.receive_json_from()
    await comm.send_json_to({"type": "ai_cancel", "job_id": str(other_job.id)})
    assert await comm.receive_nothing(timeout=0.3)
    await comm.disconnect()

    await other_job.arefresh_from_db()
    assert other_job.status == "queued"


@pytest.mark.django_db(transaction=True)
async def test_sync_only_broadcasts_newly_applied_ops() -> None:
    """A reconnecting client must not re-broadcast the whole missed history to the room."""
    owner = await User.objects.acreate(username="ws_sync")
    doc = await Document.objects.acreate(owner=owner)
    history = [_insert("h", 1, ROOT, "a")]
    history.append(_insert("h", 2, history[0].char_id, "b"))
    await sync_to_async(services.apply_operations)(doc.id, history)

    watcher = ws_communicator(doc.id, owner)
    await watcher.connect()
    await watcher.receive_json_from()

    returning = ws_communicator(doc.id, owner)
    await returning.connect()
    await returning.receive_json_from()
    offline_op = _insert("offline", 5, history[1].char_id, "c")
    await returning.send_json_to({"type": "sync", "last_seq": 0, "pending": [offline_op.to_dict()]})
    ack = await returning.receive_json_from()
    assert [m["seq"] for m in ack["missed"]] == [1, 2, 3]

    broadcast = await watcher.receive_json_from()
    assert broadcast["type"] == "ops"
    assert [i["seq"] for i in broadcast["ops"]] == [3]
    assert await watcher.receive_nothing(timeout=0.3)

    await watcher.disconnect()
    await returning.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_missed_summary_only_on_reconnect_not_gap_repair(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap-repair syncs once spawned an AI summary job each; only reconnects should."""
    settings.MISSED_SUMMARY_THRESHOLD = 2  # type: ignore[attr-defined]
    dispatched: list[tuple] = []

    async def fake_dispatch(self, task, *args):  # type: ignore[no-untyped-def]
        dispatched.append(args)

    monkeypatch.setattr("documents.consumers.DocumentConsumer._dispatch", fake_dispatch)

    owner = await User.objects.acreate(username="ws_summary")
    doc = await Document.objects.acreate(owner=owner)
    ops = [_insert("h", 1, ROOT, "a")]
    for clock in range(2, 6):
        ops.append(_insert("h", clock, ops[-1].char_id, "a"))
    await sync_to_async(services.apply_operations)(doc.id, ops)

    comm = ws_communicator(doc.id, owner)
    await comm.connect()
    await comm.receive_json_from()

    for reason in ("gap", "reconnect", "gap", "reconnect"):
        await comm.send_json_to({"type": "sync", "reason": reason, "last_seq": 1, "pending": []})
        await comm.receive_json_from()
    await comm.disconnect()

    assert len(dispatched) == 1
    assert await AIJob.objects.filter(document=doc, kind="summary").acount() == 1


def test_owner_pages_render(shared_doc: dict) -> None:
    client = Client()
    client.force_login(shared_doc["owner"])
    doc_id = shared_doc["doc"].id

    editor = client.get(f"/docs/{doc_id}/")
    assert editor.status_code == 200
    assert editor.context["user_role"] == "owner"
    assert editor.context["initial_text"] == "A"

    listing = client.get("/")
    assert listing.status_code == 200
    assert shared_doc["doc"] in listing.context["documents"]

    assert client.get(f"/docs/{doc_id}/share/").json()["owner"] == "owner"
