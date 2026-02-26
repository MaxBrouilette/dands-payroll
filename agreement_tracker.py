"""
Agreement Tracker
=================
Manages employee agreement metadata (signed PDFs, dates).

Data is stored in the Supabase `agreements` table.
"""

import db


def load_agreements(employee_name):
    """Load all agreement metadata for an employee. Returns dict."""
    return db.get_agreements(employee_name)


def save_agreements(employee_name, data):
    """Save agreement metadata dict (upserts all keys)."""
    for key, val in data.items():
        db.set_agreement(
            employee_name, key,
            signed_pdf=val.get("signed_pdf"),
            signed_date=val.get("signed_date"),
        )


def get_agreement(employee_name, agreement_key):
    """Get metadata for a specific agreement. Returns dict or {}."""
    return load_agreements(employee_name).get(agreement_key, {})


def set_agreement(employee_name, agreement_key, signed_pdf, signed_date):
    """Record a signed agreement."""
    db.set_agreement(employee_name, agreement_key,
                     signed_pdf=signed_pdf, signed_date=signed_date)
