"""
LLM module for Hackathon Searcher.

Uses a configurable LLM provider for all LLM tasks:
- Event-page interpretation
- Travel-support extraction
- Sponsor interpretation
- Event-theme classification
- Eligibility interpretation
- Semantic matching of application questions
- Personalized application-answer generation
- Application-quality review
- Hallucination / factual consistency review
- Travel-support probability estimation
- Sponsor-quality estimation

With exponential backoff retry and structured output support.
"""

import json
import time
from typing import Optional, Type, TypeVar

import httpx

from pydantic import BaseModel

from hackathon_searcher.settings import settings

T = TypeVar("T", bound=BaseModel)
SUPPORTED_PROVIDERS = ("openai", "anthropic", "gemini", "mistral", "openai_compatible")

# Retry configuration
MAX_RETRIES = 3
BASE_DELAY = 1.0  # seconds
MAX_DELAY = 30.0  # seconds


def _provider_key() -> str:
    """Resolve the configured provider's key without exposing it in output."""
    import os
    provider = settings.LLM_PROVIDER
    names = {
        "openai": ("LLM_API_KEY", "OPENAI_API_KEY"),
        "anthropic": ("LLM_API_KEY", "ANTHROPIC_API_KEY"),
        "gemini": ("LLM_API_KEY", "GEMINI_API_KEY"),
        "mistral": ("LLM_API_KEY", "MISTRAL_API_KEY"),
        "openai_compatible": ("LLM_API_KEY",),
    }
    for name in names.get(provider, ("LLM_API_KEY",)):
        if value := os.getenv(name, ""):
            return value
    raise ValueError(f"No API key configured for LLM_PROVIDER={provider}")


def llm_configuration_status() -> dict[str, str | bool]:
    """Check local LLM configuration without printing or calling with the key."""
    provider = settings.LLM_PROVIDER
    if provider not in SUPPORTED_PROVIDERS:
        return {"ready": False, "provider": provider, "model": settings.LLM_MODEL,
                "message": "Unsupported LLM provider. Run `python -m hackathon_searcher.cli setup` and choose a supported provider."}
    if not settings.LLM_MODEL.strip():
        return {"ready": False, "provider": provider, "model": "",
                "message": "LLM model is missing. Run `python -m hackathon_searcher.cli setup` to add it."}
    if provider == "openai_compatible" and not settings.LLM_BASE_URL.strip():
        return {"ready": False, "provider": provider, "model": settings.LLM_MODEL,
                "message": "LLM base URL is required for openai_compatible. Run setup and provide it."}
    try:
        _provider_key()
    except ValueError:
        return {"ready": False, "provider": provider, "model": settings.LLM_MODEL,
                "message": "LLM API key is missing. Run `python -m hackathon_searcher.cli setup`; the key stays in local .env."}
    return {"ready": True, "provider": provider, "model": settings.LLM_MODEL,
            "message": "LLM configuration is present. No API request was made."}


def _chat_completion(prompt: str, system_prompt: str, model: str, temperature: float, max_tokens: int) -> str:
    """Provider adapter returning plain text from a common request shape."""
    provider, key = settings.LLM_PROVIDER, _provider_key()
    if provider in {"openai", "mistral", "openai_compatible"}:
        base_url = settings.LLM_BASE_URL.rstrip("/")
        if not base_url:
            base_url = {"openai": "https://api.openai.com/v1", "mistral": "https://api.mistral.ai/v1"}.get(provider, "")
        if not base_url:
            raise ValueError("LLM_BASE_URL is required for openai_compatible")
        response = httpx.post(f"{base_url}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
            "model": model, "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [{"role": "user", "content": prompt}],
            "temperature": temperature, "max_tokens": max_tokens,
        }, timeout=60)
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"])
    if provider == "anthropic":
        response = httpx.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, json={
            "model": model, "max_tokens": max_tokens, "system": system_prompt, "messages": [{"role": "user", "content": prompt}],
        }, timeout=60)
        response.raise_for_status()
        return "".join(block.get("text", "") for block in response.json().get("content", []) if block.get("type") == "text")
    if provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if system_prompt:
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        response = httpx.post(url, json=body, timeout=60)
        response.raise_for_status()
        return str(response.json()["candidates"][0]["content"]["parts"][0]["text"])
    raise ValueError(f"Unsupported LLM provider: {provider}")


