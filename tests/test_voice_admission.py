import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock

import app as app_module


class IsolatedSQLiteVoiceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.temp_dir.name, "voice-test.db")
        self.db_patch = mock.patch.object(
            app_module, "db", side_effect=self.connect
        )
        self.postgres_patch = mock.patch.object(
            app_module, "using_postgres", return_value=False
        )
        self.db_patch.start()
        self.postgres_patch.start()
        app_module.init_db()
        self.execute(
            """
            INSERT INTO agent_profiles (
                user_id, voice_enabled, selected_voice
            ) VALUES (?, 1, 'marin')
            """,
            (7,)
        )

    def tearDown(self):
        self.postgres_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def connect(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def execute(self, statement, parameters=()):
        connection = self.connect()
        cursor = connection.execute(statement, parameters)
        connection.commit()
        rows = cursor.fetchall()
        connection.close()
        return rows

    def enabled_environment(self):
        return mock.patch.dict(
            os.environ,
            {
                "VOICE_RUNTIME_ENABLED": "true",
                "OPENAI_API_KEY": "synthetic-test-key",
            },
            clear=True
        )

    def admit(self, request_id, user_id=7):
        return app_module.admit_voice_session(
            user_id, 42, None, request_id
        )


class SQLiteAdmissionConcurrencyTestCase(IsolatedSQLiteVoiceTestCase):
    def run_concurrent(self, first_request_id, second_request_id):
        barrier = threading.Barrier(2)

        def worker(request_id):
            barrier.wait()
            return self.admit(request_id)

        with self.enabled_environment(), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(worker, first_request_id),
                pool.submit(worker, second_request_id),
            ]
            return [future.result() for future in futures]

    def test_different_request_ids_create_only_one_nonterminal_row(self):
        results = self.run_concurrent("attempt-one", "attempt-two")
        self.assertEqual(sum(result["ok"] for result in results), 1)
        rejected = next(result for result in results if not result["ok"])
        self.assertIn(
            rejected["code"], {"voice_session_active", "voice_admission_busy"}
        )
        count = self.execute(
            """
            SELECT COUNT(*) FROM agent_voice_sessions
            WHERE status IN ('starting', 'active')
            """
        )[0][0]
        self.assertEqual(count, 1)

    def test_same_request_id_is_idempotent_under_concurrency(self):
        results = self.run_concurrent("same-attempt", "same-attempt")
        self.assertEqual(sum(result["ok"] for result in results), 1)
        rejected = next(result for result in results if not result["ok"])
        self.assertIn(
            rejected["code"],
            {"voice_handshake_in_progress", "voice_admission_busy"}
        )
        count = self.execute(
            """
            SELECT COUNT(*) FROM agent_voice_sessions
            WHERE user_id = 7 AND handshake_request_id = 'same-attempt'
            """
        )[0][0]
        self.assertEqual(count, 1)

    def test_lock_releases_after_success_and_after_rollback(self):
        with self.enabled_environment():
            first = self.admit("first-attempt")
            self.assertTrue(first["ok"])
            app_module.finish_voice_session(
                7, first["voice_session_id"], "test_complete"
            )
            second = self.admit("second-attempt")
            self.assertTrue(second["ok"])
            app_module.finish_voice_session(
                7, second["voice_session_id"], "test_complete"
            )
            with mock.patch.object(
                app_module,
                "_admit_voice_session_in_transaction",
                side_effect=RuntimeError("synthetic rollback")
            ):
                failed = self.admit("rollback-attempt")
            self.assertEqual(failed["code"], "voice_admission_busy")
            after_rollback = self.admit("after-rollback")
        self.assertTrue(after_rollback["ok"])

    def test_admission_never_calls_upstream_http(self):
        with self.enabled_environment(), mock.patch.object(
            app_module.requests, "post"
        ) as upstream:
            result = self.admit("local-only")
        self.assertTrue(result["ok"])
        upstream.assert_not_called()

    def test_real_atomic_rejections_insert_nothing_and_call_no_upstream(self):
        cases = (
            ({}, None, "voice_runtime_disabled"),
            (
                {
                    "VOICE_RUNTIME_ENABLED": "true",
                    "OPENAI_API_KEY": "synthetic-test-key",
                },
                0,
                "voice_preference_disabled",
            ),
            (
                {"VOICE_RUNTIME_ENABLED": "true"},
                1,
                "voice_not_configured",
            ),
        )
        for index, (environment, voice_enabled, expected_code) in enumerate(cases):
            with self.subTest(expected_code=expected_code):
                self.execute(
                    "DELETE FROM agent_voice_sessions WHERE user_id = 7"
                )
                if voice_enabled is not None:
                    self.execute(
                        """
                        UPDATE agent_profiles
                        SET voice_enabled = ? WHERE user_id = 7
                        """,
                        (voice_enabled,)
                    )
                with mock.patch.dict(os.environ, environment, clear=True), \
                        mock.patch.object(app_module.requests, "post") as upstream:
                    result = self.admit(f"rejected-{index}")
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], expected_code)
                self.assertEqual(
                    self.execute(
                        """
                        SELECT COUNT(*) FROM agent_voice_sessions
                        WHERE user_id = 7
                        """
                    )[0][0],
                    0
                )
                upstream.assert_not_called()
                self.execute(
                    """
                    UPDATE agent_profiles
                    SET voice_enabled = 1 WHERE user_id = 7
                    """
                )

    def test_expired_row_is_reconciled_before_admission(self):
        now = app_module.utc_now()
        self.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, handshake_request_id, started_at,
                expires_at, created_at, updated_at
            ) VALUES (?, 'active', ?, ?, ?, ?, ?)
            """,
            (
                7, "expired-active", now - timedelta(minutes=2),
                now - timedelta(seconds=1), now - timedelta(minutes=2),
                now - timedelta(minutes=2)
            )
        )
        with self.enabled_environment():
            result = self.admit("replacement-attempt")
        self.assertTrue(result["ok"])
        old_status, old_reason = self.execute(
            """
            SELECT status, disconnect_reason
            FROM agent_voice_sessions
            WHERE handshake_request_id = 'expired-active'
            """
        )[0]
        self.assertEqual((old_status, old_reason), ("ended", "server_expired"))

    def test_ended_request_id_cannot_be_reused(self):
        with self.enabled_environment():
            first = self.admit("one-shot")
            app_module.finish_voice_session(
                7, first["voice_session_id"], "test_complete"
            )
            repeated = self.admit("one-shot")
        self.assertEqual(repeated["code"], "voice_request_id_reused")

    def test_different_users_may_use_the_same_request_id(self):
        self.execute(
            """
            INSERT INTO agent_profiles (
                user_id, voice_enabled, selected_voice
            ) VALUES (8, 1, 'marin')
            """
        )
        with self.enabled_environment():
            first = self.admit("shared-attempt", user_id=7)
            second = self.admit("shared-attempt", user_id=8)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])


class VoiceLifecycleTestCase(IsolatedSQLiteVoiceTestCase):
    def test_starting_activates_once_and_retains_expiry(self):
        with self.enabled_environment():
            admission = self.admit("activation-attempt")
        before = self.execute(
            """
            SELECT status, expires_at FROM agent_voice_sessions
            WHERE id = ?
            """,
            (admission["voice_session_id"],)
        )[0]
        activated = app_module.activate_voice_session(
            7, admission["voice_session_id"], "activation-attempt"
        )
        activated_again = app_module.activate_voice_session(
            7, admission["voice_session_id"], "activation-attempt"
        )
        after = self.execute(
            """
            SELECT status, expires_at FROM agent_voice_sessions
            WHERE id = ?
            """,
            (admission["voice_session_id"],)
        )[0]
        self.assertTrue(activated)
        self.assertFalse(activated_again)
        self.assertEqual(before[1], after[1])
        self.assertEqual(after[0], "active")

    def test_activation_requires_exact_ownership_and_matching_request(self):
        with self.enabled_environment():
            admission = self.admit("owned-activation")
        session_id = admission["voice_session_id"]
        before_updated = self.execute(
            "SELECT updated_at FROM agent_voice_sessions WHERE id = ?",
            (session_id,)
        )[0][0]
        self.assertFalse(
            app_module.activate_voice_session(8, session_id, "owned-activation")
        )
        self.assertFalse(
            app_module.activate_voice_session(7, session_id + 1, "owned-activation")
        )
        self.assertFalse(
            app_module.activate_voice_session(7, session_id, "wrong-request")
        )
        unchanged = self.execute(
            "SELECT status, updated_at FROM agent_voice_sessions WHERE id = ?",
            (session_id,)
        )[0]
        self.assertEqual(unchanged, ("starting", before_updated))
        self.assertTrue(
            app_module.activate_voice_session(7, session_id, "owned-activation")
        )
        changed = self.execute(
            "SELECT status, updated_at FROM agent_voice_sessions WHERE id = ?",
            (session_id,)
        )[0]
        self.assertEqual(changed[0], "active")
        self.assertGreaterEqual(
            app_module.parse_db_datetime(changed[1]),
            app_module.parse_db_datetime(before_updated)
        )

    def test_expired_or_reconciled_starting_row_cannot_be_activated(self):
        now = app_module.utc_now()
        with self.enabled_environment():
            admission = app_module.admit_voice_session(
                7, 42, None, "expires-before-activation", now=now
            )
        session_id = admission["voice_session_id"]
        after_expiry = now + timedelta(
            seconds=app_module.get_voice_config()["session_max_seconds"] + 1
        )
        reconciled = app_module.reconcile_expired_voice_sessions(
            7, now=after_expiry
        )
        self.assertTrue(reconciled["ok"])
        self.assertFalse(
            app_module.activate_voice_session(
                7, session_id, "expires-before-activation", now=after_expiry
            )
        )
        row = self.execute(
            """
            SELECT status, disconnect_reason
            FROM agent_voice_sessions WHERE id = ?
            """,
            (session_id,)
        )[0]
        self.assertEqual(row, ("ended", "handshake_expired"))
        repeated = app_module.reconcile_expired_voice_sessions(
            7, now=after_expiry
        )
        self.assertEqual(repeated["total"], 0)
        self.assertFalse(
            app_module.activate_voice_session(
                7, session_id, "expires-before-activation", now=after_expiry
            )
        )
        self.assertEqual(
            self.execute(
                """
                SELECT status, disconnect_reason
                FROM agent_voice_sessions WHERE id = ?
                """,
                (session_id,)
            )[0],
            ("ended", "handshake_expired")
        )

    def test_finish_is_idempotent_preserves_first_terminal_values(self):
        with self.enabled_environment():
            admission = self.admit("finish-attempt")
        first_time = app_module.utc_now()
        first = app_module.finish_voice_session(
            7, admission["voice_session_id"], "first_reason", first_time
        )
        second = app_module.finish_voice_session(
            7, admission["voice_session_id"], "later_reason",
            first_time + timedelta(minutes=1)
        )
        row = self.execute(
            """
            SELECT status, ended_at, duration_seconds, disconnect_reason
            FROM agent_voice_sessions WHERE id = ?
            """,
            (admission["voice_session_id"],)
        )[0]
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(row[0], "ended")
        self.assertGreaterEqual(row[2], 0)
        self.assertEqual(row[3], "first_reason")
        self.assertEqual(
            app_module.parse_db_datetime(row[1]),
            first_time
        )

    def test_cross_user_finish_is_rejected(self):
        with self.enabled_environment():
            admission = self.admit("owned-attempt")
        result = app_module.finish_voice_session(
            8, admission["voice_session_id"], "not_owner"
        )
        status = self.execute(
            "SELECT status FROM agent_voice_sessions WHERE id = ?",
            (admission["voice_session_id"],)
        )[0][0]
        self.assertFalse(result["found"])
        self.assertEqual(status, "starting")

    def test_reconciliation_handles_starting_active_legacy_and_repeats(self):
        now = app_module.utc_now()
        rows = (
            (8, "starting", "old-start", now - timedelta(minutes=2),
             None),
            (9, "active", "expired", now - timedelta(minutes=2),
             now - timedelta(seconds=1)),
            (10, "active", "legacy", now - timedelta(minutes=20), None),
        )
        for user_id, status, request_id, started, expires in rows:
            self.execute(
                """
                INSERT INTO agent_voice_sessions (
                    user_id, status, handshake_request_id, started_at,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, status, request_id, started, expires, started, started)
            )
        first = app_module.reconcile_expired_voice_sessions(now=now)
        second = app_module.reconcile_expired_voice_sessions(now=now)
        reasons = dict(self.execute(
            """
            SELECT handshake_request_id, disconnect_reason
            FROM agent_voice_sessions
            WHERE handshake_request_id IN ('old-start', 'expired', 'legacy')
            """
        ))
        self.assertEqual(first["total"], 3)
        self.assertEqual(second["total"], 0)
        self.assertEqual(reasons["old-start"], "handshake_expired")
        self.assertEqual(reasons["expired"], "server_expired")
        self.assertEqual(reasons["legacy"], "legacy_session_expired")

    def test_reconciliation_timestamp_precedence_and_fail_closed_policy(self):
        now = app_module.utc_now()
        future = now + timedelta(minutes=5)
        past = now - timedelta(seconds=1)
        cases = (
            (8, "starting", "future-expiry", "malformed", future),
            (9, "starting", "past-expiry", "malformed", past),
            (10, "active", "active-past-expiry", "malformed", past),
            (
                11, "starting", "bad-expiry-valid-fallback",
                now - timedelta(minutes=2), "malformed"
            ),
            (12, "active", "bad-all-active", "malformed", "malformed"),
            (13, "starting", "bad-all-starting", "malformed", "malformed"),
            (14, "active", "fresh-valid", now, future),
            (
                15, "active", "bad-expiry-active-fallback",
                now - timedelta(minutes=20), "malformed"
            ),
        )
        for user_id, status, request_id, started, expires in cases:
            self.execute(
                """
                INSERT INTO agent_voice_sessions (
                    user_id, status, handshake_request_id, started_at,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, status, request_id, started, expires,
                    started, now
                )
            )
        first = app_module.reconcile_expired_voice_sessions(now=now)
        second = app_module.reconcile_expired_voice_sessions(now=now)
        states = dict(
            self.execute(
                """
                SELECT handshake_request_id, status
                FROM agent_voice_sessions
                """
            )
        )
        reasons = dict(
            self.execute(
                """
                SELECT handshake_request_id, disconnect_reason
                FROM agent_voice_sessions
                """
            )
        )
        self.assertTrue(first["ok"])
        self.assertEqual(second["total"], 0)
        self.assertEqual(states["future-expiry"], "starting")
        self.assertEqual(states["fresh-valid"], "active")
        self.assertEqual(reasons["past-expiry"], "handshake_expired")
        self.assertEqual(reasons["active-past-expiry"], "server_expired")
        self.assertEqual(
            reasons["bad-expiry-valid-fallback"], "handshake_expired"
        )
        self.assertEqual(
            reasons["bad-all-active"], "invalid_session_timestamp"
        )
        self.assertEqual(
            reasons["bad-all-starting"], "invalid_session_timestamp"
        )
        self.assertEqual(
            reasons["bad-expiry-active-fallback"], "legacy_session_expired"
        )

    def test_runtime_reconciliation_does_not_process_ended_history(self):
        self.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, handshake_request_id, started_at,
                expires_at, ended_at, disconnect_reason, created_at, updated_at
            ) VALUES (
                20, 'ended', 'ended-history', 'malformed', 'malformed',
                CURRENT_TIMESTAMP, 'original_reason', 'malformed',
                CURRENT_TIMESTAMP
            )
            """
        )
        result = app_module.reconcile_expired_voice_sessions()
        self.assertTrue(result["ok"])
        self.assertEqual(
            self.execute(
                """
                SELECT status, disconnect_reason
                FROM agent_voice_sessions
                WHERE handshake_request_id = 'ended-history'
                """
            )[0],
            ("ended", "original_reason")
        )

    def test_request_id_validation(self):
        self.assertEqual(
            app_module.normalize_voice_request_id(None)[1],
            "voice_request_id_required"
        )
        for value in ("contains space", "bad/slash", "x" * 121, "ümlaut"):
            with self.subTest(value=value):
                self.assertEqual(
                    app_module.normalize_voice_request_id(value)[1],
                    "invalid_voice_request_id"
                )
        valid = "AZaz09_under-score.period:colon"
        self.assertEqual(
            app_module.normalize_voice_request_id(valid), (valid, None)
        )

    def test_route_rejects_missing_or_invalid_request_id_before_admission(self):
        client = app_module.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = 7
            flask_session[app_module.VOICE_CSRF_SESSION_KEY] = "c" * 40
        base_headers = {
            "Content-Type": "application/sdp",
            "Origin": "http://localhost",
            app_module.VOICE_CSRF_HEADER: "c" * 40,
        }
        with self.enabled_environment(), mock.patch.object(
            app_module, "admit_voice_session"
        ) as admission:
            missing = client.post(
                "/api/realtime/session",
                data="v=0\r\no=-\r\ns=-\r\nt=0 0\r\n",
                headers=base_headers
            )
            invalid_headers = dict(base_headers)
            invalid_headers[app_module.VOICE_REQUEST_ID_HEADER] = "bad/request"
            invalid = client.post(
                "/api/realtime/session",
                data="v=0\r\no=-\r\ns=-\r\nt=0 0\r\n",
                headers=invalid_headers
            )
        self.assertEqual(
            missing.get_json()["code"], "voice_request_id_required"
        )
        self.assertEqual(
            invalid.get_json()["code"], "invalid_voice_request_id"
        )
        admission.assert_not_called()

    def test_opportunistic_reconciliation_is_monotonic_throttled(self):
        original_last_run = app_module._voice_reconcile_last_run
        app_module._voice_reconcile_last_run = 0.0
        try:
            with app_module.app.test_request_context("/health-check"), \
                    mock.patch.dict(
                        app_module.app.config, {"TESTING": False}
                    ), \
                    mock.patch.object(
                        app_module.time, "monotonic",
                        side_effect=[100.0, 100.0, 110.0]
                    ), \
                    mock.patch.object(
                        app_module,
                        "reconcile_expired_voice_sessions",
                        return_value={"ok": True, "total": 0}
                    ) as reconcile:
                app_module.opportunistic_voice_session_reconciliation()
                app_module.opportunistic_voice_session_reconciliation()
        finally:
            app_module._voice_reconcile_last_run = original_last_run
        reconcile.assert_called_once_with()

    def test_cli_prints_only_safe_counts(self):
        runner = app_module.app.test_cli_runner()
        safe_counts = {
            "ok": True,
            "total": 4,
            "handshake_expired": 1,
            "server_expired": 1,
            "legacy_session_expired": 1,
            "invalid_session_timestamp": 0,
            "duplicate_session_reconciled": 1,
        }
        with mock.patch.object(
            app_module,
            "reconcile_expired_voice_sessions",
            return_value=safe_counts
        ):
            result = runner.invoke(args=["reconcile-voice-sessions"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("ended=4", result.output)
        self.assertNotIn("user_id", result.output)
        self.assertNotIn("SDP", result.output)

    def test_cli_failure_is_nonzero_safe_and_not_completed(self):
        runner = app_module.app.test_cli_runner()
        with mock.patch.object(
            app_module,
            "reconcile_expired_voice_sessions",
            return_value={
                "ok": False,
                "code": "voice_reconciliation_failed",
            }
        ), mock.patch.object(app_module.requests, "post") as upstream:
            result = runner.invoke(args=["reconcile-voice-sessions"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(
            "Voice-session reconciliation failed safely.", result.output
        )
        self.assertNotIn("complete", result.output.lower())
        self.assertNotIn("synthetic database failure", result.output)
        upstream.assert_not_called()

    def test_failed_reconciliation_is_not_reported_as_zero_success(self):
        with mock.patch.object(
            app_module,
            "_reconcile_voice_sessions_in_transaction",
            side_effect=sqlite3.DatabaseError("synthetic internal SQL detail")
        ):
            result = app_module.reconcile_expired_voice_sessions()
        self.assertEqual(
            result,
            {"ok": False, "code": "voice_reconciliation_failed"}
        )
        connection = self.connect()
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
        connection.close()

    def test_unexpected_route_handshake_exception_finalizes_owned_row(self):
        client = app_module.app.test_client()
        csrf_token = "r" * 40
        request_id = "unexpected-runtime-error"
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = 7
            flask_session[app_module.VOICE_CSRF_SESSION_KEY] = csrf_token
        headers = {
            "Content-Type": "application/sdp",
            "Origin": "http://localhost",
            app_module.VOICE_CSRF_HEADER: csrf_token,
            app_module.VOICE_REQUEST_ID_HEADER: request_id,
        }
        with self.enabled_environment(), \
                mock.patch.object(app_module, "get_active_project", return_value=None), \
                mock.patch.object(
                    app_module,
                    "get_or_create_agent_conversation",
                    return_value=(42,)
                ), \
                mock.patch.object(
                    app_module,
                    "create_realtime_sdp_answer",
                    side_effect=RuntimeError("synthetic private exception")
                ):
            response = client.post(
                "/api/realtime/session",
                data="v=0\r\no=-\r\ns=-\r\nt=0 0\r\n",
                headers=headers
            )
        rows = self.execute(
            """
            SELECT status, disconnect_reason
            FROM agent_voice_sessions
            WHERE user_id = 7 AND handshake_request_id = ?
            """,
            (request_id,)
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "upstream_unavailable")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("synthetic private exception", response.get_data(as_text=True))
        self.assertNotEqual(response.mimetype, "application/sdp")
        self.assertNotIn("v=0", response.get_data(as_text=True))
        self.assertEqual(rows, [("ended", "upstream_unexpected_error")])
        self.assertEqual(
            self.execute(
                """
                SELECT COUNT(*) FROM agent_voice_sessions
                WHERE user_id = 7 AND status IN ('starting', 'active')
                """
            )[0][0],
            0
        )


class VoiceMigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.temp_dir.name, "migration.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def connect(self):
        return sqlite3.connect(self.database_path, timeout=5)

    def test_nullable_legacy_timestamps_fail_closed(self):
        connection = self.connect()
        connection.execute(
            """
            CREATE TABLE agent_voice_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TIMESTAMP,
                expires_at TIMESTAMP,
                ended_at TIMESTAMP,
                duration_seconds INTEGER DEFAULT 0,
                disconnect_reason TEXT,
                updated_at TIMESTAMP,
                created_at TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, started_at, expires_at, created_at
            ) VALUES (7, 'starting', NULL, NULL, NULL)
            """
        )
        connection.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, started_at, expires_at, created_at
            ) VALUES (8, 'active', NULL, NULL, NULL)
            """
        )
        connection.commit()
        connection.close()
        with mock.patch.object(app_module, "db", side_effect=self.connect), \
                mock.patch.object(app_module, "using_postgres", return_value=False):
            first = app_module.reconcile_expired_voice_sessions()
            second = app_module.reconcile_expired_voice_sessions()
        connection = self.connect()
        rows = connection.execute(
            """
            SELECT status, disconnect_reason, duration_seconds
            FROM agent_voice_sessions ORDER BY id
            """
        ).fetchall()
        connection.close()
        self.assertTrue(first["ok"])
        self.assertEqual(first["invalid_session_timestamp"], 2)
        self.assertEqual(second["total"], 0)
        self.assertEqual(
            rows,
            [
                ("ended", "invalid_session_timestamp", 0),
                ("ended", "invalid_session_timestamp", 0),
            ]
        )

    def test_phase_2b1_schema_migrates_reconciles_and_is_repeatable(self):
        connection = self.connect()
        connection.execute(
            """
            CREATE TABLE agent_voice_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                conversation_id INTEGER,
                project_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                model_name TEXT,
                voice_name TEXT,
                started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                disconnect_reason TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO agent_voice_sessions (user_id, status) VALUES (7, 'active')"
        )
        connection.execute(
            "INSERT INTO agent_voice_sessions (user_id, status) VALUES (7, 'active')"
        )
        connection.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, disconnect_reason
            ) VALUES (8, 'ended', 'preserved_reason')
            """
        )
        connection.execute(
            "INSERT INTO agent_voice_sessions (user_id, status) VALUES (9, 'active')"
        )
        connection.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, started_at, created_at
            ) VALUES (10, 'starting', '2099-01-01', '2099-01-01')
            """
        )
        connection.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, started_at, created_at
            ) VALUES (10, 'starting', '2099-01-02', '2099-01-02')
            """
        )
        connection.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, started_at, created_at
            ) VALUES (11, 'starting', '2099-01-01', '2099-01-01')
            """
        )
        connection.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, started_at, created_at
            ) VALUES (11, 'active', '2099-01-02', '2099-01-02')
            """
        )
        connection.commit()
        connection.close()

        with mock.patch.object(app_module, "db", side_effect=self.connect), \
                mock.patch.object(app_module, "using_postgres", return_value=False):
            app_module.init_db()
            app_module.init_db()

        connection = self.connect()
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(agent_voice_sessions)"
            )
        }
        user_seven = connection.execute(
            """
            SELECT id, status, disconnect_reason
            FROM agent_voice_sessions
            WHERE user_id = 7 ORDER BY id
            """
        ).fetchall()
        preserved = connection.execute(
            """
            SELECT status, disconnect_reason
            FROM agent_voice_sessions WHERE user_id = 8
            """
        ).fetchone()
        single_active = connection.execute(
            "SELECT status FROM agent_voice_sessions WHERE user_id = 9"
        ).fetchone()
        starting_duplicates = connection.execute(
            """
            SELECT status, disconnect_reason
            FROM agent_voice_sessions WHERE user_id = 10 ORDER BY id
            """
        ).fetchall()
        mixed_duplicates = connection.execute(
            """
            SELECT status, disconnect_reason
            FROM agent_voice_sessions WHERE user_id = 11 ORDER BY id
            """
        ).fetchall()
        indexes = {
            row[0]: row[1]
            for row in connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE type = 'index' AND name LIKE 'idx_agent_voice_sessions_%'
                """
            )
        }
        connection.close()

        self.assertTrue(
            {"handshake_request_id", "expires_at", "updated_at"} <= columns
        )
        self.assertEqual(
            sum(status in {"starting", "active"} for _, status, _ in user_seven),
            1
        )
        self.assertEqual(
            user_seven[0][2], "duplicate_session_reconciled"
        )
        self.assertEqual(preserved, ("ended", "preserved_reason"))
        self.assertEqual(single_active, ("active",))
        self.assertEqual(
            starting_duplicates,
            [
                ("ended", "duplicate_session_reconciled"),
                ("starting", None),
            ]
        )
        self.assertEqual(
            mixed_duplicates,
            [
                ("ended", "duplicate_session_reconciled"),
                ("active", None),
            ]
        )
        self.assertIn("idx_agent_voice_sessions_user_request", indexes)
        self.assertIn("idx_agent_voice_sessions_user_nonterminal", indexes)
        self.assertIn(
            "idx_agent_voice_sessions_status_expires_user", indexes
        )
        self.assertIn("WHERE handshake_request_id IS NOT NULL", indexes[
            "idx_agent_voice_sessions_user_request"
        ])
        self.assertIn("WHERE status IN ('starting', 'active')", indexes[
            "idx_agent_voice_sessions_user_nonterminal"
        ])

    def test_fresh_schema_has_starting_default_and_unique_indexes(self):
        with mock.patch.object(app_module, "db", side_effect=self.connect), \
                mock.patch.object(app_module, "using_postgres", return_value=False):
            app_module.init_db()
        connection = self.connect()
        status_column = next(
            row for row in connection.execute(
                "PRAGMA table_info(agent_voice_sessions)"
            ) if row[1] == "status"
        )
        index_names = {
            row[1] for row in connection.execute(
                "PRAGMA index_list(agent_voice_sessions)"
            )
        }
        connection.close()
        self.assertEqual(status_column[4], "'starting'")
        self.assertIn(
            "idx_agent_voice_sessions_user_request", index_names
        )
        self.assertIn(
            "idx_agent_voice_sessions_user_nonterminal", index_names
        )
        self.assertIn(
            "idx_agent_voice_sessions_status_expires_user", index_names
        )

        connection = self.connect()
        connection.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, handshake_request_id
            ) VALUES (7, 'starting', 'unique-attempt')
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO agent_voice_sessions (
                    user_id, status, handshake_request_id
                ) VALUES (7, 'starting', 'other-attempt')
                """
            )
        connection.rollback()
        connection.execute(
            """
            UPDATE agent_voice_sessions
            SET status = 'ended' WHERE user_id = 7
            """
        )
        connection.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, handshake_request_id
            ) VALUES (8, 'ended', 'reused-index-test')
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO agent_voice_sessions (
                    user_id, status, handshake_request_id
                ) VALUES (8, 'ended', 'reused-index-test')
                """
            )
        connection.close()


