"""
Decals and Signs — Payroll Generator UI
"""

import os
import json
import glob
import hashlib
import tempfile
import calendar
import streamlit as st
import pandas as pd
import re
import fitz
from datetime import date, datetime, timedelta
import db as _db
import storage_client as _sc
from auth import require_auth
from generate_payroll import generate_payroll
from tax_calculator import (estimate_deductions, employer_portions,
                            available_tax_years, RATES, EI_EMPLOYER_MULTIPLE)
from remittance_tracker import (log_payroll, monthly_summary,
                                available_months,
                                get_remittance_status, mark_remitted,
                                clear_remitted, ytd_summary)
from audit_tracker import (get_all_audit_entries, get_audit_summary,
                           log_manual_entry, delete_manual_entry,
                           get_manual_entries_indexed)
from resolution_tracker import (get_resolution_for_period, log_resolution,
                                save_receipt, get_receipt_bytes)
from t4_generator import (aggregate_t4_data, generate_t4_pdf,
                          aggregate_t4sum_data, generate_t4sum_pdf)
from pay_rules import (overtime_rate, stat_rate, get_stats_in_period,
                       is_vacation_eligible, is_stat_eligible,
                       vacation_pay_rate, years_of_service,
                       SCHEDULE_TYPES, SCHEDULE_TYPE_KEYS,
                       DEFAULT_SCHEDULE_TYPE, get_schedule,
                       PAID_DAY_OFF_STATS, get_all_stats)
from generate_agreements import generate_averaging_agreement
from generate_letters import (generate_verification_letter,
                               generate_wage_change_letter,
                               generate_termination_letter)
from employer_config import load_employer, save_employer, get_theme_assets
from agreement_tracker import load_agreements, set_agreement
from vacation_tracker import (get_vacation_accrual, record_vacation_payout,
                              delete_vacation_payout, set_gross_override)
from roe_generator import (ROE_REASON_CODES, SEPARATION_CODES, LEAVE_CODES,
                           get_employee_remittance_entries,
                           calculate_insurable_hours, assemble_roe_data,
                           generate_roe_pdf, load_roe_history)
from utils import parse_payroll_date
from trash_tracker import (soft_delete, get_trash, restore_entry,
                           purge_expired, get_retention_days,
                           trash_summary, RETENTION_PRESETS)

# ── Config ───────────────────────────────────────────────────
st.set_page_config(
    page_title=f"{load_employer().get('short_name', 'Payroll')} Payroll",
    page_icon="💰", layout="wide")

if not require_auth():
    st.stop()

PAYROLL_DIR   = os.path.dirname(os.path.abspath(__file__))
EMPLOYEES_DIR = os.path.join(PAYROLL_DIR, "employees")
ASSETS_DIR    = os.path.join(PAYROLL_DIR, "assets")
os.makedirs(EMPLOYEES_DIR, exist_ok=True)

# Theme assets are resolved dynamically via get_theme_assets() at use sites

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

PROVINCES = [
    "Alberta", "British Columbia", "Manitoba", "New Brunswick",
    "Newfoundland and Labrador", "Northwest Territories", "Nova Scotia",
    "Nunavut", "Ontario", "Prince Edward Island", "Quebec",
    "Saskatchewan", "Yukon",
]


# ── Helpers ──────────────────────────────────────────────────

def ordinal(n):
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd','th'][min(n % 10, 4)]}"


def format_date(d):
    return f"{d.strftime('%B')} {ordinal(d.day)}, {d.year}"


_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _get_pdf_bytes(path):
    """Return bytes for a PDF path (Supabase Storage path or local file path)."""
    if not path:
        return None
    # Absolute path: try file directly
    if os.path.isabs(path):
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None
    # Looks like a Storage path (relative, starts with "employees/" or "t4-")
    if path.startswith("employees/") or path.startswith("t4-"):
        try:
            return _sc.download_pdf(_sc.PDF_BUCKET, path)
        except Exception:
            pass
    # Try local (relative to app directory)
    try:
        with open(os.path.join(_APP_DIR, path), "rb") as f:
            return f.read()
    except OSError:
        return None


def _pdf_exists(path):
    """Check whether a PDF path is accessible (Storage or local)."""
    if not path:
        return False
    if os.path.isabs(path):
        return os.path.isfile(path)
    if path.startswith("employees/") or path.startswith("t4-"):
        try:
            data = _sc.download_pdf(_sc.PDF_BUCKET, path)
            return bool(data)
        except Exception:
            pass
    return os.path.isfile(os.path.join(_APP_DIR, path))


def get_pay_period(year, month, cycle):
    prev_month = month - 1
    prev_year  = year
    if prev_month == 0:
        prev_month = 12
        prev_year  = year - 1
    prev_last_day = calendar.monthrange(prev_year, prev_month)[1]
    if cycle == "15th":
        return (date(prev_year, prev_month, prev_last_day - 3),
                date(year, month, 11),
                date(year, month, 15))
    else:
        return (date(prev_year, prev_month, 12),
                date(prev_year, prev_month, prev_last_day - 4),
                date(year, month, 1))


def fmt_phone(raw):
    """Format phone: (780) 555-1234"""
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return raw  # return as-is if not standard


def fmt_sin(raw):
    """Format SIN: 123 456 789"""
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) == 9:
        return f"{digits[:3]} {digits[3:6]} {digits[6:]}"
    return raw


def fmt_postal(raw):
    """Format postal code: T5Y 1Z4"""
    clean = str(raw).upper().replace(" ", "").replace("-", "")
    if len(clean) == 6:
        return f"{clean[:3]} {clean[3:]}"
    return raw


