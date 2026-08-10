"""
Application form understanding module.

Discovers application forms, extracts fields, matches questions to the
answer library, and generates tailored answers using verified profile data.

Supports: Typeform, Tally, Google Forms, Luma, Fillout, Airtable, Devpost,
and custom websites.
"""

import json
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from hackathon_searcher.models import FormField, FormSnapshot
from hackathon_searcher.profile import profile


# Known form provider patterns
FORM_PROVIDERS = {
    "typeform.com": "typeform",
    "tally.so": "tally",
    "docs.google.com/forms": "google_forms",
    "lu.ma": "luma",
    "fillout.com": "fillout",
    "airtable.com": "airtable",
    "devpost.com": "devpost",
}

# Sensitive/legal question patterns that should never be answered
# without explicit profile data
SENSITIVE_QUESTION_PATTERNS = [
    r"citizenship",
    r"visa\s*(status|type|required)",
    r"criminal",
    r"disability",
    r"medical",
    r"gender\s*identity",
    r"ethnicity",
    r"legal\s*declaration",
    r"financial\s*declaration",
    r"contractual",
    r"photo\s*consent",
    r"terms\s*(and|&)\s*conditions.*agree",
]


def detect_form_provider(url: str) -> Optional[str]:
    """Detect which form provider hosts the application."""
    url_lower = url.lower()
    for domain, provider in FORM_PROVIDERS.items():
        if domain in url_lower:
            return provider
    return "custom"


def extract_form_fields(html: str, base_url: str = "") -> list[FormField]:
    """
    Extract form fields from HTML.

    Handles common form patterns across providers.
    Returns a list of FormField objects.
    """
    soup = BeautifulSoup(html, "lxml")
    fields = []
    order = 0

    # --- Standard HTML form elements ---
    for form in soup.find_all("form"):
        for element in form.find_all(["input", "textarea", "select"]):
            field = _parse_form_element(element, order)
            if field:
                fields.append(field)
                order += 1

    # If no <form> found, look for loose inputs (common in JS-heavy apps)
    if not fields:
        for element in soup.find_all(["input", "textarea", "select"]):
            field = _parse_form_element(element, order)
            if field:
                fields.append(field)
                order += 1

    # --- Also look for label/div patterns (Typeform-style) ---
    if not fields:
        fields = _extract_typeform_style_fields(soup, order)

    return fields


def _parse_form_element(element, order: int) -> Optional[FormField]:
    """Parse a single HTML form element into a FormField."""
    tag = element.name.lower()
    field_type = element.get("type", "text") if tag == "input" else tag
    name = element.get("name", element.get("id", ""))
    placeholder = element.get("placeholder", "")
    required = element.get("required") is not None or element.get("aria-required") == "true"

    # Try to find label
    label_text = ""
    element_id = element.get("id", "")
    if element_id:
        label = element.find_previous("label", attrs={"for": element_id})
        if not label:
            # Try next sibling or parent label
            label = element.find_parent("label")
        if label:
            label_text = label.get_text(strip=True)

    if not label_text:
        # Use aria-label or placeholder
        label_text = element.get("aria-label", "") or placeholder or name

    # Skip hidden, submit, and button inputs
    skip_types = {"hidden", "submit", "button", "reset", "image"}
    if field_type in skip_types:
        return None

    # Map field type
    type_map = {
        "email": "email",
        "tel": "phone",
        "url": "url",
        "number": "text",
        "date": "date",
        "checkbox": "checkbox",
        "radio": "radio",
        "file": "file",
        "textarea": "textarea",
        "select": "dropdown",
        "text": "text",
    }

    mapped_type = type_map.get(field_type, "text")

    # Extract options for dropdown/radio/checkbox
    options = []
    if tag == "select":
        for opt in element.find_all("option"):
            val = opt.get("value", "")
            if val:
                options.append(opt.get_text(strip=True))
    elif field_type == "radio":
        # Find all radio buttons with same name
        pass  # Handled at form level

    return FormField(
        label=label_text[:200],
        name=name,
        field_type=mapped_type,
        required=required,
        options=options,
        placeholder=placeholder,
        order=order,
    )


def _extract_typeform_style_fields(soup, start_order: int) -> list[FormField]:
    """Extract fields from Typeform-style (div-based) forms."""
    fields = []
    order = start_order

    # Look for common Typeform/Tally class patterns
    for container in soup.find_all("div", class_=lambda c: c and any(
        x in (c or "").lower() for x in ["question", "field", "form-field", "input"]
    )):
        label_el = container.find(["label", "span", "div"], class_=lambda c: c and "label" in (c or "").lower())
        input_el = container.find(["input", "textarea", "select"])

        if input_el:
            label_text = label_el.get_text(strip=True) if label_el else ""
            field = FormField(
                label=label_text[:200],
                name=input_el.get("name", input_el.get("id", "")),
                field_type="text",
                required=False,
                order=order,
            )
            fields.append(field)
            order += 1

    return fields


