"""
Migrate Local Payroll Data to Supabase
=======================================
One-time migration script.  Run locally with service_role credentials.

Usage:
    python migrate_to_supabase.py

Credentials (set one of these before running):
    Option A — .streamlit/secrets.toml:
        SUPABASE_URL = "https://xxxx.supabase.co"
        SUPABASE_KEY = "eyJ..."   # use service_role key for migration

    Option B — environment variables:
        set SUPABASE_URL=https://xxxx.supabase.co
        set SUPABASE_KEY=eyJ...

What this does:
    1. Upload employer_config.json → employer_config table
    2. Upload employees (profile.json, wage_history, vacation, agreements)
    3. Upload remittances (all YYYY-MM.json files)
    4. Upload remittance status files
    5. Upload local PDFs → payroll-pdfs Storage bucket
    6. Update remittance/agreement/roe rows with new Storage paths
    7. Upload theme assets + CRA templates → payroll-assets Storage bucket

Safe to run multiple times (upsert semantics throughout).
"""

import os
import re
import sys
import json
import glob
from datetime import date, datetime

# ── Locate payroll root ───────────────────────────────────────────────────────
PAYROLL_DIR   = os.path.dirname(os.path.abspath(__file__))
EMPLOYEES_DIR = os.path.join(PAYROLL_DIR, "employees")
REMIT_DIR     = os.path.join(PAYROLL_DIR, "remittances")
AUDIT_DIR     = os.path.join(PAYROLL_DIR, "audit")
ASSETS_DIR    = os.path.join(PAYROLL_DIR, "assets")


# ── Supabase client ───────────────────────────────────────────────────────────
def _make_client():
    url = key = None
    # Try .streamlit/secrets.toml
    secrets_path = os.path.join(PAYROLL_DIR, ".streamlit", "secrets.toml")
    if os.path.isfile(secrets_path):
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                tomllib = None
        if tomllib:
            try:
                with open(secrets_path, "rb") as f:
                    secrets = tomllib.load(f)
                url = secrets.get("SUPABASE_URL")
                key = secrets.get("SUPABASE_KEY")
            except Exception as e:
                print(f"  [warn] Could not read secrets.toml: {e}")
    # Fallback: env vars
    url = url or os.environ.get("SUPABASE_URL")
    key = key or os.environ.get("SUPABASE_KEY")

    if not url or not key:
        print("\n[ERROR] Supabase credentials not found.")
        print("Set SUPABASE_URL and SUPABASE_KEY in .streamlit/secrets.toml or env vars.")
        sys.exit(1)

    from supabase import create_client
    client = create_client(url, key)
    print(f"  Connected to: {url}")
    return client


# ── Ordinal parsing helper ─────────────────────────────────────────────────────
_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)", re.IGNORECASE)

def _parse_display_date(date_str):
    """Parse 'February 1st, 2026' → date object (or None)."""
    if not date_str:
        return None
    try:
        clean = _ORDINAL_RE.sub(r"\1", date_str)
        return datetime.strptime(clean.strip(), "%B %d, %Y").date()
    except (ValueError, AttributeError):
        return None


def _safe_date(val):
    """Return ISO string or None for a date/string value."""
    if not val:
        return None
    if isinstance(val, date):
        return val.isoformat()
    return str(val) if val else None


# ── Step 1: Employer config ───────────────────────────────────────────────────
def migrate_employer_config(client):
    print("\n[1] Migrating employer_config …")
    config_path = os.path.join(PAYROLL_DIR, "employer_config.json")
    if not os.path.isfile(config_path):
        print("    employer_config.json not found — skipping.")
        return
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    client.table("employer_config").upsert(
        {"id": 1, "config": config, "updated_at": datetime.now().isoformat()},
        on_conflict="id"
    ).execute()
    print("    Done.")


