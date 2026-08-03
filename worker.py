import argparse
import contextvars
import logging
import os
import secrets
import signal
import threading
import time

from app import (
    WORKER_HEARTBEAT_INTERVAL_SECONDS,
    WORKER_ROLE,
    get_monitoring_config,
    init_db,
    resolve_worker_deployed_commit,
    run_background_job_once,
    run_worker_canary_once,
    run_voice_maintenance_once,
    start_worker_heartbeat,
    update_worker_heartbeat,
)


logger = logging.getLogger(__name__)
WORKER_OWNERSHIP_LOST = "worker_heartbeat_ownership_lost"
_worker_canary_context = contextvars.ContextVar(
    "worker_canary_context", default=None
)


def heartbeat_ownership_lost(result):
    return (
        isinstance(result, dict)
        and result.get("code") == WORKER_OWNERSHIP_LOST
    )


def stop_for_heartbeat_ownership_loss():
    logger.warning(WORKER_OWNERSHIP_LOST)
    return WORKER_OWNERSHIP_LOST


def run_worker_iteration_details(worker_id):
    canary_context = _worker_canary_context.get()
    worker_role, instance_id, deployed_commit = (
        canary_context if canary_context is not None else (None, None, None)
    )
    voice_processed = False
    voice_maintenance_completed = False
    canary_processed = False
    canary_completed = False
    try:
        voice_counts = run_voice_maintenance_once()
        voice_maintenance_completed = True
        voice_processed = bool(voice_counts["processed"])
        logger.info(
            "voice_maintenance_completed role=%s processed=%d",
            WORKER_ROLE,
            int(voice_counts["processed"]),
        )
    except Exception:
        logger.warning("voice_maintenance_failed")

    canary = {
        "processed": False,
        "completed": False,
        "code": "worker_canary_no_work",
    }
    if worker_role is not None and instance_id is not None:
        try:
            canary = run_worker_canary_once(
                worker_role, instance_id, deployed_commit
            )
        except Exception:
            logger.warning("worker_canary_failed")
            canary = {
                "processed": False,
                "completed": False,
                "code": "worker_canary_unavailable",
            }
        if heartbeat_ownership_lost(canary):
            return {
                "processed": bool(voice_processed),
                "voice_maintenance_completed": voice_maintenance_completed,
                "canary_completed": False,
                "generic_job_completed": False,
                "ownership_lost": True,
            }
        canary_processed = bool(canary.get("processed"))
        canary_completed = bool(canary.get("completed"))
        if canary.get("code") == "worker_canary_unavailable":
            logger.warning("worker_canary_unavailable")

    background_processed = run_background_job_once(worker_id)
    return {
        "processed": bool(
            voice_processed or canary_processed or background_processed
        ),
        "voice_maintenance_completed": voice_maintenance_completed,
        "canary_completed": canary_completed,
        "generic_job_completed": bool(background_processed),
        "ownership_lost": False,
    }


def run_worker_iteration_with_canary(
    worker_id, worker_role, instance_id, deployed_commit
):
    context_token = _worker_canary_context.set(
        (worker_role, instance_id, deployed_commit)
    )
    try:
        return run_worker_iteration_details(worker_id)
    finally:
        _worker_canary_context.reset(context_token)


def run_worker_iteration(worker_id):
    return run_worker_iteration_details(worker_id)["processed"]


def run_worker_loop_iteration(
    worker_id, stop_requested, poll_seconds
):
    processed = run_worker_iteration(worker_id)
    if not processed:
        stop_requested.wait(poll_seconds)
    return processed


def generate_worker_instance_id():
    return secrets.token_urlsafe(48)


