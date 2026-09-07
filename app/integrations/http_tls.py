"""Verified requests sessions with optional Windows trust augmentation."""

import atexit
import os
from pathlib import Path
import shutil
import ssl
import tempfile
import threading

import certifi
import requests


_bundle_lock = threading.Lock()
_windows_bundle: str | None = None


def _remove_bundle() -> None:
    if _windows_bundle:
        Path(_windows_bundle).unlink(missing_ok=True)


def _windows_combined_bundle() -> str:
    global _windows_bundle
    with _bundle_lock:
        if _windows_bundle:
            return _windows_bundle

        handle, bundle_path = tempfile.mkstemp(
            prefix="ohmysmell-ca-",
            suffix=".pem",
        )
        os.close(handle)
        with open(certifi.where(), "rb") as source, open(
            bundle_path, "wb"
        ) as target:
            shutil.copyfileobj(source, target)
            target.write(b"\n")
            for store_name in ("ROOT", "CA"):
                try:
                    certificates = ssl.enum_certificates(store_name)
                except (OSError, PermissionError):
                    continue
                for certificate, encoding, _trust in certificates:
                    if encoding != "x509_asn":
                        continue
                    pem = ssl.DER_cert_to_PEM_cert(certificate)
                    target.write(pem.encode("ascii"))

        _windows_bundle = bundle_path
        atexit.register(_remove_bundle)
        return bundle_path


def verified_ca_bundle(explicit_path: str | None = None) -> str:
    configured = (
        explicit_path
        or os.getenv("MOYSKLAD_CA_BUNDLE")
        or os.getenv("REQUESTS_CA_BUNDLE")
    )
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise RuntimeError("Configured CA bundle does not exist")
        return str(path)
    if os.name == "nt" and hasattr(ssl, "enum_certificates"):
        return _windows_combined_bundle()
    return certifi.where()


def verified_session(ca_bundle: str | None = None) -> requests.Session:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    session = requests.Session()
    session.verify = verified_ca_bundle(ca_bundle)
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=2, backoff_factor=0.3, allowed_methods=frozenset({"GET", "HEAD"}),
        status_forcelist=(429, 502, 503, 504), respect_retry_after_header=False,
    )))
    return session
