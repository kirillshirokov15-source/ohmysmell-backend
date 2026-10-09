"""Read-only Railway/DB audit. Never print variables, contacts or message bodies."""
import json
import base64
import os
from pathlib import Path
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

PROJECT = "142df0b6-cee3-41a8-9353-ea14ec15f0b0"
ENVIRONMENT = "fa04fc09-13d2-4bc4-8f99-486422c7fcb7"
APPROVED_USER = 898019732
SERVICES = {"backend": "ohmysmell-backend-staging", "manager": "ohmysmell-manager-bot-staging",
            "client": "ohmysmell-client-bot-staging"}
FLAGS = ("APP_ENV", "EXTERNAL_WRITES_ENABLED", "TILDA_ENABLED", "ORDER_DESK_SEND_ENABLED",
         "CLIENT_TELEGRAM_ENABLED", "CLIENT_ORDER_DESK_ENABLED")
PRESENCE = ("DATABASE_URL", "TELEGRAM_BOT_TOKEN", "CLIENT_TELEGRAM_BOT_TOKEN", "CLIENT_TELEGRAM_BOT_USERNAME",
            "INTERNAL_API_TOKEN", "TILDA_WEBHOOK_SECRET", "TILDA_FIELD_MAP_JSON", "TILDA_ITEM_FIELD_MAP_JSON",
            "TILDA_PRODUCT_MAP_JSON", "MANAGER_TELEGRAM_CHAT_ID", "MANAGER_TELEGRAM_USER_IDS")


