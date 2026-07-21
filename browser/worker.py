import argparse
import asyncio
import time

from app import (
    claim_next_browser_task, create_browser_session_record, finish_browser_session_record,
    get_browser_config, record_browser_action, save_browser_task_completion,
    update_browser_task_status, validate_browser_action, recover_stale_browser_tasks,
    cleanup_expired_browser_artifacts, create_agent_approval
)
from browser.artifact_store import save_screenshot_bytes
from browser.computer_service import create_computer_response, extract_computer_actions, computer_model_available
from browser.playwright_harness import BrowserHarness


async def run_browser_task(task):
    config = get_browser_config()
    allowed_domains = __import__("json").loads(task[8] or "[]")
    if not config["enabled"]:
        save_browser_task_completion(task, "failed", "Browser control is disabled.", "Browser control disabled.")
        return
    if not computer_model_available():
        save_browser_task_completion(task, "failed", "Browser control is missing OPENAI_COMPUTER_MODEL or OPENAI_API_KEY.", "Computer model unavailable.")
        return

    session_id = None
    action_count = 0
    screenshot_count = 0
    start = time.monotonic()
    try:
        update_browser_task_status(task[0], "running", "Starting isolated Chromium context.")
        session_id = create_browser_session_record(task)
        async with BrowserHarness(task) as browser:
            current_url = await browser.goto(task[7], allowed_domains)
            screenshot = await browser.screenshot()
            screenshot_count += 1
            save_screenshot_bytes(task[0], session_id, task[1], screenshot, screenshot_count)
            previous_response_id = None
            final_summary = "Browser inspection completed safely."

            while action_count < config["max_actions"]:
                if time.monotonic() - start > config["task_timeout_seconds"]:
                    save_browser_task_completion(task, "timed_out", "Browser task timed out before completion.", "Task timeout.")
                    return
                response, error = create_computer_response(task[6], screenshot, previous_response_id)
                if error:
                    save_browser_task_completion(task, "failed", error, error)
                    return
                previous_response_id = getattr(response, "id", None)
                actions = extract_computer_actions(response)
                if not actions:
                    save_browser_task_completion(task, "completed", final_summary)
                    return
                for action in actions:
                    action_count += 1
                    validation = validate_browser_action(task, action, current_url)
                    if validation.get("blocked"):
                        record_browser_action(task[0], session_id, task[1], action_count, action, validation, "blocked", current_url)
                        save_browser_task_completion(task, "blocked", validation["reason"], validation["reason"])
                        return
                    if validation.get("requires_approval") or validation.get("requires_user_handoff"):
                        approval_id = create_agent_approval(
                            task[1], task[2], task[4], "browser_action", validation.get("risk_level", "medium"),
                            {
                                "browser_task_id": task[0],
                                "objective": task[6],
                                "current_url": current_url,
                                "proposed_action": action.get("type", "action"),
                                "action_summary": "A browser action would interact with a form, handoff, or domain boundary.",
                                "data_shared": "Sensitive values are not stored. Review before approving."
                            },
                            validation["reason"]
                        )
                        validation["approval_id"] = approval_id
                        record_browser_action(task[0], session_id, task[1], action_count, action, validation, "waiting_for_user", current_url)
                        save_browser_task_completion(task, "waiting_for_user", validation["reason"])
                        return
                    current_url = await browser.execute_action(action)
                    record_browser_action(task[0], session_id, task[1], action_count, action, validation, "executed", current_url)
                    screenshot = await browser.screenshot()
                    screenshot_count += 1
                    if screenshot_count <= config["max_screenshots"]:
                        save_screenshot_bytes(task[0], session_id, task[1], screenshot, screenshot_count, "final_screenshot" if action_count >= config["max_actions"] else "screenshot")
                if screenshot_count >= config["max_screenshots"]:
                    final_summary = "Browser inspection stopped after reaching the screenshot limit."
                    save_browser_task_completion(task, "completed", final_summary)
                    return
            save_browser_task_completion(task, "completed", "Browser inspection stopped after reaching the safe action limit.")
    except Exception as exc:
        save_browser_task_completion(task, "failed", "Browser task failed safely. Other BusinessBuilder features were not affected.", str(exc)[:500])
    finally:
        if session_id:
            finish_browser_session_record(session_id, "ended", "task_finished")


async def run_once():
    recover_stale_browser_tasks()
    cleanup_expired_browser_artifacts()
    task = claim_next_browser_task()
    if not task:
        return False
    await run_browser_task(task)
    return True


def main():
    parser = argparse.ArgumentParser(description="BusinessBuilder AI restricted browser worker")
    parser.add_argument("--once", action="store_true", help="Claim at most one queued browser task and exit.")
    args = parser.parse_args()
    config = get_browser_config()
    if not config["enabled"]:
        print("Browser control disabled. Set BROWSER_CONTROL_ENABLED=true for local testing.")
        return
    if args.once:
        asyncio.run(run_once())
        return
    while True:
        asyncio.run(run_once())
        time.sleep(config["worker_poll_seconds"])


if __name__ == "__main__":
    main()
