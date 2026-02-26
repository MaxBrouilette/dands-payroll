"""
Payroll Audit Tracker
=====================
Compares actual payroll deductions against CRA T4127 formula estimates.

Two data sources:
  1. AUTOMATIC — pulls from remittance data (every payroll in the DB)
  2. MANUAL    — stored in audit_manual table (for pre-system historical payrolls)

Both are merged into a unified audit view with variance analysis.
"""

import os
from tax_calculator import estimate_deductions
import db


# ════════════════════════════════════════════════════════════
#  AUTOMATIC AUDIT — from remittance data
# ════════════════════════════════════════════════════════════

def _build_audit_from_remittance(entry, pay_year, pay_month):
    """Convert a remittance entry into an audit entry with T4127 comparison."""
    gross   = entry["gross"]
    formula = estimate_deductions(gross, pay_periods=24, tax_year=pay_year, pay_month=pay_month)

    actual = {
        "cpp":      round(entry["cpp_employee"] or 0, 2),
        "cpp2":     round(entry.get("cpp2_employee") or 0, 2),
        "ei":       round(entry["ei_employee"] or 0, 2),
        "fed_tax":  round(entry["fed_tax"] or 0, 2),
        "prov_tax": round(entry["prov_tax"] or 0, 2),
    }

    variance = {k: round(actual[k] - formula[k], 2) for k in actual}

    # PDF path is a Storage path on cloud — treat as present if non-empty
    pdf_path    = entry.get("pdf_path")
    pdf_missing = False  # can't stat Storage paths; trust DB integrity

    return {
        "employee":     entry["employee"],
        "payment_date": entry["payment_date"],
        "pay_year":     pay_year,
        "pay_month":    pay_month,
        "period":       entry.get("period", ""),
        "gross":        gross,
        "actual":       actual,
        "formula":      formula,
        "variance":     variance,
        "source":       "auto",
        "pdf_path":     pdf_path,
        "pdf_missing":  pdf_missing,
    }


def get_auto_audit_entries(employee=None):
    """Build audit entries from ALL remittance data in the DB."""
    entries = []
    for year, month in db.available_months()[0]:
        for r in db.get_remittances(year, month):
            if employee and r["employee"] != employee:
                continue
            entries.append(_build_audit_from_remittance(r, year, month))
    entries.sort(key=lambda e: (e["pay_year"], e["pay_month"], e["employee"]))
    return entries


# ════════════════════════════════════════════════════════════
#  MANUAL AUDIT — for pre-system historical data
# ════════════════════════════════════════════════════════════

def log_manual_entry(employee, payment_date_str, pay_year, pay_month,
                     period_from, period_to, hours, gross,
                     actual_cpp, actual_ei, actual_fed, actual_prov,
                     actual_cpp2=0):
    """Add a pre-system payroll entry with actual vs formula comparison."""
    formula = estimate_deductions(gross, pay_periods=24, tax_year=pay_year, pay_month=pay_month)

    actual = {
        "cpp":      round(actual_cpp, 2),
        "cpp2":     round(actual_cpp2, 2),
        "ei":       round(actual_ei, 2),
        "fed_tax":  round(actual_fed, 2),
        "prov_tax": round(actual_prov, 2),
    }
    variance = {k: round(actual[k] - formula[k], 2) for k in actual}

    entry = {
        "employee":     employee,
        "payment_date": payment_date_str,
        "pay_year":     pay_year,
        "pay_month":    pay_month,
        "period_from":  period_from,
        "period_to":    period_to,
        "hours":        hours,
        "gross":        gross,
        "actual":       actual,
        "formula":      formula,
        "variance":     variance,
        "source":       "manual",
    }

    return db.log_audit_entry(entry)


def get_manual_audit_entries(employee=None):
    """Load all manually entered audit entries."""
    return db.get_manual_audit_entries(employee=employee)


def delete_manual_entry(year, index):
    """Remove a manual entry by positional index within the year.

    Soft-deletes to trash. Returns True if deleted.
    """
    return db.delete_audit_entry(year, index)


def get_manual_entries_indexed(year, employee=None):
    """Return (file_index, entry) pairs for a year's manual entries."""
    return db.get_manual_entries_indexed(year, employee=employee)


# ════════════════════════════════════════════════════════════
#  COMBINED AUDIT — merges both sources
# ════════════════════════════════════════════════════════════

def get_all_audit_entries(employee=None):
    """Get all audit entries from both auto (remittance) and manual sources."""
    auto   = get_auto_audit_entries(employee)
    manual = get_manual_audit_entries(employee)

    auto_keys = {(e["employee"], e["payment_date"]) for e in auto}
    for e in manual:
        key = (e["employee"], e["payment_date"])
        if key in auto_keys:
            e["duplicate_warning"] = (
                f"Manual entry duplicates an auto-generated entry for "
                f"{e['employee']} on {e['payment_date']}")

    combined = auto + manual
    combined.sort(key=lambda e: (e["pay_year"], e["pay_month"], e["employee"]))
    return combined


def get_audit_entries(year, employee=None):
    """Get all audit entries for a specific year."""
    all_entries = get_all_audit_entries(employee=employee)
    return [e for e in all_entries if e["pay_year"] == year]


def available_audit_years():
    """Return sorted list of years that have any audit data (auto or manual)."""
    entries = get_all_audit_entries()
    return sorted(set(e["pay_year"] for e in entries))


def get_audit_summary(entries):
    """Compute totals: actual vs formula, with variance. Returns None if no entries."""
    if not entries:
        return None

    keys = ["cpp", "cpp2", "ei", "fed_tax", "prov_tax"]
    actual_totals   = {k: 0.0 for k in keys}
    formula_totals  = {k: 0.0 for k in keys}
    variance_totals = {k: 0.0 for k in keys}
    total_gross = 0.0
    employees = set()

    for e in entries:
        total_gross += e["gross"]
        employees.add(e["employee"])
        for k in keys:
            actual_totals[k]   = round(actual_totals[k]   + e["actual"].get(k, 0),   2)
            formula_totals[k]  = round(formula_totals[k]  + e["formula"].get(k, 0),  2)
            variance_totals[k] = round(variance_totals[k] + e["variance"].get(k, 0), 2)

    actual_total  = round(sum(actual_totals.values()), 2)
    formula_total = round(sum(formula_totals.values()), 2)

    flags = []
    for k in keys:
        v = variance_totals[k]
        label = {"cpp": "CPP", "cpp2": "CPP2", "ei": "EI",
                 "fed_tax": "Federal Tax", "prov_tax": "Provincial Tax"}[k]
        sign = "+" if v >= 0 else "-"
        fv = f"{sign}${abs(v):.2f}"
        if abs(v) > 200:
            flags.append(f"{label}: {fv} — action needed")
        elif abs(v) > 50:
            flags.append(f"{label}: {fv} — review recommended")

    return {
        "entry_count":     len(entries),
        "employees":       sorted(employees),
        "total_gross":     round(total_gross, 2),
        "actual_totals":   actual_totals,
        "formula_totals":  formula_totals,
        "variance_totals": variance_totals,
        "actual_total":    actual_total,
        "formula_total":   formula_total,
        "variance_total":  round(actual_total - formula_total, 2),
        "flags":           flags,
    }
