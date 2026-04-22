from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path


@dataclass(frozen=True)
class ImportFailure:
    name: str
    error: str


def fast_check_modules() -> list[str]:
    # Curated set that exercises the most common extension modules + core stdlib.
    return [
        "sys",
        "os",
        "re",
        "json",
        "ctypes",
        "ssl",
        "sqlite3",
        "hashlib",
        "socket",
        "select",
        "asyncio",
        "multiprocessing",
        "concurrent.futures",
        "urllib.request",
        "http.client",
        "email",
        "xml.etree.ElementTree",
        "lzma",
        "bz2",
        "zipfile",
        "tarfile",
        "uuid",
        "zoneinfo",
        "ensurepip",
        "venv",
    ]


def fast_check_layout() -> list[str]:
    """
    Return a list of human-readable layout issues (empty means OK).

    These checks are intentionally lightweight and filesystem-based.
    """
    issues: list[str] = []
    if not getattr(sys, "executable", None):
        return issues

    exe = Path(sys.executable).resolve()
    root = exe.parent

    if sys.platform.startswith("win"):
        if not (root / "Lib").is_dir():
            issues.append("missing: Lib/")
        if not (root / "DLLs").is_dir():
            issues.append("missing: DLLs/")
        # Official Windows installs ship Tcl/Tk resources under a single tcl/ tree.
        tcl_root = root / "tcl"
        if not tcl_root.is_dir():
            issues.append("missing: tcl/")
        else:
            has_tcl_dir = any(
                child.is_dir() and child.name.lower().startswith("tcl")
                for child in tcl_root.iterdir()
            )
            has_tk_dir = any(
                child.is_dir() and child.name.lower().startswith("tk")
                for child in tcl_root.iterdir()
            )
            if not has_tcl_dir:
                issues.append("missing: tcl/tcl*/")
            if not has_tk_dir:
                issues.append("missing: tcl/tk*/")

    return issues


def expected_missing_for_platform(name: str, platform_name: str) -> bool:
    """
    Return True for modules that are platform-specific and therefore expected to
    be missing on the current runtime platform.
    """
    windows_only = {
        "_msi",
        "_overlapped",
        "_winapi",
        "_wmi",
        "msilib",
        "msvcrt",
        "nt",
        "winreg",
        "winsound",
    }
    macos_only = {
        "_scproxy",
    }
    non_windows_posix = {
        "posix",
        "pwd",
        "grp",
        "termios",
        "tty",
        "pty",
        "fcntl",
        "resource",
        "syslog",
    }
    linux_like_only = {
        "ossaudiodev",
        "spwd",
    }
    optional_extension_modules = {
        "_gdbm",
    }

    if platform_name.startswith("win"):
        return name in (non_windows_posix | linux_like_only | macos_only)
    if platform_name == "darwin":
        return name in (windows_only | linux_like_only | optional_extension_modules)
    # linux and other posix-like targets
    return name in (windows_only | macos_only | optional_extension_modules)


def import_module_subprocess(python_exe: str, name: str, timeout_s: int) -> str | None:
    code = (
        "import importlib,sys\n"
        "name=sys.argv[1]\n"
        "importlib.import_module(name)\n"
        "print('OK')\n"
    )
    try:
        proc = subprocess.run(
            [python_exe, "-c", code, name],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"TimeoutError('import timed out after {timeout_s}s')"

    if proc.returncode == 0:
        return None
    err = (proc.stderr or "").strip() or (proc.stdout or "").strip() or f"exit={proc.returncode}"
    return err.splitlines()[-1] if err else f"exit={proc.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Import-check standard library modules.")
    parser.add_argument(
        "--mode",
        choices=["fast", "full"],
        default="fast",
        help="fast: curated imports; full: try every sys.stdlib_module_names entry (subprocess + timeout).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=15,
        help="Per-module timeout for --mode full (default: 15).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Do not treat POSIX-only modules as expected failures on Windows.",
    )
    args = parser.parse_args()

    if args.mode == "fast":
        names = fast_check_modules()
        failures: list[ImportFailure] = []
        for name in names:
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001
                failures.append(ImportFailure(name=name, error=repr(exc)))

        layout_issues = fast_check_layout()

        result = {
            "mode": "fast",
            "python_version": sys.version,
            "checked": len(names),
            "failure_count": len(failures),
            "failures": [f.__dict__ for f in failures],
            "layout_issues": layout_issues,
        }
        print(json.dumps(result, indent=2))
        return 1 if (failures or layout_issues) else 0

    names = sorted(getattr(sys, "stdlib_module_names", ()))
    if not names:
        print("sys.stdlib_module_names is unavailable on this Python.")
        return 2

    failures = []
    unexpected = []
    for name in names:
        err = import_module_subprocess(sys.executable, name, args.timeout_seconds)
        if err is None:
            continue
        failures.append(ImportFailure(name=name, error=err))
        if (not args.strict) and expected_missing_for_platform(name, sys.platform):
            continue
        unexpected.append(name)

    result = {
        "mode": "full",
        "python_version": sys.version,
        "stdlib_total": len(names),
        "failure_count": len(failures),
        "unexpected_failure_count": len(unexpected),
        "unexpected_failures": unexpected,
        "failures_preview": [f.__dict__ for f in failures[:50]],
    }
    print(json.dumps(result, indent=2))
    return 1 if unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())

