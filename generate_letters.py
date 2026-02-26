"""
================================================================
  DECALS AND SIGNS — EMPLOYMENT LETTER PDF GENERATOR
================================================================

Generates formal employment letters:
  1. Employment Verification Letter
  2. Wage Change Notification Letter
  3. Termination Letter

Uses ReportLab Platypus for clean single-page rendering,
with canvas callbacks for branded header/footer/watermark.

================================================================
"""

import os
from datetime import date
from io import BytesIO

import fitz  # PyMuPDF
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import HexColor, black
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable,
)

from employer_config import load_employer, get_theme_asset_bytes


# ================================================================
#  CONSTANTS
# ================================================================

PAGE_W, PAGE_H = letter
ML = 50
RED = HexColor("#CC0000")

PAYROLL_DIR = os.path.dirname(os.path.abspath(__file__))
EMPLOYEES_DIR = os.path.join(PAYROLL_DIR, "employees")
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


def _upload_or_save_letter(pdf_bytes: bytes, employee_name: str, fname: str) -> str:
    """Upload to Supabase Storage or write to local letters folder."""
    try:
        import storage_client as _sc
        import db as _db
        emp_uuid = _db._employee_id(employee_name)
        if emp_uuid:
            storage_path = f"employees/{emp_uuid}/letters/{fname}"
            _sc.upload_pdf(_sc.PDF_BUCKET, storage_path, pdf_bytes)
            return storage_path
    except Exception:
        pass
    out_dir = _letters_dir(employee_name)
    filepath = os.path.join(out_dir, fname)
    with open(filepath, "wb") as _f:
        _f.write(pdf_bytes)
    return filepath


def _ordinal(n):
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd','th'][min(n % 10, 4)]}"


def _fmt_date(d):
    """Format date as 'February 21st, 2026'. Accepts date or ISO string."""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return f"{d.strftime('%B')} {_ordinal(d.day)}, {d.year}"


def _info_table(data_pairs, col_widths=None):
    """Build a 2-column label/value table."""
    if col_widths is None:
        col_widths = [2.2 * inch, 4.3 * inch]
    rows = []
    for label, value in data_pairs:
        rows.append([
            Paragraph(f"<b>{label}</b>",
                      ParagraphStyle("_l", fontName="Helvetica-Bold",
                                     fontSize=10, leading=13)),
            Paragraph(str(value),
                      ParagraphStyle("_v", fontName="Helvetica",
                                     fontSize=10, leading=13)),
        ])
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, HexColor("#DDDDDD")),
    ]))
    return t


