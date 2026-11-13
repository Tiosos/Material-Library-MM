-- Contract assertions for the T3 write-back state machine, runnable with psql.
--
-- Design: docs/t3-writeback.md sections 5, 6, 11 and 12 (delivery step 4).
-- CI runs this against a FRESH database after loading
-- material.sql, search.sql, merge.sql, seed.sql, writeback.sql and importing
-- the committed snapshot fixtures/joineryflow-hartwood.jsonl. It is the
-- write-side counterpart of scripts/etl_contract.sql: every check raises, so
-- ON_ERROR_STOP=1 turns a broken state machine into a red build.
--
-- merge.sql is required by section 11: the T3 x T2 rule is half-owned by
-- material_merge_preview(), which refuses a merge while a request is open.
--
-- The whole block runs inside a transaction that is rolled back. The contract
-- exercises real transitions (submit, decide, claim, settle, retry) but leaves
-- no request or event rows behind, so the integration tests that follow start
-- from a clean database and the file can be run more than once.
--
-- One order correction versus docs section 5: writeback.sql must load AFTER
-- seed.sql, because material_writeback_catalog.category_code references the
-- material_category rows that seed.sql inserts. Loading it before seed.sql
-- fails on that foreign key.
--
-- Usage: psql -q -v ON_ERROR_STOP=1 -d <database> -f scripts/writeback_contract.sql

\set ON_ERROR_STOP on

BEGIN;

DO $$
DECLARE
    v_uids     text[];
    v_uid      text;
    v_hardware text;
    v_id       bigint;
    v_pending  bigint;
    v_rejected bigint;
    v_failed   bigint;
    v_unknown  bigint;
    v_stale    bigint;
    v_merged   bigint;
    r          jsonb;
    v_claim    jsonb;
    v_events   bigint;
    v_count    bigint;
    v_src      text;
    v_status   text;
    v_attempts bigint;
    v_last     bigint;
    v_applied  timestamptz;
    v_claimed  timestamptz;
    v_cost     numeric;
    v_rev      integer;
    v_null     bigint;
    v_null_uid text;
    v_verify   bigint;
    v_differs  bigint;
    v_adv      integer;
    v_adv_start integer;
    v_wm_id    bigint;
    v_wm_at    timestamptz;
