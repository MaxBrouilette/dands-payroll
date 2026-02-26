"""
================================================================
  RECORD OF EMPLOYMENT (ROE) GENERATOR
================================================================

Assembles ROE data from payroll records and generates a branded
employer-retained reference document for submission via ROE Web.

ROE Blocks:
  1-9:   Administrative (employer/employee info)
  10-12: Employment period
  13-14: Occupation, recall
  15A-C: Insurable hours & earnings
  16:    Reason code
  17A-C: Separation payments (vacation, stat, other)
  18-22: Comments, leave details, contact, certification

Reference: Service Canada — "How to Complete the ROE Form"
================================================================
"""

import os
import json
import re
import calendar
from datetime import date, timedelta
from io import BytesIO

import fitz  # PyMuPDF
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable,
)

import db as _db
from utils import parse_payroll_date
from vacation_tracker import get_vacation_accrual
from pay_rules import get_stats_in_period, is_stat_eligible, get_schedule
from employer_config import load_employer, get_theme_asset_bytes


# ================================================================
#  CONSTANTS
# ================================================================

PAGE_W, PAGE_H = letter
ML = 50
RED = HexColor("#CC0000")
LIGHT_RED = HexColor("#FFF0F0")
HEADER_BG = HexColor("#EEEEEE")

PAYROLL_DIR = os.path.dirname(os.path.abspath(__file__))
EMPLOYEES_DIR = os.path.join(PAYROLL_DIR, "employees")
ASSETS_DIR = os.path.join(PAYROLL_DIR, "assets")

ROE_REASON_CODES = {
    "A": "Shortage of work / End of contract",
    "B": "Strike or lockout",
    "D": "Illness or injury",
    "E": "Quit",
    "F": "Maternity",
    "G": "Mandatory retirement",
    "K": "Other",
    "M": "Dismissal or suspension",
    "N": "Leave of absence",
    "P": "Parental",
    "Z": "Compassionate care / Family caregiver",
}

# Reason codes that require end_date (separation reasons)
SEPARATION_CODES = {"A", "B", "E", "G", "K", "M"}
# Reason codes for leave (active employees)
LEAVE_CODES = {"D", "F", "N", "P", "Z"}

# Semi-monthly: max pay periods for ROE blocks
MAX_PERIODS_15A = 25   # Insurable hours
MAX_PERIODS_15B = 13   # Insurable earnings (best-weeks)
MAX_PERIODS_15C = 25   # Earnings by pay period (electronic ROE)


# ================================================================
#  DATA ASSEMBLY
# ================================================================

def _parse_payment_date(date_str):
    """Parse date strings. Delegates to utils."""
    return parse_payroll_date(date_str)


def get_rate_for_date(wage_history, target_date):
    """Find the hourly rate effective on a given date."""
    if not wage_history:
        return 0.0
    rate = wage_history[0]["rate"]
    for entry in wage_history:
        eff = date.fromisoformat(entry["effective_date"])
        if eff <= target_date:
            rate = entry["rate"]
        else:
            break
    return rate


def get_employee_remittance_entries(employee):
    """Collect all remittance entries for an employee, sorted most-recent first.

    Returns list of dicts:
      {payment_date: date, period_str: str, gross: float,
       hours: float|None, original: dict}
    """
    entries = []
    for year, month in _db.available_months()[0]:
        for e in _db.get_remittances(year, month):
            if e.get("employee") != employee:
                continue
            pay_date = _parse_payment_date(e.get("payment_date", ""))
            if pay_date is None:
                pay_date = date(year, month, 15)
            entries.append({
                "payment_date": pay_date,
                "period_str": e.get("period", ""),
                "gross": e["gross"],
                "hours": e.get("hours"),
                "original": e,
            })

    entries.sort(key=lambda x: x["payment_date"], reverse=True)
    return entries


