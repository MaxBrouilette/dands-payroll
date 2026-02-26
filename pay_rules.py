"""
Alberta Pay Rules
=================
Statutory holiday calendar (2024-2027), overtime rates,
stat holiday pay, and vacation pay calculations.

Sources:
  - https://www.alberta.ca/overtime-hours-overtime-pay
  - https://www.alberta.ca/general-holidays
  - https://www.alberta.ca/vacation-pay
"""

from datetime import date

# ════════════════════════════════════════════════════════════
#  ALBERTA STATUTORY HOLIDAYS — 9 mandatory general holidays
# ════════════════════════════════════════════════════════════

ALBERTA_STATS = {
    2024: [
        (date(2024, 1, 1),  "New Year's Day"),
        (date(2024, 2, 19), "Family Day"),
        (date(2024, 3, 29), "Good Friday"),
        (date(2024, 5, 20), "Victoria Day"),
        (date(2024, 7, 1),  "Canada Day"),
        (date(2024, 9, 2),  "Labour Day"),
        (date(2024, 10, 14),"Thanksgiving"),
        (date(2024, 11, 11),"Remembrance Day"),
        (date(2024, 12, 25),"Christmas Day"),
    ],
    2025: [
        (date(2025, 1, 1),  "New Year's Day"),
        (date(2025, 2, 17), "Family Day"),
        (date(2025, 4, 18), "Good Friday"),
        (date(2025, 5, 19), "Victoria Day"),
        (date(2025, 7, 1),  "Canada Day"),
        (date(2025, 9, 1),  "Labour Day"),
        (date(2025, 10, 13),"Thanksgiving"),
        (date(2025, 11, 11),"Remembrance Day"),
        (date(2025, 12, 25),"Christmas Day"),
    ],
    2026: [
        (date(2026, 1, 1),  "New Year's Day"),
        (date(2026, 2, 16), "Family Day"),
        (date(2026, 4, 3),  "Good Friday"),
        (date(2026, 5, 18), "Victoria Day"),
        (date(2026, 7, 1),  "Canada Day"),
        (date(2026, 9, 7),  "Labour Day"),
        (date(2026, 10, 12),"Thanksgiving"),
        (date(2026, 11, 11),"Remembrance Day"),
        (date(2026, 12, 25),"Christmas Day"),
    ],
    2027: [
        (date(2027, 1, 1),  "New Year's Day"),
        (date(2027, 2, 15), "Family Day"),
        (date(2027, 3, 26), "Good Friday"),
        (date(2027, 5, 24), "Victoria Day"),
        (date(2027, 7, 1),  "Canada Day"),
        (date(2027, 9, 6),  "Labour Day"),
        (date(2027, 10, 11),"Thanksgiving"),
        (date(2027, 11, 11),"Remembrance Day"),
        (date(2027, 12, 25),"Christmas Day"),
    ],
}

# Company policy: Christmas and New Year's are paid days off (not worked).
# All other stats are mandatory work days at 1.5x.
PAID_DAY_OFF_STATS = {"New Year's Day", "Christmas Day"}


# ════════════════════════════════════════════════════════════
#  RATE CALCULATIONS
# ════════════════════════════════════════════════════════════

def overtime_rate(regular_rate):
    """Alberta OT rate = 1.5x regular rate."""
    return round(regular_rate * 1.5, 2)


def stat_rate(regular_rate):
    """Stat holiday work rate = 1.5x regular rate (Alberta Option 1)."""
    return round(regular_rate * 1.5, 2)


def vacation_pay_rate(years_of_service):
    """Alberta vacation pay rate: 4% (<5 yrs), 6% (5+ yrs)."""
    return 0.06 if years_of_service >= 5 else 0.04


def calc_vacation_pay(total_wages, years_of_service):
    """Calculate vacation pay on total wages earned."""
    return round(total_wages * vacation_pay_rate(years_of_service), 2)


# ════════════════════════════════════════════════════════════
#  ELIGIBILITY CHECKS
# ════════════════════════════════════════════════════════════

def is_vacation_eligible(start_date, as_of=None, end_date=None):
    """Alberta: vacation pay applies after 1 year of employment.
    Uses anniversary-date logic to handle leap years correctly.
    Returns False if employee is terminated (as_of > end_date)."""
    if not start_date:
        return False
    as_of = as_of or date.today()
    if end_date and as_of > end_date:
        return False
    try:
        first_anniversary = date(start_date.year + 1,
                                 start_date.month, start_date.day)
    except ValueError:
        # Feb 29 start → Feb 28 in non-leap year
        first_anniversary = date(start_date.year + 1,
                                 start_date.month, 28)
    return as_of >= first_anniversary


