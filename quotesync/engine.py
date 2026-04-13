"""
QuoteSync Automation Engine.

Launches a browser, runs one or more carrier adapters against a prospect profile.
The browser runs in 'headed' mode (visible) so you can watch it work and
intervene for CAPTCHAs or MFA prompts.

Session persistence
-------------------
After a successful login the browser's storage state (cookies, localStorage, etc.)
is saved to sessions/<carrier_slug>.json.  On the next run the saved state is
loaded and login is skipped if the session is still valid.  This avoids repeated
MFA prompts — you authenticate once and the session is reused until it expires.

Sessions are gitignored and stored only on your local machine.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

from quotesync.carriers.base import CarrierAdapter
from quotesync.models.prospect import ProspectProfile

SESSIONS_DIR = Path(__file__).parent.parent / "sessions"


def _session_path(adapter: CarrierAdapter) -> Path:
    """Return the path to the saved session file for this adapter."""
    SESSIONS_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", adapter.name.lower()).strip("_")
    return SESSIONS_DIR / f"{slug}.json"


async def _try_restore_session(context, adapter: CarrierAdapter, page) -> bool:
    """Navigate to the adapter's start URL and check if the session is still valid.

    Returns True if already authenticated (login page was not shown), False otherwise.
    """
    await page.goto(adapter.login_url)
    # Allow redirects to settle
    try:
        await page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass

    url = page.url.lower()
    # If we ended up on a login/auth page the session has expired
    on_login_page = any(x in url for x in ["login", "signin", "account/login", "/auth", "sso"])
    return not on_login_page


async def run_carrier(adapter: CarrierAdapter, profile: ProspectProfile, headed: bool = True) -> None:
    """Run a single carrier adapter against a prospect profile.

    Attempts to reuse a saved browser session to skip login.  Falls back to
    a full login if the session is missing or expired, then saves the new session.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)

        session_file = _session_path(adapter)

        # Try loading saved session first
        if session_file.exists():
            print(f"[QuoteSync] Loading saved session for {adapter.name}...")
            context = await browser.new_context(storage_state=str(session_file))
            page = await context.new_page()
            already_logged_in = await _try_restore_session(context, adapter, page)
        else:
            context = await browser.new_context()
            page = await context.new_page()
            already_logged_in = False
            # Still need to navigate to login_url for the login flow
            await page.goto(adapter.login_url)

        print(f"[QuoteSync] Starting: {adapter.name}")

        try:
            if already_logged_in:
                print(f"[QuoteSync] Session valid — skipping login for {adapter.name}")
            else:
                if session_file.exists():
                    print(f"[QuoteSync] Saved session expired — logging in again...")
                await adapter.login(page)
                # Save session immediately after successful login
                await context.storage_state(path=str(session_file))
                print(f"[QuoteSync] Session saved to {session_file.name}")

            await adapter.navigate_to_new_quote(page)
            await adapter.fill_quote(page, profile)
            print(f"[QuoteSync] Completed: {adapter.name}")

            # Refresh session file after a successful run (keeps it alive)
            await context.storage_state(path=str(session_file))

        except Exception as e:
            import traceback
            print(f"[QuoteSync] Error with {adapter.name}: {e}")
            traceback.print_exc()
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
