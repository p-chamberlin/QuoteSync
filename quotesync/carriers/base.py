"""
Base carrier adapter — all carrier-specific adapters inherit from this.

Each carrier adapter defines how to:
1. Log in to the portal
2. Navigate to the new quote form
3. Map prospect profile fields to the carrier's form fields
4. Fill in each page/step of the form
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from playwright.async_api import Page

from quotesync.models.prospect import ProspectProfile


class CarrierAdapter(ABC):
    """Base class for all carrier portal adapters."""

    # Human-readable carrier name
    name: str = "Unknown Carrier"

    # Login URL for the carrier portal
    login_url: str = ""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    @abstractmethod
    async def login(self, page: Page) -> None:
        """Log into the carrier portal. Pause for MFA if needed."""

    @abstractmethod
    async def navigate_to_new_quote(self, page: Page) -> None:
        """Navigate from the dashboard to the 'start new quote' form."""

    async def navigate_to_saved_quote(self, page: Page) -> None:
        """Navigate to an in-progress/saved quote for resume mode.

        Default implementation falls back to navigate_to_new_quote.
        Carriers that support saved-quote retrieval should override this.
        """
        await self.navigate_to_new_quote(page)

    @abstractmethod
    async def fill_quote(self, page: Page, profile: ProspectProfile) -> dict | None:
        """Fill in the quote form using the prospect profile data.

        Returns a dict with result data (e.g. {"premium": "$4,775"}) or None.
        """

    async def run(self, page: Page, profile: ProspectProfile) -> None:
        """Full workflow: login → navigate → fill."""
        await page.goto(self.login_url)
        await self.login(page)
        await self.navigate_to_new_quote(page)
        await self.fill_quote(page, profile)
