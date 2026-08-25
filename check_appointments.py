#!/usr/bin/env python3
"""
TLScontact appointment slot checker.

Loads the appointment booking page using a real (headless) browser with
your logged-in session cookies, looks for signs that a slot is available,
and emails you if it finds one.

This runs once per invocation — the GitHub Actions workflow calls it
on a schedule (see .github/workflows/check.yml).
"""

import json
import os
import smtplib
import sys
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

URL = "https://visas-fr.tlscontact.com/workflow/appointment-booking/tnTUN2fr/28361308"

# Phrases that indicate NO appointments are available (French TLScontact UI).
# If NONE of these are found on the page, we assume something changed and alert.
NO_SLOTS_PHRASES = [
    "aucun rendez-vous",
    "aucune disponibilit",
    "no appointment",
    "no availability",
    "pas de cr\u00e9neau",
    "pas de rendez-vous disponible",
]


def _normalize_same_site(value):
    """Cookie-Editor uses values like 'no_restriction' / 'unspecified' / 'lax';
    Playwright requires exactly 'Strict', 'Lax', or 'None'."""
    if not value:
        return "Lax"
    v = str(value).strip().lower()
    if v in ("no_restriction", "none"):
        return "None"
    if v in ("strict",):
        return "Strict"
    # 'lax', 'unspecified', or anything else -> default to Lax
    return "Lax"


def load_cookies():
    """Cookies exported from your logged-in browser session (JSON array),
    normalized into the exact shape Playwright expects."""
    raw = os.environ.get("TLS_COOKIES_JSON")
    if not raw:
        print("ERROR: TLS_COOKIES_JSON env var is missing.", file=sys.stderr)
        sys.exit(1)

    raw_cookies = json.loads(raw)
    normalized = []
    for c in raw_cookies:
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": _normalize_same_site(c.get("sameSite")),
        }
        # Browsers require Secure=true for SameSite=None cookies; enforce
        # that here so Playwright doesn't reject the cookie.
        if cookie["sameSite"] == "None":
            cookie["secure"] = True
        # Session cookies have no expiry; Cookie-Editor uses 'expirationDate'
        # (seconds since epoch, float). Playwright wants 'expires' as the
        # same, or -1 / omitted for a session cookie.
        expiration = c.get("expirationDate")
        if expiration:
            cookie["expires"] = expiration
        normalized.append(cookie)
    return normalized


def send_email(subject: str, body: str):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    to_addr = os.environ["ALERT_EMAIL_TO"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, [to_addr], msg.as_string())


def check_once() -> bool:
    """Returns True if slots appear to be available."""
    cookies = load_cookies()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        context.add_cookies(cookies)
        page = context.new_page()

        page.goto(URL, wait_until="networkidle", timeout=45000)
        # Give any lazy-loaded calendar/slot widget a moment to render.
        page.wait_for_timeout(4000)

        content = page.content().lower()
        screenshot_path = "page_state.png"
        page.screenshot(path=screenshot_path, full_page=True)

        browser.close()

    no_slots = any(phrase in content for phrase in NO_SLOTS_PHRASES)

    if no_slots:
        print("No slots detected (matched a 'no availability' phrase).")
        return False

    print("No 'no availability' phrase matched — slots might be open!")
    return True


def main():
    try:
        slots_available = check_once()
    except Exception as exc:  # noqa: BLE001
        print(f"Check failed with an error: {exc}", file=sys.stderr)
        # Optionally email yourself on repeated failures (e.g. cookies expired)
        if os.environ.get("ALERT_ON_ERROR") == "1":
            send_email(
                "[TLS checker] Script error — check your cookies",
                f"The checker hit an error, possibly expired login cookies:\n\n{exc}",
            )
        sys.exit(1)

    if slots_available:
        send_email(
            "TLScontact: possible appointment slot available!",
            f"The checker no longer sees a 'no availability' message on:\n{URL}\n\n"
            "Go check and book immediately — slots disappear fast.\n"
            "(A screenshot was saved as a workflow artifact for reference.)",
        )
        print("Alert email sent.")
    else:
        print("Still no appointments. No email sent.")


if __name__ == "__main__":
    main()
