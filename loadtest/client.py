"""
A protocol-complete simulated editor, shared by the load test and the chaos harness.

It speaks the same protocol as static/js/sync.js + editor.js:

* every op is sent with the `base_seq` it was created from and kept in `pending`
  until acked; a dropped connection keeps typing offline;
* on (re)connect it takes the server's `init`, replays its pending ops onto it
  (or rebases them under a fresh site id if they were rejected or are older than
  the GC point) and sends `sync`;
* it tracks the highest contiguous seq, repairs gaps, and reports its GC
  watermark with `stable`.

Propagation latency is measured against a process-wide send-time table, so all
clients must live in one process.
"""

import asyncio
import contextlib
import json
import random
import secrets
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from crdt.ops import Op
from crdt.rebase import rebase_pending
from crdt.rga import RGA

# Matches static/js/sync.js.
GAP_REPAIR_DELAY_SEC = 1.0
STABLE_INTERVAL_SEC = 2.0

# op_id -> perf_counter() at send time, shared by all clients in this process
SEND_TIMES: dict[str, float] = {}


@dataclass
class ClientStats:
    ops_sent: int = 0
    chars_sent: int = 0
    acked: int = 0
    rejected: int = 0
    rebases: int = 0
    reconnects: int = 0
    gap_repairs: int = 0
    ack_rtts: list[float] = field(default_factory=list)
    propagation: list[float] = field(default_factory=list)
    last_ack_at: float = 0.0