BEGIN
    -- 1. Additive columns, projection tables and the untouched CHECK -----------
    SELECT count(*) INTO v_count FROM information_schema.columns
     WHERE table_name = 'material_change_request'
       AND column_name IN ('base', 'attempts', 'claimed_at', 'claimed_by',
                           'idempotency_key', 'last_status');
    IF v_count <> 6 THEN
        RAISE EXCEPTION 'writeback.sql added % of the six expected columns', v_count;
    END IF;
    SELECT count(*) INTO v_count FROM material_writeback_catalog;
    IF v_count <> 9 THEN
        RAISE EXCEPTION 'material_writeback_catalog has % rows, expected 9', v_count;
    END IF;
    SELECT count(*) INTO v_count FROM material_writeback_field;
    IF v_count <> 7 THEN
        RAISE EXCEPTION 'material_writeback_field has % rows, expected 7', v_count;
    END IF;

    -- The five-state CHECK must not have grown a 'claimed' sixth state (D13).
    BEGIN
        INSERT INTO material_change_request
            (material_uid, target_system, diffs, requested_by, status)
        SELECT material_uid, 'joineryflow', '[]'::jsonb, 'contract', 'claimed'
          FROM material WHERE source_system = 'joineryflow' ORDER BY source_id LIMIT 1;
        RAISE EXCEPTION 'the status CHECK was widened to allow a sixth state';
    EXCEPTION WHEN check_violation THEN
        NULL;  -- expected: claimed_at is the in-flight marker, not a status
    END;

    SELECT prosrc INTO v_src FROM pg_proc
     WHERE proname = 'material_change_request_claim';
    IF v_src IS NULL OR position('SKIP LOCKED' in v_src) = 0 THEN
        RAISE EXCEPTION 'claim() no longer uses FOR UPDATE SKIP LOCKED';
    END IF;

    SELECT array_agg(material_uid ORDER BY source_id) INTO v_uids
      FROM (
            SELECT material_uid, source_id FROM material
             WHERE source_system = 'joineryflow' AND unit_cost IS NOT NULL
             ORDER BY source_id LIMIT 10
           ) t;
    IF coalesce(array_length(v_uids, 1), 0) < 10 THEN
        RAISE EXCEPTION 'the snapshot does not provide ten priced joineryflow rows';
    END IF;
    SELECT material_uid INTO v_hardware FROM material
     WHERE category_code = 'hardware' AND source_system = 'joineryflow'
     ORDER BY source_id LIMIT 1;

    -- 2. Submit: before is computed here, allowlist is enforced here -----------
    -- Record the watermark's starting point, so this contract can be run on a
    -- database that has already applied a change.
    SELECT coalesce((SELECT advances FROM material_writeback_state
                       WHERE target_system = 'joineryflow'), 0) INTO v_adv_start;
    SELECT material_change_request_submit(
               v_uids[1], '[{"path":"unit_cost","after":999.5}]'::jsonb,
               'contract demo', 'requester') INTO r;
    IF NOT (r->>'ok')::boolean OR r->>'code' <> 'submitted' THEN
        RAISE EXCEPTION 'the demo unit_cost diff was rejected: %', r;
    END IF;
    v_id := (r->>'id')::bigint;
    IF (r->'diffs'->0->>'before') IS NULL OR r->'diffs'->0->>'after' <> '999.5' THEN
        RAISE EXCEPTION 'submit did not compute before server-side: %', r->'diffs';
    END IF;
    IF r->'base'->'values'->>'unit_cost' IS NULL THEN
        RAISE EXCEPTION 'submit did not record the field-level base fingerprint';
    END IF;

    SELECT material_change_request_submit(
               v_uids[2], '[{"path":"archived_at","after":null}]'::jsonb,
               NULL, 'requester') INTO r;
    IF r->>'code' <> 'forbidden_path' OR r->>'path' <> 'archived_at' THEN
        RAISE EXCEPTION 'archived_at must be rejected with its path named: %', r;
    END IF;
    SELECT material_change_request_submit(
               v_uids[2], '[{"path":"price.unit_cost","after":1}]'::jsonb,
               NULL, 'requester') INTO r;
    IF r->>'code' <> 'forbidden_path' THEN
        RAISE EXCEPTION 'price.* must be rejected at submit: %', r;
    END IF;
    IF v_hardware IS NOT NULL THEN
        SELECT material_change_request_submit(
                   v_hardware, '[{"path":"attributes.colour","after":"Red"}]'::jsonb,
                   NULL, 'requester') INTO r;
        IF r->>'code' <> 'forbidden_path' THEN
            RAISE EXCEPTION 'an attribute outside its table allowlist was accepted: %', r;
        END IF;
    END IF;
    SELECT material_change_request_submit(
               v_uids[1], '[{"path":"unit_cost","after":1000}]'::jsonb,
               NULL, 'requester') INTO r;
    IF r->>'code' <> 'open_request' THEN
        RAISE EXCEPTION 'a second open request on the same uid+path was accepted: %', r;
    END IF;

    -- 3. pending -> applied is illegal; only a human may decide ---------------
    SELECT material_change_request_settle(v_id, 'applied', 200, NULL, 'worker') INTO r;
    IF r->>'code' <> 'conflict' OR r->>'status' <> 'pending' THEN
        RAISE EXCEPTION 'pending -> applied was not refused: %', r;
    END IF;
    SELECT material_change_request_decide(v_id, 'approve', NULL, 'requester') INTO r;
    IF r->>'code' <> 'self_approval' THEN
        RAISE EXCEPTION 'the submitter was allowed to approve their own request: %', r;
    END IF;

    -- 4. pending -> approved, one event per transition -------------------------
    SELECT material_change_request_decide(v_id, 'approve', 'looks right', 'reviewer') INTO r;
    IF NOT (r->>'ok')::boolean OR r->>'code' <> 'approved' THEN
        RAISE EXCEPTION 'approve by a different actor failed: %', r;
    END IF;
    SELECT count(*) INTO v_events FROM material_event
     WHERE entity = 'material_change_request' AND (payload->>'request_id')::bigint = v_id;
    IF v_events <> 2 THEN
        RAISE EXCEPTION 'expected 2 events after submit+approve, got %', v_events;
    END IF;
    SELECT material_change_request_decide(v_id, 'approve', NULL, 'reviewer') INTO r;
    IF r->>'code' <> 'already_decided' THEN
        RAISE EXCEPTION 'a decided request was decided again: %', r;
    END IF;

    -- 5. Claim writes no event; the key is stable and unique -------------------
    SELECT material_change_request_claim(10, 'worker', interval '5 minutes', v_id) INTO v_claim;
    IF (v_claim->>'claimed')::integer <> 1
       OR (v_claim->'rows'->0->>'attempts') <> '1'
       OR v_claim->'rows'->0->>'idempotency_key' <> 'mlcr-' || v_id::text THEN
        RAISE EXCEPTION 'claim did not produce one row with a stable key: %', v_claim;
    END IF;
    SELECT count(*) INTO v_events FROM material_event
     WHERE entity = 'material_change_request' AND (payload->>'request_id')::bigint = v_id;
    IF v_events <> 2 THEN
        RAISE EXCEPTION 'claim wrote % extra events (claim is bookkeeping)', v_events - 2;
    END IF;
    SELECT count(*) INTO v_count FROM material_change_request
     WHERE idempotency_key IS NOT NULL
       AND idempotency_key <> ('mlcr-' || id::text);
    IF v_count <> 0 THEN
        RAISE EXCEPTION '% idempotency keys do not match their request id', v_count;
    END IF;

    -- 6. A live lease blocks a second claim; an expired one can be reclaimed ---
    SELECT material_change_request_claim(10, 'worker2', interval '5 minutes', v_id) INTO r;
    IF (r->>'claimed')::integer <> 0 THEN
        RAISE EXCEPTION 'a live lease was reclaimed: %', r;
    END IF;
    UPDATE material_change_request
       SET claimed_at = now() - interval '10 minutes' WHERE id = v_id;
    SELECT material_change_request_claim(10, 'worker2', interval '5 minutes', v_id) INTO r;
    IF (r->>'claimed')::integer <> 1 OR (r->'rows'->0->>'attempts') <> '2' THEN
        RAISE EXCEPTION 'an expired lease was not reclaimed with attempts+1: %', r;
    END IF;

    -- 7. approved -> applied, exactly three events ----------------------------
    SELECT material_change_request_settle(v_id, 'applied', 200, NULL, 'worker2') INTO r;
    IF NOT (r->>'ok')::boolean OR r->>'code' <> 'applied' THEN
        RAISE EXCEPTION 'settle applied failed: %', r;
    END IF;
    SELECT status, attempts, last_status, applied_at, claimed_at
      INTO v_status, v_attempts, v_last, v_applied, v_claimed
      FROM material_change_request WHERE id = v_id;
    IF v_status <> 'applied' OR v_attempts <> 2 OR v_last <> 200
       OR v_applied IS NULL OR v_claimed IS NOT NULL THEN
        RAISE EXCEPTION 'applied row state is wrong: status=% attempts=% last=%',
            v_status, v_attempts, v_last;
    END IF;
    SELECT count(*) INTO v_events FROM material_event
     WHERE entity = 'material_change_request' AND (payload->>'request_id')::bigint = v_id;
    IF v_events <> 3 THEN
        RAISE EXCEPTION 'happy path must emit exactly 3 events, got %', v_events;
    END IF;

    -- 8. pending -> rejected, and rejected is terminal ------------------------
    SELECT material_change_request_submit(
               v_uids[3], '[{"path":"unit_cost","after":888}]'::jsonb,
               NULL, 'requester') INTO r;
    v_rejected := (r->>'id')::bigint;
    SELECT material_change_request_decide(v_rejected, 'reject', 'not needed', 'reviewer') INTO r;
    IF r->>'code' <> 'rejected' THEN
        RAISE EXCEPTION 'reject failed: %', r;
    END IF;
    SELECT material_change_request_decide(v_rejected, 'approve', NULL, 'reviewer') INTO r;
    IF r->>'code' <> 'already_decided' THEN
        RAISE EXCEPTION 'rejected is not terminal: %', r;
    END IF;

    -- 9. approved -> failed, both known and unknown ---------------------------
    SELECT material_change_request_submit(
               v_uids[4], '[{"path":"unit_cost","after":777}]'::jsonb,
               NULL, 'requester') INTO r;
    v_failed := (r->>'id')::bigint;
    SELECT material_change_request_decide(v_failed, 'approve', NULL, 'reviewer') INTO r;
    SELECT material_change_request_claim(10, 'worker', interval '5 minutes', v_failed) INTO r;
    SELECT material_change_request_settle(v_failed, 'failed', 400, 'bad request', 'worker') INTO r;
    IF r->>'code' <> 'failed' OR r->>'error' <> 'bad request' THEN
        RAISE EXCEPTION 'a known 4xx was not recorded as failed: %', r;
    END IF;
    SELECT material_change_request_retry(v_failed, 'auditor', 'try again') INTO r;
    IF NOT (r->>'ok')::boolean OR r->>'code' <> 'approved' THEN
        RAISE EXCEPTION 'a known failure could not be retried: %', r;
    END IF;

    SELECT material_change_request_submit(
               v_uids[5], '[{"path":"unit_cost","after":666}]'::jsonb,
               NULL, 'requester') INTO r;
    v_unknown := (r->>'id')::bigint;
    SELECT material_change_request_decide(v_unknown, 'approve', NULL, 'reviewer') INTO r;
    SELECT material_change_request_claim(10, 'worker', interval '5 minutes', v_unknown) INTO r;
    SELECT material_change_request_settle(v_unknown, 'unknown', 503, 'timeout', 'worker') INTO r;
    IF r->>'code' <> 'failed' OR r->>'error' <> 'uncertain_outcome' THEN
        RAISE EXCEPTION 'a 5xx was not recorded as an uncertain outcome: %', r;
    END IF;
    SELECT material_change_request_retry(v_unknown, 'auditor', NULL) INTO r;
    IF r->>'code' <> 'uncertain_outcome' THEN
        RAISE EXCEPTION 'an uncertain outcome was retried automatically: %', r;
    END IF;

    -- 10. Stale approvals are refused at approve and at apply -----------------
    SELECT material_change_request_submit(
               v_uids[6], '[{"path":"unit_cost","after":555}]'::jsonb,
               NULL, 'requester') INTO r;
    v_stale := (r->>'id')::bigint;
    UPDATE material SET unit_cost = 12345 WHERE material_uid = v_uids[6];
    SELECT material_change_request_decide(v_stale, 'approve', NULL, 'reviewer') INTO r;
    IF r->>'code' <> 'stale_base' THEN
        RAISE EXCEPTION 'a stale approval was accepted: %', r;
    END IF;

    SELECT material_change_request_submit(
               v_uids[7], '[{"path":"unit_cost","after":444}]'::jsonb,
               NULL, 'requester') INTO r;
    v_pending := (r->>'id')::bigint;
    SELECT material_change_request_decide(v_pending, 'approve', NULL, 'reviewer') INTO r;
    UPDATE material SET unit_cost = 23456 WHERE material_uid = v_uids[7];
    SELECT material_change_request_claim(10, 'worker', interval '5 minutes', v_pending) INTO r;
    SELECT material_change_request_settle(v_pending, 'applied', 200, NULL, 'worker') INTO r;
    IF r->>'code' <> 'failed' OR r->>'error' <> 'stale_base'
       OR r->>'outcome' <> 'applied_unverified' THEN
        RAISE EXCEPTION 'a stale 2xx was not reported as applied_unverified: %', r;
    END IF;
    -- and it must not have touched the master record (docs 6.2)
    IF (SELECT unit_cost FROM material WHERE material_uid = v_uids[7]) <> 23456 THEN
        RAISE EXCEPTION 'a stale settle still wrote the master record';
    END IF;

    -- 11. T3 x T2: a merge and an in-flight request cannot both be true -------
    -- The two tables meet at material_uid. A merge supersedes the loser but
    -- keeps its field values, so the base fingerprint alone would let a
    -- tombstone be approved and patched upstream. These four guards are the
    -- contract for that (docs/t3-writeback.md 6.1).
    SELECT material_change_request_submit(
               v_uids[8], '[{"path":"unit_cost","after":333}]'::jsonb,
               NULL, 'requester') INTO r;
    IF NOT (r->>'ok')::boolean THEN
        RAISE EXCEPTION 'could not file the T3 x T2 request: %', r;
    END IF;
    v_merged := (r->>'id')::bigint;

    -- (a) T2 refuses the merge while the request is open, and names the request
    SELECT material_merge_preview(v_uids[8], v_uids[9]) INTO r;
    IF r IS NULL OR NOT ((r->'blockers') ? 'open_change_request')
       OR NOT ((r->'blocking_requests') @> to_jsonb(v_merged)) THEN
        RAISE EXCEPTION 'an open request did not block the merge preview: %', r;
    END IF;
    SELECT material_merge(v_uids[8], v_uids[9], v_uids[9], 'contract', true, 'contract') INTO r;
    IF (r->>'ok')::boolean OR r->>'code' <> 'conflict'
       OR NOT ((r->'blockers') ? 'open_change_request') THEN
        RAISE EXCEPTION 'material_merge ignored an open write-back request: %', r;
    END IF;

    -- (b) approving a tombstone is refused, with the material status named
    UPDATE material SET status = 'superseded', superseded_by = v_uids[9]
     WHERE material_uid = v_uids[8];
    SELECT material_change_request_decide(v_merged, 'approve', NULL, 'reviewer') INTO r;
    IF r->>'code' <> 'material_inactive' OR r->>'material_status' <> 'superseded' THEN
        RAISE EXCEPTION 'a superseded material was approved: %', r;
    END IF;
    -- (c) and the plan refuses to build a PATCH for it, which is the guard the
    -- executor turns into settle('failed', error='material_inactive')
    SELECT material_change_request_plan(v_merged) INTO r;
    IF (r->>'ok')::boolean OR r->>'code' <> 'material_inactive' THEN
        RAISE EXCEPTION 'the plan built a PATCH for a tombstone: %', r;
    END IF;

    -- restore, approve, then merge between approve and apply
    UPDATE material SET status = 'active', superseded_by = NULL
     WHERE material_uid = v_uids[8];
    SELECT material_change_request_decide(v_merged, 'approve', NULL, 'reviewer') INTO r;
    IF NOT (r->>'ok')::boolean THEN
        RAISE EXCEPTION 'the restored material could not be approved: %', r;
    END IF;
    SELECT material_change_request_claim(10, 'worker', interval '5 minutes', v_merged) INTO r;
    SELECT unit_cost INTO v_cost FROM material WHERE material_uid = v_uids[8];
    UPDATE material SET status = 'superseded', superseded_by = v_uids[9]
     WHERE material_uid = v_uids[8];
    -- (d) a 2xx that landed on a row merged away mid-flight is not 'applied'
    SELECT material_change_request_settle(v_merged, 'applied', 200, NULL, 'worker') INTO r;
    IF r->>'code' <> 'failed' OR r->>'error' <> 'material_inactive'
       OR r->>'outcome' <> 'applied_unverified' THEN
        RAISE EXCEPTION 'a 2xx on a tombstone was recorded as applied: %', r;
    END IF;
    IF (SELECT unit_cost FROM material WHERE material_uid = v_uids[8]) IS DISTINCT FROM v_cost THEN
        RAISE EXCEPTION 'a tombstone was written to the master record';
    END IF;
    SELECT material_change_request_retry(v_merged, 'auditor', NULL) INTO r;
    IF r->>'code' <> 'material_inactive' THEN
        RAISE EXCEPTION 'a retry re-approved a tombstone: %', r;
    END IF;

    -- the blocker is about open requests only: once none is open, the pair is
    -- mergeable again and the preview stops naming a request
    UPDATE material SET status = 'active', superseded_by = NULL
     WHERE material_uid = v_uids[8];
    SELECT material_merge_preview(v_uids[8], v_uids[9]) INTO r;
    IF (r->'blockers') ? 'open_change_request'
       OR jsonb_array_length(r->'blocking_requests') <> 0 THEN
        RAISE EXCEPTION 'a decided request still blocked the merge: %', r->'blockers';
    END IF;

    -- 12. The plan is a pure preview: no token, forbidden keys ----------------
    SELECT material_change_request_plan(v_id) INTO r;
    IF NOT (r->>'ok')::boolean OR r->>'method' <> 'PATCH'
       OR r->>'url' !~ '^/catalog/[a-z-]+/[0-9]+$'
       OR r->'headers' ? 'Authorization'
       OR r->'body' ? 'archived_at' OR r->'body' ? 'status' THEN
        RAISE EXCEPTION 'the plan is not a safe PATCH preview: %', r;
    END IF;

    -- 13. applied writes the master record (design 6.2) ----------------------
    -- The happy path above applied unit_cost 999.5 to v_uids[1]. The library is
    -- the source of truth, so the value must be there and the diff must not be
    -- filable again.
    SELECT unit_cost, rev INTO v_cost, v_rev
      FROM material WHERE material_uid = v_uids[1];
    IF v_cost IS DISTINCT FROM 999.5::numeric THEN
        RAISE EXCEPTION 'applied did not write the master record (unit_cost=%)', v_cost;
    END IF;
    IF v_rev < 2 THEN
        RAISE EXCEPTION 'the local write did not bump rev (rev=%)', v_rev;
    END IF;
    SELECT material_change_request_submit(
               v_uids[1], '[{"path":"unit_cost","after":999.5}]'::jsonb,
               NULL, 'requester') INTO r;
    IF r->>'code' <> 'no_change' THEN
        RAISE EXCEPTION 'an applied diff could be filed a second time: %', r;
    END IF;

    -- clearing a field is part of the contract and lands as SQL NULL, not as
    -- the string 'null' and not as a no-op
    SELECT material_uid INTO v_null_uid FROM material
     WHERE source_system = 'joineryflow' AND product_type IS NOT NULL
       AND status = 'active'
     ORDER BY source_id LIMIT 1;
    IF v_null_uid IS NULL THEN
        RAISE EXCEPTION 'the snapshot has no joineryflow row with a product_type';
    END IF;
    SELECT material_change_request_submit(
               v_null_uid, '[{"path":"product_type","after":null}]'::jsonb,
               NULL, 'requester') INTO r;
    IF NOT (r->>'ok')::boolean THEN
        RAISE EXCEPTION 'after: null was rejected for product_type: %', r;
    END IF;
    v_null := (r->>'id')::bigint;
    SELECT material_change_request_decide(v_null, 'approve', NULL, 'reviewer') INTO r;
    SELECT material_change_request_claim(10, 'worker', interval '5 minutes', v_null) INTO r;
    SELECT material_change_request_settle(v_null, 'applied', 200, NULL, 'worker') INTO r;
    IF (SELECT product_type FROM material WHERE material_uid = v_null_uid) IS NOT NULL THEN
        RAISE EXCEPTION 'clearing product_type did not write NULL';
    END IF;

    -- 14. the upstream-write watermark survives request retention (docs 6.3) --
    -- Exactly four settles reported a 2xx in this contract: the happy path (7),
    -- the stale 2xx (10), the tombstone 2xx (11) and the null clear (13). The
    -- pending -> applied refusal (3), the 400 (9) and the 503 (9) must not
    -- advance it: a failed or unknown outcome is not an upstream write.
    SELECT advances, last_request_id, last_upstream_write_at
      INTO v_adv, v_wm_id, v_wm_at
      FROM material_writeback_state WHERE target_system = 'joineryflow';
    IF v_adv <> v_adv_start + 4 OR v_wm_id <> v_null OR v_wm_at IS NULL THEN
        RAISE EXCEPTION 'wrong upstream watermark: advances=% (expected %) last_request_id=%',
            v_adv, v_adv_start + 4, v_wm_id;
    END IF;
    -- retention removes terminal request rows, not the watermark
    DELETE FROM material_change_request WHERE status IN ('applied', 'rejected');
    SELECT count(*) INTO v_count FROM material_writeback_state
     WHERE target_system = 'joineryflow';
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'pruning terminal requests removed the watermark';
    END IF;

    -- 15. verify settles a write whose outcome was unknown (docs 6.4) --------
    -- A 503 leaves the request failed / uncertain_outcome and the master row
    -- untouched. Only a GET can tell whether the PATCH landed, so the worker
    -- supplies a verdict and SQL still owns every transition.
    SELECT material_change_request_submit(
               v_uids[8], '[{"path":"unit_cost","after":321}]'::jsonb,
               NULL, 'requester') INTO r;
    v_verify := (r->>'id')::bigint;
    PERFORM material_change_request_decide(v_verify, 'approve', NULL, 'reviewer');
    PERFORM material_change_request_claim(10, 'worker', interval '5 minutes', v_verify);
    PERFORM material_change_request_settle(v_verify, 'unknown', 503, 'gateway', 'worker');
    SELECT status, error INTO v_status, v_src
      FROM material_change_request WHERE id = v_verify;
    IF v_status <> 'failed' OR v_src <> 'uncertain_outcome' THEN
        RAISE EXCEPTION 'a 503 did not leave the request uncertain (%/%)', v_status, v_src;
    END IF;

    -- a non-candidate is refused before any GET is spent on it: a rejected
    -- request and a pending one
    SELECT material_change_request_submit(
               v_uids[10], '[{"path":"unit_cost","after":2}]'::jsonb,
               NULL, 'requester') INTO r;
    v_rejected := (r->>'id')::bigint;
    PERFORM material_change_request_decide(v_rejected, 'reject', NULL, 'reviewer');
    SELECT material_change_request_verify_plan(v_rejected) INTO r;
    IF (r->>'ok')::boolean OR r->>'code' <> 'not_candidate' THEN
        RAISE EXCEPTION 'verify_plan accepted a rejected request: %', r;
    END IF;
    SELECT material_change_request_submit(
               v_uids[9], '[{"path":"unit_cost","after":654}]'::jsonb,
               NULL, 'requester') INTO r;
    v_pending := (r->>'id')::bigint;
    SELECT material_change_request_verify_plan(v_pending) INTO r;
    IF (r->>'ok')::boolean OR r->>'code' <> 'not_candidate' THEN
        RAISE EXCEPTION 'verify_plan accepted a pending request: %', r;
    END IF;

    -- the plan names the GET and the flat upstream values it has to show
    SELECT material_change_request_verify_plan(v_verify) INTO r;
    IF NOT (r->>'ok')::boolean OR coalesce(r->>'url', '') = ''
       OR r->'expected' <> '{"unit_cost": 321}'::jsonb THEN
        RAISE EXCEPTION 'verify_plan is wrong for an uncertain request: %', r;
    END IF;

    -- an unknown verdict is rejected outright
    BEGIN
        PERFORM material_change_request_verify(v_verify, 'maybe', 'auditor', false);
        RAISE EXCEPTION 'verify accepted an unknown verdict';
    EXCEPTION WHEN invalid_parameter_value THEN
        NULL;
    END;

    -- read-only by default: the verdict is reported, nothing moves
    SELECT advances INTO v_adv_start FROM material_writeback_state
     WHERE target_system = 'joineryflow';
    SELECT count(*) INTO v_events FROM material_event
     WHERE entity = 'material_change_request'
       AND (payload->>'request_id')::bigint = v_verify;
    SELECT material_change_request_verify(
               v_verify, 'upstream_matches', 'auditor', false) INTO r;
    IF r->>'code' <> 'verification_only' OR r->>'would' <> 'applied' THEN
        RAISE EXCEPTION 'verify without resolve did not stay read-only: %', r;
    END IF;
    -- an unreachable upstream is never read as "not applied", even with resolve
    SELECT material_change_request_verify(
               v_verify, 'unreachable', 'auditor', true) INTO r;
    IF r->>'code' <> 'verification_only' THEN
        RAISE EXCEPTION 'an unreachable verdict changed the request: %', r;
    END IF;
    SELECT status, error INTO v_status, v_src
      FROM material_change_request WHERE id = v_verify;
    IF v_status <> 'failed' OR v_src <> 'uncertain_outcome' THEN
        RAISE EXCEPTION 'a read-only verify moved the request to %/%', v_status, v_src;
    END IF;
    SELECT count(*) INTO v_count FROM material_event
     WHERE entity = 'material_change_request'
       AND (payload->>'request_id')::bigint = v_verify;
    IF v_count <> v_events THEN
        RAISE EXCEPTION 'a read-only verify wrote % event(s)', v_count - v_events;
    END IF;
    IF (SELECT advances FROM material_writeback_state
         WHERE target_system = 'joineryflow') IS DISTINCT FROM v_adv_start THEN
        RAISE EXCEPTION 'a read-only verify advanced the upstream watermark';
    END IF;

    -- a confirmed match resolves it: the master row catches up, the watermark
    -- advances and exactly one event is written
    SELECT material_change_request_verify(
               v_verify, 'upstream_matches', 'auditor', true) INTO r;
    IF r->>'code' <> 'applied' THEN
        RAISE EXCEPTION 'a confirmed match was not applied: %', r;
    END IF;
    SELECT status, error, applied_at INTO v_status, v_src, v_applied
      FROM material_change_request WHERE id = v_verify;
    IF v_status <> 'applied' OR v_src IS NOT NULL OR v_applied IS NULL THEN
        RAISE EXCEPTION 'verify applied left the row at %/%', v_status, v_src;
    END IF;
    SELECT unit_cost INTO v_cost FROM material WHERE material_uid = v_uids[8];
    IF v_cost IS DISTINCT FROM 321::numeric THEN
        RAISE EXCEPTION 'a verified write did not reach the master row (unit_cost=%)', v_cost;
    END IF;
    IF (SELECT advances FROM material_writeback_state
         WHERE target_system = 'joineryflow') <> v_adv_start + 1 THEN
        RAISE EXCEPTION 'a verified write did not advance the upstream watermark';
    END IF;
    SELECT count(*) INTO v_count FROM material_event
     WHERE entity = 'material_change_request'
       AND (payload->>'request_id')::bigint = v_verify;
    IF v_count <> v_events + 1 THEN
        RAISE EXCEPTION 'verify applied wrote % events, expected 1', v_count - v_events;
    END IF;
    -- a resolved request is no longer a candidate
    SELECT material_change_request_verify_plan(v_verify) INTO r;
    IF (r->>'ok')::boolean OR r->>'code' <> 'not_candidate' THEN
        RAISE EXCEPTION 'verify_plan accepted a verified request: %', r;
    END IF;

    -- a confirmed miss is a known failure, not a success: the master row and
    -- the watermark stay put, and a human may now retry it
    SELECT material_change_request_submit(
               v_uids[10], '[{"path":"unit_cost","after":987}]'::jsonb,
               NULL, 'requester') INTO r;
    v_differs := (r->>'id')::bigint;
    PERFORM material_change_request_decide(v_differs, 'approve', NULL, 'reviewer');
    PERFORM material_change_request_claim(10, 'worker', interval '5 minutes', v_differs);
    PERFORM material_change_request_settle(v_differs, 'unknown', 502, 'gateway', 'worker');
    SELECT unit_cost INTO v_cost FROM material WHERE material_uid = v_uids[10];
    SELECT material_change_request_verify(
               v_differs, 'upstream_differs', 'auditor', true) INTO r;
    IF r->>'code' <> 'verified_not_applied' THEN
        RAISE EXCEPTION 'a mismatch was not recorded as such: %', r;
    END IF;
    SELECT status, error INTO v_status, v_src
      FROM material_change_request WHERE id = v_differs;
    IF v_status <> 'failed' OR v_src <> 'verified_not_applied' THEN
        RAISE EXCEPTION 'a mismatch left the row at %/%', v_status, v_src;
    END IF;
    IF (SELECT unit_cost FROM material WHERE material_uid = v_uids[10])
       IS DISTINCT FROM v_cost THEN
        RAISE EXCEPTION 'a mismatch wrote the master row anyway';
    END IF;
    IF (SELECT advances FROM material_writeback_state
         WHERE target_system = 'joineryflow') <> v_adv_start + 1 THEN
        RAISE EXCEPTION 'a mismatch advanced the upstream watermark';
    END IF;
    SELECT material_change_request_retry(v_differs, 'carol', 'verified absent upstream') INTO r;
    IF r->>'code' <> 'approved' THEN
        RAISE EXCEPTION 'a verified miss could not be retried: %', r;
    END IF;

    RAISE NOTICE 'writeback contract OK: 9 endpoints, 7 fields, 6 additive columns, '
                 'submit/decide/claim/settle/retry transitions, 1 event per transition, '
                 'T3 x T2 guards (merge blocker, decide, plan, settle, retry), '
                 'applied writes the master record and cannot be filed twice, '
                 'the upstream watermark survives request retention, '
                 'verify stays read-only unless resolved and never guesses';
END $$;

ROLLBACK;
