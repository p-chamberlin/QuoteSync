"""
Example carrier adapter — demonstrates how to build one for a real carrier.

This is a TEMPLATE. To build a real adapter:
1. Copy this file and rename it (e.g., hartford.py, travelers.py)
2. Fill in the login_url, CSS selectors, and form-filling logic
3. Test it step-by-step with `headed=True` so you can watch the browser

The selectors below are PLACEHOLDERS — you'd replace them with the actual
CSS selectors from the carrier's portal after inspecting their HTML.
"""

from __future__ import annotations

from playwright.async_api import Page

from quotesync.carriers.base import CarrierAdapter
from quotesync.models.prospect import ProspectProfile


class ExampleCarrierAdapter(CarrierAdapter):
    name = "Example Carrier"
    login_url = "https://example-carrier-portal.com/login"

    async def login(self, page: Page) -> None:
        # Type username and password into the login form
        await page.fill("#username", self.username)
        await page.fill("#password", self.password)
        await page.click("#login-button")

        # Wait for the dashboard to load (confirms login succeeded)
        await page.wait_for_selector("#dashboard", timeout=30000)

        # If MFA is required, pause so the user can complete it manually:
        # await page.pause()

    async def navigate_to_new_quote(self, page: Page) -> None:
        await page.click("a[href='/new-quote']")
        await page.wait_for_selector("#quote-form", timeout=15000)

    async def fill_quote(self, page: Page, profile: ProspectProfile) -> None:
        # --- Page 1: Business Info ---
        await page.fill("#insured-name", profile.legal_business_name)

        if profile.dba:
            await page.fill("#dba", profile.dba)

        if profile.fein:
            await page.fill("#fein", profile.fein)

        if profile.entity_type:
            await page.select_option("#entity-type", profile.entity_type.value)

        # Mailing address
        addr = profile.mailing_address
        await page.fill("#street", addr.street)
        await page.fill("#city", addr.city)
        await page.fill("#state", addr.state)
        await page.fill("#zip", addr.zip_code)

        # Click 'Next' to go to page 2
        await page.click("#next-button")
        await page.wait_for_selector("#page-2", timeout=15000)

        # --- Page 2: Operations & Revenue ---
        if profile.operations_description:
            await page.fill("#operations", profile.operations_description)

        if profile.gross_revenue_current:
            await page.fill("#revenue", str(int(profile.gross_revenue_current)))

        if profile.total_annual_payroll:
            await page.fill("#payroll", str(int(profile.total_annual_payroll)))

        if profile.num_employees_ft:
            await page.fill("#employees", str(profile.num_employees_ft))

        # Continue to next page...
        await page.click("#next-button")

        # --- Add more pages as needed ---
        # Each carrier's form has different pages and fields.
        # Inspect the HTML, find the selectors, and map them here.