# ── Step 2: Employees ─────────────────────────────────────────────────────────
def migrate_employees(client):
    print("\n[2] Migrating employees …")
    emp_uuid_map = {}   # name → UUID (for later PDF upload)

    if not os.path.isdir(EMPLOYEES_DIR):
        print("    employees/ directory not found — skipping.")
        return emp_uuid_map

    for emp_name in sorted(os.listdir(EMPLOYEES_DIR)):
        emp_dir = os.path.join(EMPLOYEES_DIR, emp_name)
        if not os.path.isdir(emp_dir):
            continue

        profile_path = os.path.join(emp_dir, "profile.json")
        if not os.path.isfile(profile_path):
            print(f"    [{emp_name}] No profile.json — skipping employee record.")
            continue

        with open(profile_path, "r", encoding="utf-8") as f:
            profile = json.load(f)

        print(f"    [{emp_name}] Inserting employee record …")
        emp_fields = {
            "employee_name":          profile.get("employee_name") or emp_name,
            "employee_id":            profile.get("employee_id", ""),
            "position":               profile.get("position", ""),
            "payment_method":         profile.get("payment_method", ""),
            "schedule_type":          profile.get("schedule_type", "standard"),
            "regular_rate":           profile.get("regular_rate"),
            "sin":                    profile.get("sin", ""),
            "dob":                    _safe_date(profile.get("dob")),
            "phone":                  profile.get("phone", ""),
            "email":                  profile.get("email", ""),
            "employment_type":        profile.get("employment_type", "Full-Time"),
            "province":               profile.get("province", "Alberta"),
            "start_date":             _safe_date(profile.get("start_date")),
            "end_date":               _safe_date(profile.get("end_date")),
            "emergency_contact_name":  profile.get("emergency_contact_name", ""),
            "emergency_contact_phone": profile.get("emergency_contact_phone", ""),
            "bank_institution":        profile.get("bank_institution", ""),
            "bank_transit":            profile.get("bank_transit", ""),
            "bank_account":            profile.get("bank_account", ""),
            "td1_fed_claim":           profile.get("td1_fed_claim", 0),
            "td1_prov_claim":          profile.get("td1_prov_claim", 0),
            "td1_extra_deduction":     profile.get("td1_extra_deduction", 0),
            "td1_exempt":              profile.get("td1_exempt", False),
            "employee_address":        profile.get("employee_address", []),
            "part_time":               profile.get("part_time"),
            "is_archived":             profile.get("is_archived", False),
            "updated_at":              datetime.now().isoformat(),
        }

        # Upsert by employee_name
        existing = (client.table("employees")
                    .select("id")
                    .eq("employee_name", emp_fields["employee_name"])
                    .maybe_single()
                    .execute())

        if existing and existing.data:
            emp_id = existing.data["id"]
            client.table("employees").update(emp_fields).eq("id", emp_id).execute()
            print(f"      Updated existing record (id={emp_id})")
        else:
            result = client.table("employees").insert(emp_fields).execute()
            emp_id = result.data[0]["id"]
            print(f"      Inserted new record (id={emp_id})")

        emp_uuid_map[emp_name] = emp_id

        # Wage history
        wage_history = profile.get("wage_history", [])
        if wage_history:
            client.table("wage_history").delete().eq("employee_id", emp_id).execute()
            client.table("wage_history").insert([
                {"employee_id": emp_id,
                 "rate": w["rate"],
                 "effective_date": _safe_date(w.get("effective_date")),
                 "note": w.get("note", "")}
                for w in wage_history
            ]).execute()
            print(f"      {len(wage_history)} wage history entries migrated.")

        # Vacation payouts
        vac_payouts = profile.get("vacation_payouts", [])
        if vac_payouts:
            for vp in vac_payouts:
                client.table("vacation_payouts").insert({
                    "employee_id": emp_id,
                    "date": _safe_date(vp.get("date")),
                    "amount": vp.get("amount", 0),
                    "year_ending": _safe_date(vp.get("year_ending")),
                    "method": vp.get("method", "Cheque"),
                    "note": vp.get("note", ""),
                }).execute()
            print(f"      {len(vac_payouts)} vacation payout entries migrated.")

        # Vacation gross overrides
        vac_overrides = profile.get("vacation_gross_overrides", {})
        for year_key, gross_amount in vac_overrides.items():
            client.table("vacation_gross_overrides").upsert({
                "employee_id": emp_id,
                "year_key": year_key,
                "gross_amount": round(float(gross_amount), 2),
            }, on_conflict="employee_id,year_key").execute()
        if vac_overrides:
            print(f"      {len(vac_overrides)} vacation gross overrides migrated.")

        # Agreements (from agreements.json in employee folder)
        agr_path = os.path.join(emp_dir, "agreements.json")
        if os.path.isfile(agr_path):
            try:
                with open(agr_path, "r", encoding="utf-8") as f:
                    agr_data = json.load(f)
                for agr_key, agr_val in agr_data.items():
                    client.table("agreements").upsert({
                        "employee_id": emp_id,
                        "agreement_key": agr_key,
                        "signed_pdf_path": agr_val.get("signed_pdf"),
                        "signed_date": _safe_date(agr_val.get("signed_date")),
                        "updated_at": datetime.now().isoformat(),
                    }, on_conflict="employee_id,agreement_key").execute()
                print(f"      {len(agr_data)} agreement records migrated.")
            except Exception as e:
                print(f"      [warn] Could not migrate agreements.json: {e}")

    return emp_uuid_map


