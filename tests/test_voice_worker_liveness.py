import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

import app as app_module
import worker as worker_module


SYNTHETIC_COMMIT = "a" * 40
SYNTHETIC_OTHER_COMMIT = "b" * 40
SYNTHETIC_INSTANCE = "synthetic-worker-instance"
INVALID_COMMITS = (
    "a" * 39,
    "a" * 41,
    " " + "a" * 40,
    "a" * 40 + " ",
    "\t" + "a" * 40,
    "a" * 40 + "\t",
    "a" * 40 + "\n",
    "a" * 40 + "\r",
    "a" * 40 + "\r\n",
    "a" * 40 + "\0",
    "\u00a0" + "a" * 40,
    "\u2003" + "a" * 40,
    "g" * 40,
    "Ａ" * 40,
    "!" * 40,
)


class SQLiteHeartbeatTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(
            self.temp_dir.name, "worker-heartbeat.db"
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
        try:
            cursor = connection.execute(statement, parameters)
            rows = cursor.fetchall()
            connection.commit()
            return rows
        finally:
            connection.close()

    def start(self, instance_id=SYNTHETIC_INSTANCE, now=None, commit=SYNTHETIC_COMMIT):
        return app_module.start_worker_heartbeat(
            app_module.WORKER_ROLE, instance_id, commit, now=now
        )

    def heartbeat_row(self):
        rows = self.execute(
            """
            SELECT worker_role, instance_id, deployed_commit, started_at,
                   last_poll_at, last_success_at,
                   last_voice_maintenance_at, last_generic_job_at,
                   last_error_code, updated_at
            FROM worker_heartbeats
            WHERE worker_role = ?
            """,
            (app_module.WORKER_ROLE,),
        )
        return rows[0] if rows else None

    def test_fresh_schema_and_repeatable_init_db(self):
        columns = [
            row[1]
            for row in self.execute("PRAGMA table_info(worker_heartbeats)")
        ]
        self.assertEqual(columns, [
            "worker_role", "instance_id", "deployed_commit", "started_at",
            "last_poll_at", "last_success_at", "last_voice_maintenance_at",
            "last_generic_job_at", "last_error_code", "updated_at",
        ])
        app_module.init_db()
        self.assertEqual(
            self.execute("SELECT COUNT(*) FROM worker_heartbeats")[0][0], 0
        )

    def test_startup_upsert_keeps_one_row_per_role(self):
        first = app_module.utc_now()
        second = first + timedelta(seconds=5)
        self.assertTrue(self.start(now=first)["ok"])
        self.assertTrue(self.start("replacement-instance", second)["ok"])
        self.assertEqual(
            self.execute("SELECT COUNT(*) FROM worker_heartbeats")[0][0], 1
        )
        row = self.heartbeat_row()
        self.assertEqual(row[1], "replacement-instance")
        self.assertEqual(app_module.parse_db_datetime(row[3]), second)
        self.assertIsNone(row[5])
        self.assertIsNone(row[8])

    def test_periodic_update_preserves_started_at_and_sets_semantic_times(self):
        started = app_module.utc_now()
        updated = started + timedelta(seconds=30)
        self.start(now=started)
        result = app_module.update_worker_heartbeat(
            app_module.WORKER_ROLE,
            SYNTHETIC_INSTANCE,
            SYNTHETIC_COMMIT,
            poll_completed=True,
            iteration_succeeded=True,
            voice_maintenance_completed=True,
            generic_job_completed=True,
            now=updated,
        )
        self.assertTrue(result["ok"])
        row = self.heartbeat_row()
        self.assertEqual(app_module.parse_db_datetime(row[3]), started)
        for index in (4, 5, 6, 7, 9):
            self.assertEqual(app_module.parse_db_datetime(row[index]), updated)

    def test_update_requires_exact_instance_ownership(self):
        self.start()
        before = self.heartbeat_row()
        result = app_module.update_worker_heartbeat(
            app_module.WORKER_ROLE,
            "older-synthetic-instance",
            SYNTHETIC_COMMIT,
            poll_completed=True,
            iteration_succeeded=True,
        )
        self.assertEqual(result["code"], "worker_heartbeat_ownership_lost")
        self.assertEqual(self.heartbeat_row(), before)

    def test_replaced_process_cannot_overwrite_new_heartbeat(self):
        started = app_module.utc_now()
        self.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, handshake_request_id, upstream_call_id,
                termination_status, termination_attempts, started_at,
                created_at, updated_at
            ) VALUES (?, 'ended', ?, ?, 'pending', 2, ?, ?, ?)
            """,
            (7, "replacement-request", "replacement-call", started,
             started, started),
        )
        voice_before = self.execute(
            "SELECT termination_status, termination_attempts "
            "FROM agent_voice_sessions"
        )
        self.start("old-process", started)
        self.start("new-process", started + timedelta(seconds=1))
        old_result = app_module.update_worker_heartbeat(
            app_module.WORKER_ROLE,
            "old-process",
            SYNTHETIC_COMMIT,
            poll_completed=True,
            now=started + timedelta(seconds=60),
        )
        new_result = app_module.update_worker_heartbeat(
            app_module.WORKER_ROLE,
            "new-process",
            SYNTHETIC_COMMIT,
            poll_completed=True,
            iteration_succeeded=True,
            now=started + timedelta(seconds=61),
        )
        self.assertEqual(
            old_result,
            {"ok": False, "code": "worker_heartbeat_ownership_lost"},
        )
        self.assertNotIn("instance_id", old_result)
        self.assertTrue(new_result["ok"])
        self.assertEqual(self.heartbeat_row()[1], "new-process")
        self.assertEqual(
            self.execute(
                "SELECT termination_status, termination_attempts "
                "FROM agent_voice_sessions"
            ),
            voice_before,
        )

    def test_error_code_is_bounded_safe_and_success_clears_it(self):
        self.start()
        invalid = app_module.update_worker_heartbeat(
            app_module.WORKER_ROLE,
            SYNTHETIC_INSTANCE,
            SYNTHETIC_COMMIT,
            error_code="unsafe error with spaces",
        )
        self.assertFalse(invalid["ok"])
        valid = app_module.update_worker_heartbeat(
            app_module.WORKER_ROLE,
            SYNTHETIC_INSTANCE,
            SYNTHETIC_COMMIT,
            error_code="worker_iteration_failed",
        )
        self.assertTrue(valid["ok"])
        self.assertEqual(self.heartbeat_row()[8], "worker_iteration_failed")
        app_module.update_worker_heartbeat(
            app_module.WORKER_ROLE,
            SYNTHETIC_INSTANCE,
            SYNTHETIC_COMMIT,
            poll_completed=True,
            iteration_succeeded=True,
        )
        self.assertIsNone(self.heartbeat_row()[8])

    def test_liveness_recent_then_stale_after_600_seconds(self):
        now = app_module.utc_now()
        self.start(now=now)
        app_module.update_worker_heartbeat(
            app_module.WORKER_ROLE,
            SYNTHETIC_INSTANCE,
            SYNTHETIC_COMMIT,
            poll_completed=True,
            iteration_succeeded=True,
            voice_maintenance_completed=True,
            now=now,
        )
        recent = app_module.get_worker_liveness(
            app_module.WORKER_ROLE, now=now + timedelta(seconds=600)
        )
        stale = app_module.get_worker_liveness(
            app_module.WORKER_ROLE, now=now + timedelta(seconds=601)
        )
        self.assertFalse(recent["stale"])
        self.assertTrue(stale["stale"])
        self.assertEqual(recent["heartbeat_age_seconds"], 600)
        self.assertNotIn("instance_id", recent)

    def test_missing_success_or_malformed_timestamp_fails_closed(self):
        now = app_module.utc_now()
        self.start(now=now)
        missing_success = app_module.get_worker_liveness(
            app_module.WORKER_ROLE, now=now
        )
        self.assertTrue(missing_success["stale"])
        self.execute(
            "UPDATE worker_heartbeats SET last_poll_at = 'malformed', "
            "last_success_at = 'malformed' WHERE worker_role = ?",
            (app_module.WORKER_ROLE,),
        )
        malformed = app_module.get_worker_liveness(
            app_module.WORKER_ROLE, now=now
        )
        self.assertTrue(malformed["stale"])
        self.assertIsNone(malformed["heartbeat_age_seconds"])
        self.assertIsNone(malformed["success_age_seconds"])

    def test_age_values_are_nonnegative(self):
        now = app_module.utc_now()
        future = now + timedelta(seconds=30)
        self.start(now=future)
        app_module.update_worker_heartbeat(
            app_module.WORKER_ROLE,
            SYNTHETIC_INSTANCE,
            SYNTHETIC_COMMIT,
            poll_completed=True,
            iteration_succeeded=True,
            now=future,
        )
        result = app_module.get_worker_liveness(
            app_module.WORKER_ROLE, now=now
        )
        self.assertEqual(result["heartbeat_age_seconds"], 0)
        self.assertEqual(result["success_age_seconds"], 0)

    def test_heartbeat_failure_does_not_modify_voice_termination_rows(self):
        now = app_module.utc_now()
        self.execute(
            """
            INSERT INTO agent_voice_sessions (
                user_id, status, handshake_request_id, upstream_call_id,
                termination_status, termination_attempts, started_at,
                created_at, updated_at
            ) VALUES (?, 'ended', ?, ?, 'pending', 2, ?, ?, ?)
            """,
            (7, "synthetic-request", "synthetic-call", now, now, now),
        )
        before = self.execute(
            "SELECT termination_status, termination_attempts "
            "FROM agent_voice_sessions"
        )
        with mock.patch.object(
            app_module, "db", side_effect=RuntimeError("synthetic raw SQL error")
        ), self.assertLogs(app_module.logger, level="WARNING") as logs:
            result = app_module.update_worker_heartbeat(
                app_module.WORKER_ROLE,
                SYNTHETIC_INSTANCE,
                SYNTHETIC_COMMIT,
                error_code="worker_iteration_failed",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(
            self.execute(
                "SELECT termination_status, termination_attempts "
                "FROM agent_voice_sessions"
            ),
            before,
        )
        self.assertNotIn("synthetic raw SQL error", "\n".join(logs.output))

    def test_schema_and_row_contain_no_voice_or_user_identifiers(self):
        self.start()
        column_names = {
            row[1] for row in self.execute("PRAGMA table_info(worker_heartbeats)")
        }
        prohibited = {
            "user_id", "voice_session_id", "call_id", "claim_token",
            "transcript", "api_key", "database_url", "service_url",
        }
        self.assertTrue(column_names.isdisjoint(prohibited))
        serialized = "|".join(str(value or "") for value in self.heartbeat_row())
        for value in ("synthetic-call", "synthetic-user", "synthetic-api-key"):
            self.assertNotIn(value, serialized)


class ValidationAndSchemaTestCase(unittest.TestCase):
    def test_commit_resolution_uses_only_valid_render_metadata(self):
        with mock.patch.dict(os.environ, {
            "RENDER": "true", "RENDER_GIT_COMMIT": "A" * 40
        }, clear=True):
            self.assertEqual(
                app_module.resolve_worker_deployed_commit(), "a" * 40
            )
        for environment in (
            {},
            {"RENDER": "false", "RENDER_GIT_COMMIT": "a" * 40},
            {"RENDER": "true", "RENDER_GIT_COMMIT": "not-a-commit"},
            {"RENDER": "true", "RENDER_GIT_COMMIT": "a" * 39},
            *(
                {"RENDER": "true", "RENDER_GIT_COMMIT": value}
                for value in INVALID_COMMITS
                if "\0" not in value
            ),
        ):
            with self.subTest(environment=environment), mock.patch.dict(
                os.environ, environment, clear=True
            ):
                self.assertIsNone(app_module.resolve_worker_deployed_commit())

    def test_commit_validation_is_exact_and_normalized(self):
        valid_mixed = "0123456789abcdef0123456789ABCDEF01234567"
        self.assertEqual(
            app_module.validate_worker_commit("A" * 40), "a" * 40
        )
        self.assertEqual(
            app_module.validate_worker_commit(valid_mixed), valid_mixed.lower()
        )
        for value in (None, "", 7, *INVALID_COMMITS):
            with self.subTest(value=repr(value)):
                self.assertIsNone(app_module.validate_worker_commit(value))

    def test_role_instance_and_error_code_validation_is_exact(self):
        validators = (
            (app_module.normalize_worker_role, "businessbuilder-worker", 64),
            (app_module.normalize_worker_instance_id, "safe-instance", 120),
            (app_module.normalize_worker_error_code, "safe_error", 120),
        )
        invalid_wrappers = (
            lambda value: " " + value,
            lambda value: value + " ",
            lambda value: "\t" + value,
            lambda value: value + "\r",
            lambda value: value + "\n",
            lambda value: value + "\0",
            lambda value: "\u00a0" + value,
        )
        for validator, valid, maximum in validators:
            with self.subTest(validator=validator.__name__, case="valid"):
                self.assertIsNotNone(validator(valid))
            for wrapper in invalid_wrappers:
                candidate = wrapper(valid)
                with self.subTest(
                    validator=validator.__name__, candidate=repr(candidate)
                ):
                    self.assertIsNone(validator(candidate))
            self.assertIsNone(validator("a" * (maximum + 1)))
            self.assertIsNone(validator(7))

    def test_process_instance_identity_is_random_and_bounded(self):
        first = worker_module.generate_worker_instance_id()
        second = worker_module.generate_worker_instance_id()
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), 120)
        self.assertIsNotNone(app_module.normalize_worker_instance_id(first))

    def test_mocked_postgresql_schema_is_compatible(self):
        cursor = mock.Mock()
        with mock.patch.object(app_module, "using_postgres", return_value=True):
            app_module._create_worker_heartbeat_schema(cursor)
        statement = cursor.execute.call_args.args[0]
        self.assertIn("CREATE TABLE IF NOT EXISTS worker_heartbeats", statement)
        self.assertIn("worker_role TEXT PRIMARY KEY", statement)
        self.assertNotIn("AUTOINCREMENT", statement)


class ConnectionFailureTestCase(unittest.TestCase):
    class FailingCursor:
        rowcount = 0

        def execute(self, statement, parameters=()):
            del statement, parameters
            raise RuntimeError("synthetic database url and raw SQL detail")

    class FailingConnection:
        def __init__(self):
            self.rolled_back = False
            self.closed = False

        def cursor(self):
            return ConnectionFailureTestCase.FailingCursor()

        def commit(self):
            raise AssertionError("commit must not occur")

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    def test_start_failure_rolls_back_closes_and_logs_safely(self):
        connection = self.FailingConnection()
        with mock.patch.object(app_module, "db", return_value=connection), \
                self.assertLogs(app_module.logger, level="WARNING") as logs:
            result = app_module.start_worker_heartbeat(
                app_module.WORKER_ROLE, SYNTHETIC_INSTANCE, SYNTHETIC_COMMIT
            )
        self.assertFalse(result["ok"])
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        output = "\n".join(logs.output)
        self.assertIn("worker_heartbeat_start_failed", output)
        self.assertNotIn("synthetic database", output)
        self.assertNotIn(SYNTHETIC_INSTANCE, output)

    def test_update_failure_rolls_back_closes_and_logs_safely(self):
        connection = self.FailingConnection()
        with mock.patch.object(app_module, "db", return_value=connection), \
                self.assertLogs(app_module.logger, level="WARNING") as logs:
            result = app_module.update_worker_heartbeat(
                app_module.WORKER_ROLE,
                SYNTHETIC_INSTANCE,
                SYNTHETIC_COMMIT,
                error_code="worker_iteration_failed",
            )
        self.assertFalse(result["ok"])
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        output = "\n".join(logs.output)
        self.assertNotIn("raw SQL detail", output)
        self.assertNotIn(SYNTHETIC_INSTANCE, output)

    def test_read_failure_is_safe_and_closes(self):
        connection = self.FailingConnection()
        with mock.patch.object(app_module, "db", return_value=connection), \
                self.assertLogs(app_module.logger, level="WARNING") as logs:
            result = app_module.get_worker_liveness(app_module.WORKER_ROLE)
        self.assertFalse(result["available"])
        self.assertTrue(connection.closed)
        self.assertNotIn("raw SQL detail", "\n".join(logs.output))


class ReadinessCliTestCase(unittest.TestCase):
    def invoke(self, result):
        runner = app_module.app.test_cli_runner()
        with mock.patch.object(
            app_module, "get_worker_liveness", return_value=result
        ):
            return runner.invoke(
                args=["check-worker-liveness", "--expected-commit", SYNTHETIC_COMMIT]
            )

    def live_result(self, **overrides):
        result = {
            "available": True,
            "found": True,
            "worker_role": app_module.WORKER_ROLE,
            "deployed_commit": SYNTHETIC_COMMIT,
            "heartbeat_age_seconds": 10,
            "success_age_seconds": 11,
            "maintenance_age_seconds": 12,
            "stale": False,
        }
        result.update(overrides)
        return result

    def test_live_matching_exit_zero(self):
        result = self.invoke(self.live_result())
        self.assertEqual(result.exit_code, 0)
        self.assertIn("status=live", result.output)
        self.assertIn("commit=match", result.output)

    def test_exit_code_precedence(self):
        cases = (
            ({"available": False, "found": False}, 6, "unavailable"),
            ({"available": True, "found": False}, 2, "missing"),
            (self.live_result(stale=True, deployed_commit=None), 3, "stale"),
            (self.live_result(deployed_commit=SYNTHETIC_OTHER_COMMIT), 4, "live"),
            (self.live_result(deployed_commit=None), 5, "live"),
        )
        for liveness, exit_code, status in cases:
            with self.subTest(exit_code=exit_code):
                result = self.invoke(liveness)
                self.assertEqual(result.exit_code, exit_code)
                self.assertIn(f"status={status}", result.output)

    def test_output_is_safe_aggregate_only(self):
        liveness = self.live_result()
        liveness.update({
            "instance_id": "synthetic-secret-instance",
            "started_at": "synthetic-raw-timestamp",
            "last_error_code": "synthetic-raw-error",
        })
        result = self.invoke(liveness)
        self.assertEqual(len(result.output.strip().splitlines()), 6)
        for prohibited in (
            "synthetic-secret-instance", "synthetic-raw-timestamp",
            "synthetic-raw-error", "DATABASE_URL", "OPENAI_API_KEY",
            "user_id", "call_id", "claim_token",
        ):
            self.assertNotIn(prohibited, result.output)

    def test_unknown_ages_are_rendered_without_raw_timestamps(self):
        result = self.invoke(self.live_result(
            heartbeat_age_seconds=None,
            success_age_seconds=None,
            maintenance_age_seconds=None,
            stale=True,
        ))
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.output.count("unknown"), 3)

    def test_exact_expected_commit_validation_precedes_liveness_query(self):
        runner = app_module.app.test_cli_runner()
        for invalid in INVALID_COMMITS:
            with self.subTest(value=repr(invalid)), mock.patch.object(
                app_module, "get_worker_liveness"
            ) as liveness:
                result = runner.invoke(args=[
                    "check-worker-liveness", "--expected-commit", invalid
                ])
                self.assertEqual(result.exit_code, 2)
                liveness.assert_not_called()
                combined = result.output + getattr(result, "stderr", "")
                self.assertIn(
                    "must be exactly 40 hexadecimal characters", combined
                )
                self.assertNotIn("Traceback", combined)
                self.assertNotIn("DATABASE_URL", combined)
                self.assertNotIn("OPENAI_API_KEY", combined)
                self.assertNotIn(invalid, combined)

    def test_uppercase_expected_commit_is_valid_and_compared_lowercase(self):
        runner = app_module.app.test_cli_runner()
        with mock.patch.object(
            app_module, "get_worker_liveness",
            return_value=self.live_result(deployed_commit=SYNTHETIC_COMMIT),
        ) as liveness:
            result = runner.invoke(args=[
                "check-worker-liveness", "--expected-commit", "A" * 40
            ])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("commit=match", result.output)
        liveness.assert_called_once_with(app_module.WORKER_ROLE)


class WorkerLoopTestCase(unittest.TestCase):
    def worker_context(self, **overrides):
        patches = {
            "init_db": mock.Mock(),
            "resolve_worker_deployed_commit": mock.Mock(
                return_value=SYNTHETIC_COMMIT
            ),
            "start_worker_heartbeat": mock.Mock(return_value={
                "ok": True, "code": "worker_heartbeat_started"
            }),
            "get_monitoring_config": mock.Mock(
                return_value={"worker_poll_seconds": 7}
            ),
        }
        patches.update(overrides)
        return mock.patch.multiple(worker_module, **patches)

    def test_startup_heartbeat_follows_init_db(self):
        events = []
        with mock.patch.object(
            worker_module, "init_db", side_effect=lambda: events.append("init")
        ), mock.patch.object(
            worker_module, "resolve_worker_deployed_commit",
            side_effect=lambda: events.append("commit") or SYNTHETIC_COMMIT
        ), mock.patch.object(
            worker_module, "start_worker_heartbeat",
            side_effect=lambda *args: events.append("heartbeat") or {"ok": True}
        ), mock.patch.object(
            worker_module, "run_worker_iteration_details",
            return_value={
                "processed": False,
                "voice_maintenance_completed": True,
                "generic_job_completed": False,
            }
        ), mock.patch.object(
            worker_module, "update_worker_heartbeat", return_value={"ok": True}
        ), mock.patch.object(
            worker_module, "get_monitoring_config",
            return_value={"worker_poll_seconds": 7}
        ):
            worker_module.run_worker(
                once=True,
                instance_id_factory=lambda: SYNTHETIC_INSTANCE,
            )
        self.assertEqual(events[:3], ["init", "commit", "heartbeat"])

    def test_startup_heartbeat_failure_prevents_polling(self):
        with mock.patch.object(worker_module, "init_db"), \
                mock.patch.object(
                    worker_module, "resolve_worker_deployed_commit",
                    return_value=None
                ), mock.patch.object(
                    worker_module, "start_worker_heartbeat",
                    return_value={"ok": False}
                ), mock.patch.object(
                    worker_module, "run_worker_iteration_details"
                ) as iteration:
            with self.assertRaisesRegex(
                RuntimeError, "worker_heartbeat_start_failed"
            ):
                worker_module.run_worker(
                    max_iterations=1,
                    instance_id_factory=lambda: SYNTHETIC_INSTANCE,
                )
        iteration.assert_not_called()

    def test_voice_runs_before_generic_and_voice_failure_is_isolated(self):
        events = []
        with mock.patch.object(
            worker_module, "run_voice_maintenance_once",
            side_effect=lambda: events.append("voice") or {"processed": 0}
        ), mock.patch.object(
            worker_module, "run_background_job_once",
            side_effect=lambda worker_id: events.append("generic") or True
        ):
            details = worker_module.run_worker_iteration_details("synthetic-worker")
        self.assertEqual(events, ["voice", "generic"])
        self.assertTrue(details["processed"])

        with mock.patch.object(
            worker_module, "run_voice_maintenance_once",
            side_effect=RuntimeError("synthetic raw voice exception")
        ), mock.patch.object(
            worker_module, "run_background_job_once", return_value=True
        ) as generic, self.assertLogs(worker_module.logger, level="WARNING") as logs:
            details = worker_module.run_worker_iteration_details("synthetic-worker")
        self.assertTrue(details["processed"])
        generic.assert_called_once_with("synthetic-worker")
        self.assertNotIn("synthetic raw voice exception", "\n".join(logs.output))

    def test_processed_work_has_no_sleep_and_idle_iteration_sleeps(self):
        stop_requested = mock.Mock()
        with mock.patch.object(
            worker_module, "run_worker_iteration", return_value=True
        ):
            self.assertTrue(worker_module.run_worker_loop_iteration(
                "synthetic-worker", stop_requested, 7
            ))
        stop_requested.wait.assert_not_called()
        stop_requested.reset_mock()
        with mock.patch.object(
            worker_module, "run_worker_iteration", return_value=False
        ):
            self.assertFalse(worker_module.run_worker_loop_iteration(
                "synthetic-worker", stop_requested, 7
            ))
        stop_requested.wait.assert_called_once_with(7)

    def test_normal_heartbeat_writes_are_rate_limited(self):
        stop_requested = mock.Mock()
        stop_requested.is_set.return_value = False
        details = {
            "processed": True,
            "voice_maintenance_completed": True,
            "generic_job_completed": True,
        }
        with mock.patch.object(worker_module, "init_db"), \
                mock.patch.object(
                    worker_module, "resolve_worker_deployed_commit",
                    return_value=SYNTHETIC_COMMIT
                ), mock.patch.object(
                    worker_module, "start_worker_heartbeat",
                    return_value={"ok": True}
                ), mock.patch.object(
                    worker_module, "get_monitoring_config",
                    return_value={"worker_poll_seconds": 7}
                ), mock.patch.object(
                    worker_module, "run_worker_iteration_details",
                    return_value=details
                ), mock.patch.object(
                    worker_module, "update_worker_heartbeat",
                    return_value={"ok": True}
                ) as heartbeat:
            worker_module.run_worker(
                stop_requested=stop_requested,
                max_iterations=4,
                monotonic_fn=iter((0, 10, 29, 30)).__next__,
                instance_id_factory=lambda: SYNTHETIC_INSTANCE,
            )
        self.assertEqual(heartbeat.call_count, 2)
        stop_requested.wait.assert_not_called()

    def test_idle_heartbeat_writes_are_rate_limited(self):
        stop_requested = mock.Mock()
        stop_requested.is_set.return_value = False
        details = {
            "processed": False,
            "voice_maintenance_completed": True,
            "generic_job_completed": False,
        }
        with self.worker_context(), mock.patch.object(
            worker_module, "run_worker_iteration_details", return_value=details
        ) as iteration, mock.patch.object(
            worker_module, "update_worker_heartbeat", return_value={"ok": True}
        ) as heartbeat:
            worker_module.run_worker(
                stop_requested=stop_requested,
                max_iterations=4,
                monotonic_fn=iter((0, 10, 29, 30)).__next__,
                instance_id_factory=lambda: SYNTHETIC_INSTANCE,
            )
        self.assertEqual(iteration.call_count, 4)
        self.assertEqual(heartbeat.call_count, 2)
        self.assertEqual(stop_requested.wait.call_count, 4)

    def test_normal_heartbeat_ownership_loss_fences_worker(self):
        stop_requested = mock.Mock()
        stop_requested.is_set.return_value = False
        details = {
            "processed": True,
            "voice_maintenance_completed": True,
            "generic_job_completed": True,
        }
        with self.worker_context(), mock.patch.object(
            worker_module, "run_worker_iteration_details", return_value=details
        ) as iteration, mock.patch.object(
            worker_module, "update_worker_heartbeat",
            return_value={
                "ok": False, "code": "worker_heartbeat_ownership_lost"
            },
        ) as heartbeat, self.assertLogs(
            worker_module.logger, level="WARNING"
        ) as logs:
            result = worker_module.run_worker(
                stop_requested=stop_requested,
                max_iterations=5,
                monotonic_fn=lambda: 0,
                instance_id_factory=lambda: SYNTHETIC_INSTANCE,
            )
        self.assertEqual(result, worker_module.WORKER_OWNERSHIP_LOST)
        iteration.assert_called_once_with(mock.ANY)
        heartbeat.assert_called_once()
        stop_requested.wait.assert_not_called()
        output = "\n".join(logs.output)
        self.assertEqual(output.count("worker_heartbeat_ownership_lost"), 1)
        self.assertNotIn(SYNTHETIC_INSTANCE, output)

    def test_error_heartbeat_ownership_loss_fences_without_retry(self):
        stop_requested = mock.Mock()
        stop_requested.is_set.return_value = False
        raw_error = "synthetic raw database URL and secret"
        with self.worker_context(), mock.patch.object(
            worker_module, "run_worker_iteration_details",
            side_effect=RuntimeError(raw_error),
        ) as iteration, mock.patch.object(
            worker_module, "update_worker_heartbeat",
            return_value={
                "ok": False, "code": "worker_heartbeat_ownership_lost"
            },
        ) as heartbeat, self.assertLogs(
            worker_module.logger, level="WARNING"
        ) as logs:
            result = worker_module.run_worker(
                stop_requested=stop_requested,
                max_iterations=5,
                monotonic_fn=lambda: 0,
                instance_id_factory=lambda: SYNTHETIC_INSTANCE,
            )
        self.assertEqual(result, worker_module.WORKER_OWNERSHIP_LOST)
        iteration.assert_called_once_with(mock.ANY)
        heartbeat.assert_called_once()
        stop_requested.wait.assert_not_called()
        output = "\n".join(logs.output)
        self.assertEqual(output.count("worker_iteration_failed"), 1)
        self.assertEqual(output.count("worker_heartbeat_ownership_lost"), 1)
        self.assertNotIn(raw_error, output)
        self.assertNotIn(SYNTHETIC_INSTANCE, output)

    def test_outer_exception_records_safe_error_sleeps_and_continues(self):
        stop_requested = mock.Mock()
        stop_requested.is_set.return_value = False
        successful = {
            "processed": True,
            "voice_maintenance_completed": True,
            "generic_job_completed": True,
        }
        with mock.patch.object(worker_module, "init_db"), \
                mock.patch.object(
                    worker_module, "resolve_worker_deployed_commit",
                    return_value=None
                ), mock.patch.object(
                    worker_module, "start_worker_heartbeat",
                    return_value={"ok": True}
                ), mock.patch.object(
                    worker_module, "get_monitoring_config",
                    return_value={"worker_poll_seconds": 7}
                ), mock.patch.object(
                    worker_module, "run_worker_iteration_details",
                    side_effect=[
                        RuntimeError("synthetic database URL and token"), successful
                    ]
                ), mock.patch.object(
                    worker_module, "update_worker_heartbeat",
                    return_value={"ok": True}
                ) as heartbeat, self.assertLogs(
                    worker_module.logger, level="WARNING"
                ) as logs:
            worker_module.run_worker(
                stop_requested=stop_requested,
                max_iterations=2,
                monotonic_fn=iter((0, 31)).__next__,
                instance_id_factory=lambda: SYNTHETIC_INSTANCE,
            )
        self.assertEqual(heartbeat.call_count, 2)
        self.assertEqual(
            heartbeat.call_args_list[0].kwargs["error_code"],
            "worker_iteration_failed",
        )
        stop_requested.wait.assert_called_once_with(7)
        output = "\n".join(logs.output)
        self.assertIn("worker_iteration_failed", output)
        self.assertNotIn("synthetic database URL", output)

    def test_repeated_errors_do_not_create_heartbeat_write_busy_loop(self):
        stop_requested = mock.Mock()
        stop_requested.is_set.return_value = False
        with mock.patch.object(worker_module, "init_db"), \
                mock.patch.object(
                    worker_module, "resolve_worker_deployed_commit",
                    return_value=None
                ), mock.patch.object(
                    worker_module, "start_worker_heartbeat",
                    return_value={"ok": True}
                ), mock.patch.object(
                    worker_module, "get_monitoring_config",
                    return_value={"worker_poll_seconds": 7}
                ), mock.patch.object(
                    worker_module, "run_worker_iteration_details",
                    side_effect=RuntimeError("synthetic repeated failure")
                ), mock.patch.object(
                    worker_module, "update_worker_heartbeat",
                    return_value={"ok": False}
                ) as heartbeat, mock.patch.object(worker_module.logger, "warning"):
            worker_module.run_worker(
                stop_requested=stop_requested,
                max_iterations=3,
                monotonic_fn=iter((0, 1, 31)).__next__,
                instance_id_factory=lambda: SYNTHETIC_INSTANCE,
            )
        self.assertEqual(heartbeat.call_count, 2)
        self.assertEqual(stop_requested.wait.call_count, 3)

    def test_keyboard_interrupt_and_system_exit_are_not_swallowed(self):
        for exception in (KeyboardInterrupt(), SystemExit(9)):
            with self.subTest(exception=type(exception).__name__), \
                    mock.patch.object(worker_module, "init_db"), \
                    mock.patch.object(
                        worker_module, "resolve_worker_deployed_commit",
                        return_value=None
                    ), mock.patch.object(
                        worker_module, "start_worker_heartbeat",
                        return_value={"ok": True}
                    ), mock.patch.object(
                        worker_module, "get_monitoring_config",
                        return_value={"worker_poll_seconds": 7}
                    ), mock.patch.object(
                        worker_module, "run_worker_iteration_details",
                        side_effect=exception
                    ):
                with self.assertRaises(type(exception)):
                    worker_module.run_worker(
                        max_iterations=1,
                        instance_id_factory=lambda: SYNTHETIC_INSTANCE,
                    )

    def test_heartbeat_update_failure_does_not_stop_processing(self):
        stop_requested = mock.Mock()
        stop_requested.is_set.return_value = False
        details = {
            "processed": True,
            "voice_maintenance_completed": True,
            "generic_job_completed": True,
        }
        with mock.patch.object(worker_module, "init_db"), \
                mock.patch.object(
                    worker_module, "resolve_worker_deployed_commit",
                    return_value=None
                ), mock.patch.object(
                    worker_module, "start_worker_heartbeat",
                    return_value={"ok": True}
                ), mock.patch.object(
                    worker_module, "get_monitoring_config",
                    return_value={"worker_poll_seconds": 7}
                ), mock.patch.object(
                    worker_module, "run_worker_iteration_details",
                    return_value=details
                ) as iteration, mock.patch.object(
                    worker_module, "update_worker_heartbeat",
                    return_value={
                        "ok": False, "code": "worker_heartbeat_update_failed"
                    }
                ):
            worker_module.run_worker(
                stop_requested=stop_requested,
                max_iterations=2,
                monotonic_fn=iter((0, 1)).__next__,
                instance_id_factory=lambda: SYNTHETIC_INSTANCE,
            )
        self.assertEqual(iteration.call_count, 2)

    def test_empty_queues_make_no_openai_request_and_runtime_flag_is_irrelevant(self):
        with mock.patch.dict(
            os.environ, {"VOICE_RUNTIME_ENABLED": "false"}, clear=True
        ), mock.patch.object(
            worker_module, "run_voice_maintenance_once",
            return_value={"processed": 0}
        ) as maintenance, mock.patch.object(
            worker_module, "run_background_job_once", return_value=False
        ), mock.patch.object(app_module.requests, "post") as upstream:
            details = worker_module.run_worker_iteration_details("synthetic-worker")
        self.assertFalse(details["processed"])
        maintenance.assert_called_once_with()
        upstream.assert_not_called()
        self.assertFalse(app_module.get_voice_config()["enabled"])

    def test_worker_reachable_generic_failure_log_has_no_raw_exception(self):
        job = (1, 7, None, "deep_research", 4, 0, 3, "{}")
        with mock.patch.object(
            app_module, "claim_next_background_job", return_value=job
        ), mock.patch.object(
            app_module, "run_research_job",
            side_effect=RuntimeError("synthetic api key url call token raw body")
        ), mock.patch.object(
            app_module, "fail_background_job"
        ), self.assertLogs(app_module.logger, level="WARNING") as logs:
            self.assertTrue(app_module.run_background_job_once("synthetic-worker"))
        output = "\n".join(logs.output)
        self.assertIn("background_job_failed", output)
        self.assertNotIn("synthetic api key", output)


if __name__ == "__main__":
    unittest.main()
