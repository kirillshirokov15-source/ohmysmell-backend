"""Read-only staging inventory for q60 release; never emits credentials or PII."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from dotenv import dotenv_values
from sqlalchemy.engine import make_url
from scripts.staging_order_desk_audit import cli, PROJECT, ENVIRONMENT


def main():
    state = json.loads(cli("status", "--json"))
    assert state["id"] == PROJECT
    env = next(e["node"] for e in state["environments"]["edges"] if e["node"]["id"] == ENVIRONMENT)
    assert env["name"] == "staging"
    instances = [s["node"] for s in env["serviceInstances"]["edges"]]
    def inspect(instance):
        variables = json.loads(cli("variable", "list", "--project", PROJECT,
            "--environment", ENVIRONMENT, "--service", instance["serviceId"], "--json"))
        deployment = instance.get("latestDeployment") or {}
        meta = deployment.get("meta") or {}
        deploy = (meta.get("serviceManifest") or {}).get("deploy") or {}
        flags = {k: variables.get(k, "unset") for k in (
            "APP_ENV", "EXTERNAL_WRITES_ENABLED", "TILDA_TEST_MODE", "ORDER_DESK_SEND_ENABLED",
            "EMAIL_PROVIDER", "EMAIL_SEND_ENABLED", "BUYING_EMAIL_SEND_ENABLED")}
        # Only known booleans/environment names; no arbitrary variable values.
        flags = {k: v if v in {"true", "false", "staging", "fake", "gmail", "unset"} else "other" for k,v in flags.items()}
        scope = {k: variables.get(k, "") for k in (
            "ORDER_DESK_STAGING_MESSAGE_IDS", "ORDER_DESK_STAGING_DRAFT_IDS", "ORDER_DESK_STAGING_NOT_BEFORE")}
        return ({"name": instance["serviceName"], "id": instance["serviceId"],
            "deployment_id": deployment.get("id"), "status": deployment.get("status"),
            "commit": meta.get("commitHash"), "start": deploy.get("startCommand"),
            "healthcheck": deploy.get("healthcheckPath"), "flags": flags, "scope": scope,
            "domains": [d["domain"] for d in instance.get("domains", {}).get("serviceDomains", [])]}, variables)
    with ThreadPoolExecutor(max_workers=3) as pool:
        entries = list(pool.map(inspect, instances))
    local = dotenv_values(".env")
    stage, prod = make_url(local["STAGING_DATABASE_URL"]), make_url(local["DATABASE_URL"])
    identity = lambda u: (u.username, u.password, u.database)
    assert identity(stage) != identity(prod)
    backend = next(v for r,v in entries if r["name"] == "ohmysmell-backend-staging")
    assert identity(make_url(backend["DATABASE_URL"])) == identity(stage)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "project": PROJECT,
        "environment": ENVIRONMENT, "services": [r for r,v in entries]}
    with psycopg2.connect(local["STAGING_DATABASE_URL"], connect_timeout=10,
            options="-c default_transaction_read_only=on -c statement_timeout=10000") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            report["revision"] = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid IN ('warehouse_allocations'::regclass,'x_settlements'::regclass) ORDER BY 1,2")
            report["constraints"] = cur.fetchall()
            cur.execute("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename IN ('warehouse_allocations','x_settlements') ORDER BY 1,2")
            report["indexes"] = cur.fetchall()
            cur.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name IN ('warehouse_allocations','x_settlements') ORDER BY 1,ordinal_position")
            report["columns"] = cur.fetchall()
            for table in ("warehouse_allocations", "x_settlements"):
                cur.execute(f"SELECT count(*),pg_total_relation_size('{table}') FROM {table}")
                report[table] = cur.fetchone()
            cur.execute("SELECT count(*) FROM draft_notifications WHERE status <> 'sent'")
            report["legacy_unsent"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM buying_events WHERE notified_at IS NULL")
            report["buying_unnotified"] = cur.fetchone()[0]
            cur.execute("SELECT direction,status,count(*) FROM desk_messages GROUP BY direction,status ORDER BY 1,2")
            report["desk_queue"] = cur.fetchall()
            report["protected_hashes"] = {}
            for table, where in (("draft_orders", "id=38"), ("draft_order_items", "draft_order_id=38"),
                                 ("orders", "id=16"), ("order_items", "order_id=16")):
                cur.execute(f"SELECT row_to_json(t)::text FROM {table} t WHERE {where} ORDER BY id")
                report["protected_hashes"][table] = hashlib.sha256(json.dumps(cur.fetchall()).encode()).hexdigest()
    target = Path(".staging-artifacts/warehouse-release-preflight.json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"preflight_failed": type(error).__name__}))
        raise SystemExit(1)
