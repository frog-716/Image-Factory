import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from factory.mock import MockBase
from factory.human_ui_server import HumanReviewEngine
from factory.runner_service import request_stop, run_loop, run_once, runner_status
from factory.runtime_runner import advance_local, create_demo, dispatch_next, stage_image
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
        self.assertTrue(task_view["delivery_url"].startswith("/api/v1/deliveries/"))
        token = task_view["delivery_url"].rsplit("/", 1)[-1]
        content, filename = review_engine.delivery_file(token)
        self.assertTrue(filename.endswith(".zip"))
        self.assertEqual(content[:2], b"PK")


if __name__ == "__main__":
    unittest.main()
