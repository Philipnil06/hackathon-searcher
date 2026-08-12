"""
Scoring engine — multi-applicant edition.

Computes:
1. EVENT_QUALITY_SCORE (0-100): independent of any applicant
2. APPLICANT_FIT_SCORE (0-100): per applicant, based on their profile vs event
3. Per-applicant eligibility assessment
4. Per-applicant travel eligibility
"""

import json
from typing import Optional

from hackathon_searcher.database import get_event_by_id, update_event, log_audit, get_application, update_application
from hackathon_searcher.models import TravelSupportStatus
from hackathon_searcher.profile import profile_manager, ApplicantProfile
from hackathon_searcher.settings import settings
from hackathon_searcher.llm import classify_sponsor_quality, classify_themes, interpret_eligibility
from hackathon_searcher.travel import assess_accommodation, assess_location, assess_travel_support

# Known strong sponsors (Tier scoring reference)
KNOWN_STRONG_SPONSORS = {
    "anthropic": 19, "openai": 19, "google": 18, "microsoft": 18,
    "nvidia": 18, "meta": 17, "deepmind": 19,
    "aws": 17, "cloudflare": 16, "github": 16, "stripe": 17,
    "supabase": 15, "vercel": 15, "elevenlabs": 15, "hugging face": 16,
    "mistral": 15, "groq": 15, "cursor": 14, "lovable": 13, "replit": 14,
    "ramp": 14, "revolut": 14, "klarna": 14, "spotify": 14,
    "notion": 14, "figma": 15, "linear": 14,
    "y combinator": 18, "sequoia": 17, "accel": 16,
    "index ventures": 16, "general catalyst": 16, "a16z": 17,
    "antler": 15, "founders fund": 16,
    "anduril": 16, "boston dynamics": 16, "tesla": 15,
    "spacex": 16, "palantir": 15, "lockheed": 14,
    "plaid": 15, "wise": 14, "monzo": 13,
    "adyen": 14, "mongodb": 14, "datadog": 14, "sentry": 13,
    "redis": 14, "twilio": 14, "netlify": 13, "docker": 15,
    "entrepreneur first": 14, "pinecone": 14, "firecrawl": 12,
    "freebuff": 12, "bambu lab": 11, "seedcamp": 14,
    "linkup": 10, "modal": 13, "red bull": 8,
}

TECHNICAL_THEMES = {
    "ai": 14, "artificial intelligence": 14, "machine learning": 13,
    "ai agents": 15, "agents": 15,
    "robotics": 15, "hardware": 14, "drones": 15, "autonomy": 15,
    "computer vision": 14, "llm": 14, "nlp": 13,
    "developer tools": 13, "devtools": 13, "infrastructure": 13,
    "fintech": 13, "payments": 13, "crypto": 10, "blockchain": 10,
    "cybersecurity": 12, "security": 12, "defense": 13, "defence": 13,
    "startups": 12, "entrepreneurship": 11, "startup": 12,
    "edtech": 12, "education": 9, "consumer": 11,
    "gaming": 9, "health": 10, "climate": 10, "sustainability": 9,
    "mobile": 10, "web": 9, "data": 10, "analytics": 10,
    "open source": 11, "api": 10, "space": 12, "biotech": 11,
    "embedded": 13, "ios": 10,
}


def score_event(event_id: str) -> dict:
    """
    Score an event independently of any applicant.

    Returns event_score breakdown.
    """
    event = get_event_by_id(event_id)
    if not event:
        print(f"[scoring] Event {event_id} not found")
        return {}

    travel_score, travel_reason = _score_travel(event)
    sponsor_score, sponsor_reason = _score_sponsors(event)
    tech_score, tech_reason = _score_technical_relevance(event)
    event_quality_score, event_quality_reason = _score_event_quality(event)
    prize_score, prize_reason = _score_prizes(event)
    logistics_score, logistics_reason = _score_logistics(event)

    total = round(travel_score + sponsor_score + tech_score + event_quality_score + prize_score + logistics_score, 1)

    reasoning = f"""Travel: {travel_score}/15 - {travel_reason}
Sponsors: {sponsor_score}/20 - {sponsor_reason}
Tech relevance: {tech_score}/25 - {tech_reason}
Event quality: {event_quality_score}/15 - {event_quality_reason}
Prizes: {prize_score}/15 - {prize_reason}
Logistics: {logistics_score}/10 - {logistics_reason}
Total event score: {total}/100"""

    updates = {
        "event_score": total,
        "score_reasoning": reasoning,
    }

    if total >= settings.MIN_APPLICATION_SCORE:
        updates["status"] = "QUALIFIED"
    elif total < 40:
        updates["status"] = "SKIPPED"

    try:
        update_event(event_id, updates)
        log_audit(event_id, "EVENT_SCORED", f"Event score: {total}/100")
    except Exception as e:
        print(f"[scoring] Failed to update event {event_id}: {e}")

    return {
        "event_id": event_id,
        "event_score": total,
        "reasoning": reasoning,
        "breakdown": {
            "travel": travel_score,
            "sponsors": sponsor_score,
            "technical_relevance": tech_score,
            "event_quality": event_quality_score,
            "prizes_opportunity": prize_score,
            "logistics": logistics_score,
        },
    }


