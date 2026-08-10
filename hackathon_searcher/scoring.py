"""
Scoring engine for hackathons.

Scores every event from 0-100 based on:
- Travel support (0-40)
- Sponsor quality (0-20)
- Technical relevance (0-15)
- Event quality (0-10)
- Prize/opportunity (0-10)
- Logistics (0-5)

Also determines whether Philip should apply based on configured thresholds.
"""

import json
import re
from typing import Optional

from hackathon_searcher.database import get_event_by_id, update_event, log_audit
from hackathon_searcher.models import TravelSupportStatus, EventAnalysis
from hackathon_searcher.profile import profile
from hackathon_searcher.settings import settings


# Known strong sponsors (examples, not a fixed whitelist)
# Scored by estimated reputation/quality
KNOWN_STRONG_SPONSORS = {
    # Tier 1: Top AI companies
    "anthropic": 19, "openai": 19, "google": 18, "microsoft": 18,
    "nvidia": 18, "meta": 17, "deepmind": 19,
    # Tier 2: Major tech / infra
    "aws": 17, "cloudflare": 16, "github": 16, "stripe": 17,
    "supabase": 15, "vercel": 15, "elevenlabs": 15, "hugging face": 16,
    "mistral": 15, "groq": 15, "cursor": 14, "lovable": 13,
    "replit": 14,
    # Tier 3: Strong startup ecosystem
    "ramp": 14, "revolut": 14, "klarna": 14, "spotify": 14,
    "notion": 14, "figma": 15, "linear": 14,
    # Venture capital
    "y combinator": 18, "sequoia": 17, "accel": 16,
    "index ventures": 16, "general catalyst": 16, "a16z": 17,
    "antler": 15, "founders fund": 16,
    # Hardware / robotics / defense
    "anduril": 16, "boston dynamics": 16, "tesla": 15,
    "spacex": 16, "palantir": 15, "lockheed": 14,
    # Fintech
    "plaid": 15, "wise": 14, "monzo": 13,
    # European
    "spotify": 14, "klarna": 14, "revolut": 14, "delivery hero": 12,
    "zalando": 12, "booking.com": 13, "adyen": 14,
    # Other
    "mongodb": 14, "datadog": 14, "sentry": 13, "redis": 14,
    "twilio": 14, "netlify": 13, "docker": 15,
}

# Technical themes and their relevance scores
TECHNICAL_THEMES = {
    "ai": 14, "artificial intelligence": 14, "machine learning": 13,
    "robotics": 15, "hardware": 14, "drones": 15, "autonomy": 15,
    "computer vision": 14, "agents": 15, "llm": 14, "nlp": 13,
    "developer tools": 13, "devtools": 13, "infrastructure": 13,
    "fintech": 13, "payments": 13, "crypto": 10, "blockchain": 10,
    "cybersecurity": 12, "security": 12, "defense": 13,
    "startups": 12, "entrepreneurship": 11, "startup": 12,
    "gaming": 9, "health": 10, "climate": 10, "sustainability": 9,
    "education": 9, "social good": 8, "web3": 9,
    "mobile": 10, "web": 9, "data": 10, "analytics": 10,
    "open source": 11, "api": 10,
}


def score_event(event_id: str) -> dict:
    """
    Score a single event and determine whether to apply.

    Returns a dict with score breakdown and application decision.
    """
    event = get_event_by_id(event_id)
    if not event:
        print(f"[scoring] Event {event_id} not found")
        return {}

    # --- Travel support score (0-40) ---
    travel_score, travel_reason = _score_travel(event)

    # --- Sponsor quality score (0-20) ---
    sponsor_score, sponsor_reason = _score_sponsors(event)

    # --- Technical relevance (0-15) ---
    tech_score, tech_reason = _score_technical_relevance(event)

    # --- Event quality (0-10) ---
    event_quality_score, event_quality_reason = _score_event_quality(event)

    # --- Prize / opportunity (0-10) ---
    prize_score, prize_reason = _score_prizes(event)

    # --- Logistics (0-5) ---
    logistics_score, logistics_reason = _score_logistics(event)

    total_score = round(
        travel_score + sponsor_score + tech_score
        + event_quality_score + prize_score + logistics_score, 1
    )

    # --- Determine whether to apply ---
    travel_status = event.get("travel_support", TravelSupportStatus.UNKNOWN.value)
    should_apply, apply_reason = _should_apply(total_score, travel_status, event)

    # Build reasoning
    reasoning = f"""Travel: {travel_score}/40 - {travel_reason}
Sponsors: {sponsor_score}/20 - {sponsor_reason}
Tech relevance: {tech_score}/15 - {tech_reason}
Event quality: {event_quality_score}/10 - {event_quality_reason}
Prize/opportunity: {prize_score}/10 - {prize_reason}
Logistics: {logistics_score}/5 - {logistics_reason}
Total: {total_score}/100
Apply: {'YES' if should_apply else 'NO'} - {apply_reason}"""

    # Update event in database
    updates = {
        "score": total_score,
        "score_reasoning": reasoning,
        "confidence": min(travel_score / 40, 1.0) * 0.5 + 0.3,  # Rough confidence
    }

    if should_apply:
        updates["status"] = "QUALIFIED"
    elif total_score < 40:
        updates["status"] = "SKIPPED"

    try:
        update_event(event_id, updates)
        log_audit(event_id, "SCORED", f"Score: {total_score}/100, Apply: {should_apply}")
    except Exception as e:
        print(f"[scoring] Failed to update event {event_id}: {e}")

    return {
        "event_id": event_id,
        "total_score": total_score,
        "travel_score": travel_score,
        "sponsor_score": sponsor_score,
        "tech_score": tech_score,
        "event_quality_score": event_quality_score,
        "prize_score": prize_score,
        "logistics_score": logistics_score,
        "should_apply": should_apply,
        "reasoning": reasoning,
    }


