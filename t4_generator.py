"""
T4 Slip & T4 Summary Generator
===============================
Aggregates annual payroll data into CRA T4 box values and fills the
official CRA T4 fillable PDF form (t4-fill-25e.pdf).
Also generates the T4 Summary (T4SUM) employer filing form.

Templates:
  - assets/t4_template_2025.pdf
  - assets/t4sum_template_2025.pdf
"""

import os
from datetime import date

import fitz  # PyMuPDF

import db as _db
from tax_calculator import RATES
from employer_config import load_employer

# ── Constants ─────────────────────────────────────────────

PAYROLL_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR  = os.path.join(PAYROLL_DIR, "assets")

# Template paths used as local fallback when Storage is unavailable.
T4_TEMPLATE    = os.path.join(ASSETS_DIR, "t4_template_2025.pdf")
T4SUM_TEMPLATE = os.path.join(ASSETS_DIR, "t4sum_template_2025.pdf")


def _load_template(template_name: str) -> fitz.Document:
    """Open a CRA form template. Tries Supabase Storage first, falls back to local."""
    try:
        from storage_client import download_asset
        data = download_asset(template_name)
        return fitz.open("pdf", data)
    except Exception:
        pass
    local_path = os.path.join(ASSETS_DIR, template_name)
    return fitz.open(local_path)


# ── Aggregate T4 data from remittances ────────────────────

def aggregate_t4_data(employee, tax_year):
    """Sum all remittance entries for an employee in a given tax year.

    Returns a dict of T4 box values, or None if no data found.
    """
    total_gross          = 0.0
    total_cpp            = 0.0
    total_cpp2           = 0.0
    total_ei             = 0.0
    total_fed_tax        = 0.0
    total_prov_tax       = 0.0
    total_ei_insurable   = 0.0
    total_cpp_pensionable = 0.0
    entry_count          = 0

    for year, month in _db.available_months()[0]:
        if year != tax_year:
            continue
        for e in _db.get_remittances(year, month):
            if e.get("employee") != employee:
                continue
            entry_count += 1
            _gross = e.get("gross") or 0
            total_gross          += _gross
            total_cpp            += e.get("cpp_employee") or 0
            total_cpp2           += e.get("cpp2_employee") or 0
            total_ei             += e.get("ei_employee") or 0
            total_fed_tax        += e.get("fed_tax") or 0
            total_prov_tax       += e.get("prov_tax") or 0
            total_ei_insurable   += e.get("ei_insurable_gross") or _gross
            total_cpp_pensionable += e.get("cpp_pensionable_gross") or _gross

    if entry_count == 0:
        return None

    # Annual maximums from rate tables
    rates = RATES.get((tax_year, 1), {})
    cpp_max_pensionable = rates.get("cpp_max_pensionable", 0)
    ei_max_insurable    = rates.get("ei_max_insurable", 0)

    return {
        # Standard T4 boxes
        "box_14": round(total_gross, 2),           # Employment income
        "box_16": round(total_cpp, 2),             # Employee CPP contributions
        "box_16a": round(total_cpp2, 2),           # Employee second CPP (CPP2)
        "box_17": 0.00,                            # Employee QPP (N/A)
        "box_18": round(total_ei, 2),              # Employee EI premiums
        "box_20": 0.00,                            # RPP contributions (N/A)
        "box_22": round(total_fed_tax + total_prov_tax, 2),  # Income tax deducted
        # Box 24/26 use separately tracked insurable/pensionable amounts.
        # Old entries without these fields fall back to gross (same result).
        # If non-insurable income types are ever added (commissions, certain
        # allowances), log them with the correct ei_insurable_gross and
        # cpp_pensionable_gross values in log_payroll() — these boxes update
        # automatically.
        "box_24": round(min(total_ei_insurable, ei_max_insurable), 2),     # EI insurable earnings
        "box_26": round(min(total_cpp_pensionable, cpp_max_pensionable), 2),  # CPP pensionable earnings
        "box_44": 0.00,                            # Union dues (N/A)
        "box_46": 0.00,                            # Charitable donations (N/A)
        "box_52": 0.00,                            # Pension adjustment (N/A)
        # Extra detail (not standard boxes, but useful)
        "fed_tax":   round(total_fed_tax, 2),
        "prov_tax":  round(total_prov_tax, 2),
        "entry_count": entry_count,
        "province":  "AB",
    }


# ── Fill official CRA T4 form ────────────────────────────

