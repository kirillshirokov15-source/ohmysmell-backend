"""One reviewed code-only staging release of both Telegram workers.

Default: read-only preflight. --execute is intentionally required. Railway cannot
atomically deploy two services: a partial release is recorded and stops here.
No variables, database rows, migrations, notification scopes or other services
are changed. Archives contain only the exact supplied commit.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
from datetime import datetime

from scripts.staging_order_desk_audit import cli, PROJECT, ENVIRONMENT, SERVICES
from scripts.staging_order_desk_deploy import prepare_upload


def validate_variables(role, variables):
    if variables.get("APP_ENV") != "staging" or variables.get("EXTERNAL_WRITES_ENABLED", "").lower() != "false":
        raise RuntimeError("staging_external_write_guard_required")
    if variables.get("CLIENT_ORDER_DESK_ENABLED") != "true" and role == "client":
        raise RuntimeError("client_order_desk_required")
    # Preserve existing safety flags; never enable sends or widen recipients.
    if variables.get("ORDER_DESK_SEND_ENABLED", "false").lower() == "true":
        recipient = "ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS" if role == "client" else "ORDER_DESK_STAGING_MANAGER_CHAT_IDS"
        if not variables.get(recipient):
            raise RuntimeError("staging_recipient_allowlist_required")
        try:
            recipients = [int(value.strip()) for value in variables[recipient].split(",")]
            if any(value <= 0 if role == "client" else value >= 0 for value in recipients):
                raise ValueError()
            messages = variables.get("ORDER_DESK_STAGING_MESSAGE_IDS", "")
            drafts = variables.get("ORDER_DESK_STAGING_DRAFT_IDS", "")
            if not messages and not drafts:
                raise ValueError()
            for scope in (messages, drafts):
                if scope and any(int(value.strip()) <= 0 for value in scope.split(",")):
                    raise ValueError()
            if drafts and datetime.fromisoformat(variables.get("ORDER_DESK_STAGING_NOT_BEFORE", "").replace("Z", "+00:00")).tzinfo is None:
                raise ValueError()
        except ValueError:
            raise RuntimeError("invalid_staging_delivery_scope") from None
        # Existing send flags/scopes and delivery receipts are preserved exactly.
        # Never reset pending updates, sent rows, uncertain leases or message IDs.
    if role == "manager" and (not variables.get("MANAGER_TELEGRAM_CHAT_ID") or not variables.get("MANAGER_TELEGRAM_USER_IDS")):
        raise RuntimeError("manager_allowlist_required")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "rev-parse", "--verify", args.commit + "^{commit}"], text=True).strip()
    if args.commit != revision:
        raise RuntimeError("exact_full_commit_required")
    subprocess.run(["git", "merge-base", "--is-ancestor", "db60be7ec330769de5c126914969e50cf7bc5311", revision], check=True)
    state = json.loads(cli("status", "--json"))
    if state["id"] != PROJECT:
        raise RuntimeError("wrong_project")
    env = next(e["node"] for e in state["environments"]["edges"] if e["node"]["id"] == ENVIRONMENT)
    if env["name"] != "staging":
        raise RuntimeError("wrong_environment")
    services, variables = {}, {}
    for role in ("manager", "client"):
        services[role] = next(s["node"] for s in env["serviceInstances"]["edges"] if s["node"]["serviceName"] == SERVICES[role])
        target = ("--project", PROJECT, "--environment", ENVIRONMENT, "--service", services[role]["serviceId"])
        variables[role] = json.loads(cli("variable", "list", *target, "--json"))
        validate_variables(role, variables[role])
    manager_id = variables["manager"].get("TELEGRAM_BOT_TOKEN", "").split(":")[0]
    client_id = variables["client"].get("CLIENT_TELEGRAM_BOT_TOKEN", "").split(":")[0]
    if not manager_id or not client_id or manager_id == client_id:
        raise RuntimeError("separate_bot_identities_required")
    report = {"commit": revision, "environment": "staging", "services": [SERVICES[r] for r in services],
              "migration": False, "variables_changed": [], "deployments": {}}
    print(json.dumps(report), flush=True)
    if not args.execute:
        return
    uploads = {}
    for role in services:
        uploads[role] = prepare_upload(revision, role)
        config = (uploads[role] / "railway.client-bot.staging.toml").read_text(encoding="utf-8")
        (uploads[role] / "railway.toml").write_text(config.replace("app.workers.client_bot", f"app.workers.{role}_bot"), encoding="utf-8")
    report_path = Path(".staging-artifacts") / ("bots-release-" + revision + ".json")
    for role, service in services.items():
        target = ("--project", PROJECT, "--environment", ENVIRONMENT, "--service", service["serviceId"])
        cli("up", str(uploads[role]), "--path-as-root", "--no-gitignore", *target, "--detach", "--message", "telegram-ui " + revision)
        deadline = time.monotonic() + 360
        result = {"status": "UNKNOWN", "previous": service["latestDeployment"]["id"]}
        while time.monotonic() < deadline:
            latest = json.loads(cli("deployment", "list", "--service", service["serviceId"], "--environment", ENVIRONMENT, "--limit", "1", "--json"))[0]
            if latest["id"] != result["previous"]:
                result.update(id=latest["id"], status=latest["status"])
                if latest["status"] in {"SUCCESS", "FAILED", "CRASHED", "REMOVED"}:
                    break
            time.sleep(3)
        report["deployments"][role] = result
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"role": role, **result}), flush=True)
        if result["status"] != "SUCCESS":
            raise RuntimeError("release_incomplete_inspect_report_before_retry")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # CLI errors can contain credentials; expose categories only.
        print(json.dumps({"release_error": type(error).__name__,
            "reason": str(error) if type(error) is RuntimeError else "preflight_or_deploy_failed"}), flush=True)
        raise SystemExit(1)
