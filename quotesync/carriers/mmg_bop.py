"""
MMG Insurance (Maine Mutual Group) — BOP Quote Adapter.

Automates the Business Owner Policy (BOP) quote flow on MMG's MaineGate portal.
Portal URL: https://agencyportal.mmgins.com/private/bop/portal/index

Flow:
1. Login at login.mmgins.com
2. Navigate to Start/Manage Quotes
3. Select Commercial > Business Owner > Continue
4. Start New Quote
5. Policy: General Info → Named Insured(s) → DBA(s) → Mailing Address
6. Locations: Add Location → Add Building (5 steps)
7. Coverages: Required → Optional → Tools & Equipment → Additional Insured → Credits
8. Additional Info: Underwriting → Inspection Contact
9. Summary (review only — adapter stops here)
10. Billing (user handles manually)
"""

from __future__ import annotations

from playwright.async_api import Page

from quotesync.carriers.base import CarrierAdapter
from quotesync.models.prospect import (
    InsuredType,
    ProspectProfile,
)

# Maps our EntityType enum values to the MMG dropdown text
MMG_ENTITY_MAP = {
    "Association": "Association",
    "Common Ownership": "Common Ownership",
    "Corporation": "Corporation",
    "Executor or Trustee": "Executor or Trustee",
    "Governmental Entity": "Governmental Entity",
    "Individual": "Individual",
    "Joint Employers": "Joint Employers",
    "Joint Venture": "Joint Venture",
    "Labor Union": "Labor Union",
    "Limited Liability Company (LLC)": "Limited Liability Company (LLC)",
    "Limited Liability Partnership": "Limited Liability Partnership",
    "Limited Partnership": "Limited Partnership",
    "Multiple Status": "Multiple Status",
    "Organization, including a Corporation (but not including a Partnership, Joint Venture or Limited Liability Company)": "Organization, including a Corporation (but not including a Partnership, Joint Venture or Limited Liability Company)",
    "Other": "Other",
    "Partnership": "Partnership",
    "Religious Organization": "Religious Organization",
    "Trust": "Trust",
    "Trust or Estate": "Trust or Estate",
}

# Maps our GL limits to the MMG native <select> option values
MMG_LIABILITY_LIMITS_MAP = {
    "500K/1M": "500000",
    "1M/2M": "1000000",
    "2M/4M": "2000000",
}

TIMEOUT = 30000  # ms — consistent timeout for all waits
NAV_SETTLE = 2000  # ms — short pause after clicks that trigger Angular re-renders


async def _handle_address_validation(page) -> None:
    """Handle the USPS address validation dialog (already known to be visible when called).

    Selects the "Specified Address" radio, clicks Save & Continue, then waits for the dialog
    to close. The caller is responsible for detecting that the dialog is present before calling.
    """
    try:
        await page.locator("input[type='radio']").first.click()
        await page.wait_for_timeout(500)
        await page.locator("button:has-text('Save & Continue')").first.click()
        # Wait for dialog to close
        await page.wait_for_selector("text=Specified Address", state="hidden", timeout=10000)
        print("[MMG] USPS address validation dialog handled — accepted specified address.")
    except Exception as e:
        print(f"[MMG] Warning: USPS dialog handling had an issue: {e}")


