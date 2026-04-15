"""
QuoteSync Automation Engine.

Launches a browser, runs one or more carrier adapters against a prospect profile.
The browser runs in 'headed' mode (visible) so you can watch it work and
intervene for CAPTCHAs or MFA prompts.

Session persistence
-------------------
Each carrier gets its own Chrome profile directory under profiles/<slug>/.
Playwright opens the browser with that profile so cookies and local storage
persist between runs exactly like a real browser — log in once (including any
MFA prompt) and you stay logged in until the site's own session expires.

Profiles are gitignored and stored only on your local machine.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

from quotesync.carriers.base import CarrierAdapter
from quotesync.models.prospect import ProspectProfile

PROFILES_DIR = Path(__file__).parent.parent / "profiles"


def _profile_path(adapter: CarrierAdapter) -> Path:
    """Return the Chrome profile directory for this adapter."""
    PROFILES_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", adapter.name.lower()).strip("_")
    profile = PROFILES_DIR / slug
    profile.mkdir(exist_ok=True)
    return profile


async def run_carrier(adapter: CarrierAdapter, profile: ProspectProfile, headed: bool = True) -> dict | None:
    """Run a single carrier adapter against a prospect profile.

    Opens a persistent Chrome profile so the session survives between runs.
    If the site requires login (session expired or first run), adapter.login()
    is called; otherwise it is skipped.
    """
    async with async_playwright() as pw:
        profile_dir = _profile_path(adapter)
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=not headed,
        )
        page = await context.new_page()

        print(f"[QuoteSync] Starting: {adapter.name}")

        try:
            # Navigate to the login URL and wait for the OIDC redirect chain to fully settle.
            # The chain passes through /auth/realms/... URLs before landing on either the
            # portal dashboard (session valid) or the login form (session expired).
            # A short wait_for_load_state can time out mid-redirect, leaving page.url still
            # at an /auth/ step and causing a false "login required" detection.
            await page.goto(adapter.login_url)
            try:
                # Wait until the browser is no longer in the OIDC redirect chain
                await page.wait_for_url(
                    lambda url: "/auth/realms/" not in url and "/signin-oidc" not in url,
                    timeout=30000,
                )
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            url = page.url.lower()
            on_login_page = any(x in url for x in ["login", "signin", "account/login", "sso"])

            if on_login_page:
                print(f"[QuoteSync] Logging in to {adapter.name}...")
                await adapter.login(page)
                print(f"[QuoteSync] Logged in — profile saved to profiles/{profile_dir.name}/")
            else:
                print(f"[QuoteSync] Session valid — skipping login for {adapter.name}")

            await adapter.navigate_to_new_quote(page)
            result = await adapter.fill_quote(page, profile)
            print(f"[QuoteSync] Completed: {adapter.name}")
            return result or {}

        except Exception as e:
            import traceback
            print(f"[QuoteSync] Error with {adapter.name}: {e}")
            traceback.print_exc()
            print("[QuoteSync] Browser will stay open for 5 minutes — inspect the page, then it will close.")
            await asyncio.sleep(300)
        finally:
            await context.close()


async def run_carriers(adapters: list[CarrierAdapter], profile: ProspectProfile, headed: bool = True) -> None:
    """Run multiple carrier adapters sequentially against the same profile."""
    for adapter in adapters:
        await run_carrier(adapter, profile, headed=headed)


def load_profile(filepath: str | Path) -> ProspectProfile:
    """Load a prospect profile from a saved JSON file."""
    data = json.loads(Path(filepath).read_text())
    return ProspectProfile(**data)