def _employer_signature_block():
    """Return flowables for an employer-only signature block."""
    _co = load_employer()
    elements = [Spacer(1, 30)]
    sig_data = [
        ["_" * 40],
        [f"{_co['signer_name']}"],
        [f"{_co['signer_title']}"],
        [f"{_co['operating_as']}"],
        [""],
        ["_" * 40],
        ["Date"],
    ]
    t = Table(sig_data, colWidths=[3 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    elements.append(t)
    return elements


def _dual_signature_block():
    """Return flowables for a dual signature block (employer + employee)."""
    _co = load_employer()
    elements = [Spacer(1, 30)]
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
        topMargin=110,
        bottomMargin=80,
        leftMargin=ML,
        rightMargin=50,
    )


def _letters_dir(employee_name):
    """Return (and create) the Letters output directory."""
    d = os.path.join(EMPLOYEES_DIR, employee_name, "Letters")
    os.makedirs(d, exist_ok=True)
    return d


# ================================================================
#  1. EMPLOYMENT VERIFICATION LETTER
# ================================================================

def generate_verification_letter(employee_name, profile, letter_date=None):
    """Generate an Employment Verification Letter PDF.

    Args:
        employee_name: str
        profile: dict from load_profile()
        letter_date: date (defaults to today)

    Returns:
        str: absolute path to generated PDF
    """
    _co = load_employer()
    COMPANY_NAME    = _co["legal_name"]
    COMPANY_OA      = _co["operating_as"]
    COMPANY_FULL    = _co.get("full_name", f"{COMPANY_NAME} o/a {COMPANY_OA}")
    COMPANY_ADDRESS = _co["address_oneline"]
    letter_date = letter_date or date.today()
    styles = _build_styles()

    emp_name = profile.get("employee_name", employee_name)
    position = profile.get("position", "Employee")
    emp_type = profile.get("employment_type", "Full-Time")
    start_str = profile.get("start_date", "")
    end_str = profile.get("end_date", "")
    wh = profile.get("wage_history", [])
    current_rate = wh[-1]["rate"] if wh else 0

    start_date = date.fromisoformat(start_str) if start_str else None
    end_date = date.fromisoformat(end_str) if end_str else None

    # Status text
    if end_date:
        status_text = (
            f"was employed from {_fmt_date(start_date)} "
            f"to {_fmt_date(end_date)}")
    else:
        status_text = (
            f"has been employed since {_fmt_date(start_date)}"
            if start_date else "is employed")

    elements = []

    # Title
    elements.append(Paragraph(
        "EMPLOYMENT VERIFICATION LETTER", styles["DocTitle"]))
    elements.append(Paragraph(COMPANY_FULL, styles["DocSubtitle"]))
    elements.append(HRFlowable(
        width="100%", thickness=1, color=RED, spaceAfter=12))

    # Date
    elements.append(Paragraph(
        f"Date: {_fmt_date(letter_date)}", styles["Body"]))
    elements.append(Spacer(1, 12))

    # Salutation
    elements.append(Paragraph(
        "To Whom It May Concern,", styles["BodyBold"]))
    elements.append(Spacer(1, 8))

    # Body
    elements.append(Paragraph(
        f"This letter confirms that <b>{emp_name}</b> {status_text} "
        f"by {COMPANY_NAME}, operating as {COMPANY_OA}, located at "
        f"{COMPANY_ADDRESS}.",
        styles["Body"]))
    elements.append(Spacer(1, 8))

    # Details table
    details = [
        ("Employee Name:", emp_name),
        ("Position:", position),
        ("Employment Type:", emp_type),
    ]
    if start_date:
        details.append(("Start Date:", _fmt_date(start_date)))
    if end_date:
        details.append(("End Date:", _fmt_date(end_date)))
    if current_rate > 0:
        details.append(("Current Rate of Pay:",
                        f"${current_rate:.2f} per hour"))
    details.append(("Employment Status:",
                    "Terminated" if end_date else "Currently Employed"))

    elements.append(_info_table(details))
    elements.append(Spacer(1, 12))

    # Closing
    elements.append(Paragraph(
        "This letter is provided at the request of the above-named "
        "individual for verification purposes only. It does not "
        "constitute a guarantee of continued employment.",
        styles["Body"]))
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(
        "Should you require further information, please do not "
        "hesitate to contact the undersigned.",
        styles["Body"]))

    # Signature
    elements.extend(_employer_signature_block())

    # Fine print
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(
        f"Generated on {_fmt_date(letter_date)}. "
        f"Valid as of the date of issue.",
        styles["Fine"]))

    # Build PDF
    fname = f"Verification_{letter_date.isoformat()}.pdf"
    _fp, _lp = _make_page_callbacks()
    _buf = BytesIO()
    doc = _make_doc(_buf, f"Verification Letter — {emp_name}")
    doc.build(elements, onFirstPage=_fp, onLaterPages=_lp)
    _buf.seek(0)
    return _upload_or_save_letter(_buf.getvalue(), employee_name, fname)


# ================================================================
#  2. WAGE CHANGE NOTIFICATION LETTER
# ================================================================

