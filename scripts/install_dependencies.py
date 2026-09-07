"""Install pinned dependencies using the verified Windows + certifi trust bundle."""
import os
import subprocess
import sys
from app.integrations.http_tls import verified_ca_bundle


if __name__ == "__main__":
    environment = dict(os.environ, PIP_CERT=verified_ca_bundle())
    raise SystemExit(subprocess.call([sys.executable, "-m", "pip", "install", "-q",
        "--disable-pip-version-check", "--no-input", "-r", "requirements-dev.txt"], env=environment))
