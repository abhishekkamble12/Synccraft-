"""
Concurrent WebSocket load test for the collaborative sync engine.

N simulated clients, each with its own local RGA replica, type into the same
document at human-ish speed. Because every client lives in this one process,
send and receive timestamps share a clock, so we measure true propagation
latency (client A sends -> client B receives the broadcast), not just ack RTT.

At the end every client replica *and* the server's persisted text must be
identical; the script exits non-zero otherwise.

    python -m loadtest.swarm --url http://localhost --clients 50 --ops 40
"""

import argparse
import asyncio
import json
import random
import re
import secrets
import sys
import time
from typing import Any

import httpx
import websockets

from crdt.ops import Op
from crdt.rga import RGA

# Matches static/js/sync.js: wait this long before treating a seq gap as a drop.
GAP_REPAIR_DELAY_SEC = 1.0

# op_id -> perf_counter() at send time, shared by all clients in this process
SEND_TIMES: dict[str, float] = {}


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered)) - 1))
    return round(ordered[idx], 2)


async def login_and_create_doc(base_url: str, username: str, password: str) -> tuple[str, str]:
    """Register (or log in) a user and create a fresh document. Returns (sessionid, doc_id)."""
    async with httpx.AsyncClient(base_url=base_url, follow_redirects=False, timeout=30) as http:

        async def post_form(path: str, data: dict[str, str]) -> httpx.Response:
            await http.get(path)
            token = http.cookies.get("csrftoken", "")
            return await http.post(
                path,
                data={**data, "csrfmiddlewaretoken": token},
                headers={"Referer": f"{base_url}{path}"},
            )

        await post_form(
            "/accounts/register/",
            {"username": username, "password1": password, "password2": password},
        )
        if "sessionid" not in http.cookies:
            resp = await post_form("/accounts/login/", {"username": username, "password": password})
            if "sessionid" not in http.cookies:
                raise RuntimeError(f"Login failed ({resp.status_code}); check credentials.")

        token = http.cookies.get("csrftoken", "")
        resp = await http.post(
            "/docs/create/",
            data={"title": f"loadtest {time.strftime('%H:%M:%S')}", "csrfmiddlewaretoken": token},
            headers={"Referer": f"{base_url}/"},
        )
        match = re.search(r"/docs/([0-9a-f-]{36})/", resp.headers.get("location", ""))
        if not match:
            raise RuntimeError(f"Document creation failed ({resp.status_code}).")
        return http.cookies["sessionid"], match.group(1)


async def fetch_server_text(base_url: str, sessionid: str, doc_id: str) -> tuple[int, str]:
    async with httpx.AsyncClient(
        base_url=base_url, cookies={"sessionid": sessionid}, timeout=30
    ) as http:
        head = (await http.get(f"/api/docs/{doc_id}/history/?after=0")).json()["head_seq"]
        text = (await http.get(f"/api/docs/{doc_id}/at/{head}/")).json()["text"]
        return head, text


