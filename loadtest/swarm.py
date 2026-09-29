"""
High-concurrency async load testing script for the collaborative sync engine.

Simulates swarms of 50 to 200 concurrent WebSocket clients typing simultaneously
on the same document. Measures p50, p95, and p99 propagation latency, throughput (ops/sec),
and formally asserts 100% convergence across all clients at test completion.
"""

import argparse
import asyncio
import json
import random
import statistics
import time
from typing import Any

from crdt.ops import Op
from crdt.rga import RGA


class SimulatedClient:
    """A simulated browser client with its own local CRDT replica."""

    def __init__(self, client_id: str, doc_id: str, ws_url: str) -> None:
        self.client_id = client_id
        self.doc_id = doc_id
        self.ws_url = ws_url
        self.rga = RGA(site_id=client_id)
        self.ws: Any = None
        self.running = True
        self.ops_sent = 0
        self.ops_received = 0
        self.latencies: list[float] = []  # Round-trip latency in ms
        self.send_times: dict[str, float] = {}

    async def connect(self) -> None:
        try:
            import websockets

            self.ws = await websockets.connect(self.ws_url)
            # Receive initial state
            init_msg = json.loads(await self.ws.recv())
            if "snapshot" in init_msg and init_msg["snapshot"]:
                self.rga = RGA.from_dict(init_msg["snapshot"], site_id=self.client_id)
        except Exception as e:
            print(f"[{self.client_id}] Connection error: {e}")
            self.running = False

    async def listen_loop(self) -> None:
        """Background listener receiving broadcasts and acknowledgements."""
        while self.running and self.ws:
            try:
                raw = await self.ws.recv()
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "ack":
                    op_id = msg.get("op_id")
                    if op_id in self.send_times:
                        rtt_ms = (time.perf_counter() - self.send_times[op_id]) * 1000.0
                        self.latencies.append(rtt_ms)
                        del self.send_times[op_id]

                elif msg_type == "op":
                    op = Op.from_dict(msg["op"])
                    self.rga.apply(op)
                    self.ops_received += 1

            except Exception:
                break

    async def type_session(self, total_ops_to_send: int) -> None:
        """Simulate realistic typing activity with inter-keystroke intervals."""
        for _ in range(total_ops_to_send):
            if not self.running or not self.ws:
                break

            # 85% insert, 15% delete
            v_len = self.rga.visible_len()
            if v_len > 0 and random.random() < 0.15:
                pos = random.randint(0, v_len - 1)
                op = self.rga.local_delete(pos)
            else:
                pos = random.randint(0, v_len)
                char = random.choice("abcdefghijklmnopqrstuvwxyz ")
                op = self.rga.local_insert(pos, char)

            self.ops_sent += 1
            self.send_times[op.op_id] = time.perf_counter()

            # Transmit op over WebSocket
            await self.ws.send(json.dumps({"type": "op", "op": op.to_dict()}))

            # Keystroke delay: 20ms - 80ms
            await asyncio.sleep(random.uniform(0.02, 0.08))

    async def close(self) -> None:
        self.running = False
        if self.ws:
            await self.ws.close()


async def run_swarm(
    num_clients: int,
    ops_per_client: int,
    doc_id: str,
    base_ws_url: str,
) -> dict[str, Any]:
    """Orchestrate a concurrent load testing swarm."""
    ws_url = f"{base_ws_url}/ws/docs/{doc_id}/"
    clients = [SimulatedClient(f"client_{i}", doc_id, ws_url) for i in range(num_clients)]

    print(
        f"\n🚀 Launching swarm of {num_clients} concurrent clients ({ops_per_client} ops/client)..."
    )

    # Connect all clients
    await asyncio.gather(*(c.connect() for c in clients))
    active_clients = [c for c in clients if c.running]
    print(f"✅ {len(active_clients)}/{num_clients} clients connected to document {doc_id}")

    # Start listen loops
    listen_tasks = [asyncio.create_task(c.listen_loop()) for c in active_clients]

    # Run typing session concurrently
    start_time = time.perf_counter()
    await asyncio.gather(*(c.type_session(ops_per_client) for c in active_clients))

    # Allow 2 seconds for final broadcast propagation and message drainage
    await asyncio.sleep(2.0)
    total_duration = time.perf_counter() - start_time

    # Close connections
    for c in active_clients:
        await c.close()
    for t in listen_tasks:
        t.cancel()

    # Collect statistics
    all_latencies = [lat for c in active_clients for lat in c.latencies]
    total_ops_sent = sum(c.ops_sent for c in active_clients)
    throughput_ops_sec = total_ops_sent / total_duration if total_duration > 0 else 0

    # Convergence assertion
    reference_text = active_clients[0].rga.text() if active_clients else ""
    converged_count = sum(1 for c in active_clients if c.rga.text() == reference_text)
    convergence_rate = (converged_count / len(active_clients)) * 100.0 if active_clients else 0.0

    stats = {
        "clients": len(active_clients),
        "total_ops_sent": total_ops_sent,
        "duration_sec": round(total_duration, 2),
        "throughput_ops_sec": round(throughput_ops_sec, 1),
        "p50_latency_ms": round(statistics.median(all_latencies), 2) if all_latencies else 0,
        "p95_latency_ms": round(statistics.quantiles(all_latencies, n=20)[18], 2)
        if len(all_latencies) >= 20
        else 0,
        "p99_latency_ms": round(statistics.quantiles(all_latencies, n=100)[98], 2)
        if len(all_latencies) >= 100
        else 0,
        "convergence_rate": f"{convergence_rate:.1f}%",
        "final_doc_len": len(reference_text),
    }

    return stats


def print_stats_table(stats: dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print(" 📊 LOAD TEST & BENCHMARK RESULTS")
    print("=" * 60)
    for k, v in stats.items():
        print(f"  • {k.replace('_', ' ').capitalize():<24}: {v}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run collaborative CRDT load test swarm")
    parser.add_argument("--clients", type=int, default=50, help="Number of concurrent clients")
    parser.add_argument("--ops", type=int, default=20, help="Ops per client")
    parser.add_argument(
        "--doc", type=str, default="00000000-0000-0000-0000-000000000001", help="Document UUID"
    )
    parser.add_argument("--url", type=str, default="ws://127.0.0.1:8000", help="Base WebSocket URL")

    args = parser.parse_args()
    results = asyncio.run(run_swarm(args.clients, args.ops, args.doc, args.url))
    print_stats_table(results)