def calculate_insurable_hours(entries, wage_history):
    """Calculate insurable hours for each pay period.

    For entries with stored hours, use those. Otherwise back-calculate
    from gross / hourly rate.

    Returns list of dicts:
      {pp_num, payment_date, period_str, gross, rate, hours, source}
    """
    result = []
    for i, e in enumerate(entries):
        rate = get_rate_for_date(wage_history, e["payment_date"])

        if e["hours"] is not None:
            hours = e["hours"]
            source = "recorded"
        elif rate > 0:
            hours = round((e.get("gross") or 0) / rate * 2) / 2  # round to 0.5
            source = "estimated"
        else:
            hours = 0.0
            source = "unknown"

        result.append({
            "pp_num": i + 1,
            "payment_date": e["payment_date"],
            "period_str": e["period_str"],
            "gross": e["gross"],
            "rate": rate,
            "hours": hours,
            "source": source,
        })

    return result


def _final_pay_period_end(last_day_paid):
    """Determine end date of the semi-monthly pay period containing last_day_paid.

    Semi-monthly periods end on the 15th or last day of month.
    """
    if last_day_paid.day <= 15:
        return date(last_day_paid.year, last_day_paid.month, 15)
    else:
        last_day = calendar.monthrange(last_day_paid.year,
                                       last_day_paid.month)[1]
        return date(last_day_paid.year, last_day_paid.month, last_day)


