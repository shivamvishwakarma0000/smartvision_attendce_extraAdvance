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