def generate_answer(field: FormField, event_context: Optional[dict] = None) -> str:
    """
    Generate an answer for a form field using the profile and answer library.

    Returns the answer string, or "UNKNOWN_REQUIRED_FIELD" if the answer
    cannot be derived from verified data and the field is required.
    """
    question = field.label or field.name or ""
    question_lower = question.lower()

    # --- Email ---
    if field.field_type == "email":
        email = profile.contact.get("email", "")
        if email:
            field.answer_source = "profile"
            field.answer_confidence = 1.0
            return email
        if field.required:
            return "UNKNOWN_REQUIRED_FIELD"
        return ""

    # --- Phone ---
    if field.field_type == "phone":
        phone = profile.contact.get("phone", "")
        if phone:
            field.answer_source = "profile"
            field.answer_confidence = 1.0
            return phone
        return ""

    # --- URL fields ---
    if field.field_type == "url":
        url = _match_url_field(question_lower)
        if url:
            field.answer_source = "profile"
            field.answer_confidence = 1.0
            return url
        return ""

    # --- Name ---
    if _is_name_field(question_lower):
        field.answer_source = "profile"
        field.answer_confidence = 1.0
        return profile.name

    # --- Location ---
    if _is_location_field(question_lower):
        field.answer_source = "profile"
        field.answer_confidence = 1.0
        return f"{profile.city}, {profile.country}"

    # --- Age ---
    if _is_age_field(question_lower):
        field.answer_source = "profile"
        field.answer_confidence = 1.0
        return str(profile.age)

    # --- Education ---
    if _is_education_field(question_lower):
        field.answer_source = "profile"
        field.answer_confidence = 1.0
        return profile.education

    # --- Travel support questions ---
    if _is_travel_support_question(question_lower):
        field.answer_source = "profile"
        field.answer_confidence = 1.0
        return profile.get_tailored_answer("do you require travel assistance", event_context) or "Yes"

    # --- Sensitive questions ---
    if _is_sensitive_question(question_lower):
        if field.required:
            return "UNKNOWN_REQUIRED_FIELD"
        return ""

    # --- Match against answer library ---
    match = profile.match_answer(question, event_context)
    if match and match["confidence"] >= 0.5:
        answer = profile.get_tailored_answer(question, event_context)
        field.answer_source = "answer_library"
        field.answer_confidence = match["confidence"]
        return answer or ""

    # --- Dropdown / radio ---
    if field.options and field.field_type in ("dropdown", "radio"):
        # Try to pick the best option
        for opt in field.options:
            opt_lower = opt.lower().strip()
            if "sweden" in opt_lower or "stockholm" in opt_lower:
                field.answer_source = "profile"
                field.answer_confidence = 0.8
                return opt
            if "yes" in opt_lower and _is_yes_no_field(question_lower):
                field.answer_source = "answer_library"
                field.answer_confidence = 0.6
                return opt
        return field.options[0] if field.options else ""

    # --- Yes/No fields ---
    if field.field_type in ("checkbox",) or _is_yes_no_field(question_lower):
        return "Yes"

    # --- Country ---
    if field.field_type == "country" or _is_country_field(question_lower):
        return profile.country

    # --- Unknown ---
    if field.required:
        return "UNKNOWN_REQUIRED_FIELD"
    return ""


def _match_url_field(question_lower: str) -> str:
    """Match URL field to the appropriate profile URL."""
    contact = profile.contact
    if "github" in question_lower:
        return contact.get("github", "")
    if "linkedin" in question_lower:
        return contact.get("linkedin", "")
    if "portfolio" in question_lower:
        return contact.get("portfolio", "")
    if "website" in question_lower or "personal site" in question_lower:
        return contact.get("website", "")
    return ""


def _is_name_field(question_lower: str) -> bool:
    patterns = ["name", "full name", "first name", "last name"]
    return any(p in question_lower for p in patterns) and "hackathon" not in question_lower and "event" not in question_lower


def _is_location_field(question_lower: str) -> bool:
    patterns = ["where are you based", "where do you live", "location", "city", "country of residence"]
    return any(p in question_lower for p in patterns)


def _is_age_field(question_lower: str) -> bool:
    return "age" in question_lower or "how old" in question_lower


def _is_education_field(question_lower: str) -> bool:
    patterns = ["education", "university", "school", "what are you studying", "degree"]
    return any(p in question_lower for p in patterns)


def _is_travel_support_question(question_lower: str) -> bool:
    patterns = [
        "travel support", "travel assistance", "travel reimbursement",
        "flight", "travel stipend", "do you need travel",
        "require travel", "travel grant",
    ]
    return any(p in question_lower for p in patterns)


def _is_sensitive_question(question_lower: str) -> bool:
    import re
    for pattern in SENSITIVE_QUESTION_PATTERNS:
        if re.search(pattern, question_lower):
            return True
    return False


def _is_yes_no_field(question_lower: str) -> bool:
    patterns = ["do you", "are you", "have you", "will you", "can you", "would you"]
    return any(question_lower.startswith(p) for p in patterns)


def _is_country_field(question_lower: str) -> bool:
    patterns = ["country", "nationality", "citizenship"]
    return any(p in question_lower for p in patterns) and "visa" not in question_lower


def create_submission_snapshot(
    event_id: str,
    event_name: str,
    application_url: str,
    fields: list[FormField],
    score: float,
    reason: str,
    travel_support_status: str,
) -> FormSnapshot:
    """Create a complete snapshot of an application before submission."""
    from datetime import datetime, timezone

    return FormSnapshot(
        event_id=event_id,
        event_name=event_name,
        application_url=application_url,
        fields=fields,
        timestamp=datetime.now(timezone.utc).isoformat(),
        score=score,
        reason_for_applying=reason,
        travel_support_status=travel_support_status,
    )


def validate_application(fields: list[FormField]) -> list[str]:
    """
    Validate an application before submission.

    Returns a list of issues found, empty if everything looks good.
    """
    issues = []

    for field in fields:
        # Check required fields are filled
        if field.required and (not field.answer or field.answer == "UNKNOWN_REQUIRED_FIELD"):
            issues.append(f"Required field '{field.label}' is missing: {field.answer}")

        # Check no fabricated data
        if field.answer_source == "generated" and field.required:
            issues.append(f"Field '{field.label}' has generated (non-verified) answer")

        # Check URLs look valid
        if field.field_type == "url" and field.answer:
            parsed = urlparse(field.answer)
            if not parsed.scheme or not parsed.netloc:
                issues.append(f"Field '{field.label}' has invalid URL: {field.answer}")

    return issues
