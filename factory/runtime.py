"""Durable, single-host Workflow Runtime for the Image Factory.

This module deliberately does not call Feishu, an image provider, or a shell.
It owns only the local execution record: frozen plans, runs, step runs,
attempts, budget reservations, trusted review snapshots, and an outbox journal
for a separate status projector.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class ContractError(ValueError):
    """The request or durable contract is malformed."""


class Conflict(ContractError):
    """The caller attempted to change a frozen or already-confirmed value."""


class Blocked(ContractError):
    """The operation is not safe in the current durable state."""


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_NODE_TYPES = {
    "codex_brief",
    "image_generate",
    "compose",
    "human_review",
    "export",
    "metrics",
}
_EXTERNAL_NODES = {"codex_brief", "image_generate"}


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ContractError("Unsafe or empty identifier")
    return value


def integer(value: Any, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ContractError(f"{label}: integer in [{low}, {high}] required")
    return value


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} required")
    return value


def checked_plan(plan: dict) -> dict:
    """Validate and detach the only workflow definition the runtime accepts."""
    if not isinstance(plan, dict):
        raise ContractError("Plan must be an object")
    try:
        frozen = json.loads(canonical(plan))
    except (TypeError, ValueError) as exc:
        raise ContractError("Plan must be JSON data") from exc
    if frozen.get("schema_version") != 1 or frozen.get("category") != "kids_shoes":
        raise ContractError("Only schema=1, category=kids_shoes supported")
    identifier(frozen.get("run_id"))
    if frozen.get("mode") not in ("demo", "production"):
        raise ContractError("Explicit demo or production mode required")
    _nonempty_text(frozen.get("workflow_version"), "Workflow version")
    integer(frozen.get("max_image_calls"), 0, 100, "max_image_calls")
    if "authorization_id" in frozen:
        identifier(frozen["authorization_id"])
        if "authorization_scope" in frozen:
            try:
                canonical(frozen["authorization_scope"])
            except (TypeError, ValueError) as exc:
                raise ContractError("authorization_scope must be JSON data") from exc
    steps = frozen.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ContractError("Nonempty ordered steps required")
    seen: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ContractError("Step must be an object")
        sid = identifier(step.get("id"))
        if sid in seen or step.get("kind") not in _NODE_TYPES:
            raise ContractError("Duplicate step or unsupported node type")
        dependencies = step.get("depends_on", [])
        if not isinstance(dependencies, list):
            raise ContractError("depends_on must be a list")
        if any(not isinstance(dep, str) for dep in dependencies):
            raise ContractError("Dependencies must be step identifiers")
        if len(dependencies) != len(set(dependencies)):
            raise ContractError("Duplicate step dependency")
        if any(dep not in seen for dep in dependencies):
            raise ContractError("Dependencies must precede step; no cycles or forward references")
        seen.add(sid)
    return frozen


def _validate_receipt_shape(
    receipt: dict, job: dict, run_mode: str, required_origin: str
) -> None:
    if not isinstance(receipt, dict):
        raise ContractError("Receipt must be an object")
    if receipt.get("job_id") != job["job_id"]:
        raise Conflict("Receipt does not belong to frozen job")
    if receipt.get("request_hash") != job["request_hash"]:
        raise Conflict("Receipt does not belong to frozen request")
    if receipt.get("attempt_id") not in (None, job.get("attempt_id")):
        raise Conflict("Receipt does not belong to frozen attempt")
    if receipt.get("mode") != run_mode or receipt.get("status") != "succeeded":
        raise Blocked("Mode mismatch or unsuccessful receipt")
    if receipt.get("origin") != required_origin:
        raise Blocked("Receipt origin mismatch; mock is not native generation")
    for key in ("tool", "model_reported", "evidence_ref"):
        _nonempty_text(receipt.get(key), f"Missing receipt {key}")
    outputs = receipt.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise ContractError("Receipt needs at least one output")
    definition = job.get("request", {})
    if isinstance(definition, str):
        definition = json.loads(definition)
    node = definition.get("definition", {})
    if node.get("kind") == "image_generate":
        expected = node.get("independent_image_count", node.get("max_outputs", 1))
        integer(expected, 1, 10, "independent_image_count")
        if len(outputs) != expected:
            raise Conflict("Unexpected image output count")
    names: set[str] = set()
    for output in outputs:
        if not isinstance(output, dict):
            raise ContractError("Receipt output must be an object")
        name = identifier(output.get("name"))
        if name in names:
            raise Conflict("Duplicate output name")
        names.add(name)
        if not isinstance(output.get("sha256"), str) or not _HEX64.fullmatch(output["sha256"]):
            raise ContractError("Output hash required")
        integer(output.get("bytes"), 1, 100_000_000, "output bytes")


def validate_receipt(receipt: dict, job: dict, run_mode: str, required_origin: str) -> None:
    """Public shape validation; the Runtime additionally reads actual bytes."""
    _validate_receipt_shape(receipt, job, run_mode, required_origin)


def _latest_reviews(events: list[dict]) -> dict[str, dict]:
    if not isinstance(events, list):
        raise ContractError("Review events required")
    latest: dict[str, dict] = {}
    seen: set[tuple[str, int]] = set()
    for event in events:
        if not isinstance(event, dict):
            raise ContractError("Review event must be an object")
        aid = identifier(event.get("asset_id"))
        revision = event.get("revision")
        if type(revision) is not int or revision < 1:
            raise ContractError("Trusted monotonic revision required")
        if (aid, revision) in seen:
            raise Conflict("Conflicting or duplicated review revision")
        seen.add((aid, revision))
        if aid not in latest or revision > latest[aid]["revision"]:
            latest[aid] = event
    return latest


def validate_review_snapshot(snapshot: dict, mode: str) -> dict:
    """Validate a snapshot independently supplied by a trusted adapter.

    This function does not authenticate a person. The adapter must obtain the
    actor and latest records from the authoritative review system before
    passing the snapshot to ``finish_review``.
    """
    if not isinstance(snapshot, dict) or snapshot.get("mode") != mode or mode not in (
        "demo",
        "production",
    ):
        raise Blocked("Review scope mismatch")
    run_id = identifier(snapshot.get("run_id"))
    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ContractError("Candidate set required")
    ids: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ContractError("Candidate must be an object")
        aid = identifier(candidate.get("asset_id"))
        if not isinstance(candidate.get("sha256"), str) or not _HEX64.fullmatch(candidate["sha256"]):
            raise ContractError("Candidate hash required")
        ids.append(aid)
    if len(ids) != len(set(ids)):
        raise Conflict("Duplicate candidates")
    latest = _latest_reviews(snapshot.get("events", []))
    if set(latest) != set(ids):
        raise Blocked("All candidates must have review; unknown extra assets forbidden")
    approved: list[str] = []
    rejected: list[str] = []
    for candidate in candidates:
        aid = candidate["asset_id"]
        event = latest[aid]
        if event.get("run_id") != run_id or event.get("mode") != mode:
            raise Blocked("Review belongs to different run or mode")
        if event.get("sha256") != candidate.get("sha256"):
            raise Conflict("Review must bind exact current asset bytes")
        if event.get("decision") not in ("approve", "reject"):
            raise Blocked("Pending/revoked review needs a new decision")
        if not isinstance(event.get("actor_id"), str) or not event["actor_id"].strip():
            raise Blocked("Human actor required")
        if event.get("human_confirmed") is not True:
            raise Blocked("Explicit human confirmation required")
        if not isinstance(event.get("reason"), str) or not event["reason"].strip():
            raise Blocked("Review reason required")
        required = ("demo_visual_ok", "demo_use_only") if mode == "demo" else (
            "product_accuracy",
            "brand_channel_ok",
        )
        if event.get("decision") == "approve":
            if any(event.get(field) is not True for field in required):
                raise Blocked("Required checks incomplete: " + ", ".join(required))
            approved.append(aid)
        else:
            rejected.append(aid)
    return {"approved": approved, "rejected": rejected, "latest": latest}


def _now() -> str:
    return str(time.time())


class Runtime:
    """SQLite-backed execution authority for one local runtime.

    ``single_instance=True`` adds an OS advisory lock. Even without that
    option, all claims and budget updates use ``BEGIN IMMEDIATE`` and remain
    atomic across connections to the same database.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        authorization: dict | None = None,
        single_instance: bool = False,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_runs(
                id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                plan TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                max_calls INTEGER NOT NULL,
                spent INTEGER NOT NULL DEFAULT 0,
                authorization_id TEXT,
                authorization_scope TEXT,
                state TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_steps(
                run_id TEXT NOT NULL REFERENCES runtime_runs(id),
                id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                kind TEXT NOT NULL,
                depends_on TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                output TEXT,
                PRIMARY KEY(run_id,id)
            );
            CREATE TABLE IF NOT EXISTS runtime_attempts(
                job_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                state TEXT NOT NULL,
                request TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                origin TEXT NOT NULL,
                worker_handle TEXT,
                receipt TEXT,
                outcome_evidence TEXT,
                claimed_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id,step_id,attempt_no),
                FOREIGN KEY(run_id,step_id) REFERENCES runtime_steps(run_id,id)
            );
            CREATE TABLE IF NOT EXISTS runtime_authorizations(
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                scope_hash TEXT NOT NULL,
                max_calls INTEGER NOT NULL,
                reserved INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_writes(
                id TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                receipt TEXT,
                evidence TEXT
            );
            CREATE TABLE IF NOT EXISTS runtime_outbox(
                op_id TEXT PRIMARY KEY,
                run_id TEXT,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                receipt TEXT,
                evidence TEXT,
                claimed_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._lock_handle = None
        if single_instance:
            self.acquire_instance()
        self.default_authorization = authorization
        if authorization is not None:
            with self.txn():
                self._register_authorization(authorization)

    def close(self) -> None:
        self.release_instance()
        self.db.close()

    def acquire_instance(self) -> None:
        if self._lock_handle is not None:
            return
        try:
            import fcntl  # type: ignore
        except ImportError as exc:  # pragma: no cover - supported hosts are Unix
            raise Blocked("OS single-instance lock is unavailable") from exc
        path = self.db_path.with_name(self.db_path.name + ".lock")
        handle = path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise Blocked("Another runtime instance owns this database") from exc
        self._lock_handle = handle

    def release_instance(self) -> None:
        if self._lock_handle is None:
            return
        try:
            import fcntl  # type: ignore

            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_handle.close()
            self._lock_handle = None

    @contextmanager
    def txn(self) -> Iterator[None]:
        try:
            self.db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise Blocked("Runtime database is busy; no claim was made") from exc
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def _register_authorization(self, grant: dict) -> dict:
        if not isinstance(grant, dict):
            raise ContractError("Authorization must be an object")
        auth_id = grant.get("authorization_id", grant.get("id"))
        identifier(auth_id)
        scope = grant.get("scope", grant.get("authorization_scope"))
        if scope is None:
            raise ContractError("Authorization scope required")
        scope_json = canonical(scope)
        scope_hash = digest(scope)
        limit = grant.get("project_image_calls_limit")
        if limit is None:
            limit = grant.get("total_image_calls", grant.get("max_image_calls"))
        integer(limit, 0, 100, "project_image_calls_limit")
        payload = canonical(grant)
        old = self.db.execute("SELECT * FROM runtime_authorizations WHERE id=?", (auth_id,)).fetchone()
        if old is not None:
            if old["scope_hash"] != scope_hash or old["max_calls"] != limit:
                raise Conflict("authorization_id is bound to a different scope or budget")
            return dict(old)
        self.db.execute(
            "INSERT INTO runtime_authorizations(id,scope,scope_hash,max_calls,payload,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (auth_id, scope_json, scope_hash, limit, payload, _now(), _now()),
        )
        row = self.db.execute("SELECT * FROM runtime_authorizations WHERE id=?", (auth_id,)).fetchone()
        return dict(row)

    def register_authorization(self, grant: dict) -> str:
        with self.txn():
            self._register_authorization(grant)
        return grant.get("authorization_id", grant.get("id"))

    def authorization_status(self, authorization_id: str) -> dict:
        identifier(authorization_id)
        row = self.db.execute("SELECT * FROM runtime_authorizations WHERE id=?", (authorization_id,)).fetchone()
        if row is None:
            raise ContractError("Unknown authorization")
        return {
            "authorization_id": row["id"],
            "scope": json.loads(row["scope"]),
            "max_calls": row["max_calls"],
            "reserved": row["reserved"],
            "remaining": row["max_calls"] - row["reserved"],
        }

    def _run(self, run_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM runtime_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ContractError("Unknown run")
        return row

    def _step(self, run_id: str, step_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM runtime_steps WHERE run_id=? AND id=?", (run_id, step_id)
        ).fetchone()
        if row is None:
            raise ContractError("Unknown step")
        return row

    def _step_ready(self, run_id: str, step_id: str) -> sqlite3.Row:
        step = self._step(run_id, step_id)
        n = self._next(run_id)
        if n.get("step") != step_id or n.get("state") != "pending":
            raise Blocked("Step not ready; preserve waiting/unknown state")
        return step

    def _derive_run_state(self, run_id: str) -> str:
        rows = self.db.execute(
            "SELECT kind,state FROM runtime_steps WHERE run_id=? ORDER BY idx", (run_id,)
        ).fetchall()
        states = [row["state"] for row in rows]
        if states and all(state == "succeeded" for state in states):
            return "completed"
        if "unknown" in states:
            return "blocked"
        if "waiting_review" in states:
            return "waiting_review"
        if "waiting_worker" in states:
            return "waiting_worker"
        if "running" in states:
            return "running"
        review_succeeded = any(
            row["kind"] == "human_review" and row["state"] == "succeeded" for row in rows
        )
        if review_succeeded:
            return "delivery_pending_sync"
        return "queued"

    def _touch_run(self, run_id: str) -> None:
        self.db.execute(
            "UPDATE runtime_runs SET state=?,updated_at=? WHERE id=?",
            (self._derive_run_state(run_id), _now(), run_id),
        )

    def _event(self, run_id: str, event: str, detail: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO runtime_events(run_id,event,detail,created_at) VALUES(?,?,?,?)",
            (run_id, event, canonical(detail), _now()),
        )
        seq = int(cur.lastrowid)
        payload = {
            "run_id": run_id,
            "event": event,
            "detail": detail,
            "state": self._derive_run_state(run_id),
            "event_seq": seq,
        }
        payload_text = canonical(payload)
        self.db.execute(
            "INSERT INTO runtime_outbox(op_id,run_id,kind,payload,payload_hash,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                f"status_{run_id}_{seq}",
                run_id,
                "status_projection",
                payload_text,
                digest(payload),
                "pending",
                _now(),
                _now(),
            ),
        )
        return seq

    def create(self, plan: dict, authorization: dict | None = None) -> str:
        frozen = checked_plan(plan)
        auth = authorization or self.default_authorization
        auth_id = frozen.get("authorization_id")
        if auth is not None and not auth_id:
            raise Blocked("Authorization-scoped runtime requires authorization_id in the frozen plan")
        with self.txn():
            if auth_id:
                if auth is None:
                    old_auth = self.db.execute(
                        "SELECT id FROM runtime_authorizations WHERE id=?", (auth_id,)
                    ).fetchone()
                    if old_auth is None:
                        raise Blocked("Frozen plan requires a registered authorization")
                else:
                    if auth.get("authorization_id", auth.get("id")) != auth_id:
                        raise Blocked("Authorization does not bind the frozen plan")
                    self._register_authorization(auth)
                grant = self.db.execute(
                    "SELECT * FROM runtime_authorizations WHERE id=?", (auth_id,)
                ).fetchone()
                if frozen.get("authorization_scope") is not None and digest(
                    frozen["authorization_scope"]
                ) != grant["scope_hash"]:
                    raise Conflict("Frozen plan scope differs from authorization scope")
            existing = self.db.execute("SELECT * FROM runtime_runs WHERE id=?", (frozen["run_id"],)).fetchone()
            plan_hash = digest(frozen)
            if existing is not None:
                if existing["plan_hash"] != plan_hash:
                    raise Conflict("Frozen run changed; create a new revision")
                return "existing"
            scope_json = None
            if auth_id:
                scope_json = self.db.execute(
                    "SELECT scope FROM runtime_authorizations WHERE id=?", (auth_id,)
                ).fetchone()[0]
            now = _now()
            self.db.execute(
                "INSERT INTO runtime_runs(id,mode,plan,plan_hash,max_calls,authorization_id,authorization_scope,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    frozen["run_id"],
                    frozen["mode"],
                    canonical(frozen),
                    plan_hash,
                    frozen["max_image_calls"],
                    auth_id,
                    scope_json,
                    "queued",
                    now,
                    now,
                ),
            )
            for index, step in enumerate(frozen["steps"]):
                self.db.execute(
                    "INSERT INTO runtime_steps(run_id,id,idx,kind,depends_on) VALUES(?,?,?,?,?)",
                    (
                        frozen["run_id"],
                        step["id"],
                        index,
                        step["kind"],
                        canonical(step.get("depends_on", [])),
                    ),
                )
            self._event(frozen["run_id"], "run_created", {"plan_hash": plan_hash})
        return "created"

    def plan(self, run_id: str) -> dict:
        return json.loads(self._run(run_id)["plan"])

    def _next(self, run_id: str) -> dict:
        self._run(run_id)
        row = self.db.execute(
            "SELECT * FROM runtime_steps WHERE run_id=? AND state!=? ORDER BY idx LIMIT 1",
            (run_id, "succeeded"),
        ).fetchone()
        if row is None:
            return {"state": "completed", "step": None}
        step = dict(row)
        if step["state"] == "pending":
            earlier = self.db.execute(
                "SELECT COUNT(*) FROM runtime_steps WHERE run_id=? AND idx<? AND state!=?",
                (run_id, step["idx"], "succeeded"),
            ).fetchone()[0]
            if earlier:
                return {"state": "blocked", "step": step["id"], "kind": step["kind"]}
        return {"state": step["state"], "step": step["id"], "kind": step["kind"]}

    def next(self, run_id: str) -> dict:
        return self._next(run_id)

    def step_output(self, run_id: str, step_id: str) -> dict:
        row=self._step(run_id,step_id)
        if row["state"]!="succeeded" or row["output"] is None:
            raise Blocked("Step has no accepted output")
        return json.loads(row["output"])

    def _authorization_for_dispatch(self, run: sqlite3.Row, authorization: dict) -> sqlite3.Row | None:
        auth_id = run["authorization_id"]
        if not auth_id:
            return None
        supplied = authorization.get("authorization_id", authorization.get("id"))
        if supplied != auth_id:
            raise Blocked("Dispatch authorization does not bind the run")
        grant = self.db.execute("SELECT * FROM runtime_authorizations WHERE id=?", (auth_id,)).fetchone()
        if grant is None:
            raise Blocked("Unknown frozen authorization")
        scope = authorization.get("scope", authorization.get("authorization_scope"))
        if scope is None or digest(scope) != grant["scope_hash"]:
            raise Blocked("Authorization scope mismatch")
        supplied_limit = authorization.get("project_image_calls_limit")
        if supplied_limit is None:
            supplied_limit = authorization.get("total_image_calls")
        if supplied_limit != grant["max_calls"]:
            raise Blocked("Authorization budget mismatch")
        return grant

    def registered_dispatch_authorization(self, run_id: str) -> dict:
        """Rebuild a dispatch authorization from the frozen project grant."""
        run=self._run(run_id)
        auth_id=run["authorization_id"]
        if not auth_id:
            raise Blocked("Run has no project authorization")
        grant=self.db.execute(
            "SELECT * FROM runtime_authorizations WHERE id=?",(auth_id,)
        ).fetchone()
        if grant is None:
            raise Blocked("Frozen authorization is missing")
        payload=json.loads(grant["payload"])
        if payload.get("approved") is not True or not str(payload.get("actor_id","")).strip():
            raise Blocked("Persisted authorization is not explicitly approved and attributed")
        return {"run_id":run_id,"plan_hash":run["plan_hash"],"approved":True,
                "actor_id":payload["actor_id"],"max_image_calls":run["max_calls"],
                "authorization_id":auth_id,"scope":json.loads(grant["scope"]),
                "project_image_calls_limit":grant["max_calls"]}

    def dispatch_registered(self, run_id: str, step_id: str, *, origin: str) -> dict:
        return self.dispatch(
            run_id,step_id,
            authorization=self.registered_dispatch_authorization(run_id),origin=origin,
        )

    def dispatch(self, run_id: str, step_id: str, *, authorization: dict, origin: str) -> dict:
        if not isinstance(authorization, dict):
            raise Blocked("Explicit authorization required")
        with self.txn():
            self._step_ready(run_id, step_id)
            run, step = self._run(run_id), self._step(run_id, step_id)
            if step["kind"] not in _EXTERNAL_NODES:
                raise ContractError("Not an external worker node")
            if origin not in ("native", "mock") or (
                run["mode"] == "production" and origin != "native"
            ):
                raise Blocked("Unsupported origin or mock in production")
            if authorization.get("run_id") != run_id or authorization.get("plan_hash") != run["plan_hash"]:
                raise Blocked("Authorization must bind run and frozen plan")
            if authorization.get("approved") is not True or not str(
                authorization.get("actor_id", "")
            ).strip():
                raise Blocked("Explicit, attributed authorization required")
            grant = None
            if step["kind"] == "image_generate":
                if authorization.get("max_image_calls") != run["max_calls"]:
                    raise Blocked("Authorization budget mismatch")
                if run["spent"] >= run["max_calls"]:
                    raise Blocked("Image call budget exhausted")
                grant = self._authorization_for_dispatch(run, authorization)
                if grant is not None:
                    cur = self.db.execute(
                        "UPDATE runtime_authorizations SET reserved=reserved+1,updated_at=? "
                        "WHERE id=? AND reserved < max_calls",
                        (_now(), grant["id"]),
                    )
                    if cur.rowcount != 1:
                        raise Blocked("Project/session image budget exhausted")
            plan = json.loads(run["plan"])
            definition = next(item for item in plan["steps"] if item["id"] == step_id)
            upstream: dict[str, Any] = {}
            for row in self.db.execute(
                "SELECT id,output FROM runtime_steps WHERE run_id=? AND state=? AND output IS NOT NULL",
                (run_id, "succeeded"),
            ):
                upstream[row[0]] = json.loads(row[1])
            attempt_no = self.db.execute(
                "SELECT COUNT(*) FROM runtime_attempts WHERE run_id=? AND step_id=?", (run_id, step_id)
            ).fetchone()[0] + 1
            request = {
                "run_id": run_id,
                "step_id": step_id,
                "mode": run["mode"],
                "category": "kids_shoes",
                "plan_hash": run["plan_hash"],
                "definition": definition,
                "attempt": attempt_no,
                "upstream": upstream,
            }
            request_hash = digest(request)
            job_id = "job_" + request_hash[:28]
            attempt_id = "att_" + request_hash[:32]
            self.db.execute(
                "INSERT INTO runtime_attempts(job_id,attempt_id,run_id,step_id,attempt_no,state,request,request_hash,origin,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    attempt_id,
                    run_id,
                    step_id,
                    attempt_no,
                    "waiting",
                    canonical(request),
                    request_hash,
                    origin,
                    _now(),
                    _now(),
                ),
            )
            if step["kind"] == "image_generate":
                self.db.execute("UPDATE runtime_runs SET spent=spent+1 WHERE id=?", (run_id,))
            self.db.execute(
                "UPDATE runtime_steps SET state=? WHERE run_id=? AND id=?",
                ("waiting_worker", run_id, step_id),
            )
            self._touch_run(run_id)
            self._event(run_id, "dispatched", {"job_id": job_id, "step": step_id, "origin": origin})
        return {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "request_hash": request_hash,
            "request": request,
            "origin": origin,
        }

    def _job_row(self, job_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM runtime_attempts WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ContractError("Unknown job")
        return row

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["id"] = result["job_id"]
        result["request"] = result["request"]
        return result

    def job(self, job_id: str) -> dict:
        return self._job_dict(self._job_row(job_id))

    def attempt(self, attempt_id: str) -> dict:
        row = self.db.execute("SELECT * FROM runtime_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise ContractError("Unknown attempt")
        return self._job_dict(row)

    def active_jobs(self, run_id: str) -> list[dict]:
        self._run(run_id)
        rows=self.db.execute(
            "SELECT * FROM runtime_attempts WHERE run_id=? AND state IN ('waiting','unknown') ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [self._job_dict(row) for row in rows]

    @staticmethod
    def _artifact_bytes(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, (str, Path)):
            path = Path(value)
            if path.is_symlink() or not path.is_file():
                raise Conflict("Artifact path is not a regular file")
            return path.read_bytes()
        raise Conflict("Actual artifact bytes are required")

    @classmethod
    def _check_bytes(cls, receipt: dict, artifacts: dict[str, Any]) -> None:
        if not isinstance(artifacts, dict):
            raise Conflict("Actual artifact mapping is required")
        expected = {output["name"] for output in receipt["outputs"]}
        if set(artifacts) != expected:
            raise Conflict("Receipt and actual file sets differ")
        for output in receipt["outputs"]:
            content = cls._artifact_bytes(artifacts[output["name"]])
            if sha(content) != output["sha256"] or len(content) != output["bytes"]:
                raise Conflict("Artifact hash/length mismatch")

    def accept(self, job_id: str, receipt: dict, artifacts: dict[str, Any]) -> str:
        with self.txn():
            row = self._job_row(job_id)
            run = self._run(row["run_id"])
            job = self._job_dict(row)
            validate_receipt(receipt, job, run["mode"], row["origin"])
            if row["state"] == "succeeded":
                if row["receipt"] != canonical(receipt):
                    raise Conflict("Different receipt for completed job")
                self._check_bytes(receipt, artifacts)
                return "existing"
            if row["state"] not in ("waiting", "unknown"):
                raise Blocked("Job already resolved as unsuccessful")
            self._check_bytes(receipt, artifacts)
            self.db.execute(
                "UPDATE runtime_attempts SET state=?,receipt=?,updated_at=? WHERE job_id=?",
                ("succeeded", canonical(receipt), _now(), job_id),
            )
            self.db.execute(
                "UPDATE runtime_steps SET state=?,output=? WHERE run_id=? AND id=?",
                ("succeeded", canonical(receipt), row["run_id"], row["step_id"]),
            )
            self._touch_run(row["run_id"])
            self._event(
                row["run_id"],
                "receipt_accepted",
                {"job_id": job_id, "receipt_hash": digest(receipt)},
            )
        return "accepted"

    def mark_unknown(self, job_id: str, evidence: str) -> None:
        _nonempty_text(evidence, "Evidence reference")
        with self.txn():
            row = self._job_row(job_id)
            if row["state"] not in ("waiting", "unknown"):
                raise Blocked("Cannot make resolved job unknown")
            self.db.execute(
                "UPDATE runtime_attempts SET state=?,outcome_evidence=?,updated_at=? WHERE job_id=?",
                ("unknown", evidence, _now(), job_id),
            )
            self.db.execute(
                "UPDATE runtime_steps SET state=? WHERE run_id=? AND id=?",
                ("unknown", row["run_id"], row["step_id"]),
            )
            self._touch_run(row["run_id"])
            self._event(row["run_id"], "unknown", {"job_id": job_id, "evidence": evidence})

    def prove_no_effect(
        self,
        job_id: str,
        *,
        worker_terminal: bool,
        no_effect_verified: bool,
        evidence: str,
    ) -> None:
        if worker_terminal is not True or no_effect_verified is not True or not str(evidence).strip():
            raise Blocked("Absent-now alone is insufficient to retry")
        with self.txn():
            row = self._job_row(job_id)
            if row["state"] != "unknown":
                raise Blocked("Only unknown jobs can be resolved here")
            self.db.execute(
                "UPDATE runtime_attempts SET state=?,outcome_evidence=?,updated_at=? WHERE job_id=?",
                ("failed_confirmed", evidence, _now(), job_id),
            )
            self.db.execute(
                "UPDATE runtime_steps SET state=? WHERE run_id=? AND id=?",
                ("pending", row["run_id"], row["step_id"]),
            )
            self._touch_run(row["run_id"])
            self._event(
                row["run_id"],
                "failure_confirmed",
                {"job_id": job_id, "evidence": evidence},
            )

    def record_definitive_failure(self, job_id: str, evidence: str) -> None:
        """Close a waiting attempt after a concrete, unusable worker output.

        The dispatch has already consumed its image-call reservation.  This
        transition deliberately does not refund either the run counter or the
        project authorization.  It only makes the step eligible for a new,
        separately reserved attempt.
        """
        _nonempty_text(evidence, "Definitive failure evidence")
        with self.txn():
            row = self._job_row(job_id)
            if row["state"] != "waiting":
                raise Blocked("Only a waiting job with a definite output can fail here")
            self.db.execute(
                "UPDATE runtime_attempts SET state=?,outcome_evidence=?,updated_at=? WHERE job_id=?",
                ("failed_confirmed", evidence, _now(), job_id),
            )
            self.db.execute(
                "UPDATE runtime_steps SET state=? WHERE run_id=? AND id=?",
                ("pending", row["run_id"], row["step_id"]),
            )
            self._touch_run(row["run_id"])
            self._event(
                row["run_id"],
                "definitive_failure",
                {"job_id": job_id, "evidence": evidence},
            )

    def finish_local(self, run_id: str, step_id: str, output: dict) -> None:
        if not isinstance(output, dict):
            raise ContractError("Local output must be an object")
        with self.txn():
            self._step_ready(run_id, step_id)
            step = self._step(run_id, step_id)
            if step["kind"] in (_EXTERNAL_NODES | {"human_review"}):
                raise Blocked("External/review nodes cannot use local completion")
            self.db.execute(
                "UPDATE runtime_steps SET state=?,output=? WHERE run_id=? AND id=?",
                ("succeeded", canonical(output), run_id, step_id),
            )
            self._touch_run(run_id)
            self._event(run_id, "local_complete", {"step": step_id, "output_hash": digest(output)})

    def wait_review(self, run_id: str, step_id: str) -> None:
        with self.txn():
            self._step_ready(run_id, step_id)
            if self._step(run_id, step_id)["kind"] != "human_review":
                raise ContractError("Not a review step")
            self.db.execute(
                "UPDATE runtime_steps SET state=? WHERE run_id=? AND id=?",
                ("waiting_review", run_id, step_id),
            )
            self._touch_run(run_id)
            self._event(run_id, "review_wait", {"step": step_id})

    def finish_review(self, run_id: str, step_id: str, *, validated_snapshot: dict) -> None:
        mode = self._run(run_id)["mode"]
        validate_review_snapshot(validated_snapshot, mode)
        if validated_snapshot.get("run_id") != run_id:
            raise Conflict("Wrong run review")
        with self.txn():
            step = self._step(run_id, step_id)
            if step["state"] != "waiting_review":
                raise Blocked("Run is not waiting for review")
            if step["kind"] != "human_review":
                raise ContractError("Not a review step")
            self.db.execute(
                "UPDATE runtime_steps SET state=?,output=? WHERE run_id=? AND id=?",
                ("succeeded", canonical(validated_snapshot), run_id, step_id),
            )
            self._touch_run(run_id)
            self._event(
                run_id,
                "review_complete",
                {"snapshot_hash": digest(validated_snapshot)},
            )

    def begin_write(self, operation_id: str, payload: dict) -> str:
        identifier(operation_id)
        payload_hash = digest(payload)
        with self.txn():
            old = self.db.execute("SELECT * FROM runtime_writes WHERE id=?", (operation_id,)).fetchone()
            if old:
                if old["payload_hash"] != payload_hash:
                    raise Conflict("Write id reused with different payload")
                if old["state"] == "confirmed":
                    return "existing"
                raise Blocked("Write pending/unknown; reconcile, never resend")
            self.db.execute(
                "INSERT INTO runtime_writes(id,payload_hash,state,receipt,evidence) VALUES(?,?,?,?,?)",
                (operation_id, payload_hash, "pending", None, None),
            )
        return "reserved"

    def unknown_write(self, operation_id: str) -> None:
        with self.txn():
            cur = self.db.execute(
                "UPDATE runtime_writes SET state=? WHERE id=? AND state=?",
                ("unknown", operation_id, "pending"),
            )
            if cur.rowcount != 1:
                raise Blocked("Write not pending")

    def confirm_write(
        self,
        operation_id: str,
        *,
        expected_hash: str,
        observed_hash: str,
        receipt: dict,
    ) -> str:
        if expected_hash != observed_hash or not receipt:
            raise Conflict("Read-back did not confirm intended effect")
        with self.txn():
            row = self.db.execute("SELECT * FROM runtime_writes WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise ContractError("Unjournaled write")
            if expected_hash != row["payload_hash"]:
                raise Conflict("Proof hash does not match journaled payload")
            if row["state"] == "confirmed":
                if row["receipt"] != canonical(receipt):
                    raise Conflict("Confirmed write receipt changed")
                return "existing"
            if row["state"] != "pending" and row["state"] != "unknown":
                raise Blocked("Write cannot be confirmed")
            self.db.execute(
                "UPDATE runtime_writes SET state=?,receipt=? WHERE id=?",
                ("confirmed", canonical(receipt), operation_id),
            )
        return "confirmed"

    def write_status(self, operation_id: str) -> dict | None:
        """Read one durable remote-write journal entry without changing it."""
        identifier(operation_id)
        row = self.db.execute(
            "SELECT id,payload_hash,state,receipt,evidence FROM runtime_writes WHERE id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["receipt"] = json.loads(result["receipt"]) if result.get("receipt") else None
        return result

    def confirmed_review_records(self, run_id: str) -> dict[str, dict]:
        """Return Feishu review rows proven by this Runtime's write journal."""
        self._run(run_id)
        result: dict[str, dict] = {}
        rows = self.db.execute(
            "SELECT receipt FROM runtime_writes WHERE state='confirmed' AND receipt IS NOT NULL"
        ).fetchall()
        for row in rows:
            try:
                receipt = json.loads(row["receipt"])
            except (TypeError, ValueError):
                continue
            if (
                isinstance(receipt, dict)
                and receipt.get("kind") == "human_review"
                and receipt.get("run_id") == run_id
                and isinstance(receipt.get("record_id"), str)
            ):
                result[receipt["record_id"]] = receipt
        return result

    def audit(self, run_id: str, event: str, detail: dict) -> int:
        """Append a human-operation audit without creating a status projection."""
        self._run(run_id)
        _nonempty_text(event, "Audit event")
        detail_text = canonical(detail)
        with self.txn():
            cur = self.db.execute(
                "INSERT INTO runtime_events(run_id,event,detail,created_at) VALUES(?,?,?,?)",
                (run_id, event, detail_text, _now()),
            )
        return int(cur.lastrowid)

    def pending_outbox(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM runtime_outbox WHERE state IN ('pending','claimed','unknown') ORDER BY rowid"
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            result.append(value)
        return result

    def claim_outbox(self, owner: str) -> dict | None:
        _nonempty_text(owner, "Outbox owner")
        with self.txn():
            row = self.db.execute(
                "SELECT op_id FROM runtime_outbox WHERE state='pending' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = self.db.execute(
                "UPDATE runtime_outbox SET state='claimed',claimed_by=?,updated_at=? "
                "WHERE op_id=? AND state='pending'",
                (owner, _now(), row["op_id"]),
            )
            if cur.rowcount != 1:
                return None
            claimed = self.db.execute("SELECT * FROM runtime_outbox WHERE op_id=?", (row["op_id"],)).fetchone()
            result = dict(claimed)
            result["payload"] = json.loads(result["payload"])
            return result

    def confirm_outbox(
        self,
        operation_id: str,
        *,
        expected_hash: str,
        observed_hash: str,
        receipt: dict,
    ) -> str:
        if expected_hash != observed_hash or not receipt:
            raise Conflict("Outbox read-back did not confirm intended effect")
        with self.txn():
            row = self.db.execute("SELECT * FROM runtime_outbox WHERE op_id=?", (operation_id,)).fetchone()
            if row is None:
                raise ContractError("Unknown outbox operation")
            if row["payload_hash"] != expected_hash:
                raise Conflict("Outbox proof hash does not match journaled payload")
            if row["state"] == "confirmed":
                if row["receipt"] != canonical(receipt):
                    raise Conflict("Confirmed outbox receipt changed")
                return "existing"
            if row["state"] not in ("pending", "claimed"):
                raise Blocked("Outbox operation is unresolved; reconcile before confirming")
            self.db.execute(
                "UPDATE runtime_outbox SET state='confirmed',receipt=?,updated_at=? WHERE op_id=?",
                (canonical(receipt), _now(), operation_id),
            )
        return "confirmed"

    def unknown_outbox(self, operation_id: str, evidence: str) -> None:
        _nonempty_text(evidence, "Outbox evidence")
        with self.txn():
            cur = self.db.execute(
                "UPDATE runtime_outbox SET state='unknown',evidence=?,updated_at=? "
                "WHERE op_id=? AND state IN ('pending','claimed')",
                (evidence, _now(), operation_id),
            )
            if cur.rowcount != 1:
                raise Blocked("Outbox operation is not pending")

    def enqueue_status(self, run_id: str, payload: dict) -> str:
        """Journal an explicit status projection without performing a remote write."""
        self._run(run_id)
        payload_hash = digest(payload)
        op_id = "status_manual_" + run_id + "_" + payload_hash[:24]
        with self.txn():
            old = self.db.execute("SELECT * FROM runtime_outbox WHERE op_id=?", (op_id,)).fetchone()
            if old:
                if old["payload_hash"] != payload_hash:
                    raise Conflict("Status operation id reused with a different payload")
                return op_id
            self.db.execute(
                "INSERT INTO runtime_outbox(op_id,run_id,kind,payload,payload_hash,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (op_id, run_id, "status_projection", canonical(payload), payload_hash, "pending", _now(), _now()),
            )
        return op_id

    def status(self, run_id: str) -> dict:
        run = self._run(run_id)
        result = {
            "run_id": run_id,
            "mode": run["mode"],
            "state": run["state"],
            "next": self.next(run_id),
            "image_calls_reserved": run["spent"],
            "image_calls_max": run["max_calls"],
            "unknown_jobs": [
                row[0]
                for row in self.db.execute(
                    "SELECT job_id FROM runtime_attempts WHERE run_id=? AND state=?", (run_id, "unknown")
                )
            ],
            "pending_outbox": self.db.execute(
                "SELECT COUNT(*) FROM runtime_outbox WHERE run_id=? AND state!='confirmed'", (run_id,)
            ).fetchone()[0],
        }
        if run["authorization_id"]:
            result["authorization"] = self.authorization_status(run["authorization_id"])
        return result


__all__ = [
    "Blocked",
    "Conflict",
    "ContractError",
    "Runtime",
    "canonical",
    "checked_plan",
    "digest",
    "identifier",
    "sha",
    "validate_receipt",
    "validate_review_snapshot",
]