def assemble_roe_data(employee, profile, reason_code, last_day_paid,
                      expected_recall="Not returning",
                      recall_date=None,
                      comments="",
                      other_monies=None,
                      contact_name=None, contact_phone="",
                      hours_override=None,
                      vacation_pay_override=None):
    """Assemble all ROE block data into a single dict.

    Args:
        employee: str employee name
        profile: dict from load_profile()
        reason_code: str (e.g., "E", "A", "M")
        last_day_paid: date (Block 11)
        expected_recall: "Not returning" | "Unknown" | "Specific date"
        recall_date: date (only if expected_recall == "Specific date")
        comments: str (Block 18)
        other_monies: list of dicts [{type, description, amount}]
        contact_name: str (Block 21, defaults to employer signer)
        contact_phone: str (Block 21)
        hours_override: dict {pp_num: hours} for manual corrections
        vacation_pay_override: float (override auto-calculated vacation balance)
    """
    other_monies = other_monies or []
    hours_override = hours_override or {}
    _co = load_employer()
    contact_name = contact_name or _co.get("signer_name", "")

    start_date = date.fromisoformat(profile["start_date"])
    wage_history = profile.get("wage_history", [])

    # Collect remittance entries
    all_entries = get_employee_remittance_entries(employee)
    # Filter to entries on or before last_day_paid
    entries = [e for e in all_entries if e["payment_date"] <= last_day_paid]

    # Calculate hours
    period_data = calculate_insurable_hours(entries, wage_history)

    # Apply manual overrides
    for pd in period_data:
        if pd["pp_num"] in hours_override:
            pd["hours"] = hours_override[pd["pp_num"]]
            pd["source"] = "manual"

    # Block 15A — total insurable hours (max 25 semi-monthly periods)
    hours_periods = period_data[:MAX_PERIODS_15A]
    total_hours = round(sum(p["hours"] for p in hours_periods), 2)

    # Block 15B — total insurable earnings (max 13 semi-monthly periods)
    earnings_periods = period_data[:MAX_PERIODS_15B]
    total_earnings = round(sum(p["gross"] for p in earnings_periods), 2)

    # Block 15C — earnings by pay period (max 25 periods)
    block_15c = period_data[:MAX_PERIODS_15C]

    # Block 12 — final pay period ending date
    final_pp_end = _final_pay_period_end(last_day_paid)

    # Block 17A — vacation pay
    if vacation_pay_override is not None:
        vac_pay = round(vacation_pay_override, 2)
    else:
        vac_data = get_vacation_accrual(employee)
        vac_pay = round(vac_data["balance"], 2) if vac_data else 0.0

    # Block 17B — stat holiday pay after last day paid
    sched = get_schedule(profile.get("schedule_type", "standard"))
    stat_pay = 0.0
    stat_details = []
    # Check stats in the 30 days after last day paid
    stats_after = get_stats_in_period(
        last_day_paid + timedelta(days=1),
        last_day_paid + timedelta(days=30))
    emp_start = date.fromisoformat(profile["start_date"])
    for stat_date, stat_name, is_day_off in stats_after:
        eligible = is_stat_eligible(
            emp_start, as_of=stat_date,
            days_per_week=sched.get("days_per_week", 5))
        if eligible:
            rate = get_rate_for_date(wage_history, stat_date)
            hpd = sched.get("hours_per_day", 8)
            amount = round(rate * hpd, 2)
            stat_pay += amount
            stat_details.append({
                "date": stat_date,
                "name": stat_name,
                "amount": amount,
            })
    stat_pay = round(stat_pay, 2)

    # Build CRA BN in 15-char format
    cra_bn = _co.get("cra_bn", "").replace(" ", "")

    # Employee SIN stripped
    sin_raw = profile.get("sin", "").replace(" ", "")

    # Employee address as one line
    addr_lines = profile.get("employee_address", [])
    addr_oneline = ", ".join(a for a in addr_lines if a)

    # Block 14 — recall
    if expected_recall == "Specific date" and recall_date:
        recall_display = recall_date.isoformat()
    else:
        recall_display = expected_recall

    return {
        "generation_date": date.today(),
        "employee": employee,

        # Block 4 — Employer
        "employer_name": _co.get("legal_name", ""),
        "employer_oa": _co.get("operating_as", ""),
        "employer_address": _co.get("address_oneline", ""),
        "employer_address_lines": _co.get("address_lines", []),

        # Block 5 — CRA BN
        "cra_bn": cra_bn,

        # Block 6 — Pay period type
        "pay_period_type": "Semi-monthly",
        "pay_period_code": "S",

        # Block 7 — Postal code
        "employer_postal": (_co.get("address_lines", ["", "", ""])[2]
                            if len(_co.get("address_lines", [])) > 2
                            else ""),

        # Block 8 — SIN
        "sin": sin_raw,
        "sin_formatted": (f"{sin_raw[:3]}-{sin_raw[3:6]}-{sin_raw[6:]}"
                          if len(sin_raw) == 9 else sin_raw),

        # Block 9 — Employee
        "employee_name": profile.get("employee_name", employee),
        "employee_address": addr_oneline,
        "employee_address_lines": addr_lines,

        # Block 10 — First day worked
        "first_day_worked": start_date,

        # Block 11 — Last day for which paid
        "last_day_paid": last_day_paid,

        # Block 12 — Final pay period ending date
        "final_pp_end": final_pp_end,

        # Block 13 — Occupation
        "occupation": profile.get("position", ""),

        # Block 14 — Expected recall
        "expected_recall": recall_display,

        # Block 15A — Total insurable hours
        "total_insurable_hours": total_hours,

        # Block 15B — Total insurable earnings
        "total_insurable_earnings": total_earnings,

        # Block 15C — Earnings by pay period
        "earnings_by_period": block_15c,

        # Block 16 — Reason
        "reason_code": reason_code,
        "reason_description": ROE_REASON_CODES.get(reason_code, ""),

        # Block 17A — Vacation pay
        "vacation_pay": vac_pay,

        # Block 17B — Stat holiday pay
        "stat_holiday_pay": stat_pay,
        "stat_holiday_details": stat_details,

        # Block 17C — Other monies
        "other_monies": other_monies,

        # Block 18 — Comments
        "comments": comments,

        # Block 20 — Language
        "language": "English",

        # Block 21 — Contact
        "contact_name": contact_name,
        "contact_phone": contact_phone,

        # Metadata
        "period_count": len(block_15c),
        "has_estimated_hours": any(
            p["source"] == "estimated" for p in block_15c),
    }


# ================================================================
#  PDF GENERATION (ReportLab Platypus)
# ================================================================

