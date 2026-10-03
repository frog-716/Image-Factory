import tempfile
import http.client
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from factory.human_ui_server import HumanReviewEngine, build_server, serve_human_ui
from factory.mock import MockBase
from factory.runtime import Blocked, ContractError, Runtime
from factory.runtime_runner import advance_local, create_demo, dispatch_next, runtime_db, stage_image
from factory.state import State
from factory.util import UnknownWrite
from factory.v1_live import publish_candidates, seed_demo, sync_review_to_runtime


ROOT = Path(__file__).resolve().parents[1]


class HumanUIStartupTests(unittest.TestCase):
    def test_stale_active_run_is_rejected_before_feishu_or_browser_start(self):
        class IdentityNeverCalled:
            def current_user(self):
                raise AssertionError("Feishu should not be called for a missing run")

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ContractError, "Unknown run"):
                serve_human_ui(IdentityNeverCalled(), Path(temporary), "DELETED-RUN", port=0)


class ActorMockBase(MockBase):
    """Offline Feishu double whose system creator is the authenticated actor."""

    def __init__(self, root: Path, actor_id: str = "ou_human"):
        super().__init__(root)
        self.actor_id = actor_id
        self.config = {"reviewer_open_ids": [actor_id]}
        self.create_attempts = 0
        self.unknown_after_create = False
        self.identity_calls = 0

    def current_user(self):
        self.identity_calls += 1
        return {"open_id": self.actor_id}

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
            self.assertIn("仅限演示", item["display"]["approval_confirmation"])
            self.assertIn("不代表可上架或投放", item["display"]["approval_confirmation"])

    def test_form_task_candidate_lookup_uses_frozen_record_not_business_title(self):
        task = self.base.find_unique("tasks", "任务名", self.run_id)
        self.base.patch("tasks", task["record_id"], {"任务名": "我给童鞋起的任务名"})
        real = Runtime(runtime_db(self.state_root))

        class FormRuntime:
            def __init__(self, wrapped, task_id):
                self.wrapped = wrapped
                self.task_id = task_id

            def plan(self, run_id):
                return {**self.wrapped.plan(run_id),
                        "form_task_record_id": self.task_id,
                        "intake_binding": {"task_projection": {
                            "商品": task["fields"]["商品"],
                            "流程": task["fields"]["流程"],
                        }}}

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

        try:
            rows = self.engine._candidate_rows(FormRuntime(real, task["record_id"]))
            self.assertEqual(len(rows), 3)
        finally:
            real.close()

    def test_form_review_labels_do_not_claim_other_scenes_or_fictional_product(self):
        task = self.base.find_unique("tasks", "任务名", self.run_id)
        self.base.patch("tasks", task["record_id"], {"任务名": "我给童鞋起的任务名"})
        original_runtime = self.engine._runtime

        class FormRuntime:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def plan(self, run_id):
                return {**self.wrapped.plan(run_id),
                        "form_task_record_id": task["record_id"],
                        "intake_binding": {"task_projection": {
                            "商品": task["fields"]["商品"],
                            "流程": task["fields"]["流程"],
                        }}}

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

        with patch.object(self.engine, "_runtime", side_effect=lambda: FormRuntime(original_runtime())):
            items = self.engine.pending_reviews()["items"]
        self.assertEqual([item["display"]["title"] for item in items],
                         ["候选 A", "候选 B", "候选 C"])
        for item in items:
            confirmation = item["display"]["approval_confirmation"]
            self.assertIn("仅限演示", confirmation)
            self.assertNotIn("虚构商品", confirmation)

    def test_form_review_blocks_changed_product_after_candidate_projection(self):
        task = self.base.find_unique("tasks", "任务名", self.run_id)
        original_product = task["fields"]["商品"]
        original_flow = task["fields"]["流程"]
        another = self.base.create_record("products", {"商品名": "另一款", "SKU": "OTHER"})
        self.base.patch("tasks", task["record_id"], {"商品": [another["record_id"]]})
        real = Runtime(runtime_db(self.state_root))

        class FormRuntime:
            def plan(self, run_id):
                return {**real.plan(run_id), "form_task_record_id": task["record_id"],
                        "intake_binding": {"task_projection": {
                            "商品": original_product, "流程": original_flow,
                        }}}

            def __getattr__(self, name):
                return getattr(real, name)

        try:
            with self.assertRaisesRegex(Blocked, "冻结|商品"):
                self.engine._candidate_rows(FormRuntime())
        finally:
            real.close()

    def test_unknown_runtime_mode_fails_closed_before_review_list(self):
        class UnknownModeRuntime:
            def status(self, run_id):
                return {
                    "state": "waiting_review",
                    "next": {"kind": "human_review", "step": "human-review"},
                }

            def plan(self, run_id):
                return {
                    "mode": "unexpected",
                    "steps": [{"id": "human-review", "kind": "human_review", "policy": "custom-v1"}],
                }

        with self.assertRaisesRegex(Blocked, "模式"):
            self.engine._runtime_review_contract(UnknownModeRuntime())

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

    def test_review_revalidation_download_uses_larkcli_allowed_temp_root(self):
        item = self.engine.pending_reviews()["items"][0]
        allowed_root = Path("/tmp").resolve()

        def stop_after_checking_path(table, record_id, attachment, destination):
            path = Path(destination).resolve()
            self.assertTrue(
                path.is_relative_to(allowed_root),
                f"Feishu CLI only allows temporary downloads under /tmp; got {path}",
            )
            raise RuntimeError("stop before writing review")

        with patch.object(
            self.base, "download_attachment", side_effect=stop_after_checking_path,
        ), self.assertRaisesRegex(RuntimeError, "stop before writing review"):
            self.engine.submit_review(item["review_token"], "approved", "")

        self.assertEqual(self.base.list("reviews"), [])

    def test_switched_feishu_identity_is_rejected_before_review_write(self):
        item = self.engine.pending_reviews()["items"][0]
        self.base.actor_id = "ou_switched_account"

        with self.assertRaises(Blocked):
            self.engine.submit_review(item["review_token"], "approved", "")

        self.assertEqual(self.base.create_attempts, 0)
        self.assertEqual(self.base.list("reviews"), [])

    def test_identity_change_at_last_check_does_not_leave_pending_review(self):
        item = self.engine.pending_reviews()["items"][0]
        with patch.object(self.base, "current_user", side_effect=[
            {"open_id": self.actor}, {"open_id": "ou_switched_account"},
        ]):
            with self.assertRaises(Blocked):
                self.engine.submit_review(item["review_token"], "approved", "")

        self.assertEqual(self.base.create_attempts, 0)
        self.assertEqual(self.base.list("reviews"), [])
        retried = self.engine.submit_review(item["review_token"], "approved", "")
        self.assertEqual(retried, {"status": "reviewed", "decision": "approved"})
        self.assertEqual(self.base.create_attempts, 1)

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
            on_review_confirmed=lambda run_id: wakes.append(run_id),
        )
        item = engine.pending_reviews()["items"][0]
        engine.submit_review(item["review_token"], "approved", "")
        engine.submit_review(item["review_token"], "approved", "")
        self.assertEqual(wakes, [self.run_id])

    def test_task_view_says_reviews_submitted_while_runner_has_not_advanced(self):
        for item in self.engine.pending_reviews()["items"]:
            self.engine.submit_review(item["review_token"], "approved", "")
        display = self.engine.task_list()["items"][0]["display"]
        self.assertEqual(display["status"], "审核已提交")
        self.assertIn("等待运行器", display["detail"])
        self.assertIsNone(display["delivery_url"])

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

    def test_http_ui_starts_empty_then_tracks_active_run_and_invalidates_old_tokens(self):
        # A persisted Runtime is not implicitly active; the user can still open
        # the two-page app while no active-v1-run pointer exists.
        (self.state_root / "active-v1-run.json").unlink()
        server, thread, patcher = self._start_served_ui(run_id=None)
        self.assertEqual(self.base.identity_calls, 1)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            cookie, csrf = self._open_session(connection, server)
            headers = {"Host": f"127.0.0.1:{server.server_address[1]}", "Cookie": cookie}

            self.assertEqual(self._get_json(connection, "/api/v1/reviews/pending", headers), {"items": []})
            self.assertEqual(self._get_json(connection, "/api/v1/tasks", headers), {"items": []})

            # Refreshes resolve the current pointer. The first Runtime is already
            # waiting for review, so its candidates become visible now.
            from factory.util import write_json
            write_json(self.state_root / "active-v1-run.json", {"run_id": self.run_id})
            first = self._get_json(connection, "/api/v1/reviews/pending", headers)
            self.assertEqual(len(first["items"]), 3)
            old_token = first["items"][0]["review_token"]

            second_run = "V1-DEMO-KIDS-HUMAN-UI-ALT-002"
            create_demo(
                self.state_root,
                ROOT / "templates/runtime-workflow-kids-demo-v1.json",
                second_run,
                "AUTH-HUMAN-UI-ALT-6",
                "user_test",
            )
            task_list = self._get_json(connection, "/api/v1/tasks", headers)
            self.assertEqual(task_list["items"][0]["display"]["status"], "排队中")
            self.assertEqual(self._get_json(connection, "/api/v1/reviews/pending", headers), {"items": []})

            response = self._request(
                connection,
                "GET",
                "/api/v1/reviews/" + old_token + "/image",
                headers,
            )
            self.assertEqual(response.status, 409)
            self.assertIn("无效或已过期", json.loads(response.read())["message"])
            self.assertEqual(self.base.list("reviews"), [])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            patcher.stop()

    def test_http_ui_blocks_malformed_pointer_and_pointer_to_missing_runtime(self):
        (self.state_root / "active-v1-run.json").unlink()
        server, thread, patcher = self._start_served_ui(run_id=None)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            cookie, _ = self._open_session(connection, server)
            headers = {"Host": f"127.0.0.1:{server.server_address[1]}", "Cookie": cookie}

            pointer = self.state_root / "active-v1-run.json"
            pointer.write_text("{invalid", encoding="utf-8")
            malformed = self._request(connection, "GET", "/api/v1/tasks", headers)
            self.assertEqual(malformed.status, 409)
            self.assertIn("active-v1-run", json.loads(malformed.read())["message"])

            from factory.util import write_json
            write_json(pointer, {"run_id": "V1-DEMO-KIDS-MISSING-999"})
            missing = self._request(connection, "GET", "/api/v1/tasks", headers)
            self.assertEqual(missing.status, 409)
            self.assertIn("Runtime", json.loads(missing.read())["message"])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            patcher.stop()

    def test_http_review_wakeup_and_deferred_audit_use_active_run_id(self):
        server, thread, server_patcher = self._start_served_ui(run_id=None)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        wake_finished = threading.Event()
        runner_ids = []
        audit_ids = []
        try:
            cookie, csrf = self._open_session(connection, server)
            host = f"127.0.0.1:{server.server_address[1]}"
            headers = {"Host": host, "Cookie": cookie}
            pending = self._get_json(connection, "/api/v1/reviews/pending", headers)
            token = pending["items"][0]["review_token"]

            def fail_runner(base, state, state_root, run_id):
                runner_ids.append(run_id)
                raise RuntimeError("offline runner failure")

            original_audit = Runtime.audit

            def capture_deferred_audit(runtime, run_id, event, detail):
                if event == "human_ui_runner_wake_deferred":
                    audit_ids.append(run_id)
                    wake_finished.set()
                return original_audit(runtime, run_id, event, detail)

            with patch("factory.runner_service.run_once", side_effect=fail_runner), patch.object(
                Runtime, "audit", capture_deferred_audit
            ):
                body = json.dumps({"decision": "rejected", "reason": "offline contract test"})
                post_headers = {
                    **headers,
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body.encode("utf-8"))),
                    "X-CSRF-Token": csrf,
                }
                response = self._request(
                    connection,
                    "POST",
                    f"/api/v1/reviews/{token}/decision",
                    post_headers,
                    body,
                )
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()),
                                 {"status": "reviewed", "decision": "rejected"})
                self.assertTrue(wake_finished.wait(3), "wake error was not journaled")

            self.assertEqual(runner_ids, [self.run_id])
            self.assertEqual(audit_ids, [self.run_id])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            server_patcher.stop()

    def _start_served_ui(self, run_id):
        import factory.human_ui_server as human_ui_server

        ready = threading.Event()
        created = []
        original = human_ui_server.build_server

        def capture(engine, host="127.0.0.1", port=0):
            server = original(engine, host=host, port=port)
            created.append(server)
            serve_forever = server.serve_forever

            def signal_then_serve(*args, **kwargs):
                ready.set()
                return serve_forever(*args, **kwargs)

            server.serve_forever = signal_then_serve
            return server

        patcher = patch("factory.human_ui_server.build_server", side_effect=capture)
        patcher.start()
        errors = []

        def serve():
            try:
                serve_human_ui(self.base, self.state_root, run_id, port=0)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        if not ready.wait(5):
            patcher.stop()
            self.fail(f"Human UI did not start: {errors!r}")
        self.assertEqual(errors, [])
        return created[0], thread, patcher

    @staticmethod
    def _request(connection, method, path, headers, body=None):
        connection.request(method, path, body=body, headers=headers)
        return connection.getresponse()

    def _open_session(self, connection, server):
        host = f"127.0.0.1:{server.server_address[1]}"
        response = self._request(
            connection,
            "GET",
            "/?access=" + server.app.bootstrap_token,
            {"Host": host},
        )
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        response.read()
        headers = {"Host": host, "Cookie": cookie}
        csrf = self._get_json(connection, "/api/v1/session", headers)["csrf"]
        return cookie, csrf

    def _get_json(self, connection, path, headers):
        response = self._request(connection, "GET", path, headers)
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200, payload)
        return payload


if __name__ == "__main__":
    unittest.main()
