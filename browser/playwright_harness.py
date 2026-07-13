from app import get_browser_config, validate_browser_url


class BrowserHarness:
    def __init__(self, task):
        self.task = task
        try:
            import json
            self.allowed_domains = json.loads(task[8] or "[]")
        except Exception:
            self.allowed_domains = []
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed. Run pip install -r requirements.txt, then python -m playwright install chromium.") from exc
        config = get_browser_config()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=config["headless"],
            downloads_path=None
        )
        self.context = await self.browser.new_context(
            viewport={"width": config["viewport_width"], "height": config["viewport_height"]},
            accept_downloads=False,
            ignore_https_errors=False,
            storage_state={"cookies": [], "origins": []}
        )
        self.context.set_default_timeout(12_000)
        self.page = await self.context.new_page()
        self.page.on("download", lambda download: download.cancel())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def goto(self, url, allowed_domains):
        ok, safe_url, _, reason = validate_browser_url(url, allowed_domains)
        if not ok:
            raise RuntimeError(reason)
        await self.page.goto(safe_url, wait_until="domcontentloaded", timeout=20_000)
        ok, _, _, reason = validate_browser_url(self.page.url, allowed_domains)
        if not ok:
            raise RuntimeError(f"Navigation blocked after redirect: {reason}")
        return self.page.url

    async def screenshot(self):
        return await self.page.screenshot(full_page=False, type="png")

    async def execute_action(self, action):
        action_type = (action or {}).get("type")
        if action_type == "wait":
            await self.page.wait_for_timeout(int(action.get("ms", 700)))
        elif action_type == "scroll":
            dy = int(action.get("dy", action.get("delta_y", 500)) or 500)
            await self.page.mouse.wheel(0, dy)
        elif action_type == "move":
            await self.page.mouse.move(float(action.get("x", 10)), float(action.get("y", 10)))
        elif action_type in {"click", "double_click"}:
            method = self.page.mouse.dblclick if action_type == "double_click" else self.page.mouse.click
            await method(float(action.get("x", 10)), float(action.get("y", 10)))
        elif action_type == "keypress":
            await self.page.keyboard.press(str(action.get("key", "Tab"))[:40])
        elif action_type == "navigate":
            await self.goto(str(action.get("url", "")), self.allowed_domains)
        else:
            raise RuntimeError(f"Unsupported or approval-required action: {action_type}")
        return self.page.url
