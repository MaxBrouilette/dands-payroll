"""
Supabase Database Client
========================
CRUD wrappers for all 12 tables. Replaces all local JSON file I/O.

Credentials are read from (in order):
  1. Streamlit secrets  (st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
  2. Environment variables (SUPABASE_URL, SUPABASE_KEY)

If neither is available, functions raise RuntimeError with a clear message.
"""

import os
from datetime import datetime, timedelta
import streamlit as st


@st.cache_resource
def _client():
    """Return a cached Supabase client (shared across reruns)."""
    url = key = None
    try:
        url = st.secrets.get("SUPABASE_URL")
        key = st.secrets.get("SUPABASE_KEY")
    except Exception:
        pass
    if not url:
        url = os.environ.get("SUPABASE_URL")
    if not key:
        key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "Supabase credentials not configured.\n"
            "Add SUPABASE_URL and SUPABASE_KEY to .streamlit/secrets.toml "
            "or set them as environment variables."
        )
    from supabase import create_client
    return create_client(url, key)


def _bust_employee_cache():
    """Call after any employee write to invalidate read caches."""
    list_employees.clear()
    list_all_employees.clear()
    get_employee.clear()


def _bust_remittance_cache():
    """Call after any remittance write to invalidate read caches."""
    available_months.clear()
    get_remittances.clear()


# ── Employee name → UUID cache (in-process, per session) ─────────────
_emp_id_cache: dict[str, str] = {}


def _employee_id(name: str) -> str | None:
    """Look up employee UUID by display name. Cached in-process."""
    if name in _emp_id_cache:
        return _emp_id_cache[name]
    result = (_client().table("employees")
              .select("id")
              .eq("employee_name", name)
              .maybe_single()
              .execute())
    if result.data:
        _emp_id_cache[name] = result.data["id"]
        return result.data["id"]
    return None


def _parse_display_date(date_str):
    """Parse 'February 1st, 2026' to a date object for ISO column storage."""
    from utils import parse_payroll_date
    return parse_payroll_date(date_str)


# ════════════════════════════════════════════════════════════
#  EMPLOYEES
# ════════════════════════════════════════════════════════════

@st.cache_data(ttl=300)
def list_employees() -> list[str]:
    """Return sorted list of all active employee names."""
    result = (_client().table("employees")
              .select("employee_name")
              .eq("is_archived", False)
              .execute())
    return sorted(r["employee_name"] for r in result.data)


@st.cache_data(ttl=300)
def list_all_employees() -> list[str]:
    """Return sorted list of all employees including archived."""
    result = _client().table("employees").select("employee_name").execute()
    return sorted(r["employee_name"] for r in result.data)


@st.cache_data(ttl=300)
def get_employee(name: str) -> dict | None:
    """Get full employee record by name, including nested data. Returns None if not found."""
    result = (_client().table("employees")
              .select("*")
              .eq("employee_name", name)
              .maybe_single()
              .execute())
    if not result.data:
        return None
    emp = dict(result.data)

    # Attach wage_history
    wh = (_client().table("wage_history")
          .select("*")
          .eq("employee_id", emp["id"])
          .order("effective_date")
          .execute())
    emp["wage_history"] = [
        {"rate": r["rate"], "effective_date": r["effective_date"], "note": r.get("note", "")}
        for r in wh.data
    ]

    # Attach vacation_payouts (active only)
    vp = (_client().table("vacation_payouts")
          .select("*")
          .eq("employee_id", emp["id"])
          .is_("deleted_at", "null")
          .order("date")
          .execute())
    emp["vacation_payouts"] = [
        {"id": r["id"], "date": r["date"], "amount": r["amount"],
         "year_ending": r["year_ending"], "method": r["method"], "note": r.get("note", "")}
        for r in vp.data
    ]

    # Attach vacation_gross_overrides as {year_key: amount}
    vo = (_client().table("vacation_gross_overrides")
          .select("*")
          .eq("employee_id", emp["id"])
          .execute())
    emp["vacation_gross_overrides"] = {r["year_key"]: r["gross_amount"] for r in vo.data}

    return emp