def generate_wage_change_letter(employee_name, profile, old_rate, new_rate,
                                 effective_date, note=""):
    """Generate a Wage Change Notification Letter PDF.

    Args:
        employee_name: str
        profile: dict
        old_rate: float
        new_rate: float
        effective_date: date
        note: str - reason/note

    Returns:
        str: absolute path to generated PDF
    """
    _co = load_employer()
    COMPANY_FULL = _co.get("full_name",
                           f"{_co['legal_name']} o/a {_co['operating_as']}")
    if isinstance(effective_date, str):
        effective_date = date.fromisoformat(effective_date)
    styles = _build_styles()
    emp_name = profile.get("employee_name", employee_name)
    position = profile.get("position", "Employee")
    change = new_rate - old_rate
    change_pct = (change / old_rate * 100) if old_rate > 0 else 0

    elements = []

    # Title
    elements.append(Paragraph(
        "WAGE CHANGE NOTIFICATION", styles["DocTitle"]))
    elements.append(Paragraph(COMPANY_FULL, styles["DocSubtitle"]))
    elements.append(HRFlowable(
        width="100%", thickness=1, color=RED, spaceAfter=12))

    # Date
    elements.append(Paragraph(
        f"Date: {_fmt_date(date.today())}", styles["Body"]))
    elements.append(Spacer(1, 12))

    # Addressed to employee
    elements.append(Paragraph(
        f"Dear {emp_name},", styles["BodyBold"]))
    elements.append(Spacer(1, 8))

    # Body
    elements.append(Paragraph(
        f"This letter confirms a change to your hourly rate of pay, "
        f"effective <b>{_fmt_date(effective_date)}</b>.",
        styles["Body"]))
    elements.append(Spacer(1, 8))

    # Rate change table
    direction = "increase" if change > 0 else "decrease"
    details = [
        ("Employee:", emp_name),
        ("Position:", position),
        ("Previous Rate:", f"${old_rate:.2f} per hour"),
        ("New Rate:", f"${new_rate:.2f} per hour"),
        ("Change:", f"{'+'if change >= 0 else ''}"
                    f"${change:.2f}/hr ({change_pct:+.1f}%)"),
        ("Effective Date:", _fmt_date(effective_date)),
    ]
    elements.append(_info_table(details))
    elements.append(Spacer(1, 8))

    if note:
        elements.append(Paragraph(
            f"<b>Reason:</b> {note}", styles["Body"]))
        elements.append(Spacer(1, 6))

    elements.append(Paragraph(
        f"This rate {direction} will be reflected on all payroll "
        f"calculations from the effective date forward. All other "
        f"terms and conditions of your employment remain unchanged.",
        styles["Body"]))
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(
        "Please sign below to acknowledge receipt of this notification.",
        styles["Body"]))

    # Dual signature
    elements.extend(_dual_signature_block())

    # Build PDF
    fname = f"Wage_Change_{effective_date.isoformat()}.pdf"
    _fp, _lp = _make_page_callbacks()
    _buf = BytesIO()
    doc = _make_doc(_buf, f"Wage Change — {emp_name}")
    doc.build(elements, onFirstPage=_fp, onLaterPages=_lp)
    _buf.seek(0)
    return _upload_or_save_letter(_buf.getvalue(), employee_name, fname)


# ================================================================
#  3. TERMINATION LETTER
# ================================================================

TERM_TYPE_LABELS = {
    "with_cause": "Termination for Cause",
    "without_cause": "Termination Without Cause",
    "resignation_accepted": "Acceptance of Resignation",
}


