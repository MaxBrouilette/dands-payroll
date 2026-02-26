"""
CRA Payroll Deduction Estimator — Multi-Year
=============================================
Implements the T4127 payroll deduction formulas for:
  - CPP (Canada Pension Plan)
  - EI  (Employment Insurance)
  - Federal Income Tax
  - Alberta Provincial Tax

Sources (all verified from official CRA T4127 publications):
  - 2024: T4127-01-24e.pdf  (120th Edition, Jan 1 2024)
  - 2025: T4127-01-25e.pdf  (121st Edition, Jan 1 2025)
         T4127-jul-25e.pdf (121st Edition rev, Jul 1 2025)
  - 2026: T4127-01-26e.pdf  (122nd Edition, Jan 1 2026)

Usage:
    from tax_calculator import estimate_deductions
    result = estimate_deductions(gross=1674.00, pay_periods=26, tax_year=2026)

NOTE: These are estimates. Always verify with CRA PDOC or your payroll advisor.
"""

from datetime import date

INF = float("inf")
EI_EMPLOYER_MULTIPLE = 1.4   # constant across all years


# ════════════════════════════════════════════════════════════
#  RATE TABLES — keyed by (year, half)
#  half=1 → Jan–Jun rates,  half=2 → Jul–Dec rates
#  If no mid-year change, both halves are identical.
# ════════════════════════════════════════════════════════════

RATES = {
    # ── 2024 ──────────────────────────────────────────────
    # T4127-01-24e.pdf (120th Edition)
    (2024, 1): {
        "cpp_rate":           0.0595,
        "cpp_exemption":      3500.00,
        "cpp_max_pensionable": 68500.00,
        "cpp_max_annual":     3867.50,
        "ei_rate":            0.0166,
        "ei_max_insurable":   63200.00,
        "ei_max_annual":      1049.12,
        "fed_brackets": [
            (55867,  0.15),
            (111733, 0.205),
            (173205, 0.26),
            (246752, 0.29),
            (INF,    0.33),
        ],
        "fed_lowest_rate":    0.15,
        "fed_bpa":            15705.00,
        "fed_cea":            1433.00,
        "ab_brackets": [
            (148269, 0.10),
            (177922, 0.12),
            (237230, 0.13),
            (355845, 0.14),
            (INF,    0.15),
        ],
        "ab_lowest_rate":     0.10,
        "ab_bpa":             21885.00,
        # CPP2 — second additional contribution (earnings between YMPE and YAMPE)
        "cpp2_rate":          0.04,
        "cpp2_yampe":         73200.00,
        "cpp2_max_annual":    188.00,
    },

    # ── 2025 Jan–Jun ─────────────────────────────────────
    # T4127-01-25e.pdf (121st Edition)
    (2025, 1): {
        "cpp_rate":           0.0595,
        "cpp_exemption":      3500.00,
        "cpp_max_pensionable": 71300.00,
        "cpp_max_annual":     4034.10,
        "ei_rate":            0.0164,
        "ei_max_insurable":   65700.00,
        "ei_max_annual":      1077.48,
        "fed_brackets": [
            (57375,  0.15),
            (114750, 0.205),
            (177882, 0.26),
            (253414, 0.29),
            (INF,    0.33),
        ],
        "fed_lowest_rate":    0.15,
        "fed_bpa":            16129.00,
        "fed_cea":            1471.00,
        "ab_brackets": [
            (151234, 0.10),
            (181481, 0.12),
            (241974, 0.13),
            (362961, 0.14),
            (INF,    0.15),
        ],
        "ab_lowest_rate":     0.10,
        "ab_bpa":             22323.00,
        "cpp2_rate":          0.04,
        "cpp2_yampe":         81200.00,
        "cpp2_max_annual":    396.00,
    },

    # ── 2025 Jul–Dec ─────────────────────────────────────
    # T4127-jul-25e.pdf (121st Edition, revised Jul 1)
    # Federal lowest rate prorated to 14% (was 15% → 14.5% annual)
    # Alberta lowest rate prorated to 6% (was 10% → 8% annual)
    # New AB bracket at $60,000
    (2025, 2): {
        "cpp_rate":           0.0595,
        "cpp_exemption":      3500.00,
        "cpp_max_pensionable": 71300.00,
        "cpp_max_annual":     4034.10,
        "ei_rate":            0.0164,
        "ei_max_insurable":   65700.00,
        "ei_max_annual":      1077.48,
        "fed_brackets": [
            (57375,  0.14),
            (114750, 0.205),
            (177882, 0.26),
            (253414, 0.29),
            (INF,    0.33),
        ],
        "fed_lowest_rate":    0.14,
        "fed_bpa":            16129.00,
        "fed_cea":            1471.00,
        "ab_brackets": [
            (60000,  0.06),
            (151234, 0.10),
            (181481, 0.12),
            (241974, 0.13),
            (362961, 0.14),
            (INF,    0.15),
        ],
        "ab_lowest_rate":     0.06,
        "ab_bpa":             22323.00,
        "cpp2_rate":          0.04,
        "cpp2_yampe":         81200.00,
        "cpp2_max_annual":    396.00,
    },

    # ── 2026 ──────────────────────────────────────────────
    # T4127-01-26e.pdf (122nd Edition)
    (2026, 1): {
        "cpp_rate":           0.0595,
        "cpp_exemption":      3500.00,
        "cpp_max_pensionable": 74600.00,
        "cpp_max_annual":     4230.45,
        "ei_rate":            0.0163,
        "ei_max_insurable":   68900.00,
        "ei_max_annual":      1123.07,
        "fed_brackets": [
            (58523,  0.14),
            (117045, 0.205),
            (181440, 0.26),
            (258482, 0.29),
            (INF,    0.33),
        ],
        "fed_lowest_rate":    0.14,
        "fed_bpa":            16452.00,
        "fed_cea":            1501.00,
        "ab_brackets": [
            (61200,  0.08),
            (154259, 0.10),
            (185111, 0.12),
            (246813, 0.13),
            (370220, 0.14),
            (INF,    0.15),
        ],
        "ab_lowest_rate":     0.08,
        "ab_bpa":             22769.00,
        "cpp2_rate":          0.04,
        "cpp2_yampe":         85000.00,
        "cpp2_max_annual":    416.00,
    },
}