def _build_styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle(
        "DocTitle", parent=ss["Title"],
        fontSize=16, leading=20, spaceAfter=4,
        textColor=black, alignment=TA_CENTER,
    ))
    ss.add(ParagraphStyle(
        "DocSubtitle", parent=ss["Normal"],
        fontSize=10, leading=14, spaceAfter=10,
        textColor=HexColor("#555555"), alignment=TA_CENTER,
    ))
    ss.add(ParagraphStyle(
        "Section", parent=ss["Heading2"],
        fontSize=11, leading=15, spaceBefore=12, spaceAfter=4,
        textColor=RED, fontName="Helvetica-Bold",
    ))
    ss.add(ParagraphStyle(
        "Body", parent=ss["Normal"],
        fontSize=9, leading=13, spaceAfter=4,
        fontName="Helvetica",
    ))
    ss.add(ParagraphStyle(
        "BodyBold", parent=ss["Normal"],
        fontSize=9, leading=13, spaceAfter=4,
        fontName="Helvetica-Bold",
    ))
    ss.add(ParagraphStyle(
        "Fine", parent=ss["Normal"],
        fontSize=8, leading=11, spaceAfter=3,
        textColor=HexColor("#666666"), fontName="Helvetica",
    ))
    return ss


def _load_pdf_image(source, dpi=200):
    """Convert first page of a PDF to ImageReader.

    source: file path (str), raw bytes, or BytesIO object.
    """
    if isinstance(source, (bytes, bytearray)):
        doc = fitz.open("pdf", source)
    elif hasattr(source, "read"):
        source.seek(0)
        doc = fitz.open("pdf", source.read())
    else:
        doc = fitz.open(source)
    page = doc[0]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=True)
    img_bytes = pix.tobytes("png")
    rect = page.rect
    doc.close()
    return ImageReader(BytesIO(img_bytes)), rect.width, rect.height


def _make_page_callbacks():
    """Fetch theme assets once, return (first_page_fn, later_pages_fn) closures."""
    _assets = {}
    try:
        _assets = get_theme_asset_bytes()
    except Exception:
        pass

    def _common(canvas_obj, doc):
        canvas_obj.saveState()
        footer_b = _assets.get("footer") or b""
        if footer_b:
            ftr_img, fw, fh = _load_pdf_image(footer_b)
            scale = PAGE_W / fw
            canvas_obj.drawImage(ftr_img, 0, 0,
                                 width=PAGE_W, height=fh * scale, mask="auto")
        logo_b = _assets.get("logo") or b""
        if logo_b:
            logo_img, lw, lh = _load_pdf_image(logo_b)
            target_w = PAGE_W * 0.50
            scale = target_w / lw
            target_h = lh * scale
            canvas_obj.setFillAlpha(0.08)
            canvas_obj.setStrokeAlpha(0.08)
            canvas_obj.drawImage(logo_img,
                                 (PAGE_W - target_w) / 2,
                                 (PAGE_H - target_h) / 2,
                                 width=target_w, height=target_h, mask="auto")
        canvas_obj.setFont("Helvetica", 8)
        canvas_obj.setFillColor(HexColor("#999999"))
        canvas_obj.drawCentredString(PAGE_W / 2, 30, f"Page {doc.page}")
        canvas_obj.restoreState()

    def _first_page(canvas_obj, doc):
        canvas_obj.saveState()
        header_b = _assets.get("header") or b""
        if header_b:
            hdr_img, hw, hh = _load_pdf_image(header_b)
            scale = PAGE_W / hw
            canvas_obj.drawImage(hdr_img, 0, PAGE_H - hh * scale,
                                 width=PAGE_W, height=hh * scale, mask="auto")
        canvas_obj.restoreState()
        _common(canvas_obj, doc)

    def _later_pages(canvas_obj, doc):
        _common(canvas_obj, doc)

    return _first_page, _later_pages