def save_employee(data: dict) -> str:
    """Insert or update an employee record. Returns the UUID."""
    emp_id = data.get("id") or _employee_id(data["employee_name"])

    fields = {
        "employee_name":          data["employee_name"],
        "employee_id":            data.get("employee_id", ""),
        "position":               data.get("position", ""),
        "payment_method":         data.get("payment_method", ""),
        "schedule_type":          data.get("schedule_type", "standard"),
        "regular_rate":           data.get("regular_rate"),
        "sin":                    data.get("sin", ""),
        "dob":                    data.get("dob") or None,
        "phone":                  data.get("phone", ""),
        "email":                  data.get("email", ""),
        "employment_type":        data.get("employment_type", "Full-Time"),
        "province":               data.get("province", "Alberta"),
        "start_date":             data.get("start_date") or None,
        "end_date":               data.get("end_date") or None,
        "emergency_contact_name":  data.get("emergency_contact_name", ""),
        "emergency_contact_phone": data.get("emergency_contact_phone", ""),
        "bank_institution":        data.get("bank_institution", ""),
        "bank_transit":            data.get("bank_transit", ""),
        "bank_account":            data.get("bank_account", ""),
        "td1_fed_claim":           data.get("td1_fed_claim", 0),
        "td1_prov_claim":          data.get("td1_prov_claim", 0),
        "td1_extra_deduction":     data.get("td1_extra_deduction", 0),
        "td1_exempt":              data.get("td1_exempt", False),
        "employee_address":        data.get("employee_address", []),
        "part_time":               data.get("part_time"),
        "is_archived":             data.get("is_archived", False),
        "updated_at":              datetime.now().isoformat(),
    }

    if emp_id:
        _client().table("employees").update(fields).eq("id", emp_id).execute()
    else:
        result = _client().table("employees").insert(fields).execute()
        emp_id = result.data[0]["id"]
        _emp_id_cache[data["employee_name"]] = emp_id

    # Replace wage_history entirely
    wage_history = data.get("wage_history", [])
    if wage_history is not None:
        _client().table("wage_history").delete().eq("employee_id", emp_id).execute()
        if wage_history:
            _client().table("wage_history").insert([
                {"employee_id": emp_id,
                 "rate": w["rate"],
                 "effective_date": w["effective_date"],
                 "note": w.get("note", "")}
                for w in wage_history
            ]).execute()

    # Upsert vacation_gross_overrides
    for year_key, amount in data.get("vacation_gross_overrides", {}).items():
        (_client().table("vacation_gross_overrides")
         .upsert({"employee_id": emp_id, "year_key": year_key,
                  "gross_amount": round(float(amount), 2)},
                 on_conflict="employee_id,year_key")
         .execute())

    _bust_employee_cache()
    return emp_id


# ════════════════════════════════════════════════════════════
#  REMITTANCES
# ════════════════════════════════════════════════════════════

def _normalize_remittance(r: dict) -> dict:
    """Map DB columns → legacy key names expected throughout the app."""
    return {
        "id":                  r.get("id"),
        "payment_date":        r.get("payment_date"),
        "employee":            r.get("employee_name"),
        "period":              r.get("period_label", ""),
        "gross":               r.get("gross"),
        "cpp_employee":        r.get("cpp_employee"),
        "cpp_employer":        r.get("cpp_employer"),
        "cpp2_employee":       r.get("cpp2_employee", 0),
        "cpp2_employer":       r.get("cpp2_employer", 0),
        "ei_employee":         r.get("ei_employee"),
        "ei_employer":         r.get("ei_employer"),
        "fed_tax":             r.get("fed_tax"),
        "prov_tax":            r.get("prov_tax"),
        "total_remittance":    r.get("total_remittance"),
        "pdf_path":            r.get("pdf_storage_path"),
        "hours":               r.get("hours"),
        "ei_insurable_gross":  r.get("ei_insurable_gross"),
        "cpp_pensionable_gross": r.get("cpp_pensionable_gross"),
        "pay_year":            r.get("pay_year"),
        "pay_month":           r.get("pay_month"),
    }


