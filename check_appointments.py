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
import time
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

URL = "https://visas-fr.tlscontact.com/workflow/appointment-booking/tnTUN2fr/28361308"

STATE_FILE = "state.json"
LOGGED_OUT_ALERT_COOLDOWN_HOURS = 4

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

# Phrases suggesting Cloudflare (or similar) is showing an interstitial
# challenge page instead of the real site content.
BLOCKED_PHRASES = [
    "just a moment",
    "checking your browser",
    "attention required",
    "cf-browser-verification",
    "verify you are human",
    "please wait while we verify",
    "ray id",
]

MIN_EXPECTED_COOKIES = 6  # a real logged-in session usually has more than 4


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def should_send_cooldown_alert(key: str, cooldown_hours: float) -> bool:
    state = _load_state()
    last = state.get(key, 0)
    if time.time() - last < cooldown_hours * 3600:
        return False
    state[key] = time.time()
    _save_state(state)
    return True


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
    """Returns one of: 'no_slots', 'logged_out', 'blocked', 'uncertain'."""
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

        # Poll for up to ~20s: keep re-checking content as the page finishes
        # rendering, instead of trusting one fixed-length sleep. Stop early
        # the moment we recognize a known state.
        content = ""
        for _ in range(10):
            content = page.content().lower()
            content = content.replace("\u2019", "'").replace("\u2018", "'")
            if (
                any(p in content for p in NO_SLOTS_PHRASES)
                or any(p in content for p in LOGGED_OUT_PHRASES)
                or any(p in content for p in BLOCKED_PHRASES)
            ):
                break
            page.wait_for_timeout(2000)

        screenshot_path = "page_state.png"
        page.screenshot(path=screenshot_path, full_page=True)

        final_url = page.url
        title = page.title()
        browser.close()

    # Debug: which category each phrase list matched, plus page identity —
    # deliberately not printing the raw page content (could contain your
    # personal application details, and this may be a public repo's logs).
    print(f"Final URL: {final_url}")
    print(f"Page title: {title}")
    print(f"Content length: {len(content)} chars")
    print(f"NO_SLOTS matched: {[p for p in NO_SLOTS_PHRASES if p in content]}")
    print(f"LOGGED_OUT matched: {[p for p in LOGGED_OUT_PHRASES if p in content]}")
    print(f"BLOCKED matched: {[p for p in BLOCKED_PHRASES if p in content]}")

    if any(phrase in content for phrase in BLOCKED_PHRASES):
        print("Page looks like a bot-check / interstitial page, not real content.")
        return "blocked"

    if any(phrase in content for phrase in LOGGED_OUT_PHRASES):
        print("Page looks like a login/session-expired page, not the appointment page.")
        return "logged_out"

    no_slots = any(phrase in content for phrase in NO_SLOTS_PHRASES)

    if no_slots:
        print("No slots detected (matched a 'no availability' phrase).")
        return "no_slots"

    print(
        "No known phrase matched (not 'no slots', not logged-out, not blocked). "
        "This is ambiguous — could be real slots, or could be an unrecognized "
        "page state. Flagging as 'uncertain' rather than claiming slots are open."
    )
    return "uncertain"


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

    if result == "no_slots":
        print("Still no appointments. No email sent.")
        return

    if result == "logged_out":
        print("Session appears logged out — re-export your cookies.")
        if should_send_cooldown_alert("last_logged_out_alert", LOGGED_OUT_ALERT_COOLDOWN_HOURS):
            send_email(
                "[TLS checker] Session expired — please refresh cookies",
                "The checker landed on what looks like a login page instead of "
                "the appointment page. Your session cookies have likely expired "
                "— re-export them from your browser and update the "
                "TLS_COOKIES_JSON secret.\n\n"
                f"(You won't get another one of these for {LOGGED_OUT_ALERT_COOLDOWN_HOURS}h, "
                "to avoid spamming you while you fix it.)",
            )
        return

    if result == "blocked":
        print("Page looks like a bot-check/interstitial page.")
        if should_send_cooldown_alert("last_blocked_alert", LOGGED_OUT_ALERT_COOLDOWN_HOURS):
            send_email(
                "[TLS checker] Got a bot-check page, not the real site",
                "The checker's request seems to have been intercepted by a "
                "verification/interstitial page instead of loading the real "
                "appointment page. This usually resolves itself, but if it "
                "keeps happening the checker may need adjusting.\n\n"
                f"(You won't get another one of these for {LOGGED_OUT_ALERT_COOLDOWN_HOURS}h.)",
            )
        return

    if result == "uncertain":
        print("Ambiguous page state — sending a 'please verify manually' alert, not a confident one.")
        if should_send_cooldown_alert("last_uncertain_alert", 1):
            send_email(
                "TLScontact: page state changed — please check manually",
                f"The checker no longer recognizes the page state on:\n{URL}\n\n"
                "This does NOT confidently mean slots are open — it just means "
                "the known 'no availability' message wasn't found. It could be "
                "real news, or it could be a page change / rendering issue. "
                "Please go check the site yourself.\n"
                "(A screenshot was saved as a workflow artifact for reference.)\n\n"
                "(You won't get another one of these for 1h, to avoid repeat pings "
                "while the page stays in this state.)",
            )
        return


if __name__ == "__main__":
    main()
