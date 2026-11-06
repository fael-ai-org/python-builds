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


def load_segments(path: Path) -> list[tuple[int, int, int]]:
    output = subprocess.check_output(["readelf", "-lW", str(path)], text=True)
    segments = []
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0] == "LOAD":
            segments.append((int(fields[1], 16), int(fields[2], 16), int(fields[-1], 16)))
    if not segments:
        raise RuntimeError(f"ELF runtime has no loadable segments: {path.name}")
    return segments


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
            installed.add(private)
    for path in lib.iterdir():
        if path.is_symlink() and path.name in aliases:
            path.unlink()

    for path in elf_files(root):
        needed = patch_value("--print-needed", path).splitlines()
        alignment = max(4096, *(segment[2] for segment in load_segments(path)))
        if alignment > 2**30 or alignment & (alignment - 1):
            raise RuntimeError(f"Invalid ELF segment alignment: {path.name}")
        # Preserve the input's segment alignment. In particular, manylinux
        # libraries can use 2 MiB alignment even on a 4 KiB-page host.
        command = ["patchelf", "--page-size", str(alignment)]
        if path.parent == lib and path.name in installed:
            command.extend(["--set-soname", path.name])
        for name in needed:
            if name in aliases:
                command.extend(["--replace-needed", name, aliases[name]])
        relative = os.path.relpath(lib, path.parent).replace(os.sep, "/")
        runpath = "$ORIGIN" if relative == "." else "$ORIGIN/" + relative
        subprocess.run([*command, "--set-rpath", runpath, str(path)], check=True)


def validate_linux_runtime(root: Path) -> dict:
    """Fail closed if generic bundled names or nonrelative lookup paths return."""
    failures = []
    files = elf_files(root)
    if not files:
        return {"passed": False, "failures": ["No packaged ELF files found"]}
    for path in files:
        relative = path.relative_to(root).as_posix()
        for offset, address, alignment in load_segments(path):
            if alignment > 1 and offset % alignment != address % alignment:
                failures.append(f"{relative}: ELF load segment alignment is inconsistent")
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