# 2024 had no mid-year change
RATES[(2024, 2)] = RATES[(2024, 1)]
# 2026 (no mid-year change announced yet)
RATES[(2026, 2)] = RATES[(2026, 1)]


def _get_rates(tax_year, pay_month=None):
    """Look up the correct rate table for a given year and month.

    For 2025, uses Jan–Jun rates for months 1-6 and Jul–Dec rates for 7-12.
    For other years, the half doesn't matter (both halves are the same).
    """
    if pay_month is None:
        pay_month = 1  # default to Jan rates
    half = 1 if pay_month <= 6 else 2
    key = (tax_year, half)
    if key not in RATES:
        raise ValueError(
            f"No CRA rate data for {tax_year}. "
            f"Supported years: {sorted(set(y for y, _ in RATES))}"
        )
    return RATES[key]


# ════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════

def _bracket_tax(taxable, brackets):
    """Calculate tax using progressive brackets."""
    tax  = 0.0
    prev = 0.0
    for ceiling, rate in brackets:
        if taxable <= prev:
            break
        chunk = min(taxable, ceiling) - prev
        tax  += chunk * rate
        prev  = ceiling
    return tax


def available_tax_years():
    """Return sorted list of years that have rate data."""
    return sorted(set(y for y, _ in RATES))


# ════════════════════════════════════════════════════════════
#  MAIN FUNCTION
# ════════════════════════════════════════════════════════════

