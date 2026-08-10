"""
Pydantic models for structured data throughout the Hackathon Searcher.

Used for LLM outputs, event representation, and application data.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TravelSupportStatus(str, Enum):
    CONFIRMED_FLIGHTS = "CONFIRMED_FLIGHTS"
    CONFIRMED_TRAVEL_REIMBURSEMENT = "CONFIRMED_TRAVEL_REIMBURSEMENT"
    CONFIRMED_TRAVEL_STIPEND = "CONFIRMED_TRAVEL_STIPEND"
    CONFIRMED_ACCOMMODATION_ONLY = "CONFIRMED_ACCOMMODATION_ONLY"
    TRAVEL_SUPPORT_MENTIONED = "TRAVEL_SUPPORT_MENTIONED"
    POSSIBLE_TRAVEL_SUPPORT = "POSSIBLE_TRAVEL_SUPPORT"
    NO_TRAVEL_INFORMATION = "NO_TRAVEL_INFORMATION"
    NO_TRAVEL_SUPPORT = "NO_TRAVEL_SUPPORT"
    UNKNOWN = "UNKNOWN"


class ApplicationStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    RESEARCHING = "RESEARCHING"
    QUALIFIED = "QUALIFIED"
    SKIPPED = "SKIPPED"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    BLOCKED_CAPTCHA = "BLOCKED_CAPTCHA"
    BLOCKED_UNKNOWN_FIELD = "BLOCKED_UNKNOWN_FIELD"
    BLOCKED_LOGIN = "BLOCKED_LOGIN"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    ACCEPTED = "ACCEPTED"
    WAITLISTED = "WAITLISTED"


class TravelSupportDetails(BaseModel):
    status: TravelSupportStatus = TravelSupportStatus.UNKNOWN
    confidence: float = 0.0
    amount: Optional[float] = None
    currency: str = ""
    max_reimbursement: Optional[float] = None
    eligible_countries: list[str] = Field(default_factory=list)
    eligible_participants: str = ""
    requires_approval: bool = False
    reimbursement_timing: str = ""  # before or after event
    receipts_required: bool = False
    economy_only: bool = False
    pre_approval_required: bool = False
    application_process: str = ""
    source_url: str = ""
    source_text: str = ""
    source_timestamp: str = ""


class SponsorAssessment(BaseModel):
    name: str = ""
    quality_score: float = 0.0  # 0-20
    is_host: bool = False
    is_judge: bool = False
    is_mentor: bool = False
    is_prize_sponsor: bool = False
    provides_api_credits: bool = False
    relevance: str = ""


class EventAnalysis(BaseModel):
    """Structured LLM output for event analysis."""
    event_name: str = ""
    eligible: bool = True
    physical: bool = True
    travel_support: TravelSupportDetails = Field(default_factory=TravelSupportDetails)
    sponsors: list[SponsorAssessment] = Field(default_factory=list)
    technical_relevance: float = 0.0  # 0-15
    event_quality: float = 0.0  # 0-10
    prize_opportunity: float = 0.0  # 0-10
    logistics_score: float = 0.0  # 0-5
    sponsor_score: float = 0.0  # 0-20
    travel_score: float = 0.0  # 0-40
    total_score: float = 0.0  # 0-100
    apply: bool = False
    reason: str = ""
    eligibility_concerns: list[str] = Field(default_factory=list)


class FormField(BaseModel):
    """Represents a single field in an application form."""
    label: str = ""
    name: str = ""
    field_type: str = "text"  # text, textarea, email, phone, url, dropdown, radio, checkbox, date, country, file, multi-select, yes_no, conditional
    required: bool = False
    options: list[str] = Field(default_factory=list)
    placeholder: str = ""
    description: str = ""
    order: int = 0
    answer: str = ""
    answer_confidence: float = 0.0
    answer_source: str = ""  # profile, answer_library, generated, manual, unknown
    page_number: int = 0


class FormSnapshot(BaseModel):
    """Complete snapshot of an application before submission."""
    event_id: str = ""
    event_name: str = ""
    application_url: str = ""
    fields: list[FormField] = Field(default_factory=list)
    timestamp: str = ""
    score: float = 0.0
    reason_for_applying: str = ""
    travel_support_status: str = ""


class DailyReport(BaseModel):
    """Report produced after each crawl."""
    events_scanned: int = 0
    events_new: int = 0
    events_updated: int = 0
    applications_submitted: int = 0
    applications_blocked: int = 0
    top_applications: list[dict] = Field(default_factory=list)
    new_high_score_events: list[dict] = Field(default_factory=list)
    blocked_events: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    timestamp: str = ""
