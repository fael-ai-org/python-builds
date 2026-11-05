from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_portable import copy_windows_development_files


class WindowsDevelopmentLayoutTests(unittest.TestCase):
    def fixture(self, root):
        source, build, output = root / "source", root / "amd64", root / "python"
        (source / "PC").mkdir(parents=True)
        build.mkdir()
        for name in ("venvlauncher.exe", "venvwlauncher.exe", "python313.lib", "python3.lib"):
            (build / name).write_bytes(name.encode())
        (source / "PC" / "pyconfig.h").write_bytes(b"matching build configuration")
        return source, build, output

    def test_retains_venv_launchers_and_native_extension_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, build, output = self.fixture(Path(temporary))
            copy_windows_development_files(source, build, output, version="3.13.18")
            for name in ("venvlauncher.exe", "venvwlauncher.exe"):
                self.assertEqual((output / "Lib/venv/scripts/nt" / name).read_bytes(), (build / name).read_bytes())
            self.assertEqual((output / "Include/pyconfig.h").read_bytes(), (source / "PC/pyconfig.h").read_bytes())
            self.assertEqual((output / "libs/python313.lib").read_bytes(), b"python313.lib")

    def test_missing_build_output_fails_before_partial_copy(self):
        for missing in ("venvlauncher.exe", "venvwlauncher.exe", "python313.lib", "python3.lib", "pyconfig.h"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                source, build, output = self.fixture(Path(temporary))
                if missing.endswith(".lib"):
                    for path in build.glob("*.lib"):
                        path.unlink()
                elif missing == "pyconfig.h":
                    (source / "PC/pyconfig.h").unlink()
                else:
                    (build / missing).unlink()
                with self.assertRaises(RuntimeError):
                    copy_windows_development_files(source, build, output, version="3.13.18")
                self.assertFalse(output.exists())

    def test_generated_build_header_takes_precedence_over_source_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, build, output = self.fixture(Path(temporary))
            (build / "pyconfig.h").write_bytes(b"generated matching configuration")
            copy_windows_development_files(source, build, output, version="3.13.18")
            self.assertEqual((output / "Include/pyconfig.h").read_bytes(), b"generated matching configuration")

    def test_version_specific_launchers_match_installed_venv_contract(self):
        for version in ("3.10.22", "3.11.18", "3.12.16", "3.13.18", "3.14.10"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                source, build, output = self.fixture(Path(temporary))
                copy_windows_development_files(source, build, output, version=version)
                modern = int(version.split(".")[1]) >= 13
                launchers = output / "Lib/venv/scripts/nt"
                expected = ("venvlauncher.exe", "venvwlauncher.exe") if modern else ("python.exe", "pythonw.exe")
                self.assertEqual({p.name for p in launchers.iterdir()}, set(expected))
                for original, installed in zip(("venvlauncher.exe", "venvwlauncher.exe"), expected):
                    self.assertEqual((launchers / installed).read_bytes(), (build / original).read_bytes())


if __name__ == "__main__":
    unittest.main()
