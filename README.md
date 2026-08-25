# TLScontact appointment slot alert

Checks your TLScontact appointment page every 10 minutes (for free, in the
cloud, via GitHub Actions — no computer of yours needs to stay on) and
emails you the moment it looks like a slot might be open.

## How it works
A small script opens the page with a real (headless) browser using your
logged-in session cookies, and looks for the "no appointments available"
message. If that message is gone, it assumes something changed and emails
you immediately so you can go book before it disappears.

**Important limitation:** this is a heuristic, not a guarantee. TLScontact
can change their page text/structure at any time, which would break the
detection. If no alert ever arrives, check in yourself every so often as a
backup, especially in the first few days.

## One-time setup (about 15 minutes)

### 1. Create a free GitHub account (if you don't have one)
https://github.com/join

### 2. Create a new **public** repository
Public repos get unlimited free Actions minutes, which is what lets this
check every 10 minutes indefinitely for free. (Your cookies/email password
are stored as encrypted "Secrets" — nobody can read them even though the
repo is public, but don't put them anywhere else in the code.)

Upload these files to it (drag-and-drop on github.com works fine):
- `check_appointments.py`
- `requirements.txt`
- `.github/workflows/check.yml`

### 3. Get your session cookies
1. Log in to the TLScontact site normally in Chrome, and get to the
   appointment page.
2. Install a cookie-export extension, e.g. **"Cookie-Editor"** (Chrome Web
   Store, free).
3. On the TLScontact tab, open Cookie-Editor → **Export** → **Export as
   JSON**. This copies a JSON array of your cookies to your clipboard.

### 4. Create a Gmail "App Password" (so the script can send email)
1. Turn on 2-Step Verification on your Google account if not already on:
   https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password (name it e.g. "tls-alert") and copy the 16-character code.

### 5. Add the secrets to your GitHub repo
In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add these four:

| Secret name           | Value                                              |
|------------------------|-----------------------------------------------------|
| `TLS_COOKIES_JSON`     | the JSON you copied from Cookie-Editor              |
| `GMAIL_USER`           | your Gmail address                                  |
| `GMAIL_APP_PASSWORD`   | the 16-character app password from step 4           |
| `ALERT_EMAIL_TO`       | the email address you want the alert sent to        |

### 6. Test it
Go to the **Actions** tab in your repo → "Check TLS appointment slots" →
**Run workflow** (this uses the `workflow_dispatch` trigger). Watch the run;
click into it and check the logs, and download the `page-state` screenshot
artifact to confirm it actually loaded the real, logged-in page (not a
login screen — if it shows a login screen, your cookies expired or weren't
copied correctly).

Once that works, it will run automatically every 10 minutes from then on.

## Maintenance
- **Cookies expire.** Sessions typically last hours to a couple of weeks
  depending on the site. If you stop getting confidence it's working,
  re-export cookies (step 3) and update the `TLS_COOKIES_JSON` secret.
- To get notified automatically when the cookies expire (instead of silent
  failure), change `ALERT_ON_ERROR: "0"` to `"1"` in `check.yml` after your
  first successful run.
- To check more or less often, edit the `cron` line in `check.yml`
  (`*/10 * * * *` = every 10 minutes; `*/5 * * * *` = every 5 minutes).
  Very frequent checks increase the chance the site flags/blocks the
  requests, so I'd avoid going below every 5 minutes.
