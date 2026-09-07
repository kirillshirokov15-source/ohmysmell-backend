"""Query OSV for public pinned PyPI dependencies; no credentials are sent."""
import json
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    packages = []
    for filename in ("requirements.txt", "requirements-dev.txt"):
        for line in Path(filename).read_text(encoding="utf-8").splitlines():
            if "==" in line and not line.startswith("#"):
                name, version = line.split("==", 1)
                packages.append((name.strip(), version.strip()))
    body = json.dumps({"queries": [{"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
                                     for name, version in packages]}).encode()
    try:
        request = Request("https://api.osv.dev/v1/querybatch", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            results = json.load(response)["results"]
        report = {"checked_packages": len(packages), "source": "OSV", "findings": [
            {"package": name, "version": version, "ids": [v["id"] for v in result.get("vulns", [])]}
            for (name, version), result in zip(packages, results) if result.get("vulns")]}
        for finding in report["findings"]:
            fixed = set()
            for identifier in finding["ids"]:
                if not identifier.startswith("GHSA-"):
                    continue
                with urlopen("https://api.osv.dev/v1/vulns/" + identifier, timeout=20) as response:
                    advisory = json.load(response)
                for affected in advisory.get("affected", []):
                    for interval in affected.get("ranges", []):
                        fixed.update(e["fixed"] for e in interval.get("events", []) if "fixed" in e)
            finding["fixed_versions"] = sorted(fixed)
    except Exception as error:
        report = {"error_type": type(error).__name__, "source": "OSV"}
    Path(".staging-artifacts").mkdir(exist_ok=True)
    Path(".staging-artifacts/dependencies.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
