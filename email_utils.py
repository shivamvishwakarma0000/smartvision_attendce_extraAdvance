# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - SMTP EMAIL DISPATCHER UTILITY
# ==============================================================================
# Description: Automated email notification dispatcher using SMTP (TLS/SSL).
#              Dispatches attendance warnings, OTP verifications, password reset links,
#              and emergency absence alerts with graceful development fallback logging.
# ==============================================================================

import os
import smtplib
import threading
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import current_app

def _dispatch_resend_api(api_key, sender, to_email, subject, body_text, body_html):
    """Dispatches transactional email via Resend Cloud API (Port 443 HTTPS)."""
    try:
        from_addr = sender if ('@' in sender and 'gmail' not in sender.lower()) else "SmartVision <onboarding@resend.dev>"
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json"
        }
        payload = {
            "from": from_addr,
            "to": [to_email],
            "subject": subject,
            "html": body_html if body_html else body_text,
            "text": body_text
        }
        res = requests.post("https://api.resend.com/emails", json=payload, headers=headers, timeout=6)
        if res.status_code in (200, 201):
            print(f"[Resend API Sent] Subject: '{subject}' to {to_email}", flush=True)
            return True
        else:
            print(f"[Resend API Error: {res.status_code}] {res.text}", flush=True)
            return False
    except Exception as e:
        print(f"[Resend API Exception: {e}]", flush=True)
        return False