def _fmt_money(val):
    """Format amount for T4 box — no $ sign, just digits with decimals."""
    if val == 0:
        return ""
    return f"{val:,.2f}"


def _split_name(full_name):
    """Split 'First Last' into (last, first, initial)."""
    parts = full_name.strip().split()
    if len(parts) >= 2:
        return parts[-1], parts[0], ""
    return full_name, "", ""


def _format_address(profile):
    """Build a single address string from profile."""
    addr = profile.get("employee_address", [])
    if not addr:
        return ""
    return ", ".join(a for a in addr if a)


def generate_t4_pdf(employee, tax_year, t4_data, profile, output_path=None):
    """Fill the official CRA T4 PDF form with employee data.

    Args:
        employee:  employee name
        tax_year:  int
        t4_data:   dict from aggregate_t4_data()
        profile:   dict with employee_address, SIN, etc.
        output_path: optional override for output file path (for previews)

    Returns:
        Storage path or absolute local path to the generated PDF.
    """
    safe_name = employee.replace(" ", "_")
    filename = f"T4_{tax_year}_{safe_name}.pdf"

    # Open template from Storage or local file.
    doc = _load_template("t4_template_2025.pdf")
    page = doc[0]

    last_name, first_name, initial = _split_name(employee)
    sin = profile.get("sin", "")
    address = _format_address(profile)

    _co = load_employer()

    # Field values to fill — applied to both Slip1 and Slip2
    slip_data = {
        "EmployersName[0].Slip1EmployersName[0]": _co["full_name"],
        "Year[0].Slip1Year[0]":                    str(tax_year),
        "EmployersAccount[0].Slip1Box54[0]":       _co["cra_bn"],
        "Box12[0].Slip1Box12[0]":                  sin,
        "Box14[0].Slip1Box14[0]":                  _fmt_money(t4_data["box_14"]),
        "Box22[0].Slip1Box22[0]":                  _fmt_money(t4_data["box_22"]),
        "Box16[0].Slip1Box16[0]":                  _fmt_money(t4_data["box_16"]),
        "Box16A[0].Slip1Box16A[0]":                _fmt_money(t4_data["box_16a"]),
        "Box17[0].Slip1Box17[0]":                  _fmt_money(t4_data["box_17"]),
        "Box24[0].Slip1Box24[0]":                  _fmt_money(t4_data["box_24"]),
        "Box26[0].Slip1Box26[0]":                  _fmt_money(t4_data["box_26"]),
        "Box18[0].Slip1Box18[0]":                  _fmt_money(t4_data["box_18"]),
        "Box44[0].Slip1Box44[0]":                  _fmt_money(t4_data["box_44"]),
        "Box20[0].Slip1Box20[0]":                  _fmt_money(t4_data["box_20"]),
        "Box46[0].Slip1Box46[0]":                  _fmt_money(t4_data["box_46"]),
        "Box52[0].Slip1Box52[0]":                  _fmt_money(t4_data["box_52"]),
        "Employee[0].LastName[0].Slip1LastName[0]":   last_name,
        "Employee[0].FirstName[0].Slip1FirstName[0]": first_name,
        "Employee[0].Initial[0].Slip1Initial[0]":     initial,
        "Employee[0].Slip1Address[0]":                address,
    }

    # Province of employment — Box 10
    # The CRA form uses a combo box; Alberta = "AB"
    province_fields = {
        "Box10[0].Slip1Box10[0]": t4_data.get("province", "AB"),
    }

    # Fill both Slip1 and Slip2 (top and bottom copies on page 1)
    for slip_prefix in ["form1[0].Page1[0].Slip1[0].",
                        "form1[0].Page1[0].Slip2[0]."]:
        for field_suffix, value in slip_data.items():
            field_name = slip_prefix + field_suffix
            widget = page.first_widget
            while widget:
                if widget.field_name == field_name:
                    widget.field_value = value
                    widget.update()
                    break
                widget = widget.next
        # Province combo box
        for field_suffix, value in province_fields.items():
            field_name = slip_prefix + field_suffix
            widget = page.first_widget
            while widget:
                if widget.field_name == field_name:
                    widget.field_value = value
                    widget.update()
                    break
                widget = widget.next

    # If explicit output_path given (e.g. preview), write there.
    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        doc.save(output_path)
        doc.close()
        return output_path

    pdf_bytes = doc.write()
    doc.close()

    # Try Supabase Storage.
    try:
        import storage_client as _sc
        emp_uuid = _db._employee_id(employee)
        if emp_uuid:
            storage_path = f"employees/{emp_uuid}/t4/{filename}"
            _sc.upload_pdf(_sc.PDF_BUCKET, storage_path, pdf_bytes)
            return storage_path
    except Exception:
        pass

    # Local fallback.
    t4_dir = os.path.join(PAYROLL_DIR, "employees", employee, "T4")
    os.makedirs(t4_dir, exist_ok=True)
    out_path = os.path.join(t4_dir, filename)
    with open(out_path, "wb") as _f:
        _f.write(pdf_bytes)
    return out_path


