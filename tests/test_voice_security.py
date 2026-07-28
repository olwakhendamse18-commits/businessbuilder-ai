import hashlib
import json
import os
import unittest
from unittest import mock

import requests

import app as app_module


VALID_SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
ORIGIN = "http://localhost"
CSRF_TOKEN = "test-voice-csrf-token-with-more-than-32-characters"
VOICE_REQUEST_ID = "phase2b1-security-test"


def profile(voice_enabled=True, selected_voice="marin"):
    return (1, 7, "", "calm", 1 if voice_enabled else 0, selected_voice, "standard", None, None)


class VoiceSecurityTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def authenticate(self, csrf_token=CSRF_TOKEN):
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = 7
            if csrf_token is not None:
                flask_session[app_module.VOICE_CSRF_SESSION_KEY] = csrf_token

    def voice_headers(self, csrf_token=CSRF_TOKEN, origin=ORIGIN, content_type="application/sdp"):
        headers = {
            "Content-Type": content_type,
            "Origin": origin,
            app_module.VOICE_REQUEST_ID_HEADER: VOICE_REQUEST_ID,
        }
        if csrf_token is not None:
            headers[app_module.VOICE_CSRF_HEADER] = csrf_token
        return headers

    def enabled_environment(self):
        return mock.patch.dict(
            os.environ,
            {"VOICE_RUNTIME_ENABLED": "true", "OPENAI_API_KEY": "test-standard-key"},
            clear=True
        )

    def admitted_route_patches(self, upstream_result=(VALID_SDP, None)):
        return [
            mock.patch.object(
                app_module,
                "admit_voice_session",
                return_value={
                    "ok": True,
                    "voice_session_id": 99,
                    "conversation_id": 42,
                    "voice_name": "marin",
                }
            ),
            mock.patch.object(app_module, "get_active_project", return_value=None),
            mock.patch.object(app_module, "get_or_create_agent_conversation", return_value=(42,)),
            mock.patch.object(app_module, "create_realtime_sdp_answer", return_value=upstream_result),
            mock.patch.object(app_module, "activate_voice_session", return_value=True),
        ]

    def test_voice_runtime_flag_defaults_false_and_rejects_unrecognized_values(self):
        for value in (None, "", "false", "0", "not-a-boolean"):
            environment = {"BROWSER_CONTROL_ENABLED": "true"}
            if value is not None:
                environment["VOICE_RUNTIME_ENABLED"] = value
            with self.subTest(value=value), mock.patch.dict(os.environ, environment, clear=True):
                self.assertFalse(app_module.get_voice_config()["enabled"])
                self.assertTrue(app_module.get_browser_config()["enabled"])

        with mock.patch.dict(os.environ, {"VOICE_RUNTIME_ENABLED": "true"}, clear=True):
            self.assertTrue(app_module.get_voice_config()["enabled"])

    def test_disabled_runtime_rejects_before_session_creation_or_upstream(self):
        self.authenticate()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(app_module.requests, "post") as upstream:
            response = self.client.post(
                "/api/realtime/session",
                data=VALID_SDP,
                headers=self.voice_headers()
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "voice_runtime_disabled")
        upstream.assert_not_called()

    def test_disabled_voice_preference_rejects_route_before_session_creation(self):
        self.authenticate()
        with self.enabled_environment(), \
                mock.patch.object(
                    app_module,
                    "admit_voice_session",
                    return_value={
                        "ok": False,
                        "message": "Voice is disabled in your BusinessBuilder settings.",
                        "code": "voice_preference_disabled",
                        "status": 403,
                    }
                ):
            response = self.client.post(
                "/api/realtime/session",
                data=VALID_SDP,
                headers=self.voice_headers()
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "voice_preference_disabled")

    def test_saved_voice_is_validated_with_marin_fallback(self):
        self.assertEqual(
            app_module.resolve_realtime_voice(7, profile(True, "cedar")),
            "cedar"
        )
        for saved in ("builder", "", "MARIN-OVERRIDE", "https://evil.example"):
            with self.subTest(saved=saved):
                self.assertEqual(
                    app_module.resolve_realtime_voice(7, profile(True, saved)),
                    "marin"
                )

    def test_exact_nested_session_schema_and_legacy_fields_absent(self):
        session_config = app_module.build_realtime_session_config(7, "cedar")

        self.assertEqual(
            session_config,
            {
                "type": "realtime",
                "model": "gpt-realtime-2.1",
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "low",
                            "create_response": False,
                            "interrupt_response": True,
                        },
                    },
                    "output": {"voice": "cedar"},
                },
                "instructions": app_module.realtime_voice_instructions(),
                "tools": [],
                "tool_choice": "none",
                "max_output_tokens": 900,
            }
        )
        for legacy_field in (
            "modalities",
            "voice",
            "input_audio_transcription",
            "turn_detection",
            "max_response_output_tokens",
            "response",
        ):
            self.assertNotIn(legacy_field, session_config)

    def test_safety_identifier_is_secret_keyed_and_user_specific(self):
        first = app_module.voice_safety_identifier(7, "secret-one")
        repeat = app_module.voice_safety_identifier(7, "secret-one")
        other_user = app_module.voice_safety_identifier(8, "secret-one")
        other_secret = app_module.voice_safety_identifier(7, "secret-two")
        previous = hashlib.sha256(b"businessbuilder-ai-realtime-safety:7").hexdigest()

        self.assertEqual(first, repeat)
        self.assertNotEqual(first, other_user)
        self.assertNotEqual(first, other_secret)
        self.assertNotEqual(first, "7")
        self.assertNotEqual(first, previous)

    def test_upstream_request_is_pinned_multipart_and_does_not_redirect(self):
        upstream_response = mock.Mock(status_code=201, text=VALID_SDP)
        malicious_environment = {
            "OPENAI_API_KEY": "test-standard-key",
            "OPENAI_REALTIME_WEBRTC_URL": "https://evil.example/steal",
            "OPENAI_REALTIME_MODEL": "attacker-model",
            "OPENAI_REALTIME_VOICE": "attacker-voice",
        }
        with mock.patch.dict(os.environ, malicious_environment, clear=True), \
                mock.patch.object(app_module.requests, "post", return_value=upstream_response) as post:
            answer, error_code = app_module.create_realtime_sdp_answer(7, VALID_SDP, "marin")

        self.assertEqual(answer, VALID_SDP)
        self.assertIsNone(error_code)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.openai.com/v1/realtime/calls")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-standard-key")
        self.assertIn("OpenAI-Safety-Identifier", kwargs["headers"])
        self.assertNotIn("X-OpenAI-Safety-Identifier", kwargs["headers"])
        self.assertEqual(kwargs["timeout"], 12)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["files"]["sdp"], ("offer.sdp", VALID_SDP, "application/sdp"))
        sent_session = json.loads(kwargs["files"]["session"][1])
        self.assertEqual(sent_session["model"], "gpt-realtime-2.1")
        self.assertEqual(sent_session["audio"]["output"]["voice"], "marin")

    def test_upstream_failures_are_classified_without_response_bodies(self):
        cases = (
            (requests.Timeout("secret timeout"), "upstream_timeout"),
            (requests.ConnectionError("secret connection"), "upstream_unavailable"),
            (mock.Mock(status_code=401, text="secret upstream body"), "upstream_authentication_failed"),
            (mock.Mock(status_code=429, text="secret upstream body"), "upstream_rate_limited"),
            (mock.Mock(status_code=503, text="secret upstream body"), "upstream_unavailable"),
            (mock.Mock(status_code=201, text="not sdp"), "invalid_upstream_response"),
        )
        for result, expected_code in cases:
            with self.subTest(expected_code=expected_code), \
                    mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
                if isinstance(result, Exception):
                    patcher = mock.patch.object(app_module.requests, "post", side_effect=result)
                else:
                    patcher = mock.patch.object(app_module.requests, "post", return_value=result)
                with patcher:
                    answer, error_code = app_module.create_realtime_sdp_answer(7, VALID_SDP, "marin")
            self.assertIsNone(answer)
            self.assertEqual(error_code, expected_code)

    def test_session_route_security_controls(self):
        self.authenticate()
        with self.enabled_environment():
            wrong_type = self.client.post(
                "/api/realtime/session",
                json={"sdp": VALID_SDP},
                headers={"Origin": ORIGIN, app_module.VOICE_CSRF_HEADER: CSRF_TOKEN}
            )
            malicious_origin = self.client.post(
                "/api/realtime/session",
                data=VALID_SDP,
                headers=self.voice_headers(origin="https://evil.example")
            )
            missing_token = self.client.post(
                "/api/realtime/session",
                data=VALID_SDP,
                headers=self.voice_headers(csrf_token=None)
            )
            wrong_token = self.client.post(
                "/api/realtime/session",
                data=VALID_SDP,
                headers=self.voice_headers(csrf_token="wrong-token")
            )

        self.assertEqual(wrong_type.get_json()["code"], "invalid_content_type")
        self.assertEqual(malicious_origin.get_json()["code"], "invalid_origin")
        self.assertEqual(missing_token.get_json()["code"], "csrf_failed")
        self.assertEqual(wrong_token.get_json()["code"], "csrf_failed")

    def test_session_route_rejects_cross_site_fetch_and_oversized_sdp(self):
        self.authenticate()
        with self.enabled_environment():
            cross_site_headers = self.voice_headers()
            cross_site_headers["Sec-Fetch-Site"] = "cross-site"
            cross_site = self.client.post(
                "/api/realtime/session",
                data=VALID_SDP,
                headers=cross_site_headers
            )
            oversized = self.client.post(
                "/api/realtime/session",
                data="v=0\r\n" + ("x" * app_module.VOICE_SESSION_MAX_SDP_BYTES),
                headers=self.voice_headers()
            )

        self.assertEqual(cross_site.get_json()["code"], "invalid_origin")
        self.assertEqual(oversized.get_json()["code"], "sdp_too_large")

    def test_session_route_rejects_invalid_sdp_shape(self):
        self.authenticate()
        with self.enabled_environment():
            response = self.client.post(
                "/api/realtime/session",
                data="v=0\r\ns=-\r\n",
                headers=self.voice_headers()
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "invalid_sdp")

    def test_anonymous_session_request_is_rejected(self):
        response = self.client.post(
            "/api/realtime/session",
            data=VALID_SDP,
            headers=self.voice_headers()
        )
        self.assertEqual(response.status_code, 401)

    def test_correct_session_security_reaches_mocked_handshake(self):
        self.authenticate()
        patches = self.admitted_route_patches()
        with self.enabled_environment():
            started = [patcher.start() for patcher in patches]
            try:
                response = self.client.post(
                    "/api/realtime/session",
                    data=VALID_SDP,
                    headers=self.voice_headers()
                )
            finally:
                for patcher in reversed(patches):
                    patcher.stop()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/sdp")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        started[0].assert_called_once_with(7, 42, None, VOICE_REQUEST_ID)
        started[4].assert_called_once_with(7, 99, VOICE_REQUEST_ID)

    def test_activation_failure_never_returns_upstream_sdp(self):
        self.authenticate()
        patches = self.admitted_route_patches()
        finish = mock.patch.object(app_module, "finish_voice_session")
        with self.enabled_environment():
            started = [patcher.start() for patcher in patches]
            started[4].return_value = False
            finish_mock = finish.start()
            try:
                response = self.client.post(
                    "/api/realtime/session",
                    data=VALID_SDP,
                    headers=self.voice_headers()
                )
            finally:
                finish.stop()
                for patcher in reversed(patches):
                    patcher.stop()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "voice_activation_failed")
        finish_mock.assert_called_once_with(
            7,
            99,
            "voice_activation_failed",
            handshake_request_id=VOICE_REQUEST_ID
        )

    def test_route_finalizes_local_session_when_handshake_fails(self):
        self.authenticate()
        patches = self.admitted_route_patches((None, "upstream_timeout"))
        finish = mock.patch.object(app_module, "finish_voice_session")
        with self.enabled_environment():
            for patcher in patches:
                patcher.start()
            finish_mock = finish.start()
            try:
                response = self.client.post(
                    "/api/realtime/session",
                    data=VALID_SDP,
                    headers=self.voice_headers()
                )
            finally:
                finish.stop()
                for patcher in reversed(patches):
                    patcher.stop()

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.get_json()["code"], "upstream_timeout")
        finish_mock.assert_called_once_with(
            7,
            99,
            "upstream_timeout",
            handshake_request_id=VOICE_REQUEST_ID
        )

    def test_session_end_requires_json_origin_and_csrf(self):
        self.authenticate()
        with self.enabled_environment(), mock.patch.object(
                app_module,
                "finish_voice_session",
                return_value={"found": True, "ended": True, "changed": True}
        ) as finish:
            missing_token = self.client.post(
                "/api/realtime/session/end",
                json={"voice_session_id": 99},
                headers={"Origin": ORIGIN}
            )
            malicious_origin = self.client.post(
                "/api/realtime/session/end",
                json={"voice_session_id": 99},
                headers={"Origin": "https://evil.example", app_module.VOICE_CSRF_HEADER: CSRF_TOKEN}
            )
            correct = self.client.post(
                "/api/realtime/session/end",
                json={"voice_session_id": 99},
                headers={"Origin": ORIGIN, app_module.VOICE_CSRF_HEADER: CSRF_TOKEN}
            )

        self.assertEqual(missing_token.get_json()["code"], "csrf_failed")
        self.assertEqual(malicious_origin.get_json()["code"], "invalid_origin")
        self.assertEqual(correct.status_code, 200)
        self.assertEqual(correct.headers["Cache-Control"], "no-store")
        finish.assert_called_once_with(7, 99, "client_disconnected")

    def test_session_end_rejects_anonymous_and_wrong_content_type(self):
        anonymous_client = app_module.app.test_client()
        anonymous = anonymous_client.post(
            "/api/realtime/session/end",
            json={"voice_session_id": 99},
            headers={"Origin": ORIGIN, app_module.VOICE_CSRF_HEADER: CSRF_TOKEN}
        )
        self.authenticate()
        with self.enabled_environment():
            wrong_type = self.client.post(
                "/api/realtime/session/end",
                data="voice_session_id=99",
                headers={
                    "Origin": ORIGIN,
                    "Content-Type": "application/x-www-form-urlencoded",
                    app_module.VOICE_CSRF_HEADER: CSRF_TOKEN,
                }
            )
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(wrong_type.get_json()["code"], "invalid_content_type")

    def test_voice_csrf_token_is_stable_within_authenticated_session(self):
        self.authenticate(csrf_token=None)
        with self.client:
            with app_module.app.test_request_context("/command-center"):
                app_module.session["user_id"] = 7
                first = app_module.get_voice_csrf_token()
                second = app_module.get_voice_csrf_token()
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 32)

    def test_browser_control_default_remains_disabled(self):
        with mock.patch.dict(os.environ, {"VOICE_RUNTIME_ENABLED": "true"}, clear=True):
            self.assertFalse(app_module.get_browser_config()["enabled"])


class AgentMessageSecurityTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = 7

    def post_message(self, message, origin=ORIGIN, content_type=None):
        if content_type:
            return self.client.post(
                "/api/agent/message",
                data=message,
                headers={"Origin": origin, "Content-Type": content_type}
            )
        return self.client.post(
            "/api/agent/message",
            json={"message": message, "request_id": "request-boundary"},
            headers={"Origin": origin}
        )

    def test_empty_message_is_rejected_before_reservation(self):
        with mock.patch.object(app_module, "reserve_agent_message_request") as reserve:
            response = self.post_message("   ")
        self.assertEqual(response.status_code, 400)
        reserve.assert_not_called()

    def test_exact_limit_is_accepted_and_text_route_still_works(self):
        result = {
            "reply": "Canonical reply",
            "visible_plan": [],
            "approval_needed": False,
            "approval_id": None,
            "task_id": None,
            "risk_level": "low",
        }
        with mock.patch.object(app_module, "normalize_agent_request_id", return_value="request-boundary"), \
                mock.patch.object(app_module, "reserve_agent_message_request", return_value=(True, None)), \
                mock.patch.object(app_module, "get_active_project", return_value=None), \
                mock.patch.object(app_module, "get_or_create_agent_conversation", return_value=(42,)), \
                mock.patch.object(app_module, "save_agent_message") as save_message, \
                mock.patch.object(app_module, "run_businessbuilder_agent", return_value=result) as run_agent, \
                mock.patch.object(app_module, "complete_agent_message_request"):
            response = self.post_message("a" * app_module.AGENT_MESSAGE_MAX_CHARS)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["reply"], "Canonical reply")
        run_agent.assert_called_once()
        self.assertEqual(save_message.call_count, 2)

    def test_limit_plus_one_writes_and_reserves_nothing(self):
        with mock.patch.object(app_module, "reserve_agent_message_request") as reserve, \
                mock.patch.object(app_module, "get_or_create_agent_conversation") as create_conversation, \
                mock.patch.object(app_module, "save_agent_message") as save_message, \
                mock.patch.object(app_module, "run_businessbuilder_agent") as run_agent:
            response = self.post_message("a" * (app_module.AGENT_MESSAGE_MAX_CHARS + 1))

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["code"], "agent_message_too_long")
        reserve.assert_not_called()
        create_conversation.assert_not_called()
        save_message.assert_not_called()
        run_agent.assert_not_called()

    def test_agent_message_requires_json_and_same_origin(self):
        wrong_type = self.post_message("hello", content_type="text/plain")
        malicious_origin = self.post_message("hello", origin="https://evil.example")
        self.assertEqual(wrong_type.get_json()["code"], "invalid_content_type")
        self.assertEqual(malicious_origin.get_json()["code"], "invalid_origin")


class CommandCenterRegressionTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def test_authenticated_command_center_routes_still_render(self):
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = 7
        with mock.patch.object(app_module, "build_command_center_context", return_value={}), \
                mock.patch.object(app_module, "render_template", return_value="rendered"):
            text_response = self.client.get("/command-center")
            voice_response = self.client.get("/command-center/voice")

        self.assertEqual(text_response.status_code, 200)
        self.assertEqual(voice_response.status_code, 200)

    def test_anonymous_command_center_routes_still_redirect(self):
        self.assertEqual(self.client.get("/command-center").status_code, 302)
        self.assertEqual(self.client.get("/command-center/voice").status_code, 302)


if __name__ == "__main__":
    unittest.main()
