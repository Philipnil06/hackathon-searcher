"""
Application form understanding module — multi-applicant edition.

Discovers application forms, extracts fields, matches questions to the
applicant's answer library, and generates tailored answers using verified profile data.
Uses the LLM for semantic matching and answer generation when appropriate.
"""

import json
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from hackathon_searcher.models import FormField, FormSnapshot
from hackathon_searcher.profile import ApplicantProfile
from hackathon_searcher.llm import generate_application_answer
from hackathon_searcher.settings import settings

FORM_PROVIDERS = {
    "typeform.com": "typeform",
    "tally.so": "tally",
    "docs.google.com/forms": "google_forms",
    "forms.gle": "google_forms",
    "lu.ma": "luma",
    "luma.com": "luma",
    "fillout.com": "fillout",
    "airtable.com": "airtable",
    "devpost.com": "devpost",
}

SENSITIVE_QUESTION_PATTERNS = [
    r"citizenship", r"visa\s*(status|type|required)", r"criminal",
    r"disability", r"medical", r"gender\s*identity", r"ethnicity",
    r"lgbtq", r"sexual\s*orientation", r"identity\s+as",
    r"legal\s*declaration", r"financial\s*declaration", r"contractual",
    r"photo\s*consent", r"terms\s*(and|&)\s*conditions.*agree",
    r"parental\s*consent", r"marketing\s*consent", r"privacy\s*declaration",
]

# The applicants explicitly confirmed this as their shared discovery source.
# It is therefore a verified user-supplied fact, not a guessed fallback.
DISCOVERY_SOURCE_ANSWER = "HackathonHub.eu"


def detect_form_provider(url: str) -> Optional[str]:
    url_lower = url.lower()
    for domain, provider in FORM_PROVIDERS.items():
        if domain in url_lower:
            return provider
    return "custom"


def extract_form_fields(html: str, base_url: str = "") -> list[FormField]:
    soup = BeautifulSoup(html, "lxml")
    fields = []
    order = 0

    for form in soup.find_all("form"):
        for element in form.find_all(["input", "textarea", "select"]):
            field = _parse_form_element(element, order)
            if field:
                fields.append(field)
                order += 1

    if not fields:
        for element in soup.find_all(["input", "textarea", "select"]):
            field = _parse_form_element(element, order)
            if field:
                fields.append(field)
                order += 1

    if not fields:
        fields = _extract_typeform_style_fields(soup, order)

    return fields


def _parse_form_element(element, order: int) -> Optional[FormField]:
    tag = element.name.lower()
    field_type = element.get("type", "text") if tag == "input" else tag
    name = element.get("name", element.get("id", ""))
    placeholder = element.get("placeholder", "")
    required = element.get("required") is not None or element.get("aria-required") == "true"

    label_text = ""
    element_id = element.get("id", "")
    if element_id:
        label = element.find_previous("label", attrs={"for": element_id})
        if not label:
            label = element.find_parent("label")
        if label:
            label_text = label.get_text(strip=True)
    if not label_text:
        label_text = element.get("aria-label", "") or placeholder or name
    required = required or "*" in label_text

    skip_types = {"hidden", "submit", "button", "reset", "image"}
    if field_type in skip_types:
        return None

    type_map = {
        "email": "email", "tel": "phone", "url": "url", "number": "text",
        "date": "date", "checkbox": "checkbox", "radio": "radio",
        "file": "file", "textarea": "textarea", "select": "dropdown", "text": "text",
    }
    mapped_type = type_map.get(field_type, "text")

    options = []
    if tag == "select":
        for opt in element.find_all("option"):
            val = opt.get("value", "")
            if val:
                options.append(opt.get_text(strip=True))

    return FormField(
        label=label_text[:200], name=name, field_type=mapped_type,
        required=required, options=options, placeholder=placeholder, order=order,
        selector=f"#{element_id}" if element_id else (f"[name='{name}']" if name else ""),
    )


