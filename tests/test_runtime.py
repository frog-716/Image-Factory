import hashlib
import tempfile
import unittest
from pathlib import Path

from factory.runtime import (
    Blocked,
    Conflict,
    ContractError,
    Runtime,
    canonical,
    digest,
)
from factory.state import State


def plan(run_id="RUN-001", mode="demo", budget=2):
    return {
        "schema_version": 1,
        "category": "kids_shoes",
        "run_id": run_id,
        "mode": mode,
        "workflow_version": "wf-v1",
        "max_image_calls": budget,
        "steps": [
            {"id": "brief", "kind": "codex_brief", "depends_on": []},
            {"id": "image", "kind": "image_generate", "depends_on": ["brief"]},
            {"id": "review", "kind": "human_review", "depends_on": ["image"]},
            {"id": "export", "kind": "export", "depends_on": ["review"]},
        ],
    }


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.sqlite3"
        self.runtime = Runtime(self.db)
        self.p = plan()
        self.runtime.create(self.p)
        self.auth = {
            "run_id": self.p["run_id"],
            "plan_hash": digest(self.p),
            "approved": True,
            "actor_id": "human-1",
            "max_image_calls": self.p["max_image_calls"],
        }

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def _brief(self):
        job = self.runtime.dispatch(
            self.p["run_id"], "brief", authorization=self.auth, origin="mock"
        )
        receipt, files = self._receipt(job, b"brief-bytes")
        self.runtime.accept(job["job_id"], receipt, files)

    def _dispatch(self):
        self._brief()
        return self.runtime.dispatch(
            self.p["run_id"], "image", authorization=self.auth, origin="mock"
        )

    def _receipt(self, job, content=b"image-bytes"):
        return (
            {
                "job_id": job["job_id"],
                "attempt_id": job["attempt_id"],
                "request_hash": job["request_hash"],
                "mode": "demo",
                "status": "succeeded",
                "origin": "mock",
                "tool": "test-tool",
                "model_reported": "not_reported",
                "evidence_ref": "evidence.txt",
                "outputs": [
                    {
                        "name": "image.png",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "bytes": len(content),
                    }
                ],
            },
            {"image.png": content},
        )

    def test_plan_is_frozen_and_rejects_untrusted_nodes(self):
        modified = dict(self.p)
        modified["workflow_version"] = "wf-v2"
        with self.assertRaises(Conflict):
            self.runtime.create(modified)
        bad = dict(self.p)
        bad["run_id"] = "RUN-002"
        bad["steps"] = [{"id": "bad", "kind": "shell", "depends_on": []}]
        with self.assertRaises(ContractError):
            Runtime(self.db).create(bad)

    def test_budget_is_reserved_before_dispatch_and_duplicate_claim_is_atomic(self):
        self._brief()
        job = self.runtime.dispatch(
            self.p["run_id"], "image", authorization=self.auth, origin="mock"
        )
        self.assertEqual(self.runtime.status(self.p["run_id"])["image_calls_reserved"], 1)
        with self.assertRaises(Blocked):
            self.runtime.dispatch(
                self.p["run_id"], "image", authorization=self.auth, origin="mock"
            )
        other = Runtime(self.db)
        try:
            with self.assertRaises(Blocked):
                other.dispatch(
                    self.p["run_id"], "image", authorization=self.auth, origin="mock"
                )
        finally:
            other.close()
        self.assertEqual(self.runtime.job(job["job_id"])["state"], "waiting")

    def test_receipt_requires_request_hash_and_real_bytes(self):
        job = self._dispatch()
        receipt, files = self._receipt(job)
        bad = dict(receipt)
        bad["request_hash"] = "bad"
        with self.assertRaises(Conflict):
            self.runtime.accept(job["job_id"], bad, files)
        with self.assertRaises(Conflict):
            self.runtime.accept(job["job_id"], receipt, {"image.png": b"tampered"})
        self.assertEqual(self.runtime.accept(job["job_id"], receipt, files), "accepted")
        self.assertEqual(self.runtime.accept(job["job_id"], receipt, files), "existing")

    def test_unknown_never_retries_without_definitive_no_effect(self):
        job = self._dispatch()
        self.runtime.mark_unknown(job["job_id"], "timeout evidence")
        with self.assertRaises(Blocked):
            self.runtime.dispatch(
                self.p["run_id"], "image", authorization=self.auth, origin="mock"
            )
        with self.assertRaises(Blocked):
            self.runtime.prove_no_effect(
                job["job_id"], worker_terminal=False, no_effect_verified=True, evidence="empty GET"
            )
        self.runtime.prove_no_effect(
            job["job_id"], worker_terminal=True, no_effect_verified=True, evidence="terminal proof"
        )
        retry = self.runtime.dispatch(
            self.p["run_id"], "image", authorization=self.auth, origin="mock"
        )
        self.assertNotEqual(job["attempt_id"], retry["attempt_id"])
        self.assertEqual(self.runtime.status(self.p["run_id"])["image_calls_reserved"], 2)

    def test_definitive_bad_output_keeps_spent_budget_and_requires_new_attempt(self):
        job = self._dispatch()
        self.runtime.record_definitive_failure(job["job_id"], "decoded output violates alpha contract")
        self.assertEqual(self.runtime.status(self.p["run_id"])["image_calls_reserved"], 1)
        retry = self.runtime.dispatch(
            self.p["run_id"], "image", authorization=self.auth, origin="mock"
        )
        self.assertNotEqual(job["attempt_id"], retry["attempt_id"])
        self.assertEqual(self.runtime.status(self.p["run_id"])["image_calls_reserved"], 2)

    def test_review_wait_finish_requires_trusted_complete_snapshot(self):
        job = self._dispatch()
        receipt, files = self._receipt(job)
        self.runtime.accept(job["job_id"], receipt, files)
        self.runtime.wait_review(self.p["run_id"], "review")
        snapshot = {
            "run_id": self.p["run_id"],
            "mode": "demo",
            "candidates": [{"asset_id": "A", "sha256": "a" * 64}],
            "events": [
                {
                    "asset_id": "A",
                    "revision": 1,
                    "run_id": self.p["run_id"],
                    "mode": "demo",
                    "sha256": "a" * 64,
                    "decision": "approve",
                    "actor_id": "reviewer",
                    "human_confirmed": True,
                    "reason": "looks good",
                    "demo_visual_ok": True,
                    "demo_use_only": True,
                }
            ],
        }
        self.runtime.finish_review(self.p["run_id"], "review", validated_snapshot=snapshot)
        self.assertEqual(self.runtime.next(self.p["run_id"])["step"], "export")

    def test_outbox_is_durable_and_restart_keeps_waiting_worker(self):
        job = self._dispatch()
        pending = self.runtime.pending_outbox()
        self.assertTrue(pending)
        self.runtime.close()
        self.runtime = Runtime(self.db)
        self.assertEqual(self.runtime.next(self.p["run_id"])["state"], "waiting_worker")
        self.assertEqual(self.runtime.job(job["job_id"])["state"], "waiting")
        self.assertTrue(self.runtime.pending_outbox())

    def test_authorization_budget_is_shared_across_runs_and_survives_restart(self):
        self.runtime.close()
        grant = {
            "authorization_id": "AUTH-6",
            "scope": {"project_id": "P1", "session_id": "S1"},
            "project_image_calls_limit": 6,
            "approved": True,
            "actor_id": "human-1",
        }
        self.runtime = Runtime(self.db, authorization=grant)
        for index in range(6):
            p = plan(run_id=f"RUN-{index + 10}", budget=1)
            p["authorization_id"] = grant["authorization_id"]
            p["authorization_scope"] = grant["scope"]
            self.runtime.create(p)
            auth = {
                "run_id": p["run_id"],
                "plan_hash": digest(p),
                "approved": True,
                "actor_id": "human-1",
                "max_image_calls": 1,
                "authorization_id": grant["authorization_id"],
                "scope": grant["scope"],
                "project_image_calls_limit": 6,
            }
            brief_job = self.runtime.dispatch(p["run_id"], "brief", authorization=auth, origin="mock")
            brief_receipt, brief_files = self._receipt(brief_job, b"brief-bytes")
            self.runtime.accept(brief_job["job_id"], brief_receipt, brief_files)
            job = self.runtime.dispatch(p["run_id"], "image", authorization=auth, origin="mock")
            self.runtime.mark_unknown(job["job_id"], "unknown attempt")
        p7 = plan(run_id="RUN-016", budget=1)
        p7["authorization_id"] = grant["authorization_id"]
        p7["authorization_scope"] = grant["scope"]
        self.runtime.create(p7)
        auth7 = {
            "run_id": p7["run_id"],
            "plan_hash": digest(p7),
            "approved": True,
            "actor_id": "human-1",
            "max_image_calls": 1,
            "authorization_id": grant["authorization_id"],
            "scope": grant["scope"],
            "project_image_calls_limit": 6,
        }
        brief_job = self.runtime.dispatch(p7["run_id"], "brief", authorization=auth7, origin="mock")
        brief_receipt, brief_files = self._receipt(brief_job, b"brief-bytes")
        self.runtime.accept(brief_job["job_id"], brief_receipt, brief_files)
        with self.assertRaises(Blocked):
            self.runtime.dispatch(p7["run_id"], "image", authorization=auth7, origin="mock")
        with self.assertRaises(Blocked):
            self.runtime.create(plan(run_id="RUN-BYPASS", budget=1))
        self.runtime.close()
        self.runtime = Runtime(self.db, authorization=grant)
        self.assertEqual(self.runtime.authorization_status("AUTH-6")["reserved"], 6)

    def test_runtime_tables_coexist_with_legacy_state_in_same_ledger(self):
        self.runtime.close()
        shared = Path(self.temp.name) / "ledger.sqlite3"
        legacy = State(shared.parent)
        try:
            legacy.save({"id": "legacy_run", "task_id": "legacy_task"})
        finally:
            legacy.close()
        self.runtime = Runtime(shared)
        self.runtime.create(plan(run_id="V1-RUN"))
        self.assertEqual(self.runtime.status("V1-RUN")["state"], "queued")
        legacy = State(shared.parent)
        try:
            self.assertEqual(legacy.run("legacy_run")["task_id"], "legacy_task")
        finally:
            legacy.close()


if __name__ == "__main__":
    unittest.main()