def generate_termination_letter(employee_name, profile, term_type,
                                 reason, final_pay_date,
                                 severance=0, comments=""):
    """Generate a Termination Letter PDF.

    Args:
        employee_name: str
        profile: dict
        term_type: "with_cause" | "without_cause" | "resignation_accepted"
        reason: str
        final_pay_date: date
        severance: float
        comments: str

    Returns:
        str: absolute path to generated PDF
    """
    _co = load_employer()
    COMPANY_OA   = _co["operating_as"]
    COMPANY_FULL = _co.get("full_name",
                           f"{_co['legal_name']} o/a {_co['operating_as']}")
    if isinstance(final_pay_date, str):
        final_pay_date = date.fromisoformat(final_pay_date)
    styles = _build_styles()
    emp_name = profile.get("employee_name", employee_name)
    position = profile.get("position", "Employee")
    start_str = profile.get("start_date", "")
    end_str = profile.get("end_date", "")

    start_date = date.fromisoformat(start_str) if start_str else None
    end_date = date.fromisoformat(end_str) if end_str else None

    title_text = TERM_TYPE_LABELS.get(term_type, "Termination of Employment")
    is_resignation = (term_type == "resignation_accepted")

    elements = []

    # Title
    elements.append(Paragraph(
        title_text.upper(), styles["DocTitle"]))
    elements.append(Paragraph(
        f"{COMPANY_FULL} — Confidential", styles["DocSubtitle"]))
    elements.append(HRFlowable(
        width="100%", thickness=1, color=RED, spaceAfter=12))

    # Date
    elements.append(Paragraph(
        f"Date: {_fmt_date(date.today())}", styles["Body"]))
    elements.append(Spacer(1, 12))

    # Addressed to employee
    elements.append(Paragraph(
        f"Dear {emp_name},", styles["BodyBold"]))
    elements.append(Spacer(1, 8))

    # Opening paragraph — varies by type
    if is_resignation:
        elements.append(Paragraph(
            f"This letter confirms that your resignation from "
            f"{COMPANY_OA} has been accepted, effective "
            f"<b>{_fmt_date(end_date)}</b>." if end_date else
            f"This letter confirms that your resignation from "
            f"{COMPANY_OA} has been accepted.",
            styles["Body"]))
    elif term_type == "with_cause":
        elements.append(Paragraph(
            f"This letter confirms that your employment with "
            f"{COMPANY_OA} is terminated effective "
            f"<b>{_fmt_date(end_date)}</b> for cause."
            if end_date else
            f"This letter confirms that your employment with "
            f"{COMPANY_OA} is terminated for cause.",
            styles["Body"]))
    else:  # without_cause
        elements.append(Paragraph(
            f"This letter confirms that your employment with "
            f"{COMPANY_OA} is terminated effective "
            f"<b>{_fmt_date(end_date)}</b> without cause."
            if end_date else
            f"This letter confirms that your employment with "
            f"{COMPANY_OA} is terminated without cause.",
            styles["Body"]))

    elements.append(Spacer(1, 8))

    # Employment details
    elements.append(Paragraph("Employment Details", styles["Section"]))
    details = [("Employee:", emp_name), ("Position:", position)]
    if start_date:
        details.append(("Start Date:", _fmt_date(start_date)))
    if end_date:
        details.append(("Last Day of Employment:", _fmt_date(end_date)))
    elements.append(_info_table(details))
    elements.append(Spacer(1, 8))

    # Reason
    elements.append(Paragraph("Reason", styles["Section"]))
    elements.append(Paragraph(reason, styles["Body"]))
    elements.append(Spacer(1, 8))

    # Final pay details
    elements.append(Paragraph("Final Pay", styles["Section"]))
    pay_details = [
        ("Final Pay Date:", _fmt_date(final_pay_date)),
    ]
    if severance > 0:
        pay_details.append(("Severance:", f"${severance:,.2f}"))
    elements.append(_info_table(pay_details))
    elements.append(Spacer(1, 4))
    elements.append(Paragraph(
        "Your final pay will include all outstanding wages and any "
        "vacation pay owing under the Alberta Employment Standards Code.",
        styles["Body"]))
    elements.append(Spacer(1, 8))

    # ROE note
    elements.append(Paragraph("Record of Employment", styles["Section"]))
    elements.append(Paragraph(
        "A Record of Employment (ROE) will be issued electronically "
        "via Service Canada within 5 calendar days of your last day "
        "of employment, as required by law.",
        styles["Body"]))
    elements.append(Spacer(1, 8))

    # Company property
    elements.append(Paragraph("Company Property", styles["Section"]))
    elements.append(Paragraph(
        "All company property, including but not limited to keys, "
        "access cards, equipment, tools, and documents, must be "
        "returned on or before your last day of employment.",
        styles["Body"]))

    # Additional comments
    if comments:
        elements.append(Spacer(1, 8))
        elements.append(Paragraph("Additional Notes", styles["Section"]))
        elements.append(Paragraph(comments, styles["Body"]))

    # Dual signature
    elements.extend(_dual_signature_block())

    # Fine print
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(
        "This document is confidential and intended solely for the "
        "named recipient.", styles["Fine"]))

    # Build PDF
    fname = f"Termination_{date.today().isoformat()}.pdf"
    _fp, _lp = _make_page_callbacks()
    _buf = BytesIO()
    doc = _make_doc(_buf, f"{title_text} — {emp_name}")
    doc.build(elements, onFirstPage=_fp, onLaterPages=_lp)
    _buf.seek(0)
    return _upload_or_save_letter(_buf.getvalue(), employee_name, fname)
