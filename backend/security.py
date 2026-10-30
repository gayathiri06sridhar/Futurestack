import hashlib
import hmac
import secrets
import os
import shutil
import subprocess


PASSWORD_ITERATIONS = 600_000
SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Passwords must contain at least 12 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iteration_text, salt_hex, digest_hex = password_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iteration_text)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_session_tokens() -> tuple[str, str, str, str]:
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    csrf_token = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
    return token, hashlib.sha256(token.encode()).hexdigest(), csrf_token, hashlib.sha256(
        csrf_token.encode()
    ).hexdigest()


def valid_csrf_token(provided: str | None, expected_hash: str) -> bool:
    if not provided:
        return False
    actual_hash = hashlib.sha256(provided.encode()).hexdigest()
    return hmac.compare_digest(actual_hash, expected_hash)


class MalwareScannerError(Exception):
    pass


class MalwareDetectedError(MalwareScannerError):
    pass


def scan_uploaded_bytes(data: bytes) -> dict[str, str]:
    scanner = os.getenv("MALWARE_SCANNER", "disabled").strip().lower()
    if scanner == "disabled":
        return {"status": "not_scanned", "scanner": "disabled"}
    if scanner != "clamav":
        raise MalwareScannerError("Configured malware scanner is not supported.")
    executable = os.getenv("CLAMSCAN_PATH") or shutil.which("clamscan")
    if not executable:
        raise MalwareScannerError("MALWARE_SCANNER=clamav but clamscan is unavailable.")
    try:
        result = subprocess.run(
            [executable, "--no-summary", "-"],
            input=data,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MalwareScannerError("The configured malware scan could not complete.") from exc
    if result.returncode == 0:
        return {"status": "clean", "scanner": "clamav"}
    if result.returncode == 1:
        raise MalwareDetectedError("The upload was rejected by the malware scanner.")
    raise MalwareScannerError("ClamAV returned an error while scanning the upload.")
