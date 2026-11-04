-- Day-to-day Operations queries on the reconciliation tables.
-- Run with:  psql "$RECON_DSN" -f sql/ops_queries.sql
-- Optional:  psql "$RECON_DSN" -v record_id=2596 -f sql/ops_queries.sql

\if :{?record_id}
\else
\set record_id 2596
\endif
\pset footer off

\echo '\n== 1. Pipeline: rows landed vs rows reconciled (a gap means the engine is behind) =='
SELECT 'internal' AS feed,
       (SELECT count(*) FROM internal_trades)                                   AS rows_landed,
       (SELECT count(*) FROM recon_current_status WHERE side = 'internal')      AS records_reconciled
UNION ALL
SELECT 'confirmation',
       (SELECT count(*) FROM broker_confirmations),
       (SELECT count(*) FROM recon_current_status WHERE side = 'confirmation');

\echo '\n== 2. Status overview =='
SELECT status,
       count(*) FILTER (WHERE side = 'internal')     AS trades,
       count(*) FILTER (WHERE side = 'confirmation') AS confirmations
FROM recon_current_status
GROUP BY status
ORDER BY CASE status WHEN 'PROCESSED' THEN 0 WHEN 'BREAK' THEN 1 WHEN 'UNCONFIRMED' THEN 2
                     WHEN 'UNBOOKED' THEN 3 ELSE 4 END;

\echo '\n== 3. Open exceptions, most severe and oldest first =='
SELECT severity, status, side, record_id, partners, instrument, broker,
       to_char(first_status_at, 'MM-DD HH24:MI') AS flagged, left(what, 70) AS what
FROM v_open_exceptions;

\echo '\n== 4. Breaks: which field differs, booked vs confirmed =='
SELECT l.internal_id, l.confirmation_id,
       m ->> 'field'        AS field,
       m ->> 'internal'     AS booked,
       m ->> 'confirmation' AS confirmed,
       m ->> 'severity'     AS severity
FROM recon_links l
CROSS JOIN LATERAL (SELECT payload FROM recon_status_events    -- a later note must not hide the break
                    WHERE side = 'internal' AND record_id = l.internal_id AND kind = 'STATUS'
                    ORDER BY seq DESC LIMIT 1) e
CROSS JOIN LATERAL jsonb_array_elements(e.payload -> 'mismatches') AS m
WHERE l.link_type = 'soft'
ORDER BY CASE m ->> 'severity' WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, l.internal_id;

\echo '\n== 5. Status deadline check: records that got their first status late (expected: none) =='
SELECT side, record_id, inserted_at, deadline, first_status_at
FROM recon_current_status
WHERE first_status_at IS NULL OR first_status_at > deadline;

\echo '\n== 6. Flagged first, resolved later (who was late, and by how much) =='
SELECT side, record_id, prev_status AS was, to_char(sim_time, 'MM-DD HH24:MI') AS resolved_at,
       late_minutes, note
FROM recon_status_events
WHERE kind = 'STATUS' AND status = 'PROCESSED'
  AND prev_status IN ('BREAK', 'UNCONFIRMED', 'UNBOOKED', 'POSSIBLE_DUPLICATE')
ORDER BY sim_time;

\echo '\n== 7. Broker timeliness =='
SELECT * FROM v_broker_kpi;

\echo '\n== 8. End-of-day open records (a break counts both sides), from the daily snapshots =='
SELECT payload -> 'details' ->> 'day'                     AS day,
       jsonb_array_length(payload -> 'details' -> 'open') AS open_records,
       (payload -> 'details' -> 'counts' -> 'internal' ->> 'PROCESSED')::int     AS trades_processed,
       (payload -> 'details' -> 'counts' -> 'confirmation' ->> 'PROCESSED')::int AS confirmations_processed
FROM recon_status_events
WHERE kind = 'SNAPSHOT'
ORDER BY day;

\echo '\n== 9. Audit trail of one record (set with -v record_id=...) =='
SELECT seq, to_char(sim_time, 'MM-DD HH24:MI') AS sim_time, kind, side, status, severity,
       counterparty_ids, resolved_by, actor, left(coalesce(nullif(note, ''), what), 80) AS detail, rule_version
FROM recon_status_events
WHERE record_id = :record_id OR :record_id = ANY (counterparty_ids)
ORDER BY seq;

\echo '\n== 10. Data quality: quarantined rows and notices =='
SELECT event_id, side, source_row, left(reason, 90) AS reason FROM quarantine
UNION ALL
SELECT event_id, side, NULL, left(what, 90) FROM recon_status_events WHERE kind = 'DATA_QUALITY'
ORDER BY 1;
