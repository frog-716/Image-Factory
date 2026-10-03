import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image, ImageDraw

from factory.mock import MockBase
from factory.runtime_runner import advance_local, create_demo, dispatch_next, stage_image
from factory.state import State
from factory.util import FactoryError, file_hash
from factory.v1_live import (project_status_outbox, publish_candidates, seed_demo,
                             sync_review_to_runtime)


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

    def prepare_import_projection(self, *, operation="import_product_source"):
        seeded = seed_demo(self.base, self.state, self.run_id)
        task_id = seeded["task_record_id"]
        self.base.update_record("tasks", task_id, {"任务名": "表单提交的任务"})
        product_id = seeded["product_record_id"]

        original = self.image("product")
        source_path = self.state_root / "runtime-runs" / self.run_id / "product_source_imported.png"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(original.read_bytes())
        source_sha = file_hash(source_path)
        source = self.base.create_record("assets", {
            "素材名": "已授权商品原图", "资产ID": "FORM-ORIGINAL-001",
            "类型": ["商品原图"], "商品": [product_id], "来源任务": [],
            "来源说明": "离线测试原图来源", "授权说明": "离线测试生图授权",
            "允许用于生图": True, "SHA256": source_sha, "示例数据": True,
        })
        self.base.upload_attachment("assets", source["record_id"], "图片", original)
        self.base.update_record("products", product_id,
                                {"默认商品素材": [source["record_id"]]})

        outputs = {}
        for scene in ("white", "outdoor", "indoor", "studio"):
            path = self.state_root / "runtime-runs" / self.run_id / (scene + ".png")
            Image.new("RGB", (256, 256), (210, 225, 235)).save(path)
            outputs[scene] = {"name": path.name,
                              "local_path": str(path.relative_to(self.state_root)),
                              "sha256": file_hash(path)}

        class RuntimeStub:
            def __init__(self):
                self.plan_value = {
                    "run_id": self_run_id,
                    "form_task_record_id": task_id,
                    "intake_binding": {"task_projection": {
                        "商品": [product_id], "流程": [seeded["workflow_record_id"]],
                    }},
                    "steps": [{
                        "id": "product-source", "kind": "compose",
                        "operation": operation,
                        "reference_asset": {
                            "asset_id": source["record_id"],
                            "business_asset_id": "FORM-ORIGINAL-001",
                            "sha256": source_sha,
                        },
                    }],
                }

            def next(self, run_id):
                return {"state": "waiting_review"}

            def plan(self, run_id):
                return self.plan_value

            def step_output(self, run_id, step_id):
                if step_id == "product-source":
                    return {"outputs": [{**outputs["white"],
                                         "local_path": str(source_path.relative_to(self_state_root)),
                                         "sha256": source_sha,
                                         "source_asset_id": source["record_id"]}]}
                if step_id == "white-preview":
                    return {"output": outputs["white"]}
                if step_id.startswith("background-"):
                    return {"outputs": [outputs[step_id.removeprefix("background-")] ]}
                if step_id == "candidate-composition":
                    return {"candidates": {
                        scene: {"output": outputs[scene]} for scene in ("outdoor", "indoor", "studio")
                    }}
                raise AssertionError(step_id)

            def confirmed_review_records(self, run_id):
                return {}

            def close(self):
                pass

        self_run_id = self.run_id
        self_state_root = self.state_root
        return seeded, source, RuntimeStub

    def test_imported_product_source_reuses_authorized_asset_without_default_write(self):
        seeded, source, runtime = self.prepare_import_projection()
        keep_default = self.base.create_record("assets", {
            "素材名": "原有默认素材", "资产ID": "KEEP-DEFAULT-001",
            "类型": ["商品原图"], "商品": [seeded["product_record_id"]],
            "来源说明": "离线测试", "授权说明": "离线测试", "允许用于生图": True,
            "SHA256": source["fields"]["SHA256"], "示例数据": True,
        })
        self.base.upload_attachment("assets", keep_default["record_id"], "图片", self.image("product"))
        self.base.update_record("products", seeded["product_record_id"],
                                {"默认商品素材": [keep_default["record_id"]]})
        initial_assets = self.base.list("assets")

        with patch("factory.v1_live.Runtime", return_value=runtime()):
            published = publish_candidates(self.base, self.state, self.state_root, self.run_id)

        product = self.base.get_record("products", seeded["product_record_id"])["fields"]
        self.assertEqual(published["assets"][self.run_id + "-PRODUCT-RGBA"], source["record_id"])
        self.assertEqual(published["task_record_id"], seeded["task_record_id"])
        self.assertEqual(product["默认商品素材"], [keep_default["record_id"]])
        self.assertEqual(len(self.base.list("assets")) - len(initial_assets), 7)
        source_after = self.base.get_record("assets", source["record_id"])["fields"]
        self.assertEqual(len(source_after["图片"]), 1)
        self.assertEqual(source_after["授权说明"], "离线测试生图授权")
        derived_authorization = "基于已授权商品素材的 Demo 派生图，仅供演示审核；禁止真实发布投放"
        white_fields = self.base.get_record(
            "assets", published["assets"][self.run_id + "-PRODUCT-WHITE"])["fields"]
        self.assertEqual(white_fields["授权说明"], derived_authorization)
        for scene, slot in (("outdoor", "A"), ("indoor", "B"), ("studio", "C")):
            for suffix, label in (("BG-" + scene.upper(), "背景方案 " + slot),
                                  ("CAND-" + scene.upper(), "候选 " + slot)):
                record_id = published["assets"][self.run_id + "-" + suffix]
                fields = self.base.get_record("assets", record_id)["fields"]
                self.assertEqual(fields["授权说明"], derived_authorization)
                self.assertEqual(fields["素材名"], self.run_id + " / " + label)

        with patch("factory.v1_live.Runtime", return_value=runtime()):
            review_sync = sync_review_to_runtime(self.base, self.state_root, self.run_id)
        self.assertEqual(review_sync["status"], "waiting")
        self.assertEqual(len(review_sync["missing"]), 3)

    def test_form_source_cannot_be_projected_to_another_product_after_admission(self):
        seeded, source, runtime = self.prepare_import_projection()
        another = self.base.create_record("products", {
            "商品名": "另一款商品", "SKU": "DIFFERENT-SKU", "示例数据": True,
        })["record_id"]
        self.base.update_record("tasks", seeded["task_record_id"], {"商品": [another]})
        self.base.update_record("assets", source["record_id"], {"商品": [another]})
        count_before = len(self.base.list("assets"))

        with patch("factory.v1_live.Runtime", return_value=runtime()):
            with self.assertRaisesRegex(FactoryError, "冻结|商品"):
                publish_candidates(self.base, self.state, self.state_root, self.run_id)

        self.assertEqual(len(self.base.list("assets")), count_before)

    def test_status_projector_resolves_form_task_by_frozen_record_id(self):
        seeded = seed_demo(self.base, self.state, self.run_id)
        self.base.update_record("tasks", seeded["task_record_id"], {"任务名": "表单任务名称"})
        plan = {
            "form_task_record_id": seeded["task_record_id"],
            "intake_binding": {"task_projection": {
                "商品": [seeded["product_record_id"]],
                "流程": [seeded["workflow_record_id"]],
            }},
        }

        class StatusRuntime:
            def __init__(self):
                self.claimed = False

            def plan(self, run_id):
                return plan

            def claim_outbox(self, owner, run_id=None):
                if run_id != self_run_id:
                    raise AssertionError("status projector must scope claims to its run")
                if self.claimed:
                    return None
                self.claimed = True
                return {"op_id": "status-test", "run_id": self_run_id,
                        "kind": "status_projection", "payload": {
                            "event_seq": 2, "state": "waiting_worker"},
                        "payload_hash": "offline-test-hash"}

            def confirm_outbox(self, *args, **kwargs):
                pass

            def pending_outbox(self):
                return []

            def unknown_outbox(self, *args, **kwargs):
                raise AssertionError("unexpected unknown outbox")

            def close(self):
                pass

        self_run_id = self.run_id
        with patch("factory.v1_live.Runtime", return_value=StatusRuntime()):
            result = project_status_outbox(self.base, self.state_root, self.run_id)

        task = self.base.get_record("tasks", seeded["task_record_id"])["fields"]
        self.assertEqual(result["confirmed"], ["status-test"])
        self.assertEqual(task["运行ID"], self.run_id)
        self.assertEqual(task["系统状态"], ["生成中"])

    def test_source_asset_id_without_import_operation_is_not_an_opt_in(self):
        _, _, runtime = self.prepare_import_projection(operation="not-import")
        initial_assets = self.base.list("assets")

        with patch("factory.v1_live.Runtime", return_value=runtime()):
            with self.assertRaises(FactoryError):
                publish_candidates(self.base, self.state, self.state_root, self.run_id)

        self.assertEqual(self.base.list("assets"), initial_assets)

    def test_imported_product_source_requires_current_permission(self):
        _, source, runtime = self.prepare_import_projection()
        self.base.update_record("assets", source["record_id"], {"允许用于生图": False})
        initial_assets = self.base.list("assets")

        with patch("factory.v1_live.Runtime", return_value=runtime()):
            with self.assertRaises(FactoryError):
                publish_candidates(self.base, self.state, self.state_root, self.run_id)

        self.assertEqual(self.base.list("assets"), initial_assets)

    def test_imported_product_source_requires_matching_product(self):
        seeded, source, runtime = self.prepare_import_projection()
        other_product = self.base.create_record("products", {
            "商品名": "其他离线商品", "SKU": "OTHER-001", "示例数据": True,
        })
        self.base.update_record("assets", source["record_id"], {"商品": [other_product["record_id"]]})
        initial_assets = self.base.list("assets")

        with patch("factory.v1_live.Runtime", return_value=runtime()):
            with self.assertRaises(FactoryError):
                publish_candidates(self.base, self.state, self.state_root, self.run_id)

        self.assertEqual(self.base.list("assets"), initial_assets)

    def test_imported_product_source_requires_registered_sha_to_match(self):
        _, source, runtime = self.prepare_import_projection()
        self.base.update_record("assets", source["record_id"], {"SHA256": "0" * 64})
        initial_assets = self.base.list("assets")

        with patch("factory.v1_live.Runtime", return_value=runtime()):
            with self.assertRaises(FactoryError):
                publish_candidates(self.base, self.state, self.state_root, self.run_id)

        self.assertEqual(self.base.list("assets"), initial_assets)

    def test_imported_product_source_requires_single_matching_attachment(self):
        _, source, runtime = self.prepare_import_projection()
        replacement = self.image("different")
        attachment = self.base.upload_attachment("assets", source["record_id"], "图片", replacement)
        self.base.update_record("assets", source["record_id"], {"图片": [attachment]})
        initial_assets = self.base.list("assets")

        with patch("factory.v1_live.Runtime", return_value=runtime()):
            with self.assertRaises(FactoryError):
                publish_candidates(self.base, self.state, self.state_root, self.run_id)

        self.assertEqual(self.base.list("assets"), initial_assets)

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

    def test_status_projector_does_not_claim_another_runs_pending_outbox(self):
        from factory.runtime import Runtime
        from factory.runtime_runner import runtime_db

        other_run_id = "V1-DEMO-KIDS-OTHER-001"
        seed_demo(self.base, self.state, self.run_id)
        seed_demo(self.base, self.state, other_run_id)

        # Clear the target run's creation event so the other run's new event is
        # deliberately first in the shared outbox queue.
        runtime = Runtime(runtime_db(self.state_root))
        try:
            for operation in runtime.pending_outbox():
                runtime.confirm_outbox(
                    operation["op_id"],
                    expected_hash=operation["payload_hash"],
                    observed_hash=operation["payload_hash"],
                    receipt={"test_setup": "settled creation event"},
                )
        finally:
            runtime.close()

        create_demo(self.state_root, ROOT / "templates/runtime-workflow-kids-demo-v1.json",
                    other_run_id, "AUTH-PROJECTION-OTHER-6", "user_test")

        runtime = Runtime(runtime_db(self.state_root))
        try:
            target_op_id = runtime.enqueue_status(
                self.run_id, {"event_seq": 200, "state": "waiting_review"})
            pending_before = runtime.pending_outbox()
            self.assertEqual(pending_before[0]["run_id"], other_run_id)
            self.assertEqual(pending_before[-1]["op_id"], target_op_id)
        finally:
            runtime.close()

        result = project_status_outbox(self.base, self.state_root, self.run_id)

        runtime = Runtime(runtime_db(self.state_root))
        try:
            pending_after = runtime.pending_outbox()
            other_operations = [operation for operation in pending_after
                                if operation["run_id"] == other_run_id]
            self.assertTrue(other_operations)
            self.assertTrue(all(operation["state"] == "pending"
                                for operation in other_operations))
            self.assertNotIn(target_op_id,
                             {operation["op_id"] for operation in pending_after})
        finally:
            runtime.close()

        self.assertEqual(result["confirmed"], [target_op_id])

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
