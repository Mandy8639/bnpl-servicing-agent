-- Synthetic BNPL ledger. Base tables hold the data; agents read only the v_* views
-- through bnpl.governed, which enforces that rule with a SQLite authorizer.

CREATE TABLE ledger_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE merchants (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    sector       TEXT NOT NULL
                 CHECK (sector IN ('Travel', 'Medical', 'Retail', 'Home Improvement')),
    under_review INTEGER NOT NULL DEFAULT 0 CHECK (under_review IN (0, 1))
);

CREATE TABLE loans (
    id            TEXT PRIMARY KEY,
    merchant_id   TEXT NOT NULL REFERENCES merchants (id),
    customer_ref  TEXT NOT NULL,
    principal     REAL NOT NULL CHECK (principal BETWEEN 50 AND 50000),
    term_months   REAL NOT NULL CHECK (term_months BETWEEN 1.5 AND 84),
    apr           REAL NOT NULL CHECK (apr BETWEEN 0 AND 0.36),
    originated_on TEXT NOT NULL,
    status        TEXT NOT NULL
                  CHECK (status IN ('current', 'overdue', 'in_plan', 'charged_off'))
);

CREATE TABLE payments (
    id        INTEGER PRIMARY KEY,
    loan_id   TEXT NOT NULL REFERENCES loans (id),
    due_date  TEXT NOT NULL,
    paid_date TEXT,
    amount    REAL NOT NULL CHECK (amount > 0),
    status    TEXT NOT NULL CHECK (status IN ('paid', 'late_paid', 'missed', 'scheduled'))
);