def log_remittance(entry: dict) -> dict | None:
    """Insert a remittance entry. Returns the inserted row (normalized)."""
    emp_id = _employee_id(entry["employee"])
    pay_dt = _parse_display_date(entry["payment_date"])
    row = {
        "employee_id":           emp_id,
        "employee_name":         entry["employee"],
        "payment_date":          entry["payment_date"],
        "payment_date_iso":      pay_dt.isoformat() if pay_dt else None,
        "pay_year":              entry.get("pay_year") or (pay_dt.year if pay_dt else None),
        "pay_month":             entry.get("pay_month") or (pay_dt.month if pay_dt else None),
        "period_label":          entry.get("period", ""),
        "gross":                 entry.get("gross"),
        "cpp_employee":          entry.get("cpp_employee"),
        "cpp_employer":          entry.get("cpp_employer"),
        "cpp2_employee":         entry.get("cpp2_employee", 0),
        "cpp2_employer":         entry.get("cpp2_employer", 0),
        "ei_employee":           entry.get("ei_employee"),
        "ei_employer":           entry.get("ei_employer"),
        "fed_tax":               entry.get("fed_tax"),
        "prov_tax":              entry.get("prov_tax"),
        "total_remittance":      entry.get("total_remittance"),
        "hours":                 entry.get("hours"),
        "ei_insurable_gross":    entry.get("ei_insurable_gross"),
        "cpp_pensionable_gross": entry.get("cpp_pensionable_gross"),
        "pdf_storage_path":      entry.get("pdf_path"),
    }
    result = _client().table("remittances").insert(row).execute()
    _bust_remittance_cache()
    return _normalize_remittance(result.data[0]) if result.data else None


@st.cache_data(ttl=300)
def get_remittances(year: int, month: int) -> list:
    """Return all active remittance entries for a specific month."""
    result = (_client().table("remittances")
              .select("*")
              .eq("pay_year", year)
              .eq("pay_month", month)
              .is_("deleted_at", "null")
              .order("payment_date_iso")
              .execute())
    return [_normalize_remittance(r) for r in result.data]


def get_all_remittances_for_employee(employee_name: str) -> list:
    """Return all active remittances for an employee (used by vacation calc)."""
    result = (_client().table("remittances")
              .select("*")
              .eq("employee_name", employee_name)
              .is_("deleted_at", "null")
              .order("payment_date_iso")
              .execute())
    return [_normalize_remittance(r) for r in result.data]


def find_remittance(employee: str, payment_date_str: str, year: int, month: int) -> dict | None:
    """Find a single remittance entry by employee + payment_date + month."""
    result = (_client().table("remittances")
              .select("*")
              .eq("employee_name", employee)
              .eq("payment_date", payment_date_str)
              .eq("pay_year", year)
              .eq("pay_month", month)
              .is_("deleted_at", "null")
              .maybe_single()
              .execute())
    return _normalize_remittance(result.data) if result.data else None


def update_remittance_pdf_path(remittance_id: str, path: str):
    """Update the pdf_storage_path for a remittance entry."""
    (_client().table("remittances")
     .update({"pdf_storage_path": path})
     .eq("id", remittance_id)
     .execute())


def soft_delete_remittance(remittance_id: str):
    """Soft-delete a remittance row (sets deleted_at timestamp)."""
    (_client().table("remittances")
     .update({"deleted_at": datetime.now().isoformat()})
     .eq("id", remittance_id)
     .execute())
    _bust_remittance_cache()


@st.cache_data(ttl=300)
def available_months() -> tuple[list, list]:
    """Return ([(year, month), ...], []) of months with remittance data."""
    result = (_client().table("remittances")
              .select("pay_year,pay_month")
              .is_("deleted_at", "null")
              .execute())
    seen: set[tuple] = set()
    months = []
    for r in result.data:
        if r["pay_year"] and r["pay_month"]:
            k = (int(r["pay_year"]), int(r["pay_month"]))
            if k not in seen:
                seen.add(k)
                months.append(k)
    months.sort()
    return months, []  # second item kept for API compatibility


# ════════════════════════════════════════════════════════════
#  REMITTANCE STATUS
# ════════════════════════════════════════════════════════════

def get_remittance_status(year: int, month: int) -> dict:
    result = (_client().table("remittance_status")
              .select("*")
              .eq("pay_year", year)
              .eq("pay_month", month)
              .maybe_single()
              .execute())
    if result.data:
        return {
            "remitted":       result.data.get("remitted", False),
            "remitted_date":  result.data.get("remitted_date", "") or "",
            "confirmation":   result.data.get("confirmation", ""),
            "notes":          result.data.get("notes", ""),
        }
    return {"remitted": False, "remitted_date": "", "confirmation": "", "notes": ""}