def score_applicant_fit(event_id: str, applicant_id: str) -> dict:
    """
    Score how well a specific applicant fits this event.

    Returns fit score and eligibility determination.
    """
    event = get_event_by_id(event_id)
    if not event:
        return {"error": "Event not found"}

    profile = profile_manager.get(applicant_id)
    if not profile:
        return {"error": f"Applicant {applicant_id} not found"}

    # Deterministic location/support checks run before form work or ambiguous
    # research. They keep simple geographic decisions cheap and transparent.
    location = assess_location(event, profile)
    travel = assess_travel_support(event, profile)
    accommodation = assess_accommodation(event, profile)
    eligibility = _assess_eligibility(event, profile)
    travel_eligible = bool(travel["meets_requirement"])

    # --- Fit scoring ---
    # Tech alignment: how well do the applicant's interests/skills match the event themes?
    themes_raw = event.get("themes", "[]")
    if isinstance(themes_raw, str):
        try:
            themes = json.loads(themes_raw)
        except (json.JSONDecodeError, TypeError):
            themes = []
    else:
        themes = themes_raw

    description = (event.get("description", "") + " " + event.get("event_name", "")).lower()
    themes = _expanded_event_themes(themes, description)

    tech_fit = _score_tech_alignment(profile, themes, description)  # 0-30
    project_fit = _score_project_relevance(profile, themes, description)  # 0-25
    experience_fit = _score_experience_relevance(profile, event)  # 0-20
    event_type_fit = _score_event_type_fit(profile, event)  # 0-15
    location_fit = float(location["fit"])
    travel_fit = min(10.0, float(travel["fit"]) * 0.8 + float(accommodation["fit"]) * 0.7)

    fit_score = round(min(100.0, tech_fit + project_fit + experience_fit + event_type_fit + location_fit + travel_fit), 1)

    # Should apply?
    event_score = event.get("event_score", 0)
    should_apply, apply_reason = _should_apply_for_applicant(
        event_score, fit_score, eligibility, location, travel, accommodation, event
    )

    event_components = score_event(event_id).get("breakdown", {})
    apply_score, apply_breakdown = calculate_apply_score(event, fit_score, event_components, location, travel, accommodation)
    result = {
        "applicant_id": applicant_id,
        "fit_score": fit_score,
        "eligible": eligibility["eligible"],
        "eligibility_reasoning": eligibility["reasoning"],
        "eligibility_confidence": eligibility["confidence"],
        "eligibility_status": eligibility.get("eligibility", "ELIGIBLE" if eligibility.get("eligible") else "UNCERTAIN"),
        "eligibility_source_evidence": eligibility.get("source_evidence", ""),
        "eligibility_requirements": eligibility.get("requirements", eligibility.get("concerns", [])),
        "travel_eligible": travel_eligible,
        "location_allowed": bool(location["allowed"]),
        "location_status": location["status"],
        "location_reasoning": location["reason"],
        "location_fit": location_fit,
        "travel_requirement_met": bool(travel["meets_requirement"]),
        "travel_requirement_status": travel["status"],
        "travel_reasoning": travel["reason"],
        "accommodation_requirement_met": bool(accommodation["meets_requirement"]),
        "accommodation_requirement_status": accommodation["status"],
        "accommodation_reasoning": accommodation["reason"],
        "should_apply": should_apply,
        "apply_reason": apply_reason,
        "apply_score": apply_score,
        "apply_breakdown": apply_breakdown,
        "breakdown": {
            "tech_fit": tech_fit,
            "project_fit": project_fit,
            "experience_fit": experience_fit,
            "event_type_fit": event_type_fit,
            "location_fit": location_fit,
            "travel_fit": travel_fit,
        }
    }
    # Keep cached application records aligned with the calculation used by the
    # live queue. Without this, discovery could display stale fit scores after
    # a scoring-calibration change.
    if get_application(event_id, applicant_id):
        update_application(event_id, applicant_id, {
            "applicant_fit_score": fit_score,
            "eligibility_status": result["eligibility_status"],
            "eligibility_reasoning": result["eligibility_reasoning"],
            "eligibility_confidence": result["eligibility_confidence"],
            "eligibility_source_evidence": result["eligibility_source_evidence"],
            "eligibility_requirements": result["eligibility_requirements"],
            "travel_eligible": 1 if travel_eligible else 0,
            "location_fit_score": location_fit,
            "location_status": location["status"],
            "travel_requirement_status": travel["status"],
            "accommodation_requirement_status": accommodation["status"],
            "travel_score": event_components.get("travel", 0.0),
            "apply_score": apply_score,
        })
    return result