CREATE TABLE cases (
    id             TEXT PRIMARY KEY,
    loan_id        TEXT NOT NULL REFERENCES loans (id),
    type           TEXT NOT NULL
                   CHECK (type IN ('missed_payment', 'dispute', 'at_risk', 'duplicate_charge')),
    opened_on      TEXT NOT NULL,
    amount_overdue REAL NOT NULL CHECK (amount_overdue >= 0),
    days_overdue   INTEGER NOT NULL CHECK (days_overdue >= 0),
    risk_flags     TEXT NOT NULL DEFAULT '[]',
    comms_history  TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE decisions (
    id              INTEGER PRIMARY KEY,
    case_id         TEXT NOT NULL,
    proposed_action TEXT NOT NULL,
    guardrail_verdict TEXT NOT NULL CHECK (guardrail_verdict IN ('ALLOW', 'HOLD', 'BLOCK')),
    final_action    TEXT,
    actor           TEXT NOT NULL,
    reason          TEXT NOT NULL,
    timestamp       TEXT NOT NULL
);

CREATE TABLE exceptions (
    id          INTEGER PRIMARY KEY,
    case_id     TEXT,
    kind        TEXT NOT NULL CHECK (kind IN ('HOLD', 'BLOCK', 'PAUSE', 'BREACH', 'CLEAR')),
    reason_code TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    timestamp   TEXT NOT NULL
);

CREATE TABLE merchant_reports (
    id                 INTEGER PRIMARY KEY,
    merchant_id        TEXT NOT NULL,
    period_end         TEXT NOT NULL,
    risk_level         TEXT,
    recommended_action TEXT,
    body               TEXT NOT NULL,
    verdict            TEXT NOT NULL CHECK (verdict IN ('ALLOW', 'HOLD', 'BLOCK')),
    reason_code        TEXT NOT NULL,
    status             TEXT NOT NULL
                       CHECK (status IN ('published', 'held', 'blocked', 'approved', 'rejected')),
    actor              TEXT NOT NULL,
    reason             TEXT NOT NULL DEFAULT '',
    supersedes         INTEGER,
    timestamp          TEXT NOT NULL
);

CREATE TRIGGER merchant_reports_no_update BEFORE UPDATE ON merchant_reports
BEGIN SELECT RAISE(ABORT, 'merchant_reports is append-only'); END;
CREATE TRIGGER merchant_reports_no_delete BEFORE DELETE ON merchant_reports
BEGIN SELECT RAISE(ABORT, 'merchant_reports is append-only'); END;

-- The audit log and the exception inventory are append-only.
CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
CREATE TRIGGER exceptions_no_update BEFORE UPDATE ON exceptions
BEGIN SELECT RAISE(ABORT, 'exceptions is append-only'); END;
CREATE TRIGGER exceptions_no_delete BEFORE DELETE ON exceptions
BEGIN SELECT RAISE(ABORT, 'exceptions is append-only'); END;

CREATE INDEX idx_loans_merchant ON loans (merchant_id);
CREATE INDEX idx_payments_loan ON payments (loan_id);
CREATE INDEX idx_cases_loan ON cases (loan_id);
CREATE INDEX idx_decisions_case ON decisions (case_id);

-- Governed views: the only surface the agent may read.

CREATE VIEW v_ledger_meta AS
SELECT key, value FROM ledger_meta;

CREATE VIEW v_merchants AS
SELECT id, name, sector, under_review FROM merchants;

CREATE VIEW v_loans AS
SELECT id, merchant_id, customer_ref, principal, term_months, apr, originated_on, status
FROM loans;

CREATE VIEW v_payments AS
SELECT id, loan_id, due_date, paid_date, amount, status FROM payments;

CREATE VIEW v_cases AS
SELECT
    c.id             AS case_id,
    c.type           AS case_type,
    c.opened_on,
    c.amount_overdue,
    c.days_overdue,
    c.risk_flags,
    c.comms_history,
    l.id             AS loan_id,
    l.customer_ref,
    l.principal,
    l.term_months,
    l.apr,
    l.originated_on,
    l.status         AS loan_status,
    m.id             AS merchant_id,
    m.name           AS merchant_name,
    m.sector,
    m.under_review   AS merchant_under_review
FROM cases c
JOIN loans l ON l.id = c.loan_id
JOIN merchants m ON m.id = l.merchant_id;

CREATE VIEW v_decisions AS
SELECT id, case_id, proposed_action, guardrail_verdict, final_action, actor, reason, timestamp
FROM decisions;

CREATE VIEW v_decision_watermark AS
SELECT COALESCE(MAX(id), 0) AS last_id FROM decisions;

CREATE VIEW v_exceptions AS
SELECT id, case_id, kind, reason_code, detail, timestamp FROM exceptions;

CREATE VIEW v_merchant_reports AS
SELECT id, merchant_id, period_end, risk_level, recommended_action, body, verdict, reason_code,
       status, actor, reason, supersedes, timestamp
FROM merchant_reports;

-- Portfolio health per merchant: the only input the merchant-report agent reads.
CREATE VIEW v_merchant_metrics AS
SELECT
    m.id AS merchant_id,
    m.name AS merchant_name,
    m.sector,
    m.under_review,
    (SELECT COUNT(*) FROM loans l WHERE l.merchant_id = m.id) AS loans_total,
    (SELECT COUNT(*) FROM loans l WHERE l.merchant_id = m.id AND l.status = 'overdue')
        AS loans_overdue,
    (SELECT COUNT(*) FROM loans l WHERE l.merchant_id = m.id AND l.status = 'charged_off')
        AS loans_charged_off,
    (SELECT COUNT(*) FROM loans l WHERE l.merchant_id = m.id AND l.status = 'in_plan')
        AS loans_in_plan,
    (SELECT COALESCE(SUM(c.amount_overdue), 0) FROM cases c JOIN loans l ON l.id = c.loan_id
        WHERE l.merchant_id = m.id) AS amount_overdue,
    (SELECT COUNT(*) FROM cases c JOIN loans l ON l.id = c.loan_id
        WHERE l.merchant_id = m.id AND c.type = 'dispute') AS open_disputes,
    (SELECT COUNT(*) FROM cases c JOIN loans l ON l.id = c.loan_id
        WHERE l.merchant_id = m.id AND c.risk_flags LIKE '%fraud_flag%') AS fraud_flags,
    (SELECT COUNT(*) FROM exceptions e JOIN cases c ON c.id = e.case_id
        JOIN loans l ON l.id = c.loan_id
        WHERE l.merchant_id = m.id AND e.kind = 'HOLD') AS exceptions_held,
    (SELECT COUNT(*) FROM exceptions e JOIN cases c ON c.id = e.case_id
        JOIN loans l ON l.id = c.loan_id
        WHERE l.merchant_id = m.id AND e.kind = 'BLOCK') AS exceptions_blocked
FROM merchants m;
