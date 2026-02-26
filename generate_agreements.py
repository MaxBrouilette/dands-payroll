"""
================================================================
  DECALS AND SIGNS — EMPLOYMENT AGREEMENT PDF GENERATOR
================================================================

Generates formal employment agreements for signing:
  1. Averaging Agreement (schedule-specific)
  2. Stat Holiday Policy Acknowledgment
  3. Cell Phone / Break Tier Policy
  4. Back-Pay Settlement & Release

Uses ReportLab Platypus for clean multi-page text rendering,
with canvas callbacks for branded header/footer/watermark.

================================================================
"""

import os
from datetime import date, datetime
from io import BytesIO

import fitz  # PyMuPDF
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable,
)
from reportlab.pdfgen import canvas

from pay_rules import get_schedule, SCHEDULE_TYPES, ALBERTA_STATS, PAID_DAY_OFF_STATS
from employer_config import load_employer, get_theme_asset_bytes


# ================================================================
#  CONSTANTS
# ================================================================

PAGE_W, PAGE_H = letter
ML = 50
MR = PAGE_W - 50
RED = HexColor("#CC0000")

PAYROLL_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(PAYROLL_DIR, "assets")


# ================================================================
#  STYLES
# ================================================================

def _build_styles():
    ss = getSampleStyleSheet()

    ss.add(ParagraphStyle(
        "DocTitle", parent=ss["Title"],
        fontSize=16, leading=20, spaceAfter=6,
        textColor=black, alignment=TA_CENTER,
    ))
    ss.add(ParagraphStyle(
        "DocSubtitle", parent=ss["Normal"],
        fontSize=10, leading=14, spaceAfter=12,
        textColor=HexColor("#555555"), alignment=TA_CENTER,
    ))
    ss.add(ParagraphStyle(
        "Section", parent=ss["Heading2"],
        fontSize=12, leading=16, spaceBefore=14, spaceAfter=6,
        textColor=black, fontName="Helvetica-Bold",
    ))
    ss.add(ParagraphStyle(
        "Body", parent=ss["Normal"],
        fontSize=10, leading=14, spaceAfter=6,
        alignment=TA_JUSTIFY, fontName="Helvetica",
    ))
    ss.add(ParagraphStyle(
        "BodyBold", parent=ss["Normal"],
        fontSize=10, leading=14, spaceAfter=6,
        alignment=TA_JUSTIFY, fontName="Helvetica-Bold",
    ))
    ss.add(ParagraphStyle(
        "Indent", parent=ss["Normal"],
        fontSize=10, leading=14, spaceAfter=4,
        leftIndent=24, alignment=TA_LEFT, fontName="Helvetica",
    ))
    ss.add(ParagraphStyle(
        "SignLine", parent=ss["Normal"],
        fontSize=10, leading=24, spaceAfter=2,
        fontName="Helvetica",
    ))
    ss.add(ParagraphStyle(
        "Fine", parent=ss["Normal"],
        fontSize=8, leading=11, spaceAfter=4,
        textColor=HexColor("#666666"), fontName="Helvetica",
    ))
    return ss


# ================================================================
#  HELPERS
# ================================================================

