"""
Unit tests for LamportClock.
"""

from concurrent.futures import ThreadPoolExecutor
import pytest
from crdt.clock import LamportClock


def test_clock_initial_value() -> None:
    clock = LamportClock()
    assert clock.value == 0
    assert clock.peek() == 0

    custom_clock = LamportClock(42)
    assert custom_clock.value == 42


def test_clock_negative_init_raises() -> None:
    with pytest.raises(ValueError):
        LamportClock(-1)


def test_clock_tick() -> None:
    clock = LamportClock()
    assert clock.tick() == 1
    assert clock.tick() == 2
    assert clock.value == 2


def test_clock_update() -> None:
    clock = LamportClock(5)
    # Remote timestamp smaller than local: advances local + 1
    new_ts = clock.update(2)
    assert new_ts == 6
    assert clock.value == 6

    # Remote timestamp larger than local: advances remote + 1
    new_ts = clock.update(10)
    assert new_ts == 11
    assert clock.value == 11


def test_clock_thread_safety() -> None:
    clock = LamportClock()
    iterations = 1000

    def worker() -> None:
        for _ in range(iterations):
            clock.tick()

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker) for _ in range(4)]
        for f in futures:
            f.result()

    assert clock.value == 4 * iterations