def set_remittance_status(year: int, month: int, data: dict):
    (_client().table("remittance_status")
     .upsert({
         "pay_year":      year,
         "pay_month":     month,
         "remitted":      data.get("remitted", False),
         "remitted_date": data.get("remitted_date") or None,
         "confirmation":  data.get("confirmation", ""),
         "notes":         data.get("notes", ""),
         "updated_at":    datetime.now().isoformat(),
     }, on_conflict="pay_year,pay_month")
     .execute())


def clear_remittance_status(year: int, month: int):
    set_remittance_status(year, month, {"remitted": False})


# ════════════════════════════════════════════════════════════
#  MANUAL AUDIT ENTRIES
# ════════════════════════════════════════════════════════════

def _normalize_audit(r: dict) -> dict:
    return {
        "id":           r.get("id"),
        "employee":     r.get("employee_name"),
        "payment_date": r.get("payment_date"),
        "pay_year":     r.get("pay_year"),
        "pay_month":    r.get("pay_month"),
        "period_from":  r.get("period_from", ""),
        "period_to":    r.get("period_to", ""),
        "hours":        r.get("hours"),
        "gross":        r.get("gross"),
        "actual": {
            "cpp":      r.get("actual_cpp"),
            "cpp2":     r.get("actual_cpp2", 0),
            "ei":       r.get("actual_ei"),
            "fed_tax":  r.get("actual_fed_tax"),
            "prov_tax": r.get("actual_prov_tax"),
        },
        "formula": {
            "cpp":      r.get("formula_cpp"),
            "cpp2":     r.get("formula_cpp2", 0),
            "ei":       r.get("formula_ei"),
            "fed_tax":  r.get("formula_fed_tax"),
            "prov_tax": r.get("formula_prov_tax"),
        },
        "variance": {
            "cpp":      r.get("variance_cpp"),
            "cpp2":     r.get("variance_cpp2", 0),
            "ei":       r.get("variance_ei"),
            "fed_tax":  r.get("variance_fed_tax"),
            "prov_tax": r.get("variance_prov_tax"),
        },
        "source": "manual",
    }


def log_audit_entry(entry: dict) -> dict:
    """Insert a manual audit entry."""
    emp_id = _employee_id(entry["employee"])
    pay_dt = _parse_display_date(entry["payment_date"])
    result = _client().table("audit_manual").insert({
        "employee_id":     emp_id,
        "employee_name":   entry["employee"],
        "payment_date":    entry["payment_date"],
        "payment_date_iso": pay_dt.isoformat() if pay_dt else None,
        "pay_year":        entry.get("pay_year"),
        "pay_month":       entry.get("pay_month"),
        "period_from":     entry.get("period_from", ""),
        "period_to":       entry.get("period_to", ""),
        "hours":           entry.get("hours"),
        "gross":           entry.get("gross"),
        "actual_cpp":      entry.get("actual", {}).get("cpp"),
        "actual_cpp2":     entry.get("actual", {}).get("cpp2", 0),
        "actual_ei":       entry.get("actual", {}).get("ei"),
        "actual_fed_tax":  entry.get("actual", {}).get("fed_tax"),
        "actual_prov_tax": entry.get("actual", {}).get("prov_tax"),
        "formula_cpp":     entry.get("formula", {}).get("cpp"),
        "formula_cpp2":    entry.get("formula", {}).get("cpp2", 0),
        "formula_ei":      entry.get("formula", {}).get("ei"),
        "formula_fed_tax": entry.get("formula", {}).get("fed_tax"),
        "formula_prov_tax":entry.get("formula", {}).get("prov_tax"),
        "variance_cpp":    entry.get("variance", {}).get("cpp"),
        "variance_cpp2":   entry.get("variance", {}).get("cpp2", 0),
        "variance_ei":     entry.get("variance", {}).get("ei"),
        "variance_fed_tax":entry.get("variance", {}).get("fed_tax"),
        "variance_prov_tax":entry.get("variance", {}).get("prov_tax"),
    }).execute()
    return _normalize_audit(result.data[0]) if result.data else entry


def get_manual_audit_entries(employee: str | None = None) -> list:
    q = (_client().table("audit_manual")
         .select("*")
         .is_("deleted_at", "null"))
    if employee:
        q = q.eq("employee_name", employee)
    result = q.order("pay_year").order("pay_month").execute()
    return [_normalize_audit(r) for r in result.data]