class FakeCursor:
    def __init__(self):
        self.executions = []

    def execute(self, statement, parameters=None):
        self.executions.append((statement, parameters))


class FakeConnection:
    def __init__(self):
        self.cursor_value = FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class PostgreSQLAdmissionPathTestCase(unittest.TestCase):
    def test_transaction_scoped_advisory_lock_precedes_admission_and_commits(self):
        connection = FakeConnection()
        order = []

        def core(*args):
            order.append("core")
            return {"ok": True}

        with mock.patch.object(app_module, "db", return_value=connection), \
                mock.patch.object(app_module, "using_postgres", return_value=True), \
                mock.patch.object(
                    app_module, "_admit_voice_session_in_transaction",
                    side_effect=core
                ):
            result = app_module.admit_voice_session(
                7, 42, None, "postgres-attempt"
            )

        self.assertTrue(result["ok"])
        statement, parameters = connection.cursor_value.executions[0]
        self.assertIn("pg_advisory_xact_lock", statement)
        self.assertEqual(
            parameters, (app_module.VOICE_ADMISSION_LOCK_NAMESPACE, 7)
        )
        self.assertEqual(order, ["core"])
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_postgresql_failure_rolls_back_and_closes(self):
        connection = FakeConnection()
        with mock.patch.object(app_module, "db", return_value=connection), \
                mock.patch.object(app_module, "using_postgres", return_value=True), \
                mock.patch.object(
                    app_module, "_admit_voice_session_in_transaction",
                    side_effect=RuntimeError("synthetic failure")
                ):
            result = app_module.admit_voice_session(
                7, 42, None, "postgres-failure"
            )
        self.assertEqual(result["code"], "voice_admission_busy")
        self.assertFalse(connection.committed)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
