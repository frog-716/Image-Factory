import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from factory.runtime_runner import (advance_local, create_asset_batch, create_demo,
                                    dispatch_next, reject_image, stage_image)


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

    def _create_import_runtime(self, run_id, *, state_root=None, source_bytes=None,
                               local_path="references/product.png", sha256=None,
                               asset_id="ASSET-FRONT-001"):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        state = Path(state_root or self.state)
        source = state / "references" / "product.png"
        source.parent.mkdir(parents=True, exist_ok=True)
        if source_bytes is None:
            source_bytes = self.product().read_bytes()
        source.write_bytes(source_bytes)
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        plan = {
            "schema_version": 1,
            "category": "kids_shoes",
            "run_id": run_id,
            "mode": "demo",
            "workflow_version": "wf-v1",
            "max_image_calls": 0,
            "steps": [
                {
                    "id": "product-source",
                    "kind": "compose",
                    "operation": "import_product_source",
                    "reference_asset": {
                        "local_path": local_path,
                        "sha256": sha256 or source_sha256,
                        "asset_id": asset_id,
                    },
                },
                {
                    "id": "white-preview",
                    "kind": "compose",
                    "operation": "white_preview",
                    "depends_on": ["product-source"],
                },
            ],
        }
        runtime = Runtime(runtime_db(state), single_instance=True)
        try:
            runtime.create(plan)
        finally:
            runtime.close()
        return state, source, source_bytes, source_sha256

    def test_runtime_imports_product_source_bytes_for_existing_white_preview(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        run_id = "LOCAL-IMPORT-OFFLINE-001"
        state, _, source_bytes, source_sha256 = self._create_import_runtime(run_id)

        result = advance_local(state, run_id)
        self.assertEqual(result["advanced"], ["product-source", "white-preview"])
        runtime = Runtime(runtime_db(state), single_instance=True)
        try:
            receipt = runtime.step_output(run_id, "product-source")
            status = runtime.status(run_id)
        finally:
            runtime.close()

        imported = state / "runtime-runs" / run_id / "product_source_imported.png"
        self.assertEqual(result["next"]["state"], "completed")
        self.assertEqual(status["image_calls_reserved"], 0)
        self.assertEqual(imported.read_bytes(), source_bytes)
        self.assertEqual(receipt["outputs"], [{
            "name": "product_source_imported.png",
            "sha256": source_sha256,
            "bytes": len(source_bytes),
            "local_path": f"runtime-runs/{run_id}/product_source_imported.png",
            "role": "product_rgba",
            "source_asset_id": "ASSET-FRONT-001",
        }])

    def test_product_source_import_rejects_absolute_traversal_and_symlink_paths(self):
        from factory.util import FactoryError

        for index, path_kind in enumerate(("absolute", "traversal", "symlink"), 1):
            with self.subTest(path_kind=path_kind):
                state = Path(self.temp.name) / f"path-state-{index}"
                source = state / "references" / "product.png"
                source.parent.mkdir(parents=True)
                source.write_bytes(self.product().read_bytes())
                if path_kind == "absolute":
                    path = str(source)
                elif path_kind == "traversal":
                    path = "../outside.png"
                else:
                    link = state / "links" / "product.png"
                    link.parent.mkdir(parents=True)
                    link.symlink_to(source)
                    path = "links/product.png"
                run_id = f"LOCAL-IMPORT-ATTACK-{index}"
                self._create_import_runtime(run_id, state_root=state, local_path=path)
                with self.assertRaises(FactoryError):
                    advance_local(state, run_id)
                self.assertFalse((state / "runtime-runs" / run_id /
                                  "product_source_imported.png").exists())

    def test_product_source_import_rejects_sha_mismatch_and_unsafe_asset_id(self):
        from factory.runtime import Blocked, Runtime
        from factory.runtime_runner import runtime_db

        cases = [
            ("sha", "ASSET-FRONT-001", "0" * 64),
            ("asset-id", "../outside", None),
        ]
        for run_id, (case, asset_id, sha256) in zip(
                ("LOCAL-IMPORT-SHA", "LOCAL-IMPORT-ASSET-ID"), cases):
            with self.subTest(case=case):
                state = Path(self.temp.name) / f"{case}-state"
                self._create_import_runtime(run_id, state_root=state,
                                            asset_id=asset_id, sha256=sha256)
                with self.assertRaises(Blocked):
                    advance_local(state, run_id)
                runtime = Runtime(runtime_db(state), single_instance=True)
                try:
                    self.assertEqual(runtime.next(run_id)["state"], "pending")
                    self.assertEqual(runtime.status(run_id)["image_calls_reserved"], 0)
                finally:
                    runtime.close()

    def test_product_source_import_requires_real_alpha_and_preserves_conflicting_target(self):
        from factory.runtime import Conflict, Runtime
        from factory.runtime_runner import runtime_db
        from factory.util import FactoryError

        opaque_path = Path(self.temp.name) / "opaque.png"
        Image.new("RGBA", (128, 96), (255, 255, 255, 255)).save(opaque_path)
        alpha_state = Path(self.temp.name) / "alpha-state"
        alpha_run = "LOCAL-IMPORT-OPAQUE"
        self._create_import_runtime(alpha_run, state_root=alpha_state,
                                    source_bytes=opaque_path.read_bytes())
        with self.assertRaises(FactoryError):
            advance_local(alpha_state, alpha_run)
        self.assertFalse((alpha_state / "runtime-runs" / alpha_run /
                          "product_source_imported.png").exists())

        conflict_state = Path(self.temp.name) / "conflict-state"
        conflict_run = "LOCAL-IMPORT-CONFLICT"
        self._create_import_runtime(conflict_run, state_root=conflict_state)
        target = conflict_state / "runtime-runs" / conflict_run / "product_source_imported.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"pre-existing different bytes")
        with self.assertRaises(Conflict):
            advance_local(conflict_state, conflict_run)
        self.assertEqual(target.read_bytes(), b"pre-existing different bytes")
        runtime = Runtime(runtime_db(conflict_state), single_instance=True)
        try:
            self.assertEqual(runtime.next(conflict_run)["state"], "pending")
            self.assertEqual(runtime.status(conflict_run)["image_calls_reserved"], 0)
        finally:
            runtime.close()

    def test_product_source_import_rejects_transparent_non_png_bytes(self):
        from factory.runtime import Blocked

        webp_path = Path(self.temp.name) / "transparent.webp"
        image = Image.new("RGBA", (128, 96), (255, 255, 255, 0))
        ImageDraw.Draw(image).rounded_rectangle(
            (16, 20, 112, 80), radius=8, fill=(245, 248, 255, 255))
        image.save(webp_path, format="WEBP", lossless=True)
        state = Path(self.temp.name) / "webp-state"
        run_id = "LOCAL-IMPORT-WEBP"
        self._create_import_runtime(run_id, state_root=state,
                                    source_bytes=webp_path.read_bytes())

        with self.assertRaises(Blocked):
            advance_local(state, run_id)

        self.assertFalse((state / "runtime-runs" / run_id /
                          "product_source_imported.png").exists())

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

    def test_new_composition_recipe_is_opt_in_for_an_isolated_runtime(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        plan = json.loads((ROOT / "templates/runtime-workflow-kids-demo-v1.json").read_text())
        plan["workflow_version"] = "kids-background-v2"
        next(step for step in plan["steps"] if step["id"] == "candidate-composition")[
            "operation"] = "compose_three_candidates_grounded_v2"
        template = Path(self.temp.name) / "v2-plan.json"
        template.write_text(json.dumps(plan), encoding="utf-8")
        state = Path(self.temp.name) / "state-v2"
        run_id = "V1-DEMO-KIDS-OFFLINE-V2"
        create_demo(state, template, run_id, "AUTH-OFFLINE-V2-6", "user_test")
        for index, scene in enumerate((None, "outdoor", "indoor", "studio"), 1):
            job = dispatch_next(state, run_id)
            image = self.product() if scene is None else self.background(scene)
            stage_image(state, job["job_id"], image, self.evidence,
                        "mock-test", f"offline-v2-{index}")
            result = advance_local(state, run_id)
        self.assertEqual(result["next"]["state"], "waiting_review")
        runtime = Runtime(runtime_db(state), single_instance=True)
        try:
            candidates = runtime.step_output(run_id, "candidate-composition")["candidates"]
        finally:
            runtime.close()
        self.assertEqual(set(candidates), {"outdoor", "indoor", "studio"})
        self.assertTrue(all(item["algorithm"] == "alpha-composite-grounded-v2"
                            for item in candidates.values()))

    def test_grounded_recipe_cannot_hide_under_v1_workflow_version(self):
        from factory.runtime import Blocked

        plan = json.loads((ROOT / "templates/runtime-workflow-kids-demo-v1.json").read_text())
        next(step for step in plan["steps"] if step["id"] == "candidate-composition")[
            "operation"] = "compose_three_candidates_grounded_v2"
        template = Path(self.temp.name) / "mismatched-plan.json"
        template.write_text(json.dumps(plan), encoding="utf-8")
        state = Path(self.temp.name) / "mismatched-state"
        with self.assertRaises(Blocked):
            create_demo(state, template, "V1-DEMO-KIDS-OFFLINE-MISMATCH",
                        "AUTH-OFFLINE-MISMATCH-6", "user_test")
        self.assertFalse((state / "ledger.sqlite3").exists())

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

    def test_new_fictional_asset_batch_has_separate_six_call_grant(self):
        from factory.runtime import Blocked

        asset_state = Path(self.temp.name) / "asset-state"
        template = ROOT / "templates/runtime-fictional-kids-assets-v1.json"
        result = create_asset_batch(asset_state, template,
                                    "FICTIONAL-ASSET-20260927-001",
                                    "AUTH-FICTIONAL-ASSET-20260927-6", "user_test",
                                    self.product())
        self.assertEqual(result["run"]["authorization"]["max_calls"], 6)
        self.assertEqual(result["run"]["authorization"]["reserved"], 0)
        self.assertEqual(result["run"]["authorization"]["scope"]["namespace"],
                         "FICTIONAL-ASSET-LAB")
        self.assertEqual(result["reference_asset"]["source"],
                         "caller_supplied_local_reference")
        self.assertEqual(dispatch_next(asset_state, "FICTIONAL-ASSET-20260927-001")
                         ["budget"]["reserved"], 1)
        self.assertEqual(self.state.joinpath("ledger.sqlite3").is_file(), True)
        with self.assertRaises(Blocked):
            create_asset_batch(asset_state, template, "V1-DEMO-KIDS-001",
                               "AUTH-ATTEMPT", "user_test", self.product())

    def test_catalog_followup_shares_original_six_call_authorization(self):
        asset_state = Path(self.temp.name) / "asset-state"
        auth = "AUTH-FICTIONAL-ASSET-20260927-6"
        source = self.product()
        create_asset_batch(asset_state,
                           ROOT / "templates/runtime-fictional-kids-assets-v1.json",
                           "FICTIONAL-ASSET-20260927-001", auth, "user_test", source)
        failed_job = dispatch_next(asset_state, "FICTIONAL-ASSET-20260927-001")
        invalid = Path(self.temp.name) / "invalid.png"
        Image.new("RGB", (128, 96), "white").save(invalid)
        reject_image(asset_state, failed_job["job_id"], invalid, self.evidence,
                     "native output not valid RGBA")
        followup = create_asset_batch(
            asset_state, ROOT / "templates/runtime-fictional-kids-catalog-v1.json",
            "FICTIONAL-ASSET-20260927-CATALOG", auth, "user_test", source)
        self.assertEqual(followup["run"]["authorization"]["reserved"], 1)
        self.assertEqual(followup["run"]["authorization"]["remaining"], 5)
        second = dispatch_next(asset_state, "FICTIONAL-ASSET-20260927-CATALOG")
        self.assertEqual(second["budget"]["reserved"], 2)
        self.assertEqual(second["budget"]["remaining"], 4)

    def test_missing_frozen_reference_blocks_dispatch_without_spending(self):
        from factory.runtime import Blocked, Runtime
        from factory.runtime_runner import runtime_db

        asset_state = Path(self.temp.name) / "asset-state"
        run = "FICTIONAL-ASSET-20260927-CATALOG"
        result = create_asset_batch(
            asset_state, ROOT / "templates/runtime-fictional-kids-catalog-v1.json",
            run, "AUTH-FICTIONAL-ASSET-20260927-6", "user_test", self.product())
        Path(result["reference_asset"]["local_path"]).unlink()
        with self.assertRaises(Blocked):
            dispatch_next(asset_state, run)
        runtime = Runtime(runtime_db(asset_state), single_instance=True)
        try:
            self.assertEqual(runtime.status(run)["authorization"]["reserved"], 0)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