def get_manual_entries_indexed(year: int, employee: str | None = None) -> list:
    """Return [(index, entry), ...] for a year — index used by delete UI."""
    q = (_client().table("audit_manual")
         .select("*")
         .is_("deleted_at", "null")
         .eq("pay_year", year))
    if employee:
        q = q.eq("employee_name", employee)
    result = q.order("pay_month").order("employee_name").execute()
    return [(i, _normalize_audit(r)) for i, r in enumerate(result.data)]


def delete_audit_entry(year: int, index: int) -> bool:
    """Soft-delete a manual audit entry by positional index within the year."""
    entries_raw = (_client().table("audit_manual")
                   .select("*")
                   .is_("deleted_at", "null")
                   .eq("pay_year", year)
                   .order("pay_month")
                   .order("employee_name")
                   .execute()).data
    if not (0 <= index < len(entries_raw)):
        return False
    row = entries_raw[index]
    (_client().table("audit_manual")
     .update({"deleted_at": datetime.now().isoformat()})
     .eq("id", row["id"])
     .execute())
    soft_delete("audit_manual", row["employee_name"],
                {"year": year, "pay_month": row.get("pay_month"),
                 "payment_date": row.get("payment_date")},
                _normalize_audit(row))
    return True


# ════════════════════════════════════════════════════════════
#  VACATION PAYOUTS & OVERRIDES
# ════════════════════════════════════════════════════════════

def get_vacation_payouts(employee_name: str) -> list:
    emp_id = _employee_id(employee_name)
    if not emp_id:
        return []
    result = (_client().table("vacation_payouts")
              .select("*")
              .eq("employee_id", emp_id)
              .is_("deleted_at", "null")
              .order("date")
              .execute())
    return [{"id": r["id"], "date": r["date"], "amount": r["amount"],
             "year_ending": r["year_ending"], "method": r["method"],
             "note": r.get("note", "")}
            for r in result.data]


def add_vacation_payout(employee_name: str, payout: dict) -> str | None:
    emp_id = _employee_id(employee_name)
    result = (_client().table("vacation_payouts")
              .insert({"employee_id": emp_id,
                       "date":         payout.get("date"),
                       "amount":       payout.get("amount"),
                       "year_ending":  payout.get("year_ending"),
                       "method":       payout.get("method", "Cheque"),
                       "note":         payout.get("note", "")})
              .execute())
    return result.data[0]["id"] if result.data else None


def delete_vacation_payout(employee_name: str, index: int) -> dict | None:
    """Soft-delete a vacation payout by positional index. Returns removed entry or None."""
    payouts = get_vacation_payouts(employee_name)
    if not (0 <= index < len(payouts)):
        return None
    removed = payouts[index]
    (_client().table("vacation_payouts")
     .update({"deleted_at": datetime.now().isoformat()})
     .eq("id", removed["id"])
     .execute())
    soft_delete("vacation_payouts", employee_name,
                {"amount": removed.get("amount", 0),
                 "date":   removed.get("date", ""),
                 "note":   removed.get("note", "")},
                removed)
    return removed


def get_vacation_gross_overrides(employee_name: str) -> dict:
    emp_id = _employee_id(employee_name)
    if not emp_id:
        return {}
    result = (_client().table("vacation_gross_overrides")
              .select("*")
              .eq("employee_id", emp_id)
              .execute())
    return {r["year_key"]: r["gross_amount"] for r in result.data}


def set_vacation_gross_override(employee_name: str, year_key: str, amount: float):
    emp_id = _employee_id(employee_name)
    (_client().table("vacation_gross_overrides")
     .upsert({"employee_id": emp_id, "year_key": year_key,
               "gross_amount": round(float(amount), 2)},
             on_conflict="employee_id,year_key")
     .execute())


# ════════════════════════════════════════════════════════════
#  RESOLUTIONS
# ════════════════════════════════════════════════════════════

def _normalize_resolution(r: dict) -> dict:
    return {
        "id":                r.get("id"),
        "employee":          r.get("employee_name"),
        "payment_date":      r.get("payment_date"),
        "pay_year":          r.get("pay_year"),
        "pay_month":         r.get("pay_month"),
        "variance_amount":   r.get("variance_amount"),
        "resolution_method": r.get("resolution_method"),
        "resolution_amount": r.get("resolution_amount"),
        "resolution_date":   r.get("resolution_date"),
        "reference_number":  r.get("reference_number", ""),
        "receipt_file":      r.get("receipt_storage_path", ""),
        "notes":             r.get("notes", ""),
        "resolved_at":       r.get("resolved_at"),
    }


