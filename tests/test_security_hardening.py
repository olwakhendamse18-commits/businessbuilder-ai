import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as app_module


ROOT = Path(__file__).resolve().parents[1]


class SecurityHeadersAndCsrfTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True, SECRET_KEY="security-hardening-test")
        self.client = app_module.app.test_client()

    def test_security_headers_are_present(self):
        response = self.client.get("/landing")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("object-src 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(
            response.headers["Referrer-Policy"],
            "strict-origin-when-cross-origin",
        )

    def test_cross_site_post_is_rejected_and_token_post_is_accepted(self):
        page = self.client.get("/logout")
        token_match = re.search(
            rb'<meta name="csrf-token" content="([a-f0-9]{64})">',
            page.data,
        )
        self.assertIsNotNone(token_match)
        rejected = self.client.post(
            "/logout",
            headers={"Origin": "https://attacker.invalid"},
        )
        self.assertEqual(rejected.status_code, 403)
        accepted = self.client.post(
            "/logout",
            data={"_csrf_token": token_match.group(1).decode("ascii")},
        )
        self.assertEqual(accepted.status_code, 302)

    def test_mutating_routes_do_not_accept_get(self):
        routes = [
            "/new_chat",
            "/switch_business_project/1",
            "/complete_step/1/example",
            "/generate_business_plan",
            "/generate_shopify_plan",
            "/generate_canva_branding",
            "/generate_full_store",
            "/create_shopify_product",
            "/paystack_checkout",
        ]
        for route in routes:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 405)


class UploadHardeningTestCase(unittest.TestCase):
    def _write(self, content):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.addCleanup(lambda: os.path.exists(handle.name) and os.remove(handle.name))
        handle.write(content)
        handle.close()
        return handle.name

    def test_upload_signatures_are_checked(self):
        valid_pdf = self._write(b"%PDF-1.7\nsynthetic")
        fake_pdf = self._write(b"not a pdf")
        valid_png = self._write(b"\x89PNG\r\n\x1a\nsynthetic")
        self.assertTrue(app_module.validate_uploaded_file(valid_pdf, ".pdf"))
        self.assertFalse(app_module.validate_uploaded_file(fake_pdf, ".pdf"))
        self.assertTrue(app_module.validate_uploaded_file(valid_png, ".png"))
        self.assertFalse(app_module.validate_uploaded_file(valid_png, ".jpg"))

    def test_upload_policy_is_bounded_and_uses_random_names(self):
        self.assertEqual(app_module.app.config["MAX_CONTENT_LENGTH"], 10 * 1024 * 1024)
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("uuid.uuid4().hex", source)
        self.assertIn('str(session["user_id"])', source)
        self.assertNotIn('os.path.join(\n        app.config["UPLOAD_FOLDER"],\n        filename', source)


class AuthenticationHardeningTestCase(unittest.TestCase):
    def setUp(self):
        app_module._auth_failures.clear()

    def tearDown(self):
        app_module._auth_failures.clear()

    def test_rate_limit_is_bounded_and_clearable(self):
        with app_module.app.test_request_context("/login", environ_base={"REMOTE_ADDR": "127.0.0.9"}):
            for offset in range(app_module.AUTH_RATE_LIMIT_MAX_FAILURES):
                app_module.record_auth_failure("person@example.com", now=100 + offset)
            limited, retry_after = app_module.auth_rate_limit_status(
                "person@example.com", now=110
            )
            self.assertTrue(limited)
            self.assertGreater(retry_after, 0)
            app_module.clear_auth_failures("person@example.com")
            self.assertEqual(
                app_module.auth_rate_limit_status("person@example.com", now=110),
                (False, 0),
            )

    def test_password_and_cookie_policy(self):
        self.assertGreaterEqual(app_module.AUTH_PASSWORD_MIN_LENGTH, 12)
        self.assertLessEqual(app_module.AUTH_RATE_LIMIT_MAX_KEYS, 4096)
        self.assertTrue(app_module.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app_module.app.config["SESSION_COOKIE_SAMESITE"], "Lax")


class ProjectHygieneTestCase(unittest.TestCase):
    def test_legacy_chat_uses_text_only_dom_updates(self):
        template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("messageElement.textContent", template)
        self.assertNotIn("chatBox.innerHTML", template)

    def test_dependencies_are_pinned_and_logs_are_ignored(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertTrue(requirements)
        self.assertTrue(all("==" in line for line in requirements if line.strip()))
        self.assertIn("*.log", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_service_worker_does_not_cache_session_bearing_html(self):
        service_worker = (ROOT / "static" / "service-worker.js").read_text(encoding="utf-8")
        core_assets = service_worker.split("]", 1)[0]
        self.assertNotIn('"/landing"', core_assets)
        self.assertNotIn('"/pricing"', core_assets)
        self.assertIn('request.mode === "navigate"', service_worker)

    def test_healthz_is_safe_and_bounded(self):
        connection = MagicMock()
        connection.cursor.return_value.fetchone.return_value = (1,)
        with patch.object(app_module, "db", return_value=connection):
            response = app_module.app.test_client().get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})
        self.assertGreaterEqual(connection.close.call_count, 1)


if __name__ == "__main__":
    unittest.main()
