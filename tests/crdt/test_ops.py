"""
Unit tests for Op dataclass and serialization.
"""

import pytest

from crdt.ids import ROOT, CharId
from crdt.ops import Op, spans_from_ids


def test_create_insert_op_run() -> None:
    op = Op.create_insert(site_id="site1", char_id=CharId(4, "site1"), parent_id=ROOT, text="Hey")
    assert op.type == "insert"
    assert op.lamport == 6  # clock of the last character
    assert len(op) == 3
    assert list(op.inserted_ids()) == [CharId(4, "site1"), CharId(5, "site1"), CharId(6, "site1")]
    assert list(op.target_ids()) == []
    assert len(op.op_id) > 0


def test_create_delete_op_spans() -> None:
    op = Op.create_delete(
        site_id="site1", lamport=9, spans=[(CharId(1, "a"), 3), (CharId(7, "b"), 1)]
    )
    assert op.type == "delete"
    assert len(op) == 4
    assert list(op.target_ids()) == [CharId(1, "a"), CharId(2, "a"), CharId(3, "a"), CharId(7, "b")]
    assert op.text is None and op.char_id is None


def test_invalid_ops_raise() -> None:
    with pytest.raises(ValueError):
        Op.create_insert("s", CharId(1, "s"), ROOT, "")
    with pytest.raises(ValueError):
        Op(op_id="1", site_id="s", lamport=1, type="insert", char_id=CharId(1, "s"), text="A")
    with pytest.raises(ValueError):
        Op(op_id="1", site_id="s", lamport=1, type="delete", spans=())
    with pytest.raises(ValueError):
        Op(op_id="1", site_id="s", lamport=1, type="delete", spans=((CharId(1, "s"), 0),))
    with pytest.raises(ValueError):
        Op(
            op_id="1",
            site_id="s",
            lamport=1,
            type="delete",
            spans=((CharId(1, "s"), 1),),
            text="A",
        )


def test_op_dict_and_json_round_trip() -> None:
    ins = Op.create_insert(
        site_id="s_alpha",
        char_id=CharId(10, "s_alpha"),
        parent_id=CharId(9, "s_alpha"),
        text="Zz",
        op_id="custom-op-id-123",
    )
    d = ins.to_dict()
    assert d == {
        "op_id": "custom-op-id-123",
        "site_id": "s_alpha",
        "lamport": 11,
        "type": "insert",
        "char_id": "10@s_alpha",
        "parent_id": "9@s_alpha",
        "text": "Zz",
    }
    assert Op.from_dict(d) == ins
    assert Op.from_json(ins.to_json()) == ins

    dele = Op.create_delete("s", 12, [(CharId(10, "s_alpha"), 2)], op_id="d1")
    assert dele.to_dict()["spans"] == [["10@s_alpha", 2]]
    assert Op.from_dict(dele.to_dict()) == dele


def test_legacy_single_char_format_is_still_readable() -> None:
    """The op log in the database holds pre-RLE payloads; they must keep loading."""
    legacy_insert = {
        "op_id": "a",
        "site_id": "s",
        "lamport": 3,
        "type": "insert",
        "char_id": "3@s",
        "parent_id": "0@",
        "char": "x",
    }
    legacy_delete = {
        "op_id": "b",
        "site_id": "s",
        "lamport": 4,
        "type": "delete",
        "char_id": "3@s",
        "parent_id": None,
        "char": None,
    }
    ins = Op.from_dict(legacy_insert)
    assert ins.text == "x" and ins.char_id == CharId(3, "s") and ins.parent_id == ROOT
    dele = Op.from_dict(legacy_delete)
    assert list(dele.target_ids()) == [CharId(3, "s")]


def test_spans_from_ids_merges_consecutive_clocks_per_site() -> None:
    ids = [CharId(1, "a"), CharId(2, "a"), CharId(3, "a"), CharId(4, "b"), CharId(9, "a")]
    assert spans_from_ids(ids) == [(CharId(1, "a"), 3), (CharId(4, "b"), 1), (CharId(9, "a"), 1)]
