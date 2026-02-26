"""
Supabase Storage Client
=======================
Upload / download / list wrappers for Supabase Storage.

Buckets:
  payroll-pdfs    — generated PDF documents (private, signed URLs for download)
  payroll-assets  — theme PDFs + CRA T4 templates (private, fetched at generation time)

Falls back to local files for assets when Storage is unavailable (local dev mode).
"""

import os
from io import BytesIO

PDF_BUCKET   = "payroll-pdfs"
ASSET_BUCKET = "payroll-assets"

# Local assets root — used as fallback when Supabase Storage is unavailable
_LOCAL_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def _client():
    """Return a Supabase client via credentials in secrets/env."""
    url = key = None
    try:
        import streamlit as st
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


# ════════════════════════════════════════════════════════════
#  UPLOAD
# ════════════════════════════════════════════════════════════

def upload_pdf(bucket: str, path: str, data: bytes) -> str:
    """Upload PDF bytes to Supabase Storage. Returns the storage path.

    Uses upsert=true so regenerated documents overwrite previous versions.
    """
    path = path.lstrip("/")
    (_client().storage
     .from_(bucket)
     .upload(path, data,
             file_options={"content-type": "application/pdf", "upsert": "true"}))
    return path


def upload_file(bucket: str, path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload arbitrary file bytes (e.g. receipt images). Returns storage path."""
    path = path.lstrip("/")
    (_client().storage
     .from_(bucket)
     .upload(path, data,
             file_options={"content-type": content_type, "upsert": "true"}))
    return path


# ════════════════════════════════════════════════════════════
#  DOWNLOAD
# ════════════════════════════════════════════════════════════

def download_pdf(bucket: str, path: str) -> bytes:
    """Download a file from Supabase Storage. Returns raw bytes."""
    path = path.lstrip("/")
    return _client().storage.from_(bucket).download(path)


def download_asset(relative_path: str) -> bytes:
    """Download a theme asset or CRA template from the payroll-assets bucket.

    Falls back to the local assets/ directory when Storage is unavailable
    (e.g. during local development before Supabase is configured).

    Args:
        relative_path: path relative to the bucket root, e.g.
                       "themes/theme_1/header.pdf" or "t4_template_2025.pdf"
    """
    try:
        return download_pdf(ASSET_BUCKET, relative_path)
    except Exception:
        # Fallback: try local file under assets/
        local_path = os.path.join(_LOCAL_ASSETS, relative_path)
        if os.path.isfile(local_path):
            with open(local_path, "rb") as f:
                return f.read()
        raise FileNotFoundError(
            f"Asset not found in Supabase Storage or locally: {relative_path}\n"
            f"Upload it to the '{ASSET_BUCKET}' bucket or place it at: {local_path}"
        )


# ════════════════════════════════════════════════════════════
#  LIST
# ════════════════════════════════════════════════════════════

def list_files(bucket: str, prefix: str) -> list[str]:
    """List files under a prefix. Returns sorted list of full paths (prefix/name)."""
    prefix = prefix.strip("/")
    try:
        items = _client().storage.from_(bucket).list(prefix)
        paths = []
        for item in items:
            name = item.get("name", "")
            if name and not name.startswith(".") and item.get("id"):
                # Only include actual files (id present), not virtual folders
                paths.append(f"{prefix}/{name}")
        return sorted(paths)
    except Exception:
        return []


def list_files_recursive(bucket: str, prefix: str) -> list[str]:
    """Recursively list all files under a prefix (walks sub-folders)."""
    prefix = prefix.strip("/")
    all_paths = []
    try:
        items = _client().storage.from_(bucket).list(prefix)
        for item in items:
            name = item.get("name", "")
            if not name or name.startswith("."):
                continue
            full = f"{prefix}/{name}"
            if item.get("id"):
                all_paths.append(full)   # actual file
            else:
                all_paths.extend(list_files_recursive(bucket, full))  # folder
    except Exception:
        pass
    return sorted(all_paths)


# ════════════════════════════════════════════════════════════
#  SIGNED URLS  (for download buttons)
# ════════════════════════════════════════════════════════════

def get_signed_url(bucket: str, path: str, expires: int = 3600) -> str:
    """Generate a temporary signed URL for a private file."""
    path = path.lstrip("/")
    result = _client().storage.from_(bucket).create_signed_url(path, expires)
    return result.get("signedURL", "")


# ════════════════════════════════════════════════════════════
#  RECEIPTS  (resolution evidence)
# ════════════════════════════════════════════════════════════

def upload_receipt(employee_id: str, payment_date_iso: str,
                   file_bytes: bytes, ext: str) -> str:
    """Upload a resolution receipt file. Returns the storage path."""
    ext = ext.lstrip(".")
    filename = f"{payment_date_iso}_resolution.{ext}"
    path = f"employees/{employee_id}/receipts/{filename}"
    content_type = "application/pdf" if ext.lower() == "pdf" else "image/jpeg"
    return upload_file(PDF_BUCKET, path, file_bytes, content_type)


# ════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════

def storage_path_to_filename(path: str) -> str:
    """Extract the filename from a storage path."""
    return path.split("/")[-1] if path else ""


def employee_pdf_prefix(employee_id: str, doc_type: str) -> str:
    """Return the standard storage prefix for an employee's documents.

    doc_type: "paystubs" | "agreements" | "letters" | "t4" | "roe" | "receipts"
    """
    return f"employees/{employee_id}/{doc_type}"
