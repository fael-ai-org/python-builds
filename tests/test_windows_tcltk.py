from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_portable import (  # noqa: E402
    copy_windows_tcltk,
    extract_tcl_zip_overlay,
    map_tcl_zip_member,
    windows_tcl_layout_complete,
)


class WindowsTclTkTests(unittest.TestCase):
    def test_map_embedded_library_prefixes(self) -> None:
        self.assertEqual(map_tcl_zip_member("tcl_library/init.tcl"), "tcl9.0/init.tcl")
        self.assertEqual(map_tcl_zip_member("tk_library/tk.tcl"), "tk9.0/tk.tcl")
        self.assertEqual(map_tcl_zip_member("tcl9.0/init.tcl"), "tcl9.0/init.tcl")
        self.assertIsNone(map_tcl_zip_member("unrelated/file.txt"))

    def test_copy_from_env_without_lib_parent_name(self) -> None:
        root = ROOT / "tests" / "_windows_tcltk_scratch"
        src_dir = root / "src"
        build_out = root / "amd64"
        python_dir = root / "python"
        tcl_scripts = src_dir / "externals" / "tcltk-9.0.4" / "share" / "tcl9.0"
        tk_scripts = src_dir / "externals" / "tcltk-9.0.4" / "share" / "tk9.0"
        tcl_scripts.mkdir(parents=True, exist_ok=True)
        tk_scripts.mkdir(parents=True, exist_ok=True)
        (tcl_scripts / "init.tcl").write_text("# tcl", encoding="utf-8")
        (tk_scripts / "tk.tcl").write_text("# tk", encoding="utf-8")
        build_out.mkdir(parents=True, exist_ok=True)
        python_dir.mkdir(parents=True, exist_ok=True)
        (build_out / "TCL_LIBRARY.env").write_text(str(tcl_scripts), encoding="utf-8")
        (build_out / "TK_LIBRARY.env").write_text(str(tk_scripts), encoding="utf-8")

        try:
            copy_windows_tcltk(src_dir, build_out, python_dir)
            self.assertTrue(windows_tcl_layout_complete(python_dir))
            self.assertTrue((python_dir / "tcl" / "tcl9.0" / "init.tcl").is_file())
            self.assertTrue((python_dir / "tcl" / "tk9.0" / "tk.tcl").is_file())
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_extract_zip_overlay_from_fake_dll(self) -> None:
        root = ROOT / "tests" / "_windows_tcltk_zip"
        root.mkdir(parents=True, exist_ok=True)
        dll = root / "tcl90.dll"
        dest = root / "tcl"
        try:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as zf:
                zf.writestr("tcl_library/init.tcl", "# tcl")
                zf.writestr("tk_library/tk.tcl", "# tk")
            dll.write_bytes(buffer.getvalue())
            self.assertTrue(extract_tcl_zip_overlay(dll, dest))
            self.assertTrue((dest / "tcl9.0" / "init.tcl").is_file())
            self.assertTrue((dest / "tk9.0" / "tk.tcl").is_file())
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
