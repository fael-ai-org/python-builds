from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from release_inventory import TARGETS, archive_name, expected_archives, fetch_inventory, validate_directory
from carry_forward_release_assets import carry_forward_assets, download_carried_assets


class ReleaseInventoryTests(unittest.TestCase):
    versions = {"3.12": "3.12.16", "3.13": "3.13.18"}

    def inventory(self):
        return {"tag_name": "old", "draft": True, "assets": [
            {"name": name + suffix, "id": index + 1, "state": "uploaded", "size": 100,
             "digest": "sha256:" + "a" * 64}
            for index, name in enumerate(expected_archives(self.versions)) for suffix in ("", ".sha256")]
                + [{"name": "SHA256SUMS.txt", "state": "uploaded", "size": 100, "digest": "sha256:" + "a" * 64}]}

    def plan(self, inventory, *, force=False, versions=None, public=False):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            details = {major: {"version": version, "tag_commit_sha": "a" * 40}
                       for major, version in self.versions.items()}
            (root / "state.json").write_text(json.dumps({"release_tag": "old", "details": details}))
            (root / "resolved.json").write_text(json.dumps({
                major: {"version": version, "tag_commit_sha": "b" * 40}
                for major, version in (versions or self.versions).items()}))
            (root / "inventory.json").write_text(json.dumps(inventory))
            command = [sys.executable, str(ROOT / "scripts/plan_release.py"),
                       "--resolved-file", str(root / "resolved.json"),
                       "--state-file", str(root / "state.json"),
                       "--inventory-file", str(root / "inventory.json")]
            if force:
                command.append("--force")
            if public:
                command += ["--release-tag", "old", "--candidate-suffix", "12345"]
            return json.loads(subprocess.check_output(command, text=True))

    def test_complete_unchanged_release_does_not_build(self):
        self.assertFalse(self.plan(self.inventory())["should_build"])

    def test_missing_archive_or_checksum_rebuilds_only_affected_target(self):
        name = archive_name("3.12.16", TARGETS[0])
        for suffix in ("", ".sha256"):
            with self.subTest(suffix=suffix):
                inventory = self.inventory()
                inventory["assets"] = [a for a in inventory["assets"] if a["name"] != name + suffix]
                plan = self.plan(inventory)
                self.assertEqual([t["archive_name"] for t in plan["build_targets"]], [name])
                self.assertEqual(plan["release_reason"], "missing-assets")

    def test_missing_release_rebuilds_all_targets(self):
        self.assertEqual(len(self.plan({"assets": []})["build_targets"]), 8)

    def test_missing_global_checksum_index_is_repaired(self):
        inventory = self.inventory()
        inventory["assets"] = [a for a in inventory["assets"] if a["name"] != "SHA256SUMS.txt"]
        self.assertEqual(len(self.plan(inventory)["build_targets"]), 1)

    def test_version_bump_and_missing_unchanged_target_are_combined(self):
        inventory = self.inventory()
        missing = archive_name("3.12.16", TARGETS[1])
        inventory["assets"] = [a for a in inventory["assets"] if a["name"] != missing]
        plan = self.plan(inventory, versions={"3.12": "3.12.16", "3.13": "3.13.19"})
        self.assertEqual(len(plan["build_targets"]), 5)
        self.assertIn(missing, [t["archive_name"] for t in plan["build_targets"]])

    def test_force_builds_all_and_public_release_gets_new_candidate(self):
        inventory = self.inventory()
        inventory["draft"] = False
        plan = self.plan(inventory, force=True, public=True)
        self.assertEqual(len(plan["build_targets"]), 8)
        self.assertEqual(plan["release_tag"], "old-build-12345")

    def populate(self, root):
        for name in expected_archives(self.versions):
            data = name.encode()
            (root / name).write_bytes(data)
            (root / (name + ".sha256")).write_text(hashlib.sha256(data).hexdigest() + "  " + name + "\n")

    def test_final_gate_rejects_missing_corrupt_wrong_named_and_extra_files(self):
        for mode in ("missing-archive", "missing-checksum", "corrupt", "wrong-name", "extra", "empty"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                self.populate(root)
                self.assertEqual(len(validate_directory(root, self.versions)), 8)
                name = sorted(expected_archives(self.versions))[0]
                if mode == "missing-archive":
                    (root / name).unlink()
                elif mode == "missing-checksum":
                    (root / (name + ".sha256")).unlink()
                elif mode in ("corrupt", "empty"):
                    (root / name).write_bytes(b"changed" if mode == "corrupt" else b"")
                elif mode == "wrong-name":
                    p = root / (name + ".sha256")
                    p.write_text(p.read_text().replace(name, "wrong.zip"))
                else:
                    (root / "unexpected.zip").write_bytes(b"extra")
                with self.assertRaises(ValueError):
                    validate_directory(root, self.versions)

    def test_carry_forward_cannot_relabel_modified_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            source, dest = Path(raw) / "old", Path(raw) / "new"
            source.mkdir()
            name = archive_name("3.12.16", TARGETS[0])
            (source / name).write_bytes(b"modified")
            with self.assertRaises(ValueError):
                carry_forward_assets(source_dir=source, dest_dir=dest,
                                     plan={"current_versions": self.versions, "inventory_checked": True,
                                           "carry_forward_digests": {name: "a" * 64}})

    def test_incremental_assembly_carries_only_missing_files_then_verifies_all_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.populate(root)
            name = archive_name("3.12.16", TARGETS[0])
            data = (root / name).read_bytes()
            (root / name).unlink()
            source = {"name": name, "id": 123, "size": len(data),
                      "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
            plan = {"current_versions": self.versions, "carry_forward_sources": [source]}
            with patch("release_asset_download.open_https", return_value=io.BytesIO(data)) as transfer:
                download_carried_assets(plan, root, "fael-ai-org/python-builds")
                transfer.assert_called_once_with("https://api.github.com/repos/fael-ai-org/python-builds/releases/assets/123")
            self.assertEqual(len(validate_directory(root, self.versions)), 8)
            # All rebuilt files need no prior release or network request.
            with patch("release_asset_download.open_https") as transfer:
                download_carried_assets({**plan, "carry_forward_sources": []}, root, "fael-ai-org/python-builds")
                transfer.assert_not_called()

    def test_carry_download_rejects_missing_truncated_and_corrupt_sources(self):
        name = archive_name("3.12.16", TARGETS[0])
        data = b"original"
        asset = {"name": name, "id": 123, "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
        for mode in ("missing", "truncated", "corrupt"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                plan = {"current_versions": self.versions, "carry_forward_sources": [] if mode == "missing" else [asset]}
                body = data[:-1] if mode == "truncated" else b"modified"
                with patch("release_asset_download.open_https", return_value=io.BytesIO(body)):
                    with self.assertRaises(ValueError):
                        download_carried_assets(plan, Path(raw), "fael-ai-org/python-builds")

    def test_api_failures_are_not_treated_as_missing_release(self):
        with patch.dict(os.environ, {"GH_TOKEN": "test"}):
            for code in (401, 403, 500):
                with self.subTest(code=code), patch("release_inventory.urlopen", side_effect=HTTPError(
                        "https://api.github.com", code, "failure", {}, None)):
                    with self.assertRaises(RuntimeError):
                        fetch_inventory("tag", "owner/repo")

    def test_inventory_omits_untrusted_labels_and_urls_from_release_plan(self):
        inventory = self.inventory()
        for asset in inventory["assets"]:
            asset["label"] = "untrusted'$(command)"
            asset["url"] = "https://untrusted.invalid/"
        plan = self.plan(inventory)
        self.assertTrue(plan["carry_forward_sources"])
        self.assertTrue(all(set(a) == {"name", "id", "size", "digest"}
                            for a in plan["carry_forward_sources"]))

    def test_workflow_entry_paths_use_inventory_and_gate_before_mutations(self):
        for filename in ("check.yml", "build-python.yml"):
            text = (ROOT / ".github/workflows" / filename).read_text()
            self.assertIn("scripts/release_inventory.py fetch", text)
            self.assertIn("--inventory-file release-inventory.json", text)
        text = (ROOT / ".github/workflows/build-python.yml").read_text()
        gate = text.index("scripts/release_inventory.py verify")
        self.assertLess(gate, text.index("scripts/update_release_state.py \\", gate))
        self.assertLess(gate, text.index("      - name: Create release"))
        self.assertIn('plan["build_targets"]', text)
        self.assertNotIn("Could not download previous release", text)


if __name__ == "__main__":
    unittest.main()
