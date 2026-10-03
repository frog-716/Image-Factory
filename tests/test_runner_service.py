import tempfile
import unittest
import json
from pathlib import Path

from PIL import Image, ImageDraw

from factory.mock import MockBase
from factory.human_ui_server import HumanReviewEngine
from factory.runner_service import _task_id, request_stop, run_loop, run_once, runner_status
from factory.runtime import Runtime
from factory.runtime_runner import advance_local, create_demo, dispatch_next, runtime_db, stage_image
from factory.state import State
from factory.v1_live import seed_demo


ROOT = Path(__file__).resolve().parents[1]


class RunnerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_root = self.root / "state"
        self.run_id = "V1-DEMO-KIDS-RUNNER-001"
        class ActorBase(MockBase):
            def __init__(inner, root):
                super().__init__(root)
                inner.config = {"reviewer_open_ids": ["human-reviewer"]}
                inner.mock_reviewer_open_id = "human-reviewer"
            def create_record(inner, table, fields):
                if table == "reviews":
                    fields = {**fields, "创建人": [{"id": "human-reviewer"}],
                              "创建时间": "2026-09-21T08:00:00+00:00"}
                return super().create_record(table, fields)
        self.base = ActorBase(self.root / "remote")
        self.state = State(self.state_root)
        seed_demo(self.base, self.state, self.run_id)
        create_demo(self.state_root, ROOT / "templates/runtime-workflow-kids-demo-v1.json",
                    self.run_id, "AUTH-RUNNER-6", "user_test")
        self.evidence = self.root / "evidence.txt"
        self.evidence.write_text("OFFLINE TEST ONLY\n", encoding="utf-8")

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_loop_dispatches_once_persists_heartbeat_and_exposes_job_file(self):
        result = run_loop(self.base, self.state, self.state_root, self.run_id,
                          interval=1, max_cycles=1)
        self.assertEqual(result["state"], "waiting_worker")
        status = runner_status(self.state_root, self.run_id)
        self.assertEqual(status["runtime"]["image_calls_reserved"], 1)
        self.assertEqual(status["heartbeat"]["state"], "waiting_worker")
        self.assertEqual(len(status["active_jobs"]), 1)
        self.assertTrue(Path(status["active_jobs"][0]["job_file"]).is_file())
        task = self.base.find_unique("tasks", "任务名", self.run_id)["fields"]
        self.assertEqual(task["系统状态"], ["生成中"])
        stopped = request_stop(self.state_root, self.run_id)
        self.assertTrue(stopped["stop_requested"])
        self.assertTrue(runner_status(self.state_root, self.run_id)["stop_requested"])

    def test_form_runtime_resolves_frozen_task_record_not_business_title(self):
        form_run_id = "V1-DEMO-KIDS-FORM-RESOLVE"
        seeded = self.base.find_unique("tasks", "任务名", self.run_id)["fields"]
        task = self.base.create("tasks", {
            "任务名": "人类填写的童鞋任务", "运行ID": form_run_id,
            "命名空间": "V1-DEMO-KIDS", "模式": ["demo"],
            "审核策略版本": "demo-v2",
            "商品": seeded["商品"], "流程": seeded["流程"],
        })
        plan = json.loads((ROOT / "templates/runtime-workflow-kids-demo-v1.json").read_text())
        plan["run_id"] = form_run_id
        plan["form_task_record_id"] = task["record_id"]
        plan["intake_binding"] = {"task_projection": {
            "商品": seeded["商品"], "流程": seeded["流程"],
        }}
        runtime = Runtime(runtime_db(self.state_root), single_instance=True)
        try:
            runtime.create(plan)
            self.assertEqual(_task_id(self.base, form_run_id, runtime), task["record_id"])
            self.base.patch("tasks", task["record_id"], {"运行ID": "FORGED-OTHER"})
            with self.assertRaises(Exception):
                _task_id(self.base, form_run_id, runtime)
            self.base.patch("tasks", task["record_id"], {"运行ID": form_run_id})
            other = self.base.create("products", {"商品名": "另一款", "SKU": "OTHER"})
            self.base.patch("tasks", task["record_id"], {"商品": [other["record_id"]]})
            with self.assertRaises(Exception):
                _task_id(self.base, form_run_id, runtime)
        finally:
            runtime.close()

    def _image(self, role):
        path = self.root / f"{role}.png"
        if role == "product":
            image = Image.new("RGBA", (128, 96), (255, 255, 255, 0))
            ImageDraw.Draw(image).rounded_rectangle((16, 20, 112, 80), radius=8,
                                                     fill=(245, 248, 255, 255))
        else:
            image = Image.new("RGB", (256, 256), (210, 225, 235))
        image.save(path)
        return path

    def test_after_human_reviews_runner_exports_zip_and_simulated_metrics(self):
        for role in ("product", "outdoor", "indoor", "studio"):
            job = dispatch_next(self.state_root, self.run_id)
            stage_image(self.state_root, job["job_id"], self._image(role), self.evidence,
                        "mock-test", "offline-" + role)
            advance_local(self.state_root, self.run_id)
        waiting = run_once(self.base, self.state, self.state_root, self.run_id)
        self.assertEqual(waiting["state"], "waiting_review")
        review_engine = HumanReviewEngine(
            self.base, self.state_root, self.run_id, reviewer_id="human-reviewer"
        )
        candidates = review_engine.pending_reviews()["items"]
        self.assertEqual(len(candidates), 3)
        for item in candidates:
            review_engine.submit_review(
                item["review_token"], "approved", "offline contract test"
            )
        accepted = run_once(self.base, self.state, self.state_root, self.run_id)
        self.assertEqual(accepted["review"]["status"], "accepted")
        exported = run_once(self.base, self.state, self.state_root, self.run_id)
        self.assertEqual(exported["export"]["delivered_count"], 3)
        self.assertTrue(exported["export"]["round_trip_sha256_equal"])
        completed = run_once(self.base, self.state, self.state_root, self.run_id)
        self.assertEqual(completed["state"], "completed_demo")
        self.assertEqual(len(self.base.list("placements")), 3)
        self.assertEqual(len(self.base.list("metrics")), 3)
        task = self.base.find_unique("tasks", "任务名", self.run_id)["fields"]
        self.assertEqual(task["系统状态"], ["已完成"])
        task_view = review_engine.task_list()["items"][0]["display"]
        self.assertEqual(task_view["status"], "已完成")
        self.assertIn("可下载", task_view["detail"])
        self.assertTrue(task_view["delivery_url"].startswith("/api/v1/deliveries/"))
        token = task_view["delivery_url"].rsplit("/", 1)[-1]
        content, filename = review_engine.delivery_file(token)
        self.assertTrue(filename.endswith(".zip"))
        self.assertEqual(content[:2], b"PK")

    def test_all_rejected_task_view_explains_why_there_is_no_zip(self):
        for role in ("product", "outdoor", "indoor", "studio"):
            job = dispatch_next(self.state_root, self.run_id)
            stage_image(self.state_root, job["job_id"], self._image(role), self.evidence,
                        "mock-test", "offline-" + role)
            advance_local(self.state_root, self.run_id)
        run_once(self.base, self.state, self.state_root, self.run_id)
        review_engine = HumanReviewEngine(
            self.base, self.state_root, self.run_id, reviewer_id="human-reviewer"
        )
        for item in review_engine.pending_reviews()["items"]:
            review_engine.submit_review(item["review_token"], "rejected", "外观不合格")
        run_once(self.base, self.state, self.state_root, self.run_id)
        run_once(self.base, self.state, self.state_root, self.run_id)
        completed = run_once(self.base, self.state, self.state_root, self.run_id)
        self.assertEqual(completed["state"], "completed_demo")
        display = review_engine.task_list()["items"][0]["display"]
        self.assertEqual(display["status"], "无可交付图片")
        self.assertIn("全部图片已退回", display["detail"])
        self.assertIsNone(display["delivery_url"])


if __name__ == "__main__":
    unittest.main()
