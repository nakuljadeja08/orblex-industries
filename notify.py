"""
notify.py — email alerts for the automated trading jobs (replaces the Claude tasks' phone
pushes). Uses the newsletter's SMTP settings but sends ONLY to ALERT_TO (default: the SMTP
account's own mailbox), never to the newsletter list: alerts mention positions and orders.
"""

import logging
import os
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send(subject: str, body: str) -> bool:
    user, pw = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    to = [a.strip() for a in os.environ.get("ALERT_TO", user).split(",") if a.strip()]
    if not user or not pw or not to:
        logger.warning("  email not sent: SMTP_USER / SMTP_PASSWORD / ALERT_TO missing")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[Orblex] {subject}"
    msg["From"] = os.environ.get("NEWSLETTER_FROM") or user
    msg["To"] = ", ".join(to)
    try:
        with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "587"))) as s:
            s.ehlo()
            s.starttls()
            s.login(user, pw)
            s.sendmail(user, to, msg.as_string())
        return True
    except Exception as e:                       # an alert failure must never fail the job
        logger.warning(f"  email failed: {e}")
        return False
