import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock

import requests

import app as app_module
import worker as worker_module


VALID_SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
SYNTHETIC_CALL_ID = "synthetic-call-id"


class LocationValidationTestCase(unittest.TestCase):
    def test_documented_relative_and_canonical_absolute_locations(self):
        self.assertEqual(
            app_module.extract_realtime_call_id(
                "/v1/realtime/calls/synthetic-call-id"
            ),
            SYNTHETIC_CALL_ID,
        )
        self.assertEqual(
            app_module.extract_realtime_call_id(
                "https://api.openai.com/v1/realtime/calls/synthetic-call-id"
            ),
            SYNTHETIC_CALL_ID,
        )
        self.assertEqual(
            app_module.extract_realtime_call_id(
                "/v1/realtime/calls/opaque:unicode-é"
            ),
            "opaque:unicode-é",
        )

    def test_location_authority_and_path_rejections(self):
        invalid_locations = (
            "http://api.openai.com/v1/realtime/calls/id",
            "https://evil.example/v1/realtime/calls/id",
            "https://api.openai.com:443/v1/realtime/calls/id",
            "https://user@api.openai.com/v1/realtime/calls/id",
            "https://user:pass@api.openai.com/v1/realtime/calls/id",
            "/v1/realtime/call/id",
            "/v1/realtime/calls/id/extra",
            "v1/realtime/calls/id",
        )
        for value in invalid_locations:
            with self.subTest(value=value):
                self.assertIsNone(app_module.extract_realtime_call_id(value))

    def test_location_query_fragment_encoding_and_segment_rejections(self):
        invalid_locations = (
            "/v1/realtime/calls/id?x=1",
            "/v1/realtime/calls/id#fragment",
            "/v1/realtime/calls/",
            "/v1/realtime/calls/.",
            "/v1/realtime/calls/..",
            "/v1/realtime/calls/a%2Fb",
            "/v1/realtime/calls/a%5Cb",
            "/v1/realtime/calls/a%41",
            "/v1/realtime/calls/a\\b",
            "/v1/realtime/calls/a b",
            "/v1/realtime/calls/a\tb",
            "/v1/realtime/calls/a\nb",
            "/v1/realtime/calls/a\u0080b",
        )
        for value in invalid_locations:
            with self.subTest(value=value):
                self.assertIsNone(app_module.extract_realtime_call_id(value))

    def test_call_id_byte_limit_and_no_prefix_requirement(self):
        self.assertTrue(app_module.valid_realtime_call_id("not-rtc-prefixed"))
        self.assertTrue(app_module.valid_realtime_call_id("é" * 127))
        self.assertFalse(app_module.valid_realtime_call_id("é" * 128))
        self.assertIsNone(
            app_module.extract_realtime_call_id(
                "/v1/realtime/calls/" + ("é" * 128)
            )
        )

    def test_location_parser_adversarial_edge_cases(self):
        self.assertEqual(
            app_module.extract_realtime_call_id(
                "https://API.OpenAI.COM/v1/realtime/calls/mixed-case-host"
            ),
            "mixed-case-host",
        )
        invalid_locations = (
            "https://api.openai.com./v1/realtime/calls/id",
            "https://api.openai.com\u3002evil/v1/realtime/calls/id",
            "https://api%2eopenai.com/v1/realtime/calls/id",
            "/v1/realtime/calls/a%252Fb",
            "/v1/realtime/calls/a%255Cb",
            "/v1/realtime/calls/a%ZZb",
            "/v1/realtime/calls/id;parameter",
            "//api.openai.com/v1/realtime/calls/id",
            "//v1/realtime/calls/id",
            "/v1/realtime/calls/a\rb",
            "/v1/realtime/calls/a\nb",
            "/v1/realtime/calls/a\tb",
            "/v1/realtime/calls/a\x00b",
            "/v1/realtime/calls/a\x1fb",
            "/v1/realtime/calls/%2e",
            "/v1/realtime/calls/%2e%2e",
            " /v1/realtime/calls/id",
            "/v1/realtime/calls/id ",
            "/v1/realtime/calls/internal space",
        )
        for value in invalid_locations:
            with self.subTest(value=repr(value)):
                self.assertIsNone(app_module.extract_realtime_call_id(value))


