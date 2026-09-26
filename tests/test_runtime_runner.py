import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from factory.runtime_runner import advance_local, create_demo, dispatch_next, reject_image, stage_image


ROOT = Path(__file__).resolve().parents[1]


class RuntimeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        self.run_id = "V1-DEMO-KIDS-OFFLINE-001"
        create_demo(
            self.state,
            ROOT / "templates/runtime-workflow-kids-demo-v1.json",
            self.run_id,
            "AUTH-OFFLINE-6",
            "user_test",
        )
        self.evidence = Path(self.temp.name) / "evidence.txt"
        self.evidence.write_text("OFFLINE TEST; no native image call.\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def product(self):
        path = Path(self.temp.name) / "product.png"
        image = Image.new("RGBA", (128, 96), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((16, 20, 112, 80), radius=8, fill=(245, 248, 255, 255))
        image.save(path)
        return path

    def background(self, scene):
        path = Path(self.temp.name) / f"{scene}.png"
        colors = {"outdoor": (180, 220, 230), "indoor": (240, 225, 205), "studio": (220, 230, 250)}
        Image.new("RGB", (256, 256), colors[scene]).save(path)
        return path

    def test_four_real_file_boundaries_and_local_pipeline_reach_review(self):
        job = dispatch_next(self.state, self.run_id)
        stage_image(self.state, job["job_id"], self.product(), self.evidence, "mock-test", "offline-1")
        first = advance_local(self.state, self.run_id)
        self.assertEqual(first["advanced"], ["white-preview"])

        for index, scene in enumerate(("outdoor", "indoor", "studio"), 2):
            job = dispatch_next(self.state, self.run_id)
            self.assertEqual(job["step"], "background-" + scene)
            stage_image(self.state, job["job_id"], self.background(scene), self.evidence,
                        "mock-test", f"offline-{index}")
            result = advance_local(self.state, self.run_id)

        self.assertEqual(result["next"]["state"], "waiting_review")
        self.assertEqual(result["status"]["image_calls_reserved"], 4)
        candidates = self.state / "runtime-runs" / self.run_id / "candidates"
        self.assertEqual(sorted(path.name for path in candidates.glob("*.png")), [
            "candidate-indoor.png", "candidate-outdoor.png", "candidate-studio.png"
        ])

    def test_rejected_native_output_is_preserved_and_next_attempt_reserves_again(self):
        job = dispatch_next(self.state, self.run_id)
        bad = Path(self.temp.name) / "bad.jpg"
        Image.new("RGB", (128, 96), (255, 255, 255)).save(bad)
        result = reject_image(self.state, job["job_id"], bad, self.evidence,
                              "product output has no alpha channel")
        self.assertEqual(result["state"], "failed_confirmed")
        self.assertEqual(result["budget"]["reserved"], 1)
        retry = dispatch_next(self.state, self.run_id)
        self.assertNotEqual(job["attempt_id"], retry["attempt_id"])
        self.assertEqual(retry["budget"]["reserved"], 2)
        self.assertTrue((self.state / "runtime-jobs" / job["job_id"] /
                         "failed" / "failure.json").is_file())


if __name__ == "__main__":
    unittest.main()
