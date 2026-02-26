-- ============================================================
--  D&S Payroll — Initial Schema
--  All 12 tables for the Supabase migration
-- ============================================================

-- ── Employees ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS employees (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_name           TEXT UNIQUE NOT NULL,
  employee_id             TEXT,
  position                TEXT,
  payment_method          TEXT,
  schedule_type           TEXT DEFAULT 'standard',
  regular_rate            NUMERIC(10,2),
  sin                     TEXT,
  dob                     DATE,
  phone                   TEXT,
  email                   TEXT,
  employment_type         TEXT DEFAULT 'Full-Time',
  province                TEXT DEFAULT 'Alberta',
  start_date              DATE NOT NULL DEFAULT CURRENT_DATE,
  end_date                DATE,
  emergency_contact_name  TEXT,
  emergency_contact_phone TEXT,
  bank_institution        TEXT,
  bank_transit            TEXT,
  bank_account            TEXT,
  td1_fed_claim           NUMERIC(10,2) DEFAULT 0,
  td1_prov_claim          NUMERIC(10,2) DEFAULT 0,
  td1_extra_deduction     NUMERIC(10,2) DEFAULT 0,
  td1_exempt              BOOLEAN DEFAULT FALSE,
  employee_address        JSONB DEFAULT '[]',
  part_time               JSONB,
  is_archived             BOOLEAN DEFAULT FALSE,
  created_at              TIMESTAMPTZ DEFAULT NOW(),
  updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ── Wage History ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wage_history (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id   UUID REFERENCES employees(id) ON DELETE CASCADE,
  rate          NUMERIC(10,2) NOT NULL,
  effective_date DATE NOT NULL,
  note          TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ── Remittances ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS remittances (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id           UUID REFERENCES employees(id),
  employee_name         TEXT NOT NULL,
  payment_date          TEXT NOT NULL,
  payment_date_iso      DATE,
  pay_year              INT,
  pay_month             INT,
  period_label          TEXT,
  gross                 NUMERIC(10,2),
  cpp_employee          NUMERIC(10,2),
  cpp_employer          NUMERIC(10,2),
  cpp2_employee         NUMERIC(10,2) DEFAULT 0,
  cpp2_employer         NUMERIC(10,2) DEFAULT 0,
  ei_employee           NUMERIC(10,2),
  ei_employer           NUMERIC(10,2),
  fed_tax               NUMERIC(10,2),
  prov_tax              NUMERIC(10,2),
  total_remittance      NUMERIC(10,2),
  hours                 NUMERIC(8,2),
  ei_insurable_gross    NUMERIC(10,2),
  cpp_pensionable_gross NUMERIC(10,2),
  pdf_storage_path      TEXT,
  created_at            TIMESTAMPTZ DEFAULT NOW(),
  deleted_at            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_remittances_year_month
  ON remittances(pay_year, pay_month);
CREATE INDEX IF NOT EXISTS idx_remittances_employee
  ON remittances(employee_name);

-- ── Remittance Status ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS remittance_status (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pay_year       INT NOT NULL,
  pay_month      INT NOT NULL,
  remitted       BOOLEAN DEFAULT FALSE,
  remitted_date  DATE,
  confirmation   TEXT,
  notes          TEXT,
  updated_at     TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(pay_year, pay_month)
);

-- ── Audit Manual Entries ───────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_manual (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id      UUID REFERENCES employees(id),
  employee_name    TEXT NOT NULL,
  payment_date     TEXT NOT NULL,
  payment_date_iso DATE,
  pay_year         INT,
  pay_month        INT,
  period_from      TEXT,
  period_to        TEXT,
  hours            NUMERIC(8,2),
  gross            NUMERIC(10,2),
  actual_cpp       NUMERIC(10,2),
  actual_cpp2      NUMERIC(10,2) DEFAULT 0,
  actual_ei        NUMERIC(10,2),
  actual_fed_tax   NUMERIC(10,2),
  actual_prov_tax  NUMERIC(10,2),
  formula_cpp      NUMERIC(10,2),
  formula_cpp2     NUMERIC(10,2) DEFAULT 0,
  formula_ei       NUMERIC(10,2),
  formula_fed_tax  NUMERIC(10,2),
  formula_prov_tax NUMERIC(10,2),
  variance_cpp     NUMERIC(10,2),
  variance_cpp2    NUMERIC(10,2) DEFAULT 0,
  variance_ei      NUMERIC(10,2),
  variance_fed_tax NUMERIC(10,2),
  variance_prov_tax NUMERIC(10,2),
  created_at       TIMESTAMPTZ DEFAULT NOW(),
  deleted_at       TIMESTAMPTZ
);

-- ── Vacation Payouts ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS vacation_payouts (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id UUID REFERENCES employees(id) ON DELETE CASCADE,
  date        DATE NOT NULL,
  amount      NUMERIC(10,2),
  year_ending DATE,
  method      TEXT,
  note        TEXT,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  deleted_at  TIMESTAMPTZ
);

-- ── Vacation Gross Overrides ───────────────────────────────
CREATE TABLE IF NOT EXISTS vacation_gross_overrides (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id UUID REFERENCES employees(id) ON DELETE CASCADE,
  year_key    TEXT NOT NULL,
  gross_amount NUMERIC(10,2),
  UNIQUE(employee_id, year_key)
);

-- ── Resolutions ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS resolutions (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id         UUID REFERENCES employees(id),
  employee_name       TEXT NOT NULL,
  payment_date        TEXT NOT NULL,
  pay_year            INT,
  pay_month           INT,
  variance_amount     NUMERIC(10,2),
  resolution_method   TEXT,
  resolution_amount   NUMERIC(10,2),
  resolution_date     DATE,
  reference_number    TEXT,
  receipt_storage_path TEXT,
  notes               TEXT,
  resolved_at         TIMESTAMPTZ,
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  deleted_at          TIMESTAMPTZ
);

-- ── Agreements ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agreements (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id     UUID REFERENCES employees(id),
  agreement_key   TEXT NOT NULL,
  signed_pdf_path TEXT,
  signed_date     DATE,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(employee_id, agreement_key)
);

-- ── ROE History ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS roe_history (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id      UUID REFERENCES employees(id),
  generation_date  DATE,
  reason_code      TEXT,
  reason           TEXT,
  last_day_paid    DATE,
  total_hours      NUMERIC(8,2),
  total_earnings   NUMERIC(10,2),
  vacation_pay     NUMERIC(10,2),
  pdf_storage_path TEXT,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Trash / Recovery Bin ───────────────────────────────────
CREATE TABLE IF NOT EXISTS trash (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  deleted_at     TIMESTAMPTZ DEFAULT NOW(),
  item_type      TEXT NOT NULL,
  employee_name  TEXT,
  context        JSONB,
  original_entry JSONB NOT NULL
);

-- ── Employer Config ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS employer_config (
  id         INT PRIMARY KEY DEFAULT 1,
  config     JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Insert a blank row so there's always something to update
INSERT INTO employer_config (id, config)
VALUES (1, '{}')
ON CONFLICT (id) DO NOTHING;
