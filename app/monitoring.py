"""Optional error monitoring hook. No network unless explicitly configured."""
import os


def scrub_event(event, hint):
    # Only SDK exception class/stack frames and bounded operational tags leave
    # the process. Messages, locals, breadcrumbs, requests and user data do not.
    return {key: event[key] for key in ("event_id", "timestamp", "platform", "level", "release", "environment") if key in event} | {
        "message": "application_error",
        "exception": {"values": [{"type": value.get("type", "Error"),
            "value": "redacted", "stacktrace": {"frames": [
                {k: frame[k] for k in ("filename", "function", "lineno", "module") if k in frame}
                for frame in value.get("stacktrace", {}).get("frames", [])]}}
            for value in event.get("exception", {}).get("values", [])]}}


def configure_monitoring():
    if os.getenv("MONITORING_ENABLED", "false").lower() != "true":
        return
    dsn = os.getenv("SENTRY_DSN", "")
    if not dsn:
        raise ValueError("Monitoring enabled but SENTRY_DSN missing")
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration
    sentry_sdk.init(dsn=dsn, environment=os.getenv("APP_ENV", "development"),
        default_integrations=False, integrations=[LoggingIntegration(event_level=40)],
        auto_enabling_integrations=False, auto_session_tracking=False, send_client_reports=False,
        enable_metrics=False,
        send_default_pii=False, include_local_variables=False, traces_sample_rate=0,
        before_send=scrub_event, max_breadcrumbs=0)
