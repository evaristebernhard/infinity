"""Skill imports are bounded instruction assets, never executable authority."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from infinity_research.skills import MAX_FILE_BYTES, SkillRegistry


class SkillRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sources = self.root / "source"
        self.sources.mkdir()

    def skill(self, name="test-method", *, header=None, body="Inspect evidence before claiming success."):
        directory = self.sources / name
        directory.mkdir()
        header = header or f"name: {name}\ndescription: Use to check a research method."
        (directory / "SKILL.md").write_text(f"---\n{header}\n---\n\n{body}\n", encoding="utf-8")
        return directory

    def test_builtins_have_distinct_triggers_and_resources_load_on_demand(self):
        registry = SkillRegistry.builtin()
        catalog = registry.catalog()
        self.assertEqual(len(catalog), 12)
        self.assertEqual(len({row["description"] for row in catalog}), 12)
        self.assertTrue(all("content" not in row and "resources" not in row for row in catalog))
        expected = {"research-decomposition", "source-research", "experiment-design",
                    "evidence-review", "solver-improvement", "counterexample-search",
                    "formal-proof", "geometric-packing", "kaggle-validation", "code-debugging",
                    "reproduce-result", "strategy-evolution"}
        self.assertEqual({row["name"] for row in catalog}, expected)
        for name in expected:
            result = registry.load(name)
            self.assertFalse(result["grants_tools"])
            self.assertGreater(len(result["content"]), 500)
            self.assertNotIn("TODO", result["content"])
            self.assertEqual(result["provenance"]["kind"], "builtin")
            for resource in result["resources"]:
                self.assertEqual(registry.load(name, resource["path"])["sha256"], resource["sha256"])

    def test_yaml_folded_descriptions_and_optional_fields_do_not_grant_tools(self):
        directory = self.skill(header="""name: test-method
description: >-
  Compare numerical methods:
  preserve evidence and failures.
license: Apache-2.0
compatibility: An available execution tool
allowed-tools: Shell Admin
metadata:
  version: "1.0"