def calculate_apply_score(
    event: dict,
    fit_score: float,
    components: dict[str, float],
    location: dict | None = None,
    travel: dict | None = None,
    accommodation: dict | None = None,
) -> tuple[float, dict[str, float]]:
    """Rank applications with location first and travel as one preference signal."""
    location, travel, accommodation = location or {}, travel or {}, accommodation or {}
    location_value = round(min(12.0, float(location.get("fit", 5.0)) * 1.2), 1)
    travel_value = round(min(10.0, float(travel.get("fit", 5.0))), 1)
    accommodation_value = round(min(3.0, float(accommodation.get("fit", 0.0))), 1)
    opportunity = min(10.0, float(components.get("prizes_opportunity", 0)) + float(components.get("sponsors", 0)) * 0.25)
    deadline = 2.0 if not event.get("application_deadline") else 3.0
    breakdown = {
        "applicant_fit": round(fit_score * 0.30, 1),
        "event_quality_relevance": round(min(25.0, float(components.get("technical_relevance", 0)) + float(components.get("event_quality", 0))), 1),
        "location_value": location_value,
        "travel_value": travel_value,
        "accommodation_value": accommodation_value,
        "opportunity": round(opportunity, 1),
        "logistics": round(min(5.0, float(components.get("logistics", 0))), 1),
        "deadline_urgency": deadline,
    }
    return round(sum(breakdown.values()), 1), breakdown


def _expanded_event_themes(raw_themes: list[str], description: str) -> list[str]:
    """Normalize tags and add explicit technical themes stated on the event page.

    Source tags such as ``defense-security`` otherwise fail to match verified
    applicant facts such as ``defense`` or ``security``. This affects only
    applicant fit; event-level technical relevance already reads the page text.
    """
    import re

    description = description.lower()
    expanded = {str(theme).strip().lower() for theme in raw_themes if str(theme).strip()}
    for theme in list(expanded):
        expanded.update(part for part in re.split(r"[\s/_-]+", theme) if part)
    for technical_theme in TECHNICAL_THEMES:
        if technical_theme in description:
            expanded.add(technical_theme)
    return sorted(expanded)


