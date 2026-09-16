"""Scan tracked/reviewable source without ever printing credential values."""
import json
import base64
from pathlib import Path
import subprocess
from urllib.parse import urlparse, unquote
from dotenv import dotenv_values


def main():
    files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard"], text=True).splitlines()
    private = []
    environment = dotenv_values(".env")
    for key, value in environment.items():
        if value and any(word in key for word in ("TOKEN", "SECRET", "PASSWORD")) and len(value) >= 12:
            private.append(value)
        if value and "DATABASE_URL" in key:
            password = urlparse(value).password
            if password and len(password) >= 8:
                private.extend([password, unquote(password)])
    # OAuth documents live outside the repository. Scan for their actual secret
    # values too; filenames alone cannot detect an accidentally pasted token.
    for key in ("GMAIL_CREDENTIALS_FILE", "GMAIL_TOKEN_FILE"):
        filename = environment.get(key)
        if filename and Path(filename).is_file():
            raw = Path(filename).read_bytes()
            document = json.loads(raw.decode("utf-8-sig"))
            private.append(base64.b64encode(raw).decode())
            def collect(value):
                if isinstance(value, dict):
                    for field, item in value.items():
                        if field in {"token", "access_token", "refresh_token", "client_secret"} and isinstance(item, str) and len(item) >= 8:
                            private.append(item)
                        else:
                            collect(item)
            collect(document)
    issues = []
    for name in set(files):
        path = Path(name)
        if not path.is_file():
            continue
        if path.name == ".env" or "secrets" in path.parts or path.suffix in {".pem", ".key"}:
            issues.append({"path": name, "issue": "private_file_in_reviewable_tree"})
        content = path.read_text(encoding="utf-8", errors="replace")
        if any(value in content for value in private):
            issues.append({"path": name, "issue": "credential_value_detected"})
    report = {"reviewable_files": len(set(files)), "credential_findings": issues}
    print(json.dumps(report))
    raise SystemExit(bool(issues))


if __name__ == "__main__":
    main()
