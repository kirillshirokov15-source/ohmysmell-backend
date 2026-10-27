"""Reversible presentation-only speed optimizations for Manager Bot.

No permissions, idempotency, warehouse write flags or order rules are changed.
"""
import os


def enabled() -> bool:
    return os.getenv("OMS_MANAGER_FAST_CARDS_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
