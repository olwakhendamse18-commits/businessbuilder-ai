import argparse
import os
import signal
import threading

from app import get_monitoring_config, init_db, run_background_job_once


def main():
    parser = argparse.ArgumentParser(description="BusinessBuilder AI background worker")
    parser.add_argument("--once", action="store_true", help="Run one queued job and exit")
    args = parser.parse_args()

    init_db()
    worker_id = f"businessbuilder-worker-{os.getpid()}"

    if args.once:
        processed = run_background_job_once(worker_id)
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
        processed = run_background_job_once(worker_id)
        if not processed:
            stop_requested.wait(poll_seconds)

    print("BusinessBuilder AI worker stopped.", flush=True)


if __name__ == "__main__":
    main()
