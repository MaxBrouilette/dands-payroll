"""
Variance Resolution Tracker
============================
Records how payroll variance discrepancies were resolved (cheque, e-transfer,
adjustment on next remittance, etc.) and stores uploaded receipt files in
Supabase Storage.
"""

import os
from datetime import datetime
import db
import storage_client


def save_receipt(employee_name, payment_date_iso, uploaded_file):
    """Upload a receipt file to Supabase Storage. Returns the storage path.

    Falls back to saving locally when Storage is not configured.
    """
    ext = os.path.splitext(uploaded_file.name)[1] or ".pdf"
    file_bytes = uploaded_file.getbuffer()

    # Get employee UUID for the storage path
    emp_id = db._employee_id(employee_name) or employee_name.replace(" ", "_")

    try:
        return storage_client.upload_receipt(emp_id, payment_date_iso,
                                              bytes(file_bytes), ext)
    except Exception:
        # Fallback: save locally
        payroll_dir = os.path.dirname(os.path.abspath(__file__))
        dest_dir    = os.path.join(payroll_dir, "employees", employee_name, "receipts")
        os.makedirs(dest_dir, exist_ok=True)
        filename = f"{payment_date_iso}_resolution{ext}"
        dest = os.path.join(dest_dir, filename)
        with open(dest, "wb") as f:
            f.write(file_bytes)
        return os.path.relpath(dest, payroll_dir)


def get_receipt_bytes(storage_path):
    """Download a receipt from Storage. Returns bytes or None."""
    if not storage_path:
        return None
    try:
        return storage_client.download_pdf(storage_client.PDF_BUCKET, storage_path)
    except Exception:
        # Fallback: try local file
        payroll_dir = os.path.dirname(os.path.abspath(__file__))
        local = os.path.join(payroll_dir, storage_path)
        if os.path.isfile(local):
            with open(local, "rb") as f:
                return f.read()
        return None


def log_resolution(employee, payment_date_str, pay_year, pay_month,
                   variance_amount, resolution_method, resolution_amount,
                   resolution_date, reference_number="",
                   receipt_file_path="", notes=""):
    """Record a variance resolution."""
    entry = {
        "employee":          employee,
        "payment_date":      payment_date_str,
        "pay_year":          pay_year,
        "pay_month":         pay_month,
        "variance_amount":   round(variance_amount, 2),
        "resolution_method": resolution_method,
        "resolution_amount": round(resolution_amount, 2),
        "resolution_date":   resolution_date,
        "reference_number":  reference_number,
        "receipt_file":      receipt_file_path,
        "notes":             notes,
        "resolved_at":       datetime.now().isoformat(timespec="seconds"),
    }
    return db.log_resolution(entry)


def get_resolution_for_period(employee, payment_date_str):
    """Look up a resolution by employee + payment date. Returns first match or None."""
    return db.get_resolution_for_period(employee, payment_date_str)


def get_resolutions(year, employee=None):
    """Return all resolutions for a year, optionally filtered by employee."""
    return db.get_resolutions(year, employee=employee)


def get_all_resolution_years():
    """Return sorted list of years that have resolution data."""
    return db.get_all_resolution_years()


def delete_resolution(year, index):
    """Remove a resolution by positional index. Soft-deletes to trash. Returns True if deleted."""
    return db.delete_resolution(year, index)