# ── Step 3: Remittances ───────────────────────────────────────────────────────
def migrate_remittances(client):
    print("\n[3] Migrating remittances …")
    remit_id_map = {}  # (employee, payment_date) → DB UUID

    if not os.path.isdir(REMIT_DIR):
        print("    remittances/ directory not found — skipping.")
        return remit_id_map

    for json_file in sorted(glob.glob(os.path.join(REMIT_DIR, "[0-9][0-9][0-9][0-9]-[0-9][0-9].json"))):
        fname = os.path.basename(json_file)
        match = re.match(r"(\d{4})-(\d{2})\.json", fname)
        if not match:
            continue
        pay_year, pay_month = int(match.group(1)), int(match.group(2))

        with open(json_file, "r", encoding="utf-8") as f:
            try:
                entries = json.load(f)
            except json.JSONDecodeError as e:
                print(f"    [{fname}] JSON error: {e} — skipping.")
                continue

        if not isinstance(entries, list):
            print(f"    [{fname}] Unexpected format — skipping.")
            continue

        print(f"    [{fname}] {len(entries)} entries …")
        for entry in entries:
            if not entry.get("employee") or not entry.get("payment_date"):
                continue

            # Look up employee UUID
            emp_result = (client.table("employees")
                          .select("id")
                          .eq("employee_name", entry["employee"])
                          .maybe_single()
                          .execute())
            emp_id = emp_result.data["id"] if (emp_result and emp_result.data) else None

            pay_dt = _parse_display_date(entry["payment_date"])

            # Check for existing row to avoid duplicates
            existing_rem = (client.table("remittances")
                            .select("id")
                            .eq("employee_name", entry["employee"])
                            .eq("payment_date", entry["payment_date"])
                            .eq("pay_year", pay_year)
                            .eq("pay_month", pay_month)
                            .maybe_single()
                            .execute())
            if existing_rem and existing_rem.data:
                remit_id_map[(entry["employee"], entry["payment_date"])] = (
                    existing_rem.data["id"])
                continue  # already migrated

            row = {
                "employee_id":           emp_id,
                "employee_name":         entry["employee"],
                "payment_date":          entry["payment_date"],
                "payment_date_iso":      pay_dt.isoformat() if pay_dt else None,
                "pay_year":              pay_year,
                "pay_month":             pay_month,
                "period_label":          entry.get("period", ""),
                "gross":                 entry.get("gross"),
                "cpp_employee":          entry.get("cpp_employee"),
                "cpp_employer":          entry.get("cpp_employer"),
                "cpp2_employee":         entry.get("cpp2_employee", 0),
                "cpp2_employer":         entry.get("cpp2_employer", 0),
                "ei_employee":           entry.get("ei_employee"),
                "ei_employer":           entry.get("ei_employer"),
                "fed_tax":               entry.get("fed_tax"),
                "prov_tax":              entry.get("prov_tax"),
                "total_remittance":      entry.get("total_remittance"),
                "hours":                 entry.get("hours"),
                "ei_insurable_gross":    entry.get("ei_insurable_gross"),
                "cpp_pensionable_gross": entry.get("cpp_pensionable_gross"),
                "pdf_storage_path":      entry.get("pdf_path"),  # will update later
            }
            result = client.table("remittances").insert(row).execute()
            if result.data:
                row_id = result.data[0]["id"]
                remit_id_map[(entry["employee"], entry["payment_date"])] = row_id

        # Status file
        status_path = os.path.join(REMIT_DIR, fname.replace(".json", "-status.json"))
        if os.path.isfile(status_path):
            try:
                with open(status_path, "r", encoding="utf-8") as f:
                    status = json.load(f)
                if status:
                    client.table("remittance_status").upsert({
                        "pay_year":       pay_year,
                        "pay_month":      pay_month,
                        "remitted":       status.get("remitted", False),
                        "remitted_date":  _safe_date(status.get("remitted_date")),
                        "confirmation":   status.get("confirmation", ""),
                        "notes":          status.get("notes", ""),
                        "updated_at":     datetime.now().isoformat(),
                    }, on_conflict="pay_year,pay_month").execute()
                    print(f"      Status file migrated.")
            except Exception as e:
                print(f"      [warn] Status file error: {e}")

    return remit_id_map


