from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from infinity_research.cli import smoke
from infinity_research.core import experiment, finalize, predict_test, recover, verify_inputs
from infinity_research.domains import smoke as domain
from infinity_research.spec import file_hash, tree_hash
from infinity_research.store import ResearchStore
from infinity_research.executor import ExecutionError


class CampaignTests(unittest.TestCase):
    def test_test_prediction_uses_frozen_program_without_reopening_research(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            domain.prepare(data)
            (data / "public_test").mkdir()
            manifest = json.loads((data / "public" / "manifest.json").read_text())
            manifest.update(split="test", predict=[{"id": "unlabelled-test", "x": 1.0}])
            (data / "public_test" / "manifest.json").write_text(json.dumps(manifest))
            spec = domain.task(data)
            spec["data_sha256"] = tree_hash(data)
            spec["evaluator_sha256"] = file_hash(Path(domain.__file__))
            store = ResearchStore.create(root / "run", spec)
            with self.assertRaisesRegex(ValueError, "successful final"):
                predict_test(store, root / "test-output", timeout_seconds=5)
            code = domain.LINEAR + "\n(output / 'test_execution.json').mkdir()\n"
            candidate = experiment(store, {
                "idempotency_key": "frozen", "hypothesis": "fit the training data",
                "family": "linear", "files": {"main.py": code},
                "seed": 173, "timeout_seconds": 5,
            })
            final = finalize(store)
            before = store.report()
            result = predict_test(store, root / "test-output", timeout_seconds=5)
            self.assertEqual(result["status"], "completed_unscored")
            self.assertEqual(result["candidate_id"], candidate["id"])
            self.assertEqual(result["candidate_sha256"], final["evidence"]["candidate_sha256"])
            self.assertEqual(result["seed"], 173)
            self.assertIsNone(result["score"])
            self.assertFalse(result["charged_to_research_budget"])
            predictions = json.loads((root / "test-output" / "predictions.json").read_text())
            self.assertEqual([row["id"] for row in predictions["predictions"]], ["unlabelled-test"])
            self.assertTrue((root / "test-output" / "test_execution.json").is_dir())
            self.assertEqual(json.loads((root / "test-output.test_execution.json").read_text()), result)
            self.assertEqual((root / "test-output.source" / "main.py").read_text(), code)
            after = store.report()
            self.assertEqual(after["budget"], before["budget"])
            self.assertEqual(after["final_evaluation"], before["final_evaluation"])
            with self.assertRaisesRegex(ValueError, "must be new"):
                predict_test(store, root / "test-output", timeout_seconds=5)

    def test_real_subprocess_campaign_promotes_and_seals_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = smoke(Path(tmp) / "demo")
            self.assertLess(result["validation_scores"]["linear"], result["validation_scores"]["baseline"])
            self.assertLess(result["final_mse"], 0.01)
            self.assertTrue(result["duplicate_reused"])
            self.assertEqual(result["experiments_recorded"], 2)

    def test_failed_trial_does_not_block_recovery_and_inputs_are_sealed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            domain.prepare(root / "data")
            spec = domain.task(root / "data")
            spec["data_sha256"] = tree_hash(root / "data")
            spec["evaluator_sha256"] = file_hash(Path(domain.__file__))
            store = ResearchStore.create(root / "run", spec)
            bad = experiment(store, {"idempotency_key": "bad", "hypothesis": "failure probe",
                                    "family": "baseline", "files": {"main.py": "raise RuntimeError('intentional')"},
                                    "seed": 1, "timeout_seconds": 5})
            self.assertEqual(bad["state"], "failed")
            self.assertIsNone(store.context()["best_candidate_id"])
            queued = store.propose({"idempotency_key": "saved", "hypothesis": "persist before restart",
                                    "family": "linear", "files": {"main.py": domain.LINEAR},
                                    "seed": 1, "timeout_seconds": 5})
            fresh = ResearchStore(root / "run")
            self.assertEqual(recover(fresh)["states"][queued["candidate_id"]], "succeeded")
            with (root / "data" / "public" / "manifest.json").open("a") as stream:
                stream.write(" ")
            with self.assertRaisesRegex(ValueError, "dataset changed"):
                verify_inputs(fresh)

    def test_another_live_rpc_recovers_an_orphaned_queue_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            domain.prepare(root / "data")
            spec = domain.task(root / "data")
            spec["data_sha256"] = tree_hash(root / "data")
            spec["evaluator_sha256"] = file_hash(Path(domain.__file__))
            store = ResearchStore.create(root / "run", spec)
            orphan = store.propose({"idempotency_key": "orphan", "hypothesis": "caller vanished",
                                    "family": "baseline", "files": {"main.py": domain.BASELINE},
                                    "seed": 1, "timeout_seconds": 5})
            second = experiment(store, {"idempotency_key": "live", "hypothesis": "next worker",
                                       "family": "linear", "files": {"main.py": domain.LINEAR},
                                       "seed": 1, "timeout_seconds": 5})
            self.assertEqual(store.candidate(orphan["candidate_id"])["state"], "succeeded")
            self.assertEqual(second["state"], "succeeded")

    def test_cleanup_failure_stops_new_admission_instead_of_reusing_the_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            domain.prepare(root / "data")
            spec = domain.task(root / "data")
            spec["data_sha256"] = tree_hash(root / "data")
            spec["evaluator_sha256"] = file_hash(Path(domain.__file__))
            store = ResearchStore.create(root / "run", spec)
            with patch("infinity_research.core.execute_candidate", side_effect=ExecutionError("rm failed", kind="cleanup_failed")):
                result = experiment(store, {"idempotency_key": "cleanup", "hypothesis": "probe",
                                            "family": "baseline", "files": {"main.py": domain.BASELINE},
                                            "seed": 1, "timeout_seconds": 5})
            self.assertEqual(result["state"], "failed")
            self.assertTrue(store.context()["stop"])
            self.assertIn("infrastructure_failure", store.context()["stop_reason"])


if __name__ == "__main__":
    unittest.main()
