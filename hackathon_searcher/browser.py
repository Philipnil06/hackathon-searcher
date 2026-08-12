"""
Browser automation module — multi-applicant edition.

Separate browser contexts for each applicant (important for OAuth, sessions, cookies).
Supports provider authentication flows for separately configured applicant identities.
Handles form filling, CAPTCHA detection, and submission.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from urllib.parse import urljoin

from hackathon_searcher.models import FormField
from hackathon_searcher.profile import ApplicantProfile
from hackathon_searcher.settings import settings


class BrowserSession:
    """Wraps a Playwright browser session for one applicant."""

    def __init__(self, applicant_id: str = "", headless: Optional[bool] = None, persistent: bool = False, channel: Optional[str] = None):
        self.applicant_id = applicant_id
        self._headless = headless if headless is not None else settings.HEADLESS
        self._persistent = persistent and bool(applicant_id)
        self._channel = channel
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
            raise ImportError("Playwright not installed. Run: pip install playwright && playwright install chromium")

        self._playwright = sync_playwright().start()
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        if self._persistent:
            profile_path = Path(settings.BROWSER_PROFILES_DIR) / self.applicant_id
            profile_path.mkdir(parents=True, exist_ok=True)
            self._browser = None
            launch_options = {
                "user_data_dir": str(profile_path),
                "headless": self._headless,
                "user_agent": user_agent,
            }
            if self._channel:
                launch_options["channel"] = self._channel
            self._context = self._playwright.chromium.launch_persistent_context(**launch_options)
        else:
            launch_options = {"headless": self._headless}
            if self._channel:
                launch_options["channel"] = self._channel
            self._browser = self._playwright.chromium.launch(**launch_options)
            self._context = self._browser.new_context(user_agent=user_agent)
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
        try:
            self.page.goto(url, wait_until="domcontentloaded")
            time.sleep(1)
            return True
        except Exception as e:
            print(f"[browser] Navigation failed for {url}: {e}")
            return False

    def get_page_html(self) -> str:
        return self.page.content()

    def get_page_text(self) -> str:
        return self.page.inner_text("body")

    def find_form(self) -> bool:
        try:
            return self.page.query_selector("form, [role='form'], .form, #form") is not None
        except Exception:
            return False

    def fill_field(self, field: FormField) -> bool:
        if not field.answer:
            return True
        try:
            selector = self._build_selector(field)
            if field.field_type in ("text", "textarea", "email", "phone", "url", "date"):
                self.page.fill(selector, field.answer)
                return True
            elif field.field_type == "dropdown":
                self.page.select_option(selector, label=field.answer)
                return True
            elif field.field_type == "radio":
                try:
                    self.page.click(f"label:has-text('{field.answer}')")
                except Exception:
                    options = self.page.query_selector_all(selector)
                    for opt in options:
                        label = opt.get_attribute("value") or opt.inner_text()
                        if field.answer.lower() in label.lower():
                            opt.click()
                            return True
                return True
            elif field.field_type == "checkbox":
                checkbox = self.page.query_selector(selector)
                if checkbox:
                    should_check = field.answer.lower() in ("yes", "true", "1")
                    if should_check and not checkbox.is_checked():
                        checkbox.check()
                    elif not should_check and checkbox.is_checked():
                        checkbox.uncheck()
                return True
            elif field.field_type == "file":
                if os.path.exists(field.answer):
                    self.page.set_input_files(selector, field.answer)
                return True
            return True
        except Exception as e:
            # Browser error messages can include DOM text outside the active
            # Windows console code page.  Logging must never turn a normal
            # field-level failure into a fatal validation failure.
            message = f"[browser] Failed to fill field '{field.label}': {e}"
            print(message.encode("ascii", errors="backslashreplace").decode("ascii"))
            return False

    def _build_selector(self, field: FormField) -> str:
        if field.selector:
            return field.selector
        if field.name:
            return f"[name='{field.name}'], #{field.name}"
        if field.label:
            escaped = field.label.replace("'", "\\'")
            return f"*[aria-label='{escaped}']"
        return "input, textarea, select"

    def get_rendered_form_fields(self) -> list[FormField]:
        """Extract visible labels by DOM proximity for JS-heavy forms such as Luma."""
        rows = self.page.evaluate("""() => {
          const controls=[...document.querySelectorAll('input:not([type=hidden]),textarea,select')]
            .filter(e=>!e.getAttribute('aria-hidden'));
          const texts=[...document.querySelectorAll('label,legend,p,span,div')].map(e=>{
            const r=e.getBoundingClientRect(), t=(e.innerText||'').trim();
            return {t,x:r.x,y:r.y,b:r.bottom,w:r.width};
          }).filter(x=>x.t && x.t.length<450 && x.w>0 && x.b>0);
          return controls.map((e,i)=>{
            const r=e.getBoundingClientRect();
            const candidates=texts.filter(t=>t.b<=r.y+10 && r.y-t.b<180 && Math.abs(t.x-r.x)<700)
              .sort((a,b)=>(r.y-a.b)-(r.y-b.b));
            const nearby=candidates[0]?.t||'';
            const id=e.id, name=e.name;
            return {label: nearby, name, id, type:e.type||e.tagName.toLowerCase(), required:e.required||e.getAttribute('aria-required')==='true', placeholder:e.placeholder||'', selector:id?`#${CSS.escape(id)}`:(name?`[name="${CSS.escape(name)}"]`:''), order:i};
          });
        }""")
        result=[]
        for row in rows:
            ftype={"email":"email","textarea":"textarea","checkbox":"checkbox","radio":"radio","tel":"phone"}.get(row["type"],"dropdown" if row["placeholder"]=="Select an option" else "text")
            result.append(FormField(label=row["label"][:300], name=row["name"], field_type=ftype, required=bool(row["required"]), placeholder=row["placeholder"], order=row["order"], selector=row["selector"], description=row["label"][:500]))
        return result

    def click_button(self, text: str) -> bool:
        try:
            self.page.click(f"button:has-text('{text}'), input[type='submit'][value='{text}'], a:has-text('{text}')")
            return True
        except Exception as e:
            print(f"[browser] Failed to click '{text}': {e}")
            return False

    def discovery_actions(self) -> list[dict]:
        """Return rendered, non-submit navigation actions for discovery."""
        actions = []
        try:
            for element in self.page.query_selector_all("a, button, [role='button']"):
                text = " ".join(filter(None, [
                    element.inner_text(), element.get_attribute("aria-label"),
                    element.get_attribute("value"),
                ])).strip()
                href = element.get_attribute("href") or ""
                if not text and not href:
                    continue
                lowered = text.lower()
                if any(word in lowered for word in ("submit application", "submit", "send application", "pay now")):
                    continue
                actions.append({"text": text, "href": href, "element": element})
        except Exception:
            return []
        return actions

    def click_discovery_action(self, action: dict) -> bool:
        """Click a rendered discovery action; never click a final submit control."""
        try:
            action["element"].click(timeout=10000)
            self.wait_for_navigation()
            return True
        except Exception:
            return False

    def wait_for_navigation(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

    def get_confirmation_text(self) -> str:
        selectors = [
            ".confirmation", ".success", ".thank-you", '[class*="success"]',
            '[class*="confirmation"]', '[class*="thank"]', "main h1", "h1", "h2",
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
        try:
            self.page.screenshot(path=path, full_page=True)
            return True
        except Exception as e:
            print(f"[browser] Screenshot failed: {e}")
            return False

    def is_captcha_present(self) -> bool:
        captcha_selectors = [
            "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
            ".g-recaptcha", ".h-captcha", "[class*='captcha']", "div[class*='turnstile']",
        ]
        for selector in captcha_selectors:
            for element in self.page.query_selector_all(selector):
                try:
                    if element.is_visible():
                        return True
                except Exception:
                    continue
        return False

    def is_google_oauth_required(self) -> bool:
        """Detect if the page requires Google OAuth."""
        try:
            oauth_indicators = [
                "Sign in with Google", "Continue with Google",
                "google.com/accounts", "accounts.google.com",
            ]
            page_text = self.get_page_text()
            page_url = self.page.url
            for indicator in oauth_indicators:
                if indicator.lower() in page_text.lower() or indicator in page_url:
                    return True
            return False
        except Exception:
            return False

    def set_email_field(self, email: str) -> bool:
        """Try to fill the email field on a Google OAuth page."""
        try:
            self.page.fill("input[type='email']", email)
            self.page.click("button:has-text('Next'), #identifierNext, button[jsname]")
            return True
        except Exception as e:
            print(f"[browser] Google email fill failed: {e}")
            return False


def fill_application_form(
    application_url: str,
    fields: list[FormField],
    profile: ApplicantProfile,
    dry_run: bool = True,
) -> dict:
    """
    Fill and optionally submit an application form for a specific applicant.

    Uses applicant-specific browser context.
    """
    applicant_id = profile.applicant_id
    result = {
        "status": "unknown", "confirmation_text": "", "confirmation_url": "",
        "error": "", "captcha_blocked": False, "applicant_id": applicant_id,
    }

    screenshot_dir = Path("screenshots") / applicant_id
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Public application flows start from a clean session. Authentication is
        # only handled separately when a site demonstrably requires it.
        with BrowserSession() as browser:
            if not browser.navigate(application_url):
                result["status"] = "BLOCKED_LOGIN"
                result["error"] = "Could not navigate to application URL"
                return result

            # Check for Google OAuth
            if browser.is_google_oauth_required():
                oauth_email = profile.google_oauth_email or profile.email
                print(f"[browser] Google OAuth detected for {applicant_id}, using {oauth_email}")
                # We don't automatically handle OAuth passwords — mark as needing manual auth
                result["status"] = "BLOCKED_LOGIN"
                result["error"] = "Google OAuth required — needs authenticated session"
                return result

            if browser.is_captcha_present():
                result["status"] = "BLOCKED_CAPTCHA"
                result["captcha_blocked"] = True
                result["error"] = "CAPTCHA detected"
                return result

            page_text = browser.get_page_text().lower()
            login_indicators = ["sign in", "log in", "create account", "please login"]
            is_luma = "luma.com" in browser.page.url.lower()
            if any(ind in page_text for ind in login_indicators) and not is_luma:
                if not browser.find_form():
                    result["status"] = "BLOCKED_LOGIN"
                    result["error"] = "Login required"
                    return result

            # Fill fields
            for field in fields:
                if field.answer and field.answer != "UNKNOWN_REQUIRED_FIELD":
                    browser.fill_field(field)
                    time.sleep(0.3)

            browser.take_screenshot(str(screenshot_dir / f"pre_submit.png"))

            if dry_run:
                result["status"] = "DRY_RUN_COMPLETE"
                result["confirmation_text"] = "[DRY RUN — submit not clicked]"
                return result

            # Submit
            submit_clicked = False
            for btn in ["Submit", "Apply", "Send", "Register", "Continue", "Next"]:
                if browser.click_button(btn):
                    submit_clicked = True
                    break
            if not submit_clicked:
                try:
                    browser.page.keyboard.press("Enter")
                    submit_clicked = True
                except Exception:
                    pass
            if not submit_clicked:
                result["status"] = "APPLYING"
                result["error"] = "Could not find submit button"
                return result

            browser.wait_for_navigation()
            time.sleep(2)

            if browser.is_captcha_present():
                result["status"] = "BLOCKED_CAPTCHA"
                result["captcha_blocked"] = True
                return result

            result["confirmation_text"] = browser.get_confirmation_text()
            result["confirmation_url"] = browser.page.url
            browser.take_screenshot(str(screenshot_dir / f"post_submit.png"))
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


def discover_application_form(event_url: str, applicant_id: str = "") -> Optional[str]:
    """Visit the event website to find the application form URL."""
    try:
        with BrowserSession(applicant_id=applicant_id) as browser:
            if not browser.navigate(event_url):
                return None
            html = browser.get_page_html()
            soup = BeautifulSoup(html, "lxml")
            apply_patterns = [
                r"apply", r"register", r"sign.?up", r"join", r"participate",
                r"submit", r"application", r"registration",
            ]
            for link in soup.find_all("a", href=True):
                text = link.get_text(strip=True).lower()
                href = link.get("href", "")
                for pattern in apply_patterns:
                    if re.search(pattern, text) or re.search(pattern, href, re.IGNORECASE):
                        return urljoin(event_url, href)
            return None
    except Exception as e:
        print(f"[browser] Form discovery failed: {e}")
        return None