def consent_policy_for_field(field: FormField, profile=None) -> tuple[str, str]:
    """Return deterministic consent answer and policy category; never delegate to LLM."""
    text = f"{field.label} {field.description}".lower()
    if not any(word in text for word in ("consent", "agree", "privacy", "terms", "guidelines", "newsletter", "marketing", "talent", "photo", "media", "share")):
        return "", "NOT_CONSENT"
    required = field.required or "required" in text or "*" in field.label
    if "newsletter" in text:
        return ("Yes" if settings.ACCEPT_OPTIONAL_NEWSLETTER else "No"), "OPTIONAL_NEWSLETTER"
    if "marketing" in text:
        return ("Yes" if settings.ACCEPT_OPTIONAL_MARKETING else "No"), "OPTIONAL_MARKETING"
    if "talent" in text or "recruit" in text or "sponsor" in text or "share your profile" in text:
        # This preference is profile-local: one team member's consent can never
        # affect another member's answer.
        preferences = getattr(profile, "data", {}).get("consent", {}) if profile else {}
        accept = bool(preferences.get("accept_optional_talent_pool", settings.ACCEPT_OPTIONAL_TALENT_POOL))
        return ("Yes" if accept else "No"), "OPTIONAL_TALENT_POOL"
    if "photo" in text or "media" in text:
        return ("Yes" if settings.ACCEPT_OPTIONAL_MEDIA else "No"), "OPTIONAL_MEDIA"
    if "security guidelines" in text or "terms" in text or "event rules" in text:
        return ("Yes" if required and settings.ACCEPT_REQUIRED_EVENT_RULES else "No"), "REQUIRED_EVENT_RULES"
    if "privacy" in text or "data processing" in text:
        return ("Yes" if required and settings.ACCEPT_REQUIRED_DATA_PROCESSING else "No"), "REQUIRED_DATA_PROCESSING"
    return "UNKNOWN_REQUIRED_FIELD" if required else "No", "UNCLASSIFIED_CONSENT"


def _extract_typeform_style_fields(soup, start_order: int) -> list[FormField]:
    fields = []
    order = start_order
    for container in soup.find_all("div", class_=lambda c: c and any(
        x in (c or "").lower() for x in ["question", "field", "form-field", "input"]
    )):
        label_el = container.find(["label", "span", "div"], class_=lambda c: c and "label" in (c or "").lower())
        input_el = container.find(["input", "textarea", "select"])
        if input_el:
            label_text = label_el.get_text(strip=True) if label_el else ""
            fields.append(FormField(
                label=label_text[:200], name=input_el.get("name", input_el.get("id", "")),
                field_type="text", required=False, order=order,
            ))
            order += 1
    return fields


