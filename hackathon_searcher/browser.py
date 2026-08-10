"""
Browser automation module using Playwright.

Handles JavaScript-heavy pages, multi-step forms, conditional fields,
navigation, file uploads, dropdowns, checkboxes, and submission.

Designed as modular tools the agent can call.
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

from hackathon_searcher.models import FormField, FormSnapshot
from hackathon_searcher.settings import settings


class BrowserSession:
    """
    Wraps a Playwright browser session for form interaction.

    Lazily imports Playwright to avoid requiring it for non-browser operations.
    """

    def __init__(self, headless: Optional[bool] = None):
        self._headless = headless if headless is not None else settings.HEADLESS
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ImportError(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            )
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self._page = self._context.new_page()
        self._page.set_default_timeout(settings.BROWSER_TIMEOUT_MS)

    def close(self) -> None:
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    @property
    def page(self):
        if not self._page:
            raise RuntimeError("Browser not started. Use 'with BrowserSession() as browser:'")
        return self._page

    def navigate(self, url: str) -> bool:
        """Navigate to a URL. Returns True if successful."""
        try:
            self.page.goto(url, wait_until="domcontentloaded")
            time.sleep(1)  # Let JS initialize
            return True
        except Exception as e:
            print(f"[browser] Navigation failed for {url}: {e}")
            return False

    def get_page_html(self) -> str:
        """Get the current page HTML."""
        return self.page.content()

    def get_page_text(self) -> str:
        """Get the visible text of the current page."""
        return self.page.inner_text("body")

    def find_form(self) -> bool:
        """Check if the current page has a form."""
        try:
            form = self.page.query_selector("form, [role='form'], .form, #form")
            return form is not None
        except Exception:
            return False

    def fill_field(self, field: FormField) -> bool:
        """
        Fill a single form field using Playwright.

        Handles text, textarea, email, dropdown, radio, checkbox, etc.
        Returns True if successful.
        """
        if not field.answer:
            return True  # Nothing to fill

        try:
            selector = self._build_selector(field)

            if field.field_type in ("text", "textarea", "email", "phone", "url", "date"):
                self.page.fill(selector, field.answer)
                return True

            elif field.field_type == "dropdown":
                self.page.select_option(selector, label=field.answer)
                return True

            elif field.field_type == "radio":
                # Find the radio option matching the answer
                options = self.page.query_selector_all(f"{selector}")
                for opt in options:
                    label = opt.get_attribute("value") or opt.inner_text()
                    if field.answer.lower() in label.lower():
                        opt.click()
                        return True
                # Try clicking the label
                self.page.click(f"label:has-text('{field.answer}')")
                return True

            elif field.field_type == "checkbox":
                checkbox = self.page.query_selector(selector)
                if checkbox:
                    is_checked = checkbox.is_checked()
                    should_check = field.answer.lower() in ("yes", "true", "1")
                    if should_check and not is_checked:
                        checkbox.check()
                    elif not should_check and is_checked:
                        checkbox.uncheck()
                return True

            elif field.field_type == "file":
                file_path = field.answer
                if os.path.exists(file_path):
                    self.page.set_input_files(selector, file_path)
                return True

            elif field.field_type == "multi-select":
                # Handle multi-select
                pass

            return True

        except Exception as e:
            print(f"[browser] Failed to fill field '{field.label}': {e}")
            return False

    def _build_selector(self, field: FormField) -> str:
        """Build a CSS selector for a form field."""
        if field.name:
            # Try name/id selector first
            selector = f"[name='{field.name}'], #{field.name}"
            return selector

        if field.label:
            # Use label text to find the field
            escaped = field.label.replace("'", "\\'")
            return f"*[aria-label='{escaped}']"

        return "input, textarea, select"

    def click_button(self, text: str) -> bool:
        """Click a button by its text content."""
        try:
            self.page.click(f"button:has-text('{text}'), input[type='submit'][value='{text}'], a:has-text('{text}')")
            return True
        except Exception as e:
            print(f"[browser] Failed to click button '{text}': {e}")
            return False

    def wait_for_navigation(self) -> None:
        """Wait for navigation to complete."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

    def get_confirmation_text(self) -> str:
        """Try to extract confirmation text after submission."""
        selectors = [
            ".confirmation", ".success", ".thank-you", '[class*="success"]',
            '[class*="confirmation"]', '[class*="thank"]', "main h1",
            "h1", "h2",
        ]
        for selector in selectors:
            try:
                el = self.page.query_selector(selector)
                if el:
                    return el.inner_text()[:1000]
            except Exception:
                continue
        return ""

    def take_screenshot(self, path: str) -> bool:
        """Take a screenshot of the current page."""
        try:
            self.page.screenshot(path=path, full_page=True)
            return True
        except Exception as e:
            print(f"[browser] Screenshot failed: {e}")
            return False

    def is_captcha_present(self) -> bool:
        """Check if a CAPTCHA is present on the page."""
        captcha_indicators = [
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            ".g-recaptcha",
            ".h-captcha",
            "[class*='captcha']",
            "div[class*='turnstile']",
        ]
        for selector in captcha_indicators:
            if self.page.query_selector(selector):
                return True
        return False


