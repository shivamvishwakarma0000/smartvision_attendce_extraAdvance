# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - APPLICATION CONFIGURATION
# ==============================================================================
# Description: Central application environment settings, database URI parameters,
#              Google OAuth 2.0 API credentials, and SMTP email server properties.
# ==============================================================================

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Allow HTTP transport for local OAuth redirect URI testing
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# ==============================================================================
# MAIN APPLICATION CONFIGURATION CLASS
# ==============================================================================
class Config:
    """
    Configuration settings for the Flask application.
    Loaded dynamically from local .env or production environment variables.
    """
    # --------------------------------------------------------------------------
    # 1. CORE APPLICATION SECURITY KEY
    # --------------------------------------------------------------------------
    SECRET_KEY = os.environ.get('SECRET_KEY', 'smartvision-dev-secret-key-change-in-production')

    # --------------------------------------------------------------------------
    # 2. DATABASE ORM CONNECTION (SQLAlchemy)
    # --------------------------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///smartvision.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --------------------------------------------------------------------------
    # 3. GOOGLE OAUTH 2.0 SSO CREDENTIALS
    # --------------------------------------------------------------------------
    GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
    GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')

    # --------------------------------------------------------------------------
    # 4. SMTP MAIL SERVER & NOTIFICATION CREDENTIALS
    # --------------------------------------------------------------------------
    SMTP_HOST = os.environ.get('SMTP_HOST', '')
    SMTP_PORT = int(os.environ.get('SMTP_PORT', 587)) if os.environ.get('SMTP_PORT') and os.environ.get('SMTP_PORT').isdigit() else 587
    SMTP_USERNAME = os.environ.get('SMTP_USERNAME', '')
    SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
    SMTP_USE_TLS = os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true'
    SMTP_SENDER_EMAIL = os.environ.get('SMTP_SENDER_EMAIL', 'noreply@smartvision.com')