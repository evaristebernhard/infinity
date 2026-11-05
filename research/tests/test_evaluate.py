"""Keep the Darwin fixture exception separate from production resource limits."""

import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch

from infinity_research.evaluate import _apply_resource_limits
from infinity_research.spec import load_task


class EvaluatorLimitTests(unittest.TestCase):
    def test_darwin_smoke_keeps_cpu_limit_and_discloses_memory_exception(self):
        def darwin_limit(which, limits):
            if which == resource.RLIMIT_AS:
                raise ValueError("current limit exceeds maximum limit")

        with patch("sys.platform", "darwin"), patch("resource.setrlimit", side_effect=darwin_limit) as setter:
            result = _apply_resource_limits("smoke", 512, 2.5)
        setter.assert_called_once_with(resource.RLIMIT_CPU, (3, 4))
        self.assertFalse(result["memory_enforced"])
        self.assertIsNone(result["address_space_limit_bytes"])
        self.assertIn("Trusted Darwin smoke", result["reason"])

    def test_darwin_rejects_every_other_domain_before_evaluation(self):
        for domain in ("filament", "unregistered"):
            with self.subTest(domain=domain), patch("sys.platform", "darwin"), patch("resource.setrlimit") as setter:
                with self.assertRaisesRegex(ValueError, "Production evaluation requires Linux/WSL"):
                    _apply_resource_limits(domain, 512, 2.5)
                setter.assert_not_called()

    def test_darwin_production_init_rejects_before_dataset_or_docker_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "task.json"
            # No dataset or Docker is needed to discover this unsupported host.
            task.write_text(json.dumps({"domain": "filament"}))
            with patch("sys.platform", "darwin"):
                with self.assertRaisesRegex(ValueError, "Production evaluation requires Linux/WSL"):
                    load_task(task)

    def test_linux_applies_address_space_and_cpu_limits_for_all_domains(self):
        for domain in ("smoke", "filament"):
            with self.subTest(domain=domain), patch("sys.platform", "linux"), patch("resource.setrlimit") as setter:
                result = _apply_resource_limits(domain, 512, 2.5)
                self.assertEqual(setter.call_args_list, [
                    call(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024)),
                    call(resource.RLIMIT_CPU, (3, 4)),
                ])
                self.assertTrue(result["memory_enforced"])

    def test_linux_resource_limit_failures_are_not_ignored_for_smoke(self):
        with patch("sys.platform", "linux"), patch("resource.setrlimit", side_effect=ValueError("denied")):
            with self.assertRaisesRegex(ValueError, "denied"):
                _apply_resource_limits("smoke", 512, 2.5)

    @unittest.skipUnless(sys.platform == "linux", "exercise the real Linux address-space limit")
    def test_linux_real_subprocess_rejects_allocation_above_memory_limit(self):
        # Run in another interpreter: lowering a hard limit in the test runner
        # would permanently affect unrelated tests.
        script = """
import json, resource
from infinity_research.evaluate import _apply_resource_limits
limits = _apply_resource_limits('smoke', 256, 5)
assert resource.getrlimit(resource.RLIMIT_CPU) == (5, 6)
print(json.dumps(limits), flush=True)
bytearray(512 * 1024 * 1024)
"""
        result = subprocess.run([sys.executable, "-c", script],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MemoryError", result.stderr)
        self.assertTrue(json.loads(result.stdout)["memory_enforced"])


if __name__ == "__main__":
    unittest.main()
