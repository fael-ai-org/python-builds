"""Private ELF dependency names and relocatable, process-local library lookup."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


def is_elf(path: Path) -> bool:
    """A loadable ELF executable/shared object, excluding compiler object files."""
    if not path.is_file() or path.is_symlink():
        return False
    with path.open("rb") as stream:
        header = stream.read(18)
    if len(header) != 18 or header[:4] != b"\x7fELF" or header[5] not in (1, 2):
        return False
    return int.from_bytes(header[16:18], "little" if header[5] == 1 else "big") in (2, 3)


def elf_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if is_elf(path))


def patch_value(option: str, path: Path) -> str:
    return subprocess.check_output(["patchelf", option, str(path)], text=True).strip()


def configure_linux_runtime(root: Path) -> None:
    """Namespace third-party libraries before rewriting every ELF consumer.

    SONAME isolation is necessary even without LD_LIBRARY_PATH: an imported
    extension's dependencies can otherwise satisfy an unrelated host library's
    DT_NEEDED entry. CPython's own library keeps its public embedding identity.
    """
    root = root.resolve()
    lib = root / "lib"
    records = []
    aliases: dict[str, str] = {}
    for path in sorted(lib.iterdir()):
        if ".so" not in path.name or path.name.startswith("libpython") or not is_elf(path):
            continue
        soname = patch_value("--print-soname", path) or path.name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        private = f"libpyportable-{digest}-{soname}"
        for alias in (path.name, soname):
            previous = aliases.setdefault(alias, private)
            if previous != private:
                raise RuntimeError(f"Conflicting bundled library identity: {alias}")
        records.append((path, private))

    # All identities are checked before changing the artifact. Duplicate linker
    # aliases with identical bytes collapse to one private library.
    installed: set[str] = set()
    for path, private in records:
        destination = lib / private
        if private in installed:
            path.unlink()
        else:
            if destination.exists():
                raise RuntimeError(f"Private library destination already exists: {private}")
            path.rename(destination)
            subprocess.run(["patchelf", "--set-soname", private, str(destination)], check=True)
            installed.add(private)
    for path in lib.iterdir():
        if path.is_symlink() and path.name in aliases:
            path.unlink()

    for path in elf_files(root):
        needed = patch_value("--print-needed", path).splitlines()
        for name in needed:
            if name in aliases:
                subprocess.run(["patchelf", "--replace-needed", name, aliases[name], str(path)], check=True)
        relative = os.path.relpath(lib, path.parent).replace(os.sep, "/")
        runpath = "$ORIGIN" if relative == "." else "$ORIGIN/" + relative
        subprocess.run(["patchelf", "--set-rpath", runpath, str(path)], check=True)


def validate_linux_runtime(root: Path) -> dict:
    """Fail closed if generic bundled names or nonrelative lookup paths return."""
    failures = []
    files = elf_files(root)
    if not files:
        return {"passed": False, "failures": ["No packaged ELF files found"]}
    for path in files:
        relative = path.relative_to(root).as_posix()
        output = subprocess.check_output(["readelf", "-d", str(path)], text=True)
        for line in output.splitlines():
            if not any(tag in line for tag in ("(NEEDED)", "(SONAME)")):
                continue
            value = line.split("[", 1)[-1].split("]", 1)[0]
            if "/" in value:
                failures.append(f"{relative}: nonrelative ELF dependency or lookup path")
        lookup = [line.split("[", 1)[-1].split("]", 1)[0] for line in output.splitlines()
                  if "(RUNPATH)" in line or "(RPATH)" in line]
        target = os.path.relpath(root / "lib", path.parent).replace(os.sep, "/")
        expected = "$ORIGIN" if target == "." else "$ORIGIN/" + target
        if lookup != [expected]:
            failures.append(f"{relative}: ELF lookup is not confined to the relative runtime directory")
        if path.parent == root / "lib" and ".so" in path.name and not path.name.startswith(("libpython", "libpyportable-")):
            failures.append(f"{relative}: bundled library lacks a private identity")
        for line in output.splitlines():
            if "(NEEDED)" in line:
                name = line.split("[", 1)[-1].split("]", 1)[0]
                if name.startswith("libpyportable-") and not (root / "lib" / name).is_file():
                    failures.append(f"{relative}: private dependency is missing")
    for path in (root / "bin").glob("python*"):
        if path.is_file() and not path.is_symlink() and not is_elf(path):
            if "LD_LIBRARY_PATH" in path.read_text(errors="replace"):
                failures.append(f"bin/{path.name}: launcher exports global library lookup")
    return {"passed": not failures, "elf_files": len(files), "failures": failures}
