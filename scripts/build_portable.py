#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import stat
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from resolve_latest_patch import details_for_version, fetch_tag_refs, latest_detail_for_major


def run(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def remove_tree(path: Path) -> None:
    def onerror(func, target, exc_info):  # type: ignore[no-untyped-def]
        if not os.path.exists(target):
            return
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=onerror)


def allocate_work_dir(root: Path, base_name: str) -> Path:
    preferred = root / "build" / base_name
    if not preferred.exists():
        return preferred

    try:
        remove_tree(preferred)
        return preferred
    except PermissionError:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        fallback = root / "build" / f"{base_name}-{timestamp}"
        print(
            f"Warning: could not remove existing build directory {preferred}; "
            f"using {fallback} instead."
        )
        return fallback


def ensure_windows_admin() -> None:
    """
    Best-effort elevation for Windows local builds.

    Note: this cannot override code integrity / Device Guard policies that block
    specific binaries; it only ensures the process is elevated (UAC) where
    allowed.
    """
    if os.name != "nt":
        return
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Avoid UAC prompts / hangs in CI environments.
        return
    if os.environ.get("PYTHON_BUILDS_ELEVATED") == "1":
        return

    try:
        import ctypes  # local import to keep non-Windows platforms light

        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        # If we can't determine, attempt elevation anyway.
        is_admin = False

    if is_admin:
        return

    # Relaunch this script elevated via UAC prompt.
    os.environ["PYTHON_BUILDS_ELEVATED"] = "1"
    # Ensure the elevated process starts in the same working directory,
    # otherwise local imports (e.g. resolve_latest_patch.py) can fail.
    cwd = os.getcwd()
    full_args = subprocess.list2cmdline(sys.argv[:])
    # If sys.argv[0] is relative, ShellExecuteW uses lpDirectory for resolution.
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, full_args, cwd, 1)
    # ShellExecuteW returns >32 on success; <=32 indicates failure/cancel.
    if int(rc) <= 32:
        print("Elevation request cancelled/failed; continuing without admin.")
        return

    print(f"Elevation requested (ShellExecuteW rc={rc}); please wait for the elevated process.")
    raise SystemExit(0)


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        destination.write_bytes(response.read())


def sha256sum(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_expected_source_sha256(version: str, filename: str) -> str | None:
    checksums_url = f"https://www.python.org/ftp/python/{version}/SHA256SUMS"
    try:
        with urllib.request.urlopen(checksums_url, timeout=120) as response:
            content = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    for line in content.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1] == filename:
            return parts[0].lower()
    return None


def download_verified_source(version: str, stage_dir: Path) -> tuple[Path, str, bool]:
    filename = f"Python-{version}.tgz"
    archive = stage_dir / filename
    url = f"https://www.python.org/ftp/python/{version}/{filename}"
    download(url, archive)

    expected_sha256 = fetch_expected_source_sha256(version, filename)
    actual_sha256 = sha256sum(archive)
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"Checksum mismatch for {filename}: expected {expected_sha256}, got {actual_sha256}"
        )
    return archive, actual_sha256, expected_sha256 is not None


