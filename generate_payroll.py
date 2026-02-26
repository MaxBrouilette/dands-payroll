#!/usr/bin/env python3
"""
================================================================
  DECALS AND SIGNS — PAYROLL PDF GENERATOR
================================================================

  Generates CRA-compliant paystubs with:
    - Earnings breakdown + Gross Pay total
    - Deductions with YTD tracking
    - Employer contributions (CPP/EI match)
    - Net Pay with YTD
    - Vacation accrual info
    - Masked SIN, pay period numbering

  Usage:
      python generate_payroll.py

  Output:
      ./employees/<Employee Name>/Payroll_YYYY-MM-DD_<Name>.pdf

================================================================
"""

import os
from datetime import datetime
import fitz                                    # PyMuPDF — PDF-to-image
from utils import parse_payroll_date
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from employer_config import EMPLOYER as _CO, get_theme_asset_bytes


# ================================================================
#  PAYROLL DATA — Edit this section for each payroll run
# ================================================================

PAYROLL_DATA = {
    # ---- Company (from employer_config.json) ----
    "employer_name":      _CO.get("legal_name", ""),
    "company_address":    _CO.get("address_lines", []),

    # ---- Employee ----
    "employee_name":      "Petro Smetaniuk",
    "employee_id":        "N/A",
    "employee_address":   ["305, 3431-139 Ave", "Edmonton, AB", "T5Y 1Z4"],
    "position":           "Graphic Designer",
    "payment_method":     "E-Transfer",
    "sin_masked":         "***-***-456",

    # ---- Pay Period ----
    "period_from":        "January 28th, 2026",
    "period_to":          "February 11th, 2026",
    "payment_date":       "February 15th, 2026",

    # ---- Earnings ----
    "regular_rate":       23.25,
    "regular_hours":      98.5,
    "commission":         None,

    # ---- Deductions (employee) ----
    "cpp":                127.59,
    "ei":                 37.33,
    "fed_tax":            193.35,
    "prov_tax":           94.50,

    # ---- Employer contributions ----
    "cpp_employer":       127.59,
    "ei_employer":        52.26,

    # ---- YTD ----
    "ytd_gross":          6434.44,
    "ytd_net":            5199.82,
    "ytd_cpp":            350.00,
    "ytd_ei":             102.00,
    "ytd_fed_tax":        530.00,
    "ytd_prov_tax":       260.00,

    # ---- Vacation ----
    "vacation_rate":      0.04,
    "vacation_balance":   257.38,

    # ---- Branding Assets (loaded from employer_config at generation time) ----
    "logo_opacity": 0.15,
}


# ================================================================
#  CONSTANTS
# ================================================================

PAGE_W, PAGE_H = letter    # 612 x 792 points
ML  = 50                   # left margin
MR  = PAGE_W - 50          # right margin (562)
CW  = MR - ML              # content width (512)
RED = HexColor("#CC0000")
HIGHLIGHT = HexColor("#FFF0F0")


# ================================================================
#  HELPERS
# ================================================================