def _score_travel(event: dict) -> tuple[float, str]:
    """Score travel support from 0-40."""
    status = event.get("travel_support", TravelSupportStatus.UNKNOWN.value)
    confidence = float(event.get("travel_support_confidence", 0))

    status_map = {
        TravelSupportStatus.CONFIRMED_FLIGHTS.value: (40, 0.9),
        TravelSupportStatus.CONFIRMED_TRAVEL_REIMBURSEMENT.value: (38, 0.85),
        TravelSupportStatus.CONFIRMED_TRAVEL_STIPEND.value: (32, 0.8),
        TravelSupportStatus.CONFIRMED_ACCOMMODATION_ONLY.value: (10, 0.9),
        TravelSupportStatus.TRAVEL_SUPPORT_MENTIONED.value: (25, 0.6),
        TravelSupportStatus.POSSIBLE_TRAVEL_SUPPORT.value: (18, 0.4),
        TravelSupportStatus.NO_TRAVEL_INFORMATION.value: (0, 0.0),
        TravelSupportStatus.NO_TRAVEL_SUPPORT.value: (0, 0.9),
        TravelSupportStatus.UNKNOWN.value: (0, 0.0),
    }

    base_score, base_conf = status_map.get(status, (0, 0))
    # Adjust by confidence
    adjusted = base_score * confidence
    reason = f"{status} (confidence: {confidence})"

    return round(adjusted, 1), reason


def _score_sponsors(event: dict) -> tuple[float, str]:
    """Score sponsor quality from 0-20."""
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

    total_sponsor_score = 0.0
    scored_sponsors = []

    for sponsor in sponsors:
        name = sponsor if isinstance(sponsor, str) else sponsor.get("name", "")
        if not name:
            continue
        name_lower = name.lower().strip()

        # Check against known sponsors
        best_score = 5.0  # Default for unknown sponsors
        for known, score in KNOWN_STRONG_SPONSORS.items():
            if known in name_lower or name_lower in known:
                best_score = max(best_score, score)
                break

        total_sponsor_score += best_score
        scored_sponsors.append(f"{name}:{best_score}")

    # Cap and normalize: more sponsors = higher score, but diminishing returns
    # Average sponsor quality, scaled by sqrt of count
    avg_quality = total_sponsor_score / len(scored_sponsors) if scored_sponsors else 0
    count_factor = min(len(scored_sponsors) ** 0.5, 3)  # sqrt cap
    final = min(avg_quality * count_factor / 3, 20)

    reason = f"{len(scored_sponsors)} sponsors found"
    return round(final, 1), reason


def _score_technical_relevance(event: dict) -> tuple[float, str]:
    """Score technical relevance from 0-15."""
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

    combined_text = f"{' '.join(themes) if themes else ''} {description} {event_name}".lower()

    max_theme_score = 0.0
    matched_themes = []

    for theme, score in TECHNICAL_THEMES.items():
        if theme in combined_text:
            max_theme_score = max(max_theme_score, score)
            matched_themes.append(theme)

    # Also check Philip's interests against the event
    interest_bonus = 0
    for interest in profile.interests:
        if interest.lower() in combined_text:
            interest_bonus += 1

    final = min(max_theme_score + interest_bonus, 15)
    reason = f"Themes: {', '.join(matched_themes[:5])}" if matched_themes else "No technical themes identified"

    return round(final, 1), reason


def _score_event_quality(event: dict) -> tuple[float, str]:
    """Score event quality from 0-10."""
    score = 5.0  # Start at neutral

    # Organizer reputation
    organizer = event.get("organizer", "")
    if organizer:
        score += 1.0

    # Has judges
    judges_raw = event.get("judges", "[]")
    if isinstance(judges_raw, str):
        try:
            judges = json.loads(judges_raw)
        except (json.JSONDecodeError, TypeError):
            judges = []
    else:
        judges = judges_raw
    if judges:
        score += 1.0

    # Has description
    if event.get("description", ""):
        score += 0.5

    # Physical events score higher
    if event.get("physical_or_online", "").lower() in ("physical", "in-person", "in person"):
        score += 2.0
    elif event.get("physical_or_online", "").lower() in ("hybrid",):
        score += 1.0

    final = min(score, 10)
    reason = f"Organizer: {'yes' if organizer else 'no'}, Judges: {'yes' if judges else 'no'}"
    return round(final, 1), reason


