import json
import os
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

SESSION_FILE = Path(__file__).parent.parent / "session.json"


def _save_cookies(context):
    cookies = context.cookies()
    SESSION_FILE.write_text(json.dumps(cookies))


def _load_cookies(context):
    if SESSION_FILE.exists():
        cookies = json.loads(SESSION_FILE.read_text())
        context.add_cookies(cookies)
        return True
    return False


def _text(el):
    if el is None:
        return ""
    return el.inner_text().strip()


def _new_browser(p):
    return p.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--start-maximized"],
    )


def _new_context(browser):
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context


def _scroll(page, times=6):
    for _ in range(times):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        time.sleep(random.uniform(0.6, 1.0))


def _section_text(page, heading: str) -> str:
    """Return the text content of a named section (e.g. 'About', 'Experience')."""
    anchor_id = heading.lower()
    anchor = page.query_selector(f"#{anchor_id}")
    if anchor:
        see_more = page.query_selector(
            f"#{anchor_id} ~ div button.inline-show-more-text__button, "
            f"#{anchor_id} ~ div button[aria-label*='more']"
        )
        if see_more:
            try:
                see_more.click()
                time.sleep(0.5)
            except Exception:
                pass
        for sel in [
            f"#{anchor_id} ~ div span[aria-hidden='true']",
            f"#{anchor_id} ~ div .pv-shared-text-with-see-more span[aria-hidden='true']",
        ]:
            el = page.query_selector(sel)
            if el:
                t = _text(el)
                if t and len(t) > 10:
                    return t

    sections = page.query_selector_all("section")
    for section in sections:
        h2 = section.query_selector("h2")
        if h2 and heading.lower() in _text(h2).lower():
            for btn_sel in [
                "button.inline-show-more-text__button",
                "button[aria-label*='more']",
                "button",
            ]:
                see_more = section.query_selector(btn_sel)
                if see_more:
                    btn_text = _text(see_more).lower()
                    if "more" in btn_text or "see" in btn_text:
                        try:
                            see_more.click()
                            time.sleep(0.5)
                        except Exception:
                            pass
                        break
            t = _text(section)
            lines = [
                ln for ln in t.splitlines()
                if ln.strip()
                and ln.strip().lower() != heading.lower()
                and ln.strip() not in {"…", "… more", "...more", "show more"}
            ]
            return "\n".join(lines).strip()
    return ""


def _login(page, context):
    """Log in to LinkedIn using cached cookies or credentials."""
    email = os.getenv("LINKEDIN_EMAIL")
    password = os.getenv("LINKEDIN_PASSWORD")

    if not email or not password:
        raise ValueError("LINKEDIN_EMAIL and LINKEDIN_PASSWORD must be set in .env")

    logged_in = False
    if _load_cookies(context):
        try:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            if "feed" in page.url or "mynetwork" in page.url or "linkedin.com" in page.url:
                logged_in = True
            else:
                print("[scraper] cached session invalid, re-logging in")
                SESSION_FILE.unlink(missing_ok=True)
        except Exception as e:
            print(f"[scraper] feed check failed ({e}), will re-login")
            SESSION_FILE.unlink(missing_ok=True)

    if not logged_in:
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=20000)
        time.sleep(random.uniform(1.5, 2.5))
        page.fill("#username", email)
        time.sleep(random.uniform(0.5, 1.0))
        page.fill("#password", password)
        time.sleep(random.uniform(0.5, 1.0))
        page.click('button[type="submit"]')
        time.sleep(5)

        current = page.url
        print(f"[scraper] after login url={current}")

        if "checkpoint" in current or "challenge" in current or "verify" in current:
            raise ValueError(
                "LinkedIn requires email/phone verification. "
                "Please complete it in the browser window, then try again."
            )
        if "login" in current:
            raise ValueError("Login failed — check your email and password in .env")

        _save_cookies(context)