def _dollars_to_words(amount):
    """Convert a dollar amount to written form for cheques.
    e.g. 1530.35 → 'One Thousand Five Hundred Thirty and 35/100 Dollars'
    """
    if amount < 0:
        return f"NEGATIVE ({_dollars_to_words(abs(amount))})"
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
            "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen",
            "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen",
            "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty",
            "Seventy", "Eighty", "Ninety"]

    def _under_1000(n):
        if n == 0:
            return ""
        elif n < 20:
            return ones[n]
        elif n < 100:
            return tens[n // 10] + ("" if n % 10 == 0
                                    else " " + ones[n % 10])
        else:
            rest = _under_1000(n % 100)
            return ones[n // 100] + " Hundred" + (
                " " + rest if rest else "")

    dollars = int(amount)
    cents = round(amount * 100) % 100

    if dollars == 0:
        words = "Zero"
    else:
        parts = []
        if dollars >= 1_000_000:
            parts.append(_under_1000(dollars // 1_000_000) + " Million")
            dollars %= 1_000_000
        if dollars >= 1_000:
            parts.append(_under_1000(dollars // 1_000) + " Thousand")
            dollars %= 1_000
        if dollars > 0:
            parts.append(_under_1000(dollars))
        words = " ".join(parts)

    return f"{words} and {cents:02d}/100 Dollars"


def get_rate_for_date(wage_history, target_date):
    """Return the hourly rate effective on a given date.

    Walks wage_history sorted by effective_date, returns the most
    recent rate on or before target_date.  Falls back to the last
    entry if all dates are in the future or "Unknown".
    """
    dated = []
    fallback_rate = None
    for wh in wage_history:
        eff = wh.get("effective_date", "Unknown")
        r = wh.get("rate", 0)
        fallback_rate = r  # keep last as fallback
        if eff and eff != "Unknown":
            try:
                dated.append((date.fromisoformat(eff), r))
            except ValueError:
                pass
    if not dated:
        return fallback_rate or 0.0
    dated.sort(key=lambda x: x[0])
    result = dated[0][1]  # default to earliest known rate
    for eff_date, r in dated:
        if eff_date <= target_date:
            result = r
        else:
            break
    return result


def add_wage_change(employee_name, new_rate, effective_date, note=""):
    """Add a wage change entry to an employee's profile."""
    profile = load_profile(employee_name)
    wage_history = profile.get("wage_history", [])
    wage_history.append({
        "rate": new_rate,
        "effective_date": effective_date.isoformat()
                         if hasattr(effective_date, 'isoformat')
                         else str(effective_date),
        "note": note,
    })
    # Sort by effective_date
    def _sort_key(e):
        d = e.get("effective_date", "")
        if d and d != "Unknown":
            try:
                return date.fromisoformat(d)
            except ValueError:
                pass
        return date.min
    wage_history.sort(key=_sort_key)
    profile["wage_history"] = wage_history
    # Update current rate to the most recent entry
    if wage_history:
        profile["regular_rate"] = wage_history[-1]["rate"]
    try:
        _db.save_employee(profile)
    except Exception:
        path = os.path.join(EMPLOYEES_DIR, employee_name, "profile.json")
        with open(path, "w") as f:
            json.dump(profile, f, indent=2)


# WCB rate loaded from employer_config.json (editable in sidebar → Employer Profile)
from employer_config import EMPLOYER as _EMPLOYER_INIT
WCB_RATE_PER_100 = _EMPLOYER_INIT.get("wcb_rate_per_100", 3.50)


def _compute_employer_cost(employee_name, wage_history):
    """Compute actualized employer cost per hour from remittance data.

    Walks all remittance entries for this employee, sums gross pay +
    employer CPP + employer EI + WCB premium, estimates hours from
    gross ÷ effective rate, and returns the true cost-per-hour.
    """
    all_months, _ = available_months()
    if not all_months:
        return None

    entries = []
    for year, month in all_months:
        try:
            raw = _db.get_remittances(year, month)
        except Exception:
            continue
        for r in raw:
            if r.get("employee") == employee_name:
                entries.append({"year": year, "month": month, **r})

    if not entries:
        return None

    total_gross = 0.0
    total_cpp_er = 0.0
    total_cpp2_er = 0.0
    total_ei_er = 0.0
    total_wcb = 0.0
    total_est_hours = 0.0
    monthly = {}

    for e in entries:
        gross = e["gross"]
        cpp_er = e.get("cpp_employer", 0)
        cpp2_er = e.get("cpp2_employer", 0)
        ei_er = e.get("ei_employer", 0)
        wcb = gross * WCB_RATE_PER_100 / 100

        # Estimate hours using effective rate for that period
        approx_date = date(e["year"], e["month"], 15)
        eff_rate = get_rate_for_date(wage_history, approx_date)
        est_hours = round(gross / eff_rate, 2) if eff_rate > 0 else 0

        total_gross += gross
        total_cpp_er += cpp_er
        total_cpp2_er += cpp2_er
        total_ei_er += ei_er
        total_wcb += wcb
        total_est_hours += est_hours

        key = f"{e['year']}-{e['month']:02d}"
        if key not in monthly:
            monthly[key] = {
                "gross": 0, "cpp_er": 0, "cpp2_er": 0, "ei_er": 0,
                "wcb": 0, "est_hours": 0, "entries": 0,
            }
        m = monthly[key]
        m["gross"] += gross
        m["cpp_er"] += cpp_er
        m["cpp2_er"] += cpp2_er
        m["ei_er"] += ei_er
        m["wcb"] += wcb
        m["est_hours"] += est_hours
        m["entries"] += 1

    total_cost = total_gross + total_cpp_er + total_cpp2_er + total_ei_er + total_wcb
    actualized = total_cost / total_est_hours if total_est_hours > 0 else 0

    for key in monthly:
        m = monthly[key]
        m["total_cost"] = round(
            m["gross"] + m["cpp_er"] + m["cpp2_er"] + m["ei_er"] + m["wcb"], 2)
        m["actualized_rate"] = (
            round(m["total_cost"] / m["est_hours"], 2)
            if m["est_hours"] > 0 else 0)
        m["gross"] = round(m["gross"], 2)
        m["cpp_er"] = round(m["cpp_er"], 2)
        m["cpp2_er"] = round(m["cpp2_er"], 2)
        m["ei_er"] = round(m["ei_er"], 2)
        m["wcb"] = round(m["wcb"], 2)
        m["est_hours"] = round(m["est_hours"], 1)

    return {
        "actualized_rate": round(actualized, 2),
        "total_cost": round(total_cost, 2),
        "total_gross": round(total_gross, 2),
        "total_cpp_er": round(total_cpp_er, 2),
        "total_cpp2_er": round(total_cpp2_er, 2),
        "total_ei_er": round(total_ei_er, 2),
        "total_wcb": round(total_wcb, 2),
        "total_est_hours": round(total_est_hours, 1),
        "entry_count": len(entries),
        "monthly": monthly,
    }


def _parse_payment_date(date_str):
    """Parse 'February 1st, 2026' → date object. Delegates to utils."""
    return parse_payroll_date(date_str)


def _compute_ytd(employee_name, pay_year, before_date=None):
    """Auto-calculate YTD gross, net, and per-deduction totals from
    remittance data.

    Only includes entries with payment dates strictly before before_date
    (a date object), so the current period can be added on top without
    double-counting — even during regeneration.

    Returns (ytd_gross, ytd_net, count, ytd_deductions_dict).
    """
    ytd_gross = 0.0
    ytd_cpp = 0.0
    ytd_cpp2 = 0.0
    ytd_ei = 0.0
    ytd_fed = 0.0
    ytd_prov = 0.0
    count = 0
    _ytd_skipped = 0
    for year, month in available_months()[0]:
        if year != pay_year:
            continue
        try:
            _month_entries = _db.get_remittances(year, month)
        except Exception:
            _month_entries = []
        for e in _month_entries:
            if e.get("employee") != employee_name:
                continue
            if before_date:
                try:
                    entry_date = _parse_payment_date(e["payment_date"])
                    if entry_date >= before_date:
                        continue
                except (ValueError, KeyError):
                    _ytd_skipped += 1
                    continue
            count += 1
            ytd_gross += e["gross"]
            ytd_cpp += e.get("cpp_employee", 0)
            ytd_cpp2 += e.get("cpp2_employee", 0)
            ytd_ei += e.get("ei_employee", 0)
            ytd_fed += e.get("fed_tax", 0)
            ytd_prov += e.get("prov_tax", 0)
    ytd_ded = ytd_cpp + ytd_cpp2 + ytd_ei + ytd_fed + ytd_prov
    return (round(ytd_gross, 2), round(ytd_gross - ytd_ded, 2), count,
            {"cpp": round(ytd_cpp, 2), "cpp2": round(ytd_cpp2, 2),
             "ei": round(ytd_ei, 2), "fed_tax": round(ytd_fed, 2),
             "prov_tax": round(ytd_prov, 2)},
            _ytd_skipped)


def _compute_stat_holiday_pay(employee_name, stat_date, days_per_week=5):
    """Alberta ESA: avg daily wage for general holiday pay.

    Formula: (wages in 4 weeks before holiday, excl. OT & holiday pay)
             ÷ days actually worked in that period.

    Since remittance data stores gross (not broken down by regular/OT),
    we use gross as an approximation and estimate days worked from the
    employee's schedule. The result is labelled as an estimate.

    Returns (estimated_pay, payroll_count, estimated_days).
    """
    period_start = stat_date - timedelta(days=28)
    period_end = stat_date - timedelta(days=1)
    total_wages = 0.0
    count = 0
    for year, month in available_months()[0]:
        try:
            _stat_month = _db.get_remittances(year, month)
        except Exception:
            _stat_month = []
        for e in _stat_month:
            if e.get("employee") != employee_name:
                continue
            try:
                clean = re.sub(r'(\d+)(st|nd|rd|th)', r'\1',
                               e["payment_date"])
                pay_dt = datetime.strptime(clean, "%B %d, %Y").date()
            except (ValueError, KeyError):
                continue
            if period_start <= pay_dt <= period_end:
                total_wages += e["gross"]
                count += 1
    if count == 0:
        return 0.0, 0, 0
    # Estimate days worked: 4 weeks × days_per_week from schedule
    est_days = 4 * days_per_week
    return round(total_wages / est_days, 2), count, est_days


def get_existing_employees():
    try:
        names = _db.list_all_employees()
        if names:
            return sorted(names)
    except Exception:
        pass
    # Local fallback
    if not os.path.isdir(EMPLOYEES_DIR):
        return []
    results = []
    for n in os.listdir(EMPLOYEES_DIR):
        d = os.path.join(EMPLOYEES_DIR, n)
        if not os.path.isdir(d):
            continue
        has_profile = os.path.isfile(os.path.join(d, "profile.json"))
        has_pdfs = any(f.endswith(".pdf") for f in os.listdir(d))
        has_agreements = os.path.isdir(os.path.join(d, "agreements"))
        if has_profile or has_pdfs or has_agreements:
            results.append(n)
    return sorted(results)


def get_employee_payrolls(name):
    folder = os.path.join(EMPLOYEES_DIR, name)
    if not os.path.isdir(folder):
        return []
    return sorted(glob.glob(os.path.join(folder, "*.pdf")))


def load_profile(name):
    try:
        data = _db.get_employee(name)
        if data:
            return data
    except Exception:
        pass
    # Local fallback
    path = os.path.join(EMPLOYEES_DIR, name, "profile.json")
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            st.error(f"Corrupted profile: {path} — {exc}")
    return {}


def save_profile(name, updates):
    """Merge updates into the existing profile. Fields not in updates
    are preserved — no more silent data loss when new fields are added.

    wage_history is NEVER overwritten here (managed by add_wage_change()).
    regular_rate is kept in sync with the latest wage_history entry.
    """
    folder = os.path.join(EMPLOYEES_DIR, name)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "profile.json")

    existing = {}
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            st.warning(
                f"⚠ Profile file for **{name}** is corrupted. "
                f"Existing data cannot be read — saving will start fresh. "
                f"Check `{path}` and restore from backup if needed.")
            existing = {}

    # Merge: existing fields are preserved, updates overwrite matching keys
    merged = {**existing, **updates}

    # wage_history is managed exclusively by add_wage_change() —
    # always keep the existing version, never accept it from the form
    merged["wage_history"] = existing.get("wage_history", [])

    # Keep regular_rate in sync with latest wage history entry
    if merged["wage_history"]:
        merged["regular_rate"] = merged["wage_history"][-1]["rate"]

    try:
        _db.save_employee(merged)
    except Exception:
        pass
    # Always keep local JSON in sync as backup
    with open(path, "w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)


# ── Session state init ───────────────────────────────────────

if "loaded_employee" not in st.session_state:
    st.session_state.loaded_employee = None
if "save_msg" not in st.session_state:
    st.session_state.save_msg = None


# ── Sidebar ──────────────────────────────────────────────────

_co = load_employer()
st.sidebar.title(f"{_co.get('short_name', 'D&S')} Payroll")
st.sidebar.markdown("---")

existing = get_existing_employees()
options  = ["+ New Employee"] + existing
selected = st.sidebar.selectbox("Employee", options)

# Load profile when selection changes
if selected != st.session_state.loaded_employee:
    st.session_state.loaded_employee = selected
    # Clear stale payroll state from previous employee
    for _stale_key in ["_last_pdf", "_last_remittance", "_preview_img",
                        "_t4_preview_img", "_roe_preview_img",
                        "_verify_letter_path", "_wage_letter_path",
                        "_term_letter_path",
                        "_overwrite_conflict", "_force_overwrite",
                        "_vac_del_confirm", "_audit_del_confirm",
                        "show_vacation"]:
        st.session_state.pop(_stale_key, None)
    if selected != "+ New Employee":
        p = load_profile(selected)
        addr = p.get("employee_address") or ["", "Edmonton, AB", ""]
        st.session_state["f_emp_name"]   = p.get("employee_name", selected)
        st.session_state["f_emp_id"]     = p.get("employee_id", "N/A")
        st.session_state["f_addr1"]      = addr[0] if len(addr) > 0 else ""
        st.session_state["f_addr2"]      = addr[1] if len(addr) > 1 else "Edmonton, AB"
        st.session_state["f_addr3"]      = fmt_postal(addr[2]) if len(addr) > 2 and addr[2] else ""
        st.session_state["f_position"]   = p.get("position", "")
        _pay_methods = ["Direct Deposit (EFT)", "E-Transfer", "Cheque"]
        _pm = p.get("payment_method", "Direct Deposit (EFT)")
        st.session_state["f_pay_method"] = (
            _pm if _pm in _pay_methods else "Direct Deposit (EFT)")
        st.session_state["f_sin"]        = fmt_sin(p.get("sin", "")) if p.get("sin") else ""
        st.session_state["f_phone"]      = fmt_phone(p.get("phone", "")) if p.get("phone") else ""
        st.session_state["f_email"]      = p.get("email", "")
        st.session_state["f_bank_inst"]    = p.get("bank_institution", "")
        st.session_state["f_bank_transit"] = p.get("bank_transit", "")
        st.session_state["f_bank_account"] = p.get("bank_account", "")
        st.session_state["f_td1_fed"]      = p.get("td1_fed_claim", 0.0)
        st.session_state["f_td1_prov"]     = p.get("td1_prov_claim", 0.0)
        st.session_state["f_td1_extra"]    = p.get("td1_extra_deduction", 0.0)
        st.session_state["f_td1_exempt"]   = p.get("td1_exempt", False)
        st.session_state["f_emerg_name"] = p.get("emergency_contact_name", "")
        st.session_state["f_emerg_phone"] = fmt_phone(p.get("emergency_contact_phone", "")) if p.get("emergency_contact_phone") else ""
        _et = p.get("employment_type", "Full-Time")
        st.session_state["f_emp_type"]   = (
            _et if _et in ["Full-Time", "Part-Time", "Casual"]
            else "Full-Time")
        prov_val = p.get("province", "Alberta")
        st.session_state["f_province"]   = (
            prov_val if prov_val in PROVINCES else "Alberta")
        _sched = p.get("schedule_type", DEFAULT_SCHEDULE_TYPE)
        st.session_state["f_schedule_type"] = (
            _sched if _sched in SCHEDULE_TYPE_KEYS
            else DEFAULT_SCHEDULE_TYPE)
        st.session_state["f_has_averaging"] = (_sched != "standard"
                                                and _sched in SCHEDULE_TYPE_KEYS)
        sd = p.get("start_date")
        st.session_state["f_start_date"] = (
            date.fromisoformat(sd) if sd else date(2024, 1, 1))
        ed = p.get("end_date")
        st.session_state["f_end_date"] = (
            date.fromisoformat(ed) if ed else None)
        st.session_state["f_currently_employed"] = (ed is None)
        dob = p.get("dob")
        st.session_state["f_dob"]        = (
            date.fromisoformat(dob) if dob else date(1990, 1, 1))
        st.session_state["f_cpp"]        = 0.0
        st.session_state["f_cpp2"]       = 0.0
        st.session_state["f_ei"]         = 0.0
        st.session_state["f_fed_tax"]    = 0.0
        st.session_state["f_prov_tax"]   = 0.0
        _pt = p.get("part_time") or {}
        _freq_rev = {24: "Semi-Monthly (24/yr)", 26: "Bi-Weekly (26/yr)",
                     52: "Weekly (52/yr)"}
        st.session_state["f_pt_days_pw"]  = int(_pt.get("days_per_week", 3))
        st.session_state["f_pt_hours_pd"] = float(_pt.get("hours_per_day", 6.0))
        st.session_state["f_pt_pay_freq"] = _freq_rev.get(
            int(_pt.get("pay_periods", 24)), "Semi-Monthly (24/yr)")
    else:
        st.session_state["f_emp_name"]   = ""
        st.session_state["f_emp_id"]     = "N/A"
        st.session_state["f_addr1"]      = ""
        st.session_state["f_addr2"]      = "Edmonton, AB"
        st.session_state["f_addr3"]      = ""
        st.session_state["f_position"]   = ""
        st.session_state["f_pay_method"] = "Direct Deposit (EFT)"
        st.session_state["f_sin"]        = ""
        st.session_state["f_phone"]      = ""
        st.session_state["f_email"]      = ""
        st.session_state["f_bank_inst"]    = ""
        st.session_state["f_bank_transit"] = ""
        st.session_state["f_bank_account"] = ""
        st.session_state["f_td1_fed"]      = 0.0
        st.session_state["f_td1_prov"]     = 0.0
        st.session_state["f_td1_extra"]    = 0.0
        st.session_state["f_td1_exempt"]   = False
        st.session_state["f_emerg_name"] = ""
        st.session_state["f_emerg_phone"] = ""
        st.session_state["f_emp_type"]   = "Full-Time"
        st.session_state["f_province"]   = "Alberta"
        st.session_state["f_schedule_type"] = DEFAULT_SCHEDULE_TYPE
        st.session_state["f_has_averaging"] = False
        st.session_state["f_start_date"] = date.today()
        st.session_state["f_end_date"]   = None
        st.session_state["f_currently_employed"] = True
        st.session_state["f_dob"]        = date(1990, 1, 1)
        st.session_state["f_cpp"]        = 0.0
        st.session_state["f_cpp2"]       = 0.0
        st.session_state["f_ei"]         = 0.0
        st.session_state["f_fed_tax"]    = 0.0
        st.session_state["f_prov_tax"]   = 0.0
        st.session_state["f_pt_days_pw"]  = 3
        st.session_state["f_pt_hours_pd"] = 6.0
        st.session_state["f_pt_pay_freq"] = "Semi-Monthly (24/yr)"

# ── Document Index (sidebar) ──
if selected != "+ New Employee":
    _emp_dir = os.path.join(PAYROLL_DIR, "employees", selected)
    _doc_sections = []

    # Try to get employee UUID for Storage listing
    _sidebar_emp_uuid = None
    try:
        _sidebar_emp_uuid = _db._employee_id(selected)
    except Exception:
        pass

    def _list_docs(storage_prefix, local_pattern):
        """List docs from Storage (if UUID known) or local glob."""
        paths = []
        if _sidebar_emp_uuid:
            try:
                paths = _sc.list_files(_sc.PDF_BUCKET,
                                       f"employees/{_sidebar_emp_uuid}/{storage_prefix}")
            except Exception:
                pass
        if not paths:
            paths = sorted(glob.glob(local_pattern))
        return paths

    # 1. Paystubs (grouped by year)
    _paystubs = _list_docs("paystubs", os.path.join(_emp_dir, "Payroll_*.pdf"))
    if _paystubs:
        _by_year = {}
        for _pp in _paystubs:
            _fn = _sc.storage_path_to_filename(_pp) if _sidebar_emp_uuid and _pp.startswith("employees/") else os.path.basename(_pp)
            _base = _fn.replace(".pdf", "").replace("Payroll_", "")
            _yr = _base[:4] if _base[:4].isdigit() else "Other"
            _by_year.setdefault(_yr, []).append((_pp, _fn))
        _doc_sections.append(("Paystubs", _paystubs, _by_year))

    # 2. T4 Slips
    _t4_pdfs = _list_docs("t4", os.path.join(_emp_dir, "T4", "T4_*.pdf"))
    if _t4_pdfs:
        _doc_sections.append(("T4 Slips", [(p, _sc.storage_path_to_filename(p) if p.startswith("employees/") else os.path.basename(p)) for p in _t4_pdfs], None))

    # 3. ROE Documents
    _roe_pdfs = _list_docs("roe", os.path.join(_emp_dir, "ROE", "ROE_*.pdf"))
    if _roe_pdfs:
        _doc_sections.append(("ROE Documents", [(p, _sc.storage_path_to_filename(p) if p.startswith("employees/") else os.path.basename(p)) for p in _roe_pdfs], None))

    # 4. Agreements
    _agr_pdfs = _list_docs("agreements", os.path.join(_emp_dir, "agreements", "*.pdf"))
    if _agr_pdfs:
        _doc_sections.append(("Agreements", [(p, _sc.storage_path_to_filename(p) if p.startswith("employees/") else os.path.basename(p)) for p in _agr_pdfs], None))

    # 5. Letters
    _ltr_pdfs = _list_docs("letters", os.path.join(_emp_dir, "Letters", "*.pdf"))
    if _ltr_pdfs:
        _doc_sections.append(("Letters", [(p, _sc.storage_path_to_filename(p) if p.startswith("employees/") else os.path.basename(p)) for p in _ltr_pdfs], None))

    if _doc_sections:
        st.sidebar.markdown("---")
        st.sidebar.subheader("Documents")

        for _sec_name, _sec_files, _year_groups in _doc_sections:
            if _year_groups:
                # Paystubs — sub-group by year
                _s_years = sorted(_year_groups.keys(), reverse=True)
                with st.sidebar.expander(
                        f"{_sec_name} ({len(_paystubs)})"):
                    for _sy in _s_years:
                        st.caption(f"**{_sy}**")
                        for _sp, _sfn in _year_groups[_sy]:
                            _sb = _sfn.replace(".pdf", "").replace(
                                "Payroll_", "")
                            if _sb[:4].isdigit() and "-" in _sb[:10]:
                                try:
                                    _sd = date.fromisoformat(_sb[:10])
                                    _sl = _sd.strftime("%b %d, %Y")
                                except ValueError:
                                    _sl = _sb.replace("_", " ")
                            else:
                                _sl = _sb.replace("_", " ")
                            _sbytes = _get_pdf_bytes(_sp)
                            if _sbytes:
                                st.download_button(
                                    _sl, data=_sbytes,
                                    file_name=_sfn,
                                    mime="application/pdf",
                                    key=f"di_{_sp}")
            else:
                with st.sidebar.expander(
                        f"{_sec_name} ({len(_sec_files)})"):
                    for _sp, _sfn in _sec_files:
                        _sl = _sfn.replace(".pdf", "").replace(
                            "_", " ")
                        _sbytes = _get_pdf_bytes(_sp)
                        if _sbytes:
                            st.download_button(
                                _sl, data=_sbytes,
                                file_name=_sfn,
                                mime="application/pdf",
                                key=f"di_{_sp}")

st.sidebar.markdown("---")
st.sidebar.caption(_co.get("operating_as", ""))

with st.sidebar.expander("Employer Profile"):
    _co_legal = st.text_input("Legal Name", value=_co.get("legal_name", ""),
                              key="_co_legal")
    _co_oa = st.text_input("Operating As", value=_co.get("operating_as", ""),
                           key="_co_oa")
    _co_addr1 = st.text_input("Address Line 1",
                              value=_co.get("address_lines", [""])[0] if _co.get("address_lines") else "",
                              key="_co_addr1")
    _co_addr2 = st.text_input("City, Province",
                              value=_co.get("address_lines", [])[1] if len(_co.get("address_lines", [])) > 1 else "",
                              key="_co_addr2")
    _co_addr3 = st.text_input("Postal Code",
                              value=_co.get("address_lines", [])[2] if len(_co.get("address_lines", [])) > 2 else "",
                              key="_co_addr3")
    _co_bn = st.text_input("CRA Business Number",
                           value=_co.get("cra_bn", ""), key="_co_bn")
    _co_signer = st.text_input("Authorized Signer",
                               value=_co.get("signer_name", ""), key="_co_signer")
    _co_title = st.text_input("Signer Title",
                              value=_co.get("signer_title", ""), key="_co_title")
    _co_email = st.text_input("Contact Email",
                              value=_co.get("signer_email", ""), key="_co_email")
    _co_wcb = st.number_input("WCB Rate (per $100)",
                              value=_co.get("wcb_rate_per_100", 3.50),
                              min_value=0.0, step=0.01, key="_co_wcb")
    _retention_labels = list(RETENTION_PRESETS.keys())
    _retention_days_cur = _co.get("trash_retention_days", 7)
    _retention_label_cur = next(
        (k for k, v in RETENTION_PRESETS.items()
         if v == _retention_days_cur), "1 week")
    _co_retention = st.selectbox(
        "Recovery Bin Retention",
        _retention_labels,
        index=_retention_labels.index(_retention_label_cur),
        key="_co_retention",
        help="How long deleted items remain recoverable before permanent removal.")
    # ── Document Theme ──
    st.markdown("---")
    _themes = _co.get("themes", {})
    _theme_keys = list(_themes.keys())
    _theme_labels = {k: _themes[k].get("label", k) for k in _theme_keys}
    _cur_active = _co.get("active_theme", "theme_1")
    _co_theme = st.selectbox(
        "Document Theme",
        _theme_keys,
        format_func=lambda k: _theme_labels.get(k, k),
        index=_theme_keys.index(_cur_active) if _cur_active in _theme_keys else 0,
        key="_co_theme",
        help="Applied to all paystubs, agreements, and letters.")

    # Live header preview
    _prev_header = _themes.get(_co_theme, {}).get("header", "")
    if _prev_header:
        _prev_abs = (_prev_header if os.path.isabs(_prev_header)
                     else os.path.join(PAYROLL_DIR, _prev_header))
        if os.path.isfile(_prev_abs):
            try:
                _prev_doc = fitz.open(_prev_abs)
                _prev_pix = _prev_doc[0].get_pixmap(dpi=100)
                st.image(_prev_pix.tobytes("png"), use_container_width=True)
                _prev_doc.close()
            except Exception:
                st.caption("(Preview unavailable)")

    if st.button("Save Employer Profile", key="btn_save_co"):
        _new_co = {
            "legal_name": _co_legal,
            "operating_as": _co_oa,
            "full_name": f"{_co_legal} o/a {_co_oa}",
            "short_name": _co.get("short_name", "D&S"),
            "address_lines": [_co_addr1, _co_addr2, _co_addr3],
            "address_oneline": f"{_co_addr1}, {_co_addr2}  {_co_addr3}",
            "cra_bn": _co_bn,
            "signer_name": _co_signer,
            "signer_title": _co_title,
            "signer_email": _co_email,
            "wcb_rate_per_100": _co_wcb,
            "wcb_code": _co.get("wcb_code", ""),
            "wcb_note": _co.get("wcb_note", ""),
            "trash_retention_days": RETENTION_PRESETS[_co_retention],
            "active_theme": _co_theme,
            "themes": _themes,
        }
        save_employer(_new_co)
        st.success("Employer profile saved.")
        st.rerun()


# ══════════════════════════════════════════════════════════════
#  TABS
# ══════════════════════════════════════════════════════════════

tab_payroll, tab_remittances, tab_audit, tab_t4, tab_roe, tab_insights, tab_docs = st.tabs(
    ["Payroll", "Remittances", "Audit", "T4 Slips", "ROE", "Insights", "Documents"])


# ══════════════════════════════════════════════════════════════
#  TAB 1: GENERATE PAYROLL
# ══════════════════════════════════════════════════════════════

with tab_payroll:
    st.title("Generate Payroll")
    st.markdown("---")

    col1, col2 = st.columns(2)

    # ── Left: Employee Info ──
    with col1:
        st.subheader("Employee Info")
        employer_name = _co.get("legal_name", "Hot Plates Inc.")
        st.caption(f"Employer: **{employer_name}**")

        # Employment status badge
        _prof_end = st.session_state.get("f_end_date")
        if _prof_end:
            st.warning(f"Employment ended: **{_prof_end.strftime('%B %d, %Y')}**")

        # ── Onboarding Checklist ──
        if selected != "+ New Employee":
            _ob_prof = load_profile(selected)
            # Check for any paystub in DB or locally
            _ob_pdfs_local = glob.glob(
                os.path.join(PAYROLL_DIR, "employees", selected, "Payroll_*.pdf"))
            _ob_has_payroll = bool(_ob_pdfs_local)
            if not _ob_has_payroll:
                try:
                    _ob_months = available_months()[0]
                    for _ob_yr, _ob_mo in _ob_months:
                        for _ob_e in _db.get_remittances(_ob_yr, _ob_mo):
                            if _ob_e.get("employee") == selected:
                                _ob_has_payroll = True
                                break
                        if _ob_has_payroll:
                            break
                except Exception:
                    pass
            if not _ob_has_payroll:
                with st.expander("Onboarding Checklist", expanded=True):
                    _on_checks = []
                    _on_checks.append((
                        "Start date set",
                        bool(_ob_prof.get("start_date"))))
                    _on_checks.append((
                        "SIN on file",
                        bool(_ob_prof.get("sin", "").strip())))
                    _on_checks.append((
                        "Date of birth",
                        bool(_ob_prof.get("dob"))))
                    _on_checks.append((
                        "Address on file",
                        bool("".join(_ob_prof.get(
                            "employee_address", [])).strip())))
                    _on_checks.append((
                        "Emergency contact",
                        bool(_ob_prof.get(
                            "emergency_contact_name", "").strip())))
                    _on_checks.append((
                        "Payment method set",
                        bool(_ob_prof.get("payment_method"))))
                    _td1_set = (_ob_prof.get("td1_fed_claim", 0) > 0
                                or _ob_prof.get("td1_exempt", False))
                    _on_checks.append(("TD1 claim set", _td1_set))
                    _on_checks.append((
                        "Starting wage entered",
                        bool(_ob_prof.get("wage_history"))))
                    _on_checks.append((
                        "Schedule type set",
                        bool(_ob_prof.get("schedule_type"))))
                    # Averaging agreement (only if non-standard)
                    _ob_sched = _ob_prof.get("schedule_type", "standard")
                    if _ob_sched != "standard":
                        _ob_agrs = load_agreements(selected)
                        _ob_agr_key = f"averaging_{_ob_sched}"
                        _on_checks.append((
                            "Averaging agreement signed",
                            bool(_ob_agrs.get(_ob_agr_key, {}).get(
                                "signed_date"))))

                    _on_done = sum(1 for _, ok in _on_checks if ok)
                    st.progress(_on_done / len(_on_checks),
                                text=f"{_on_done}/{len(_on_checks)} complete")
                    for _lbl, _ok in _on_checks:
                        if _ok:
                            st.markdown(
                                f"- :white_check_mark: ~~{_lbl}~~")
                        else:
                            st.markdown(f"- :x: **{_lbl}**")

        # ── Identity ──
        employee_name  = st.text_input("Employee Name", key="f_emp_name")
        ei_c1, ei_c2   = st.columns(2)
        with ei_c1:
            employee_id = st.text_input("Employee ID", key="f_emp_id")
        with ei_c2:
            sin         = st.text_input("SIN", key="f_sin",
                                        placeholder="123 456 789")
        dob            = st.date_input("Date of Birth", key="f_dob")

        # ── Contact ──
        _is_new = (selected == "+ New Employee")
        with st.expander("Contact & Address", expanded=_is_new):
            phone      = st.text_input("Phone", key="f_phone",
                                       placeholder="(780) 555-1234")
            email      = st.text_input("Email", key="f_email",
                                       placeholder="name@example.com")
            addr1      = st.text_input("Address", key="f_addr1")
            addr_c1, addr_c2 = st.columns(2)
            with addr_c1:
                addr2  = st.text_input("City, Province", key="f_addr2")
            with addr_c2:
                addr3  = st.text_input("Postal Code", key="f_addr3",
                                       placeholder="T5Y 1Z4")

        # ── Employment ──
        with st.expander("Employment Details", expanded=_is_new):
            emp_c1, emp_c2 = st.columns(2)
            with emp_c1:
                position       = st.text_input("Position / Title",
                                               key="f_position")
                employment_type = st.selectbox(
                    "Employment Type",
                    ["Full-Time", "Part-Time", "Casual"],
                    key="f_emp_type")
            with emp_c2:
                start_date     = st.date_input(
                    "Start Date", key="f_start_date",
                    help="Drives vacation pay and stat eligibility.")
                province       = st.selectbox(
                    "Province of Employment", PROVINCES,
                    key="f_province")

            # ── Part-Time Schedule (inline when Part-Time selected) ──
            _is_pt = (employment_type == "Part-Time")
            if _is_pt:
                _pt_c1, _pt_c2, _pt_c3 = st.columns(3)
                with _pt_c1:
                    pt_days_pw = st.number_input(
                        "Days/Week", min_value=1, max_value=5,
                        value=3, step=1, key="f_pt_days_pw",
                        help="Scheduled workdays per week")
                with _pt_c2:
                    pt_hours_pd = st.number_input(
                        "Hours/Day", min_value=1.0, max_value=12.0,
                        value=6.0, step=0.5, format="%.1f",
                        key="f_pt_hours_pd",
                        help="Scheduled hours per day")
                with _pt_c3:
                    _freq_map = {
                        "Semi-Monthly (24/yr)": 24,
                        "Bi-Weekly (26/yr)":    26,
                        "Weekly (52/yr)":       52,
                    }
                    pt_pay_freq = st.selectbox(
                        "Pay Frequency", list(_freq_map.keys()),
                        key="f_pt_pay_freq",
                        help="How often this employee is paid per year")
                    pt_pay_periods = _freq_map[pt_pay_freq]
                _annual_pt_hours = pt_hours_pd * pt_days_pw * 52
                if _annual_pt_hours < 700:
                    st.warning(
                        f"**EI Hours:** Projected annual hours = "
                        f"**{_annual_pt_hours:.0f} hrs**. "
                        f"Alberta EI qualifying threshold is up to "
                        f"**700 insurable hours** — employee may not "
                        f"accumulate sufficient hours to claim EI benefits. "
                        f"Verify with Service Canada regional rate.")
                else:
                    st.caption(
                        f"Projected annual hours: **{_annual_pt_hours:.0f} hrs** "
                        f"(\u2713 above 700-hr EI threshold)")
            else:
                pt_days_pw     = 5
                pt_hours_pd    = 8.0
                pt_pay_periods = 24

            # ── Employment Status / End Date ──
            _currently_employed = st.checkbox(
                "Currently Employed",
                key="f_currently_employed")
            if _currently_employed:
                end_date = None
            else:
                _ed_default = (st.session_state.get("f_end_date")
                               or date.today())
                end_date = st.date_input(
                    "End Date (Last Day Worked)",
                    value=_ed_default,
                    key="f_end_date_input",
                    help="Last date of employment. Stops vacation accrual.")
                st.session_state["f_end_date"] = end_date
            payment_method = st.selectbox(
                "Payment Method",
                ["Direct Deposit (EFT)", "E-Transfer", "Cheque"],
                key="f_pay_method")

            if payment_method == "Direct Deposit (EFT)":
                _bc1, _bc2, _bc3 = st.columns(3)
                with _bc1:
                    bank_inst = st.text_input(
                        "Institution #", key="f_bank_inst",
                        placeholder="001")
                with _bc2:
                    bank_transit = st.text_input(
                        "Transit #", key="f_bank_transit",
                        placeholder="12345")
                with _bc3:
                    bank_account = st.text_input(
                        "Account #", key="f_bank_account",
                        placeholder="1234567",
                        type="password")
                    if bank_account:
                        st.caption(f"Account: ••••{bank_account[-4:]}")
            else:
                bank_inst = st.session_state.get("f_bank_inst", "")
                bank_transit = st.session_state.get("f_bank_transit", "")
                bank_account = st.session_state.get("f_bank_account", "")

            # ── TD1 Tax Credit Claims ──
            st.markdown("---")
            st.caption("**TD1 Tax Credit Claims** — affects withholding calculations")
            _td1_c1, _td1_c2 = st.columns(2)
            with _td1_c1:
                td1_fed = st.number_input(
                    "Federal TD1 Claim ($)",
                    min_value=0.0, step=100.0,
                    key="f_td1_fed",
                    help="From TD1 form. 0 = use default BPA only.")
                td1_extra = st.number_input(
                    "Additional Tax Deduction ($)",
                    min_value=0.0, step=10.0,
                    key="f_td1_extra",
                    help="Extra withholding per pay (TD1 Line 4)")
            with _td1_c2:
                td1_prov = st.number_input(
                    "Provincial TD1 Claim ($)",
                    min_value=0.0, step=100.0,
                    key="f_td1_prov",
                    help="From TD1AB form. 0 = use default BPA only.")
                td1_exempt = st.checkbox(
                    "Tax Exempt (TD1 total income < BPA)",
                    key="f_td1_exempt",
                    help="If checked, no income tax is withheld.")

            # ── Schedule / Averaging Agreement ──
            st.caption("Preset: **Standard (5x8)** -- 40 hrs/week, "
                       "OT after 8 hrs/day or 44 hrs/week")
            _has_averaging = st.checkbox(
                "Averaging Agreement",
                key="f_has_averaging")

            if _has_averaging:
                _avg_options = [k for k in SCHEDULE_TYPE_KEYS if k != "standard"]
                _cur_sched = st.session_state.get(
                    "f_schedule_type", DEFAULT_SCHEDULE_TYPE)
                _avg_idx = (_avg_options.index(_cur_sched)
                            if _cur_sched in _avg_options else 0)
                schedule_type = st.selectbox(
                    "Agreement Type",
                    _avg_options,
                    index=_avg_idx,
                    format_func=lambda k: SCHEDULE_TYPES[k]["label"],
                    key="f_schedule_type")
                _sched_info = get_schedule(schedule_type)
                st.info(
                    f"**Averaging Agreement** -- "
                    f"{_sched_info['summary']}")

                # ── Signed agreement PDF ──
                _agr_key = f"averaging_{schedule_type}"
                _agr_data = load_agreements(selected).get(_agr_key, {}) if selected != "+ New Employee" else {}
                _signed_pdf = _agr_data.get("signed_pdf")
                _signed_date = _agr_data.get("signed_date")
                _signed_bytes = _get_pdf_bytes(_signed_pdf) if _signed_pdf else None
                _has_file = bool(_signed_bytes)

                if _has_file and _signed_date:
                    st.success(f"Signed copy on file (dated {_signed_date})")
                    st.download_button(
                        "Download Signed PDF", _signed_bytes,
                        file_name=os.path.basename(_signed_pdf),
                        mime="application/pdf",
                        key="dl_signed_avg")
                else:
                    st.warning("No signed copy on file")

                _up_c1, _up_c2 = st.columns([3, 2])
                with _up_c1:
                    _avg_upload = st.file_uploader(
                        "Upload signed agreement",
                        type=["pdf"], key="upload_avg_agr")
                with _up_c2:
                    _avg_sign_date = st.date_input(
                        "Date Signed", value=date.today(),
                        key="avg_sign_date")
                    # Generate PDF only on button click, cache the path
                    _name = employee_name.strip()
                    if st.button("Generate Document", key="btn_gen_avg"):
                        _emp_rate = None
                        _wh_gen = load_profile(selected).get(
                            "wage_history", [])
                        if _wh_gen:
                            _emp_rate = _wh_gen[-1]["rate"]
                        _gen_path = generate_averaging_agreement(
                            _name, schedule_type,
                            _avg_sign_date, _emp_rate)
                        st.session_state["_avg_agr_path"] = _gen_path

                    _cached_avg = st.session_state.get("_avg_agr_path")
                    if _cached_avg:
                        _avg_bytes = _get_pdf_bytes(_cached_avg)
                        if _avg_bytes:
                            st.download_button(
                                "Download Document", _avg_bytes,
                                file_name=os.path.basename(_cached_avg),
                                mime="application/pdf",
                                key="dl_gen_avg")

                if _avg_upload is not None:
                    _name = employee_name.strip()
                    _agr_dir = os.path.join(
                        EMPLOYEES_DIR, _name, "agreements")
                    os.makedirs(_agr_dir, exist_ok=True)
                    _save_name = (f"{_agr_key}_signed_"
                                  f"{_avg_sign_date.isoformat()}.pdf")
                    _save_path = os.path.join(_agr_dir, _save_name)
                    with open(_save_path, "wb") as _f:
                        _f.write(_avg_upload.getbuffer())
                    set_agreement(_name, _agr_key,
                                  _save_path, _avg_sign_date.isoformat())
                    st.session_state.save_msg = (
                        f"Signed averaging agreement uploaded "
                        f"(dated {_avg_sign_date}).")
                    st.rerun()
            else:
                schedule_type = "standard"
                st.session_state["f_schedule_type"] = "standard"
                _sched_info = get_schedule(schedule_type)

            # ── Wage / Rate Management ──
            st.markdown("---")
            _wh = (load_profile(selected).get("wage_history", [])
                   if selected != "+ New Employee" else [])
            if _wh:
                _cur_rate = _wh[-1]["rate"]
                st.markdown(f"**Current Rate:** ${_cur_rate:.2f}/hr")
                wh_rows = []
                for _wh_entry in reversed(_wh):
                    wh_rows.append({
                        "Effective": _wh_entry.get("effective_date",
                                                    "Unknown"),
                        "Rate": f"${_wh_entry['rate']:.2f}/hr",
                        "Note": _wh_entry.get("note", ""),
                    })
                st.dataframe(
                    pd.DataFrame(wh_rows), use_container_width=True,
                    hide_index=True,
                    height=min(len(wh_rows) * 35 + 38, 200))

                # ── Wage Change Letter ──
                if len(_wh) >= 2:
                    _prev_rate = _wh[-2]["rate"]
                    _prev_eff = _wh[-2].get("effective_date", "")
                    _cur_eff = _wh[-1].get("effective_date", "")
                    if st.button("Generate Wage Change Letter",
                                 key="btn_wage_letter"):
                        try:
                            _wl_path = generate_wage_change_letter(
                                selected,
                                load_profile(selected),
                                _prev_rate, _cur_rate,
                                _cur_eff,
                                note=_wh[-1].get("note", ""))
                            st.session_state["_wage_letter_path"] = _wl_path
                            st.success(
                                f"Wage change letter generated: "
                                f"${_prev_rate:.2f} → ${_cur_rate:.2f}")
                        except Exception as _e:
                            st.error(f"Error: {_e}")
                    if st.session_state.get("_wage_letter_path"):
                        _wlp = st.session_state["_wage_letter_path"]
                        _wlp_bytes = _get_pdf_bytes(_wlp)
                        if _wlp_bytes:
                            st.download_button(
                                f"Download {os.path.basename(_wlp)}",
                                data=_wlp_bytes,
                                file_name=os.path.basename(_wlp),
                                mime="application/pdf",
                                key="dl_wage_letter")
            else:
                st.caption("No rate set. Add a wage entry below "
                           "to set the starting rate.")

            _wc_label = ("Add Wage Change" if _wh
                         else "Set Starting Rate")
            with st.expander(_wc_label):
                wc_c1, wc_c2 = st.columns(2)
                with wc_c1:
                    wc_rate = st.number_input(
                        "Rate ($/hr)", min_value=0.0,
                        step=0.25, format="%.2f", key="wc_rate")
                with wc_c2:
                    wc_date = st.date_input(
                        "Effective Date", value=date.today(),
                        key="wc_date")
                wc_note = st.text_input(
                    "Note", key="wc_note",
                    placeholder="e.g., Annual raise")
                if st.button("Save Wage Change"):
                    if wc_rate > 0 and employee_name.strip():
                        add_wage_change(employee_name.strip(),
                                        wc_rate, wc_date, wc_note)
                        st.session_state.save_msg = (
                            f"Wage change added: ${wc_rate:.2f}/hr "
                            f"effective {wc_date}")
                        st.rerun()
                    elif wc_rate <= 0:
                        st.warning("Rate must be greater than $0.")
                    else:
                        st.warning("Enter an employee name first.")

        # ── Emergency Contact ──
        with st.expander("Emergency Contact", expanded=_is_new):
            ec_c1, ec_c2 = st.columns(2)
            with ec_c1:
                emerg_name  = st.text_input("Contact Name",
                                            key="f_emerg_name")
            with ec_c2:
                emerg_phone = st.text_input("Contact Phone",
                                            key="f_emerg_phone",
                                            placeholder="(780) 555-1234")

        # ── Save Employee Info (without generating payroll) ──
        _INVALID_PATH_CHARS = set('\\/:*?"<>|')
        if st.button("Save Employee Info"):
            _bad_chars = _INVALID_PATH_CHARS & set(employee_name)
            _sin_digits = "".join(c for c in str(sin) if c.isdigit())
            if _bad_chars:
                st.error(f"Employee name contains invalid characters: "
                         f"{'  '.join(sorted(_bad_chars))}")
            elif sin and len(_sin_digits) != 9:
                st.error("SIN must be exactly 9 digits (e.g. 123 456 789)")
            elif employee_name.strip():
                save_profile(employee_name.strip(), {
                    "employee_name":    employee_name.strip(),
                    "employee_id":      employee_id,
                    "employee_address": [addr1, addr2, fmt_postal(addr3)],
                    "position":         position,
                    "payment_method":   payment_method,
                    "schedule_type":    schedule_type,
                    "sin":              fmt_sin(sin),
                    "dob":              dob.isoformat(),
                    "phone":            fmt_phone(phone),
                    "email":            email,
                    "employment_type":  employment_type,
                    "province":         province,
                    "start_date":       start_date.isoformat(),
                    "end_date":         end_date.isoformat() if end_date else None,
                    "emergency_contact_name":  emerg_name,
                    "emergency_contact_phone": fmt_phone(emerg_phone) if emerg_phone else "",
                    "bank_institution": bank_inst,
                    "bank_transit":     bank_transit,
                    "bank_account":     bank_account,
                    "td1_fed_claim":       td1_fed,
                    "td1_prov_claim":      td1_prov,
                    "td1_extra_deduction": td1_extra,
                    "td1_exempt":          td1_exempt,
                    "part_time": {
                        "enabled":      _is_pt,
                        "days_per_week": pt_days_pw,
                        "hours_per_day": pt_hours_pd,
                        "pay_periods":   pt_pay_periods,
                    },
                })
                st.session_state.save_msg = "Employee info saved."
                st.rerun()
            else:
                st.warning("Enter an employee name first.")

        # Show save confirmation (survives rerun)
        if st.session_state.save_msg:
            st.success(st.session_state.save_msg)
            st.session_state.save_msg = None

        # ── Verification Letter ──
        if (selected != "+ New Employee"
                and load_profile(selected).get("start_date")
                and load_profile(selected).get("wage_history")):
            st.markdown("---")
            if st.button("Generate Verification Letter",
                         key="btn_verify_letter"):
                try:
                    _vl_path = generate_verification_letter(
                        selected, load_profile(selected))
                    st.session_state["_verify_letter_path"] = _vl_path
                    st.success(
                        f"Verification letter generated: "
                        f"{os.path.basename(_vl_path)}")
                except Exception as _e:
                    st.error(f"Error: {_e}")
            if st.session_state.get("_verify_letter_path"):
                _vlp = st.session_state["_verify_letter_path"]
                _vlp_bytes = _get_pdf_bytes(_vlp)
                if _vlp_bytes:
                    st.download_button(
                        f"Download {os.path.basename(_vlp)}",
                        data=_vlp_bytes,
                        file_name=os.path.basename(_vlp),
                        mime="application/pdf",
                        key="dl_verify_letter")

    # ── Right: Pay Cycle ──
    with col2:
        st.subheader("Pay Cycle")
        today = date.today()
        pc1, pc2 = st.columns(2)
        with pc1:
            pay_month = st.selectbox("Payment Month", MONTHS,
                                     index=today.month - 1)
        with pc2:
            pay_year = st.number_input("Year", min_value=2024, max_value=2035,
                                       value=today.year, step=1)

        pay_cycle = st.radio("Pay Date", ["1st", "15th"], horizontal=True)
        month_idx = MONTHS.index(pay_month) + 1

        # Guard: can't set payment date more than 1 month into the future
        max_future_month = today.month + 1
        max_future_year  = today.year
        if max_future_month > 12:
            max_future_month = 1
            max_future_year += 1
        selected_too_far = (pay_year > max_future_year or
                            (pay_year == max_future_year and
                             month_idx > max_future_month))
        if selected_too_far:
            st.warning(f"Payment date can't be more than 1 month ahead "
                       f"(max: {MONTHS[max_future_month - 1]} {max_future_year})")

        try:
            auto_from, auto_to, auto_pay = get_pay_period(pay_year, month_idx, pay_cycle)
        except Exception as _pp_err:
            st.error(f"Could not calculate pay period: {_pp_err}")
            st.stop()

        st.caption(f"Period: **{format_date(auto_from)}** → **{format_date(auto_to)}**  ·  "
                   f"Payment: **{format_date(auto_pay)}**")

        tweak = st.checkbox("Adjust dates manually")
        if tweak:
            tc1, tc2, tc3 = st.columns(3)
            with tc1:
                final_from = st.date_input("Period From", value=auto_from)
            with tc2:
                final_to = st.date_input("Period To", value=auto_to)
            with tc3:
                final_pay = st.date_input("Payment Date", value=auto_pay)
        else:
            final_from, final_to, final_pay = auto_from, auto_to, auto_pay

        # ── Employer Cost Analysis ──
        if selected != "+ New Employee":
            _pr_cost = load_profile(selected)
            _wh_cost = _pr_cost.get("wage_history", [])
            if _wh_cost:
                _cost = _compute_employer_cost(selected, _wh_cost)
                if _cost:
                    st.subheader("Employer Cost Analysis")

                    _nom = _wh_cost[-1]["rate"]
                    _act = _cost["actualized_rate"]
                    _markup_pct = (
                        (_act / _nom - 1) * 100
                        if _nom > 0 else 0)

                    _cc1, _cc2, _cc3 = st.columns(3)
                    _cc1.metric("Nominal Rate",
                                f"${_nom:.2f}/hr")
                    _cc2.metric("Actualized Rate",
                                f"${_act:.2f}/hr")
                    _cc3.metric("Employer Markup",
                                f"+{_markup_pct:.1f}%")

                    st.caption(
                        f"Based on {_cost['entry_count']} payrolls"
                        f" · {_cost['total_est_hours']:.0f}"
                        f" est. hours")

                    with st.expander("Calculations"):
                        _cpp2_line = ""
                        if _cost.get("total_cpp2_er", 0) > 0:
                            _cpp2_line = (
                                f"+ Employer CPP2:    "
                                f"${_cost['total_cpp2_er']:>10,.2f}\n")
                        st.code(
                            f"  Gross Pay:        "
                            f"${_cost['total_gross']:>10,.2f}\n"
                            f"+ Employer CPP:     "
                            f"${_cost['total_cpp_er']:>10,.2f}\n"
                            f"{_cpp2_line}"
                            f"+ Employer EI:      "
                            f"${_cost['total_ei_er']:>10,.2f}\n"
                            f"+ WCB Premium:      "
                            f"${_cost['total_wcb']:>10,.2f}"
                            f"  (${WCB_RATE_PER_100:.2f}/$100)\n"
                            f"{'─' * 40}\n"
                            f"  Total Cost:       "
                            f"${_cost['total_cost']:>10,.2f}\n"
                            f"÷ Est. Hours:       "
                            f"{_cost['total_est_hours']:>10.0f}\n"
                            f"{'─' * 40}\n"
                            f"  Actualized Rate:  "
                            f"${_cost['actualized_rate']:>10.2f}"
                            f"/hr",
                            language=None)

                        st.markdown("**Monthly Breakdown**")
                        _m_rows = []
                        for key in sorted(
                                _cost["monthly"].keys()):
                            m = _cost["monthly"][key]
                            _yr, _mo = key.split("-")
                            _m_rows.append({
                                "Month": (
                                    f"{MONTHS[int(_mo)-1]}"
                                    f" {_yr}"),
                                "Gross":
                                    f"${m['gross']:,.2f}",
                                "Er CPP":
                                    f"${m['cpp_er']:,.2f}",
                                "Er EI":
                                    f"${m['ei_er']:,.2f}",
                                "WCB":
                                    f"${m['wcb']:,.2f}",
                                "Total Cost":
                                    f"${m['total_cost']:,.2f}",
                                "Cost/hr":
                                    f"${m['actualized_rate']:.2f}",
                            })
                        st.dataframe(
                            pd.DataFrame(_m_rows),
                            use_container_width=True,
                            hide_index=True)

                        st.caption(
                            "Actualized rate = total employer"
                            " cost ÷ estimated hours. Includes"
                            " gross pay + employer CPP match +"
                            " employer EI (1.4×) + WCB premium"
                            f" (${WCB_RATE_PER_100:.2f}/$100"
                            " insurable earnings).")

        # ── Vacation Pay Accrual ──
        if selected != "+ New Employee":
            _vac_data = get_vacation_accrual(selected)
            if _vac_data and _vac_data["vacation_years"]:
                _vac_start = _vac_data["start_date"]
                _vac_end = st.session_state.get("f_end_date")
                _vac_eligible = is_vacation_eligible(
                    _vac_start, end_date=_vac_end)

                if _vac_eligible:
                    st.subheader("Vacation Pay Accrual")
                    st.caption(
                        f"Annual payout · Anniversary: "
                        f"{_vac_start.strftime('%B %d')}")
                elif _vac_end:
                    # Terminated before 1 year — 4% owed per ES Code s.42
                    st.subheader("Vacation Pay — Termination Payout")
                    st.info(
                        "**Not vacation-eligible** (< 1 year employed). "
                        "Per Alberta ES Code s.42: *\"If employment "
                        "terminates before an employee is entitled to "
                        "take a first vacation, the employer must pay "
                        "4% of the employee's wages earned during "
                        "employment.\"*")
                else:
                    # Active but < 1 year — no entitlement yet
                    _first_anniv = date(
                        _vac_start.year + 1,
                        _vac_start.month, _vac_start.day)
                    st.subheader("Vacation Pay — Not Yet Eligible")
                    st.caption(
                        f"No vacation entitlement until 1 year of "
                        f"employment · Eligible: "
                        f"{_first_anniv.strftime('%B %d, %Y')}")
                    st.info(
                        "Employee has worked less than 1 year. "
                        "No vacation time or pay is owed unless "
                        "employment ends (4% termination payout).")

                # Summary metrics
                _vc1, _vc2, _vc3 = st.columns(3)
                _bal = _vac_data["balance"]
                if _vac_eligible:
                    _vc1.metric("Total Accrued",
                                f"${_vac_data['total_accrued']:,.2f}")
                    _vc2.metric("Total Paid",
                                f"${_vac_data['total_paid']:,.2f}")
                    _vc3.metric("Balance Owing",
                                f"${_bal:,.2f}",
                                delta=None if _bal == 0
                                else f"${_bal:,.2f} due")
                elif _vac_end:
                    # Terminated < 1 year — statutory payout, not accrual
                    _vc1.metric("Gross Wages Earned",
                                f"${_vac_data['total_accrued'] / 0.04:,.2f}"
                                if _vac_data['total_accrued'] > 0
                                else "$0.00")
                    _vc2.metric("Paid Out",
                                f"${_vac_data['total_paid']:,.2f}")
                    _vc3.metric("Statutory Payout (4%)",
                                f"${_bal:,.2f}",
                                delta=None if _bal == 0
                                else f"${_bal:,.2f} owing")
                else:
                    # Active < 1 year — no entitlement, show contingent
                    _vc1.metric("Gross Wages to Date",
                                f"${_vac_data['total_accrued'] / 0.04:,.2f}"
                                if _vac_data['total_accrued'] > 0
                                else "$0.00")
                    _vc2.metric("Vacation Entitlement", "$0.00")
                    _vc3.metric("If Terminated (4%)",
                                f"${_vac_data['total_accrued']:,.2f}")

                # Year-by-year detail
                _vac_rows = []
                for vy in _vac_data["vacation_years"]:
                    _src = vy["data_source"]
                    if _src == "none":
                        _src_label = "No data"
                    elif _src == "manual":
                        _src_label = "Manual"
                    else:
                        _src_label = f"{vy['entry_count']} payrolls"

                    _vac_status = vy["status"]
                    if _vac_status == "final":
                        _vac_status_label = "Terminated"
                    elif _vac_status == "complete" and not _vac_eligible:
                        _vac_status_label = "Ended (< 1 yr)"
                    else:
                        _vac_status_label = _vac_status.title()

                    # Use correct column labels based on eligibility
                    _amt_label = ("Accrued" if _vac_eligible
                                  else "4% Payout")
                    _bal_label = ("Balance" if _vac_eligible
                                  else "Owing")

                    _vac_rows.append({
                        "Year": f"Year {vy['year_num']}",
                        "Period": (f"{vy['from'].strftime('%b %Y')} – "
                                   f"{vy['to'].strftime('%b %Y')}"),
                        "Status": _vac_status_label,
                        "Gross Wages": f"${vy['gross']:,.2f}",
                        "Rate": f"{vy['rate']:.0%}",
                        _amt_label: f"${vy['accrued']:,.2f}",
                        "Paid": f"${vy['paid']:,.2f}",
                        _bal_label: f"${vy['balance']:,.2f}",
                        "Source": _src_label,
                    })

                def _style_vac(df):
                    s = pd.DataFrame("", index=df.index,
                                     columns=df.columns)
                    for idx in df.index:
                        _st = df.loc[idx, "Status"]
                        if _st == "Current":
                            s.loc[idx, "Status"] = (
                                "background-color: #e3f2fd; "
                                "font-weight: bold")
                        elif _st == "Complete":
                            s.loc[idx, "Status"] = (
                                "background-color: #d4edda; "
                                "color: #155724")
                        elif _st in ("Terminated", "Ended (< 1 yr)"):
                            s.loc[idx, "Status"] = (
                                "background-color: #f8d7da; "
                                "color: #721c24; "
                                "font-weight: bold")
                        if df.loc[idx, "Source"] == "No data":
                            s.loc[idx, :] = (
                                "background-color: #fff3e0; "
                                "color: #e65100")
                        # Highlight the amount column
                        if _amt_label in df.columns:
                            s.loc[idx, _amt_label] = (
                                "background-color: #e3f2fd; "
                                "font-weight: bold")
                    return s

                _df_vac = pd.DataFrame(_vac_rows)
                _styled_vac = _df_vac.style.apply(
                    _style_vac, axis=None)
                st.dataframe(_styled_vac,
                             use_container_width=True,
                             hide_index=True)

                # Warn about missing data
                _no_data = [vy for vy in _vac_data["vacation_years"]
                            if vy["data_source"] == "none"]
                if _no_data:
                    st.warning(
                        f"{len(_no_data)} vacation year(s) have no "
                        f"gross wage data. Add manual overrides "
                        f"below to calculate accrual.")

                # Manual gross override + record payout
                with st.expander("Manage Vacation Pay"):
                    _vt2, _vt1, _vt3 = st.tabs(
                        ["Record Payout", "Set Gross Override",
                         "Delete Payout"])

                    with _vt1:
                        st.caption(
                            "Enter total gross wages for vacation "
                            "years without remittance data.")
                        _override_years = [
                            vy for vy in _vac_data["vacation_years"]
                            if vy["data_source"] in ("none", "manual")]
                        if not _override_years:
                            st.info("All vacation years have "
                                    "remittance data.")
                        else:
                            _ov_labels = [
                                f"Year {vy['year_num']}: "
                                f"{vy['from'].isoformat()} to "
                                f"{vy['to'].isoformat()}"
                                for vy in _override_years]
                            _ov_sel = st.selectbox(
                                "Vacation year",
                                range(len(_ov_labels)),
                                format_func=lambda i: _ov_labels[i],
                                key="vac_ov_sel")
                            _sel_vy = _override_years[_ov_sel]
                            _ov_amount = st.number_input(
                                "Total Gross Wages ($)",
                                min_value=0.0,
                                value=float(_sel_vy["gross"]),
                                step=100.0, format="%.2f",
                                key="vac_ov_amount")
                            if st.button("Save Override"):
                                set_gross_override(
                                    selected,
                                    _sel_vy["from"].year,
                                    _sel_vy["to"].year,
                                    _ov_amount)
                                st.success(
                                    f"Gross override saved: "
                                    f"${_ov_amount:,.2f}")
                                st.rerun()

                    with _vt2:
                        st.caption(
                            "Record an annual vacation pay payout.")
                        _pay_years = [
                            vy for vy in _vac_data["vacation_years"]
                            if vy["balance"] > 0]
                        if not _pay_years:
                            st.info("No outstanding vacation pay "
                                    "balance.")
                        else:
                            _py_labels = [
                                f"Year {vy['year_num']} — "
                                f"${vy['balance']:,.2f} owing"
                                for vy in _pay_years]
                            _py_sel = st.selectbox(
                                "Vacation year",
                                range(len(_py_labels)),
                                format_func=lambda i: _py_labels[i],
                                key="vac_py_sel")
                            _sel_py = _pay_years[_py_sel]
                            _vpc1, _vpc2 = st.columns(2)
                            with _vpc1:
                                _py_amount = st.number_input(
                                    "Payout Amount ($)",
                                    min_value=0.0,
                                    value=float(
                                        _sel_py["balance"]),
                                    step=0.01, format="%.2f",
                                    key="vac_py_amount")
                            with _vpc2:
                                _py_date = st.date_input(
                                    "Payout Date",
                                    value=date.today(),
                                    key="vac_py_date")
                            _py_method = st.selectbox(
                                "Method",
                                ["Cheque", "E-Transfer",
                                 "Direct Deposit (EFT)",
                                 "Added to Payroll"],
                                key="vac_py_method")
                            if st.button("Record Payout",
                                         type="primary"):
                                record_vacation_payout(
                                    selected, _py_amount,
                                    _sel_py["to"],
                                    _py_date, _py_method)
                                st.success(
                                    f"Vacation payout of "
                                    f"${_py_amount:,.2f} recorded.")
                                st.rerun()

                    with _vt3:
                        st.caption(
                            "Remove an incorrectly recorded "
                            "vacation payout.")
                        _all_payouts = _vac_data.get("payouts", [])
                        if not _all_payouts:
                            st.info("No recorded payouts to delete.")
                        else:
                            _del_labels = [
                                f"{p['date']} — ${p['amount']:,.2f} "
                                f"({p.get('method', '—')}) "
                                f"for year ending {p['year_ending']}"
                                for p in _all_payouts]
                            _del_sel = st.selectbox(
                                "Select payout to delete",
                                range(len(_del_labels)),
                                format_func=lambda i: _del_labels[i],
                                key="vac_del_sel")
                            _del_entry = _all_payouts[_del_sel]
                            st.warning(
                                f"**{_del_entry['date']}** — "
                                f"${_del_entry['amount']:,.2f} via "
                                f"{_del_entry.get('method', '—')}")
                            if st.button("Delete This Payout",
                                         type="primary",
                                         key="btn_vac_del_req"):
                                st.session_state["_vac_del_confirm"] = _del_sel
                                st.rerun()
                            if st.session_state.get("_vac_del_confirm") is not None:
                                st.warning(
                                    "Confirm deletion — this will be "
                                    "moved to the recovery bin.")
                                _vdc1, _vdc2, _ = st.columns([1, 1, 4])
                                with _vdc1:
                                    if st.button("Yes, Delete",
                                                 type="primary",
                                                 key="btn_vac_del_yes"):
                                        delete_vacation_payout(
                                            selected,
                                            st.session_state.pop(
                                                "_vac_del_confirm"))
                                        st.success("Payout moved to recovery bin.")
                                        st.rerun()
                                with _vdc2:
                                    if st.button("Cancel",
                                                 key="btn_vac_del_cancel"):
                                        st.session_state.pop(
                                            "_vac_del_confirm", None)
                                        st.rerun()

    # ══════════════════════════════════════════════════════════
    #  EARNINGS — full-width, prominent
    # ══════════════════════════════════════════════════════════
    st.markdown("---")

    # Auto-rate lookup based on pay period
    _pr = (load_profile(selected) if selected != "+ New Employee"
           else {})
    _wh_earn = _pr.get("wage_history", [])
    if _wh_earn:
        rate = get_rate_for_date(_wh_earn, final_pay)
        _earn_cur_rate = _wh_earn[-1]["rate"]
    else:
        rate = 0.0
        _earn_cur_rate = 0.0

    st.subheader("Earnings")

    # ── Schedule-aware OT threshold info ──
    _sched_key = _pr.get("schedule_type", DEFAULT_SCHEDULE_TYPE)
    _sched = get_schedule(_sched_key)
    if _sched["is_averaging"]:
        st.info(
            f"**{_sched['label']}** \u2014 "
            f"OT after {_sched['ot_daily_threshold']} hrs/day or "
            f"{_sched['ot_weekly_threshold']} hrs/week at "
            f"{_sched['ot_multiplier']}\u00d7 rate.")
    else:
        st.caption(
            f"Schedule: {_sched['label']} \u2014 "
            f"OT after {_sched['ot_daily_threshold']} hrs/day or "
            f"{_sched['ot_weekly_threshold']} hrs/week")

    if rate <= 0:
        st.warning("No rate set. Add a wage entry in "
                   "Employment Details.")
    elif _wh_earn and abs(rate - _earn_cur_rate) > 0.001:
        st.info(
            f"Historical rate for this period: "
            f"**${rate:.2f}/hr** — current rate is "
            f"${_earn_cur_rate:.2f}/hr")

    _reg_label = "Regular Hours"
    if rate > 0:
        _reg_label += f"  ·  ${rate:.2f}/hr"
    hours = st.number_input(_reg_label, min_value=0.0, max_value=200.0,
                            value=0.0, step=0.25, format="%.2f",
                            key="f_hours")

    # ── Overtime ──
    ot_rate_val = overtime_rate(rate) if rate > 0 else 0.0
    stat_rate_val = stat_rate(rate) if rate > 0 else 0.0

    _ot_label = "Overtime Hours"
    if rate > 0:
        _ot_label += f"  ·  ${ot_rate_val:.2f}/hr (1.5×)"
    ot_hours = st.number_input(_ot_label, min_value=0.0, max_value=200.0,
                               value=0.0, step=0.25, format="%.2f")

    # Get employee schedule for days-per-week estimate
    # Part-time override takes precedence over schedule default
    _emp_sched_key = st.session_state.get(
        "f_schedule_type", DEFAULT_SCHEDULE_TYPE)
    _emp_sched = get_schedule(_emp_sched_key)
    if st.session_state.get("f_emp_type") == "Part-Time":
        _days_pw = int(st.session_state.get("f_pt_days_pw", 3))
    else:
        _days_pw = _emp_sched.get("days_per_week", 5)

    # ── Statutory Holiday ──
    stats_in_period = get_stats_in_period(final_from, final_to)
    _stat_eligible = is_stat_eligible(start_date, as_of=final_pay,
                                      days_per_week=_days_pw,
                                      end_date=end_date)
    _has_stats = bool(stats_in_period)

    # Auto-calculate stat holiday pay from prior payroll data
    _auto_stat_pay = 0.0
    _stat_pay_entries = 0
    _stat_est_days = 0
    if (_has_stats and _stat_eligible
            and selected != "+ New Employee"
            and employee_name.strip()):
        _auto_stat_pay, _stat_pay_entries, _stat_est_days = (
            _compute_stat_holiday_pay(
                employee_name.strip(), stats_in_period[0][0],
                days_per_week=_days_pw))

    if _has_stats:
        # Build per-stat info
        _stat_details = []
        _any_day_off = False
        _all_day_off = True
        for s_date, s_name, s_is_day_off in stats_in_period:
            _stat_details.append({
                "date": s_date, "name": s_name,
                "is_day_off": s_is_day_off})
            if s_is_day_off:
                _any_day_off = True
            else:
                _all_day_off = False

        stat_names = ", ".join(
            f"{s['name']} ({s['date'].strftime('%b %d')})"
            for s in _stat_details)

        # ── Info banner + options ──
        if not _stat_eligible:
            st.warning(
                f"**Stat Holiday in Period:** {stat_names}  \n"
                f"Employee not eligible (< 30 workdays employed "
                f"\u2014 requires ~6 weeks)")
            # Not eligible employees can still work (regular pay only)
            _ne_sel = st.radio(
                "Did employee work?",
                ["No (No Pay)", "Yes (Regular Pay Only)"],
                horizontal=True, key="stat_ne_worked")
            if _ne_sel == "Yes (Regular Pay Only)":
                stat_worked_sel = "Worked (Regular)"
                st.caption(
                    "Not eligible \u2014 hours count as regular pay, "
                    "no stat premium")
            else:
                stat_worked_sel = "Not Eligible"
        else:
            # Determine smart default for eligible employees
            _options = [
                "Worked 1.5\u00d7",
                "Banked (Substitute Day)",
                "Paid Day Off",
                "Not Eligible",
            ]
            if _all_day_off:
                _day_off_names = ", ".join(
                    s["name"] for s in _stat_details if s["is_day_off"])
                st.info(
                    f"**Stat Holiday in Period:** {stat_names}  \n"
                    f"Company policy: {_day_off_names} "
                    f"= paid day off (not worked)")
                _default_idx = 2  # "Paid Day Off"
            elif _any_day_off:
                st.info(f"**Stat Holiday in Period:** {stat_names}")
                _default_idx = 2
            else:
                st.info(
                    f"**Stat Holiday in Period:** {stat_names}  \n"
                    f"Company policy: mandatory work day at 1.5\u00d7")
                _default_idx = 0  # "Worked 1.5×"

            stat_worked_sel = st.radio(
                "Stat Status", _options,
                index=_default_idx,
                horizontal=True, key="stat_worked")

        _stat_help = (
            f"Alberta ESA: 4-week wages (excl. OT & holiday pay) "
            f"\u00f7 days worked (~{_stat_est_days})"
            if _stat_est_days > 0
            else "Avg daily wage \u2014 4-week wages \u00f7 days worked")

        if stat_worked_sel == "Worked 1.5\u00d7":
            # Eligible + worked: avg daily wage + 1.5× for hours
            sh_c1, sh_c2 = st.columns(2)
            with sh_c1:
                _sh_label = "Stat Hours Worked"
                if rate > 0:
                    _sh_label += (f"  \u00b7  ${stat_rate_val:.2f}/hr "
                                  f"(1.5\u00d7)")
                stat_hours = st.number_input(
                    _sh_label, min_value=0.0, max_value=200.0,
                    value=0.0, step=0.25, format="%.2f")
            with sh_c2:
                holiday_pay = st.number_input(
                    "Stat Holiday Pay ($)", min_value=0.0,
                    value=float(_auto_stat_pay),
                    step=0.01, format="%.2f",
                    help=_stat_help,
                    key="f_stat_hol_pay_w")
        elif stat_worked_sel == "Paid Day Off":
            # Eligible + day off: avg daily wage only
            stat_hours = 0.0
            holiday_pay = st.number_input(
                "Stat Holiday Pay ($)", min_value=0.0,
                value=float(_auto_stat_pay),
                step=0.01, format="%.2f",
                help=_stat_help,
                key="f_stat_hol_pay_d")
        elif stat_worked_sel == "Banked (Substitute Day)":
            # Eligible + worked at regular rate, day off banked for later
            st.caption(
                "Employee works at regular rate. A substitute paid "
                "day off (avg daily wage) is owed at a later date.")
            stat_hours = 0.0  # hours go into regular, not stat premium
            holiday_pay = 0.0  # banked, not paid this period
            _bank_hrs = st.number_input(
                "Hours Worked (added to regular)", min_value=0.0,
                value=0.0, step=0.25, format="%.2f",
                key="f_stat_bank_hrs")
            # Add banked hours to regular hours for this period
            hours = hours + _bank_hrs
            if _bank_hrs > 0:
                st.caption(
                    f"Includes {_bank_hrs:.2f} banked stat hrs "
                    f"({hours - _bank_hrs:.2f} + {_bank_hrs:.2f} "
                    f"= **{hours:.2f} hrs** total regular)")
        elif stat_worked_sel == "Worked (Regular)":
            # Not eligible but worked: regular rate only
            stat_hours = 0.0
            holiday_pay = 0.0
            _ne_hrs = st.number_input(
                "Hours Worked (regular rate)", min_value=0.0,
                value=0.0, step=0.25, format="%.2f",
                key="f_stat_ne_hrs")
            hours = hours + _ne_hrs
            if _ne_hrs > 0:
                st.caption(
                    f"Includes {_ne_hrs:.2f} stat-worked hrs at regular rate "
                    f"({hours - _ne_hrs:.2f} + {_ne_hrs:.2f} "
                    f"= **{hours:.2f} hrs** total regular)")
        elif stat_worked_sel == "Not Eligible":
            # Manual override — employer marks employee as not eligible
            st.caption(
                "Override: employee marked as not eligible for stat "
                "holiday pay (e.g., did not work scheduled shift "
                "before/after the holiday).")
            _ne_sel2 = st.radio(
                "Did employee work on the stat?",
                ["No (No Pay)", "Yes (Regular Pay Only)"],
                horizontal=True, key="stat_ne_override")
            if _ne_sel2 == "Yes (Regular Pay Only)":
                stat_hours = 0.0
                holiday_pay = 0.0
                _ne_hrs2 = st.number_input(
                    "Hours Worked (regular rate)", min_value=0.0,
                    value=0.0, step=0.25, format="%.2f",
                    key="f_stat_ne_hrs2")
                hours = hours + _ne_hrs2
            else:
                stat_hours = 0.0
                holiday_pay = 0.0
        else:
            # Fallback
            stat_hours = 0.0
            holiday_pay = 0.0

        if _stat_pay_entries > 0 and _stat_eligible:
            st.caption(
                f"Estimate: ${_auto_stat_pay:,.2f} "
                f"({_stat_pay_entries} payroll(s) in 4 weeks "
                f"\u00f7 {_stat_est_days} est. days worked)")
    else:
        stat_hours = 0.0
        holiday_pay = 0.0

    # ── Vacation Pay (toggle) ──
    _vac_eligible = is_vacation_eligible(start_date, as_of=final_pay,
                                         end_date=end_date)
    if st.checkbox("Show Vacation Pay", value=_vac_eligible,
                   key="show_vacation",
                   help="Show vacation pay as a line item on this paystub. "
                        "Uncheck to defer — accrual is still tracked in the "
                        "Vacation tracker regardless. Use when paying vacation "
                        "annually, on anniversary, or as a separate payment."):
        if _vac_eligible:
            yos = years_of_service(start_date, as_of=final_pay,
                                   end_date=end_date)
            vp_rate = vacation_pay_rate(yos)
            _vac_pct = f"{vp_rate:.0%}"
            _vac_tier = ("1–5 years" if yos < 5 else "5+ years")
            # Total gross before vacation (regular + OT + stat + holiday)
            _vp_gross = round(rate * hours, 2)
            if ot_hours > 0:
                _vp_gross += round(ot_rate_val * ot_hours, 2)
            if stat_hours > 0:
                _vp_gross += round(stat_rate_val * stat_hours, 2)
            _vp_gross += holiday_pay
            st.caption(
                f"{_vac_pct} · {_vac_tier} · "
                f"since {start_date.strftime('%b %Y')}")
            expected_vp = round(_vp_gross * vp_rate, 2) if _vp_gross > 0 else 0.0
            vacation_pay = st.number_input(
                "Vacation Pay ($)", min_value=0.0,
                value=float(expected_vp), step=0.01, format="%.2f")
            if _vp_gross > 0:
                actual_vp_pct = (vacation_pay / _vp_gross * 100
                                 if _vp_gross > 0 else 0.0)
                off = abs(vacation_pay - expected_vp) > 0.01
                color = "red" if off else "grey"
                _vp_msg = f"{actual_vp_pct:.1f}% of ${_vp_gross:,.2f} gross"
                if off:
                    _vp_msg += (f" · expected {_vac_pct} = "
                                f"${expected_vp:,.2f}")
                st.markdown(
                    f"<span style='font-size:12px; color:{color}'>"
                    f"{_vp_msg}</span>",
                    unsafe_allow_html=True)
        else:
            st.caption("Not eligible (< 1 year employed) — $0")
            vacation_pay = 0.0
    else:
        vacation_pay = 0.0

    # ── Commission (toggle) ──
    if st.checkbox("Commission", key="show_commission"):
        commission = st.number_input(
            "Commission ($)", min_value=0.0,
            value=0.0, step=0.01, format="%.2f")
    else:
        commission = 0.0

    # ── Deductions ───
    st.markdown("---")
    st.subheader("Deductions")

    # Estimate button
    gross_for_est = (round(rate * hours, 2)
        + (round(ot_rate_val * ot_hours, 2) if ot_hours > 0 else 0)
        + (round(stat_rate_val * stat_hours, 2) if stat_hours > 0 else 0)
        + holiday_pay + vacation_pay
        + commission)

    if st.button(f"Estimate Deductions (CRA {pay_year})") and gross_for_est > 0:
        est = estimate_deductions(gross_for_est, pay_periods=pt_pay_periods,
                                  tax_year=pay_year, pay_month=month_idx,
                                  td1_fed_claim=td1_fed, td1_prov_claim=td1_prov,
                                  td1_extra=td1_extra, td1_exempt=td1_exempt)
        st.session_state["f_cpp"]      = est["cpp"]
        st.session_state["f_cpp2"]     = est["cpp2"]
        st.session_state["f_ei"]       = est["ei"]
        st.session_state["f_fed_tax"]  = est["fed_tax"]
        st.session_state["f_prov_tax"] = est["prov_tax"]
        st.rerun()

    # Initialize deduction keys if not set
    for key in ["f_cpp", "f_cpp2", "f_ei", "f_fed_tax", "f_prov_tax"]:
        if key not in st.session_state:
            st.session_state[key] = 0.0

    # CRA expected amounts (live, for comparison)
    _cra = (estimate_deductions(gross_for_est, pay_periods=pt_pay_periods,
                                tax_year=pay_year, pay_month=month_idx,
                                td1_fed_claim=td1_fed, td1_prov_claim=td1_prov,
                                td1_extra=td1_extra, td1_exempt=td1_exempt)
            if gross_for_est > 0 else None)

    dc1, dc2, dc3, dc4, dc5 = st.columns(5)

    def _ded_caption(col, entered, cra_val):
        """Show live % under a deduction field, red if mismatched."""
        if gross_for_est <= 0:
            return
        actual_pct = entered / gross_for_est * 100
        off = cra_val is not None and abs(entered - cra_val) > 0.01
        color = "red" if off else "grey"
        msg = f"{actual_pct:.2f}%"
        if off:
            msg += f" · expected ${cra_val:,.2f}"
        col.markdown(
            f"<span style='font-size:12px; color:{color}'>"
            f"{msg}</span>",
            unsafe_allow_html=True)

    with dc1:
        cpp = st.number_input("CPP ($)", min_value=0.0, step=0.01,
                              format="%.2f", key="f_cpp")
        _ded_caption(dc1, cpp, _cra["cpp"] if _cra else None)
    with dc2:
        cpp2 = st.number_input("CPP2 ($)", min_value=0.0, step=0.01,
                               format="%.2f", key="f_cpp2")
        _ded_caption(dc2, cpp2, _cra["cpp2"] if _cra else None)
    with dc3:
        ei = st.number_input("EI ($)", min_value=0.0, step=0.01,
                             format="%.2f", key="f_ei")
        _ded_caption(dc3, ei, _cra["ei"] if _cra else None)
    with dc4:
        fed_tax = st.number_input("Federal Tax ($)", min_value=0.0, step=0.01,
                                  format="%.2f", key="f_fed_tax")
        _ded_caption(dc4, fed_tax, _cra["fed_tax"] if _cra else None)
    with dc5:
        prov_tax = st.number_input("Provincial Tax ($)", min_value=0.0, step=0.01,
                                   format="%.2f", key="f_prov_tax")
        _ded_caption(dc5, prov_tax, _cra["prov_tax"] if _cra else None)

    st.caption(f"Estimates based on CRA {pay_year} rates (T4127) — verify with your payroll advisor")

    # ── YTD (auto-calculated from remittance data) ───
    st.markdown("---")
    _ytd_current_ded = round(cpp + cpp2 + ei + fed_tax + prov_tax, 2)
    _ytd_current_net = round(gross_for_est - _ytd_current_ded, 2)

    _ytd_prior_gross = 0.0
    _ytd_prior_net = 0.0
    _ytd_prior_count = 0
    _ytd_prior_deds = {"cpp": 0, "cpp2": 0, "ei": 0,
                       "fed_tax": 0, "prov_tax": 0}
    if selected != "+ New Employee" and employee_name.strip():
        _ytd_prior_gross, _ytd_prior_net, _ytd_prior_count, _ytd_prior_deds, _ytd_skip = (
            _compute_ytd(employee_name.strip(), pay_year,
                         before_date=final_pay))
        if _ytd_skip:
            st.warning(f"YTD: {_ytd_skip} entry(s) skipped due to unparseable dates")

    _ytd_auto_gross = round(_ytd_prior_gross + gross_for_est, 2)
    _ytd_auto_net = round(_ytd_prior_net + _ytd_current_net, 2)
    # Per-deduction YTD (prior + current)
    _ytd_cpp = round(_ytd_prior_deds["cpp"] + cpp, 2)
    _ytd_cpp2 = round(_ytd_prior_deds["cpp2"] + cpp2, 2)
    _ytd_ei = round(_ytd_prior_deds["ei"] + ei, 2)
    _ytd_fed_tax = round(_ytd_prior_deds["fed_tax"] + fed_tax, 2)
    _ytd_prov_tax = round(_ytd_prior_deds["prov_tax"] + prov_tax, 2)

    ytd_override = st.checkbox("Override YTD values")
    if ytd_override:
        yc1, yc2 = st.columns(2)
        with yc1:
            ytd_gross_val = st.number_input(
                "YTD Gross ($)", min_value=0.0,
                value=float(_ytd_auto_gross),
                step=0.01, format="%.2f")
        with yc2:
            ytd_net_val = st.number_input(
                "YTD Net ($)", min_value=0.0,
                value=float(_ytd_auto_net),
                step=0.01, format="%.2f")
    else:
        ytd_gross_val = _ytd_auto_gross
        ytd_net_val = _ytd_auto_net

    if _ytd_prior_count > 0:
        st.caption(
            f"YTD auto-calculated: {_ytd_prior_count} prior "
            f"(${_ytd_prior_gross:,.2f}) + current "
            f"(${gross_for_est:,.2f}) = "
            f"**${ytd_gross_val:,.2f}** gross / "
            f"**${ytd_net_val:,.2f}** net")
    else:
        st.caption("First payroll of the year — YTD matches current period")

    # ── Live Calculations ───
    st.markdown("---")
    reg_total   = round(rate * hours, 2)
    ot_total    = round(ot_rate_val * ot_hours, 2) if ot_hours > 0 else 0
    stat_total  = round(stat_rate_val * stat_hours, 2) if stat_hours > 0 else 0
    total_gross = round(reg_total + ot_total + stat_total + holiday_pay + vacation_pay + commission, 2)
    total_ded   = round(cpp + cpp2 + ei + fed_tax + prov_tax, 2)
    net_pay     = round(total_gross - total_ded, 2)

    _op_style = ("font-size:28px; font-weight:700; color:#888; "
                 "text-align:center; padding-top:28px")
    mc1, op1, mc2, op2, mc3 = st.columns([3, 1, 3, 1, 3])
    mc1.metric("Gross Pay", f"${total_gross:,.2f}")
    op1.markdown(f"<div style='{_op_style}'>−</div>",
                 unsafe_allow_html=True)
    mc2.metric("Total Deductions", f"${total_ded:,.2f}")
    op2.markdown(f"<div style='{_op_style}'>=</div>",
                 unsafe_allow_html=True)
    mc3.metric("Net Pay", f"${net_pay:,.2f}")

    # ── Gross breakdown (only when extras beyond regular pay) ──
    _gross_lines = []
    if reg_total > 0:
        _gross_lines.append(f"Regular: ${reg_total:,.2f}")
    if ot_total > 0:
        _gross_lines.append(f"Overtime: ${ot_total:,.2f}")
    if stat_total > 0:
        _gross_lines.append(f"Stat Hours: ${stat_total:,.2f}")
    if holiday_pay > 0:
        _gross_lines.append(f"Stat Holiday Pay: ${holiday_pay:,.2f}")
    if vacation_pay > 0:
        _gross_lines.append(f"Vacation Pay: ${vacation_pay:,.2f}")
    if commission > 0:
        _gross_lines.append(f"Commission: ${commission:,.2f}")

    if len(_gross_lines) > 1:
        mc1.caption(" + ".join(_gross_lines))

    # ── Validation + Generate ───
    st.markdown("---")
    errors = []
    if not employee_name.strip():
        errors.append("Employee Name is required")
    if hours <= 0:
        errors.append("Regular Hours must be greater than 0")
    if rate <= 0:
        errors.append("Hourly rate must be greater than $0 — check wage history")
    if total_gross <= 0:
        errors.append("Gross pay is $0 — check rate and hours")
    if selected_too_far:
        errors.append("Payment date is too far in the future")
    for e in errors:
        st.warning(e)

    # Employer contributions for paystub
    _emp_portions = employer_portions(cpp, ei, cpp2)

    # Masked SIN for paystub (***-***-456)
    _sin_clean = re.sub(r'\D', '', sin) if sin else ""
    _sin_masked = (f"***-***-{_sin_clean[-3:]}"
                   if len(_sin_clean) >= 3 else "")

    # Vacation accrual info (historical + current period)
    # Only shown on paystub when the Vacation Pay toggle is on.
    _vac_rate = None
    _vac_balance = None
    if st.session_state.get("show_vacation", True) \
            and selected != "+ New Employee" and employee_name.strip():
        _vac_info = get_vacation_accrual(employee_name.strip())
        if _vac_info:
            # Use the current year's rate
            for _vy in _vac_info["vacation_years"]:
                if _vy["status"] == "current":
                    _vac_rate = _vy["rate"]
                    break
            if _vac_rate is None and _vac_info["vacation_years"]:
                _vac_rate = _vac_info["vacation_years"][-1]["rate"]
            _vac_balance = _vac_info["balance"]
            # Add current period's accrual ONLY for new payrolls
            # (on overwrite/regeneration the entry is already in remittances)
            _is_overwrite = st.session_state.get("_force_overwrite", False)
            if _vac_rate is not None and not _is_overwrite:
                _vac_balance = round(
                    _vac_balance + (gross_for_est * _vac_rate), 2)

    # Build paystub data dict (shared by preview + generate)
    _paystub_data = {
        "employer_name":    employer_name,
        "company_address":  _co.get("address_lines", []),
        "employee_name":    employee_name.strip(),
        "employee_id":      employee_id,
        "employee_address": [addr1, addr2, addr3],
        "position":         position,
        "payment_method":   payment_method,
        "schedule_type":    _pr.get("schedule_type", DEFAULT_SCHEDULE_TYPE),
        "sin_masked":       _sin_masked,
        "period_from":      format_date(final_from),
        "period_to":        format_date(final_to),
        "payment_date":     format_date(final_pay),
        "regular_rate":     rate,
        "regular_hours":    hours,
        "overtime_rate":    ot_rate_val if ot_hours > 0 else None,
        "overtime_hours":   ot_hours if ot_hours > 0 else None,
        "stat_rate":        stat_rate_val if stat_hours > 0 else None,
        "stat_hours":       stat_hours if stat_hours > 0 else None,
        "holiday_pay":      holiday_pay if holiday_pay > 0 else None,
        "vacation_pay":     vacation_pay if vacation_pay > 0 else None,
        "commission":       commission if commission > 0 else None,
        "cpp":              cpp,
        "cpp2":             cpp2,
        "ei":               ei,
        "fed_tax":          fed_tax,
        "prov_tax":         prov_tax,
        "cpp_employer":     _emp_portions["cpp_employer"],
        "cpp2_employer":    _emp_portions["cpp2_employer"],
        "ei_employer":      _emp_portions["ei_employer"],
        "ytd_gross":        ytd_gross_val,
        "ytd_net":          ytd_net_val,
        "ytd_cpp":          _ytd_cpp,
        "ytd_cpp2":         _ytd_cpp2,
        "ytd_ei":           _ytd_ei,
        "ytd_fed_tax":      _ytd_fed_tax,
        "ytd_prov_tax":     _ytd_prov_tax,
        "vacation_rate":    _vac_rate,
        "vacation_balance": _vac_balance,
        **{f"{k}_pdf": v
           for k, v in get_theme_assets(_co).items()},
        "logo_opacity":     0.15,
    }

    # ── Generate button ──
    _do_generate = st.button("Generate Payroll PDF", type="primary",
                             disabled=bool(errors),
                             use_container_width=True)

    # Clear cached preview when Generate fires so it re-renders after save
    if _do_generate:
        st.session_state.pop("_preview_img", None)
        st.session_state.pop("_preview_hash", None)

    # ── Live PDF Preview (auto-regenerates when paystub data changes) ──
    if not errors and employee_name.strip() and selected != "+ New Employee":
        _phash = hashlib.md5(
            json.dumps(_paystub_data, sort_keys=True, default=str).encode()
        ).hexdigest()
        if st.session_state.get("_preview_hash") != _phash:
            with st.spinner("Updating preview..."):
                try:
                    _tmp = tempfile.NamedTemporaryFile(suffix=".pdf",
                                                       delete=False)
                    _tmp.close()
                    generate_payroll(_paystub_data, output_path=_tmp.name)
                    _doc = fitz.open(_tmp.name)
                    _pix = _doc[0].get_pixmap(dpi=150)
                    st.session_state["_preview_img"] = _pix.tobytes("png")
                    st.session_state["_preview_hash"] = _phash
                    _doc.close()
                    os.remove(_tmp.name)
                except Exception as _e:
                    st.warning(f"Preview: {_e}")

    if st.session_state.get("_preview_img"):
        with st.expander("Paystub Preview", expanded=True):
            _pv_pad, _pv_center, _pv_pad2 = st.columns([1, 3, 1])
            with _pv_center:
                st.image(st.session_state["_preview_img"],
                         use_container_width=True)

    # ── Step 1: Generate PDF (must happen first) ──
    if _do_generate:
        st.session_state.pop("_preview_img", None)
        st.session_state.pop("_overwrite_conflict", None)
        emp_name = employee_name.strip()

        save_profile(emp_name, {
            "employee_name":    emp_name,
            "employee_id":      employee_id,
            "employee_address": [addr1, addr2, fmt_postal(addr3)],
            "position":         position,
            "payment_method":   payment_method,
            "schedule_type":    schedule_type,
            "regular_rate":     rate,
            "sin":              fmt_sin(sin),
            "dob":              dob.isoformat(),
            "phone":            fmt_phone(phone),
            "email":            email,
            "employment_type":  employment_type,
            "province":         province,
            "start_date":       start_date.isoformat(),
            "end_date":         end_date.isoformat() if end_date else None,
            "emergency_contact_name":  emerg_name,
            "emergency_contact_phone": fmt_phone(emerg_phone) if emerg_phone else "",
            "bank_institution": bank_inst,
            "bank_transit":     bank_transit,
            "bank_account":     bank_account,
            "td1_fed_claim":       td1_fed,
            "td1_prov_claim":      td1_prov,
            "td1_extra_deduction": td1_extra,
            "td1_exempt":          td1_exempt,
            "part_time": {
                "enabled":       _is_pt,
                "days_per_week": pt_days_pw,
                "hours_per_day": pt_hours_pd,
                "pay_periods":   pt_pay_periods,
            },
        })

        try:
            _force = st.session_state.pop("_force_overwrite", False)

            # Check for existing record BEFORE generating PDF
            _total_hours_worked = round(hours + ot_hours + stat_hours, 2)
            entry = log_payroll(
                payment_date_str=format_date(final_pay),
                employee=emp_name,
                period_from=format_date(final_from),
                period_to=format_date(final_to),
                gross=total_gross,
                cpp=cpp, ei=ei, fed_tax=fed_tax, prov_tax=prov_tax,
                pay_year=final_pay.year,
                pay_month=final_pay.month,
                pdf_path=None,
                force_overwrite=_force,
                cpp2=cpp2,
                hours=_total_hours_worked,
                ei_insurable_gross=total_gross,
                cpp_pensionable_gross=total_gross,
            )

            if isinstance(entry, dict) and entry.get("conflict"):
                old = entry["existing"]
                st.session_state["_overwrite_conflict"] = True
                st.error(
                    f"A payroll record already exists for "
                    f"**{emp_name}** on "
                    f"**{format_date(final_pay)}** "
                    f"(Gross: ${old['gross']:,.2f}). "
                    f"Generating again would overwrite it.")
            else:
                filepath = generate_payroll(_paystub_data)
                st.session_state.pop("_preview_img", None)

                # Update the log entry with the actual PDF path
                entry["pdf_path"] = filepath
                _rem_id = entry.get("id")
                if _rem_id:
                    try:
                        _db.update_remittance_pdf_path(_rem_id, filepath)
                    except Exception:
                        pass

                st.session_state["_last_pdf"] = filepath
                st.session_state["_last_remittance"] = entry
                st.session_state.pop("_overwrite_conflict", None)
                st.rerun()
        except Exception as e:
            st.error(f"Error: {e}")

    # Overwrite confirmation prompt
    if st.session_state.get("_overwrite_conflict"):
        st.warning("Do you want to overwrite the existing record?")
        ow_c1, ow_c2, _ow_sp = st.columns([1, 1, 4])
        with ow_c1:
            if st.button("Yes, Overwrite", type="primary"):
                st.session_state["_force_overwrite"] = True
                st.session_state.pop("_overwrite_conflict", None)
                st.rerun()
        with ow_c2:
            if st.button("Cancel"):
                st.session_state.pop("_overwrite_conflict", None)
                st.rerun()

    # ── Step 2: Post-generation actions (only after PDF exists) ──
    _last_pdf = st.session_state.get("_last_pdf")
    _last_rem = st.session_state.get("_last_remittance")

    _last_pdf_bytes = _get_pdf_bytes(_last_pdf) if _last_pdf else None
    if _last_pdf and _last_pdf_bytes:
        st.success(f"Payroll generated: {os.path.basename(_last_pdf)}")
        if _last_rem:
            st.info(
                f"${_last_rem['total_remittance']:,.2f} logged for CRA "
                f"remittance (includes "
                f"${_last_rem['cpp_employer']:,.2f} employer CPP + "
                f"${_last_rem.get('cpp2_employer', 0):,.2f} employer CPP2 + "
                f"${_last_rem['ei_employer']:,.2f} employer EI)")

        _pay_period_label = (f"{format_date(final_from)} – "
                             f"{format_date(final_to)}")
        _emp = employee_name.strip()

        ac1, ac2, ac3 = st.columns(3)
        with ac1:
            st.download_button(
                label="Download PDF", data=_last_pdf_bytes,
                file_name=os.path.basename(_last_pdf),
                mime="application/pdf",
                use_container_width=True)
        with ac2:
            _do_pay = st.button("Pay", use_container_width=True)
        with ac3:
            _do_email = st.button("Email Stub",
                                  use_container_width=True)

        # ── Pay Form ──
        if _do_pay:
            st.markdown("---")
            if payment_method == "E-Transfer":
                st.markdown("**Interac e-Transfer**")
                st.code(
                    f"Recipient:  {_emp}\n"
                    f"Email:      {email}\n"
                    f"Amount:     ${net_pay:,.2f}\n"
                    f"Message:    Payroll {_pay_period_label}",
                    language=None)

            elif payment_method == "Direct Deposit (EFT)":
                st.markdown("**Direct Deposit (EFT)**")
                st.code(
                    f"Employee:      {_emp}\n"
                    f"Institution:   "
                    f"{bank_inst or '___'}\n"
                    f"Transit:       "
                    f"{bank_transit or '_____'}\n"
                    f"Account:       "
                    f"{bank_account or '____________'}\n"
                    f"Amount:        ${net_pay:,.2f}\n"
                    f"Reference:     Payroll {_pay_period_label}",
                    language=None)
                if not bank_inst:
                    st.caption("Bank details not on file — add them "
                               "under Banking Details (EFT) above.")

            elif payment_method == "Cheque":
                st.markdown("**Cheque**")
                st.code(
                    f"Pay to:     {_emp}\n"
                    f"Amount:     ${net_pay:,.2f}\n"
                    f"Written:    {_dollars_to_words(net_pay)}\n"
                    f"Date:       {format_date(final_pay)}\n"
                    f"Memo:       Payroll {_pay_period_label}",
                    language=None)

        # ── Email Stub ──
        if _do_email:
            st.markdown("---")
            if not email:
                st.warning("No email on file for this employee.")
            else:
                st.markdown(
                    f"**Ready to send:** "
                    f"`{os.path.basename(_last_pdf)}` → "
                    f"**{email}**")
                st.caption(
                    "Email delivery not yet configured. "
                    "PDF is saved to disk and can be attached "
                    "manually.")


# ══════════════════════════════════════════════════════════════
#  TAB 2: REMITTANCES
# ══════════════════════════════════════════════════════════════

with tab_remittances:
    st.title("CRA Remittance Summary")
    st.markdown("Monthly deduction totals — due to CRA by the 15th of the following month.")
    st.markdown("---")

    months_available, _skipped_remit = available_months()
    if _skipped_remit:
        st.warning(f"Malformed remittance file(s) skipped: {', '.join(_skipped_remit)}")

    if not months_available:
        st.info("No remittance data yet. Generate a payroll to start tracking.")
    else:
        rc1, rc2 = st.columns(2)
        with rc1:
            r_month = st.selectbox("Month", MONTHS,
                                   index=months_available[-1][1] - 1,
                                   key="r_month")
        with rc2:
            r_year = st.number_input("Year", min_value=2024, max_value=2035,
                                     value=months_available[-1][0], step=1,
                                     key="r_year")

        # ── Pay Calendar ──
        with st.expander(f"Pay Calendar — {r_year}", expanded=False):
            _cal_today = date.today()
            _cal_rows = []
            for _cm in range(1, 13):
                _pay1 = date(r_year, _cm, 1)
                _pay2 = date(r_year, _cm, 15)
                if _cm == 12:
                    _rem_due = date(r_year + 1, 1, 15)
                else:
                    _rem_due = date(r_year, _cm + 1, 15)
                _status = get_remittance_status(r_year, _cm)
                if _status.get("remitted"):
                    _st_label = "Remitted"
                elif _cal_today > _rem_due:
                    _st_label = "OVERDUE"
                elif (_rem_due - _cal_today).days <= 14:
                    _st_label = "Due Soon"
                elif monthly_summary(r_year, _cm) is not None:
                    _st_label = "Pending"
                else:
                    _st_label = "—"
                _cal_rows.append({
                    "Month": MONTHS[_cm - 1],
                    "Pay Date 1": _pay1.strftime("%b %d"),
                    "Pay Date 2": _pay2.strftime("%b %d"),
                    "Remittance Due": _rem_due.strftime("%b %d, %Y"),
                    "Status": _st_label,
                })
            _cal_df = pd.DataFrame(_cal_rows)

            def _cal_style(row):
                s = row["Status"]
                if s == "OVERDUE":
                    return ["background-color: #ffcdd2; color: #b71c1c"] * len(row)
                elif s == "Due Soon":
                    return ["background-color: #fff9c4; color: #6d4c00"] * len(row)
                elif s == "Remitted":
                    return ["background-color: #e0e0e0; color: #424242"] * len(row)
                return [""] * len(row)

            st.dataframe(_cal_df.style.apply(_cal_style, axis=1),
                         use_container_width=True, hide_index=True)

            _stats_list = get_all_stats(r_year)
            if _stats_list:
                _stat_str = "  \n".join(
                    f"**{s[1]}** — {s[0].strftime('%B %d')}"
                    for s in _stats_list)
                st.markdown(
                    f"**Statutory Holidays ({r_year})**  \n{_stat_str}")
            st.markdown(f"**T4 Filing Deadline:** Last day of February "
                        f"{r_year + 1}")
            st.markdown(f"**WCB Annual Return:** March 31, {r_year + 1}")

        r_month_idx = MONTHS.index(r_month) + 1
        summary = monthly_summary(r_year, r_month_idx)

        if summary is None:
            st.warning(f"No payrolls recorded for {r_month} {r_year}.")
        else:
            # ── Payroll entries table ──
            st.subheader(f"Payrolls in {r_month} {r_year}")

            rows = []
            for e in summary["entries"]:
                row = {
                    "Employee":     e["employee"],
                    "Pay Date":     e["payment_date"],
                    "Gross":        f"${e['gross']:,.2f}",
                    "CPP (Emp)":    f"${e['cpp_employee']:,.2f}",
                    "CPP (Er)":     f"${e['cpp_employer']:,.2f}",
                    "CPP2 (Emp)":   f"${e.get('cpp2_employee', 0):,.2f}",
                    "CPP2 (Er)":    f"${e.get('cpp2_employer', 0):,.2f}",
                    "EI (Emp)":     f"${e['ei_employee']:,.2f}",
                    "EI (Er)":      f"${e['ei_employer']:,.2f}",
                    "Fed Tax":      f"${e['fed_tax']:,.2f}",
                    "Prov Tax":     f"${e['prov_tax']:,.2f}",
                    "Remittance":   f"${e['total_remittance']:,.2f}",
                }
                rows.append(row)
            st.dataframe(pd.DataFrame(rows), use_container_width=True,
                         hide_index=True)

            # ── Summary totals ──
            st.markdown("---")
            st.subheader("Remittance Totals")

            t = summary["totals"]
            sc1, sc2, sc3 = st.columns(3)

            with sc1:
                st.metric("CPP Total",
                          f"${t['cpp_employee'] + t['cpp_employer']:,.2f}",
                          f"Employee ${t['cpp_employee']:,.2f} + Employer ${t['cpp_employer']:,.2f}")
                cpp2_emp_t = t.get("cpp2_employee", 0)
                cpp2_er_t = t.get("cpp2_employer", 0)
                if cpp2_emp_t + cpp2_er_t > 0:
                    st.metric("CPP2 Total",
                              f"${cpp2_emp_t + cpp2_er_t:,.2f}",
                              f"Employee ${cpp2_emp_t:,.2f} + Employer ${cpp2_er_t:,.2f}")
                st.metric("EI Total",
                          f"${t['ei_employee'] + t['ei_employer']:,.2f}",
                          f"Employee ${t['ei_employee']:,.2f} + Employer ${t['ei_employer']:,.2f}")

            with sc2:
                st.metric("Federal Tax", f"${t['fed_tax']:,.2f}")
                st.metric("Provincial Tax (AB)", f"${t['prov_tax']:,.2f}")

            with sc3:
                st.metric("Total Due to CRA",
                          f"${t['total_remittance']:,.2f}")
                st.metric("Due Date", summary["due_date"])

            # ── Employer cost note ──
            employer_extra = round(t["cpp_employer"] + t.get("cpp2_employer", 0) + t["ei_employer"], 2)
            st.markdown("---")
            _cost_parts = f"CPP match ${t['cpp_employer']:,.2f}"
            if t.get("cpp2_employer", 0) > 0:
                _cost_parts += f" + CPP2 match ${t['cpp2_employer']:,.2f}"
            _cost_parts += f" + EI x1.4 ${t['ei_employer']:,.2f}"
            st.caption(
                f"Employer cost above employee deductions: **${employer_extra:,.2f}** "
                f"({_cost_parts})"
            )

            # ── Mark as Remitted ──
            st.markdown("---")
            _r_status = get_remittance_status(r_year, r_month_idx)

            if _r_status["remitted"]:
                st.success(
                    f"Remitted to CRA on **{_r_status['remitted_date']}**"
                    + (f" — Confirmation: {_r_status['confirmation']}"
                       if _r_status["confirmation"] else ""))
                if _r_status.get("notes"):
                    st.caption(f"Notes: {_r_status['notes']}")
                if st.button("Clear Remittance Status",
                             key="clear_remit"):
                    clear_remitted(r_year, r_month_idx)
                    st.rerun()
            else:
                with st.expander("Mark as Remitted to CRA"):
                    _rm_date = st.date_input(
                        "Date remitted", value=today,
                        key="remit_date")
                    _rm_conf = st.text_input(
                        "Confirmation / reference number",
                        key="remit_conf")
                    _rm_notes = st.text_input(
                        "Notes (optional)", key="remit_notes")
                    if st.button("Mark as Remitted", type="primary",
                                 key="do_remit"):
                        mark_remitted(r_year, r_month_idx,
                                      _rm_date.strftime("%B %d, %Y"),
                                      _rm_conf, _rm_notes)
                        st.rerun()

            # ── Export monthly summary (CSV) ──
            st.markdown("---")
            _csv_rows = [
                ["Employee", "Pay Date", "Gross",
                 "CPP (Emp)", "CPP (Er)", "CPP2 (Emp)", "CPP2 (Er)",
                 "EI (Emp)", "EI (Er)",
                 "Fed Tax", "Prov Tax", "Total Remittance"]]
            for e in summary["entries"]:
                _csv_rows.append([
                    e["employee"], e["payment_date"],
                    f"{e['gross']:.2f}",
                    f"{e['cpp_employee']:.2f}",
                    f"{e['cpp_employer']:.2f}",
                    f"{e.get('cpp2_employee', 0):.2f}",
                    f"{e.get('cpp2_employer', 0):.2f}",
                    f"{e['ei_employee']:.2f}",
                    f"{e['ei_employer']:.2f}",
                    f"{e['fed_tax']:.2f}",
                    f"{e['prov_tax']:.2f}",
                    f"{e['total_remittance']:.2f}",
                ])
            _csv_rows.append([])
            _csv_rows.append([
                "TOTALS", "", f"{t['gross']:.2f}",
                f"{t['cpp_employee']:.2f}",
                f"{t['cpp_employer']:.2f}",
                f"{t.get('cpp2_employee', 0):.2f}",
                f"{t.get('cpp2_employer', 0):.2f}",
                f"{t['ei_employee']:.2f}",
                f"{t['ei_employer']:.2f}",
                f"{t['fed_tax']:.2f}",
                f"{t['prov_tax']:.2f}",
                f"{t['total_remittance']:.2f}",
            ])
            _csv_text = "\n".join(
                ",".join(str(c) for c in row) for row in _csv_rows)
            st.download_button(
                "Export Monthly Summary (CSV)",
                data=_csv_text,
                file_name=f"Remittance_{r_year}-{r_month_idx:02d}.csv",
                mime="text/csv",
                key="dl_remit_csv")

        # ── YTD Remittance Totals ──
        st.markdown("---")
        st.subheader(f"Year-to-Date Remittances — {r_year}")
        _ytd = ytd_summary(r_year)

        if _ytd is None:
            st.info(f"No remittance data for {r_year}.")
        else:
            _yt = _ytd["totals"]
            _ytc1, _ytc2, _ytc3 = st.columns(3)
            with _ytc1:
                st.metric("YTD Gross Payroll",
                          f"${_yt['gross']:,.2f}")
                st.metric("YTD CPP (Emp + Er)",
                          f"${_yt['cpp_employee'] + _yt['cpp_employer']:,.2f}")
            with _ytc2:
                st.metric("YTD EI (Emp + Er)",
                          f"${_yt['ei_employee'] + _yt['ei_employer']:,.2f}")
                st.metric("YTD Tax (Fed + Prov)",
                          f"${_yt['fed_tax'] + _yt['prov_tax']:,.2f}")
            with _ytc3:
                st.metric("YTD Total Remitted",
                          f"${_yt['total_remittance']:,.2f}")
                st.caption(
                    f"Across {len(_ytd['months'])} "
                    f"{'months' if len(_ytd['months']) != 1 else 'month'}")

        # ── WCB Annual Return Summary ──
        st.markdown("---")
        st.subheader(f"WCB Annual Return Summary — {r_year}")

        _wcb_conf = load_employer()
        _wcb_rate = _wcb_conf.get("wcb_rate_per_100", 3.50)
        _wcb_code = _wcb_conf.get("wcb_code", "")

        _wcb_ytd = ytd_summary(r_year)
        if _wcb_ytd:
            _wcb_gross = _wcb_ytd["totals"]["gross"]
            _wcb_premium = round(_wcb_gross / 100 * _wcb_rate, 2)

            _wc1, _wc2, _wc3 = st.columns(3)
            with _wc1:
                st.metric("Total Assessable Earnings",
                          f"${_wcb_gross:,.2f}")
            with _wc2:
                st.metric("WCB Premium Rate",
                          f"${_wcb_rate:.2f} per $100")
            with _wc3:
                st.metric("Total Premium Due",
                          f"${_wcb_premium:,.2f}")

            if _wcb_code:
                st.caption(
                    f"WCB Classification Code: {_wcb_code}")

            with st.expander("Monthly WCB Breakdown"):
                _wcb_rows = []
                for _mo in _wcb_ytd["months"]:
                    _ms = monthly_summary(r_year, _mo)
                    if _ms:
                        _mg = _ms["totals"]["gross"]
                        _mp = round(_mg / 100 * _wcb_rate, 2)
                        _wcb_rows.append({
                            "Month": MONTHS[_mo - 1],
                            "Gross Earnings": f"${_mg:,.2f}",
                            "WCB Premium": f"${_mp:,.2f}",
                        })
                if _wcb_rows:
                    st.dataframe(pd.DataFrame(_wcb_rows),
                                 hide_index=True,
                                 use_container_width=True)
        else:
            st.caption(
                f"No payroll data for {r_year} to calculate WCB.")


# ══════════════════════════════════════════════════════════════
#  TAB 3: AUDIT
# ══════════════════════════════════════════════════════════════

with tab_audit:
    st.title("Payroll Deduction Audit")
    st.markdown("Compare actual deductions against CRA T4127 formula estimates.  \n"
                "Every pay period since January 2024 is listed — missing data is flagged.")
    st.markdown("---")

    if selected == "+ New Employee":
        st.info("Select an employee from the sidebar to view their audit.")
    else:
        audit_emp = selected
        st.markdown(f"**Employee: {audit_emp}**")

        # ── Filters ──
        fc1, fc2 = st.columns(2)
        with fc1:
            year_list = list(range(2024, today.year + 1))
            year_options = ["All Time (2024-Present)"] + [str(y) for y in year_list]
            f_year_sel = st.selectbox("Year", year_options, key="f_audit_year")
        with fc2:
            period_options = (["Full Year", "Q1 (Jan-Mar)", "Q2 (Apr-Jun)",
                               "Q3 (Jul-Sep)", "Q4 (Oct-Dec)"] + MONTHS)
            f_period = st.selectbox("Period", period_options, key="f_audit_period")

        # Resolve filters
        if f_year_sel == "All Time (2024-Present)":
            filter_years = year_list
        else:
            filter_years = [int(f_year_sel)]

        quarter_months = {
            "Q1 (Jan-Mar)": [1, 2, 3], "Q2 (Apr-Jun)": [4, 5, 6],
            "Q3 (Jul-Sep)": [7, 8, 9], "Q4 (Oct-Dec)": [10, 11, 12],
        }
        if f_period == "Full Year":
            filter_months = list(range(1, 13))
        elif f_period in quarter_months:
            filter_months = quarter_months[f_period]
        else:
            filter_months = [MONTHS.index(f_period) + 1]

        # ── Generate complete pay-period timeline ──
        _audit_profile = load_profile(audit_emp)
        _emp_start_str = _audit_profile.get("start_date", "")
        _emp_start = (date.fromisoformat(_emp_start_str)
                      if _emp_start_str else None)
        _emp_end_str = _audit_profile.get("end_date", "")
        _emp_end = (date.fromisoformat(_emp_end_str)
                    if _emp_end_str else None)

        all_periods = []
        for yr in filter_years:
            for mo in filter_months:
                for cycle in ["1st", "15th"]:
                    try:
                        p_from, p_to, p_pay = get_pay_period(yr, mo, cycle)
                    except Exception:
                        continue
                    if p_pay > today:
                        continue
                    # Determine status for periods before employment
                    _pre_employment = (_emp_start and p_pay < _emp_start)
                    _post_termination = (_emp_end and p_pay > _emp_end)
                    all_periods.append({
                        "year": yr, "month": mo, "cycle": cycle,
                        "period_from": p_from, "period_to": p_to,
                        "payment_date": p_pay,
                        "payment_date_str": format_date(p_pay),
                        "pre_employment": _pre_employment,
                        "post_termination": _post_termination,
                    })

        # ── Load existing audit data for this employee ──
        audit_entries = get_all_audit_entries(employee=audit_emp)
        entry_lookup = {}
        _dup_warnings = []
        for e in audit_entries:
            entry_lookup[e["payment_date"]] = e
            if e.get("duplicate_warning"):
                _dup_warnings.append(e["duplicate_warning"])
        if _dup_warnings:
            st.warning("Duplicate entries detected:\n- " +
                       "\n- ".join(_dup_warnings))

        # ── Add Historical Entry (collapsed) ──
        with st.expander("Add Historical Payroll Entry"):
            st.caption(f"Adding entry for **{audit_emp}**")
            ac1, ac2 = st.columns(2)

            with ac1:
                apc1, apc2 = st.columns(2)
                with apc1:
                    a_month = st.selectbox("Payment Month", MONTHS, key="a_month")
                with apc2:
                    a_year = st.number_input("Year", min_value=2024,
                                              max_value=today.year, step=1,
                                              value=today.year, key="a_year")
                a_cycle = st.radio("Pay Date", ["1st", "15th"], horizontal=True,
                                   key="a_cycle")
                a_month_idx = MONTHS.index(a_month) + 1
                a_from, a_to, a_pay = get_pay_period(a_year, a_month_idx, a_cycle)
                st.caption(f"Period: **{format_date(a_from)}** to "
                           f"**{format_date(a_to)}**")

            with ac2:
                a_hours = st.number_input("Hours", min_value=0.0, value=0.0,
                                           step=0.25, format="%.2f", key="a_hours")
                a_gross = st.number_input("Gross Pay ($)", min_value=0.0, value=0.0,
                                           step=0.01, format="%.2f", key="a_gross")
                st.markdown("**Actual deductions (what was withheld):**")
                adc1, adc2 = st.columns(2)
                with adc1:
                    a_cpp = st.number_input("CPP ($)", min_value=0.0, step=0.01,
                                             format="%.2f", key="a_cpp")
                    a_fed = st.number_input("Federal Tax ($)", min_value=0.0,
                                             step=0.01, format="%.2f", key="a_fed")
                with adc2:
                    a_ei = st.number_input("EI ($)", min_value=0.0, step=0.01,
                                            format="%.2f", key="a_ei")
                    a_prov = st.number_input("Provincial Tax ($)", min_value=0.0,
                                              step=0.01, format="%.2f", key="a_prov")

            if st.button("Add Entry", type="primary"):
                a_errors = []
                if a_gross <= 0:
                    a_errors.append("Gross pay must be > $0")
                for ae in a_errors:
                    st.warning(ae)
                if not a_errors:
                    entry = log_manual_entry(
                        employee=audit_emp,
                        payment_date_str=format_date(a_pay),
                        pay_year=a_year, pay_month=a_month_idx,
                        period_from=format_date(a_from),
                        period_to=format_date(a_to),
                        hours=a_hours, gross=a_gross,
                        actual_cpp=a_cpp, actual_ei=a_ei,
                        actual_fed=a_fed, actual_prov=a_prov,
                    )
                    total_var = round(sum(entry["variance"].values()), 2)
                    _tv_s = f"{'+'if total_var>=0 else'-'}${abs(total_var):.2f}"
                    if abs(total_var) < 2:
                        st.success(f"Entry added — variance {_tv_s} "
                                   f"(normal rounding)")
                    elif abs(total_var) < 10:
                        st.info(f"Entry added — variance {_tv_s}")
                    else:
                        st.warning(f"Entry added — variance {_tv_s} "
                                   f"(significant)")
                    st.rerun()

        # ── Pay Period Timeline ──
        st.markdown("---")

        if not all_periods:
            st.info("No pay periods match the selected filters.")
        else:
            # Scope label
            scope_parts = [audit_emp]
            if f_year_sel == "All Time (2024-Present)":
                scope_parts.append("All Time")
            else:
                scope_parts.append(f_year_sel)
            if f_period != "Full Year":
                scope_parts.append(f_period.split(" (")[0])
            scope_label = " — ".join(scope_parts)

            st.subheader(f"Audit — {scope_label}")

            # Build rows — every pay period gets a row
            rows = []
            data_count = 0
            missing_count = 0
            orphan_count = 0
            entries_with_data = []

            def _var_fmt(v):
                """Format variance — sign before $, subtle indicators."""
                sign = "+" if v >= 0 else "-"
                amt = abs(v)
                if amt < 2:
                    return f"{sign}${amt:.2f}"
                elif amt < 10:
                    return f"{sign}${amt:.2f} ~"
                else:
                    return f"{sign}${amt:.2f} !"

            unresolved_periods = []  # for resolution UI

            for p in all_periods:
                # Skip pre-employment periods — show "NOT EMPLOYED"
                if p.get("pre_employment"):
                    period_str = (f"{format_date(p['period_from'])} - "
                                  f"{format_date(p['period_to'])}")
                    rows.append({
                        "Pay Period":   period_str,
                        "Payment Date": p["payment_date_str"],
                        "Status":       "NOT EMPLOYED",
                        "Gross":        "",
                        "CPP":          "", "CPP (T4127)": "",
                        "EI":           "", "EI (T4127)":  "",
                        "Fed Tax":      "", "Fed (T4127)": "",
                        "Prov Tax":     "", "Prov (T4127)": "",
                        "Total Var":    "",
                    })
                    continue

                if p.get("post_termination"):
                    period_str = (f"{format_date(p['period_from'])} - "
                                  f"{format_date(p['period_to'])}")
                    rows.append({
                        "Pay Period":   period_str,
                        "Payment Date": p["payment_date_str"],
                        "Status":       "TERMINATED",
                        "Gross":        "",
                        "CPP":          "", "CPP (T4127)": "",
                        "EI":           "", "EI (T4127)":  "",
                        "Fed Tax":      "", "Fed (T4127)": "",
                        "Prov Tax":     "", "Prov (T4127)": "",
                        "Total Var":    "",
                    })
                    continue

                entry = entry_lookup.get(p["payment_date_str"])
                period_str = (f"{format_date(p['period_from'])} - "
                              f"{format_date(p['period_to'])}")

                if entry:
                    data_count += 1
                    entries_with_data.append(entry)
                    total_var = round(sum(entry["variance"].values()), 2)

                    # Check for missing PDF (orphaned record)
                    _pdf_missing = entry.get("pdf_missing", False)

                    # Check for resolution
                    resolution = get_resolution_for_period(
                        audit_emp, p["payment_date_str"])
                    if _pdf_missing:
                        status = "PDF MISSING"
                        orphan_count += 1
                    elif resolution:
                        status = "RESOLVED"
                    elif abs(total_var) >= 2:
                        status = "VARIANCE"
                        unresolved_periods.append({
                            "payment_date_str": p["payment_date_str"],
                            "period_str":       period_str,
                            "total_var":        total_var,
                            "pay_year":         p["year"],
                            "pay_month":        p["month"],
                        })
                    else:
                        status = "OK"

                    row = {
                        "Pay Period":   period_str,
                        "Payment Date": p["payment_date_str"],
                        "Status":       status,
                        "Gross":        f"${entry['gross']:,.2f}",
                        "CPP":          f"${entry['actual']['cpp']:.2f}",
                        "CPP (T4127)":  f"${entry['formula']['cpp']:.2f}",
                    }
                    # CPP2 columns only if any entry has non-zero CPP2
                    _has_cpp2 = entry["actual"].get("cpp2", 0) != 0 or entry["formula"].get("cpp2", 0) != 0
                    if _has_cpp2:
                        row["CPP2"] = f"${entry['actual'].get('cpp2', 0):.2f}"
                        row["CPP2 (T4127)"] = f"${entry['formula'].get('cpp2', 0):.2f}"
                    row.update({
                        "EI":           f"${entry['actual']['ei']:.2f}",
                        "EI (T4127)":   f"${entry['formula']['ei']:.2f}",
                        "Fed Tax":      f"${entry['actual']['fed_tax']:.2f}",
                        "Fed (T4127)":  f"${entry['formula']['fed_tax']:.2f}",
                        "Prov Tax":     f"${entry['actual']['prov_tax']:.2f}",
                        "Prov (T4127)": f"${entry['formula']['prov_tax']:.2f}",
                        "Total Var":    _var_fmt(total_var),
                    })
                    rows.append(row)
                else:
                    missing_count += 1
                    rows.append({
                        "Pay Period":   period_str,
                        "Payment Date": p["payment_date_str"],
                        "Status":       "NO DATA",
                        "Gross":        "",
                        "CPP":          "", "CPP (T4127)": "",
                        "EI":           "", "EI (T4127)":  "",
                        "Fed Tax":      "", "Fed (T4127)": "",
                        "Prov Tax":     "", "Prov (T4127)": "",
                        "Total Var":    "",
                    })

            # Metrics
            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("Total Pay Periods", len(all_periods))
            mc2.metric("With Data", data_count)
            mc3.metric("Missing", missing_count)

            if orphan_count > 0:
                st.warning(
                    f"{orphan_count} record(s) have no source PDF on "
                    f"disk. The payroll stub may have been deleted. "
                    f"Remittance log data is preserved but cannot be "
                    f"verified against the original document.")

            # Timeline table — color coded
            df_timeline = pd.DataFrame(rows)

            actual_cols  = [c for c in ["CPP", "CPP2", "EI", "Fed Tax", "Prov Tax"]
                           if c in df_timeline.columns]
            formula_cols = [c for c in ["CPP (T4127)", "CPP2 (T4127)", "EI (T4127)",
                            "Fed (T4127)", "Prov (T4127)"]
                           if c in df_timeline.columns]

            def _style_audit(df):
                s = pd.DataFrame("", index=df.index, columns=df.columns)
                for idx in df.index:
                    if df.loc[idx, "Status"] == "PDF MISSING":
                        s.loc[idx, :] = (
                            "background-color: #fff3e0; color: #e65100")
                        s.loc[idx, "Status"] = (
                            "background-color: #ff9800; color: #fff; "
                            "font-weight: bold")
                    elif df.loc[idx, "Status"] == "NO DATA":
                        s.loc[idx, :] = (
                            "background-color: #f5f5f5; color: #aaaaaa")
                    elif df.loc[idx, "Status"] == "RESOLVED":
                        s.loc[idx, :] = (
                            "background-color: #e0f2f1")
                        s.loc[idx, "Status"] = (
                            "background-color: #80cbc4; color: #004d40; "
                            "font-weight: bold")
                    else:
                        # Status badge
                        s.loc[idx, "Status"] = (
                            "background-color: #d4edda; color: #155724")
                        # Actual columns — light blue
                        for c in actual_cols:
                            s.loc[idx, c] = "background-color: #e3f2fd"
                        # Formula columns — warm grey
                        for c in formula_cols:
                            s.loc[idx, c] = "background-color: #f5f5f0"
                        # Variance — subtle text-only per period
                        try:
                            v = float(str(df.loc[idx, "Total Var"])
                                      .replace("$", "").replace("~", "")
                                      .replace("!", "").strip())
                            if abs(v) < 2:
                                s.loc[idx, "Total Var"] = (
                                    "color: #6c757d")
                            elif abs(v) < 10:
                                s.loc[idx, "Total Var"] = (
                                    "color: #856404")
                            else:
                                s.loc[idx, "Total Var"] = (
                                    "color: #dc3545; "
                                    "font-weight: bold")
                        except (ValueError, TypeError):
                            pass
                return s

            styled = df_timeline.style.apply(_style_audit, axis=None)
            st.dataframe(styled, use_container_width=True,
                         hide_index=True)
            st.caption("Per-period variances are expected rounding. "
                       "See annual summary below for CRA-relevant totals.")

            # ── Summary (entries with data only) ──
            if entries_with_data:
                st.markdown("---")
                summary = get_audit_summary(entries_with_data)
                if summary:
                    st.subheader(f"Summary — {scope_label}")

                    asc1, asc2, asc3 = st.columns(3)
                    with asc1:
                        st.metric("Payrolls Audited", summary["entry_count"])
                        st.metric("Total Gross",
                                  f"${summary['total_gross']:,.2f}")
                    with asc2:
                        st.metric("Actual Deducted",
                                  f"${summary['actual_total']:,.2f}")
                        st.metric("Formula (T4127)",
                                  f"${summary['formula_total']:,.2f}")
                    with asc3:
                        vt = summary["variance_total"]
                        _vt_sign = "+" if vt >= 0 else "-"
                        _vt_str = f"{_vt_sign}${abs(vt):,.2f}"
                        if abs(vt) < 25:
                            verdict = "Within CRA tolerance"
                        elif abs(vt) < 100:
                            verdict = "Review recommended"
                        else:
                            verdict = "CRA action likely needed"
                        st.metric("Total Variance", _vt_str)
                        st.metric("Verdict", verdict)

                    # Plain-English standing + verdict banner
                    _amt = f"${abs(vt):,.2f}"
                    if abs(vt) < 0.01:
                        _msg = (f"{verdict} — {audit_emp} is in good "
                                f"standing. Deductions match CRA "
                                f"formula exactly.")
                        st.success(_msg)
                    elif vt > 0:
                        _msg = (f"{verdict} — Over-deducted by {_amt}. "
                                f"Employer owes {audit_emp} {_amt}.")
                        if abs(vt) < 25:
                            st.success(_msg)
                        elif abs(vt) < 100:
                            st.warning(_msg + " Check category "
                                       "breakdown below.")
                        else:
                            st.error(_msg + " Resolution and "
                                     "documentation required.")
                    else:
                        _msg = (f"{verdict} — Under-deducted by {_amt}. "
                                f"{audit_emp} owes {_amt} back.")
                        if abs(vt) < 25:
                            st.success(_msg)
                        elif abs(vt) < 100:
                            st.warning(_msg + " Check category "
                                       "breakdown below.")
                        else:
                            st.error(_msg + " Resolution and "
                                     "documentation required.")

                    # Breakdown table
                    st.markdown("**Variance by category:**")
                    _emp_first = audit_emp.split()[0]
                    var_rows = []
                    _var_cats = [("cpp", "CPP"), ("cpp2", "CPP2"),
                                 ("ei", "EI"), ("fed_tax", "Federal Tax"),
                                 ("prov_tax", "Provincial Tax")]
                    # Only show CPP2 row if there's any data
                    if (summary['actual_totals'].get('cpp2', 0) == 0
                            and summary['formula_totals'].get('cpp2', 0) == 0):
                        _var_cats = [c for c in _var_cats if c[0] != "cpp2"]
                    for k, label in _var_cats:
                        _v = summary['variance_totals'][k]
                        _vs = f"{'+'if _v>=0 else'-'}${abs(_v):,.2f}"
                        if abs(_v) < 0.01:
                            _owed = "—"
                        elif _v > 0:
                            _owed = f"Owed to {_emp_first}"
                        else:
                            _owed = "Owed to employer/CRA"
                        var_rows.append({
                            "Category": label,
                            "Actual":   f"${summary['actual_totals'][k]:,.2f}",
                            "Formula":  f"${summary['formula_totals'][k]:,.2f}",
                            "Variance": _vs,
                            "Owed To":  _owed,
                        })
                    # Vacation pay — add as row in variance table
                    _vac_accrual = get_vacation_accrual(audit_emp)
                    if _vac_accrual and _vac_accrual["vacation_years"]:
                        _vac_bal = _vac_accrual["balance"]
                        var_rows.append({
                            "Category": "Vacation Pay",
                            "Actual":   f"${_vac_accrual['total_accrued']:,.2f} accrued",
                            "Formula":  f"${_vac_accrual['total_paid']:,.2f} paid",
                            "Variance": f"${_vac_bal:,.2f}",
                            "Owed To":  f"Owed to {_emp_first}" if _vac_bal > 0 else "—",
                        })
                        _overdue = [vy for vy in _vac_accrual["vacation_years"]
                                    if vy["status"] in ("complete", "final")
                                    and vy["balance"] > 0]
                        if _overdue:
                            summary["flags"].append(
                                f"Vacation Pay: ${_vac_bal:,.2f} owing — "
                                f"{len(_overdue)} completed year(s) unpaid")

                    df_var = pd.DataFrame(var_rows)

                    def _style_var_summary(df):
                        s = pd.DataFrame("", index=df.index,
                                         columns=df.columns)
                        for idx in df.index:
                            s.loc[idx, "Actual"] = (
                                "background-color: #e3f2fd; "
                                "font-weight: bold")
                            s.loc[idx, "Formula"] = (
                                "background-color: #f5f5f0")
                        return s

                    styled_var = df_var.style.apply(
                        _style_var_summary, axis=None)
                    st.dataframe(styled_var,
                                 use_container_width=True, hide_index=True)

                    if summary["flags"]:
                        st.markdown("---")
                        for flag in summary["flags"]:
                            st.warning(flag)

            # ── Resolve a Variance ──
            if unresolved_periods:
                st.markdown("---")
                with st.expander("Resolve a Variance"):
                    st.caption(
                        "Record how a variance was resolved (cheque, "
                        "e-transfer, remittance adjustment). "
                        "Upload a receipt for audit documentation.")

                    res_options = [
                        f"{up['payment_date_str']} — Var: "
                        f"${up['total_var']:+.2f}"
                        for up in unresolved_periods
                    ]
                    res_sel = st.selectbox(
                        "Pay period with variance",
                        range(len(res_options)),
                        format_func=lambda i: res_options[i],
                        key="res_period")

                    selected_up = unresolved_periods[res_sel]

                    # Sync resolution amount when selected period changes
                    if st.session_state.get("_last_res_sel") != res_sel:
                        st.session_state["_last_res_sel"] = res_sel
                        st.session_state["res_amount"] = abs(
                            selected_up["total_var"])

                    rc1, rc2 = st.columns(2)
                    with rc1:
                        res_method = st.selectbox(
                            "Resolution method",
                            ["E-Transfer", "Cheque",
                             "Adjusted on next remittance", "Other"],
                            key="res_method")
                        res_amount = st.number_input(
                            "Resolution amount ($)",
                            value=abs(selected_up["total_var"]),
                            min_value=0.0, step=0.01,
                            format="%.2f", key="res_amount")
                    with rc2:
                        res_date = st.date_input(
                            "Resolution date", value=today,
                            key="res_date")
                        res_ref = st.text_input(
                            "Reference / confirmation #",
                            key="res_ref")

                    res_notes = st.text_area(
                        "Notes (optional)", key="res_notes",
                        height=68)
                    res_receipt = st.file_uploader(
                        "Upload receipt (PDF/PNG/JPG)",
                        type=["pdf", "png", "jpg", "jpeg"],
                        key="res_receipt")

                    if st.button("Submit Resolution", type="primary"):
                        receipt_path = ""
                        if res_receipt:
                            pay_iso = (
                                f"{selected_up['pay_year']}-"
                                f"{selected_up['pay_month']:02d}")
                            receipt_path = save_receipt(
                                audit_emp, pay_iso, res_receipt)

                        log_resolution(
                            employee=audit_emp,
                            payment_date_str=selected_up[
                                "payment_date_str"],
                            pay_year=selected_up["pay_year"],
                            pay_month=selected_up["pay_month"],
                            variance_amount=selected_up["total_var"],
                            resolution_method=res_method,
                            resolution_amount=res_amount,
                            resolution_date=str(res_date),
                            reference_number=res_ref,
                            receipt_file_path=receipt_path,
                            notes=res_notes,
                        )
                        st.success("Resolution recorded!")
                        st.rerun()

            # ── Resolution details for resolved periods ──
            resolved_shown = False
            for p in all_periods:
                res = get_resolution_for_period(
                    audit_emp, p["payment_date_str"])
                if res:
                    if not resolved_shown:
                        st.markdown("---")
                        st.subheader("Resolutions on File")
                        resolved_shown = True
                    with st.expander(
                            f"{res['payment_date']} — "
                            f"{res['resolution_method']} "
                            f"(${res['resolution_amount']:,.2f})"):
                        dc1, dc2 = st.columns(2)
                        with dc1:
                            st.markdown(
                                f"**Variance:** "
                                f"${res['variance_amount']:+,.2f}")
                            st.markdown(
                                f"**Method:** {res['resolution_method']}")
                            st.markdown(
                                f"**Amount:** "
                                f"${res['resolution_amount']:,.2f}")
                        with dc2:
                            st.markdown(
                                f"**Date:** {res['resolution_date']}")
                            if res.get("reference_number"):
                                st.markdown(
                                    f"**Ref #:** "
                                    f"{res['reference_number']}")
                            st.markdown(
                                f"**Recorded:** {res['resolved_at']}")
                        if res.get("notes"):
                            st.markdown(f"**Notes:** {res['notes']}")
                        if res.get("receipt_file"):
                            _rec_bytes = get_receipt_bytes(
                                res["receipt_file"])
                            if _rec_bytes:
                                _rec_fname = os.path.basename(
                                    res["receipt_file"])
                                st.download_button(
                                    "Download Receipt",
                                    data=_rec_bytes,
                                    file_name=_rec_fname,
                                    key=f"dl_{res['payment_date']}")

            # ── Delete manual entry ──
            # Collect all years that might have manual data
            manual_years = sorted(set(
                e["pay_year"] for e in audit_entries
                if e.get("source") == "manual"
            ))
            if manual_years:
                st.markdown("---")
                with st.expander("Delete a manual entry"):
                    del_options = []
                    del_keys = []  # (year, file_index)
                    for yr in manual_years:
                        for file_idx, e in get_manual_entries_indexed(
                                yr, employee=audit_emp):
                            label = (f"{e['payment_date']} — "
                                     f"${e['gross']:,.2f}")
                            del_options.append(label)
                            del_keys.append((yr, file_idx))
                    if del_options:
                        del_sel_idx = st.selectbox(
                            "Select entry", range(len(del_options)),
                            format_func=lambda i: del_options[i],
                            key="del_sel")
                        if st.button("Delete Entry",
                                     key="btn_audit_del_req"):
                            st.session_state["_audit_del_confirm"] = (
                                del_keys[del_sel_idx])
                            st.rerun()
                        if st.session_state.get("_audit_del_confirm"):
                            st.warning(
                                "Confirm deletion — this will be "
                                "moved to the recovery bin.")
                            _adc1, _adc2, _ = st.columns([1, 1, 4])
                            with _adc1:
                                if st.button("Yes, Delete",
                                             type="primary",
                                             key="btn_audit_del_yes"):
                                    _ayr, _aidx = st.session_state.pop(
                                        "_audit_del_confirm")
                                    delete_manual_entry(_ayr, _aidx)
                                    st.success(
                                        "Entry moved to recovery bin.")
                                    st.rerun()
                            with _adc2:
                                if st.button("Cancel",
                                             key="btn_audit_del_cancel"):
                                    st.session_state.pop(
                                        "_audit_del_confirm", None)
                                    st.rerun()

    # ── Recently Deleted (Recovery Bin) ──────────────────────
    st.markdown("---")
    _trash_ret = get_retention_days(_co)
    _trash_all = get_trash()
    _trash_count = len(_trash_all)
    with st.expander(
            f"Recovery Bin ({_trash_count} item"
            f"{'s' if _trash_count != 1 else ''})",
            expanded=False):
        if not _trash_all:
            st.caption("Recovery bin is empty.")
        else:
            _TYPE_LABELS = {
                "remittances":       "Remittance",
                "audit_manual":      "Audit Entry",
                "vacation_payouts":  "Vacation Payout",
                "resolutions":       "Resolution",
            }
            _rb_filter = st.selectbox(
                "Filter by type",
                ["All"] + list(_TYPE_LABELS.values()),
                key="rb_type_filter")
            _rb_rows = []
            for _ti, _te in enumerate(_trash_all):
                _tlabel = _TYPE_LABELS.get(_te["_trash_type"], _te["_trash_type"])
                if _rb_filter != "All" and _tlabel != _rb_filter:
                    continue
                _ctx = _te.get("context", {})
                _ctx_str = " · ".join(
                    str(v) for v in _ctx.values() if v)
                _rb_rows.append({
                    "_idx": _ti,
                    "_type": _te["_trash_type"],
                    "_trash_id": _te.get("_trash_id", _te.get("id", "")),
                    "Deleted": _te["deleted_at"][:16].replace("T", " "),
                    "Type": _tlabel,
                    "Employee": _te.get("employee", "—"),
                    "Detail": _ctx_str,
                })
            if _rb_rows:
                st.dataframe(
                    pd.DataFrame([{k: v for k, v in r.items()
                                   if not k.startswith("_")}
                                  for r in _rb_rows]),
                    use_container_width=True, hide_index=True)

                _rb_sel = st.selectbox(
                    "Select item to restore",
                    range(len(_rb_rows)),
                    format_func=lambda i: (
                        f"{_rb_rows[i]['Deleted']} — "
                        f"{_rb_rows[i]['Type']} — "
                        f"{_rb_rows[i]['Employee']}"),
                    key="rb_sel")
                if st.button("Restore Selected", key="btn_rb_restore"):
                    _rrow = _rb_rows[_rb_sel]
                    _orig, _full = restore_entry(_rrow["_trash_id"])
                    # Restore logic per type
                    if _rrow["_type"] == "remittances":
                        try:
                            _db.log_remittance(_orig)
                        except Exception as _re:
                            st.error(f"Restore error: {_re}")
                        st.success("Remittance entry restored.")
                    elif _rrow["_type"] == "vacation_payouts":
                        try:
                            from vacation_tracker import record_vacation_payout
                            record_vacation_payout(
                                employee=_rrow["Employee"],
                                amount=_orig.get("amount", 0),
                                year_ending=_orig.get("year_ending", ""),
                                payout_date=date.fromisoformat(_orig["date"])
                                    if _orig.get("date") else None,
                                method=_orig.get("method", "Cheque"),
                                note=_orig.get("note", ""))
                        except Exception as _re:
                            st.error(f"Restore error: {_re}")
                        st.success("Vacation payout restored.")
                    elif _rrow["_type"] == "audit_manual":
                        from audit_tracker import log_manual_entry
                        log_manual_entry(
                            employee=_orig.get("employee", ""),
                            payment_date_str=_orig.get(
                                "payment_date", ""),
                            pay_year=_orig.get("pay_year",
                                               date.today().year),
                            pay_month=_orig.get("pay_month",
                                                date.today().month),
                            gross=_orig.get("gross", 0),
                            cpp=_orig.get("cpp", 0),
                            ei=_orig.get("ei", 0),
                            fed_tax=_orig.get("fed_tax", 0),
                            prov_tax=_orig.get("prov_tax", 0),
                            net_pay=_orig.get("net_pay", 0),
                            source="restored")
                        st.success("Audit entry restored.")
                    else:
                        st.info(
                            "Automatic restore not available for this "
                            "type. Original data shown below.")
                        st.json(_orig)
                    st.rerun()

            # Purge expired
            st.markdown("---")
            st.caption(
                f"Retention: **{_trash_ret} day"
                f"{'s' if _trash_ret != 1 else ''}**. "
                f"Items older than this are permanently deleted on purge.")
            if st.button("Purge Expired Items", key="btn_purge_trash"):
                _purged = purge_expired(_trash_ret)
                _total_purged = sum(_purged.values())
                st.success(
                    f"Purged {_total_purged} expired item"
                    f"{'s' if _total_purged != 1 else ''}.")
                st.rerun()


# ══════════════════════════════════════════════════════════════
#  TAB 4: T4 SLIPS
# ══════════════════════════════════════════════════════════════

with tab_t4:
    st.title("T4 Statement of Remuneration Paid")
    st.markdown("Generate year-end T4 summaries from payroll data.")

    _t4_sel_year = st.number_input(
        "Tax Year", min_value=2024, max_value=today.year,
        value=today.year, step=1, key="t4_year")
    st.markdown("---")

    if selected == "+ New Employee":
        st.info("Select an employee from the sidebar to generate T4 slips.")
    else:
        t4_emp = selected
        st.markdown(f"**Employee: {t4_emp}**")

        t4_data = aggregate_t4_data(t4_emp, _t4_sel_year)

        if t4_data is None:
            st.warning(
                f"No payroll data found for {t4_emp} in {_t4_sel_year}. "
                f"Generate payrolls first or check the Remittances tab.")
        else:
            profile = load_profile(t4_emp)
            sin_val = profile.get("sin", "")

            if not sin_val:
                st.warning(
                    "SIN not on file for this employee. Add it in the "
                    "Generate Payroll tab (Employee Info section) before "
                    "generating the official T4.")

            # Warn if tracked insurable earnings differ from total gross
            if t4_data.get("box_24", 0) != t4_data.get("box_14", 0) or \
               t4_data.get("box_26", 0) != t4_data.get("box_14", 0):
                st.info(
                    "Box 24 (EI insurable) or Box 26 (CPP pensionable) "
                    "differs from Box 14 (total income). This is expected if "
                    "the annual maximum was reached, or if non-insurable "
                    "income types were logged. Verify before filing.")
            else:
                st.caption(
                    "Box 24 and Box 26 are tracked per-payroll via "
                    "ei\\_insurable\\_gross and cpp\\_pensionable\\_gross fields. "
                    "If non-insurable income is ever added (commissions, certain "
                    "allowances), ensure those amounts are excluded when logging "
                    "the payroll.")

            # ── Box preview table ──
            st.subheader(f"T4 Preview — {t4_emp} ({_t4_sel_year})")
            st.caption(
                f"Based on {t4_data['entry_count']} payroll "
                f"{'entries' if t4_data['entry_count'] != 1 else 'entry'}")

            box_rows = [
                {"Box": "14", "Description": "Employment income",
                 "Amount": f"${t4_data['box_14']:,.2f}"},
                {"Box": "16", "Description": "Employee's CPP contributions",
                 "Amount": f"${t4_data['box_16']:,.2f}"},
                {"Box": "26", "Description": "CPP/QPP pensionable earnings",
                 "Amount": f"${t4_data['box_26']:,.2f}"},
            ]
            if t4_data.get("box_16a", 0) > 0:
                box_rows.append(
                    {"Box": "16A", "Description": "Employee's CPP2 contributions",
                     "Amount": f"${t4_data['box_16a']:,.2f}"})
            box_rows += [
                {"Box": "18", "Description": "Employee's EI premiums",
                 "Amount": f"${t4_data['box_18']:,.2f}"},
                {"Box": "24", "Description": "EI insurable earnings",
                 "Amount": f"${t4_data['box_24']:,.2f}"},
                {"Box": "22", "Description": "Income tax deducted",
                 "Amount": f"${t4_data['box_22']:,.2f}"},
            ]

            df_t4 = pd.DataFrame(box_rows)

            def _style_t4(df):
                s = pd.DataFrame("", index=df.index, columns=df.columns)
                for idx in df.index:
                    s.loc[idx, "Amount"] = (
                        "background-color: #e3f2fd; font-weight: bold")
                    s.loc[idx, "Box"] = (
                        "background-color: #f5f5f0; font-weight: bold")
                return s

            styled_t4 = df_t4.style.apply(_style_t4, axis=None)
            st.dataframe(styled_t4, use_container_width=True,
                         hide_index=True)

            # Tax breakdown
            st.markdown("**Tax deduction breakdown (Box 22):**")
            tbc1, tbc2 = st.columns(2)
            with tbc1:
                st.metric("Federal Tax", f"${t4_data['fed_tax']:,.2f}")
            with tbc2:
                st.metric("Provincial Tax (AB)",
                          f"${t4_data['prov_tax']:,.2f}")

            # ── Preview / Generate buttons ──
            st.markdown("---")
            _t4_pv_col, _t4_gen_col = st.columns(2)
            with _t4_pv_col:
                _do_t4_preview = st.button("Preview T4",
                                           use_container_width=True)
            with _t4_gen_col:
                _do_t4_generate = st.button("Generate T4 PDF",
                                            type="primary",
                                            use_container_width=True)

            # Clear preview when Generate is clicked
            if _do_t4_generate:
                st.session_state.pop("_t4_preview_img", None)

            if _do_t4_preview:
                try:
                    import tempfile
                    _tmp_t4 = tempfile.NamedTemporaryFile(
                        suffix=".pdf", delete=False)
                    _tmp_t4.close()
                    generate_t4_pdf(
                        t4_emp, _t4_sel_year, t4_data, profile,
                        output_path=_tmp_t4.name)
                    _t4_doc = fitz.open(_tmp_t4.name)
                    _t4_pix = _t4_doc[0].get_pixmap(dpi=150)
                    st.session_state["_t4_preview_img"] = _t4_pix.tobytes("png")
                    _t4_doc.close()
                    os.remove(_tmp_t4.name)
                except Exception as _e:
                    st.error(f"Preview error: {_e}")

            if st.session_state.get("_t4_preview_img"):
                with st.expander("T4 Preview", expanded=True):
                    _t4_pad, _t4_center, _t4_pad2 = st.columns([1, 3, 1])
                    with _t4_center:
                        st.image(st.session_state["_t4_preview_img"],
                                 use_container_width=True)
                    if st.button("Close Preview", key="close_t4_preview"):
                        st.session_state.pop("_t4_preview_img", None)
                        st.rerun()

            if _do_t4_generate:
                try:
                    _t4_path = generate_t4_pdf(
                        t4_emp, _t4_sel_year, t4_data, profile)
                    st.success(f"T4 generated: {os.path.basename(_t4_path)}")
                    _t4_bytes = _get_pdf_bytes(_t4_path)
                    if _t4_bytes:
                        st.download_button(
                            "Download T4 PDF",
                            data=_t4_bytes,
                            file_name=os.path.basename(_t4_path),
                            mime="application/pdf",
                            key="dl_t4")
                except Exception as _e:
                    st.error(f"Generation error: {_e}")

    # ── T4 Summary (all employees) ─────────────────────────────
    st.markdown("---")
    st.subheader(f"T4 Summary — All Employees ({_t4_sel_year})")

    _all_emps = get_existing_employees()
    _summary_rows = []
    _totals = {"box_14": 0, "box_16": 0, "box_16a": 0,
               "box_18": 0, "box_22": 0, "box_24": 0, "box_26": 0}
    _emp_count = 0

    for _emp in _all_emps:
        _edata = aggregate_t4_data(_emp, _t4_sel_year)
        if _edata is None:
            continue
        _emp_count += 1
        for k in _totals:
            _totals[k] += _edata.get(k, 0)
        _summary_rows.append({
            "Employee": _emp,
            "Box 14 (Income)": f"${_edata['box_14']:,.2f}",
            "Box 16 (CPP)": f"${_edata['box_16']:,.2f}",
            "Box 18 (EI)": f"${_edata['box_18']:,.2f}",
            "Box 22 (Tax)": f"${_edata['box_22']:,.2f}",
        })

    if _summary_rows:
        # Per-employee breakdown
        _df_summary = pd.DataFrame(_summary_rows)
        st.dataframe(_df_summary, use_container_width=True,
                     hide_index=True)

        # Totals row
        st.markdown(f"**Totals across {_emp_count} "
                    f"{'employees' if _emp_count != 1 else 'employee'}:**")
        _tc1, _tc2, _tc3, _tc4 = st.columns(4)
        with _tc1:
            st.metric("Total Income (14)",
                      f"${_totals['box_14']:,.2f}")
        with _tc2:
            st.metric("Total CPP (16)",
                      f"${_totals['box_16']:,.2f}")
        with _tc3:
            st.metric("Total EI (18)",
                      f"${_totals['box_18']:,.2f}")
        with _tc4:
            st.metric("Total Tax (22)",
                      f"${_totals['box_22']:,.2f}")

        # ── T4 Summary (T4SUM) — CRA filing form ──
        st.markdown("---")
        st.subheader("T4 Summary Filing Form (T4SUM)")
        st.caption("CRA employer filing form — totals all T4 slips for the year.")

        _t4sum_data = aggregate_t4sum_data(_t4_sel_year)

        if _t4sum_data:
            # Show key T4SUM boxes
            _s1, _s2, _s3 = st.columns(3)
            with _s1:
                st.metric("Box 80 (Total Reported)",
                          f"${_t4sum_data['box_80']:,.2f}")
            with _s2:
                st.metric("Box 82 (Total Remitted)",
                          f"${_t4sum_data['box_82']:,.2f}")
            with _s3:
                _diff = _t4sum_data["difference"]
                if _diff >= 0:
                    st.metric("Overpayment", f"${_diff:,.2f}")
                else:
                    st.metric("Balance Owing", f"${abs(_diff):,.2f}")

            # Preview / Generate buttons — same pattern as T4 slips
            _sum_pv_col, _sum_gen_col = st.columns(2)
            with _sum_pv_col:
                _do_sum_preview = st.button("Preview T4 Summary",
                                            use_container_width=True)
            with _sum_gen_col:
                _do_sum_generate = st.button("Generate T4 Summary PDF",
                                             type="primary",
                                             use_container_width=True)

            # Clear preview when Generate is clicked
            if _do_sum_generate:
                st.session_state.pop("_t4sum_preview_img", None)

            if _do_sum_preview:
                try:
                    import tempfile
                    _tmp_sum = tempfile.NamedTemporaryFile(
                        suffix=".pdf", delete=False)
                    _tmp_sum.close()
                    generate_t4sum_pdf(_t4_sel_year, _t4sum_data,
                                      output_path=_tmp_sum.name)
                    _sum_doc = fitz.open(_tmp_sum.name)
                    _sum_pix = _sum_doc[0].get_pixmap(dpi=150)
                    st.session_state["_t4sum_preview_img"] = (
                        _sum_pix.tobytes("png"))
                    _sum_doc.close()
                    os.remove(_tmp_sum.name)
                except Exception as _e:
                    st.error(f"Preview error: {_e}")

            if st.session_state.get("_t4sum_preview_img"):
                with st.expander("T4 Summary Preview", expanded=True):
                    _sp1, _sp2, _sp3 = st.columns([1, 3, 1])
                    with _sp2:
                        st.image(st.session_state["_t4sum_preview_img"],
                                 use_container_width=True)
                    if st.button("Close Preview", key="close_t4sum_preview"):
                        st.session_state.pop("_t4sum_preview_img", None)
                        st.rerun()

            if _do_sum_generate:
                try:
                    _sum_path = generate_t4sum_pdf(_t4_sel_year, _t4sum_data)
                    st.success(
                        f"T4 Summary generated: {os.path.basename(_sum_path)}")
                    _sum_bytes = _get_pdf_bytes(_sum_path)
                    if _sum_bytes:
                        st.download_button(
                            "Download T4 Summary PDF",
                            data=_sum_bytes,
                            file_name=os.path.basename(_sum_path),
                            mime="application/pdf",
                            key="dl_t4sum")
                except Exception as _e:
                    st.error(f"Generation error: {_e}")

    # ── T2200 — Declaration of Conditions of Employment ──
    st.markdown("---")
    st.subheader(f"T2200 — Declaration of Conditions ({_t4_sel_year})")
    st.caption(
        "CRA T2200 declares conditions of employment. Required if "
        "an employee claims employment expenses on their personal return.")

    if selected != "+ New Employee":
        _t2200_profile = load_profile(selected)
        _t2200_all = _t2200_profile.get("t2200", {})
        _t2200_year = _t2200_all.get(str(_t4_sel_year), {})

        st.markdown(f"**Employee: {selected}**")
        _t2200_start = _t2200_profile.get("start_date", "")
        _t2200_end = _t2200_profile.get("end_date")
        _t2200_period_end = (_t2200_end if _t2200_end
                              else f"{_t4_sel_year}-12-31")
        st.caption(f"Period: {_t2200_start} to {_t2200_period_end}")

        _q1 = st.checkbox(
            "Did the employee pay for supplies used in their duties?",
            value=_t2200_year.get("supplies", False),
            key="t2200_supplies")
        _q2 = st.checkbox(
            "Did the employee use a personal vehicle for work?",
            value=_t2200_year.get("vehicle", False),
            key="t2200_vehicle")
        _q3 = st.checkbox(
            "Did the employee work from home?",
            value=_t2200_year.get("home_office", False),
            key="t2200_home")
        _q3_pct = 0
        if _q3:
            _q3_pct = st.slider(
                "Percentage of time working from home",
                0, 100,
                value=_t2200_year.get("home_pct", 0),
                key="t2200_home_pct")
        _q4 = st.checkbox(
            "Did the employee pay for home office expenses?",
            value=_t2200_year.get("home_expenses", False),
            key="t2200_home_exp")
        _q5 = st.checkbox(
            "Did the employee pay for tools required for work?",
            value=_t2200_year.get("tools", False),
            key="t2200_tools")

        if st.button("Save T2200 Declarations", key="btn_save_t2200"):
            _t2200_data = {
                "supplies": _q1,
                "vehicle": _q2,
                "home_office": _q3,
                "home_pct": _q3_pct if _q3 else 0,
                "home_expenses": _q4,
                "tools": _q5,
            }
            _t2200_all[str(_t4_sel_year)] = _t2200_data
            save_profile(selected, {"t2200": _t2200_all})
            st.success(f"T2200 declarations saved for {_t4_sel_year}.")

        st.caption(
            f"For most {_co.get('operating_as', _co['legal_name'])} employees "
            "all answers will be 'No'. "
            "This section is available for CRA compliance if requested.")
    else:
        st.info("Select an employee to manage T2200 declarations.")


# ══════════════════════════════════════════════════════════════
#  TAB 5: RECORD OF EMPLOYMENT (ROE)
# ══════════════════════════════════════════════════════════════

with tab_roe:
    st.title("Record of Employment")
    st.markdown("Generate ROE reference documents for Service Canada (ROE Web).")

    if selected == "+ New Employee":
        st.info("Select an employee from the sidebar to generate an ROE.")
    else:
        _roe_profile = load_profile(selected)
        _roe_end_str = _roe_profile.get("end_date", "")
        _roe_end = (date.fromisoformat(_roe_end_str) if _roe_end_str else None)
        _roe_start_str = _roe_profile.get("start_date", "")
        _roe_start = (date.fromisoformat(_roe_start_str)
                      if _roe_start_str else None)
        _roe_wh = _roe_profile.get("wage_history", [])
        _emp_conf = load_employer()

        # Pre-initialize variables used across conditional blocks
        _roe_filtered = []
        _hrs_overrides = {}
        _other_monies = []

        # ── Offboarding Checklist ─────────────────────────────
        if _roe_end:
            with st.expander("Offboarding Checklist", expanded=True):
                _ob_checks = []

                # 1. End date set
                _ob_checks.append(("End date recorded", True))

                # 2. Final payroll generated
                _emp_dir = os.path.join(PAYROLL_DIR, "employees", selected)
                _has_final = bool(glob.glob(
                    os.path.join(_emp_dir, "Payroll_*.pdf")))
                if not _has_final:
                    try:
                        for _fy, _fm in available_months()[0]:
                            for _fe in _db.get_remittances(_fy, _fm):
                                if _fe.get("employee") == selected:
                                    _has_final = True
                                    break
                            if _has_final:
                                break
                    except Exception:
                        pass
                _ob_checks.append(("Final payroll generated", _has_final))

                # 3. Vacation pay settled
                _vac_data = get_vacation_accrual(selected)
                _vac_settled = False
                if _vac_data:
                    _vac_settled = abs(_vac_data.get("balance", 999)) < 1.00
                _ob_checks.append(("Vacation pay settled", _vac_settled))

                # 4. ROE generated
                _roe_hist = load_roe_history(selected)
                _ob_checks.append(("ROE generated", bool(_roe_hist)))

                # 5. Termination letter
                _end_year = _roe_end.year
                _has_term = bool(glob.glob(
                    os.path.join(_emp_dir, "Letters", "Termination_*.pdf")))
                if not _has_term and _sidebar_emp_uuid:
                    try:
                        _term_files = _sc.list_files(
                            _sc.PDF_BUCKET,
                            f"employees/{_sidebar_emp_uuid}/letters")
                        _has_term = any("Termination" in f for f in _term_files)
                    except Exception:
                        pass
                _ob_checks.append((
                    "Termination letter generated", _has_term))

                # 6. T4 for final year
                _has_t4 = bool(glob.glob(
                    os.path.join(_emp_dir, "T4", f"T4_{_end_year}_*.pdf")))
                if not _has_t4 and _sidebar_emp_uuid:
                    try:
                        _t4_files = _sc.list_files(
                            _sc.PDF_BUCKET,
                            f"employees/{_sidebar_emp_uuid}/t4")
                        _has_t4 = any(f"T4_{_end_year}" in f for f in _t4_files)
                    except Exception:
                        pass
                _ob_checks.append((
                    f"T4 for {_end_year}", _has_t4))

                # Render
                _done = sum(1 for _, ok in _ob_checks if ok)
                st.progress(_done / len(_ob_checks),
                            text=f"{_done}/{len(_ob_checks)} complete")
                for _label, _ok in _ob_checks:
                    if _ok:
                        st.markdown(f"- :white_check_mark: ~~{_label}~~")
                    else:
                        st.markdown(f"- :x: **{_label}**")

            st.markdown("---")

        # ── Termination Letter ────────────────────────────────
        if _roe_end:
            st.subheader("Termination Letter")
            st.caption("Generate a formal letter for the employee's file.")

            _tc1, _tc2 = st.columns(2)
            with _tc1:
                _term_type = st.selectbox(
                    "Termination Type",
                    ["without_cause", "with_cause", "resignation_accepted"],
                    format_func=lambda x: {
                        "without_cause": "Without Cause",
                        "with_cause": "With Cause",
                        "resignation_accepted": "Resignation Accepted"
                    }[x],
                    key="roe_term_type")
            with _tc2:
                _term_final_pay = st.date_input(
                    "Final Pay Date",
                    value=_roe_end,
                    key="roe_term_final_pay")
            _term_reason = st.text_area(
                "Reason / Description",
                placeholder="Describe the reason for separation...",
                key="roe_term_reason")
            _term_severance = st.number_input(
                "Severance Amount ($)", min_value=0.0, step=100.0,
                value=0.0, key="roe_term_severance")
            _term_comments = st.text_area(
                "Additional Comments",
                placeholder="Any additional notes for the letter...",
                key="roe_term_comments")

            if st.button("Generate Termination Letter",
                         key="btn_gen_term_letter"):
                if not _term_reason.strip():
                    st.warning("Please provide a reason for the termination.")
                else:
                    try:
                        _term_path = generate_termination_letter(
                            selected, _roe_profile,
                            _term_type, _term_reason.strip(),
                            _term_final_pay.isoformat(),
                            severance=_term_severance,
                            comments=_term_comments.strip())
                        st.session_state["_term_letter_path"] = _term_path
                        st.success(
                            f"Termination letter generated: "
                            f"{os.path.basename(_term_path)}")
                    except Exception as _e:
                        st.error(f"Error generating letter: {_e}")

            if st.session_state.get("_term_letter_path"):
                _tlp = st.session_state["_term_letter_path"]
                _tlp_bytes = _get_pdf_bytes(_tlp)
                if _tlp_bytes:
                    st.download_button(
                        f"Download {os.path.basename(_tlp)}",
                        data=_tlp_bytes,
                        file_name=os.path.basename(_tlp),
                        mime="application/pdf",
                        key="dl_term_letter")

            st.markdown("---")

        # ── Blocks 3-5: Employer Information (read-only) ────
        st.subheader("Employer Information")
        st.caption("Blocks 3–5 — auto-populated from employer config")
        _ei_c1, _ei_c2, _ei_c3 = st.columns(3)
        with _ei_c1:
            st.text_input(
                "Block 3 — Employer Name",
                value=_emp_conf.get("legal_name", ""),
                disabled=True, key="roe_b3")
        with _ei_c2:
            _addr_lines = _emp_conf.get("address_lines", [])
            st.text_input(
                "Block 4 — Employer Address",
                value=", ".join(a for a in _addr_lines if a),
                disabled=True, key="roe_b4")
        with _ei_c3:
            st.text_input(
                "Block 5 — CRA Business Number",
                value=_emp_conf.get("cra_bn", ""),
                disabled=True, key="roe_b5")

        # ── Block 6: Pay Period Type (read-only) ────────────
        st.text_input(
            "Block 6 — Pay Period Type",
            value="S — Semi-monthly (24 pay periods/year)",
            disabled=True, key="roe_b6")

        # ── Blocks 8-9: Employee Information (read-only) ────
        st.markdown("---")
        st.subheader("Employee Information")
        st.caption("Blocks 8–9 — from employee profile")

        _sin_val = _roe_profile.get("sin", "")
        _emp_name_val = _roe_profile.get("employee_name", selected)
        _emp_addr = _roe_profile.get("employee_address", [])
        _emp_addr_str = ", ".join(a for a in _emp_addr if a)

        _ee_c1, _ee_c2 = st.columns(2)
        with _ee_c1:
            st.text_input(
                "Block 8 — Social Insurance Number",
                value=_sin_val if _sin_val else "Not on file",
                disabled=True, key="roe_b8")
        with _ee_c2:
            st.text_input(
                "Block 9 — Employee Name",
                value=_emp_name_val,
                disabled=True, key="roe_b9_name")
        st.text_input(
            "Block 9 — Employee Address",
            value=_emp_addr_str if _emp_addr_str else "Not on file",
            disabled=True, key="roe_b9_addr")

        if not _sin_val:
            st.warning(
                "SIN not on file. Add it in the Payroll tab "
                "(Employee Info section) before generating the ROE.")

        # ── Blocks 10-12: Employment Period ─────────────────
        st.markdown("---")
        st.subheader("Employment Period")
        st.caption("Blocks 10–12")

        _ep_c1, _ep_c2, _ep_c3 = st.columns(3)
        with _ep_c1:
            st.text_input(
                "Block 10 — First Day Worked",
                value=(_roe_start.strftime("%Y-%m-%d")
                       if _roe_start else "Not set"),
                disabled=True, key="roe_b10")
        with _ep_c2:
            _ldp_default = _roe_end if _roe_end else today
            _roe_last_day_paid = st.date_input(
                "Block 11 — Last Day for Which Paid",
                value=_ldp_default,
                key="roe_ldp")
        with _ep_c3:
            # Block 12 — auto-calculated from Block 11
            from roe_generator import _final_pay_period_end
            _b12_date = _final_pay_period_end(_roe_last_day_paid)
            st.text_input(
                "Block 12 — Final Pay Period Ending Date",
                value=_b12_date.strftime("%Y-%m-%d"),
                disabled=True, key="roe_b12")

        if _roe_end:
            st.caption(
                f"Employment ended: {_roe_end.strftime('%B %d, %Y')}")

        # ── Blocks 13-14: Occupation & Recall ───────────────
        st.markdown("---")
        st.subheader("Occupation & Recall")
        st.caption("Blocks 13–14")

        _or_c1, _or_c2 = st.columns(2)
        with _or_c1:
            st.text_input(
                "Block 13 — Occupation",
                value=_roe_profile.get("position", ""),
                disabled=True, key="roe_b13")
        with _or_c2:
            _recall_options = ["Not returning", "Unknown", "Specific date"]
            _roe_recall = st.selectbox(
                "Block 14 — Expected Date of Recall",
                options=_recall_options,
                key="roe_recall")

        _roe_recall_date = None
        if _roe_recall == "Specific date":
            _roe_recall_date = st.date_input(
                "Recall Date",
                value=today + timedelta(days=90),
                key="roe_recall_date")

        # ── Block 15: Insurable Earnings & Hours ────────────
        st.markdown("---")
        st.subheader("Insurable Earnings & Hours")
        st.caption("Blocks 15A–15C")

        _roe_entries = get_employee_remittance_entries(selected)

        if not _roe_entries:
            st.warning(
                f"No remittance data found for {selected}. "
                f"Generate payrolls first to populate ROE earnings.")
        else:
            # Filter entries up to last day paid
            _roe_filtered = [
                e for e in _roe_entries
                if e["payment_date"] <= _roe_last_day_paid]

            if not _roe_filtered:
                st.warning(
                    f"No remittance entries on or before "
                    f"{_roe_last_day_paid.strftime('%B %d, %Y')}.")
            else:
                _roe_period_data = calculate_insurable_hours(
                    _roe_filtered, _roe_wh)

                # Block 15C — earnings by pay period table
                st.markdown("**Block 15C — Insurable Earnings by Pay Period**")
                _roe_rows = []
                for _pp in _roe_period_data:
                    _roe_rows.append({
                        "P.P.#": _pp["pp_num"],
                        "Period": _pp["period_str"],
                        "Payment Date": _pp["payment_date"].strftime(
                            "%b %d, %Y"),
                        "Earnings": f"${_pp['gross']:,.2f}",
                        "Rate": f"${_pp['rate']:.2f}/hr",
                        "Hours": f"{_pp['hours']:.1f}",
                        "Source": _pp["source"].title(),
                    })

                if _roe_rows:
                    _roe_df = pd.DataFrame(_roe_rows)

                    def _style_roe_table(df):
                        s = pd.DataFrame("", index=df.index,
                                         columns=df.columns)
                        for idx in df.index:
                            s.loc[idx, "Earnings"] = (
                                "background-color: #e3f2fd; "
                                "font-weight: bold")
                            s.loc[idx, "Hours"] = (
                                "background-color: #fff3e0; "
                                "font-weight: bold")
                            if df.loc[idx, "Source"] == "Estimated":
                                s.loc[idx, "Source"] = (
                                    "color: #e65100; "
                                    "font-style: italic")
                        return s

                    st.dataframe(
                        _roe_df.style.apply(_style_roe_table, axis=None),
                        use_container_width=True, hide_index=True)

                    # Block 15A & 15B — totals
                    _total_hrs = round(sum(
                        p["hours"] for p in _roe_period_data[:25]), 2)
                    _total_earn = round(sum(
                        p["gross"] for p in _roe_period_data[:13]), 2)
                    _mc1, _mc2, _mc3 = st.columns(3)
                    with _mc1:
                        st.metric("Block 15A — Total Insurable Hours",
                                  f"{_total_hrs:.1f}")
                    with _mc2:
                        st.metric("Block 15B — Total Insurable Earnings",
                                  f"${_total_earn:,.2f}")
                    with _mc3:
                        st.metric("Pay Periods", len(_roe_period_data))

                    if any(p["source"] == "estimated"
                           for p in _roe_period_data):
                        st.caption(
                            "Hours marked *Estimated* are calculated from "
                            "gross \u00f7 hourly rate. Verify accuracy and "
                            "override below if needed.")

                    # Hours override section
                    with st.expander("Adjust Hours (manual overrides)"):
                        st.caption(
                            "Override estimated hours for any pay period. "
                            "Leave at 0 to use the calculated value.")
                        _hrs_overrides = {}
                        _ovr_cols = st.columns(
                            min(len(_roe_period_data), 4))
                        for _oi, _pp in enumerate(_roe_period_data):
                            with _ovr_cols[_oi % len(_ovr_cols)]:
                                _ovr_val = st.number_input(
                                    f"P.P. {_pp['pp_num']}",
                                    min_value=0.0,
                                    max_value=300.0,
                                    value=0.0,
                                    step=0.5,
                                    key=f"roe_hrs_{_pp['pp_num']}")
                                if _ovr_val > 0:
                                    _hrs_overrides[_pp["pp_num"]] = _ovr_val

        # ── Block 16: Reason for Issuing ────────────────────
        st.markdown("---")
        st.subheader("Reason for Issuing")
        st.caption("Block 16")

        _reason_options = [f"{k} — {v}" for k, v in ROE_REASON_CODES.items()]
        # Default to "M" (Dismissal) if terminated, else "K" (Other)
        _default_idx = 0
        if _roe_end:
            _reason_keys = list(ROE_REASON_CODES.keys())
            if "M" in _reason_keys:
                _default_idx = _reason_keys.index("M")
        _roe_reason_sel = st.selectbox(
            "Block 16 — Reason for Issuing this ROE",
            options=_reason_options,
            index=_default_idx,
            key="roe_reason")
        _roe_reason_code = _roe_reason_sel.split(" \u2014 ")[0]

        # Guard: separation reasons require end_date
        _roe_is_separation = _roe_reason_code in SEPARATION_CODES
        if _roe_is_separation and not _roe_end:
            st.warning(
                f"Reason code **{_roe_reason_code}** is a separation reason. "
                f"Set the employee's **End Date** in the Payroll tab first.")

        # ── Blocks 17A-C: Separation Payments ───────────────
        st.markdown("---")
        st.subheader("Separation Payments")
        st.caption("Blocks 17A–17C")

        # 17A — Vacation pay
        _vac_data = get_vacation_accrual(selected)
        _vac_balance = (_vac_data["balance"]
                       if _vac_data else 0.0)
        _roe_vac_pay = st.number_input(
            "Block 17A — Vacation Pay",
            min_value=0.0,
            value=round(_vac_balance, 2),
            step=0.01,
            format="%.2f",
            key="roe_vac_pay",
            help="Auto-filled from vacation tracker. Edit if needed.")

        # 17B — Stat holiday pay (auto-calculated, shown as info)
        st.text_input(
            "Block 17B — Statutory Holiday Pay",
            value="Auto-calculated from stat holidays after last day paid",
            disabled=True,
            key="roe_stat_info")

        # 17C — Other monies
        with st.expander("Block 17C — Other Monies"):
            st.caption(
                "Add severance, bonuses, or other payments not included "
                "in regular pay.")
            _other_monies = []
            _om_count = st.number_input(
                "Number of other payment entries",
                min_value=0, max_value=10, value=0,
                step=1, key="roe_om_count")
            for _omi in range(int(_om_count)):
                _om_r1, _om_r2, _om_r3 = st.columns([2, 3, 2])
                with _om_r1:
                    _om_type = st.text_input(
                        "Type", value="", key=f"roe_om_type_{_omi}")
                with _om_r2:
                    _om_desc = st.text_input(
                        "Description", value="",
                        key=f"roe_om_desc_{_omi}")
                with _om_r3:
                    _om_amt = st.number_input(
                        "Amount", min_value=0.0, value=0.0,
                        step=0.01, format="%.2f",
                        key=f"roe_om_amt_{_omi}")
                if _om_type and _om_amt > 0:
                    _other_monies.append({
                        "type": _om_type,
                        "description": _om_desc,
                        "amount": _om_amt,
                    })

        # ── Block 18: Comments ──────────────────────────────
        st.markdown("---")
        _roe_comments = st.text_area(
            "Block 18 — Comments",
            value="",
            key="roe_comments",
            help="Required for reason code K (Other). "
                 "Optional for all other codes.")

        # ── Blocks 20-22: Contact & Certification ───────────
        st.markdown("---")
        st.subheader("Contact & Certification")
        st.caption("Blocks 20–22")

        _ct_c1, _ct_c2, _ct_c3 = st.columns(3)
        with _ct_c1:
            st.text_input(
                "Block 20 — Communication Language",
                value="English",
                disabled=True, key="roe_b20")
        with _ct_c2:
            _roe_phone = st.text_input(
                "Block 21 — Contact Telephone",
                value="",
                key="roe_contact_phone")
        with _ct_c3:
            _roe_contact = st.text_input(
                "Block 22 — Issuer Name",
                value=_emp_conf.get("signer_name", ""),
                key="roe_contact_name")

        # ── Preview / Generate buttons ──────────────────────
        st.markdown("---")

        _roe_can_generate = bool(_roe_entries) and bool(_roe_filtered)

        _roe_pv_col, _roe_gen_col = st.columns(2)
        with _roe_pv_col:
            _do_roe_preview = st.button(
                "Preview ROE",
                use_container_width=True,
                disabled=not _roe_can_generate)
        with _roe_gen_col:
            _do_roe_generate = st.button(
                "Generate ROE PDF",
                type="primary",
                use_container_width=True,
                disabled=not _roe_can_generate)

        # Collect hours overrides
        _final_hrs_overrides = _hrs_overrides

        # Clear preview when Generate is clicked
        if _do_roe_generate:
            st.session_state.pop("_roe_preview_img", None)

        if _do_roe_preview and _roe_can_generate:
            try:
                import tempfile
                _roe_data = assemble_roe_data(
                    employee=selected,
                    profile=_roe_profile,
                    reason_code=_roe_reason_code,
                    last_day_paid=_roe_last_day_paid,
                    expected_recall=_roe_recall,
                    recall_date=_roe_recall_date,
                    comments=_roe_comments,
                    other_monies=_other_monies,
                    contact_name=_roe_contact,
                    contact_phone=_roe_phone,
                    hours_override=_final_hrs_overrides,
                    vacation_pay_override=_roe_vac_pay)

                _tmp_roe = tempfile.NamedTemporaryFile(
                    suffix=".pdf", delete=False)
                _tmp_roe.close()
                generate_roe_pdf(_roe_data, output_path=_tmp_roe.name)
                _roe_doc = fitz.open(_tmp_roe.name)
                _roe_imgs = []
                for _pi in range(len(_roe_doc)):
                    _roe_pix = _roe_doc[_pi].get_pixmap(dpi=150)
                    _roe_imgs.append(_roe_pix.tobytes("png"))
                st.session_state["_roe_preview_img"] = _roe_imgs
                _roe_doc.close()
                os.remove(_tmp_roe.name)
            except Exception as _e:
                st.error(f"Preview error: {_e}")

        if st.session_state.get("_roe_preview_img"):
            with st.expander("ROE Preview", expanded=True):
                _rp1, _rp2, _rp3 = st.columns([1, 3, 1])
                with _rp2:
                    for _img_bytes in st.session_state["_roe_preview_img"]:
                        st.image(_img_bytes, use_container_width=True)
                if st.button("Close Preview", key="close_roe_preview"):
                    st.session_state.pop("_roe_preview_img", None)
                    st.rerun()

        if _do_roe_generate and _roe_can_generate:
            try:
                _roe_data = assemble_roe_data(
                    employee=selected,
                    profile=_roe_profile,
                    reason_code=_roe_reason_code,
                    last_day_paid=_roe_last_day_paid,
                    expected_recall=_roe_recall,
                    recall_date=_roe_recall_date,
                    comments=_roe_comments,
                    other_monies=_other_monies,
                    contact_name=_roe_contact,
                    contact_phone=_roe_phone,
                    hours_override=_final_hrs_overrides,
                    vacation_pay_override=_roe_vac_pay)

                _roe_pdf_path = generate_roe_pdf(_roe_data)
                from roe_generator import save_roe_metadata
                save_roe_metadata(selected, _roe_data, _roe_pdf_path)

                st.success(
                    f"ROE generated: {os.path.basename(_roe_pdf_path)}")
                _roe_bytes = _get_pdf_bytes(_roe_pdf_path)
                if _roe_bytes:
                    st.download_button(
                        "Download ROE PDF",
                        data=_roe_bytes,
                        file_name=os.path.basename(_roe_pdf_path),
                        mime="application/pdf",
                        key="dl_roe")
            except Exception as _e:
                st.error(f"Generation error: {_e}")

        # ── ROE History ──────────────────────────────────────
        st.markdown("---")
        st.subheader("ROE History")

        _roe_history = load_roe_history(selected)
        if _roe_history:
            _hist_rows = []
            for _rh in _roe_history:
                _hist_rows.append({
                    "Date": _rh.get("generation_date", ""),
                    "Reason": (f"{_rh.get('reason_code', '')} \u2014 "
                               f"{_rh.get('reason_description', '')}"),
                    "Last Day Paid": _rh.get("last_day_paid", ""),
                    "Total Hours": _rh.get("total_insurable_hours", ""),
                    "Total Earnings": (
                        f"${_rh.get('total_insurable_earnings', 0):,.2f}"),
                    "PDF": os.path.basename(
                        _rh.get("pdf_path", "")),
                })
            st.dataframe(pd.DataFrame(_hist_rows),
                         use_container_width=True, hide_index=True)

            for _rh in _roe_history:
                _rh_path = _rh.get("pdf_path", "")
                if _rh_path:
                    _rh_bytes = _get_pdf_bytes(_rh_path)
                    if _rh_bytes:
                        st.download_button(
                            f"Download {os.path.basename(_rh_path)}",
                            data=_rh_bytes,
                            file_name=os.path.basename(_rh_path),
                            mime="application/pdf",
                            key=f"dl_roe_hist_{_rh.get('generation_date', '')}")
        else:
            st.caption("No ROE documents have been generated for this employee.")


# ══════════════════════════════════════════════════════════════
#  TAB 6: INSIGHTS
# ══════════════════════════════════════════════════════════════

with tab_insights:
    st.header("Insights")
    st.caption("Tax optimization and employer cost analysis")

    if selected == "+ New Employee":
        st.info("Select an employee to view insights.")
    else:
        _ins_profile = load_profile(selected)
        _ins_wh = _ins_profile.get("wage_history", [])
        _ins_rate = _ins_wh[-1]["rate"] if _ins_wh else 0
        _ins_sched = _ins_profile.get("schedule_type", DEFAULT_SCHEDULE_TYPE)
        _ins_sched_info = get_schedule(_ins_sched)
        _ins_dpw = _ins_sched_info.get("days_per_week", 5)
        _ins_hpd = _ins_sched_info.get("hours_per_day", 8)
        _ins_start = _ins_profile.get("start_date")
        _ins_end_str = _ins_profile.get("end_date")
        _ins_end_date = (date.fromisoformat(_ins_end_str)
                         if _ins_end_str else None)
        _ins_year = date.today().year

        # TD1 claims from profile
        _ins_td1_fed = _ins_profile.get("td1_fed_claim", 0.0)
        _ins_td1_prov = _ins_profile.get("td1_prov_claim", 0.0)
        _ins_td1_extra = _ins_profile.get("td1_extra_deduction", 0.0)
        _ins_td1_exempt = _ins_profile.get("td1_exempt", False)

        # ── EMPLOYEE INSIGHTS ────────────────────────────────
        st.subheader("Employee Tax Profile")

        _ins_ec1, _ins_ec2 = st.columns(2)

        with _ins_ec1:
            st.markdown("**TD1 Status**")
            if _ins_td1_exempt:
                st.success("Tax Exempt — no income tax withheld")
            elif _ins_td1_fed > 0 or _ins_td1_prov > 0:
                if _ins_td1_fed > 0:
                    st.info(f"Federal TD1 claim: **${_ins_td1_fed:,.2f}**")
                if _ins_td1_prov > 0:
                    st.info(f"Provincial TD1 claim: **${_ins_td1_prov:,.2f}**")
                if _ins_td1_extra > 0:
                    st.info(f"Additional deduction: **${_ins_td1_extra:,.2f}**/pay")
            else:
                st.warning(
                    "Using default BPA only. If employee has a spouse/partner, "
                    "disability, tuition, or other credits, update TD1 claims "
                    "in the Payroll tab under Employment Details to reduce "
                    "withholding.")

        with _ins_ec2:
            st.markdown("**Withholding Comparison**")
            # Compare default BPA vs current TD1 claims
            _sample_gross = round(_ins_rate * _ins_hpd * (_ins_dpw * 2), 2)
            if _sample_gross > 0:
                _est_default = estimate_deductions(
                    _sample_gross, pay_periods=24,
                    tax_year=_ins_year, pay_month=date.today().month)
                _est_td1 = estimate_deductions(
                    _sample_gross, pay_periods=24,
                    tax_year=_ins_year, pay_month=date.today().month,
                    td1_fed_claim=_ins_td1_fed,
                    td1_prov_claim=_ins_td1_prov,
                    td1_extra=_ins_td1_extra,
                    td1_exempt=_ins_td1_exempt)

                _def_total = (_est_default["cpp"] + _est_default["cpp2"] +
                              _est_default["ei"] + _est_default["fed_tax"] +
                              _est_default["prov_tax"])
                _td1_total = (_est_td1["cpp"] + _est_td1["cpp2"] +
                              _est_td1["ei"] + _est_td1["fed_tax"] +
                              _est_td1["prov_tax"])
                _savings_per_pay = round(_def_total - _td1_total, 2)

                if _savings_per_pay > 0.01:
                    st.success(
                        f"TD1 claims save **${_savings_per_pay:,.2f}/pay** "
                        f"(~${round(_savings_per_pay * 24):,}/year)")
                    st.caption(
                        f"Based on ${_sample_gross:,.2f} gross per pay period")
                elif _ins_td1_exempt:
                    st.success(
                        f"Tax exempt saves **${round(_est_default['fed_tax'] + _est_default['prov_tax'], 2):,.2f}/pay** "
                        f"in income tax")
                else:
                    st.caption("No TD1 savings — using default BPA.")
            else:
                st.caption("Add wage history to see withholding comparison.")

        # ── Spousal/Common-Law Reminder ──
        st.markdown("---")
        st.markdown("**Tax Credit Reminders**")
        _reminder_items = []
        if _ins_td1_fed == 0:
            _reminder_items.append(
                "If employee has a **spouse/common-law partner** earning under the BPA, "
                "they may claim the spousal amount on TD1 (Line 30300)")
        _reminder_items.append(
            "Employees can claim **tuition, disability, caregiver** amounts on TD1 to reduce withholding")
        _reminder_items.append(
            "If total income will be **below the BPA** this year, employee can check 'Tax Exempt'")
        for item in _reminder_items:
            st.caption(f"- {item}")

        # ── EMPLOYER INSIGHTS ────────────────────────────────
        st.markdown("---")
        st.subheader("Employer Cost Analysis")

        # Get rate table for current year
        _half = 1 if date.today().month <= 6 else 2
        _rt = RATES.get((_ins_year, _half), RATES.get((_ins_year, 1), {}))
        _cpp_rate = _rt.get("cpp_rate", 0.0595)
        _ei_rate = _rt.get("ei_rate", 0.0164)

        _er_c1, _er_c2 = st.columns(2)

        with _er_c1:
            st.markdown("**Statutory Burden per $1 of Wages**")
            _cpp_er_pct = _cpp_rate * 100
            _ei_er_pct = _ei_rate * EI_EMPLOYER_MULTIPLE * 100
            _wcb_pct = WCB_RATE_PER_100
            _total_burden_pct = _cpp_er_pct + _ei_er_pct + _wcb_pct

            _burden_df = pd.DataFrame([
                {"Component": "CPP (employer match)", "Rate": f"{_cpp_er_pct:.2f}%",
                 "Per $1,000": f"${_cpp_rate * 1000:.2f}"},
                {"Component": "EI (1.4x employee)",
                 "Rate": f"{_ei_er_pct:.2f}%",
                 "Per $1,000": f"${_ei_rate * EI_EMPLOYER_MULTIPLE * 1000:.2f}"},
                {"Component": "WCB", "Rate": f"{_wcb_pct:.2f}%",
                 "Per $1,000": f"${_wcb_pct * 10:.2f}"},
                {"Component": "**Total**",
                 "Rate": f"**{_total_burden_pct:.2f}%**",
                 "Per $1,000": f"**${_total_burden_pct * 10:.2f}**"},
            ])
            st.dataframe(_burden_df, hide_index=True, use_container_width=True)
            st.caption(
                f"Every $1.00 of wages costs the employer "
                f"**${1 + _total_burden_pct / 100:.4f}**")

        with _er_c2:
            st.markdown(f"**{selected} — Cost Projection ({_ins_year})**")
            if _ins_rate > 0:
                _hrs_per_pay = _ins_hpd * _ins_dpw * 2  # semi-monthly ~ 2 weeks
                _gross_per_pay = round(_ins_rate * _hrs_per_pay, 2)
                _annual_gross = round(_gross_per_pay * 24, 2)
                _annual_burden = round(_annual_gross * _total_burden_pct / 100, 2)
                _annual_total = round(_annual_gross + _annual_burden, 2)

                st.metric("Hourly Rate", f"${_ins_rate:.2f}")
                st.metric("Est. Annual Gross", f"${_annual_gross:,.2f}")
                st.metric("Est. Employer Burden", f"${_annual_burden:,.2f}",
                           delta=f"+{_total_burden_pct:.1f}%", delta_color="inverse")
                st.metric("Est. Total Cost", f"${_annual_total:,.2f}")
            else:
                st.caption("Add wage history to see cost projection.")

        # ── Actualized Cost from Remittance Data ──
        _ec_data = _compute_employer_cost(selected, _ins_wh)
        if _ec_data:
            st.markdown("---")
            st.markdown("**Actualized Employer Cost (from payroll data)**")

            _ac1, _ac2, _ac3, _ac4 = st.columns(4)
            with _ac1:
                st.metric("Total Gross Paid",
                          f"${_ec_data['total_gross']:,.2f}")
            with _ac2:
                _er_total = round(
                    _ec_data["total_cpp_er"] + _ec_data["total_cpp2_er"] +
                    _ec_data["total_ei_er"] + _ec_data["total_wcb"], 2)
                st.metric("Total Employer Burden",
                          f"${_er_total:,.2f}")
            with _ac3:
                st.metric("True Cost/Hour",
                          f"${_ec_data['actualized_rate']:.2f}",
                          delta=f"+${round(_ec_data['actualized_rate'] - _ins_rate, 2):.2f} over wage")
            with _ac4:
                st.metric("Paystubs Processed",
                          f"{_ec_data['entry_count']}")

            # Monthly breakdown table
            if _ec_data.get("monthly"):
                with st.expander("Monthly Breakdown"):
                    _rows = []
                    for _mk in sorted(_ec_data["monthly"].keys()):
                        _mv = _ec_data["monthly"][_mk]
                        _rows.append({
                            "Month": _mk,
                            "Gross": f"${_mv['gross']:,.2f}",
                            "CPP (Er)": f"${_mv['cpp_er']:,.2f}",
                            "EI (Er)": f"${_mv['ei_er']:,.2f}",
                            "WCB": f"${_mv['wcb']:,.2f}",
                            "Total Cost": f"${_mv['total_cost']:,.2f}",
                            "$/hr": f"${_mv['actualized_rate']:.2f}",
                            "Est Hours": f"{_mv['est_hours']:.0f}",
                        })
                    st.dataframe(pd.DataFrame(_rows), hide_index=True,
                                 use_container_width=True)

        # ── Non-Taxable Allowance Optimizer ──
        st.markdown("---")
        st.markdown("**Non-Taxable Allowance Opportunities**")
        st.caption(
            "Non-taxable allowances bypass CPP, EI, WCB, and income tax — "
            "saving both employer and employee money.")

        _allow_data = [
            {
                "Allowance": "Vehicle (per-km)",
                "2026 CRA Rate": "$0.72/km (first 5,000) + $0.66/km after",
                "Example": "$200/month = $2,400/yr",
                "Employer Savings": f"~${round(2400 * _total_burden_pct / 100):,}/yr",
                "Requirement": "Reasonable estimate, employee tracks km",
            },
            {
                "Allowance": "Cell Phone",
                "2026 CRA Rate": "Reasonable amount",
                "Example": "$75/month = $900/yr",
                "Employer Savings": f"~${round(900 * _total_burden_pct / 100):,}/yr",
                "Requirement": "Business use documented",
            },
            {
                "Allowance": "Tools/Supplies",
                "2026 CRA Rate": "Actual cost",
                "Example": "$100/month = $1,200/yr",
                "Employer Savings": f"~${round(1200 * _total_burden_pct / 100):,}/yr",
                "Requirement": "Employee provides own tools for work",
            },
        ]
        st.dataframe(pd.DataFrame(_allow_data), hide_index=True,
                     use_container_width=True)

        # ── Stat Holiday Projection ──
        st.markdown("---")
        st.markdown("**Stat Holiday Cost Projection**")
        _stats_this_year = get_stats_in_period(
            date(_ins_year, 1, 1), date(_ins_year, 12, 31))

        if _ins_start:
            try:
                _start_dt = date.fromisoformat(_ins_start)
            except (ValueError, TypeError):
                _start_dt = None
        else:
            _start_dt = None

        if _stats_this_year:
            _stat_rows = []
            _total_stat_cost = 0
            for _sd, _sn, _is_pdo in _stats_this_year:
                _eligible = is_stat_eligible(
                    _start_dt, as_of=_sd,
                    days_per_week=_ins_dpw,
                    end_date=_ins_end_date) if _start_dt else False
                _day_cost = round(_ins_rate * _ins_hpd, 2) if _ins_rate > 0 else 0
                if _eligible:
                    _total_stat_cost += _day_cost
                _stat_rows.append({
                    "Date": _sd.strftime("%b %d"),
                    "Holiday": _sn,
                    "Eligible": "Yes" if _eligible else "No",
                    "Type": "Paid Day Off" if _is_pdo else "General",
                    "Est. Cost": f"${_day_cost:.2f}" if _eligible else "—",
                })
            st.dataframe(pd.DataFrame(_stat_rows), hide_index=True,
                         use_container_width=True)
            st.caption(
                f"Total estimated stat cost ({_ins_year}): "
                f"**${_total_stat_cost:,.2f}** "
                f"({_ins_dpw} days/week schedule — "
                f"eligibility threshold: {int(30 / _ins_dpw * 7) + 1} calendar days)")
        else:
            st.caption(f"No stat holiday data available for {_ins_year}.")


# ══════════════════════════════════════════════════════════════
#  TAB 7: DOCUMENTS
# ══════════════════════════════════════════════════════════════

with tab_docs:
    st.title("Documents")
    st.markdown("Browse and download all documents for the selected employee.")

    if selected == "+ New Employee":
        st.info("Select an employee from the sidebar to view their documents.")
    else:
        _docs_emp = selected

        # Resolve employee UUID for Storage listing
        _docs_uuid = None
        try:
            _docs_uuid = _db._employee_id(_docs_emp)
        except Exception:
            pass

        def _docs_list(storage_sub, local_pattern):
            """List files from Storage or local glob."""
            if _docs_uuid:
                try:
                    paths = _sc.list_files(
                        _sc.PDF_BUCKET,
                        f"employees/{_docs_uuid}/{storage_sub}")
                    if paths:
                        return [(p, _sc.storage_path_to_filename(p))
                                for p in paths]
                except Exception:
                    pass
            return [(p, os.path.basename(p))
                    for p in sorted(glob.glob(local_pattern))]

        _docs_emp_dir = os.path.join(PAYROLL_DIR, "employees", _docs_emp)

        # ── Paystubs ──────────────────────────────────────────
        _ps_files = _docs_list(
            "paystubs",
            os.path.join(_docs_emp_dir, "Payroll_*.pdf"))
        with st.expander(f"Paystubs ({len(_ps_files)})", expanded=bool(_ps_files)):
            if not _ps_files:
                st.caption("No paystubs generated yet.")
            else:
                _ps_by_year = {}
                for _pp, _pfn in _ps_files:
                    _pbase = _pfn.replace(".pdf", "").replace("Payroll_", "")
                    _pyr = _pbase[:4] if _pbase[:4].isdigit() else "Other"
                    _ps_by_year.setdefault(_pyr, []).append((_pp, _pfn))
                for _pyr in sorted(_ps_by_year.keys(), reverse=True):
                    st.markdown(f"**{_pyr}**")
                    for _pp, _pfn in _ps_by_year[_pyr]:
                        _pbase = _pfn.replace(".pdf", "").replace("Payroll_", "")
                        try:
                            _plabel = date.fromisoformat(
                                _pbase[:10]).strftime("%b %d, %Y")
                        except (ValueError, TypeError):
                            _plabel = _pbase.replace("_", " ")
                        _pb = _get_pdf_bytes(_pp)
                        if _pb:
                            st.download_button(
                                _plabel, data=_pb, file_name=_pfn,
                                mime="application/pdf",
                                key=f"docs_ps_{_pp}")

        # ── T4 Slips ──────────────────────────────────────────
        _t4d_files = _docs_list(
            "t4",
            os.path.join(_docs_emp_dir, "T4", "T4_*.pdf"))
        with st.expander(f"T4 Slips ({len(_t4d_files)})"):
            if not _t4d_files:
                st.caption("No T4 slips generated yet.")
            else:
                for _tp, _tfn in _t4d_files:
                    _tb = _get_pdf_bytes(_tp)
                    if _tb:
                        st.download_button(
                            _tfn.replace(".pdf", "").replace("_", " "),
                            data=_tb, file_name=_tfn,
                            mime="application/pdf",
                            key=f"docs_t4_{_tp}")

        # ── ROE ───────────────────────────────────────────────
        _roed_files = _docs_list(
            "roe",
            os.path.join(_docs_emp_dir, "ROE", "ROE_*.pdf"))
        with st.expander(f"Records of Employment ({len(_roed_files)})"):
            if not _roed_files:
                st.caption("No ROE documents generated yet.")
            else:
                for _rp, _rfn in _roed_files:
                    _rb = _get_pdf_bytes(_rp)
                    if _rb:
                        st.download_button(
                            _rfn.replace(".pdf", "").replace("_", " "),
                            data=_rb, file_name=_rfn,
                            mime="application/pdf",
                            key=f"docs_roe_{_rp}")

        # ── Agreements ────────────────────────────────────────
        _agrd_files = _docs_list(
            "agreements",
            os.path.join(_docs_emp_dir, "agreements", "*.pdf"))
        with st.expander(f"Agreements ({len(_agrd_files)})"):
            if not _agrd_files:
                st.caption("No agreement documents yet.")
            else:
                for _ap, _afn in _agrd_files:
                    _ab = _get_pdf_bytes(_ap)
                    if _ab:
                        st.download_button(
                            _afn.replace(".pdf", "").replace("_", " "),
                            data=_ab, file_name=_afn,
                            mime="application/pdf",
                            key=f"docs_agr_{_ap}")

        # ── Letters ───────────────────────────────────────────
        _ltrd_files = _docs_list(
            "letters",
            os.path.join(_docs_emp_dir, "Letters", "*.pdf"))
        with st.expander(f"Letters ({len(_ltrd_files)})"):
            if not _ltrd_files:
                st.caption("No letters generated yet.")
            else:
                for _lp, _lfn in _ltrd_files:
                    _lb = _get_pdf_bytes(_lp)
                    if _lb:
                        st.download_button(
                            _lfn.replace(".pdf", "").replace("_", " "),
                            data=_lb, file_name=_lfn,
                            mime="application/pdf",
                            key=f"docs_ltr_{_lp}")