def _score_prizes(event: dict) -> tuple[float, str]:
    """Score prizes and opportunities from 0-10."""
    score = 3.0  # Baseline
    prizes_raw = event.get("prizes", "[]")
    if isinstance(prizes_raw, str):
        try:
            prizes = json.loads(prizes_raw)
        except (json.JSONDecodeError, TypeError):
            prizes = []
    else:
        prizes = prizes_raw

    prizes_text = str(prizes).lower() if prizes else ""

    if prizes:
        score += 2.0

    # Cash prizes
    if re.search(r'\$|€|usd|eur|cash|prize\s*pool', prizes_text):
        score += 2.0

    # API credits / hardware
    if re.search(r'credits?|hardware|gpu', prizes_text):
        score += 1.0

    final = min(score, 10)
    return round(final, 1), f"{'Has prizes' if prizes else 'No prizes listed'}"


def _score_logistics(event: dict) -> tuple[float, str]:
    """Score logistics from 0-5."""
    score = 3.0  # Baseline

    city = event.get("city", "").lower()
    country = event.get("country", "").lower()

    # Distance from Stockholm
    if city == "stockholm" or "stockholm" in city:
        score = 5.0
    elif country in ("sweden", "se") or city in ("uppsala", "göteborg", "gothenburg", "malmö"):
        score = 5.0
    elif country in ("denmark", "dk", "norway", "no", "finland", "fi"):
        score = 4.5
    elif country in ("germany", "de", "uk", "united kingdom", "netherlands", "nl", "belgium", "be"):
        score = 4.0
    elif country in ("france", "fr", "austria", "at", "switzerland", "ch", "poland", "pl"):
        score = 3.5
    else:
        score = 2.5  # Further away but still Europe
        if country and country not in (
            "sweden", "se", "denmark", "dk", "norway", "no", "finland", "fi",
            "germany", "de", "uk", "united kingdom", "netherlands", "nl",
            "belgium", "be", "france", "fr", "austria", "at", "switzerland", "ch",
            "poland", "pl", "italy", "it", "spain", "es", "portugal", "pt",
            "ireland", "ie", "estonia", "ee", "latvia", "lv", "lithuania", "lt",
            "czech", "cz", "hungary", "hu", "romania", "ro"
        ):
            score = 1.5  # Outside Europe - only worth it with flight support

    # Physical events
    if event.get("physical_or_online", "").lower() in ("physical", "in-person", "in person"):
        score = min(score + 0.5, 5.0)
    elif event.get("physical_or_online", "").lower() == "online":
        score -= 1.0  # Online events are less attractive

    reason = f"Location: {city or 'unknown'}, {country or 'unknown'}"
    return round(max(score, 0), 1), reason


def _should_apply(total_score: float, travel_status: str, event: dict) -> tuple[bool, str]:
    """Determine whether Philip should apply based on score and overrides."""
    # Override: confirmed flight reimbursement → ALWAYS apply
    if travel_status in (
        TravelSupportStatus.CONFIRMED_FLIGHTS.value,
        TravelSupportStatus.CONFIRMED_TRAVEL_REIMBURSEMENT.value,
    ):
        return True, "Flight reimbursement confirmed — automatic application"

    # Override: confirmed travel stipend → always apply
    if travel_status == TravelSupportStatus.CONFIRMED_TRAVEL_STIPEND.value:
        return True, "Travel stipend confirmed — automatic application"

    # Default thresholds
    if total_score >= settings.MIN_APPLICATION_SCORE:
        return True, f"Score >= {settings.MIN_APPLICATION_SCORE}"

    if total_score >= 40:
        # Check if there's significant sponsor quality or technical relevance
        sponsors_raw = event.get("sponsors", "[]")
        try:
            sponsors = json.loads(sponsors_raw) if isinstance(sponsors_raw, str) else sponsors_raw
        except (json.JSONDecodeError, TypeError):
            sponsors = []
        if sponsors and len(sponsors) >= 2:
            return True, "Score 40-54 with significant sponsors"

        # Possible travel support
        if travel_status in (
            TravelSupportStatus.POSSIBLE_TRAVEL_SUPPORT.value,
            TravelSupportStatus.TRAVEL_SUPPORT_MENTIONED.value,
        ):
            return True, "Score 40-54 with possible travel support"

    return False, f"Score too low ({total_score})"


def score_all_qualified() -> list[dict]:
    """Score all events that are in DISCOVERED or RESEARCHING status."""
    from hackathon_searcher.database import get_events_needing_research
    events = get_events_needing_research()
    results = []
    for event in events:
        try:
            result = score_event(event["event_id"])
            results.append(result)
        except Exception as e:
            print(f"[scoring] Error scoring event {event.get('event_id')}: {e}")
    return results


import re