def _dispatch_brevo_api(api_key, sender, to_email, subject, body_text, body_html):
    """Dispatches transactional email via Brevo / Sendinblue Cloud API (Port 443 HTTPS)."""
    try:
        from_email = sender if ('@' in sender) else "smartvision.portal@gmail.com"
        headers = {
            "api-key": api_key.strip(),
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        payload = {
            "sender": {"name": "SmartVision Portal", "email": from_email},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": body_html if body_html else body_text,
            "textContent": body_text
        }
        res = requests.post("https://api.brevo.com/v3/smtp/email", json=payload, headers=headers, timeout=6)
        if res.status_code in (200, 201):
            print(f"[Brevo API Sent] Subject: '{subject}' to {to_email}", flush=True)
            return True
        else:
            print(f"[Brevo API Error: {res.status_code}] {res.text}", flush=True)
            return False
    except Exception as e:
        print(f"[Brevo API Exception: {e}]", flush=True)
        return False

def _dispatch_smtp_worker(host, port, username, password, sender, use_tls, to_email, subject, body_text, body_html):
    """Worker function for SMTP dispatch with direct Port 465 SSL primary and Port 587 fallback."""
    clean_user = (username or 'vishshivam16@gmail.com').strip()
    clean_pass = (password or 'vyrnmtsahqxychqh').strip().replace(' ', '')
    clean_sender = clean_user if ('gmail' in (host or 'smtp.gmail.com').lower()) else (sender or clean_user).strip()

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f"SmartVision Portal <{clean_sender}>"
    msg['To'] = to_email

    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
    if body_html:
        msg.attach(MIMEText(body_html, 'html', 'utf-8'))

    # Method 1: Port 465 (Direct SSL) - Works reliably on cloud platforms (Render, AWS, GCP)
    try:
        server = smtplib.SMTP_SSL(host or 'smtp.gmail.com', 465, timeout=5)
        server.login(clean_user, clean_pass)
        server.sendmail(clean_sender, [to_email], msg.as_string())
        server.quit()
        print(f"[SMTP Mail Sent via Port 465 SSL] Subject: '{subject}' to {to_email}", flush=True)
        return True
    except Exception as e_ssl:
        print(f"[SMTP Port 465 SSL Failed: {e_ssl}] Attempting Port 587 STARTTLS...", flush=True)

    # Method 2: Port 587 (STARTTLS) Fallback
    try:
        server = smtplib.SMTP(host or 'smtp.gmail.com', 587, timeout=5)
        server.starttls()
        server.login(clean_user, clean_pass)
        server.sendmail(clean_sender, [to_email], msg.as_string())
        server.quit()
        print(f"[SMTP Mail Sent via Port 587 STARTTLS] Subject: '{subject}' to {to_email}", flush=True)
        return True
    except Exception as e_tls:
        print(f"[SMTP Port 587 STARTTLS Failed: {e_tls}]", flush=True)
        return False

def _dispatch_unified_worker(resend_api_key, brevo_api_key, host, port, username, password, sender, use_tls, to_email, subject, body_text, body_html):
    """Unified email dispatcher with automatic failover chain."""
    # 1. Try Resend Cloud API
    if resend_api_key:
        if _dispatch_resend_api(resend_api_key, sender, to_email, subject, body_text, body_html):
            return True
        print("[Resend Notice: Delivery failed or sandbox restricted] Automatically falling back to SMTP...", flush=True)

    # 2. Try Brevo Cloud API
    if brevo_api_key:
        if _dispatch_brevo_api(brevo_api_key, sender, to_email, subject, body_text, body_html):
            return True
        print("[Brevo Notice: Delivery failed] Falling back to SMTP...", flush=True)

    # 3. Try Direct SMTP (Port 465 SSL -> Port 587 STARTTLS)
    if host and username and password:
        if _dispatch_smtp_worker(host, port, username, password, sender, use_tls, to_email, subject, body_text, body_html):
            return True

    # 4. Safe Console Log Fallback
    print(f"[EMAIL DEV FALLBACK LOG] To: {to_email} | Subject: {subject}\n{body_text}", flush=True)
    return False

# ==============================================================================
# CORE EMAIL DISPATCH FUNCTION
# ==============================================================================
def send_email(to_email, subject, body_text, body_html=None, sync=False):
    """
    Dispatches email via Resend API, Brevo API, or SMTP if credentials are configured.
    Falls back gracefully to development console logging if offline.
    """
    resend_api_key = os.environ.get('RESEND_API_KEY', '').strip()
    brevo_api_key = os.environ.get('BREVO_API_KEY', '').strip()

    try:
        host = current_app.config.get('SMTP_HOST') or 'smtp.gmail.com'
        port = current_app.config.get('SMTP_PORT', 587)
        username = current_app.config.get('SMTP_USERNAME') or 'vishshivam16@gmail.com'
        password = current_app.config.get('SMTP_PASSWORD') or 'vyrn mtsa hqxy chqh'
        sender = current_app.config.get('SMTP_SENDER_EMAIL') or username or 'vishshivam16@gmail.com'
        use_tls = current_app.config.get('SMTP_USE_TLS', True)
    except Exception:
        host = os.environ.get('SMTP_HOST') or os.environ.get('MAIL_SERVER') or 'smtp.gmail.com'
        port = int(os.environ.get('SMTP_PORT') or os.environ.get('MAIL_PORT', 587))
        username = os.environ.get('SMTP_USERNAME') or os.environ.get('MAIL_USERNAME') or 'vishshivam16@gmail.com'
        password = os.environ.get('SMTP_PASSWORD') or os.environ.get('MAIL_PASSWORD') or 'vyrn mtsa hqxy chqh'
        sender = os.environ.get('SMTP_SENDER_EMAIL') or os.environ.get('MAIL_DEFAULT_SENDER') or username or 'vishshivam16@gmail.com'
        use_tls = os.environ.get('SMTP_USE_TLS', 'True').lower() in ('true', '1', 'yes')

    if sync:
        _dispatch_unified_worker(resend_api_key, brevo_api_key, host, port, username, password, sender, use_tls, to_email, subject, body_text, body_html)
    else:
        threading.Thread(
            target=_dispatch_unified_worker,
            args=(resend_api_key, brevo_api_key, host, port, username, password, sender, use_tls, to_email, subject, body_text, body_html),
            daemon=True
        ).start()

    return True, "Email dispatch initiated."
