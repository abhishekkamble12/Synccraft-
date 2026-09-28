"""
Test vector verification test runner in Python.
"""

import json
from pathlib import Path
import pytest
from crdt.ops import Op
from crdt.rga import RGA

VECTORS_DIR = Path(__file__).resolve().parent


def test_python_vector_verification() -> None:
    vector_files = list(VECTORS_DIR.glob("*.json"))
    if not vector_files:
        # If not yet generated, generate now
        from tests.vectors.generate_vectors import main
        main()
        vector_files = list(VECTORS_DIR.glob("*.json"))

    assert len(vector_files) > 0

    for vf in vector_files:
        with open(vf, "r", encoding="utf-8") as f:
            data = json.load(f)

        replica = RGA(site_id="verifier")
        for op_dict in data["operations"]:
            op = Op.from_dict(op_dict)
            replica.apply(op)

        assert replica.text() == data["expected_text"], (
            f"Vector {vf.name} failed: got {replica.text()!r}, expected {data['expected_text']!r}"
        )
        assert replica.visible_len() == data["expected_len"]
