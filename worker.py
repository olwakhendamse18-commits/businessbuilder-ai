import argparse
import logging
import os
import signal
import threading

from app import (
    get_monitoring_config,
    init_db,
    run_background_job_once,
    run_voice_maintenance_once,
)


logger = logging.getLogger(__name__)


def run_worker_iteration(worker_id):
    voice_processed = False
    try:
        voice_counts = run_voice_maintenance_once()
        voice_processed = bool(voice_counts["processed"])
    except Exception:
        logger.warning(
            "Voice maintenance iteration failed safely. "
            "Generic background processing will continue."
        )
    background_processed = run_background_job_once(worker_id)
    return voice_processed or background_processed


def run_worker_loop_iteration(
    worker_id, stop_requested, poll_seconds
):
    processed = run_worker_iteration(worker_id)
    if not processed:
        stop_requested.wait(poll_seconds)
    return processed


def main():
    parser = argparse.ArgumentParser(description="BusinessBuilder AI background worker")
    parser.add_argument("--once", action="store_true", help="Run one queued job and exit")
    args = parser.parse_args()

    init_db()
    worker_id = f"businessbuilder-worker-{os.getpid()}"

    if args.once:
        processed = run_worker_iteration(worker_id)
        print("processed" if processed else "no queued jobs")
        return

    stop_requested = threading.Event()

    def request_stop(signum, frame):
        del signum, frame
        print("BusinessBuilder AI worker stopping after the current job.", flush=True)
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    print("BusinessBuilder AI worker started. For local SQLite, run only one worker.", flush=True)
    poll_seconds = get_monitoring_config()["worker_poll_seconds"]
    while not stop_requested.is_set():
        run_worker_loop_iteration(
            worker_id, stop_requested, poll_seconds
        )

    print("BusinessBuilder AI worker stopped.", flush=True)


if __name__ == "__main__":
    main()