class StructuredHandshakeTestCase(unittest.TestCase):
    def environment(self):
        return mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-test-key"},
            clear=True,
        )

    def test_exact_201_sdp_and_location_are_required(self):
        response = mock.Mock(
            status_code=201,
            text=VALID_SDP,
            headers={"Location": "/v1/realtime/calls/synthetic-call-id"},
        )
        with self.environment(), mock.patch.object(
            app_module.requests, "post", return_value=response
        ):
            result = app_module.create_realtime_sdp_answer(
                7, VALID_SDP, "marin"
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["answer_sdp"], VALID_SDP)
        self.assertEqual(result["call_id"], SYNTHETIC_CALL_ID)

    def test_missing_or_malformed_location_withholds_sdp(self):
        for headers in ({}, {"Location": "https://evil.example/call"}):
            response = mock.Mock(
                status_code=201, text=VALID_SDP, headers=headers
            )
            with self.subTest(headers=headers), self.environment(), \
                    mock.patch.object(
                        app_module.requests, "post", return_value=response
                    ):
                result = app_module.create_realtime_sdp_answer(
                    7, VALID_SDP, "marin"
                )
            self.assertFalse(result["ok"])
            self.assertIsNone(result["answer_sdp"])
            self.assertIsNone(result["call_id"])
            self.assertEqual(
                result["disconnect_reason"], "upstream_call_unidentified"
            )

    def test_other_2xx_is_not_success(self):
        response = mock.Mock(status_code=200, text=VALID_SDP, headers={})
        with self.environment(), mock.patch.object(
            app_module.requests, "post", return_value=response
        ):
            result = app_module.create_realtime_sdp_answer(
                7, VALID_SDP, "marin"
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_upstream_response")


class IsolatedTerminationTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(
            self.temp_dir.name, "voice-termination.db"
        )
        self.db_patch = mock.patch.object(
            app_module, "db", side_effect=self.connect
        )
        self.postgres_patch = mock.patch.object(
            app_module, "using_postgres", return_value=False
        )
        self.db_patch.start()
        self.postgres_patch.start()
        app_module.init_db()

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

    def insert_session(
        self,
        status="active",
        call_id=SYNTHETIC_CALL_ID,
        termination_status="not_requested",
        expires_at=None,
        attempts=0,
        lease_expires_at=None,
        user_id=7,
    ):
        now = app_module.utc_now()
        expires_at = expires_at or now + timedelta(minutes=10)
        connection = self.connect()
        try:
            cursor = connection.execute(
                """
                INSERT INTO agent_voice_sessions (
                    user_id, status, handshake_request_id, upstream_call_id,
                    termination_status, termination_attempts, started_at,
                    expires_at, termination_lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    status,
                    "synthetic-request-" + os.urandom(4).hex(),
                    call_id,
                    termination_status,
                    attempts,
                    now,
                    expires_at,
                    lease_expires_at,
                    now,
                    now,
                ),
            )
            session_id = cursor.lastrowid
            connection.commit()
            return session_id
        finally:
            connection.close()

    def row(self, session_id):
        return self.execute(
            """
            SELECT status, upstream_call_id, termination_status,
                   termination_attempts, termination_requested_at,
                   termination_next_attempt_at, termination_accepted_at,
                   termination_error_code, termination_claim_token,
                   termination_lease_expires_at
            FROM agent_voice_sessions WHERE id = ?
            """,
            (session_id,),
        )[0]

    def test_fresh_schema_and_indexes(self):
        columns = {
            row[1] for row in self.execute(
                "PRAGMA table_info(agent_voice_sessions)"
            )
        }
        indexes = {
            row[1] for row in self.execute(
                "PRAGMA index_list(agent_voice_sessions)"
            )
        }
        self.assertTrue(
            {
                "upstream_call_id",
                "termination_status",
                "termination_attempts",
                "termination_requested_at",
                "termination_last_attempt_at",
                "termination_next_attempt_at",
                "termination_accepted_at",
                "termination_error_code",
                "termination_claim_token",
                "termination_lease_expires_at",
            } <= columns
        )
        self.assertIn(
            "idx_agent_voice_sessions_upstream_call", indexes
        )
        self.assertIn(
            "idx_agent_voice_sessions_termination_due", indexes
        )

    def test_atomic_activation_stores_call_identity(self):
        now = app_module.utc_now()
        session_id = self.insert_session(
            status="starting", call_id=None,
            termination_status="not_applicable"
        )
        result = app_module.activate_voice_session(
            7,
            session_id,
            self.execute(
                "SELECT handshake_request_id FROM agent_voice_sessions WHERE id=?",
                (session_id,),
            )[0][0],
            SYNTHETIC_CALL_ID,
            now=now,
        )
        row = self.row(session_id)
        self.assertTrue(result["ok"])
        self.assertEqual(row[0:3], (
            "active", SYNTHETIC_CALL_ID, "not_requested"
        ))

    def test_invalid_and_duplicate_call_ids_cannot_activate(self):
        first = self.insert_session(
            status="ended", termination_status="accepted"
        )
        second = self.insert_session(
            status="starting", call_id=None,
            termination_status="not_applicable"
        )
        request_id = self.execute(
            "SELECT handshake_request_id FROM agent_voice_sessions WHERE id=?",
            (second,),
        )[0][0]
        invalid = app_module.activate_voice_session(
            7, second, request_id, "bad/id"
        )
        duplicate = app_module.activate_voice_session(
            7, second, request_id, SYNTHETIC_CALL_ID
        )
        self.assertFalse(invalid["ok"])
        self.assertFalse(duplicate["ok"])
        self.assertEqual(duplicate["code"], "duplicate_upstream_call_id")
        self.assertEqual(self.row(first)[0], "ended")
        self.assertEqual(self.row(second)[0], "starting")

    def test_activation_cleanup_preserves_terminal_states_repeatedly(self):
        now = app_module.utc_now()
        cases = (
            ("accepted", "synthetic-accepted-call"),
            ("failed_permanent", "synthetic-permanent-call"),
        )
        for status, call_id in cases:
            with self.subTest(status=status):
                session_id = self.insert_session(
                    status="ended", call_id=call_id,
                    termination_status=status, attempts=5,
                )
                accepted_at = now if status == "accepted" else None
                error_code = (
                    "synthetic_permanent_error"
                    if status == "failed_permanent" else None
                )
                self.execute(
                    """
                    UPDATE agent_voice_sessions
                    SET termination_accepted_at = ?,
                        termination_error_code = ?,
                        termination_next_attempt_at = ?,
                        termination_claim_token = 'synthetic-old-token',
                        termination_lease_expires_at = ?
                    WHERE id = ?
                    """,
                    (
                        accepted_at, error_code, now + timedelta(minutes=2),
                        now + timedelta(minutes=1), session_id,
                    ),
                )
                request_id = self.execute(
                    """
                    SELECT handshake_request_id
                    FROM agent_voice_sessions WHERE id = ?
                    """,
                    (session_id,),
                )[0][0]
                for _ in range(2):
                    result = app_module.prepare_voice_activation_failure(
                        7, session_id, request_id, call_id, now=now
                    )
                    self.assertTrue(result["durable"])
                stored = self.execute(
                    """
                    SELECT termination_status, termination_attempts,
                           termination_accepted_at, termination_error_code,
                           termination_next_attempt_at,
                           termination_claim_token,
                           termination_lease_expires_at
                    FROM agent_voice_sessions WHERE id = ?
                    """,
                    (session_id,),
                )[0]
                self.assertEqual(stored[0], status)
                self.assertEqual(stored[1], 5)
                self.assertEqual(
                    app_module.parse_db_datetime(stored[2]), accepted_at
                )
                self.assertEqual(stored[3], error_code)
                self.assertIsNone(stored[4])
                self.assertIsNone(stored[5])
                self.assertIsNone(stored[6])

    def test_activation_cleanup_preserves_valid_in_progress_lease(self):
        now = app_module.utc_now()
        lease = now + timedelta(minutes=1)
        session_id = self.insert_session(
            status="ended", call_id="synthetic-leased-call",
            termination_status="in_progress", attempts=3,
            lease_expires_at=lease,
        )
        self.execute(
            """
            UPDATE agent_voice_sessions
            SET termination_claim_token = 'synthetic-live-token'
            WHERE id = ?
            """,
            (session_id,),
        )
        request_id = self.execute(
            "SELECT handshake_request_id FROM agent_voice_sessions WHERE id=?",
            (session_id,),
        )[0][0]
        app_module.prepare_voice_activation_failure(
            7, session_id, request_id, "synthetic-leased-call", now=now
        )
        row = self.row(session_id)
        self.assertEqual(row[2], "in_progress")
        self.assertEqual(row[3], 3)
        self.assertEqual(row[8], "synthetic-live-token")
        self.assertEqual(app_module.parse_db_datetime(row[9]), lease)

    def test_activation_cleanup_preserves_pending_due_and_future_retry(self):
        now = app_module.utc_now()
        schedules = (
            now - timedelta(seconds=1),
            now + timedelta(minutes=4),
        )
        for index, schedule in enumerate(schedules):
            with self.subTest(schedule=schedule):
                call_id = f"synthetic-pending-cleanup-{index}"
                session_id = self.insert_session(
                    status="ended", call_id=call_id,
                    termination_status="pending", attempts=2,
                )
                self.execute(
                    """
                    UPDATE agent_voice_sessions
                    SET termination_requested_at = ?,
                        termination_next_attempt_at = ?,
                        termination_error_code = 'synthetic_retry_code'
                    WHERE id = ?
                    """,
                    (now - timedelta(minutes=1), schedule, session_id),
                )
                request_id = self.execute(
                    """
                    SELECT handshake_request_id
                    FROM agent_voice_sessions WHERE id = ?
                    """,
                    (session_id,),
                )[0][0]
                app_module.prepare_voice_activation_failure(
                    7, session_id, request_id, call_id, now=now
                )
                row = self.row(session_id)
                self.assertEqual(row[2], "pending")
                self.assertEqual(row[3], 2)
                self.assertEqual(
                    app_module.parse_db_datetime(row[5]), schedule
                )
                self.assertEqual(row[7], "synthetic_retry_code")

    def test_activation_cleanup_schedules_initial_termination_states(self):
        now = app_module.utc_now()
        cases = (
            ("active", "not_requested", "synthetic-not-requested", True),
            ("starting", "not_applicable", None, False),
        )
        for index, (status, termination_status, call_id, identified) in enumerate(
            cases
        ):
            with self.subTest(termination_status=termination_status):
                expected_call_id = (
                    call_id or f"synthetic-new-cleanup-{index}"
                )
                session_id = self.insert_session(
                    status=status, call_id=call_id,
                    termination_status=termination_status,
                )
                request_id = self.execute(
                    """
                    SELECT handshake_request_id
                    FROM agent_voice_sessions WHERE id = ?
                    """,
                    (session_id,),
                )[0][0]
                result = app_module.prepare_voice_activation_failure(
                    7, session_id, request_id, expected_call_id, now=now
                )
                row = self.row(session_id)
                self.assertTrue(result["durable"])
                self.assertEqual(row[0:3], (
                    "ended", expected_call_id, "pending"
                ))
                self.assertIsNotNone(row[4])
                self.assertIsNotNone(row[5])

    def test_activation_identity_conflict_preserves_existing_states(self):
        now = app_module.utc_now()
        cases = (
            ("active", "not_requested", 0),
            ("ended", "accepted", 3),
            ("ended", "failed_permanent", 5),
            ("ended", "pending", 4),
        )
        for index, (lifecycle, termination, attempts) in enumerate(cases):
            with self.subTest(termination=termination):
                call_a = f"synthetic-existing-call-{index}"
                call_b = f"synthetic-new-call-{index}"
                session_id = self.insert_session(
                    status=lifecycle, call_id=call_a,
                    termination_status=termination, attempts=attempts,
                )
                accepted_at = now if termination == "accepted" else None
                error_code = (
                    "synthetic_permanent_error"
                    if termination == "failed_permanent" else None
                )
                next_attempt = (
                    now + timedelta(minutes=5)
                    if termination == "pending" else None
                )
                self.execute(
                    """
                    UPDATE agent_voice_sessions
                    SET termination_accepted_at = ?,
                        termination_error_code = ?,
                        termination_next_attempt_at = ?
                    WHERE id = ?
                    """,
                    (accepted_at, error_code, next_attempt, session_id),
                )
                request_id = self.execute(
                    """SELECT handshake_request_id
                       FROM agent_voice_sessions WHERE id = ?""",
                    (session_id,),
                )[0][0]
                before = self.row(session_id)
                with mock.patch.object(
                    app_module, "hangup_realtime_call",
                    return_value={
                        "accepted": True, "retryable": False,
                        "error_code": "",
                    },
                ) as hangup, mock.patch.object(
                    app_module, "attempt_voice_termination_for_session"
                ) as durable_attempt:
                    cleanup = app_module.handle_voice_activation_failure(
                        7, session_id, request_id, call_b
                    )
                after = self.row(session_id)
                self.assertEqual(cleanup["code"],
                                 "upstream_call_identity_conflict")
                self.assertFalse(cleanup["durable"])
                self.assertEqual(after, before)
                self.assertEqual(after[1], call_a)
                hangup.assert_called_once_with(call_b)
                durable_attempt.assert_not_called()

    def test_activation_cleanup_scope_mismatch_mutates_nothing(self):
        session_id = self.insert_session(
            status="active", call_id="synthetic-owned-call",
            termination_status="not_requested",
        )
        request_id = self.execute(
            """SELECT handshake_request_id
               FROM agent_voice_sessions WHERE id = ?""",
            (session_id,),
        )[0][0]
        before = self.row(session_id)
        mismatches = (
            (8, session_id, request_id, "synthetic-wrong-user-call"),
            (7, session_id + 999, request_id, "synthetic-wrong-session-call"),
            (7, session_id, "wrong-request", "synthetic-wrong-request-call"),
        )
        with mock.patch.object(
            app_module, "hangup_realtime_call",
            return_value={
                "accepted": True, "retryable": False, "error_code": "",
            },
        ) as hangup:
            for user_id, local_id, handshake_id, new_call in mismatches:
                cleanup = app_module.handle_voice_activation_failure(
                    user_id, local_id, handshake_id, new_call
                )
                self.assertFalse(cleanup["durable"])
        self.assertEqual(self.row(session_id), before)
        self.assertEqual(
            [call.args[0] for call in hangup.call_args_list],
            [item[3] for item in mismatches],
        )

    def test_request_termination_is_durable_and_idempotent(self):
        session_id = self.insert_session()
        first = app_module.request_voice_termination(
            7, session_id, "client_disconnected"
        )
        second = app_module.request_voice_termination(
            7, session_id, "later_reason"
        )
        row = self.row(session_id)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(row[0], "ended")
        self.assertEqual(row[2], "pending")
        self.assertIsNotNone(row[4])
        reason = self.execute(
            "SELECT disconnect_reason FROM agent_voice_sessions WHERE id=?",
            (session_id,),
        )[0][0]
        self.assertEqual(reason, "client_disconnected")

    def test_repeated_end_preserves_pending_retry_schedule(self):
        session_id = self.insert_session(
            status="ended", termination_status="pending"
        )
        now = app_module.utc_now()
        scheduled_at = now + timedelta(minutes=4)
        self.execute(
            """
            UPDATE agent_voice_sessions
            SET termination_requested_at = ?,
                termination_next_attempt_at = ?
            WHERE id = ?
            """,
            (now, scheduled_at, session_id),
        )

        result = app_module.request_voice_termination(
            7, session_id, "repeated_client_end",
            now=now + timedelta(seconds=10),
        )

        self.assertTrue(result["found"])
        stored_schedule = self.execute(
            """
            SELECT termination_next_attempt_at
            FROM agent_voice_sessions
            WHERE id = ?
            """,
            (session_id,),
        )[0][0]
        self.assertEqual(
            app_module.parse_db_datetime(stored_schedule), scheduled_at
        )

    def test_end_without_call_id_remains_not_applicable(self):
        session_id = self.insert_session(
            status="starting", call_id=None,
            termination_status="not_applicable"
        )
        result = app_module.request_voice_termination(7, session_id)
        self.assertEqual(result["termination_status"], "not_applicable")
        self.assertEqual(self.row(session_id)[0:3], (
            "ended", None, "not_applicable"
        ))

    def test_concurrent_requests_and_claims_have_one_owner(self):
        session_id = self.insert_session()
        with ThreadPoolExecutor(max_workers=2) as pool:
            request_results = list(pool.map(
                lambda _: app_module.request_voice_termination(7, session_id),
                range(2),
            ))
        self.assertEqual(sum(result["changed"] for result in request_results), 1)

        barrier = threading.Barrier(2)

        def claim(_):
            barrier.wait()
            return app_module.claim_due_voice_termination(session_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, range(2)))
        self.assertEqual(sum(value is not None for value in claims), 1)

    def test_stale_lease_is_reclaimed(self):
        session_id = self.insert_session(
            status="ended",
            termination_status="in_progress",
            attempts=1,
            lease_expires_at=app_module.utc_now() - timedelta(seconds=1),
        )
        claim = app_module.claim_due_voice_termination(session_id)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["attempt"], 2)

    def test_stale_attempt_seven_is_claimed_once_as_attempt_eight(self):
        now = app_module.utc_now()
        session_id = self.insert_session(
            status="ended",
            termination_status="in_progress",
            attempts=7,
            lease_expires_at=now - timedelta(seconds=1),
        )
        self.execute(
            """
            UPDATE agent_voice_sessions
            SET termination_claim_token = 'synthetic-stale-token'
            WHERE id = ?
            """,
            (session_id,),
        )
        claim = app_module.claim_due_voice_termination(
            session_id, now=now
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim["attempt"], 8)
        row = self.row(session_id)
        self.assertEqual(row[2], "in_progress")
        self.assertEqual(row[3], 8)
        self.assertNotEqual(row[8], "synthetic-stale-token")

    def test_stale_attempt_eight_fails_permanently_without_ninth_http(self):
        now = app_module.utc_now()
        session_id = self.insert_session(
            status="ended",
            termination_status="in_progress",
            attempts=8,
            lease_expires_at=now - timedelta(seconds=1),
        )
        self.execute(
            """
            UPDATE agent_voice_sessions
            SET termination_claim_token = 'synthetic-final-stale-token',
                termination_next_attempt_at = ?
            WHERE id = ?
            """,
            (now - timedelta(seconds=2), session_id),
        )
        with mock.patch.object(
            app_module, "hangup_realtime_call"
        ) as hangup:
            counts = app_module.run_voice_maintenance_once(limit=5)
            repeated = app_module.reconcile_expired_voice_sessions(now=now)
            claim = app_module.claim_due_voice_termination(
                session_id, now=now
            )
        row = self.row(session_id)
        self.assertEqual(counts["processed"], 0)
        self.assertTrue(repeated["ok"])
        self.assertIsNone(claim)
        hangup.assert_not_called()
        self.assertEqual(row[2], "failed_permanent")
        self.assertEqual(row[3], 8)
        self.assertEqual(
            row[7], "voice_termination_attempts_exhausted"
        )
        self.assertIsNone(row[5])
        self.assertIsNone(row[8])
        self.assertIsNone(row[9])

    def test_repeated_end_route_finalizes_stale_attempt_eight(self):
        now = app_module.utc_now()
        session_id = self.insert_session(
            status="ended", termination_status="in_progress",
            attempts=8, lease_expires_at=now - timedelta(seconds=1),
        )
        self.execute(
            """
            UPDATE agent_voice_sessions
            SET termination_claim_token = 'synthetic-expired-final-token',
                termination_next_attempt_at = ?
            WHERE id = ?
            """,
            (now - timedelta(seconds=2), session_id),
        )
        previous_testing = app_module.app.config.get("TESTING")
        app_module.app.config["TESTING"] = True
        self.addCleanup(
            app_module.app.config.__setitem__, "TESTING", previous_testing
        )
        client = app_module.app.test_client()
        csrf_token = "synthetic-csrf-token-that-is-long-enough"
        with client.session_transaction() as browser_session:
            browser_session["user_id"] = 7
            browser_session[app_module.VOICE_CSRF_SESSION_KEY] = csrf_token
        headers = {
            "Origin": "http://localhost",
            "Sec-Fetch-Site": "same-origin",
            app_module.VOICE_CSRF_HEADER: csrf_token,
        }
        with mock.patch.object(
            app_module, "hangup_realtime_call"
        ) as hangup:
            first = client.post(
                "/api/realtime/session/end",
                json={"voice_session_id": session_id}, headers=headers,
            )
            second = client.post(
                "/api/realtime/session/end",
                json={"voice_session_id": session_id}, headers=headers,
            )
            claim = app_module.claim_due_voice_termination(
                session_id, now=now
            )
            maintenance = app_module.run_voice_maintenance_once(limit=5)
        row = self.row(session_id)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.get_json(), {
            "status": "ended", "termination": "failed"
        })
        self.assertEqual(second.get_json(), first.get_json())
        self.assertEqual(row[0], "ended")
        self.assertEqual(row[2], "failed_permanent")
        self.assertEqual(row[3], 8)
        self.assertEqual(
            row[7], "voice_termination_attempts_exhausted"
        )
        self.assertIsNone(row[5])
        self.assertIsNone(row[8])
        self.assertIsNone(row[9])
        self.assertIsNone(claim)
        self.assertEqual(maintenance["processed"], 0)
        hangup.assert_not_called()
        for key in ("call_id", "claim_token", "next_attempt_at"):
            self.assertNotIn(key, first.get_json())

    def test_nonexpired_attempt_eight_lease_is_preserved(self):
        now = app_module.utc_now()
        lease = now + timedelta(minutes=1)
        session_id = self.insert_session(
            status="ended",
            termination_status="in_progress",
            attempts=8,
            lease_expires_at=lease,
        )
        self.execute(
            """
            UPDATE agent_voice_sessions
            SET termination_claim_token = 'synthetic-final-live-token'
            WHERE id = ?
            """,
            (session_id,),
        )
        first = app_module.reconcile_expired_voice_sessions(now=now)
        second = app_module.reconcile_expired_voice_sessions(now=now)
        claim = app_module.claim_due_voice_termination(
            session_id, now=now
        )
        row = self.row(session_id)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertIsNone(claim)
        self.assertEqual(row[2], "in_progress")
        self.assertEqual(row[3], 8)
        self.assertEqual(row[8], "synthetic-final-live-token")
        self.assertEqual(app_module.parse_db_datetime(row[9]), lease)

    def test_terminal_and_not_due_rows_are_not_claimable(self):
        now = app_module.utc_now()
        cases = (
            ("ended", "accepted", "synthetic-nonclaim-accepted"),
            ("ended", "failed_permanent", "synthetic-nonclaim-failed"),
            ("ended", "not_applicable", None),
            ("active", "not_requested", "synthetic-nonclaim-active"),
            ("ended", "pending", "synthetic-nonclaim-future"),
            ("ended", "in_progress", "synthetic-nonclaim-leased"),
        )
        session_ids = []
        for status, termination_status, call_id in cases:
            session_id = self.insert_session(
                status=status,
                call_id=call_id,
                termination_status=termination_status,
                lease_expires_at=(
                    now + timedelta(minutes=1)
                    if termination_status == "in_progress" else None
                ),
            )
            if termination_status == "pending":
                self.execute(
                    """
                    UPDATE agent_voice_sessions
                    SET termination_next_attempt_at = ?
                    WHERE id = ?
                    """,
                    (now + timedelta(minutes=2), session_id),
                )
            session_ids.append(session_id)
        for session_id in session_ids:
            with self.subTest(session_id=session_id):
                self.assertIsNone(
                    app_module.claim_due_voice_termination(
                        session_id, now=now
                    )
                )

    def test_accepted_claim_records_acceptance_not_completion(self):
        session_id = self.insert_session(
            status="ended", termination_status="pending"
        )
        claim = app_module.claim_due_voice_termination(session_id)
        outcome = app_module.finalize_voice_termination_claim(
            claim,
            {"accepted": True, "retryable": False, "error_code": ""},
        )
        row = self.row(session_id)
        self.assertEqual(outcome["status"], "accepted")
        self.assertEqual(row[2], "accepted")
        self.assertIsNotNone(row[6])
        self.assertNotIn(
            "termination_completed_at",
            {
                value[1] for value in self.execute(
                    "PRAGMA table_info(agent_voice_sessions)"
                )
            },
        )

    def test_retry_schedule_and_attempt_exhaustion(self):
        session_id = self.insert_session(
            status="ended", termination_status="pending"
        )
        claim = app_module.claim_due_voice_termination(session_id)
        outcome = app_module.finalize_voice_termination_claim(
            claim,
            {
                "accepted": False,
                "retryable": True,
                "error_code": "upstream_hangup_timeout",
            },
            jitter_seconds=0,
        )
        row = self.row(session_id)
        self.assertEqual(outcome["status"], "pending")
        self.assertEqual(row[2], "pending")
        self.assertIsNotNone(row[5])

        exhausted_id = self.insert_session(
            status="ended",
            call_id="synthetic-exhausted-call",
            termination_status="pending",
            attempts=7,
        )
        exhausted_claim = app_module.claim_due_voice_termination(exhausted_id)
        exhausted = app_module.finalize_voice_termination_claim(
            exhausted_claim,
            {
                "accepted": False,
                "retryable": True,
                "error_code": "upstream_hangup_timeout",
            },
            jitter_seconds=0,
        )
        self.assertEqual(exhausted["status"], "failed_permanent")
        self.assertEqual(self.row(exhausted_id)[2], "failed_permanent")

    def test_old_claimant_cannot_overwrite_new_claim(self):
        session_id = self.insert_session(
            status="ended", termination_status="pending"
        )
        old_claim = app_module.claim_due_voice_termination(session_id)
        self.execute(
            """
            UPDATE agent_voice_sessions
            SET termination_claim_token='synthetic-new-token'
            WHERE id=?
            """,
            (session_id,),
        )
        outcome = app_module.finalize_voice_termination_claim(
            old_claim,
            {"accepted": True, "retryable": False, "error_code": ""},
        )
        self.assertEqual(outcome["status"], "claim_lost")
        self.assertEqual(self.row(session_id)[2], "in_progress")

    def test_expiry_schedules_only_identified_calls(self):
        now = app_module.utc_now()
        identified = self.insert_session(
            expires_at=now - timedelta(seconds=1)
        )
        unidentified = self.insert_session(
            call_id=None,
            termination_status="not_applicable",
            expires_at=now - timedelta(seconds=1),
            user_id=8,
        )
        with mock.patch.object(app_module.requests, "post") as post:
            result = app_module.reconcile_expired_voice_sessions(now=now)
        self.assertTrue(result["ok"])
        post.assert_not_called()
        self.assertEqual(self.row(identified)[0:3], (
            "ended", SYNTHETIC_CALL_ID, "pending"
        ))
        self.assertEqual(self.row(unidentified)[0:3], (
            "ended", None, "not_applicable"
        ))

    def test_worker_maintenance_processes_a_bounded_batch(self):
        for index in range(3):
            self.insert_session(
                status="ended",
                call_id=f"synthetic-call-{index}",
                termination_status="pending",
            )
        with mock.patch.object(
            app_module,
            "hangup_realtime_call",
            return_value={
                "accepted": True, "retryable": False, "error_code": ""
            },
        ) as hangup:
            counts = app_module.run_voice_maintenance_once(limit=2)
        self.assertEqual(counts["processed"], 2)
        self.assertEqual(hangup.call_count, 2)
        statuses = self.execute(
            """
            SELECT termination_status, COUNT(*)
            FROM agent_voice_sessions
            GROUP BY termination_status
            """
        )
        self.assertIn(("accepted", 2), statuses)
        self.assertIn(("pending", 1), statuses)

    def test_concurrent_processors_issue_one_hangup_and_one_finalization(self):
        session_id = self.insert_session(
            status="ended", termination_status="pending"
        )
        start = threading.Barrier(2)

        def process(_):
            start.wait()
            claim = app_module.claim_due_voice_termination(session_id)
            if not claim:
                return {"claimed": False, "status": "claim_lost"}
            outcome = app_module.process_voice_termination_claim(claim)
            return {"claimed": True, "status": outcome["status"]}

        with mock.patch.object(
            app_module,
            "hangup_realtime_call",
            return_value={
                "accepted": True, "retryable": False, "error_code": ""
            },
        ) as hangup, ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(process, range(2)))

        self.assertEqual(
            sum(outcome["claimed"] for outcome in outcomes), 1
        )
        self.assertEqual(
            sum(outcome["status"] == "accepted" for outcome in outcomes), 1
        )
        self.assertEqual(hangup.call_count, 1)
        row = self.row(session_id)
        self.assertEqual(row[2], "accepted")
        self.assertIsNone(row[8])
        self.assertIsNone(row[9])
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        finally:
            connection.close()

    def test_abandoned_end_response_preserves_worker_recovery(self):
        session_id = self.insert_session()
        previous_testing = app_module.app.config.get("TESTING")
        app_module.app.config["TESTING"] = True
        self.addCleanup(
            app_module.app.config.__setitem__, "TESTING", previous_testing
        )
        client = app_module.app.test_client()
        csrf_token = "synthetic-csrf-token-that-is-long-enough"
        with client.session_transaction() as browser_session:
            browser_session["user_id"] = 7
            browser_session[app_module.VOICE_CSRF_SESSION_KEY] = csrf_token
        headers = {
            "Origin": "http://localhost",
            "Sec-Fetch-Site": "same-origin",
            app_module.VOICE_CSRF_HEADER: csrf_token,
        }
        with mock.patch.object(
            app_module,
            "attempt_voice_termination_for_session",
            side_effect=RuntimeError("synthetic abandoned response"),
        ):
            abandoned = client.post(
                "/api/realtime/session/end",
                json={
                    "voice_session_id": session_id,
                    "reason": "client_disconnected",
                },
                headers=headers,
            )
        self.assertEqual(abandoned.status_code, 500)

        durable = self.row(session_id)
        self.assertEqual(durable[0], "ended")
        self.assertEqual(durable[2], "pending")
        claim = app_module.claim_due_voice_termination(session_id)
        self.assertIsNotNone(claim)
        with mock.patch.object(
            app_module,
            "hangup_realtime_call",
            return_value={
                "accepted": True, "retryable": False, "error_code": ""
            },
        ):
            outcome = app_module.process_voice_termination_claim(claim)
        self.assertEqual(outcome["status"], "accepted")
        repeated = client.post(
            "/api/realtime/session/end",
            json={"voice_session_id": session_id},
            headers=headers,
        )
        self.assertNotIn("call_id", repeated.get_json())
        self.assertNotIn("claim_token", repeated.get_json())


class TrackingSQLiteConnection:
    def __init__(self, connection):
        self.connection = connection
        self.rolled_back = False
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def rollback(self):
        self.rolled_back = True
        return self.connection.rollback()

    def close(self):
        self.closed = True
        return self.connection.close()


class VoiceCallIdMigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(
            self.temp_dir.name, "voice-call-id-migration.db"
        )
        connection = self.connect()
        connection.execute(
            """
            CREATE TABLE agent_voice_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'starting',
                upstream_call_id TEXT,
                termination_status TEXT NOT NULL DEFAULT 'not_applicable',
                termination_attempts INTEGER NOT NULL DEFAULT 0,
                termination_requested_at TIMESTAMP,
                termination_last_attempt_at TIMESTAMP,
                termination_next_attempt_at TIMESTAMP,
                termination_accepted_at TIMESTAMP,
                termination_error_code TEXT,
                termination_claim_token TEXT,
                termination_lease_expires_at TIMESTAMP,
                started_at TIMESTAMP,
                expires_at TIMESTAMP,
                ended_at TIMESTAMP,
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                disconnect_reason TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
            """
        )
        connection.commit()
        connection.close()
        self.db_patch = mock.patch.object(
            app_module, "db", side_effect=self.connect
        )
        self.postgres_patch = mock.patch.object(
            app_module, "using_postgres", return_value=False
        )
        self.db_patch.start()
        self.postgres_patch.start()

    def tearDown(self):
        self.postgres_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def connect(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def insert_preindex(
        self, user_id, status, call_id, termination_status,
        updated_at, attempts=0, token=None, lease=None, next_attempt=None,
    ):
        connection = self.connect()
        cursor = connection.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, upstream_call_id, termination_status,
                termination_attempts, termination_next_attempt_at,
                termination_claim_token, termination_lease_expires_at,
                started_at, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, status, call_id, termination_status, attempts,
                next_attempt, token, lease, updated_at,
                "2099-01-01T00:00:00+00:00", updated_at, updated_at,
            ),
        )
        connection.commit()
        session_id = cursor.lastrowid
        connection.close()
        return session_id

    def fetch(self, statement, parameters=()):
        connection = self.connect()
        rows = connection.execute(statement, parameters).fetchall()
        connection.close()
        return rows

    def test_duplicate_survivors_are_deterministic_and_index_is_repeatable(self):
        active = self.insert_preindex(
            11, "active", "duplicate-mixed", "not_requested",
            "2099-01-04T00:00:00+00:00",
        )
        accepted = self.insert_preindex(
            12, "ended", "duplicate-mixed", "accepted",
            "2099-01-01T00:00:00+00:00", attempts=2,
        )
        ended_old = self.insert_preindex(
            13, "ended", "duplicate-ended", "pending",
            "2099-01-02T00:00:00+00:00",
        )
        ended_new = self.insert_preindex(
            14, "ended", "duplicate-ended", "pending",
            "2099-01-01T00:00:00+00:00",
        )
        triple_ids = [
            self.insert_preindex(
                20 + index, "active", "duplicate-triple", "not_requested",
                "2099-01-03T00:00:00+00:00",
            )
            for index in range(3)
        ]

        app_module.init_db()
        app_module.init_db()

        mixed = self.fetch(
            """
            SELECT id, status, upstream_call_id, termination_status,
                   termination_error_code
            FROM agent_voice_sessions WHERE id IN (?, ?) ORDER BY id
            """,
            (active, accepted),
        )
        self.assertIsNone(mixed[0][2])
        self.assertEqual(
            mixed[0][4], "duplicate_upstream_call_id_reconciled"
        )
        self.assertEqual(mixed[1][2:4], ("duplicate-mixed", "accepted"))
        ended = self.fetch(
            """
            SELECT id, upstream_call_id, termination_error_code
            FROM agent_voice_sessions WHERE id IN (?, ?) ORDER BY id
            """,
            (ended_old, ended_new),
        )
        self.assertEqual(ended[0][1], "duplicate-ended")
        self.assertIsNone(ended[1][1])
        triple = self.fetch(
            """
            SELECT id, upstream_call_id
            FROM agent_voice_sessions
            WHERE id IN (?, ?, ?) ORDER BY id
            """,
            tuple(triple_ids),
        )
        self.assertEqual(
            [row[0] for row in triple if row[1] == "duplicate-triple"],
            [max(triple_ids)],
        )
        indexes = {
            row[1] for row in self.fetch(
                "PRAGMA index_list(agent_voice_sessions)"
            )
        }
        self.assertIn(
            "idx_agent_voice_sessions_upstream_call", indexes
        )

    def test_invalid_identities_are_cleared_without_reviving_terminals(self):
        invalid_values = (
            "",
            "   ",
            "invalid/slash",
            "invalid\\backslash",
            "invalid\x00control",
            "é" * 128,
        )
        ids = []
        for index, call_id in enumerate(invalid_values):
            status = "active" if index < 4 else "ended"
            termination_status = (
                "accepted" if index == 4
                else "failed_permanent" if index == 5
                else "not_requested"
            )
            ids.append(self.insert_preindex(
                40 + index, status, call_id, termination_status,
                "2099-01-01T00:00:00+00:00",
                attempts=4,
                token="synthetic-migration-token",
                lease="2099-01-02T00:00:00+00:00",
                next_attempt="2099-01-02T00:00:00+00:00",
            ))

        app_module.init_db()
        app_module.init_db()

        rows = self.fetch(
            """
            SELECT status, upstream_call_id, termination_status,
                   termination_attempts, termination_error_code,
                   termination_claim_token, termination_lease_expires_at,
                   termination_next_attempt_at, duration_seconds
            FROM agent_voice_sessions
            WHERE id IN (?, ?, ?, ?, ?, ?)
            ORDER BY id
            """,
            tuple(ids),
        )
        for row in rows:
            self.assertEqual(row[0], "ended")
            self.assertIsNone(row[1])
            self.assertEqual(row[2], "not_applicable")
            self.assertEqual(row[3], 0)
            self.assertEqual(
                row[4], "invalid_upstream_call_id_reconciled"
            )
            self.assertIsNone(row[5])
            self.assertIsNone(row[6])
            self.assertIsNone(row[7])
            self.assertGreaterEqual(row[8], 0)

    def test_valid_identity_state_combinations_are_reconciled(self):
        now = "2099-01-01T00:00:00+00:00"
        ended_not_requested = self.insert_preindex(
            60, "ended", "valid-ended-not-requested", "not_requested", now
        )
        active_pending = self.insert_preindex(
            61, "active", "valid-active-pending", "pending", now
        )
        accepted_dirty = self.insert_preindex(
            62, "ended", "valid-accepted-dirty", "accepted", now,
            attempts=3, token="synthetic-dirty-token",
            lease="2099-01-02T00:00:00+00:00",
            next_attempt="2099-01-02T00:00:00+00:00",
        )

        app_module.init_db()

        rows = {
            row[0]: row[1:]
            for row in self.fetch(
                """
                SELECT id, status, termination_status,
                       termination_claim_token,
                       termination_lease_expires_at,
                       termination_next_attempt_at
                FROM agent_voice_sessions
                WHERE id IN (?, ?, ?)
                """,
                (ended_not_requested, active_pending, accepted_dirty),
            )
        }
        self.assertEqual(
            rows[ended_not_requested][0:2], ("ended", "pending")
        )
        self.assertEqual(rows[active_pending][0:2], ("ended", "pending"))
        self.assertEqual(
            rows[accepted_dirty],
            ("ended", "accepted", None, None, None),
        )

    def test_migration_failure_rolls_back_closes_and_skips_unique_index(self):
        tracking = TrackingSQLiteConnection(self.connect())
        observed = {}

        def fail_after_columns(cursor, now=None):
            cursor.execute("PRAGMA table_info(agent_voice_sessions)")
            observed["columns"] = {row[1] for row in cursor.fetchall()}
            raise RuntimeError("synthetic raw migration failure detail")

        with self.assertLogs(app_module.logger, level="WARNING") as logs, \
                mock.patch.object(app_module, "db", return_value=tracking), \
                mock.patch.object(
                    app_module,
                    "_reconcile_voice_call_ids_for_migration",
                    side_effect=fail_after_columns,
                ), mock.patch.object(app_module.requests, "post") as post:
            with self.assertRaisesRegex(
                RuntimeError, "^database_initialization_failed$"
            ):
                app_module.init_db()

        self.assertIn("termination_claim_token", observed["columns"])
        self.assertTrue(tracking.rolled_back)
        self.assertTrue(tracking.closed)
        indexes = {
            row[1] for row in self.fetch(
                "PRAGMA index_list(agent_voice_sessions)"
            )
        }
        self.assertNotIn(
            "idx_agent_voice_sessions_upstream_call", indexes
        )
        combined_logs = "\n".join(logs.output)
        self.assertIn("schema_migration_failed", combined_logs)
        self.assertNotIn("synthetic raw migration failure detail", combined_logs)
        post.assert_not_called()


class HangupClassificationTestCase(unittest.TestCase):
    def environment(self):
        return mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-test-key"},
            clear=True,
        )

    def test_pinned_hangup_shape_and_200_acceptance(self):
        response = mock.Mock(status_code=200)
        with self.environment(), mock.patch.object(
            app_module.requests, "post", return_value=response
        ) as post:
            result = app_module.hangup_realtime_call(SYNTHETIC_CALL_ID)
        self.assertTrue(result["accepted"])
        post.assert_called_once_with(
            "https://api.openai.com/v1/realtime/calls/"
            "synthetic-call-id/hangup",
            headers={"Authorization": "Bearer synthetic-test-key"},
            timeout=(3.0, 10.0),
            allow_redirects=False,
        )

    def test_status_classifications(self):
        cases = (
            (401, False, False, "upstream_hangup_authentication_failed"),
            (403, False, False, "upstream_hangup_authentication_failed"),
            (429, False, True, "upstream_hangup_rate_limited"),
            (500, False, True, "upstream_hangup_unavailable"),
            (302, False, False, "upstream_hangup_protocol_error"),
            (204, False, False, "upstream_hangup_protocol_error"),
            (404, False, False, "upstream_hangup_protocol_error"),
        )
        for status, accepted, retryable, code in cases:
            with self.subTest(status=status), self.environment(), \
                    mock.patch.object(
                        app_module.requests,
                        "post",
                        return_value=mock.Mock(status_code=status),
                    ):
                result = app_module.hangup_realtime_call(SYNTHETIC_CALL_ID)
            self.assertEqual(result["accepted"], accepted)
            self.assertEqual(result["retryable"], retryable)
            self.assertEqual(result["error_code"], code)

    def test_network_failure_classifications(self):
        cases = (
            (requests.Timeout("synthetic"), "upstream_hangup_timeout"),
            (
                requests.ConnectionError("synthetic"),
                "upstream_hangup_unavailable",
            ),
        )
        for error, code in cases:
            with self.subTest(code=code), self.environment(), \
                    mock.patch.object(
                        app_module.requests, "post", side_effect=error
                    ):
                result = app_module.hangup_realtime_call(SYNTHETIC_CALL_ID)
            self.assertTrue(result["retryable"])
            self.assertEqual(result["error_code"], code)

    def test_missing_api_key_is_permanent_and_makes_no_request(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(app_module.requests, "post") as post, \
                mock.patch.object(app_module.logger, "warning") as warning:
            result = app_module.hangup_realtime_call(SYNTHETIC_CALL_ID)
        self.assertEqual(result, {
            "accepted": False,
            "retryable": False,
            "error_code": "upstream_hangup_authentication_failed",
        })
        post.assert_not_called()
        warning.assert_not_called()

    def test_generic_request_exception_is_retryable_and_sanitized(self):
        with self.environment(), mock.patch.object(
            app_module.requests,
            "post",
            side_effect=requests.RequestException(
                "synthetic request detail with call and URL"
            ),
        ), mock.patch.object(app_module.logger, "warning") as warning:
            result = app_module.hangup_realtime_call(SYNTHETIC_CALL_ID)
        self.assertEqual(result, {
            "accepted": False,
            "retryable": True,
            "error_code": "upstream_hangup_unavailable",
        })
        warning.assert_not_called()

    def test_backoff_is_bounded_and_injectable(self):
        expected = (15, 30, 60, 120, 240, 480, 900, 900)
        self.assertEqual(
            tuple(
                app_module.voice_termination_backoff_seconds(
                    attempt, jitter_seconds=0
                )
                for attempt in range(1, 9)
            ),
            expected,
        )
        self.assertEqual(
            app_module.voice_termination_backoff_seconds(
                1, jitter_seconds=99
            ),
            20,
        )


class FakePostgresCursor:
    def __init__(
        self, selected_row=None, rowcount=1, fail_pattern=None, events=None
    ):
        self.selected_row = selected_row
        self.executed = []
        self.rowcount = rowcount
        self.fail_pattern = fail_pattern
        self.events = events if events is not None else []

    def execute(self, statement, parameters=()):
        self.executed.append((statement, parameters))
        self.events.append(("execute", statement))
        if self.fail_pattern and self.fail_pattern in statement:
            raise RuntimeError("synthetic sql failure detail")

    def fetchone(self):
        row = self.selected_row
        self.selected_row = None
        return row

    def fetchall(self):
        return []


class FakePostgresConnection:
    def __init__(
        self, selected_row=None, rowcount=1, fail_pattern=None, events=None
    ):
        self.events = events if events is not None else []
        self.cursor_instance = FakePostgresCursor(
            selected_row, rowcount=rowcount,
            fail_pattern=fail_pattern, events=self.events
        )
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True
        self.events.append(("commit", None))

    def rollback(self):
        self.rolled_back = True
        self.events.append(("rollback", None))

    def close(self):
        self.closed = True
        self.events.append(("close", None))


class PostgreSQLTerminationPathTestCase(unittest.TestCase):
    def test_claim_uses_skip_locked_and_closes_before_network(self):
        connection = FakePostgresConnection(
            (91, SYNTHETIC_CALL_ID, 0)
        )
        with mock.patch.object(
            app_module, "using_postgres", return_value=True
        ), mock.patch.object(
            app_module, "db", return_value=connection
        ):
            claim = app_module.claim_due_voice_termination()
        statements = [
            statement for statement, _ in connection.cursor_instance.executed
        ]
        self.assertIsNotNone(claim)
        select_index = next(
            index for index, statement in enumerate(statements)
            if "FOR UPDATE SKIP LOCKED" in statement
        )
        update_index = next(
            index for index, statement in enumerate(statements)
            if "SET termination_status = 'in_progress'" in statement
        )
        self.assertLess(select_index, update_index)
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)

    def test_claim_token_finalization_uses_guarded_update(self):
        connection = FakePostgresConnection()
        claim = {
            "voice_session_id": 91,
            "call_id": SYNTHETIC_CALL_ID,
            "claim_token": "synthetic-claim-token",
            "attempt": 1,
        }
        with mock.patch.object(
            app_module, "using_postgres", return_value=True
        ), mock.patch.object(
            app_module, "db", return_value=connection
        ):
            outcome = app_module.finalize_voice_termination_claim(
                claim,
                {"accepted": True, "retryable": False, "error_code": ""},
            )
        statement, parameters = connection.cursor_instance.executed[0]
        self.assertTrue(outcome["updated"])
        self.assertIn("termination_claim_token = %s", statement)
        self.assertEqual(parameters[-1], "synthetic-claim-token")
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)

    def test_claim_sql_failure_rolls_back_closes_and_never_calls_http(self):
        connection = FakePostgresConnection(
            selected_row=(91, SYNTHETIC_CALL_ID, 0),
            fail_pattern="FOR UPDATE SKIP LOCKED",
        )
        with self.assertLogs(app_module.logger, level="WARNING") as logs, \
                mock.patch.object(
                    app_module, "using_postgres", return_value=True
                ), mock.patch.object(
                    app_module, "db", return_value=connection
                ), mock.patch.object(
                    app_module, "hangup_realtime_call"
                ) as hangup:
            claim = app_module.claim_due_voice_termination()
        self.assertIsNone(claim)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.closed)
        hangup.assert_not_called()
        self.assertNotIn("synthetic sql failure detail", "\n".join(logs.output))
        self.assertIn(
            "voice_termination_claim_lost", "\n".join(logs.output)
        )

    def test_old_postgresql_claim_token_is_rejected(self):
        connection = FakePostgresConnection(rowcount=0)
        claim = {
            "voice_session_id": 91,
            "call_id": SYNTHETIC_CALL_ID,
            "claim_token": "synthetic-old-claim-token",
            "attempt": 1,
        }
        with mock.patch.object(
            app_module, "using_postgres", return_value=True
        ), mock.patch.object(
            app_module, "db", return_value=connection
        ):
            outcome = app_module.finalize_voice_termination_claim(
                claim,
                {"accepted": True, "retryable": False, "error_code": ""},
            )
        statement, parameters = connection.cursor_instance.executed[0]
        self.assertEqual(outcome, {
            "updated": False, "status": "claim_lost"
        })
        self.assertIn("termination_status = 'in_progress'", statement)
        self.assertEqual(parameters[-2], 91)
        self.assertEqual(parameters[-1], "synthetic-old-claim-token")
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_postgresql_claim_commits_and_closes_before_hangup(self):
        events = []
        claim_connection = FakePostgresConnection(
            selected_row=(91, SYNTHETIC_CALL_ID, 0), events=events
        )
        finalization_connection = FakePostgresConnection(events=events)

        def hangup(_call_id):
            self.assertTrue(claim_connection.committed)
            self.assertTrue(claim_connection.closed)
            events.append(("hangup", None))
            return {
                "accepted": True, "retryable": False, "error_code": ""
            }

        with mock.patch.object(
            app_module, "using_postgres", return_value=True
        ), mock.patch.object(
            app_module, "db",
            side_effect=[claim_connection, finalization_connection]
        ) as database, mock.patch.object(
            app_module, "hangup_realtime_call", side_effect=hangup
        ):
            claim = app_module.claim_due_voice_termination()
            self.assertEqual(database.call_count, 1)
            outcome = app_module.process_voice_termination_claim(claim)

        self.assertEqual(outcome["status"], "accepted")
        select_position = next(
            index for index, event in enumerate(events)
            if event[0] == "execute"
            and "FOR UPDATE SKIP LOCKED" in event[1]
        )
        claim_update_position = next(
            index for index, event in enumerate(events)
            if event[0] == "execute"
            and "SET termination_status = 'in_progress'" in event[1]
        )
        first_commit = events.index(("commit", None))
        first_close = events.index(("close", None))
        hangup_position = events.index(("hangup", None))
        self.assertLess(select_position, claim_update_position)
        self.assertLess(claim_update_position, first_commit)
        self.assertLess(first_commit, first_close)
        self.assertLess(first_close, hangup_position)
        self.assertEqual(database.call_count, 2)
        self.assertTrue(finalization_connection.committed)
        self.assertTrue(finalization_connection.closed)

    def test_postgresql_finalization_failure_rolls_back_and_closes(self):
        connection = FakePostgresConnection(
            fail_pattern="SET termination_status = 'accepted'"
        )
        claim = {
            "voice_session_id": 91,
            "call_id": SYNTHETIC_CALL_ID,
            "claim_token": "synthetic-claim-token",
            "attempt": 1,
        }
        with self.assertLogs(app_module.logger, level="WARNING") as logs, \
                mock.patch.object(
                    app_module, "using_postgres", return_value=True
                ), mock.patch.object(
                    app_module, "db", return_value=connection
                ):
            outcome = app_module.finalize_voice_termination_claim(
                claim,
                {"accepted": True, "retryable": False, "error_code": ""},
            )
        self.assertEqual(outcome, {
            "updated": False, "status": "claim_lost"
        })
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.closed)
        self.assertNotIn("synthetic sql failure detail", "\n".join(logs.output))


class WorkerIntegrationTestCase(unittest.TestCase):
    def test_voice_failure_does_not_block_generic_processing(self):
        with mock.patch.object(
            worker_module,
            "run_voice_maintenance_once",
            side_effect=RuntimeError("synthetic failure"),
        ), mock.patch.object(
            worker_module, "run_background_job_once", return_value=True
        ) as generic:
            processed = worker_module.run_worker_iteration("synthetic-worker")
        self.assertTrue(processed)
        generic.assert_called_once_with("synthetic-worker")

    def test_worker_loop_preserves_busy_drain_and_idle_sleep(self):
        stop_requested = mock.Mock()
        with mock.patch.object(
            worker_module, "run_worker_iteration", return_value=True
        ):
            processed = worker_module.run_worker_loop_iteration(
                "synthetic-worker", stop_requested, 7
            )
        self.assertTrue(processed)
        stop_requested.wait.assert_not_called()

        stop_requested.reset_mock()
        with mock.patch.object(
            worker_module, "run_worker_iteration", return_value=False
        ):
            processed = worker_module.run_worker_loop_iteration(
                "synthetic-worker", stop_requested, 7
            )
        self.assertFalse(processed)
        stop_requested.wait.assert_called_once_with(7)

    def test_voice_failure_with_idle_generic_work_sleeps_safely(self):
        stop_requested = mock.Mock()
        with mock.patch.object(
            worker_module,
            "run_voice_maintenance_once",
            side_effect=RuntimeError("synthetic failure"),
        ), mock.patch.object(
            worker_module, "run_background_job_once", return_value=False
        ) as generic:
            processed = worker_module.run_worker_loop_iteration(
                "synthetic-worker", stop_requested, 7
            )
        self.assertFalse(processed)
        generic.assert_called_once_with("synthetic-worker")
        stop_requested.wait.assert_called_once_with(7)


if __name__ == "__main__":
    unittest.main()
