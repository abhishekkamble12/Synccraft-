"""
Concurrent WebSocket load test for the collaborative sync engine.

N simulated clients (loadtest/client.py), each with its own local RGA replica,
type into the same document at human-ish speed. Because every client lives in
this one process, send and receive timestamps share a clock, so we measure true
propagation latency (client A sends -> client B receives the broadcast), not
just ack RTT.

At the end every client replica *and* the server's persisted text must be
identical; the script exits non-zero otherwise.

    python -m loadtest.swarm --url http://localhost --clients 50 --ops 40
    python -m loadtest.swarm --paste-chance 0.05   # mix in 50-400 char pastes
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

from loadtest.client import SimulatedClient


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


async def open_clients(
    base_url: str, count: int, sessionid: str, doc_id: str
) -> list[SimulatedClient]:
    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
    headers = {"Cookie": f"sessionid={sessionid}", "Origin": base_url}
    clients = [
        SimulatedClient(f"c{i}", f"{ws_base}/ws/docs/{doc_id}/", headers) for i in range(count)
    ]
    for first in range(0, len(clients), 50):  # stagger connects to avoid a SYN storm
        batch = clients[first : first + 50]
        await asyncio.gather(*(c.start() for c in batch))
        await asyncio.gather(*(asyncio.wait_for(c.ready.wait(), 30) for c in batch))
    return clients


async def drain(clients: list[SimulatedClient], timeout: float) -> None:
    """Wait until every op is acked and every client has a gap-free view of the log."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if all(c.settled for c in clients):
            head = max(c.contiguous for c in clients)
            if all(c.contiguous == head for c in clients):
                return
            for c in clients:
                if c.contiguous < head:
                    await c._send_sync("gap")
        await asyncio.sleep(0.2)


def summarize(
    clients: list[SimulatedClient],
    started: float,
    typing_done: float,
    head_seq: int,
    server_text: str,
) -> dict[str, Any]:
    stats = [c.stats for c in clients]
    total_sent = sum(s.ops_sent for s in stats)
    texts = {c.rga.text() for c in clients}
    propagation = [p for s in stats for p in s.propagation]
    rtts = [r for s in stats for r in s.ack_rtts]
    all_acked = max((s.last_ack_at for s in stats), default=typing_done)
    return {
        "clients": len(clients),
        "ops_sent": total_sent,
        "chars_sent": sum(s.chars_sent for s in stats),
        "ops_acked": sum(s.acked for s in stats),
        "ops_rejected": sum(s.rejected for s in stats),
        "rebases": sum(s.rebases for s in stats),
        "reconnects": sum(s.reconnects for s in stats),
        "server_head_seq": head_seq,
        "broadcasts_received": len(propagation),
        "gap_repairs": sum(s.gap_repairs for s in stats),
        "offered_load_ops_per_sec": round(total_sent / (typing_done - started), 1),
        "committed_ops_per_sec": round(head_seq / max(all_acked - started, 1e-9), 1),
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
        "unacked_ops": sum(len(c.pending) for c in clients),
        "converged_with_server": len(texts) == 1 and server_text in texts,
        "final_doc_len": len(server_text),
    }


async def run_swarm(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.url.rstrip("/")
    password = args.password or secrets.token_urlsafe(16)
    sessionid, doc_id = await login_and_create_doc(base_url, args.username, password)
    clients = await open_clients(base_url, args.clients, sessionid, doc_id)

    async def typist(client: SimulatedClient, seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(args.ops):
            await client.type_one(rng, paste_chance=args.paste_chance)
            await asyncio.sleep(rng.uniform(args.min_delay, args.max_delay))

    started = time.perf_counter()
    await asyncio.gather(*(typist(c, i) for i, c in enumerate(clients)))
    typing_done = time.perf_counter()
    await drain(clients, args.drain_timeout)

    head_seq, server_text = await fetch_server_text(base_url, sessionid, doc_id)
    result = {"doc_id": doc_id, **summarize(clients, started, typing_done, head_seq, server_text)}
    await asyncio.gather(*(c.close() for c in clients))
    return result


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
    parser.add_argument(
        "--paste-chance", type=float, default=0.0, help="Probability an edit is a 50-400 char paste"
    )
    parser.add_argument("--drain-timeout", type=float, default=60.0)
    parser.add_argument("--username", default=f"loadtest_{secrets.token_hex(3)}")
    parser.add_argument("--password", default=None)
    args = parser.parse_args()

    result = asyncio.run(run_swarm(args))
    print(json.dumps(result, indent=2))
    if not result["converged_with_server"] or result["unacked_ops"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