def generate_answer_for_field(
    field: FormField,
    profile: ApplicantProfile,
    event_context: Optional[dict] = None,
    use_llm: bool = True,
) -> str:
    """
    Generate an answer for a form field using the applicant's profile.

    Uses deterministic matching first, falls back to LLM for complex questions.
    """
    question = field.label or field.name or ""
    question_lower = question.lower()

    consent_answer, consent_kind = consent_policy_for_field(field, profile)
    if consent_kind != "NOT_CONSENT":
        return consent_answer

    # A required control without a readable question is never safe to infer.
    # In particular, do not turn an unknown checkbox into a blanket "Yes".
    if not question.strip():
        return "UNKNOWN_REQUIRED_FIELD" if field.required else ""

    # --- Deterministic matches ---
    if field.field_type == "email":
        email = profile.email
        if email:
            return email
        if field.required:
            return "UNKNOWN_REQUIRED_FIELD"
        return ""

    if field.field_type == "phone":
        phone = profile.phone
        if phone:
            return phone
        return ""

    if field.field_type == "url":
        url = _match_url_field(question_lower, profile)
        if url:
            return url
        return ""

    # Some providers expose profile links as ordinary text inputs. Resolve
    # these semantics before generic name/LLM handling.
    if any(term in question_lower for term in ("github", "linkedin", "portfolio", "personal website", "website")):
        url = _match_url_field(question_lower, profile)
        if url:
            return url

    if _is_team_question(question_lower):
        library_answer = next((entry.get("answer", "") for entry in profile.answers if entry.get("id") == "team_or_individual"), "")
        if library_answer:
            return library_answer
        partner = profile.default_partner.replace("_", " ").title()
        return f"I'm applying together with {partner}." if partner else "UNKNOWN_REQUIRED_FIELD"

    if _is_skills_question(question_lower):
        return ", ".join(str(skill) for skill in profile.skills if skill)

    if _is_name_field(question_lower):
        # Check if they ask for full/legal name
        if "full" in question_lower or "legal" in question_lower:
            return profile.full_name
        return profile.name

    if _is_location_field(question_lower):
        return f"{profile.city}, {profile.country}"

    if _is_age_field(question_lower):
        return str(profile.age)

    if _is_education_field(question_lower):
        edu = profile.education
        if edu.get("english_description"):
            return edu["english_description"]
        if edu.get("degree"):
            return edu["degree"]
        return profile.education_text

    if _is_travel_support_question(question_lower):
        origin = ", ".join(part for part in (profile.city, profile.country) if part)
        return f"Yes — I would be travelling from {origin}." if origin else "UNKNOWN_REQUIRED_FIELD"

    if _is_sensitive_question(question_lower):
        if field.required:
            return "UNKNOWN_REQUIRED_FIELD"
        return ""

    # Discovery-source questions are normally blocked rather than guessed. The
    # user has explicitly supplied HackathonHub.eu as the common source.
    if _is_discovery_source_question(question_lower):
        return DISCOVERY_SOURCE_ANSWER

    # --- Answer library match ---
    match = profile.match_answer(question, event_context)
    if match and match["confidence"] >= 0.6:
        return match["answer"]

    # --- Dropdown / radio ---
    is_custom_dropdown = field.field_type in ("dropdown", "radio") or str(field.placeholder or "").strip().lower() in {"select an option", "välj ett alternativ"}
    if is_custom_dropdown and field.options:
        for opt in field.options:
            opt_lower = opt.lower().strip()
            if "sweden" in opt_lower or "stockholm" in opt_lower:
                return opt
            if "yes" in opt_lower and _is_yes_no_field(question_lower):
                return opt
        # Selecting the first option would be a guess.  A required unknown
        # choice must block rather than silently selecting a value.
        return "UNKNOWN_REQUIRED_FIELD" if field.required else ""

    # Custom dropdowns without an observed option list must never receive a
    # prose LLM answer. The browser can only safely choose a value after the
    # live menu exposes a matching option.
    if is_custom_dropdown:
        return "UNKNOWN_REQUIRED_FIELD" if field.required or "*" in question else ""

    if field.field_type in ("checkbox",) or _is_yes_no_field(question_lower):
        return "Yes"

    if field.field_type == "country" or _is_country_field(question_lower):
        return profile.country

    # --- LLM generation for complex questions ---
    if use_llm and question and len(question) > 10:
        try:
            llm_answer = generate_application_answer(
                question=question,
                question_type=field.field_type,
                applicant_profile=profile.data,
                event_context=event_context or {},
                answer_library=profile.answers,
            )
            if llm_answer and "UNKNOWN" not in llm_answer.upper():
                return llm_answer
        except Exception as e:
            print(f"[forms] LLM answer generation failed: {e}")

    if field.required:
        return "UNKNOWN_REQUIRED_FIELD"
    return ""


def _match_url_field(question_lower: str, profile: ApplicantProfile) -> str:
    if "github" in question_lower:
        if "username" in question_lower:
            return urlparse(profile.github).path.strip("/").split("/")[0] if profile.github else ""
        return profile.github
    if "linkedin" in question_lower:
        return profile.linkedin
    if "portfolio" in question_lower:
        return profile.portfolio
    if "website" in question_lower or "personal site" in question_lower:
        return profile.website
    return ""


def _is_name_field(ql: str) -> bool:
    if any(term in ql for term in ("username", "team", "teammate", "names of")):
        return False
    return bool(re.search(r"\b(?:full|first|last)?\s*name\b", ql)) and "hackathon" not in ql and "event" not in ql


def _is_team_question(ql: str) -> bool:
    return any(term in ql for term in ("team or", "coming with a team", "teammate", "applying as a team"))