def _assess_eligibility(event: dict, profile: ApplicantProfile) -> dict:
    """Assess whether an applicant is eligible for an event."""
    eligibility_text = event.get("eligibility_rules_raw", "")
    age_req = event.get("age_requirement", "")
    student_req = event.get("student_requirement", "")
    nationality_req = event.get("nationality_requirement", "")

    full_rules = f"Age requirement: {age_req}\nStudent requirement: {student_req}\nNationality: {nationality_req}\n{eligibility_text}"

    # First do deterministic checks
    concerns = []
    profile_age = profile.age

    # Age check
    if age_req:
        import re
        age_match = re.search(r'(\d+)\+', age_req)
        if age_match:
            min_age = int(age_match.group(1))
            if profile_age < min_age:
                return {"eligibility": "INELIGIBLE", "eligible": False, "likely_eligible": False, "reasoning": f"Age {profile_age} < {min_age} minimum", "concerns": [f"Minimum age {min_age}"], "requirements": [f"Minimum age {min_age}"], "confidence": 1.0}

    # Student check
    if student_req:
        sr_lower = student_req.lower()
        if "university" in sr_lower or "college" in sr_lower:
            if not profile.is_university_student:
                return {"eligibility": "INELIGIBLE", "eligible": False, "likely_eligible": False, "reasoning": "Requires university/college enrollment", "concerns": ["University student required"], "requirements": ["University student required"], "confidence": 0.9}

    # If rules are complex, use LLM
    if eligibility_text and len(eligibility_text) > 50:
        try:
            result = interpret_eligibility(full_rules, profile.data)
            if result:
                state = str(result.get("eligibility", "")).upper()
                if state not in {"ELIGIBLE", "INELIGIBLE", "UNCERTAIN"}:
                    if result.get("eligible") is True and float(result.get("confidence", 0.0) or 0.0) >= 0.75:
                        state = "ELIGIBLE"
                    elif result.get("eligible") is False and float(result.get("confidence", 0.0) or 0.0) >= 0.9:
                        state = "INELIGIBLE"
                    else:
                        state = "UNCERTAIN"
                result["eligibility"] = state
                result.setdefault("requirements", result.get("concerns", []))
                result.setdefault("source_evidence", "")
                result["eligible"] = state == "ELIGIBLE"
                result["likely_eligible"] = state == "ELIGIBLE"
                if state == "UNCERTAIN" and not _has_material_eligibility_requirement(full_rules):
                    return {"eligibility": "ELIGIBLE_NO_EXCLUSION_FOUND", "eligible": True, "likely_eligible": True,
                            "reasoning": "No explicit age, nationality, student, residency, clearance, or professional exclusion found in official event/form material",
                            "requirements": [], "source_evidence": full_rules[:1500], "confidence": 0.75}
                return result
        except Exception as e:
            print(f"[scoring] LLM eligibility check failed: {e}")

    # Default: assume eligible unless blocked by deterministic checks
    return {"eligibility": "ELIGIBLE_NO_EXCLUSION_FOUND", "eligible": True, "likely_eligible": True, "reasoning": "No material exclusion found in official event or public registration material" if not concerns else "; ".join(concerns), "concerns": concerns, "requirements": concerns, "source_evidence": "Deterministic official event/form checks", "confidence": 0.75}


def _has_material_eligibility_requirement(text: str) -> bool:
    lowered = text.lower()
    if "no age, student, nationality, or location requirements are stated" in lowered:
        return False
    return any(marker in lowered for marker in ("must be 18", "must be 21", "citizens only", "security clearance required", "must be a resident", "university enrollment required", "professional experience required"))


def _assess_travel_eligibility(event: dict, profile: ApplicantProfile) -> bool:
    """Check if travel support rules might cover this applicant."""
    # If no travel support, not eligible
    ts = event.get("travel_support", "UNKNOWN")
    if ts in ("NO_TRAVEL_SUPPORT", "NO_TRAVEL_INFORMATION", "UNKNOWN"):
        return False

    # If confirmed flights/reimbursement, check rules
    ts_details = event.get("travel_support_details", "{}")
    if isinstance(ts_details, str):
        try:
            ts_details = json.loads(ts_details)
        except (json.JSONDecodeError, TypeError):
            ts_details = {}

    # Most travel support is for all participants
    return True


def _score_tech_alignment(profile: ApplicantProfile, themes: list[str], description: str) -> float:
    """Score 0-30 how well applicant interests align with event themes."""
    score = 5.0  # baseline
    interests = [i.lower() for i in profile.interests]
    skills = [s.lower() for s in profile.skills]

    for theme in themes:
        theme_lower = theme.lower()
        for interest in interests:
            if theme_lower in interest or interest in theme_lower:
                score += 3.0
                break
        for skill in skills:
            if theme_lower in skill or skill in theme_lower:
                score += 2.0
                break

    return min(score, 30)


def _score_project_relevance(profile: ApplicantProfile, themes: list[str], description: str) -> float:
    """Score 0-25 how relevant applicant's projects are."""
    score = 3.0
    projects = profile.projects

    for project in projects:
        relevant_for = project.get("relevant_for", [])
        for rf in relevant_for:
            rf_lower = rf.lower()
            for theme in themes:
                if rf_lower in theme.lower() or theme.lower() in rf_lower:
                    score += 4.0
                    break
            if rf_lower in description:
                score += 2.0

    return min(score, 25)


