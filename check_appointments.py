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
# These are the *exact* phrases confirmed on the real page. If NONE of these
# are found, we assume something changed and alert.
NO_SLOTS_PHRASES = [
    "n'avons actuellement plus de cr\u00e9neaux de rendez-vous disponibles",
    "aucun cr\u00e9neau n'est disponible pour le moment",
    "aucun cr\u00e9neau",  # broader fallback
    "plus de cr\u00e9neaux de rendez-vous disponibles",  # broader fallback
    # Older/generic fallbacks kept in case wording varies elsewhere:
    "aucun rendez-vous",
    "aucune disponibilit",
    "no appointment",
    "no availability",
]

# Phrases suggesting the session isn't actually logged in (expired/invalid
# cookies), so a "no NO_SLOTS_PHRASES found" result would be a false alarm,
# not real news.
LOGGED_OUT_PHRASES = [
    "se connecter",
    "connexion",
    "identifiant",
    "mot de passe",
    "sign in",
    "log in",
    "session expir",
    "veuillez vous connecter",
]

MIN_EXPECTED_COOKIES = 6  # a real logged-in session usually has more than 4


def _normalize_same_site(value):
    """Cookie-Editor uses values like 'no_restriction' / 'unspecified' / 'lax';
    Playwright requires exactly 'Strict', 'Lax', or 'None'."""
    try:
        v = str(value).strip().lower()
    except Exception:
        return "Lax"
    if v in ("no_restriction", "none", "none.", "null"):
        return "None"
    if v == "strict":
        return "Strict"
    # 'lax', 'unspecified', '', 'true', 'false', or anything else -> Lax
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


def check_once() -> str:
    """Returns one of: 'slots', 'no_slots', 'logged_out'."""
    print("check_appointments.py version: cookie-normalizer-v2")
    cookies = load_cookies()

    # Debug: show what we're about to hand Playwright (no cookie values).
    same_site_summary = [(c["name"], c["sameSite"]) for c in cookies]
    print(f"Loaded {len(cookies)} cookies. name/sameSite pairs: {same_site_summary}")
    if len(cookies) < MIN_EXPECTED_COOKIES:
        print(
            f"WARNING: only {len(cookies)} cookies loaded, fewer than the "
            f"expected {MIN_EXPECTED_COOKIES}+ for a real logged-in session. "
            "Re-export cookies while fully logged in and on the appointment page."
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        try:
            context.add_cookies(cookies)
        except Exception:
            print("add_cookies failed. Full normalized cookie list (values redacted):")
            for c in cookies:
                redacted = {k: ("<redacted>" if k == "value" else v) for k, v in c.items()}
                print(redacted)
            raise
        page = context.new_page()

        page.goto(URL, wait_until="networkidle", timeout=45000)
        # Give any lazy-loaded calendar/slot widget a moment to render.
        page.wait_for_timeout(4000)

        content = page.content().lower()
        # Normalize curly/smart apostrophes to plain ones so phrase matching
        # doesn't depend on which one the site happens to render.
        content = content.replace("\u2019", "'").replace("\u2018", "'")
        screenshot_path = "page_state.png"
        page.screenshot(path=screenshot_path, full_page=True)

        browser.close()

    if any(phrase in content for phrase in LOGGED_OUT_PHRASES):
        print("Page looks like a login/session-expired page, not the appointment page.")
        return "logged_out"

    no_slots = any(phrase in content for phrase in NO_SLOTS_PHRASES)

    if no_slots:
        print("No slots detected (matched a 'no availability' phrase).")
        return "no_slots"

    print("No 'no availability' phrase matched, and page looks logged in — slots might be open!")
    return "slots"


def main():
    try:
        result = check_once()
    except Exception as exc:  # noqa: BLE001
        print(f"Check failed with an error: {exc}", file=sys.stderr)
        # Optionally email yourself on repeated failures (e.g. cookies expired)
        if os.environ.get("ALERT_ON_ERROR") == "1":
            send_email(
                "[TLS checker] Script error — check your cookies",
                f"The checker hit an error, possibly expired login cookies:\n\n{exc}",
            )
        sys.exit(1)

    if result == "slots":
        send_email(
            "TLScontact: possible appointment slot available!",
            f"The checker no longer sees a 'no availability' message on:\n{URL}\n\n"
            "Go check and book immediately — slots disappear fast.\n"
            "(A screenshot was saved as a workflow artifact for reference.)",
        )
        print("Alert email sent.")
    elif result == "logged_out":
        print("Session appears logged out — re-export your cookies. No alert sent (would be a false positive).")
        if os.environ.get("ALERT_ON_ERROR") == "1":
            send_email(
                "[TLS checker] Session expired — please refresh cookies",
                "The checker landed on what looks like a login page instead of "
                "the appointment page. Your session cookies have likely expired "
                "— re-export them from your browser and update the "
                "TLS_COOKIES_JSON secret.",
            )
    else:
        print("Still no appointments. No email sent.")


if __name__ == "__main__":
    main()
