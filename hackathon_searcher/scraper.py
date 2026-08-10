"""
Hackathon Hub scraper.

Discovers hackathons from hackathonhub.eu using the best available source:
1. Public API (if discoverable)
2. Structured page data / embedded JSON
3. HTML scraping
4. Browser automation as fallback

Never attempts to circumvent authentication, CAPTCHAs, or anti-bot protections.
"""

import json
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from hackathon_searcher.database import (
    make_fingerprint,
    event_exists_by_fingerprint,
    get_event_by_fingerprint,
    insert_event,
    update_event,
)
from hackathon_searcher.models import TravelSupportStatus
from hackathon_searcher.settings import settings


HACKATHON_HUB_URL = settings.HACKATHON_HUB_URL
USER_AGENT = "HackathonSearcher/0.1 (+https://github.com; personal agent)"


def _make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=30,
        follow_redirects=True,
    )


# --- Event extraction from HTML ---

def _extract_embedded_json(html: str) -> Optional[dict]:
    """Try to find embedded JSON data in script tags (Next.js __NEXT_DATA__, etc.)."""
    soup = BeautifulSoup(html, "lxml")

    # Next.js __NEXT_DATA__
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data and next_data.string:
        try:
            data = json.loads(next_data.string)
            return data
        except json.JSONDecodeError:
            pass

    # Generic JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        if script.string:
            try:
                return json.loads(script.string)
            except json.JSONDecodeError:
                pass

    # Look for window.__INITIAL_STATE__ or similar
    for script in soup.find_all("script"):
        if script.string:
            for pattern in [
                r"window\.__INITIAL_STATE__\s*=\s*({.*?});",
                r"window\.__DATA__\s*=\s*({.*?});",
                r"window\.__PRELOADED_STATE__\s*=\s*({.*?});",
            ]:
                match = re.search(pattern, script.string, re.DOTALL)
                if match:
                    try:
                        return json.loads(match.group(1))
                    except json.JSONDecodeError:
                        pass

    return None


def _extract_events_from_html(html: str, base_url: str) -> list[dict]:
    """Extract events from HTML using known patterns."""
    soup = BeautifulSoup(html, "lxml")
    events = []

    # Common patterns for event/hackathon cards
    card_selectors = [
        ".event-card", ".hackathon-card", '[class*="event"]', '[class*="hackathon"]',
        "article", ".card", '[class*="Card"]',
        "a[href*='/events/']", "a[href*='/hackathon/']",
    ]

    seen_urls = set()

    for selector in card_selectors:
        for card in soup.select(selector):
            # If it's an <a> tag, use it directly
            if card.name == "a":
                event_url = urljoin(base_url, card.get("href", ""))
                if event_url in seen_urls:
                    continue
                seen_urls.add(event_url)

                name_el = card.find(["h2", "h3", "h4", "span"], class_=re.compile(r"title|name", re.I))
                event_name = name_el.get_text(strip=True) if name_el else card.get_text(strip=True)

                events.append({
                    "event_name": event_name[:200],
                    "hackathonhub_url": event_url,
                    "source_element": "link_card",
                })
                continue

            # Otherwise, find links inside the card
            link = card.find("a", href=re.compile(r"/events?/|/hackathon"))
            if not link:
                continue
            event_url = urljoin(base_url, link.get("href", ""))
            if event_url in seen_urls:
                continue
            seen_urls.add(event_url)

            name_el = card.find(["h2", "h3", "h4", "span"], class_=re.compile(r"title|name", re.I))
            event_name = name_el.get_text(strip=True) if name_el else card.get_text(strip=True)

            # Try to extract date/location
            date_el = card.find(["time", "span"], class_=re.compile(r"date", re.I))
            location_el = card.find(["span", "div"], class_=re.compile(r"location|city|place", re.I))

            events.append({
                "event_name": event_name[:200],
                "hackathonhub_url": event_url,
                "city": location_el.get_text(strip=True) if location_el else "",
                "start_date": date_el.get_text(strip=True) if date_el else "",
                "source_element": "card",
            })

    return events


def _extract_event_details_from_page(html: str, url: str) -> dict:
    """Extract detailed event information from an individual event page."""
    soup = BeautifulSoup(html, "lxml")
    details: dict = {}

    # Title
    title_el = soup.find(["h1", "h2"], class_=re.compile(r"title|heading", re.I))
    if not title_el:
        title_el = soup.find("title")
    details["event_name"] = title_el.get_text(strip=True) if title_el else ""

    # Description - look for main content
    for selector in ["main p", "article p", ".description p", ".content p", '[class*="description"]']:
        desc_el = soup.select_one(selector)
        if desc_el:
            details["description"] = desc_el.get_text(strip=True)[:2000]
            break

    # External link
    ext_link = soup.find("a", href=re.compile(r"https?://"), string=re.compile(r"website|register|apply|visit", re.I))
    if not ext_link:
        ext_link = soup.find("a", class_=re.compile(r"external|website|link|button", re.I))
    if ext_link:
        href = ext_link.get("href", "")
        if urlparse(href).netloc not in ("hackathonhub.eu", "www.hackathonhub.eu", ""):
            details["event_url"] = href

    # Dates
    for time_el in soup.find_all("time"):
        datetime_attr = time_el.get("datetime", "")
        if datetime_attr:
            details.setdefault("start_date", datetime_attr)

    # Location
    location_el = soup.find(["span", "div"], class_=re.compile(r"location|city|venue", re.I))
    if location_el:
        location_text = location_el.get_text(strip=True)
        # Try to parse city, country
        parts = [p.strip() for p in location_text.split(",")]
        if len(parts) >= 1:
            details["city"] = parts[0]
        if len(parts) >= 2:
            details["country"] = parts[-1]

    details["hackathonhub_url"] = url
    return details


