"""Deterministic location and travel-preference evaluation.

This module deliberately does not call an LLM.  It evaluates structured event
location and explicit travel facts before expensive research or form work.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from hackathon_searcher.profile import ApplicantProfile

TRAVEL_SCOPES = ("city", "country", "region", "anywhere")
SUPPORT_PREFERENCES = ("not_important", "preferred", "required")
ACCOMMODATION_PREFERENCES = ("not_important", "preferred", "required")
SUPPORT_TYPES = ("flight_credits", "train_credits", "travel_reimbursement", "accommodation", "any_travel_support")

# The set is intentionally isolated from policy so additional regions can be
# added later without changing the preference schema.
EUROPE_COUNTRIES = {
    "albania", "andorra", "armenia", "austria", "azerbaijan", "belarus", "belgium", "bosnia and herzegovina",
    "bulgaria", "croatia", "cyprus", "czech republic", "czechia", "denmark", "estonia", "finland", "france",
    "georgia", "germany", "greece", "hungary", "iceland", "ireland", "italy", "kosovo", "latvia", "liechtenstein",
    "lithuania", "luxembourg", "malta", "moldova", "monaco", "montenegro", "netherlands", "north macedonia",
    "norway", "poland", "portugal", "romania", "san marino", "serbia", "slovakia", "slovenia", "spain", "sweden",
    "switzerland", "turkey", "ukraine", "united kingdom", "vatican city",
}
COUNTRY_ALIASES = {
    "se": "sweden", "sverige": "sweden", "de": "germany", "deutschland": "germany", "fr": "france", "fi": "finland",
    "no": "norway", "dk": "denmark", "gb": "united kingdom", "uk": "united kingdom", "nl": "netherlands",
    "be": "belgium", "es": "spain", "it": "italy", "pt": "portugal", "ie": "ireland", "pl": "poland",
    "at": "austria", "ch": "switzerland", "ee": "estonia", "lv": "latvia", "lt": "lithuania", "cz": "czechia",
}


def _normalized(value: object) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(char for char in raw if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", raw).strip().lower()


def normalize_country(value: object) -> str:
    normalized = _normalized(value)
    return COUNTRY_ALIASES.get(normalized, normalized)


def normalize_city(value: object) -> str:
    return _normalized(value)


def _list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def travel_preferences(profile: ApplicantProfile) -> dict[str, Any]:
    """Return a canonical preference object while safely interpreting v1 data."""
    raw = profile.data.get("travel_preferences", {})
    raw = raw if isinstance(raw, dict) else {}
    legacy_regions = {_normalized(value) for value in _list(profile.data.get("travel_regions", []))}
    legacy_support = str(profile.data.get("travel_support_preference", "")).lower()
    scope = str(raw.get("scope", "")).lower()
    if scope not in TRAVEL_SCOPES:
        scope = "region" if "europe" in legacy_regions else "anywhere"
    support = str(raw.get("travel_support", legacy_support)).lower()
    if support not in SUPPORT_PREFERENCES:
        # Legacy `travel_support_wanted` meant it was a bonus, not a proof that
        # the person could never attend without it.
        support = "preferred" if profile.data.get("travel_support_wanted") else "not_important"
    accommodation = str(raw.get("accommodation", "not_important")).lower()
    if accommodation not in ACCOMMODATION_PREFERENCES:
        accommodation = "not_important"
    accepted = [str(item).lower() for item in _list(raw.get("accepted_support", []))]
    accepted = [item for item in accepted if item in SUPPORT_TYPES]
    if support != "not_important" and not accepted:
        accepted = ["any_travel_support"]
    minimum = raw.get("minimum_reimbursement_eur")
    try:
        minimum = float(minimum) if minimum not in (None, "") else None
    except (TypeError, ValueError):
        minimum = None
    return {
        "scope": scope,
        "region": str(raw.get("region", "europe")).lower() or "europe",
        "travel_support": support,
        "accepted_support": accepted,
        "minimum_reimbursement_eur": minimum if minimum is None or minimum >= 0 else None,
        "accommodation": accommodation,
        "include_remote": bool(raw.get("include_remote", True)),
    }


def _event_mode(event: dict[str, Any]) -> str:
    return _normalized(event.get("physical_or_online", "unknown"))


def assess_location(event: dict[str, Any], profile: ApplicantProfile) -> dict[str, Any]:
    prefs = travel_preferences(profile)
    mode = _event_mode(event)
    event_city = normalize_city(event.get("city"))
    event_country = normalize_country(event.get("country"))
    home_city, home_country = normalize_city(profile.city), normalize_country(profile.country)
    if mode == "online":
        if prefs["include_remote"]:
            return {"allowed": True, "status": "REMOTE_ALLOWED", "fit": 8.0, "reason": "Remote event allowed by profile"}
        return {"allowed": False, "status": "REMOTE_DISABLED", "fit": 0.0, "reason": "Remote events are disabled in profile"}
    if prefs["scope"] == "city":
        allowed = bool(home_city and event_city and home_city == event_city and (not home_country or not event_country or home_country == event_country))
        return {"allowed": allowed, "status": "LOCAL" if allowed else "OUTSIDE_CITY", "fit": 10.0 if allowed else 0.0,
                "reason": "Same city" if allowed else "Outside the configured home city"}
    if prefs["scope"] == "country":
        allowed = bool(home_country and event_country and home_country == event_country)
        return {"allowed": allowed, "status": "SAME_COUNTRY" if allowed else "OUTSIDE_COUNTRY", "fit": 9.0 if allowed else 0.0,
                "reason": "Within home country" if allowed else "Outside the configured home country"}
    if prefs["scope"] == "region":
        if prefs["region"] != "europe":
            return {"allowed": False, "status": "UNSUPPORTED_REGION", "fit": 0.0, "reason": f"Unsupported region: {prefs['region']}"}
        allowed = event_country in EUROPE_COUNTRIES
        if event_country == home_country and event_city == home_city:
            fit = 10.0
        elif event_country == home_country:
            fit = 8.5
        elif allowed:
            fit = 6.5
        else:
            fit = 0.0
        return {"allowed": allowed, "status": "REGION_MATCH" if allowed else "OUTSIDE_REGION", "fit": fit,
                "reason": "Within configured Europe region" if allowed else "Outside configured Europe region"}
    # A global preference intentionally does not reject an event with a missing
    # location. It still ranks known local events above distant ones.
    if event_country and event_country == home_country and event_city == home_city:
        fit = 10.0
    elif event_country and event_country == home_country:
        fit = 8.0
    elif event_country in EUROPE_COUNTRIES and home_country in EUROPE_COUNTRIES:
        fit = 6.0
    else:
        fit = 4.0
    return {"allowed": True, "status": "ANYWHERE", "fit": fit, "reason": "Global travel scope"}


def _details(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("travel_support_details", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {"source_text": raw}
    return raw if isinstance(raw, dict) else {}


def _support_types(event: dict[str, Any], details: dict[str, Any]) -> set[str]:
    status = str(event.get("official_travel_status") or event.get("final_travel_status") or event.get("travel_support") or "").upper()
    text = " ".join(str(value) for value in (status, details.get("source_text", ""), details.get("llm_extracted", ""), details.get("conditions", ""))).lower()
    types: set[str] = set()
    if "FLIGHT" in status or "flight" in text:
        types.add("flight_credits")
    if "train" in text or "rail" in text:
        types.add("train_credits")
    if "REIMBURSEMENT" in status or "STIPEND" in status or "reimburse" in text or "stipend" in text or details.get("travel_costs_covered"):
        types.add("travel_reimbursement")
    if "ACCOMMODATION" in status or "hotel" in text or details.get("accommodation_provided") or details.get("accommodation_costs_covered"):
        types.add("accommodation")
    return types


def _amount_eur(event: dict[str, Any], details: dict[str, Any]) -> float | None:
    raw = details.get("amount", details.get("max_reimbursement", event.get("travel_support_amount", "")))
    currency = str(details.get("currency", event.get("travel_support_currency", "EUR"))).upper()
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        match = re.search(r"(?:€|EUR\s*)(\d+(?:[.,]\d+)?)", str(raw))
        amount = float(match.group(1).replace(",", ".")) if match else None
    return amount if amount is not None and currency in {"", "EUR", "€"} else None


def assess_travel_support(event: dict[str, Any], profile: ApplicantProfile) -> dict[str, Any]:
    prefs, details = travel_preferences(profile), _details(event)
    offered, amount = _support_types(event, details), _amount_eur(event, details)
    status = str(event.get("official_travel_status") or event.get("final_travel_status") or event.get("travel_support") or "UNKNOWN").upper()
    unknown = status in {"", "UNKNOWN", "NO_TRAVEL_INFORMATION", "UNVERIFIED_TRAVEL_SUPPORT"}
    absent = status == "NO_TRAVEL_SUPPORT" or (not unknown and not offered)
    accepted = set(prefs["accepted_support"])
    matching_support = {kind for kind in offered if "any_travel_support" in accepted or kind in accepted}
    minimum = prefs["minimum_reimbursement_eur"]
    amount_ok = minimum is None or (amount is not None and amount >= minimum)
    useful = bool(matching_support and amount_ok)
    if prefs["travel_support"] == "not_important":
        return {"meets_requirement": True, "status": "NOT_REQUIRED", "useful": useful, "fit": 5.0,
                "reason": "Travel support is not important", "offered_types": sorted(offered), "amount_eur": amount}
    if prefs["travel_support"] == "preferred":
        return {"meets_requirement": True, "status": "PREFERRED_MATCH" if useful else ("UNKNOWN" if unknown else "NOT_MATCHED"),
                "useful": useful, "fit": 10.0 if useful else 5.0, "reason": "Useful preferred support found" if useful else "Travel support preferred but not required",
                "offered_types": sorted(offered), "amount_eur": amount}
    if unknown:
        return {"meets_requirement": False, "status": "UNKNOWN_REQUIRED", "useful": False, "fit": 0.0,
                "reason": "Required travel support is not verified", "offered_types": sorted(offered), "amount_eur": amount}
    if absent or not matching_support:
        return {"meets_requirement": False, "status": "UNSUPPORTED_REQUIRED", "useful": False, "fit": 0.0,
                "reason": "Event does not offer an accepted travel support type", "offered_types": sorted(offered), "amount_eur": amount}
    if not amount_ok:
        return {"meets_requirement": False, "status": "BELOW_MINIMUM", "useful": False, "fit": 0.0,
                "reason": f"Verified reimbursement is below the configured €{minimum:g} minimum", "offered_types": sorted(offered), "amount_eur": amount}
    return {"meets_requirement": True, "status": "REQUIRED_MATCH", "useful": True, "fit": 10.0,
            "reason": "Required transport support is verified", "offered_types": sorted(offered), "amount_eur": amount}


def assess_accommodation(event: dict[str, Any], profile: ApplicantProfile) -> dict[str, Any]:
    prefs, details = travel_preferences(profile), _details(event)
    offered = "accommodation" in _support_types(event, details)
    preference = prefs["accommodation"]
    if preference == "not_important":
        return {"meets_requirement": True, "status": "NOT_REQUIRED", "fit": 0.0, "reason": "Accommodation is not important"}
    if preference == "preferred":
        return {"meets_requirement": True, "status": "PREFERRED_MATCH" if offered else "NOT_MATCHED", "fit": 3.0 if offered else 0.0,
                "reason": "Accommodation preferred"}
    return {"meets_requirement": offered, "status": "REQUIRED_MATCH" if offered else "UNSUPPORTED_REQUIRED", "fit": 3.0 if offered else 0.0,
            "reason": "Required accommodation is verified" if offered else "Required accommodation is not verified"}


def requirements_met(event: dict[str, Any], profile: ApplicantProfile) -> dict[str, Any]:
    location, support, accommodation = assess_location(event, profile), assess_travel_support(event, profile), assess_accommodation(event, profile)
    return {"location": location, "support": support, "accommodation": accommodation,
            "passes": bool(location["allowed"] and support["meets_requirement"] and accommodation["meets_requirement"])}