def _retry_with_backoff(fn, max_retries: int = MAX_RETRIES):
    """Execute a function with exponential backoff retry. Skips retry on 400 errors."""
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_exception = e
            # Don't retry on 400-level errors (bad request, won't fix itself)
            if "400" in str(e) or "401" in str(e) or "403" in str(e) or "No API key configured" in str(e):
                print(f"[llm] Non-retryable error: {e}")
                raise e
            if attempt < max_retries:
                delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                print(f"[llm] Retry {attempt + 1}/{max_retries} after {delay:.1f}s: {e}")
                time.sleep(delay)
            else:
                print(f"[llm] All retries exhausted: {e}")
    raise last_exception


def complete(
    prompt: str,
    system_prompt: str = "",
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2000,
) -> Optional[str]:
    """
    Simple text completion. Returns the response text or None on failure.
    """
    model = model or settings.LLM_MODEL

    try:
        def _call():
            return _chat_completion(prompt, system_prompt, model, temperature, max_tokens)

        return _retry_with_backoff(_call)
    except Exception as e:
        print(f"[llm] Completion failed: {e}")
        return None


def complete_structured(
    prompt: str,
    output_schema: Type[T],
    system_prompt: str = "",
    model: Optional[str] = None,
    temperature: float = 0.3,
) -> Optional[T]:
    """
    Structured completion using Pydantic model as output schema.
    Requests JSON and parses it. Falls back gracefully.
    """
    model = model or settings.LLM_MODEL
    json_schema = output_schema.model_json_schema()

    full_prompt = prompt + "\n\nRespond ONLY with valid JSON matching this schema. No markdown, no explanation:\n" + json.dumps(json_schema, indent=2)

    try:
        def _call():
            result_text = _chat_completion(full_prompt, system_prompt, model, temperature, 4000)
            if result_text:
                if result_text.startswith("```"):
                    lines = result_text.split("\n")
                    result_text = "\n".join(lines[1:-1])
                return output_schema.model_validate_json(result_text)
            return None

        return _retry_with_backoff(_call)
    except Exception as e:
        print(f"[llm] Structured completion failed: {e}")
        return None


def analyze_event_page(html_text: str, event_name: str = "") -> Optional[dict]:
    """
    Analyze an event's external website page(s) to extract structured information.

    Returns a dict with travel_support, sponsors, themes, eligibility info, etc.
    """
    from hackathon_searcher.models import EventPageAnalysis

    prompt = f"""Analyze this hackathon event page content and extract structured information.

Event name (if known): {event_name}

Page content:
{html_text[:15000]}

Extract:
- travel_support_status: CONFIRMED_FLIGHTS, CONFIRMED_TRAVEL_REIMBURSEMENT, CONFIRMED_TRAVEL_STIPEND, CONFIRMED_ACCOMMODATION_ONLY, TRAVEL_SUPPORT_MENTIONED, POSSIBLE_TRAVEL_SUPPORT, NO_TRAVEL_INFORMATION, NO_TRAVEL_SUPPORT
- travel_support_details: amount, currency, requirements, any conditions
- sponsors: list of company names that are sponsoring or involved
- themes: list of technical themes (AI, robotics, hardware, fintech, etc.)
- is_physical: true if physical/in-person event
- application_deadline: if found
- eligibility_requirements: any age, student, location, nationality requirements
- prizes_description: what winners receive

Be precise. Use NO_TRAVEL_INFORMATION if nothing about travel is mentioned at all.
Do NOT infer travel support just because well-known sponsors are present."""

    system = "You are an AI that extracts structured information from hackathon event pages. Be precise and conservative. Only report what is explicitly stated."

    result = complete_structured(prompt, EventPageAnalysis, system_prompt=system)
    return result.model_dump() if result else None


