"""
Integration test suite for document history, time-travel reconstruction,
and non-destructive revert with concurrent editing convergence (Requirement 04).
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client

from crdt.ids import CharId, ROOT
from crdt.ops import Op
from documents.models import Document, Operation
import documents.services as services


@pytest.mark.django_db
def test_history_api_and_state_at_seq() -> None:
    user = User.objects.create_user(username="history_tester", password="pw")
    client = Client()
    client.force_login(user)

    doc = Document.objects.create(title="History API Doc", owner=user)

    # Apply 3 operations: "A", "B", "C"
    c1 = CharId(1, "s1")
    c2 = CharId(2, "s1")
    c3 = CharId(3, "s1")
    services.apply_operation(doc.id, Op.create_insert("s1", 1, c1, ROOT, "A"), user)
    services.apply_operation(doc.id, Op.create_insert("s1", 2, c2, c1, "B"), user)
    services.apply_operation(doc.id, Op.create_insert("s1", 3, c3, c2, "C"), user)

    # 1. Test GET /api/docs/<id>/history/
    res_hist = client.get(f"/api/docs/{doc.id}/history/")
    assert res_hist.status_code == 200
    data_hist = res_hist.json()
    assert data_hist["total_ops"] == 3
    assert data_hist["head_seq"] == 3

    # 2. Test GET /api/docs/<id>/at/1/ -> should be "A"
    res_at_1 = client.get(f"/api/docs/{doc.id}/at/1/")
    assert res_at_1.status_code == 200
    assert res_at_1.json()["text"] == "A"

    # 3. Test GET /api/docs/<id>/at/2/ -> should be "AB"
    res_at_2 = client.get(f"/api/docs/{doc.id}/at/2/")
    assert res_at_2.status_code == 200
    assert res_at_2.json()["text"] == "AB"

    # 4. Test GET /api/docs/<id>/at/3/ -> should be "ABC"
    res_at_3 = client.get(f"/api/docs/{doc.id}/at/3/")
    assert res_at_3.status_code == 200
    assert res_at_3.json()["text"] == "ABC"


@pytest.mark.django_db
def test_revert_api_generates_compensating_ops_and_converges() -> None:
    """
    Requirement 04: Maintain full edit history and support reverting the document
    to any earlier point in time via compensating operations.
    """
    user = User.objects.create_user(username="revert_tester", password="pw")
    client = Client()
    client.force_login(user)

    doc = Document.objects.create(title="Revert Target Doc", owner=user)

    # Initial text: "HELLO WORLD"
    text = "HELLO WORLD"
    parent = ROOT
    for i, ch in enumerate(text, start=1):
        cid = CharId(i, "s1")
        op = Op.create_insert("s1", i, cid, parent, ch)
        services.apply_operation(doc.id, op, user)
        parent = cid

    assert services.get_or_load_document_rga(doc.id).text() == "HELLO WORLD"
    assert doc.operations.count() == 11

    # Revert to sequence 5 ("HELLO")
    res_revert = client.post(f"/api/docs/{doc.id}/revert/5/")
    assert res_revert.status_code == 200
    data_revert = res_revert.json()
    assert data_revert["success"] is True
    assert data_revert["current_text"] == "HELLO"

    # Verify that history was NOT erased (log only grew via compensating ops)
    doc.refresh_from_db()
    assert doc.head_seq > 11
    assert doc.operations.count() > 11
    assert services.get_or_load_document_rga(doc.id).text() == "HELLO"


@pytest.mark.django_db
def test_concurrent_editing_during_revert_converges() -> None:
    """
    Client A reverts document from "ABC" to "A" (seq 1).
    Concurrently, Client B generates and applies an insert of "Z" after "A".
    Both sets of operations must merge conflict-free without error.
    """
    user = User.objects.create_user(username="concurrent_reverter")
    doc = Document.objects.create(title="Concurrent Revert", owner=user)

    # Build "ABC"
    c1 = CharId(1, "s1")
    c2 = CharId(2, "s1")
    c3 = CharId(3, "s1")
    services.apply_operation(doc.id, Op.create_insert("s1", 1, c1, ROOT, "A"), user)
    services.apply_operation(doc.id, Op.create_insert("s1", 2, c2, c1, "B"), user)
    services.apply_operation(doc.id, Op.create_insert("s1", 3, c3, c2, "C"), user)

    # 1. Client A generates revert ops to state at seq 1 ("A")
    revert_ops = services.generate_revert_operations(doc.id, target_seq=1, site_id="site_revert")

    # 2. Client B concurrently generates an insert of "Z" after "A"
    cz = CharId(10, "client_b")
    op_b = Op.create_insert("client_b", 10, cz, c1, "Z")

    # 3. Interleaved application: Apply op_b, then revert_ops
    services.apply_operation(doc.id, op_b, user)
    for rop in revert_ops:
        services.apply_operation(doc.id, rop, user)

    # Converged text should be "AZ" (since B and C were deleted by the revert, but Z was inserted after A)
    rga = services.get_or_load_document_rga(doc.id)
    assert rga.text() == "AZ"
