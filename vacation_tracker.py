"""
Vacation Pay Accrual Tracker
=============================
Calculates vacation pay accrual based on Alberta Employment Standards:
  - 4% of total gross wages for years 1-5 of employment
  - 6% of total gross wages after 5 years

Accrual is computed from remittance data in the DB, with manual gross
overrides for historical periods without system data.
"""

from datetime import date, timedelta
from utils import parse_payroll_date
import db


def _safe_anniversary(base_date, year_offset):
    """Build anniversary date, handling Feb 29 → Feb 28 in non-leap years."""
    try:
        return date(base_date.year + year_offset, base_date.month, base_date.day)
    except ValueError:
        return date(base_date.year + year_offset, base_date.month, 28)


def get_vacation_years(start_date, as_of=None, end_date=None):
    """Generate vacation year periods from start_date to now.

    Returns list of dicts with year_num, from, to, rate, status, label.
    """
    as_of  = as_of or date.today()
    cutoff = min(as_of, end_date) if end_date else as_of
    years  = []
    year_num = 1

    while True:
        yr_from = _safe_anniversary(start_date, year_num - 1)
        yr_to   = _safe_anniversary(start_date, year_num) - timedelta(days=1)

        if yr_from > cutoff:
            break

        if end_date and yr_to > end_date:
            yr_to = end_date

        rate = 0.04 if year_num <= 5 else 0.06

        if end_date and yr_to <= end_date and yr_to < as_of:
            status = "complete"
        elif end_date and yr_to == end_date:
            status = "final"
        elif yr_to < as_of:
            status = "complete"
        elif yr_from <= as_of <= yr_to:
            status = "current"
        else:
            status = "future"

        years.append({
            "year_num": year_num,
            "from":     yr_from,
            "to":       yr_to,
            "rate":     rate,
            "status":   status,
            "label":    f"Year {year_num} ({yr_from.isoformat()} to {yr_to.isoformat()})",
        })

        if end_date and yr_to >= end_date:
            break
        year_num += 1
        if year_num > 50:
            break

    return years


def _gross_from_remittances(employee, period_from, period_to):
    """Sum gross pay from DB remittance entries within a date range.

    Returns (gross_total, entry_count).
    """
    total = 0.0
    count = 0
    for e in db.get_all_remittances_for_employee(employee):
        pay_date = parse_payroll_date(e.get("payment_date", ""))
        if pay_date is None:
            # Fallback: use pay_year/pay_month mid-month
            yr = e.get("pay_year")
            mo = e.get("pay_month")
            if yr and mo:
                pay_date = date(int(yr), int(mo), 15)
        if pay_date and period_from <= pay_date <= period_to:
            total += e.get("gross") or 0
            count += 1
    return round(total, 2), count


def get_vacation_accrual(employee):
    """Calculate full vacation pay accrual status for an employee.

    Returns dict with vacation_years, total_accrued, total_paid, balance, payouts.
    Returns None if employee has no start_date.
    """
    emp = db.get_employee(employee)
    if not emp:
        return None
    start_str = emp.get("start_date")
    if not start_str:
        return None

    start_date = date.fromisoformat(start_str)
    end_str    = emp.get("end_date")
    end_date   = date.fromisoformat(end_str) if end_str else None
    today      = date.today()

    vac_years       = get_vacation_years(start_date, as_of=today, end_date=end_date)
    gross_overrides = emp.get("vacation_gross_overrides", {})
    payouts         = emp.get("vacation_payouts", [])

    total_accrued = 0.0
    total_paid    = 0.0
    year_details  = []

    for vy in vac_years:
        override_key = f"{vy['from'].year}-{vy['to'].year}"
        if override_key in gross_overrides:
            gross       = gross_overrides[override_key]
            data_source = "manual"
            entry_count = 0
        else:
            gross, entry_count = _gross_from_remittances(employee, vy["from"], vy["to"])
            data_source = "remittance" if entry_count > 0 else "none"

        accrued = round(gross * vy["rate"], 2)
        total_accrued += accrued

        year_paid = 0.0
        for p in payouts:
            if p.get("year_ending", "") == vy["to"].isoformat():
                year_paid += p.get("amount", 0) or 0
        year_paid = round(year_paid, 2)
        total_paid += year_paid

        year_details.append({
            **vy,
            "gross":       gross,
            "accrued":     accrued,
            "paid":        year_paid,
            "balance":     round(accrued - year_paid, 2),
            "data_source": data_source,
            "entry_count": entry_count,
        })

    return {
        "start_date":    start_date,
        "vacation_years": year_details,
        "total_accrued": round(total_accrued, 2),
        "total_paid":    round(total_paid, 2),
        "balance":       round(total_accrued - total_paid, 2),
        "payouts":       payouts,
    }


def record_vacation_payout(employee, amount, year_ending, payout_date=None,
                            method="Cheque", note=""):
    """Record a vacation pay payout for an employee."""
    payout = {
        "date":        (payout_date or date.today()).isoformat(),
        "amount":      round(amount, 2),
        "year_ending": year_ending if isinstance(year_ending, str)
                       else year_ending.isoformat(),
        "method":      method,
        "note":        note,
    }
    db.add_vacation_payout(employee, payout)
    return payout


def delete_vacation_payout(employee, index):
    """Delete a vacation payout by positional index.

    Soft-deletes to trash. Returns removed entry or None.
    """
    return db.delete_vacation_payout(employee, index)


def set_gross_override(employee, year_from, year_to, gross_amount):
    """Set a manual gross wage override for a vacation year."""
    db.set_vacation_gross_override(employee, f"{year_from}-{year_to}", gross_amount)
