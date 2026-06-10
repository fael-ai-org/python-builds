#!/usr/bin/env python3
from __future__ import annotations

"""Shared artifact signing helpers for build outputs.

Environment contract used across projects:

- BINARY_SIGNING_MODE=off|auto|required
- BINARY_SIGN_COMMAND=<custom command template>
- BINARY_SIGNTOOL_PATH=<optional signtool.exe path>
- BINARY_SIGN_PFX_FILE=<path to .pfx>
- BINARY_SIGN_PFX_PASSWORD=<pfx password>
- BINARY_SIGN_CERT_SHA1=<thumbprint for cert store lookup>
- BINARY_SIGN_CERT_SUBJECT=<subject name for cert store lookup>
- BINARY_SIGN_TIMESTAMP_URL=<RFC3161 timestamp URL>
- BINARY_SIGN_DESCRIPTION=<optional file description>
- BINARY_SIGN_MACOS_IDENTITY=<codesign identity, '-' for ad hoc>
- BINARY_SIGN_FORCE=1 to re-sign files that already have a valid signature

The custom command template receives these placeholders: {file}, {input},
{output}, {description}, {digest}, {timestamp_url}, {identity}, {pfx_file},
{pfx_password}, {cert_sha1}, and {cert_subject}.
"""

import base64
import csv
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"
WINDOWS_EXTENSIONS = {".dll", ".exe", ".pyd"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_signable_candidate(path: Path) -> bool:
    if not path.exists() or not path.is_file() or path.is_symlink():
        return False

    lower_name = path.name.lower()
    lower_suffix = path.suffix.lower()
    if lower_suffix in WINDOWS_EXTENSIONS:
        return True
    if lower_name.endswith(".dylib"):
        return True
    return re.search(r"\.so(?:\.|$)", lower_name) is not None


def _record_digest(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"sha256={encoded}"


@dataclass(frozen=True)
class SigningConfig:
    mode: str
    command_template: str | None
    signtool_path: str | None
    pfx_file: str | None
    pfx_password: str | None
    cert_sha1: str | None
    cert_subject: str | None
    timestamp_url: str
    description: str | None
    macos_identity: str | None
    force: bool

    @property
    def required(self) -> bool:
        return self.mode == "required"

    @property
    def disabled(self) -> bool:
        return self.mode == "off"

    @property
    def has_windows_signer(self) -> bool:
        if not self.signtool_path:
            return False
        return bool(self.pfx_file or self.cert_sha1 or self.cert_subject)

    @classmethod
    def from_env(cls) -> "SigningConfig":
        mode = os.environ.get("BINARY_SIGNING_MODE", "auto").strip().lower() or "auto"
        if mode not in {"off", "auto", "required"}:
            raise RuntimeError(f"Unsupported BINARY_SIGNING_MODE: {mode}")

        signtool_path = os.environ.get("BINARY_SIGNTOOL_PATH") or shutil.which("signtool")
        return cls(
            mode=mode,
            command_template=os.environ.get("BINARY_SIGN_COMMAND"),
            signtool_path=signtool_path,
            pfx_file=os.environ.get("BINARY_SIGN_PFX_FILE"),
            pfx_password=os.environ.get("BINARY_SIGN_PFX_PASSWORD"),
            cert_sha1=os.environ.get("BINARY_SIGN_CERT_SHA1"),
            cert_subject=os.environ.get("BINARY_SIGN_CERT_SUBJECT"),
            timestamp_url=os.environ.get("BINARY_SIGN_TIMESTAMP_URL", DEFAULT_TIMESTAMP_URL),
            description=os.environ.get("BINARY_SIGN_DESCRIPTION"),
            macos_identity=os.environ.get("BINARY_SIGN_MACOS_IDENTITY"),
            force=_truthy(os.environ.get("BINARY_SIGN_FORCE")),
        )


class ArtifactSigner:
    def __init__(self, config: SigningConfig | None = None) -> None:
        self.config = config or SigningConfig.from_env()

    def sign_directory(self, root: Path, *, label: str | None = None) -> list[Path]:
        candidates = sorted(
            (path for path in root.rglob("*") if _is_signable_candidate(path)),
            key=lambda path: (-len(path.parts), str(path).lower()),
        )
        return self.sign_paths(candidates, label=label)

    def sign_paths(self, paths: Iterable[Path], *, label: str | None = None) -> list[Path]:
        unique_paths: list[Path] = []
        seen: set[str] = set()
        for raw_path in paths:
            path = raw_path.resolve()
            if not _is_signable_candidate(path):
                continue
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            unique_paths.append(path)

        if not unique_paths:
            return []

        if self.config.disabled:
            return []

        if not self._can_sign_any(unique_paths):
            if self.config.required:
                raise RuntimeError(
                    "Signing was required, but no signer is configured for the discovered artifacts."
                )
            if label:
                print(f"Signing not configured for {label}; leaving artifacts unsigned.")
            return []

        signed: list[Path] = []
        for path in unique_paths:
            if self._should_skip_existing_signature(path):
                continue
            if not self._can_sign_path(path):
                if self.config.required:
                    raise RuntimeError(f"Signing required but unsupported for artifact: {path}")
                continue
            self._sign_path(path)
            signed.append(path)

        if label and signed:
            print(f"Signed {len(signed)} artifact(s) for {label}.")
        return signed

    def sign_wheel(self, wheel_path: Path, *, label: str | None = None) -> list[Path]:
        if not wheel_path.exists() or not wheel_path.is_file():
            raise FileNotFoundError(f"Wheel not found: {wheel_path}")

        if self.config.disabled:
            return []

        with tempfile.TemporaryDirectory(prefix="signed-wheel-") as temp_dir:
            work_dir = Path(temp_dir)
            with zipfile.ZipFile(wheel_path, "r") as zf:
                zf.extractall(work_dir)

            signed = self.sign_directory(work_dir, label=label)
            if not signed:
                return []

            self._rewrite_wheel_record(work_dir)

            rebuilt = work_dir / wheel_path.name
            with zipfile.ZipFile(rebuilt, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for file_path in sorted(work_dir.rglob("*")):
                    if not file_path.is_file() or file_path == rebuilt:
                        continue
                    arcname = file_path.relative_to(work_dir).as_posix()
                    zf.write(file_path, arcname=arcname)

            shutil.move(str(rebuilt), str(wheel_path))
            return signed

    def _can_sign_any(self, paths: list[Path]) -> bool:
        if self.config.command_template:
            return True
        if sys.platform == "win32":
            return any(path.suffix.lower() in WINDOWS_EXTENSIONS for path in paths) and self.config.has_windows_signer
        if sys.platform == "darwin":
            return any(self._is_macos_binary(path) for path in paths) and bool(self.config.macos_identity)
        return False

    def _can_sign_path(self, path: Path) -> bool:
        if self.config.command_template:
            return True
        if sys.platform == "win32" and path.suffix.lower() in WINDOWS_EXTENSIONS:
            return self.config.has_windows_signer
        if sys.platform == "darwin" and self._is_macos_binary(path):
            return bool(self.config.macos_identity)
        return False

    def _should_skip_existing_signature(self, path: Path) -> bool:
        if self.config.force:
            return False
        if sys.platform != "win32":
            return False
        if path.suffix.lower() not in WINDOWS_EXTENSIONS:
            return False
        return self._has_valid_windows_signature(path)

    def _sign_path(self, path: Path) -> None:
        if self.config.command_template:
            self._sign_with_custom_command(path)
            return
        if sys.platform == "win32":
            self._sign_windows(path)
            return
        if sys.platform == "darwin":
            self._sign_macos(path)
            return
        raise RuntimeError(f"No built-in signer is available for {path}")

    def _sign_windows(self, path: Path) -> None:
        if not self.config.signtool_path:
            raise RuntimeError("signtool was not found; set BINARY_SIGNTOOL_PATH or install the Windows SDK")

        command = [self.config.signtool_path, "sign", "/fd", "sha256"]
        if self.config.timestamp_url:
            command.extend(["/tr", self.config.timestamp_url, "/td", "sha256"])
        if self.config.description:
            command.extend(["/d", self.config.description])
        if self.config.pfx_file:
            command.extend(["/f", self.config.pfx_file])
            if self.config.pfx_password:
                command.extend(["/p", self.config.pfx_password])
        elif self.config.cert_sha1:
            command.extend(["/sha1", self.config.cert_sha1])
        elif self.config.cert_subject:
            command.extend(["/n", self.config.cert_subject])
        else:
            raise RuntimeError(
                "Configure BINARY_SIGN_PFX_FILE, BINARY_SIGN_CERT_SHA1, or BINARY_SIGN_CERT_SUBJECT to sign Windows binaries."
            )
        command.append(str(path))
        subprocess.run(command, check=True)

        if not self._has_valid_windows_signature(path):
            raise RuntimeError(f"Authenticode verification failed after signing: {path}")

    def _sign_macos(self, path: Path) -> None:
        identity = self.config.macos_identity
        if not identity:
            raise RuntimeError(
                "Set BINARY_SIGN_MACOS_IDENTITY to sign Mach-O binaries on macOS."
            )
        subprocess.run(
            ["codesign", "--force", "--timestamp", "--sign", identity, str(path)],
            check=True,
        )
        subprocess.run(["codesign", "--verify", "--verbose=2", str(path)], check=True)

    def _sign_with_custom_command(self, path: Path) -> None:
        assert self.config.command_template is not None
        command_parts = shlex.split(self.config.command_template, posix=os.name != "nt")
        if not command_parts:
            raise RuntimeError("BINARY_SIGN_COMMAND did not contain a command")

        output_path = path.parent / f"{path.name}.signed"
        uses_output = any("{output}" in part for part in command_parts)
        values = {
            "file": str(path),
            "input": str(path),
            "output": str(output_path),
            "description": self.config.description or "",
            "digest": "sha256",
            "timestamp_url": self.config.timestamp_url,
            "identity": self.config.macos_identity or "",
            "pfx_file": self.config.pfx_file or "",
            "pfx_password": self.config.pfx_password or "",
            "cert_sha1": self.config.cert_sha1 or "",
            "cert_subject": self.config.cert_subject or "",
        }
        rendered = [part.format_map(values) for part in command_parts]
        subprocess.run(rendered, check=True)
        if uses_output:
            if not output_path.exists():
                raise RuntimeError(f"Custom signing command did not produce {output_path}")
            shutil.move(str(output_path), str(path))

    def _has_valid_windows_signature(self, path: Path) -> bool:
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            "$signature = Get-AuthenticodeSignature -LiteralPath $args[0]; if ($signature.Status -eq 'Valid') { exit 0 } exit 1",
            str(path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        return result.returncode == 0

    def _is_macos_binary(self, path: Path) -> bool:
        lower_name = path.name.lower()
        return lower_name.endswith(".dylib") or re.search(r"\.so(?:\.|$)", lower_name) is not None

    def _rewrite_wheel_record(self, wheel_root: Path) -> None:
        dist_info_dirs = sorted(wheel_root.glob("*.dist-info"))
        if len(dist_info_dirs) != 1:
            raise RuntimeError(
                f"Expected one .dist-info directory while rewriting RECORD, found {len(dist_info_dirs)}"
            )

        record_path = dist_info_dirs[0] / "RECORD"
        if not record_path.exists():
            raise RuntimeError(f"Wheel RECORD not found: {record_path}")

        record_rel = record_path.relative_to(wheel_root).as_posix()
        with record_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            for file_path in sorted(path for path in wheel_root.rglob("*") if path.is_file()):
                rel_path = file_path.relative_to(wheel_root).as_posix()
                if rel_path == record_rel:
                    writer.writerow([rel_path, "", ""])
                    continue
                writer.writerow([rel_path, _record_digest(file_path), str(file_path.stat().st_size)])


def sign_directory_binaries(root: Path, *, label: str | None = None) -> list[Path]:
    return ArtifactSigner().sign_directory(root, label=label)


def sign_paths(paths: Iterable[Path], *, label: str | None = None) -> list[Path]:
    return ArtifactSigner().sign_paths(paths, label=label)


def sign_wheel_binaries(wheel_path: Path, *, label: str | None = None) -> list[Path]:
    return ArtifactSigner().sign_wheel(wheel_path, label=label)