import argparse
import os
import time

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

    print("BusinessBuilder AI worker started. For local SQLite, run only one worker.")
    poll_seconds = get_monitoring_config()["worker_poll_seconds"]
    try:
        while True:
            processed = run_background_job_once(worker_id)
            if not processed:
                time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("BusinessBuilder AI worker stopped.")


if __name__ == "__main__":
    main()