def estimate_deductions(gross, pay_periods=26, province="AB",
                        tax_year=None, pay_month=None,
                        td1_fed_claim=0.0, td1_prov_claim=0.0,
                        td1_extra=0.0, td1_exempt=False):
    """Estimate CRA payroll deductions for a single pay period.

    Args:
        gross:          Gross pay for the period ($)
        pay_periods:    Number of pay periods per year (26 = bi-weekly)
        province:       Province code (only "AB" implemented)
        tax_year:       Tax year for rate lookup (default: current year)
        pay_month:      Month of payment (1-12), affects 2025 mid-year rates
        td1_fed_claim:  Federal TD1 total claim amount (0 = use default BPA)
        td1_prov_claim: Provincial TD1 total claim amount (0 = use default BPA)
        td1_extra:      Additional tax deduction per pay period (TD1 Line 4)
        td1_exempt:     If True, no income tax withheld

    Returns:
        dict with keys: cpp, ei, fed_tax, prov_tax
    """
    if tax_year is None:
        tax_year = date.today().year
    r = _get_rates(tax_year, pay_month)

    # ── CPP ──────────────────────────────────────────────
    period_exemption = r["cpp_exemption"] / pay_periods
    pensionable      = max(0, gross - period_exemption)
    max_per_period   = r["cpp_max_annual"] / pay_periods
    cpp = min(round(pensionable * r["cpp_rate"], 2), round(max_per_period, 2))

    # ── EI ───────────────────────────────────────────────
    max_ei_per_period = r["ei_max_annual"] / pay_periods
    ei = min(round(gross * r["ei_rate"], 2), round(max_ei_per_period, 2))

    # ── CPP2 (second additional — earnings between YMPE and YAMPE) ──
    period_ympe  = r["cpp_max_pensionable"] / pay_periods
    period_yampe = r["cpp2_yampe"] / pay_periods
    cpp2_pensionable = max(0, min(gross, period_yampe) - period_ympe)
    max_cpp2_per_period = r["cpp2_max_annual"] / pay_periods
    cpp2 = min(round(cpp2_pensionable * r["cpp2_rate"], 2),
               round(max_cpp2_per_period, 2))

    # ── Annualize for income tax ─────────────────────────
    annual_gross = gross * pay_periods
    annual_cpp   = cpp * pay_periods
    annual_ei    = ei * pay_periods
    annual_net_income = annual_gross - annual_cpp - annual_ei

    # ── Federal Tax ──────────────────────────────────────
    fed_tax = 0.0
    prov_tax = 0.0
    if td1_exempt:
        # TD1 exempt: no income tax, but CPP/EI still apply
        pass
    else:
        fed_taxable = max(0, annual_net_income)
        fed_tax_gross = _bracket_tax(fed_taxable, r["fed_brackets"])

        # TD1 claim replaces BPA when > 0 (claim already includes BPA)
        _fed_personal = td1_fed_claim if td1_fed_claim > 0 else r["fed_bpa"]
        fed_credits = (_fed_personal + r["fed_cea"] +
                       annual_cpp + annual_ei) * r["fed_lowest_rate"]

        annual_fed_tax = max(0, fed_tax_gross - fed_credits)
        fed_tax = round(annual_fed_tax / pay_periods, 2)

        # ── Provincial Tax (Alberta) ─────────────────────────
        if province == "AB":
            ab_taxable = max(0, annual_net_income)
            ab_tax_gross = _bracket_tax(ab_taxable, r["ab_brackets"])

            _ab_personal = td1_prov_claim if td1_prov_claim > 0 else r["ab_bpa"]
            ab_credits = (_ab_personal + annual_cpp + annual_ei) * r["ab_lowest_rate"]

            annual_ab_tax = max(0, ab_tax_gross - ab_credits)
            prov_tax = round(annual_ab_tax / pay_periods, 2)

        # Additional tax deduction per pay period (TD1 Line 4)
        if td1_extra > 0:
            fed_tax = round(fed_tax + td1_extra, 2)

    # Effective rates for display
    cpp_pct  = r["cpp_rate"] * 100
    cpp2_pct = r["cpp2_rate"] * 100
    ei_pct   = r["ei_rate"] * 100
    fed_eff  = (fed_tax / gross * 100) if gross > 0 else 0.0
    prov_eff = (prov_tax / gross * 100) if gross > 0 else 0.0

    return {
        "cpp":      cpp,
        "cpp2":     cpp2,
        "ei":       ei,
        "fed_tax":  fed_tax,
        "prov_tax": prov_tax,
        "cpp_pct":  cpp_pct,
        "cpp2_pct": cpp2_pct,
        "ei_pct":   ei_pct,
        "fed_eff_pct": fed_eff,
        "prov_eff_pct": prov_eff,
    }


# ════════════════════════════════════════════════════════════
#  EMPLOYER PORTIONS (for remittance)
# ════════════════════════════════════════════════════════════

def employer_portions(cpp_employee, ei_employee, cpp2_employee=0):
    """Calculate employer's matching CPP, CPP2, and EI contributions.

    Returns:
        dict with keys: cpp_employer, cpp2_employer, ei_employer
    """
    return {
        "cpp_employer":  round(cpp_employee, 2),
        "cpp2_employer": round(cpp2_employee, 2),
        "ei_employer":   round(ei_employee * EI_EMPLOYER_MULTIPLE, 2),
    }


# ── Quick test ───────────────────────────────────────────
if __name__ == "__main__":
    gross = 1674.00
    print("=" * 60)
    for year in available_tax_years():
        months = [1, 7] if year == 2025 else [1]
        for month in months:
            label = f"{year}" if month == 1 else f"{year} Jul+"
            r = estimate_deductions(gross, tax_year=year, pay_month=month)
            ded = r["cpp"] + r["cpp2"] + r["ei"] + r["fed_tax"] + r["prov_tax"]
            net = gross - ded
            print(f"CRA {label:>8}  |  CPP ${r['cpp']:>7.2f}  "
                  f"CPP2 ${r['cpp2']:>6.2f}  "
                  f"EI ${r['ei']:>6.2f}  "
                  f"Fed ${r['fed_tax']:>7.2f}  "
                  f"AB ${r['prov_tax']:>6.2f}  "
                  f"| Net ${net:>8.2f}")
    print("=" * 60)
    print(f"\nGross: ${gross:,.2f} bi-weekly")

    print("\n2026 employer portions:")
    r26 = estimate_deductions(gross, tax_year=2026)
    emp = employer_portions(r26["cpp"], r26["ei"], r26["cpp2"])
    for k, v in emp.items():
        print(f"  {k:>15}: ${v:,.2f}")
