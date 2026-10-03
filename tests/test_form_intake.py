import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from support import Fixture
from factory.util import FactoryError, file_hash, read_json


class FormIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(self.temp.name)
        self.fixture.base.patch("workflows", self.fixture.flow, {"示例数据": True})
        self.fixture.config["intake_profile"] = {
            "allowed_product_ids": [self.fixture.product],
            "workflow_id": self.fixture.flow,
            "channel": "DEMO",
            "placement": "演示审图台",
            "namespace": "V1-DEMO-KIDS-INTAKE-OFFLINE",
            "mode": "demo",
            "review_policy_version": "demo-v2",
            "max_calls": 6,
        }
        self.form_record = self.fixture.base.create("tasks", {
            "选择商品": ["测试鞋"],
            "图片用途": "场景展示",
            "消费场景": "室内",
            "视觉风格": "清爽",
            "数量": 3,
            "附加要求": "保持鞋面结构",
            "创建确认": "确认创建演示任务",
        })["record_id"]

    def tearDown(self):
        self.fixture.close()
        self.temp.cleanup()

    def offline_grant(self):
        self.fixture.config["intake_profile"]["namespace"] = "V1-DEMO-KIDS"
        return {
            "authorization_id": "AUTH-FORM-OFFLINE-6",
            "scope": {"namespace": "V1-DEMO-KIDS", "category": "kids_shoes",
                      "mode": "demo", "grant": "offline-form-test",
                      "product_record_id": self.fixture.product},
            "project_image_calls_limit": 6,
            "approved": True,
            "actor_id": "ou_demo",
            "source": "offline test authorization, not a live grant",
        }

    def second_form(self):
        return self.fixture.base.create("tasks", {
            "选择商品": ["测试鞋"],
            "图片用途": "场景展示",
            "消费场景": "室内",
            "视觉风格": "清爽",
            "数量": 3,
            "附加要求": "保持鞋面结构",
            "创建确认": "确认创建演示任务",
        })["record_id"]

    def set_runtime_state(self, run_id, state):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            if state == "completed":
                runtime.db.execute(
                    "UPDATE runtime_steps SET state='succeeded', output='{}' WHERE run_id=?",
                    (run_id,),
                )
            runtime.db.execute(
                "UPDATE runtime_runs SET state=? WHERE id=?", (state, run_id)
            )
            runtime.db.commit()
        finally:
            runtime.close()

    def test_six_business_fields_can_be_previewed_without_writes_or_dispatch(self):
        original = self.fixture.base.list("tasks")
        result = self.fixture.engine.preview_form_record(self.form_record)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["product_id"], self.fixture.product)
        self.assertEqual(result["flow_id"], self.fixture.flow)
        self.assertEqual(self.fixture.base.list("tasks"), original)
        self.assertIsNone(self.fixture.state.by_task(self.form_record))

    def test_preview_download_uses_larkcli_allowed_temporary_root(self):
        downloaded = []
        original = self.fixture.base.download_attachment

        def capture_download(table, record_id, attachment, destination):
            downloaded.append(Path(destination).resolve())
            return original(table, record_id, attachment, destination)

        with patch.object(
            self.fixture.base, "download_attachment", side_effect=capture_download,
        ):
            self.fixture.engine.preview_form_record(self.form_record)

        allowed_root = Path("/tmp").resolve()
        self.assertTrue(downloaded)
        self.assertTrue(
            all(path.is_relative_to(allowed_root) for path in downloaded),
            f"Feishu CLI only allows temporary downloads under /tmp; got {downloaded}",
        )

    def test_single_choice_confirmation_list_is_accepted(self):
        self.fixture.base.patch(
            "tasks", self.form_record, {"创建确认": ["确认创建演示任务"]},
        )

        accepted = self.fixture.engine.admit_form_record(
            self.form_record, self.offline_grant(),
        )

        self.assertEqual(accepted["task_record_id"], self.form_record)
        self.assertEqual(
            self.fixture.base.get_record("tasks", self.form_record)["fields"]["提交"],
            True,
        )

    def test_unsubmitted_complete_form_cannot_be_admitted(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        self.fixture.base.patch("tasks", self.form_record, {"创建确认": "暂不创建"})
        before = self.fixture.base.get_record("tasks", self.form_record)

        with self.assertRaisesRegex(FactoryError, "确认创建"):
            self.fixture.engine.admit_form_record(self.form_record, self.offline_grant())

        self.assertEqual(self.fixture.base.get_record("tasks", self.form_record), before)
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 0)
        finally:
            runtime.close()

    def test_only_exact_confirmation_choice_is_an_admission_signal(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        for value in (1, "true", ["其他选项"], ["确认创建演示任务", "其他选项"], True):
            with self.subTest(value=value):
                self.fixture.base.patch("tasks", self.form_record, {"创建确认": value})
                with self.assertRaisesRegex(FactoryError, "确认创建"):
                    self.fixture.engine.admit_form_record(
                        self.form_record, self.offline_grant(),
                    )
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 0)
        finally:
            runtime.close()

    def test_authorized_form_becomes_one_queued_v1_runtime_without_spending_calls(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        grant = self.offline_grant()
        original_count = len(self.fixture.base.list("tasks"))

        accepted = self.fixture.engine.admit_form_record(self.form_record, grant)

        run_id = accepted["run_id"]
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            status = runtime.status(run_id)
            plan = runtime.plan(run_id)
        finally:
            runtime.close()
        task = self.fixture.base.get_record("tasks", self.form_record)["fields"]
        self.assertEqual(status["state"], "queued")
        self.assertEqual(status["image_calls_reserved"], 0)
        self.assertEqual(status["next"]["step"], "product-source")
        self.assertEqual(plan["steps"][0]["operation"], "import_product_source")
        self.assertEqual(task["运行ID"], run_id)
        self.assertEqual(task["商品"], [self.fixture.product])
        self.assertEqual(task["流程"], [self.fixture.flow])
        self.assertEqual(task["提交"], True)
        self.assertEqual(len(self.fixture.base.list("tasks")), original_count)

    def test_identical_form_retry_reconciles_one_runtime_and_one_task(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        grant = self.offline_grant()
        first = self.fixture.engine.admit_form_record(self.form_record, grant)
        task_after_first = self.fixture.base.get_record("tasks", self.form_record)

        second = self.fixture.engine.admit_form_record(self.form_record, grant)

        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(self.fixture.base.get_record("tasks", self.form_record), task_after_first)
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 1)
            self.assertEqual(runtime.status(first["run_id"])["image_calls_reserved"], 0)
        finally:
            runtime.close()

    def test_completed_run_allows_a_second_form_with_a_new_grant(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        first_grant = self.offline_grant()
        first = self.fixture.engine.admit_form_record(self.form_record, first_grant)
        self.set_runtime_state(first["run_id"], "completed")
        second_record = self.second_form()
        second_grant = self.offline_grant()
        second_grant["authorization_id"] = "AUTH-FORM-OFFLINE-SECOND"

        second = self.fixture.engine.admit_form_record(second_record, second_grant)

        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertEqual(
            self.fixture.base.get_record("tasks", self.form_record)["fields"]["运行ID"],
            first["run_id"],
        )
        self.assertEqual(
            self.fixture.base.get_record("tasks", second_record)["fields"]["运行ID"],
            second["run_id"],
        )
        self.assertEqual(
            read_json(self.fixture.state.root / "active-v1-run.json"),
            {"run_id": second["run_id"], "authorization_id": second_grant["authorization_id"]},
        )
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            self.assertEqual(runtime.status(first["run_id"])["state"], "completed")
            self.assertEqual(runtime.status(second["run_id"])["state"], "queued")
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 2)
        finally:
            runtime.close()

    def test_queued_running_and_review_waiting_run_block_a_second_form(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        first = self.fixture.engine.admit_form_record(self.form_record, self.offline_grant())
        second_record = self.second_form()
        second_grant = self.offline_grant()
        second_grant["authorization_id"] = "AUTH-FORM-OFFLINE-SECOND"

        for state in ("queued", "running", "waiting_review"):
            with self.subTest(state=state):
                self.set_runtime_state(first["run_id"], state)
                with self.assertRaises(FactoryError):
                    self.fixture.engine.admit_form_record(second_record, second_grant)
                runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
                try:
                    self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 1)
                finally:
                    runtime.close()

    def test_uncertain_pointer_write_blocks_new_run_without_retrying_the_write(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db
        from factory.util import write_json as actual_write_json

        grant = self.offline_grant()
        pointer_path = self.fixture.state.root / "active-v1-run.json"
        attempted_writes = []

        def fail_before_pointer_write(path, value):
            if path == pointer_path:
                attempted_writes.append(value)
                raise TimeoutError("pointer write outcome unknown")
            return actual_write_json(path, value)

        with patch("factory.engine.write_json", side_effect=fail_before_pointer_write):
            with self.assertRaises(TimeoutError):
                self.fixture.engine.admit_form_record(self.form_record, grant)

        self.assertFalse(pointer_path.exists())
        self.assertEqual(len(attempted_writes), 1)
        second_record = self.second_form()
        second_grant = self.offline_grant()
        second_grant["authorization_id"] = "AUTH-FORM-OFFLINE-SECOND"
        with self.assertRaises(FactoryError):
            self.fixture.engine.admit_form_record(second_record, second_grant)

        self.assertFalse(pointer_path.exists())
        self.assertEqual(len(attempted_writes), 1)
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 1)
        finally:
            runtime.close()

    def test_runtime_created_before_pointer_journal_is_recovered_without_orphaning(self):
        grant = self.offline_grant()
        effect = self.fixture.state.effect

        def stop_before_pointer_journal(key, find, write):
            if key.startswith("form-intake:active-v1-run:"):
                raise TimeoutError("process stopped before pointer effect journal")
            return effect(key, find, write)

        with patch.object(self.fixture.state, "effect", side_effect=stop_before_pointer_journal):
            with self.assertRaises(TimeoutError):
                self.fixture.engine.admit_form_record(self.form_record, grant)

        pointer_path = self.fixture.state.root / "active-v1-run.json"
        self.assertFalse(pointer_path.exists())
        second = self.second_form()
        second_grant = self.offline_grant()
        second_grant["authorization_id"] = "AUTH-FORM-OFFLINE-SECOND"
        with self.assertRaises(FactoryError):
            self.fixture.engine.admit_form_record(second, second_grant)

        recovered = self.fixture.engine.admit_form_record(self.form_record, grant)
        self.assertEqual(read_json(pointer_path)["run_id"], recovered["run_id"])
        self.assertEqual(
            self.fixture.base.get_record("tasks", self.form_record)["fields"]["运行ID"],
            recovered["run_id"],
        )

    def test_applied_but_unconfirmed_pointer_write_is_reconciled_by_readback(self):
        from factory.util import write_json as actual_write_json

        grant = self.offline_grant()
        pointer_path = self.fixture.state.root / "active-v1-run.json"
        attempted_writes = []

        def apply_then_timeout(path, value):
            result = actual_write_json(path, value)
            if path == pointer_path:
                attempted_writes.append(value)
                raise TimeoutError("pointer write reply lost")
            return result

        with patch("factory.engine.write_json", side_effect=apply_then_timeout):
            with self.assertRaises(TimeoutError):
                self.fixture.engine.admit_form_record(self.form_record, grant)

        pointer = read_json(pointer_path)
        self.assertEqual(len(attempted_writes), 1)
        accepted = self.fixture.engine.admit_form_record(self.form_record, grant)

        self.assertEqual(accepted["run_id"], pointer["run_id"])
        self.assertEqual(read_json(pointer_path), pointer)
        self.assertEqual(len(attempted_writes), 1)
        effect = self.fixture.state.db.execute(
            "SELECT state FROM effects WHERE key=?",
            (f"form-intake:active-v1-run:{pointer['run_id']}",),
        ).fetchone()
        self.assertEqual(effect[0], "done")

    def test_changed_business_input_cannot_replace_frozen_form_run(self):
        grant = self.offline_grant()
        first = self.fixture.engine.admit_form_record(self.form_record, grant)
        self.fixture.base.patch("tasks", self.form_record, {"视觉风格": "完全不同的风格"})

        with self.assertRaises(FactoryError):
            self.fixture.engine.admit_form_record(self.form_record, grant)

        self.assertEqual(
            self.fixture.base.get_record("tasks", self.form_record)["fields"]["运行ID"],
            first["run_id"],
        )

    def test_submission_intent_cannot_change_during_admission_retry(self):
        grant = self.offline_grant()
        first = self.fixture.engine.admit_form_record(self.form_record, grant)
        self.fixture.base.patch("tasks", self.form_record, {"创建确认": "暂不创建"})

        with self.assertRaisesRegex(FactoryError, "业务字段"):
            self.fixture.engine.admit_form_record(self.form_record, grant)

        self.assertEqual(
            self.fixture.base.get_record("tasks", self.form_record)["fields"]["运行ID"],
            first["run_id"],
        )
        self.assertEqual(
            self.fixture.base.get_record("tasks", self.form_record)["fields"]["创建确认"],
            "暂不创建",
        )

    def test_timeout_after_remote_task_patch_only_reconciles_on_retry(self):
        grant = self.offline_grant()
        update = self.fixture.base.update_record

        def applied_then_timeout(table, record_id, fields):
            result = update(table, record_id, fields)
            if table == "tasks" and record_id == self.form_record:
                raise TimeoutError("reply lost after task patch")
            return result

        with patch.object(self.fixture.base, "update_record", side_effect=applied_then_timeout):
            with self.assertRaises(TimeoutError):
                self.fixture.engine.admit_form_record(self.form_record, grant)

        recovered = self.fixture.engine.admit_form_record(self.form_record, grant)
        self.assertEqual(
            self.fixture.base.get_record("tasks", self.form_record)["fields"]["运行ID"],
            recovered["run_id"],
        )
        self.assertEqual(len(self.fixture.base.list("tasks")), 2)

    def test_runtime_and_pointer_without_task_journal_resume_first_projection(self):
        grant = self.offline_grant()
        effect = self.fixture.state.effect

        def stop_before_task_journal(key, find, write):
            if key.startswith(f"form-intake:task:{self.form_record}:"):
                raise TimeoutError("process stopped before task effect journal")
            return effect(key, find, write)

        with patch.object(self.fixture.state, "effect", side_effect=stop_before_task_journal):
            with self.assertRaises(TimeoutError):
                self.fixture.engine.admit_form_record(self.form_record, grant)

        before = self.fixture.base.get_record("tasks", self.form_record)
        self.assertNotIn("运行ID", before["fields"])
        accepted = self.fixture.engine.admit_form_record(self.form_record, grant)
        self.assertEqual(self.fixture.base.get_record("tasks", self.form_record)["fields"]["运行ID"],
                         accepted["run_id"])

    def test_unknown_task_effect_without_remote_result_stays_blocked(self):
        grant = self.offline_grant()
        update = self.fixture.base.update_record
        calls = []

        def fail_before_task_update(table, record_id, fields):
            if table == "tasks" and record_id == self.form_record:
                calls.append(1)
                raise TimeoutError("no task patch result")
            return update(table, record_id, fields)

        with patch.object(self.fixture.base, "update_record", side_effect=fail_before_task_update):
            with self.assertRaises(TimeoutError):
                self.fixture.engine.admit_form_record(self.form_record, grant)
        with self.assertRaises(FactoryError):
            self.fixture.engine.admit_form_record(self.form_record, grant)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("运行ID", self.fixture.base.get_record("tasks", self.form_record)["fields"])

    def test_missing_grant_does_not_create_runtime_or_patch_form(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        self.offline_grant()
        before = self.fixture.base.get_record("tasks", self.form_record)
        with self.assertRaisesRegex(FactoryError, "授权"):
            self.fixture.engine.admit_form_record(self.form_record, None)
        self.assertEqual(self.fixture.base.get_record("tasks", self.form_record), before)
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 0)
        finally:
            runtime.close()

    def test_grant_without_actor_is_rejected_before_any_runtime_or_feishu_write(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        grant = self.offline_grant()
        grant.pop("actor_id")
        before = self.fixture.base.get_record("tasks", self.form_record)

        with self.assertRaisesRegex(FactoryError, "授权人"):
            self.fixture.engine.admit_form_record(self.form_record, grant)

        self.assertEqual(self.fixture.base.get_record("tasks", self.form_record), before)
        self.assertFalse((self.fixture.state.root / "active-v1-run.json").exists())
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 0)
        finally:
            runtime.close()

    def test_grant_without_product_binding_is_rejected(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        grant = self.offline_grant()
        grant["scope"].pop("product_record_id")
        before = self.fixture.base.get_record("tasks", self.form_record)

        with self.assertRaisesRegex(FactoryError, "商品"):
            self.fixture.engine.admit_form_record(self.form_record, grant)

        self.assertEqual(self.fixture.base.get_record("tasks", self.form_record), before)
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 0)
        finally:
            runtime.close()

    def test_grant_for_another_product_is_rejected(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        grant = self.offline_grant()
        grant["scope"]["product_record_id"] = "REC-OTHER-PRODUCT"
        before = self.fixture.base.get_record("tasks", self.form_record)

        with self.assertRaisesRegex(FactoryError, "商品"):
            self.fixture.engine.admit_form_record(self.form_record, grant)

        self.assertEqual(self.fixture.base.get_record("tasks", self.form_record), before)
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 0)
        finally:
            runtime.close()

    def test_admission_rejects_namespace_suffix_before_queuing(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        profile = self.fixture.config["intake_profile"]
        grant = self.offline_grant()
        profile["namespace"] = "V1-DEMO-KIDS-INTAKE-OFFLINE"
        grant["scope"]["namespace"] = profile["namespace"]
        with self.assertRaisesRegex(FactoryError, "命名空间"):
            self.fixture.engine.admit_form_record(self.form_record, grant)
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 0)
        finally:
            runtime.close()

    def test_form_cannot_supply_engine_owned_status_or_policy(self):
        self.fixture.base.patch("tasks", self.form_record, {
            "提交": True,
            "审核策略版本": "production-v1",
            "运行ID": "run_spoofed",
        })
        with self.assertRaisesRegex(FactoryError, "技术字段"):
            self.fixture.engine.preview_form_record(self.form_record)

    def test_form_cannot_set_engine_owned_submit_checkbox(self):
        self.fixture.base.patch("tasks", self.form_record, {"提交": True})

        with self.assertRaisesRegex(FactoryError, "技术字段"):
            self.fixture.engine.preview_form_record(self.form_record)

    def test_demo_preview_rejects_a_production_product_even_if_allowlisted(self):
        self.fixture.base.patch("products", self.fixture.product, {"示例数据": False})
        with self.assertRaisesRegex(FactoryError, "模式"):
            self.fixture.engine.preview_form_record(self.form_record)

    def test_product_outside_trusted_scope_is_not_selected_by_display_name(self):
        self.fixture.config["intake_profile"]["allowed_product_ids"] = []
        with self.assertRaisesRegex(FactoryError, "受理范围"):
            self.fixture.engine.preview_form_record(self.form_record)

    def test_revoked_source_permission_still_blocks_preview(self):
        self.fixture.base.patch("assets", self.fixture.asset, {"允许用于生图": False})
        with self.assertRaisesRegex(FactoryError, "授权或可用确认"):
            self.fixture.engine.preview_form_record(self.form_record)

    def test_source_without_registered_sha_cannot_be_previewed_as_traceable(self):
        original = self.fixture.base.list("tasks")
        self.fixture.base.patch("assets", self.fixture.asset, {"SHA256": ""})
        with self.assertRaisesRegex(FactoryError, "SHA256"):
            self.fixture.engine.preview_form_record(self.form_record)
        self.assertEqual(self.fixture.base.list("tasks"), original)
        self.assertIsNone(self.fixture.state.by_task(self.form_record))

    def test_malformed_trusted_scope_must_fail_closed(self):
        self.fixture.config["intake_profile"]["allowed_product_ids"] = "x" + self.fixture.product + "x"
        with self.assertRaisesRegex(FactoryError, "受信任务受理配置"):
            self.fixture.engine.preview_form_record(self.form_record)

    def test_demo_policy_cannot_use_an_unrelated_namespace(self):
        self.fixture.config["intake_profile"]["namespace"] = "ISOLATED-DEMO-TEST"
        with self.assertRaisesRegex(FactoryError, "策略"):
            self.fixture.engine.preview_form_record(self.form_record)

    def test_demo_profile_cannot_expand_image_call_limit(self):
        self.fixture.config["intake_profile"]["max_calls"] = 7
        with self.assertRaisesRegex(FactoryError, "调用上限"):
            self.fixture.engine.preview_form_record(self.form_record)
        self.assertIsNone(self.fixture.state.by_task(self.form_record))

    def test_opaque_product_image_is_not_previewed_as_ready_for_composition(self):
        opaque = Path(self.temp.name) / "opaque.png"
        Image.new("RGB", (64, 64), "white").save(opaque)
        self.fixture.base.patch("assets", self.fixture.asset, {
            "图片": [], "SHA256": file_hash(opaque),
        })
        self.fixture.base.upload("assets", self.fixture.asset, "图片", opaque)
        with self.assertRaisesRegex(FactoryError, "透明"):
            self.fixture.engine.preview_form_record(self.form_record)


if __name__ == "__main__":
    unittest.main()
