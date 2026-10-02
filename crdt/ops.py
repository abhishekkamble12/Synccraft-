"""
Operation dataclasses and serialization for CRDT synchronization.

Ops are run-length encoded:

* An **insert** carries a run of text. Character `i` of the run gets the id
  `(char_id.clock + i, site_id)` and its parent is character `i - 1` (the first
  character's parent is `parent_id`). Applying one run op is therefore exactly
  equivalent to applying `len(text)` single-character ops typed left to right,
  so the convergence argument for single characters carries over unchanged.
  `lamport` is the clock of the run's last character.
* A **delete** carries a list of `(first_id, count)` spans. A span covers the ids
  `first_id.clock .. first_id.clock + count - 1` of one site, which is how text
  typed in one burst is numbered, so deleting a typed word is a single span.

The pre-RLE wire format (one `char` per insert, one `char_id` per delete) is still
accepted by `from_dict`, because the op log in the database contains it.
"""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from crdt.ids import CharId

OpType = Literal["insert", "delete"]

Span = tuple[CharId, int]


@dataclass(frozen=True, slots=True)
class Op:
    """
    An immutable CRDT operation: an insert of a run of text, or a delete of spans of ids.

    Attributes:
        op_id: Globally unique idempotency key (e.g. UUID).
        site_id: Originating client or replica ID.
        lamport: Lamport timestamp of the op (for inserts, the clock of the last character).
        type: 'insert' or 'delete'.
        char_id: Insert only: id of the first character of the run.
        parent_id: Insert only: id of the character the run is inserted after (ROOT for start).
        text: Insert only: the inserted characters (at least one).
        spans: Delete only: `(first_id, count)` id ranges to tombstone.
    """

    op_id: str
    site_id: str
    lamport: int
    type: OpType
    char_id: CharId | None = None
    parent_id: CharId | None = None
    text: str | None = None
    spans: tuple[Span, ...] = ()

    def __post_init__(self) -> None:
        if self.type == "insert":
            if not self.text:
                raise ValueError("Insert operation must carry at least one character in 'text'.")
            if self.char_id is None or self.parent_id is None:
                raise ValueError("Insert operation must specify 'char_id' and 'parent_id'.")
            if self.spans:
                raise ValueError("Insert operation must not carry delete spans.")
        elif self.type == "delete":
            if not self.spans:
                raise ValueError("Delete operation must carry at least one span.")
            if any(count < 1 for _, count in self.spans):
                raise ValueError("Delete span counts must be positive.")
            if self.text is not None or self.char_id is not None or self.parent_id is not None:
                raise ValueError("Delete operation must not carry insert fields.")
        else:
            raise ValueError(f"Invalid operation type: {self.type}. Must be 'insert' or 'delete'.")

    # -------------------------------------------------------------------------
    # Factories
    # -------------------------------------------------------------------------

    @classmethod
    def create_insert(
        cls,
        site_id: str,
        char_id: CharId,
        parent_id: CharId,
        text: str,
        op_id: str | None = None,
    ) -> "Op":
        """Insert `text` after `parent_id`; ids run from `char_id` upward."""
        return cls(
            op_id=op_id or uuid.uuid4().hex,
            site_id=site_id,
            lamport=char_id.clock + len(text) - 1,
            type="insert",
            char_id=char_id,
            parent_id=parent_id,
            text=text,
        )

    @classmethod
    def create_delete(
        cls,
        site_id: str,
        lamport: int,
        spans: tuple[Span, ...] | list[Span],
        op_id: str | None = None,
    ) -> "Op":
        """Tombstone every id covered by `spans`."""
        return cls(
            op_id=op_id or uuid.uuid4().hex,
            site_id=site_id,
            lamport=lamport,
            type="delete",
            spans=tuple(spans),
        )

    # -------------------------------------------------------------------------
    # Derived views
    # -------------------------------------------------------------------------

    def __len__(self) -> int:
        """Number of characters this op inserts or deletes."""
        if self.type == "insert":
            return len(self.text or "")
        return sum(count for _, count in self.spans)

    def inserted_ids(self) -> Iterator[CharId]:
        """Ids assigned to the inserted characters, in order (empty for deletes)."""
        if self.type != "insert" or self.char_id is None or self.text is None:
            return
        first = self.char_id
        for i in range(len(self.text)):
            yield CharId(first.clock + i, first.site_id)

    def target_ids(self) -> Iterator[CharId]:
        """Ids a delete tombstones (empty for inserts)."""
        for first, count in self.spans:
            for i in range(count):
                yield CharId(first.clock + i, first.site_id)

    # -------------------------------------------------------------------------
    # Serialization
    # -------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert operation to a JSON-serializable dictionary."""
        base: dict[str, Any] = {
            "op_id": self.op_id,
            "site_id": self.site_id,
            "lamport": self.lamport,
            "type": self.type,
        }
        if self.type == "insert":
            base["char_id"] = str(self.char_id)
            base["parent_id"] = str(self.parent_id)
            base["text"] = self.text
        else:
            base["spans"] = [[str(first), count] for first, count in self.spans]
        return base

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Op":
        """Construct an Op from a serialized dictionary (current or legacy format)."""
        op_type = data["type"]
        if op_type == "insert":
            text = data["text"] if "text" in data else data.get("char")
            if not isinstance(text, str):
                raise ValueError("Insert op 'text' must be a string.")
            return cls(
                op_id=str(data["op_id"]),
                site_id=str(data["site_id"]),
                lamport=int(data["lamport"]),
                type="insert",
                char_id=_parse_id(data["char_id"]),
                parent_id=_parse_id(data["parent_id"]),
                text=text,
            )
        if op_type == "delete":
            if "spans" in data:
                raw_spans = data["spans"]
                if not isinstance(raw_spans, list):
                    raise ValueError("Delete op 'spans' must be a list.")
                spans = tuple((_parse_id(first), int(count)) for first, count in raw_spans)
            else:
                spans = ((_parse_id(data["char_id"]), 1),)
            return cls(
                op_id=str(data["op_id"]),
                site_id=str(data["site_id"]),
                lamport=int(data["lamport"]),
                type="delete",
                spans=spans,
            )
        raise ValueError(f"Invalid operation type: {op_type!r}")

    def to_json(self) -> str:
        """Serialize operation to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "Op":
        """Deserialize operation from JSON string."""
        return cls.from_dict(json.loads(s))


def _parse_id(raw: Any) -> CharId:
    if isinstance(raw, str):
        return CharId.from_str(raw)
    if isinstance(raw, dict):
        return CharId(clock=int(raw["clock"]), site_id=str(raw["site_id"]))
    raise ValueError(f"Invalid CharId: {raw!r}")


def spans_from_ids(ids: list[CharId]) -> list[Span]:
    """Collapse ids into maximal `(first, count)` runs of consecutive same-site clocks."""
    spans: list[Span] = []
    for cid in ids:
        if spans:
            first, count = spans[-1]
            if first.site_id == cid.site_id and first.clock + count == cid.clock:
                spans[-1] = (first, count + 1)
                continue
        spans.append((cid, 1))
    return spans