def _info_table(data_pairs, col_widths=None):
    """Build a 2-column label/value table."""
    if col_widths is None:
        col_widths = [2.2 * inch, 4.3 * inch]
    rows = []
    for label, value in data_pairs:
        rows.append([
            Paragraph(f"<b>{label}</b>",
                      ParagraphStyle("_l", fontName="Helvetica-Bold",
                                     fontSize=9, leading=12)),
            Paragraph(str(value),
                      ParagraphStyle("_v", fontName="Helvetica",
                                     fontSize=9, leading=12)),
        ])
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, HexColor("#DDDDDD")),
    ]))
    return t


def _fmt_date(d):
    """Format a date for display."""
    if isinstance(d, date):
        return d.strftime("%B %d, %Y")
    return str(d) if d else ""


def _fmt_money(val):
    """Format as $X,XXX.XX."""
    return f"${val:,.2f}"


def generate_roe_pdf(roe_data, output_path=None):
    """Generate a branded ROE reference PDF.

    Args:
        roe_data: dict from assemble_roe_data()
        output_path: str file path (write there if given; otherwise use Storage/local)

    Returns:
        str: storage path or local file path
    """
    employee = roe_data["employee"]
    safe_name = employee.replace(" ", "_")
    gen_date = roe_data["generation_date"].isoformat()
    fname = f"ROE_{gen_date}_{safe_name}.pdf"

    styles = _build_styles()

    _buf = BytesIO()
    doc = SimpleDocTemplate(
        _buf,
        pagesize=letter,
        title=f"ROE — {employee}",
        topMargin=110,
        bottomMargin=80,
        leftMargin=ML,
        rightMargin=50,
    )

    elements = []

    # ── Title ──
    elements.append(Paragraph(
        "RECORD OF EMPLOYMENT", styles["DocTitle"]))
    elements.append(Paragraph(
        "Employer-Retained Reference Document · ROE Web Submission Aid",
        styles["DocSubtitle"]))
    elements.append(Paragraph(
        f"Generated: {_fmt_date(roe_data['generation_date'])}",
        styles["Fine"]))
    elements.append(Spacer(1, 8))

    # ── Employer Information (Blocks 4-7) ──
    elements.append(Paragraph("Employer Information", styles["Section"]))
    elements.append(_info_table([
        ("Legal Name (Block 4)", roe_data["employer_name"]),
        ("Operating As", roe_data["employer_oa"]),
        ("Address", roe_data["employer_address"]),
        ("CRA BN (Block 5)", roe_data["cra_bn"]),
        ("Pay Period Type (Block 6)", roe_data["pay_period_type"]),
        ("Postal Code (Block 7)", roe_data["employer_postal"]),
    ]))
    elements.append(Spacer(1, 6))

    # ── Employee Information (Blocks 8-10, 13) ──
    elements.append(Paragraph("Employee Information", styles["Section"]))
    elements.append(_info_table([
        ("SIN (Block 8)", roe_data["sin_formatted"]),
        ("Name (Block 9)", roe_data["employee_name"]),
        ("Address", roe_data["employee_address"]),
        ("First Day Worked (Block 10)",
         _fmt_date(roe_data["first_day_worked"])),
        ("Occupation (Block 13)", roe_data["occupation"]),
    ]))
    elements.append(Spacer(1, 6))

    # ── Separation Details (Blocks 11-12, 14, 16) ──
    elements.append(Paragraph("Separation Details", styles["Section"]))
    reason_display = (f"Code {roe_data['reason_code']} — "
                      f"{roe_data['reason_description']}")
    elements.append(_info_table([
        ("Last Day Paid (Block 11)", _fmt_date(roe_data["last_day_paid"])),
        ("Final Pay Period End (Block 12)",
         _fmt_date(roe_data["final_pp_end"])),
        ("Reason (Block 16)", reason_display),
        ("Expected Recall (Block 14)", roe_data["expected_recall"]),
    ]))
    elements.append(Spacer(1, 6))

    # ── Insurable Earnings by Pay Period (Block 15C) ──
    elements.append(Paragraph(
        "Insurable Earnings by Pay Period (Block 15C)",
        styles["Section"]))

    if roe_data["has_estimated_hours"]:
        elements.append(Paragraph(
            "Note: Hours marked with * are estimated from "
            "gross pay ÷ hourly rate. Verify before submitting to ROE Web.",
            styles["Fine"]))

    # Build table
    header_row = ["P.P.", "Period", "Earnings", "Rate", "Hours", "Source"]
    table_data = [header_row]

    for p in roe_data["earnings_by_period"]:
        hours_str = f"{p['hours']:.1f}"
        if p["source"] == "estimated":
            hours_str += " *"
        table_data.append([
            str(p["pp_num"]),
            p["period_str"],
            _fmt_money(p["gross"]),
            f"${p['rate']:.2f}/hr",
            hours_str,
            p["source"].title(),
        ])

    # Totals row
    table_data.append([
        "", "TOTALS",
        _fmt_money(roe_data["total_insurable_earnings"]),
        "",
        f"{roe_data['total_insurable_hours']:.1f}",
        "",
    ])

    col_w = [0.4 * inch, 2.4 * inch, 1.0 * inch, 0.9 * inch,
             0.7 * inch, 0.8 * inch]
    t = Table(table_data, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CCCCCC")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ALIGNMENT", (0, 0), (0, -1), "CENTER"),
        ("ALIGNMENT", (2, 0), (2, -1), "RIGHT"),
        ("ALIGNMENT", (3, 0), (3, -1), "RIGHT"),
        ("ALIGNMENT", (4, 0), (4, -1), "RIGHT"),
        # Totals row styling
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), LIGHT_RED),
        ("LINEABOVE", (0, -1), (-1, -1), 1.5, RED),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 4))

    # Summary metrics
    elements.append(Paragraph(
        f"<b>Block 15A — Total Insurable Hours:</b> "
        f"{roe_data['total_insurable_hours']:.1f} &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"<b>Block 15B — Total Insurable Earnings:</b> "
        f"{_fmt_money(roe_data['total_insurable_earnings'])} "
        f"(last {min(len(roe_data['earnings_by_period']), MAX_PERIODS_15B)} "
        f"pay periods)",
        styles["Body"]))
    elements.append(Spacer(1, 8))

    # ── Separation Payments (Block 17) ──
    elements.append(Paragraph("Separation Payments (Block 17)",
                              styles["Section"]))

    pay_rows = [["Block", "Type", "Amount"]]
    pay_rows.append(["17A", "Vacation Pay",
                     _fmt_money(roe_data["vacation_pay"])])

    if roe_data["stat_holiday_pay"] > 0:
        stat_desc = "Stat Holiday Pay"
        if roe_data["stat_holiday_details"]:
            names = ", ".join(s["name"]
                              for s in roe_data["stat_holiday_details"])
            stat_desc += f" ({names})"
        pay_rows.append(["17B", stat_desc,
                         _fmt_money(roe_data["stat_holiday_pay"])])
    else:
        pay_rows.append(["17B", "Statutory Holiday Pay", "$0.00"])

    for om in roe_data["other_monies"]:
        desc = om.get("description", om.get("type", "Other"))
        pay_rows.append(["17C", desc,
                         _fmt_money(om.get("amount", 0))])

    if not roe_data["other_monies"]:
        pay_rows.append(["17C", "Other Monies", "None"])

    t17 = Table(pay_rows, colWidths=[0.6 * inch, 3.8 * inch, 1.2 * inch])
    t17.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CCCCCC")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ALIGNMENT", (2, 0), (2, -1), "RIGHT"),
    ]))
    elements.append(t17)
    elements.append(Spacer(1, 8))

    # ── Comments (Block 18) ──
    if roe_data["comments"]:
        elements.append(Paragraph("Comments (Block 18)",
                                  styles["Section"]))
        elements.append(Paragraph(roe_data["comments"], styles["Body"]))
        elements.append(Spacer(1, 6))

    # ── Contact (Blocks 20-21) ──
    elements.append(Paragraph("Issuer Information", styles["Section"]))
    elements.append(_info_table([
        ("Language (Block 20)", roe_data["language"]),
        ("Contact Name (Block 21)", roe_data["contact_name"]),
        ("Contact Phone", roe_data["contact_phone"]),
    ]))
    elements.append(Spacer(1, 12))

    # ── Signature Block ──
    elements.append(HRFlowable(
        width="100%", thickness=1, color=HexColor("#CCCCCC")))
    elements.append(Spacer(1, 20))

    _co_roe = load_employer()
    signer = _co_roe.get("signer_name", "")
    title = _co_roe.get("signer_title", "")
    sig_data = [
        ["_" * 40, "", "_" * 40],
        [signer, "", ""],
        [title, "", ""],
        ["", "", ""],
        ["_" * 40, "", "_" * 40],
        ["Signature", "", "Date"],
    ]
    sig_t = Table(sig_data, colWidths=[2.3 * inch, 1.5 * inch, 2.3 * inch])
    sig_t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    elements.append(sig_t)

    # ── Fine print ──
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(
        "This document is an employer-retained reference copy. "
        "The official ROE must be submitted electronically via "
        "ROE Web (Service Canada) within 5 calendar days of the "
        "end of the pay period containing the interruption of earnings.",
        styles["Fine"]))

    # Build PDF
    _fp, _lp = _make_page_callbacks()
    doc.build(elements, onFirstPage=_fp, onLaterPages=_lp)
    _buf.seek(0)
    pdf_bytes = _buf.getvalue()

    # If explicit output_path requested (e.g. preview), write there.
    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "wb") as _f:
            _f.write(pdf_bytes)
        return output_path

    # Try Supabase Storage.
    try:
        import storage_client as _sc
        emp_uuid = _db._employee_id(employee)
        if emp_uuid:
            storage_path = f"employees/{emp_uuid}/roe/{fname}"
            _sc.upload_pdf(_sc.PDF_BUCKET, storage_path, pdf_bytes)
            return storage_path
    except Exception:
        pass

    # Local fallback.
    roe_dir = os.path.join(EMPLOYEES_DIR, employee, "ROE")
    os.makedirs(roe_dir, exist_ok=True)
    local_path = os.path.join(roe_dir, fname)
    with open(local_path, "wb") as _f:
        _f.write(pdf_bytes)
    return local_path


