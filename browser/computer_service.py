import base64
import os

from openai import OpenAI

from app import get_browser_config
from browser.prompt_policy import BROWSER_AGENT_INSTRUCTIONS


def computer_model_available():
    config = get_browser_config()
    return bool(os.getenv("OPENAI_API_KEY") and config["computer_model"])


def create_computer_response(objective, screenshot_bytes, previous_response_id=None):
    """Create a Responses API computer-tool request.

    Automated tests should mock this function. It intentionally avoids logging
    screenshots, response bodies, headers, cookies, or provider IDs.
    """
    config = get_browser_config()
    if not computer_model_available():
        return None, "OpenAI computer model is not configured."
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    screenshot = base64.b64encode(screenshot_bytes).decode("ascii")
    content = [
        {"type": "input_text", "text": f"{BROWSER_AGENT_INSTRUCTIONS}\n\nTask objective:\n{objective}"},
        {
            "type": "input_image",
            "image_url": f"data:image/png;base64,{screenshot}",
            "detail": "low"
        }
    ]
    try:
        kwargs = {
            "model": config["computer_model"],
            "tools": [{"type": "computer"}],
            "input": [{"role": "user", "content": content}]
        }
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        response = client.responses.create(**kwargs)
    except Exception:
        return None, "OpenAI computer-use request failed."
    return response, None


def extract_computer_actions(response):
    actions = []
    for item in getattr(response, "output", []) or []:
        item_type = getattr(item, "type", "") or (item.get("type") if isinstance(item, dict) else "")
        if item_type != "computer_call":
            continue
        raw_actions = getattr(item, "actions", None) or (item.get("actions") if isinstance(item, dict) else None) or []
        for raw in raw_actions:
            if isinstance(raw, dict):
                actions.append(raw)
            else:
                actions.append({key: getattr(raw, key) for key in dir(raw) if not key.startswith("_") and key in {"type", "x", "y", "text", "button", "url"}})
    return actions