def _load_pdf_image(source, dpi=200):
    """Convert first page of a PDF to a PNG ImageReader.

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
            canvas_obj.setFillAlpha(0.15)
            canvas_obj.setStrokeAlpha(0.15)
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


def _upload_or_save_agreement(pdf_bytes: bytes, employee_name: str, fname: str) -> str:
    """Upload to Supabase Storage or write to local agreements folder."""
    try:
        import storage_client as _sc
        import db as _db
        emp_uuid = _db._employee_id(employee_name)
        if emp_uuid:
            storage_path = f"employees/{emp_uuid}/agreements/{fname}"
            _sc.upload_pdf(_sc.PDF_BUCKET, storage_path, pdf_bytes)
            return storage_path
    except Exception:
        pass
    out = _out_dir(employee_name)
    filepath = os.path.join(out, fname)
    with open(filepath, "wb") as _f:
        _f.write(pdf_bytes)
    return filepath


def _ordinal(n):
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd','th'][min(n % 10, 4)]}"


def _fmt_date(d):
    """Format date as 'February 21st, 2026'."""
    return f"{d.strftime('%B')} {_ordinal(d.day)}, {d.year}"


def _signature_block(styles):
    """Return flowables for a dual signature block."""
    _co = load_employer()
    elements = []
    elements.append(Spacer(1, 30))

    sig_data = [
        ["_" * 40, "", "_" * 40],
        [f"{_co['signer_name']}", "", "Employee Name (Print)"],
        [f"{_co['signer_title']}", "", ""],
        ["", "", ""],
        ["_" * 40, "", "_" * 40],
        ["Signature", "", "Signature"],
        ["", "", ""],
        ["_" * 40, "", "_" * 40],
        ["Date", "", "Date"],
    ]
    t = Table(sig_data, colWidths=[2.3 * inch, 0.9 * inch, 2.3 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("ALIGNMENT", (0, 0), (0, -1), "LEFT"),
        ("ALIGNMENT", (2, 0), (2, -1), "LEFT"),
    ]))
    elements.append(t)
    return elements


def _make_doc(filepath, title):
    """Create a SimpleDocTemplate with branded margins."""
    return SimpleDocTemplate(
        filepath,
        pagesize=letter,
        title=title,
        topMargin=110,      # space for header
        bottomMargin=80,     # space for footer
        leftMargin=ML,
        rightMargin=50,
    )


def _out_dir(employee_name):
    """Return (and create) the agreements output directory for an employee."""
    d = os.path.join(PAYROLL_DIR, "employees", employee_name, "agreements")
    os.makedirs(d, exist_ok=True)
    return d


# ================================================================
#  1. AVERAGING AGREEMENT
# ================================================================

def generate_averaging_agreement(employee_name, schedule_key, effective_date=None,
                                  regular_rate=None):
    """Generate a formal Averaging Agreement PDF.

    Args:
        employee_name: Full employee name
        schedule_key:  Key from SCHEDULE_TYPES (e.g. "5x9_design")
        effective_date: date object (defaults to today)
        regular_rate:  Current hourly rate (for reference on doc)

    Returns:
        str: File path to generated PDF
    """
    _co = load_employer()
    COMPANY_NAME         = _co["legal_name"]
    COMPANY_OA           = _co["operating_as"]
    COMPANY_ADDRESS      = _co["address_oneline"]
    sched = get_schedule(schedule_key)
    effective_date = effective_date or date.today()
    styles = _build_styles()
    elements = []

    # -- Title --
    elements.append(Paragraph("AVERAGING AGREEMENT", styles["DocTitle"]))
    elements.append(Paragraph(
        f"Pursuant to the <i>Alberta Employment Standards Code</i>, "
        f"Part 2, Division 4 - Hours of Work",
        styles["DocSubtitle"],
    ))
    elements.append(HRFlowable(
        width="100%", thickness=1, color=RED, spaceAfter=12,
    ))

    # -- Parties --
    elements.append(Paragraph("1. PARTIES", styles["Section"]))
    elements.append(Paragraph(
        f"This Averaging Agreement (the \"Agreement\") is entered into between:",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"<b>Employer:</b> {COMPANY_NAME}, operating as {COMPANY_OA}<br/>"
        f"{COMPANY_ADDRESS}",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"<b>Employee:</b> {employee_name}",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"<b>Effective Date:</b> {_fmt_date(effective_date)}",
        styles["Indent"],
    ))

    # -- Schedule --
    elements.append(Paragraph("2. WORK SCHEDULE", styles["Section"]))
    elements.append(Paragraph(
        f"The Employee agrees to work the following schedule:",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"<b>Schedule Type:</b> {sched['label']}",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"<b>Days per Week:</b> {sched['days_per_week']}",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"<b>Hours per Day:</b> {sched['hours_per_day']}",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"<b>Hours per Week:</b> {sched['hours_per_week']}",
        styles["Indent"],
    ))
    if regular_rate:
        elements.append(Paragraph(
            f"<b>Current Regular Rate:</b> ${regular_rate:.2f}/hour",
            styles["Indent"],
        ))

    # -- Overtime --
    elements.append(Paragraph("3. OVERTIME PROVISIONS", styles["Section"]))
    elements.append(Paragraph(
        f"Under this averaging agreement, overtime is calculated as follows:",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"<b>Daily Overtime Threshold:</b> {sched['ot_daily_threshold']} hours "
        f"(hours worked beyond {sched['ot_daily_threshold']} in a single day "
        f"are paid at {sched['ot_multiplier']}x the regular rate)",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"<b>Weekly Overtime Threshold:</b> {sched['ot_weekly_threshold']} hours "
        f"(hours worked beyond {sched['ot_weekly_threshold']} in a work week "
        f"are paid at {sched['ot_multiplier']}x the regular rate)",
        styles["Indent"],
    ))

    if sched.get("guaranteed_ot_per_week"):
        elements.append(Paragraph(
            f"<b>Guaranteed Overtime:</b> Under this schedule, "
            f"{sched['hours_per_week']} scheduled hours per week exceeds the "
            f"{sched['ot_weekly_threshold']}-hour weekly threshold by "
            f"{sched['guaranteed_ot_per_week']:.0f} hour(s). This "
            f"{sched['guaranteed_ot_per_week']:.0f} hour(s) per week is paid at "
            f"the overtime rate of {sched['ot_multiplier']}x.",
            styles["Indent"],
        ))

    if sched.get("callback_max_hours"):
        elements.append(Paragraph(
            f"<b>Callback Provision:</b> The Employee may be called in on a "
            f"scheduled day off with a minimum of {sched['callback_notice_days']} "
            f"days' written notice for up to {sched['callback_max_hours']} "
            f"additional hours. These hours are paid at the regular rate provided "
            f"weekly totals do not exceed {sched['ot_weekly_threshold']} hours.",
            styles["Indent"],
        ))

    # -- Terms --
    elements.append(Paragraph("4. TERMS AND CONDITIONS", styles["Section"]))

    elements.append(Paragraph(
        f"a) This Agreement is made voluntarily by both parties and may be "
        f"terminated by either party with <b>two (2) weeks' written notice</b>.",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"b) Upon termination of this Agreement, the standard overtime provisions "
        f"under the <i>Alberta Employment Standards Code</i> shall apply "
        f"(overtime after 8 hours/day or 44 hours/week).",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"c) This Agreement does not affect any other terms of employment, "
        f"including but not limited to: wages, benefits, vacation entitlements, "
        f"or termination provisions.",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"d) The Employer shall maintain accurate records of all hours worked "
        f"by the Employee under this Agreement.",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"e) Nothing in this Agreement reduces the Employee's entitlements "
        f"under the <i>Alberta Employment Standards Code</i>.",
        styles["Body"],
    ))

    # -- Acknowledgment --
    elements.append(Paragraph("5. ACKNOWLEDGMENT", styles["Section"]))
    elements.append(Paragraph(
        f"The Employee acknowledges that they have read this Agreement, "
        f"understand its terms, and enter into it freely. The Employee "
        f"acknowledges that they have had the opportunity to seek independent "
        f"advice before signing.",
        styles["Body"],
    ))

    # -- Signatures --
    elements.extend(_signature_block(styles))

    # -- Build PDF --
    fname = f"Averaging_Agreement_{schedule_key}_{effective_date.isoformat()}.pdf"
    _fp, _lp = _make_page_callbacks()
    _buf = BytesIO()
    doc = _make_doc(_buf, f"Averaging Agreement - {employee_name}")
    doc.build(elements, onFirstPage=_fp, onLaterPages=_lp)
    _buf.seek(0)
    return _upload_or_save_agreement(_buf.getvalue(), employee_name, fname)


# ================================================================
#  2. STAT HOLIDAY POLICY
# ================================================================

def generate_stat_policy(employee_name, effective_date=None):
    """Generate a Stat Holiday Policy Acknowledgment PDF.

    Args:
        employee_name: Full employee name
        effective_date: date object (defaults to today)

    Returns:
        str: File path to generated PDF
    """
    _co = load_employer()
    COMPANY_NAME    = _co["legal_name"]
    COMPANY_OA      = _co["operating_as"]
    effective_date = effective_date or date.today()
    styles = _build_styles()
    elements = []
    year = effective_date.year

    elements.append(Paragraph("STATUTORY HOLIDAY POLICY", styles["DocTitle"]))
    elements.append(Paragraph(
        f"{COMPANY_NAME}, operating as {COMPANY_OA}",
        styles["DocSubtitle"],
    ))
    elements.append(HRFlowable(
        width="100%", thickness=1, color=RED, spaceAfter=12,
    ))

    # -- Overview --
    elements.append(Paragraph("1. OVERVIEW", styles["Section"]))
    elements.append(Paragraph(
        f"This policy outlines how {COMPANY_OA} observes and compensates "
        f"employees for the nine (9) mandatory general holidays recognized "
        f"under the <i>Alberta Employment Standards Code</i>. "
        f"This policy is effective as of {_fmt_date(effective_date)}.",
        styles["Body"],
    ))

    # -- Holiday List --
    elements.append(Paragraph("2. RECOGNIZED HOLIDAYS", styles["Section"]))

    stats = ALBERTA_STATS.get(year, ALBERTA_STATS.get(year - 1, []))
    for stat_date, stat_name in stats:
        is_day_off = stat_name in PAID_DAY_OFF_STATS
        tag = " -- <b>Paid Day Off</b>" if is_day_off else " -- <b>Work Day (1.5x)</b>"
        elements.append(Paragraph(
            f"- {stat_name} ({stat_date.strftime('%B %d')}){tag}",
            styles["Indent"],
        ))

    # -- Company Policy --
    elements.append(Paragraph("3. COMPANY POLICY", styles["Section"]))
    elements.append(Paragraph(
        f"<b>Paid Days Off:</b> Christmas Day and New Year's Day are paid "
        f"days off. Eligible employees receive their average daily wage for "
        f"these holidays without being required to work.",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"<b>Mandatory Work Days:</b> All other general holidays (Family Day, "
        f"Good Friday, Victoria Day, Canada Day, Labour Day, Thanksgiving, "
        f"Remembrance Day) are scheduled work days. Employees who work on "
        f"these days are compensated at <b>1.5 times</b> their regular rate "
        f"for all hours worked, plus general holiday pay (average daily wage).",
        styles["Body"],
    ))

    # -- Eligibility --
    elements.append(Paragraph("4. ELIGIBILITY", styles["Section"]))
    elements.append(Paragraph(
        f"Under Alberta law, an employee is eligible for general holiday pay "
        f"after completing <b>30 workdays</b> of employment with the Employer. "
        f"Employees must work their scheduled shift immediately before and "
        f"after the holiday to qualify, unless the Employer has approved "
        f"the absence.",
        styles["Body"],
    ))

    # -- Calculation --
    elements.append(Paragraph("5. HOLIDAY PAY CALCULATION", styles["Section"]))
    elements.append(Paragraph(
        f"General holiday pay is calculated as:",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"<b>Average Daily Wage</b> = Total regular wages earned in the "
        f"4 weeks immediately before the holiday, divided by the number of "
        f"days worked in that period.",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"For employees who work on a general holiday, compensation is:",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"Average Daily Wage <b>+</b> 1.5x regular rate for hours worked",
        styles["Indent"],
    ))

    # -- Acknowledgment --
    elements.append(Paragraph("6. ACKNOWLEDGMENT", styles["Section"]))
    elements.append(Paragraph(
        f"By signing below, the Employee acknowledges that they have read "
        f"and understood this Statutory Holiday Policy and agree to its terms.",
        styles["Body"],
    ))

    elements.extend(_signature_block(styles))

    # -- Build PDF --
    fname = f"Stat_Holiday_Policy_{effective_date.isoformat()}.pdf"
    _fp, _lp = _make_page_callbacks()
    _buf = BytesIO()
    doc = _make_doc(_buf, f"Stat Holiday Policy - {employee_name}")
    doc.build(elements, onFirstPage=_fp, onLaterPages=_lp)
    _buf.seek(0)
    return _upload_or_save_agreement(_buf.getvalue(), employee_name, fname)


# ================================================================
#  3. CELL PHONE / BREAK TIER POLICY
# ================================================================

def generate_break_policy(employee_name, tier=1, effective_date=None):
    """Generate a Cell Phone / Break Tier Policy PDF.

    Args:
        employee_name: Full employee name
        tier: 1 (Standard) or 2 (Flex Break)
        effective_date: date object (defaults to today)

    Returns:
        str: File path to generated PDF
    """
    _co = load_employer()
    COMPANY_NAME    = _co["legal_name"]
    COMPANY_OA      = _co["operating_as"]
    effective_date = effective_date or date.today()
    styles = _build_styles()
    elements = []

    elements.append(Paragraph("CELL PHONE &amp; BREAK POLICY", styles["DocTitle"]))
    elements.append(Paragraph(
        f"{COMPANY_NAME}, operating as {COMPANY_OA}",
        styles["DocSubtitle"],
    ))
    elements.append(HRFlowable(
        width="100%", thickness=1, color=RED, spaceAfter=12,
    ))

    # -- Purpose --
    elements.append(Paragraph("1. PURPOSE", styles["Section"]))
    elements.append(Paragraph(
        f"This policy establishes guidelines for cell phone use and break "
        f"periods during work hours. The policy recognizes two tiers of "
        f"privileges based on employee role, performance, and management "
        f"discretion. Effective {_fmt_date(effective_date)}.",
        styles["Body"],
    ))

    # -- Alberta Requirements --
    elements.append(Paragraph("2. LEGAL REQUIREMENTS", styles["Section"]))
    elements.append(Paragraph(
        f"Under the <i>Alberta Employment Standards Code</i>, employees "
        f"are entitled to a minimum 30-minute break within every 5 consecutive "
        f"hours of work. This break may be paid or unpaid at the Employer's "
        f"discretion. This policy meets or exceeds all legal requirements.",
        styles["Body"],
    ))

    # -- Tier 1 --
    elements.append(Paragraph("3. TIER 1 -- STANDARD", styles["Section"]))
    elements.append(Paragraph(
        f"<b>Break Entitlement:</b>",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"- One (1) x 30-minute meal break (unpaid) per shift",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- Reasonable washroom breaks as needed",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"<b>Cell Phone Use:</b>",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"- Personal cell phones must be stored away from the work area "
        f"during working hours",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- Cell phone use is permitted during the meal break only",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- Emergency calls may be taken at any time with supervisor notice",
        styles["Indent"],
    ))

    # -- Tier 2 --
    elements.append(Paragraph("4. TIER 2 -- FLEX BREAK", styles["Section"]))
    elements.append(Paragraph(
        f"<b>Break Entitlement:</b>",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"- One (1) x 30-minute meal break (unpaid) per shift",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- Two (2) x 10-minute paid rest breaks per shift",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- Reasonable washroom breaks as needed",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"<b>Cell Phone Use:</b>",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"- Personal cell phone use is permitted at the workstation "
        f"provided it does not interfere with work duties or productivity",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- Music, podcasts, and similar media may be played at a "
        f"reasonable volume that does not disturb others",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- Video content (YouTube, streaming, games) is not permitted "
        f"during working hours outside of break periods",
        styles["Indent"],
    ))

    # -- Assignment & Revocation --
    elements.append(Paragraph("5. TIER ASSIGNMENT", styles["Section"]))
    elements.append(Paragraph(
        f"Tier assignment is at the sole discretion of management. "
        f"Tier 2 privileges may be <b>revoked at any time</b> and reduced "
        f"to Tier 1 if:",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"- Cell phone use interferes with work duties or productivity",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- Break periods are abused or extended beyond their allotted time",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- Workplace safety is compromised",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- Video content or games are used during work hours",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"Revocation does not require written notice. "
        f"Tier 2 may be reinstated at management's discretion following "
        f"a review period.",
        styles["Body"],
    ))

    # -- Employee Assignment --
    elements.append(Paragraph("6. YOUR ASSIGNMENT", styles["Section"]))
    tier_label = "Tier 1 -- Standard" if tier == 1 else "Tier 2 -- Flex Break"
    elements.append(Paragraph(
        f"<b>{employee_name}</b> is assigned to: <b>{tier_label}</b>",
        styles["Body"],
    ))

    # -- Acknowledgment --
    elements.append(Paragraph("7. ACKNOWLEDGMENT", styles["Section"]))
    elements.append(Paragraph(
        f"By signing below, the Employee acknowledges that they have read "
        f"and understood this Cell Phone &amp; Break Policy, understand which "
        f"tier they are assigned to, and agree to comply with its terms.",
        styles["Body"],
    ))

    elements.extend(_signature_block(styles))

    # -- Build PDF --
    fname = f"Break_Policy_Tier{tier}_{effective_date.isoformat()}.pdf"
    _fp, _lp = _make_page_callbacks()
    _buf = BytesIO()
    doc = _make_doc(_buf, f"Break Policy - {employee_name}")
    doc.build(elements, onFirstPage=_fp, onLaterPages=_lp)
    _buf.seek(0)
    return _upload_or_save_agreement(_buf.getvalue(), employee_name, fname)


# ================================================================
#  4. BACK-PAY SETTLEMENT & RELEASE
# ================================================================

def generate_settlement(employee_name, settlement_amount, description,
                         period_from, period_to, effective_date=None,
                         already_paid=0.0, notes=""):
    """Generate a Settlement Agreement & Release PDF.

    Framed as a compromise of disputed amounts (T4A box 028 treatment)
    rather than wage arrears. Each agreement is specific to the named
    employee and covers only the period and matters described.

    Args:
        employee_name:    Full employee name
        settlement_amount: Total settlement amount ($)
        description:      What the dispute covers (e.g. "Statutory holiday pay calculations")
        period_from:      date object (start of period covered)
        period_to:        date object (end of period covered)
        effective_date:   date object for signing (defaults to today)
        already_paid:     Amount already paid informally ($)
        notes:            Additional notes/context

    Returns:
        str: File path to generated PDF
    """
    _co = load_employer()
    COMPANY_NAME    = _co["legal_name"]
    COMPANY_OA      = _co["operating_as"]
    COMPANY_ADDRESS = _co["address_oneline"]
    effective_date = effective_date or date.today()
    styles = _build_styles()
    elements = []

    net_owing = round(settlement_amount - already_paid, 2)

    elements.append(Paragraph("SETTLEMENT AGREEMENT &amp; RELEASE", styles["DocTitle"]))
    elements.append(Paragraph(
        f"Confidential",
        styles["DocSubtitle"],
    ))
    elements.append(HRFlowable(
        width="100%", thickness=1, color=RED, spaceAfter=12,
    ))

    # -- Parties --
    elements.append(Paragraph("1. PARTIES", styles["Section"]))
    elements.append(Paragraph(
        f"This Settlement Agreement and Release (the \"Agreement\") is entered "
        f"into between:",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"<b>Employer:</b> {COMPANY_NAME}, operating as {COMPANY_OA}<br/>"
        f"{COMPANY_ADDRESS}",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"<b>Employee:</b> {employee_name}",
        styles["Indent"],
    ))

    # -- Background --
    elements.append(Paragraph("2. BACKGROUND", styles["Section"]))
    elements.append(Paragraph(
        f"A dispute has arisen between the Employer and the Employee "
        f"regarding certain compensation matters during the period "
        f"from <b>{_fmt_date(period_from)}</b> to <b>{_fmt_date(period_to)}</b>. "
        f"The parties hold differing views on the amounts, if any, that may "
        f"be owing.",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"<b>Subject Matter:</b> {description}",
        styles["Indent"],
    ))
    if notes:
        elements.append(Paragraph(
            f"<b>Details:</b> {notes}",
            styles["Indent"],
        ))
    elements.append(Paragraph(
        f"The parties wish to resolve this dispute on a without-prejudice "
        f"basis and without any admission of liability by either party.",
        styles["Body"],
    ))

    # -- Settlement Terms --
    elements.append(Paragraph("3. COMPROMISE &amp; SETTLEMENT", styles["Section"]))
    elements.append(Paragraph(
        f"In order to resolve the above dispute and to avoid the cost and "
        f"uncertainty of further proceedings, the parties have agreed to "
        f"compromise the disputed amounts as follows:",
        styles["Body"],
    ))

    settle_data = [
        ["Item", "Amount"],
        ["Compromise Settlement Amount", f"${settlement_amount:,.2f}"],
    ]
    if already_paid > 0:
        settle_data.append(["Less: Amounts Previously Paid", f"(${already_paid:,.2f})"])
    settle_data.append(["Net Amount Payable", f"${net_owing:,.2f}"])

    t = Table(settle_data, colWidths=[3.2 * inch, 1.8 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#EEEEEE")),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CCCCCC")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ALIGNMENT", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 8))

    elements.append(Paragraph(
        f"The Employer agrees to pay the Employee the Net Amount Payable "
        f"of <b>${net_owing:,.2f}</b> within 30 days of the execution of this "
        f"Agreement. Payment will be made via the Employee's regular payment "
        f"method.",
        styles["Body"],
    ))

    # -- Tax Reporting --
    elements.append(Paragraph("4. TAX REPORTING", styles["Section"]))
    elements.append(Paragraph(
        f"This settlement represents a compromise of disputed amounts and "
        f"is not a payment of wages, salary, or employment income. The "
        f"settlement amount will be reported on a T4A information slip "
        f"(box 028 — Other Income) for the applicable tax year. No source "
        f"deductions for CPP or EI will be applied.",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"The Employee acknowledges that the settlement amount is taxable "
        f"income and that the Employee is solely responsible for reporting "
        f"it on their personal income tax return and for any taxes owing.",
        styles["Body"],
    ))

    # -- Release --
    elements.append(Paragraph("5. RELEASE", styles["Section"]))
    elements.append(Paragraph(
        f"In consideration of the settlement payment described above, the "
        f"Employee releases and forever discharges the Employer, its owners, "
        f"officers, directors, and agents from any and all claims, demands, "
        f"actions, or causes of action arising from or related to the "
        f"matters described in Section 2, for the period specified.",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"This release applies only to the specific matters and period "
        f"described in this Agreement. It does not affect:",
        styles["Body"],
    ))
    elements.append(Paragraph(
        f"- Any claims arising after the date of this Agreement",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- The Employee's ongoing employment relationship with the Employer",
        styles["Indent"],
    ))
    elements.append(Paragraph(
        f"- Any matters not specifically addressed in this Agreement",
        styles["Indent"],
    ))

    # -- No Admission --
    elements.append(Paragraph("6. NO ADMISSION OF LIABILITY", styles["Section"]))
    elements.append(Paragraph(
        f"This Agreement is made on a without-prejudice basis. Nothing in "
        f"this Agreement constitutes an admission of liability, wrongdoing, "
        f"or obligation by either party. The settlement amount represents a "
        f"negotiated compromise reached in good faith to resolve a dispute.",
        styles["Body"],
    ))

    # -- Confidentiality --
    elements.append(Paragraph("7. CONFIDENTIALITY", styles["Section"]))
    elements.append(Paragraph(
        f"Both parties agree to keep the terms and amount of this settlement "
        f"confidential, except as required by law, professional advisors, "
        f"or tax reporting obligations.",
        styles["Body"],
    ))

    # -- Voluntary --
    elements.append(Paragraph("8. VOLUNTARY AGREEMENT", styles["Section"]))
    elements.append(Paragraph(
        f"Both parties acknowledge that this Agreement is entered into "
        f"voluntarily, without duress or undue influence. The Employee "
        f"acknowledges that they have been given the opportunity to seek "
        f"independent legal advice before signing.",
        styles["Body"],
    ))

    # -- Signatures --
    elements.extend(_signature_block(styles))

    # -- Build PDF --
    fname = f"Settlement_{effective_date.isoformat()}.pdf"
    _fp, _lp = _make_page_callbacks()
    _buf = BytesIO()
    doc = _make_doc(_buf, f"Settlement Agreement - {employee_name}")
    doc.build(elements, onFirstPage=_fp, onLaterPages=_lp)
    _buf.seek(0)
    return _upload_or_save_agreement(_buf.getvalue(), employee_name, fname)


# ================================================================
#  FULL PACKAGE — Generate all applicable agreements at once
# ================================================================

def generate_full_package(employee_name, schedule_key="standard",
                           regular_rate=None, break_tier=2,
                           settlement_amount=None, settlement_desc=None,
                           settlement_period_from=None, settlement_period_to=None,
                           settlement_already_paid=0.0, settlement_notes="",
                           effective_date=None):
    """Generate the full signing package for an employee.

    Returns:
        list of (label, filepath) tuples
    """
    effective_date = effective_date or date.today()
    results = []

    # 1. Averaging Agreement (only for non-standard schedules)
    if schedule_key and schedule_key != "standard":
        path = generate_averaging_agreement(
            employee_name, schedule_key, effective_date, regular_rate,
        )
        results.append(("Averaging Agreement", path))

    # 2. Stat Holiday Policy (always)
    path = generate_stat_policy(employee_name, effective_date)
    results.append(("Stat Holiday Policy", path))

    # 3. Break Policy (always)
    path = generate_break_policy(employee_name, break_tier, effective_date)
    results.append(("Cell Phone & Break Policy", path))

    # 4. Settlement (only if amount provided)
    if settlement_amount and settlement_amount > 0:
        path = generate_settlement(
            employee_name, settlement_amount,
            settlement_desc or "Employment entitlement adjustment",
            settlement_period_from or date(effective_date.year - 2, effective_date.month, effective_date.day),
            settlement_period_to or effective_date,
            effective_date, settlement_already_paid, settlement_notes,
        )
        results.append(("Settlement Agreement", path))

    return results


# ================================================================
#  CLI TEST
# ================================================================

if __name__ == "__main__":
    print("Generating test package for Petro Smetaniuk...")
    docs = generate_full_package(
        employee_name="Petro Smetaniuk",
        schedule_key="5x9_design",
        regular_rate=23.25,
        break_tier=2,
    )
    for label, path in docs:
        print(f"  {label}: {path}")
