import tempfile
import http.client
import json
import threading
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from factory.human_ui_server import HumanReviewEngine, build_server
from factory.mock import MockBase
from factory.runtime import Blocked, Runtime
from factory.runtime_runner import advance_local, create_demo, dispatch_next, runtime_db, stage_image
from factory.state import State
from factory.util import UnknownWrite
from factory.v1_live import publish_candidates, seed_demo, sync_review_to_runtime


ROOT = Path(__file__).resolve().parents[1]


class ActorMockBase(MockBase):
    """Offline Feishu double whose system creator is the authenticated actor."""

    def __init__(self, root: Path, actor_id: str = "ou_human"):
        super().__init__(root)
        self.actor_id = actor_id
        self.config = {"reviewer_open_ids": [actor_id]}
        self.create_attempts = 0
        self.unknown_after_create = False

    def create_record(self, table, fields):
        if table != "reviews":
            return super().create_record(table, fields)
        self.create_attempts += 1
        stored = dict(fields)
        stored["创建人"] = [{"id": self.actor_id}]
        stored["创建时间"] = f"2026-09-21T08:00:{self.create_attempts:02d}+00:00"
        row = super().create_record(table, stored)
        if self.unknown_after_create:
            self.unknown_after_create = False
            raise UnknownWrite("offline response lost after commit")
        return row


class HumanReviewEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_root = self.root / "state"
        self.run_id = "V1-DEMO-KIDS-HUMAN-UI-001"
        self.actor = "ou_human"
        self.base = ActorMockBase(self.root / "remote", self.actor)
        self.state = State(self.state_root)
        seed_demo(self.base, self.state, self.run_id)
        create_demo(
            self.state_root,
            ROOT / "templates/runtime-workflow-kids-demo-v1.json",
            self.run_id,
            "AUTH-HUMAN-UI-6",
            "user_test",
        )
        evidence = self.root / "evidence.txt"
        evidence.write_text("OFFLINE TEST ONLY\n", encoding="utf-8")
        for role in ("product", "outdoor", "indoor", "studio"):
            image = self.root / f"{role}.png"
            if role == "product":
                canvas = Image.new("RGBA", (128, 96), (255, 255, 255, 0))
                ImageDraw.Draw(canvas).rounded_rectangle(
                    (16, 20, 112, 80), radius=8, fill=(245, 248, 255, 255)
                )
            else:
                canvas = Image.new("RGB", (256, 256), (210, 225, 235))
            canvas.save(image)
            job = dispatch_next(self.state_root, self.run_id)
            stage_image(
                self.state_root,
                job["job_id"],
                image,
                evidence,
                "mock-test",
                "offline-" + role,
            )
            advance_local(self.state_root, self.run_id)
        publish_candidates(self.base, self.state, self.state_root, self.run_id)
        self.engine = HumanReviewEngine(
            self.base, self.state_root, self.run_id, reviewer_id=self.actor
        )

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_pending_contract_exposes_only_human_fields_and_opaque_tokens(self):
        result = self.engine.pending_reviews()
        self.assertEqual(len(result["items"]), 3)
        forbidden = {
            "sha256",
            "asset_id",
            "run_id",
            "workflow_id",
            "policy",
            "review_version",
            "reviewer_id",
            "mode",
        }
        for item in result["items"]:
            self.assertFalse(forbidden.intersection(item))
            self.assertEqual(
                set(item), {"review_token", "display", "status"}
            )
            self.assertFalse(forbidden.intersection(item["display"]))
            self.assertNotIn(self.run_id, item["review_token"])
            self.assertNotIn("CAND", item["review_token"])
            self.assertTrue(self.engine.review_image(item["review_token"]).is_file())

    def test_submit_binds_current_runtime_candidate_policy_actor_and_version(self):
        item = self.engine.pending_reviews()["items"][0]
        result = self.engine.submit_review(item["review_token"], "approved", "")
        self.assertEqual(result, {"status": "reviewed", "decision": "approved"})
        rows = self.base.list("reviews")
        self.assertEqual(len(rows), 1)
        fields = rows[0]["fields"]
        self.assertEqual(fields["审核人"], [self.actor])
        self.assertEqual(fields["审核策略版本"], "demo-v2")
        self.assertEqual(fields["运行ID"], self.run_id)
        self.assertEqual(fields["审核版本"], 1)
        self.assertTrue(fields["人工确认"])
        self.assertTrue(fields["Demo视觉检查"])
        self.assertTrue(fields["仅限演示使用"])
        self.assertFalse(fields["商品准确"])
        self.assertFalse(fields["品牌及渠道检查"])
        self.assertEqual(fields["创建人"], [{"id": self.actor}])

    def test_double_click_and_retry_after_lost_response_create_one_review(self):
        item = self.engine.pending_reviews()["items"][0]
        self.base.unknown_after_create = True
        first = self.engine.submit_review(item["review_token"], "rejected", "边缘需要检查")
        second = self.engine.submit_review(item["review_token"], "rejected", "边缘需要检查")
        self.assertEqual(first, second)
        self.assertEqual(first, {"status": "reviewed", "decision": "rejected"})
        self.assertEqual(len(self.base.list("reviews")), 1)
        self.assertEqual(self.base.create_attempts, 1)

    def test_confirmed_review_wakes_runner_once_but_duplicate_does_not(self):
        wakes = []
        engine = HumanReviewEngine(
            self.base,
            self.state_root,
            self.run_id,
            reviewer_id=self.actor,
            on_review_confirmed=lambda: wakes.append("wake"),
        )
        item = engine.pending_reviews()["items"][0]
        engine.submit_review(item["review_token"], "approved", "")
        engine.submit_review(item["review_token"], "approved", "")
        self.assertEqual(wakes, ["wake"])

    def test_changed_candidate_rejects_stale_page_without_remote_write(self):
        item = self.engine.pending_reviews()["items"][0]
        candidate = next(
            row for row in self.base.list("assets")
            if row["fields"].get("资产角色") == ["candidate"]
            and row["fields"].get("槽位") == "A"
        )
        self.base.patch("assets", candidate["record_id"], {"SHA256": "f" * 64})
        with self.assertRaisesRegex(Blocked, "候选.*变化|过期"):
            self.engine.submit_review(item["review_token"], "approved", "")
        self.assertEqual(self.base.list("reviews"), [])

    def test_invalid_or_extra_decision_data_is_rejected_before_write(self):
        item = self.engine.pending_reviews()["items"][0]
        with self.assertRaisesRegex(ValueError, "decision"):
            self.engine.submit_review(item["review_token"], "approve", "")
        with self.assertRaisesRegex(ValueError, "reason"):
            self.engine.submit_review(item["review_token"], "approved", 123)
        self.assertEqual(self.base.list("reviews"), [])

    def test_runner_consumes_only_engine_confirmed_review_rows(self):
        pending = self.engine.pending_reviews()["items"]
        # A structurally plausible manual row is deliberately not journaled by Engine.
        candidate = next(
            row for row in self.base.list("assets")
            if row["fields"].get("资产角色") == ["candidate"]
            and row["fields"].get("槽位") == "A"
        )
        self.base.create(
            "reviews",
            {
                "审核标题": "manual bypass",
                "图片资产": [candidate["record_id"]],
                "目标SHA256": candidate["fields"]["SHA256"],
                "结论": ["通过"],
                "审核人": [{"id": self.actor}],
                "创建人": [{"id": self.actor}],
                "创建时间": "2026-09-21T07:00:00+00:00",
                "人工确认": True,
                "原因": "manual",
                "Demo视觉检查": True,
                "仅限演示使用": True,
                "审核策略版本": "demo-v2",
                "运行ID": self.run_id,
                "审核版本": 1,
            },
        )
        waiting = sync_review_to_runtime(self.base, self.state_root, self.run_id)
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(len(waiting["missing"]), 3)

        for index, item in enumerate(pending):
            decision = "approved" if index < 2 else "rejected"
            self.engine.submit_review(item["review_token"], decision, "offline")
        first_asset_reviews = [
            row for row in self.base.list("reviews")
            if row["fields"].get("图片资产") == [candidate["record_id"]]
        ]
        self.assertEqual(
            sorted(row["fields"]["审核版本"] for row in first_asset_reviews), [1, 2]
        )
        accepted = sync_review_to_runtime(self.base, self.state_root, self.run_id)
        self.assertEqual(accepted["status"], "accepted")
        runtime = Runtime(runtime_db(self.state_root))
        try:
            self.assertEqual(runtime.next(self.run_id)["kind"], "export")
        finally:
            runtime.close()

    def test_loopback_api_requires_bootstrap_cookie_csrf_and_rejects_extra_fields(self):
        server = build_server(self.engine, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/api/v1/reviews/pending", headers={"Host": f"127.0.0.1:{port}"})
            self.assertEqual(connection.getresponse().status, 403)

            entry = "/?access=" + server.app.bootstrap_token
            connection.request("GET", entry, headers={"Host": f"127.0.0.1:{port}"})
            response = connection.getresponse()
            self.assertEqual(response.status, 303)
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            response.read()

            headers = {"Host": f"127.0.0.1:{port}", "Cookie": cookie}
            connection.request("GET", "/api/v1/session", headers=headers)
            session = json.loads(connection.getresponse().read())
            csrf = session["csrf"]
            connection.request("GET", "/api/v1/reviews/pending", headers=headers)
            review_response = connection.getresponse()
            self.assertEqual(review_response.status, 200)
            raw = review_response.read().decode("utf-8")
            for forbidden in ("sha256", "asset_id", "run_id", "reviewer_id", "policy"):
                self.assertNotIn(forbidden, raw.lower())
            token = json.loads(raw)["items"][0]["review_token"]

            body = json.dumps({"decision": "approved", "reviewer_id": "ou_attacker"})
            post_headers = {
                **headers,
                "Content-Type": "application/json",
                "Content-Length": str(len(body.encode("utf-8"))),
                "X-CSRF-Token": csrf,
            }
            connection.request(
                "POST", f"/api/v1/reviews/{token}/decision", body=body, headers=post_headers
            )
            self.assertEqual(connection.getresponse().status, 400)
            self.assertEqual(self.base.list("reviews"), [])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