def fill_application_form(
    application_url: str,
    fields: list[FormField],
    dry_run: bool = True,
) -> dict:
    """
    Fill and optionally submit an application form using Playwright.

    In dry_run mode: fills fields but does NOT click submit.
    In live mode: fills fields and clicks submit.

    Returns a result dict with status and details.
    """
    result = {
        "status": "unknown",
        "confirmation_text": "",
        "confirmation_url": "",
        "error": "",
        "captcha_blocked": False,
    }

    screenshot_dir = Path("screenshots")
    screenshot_dir.mkdir(exist_ok=True)

    try:
        with BrowserSession() as browser:
            # Navigate to application
            if not browser.navigate(application_url):
                result["status"] = "BLOCKED_LOGIN"
                result["error"] = "Could not navigate to application URL"
                return result

            # Check for CAPTCHA
            if browser.is_captcha_present():
                result["status"] = "BLOCKED_CAPTCHA"
                result["captcha_blocked"] = True
                result["error"] = "CAPTCHA detected — cannot bypass"
                return result

            # Check for login wall
            page_text = browser.get_page_text().lower()
            login_indicators = ["sign in", "log in", "create account", "please login"]
            if any(ind in page_text for ind in login_indicators):
                # Check if there's also a form, might just be a login-optional page
                if not browser.find_form():
                    result["status"] = "BLOCKED_LOGIN"
                    result["error"] = "Login required — no form found"
                    return result

            # Fill fields
            for field in fields:
                if field.answer and field.answer != "UNKNOWN_REQUIRED_FIELD":
                    browser.fill_field(field)
                    time.sleep(0.3)  # Small delay between fields

            # Take pre-submission screenshot
            event_id_safe = result.get("event_id", "unknown")
            browser.take_screenshot(str(screenshot_dir / f"pre_submit_{event_id_safe}.png"))

            if dry_run:
                result["status"] = "READY_TO_APPLY"
                result["confirmation_text"] = "[DRY RUN — submit not clicked]"
                return result

            # Submit the form
            submit_clicked = False
            for button_text in ["Submit", "Apply", "Send", "Register", "Continue", "Next"]:
                if browser.click_button(button_text):
                    submit_clicked = True
                    break

            if not submit_clicked:
                # Try pressing Enter on the last field
                try:
                    browser.page.keyboard.press("Enter")
                    submit_clicked = True
                except Exception:
                    pass

            if not submit_clicked:
                result["status"] = "APPLYING"
                result["error"] = "Could not find submit button"
                return result

            # Wait for confirmation
            browser.wait_for_navigation()
            time.sleep(2)

            # Check for CAPTCHA after submission attempt
            if browser.is_captcha_present():
                result["status"] = "BLOCKED_CAPTCHA"
                result["captcha_blocked"] = True
                return result

            # Extract confirmation
            confirmation_text = browser.get_confirmation_text()
            result["confirmation_text"] = confirmation_text
            result["confirmation_url"] = browser.page.url

            # Take post-submission screenshot
            browser.take_screenshot(str(screenshot_dir / f"post_submit_{event_id_safe}.png"))

            result["status"] = "APPLIED"
            return result

    except ImportError as e:
        result["status"] = "BLOCKED_LOGIN"
        result["error"] = f"Playwright not available: {e}"
        return result
    except Exception as e:
        result["status"] = "APPLYING"
        result["error"] = str(e)
        return result


def discover_application_form(event_url: str) -> Optional[str]:
    """
    Visit the event website and try to find the application form URL.

    Returns the application form URL if found.
    """
    try:
        with BrowserSession() as browser:
            if not browser.navigate(event_url):
                return None

            html = browser.get_page_html()

            # Parse links looking for application-related URLs
            from bs4 import BeautifulSoup
            from urllib.parse import urljoin

            soup = BeautifulSoup(html, "lxml")
            apply_patterns = [
                r"apply", r"register", r"sign.?up", r"join", r"participate",
                r"submit", r"application", r"registration",
            ]

            import re
            for link in soup.find_all("a", href=True):
                text = link.get_text(strip=True).lower()
                href = link.get("href", "")
                for pattern in apply_patterns:
                    if re.search(pattern, text) or re.search(pattern, href, re.IGNORECASE):
                        resolved = urljoin(event_url, href)
                        return resolved

            return None
    except Exception as e:
        print(f"[browser] Failed to discover application form: {e}")
        return None
