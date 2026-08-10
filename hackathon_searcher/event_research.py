"""
Event research module.

For each promising event, visits the official event website to extract:
- Travel support / flight credit information
- Sponsor lists
- Application details
- Event format and logistics

Extracts facts rather than merely storing raw page text.
"""

import json
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from hackathon_searcher.database import get_event_by_id, update_event, log_audit
from hackathon_searcher.models import TravelSupportStatus


USER_AGENT = "HackathonSearcher/0.1 (+https://github.com; personal agent)"

# Pages to search on external event websites
RELEVANT_PAGES = [
    "",  # homepage
    "/faq",
    "/apply",
    "/register",
    "/registration",
    "/about",
    "/travel",
    "/participation",
    "/rules",
    "/eligibility",
    "/logistics",
    "/accommodation",
    "/sponsors",
    "/schedule",
    "/hackathon",
    "/event",
    "/details",
    "/info",
]

# Travel support keywords and their classification
TRAVEL_KEYWORDS_CONFIRMED = [
    (r"flight\s*(credit|reimburs|covered|paid|provided|sponsor)", TravelSupportStatus.CONFIRMED_FLIGHTS),
    (r"travel\s*(reimburs|covered|paid|provided|sponsor|stipend)", TravelSupportStatus.CONFIRMED_TRAVEL_REIMBURSEMENT),
    (r"transport\s*(reimburs|covered|paid|provided)", TravelSupportStatus.CONFIRMED_TRAVEL_REIMBURSEMENT),
    (r"airfare\s*(reimburs|covered|paid)", TravelSupportStatus.CONFIRMED_FLIGHTS),
    (r"travel\s*(grant|scholarship|assist)", TravelSupportStatus.CONFIRMED_TRAVEL_STIPEND),
    (r"accommodation\s*(covered|provided|paid|free|included)", TravelSupportStatus.CONFIRMED_ACCOMMODATION_ONLY),
]

TRAVEL_KEYWORDS_POSSIBLE = [
    r"travel\s*support",
    r"travel\s*(may|might|can)\s*(be|provide)",
    r"reimburs",
    r"stipend",
    r"flight\s*(help|assist)",
    r"financial\s*(assist|support|aid)",
    r"need-based.*(travel|flight)",
]


def _make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=15,
        follow_redirects=True,
    )


def fetch_page(url: str) -> Optional[str]:
    """Fetch a single page. Returns HTML or None."""
    client = _make_client()
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"[research] Failed to fetch {url}: {e}")
        return None
    finally:
        client.close()


def research_event(event_id: str) -> dict:
    """
    Research an event by visiting its external website.

    Returns a dict of extracted information:
    - travel_support, travel_support_type, travel_support_amount, etc.
    - sponsors extracted from the site
    - application_url if found on external site
    """
    event = get_event_by_id(event_id)
    if not event:
        print(f"[research] Event {event_id} not found in database")
        return {}

    event_url = event.get("event_url", "")
    if not event_url:
        print(f"[research] No external URL for event {event_id}, skipping")
        return {}

    log_audit(event_id, "RESEARCH_START", f"Researching {event_url}")

    findings: dict = {}
    all_text = ""

    # Fetch relevant pages
    for suffix in RELEVANT_PAGES:
        page_url = _resolve_page_url(event_url, suffix)
        if not page_url:
            continue

        html = fetch_page(page_url)
        if not html:
            continue

        text = BeautifulSoup(html, "lxml").get_text(separator=" ", strip=True)
        all_text += f"\n--- {page_url} ---\n{text}"

        # Check each page for travel support
        ts = _extract_travel_support(text, page_url)
        if ts and ts.get("status") not in (None, TravelSupportStatus.NO_TRAVEL_INFORMATION, TravelSupportStatus.UNKNOWN):
            # Prefer confirmed over possible
            current_status = findings.get("travel_support_status")
            new_status = ts["status"]
            if _is_better_status(new_status, current_status):
                findings.update(ts)

        # Extract sponsors from each page
        sponsors = _extract_sponsors_from_text(text)
        if sponsors:
            existing_sponsors = findings.get("sponsors", [])
            for s in sponsors:
                if s not in existing_sponsors:
                    existing_sponsors.append(s)
            findings["sponsors"] = existing_sponsors

        # Look for application URLs
        app_url = _find_application_url(html, page_url)
        if app_url and not findings.get("application_url"):
            findings["application_url"] = app_url

        # Look for prizes
        prizes = _extract_prizes(text)
        if prizes and not findings.get("prizes"):
            findings["prizes"] = prizes

    # If no confirmed travel support found in any page, do a broader search of combined text
    if not findings.get("travel_support_status"):
        ts = _extract_travel_support(all_text, event_url)
        if ts:
            findings.update(ts)

    # Store findings
    if findings:
        try:
            update_event(event_id, findings)
            log_audit(event_id, "RESEARCH_COMPLETE", f"Found: {json.dumps({k: v for k, v in findings.items() if k != 'all_text'}, default=str)[:500]}")
        except Exception as e:
            print(f"[research] Failed to update event {event_id}: {e}")

    return findings


def _resolve_page_url(base_url: str, suffix: str) -> Optional[str]:
    """Resolve a page URL relative to the base URL."""
    if not suffix:
        return base_url
    return urljoin(base_url.rstrip("/") + "/", suffix.lstrip("/"))


