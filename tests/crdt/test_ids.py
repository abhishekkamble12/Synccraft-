"""
Unit tests for CharId and ROOT sentinel.
"""

import pytest
from crdt.ids import CharId, ROOT


def test_root_sentinel() -> None:
    assert ROOT.clock == 0
    assert ROOT.site_id == ""
    assert str(ROOT) == "0@"


def test_char_id_creation_and_fields() -> None:
    cid = CharId(clock=5, site_id="client_a")
    assert cid.clock == 5
    assert cid.site_id == "client_a"


def test_char_id_immutability() -> None:
    cid = CharId(clock=1, site_id="a")
    with pytest.raises(Exception):
        cid.clock = 2  # type: ignore[misc]


def test_char_id_ordering_clock_precedence() -> None:
    cid1 = CharId(clock=1, site_id="z")
    cid2 = CharId(clock=2, site_id="a")
    assert cid1 < cid2
    assert cid2 > cid1
    assert not (cid1 >= cid2)
    assert not (cid2 <= cid1)


def test_char_id_ordering_site_tie_breaking() -> None:
    cid_a = CharId(clock=3, site_id="site_a")
    cid_b = CharId(clock=3, site_id="site_b")
    assert cid_a < cid_b
    assert cid_b > cid_a


def test_char_id_equality_and_hashing() -> None:
    cid1 = CharId(clock=4, site_id="site_1")
    cid2 = CharId(clock=4, site_id="site_1")
    cid3 = CharId(clock=4, site_id="site_2")

    assert cid1 == cid2
    assert cid1 != cid3
    assert hash(cid1) == hash(cid2)

    id_set = {cid1}
    assert cid2 in id_set
    assert cid3 not in id_set


def test_char_id_string_serialization() -> None:
    cid = CharId(clock=42, site_id="peer_node_99")
    serialized = str(cid)
    assert serialized == "42@peer_node_99"

    deserialized = CharId.from_str(serialized)
    assert deserialized == cid
    assert deserialized.clock == 42
    assert deserialized.site_id == "peer_node_99"


def test_char_id_invalid_string_parsing() -> None:
    with pytest.raises(ValueError):
        CharId.from_str("invalid_string_no_at")