def run_worker(
    stop_requested=None,
    once=False,
    max_iterations=None,
    monotonic_fn=None,
    instance_id_factory=None,
):
    monotonic_fn = monotonic_fn or time.monotonic
    instance_id_factory = instance_id_factory or generate_worker_instance_id
    stop_requested = stop_requested or threading.Event()

    init_db()
    instance_id = instance_id_factory()
    deployed_commit = resolve_worker_deployed_commit()
    startup = start_worker_heartbeat(
        WORKER_ROLE, instance_id, deployed_commit
    )
    if not startup.get("ok"):
        raise RuntimeError("worker_heartbeat_start_failed")

    logger.info(
        "worker_started role=%s commit_state=%s",
        WORKER_ROLE,
        "known" if deployed_commit else "unknown",
    )
    worker_id = f"{WORKER_ROLE}-{os.getpid()}"
    poll_seconds = get_monitoring_config()["worker_poll_seconds"]

    if once:
        details = run_worker_iteration_with_canary(
            worker_id, WORKER_ROLE, instance_id, deployed_commit
        )
        if details.get("ownership_lost"):
            return stop_for_heartbeat_ownership_loss()
        heartbeat = update_worker_heartbeat(
            WORKER_ROLE,
            instance_id,
            deployed_commit,
            poll_completed=True,
            iteration_succeeded=True,
            voice_maintenance_completed=details["voice_maintenance_completed"],
            generic_job_completed=details["generic_job_completed"],
        )
        if heartbeat_ownership_lost(heartbeat):
            return stop_for_heartbeat_ownership_loss()
        return details["processed"]

    iterations = 0
    last_heartbeat_attempt = None
    pending_voice_maintenance = False
    pending_generic_job = False

    while not stop_requested.is_set():
        try:
            details = run_worker_iteration_with_canary(
                worker_id, WORKER_ROLE, instance_id, deployed_commit
            )
        except Exception:
            logger.warning("worker_iteration_failed")
            monotonic_now = monotonic_fn()
            heartbeat_due = (
                last_heartbeat_attempt is None
                or monotonic_now - last_heartbeat_attempt
                >= WORKER_HEARTBEAT_INTERVAL_SECONDS
            )
            if heartbeat_due:
                last_heartbeat_attempt = monotonic_now
                heartbeat = update_worker_heartbeat(
                    WORKER_ROLE,
                    instance_id,
                    deployed_commit,
                    error_code="worker_iteration_failed",
                )
                if heartbeat_ownership_lost(heartbeat):
                    return stop_for_heartbeat_ownership_loss()
            stop_requested.wait(poll_seconds)
        else:
            if details.get("ownership_lost"):
                return stop_for_heartbeat_ownership_loss()
            pending_voice_maintenance = (
                pending_voice_maintenance
                or details["voice_maintenance_completed"]
            )
            pending_generic_job = (
                pending_generic_job or details["generic_job_completed"]
            )
            monotonic_now = monotonic_fn()
            heartbeat_due = (
                last_heartbeat_attempt is None
                or monotonic_now - last_heartbeat_attempt
                >= WORKER_HEARTBEAT_INTERVAL_SECONDS
            )
            if heartbeat_due:
                last_heartbeat_attempt = monotonic_now
                heartbeat = update_worker_heartbeat(
                    WORKER_ROLE,
                    instance_id,
                    deployed_commit,
                    poll_completed=True,
                    iteration_succeeded=True,
                    voice_maintenance_completed=pending_voice_maintenance,
                    generic_job_completed=pending_generic_job,
                )
                if heartbeat_ownership_lost(heartbeat):
                    return stop_for_heartbeat_ownership_loss()
                if heartbeat.get("ok"):
                    pending_voice_maintenance = False
                    pending_generic_job = False
            if not details["processed"]:
                stop_requested.wait(poll_seconds)

        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            break

    return None


def main():
    parser = argparse.ArgumentParser(description="BusinessBuilder AI background worker")
    parser.add_argument("--once", action="store_true", help="Run one queued job and exit")
    args = parser.parse_args()

    stop_requested = threading.Event()

    def request_stop(signum, frame):
        del signum, frame
        logger.info("worker_stop_requested role=%s", WORKER_ROLE)
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    processed = run_worker(stop_requested=stop_requested, once=args.once)
    if processed == WORKER_OWNERSHIP_LOST:
        return
    if args.once:
        print("processed" if processed else "no queued jobs")
    else:
        logger.info("worker_stopped role=%s", WORKER_ROLE)


if __name__ == "__main__":
    main()
