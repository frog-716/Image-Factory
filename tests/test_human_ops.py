import unittest

from factory.cli import parser
from factory.human_ops import describe_run, describe_runtime, operator_contract


def runtime_status(
    *,
    run_id="V1-DEMO-KIDS-LOCAL-001",
    mode="demo",
    state="waiting_worker",
    next_state=None,
    step="product-source",
    kind="image_generate",
    reserved=1,
    maximum=6,
    unknown_jobs=None,
    pending_outbox=0,
):
    return {
        "run_id": run_id,
        "mode": mode,
        "state": state,
        "next": {
            "state": next_state or state,
            "step": step,
            "kind": kind,
        },
        "image_calls_reserved": reserved,
        "image_calls_max": maximum,
        "unknown_jobs": list(unknown_jobs or []),
        "pending_outbox": pending_outbox,
    }


def review_snapshot(run_id, mode="demo", events=None):
    return {
        "run_id": run_id,
        "mode": mode,
        "candidates": [
            {"asset_id": "CAND-A", "sha256": "a" * 64},
            {"asset_id": "CAND-B", "sha256": "b" * 64},
        ],
        "events": list(events or []),
    }


def approved_event(run_id, asset_id, sha256, *, mode="demo", **checks):
    defaults = {
        "demo_visual_ok": mode == "demo",
        "demo_use_only": mode == "demo",
        "product_accuracy": mode == "production",
        "brand_channel_ok": mode == "production",
    }
    defaults.update(checks)
    return {
        "asset_id": asset_id,
        "revision": 1,
        "run_id": run_id,
        "mode": mode,
        "sha256": sha256,
        "decision": "approve",
        "actor_id": "human-reviewer",
        "human_confirmed": True,
        "reason": "checked",
        **defaults,
    }