# ================================================================
#  ROE HISTORY (JSON persistence)
# ================================================================

def _roe_history_path(employee):
    return os.path.join(EMPLOYEES_DIR, employee, "ROE", "roe_history.json")


def load_roe_history(employee):
    """Load list of previously generated ROEs. Tries DB first, then local JSON."""
    try:
        return _db.get_roe_history(employee)
    except Exception:
        pass
    # Local JSON fallback
    path = _roe_history_path(employee)
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            import sys
            print(f"WARNING: Corrupted ROE history skipped: {path}",
                  file=sys.stderr)
    return []


def save_roe_metadata(employee, roe_data, pdf_path):
    """Save metadata for a generated ROE."""
    entry = {
        "date": roe_data["generation_date"].isoformat(),
        "reason_code": roe_data["reason_code"],
        "reason": roe_data["reason_description"],
        "last_day_paid": roe_data["last_day_paid"].isoformat(),
        "total_hours": roe_data["total_insurable_hours"],
        "total_earnings": roe_data["total_insurable_earnings"],
        "vacation_pay": roe_data["vacation_pay"],
        "pdf_path": pdf_path,
    }
    try:
        _db.save_roe(employee, entry)
        return
    except Exception:
        pass
    # Local JSON fallback
    roe_dir = os.path.join(EMPLOYEES_DIR, employee, "ROE")
    os.makedirs(roe_dir, exist_ok=True)
    history = []
    path = _roe_history_path(employee)
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                history = json.load(f)
        except Exception:
            pass
    history.append(entry)
    with open(path, "w") as f:
        json.dump(history, f, indent=2)