def _score_experience_relevance(profile: ApplicantProfile, event: dict) -> float:
    """Score 0-20 based on hackathon and work experience relevance."""
    score = 5.0

    # Multiple hackathons attended
    hackathon_count = len(profile.hackathon_experience)
    if hackathon_count >= 5:
        score += 5.0
    elif hackathon_count >= 2:
        score += 3.0

    # Has wins
    if profile.achievements:
        for ach in profile.achievements:
            ach_text = str(ach).lower()
            if "win" in ach_text or "prize" in ach_text or "1st" in ach_text:
                score += 2.0
                break

    # Work experience
    if profile.work_experience:
        score += 2.0

    # Founder experience
    for we in profile.work_experience:
        if we.get("type") == "founder":
            score += 3.0
            break

    return min(score, 20)


def _score_event_type_fit(profile: ApplicantProfile, event: dict) -> float:
    """Score 0-15 based on event type fit."""
    score = 5.0
    event_name = event.get("event_name", "").lower()
    description = event.get("description", "").lower()
    combined = f"{event_name} {description}"

    # Student hackathons
    if "student" in combined:
        if profile.current_student:
            score += 5.0
        else:
            score -= 2.0

    # Edtech
    if "edtech" in combined or "education" in combined or "learning" in combined:
        if "plue" in str(profile.projects).lower() or "edtech" in [i.lower() for i in profile.interests]:
            score += 5.0

    return min(max(score, 0), 15)


def _should_apply_for_applicant(
    event_score: float,
    fit_score: float,
    eligibility: dict,
    location: dict,
    travel: dict,
    accommodation: dict,
    event: dict,
) -> tuple[bool, str]:
    """Determine if a specific applicant should apply."""
    if not eligibility.get("eligible", False):
        return False, f"Not eligible: {eligibility.get('reasoning', 'Unknown')}"
    if not location.get("allowed"):
        return False, f"Location does not fit: {location.get('reason', 'Unknown')}"
    if not travel.get("meets_requirement"):
        return False, f"Travel requirement not met: {travel.get('reason', 'Unknown')}"
    if not accommodation.get("meets_requirement"):
        return False, f"Accommodation requirement not met: {accommodation.get('reason', 'Unknown')}"
    combined = (event_score + fit_score) / 2

    if combined >= settings.MIN_APPLICATION_SCORE:
        return True, f"Combined score {combined:.0f} >= {settings.MIN_APPLICATION_SCORE}"

    return False, f"Combined score too low ({combined:.0f})"


# --- Event-level scoring (unchanged from original) ---

def _score_travel(event: dict) -> tuple[float, str]:
    status = event.get("travel_support", TravelSupportStatus.UNKNOWN.value)
    confidence = float(event.get("travel_support_confidence", 0))

    status_map = {
        TravelSupportStatus.CONFIRMED_FLIGHTS.value: (15, "Flights confirmed"),
        TravelSupportStatus.CONFIRMED_TRAVEL_REIMBURSEMENT.value: (14, "Travel reimbursement confirmed"),
        TravelSupportStatus.CONFIRMED_TRAVEL_STIPEND.value: (12, "Travel stipend confirmed"),
        TravelSupportStatus.CONFIRMED_ACCOMMODATION_ONLY.value: (4, "Accommodation only"),
        TravelSupportStatus.TRAVEL_SUPPORT_MENTIONED.value: (8, "Travel support mentioned"),
        TravelSupportStatus.POSSIBLE_TRAVEL_SUPPORT.value: (5, "Possible travel support"),
        TravelSupportStatus.NO_TRAVEL_INFORMATION.value: (0, "No travel info"),
        TravelSupportStatus.NO_TRAVEL_SUPPORT.value: (0, "No travel support"),
        TravelSupportStatus.UNKNOWN.value: (0, "Unknown"),
    }

    base_score, reason = status_map.get(status, (0, "Unknown"))
    adjusted = base_score * confidence
    return round(adjusted, 1), f"{reason} (confidence: {confidence})"


