"""Reversible manager UX. No changes to authorization, order state or stock writes."""
import os


def enabled() -> bool:
    return os.getenv("OMS_MANAGER_UI_V2_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