# ── Step 4: Audit manual entries ──────────────────────────────────────────────
def migrate_audit(client):
    print("\n[4] Migrating audit entries …")
    if not os.path.isdir(AUDIT_DIR):
        print("    audit/ directory not found — skipping.")
        return
    for json_file in sorted(glob.glob(os.path.join(AUDIT_DIR, "*.json"))):
        with open(json_file, "r", encoding="utf-8") as f:
            try:
                entries = json.load(f)
            except json.JSONDecodeError:
                continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            emp_result = (client.table("employees")
                          .select("id")
                          .eq("employee_name", entry.get("employee", ""))
                          .maybe_single()
                          .execute())
            emp_id = emp_result.data["id"] if (emp_result and emp_result.data) else None
            pay_dt = _parse_display_date(entry.get("payment_date", ""))
            client.table("audit_manual").insert({
                "employee_id":    emp_id,
                "employee_name":  entry.get("employee", ""),
                "payment_date":   entry.get("payment_date", ""),
                "payment_date_iso": pay_dt.isoformat() if pay_dt else None,
                "pay_year":       entry.get("pay_year"),
                "pay_month":      entry.get("pay_month"),
                "period_from":    entry.get("period_from", ""),
                "period_to":      entry.get("period_to", ""),
                "hours":          entry.get("hours"),
                "gross":          entry.get("gross"),
                "actual_cpp":     entry.get("cpp_employee", 0),
                "actual_ei":      entry.get("ei_employee", 0),
                "actual_fed_tax": entry.get("fed_tax", 0),
                "actual_prov_tax": entry.get("prov_tax", 0),
                "formula_cpp":    entry.get("formula_cpp", 0),
                "formula_ei":     entry.get("formula_ei", 0),
                "formula_fed_tax": entry.get("formula_fed_tax", 0),
                "formula_prov_tax": entry.get("formula_prov_tax", 0),
                "variance_cpp":   entry.get("variance_cpp", 0),
                "variance_ei":    entry.get("variance_ei", 0),
                "variance_fed_tax": entry.get("variance_fed_tax", 0),
                "variance_prov_tax": entry.get("variance_prov_tax", 0),
            }).execute()
        print(f"    {os.path.basename(json_file)}: {len(entries)} entries migrated.")