# --- Main scraping functions ---

def fetch_hackathon_hub() -> str:
    """Fetch the Hackathon Hub main page."""
    client = _make_client()
    try:
        resp = client.get(HACKATHON_HUB_URL)
        resp.raise_for_status()
        return resp.text
    finally:
        client.close()


def fetch_event_page(url: str) -> Optional[str]:
    """Fetch an individual event page."""
    client = _make_client()
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text
    finally:
        client.close()


def discover_events() -> list[dict]:
    """
    Main discovery function.

    Returns a list of raw event dicts discovered from Hackathon Hub.
    Each dict has at minimum: event_name, hackathonhub_url.
    """
    print(f"[scraper] Fetching {HACKATHON_HUB_URL}...")
    try:
        html = fetch_hackathon_hub()
    except Exception as e:
        print(f"[scraper] Failed to fetch Hackathon Hub: {e}")
        return []

    # Try embedded JSON first
    embedded = _extract_embedded_json(html)
    if embedded:
        print("[scraper] Found embedded JSON data")
        # Try to extract events from common Next.js page props structures
        events = _extract_events_from_json(embedded)
        if events:
            print(f"[scraper] Extracted {len(events)} events from embedded JSON")
            return events

    # Fall back to HTML extraction
    print("[scraper] Falling back to HTML extraction")
    events = _extract_events_from_html(html, HACKATHON_HUB_URL)
    print(f"[scraper] Extracted {len(events)} events from HTML")
    return events


def _extract_events_from_json(data: dict) -> list[dict]:
    """Extract events from embedded JSON data (handles common patterns)."""
    events = []

    def _search(obj, depth=0):
        if depth > 10:
            return
        if isinstance(obj, dict):
            # Common keys that might contain event lists
            for key in ["events", "hackathons", "items", "data", "results", "listings", "posts"]:
                if key in obj and isinstance(obj[key], list):
                    for item in obj[key]:
                        if isinstance(item, dict):
                            event = _parse_json_event(item)
                            if event:
                                events.append(event)
            for v in obj.values():
                _search(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _search(item, depth + 1)

    _search(data)
    return events


def _parse_json_event(item: dict) -> Optional[dict]:
    """Parse a single event from JSON data."""
    name = (
        item.get("title") or item.get("name") or item.get("event_name")
        or item.get("Title") or item.get("Name") or ""
    )
    if not name:
        return None

    url = (
        item.get("url") or item.get("link") or item.get("href")
        or item.get("slug") or ""
    )
    if url and not url.startswith("http"):
        url = urljoin(HACKATHON_HUB_URL, url)

    return {
        "event_name": str(name)[:200],
        "hackathonhub_url": str(url) if url else "",
        "organizer": str(item.get("organizer", item.get("organizer_name", ""))),
        "city": str(item.get("city", item.get("location", ""))),
        "country": str(item.get("country", "")),
        "start_date": str(item.get("start_date", item.get("date", item.get("startDate", "")))),
        "end_date": str(item.get("end_date", item.get("endDate", ""))),
        "description": str(item.get("description", item.get("summary", "")))[:2000],
        "physical_or_online": str(item.get("type", item.get("format", "unknown"))),
        "source": "json",
    }


def scrape_event_details(event: dict) -> dict:
    """Scrape detailed information from an individual event page."""
    url = event.get("hackathonhub_url", "")
    if not url:
        return event

    print(f"[scraper] Fetching event page: {url}")
    try:
        html = fetch_event_page(url)
    except Exception as e:
        print(f"[scraper] Failed to fetch event page {url}: {e}")
        return event

    if not html:
        return event

    details = _extract_event_details_from_page(html, url)
    # Merge: don't overwrite existing values with empty ones
    for k, v in details.items():
        if v and not event.get(k):
            event[k] = v

    # Also try embedded JSON on the detail page
    embedded = _extract_embedded_json(html)
    if embedded:
        json_events = _extract_events_from_json(embedded)
        if json_events:
            for k, v in json_events[0].items():
                if v and not event.get(k):
                    event[k] = v

    return event


def classify_event(event: dict) -> str:
    """
    Classify an event against the database.

    Returns: 'NEW', 'UPDATED', 'KNOWN', or 'CLOSED'
    """
    fingerprint = make_fingerprint(
        event.get("event_name", ""),
        event.get("start_date", ""),
        event.get("organizer", ""),
        event.get("city", "")
    )

    existing = get_event_by_fingerprint(fingerprint)
    if not existing:
        return "NEW"

    # Check if relevant fields changed
    check_fields = [
        "event_url", "application_url", "application_deadline",
        "description", "sponsors", "themes"
    ]
    for field in check_fields:
        old_val = str(existing.get(field, "")).strip()
        new_val = str(event.get(field, "")).strip()
        if old_val != new_val and new_val:
            return "UPDATED"

    return "KNOWN"


def process_discovered_events(raw_events: list[dict]) -> tuple[list[str], list[str], int]:
    """
    Process discovered events: classify, store new/updated, skip known.

    Returns: (new_ids, updated_ids, skipped_count)
    """
    new_ids = []
    updated_ids = []

    for event in raw_events:
        classification = classify_event(event)

        if classification == "KNOWN":
            continue

        # For new/updated, scrape the detail page for more info
        event = scrape_event_details(event)

        fingerprint = make_fingerprint(
            event.get("event_name", ""),
            event.get("start_date", ""),
            event.get("organizer", ""),
            event.get("city", "")
        )

        event["fingerprint"] = fingerprint

        if classification == "NEW":
            event["event_id"] = f"evt_{fingerprint[:16]}"
            event["status"] = "DISCOVERED"
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
                              "description", "sponsors", "themes"]:
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
