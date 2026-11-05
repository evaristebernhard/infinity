"""Transactional research ledger; budgets and promotions are host decisions."""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid

from .spec import canonical, digest, source_files, validate


class ResearchStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "research.sqlite3"
        if not self.path.is_file():
            raise ValueError(f"Campaign is not initialized: {self.root}")
        self.spec = json.loads((self.root / "task.json").read_text())
        validate(self.spec)
        with self._db() as db:
            expected = db.execute("SELECT task_hash FROM campaign WHERE id=1").fetchone()[0]
        if digest(self.spec) != expected:
            raise ValueError("Task contract changed; create a new campaign instead of mixing evaluations")

    @classmethod
    def create(cls, root: Path, spec: dict) -> "ResearchStore":
        validate(spec)
        root = root.resolve()
        if root == Path(spec["data"]["prepared_root"]) or root.is_relative_to(Path(spec["data"]["prepared_root"])):
            raise ValueError("Campaign output must be outside the sealed dataset")
        if root.exists() and any(root.iterdir()):
            raise ValueError(f"Campaign directory must be new or empty: {root}")
        root.mkdir(parents=True, exist_ok=True)
        (root / "candidates").mkdir()
        (root / "task.json").write_text(canonical(spec) + "\n")
        db = sqlite3.connect(root / "research.sqlite3")
        try:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE campaign (
                    id INTEGER PRIMARY KEY CHECK(id=1), task_hash TEXT NOT NULL,
                    created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'researching',
                    reason TEXT, best_id TEXT, final_status TEXT, final_json TEXT,
                    final_owner TEXT, final_started REAL, final_deadline REAL
                );
                CREATE TABLE candidates (
                    id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                    idempotency_key TEXT UNIQUE NOT NULL, parent_id TEXT,
                    hypothesis TEXT NOT NULL, family TEXT NOT NULL, files TEXT NOT NULL,
                    seed INTEGER NOT NULL, timeout REAL NOT NULL, state TEXT NOT NULL,
                    created REAL NOT NULL, started REAL, finished REAL, elapsed REAL NOT NULL DEFAULT 0,
                    owner TEXT, slot INTEGER, deadline REAL, heartbeat REAL,
                    score REAL, result TEXT, error TEXT, improved INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX candidate_state ON candidates(state, created);
                CREATE TABLE experiment_requests (
                    idempotency_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, candidate_id TEXT NOT NULL
                );
                CREATE TABLE calls (
                    request_id TEXT PRIMARY KEY, created REAL NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER
                );
                CREATE TABLE findings (
                    id INTEGER PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, text TEXT NOT NULL, evidence_ids TEXT NOT NULL,
                    created REAL NOT NULL
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL
                );
            """)
            db.execute("INSERT INTO campaign(id,task_hash,created) VALUES(1,?,?)", (digest(spec), time.time()))
            db.commit()
        finally:
            db.close()
        return cls(root)

    @contextmanager
    def _db(self, write: bool = False):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        if write:
            db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, kind, payload):
        db.execute("INSERT INTO events(created,kind,payload) VALUES(?,?,?)",
                   (time.time(), kind, canonical(payload)))

    def _recover(self, db):
        now = time.time()
        # A lost worker keeps its resource reservation until its hard execution deadline.
        # Its late result cannot pass the state + owner fencing check in finish().
        expired = db.execute("SELECT * FROM candidates WHERE state='running' AND deadline<?", (now,)).fetchall()
        for row in expired:
            db.execute("UPDATE candidates SET state='interrupted',finished=?,elapsed=timeout,error=? WHERE id=?",
                       (now, "Worker lost past its hard deadline; full reserved time charged", row["id"]))
            self._event(db, "experiment_interrupted", {"candidate_id": row["id"]})
        campaign = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
        if campaign["status"] != "researching" or now >= self._search_deadline(campaign):
            db.execute("UPDATE candidates SET state='cancelled',finished=?,error=? WHERE state='queued'",
                       (now, "Search stopped before this experiment could start"))
        if campaign["final_status"] == "running" and campaign["final_deadline"] < now:
            db.execute("UPDATE campaign SET final_status='interrupted',reason=? WHERE id=1",
                       ("Final evaluation interrupted; automatic rerun is disabled",))

    def _totals(self, db):
        rows = db.execute("""SELECT COUNT(*) AS count,
            COALESCE(SUM(elapsed),0) AS spent,
            COALESCE(SUM(CASE WHEN state IN ('queued','running') THEN timeout ELSE 0 END),0) AS reserved,
            COALESCE(SUM(CASE WHEN state IN ('queued','running') THEN 1 ELSE 0 END),0) AS active,
            COALESCE(SUM(CASE WHEN state='running' THEN 1 ELSE 0 END),0) AS running
            FROM candidates""").fetchone()
        return dict(rows)

    def _stop_reason(self, db, *, for_experiment=False):
        campaign = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
        b = self.spec["budget"]
        if campaign["status"] != "researching":
            return campaign["reason"] or campaign["status"]
        if time.time() >= self._search_deadline(campaign):
            return "wall_time_budget_exhausted"
        totals = self._totals(db)
        if totals["count"] >= b["max_experiments"]:
            return "experiment_budget_exhausted"
        # Reservations are temporary: fast trials release unused seconds. Do not permanently
        # stop the LLM workers merely because the current queue fills the available capacity.
        if totals["spent"] >= b["max_total_trial_seconds"] - b["final_trial_seconds"]:
            return "trial_time_budget_exhausted"
        if not for_experiment and db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] >= b["max_model_calls"]:
            return "model_call_budget_exhausted"
        return None

    def _search_deadline(self, campaign):
        return (campaign["created"] + self.spec["budget"]["max_wall_seconds"]
                - self.spec["budget"]["final_trial_seconds"] - 30)

    def reserve_model_call(self, request_id: str) -> dict:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 300:
            raise ValueError("Invalid request_id")
        with self._db(True) as db:
            self._recover(db)
            # Call IDs are one-use; reusing a previous reservation must not buy another invocation.
            if db.execute("SELECT 1 FROM calls WHERE request_id=?", (request_id,)).fetchone():
                return {"allowed": False, "reason": "request_id_already_reserved"}
            reason = self._stop_reason(db)
            if reason:
                return {"allowed": False, "reason": reason}
            db.execute("INSERT INTO calls(request_id,created) VALUES(?,?)", (request_id, time.time()))
            index = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            campaign = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
            return {"allowed": True, "call_index": index,
                    "max_output_tokens": self.spec["budget"]["max_output_tokens"],
                    "search_wall_seconds_remaining": max(0, self._search_deadline(campaign) - time.time())}

    def record_usage(self, request_id: str, input_tokens: int, output_tokens: int) -> dict:
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (input_tokens, output_tokens)):
            raise ValueError("Token usage must be nonnegative integers")
        with self._db(True) as db:
            row = db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown model call reservation")
            if row["input_tokens"] is not None:
                if (row["input_tokens"], row["output_tokens"]) != (input_tokens, output_tokens):
                    raise ValueError("Conflicting usage for a previously recorded model call")
                return {"recorded": True, "duplicate": True}
            db.execute("UPDATE calls SET input_tokens=?,output_tokens=? WHERE request_id=?",
                       (input_tokens, output_tokens, request_id))
        return {"recorded": True}

    def propose(self, args: dict) -> dict:
        files = source_files(args.get("files"))
        key = args.get("idempotency_key")
        hypothesis, family = args.get("hypothesis"), args.get("family")
        if not isinstance(key, str) or not key or len(key) > 500:
            raise ValueError("A bounded idempotency_key is required")
        if not isinstance(hypothesis, str) or not hypothesis.strip() or len(hypothesis) > 8000:
            raise ValueError("hypothesis must contain 1..8000 characters")
        if family not in self.spec["search"]["families"]:
            raise ValueError("family must be one of the configured research families")
        seed = args.get("seed", 42)
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
            raise ValueError("seed must be a uint32 integer")
        timeout = args.get("timeout_seconds", self.spec["budget"]["max_trial_seconds"])
        if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
                or not math.isfinite(timeout) or not 0 < timeout <= self.spec["budget"]["max_trial_seconds"]):
            raise ValueError("timeout_seconds exceeds the task limit or is invalid")
        fingerprint = digest({"files": files, "seed": seed, "timeout": timeout})
        with self._db(True) as db:
            self._recover(db)
            previous = db.execute("""SELECT candidates.* FROM experiment_requests
                JOIN candidates ON candidates.id=experiment_requests.candidate_id
                WHERE experiment_requests.idempotency_key=?""", (key,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("Idempotency key reused with different candidate content")
                return {"candidate_id": previous["id"], "duplicate": True}
            previous = db.execute("SELECT * FROM candidates WHERE fingerprint=?", (fingerprint,)).fetchone()
            if previous:
                db.execute("INSERT INTO experiment_requests VALUES(?,?,?)", (key, fingerprint, previous["id"]))
                return {"candidate_id": previous["id"], "duplicate": True}
            reason = self._stop_reason(db, for_experiment=True)
            if reason:
                raise ValueError(f"New experiment rejected: {reason}")
            parent = args.get("parent_id")
            if parent is not None and not db.execute("SELECT 1 FROM candidates WHERE id=?", (parent,)).fetchone():
                raise ValueError("Unknown parent_id")
            totals = self._totals(db)
            limit = self.spec["budget"]["max_total_trial_seconds"] - self.spec["budget"]["final_trial_seconds"]
            if totals["spent"] + totals["reserved"] + timeout > limit:
                raise ValueError("Insufficient remaining trial seconds; reduce timeout_seconds")
            if totals["active"] >= self.spec["budget"]["max_workers"]:
                raise ValueError("Outstanding experiment queue is full")
            candidate_id = uuid.uuid4().hex
            db.execute("""INSERT INTO candidates(id,fingerprint,idempotency_key,parent_id,hypothesis,
                family,files,seed,timeout,state,created) VALUES(?,?,?,?,?,?,?,?,?,'queued',?)""",
                       (candidate_id, fingerprint, key, parent, hypothesis, family,
                        canonical(files), seed, timeout, time.time()))
            db.execute("INSERT INTO experiment_requests VALUES(?,?,?)", (key, fingerprint, candidate_id))
            self._event(db, "experiment_proposed", {"candidate_id": candidate_id, "family": family,
                                                    "parent_id": parent, "fingerprint": fingerprint})
        return {"candidate_id": candidate_id, "duplicate": False}

    def claim(self, candidate_id: str | None, owner: str) -> dict | None:
        with self._db(True) as db:
            self._recover(db)
            if candidate_id is None:
                row = db.execute("SELECT * FROM candidates WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
                if row is None:
                    return None
                candidate_id = row["id"]
            else:
                row = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown candidate")
            if row["state"] != "queued":
                return None
            campaign = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
            remaining = self._search_deadline(campaign) - time.time()
            if campaign["status"] != "researching" or remaining <= 0:
                db.execute("UPDATE candidates SET state='cancelled',finished=?,error=? WHERE id=?",
                           (time.time(), "Campaign stopped before this experiment started", candidate_id))
                return None
            active = {r[0] for r in db.execute("SELECT slot FROM candidates WHERE state='running'")}
            available = [i for i in range(self.spec["budget"]["max_parallel_experiments"]) if i not in active]
            if not available:
                return None
            started = time.time()
            timeout = min(row["timeout"], remaining)
            db.execute("UPDATE candidates SET state='running',owner=?,slot=?,started=?,heartbeat=?,deadline=? WHERE id=?",
                       (owner, available[0], started, started, started + timeout + 10, candidate_id))
            self._event(db, "experiment_started", {"candidate_id": candidate_id, "slot": available[0]})
            result = dict(row)
            result.update(state="running", owner=owner, slot=available[0], started=started,
                          effective_timeout=timeout)
            return result

    def heartbeat(self, candidate_id: str, owner: str):
        with self._db(True) as db:
            cursor = db.execute("UPDATE candidates SET heartbeat=? WHERE id=? AND owner=? AND state='running'",
                                (time.time(), candidate_id, owner))
            if cursor.rowcount != 1:
                raise RuntimeError("Experiment lease no longer belongs to this worker")

    def finish(self, candidate_id: str, owner: str, *, result: dict | None,
               elapsed: float, error: str | None = None) -> dict:
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("Elapsed time must be finite and nonnegative")
        if result is not None:
            score = result.get("score")
            if (result.get("metric") != self.spec["objective"]["name"]
                    or isinstance(score, bool) or not isinstance(score, (int, float))
                    or not math.isfinite(score)):
                raise ValueError("Invalid evaluator metric or non-finite score")
        with self._db(True) as db:
            self._recover(db)
            row = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if row is None or row["owner"] != owner or row["state"] != "running":
                # Preserve any expired-lease recovery even though this caller's result is rejected.
                db.commit()
                raise RuntimeError("Rejected late, duplicate, or foreign experiment completion")
            score = result["score"] if result is not None else None
            state = "succeeded" if result is not None else "failed"
            best = db.execute("SELECT score FROM candidates WHERE id=(SELECT best_id FROM campaign WHERE id=1)").fetchone()
            improve = result is not None and (best is None or self._better(score, best["score"]))
            progress = improve and (best is None or abs(score - best["score"]) > self.spec["search"]["min_delta"])
            db.execute("""UPDATE candidates SET state=?,finished=?,elapsed=?,score=?,result=?,error=?,improved=?
                WHERE id=?""", (state, time.time(), elapsed, score,
                                canonical(result) if result is not None else None, error, int(progress), candidate_id))
            if improve:
                db.execute("UPDATE campaign SET best_id=? WHERE id=1", (candidate_id,))
            self._event(db, "experiment_completed", {"candidate_id": candidate_id, "state": state,
                                                     "score": score, "promoted": bool(improve), "error": error})
        return self.candidate(candidate_id)

    def _better(self, candidate: float, best: float) -> bool:
        return candidate > best if self.spec["objective"]["direction"] == "maximize" else candidate < best

    def candidate(self, candidate_id: str, *, include_files: bool = False) -> dict:
        with self._db() as db:
            row = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown candidate_id")
        result = dict(row)
        files = result.pop("files")
        for key in ("owner", "idempotency_key"):
            result.pop(key, None)
        if include_files:
            result["files"] = json.loads(files)
        if result["result"]:
            result["result"] = json.loads(result["result"])
        return result

    def finding(self, args: dict) -> dict:
        kind, text, key = args.get("kind"), args.get("text"), args.get("idempotency_key")
        if kind not in ("hypothesis", "observation", "failure", "method"):
            raise ValueError("Unknown finding kind")
        if not isinstance(text, str) or not 0 < len(text) <= 12000 or not isinstance(key, str) or not 0 < len(key) <= 500:
            raise ValueError("Finding text or idempotency_key is invalid")
        evidence = args.get("evidence_ids", [])
        if not isinstance(evidence, list) or len(evidence) > 30 or any(not isinstance(x, str) for x in evidence):
            raise ValueError("evidence_ids must be a list of candidate IDs")
        with self._db(True) as db:
            for cid in evidence:
                if not db.execute("SELECT 1 FROM candidates WHERE id=? AND state IN ('succeeded','failed','interrupted')", (cid,)).fetchone():
                    raise ValueError(f"Evidence must refer to a completed experiment: {cid}")
            previous = db.execute("SELECT * FROM findings WHERE idempotency_key=?", (key,)).fetchone()
            if previous:
                if (previous["kind"], previous["text"], previous["evidence_ids"]) != (kind, text, canonical(evidence)):
                    raise ValueError("Finding idempotency key reused with different content")
                return {"finding_id": previous["id"], "duplicate": True}
            cur = db.execute("INSERT INTO findings(idempotency_key,kind,text,evidence_ids,created) VALUES(?,?,?,?,?)",
                             (key, kind, text, canonical(evidence), time.time()))
            self._event(db, "finding_recorded", {"finding_id": cur.lastrowid, "kind": kind})
            return {"finding_id": cur.lastrowid, "status": "agent_authored_not_independently_verified"}

    def context(self) -> dict:
        with self._db(True) as db:
            self._recover(db)
            campaign = dict(db.execute("SELECT * FROM campaign WHERE id=1").fetchone())
            totals = self._totals(db)
            reason = self._stop_reason(db)
            calls = db.execute("""SELECT COUNT(*) n,COALESCE(SUM(input_tokens),0) i,
                COALESCE(SUM(output_tokens),0) o,
                SUM(CASE WHEN input_tokens IS NULL THEN 1 ELSE 0 END) unknown FROM calls""").fetchone()
            recent = db.execute("SELECT id,hypothesis,family,state,score,error,improved FROM candidates ORDER BY created DESC LIMIT 12").fetchall()
            order = "DESC" if self.spec["objective"]["direction"] == "maximize" else "ASC"
            leaders = db.execute(f"SELECT id,hypothesis,family,score,seed FROM candidates WHERE state='succeeded' ORDER BY score {order} LIMIT 5").fetchall()
            families = {name: {"attempts": 0, "active": 0, "improvements": 0} for name in self.spec["search"]["families"]}
            for row in db.execute("SELECT family,COUNT(*) n,SUM(CASE WHEN state IN ('queued','running') THEN 1 ELSE 0 END) a,SUM(improved) i FROM candidates GROUP BY family"):
                families[row["family"]] = {"attempts": row["n"], "active": row["a"], "improvements": row["i"]}
            findings = [dict(row) for row in db.execute("SELECT id,kind,text,evidence_ids FROM findings ORDER BY id DESC LIMIT 20")]
            progress_rows = db.execute("""SELECT improved FROM candidates
                WHERE state IN ('succeeded','failed','interrupted') ORDER BY finished DESC
                LIMIT ?""", (self.spec["search"]["stagnation_trials"],)).fetchall()
        for item in findings:
            item["evidence_ids"] = json.loads(item["evidence_ids"])
            item["status"] = "agent_authored_not_independently_verified"
        # Untried/less-used families remain available; successful ones receive additional priority.
        total = max(1, totals["count"])
        priorities = {name: ((v["improvements"] + 1) / (v["attempts"] + 1)
                            + math.sqrt(2 * math.log(total + 1) / (v["attempts"] + 1))
                            - v["active"]) for name, v in families.items()}
        stalled = 0
        for row in progress_rows:
            if row["improved"]:
                break
            stalled += 1
        return {
            "stop": reason is not None, "stop_reason": reason,
            "draining": reason is not None and totals["active"] > 0,
            "active_experiments": totals["active"], "running_experiments": totals["running"],
            "wall_seconds_remaining": max(0, campaign["created"] + self.spec["budget"]["max_wall_seconds"] - time.time()),
            "search_wall_seconds_remaining": max(0, self._search_deadline(campaign) - time.time()),
            "max_workers": self.spec["budget"]["max_workers"],
            "max_output_tokens": self.spec["budget"]["max_output_tokens"],
            "task": {k: self.spec[k] for k in ("task_id", "domain", "description", "objective", "search") if k in self.spec},
            "task_sha256": digest(self.spec), "data_sha256": self.spec["data_sha256"],
            "evaluator_sha256": self.spec["evaluator_sha256"],
            "budget": {**self.spec["budget"], "experiments_used": totals["count"],
                       "trial_seconds_used": totals["spent"], "trial_seconds_reserved": totals["reserved"],
                       "trial_seconds_available": max(0, self.spec["budget"]["max_total_trial_seconds"]
                           - self.spec["budget"]["final_trial_seconds"] - totals["spent"] - totals["reserved"]),
                       "model_calls_used": calls["n"], "observed_input_tokens": calls["i"],
                       "observed_output_tokens": calls["o"], "calls_without_usage": calls["unknown"] or 0},
            "best_candidate_id": campaign["best_id"], "leaderboard": [dict(r) for r in leaders],
            "recent_experiments": [dict(r) for r in recent], "findings": findings,
            "families": families, "suggested_family": max(priorities, key=priorities.get),
            "replan_recommended": stalled >= self.spec["search"]["stagnation_trials"],
            "baseline_files": self.spec["baseline_files"],
            "execution_contract": {
                "entrypoint": "python /candidate/main.py /input/manifest.json /output",
                "input": "Frozen training labels and unlabelled validation images/data in /input",
                "output": "/output/predictions.json; follow baseline_files for the domain schema",
                "seed_environment": "RESEARCH_SEED",
                "network": "disabled in Docker",
                "resources": {k: v for k, v in self.spec["executor"].items() if k != "allow_local"},
                "timeout": "Whole trial including setup and host evaluation; execution reserves time for evaluation",
                "diagnostics": "read_candidate returns the bounded execution log; these diagnostics are unverified",
            },
            "final_status": campaign["final_status"],
        }

    def stop(self, reason="stopped_by_owner"):
        with self._db(True) as db:
            db.execute("UPDATE campaign SET status='stopped',reason=? WHERE id=1", (reason,))
            db.execute("UPDATE candidates SET state='cancelled',finished=?,error=? WHERE state='queued'",
                       (time.time(), reason))
            self._event(db, "campaign_stopped", {"reason": reason})

    def begin_final(self) -> dict:
        with self._db(True) as db:
            self._recover(db)
            campaign = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
            if campaign["final_status"] is not None:
                raise ValueError("Final evaluation was already started; querying holdout repeatedly is disabled")
            if self._totals(db)["active"]:
                raise ValueError("Wait for active experiments to finish (or stop and drain the campaign)")
            if campaign["best_id"] is None:
                raise ValueError("There is no independently evaluated candidate to finalize")
            owner = uuid.uuid4().hex
            now = time.time()
            timeout = self.spec["budget"]["final_trial_seconds"]
            if self._totals(db)["spent"] + timeout > self.spec["budget"]["max_total_trial_seconds"]:
                raise ValueError("Not enough total trial budget remains for final evaluation")
            if now + timeout > campaign["created"] + self.spec["budget"]["max_wall_seconds"]:
                raise ValueError("Not enough wall-clock budget remains for final evaluation")
            db.execute("""UPDATE campaign SET status='finalizing',reason='final_evaluation_started',
                final_status='running',final_owner=?,final_started=?,final_deadline=? WHERE id=1""",
                       (owner, now, now + timeout + 10))
            self._event(db, "final_started", {"candidate_id": campaign["best_id"]})
            return {"candidate_id": campaign["best_id"], "owner": owner, "timeout": timeout}

    def finish_final(self, owner: str, result: dict | None, error: str | None = None):
        with self._db(True) as db:
            self._recover(db)
            row = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
            if row["final_owner"] != owner or row["final_status"] != "running":
                db.commit()
                raise RuntimeError("Final evaluation lease lost")
            state = "completed" if result else "failed"
            db.execute("UPDATE campaign SET status='completed',final_status=?,final_json=?,reason=? WHERE id=1",
                       (state, canonical(result) if result else None, error or "final_evaluation_completed"))
            self._event(db, "final_completed", {"state": state, "error": error})

    def report(self) -> dict:
        context = self.context()
        with self._db() as db:
            row = db.execute("SELECT final_json,reason FROM campaign WHERE id=1").fetchone()
            context["final_evaluation"] = json.loads(row["final_json"]) if row["final_json"] else None
            context["campaign_reason"] = row["reason"]
            context["events"] = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id")]
        context.pop("baseline_files", None)
        return context

    def unfinished(self) -> list[str]:
        with self._db(True) as db:
            self._recover(db)
            return [row[0] for row in db.execute(
                "SELECT id FROM candidates WHERE state IN ('queued','running') ORDER BY created,id")]