async def construction_select(page, value: str) -> None:
    """Select a Construction type from MMG's custom searchable dropdown.

    The trigger is a readonly input (id ends with '_selectedInput') that follows
    the 'Construction' label. Clicking it opens a search box. We type to filter,
    then press Enter to confirm the first (and only) filtered result.
    """
    # Use exact=True so we match the "Construction" label only, not "Construction Details"
    # in the sidebar. Then find the first readonly _selectedInput after that label.
    trigger = page.get_by_text("Construction", exact=True).locator(
        "xpath=following::input[@readonly][1]"
    )
    await trigger.click()

    await page.wait_for_selector(".dropdown-search-input", state="visible", timeout=10000)
    # Click the search input to ensure focus, then type character by character
    search = page.locator(".dropdown-search-input").first
    await search.click()
    await page.keyboard.type(value, delay=80)
    await page.wait_for_timeout(500)

    # Use page.keyboard.press — fires to the focused element without holding a
    # reference to the search input, which Angular may re-render mid-type.
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(500)

    # Verify something was selected (trigger input should no longer show placeholder)
    selected_val = await trigger.input_value()
    if selected_val and selected_val.strip():
        print(f"[MMG] Construction selected: '{selected_val}'")
        return

    # Fallback: JS click on the first visible leaf element matching the value
    # within the dropdown panel (sibling of the search input)
    clicked = await page.evaluate("""
        (val) => {
            const input = document.querySelector('.dropdown-search-input');
            if (!input) return 'no-input';
            let container = input.parentElement;
            for (let i = 0; i < 6; i++) {
                if (!container) break;
                const leaves = Array.from(container.querySelectorAll('*')).filter(
                    el => el.children.length === 0
                        && el.offsetParent !== null
                        && el.textContent.trim().toLowerCase().includes(val.toLowerCase())
                );
                if (leaves.length) {
                    leaves[0].click();
                    return 'js-clicked:' + leaves[0].textContent.trim();
                }
                container = container.parentElement;
            }
            return 'not-found';
        }
    """, value)
    print(f"[MMG] Construction JS fallback: {clicked}")
    await page.wait_for_timeout(400)

    selected_val = await trigger.input_value()
    if not selected_val or not selected_val.strip():
        raise ValueError(f"construction_select: '{value}' not selected (trigger still empty).")


async def choices_select(page, label: str, nth: int = -1) -> None:
    """Select an option in a Choices.js dropdown by its label text.

    The portal uses Choices.js for all dropdowns — native select_option does not work.
    nth=-1 targets the last .choices__inner on the page (most recently added context).
    Pass nth=0,1,2... to target a specific dropdown by index.
    Matches by inner text (case-insensitive substring) so partial matches work.
    """
    triggers = page.locator(".choices__inner")
    count = await triggers.count()
    index = nth if nth >= 0 else count - 1
    await triggers.nth(index).click()
    # Wait for the open dropdown specifically — Choices.js adds 'is-open' to the active dropdown
    await page.wait_for_selector(".choices.is-open .choices__item--selectable", timeout=TIMEOUT)
    options = page.locator(".choices.is-open .choices__item--selectable")
    opt_count = await options.count()
    available = []
    for i in range(opt_count):
        opt = options.nth(i)
        text = (await opt.inner_text()).strip()
        available.append(text)
        if label.lower() in text.lower():
            await opt.click()
            await page.wait_for_timeout(500)
            return
    # Close the open dropdown before raising
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)
    raise ValueError(
        f"choices_select: option '{label}' not found. Available options: {available}"
    )


