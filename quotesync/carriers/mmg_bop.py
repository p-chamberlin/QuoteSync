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

# Maps our GL limits to the MMG dropdown values
MMG_LIABILITY_LIMITS_MAP = {
    "500K/1M": "$500,000",
    "1M/2M": "$1,000,000",
    "2M/4M": "$2,000,000",
}


class MMGBopAdapter(CarrierAdapter):
    """Adapter for MMG Insurance BOP (Business Owner Policy) quotes."""

    name = "MMG Insurance — BOP"
    login_url = "https://login.mmgins.com"

    async def login(self, page: Page) -> None:
        """Log into MMG's MaineGate portal."""
        # Wait for login form to load
        await page.wait_for_selector("input[type='text'], input[type='email']", timeout=15000)

        # Fill credentials
        inputs = await page.query_selector_all("input[type='text'], input[type='email']")
        if inputs:
            await inputs[0].fill(self.username)
        password_input = await page.query_selector("input[type='password']")
        if password_input:
            await password_input.fill(self.password)

        # Click Login button
        await page.click("button:has-text('Login'), input[value='Login']")

        # Wait for redirect to dashboard
        await page.wait_for_url("**/connect.mmgins.com/**", timeout=30000)
        print("[MMG] Logged in successfully.")

    async def navigate_to_new_quote(self, page: Page) -> None:
        """Navigate from dashboard to start a new BOP quote."""
        # Click "Start / Manage Quotes"
        await page.click("a:has-text('Start / Manage Quotes')")
        await page.wait_for_url("**/rating/quotes**", timeout=15000)

        # Select Commercial radio and Business Owner from Line of Business
        await page.click("input[value='Commercial'], label:has-text('Commercial')")
        await page.select_option("select", label="Business Owner")
        await page.click("button:has-text('Continue'), input[value='Continue']")

        # Wait for BOP Quote List
        await page.wait_for_selector("text=BOP Quote List", timeout=15000)

        # Click Start New Quote
        await page.click("button:has-text('Start New Quote'), a:has-text('Start New Quote')")

        # Wait for the quote form to load
        await page.wait_for_url("**/agencyportal.mmgins.com/**", timeout=30000)
        await page.wait_for_selector("text=General Info", timeout=15000)
        print("[MMG] New BOP quote started.")

    async def fill_quote(self, page: Page, profile: ProspectProfile) -> None:
        """Fill the entire BOP quote form."""
        await self._fill_general_info(page, profile)
        await self._fill_named_insured(page, profile)
        await self._fill_dba(page, profile)
        await self._fill_mailing_address(page, profile)
        await self._fill_location(page, profile)
        await self._fill_coverages(page, profile)
        await self._fill_additional_info(page, profile)
        # Stop at Summary — user reviews and decides to bind or save
        print("[MMG] Quote form filled. Review the Summary and Billing pages manually.")

    # --- Page-by-page fill methods ---

    async def _fill_general_info(self, page: Page, profile: ProspectProfile) -> None:
        """Policy > General Info page."""
        print("[MMG] Filling General Info...")

        # Effective date
        if profile.effective_date:
            date_str = profile.effective_date.strftime("%m/%d/%Y")
            date_input = page.locator("input[id*='effective'], input[name*='effective']").first
            await date_input.clear()
            await date_input.fill(date_str)

        # Entity type
        if profile.entity_type:
            entity_text = profile.entity_type.value
            await page.select_option("select", label=entity_text)

        # Click Next
        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=Named Insured", timeout=15000)

    async def _fill_named_insured(self, page: Page, profile: ProspectProfile) -> None:
        """Policy > Named Insured(s) page."""
        print("[MMG] Filling Named Insured...")

        # Click "+ Named Insured"
        await page.click("button:has-text('Named Insured')")
        await page.wait_for_selector("text=Add First Named Insured", timeout=10000)

        is_org = profile.insured_type == InsuredType.ORGANIZATION or bool(profile.legal_business_name and not profile.first_name)

        if is_org:
            # Click Organization radio
            org_radio = page.locator("text=Organization").first
            await org_radio.click()
            await page.wait_for_selector("text=Organization Name", timeout=5000)

            # Organization Name
            await page.fill("input[id*='organization'], input[name*='organization']", profile.legal_business_name)

            # FEIN
            if profile.fein:
                fein_input = page.locator("input[id*='fein'], input[id*='tin']").first
                await fein_input.fill(profile.fein)

            # Phone
            if profile.contact_phone:
                phone_input = page.locator("input[id*='phone'], input[name*='phone']").first
                await phone_input.fill(profile.contact_phone)

            # Email
            if profile.contact_email:
                email_input = page.locator("input[id*='email'], input[name*='email']").first
                await email_input.fill(profile.contact_email)

        else:
            # Individual mode (default)
            if profile.first_name:
                await page.fill("input[id*='first'], input[name*='first']", profile.first_name)
            if profile.last_name:
                await page.fill("input[id*='last'], input[name*='last']", profile.last_name)
            if profile.date_of_birth:
                dob_str = profile.date_of_birth.strftime("%m/%d/%Y")
                dob_input = page.locator("input[id*='birth'], input[name*='birth']").first
                await dob_input.fill(dob_str)
            if profile.contact_phone:
                phone_input = page.locator("input[id*='phone'], input[name*='phone']").first
                await phone_input.fill(profile.contact_phone)
            if profile.contact_email:
                email_input = page.locator("input[id*='email'], input[name*='email']").first
                await email_input.fill(profile.contact_email)

        # Click Save & Continue
        await page.click("button:has-text('Save & Continue')")
        await page.wait_for_timeout(2000)

        # Click Next to go to DBA(s)
        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=DBA", timeout=10000)

    async def _fill_dba(self, page: Page, profile: ProspectProfile) -> None:
        """Policy > DBA(s) page."""
        print("[MMG] Filling DBA...")

        if profile.dba:
            await page.click("button:has-text('DBA')")
            await page.wait_for_timeout(1000)
            dba_input = page.locator("input[id*='dba'], input[name*='dba']").first
            await dba_input.fill(profile.dba)
            await page.click("button:has-text('Save')")
            await page.wait_for_timeout(1000)

        # Click Next to Mailing Address
        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=Mailing Address", timeout=10000)

    async def _fill_mailing_address(self, page: Page, profile: ProspectProfile) -> None:
        """Policy > Mailing Address page."""
        print("[MMG] Filling Mailing Address...")

        addr = profile.mailing_address

        # Fill address fields directly (more reliable than autocomplete)
        address_input = page.locator("input[id*='address'], input[name*='address']").first
        await address_input.fill(addr.street)

        city_input = page.locator("input[id*='city'], input[name*='city']").first
        await city_input.fill(addr.city)

        # State dropdown
        if addr.state:
            await page.select_option("select[id*='state'], select[name*='state']", label=addr.state)

        zip_input = page.locator("input[id*='zip'], input[name*='zip']").first
        await zip_input.fill(addr.zip_code)

        # Click Next to Locations
        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=Location", timeout=10000)

    async def _fill_location(self, page: Page, profile: ProspectProfile) -> None:
        """Locations section — add location and building."""
        print("[MMG] Filling Location...")

        # Click Add Location
        await page.click("button:has-text('Add Location')")
        await page.wait_for_selector("text=Add Location", timeout=10000)

        # Check if same as mailing address
        if profile.mailing_is_primary_location:
            toggle = page.locator("text=Same as Mailing Address").locator("..").locator("input, button, [role='switch']").first
            await toggle.click()
        else:
            # Fill location address from first location or mailing address
            loc = profile.locations[0] if profile.locations else None
            addr = loc.address if loc else profile.mailing_address

            address_input = page.locator("input[id*='address'], input[name*='address']").first
            await address_input.fill(addr.street)

            city_input = page.locator("input[id*='city'], input[name*='city']").first
            await city_input.fill(addr.city)

            if addr.state:
                await page.select_option("select[id*='state'], select[name*='state']", label=addr.state)

            zip_input = page.locator("input[id*='zip'], input[name*='zip']").first
            await zip_input.fill(addr.zip_code)

        # Distance to Hydrant
        loc = profile.locations[0] if profile.locations else None
        if loc and loc.distance_to_hydrant:
            await page.select_option("select", label=loc.distance_to_hydrant.value)

        # Save & Continue
        await page.click("button:has-text('Save & Continue')")
        await page.wait_for_timeout(2000)

        # Now add Building/BPP
        await page.click("button:has-text('Add Building/BPP')")
        await page.wait_for_selector("text=Building Details", timeout=10000)

        await self._fill_building(page, profile)

        # After building wizard, back on Locations page — click Next to Coverages
        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=Coverages", timeout=10000)

    async def _fill_building(self, page: Page, profile: ProspectProfile) -> None:
        """Building wizard — 5 steps inside the Location."""
        prop = profile.property

        # Step 1: Building Details
        print("[MMG] Filling Building Details (Step 1/5)...")
        if prop.building_description:
            desc_input = page.locator("input[id*='description'], textarea[id*='description']").first
            await desc_input.fill(prop.building_description)

        if prop.building_value:
            limit_input = page.locator("input[id*='building'][id*='limit'], input[name*='building']").first
            await limit_input.fill(str(int(prop.building_value)))

        if prop.bpp_value:
            bpp_input = page.locator("input[id*='personal'], input[name*='bpp']").first
            await bpp_input.fill(str(int(prop.bpp_value)))

        if prop.annual_gross_receipts:
            receipts_input = page.locator("input[id*='receipt'], input[id*='gross']").first
            await receipts_input.fill(str(int(prop.annual_gross_receipts)))

        if prop.sprinklered:
            sprinkler_checkbox = page.locator("text=Automatic Sprinkler").locator("..").locator("input[type='checkbox']").first
            await sprinkler_checkbox.check()

        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=Construction Details", timeout=10000)

        # Step 2: Construction Details
        print("[MMG] Filling Construction Details (Step 2/5)...")
        if prop.construction_type:
            await page.select_option("select", label=prop.construction_type.value)

        if prop.occupied_by:
            occupied_selects = page.locator("select")
            count = await occupied_selects.count()
            for i in range(count):
                select = occupied_selects.nth(i)
                options_text = await select.inner_text()
                if "Owner" in options_text:
                    await select.select_option(label=prop.occupied_by.value)
                    break

        if prop.year_built:
            year_input = page.locator("input[id*='constructed'], input[id*='year']").first
            await year_input.fill(str(prop.year_built))

        if prop.square_footage:
            sqft_input = page.locator("input[id*='square'], input[id*='area']").first
            await sqft_input.fill(str(prop.square_footage))

        if prop.roof_year_updated:
            roof_input = page.locator("input[id*='roof']").first
            await roof_input.fill(str(prop.roof_year_updated))

        if prop.number_of_stories:
            stories_input = page.locator("input[id*='stories'], input[id*='stor']").first
            await stories_input.fill(str(prop.number_of_stories))

        # Electrical section
        if prop.electrical_year_updated:
            elec_year_input = page.locator("input[id*='electrical'][id*='year'], input[name*='electrical']").first
            await elec_year_input.fill(str(prop.electrical_year_updated))

        # Plumbing section
        if prop.plumbing_year_updated:
            plumb_input = page.locator("input[id*='plumbing']").first
            await plumb_input.fill(str(prop.plumbing_year_updated))

        # Heating section
        if prop.hvac_year_updated:
            heat_input = page.locator("input[id*='heating'][id*='year']").first
            await heat_input.fill(str(prop.hvac_year_updated))

        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=Building Classification", timeout=10000)

        # Step 3: Building Classification
        print("[MMG] Filling Building Classification (Step 3/5)...")
        if prop.business_owner_class:
            await page.select_option("select", label=prop.business_owner_class)

        # Checkboxes
        if prop.fire_extinguishers:
            await page.check("text=Fire Extinguishers")
        if prop.smoke_detectors:
            await page.check("text=Smoke Detectors")
        if prop.security_cameras:
            await page.check("text=Security Cameras")

        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=Mortgagee", timeout=10000)

        # Step 4: Mortgagee & Loss Payee — skip unless data provided
        print("[MMG] Mortgagee & Loss Payee (Step 4/5) — skipping unless data provided...")
        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=Optional Coverages", timeout=10000)

        # Step 5: Optional Coverages — leave at defaults
        print("[MMG] Optional Coverages (Step 5/5) — leaving at defaults...")
        await page.click("button:has-text('Save & Continue')")
        await page.wait_for_timeout(2000)

    async def _fill_coverages(self, page: Page, profile: ProspectProfile) -> None:
        """Coverages section — Required, Optional, Tools, Additional Insured, Credits."""
        print("[MMG] Filling Coverages...")

        # Required coverages — set liability limit
        gl_limit = MMG_LIABILITY_LIMITS_MAP.get(profile.gl.desired_limits, "$1,000,000")
        liability_select = page.locator("select").first
        await liability_select.select_option(label=gl_limit)

        # Click Next through remaining coverage sub-pages (leave at defaults)
        for section_name in ["Optional", "Tools & Equipment", "Additional Insured", "Credits"]:
            await page.click("button:has-text('Next')")
            await page.wait_for_timeout(2000)
            print(f"[MMG] Coverages > {section_name} — leaving at defaults...")

        # After Credits, Next goes to Additional Info
        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=Additional Info", timeout=10000)

    async def _fill_additional_info(self, page: Page, profile: ProspectProfile) -> None:
        """Additional Info > Underwriting and Inspection Contact."""
        print("[MMG] Filling Additional Info > Underwriting...")

        # Currently insured? (Yes/No buttons)
        if profile.currently_insured is True:
            await page.click("button:has-text('Yes')")
            await page.wait_for_timeout(1000)
            # Conditional: Does your agency manage the policy?
            if profile.agency_manages_current_policy is not None:
                answer = "Yes" if profile.agency_manages_current_policy else "No"
                buttons = page.locator(f"button:has-text('{answer}')")
                if await buttons.count() > 1:
                    await buttons.nth(1).click()
            # Current carrier dropdown
            if profile.current_carrier:
                await page.select_option("select", label=profile.current_carrier)
            # Years in business
            if profile.years_in_business:
                selects = page.locator("select")
                count = await selects.count()
                for i in range(count):
                    s = selects.nth(i)
                    text = await s.inner_text()
                    if "years" in text.lower() or "10+" in text:
                        await s.select_option(label=profile.years_in_business)
                        break
            # Continuous coverage
            if profile.continuous_coverage is not None:
                answer = "Yes" if profile.continuous_coverage else "No"
                await page.click(f"button:has-text('{answer}')")
        elif profile.currently_insured is False:
            await page.click("button:has-text('No')")

        # Cancellations/non-renewals
        if profile.non_renewals_or_cancellations is not None:
            answer = "No" if not profile.non_renewals_or_cancellations else "Yes"
            await page.click(f"button:has-text('{answer}')")

        # Felony
        if profile.convicted_of_felony is not None:
            answer = "No" if not profile.convicted_of_felony else "Yes"
            await page.click(f"button:has-text('{answer}')")

        # Bankruptcy
        if profile.filed_bankruptcy is not None:
            answer = "No" if not profile.filed_bankruptcy else "Yes"
            await page.click(f"button:has-text('{answer}')")

        # Operations description
        if profile.operations_description:
            textarea = page.locator("textarea").first
            await textarea.fill(profile.operations_description)

        # Other businesses
        if profile.operates_other_businesses is not None:
            answer = "No" if not profile.operates_other_businesses else "Yes"
            await page.click(f"button:has-text('{answer}')")

        # Losses past 3 years
        if profile.losses_past_3_years is not None:
            answer = "No" if not profile.losses_past_3_years else "Yes"
            await page.click(f"button:has-text('{answer}')")

        # Click Next to Inspection Contact
        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=Inspection Contact", timeout=10000)

        # Inspection Contact — just select the first contact card
        print("[MMG] Selecting Inspection Contact...")
        contact_card = page.locator("[class*='contact'], [class*='card']").first
        if await contact_card.count() > 0:
            await contact_card.click()

        # Click Next to Summary
        await page.click("button:has-text('Next')")
        await page.wait_for_selector("text=Summary", timeout=10000)
        print("[MMG] Reached Summary page. Quote form is complete.")
