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
    """Worker thread function for non-blocking SMTP dispatch with dual-port failover."""
    try:
        sender_email = str(sender).strip()
        if '<' in sender_email and '>' in sender_email:
            sender_email = sender_email.split('<')[1].split('>')[0].strip()

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"SmartVision Portal <{sender_email}>"
        msg['To'] = to_email

        msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
        if body_html:
            msg.attach(MIMEText(body_html, 'html', 'utf-8'))

        # List of ports to attempt (primary configured port first, then fallback)
        ports_to_try = [port]
        if port != 465 and 465 not in ports_to_try:
            ports_to_try.append(465)
        if port != 587 and 587 not in ports_to_try:
            ports_to_try.append(587)

        last_error = None
        for attempt_port in ports_to_try:
            try:
                if attempt_port == 465:
                    server = smtplib.SMTP_SSL(host, attempt_port, timeout=15)
                else:
                    server = smtplib.SMTP(host, attempt_port, timeout=15)
                    if use_tls:
                        server.starttls()
                        
                server.login(username, password)
                server.sendmail(sender_email, [to_email], msg.as_string())
                server.quit()
                print(f"[SMTP Mail Sent] Subject: '{subject}' successfully delivered to {to_email} (via port {attempt_port})", flush=True)
                return True
            except Exception as e:
                last_error = e
                print(f"[SMTP Port {attempt_port} Warning] Could not deliver via port {attempt_port}: {e}. Trying next port...", flush=True)

        print(f"[SMTP Error] Failed to send email to {to_email} after all attempts: {last_error}", flush=True)
        return False
    except Exception as e:
        print(f"[SMTP Fatal Error] Worker encountered an error for {to_email}: {e}", flush=True)
        return False

# ==============================================================================
# CORE EMAIL DISPATCH FUNCTION
# ==============================================================================
def send_email(to_email, subject, body_text, body_html=None, sync=False):
    """
    Dispatches email via SMTP if credentials are configured in environment.
    Falls back to a clean server console log if SMTP credentials are not configured.
    By default dispatches asynchronously in a background thread to prevent UI freezing.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    host = os.environ.get('SMTP_HOST')
    port = int(os.environ.get('SMTP_PORT', 465)) if os.environ.get('SMTP_PORT') and str(os.environ.get('SMTP_PORT')).isdigit() else 465
    username = os.environ.get('SMTP_USERNAME')
    password = os.environ.get('SMTP_PASSWORD')
    sender = os.environ.get('SMTP_SENDER_EMAIL') or username or 'noreply@smartvision.com'
    use_tls = os.environ.get('SMTP_USE_TLS', 'true').lower() in ('true', '1', 'yes')

    # Fallback to current_app if available
    try:
        if current_app:
            host = current_app.config.get('SMTP_HOST') or host
            port = int(current_app.config.get('SMTP_PORT', port))
            username = current_app.config.get('SMTP_USERNAME') or username
            password = current_app.config.get('SMTP_PASSWORD') or password
            sender = current_app.config.get('SMTP_SENDER_EMAIL') or sender
            use_tls = current_app.config.get('SMTP_USE_TLS', use_tls)
    except Exception:
        pass

    if not host:
        host = 'smtp.gmail.com'

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


# ==============================================================================
# RICH REGISTRATION OTP EMAIL BUILDER
# ==============================================================================
def send_registration_otp_email(to_email, otp_code, verify_link=None, sync=False):
    """
    Sends a beautifully styled HTML registration verification email with OTP code & direct verification button.
    """
    subject = f"Your SmartVision Verification Code: {otp_code}"
    
    plain_text = f"""Hello,

Your email verification code for SmartVision Attendance Portal is:

{otp_code}

Please enter this 6-digit code on the registration page to verify your email address.
This code will expire in 15 minutes.

{f'Alternatively, you can verify directly by clicking this link: {verify_link}' if verify_link else ''}

