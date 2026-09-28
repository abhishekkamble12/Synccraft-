"""
Character identifiers for CRDT total ordering.

Globally unique identifier represented by a pair: (lamport_clock, site_id).
"""

from dataclasses import dataclass
from functools import total_ordering
from typing import Any


@total_ordering
@dataclass(frozen=True, slots=True)
class CharId:
    """
    Globally unique, immutable character identifier in an RGA CRDT.

    Attributes:
        clock: Lamport logical clock counter.
        site_id: Globally unique client or replica identifier.
    """

    clock: int
    site_id: str

    def __lt__(self, other: Any) -> bool:
        if not isinstance(other, CharId):
            return NotImplemented
        # Total ordering: higher clock is greater; tie-break with site_id
        if self.clock != other.clock:
            return self.clock < other.clock
        return self.site_id < other.site_id

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, CharId):
            return NotImplemented
        return self.clock == other.clock and self.site_id == other.site_id

    def __hash__(self) -> int:
        return hash((self.clock, self.site_id))

    def __str__(self) -> str:
        return f"{self.clock}@{self.site_id}"

    @classmethod
    def from_str(cls, s: str) -> "CharId":
        """
        Parse a CharId from its string representation 'clock@site_id'.
        """
        if "@" not in s:
            raise ValueError(f"Invalid CharId format: {s}. Expected 'clock@site_id'.")
        parts = s.split("@", 1)
        return cls(clock=int(parts[0]), site_id=parts[1])


# Sentinel root identifier representing the start of document (before first char)
ROOT = CharId(clock=0, site_id="")
