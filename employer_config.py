"""
Employer Configuration
======================
Single source of truth for company info. All modules import from here.

Data lives in the Supabase `employer_config` table (single row, id=1).
Falls back to the local employer_config.json file when Supabase is not
configured (local dev / pre-migration).

get_theme_asset_bytes() returns asset content as bytes so generators can
run on Streamlit Cloud without a local filesystem.
"""

import os
import json

_DIR       = os.path.dirname(os.path.abspath(__file__))
_JSON_PATH = os.path.join(_DIR, "employer_config.json")


# ════════════════════════════════════════════════════════════
#  LOAD / SAVE
# ════════════════════════════════════════════════════════════

def load_employer() -> dict:
    """Load employer config. Tries Supabase DB first, falls back to JSON."""
    try:
        import db as _db
        data = _db.load_employer()
        if data:
            return data
    except Exception:
        pass
    # Fallback: local JSON file
    try:
        with open(_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"Employer config is corrupted: {_JSON_PATH}\nError: {exc}"
        ) from exc
    except FileNotFoundError:
        return {}


def save_employer(data: dict):
    """Save employer config to both Supabase DB and local JSON backup."""
    try:
        import db as _db
        _db.save_employer(data)
    except Exception:
        pass
    try:
        with open(_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
#  THEME ASSETS — bytes-based for cloud-compatible generators
# ════════════════════════════════════════════════════════════

def get_theme_asset_bytes(config: dict | None = None) -> dict:
    """Return bytes for the active theme's header, logo, and footer PDFs.

    Tries Supabase Storage first; falls back to local files in assets/.
    Returns: {"header": b"...", "logo": b"...", "footer": b"..."}
    """
    if config is None:
        config = load_employer()

    active = config.get("active_theme", "theme_1")
    themes = config.get("themes", {})
    theme  = themes.get(active, {})

    def _to_storage_path(local_rel: str) -> str:
        """Convert 'assets/themes/theme_1/header.pdf' → 'themes/theme_1/header.pdf'."""
        if not local_rel:
            return ""
        return local_rel.replace("assets/", "", 1).lstrip("/")

    def _get_bytes(storage_path: str, local_fallback: str) -> bytes:
        if storage_path:
            try:
                from storage_client import download_asset
                return download_asset(storage_path)
            except Exception:
                pass
        local_path = os.path.join(_DIR, local_fallback) if local_fallback else ""
        if local_path and os.path.isfile(local_path):
            with open(local_path, "rb") as fh:
                return fh.read()
        return b""

    return {
        "header": _get_bytes(
            _to_storage_path(theme.get("header", "")),
            theme.get("header", "assets/header.pdf"),
        ),
        "logo": _get_bytes(
            _to_storage_path(theme.get("logo", "")),
            theme.get("logo", "assets/logo.pdf"),
        ),
        "footer": _get_bytes(
            _to_storage_path(theme.get("footer", "")),
            theme.get("footer", "assets/footer.pdf"),
        ),
    }


def get_theme_assets(config: dict | None = None) -> dict:
    """Return absolute local file paths for the active theme.

    Kept for backward-compat. Prefer get_theme_asset_bytes() for generators
    since file paths don't work on Streamlit Cloud.
    """
    if config is None:
        config = load_employer()
    active = config.get("active_theme", "theme_1")
    themes = config.get("themes", {})
    theme  = themes.get(active, {})

    def _abs(rel):
        if not rel:
            return ""
        if os.path.isabs(rel):
            return rel
        return os.path.join(_DIR, rel)

    return {
        "header": _abs(theme.get("header", "assets/header.pdf")),
        "logo":   _abs(theme.get("logo",   "assets/logo.pdf")),
        "footer": _abs(theme.get("footer", "assets/footer.pdf")),
    }


# ════════════════════════════════════════════════════════════
#  MODULE-LEVEL CONVENIENCE
# ════════════════════════════════════════════════════════════

# Loaded at import time with a safe fallback so other modules can do
# `from employer_config import EMPLOYER` without crashing at startup.
try:
    EMPLOYER = load_employer()
except Exception:
    EMPLOYER = {}
