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
    ASSOCIATION = "Association"
    COMMON_OWNERSHIP = "Common Ownership"
    CORPORATION = "Corporation"
    EXECUTOR_OR_TRUSTEE = "Executor or Trustee"
    GOVERNMENTAL_ENTITY = "Governmental Entity"
    INDIVIDUAL = "Individual"
    JOINT_EMPLOYERS = "Joint Employers"
    JOINT_VENTURE = "Joint Venture"
    LABOR_UNION = "Labor Union"
    LLC = "Limited Liability Company (LLC)"
    LLP = "Limited Liability Partnership"
    LIMITED_PARTNERSHIP = "Limited Partnership"
    MULTIPLE_STATUS = "Multiple Status"
    ORGANIZATION = "Organization, including a Corporation (but not including a Partnership, Joint Venture or Limited Liability Company)"
    OTHER = "Other"
    PARTNERSHIP = "Partnership"
    RELIGIOUS_ORGANIZATION = "Religious Organization"
    TRUST = "Trust"
    TRUST_OR_ESTATE = "Trust or Estate"


class ConstructionType(str, Enum):
    FRAME = "Frame"
    JOISTED_MASONRY = "Joisted Masonry"
    NON_COMBUSTIBLE = "Non-Combustible"
    FIRE_RESISTIVE = "Fire Resistive"
    MODIFIED_FIRE_RESISTIVE = "Modified Fire Resistive"


class OccupiedBy(str, Enum):
    OWNER = "Owner Occupied"
    NON_OWNER = "Non owner"


class DistanceToHydrant(str, Enum):
    WITHIN_1000 = "1 to 1,000 Ft"
    OVER_1000 = "Over 1,000 Ft"


class InsuredType(str, Enum):
    INDIVIDUAL = "Individual"
    ORGANIZATION = "Organization"


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
    same_as_mailing: Optional[bool] = None
    owned_or_leased: str = ""  # "Owned" or "Leased"
    square_footage: Optional[int] = None
    year_built: Optional[int] = None
    distance_to_hydrant: Optional[DistanceToHydrant] = None


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
    building_description: str = ""
    building_value: Optional[float] = None
    valuation: str = "Replacement Cost"
    bpp_value: Optional[float] = None  # Business Personal Property
    annual_gross_receipts: Optional[float] = None
    business_income_limit: Optional[float] = None
    construction_type: Optional[ConstructionType] = None
    occupied_by: Optional[OccupiedBy] = None
    square_footage: Optional[int] = None
    year_built: Optional[int] = None
    number_of_stories: Optional[int] = None
    roof_year_updated: Optional[int] = None
    roof_material: str = ""
    electrical_year_updated: Optional[int] = None
    electrical_amp_service: str = ""
    electrical_box_type: str = ""
    electrical_wiring_type: str = ""
    plumbing_year_updated: Optional[int] = None
    hvac_year_updated: Optional[int] = None
    heating_service: str = ""
    sprinklered: Optional[bool] = None
    any_vacant_portions: Optional[bool] = None
    burglar_alarm: Optional[bool] = None
    fire_alarm: Optional[bool] = None
    fire_extinguishers: Optional[bool] = None
    smoke_detectors: Optional[bool] = None
    security_cameras: Optional[bool] = None
    central_station_monitored: Optional[bool] = None
    mortgagee_name: str = ""
    mortgagee_address: str = ""
    # BOP classification
    business_owner_class: str = ""
    property_deductible: str = "$1,000"
    inflation_guard: str = "5%"


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
    insured_type: Optional[InsuredType] = None  # Individual or Organization
    legal_business_name: str = ""
    first_name: str = ""
    last_name: str = ""
    middle_initial: str = ""
    suffix: str = ""
    date_of_birth: Optional[date] = None
    dba: str = ""
    fein: str = ""
    entity_type: Optional[EntityType] = None
    year_established: Optional[int] = None
    years_in_business: str = ""  # e.g. "10+ years" for MMG dropdown
    website: str = ""
    contact_name: str = ""
    contact_title: str = ""
    contact_phone: str = ""
    contact_phone_type: str = ""  # e.g. "Business", "Cell", "Home"
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

    # 8. Loss History & Underwriting
    claims: list[Claim] = Field(default_factory=list)
    any_claims_over_25k: Optional[bool] = None
    currently_insured: Optional[bool] = None
    agency_manages_current_policy: Optional[bool] = None
    current_carrier: str = ""
    continuous_coverage: Optional[bool] = None
    convicted_of_felony: Optional[bool] = None
    filed_bankruptcy: Optional[bool] = None
    operates_other_businesses: Optional[bool] = None
    losses_past_3_years: Optional[bool] = None
    reason_for_shopping: str = ""

    # 9. Prior Insurance
    prior_insurance: list[PriorInsurance] = Field(default_factory=list)
    coverage_gaps_last_3_years: Optional[bool] = None
    non_renewals_or_cancellations: Optional[bool] = None

    # Desired effective date
    effective_date: Optional[date] = None