def build_windows(version: str, target_arch: str, stage_dir: Path) -> None:
    if target_arch != "x86_64":
        raise RuntimeError(f"Unsupported Windows architecture: {target_arch}")

    archive, _, verified = download_verified_source(version, stage_dir)
    if not verified:
        print(
            f"Warning: checksum index unavailable for Python {version}; "
            "recording local SHA256 only."
        )

    src_dir = stage_dir / f"Python-{version}"
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(stage_dir)

    run(
        ["cmd", "/c", "PCbuild\\build.bat", "-c", "Release", "-p", "x64"],
        cwd=src_dir,
    )

    build_out_dir = src_dir / "PCbuild" / "amd64"
    if not build_out_dir.exists():
        raise RuntimeError(f"Build output not found: {build_out_dir}")

    python_dir = stage_dir / "python"
    python_dir.mkdir(parents=True, exist_ok=True)
    dlls_dir = python_dir / "DLLs"
    dlls_dir.mkdir(parents=True, exist_ok=True)

    # Core executables and runtime DLLs
    for pattern in [
        "python.exe",
        "pythonw.exe",
        "python*.dll",
        "vcruntime*.dll",
        "ucrtbase.dll",
    ]:
        for match in glob.glob(str(build_out_dir / pattern)):
            src = Path(match)
            dst = python_dir / src.name
            if src.is_file():
                shutil.copy2(src, dst)

    # Native extension modules live in python/DLLs.
    for match in glob.glob(str(build_out_dir / "*.pyd")):
        src = Path(match)
        dst = dlls_dir / src.name
        if src.is_file():
            shutil.copy2(src, dst)

    # Dependent runtime DLLs must be on the loader search path.
    # Putting these next to python.exe matches the standard CPython layout
    # and avoids ImportError: DLL load failed for extension modules.
    for match in glob.glob(str(build_out_dir / "*.dll")):
        src = Path(match)
        dst = python_dir / src.name
        if src.is_file():
            shutil.copy2(src, dst)

    lib_src = src_dir / "Lib"
    if not lib_src.exists():
        raise RuntimeError(f"Stdlib directory not found: {lib_src}")
    shutil.copytree(lib_src, python_dir / "Lib", dirs_exist_ok=True)

    include_src = src_dir / "Include"
    if include_src.exists():
        shutil.copytree(include_src, python_dir / "Include", dirs_exist_ok=True)

    # Match the official Windows layout closely: bundle the Tcl/Tk runtime tree
    # under python/tcl, which is where python.org installs place the resources.
    def _read_env_file(path: Path) -> str | None:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8-sig").strip()
        return text or None

    def _copy_tcltk_from_env(env_name: str) -> None:
        env_path = build_out_dir / env_name
        lib_path_raw = _read_env_file(env_path)
        if not lib_path_raw:
            return
        lib_path = Path(lib_path_raw)
        if not lib_path.exists():
            print(f"Warning: {env_name} points to missing path: {lib_path}")
            return

        # The env file points at .../lib/tcl8.6 (or tk8.6). Copy the whole parent
        # `lib` directory into python/tcl so the packaged layout matches the
        # python.org Windows installer structure.
        lib_parent = lib_path.parent
        if lib_parent.name.lower() != "lib":
            print(f"Warning: unexpected {env_name} layout: {lib_path}")
            return

        dest_root = python_dir / "tcl"
        shutil.copytree(lib_parent, dest_root, dirs_exist_ok=True)

    _copy_tcltk_from_env("TCL_LIBRARY.env")
    _copy_tcltk_from_env("TK_LIBRARY.env")

    license_txt = build_out_dir / "LICENSE.txt"
    if license_txt.is_file():
        shutil.copy2(license_txt, python_dir / "LICENSE.txt")


def prepend_env_paths(env: dict[str, str], key: str, paths: list[Path]) -> None:
    existing = env.get(key, "")
    additions = [str(path) for path in paths if path.exists()]
    if not additions:
        return
    env[key] = os.pathsep.join(additions + ([existing] if existing else []))


def prepend_env_flags(env: dict[str, str], key: str, flags: list[str]) -> None:
    existing = env.get(key, "")
    additions = [flag for flag in flags if flag]
    if not additions:
        return
    env[key] = " ".join(additions + ([existing] if existing else []))


