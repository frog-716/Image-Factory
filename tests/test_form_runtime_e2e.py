"""Offline form-to-delivery trace using MockBase and mock image receipts only."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from factory.human_ui_server import HumanReviewEngine
from factory.runner_service import run_once
from factory.runtime import Runtime
from factory.runtime_runner import advance_local, dispatch_next, runtime_db, stage_image
from factory.v1_live import publish_candidates


class FormRuntimeE2ETests(unittest.TestCase):
    def test_form_enters_existing_runner_and_delivers_only_approved_mock_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            try:
                fixture.base.patch("workflows", fixture.flow, {"示例数据": True})
                fixture.config["intake_profile"] = {
                    "allowed_product_ids": [fixture.product],
                    "workflow_id": fixture.flow,
                    "channel": "DEMO", "placement": "演示审图台",
                    "namespace": "V1-DEMO-KIDS", "mode": "demo",
                    "review_policy_version": "demo-v2", "max_calls": 6,
                }
                form = fixture.base.create("tasks", {
                    "选择商品": ["测试鞋"], "图片用途": "展示图",
                    "消费场景": "室内阅读角", "视觉风格": "柔和",
                    "数量": 3, "附加要求": "保留原鞋外观",
                    "创建确认": "确认创建演示任务",
                })["record_id"]
                grant = {
                    "authorization_id": "AUTH-OFFLINE-FORM-E2E-6",
                    "scope": {"namespace": "V1-DEMO-KIDS", "category": "kids_shoes",
                              "mode": "demo", "grant": "offline-e2e-only",
                              "product_record_id": fixture.product},
                    "project_image_calls_limit": 6, "approved": True,
                    "actor_id": "ou_demo", "source": "offline test only",
                }
                accepted = fixture.engine.admit_form_record(form, grant)
                run_id = accepted["run_id"]
                state_root = fixture.state.root

                advance_local(state_root, run_id)
                evidence = Path(temporary) / "mock-evidence.txt"
                evidence.write_text("OFFLINE MOCK ONLY\n", encoding="utf-8")
                for index in range(1, 4):
                    job = dispatch_next(state_root, run_id)
                    stage_image(state_root, job["job_id"],
                                fixture.fixture / f"demo-background-{index}.png",
                                evidence, "mock-test", f"offline-background-{index}")
                    advance_local(state_root, run_id)

                published = publish_candidates(fixture.base, fixture.state, state_root, run_id)
                self.assertEqual(published["task_record_id"], form)
                self.assertEqual(published["assets"][run_id + "-PRODUCT-RGBA"], fixture.asset)
                self.assertEqual(len(fixture.base.list("assets")), 1 + 7)
                self.assertEqual(
                    fixture.base.get_record("products", fixture.product)["fields"]["默认商品素材"],
                    [fixture.asset],
                )

                ui = HumanReviewEngine(fixture.base, state_root, run_id, reviewer_id="ou_demo")
                items = ui.pending_reviews()["items"]
                self.assertEqual([item["display"]["title"] for item in items],
                                 ["候选 A", "候选 B", "候选 C"])
                create = fixture.base.create_record

                def actor_created_review(table, fields):
                    if table == "reviews":
                        fields = {**fields, "创建人": [{"id": "ou_demo"}],
                                  "创建时间": "2026-09-27T08:00:00+00:00"}
                    return create(table, fields)

                with patch.object(fixture.base, "create_record", side_effect=actor_created_review):
                    ui.submit_review(items[0]["review_token"], "approved", "离线测试")
                    ui.submit_review(items[1]["review_token"], "approved", "离线测试")
                    ui.submit_review(items[2]["review_token"], "rejected", "离线测试退回")

                accepted_review = run_once(fixture.base, fixture.state, state_root, run_id)
                self.assertEqual(accepted_review["review"]["status"], "accepted")
                exported = run_once(fixture.base, fixture.state, state_root, run_id)
                self.assertEqual(exported["export"]["delivered_count"], 2)
                completed = run_once(fixture.base, fixture.state, state_root, run_id)
                self.assertEqual(completed["state"], "completed_demo")
                runtime = Runtime(runtime_db(state_root))
                try:
                    self.assertEqual(runtime.status(run_id)["image_calls_reserved"], 3)
                finally:
                    runtime.close()
                view = ui.task_list()["items"][0]["display"]
                self.assertEqual(view["status"], "已完成")
                token = view["delivery_url"].rsplit("/", 1)[-1]
                data, filename = ui.delivery_file(token)
                self.assertTrue(filename.endswith(".zip"))
                self.assertEqual(data[:2], b"PK")
            finally:
                fixture.close()