def log_resolution(entry: dict) -> dict:
    emp_id = _employee_id(entry["employee"])
    result = _client().table("resolutions").insert({
        "employee_id":        emp_id,
        "employee_name":      entry["employee"],
        "payment_date":       entry["payment_date"],
        "pay_year":           entry.get("pay_year"),
        "pay_month":          entry.get("pay_month"),
        "variance_amount":    entry.get("variance_amount"),
        "resolution_method":  entry.get("resolution_method"),
        "resolution_amount":  entry.get("resolution_amount"),
        "resolution_date":    entry.get("resolution_date"),
        "reference_number":   entry.get("reference_number", ""),
        "receipt_storage_path": entry.get("receipt_file", ""),
        "notes":              entry.get("notes", ""),
        "resolved_at":        entry.get("resolved_at"),
    }).execute()
    return _normalize_resolution(result.data[0]) if result.data else entry


def get_resolutions(year: int, employee: str | None = None) -> list:
    q = (_client().table("resolutions")
         .select("*")
         .eq("pay_year", year)
         .is_("deleted_at", "null"))
    if employee:
        q = q.eq("employee_name", employee)
    result = q.order("pay_month").order("employee_name").execute()
    return [_normalize_resolution(r) for r in result.data]


def get_resolution_for_period(employee: str, payment_date_str: str) -> dict | None:
    result = (_client().table("resolutions")
              .select("*")
              .eq("employee_name", employee)
              .eq("payment_date", payment_date_str)
              .is_("deleted_at", "null")
              .maybe_single()
              .execute())
    return _normalize_resolution(result.data) if result.data else None


def get_all_resolution_years() -> list[int]:
    result = (_client().table("resolutions")
              .select("pay_year")
              .is_("deleted_at", "null")
              .execute())
    return sorted(set(r["pay_year"] for r in result.data if r["pay_year"]))


def delete_resolution(year: int, index: int) -> bool:
    """Soft-delete a resolution by positional index within the year."""
    entries_raw = (_client().table("resolutions")
                   .select("*")
                   .eq("pay_year", year)
                   .is_("deleted_at", "null")
                   .order("pay_month")
                   .order("employee_name")
                   .execute()).data
    if not (0 <= index < len(entries_raw)):
        return False
    row = entries_raw[index]
    (_client().table("resolutions")
     .update({"deleted_at": datetime.now().isoformat()})
     .eq("id", row["id"])
     .execute())
    soft_delete("resolutions", row["employee_name"],
                {"year": year, "payment_date": row.get("payment_date")},
                _normalize_resolution(row))
    return True


# ════════════════════════════════════════════════════════════
#  AGREEMENTS
# ════════════════════════════════════════════════════════════

def get_agreements(employee_name: str) -> dict:
    """Return {agreement_key: {signed_pdf, signed_date}} for an employee."""
    emp_id = _employee_id(employee_name)
    if not emp_id:
        return {}
    result = (_client().table("agreements")
              .select("*")
              .eq("employee_id", emp_id)
              .execute())
    return {r["agreement_key"]: {"signed_pdf":  r.get("signed_pdf_path"),
                                  "signed_date": r.get("signed_date")}
            for r in result.data}


def set_agreement(employee_name: str, agreement_key: str,
                  signed_pdf: str, signed_date=None):
    """Upsert an agreement record."""
    emp_id = _employee_id(employee_name)
    (_client().table("agreements")
     .upsert({"employee_id":    emp_id,
               "agreement_key": agreement_key,
               "signed_pdf_path": signed_pdf,
               "signed_date":    signed_date,
               "updated_at":     datetime.now().isoformat()},
             on_conflict="employee_id,agreement_key")
     .execute())


# ════════════════════════════════════════════════════════════
#  ROE HISTORY
# ════════════════════════════════════════════════════════════

