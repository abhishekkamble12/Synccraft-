"""
CRDT micro-benchmarks: the original implementation (benchmarks/legacy, v1) against
the current one (crdt/), on the same machine, workloads and seeds.

  1. Positional edits   local insert/delete at a random visible position (what an
                        editor does per keystroke), and position <-> id lookups,
                        at document sizes 1k / 10k / 100k.
  2. Paste              k characters pasted into the middle of a 100k-char document.
  3. Remote apply       integrating a stream of remote ops (the server's hot path).
  4. Snapshot / init    bytes of the state a client downloads on connect.

    python -m benchmarks.crdt_bench            # writes docs/benchmarks/crdt.json
"""

import json
import platform
import random
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from benchmarks.legacy.ids import CharId as LegacyCharId
from benchmarks.legacy.rga import RGA as LegacyRGA
from crdt.rga import RGA

OUT = Path(__file__).resolve().parent.parent / "docs" / "benchmarks" / "crdt.json"


def build_new(n: int) -> RGA:
    return RGA.from_dict({"site_id": "seed", "clock": n, "runs": [["1@seed", "x" * n, 0]]}, "bench")


def build_legacy(n: int) -> LegacyRGA:
    nodes = [
        {
            "char_id": f"{i}@seed",
            "char": "x",
            "deleted": False,
            "parent_id": f"{i - 1}@seed" if i > 1 else "0@",
        }
        for i in range(1, n + 1)
    ]
    return LegacyRGA.from_dict({"site_id": "seed", "clock": n, "nodes": nodes}, "bench")


def per_op_us(fn: Callable[[], object], repeat: int) -> float:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1e6)
    return round(statistics.median(samples), 2)


def positional(n: int, repeat: int) -> dict[str, Any]:
    out: dict[str, Any] = {"doc_chars": n}
    for name, doc in (("legacy", build_legacy(n)), ("current", build_new(n))):
        rng = random.Random(n)

        def insert(d: Any = doc) -> None:
            d.local_insert(rng.randint(0, d.visible_len()), "y")

        def delete(d: Any = doc) -> None:
            d.local_delete(rng.randint(0, d.visible_len() - 1))

        def lookup(d: Any = doc) -> None:
            d.pos_of_char_id(d.char_id_at(rng.randint(0, d.visible_len() - 1)))

        out[name] = {
            "insert_us": per_op_us(insert, repeat),
            "delete_us": per_op_us(delete, repeat),
            "id_lookup_roundtrip_us": per_op_us(lookup, repeat),
        }
    out["insert_speedup"] = round(out["legacy"]["insert_us"] / out["current"]["insert_us"], 1)
    return out


def paste(n: int, k: int) -> dict[str, Any]:
    text = "".join(random.Random(k).choice("abcdefgh ") for _ in range(k))
    legacy = build_legacy(n)
    start = time.perf_counter()
    legacy_ops = [legacy.local_insert(n // 2 + i, ch) for i, ch in enumerate(text)]
    legacy_ms = (time.perf_counter() - start) * 1000

    current = build_new(n)
    start = time.perf_counter()
    op = current.local_insert(n // 2, text)
    current_ms = (time.perf_counter() - start) * 1000
    assert legacy.text() == current.text()
    return {
        "doc_chars": n,
        "pasted_chars": k,
        "legacy": {
            "ms": round(legacy_ms, 1),
            "ops": len(legacy_ops),
            "wire_bytes": sum(len(o.to_json()) for o in legacy_ops),
        },
        "current": {"ms": round(current_ms, 2), "ops": 1, "wire_bytes": len(op.to_json())},
        "speedup": round(legacy_ms / current_ms, 1),
    }


def remote_apply(ops_count: int) -> dict[str, Any]:
    """A typist's single-character ops, integrated by a fresh replica (server path)."""
    rng = random.Random(9)
    author_new, author_old = RGA("typist"), LegacyRGA("typist")
    new_ops, old_ops = [], []
    for _ in range(ops_count):
        length = author_new.visible_len()
        if length and rng.random() < 0.15:
            pos = rng.randint(0, length - 1)
            new_ops.append(author_new.local_delete(pos))
            old_ops.append(author_old.local_delete(pos))
        else:
            pos, ch = rng.randint(0, length), rng.choice("abc ")
            new_ops.append(author_new.local_insert(pos, ch))
            old_ops.append(author_old.local_insert(pos, ch))

    results = {}
    for name, factory, ops in (("legacy", LegacyRGA, old_ops), ("current", RGA, new_ops)):
        replica = factory("server")
        start = time.perf_counter()
        for op in ops:
            replica.apply(op)
        results[name] = {"us_per_op": round((time.perf_counter() - start) * 1e6 / len(ops), 2)}
    return {"ops": ops_count, **results}


def snapshot_sizes() -> dict[str, Any]:
    def size(obj: Any) -> int:
        return len(json.dumps(obj, separators=(",", ":")))

    # (a) The audit case: 20,000 characters typed, then all deleted.
    legacy = LegacyRGA("s")
    for i in range(20_000):
        legacy.local_insert(i, "x")
    for _ in range(20_000):
        legacy.local_delete(0)
    current = RGA("s")
    current.local_insert(0, "x" * 20_000)
    current.local_delete(0, 20_000)
    uncompacted = size(current.to_dict())
    current.compact()
    erased = {
        # v1's init frame carried the text *and* the snapshot.
        "legacy_init_bytes": size({"text": legacy.text(), "snapshot": legacy.to_dict()}),
        "current_init_bytes_before_gc": uncompacted,
        "current_init_bytes_after_gc": size(current.to_dict()),
    }

    # (b) A realistic session: bursts typed at random places, 15% deletes. Both
    # implementations replay the same edit script (v1 one character at a time).
    rng = random.Random(5)
    legacy, current = LegacyRGA("s"), RGA("s")
    typed = deleted = 0
    while current.visible_len() < 20_000:
        length = current.visible_len()
        if length > 20 and rng.random() < 0.15:
            pos, count = rng.randint(0, length - 10), rng.randint(1, 10)
            current.local_delete(pos, count)
            for _ in range(count):
                legacy.local_delete(pos)
            deleted += count
        else:
            pos = rng.randint(0, length)
            word = "".join(rng.choice("etaoin shrdlu") for _ in range(rng.randint(1, 30)))
            current.local_insert(pos, word)
            for i, ch in enumerate(word):
                legacy.local_insert(pos + i, ch)
            typed += len(word)
    assert legacy.text() == current.text()
    uncompacted = size(current.to_dict())
    tombstones = current.total_len() - current.visible_len()
    current.compact()
    realistic = {
        "visible_chars": current.visible_len(),
        "chars_typed": typed,
        "chars_deleted": deleted,
        "tombstones_before_gc": tombstones,
        "legacy_init_bytes": size({"text": legacy.text(), "snapshot": legacy.to_dict()}),
        "current_init_bytes_before_gc": uncompacted,
        "current_init_bytes_after_gc": size(current.to_dict()),
    }
    return {"typed_then_erased_20k": erased, "realistic_20k": realistic}


def main() -> None:
    results: dict[str, Any] = {
        "machine": f"{platform.processor()} / {platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "positional": [positional(n, repeat) for n, repeat in ((1_000, 400), (10_000, 200), (100_000, 50))],
        "paste": [paste(100_000, 1_000), paste(100_000, 10_000)],
        "remote_apply": remote_apply(20_000),
        "snapshot": snapshot_sizes(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
