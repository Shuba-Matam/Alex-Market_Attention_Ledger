"""Password-reset delivery through a free Gmail Apps Script relay.

Render's Free plan blocks outbound SMTP, so the backend POSTs over HTTPS to a
small Apps Script web app owned by the Gmail account. That script calls
MailApp.sendEmail and therefore sends from the Gmail address. No paid service,
no API key, and no OAuth refresh token that expires weekly.

Without relay configuration the code is written to the server log, which keeps
local development and tests usable.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def send_password_reset_code(to_email: str, code: str) -> None:
    url = os.environ.get("GMAIL_RELAY_URL")
    secret = os.environ.get("GMAIL_RELAY_SECRET")
    if not url or not secret:
        print(f"[password-reset] Gmail relay unset; code for {to_email}: {code}",
              file=sys.stderr)
        return
    payload = json.dumps({
        "secret": secret,
        "to": to_email,
        "subject": "Your Alex Watchlist password reset code",
        "text": (
            f"Your reset code is {code}.\n\n"
            "It expires in 10 minutes and can be used once. "
            "If you did not request this, you can ignore this email."
        ),
    }).encode()
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except Exception as error:  # delivery state must not leak to the caller
        print(f"[password-reset] Gmail relay failed for {to_email}: {error}",
              file=sys.stderr)