def is_stat_eligible(start_date, as_of=None, days_per_week=5, end_date=None):
    """Alberta: stat holiday pay requires 30 workdays of employment.
    Returns False if employee is terminated (as_of > end_date).

    Calendar-day threshold varies by schedule:
      5 days/week → 30 workdays = 6 weeks   = 42 calendar days
      4 days/week → 30 workdays = 7.5 weeks = 53 calendar days
    """
    if not start_date:
        return False
    as_of = as_of or date.today()
    if end_date and as_of > end_date:
        return False
    if days_per_week <= 0:
        days_per_week = 5
    weeks_needed = 30 / days_per_week
    calendar_days = int(weeks_needed * 7) + (1 if weeks_needed % 1 else 0)
    return (as_of - start_date).days >= calendar_days


def years_of_service(start_date, as_of=None, end_date=None):
    """Calculate full years of employment using anniversary dates.
    If end_date is set, caps calculation at the termination date."""
    if not start_date:
        return 0
    as_of = as_of or date.today()
    if end_date:
        as_of = min(as_of, end_date)
    years = as_of.year - start_date.year
    try:
        anniversary = date(as_of.year, start_date.month, start_date.day)
    except ValueError:
        # Feb 29 → Feb 28 in non-leap year
        anniversary = date(as_of.year, start_date.month, 28)
    if as_of < anniversary:
        years -= 1
    return max(years, 0)


# ════════════════════════════════════════════════════════════
#  STAT HOLIDAY LOOKUP
# ════════════════════════════════════════════════════════════

def get_stats_in_period(period_from, period_to):
    """Return stat holidays falling within a date range.

    Args:
        period_from: date object (start of pay period)
        period_to:   date object (end of pay period)

    Returns:
        list of (date, name, is_paid_day_off) tuples
    """
    results = []
    # Check all years that could overlap the period
    for year in range(period_from.year, period_to.year + 1):
        if year not in ALBERTA_STATS:
            if year > max(ALBERTA_STATS.keys()):
                import warnings
                warnings.warn(
                    f"Stat holiday data not available for {year}. "
                    f"Update ALBERTA_STATS in pay_rules.py.")
            continue
        for stat_date, stat_name in ALBERTA_STATS[year]:
            if period_from <= stat_date <= period_to:
                is_day_off = stat_name in PAID_DAY_OFF_STATS
                results.append((stat_date, stat_name, is_day_off))
    return results


def get_all_stats(year):
    """Return all 9 Alberta stat holidays for a given year."""
    return ALBERTA_STATS.get(year, [])


# ════════════════════════════════════════════════════════════
#  OVERTIME THRESHOLD INFO
# ════════════════════════════════════════════════════════════

# Alberta standard OT thresholds (for reference / display)
OT_DAILY_THRESHOLD = 8      # hours per day
OT_WEEKLY_THRESHOLD = 44    # hours per week
OT_MULTIPLIER = 1.5


# ════════════════════════════════════════════════════════════
#  SCHEDULE TYPE DEFINITIONS (Averaging Agreements)
# ════════════════════════════════════════════════════════════

