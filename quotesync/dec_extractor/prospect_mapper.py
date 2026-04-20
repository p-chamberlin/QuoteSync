"""
Maps extracted dec page data to QuoteSync's ProspectProfile model.

This is the integration bridge: once a dec page is extracted, the resulting
ProspectProfile can be loaded directly into QuoteSync for carrier quoting.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from quotesync.models.prospect import (
    Address,
    Claim,
    ClaimStatus,
    EntityType,
    GLDetails,
    LineOfBusiness,
    Location,
    PriorInsurance,
    PropertyDetails,
    ProspectProfile,
)

# ---------------------------------------------------------------------------
# Entity type normalization
# ---------------------------------------------------------------------------

_ENTITY_MAP: dict[str, EntityType] = {
    "llc": EntityType.LLC,
    "limited liability company": EntityType.LLC,
    "corporation": EntityType.CORPORATION,
    "corp": EntityType.CORPORATION,
    "inc": EntityType.CORPORATION,
    "incorporated": EntityType.CORPORATION,
    "individual": EntityType.INDIVIDUAL,
    "sole proprietor": EntityType.INDIVIDUAL,
    "sole proprietorship": EntityType.INDIVIDUAL,
    "partnership": EntityType.PARTNERSHIP,
    "limited partnership": EntityType.LIMITED_PARTNERSHIP,
    "lp": EntityType.LIMITED_PARTNERSHIP,
    "llp": EntityType.LLP,
    "limited liability partnership": EntityType.LLP,
    "trust": EntityType.TRUST,
    "association": EntityType.ASSOCIATION,
    "joint venture": EntityType.JOINT_VENTURE,
}


def _parse_entity_type(raw: str | None) -> EntityType | None:
    if not raw:
        return None
    key = raw.strip().lower()
    # Exact match first
    if key in _ENTITY_MAP:
        return _ENTITY_MAP[key]
    # Partial match
    for k, v in _ENTITY_MAP.items():
        if k in key:
            return v
    return None


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            from datetime import datetime
            return datetime.strptime(raw, fmt).date()
        except (ValueError, TypeError):
            pass
    return None


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------

def _parse_address(raw: dict | None) -> Address:
    if not raw:
        return Address()
    return Address(
        street=raw.get("street") or "",
        city=raw.get("city") or "",
        state=raw.get("state") or "",
        zip_code=raw.get("zip_code") or "",
    )


# ---------------------------------------------------------------------------
# GL limits
# ---------------------------------------------------------------------------

def _parse_gl(raw: dict | None) -> GLDetails:
    if not raw:
        return GLDetails()
    occ = raw.get("each_occurrence")
    agg = raw.get("general_aggregate")
    if occ and agg:
        occ_str = f"${int(occ):,}" if isinstance(occ, (int, float)) else str(occ)
        agg_str = f"${int(agg):,}" if isinstance(agg, (int, float)) else str(agg)
        limits = f"{occ_str}/{agg_str}"
    elif occ:
        limits = f"${int(occ):,}" if isinstance(occ, (int, float)) else str(occ)
    else:
        limits = "1M/2M"  # default
    return GLDetails(desired_limits=limits)


# ---------------------------------------------------------------------------
# Claim parsing
# ---------------------------------------------------------------------------

def _parse_claims(raw: list[dict] | None) -> list[Claim]:
    if not raw:
        return []
    claims = []
    for c in raw:
        claims.append(
            Claim(
                date_of_loss=_parse_date(c.get("date")),
                description=c.get("description") or "",
                amount_paid=c.get("amount"),
            )
        )
    return claims


# ---------------------------------------------------------------------------
# Main mapper
# ---------------------------------------------------------------------------

def to_prospect_profile(extracted: dict[str, Any]) -> ProspectProfile:
    """
    Convert extracted dec page data (from extract_dec_page) to a ProspectProfile.

    The ProspectProfile can then be saved as JSON and loaded into QuoteSync
    for automated carrier portal submissions.
    """
    addr = _parse_address(extracted.get("mailing_address"))

    locations: list[Location] = []
    for loc in extracted.get("locations") or []:
        locations.append(Location(address=_parse_address(loc)))

    # Prior insurance from the dec page itself
    prior: list[PriorInsurance] = []
    carrier = extracted.get("carrier_name")
    premium = extracted.get("total_premium")
    exp = _parse_date(extracted.get("expiration_date"))
    if carrier:
        prior.append(
            PriorInsurance(
                carrier_name=carrier,
                annual_premium=premium,
                expiration_date=exp,
            )
        )

    # Property details
    prop_raw = extracted.get("property_limits") or {}
    prop = PropertyDetails(
        building_value=prop_raw.get("building"),
        bpp_value=prop_raw.get("bpp"),
        business_income_limit=prop_raw.get("business_income"),
        property_deductible=prop_raw.get("deductible") or "$1,000",
    )

    profile = ProspectProfile(
        # Named insured
        legal_business_name=extracted.get("named_insured") or "",
        dba=extracted.get("dba") or "",
        fein=extracted.get("fein") or "",
        entity_type=_parse_entity_type(extracted.get("entity_type")),
        year_established=extracted.get("year_established"),

        # Address & locations
        mailing_address=addr,
        locations=locations,

        # Operations
        operations_description=extracted.get("operations_description") or "",
        sic_naics_code=extracted.get("sic_code") or extracted.get("naics_code") or "",

        # Revenue & staffing
        gross_revenue_current=extracted.get("gross_receipts"),
        total_annual_payroll=extracted.get("annual_payroll"),
        num_employees_ft=extracted.get("num_employees"),

        # Coverage details
        gl=_parse_gl(extracted.get("gl_limits")),
        property=prop,

        # Loss history
        claims=_parse_claims(extracted.get("claims_history")),
        losses_past_3_years=bool(extracted.get("claims_history")),

        # Dates
        effective_date=_parse_date(extracted.get("effective_date")),

        # Prior insurance (the carrier on the dec page)
        prior_insurance=prior,
        current_carrier=carrier or "",
        currently_insured=True if carrier else None,
    )

    return profile
