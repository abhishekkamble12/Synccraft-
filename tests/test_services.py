"""
Integration tests for domain services (services.py).
"""

import pytest
from django.contrib.auth.models import User

from crdt.ids import ROOT, CharId
from crdt.ops import Op
from documents.models import Document, Operation
from documents.services import (
    OpResult,
    apply_operation,
    generate_revert_operations,
    get_or_load_document_rga,
    reconstruct_state_at_seq,
    sync_client_state,
)


@pytest.mark.django_db
def test_apply_operation_sequence_and_persistence() -> None:
    user = User.objects.create_user(username="alice", password="password")
    doc = Document.objects.create(title="Test Doc", owner=user)

    # Create and apply first op
    cid1 = CharId(1, "s1")
    op1 = Op.create_insert("s1", cid1, ROOT, "A")

    assert apply_operation(doc.id, op1, user=user) == OpResult(1, True)

    # Create and apply second op
    cid2 = CharId(2, "s1")
    op2 = Op.create_insert("s1", cid2, cid1, "B")

    assert apply_operation(doc.id, op2, user=user) == OpResult(2, True)

    doc.refresh_from_db()
    assert doc.head_seq == 2

    # Verify RGA loaded from DB matches
    rga = get_or_load_document_rga(doc.id)
    assert rga.text() == "AB"


@pytest.mark.django_db
def test_apply_duplicate_operation_is_idempotent() -> None:
    user = User.objects.create_user(username="bob")
    doc = Document.objects.create(title="Duplicate Test", owner=user)

    cid = CharId(1, "s1")
    op = Op.create_insert("s1", cid, ROOT, "X", op_id="unique-fixed-id")

    assert apply_operation(doc.id, op, user=user) == OpResult(1, True)

    # Re-apply identical op: acknowledged with its original seq, not persisted again
    assert apply_operation(doc.id, op, user=user) == OpResult(1, False)

    assert Operation.objects.filter(document=doc).count() == 1


@pytest.mark.django_db
def test_sync_client_state_reconnect() -> None:
    user = User.objects.create_user(username="carol")
    doc = Document.objects.create(title="Sync Test", owner=user)

    # Server has operations 1, 2, 3
    c1 = CharId(1, "s1")
    c2 = CharId(2, "s1")
    c3 = CharId(3, "s1")
    op1 = Op.create_insert("s1", c1, ROOT, "A")
    op2 = Op.create_insert("s1", c2, c1, "B")
    op3 = Op.create_insert("s1", c3, c2, "C")

    apply_operation(doc.id, op1, user)
    apply_operation(doc.id, op2, user)
    apply_operation(doc.id, op3, user)

    # Client was offline since seq 1, generated pending op4
    c4 = CharId(10, "client_offline")
    op_offline = Op.create_insert("client_offline", c4, c1, "Z")

    result = sync_client_state(
        doc_id=doc.id,
        last_seq=1,
        pending_ops=[op_offline],
        user=user,
    )

    # Missed ops are seq 2, 3 plus the client's own op persisted as seq 4
    assert [m["seq"] for m in result.missed] == [2, 3, 4]
    assert op_offline.op_id in result.acked
    assert result.head_seq == 4
    assert [seq for seq, _ in result.newly_applied] == [4]

    # Replaying the same sync is idempotent: nothing new is persisted or broadcast
    again = sync_client_state(doc.id, last_seq=4, pending_ops=[op_offline], user=user)
    assert again.newly_applied == []
    assert again.head_seq == 4


@pytest.mark.django_db
def test_history_reconstruction_and_revert_generation() -> None:
    user = User.objects.create_user(username="david")
    doc = Document.objects.create(title="History Doc", owner=user)

    # Type "HELLO" (5 ops)
    chars = ["H", "E", "L", "L", "O"]
    parent = ROOT
    for idx, ch in enumerate(chars, start=1):
        cid = CharId(idx, "s1")
        op = Op.create_insert("s1", cid, parent, ch)
        apply_operation(doc.id, op, user)
        parent = cid

    # Reconstruct text at sequence 2 -> "HE"
    _, text_at_2 = reconstruct_state_at_seq(doc.id, 2)
    assert text_at_2 == "HE"

    # Reconstruct text at sequence 5 -> "HELLO"
    _, text_at_5 = reconstruct_state_at_seq(doc.id, 5)
    assert text_at_5 == "HELLO"

    # Generate revert ops from "HELLO" to sequence 2 ("HE")
    revert_ops, _ = generate_revert_operations(doc.id, target_seq=2, site_id="revert_site")
    assert len(revert_ops) > 0

    # Apply revert ops
    for rop in revert_ops:
        apply_operation(doc.id, rop, user)

    # Current text is now reverted to "HE"
    current_rga = get_or_load_document_rga(doc.id)
    assert current_rga.text() == "HE"
