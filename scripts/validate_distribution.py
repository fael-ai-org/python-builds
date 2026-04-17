from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


def extract_archive(archive_path: Path, destination: Path) -> Path:
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(destination)
        return destination

    if archive_path.name.endswith(".tar.gz"):
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(destination)
        return destination

    raise RuntimeError(f"Unsupported archive format: {archive_path}")


def distribution_python(dist_root: Path) -> Path:
    windows_python = dist_root / "python" / "python.exe"
    if windows_python.is_file():
        return windows_python

    posix_python = dist_root / "python" / "bin" / "python3"
    if posix_python.is_file():
        return posix_python

    raise RuntimeError(f"Could not locate packaged Python in {dist_root}")


def run_python_json(python_exe: Path | str, code: str, *args: str) -> dict:
    proc = subprocess.run(
        [str(python_exe), "-c", code, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def run_check(checker: Path, python_exe: Path, mode: str, timeout_seconds: int) -> tuple[int, dict]:
    args = [str(python_exe), str(checker), "--mode", mode]
    if mode == "full":
        args.extend(["--timeout-seconds", str(timeout_seconds)])

    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse {mode} validation output: {exc}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        ) from exc
    return proc.returncode, payload


def compare_stdlib_sets(baseline_python: str, packaged_python: Path) -> dict:
    code = (
        "import json, sys\n"
        "print(json.dumps({"
        "'version': list(sys.version_info[:3]), "
        "'stdlib': sorted(sys.stdlib_module_names)"
        "}))\n"
    )
    baseline = run_python_json(baseline_python, code)
    packaged = run_python_json(packaged_python, code)

    baseline_set = set(baseline["stdlib"])
    packaged_set = set(packaged["stdlib"])
    return {
        "baseline_version": baseline["version"],
        "packaged_version": packaged["version"],
        "missing_from_packaged": sorted(baseline_set - packaged_set),
        "extra_in_packaged": sorted(packaged_set - baseline_set),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a packaged portable Python archive.")
    parser.add_argument("archive", help="Path to a built archive (.zip or .tar.gz).")
    parser.add_argument(
        "--baseline-python",
        help="Optional Python interpreter to compare sys.stdlib_module_names against.",
    )
    parser.add_argument(
        "--full-timeout-seconds",
        type=int,
        default=5,
        help="Per-module timeout for the full stdlib import sweep.",
    )
    args = parser.parse_args()

    archive_path = Path(args.archive).resolve()
    checker = Path(__file__).with_name("check_stdlib.py")

    with tempfile.TemporaryDirectory(prefix="python-builds-validate-") as tmpdir:
        dist_root = extract_archive(archive_path, Path(tmpdir))
        packaged_python = distribution_python(dist_root)

        fast_exit, fast_payload = run_check(checker, packaged_python, "fast", args.full_timeout_seconds)
        full_exit, full_payload = run_check(checker, packaged_python, "full", args.full_timeout_seconds)

        stdlib_compare = None
        compare_failed = False
        if args.baseline_python:
            stdlib_compare = compare_stdlib_sets(args.baseline_python, packaged_python)
            compare_failed = bool(
                stdlib_compare["missing_from_packaged"] or stdlib_compare["extra_in_packaged"]
            )

        summary = {
            "archive": str(archive_path),
            "fast_exit": fast_exit,
            "fast": fast_payload,
            "full_exit": full_exit,
            "full": full_payload,
            "stdlib_compare": stdlib_compare,
        }
        print(json.dumps(summary, indent=2))

        # Fast checks are the release gate. Full sweep remains diagnostic because
        # portable builds can legitimately omit some platform-specific/optional modules.
        if fast_exit != 0 or compare_failed:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())