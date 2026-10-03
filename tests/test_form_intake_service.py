import tempfile
import unittest
from unittest.mock import patch

from support import Fixture
from factory.runtime import Runtime
from factory.runtime_runner import runtime_db
from factory.form_intake_service import poll_form_once
from factory.util import FactoryError, UnknownWrite


class FormIntakeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(self.temp.name)
        self.fixture.base.patch("workflows", self.fixture.flow, {"示例数据": True})
        self.fixture.config["intake_profile"] = {
            "allowed_product_ids": [self.fixture.product],
            "workflow_id": self.fixture.flow,
            "channel": "DEMO",
            "placement": "演示审图台",
            "namespace": "V1-DEMO-KIDS",
            "mode": "demo",
            "review_policy_version": "demo-v2",
            "max_calls": 6,
        }
        self.grant = {
            "authorization_id": "AUTH-FORM-SERVICE-OFFLINE-6",
            "scope": {"namespace": "V1-DEMO-KIDS", "category": "kids_shoes",
                      "mode": "demo", "grant": "offline-form-service-test",
                      "product_record_id": self.fixture.product},
            "project_image_calls_limit": 6,
            "approved": True,
            "actor_id": "ou_demo",
            "source": "offline test only",
        }
        self.form_record = self.add_form()

    def tearDown(self):
        self.fixture.close()
        self.temp.cleanup()

    def add_form(self, **overrides):
        fields = {
            "选择商品": ["测试鞋"],
            "图片用途": "场景展示",
            "消费场景": "室内",
            "视觉风格": "清爽",
            "数量": 3,
            "附加要求": "保持鞋面结构",
            "创建确认": "确认创建演示任务",
        }
        fields.update(overrides)
        return self.fixture.base.create("tasks", fields)["record_id"]

    def test_one_unbound_form_is_admitted_as_queued_without_spending_image_calls(self):
        result = poll_form_once(self.fixture.engine, self.grant)

        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["task_record_id"], self.form_record)
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            status = runtime.status(result["run_id"])
        finally:
            runtime.close()
        self.assertEqual(status["state"], "queued")
        self.assertEqual(status["image_calls_reserved"], 0)

    def test_complete_form_without_explicit_submission_is_left_unbound(self):
        self.fixture.base.patch("tasks", self.form_record, {"创建确认": "暂不创建"})

        result = poll_form_once(self.fixture.engine, self.grant)

        self.assertEqual(result, {"status": "idle"})
        row = self.fixture.base.get_record("tasks", self.form_record)["fields"]
        self.assertNotIn("运行ID", row)
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 0)
        finally:
            runtime.close()

    def test_only_exact_confirmation_choice_is_polled(self):
        for value in (1, "true", ["其他选项"], ["确认创建演示任务", "其他选项"], True):
            with self.subTest(value=value):
                self.fixture.base.patch("tasks", self.form_record, {"创建确认": value})
                self.assertEqual(poll_form_once(self.fixture.engine, self.grant), {"status": "idle"})

        self.assertNotIn(
            "运行ID", self.fixture.base.get_record("tasks", self.form_record)["fields"],
        )

    def test_single_choice_confirmation_list_is_accepted(self):
        self.fixture.base.patch(
            "tasks", self.form_record, {"创建确认": ["确认创建演示任务"]},
        )

        result = poll_form_once(self.fixture.engine, self.grant)

        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["task_record_id"], self.form_record)

    def test_poll_scans_only_with_complete_local_profile_and_explicit_grant(self):
        original = self.fixture.base.list_records
        reads = []

        def capture(*args, **kwargs):
            reads.append((args, kwargs))
            return original(*args, **kwargs)

        with patch.object(self.fixture.base, "list_records", side_effect=capture):
            self.fixture.engine.config.pop("intake_profile")
            with self.assertRaises(FactoryError):
                poll_form_once(self.fixture.engine, self.grant)

            self.fixture.engine.config["intake_profile"] = {
                "allowed_product_ids": [self.fixture.product],
                "workflow_id": self.fixture.flow,
                "channel": "DEMO",
                "placement": "演示审图台",
                "namespace": "V1-DEMO-KIDS",
                "mode": "demo",
                "review_policy_version": "demo-v2",
                "max_calls": 6,
            }
            for grant in (None, {}, {"authorization_id": "incomplete"}):
                with self.subTest(grant=grant), self.assertRaises(FactoryError):
                    poll_form_once(self.fixture.engine, grant)

        self.assertEqual(reads, [])

    def test_no_pending_form_returns_idle_and_queries_only_the_intake_fields(self):
        self.fixture.base.patch("tasks", self.form_record, {"运行ID": "RUN-ALREADY-BOUND"})
        self.add_form(**{"选择商品": []})
        self.add_form(**{"选择商品": []})
        original = self.fixture.base.list_records
        reads = []

        def capture(*args, **kwargs):
            reads.append((args, kwargs))
            return original(*args, **kwargs)

        with patch.object(self.fixture.base, "list_records", side_effect=capture):
            result = poll_form_once(self.fixture.engine, self.grant)

        self.assertEqual(result, {"status": "idle"})
        self.assertEqual(len(reads), 1)
        args, kwargs = reads[0]
        self.assertEqual(args, ("tasks",))
        self.assertEqual(kwargs["field_names"], [
            "选择商品", "图片用途", "消费场景", "视觉风格", "数量", "附加要求",
            "创建确认", "运行ID", "提交", "系统状态",
        ])
        self.assertEqual(kwargs["filter_json"], {
            "logic": "and", "conditions": [["选择商品", "non_empty"]],
        })

    def test_multiple_pending_forms_fail_closed_without_admitting_either(self):
        self.add_form(**{"附加要求": "第二条未绑定草稿"})

        with self.assertRaisesRegex(FactoryError, "多条待受理"):
            poll_form_once(self.fixture.engine, self.grant)

        for row in self.fixture.base.list("tasks"):
            if "选择商品" in row["fields"]:
                self.assertNotIn("运行ID", row["fields"])
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            count = runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0]
        finally:
            runtime.close()
        self.assertEqual(count, 0)

    def test_unsubmitted_draft_does_not_block_one_explicitly_submitted_form(self):
        draft = self.add_form(**{"创建确认": "暂不创建", "附加要求": "尚未提交的草稿"})

        result = poll_form_once(self.fixture.engine, self.grant)

        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["task_record_id"], self.form_record)
        self.assertNotIn("运行ID", self.fixture.base.get_record("tasks", draft)["fields"])

    def test_bound_form_is_not_admitted_again_on_a_later_poll(self):
        first = poll_form_once(self.fixture.engine, self.grant)

        second = poll_form_once(self.fixture.engine, self.grant)

        self.assertEqual(second, {"status": "idle"})
        self.assertEqual(
            self.fixture.base.get_record("tasks", self.form_record)["fields"]["运行ID"],
            first["run_id"],
        )
        runtime = Runtime(runtime_db(self.fixture.state.root), single_instance=True)
        try:
            count = runtime.db.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0]
            status = runtime.status(first["run_id"])
        finally:
            runtime.close()
        self.assertEqual(count, 1)
        self.assertEqual(status["image_calls_reserved"], 0)

    def test_unknown_write_is_propagated_without_a_retry(self):
        error = UnknownWrite("write result unknown")
        writes = []

        def unknown_task_write(table, record_id, fields):
            if table == "tasks" and record_id == self.form_record:
                writes.append((table, record_id))
                raise error
            raise AssertionError("intake should only write its task projection")

        with patch.object(self.fixture.base, "update_record", side_effect=unknown_task_write):
            with self.assertRaises(UnknownWrite) as raised:
                poll_form_once(self.fixture.engine, self.grant)

        self.assertIs(raised.exception, error)
        self.assertEqual(writes, [("tasks", self.form_record)])


if __name__ == "__main__":
    unittest.main()
