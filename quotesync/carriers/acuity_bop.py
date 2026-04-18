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

import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
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
# Step registry — names used for resume_from
# ---------------------------------------------------------------------------

STEPS = [
    "policy_dropdowns",
    "nature_of_business",
    "class_questions",
    "named_insured",
    "save_dialog",
    "dun_bradstreet",
    "line_selection",
    "location",
    "building",
    "unit_options",
    "policy_options",
    "additional_interests",
    "additional_info",
    "general_info",
    "premium",
]

# State file — persists the last saved quote name for resume support
_STATE_FILE = Path(__file__).parent.parent.parent / "data" / ".acuity_state.json"


def _save_state(data: dict) -> None:
    _STATE_FILE.parent.mkdir(exist_ok=True)
    _STATE_FILE.write_text(json.dumps(data, indent=2))


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {}

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

        # Fill username — click first so any JS focus handlers fire before we type
        user_input = page.locator("input[name='Ecom_User_AcuityLoginID']").first
        await user_input.click()
        await user_input.fill(self.username)

        # Fill password
        pass_input = page.locator("input[type='password']").first
        await pass_input.click()
        await pass_input.fill(self.password)

        # Click Log In — button text is "Log In" (with space), not "Login"
        await page.click("button:has-text('Log In'), input[type='submit'], input[value='Login']")

        # Wait for Agent Center dashboard (may pause for MFA — give 120s)
        try:
            await page.wait_for_url("**/agentcenter**", timeout=120_000)
        except PlaywrightTimeout:
            logger.warning("[Acuity] Login redirect timed out — user may need to complete MFA manually.")
            await page.wait_for_url("**/agentcenter**", timeout=300_000)

        logger.info("[Acuity] Logged in successfully.")

    # ----- navigate to new quote -----

    async def _open_irating(self, page: Page) -> Page:
        """Open the iRating popup from Agent Center and return the iRating page."""
        async with page.context.expect_page() as new_page_info:
            await page.click("a:has-text('iRating'), a[href*='irating'], a[href*='MainServlet']")
        irating_page = await new_page_info.value
        # Wait for the Main Menu content rather than the URL — the popup may go through
        # redirects or load an intermediate page before settling on MainServlet.
        await irating_page.wait_for_load_state("domcontentloaded", timeout=30_000)
        await irating_page.wait_for_selector(
            "a:has-text('Start Full Quote'), .pageHeaderBanner:has-text('Main Menu')",
            timeout=60_000,
        )
        logger.info("[Acuity] iRating Main Menu loaded. URL: %s", irating_page.url)
        self._page = irating_page
        return irating_page

    async def navigate_to_new_quote(self, page: Page) -> None:
        """From Agent Center, open iRating and click Start Full Quote."""
        logger.info("[Acuity] Navigating to iRating...")
        irating_page = await self._open_irating(page)

        # Main Menu is now visible — click Start Full Quote to begin
        await irating_page.wait_for_selector("a:has-text('Start Full Quote')", timeout=30_000)
        await irating_page.click("a:has-text('Start Full Quote')")
        await irating_page.wait_for_load_state("domcontentloaded")
        logger.info("[Acuity] iRating opened — Start Full Quote clicked.")

    async def navigate_to_saved_quote(self, page: Page) -> None:
        """Resume mode: open iRating and retrieve the last saved in-progress quote.

        Uses the 'Search by Save Name' form on the Main Menu to find and open
        the quote saved during _handle_save_dialog.  Falls back to a new quote
        if no saved state exists.
        """
        state = _load_state()
        quote_name = state.get("last_quote_name")

        if not quote_name:
            logger.warning("[Acuity] No saved quote name found — starting a new quote instead.")
            await self.navigate_to_new_quote(page)
            return

        logger.info("[Acuity] Resume: opening iRating to retrieve '%s'...", quote_name)
        irating_page = await self._open_irating(page)

        # Main Menu search form: fill save name + click Go
        await irating_page.wait_for_selector("input[id='searchName']", timeout=15_000)
        await irating_page.fill("input[id='searchName']", quote_name)
        await irating_page.click("input[name='searchBySaveName']")
        await irating_page.wait_for_load_state("domcontentloaded")
        await irating_page.wait_for_timeout(2000)

        # Results list — click the first matching row
        try:
            first_result = irating_page.locator("table a, table tr td a").first
            await first_result.click(timeout=10_000)
            await irating_page.wait_for_load_state("domcontentloaded")
            logger.info("[Acuity] Resumed quote: '%s'", quote_name)
        except PlaywrightTimeout:
            logger.warning(
                "[Acuity] Could not find saved quote '%s' in results — starting new quote.",
                quote_name,
            )
            await irating_page.click("a:has-text('Start Full Quote')")
            await irating_page.wait_for_load_state("domcontentloaded")

    async def _setup_policy_dropdowns(self, page: Page, profile: ProspectProfile) -> None:
        """Fill the Policy Selection page dropdowns.

        Cascade order: Exposure State → Policy Type → Line → Term.

        What we know from portal inspection:
        - Exposure State: Dijit Select wrapping hidden <select id="AgcyState">.
          Outer container has [widgetid='AgcyState'].  Click it to open popup.
        - Policy Type: Dijit Select, widgetid unknown until runtime — found by
          dumping all [widgetid] elements and excluding known ones.
        - Line (#line): Dijit ComboBox — the visible text input HAS id="line".
          Fill by clicking it and typing "Bis-Pak", then pick from dropdown.
        - Term (#term): Same ComboBox pattern, input id="term".
        """
        logger.info("[Acuity] Setting up policy dropdowns...")
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(500)

        state = profile.mailing_address.state or "ME"

        # Dump all Dijit widget container IDs for diagnostics
        wid_dump = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('[widgetid]')).map(el => ({
                wid: el.getAttribute('widgetid'),
                tag: el.tagName,
                cls: el.className.slice(0, 50)
            }))
        """)
        print(f"[Acuity] [widgetid] elements: {wid_dump}")

        # ── Reusable: open a Dijit widget and click the option text in its popup ──

        async def _open_and_pick(widget_selector: str, option_text: str, log_name: str) -> bool:
            """Click widget_selector to open its Dijit popup, then click option_text."""
            try:
                await page.click(widget_selector, timeout=5000)
                print(f"[Acuity] Opened popup for {log_name}")
                await page.wait_for_timeout(700)
            except Exception as e:
                print(f"[Acuity] Could not open {log_name} ({widget_selector}): {e}")
                return False

            # Click the matching option using JS TreeWalker across all popup containers
            result = await page.evaluate(
                """([optText]) => {
                    const norm = s => s.trim().toLowerCase();
                    const target = norm(optText);
                    const containers = document.querySelectorAll(
                        '.dijitPopup, .dijitSelectMenu, .dijitMenu, [role="listbox"], .dijitComboBoxMenu'
                    );
                    for (const c of containers) {
                        const walker = document.createTreeWalker(c, NodeFilter.SHOW_ELEMENT);
                        let node;
                        while ((node = walker.nextNode())) {
                            if (norm(node.textContent) === target && node.childElementCount === 0) {
                                const r = node.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {
                                    node.click();
                                    return 'ok:' + node.tagName + ':' + node.className.slice(0, 30);
                                }
                            }
                        }
                    }
                    // Pass 2: any popup item that starts with or contains the target
                    for (const c of containers) {
                        const walker = document.createTreeWalker(c, NodeFilter.SHOW_ELEMENT);
                        let node;
                        while ((node = walker.nextNode())) {
                            const t = norm(node.textContent);
                            if ((t.startsWith(target) || t.includes(target)) && node.childElementCount === 0) {
                                const r = node.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {
                                    node.click();
                                    return 'ok-contains:' + node.textContent.trim().slice(0,30);
                                }
                            }
                        }
                    }
                    return 'not-found';
                }""",
                [option_text],
            )
            print(f"[Acuity] {log_name} → '{option_text}': {result}")
            if str(result).startswith("ok"):
                # Wait for AJAX cascade triggered by this selection
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    await page.wait_for_timeout(1000)
                return True
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            return False

        # ── Reusable: fill a Dijit ComboBox by typing + picking from suggestion list ──

        async def _fill_combobox(input_id: str, value: str, log_name: str) -> bool:
            """Click the ComboBox input, type value, pick matching suggestion."""
            try:
                await page.click(f"#{input_id}", click_count=3)  # select-all so we replace any text
                await page.wait_for_timeout(200)
                await page.type(f"#{input_id}", value, delay=80)  # type chars to trigger autocomplete
                await page.wait_for_timeout(800)
            except Exception as e:
                print(f"[Acuity] ComboBox #{input_id} fill failed: {e}")
                return False

            # Try clicking the suggestion in the dropdown
            result = await page.evaluate(
                """([optText]) => {
                    const norm = s => s.trim().toLowerCase();
                    const target = norm(optText);
                    const containers = document.querySelectorAll(
                        '.dijitComboBoxMenu, .dijitPopup, [role="listbox"]'
                    );
                    // Pass 1: exact case-insensitive match
                    for (const c of containers) {
                        const walker = document.createTreeWalker(c, NodeFilter.SHOW_ELEMENT);
                        let node;
                        while ((node = walker.nextNode())) {
                            if (norm(node.textContent) === target && node.childElementCount === 0) {
                                const r = node.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {
                                    node.click();
                                    return 'ok:' + node.tagName + ':' + node.textContent.trim().slice(0,30);
                                }
                            }
                        }
                    }
                    // Pass 2: suggestion starts with typed text (handles truncated options)
                    for (const c of containers) {
                        const walker = document.createTreeWalker(c, NodeFilter.SHOW_ELEMENT);
                        let node;
                        while ((node = walker.nextNode())) {
                            if (norm(node.textContent).startsWith(target) && node.childElementCount === 0) {
                                const r = node.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {
                                    node.click();
                                    return 'ok-starts:' + node.tagName + ':' + node.textContent.trim().slice(0,30);
                                }
                            }
                        }
                    }
                    return 'not-found';
                }""",
                [value],
            )
            print(f"[Acuity] {log_name} ComboBox suggestion: {result}")
            if result == "not-found":
                # No autocomplete dropdown — just press Enter to accept the typed value
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(500)
            return True

        # ── Exposure State ─────────────────────────────────────────────────
        # Dijit Select: outer container has widgetid="AgcyState"
        await _open_and_pick("[widgetid='AgcyState']", state, "ExposureState")

        # After ExposureState AJAX, the `line` ComboBox auto-opens a popup
        # with Commercial / Personal options.  Press Escape to close it so it
        # doesn't block our next click, then re-open it deliberately.
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(800)

        # ── Line = Commercial ──────────────────────────────────────────────
        # The portal labels [widgetid='line'] as "Policy Type" (Commercial/Personal).
        # We must pick "Commercial" here before the Plan (Bis-Pak) options appear.
        await _open_and_pick("[widgetid='line']", "Commercial", "Line(PolicyType)")

        # Give AJAX time to load Plan/Bis-Pak options after Commercial is selected
        await page.wait_for_timeout(1500)

        # ── Plan = Bis-Pak ─────────────────────────────────────────────────
        # After Line=Commercial, the `plan` ComboBox (portal labels as "Line" or
        # "Program") loads specific products including Bis-Pak.
        await _fill_combobox("plan", "Bis-Pak", "Plan")
        plan_val = await page.evaluate("document.getElementById('plan')?.value")
        print(f"[Acuity] Plan current value: '{plan_val}'")

        # Give AJAX time to load Term options after Plan selection
        await page.wait_for_timeout(1000)

        # ── Insured Information (prefill section) ─────────────────────────
        # Fill insured info FIRST — it triggers AJAX that resets Term and
        # Producer, so we must fill those AFTER the insured section settles.
        # Inner inputs use id="clBusinessName", "clStreet", "clCity", "clZipcode".
        # clState is a Dijit Select.
        biz_name = profile.legal_business_name or f"{profile.first_name or ''} {profile.last_name or ''}".strip()
        if biz_name:
            try:
                await page.fill("#clBusinessName", biz_name)
                await page.wait_for_timeout(200)
                print(f"[Acuity] Business Name filled: '{biz_name}'")
            except Exception as e:
                print(f"[Acuity] Business Name fill failed: {e}")

        addr = profile.mailing_address
        if addr.street:
            try:
                # Inner input has name="clStreet" (id is a generated "customTextBox..." value)
                await page.fill("input[name='clStreet']", addr.street)
            except Exception:
                pass
        if addr.city:
            try:
                await page.fill("#clCity", addr.city)
            except Exception:
                pass
        if addr.state:
            await _open_and_pick("[widgetid='clState']", addr.state, "InsuredState")
        if addr.zip_code:
            try:
                await page.fill("#clZipcode", addr.zip_code)
            except Exception:
                pass

        # Wait for insured AJAX to fully settle before filling Term/Producer
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            await page.wait_for_timeout(2000)
        await page.wait_for_timeout(800)

        # ── Term ───────────────────────────────────────────────────────────
        # Fill AFTER insured info — insured AJAX resets this field.
        await _fill_combobox("term", "12 months", "Term")
        term_val = await page.evaluate("document.getElementById('term')?.value")
        print(f"[Acuity] Term current value: '{term_val}'")

        # ── Producer: search for Chamberlin ───────────────────────────────
        # Fill AFTER insured info — insured AJAX also resets Producer.
        # Open the subAgentCode widget and click the option containing "Chamberlin".
        # Falls back to first non-blank option if Chamberlin not found.
        await page.evaluate("""() => {
            const el = document.querySelector("[widgetid='subAgentCode']");
            if (el) el.click();
        }""")
        await page.wait_for_timeout(700)
        producer_pick = await page.evaluate("""() => {
            const norm = s => s.trim().toLowerCase();
            const containers = document.querySelectorAll(
                '.dijitPopup, .dijitSelectMenu, .dijitMenu, [role="listbox"], .dijitComboBoxMenu'
            );
            // Pass 1: find option containing "chamberlin"
            for (const c of containers) {
                const walker = document.createTreeWalker(c, NodeFilter.SHOW_ELEMENT);
                let node;
                while ((node = walker.nextNode())) {
                    const t = norm(node.textContent);
                    if (t.includes('chamberlin') && node.childElementCount === 0) {
                        const r = node.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            node.click();
                            return 'ok-chamberlin:' + node.textContent.trim().slice(0, 40);
                        }
                    }
                }
            }
            // Pass 2: first non-blank, non-"select" option
            for (const c of containers) {
                const walker = document.createTreeWalker(c, NodeFilter.SHOW_ELEMENT);
                let node;
                while ((node = walker.nextNode())) {
                    const t = norm(node.textContent);
                    if (t && t !== 'select' && node.childElementCount === 0) {
                        const r = node.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            node.click();
                            return 'ok-fallback:' + node.textContent.trim().slice(0, 40);
                        }
                    }
                }
            }
            return 'not-found';
        }""")
        print(f"[Acuity] Producer: {producer_pick}")
        if not producer_pick.startswith("ok"):
            await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)

        # ── CSR — pick first available option ─────────────────────────────
        await page.evaluate("""() => {
            const el = document.querySelector("[widgetid='csrAccountManagerCode']");
            if (el) el.click();
        }""")
        await page.wait_for_timeout(600)
        csr_pick = await page.evaluate("""() => {
            const norm = s => s.trim().toLowerCase();
            const containers = document.querySelectorAll(
                '.dijitPopup, .dijitSelectMenu, .dijitMenu, [role="listbox"], .dijitComboBoxMenu'
            );
            for (const c of containers) {
                const walker = document.createTreeWalker(c, NodeFilter.SHOW_ELEMENT);
                let node;
                while ((node = walker.nextNode())) {
                    const t = norm(node.textContent);
                    if (t && t !== 'select' && node.childElementCount === 0) {
                        const r = node.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            node.click();
                            return 'ok:' + node.textContent.trim().slice(0, 40);
                        }
                    }
                }
            }
            return 'not-found';
        }""")
        print(f"[Acuity] CSR: {csr_pick}")
        if not csr_pick.startswith("ok"):
            await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)

        # Click Next to proceed to Nature of Business
        await page.evaluate("() => { document.getElementById('nextButton')?.click(); }")
        # wait_for_load_state("domcontentloaded") can resolve mid-redirect, destroying the
        # execution context before page.title() runs.  Use "load" + a retry instead.
        try:
            await page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

        try:
            title = await page.title()
        except Exception:
            await page.wait_for_timeout(2000)
            title = await page.title()
        if "Policy Selection" in title:
            print(f"[Acuity] WARNING: Still on Policy Selection ('{title}').")
        else:
            print(f"[Acuity] Advanced → '{title}'")
        logger.info("[Acuity] Policy dropdowns done.")

    # ----- fill_quote (master orchestrator) -----

    async def fill_quote(
        self,
        page: Page,
        profile: ProspectProfile,
        resume_from: str | None = None,
    ) -> dict | None:
        """Fill the entire Bis-Pak quote form, stopping at Premium Summary.

        resume_from: name of the step to resume from (see STEPS list at top of
        file).  All earlier steps are skipped — the browser must already be at
        the correct page state (navigate_to_saved_quote handles that).
        """
        # Use the iRating popup page if available
        p = getattr(self, "_page", page)

        # Determine which step index to start from
        start_idx = 0
        if resume_from:
            if resume_from in STEPS:
                start_idx = STEPS.index(resume_from)
                logger.info("[Acuity] Resuming from step %d: '%s'", start_idx, resume_from)
            else:
                logger.warning(
                    "[Acuity] Unknown resume_from step '%s' — running all steps. "
                    "Valid steps: %s", resume_from, STEPS
                )

        def _should_run(step: str) -> bool:
            return STEPS.index(step) >= start_idx

        def _skip(step: str) -> None:
            logger.info("[Acuity] Skipping step: %s", step)

        if _should_run("policy_dropdowns"):
            await self._setup_policy_dropdowns(p, profile)
        else:
            _skip("policy_dropdowns")

        if _should_run("nature_of_business"):
            await self._fill_nature_of_business(p, profile)
        else:
            _skip("nature_of_business")

        if _should_run("class_questions"):
            await self._fill_class_questions(p, profile)
        else:
            _skip("class_questions")

        if _should_run("named_insured"):
            await self._fill_named_insured(p, profile)
        else:
            _skip("named_insured")

        if _should_run("save_dialog"):
            await self._handle_save_dialog(p, profile)
        else:
            _skip("save_dialog")

        if _should_run("dun_bradstreet"):
            await self._handle_dun_bradstreet(p)
        else:
            _skip("dun_bradstreet")

        if _should_run("line_selection"):
            await self._fill_line_selection(p, profile)
        else:
            _skip("line_selection")

        if _should_run("location"):
            await self._fill_location(p, profile)
        else:
            _skip("location")

        if _should_run("building"):
            await self._fill_building(p, profile)
        else:
            _skip("building")

        if _should_run("unit_options"):
            await self._fill_unit_options(p)
        else:
            _skip("unit_options")

        if _should_run("policy_options"):
            await self._fill_policy_options(p)
        else:
            _skip("policy_options")

        if _should_run("additional_interests"):
            await self._fill_additional_interests(p)
        else:
            _skip("additional_interests")

        if _should_run("additional_info"):
            await self._fill_additional_info(p, profile)
        else:
            _skip("additional_info")

        if _should_run("general_info"):
            await self._fill_general_info(p, profile)
        else:
            _skip("general_info")

        premium = await self._scrape_premium(p)

        logger.info("[Acuity] === QUOTE COMPLETE ===")
        logger.info("[Acuity] Premium: %s", premium)
        logger.info("[Acuity] STOPPED before bind. Agent must review manually.")
        return {"premium": str(premium) if premium else None, "carrier": self.name}

    # ----- Page: Nature of Business -----

    async def _fill_nature_of_business(self, page: Page, profile: ProspectProfile) -> None:
        """Search for class code on the Nature of Business page."""
        logger.info("[Acuity] Filling Nature of Business...")

        # Guard: make sure we actually advanced off Policy Selection
        title = await page.title()
        if "Policy Selection" in title:
            raise RuntimeError(
                f"[Acuity] _fill_nature_of_business called while still on Policy Selection "
                f"(title='{title}'). Policy dropdowns likely failed validation."
            )
        print(f"[Acuity] Nature of Business page: '{title}'")

        # KEY FACT: The NofB search UI lives inside an iframe, not the main page DOM.
        # Dialog: <div id="noboTableDiv" widgetid="noboTableDiv" class="dijitDialog">
        # Iframe:  <iframe class="noboTableIFrame"
        #            src="/irating/servlet/CCServlet?PageID=...NoboTablePage">
        # All fields (noboDescription, glClass, naicsCode, plan) are in the iframe's DOM.
        # The iframe auto-opens with a ~2s delay after page navigation.
        # Use page.frame_locator('iframe.noboTableIFrame') to interact with its content.
        # Use JS .click() on nextButton at the end to bypass the modal overlay.

        # Wait for the NofB modal iframe to appear (opens ~2s after page load)
        try:
            await page.wait_for_selector("iframe.noboTableIFrame", timeout=30000)
            print("[Acuity] NofB modal iframe appeared")
        except PlaywrightTimeout:
            print("[Acuity] WARNING: NofB modal iframe did not appear after 30s")

        # Give the iframe content a moment to render
        await page.wait_for_timeout(1000)

        # All NofB fields are in the iframe — use frame_locator to interact with them
        nob = page.frame_locator("iframe.noboTableIFrame")

        # Determine search term
        raw_code = (profile.sic_naics_code or "").strip()
        desc_term = (profile.operations_description or "").strip()
        is_gl_code = raw_code and len(raw_code) <= 5 and " " not in raw_code
        is_naics = raw_code and len(raw_code) == 6 and raw_code.isdigit()
        gl_code = raw_code if is_gl_code else ""
        naics_code = raw_code if is_naics else ""
        if raw_code and not is_gl_code and not is_naics:
            # Word-form: normalize for Acuity's substring search.
            # Lowercase + singularize: "Apartments" → "apartment" matches
            # "Apartment Buildings", "Apartment - 4 Stories", etc.
            term = raw_code.lower()
            if term.endswith("s") and len(term) > 4:
                term = term[:-1]  # "apartments" → "apartment"
            desc_term = term

        if naics_code:
            try:
                await nob.locator("input[name='naicsCode']").fill(naics_code)
                print(f"[Acuity] NAICS set: '{naics_code}'")
            except Exception as e:
                print(f"[Acuity] NAICS fill error: {e}")
        elif gl_code:
            try:
                await nob.locator("input[name='glClass']").fill(gl_code)
                print(f"[Acuity] GL Class set: '{gl_code}'")
            except Exception as e:
                print(f"[Acuity] GL class fill error: {e}")
        elif desc_term:
            try:
                await nob.locator("input[name='noboDescription']").fill(desc_term)
                print(f"[Acuity] Description set: '{desc_term}'")
            except Exception as e:
                print(f"[Acuity] Description fill error: {e}")

        # Click Search button inside the iframe
        search_clicked = False
        for sel in [
            "input[value='Search']",
            "input[type='submit']",
            "button:has-text('Search')",
            "a:has-text('Search')",
        ]:
            try:
                await nob.locator(sel).first.click(timeout=3000)
                print(f"[Acuity] Search clicked via: {sel}")
                search_clicked = True
                break
            except Exception:
                pass
        if not search_clicked:
            # Fallback: press Enter in whichever field was filled
            field_name = "naicsCode" if naics_code else ("glClass" if gl_code else "noboDescription")
            try:
                await nob.locator(f"input[name='{field_name}']").press("Enter")
                print(f"[Acuity] Search via Enter on {field_name}")
            except Exception as e:
                print(f"[Acuity] Search Enter fallback error: {e}")

        await page.wait_for_timeout(2000)

        # Helper: click first result — results render as radio buttons, not links
        async def _click_first_result() -> bool:
            for sel in ["input[type='radio']", "table a, td a"]:
                try:
                    loc = nob.locator(sel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=3000)
                        print(f"[Acuity] Result selected via: {sel}")
                        return True
                except Exception:
                    pass
            return False

        # Helper: search with a given description term
        async def _search_desc(term: str) -> None:
            await nob.locator("input[name='noboDescription']").fill(term)
            for sel in ["input[value='Search']", "input[type='submit']"]:
                try:
                    await nob.locator(sel).first.click(timeout=2000)
                    return
                except Exception:
                    pass
            await nob.locator("input[name='noboDescription']").press("Enter")

        # Helper: select from Business Type dropdown and search — this is how the
        # Acuity NofB dialog actually works (dropdown filters the class list, not description)
        async def _search_by_business_type(term: str) -> bool:
            try:
                sel_elem = nob.locator("select")
                options: list[str] = await sel_elem.locator("option").all_text_contents()
                term_lower = term.lower()
                match = next(
                    (o for o in options if o.strip() and
                     (term_lower in o.lower() or o.lower().strip() in term_lower)),
                    None,
                )
                if not match:
                    print(f"[Acuity] No Business Type option matches '{term}' — options: {options[:10]}")
                    return False
                await sel_elem.select_option(label=match)
                print(f"[Acuity] Business Type selected: '{match}'")
                await page.wait_for_timeout(500)
                for btn in ["input[value='Search']", "input[type='submit']"]:
                    try:
                        await nob.locator(btn).first.click(timeout=2000)
                        break
                    except Exception:
                        pass
                await page.wait_for_timeout(2000)
                return True
            except Exception as e:
                print(f"[Acuity] Business Type dropdown error: {e}")
                return False

        search_label = naics_code or gl_code or desc_term
        if not await _click_first_result():
            # Description search returned nothing — try Business Type dropdown (primary Acuity workflow)
            if desc_term:
                print(f"[Acuity] No results for '{search_label}' — trying Business Type dropdown")
                await _search_by_business_type(desc_term)
                await page.wait_for_timeout(2000)

            if not await _click_first_result():
                # Try first word of description as Business Type
                if desc_term and " " in desc_term:
                    retry_term = desc_term.split()[0]
                    print(f"[Acuity] Retrying Business Type with first word: '{retry_term}'")
                    await _search_by_business_type(retry_term)
                    await page.wait_for_timeout(2000)
                elif desc_term:
                    # Try description search fallback with first word
                    await _search_desc(desc_term.split()[0])
                    await page.wait_for_timeout(2000)

            if not await _click_first_result():
                print(f"[Acuity] No results — waiting 120s for manual class selection.")
                await page.wait_for_timeout(120_000)
                # After 120s, check if user selected a class (modal auto-closes on selection)
                modal_still_open = await page.evaluate("""() => {
                    const d = document.getElementById('noboTableDiv');
                    if (!d) return false;
                    const s = getComputedStyle(d);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }""")
                if modal_still_open:
                    if not await _click_first_result():
                        print("[Acuity] WARNING: No class selected after 120s — proceeding anyway. "
                              "Quote may fail validation.")
            else:
                print(f"[Acuity] Selected first class code result")
                await page.wait_for_timeout(1500)
        else:
            print(f"[Acuity] Selected first class code result for '{search_label}'")
            await page.wait_for_timeout(1500)

        # After class selection, handle any class-specific questions
        await self._handle_class_questions(page, profile)

        # Click Next via JS (NofB iframe always intercepts Playwright pointer events)
        await page.evaluate("() => { document.getElementById('nextButton')?.click(); }")

        # Wait for the NofB modal to close — if no class was selected, validation
        # rejects the click and the modal stays open, blocking all subsequent steps.
        try:
            await page.wait_for_function(
                """() => {
                    const d = document.getElementById('noboTableDiv');
                    if (!d) return true;
                    const s = getComputedStyle(d);
                    return s.display === 'none' || s.visibility === 'hidden'
                        || d.getAttribute('aria-hidden') === 'true';
                }""",
                timeout=15_000,
            )
            print("[Acuity] NofB modal closed — advancing to Named Insured.")
        except PlaywrightTimeout:
            raise RuntimeError(
                "[Acuity] NofB modal did not close after Next click — no class was selected. "
                "Run again and select a class within the 120s window."
            )

        await page.wait_for_timeout(2000)
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

    # ----- Page: Plan/Line Questions (habitational class-specific) -----

    async def _fill_class_questions(self, page: Page, profile: ProspectProfile) -> None:
        """Fill the habitational Plan/Line Questions page that appears between
        Nature of Business and Named Insured for apartment/habitational classes.

        Most answers are hardcoded Acuity-safe defaults:
        - Exclusion questions (Yes → risk declined) are answered "No"
        - Code-baseline questions are answered to match universal apartment code
        - Amenities / fireplace exposures default to "None"

        Prospect-specific answers (monthlyRent, studentHousing, assistedLiving,
        seasonalOrShortTerm, tenantInsurance, smokingAllowed, amenities) are
        TODO(habitational) until a ProspectProfile sub-model is added.
        """
        title = await page.title()
        print(f"[Acuity CQ] _fill_class_questions — page: '{title}'")

        # Guard: only run when the class-questions page is actually visible.
        # less100Amp is the first widget on the page — if it isn't present, skip.
        marker_count = await page.locator("[widgetid='less100Amp']").count()
        if not marker_count:
            print("[Acuity CQ] less100Amp marker not found — class-questions page not active, skipping.")
            return

        await page.wait_for_timeout(1000)

        # Description of Operations (free text)
        desc = (profile.operations_description or "Apartment buildings").strip()[:200]
        try:
            await page.fill("#descriptionOfOperations", desc, timeout=3000)
            print(f"[Acuity CQ] descriptionOfOperations: '{desc}'")
        except Exception as e:
            print(f"[Acuity CQ] descriptionOfOperations fill failed: {e}")

        # Yes/No questions — Dijit yesNoButtons widgets.
        # First try dijit.byId(wid).set('value', bool); fall back to DOM click
        # walking up from the matched text leaf to a .dijitButton ancestor.
        yn_defaults = {
            "less100Amp": "No",              # Exclusion: <100A service is unacceptable
            "structureBuiltForHuman": "Yes", # Code baseline for habitational
            "studentHousing": "No",          # TODO(habitational): prospect-specific
            "assistedLiving": "No",          # TODO(habitational): prospect-specific
            "seasonalOrShortTerm": "No",     # TODO(habitational): prospect-specific
            "grillsPermitted": "No",         # Exclusion: grills on balconies unacceptable
            "unrepairedRoof": "No",          # Exclusion: unrepaired damage unacceptable
            "tenantInsurance": "Yes",        # Default: agency requires tenants to carry HO-4
            "smokingAllowed": "No",          # TODO(habitational): prospect-specific
            "twoMeansEgress": "Yes",         # Code baseline for habitational
        }
        for widget_id, answer in yn_defaults.items():
            try:
                widget = page.locator(f"[widgetid='{widget_id}']").first
                btn = widget.locator(
                    f"button:has-text('{answer}'), "
                    f"[role='button']:has-text('{answer}')"
                ).first
                await btn.click(timeout=5000)
                print(f"[Acuity CQ] {widget_id} → '{answer}': clicked")
            except Exception as e:
                html = await page.evaluate(
                    f"() => document.querySelector(\"[widgetid='{widget_id}']\")?.innerHTML?.slice(0,300) ?? 'not-found'"
                )
                print(f"[Acuity CQ] {widget_id} → '{answer}' FAILED: {e}")
                print(f"[Acuity CQ] {widget_id} markup: {html}")

        # Monthly rent — required text input, no schema field yet
        monthly_rent = "1500"  # TODO(habitational): source from profile.retail_habitational
        try:
            await page.fill("#monthlyRent", monthly_rent, timeout=3000)
            print(f"[Acuity CQ] monthlyRent: '{monthly_rent}' (hardcoded default)")
        except Exception as e:
            print(f"[Acuity CQ] monthlyRent fill failed: {e}")

        # Amenities group        — check "None"             (APQ0053)
        # Fireplace exposures    — check "None"             (APQ0059)
        # Smoke detectors        — check "Battery operated" (APQ0062) — always-present default
        for cb_id, label in [
            ("APQ0053__", "None (amenities)"),
            ("APQ0059__", "None (fireplace exposures)"),
            ("APQ0062__", "Battery operated (smoke detectors)"),
        ]:
            result = await page.evaluate(
                """(id) => {
                    const el = document.getElementById(id);
                    if (!el) return 'not-found';
                    if (!el.checked) el.click();
                    return el.checked ? 'checked' : 'failed';
                }""",
                cb_id,
            )
            print(f"[Acuity CQ] {cb_id} — {label}: {result}")

            # After checking "Battery operated", check the Semi-annually sub-question.
            if cb_id == "APQ0062__" and result in ("checked", "already-checked"):
                await page.wait_for_timeout(500)
                semi_result = await page.evaluate(
                    """() => {
                        const el = document.getElementById('APQ0065__');
                        if (!el) return 'not-found';
                        if (!el.checked) el.click();
                        return el.checked ? 'checked' : 'failed';
                    }"""
                )
                print(f"[Acuity CQ] APQ0065__ — Semi-annually: {semi_result}")

        # Circuit protection / wiring dropdown (APQ0067__) — required select.
        # Default to "Circuit breakers" (universal in modern apartments).
        cb_result = await page.evaluate(
            """(target) => {
                const inp = document.getElementById('APQ0067__');
                if (!inp) return 'no-input';
                // Find the enclosing Dijit widget and click to open the popup
                const widget = inp.closest('[widgetid]') || inp.parentElement;
                if (widget) widget.click();
                return 'opened';
            }""",
            "Circuit breakers",
        )
        print(f"[Acuity CQ] APQ0067 trigger: {cb_result}")
        await page.wait_for_timeout(500)
        pick_result = await page.evaluate(
            """(target) => {
                const norm = s => (s || '').trim().toLowerCase();
                const t = norm(target);
                const containers = document.querySelectorAll(
                    '.dijitPopup, .dijitSelectMenu, .dijitMenu, [role="listbox"], .dijitComboBoxMenu'
                );
                for (const c of containers) {
                    const walker = document.createTreeWalker(c, NodeFilter.SHOW_ELEMENT);
                    let n;
                    while ((n = walker.nextNode())) {
                        if (n.childElementCount > 0) continue;
                        const nt = norm(n.textContent);
                        if (nt === t || nt.startsWith(t) || nt.includes(t)) {
                            const r = n.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                n.click();
                                return 'ok:' + n.textContent.trim().slice(0, 40);
                            }
                        }
                    }
                }
                return 'not-found';
            }""",
            "Circuit breakers",
        )
        print(f"[Acuity CQ] APQ0067 pick 'Circuit breakers': {pick_result}")

        # Habitational pre-bind acknowledgment checkbox
        result = await page.evaluate(
            """() => {
                const el = document.getElementById('habRiskPreBindCheckBox');
                if (!el) return 'not-found';
                if (!el.checked) el.click();
                return el.checked ? 'checked' : 'failed';
            }"""
        )
        print(f"[Acuity CQ] habRiskPreBindCheckBox: {result}")

        await page.wait_for_timeout(800)

        next_result = await page.evaluate("""() => {
            const b = document.getElementById('nextButton');
            if (b) { b.click(); return 'ok'; }
            return 'not-found';
        }""")
        print(f"[Acuity CQ] Next click -> {next_result}")
        await page.wait_for_timeout(3000)

        logger.info("[Acuity] Class Questions complete.")

    # ----- Page: Named Insured -----

    async def _fill_named_insured(self, page: Page, profile: ProspectProfile) -> None:
        """Fill the Named Insured and address page."""
        logger.info("[Acuity] Filling Named Insured...")

        # Guard: if class-questions widgets are still visible, the previous step
        # didn't advance. Fail loudly rather than silently filling nothing.
        still_on_cq = await page.locator("[widgetid='less100Amp']").count()
        if still_on_cq:
            raise RuntimeError(
                "[Acuity] _fill_named_insured called while Plan/Line Questions page "
                "is still active (less100Amp widget visible). The class_questions "
                "step failed to advance — check its logs above."
            )

        # ── Wait for Named Insured page to be active ────────────────────────
        # Acuity loads all pages simultaneously; after NofB Next, the NI content
        # pane becomes visible. Wait for title to change from "Nature of Business".
        print("[Acuity NI] Waiting for Named Insured page to become active...")
        try:
            await page.wait_for_function(
                "() => !document.title.includes('Nature of Business')",
                timeout=30000,
            )
            ni_title = await page.title()
            print(f"[Acuity NI] Page active: '{ni_title}'")
        except PlaywrightTimeout:
            ni_title = await page.title()
            print(f"[Acuity NI] WARNING: title still '{ni_title}' after 30s — proceeding anyway")

        await page.wait_for_timeout(1500)  # let Dijit widgets finish rendering

        # ── Diagnostic: visible widgetid elements and text inputs ────────────
        vis_dump = await page.evaluate("""() => {
            function isVisible(el) {
                let e = el;
                while (e) {
                    const s = getComputedStyle(e);
                    if (s.display === 'none' || s.visibility === 'hidden') return false;
                    e = e.parentElement;
                }
                return el.offsetWidth > 0 || el.offsetHeight > 0;
            }
            const wids = Array.from(document.querySelectorAll('[widgetid]'))
                .filter(isVisible)
                .map(el => ({wid: el.getAttribute('widgetid'), cls: el.className.slice(0, 40)}));
            const inputs = Array.from(document.querySelectorAll('input'))
                .filter(isVisible)
                .map(el => ({id: el.id, name: el.name, type: el.type, val: el.value.slice(0,20)}));
            return {wids, inputs};
        }""")
        print(f"[Acuity NI] visible widgetids: {[w['wid'] for w in vis_dump.get('wids', [])]}")
        print(f"[Acuity NI] visible inputs: {vis_dump.get('inputs', [])}")

        # ── Reusable: open a Dijit Select popup and click the matching option ──
        async def _pick(widget_selector: str, option_text: str, log_name: str) -> bool:
            try:
                await page.click(widget_selector, timeout=5000)
                await page.wait_for_timeout(700)
            except Exception as e:
                print(f"[Acuity NI] Could not open {log_name}: {e}")
                return False
            result = await page.evaluate(
                """([optText]) => {
                    const norm = s => s.trim().toLowerCase();
                    const target = norm(optText);
                    const containers = document.querySelectorAll(
                        '.dijitPopup, .dijitSelectMenu, .dijitMenu, [role="listbox"], .dijitComboBoxMenu'
                    );
                    for (const c of containers) {
                        const walker = document.createTreeWalker(c, NodeFilter.SHOW_ELEMENT);
                        let node;
                        while ((node = walker.nextNode())) {
                            if (norm(node.textContent) === target && node.childElementCount === 0) {
                                const r = node.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {
                                    node.click();
                                    return 'ok:' + node.tagName + ':' + node.className.slice(0, 30);
                                }
                            }
                        }
                    }
                    for (const c of containers) {
                        const walker = document.createTreeWalker(c, NodeFilter.SHOW_ELEMENT);
                        let node;
                        while ((node = walker.nextNode())) {
                            const t = norm(node.textContent);
                            if ((t.startsWith(target) || t.includes(target)) && node.childElementCount === 0) {
                                const r = node.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {
                                    node.click();
                                    return 'ok-contains:' + node.textContent.trim().slice(0, 30);
                                }
                            }
                        }
                    }
                    return 'not-found';
                }""",
                [option_text],
            )
            print(f"[Acuity NI] {log_name} → '{option_text}': {result}")
            if str(result).startswith("ok"):
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    await page.wait_for_timeout(1000)
                return True
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            return False

        # ── Business name ────────────────────────────────────────────────────
        # On Policy Selection, clBusinessName was already pre-filled.
        # Named Insured page has a separate insuredName field. Try known ids;
        # the diagnostic above will reveal the real id if these fail.
        name = profile.legal_business_name
        if not name and profile.first_name:
            name = f"{profile.first_name} {profile.last_name}".strip()

        name_filled = False
        for field_id in ["insuredName", "namedInsured", "insName", "businessName", "insured_name"]:
            try:
                await page.fill(f"#{field_id}", name, timeout=3000)
                print(f"[Acuity NI] business name filled via #{field_id}")
                name_filled = True
                break
            except Exception:
                pass
        if not name_filled:
            print(f"[Acuity NI] WARNING: could not fill business name '{name}' — check diagnostic above for correct field id")

        # ── Address ──────────────────────────────────────────────────────────
        # Named Insured address widgetids (from diagnostic): street, city, state, zipcode
        # Inner inputs have id = widgetid for Dijit TextBox.
        addr = profile.mailing_address

        if addr.street:
            for fid in ["street", "street1", "addr1", "address"]:
                try:
                    await page.fill(f"#{fid}", addr.street, timeout=3000)
                    print(f"[Acuity NI] street filled via #{fid}")
                    break
                except Exception:
                    pass

        if addr.city:
            for fid in ["city", "city1", "insuredCity"]:
                try:
                    await page.fill(f"#{fid}", addr.city, timeout=3000)
                    print(f"[Acuity NI] city filled via #{fid}")
                    break
                except Exception:
                    pass

        if addr.zip_code:
            for fid in ["zipcode", "zip", "zipCode", "postalCode"]:
                try:
                    await page.fill(f"#{fid}", addr.zip_code, timeout=3000)
                    print(f"[Acuity NI] zip filled via #{fid}")
                    break
                except Exception:
                    pass

        if addr.state:
            # State is a Dijit Select — use popup-click approach
            state_picked = await _pick("[widgetid='state']", addr.state, "NI State")
            if not state_picked:
                # Fallback: try native select
                try:
                    await page.select_option("select[name='state'], select[id='state']", label=addr.state)
                except Exception:
                    pass

        await self._fill_ni_contact_and_business(page, profile)
        await page.wait_for_timeout(500)

        # ── Click Next via JS (same pattern as NofB) ─────────────────────────
        next_result = await page.evaluate("""() => {
            const btn = document.getElementById('nextButton')
                || Array.from(document.querySelectorAll('input[value="Next"], button'))
                    .find(b => (b.value || b.textContent || '').trim() === 'Next');
            if (btn) { btn.click(); return 'ok:' + (btn.id || btn.value || btn.textContent.trim()); }
            return 'not_found';
        }""")
        print(f"[Acuity NI] Next click -> {next_result}")
        if next_result == 'not_found':
            await page.evaluate("() => { document.getElementById('nextButton')?.click(); }")

        await page.wait_for_timeout(3000)
        logger.info("[Acuity] Named Insured complete.")

    async def _fill_ni_contact_and_business(self, page: Page, profile: ProspectProfile) -> None:
        """Fill Business Phone, General Contact, Email, FEIN, employees, Owner on NI page."""

        # Business Phone — split 10 digits into (area, prefix, line).
        # Fallback to Allen Insurance agency line if profile.contact_phone is empty.
        def _split_phone(raw: str):
            digits = "".join(ch for ch in (raw or "") if ch.isdigit())
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) != 10:
                return None
            return digits[:3], digits[3:6], digits[6:]

        phone_parts = _split_phone(profile.contact_phone) or _split_phone("2072364311")
        area, pre, line = phone_parts
        try:
            await page.fill("#busareacode", area, timeout=3000)
            await page.fill("#busphonenbr1", pre, timeout=3000)
            await page.fill("#busphonenbr2", line, timeout=3000)
            print(f"[Acuity NI] business phone: ({area}) {pre}-{line}")
        except Exception as e:
            print(f"[Acuity NI] business phone FAILED: {e}")

        # General Business Contact name
        first = (profile.first_name or "").strip()
        last = (profile.last_name or "").strip()
        if not first and profile.contact_name:
            parts = profile.contact_name.strip().split(None, 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else last
        if first:
            try:
                await page.fill("#genBusContactFirstName", first, timeout=3000)
                print(f"[Acuity NI] contact first name: '{first}'")
            except Exception as e:
                print(f"[Acuity NI] contact first name FAILED: {e}")
        if last:
            try:
                await page.fill("#genBusContactLastName", last, timeout=3000)
                print(f"[Acuity NI] contact last name: '{last}'")
            except Exception as e:
                print(f"[Acuity NI] contact last name FAILED: {e}")

        # Cell Phone → always "Same as Business"
        try:
            result = await page.evaluate(
                """() => {
                    const el = document.getElementById('genBusContactPhoneSame');
                    if (!el) return 'not-found';
                    if (!el.checked) el.click();
                    return el.checked ? 'checked' : 'failed';
                }"""
            )
            print(f"[Acuity NI] cell phone Same as Business: {result}")
        except Exception as e:
            print(f"[Acuity NI] cell phone FAILED: {e}")

        # Email — fill from profile, else check None Available
        if profile.contact_email:
            try:
                await page.fill("#genBusContactEmail", profile.contact_email, timeout=3000)
                print(f"[Acuity NI] email: '{profile.contact_email}'")
            except Exception as e:
                print(f"[Acuity NI] email FAILED: {e}")
        else:
            try:
                result = await page.evaluate(
                    """() => {
                        const el = document.getElementById('genBusContactEmailNone');
                        if (!el) return 'not-found';
                        if (!el.checked) el.click();
                        return el.checked ? 'checked' : 'failed';
                    }"""
                )
                print(f"[Acuity NI] email None: {result}")
            except Exception as e:
                print(f"[Acuity NI] email None FAILED: {e}")

        # Business Information: FEIN, year started, employees
        if profile.fein:
            fein_digits = "".join(ch for ch in profile.fein if ch.isdigit())
            try:
                await page.fill("#fedIdSocSecNbr", fein_digits, timeout=3000)
                print(f"[Acuity NI] FEIN: '{fein_digits}'")
            except Exception as e:
                print(f"[Acuity NI] FEIN FAILED: {e}")
        if profile.year_established:
            try:
                await page.fill("#yearstarted", str(profile.year_established), timeout=3000)
                print(f"[Acuity NI] year started: {profile.year_established}")
            except Exception as e:
                print(f"[Acuity NI] year started FAILED: {e}")
        try:
            await page.fill("#nbrFullTimeEmp", str(profile.num_employees_ft or 0), timeout=3000)
            await page.fill("#nbrPartTimeEmp", str(profile.num_employees_pt or 0), timeout=3000)
            print(f"[Acuity NI] employees: FT={profile.num_employees_ft or 0}, PT={profile.num_employees_pt or 0}")
        except Exception as e:
            print(f"[Acuity NI] employees FAILED: {e}")

        # Business Owner: name + Same as Insured address
        if first:
            try:
                await page.fill("#busOwnerFirstName", first, timeout=3000)
                print(f"[Acuity NI] owner first name: '{first}'")
            except Exception as e:
                print(f"[Acuity NI] owner first name FAILED: {e}")
        if last:
            try:
                await page.fill("#busOwnerLastName", last, timeout=3000)
                print(f"[Acuity NI] owner last name: '{last}'")
            except Exception as e:
                print(f"[Acuity NI] owner last name FAILED: {e}")
        try:
            result = await page.evaluate(
                """() => {
                    const el = document.getElementById('busOwnerAddressSameAsIns');
                    if (!el) return 'not-found';
                    if (!el.checked) el.click();
                    return el.checked ? 'checked' : 'failed';
                }"""
            )
            print(f"[Acuity NI] owner Same as Insured: {result}")
        except Exception as e:
            print(f"[Acuity NI] owner Same as Insured FAILED: {e}")

    # ----- Save New Copy dialog -----

    async def _handle_save_dialog(self, page: Page, profile: ProspectProfile) -> None:
        """Handle the 'Save Name Entry' dialog that appears after Named Insured.

        Marker: the dialog has #continueButton (name=performSave, value=Continue).
        Acuity pre-fills Save Name with the legal business name, so we just click
        Continue. We also read back the current save name for resume_from state.
        """
        logger.info("[Acuity] Checking for Save Name dialog...")
        try:
            await page.wait_for_selector("#continueButton", timeout=15000)
        except PlaywrightTimeout:
            logger.info("[Acuity] No Save dialog appeared — continuing.")
            return

        # Read the pre-filled save name for resume_from persistence.
        save_name = ""
        try:
            save_name = await page.evaluate("""() => {
                const candidates = document.querySelectorAll(
                    "input[name*='aveName'], input[id*='aveName'], input[name*='quoteName']"
                );
                for (const el of candidates) {
                    if (el.offsetWidth > 0 && el.value) return el.value;
                }
                return '';
            }""")
        except Exception:
            pass
        if not save_name:
            save_name = profile.legal_business_name or profile.last_name or "Quote"

        try:
            await page.evaluate("() => { document.getElementById('continueButton')?.click(); }")
            await page.wait_for_timeout(3000)
            logger.info("[Acuity] Save dialog: clicked Continue (save name: '%s')", save_name)
            _save_state({"last_quote_name": save_name})
        except Exception as e:
            logger.info("[Acuity] Save dialog Continue click failed: %s", e)

    # ----- D&B Search -----

    async def _handle_dun_bradstreet(self, page: Page) -> None:
        """Handle the D&B search results page — skip or let it auto-match."""
        logger.info("[Acuity] Checking for D&B search results...")
        try:
            # Only actual submit buttons labeled Skip / None of the Above —
            # avoid matching 'value*=\"None\"' which collides with Suffix='None' textboxes.
            skip_btn = page.locator(
                "input[type='submit'][value='Skip'], "
                "input[type='button'][value='Skip'], "
                "input[type='submit'][value='None of the Above'], "
                "input[type='button'][value='None of the Above'], "
                "button:has-text('Skip'), "
                "button:has-text('None of the Above')"
            ).first
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
        title = await page.title()
        print(f"[Acuity] _fill_line_selection — page: '{title}'")

        # Program dropdown — default to "Deluxe" if available
        try:
            await page.select_option("select[name*='rogram'], select[name*='Program']", label="Deluxe")
        except Exception:
            pass  # default is fine

        # Liability Limits — map from GL details (guard for None gl)
        limits = (profile.gl.desired_limits if profile.gl else None) or "1M/2M"
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
                print(f"[Acuity] Liability limit = {limit_value}")
        except Exception as e:
            logger.warning("[Acuity] Could not set Liability Limit to '%s': %s", limit_value, e)

        # Subcontractors question — may appear here
        if profile.uses_subcontractors is False:
            try:
                if await page.locator("select[name*='ubcontract']").count() > 0:
                    await page.select_option("select[name*='ubcontract']", label="No")
            except Exception:
                pass
        elif profile.uses_subcontractors is True:
            if profile.pct_work_subcontracted and profile.pct_work_subcontracted > 50:
                logger.warning("[Acuity] >50%% subcontracted — may be ineligible for Bis-Pak.")

        await page.evaluate("() => { document.getElementById('nextButton')?.click(); }")
        await page.wait_for_timeout(3000)
        print("[Acuity] Line Selection done.")

    # ----- Location modal -----

    async def _fill_location(self, page: Page, profile: ProspectProfile) -> None:
        """Add location via the Location modal."""
        title = await page.title()
        print(f"[Acuity] _fill_location — page: '{title}'")

        # Click "Add Location" button
        try:
            await page.click(
                "input[value*='Add Location'], button:has-text('Add Location'), a:has-text('Add Location')",
                timeout=5000,
            )
            await page.wait_for_timeout(2000)
        except Exception:
            print("[Acuity] No 'Add Location' button — may already be on location form.")

        # Location address — use first location or mailing address
        loc = profile.locations[0] if profile.locations else None
        addr = loc.address if loc else profile.mailing_address

        if addr.street:
            try:
                await page.locator(
                    "input[name*='ddress'], input[name*='street'], input[name*='Address']"
                ).first.fill(addr.street)
            except Exception:
                pass

        if addr.city:
            try:
                await page.locator("input[name*='ity'], input[name*='City']").first.fill(addr.city)
            except Exception:
                pass

        if addr.state:
            try:
                # Try case-insensitive-ish partial match for state select
                await page.select_option(
                    "select[name*='tate'], select[name*='State'], select[name*='STATE']",
                    label=addr.state,
                )
            except Exception:
                pass

        if addr.zip_code:
            try:
                await page.locator(
                    "input[name*='ip'], input[name*='Zip'], input[name*='ZIP']"
                ).first.fill(addr.zip_code)
                await page.wait_for_timeout(1500)  # ZIP triggers territory lookup
            except Exception:
                pass

        # Territory / Protection Class — auto-filled from ZIP; just wait
        await page.wait_for_timeout(1000)

        # Click Add/Save to confirm location — prefer specific "Add Location" text
        for btn_sel in [
            "input[value='Add Location']",
            "button:has-text('Add Location')",
            "input[value='Add']",
            "button:has-text('Add')",
            "input[value='Save']",
        ]:
            try:
                await page.click(btn_sel, timeout=2000)
                await page.wait_for_timeout(3000)
                print(f"[Acuity] Location confirmed via '{btn_sel}'")
                break
            except Exception:
                continue

        print("[Acuity] Location done.")

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
        for btn_sel in [
            "input[value='Add Building']",
            "button:has-text('Add Building')",
            "input[value='Add']",
            "input[value='Save']",
        ]:
            try:
                await page.click(btn_sel, timeout=2000)
                await page.wait_for_timeout(3000)
                print(f"[Acuity] Building confirmed via '{btn_sel}'")
                break
            except Exception:
                continue

        # Add Liability Class modal may appear — click Add if present
        # (be specific: look for a button/input that contains "Liability" or matches exactly "Add")
        try:
            add_class_btn = page.locator(
                "button:has-text('Add Liability'), input[value='Add Liability'], "
                "button:has-text('Add Class'), input[value='Add Class']"
            )
            await add_class_btn.click(timeout=4000)
            await page.wait_for_timeout(2000)
            print("[Acuity] Add Liability Class clicked.")
        except PlaywrightTimeout:
            pass

        print("[Acuity] Building done.")

    # ----- Bis-Pak Unit Options -----

    async def _fill_unit_options(self, page: Page) -> None:
        """Bis-Pak Unit Options page — leave optional coverages at defaults."""
        logger.info("[Acuity] Bis-Pak Unit Options — leaving at defaults...")

        # Click Next to proceed (don't toggle any optional coverages)
        try:
            await page.evaluate("() => { document.getElementById('nextButton')?.click(); }")
            await page.wait_for_timeout(3000)
        except Exception:
            pass

    # ----- Bis-Pak Policy Options -----

    async def _fill_policy_options(self, page: Page) -> None:
        """Bis-Pak Policy Options page — leave optional coverages at defaults."""
        logger.info("[Acuity] Bis-Pak Policy Options — leaving at defaults...")

        try:
            await page.evaluate("() => { document.getElementById('nextButton')?.click(); }")
            await page.wait_for_timeout(3000)
        except Exception:
            pass

    # ----- Additional Interests -----

    async def _fill_additional_interests(self, page: Page) -> None:
        """Additional Interests page — skip (no additional insureds to add)."""
        logger.info("[Acuity] Additional Interests — skipping...")

        try:
            await page.evaluate("() => { document.getElementById('nextButton')?.click(); }")
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
        await page.evaluate("() => { document.getElementById('nextButton')?.click(); }")
        await page.wait_for_timeout(3000)
        logger.info("[Acuity] Loss History complete.")

    # ----- Additional Info: General Information -----

    async def _fill_general_info(self, page: Page, profile: ProspectProfile) -> None:
        """Fill the General Info page (max stories question)."""
        title = await page.title()
        print(f"[Acuity] _fill_general_info — page: '{title}'")

        # "Up to how many stories does the insured building have?"
        stories = (profile.property.number_of_stories if profile.property else None) or 1
        if stories >= 4:
            dropdown_value = "4+"
            logger.warning("[Acuity] Building has 4+ stories — may exceed Bis-Pak eligibility.")
        else:
            dropdown_value = str(stories)

        # Dump all selects so we can see what's on this page
        select_info = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('select')).map(s => ({
                id: s.id, name: s.name,
                opts: Array.from(s.options).map(o => o.text).slice(0, 8)
            }));
        }""")
        print(f"[Acuity] General Info selects: {select_info}")

        # Try to find the stories dropdown by matching option text
        set_stories = await page.evaluate(
            """([val]) => {
                const selects = Array.from(document.querySelectorAll('select'));
                for (const s of selects) {
                    const opt = Array.from(s.options).find(o => o.text.trim() === val);
                    if (opt) {
                        s.value = opt.value;
                        s.dispatchEvent(new Event('change', {bubbles: true}));
                        return 'ok:' + (s.id || s.name) + '=' + opt.text;
                    }
                }
                // Try numeric match (option text might include "1 Story" etc)
                const numVal = val.replace('+', '');
                for (const s of selects) {
                    const opt = Array.from(s.options).find(o => o.text.trim().startsWith(numVal));
                    if (opt) {
                        s.value = opt.value;
                        s.dispatchEvent(new Event('change', {bubbles: true}));
                        return 'ok-prefix:' + (s.id || s.name) + '=' + opt.text;
                    }
                }
                return 'not-found:' + val;
            }""",
            [dropdown_value],
        )
        print(f"[Acuity] Stories dropdown result: {set_stories}")

        # Click Next — should advance to Premium Summary
        await page.evaluate("() => { document.getElementById('nextButton')?.click(); }")
        await page.wait_for_timeout(5000)
        print("[Acuity] General Info done.")

    # ----- Premium Summary (STOP HERE) -----

    async def _scrape_premium(self, page: Page) -> Optional[Decimal]:
        """Scrape the premium from the Premium Summary page. Do NOT bind."""
        title = await page.title()
        print(f"[Acuity] _scrape_premium — page: '{title}'")

        # Wait for a premium-summary indicator — try multiple possible texts
        ready_selectors = [
            "text=Your quote is ready to bind",
            "text=ready to bind",
            "text=Premium Summary",
            "text=Annual Premium",
            "text=Total Premium",
        ]
        found_summary = False
        for sel in ready_selectors:
            try:
                await page.wait_for_selector(sel, timeout=15_000)
                print(f"[Acuity] Premium page confirmed via: {sel}")
                found_summary = True
                break
            except PlaywrightTimeout:
                continue

        if not found_summary:
            logger.warning("[Acuity] Did not confirm premium page — scraping best-effort.")

        # Dump full page text for diagnostics
        page_text = await page.evaluate("document.body.innerText")
        print(f"[Acuity] Premium page text (first 600 chars): {page_text[:600]}")

        # Try to find a dollar amount using TreeWalker to scan all text nodes
        premium_js = await page.evaluate("""() => {
            // Walk all text nodes and find dollar amounts
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const results = [];
            let node;
            while ((node = walker.nextNode())) {
                const txt = node.textContent.trim();
                // Match dollar amounts like $1,234.00 or $1,234
                const m = txt.match(/\\$([\\d,]+(?:\\.\\d{2})?)/);
                if (m) {
                    // Include the parent label for context
                    const label = node.parentElement ? node.parentElement.textContent.trim().slice(0, 80) : '';
                    results.push({amount: m[1], context: label});
                }
            }
            return results;
        }""")
        print(f"[Acuity] Dollar amounts on premium page: {premium_js}")

        # Look for the headline annual premium — prefer amounts near "Annual" or "Total" labels
        def _parse_decimal(s: str) -> Optional[Decimal]:
            try:
                return Decimal(s.replace(",", ""))
            except Exception:
                return None

        # Strategy 1: context-aware — find amount next to "Annual" or "Total" keyword
        for item in premium_js:
            ctx = item.get("context", "").lower()
            if any(kw in ctx for kw in ("annual", "total", "premium")):
                val = _parse_decimal(item["amount"])
                if val and val > 100:
                    print(f"[Acuity] Premium (context match): ${val}")
                    return val

        # Strategy 2: largest dollar amount on page (usually the headline premium)
        amounts = [_parse_decimal(i["amount"]) for i in premium_js]
        amounts = [a for a in amounts if a and a > 100]
        if amounts:
            premium = max(amounts)
            print(f"[Acuity] Premium (largest amount): ${premium}")
            return premium

        logger.warning("[Acuity] Could not parse premium amount from page.")
        return None
