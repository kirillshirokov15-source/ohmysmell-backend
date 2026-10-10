"""Run opt-in tests against the schema recorded by validate_staging."""
import json
import os
from pathlib import Path
import subprocess
import sys
from dotenv import load_dotenv
from app.bot.staging_runner import validate_staging_config


def main():
    load_dotenv()
    validate_staging_config(dict(os.environ))
    report = json.loads(Path(".staging-artifacts/validation.json").read_text(encoding="utf-8"))
    if report.get("alembic_check") != "passed" or report.get('tests_started'):
        raise RuntimeError("Fresh schema validation must pass first")
    # Integration fixtures retain audit history; every suite requires a new schema.
    report['tests_started'] = True
    Path('.staging-artifacts/validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    environment = dict(os.environ, OMS_STAGING_TESTS="1", OMS_STAGING_SCHEMA=report["schema"])
    result = subprocess.run([sys.executable, "-m", "pytest", "tests/test_postgres_staging.py", "tests/test_tilda_postgres.py",
                             "tests/test_order_desk_hardening_postgres.py", "tests/test_supply_acceptance_postgres.py",
                             "tests/test_order_sources_postgres.py", "tests/test_ohmysmell_v1_postgres.py", "-q",
                             "--tb=short", "--junitxml=.staging-artifacts/staging-tests.xml", *sys.argv[1:]], env=environment)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
