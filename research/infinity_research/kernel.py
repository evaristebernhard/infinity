"""Domain-neutral, durable work graph for schema-2 research campaigns.

The graph records what agents did, not whether their conclusions are true.
Only registered host tools produce effect records; an effect's *meaning* is
defined by that tool. A completed work item is never itself a proof verdict.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
import uuid

from .spec import canonical, digest


TERMINAL_STATES = {"succeeded", "failed", "blocked", "cancelled"}
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}\Z")
_MUTATIONS = {"create_work", "delegate_work", "claim_work", "renew_work", "wait_work",
              "complete_work", "fail_work"}
_READY_FROM = """ FROM work w LEFT JOIN work_policies policy ON policy.work_id=w.id
    WHERE w.state='pending' AND NOT EXISTS (
        SELECT 1 FROM dependencies d JOIN work p ON p.id=d.depends_on
        WHERE d.work_id=w.id AND CASE COALESCE(policy.join_mode,'all_success')
            WHEN 'all_settled' THEN p.state NOT IN ('succeeded','failed','blocked','cancelled')
            ELSE p.state!='succeeded' END)"""


def _text(value: Any, name: str, maximum: int = 12000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a nonempty string of at most {maximum} characters")
    return value


def _names(value: Any, name: str, maximum: int = 128) -> list[str]:
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(item, str) or not _NAME.fullmatch(item) for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{name} must contain distinct bounded names")
    return list(value)


def _object(value: Any, name: str, maximum_bytes: int = 65536) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    encoded = canonical(value)
    if len(encoded.encode()) > maximum_bytes:
        raise ValueError(f"{name} exceeds {maximum_bytes} bytes")
    return json.loads(encoded)


def validate_contract(spec: dict) -> dict:
    """Return a normalized copy; this deliberately has no dataset/metric fields."""
    normalized = _object(spec, "contract", 1024 * 1024)
    if normalized.get("schema_version") != 2:
        raise ValueError("Generic campaigns require schema_version 2")
    _text(normalized.get("task_id"), "task_id", 200)
    _text(normalized.get("goal"), "goal", 32000)
    normalized["capabilities"] = _names(normalized.get("capabilities", []), "capabilities")
    limits = normalized.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    ceilings = {"max_workers": 1000, "max_model_calls": 1_000_000_000,
                "max_output_tokens": 10_000_000, "max_work_items": 100_000,
                "max_failures": 1000}
    for key, ceiling in ceilings.items():
        value = limits.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            raise ValueError(f"limits.{key} must be an integer in 1..{ceiling}")
    limits.setdefault("max_parallel_tools", min(limits["max_workers"], 4))
    parallel = limits["max_parallel_tools"]
    if isinstance(parallel, bool) or not isinstance(parallel, int) or not 1 <= parallel <= 10000:
        raise ValueError("limits.max_parallel_tools must be an integer in 1..10000")
    for key in ("max_wall_seconds", "lease_seconds"):
        value = limits.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 < value <= 366 * 86400):
            raise ValueError(f"limits.{key} must be finite, positive and at most 366 days")
    if limits["lease_seconds"] > limits["max_wall_seconds"]:
        raise ValueError("lease_seconds cannot exceed max_wall_seconds")
    if "max_parallel_model_calls" in limits:
        parallel_calls = limits["max_parallel_model_calls"]
        if (isinstance(parallel_calls, bool) or not isinstance(parallel_calls, int)
                or not 1 <= parallel_calls <= limits["max_workers"]):
            raise ValueError("limits.max_parallel_model_calls must be an integer in 1..max_workers")
    if "delegation" in normalized:
        delegation = normalized["delegation"]
        if not isinstance(delegation, dict) or set(delegation) != {"max_depth", "max_children_per_work"}:
            raise ValueError("delegation requires max_depth and max_children_per_work")
        for key, lower, upper in (("max_depth", 0, 2), ("max_children_per_work", 1, 32)):
            value = delegation[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"delegation.{key} must be an integer in {lower}..{upper}")
        if "brief" in normalized:
            normalized["brief"] = _object(normalized["brief"], "root brief", 32 * 1024)
    return normalized


class GenericStore:
    """SQLite work/effect ledger. Each mutating operation is one short transaction."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "kernel.sqlite3"
        if not self.path.is_file():
            raise ValueError(f"Generic campaign is not initialized: {self.root}")
        self.spec = validate_contract(json.loads((self.root / "task.json").read_text()))
        with self._db() as db:
            row = db.execute("SELECT task_hash FROM campaign WHERE id=1").fetchone()
        if row is None or row["task_hash"] != digest(self.spec):
            raise ValueError("Task contract changed; create a new campaign")
        # Additive metadata tables do not rewrite a sealed contract or reinterpret
        # legacy parent_id provenance. Missing policies retain all-success joins.
        with self._db(True) as db:
            self._install_auxiliary_tables(db)
            if "delegation" in self.spec:
                root = db.execute("SELECT parent_work_id,depth FROM delegations WHERE work_id='root'").fetchone()
                missing = db.execute("""SELECT 1 FROM work w LEFT JOIN delegations d ON d.work_id=w.id
                    WHERE d.work_id IS NULL LIMIT 1""").fetchone()
                if root is None or root["parent_work_id"] is not None or root["depth"] != 0 or missing:
                    raise ValueError("Hierarchy metadata is missing; initialize a new campaign rather than converting provenance")

    @staticmethod
    def _install_auxiliary_tables(db):
        # execute(), rather than executescript(), keeps this migration inside
        # the caller's transaction even when multiple RPC processes open it.
        statements = [
            """CREATE TABLE IF NOT EXISTS work_policies (
                work_id TEXT PRIMARY KEY REFERENCES work(id),
                join_mode TEXT NOT NULL CHECK(join_mode IN ('all_success','all_settled')))""",
            """CREATE TABLE IF NOT EXISTS delegations (
                work_id TEXT PRIMARY KEY REFERENCES work(id), parent_work_id TEXT REFERENCES work(id),
                depth INTEGER NOT NULL CHECK(depth BETWEEN 0 AND 2), brief TEXT NOT NULL,
                CHECK((parent_work_id IS NULL AND depth=0) OR (parent_work_id IS NOT NULL AND depth>0)))""",
            "CREATE INDEX IF NOT EXISTS delegation_parent ON delegations(parent_work_id,work_id)",
            """CREATE TABLE IF NOT EXISTS worker_bindings (
                owner TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES work(id), created REAL NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS worker_binding_work ON worker_bindings(work_id,owner)",
        ]
        for statement in statements:
            db.execute(statement)

    @classmethod
    def create(cls, root: Path, spec: dict) -> "GenericStore":
        spec = validate_contract(spec)
        root = root.resolve()
        if root.exists():
            entries = list(root.iterdir())
            if any(entry.name != "skills" or not entry.is_dir() or entry.is_symlink()
                   for entry in entries):
                raise ValueError("Campaign must be new or empty, except for a prepared skills snapshot")
        root.mkdir(parents=True, exist_ok=True)
        path = root / "kernel.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE campaign (
                    id INTEGER PRIMARY KEY CHECK(id=1), task_hash TEXT NOT NULL,
                    created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active', reason TEXT
                );
                CREATE TABLE work (
                    id TEXT PRIMARY KEY, goal TEXT NOT NULL, parent_id TEXT REFERENCES work(id),
                    skill_names TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT, lease_token TEXT, lease_expires REAL,
                    result TEXT, effect_ids TEXT NOT NULL DEFAULT '[]', error TEXT
                );
                CREATE INDEX work_state ON work(state,created,id);
                CREATE TABLE dependencies (
                    work_id TEXT NOT NULL REFERENCES work(id),
                    depends_on TEXT NOT NULL REFERENCES work(id),
                    PRIMARY KEY(work_id,depends_on), CHECK(work_id != depends_on)
                );
                CREATE INDEX dependency_reverse ON dependencies(depends_on,work_id);
                CREATE TABLE requests (
                    request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL
                );
                CREATE TABLE effects (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    fingerprint TEXT NOT NULL, owner TEXT NOT NULL,
                    work_id TEXT NOT NULL REFERENCES work(id), lease_token TEXT NOT NULL,
                    attempt INTEGER NOT NULL, capability TEXT NOT NULL, arguments TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, finished REAL,
                    result TEXT, error TEXT
                );
                CREATE INDEX effect_state ON effects(state,work_id);
                CREATE TABLE calls (
                    request_id TEXT PRIMARY KEY, created REAL NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL
                );
            """)
            now = time.time()
            db.execute("INSERT INTO campaign(id,task_hash,created) VALUES(1,?,?)", (digest(spec), now))
            db.execute("INSERT INTO work(id,goal,skill_names,state,created,updated) VALUES('root',?,'[]','pending',?,?)",
                       (spec["goal"], now, now))
            cls._install_auxiliary_tables(db)
            if "delegation" in spec:
                db.execute("INSERT INTO delegations(work_id,parent_work_id,depth,brief) VALUES('root',NULL,0,?)",
                           (canonical(spec.get("brief", {})),))
            db.commit()
            (root / "task.json").write_text(canonical(spec) + "\n")
        finally:
            db.close()
        return cls(root)

    @contextmanager
    def _db(self, write: bool = False):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
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
    def _event(db, kind: str, payload: dict):
        db.execute("INSERT INTO events(created,kind,payload) VALUES(?,?,?)",
                   (time.time(), kind, canonical(payload)))

    def _deadline(self, db) -> float:
        return (db.execute("SELECT created FROM campaign WHERE id=1").fetchone()[0]
                + self.spec["limits"]["max_wall_seconds"])

    def _refresh(self, db) -> dict:
        """Expire leases, fence their effects, then propagate failed prerequisites."""
        now = time.time()
        expired = db.execute("SELECT * FROM work WHERE state='leased' AND lease_expires<=?", (now,)).fetchall()
        interrupted = 0
        for row in expired:
            failures = row["failure_count"] + 1
            state = "failed" if failures >= self.spec["limits"]["max_failures"] else "pending"
            updated = db.execute("""UPDATE effects SET state='interrupted',finished=?,error=?
                WHERE work_id=? AND lease_token=? AND state='running'""",
                                 (now, "Lease expired; effect outcome is unknown and is not automatically replayed",
                                  row["id"], row["lease_token"]))
            interrupted += updated.rowcount
            db.execute("""UPDATE work SET state=?,failure_count=?,updated=?,error=?,
                lease_owner=NULL,lease_token=NULL,lease_expires=NULL WHERE id=?""",
                       (state, failures, now, "Lease expired before work completion", row["id"]))
            self._event(db, "lease_expired", {"work_id": row["id"], "attempt": row["attempt_count"],
                                              "state": state})
        campaign = db.execute("SELECT status FROM campaign WHERE id=1").fetchone()
        if campaign["status"] != "active" or now >= self._deadline(db):
            db.execute("UPDATE work SET state='cancelled',updated=?,error=? WHERE state='pending'",
                       (now, "Campaign stopped before this work could be claimed"))
        blocked = 0
        while True:
            updated = db.execute("""UPDATE work SET state='blocked',updated=?,error=?
                WHERE state='pending'
                AND COALESCE((SELECT join_mode FROM work_policies WHERE work_id=work.id),'all_success')='all_success'
                AND EXISTS (
                    SELECT 1 FROM dependencies d JOIN work prerequisite ON prerequisite.id=d.depends_on
                    WHERE d.work_id=work.id AND prerequisite.state IN ('failed','blocked','cancelled'))""",
                                 (now, "A required dependency did not succeed"))
            blocked += updated.rowcount
            if not updated.rowcount:
                break
        if "delegation" in self.spec:
            root_state = db.execute("SELECT state FROM work WHERE id='root'").fetchone()[0]
            active = db.execute("SELECT status FROM campaign WHERE id=1").fetchone()[0] == "active"
            if active and root_state in ("failed", "blocked"):
                # A permanently failed root has no remaining coordinator to
                # integrate new work. Close admission and cancel queued work;
                # already leased work/effects may still drain. Intermediate
                # failed branches do not stop their siblings or descendants.
                # _stop_in_transaction marks status first, so its refresh does
                # not recurse into another stop transition.
                self._stop_in_transaction(db, "root_work_" + root_state)
        return {"expired_leases": len(expired), "interrupted_effects": interrupted,
                "blocked_work": blocked, "replay_policy": "no_automatic_effect_replay"}

    def _counts(self, db) -> dict:
        counts = {name: 0 for name in ("pending", "leased", "succeeded", "failed", "blocked", "cancelled")}
        counts.update({row["state"]: row["n"] for row in db.execute("SELECT state,COUNT(*) n FROM work GROUP BY state")})
        return counts

    @staticmethod
    def _ready_rows(db, limit: int = 64):
        return db.execute("SELECT w.*" + _READY_FROM + " ORDER BY w.created,w.id LIMIT ?", (limit,)).fetchall()

    @staticmethod
    def _ready_count(db) -> int:
        return db.execute("SELECT COUNT(*)" + _READY_FROM).fetchone()[0]

    def _stop_reason(self, db, *, include_model_limit: bool = True) -> str | None:
        campaign = db.execute("SELECT status,reason FROM campaign WHERE id=1").fetchone()
        if campaign["status"] != "active":
            return campaign["reason"] or "stopped_by_owner"
        if time.time() >= self._deadline(db):
            return "wall_time_budget_exhausted"
        counts = self._counts(db)
        effects_running = db.execute("SELECT COUNT(*) FROM effects WHERE state='running'").fetchone()[0]
        if not counts["pending"] and not counts["leased"] and not effects_running:
            if "delegation" in self.spec:
                root = db.execute("SELECT state FROM work WHERE id='root'").fetchone()[0]
                return "work_graph_completed" if root == "succeeded" else "work_graph_exhausted"
            return ("work_graph_completed" if not sum(counts[name] for name in ("failed", "blocked", "cancelled"))
                    else "work_graph_exhausted")
        if (include_model_limit and db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
                >= self.spec["limits"]["max_model_calls"]):
            return "model_call_budget_exhausted"
        return None

    def _admission(self, db):
        reason = self._stop_reason(db, include_model_limit=False)
        if reason:
            raise RuntimeError(f"Campaign admission closed: {reason}")

    def _row(self, db, work_id: str):
        _text(work_id, "work_id", 100)
        row = db.execute("SELECT * FROM work WHERE id=?", (work_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown work item: {work_id}")
        return row

    def _work(self, db, row, owner: str | None = None) -> dict:
        result = dict(row)
        result["skill_names"] = json.loads(result["skill_names"])
        result["effect_ids"] = json.loads(result["effect_ids"])
        result["result"] = json.loads(result["result"]) if result["result"] is not None else None
        result["depends_on"] = [r[0] for r in db.execute("SELECT depends_on FROM dependencies WHERE work_id=? ORDER BY depends_on",
                                                       (row["id"],))]
        # A crash may happen after an effect commits but before complete_work
        # cites it. Discover every attempt's receipts without relying on the
        # previous model conversation or treating them as accepted conclusions.
        result["effect_count"] = db.execute("SELECT COUNT(*) FROM effects WHERE work_id=?", (row["id"],)).fetchone()[0]
        recent = db.execute("""SELECT id,capability,state,attempt,created,finished,
            (result IS NOT NULL) AS result_available FROM effects WHERE work_id=?
            ORDER BY created DESC,id DESC LIMIT 8""", (row["id"],)).fetchall()
        result["recent_effects"] = [self._effect_summary(effect) for effect in recent]
        policy = db.execute("SELECT join_mode FROM work_policies WHERE work_id=?", (row["id"],)).fetchone()
        result["join_mode"] = policy["join_mode"] if policy else "all_success"
        result.update(self._delegation_info(db, row["id"]))
        result["result_status"] = "agent_authored_not_independently_verified"
        if owner is None or row["lease_owner"] != owner:
            result.pop("lease_token")
        return result

    def _delegation_info(self, db, work_id: str) -> dict:
        if "delegation" not in self.spec:
            return {}
        row = db.execute("SELECT * FROM delegations WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            raise ValueError("Missing hierarchy metadata for work")
        children = db.execute("SELECT COUNT(*) FROM delegations WHERE parent_work_id=?", (work_id,)).fetchone()[0]
        remaining = max(0, self.spec["delegation"]["max_children_per_work"] - children)
        return {"delegation_parent_id": row["parent_work_id"], "delegation_depth": row["depth"],
                "brief": json.loads(row["brief"]), "children_count": children,
                "remaining_child_slots": remaining,
                "can_delegate": row["depth"] < self.spec["delegation"]["max_depth"] and remaining > 0}

    def _bound_work(self, db, owner: str, work_id: str | None = None) -> str:
        row = db.execute("SELECT work_id FROM worker_bindings WHERE owner=?", (owner,)).fetchone()
        if row is None:
            raise RuntimeError("Hierarchy worker is not bound by the host")
        if work_id is not None and row["work_id"] != work_id:
            raise RuntimeError("Hierarchy worker is bound to another work item")
        return row["work_id"]

    def bind_worker(self, owner: str, work_id: str) -> dict:
        """Host-only immutable session binding; it grants no lease or new tools."""
        _text(owner, "owner", 300)
        _text(work_id, "work_id", 100)
        if "delegation" not in self.spec:
            raise ValueError("Worker binding requires a hierarchy campaign")
        with self._db(True) as db:
            self._row(db, work_id)
            self._delegation_info(db, work_id)
            row = db.execute("SELECT work_id FROM worker_bindings WHERE owner=?", (owner,)).fetchone()
            if row is not None:
                if row["work_id"] != work_id:
                    raise RuntimeError("Hierarchy worker binding is immutable")
                return {"bound": True, "owner": owner, "work_id": work_id, "duplicate": True}
            db.execute("INSERT INTO worker_bindings(owner,work_id,created) VALUES(?,?,?)", (owner, work_id, time.time()))
            self._event(db, "worker_bound", {"owner": owner, "work_id": work_id})
            return {"bound": True, "owner": owner, "work_id": work_id}

    @staticmethod
    def _identity(owner: str, request_key: str):
        _text(owner, "owner", 300)
        _text(request_key, "request_key", 500)

    @staticmethod
    def _replay(db, request_key: str, fingerprint: str) -> dict | None:
        previous = db.execute("SELECT * FROM requests WHERE request_key=?", (request_key,)).fetchone()
        if previous is None:
            return None
        if previous["fingerprint"] != fingerprint:
            raise ValueError("Request key reused with different operation, owner or content")
        return {**json.loads(previous["response"]), "duplicate": True}

    @staticmethod
    def _save(db, request_key: str, fingerprint: str, response: dict) -> dict:
        db.execute("INSERT INTO requests VALUES(?,?,?)", (request_key, fingerprint, canonical(response)))
        return response

    def _lease(self, db, work_id: str, owner: str, token: str):
        _text(owner, "owner", 300)
        _text(token, "lease_token", 100)
        if "delegation" in self.spec:
            self._bound_work(db, owner, work_id)
        row = self._row(db, work_id)
        if (row["state"] != "leased" or row["lease_owner"] != owner or row["lease_token"] != token
                or row["lease_expires"] <= time.time()):
            # Commit any expiration discovered before rejecting a late completion.
            db.commit()
            raise RuntimeError("Work lease lost or expired; stale attempt cannot mutate the task")
        return row

    @staticmethod
    def _no_running_effects(db, work_id: str):
        if db.execute("SELECT 1 FROM effects WHERE work_id=? AND state='running' LIMIT 1", (work_id,)).fetchone():
            raise RuntimeError("Work still has running tools; wait for their results before releasing the lease")

    def _dependencies(self, db, work_id: str | None, deps: Any) -> list[str]:
        ids = _names(deps, "depends_on", 256)
        for dependency in ids:
            self._row(db, dependency)
            if dependency == work_id:
                raise ValueError("Work cannot depend on itself")
            if work_id:
                edges = "SELECT work_id source,depends_on target FROM dependencies"
                if "delegation" in self.spec:
                    edges += " UNION SELECT parent_work_id,work_id FROM delegations WHERE parent_work_id IS NOT NULL"
                if db.execute("WITH RECURSIVE edges(source,target) AS (" + edges + """), ancestors(id) AS (
                    SELECT ? UNION SELECT e.target FROM edges e JOIN ancestors a ON e.source=a.id
                    ) SELECT 1 FROM ancestors WHERE id=?""", (dependency, work_id)).fetchone():
                    raise ValueError("Dependency would create a cycle, including required descendant joins")
        return ids

    def dispatch_work(self, operation: str, args: dict, *, owner: str, request_key: str) -> dict:
        """Agent-facing graph methods; the native transport supplies owner/key."""
        args = _object(args, "arguments", 128 * 1024)
        _text(owner, "owner", 300)
        if operation == "read_work":
            return self.read_work(args.get("work_id"), owner=owner)
        if operation not in _MUTATIONS:
            raise ValueError(f"Unknown work operation: {operation}")
        self._identity(owner, request_key)
        fingerprint = digest({"operation": operation, "owner": owner, "arguments": args})
        with self._db(True) as db:
            self._refresh(db)
            if "delegation" in self.spec:
                if operation == "create_work":
                    raise RuntimeError("create_work is disabled in hierarchy campaigns; use delegate_work")
                self._bound_work(db, owner, args.get("work_id"))
            previous = self._replay(db, request_key, fingerprint)
            if previous is not None:
                return previous
            if db.execute("SELECT 1 FROM effects WHERE request_key=?", (request_key,)).fetchone():
                raise ValueError("Request key already belongs to a host tool effect")
            if operation == "create_work":
                result = self._create_work(db, args)
            elif operation == "delegate_work":
                result = self._delegate_work(db, args, owner)
            elif operation == "claim_work":
                result = self._claim_work(db, args, owner)
            else:
                row = self._lease(db, args.get("work_id"), owner, args.get("lease_token"))
                if operation == "renew_work":
                    expiry = min(self._deadline(db), time.time() + self.spec["limits"]["lease_seconds"])
                    db.execute("UPDATE work SET lease_expires=?,updated=? WHERE id=?", (expiry, time.time(), row["id"]))
                elif operation == "wait_work":
                    self._wait_work(db, row, args)
                elif operation == "complete_work":
                    self._complete_work(db, row, args)
                else:
                    self._fail_work(db, row, args)
                self._refresh(db)
                result = self._work(db, self._row(db, row["id"]), owner)
            return self._save(db, request_key, fingerprint, result)

    def _create_work(self, db, args: dict) -> dict:
        if "delegation" in self.spec:
            raise RuntimeError("create_work is disabled in hierarchy campaigns; use delegate_work")
        self._admission(db)
        goal = _text(args.get("goal"), "goal", 32000)
        skills = _names(args.get("skill_names", []), "skill_names")
        parent = args.get("parent_id")
        if parent is not None:
            self._row(db, parent)
        dependencies = self._dependencies(db, None, args.get("depends_on", []))
        if db.execute("SELECT COUNT(*) FROM work").fetchone()[0] >= self.spec["limits"]["max_work_items"]:
            raise RuntimeError("Work item limit reached; finish or refine existing work")
        work_id, now = "w_" + uuid.uuid4().hex, time.time()
        db.execute("""INSERT INTO work(id,goal,parent_id,skill_names,state,created,updated)
            VALUES(?,?,?,?,'pending',?,?)""", (work_id, goal, parent, canonical(skills), now, now))
        db.executemany("INSERT INTO dependencies VALUES(?,?)", [(work_id, dependency) for dependency in dependencies])
        self._refresh(db)
        self._event(db, "work_created", {"work_id": work_id, "parent_id": parent})
        return {"work_id": work_id, "work": self._work(db, self._row(db, work_id))}

    def _delegate_work(self, db, args: dict, owner: str) -> dict:
        if "delegation" not in self.spec:
            raise ValueError("delegate_work requires a hierarchy campaign")
        if set(args) - {"work_id", "lease_token", "goal", "skill_names", "depends_on", "brief"}:
            raise ValueError("Unexpected delegation arguments; parent and depth are host-derived")
        self._admission(db)
        parent = self._lease(db, args.get("work_id"), owner, args.get("lease_token"))
        hierarchy = self._delegation_info(db, parent["id"])
        if hierarchy["delegation_depth"] >= self.spec["delegation"]["max_depth"]:
            raise RuntimeError("Maximum delegation depth reached")
        if hierarchy["remaining_child_slots"] == 0:
            raise RuntimeError("Lifetime child limit reached for this work item")
        if db.execute("SELECT COUNT(*) FROM work").fetchone()[0] >= self.spec["limits"]["max_work_items"]:
            raise RuntimeError("Work item limit reached; finish existing work")
        goal = _text(args.get("goal"), "goal", 32000)
        skills = _names(args.get("skill_names", []), "skill_names")
        brief = _object(args.get("brief", {}), "delegation brief", 32 * 1024)
        # The new child has an implicit parent->child join edge. Reject a child
        # dependency that can return to its parent through any explicit or
        # implicit join edge, before admitting a permanently deadlocked branch.
        dependencies = self._dependencies(db, parent["id"], args.get("depends_on", []))
        work_id, now = "w_" + uuid.uuid4().hex, time.time()
        db.execute("""INSERT INTO work(id,goal,parent_id,skill_names,state,created,updated)
            VALUES(?,?,?,?,'pending',?,?)""", (work_id, goal, parent["id"], canonical(skills), now, now))
        db.execute("INSERT INTO delegations(work_id,parent_work_id,depth,brief) VALUES(?,?,?,?)",
                   (work_id, parent["id"], hierarchy["delegation_depth"] + 1, canonical(brief)))
        db.executemany("INSERT INTO dependencies VALUES(?,?)", [(work_id, dependency) for dependency in dependencies])
        self._refresh(db)
        self._event(db, "work_delegated", {"work_id": work_id, "parent_work_id": parent["id"],
                                            "depth": hierarchy["delegation_depth"] + 1})
        return {"work_id": work_id, "work": self._work(db, self._row(db, work_id))}

    def _claim_work(self, db, args: dict, owner: str) -> dict:
        self._admission(db)
        requested = args.get("work_id")
        if "delegation" in self.spec:
            requested = self._bound_work(db, owner, requested)
        owned = db.execute("SELECT * FROM work WHERE state='leased' AND lease_owner=?", (owner,)).fetchone()
        if owned:
            if requested is not None and requested != owned["id"]:
                raise RuntimeError("A worker must release its current lease before claiming another item")
            row = owned
        else:
            if requested is not None:
                self._row(db, requested)
                row = db.execute("SELECT w.*" + _READY_FROM + " AND w.id=?", (requested,)).fetchone()
            else:
                ready_rows = self._ready_rows(db, 1)
                row = ready_rows[0] if ready_rows else None
            if row is None:
                return {"claimed": False, "reason": "no_ready_work", "ready_work_count": self._ready_count(db)}
            leased = db.execute("SELECT COUNT(*) FROM work WHERE state='leased'").fetchone()[0]
            if leased >= self.spec["limits"]["max_workers"]:
                return {"claimed": False, "reason": "worker_capacity_busy", "ready_work_count": self._ready_count(db)}
            token, now = uuid.uuid4().hex, time.time()
            expiry = min(self._deadline(db), now + self.spec["limits"]["lease_seconds"])
            db.execute("""UPDATE work SET state='leased',lease_owner=?,lease_token=?,lease_expires=?,
                attempt_count=attempt_count+1,updated=? WHERE id=?""", (owner, token, expiry, now, row["id"]))
            row = self._row(db, row["id"])
            self._event(db, "work_claimed", {"work_id": row["id"], "owner": owner, "attempt": row["attempt_count"]})
        return {"claimed": True, "work_id": row["id"], "lease_token": row["lease_token"],
                "lease_expires": row["lease_expires"], "work": self._work(db, row, owner)}

    def _wait_work(self, db, row, args: dict):
        self._no_running_effects(db, row["id"])
        join_mode = args.get("join_mode", "all_success")
        if join_mode not in ("all_success", "all_settled"):
            raise ValueError("join_mode must be all_success or all_settled")
        if "delegation" in self.spec:
            # A fresh batch replaces the previous wait, whose failed outcomes
            # remain discoverable through children and immutable evidence.
            db.execute("DELETE FROM dependencies WHERE work_id=?", (row["id"],))
        dependencies = self._dependencies(db, row["id"], args.get("depends_on"))
        if not dependencies:
            raise ValueError("wait_work needs at least one dependency")
        db.executemany("INSERT OR IGNORE INTO dependencies VALUES(?,?)", [(row["id"], dep) for dep in dependencies])
        db.execute("""INSERT INTO work_policies(work_id,join_mode) VALUES(?,?)
            ON CONFLICT(work_id) DO UPDATE SET join_mode=excluded.join_mode""", (row["id"], join_mode))
        db.execute("""UPDATE work SET state='pending',updated=?,lease_owner=NULL,lease_token=NULL,
            lease_expires=NULL WHERE id=?""", (time.time(), row["id"]))
        self._event(db, "work_waiting", {"work_id": row["id"], "depends_on": dependencies, "join_mode": join_mode})

    def _complete_work(self, db, row, args: dict):
        self._no_running_effects(db, row["id"])
        if "delegation" in self.spec:
            unfinished = db.execute("""WITH RECURSIVE descendants(id) AS (
                SELECT work_id FROM delegations WHERE parent_work_id=?
                UNION SELECT d.work_id FROM delegations d JOIN descendants parent ON d.parent_work_id=parent.id)
                SELECT w.id FROM work w JOIN descendants d ON d.id=w.id
                WHERE w.state NOT IN ('succeeded','failed','blocked','cancelled') LIMIT 1""", (row["id"],)).fetchone()
            if unfinished:
                raise RuntimeError("Cannot complete work while delegated descendants are unfinished")
        result = _object(args.get("result"), "result")
        effects = _names(args.get("effect_ids", []), "effect_ids", 128)
        for effect_id in effects:
            effect = db.execute("SELECT state FROM effects WHERE id=?", (effect_id,)).fetchone()
            if effect is None or effect["state"] == "running":
                raise ValueError("effect_ids must reference finished host tool records")
        db.execute("""UPDATE work SET state='succeeded',result=?,effect_ids=?,updated=?,error=NULL,
            lease_owner=NULL,lease_token=NULL,lease_expires=NULL WHERE id=?""",
                   (canonical(result), canonical(effects), time.time(), row["id"]))
        self._event(db, "work_completed", {"work_id": row["id"], "effect_ids": effects,
                                            "result_status": "agent_authored_not_independently_verified"})

    def _fail_work(self, db, row, args: dict):
        self._no_running_effects(db, row["id"])
        error = _text(args.get("error"), "error", 8000)
        retry = args.get("retry", True)
        if not isinstance(retry, bool):
            raise ValueError("retry must be a boolean")
        failures = row["failure_count"] + 1
        state = "pending" if retry and failures < self.spec["limits"]["max_failures"] else "failed"
        db.execute("""UPDATE work SET state=?,failure_count=?,error=?,updated=?,
            lease_owner=NULL,lease_token=NULL,lease_expires=NULL WHERE id=?""",
                   (state, failures, error, time.time(), row["id"]))
        self._event(db, "work_failed", {"work_id": row["id"], "state": state, "failure_count": failures})

    def read_work(self, work_id: str, *, owner: str | None = None) -> dict:
        with self._db(True) as db:
            self._refresh(db)
            return self._work(db, self._row(db, work_id), owner)

    def list_work(self, *, state: str | None = None, after_id: str | None = None,
                  limit: int = 25) -> dict:
        """Page the graph by immutable ID; lease credentials are always omitted."""
        if state is not None and state not in TERMINAL_STATES | {"pending", "leased"}:
            raise ValueError("Unknown work state")
        if after_id is not None:
            _text(after_id, "after_id", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("list_work limit must be an integer in 1..100")
        filters, values = [], []
        if state is not None:
            filters.append("state=?")
            values.append(state)
        if after_id is not None:
            filters.append("id>?")
            values.append(after_id)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self._db(True) as db:
            self._refresh(db)
            rows = db.execute("SELECT * FROM work" + where + " ORDER BY id LIMIT ?",
                              [*values, limit + 1]).fetchall()
            return {"items": [self._work(db, row) for row in rows[:limit]],
                    "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}

    def reserve_effect(self, owner: str, work_id: str, lease_token: str,
                       capability: str, arguments: dict, request_key: str) -> dict:
        """Reserve one host tool effect. A replay is a receipt, never a new call."""
        self._identity(owner, request_key)
        if capability not in self.spec["capabilities"]:
            raise ValueError("Capability is not enabled in the immutable task contract")
        arguments = _object(arguments, "tool arguments", 512 * 1024)
        fingerprint = digest({"owner": owner, "work_id": work_id, "lease_token": lease_token,
                              "capability": capability, "arguments": arguments})
        with self._db(True) as db:
            self._refresh(db)
            if "delegation" in self.spec:
                self._bound_work(db, owner, work_id)
            previous = db.execute("SELECT * FROM effects WHERE request_key=?", (request_key,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("Effect request key reused with different content or lease")
                return {**self._effect(previous), "effect_id": previous["id"], "execute": False, "duplicate": True}
            if db.execute("SELECT 1 FROM requests WHERE request_key=?", (request_key,)).fetchone():
                raise ValueError("Request key already belongs to a work mutation")
            self._admission(db)
            row = self._lease(db, work_id, owner, lease_token)
            running = db.execute("SELECT COUNT(*) FROM effects WHERE state='running'").fetchone()[0]
            if running >= self.spec["limits"]["max_parallel_tools"]:
                raise RuntimeError("Tool capacity busy; retry after an active tool finishes")
            effect_id = "e_" + uuid.uuid4().hex
            db.execute("""INSERT INTO effects(id,request_key,fingerprint,owner,work_id,lease_token,attempt,
                capability,arguments,state,created) VALUES(?,?,?,?,?,?,?,?,?,'running',?)""",
                       (effect_id, request_key, fingerprint, owner, work_id, lease_token,
                        row["attempt_count"], capability, canonical(arguments), time.time()))
            self._event(db, "effect_reserved", {"effect_id": effect_id, "work_id": work_id, "capability": capability})
            effect = db.execute("SELECT * FROM effects WHERE id=?", (effect_id,)).fetchone()
            return {**self._effect(effect), "effect_id": effect_id, "execute": True}

    @staticmethod
    def _effect(row) -> dict:
        item = dict(row)
        # Lease credentials and transport keys are not part of shared evidence.
        for name in ("lease_token", "request_key", "fingerprint"):
            item.pop(name)
        item["arguments"] = json.loads(item["arguments"])
        item["arguments_sha256"] = digest(item["arguments"])
        item["result"] = json.loads(item["result"]) if item["result"] is not None else None
        item["result_sha256"] = digest(item["result"]) if item["result"] is not None else None
        item["result_status"] = "host_tool_output"
        return item

    def finish_effect(self, effect_id: str, owner: str, work_id: str, lease_token: str, *,
                      result: dict | None = None, error: str | None = None,
                      stop_reason: str | None = None) -> dict:
        """Host-only completion, atomically applying any requested campaign stop.

        The caller determines whether stopping is required. The kernel neither
        interprets domain-specific result fields nor accepts stops from agents.
        """
        if (result is None) == (error is None):
            raise ValueError("Provide exactly one of a tool result or an execution error")
        if result is not None:
            result = _object(result, "tool result", 2 * 1024 * 1024)
        if error is not None:
            _text(error, "tool error", 8000)
        if stop_reason is not None:
            _text(stop_reason, "stop reason", 1000)
        encoded = canonical(result) if result is not None else None
        with self._db(True) as db:
            self._refresh(db)
            if "delegation" in self.spec:
                self._bound_work(db, owner, work_id)
            row = db.execute("SELECT * FROM effects WHERE id=?", (effect_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown effect")
            if (row["owner"], row["work_id"], row["lease_token"]) != (owner, work_id, lease_token):
                raise RuntimeError("Effect owner or attempt does not match")
            if row["state"] in ("succeeded", "failed"):
                if (row["result"], row["error"]) != (encoded, error):
                    raise ValueError("Completed effect is immutable")
                if stop_reason is not None and db.execute("SELECT status FROM campaign WHERE id=1").fetchone()[0] == "active":
                    self._stop_in_transaction(db, stop_reason)
                return {**self._effect(row), "duplicate": True}
            if row["state"] != "running":
                db.commit()
                raise RuntimeError("Effect was interrupted; late result is fenced")
            self._lease(db, work_id, owner, lease_token)
            state = "succeeded" if result is not None else "failed"
            db.execute("UPDATE effects SET state=?,finished=?,result=?,error=? WHERE id=?",
                       (state, time.time(), encoded, error, effect_id))
            self._event(db, "effect_completed", {"effect_id": effect_id, "state": state,
                                                "result_sha256": digest(result) if result is not None else None})
            if stop_reason is not None:
                self._stop_in_transaction(db, stop_reason)
            return self._effect(db.execute("SELECT * FROM effects WHERE id=?", (effect_id,)).fetchone())

    def effect(self, effect_id: str) -> dict:
        _text(effect_id, "effect_id", 100)
        with self._db(True) as db:
            self._refresh(db)
            row = db.execute("SELECT * FROM effects WHERE id=?", (effect_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown effect")
            return self._effect(row)

    @staticmethod
    def _effect_summary(row) -> dict:
        summary = dict(row)
        summary["result_available"] = bool(summary["result_available"])
        summary["result_status"] = "host_tool_output"
        return summary

    def list_effects(self, work_id: str, *, after_id: str | None = None,
                     limit: int = 25) -> dict:
        """Discover a work item's receipts, including ones not yet cited.

        Only bounded metadata is returned. Use effect(id) for the original
        arguments, exact result and hashes after choosing a relevant receipt.
        """
        _text(work_id, "work_id", 100)
        if after_id is not None:
            _text(after_id, "after_id", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("list_effects limit must be an integer in 1..100")
        with self._db(True) as db:
            self._refresh(db)
            self._row(db, work_id)
            after = " AND id>?" if after_id is not None else ""
            values = [work_id, after_id, limit + 1] if after_id is not None else [work_id, limit + 1]
            rows = db.execute("""SELECT id,capability,state,attempt,created,finished,
                (result IS NOT NULL) AS result_available FROM effects WHERE work_id=?"""
                              + after + " ORDER BY id LIMIT ?", values).fetchall()
            return {"items": [self._effect_summary(row) for row in rows[:limit]],
                    "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}

    def interrupt_effect(self, owner: str, request_key: str, reason: str) -> dict:
        """Host-only fencing after a tool RPC exits without a usable response.

        Worker heartbeats can legitimately keep the parent work alive while a
        tool RPC is lost. Fence that one effect explicitly so its slot does not
        remain reserved for the lifetime of a healthy worker. The caller must
        first stop/lose the RPC; interruption records an unknown outcome and
        does not assert that a remote side effect never happened.
        """
        self._identity(owner, request_key)
        _text(reason, "interruption reason", 8000)
        with self._db(True) as db:
            self._refresh(db)
            row = db.execute("SELECT * FROM effects WHERE request_key=?", (request_key,)).fetchone()
            if row is None:
                return {"found": False, "interrupted": False}
            if row["owner"] != owner:
                raise RuntimeError("Effect request belongs to another owner")
            interrupted = row["state"] == "running"
            if interrupted:
                db.execute("UPDATE effects SET state='interrupted',finished=?,error=? WHERE id=?",
                           (time.time(), "Tool RPC interrupted; outcome is unknown: " + reason, row["id"]))
                self._event(db, "effect_interrupted", {"effect_id": row["id"], "work_id": row["work_id"],
                                                       "reason": reason})
                row = db.execute("SELECT * FROM effects WHERE id=?", (row["id"],)).fetchone()
            return {"found": True, "interrupted": interrupted, "effect": self._effect(row)}

    def reserve_model_call(self, request_id: str) -> dict:
        _text(request_id, "request_id", 300)
        with self._db(True) as db:
            self._refresh(db)
            if db.execute("SELECT 1 FROM calls WHERE request_id=?", (request_id,)).fetchone():
                return {"allowed": False, "reason": "request_id_already_reserved"}
            reason = self._stop_reason(db)
            if reason:
                return {"allowed": False, "reason": reason}
            db.execute("INSERT INTO calls(request_id,created) VALUES(?,?)", (request_id, time.time()))
            return {"allowed": True, "call_index": db.execute("SELECT COUNT(*) FROM calls").fetchone()[0],
                    "max_output_tokens": self.spec["limits"]["max_output_tokens"],
                    "search_wall_seconds_remaining": max(0, self._deadline(db) - time.time())}

    def record_usage(self, request_id: str, input_tokens: int, output_tokens: int) -> dict:
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (input_tokens, output_tokens)):
            raise ValueError("Token usage must be nonnegative integers")
        with self._db(True) as db:
            row = db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown model call reservation")
            if row["input_tokens"] is not None:
                if (row["input_tokens"], row["output_tokens"]) != (input_tokens, output_tokens):
                    raise ValueError("Conflicting usage for a recorded model call")
                return {"recorded": True, "duplicate": True}
            db.execute("UPDATE calls SET input_tokens=?,output_tokens=? WHERE request_id=?",
                       (input_tokens, output_tokens, request_id))
            return {"recorded": True}

    def recover(self) -> dict:
        with self._db(True) as db:
            result = self._refresh(db)
            result["work_counts"] = self._counts(db)
            return result

    def renew_live_leases(self, owners: list[str]) -> dict:
        """Host-only liveness heartbeat, including while a model is thinking.

        The launcher supplies its currently live native thread IDs. Expiration
        is processed first, so a heartbeat cannot revive a lost attempt. This
        method intentionally creates neither events nor request receipts for
        every polling tick; actual work/effect transitions remain journaled.
        """
        if not isinstance(owners, list) or len(owners) > self.spec["limits"]["max_workers"]:
            raise ValueError("Heartbeat owners must be a list within max_workers")
        for owner in owners:
            _text(owner, "heartbeat owner", 300)
        if len(set(owners)) != len(owners):
            raise ValueError("Heartbeat owners must be distinct")
        with self._db(True) as db:
            recovery = self._refresh(db)
            if "delegation" in self.spec:
                for owner in owners:
                    self._bound_work(db, owner)
            now = time.time()
            expiry = min(self._deadline(db), now + self.spec["limits"]["lease_seconds"])
            renewed = 0
            if owners and expiry > now:
                placeholders = ",".join("?" for _ in owners)
                # Owner stops close admission but allow already-live work and
                # tools to drain. Their leases remain bounded by the hard wall.
                changed = db.execute("""UPDATE work SET lease_expires=?
                    WHERE state='leased' AND lease_expires>? AND lease_owner IN ("""
                                     + placeholders + ")", [expiry, now, *owners])
                renewed = changed.rowcount
            return {"renewed": renewed, "lease_expires": expiry if renewed else None,
                    "expired_leases": recovery["expired_leases"],
                    "interrupted_effects": recovery["interrupted_effects"]}

    def _stop_in_transaction(self, db, reason: str):
        """Apply host stop policy inside the caller's existing write transaction."""
        db.execute("UPDATE campaign SET status='stopped',reason=? WHERE id=1", (reason,))
        self._refresh(db)
        self._event(db, "campaign_stopped", {"reason": reason})

    def stop(self, reason: str = "stopped_by_owner"):
        _text(reason, "stop reason", 1000)
        with self._db(True) as db:
            self._stop_in_transaction(db, reason)

    def context(self) -> dict:
        with self._db(True) as db:
            self._refresh(db)
            reason = self._stop_reason(db)
            counts = self._counts(db)
            remaining = max(0, self._deadline(db) - time.time())
            calls = db.execute("""SELECT COUNT(*) n,COALESCE(SUM(input_tokens),0) i,
                COALESCE(SUM(output_tokens),0) o,
                COALESCE(SUM(CASE WHEN input_tokens IS NULL THEN 1 ELSE 0 END),0) unknown FROM calls""").fetchone()
            active = [dict(row) for row in db.execute("""SELECT id AS work_id,lease_owner AS owner,
                lease_expires AS expiry FROM work WHERE state='leased' ORDER BY created,id LIMIT 1000""")]
            ready = [{"work_id": row["id"], "goal": row["goal"][:1024],
                      "goal_truncated": len(row["goal"]) > 1024,
                      "skill_names": json.loads(row["skill_names"]),
                      **self._delegation_info(db, row["id"])}
                     for row in self._ready_rows(db)]
            recent = [self._work(db, row) for row in db.execute("SELECT * FROM work ORDER BY updated DESC,id LIMIT 32")]
            # The shared snapshot contains bounded summaries; read_work retrieves exact results.
            for item in recent:
                result = item.pop("result")
                item["result_available"] = result is not None
                item["result_sha256"] = digest(result) if result is not None else None
                item["goal_truncated"] = len(item["goal"]) > 1024
                item["goal"] = item["goal"][:1024]
            running_effects = db.execute("SELECT COUNT(*) FROM effects WHERE state='running'").fetchone()[0]
            result = {
                "schema_version": 2, "run_kind": "generic", "task_sha256": digest(self.spec),
                "task": {"task_id": self.spec["task_id"], "goal": self.spec["goal"]},
                "root_work_id": "root", "capabilities": self.spec["capabilities"],
                "stop": reason is not None, "stop_reason": reason,
                "draining": reason is not None and running_effects > 0,
                "objective_status": "not_inferred_from_work_completion",
                "ready_work_count": self._ready_count(db), "ready_work": ready,
                "work_counts": counts, "active_leases": active, "recent_work": recent,
                "active_tools": running_effects,
                "max_workers": self.spec["limits"]["max_workers"],
                "max_output_tokens": self.spec["limits"]["max_output_tokens"],
                "wall_seconds_remaining": remaining, "search_wall_seconds_remaining": remaining,
                "budget": {**self.spec["limits"], "model_calls_used": calls["n"],
                           "observed_input_tokens": calls["i"], "observed_output_tokens": calls["o"],
                           "calls_without_usage": calls["unknown"]},
            }
            if "delegation" in self.spec:
                result["delegation"] = dict(self.spec["delegation"])
            if "max_parallel_model_calls" in self.spec["limits"]:
                result["max_parallel_model_calls"] = self.spec["limits"]["max_parallel_model_calls"]
            return result

    def report(self) -> dict:
        result = self.context()
        with self._db() as db:
            result["work"] = [self._work(db, row) for row in db.execute("SELECT * FROM work ORDER BY created,id")]
            result["effects"] = [self._effect(row) for row in db.execute("SELECT * FROM effects ORDER BY created,id")]
            result["events"] = [{**dict(row), "payload": json.loads(row["payload"])}
                                for row in db.execute("SELECT * FROM events ORDER BY id")]
        return result
