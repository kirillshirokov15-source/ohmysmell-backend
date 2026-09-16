"""Repeatable staging matching probe (five synthetic inbound drafts, no exports)."""
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter
from uuid import uuid4
from datetime import datetime, timezone


def main():
    from dotenv import load_dotenv
    load_dotenv()
    from app.bot.staging_runner import validate_staging_config
    validate_staging_config(dict(os.environ))
    from app.integrations.http_tls import verified_session
    token = os.getenv("STAGING_INTERNAL_API_TOKEN") or Path(".staging-artifacts/remote_internal_token").read_text().strip()
    samples, drafts = [], []
    run = "rc-perf-" + uuid4().hex
    with verified_session() as session:
        for index in range(5):
            payload = {"external_message_id": f"{run}-{index}", "sender_email": run+"@example.invalid",
                "sender_name": "SYNTHETIC matching benchmark", "subject": "SYNTHETIC read-only matching",
                "body_text": "MARV007 x1", "received_at": datetime.now(timezone.utc).isoformat()}
            started = perf_counter()
            response = session.post("https://ohmysmell-backend-staging-staging.up.railway.app/internal/email/messages",
                headers={"X-Internal-API-Token": token}, json=payload, timeout=(10,60))
            assert response.status_code == 200
            body = response.json()
            assert body["items"][0]["match_status"] == "matched"
            samples.append(round((perf_counter()-started)*1000,2)); drafts.append(body["id"])
    report = {"passed": True, "samples_ms": samples, "median_ms": median(samples),
        "min_ms": min(samples), "max_ms": max(samples), "draft_ids": drafts,
        "scope": "workstation -> staging API: ingestion + matching + DB, shared synthetic customer; no external writes"}
    Path(".staging-artifacts/matching_benchmark.json").write_text(json.dumps(report,indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    try: main()
    except Exception as error:
        print(json.dumps({"passed":False,"error_type":type(error).__name__})); raise SystemExit(1)