def load_pdf_image(source, dpi=300):
    """Convert first page of a PDF to a high-res PNG ImageReader.

    source: file path (str), raw bytes, or BytesIO object.
    Returns (ImageReader, page_width_pt, page_height_pt).
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


def money(val):
    """Format as $X,XXX.XX — empty string if None."""
    if val is None:
        return ""
    return f"${val:,.2f}"


def money_z(val):
    """Format as $X,XXX.XX — shows $0.00 for zero, empty only for None."""
    if val is None:
        return ""
    return f"${val:,.2f}"


def num(val):
    """Format a number cleanly (drop trailing zeros)."""
    if val is None:
        return ""
    return f"{val:g}"


def rounded_cell(c, x, y, w, h, text,
                 border=RED, fill=white,
                 font="Helvetica-Bold", size=12,
                 color=black, radius=5):
    """Draw a rounded-rectangle cell with centered text."""
    c.saveState()
    c.setStrokeColor(border)
    c.setFillColor(fill)
    c.setLineWidth(1.2)
    c.roundRect(x, y, w, h, radius, stroke=1, fill=1)
    c.setFillColor(color)
    c.setFont(font, size)
    c.drawCentredString(x + w / 2, y + (h - size) / 2 + 1, text)
    c.restoreState()


def label_value(c, x, y, label, value, lsize=11, vsize=11):
    """Draw 'Label: Value' — label regular, value bold."""
    c.setFont("Helvetica", lsize)
    c.setFillColor(black)
    c.drawString(x, y, label)
    tw = c.stringWidth(label, "Helvetica", lsize)
    c.setFont("Helvetica-Bold", vsize)
    c.drawString(x + tw, y, value)


# ================================================================
#  MAIN GENERATOR
# ================================================================

def generate_payroll(data, output_path=None):
    """Build the full payroll PDF and return the output file path.
    If output_path is given, write there instead of the default location."""

    # ── Calculations ──────────────────────────────────────────
    reg_rate  = data.get("regular_rate") or 0
    reg_hours = data.get("regular_hours") or 0
    reg_total = round(reg_rate * reg_hours, 2)

    ot_rate   = data.get("overtime_rate") or 0
    ot_hours  = data.get("overtime_hours") or 0
    ot_total  = round(ot_rate * ot_hours, 2)

    stat_rt   = data.get("stat_rate") or 0
    stat_hrs  = data.get("stat_hours") or 0
    stat_total = round(stat_rt * stat_hrs, 2)

    hol_pay   = data.get("holiday_pay") or 0
    vac_pay   = data.get("vacation_pay") or 0
    comm      = data.get("commission") or 0

    gross     = reg_total + ot_total + stat_total + hol_pay + vac_pay + comm
    total_hrs = (reg_hours or 0) + (ot_hours or 0) + (stat_hrs or 0)

    cpp       = data.get("cpp") or 0
    cpp2      = data.get("cpp2") or 0
    ei        = data.get("ei") or 0
    fed_tax   = data.get("fed_tax") or 0
    prov_tax  = data.get("prov_tax") or 0
    total_ded = round(cpp + cpp2 + ei + fed_tax + prov_tax, 2)

    net_pay   = round(gross - total_ded, 2)
    ytd_gross = data["ytd_gross"] if data.get("ytd_gross") is not None else gross
    ytd_net   = data["ytd_net"]   if data.get("ytd_net")   is not None else net_pay

    # Employer contributions
    cpp_er  = data.get("cpp_employer") or 0
    cpp2_er = data.get("cpp2_employer") or 0
    ei_er   = data.get("ei_employer") or 0
    total_er = round(cpp_er + cpp2_er + ei_er, 2)

    # YTD deductions (include current period)
    ytd_cpp  = data.get("ytd_cpp") or cpp
    ytd_cpp2 = data.get("ytd_cpp2") or cpp2
    ytd_ei   = data.get("ytd_ei") or ei
    ytd_fed  = data.get("ytd_fed_tax") or fed_tax
    ytd_prov = data.get("ytd_prov_tax") or prov_tax
    ytd_ded  = round(ytd_cpp + ytd_cpp2 + ytd_ei + ytd_fed + ytd_prov, 2)

    # ── Output path ───────────────────────────────────────────
    name      = data["employee_name"]
    safe_name = name.replace(" ", "_")

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "employees", name)

    pay_str = data["payment_date"]
    pay_dt = parse_payroll_date(pay_str)
    if pay_dt is None:
        raise ValueError(f"Cannot parse payment date: {pay_str!r}")
    iso_date = pay_dt.strftime("%Y-%m-%d")

    filepath = os.path.join(out_dir, f"Payroll_{iso_date}_{safe_name}.pdf")

    # ── Theme Assets ──────────────────────────────────────────
    _assets = {}
    try:
        _assets = get_theme_asset_bytes()
    except Exception:
        pass

    # ── Canvas (writes to memory buffer) ─────────────────────
    _buf = BytesIO()
    c = canvas.Canvas(_buf, pagesize=letter)
    c.setTitle(f"Payroll - {name}")

    # ==========================================================
    #  LOGO WATERMARK — drawn first so it sits behind everything
    # ==========================================================
    _logo_bytes = _assets.get("logo") or b""
    if _logo_bytes:
        logo_img, logo_orig_w, logo_orig_h = load_pdf_image(_logo_bytes)
        target_w = PAGE_W * 0.60
        scale    = target_w / logo_orig_w
        target_h = logo_orig_h * scale

        c.saveState()
        c.setFillAlpha(data.get("logo_opacity", 0.15))
        c.setStrokeAlpha(data.get("logo_opacity", 0.15))
        c.drawImage(logo_img,
                    (PAGE_W - target_w) / 2,
                    (PAGE_H - target_h) / 2,
                    width=target_w, height=target_h,
                    mask='auto')
        c.restoreState()

    # ==========================================================
    #  HEADER — full page width, flush to top edge
    # ==========================================================
    header_h = 80
    _header_bytes = _assets.get("header") or b""
    if _header_bytes:
        hdr_img, hdr_orig_w, hdr_orig_h = load_pdf_image(_header_bytes)
        hdr_scale = PAGE_W / hdr_orig_w
        header_h  = hdr_orig_h * hdr_scale
        c.drawImage(hdr_img, 0, PAGE_H - header_h,
                    width=PAGE_W, height=header_h, mask='auto')

    # ==========================================================
    #  INFO SECTION
    # ==========================================================
    y  = PAGE_H - header_h - 14
    lx = ML + 10
    rx = 345
    vgap = 18

    # ── Left column ──
    label_value(c, lx, y, "Employer Name: ", data["employer_name"])

    y -= vgap + 16
    label_value(c, lx, y, "Employee Name: ", data["employee_name"])

    y -= vgap + 4
    label_value(c, lx, y, "Position/Title: ", data["position"])

    y -= vgap
    label_value(c, lx, y, "Payment Method: ", data["payment_method"])

    # Schedule type (averaging agreements only)
    _sched_key = data.get("schedule_type")
    if _sched_key and _sched_key != "standard":
        from pay_rules import get_schedule as _get_sched
        _sched_def = _get_sched(_sched_key)
        y -= vgap
        label_value(c, lx, y, "Schedule: ", _sched_def["label"])

    # ── Right column ──
    ry = PAGE_H - header_h - 14
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(black)
    c.drawString(rx, ry, "Employer Address:")
    ry -= 14
    for addr_line in data["company_address"]:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(rx, ry, addr_line)
        ry -= 13

    ry -= 4
    label_value(c, rx, ry, "Employee ID No: ", data["employee_id"])

    # Masked SIN + Employee Address on same block
    sin_masked = data.get("sin_masked", "")
    if sin_masked:
        ry -= vgap
        label_value(c, rx, ry, "SIN: ", sin_masked)

    ry -= vgap
    c.setFont("Helvetica-Bold", 11)
    c.drawString(rx, ry, "Employee Address:")
    ry -= 14
    for addr_line in data["employee_address"]:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(rx, ry, addr_line)
        ry -= 13

    # Use the lower of left/right column bottoms
    y = min(y, ry)

    # ==========================================================
    #  PAYMENT PERIOD ROW
    # ==========================================================
    y -= 22
    bw = 160;  bh = 26
    spacing = (CW - 3 * bw) / 2
    bx = [ML + i * (bw + spacing) for i in range(3)]

    pp_headers = ["Payment Period (From)", "Payment Period (To)",
                  "Payment Date"]
    pp_values  = [data["period_from"], data["period_to"],
                  data["payment_date"]]

    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(black)
    for i, hdr in enumerate(pp_headers):
        c.drawCentredString(bx[i] + bw / 2, y, hdr)

    y -= bh + 4
    for i, val in enumerate(pp_values):
        rounded_cell(c, bx[i], y, bw, bh, val,
                     font="Helvetica-Bold", size=10, color=black)

    # ==========================================================
    #  EARNINGS TABLE
    # ==========================================================
    y -= 25
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(black)
    c.drawString(ML, y, "Earnings")

    # Build dynamic earnings rows: (label, rate, hours, total)
    # rate/hours = None for flat-amount items (no empty boxes drawn)
    earnings_rows = [("Regular", reg_rate, reg_hours, reg_total)]
    if ot_hours and ot_hours > 0:
        earnings_rows.append(("\u00d71.5 Overtime", ot_rate, ot_hours,
                              ot_total))
    if stat_hrs and stat_hrs > 0:
        earnings_rows.append(("\u00d71.5 Stat Holiday", stat_rt, stat_hrs,
                              stat_total))
    if hol_pay and hol_pay > 0:
        earnings_rows.append(("Holiday Pay", None, None, hol_pay))
    if vac_pay and vac_pay > 0:
        earnings_rows.append(("Vacation Pay", None, None, vac_pay))
    if comm:
        earnings_rows.append(("Commission", None, None, comm))

    has_gross_row = len(earnings_rows) > 1

    # Column positions: [Type, Rate, Hours, Current]
    ecx = [ML, ML + 145, ML + 240, ML + 320]
    ecw = [135, 85, 70, 120]
    ech = 22
    egap = 3

    # Column headers
    y -= 18
    c.setFont("Helvetica", 8)
    c.setFillColor(black)
    for i, hdr in enumerate(["", "Rate", "Hours", "Current"]):
        c.drawCentredString(ecx[i] + ecw[i] / 2, y, hdr)

    # Earnings rows
    for row_idx, (label, r_rate, r_hours, r_total) in enumerate(
            earnings_rows):
        y -= ech + (3 if row_idx == 0 else egap)
        is_flat = r_rate is None  # flat-amount item (no rate/hours)

        if is_flat:
            # Span type across first 3 columns, then just current
            wide_w = ecx[3] - ecx[0] - 10
            rounded_cell(c, ecx[0], y, wide_w, ech, label, size=10)
            rounded_cell(c, ecx[3], y, ecw[3], ech, money_z(r_total),
                         size=10)
        else:
            for i, val in enumerate([label, money(r_rate), num(r_hours),
                                     money_z(r_total)]):
                rounded_cell(c, ecx[i], y, ecw[i], ech, val, size=10)

    # ── GROSS PAY TOTAL ROW (only when 2+ earning types) ──
    if has_gross_row:
        y -= ech + egap + 2
        c.setStrokeColor(RED)
        c.setLineWidth(1.0)
        c.line(ecx[0], y + ech + 1, ecx[3] + ecw[3], y + ech + 1)

        # Blended rate = hourly earnings only / total hours
        hourly_earn = reg_total + ot_total + stat_total
        blended = (f"${hourly_earn / total_hrs:,.2f}"
                   if total_hrs > 0 else "")
        gross_vals = [
            "GROSS PAY",
            blended,
            f"{total_hrs:g}" if total_hrs > 0 else "",
            money_z(gross),
        ]
        for i, val in enumerate(gross_vals):
            fill = HIGHLIGHT if i == 3 else white
            rounded_cell(c, ecx[i], y, ecw[i], ech, val, size=10,
                         fill=fill)

    # YTD Gross row — label tile spans left 3 cols, value tile under Current
    y -= ech + egap
    wide_w = ecx[3] - ecx[0] - 10
    rounded_cell(c, ecx[0], y, wide_w, ech, "YTD Gross", size=10)
    rounded_cell(c, ecx[3], y, ecw[3], ech, money_z(ytd_gross),
                 size=10, fill=HIGHLIGHT)

    # ==========================================================
    #  DEDUCTIONS SECTION
    # ==========================================================
    y -= 25
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(black)
    c.drawString(ML, y, "Deductions")

    ded_items = [("CPP= ", cpp)]
    if cpp2 > 0:
        ded_items.append(("CPP2= ", cpp2))
    ded_items += [
        ("EI= ",               ei),
        ("FED TAX= ",          fed_tax),
        ("PROV TAX= ",         prov_tax),
        ("TOTAL DEDUCTIONS= ", total_ded),
    ]
    dx       = ML + 15
    line_end = ML + 350

    for lbl, val in ded_items:
        y -= 20
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(black)
        c.drawString(dx, y, lbl.upper())
        tw = c.stringWidth(lbl.upper(), "Helvetica-Bold", 10)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(dx + tw, y - 1, money_z(val))
        c.setStrokeColor(RED)
        c.setLineWidth(0.5)
        c.line(dx, y - 6, line_end, y - 6)

    # YTD Total Deductions
    y -= 14
    c.setFont("Helvetica", 8)
    c.setFillColor(black)
    c.drawString(dx, y, f"YTD Total Deductions: {money_z(ytd_ded)}")

    # ==========================================================
    #  NET PAY SECTION
    # ==========================================================
    y -= 25
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(black)
    c.drawString(ML, y, "Net Pay")

    npx = [ML, ML + 130, ML + 290]
    npw = [100, 140, 140]
    nph = 24

    y -= 18
    c.setFont("Helvetica", 8)
    c.setFillColor(black)
    for i, hdr in enumerate(["", "Current Total", "YTD Net"]):
        c.drawCentredString(npx[i] + npw[i] / 2, y, hdr)

    y -= nph + 3
    for i, (val, tc, fs) in enumerate([
        ("Net Pay", black, 11),
        (money_z(net_pay), black, 13),
        (money_z(ytd_net), black, 13),
    ]):
        fill = HIGHLIGHT if i >= 1 else white
        rounded_cell(c, npx[i], y, npw[i], nph, val,
                     size=fs, color=tc, fill=fill)

    # ==========================================================
    #  VACATION PAY ACCRUAL (info line)
    # ==========================================================
    vac_rate = data.get("vacation_rate")
    vac_balance = data.get("vacation_balance")
    if vac_rate is not None:
        y -= 16
        c.setFont("Helvetica", 8)
        c.setFillColor(black)
        vac_text = f"Vacation Accrual Rate: {vac_rate:.0%}"
        if vac_balance is not None:
            vac_text += (f"   |   Vacation Pay Balance: "
                         f"{money_z(vac_balance)}")
        c.drawString(ML + 10, y, vac_text)

    # ==========================================================
    #  FOOTER — full page width, flush to bottom edge
    # ==========================================================
    _footer_bytes = _assets.get("footer") or b""
    if _footer_bytes:
        ftr_img, ftr_orig_w, ftr_orig_h = load_pdf_image(_footer_bytes)
        ftr_scale = PAGE_W / ftr_orig_w
        footer_h  = ftr_orig_h * ftr_scale
        c.drawImage(ftr_img, 0, 0,
                    width=PAGE_W, height=footer_h, mask='auto')

    # ── SAVE ──────────────────────────────────────────────────
    c.save()
    _buf.seek(0)
    pdf_bytes = _buf.getvalue()

    # If an explicit output_path was given (e.g. for on-screen preview), write there.
    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "wb") as _f:
            _f.write(pdf_bytes)
        print(f"Payroll PDF created: {output_path}")
        return output_path

    # Try Supabase Storage first.
    try:
        import storage_client as _sc
        import db as _db
        _emp_uuid = _db._employee_id(name)
        if _emp_uuid:
            _storage_path = (f"employees/{_emp_uuid}/paystubs/"
                             f"Payroll_{iso_date}_{safe_name}.pdf")
            _sc.upload_pdf(_sc.PDF_BUCKET, _storage_path, pdf_bytes)
            print(f"Payroll PDF uploaded: {_storage_path}")
            return _storage_path
    except Exception:
        pass

    # Local fallback.
    os.makedirs(out_dir, exist_ok=True)
    with open(filepath, "wb") as _f:
        _f.write(pdf_bytes)
    print(f"Payroll PDF created: {filepath}")
    return filepath


# ================================================================
#  RUN
# ================================================================
if __name__ == "__main__":
    generate_payroll(PAYROLL_DATA)
