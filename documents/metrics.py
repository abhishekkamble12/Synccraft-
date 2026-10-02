"""
Prometheus metrics for the sync engine. Each Daphne process exposes its own /metrics.
"""

from django.http import HttpRequest, HttpResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

OPS_COMMITTED = Counter(
    "collab_ops_committed_total", "CRDT operations persisted to the op log.", ["type"]
)
COMMIT_BATCH_SIZE = Histogram(
    "collab_commit_batch_size",
    "Ops per group-commit transaction.",
    buckets=(1, 2, 4, 8, 16, 32, 64, 128, 256),
)
COMMIT_SECONDS = Histogram(
    "collab_commit_seconds",
    "Wall time of one group-commit transaction (DB write + replica apply).",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
ACTIVE_CONNECTIONS = Gauge("collab_active_connections", "Open document WebSockets.")
SYNCS = Counter("collab_syncs_total", "Client sync requests.", ["reason"])
SNAPSHOTS_CREATED = Counter("collab_snapshots_created_total", "Periodic RGA snapshots written.")
AI_JOBS = Counter("collab_ai_jobs_total", "Finished AI jobs.", ["kind", "status"])
AI_JOB_SECONDS = Histogram(
    "collab_ai_job_seconds",
    "End-to-end AI job latency.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)


def metrics_export_view(request: HttpRequest) -> HttpResponse:
    """Expose Prometheus metrics on /metrics."""
    return HttpResponse(generate_latest(), content_type=CONTENT_TYPE_LATEST)
