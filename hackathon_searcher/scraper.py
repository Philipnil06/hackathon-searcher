"""
Hackathon Hub scraper — uses Playwright to intercept the Supabase REST API.

The site is a client-side SPA. Instead of scraping HTML or reverse-engineering
auth, we intercept the actual API response the page receives from Supabase.

Discovered events are mapped to our internal schema with all available fields.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

from hackathon_searcher.database import (
    make_fingerprint, event_exists_by_fingerprint,
    get_event_by_fingerprint, insert_event, update_event,
)
from hackathon_searcher.settings import settings

SUPABASE_URL = "https://czcrgiykicowicoufthv.supabase.co"
HACKATHON_HUB_URL = settings.HACKATHON_HUB_URL


def discover_events() -> list[dict]:
    """
    Main discovery function.

    Opens Hackathon Hub in Playwright, waits for the Supabase API call
    to complete, intercepts the JSON response, and maps to our event schema.

    Returns a list of raw event dicts.
    """
    print(f"[scraper] Discovering events from {HACKATHON_HUB_URL}...")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[scraper] Playwright not available, cannot discover events")
        return []

    raw_events = []
    api_data = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            def handle_response(response):
                nonlocal api_data
                url = response.url
                if 'supabase.co/rest/v1/events_public' in url and response.status == 200:
                    try:
                        api_data = response.json()
                    except Exception:
                        pass

            page.on('response', handle_response)
            page.goto(f"{HACKATHON_HUB_URL.rstrip('/')}/events", wait_until="networkidle", timeout=30000)
            # Wait a bit more for data to load
            page.wait_for_timeout(2000)
            browser.close()

    except Exception as e:
        print(f"[scraper] Playwright discovery failed: {e}")
        return []

    if isinstance(api_data, list):
        print(f"[scraper] Intercepted {len(api_data)} events from Supabase API")
        for item in api_data:
            event = _map_api_event(item)
            if event:
                raw_events.append(event)
    elif isinstance(api_data, dict):
        # Might have the data nested
        items = api_data.get('data') or api_data.get('items') or api_data.get('events') or []
        if isinstance(items, list):
            print(f"[scraper] Extracted {len(items)} events from nested API response")
            for item in items:
                event = _map_api_event(item)
                if event:
                    raw_events.append(event)
        else:
            print(f"[scraper] Unexpected API response format: {list(api_data.keys())[:5]}")
    else:
        print(f"[scraper] No events found in API response")

    return raw_events


def _map_api_event(item: dict) -> Optional[dict]:
    """Map a Supabase API event to our internal schema."""
    event_id_raw = item.get("id", "")
    title = item.get("title_en") or item.get("title") or ""
    if not title:
        return None

    # Determine travel support status
    travel_support_status = "NO_TRAVEL_INFORMATION"
    travel_support_confidence = 0.0

    if item.get("travel_costs_covered"):
        travel_support_status = "CONFIRMED_TRAVEL_REIMBURSEMENT"
        travel_support_confidence = 0.85
    elif item.get("accommodation_costs_covered"):
        travel_support_status = "CONFIRMED_ACCOMMODATION_ONLY"
        travel_support_confidence = 0.85
    elif item.get("accommodation_provided"):
        travel_support_status = "CONFIRMED_ACCOMMODATION_ONLY"
        travel_support_confidence = 0.80

    # Build travel support details
    travel_details = {
        "source": "hackathonhub_api",
        "travel_costs_covered": item.get("travel_costs_covered", False),
        "accommodation_provided": item.get("accommodation_provided", False),
        "accommodation_costs_covered": item.get("accommodation_costs_covered", False),
        "meals_included": item.get("meals_included", False),
    }

    # Location
    city = item.get("city") or ""
    country = item.get("country") or ""
    location_type = item.get("location_type", "")

    # Physical/online
    physical_or_online = "unknown"
    if location_type == "in-person":
        physical_or_online = "physical"
    elif location_type == "online":
        physical_or_online = "online"
    elif location_type == "hybrid":
        physical_or_online = "hybrid"

    # Build event URL and external URL
    event_slug = item.get("url") or ""
    hackathonhub_url = f"{HACKATHON_HUB_URL.rstrip('/')}/events/{event_slug}" if event_slug else ""

    # Extract external URL from hackathonhub URL slug if it contains an external URL
    external_url = ""
    if event_slug and event_slug.startswith("http"):
        external_url = event_slug
    elif event_slug and "/events/http" in hackathonhub_url:
        idx = hackathonhub_url.index("/events/") + 8
        external_url = hackathonhub_url[idx:]

    # Parse tags as themes
    tags = item.get("tags", [])
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (json.JSONDecodeError, TypeError):
            tags = []

    # Participants
    participant_limit = ""
    expected = item.get("expected_participants")
    max_team = item.get("max_team_size")
    if expected and max_team:
        participant_limit = f"~{expected} participants, max team {max_team}"
    elif expected:
        participant_limit = f"~{expected} participants"

    event = {
        "event_id": f"evt_{event_id_raw[:16]}" if event_id_raw else f"evt_{hashlib.md5(title.encode()).hexdigest()[:16]}",
        "event_name": title,
        "organizer": item.get("organizer_name", ""),
        "hackathonhub_url": hackathonhub_url,
        "event_url": external_url,
        "application_url": "",  # Will be filled by research
        "city": city,
        "country": country,
        "venue": "",
        "physical_or_online": physical_or_online,
        "start_date": item.get("start_date", "")[:10] if item.get("start_date") else "",
        "end_date": item.get("end_date", "")[:10] if item.get("end_date") else "",
        "application_deadline": item.get("application_deadline", "")[:10] if item.get("application_deadline") else "",
        "description": (item.get("description_en") or item.get("description") or "")[:2000],
        "themes": json.dumps(tags) if isinstance(tags, list) else "[]",
        "sponsors": "[]",
        "judges": "[]",
        "partners": "[]",
        "prizes": item.get("prize_money", "") or "",
        "participant_limit": participant_limit,
        "travel_support": travel_support_status,
        "travel_support_type": travel_support_status,
        "travel_support_confidence": travel_support_confidence,
        "travel_support_source": "hackathonhub_api",
        "travel_support_details": json.dumps(travel_details),
        "flight_credits": "Yes" if item.get("travel_costs_covered") else "",
        "accommodation": "Yes" if item.get("accommodation_provided") else "",
        "food": "Yes" if item.get("meals_included") else "",
        "extra_data": json.dumps({
            "api_id": event_id_raw,
            "level": item.get("level", ""),
            "language": item.get("language", ""),
            "price_min": item.get("price_min"),
            "price_max": item.get("price_max"),
            "registration_status": item.get("registration_status", ""),
            "status": item.get("status", ""),
            "beginner_friendly": item.get("beginner_friendly", False),
            "team_formation_supported": item.get("team_formation_supported", False),
            "on_site_hardware_provided": item.get("on_site_hardware_provided", False),
            "mentoring_available": item.get("mentoring_available", False),
            "reference_number": item.get("reference_number", ""),
            "share_sentence_en": item.get("share_sentence_en", ""),
        }),
    }

    return event


def process_discovered_events(raw_events: list[dict]) -> tuple[list[str], list[str], int]:
    """
    Process discovered events: classify, store new/updated, skip known.
    Returns: (new_ids, updated_ids, skipped_count)
    """
    new_ids = []
    updated_ids = []

    for event in raw_events:
        classification = _classify_event(event)

        if classification == "KNOWN":
            continue

        fingerprint = make_fingerprint(
            event.get("event_name", ""),
            event.get("start_date", ""),
            event.get("organizer", ""),
            event.get("city", "")
        )
        event["fingerprint"] = fingerprint

        if classification == "NEW":
            try:
                insert_event(event)
                new_ids.append(event["event_id"])
            except Exception as e:
                print(f"[scraper] Failed to insert event {event.get('event_name')}: {e}")

        elif classification == "UPDATED":
            existing = get_event_by_fingerprint(fingerprint)
            if existing:
                updates = {}
                for field in ["event_url", "application_url", "application_deadline",
                              "description", "sponsors", "themes", "travel_support",
                              "travel_support_type", "travel_support_confidence"]:
                    new_val = event.get(field, "")
                    if new_val and str(new_val) != str(existing.get(field, "")):
                        updates[field] = new_val
                if updates:
                    try:
                        update_event(existing["event_id"], updates)
                        updated_ids.append(existing["event_id"])
                    except Exception as e:
                        print(f"[scraper] Failed to update event {event.get('event_name')}: {e}")

    skipped = len(raw_events) - len(new_ids) - len(updated_ids)
    return new_ids, updated_ids, skipped


def _classify_event(event: dict) -> str:
    """Classify an event against the database: NEW, UPDATED, KNOWN."""
    fingerprint = make_fingerprint(
        event.get("event_name", ""),
        event.get("start_date", ""),
        event.get("organizer", ""),
        event.get("city", "")
    )

    existing = get_event_by_fingerprint(fingerprint)
    if not existing:
        return "NEW"

    check_fields = [
        "event_url", "application_url", "application_deadline",
        "description", "sponsors", "themes", "travel_support"
    ]
    for field in check_fields:
        old_val = str(existing.get(field, "")).strip()
        new_val = str(event.get(field, "")).strip()
        if old_val != new_val and new_val:
            return "UPDATED"

    return "KNOWN"
