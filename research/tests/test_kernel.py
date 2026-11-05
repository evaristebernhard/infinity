"""Real SQLite concurrency/recovery tests for the domain-neutral research kernel."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from infinity_research.kernel import GenericStore, validate_contract


def contract(**limits):
    return {"schema_version": 2, "task_id": "generic-fixture",
            "goal": "Explore alternative constructions and report only checked evidence",
            "capabilities": ["exact_check", "artifact_store"],
            "limits": {"max_workers": 8, "max_model_calls": 100,
                       "max_output_tokens": 2048, "max_wall_seconds": 3600,
                       "max_work_items": 100, "max_failures": 3,
                       "lease_seconds": 60, **limits}}


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "campaign"
        self.store = GenericStore.create(self.root, contract())
        self.next_key = 0

    def op(self, operation, args=None, owner="worker-1", key=None):
        self.next_key += 1
        return self.store.dispatch_work(operation, args or {}, owner=owner,
                                        request_key=key or f"{owner}:{self.next_key}")

    def claim(self, work_id=None, owner="worker-1"):
        return self.op("claim_work", {"work_id": work_id} if work_id else {}, owner)

    def lease_args(self, claim):
        return {"work_id": claim["work_id"], "lease_token": claim["lease_token"]}

    def complete(self, claim, owner="worker-1", **result):
        return self.op("complete_work", {**self.lease_args(claim), "result": result}, owner)

    def expire(self, seconds=61):
        # Advancing the ledger clock avoids timing-sensitive sleeps in recovery tests.
        return patch("infinity_research.kernel.time.time", return_value=time.time() + seconds)

    def test_schema_is_generic_and_contract_is_sealed(self):
        context = self.store.context()
        self.assertEqual(context["run_kind"], "generic")
        self.assertEqual(context["ready_work_count"], 1)
        self.assertEqual(context["budget"]["max_parallel_tools"], 4)
        self.assertNotIn("data", self.store.spec)
        self.assertNotIn("objective", self.store.spec)
        changed = json.loads((self.root / "task.json").read_text())
        changed["goal"] = "A more convenient objective"
        (self.root / "task.json").write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "contract changed"):
            GenericStore(self.root)
        with self.assertRaisesRegex(ValueError, "schema_version 2"):
            validate_contract({**contract(), "schema_version": 1})
        for bad in (True, 0, -1, float("inf"), "100"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_contract(contract(max_workers=bad))

    def test_initialization_accepts_only_a_prepared_skill_directory(self):
        other = Path(self.tmp.name) / "with-skills"
        (other / "skills").mkdir(parents=True)
        (other / "skills" / "manifest.json").write_text("{}")
        store = GenericStore.create(other, contract())
        self.assertEqual(store.read_work("root")["state"], "pending")
        with self.assertRaisesRegex(ValueError, "new or empty"):
            GenericStore.create(other, contract())
        bad = Path(self.tmp.name) / "with-unrelated-files"
        bad.mkdir()
        (bad / "other.txt").write_text("preserve me")
        with self.assertRaisesRegex(ValueError, "new or empty"):
            GenericStore.create(bad, contract())
        self.assertEqual((bad / "other.txt").read_text(), "preserve me")

    def test_concurrent_claims_admit_exactly_one_owner_for_one_item(self):
        def claim(index):
            fresh = GenericStore(self.root)
            return fresh.dispatch_work("claim_work", {}, owner=f"worker-{index}", request_key=f"claim-{index}")

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(claim, range(100)))
        claimed = [item for item in results if item["claimed"]]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(self.store.context()["work_counts"]["leased"], 1)
        lease = claimed[0]
        public = self.store.read_work("root")
        self.assertNotIn("lease_token", public)
        self.assertNotIn(lease["lease_token"], json.dumps(self.store.context()))

    def test_request_identity_binds_owner_operation_and_content(self):
        args = {"goal": "Investigate branch A", "parent_id": "root"}
        first = self.op("create_work", args, key="create-id")
        replay = self.op("create_work", args, key="create-id")
        self.assertEqual(first["work_id"], replay["work_id"])
        self.assertTrue(replay["duplicate"])
        for changed, owner in (({**args, "goal": "Different"}, "worker-1"), (args, "worker-2")):
            with self.assertRaisesRegex(ValueError, "different operation, owner or content"):
                self.op("create_work", changed, owner, key="create-id")
        with self.assertRaisesRegex(ValueError, "different operation, owner or content"):
            self.op("claim_work", {}, key="create-id")
        self.assertEqual(len(self.store.list_work()["items"]), 2)

    def test_decomposition_waits_without_failure_then_parent_resumes(self):
        root = self.claim()
        children = [self.op("create_work", {"goal": f"Alternative {index}", "parent_id": "root"})["work_id"]
                    for index in range(2)]
        waiting = self.op("wait_work", {**self.lease_args(root), "depends_on": children})
        self.assertEqual(waiting["state"], "pending")
        self.assertEqual(waiting["failure_count"], 0)
        self.assertEqual(self.store.context()["ready_work_count"], 2)
        self.assertFalse(self.store.context()["stop"])
        self.assertFalse(self.claim("root")["claimed"])
        for index, child in enumerate(children):
            lease = self.claim(child, owner=f"child-{index}")
            self.complete(lease, owner=f"child-{index}", note="child outcome")
        resumed = self.claim("root")
        self.assertNotEqual(root["lease_token"], resumed["lease_token"])
        self.assertEqual(resumed["work"]["attempt_count"], 2)
        self.assertEqual(resumed["work"]["failure_count"], 0)
        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            self.complete(root, note="late parent result")
        final = self.complete(resumed, conclusion="Compare both branches", verified=True)
        self.assertEqual(final["result_status"], "agent_authored_not_independently_verified")
        self.assertEqual(self.store.context()["stop_reason"], "work_graph_completed")
        self.assertEqual(self.store.context()["objective_status"], "not_inferred_from_work_completion")

    def test_dependency_cycle_is_rejected_and_failed_dependencies_block_transitively(self):
        root = self.claim()
        child = self.op("create_work", {"goal": "Wait for root", "depends_on": ["root"]})["work_id"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.op("wait_work", {**self.lease_args(root), "depends_on": [child]})
        with self.assertRaisesRegex(ValueError, "itself"):
            self.op("wait_work", {**self.lease_args(root), "depends_on": ["root"]})
        self.assertEqual(self.store.read_work("root")["state"], "leased")
        tail = self.op("create_work", {"goal": "After child", "depends_on": [child]})["work_id"]
        self.op("fail_work", {**self.lease_args(root), "error": "No route found", "retry": False})
        self.assertEqual(self.store.read_work(child)["state"], "blocked")
        self.assertEqual(self.store.read_work(tail)["state"], "blocked")
        self.assertEqual(self.store.context()["stop_reason"], "work_graph_exhausted")
        self.assertEqual(self.store.context()["objective_status"], "not_inferred_from_work_completion")

    def test_lost_lease_requeues_then_fences_a_late_completion(self):
        old = self.claim()
        self.assertEqual(self.store.recover()["expired_leases"], 0)
        with self.expire():
            fresh = GenericStore(self.root)
            recovered = fresh.recover()
            self.assertEqual(recovered["expired_leases"], 1)
            new = fresh.dispatch_work("claim_work", {"work_id": "root"}, owner="replacement", request_key="replacement-claim")
            self.assertNotEqual(old["lease_token"], new["lease_token"])
            self.assertEqual(new["work"]["failure_count"], 1)
            with self.assertRaisesRegex(RuntimeError, "lease lost"):
                self.complete(old, note="late stale answer")
            fresh.dispatch_work("complete_work", {**self.lease_args(new), "result": {"note": "recovered"}},
                                owner="replacement", request_key="replacement-complete")
        self.assertEqual(self.store.read_work("root")["result"], {"note": "recovered"})

    def test_expiry_retries_are_bounded_but_waits_do_not_count_as_failures(self):
        for failure in range(3):
            lease = self.claim()
            self.assertEqual(lease["work"]["failure_count"], failure)
            result = self.op("fail_work", {**self.lease_args(lease), "error": "Attempt failed"})
            self.assertEqual(result["state"], "pending" if failure < 2 else "failed")
        self.assertEqual(self.store.context()["stop_reason"], "work_graph_exhausted")

    def test_lease_owner_is_transport_identity_and_renewal_is_fenced(self):
        lease = self.claim()
        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            self.op("renew_work", self.lease_args(lease), owner="impostor")
        with patch("infinity_research.kernel.time.time", return_value=time.time() + 20):
            renewed = self.op("renew_work", self.lease_args(lease))
        self.assertGreater(renewed["lease_expires"], lease["lease_expires"])
        self.assertEqual(renewed["failure_count"], 0)
        self.assertFalse(self.claim("root", owner="other")["claimed"])

    def test_host_heartbeat_keeps_long_thinking_alive_without_reviving_absent_owners(self):
        root = self.claim()
        child = self.op("create_work", {"goal": "A worker which will disappear"})["work_id"]
        lost = self.claim(child, owner="lost-worker")
        before = self.store.report()
        just_before_expiry = min(root["lease_expires"], lost["lease_expires"]) - 1
        with patch("infinity_research.kernel.time.time", return_value=just_before_expiry):
            heartbeat = self.store.renew_live_leases(["worker-1"])
        self.assertEqual(heartbeat["renewed"], 1)
        self.assertGreater(heartbeat["lease_expires"], root["lease_expires"])
        # A successful heartbeat does not inflate the event journal or create
        # a model/tool receipt; polling may run for days.
        self.assertEqual(len(self.store.report()["events"]), len(before["events"]))
        after_old_expiry = max(root["lease_expires"], lost["lease_expires"]) + 1
        with patch("infinity_research.kernel.time.time", return_value=after_old_expiry):
            recovery = self.store.recover()
            self.assertEqual(recovery["expired_leases"], 1)
            self.assertEqual(self.store.read_work("root")["state"], "leased")
            self.assertEqual(self.store.read_work(child)["state"], "pending")
            not_revived = self.store.renew_live_leases(["lost-worker"])
            self.assertEqual(not_revived["renewed"], 0)
            self.assertEqual(self.store.read_work(child)["failure_count"], 1)
            self.complete(root, note="A healthy model finished after the original expiry")

    def test_host_heartbeat_expires_before_renewal_and_preserves_unknown_effects(self):
        lease = self.claim()
        effect = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "lost-tool")
        with patch("infinity_research.kernel.time.time", return_value=lease["lease_expires"] + 1):
            result = self.store.renew_live_leases(["worker-1"])
            self.assertEqual(result["renewed"], 0)
            self.assertEqual(result["expired_leases"], 1)
            self.assertEqual(result["interrupted_effects"], 1)
            self.assertEqual(self.store.effect(effect["effect_id"])["state"], "interrupted")
            self.assertEqual(self.store.read_work("root")["state"], "pending")
            with self.assertRaisesRegex(RuntimeError, "lease lost"):
                self.complete(lease, note="Cannot revive a stale result")

    def test_owner_stop_allows_only_existing_live_work_to_renew_until_hard_wall(self):
        lease = self.claim()
        child = self.op("create_work", {"goal": "Must never be claimed after stop"})["work_id"]
        self.store.stop()
        with patch("infinity_research.kernel.time.time", return_value=lease["lease_expires"] - 1):
            heartbeat = self.store.renew_live_leases(["worker-1"])
            self.assertEqual(heartbeat["renewed"], 1)
            with self.assertRaisesRegex(RuntimeError, "admission closed"):
                self.claim(child, owner="replacement")
            self.assertEqual(self.store.read_work(child)["state"], "cancelled")
        with patch("infinity_research.kernel.time.time", return_value=time.time() + 3601):
            expired = self.store.renew_live_leases(["worker-1"])
            self.assertEqual(expired["renewed"], 0)
            self.assertEqual(self.store.read_work("root")["state"], "cancelled")

    def test_host_heartbeat_validates_owners_and_is_not_an_agent_operation(self):
        for owners in (None, "worker-1", [""], [" "], [True], ["x" * 301],
                       ["same", "same"], [f"worker-{index}" for index in range(9)]):
            with self.subTest(owners=owners), self.assertRaises(ValueError):
                self.store.renew_live_leases(owners)
        self.assertEqual(self.store.renew_live_leases([])["renewed"], 0)
        with self.assertRaisesRegex(ValueError, "Unknown work operation"):
            self.op("renew_live_leases", {"owners": ["worker-1"]})

    def test_effect_reservation_is_idempotent_and_unknown_outcomes_never_replay(self):
        lease = self.claim()
        args = ("worker-1", "root", lease["lease_token"], "exact_check", {"candidate": 7}, "check-key")
        started = self.store.reserve_effect(*args)
        self.assertTrue(started["execute"])
        duplicate = self.store.reserve_effect(*args)
        self.assertFalse(duplicate["execute"])
        self.assertEqual(duplicate["state"], "running")
        with self.assertRaisesRegex(ValueError, "different content or lease"):
            self.store.reserve_effect(*args[:4], {"candidate": 8}, "check-key")
        with self.assertRaisesRegex(RuntimeError, "running tools"):
            self.complete(lease, note="cannot leave while tool runs")
        with self.expire():
            recovered = GenericStore(self.root).recover()
            self.assertEqual(recovered["interrupted_effects"], 1)
            unknown = self.store.reserve_effect(*args)
            self.assertFalse(unknown["execute"])
            self.assertEqual(unknown["state"], "interrupted")
            self.assertIsNone(unknown["result"])
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.store.finish_effect(started["effect_id"], "worker-1", "root", lease["lease_token"],
                                         result={"judgement": "verified"})
        self.assertEqual(self.store.effect(started["effect_id"])["state"], "interrupted")

    def test_host_interrupt_releases_orphaned_tool_even_while_worker_heartbeats(self):
        self.store = GenericStore.create(Path(self.tmp.name) / "one-tool-slot", contract(max_parallel_tools=1))
        lease = self.claim()
        effect = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "lost-rpc")
        with self.assertRaisesRegex(RuntimeError, "capacity busy"):
            self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "next-rpc")
        with patch("infinity_research.kernel.time.time", return_value=lease["lease_expires"] - 1):
            self.assertEqual(self.store.renew_live_leases(["worker-1"])["renewed"], 1)
        with patch("infinity_research.kernel.time.time", return_value=lease["lease_expires"] + 1):
            self.assertEqual(self.store.context()["active_tools"], 1)
            interrupted = self.store.interrupt_effect("worker-1", "lost-rpc", "RPC process exited before replying")
            self.assertTrue(interrupted["found"])
            self.assertTrue(interrupted["interrupted"])
            self.assertEqual(interrupted["effect"]["state"], "interrupted")
            self.assertIn("unknown", interrupted["effect"]["error"])
            self.assertIsNone(interrupted["effect"]["result"])
            self.assertEqual(self.store.context()["active_tools"], 0)
            self.assertEqual(self.store.read_work("root")["state"], "leased")
            self.assertEqual(self.store.read_work("root")["failure_count"], 0)
            replacement = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "next-rpc")
            self.assertTrue(replacement["execute"])
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.store.finish_effect(effect["id"], "worker-1", "root", lease["lease_token"],
                                         result={"judgement": "verified"})
            self.store.finish_effect(replacement["id"], "worker-1", "root", lease["lease_token"],
                                     result={"judgement": "unknown"})
            self.complete(lease, note="Report checked evidence and retain the interrupted outcome")

    def test_host_interruption_is_idempotent_and_does_not_overwrite_finished_receipts(self):
        lease = self.claim()
        effect = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "interrupt-once")
        first = self.store.interrupt_effect("worker-1", "interrupt-once", "transport closed")
        event_count = len(self.store.report()["events"])
        again = self.store.interrupt_effect("worker-1", "interrupt-once", "second notification")
        self.assertFalse(again["interrupted"])
        self.assertEqual(first["effect"], again["effect"])
        self.assertEqual(len(self.store.report()["events"]), event_count)
        self.assertEqual(self.store.effect(effect["id"]), first["effect"])
        for index, outcome in enumerate(({"result": {"judgement": "verified"}}, {"error": "bad candidate"})):
            key = f"finished-{index}"
            record = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, key)
            finished = self.store.finish_effect(record["id"], "worker-1", "root", lease["lease_token"], **outcome)
            notification = self.store.interrupt_effect("worker-1", key, "reply was lost after commit")
            self.assertFalse(notification["interrupted"])
            self.assertEqual(notification["effect"], finished)
            self.assertEqual(self.store.effect(record["id"]), finished)

    def test_host_interruption_checks_owner_and_is_not_an_agent_operation(self):
        lease = self.claim()
        effect = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "owned")
        with self.assertRaisesRegex(RuntimeError, "another owner"):
            self.store.interrupt_effect("foreign-worker", "owned", "wrong caller")
        self.assertEqual(self.store.effect(effect["id"])["state"], "running")
        self.assertEqual(self.store.interrupt_effect("worker-1", "not-reserved", "RPC failed before admission"),
                         {"found": False, "interrupted": False})
        for reason in ("", " ", None, "x" * 8001):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                self.store.interrupt_effect("worker-1", "owned", reason)
        with self.assertRaisesRegex(ValueError, "Unknown work operation"):
            self.op("interrupt_effect", {"request_key": "owned", "reason": "agent requested interruption"})

    def test_host_result_is_immutable_but_unknown_judgement_is_not_false(self):
        lease = self.claim()
        effect = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "host-check")
        result = {"judgement": "unknown", "method": "bounded_checker", "reason": "precision exhausted"}
        finished = self.store.finish_effect(effect["effect_id"], "worker-1", "root", lease["lease_token"], result=result)
        self.assertEqual(finished["state"], "succeeded")  # Tool completed, mathematical verdict remains unknown.
        self.assertEqual(finished["result"], result)
        self.assertEqual(finished["result_status"], "host_tool_output")
        duplicate = self.store.finish_effect(effect["effect_id"], "worker-1", "root", lease["lease_token"], result=result)
        self.assertTrue(duplicate["duplicate"])
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.finish_effect(effect["effect_id"], "worker-1", "root", lease["lease_token"],
                                     result={"judgement": "verified"})
        final = self.op("complete_work", {**self.lease_args(lease), "result": {"status": "unresolved"},
                                           "effect_ids": [effect["effect_id"]]})
        self.assertEqual(final["effect_ids"], [effect["effect_id"]])
        self.assertEqual(final["result_status"], "agent_authored_not_independently_verified")
        self.assertNotIn("lease_token", self.store.effect(effect["effect_id"]))

    def test_effect_result_and_host_stop_are_atomic_to_readers_and_admission(self):
        lease = self.claim()
        effect = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "needs-stop")
        other_id = self.op("create_work", {"goal": "Concurrent ready worker"})["work_id"]
        other_lease = self.claim(other_id, owner="other-worker")
        other = GenericStore(self.root)
        result_written, release_stop, contender_started = threading.Event(), threading.Event(), threading.Event()
        original_stop = self.store._stop_in_transaction

        def pause_before_stop(db, reason):
            result_written.set()
            if not release_stop.wait(timeout=5):
                raise RuntimeError("Test did not release the paused stop transaction")
            original_stop(db, reason)

        def competing_admission():
            contender_started.set()
            return other.reserve_effect("other-worker", other_id, other_lease["lease_token"],
                                        "exact_check", {}, "competing-effect")

        with patch.object(self.store, "_stop_in_transaction", side_effect=pause_before_stop):
            with ThreadPoolExecutor(max_workers=2) as pool:
                finisher = pool.submit(self.store.finish_effect, effect["id"], "worker-1", "root", lease["lease_token"],
                                       result={"diagnostic": "host requests a stop"}, stop_reason="host_failure")
                try:
                    self.assertTrue(result_written.wait(timeout=3))
                    # The finish update has executed, but its encompassing
                    # transaction has not committed. A separate WAL reader must
                    # still see the old receipt, never completed + active.
                    with sqlite3.connect(self.store.path) as reader:
                        visible = reader.execute("""SELECT e.state,e.result,c.status FROM effects e
                            CROSS JOIN campaign c WHERE e.id=? AND c.id=1""", (effect["id"],)).fetchone()
                    self.assertEqual(visible, ("running", None, "active"))
                    contender = pool.submit(competing_admission)
                    self.assertTrue(contender_started.wait(timeout=3))
                    with self.assertRaises(TimeoutError):
                        contender.result(timeout=0.05)
                finally:
                    release_stop.set()
                self.assertEqual(finisher.result(timeout=3)["state"], "succeeded")
                with self.assertRaisesRegex(RuntimeError, "admission closed: host_failure"):
                    contender.result(timeout=3)
        with sqlite3.connect(self.store.path) as reader:
            visible = reader.execute("""SELECT e.state,c.status,c.reason FROM effects e
                CROSS JOIN campaign c WHERE e.id=? AND c.id=1""", (effect["id"],)).fetchone()
            admitted = reader.execute("SELECT COUNT(*) FROM effects WHERE request_key='competing-effect'").fetchone()[0]
        self.assertEqual(visible, ("succeeded", "stopped", "host_failure"))
        self.assertEqual(admitted, 0)

    def test_failed_atomic_stop_rolls_back_receipt_campaign_and_queue_together(self):
        lease = self.claim()
        child = self.op("create_work", {"goal": "Must remain pending if the transaction rolls back"})["work_id"]
        effect = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "rollback-stop")
        previous_events = self.store.report()["events"]
        original_event = self.store._event

        def fail_after_stop_update(db, kind, payload):
            if kind == "campaign_stopped":
                # Fail after the stop and queued-work cancellation SQL ran.
                raise RuntimeError("Simulated process failure before commit")
            original_event(db, kind, payload)

        with patch.object(self.store, "_event", side_effect=fail_after_stop_update):
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                self.store.finish_effect(effect["id"], "worker-1", "root", lease["lease_token"],
                                         result={"diagnostic": "host failure"}, stop_reason="host_failure")
        resumed = GenericStore(self.root)
        self.assertEqual(resumed.effect(effect["id"])["state"], "running")
        self.assertIsNone(resumed.effect(effect["id"])["result"])
        self.assertFalse(resumed.context()["stop"])
        self.assertEqual(resumed.read_work(child)["state"], "pending")
        self.assertEqual(resumed.report()["events"], previous_events)

    def test_restart_discovers_effect_committed_before_work_completion(self):
        old = self.claim()
        effect = self.store.reserve_effect("worker-1", "root", old["lease_token"], "exact_check",
                                           {"candidate": "a concrete construction"}, "before-crash")
        result = {"judgement": "verified", "scope": "finite construction only"}
        committed = self.store.finish_effect(effect["id"], "worker-1", "root", old["lease_token"], result=result)
        self.assertEqual(self.store.read_work("root")["effect_ids"], [])
        # The launcher exits here: no complete_work call, no old conversation.
        with patch("infinity_research.kernel.time.time", return_value=old["lease_expires"] + 1):
            restarted = GenericStore(self.root)
            fresh = restarted.dispatch_work("claim_work", {"work_id": "root"},
                                             owner="resumed-worker", request_key="after-crash")
            self.assertTrue(fresh["claimed"])
            self.assertEqual(fresh["work"]["effect_ids"], [])
            self.assertEqual(fresh["work"]["effect_count"], 1)
            summary = fresh["work"]["recent_effects"][0]
            self.assertEqual(summary["id"], effect["id"])
            self.assertEqual(summary["attempt"], 1)
            self.assertTrue(summary["result_available"])
            self.assertEqual(summary["state"], "succeeded")
            reread = restarted.effect(summary["id"])
            self.assertEqual(reread["result_sha256"], committed["result_sha256"])
            self.assertEqual(reread["result"], result)
            self.assertEqual(restarted.read_work("root")["recent_effects"], [summary])

    def test_effect_pagination_is_complete_bounded_and_omits_payloads_and_authority(self):
        lease = self.claim()
        other = self.op("create_work", {"goal": "No effects here"})["work_id"]
        ids, states = set(), set()
        for index in range(16):
            key = f"private-request-{index}"
            effect = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check",
                                               {"private_argument": "not discovery metadata"}, key)
            ids.add(effect["id"])
            if index >= 12:
                states.add("running")
            elif index % 3 == 0:
                self.store.finish_effect(effect["id"], "worker-1", "root", lease["lease_token"],
                                         result={"private_result": "read the selected effect separately"})
                states.add("succeeded")
            elif index % 3 == 1:
                self.store.finish_effect(effect["id"], "worker-1", "root", lease["lease_token"],
                                         error="private diagnostic")
                states.add("failed")
            else:
                self.store.interrupt_effect("worker-1", key, "private interruption reason")
                states.add("interrupted")
        seen, cursor, seen_states = [], None, set()
        expected_keys = {"id", "capability", "state", "attempt", "created", "finished",
                         "result_status", "result_available"}
        while True:
            page = self.store.list_effects("root", after_id=cursor, limit=3)
            self.assertLessEqual(len(page["items"]), 3)
            for item in page["items"]:
                self.assertEqual(set(item), expected_keys)
                seen.append(item["id"])
                seen_states.add(item["state"])
                self.assertEqual(item["result_available"], item["state"] == "succeeded")
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, sorted(ids))
        self.assertEqual(seen_states, states)
        current = self.store.read_work("root")
        self.assertEqual(current["effect_count"], 16)
        self.assertEqual(len(current["recent_effects"]), 8)
        self.assertTrue(all(set(item) == expected_keys for item in current["recent_effects"]))
        self.assertEqual(current["effect_ids"], [])
        self.assertEqual(self.store.list_effects(other), {"items": [], "next_cursor": None})
        for limit in (0, 101, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.store.list_effects("root", limit=limit)
        with self.assertRaisesRegex(ValueError, "Unknown work"):
            self.store.list_effects("not-a-work-item")

    def test_bounded_large_tool_results_fit_the_capability_envelope(self):
        lease = self.claim()
        effect = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "large")
        result = {"candidate_output": "x" * (600 * 1024), "judgement": "unverified"}
        self.store.finish_effect(effect["effect_id"], "worker-1", "root", lease["lease_token"], result=result)
        self.assertEqual(self.store.effect(effect["effect_id"])["result"], result)

    def test_capability_allowlist_and_effect_slots_are_enforced_atomically(self):
        root = self.claim()
        with self.assertRaisesRegex(ValueError, "not enabled"):
            self.store.reserve_effect("worker-1", "root", root["lease_token"], "unregistered", {}, "denied")
        leases = [("worker-1", root)]
        for index in range(5):
            child = self.op("create_work", {"goal": f"Branch {index}"})["work_id"]
            owner = f"extra-{index}"
            leases.append((owner, self.claim(child, owner)))

        def reserve(item):
            owner, lease = item
            try:
                return GenericStore(self.root).reserve_effect(owner, lease["work_id"], lease["lease_token"],
                                                              "exact_check", {}, f"tool-{owner}")
            except RuntimeError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(reserve, leases))
        accepted = [item for item in results if isinstance(item, dict)]
        refused = [item for item in results if isinstance(item, str)]
        self.assertEqual(len(accepted), 4)
        self.assertEqual(len(refused), 2)
        self.assertTrue(all("capacity busy" in message for message in refused))
        self.assertEqual(self.store.context()["active_tools"], 4)
        self.assertFalse(self.store.context()["stop"])

    def test_effect_and_work_transport_keys_cannot_alias(self):
        lease = self.claim()
        self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "same-key")
        with self.assertRaisesRegex(ValueError, "host tool effect"):
            self.op("create_work", {"goal": "alias"}, key="same-key")
        self.op("create_work", {"goal": "bound key"}, key="work-key")
        with self.assertRaisesRegex(ValueError, "work mutation"):
            self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "work-key")

    def test_model_admission_survives_restart_and_drains_already_admitted_work(self):
        other = GenericStore.create(Path(self.tmp.name) / "one-call", contract(max_model_calls=1))
        lease = other.dispatch_work("claim_work", {}, owner="owner", request_key="claim")
        admission = other.reserve_model_call("request")
        self.assertTrue(admission["allowed"])
        other = GenericStore(other.root)
        self.assertEqual(other.reserve_model_call("request")["reason"], "request_id_already_reserved")
        self.assertFalse(other.reserve_model_call("next")["allowed"])
        effect = other.reserve_effect("owner", "root", lease["lease_token"], "exact_check", {}, "last-tool")
        self.assertTrue(other.context()["draining"])
        other.finish_effect(effect["effect_id"], "owner", "root", lease["lease_token"], result={"value": 1})
        other.dispatch_work("complete_work", {**self.lease_args(lease), "result": {"finished": True}},
                            owner="owner", request_key="finish")
        self.assertEqual(other.context()["stop_reason"], "work_graph_completed")
        other.record_usage("request", 20, 10)
        self.assertTrue(other.record_usage("request", 20, 10)["duplicate"])
        with self.assertRaisesRegex(ValueError, "Conflicting usage"):
            other.record_usage("request", 20, 11)

    def test_stop_cancels_pending_work_but_preserves_inflight_tool_receipt(self):
        lease = self.claim()
        child = self.op("create_work", {"goal": "Queued"})["work_id"]
        effect = self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "before-stop")
        self.store.stop()
        self.assertEqual(self.store.read_work(child)["state"], "cancelled")
        self.assertTrue(self.store.context()["draining"])
        with self.assertRaisesRegex(RuntimeError, "admission closed"):
            self.store.reserve_effect("worker-1", "root", lease["lease_token"], "exact_check", {}, "after-stop")
        self.store.finish_effect(effect["effect_id"], "worker-1", "root", lease["lease_token"], result={"value": 1})
        self.complete(lease, note="inflight work drained")
        self.assertEqual(self.store.context()["stop_reason"], "stopped_by_owner")
        self.assertFalse(self.store.context()["draining"])

    def test_pagination_covers_large_graph_without_exposing_lease_credentials(self):
        lease = self.claim()
        ids = {"root"}
        for index in range(40):
            ids.add(self.op("create_work", {"goal": f"Branch {index}"})["work_id"])
        seen, cursor = [], None
        while True:
            page = self.store.list_work(after_id=cursor, limit=7)
            seen.extend(item["id"] for item in page["items"])
            self.assertNotIn(lease["lease_token"], json.dumps(page))
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, sorted(ids))
        self.assertEqual([item["id"] for item in self.store.list_work(state="leased")["items"]], ["root"])
        with self.assertRaises(ValueError):
            self.store.list_work(limit=True)
        with self.assertRaises(ValueError):
            self.store.list_work(state="verified")

    def test_shared_context_summarizes_large_goals_but_read_preserves_them(self):
        goal = "A detailed research direction. " * 1000
        child = self.op("create_work", {"goal": goal})["work_id"]
        context = self.store.context()
        ready = next(item for item in context["ready_work"] if item["work_id"] == child)
        recent = next(item for item in context["recent_work"] if item["id"] == child)
        self.assertTrue(ready["goal_truncated"])
        self.assertTrue(recent["goal_truncated"])
        self.assertEqual(len(ready["goal"]), 1024)
        self.assertEqual(self.store.read_work(child)["goal"], goal)


class HierarchyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.counter = 0
        self.store = self.new_store("campaign")

    def new_store(self, name, *, max_depth=2, max_children=3, **limits):
        spec = contract(**{"max_workers": 4, **limits})
        spec["delegation"] = {"max_depth": max_depth, "max_children_per_work": max_children}
        spec["brief"] = {"subject": "math, competition, or invention research", "data_only": True}
        return GenericStore.create(self.root / name, spec)

    def op(self, name, args=None, *, owner="root-session", key=None):
        self.counter += 1
        return self.store.dispatch_work(name, args or {}, owner=owner,
                                        request_key=key or f"{owner}:{self.counter}")

    def claim(self, work_id="root", owner="root-session"):
        self.store.bind_worker(owner, work_id)
        return self.op("claim_work", {"work_id": work_id}, owner=owner)

    @staticmethod
    def lease(claim):
        return {"work_id": claim["work_id"], "lease_token": claim["lease_token"]}

    def delegate(self, claim, *, owner="root-session", goal="Explore a concrete alternative", **args):
        return self.op("delegate_work", {**self.lease(claim), "goal": goal, **args}, owner=owner)["work_id"]

    def wait(self, claim, children, *, owner="root-session", mode="all_settled"):
        return self.op("wait_work", {**self.lease(claim), "depends_on": children, "join_mode": mode}, owner=owner)

    def complete(self, claim, *, owner="root-session", result=None):
        return self.op("complete_work", {**self.lease(claim), "result": result or {"status": "reported"}}, owner=owner)

    def fail(self, claim, *, owner="root-session"):
        return self.op("fail_work", {**self.lease(claim), "error": "This route did not work", "retry": False}, owner=owner)

    def test_opt_in_contract_validates_bounds_without_changing_legacy_hash_input(self):
        normalized = validate_contract(contract())
        self.assertNotIn("delegation", normalized)
        self.assertNotIn("max_parallel_model_calls", normalized["limits"])
        for delegation in ({"max_depth": 3, "max_children_per_work": 3},
                           {"max_depth": True, "max_children_per_work": 3},
                           {"max_depth": 2, "max_children_per_work": 0},
                           {"max_depth": 2, "max_children_per_work": 33},
                           {"max_depth": 2}, None):
            with self.subTest(delegation=delegation), self.assertRaises(ValueError):
                validate_contract({**contract(), "delegation": delegation})
        for slots in (0, 9, True, 1.5):
            with self.subTest(slots=slots), self.assertRaises(ValueError):
                validate_contract(contract(max_parallel_model_calls=slots))
        self.assertEqual(validate_contract(contract(max_parallel_model_calls=2))["limits"]["max_parallel_model_calls"], 2)
        zero = self.new_store("no-descendants", max_depth=0)
        zero.bind_worker("root", "root")
        claim = zero.dispatch_work("claim_work", {}, owner="root", request_key="claim")
        with self.assertRaisesRegex(RuntimeError, "depth"):
            zero.dispatch_work("delegate_work", {**self.lease(claim), "goal": "forbidden child"},
                               owner="root", request_key="delegate")

    def test_parent_depth_and_authority_are_host_derived_and_create_work_cannot_bypass(self):
        root = self.claim()
        child_id = self.delegate(root, brief={"delegation_depth": 0, "capabilities": ["not-a-grant"]})
        child = self.claim(child_id, "child-session")
        grandchild_id = self.delegate(child, owner="child-session")
        grandchild = self.claim(grandchild_id, "grandchild-session")
        self.assertEqual([root["work"]["delegation_depth"], child["work"]["delegation_depth"],
                          grandchild["work"]["delegation_depth"]], [0, 1, 2])
        self.assertEqual(child["work"]["delegation_parent_id"], "root")
        self.assertEqual(grandchild["work"]["delegation_parent_id"], child_id)
        self.assertFalse(grandchild["work"]["can_delegate"])
        with self.assertRaisesRegex(RuntimeError, "depth"):
            self.delegate(grandchild, owner="grandchild-session")
        with self.assertRaisesRegex(ValueError, "host-derived"):
            self.delegate(grandchild, owner="grandchild-session", delegation_depth=0)
        with self.assertRaisesRegex(RuntimeError, "bound to another"):
            self.delegate(root, owner="grandchild-session")
        for parent in (None, "root", child_id):
            with self.assertRaisesRegex(RuntimeError, "create_work is disabled"):
                self.op("create_work", {"goal": "Bypass", "parent_id": parent}, owner="grandchild-session")
        with self.store._db(True) as db:
            with self.assertRaisesRegex(RuntimeError, "create_work is disabled"):
                self.store._create_work(db, {"goal": "Host private-path bypass"})
        self.assertEqual(sum(self.store.context()["work_counts"].values()), 3)

    def test_bindings_are_immutable_and_required_for_every_lease_mutation(self):
        with self.assertRaisesRegex(RuntimeError, "not bound"):
            self.op("claim_work")
        root = self.claim()
        child_id = self.delegate(root)
        self.store.bind_worker("child-session", child_id)
        with self.assertRaisesRegex(RuntimeError, "immutable"):
            self.store.bind_worker("child-session", "root")
        with self.assertRaisesRegex(RuntimeError, "bound to another"):
            self.op("claim_work", {"work_id": "root"}, owner="child-session")
        for operation, extra in (("renew_work", {}), ("delegate_work", {"goal": "illegal"}),
                                 ("wait_work", {"depends_on": [child_id]}),
                                 ("complete_work", {"result": {}}), ("fail_work", {"error": "fail"})):
            with self.subTest(operation=operation), self.assertRaisesRegex(RuntimeError, "not bound"):
                self.op(operation, {**self.lease(root), **extra}, owner="unbound-session")
        with self.assertRaisesRegex(RuntimeError, "not bound"):
            self.store.reserve_effect("unbound-session", "root", root["lease_token"], "exact_check", {}, "unbound-effect")
        with self.assertRaisesRegex(RuntimeError, "not bound"):
            self.store.renew_live_leases(["unbound-session"])
        with self.assertRaisesRegex(ValueError, "Unknown work operation"):
            self.op("bind_worker", {"owner": "child-session", "work_id": "root"})

    def test_concurrent_delegation_enforces_lifetime_quota_and_idempotent_replays(self):
        root = self.claim()

        def submit(index):
            arguments = {**self.lease(root), "goal": f"Alternative {index}"}
            try:
                result = GenericStore(self.store.root).dispatch_work("delegate_work", arguments,
                            owner="root-session", request_key=f"parallel-child-{index}")
                return index, arguments, result
            except RuntimeError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(submit, range(24)))
        accepted = [item for item in outcomes if isinstance(item, tuple)]
        self.assertEqual(len(accepted), 3)
        self.assertTrue(all("Lifetime child limit" in item for item in outcomes if isinstance(item, str)))
        index, arguments, first = accepted[0]
        replay = self.op("delegate_work", arguments, key=f"parallel-child-{index}")
        self.assertEqual(replay["work_id"], first["work_id"])
        for index, _, result in accepted:
            child = self.claim(result["work_id"], f"child-{index}")
            self.fail(child, owner=f"child-{index}")
        self.store = GenericStore(self.store.root)
        self.assertEqual(self.store.read_work("root")["children_count"], 3)
        self.assertEqual(self.store.read_work("root")["remaining_child_slots"], 0)
        with self.assertRaisesRegex(RuntimeError, "Lifetime child limit"):
            self.delegate(root)
        self.wait(root, [result["work_id"] for _, _, result in accepted])
        resumed = self.claim()
        with self.assertRaisesRegex(RuntimeError, "Lifetime child limit"):
            self.delegate(resumed)

    def test_one_global_slot_runs_two_levels_and_merges_a_failed_route(self):
        spec = contract(max_workers=1, max_parallel_model_calls=1)
        spec["delegation"] = {"max_depth": 2, "max_children_per_work": 3}
        self.store = GenericStore.create(self.root / "one-slot", spec)
        root = self.claim()
        child_id = self.delegate(root)
        self.assertEqual(self.claim(child_id, "child-session")["reason"], "worker_capacity_busy")
        self.wait(root, [child_id])
        self.assertEqual(self.store.context()["work_counts"]["leased"], 0)
        child = self.claim(child_id, "child-session")
        grandchild_id = self.delegate(child, owner="child-session")
        self.assertEqual(self.claim(grandchild_id, "grandchild-session")["reason"], "worker_capacity_busy")
        self.wait(child, [grandchild_id], owner="child-session")
        grandchild = self.claim(grandchild_id, "grandchild-session")
        self.fail(grandchild, owner="grandchild-session")
        child_resumed = self.claim(child_id, "child-session")
        self.assertEqual(child_resumed["work"]["failure_count"], 0)
        self.assertEqual(self.store.read_work(grandchild_id)["state"], "failed")
        self.complete(child_resumed, owner="child-session", result={"route": "failed; report evidence"})
        root_resumed = self.claim()
        completed = self.complete(root_resumed, result={"status": "unresolved"})
        self.assertEqual(completed["result_status"], "agent_authored_not_independently_verified")
        context = self.store.context()
        self.assertEqual(context["stop_reason"], "work_graph_completed")
        self.assertEqual(context["work_counts"]["failed"], 1)
        self.assertEqual(context["budget"]["max_parallel_model_calls"], 1)

    def test_all_success_still_blocks_a_parent_after_a_terminal_failed_child(self):
        root = self.claim()
        child_id = self.delegate(root)
        self.wait(root, [child_id], mode="all_success")
        child = self.claim(child_id, "child-session")
        self.fail(child, owner="child-session")
        self.assertEqual(self.store.read_work("root")["state"], "blocked")
        self.assertEqual(self.store.context()["stop_reason"], "root_work_blocked")

    def test_terminal_root_failure_closes_new_work_but_drains_admitted_effects(self):
        root = self.claim()
        child_id = self.delegate(root)
        child = self.claim(child_id, "child-session")
        queued_grandchild = self.delegate(child, owner="child-session")
        effect = self.store.reserve_effect("child-session", child_id, child["lease_token"],
                                           "exact_check", {}, "already-admitted")
        self.fail(root)
        context = self.store.context()
        self.assertEqual(context["stop_reason"], "root_work_failed")
        self.assertTrue(context["draining"])
        self.assertEqual(self.store.read_work(queued_grandchild)["state"], "cancelled")
        self.assertEqual(self.store.read_work(child_id)["state"], "leased")
        with self.assertRaisesRegex(RuntimeError, "admission closed: root_work_failed"):
            self.delegate(child, owner="child-session")
        with self.assertRaisesRegex(RuntimeError, "admission closed: root_work_failed"):
            self.store.reserve_effect("child-session", child_id, child["lease_token"],
                                       "exact_check", {}, "forbidden-new-effect")
        completed = self.store.finish_effect(effect["id"], "child-session", child_id, child["lease_token"],
                                            result={"judgement": "unknown", "note": "admitted work drained"})
        self.assertEqual(completed["state"], "succeeded")
        self.complete(child, owner="child-session")
        self.assertEqual(self.store.context()["stop_reason"], "root_work_failed")
        self.assertFalse(self.store.context()["draining"])

    def test_hierarchy_wait_replaces_the_old_failed_batch_but_flat_wait_accumulates(self):
        for hierarchical in (True, False):
            with self.subTest(hierarchical=hierarchical):
                self.store = (self.new_store("second-hierarchy") if hierarchical
                              else GenericStore.create(self.root / "flat-batches", contract()))
                if hierarchical:
                    root = self.claim()
                    first = self.delegate(root)
                else:
                    root = self.op("claim_work", {"work_id": "root"})
                    first = self.op("create_work", {"goal": "First route"})["work_id"]
                self.wait(root, [first])
                child = (self.claim(first, "first-session") if hierarchical else
                         self.op("claim_work", {"work_id": first}, owner="first-session"))
                self.fail(child, owner="first-session")
                root = (self.claim() if hierarchical else self.op("claim_work", {"work_id": "root"}))
                second = (self.delegate(root) if hierarchical else
                          self.op("create_work", {"goal": "Replacement route"})["work_id"])
                self.wait(root, [second], mode="all_success")
                current = self.store.read_work("root")
                self.assertEqual(set(current["depends_on"]), {second} if hierarchical else {first, second})
                self.assertEqual(current["state"], "pending" if hierarchical else "blocked")
                child = (self.claim(second, "second-session") if hierarchical else
                         self.op("claim_work", {"work_id": second}, owner="second-session"))
                self.complete(child, owner="second-session")
                if hierarchical:
                    resumed = self.claim()
                    self.assertEqual(resumed["work"]["children_count"], 2)
                    self.complete(resumed)
                    self.assertEqual(self.store.context()["stop_reason"], "work_graph_completed")
                else:
                    self.assertEqual(self.store.context()["stop_reason"], "work_graph_exhausted")

    def test_cycles_include_implicit_parent_joins_before_a_parent_explicitly_waits(self):
        root = self.claim()
        first = self.delegate(root, goal="A")
        second = self.delegate(root, goal="B")
        a = self.claim(first, "a-session")
        self.wait(a, [second], owner="a-session")
        b = self.claim(second, "b-session")
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.delegate(b, owner="b-session", depends_on=[first])
        self.assertEqual(self.store.read_work(second)["children_count"], 0)
        grandchild_id = self.delegate(b, owner="b-session")
        grandchild = self.claim(grandchild_id, "grandchild-session")
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.wait(grandchild, [first], owner="grandchild-session")
        self.assertEqual(self.store.read_work(grandchild_id)["state"], "leased")
        self.assertEqual(self.store.read_work(grandchild_id)["depends_on"], [])
        with self.assertRaisesRegex(ValueError, "itself"):
            self.delegate(root, depends_on=["root"])
        self.assertEqual(self.store.read_work("root")["children_count"], 2)

    def test_completion_checks_live_grandchildren_even_if_the_direct_child_failed(self):
        root = self.claim()
        child_id = self.delegate(root)
        child = self.claim(child_id, "child-session")
        grandchild_id = self.delegate(child, owner="child-session")
        self.fail(child, owner="child-session")
        self.assertEqual(self.store.read_work(child_id)["state"], "failed")
        with self.assertRaisesRegex(RuntimeError, "descendants are unfinished"):
            self.complete(root)
        grandchild = self.claim(grandchild_id, "grandchild-session")
        with self.assertRaisesRegex(RuntimeError, "descendants are unfinished"):
            self.complete(root)
        self.complete(grandchild, owner="grandchild-session")
        self.complete(root)
        self.assertEqual(self.store.context()["stop_reason"], "work_graph_completed")

    def test_restart_keeps_hierarchy_bindings_and_seals_brief_as_data(self):
        root = self.claim()
        brief = {"question": "Check a construction", "nested": {"depth": 0}, "tools": ["not-permission"]}
        child_id = self.delegate(root, brief=brief, skill_names=["evidence-review"])
        before = (self.store.root / "task.json").read_bytes()
        with patch("infinity_research.kernel.time.time", return_value=root["lease_expires"] + 1):
            self.store = GenericStore(self.store.root)
            self.store.bind_worker("replacement-root", "root")
            resumed = self.op("claim_work", {}, owner="replacement-root")
            self.assertEqual(resumed["work"]["children_count"], 1)
            self.assertEqual(resumed["work"]["delegation_depth"], 0)
            with self.assertRaisesRegex(RuntimeError, "lease lost"):
                self.op("renew_work", self.lease(root))
            with self.assertRaisesRegex(RuntimeError, "immutable"):
                self.store.bind_worker("root-session", child_id)
            child = self.claim(child_id, "child-session")
            self.assertEqual(child["work"]["brief"], brief)
            self.assertEqual(child["work"]["delegation_depth"], 1)
        self.assertEqual((self.store.root / "task.json").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "JSON object"):
            self.delegate(child, owner="child-session", brief="not a data object")
        with self.assertRaisesRegex(ValueError, "32768"):
            self.delegate(child, owner="child-session", brief={"text": "x" * 32768})

    def test_legacy_database_migration_preserves_contract_hash_and_lineage_semantics(self):
        self.store = GenericStore.create(self.root / "legacy", contract())
        root = self.op("claim_work")
        child = self.op("create_work", {"goal": "Legacy child", "parent_id": "root"})["work_id"]
        self.wait(root, [child], mode="all_success")
        task_bytes = (self.store.root / "task.json").read_bytes()
        with sqlite3.connect(self.store.path) as db:
            task_hash = db.execute("SELECT task_hash FROM campaign").fetchone()[0]
            for table in ("worker_bindings", "delegations", "work_policies"):
                db.execute(f"DROP TABLE {table}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            migrated = list(pool.map(lambda _: GenericStore(self.store.root), range(8)))
        self.store = migrated[0]
        self.assertEqual((self.store.root / "task.json").read_bytes(), task_bytes)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute("SELECT task_hash FROM campaign").fetchone()[0], task_hash)
        self.assertNotIn("delegation", self.store.context())
        self.assertNotIn("max_parallel_model_calls", self.store.spec["limits"])
        self.assertNotIn("delegation_depth", self.store.read_work(child))
        self.assertEqual(self.store.read_work(child)["parent_id"], "root")
        self.assertEqual(self.store.read_work("root")["join_mode"], "all_success")
        child_claim = self.op("claim_work", {"work_id": child}, owner="any-unbound-flat-worker")
        self.complete(child_claim, owner="any-unbound-flat-worker")
        self.assertEqual(self.store.context()["ready_work_count"], 1)


if __name__ == "__main__":
    unittest.main()
