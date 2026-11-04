-- Broker trades reconciliation: PostgreSQL schema.
-- Safe to run repeatedly (IF NOT EXISTS, views are recreated).

-- Landing tables: every delivered row as received (text), append-only. The hand-over point to the engine.
-- Unique on the delivery (file and line, message id ...), which makes every adapter safe to re-run. Not on
-- trade_id: a feed may deliver an id twice, and the engine decides whether that is a resend or an amendment.
CREATE TABLE IF NOT EXISTS internal_trades (
    ingest_id    BIGSERIAL PRIMARY KEY,
    trade_id     TEXT, inserted_at TEXT, created_at TEXT, instrument TEXT, price TEXT,
    volume       TEXT, fee TEXT, currency TEXT, trader TEXT, broker TEXT,
    source_file  TEXT NOT NULL,
    source_row   INT  NOT NULL,
    loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_file, source_row)
);

CREATE TABLE IF NOT EXISTS broker_confirmations (
    ingest_id        BIGSERIAL PRIMARY KEY,
    confirmation_id  TEXT, inserted_at TEXT, created_at TEXT, instrument TEXT, price TEXT,
    volume           TEXT, fee TEXT, currency TEXT, trader TEXT, broker TEXT,
    source_file      TEXT NOT NULL,
    source_row       INT  NOT NULL,
    loaded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_file, source_row)
);

