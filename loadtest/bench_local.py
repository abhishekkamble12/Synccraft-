"""
Reproducible single-node benchmark: boots a fresh Daphne process on a throwaway
SQLite database, runs the swarm for each client count N times, and writes one
JSON file per run.

    python -m loadtest.bench_local --label single-node-sqlite --clients 5 10 25 50 --runs 3
    python -m loadtest.summarize "docs/benchmarks/single-node-sqlite_*.json"

`--app-dir` points at another checkout to benchmark it with the same harness
(the swarm in *that* checkout is used, so old protocol versions keep working).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def wait_for(url: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"server at {url} did not come up")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--clients", type=int, nargs="+", default=[5, 10, 25, 50])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--ops", type=int, default=20)
    parser.add_argument("--paste-chance", type=float, default=0.0)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cooldown", type=float, default=20)
    parser.add_argument("--app-dir", type=Path, default=ROOT)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "benchmarks")
    args = parser.parse_args()

    app_dir = args.app_dir.resolve()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            **os.environ,
            "DEBUG": "1",
            "SQLITE_PATH": str(Path(tmp) / "bench.sqlite3"),
            "PYTHONUNBUFFERED": "1",
        }
        env.pop("REDIS_URL", None)
        env.pop("DB_HOST", None)
        subprocess.run(
            [sys.executable, "manage.py", "migrate", "-v0"], cwd=app_dir, env=env, check=True
        )
        log = open(Path(tmp) / "daphne.log", "w")  # noqa: SIM115 - closed below
        server = subprocess.Popen(
            [sys.executable, "-m", "daphne", "-b", "127.0.0.1", "-p", str(args.port),
             "config.asgi:application"],
            cwd=app_dir, env=env, stdout=log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        base = f"http://127.0.0.1:{args.port}"
        try:
            wait_for(f"{base}/accounts/login/")
            for clients in args.clients:
                for run in range(1, args.runs + 1):
                    cmd = [
                        sys.executable, "-m", "loadtest.swarm", "--url", base,
                        "--clients", str(clients), "--ops", str(args.ops),
                    ]  # fmt: skip
                    if args.paste_chance:
                        cmd += ["--paste-chance", str(args.paste_chance)]
                    proc = subprocess.run(cmd, cwd=app_dir, env=env, capture_output=True, text=True)
                    try:
                        result = json.loads(proc.stdout)
                    except json.JSONDecodeError:
                        print(proc.stdout, proc.stderr, file=sys.stderr)
                        raise
                    result["setup"] = {
                        "label": args.label,
                        "ops_per_client": args.ops,
                        "paste_chance": args.paste_chance,
                        "server": "1 Daphne process, SQLite, in-memory channel layer",
                    }
                    out = args.out_dir / f"{args.label}_{clients}c_run{run}.json"
                    out.write_text(json.dumps(result, indent=2))
                    print(
                        f"{args.label} {clients}c run{run}: "
                        f"p50={result['propagation_ms']['p50']} "
                        f"p99={result['propagation_ms']['p99']} "
                        f"commit={result['committed_ops_per_sec']} "
                        f"converged={result['converged_with_server']}",
                        flush=True,
                    )
                    time.sleep(args.cooldown)
        finally:
            server.terminate()
            server.wait(timeout=20)
            log.close()


if __name__ == "__main__":
    main()
