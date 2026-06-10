#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import os
import platform
import secrets
import shutil
import subprocess
from pathlib import Path


DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"
SENSITIVE_ENV_KEYS = {
    "BINARY_SIGN_PFX_PASSWORD",
    "BINARY_SIGN_PFX_FILE",
    "MACOS_SIGNING_KEYCHAIN_PASSWORD",
}


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def runner_temp_dir() -> Path:
    raw = os.environ.get("RUNNER_TEMP")
    if raw:
        return Path(raw)
    return Path.cwd() / ".tmp-signing"


def write_env(name: str, value: str, *, sensitive: bool = False) -> None:
    github_env = os.environ.get("GITHUB_ENV")
    line = f"{name}={value}\n"
    if github_env:
        with Path(github_env).open("a", encoding="utf-8") as handle:
            handle.write(line)

    display_value = "<redacted>" if sensitive else value
    print(f"Configured {name}={display_value}")


def export_settings(settings: dict[str, str]) -> None:
    for key, value in settings.items():
        if value == "":
            continue
        write_env(key, value, sensitive=key in SENSITIVE_ENV_KEYS)


def decode_base64_to_file(encoded: str, destination: Path) -> Path:
    try:
        payload = base64.b64decode(encoded)
    except Exception as exc:  # pragma: no cover - defensive input validation
        raise RuntimeError(f"Failed to decode base64 signing payload: {exc}") from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination


def version_key(path: Path) -> tuple[int, ...]:
    raw = path.parent.parent.name
    parts: list[int] = []
    for token in raw.split("."):
        try:
            parts.append(int(token))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def find_signtool() -> str | None:
    existing = os.environ.get("BINARY_SIGNTOOL_PATH")
    if existing and Path(existing).is_file():
        return existing

    direct = shutil.which("signtool")
    if direct:
        return direct

    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    candidates = list(program_files_x86.glob("Windows Kits/*/bin/*/x64/signtool.exe"))
    if not candidates:
        return None

    best = sorted(candidates, key=version_key, reverse=True)[0]
    return str(best)


def configure_windows(required: bool, description: str, timestamp_url: str) -> None:
    pfx_file = (os.environ.get("WINDOWS_SIGNING_PFX_FILE") or "").strip()
    pfx_base64 = (os.environ.get("WINDOWS_SIGNING_PFX_BASE64") or "").strip()
    pfx_password = os.environ.get("WINDOWS_SIGNING_PFX_PASSWORD", "")
    cert_sha1 = (os.environ.get("WINDOWS_SIGNING_CERT_SHA1") or "").strip()
    cert_subject = (os.environ.get("WINDOWS_SIGNING_CERT_SUBJECT") or "").strip()

    effective_pfx_path = ""
    if pfx_base64:
        effective_pfx_path = str(
            decode_base64_to_file(
                pfx_base64,
                runner_temp_dir() / "artifact-signing" / "windows-signing-cert.pfx",
            )
        )
    elif pfx_file:
        candidate = Path(pfx_file).expanduser().resolve()
        if not candidate.is_file():
            raise RuntimeError(f"Configured WINDOWS_SIGNING_PFX_FILE was not found: {candidate}")
        effective_pfx_path = str(candidate)

    signtool_path = find_signtool()
    has_signing_identity = bool(effective_pfx_path or cert_sha1 or cert_subject)
    if not has_signing_identity:
        if required:
            raise RuntimeError(
                "Windows signing is required for this CI run, but no Windows signing "
                "certificate was configured. Set WINDOWS_SIGNING_PFX_BASE64 or a cert selector secret."
            )
        write_env("BINARY_SIGNING_MODE", "off")
        print("Windows signing is not configured for this job.")
        return

    if not signtool_path:
        raise RuntimeError("signtool.exe was not found on this runner.")

    settings = {
        "BINARY_SIGNING_MODE": "required" if required else "auto",
        "BINARY_SIGNTOOL_PATH": signtool_path,
        "BINARY_SIGN_PFX_FILE": effective_pfx_path,
        "BINARY_SIGN_PFX_PASSWORD": pfx_password,
        "BINARY_SIGN_CERT_SHA1": cert_sha1,
        "BINARY_SIGN_CERT_SUBJECT": cert_subject,
        "BINARY_SIGN_DESCRIPTION": description,
        "BINARY_SIGN_TIMESTAMP_URL": timestamp_url,
    }
    export_settings(settings)
    print("Windows artifact signing is configured.")


def run_command(command: list[str]) -> None:
    subprocess.run(command, check=True)