def get_roe_history(employee_name: str) -> list:
    emp_id = _employee_id(employee_name)
    if not emp_id:
        return []
    result = (_client().table("roe_history")
              .select("*")
              .eq("employee_id", emp_id)
              .order("generation_date", desc=True)
              .execute())
    return [{"date":           r.get("generation_date"),
             "reason_code":    r.get("reason_code"),
             "reason":         r.get("reason"),
             "last_day_paid":  r.get("last_day_paid"),
             "total_hours":    r.get("total_hours"),
             "total_earnings": r.get("total_earnings"),
             "vacation_pay":   r.get("vacation_pay"),
             "pdf_path":       r.get("pdf_storage_path")}
            for r in result.data]


def save_roe(employee_name: str, data: dict):
    emp_id = _employee_id(employee_name)
    (_client().table("roe_history")
     .insert({"employee_id":      emp_id,
               "generation_date": data.get("date"),
               "reason_code":     data.get("reason_code"),
               "reason":          data.get("reason"),
               "last_day_paid":   data.get("last_day_paid"),
               "total_hours":     data.get("total_hours"),
               "total_earnings":  data.get("total_earnings"),
               "vacation_pay":    data.get("vacation_pay"),
               "pdf_storage_path": data.get("pdf_path")})
     .execute())


# ════════════════════════════════════════════════════════════
#  TRASH (SOFT-DELETE RECOVERY BIN)
# ════════════════════════════════════════════════════════════

ITEM_TYPES = ("remittances", "audit_manual", "vacation_payouts", "resolutions")

RETENTION_PRESETS = {
    "24 hours": 1,
    "3 days":   3,
    "1 week":   7,
    "2 weeks":  14,
    "1 month":  30,
}
DEFAULT_RETENTION_DAYS = 7


def soft_delete(item_type: str, employee: str, context: dict, original_entry: dict):
    """Add a record to the trash table."""
    if item_type not in ITEM_TYPES:
        raise ValueError(f"Unknown item_type '{item_type}'. Must be one of {ITEM_TYPES}")
    (_client().table("trash")
     .insert({"deleted_at":     datetime.now().isoformat(),
               "item_type":     item_type,
               "employee_name": employee,
               "context":       context,
               "original_entry": original_entry})
     .execute())


def get_trash(item_type: str | None = None, employee: str | None = None) -> list:
    """Return trash entries, optionally filtered, sorted newest-first."""
    q = _client().table("trash").select("*")
    if item_type:
        q = q.eq("item_type", item_type)
    if employee:
        q = q.eq("employee_name", employee)
    result = q.order("deleted_at", desc=True).execute()
    return [{**r, "_trash_type": r["item_type"], "_trash_id": r["id"]}
            for r in result.data]


def restore_from_trash(trash_id: str) -> tuple[dict | None, dict | None]:
    """Remove a trash entry and return (original_entry, full_trash_record)."""
    result = (_client().table("trash")
              .select("*")
              .eq("id", trash_id)
              .maybe_single()
              .execute())
    if not result.data:
        return None, None
    row = result.data
    _client().table("trash").delete().eq("id", trash_id).execute()
    return row["original_entry"], row


def purge_expired(retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """Delete trash entries older than retention_days. Returns {purged: N}."""
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    result = _client().table("trash").delete().lt("deleted_at", cutoff).execute()
    return {"purged": len(result.data) if result.data else 0}


def trash_summary() -> dict:
    """Return count of trash items by type."""
    result = _client().table("trash").select("item_type").execute()
    counts = {t: 0 for t in ITEM_TYPES}
    for r in result.data:
        if r["item_type"] in counts:
            counts[r["item_type"]] += 1
    return counts


def get_retention_days(employer_config: dict) -> int:
    return int(employer_config.get("trash_retention_days", DEFAULT_RETENTION_DAYS))


# ════════════════════════════════════════════════════════════
#  EMPLOYER CONFIG  (single-row table)
# ════════════════════════════════════════════════════════════

def load_employer() -> dict:
    """Load employer config from the DB. Returns {} if not yet seeded."""
    result = (_client().table("employer_config")
              .select("config")
              .eq("id", 1)
              .maybe_single()
              .execute())
    return result.data["config"] if result.data else {}


def save_employer(data: dict):
    """Upsert employer config (single row, id=1)."""
    (_client().table("employer_config")
     .upsert({"id": 1, "config": data,
               "updated_at": datetime.now().isoformat()},
             on_conflict="id")
     .execute())