client-specific-flag: true""")
        script = directory / "scripts" / "probe.py"
        script.parent.mkdir()
        script.write_text("raise RuntimeError('A skill resource must never execute during loading')\n")
        registry = SkillRegistry.from_roots([self.sources])
        self.assertEqual(registry.catalog()[0]["description"],
                         "Compare numerical methods: preserve evidence and failures.")
        loaded = registry.load("test-method")
        self.assertEqual(loaded["metadata"]["allowed-tools"], "Shell Admin")
        self.assertFalse(loaded["grants_tools"])
        self.assertIn("raise RuntimeError", registry.load("test-method", "scripts/probe.py")["content"])

    def test_snapshot_keeps_exact_old_bytes_after_source_is_edited(self):
        package = self.skill()
        registry = SkillRegistry.from_roots([self.sources])
        snapshot = registry.snapshot(self.root / "sealed")
        before = registry.load("test-method")["content"]
        (package / "SKILL.md").write_text(before + "\nA later source revision.\n")
        frozen = SkillRegistry.from_snapshot(self.root / "sealed", expected_sha256=snapshot["sha256"])
        self.assertEqual(frozen.load("test-method")["content"], before)
        self.assertEqual(frozen.sha256, snapshot["sha256"])
        with self.assertRaisesRegex(ValueError, "changed after discovery"):
            registry.load("test-method")

    def test_snapshot_detects_changed_reference_and_new_files(self):
        package = self.skill()
        reference = package / "references" / "method.md"
        reference.parent.mkdir()
        reference.write_text("Original procedure.\n")
        registry = SkillRegistry.from_roots([self.sources])
        snapshot = registry.snapshot(self.root / "sealed")
        copied = self.root / "sealed" / "packages" / "test-method"
        (copied / "references" / "method.md").write_text("Changed procedure.\n")
        with self.assertRaisesRegex(ValueError, "package changed"):
            SkillRegistry.from_snapshot(self.root / "sealed", expected_sha256=snapshot["sha256"])
        (copied / "references" / "method.md").write_text("Original procedure.\n")
        (copied / "new.py").write_text("print('unrecorded')\n")
        with self.assertRaisesRegex(ValueError, "package changed"):
            SkillRegistry.from_snapshot(self.root / "sealed", expected_sha256=snapshot["sha256"])

    def test_lock_cannot_approve_its_own_changed_metadata(self):
        self.skill()
        registry = SkillRegistry.from_roots([self.sources])
        snapshot = registry.snapshot(self.root / "sealed")
        lock = self.root / "sealed" / "lock.json"
        manifest = json.loads(lock.read_text())
        manifest["skills"][0]["description"] = "A different procedure"
        lock.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            SkillRegistry.from_snapshot(self.root / "sealed", expected_sha256=snapshot["sha256"])
        with self.assertRaisesRegex(ValueError, "expected skill snapshot"):
            SkillRegistry.from_snapshot(self.root / "sealed", expected_sha256="")

    def test_resource_paths_cannot_escape_or_alias_the_package(self):
        self.skill()
        registry = SkillRegistry.from_roots([self.sources])
        for value in ("../secret.txt", "/secret.txt", "references/../../secret.txt",
                      "references\\secret.txt", "./SKILL.md", "references//method.md", "", "a\x00b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                registry.load("test-method", value)
        with self.assertRaisesRegex(ValueError, "Unknown resource"):
            registry.load("test-method", "unregistered.md")

    def test_symlinks_in_root_packages_and_resources_are_rejected(self):
        package = self.skill()
        outside = self.root / "outside.md"
        outside.write_text("Must not be read through a symlink.\n")
        (package / "link.md").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            SkillRegistry.from_roots([self.sources])
        (package / "link.md").unlink()
        linked_root = self.root / "linked"
        linked_root.symlink_to(self.sources, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "regular directory"):
            SkillRegistry.from_roots([linked_root])
        another = self.sources / "linked-package"
        another.symlink_to(package, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            SkillRegistry.from_roots([self.sources])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_special_files_are_rejected_without_reading_them(self):
        package = self.skill()
        os.mkfifo(package / "pipe")
        with self.assertRaisesRegex(ValueError, "regular files"):
            SkillRegistry.from_roots([self.sources])

    def test_file_size_and_file_count_are_bounded(self):
        package = self.skill()
        (package / "large.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            SkillRegistry.from_roots([self.sources])
        (package / "large.txt").unlink()
        for index in range(64):
            (package / f"part-{index}.txt").write_text("x")
        with self.assertRaisesRegex(ValueError, "64 files"):
            SkillRegistry.from_roots([self.sources])

    def test_duplicate_names_do_not_silently_shadow_another_skill(self):
        self.skill()
        with self.assertRaisesRegex(ValueError, "Duplicate skill name"):
            SkillRegistry.from_roots([self.sources, self.sources])

    def test_snapshot_selection_does_not_expose_unselected_skills(self):
        self.skill("first-method")
        self.skill("second-method")
        registry = SkillRegistry.from_roots([self.sources])
        snapshot = registry.snapshot(self.root / "sealed", names=["second-method"])
        frozen = SkillRegistry.from_snapshot(self.root / "sealed", expected_sha256=snapshot["sha256"])
        self.assertEqual([row["name"] for row in frozen.catalog()], ["second-method"])
        self.assertEqual(snapshot["skill_count"], 1)
        self.assertFalse((self.root / "sealed" / "packages" / "first-method").exists())
        with self.assertRaisesRegex(ValueError, "Unknown skill"):
            frozen.load("first-method")

    def test_existing_snapshot_is_never_overwritten_and_source_cannot_contain_snapshot(self):
        package = self.skill()
        registry = SkillRegistry.from_roots([self.sources])
        registry.snapshot(self.root / "sealed")
        with self.assertRaisesRegex(ValueError, "must not exist"):
            registry.snapshot(self.root / "sealed")
        with self.assertRaisesRegex(ValueError, "outside its source"):
            registry.snapshot(package / "nested")

    def test_binary_assets_are_hashed_and_copied_but_not_executed_or_text_decoded(self):
        package = self.skill()
        (package / "asset.bin").write_bytes(b"\xff\x00\x80")
        registry = SkillRegistry.from_roots([package])
        snapshot = registry.snapshot(self.root / "sealed")
        frozen = SkillRegistry.from_snapshot(self.root / "sealed", expected_sha256=snapshot["sha256"])
        self.assertEqual((self.root / "sealed" / "packages" / "test-method" / "asset.bin").read_bytes(),
                         b"\xff\x00\x80")
        with self.assertRaisesRegex(ValueError, "Binary skill assets"):
            frozen.load("test-method", "asset.bin")

    def test_ambiguous_yaml_and_mismatched_names_fail_before_disclosure(self):
        package = self.skill(header="name: test-method\nname: different-method\ndescription: Do something.")
        with self.assertRaisesRegex(ValueError, "unique strings"):
            SkillRegistry.from_roots([self.sources])
        (package / "SKILL.md").write_text("---\nname: wrong-name\ndescription: Do something.\n---\nInstructions")
        with self.assertRaisesRegex(ValueError, "match its containing directory"):
            SkillRegistry.from_roots([self.sources])

    def test_returned_catalog_and_metadata_cannot_mutate_registry(self):
        self.skill()
        registry = SkillRegistry.from_roots([self.sources])
        first = registry.catalog()
        first[0]["description"] = "Tampered"
        first[0]["provenance"]["source"] = "Another source"
        loaded = registry.load("test-method")
        loaded["resources"][0]["sha256"] = "0" * 64
        self.assertNotEqual(registry.catalog()[0]["description"], "Tampered")
        self.assertNotEqual(registry.load("test-method")["resources"][0]["sha256"], "0" * 64)


if __name__ == "__main__":
    unittest.main()
