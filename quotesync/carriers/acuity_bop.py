"""
Acuity Insurance — Bis-Pak (BOP) Quote Adapter.

Automates the Business Owner Policy quote flow on Acuity's iRating portal.
Portal URL: https://www.acuity.com/irating/servlet/MainServlet
Login URL: https://namid.acuity.com (Novell/NetIQ SSO)

Flow:
1. Login at namid.acuity.com (SSO — may require MFA)
2. Agent Center dashboard → open iRating window
3. MainServlet: Policy Type, Line, Exposure State, Term
4. Nature of Business: search class code, select from results
5. Class-specific dynamic questions (variable by class — pause for user)
6. Named Insured: name, address, entity type
7. Save New Copy dialog (checkpoint)
8. D&B search (skip or select match)
9. Line Selection: Program, Enhancements, Liability, Medical Expenses
10. Location modal: address, territory (ZIP-based), protection class (locked)
11. Building modal: plan, coverage, construction, roof, valuation, occupancy
12. Add Liability Class modal
13. Bis-Pak Unit Options / Policy Options
14. Additional Interests (skip)
15. Additional Info: Prior Insurance, Loss History, General Info (max stories)
16. Premium Summary (STOP — never click Bind)
"""

from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal
from typing import Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from quotesync.carriers.base import CarrierAdapter
from quotesync.models.prospect import (
    ConstructionType,
    EntityType,
    InsuredType,
    LineOfBusiness,
    ProspectProfile,
    RiskClass,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

# Our EntityType enum → Acuity's "Business Status" dropdown text.
# Acuity has 13 options; our model has 19.  Unmapped values fall to "Other".
ACUITY_ENTITY_MAP: dict[str, str] = {
    EntityType.INDIVIDUAL.value: "Individual",
    EntityType.CORPORATION.value: "Corporation",
    EntityType.LLC.value: "LLC",
    EntityType.LLP.value: "LLP",
    EntityType.PARTNERSHIP.value: "Partnership",
    EntityType.LIMITED_PARTNERSHIP.value: "Limited Partnership",
    EntityType.JOINT_VENTURE.value: "Joint Venture",
    EntityType.TRUST.value: "Trust",
    EntityType.TRUST_OR_ESTATE.value: "Trust",
    EntityType.ASSOCIATION.value: "Association",
    EntityType.RELIGIOUS_ORGANIZATION.value: "Other",
    EntityType.GOVERNMENTAL_ENTITY.value: "Governmental Subdivision",
    EntityType.EXECUTOR_OR_TRUSTEE.value: "Trust",
    EntityType.ORGANIZATION.value: "Corporation",
    EntityType.OTHER.value: "Other",
}

# Our RiskClass enum → Acuity's "Plan" / Business Type dropdown.
# Acuity has ~11 plan options; we map the 4 risk classes to their closest match.
# The actual plan is usually driven by the class code, not this dropdown.
ACUITY_PLAN_MAP: dict[str, str] = {
    RiskClass.CONTRACTOR.value: "Contractor",
    RiskClass.RETAIL.value: "Mercantile",
    RiskClass.PROFESSIONAL.value: "Office",
    RiskClass.MANUFACTURER.value: "Wholesale",
}

# Our ConstructionType enum → Acuity's Construction dropdown text.
ACUITY_CONSTRUCTION_MAP: dict[str, str] = {
    ConstructionType.FRAME.value: "Frame",
    ConstructionType.FRAME_WITH_MASONRY_VENEER.value: "Frame with Masonry Veneer",
    ConstructionType.JOISTED_MASONRY.value: "Joisted Masonry",
    ConstructionType.NON_COMBUSTIBLE.value: "Non-Combustible",
    ConstructionType.FIRE_RESISTIVE.value: "Fire Resistive",
    ConstructionType.MODIFIED_FIRE_RESISTIVE.value: "Modified Fire Resistive",
}

# Acuity's prior carrier dropdown — used for fuzzy matching.
# If the prospect's carrier isn't found, we fall back to "Other".
ACUITY_CARRIER_LIST = [
    "No coverage",
    "Acuity - A Mutual Insurance Company",
    "Agency Insurance of Maryland",
    "Allegany Insurance",
    "American Family",
    "Arbella Insurance Group",
    "Auto-Owners",
    "Barton Mutual",
    "Berkshire - Homestate",
    "Brethren Mutual",
    "Bristol West",
    "Celina Insurance",
    "Central - National",
    "Central Mutual",
    "Cincinnati Insurance",
    "Citizens Property & Casualty (FL)",
    "Columbia Insurance Group",
    "Country Companies",
    "Donegal Mutual",
    "Employers Mutual Casualty Insurance",
    "Enumclaw Insurance Group",
    "Erie Insurance",
    "Farm Bureau Property & Casualty",
    "Farm Bureau of Idaho",
    "Farmers Insurance Group",
    "Forge Group, Inc",
    "Georgia Farm Bureau",
    "Germantown Mutual",
    "Grange Insurance (WA)",
    "Grange Mutual Casualty (OH)",
    "Grinnell Re",
    "GuideOne Mutual",
    "IMT Insurance Company",
    "Infinity Insurance (a Kemper Company)",
    "Kansas Fair Plan",
    "Kemper Corporation",
    "Kentucky Farm Bureau",
    "Liberty Mutual",
    "Louisiana Citizens Property Ins Corp",
    "Main Street America Group (an American Family Company)",
    "Maryland Automobile Insurance Fund",
    "Merchants Mutual",
    "Mercury Insurance",
    "Metropolitan Property & Liability",
    "Motorists Mutual",
    "National General Group (an Allstate Company)",
    "Nationwide",
    "New Jersey Manufacturers",
    "Next Insurance",
    "North Star",
    "Ontario Insurance",
    "Pie Group Holdings",
    "Progressive",
    "Rural Mutual Insurance",
    "Safepoint Insurance",
    "Safety Insurance",
    "State Auto Insurance",
    "State Farm",
    "Stillwater (Wt Holdings Inc.)",
    "Tennessee Farmers Mutual",
    "The American Road Insurance Company",
    "Topa Insurance Group",
    "Travelers Insurance Group",
    "Tuscarora Wayne",
    "United Fire",
    "West Bend",
    "Westfield",
    "Other",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fuzzy_match_carrier(name: str) -> str:
    """Find the closest match in Acuity's carrier dropdown list."""
    if not name:
        return "No coverage"
    name_lower = name.lower().strip()
    for carrier in ACUITY_CARRIER_LIST:
        if name_lower in carrier.lower() or carrier.lower() in name_lower:
            return carrier
    # Try keyword matching (first word of carrier name)
    first_word = name_lower.split()[0] if name_lower.split() else ""
    for carrier in ACUITY_CARRIER_LIST:
        if first_word and first_word in carrier.lower():
            return carrier
    logger.warning("Prior carrier '%s' not in Acuity list — using 'Other'", name)
    return "Other"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class AcuityBopAdapter(CarrierAdapter):
    """Adapter for Acuity Insurance Bis-Pak (BOP) quotes."""

    name = "Acuity Insurance — Bis-Pak (BOP)"
    login_url = "https://namid.acuity.com/nidp/idff/sso?id=SF&sid=0&option=credential&sid=0&target=https%3A%2F%2Fwww.acuity.com%2Facuityweb%2Fagents%2Fagentcenter.xhtml"

    # ----- login -----

    async def login(self, page: Page) -> None:
        """Log into Acuity via Novell/NetIQ SSO at namid.acuity.com."""
        logger.info("[Acuity] Navigating to login...")
        await page.wait_for_selector("input[type='text'], input[name='Ecom_User_ID']", timeout=30_000)

        # Fill username / password
        user_input = page.locator("input[name='Ecom_User_ID'], input[type='text']").first
        await user_input.fill(self.username)
        pass_input = page.locator("input[name='Ecom_Password'], input[type='password']").first
        await pass_input.fill(self.password)

        # Click Login
        await page.click("input[type='submit'], button:has-text('Login')")

        # Wait for Agent Center dashboard (may pause for MFA — give 120s)
        try:
            await page.wait_for_url("**/agentcenter**", timeout=120_000)
        except PlaywrightTimeout:
            logger.warning("[Acuity] Login redirect timed out — user may need to complete MFA manually.")
            await page.wait_for_url("**/agentcenter**", timeout=300_000)

        logger.info("[Acuity] Logged in successfully.")

    # ----- navigate to new quote -----

    async def navigate_to_new_quote(self, page: Page) -> None:
        """From Agent Center, open iRating and set up a new Bis-Pak quote."""
        logger.info("[Acuity] Navigating to iRating...")

        # The iRating link opens a new window/popup.  Click it and capture.
        async with page.context.expect_page() as new_page_info:
            await page.click("a:has-text('iRating'), a[href*='irating']")
        irating_page = await new_page_info.value
        await irating_page.wait_for_load_state("domcontentloaded")

        # Now we're on MainServlet — the policy setup dropdowns
        await irating_page.wait_for_url("**/MainServlet**", timeout=30_000)
        logger.info("[Acuity] iRating window opened.")

        # Store reference — all subsequent work happens on this page
        self._page = irating_page

    async def _setup_policy_dropdowns(self, page: Page, profile: ProspectProfile) -> None:
        """Fill the cascading Policy Type / Line / State / Term dropdowns."""
        logger.info("[Acuity] Setting up policy dropdowns...")

        # Policy Type → "Bis-Pak"
        await page.select_option("select[name*='policyType'], select[name*='PolicyType']", label="Bis-Pak")
        await page.wait_for_timeout(1000)

        # Line → "BOP" (becomes available after Policy Type)
        await page.select_option("select[name*='line'], select[name*='Line']", label="BOP")
        await page.wait_for_timeout(1000)

        # Exposure State — use mailing address state
        state = profile.mailing_address.state or "ME"
        await page.select_option("select[name*='state'], select[name*='State']", label=state)
        await page.wait_for_timeout(1000)

        # Term — default 12 months
        await page.select_option("select[name*='term'], select[name*='Term']", label="12")
        await page.wait_for_timeout(500)

        # Click Next / Continue to go to Nature of Business
        await page.click("input[value='Next'], button:has-text('Next')")
        await page.wait_for_timeout(3000)
        logger.info("[Acuity] Policy dropdowns set.")

    # ----- fill_quote (master orchestrator) -----

    async def fill_quote(self, page: Page, profile: ProspectProfile) -> None:
        """Fill the entire Bis-Pak quote form, stopping at Premium Summary."""
        # Use the iRating popup page if available
        p = getattr(self, "_page", page)

        await self._setup_policy_dropdowns(p, profile)
        await self._fill_nature_of_business(p, profile)
        await self._fill_named_insured(p, profile)
        await self._handle_save_dialog(p, profile)
        await self._handle_dun_bradstreet(p)
        await self._fill_line_selection(p, profile)
        await self._fill_location(p, profile)
        await self._fill_building(p, profile)
        await self._fill_unit_options(p)
        await self._fill_policy_options(p)
        await self._fill_additional_interests(p)
        await self._fill_additional_info(p, profile)
        await self._fill_general_info(p, profile)
        premium = await self._scrape_premium(p)

        logger.info("[Acuity] === QUOTE COMPLETE ===")
        logger.info("[Acuity] Premium: $%s", premium)
        logger.info("[Acuity] STOPPED before bind. Agent must review manually.")

    # ----- Page: Nature of Business -----

    async def _fill_nature_of_business(self, page: Page, profile: ProspectProfile) -> None:
        """Search for class code on the Nature of Business page."""
        logger.info("[Acuity] Filling Nature of Business...")

        # Business Type dropdown — map from risk_class
        if profile.risk_class:
            btype = ACUITY_PLAN_MAP.get(profile.risk_class.value, "Contractor")
            try:
                await page.select_option("select[name*='businessType'], select[name*='BusinessType']", label=btype)
            except Exception:
                logger.warning("[Acuity] Could not set Business Type to '%s'", btype)

        # Search by operations description or SIC/NAICS code
        search_term = profile.sic_naics_code or profile.operations_description or ""
        if search_term:
            search_input = page.locator("input[name*='search'], input[name*='Search'], input[type='text']").first
            await search_input.fill(search_term)
            await page.click("input[value='Search'], button:has-text('Search')")
            await page.wait_for_timeout(3000)

            # Results appear in a table — click the first matching row
            # The user should verify this is the correct class code
            first_result = page.locator("table a, table tr td a").first
            try:
                await first_result.click(timeout=10_000)
                logger.info("[Acuity] Selected first class code result for '%s'", search_term)
            except PlaywrightTimeout:
                logger.warning("[Acuity] No search results found for '%s' — user must select class manually.", search_term)
                # Pause and wait for user to select a class code
                await page.wait_for_timeout(30_000)

        # After class selection, dynamic class-specific questions may appear.
        # These vary by class code and can't be fully automated.
        # We fill what we can and pause for the user to handle the rest.
        await self._handle_class_questions(page, profile)

        # Click Next to proceed to Named Insured
        await page.click("input[value='Next'], button:has-text('Next')")
        await page.wait_for_timeout(3000)
        logger.info("[Acuity] Nature of Business complete.")

    async def _handle_class_questions(self, page: Page, profile: ProspectProfile) -> None:
        """Try to answer dynamic class-specific questions, pause for unknowns."""
        # Check if there are any visible question elements
        questions = page.locator("select, input[type='radio'], input[type='checkbox']")
        count = await questions.count()
        if count == 0:
            return

        logger.info("[Acuity] Found %d class-specific question elements. Attempting auto-fill...", count)

        # Try to answer common patterns:
        # - "Residential framing" / "Roofing" checkboxes for carpentry classes
        # - "% subcontracted" for contractor classes
        # These are best-effort; user reviews in headed mode.

        # Look for subcontractor-related questions
        if profile.uses_subcontractors is not None:
            sub_labels = page.locator("text=/[Ss]ubcontract/")
            if await sub_labels.count() > 0:
                logger.info("[Acuity] Found subcontractor question — leaving for user review.")

        # Wait a beat for user to review/fill any remaining questions
        await page.wait_for_timeout(5000)

    # ----- Page: Named Insured -----

    async def _fill_named_insured(self, page: Page, profile: ProspectProfile) -> None:
        """Fill the Named Insured and address page."""
        logger.info("[Acuity] Filling Named Insured...")

        # Insured name — Acuity uses a single name field for businesses
        name = profile.legal_business_name
        if not name and profile.first_name:
            name = f"{profile.first_name} {profile.last_name}".strip()

        name_input = page.locator("input[name*='nsuredName'], input[name*='Name'], input[id*='name']").first
        try:
            await name_input.fill(name)
        except Exception:
            # Try broader selector
            inputs = page.locator("input[type='text']")
            if await inputs.count() > 0:
                await inputs.first.fill(name)

        # Address fields
        addr = profile.mailing_address
        if addr.street:
            addr_input = page.locator("input[name*='ddress'], input[name*='street'], input[id*='addr']").first
            try:
                await addr_input.fill(addr.street)
            except Exception:
                pass

        if addr.city:
            city_input = page.locator("input[name*='ity'], input[id*='city']").first
            try:
                await city_input.fill(addr.city)
            except Exception:
                pass

        if addr.state:
            try:
                await page.select_option("select[name*='tate'], select[id*='state']", label=addr.state)
            except Exception:
                pass

        if addr.zip_code:
            zip_input = page.locator("input[name*='ip'], input[id*='zip']").first
            try:
                await zip_input.fill(addr.zip_code)
            except Exception:
                pass

        # Business Status (Entity Type)
        if profile.entity_type:
            entity_text = ACUITY_ENTITY_MAP.get(profile.entity_type.value, "Other")
            try:
                await page.select_option("select[name*='usiness'], select[name*='entity'], select[name*='status']", label=entity_text)
            except Exception:
                logger.warning("[Acuity] Could not set Business Status to '%s'", entity_text)

        # Click Next
        await page.click("input[value='Next'], button:has-text('Next')")
        await page.wait_for_timeout(3000)
        logger.info("[Acuity] Named Insured complete.")

    # ----- Save New Copy dialog -----

    async def _handle_save_dialog(self, page: Page, profile: ProspectProfile) -> None:
        """Handle the 'Save New Copy' dialog that appears after Named Insured."""
        logger.info("[Acuity] Checking for Save Name dialog...")
        try:
            save_input = page.locator("input[name*='aveName'], input[name*='quoteName'], input[id*='save']")
            await save_input.wait_for(timeout=5000)
            # Auto-generate a save name from the insured name + date
            save_name = f"{profile.legal_business_name or profile.last_name} BOP {date.today().isoformat()}"
            await save_input.fill(save_name)
            await page.click("input[value='Save'], button:has-text('Save'), input[value='OK']")
            await page.wait_for_timeout(3000)
            logger.info("[Acuity] Quote saved as '%s'", save_name)
        except PlaywrightTimeout:
            logger.info("[Acuity] No Save dialog appeared — continuing.")

    # ----- D&B Search -----

    async def _handle_dun_bradstreet(self, page: Page) -> None:
        """Handle the D&B search results page — skip or let it auto-match."""
        logger.info("[Acuity] Checking for D&B search results...")
        try:
            # Look for D&B results table or skip button
            skip_btn = page.locator("input[value*='Skip'], input[value*='None'], button:has-text('Skip'), button:has-text('None of the Above')")
            await skip_btn.wait_for(timeout=5000)
            await skip_btn.click()
            await page.wait_for_timeout(2000)
            logger.info("[Acuity] Skipped D&B matching.")
        except PlaywrightTimeout:
            # D&B page may not appear, or may auto-advance
            logger.info("[Acuity] No D&B page detected — continuing.")

    # ----- Line Selection (Bis-Pak Summary) -----

    async def _fill_line_selection(self, page: Page, profile: ProspectProfile) -> None:
        """Fill the Bis-Pak line selection summary: Program, Enhancements, Liability, Med Exp."""
        logger.info("[Acuity] Filling Line Selection / Bis-Pak Summary...")

        # Program dropdown — default to "Deluxe" if available
        try:
            await page.select_option("select[name*='rogram'], select[name*='Program']", label="Deluxe")
        except Exception:
            logger.info("[Acuity] Could not set Program — using default.")

        # Property Enhancements — leave at default (Silver)
        # Business Liability Enhancements — leave at default (Silver)

        # Liability Limits — map from GL details
        limits = profile.gl.desired_limits  # e.g. "1M/2M"
        limits_map = {
            "300K/600K": "$300,000",
            "500K/1M": "$500,000",
            "1M/2M": "$1,000,000",
            "2M/4M": "$2,000,000",
        }
        limit_value = limits_map.get(limits, "$1,000,000")
        try:
            liability_selects = page.locator("select[name*='iability'], select[name*='Liability']")
            if await liability_selects.count() > 0:
                await liability_selects.first.select_option(label=limit_value)
        except Exception:
            logger.warning("[Acuity] Could not set Liability Limit to '%s'", limit_value)

        # Medical Expenses — leave at default ($5,000)

        # Subcontractors question — may appear here
        if profile.uses_subcontractors is False:
            try:
                sub_select = page.locator("select[name*='ubcontract'], text=Subcontractors")
                if await sub_select.count() > 0:
                    # Select "No" for subcontractors
                    await page.select_option("select[name*='ubcontract']", label="No")
            except Exception:
                pass
        elif profile.uses_subcontractors is True:
            if profile.pct_work_subcontracted and profile.pct_work_subcontracted > 50:
                logger.warning("[Acuity] >50%% subcontracted — may be ineligible for Bis-Pak. Continuing anyway.")

        # Click Next to proceed
        await page.click("input[value='Next'], button:has-text('Next')")
        await page.wait_for_timeout(3000)
        logger.info("[Acuity] Line Selection complete.")

    # ----- Location modal -----

    async def _fill_location(self, page: Page, profile: ProspectProfile) -> None:
        """Add location via the Location modal."""
        logger.info("[Acuity] Adding Location...")

        # Click "Add Location" button
        try:
            await page.click("input[value*='Add Location'], button:has-text('Add Location'), a:has-text('Add Location')")
            await page.wait_for_timeout(2000)
        except Exception:
            logger.warning("[Acuity] Could not find Add Location button — may already be on location page.")

        # Location address — use first location or mailing address
        loc = profile.locations[0] if profile.locations else None
        addr = loc.address if loc else profile.mailing_address

        if addr.street:
            try:
                addr_input = page.locator("input[name*='ddress'], input[name*='street']").first
                await addr_input.fill(addr.street)
            except Exception:
                pass

        if addr.city:
            try:
                await page.locator("input[name*='ity']").first.fill(addr.city)
            except Exception:
                pass

        if addr.state:
            try:
                await page.select_option("select[name*='tate']", label=addr.state)
            except Exception:
                pass

        if addr.zip_code:
            try:
                await page.locator("input[name*='ip']").first.fill(addr.zip_code)
            except Exception:
                pass

        # Territory — auto-selected by ZIP code, but may need manual selection
        # Protection Class — locked/read-only, auto-filled from ZIP

        # Click Add/Save to confirm location
        try:
            await page.click("input[value*='Add'], button:has-text('Add'), input[value='Save']")
            await page.wait_for_timeout(3000)
        except Exception:
            logger.warning("[Acuity] Could not confirm location — user may need to click Add.")

        logger.info("[Acuity] Location added.")

    # ----- Building modal -----

    async def _fill_building(self, page: Page, profile: ProspectProfile) -> None:
        """Add building via the Building modal."""
        logger.info("[Acuity] Adding Building...")
        prop = profile.property

        # Click "Add Building"
        try:
            await page.click("input[value*='Add Building'], button:has-text('Add Building'), a:has-text('Add Building')")
            await page.wait_for_timeout(2000)
        except Exception:
            logger.warning("[Acuity] Could not find Add Building button.")

        # Plan dropdown (Deluxe/Standard/Basic)
        try:
            await page.select_option("select[name*='lan'], select[name*='Plan']", label="Deluxe")
        except Exception:
            pass

        # Property Coverage — leave at default

        # Interest — "Owner" if owner-occupied
        if prop.occupied_by:
            interest = "Owner" if "owner" in prop.occupied_by.value.lower() else "Tenant"
            try:
                await page.select_option("select[name*='nterest'], select[name*='Interest']", label=interest)
            except Exception:
                pass

        # Property Deductible
        deductible_map = {
            "$250": "$250",
            "$500": "$500",
            "$1,000": "$1,000",
            "$2,500": "$2,500",
            "$5,000": "$5,000",
        }
        ded = deductible_map.get(prop.property_deductible, "$500")
        try:
            await page.select_option("select[name*='eductible'], select[name*='Deductible']", label=ded)
        except Exception:
            pass

        # Construction Type
        if prop.construction_type:
            const_text = ACUITY_CONSTRUCTION_MAP.get(prop.construction_type.value, "Frame")
            try:
                await page.select_option("select[name*='onstruction'], select[name*='Construction']", label=const_text)
                await page.wait_for_timeout(1000)
            except Exception:
                logger.warning("[Acuity] Could not set Construction Type to '%s'", const_text)

        # Roof Material
        if prop.roof_material:
            try:
                await page.select_option("select[name*='oof'], select[name*='Roof']", label=prop.roof_material)
            except Exception:
                pass

        # Year Built
        if prop.year_built:
            try:
                await page.locator("input[name*='ear'], input[name*='Year']").first.fill(str(prop.year_built))
            except Exception:
                pass

        # Number of Stories
        if prop.number_of_stories:
            try:
                await page.locator("input[name*='tor'], input[name*='Stories']").first.fill(str(prop.number_of_stories))
            except Exception:
                pass

        # Square Footage
        if prop.square_footage:
            try:
                await page.locator("input[name*='quare'], input[name*='SqFt']").first.fill(str(prop.square_footage))
            except Exception:
                pass

        # Building Limit
        if prop.building_value:
            try:
                await page.locator("input[name*='uilding'], input[name*='Building']").first.fill(str(int(prop.building_value)))
            except Exception:
                pass

        # BPP (Business Personal Property) Limit
        if prop.bpp_value:
            try:
                await page.locator("input[name*='ersonal'], input[name*='BPP']").first.fill(str(int(prop.bpp_value)))
            except Exception:
                pass

        # Valuation — default Replacement Cost
        try:
            await page.select_option("select[name*='aluation'], select[name*='Valuation']", label=prop.valuation or "Replacement Cost")
        except Exception:
            pass

        # Occupancy Description — owner-occupied
        if prop.occupied_by:
            try:
                await page.select_option("select[name*='ccupan'], select[name*='Occupancy']", label="Owner Occupied")
            except Exception:
                pass

        # Click Add/Save to confirm building
        try:
            await page.click("input[value*='Add'], button:has-text('Add Building'), input[value='Save']")
            await page.wait_for_timeout(3000)
        except Exception:
            logger.warning("[Acuity] Could not confirm building — user may need to click Add.")

        # Add Liability Class modal may appear — click Add if present
        try:
            add_class_btn = page.locator("input[value*='Add'], button:has-text('Add')")
            await add_class_btn.click(timeout=5000)
            await page.wait_for_timeout(2000)
        except PlaywrightTimeout:
            pass

        logger.info("[Acuity] Building added.")

    # ----- Bis-Pak Unit Options -----

    async def _fill_unit_options(self, page: Page) -> None:
        """Bis-Pak Unit Options page — leave optional coverages at defaults."""
        logger.info("[Acuity] Bis-Pak Unit Options — leaving at defaults...")

        # Click Next to proceed (don't toggle any optional coverages)
        try:
            await page.click("input[value='Next'], button:has-text('Next')")
            await page.wait_for_timeout(3000)
        except Exception:
            pass

    # ----- Bis-Pak Policy Options -----

    async def _fill_policy_options(self, page: Page) -> None:
        """Bis-Pak Policy Options page — leave optional coverages at defaults."""
        logger.info("[Acuity] Bis-Pak Policy Options — leaving at defaults...")

        try:
            await page.click("input[value='Next'], button:has-text('Next')")
            await page.wait_for_timeout(3000)
        except Exception:
            pass

    # ----- Additional Interests -----

    async def _fill_additional_interests(self, page: Page) -> None:
        """Additional Interests page — skip (no additional insureds to add)."""
        logger.info("[Acuity] Additional Interests — skipping...")

        try:
            await page.click("input[value='Next'], button:has-text('Next')")
            await page.wait_for_timeout(3000)
        except Exception:
            pass

    # ----- Additional Info: Other Insurance and Loss History -----

    async def _fill_additional_info(self, page: Page, profile: ProspectProfile) -> None:
        """Fill Prior/Current Insurance and Loss History (CCServlet page)."""
        logger.info("[Acuity] Filling Additional Info — Insurance & Loss History...")

        # Current Insurance Carrier dropdown — fuzzy match
        prior_carrier = ""
        if profile.prior_insurance:
            prior_carrier = profile.prior_insurance[0].carrier_name
        elif profile.current_carrier:
            prior_carrier = profile.current_carrier

        carrier_match = _fuzzy_match_carrier(prior_carrier)
        try:
            await page.select_option("select[name*='arrier'], select[name*='Carrier']", label=carrier_match)
        except Exception:
            logger.warning("[Acuity] Could not set prior carrier to '%s'", carrier_match)

        # Loss History source — hardcode "From Applicant"
        try:
            await page.select_option("select[name*='oss'], select[name*='Loss']", label="From Applicant")
        except Exception:
            pass

        # Claim History — check if there are any BOP-related claims
        bop_lines = {LineOfBusiness.GL, LineOfBusiness.PROPERTY}
        bop_claims = [c for c in profile.claims if c.line_of_business in bop_lines]

        if not bop_claims:
            try:
                await page.select_option("select[name*='laim'], select[name*='Claim']", label="No Claims")
            except Exception:
                pass
        else:
            try:
                await page.select_option("select[name*='laim'], select[name*='Claim']", label="Yes")
                await page.wait_for_timeout(1000)

                # Total number of losses
                total_count = len(bop_claims)
                loss_count_input = page.locator("input[name*='umber'], input[name*='Losses']").first
                await loss_count_input.fill(str(total_count))

                # Total paid & reserved
                total_dollars = sum(
                    (c.amount_paid or 0) + (c.amount_reserved or 0)
                    for c in bop_claims
                )
                paid_input = page.locator("input[name*='aid'], input[name*='Paid']").first
                await paid_input.fill(str(int(total_dollars)))

                # Time period (years) — max claim age, capped at 3
                today = date.today()
                max_age = max(
                    (today.year - c.date_of_loss.year) for c in bop_claims
                    if c.date_of_loss
                ) if any(c.date_of_loss for c in bop_claims) else 3
                period = str(min(max(max_age, 1), 3))
                await page.select_option("select[name*='eriod'], select[name*='Period']", label=period)
            except Exception:
                logger.warning("[Acuity] Error filling claim details — user should verify.")

        # Click Next
        await page.click("input[value='Next'], button:has-text('Next')")
        await page.wait_for_timeout(3000)
        logger.info("[Acuity] Loss History complete.")

    # ----- Additional Info: General Information -----

    async def _fill_general_info(self, page: Page, profile: ProspectProfile) -> None:
        """Fill the General Info page (max stories question)."""
        logger.info("[Acuity] Filling General Information...")

        # "Up to how many stories does the insured building have?"
        stories = profile.property.number_of_stories or 1
        if stories >= 4:
            dropdown_value = "4+"
            logger.warning("[Acuity] Building has 4+ stories — may exceed Bis-Pak eligibility.")
        else:
            dropdown_value = str(stories)

        try:
            await page.select_option("select", label=dropdown_value)
        except Exception:
            logger.warning("[Acuity] Could not set max stories to '%s'", dropdown_value)

        # Click Next — should advance to Premium Summary
        await page.click("input[value='Next'], button:has-text('Next')")
        await page.wait_for_timeout(5000)
        logger.info("[Acuity] General Info complete — advancing to Premium Summary.")

    # ----- Premium Summary (STOP HERE) -----

    async def _scrape_premium(self, page: Page) -> Optional[Decimal]:
        """Scrape the premium from the Premium Summary page. Do NOT bind."""
        logger.info("[Acuity] Scraping Premium Summary...")

        try:
            await page.wait_for_selector("text=Your quote is ready to bind", timeout=30_000)
        except PlaywrightTimeout:
            logger.warning("[Acuity] Did not reach 'ready to bind' — may need manual intervention.")
            return None

        # Scrape the headline premium
        try:
            premium_el = page.locator("text=/Premium.*\\$/").first
            premium_text = await premium_el.text_content()
            match = re.search(r"\$([\d,]+\.\d{2})", premium_text or "")
            if match:
                premium = Decimal(match.group(1).replace(",", ""))
                logger.info("[Acuity] Premium = $%s", premium)

                # Also log terrorism coverage options
                try:
                    include_el = page.locator("text=/Include Terrorism.*\\$/").first
                    include_text = await include_el.text_content()
                    logger.info("[Acuity] %s", include_text)
                except Exception:
                    pass

                try:
                    exclude_el = page.locator("text=/Exclude Terrorism.*\\$/").first
                    exclude_text = await exclude_el.text_content()
                    logger.info("[Acuity] %s", exclude_text)
                except Exception:
                    pass

                return premium
        except Exception:
            logger.warning("[Acuity] Could not parse premium amount.")

        return None
