"""
Summarise load-test JSON results into a Markdown table (medians across runs).

    python -m loadtest.summarize docs/benchmarks/single-node-sqlite_*.json
"""

import glob
import json
import statistics
import sys
from typing import Any


def summarize(paths: list[str]) -> str:
    runs: dict[int, list[dict[str, Any]]] = {}
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            result = json.load(fh)
        runs.setdefault(result["clients"], []).append(result)

    def med(results: list[dict[str, Any]], key: str, sub: str | None = None) -> float:
        return float(statistics.median(r[key][sub] if sub else r[key] for r in results))

    lines = [
        "| Clients | Offered ops/s | Committed ops/s | Propagation p50 / p95 / p99 (ms) "
        "| Ack RTT p50 (ms) | Gap re-syncs | Converged |",
        "|---:|---:|---:|---|---:|---:|:---:|",
    ]
    for clients, results in sorted(runs.items()):
        converged = sum(r["converged_with_server"] for r in results)
        lines.append(
            f"| {clients} "
            f"| {med(results, 'offered_load_ops_per_sec'):.0f} "
            f"| {med(results, 'committed_ops_per_sec'):.0f} "
            f"| {med(results, 'propagation_ms', 'p50'):,.0f} / "
            f"{med(results, 'propagation_ms', 'p95'):,.0f} / "
            f"{med(results, 'propagation_ms', 'p99'):,.0f} "
            f"| {med(results, 'ack_rtt_ms', 'p50'):,.0f} "
            f"| {med(results, 'gap_repairs'):.0f} "
            f"| {converged}/{len(results)} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    patterns = sys.argv[1:] or ["docs/benchmarks/*.json"]
    files = sorted({p for pattern in patterns for p in glob.glob(pattern)})
    if not files:
        sys.exit("no result files matched")
    print(summarize(files))