def _extract_travel_support(text: str, source_url: str) -> dict:
    """Extract travel support information from text."""
    text_lower = text.lower()
    findings: dict = {
        "travel_support_source": source_url,
    }

    # Check confirmed keywords first
    for pattern, status in TRAVEL_KEYWORDS_CONFIRMED:
        matches = re.findall(pattern, text_lower)
        if matches:
            findings["travel_support"] = status.value
            findings["travel_support_type"] = status.value
            findings["travel_support_confidence"] = 0.90

            # Try to extract amounts
            amounts = _extract_travel_amounts(text, matches)
            if amounts:
                findings.update(amounts)

            # Store the matching text as evidence
            findings["travel_support_details"] = json.dumps({
                "source_url": source_url,
                "matching_text": str(matches[:3]),
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            })
            return findings

    # Check possible keywords
    for pattern in TRAVEL_KEYWORDS_POSSIBLE:
        if re.search(pattern, text_lower):
            findings["travel_support"] = TravelSupportStatus.POSSIBLE_TRAVEL_SUPPORT.value
            findings["travel_support_type"] = TravelSupportStatus.POSSIBLE_TRAVEL_SUPPORT.value
            findings["travel_support_confidence"] = 0.50
            findings["travel_support_details"] = json.dumps({
                "source_url": source_url,
                "matching_pattern": pattern,
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            })
            return findings

    findings["travel_support"] = TravelSupportStatus.NO_TRAVEL_INFORMATION.value
    findings["travel_support_confidence"] = 0.10
    return findings


def _is_better_status(new_status, current_status) -> bool:
    """Check if the new travel support status is 'better' than current."""
    if not current_status:
        return True
    priority = {
        TravelSupportStatus.CONFIRMED_FLIGHTS.value: 7,
        TravelSupportStatus.CONFIRMED_TRAVEL_REIMBURSEMENT.value: 6,
        TravelSupportStatus.CONFIRMED_TRAVEL_STIPEND.value: 5,
        TravelSupportStatus.CONFIRMED_ACCOMMODATION_ONLY.value: 4,
        TravelSupportStatus.TRAVEL_SUPPORT_MENTIONED.value: 3,
        TravelSupportStatus.POSSIBLE_TRAVEL_SUPPORT.value: 2,
        TravelSupportStatus.NO_TRAVEL_INFORMATION.value: 1,
        TravelSupportStatus.NO_TRAVEL_SUPPORT.value: 0,
        TravelSupportStatus.UNKNOWN.value: 0,
    }
    return priority.get(new_status, 0) > priority.get(str(current_status), 0)


def _extract_travel_amounts(text: str, _matches) -> dict:
    """Try to extract monetary amounts related to travel."""
    amounts: dict = {}
    # Look for patterns like €250, $500, 300 EUR, etc.
    amount_patterns = [
        r'(?:€|EUR|euro)\s*(\d[\d,]*)',
        r'\$\s*(\d[\d,]*)',
        r'(\d[\d,]*)\s*(?:€|EUR|euro|EUR)',
        r'(\d[\d,]*)\s*(?:\$|USD|dollars)',
        r'(?:up\s*to\s*|max\w*\s*|maximum\s*)(?:€|\$)?\s*(\d[\d,]*)',
    ]
    for pattern in amount_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                amount_str = m.group(1).replace(",", "")
                amounts["travel_support_amount"] = amount_str
                if "€" in m.group(0).lower() or "eur" in m.group(0).lower() or "euro" in m.group(0).lower():
                    amounts["travel_support_currency"] = "EUR"
                elif "$" in m.group(0) or "usd" in m.group(0).lower():
                    amounts["travel_support_currency"] = "USD"
            except ValueError:
                pass
            break
    return amounts


def _extract_sponsors_from_text(text: str) -> list[str]:
    """Extract sponsor names from text near 'sponsor' keywords."""
    soup = BeautifulSoup(text, "html.parser") if "<" in text else None
    if soup:
        text = soup.get_text(separator=" ", strip=True)

    sponsors = []

    # Look for sponsor sections
    sponsor_section_patterns = [
        r'(?:sponsored\s*by|sponsors?|partners?|in\s*partnership\s*with)[:\s]*(.*?)(?:\n\n|\n[A-Z]|$)',
    ]

    for pattern in sponsor_section_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            # Try to extract company names (capitalized phrases)
            names = re.findall(r'([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)', match)
            sponsors.extend(names)

    return list(dict.fromkeys(sponsors))  # Deduplicate while preserving order


def _find_application_url(html: str, base_url: str) -> Optional[str]:
    """Find application/registration URL on a page."""
    soup = BeautifulSoup(html, "lxml")

    # Look for links with application-related text
    apply_patterns = [
        r"apply", r"register", r"sign\s*up", r"join", r"participate",
        r"submit", r"application", r"registration",
    ]

    for link in soup.find_all("a", href=True):
        text = link.get_text(strip=True).lower()
        href = link.get("href", "")
        for pattern in apply_patterns:
            if re.search(pattern, text) or re.search(pattern, href, re.IGNORECASE):
                resolved = urljoin(base_url, href)
                # Prefer external URLs over hackathonhub
                parsed = urlparse(resolved)
                if parsed.netloc:
                    return resolved

    return None


def _extract_prizes(text: str) -> Optional[str]:
    """Extract prize information from text."""
    prize_patterns = [
        r'(?:prizes?|awards?|win)[:\s]*(.*?)(?:\n\n|\n[A-Z]|$)',
        r'(?:prize\s*pool|total\s*prizes)[:\s]*(.*?)(?:\n|$)',
    ]
    for pattern in prize_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0)[:500]
    return None


def research_multiple_events(event_ids: list[str]) -> dict[str, dict]:
    """Research multiple events. Returns {event_id: findings}."""
    results = {}
    for event_id in event_ids:
        try:
            findings = research_event(event_id)
            results[event_id] = findings
        except Exception as e:
            print(f"[research] Error researching event {event_id}: {e}")
            results[event_id] = {"error": str(e)}
    return results
