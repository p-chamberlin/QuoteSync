"""
Master prospect profile data model for QuoteSync.

This is the single source of truth for all prospect/risk data.
Carrier adapters pull from this profile and map fields to each portal's form.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --- Enums ---

class EntityType(str, Enum):
    LLC = "LLC"
    CORPORATION = "Corporation"
    SOLE_PROP = "Sole Proprietorship"
    PARTNERSHIP = "Partnership"
    S_CORP = "S-Corporation"
    NON_PROFIT = "Non-Profit"


class ConstructionType(str, Enum):
    FRAME = "Frame"
    JOISTED_MASONRY = "Joisted Masonry"
    MASONRY = "Masonry Non-Combustible"
    FIRE_RESISTIVE = "Fire Resistive"
    MODIFIED_FIRE_RESISTIVE = "Modified Fire Resistive"


class RiskClass(str, Enum):
    CONTRACTOR = "Contractors/Trades"
    RETAIL = "Retail/Habitational"
    PROFESSIONAL = "Professional Services"
    MANUFACTURER = "Manufacturers/Wholesalers"


class ClaimStatus(str, Enum):
    OPEN = "Open"
    CLOSED = "Closed"


class LineOfBusiness(str, Enum):
    GL = "General Liability"
    PROPERTY = "Commercial Property"
    WC = "Workers Compensation"
    AUTO = "Commercial Auto"
    UMBRELLA = "Umbrella"
    ENO = "Errors & Omissions"


# --- Sub-models ---

class Address(BaseModel):
    street: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""


class Location(BaseModel):
    address: Address = Field(default_factory=Address)
    owned_or_leased: str = ""  # "Owned" or "Leased"
    square_footage: Optional[int] = None
    year_built: Optional[int] = None


class Claim(BaseModel):
    date_of_loss: Optional[date] = None
    line_of_business: Optional[LineOfBusiness] = None
    description: str = ""
    amount_paid: Optional[float] = None
    amount_reserved: Optional[float] = None
    status: Optional[ClaimStatus] = None


class PriorInsurance(BaseModel):
    carrier_name: str = ""
    line_of_business: Optional[LineOfBusiness] = None
    annual_premium: Optional[float] = None
    expiration_date: Optional[date] = None


# --- GL specifics ---

class GLDetails(BaseModel):
    desired_limits: str = "1M/2M"
    products_completed_ops: Optional[bool] = None
    professional_services_rendered: Optional[bool] = None
    residential_vs_commercial_pct: str = ""  # e.g. "70/30"
    new_construction_vs_renovation_pct: str = ""
    work_over_3_stories: Optional[bool] = None
    xcu_exposure: Optional[bool] = None
    liquor_liability: Optional[bool] = None


# --- Property specifics ---

class PropertyDetails(BaseModel):
    building_value: Optional[float] = None
    bpp_value: Optional[float] = None  # Business Personal Property
    business_income_limit: Optional[float] = None
    construction_type: Optional[ConstructionType] = None
    square_footage: Optional[int] = None
    year_built: Optional[int] = None
    roof_year_updated: Optional[int] = None
    roof_material: str = ""
    electrical_year_updated: Optional[int] = None
    electrical_type: str = ""
    plumbing_year_updated: Optional[int] = None
    hvac_year_updated: Optional[int] = None
    sprinklered: Optional[bool] = None
    any_vacant_portions: Optional[bool] = None
    burglar_alarm: Optional[bool] = None
    fire_alarm: Optional[bool] = None
    central_station_monitored: Optional[bool] = None
    mortgagee_name: str = ""
    mortgagee_address: str = ""


# --- Class-specific questions ---

class ContractorDetails(BaseModel):
    trade_type: str = ""  # e.g. "General", "Electrical", "Plumbing"
    largest_single_job_value: Optional[float] = None
    pulls_own_permits: Optional[bool] = None
    works_as_gc_or_sub: str = ""  # "GC", "Sub", "Both"


class RetailHabitationalDetails(BaseModel):
    number_of_units: Optional[int] = None
    swimming_pool: Optional[bool] = None
    trampoline: Optional[bool] = None
    playground: Optional[bool] = None
    hours_of_operation: str = ""
    food_service: Optional[bool] = None


class ProfessionalDetails(BaseModel):
    service_type: str = ""
    eno_exposure: Optional[bool] = None
    provides_advice_design_consulting: Optional[bool] = None


class ManufacturerDetails(BaseModel):
    products_manufactured: str = ""
    sold_under_own_label: Optional[bool] = None
    products_sold_outside_us: Optional[bool] = None
    flammable_hazardous_materials: Optional[bool] = None


# --- Master Profile ---

class ProspectProfile(BaseModel):
    """The single source of truth for a prospect/risk."""

    # 1. Named Insured & Business Info
    legal_business_name: str = ""
    dba: str = ""
    fein: str = ""
    entity_type: Optional[EntityType] = None
    year_established: Optional[int] = None
    website: str = ""
    contact_name: str = ""
    contact_title: str = ""
    contact_phone: str = ""
    contact_email: str = ""
    risk_class: Optional[RiskClass] = None

    # 2. Addresses & Locations
    mailing_address: Address = Field(default_factory=Address)
    mailing_is_primary_location: Optional[bool] = None
    locations: list[Location] = Field(default_factory=list)

    # 3. Operations
    operations_description: str = ""
    sic_naics_code: str = ""
    states_of_operation: list[str] = Field(default_factory=list)
    work_outside_us: Optional[bool] = None
    uses_subcontractors: Optional[bool] = None
    pct_work_subcontracted: Optional[int] = None
    requires_sub_certificates: Optional[bool] = None

    # 4. Revenue & Payroll
    gross_revenue_current: Optional[float] = None
    gross_revenue_prior: Optional[float] = None
    total_annual_payroll: Optional[float] = None
    num_employees_ft: Optional[int] = None
    num_employees_pt: Optional[int] = None
    num_owners_officers: Optional[int] = None

    # 5. GL Specifics
    gl: GLDetails = Field(default_factory=GLDetails)

    # 6. Property Specifics
    property: PropertyDetails = Field(default_factory=PropertyDetails)

    # 7. Class-Specific
    contractor: ContractorDetails = Field(default_factory=ContractorDetails)
    retail_habitational: RetailHabitationalDetails = Field(default_factory=RetailHabitationalDetails)
    professional: ProfessionalDetails = Field(default_factory=ProfessionalDetails)
    manufacturer: ManufacturerDetails = Field(default_factory=ManufacturerDetails)

    # 8. Loss History
    claims: list[Claim] = Field(default_factory=list)
    any_claims_over_25k: Optional[bool] = None
    currently_insured: Optional[bool] = None
    reason_for_shopping: str = ""

    # 9. Prior Insurance
    prior_insurance: list[PriorInsurance] = Field(default_factory=list)
    coverage_gaps_last_3_years: Optional[bool] = None
    non_renewals_or_cancellations: Optional[bool] = None

    # Desired effective date
    effective_date: Optional[date] = None
