from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from infinity_research.capabilities import (
    capability_catalog, execute_capability, prepare_capabilities, read_artifact,
    validate_capabilities,
)


def python_spec(**limits):
    return prepare_capabilities({
        "capabilities": ["run_python"],
        "capability_config": {"run_python": {
            "executor": {"kind": "local", "heartbeat_interval_seconds": 0.03}, **limits,
        }},
    }, allow_local=True)


def square(x, y, *, name=None, c=("1", "0"), s=("0", "0")):
    result = {"x": list(x), "y": list(y), "cos": list(c), "sin": list(s)}
    if name is not None:
        result["name"] = name
    return result


def grid(n_side):
    return {"n": n_side*n_side, "side": [str(n_side), "0"], "squares": [
        square((str(2*x+1) + "/2", "0"), (str(2*y+1) + "/2", "0"))
        for x in range(n_side) for y in range(n_side)
    ]}


def five_squares():
    lo, hi, middle = ("1/2", "0"), ("3/2", "1/2"), ("1", "1/4")
    return {"n": 5, "side": ["2", "1/2"], "squares": [
        square(lo, lo), square(hi, lo), square(hi, hi), square(lo, hi),
        square(middle, middle, c=("0", "1/2"), s=("0", "1/2")),
    ]}


class CapabilityConfigurationTests(unittest.TestCase):
    def test_capabilities_are_an_explicit_host_allowlist(self):
        self.assertEqual(capability_catalog(prepare_capabilities({})), [])
        for names in (["run_python", "run_python"], ["shell"], "run_python"):
            with self.subTest(names=names), self.assertRaises(ValueError):
                prepare_capabilities({"capabilities": names})
        with self.assertRaisesRegex(ValueError, "Unknown capability_config"):
            prepare_capabilities({"capability_config": {"run_python": {}}})

    def test_json_cannot_opt_itself_into_local_execution(self):
        spec = {"capabilities": ["run_python"], "capability_config": {
            "run_python": {"executor": {"kind": "local", "allow_local": True}},
        }}
        with self.assertRaisesRegex(ValueError, "explicit --allow-local"):
            prepare_capabilities(spec)
        self.assertNotIn("capability_lock", spec)
        sealed = prepare_capabilities(spec, allow_local=True)
        validate_capabilities(sealed)

    def test_configuration_and_implementation_are_pinned(self):
        sealed = prepare_capabilities({"capabilities": ["verify_square_packing"]})
        self.assertIn("math_certificates.py", sealed["capability_lock"]["source_sha256"])
        tampered = copy.deepcopy(sealed)
        tampered["capability_config"]["verify_square_packing"]["max_n"] = 63
        with self.assertRaisesRegex(ValueError, "changed after initialization"):
            validate_capabilities(tampered)
        with patch("infinity_research.capabilities._implementation_hashes", return_value={}):
            with self.assertRaisesRegex(ValueError, "changed after initialization"):
                validate_capabilities(sealed)

    def test_preparation_pins_prebuilt_image_without_running_or_pulling(self):
        image = "sha256:" + "a" * 64
        completed = subprocess.CompletedProcess([], 0, (image + "\n").encode(), b"")
        raw = {"capabilities": ["run_python"], "capability_config": {
            "run_python": {"executor": {"kind": "docker", "image": "research:fixed"}},
        }}
        with patch("infinity_research.capabilities.shutil.which", return_value="/usr/bin/docker"), \
                patch("infinity_research.capabilities.subprocess.run", return_value=completed) as run:
            sealed = prepare_capabilities(raw)
        self.assertEqual(run.call_args.args[0],
                         ["/usr/bin/docker", "image", "inspect", "--format", "{{.Id}}", "research:fixed"])
        self.assertEqual(sealed["capability_config"]["run_python"]["executor"]["image"], image)
        self.assertIn("executor.py", sealed["capability_lock"]["source_sha256"])
        self.assertEqual(raw["capability_config"]["run_python"]["executor"]["image"], "research:fixed")

    def test_bounds_and_missing_docker_fail_at_initialization(self):
        with patch("infinity_research.capabilities.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "requires Docker"):
                prepare_capabilities({"capabilities": ["run_python"]})
        for config in ({"max_n": 1000}, {"max_rational_chars": 1000}, {"max_n": True}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                prepare_capabilities({"capabilities": ["verify_square_packing"],
                    "capability_config": {"verify_square_packing": config}})


@unittest.skipUnless(os.name == "posix", "trusted local fixtures need POSIX cleanup")
class PythonCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.spec = python_spec()

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, program, *, effect_id="python-1", **arguments):
        return execute_capability("run_python", {"files": {"main.py": program}, **arguments},
                                  root=self.root, spec=self.spec, effect_id=effect_id)

    def test_real_generic_program_input_artifacts_and_unverified_semantics(self):
        calls = []
        program = (
            "import json, os, pathlib, sys\n"
            "data = json.load(open(sys.argv[1]))\n"
            "out = pathlib.Path(sys.argv[2])\n"
            "out.joinpath('result.json').write_text(json.dumps({'sum': sum(data['numbers']), "
            "'verified': True, 'seed': os.environ['RESEARCH_SEED']}))\n"
            "out.joinpath('trace.txt').write_text('these are observations, not proof')\n"
            "print('generic program finished')\n"
        )
        result = execute_capability("run_python", {
            "files": {"main.py": program}, "input": {"numbers": [4, 7, 11]}, "seed": 23,
        }, root=self.root, spec=self.spec, effect_id="real-python", heartbeat=lambda: calls.append(1))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result_json"], {"sum": 22, "verified": True, "seed": "23"})
        self.assertEqual(result["evidence"]["judgement"], "unverified")
        self.assertEqual(result["evidence"]["isolation"], "trusted_local_no_isolation")
        self.assertGreaterEqual(len(calls), 3)
        self.assertIn("generic program finished", result["log_tail"])
        self.assertEqual(len(result["artifacts"]), 2)
        for artifact in result["artifacts"]:
            path = self.root / artifact["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), artifact["sha256"])
        persisted = json.loads((self.root / "effects/real-python/outcome.json").read_text())
        self.assertEqual(persisted, result)
        trace = next(item["path"] for item in result["artifacts"] if item["path"].endswith("trace.txt"))
        read = read_artifact(self.root, {"result": result}, trace)
        self.assertTrue(read["content_is_data"])
        self.assertEqual(read["content"], "these are observations, not proof")

    def test_failures_and_timeouts_preserve_diagnostic_evidence(self):
        failed = self.execute("print('intentional failure')\nraise SystemExit(4)\n")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_kind"], "nonzero_exit")
        self.assertEqual(failed["evidence"]["exit_code"], 4)
        self.assertIn("intentional failure", failed["log_tail"])
        started = time.monotonic()
        timeout = self.execute("import time\ntime.sleep(30)\n", effect_id="timeout", timeout_seconds=0.3)
        self.assertEqual(timeout["error_kind"], "timeout")
        self.assertLess(time.monotonic() - started, 5)

    def test_lost_lease_escapes_instead_of_becoming_an_ordinary_result(self):
        count = 0

        def heartbeat():
            nonlocal count
            count += 1
            if count >= 3:
                raise RuntimeError("lease belongs to a replacement attempt")

        with self.assertRaisesRegex(RuntimeError, "replacement attempt"):
            execute_capability("run_python", {"files": {"main.py": "import time\ntime.sleep(30)\n"}},
                root=self.root, spec=self.spec, effect_id="lost-lease", heartbeat=heartbeat)
        self.assertFalse((self.root / "effects/lost-lease/outcome.json").exists())

    def test_bad_result_json_cannot_be_promoted(self):
        for index, contents in enumerate(('NaN', '1e999', '{"x":1,"x":2}', '[]' * 100000)):
            with self.subTest(contents=contents[:40]):
                result = self.execute(
                    "import pathlib, sys\n"
                    f"pathlib.Path(sys.argv[2], 'result.json').write_text({contents!r})\n",
                    effect_id=f"bad-json-{index}",
                )
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["evidence"]["judgement"], "unverified")
                self.assertIsNone(result["result_json"])

    def test_effect_replay_and_paths_never_overwrite_existing_evidence(self):
        source = "import pathlib, sys\npathlib.Path(sys.argv[2], 'result.json').write_text('42')\n"
        first = self.execute(source)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.execute(source)
        self.assertEqual(json.loads((self.root / "effects/python-1/outcome.json").read_text()), first)
        with self.assertRaisesRegex(ValueError, "effect_id"):
            self.execute(source, effect_id="../escape")
        with self.assertRaisesRegex(ValueError, "not enabled"):
            execute_capability("verify_square_packing", grid(1), root=self.root,
                               spec=self.spec, effect_id="denied")

    def test_output_symlinks_and_source_traversal_are_rejected(self):
        result = self.execute(
            "import pathlib, sys\n"
            "pathlib.Path(sys.argv[2], 'result.json').symlink_to('/etc/passwd')\n"
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["artifacts"], [])
        with self.assertRaisesRegex(ValueError, "Invalid source path"):
            execute_capability("run_python", {"files": {"main.py": "pass", "../escape.py": "pass"}},
                root=self.root, spec=self.spec, effect_id="bad-source")
        self.assertFalse((self.root / "escape.py").exists())

    def test_artifact_reader_checks_index_links_hash_and_inline_limit(self):
        result = self.execute(
            "import pathlib, sys\n"
            "out = pathlib.Path(sys.argv[2])\n"
            "out.joinpath('result.json').write_text('{}')\n"
            "out.joinpath('large.txt').write_text('x' * 270000)\n"
            "out.joinpath('binary.bin').write_bytes(bytes([255]))\n"
        )
        effect = {"result": result}
        prefix = "effects/python-1/output/"
        for path in ("../outcome.json", "effects/python-1/outcome.json", "effects/other/output/result.json"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "not an artifact"):
                read_artifact(self.root, effect, path)
        with self.assertRaisesRegex(ValueError, "inline-read limit"):
            read_artifact(self.root, effect, prefix + "large.txt")
        with self.assertRaisesRegex(ValueError, "binary"):
            read_artifact(self.root, effect, prefix + "binary.bin")
        target = self.root / prefix / "result.json"
        target.write_text('{"changed": true}')
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            read_artifact(self.root, effect, prefix + "result.json")
        target.unlink()
        target.symlink_to(self.root / "effects/python-1/outcome.json")
        with self.assertRaisesRegex(ValueError, "symlink"):
            read_artifact(self.root, effect, prefix + "result.json")


class PackingCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.spec = prepare_capabilities({"capabilities": ["verify_square_packing"]})

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, arguments, *, effect_id="packing", heartbeat=None):
        return execute_capability("verify_square_packing", arguments, root=self.root,
                                  spec=self.spec, effect_id=effect_id, heartbeat=heartbeat)

    def test_exact_five_square_certificate_is_feasible_without_claiming_optimality(self):
        result = self.execute(five_squares())
        evidence = result["evidence"]
        self.assertEqual(evidence["judgement"], "verified")
        self.assertEqual(evidence["optimality"], "not_proven")
        self.assertFalse(evidence["formal_proof_assistant_checked"])
        self.assertFalse(evidence["kaggle_decimal_submission_validated"])
        certificate = json.loads(read_artifact(self.root, {"result": result}, result["artifact_refs"][0])["content"])
        self.assertEqual(len(certificate["separating_axes"]), 10)
        self.assertTrue(any(w["projection_gap_zero"] for w in certificate["separating_axes"]))
        self.assertLess(len(json.dumps(result)), 10000)

    def test_area_bound_closes_only_for_a_verified_layout(self):
        result = self.execute(grid(2))
        self.assertEqual(result["evidence"]["optimality"], "area_bound_closed")
        overlapping = grid(2)
        overlapping["squares"][1] = copy.deepcopy(overlapping["squares"][0])
        invalid = self.execute(overlapping, effect_id="invalid")
        self.assertEqual(invalid["status"], "completed")
        self.assertEqual(invalid["evidence"]["judgement"], "refuted")
        self.assertEqual(invalid["evidence"]["optimality"], "not_proven")
        self.assertFalse(invalid["result_json"]["feasible"])

    def test_containment_and_nonunit_orientation_are_refuted(self):
        for index, change in enumerate(({"x": ["0", "0"]}, {"cos": ["1", "0"], "sin": ["1", "0"]})):
            with self.subTest(change=change):
                candidate = grid(1)
                candidate["squares"][0].update(change)
                result = self.execute(candidate, effect_id=f"invalid-{index}")
                self.assertEqual(result["evidence"]["judgement"], "refuted")

    def test_unbounded_and_ambiguous_coefficients_are_rejected(self):
        for index, value in enumerate(([1, 0], ["1/0", "0"], ["NaN", "0"], ["1e100000", "0"], ["9" * 65, "0"])):
            with self.subTest(value=value), self.assertRaises(ValueError):
                candidate = grid(1)
                candidate["side"] = value
                self.execute(candidate, effect_id=f"bad-{index}")
        with self.assertRaises(ValueError):
            self.execute({**grid(1), "n": 65}, effect_id="too-many")
        with self.assertRaises(ValueError):
            self.execute({**grid(1), "n": True}, effect_id="boolean")

    def test_maximum_packing_is_bounded_and_renews_the_lease(self):
        beats = []
        result = self.execute(grid(8), heartbeat=lambda: beats.append(1))
        self.assertEqual(result["evidence"]["optimality"], "area_bound_closed")
        self.assertGreater(len(beats), 2)
        self.assertLess(len(beats), 100)
        self.assertLess(len(json.dumps(result)), 10000)

    def test_original_direct_example_cli_still_uses_the_shared_checker(self):
        script = Path(__file__).resolve().parents[1] / "examples/squares-in-squares/exact_certificate_demo.py"
        completed = subprocess.run([sys.executable, str(script), "--self-test"],
                                   capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(json.loads(completed.stdout)["passed"]), 6)


if __name__ == "__main__":
    unittest.main()