def _is_skills_question(ql: str) -> bool:
    return "what are your skills" in ql or "your skills" in ql


def _is_location_field(ql: str) -> bool:
    return any(p in ql for p in ["where are you based", "where do you live", "location", "city", "country of residence"])


def _is_age_field(ql: str) -> bool:
    return "age" in ql or "how old" in ql


def _is_education_field(ql: str) -> bool:
    return any(p in ql for p in ["education", "university", "school", "what are you studying", "degree"])


def _is_travel_support_question(ql: str) -> bool:
    return any(p in ql for p in [
        "travel support", "travel assistance", "travel reimbursement",
        "flight", "travel stipend", "do you need travel", "require travel", "travel grant",
    ])


def _is_sensitive_question(ql: str) -> bool:
    for pattern in SENSITIVE_QUESTION_PATTERNS:
        if re.search(pattern, ql):
            return True
    return False


def _is_discovery_source_question(ql: str) -> bool:
    return any(pattern in ql for pattern in (
        "how did you find", "how did you hear", "where did you hear",
        "how did you discover", "referral source",
    ))


def _is_yes_no_field(ql: str) -> bool:
    return any(ql.startswith(p) for p in ["do you", "are you", "have you", "will you", "can you", "would you"])


def _is_country_field(ql: str) -> bool:
    return any(p in ql for p in ["country", "nationality", "citizenship"]) and "visa" not in ql


def create_submission_snapshot(
    event_id: str, applicant_id: str, event_name: str,
    application_url: str, fields: list[FormField],
    event_score: float, fit_score: float, reason: str, travel_support_status: str,
) -> FormSnapshot:
    now = datetime.now(timezone.utc).isoformat()
    return FormSnapshot(
        event_id=event_id, event_name=event_name,
        application_url=application_url, fields=fields,
        timestamp=now, score=fit_score,
        reason_for_applying=reason, travel_support_status=travel_support_status,
    )


def validate_application(fields: list[FormField]) -> list[str]:
    issues = []
    for field in fields:
        if field.required and (not field.answer or field.answer == "UNKNOWN_REQUIRED_FIELD"):
            issues.append(f"Required field '{field.label}' is missing")
        if field.field_type == "url" and field.answer:
            parsed = urlparse(field.answer)
            if not parsed.scheme or not parsed.netloc:
                issues.append(f"Field '{field.label}' has invalid URL: {field.answer}")
    return issues


def validate_factual_consistency(
    fields: list[FormField],
    answers: list[dict],
    profile: ApplicantProfile,
    context: str = "application",
) -> list[str]:
    """Validate answers against verified profile facts.

    Normal application fields always use ``profile.email``. The separate
    Google OAuth address is accepted only when the caller explicitly supplies
    ``context='authentication'``; it must never silently replace the normal
    application address.
    """
    issues: list[str] = []
    answer_by_label = {
        str(item.get("label", "")).strip().lower(): str(item.get("answer", ""))
        for item in answers
    }
    for field in fields:
        answer = str(getattr(field, "answer", "") or "")
        label = str(getattr(field, "label", "") or getattr(field, "name", ""))
        label_lower = label.lower()
        is_email = getattr(field, "field_type", "") == "email" or "email" in label_lower
        if answer == "UNKNOWN_REQUIRED_FIELD" and getattr(field, "required", False):
            issues.append(f"Unknown required field: {label}")
        if is_email and context != "authentication":
            if answer.lower() != profile.email.lower():
                issues.append(f"Normal application email mismatch for {label}")
            if profile.google_oauth_email and answer.lower() == profile.google_oauth_email.lower() and \
               profile.google_oauth_email.lower() != profile.email.lower():
                issues.append("OAuth email used in normal application field")
        if getattr(field, "required", False) and not answer:
            issues.append(f"Required field is empty: {label}")

    # Do not require an email to appear when the form has no email field.
    # If an email answer exists in the serialized answer list, validate it too.
    for label, answer in answer_by_label.items():
        if "email" in label and context != "authentication" and answer.lower() != profile.email.lower():
            issues.append(f"Normal application email mismatch for {label}")
    return sorted(set(issues))
