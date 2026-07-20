import os
import secrets
from pathlib import Path

from app import get_browser_config, record_browser_artifact


def browser_artifact_dir(user_id, browser_task_id):
    root = Path(get_browser_config()["storage_dir"]).resolve()
    path = root / f"user-{int(user_id)}" / f"task-{int(browser_task_id)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_screenshot_bytes(browser_task_id, browser_session_id, user_id, data, sequence_number, artifact_type="screenshot"):
    directory = browser_artifact_dir(user_id, browser_task_id)
    filename = f"{sequence_number:04d}-{secrets.token_urlsafe(18)}.png"
    path = directory / filename
    path.write_bytes(data)
    artifact_root = Path(get_browser_config()["storage_dir"]).resolve()
    if os.path.commonpath([os.fspath(artifact_root), os.fspath(path.resolve())]) != os.fspath(artifact_root):
        raise RuntimeError("Artifact path escaped browser storage root.")
    return record_browser_artifact(
        browser_task_id, browser_session_id, user_id,
        artifact_type, os.fspath(path.resolve()), sequence_number
    )