SCHEDULE_TYPES = {
    "standard": {
        "label":       "Standard (5\u00d78)",
        "short_label": "5\u00d78",
        "description": "Standard 40-hour work week across 5 days.",
        "days_per_week":       5,
        "hours_per_day":       8,
        "hours_per_week":      40,
        "ot_daily_threshold":  8,
        "ot_weekly_threshold": 44,
        "ot_multiplier":       1.5,
        "is_averaging":        False,
        "summary": (
            "Overtime after 8 hrs/day or 44 hrs/week at 1.5\u00d7 rate. "
            "Standard Alberta Employment Standards rules apply."
        ),
    },
    "5x9_design": {
        "label":       "5\u00d79",
        "short_label": "5\u00d79",
        "description": (
            "Averaging agreement. "
            "5 days \u00d7 9 hours = 45 hours/week."
        ),
        "days_per_week":       5,
        "hours_per_day":       9,
        "hours_per_week":      45,
        "ot_daily_threshold":  9,
        "ot_weekly_threshold": 44,
        "ot_multiplier":       1.5,
        "is_averaging":        True,
        "guaranteed_ot_per_week": 1.0,
        "summary": (
            "Averaging agreement: 5 days \u00d7 9 hrs = 45 hrs/week. "
            "First 44 hrs are straight time if no day exceeds 9 hrs. "
            "1 guaranteed OT hour per week (45 \u2212 44 = 1). "
            "Hours beyond 44/week or beyond 9/day = overtime at 1.5\u00d7."
        ),
    },
    "4x10_mon_off": {
        "label":       "4\u00d710 + 4 (off Mon)",
        "short_label": "4\u00d710 T-F",
        "description": (
            "Averaging agreement. "
            "4 days \u00d7 10 hours = 40 hours/week. "
            "Tue, Wed, Thu, Fri (off Monday)."
        ),
        "days_per_week":       4,
        "hours_per_day":       10,
        "hours_per_week":      40,
        "ot_daily_threshold":  10,
        "ot_weekly_threshold": 44,
        "ot_multiplier":       1.5,
        "is_averaging":        True,
        "callback_max_hours":  4,
        "callback_notice_days": 3,
        "summary": (
            "Averaging agreement: 4 days \u00d7 10 hrs = 40 hrs/week "
            "(Tue\u2013Fri \u2014 off Monday). "
            "Can be called in on day off with 3 days notice "
            "for up to 4 additional hours without overtime. "
            "Max 44 hrs/week before OT. "
            "Hours beyond 44/week or beyond 10/day = overtime at 1.5\u00d7."
        ),
    },
    "4x10_wed_off": {
        "label":       "4\u00d710 + 4 (off Wed)",
        "short_label": "4\u00d710 MTThF",
        "description": (
            "Averaging agreement. "
            "4 days \u00d7 10 hours = 40 hours/week. "
            "Mon, Tue, Thu, Fri (off Wednesday)."
        ),
        "days_per_week":       4,
        "hours_per_day":       10,
        "hours_per_week":      40,
        "ot_daily_threshold":  10,
        "ot_weekly_threshold": 44,
        "ot_multiplier":       1.5,
        "is_averaging":        True,
        "callback_max_hours":  4,
        "callback_notice_days": 3,
        "summary": (
            "Averaging agreement: 4 days \u00d7 10 hrs = 40 hrs/week "
            "(Mon, Tue, Thu, Fri \u2014 off Wednesday). "
            "Can be called in on day off with 3 days notice "
            "for up to 4 additional hours without overtime. "
            "Max 44 hrs/week before OT. "
            "Hours beyond 44/week or beyond 10/day = overtime at 1.5\u00d7."
        ),
    },
    "4x10_fri_off": {
        "label":       "4\u00d710 + 4 (off Fri)",
        "short_label": "4\u00d710 M-Th",
        "description": (
            "Averaging agreement. "
            "4 days \u00d7 10 hours = 40 hours/week. "
            "Mon, Tue, Wed, Thu (off Friday)."
        ),
        "days_per_week":       4,
        "hours_per_day":       10,
        "hours_per_week":      40,
        "ot_daily_threshold":  10,
        "ot_weekly_threshold": 44,
        "ot_multiplier":       1.5,
        "is_averaging":        True,
        "callback_max_hours":  4,
        "callback_notice_days": 3,
        "summary": (
            "Averaging agreement: 4 days \u00d7 10 hrs = 40 hrs/week "
            "(Mon\u2013Thu \u2014 off Friday). "
            "Can be called in on day off with 3 days notice "
            "for up to 4 additional hours without overtime. "
            "Max 44 hrs/week before OT. "
            "Hours beyond 44/week or beyond 10/day = overtime at 1.5\u00d7."
        ),
    },
}

SCHEDULE_TYPE_KEYS = [
    "standard", "5x9_design",
    "4x10_mon_off", "4x10_wed_off", "4x10_fri_off",
]
SCHEDULE_TYPE_LABELS = [SCHEDULE_TYPES[k]["label"] for k in SCHEDULE_TYPE_KEYS]
DEFAULT_SCHEDULE_TYPE = "standard"


def get_schedule(schedule_type_key):
    """Return schedule definition for a given key. Falls back to standard."""
    if schedule_type_key and schedule_type_key in SCHEDULE_TYPES:
        return SCHEDULE_TYPES[schedule_type_key]
    return SCHEDULE_TYPES[DEFAULT_SCHEDULE_TYPE]