def connect_profile(url: str, note: str = "") -> str:
    """Send a connection request to a LinkedIn profile. Returns a status message."""
    if not url.startswith("http"):
        url = "https://" + url
    url = url.rstrip("/") + "/"

    with sync_playwright() as p:
        browser = _new_browser(p)
        context = _new_context(browser)
        page = context.new_page()

        try:
            _login(page, context)

            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(2, 3))

            if "authwall" in page.url or "linkedin.com/login" in page.url:
                SESSION_FILE.unlink(missing_ok=True)
                raise ValueError("Session expired — please try again.")

            # ── Find the Connect button ────────────────────────────────────────
            connect_btn = None

            for sel in [
                "button[aria-label*='Connect']",
                "button:has-text('Connect')",
            ]:
                btn = page.query_selector(sel)
                if btn and "connect" in _text(btn).lower():
                    connect_btn = btn
                    break

            # If not found, open the "More" dropdown
            if not connect_btn:
                more_btn = page.query_selector("button[aria-label='More actions']")
                if not more_btn:
                    more_btn = page.query_selector("button:has-text('More')")
                if more_btn:
                    more_btn.click()
                    time.sleep(1)
                    for sel in [
                        "div[aria-label*='Connect']",
                        "span:has-text('Connect')",
                    ]:
                        btn = page.query_selector(sel)
                        if btn:
                            connect_btn = btn
                            break

            if not connect_btn:
                for indicator in ["Message", "Pending", "Following"]:
                    el = page.query_selector(f"button:has-text('{indicator}')")
                    if el:
                        return f"Already connected or request pending ('{indicator}' button found)."
                raise ValueError("Could not find the Connect button. The profile may already be a connection, or LinkedIn's layout has changed.")

            connect_btn.click()
            time.sleep(random.uniform(1, 2))

            # ── Handle the connection modal ────────────────────────────────────
            modal = page.query_selector("div[role='dialog']")
            if modal:
                if note and note.strip():
                    add_note_btn = page.query_selector("button[aria-label='Add a note']")
                    if not add_note_btn:
                        add_note_btn = page.query_selector("button:has-text('Add a note')")
                    if add_note_btn:
                        add_note_btn.click()
                        time.sleep(0.8)
                        textarea = page.query_selector("textarea[name='message']")
                        if textarea:
                            textarea.fill(note.strip()[:300])
                            time.sleep(0.5)

                for send_sel in [
                    "button[aria-label='Send now']",
                    "button[aria-label='Send invitation']",
                    "button:has-text('Send')",
                    "button:has-text('Done')",
                ]:
                    send_btn = page.query_selector(send_sel)
                    if send_btn:
                        send_btn.click()
                        time.sleep(1.5)
                        break

            print("[scraper] connection request sent")
            return "Connection request sent successfully!"

        finally:
            browser.close()


