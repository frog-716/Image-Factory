import tempfile
import unittest

from support import Fixture
from factory.demo_live import demo_export
from factory.model import sources
from factory.util import FactoryError, text


class V1PromptComponentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = Fixture(self.temp.name)

    def tearDown(self):
        self.f.close()
        self.temp.cleanup()

    def test_task_can_freeze_selected_prompt_components(self):
        prompt_steps = [
            row for row in self.f.base.list("steps")
            if text(row["fields"].get("节点类型")) == "提示词"
        ]
        self.assertEqual(len(prompt_steps), 2)
        selected = prompt_steps[0]["record_id"]
        self.f.base.patch("tasks", self.f.task, {
            "命名空间": "V1-DEMO-KIDS-TEST",
            "模式": "demo",
            "审核策略版本": "demo-v2",
            "选用提示词组件": [selected],
        })

        frozen = sources(self.f.base, self.f.task, 12, 18, allow_demo=True)

        selected_ids = [
            row["record_id"] for row in frozen["steps"]
            if text(row["fields"].get("节点类型")) == "提示词"
        ]
        self.assertEqual(selected_ids, [selected])
        self.assertEqual(frozen["namespace"], "V1-DEMO-KIDS-TEST")
        self.assertEqual(frozen["task_mode"], "demo")

    def test_non_prompt_component_selection_is_rejected(self):
        image_step = next(
            row for row in self.f.base.list("steps")
            if text(row["fields"].get("节点类型")) == "生图"
        )
        self.f.base.patch("tasks", self.f.task, {
            "命名空间": "V1-DEMO-KIDS-TEST",
            "模式": "demo",
            "审核策略版本": "demo-v2",
            "选用提示词组件": [image_step["record_id"]],
        })
        with self.assertRaises(FactoryError):
            sources(self.f.base, self.f.task, 12, 18, allow_demo=True)


class V1DemoReviewPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = Fixture(self.temp.name, count=2)
        self.f.base.patch("tasks", self.f.task, {
            "任务名": "V1-DEMO-KIDS-TEST",
            "命名空间": "V1-DEMO-KIDS-TEST",
            "模式": "demo",
            "审核策略版本": "demo-v2",
        })
        self.f.prepare()
        self.f.all_finished()

    def tearDown(self):
        self.f.close()
        self.temp.cleanup()

    def review_v2(self, sid, *, visual=True, demo_only=True):
        return self.f.review(
            sid,
            商品准确=False,
            品牌及渠道检查=False,
            Demo视觉检查=visual,
            仅限演示使用=demo_only,
            审核策略版本="demo-v2",
            运行ID=self.f.rid,
            审核版本=1,
        )

    def test_demo_policy_uses_demo_checks_not_commercial_claims(self):
        self.review_v2("001")
        self.f.review("002", choice="驳回", 商品准确=False, 品牌及渠道检查=False,
                      Demo视觉检查=False, 仅限演示使用=True,
                      审核策略版本="demo-v2", 运行ID=self.f.rid, 审核版本=1)

        result = demo_export(self.f.base, self.f.state, self.f.task)

        self.assertEqual(result["delivered_count"], 1)
        self.assertEqual(result["manifest"]["review_policy_version"], "demo-v2")

    def test_demo_policy_missing_visual_or_scope_check_is_rejected(self):
        self.review_v2("001", visual=False)
        self.review_v2("002")
        with self.assertRaises(FactoryError):
            demo_export(self.f.base, self.f.state, self.f.task)

    def test_demo_review_must_bind_runtime_run(self):
        self.f.review("001", 商品准确=False, 品牌及渠道检查=False,
                      Demo视觉检查=True, 仅限演示使用=True,
                      审核策略版本="demo-v2", 运行ID="wrong", 审核版本=1)
        self.review_v2("002")
        with self.assertRaises(FactoryError):
            demo_export(self.f.base, self.f.state, self.f.task)


if __name__ == "__main__":
    unittest.main()
