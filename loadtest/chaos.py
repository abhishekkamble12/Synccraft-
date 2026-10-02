"""
Chaos test: concurrent editing through faults, then a strict convergence check.

Runs against the docker-compose cluster (nginx -> 2 Daphne nodes, Postgres, Redis).
Clients type continuously (with occasional pastes) while the harness injects
faults on a fixed, seeded schedule:

  kill-node        SIGKILL a web node mid-edit, start it again 5 s later
  restart-node     graceful restart of a web node
  restart-redis    restart the channel layer (drops all pub/sub group memberships)
  pause-postgres   freeze the database for a few seconds (writes stall)
  partition        cut ~30% of clients off for 3-12 s; they keep typing offline

With docker-compose.chaos.yml the session TTL is a few seconds, so long partitions
outlive their GC sessions: the server collects tombstones past them and their
offline ops come back `stale`, which exercises the rebase path.

After the fault window everything is healed and drained. The run fails (exit 1)
unless every client has no unacknowledged ops, no sequence gaps, and the same
text as the server.

    docker compose -f docker-compose.yml -f docker-compose.chaos.yml up -d --build
    python -m loadtest.chaos --url http://localhost:8080 --clients 12 --duration 60
"""

import argparse
import asyncio
import json
import random
import secrets
import subprocess
import sys
import time
from typing import Any

from loadtest.client import SimulatedClient
from loadtest.swarm import drain, fetch_server_text, login_and_create_doc, open_clients, summarize

FAULTS = ["kill-node", "partition", "restart-redis", "pause-postgres", "restart-node", "partition"]


class Cluster:
    def __init__(self, compose_files: list[str]) -> None:
        self.base = ["docker", "compose"]
        for path in compose_files:
            self.base += ["-f", path]

    async def run(self, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self.base, *args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(f"docker compose {' '.join(args)} failed: {err.decode()[-500:]}")


async def inject(
    fault: str, cluster: Cluster, clients: list[SimulatedClient], rng: random.Random
) -> dict[str, Any]:
    started = time.perf_counter()
    detail: dict[str, Any] = {"fault": fault}
    if fault == "kill-node":
        node = rng.choice(["web_1", "web_2"])
        await cluster.run("kill", node)
        await asyncio.sleep(5)
        await cluster.run("start", node)
        detail["node"] = node
    elif fault == "restart-node":
        node = rng.choice(["web_1", "web_2"])
        await cluster.run("restart", node)
        detail["node"] = node
    elif fault == "restart-redis":
        await cluster.run("restart", "redis")
    elif fault == "pause-postgres":
        await cluster.run("pause", "postgres")
        await asyncio.sleep(rng.uniform(2, 4))
        await cluster.run("unpause", "postgres")
    elif fault == "partition":
        victims = rng.sample(clients, k=max(1, len(clients) * 3 // 10))
        seconds = [rng.uniform(3, 12) for _ in victims]
        await asyncio.gather(*(c.partition(s) for c, s in zip(victims, seconds, strict=True)))
        detail["clients"] = len(victims)
        detail["max_offline_s"] = round(max(seconds), 1)
    detail["took_s"] = round(time.perf_counter() - started, 1)
    return detail


async def run_chaos(args: argparse.Namespace) -> dict[str, Any]:
    rng = random.Random(args.seed)
    cluster = Cluster(args.compose_file)
    base_url = args.url.rstrip("/")
    sessionid, doc_id = await login_and_create_doc(
        base_url, f"chaos_{secrets.token_hex(3)}", secrets.token_urlsafe(16)
    )
    clients = await open_clients(base_url, args.clients, sessionid, doc_id)

    stop = asyncio.Event()

    async def typist(client: SimulatedClient, seed: int) -> None:
        local = random.Random(seed)
        while not stop.is_set():
            await client.type_one(local, paste_chance=0.03)
            await asyncio.sleep(local.uniform(0.1, 0.3))

    started = time.perf_counter()
    typists = [asyncio.create_task(typist(c, args.seed * 1000 + i)) for i, c in enumerate(clients)]

    faults: list[dict[str, Any]] = []
    deadline = started + args.duration
    index = 0
    await asyncio.sleep(3)
    while time.perf_counter() < deadline - 5:
        fault = FAULTS[index % len(FAULTS)]
        index += 1
        faults.append(await inject(fault, cluster, clients, rng))
        print(f"[chaos] {faults[-1]}", file=sys.stderr, flush=True)
        await asyncio.sleep(rng.uniform(2, 5))

    remaining = deadline - time.perf_counter()
    if remaining > 0:
        await asyncio.sleep(remaining)
    stop.set()
    await asyncio.gather(*typists)
    typing_done = time.perf_counter()

    # Heal: every service up (each fault already undoes itself; this is a safety net),
    # partitions over, then let the system converge.
    await cluster.run("up", "-d", "--no-recreate", "web_1", "web_2", "redis", "nginx")
    for client in clients:
        client.offline_until = 0.0
    await drain(clients, args.drain_timeout)

    head_seq, server_text = await fetch_server_text(base_url, sessionid, doc_id)
    result = {
        "doc_id": doc_id,
        "seed": args.seed,
        "duration_s": args.duration,
        "faults": faults,
        **summarize(clients, started, typing_done, head_seq, server_text),
        "clients_with_gaps": sum(1 for c in clients if c.seen_ahead),
    }
    await asyncio.gather(*(c.close() for c in clients))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--clients", type=int, default=12)
    parser.add_argument("--duration", type=float, default=60.0, help="Seconds of typing + faults")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--drain-timeout", type=float, default=120.0)
    parser.add_argument(
        "--compose-file",
        action="append",
        default=None,
        help="Compose files (default: docker-compose.yml + docker-compose.chaos.yml)",
    )
    args = parser.parse_args()
    if not args.compose_file:
        args.compose_file = ["docker-compose.yml", "docker-compose.chaos.yml"]

    result = asyncio.run(run_chaos(args))
    print(json.dumps(result, indent=2))
    ok = (
        result["converged_with_server"]
        and result["unacked_ops"] == 0
        and result["clients_with_gaps"] == 0
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
