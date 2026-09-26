import csv
import tempfile
import unittest
from pathlib import Path

from support import Fixture
from factory.demo_live import _same_cell, create_demo_usage, demo_export, demo_import_metrics, demo_metric_summary
from factory.metrics import COLUMNS
from factory.sync import metric_summary
from factory.util import FactoryError


class DemoLivePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = Fixture(self.temp.name, count=2)
        self.f.prepare()
        self.f.all_finished()

    def tearDown(self):
        self.f.close()
        self.temp.cleanup()

    def approve_all(self):
        self.f.review("001")
        self.f.review("002")

    def test_demo_readback_accepts_unicode_select_values(self):
        self.assertTrue(_same_cell(["场景参考"], ["场景参考"]))

    def test_demo_readback_accepts_feishu_float_normalization(self):
        self.assertTrue(_same_cell(0.0416666666666666, 0.041666666666666664))

    def write_csv(self, placement_id="DEMO-USE-001", source_kind="demo"):
        path = Path(self.temp.name) / "demo-metrics.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerow({
                "placement_id": placement_id,
                "date": "2026-09-02",
                "timezone": "Asia/Shanghai",
                "channel": "DEMO",
                "impressions": "1000",
                "clicks": "30",
                "orders": "2",
                "metric_definition": "演示固定测试口径",
                "source_kind": source_kind,
            })
        return path

    def test_demo_export_rejects_pending_review(self):
        with self.assertRaises(FactoryError):
            demo_export(self.f.base, self.f.state, self.f.task)

    def test_demo_export_rejects_formal_asset(self):
        self.f.approved = self.f.review("001")
        self.f.review("002")
        asset = self.f.state.run(self.f.rid)["slots"]["001"]["asset_record_id"]
        self.f.base.patch("assets", asset, {"示例数据": False})
        with self.assertRaises(FactoryError):
            demo_export(self.f.base, self.f.state, self.f.task)

    def test_demo_export_accepts_pregenerated_demo_reference_asset(self):
        self.f.review("001")
        self.f.review("002")
        asset = self.f.state.run(self.f.rid)["slots"]["001"]["asset_record_id"]
        self.f.base.patch("assets", asset, {"类型": "场景参考"})
        result = demo_export(self.f.base, self.f.state, self.f.task)
        self.assertEqual(result["delivered_count"], 2)
        self.assertTrue(result["round_trip_sha256_equal"])

    def test_demo_export_rejects_sha_mismatch(self):
        self.f.review("001")
        self.f.review("002")
        asset = self.f.state.run(self.f.rid)["slots"]["001"]["asset_record_id"]
        self.f.base.patch("assets", asset, {"SHA256": "0" * 64})
        with self.assertRaises(FactoryError):
            demo_export(self.f.base, self.f.state, self.f.task)

    def test_demo_export_round_trip_contains_only_approved_assets(self):
        self.f.review("001")
        self.f.review("002", choice="驳回")
        result = demo_export(self.f.base, self.f.state, self.f.task)
        self.assertEqual(result["delivered_count"], 1)
        self.assertTrue(result["round_trip_sha256_equal"])
        self.assertTrue(Path(result["zip"]).is_file())
        task = self.f.base.get("tasks", self.f.task)["fields"]
        self.assertEqual(len(task["已批准交付包"]), 1)

    def test_demo_metrics_reject_formal_usage_and_isolate_summary(self):
        self.approve_all()
        asset = self.f.state.run(self.f.rid)["slots"]["001"]["asset_record_id"]
        self.f.base.create("placements", {
            "使用记录名": "FORMAL RECORD MUST NOT ENTER DEMO",
            "使用ID": "FORMAL-USE-001",
            "图片资产": [asset],
            "目标SHA256": self.f.base.get("assets", asset)["fields"]["SHA256"],
            "渠道": "DEMO",
            "人工确认已上线": True,
        })
        with self.assertRaises(FactoryError):
            demo_import_metrics(self.f.base, self.f.state, self.f.task,
                                self.write_csv("FORMAL-USE-001"))

        usage = create_demo_usage(self.f.base, self.f.state, self.f.task, asset,
                                  "DEMO-USE-001", "DEMO", "DEMO-ACCOUNT", "DEMO-PAGE", "DEMO-PAGE-001",
                                  "2026-09-01T00:00:00+08:00")
        csv_path = self.write_csv()
        first = demo_import_metrics(self.f.base, self.f.state, self.f.task, csv_path)
        second = demo_import_metrics(self.f.base, self.f.state, self.f.task, csv_path)
        self.assertEqual(len(first["records"]), 1)
        self.assertEqual(second["records"][0]["status"], "existing")
        self.assertEqual(len(self.f.base.list("metrics")), 1)
        self.assertEqual(len(demo_metric_summary(self.f.base, self.f.task)), 1)
        self.assertEqual(metric_summary(self.f.base), [])
        self.assertEqual(metric_summary(self.f.base, include_demo=False), [])
        self.assertEqual(usage["status"], "created")

    def test_demo_metrics_reject_non_demo_csv(self):
        self.approve_all()
        asset = self.f.state.run(self.f.rid)["slots"]["001"]["asset_record_id"]
        create_demo_usage(self.f.base, self.f.state, self.f.task, asset,
                          "DEMO-USE-001", "DEMO", "DEMO-ACCOUNT", "DEMO-PAGE", "DEMO-PAGE-001",
                          "2026-09-01T00:00:00+08:00")
        with self.assertRaises(FactoryError):
            demo_import_metrics(self.f.base, self.f.state, self.f.task,
                                self.write_csv(source_kind="platform_export"))


if __name__ == "__main__":
    unittest.main()
