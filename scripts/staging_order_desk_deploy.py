"""Explicit safe code-only deployment. Never changes variables or sends Telegram.

Default is a redacted plan. --execute supports backend/client only, after tests
and with existing send/write flags disabled. Manager redeploy is separately approved.
"""
import argparse
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
from scripts.staging_order_desk_audit import cli, PROJECT, ENVIRONMENT, SERVICES


def prepare_upload(revision, role):
    """Upload only committed files, never the working tree or its credentials."""
    artifact_root = Path(".staging-artifacts").resolve()
    artifact_root.mkdir(exist_ok=True)
    upload = Path(tempfile.mkdtemp(prefix="order-desk-upload-" + role + "-", dir=artifact_root))
    archive = subprocess.check_output(["git", "archive", "--format=tar", revision])
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in source.getmembers():
            target = (upload / member.name).resolve()
            if not target.is_relative_to(upload):
                raise RuntimeError("invalid_archive_path")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.extractfile(member).read())
            else:
                raise RuntimeError("unsupported_archive_entry")
    if role == "client":
        # Existing service has no custom config path. Apply the reviewed worker
        # config to this upload without modifying Railway variables/source settings.
        (upload / "railway.toml").write_bytes((upload / "railway.client-bot.staging.toml").read_bytes())
    return upload


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
        "client_healthcheck":"/health" if args.service=="client" else None,
        "upload_config":"railway.client-bot.staging.toml as railway.toml" if args.service=="client" else "Dockerfile"}
    print(json.dumps({"plan":plan}),flush=True)
    if not args.execute:
        return
    if subprocess.check_output(["git","status","--porcelain"],text=True).strip():
        raise RuntimeError("commit_reviewed_changes_before_deploy")
    before = service["latestDeployment"]["id"]
    upload = prepare_upload(revision, args.service)
    # Archive includes only reviewed commit files. --no-gitignore is required
    # because its safe temporary parent is itself ignored in the original repo.
    cli("up",str(upload),"--path-as-root","--no-gitignore",*target,"--detach","--message","order-desk safe code "+revision)
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
