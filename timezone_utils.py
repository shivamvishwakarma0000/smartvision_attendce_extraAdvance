# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - TIMEZONE & LOCALIZATION UTILITY
# ==============================================================================
# Description: Precise Indian Standard Time (IST - UTC+05:30) helper functions
#              used for logging, student attendance timestamps, and PDF generation.
# ==============================================================================

import datetime

# ==============================================================================
# 1. INDIAN STANDARD TIMEZONE DEFINITIONS
# ==============================================================================
IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# ==============================================================================
# 2. CURRENT TIME & FORMATTING HELPERS
# ==============================================================================
def get_ist_now():
    """Returns current datetime in Indian Standard Time (UTC+5:30)."""
    return datetime.datetime.now(IST_TZ)

def format_ist_datetime(dt=None, fmt="%b %d, %Y %I:%M %p IST"):
    """
    Formats a datetime object or current time into a clean IST string representation.
    
    Parameters:
        dt (datetime, optional): Input datetime object. If None, current time is used.
        fmt (str): Date/time format specification string.
        
    Returns:
        str: Formatted datetime string with IST indicator.
    """
    if dt is None:
        dt = get_ist_now()
    elif dt.tzinfo is None:
        # Assume naive UTC or local, convert to IST
        dt = dt.replace(tzinfo=datetime.timezone.utc).astimezone(IST_TZ)
    else:
        dt = dt.astimezone(IST_TZ)
    return dt.strftime(fmt)