# ── Step 5: Upload PDFs to payroll-pdfs ───────────────────────────────────────
def upload_pdfs(client, emp_uuid_map):
    print("\n[5] Uploading PDFs to Storage (payroll-pdfs) …")
    storage = client.storage

    def _upload(local_path, storage_path):
        with open(local_path, "rb") as f:
            data = f.read()
        try:
            storage.from_("payroll-pdfs").upload(
                storage_path, data,
                file_options={"content-type": "application/pdf", "upsert": "true"})
            return True
        except Exception as e:
            print(f"      [warn] Upload failed ({storage_path}): {e}")
            return False

    # Track what was uploaded and its new storage path
    path_updates = {}   # local_abs_path → storage_path

    for emp_name, emp_uuid in emp_uuid_map.items():
        emp_dir = os.path.join(EMPLOYEES_DIR, emp_name)
        prefix  = f"employees/{emp_uuid}"

        # Paystubs
        for pdf in glob.glob(os.path.join(emp_dir, "Payroll_*.pdf")):
            sp = f"{prefix}/paystubs/{os.path.basename(pdf)}"
            if _upload(pdf, sp):
                path_updates[os.path.abspath(pdf)] = sp
                path_updates[pdf] = sp  # also map relative

        # T4 slips
        for pdf in glob.glob(os.path.join(emp_dir, "T4", "T4_*.pdf")):
            sp = f"{prefix}/t4/{os.path.basename(pdf)}"
            if _upload(pdf, sp):
                path_updates[os.path.abspath(pdf)] = sp

        # ROE documents
        for pdf in glob.glob(os.path.join(emp_dir, "ROE", "ROE_*.pdf")):
            sp = f"{prefix}/roe/{os.path.basename(pdf)}"
            if _upload(pdf, sp):
                path_updates[os.path.abspath(pdf)] = sp

        # Agreements
        for pdf in glob.glob(os.path.join(emp_dir, "agreements", "*.pdf")):
            sp = f"{prefix}/agreements/{os.path.basename(pdf)}"
            if _upload(pdf, sp):
                path_updates[os.path.abspath(pdf)] = sp

        # Letters
        for pdf in glob.glob(os.path.join(emp_dir, "Letters", "*.pdf")):
            sp = f"{prefix}/letters/{os.path.basename(pdf)}"
            if _upload(pdf, sp):
                path_updates[os.path.abspath(pdf)] = sp

        # Receipts
        for f in glob.glob(os.path.join(emp_dir, "receipts", "*")):
            ext = os.path.splitext(f)[1]
            sp = f"{prefix}/receipts/{os.path.basename(f)}"
            try:
                with open(f, "rb") as fh:
                    data = fh.read()
                ctype = "application/pdf" if ext.lower() == ".pdf" else "image/jpeg"
                storage.from_("payroll-pdfs").upload(
                    sp, data,
                    file_options={"content-type": ctype, "upsert": "true"})
                path_updates[os.path.abspath(f)] = sp
            except Exception as e:
                print(f"      [warn] Receipt upload failed: {e}")

    # T4 Summaries (not employee-specific)
    t4sum_dir = os.path.join(PAYROLL_DIR, "T4_Summaries")
    for pdf in glob.glob(os.path.join(t4sum_dir, "*.pdf")):
        sp = f"t4-summaries/{os.path.basename(pdf)}"
        if _upload(pdf, sp):
            path_updates[os.path.abspath(pdf)] = sp

    total_uploaded = sum(1 for v in path_updates.values() if v)
    print(f"    {total_uploaded} PDF(s) uploaded.")
    return path_updates


