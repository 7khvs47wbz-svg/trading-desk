"""Sends email through the same Google Apps Script web app MEDAFIND uses: a POST with a shared
secret, and the script calls MailApp.sendEmail from the owner's Gmail (about 100 emails a day).
Set EMAIL_WEBHOOK_URL and EMAIL_WEBHOOK_SECRET in the Render dashboard. Render's free plan blocks
SMTP, so this goes over HTTPS. Without them, local runs print the email to the console."""
import os

import requests


def configured():
    return bool(os.environ.get("EMAIL_WEBHOOK_URL") and os.environ.get("EMAIL_WEBHOOK_SECRET"))


def send(to, subject, body):
    if not configured():
        if os.environ.get("RENDER") or os.environ.get("PRODUCTION"):
            raise ValueError("Password reset by email isn't switched on yet. Please contact the site owner.")
        print(f"\n[DEV EMAIL] to={to}\n  {subject}\n  {body}\n", flush=True)
        return
    r = requests.post(os.environ["EMAIL_WEBHOOK_URL"], timeout=20,
                      json={"secret": os.environ["EMAIL_WEBHOOK_SECRET"], "to": to, "subject": subject, "body": body})
    if not r.ok:
        raise ValueError("Couldn't send the email right now. Please try again in a few minutes.")
