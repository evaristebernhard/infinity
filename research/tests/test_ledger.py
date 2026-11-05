import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import unittest

from infinity_research.domains import smoke
from infinity_research.spec import file_hash, source_files, tree_hash
from infinity_research.store import ResearchStore


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        smoke.prepare(self.root / "data")
        self.spec = smoke.task(self.root / "data")
        self.spec["data_sha256"] = tree_hash(self.root / "data")
        self.spec["evaluator_sha256"] = file_hash(Path(smoke.__file__))
        self.store = ResearchStore.create(self.root / "run", self.spec)

    def tearDown(self):
        self.tmp.cleanup()

    def proposal(self, key, code=None, **kwargs):
        return self.store.propose({"idempotency_key": key, "hypothesis": key,
                                   "family": "baseline", "files": {"main.py": code or f"print({key!r})"},
                                   "seed": 42, "timeout_seconds": 5, **kwargs})["candidate_id"]

    def test_atomic_call_budget_across_independent_connections(self):
        def reserve(i):
            return ResearchStore(self.store.root).reserve_model_call(str(i))["allowed"]
        with ThreadPoolExecutor(max_workers=16) as pool:
            accepted = list(pool.map(reserve, range(100)))
        self.assertEqual(sum(accepted), self.spec["budget"]["max_model_calls"])
        self.assertFalse(self.store.reserve_model_call("0")["allowed"])

    def test_semantic_dedup_and_conflicting_replay(self):
        first = self.proposal("first", code="print(1)")
        second = self.proposal("second", code="print(1)")
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "Idempotency"):
            self.proposal("first", code="print(2)")
        self.assertEqual(self.store.context()["budget"]["experiments_used"], 1)

    def test_resource_slots_are_exclusive_and_completed_results_are_fenced(self):
        ids = [self.proposal(str(i)) for i in range(3)]
        self.assertIsNotNone(self.store.claim(ids[0], "a"))
        self.assertIsNotNone(self.store.claim(ids[1], "b"))
        self.assertIsNone(self.store.claim(ids[2], "c"))
        with self.assertRaises(RuntimeError):
            self.store.finish(ids[0], "wrong", result={"metric": "mse", "score": 1}, elapsed=1)
        self.store.finish(ids[0], "a", result={"metric": "mse", "score": 1}, elapsed=1)
        self.assertIsNotNone(self.store.claim(ids[2], "c"))
        with self.assertRaises(RuntimeError):
            self.store.finish(ids[0], "a", result={"metric": "mse", "score": 0}, elapsed=1)

    def test_budget_reservation_keeps_final_evaluation_capacity(self):
        self.proposal("a", timeout_seconds=10)
        self.proposal("b", timeout_seconds=10)
        self.proposal("c", timeout_seconds=10)
        self.proposal("d", timeout_seconds=10)
        context = self.store.context()
        self.assertEqual(context["budget"]["trial_seconds_reserved"], 40)
        self.assertEqual(context["stop_reason"], "experiment_budget_exhausted")

    def test_invalid_scores_never_promote(self):
        cid = self.proposal("bad")
        self.store.claim(cid, "owner")
        for result in ({"metric": "mse", "score": float("nan")},
                       {"metric": "fake", "score": 0}, {"metric": "mse", "score": True}):
            with self.assertRaises(ValueError):
                self.store.finish(cid, "owner", result=result, elapsed=1)
        self.assertIsNone(self.store.context()["best_candidate_id"])

    def test_recovery_charges_lost_worker_and_rejects_late_result(self):
        cid = self.proposal("lost")
        self.store.claim(cid, "old")
        with self.store._db(True) as db:
            db.execute("UPDATE candidates SET deadline=? WHERE id=?", (time.time()-1, cid))
        recovered = ResearchStore(self.store.root)
        self.assertEqual(recovered.context()["budget"]["trial_seconds_used"], 5)
        self.assertEqual(recovered.candidate(cid)["state"], "interrupted")
        with self.assertRaises(RuntimeError):
            recovered.finish(cid, "old", result={"metric": "mse", "score": 0}, elapsed=1)

    def test_contract_mutation_and_path_escape_are_rejected(self):
        for filename in ("../outside.py", "/tmp/out.py", "a/../../out.py", "a\\out.py", ".env"):
            with self.assertRaises(ValueError):
                source_files({"main.py": "pass", filename: "pass"})
        changed = copy.deepcopy(self.spec)
        changed["budget"]["max_model_calls"] += 100
        (self.store.root / "task.json").write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "contract changed"):
            ResearchStore(self.store.root)

    def test_holdout_is_once_only_and_absent_from_agent_context(self):
        cid = self.proposal("a")
        self.store.claim(cid, "o")
        self.store.finish(cid, "o", result={"metric": "mse", "score": 2}, elapsed=1)
        final = self.store.begin_final()
        with self.assertRaisesRegex(ValueError, "already started"):
            self.store.begin_final()
        self.store.finish_final(final["owner"], {"score": 1234, "metric": "mse"})
        self.assertNotIn("final_evaluation", self.store.context())
        self.assertEqual(self.store.report()["final_evaluation"]["score"], 1234)

    def test_small_improvement_still_promotes_the_best_candidate(self):
        a = self.proposal("a")
        self.store.claim(a, "a")
        self.store.finish(a, "a", result={"metric": "mse", "score": 1}, elapsed=1)
        b = self.proposal("b")
        self.store.claim(b, "b")
        self.store.finish(b, "b", result={"metric": "mse", "score": 1-1e-8}, elapsed=1)
        self.assertEqual(self.store.context()["best_candidate_id"], b)

    def test_full_reservations_are_temporary_not_a_permanent_stop(self):
        spec = copy.deepcopy(self.spec)
        spec["budget"]["max_total_trial_seconds"] = 30
        store = ResearchStore.create(self.root / "temporary-full", spec)
        for key in ("a", "b"):
            store.propose({"idempotency_key": key, "hypothesis": key, "family": "baseline",
                           "files": {"main.py": f"print({key!r})"}, "seed": 42, "timeout_seconds": 10})
        self.assertEqual(store.context()["budget"]["trial_seconds_available"], 0)
        self.assertFalse(store.context()["stop"])

    def test_finish_checks_expiry_without_an_intervening_context_read(self):
        cid = self.proposal("late")
        self.store.claim(cid, "owner")
        with self.store._db(True) as db:
            db.execute("UPDATE candidates SET deadline=? WHERE id=?", (time.time()-1, cid))
        with self.assertRaises(RuntimeError):
            self.store.finish(cid, "owner", result={"metric": "mse", "score": 0}, elapsed=1)
        self.assertEqual(self.store.candidate(cid)["state"], "interrupted")

    def test_search_stops_in_time_for_the_reserved_final_stage(self):
        with self.store._db(True) as db:
            db.execute("UPDATE campaign SET created=?", (time.time()-561,))
        context = self.store.context()
        self.assertEqual(context["stop_reason"], "wall_time_budget_exhausted")
        self.assertGreater(context["wall_seconds_remaining"], self.spec["budget"]["final_trial_seconds"])

    def test_deduplicated_request_alias_is_also_bound_to_its_original_content(self):
        self.proposal("first", code="print(1)")
        self.proposal("alias", code="print(1)")
        with self.assertRaisesRegex(ValueError, "Idempotency"):
            self.proposal("alias", code="print(2)")


if __name__ == "__main__":
    unittest.main()