# ── Step 6: Update Storage paths in DB ───────────────────────────────────────
def update_storage_paths(client, path_updates):
    print("\n[6] Updating Storage paths in remittances table …")
    if not path_updates:
        print("    No path updates to apply.")
        return

    # Fetch all remittances that have a local pdf_storage_path
    result = (client.table("remittances")
              .select("id,pdf_storage_path")
              .not_.is_("pdf_storage_path", "null")
              .execute())

    updated = 0
    for row in result.data:
        old_path = row.get("pdf_storage_path", "")
        if not old_path:
            continue
        # Normalize: try absolute then as-is
        abs_old = os.path.abspath(old_path) if old_path else ""
        new_path = path_updates.get(abs_old) or path_updates.get(old_path)
        if new_path and new_path != old_path:
            client.table("remittances").update(
                {"pdf_storage_path": new_path}
            ).eq("id", row["id"]).execute()
            updated += 1

    print(f"    {updated} remittance PDF path(s) updated.")

    # Also update agreements table
    agr_result = (client.table("agreements")
                  .select("id,signed_pdf_path")
                  .not_.is_("signed_pdf_path", "null")
                  .execute())
    agr_updated = 0
    for row in agr_result.data:
        old_path = row.get("signed_pdf_path", "")
        abs_old = os.path.abspath(old_path) if old_path else ""
        new_path = path_updates.get(abs_old) or path_updates.get(old_path)
        if new_path and new_path != old_path:
            client.table("agreements").update(
                {"signed_pdf_path": new_path}
            ).eq("id", row["id"]).execute()
            agr_updated += 1
    if agr_updated:
        print(f"    {agr_updated} agreement PDF path(s) updated.")


# ── Step 7: Upload theme assets + CRA templates ───────────────────────────────
def upload_assets(client):
    print("\n[7] Uploading assets to Storage (payroll-assets) …")
    storage = client.storage

    def _upload_asset(local_path, storage_path):
        if not os.path.isfile(local_path):
            return
        with open(local_path, "rb") as f:
            data = f.read()
        try:
            storage.from_("payroll-assets").upload(
                storage_path, data,
                file_options={"content-type": "application/pdf", "upsert": "true"})
            print(f"    Uploaded: {storage_path}")
        except Exception as e:
            print(f"    [warn] Asset upload failed ({storage_path}): {e}")

    # Theme PDFs
    for theme_dir in sorted(glob.glob(os.path.join(ASSETS_DIR, "themes", "*"))):
        theme_name = os.path.basename(theme_dir)
        for asset_name in ["header.pdf", "logo.pdf", "footer.pdf"]:
            local = os.path.join(theme_dir, asset_name)
            _upload_asset(local, f"themes/{theme_name}/{asset_name}")

    # CRA templates
    for template in ["t4_template_2025.pdf", "t4sum_template_2025.pdf"]:
        _upload_asset(os.path.join(ASSETS_DIR, template), template)

    print("    Done.")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("Payroll → Supabase Migration")
    print("=" * 60)

    client = _make_client()

    migrate_employer_config(client)
    emp_uuid_map = migrate_employees(client)
    migrate_remittances(client)
    migrate_audit(client)
    path_updates = upload_pdfs(client, emp_uuid_map)
    update_storage_paths(client, path_updates)
    upload_assets(client)

    print("\n" + "=" * 60)
    print("Migration complete.")
    print(
        "Next steps:\n"
        "  1. Verify data in Supabase dashboard (Tables + Storage)\n"
        "  2. Update .streamlit/secrets.toml with the anon key for the app\n"
        "  3. Deploy to Streamlit Cloud (share.streamlit.io)\n"
        "  4. Add secrets in Streamlit Cloud dashboard\n"
        "  5. Test login + all app functions on the hosted URL"
    )
    print("=" * 60)


if __name__ == "__main__":
    main()
