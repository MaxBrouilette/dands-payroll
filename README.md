# Decals and Signs — Payroll PDF Generator

## Quick Start
```
cd C:\Users\17809\Desktop\Payroll
python generate_payroll.py
```

## How It Works
1. Open `generate_payroll.py`
2. Edit the `PAYROLL_DATA` dictionary at the top (employee info, hours, deductions, etc.)
3. Run the script — PDF saves to `employees/<Name>/Payroll_<Name>_<PayDate>.pdf`

## What To Edit Per Payroll Run
| Field | Example |
|-------|---------|
| `employee_name` | `"Petro Smetaniuk"` |
| `employee_address` | `["305, 3431-139 Ave", "Edmonton, AB", "T5Y 1Z4"]` |
| `position` | `"Graphic Designer"` |
| `payment_method` | `"Cheque"` or `"E-Transfer"` |
| `period_from` / `period_to` | `"January 28th, 2026"` |
| `payment_date` | `"February 15th, 2026"` |
| `regular_rate` | `23.25` |
| `regular_hours` | `98.5` |
| `cpp`, `ei`, `fed_tax`, `prov_tax` | From payroll guy's numbers |
| `ytd_gross` / `ytd_net` | Running totals (set `None` for first period) |

## Auto-Calculated Fields
- **Current Total** (rate x hours)
- **Total Deductions** (CPP + EI + Fed Tax + Prov Tax)
- **Net Pay** (gross - total deductions)
- **YTD** defaults to current period if set to `None`

## YTD Rules
- Based on **payment date**, not pay period dates
- If a December pay period is paid on Jan 1st, it counts toward the new year's YTD
- Set `ytd_gross` and `ytd_net` to the running totals from all prior periods in the calendar year
- Set both to `None` for the first pay period of the year

## Batch Regeneration
`regenerate_all.py` regenerates all of an employee's payrolls at once — useful when backfilling YTD corrections. Copy the pattern in that file for new employees or bulk runs.

## Branding Assets
Referenced from `C:\Users\17809\Downloads\`:
- `payroll header.pdf` — PAYROLL banner (top of page)
- `payroll footer.pdf` — Decals and Signs footer bar (bottom of page)
- `ds logo payroll - set to 15 percent opacity and center.pdf` — D&S watermark logo (60% width, centered, 15% opacity)

## Folder Structure
```
Payroll/
  generate_payroll.py       ← main script (edit & run)
  regenerate_all.py         ← batch regeneration helper
  README.md
  employees/
    Petro Smetaniuk/
      Payroll_Petro_Smetaniuk_January_1st_2026.pdf
      Payroll_Petro_Smetaniuk_January_15th_2026.pdf
      Payroll_Petro_Smetaniuk_February_1st_2026.pdf
      Payroll_Petro_Smetaniuk_February_15th_2026.pdf
    Enes Cakmak/
      Payroll_Enes_Cakmak_April_26th_2024.pdf
```

## Dependencies
```
pip install reportlab PyMuPDF
```
