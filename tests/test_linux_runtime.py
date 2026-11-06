"""Native ELF regression: bundled and host versions must coexist after relocation."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from linux_runtime import configure_linux_runtime, validate_linux_runtime


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux ELF test")
class LinuxRuntimeTests(unittest.TestCase):
    def setUp(self):
        for tool in ("cc", "patchelf", "readelf"):
            self.assertIsNotNone(shutil.which(tool), f"Linux regression requires {tool}")

    def library(self, source, output, *options):
        subprocess.run(["cc", "-shared", "-fPIC", str(source), "-o", str(output), *options],
                       check=True, capture_output=True, text=True)

    def fixture(self, root):
        package, host = root / "python", root / "host"
        lib = package / "lib"
        extension = lib / "python3.fixture/lib-dynload/consumer.so"
        extension.parent.mkdir(parents=True)
        (package / "bin").mkdir()
        host.mkdir()
        source = root / "dependency.c"
        source.write_text("int dependency_value(void) { return VALUE; }\n")
        for directory, version, value in ((lib, "OLD_1", 1), (host, "NEW_2", 2)):
            versions = directory / "versions.map"
            versions.write_text(version + " { global: dependency_value; local: *; };\n")
            self.library(source, directory / "libfixture.so.1", f"-DVALUE={value}",
                         "-Wl,-soname,libfixture.so.1", f"-Wl,--version-script={versions}")
        consumer = root / "consumer.c"
        consumer.write_text("extern int dependency_value(void); int result(void) { return dependency_value(); }\n")
        self.library(consumer, extension, f"-L{lib}", "-l:libfixture.so.1", "-Wl,-rpath,$ORIGIN/../..")
        self.library(consumer, host / "libhost.so", f"-L{host}", "-l:libfixture.so.1", "-Wl,-rpath,$ORIGIN")
        return package, host, extension.relative_to(package)

    def load(self, extension, host, reverse=False):
        paths = [(str(extension), 1), (str(host / "libhost.so"), 2)]
        if reverse:
            paths.reverse()
        code = "import ctypes\n" + "\n".join(
            f"assert ctypes.CDLL({path!r}).result() == {value}" for path, value in paths)
        return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)

    def test_private_dependencies_coexist_with_host_versions_in_both_orders_after_relocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, host, extension = self.fixture(root)
            broken = self.load(package / extension, host)
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn("NEW_2", broken.stderr)
            configure_linux_runtime(package)
            moved = root / "relocated python"
            package.rename(moved)
            self.assertFalse((moved / "lib/libfixture.so.1").exists())
            self.assertTrue(validate_linux_runtime(moved)["passed"])
            for reverse in (False, True):
                run = self.load(moved / extension, host, reverse)
                self.assertEqual(run.returncode, 0, run.stderr)

    def test_conflicting_sonames_fail_before_artifact_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            package, _, _ = self.fixture(Path(temporary))
            original = package / "lib/libfixture.so.1"
            alias = package / "lib/libfixture.so"
            alias.write_bytes(original.read_bytes() + b"different-build")
            with self.assertRaisesRegex(RuntimeError, "Conflicting bundled library identity"):
                configure_linux_runtime(package)
            self.assertTrue(original.exists())
            self.assertTrue(alias.exists())

    def test_library_without_soname_receives_private_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, _, _ = self.fixture(root)
            source = root / "unnamed.c"
            source.write_text("int result(void) { return 42; }\n")
            self.library(source, package / "lib/libunnamed.so")
            configure_linux_runtime(package)
            libraries = list((package / "lib").glob("libpyportable-*-libunnamed.so"))
            self.assertEqual(len(libraries), 1)
            code = f"import ctypes; assert ctypes.CDLL({str(libraries[0])!r}).result() == 42"
            run = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(validate_linux_runtime(package)["passed"])

    def test_compiler_object_files_are_preserved_without_elf_rewriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, _, _ = self.fixture(root)
            source = root / "object.c"
            source.write_text("int value(void) { return 42; }\n")
            target = package / "lib/python.o"
            subprocess.run(["cc", "-c", str(source), "-o", str(target)], check=True)
            original = target.read_bytes()
            configure_linux_runtime(package)
            self.assertEqual(target.read_bytes(), original)
            self.assertTrue(validate_linux_runtime(package)["passed"])

    def test_validation_rejects_launcher_environment_leak_and_generic_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            package, _, _ = self.fixture(Path(temporary))
            result = validate_linux_runtime(package)
            self.assertFalse(result["passed"])
            configure_linux_runtime(package)
            (package / "bin/python3").write_text("#!/bin/sh\nexport LD_LIBRARY_PATH=bad\n")
            result = validate_linux_runtime(package)
            self.assertFalse(result["passed"])
            self.assertTrue(any("launcher" in failure for failure in result["failures"]))


if __name__ == "__main__":
    unittest.main()
