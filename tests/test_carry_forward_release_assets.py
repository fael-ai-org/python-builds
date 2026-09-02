from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from carry_forward_release_assets import carry_forward_assets, should_keep_previous_asset


class CarryForwardReleaseAssetsTests(unittest.TestCase):
    def test_keeps_only_current_patch_per_major(self) -> None:
        self.assertTrue(should_keep_previous_asset("python-3.12.14-windows-x86_64.zip", {"3.12.14"}))
        self.assertFalse(should_keep_previous_asset("python-3.12.13-windows-x86_64.zip", {"3.12.14"}))
        self.assertTrue(should_keep_previous_asset("python-3.11.16-linux-x86_64.tar.gz.sha256", {"3.11.16"}))

    def test_copies_unchanged_current_assets_and_skips_rebuilt(self) -> None:
        plan = {"current_versions": {"3.11": "3.11.16", "3.12": "3.12.14"}}
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "previous"
            dest = Path(raw) / "release"
            source.mkdir()
            dest.mkdir()
            (source / "python-3.11.16-windows-x86_64.zip").write_text("old-311", encoding="utf-8")
            (source / "python-3.12.13-windows-x86_64.zip").write_text("old-312", encoding="utf-8")
            (source / "python-3.12.14-windows-x86_64.zip").write_text("stale-312", encoding="utf-8")
            (dest / "python-3.12.14-windows-x86_64.zip").write_text("new-312", encoding="utf-8")

            kept = carry_forward_assets(source_dir=source, dest_dir=dest, plan=plan)

            self.assertEqual(kept, ["python-3.11.16-windows-x86_64.zip"])
            self.assertEqual((dest / "python-3.11.16-windows-x86_64.zip").read_text(encoding="utf-8"), "old-311")
            self.assertEqual((dest / "python-3.12.14-windows-x86_64.zip").read_text(encoding="utf-8"), "new-312")
            self.assertFalse((dest / "python-3.12.13-windows-x86_64.zip").exists())


if __name__ == "__main__":
    unittest.main()
