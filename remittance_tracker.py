"""
CRA Remittance Tracker
======================
Logs payroll deductions and computes monthly remittance summaries.

All data is stored in Supabase (remittances + remittance_status tables).
Falls back to local JSON files when Supabase is not configured.
"""

from datetime import date
from tax_calculator import employer_portions
import db


def log_payroll(payment_date_str, employee, period_from, period_to,
                gross, cpp, ei, fed_tax, prov_tax,
                pay_year, pay_month, pdf_path=None,
                force_overwrite=False, cpp2=0, hours=None,
                ei_insurable_gross=None, cpp_pensionable_gross=None):
    """Append a payroll entry to the remittance log.

    Returns the new entry dict, or {"conflict": True, ...} if a duplicate
    exists and force_overwrite is False.
    """
    emp = employer_portions(cpp, ei, cpp2)

    total_remittance = round(
        cpp + emp["cpp_employer"] +
        cpp2 + emp["cpp2_employer"] +
        ei + emp["ei_employer"] +
        fed_tax + prov_tax, 2
    )

    entry = {
        "payment_date":          payment_date_str,
        "employee":              employee,
        "period":                f"{period_from} – {period_to}",
        "gross":                 gross,
        "cpp_employee":          cpp,
        "cpp_employer":          emp["cpp_employer"],
        "cpp2_employee":         cpp2,
        "cpp2_employer":         emp["cpp2_employer"],
        "ei_employee":           ei,
        "ei_employer":           emp["ei_employer"],
        "fed_tax":               fed_tax,
        "prov_tax":              prov_tax,
        "total_remittance":      total_remittance,
        "pdf_path":              pdf_path,
        "hours":                 hours,
        "ei_insurable_gross":    ei_insurable_gross if ei_insurable_gross is not None else gross,
        "cpp_pensionable_gross": cpp_pensionable_gross if cpp_pensionable_gross is not None else gross,
        "pay_year":              pay_year,
        "pay_month":             pay_month,
    }

    # Check for duplicate
    existing = db.find_remittance(employee, payment_date_str, pay_year, pay_month)
    if existing and not force_overwrite:
        return {"conflict": True, "existing": existing, "new": entry}

    if force_overwrite and existing:
        db.soft_delete("remittances", employee,
                       {"year": pay_year, "month": pay_month,
                        "payment_date": payment_date_str},
                       existing)
        db.soft_delete_remittance(existing["id"])

    inserted = db.log_remittance(entry)
    return inserted if inserted else entry


def monthly_summary(year, month):
    """Compute the remittance summary for a given month.

    Returns dict with totals, entry list, and due date.
    Returns None if no entries exist for that month.
    """
    entries = db.get_remittances(year, month)
    if not entries:
        return None

    totals = {
        "cpp_employee": 0, "cpp_employer":  0,
        "cpp2_employee": 0, "cpp2_employer": 0,
        "ei_employee":  0, "ei_employer":   0,
        "fed_tax":      0, "prov_tax":      0,
        "gross":        0, "total_remittance": 0,
    }
    for e in entries:
        for key in totals:
            totals[key] = round(totals[key] + (e.get(key) or 0), 2)

    if month == 12:
        due = date(year + 1, 1, 15)
    else:
        due = date(year, month + 1, 15)

    return {
        "year":        year,
        "month":       month,
        "entries":     entries,
        "totals":      totals,
        "due_date":    due.strftime("%B %d, %Y"),
        "entry_count": len(entries),
    }


def available_months():
    """List all months that have remittance data, sorted.

    Returns ([(year, month), ...], [skipped]) — same signature as before.
    """
    return db.available_months()


def get_remittance_status(year, month):
    """Get remittance status for a given month."""
    return db.get_remittance_status(year, month)


def mark_remitted(year, month, remitted_date, confirmation="", notes=""):
    """Mark a month's remittance as sent to CRA."""
    status = {
        "remitted":      True,
        "remitted_date": remitted_date,
        "confirmation":  confirmation,
        "notes":         notes,
    }
    db.set_remittance_status(year, month, status)
    return status


def clear_remitted(year, month):
    """Clear remittance status for a month."""
    db.clear_remittance_status(year, month)


def ytd_summary(year):
    """Compute year-to-date remittance totals across all months."""
    ytd = {
        "cpp_employee": 0, "cpp_employer":  0,
        "cpp2_employee": 0, "cpp2_employer": 0,
        "ei_employee":  0, "ei_employer":   0,
        "fed_tax":      0, "prov_tax":      0,
        "gross":        0, "total_remittance": 0,
    }
    months_with_data = []

    for yr, mo in available_months()[0]:
        if yr != year:
            continue
        ms = monthly_summary(yr, mo)
        if ms is None:
            continue
        months_with_data.append(mo)
        for key in ytd:
            ytd[key] = round(ytd[key] + ms["totals"].get(key, 0), 2)

    if not months_with_data:
        return None

    return {
        "year":   year,
        "months": months_with_data,
        "totals": ytd,
    }
