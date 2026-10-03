import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory import runtime_runner
from factory.cli import execute, parser
from factory.runtime import Runtime
from factory.runtime_runner import runtime_db


ROOT = Path(__file__).resolve().parents[1]
SOURCE_IMAGE = ROOT / "var/validation/topstar-5056-feishu-roundtrip/rustans-hero.jpg"


@unittest.skipUnless(SOURCE_IMAGE.is_file(), "verified local TOPSTAR source fixture is unavailable")
class TopstarSourceInitCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.config = self.root / "config.json"
        self.config.write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def command(self, run_id="TOPSTAR-SOURCE-5056-20260927-001",
                authorization_id="AUTH-TOPSTAR-5056-BEIGE-20260927-6",
                actor_id="human-explicit-topstar-5056-20260927",
                reference_image=SOURCE_IMAGE):
        return parser().parse_args([
            "--config", str(self.config),
            "--state", str(self.state),
            "runtime-init-topstar-source",
            "--run", run_id,
            "--authorization-id", authorization_id,
            "--actor-id", actor_id,
            "--reference-image", str(reference_image),
        ])

    def test_cli_freezes_one_product_source_job_without_reserving_a_call(self):
        result = execute(self.command())

        self.assertEqual(result["result"], "created")
        self.assertEqual(result["run"]["image_calls_max"], 1)
        self.assertEqual(result["run"]["image_calls_reserved"], 0)
        self.assertEqual(result["run"]["authorization"], {
            "authorization_id": "AUTH-TOPSTAR-5056-BEIGE-20260927-6",
            "scope": {
                "namespace": "V1-DEMO-KIDS",
                "category": "kids_shoes",
                "mode": "demo",
                "product_record_id": "recvwpu2KAjcoi",
                "grant": "user-confirmed-topstar-5056-20260927",
            },
            "max_calls": 6,
            "reserved": 0,
            "remaining": 6,
        })
        self.assertFalse((self.state / "active-v1-run.json").exists())
        self.assertTrue(Path(result["audit_file"]).is_file())

        runtime = Runtime(runtime_db(self.state), single_instance=True)
        try:
            plan = runtime.plan("TOPSTAR-SOURCE-5056-20260927-001")
        finally:
            runtime.close()
        self.assertEqual(plan["max_image_calls"], 1)
        self.assertEqual(len(plan["steps"]), 1)
        step = plan["steps"][0]
        self.assertEqual(step["kind"], "image_generate")
        self.assertEqual(step["output_role"], "product_rgba")
        self.assertEqual(step["reference_asset"]["product_record_id"], "recvwpu2KAjcoi")
        self.assertEqual(step["reference_asset"]["asset_id"], "recvwpuAQYH9XZ")
        self.assertEqual(
            step["reference_asset"]["sha256"],
            "13121c27e357b425cae73d25706509ebe59fb59790da6ac1481e49eeb845907b",
        )
        frozen_source = self.state / step["reference_asset"]["relative_path"]
        self.assertEqual(frozen_source.read_bytes(), SOURCE_IMAGE.read_bytes())
        audit = json.loads(Path(result["audit_file"]).read_text(encoding="utf-8"))
        self.assertEqual(audit["status"], "ready_not_dispatched")
        self.assertEqual(audit["plan_hash"], result["plan_hash"])
        self.assertEqual(audit["authorization"]["authorization_id"],
                         "AUTH-TOPSTAR-5056-BEIGE-20260927-6")
        self.assertEqual(audit["reference_image"]["snapshot_sha256"],
                         "13121c27e357b425cae73d25706509ebe59fb59790da6ac1481e49eeb845907b")
        self.assertFalse(audit["identity_crosscheck"]["tool_input"])

    def test_rejects_changed_source_without_creating_a_run_or_snapshot(self):
        from factory.runtime import Blocked

        changed = self.root / "changed-source.jpg"
        changed.write_bytes(SOURCE_IMAGE.read_bytes() + b"\x00")
        with self.assertRaises(Blocked):
            execute(self.command(reference_image=changed))

        self.assertFalse((self.state / "ledger.sqlite3").exists())
        self.assertFalse((self.state / "references").exists())

    def test_rejects_non_topstar_run_ids_before_initializing(self):
        from factory.runtime import Blocked

        with self.assertRaises(Blocked):
            execute(self.command(run_id="FICTIONAL-ASSET-20260927-001"))
        self.assertFalse((self.state / "ledger.sqlite3").exists())

    def test_repeated_initialization_is_idempotent_and_keeps_audit_immutable(self):
        first = execute(self.command())
        audit_before = Path(first["audit_file"]).read_bytes()

        second = execute(self.command())

        self.assertEqual(first["result"], "created")
        self.assertEqual(second["result"], "existing")
        self.assertEqual(Path(second["audit_file"]).read_bytes(), audit_before)
        self.assertEqual(second["run"]["authorization"]["reserved"], 0)

    def test_existing_authorization_actor_cannot_be_rewritten_in_pre_dispatch_audit(self):
        from factory.runtime import Conflict

        authorization = {
            "authorization_id": "AUTH-TOPSTAR-5056-BEIGE-20260927-6",
            "scope": {
                "namespace": "V1-DEMO-KIDS",
                "category": "kids_shoes",
                "mode": "demo",
                "product_record_id": "recvwpu2KAjcoi",
                "grant": "user-confirmed-topstar-5056-20260927",
            },
            "project_image_calls_limit": 6,
            "approved": True,
            "actor_id": "original-explicit-actor",
            "source": "user-confirmed-topstar-5056-20260927",
        }
        runtime = Runtime(runtime_db(self.state), authorization=authorization,
                          single_instance=True)
        try:
            runtime.register_authorization(authorization)
        finally:
            runtime.close()

        with self.assertRaises(Conflict):
            execute(self.command(actor_id="different-explicit-actor"))

        audit = self.state / "audit/TOPSTAR-SOURCE-5056-20260927-001-pre-dispatch.json"
        self.assertFalse(audit.exists())

    def test_post_copy_source_change_does_not_delete_preexisting_snapshot(self):
        from factory.runtime import Blocked

        snapshot = self.state / "references/TOPSTAR-SOURCE-5056-20260927-001-source.jpg"
        snapshot.parent.mkdir(parents=True)
        original_bytes = SOURCE_IMAGE.read_bytes()
        snapshot.write_bytes(original_bytes)
        original_file_hash = runtime_runner.file_hash

        def changed_source_hash(path):
            if Path(path) == SOURCE_IMAGE:
                return "0" * 64
            return original_file_hash(path)

        with mock.patch("factory.runtime_runner.file_hash", side_effect=changed_source_hash):
            with self.assertRaises(Blocked):
                execute(self.command())

        self.assertTrue(snapshot.is_file())
        self.assertEqual(snapshot.read_bytes(), original_bytes)

    def test_new_source_and_followup_run_share_one_six_call_authorization(self):
        from factory.runtime import Runtime

        result = execute(self.command())
        self.assertEqual(result["run"]["authorization"]["reserved"], 0)

        dispatch_args = parser().parse_args([
            "--config", str(self.config), "--state", str(self.state),
            "runtime-dispatch-next", "--run", "TOPSTAR-SOURCE-5056-20260927-001",
        ])
        dispatched = execute(dispatch_args)
        self.assertEqual(dispatched["budget"]["reserved"], 1)
        self.assertEqual(dispatched["budget"]["remaining"], 5)

        scope = result["run"]["authorization"]["scope"]
        authorization = {
            "authorization_id": "AUTH-TOPSTAR-5056-BEIGE-20260927-6",
            "scope": scope,
            "project_image_calls_limit": 6,
            "approved": True,
            "actor_id": "human-explicit-topstar-5056-20260927",
            "source": "user-confirmed-topstar-5056-20260927",
        }
        followup_plan = {
            "schema_version": 1,
            "category": "kids_shoes",
            "run_id": "V1-DEMO-KIDS-FORM-SIM-001",
            "mode": "demo",
            "workflow_version": "future-form-test-v1",
            "max_image_calls": 1,
            "authorization_id": authorization["authorization_id"],
            "authorization_scope": scope,
            "steps": [{
                "id": "background-test",
                "kind": "image_generate",
                "depends_on": [],
                "output_role": "background",
                "independent_image_count": 1,
                "visual_prompt": "Offline test only.",
            }],
        }
        runtime = Runtime(runtime_db(self.state), authorization=authorization,
                          single_instance=True)
        try:
            runtime.create(followup_plan, authorization=authorization)
            before_followup_dispatch = runtime.status(followup_plan["run_id"])["authorization"]
            self.assertEqual(before_followup_dispatch["reserved"], 1)
            self.assertEqual(before_followup_dispatch["remaining"], 5)
            runtime.dispatch_registered(followup_plan["run_id"], "background-test", origin="native")
            after_followup_dispatch = runtime.authorization_status(authorization["authorization_id"])
        finally:
            runtime.close()
        self.assertEqual(after_followup_dispatch["reserved"], 2)
        self.assertEqual(after_followup_dispatch["remaining"], 4)

    def test_topstar_grant_does_not_reuse_nearly_spent_old_v1_authorization(self):
        from factory.runtime import Runtime

        old_scope = {
            "namespace": "V1-DEMO-KIDS",
            "category": "kids_shoes",
            "mode": "demo",
            "grant": "user-launch-2026-09-21",
        }
        old_authorization = {
            "authorization_id": "V1-DEMO-KIDS-20260921",
            "scope": old_scope,
            "project_image_calls_limit": 6,
            "approved": True,
            "actor_id": "old-demo-actor",
            "source": "historical V1 test grant",
        }
        runtime = Runtime(runtime_db(self.state), authorization=old_authorization,
                          single_instance=True)
        try:
            for index in range(5):
                run_id = f"V1-DEMO-KIDS-OLD-{index + 1}"
                plan = {
                    "schema_version": 1,
                    "category": "kids_shoes",
                    "run_id": run_id,
                    "mode": "demo",
                    "workflow_version": "old-v1-test-v1",
                    "max_image_calls": 1,
                    "authorization_id": old_authorization["authorization_id"],
                    "authorization_scope": old_scope,
                    "steps": [{"id": "image", "kind": "image_generate", "depends_on": []}],
                }
                runtime.create(plan, authorization=old_authorization)
                runtime.dispatch_registered(run_id, "image", origin="native")
            self.assertEqual(runtime.authorization_status(old_authorization["authorization_id"])["reserved"], 5)
        finally:
            runtime.close()

        result = execute(self.command())
        self.assertEqual(result["run"]["authorization"]["authorization_id"],
                         "AUTH-TOPSTAR-5056-BEIGE-20260927-6")
        self.assertEqual(result["run"]["authorization"]["reserved"], 0)

        dispatch_args = parser().parse_args([
            "--config", str(self.config), "--state", str(self.state),
            "runtime-dispatch-next", "--run", "TOPSTAR-SOURCE-5056-20260927-001",
        ])
        execute(dispatch_args)
        runtime = Runtime(runtime_db(self.state), single_instance=True)
        try:
            self.assertEqual(runtime.authorization_status(old_authorization["authorization_id"])["reserved"], 5)
            self.assertEqual(runtime.authorization_status(
                "AUTH-TOPSTAR-5056-BEIGE-20260927-6")["reserved"], 1)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
