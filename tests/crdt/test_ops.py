"""
Unit tests for Op dataclass and serialization.
"""

import pytest
from crdt.ids import CharId, ROOT
from crdt.ops import Op


def test_create_insert_op() -> None:
    char_id = CharId(1, "site1")
    op = Op.create_insert(
        site_id="site1",
        lamport=1,
        char_id=char_id,
        parent_id=ROOT,
        char="H",
    )
    assert op.type == "insert"
    assert op.site_id == "site1"
    assert op.lamport == 1
    assert op.char_id == char_id
    assert op.parent_id == ROOT
    assert op.char == "H"
    assert len(op.op_id) > 0


def test_create_delete_op() -> None:
    char_id = CharId(1, "site1")
    op = Op.create_delete(
        site_id="site1",
        lamport=2,
        char_id=char_id,
    )
    assert op.type == "delete"
    assert op.site_id == "site1"
    assert op.lamport == 2
    assert op.char_id == char_id
    assert op.parent_id is None
    assert op.char is None


def test_invalid_ops_raise() -> None:
    char_id = CharId(1, "site1")
    # Multi-char insert
    with pytest.raises(ValueError):
        Op.create_insert("s", 1, char_id, ROOT, "HELLO")

    # Insert missing parent
    with pytest.raises(ValueError):
        Op(
            op_id="1",
            site_id="s",
            lamport=1,
            type="insert",
            char_id=char_id,
            parent_id=None,
            char="A",
        )

    # Delete with char
    with pytest.raises(ValueError):
        Op(
            op_id="1",
            site_id="s",
            lamport=1,
            type="delete",
            char_id=char_id,
            parent_id=None,
            char="A",
        )


def test_op_dict_and_json_serialization() -> None:
    char_id = CharId(10, "s_alpha")
    parent_id = CharId(9, "s_alpha")
    op = Op.create_insert(
        site_id="s_alpha",
        lamport=10,
        char_id=char_id,
        parent_id=parent_id,
        char="Z",
        op_id="custom-op-id-123",
    )

    d = op.to_dict()
    assert d["op_id"] == "custom-op-id-123"
    assert d["char_id"] == "10@s_alpha"
    assert d["parent_id"] == "9@s_alpha"

    restored = Op.from_dict(d)
    assert restored == op

    json_str = op.to_json()
    assert isinstance(json_str, str)
    restored_json = Op.from_json(json_str)
    assert restored_json == op