# ── Aggregate T4 Summary data (all employees) ──────────────

def aggregate_t4sum_data(tax_year):
    """Sum all remittance entries across ALL employees for a tax year.

    Returns a dict with T4 Summary box values, or None if no data.
    """
    total_gross       = 0.0
    total_cpp_ee      = 0.0
    total_cpp2_ee     = 0.0
    total_cpp_er      = 0.0
    total_cpp2_er     = 0.0
    total_ei_ee       = 0.0
    total_ei_er       = 0.0
    total_fed_tax     = 0.0
    total_prov_tax    = 0.0
    total_remitted    = 0.0
    employees_seen    = set()
    entry_count       = 0

    for year, month in _db.available_months()[0]:
        if year != tax_year:
            continue
        for e in _db.get_remittances(year, month):
            entry_count += 1
            employees_seen.add(e.get("employee", ""))
            total_gross    += e.get("gross") or 0
            total_cpp_ee   += e.get("cpp_employee") or 0
            total_cpp2_ee  += e.get("cpp2_employee") or 0
            total_cpp_er   += e.get("cpp_employer") or 0
            total_cpp2_er  += e.get("cpp2_employer") or 0
            total_ei_ee    += e.get("ei_employee") or 0
            total_ei_er    += e.get("ei_employer") or 0
            total_fed_tax  += e.get("fed_tax") or 0
            total_prov_tax += e.get("prov_tax") or 0
            total_remitted += e.get("total_remittance") or 0

    if entry_count == 0:
        return None

    # T4SUM box mapping
    # Box 14: total employment income (all T4 slips)
    # Box 16: total employee CPP contributions
    # Box 16A: total employee CPP2 contributions
    # Box 18: total employee EI premiums
    # Box 22: total income tax deducted
    # Box 27: total employer CPP contributions
    # Box 27A: total employer CPP2 contributions
    # Box 19: total employer EI contributions
    # Box 80: total deductions reported (all T4 slips)
    #         = employee CPP+CPP2+EI + tax + employer CPP+CPP2+EI
    # Box 82: total remittances paid to CRA during the year
    # Difference: Box 82 - Box 80 (overpayment or balance owing)
    # Box 88: number of T4 slips filed

    box_16  = round(total_cpp_ee, 2)
    box_16a = round(total_cpp2_ee, 2)
    box_18  = round(total_ei_ee, 2)
    box_22  = round(total_fed_tax + total_prov_tax, 2)
    box_27  = round(total_cpp_er, 2)
    box_27a = round(total_cpp2_er, 2)
    box_19  = round(total_ei_er, 2)
    box_80  = round(box_16 + box_16a + box_18 + box_22 +
                    box_27 + box_27a + box_19, 2)

    return {
        "box_88":  len(employees_seen),       # Number of T4 slips
        "box_14":  round(total_gross, 2),      # Employment income
        "box_16":  box_16,                     # Employee CPP
        "box_16a": box_16a,                    # Employee CPP2
        "box_18":  box_18,                     # Employee EI
        "box_22":  box_22,                     # Income tax deducted
        "box_27":  box_27,                     # Employer CPP
        "box_27a": box_27a,                    # Employer CPP2
        "box_19":  box_19,                     # Employer EI
        "box_20":  0.00,                       # RPP contributions (N/A)
        "box_52":  0.00,                       # Pension adjustment (N/A)
        "box_80":  box_80,                     # Total deductions reported
        "box_82":  round(total_remitted, 2),   # Remittances paid
        "difference": round(total_remitted - box_80, 2),
        "entry_count": entry_count,
        "slip_count":  len(employees_seen),
    }


# ── Fill official CRA T4 Summary form ──────────────────────