def configure_macos(required: bool, description: str, timestamp_url: str) -> None:
    identity = (os.environ.get("MACOS_SIGNING_IDENTITY") or "").strip()
    p12_file = (os.environ.get("MACOS_SIGNING_P12_FILE") or "").strip()
    p12_base64 = (os.environ.get("MACOS_SIGNING_P12_BASE64") or "").strip()
    p12_password = os.environ.get("MACOS_SIGNING_P12_PASSWORD", "")
    keychain_password = os.environ.get("MACOS_SIGNING_KEYCHAIN_PASSWORD") or secrets.token_urlsafe(24)

    effective_p12_path = ""
    if p12_base64:
        effective_p12_path = str(
            decode_base64_to_file(
                p12_base64,
                runner_temp_dir() / "artifact-signing" / "macos-signing-cert.p12",
            )
        )
    elif p12_file:
        candidate = Path(p12_file).expanduser().resolve()
        if not candidate.is_file():
            raise RuntimeError(f"Configured MACOS_SIGNING_P12_FILE was not found: {candidate}")
        effective_p12_path = str(candidate)

    if not identity and not effective_p12_path:
        if required:
            raise RuntimeError(
                "macOS signing is required for this CI run, but MACOS_SIGNING_IDENTITY and "
                "a signing certificate were not configured."
            )
        write_env("BINARY_SIGNING_MODE", "off")
        print("macOS signing is not configured for this job.")
        return

    if effective_p12_path:
        keychain_path = runner_temp_dir() / "artifact-signing" / "build-signing.keychain-db"
        run_command(["security", "create-keychain", "-p", keychain_password, str(keychain_path)])
        run_command(["security", "set-keychain-settings", "-lut", "21600", str(keychain_path)])
        run_command(["security", "unlock-keychain", "-p", keychain_password, str(keychain_path)])
        run_command(
            [
                "security",
                "import",
                effective_p12_path,
                "-P",
                p12_password,
                "-f",
                "pkcs12",
                "-k",
                str(keychain_path),
                "-T",
                "/usr/bin/codesign",
                "-T",
                "/usr/bin/security",
            ]
        )
        run_command(
            [
                "security",
                "set-key-partition-list",
                "-S",
                "apple-tool:,apple:,codesign:",
                "-s",
                "-k",
                keychain_password,
                str(keychain_path),
            ]
        )
        run_command(["security", "default-keychain", "-s", str(keychain_path)])
        write_env("MACOS_SIGNING_KEYCHAIN_PASSWORD", keychain_password, sensitive=True)

    if not identity:
        raise RuntimeError("MACOS_SIGNING_IDENTITY must be set when macOS signing is configured.")

    settings = {
        "BINARY_SIGNING_MODE": "required" if required else "auto",
        "BINARY_SIGN_MACOS_IDENTITY": identity,
        "BINARY_SIGN_DESCRIPTION": description,
        "BINARY_SIGN_TIMESTAMP_URL": timestamp_url,
    }
    export_settings(settings)
    print("macOS artifact signing is configured.")


def configure_generic(required: bool, description: str, timestamp_url: str) -> None:
    command = (os.environ.get("GENERIC_SIGN_COMMAND") or "").strip()
    if not command:
        if required:
            raise RuntimeError("Generic signing was required for this job, but GENERIC_SIGN_COMMAND was not configured.")
        write_env("BINARY_SIGNING_MODE", "off")
        print("Generic artifact signing is not configured for this job.")
        return

    settings = {
        "BINARY_SIGNING_MODE": "required" if required else "auto",
        "BINARY_SIGN_COMMAND": command,
        "BINARY_SIGN_DESCRIPTION": description,
        "BINARY_SIGN_TIMESTAMP_URL": timestamp_url,
    }
    export_settings(settings)
    print("Generic artifact signing is configured.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare artifact-signing environment variables for CI builds.")
    parser.add_argument("--require-windows-signing", action="store_true")
    parser.add_argument("--require-macos-signing", action="store_true")
    parser.add_argument("--require-generic-signing", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    description = (os.environ.get("SIGNING_DESCRIPTION") or os.environ.get("GITHUB_REPOSITORY") or Path.cwd().name).strip()
    timestamp_url = (os.environ.get("SIGNING_TIMESTAMP_URL") or DEFAULT_TIMESTAMP_URL).strip()

    system = platform.system().lower()
    require_windows = args.require_windows_signing or truthy(os.environ.get("CI_WINDOWS_SIGNING_REQUIRED"))
    require_macos = args.require_macos_signing or truthy(os.environ.get("CI_MACOS_SIGNING_REQUIRED"))
    require_generic = args.require_generic_signing or truthy(os.environ.get("CI_GENERIC_SIGNING_REQUIRED"))

    if system == "windows":
        configure_windows(require_windows, description, timestamp_url)
        return 0
    if system == "darwin":
        configure_macos(require_macos, description, timestamp_url)
        return 0

    configure_generic(require_generic, description, timestamp_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())