def generate_application_answer(
    question: str,
    question_type: str,
    applicant_profile: dict,
    event_context: dict,
    answer_library: list[dict],
    word_limit: Optional[int] = None,
) -> str:
    """
    Generate a personalized application answer for a specific applicant and event.

    Uses verified profile facts only. Tailored to both the applicant and the event.
    """
    profile_json = json.dumps(applicant_profile, indent=2)
    event_json = json.dumps(event_context, indent=2)
    library_json = json.dumps(answer_library, indent=2)

    word_guidance = ""
    if word_limit:
        word_guidance = f"\nKeep your answer under {word_limit} words."

    prompt = f"""Write an application answer for a hackathon application.

APPLICANT PROFILE (verified facts only):
{profile_json}

HACKATHON CONTEXT:
{event_json}

RELEVANT STORED ANSWERS (for reference):
{library_json}

QUESTION: {question}
QUESTION TYPE: {question_type}{word_guidance}

RULES:
1. Only use facts that are EXPLICITLY in the applicant profile above.
2. Never invent education, employment, awards, projects, or skills.
3. Tailor the answer to THIS specific hackathon - mention relevant themes, sponsors, or focus areas from the event context.
4. Write like an ambitious young builder, not corporate AI. Use specific stories, numbers, concrete projects. Short paragraphs. Direct language.
5. Avoid: "I'm deeply passionate about...", "at the intersection of...", em dashes, generic enthusiasm, repeating event marketing copy.
6. For short text fields: 1-2 concise sentences.
7. For motivation questions: approximately 80-180 words.
8. If the question asks for something not in the profile, say so rather than inventing.

Answer:"""

    system = "You are an AI that writes personalized hackathon applications. You only use verified facts from the provided profile. You write like a smart, ambitious young builder - specific, direct, genuine."

    result = complete(prompt, system_prompt=system, temperature=0.8, max_tokens=1000)
    return result.strip() if result else ""


def review_application_quality(
    answers: list[dict],
    applicant_profile: dict,
    event_context: dict,
) -> dict:
    """
    Review application quality before submission.

    Returns: {passes: bool, issues: list[str], suggestions: list[str]}
    """
    answers_json = json.dumps(answers, indent=2)
    profile_json = json.dumps(applicant_profile, indent=2)
    event_json = json.dumps(event_context, indent=2)

    prompt = f"""Review this hackathon application for quality and factual consistency.

APPLICANT PROFILE (verified facts only):
{profile_json}

HACKATHON CONTEXT:
{event_json}

APPLICATION ANSWERS:
{answers_json}

Check:
1. Is every factual claim supported by the profile? List any that aren't.
2. Are all required fields completed? List any missing.
3. Are answers tailored to this specific event?
4. Are answers internally consistent?
5. Are there any spelling mistakes?
6. Is there anything that sounds like AI-generated corporate speak?
7. Are any claims about travel support accurate (not overstating what's offered)?
8. Is this a strong application that stands a good chance of acceptance?

Respond with JSON:
{{
  "passes": true/false,
  "issues": ["list of problems found"],
  "suggestions": ["list of improvements"],
  "overall_quality": "poor/fair/good/strong/exceptional"
}}"""

    system = "You are a critical application reviewer. Be honest and specific about problems."
    result = complete(prompt, system_prompt=system, temperature=0.3, max_tokens=800)

    if result:
        try:
            import re
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
        except (json.JSONDecodeError, AttributeError):
            pass

    return {"passes": True, "issues": [], "suggestions": [], "overall_quality": "unreviewed"}


def estimate_travel_support_probability(
    event_info: dict,
    page_texts: list[str],
) -> dict:
    """
    Estimate travel support probability when not explicitly confirmed.

    Returns: {probability: 0.0-1.0, reasoning: str, signals: list[str]}
    """
    event_json = json.dumps(event_info, indent=2)
    texts = "\n\n---\n\n".join(page_texts[:3])[:8000]

    prompt = f"""Estimate the probability that this hackathon offers travel support (flight reimbursement, travel stipend, etc.) based on contextual signals.

EVENT INFO:
{event_json}

PAGE CONTENT:
{texts}

Look for signals like:
- Previous editions covering flights
- International participant focus
- Strong sponsor backing
- Organizer history
- Accommodation provided
- Selective/invite-only format
- Small cohort with personalized support
- Language about "international hackers" or "global participants"
- Organizer asking about travel needs

Respond with JSON:
{{
  "probability": 0.0,
  "reasoning": "explanation",
  "signals": ["list of positive signals found"],
  "recommendation": "ASK_ORGANIZER / LIKELY / POSSIBLE / UNLIKELY"
}}"""

    system = "You estimate travel support probability for hackathons. Be conservative - do not inflate probabilities."
    result = complete(prompt, system_prompt=system, temperature=0.2, max_tokens=500)

    if result:
        try:
            import re
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
        except (json.JSONDecodeError, AttributeError):
            pass

    return {"probability": 0.0, "reasoning": "Could not estimate", "signals": [], "recommendation": "UNKNOWN"}