def _score_sponsors(event: dict) -> tuple[float, str]:
    sponsors_raw = event.get("sponsors", "[]")
    if isinstance(sponsors_raw, str):
        try:
            sponsors = json.loads(sponsors_raw)
        except (json.JSONDecodeError, TypeError):
            sponsors = []
    else:
        sponsors = sponsors_raw

    if not sponsors:
        return 0, "No sponsors found"

    total = 0.0
    for sponsor in sponsors:
        name = sponsor if isinstance(sponsor, str) else sponsor.get("name", "")
        name_lower = name.lower().strip()
        best_score = 5.0
        for known, score in KNOWN_STRONG_SPONSORS.items():
            if known in name_lower or name_lower in known:
                best_score = max(best_score, score)
                break
        total += best_score

    avg = total / len(sponsors) if sponsors else 0
    count_factor = min(len(sponsors) ** 0.5, 3)
    final = min(avg * count_factor / 3, 20)
    return round(final, 1), f"{len(sponsors)} sponsors"


def _score_technical_relevance(event: dict) -> tuple[float, str]:
    themes_raw = event.get("themes", "[]")
    if isinstance(themes_raw, str):
        try:
            themes = json.loads(themes_raw)
        except (json.JSONDecodeError, TypeError):
            themes = []
    else:
        themes = themes_raw

    description = event.get("description", "")
    event_name = event.get("event_name", "")
    combined = f"{' '.join(themes)} {description} {event_name}".lower()

    max_score = 0.0
    matched = []
    for theme, score in TECHNICAL_THEMES.items():
        if theme in combined:
            max_score = max(max_score, score)
            matched.append(theme)

    final = min(max_score + len(matched) * 0.5, 25)
    return round(final, 1), f"Themes: {', '.join(matched[:5])}" if matched else "No themes identified"


def _score_event_quality(event: dict) -> tuple[float, str]:
    score = 5.0
    if event.get("organizer"):
        score += 1.0
    judges_raw = event.get("judges", "[]")
    try:
        judges = json.loads(judges_raw) if isinstance(judges_raw, str) else judges_raw
    except (json.JSONDecodeError, TypeError):
        judges = []
    if judges:
        score += 1.0
    if event.get("description"):
        score += 0.5
    p_type = event.get("physical_or_online", "").lower()
    if p_type in ("physical", "in-person", "in person"):
        score += 2.0
    elif p_type == "hybrid":
        score += 1.0
    return round(min(score, 15), 1), f"Organizer: {'yes' if event.get('organizer') else 'no'}"


def _score_prizes(event: dict) -> tuple[float, str]:
    score = 3.0
    prizes_raw = event.get("prizes", "[]")
    try:
        prizes = json.loads(prizes_raw) if isinstance(prizes_raw, str) else prizes_raw
    except (json.JSONDecodeError, TypeError):
        prizes = []
    if prizes:
        score += 2.0
    prizes_text = str(prizes).lower()
    import re
    if re.search(r'\$|€|usd|eur|cash|prize\s*pool', prizes_text):
        score += 2.0
    if re.search(r'credits?|hardware|gpu', prizes_text):
        score += 1.0
    return round(min(score, 15), 1), "Has prizes" if prizes else "No prizes listed"


def _score_logistics(event: dict) -> tuple[float, str]:
    """Event-level completeness only; applicant location is scored separately."""
    score = 2.0
    if event.get("city") and event.get("country"):
        score += 3.0
    if event.get("venue"):
        score += 1.0
    if event.get("start_date") and event.get("end_date"):
        score += 2.0
    p_type = event.get("physical_or_online", "").lower()
    if p_type in ("physical", "in-person", "in person", "hybrid", "online"):
        score += 1.0
    return round(min(score, 10.0), 1), "Event logistics completeness"


def score_all_events() -> list[dict]:
    """Score all events in DISCOVERED/RESEARCHING status, including per-applicant fit."""
    from hackathon_searcher.database import get_events_needing_research
    events = get_events_needing_research()
    results = []
    for event in events:
        try:
            result = score_event(event["event_id"])
            result["applicant_scores"] = {}
            for applicant_id in profile_manager.applicant_ids:
                fit = score_applicant_fit(event["event_id"], applicant_id)
                result["applicant_scores"][applicant_id] = fit
            results.append(result)
        except Exception as e:
            print(f"[scoring] Error scoring event {event.get('event_id')}: {e}")
    return results