class SimulatedClient:
    def __init__(self, client_id: str, ws_url: str, headers: dict[str, str]) -> None:
        self.client_id = client_id
        self.ws_url = ws_url
        self.headers = headers
        self.rga = RGA(site_id=client_id)
        self.ws: Any = None
        self.ops_sent = 0
        self.acked = 0
        self.ack_rtts: list[float] = []
        self.propagation: list[float] = []
        self.last_ack_at = 0.0
        # Same contiguous-seq tracking as static/js/sync.js: re-sync on persistent gaps.
        self.contiguous = 0
        self.seen_ahead: set[int] = set()
        self.gap_task: asyncio.Task[None] | None = None
        self.gap_repairs = 0

    async def connect(self) -> None:
        self.ws = await websockets.connect(
            self.ws_url, additional_headers=self.headers, max_size=None
        )
        init = json.loads(await self.ws.recv())
        if init.get("type") != "init":
            raise RuntimeError(f"{self.client_id}: expected init, got {init}")
        self.rga = RGA.from_dict(init["snapshot"], site_id=self.client_id)
        self.contiguous = init["head_seq"]

    def mark_seq(self, seq: int | None) -> None:
        if not seq or seq <= self.contiguous:
            return
        self.seen_ahead.add(seq)
        while self.contiguous + 1 in self.seen_ahead:
            self.seen_ahead.discard(self.contiguous + 1)
            self.contiguous += 1
        if self.seen_ahead and (self.gap_task is None or self.gap_task.done()):
            self.gap_task = asyncio.create_task(self.repair_gap())

    async def repair_gap(self) -> None:
        await asyncio.sleep(GAP_REPAIR_DELAY_SEC)
        if self.seen_ahead:
            self.gap_repairs += 1
            await self.ws.send(
                json.dumps(
                    {"type": "sync", "reason": "gap", "last_seq": self.contiguous, "pending": []}
                )
            )

    async def listen(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            now = time.perf_counter()
            if kind == "ack":
                self.acked += 1
                self.last_ack_at = now
                sent = SEND_TIMES.get(msg["op_id"])
                if sent is not None:
                    self.ack_rtts.append((now - sent) * 1000)
                self.mark_seq(msg.get("seq"))
            elif kind == "ops":
                for item in msg["ops"]:
                    op = Op.from_dict(item["op"])
                    self.rga.apply(op)
                    sent = SEND_TIMES.get(op.op_id)
                    if sent is not None:
                        self.propagation.append((now - sent) * 1000)
                    self.mark_seq(item["seq"])
            elif kind == "sync_ack":
                for missed in msg["missed"]:
                    self.rga.apply(Op.from_dict(missed["op"]))
                    self.mark_seq(missed["seq"])
            elif kind == "error":
                print(f"[{self.client_id}] server error: {msg}", file=sys.stderr)

    async def type_session(self, ops: int, min_delay: float, max_delay: float) -> None:
        for _ in range(ops):
            length = self.rga.visible_len()
            if length > 0 and random.random() < 0.15:
                op = self.rga.local_delete(random.randint(0, length - 1))
            else:
                op = self.rga.local_insert(
                    random.randint(0, length), random.choice("abcdefghijklmnopqrstuvwxyz ")
                )
            SEND_TIMES[op.op_id] = time.perf_counter()
            await self.ws.send(json.dumps({"type": "op", "op": op.to_dict()}))
            self.ops_sent += 1
            await asyncio.sleep(random.uniform(min_delay, max_delay))


async def run_swarm(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.url.rstrip("/")
    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
    password = args.password or secrets.token_urlsafe(16)
    sessionid, doc_id = await login_and_create_doc(base_url, args.username, password)
    headers = {"Cookie": f"sessionid={sessionid}", "Origin": base_url}

    clients = [
        SimulatedClient(f"c{i}-{secrets.token_hex(3)}", f"{ws_base}/ws/docs/{doc_id}/", headers)
        for i in range(args.clients)
    ]
    for first in range(0, len(clients), 50):  # stagger connects to avoid a SYN storm
        await asyncio.gather(*(c.connect() for c in clients[first : first + 50]))
    listeners = [asyncio.create_task(c.listen()) for c in clients]

    start = time.perf_counter()
    await asyncio.gather(
        *(c.type_session(args.ops, args.min_delay, args.max_delay) for c in clients)
    )
    typing_done = time.perf_counter()

    total_sent = sum(c.ops_sent for c in clients)
    expected_broadcasts = total_sent * (len(clients) - 1)
    deadline = time.perf_counter() + args.drain_timeout
    while time.perf_counter() < deadline:
        acked = sum(c.acked for c in clients)
        caught_up = all(c.contiguous >= total_sent and not c.seen_ahead for c in clients)
        if acked >= total_sent and caught_up:
            break
        await asyncio.sleep(0.1)
    all_acked = max(c.last_ack_at for c in clients)

    for c in clients:
        await c.ws.close()
    await asyncio.gather(*listeners, return_exceptions=True)

    head_seq, server_text = await fetch_server_text(base_url, sessionid, doc_id)
    texts = {c.rga.text() for c in clients}
    converged = len(texts) == 1 and server_text in texts

    propagation = [p for c in clients for p in c.propagation]
    rtts = [r for c in clients for r in c.ack_rtts]
    return {
        "doc_id": doc_id,
        "clients": len(clients),
        "ops_sent": total_sent,
        "ops_acked": sum(c.acked for c in clients),
        "server_head_seq": head_seq,
        "broadcasts_received": len(propagation),
        "broadcasts_expected": expected_broadcasts,
        "broadcasts_dropped_by_channel_layer": expected_broadcasts - len(propagation),
        "gap_repairs": sum(c.gap_repairs for c in clients),
        "offered_load_ops_per_sec": round(total_sent / (typing_done - start), 1),
        "committed_ops_per_sec": round(head_seq / (all_acked - start), 1),
        "propagation_ms": {
            "p50": percentile(propagation, 50),
            "p95": percentile(propagation, 95),
            "p99": percentile(propagation, 99),
            "max": round(max(propagation), 2) if propagation else None,
        },
        "ack_rtt_ms": {
            "p50": percentile(rtts, 50),
            "p95": percentile(rtts, 95),
            "p99": percentile(rtts, 99),
        },
        "converged_with_server": converged,
        "final_doc_len": len(server_text),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--url", default="http://localhost:8000", help="Base HTTP URL of the app")
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--ops", type=int, default=40, help="Ops per client")
    parser.add_argument(
        "--min-delay", type=float, default=0.05, help="Min seconds between keystrokes"
    )
    parser.add_argument(
        "--max-delay", type=float, default=0.15, help="Max seconds between keystrokes"
    )
    parser.add_argument("--drain-timeout", type=float, default=60.0)
    parser.add_argument("--username", default=f"loadtest_{secrets.token_hex(3)}")
    parser.add_argument("--password", default=None)
    args = parser.parse_args()

    result = asyncio.run(run_swarm(args))
    print(json.dumps(result, indent=2))
    if not result["converged_with_server"] or result["ops_acked"] < result["ops_sent"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