class HumanOpsTests(unittest.TestCase):
    def test_product_status_cli_is_read_only_surface(self):
        args = parser().parse_args(["product-v1-status", "--run", "RUN-READ-ONLY"])
        self.assertEqual(args.command, "product-v1-status")
        self.assertEqual(args.run, "RUN-READ-ONLY")

    def test_fixed_operator_contract_separates_editable_and_trusted_fields(self):
        contract = operator_contract().as_dict()

        self.assertEqual(
            contract["flow_phases"],
            [
                "select_product",
                "create_task",
                "wait_for_production",
                "inspect_image",
                "review",
                "download",
            ],
        )
        self.assertEqual(
            contract["human_task_fields"],
            ["product", "purpose", "scene", "style", "quantity", "extra_requirements"],
        )
        self.assertIn("run_id", contract["automatic_task_fields"])
        self.assertEqual(contract["review_visible_fields"], ["large_image", "decision", "reason"])
        self.assertEqual(contract["review_decisions"], ["approve", "reject"])
        self.assertTrue(contract["review_reason_optional"])
        self.assertIn("sha256", contract["trusted_bound_review_fields"])
        self.assertIn("demo_use_only", contract["trusted_bound_review_fields"])
        self.assertTrue(contract["ui_must_not_edit_trusted_bound_fields"])

    def test_waiting_worker_exposes_only_human_wait_action(self):
        view = describe_run(runtime_status())

        self.assertEqual(view.phase, "waiting_worker")
        self.assertEqual(view.next_step, "product-source")
        self.assertEqual(view.allowed_actions, ("wait_for_production",))
        self.assertNotIn("approve", view.allowed_actions)
        self.assertNotIn("dispatch", view.allowed_actions)
        self.assertEqual(view.blockers, ())
        self.assertEqual(view.budget_remaining, 5)

    def test_waiting_review_without_review_rows_explains_missing_human_work(self):
        status = runtime_status(
            state="waiting_review",
            next_state="waiting_review",
            step="human-review",
            kind="human_review",
            reserved=5,
        )
        view = describe_run(status)

        self.assertEqual(view.phase, "waiting_review")
        self.assertEqual(view.allowed_actions, ("review_candidates",))
        self.assertTrue(any(item.code == "review_snapshot_missing" for item in view.blockers))
        self.assertEqual(view.missing_reviews, ("review_snapshot",))
        self.assertFalse(view.is_blocked)
        self.assertIn("人工审核", view.next_instruction)

    def test_review_adapter_waiting_result_is_read_only_missing_context(self):
        run_id = "V1-DEMO-KIDS-LOCAL-005"
        view = describe_run(
            runtime_status(
                run_id=run_id,
                state="waiting_review",
                next_state="waiting_review",
                step="human-review",
                kind="human_review",
            ),
            review={"status": "waiting", "missing": ["CAND-A", "CAND-B"]},
        )

        self.assertEqual(view.missing_reviews, ("CAND-A", "CAND-B"))
        self.assertTrue(any(item.code == "review_missing" for item in view.blockers))

    def test_waiting_review_reports_each_missing_or_incomplete_candidate(self):
        run_id = "V1-DEMO-KIDS-LOCAL-002"
        status = runtime_status(
            run_id=run_id,
            state="waiting_review",
            next_state="waiting_review",
            step="human-review",
            kind="human_review",
        )
        snapshot = review_snapshot(
            run_id,
            events=[
                approved_event(run_id, "CAND-A", "a" * 64, demo_use_only=False),
            ],
        )
        view = describe_run(status, review=snapshot)

        self.assertEqual(view.missing_reviews, ("CAND-B",))
        codes = {item.code for item in view.blockers}
        self.assertIn("review_missing", codes)
        self.assertIn("review_incomplete", codes)
        self.assertIn("demo_use_only", " ".join(view.blockers[0].details + view.blockers[-1].details))

    def test_production_cannot_consume_demo_review_snapshot(self):
        run_id = "RUN-PRODUCTION-001"
        status = runtime_status(
            run_id=run_id,
            mode="production",
            state="waiting_review",
            next_state="waiting_review",
            step="human-review",
            kind="human_review",
        )
        view = describe_run(status, review=review_snapshot(run_id, mode="demo"))

        self.assertEqual(view.phase, "blocked")
        self.assertTrue(any(item.code == "mode_scope_mismatch" for item in view.blockers))
        self.assertNotIn("finish_review", view.allowed_actions)

    def test_demo_and_production_require_different_trusted_checks(self):
        demo_run = "V1-DEMO-KIDS-LOCAL-003"
        demo = describe_run(
            runtime_status(
                run_id=demo_run,
                state="waiting_review",
                next_state="waiting_review",
                step="human-review",
                kind="human_review",
            ),
            review=review_snapshot(
                demo_run,
                events=[
                    approved_event(demo_run, "CAND-A", "a" * 64),
                    approved_event(demo_run, "CAND-B", "b" * 64),
                ],
            ),
        )
        self.assertFalse(any(item.code == "review_incomplete" for item in demo.blockers))

        production_run = "RUN-PRODUCTION-002"
        production = describe_run(
            runtime_status(
                run_id=production_run,
                mode="production",
                state="waiting_review",
                next_state="waiting_review",
                step="human-review",
                kind="human_review",
            ),
            review=review_snapshot(
                production_run,
                mode="production",
                events=[
                    approved_event(
                        production_run,
                        "CAND-A",
                        "a" * 64,
                        mode="production",
                        product_accuracy=False,
                        brand_channel_ok=False,
                    ),
                    approved_event(
                        production_run,
                        "CAND-B",
                        "b" * 64,
                        mode="production",
                    ),
                ],
            ),
        )
        self.assertTrue(any(item.code == "review_incomplete" for item in production.blockers))
        self.assertTrue(any("product_accuracy" in item.details for item in production.blockers))

    def test_optional_review_reason_does_not_block_human_decision(self):
        run_id = "V1-DEMO-KIDS-LOCAL-004"
        events = [
            approved_event(run_id, "CAND-A", "a" * 64),
            approved_event(run_id, "CAND-B", "b" * 64),
        ]
        for event in events:
            event.pop("reason")
        view = describe_run(
            runtime_status(
                run_id=run_id,
                state="waiting_review",
                next_state="waiting_review",
                step="human-review",
                kind="human_review",
            ),
            review=review_snapshot(run_id, events=events),
        )

        self.assertFalse(any(item.code == "review_incomplete" for item in view.blockers))
        self.assertEqual(view.operator_phase, "review")

    def test_completed_with_invalid_mode_is_blocked_not_presented_as_done(self):
        view = describe_run(
            runtime_status(
                mode="unknown",
                state="completed",
                next_state="completed",
                step=None,
                kind=None,
            )
        )

        self.assertEqual(view.phase, "blocked")
        self.assertNotIn("view_result", view.allowed_actions)

    def test_delivery_pending_sync_and_completed_are_read_only_phases(self):
        delivery = describe_run(
            runtime_status(
                state="delivery_pending_sync",
                next_state="pending",
                step="approved-export",
                kind="export",
                pending_outbox=2,
            )
        )
        self.assertEqual(delivery.phase, "delivery_pending_sync")
        self.assertEqual(delivery.allowed_actions, ("wait_for_delivery",))

        completed = describe_run(
            runtime_status(
                state="completed",
                next_state="completed",
                step=None,
                kind=None,
                reserved=5,
            )
        )
        self.assertEqual(completed.phase, "completed")
        self.assertEqual(completed.allowed_actions, ("download_approved_images",))
        self.assertEqual(completed.blockers, ())

    def test_unknown_attempt_is_blocked_and_never_offers_retry(self):
        view = describe_run(
            runtime_status(
                state="blocked",
                next_state="unknown",
                step="background-outdoor",
                kind="image_generate",
                unknown_jobs=["job-unknown-1"],
            )
        )

        self.assertEqual(view.phase, "blocked")
        self.assertTrue(any(item.code == "unknown_attempt" for item in view.blockers))
        self.assertTrue(view.is_blocked)
        self.assertEqual(view.allowed_actions, ("contact_admin",))
        self.assertNotIn("retry", view.allowed_actions)
        self.assertNotIn("dispatch", view.allowed_actions)

    def test_pending_image_with_no_budget_is_explicitly_exhausted(self):
        view = describe_run(
            runtime_status(
                state="queued",
                next_state="pending",
                step="background-studio",
                kind="image_generate",
                reserved=6,
                maximum=6,
            )
        )

        self.assertEqual(view.phase, "budget_exhausted")
        self.assertTrue(any(item.code == "budget_exhausted" for item in view.blockers))
        self.assertEqual(view.allowed_actions, ("contact_admin",))
        self.assertIn("不能自动重试", view.next_instruction)

    def test_unknown_runtime_state_is_safe_blocked_model(self):
        view = describe_run(
            {
                "run_id": "RUN-UNKNOWN",
                "mode": "demo",
                "state": "future_state",
                "next": {"state": "future_state", "step": "x", "kind": "future"},
                "image_calls_reserved": 0,
                "image_calls_max": 6,
                "unknown_jobs": [],
                "pending_outbox": 0,
            }
        )

        self.assertEqual(view.phase, "blocked")
        self.assertTrue(any(item.code == "unknown_runtime_state" for item in view.blockers))

    def test_describe_runtime_reads_only_status(self):
        class ReadOnlyRuntime:
            def __init__(self):
                self.calls = []

            def status(self, run_id):
                self.calls.append(run_id)
                return runtime_status(run_id=run_id)

        runtime = ReadOnlyRuntime()
        view = describe_runtime(runtime, "RUN-READ-ONLY")

        self.assertEqual(runtime.calls, ["RUN-READ-ONLY"])
        self.assertEqual(view.run_id, "RUN-READ-ONLY")


if __name__ == "__main__":
    unittest.main()