def scrape_profile(url: str) -> dict:
    """Scrape a LinkedIn profile and return structured data."""
    if not url.startswith("http"):
        url = "https://" + url
    url = url.rstrip("/") + "/"

    with sync_playwright() as p:
        browser = _new_browser(p)
        context = _new_context(browser)
        page = context.new_page()

        try:
            _login(page, context)

            # ── Main profile page ──────────────────────────────────────────────
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(2, 3))

            if "authwall" in page.url or ("login" in page.url and "linkedin.com/login" in page.url):
                SESSION_FILE.unlink(missing_ok=True)
                raise ValueError("Session expired — please try again to re-login.")

            _scroll(page, 8)

            # ── Name ──────────────────────────────────────────────────────────
            name = ""
            for sel in ["h1.text-heading-xlarge", "h1"]:
                el = page.query_selector(sel)
                if el:
                    name = _text(el)
                    if name:
                        break
            if not name:
                for h2 in page.query_selector_all("h2"):
                    t = _text(h2)
                    if t and "notification" not in t.lower() and len(t) < 80:
                        name = t
                        break

            # ── Headline ──────────────────────────────────────────────────────
            headline = ""
            for sel in [
                ".text-body-medium.break-words",
                ".pv-text-details__left-panel .text-body-medium",
            ]:
                el = page.query_selector(sel)
                if el:
                    t = _text(el)
                    if t:
                        headline = t
                        break
            if not headline and name:
                for section in page.query_selector_all("section"):
                    t = _text(section)
                    if name in t:
                        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
                        for i, ln in enumerate(lines):
                            if ln == name:
                                for j in range(i + 1, min(i + 5, len(lines))):
                                    candidate = lines[j]
                                    if re.match(r"^·\s*\d", candidate):
                                        continue
                                    if candidate.lower() in {"message", "connect", "follow"}:
                                        continue
                                    headline = candidate
                                    break
                                break
                        if headline:
                            break

            # ── About ─────────────────────────────────────────────────────────
            about = _section_text(page, "About")

            # ── Helper: get cleaned lines from current page ────────────────────
            def _page_lines() -> list:
                raw = page.evaluate("document.body.innerText")
                lines = [l.strip() for l in raw.splitlines()]
                # strip navigation / chrome lines before first real heading
                start = 0
                nav_words = {"home", "my network", "jobs", "messaging", "notifications",
                             "me", "for business", "try premium", "skip to main content",
                             "0 notifications", "search"}
                for i, l in enumerate(lines):
                    if l and l.lower() not in nav_words and len(l) > 2:
                        start = i
                        break
                return [l for l in lines[start:] if l]

            EMPLOYMENT_TYPES = {
                "full-time", "part-time", "contract", "freelance",
                "self-employed", "internship", "seasonal", "temporary",
                "volunteer",
            }
            DATE_RE = re.compile(
                r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{4}|present|\d{4}\s*[–\-]\s*\d{4}",
                re.IGNORECASE,
            )
            DURATION_RE = re.compile(r"\d+\s*(yr|mo|year|month)", re.IGNORECASE)

            def _is_date_line(t: str) -> bool:
                return bool(DATE_RE.search(t) or DURATION_RE.search(t))

            def _is_meta_line(t: str) -> bool:
                """True for employment-type / date / duration lines."""
                clean = t.split("·")[0].strip()
                return clean.lower() in EMPLOYMENT_TYPES or _is_date_line(t)

            def _parse_entries(lines: list, section_header: str) -> list:
                """
                Find section_header in lines, then group the following lines
                into entry dicts until we hit a known footer keyword.
                """
                FOOTER_WORDS = {"about", "accessibility", "talent solutions", "careers",
                                "privacy", "show more profiles", "more profiles for you"}
                # Find the section start
                start = None
                for i, l in enumerate(lines):
                    if l.lower().strip() == section_header.lower():
                        start = i + 1
                        break
                if start is None:
                    return []

                # Collect lines until we hit footer / another top-level nav
                body = []
                for l in lines[start:]:
                    if l.lower() in FOOTER_WORDS:
                        break
                    body.append(l)

                return body

            # ── Experience via detail page ─────────────────────────────────────
            exp_url = url + "details/experience/"
            page.goto(exp_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            _scroll(page, 8)
            time.sleep(2)

            exp_lines = _parse_entries(_page_lines(), "Experience")

            def _is_company_line(t: str) -> bool:
                """Company lines contain · but are NOT date lines."""
                return "·" in t and not _is_date_line(t)

            def _is_location_line(t: str) -> bool:
                return bool(
                    re.search(r"district|area|region|metro", t, re.I)
                    or t.lower() in {"israel", "remote", "united states", "united kingdom", "tel aviv"}
                    or re.match(r"^[A-Za-z\s]+,\s*[A-Za-z\s]+$", t)
                )

            # Build a flat list of typed tokens from the raw lines
            # Each token: (kind, value)  kind = title|company|date|location|desc
            # Strategy: walk lines, classify each based on context and lookahead
            #
            # LinkedIn experience detail page structure per entry:
            #   <Title>
            #   <Company> · <EmploymentType>   OR   <Company>   (plain, no ·)
            #   <DateFrom> - <DateTo> · <Duration>
            #   [Location]
            #   [Description lines...]
            #
            # We detect entry boundaries by: after we have title+company+date,
            # the next company-line or (plain line followed by date-line) = new entry.

            experience = []
            i = 0
            while i < len(exp_lines):
                line = exp_lines[i]

                # Skip bare date lines or company·type lines at top level
                if _is_date_line(line) or _is_company_line(line):
                    i += 1
                    continue

                # ── This line is a job title ──────────────────────────────────
                title = line
                company = date_range = location = ""
                description_parts = []
                i += 1

                # Next line: either "Company · Type" or plain company name
                if i < len(exp_lines):
                    nxt = exp_lines[i]
                    if _is_company_line(nxt):
                        company = nxt.split("·")[0].strip()
                        i += 1
                    elif not _is_date_line(nxt):
                        # Plain company name (no employment type suffix)
                        company = nxt
                        i += 1

                # Next line: date
                if i < len(exp_lines) and _is_date_line(exp_lines[i]):
                    date_range = exp_lines[i].split("·")[0].strip()
                    i += 1

                # Optional location
                if i < len(exp_lines) and _is_location_line(exp_lines[i]) and not _is_date_line(exp_lines[i]):
                    location = exp_lines[i]
                    i += 1

                # Remaining lines until next entry = description
                while i < len(exp_lines):
                    nxt = exp_lines[i]
                    # Hard stop: date or company·type line = next entry metadata
                    if _is_company_line(nxt) or _is_date_line(nxt):
                        break
                    # Lookahead: if the NEXT line is a date/company, current line is next entry's title
                    lookahead1 = exp_lines[i + 1] if i + 1 < len(exp_lines) else ""
                    lookahead2 = exp_lines[i + 2] if i + 2 < len(exp_lines) else ""
                    if _is_date_line(lookahead1) or _is_company_line(lookahead1):
                        break
                    # Two-step lookahead: plain company (no ·) followed by date
                    if not _is_date_line(lookahead1) and not _is_company_line(lookahead1) and _is_date_line(lookahead2):
                        break
                    description_parts.append(nxt)
                    i += 1

                if title and company:
                    experience.append({
                        "title": title,
                        "company": company,
                        "date_range": date_range,
                        "location": location,
                        "description": "\n".join(description_parts),
                    })

            # ── Education via detail page ──────────────────────────────────────
            edu_url = url + "details/education/"
            page.goto(edu_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            _scroll(page, 4)
            time.sleep(1)

            edu_lines = _parse_entries(_page_lines(), "Education")
            education = []
            i = 0
            while i < len(edu_lines):
                line = edu_lines[i]
                if _is_meta_line(line):
                    i += 1
                    continue
                school = line
                degree = field = years = ""
                i += 1
                while i < len(edu_lines):
                    nxt = edu_lines[i]
                    if _is_date_line(nxt):
                        years = nxt.split("·")[0].strip()
                        i += 1
                    elif not _is_meta_line(nxt) and not degree:
                        degree = nxt
                        i += 1
                    elif not _is_meta_line(nxt) and not field:
                        field = nxt
                        i += 1
                    else:
                        break
                if school:
                    education.append({"school": school, "degree": degree, "field": field, "years": years})

            # ── Certifications ────────────────────────────────────────────────
            cert_url = url + "details/certifications/"
            page.goto(cert_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            _scroll(page, 4)
            time.sleep(1)

            cert_lines = _parse_entries(_page_lines(), "Licenses & certifications")
            if not cert_lines:
                cert_lines = _parse_entries(_page_lines(), "Certifications")
            certifications = []
            i = 0
            while i < len(cert_lines):
                line = cert_lines[i]
                if _is_meta_line(line):
                    i += 1
                    continue
                name = line
                issuer = cert_date = ""
                i += 1
                while i < len(cert_lines):
                    nxt = cert_lines[i]
                    if _is_date_line(nxt):
                        cert_date = nxt.split("·")[0].strip()
                        i += 1
                    elif not _is_meta_line(nxt) and not issuer:
                        issuer = nxt
                        i += 1
                    else:
                        break
                if name:
                    certifications.append({"name": name, "issuer": issuer, "date": cert_date})

            # ── Skills via detail page ─────────────────────────────────────────
            skills_url = url + "details/skills/"
            page.goto(skills_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            _scroll(page, 6)
            time.sleep(1)

            skills_lines = _parse_entries(_page_lines(), "Skills")
            # Each skill is a short non-meta line; filter out endorsement counts and meta
            skills = []
            for l in skills_lines:
                if not _is_meta_line(l) and len(l) < 80 and not re.match(r"^\d+", l) and l not in skills:
                    skills.append(l)

            # ── Languages ─────────────────────────────────────────────────────
            lang_url = url + "details/languages/"
            page.goto(lang_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            _scroll(page, 2)
            time.sleep(1)

            lang_lines = _parse_entries(_page_lines(), "Languages")
            languages = []
            proficiency_words = {"native", "bilingual", "professional", "limited", "elementary", "full"}
            i = 0
            while i < len(lang_lines):
                line = lang_lines[i]
                if _is_meta_line(line):
                    i += 1
                    continue
                lang_name = line
                proficiency = ""
                i += 1
                if i < len(lang_lines) and any(w in lang_lines[i].lower() for w in proficiency_words):
                    proficiency = lang_lines[i]
                    i += 1
                languages.append({"language": lang_name, "proficiency": proficiency})

            # ── Location from main page ────────────────────────────────────────
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            _scroll(page, 2)
            location = ""
            # Try CSS selectors first, fall back to text parsing
            for sel in [".pv-text-details__left-panel span.text-body-small", ".text-body-small.inline.t-black--light"]:
                el = page.query_selector(sel)
                if el:
                    t = _text(el)
                    if t and len(t) < 100:
                        location = t
                        break
            if not location:
                main_lines = _page_lines()
                for j, l in enumerate(main_lines):
                    if l == name and j + 2 < len(main_lines):
                        candidate = main_lines[j + 2]
                        if re.search(r"[A-Z][a-z]", candidate) and len(candidate) < 80:
                            location = candidate
                        break

            print(f"[scraper] name={name!r} exp={len(experience)} edu={len(education)} skills={len(skills)} certs={len(certifications)} langs={len(languages)}")

            if not name and not headline and not experience:
                raise ValueError("Could not extract profile data. The profile may be private, or LinkedIn blocked the request.")

            return {
                "name": name,
                "headline": headline,
                "location": location,
                "about": about,
                "experience": experience,
                "education": education,
                "certifications": certifications,
                "skills": skills,
                "languages": languages,
            }

        finally:
            browser.close()
