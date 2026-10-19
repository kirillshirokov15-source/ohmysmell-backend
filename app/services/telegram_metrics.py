"""Bounded process-local latency samples; emit quantiles without customer data."""
from collections import defaultdict, deque
import logging
from math import ceil
from time import perf_counter
from app.logging_utils import log_event

samples = defaultdict(lambda: deque(maxlen=1024))


def observe(stage, started):
    duration = (perf_counter() - started) * 1000
    values = samples[stage]
    values.append(duration)
    ordered = sorted(values)
    log_event(logging.getLogger(__name__), "telegram_latency", stage=stage,
        duration_ms=round(duration, 2), count=len(values),
        p50_ms=round(ordered[ceil(len(values) * .5) - 1], 2),
        p95_ms=round(ordered[ceil(len(values) * .95) - 1], 2))