def generate_t4sum_pdf(tax_year, t4sum_data, output_path=None):
    """Fill the official CRA T4 Summary PDF form.

    Args:
        tax_year:    int
        t4sum_data:  dict from aggregate_t4sum_data()
        output_path: optional override path for the PDF (used for previews)

    Returns:
        Storage path or absolute local path to the generated PDF.
    """
    filename = f"T4_Summary_{tax_year}.pdf"

    doc = _load_template("t4sum_template_2025.pdf")
    page = doc[0]

    # Field prefix for page 1
    pfx = "form1[0].Page1[0].Border[0]."

    _co = load_employer()

    # Map T4SUM form fields to values
    fields = {
        # Employer info
        pfx + "EmployerInfo[0].EmployerAccount[0]": _co["cra_bn"],
        pfx + "EmployerInfo[0].EmployerName[0]":    _co["full_name"],

        # Left column
        pfx + "LeftFields[0].Line88[0].Box88[0]":  str(t4sum_data["box_88"]),
        pfx + "LeftFields[0].Line14[0].Box14[0]":  _fmt_money(t4sum_data["box_14"]),
        pfx + "LeftFields[0].Line20[0].Box20[0]":  _fmt_money(t4sum_data["box_20"]),
        pfx + "LeftFields[0].Line52[0].Box52[0]":  _fmt_money(t4sum_data["box_52"]),

        # Middle column
        pfx + "MiddleFields[0].Line16[0].Box16[0]":   _fmt_money(t4sum_data["box_16"]),
        pfx + "MiddleFields[0].Line16A[0].Box16A[0]": _fmt_money(t4sum_data["box_16a"]),
        pfx + "MiddleFields[0].Line27[0].Box27[0]":   _fmt_money(t4sum_data["box_27"]),
        pfx + "MiddleFields[0].Line27A[0].Box27A[0]": _fmt_money(t4sum_data["box_27a"]),
        pfx + "MiddleFields[0].Line18[0].Box18[0]":   _fmt_money(t4sum_data["box_18"]),
        pfx + "MiddleFields[0].Line19[0].Box19[0]":   _fmt_money(t4sum_data["box_19"]),
        pfx + "MiddleFields[0].Line22[0].Box22[0]":   _fmt_money(t4sum_data["box_22"]),
        pfx + "MiddleFields[0].Line80[0].Box80[0]":   _fmt_money(t4sum_data["box_80"]),
        pfx + "MiddleFields[0].Line82[0].Box82[0]":   _fmt_money(t4sum_data["box_82"]),
        pfx + "MiddleFields[0].Difference[0].Difference[0]": _fmt_money(
            abs(t4sum_data["difference"])),

        # Date — form has "20" pre-printed; field takes last 2 digits only
        "form1[0].Page1[0].Date[0]": str(tax_year)[-2:],
    }

    # Balance owing (Box 86) vs overpayment (Box 84)
    diff = t4sum_data["difference"]
    if diff < 0:
        # Balance owing (remitted less than reported)
        fields[pfx + "MiddleFields[0].Line86[0].Box86[0]"] = _fmt_money(abs(diff))
        fields[pfx + "MiddleFields[0].AmountEnclosed[0].AmountEnclosed[0]"] = _fmt_money(abs(diff))
    elif diff > 0:
        # Overpayment
        fields[pfx + "MiddleFields[0].Line84[0].Box84[0]"] = _fmt_money(diff)

    # SIN of proprietor / contact info
    fields[pfx + "Line76[0].Box76[0]"] = ""         # Contact phone
    fields[pfx + "Box78[0].Box78[0]"] = ""           # Contact name

    for field_name, value in fields.items():
        widget = page.first_widget
        while widget:
            if widget.field_name == field_name:
                widget.field_value = value
                widget.update()
                break
            widget = widget.next

    # If explicit output_path given (e.g. preview), write there.
    if output_path:
        _dir = os.path.dirname(output_path)
        if _dir:
            os.makedirs(_dir, exist_ok=True)
        doc.save(output_path)
        doc.close()
        return output_path

    pdf_bytes = doc.write()
    doc.close()

    # Try Supabase Storage.
    try:
        import storage_client as _sc
        storage_path = f"t4-summaries/{filename}"
        _sc.upload_pdf(_sc.PDF_BUCKET, storage_path, pdf_bytes)
        return storage_path
    except Exception:
        pass

    # Local fallback.
    out_dir = os.path.join(PAYROLL_DIR, "T4_Summaries")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "wb") as _f:
        _f.write(pdf_bytes)
    return out_path
