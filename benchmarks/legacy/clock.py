"""
Lamport logical clock for causal ordering in distributed systems.

Thread-safe implementation with monotonic increment and convergence on update.
"""

from threading import Lock


class LamportClock:
    """
    A thread-safe Lamport logical clock.

    Each process/client maintains a clock counter.
    - Local events increment the clock (tick).
    - Remote events advance the clock to max(local, remote) + 1 (update).
    """

    def __init__(self, initial_value: int = 0) -> None:
        if initial_value < 0:
            raise ValueError("LamportClock initial_value must be non-negative.")
        self._value: int = initial_value
        self._lock: Lock = Lock()

    @property
    def value(self) -> int:
        """Current value of the logical clock."""
        with self._lock:
            return self._value

    def tick(self) -> int:
        """
        Advance the clock for a local event and return the new timestamp.
        """
        with self._lock:
            self._value += 1
            return self._value

    def update(self, remote_timestamp: int) -> int:
        """
        Advance the clock on receiving a message with a remote timestamp.
        Sets clock = max(local_clock, remote_timestamp) + 1.
        """
        if remote_timestamp < 0:
            raise ValueError("remote_timestamp must be non-negative.")
        with self._lock:
            self._value = max(self._value, remote_timestamp) + 1
            return self._value

    def peek(self) -> int:
        """Non-incrementing read of the current clock value."""
        with self._lock:
            return self._value

    def __repr__(self) -> str:
        return f"LamportClock({self.value})"
