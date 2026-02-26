"""
Trash / Recovery Bin
====================
Soft-delete for all payroll delete/overwrite operations.

All trash data is stored in the Supabase `trash` table.
The Audit tab surfaces a "Recently Deleted" section where records
can be inspected and restored.

Retention is configurable in employer_config (trash_retention_days).
Presets:  24 hr → 1 day | 3 days | 1 week → 7 | 2 weeks → 14 | 1 month → 30
"""

import db

# Re-export constants from db for backward compatibility
ITEM_TYPES           = db.ITEM_TYPES
RETENTION_PRESETS    = db.RETENTION_PRESETS
DEFAULT_RETENTION_DAYS = db.DEFAULT_RETENTION_DAYS


def soft_delete(item_type, employee, context, original_entry):
    """Move a record to the trash bin.

    Args:
        item_type:      One of ITEM_TYPES
        employee:       Employee name string
        context:        Dict of human-readable metadata (year, month, date, etc.)
        original_entry: Full dict of the record being deleted
    """
    db.soft_delete(item_type, employee, context, original_entry)


def get_trash(item_type=None, employee=None):
    """Return trash entries, optionally filtered, sorted newest first.

    Each entry includes _trash_type and _trash_id fields.
    """
    return db.get_trash(item_type=item_type, employee=employee)


def restore_entry(trash_id):
    """Remove an entry from trash and return (original_entry, full_record).

    The caller is responsible for writing the entry back to its original store.

    Args:
        trash_id: The UUID of the trash row (from entry["id"] or _trash_id)

    Returns:
        (original_entry dict, full_trash_record) or (None, None)
    """
    return db.restore_from_trash(trash_id)


def purge_expired(retention_days=DEFAULT_RETENTION_DAYS):
    """Permanently delete entries older than retention_days.

    Returns dict with purge count.
    """
    return db.purge_expired(retention_days)


def get_retention_days(employer_config):
    """Read trash_retention_days from employer config."""
    return db.get_retention_days(employer_config)


def trash_summary():
    """Return count of items in trash by type."""
    return db.trash_summary()
