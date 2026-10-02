"""
Replays the shared cross-language vectors through the Python implementation.
(tests/vectors/test_rga_js.mjs does the same for static/js/rga.js.)
"""

import json
from pathlib import Path

import pytest

from crdt.clock import LamportClock
from crdt.ops import Op
from crdt.rebase import rebase_pending
from crdt.rga import RGA

VECTORS_DIR = Path(__file__).resolve().parent
VECTOR_FILES = sorted(VECTORS_DIR.glob("*.json"))


def node_order(rga: RGA) -> list[list[object]]:
    return [[str(n.char_id), n.char, n.deleted] for n in rga.iter_nodes()]


@pytest.mark.parametrize("path", VECTOR_FILES, ids=lambda p: p.stem)
def test_vector(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))

    if data.get("kind") == "rebase":
        old = RGA(data["old_site"])
        for raw in [*data["shared"], *data["pending"]]:
            old.apply(Op.from_dict(raw))
        old.clock = LamportClock(data["old_clock"])
        fresh = RGA.from_dict(data["server_snapshot"], site_id=data["new_site"])
        rebase_pending(old, fresh, [Op.from_dict(raw) for raw in data["pending"]])
        assert fresh.text() == data["expected_text"]
        assert node_order(fresh) == data["expected_order"]
        return

    replica = RGA(site_id="verifier")
    for raw in data["operations"]:
        replica.apply(Op.from_dict(raw))
    assert replica.text() == data["expected_text"]
    assert replica.visible_len() == data["expected_len"]
    if "expected_order" in data:
        assert node_order(replica) == data["expected_order"]
        assert replica.to_dict()["runs"] == data["expected_runs"]
        assert replica.version_vector == data["expected_vv"]
