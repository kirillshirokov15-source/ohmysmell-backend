"""Scan tracked/reviewable source without ever printing credential values."""
import json
from pathlib import Path
import subprocess
from urllib.parse import urlparse, unquote
from dotenv import dotenv_values


def main():
    files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard"], text=True).splitlines()
    private = []
    for key, value in dotenv_values(".env").items():
        if value and any(word in key for word in ("TOKEN", "SECRET", "PASSWORD")) and len(value) >= 12:
            private.append(value)
        if value and "DATABASE_URL" in key:
            password = urlparse(value).password
            if password and len(password) >= 8:
                private.extend([password, unquote(password)])
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