def cli(*args):
    result = subprocess.run([shutil.which("railway.cmd") or "railway", *args], capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode:
        raise RuntimeError("ssh_key_unavailable" if "No SSH keys found" in result.stderr else "railway_read_failed")
    return result.stdout


def read_service(instance):
    target = ("--project", PROJECT, "--environment", ENVIRONMENT, "--service", instance["serviceId"])
    variables = json.loads(cli("variable", "list", *target, "--json"))
    deployment = instance.get("latestDeployment") or {}
    meta = deployment.get("meta") or {}
    result = {"service_id": instance["serviceId"], "status": deployment.get("status"),
        "deployment_id": deployment.get("id"), "commit": meta.get("commitHash"), "branch": meta.get("branch"),
        "flags": {k: variables.get(k) if variables.get(k) in {"true", "false", "staging"} else "unset_or_invalid" for k in FLAGS},
        "configured": {k: bool(variables.get(k)) for k in PRESENCE},
        "domains": [d["domain"] for d in instance.get("domains", {}).get("serviceDomains", [])]}
    deploy = (meta.get("serviceManifest") or {}).get("deploy") or {}
    result["deployment_config"] = {k: deploy.get(k) for k in ("startCommand", "healthcheckPath", "healthcheckTimeout")}
    try:
        rows = cli("logs", *target, "--lines", "150", "--json").splitlines()
        messages = [json.loads(row).get("message", "") for row in rows if row.strip()]
        result["log_counts"] = {event: sum(event in m for m in messages) for event in
            ("worker_ready", "worker_startup_failed", "worker_ownership_lost", "TelegramConflictError", "desk_outbox_failure")}
    except Exception as error:
        result["logs_error"] = type(error).__name__
    if instance["serviceName"] in {SERVICES["manager"], SERVICES["client"]}:
        code = '''import json,os,urllib.request
result={}
for path in ("/health","/ready"):
    try:
        r=urllib.request.urlopen("http://127.0.0.1:"+os.environ.get("PORT","8081")+path,timeout=5)
        d=json.load(r)
        result[path]={"status":r.status,"role":d.get("role"),"polling":d.get("polling"),"external_writes":d.get("external_writes")}
    except Exception as e:
        result[path]={"error_type":type(e).__name__}
print("OMS_AUDIT:"+json.dumps(result))'''
        encoded = base64.b64encode(code.encode()).decode()
        try:
            output = cli("ssh", *target, "--", "python -c 'import base64;exec(base64.b64decode(\"" + encoded + "\"))'")
            result["container_http"] = json.loads(next(line.split("OMS_AUDIT:",1)[1] for line in output.splitlines() if line.startswith("OMS_AUDIT:")))
        except Exception as error:
            result["container_http"] = {"error_type":type(error).__name__,
                "reason":"ssh_key_unavailable" if str(error)=="ssh_key_unavailable" else "probe_unavailable"}
    return result, variables


def queue_audit(connection, manager_chat=None):
    """Only message envelope metadata is selected. Other identities stay undisclosed."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        report = {"alembic": [r[0] for r in cursor.fetchall()]}
        cursor.execute("SELECT direction,status,count(*) FROM desk_messages GROUP BY direction,status ORDER BY direction,status")
        report["queue_counts"] = [{"direction": d, "status": s, "count": n} for d,s,n in cursor.fetchall()]
        cursor.execute("SELECT id,draft_id,direction,destination,status,kind,attempts FROM desk_messages ORDER BY id")
        envelope = []
        for mid,did,direction,destination,status,kind,attempts in cursor.fetchall():
            recipient = "approved_test_user" if destination == APPROVED_USER else (
                "configured_manager_group_NOT_approved" if destination == manager_chat else "other_recipient_NOT_approved")
            envelope.append({"message_id": mid, "draft_id": did, "direction": direction, "recipient": recipient,
                             "status": status, "kind": kind, "attempts": attempts})
        report["messages"] = envelope
        cursor.execute("SELECT count(*) FROM managers WHERE telegram_id=%s AND is_active=true", (APPROVED_USER,))
        report["test_manager_active"] = cursor.fetchone()[0] == 1
        for table in ("order_desks", "draft_notifications", "external_operations"):
            cursor.execute(f"SELECT count(*) FROM {table}")
            report[table + "_count"] = cursor.fetchone()[0]
        cursor.execute("SELECT status,count(*) FROM draft_notifications GROUP BY status")
        report["legacy_notification_counts"] = dict(cursor.fetchall())
        cursor.execute("SELECT count(*) FROM buying_events WHERE notified_at IS NULL")
        report["buying_unnotified_events"] = cursor.fetchone()[0]
        return report


def main():
    from dotenv import dotenv_values
    from sqlalchemy.engine import make_url
    import psycopg2
    from app.integrations.http_tls import verified_session
    status = json.loads(cli("status", "--json"))
    assert status["id"] == PROJECT and status["name"] == "eloquent-wisdom"
    environment = next(e["node"] for e in status["environments"]["edges"] if e["node"]["id"] == ENVIRONMENT)
    assert environment["name"] == "staging"
    instances = {s["node"]["serviceName"]: s["node"] for s in environment["serviceInstances"]["edges"]}
    selected = [instances[name] for name in SERVICES.values()]
    with ThreadPoolExecutor(max_workers=3) as executor:
        data = list(executor.map(read_service, selected))
    variables = {role: entry[1] for role,entry in zip(SERVICES,data)}
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "project": status["name"], "environment": "staging",
        "services": {role: entry[0] for role,entry in zip(SERVICES,data)}}
    local = dotenv_values(".env")
    stage, production = make_url(local["STAGING_DATABASE_URL"]), make_url(local["DATABASE_URL"])
    identity = lambda u: (u.username,u.password,u.database)
    assert identity(stage) != identity(production)
    assert all(identity(make_url(v["DATABASE_URL"])) == identity(stage) for v in variables.values())
    report["all_services_use_staging_database"] = True
    with psycopg2.connect(local["STAGING_DATABASE_URL"], connect_timeout=10, options="-c statement_timeout=10000") as connection:
        connection.set_session(readonly=True)
        report["database"] = queue_audit(connection, int(variables["manager"].get("MANAGER_TELEGRAM_CHAT_ID") or 0))
    manager_token = variables["manager"].get("TELEGRAM_BOT_TOKEN", "")
    client_token = variables["client"].get("CLIENT_TELEGRAM_BOT_TOKEN", "")
    report["distinct_bot_ids"] = bool(manager_token and client_token and manager_token.split(":")[0] != client_token.split(":")[0])
    http = verified_session()
    report["telegram"] = {}
    for role, token, expected in (("manager", manager_token, "OhMySmell_ManagerBot"),("client",client_token,"OhMySmell_OrdersBot")):
        try:
            result = http.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
            webhook = http.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=15).json()
            report["telegram"][role] = {"get_me_ok": result.get("ok") is True,
                "expected_username_matches": result.get("result",{}).get("username", "").lower() == expected.lower(),
                "webhook_configured": bool(webhook.get("result",{}).get("url"))}
        except Exception as error:
            report["telegram"][role] = {"error_type": type(error).__name__}
    report["http"] = {}
    for role, name in SERVICES.items():
        if role == "backend":
            continue
        domains = report["services"][role]["domains"]
        if domains:
            report["services"][role]["public_health"] = {}
            for path in ("/health", "/ready"):
                try:
                    response = http.get("https://" + domains[0] + path, timeout=15)
                    body = response.json() if response.ok else {}
                    report["services"][role]["public_health"][path] = {"status":response.status_code,
                        "role":body.get("role"), "polling":body.get("polling"), "external_writes":body.get("external_writes")}
                except Exception as error:
                    report["services"][role]["public_health"][path] = {"error_type":type(error).__name__}
    for path in ("/", "/health/db", "/openapi.json"):
        response = http.get("https://ohmysmell-backend-staging-staging.up.railway.app" + path, timeout=20)
        report["http"][path] = response.status_code
        if path == "/openapi.json" and response.ok:
            report["tilda_route_present"] = "/integrations/tilda/orders" in response.json().get("paths",{})
    report["backup_exists"] = Path("C:/Users/kater/OhMySmell-Backups/staging-before-tilda.dump").is_file()
    target = Path(".staging-artifacts/order-desk-live-audit.json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"audit_failed": type(error).__name__}))
        raise SystemExit(1)
