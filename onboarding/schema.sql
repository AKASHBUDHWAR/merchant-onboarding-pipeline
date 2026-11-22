-- ============ CORE ============
CREATE TABLE IF NOT EXISTS applications (
  id                    TEXT PRIMARY KEY,
  reference_no          TEXT UNIQUE NOT NULL,          -- human friendly e.g. APP-2026-000123
  channel               TEXT NOT NULL,
  business_type         TEXT NOT NULL,
  business_category     TEXT NOT NULL,
  mcc                   TEXT,
  legal_name            TEXT NOT NULL,
  trade_name            TEXT,
  declared_pan          TEXT,                          -- encrypted
  declared_gstin        TEXT,
  declared_cin          TEXT,
  annual_turnover_inr   INTEGER,
  expected_monthly_gmv_inr INTEGER,
  avg_ticket_inr        INTEGER,
  website_url           TEXT,
  registered_address    TEXT NOT NULL,                -- {line1,line2,city,state,state_code,pincode,country}
  operating_address     TEXT,
  contact               TEXT NOT NULL,                -- {name,email,phone,designation}
  state                 TEXT NOT NULL,                 -- master state (§6)
  sub_state             TEXT,                          -- optional finer state
  risk_band             TEXT,
  risk_score            INTEGER,
  priority              TEXT NOT NULL DEFAULT 'MEDIUM',
  info_request_round    INTEGER NOT NULL DEFAULT 0,
  duplicate_of          TEXT REFERENCES applications(id),
  merchant_id           TEXT,                          -- set after MID creation
  submitted_at          REAL,
  decided_at            REAL,
  state_entered_at      REAL NOT NULL,
  version               INTEGER NOT NULL DEFAULT 0,    -- optimistic lock
  created_at            REAL NOT NULL DEFAULT (unixepoch()),
  updated_at            REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS persons (                                   -- proprietor / partners / directors / signatories / UBOs
  id               TEXT PRIMARY KEY,
  application_id   TEXT NOT NULL REFERENCES applications(id),
  role             TEXT NOT NULL,      -- PROPRIETOR | PARTNER | DIRECTOR | AUTHORISED_SIGNATORY | UBO | TRUSTEE
  full_name        TEXT NOT NULL,
  pan              TEXT,               -- encrypted
  dob              DATE,               -- encrypted
  ownership_pct    NUMERIC(5,2),
  is_pep           BOOLEAN,
  aadhaar_last4    TEXT,
  email            TEXT,
  phone            TEXT
);

CREATE TABLE IF NOT EXISTS bank_accounts (
  id                    TEXT PRIMARY KEY,
  application_id        TEXT NOT NULL REFERENCES applications(id),
  account_holder_name   TEXT NOT NULL,
  account_number_enc    TEXT NOT NULL,
  account_number_hash   TEXT NOT NULL,                 -- sha256(salt+acct) for dedupe
  account_number_last4  TEXT NOT NULL,
  ifsc                  TEXT NOT NULL,
  bank_name             TEXT,
  account_type          TEXT,                          -- CURRENT | SAVINGS
  is_active             BOOLEAN NOT NULL DEFAULT TRUE, -- false when superseded by a change
  superseded_by         TEXT REFERENCES bank_accounts(id),
  created_at            REAL NOT NULL DEFAULT (unixepoch())
);

-- ============ DOCUMENTS & EXTRACTION ============
CREATE TABLE IF NOT EXISTS documents (
  id                TEXT PRIMARY KEY,
  application_id    TEXT NOT NULL REFERENCES applications(id),
  doc_type          TEXT NOT NULL,
  declared_doc_type TEXT,               -- what merchant said
  detected_doc_type TEXT,               -- what classifier said
  source            TEXT NOT NULL,      -- FILE | STRUCTURED_PAYLOAD
  storage_uri       TEXT,
  mime_type         TEXT,
  sha256            TEXT,               -- for duplicate file detection
  page_count        INTEGER,
  quality_score     NUMERIC(4,3),       -- 0..1 (blur/glare/crop)
  quality_issues    TEXT,              -- ["BLURRED","CROPPED","GLARE","ROTATED","PASSWORD_PROTECTED"]
  status            TEXT NOT NULL,      -- §7.2
  version           INTEGER NOT NULL DEFAULT 1, -- re-uploads increment
  replaces_document TEXT REFERENCES documents(id),
  uploaded_at       REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS extracted_fields (
  id                TEXT PRIMARY KEY,
  document_id       TEXT NOT NULL REFERENCES documents(id),
  application_id    TEXT NOT NULL REFERENCES applications(id),
  field_name        TEXT NOT NULL,      -- canonical name (§11)
  raw_value         TEXT,
  normalized_value  TEXT,
  confidence        NUMERIC(4,3),
  bbox              TEXT,              -- {page,x,y,w,h} for UI highlight
  status            TEXT NOT NULL,      -- FieldStatus
  validation_errors TEXT,              -- ["PAN_FORMAT_INVALID"]
  extractor         TEXT NOT NULL,      -- model id / "structured"
  overridden_by     TEXT,               -- operator user id
  override_reason   TEXT,
  created_at        REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS field_reconciliation (       -- one row per canonical field across sources
  application_id    TEXT NOT NULL REFERENCES applications(id),
  field_name        TEXT NOT NULL,
  form_value        TEXT,
  doc_values        TEXT,              -- [{document_id, value, confidence}]
  provider_values   TEXT,              -- [{check_type, value}]
  resolved_value    TEXT,
  match_score       NUMERIC(4,3),
  status            TEXT NOT NULL,      -- MATCH | PARTIAL_MATCH | MISMATCH | MISSING
  PRIMARY KEY (application_id, field_name)
);

-- ============ VERIFICATION ============
CREATE TABLE IF NOT EXISTS verification_checks (
  id                 TEXT PRIMARY KEY,
  application_id     TEXT NOT NULL REFERENCES applications(id),
  check_type         TEXT NOT NULL,
  status             TEXT NOT NULL,     -- CheckStatus
  required           BOOLEAN NOT NULL,
  input_snapshot     TEXT NOT NULL,    -- exact inputs sent (masked) -> detects staleness
  input_hash         TEXT NOT NULL,     -- hash of inputs; if app data changes, check -> STALE
  result             TEXT,             -- normalized provider result
  result_code        TEXT,              -- e.g. PAN_VALID, GST_CANCELLED
  attempt_count      INTEGER NOT NULL DEFAULT 0,
  next_attempt_at    REAL,
  deadline_at        REAL,       -- timeout
  waived_by          TEXT,
  waiver_reason      TEXT,
  started_at         REAL,
  completed_at       REAL,
  superseded_by      TEXT REFERENCES verification_checks(id),
  UNIQUE (application_id, check_type, id)
);

CREATE TABLE IF NOT EXISTS provider_requests (
  id                  TEXT PRIMARY KEY,
  check_id            TEXT NOT NULL REFERENCES verification_checks(id),
  provider            TEXT NOT NULL,
  attempt_no          INTEGER NOT NULL,
  idempotency_key     TEXT NOT NULL UNIQUE,   -- sent to provider
  provider_request_id TEXT,                   -- returned by provider
  request_payload     TEXT NOT NULL,         -- masked
  http_status         INTEGER,
  sync_response       TEXT,
  status              TEXT NOT NULL,          -- SENT | ACKED | CALLBACK_RECEIVED | TIMED_OUT | FAILED | LATE_CALLBACK
  sent_at             REAL NOT NULL,
  acked_at            REAL,
  timeout_at          REAL NOT NULL,
  latency_ms          INTEGER
);

CREATE TABLE IF NOT EXISTS inbound_callbacks (            -- inbox; dedupe
  id                  TEXT PRIMARY KEY,
  provider            TEXT NOT NULL,
  provider_event_id   TEXT NOT NULL,
  provider_request_id TEXT,
  signature_valid     BOOLEAN NOT NULL,
  payload             TEXT NOT NULL,
  received_at         REAL NOT NULL DEFAULT (unixepoch()),
  processing_status   TEXT NOT NULL,        -- PROCESSED | DUPLICATE | LATE | ORPHAN | REJECTED_SIGNATURE | FAILED
  processing_note     TEXT,
  UNIQUE (provider, provider_event_id)
);

-- ============ WORKFLOW ============
CREATE TABLE IF NOT EXISTS state_transitions (
  id              INTEGER PRIMARY KEY,
  application_id  TEXT NOT NULL REFERENCES applications(id),
  from_state      TEXT,
  to_state        TEXT NOT NULL,
  trigger_event   TEXT NOT NULL,
  guard_results   TEXT,                -- {"all_required_checks_passed": true, ...}
  actor_type      TEXT NOT NULL,
  actor_id        TEXT,
  reason_codes    TEXT,
  occurred_at     REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS exceptions (
  id              TEXT PRIMARY KEY,
  application_id  TEXT NOT NULL REFERENCES applications(id),
  code            TEXT NOT NULL,        -- §13
  severity        TEXT NOT NULL,        -- INFO | WARN | BLOCKING | CRITICAL
  source          TEXT NOT NULL,        -- EXTRACTION | VALIDATION | PROVIDER | RISK | DEDUPE | SYSTEM
  related_entity  TEXT,                -- {type:"check", id:...}
  details         TEXT,
  llm_summary     TEXT,                 -- human readable explanation
  status          TEXT NOT NULL,        -- OPEN | WAITING_MERCHANT | IN_REVIEW | RESOLVED | WAIVED
  resolution      TEXT,
  resolved_by     TEXT,
  created_at      REAL NOT NULL DEFAULT (unixepoch()),
  resolved_at     REAL
);

CREATE TABLE IF NOT EXISTS info_requests (
  id              TEXT PRIMARY KEY,
  application_id  TEXT NOT NULL REFERENCES applications(id),
  round_no        INTEGER NOT NULL,
  items           TEXT NOT NULL,       -- [{item_id, kind: DOCUMENT|FIELD|CLARIFICATION, doc_type?, field?, message, exception_id}]
  status          TEXT NOT NULL,        -- OPEN | PARTIALLY_FULFILLED | FULFILLED | EXPIRED | CANCELLED
  due_at          REAL NOT NULL,
  created_at      REAL NOT NULL DEFAULT (unixepoch()),
  fulfilled_at    REAL
);

CREATE TABLE IF NOT EXISTS review_tasks (
  id              TEXT PRIMARY KEY,
  application_id  TEXT NOT NULL REFERENCES applications(id),
  task_type       TEXT NOT NULL,        -- §15.1
  priority        TEXT NOT NULL,
  status          TEXT NOT NULL,        -- §7.3
  assigned_to     TEXT,
  exception_ids   TEXT,
  context         TEXT,                -- precomputed evidence bundle for reviewer
  decision        TEXT,                 -- §15.2
  decision_notes  TEXT,
  second_reviewer TEXT,                 -- four-eyes
  sla_due_at      REAL NOT NULL,
  created_at      REAL NOT NULL DEFAULT (unixepoch()),
  decided_at      REAL
);

CREATE TABLE IF NOT EXISTS merchant_accounts (
  id                TEXT PRIMARY KEY,
  application_id    TEXT NOT NULL UNIQUE REFERENCES applications(id),
  merchant_id       TEXT UNIQUE,         -- MID
  provisioning_status TEXT NOT NULL,     -- §7.4
  capabilities      TEXT,               -- [{name, status, error}]
  attempts          INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  created_at        REAL NOT NULL DEFAULT (unixepoch()),
  activated_at      REAL
);

CREATE TABLE IF NOT EXISTS change_requests (            -- post-verification modifications (e.g. bank change)
  id              TEXT PRIMARY KEY,
  application_id  TEXT NOT NULL REFERENCES applications(id),
  change_type     TEXT NOT NULL,          -- BANK_ACCOUNT_CHANGED | LEGAL_NAME_CHANGED | ...
  old_value       TEXT,                  -- masked
  new_value       TEXT,                  -- masked
  requested_by    TEXT NOT NULL,
  status          TEXT NOT NULL,          -- PENDING_REVERIFICATION | APPROVED | REJECTED
  checks_reopened TEXT,
  created_at      REAL NOT NULL DEFAULT (unixepoch())
);

-- ============ RELIABILITY ============
CREATE TABLE IF NOT EXISTS outbox_events (
  id            INTEGER PRIMARY KEY,
  aggregate_id  TEXT NOT NULL,
  event_type    TEXT NOT NULL,
  payload       TEXT NOT NULL,
  published_at  REAL,
  created_at    REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS dead_letters (
  id            TEXT PRIMARY KEY,
  source        TEXT NOT NULL,           -- PROVIDER_CALL | CALLBACK_PROCESSING | EXTRACTION | PROVISIONING
  reference_id  TEXT NOT NULL,
  payload       TEXT NOT NULL,
  error         TEXT NOT NULL,
  attempts      INTEGER NOT NULL,
  status        TEXT NOT NULL,           -- PARKED | REPLAYED | DISCARDED
  created_at    REAL NOT NULL DEFAULT (unixepoch()),
  replayed_at   REAL
);

CREATE TABLE IF NOT EXISTS scheduled_jobs (            -- timeouts, retries, reminders, SLA breach checks
  id            TEXT PRIMARY KEY,
  job_type      TEXT NOT NULL,           -- CHECK_TIMEOUT | RETRY_PROVIDER | INFO_REMINDER | INFO_EXPIRY | REVIEW_SLA
  reference_id  TEXT NOT NULL,
  run_at        REAL NOT NULL,
  status        TEXT NOT NULL,           -- SCHEDULED | DONE | CANCELLED
  payload       TEXT
);

-- ============ AUDIT ============
CREATE TABLE IF NOT EXISTS audit_log (                  -- append only; hash-chained
  id            INTEGER PRIMARY KEY,
  application_id TEXT,
  actor_type    TEXT NOT NULL,
  actor_id      TEXT,
  action        TEXT NOT NULL,            -- e.g. FIELD_OVERRIDDEN, CHECK_WAIVED, PII_VIEWED, STATE_CHANGED
  target        TEXT,
  before        TEXT,                    -- masked
  after         TEXT,                    -- masked
  ip            TEXT,
  prev_hash     TEXT,
  hash          TEXT NOT NULL,            -- sha256(prev_hash || row)
  occurred_at   REAL NOT NULL DEFAULT (unixepoch())
);

-- ============ INDEXES ============
CREATE INDEX IF NOT EXISTS ix_app_state ON applications(state, state_entered_at);
CREATE INDEX IF NOT EXISTS ix_app_pan ON applications(declared_pan);
CREATE INDEX IF NOT EXISTS ix_app_gstin ON applications(declared_gstin);
CREATE INDEX IF NOT EXISTS ix_bank_hash ON bank_accounts(account_number_hash);
CREATE INDEX IF NOT EXISTS ix_checks_app ON verification_checks(application_id, check_type);
CREATE INDEX IF NOT EXISTS ix_review_queue ON review_tasks(status, priority, sla_due_at);
CREATE INDEX IF NOT EXISTS ix_jobs_due ON scheduled_jobs(status, run_at);


CREATE TABLE IF NOT EXISTS application_payloads (
  application_id TEXT PRIMARY KEY REFERENCES applications(id), owner TEXT NOT NULL,
  data_enc TEXT NOT NULL, simulation TEXT NOT NULL, resume_state TEXT, held_at REAL, pan_hash TEXT
);
CREATE INDEX IF NOT EXISTS ix_pan_fingerprint ON application_payloads(pan_hash);
CREATE TABLE IF NOT EXISTS document_payloads (
  document_id TEXT PRIMARY KEY REFERENCES documents(id), payload_enc TEXT NOT NULL, simulation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_idempotency (
  scope TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, reply_enc TEXT NOT NULL,
  PRIMARY KEY(scope, key)
);
CREATE TABLE IF NOT EXISTS notification_outbox (
  id INTEGER PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id),
  event TEXT NOT NULL, message TEXT NOT NULL, occurred_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS callback_counts (status TEXT PRIMARY KEY, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS review_proposals (
  task_id TEXT PRIMARY KEY REFERENCES review_tasks(id), actor TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_health (
  provider TEXT PRIMARY KEY, failures INTEGER NOT NULL, open_until REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_provider_callback ON provider_requests(provider, provider_request_id);
