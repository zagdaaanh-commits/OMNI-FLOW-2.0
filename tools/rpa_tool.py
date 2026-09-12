from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
    HAS_PLAYWRIGHT = True
except (ImportError, Exception):
    HAS_PLAYWRIGHT = False
    Browser = Any
    BrowserContext = Any
    Page = Any
    async_playwright = None


class RPATool:
    """Playwright session manager for sites where an approved browser workflow is used.

    Keep selectors in environment variables or per-platform adapters. Do not hard-code
    credentials. Use this only where the platform permits automation and the account owner
    has approved the workflow.
    """

    def __init__(self) -> None:
        self.headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
        self.storage_state = os.getenv("PLAYWRIGHT_STORAGE_STATE", "")
        self.base_url = os.getenv("RPA_BASE_URL", "")

    @asynccontextmanager
    async def session(self):
        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(headless=self.headless)
            context_kwargs: Dict[str, Any] = {}
            if self.storage_state and os.path.exists(self.storage_state):
                context_kwargs["storage_state"] = self.storage_state
            context: BrowserContext = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            try:
                yield page
            finally:
                await context.close()
                await browser.close()

    async def open(self, page: Page, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded")

    async def fill_and_click(self, page: Page, input_selector: str, text: str, button_selector: str) -> None:
        await page.locator(input_selector).fill(text)
        await page.locator(button_selector).click()
