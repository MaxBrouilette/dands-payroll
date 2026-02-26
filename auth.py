"""
Authentication Gate
===================
Simple email/password login via Supabase Auth.

Usage in app.py:
    from auth import require_auth, sign_out
    if not require_auth():
        st.stop()

If Supabase credentials are not configured (local dev), auth is skipped
and the app runs without a login gate.
"""

import os
import streamlit as st


def _supabase_configured() -> bool:
    """Return True if Supabase credentials are available."""
    try:
        url = st.secrets.get("SUPABASE_URL", "")
        return bool(url and not url.startswith("https://your-project"))
    except Exception:
        url = os.environ.get("SUPABASE_URL", "")
        return bool(url)


def _auth_client():
    url = key = None
    try:
        url = st.secrets.get("SUPABASE_URL")
        key = st.secrets.get("SUPABASE_KEY")
    except Exception:
        pass
    if not url:
        url = os.environ.get("SUPABASE_URL")
    if not key:
        key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        return None
    from supabase import create_client
    return create_client(url, key)


def require_auth() -> bool:
    """Show login form if not authenticated. Returns True when OK to proceed.

    Call this at the very top of app.py before any tab rendering:
        if not require_auth():
            st.stop()
    """
    # No Supabase configured — run locally without auth gate
    if not _supabase_configured():
        return True

    if st.session_state.get("auth_user"):
        return True

    # ── Login form ────────────────────────────────────────────
    st.markdown(
        "<style>div.block-container{padding-top:4rem}</style>",
        unsafe_allow_html=True,
    )
    _, col, _ = st.columns([1, 1.6, 1])
    with col:
        st.markdown("## Payroll Sign In")
        st.markdown("---")
        with st.form("_auth_login"):
            email    = st.text_input("Email", placeholder="your@email.com")
            password = st.text_input("Password", type="password")
            submit   = st.form_submit_button("Sign In", use_container_width=True)

        if submit:
            if not email or not password:
                st.error("Please enter both email and password.")
                return False
            client = _auth_client()
            if client is None:
                st.error("Authentication service unavailable.")
                return False
            try:
                res = client.auth.sign_in_with_password(
                    {"email": email, "password": password}
                )
                st.session_state["auth_user"] = {
                    "id":    res.user.id,
                    "email": res.user.email,
                }
                st.rerun()
            except Exception:
                st.error("Incorrect email or password.")

        st.caption("Authorized access only.")

    return False


def get_current_user() -> dict | None:
    """Return the current user dict (id, email), or None."""
    return st.session_state.get("auth_user")


def sign_out():
    """Sign out and clear session."""
    st.session_state.pop("auth_user", None)
    try:
        client = _auth_client()
        if client:
            client.auth.sign_out()
    except Exception:
        pass
    st.rerun()
