import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from factory.mock import MockBase
from factory.runtime_runner import advance_local, create_demo, dispatch_next, stage_image
from factory.state import State
from factory.v1_live import project_status_outbox, publish_candidates, seed_demo


ROOT = Path(__file__).resolve().parents[1]


class V1LiveProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_root = self.root / "state"
        self.run_id = "V1-DEMO-KIDS-PROJECTION-001"
        create_demo(self.state_root, ROOT / "templates/runtime-workflow-kids-demo-v1.json",
                    self.run_id, "AUTH-PROJECTION-6", "user_test")
        self.base = MockBase(self.root / "remote")
        self.state = State(self.state_root)
        self.evidence = self.root / "evidence.txt"
        self.evidence.write_text("OFFLINE TEST ONLY\n", encoding="utf-8")

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def image(self, role):
        path = self.root / f"{role}.png"
        if role == "product":
            image = Image.new("RGBA", (128, 96), (255, 255, 255, 0))
            ImageDraw.Draw(image).rounded_rectangle((16, 20, 112, 80), radius=8,
                                                     fill=(245, 248, 255, 255))
        else:
            image = Image.new("RGB", (256, 256), (210, 225, 235))
        image.save(path)
        return path

    def finish_images(self):
        for role in ("product", "outdoor", "indoor", "studio"):
            job = dispatch_next(self.state_root, self.run_id)
            stage_image(self.state_root, job["job_id"], self.image(role), self.evidence,
                        "mock-test", "offline-" + role)
            advance_local(self.state_root, self.run_id)

    def test_seed_and_candidate_projection_are_idempotent(self):
        first = seed_demo(self.base, self.state, self.run_id)
        second = seed_demo(self.base, self.state, self.run_id)
        self.assertEqual(first, second)
        self.finish_images()
        published = publish_candidates(self.base, self.state, self.state_root, self.run_id)
        again = publish_candidates(self.base, self.state, self.state_root, self.run_id)
        self.assertEqual(published["assets"], again["assets"])
        self.assertEqual(published["roundtrip_verified"], 8)
        self.assertEqual(len(self.base.list("assets")), 8)
        task = self.base.get("tasks", first["task_record_id"])["fields"]
        self.assertEqual(task["系统状态"], ["待审核"])

    def test_status_projector_confirms_stale_outbox_without_regression(self):
        seeded = seed_demo(self.base, self.state, self.run_id)
        self.base.update_record("tasks", seeded["task_record_id"],
                                {"运行版本": 999, "系统状态": ["待审核"]})
        projected = project_status_outbox(self.base, self.state_root, self.run_id)
        self.assertTrue(projected["confirmed"])
        task = self.base.get_record("tasks", seeded["task_record_id"])["fields"]
        self.assertEqual(task["运行版本"], 999)
        self.assertEqual(task["系统状态"], ["待审核"])

    def test_candidate_projection_accepts_omitted_empty_text_fields(self):
        class EmptyTextDroppingBase(MockBase):
            def create_record(self, table, fields):
                return super().create_record(
                    table, {name: value for name, value in fields.items() if value != ""}
                )

        base = EmptyTextDroppingBase(self.root / "remote-empty-text")
        seed_demo(base, self.state, self.run_id)
        self.finish_images()
        published = publish_candidates(base, self.state, self.state_root, self.run_id)
        self.assertEqual(published["roundtrip_verified"], 8)


if __name__ == "__main__":
    unittest.main()