If you did not request this verification code, please ignore this email.

Best regards,
SmartVision Attendance Portal Team
"""

    verify_button_html = f"""
    <div style="text-align: center; margin: 24px 0 10px 0;">
        <a href="{verify_link}" style="background-color: #4f46e5; color: #ffffff; padding: 12px 28px; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 15px; display: inline-block; box-shadow: 0 4px 6px -1px rgba(79, 70, 229, 0.2);">
            ✓ Click Here to Verify Email Automatically
        </a>
    </div>
    """ if verify_link else ""

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin: 0; padding: 0; background-color: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #334155;">
        <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #0f172a; padding: 30px 15px;">
            <tr>
                <td align="center">
                    <table width="100%" max-width="520" cellpadding="0" cellspacing="0" style="max-width: 520px; background-color: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3);">
                        
                        <!-- Header Banner -->
                        <tr>
                            <td style="background: linear-gradient(135deg, #1e1b4b 0%, #312e81 50%, #4338ca 100%); padding: 32px 24px; text-align: center;">
                                <div style="display: inline-block; background: rgba(255, 255, 255, 0.15); padding: 8px 16px; border-radius: 30px; margin-bottom: 12px; border: 1px solid rgba(255, 255, 255, 0.2);">
                                    <span style="color: #ffffff; font-size: 13px; font-weight: 600; letter-spacing: 0.5px;">SMARTVISION AI PORTAL</span>
                                </div>
                                <h1 style="color: #ffffff; font-size: 24px; font-weight: 700; margin: 0; line-height: 1.3;">Email Verification</h1>
                                <p style="color: #c7d2fe; font-size: 14px; margin: 6px 0 0 0;">Complete your registration securely</p>
                            </td>
                        </tr>

                        <!-- Body Content -->
                        <tr>
                            <td style="padding: 32px 28px;">
                                <p style="font-size: 15px; color: #334155; line-height: 1.6; margin: 0 0 16px 0;">
                                    Hello,
                                </p>
                                <p style="font-size: 15px; color: #475569; line-height: 1.6; margin: 0 0 24px 0;">
                                    Thank you for registering with <strong>SmartVision Attendance Portal</strong>. Please use the 6-digit verification code below to verify your email address:
                                </p>

                                <!-- OTP Code Box -->
                                <div style="background: linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%); border: 2px dashed #6366f1; border-radius: 12px; padding: 22px; text-align: center; margin: 20px 0;">
                                    <div style="font-size: 12px; font-weight: 700; color: #4f46e5; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px;">
                                        Your 6-Digit OTP Code
                                    </div>
                                    <div style="font-family: 'Courier New', Courier, monospace; font-size: 36px; font-weight: 800; color: #1e1b4b; letter-spacing: 8px; margin: 4px 0;">
                                        {otp_code}
                                    </div>
                                    <div style="font-size: 12px; color: #64748b; margin-top: 8px;">
                                        ⏱ Valid for <strong>15 minutes</strong>
                                    </div>
                                </div>

                                {verify_button_html}

                                <div style="margin-top: 28px; padding-top: 20px; border-top: 1px solid #e2e8f0;">
                                    <p style="font-size: 13px; color: #64748b; line-height: 1.5; margin: 0;">
                                        🔒 <strong>Security Tip:</strong> Never share your verification code with anyone. SmartVision administrators will never ask for your OTP.
                                    </p>
                                </div>
                            </td>
                        </tr>

                        <!-- Footer -->
                        <tr>
                            <td style="background-color: #f8fafc; padding: 20px 24px; text-align: center; border-top: 1px solid #e2e8f0;">
                                <p style="font-size: 12px; color: #94a3b8; margin: 0; line-height: 1.5;">
                                    This is an automated system notification from SmartVision Attendance Management System.
                                </p>
                            </td>
                        </tr>

                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    return send_email(to_email, subject, plain_text, html_content, sync=sync)