def brew_prefix(formula: str) -> Path | None:
    if shutil.which("brew") is None:
        return None
    try:
        result = subprocess.run(
            ["brew", "--prefix", formula],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None

    prefix = result.stdout.strip()
    return Path(prefix) if prefix else None


def macos_dependency_prefixes() -> tuple[str, ...]:
    return (
        "/usr/local/opt/",
        "/opt/homebrew/opt/",
        "/usr/local/Cellar/",
        "/opt/homebrew/Cellar/",
    )


def preferred_macos_tcl_formula(python_version: str) -> str:
    # CPython 3.11 builds on macOS are more reliable with Tcl/Tk 8.6.
    if python_version.startswith("3.11."):
        return "tcl-tk@8"
    return "tcl-tk"


def manylinux_internal_prefix(name: str) -> Path | None:
    internal_root = Path("/opt/_internal")
    if not internal_root.exists():
        return None

    matches = sorted(
        path for path in internal_root.glob(f"{name}*") if path.is_dir()
    )
    if not matches:
        return None
    return matches[-1]


def unix_build_env(target_os: str, python_version: str, target_arch: str = "x86_64") -> dict[str, str]:
    env = os.environ.copy()
    if target_os == "linux":
        openssl_prefix = manylinux_internal_prefix("openssl")
        if openssl_prefix is not None:
            include_dir = openssl_prefix / "include"
            lib_dirs = [
                path for path in (openssl_prefix / "lib", openssl_prefix / "lib64") if path.exists()
            ]
            pkgconfig_dirs = [
                openssl_prefix / "lib" / "pkgconfig",
                openssl_prefix / "lib64" / "pkgconfig",
                openssl_prefix / "share" / "pkgconfig",
            ]

            print(f"Using manylinux internal OpenSSL from {openssl_prefix}")
            prepend_env_paths(env, "PKG_CONFIG_PATH", pkgconfig_dirs)
            prepend_env_flags(env, "CPPFLAGS", [f"-I{include_dir}"] if include_dir.exists() else [])
            prepend_env_flags(
                env,
                "LDFLAGS",
                [flag for lib_dir in lib_dirs for flag in (f"-L{lib_dir}", f"-Wl,-rpath-link,{lib_dir}")],
            )
        return env

    if target_os != "macos":
        return env

    deployment_target = "11.0" if target_arch == "arm64" else "10.15"
    env["MACOSX_DEPLOYMENT_TARGET"] = deployment_target
    min_flag = f"-mmacosx-version-min={deployment_target}"

    tcl_formula = preferred_macos_tcl_formula(python_version)
    tcl_prefix = brew_prefix(tcl_formula)
    if tcl_prefix is None and tcl_formula != "tcl-tk":
        # Fallback keeps local builds working if only the unversioned formula exists.
        tcl_prefix = brew_prefix("tcl-tk")
        if tcl_prefix is not None:
            print(
                f"Warning: {tcl_formula} not found; falling back to tcl-tk for {python_version}."
            )

    prefixes = [
        brew_prefix(formula)
        for formula in ["openssl@3", "sqlite", "xz", "zlib"]
    ]
    prefixes.append(tcl_prefix)
    prefixes = [prefix for prefix in prefixes if prefix is not None]

    include_dirs = [prefix / "include" for prefix in prefixes]
    lib_dirs = [prefix / "lib" for prefix in prefixes]
    pkgconfig_dirs = []
    for prefix in prefixes:
        pkgconfig_dirs.extend(
            [
                prefix / "lib" / "pkgconfig",
                prefix / "share" / "pkgconfig",
            ]
        )

    prepend_env_paths(env, "PKG_CONFIG_PATH", pkgconfig_dirs)
    prepend_env_flags(env, "CPPFLAGS", [f"-I{path}" for path in include_dirs if path.exists()])
    prepend_env_flags(env, "CFLAGS", [min_flag])
    prepend_env_flags(env, "LDFLAGS", [f"-L{path}" for path in lib_dirs if path.exists()] + [min_flag])
    return env


def otool_dependencies(binary: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["otool", "-L", str(binary)],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    dependencies: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        dependencies.append(stripped.split(" (", 1)[0])
    return dependencies


def is_macos_external_dependency(path: str) -> bool:
    return path.startswith(macos_dependency_prefixes())


def ensure_writable(path: Path) -> None:
    mode = path.stat().st_mode
    os.chmod(path, mode | stat.S_IWRITE)


def relative_loader_reference(consumer: Path, dependency: Path) -> str:
    relative = os.path.relpath(dependency, start=consumer.parent).replace("\\", "/")
    return f"@loader_path/{relative}"


def macos_load_targets(python_dir: Path) -> list[Path]:
    seen: set[Path] = set()
    targets: list[Path] = []

    for file in python_dir.rglob("*"):
        if not file.is_file() or file.is_symlink():
            continue
        if file.suffix in {".so", ".dylib"} or file.parent.name == "bin":
            resolved = file.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            targets.append(file)
    return targets


def bundle_macos_runtime_dependencies(python_dir: Path) -> None:
    bundled_lib_dir = python_dir / "lib" / "bundled-dylibs"
    bundled_lib_dir.mkdir(parents=True, exist_ok=True)

    # Key by *resolved* (canonical) source path so that the opt/ symlink and the
    # Cellar/ real path for the same dylib are treated as identical.
    copied_by_resolved: dict[str, Path] = {}
    pending = macos_load_targets(python_dir)
    processed: set[Path] = set()

    while pending:
        binary = pending.pop()
        resolved_binary = binary.resolve()
        if resolved_binary in processed:
            continue
        processed.add(resolved_binary)

        for dependency in otool_dependencies(binary):
            if not is_macos_external_dependency(dependency):
                continue

            source_path = Path(dependency)
            if not source_path.exists():
                raise RuntimeError(
                    f"Missing macOS runtime dependency {dependency} referenced by {binary}"
                )

            # Resolve symlinks: /opt/homebrew/opt/foo/lib/libfoo.dylib and
            # /opt/homebrew/Cellar/foo/x.y.z/lib/libfoo.dylib both resolve to
            # the same canonical path and must share one bundled copy.
            resolved_source = str(source_path.resolve())
            bundled_path = copied_by_resolved.get(resolved_source)
            if bundled_path is None:
                bundled_path = bundled_lib_dir / source_path.name
                if bundled_path.exists():
                    # Same filename but a different physical file — genuine collision.
                    raise RuntimeError(
                        f"Conflicting bundled library name: {source_path.name} is required by "
                        f"{resolved_source} but a file with that name was already bundled "
                        f"from a different source."
                    )
                shutil.copy2(resolved_source, bundled_path)
                ensure_writable(bundled_path)
                run(
                    [
                        "install_name_tool",
                        "-id",
                        f"@loader_path/{bundled_path.name}",
                        str(bundled_path),
                    ]
                )
                copied_by_resolved[resolved_source] = bundled_path
                pending.append(bundled_path)

            new_reference = relative_loader_reference(binary, bundled_path)
            run(
                [
                    "install_name_tool",
                    "-change",
                    dependency,
                    new_reference,
                    str(binary),
                ]
            )


def rewrite_linux_rpaths(python_dir: Path) -> None:
    python_bin = python_dir / "bin" / "python3"
    if python_bin.exists() and not python_bin.is_symlink():
        run(["patchelf", "--set-rpath", "$ORIGIN/../lib", str(python_bin)])
    for so in sorted(python_dir.rglob("*.so")):
        if so.is_file() and not so.is_symlink():
            run(["patchelf", "--set-rpath", "$ORIGIN/../lib", str(so)])


def strip_binaries(python_dir: Path, target_os: str) -> None:
    python_bin = python_dir / "bin" / "python3"
    if python_bin.exists() and not python_bin.is_symlink():
        run(["strip", str(python_bin)])
    for so in sorted(python_dir.rglob("*.so")):
        if so.is_file() and not so.is_symlink():
            run(["strip", "-S", str(so)])
    if target_os == "macos":
        for dylib in sorted(python_dir.rglob("*.dylib")):
            if dylib.is_file() and not dylib.is_symlink():
                run(["strip", "-S", str(dylib)])


def codesign_macos(python_dir: Path) -> None:
    python_bin = python_dir / "bin" / "python3"
    if python_bin.exists():
        run(["codesign", "--sign", "-", "--force", "--deep", str(python_bin)])


def build_unix(version: str, stage_dir: Path, target_os: str, target_arch: str = "x86_64") -> None:
    archive, _, verified = download_verified_source(version, stage_dir)
    if not verified:
        print(
            f"Warning: checksum index unavailable for Python {version}; "
            "recording local SHA256 only."
        )

    src_dir = stage_dir / f"Python-{version}"
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(stage_dir)

    python_dir = stage_dir / "python"
    env = unix_build_env(target_os, version, target_arch)
    configure_args = [
        "./configure",
        f"--prefix={python_dir}",
        "--with-ensurepip=install",
        "--enable-optimizations",
    ]
    if target_os == "linux":
        openssl_prefix = manylinux_internal_prefix("openssl")
        if openssl_prefix is not None:
            configure_args.append("--with-openssl-rpath=auto")

    run(
        configure_args,
        cwd=src_dir,
        env=env,
    )
    cpu_count = os.cpu_count() or 2
    run(["make", f"-j{cpu_count}"], cwd=src_dir, env=env)
    run(["make", "install"], cwd=src_dir, env=env)
    if target_os == "linux":
        rewrite_linux_rpaths(python_dir)
    if target_os == "macos":
        bundle_macos_runtime_dependencies(python_dir)
    strip_binaries(python_dir, target_os)
    if target_os == "macos":
        codesign_macos(python_dir)


def write_metadata(
    stage_dir: Path,
    version: str,
    target_os: str,
    target_arch: str,
    archive_name: str,
    source_url: str,
    source_sha256: str,
    cpython_tag: str,
    cpython_tag_commit_sha: str,
) -> None:
    metadata = {
        "python_version": version,
        "cpython_tag": cpython_tag,
        "cpython_tag_commit_sha": cpython_tag_commit_sha,
        "target_os": target_os,
        "target_arch": target_arch,
        "archive_name": archive_name,
        "source_url": source_url,
        "source_sha256": source_sha256,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "builder_system": platform.platform(),
    }
    (stage_dir / "METADATA.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )


def package(stage_dir: Path, output_dir: Path, base_name: str, target_os: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if target_os == "windows":
        archive_path = output_dir / f"{base_name}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root in (stage_dir / "python", stage_dir / "METADATA.json"):
                if root.is_file():
                    zf.write(root, arcname=root.relative_to(stage_dir))
                    continue
                if not root.exists():
                    continue
                for file in root.rglob("*"):
                    if file.is_file():
                        zf.write(file, arcname=file.relative_to(stage_dir))
        return archive_path

    archive_path = output_dir / f"{base_name}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        for root in (stage_dir / "python", stage_dir / "METADATA.json"):
            if root.is_file():
                tf.add(root, arcname=root.relative_to(stage_dir))
                continue
            if not root.exists():
                continue
            for file in root.rglob("*"):
                if file.is_file():
                    tf.add(file, arcname=file.relative_to(stage_dir))
    return archive_path


def smoke_test(stage_dir: Path, target_os: str) -> None:
    python_dir = stage_dir / "python"
    if target_os == "windows":
        python_exe = python_dir / "python.exe"
        run(
            [
                str(python_exe),
                "-c",
                "import ctypes,ensurepip,ssl,sqlite3,sys,venv,zoneinfo; print(sys.version)",
            ]
        )
        return

    python_exe = python_dir / "bin" / "python3"
    run(
        [
            str(python_exe),
            "-c",
            "import ctypes,ensurepip,ssl,sqlite3,sys,venv,zoneinfo; print(sys.version)",
        ]
    )


def detect_target_os() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    raise RuntimeError(f"Unsupported host OS for local auto-detection: {platform.system()}")


def detect_target_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    raise RuntimeError(f"Unsupported host architecture for local auto-detection: {platform.machine()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build portable Python archives.")
    parser.add_argument(
        "major",
        help="Python major version line (example: 3.12). Latest patch is resolved automatically.",
    )
    parser.add_argument(
        "--python-version",
        help="Explicit full Python version (example: 3.12.13). Skips API tag resolution.",
    )
    parser.add_argument("--target-os", choices=["windows", "linux", "macos"])
    parser.add_argument("--target-arch", choices=["x86_64", "arm64"])
    parser.add_argument("--output-dir", default="dist")
    parser.add_argument(
        "--cpython-tag",
        help="CPython Git tag name (example: v3.13.13). When provided together with "
             "--cpython-tag-commit-sha, skips the GitHub API call to resolve tag metadata.",
    )
    parser.add_argument(
        "--cpython-tag-commit-sha",
        help="Commit SHA that the CPython Git tag points to. Required when --cpython-tag is used.",
    )
    args = parser.parse_args()

    ensure_windows_admin()

    if args.cpython_tag and args.cpython_tag_commit_sha:
        if not args.python_version:
            raise RuntimeError("--cpython-tag requires --python-version")
        version = args.python_version
        cpython_release: dict[str, str] = {
            "version": version,
            "tag": args.cpython_tag,
            "tag_commit_sha": args.cpython_tag_commit_sha,
        }
    else:
        tag_refs = fetch_tag_refs()
        if args.python_version:
            version = args.python_version
            cpython_release = details_for_version(tag_refs, version)
        else:
            cpython_release = latest_detail_for_major(tag_refs, args.major)
            version = cpython_release["version"]
    target_os = args.target_os or detect_target_os()
    target_arch = args.target_arch or detect_target_arch()
    base_name = f"python-{version}-{target_os}-{target_arch}"

    root = Path.cwd()
    work_dir = allocate_work_dir(root, base_name)
    work_dir.mkdir(parents=True, exist_ok=True)

    stage_dir = work_dir / base_name
    stage_dir.mkdir(parents=True, exist_ok=True)

    source_url = f"https://www.python.org/ftp/python/{version}/Python-{version}.tgz"
    source_sha256 = ""
    if target_os == "windows":
        build_windows(version, target_arch, stage_dir)
        source_archive = stage_dir / f"Python-{version}.tgz"
        if source_archive.exists():
            source_sha256 = sha256sum(source_archive)
    else:
        build_unix(version, stage_dir, target_os, target_arch)
        source_archive = stage_dir / f"Python-{version}.tgz"
        if source_archive.exists():
            source_sha256 = sha256sum(source_archive)

    smoke_test(stage_dir, target_os)
    write_metadata(
        stage_dir,
        version,
        target_os,
        target_arch,
        f"{base_name}",
        source_url,
        source_sha256,
        cpython_release["tag"],
        cpython_release["tag_commit_sha"],
    )
    archive_path = package(stage_dir, root / args.output_dir, base_name, target_os)
    checksum = sha256sum(archive_path)
    sha256_path = Path(str(archive_path) + ".sha256")
    sha256_path.write_text(f"{checksum}  {archive_path.name}\n", encoding="utf-8")
    print(str(archive_path))


if __name__ == "__main__":
    main()
