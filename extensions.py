# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - EXTENSIONS & TIME UTILITIES
# ==============================================================================
# Description: Global extension instances for SQLAlchemy ORM, Flask-Login Manager,
#              Authlib OAuth Client, and Indian Standard Time (IST) localization helpers.
# ==============================================================================

from datetime import datetime, timedelta, timezone
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from authlib.integrations.flask_client import OAuth

# ==============================================================================
# 1. FLASK EXTENSION SINGLETON INSTANCES
# ==============================================================================
db = SQLAlchemy()
login_manager = LoginManager()
oauth = OAuth()

# Flask-Login Configuration
login_manager.login_view = 'auth.login'
login_manager.login_message_category = 'info'

# ==============================================================================
# 2. INDIAN STANDARD TIME (IST - UTC+05:30) TIMEZONE HELPERS
# ==============================================================================
IST = timezone(timedelta(hours=5, minutes=30))

def get_current_date():
    """Returns today's date adjusted accurately to Indian Standard Time (IST)."""
    return datetime.now(IST).date()

def get_current_time_str():
    """Returns current time string in 12-hour format with AM/PM (e.g. '10:30 AM')."""
    return datetime.now(IST).strftime('%I:%M %p')

def get_current_24h_time_str():
    """Returns current time string in 24-hour format (e.g. '14:30') for slot comparisons."""
    return datetime.now(IST).strftime('%H:%M')

def get_current_datetime_str():
    """Returns current timestamp string formatted as 'YYYYMMDDHHMMSS'."""
    return datetime.now(IST).strftime('%Y%m%d%H%M%S')