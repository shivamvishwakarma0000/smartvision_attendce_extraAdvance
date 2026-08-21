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
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import current_app

def _dispatch_smtp_worker(host, port, username, password, sender, use_tls, to_email, subject, body_text, body_html):
    """Worker thread function for non-blocking SMTP dispatch."""
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = to_email

        msg.attach(MIMEText(body_text, 'plain'))
        if body_html:
            msg.attach(MIMEText(body_html, 'html'))

        server = smtplib.SMTP(host, port, timeout=8)
        if use_tls:
            server.starttls()
        server.login(username, password)
        server.sendmail(sender, [to_email], msg.as_string())
        server.quit()
        print(f"[SMTP Mail Sent] Subject: '{subject}' to {to_email}", flush=True)
    except Exception as e:
        print(f"[SMTP Error] Failed to send email to {to_email}: {e}", flush=True)

# ==============================================================================
# CORE EMAIL DISPATCH FUNCTION
# ==============================================================================
def send_email(to_email, subject, body_text, body_html=None, sync=False):
    """
    Dispatches email via SMTP if credentials are configured in environment.
    Falls back to a clean server console log if SMTP credentials are not configured.
    By default dispatches asynchronously in a background thread to prevent UI freezing.
    
    Parameters:
        to_email (str): Recipient email address
        subject (str): Email subject line
        body_text (str): Plain text version of email message
        body_html (str, optional): HTML formatted version of email message
        sync (bool, optional): If True, waits synchronously for SMTP completion.
        
    Returns:
        tuple (bool, str): Status flag and descriptive message.
    """
    try:
        host = current_app.config.get('SMTP_HOST')
        port = current_app.config.get('SMTP_PORT', 587)
        username = current_app.config.get('SMTP_USERNAME')
        password = current_app.config.get('SMTP_PASSWORD')
        sender = current_app.config.get('SMTP_SENDER_EMAIL', 'noreply@smartvision.com')
        use_tls = current_app.config.get('SMTP_USE_TLS', True)
    except Exception:
        host = os.environ.get('SMTP_HOST') or os.environ.get('MAIL_SERVER')
        port = int(os.environ.get('SMTP_PORT') or os.environ.get('MAIL_PORT', 587))
        username = os.environ.get('SMTP_USERNAME') or os.environ.get('MAIL_USERNAME')
        password = os.environ.get('SMTP_PASSWORD') or os.environ.get('MAIL_PASSWORD')
        sender = os.environ.get('SMTP_SENDER_EMAIL') or os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@smartvision.com')
        use_tls = os.environ.get('SMTP_USE_TLS', 'True').lower() in ('true', '1', 'yes')

    # --------------------------------------------------------------------------
    # 1. ATTEMPT REAL-TIME SMTP DISPATCH
    # --------------------------------------------------------------------------
    if host and username and password:
        if sync:
            _dispatch_smtp_worker(host, port, username, password, sender, use_tls, to_email, subject, body_text, body_html)
        else:
            t = threading.Thread(
                target=_dispatch_smtp_worker,
                args=(host, port, username, password, sender, use_tls, to_email, subject, body_text, body_html),
                daemon=True
            )
            t.start()
        return True, "Email dispatch scheduled."

    # --------------------------------------------------------------------------
    # 2. DEVELOPMENT / LOCAL CONSOLE FALLBACK
    # --------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print(f"[SMARTVISION MAIL DISPATCHER (DEVELOPMENT LOG)]")
    print(f"  To      : {to_email}")
    print(f"  Subject : {subject}")
    print(f"  Content :\n{body_text}")
    print("=" * 80 + "\n", flush=True)
    return True, "Email logged to console (Development Mode)."