class MMGBopAdapter(CarrierAdapter):
    """Adapter for MMG Insurance BOP (Business Owner Policy) quotes."""

    name = "MMG Insurance — BOP"
    login_url = "https://agencyportal.mmgins.com/private/bop/portal/index"

    async def login(self, page: Page) -> None:
        """Log into MMG's MaineGate portal."""
        # Portal redirects to login.mmgins.com with proper OIDC params
        await page.wait_for_selector("input[type='text'], input[type='email']", timeout=TIMEOUT)

        # Fill credentials
        inputs = await page.query_selector_all("input[type='text'], input[type='email']")
        if inputs:
            await inputs[0].fill(self.username)
        password_input = await page.query_selector("input[type='password']")
        if password_input:
            await password_input.fill(self.password)

        await page.locator("button:has-text('Login'), input[value='Login']").first.click()

        # Wait for OIDC redirect chain to fully complete before proceeding
        await page.wait_for_url("**/connect.mmgins.com/**", timeout=TIMEOUT)
        await page.wait_for_load_state("load", timeout=TIMEOUT)
        print("[MMG] Logged in successfully.")

    async def navigate_to_new_quote(self, page: Page) -> None:
        """Navigate from dashboard to start a new BOP quote."""
        # connect.mmgins.com is an Angular SPA whose server redirects all sub-routes to /.
        # Chrome marks the original navigation as ERR_ABORTED even though the app loads fine.
        # Swallow that specific error and wait for the actual page content instead.
        try:
            await page.goto("https://connect.mmgins.com/rating/quotes", wait_until="domcontentloaded")
        except Exception as e:
            if "ERR_ABORTED" not in str(e):
                raise
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT)

        # Wait for Angular form to render
        await page.wait_for_selector("text=Line of Business", timeout=TIMEOUT)

        # Ensure Commercial is selected (usually pre-selected)
        commercial_label = page.locator("label:has-text('Commercial')").first
        if await commercial_label.count() > 0:
            await commercial_label.click()

        # Select Line of Business — native select on connect.mmgins.com (not Choices.js)
        await page.locator("select").last.select_option(label="Business Owner")
        await page.wait_for_timeout(NAV_SETTLE)

        await page.locator("button:has-text('Continue')").first.click()

        # Wait for BOP Quote List
        await page.wait_for_selector("text=BOP Quote List", timeout=TIMEOUT)

        await page.locator("button:has-text('Start New Quote'), a:has-text('Start New Quote')").first.click()

        # Wait for the BOP quote form on agencyportal
        await page.wait_for_url("**/agencyportal.mmgins.com/**", timeout=TIMEOUT)
        await page.wait_for_load_state("load", timeout=TIMEOUT)
        await page.wait_for_selector("text=General Info", timeout=TIMEOUT)
        print("[MMG] New BOP quote started.")

    async def fill_quote(self, page: Page, profile: ProspectProfile) -> None:
        """Fill the entire BOP quote form."""
        await self._fill_general_info(page, profile)
        await self._fill_named_insured(page, profile)
        await self._fill_dba(page, profile)
        await self._fill_mailing_address(page, profile)
        await self._fill_location(page, profile)
        await self._fill_coverages(page, profile)
        # Additional Info is optional — some portal flows skip straight to Summary
        if await page.locator("text=Underwriting, text=Additional Info").count() > 0:
            await self._fill_additional_info(page, profile)
        else:
            print("[MMG] No Additional Info section found — skipping to Summary.")
        print("[MMG] Quote form filled. Review the Summary and Billing pages manually.")

    # --- Page-by-page fill methods ---

    async def _fill_general_info(self, page: Page, profile: ProspectProfile) -> None:
        """Policy > General Info page."""
        print("[MMG] Filling General Info...")
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        if profile.effective_date:
            date_str = profile.effective_date.strftime("%m/%d/%Y")
            # Try multiple selector patterns for the date field
            date_input = page.locator(
                "input[id*='effective'], input[name*='effective'], "
                "input[id*='date'], input[name*='date'], "
                "input[type='date']"
            ).first
            try:
                current_val = await date_input.input_value(timeout=5000)
                if not current_val:
                    await date_input.fill(date_str)
                # If already filled, leave it — portal defaults to today
            except Exception:
                print("[MMG] Effective date field not found — leaving portal default.")

        if profile.entity_type:
            entity_text = profile.entity_type.value
            await choices_select(page, entity_text)

        # Click Next and wait until we actually leave General Info
        await page.locator("button:has-text('Next')").first.click()
        await page.wait_for_function(
            "() => !document.body.innerText.includes('This field is required')",
            timeout=TIMEOUT
        )
        # Wait for unique Named Insured page content
        await page.wait_for_selector("text=+ Named Insured", timeout=TIMEOUT)

    async def _fill_named_insured(self, page: Page, profile: ProspectProfile) -> None:
        """Policy > Named Insured(s) page."""
        print("[MMG] Filling Named Insured...")
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        # Click the "+ Named Insured" button to open the form
        await page.locator("button:has-text('Named Insured')").first.click()
        await page.wait_for_selector("text=Add First Named Insured", timeout=TIMEOUT)

        is_org = profile.insured_type == InsuredType.ORGANIZATION or bool(
            profile.legal_business_name and not profile.first_name
        )

        if is_org:
            await page.locator("label", has_text="Organization").first.click()
            await page.wait_for_selector("text=Organization Name", timeout=TIMEOUT)

            # All inputs use GUID-based IDs — target by label text proximity
            await page.locator("text=Organization Name").locator("xpath=following::input[1]").fill(
                profile.legal_business_name
            )
            if profile.fein:
                await page.locator("text=FEIN").locator("xpath=following::input[1]").fill(profile.fein)
            if profile.contact_phone:
                await page.locator("text=Phone Number").locator("xpath=following::input[1]").fill(profile.contact_phone)
            if profile.contact_email:
                await page.locator("text=Email Address").locator("xpath=following::input[1]").fill(profile.contact_email)
        else:
            if profile.first_name:
                await page.locator("text=First Name").locator("xpath=following::input[1]").fill(profile.first_name)
            if profile.last_name:
                await page.locator("text=Last Name").locator("xpath=following::input[1]").fill(profile.last_name)
            if profile.date_of_birth:
                dob_str = profile.date_of_birth.strftime("%m/%d/%Y")
                await page.locator("text=Date of Birth").locator("xpath=following::input[1]").fill(dob_str)
            if profile.contact_phone:
                await page.locator("text=Phone Number").locator("xpath=following::input[1]").fill(profile.contact_phone)
            if profile.contact_email:
                await page.locator("text=Email Address").locator("xpath=following::input[1]").fill(profile.contact_email)

        await page.locator("button:has-text('Save & Continue')").first.click()
        await page.wait_for_timeout(NAV_SETTLE)

        await page.locator("button:has-text('Next')").first.click()
        # Wait for unique DBA page content (not sidebar text)
        await page.wait_for_selector("text=+ DBA", timeout=TIMEOUT)

    async def _fill_dba(self, page: Page, profile: ProspectProfile) -> None:
        """Policy > DBA(s) page."""
        print("[MMG] Filling DBA...")
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        if profile.dba:
            await page.locator("button:has-text('DBA')").first.click()
            await page.wait_for_timeout(NAV_SETTLE)
            # Input IDs are GUIDs — target by label proximity
            await page.locator("text=DBA Name, text=DBA").locator("xpath=following::input[1]").first.fill(profile.dba)
            await page.locator("button:has-text('Save')").first.click()
            await page.wait_for_timeout(NAV_SETTLE)

        await page.locator("button:has-text('Next')").first.click()
        # Wait for unique Mailing Address page content
        await page.wait_for_selector("text=Address Search", timeout=TIMEOUT)

    async def _fill_mailing_address(self, page: Page, profile: ProspectProfile) -> None:
        """Policy > Mailing Address page."""
        print("[MMG] Filling Mailing Address...")
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        addr = profile.mailing_address

        # Use exact text match to avoid matching "Address Search"
        await page.get_by_text("Address", exact=True).locator("xpath=following::input[1]").fill(addr.street)
        await page.get_by_text("City", exact=True).locator("xpath=following::input[1]").fill(addr.city)

        if addr.state:
            # State is a native <select class="form-control"> with abbreviation values
            await page.locator("select.form-control").first.select_option(value=addr.state)

        await page.get_by_text("Zip Code", exact=True).locator("xpath=following::input[1]").fill(addr.zip_code)

        # Tab off the Zip field so Angular fires its blur/change validation before we click Next
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(1000)

        await page.locator("button:has-text('Next')").first.click()

        # After clicking Next the portal calls the USPS API (can take 10–30s).
        # Poll until the Location list page is confirmed (Add Location button visible).
        # Handle the USPS dialog if it appears along the way, then keep polling.
        print("[MMG] Waiting for USPS validation or Location page...")
        for _ in range(40):
            await page.wait_for_timeout(2000)
            if await page.locator("text=Specified Address").count() > 0:
                await _handle_address_validation(page)
                continue  # keep polling — USPS dialog close triggers navigation, need to wait for it
            if await page.locator("button:has-text('Add Location')").count() > 0:
                break
            # Portal sometimes auto-opens the Add Location modal after navigation
            if await page.locator("text=Same as Mailing Address").count() > 0:
                print("[MMG] Add Location modal already open — skipping button click.")
                break
        else:
            raise TimeoutError("[MMG] Timed out waiting for Location list after Mailing Address Next")

    async def _fill_location(self, page: Page, profile: ProspectProfile) -> None:
        """Locations section — add location and building."""
        print("[MMG] Filling Location...")
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        # Modal may already be open (portal auto-opens it after navigation)
        if not await page.locator("text=Same as Mailing Address").is_visible():
            await page.locator("button:has-text('Add Location')").first.click()
            await page.locator("text=Same as Mailing Address").wait_for(state="visible", timeout=TIMEOUT)
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT)

        # Determine which address to use for the location
        loc = profile.locations[0] if profile.locations else None
        addr = profile.mailing_address if profile.mailing_is_primary_location else (
            loc.address if loc else profile.mailing_address
        )

        if profile.mailing_is_primary_location:
            # Try the "Same as Mailing Address" Angular Material toggle via JS (label click is unreliable)
            await page.evaluate("""
                () => {
                    const btn = document.querySelector('[role="switch"]');
                    if (btn) { btn.click(); return; }
                    const cb = document.querySelector('mat-slide-toggle input[type="checkbox"]');
                    if (cb) { cb.click(); return; }
                    const bar = document.querySelector('.mat-slide-toggle-bar, .mdc-switch__track');
                    if (bar) { bar.click(); return; }
                }
            """)
            await page.wait_for_timeout(1000)

        # Fill the modal address form using direct locators for each field.
        # Modal field order: Address → Suite → City → State → Zip → Distance to Hydrant
        # The modal is appended last in the DOM, so .last / .nth(-1) targets modal fields
        # over identical background-page fields.

        # Address — last input following an "Address" label
        all_address_inputs = page.get_by_text("Address", exact=True).locator("xpath=following::input[1]")
        addr_count = await all_address_inputs.count()
        address_field = all_address_inputs.nth(addr_count - 1) if addr_count > 0 else all_address_inputs.first
        await address_field.click()
        await address_field.fill(addr.street)
        await page.wait_for_timeout(300)

        # City — maxlength="255" distinguishes it from Zip (maxlength="10").
        # Address also has maxlength="255" but gets class "input-valid" after fill,
        # so .last among unfilled (Required) 255-char inputs = modal City field.
        city_field = page.locator("input[placeholder='(Required)'][maxlength='255']").last
        await city_field.click()
        await city_field.fill(addr.city)
        await city_field.press("Tab")
        await page.wait_for_timeout(300)

        # State — real click first so Angular registers focus, then select_option sets
        # the DOM value and fires Playwright's change event.
        # State select has maxlength="255"; Distance to Hydrant has maxlength="4000" — use
        # maxlength to distinguish them since both share the select-placeholder class.
        state_select = page.locator("select.select-placeholder[maxlength='255']").last
        await state_select.click()
        await state_select.select_option(value=addr.state)
        await page.wait_for_timeout(500)

        # Zip — maxlength="10" uniquely identifies it among (Required) text inputs.
        zip_field = page.locator("input[placeholder='(Required)'][maxlength='10']").last
        await zip_field.fill(addr.zip_code)
        await zip_field.press("Tab")

        await page.wait_for_timeout(500)

        # Distance to Hydrant — required native <select> below Zip Code in the modal
        # Use the select that follows the "Distance to Hydrant" label text
        try:
            dist_select = page.locator("text=Distance to Hydrant (Ft)").locator("xpath=following::select[1]")
            if await dist_select.count() == 0:
                dist_select = page.locator("text=Distance to Hydrant").locator("xpath=following::select[1]")
            if await dist_select.count() > 0:
                await dist_select.click()
                if loc and loc.distance_to_hydrant:
                    await dist_select.select_option(label=loc.distance_to_hydrant.value)
                else:
                    await dist_select.select_option(value="1 to 1,000 Ft")
                print("[MMG] Distance to Hydrant set.")
            else:
                print("[MMG] Distance to Hydrant dropdown not found — skipping.")
        except Exception as e:
            print(f"[MMG] Distance to Hydrant error: {e}")

        await page.locator("button:has-text('Save & Continue')").first.click()

        # Race: USPS dialog or "Add Building" button becoming visible after location save
        print("[MMG] Waiting for USPS validation or Building prompt...")
        for i in range(40):
            await page.wait_for_timeout(2000)
            if await page.locator("text=Specified Address").count() > 0:
                await _handle_address_validation(page)
                continue
            if await page.locator("button:has-text('Add Building')").count() > 0:
                break
            # If "This field is required" is still showing, the form didn't save — log and stop retrying
            if await page.locator("text=This field is required").count() > 0:
                print(f"[MMG] Location form has validation errors at poll {i} — check Distance to Hydrant")
                break
            if i % 5 == 0:
                print(f"[MMG] Still waiting for Building button... ({i*2}s elapsed)")
        else:
            raise TimeoutError("[MMG] Timed out waiting for Add Building/BPP after location save")

        await page.locator("button:has-text('Add Building')").first.click()
        await page.wait_for_selector("text=Building Details", timeout=TIMEOUT)

        await self._fill_building(page, profile)

        # After the building wizard closes we're back on the Locations page.
        # Use .last — the background page may have a hidden Next button earlier in the DOM.
        await page.locator("button:has-text('Next')").last.click()
        # Wait for the Coverages section to load
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
        print("[MMG] Navigated to Coverages section.")

    async def _fill_building(self, page: Page, profile: ProspectProfile) -> None:
        """Building wizard — 5 steps inside the Location."""
        prop = profile.property

        # Step 1: Building Details
        # All input IDs on agencyportal are GUIDs — must use label-proximity XPath throughout.
        print("[MMG] Filling Building Details (Step 1/5)...")
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        if prop.building_description:
            try:
                desc_label = page.locator("text=Building Description")
                desc_input = desc_label.locator("xpath=following::input[1]")
                desc_textarea = desc_label.locator("xpath=following::textarea[1]")
                if await desc_input.count() > 0:
                    await desc_input.first.fill(prop.building_description)
                elif await desc_textarea.count() > 0:
                    await desc_textarea.first.fill(prop.building_description)
            except Exception:
                print("[MMG] Building description field not found — skipping.")

        if prop.building_value:
            try:
                await page.locator("text=Building Limit").locator(
                    "xpath=following::input[1]"
                ).first.fill(str(int(prop.building_value)))
            except Exception:
                print("[MMG] Building value field not found — skipping.")

        if prop.bpp_value and prop.bpp_value > 0:
            try:
                await page.locator("text=Business Personal Property Limit").locator(
                    "xpath=following::input[1]"
                ).first.fill(str(int(prop.bpp_value)))
            except Exception:
                print("[MMG] BPP field not found — skipping.")

        if prop.annual_gross_receipts:
            try:
                await page.locator("text=Annual Gross Receipts").locator(
                    "xpath=following::input[1]"
                ).first.fill(str(int(prop.annual_gross_receipts)))
            except Exception:
                print("[MMG] Annual gross receipts field not found — skipping.")

        if prop.sprinklered:
            try:
                await page.locator("label:has-text('Automatic Sprinkler System')").first.click()
            except Exception:
                print("[MMG] Sprinkler checkbox not found — skipping.")

        # The modal's Next button is last in the DOM; .first hits the background page's hidden one
        await page.locator("button:has-text('Next')").last.click()
        await page.wait_for_selector("text=Construction Details", timeout=TIMEOUT)

        # Step 2: Construction Details
        print("[MMG] Filling Construction Details (Step 2/5)...")
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        if prop.construction_type:
            try:
                await construction_select(page, prop.construction_type.value)
                print(f"[MMG] Construction type set to '{prop.construction_type.value}'.")
            except Exception as e:
                print(f"[MMG] Construction type error: {e}")

        if prop.occupied_by:
            try:
                # Occupied By is a native <select> with value="O" (Owner) or "N" (Non-owner)
                _occupied_by_map = {"Owner Occupied": "O", "Non owner": "N"}
                select_val = _occupied_by_map.get(prop.occupied_by.value, "O")
                occ_select = page.locator("text=Occupied By").locator("xpath=following::select[1]")
                await occ_select.click()
                await occ_select.select_option(value=select_val)
                print(f"[MMG] Occupied By set to '{prop.occupied_by.value}' (value='{select_val}').")
            except Exception as e:
                print(f"[MMG] Occupied By error: {e}")

        if prop.year_built:
            try:
                await page.locator("text=Year Constructed").locator(
                    "xpath=following::input[1]"
                ).first.fill(str(prop.year_built))
            except Exception:
                print("[MMG] Year Constructed field not found — skipping.")

        if prop.square_footage:
            try:
                await page.locator("text=Area Square Feet").locator(
                    "xpath=following::input[1]"
                ).first.fill(str(prop.square_footage))
            except Exception:
                print("[MMG] Area Square Feet field not found — skipping.")

        if prop.number_of_stories:
            try:
                await page.locator("text=Number of Stories").locator(
                    "xpath=following::input[1]"
                ).first.fill(str(prop.number_of_stories))
            except Exception:
                print("[MMG] Number of stories field not found — skipping.")

        if prop.roof_year_updated:
            try:
                await page.locator("text=Roof (Year)").locator(
                    "xpath=following::input[1]"
                ).first.fill(str(prop.roof_year_updated))
            except Exception:
                print("[MMG] Roof (Year) field not found — skipping.")

        if prop.electrical_year_updated:
            try:
                await page.locator("text=Electrical (Year)").locator(
                    "xpath=following::input[1]"
                ).first.fill(str(prop.electrical_year_updated))
            except Exception:
                print("[MMG] Electrical (Year) field not found — skipping.")

        if prop.plumbing_year_updated:
            try:
                await page.locator("text=Plumbing (Year)").locator(
                    "xpath=following::input[1]"
                ).first.fill(str(prop.plumbing_year_updated))
            except Exception:
                print("[MMG] Plumbing (Year) field not found — skipping.")

        if prop.hvac_year_updated:
            try:
                await page.locator("text=Heating (Year)").locator(
                    "xpath=following::input[1]"
                ).first.fill(str(prop.hvac_year_updated))
            except Exception:
                print("[MMG] Heating (Year) field not found — skipping.")

        await page.locator("button:has-text('Next')").last.click()
        await page.wait_for_selector("text=Building Classification", timeout=TIMEOUT)

        # Step 3: Building Classification
        print("[MMG] Filling Building Classification (Step 3/5)...")
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        if prop.business_owner_class:
            try:
                await choices_select(page, prop.business_owner_class)
            except Exception:
                print(f"[MMG] Business class '{prop.business_owner_class}' not found — skipping.")

        # Safety checkboxes — click the label so Angular wires it correctly
        for label_text, flag in [
            ("Fire Extinguisher", prop.fire_extinguishers),
            ("Smoke Detector", prop.smoke_detectors),
            ("Security Camera", prop.security_cameras),
        ]:
            if flag:
                try:
                    await page.locator(f"label:has-text('{label_text}')").first.click()
                except Exception:
                    print(f"[MMG] '{label_text}' checkbox not found — skipping.")

        await page.locator("button:has-text('Next')").last.click()
        await page.wait_for_selector("text=Mortgagee", timeout=TIMEOUT)

        # Step 4: Mortgagee & Loss Payee
        print("[MMG] Mortgagee & Loss Payee (Step 4/5)...")
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        if profile.property.mortgagee_name and profile.property.mortgagee_name.lower() != "none":
            try:
                await page.locator("button:has-text('Add Mortgagee'), a:has-text('Add Mortgagee')").first.click()
                await page.wait_for_timeout(NAV_SETTLE)
                await page.locator("text=Mortgagee Name, text=Name").locator("xpath=following::input[1]").first.fill(
                    profile.property.mortgagee_name
                )
                if profile.property.mortgagee_address:
                    await page.locator("text=Address").locator("xpath=following::input[1]").first.fill(
                        profile.property.mortgagee_address
                    )
                await page.locator("button:has-text('Save')").first.click()
                await page.wait_for_timeout(NAV_SETTLE)
            except Exception:
                print("[MMG] Mortgagee entry failed — skipping.")

        await page.locator("button:has-text('Next')").last.click()
        # Wait for the Save & Continue button — it only appears on Step 5 (Optional Coverages).
        # "text=Optional Coverages" matches the sidebar and fires too early.
        await page.wait_for_selector("button:has-text('Save & Continue')", timeout=TIMEOUT)

        # Step 5: Optional Coverages — the final button is "Save & Continue" (not "Next")
        print("[MMG] Optional Coverages (Step 5/5) — leaving at defaults...")
        await page.locator("button:has-text('Save & Continue')").last.click()
        # Wait for the loading overlay to clear and the modal to close
        await page.wait_for_selector("button:has-text('Add Building')", timeout=TIMEOUT)
        print("[MMG] Building saved — returned to Locations page.")

    async def _fill_coverages(self, page: Page, profile: ProspectProfile) -> None:
        """Coverages section — Required, Optional, Tools, Additional Insured, Credits."""
        print("[MMG] Filling Coverages...")
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        # Required coverages — Liability & Medical Expenses is a native <select class="widgetElement">
        # Default is already $1,000,000 so only change if profile specifies otherwise.
        gl_value = MMG_LIABILITY_LIMITS_MAP.get(profile.gl.desired_limits, "1000000")
        try:
            gl_select = page.locator("select.widgetElement").first
            await gl_select.select_option(value=gl_value)
            print(f"[MMG] GL limit set to value='{gl_value}'.")
        except Exception as e:
            print(f"[MMG] GL limit not set — leaving at portal default. ({e})")

        # Click Next through remaining sub-pages at defaults
        for section_name in ["Optional", "Tools & Equipment", "Additional Insured", "Credits"]:
            await page.locator("button:has-text('Next')").last.click()
            await page.wait_for_timeout(NAV_SETTLE)
            print(f"[MMG] Coverages > {section_name} — leaving at defaults...")

        # Final Next — goes to Additional Info (Underwriting) or directly to Summary
        await page.locator("button:has-text('Next')").last.click()
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
        print(f"[MMG] After Coverages Next — current page: {page.url}")

    async def _fill_additional_info(self, page: Page, profile: ProspectProfile) -> None:
        """Additional Info > Underwriting and Inspection Contact."""
        print("[MMG] Filling Additional Info > Underwriting...")
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        # Currently insured?
        if profile.currently_insured is True:
            await page.locator("button:has-text('Yes')").first.click()
            await page.wait_for_timeout(NAV_SETTLE)

            if profile.agency_manages_current_policy is not None:
                answer = "Yes" if profile.agency_manages_current_policy else "No"
                buttons = page.locator(f"button:has-text('{answer}')")
                count = await buttons.count()
                # Click the second one (first was "currently insured" answer)
                await buttons.nth(min(1, count - 1)).click()
                await page.wait_for_timeout(NAV_SETTLE)

            if profile.current_carrier:
                await choices_select(page, profile.current_carrier)

            if profile.years_in_business:
                await choices_select(page, profile.years_in_business)

            if profile.continuous_coverage is not None:
                answer = "Yes" if profile.continuous_coverage else "No"
                buttons = page.locator(f"button:has-text('{answer}')")
                await buttons.last.click()
                await page.wait_for_timeout(NAV_SETTLE)

        elif profile.currently_insured is False:
            await page.locator("button:has-text('No')").first.click()
            await page.wait_for_timeout(NAV_SETTLE)

        # Cancellations/non-renewals — click the relevant No/Yes
        if profile.non_renewals_or_cancellations is not None:
            answer = "No" if not profile.non_renewals_or_cancellations else "Yes"
            await page.locator(f"button:has-text('{answer}')").first.click()
            await page.wait_for_timeout(NAV_SETTLE)

        # Felony
        if profile.convicted_of_felony is not None:
            answer = "No" if not profile.convicted_of_felony else "Yes"
            await page.locator(f"button:has-text('{answer}')").first.click()
            await page.wait_for_timeout(NAV_SETTLE)

        # Bankruptcy
        if profile.filed_bankruptcy is not None:
            answer = "No" if not profile.filed_bankruptcy else "Yes"
            await page.locator(f"button:has-text('{answer}')").first.click()
            await page.wait_for_timeout(NAV_SETTLE)

        # Operations description
        if profile.operations_description:
            await page.locator("textarea").first.fill(profile.operations_description)

        # Other businesses
        if profile.operates_other_businesses is not None:
            answer = "No" if not profile.operates_other_businesses else "Yes"
            await page.locator(f"button:has-text('{answer}')").first.click()
            await page.wait_for_timeout(NAV_SETTLE)

        # Losses past 3 years
        if profile.losses_past_3_years is not None:
            answer = "No" if not profile.losses_past_3_years else "Yes"
            await page.locator(f"button:has-text('{answer}')").first.click()
            await page.wait_for_timeout(NAV_SETTLE)

        # Next → Inspection Contact
        await page.locator("button:has-text('Next')").first.click()
        await page.wait_for_selector("text=Inspection Contact", timeout=TIMEOUT)
        await page.wait_for_load_state("load", timeout=TIMEOUT)

        # Select the first contact card
        print("[MMG] Selecting Inspection Contact...")
        contact_card = page.locator("[class*='contact'], [class*='card']").first
        if await contact_card.count() > 0:
            await contact_card.click()
            await page.wait_for_timeout(NAV_SETTLE)

        # Next → Summary
        await page.locator("button:has-text('Next')").first.click()
        await page.wait_for_selector("text=Summary", timeout=TIMEOUT)
        print("[MMG] Reached Summary page. Quote form is complete.")
