"""Explicit safe code-only deployment. Never changes variables or sends Telegram.

Default is a redacted plan. --execute supports backend/client only, after tests
and with existing send/write flags disabled. Manager redeploy is separately approved.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
from scripts.staging_order_desk_audit import cli, PROJECT, ENVIRONMENT, SERVICES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--service", choices=("backend","client"), required=True)
    args = parser.parse_args()
    branch = subprocess.check_output(["git","branch","--show-current"],text=True).strip()
    revision = subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
    if branch != "feature/sales-core-v2":
        raise RuntimeError("wrong_branch")
    state = json.loads(cli("status","--json"))
    assert state["id"] == PROJECT and state["name"] == "eloquent-wisdom"
    env = next(e["node"] for e in state["environments"]["edges"] if e["node"]["id"] == ENVIRONMENT)
    assert env["name"] == "staging"
    service = next(s["node"] for s in env["serviceInstances"]["edges"] if s["node"]["serviceName"] == SERVICES[args.service])
    target = ("--project",PROJECT,"--environment",ENVIRONMENT,"--service",service["serviceId"])
    variables = json.loads(cli("variable","list",*target,"--json"))
    assert variables.get("APP_ENV") == "staging"
    assert variables.get("EXTERNAL_WRITES_ENABLED","").lower() == "false"
    assert variables.get("ORDER_DESK_SEND_ENABLED","false").lower() == "false"
    assert variables.get("TILDA_ENABLED","false").lower() == "false"
    if args.service == "client":
        assert variables.get("CLIENT_ORDER_DESK_ENABLED") == "true"
    plan = {"project":"eloquent-wisdom","environment":"staging","service":SERVICES[args.service],
        "service_id":service["serviceId"],"source_commit":revision,"variables_changed":[],
        "sending_enabled":False,"external_writes":False,"tilda_enabled":False,
        "client_healthcheck":"/health" if args.service=="client" else None}
    print(json.dumps({"plan":plan}),flush=True)
    if not args.execute:
        return
    if subprocess.check_output(["git","status","--porcelain"],text=True).strip():
        raise RuntimeError("commit_reviewed_changes_before_deploy")
    before = service["latestDeployment"]["id"]
    # CLI honors .gitignore: .env, local DB, dumps and artifacts are not uploaded.
    cli("up",*target,"--detach","--message","order-desk safe code "+revision)
    deadline = time.monotonic()+360
    result = None
    while time.monotonic()<deadline:
        deployments=json.loads(cli("deployment","list","--service",service["serviceId"],"--environment",ENVIRONMENT,"--limit","3","--json"))
        latest=deployments[0]
        if latest["id"]!=before:
            result={**plan,"deployment_id":latest["id"],"status":latest["status"]}
            if latest["status"] in {"SUCCESS","FAILED","CRASHED","REMOVED"}:
                break
        time.sleep(3)
    if result:
        Path(".staging-artifacts/order-desk-deploy-"+args.service+".json").write_text(json.dumps(result,indent=2),encoding="utf-8")
        print(json.dumps(result),flush=True)
    if not result or result["status"]!="SUCCESS":
        raise RuntimeError("deployment_not_successful")


if __name__=="__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"deploy_error":type(error).__name__}))
        raise SystemExit(1)
