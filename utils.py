"""
Shared Utilities
================
Common helpers used across multiple payroll modules.
Import from here rather than duplicating in each file.
"""

import re
from datetime import date, datetime


def parse_payroll_date(date_str):
    """Parse any payroll date string into a date object.

    Handles both formats used throughout the system:
    - "February 15th, 2026"  — ordinal string produced by format_date()
    - "2026-02-15"           — ISO format stored in profiles / remittances

    Returns:
        date object, or None if the string is empty or unparseable.
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    try:
        # ISO fast path: YYYY-MM-DD (also handles YYYY-MM-DDThh:mm:ss)
        if re.match(r'^\d{4}-\d{2}-\d{2}', s):
            return date.fromisoformat(s[:10])
        # Ordinal format: strip st/nd/rd/th then parse "%B %d, %Y"
        clean = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', s)
        return datetime.strptime(clean.strip(), "%B %d, %Y").date()
    except (ValueError, AttributeError):
        return None
