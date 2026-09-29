"""
Prometheus metrics collectors and structured logging utilities for collaborative sync.
"""

from typing import Any
import time

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False


# Prometheus Metrics Definition
if PROMETHEUS_AVAILABLE:
    OPS_TOTAL = Counter(
        "collab_ops_total",
        "Total number of CRDT operations applied.",
        ["type"],
    )

    OP_APPLY_LATENCY = Histogram(
        "collab_op_apply_seconds",
        "Time spent applying a CRDT operation atomically.",
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    )

    ACTIVE_CONNECTIONS = Gauge(
        "collab_active_connections",
        "Current count of active WebSocket client connections.",
    )

    RECONNECTS_TOTAL = Counter(
        "collab_reconnects_total",
        "Total count of client reconnect sync events.",
    )

    SNAPSHOTS_CREATED_TOTAL = Counter(
        "collab_snapshots_created_total",
        "Total count of periodic CRDT document snapshots taken.",
    )

    AI_TOKENS_TOTAL = Counter(
        "ai_tokens_total",
        "Total LLM tokens consumed by AI co-author.",
        ["kind"],
    )

    AI_LATENCY_SECONDS = Histogram(
        "ai_latency_seconds",
        "Latency of AI co-author generation tasks.",
        buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
    )

    AI_REQUESTS_TOTAL = Counter(
        "ai_requests_total",
        "Total AI requests by kind and status.",
        ["kind", "status"],
    )
else:
    # Fallback no-op objects if prometheus_client is not installed
    class NoOpMetric:
        def inc(self, *args: Any, **kwargs: Any) -> None: pass
        def dec(self, *args: Any, **kwargs: Any) -> None: pass
        def set(self, *args: Any, **kwargs: Any) -> None: pass
        def labels(self, *args: Any, **kwargs: Any) -> "NoOpMetric": return self
        def time(self) -> Any:
            class DummyContext:
                def __enter__(self) -> None: pass
                def __exit__(self, *args: Any) -> None: pass
            return DummyContext()

    OPS_TOTAL = NoOpMetric()  # type: ignore[assignment]
    OP_APPLY_LATENCY = NoOpMetric()  # type: ignore[assignment]
    ACTIVE_CONNECTIONS = NoOpMetric()  # type: ignore[assignment]
    RECONNECTS_TOTAL = NoOpMetric()  # type: ignore[assignment]
    SNAPSHOTS_CREATED_TOTAL = NoOpMetric()  # type: ignore[assignment]
    AI_TOKENS_TOTAL = NoOpMetric()  # type: ignore[assignment]
    AI_LATENCY_SECONDS = NoOpMetric()  # type: ignore[assignment]
    AI_REQUESTS_TOTAL = NoOpMetric()  # type: ignore[assignment]


def metrics_export_view(request: Any) -> Any:
    """
    Expose Prometheus metrics on /metrics HTTP endpoint.
    """
    from django.http import HttpResponse

    if not PROMETHEUS_AVAILABLE:
        return HttpResponse("prometheus_client not installed", status=501)

    return HttpResponse(generate_latest(), content_type=CONTENT_TYPE_LATEST)
