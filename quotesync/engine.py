"""
QuoteSync Automation Engine.

Launches a browser, runs one or more carrier adapters against a prospect profile.
The browser runs in 'headed' mode (visible) so you can watch it work and
intervene for CAPTCHAs or MFA prompts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from quotesync.carriers.base import CarrierAdapter
from quotesync.models.prospect import ProspectProfile


async def run_carrier(adapter: CarrierAdapter, profile: ProspectProfile, headed: bool = True) -> None:
    """Run a single carrier adapter against a prospect profile."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        context = await browser.new_context()
        page = await context.new_page()

        print(f"[QuoteSync] Starting: {adapter.name}")
        try:
            await adapter.run(page, profile)
            print(f"[QuoteSync] Completed: {adapter.name}")
        except Exception as e:
            import traceback
            print(f"[QuoteSync] Error with {adapter.name}: {e}")
            traceback.print_exc()
            # Keep browser open for 5 minutes so user can inspect what happened
            print("[QuoteSync] Browser will stay open for 5 minutes — inspect the page, then it will close.")
            await asyncio.sleep(300)
        finally:
            await browser.close()


async def run_carriers(adapters: list[CarrierAdapter], profile: ProspectProfile, headed: bool = True) -> None:
    """Run multiple carrier adapters sequentially against the same profile."""
    for adapter in adapters:
        await run_carrier(adapter, profile, headed=headed)


def load_profile(filepath: str | Path) -> ProspectProfile:
    """Load a prospect profile from a saved JSON file."""
    data = json.loads(Path(filepath).read_text())
    return ProspectProfile(**data)
