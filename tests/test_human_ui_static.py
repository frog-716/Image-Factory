from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "factory" / "human_ui_static"


class HumanUiStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.css = (STATIC / "styles.css").read_text(encoding="utf-8")
        cls.js = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.source = "\n".join((cls.html, cls.css, cls.js))

    def test_static_entrypoint_has_only_the_two_product_views(self):
        self.assertTrue((STATIC / "index.html").is_file())
        self.assertTrue((STATIC / "styles.css").is_file())
        self.assertTrue((STATIC / "app.js").is_file())
        self.assertEqual(self.html.count('data-view="reviews"'), 1)
        self.assertEqual(self.html.count('data-view="tasks"'), 1)
        self.assertIn('id="reviews-view"', self.html)
        self.assertIn('id="tasks-view"', self.html)
        self.assertIn("待我审核", self.html)
        self.assertIn("任务与交付", self.html)
        self.assertNotIn("Prompt 管理", self.html)
        self.assertNotIn("设置中心", self.html)

    def test_forbidden_internal_fields_are_absent_from_browser_source(self):
        forbidden = (
            r"reviewer[_-]?id",
            r"sha256",
            r"asset[_-]?id",
            r"run[_-]?id",
            r"workflow[_-]?id",
            r"review[_-]?version",
            r"policy",
            r"\bdemo\b",
            r"\bproduction\b",
        )
        for pattern in forbidden:
            self.assertIsNone(
                re.search(pattern, self.source, flags=re.IGNORECASE),
                msg=f"forbidden browser field leaked: {pattern}",
            )

    def test_engine_endpoints_and_opaque_review_path_are_used(self):
        self.assertIn("/api/v1/session", self.js)
        self.assertIn("/api/v1/reviews/pending", self.js)
        self.assertIn("/api/v1/tasks", self.js)
        self.assertIn("/api/v1/reviews/${encodeURIComponent(reviewToken)}/decision", self.js)
        self.assertIn("item.review_token", self.js)
        self.assertIn("credentials: 'same-origin'", self.js)
        self.assertIn("X-CSRF-Token", self.js)

    def test_decision_body_is_restricted_to_decision_and_optional_reason(self):
        self.assertIn("const body = { decision: decision === 'approve' ? 'approved' : 'rejected' };", self.js)
        self.assertIn("if (cleanReason) body.reason = cleanReason;", self.js)
        self.assertIn("body: JSON.stringify(body)", self.js)
        self.assertNotIn("JSON.stringify(item)", self.js)
        self.assertNotIn("JSON.stringify(state)", self.js)

    def test_explicit_confirmation_and_optional_rejection_reason_exist(self):
        self.assertIn('id="decision-dialog"', self.html)
        self.assertIn('id="confirm-decision"', self.html)
        self.assertIn('id="reason-input"', self.html)
        self.assertIn("showModal", self.js)
        self.assertIn("确认通过这张图片？", self.js)
        self.assertIn("确认退回这张图片？", self.js)
        self.assertIn("maxlength=\"500\"", self.html)
        self.assertIn("[hidden]", self.css)
        self.assertRegex(self.css, r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important")

    def test_duplicate_click_and_uncertain_result_guards_exist(self):
        self.assertIn("inFlight: new Set()", self.js)
        self.assertIn("state.inFlight.has(reviewToken)", self.js)
        self.assertIn("state.inFlight.add(reviewToken)", self.js)
        self.assertIn("elements.confirmDecision.disabled = true", self.js)
        self.assertIn("await loadReviews();", self.js)
        self.assertIn("提交结果需要确认，列表已刷新", self.js)
        self.assertIn("页面已过期或图片已变化", self.js)
        self.assertIn("new AbortController()", self.js)
        self.assertIn("controller.abort()", self.js)
        self.assertIn("item.status === 'processing'", self.js)

    def test_tasks_are_business_display_only_and_have_responsive_css(self):
        self.assertIn("displayOf(item)", self.js)
        self.assertIn("下载交付", self.js)
        self.assertIn("@media (max-width: 760px)", self.css)
        self.assertIn("object-fit: cover", self.css)
        self.assertIn("button:disabled", self.css)


if __name__ == "__main__":
    unittest.main()