class SimulatedClient:
    def __init__(self, name: str, ws_url: str, headers: dict[str, str]) -> None:
        self.name = name
        self.ws_url = ws_url
        self.headers = headers
        self.site_id = f"{name}-{secrets.token_hex(3)}"
        self.rga = RGA(site_id=self.site_id)
        self.ws: Any = None
        self.stats = ClientStats()

        # op_id -> (op, base_seq), in creation order
        self.pending: dict[str, tuple[Op, int]] = {}
        self.contiguous = 0
        self.seen_ahead: set[int] = set()
        self.rebase_required = False
        self.resync_requested = False
        self.ready = asyncio.Event()  # set once init + sync handshake completed
        self.closing = False
        self.offline_until = 0.0  # partition(): no reconnect before this time
        self._gap_task: asyncio.Task[None] | None = None
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Connect and keep the connection alive (reconnecting) until `close()`."""
        await self._connect_once()
        self._tasks.append(asyncio.create_task(self._run()))
        self._tasks.append(asyncio.create_task(self._report_stable()))

    async def close(self) -> None:
        self.closing = True
        if self.ws is not None:
            await self.ws.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def partition(self, seconds: float) -> None:
        """Cut this client off for `seconds`: the socket dies, typing continues offline."""
        self.offline_until = time.monotonic() + seconds
        self.ready.clear()
        if self.ws is not None:
            await self.ws.close()

    async def _connect_once(self) -> None:
        self.ws = await websockets.connect(
            self.ws_url, additional_headers=self.headers, max_size=None, open_timeout=10
        )
        self.ready.clear()

    async def _run(self) -> None:
        backoff = 0.2
        while not self.closing:
            with contextlib.suppress(TimeoutError, ConnectionClosed, OSError):
                await self._listen()
            if self.closing:
                return
            self.ready.clear()
            self.resync_requested = False
            await asyncio.sleep(max(0.0, self.offline_until - time.monotonic()))
            await asyncio.sleep(backoff * random.uniform(0.75, 1.25))
            try:
                await self._connect_once()
                self.stats.reconnects += 1
                backoff = 0.2
            except (TimeoutError, OSError, websockets.InvalidStatus, websockets.InvalidHandshake):
                backoff = min(backoff * 2, 5.0)

    async def _report_stable(self) -> None:
        last = -1
        while not self.closing:
            await asyncio.sleep(STABLE_INTERVAL_SEC)
            floor = min([self.contiguous, *(base for _, base in self.pending.values())])
            if (
                floor != last
                and self.ready.is_set()
                and await self._send({"type": "stable", "seq": floor})
            ):
                last = floor

    # ------------------------------------------------------------------ sending

    async def _send(self, payload: dict[str, Any]) -> bool:
        try:
            await self.ws.send(json.dumps(payload))
            return True
        except (ConnectionClosed, AttributeError):
            return False

    async def submit(self, op: Op) -> None:
        """Record a locally applied op and send it (or keep it for the next sync)."""
        base = self.contiguous
        self.pending[op.op_id] = (op, base)
        SEND_TIMES[op.op_id] = time.perf_counter()
        self.stats.ops_sent += 1
        self.stats.chars_sent += len(op)
        if self.ready.is_set() and not self.rebase_required:
            await self._send({"type": "op", "op": op.to_dict(), "base_seq": base})

    async def type_one(self, rng: random.Random, paste_chance: float = 0.0) -> None:
        length = self.rga.visible_len()
        if length > 0 and rng.random() < 0.15:
            pos = rng.randint(0, length - 1)
            op = self.rga.local_delete(pos, min(rng.choice([1, 1, 1, 3]), length - pos))
        elif rng.random() < paste_chance:
            text = "".join(rng.choice("abcdefghij klmnop") for _ in range(rng.randint(50, 400)))
            op = self.rga.local_insert(rng.randint(0, length), text)
        else:
            op = self.rga.local_insert(
                rng.randint(0, length), rng.choice("abcdefghijklmnopqrstuvwxyz ")
            )
        await self.submit(op)

    # ------------------------------------------------------------------ receiving

    def _mark_seq(self, seq: int | None) -> None:
        if not seq or seq <= self.contiguous:
            return
        self.seen_ahead.add(seq)
        while self.contiguous + 1 in self.seen_ahead:
            self.seen_ahead.discard(self.contiguous + 1)
            self.contiguous += 1
        if self.seen_ahead and (self._gap_task is None or self._gap_task.done()):
            self._gap_task = asyncio.create_task(self._repair_gap())

    async def _repair_gap(self) -> None:
        await asyncio.sleep(GAP_REPAIR_DELAY_SEC)
        if self.seen_ahead and self.ready.is_set():
            self.stats.gap_repairs += 1
            await self._send_sync("gap")

    async def _send_sync(self, reason: str) -> None:
        await self._send(
            {
                "type": "sync",
                "reason": reason,
                "site_id": self.site_id,
                "last_seq": self.contiguous,
                "pending": [
                    {"op": op.to_dict(), "base_seq": base} for op, base in self.pending.values()
                ],
            }
        )

    async def _request_resync(self) -> None:
        self.rebase_required = True
        if not self.resync_requested:
            self.resync_requested = await self._send({"type": "resync"})

    def _on_init(self, msg: dict[str, Any]) -> None:
        head, gc = msg["head_seq"], msg.get("gc_seq", 0)
        fresh = RGA.from_dict(msg["snapshot"], site_id=self.site_id)
        must_rebase = bool(self.pending) and (
            self.rebase_required or any(base < gc for _, base in self.pending.values())
        )
        if must_rebase:
            self.stats.rebases += 1
            self.site_id = f"{self.name}-{secrets.token_hex(3)}"
            fresh.site_id = self.site_id
            ops = rebase_pending(self.rga, fresh, [op for op, _ in self.pending.values()])
            self.pending = {op.op_id: (op, head) for op in ops}
            for op in ops:
                SEND_TIMES[op.op_id] = time.perf_counter()
        else:
            for op, _ in self.pending.values():
                fresh.apply(op)
        self.rga = fresh
        self.contiguous = head
        self.seen_ahead.clear()
        self.rebase_required = False
        self.resync_requested = False

    async def _listen(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            now = time.perf_counter()
            if kind == "init":
                self._on_init(msg)
                await self._send_sync("reconnect")
                self.ready.set()
            elif kind == "ack":
                self.stats.acked += 1
                self.stats.last_ack_at = now
                self.pending.pop(msg["op_id"], None)
                sent = SEND_TIMES.get(msg["op_id"])
                if sent is not None:
                    self.stats.ack_rtts.append((now - sent) * 1000)
                self._mark_seq(msg.get("seq"))
            elif kind == "ops":
                for item in msg["ops"]:
                    op = Op.from_dict(item["op"])
                    self.rga.apply(op)
                    sent = SEND_TIMES.get(op.op_id)
                    if sent is not None:
                        self.stats.propagation.append((now - sent) * 1000)
                    self._mark_seq(item["seq"])
            elif kind == "head":
                self._mark_seq(msg["seq"])  # behind the log -> schedules a gap repair
            elif kind == "sync_ack":
                for op_id in msg.get("acked", []):
                    if self.pending.pop(op_id, None) is not None:
                        self.stats.acked += 1
                        self.stats.last_ack_at = now
                for missed in msg.get("missed", []):
                    self.rga.apply(Op.from_dict(missed["op"]))
                    self._mark_seq(missed["seq"])
                if msg.get("rejected"):
                    self.stats.rejected += len(msg["rejected"])
                    await self._request_resync()
            elif kind == "error" and msg.get("code") == "op_rejected":
                self.stats.rejected += 1
                await self._request_resync()
            elif kind == "error":
                print(f"[{self.name}] server error: {msg}", file=sys.stderr)

    # ------------------------------------------------------------------ status

    @property
    def settled(self) -> bool:
        return self.ready.is_set() and not self.pending and not self.seen_ahead
