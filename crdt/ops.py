"""
Operation dataclasses and serialization for CRDT synchronization.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from crdt.ids import CharId

OpType = Literal["insert", "delete"]


@dataclass(frozen=True, slots=True)
class Op:
    """
    An immutable CRDT operation representing an insertion or deletion.

    Attributes:
        op_id: Globally unique idempotency key (e.g. UUID).
        site_id: Originating client or replica ID.
        lamport: Lamport timestamp at creation time.
        type: 'insert' or 'delete'.
        char_id: CharId of the character being inserted or deleted.
        parent_id: CharId of the preceding character for insertions (ROOT for start).
        char: Single character string for insertions.
    """

    op_id: str
    site_id: str
    lamport: int
    type: OpType
    char_id: CharId
    parent_id: CharId | None = None
    char: str | None = None

    def __post_init__(self) -> None:
        if self.type not in ("insert", "delete"):
            raise ValueError(f"Invalid operation type: {self.type}. Must be 'insert' or 'delete'.")
        if self.type == "insert":
            if self.char is None or len(self.char) != 1:
                raise ValueError("Insert operation must specify a single character in 'char'.")
            if self.parent_id is None:
                raise ValueError("Insert operation must specify a 'parent_id'.")
        if self.type == "delete" and self.char is not None:
            raise ValueError("Delete operation must have char=None.")

    @classmethod
    def create_insert(
        cls,
        site_id: str,
        lamport: int,
        char_id: CharId,
        parent_id: CharId,
        char: str,
        op_id: str | None = None,
    ) -> "Op":
        """Factory method for creating an insert operation."""
        return cls(
            op_id=op_id or uuid.uuid4().hex,
            site_id=site_id,
            lamport=lamport,
            type="insert",
            char_id=char_id,
            parent_id=parent_id,
            char=char,
        )

    @classmethod
    def create_delete(
        cls,
        site_id: str,
        lamport: int,
        char_id: CharId,
        op_id: str | None = None,
    ) -> "Op":
        """Factory method for creating a delete operation."""
        return cls(
            op_id=op_id or uuid.uuid4().hex,
            site_id=site_id,
            lamport=lamport,
            type="delete",
            char_id=char_id,
            parent_id=None,
            char=None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert operation to a JSON-serializable dictionary."""
        return {
            "op_id": self.op_id,
            "site_id": self.site_id,
            "lamport": self.lamport,
            "type": self.type,
            "char_id": str(self.char_id),
            "parent_id": str(self.parent_id) if self.parent_id is not None else None,
            "char": self.char,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Op":
        """Construct an Op from a serialized dictionary."""
        char_id = (
            CharId.from_str(data["char_id"])
            if isinstance(data["char_id"], str)
            else CharId(clock=data["char_id"]["clock"], site_id=data["char_id"]["site_id"])
        )
        parent_id: CharId | None = None
        if data.get("parent_id") is not None:
            if isinstance(data["parent_id"], str):
                parent_id = CharId.from_str(data["parent_id"])
            else:
                parent_id = CharId(
                    clock=data["parent_id"]["clock"], site_id=data["parent_id"]["site_id"]
                )

        return cls(
            op_id=data["op_id"],
            site_id=data["site_id"],
            lamport=int(data["lamport"]),
            type=data["type"],
            char_id=char_id,
            parent_id=parent_id,
            char=data.get("char"),
        )

    def to_json(self) -> str:
        """Serialize operation to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "Op":
        """Deserialize operation from JSON string."""
        return cls.from_dict(json.loads(s))