-- Every status change and notice, append-only. event_id is deterministic, so a replay
-- after a restart inserts nothing new (ON CONFLICT DO NOTHING).
CREATE TABLE IF NOT EXISTS recon_status_events (
    event_id          TEXT PRIMARY KEY,
    seq               BIGINT NOT NULL,
    sim_time          TIMESTAMP,
    kind              TEXT NOT NULL,
    side              TEXT,
    record_id         BIGINT,
    status            TEXT,
    prev_status       TEXT,
    severity          TEXT,
    counterparty_ids  BIGINT[],
    link              TEXT,
    pair_key          TEXT,
    what              TEXT,
    why               TEXT,
    next              TEXT,
    note              TEXT,
    resolved_by       TEXT,
    actor             TEXT,
    reason            TEXT,
    late_minutes      INT,
    rule_version      TEXT NOT NULL,
    payload           JSONB NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS recon_status_events_record ON recon_status_events (side, record_id);
CREATE INDEX IF NOT EXISTS recon_status_events_seq ON recon_status_events (seq);

-- Current status of every record (one row each), updated in the same transaction as the events.
CREATE TABLE IF NOT EXISTS recon_current_status (
    side             TEXT   NOT NULL,
    record_id        BIGINT NOT NULL,
    status           TEXT   NOT NULL,
    severity         TEXT,
    link             TEXT,
    partners         BIGINT[],
    group_id         TEXT,
    instrument       TEXT,
    trader           TEXT,
    broker           TEXT,
    created_at       TIMESTAMP,
    inserted_at      TIMESTAMP,
    deadline         TIMESTAMP,
    first_status_at  TIMESTAMP,
    resolved_by      TEXT,
    last_event_id    TEXT,
    what             TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (side, record_id)
);

-- Current links. The partial unique indexes make it impossible, at database level,
-- for one trade or one confirmation to be finally matched twice.
CREATE TABLE IF NOT EXISTS recon_links (
    internal_id      BIGINT NOT NULL,
    confirmation_id  BIGINT NOT NULL,
    link_type        TEXT   NOT NULL CHECK (link_type IN ('hard', 'manual', 'manual_group', 'soft', 'aggregate')),
    group_id         TEXT,
    PRIMARY KEY (internal_id, confirmation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS recon_links_one_final_per_trade
    ON recon_links (internal_id) WHERE link_type IN ('hard', 'manual');
CREATE UNIQUE INDEX IF NOT EXISTS recon_links_one_final_per_confirmation
    ON recon_links (confirmation_id) WHERE link_type IN ('hard', 'manual');
-- Every commit replaces links by confirmation_id; without this it scans the table and slows as data grows.
CREATE INDEX IF NOT EXISTS recon_links_confirmation ON recon_links (confirmation_id);

-- Where processing stands. One row, written in the same transaction as the events.
CREATE TABLE IF NOT EXISTS recon_checkpoint (
    id            INT PRIMARY KEY CHECK (id = 1),
    steps         BIGINT NOT NULL,
    finalized     BOOLEAN NOT NULL,
    rule_version  TEXT NOT NULL,
    input_digest  TEXT,
    mode          TEXT NOT NULL DEFAULT 'replay',
    payload       JSONB NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Live mode: the exact sequence of inputs and clock ticks, so a restart replays it identically.
CREATE TABLE IF NOT EXISTS recon_steps (
    step       BIGINT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('record', 'tick')),
    side       TEXT,
    ingest_id  BIGINT,
    tick_at    TIMESTAMP
);

-- Operations decisions (accept, force-match, note): who, when, what, why.
-- They survive a replay --fresh; a live --fresh forgets them, as they belong to that run's step numbers.
CREATE TABLE IF NOT EXISTS ops_actions (
    action_id    INT PRIMARY KEY,
    action       TEXT NOT NULL,
    after_step   BIGINT NOT NULL,
    actor        TEXT,
    reason       TEXT,
    payload      JSONB NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Live mode: Operations decisions are submitted here and applied by the running engine, which
-- owns the state. The request row records whether it was applied or rejected, and why.
CREATE TABLE IF NOT EXISTS ops_requests (
    request_id    BIGSERIAL PRIMARY KEY,
    action        TEXT NOT NULL,
    payload       JSONB NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'rejected')),
    result        TEXT,
    action_id     INT,
    processed_at  TIMESTAMPTZ
);

-- Rows that failed validation or conflicted with an earlier version of the same id.
CREATE TABLE IF NOT EXISTS quarantine (
    event_id     TEXT PRIMARY KEY,
    side         TEXT,
    source_row   INT,
    raw          JSONB,
    reason       TEXT,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP VIEW IF EXISTS v_open_exceptions;
CREATE VIEW v_open_exceptions AS
SELECT side, record_id, status, severity, partners, instrument, trader, broker, inserted_at,
       deadline, first_status_at, what
FROM recon_current_status
WHERE status IN ('BREAK', 'UNCONFIRMED', 'UNBOOKED', 'POSSIBLE_DUPLICATE')
ORDER BY CASE severity WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 1 ELSE 0 END DESC,
         inserted_at;

DROP VIEW IF EXISTS v_broker_kpi;
CREATE VIEW v_broker_kpi AS
-- Confirmations dated after their receipt (beyond 5 minutes of clock skew) are data errors
-- and are left out of the timeliness figures, as in the text report.
-- 5 minutes and 2 hours repeat the defaults in config.yaml: change them together.
SELECT broker,
       count(*) FILTER (WHERE side = 'confirmation')                                   AS confirmations,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM inserted_at - created_at) / 60)
              FILTER (WHERE side = 'confirmation'
                      AND inserted_at - created_at >= interval '-5 minutes'))::numeric, 1) AS median_confirmation_lag_min,
       round(max(extract(epoch FROM inserted_at - created_at) / 60)
             FILTER (WHERE side = 'confirmation')::numeric, 1)                         AS max_confirmation_lag_min,
       count(*) FILTER (WHERE side = 'confirmation'
                        AND inserted_at - created_at > interval '2 hours')             AS confirmations_over_2h,
       count(*) FILTER (WHERE side = 'confirmation'
                        AND inserted_at - created_at < interval '-5 minutes')          AS confirmations_dated_after_receipt,
       count(*) FILTER (WHERE side = 'confirmation' AND status IN
                        ('BREAK', 'UNBOOKED'))                                         AS open_confirmation_exceptions
FROM recon_current_status
GROUP BY broker
ORDER BY broker;