def interpret_eligibility(
    eligibility_text: str,
    applicant_profile: dict,
) -> dict:
    """
    Interpret eligibility rules for a specific applicant.

    Returns: {eligible: bool, reasoning: str, concerns: list[str]}
    """
    profile_json = json.dumps(applicant_profile, indent=2)

    prompt = f"""Determine if this applicant is eligible for a hackathon based on the stated rules.

ELIGIBILITY RULES:
{eligibility_text[:3000]}

APPLICANT PROFILE:
{profile_json}

IMPORTANT DISTINCTIONS:
- "University student" does NOT include high school / upper-secondary students.
- "College student" in US context means university, not high school.
- "Upper-secondary" (gymnasium) is NOT the same as university.
- If the rules say "student" without qualification, it could include any student.
- If rules say "18+" check age carefully.
- "Current student" means enrolled NOW, not graduated.

Respond with JSON:
{{
  "eligibility": "ELIGIBLE | INELIGIBLE | UNCERTAIN",
  "reason": "brief evidence-based reason",
  "requirements": ["exact requirements found in the rules"],
  "source_evidence": "short quote or precise paraphrase from the official rules",
  "confidence": 0.0,
  "eligible": true/false,
  "likely_eligible": true/false,
  "reasoning": "detailed explanation",
  "concerns": ["any eligibility concerns"],
  "confidence": 0.0
}}"""

    system = "You interpret hackathon eligibility rules precisely. You distinguish carefully between university, college, high school, and upper-secondary. When ambiguous, you note the ambiguity."
    result = complete(prompt, system_prompt=system, temperature=0.1, max_tokens=600)

    if result:
        try:
            import re
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
        except (json.JSONDecodeError, AttributeError):
            pass

    return {
        "eligibility": "UNCERTAIN",
        "eligible": False,
        "likely_eligible": False,
        "reason": "Official eligibility rules could not be interpreted",
        "reasoning": "Official eligibility rules could not be interpreted",
        "requirements": [],
        "source_evidence": "",
        "concerns": [],
        "confidence": 0.0,
    }


def classify_sponsor_quality(sponsor_name: str) -> dict:
    """
    Estimate sponsor quality based on name recognition and context.

    Returns: {quality_score: 0-20, category: str, reasoning: str}
    """
    prompt = f"""Rate this company's quality as a hackathon sponsor on a 0-20 scale.

Company: {sponsor_name}

Consider:
- Company reputation in tech/startup ecosystem
- Whether they're a major AI company, top dev-tool company, YC company, well-funded startup, VC firm, cloud provider, hardware company, etc.
- Whether they're likely to send engineers, provide mentorship, or just API credits

Respond with JSON:
{{
  "quality_score": 0,
  "category": "top_ai / major_tech / strong_startup / credible / weak / unknown",
  "reasoning": "brief explanation",
  "likely_involvement": "hosting / judging / mentoring / prizes / api_credits_only / unclear"
}}"""

    system = "You evaluate company quality for hackathon sponsorships. Be calibrated: only top-tier companies get 17+."
    result = complete(prompt, system_prompt=system, temperature=0.2, max_tokens=300)

    if result:
        try:
            import re
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
        except (json.JSONDecodeError, AttributeError):
            pass

    return {"quality_score": 5, "category": "unknown", "reasoning": "Could not classify", "likely_involvement": "unclear"}


def classify_themes(description: str, event_name: str = "") -> list[str]:
    """Classify event themes from description text."""
    prompt = f"""Extract the main technical and focus themes from this hackathon.

Event name: {event_name}
Description: {description[:2000]}

Return themes from this list only:
AI, AI agents, robotics, hardware, drones, autonomy, computer vision, developer tools, infrastructure, fintech, payments, cybersecurity, startups, edtech, consumer, mobile, web, data, gaming, health, climate, sustainability, blockchain, crypto, open source, defense, space, biotech

Return JSON: {{"themes": ["theme1", "theme2"]}}"""

    system = "You extract themes from hackathon descriptions. Be specific and accurate."
    result = complete(prompt, system_prompt=system, temperature=0.1, max_tokens=200)

    if result:
        try:
            import re
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                return data.get("themes", [])
        except (json.JSONDecodeError, AttributeError):
            pass

    return []
