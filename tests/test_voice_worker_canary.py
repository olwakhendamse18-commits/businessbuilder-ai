import os
import runpy
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock

import app as app_module
import worker as worker_module


COMMIT = "c" * 40
OTHER_COMMIT = "d" * 40
INSTANCE_A = "synthetic-canary-instance-a"
INSTANCE_B = "synthetic-canary-instance-b"
PROBE_A = "synthetic-canary-probe-a"
PROBE_B = "synthetic-canary-probe-b"
TOKEN_A = "synthetic-canary-token-a"
TOKEN_B = "synthetic-canary-token-b"


class SQLiteCanaryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.temp_dir.name, "canary.db")
        self.db_patch = mock.patch.object(
            app_module, "db", side_effect=self.connect
        )
        self.pg_patch = mock.patch.object(
            app_module, "using_postgres", return_value=False
        )
        self.db_mock = self.db_patch.start()
        self.pg_patch.start()
        app_module.init_db()

    def tearDown(self):
        self.pg_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def connect(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def execute(self, statement, parameters=()):
        connection = self.connect()
        try:
            cursor = connection.execute(statement, parameters)
            rows = cursor.fetchall()
            connection.commit()
            return rows
        finally:
            connection.close()

    def live_heartbeat(self, instance_id=INSTANCE_A, commit=COMMIT, now=None):
        now = now or app_module.utc_now()
        self.assertTrue(app_module.start_worker_heartbeat(
            app_module.WORKER_ROLE, instance_id, commit, now=now
        )["ok"])
        self.assertTrue(app_module.update_worker_heartbeat(
            app_module.WORKER_ROLE,
            instance_id,
            commit,
            poll_completed=True,
            iteration_succeeded=True,
            now=now,
        )["ok"])
        return now

    def request(self, now, probe_id=PROBE_A, commit=COMMIT):
        return app_module.request_worker_canary(
            app_module.WORKER_ROLE,
            commit,
            now=now,
            probe_id_factory=lambda: probe_id,
        )

    def row(self):
        rows = self.execute("""
            SELECT worker_role, probe_id, expected_commit, status,
                   requested_at, expires_at, claimed_at, completed_at,
                   claim_token, claim_expires_at, completed_commit,
                   result_code, updated_at
            FROM worker_canary_probes WHERE worker_role = ?
        """, (app_module.WORKER_ROLE,))
        return rows[0] if rows else None

    def test_fresh_repeatable_bounded_schema(self):
        columns = [
            row[1]
            for row in self.execute("PRAGMA table_info(worker_canary_probes)")
        ]
        self.assertEqual(columns, [
            "worker_role", "probe_id", "expected_commit", "status",
            "requested_at", "expires_at", "claimed_at", "completed_at",
            "claim_token", "claim_expires_at", "completed_commit",
            "result_code", "updated_at",
        ])
        app_module.init_db()
        self.assertEqual(
            self.execute("SELECT COUNT(*) FROM worker_canary_probes")[0][0],
            0,
        )

    def test_request_requires_live_matching_heartbeat(self):
        now = app_module.utc_now()
        missing = self.request(now)
        self.assertEqual(missing["code"], "worker_canary_worker_missing")

        self.live_heartbeat(commit=None, now=now)
        unknown = self.request(now)
        self.assertEqual(unknown["code"], "worker_canary_commit_unknown")

        self.live_heartbeat(commit=OTHER_COMMIT, now=now)
        mismatch = self.request(now)
        self.assertEqual(mismatch["code"], "worker_canary_commit_mismatch")

        self.live_heartbeat(now=now - timedelta(seconds=601))
        stale = self.request(now)
        self.assertEqual(stale["code"], "worker_canary_worker_stale")

    def test_request_is_atomic_bounded_and_reuses_active_probe(self):
        now = self.live_heartbeat()
        first = self.request(now)
        second = self.request(now + timedelta(seconds=1), PROBE_B)
        self.assertEqual(first["code"], "worker_canary_requested")
        self.assertEqual(second["code"], "worker_canary_already_active")
        self.assertEqual(second["probe_id"], PROBE_A)
        self.assertEqual(
            self.execute("SELECT COUNT(*) FROM worker_canary_probes")[0][0],
            1,
        )
        row = self.row()
        self.assertEqual(row[1], PROBE_A)
        self.assertEqual(row[3], "pending")
        self.assertIsNone(row[8])
        self.assertIsNone(row[10])

    def test_active_probe_from_different_commit_is_not_reused(self):
        now = self.live_heartbeat()
        self.request(now)
        self.live_heartbeat(INSTANCE_B, OTHER_COMMIT, now + timedelta(seconds=1))
        conflict = self.request(
            now + timedelta(seconds=1), PROBE_B, OTHER_COMMIT
        )
        self.assertEqual(
            conflict["code"], "worker_canary_active_commit_conflict"
        )
        self.assertNotIn("probe_id", conflict)
        row = self.row()
        self.assertEqual(row[1], PROBE_A)
        self.assertEqual(row[2], COMMIT)
        self.assertEqual(row[3], "pending")

    def test_concurrent_same_commit_requests_share_one_probe(self):
        now = self.live_heartbeat()
        barrier = threading.Barrier(2)

        def request(probe_id):
            barrier.wait()
            return self.request(now, probe_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(request, (PROBE_A, PROBE_B)))
        self.assertEqual(
            {result["code"] for result in results},
            {"worker_canary_requested", "worker_canary_already_active"},
        )
        self.assertEqual(len({result["probe_id"] for result in results}), 1)
        self.assertEqual(
            self.execute("SELECT COUNT(*) FROM worker_canary_probes")[0][0], 1
        )

    def test_cross_commit_request_conflict_is_serialized(self):
        now = self.live_heartbeat()
        first_complete = threading.Event()

        def first_request():
            result = self.request(now, PROBE_A, COMMIT)
            first_complete.set()
            return result

        def second_request():
            self.assertTrue(first_complete.wait(timeout=5))
            changed = now + timedelta(seconds=1)
            self.live_heartbeat(INSTANCE_B, OTHER_COMMIT, changed)
            return self.request(changed, PROBE_B, OTHER_COMMIT)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(first_request)
            second_future = executor.submit(second_request)
            first, second = first_future.result(), second_future.result()
        self.assertEqual(first["probe_id"], PROBE_A)
        self.assertEqual(
            second["code"], "worker_canary_active_commit_conflict"
        )
        self.assertNotIn("probe_id", second)
        self.assertEqual(self.row()[1:4], (PROBE_A, COMMIT, "pending"))

    def test_terminal_or_expired_row_is_replaced(self):
        now = self.live_heartbeat()
        self.request(now)
        self.execute(
            "UPDATE worker_canary_probes SET status = 'completed', "
            "result_code = ?, completed_commit = ? WHERE worker_role = ?",
            (app_module.WORKER_CANARY_VERIFIED_RESULT, COMMIT,
             app_module.WORKER_ROLE),
        )
        replaced = self.request(now + timedelta(seconds=1), PROBE_B)
        self.assertEqual(replaced["probe_id"], PROBE_B)
        self.assertEqual(self.row()[3], "pending")

        self.execute(
            "UPDATE worker_canary_probes SET status = 'pending', "
            "expires_at = ? WHERE worker_role = ?",
            (now - timedelta(seconds=1), app_module.WORKER_ROLE),
        )
        replaced_again = self.request(now, PROBE_A)
        self.assertEqual(replaced_again["probe_id"], PROBE_A)

    def test_probe_and_token_validation_is_exact(self):
        validators = (
            (app_module.normalize_worker_canary_probe_id, 64),
            (app_module.normalize_worker_canary_claim_token, 120),
            (app_module.normalize_worker_canary_result_code, 120),
        )
        for validator, maximum in validators:
            self.assertIsNotNone(validator("safe_value-1"))
            for value in (
                " safe", "safe ", "safe\n", "safe\0", "safe\t",
                "\u00a0safe", "a" * (maximum + 1), 7,
            ):
                with self.subTest(validator=validator.__name__, value=repr(value)):
                    self.assertIsNone(validator(value))

    def test_two_workers_cannot_claim_one_probe(self):
        now = self.live_heartbeat()
        self.request(now)
        barrier = threading.Barrier(2)

        def claim(token):
            barrier.wait()
            return app_module.claim_worker_canary(
                app_module.WORKER_ROLE,
                INSTANCE_A,
                COMMIT,
                now=now,
                claim_token_factory=lambda: token,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, (TOKEN_A, TOKEN_B)))
        claimed = [r for r in results if r["code"] == "worker_canary_claimed"]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(self.row()[3], "in_progress")
        self.assertIn(self.row()[8], {TOKEN_A, TOKEN_B})

    def test_claim_requires_exact_owner_and_commit(self):
        now = self.live_heartbeat()
        self.request(now)
        wrong_instance = app_module.claim_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_B, COMMIT, now=now
        )
        self.assertEqual(
            wrong_instance["code"], "worker_heartbeat_ownership_lost"
        )
        wrong_commit = app_module.claim_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, OTHER_COMMIT, now=now
        )
        self.assertEqual(
            wrong_commit["code"], "worker_canary_commit_mismatch"
        )
        self.assertEqual(self.row()[3], "pending")

    def test_claim_rejects_stale_heartbeat_without_mutating_probe(self):
        now = self.live_heartbeat()
        self.request(now)
        stale = now - timedelta(seconds=601)
        self.execute(
            "UPDATE worker_heartbeats SET last_poll_at = ?, last_success_at = ? "
            "WHERE worker_role = ?",
            (stale, stale, app_module.WORKER_ROLE),
        )
        with mock.patch.object(
            app_module, "perform_worker_canary_synthetic_step"
        ) as synthetic, mock.patch.object(
            app_module, "finalize_worker_canary"
        ) as finalize:
            result = app_module.run_worker_canary_once(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT
            )
        self.assertEqual(result["code"], "worker_canary_worker_stale")
        synthetic.assert_not_called()
        finalize.assert_not_called()
        row = self.row()
        self.assertEqual(row[3], "pending")
        self.assertIsNone(row[8])
        self.assertIsNone(row[6])
        self.assertIsNone(row[9])

    def test_claim_rejects_each_stale_or_malformed_heartbeat_timestamp(self):
        cases = (
            ("last_poll_at", app_module.utc_now() - timedelta(seconds=601)),
            ("last_success_at", app_module.utc_now() - timedelta(seconds=601)),
            ("last_poll_at", "malformed-poll"),
            ("last_success_at", "malformed-success"),
        )
        for column, value in cases:
            with self.subTest(column=column, value=str(value)):
                now = self.live_heartbeat()
                self.execute(
                    f"UPDATE worker_heartbeats SET {column} = ? "
                    "WHERE worker_role = ?",
                    (value, app_module.WORKER_ROLE),
                )
                self.execute("DELETE FROM worker_canary_probes")
                requested = self.request(now)
                self.assertEqual(
                    requested["code"], "worker_canary_worker_stale"
                )
                self.execute("""
                    INSERT INTO worker_canary_probes (
                        worker_role, probe_id, expected_commit, status,
                        requested_at, expires_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """, (
                    app_module.WORKER_ROLE, PROBE_A, COMMIT, now,
                    now + timedelta(seconds=600), now,
                ))
                claimed = app_module.claim_worker_canary(
                    app_module.WORKER_ROLE, INSTANCE_A, COMMIT, now=now
                )
                self.assertEqual(claimed["code"], "worker_canary_worker_stale")
                self.assertEqual(self.row()[3], "pending")
                self.assertIsNone(self.row()[8])

    def test_claim_time_unknown_stored_commit_is_safe_and_nonmutating(self):
        for stored_commit in (None, "synthetic-malformed-worker-commit"):
            with self.subTest(stored_commit=stored_commit):
                now = self.live_heartbeat()
                self.execute("DELETE FROM worker_canary_probes")
                requested = self.request(now)
                self.assertEqual(requested["code"], "worker_canary_requested")
                self.execute(
                    "UPDATE worker_heartbeats SET deployed_commit = ? "
                    "WHERE worker_role = ?",
                    (stored_commit, app_module.WORKER_ROLE),
                )
                voice_before = self.execute(
                    "SELECT * FROM agent_voice_sessions ORDER BY id"
                )
                tracked_connections = []

                class TrackedConnection:
                    def __init__(self, connection):
                        self.connection = connection
                        self.rolled_back = False
                        self.closed = False

                    def cursor(self):
                        return self.connection.cursor()

                    def commit(self):
                        return self.connection.commit()

                    def rollback(self):
                        self.rolled_back = True
                        return self.connection.rollback()

                    def close(self):
                        self.closed = True
                        return self.connection.close()

                def tracked_connect():
                    connection = TrackedConnection(self.connect())
                    tracked_connections.append(connection)
                    return connection

                with mock.patch.object(
                    app_module, "db", side_effect=tracked_connect
                ), mock.patch.object(
                    app_module, "perform_worker_canary_synthetic_step"
                ) as synthetic, mock.patch.object(
                    app_module, "finalize_worker_canary"
                ) as finalize, mock.patch.object(
                    app_module, "hangup_realtime_call"
                ) as hangup, mock.patch.object(
                    app_module, "create_realtime_sdp_answer"
                ) as realtime, mock.patch.object(
                    app_module.requests, "post"
                ) as post, mock.patch.object(
                    app_module.requests, "request"
                ) as request, mock.patch.object(
                    app_module, "OpenAI"
                ) as openai, mock.patch(
                    "socket.getaddrinfo"
                ) as dns, mock.patch(
                    "socket.create_connection"
                ) as socket_connect, mock.patch(
                    "urllib.request.urlopen"
                ) as urlopen:
                    result = app_module.run_worker_canary_once(
                        app_module.WORKER_ROLE, INSTANCE_A, COMMIT
                    )
                self.assertEqual(result, {
                    "processed": False,
                    "completed": False,
                    "code": "worker_canary_commit_unknown",
                })
                synthetic.assert_not_called()
                finalize.assert_not_called()
                for external in (
                    hangup, realtime, post, request, openai, dns,
                    socket_connect, urlopen,
                ):
                    external.assert_not_called()
                self.assertEqual(len(tracked_connections), 1)
                self.assertTrue(tracked_connections[0].rolled_back)
                self.assertTrue(tracked_connections[0].closed)
                row = self.row()
                self.assertEqual(row[1], PROBE_A)
                self.assertEqual(row[2], COMMIT)
                self.assertEqual(row[3], "pending")
                self.assertIsNone(row[6])
                self.assertIsNone(row[8])
                self.assertIsNone(row[9])
                self.assertEqual(
                    self.execute("SELECT * FROM agent_voice_sessions ORDER BY id"),
                    voice_before,
                )

    def test_claim_commit_mismatch_does_not_process(self):
        now = self.live_heartbeat()
        self.request(now)
        self.execute(
            "UPDATE worker_canary_probes SET expected_commit = ? "
            "WHERE worker_role = ?",
            (OTHER_COMMIT, app_module.WORKER_ROLE),
        )
        result = app_module.claim_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT, now=now
        )
        self.assertEqual(result["code"], "worker_canary_commit_mismatch")
        self.assertEqual(self.row()[3], "pending")

    def test_finalize_is_guarded_and_uses_separate_connection(self):
        now = self.live_heartbeat()
        self.request(now)
        self.db_mock.reset_mock()
        result = app_module.run_worker_canary_once(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT
        )
        self.assertTrue(result["processed"])
        self.assertTrue(result["completed"])
        self.assertEqual(result["code"], app_module.WORKER_CANARY_VERIFIED_RESULT)
        for prohibited in ("probe_id", "claim_token", "instance_id"):
            self.assertNotIn(prohibited, result)
        self.assertEqual(self.db_mock.call_count, 2)
        row = self.row()
        self.assertEqual(row[3], "completed")
        self.assertIsNone(row[8])
        self.assertIsNone(row[9])
        self.assertEqual(row[10], COMMIT)

    def test_synthetic_failure_leaves_claim_recoverable(self):
        now = self.live_heartbeat()
        self.request(now)
        with mock.patch.object(
            app_module, "perform_worker_canary_synthetic_step",
            side_effect=RuntimeError("synthetic secret failure"),
        ), mock.patch.object(
            app_module, "finalize_worker_canary"
        ) as finalize, mock.patch.object(
            app_module, "hangup_realtime_call"
        ) as hangup, mock.patch.object(
            app_module.requests, "post"
        ) as post, mock.patch.object(
            app_module.requests, "request"
        ) as request, mock.patch(
            "socket.getaddrinfo"
        ) as dns, self.assertLogs(
            app_module.logger, level="WARNING"
        ) as logs:
            result = app_module.run_worker_canary_once(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT
            )
        self.assertEqual(result, {
            "processed": True,
            "completed": False,
            "code": "worker_canary_synthetic_step_failed",
        })
        finalize.assert_not_called()
        for external in (hangup, post, request, dns):
            external.assert_not_called()
        self.assertEqual(self.row()[3], "in_progress")
        self.assertIsNotNone(self.row()[8])
        self.assertNotIn("synthetic secret failure", "\n".join(logs.output))

    def test_sqlite_claim_close_synthetic_finalize_event_order(self):
        now = self.live_heartbeat()
        self.request(now)
        events = []
        connections = []

        class CursorProxy:
            def __init__(self, cursor, label):
                self.cursor = cursor
                self.label = label

            @property
            def rowcount(self):
                return self.cursor.rowcount

            def fetchone(self):
                return self.cursor.fetchone()

            def execute(self, statement, parameters=()):
                normalized = " ".join(str(statement).split()).lower()
                if normalized == "begin immediate":
                    events.append(f"{self.label}_transaction_begin")
                elif "set status = 'in_progress'" in normalized:
                    events.append("claim_update")
                elif "set status = 'completed'" in normalized:
                    events.append("finalize_update")
                self.cursor.execute(statement, parameters)
                return self

        class ConnectionProxy:
            def __init__(self, connection, label):
                self.connection = connection
                self.label = label
                self.closed = False

            def cursor(self):
                return CursorProxy(self.connection.cursor(), self.label)

            def commit(self):
                self.connection.commit()
                events.append(f"{self.label}_commit")

            def rollback(self):
                self.connection.rollback()

            def close(self):
                self.connection.close()
                self.closed = True
                events.append(f"{self.label}_connection_close")

        def open_connection():
            label = "claim" if not connections else "finalize"
            events.append(f"{label}_connection_open")
            connection = ConnectionProxy(self.connect(), label)
            connections.append(connection)
            return connection

        def synthetic_step():
            self.assertTrue(connections[0].closed)
            self.assertEqual(len(connections), 1)
            events.append("synthetic_step")
            return {
                "ok": True,
                "code": "worker_canary_synthetic_step_completed",
            }

        with mock.patch.object(
            app_module, "db", side_effect=open_connection
        ), mock.patch.object(
            app_module, "perform_worker_canary_synthetic_step",
            side_effect=synthetic_step,
        ):
            result = app_module.run_worker_canary_once(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT
            )
        self.assertTrue(result["completed"])
        self.assertEqual(events, [
            "claim_connection_open", "claim_transaction_begin",
            "claim_update", "claim_commit", "claim_connection_close",
            "synthetic_step", "finalize_connection_open",
            "finalize_transaction_begin", "finalize_update",
            "finalize_commit", "finalize_connection_close",
        ])

    def test_only_one_concurrent_finalization_succeeds(self):
        now = self.live_heartbeat()
        self.request(now)
        claim = app_module.claim_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT, now=now,
            claim_token_factory=lambda: TOKEN_A,
        )
        barrier = threading.Barrier(2)

        def finalize(_):
            barrier.wait()
            return app_module.finalize_worker_canary(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT,
                claim["probe_id"], TOKEN_A, now=now,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(finalize, range(2)))
        self.assertEqual(sum(bool(result.get("ok")) for result in results), 1)
        self.assertEqual(self.row()[3], "completed")

    def test_wrong_or_stale_token_cannot_finalize(self):
        now = self.live_heartbeat()
        self.request(now)
        claim = app_module.claim_worker_canary(
            app_module.WORKER_ROLE,
            INSTANCE_A,
            COMMIT,
            now=now,
            claim_token_factory=lambda: TOKEN_A,
        )
        wrong = app_module.finalize_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT,
            claim["probe_id"], TOKEN_B, now=now,
        )
        self.assertEqual(wrong["code"], "worker_canary_claim_lost")
        stale = app_module.finalize_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT,
            claim["probe_id"], TOKEN_A, now=now + timedelta(seconds=31),
        )
        self.assertEqual(stale["code"], "worker_canary_claim_lost")
        self.assertEqual(self.row()[3], "in_progress")

    def test_wrong_commit_and_overall_expiry_cannot_finalize(self):
        now = self.live_heartbeat()
        self.request(now)
        claim = app_module.claim_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT, now=now,
            claim_token_factory=lambda: TOKEN_A,
        )
        self.live_heartbeat(INSTANCE_A, OTHER_COMMIT, now + timedelta(seconds=1))
        wrong_commit = app_module.finalize_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, OTHER_COMMIT,
            claim["probe_id"], TOKEN_A, now=now + timedelta(seconds=1),
        )
        self.assertEqual(wrong_commit["code"], "worker_canary_claim_lost")
        self.assertEqual(self.row()[3], "in_progress")

        self.live_heartbeat(INSTANCE_A, COMMIT, now + timedelta(seconds=601))
        expired = app_module.finalize_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT,
            claim["probe_id"], TOKEN_A, now=now + timedelta(seconds=601),
        )
        self.assertEqual(expired["code"], "worker_canary_claim_lost")
        read = app_module.get_worker_canary_result(
            app_module.WORKER_ROLE, claim["probe_id"], COMMIT,
            now=now + timedelta(seconds=601),
        )
        self.assertEqual(read["status"], "expired")
        self.assertFalse(read["verified"])

    def test_expired_claim_lease_rejects_completion_and_recovers_pending(self):
        now = self.live_heartbeat()
        self.request(now)
        claim = app_module.claim_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT, now=now,
            claim_token_factory=lambda: TOKEN_A,
        )
        rejected = app_module.finalize_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT,
            claim["probe_id"], TOKEN_A, now=now + timedelta(seconds=31),
        )
        self.assertEqual(rejected["code"], "worker_canary_claim_lost")
        read = app_module.get_worker_canary_result(
            app_module.WORKER_ROLE, PROBE_A, COMMIT,
            now=now + timedelta(seconds=31),
        )
        self.assertEqual(read["status"], "pending")
        self.assertIsNone(self.row()[8])

    def test_replaced_owner_cannot_finalize_and_new_owner_reclaims(self):
        now = self.live_heartbeat()
        self.request(now)
        old_claim = app_module.claim_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT, now=now,
            claim_token_factory=lambda: TOKEN_A,
        )
        self.live_heartbeat(INSTANCE_B, COMMIT, now + timedelta(seconds=1))
        fenced = app_module.finalize_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT,
            old_claim["probe_id"], TOKEN_A, now=now + timedelta(seconds=2),
        )
        self.assertEqual(fenced["code"], "worker_heartbeat_ownership_lost")
        reclaimed = app_module.claim_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_B, COMMIT,
            now=now + timedelta(seconds=31),
            claim_token_factory=lambda: TOKEN_B,
        )
        self.assertEqual(reclaimed["code"], "worker_canary_claimed")
        old_finalize = app_module.finalize_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_B, COMMIT,
            reclaimed["probe_id"], TOKEN_A, now=now + timedelta(seconds=31),
        )
        self.assertEqual(old_finalize["code"], "worker_canary_claim_lost")
        completed = app_module.finalize_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_B, COMMIT,
            reclaimed["probe_id"], TOKEN_B, now=now + timedelta(seconds=31),
        )
        self.assertTrue(completed["ok"])

    def test_expiry_and_stale_lease_reconciliation_is_idempotent(self):
        now = self.live_heartbeat()
        self.request(now)
        self.execute(
            "UPDATE worker_canary_probes SET status = 'in_progress', "
            "claim_token = ?, claim_expires_at = ? WHERE worker_role = ?",
            (TOKEN_A, now - timedelta(seconds=1), app_module.WORKER_ROLE),
        )
        first = app_module.get_worker_canary_result(
            app_module.WORKER_ROLE, PROBE_A, COMMIT, now=now
        )
        second = app_module.get_worker_canary_result(
            app_module.WORKER_ROLE, PROBE_A, COMMIT, now=now
        )
        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "pending")
        self.assertIsNone(self.row()[8])

        expired_at = now + timedelta(seconds=601)
        expired = app_module.get_worker_canary_result(
            app_module.WORKER_ROLE, PROBE_A, COMMIT, now=expired_at
        )
        self.assertEqual(expired["status"], "expired")
        self.live_heartbeat(INSTANCE_A, COMMIT, expired_at)
        no_claim = app_module.claim_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT, now=expired_at
        )
        self.assertEqual(no_claim["code"], "worker_canary_no_work")

    def test_completed_and_failed_remain_terminal_during_claim(self):
        now = self.live_heartbeat()
        self.request(now)
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                self.execute(
                    "UPDATE worker_canary_probes SET status = ? "
                    "WHERE worker_role = ?",
                    (status, app_module.WORKER_ROLE),
                )
                result = app_module.claim_worker_canary(
                    app_module.WORKER_ROLE, INSTANCE_A, COMMIT, now=now
                )
                self.assertEqual(result["code"], "worker_canary_no_work")
                self.assertEqual(self.row()[3], status)

    def test_read_result_is_bounded_and_verifies_exact_probe(self):
        now = self.live_heartbeat()
        self.request(now)
        app_module.run_worker_canary_once(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT
        )
        result = app_module.get_worker_canary_result(
            app_module.WORKER_ROLE, PROBE_A, COMMIT, now=now
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["age_seconds"], 0)
        for prohibited in ("probe_id", "claim_token", "instance_id"):
            self.assertNotIn(prohibited, result)
        missing = app_module.get_worker_canary_result(
            app_module.WORKER_ROLE, PROBE_B, COMMIT, now=now
        )
        self.assertFalse(missing["found"])

    def test_malformed_row_fails_closed(self):
        now = self.live_heartbeat()
        self.request(now)
        self.execute(
            "UPDATE worker_canary_probes SET requested_at = 'malformed' "
            "WHERE worker_role = ?",
            (app_module.WORKER_ROLE,),
        )
        result = app_module.get_worker_canary_result(
            app_module.WORKER_ROLE, PROBE_A, COMMIT, now=now
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["verified"])
        self.assertIsNone(result["age_seconds"])

    def test_malformed_status_expected_commit_and_expiry_fail_closed(self):
        cases = (
            ("status", "not-a-status"),
            ("expected_commit", "malformed-commit"),
            ("expires_at", "malformed-expiry"),
        )
        for column, value in cases:
            with self.subTest(column=column):
                now = self.live_heartbeat()
                self.execute("DELETE FROM worker_canary_probes")
                self.request(now)
                self.execute(
                    f"UPDATE worker_canary_probes SET {column} = ? "
                    "WHERE worker_role = ?",
                    (value, app_module.WORKER_ROLE),
                )
                result = app_module.get_worker_canary_result(
                    app_module.WORKER_ROLE, PROBE_A, COMMIT, now=now
                )
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["verified"])
                self.assertNotIn(str(value), repr(result))

    def test_malformed_completion_and_claim_fields_never_verify(self):
        cases = (
            ("completed_commit", "malformed-completed"),
            ("result_code", "malformed result"),
        )
        for column, value in cases:
            with self.subTest(column=column):
                now = self.live_heartbeat()
                self.execute("DELETE FROM worker_canary_probes")
                self.request(now)
                self.execute(
                    "UPDATE worker_canary_probes SET status = 'completed', "
                    "completed_commit = ?, result_code = ? WHERE worker_role = ?",
                    (
                        value if column == "completed_commit" else COMMIT,
                        value if column == "result_code" else
                        app_module.WORKER_CANARY_VERIFIED_RESULT,
                        app_module.WORKER_ROLE,
                    ),
                )
                result = app_module.get_worker_canary_result(
                    app_module.WORKER_ROLE, PROBE_A, COMMIT, now=now
                )
                self.assertFalse(result["verified"])
                self.assertNotIn(str(value), repr(result))

        now = self.live_heartbeat()
        self.execute("DELETE FROM worker_canary_probes")
        self.request(now)
        self.execute(
            "UPDATE worker_canary_probes SET status = 'in_progress', "
            "claim_token = ?, claim_expires_at = 'malformed-lease' "
            "WHERE worker_role = ?",
            (TOKEN_A, app_module.WORKER_ROLE),
        )
        result = app_module.get_worker_canary_result(
            app_module.WORKER_ROLE, PROBE_A, COMMIT, now=now
        )
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["verified"])
        self.assertIsNone(self.row()[8])

    def test_complete_voice_row_is_unchanged_across_canary_paths(self):
        now = self.live_heartbeat()
        next_attempt = now + timedelta(seconds=10)
        lease = now + timedelta(seconds=20)
        connection = self.connect()
        try:
            cursor = connection.execute("""
                INSERT INTO agent_voice_sessions (
                    user_id, status, handshake_request_id, upstream_call_id,
                    termination_status, termination_attempts,
                    termination_next_attempt_at, termination_claim_token,
                    termination_lease_expires_at, started_at, expires_at,
                    created_at, updated_at
                ) VALUES (?, 'ended', ?, ?, 'in_progress', 2, ?, ?, ?, ?, ?, ?, ?)
            """, (
                77, "synthetic-canary-isolation-request",
                "synthetic-canary-isolation-call", next_attempt,
                "synthetic-voice-termination-token", lease, now,
                now + timedelta(minutes=10), now, now,
            ))
            voice_id = cursor.lastrowid
            connection.commit()
        finally:
            connection.close()

        def voice_row():
            return self.execute(
                "SELECT * FROM agent_voice_sessions WHERE id = ?", (voice_id,)
            )[0]

        before = voice_row()
        self.request(now)
        self.assertTrue(app_module.run_worker_canary_once(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT
        )["completed"])
        self.assertEqual(voice_row(), before)

        self.request(now + timedelta(seconds=1), PROBE_B)
        with mock.patch.object(
            app_module, "perform_worker_canary_synthetic_step",
            return_value={"ok": False, "code": "synthetic-fixed-failure"},
        ):
            failed = app_module.run_worker_canary_once(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT
            )
        self.assertEqual(failed["code"], "worker_canary_synthetic_step_failed")
        self.assertEqual(voice_row(), before)

        self.execute(
            "UPDATE worker_canary_probes SET status = 'failed', "
            "claim_token = NULL, claim_expires_at = NULL WHERE worker_role = ?",
            (app_module.WORKER_ROLE,),
        )
        self.request(now + timedelta(seconds=2), PROBE_A)
        self.live_heartbeat(INSTANCE_B, COMMIT, now + timedelta(seconds=3))
        fenced = app_module.run_worker_canary_once(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT
        )
        self.assertEqual(fenced["code"], "worker_heartbeat_ownership_lost")
        self.assertEqual(voice_row(), before)

    def test_finalization_rejection_preserves_voice_row_and_has_no_network(self):
        now = self.live_heartbeat()
        connection = self.connect()
        try:
            cursor = connection.execute("""
                INSERT INTO agent_voice_sessions (
                    user_id, status, handshake_request_id, upstream_call_id,
                    termination_status, termination_attempts,
                    termination_requested_at, termination_last_attempt_at,
                    termination_next_attempt_at, termination_accepted_at,
                    termination_error_code, termination_claim_token,
                    termination_lease_expires_at, disconnect_reason,
                    duration_seconds, started_at, expires_at, ended_at,
                    created_at, updated_at
                ) VALUES (
                    ?, 'ended', ?, ?, 'in_progress', 2,
                    ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                88, "synthetic-finalize-rejection-request",
                "synthetic-finalize-rejection-call",
                now - timedelta(seconds=5), now - timedelta(seconds=2),
                now + timedelta(seconds=10), "synthetic-retryable-error",
                "synthetic-voice-claim-token", now + timedelta(seconds=20),
                "synthetic-client-ended", 30, now - timedelta(seconds=30),
                now + timedelta(minutes=10), now, now - timedelta(seconds=30),
                now,
            ))
            voice_id = cursor.lastrowid
            connection.commit()
        finally:
            connection.close()

        voice_before = self.execute(
            "SELECT * FROM agent_voice_sessions WHERE id = ?", (voice_id,)
        )[0]
        count_before = self.execute(
            "SELECT COUNT(*) FROM agent_voice_sessions"
        )[0][0]
        self.request(now)
        claim = app_module.claim_worker_canary(
            app_module.WORKER_ROLE, INSTANCE_A, COMMIT, now=now,
            claim_token_factory=lambda: TOKEN_A,
        )
        self.assertEqual(claim["code"], "worker_canary_claimed")
        self.live_heartbeat(INSTANCE_B, COMMIT, now + timedelta(seconds=1))

        with mock.patch.dict(
            os.environ, {"VOICE_RUNTIME_ENABLED": "false"}, clear=True
        ), mock.patch.object(
            app_module, "hangup_realtime_call"
        ) as hangup, mock.patch.object(
            app_module, "create_realtime_session", create=True
        ) as create_session, mock.patch.object(
            app_module, "create_realtime_sdp_answer"
        ) as realtime, mock.patch.object(
            app_module.requests, "post"
        ) as post, mock.patch.object(
            app_module.requests, "request"
        ) as request, mock.patch.object(
            app_module, "OpenAI"
        ) as openai, mock.patch(
            "socket.getaddrinfo"
        ) as dns, mock.patch(
            "socket.create_connection"
        ) as socket_connect, mock.patch(
            "urllib.request.urlopen"
        ) as urlopen:
            rejected = app_module.finalize_worker_canary(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT,
                claim["probe_id"], TOKEN_A,
                now=now + timedelta(seconds=1),
            )
        self.assertEqual(
            rejected, {
                "ok": False,
                "code": "worker_heartbeat_ownership_lost",
            }
        )
        for external in (
            hangup, create_session, realtime, post, request, openai, dns,
            socket_connect, urlopen,
        ):
            external.assert_not_called()
        self.assertEqual(
            self.execute(
                "SELECT * FROM agent_voice_sessions WHERE id = ?", (voice_id,)
            )[0],
            voice_before,
        )
        self.assertEqual(
            self.execute("SELECT COUNT(*) FROM agent_voice_sessions")[0][0],
            count_before,
        )
        canary = self.row()
        self.assertEqual(canary[3], "in_progress")
        self.assertEqual(canary[8], TOKEN_A)
        self.assertIsNone(canary[7])
        self.assertIsNone(canary[10])

    def test_no_openai_or_voice_session_path_is_reachable(self):
        now = self.live_heartbeat()
        self.request(now)
        before = self.execute("SELECT COUNT(*) FROM agent_voice_sessions")[0][0]
        with mock.patch.dict(
            os.environ, {"VOICE_RUNTIME_ENABLED": "false"}, clear=True
        ), mock.patch.object(
            app_module, "hangup_realtime_call"
        ) as hangup, mock.patch.object(
            app_module, "create_realtime_session", create=True
        ) as create_session, mock.patch.object(
            app_module, "create_realtime_sdp_answer"
        ) as create_sdp, mock.patch.object(
            app_module.requests, "post"
        ) as post, mock.patch.object(
            app_module.requests, "request"
        ) as request, mock.patch("socket.getaddrinfo") as dns:
            result = app_module.run_worker_canary_once(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT
            )
        self.assertTrue(result["completed"])
        for network in (hangup, create_session, create_sdp, post, request, dns):
            network.assert_not_called()
        self.assertEqual(
            self.execute("SELECT COUNT(*) FROM agent_voice_sessions")[0][0],
            before,
        )
        self.assertNotIn("upstream_call_id", {
            row[1] for row in self.execute(
                "PRAGMA table_info(worker_canary_probes)"
            )
        })

    def test_importing_worker_has_no_execution_or_network_side_effect(self):
        targets = (
            "init_db", "start_worker_heartbeat", "run_worker_canary_once",
            "run_voice_maintenance_once", "run_background_job_once",
        )
        patches = [mock.patch.object(app_module, target) for target in targets]
        started = [patch.start() for patch in patches]
        try:
            with mock.patch.object(
                app_module.requests, "post"
            ) as post, mock.patch.object(
                app_module.requests, "request"
            ) as request, mock.patch(
                "socket.getaddrinfo"
            ) as dns, mock.patch(
                "threading.Thread.start"
            ) as thread_start:
                namespace = runpy.run_module(
                    "worker", run_name="worker_import_safety"
                )
            self.assertIn("run_worker", namespace)
            for called in started + [post, request, dns, thread_start]:
                called.assert_not_called()
        finally:
            for patch in reversed(patches):
                patch.stop()


class WorkerCanaryIntegrationTestCase(unittest.TestCase):
    def test_worker_order_is_voice_canary_generic(self):
        events = []
        with mock.patch.object(
            worker_module, "run_voice_maintenance_once",
            side_effect=lambda: events.append("voice") or {"processed": 0},
        ), mock.patch.object(
            worker_module, "run_worker_canary_once",
            side_effect=lambda *args: events.append("canary") or {
                "processed": True, "completed": True,
                "code": app_module.WORKER_CANARY_VERIFIED_RESULT,
            },
        ), mock.patch.object(
            worker_module, "run_background_job_once",
            side_effect=lambda worker_id: events.append("generic") or True,
        ):
            details = worker_module.run_worker_iteration_with_canary(
                "synthetic-worker", app_module.WORKER_ROLE,
                INSTANCE_A, COMMIT,
            )
        self.assertEqual(events, ["voice", "canary", "generic"])
        self.assertTrue(details["canary_completed"])
        self.assertTrue(details["generic_job_completed"])

    def test_canary_failure_does_not_block_generic(self):
        with mock.patch.object(
            worker_module, "run_voice_maintenance_once",
            return_value={"processed": 0},
        ), mock.patch.object(
            worker_module, "run_worker_canary_once",
            return_value={
                "processed": False, "completed": False,
                "code": "worker_canary_unavailable",
            },
        ), mock.patch.object(
            worker_module, "run_background_job_once", return_value=True
        ) as generic, self.assertLogs(
            worker_module.logger, level="WARNING"
        ) as logs:
            details = worker_module.run_worker_iteration_with_canary(
                "synthetic-worker", app_module.WORKER_ROLE,
                INSTANCE_A, COMMIT,
            )
        self.assertTrue(details["processed"])
        generic.assert_called_once()
        self.assertIn("worker_canary_unavailable", "\n".join(logs.output))

    def test_synthetic_failure_does_not_block_generic(self):
        with mock.patch.object(
            worker_module, "run_voice_maintenance_once",
            return_value={"processed": 0},
        ), mock.patch.object(
            worker_module, "run_worker_canary_once",
            return_value={
                "processed": True, "completed": False,
                "code": "worker_canary_synthetic_step_failed",
            },
        ), mock.patch.object(
            worker_module, "run_background_job_once", return_value=True
        ) as generic:
            details = worker_module.run_worker_iteration_with_canary(
                "synthetic-worker", app_module.WORKER_ROLE,
                INSTANCE_A, COMMIT,
            )
        self.assertTrue(details["processed"])
        generic.assert_called_once()

    def test_canary_ownership_loss_stops_before_generic(self):
        with mock.patch.object(
            worker_module, "run_voice_maintenance_once",
            return_value={"processed": 0},
        ), mock.patch.object(
            worker_module, "run_worker_canary_once",
            return_value={
                "processed": False, "completed": False,
                "code": "worker_heartbeat_ownership_lost",
            },
        ), mock.patch.object(
            worker_module, "run_background_job_once"
        ) as generic:
            details = worker_module.run_worker_iteration_with_canary(
                "synthetic-worker", app_module.WORKER_ROLE,
                INSTANCE_A, COMMIT,
            )
        self.assertTrue(details["ownership_lost"])
        generic.assert_not_called()

    def test_worker_loop_fences_canary_ownership_loss_without_sleep(self):
        stop = mock.Mock()
        stop.is_set.return_value = False
        details = {
            "processed": False,
            "voice_maintenance_completed": True,
            "canary_completed": False,
            "generic_job_completed": False,
            "ownership_lost": True,
        }
        with mock.patch.object(worker_module, "init_db"), mock.patch.object(
            worker_module, "resolve_worker_deployed_commit", return_value=COMMIT
        ), mock.patch.object(
            worker_module, "start_worker_heartbeat", return_value={"ok": True}
        ), mock.patch.object(
            worker_module, "get_monitoring_config",
            return_value={"worker_poll_seconds": 7},
        ), mock.patch.object(
            worker_module, "run_worker_iteration_details", return_value=details
        ) as iteration, mock.patch.object(
            worker_module, "update_worker_heartbeat"
        ) as heartbeat, self.assertLogs(
            worker_module.logger, level="WARNING"
        ) as logs:
            result = worker_module.run_worker(
                stop_requested=stop,
                max_iterations=3,
                instance_id_factory=lambda: INSTANCE_A,
            )
        self.assertEqual(result, worker_module.WORKER_OWNERSHIP_LOST)
        iteration.assert_called_once()
        heartbeat.assert_not_called()
        stop.wait.assert_not_called()
        self.assertEqual(
            "\n".join(logs.output).count("worker_heartbeat_ownership_lost"),
            1,
        )

    def test_canary_aware_fully_idle_iteration_keeps_poll_sleep(self):
        stop = mock.Mock()
        stop.is_set.return_value = False
        with mock.patch.object(worker_module, "init_db"), mock.patch.object(
            worker_module, "resolve_worker_deployed_commit", return_value=COMMIT
        ), mock.patch.object(
            worker_module, "start_worker_heartbeat", return_value={"ok": True}
        ), mock.patch.object(
            worker_module, "get_monitoring_config",
            return_value={"worker_poll_seconds": 7},
        ), mock.patch.object(
            worker_module, "run_voice_maintenance_once",
            return_value={"processed": 0},
        ), mock.patch.object(
            worker_module, "run_worker_canary_once",
            return_value={
                "processed": False, "completed": False,
                "code": "worker_canary_no_work",
            },
        ), mock.patch.object(
            worker_module, "run_background_job_once", return_value=False
        ), mock.patch.object(
            worker_module, "update_worker_heartbeat", return_value={"ok": True}
        ):
            worker_module.run_worker(
                stop_requested=stop,
                max_iterations=1,
                monotonic_fn=lambda: 0,
                instance_id_factory=lambda: INSTANCE_A,
            )
        stop.wait.assert_called_once_with(7)


class CanaryCliTestCase(unittest.TestCase):
    def invoke(self, request_result, read_result=None, wait=0):
        runner = app_module.app.test_cli_runner()
        patches = [mock.patch.object(
            app_module, "request_worker_canary", return_value=request_result
        )]
        if read_result is not None:
            patches.append(mock.patch.object(
                app_module, "get_worker_canary_result",
                return_value=read_result,
            ))
        with patches[0]:
            if len(patches) == 2:
                with patches[1]:
                    return runner.invoke(args=[
                        "verify-worker-canary", "--expected-commit", COMMIT,
                        "--wait-seconds", str(wait),
                    ])
            return runner.invoke(args=[
                "verify-worker-canary", "--expected-commit", COMMIT,
                "--wait-seconds", str(wait),
            ])

    def requested(self):
        return {
            "ok": True, "code": "worker_canary_requested",
            "probe_id": PROBE_A,
        }

    def test_all_cli_exit_codes(self):
        failures = (
            ("worker_canary_worker_missing", 2, "worker_not_live"),
            ("worker_canary_worker_stale", 2, "worker_not_live"),
            ("worker_canary_commit_mismatch", 4, "worker_not_live"),
            ("worker_canary_commit_unknown", 5, "worker_not_live"),
            ("worker_canary_unavailable", 6, "unavailable"),
        )
        for code, exit_code, status in failures:
            with self.subTest(code=code):
                result = self.invoke({"ok": False, "code": code})
                self.assertEqual(result.exit_code, exit_code)
                self.assertIn(f"status={status}", result.output)

        reads = (
            ({"available": True, "status": "pending", "age_seconds": 1,
              "verified": False}, 3, "pending"),
            ({"available": True, "status": "expired", "age_seconds": 601,
              "verified": False}, 7, "expired"),
            ({"available": True, "status": "failed", "age_seconds": 2,
              "verified": False}, 8, "failed"),
            ({"available": False, "status": "failed", "age_seconds": None,
              "verified": False}, 6, "unavailable"),
            ({"available": True, "status": "completed", "age_seconds": 2,
              "verified": True}, 0, "completed"),
        )
        for read, exit_code, status in reads:
            with self.subTest(status=status):
                result = self.invoke(self.requested(), read)
                self.assertEqual(result.exit_code, exit_code)
                self.assertIn(f"status={status}", result.output)

    def test_cli_output_is_safe_and_bounded(self):
        read = {
            "available": True, "status": "completed", "age_seconds": 3,
            "verified": True, "probe_id": "synthetic-secret-probe",
            "claim_token": "synthetic-secret-token",
            "instance_id": "synthetic-secret-instance",
        }
        result = self.invoke(self.requested(), read)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(result.output.strip().splitlines()), 5)
        for prohibited in (
            "synthetic-secret-probe", "synthetic-secret-token",
            "synthetic-secret-instance", "DATABASE_URL", "OPENAI_API_KEY",
            "Traceback",
        ):
            self.assertNotIn(prohibited, result.output)

    def test_cli_cross_commit_conflict_never_reads_or_exposes_probe(self):
        runner = app_module.app.test_cli_runner()
        conflict = {
            "ok": False,
            "code": "worker_canary_active_commit_conflict",
        }
        with mock.patch.object(
            app_module, "request_worker_canary", return_value=conflict
        ), mock.patch.object(
            app_module, "get_worker_canary_result"
        ) as read, mock.patch.object(
            app_module.requests, "post"
        ) as post, mock.patch.object(
            app_module.requests, "request"
        ) as request, mock.patch("socket.getaddrinfo") as dns:
            result = runner.invoke(args=[
                "verify-worker-canary", "--expected-commit", OTHER_COMMIT,
                "--wait-seconds", "0",
            ])
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.output.strip().splitlines(), [
            "status=pending",
            f"role={app_module.WORKER_ROLE}",
            "worker_commit=match",
            "canary_age_seconds=unknown",
            "result=not_verified",
        ])
        read.assert_not_called()
        post.assert_not_called()
        request.assert_not_called()
        dns.assert_not_called()
        self.assertNotIn(PROBE_A, result.output)
        self.assertNotIn(OTHER_COMMIT, result.output)
        self.assertEqual(getattr(result, "stderr", ""), "")

    def test_invalid_arguments_are_safe_and_do_not_request(self):
        runner = app_module.app.test_cli_runner()
        with mock.patch.object(app_module, "request_worker_canary") as request:
            result = runner.invoke(args=[
                "verify-worker-canary", "--expected-commit", " padded",
                "--wait-seconds", "301",
            ])
        self.assertEqual(result.exit_code, 2)
        request.assert_not_called()
        self.assertNotIn("Traceback", result.output)
        self.assertNotIn("DATABASE_URL", result.output)

    def test_cli_polls_with_fresh_reads_and_one_second_sleep(self):
        runner = app_module.app.test_cli_runner()
        pending = {
            "available": True, "status": "pending", "age_seconds": 1,
            "verified": False,
        }
        completed = {
            "available": True, "status": "completed", "age_seconds": 2,
            "verified": True,
        }
        with mock.patch.object(
            app_module, "request_worker_canary", return_value=self.requested()
        ), mock.patch.object(
            app_module, "get_worker_canary_result",
            side_effect=[pending, completed],
        ) as read, mock.patch.object(app_module.time, "sleep") as sleep:
            result = runner.invoke(args=[
                "verify-worker-canary", "--expected-commit", COMMIT,
                "--wait-seconds", "1",
            ])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once_with(1)


class PostgreSQLCanaryPathTestCase(unittest.TestCase):
    class Cursor:
        def __init__(self, rows, fail=False, rowcount=1):
            self.rows = list(rows)
            self.statements = []
            self.rowcount = rowcount
            self.fail = fail

        def execute(self, statement, parameters=()):
            self.statements.append((statement, parameters))
            if self.fail:
                raise RuntimeError("synthetic raw PostgreSQL failure")

        def fetchone(self):
            return self.rows.pop(0) if self.rows else None

    class Connection:
        def __init__(self, rows, fail=False, rowcount=1):
            self.cursor_value = PostgreSQLCanaryPathTestCase.Cursor(
                rows, fail, rowcount
            )
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

    def canary_row(self, now, status="pending", token=None):
        return (
            app_module.WORKER_ROLE, PROBE_A, COMMIT, status, now,
            now + timedelta(seconds=600), None, None, token,
            now + timedelta(seconds=30) if token else None,
            None, None, now,
        )

    def test_mocked_postgresql_schema_request_claim_and_finalize(self):
        schema_cursor = mock.Mock()
        with mock.patch.object(app_module, "using_postgres", return_value=True):
            app_module._create_worker_canary_schema(schema_cursor)
        schema_sql = schema_cursor.execute.call_args.args[0]
        self.assertIn("worker_role TEXT PRIMARY KEY", schema_sql)
        self.assertNotIn("AUTOINCREMENT", schema_sql)

        now = app_module.utc_now()
        request_connection = self.Connection([
            (COMMIT, now, now), None,
        ])
        with mock.patch.object(app_module, "using_postgres", return_value=True), \
                mock.patch.object(app_module, "db", return_value=request_connection):
            requested = app_module.request_worker_canary(
                app_module.WORKER_ROLE, COMMIT, now=now,
                probe_id_factory=lambda: PROBE_A,
            )
        self.assertTrue(requested["ok"])
        request_sql = "\n".join(
            statement for statement, _ in request_connection.cursor_value.statements
        )
        self.assertIn("FOR UPDATE", request_sql)
        self.assertIn("ON CONFLICT", request_sql)
        self.assertTrue(request_connection.committed)
        self.assertTrue(request_connection.closed)

        claim_connection = self.Connection([
            (INSTANCE_A, COMMIT, now, now), self.canary_row(now),
        ])
        with mock.patch.object(app_module, "using_postgres", return_value=True), \
                mock.patch.object(app_module, "db", return_value=claim_connection):
            claimed = app_module.claim_worker_canary(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT, now=now,
                claim_token_factory=lambda: TOKEN_A,
            )
        self.assertEqual(claimed["code"], "worker_canary_claimed")
        claim_sql = "\n".join(
            statement for statement, _ in claim_connection.cursor_value.statements
        )
        self.assertIn("FOR UPDATE", claim_sql)
        self.assertIn("instance_id", claim_sql)
        self.assertIn("claim_token", claim_sql)
        self.assertTrue(claim_connection.committed)
        self.assertTrue(claim_connection.closed)

        final_connection = self.Connection([(INSTANCE_A, COMMIT)])
        with mock.patch.object(app_module, "using_postgres", return_value=True), \
                mock.patch.object(app_module, "db", return_value=final_connection):
            finalized = app_module.finalize_worker_canary(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT,
                PROBE_A, TOKEN_A, now=now,
            )
        self.assertTrue(finalized["ok"])
        final_sql = "\n".join(
            statement for statement, _ in final_connection.cursor_value.statements
        )
        self.assertIn("claim_token", final_sql)
        self.assertIn("claim_expires_at", final_sql)
        self.assertIn("expected_commit", final_sql)
        self.assertTrue(final_connection.committed)
        self.assertTrue(final_connection.closed)

    def test_mocked_postgresql_claim_rejections_are_distinct_and_safe(self):
        now = app_module.utc_now()
        cases = (
            (
                "ownership_lost", (INSTANCE_B, COMMIT, now, now),
                "worker_heartbeat_ownership_lost",
            ),
            (
                "stale_poll",
                (INSTANCE_A, COMMIT, now - timedelta(seconds=601), now),
                "worker_canary_worker_stale",
            ),
            (
                "stale_success",
                (INSTANCE_A, COMMIT, now, now - timedelta(seconds=601)),
                "worker_canary_worker_stale",
            ),
            (
                "malformed_poll", (INSTANCE_A, COMMIT, "bad-poll", now),
                "worker_canary_worker_stale",
            ),
            (
                "malformed_success", (INSTANCE_A, COMMIT, now, "bad-success"),
                "worker_canary_worker_stale",
            ),
            (
                "commit_mismatch", (INSTANCE_A, OTHER_COMMIT, now, now),
                "worker_canary_commit_mismatch",
            ),
            (
                "commit_unknown", (INSTANCE_A, None, now, now),
                "worker_canary_commit_unknown",
            ),
        )
        for name, heartbeat, expected_code in cases:
            with self.subTest(name=name):
                connection = self.Connection([heartbeat])
                with mock.patch.object(
                    app_module, "using_postgres", return_value=True
                ), mock.patch.object(
                    app_module, "db", return_value=connection
                ):
                    result = app_module.claim_worker_canary(
                        app_module.WORKER_ROLE, INSTANCE_A, COMMIT, now=now
                    )
                self.assertEqual(result["code"], expected_code)
                self.assertTrue(connection.rolled_back)
                self.assertTrue(connection.closed)
                statements = "\n".join(
                    statement for statement, _
                    in connection.cursor_value.statements
                )
                self.assertIn("last_poll_at", statements)
                self.assertIn("last_success_at", statements)
                self.assertNotIn("SET status = 'in_progress'", statements)

    def test_mocked_postgresql_ownership_loss_stops_before_synthetic(self):
        now = app_module.utc_now()
        connection = self.Connection([
            (INSTANCE_B, COMMIT, now, now),
        ])
        with mock.patch.object(
            app_module, "using_postgres", return_value=True
        ), mock.patch.object(
            app_module, "db", return_value=connection
        ), mock.patch.object(
            app_module, "perform_worker_canary_synthetic_step"
        ) as synthetic, mock.patch.object(
            app_module, "finalize_worker_canary"
        ) as finalize, mock.patch.object(
            app_module.requests, "post"
        ) as post, mock.patch.object(
            app_module.requests, "request"
        ) as request, mock.patch("socket.getaddrinfo") as dns:
            result = app_module.run_worker_canary_once(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT
            )
        self.assertEqual(result["code"], "worker_heartbeat_ownership_lost")
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        synthetic.assert_not_called()
        finalize.assert_not_called()
        post.assert_not_called()
        request.assert_not_called()
        dns.assert_not_called()

    def test_mocked_postgresql_claim_close_synthetic_finalize_order(self):
        now = app_module.utc_now()
        events = []

        class TracingCursor(self.Cursor):
            def __init__(self, rows, label):
                super().__init__(rows)
                self.label = label
                self.started = False

            def execute(self, statement, parameters=()):
                if not self.started:
                    self.started = True
                    events.append(f"{self.label}_transaction_begin")
                normalized = " ".join(str(statement).split()).lower()
                if "set status = 'in_progress'" in normalized:
                    events.append("claim_update")
                elif "set status = 'completed'" in normalized:
                    events.append("finalize_update")
                return super().execute(statement, parameters)

        class TracingConnection(self.Connection):
            def __init__(self, rows, label):
                self.cursor_value = TracingCursor(rows, label)
                self.label = label
                self.committed = False
                self.rolled_back = False
                self.closed = False

            def commit(self):
                self.committed = True
                events.append(f"{self.label}_commit")

            def close(self):
                self.closed = True
                events.append(f"{self.label}_connection_close")

        claim_connection = TracingConnection([
            (INSTANCE_A, COMMIT, now, now), self.canary_row(now),
        ], "claim")
        final_connection = TracingConnection([
            (INSTANCE_A, COMMIT),
        ], "finalize")
        connections = [claim_connection, final_connection]

        def open_connection():
            connection = connections.pop(0)
            events.append(f"{connection.label}_connection_open")
            return connection

        def synthetic_step():
            self.assertTrue(claim_connection.closed)
            self.assertFalse(final_connection.cursor_value.started)
            events.append("synthetic_step")
            return {
                "ok": True,
                "code": "worker_canary_synthetic_step_completed",
            }

        with mock.patch.object(
            app_module, "using_postgres", return_value=True
        ), mock.patch.object(
            app_module, "db", side_effect=open_connection
        ), mock.patch.object(
            app_module, "perform_worker_canary_synthetic_step",
            side_effect=synthetic_step,
        ), mock.patch.object(
            app_module.requests, "post"
        ) as post, mock.patch.object(
            app_module.requests, "request"
        ) as request, mock.patch("socket.getaddrinfo") as dns:
            result = app_module.run_worker_canary_once(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT
            )
        self.assertTrue(result["completed"])
        self.assertEqual(events, [
            "claim_connection_open", "claim_transaction_begin",
            "claim_update", "claim_commit", "claim_connection_close",
            "synthetic_step", "finalize_connection_open",
            "finalize_transaction_begin", "finalize_update",
            "finalize_commit", "finalize_connection_close",
        ])
        post.assert_not_called()
        request.assert_not_called()
        dns.assert_not_called()

    def test_mocked_postgresql_failure_rolls_back_closes_and_logs_safely(self):
        connection = self.Connection([], fail=True)
        with mock.patch.object(app_module, "using_postgres", return_value=True), \
                mock.patch.object(app_module, "db", return_value=connection), \
                self.assertLogs(app_module.logger, level="WARNING") as logs:
            result = app_module.claim_worker_canary(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT
            )
        self.assertEqual(result["code"], "worker_canary_unavailable")
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        self.assertNotIn(
            "synthetic raw PostgreSQL failure", "\n".join(logs.output)
        )

        final_connection = self.Connection([], fail=True)
        with mock.patch.object(app_module, "using_postgres", return_value=True), \
                mock.patch.object(app_module, "db", return_value=final_connection), \
                self.assertLogs(app_module.logger, level="WARNING") as final_logs:
            finalized = app_module.finalize_worker_canary(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT,
                PROBE_A, TOKEN_A,
            )
        self.assertEqual(finalized["code"], "worker_canary_unavailable")
        self.assertTrue(final_connection.rolled_back)
        self.assertTrue(final_connection.closed)
        self.assertNotIn(
            "synthetic raw PostgreSQL failure", "\n".join(final_logs.output)
        )

    def test_mocked_postgresql_stale_token_is_rejected(self):
        now = app_module.utc_now()
        connection = self.Connection(
            [(INSTANCE_A, COMMIT)], rowcount=0
        )
        with mock.patch.object(app_module, "using_postgres", return_value=True), \
                mock.patch.object(app_module, "db", return_value=connection):
            result = app_module.finalize_worker_canary(
                app_module.WORKER_ROLE, INSTANCE_A, COMMIT,
                PROBE_A, TOKEN_A, now=now,
            )
        self.assertEqual(result["code"], "worker_canary_claim_lost")
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
