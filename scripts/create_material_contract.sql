-- Contract assertions for approval-based material creation (step 1).
--
-- Proposal: docs/ui-create-material.md section 5, step 1. Run this against a
-- FRESH database after loading material.sql, search.sql, api.sql, merge.sql,
-- seed.sql, writeback.sql and create-material.sql. Every check raises, so
-- ON_ERROR_STOP=1 turns a broken create flow into a red build.
--
-- The whole block runs in a transaction that is rolled back: real transitions
-- are exercised (submit, update, decide, reject) but no request or event rows
-- are left behind, so the file can run repeatedly and the integration tests
-- that follow still start clean.
--
-- Usage: psql -q -v ON_ERROR_STOP=1 -d <database> -f scripts/create_material_contract.sql

\set ON_ERROR_STOP on

BEGIN;

-- All assertions live in one DO block so psql prints no intermediate result
-- rows and every failure names the assertion that tripped.
DO $$
DECLARE
    v_report jsonb;
    v_bad    jsonb;
    v_id     bigint;
    v_rej    bigint;
    v_count  integer;
BEGIN
    -- 1. Entry validation: allowlist, required fields, U19 and U20 -----------
    FOREACH v_bad IN ARRAY ARRAY[
        '{"name":"x"}'::jsonb,
        jsonb_build_object('category_code', 'board', 'name', E'  \n '),
        '{"category_code":"not_a_category","name":"x"}'::jsonb,
        '{"category_code":"board","name":"x","unit":"NOPE"}'::jsonb,
        '{"category_code":"board","name":"x","material_uid":"mat_hand_picked"}'::jsonb,
        '{"category_code":"board","name":"x","attributes":[]}'::jsonb,
        ('{"category_code":"board","name":"' || repeat('x', 1001) || '"}')::jsonb
    ]::jsonb[] LOOP
        BEGIN
            PERFORM material_create_request_submit(v_bad, 'x', 'alice');
            RAISE EXCEPTION 'invalid proposed row was accepted: %', v_bad;
        EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
        END;
    END LOOP;

    BEGIN
        PERFORM material_create_request_submit(
            '{"category_code":"board","name":"x"}'::jsonb, repeat('r', 501), 'alice');
        RAISE EXCEPTION 'a 501-character reason was accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
    END;
    BEGIN
        PERFORM material_create_request_submit(
            '{"category_code":"board","name":"x"}'::jsonb, 'x', 'alice', 'other_system');
        RAISE EXCEPTION 'a non-joineryflow target was accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
    END;
    -- exactly 1000 characters is allowed (the U20 boundary)
    v_report := material_create_request_submit(
        ('{"category_code":"board","name":"' || repeat('x', 1000) || '"}')::jsonb,
        'boundary', 'alice');
    IF v_report ->> 'code' <> 'pending' THEN
        RAISE EXCEPTION '1000-character boundary refused: %', v_report;
    END IF;

    -- 2. Submit, in-place update, author and pending guards -----------------
    v_report := material_create_request_submit(
        '{"category_code":"board","name":"First name","unit":"m2"}'::jsonb,
        'first reason', 'alice');
    IF v_report ->> 'code' <> 'pending' THEN RAISE EXCEPTION 'submit: %', v_report; END IF;
    v_id := (v_report ->> 'id')::bigint;

    v_report := material_create_request_update(
        v_id, '{"category_code":"board","name":"Second name","unit":"m2"}'::jsonb,
        'second reason', 'alice');
    IF v_report ->> 'code' <> 'pending' THEN RAISE EXCEPTION 'update: %', v_report; END IF;
    IF (v_report ->> 'id')::bigint <> v_id THEN RAISE EXCEPTION 'update changed the id'; END IF;

    IF NOT EXISTS (SELECT 1 FROM material_create_request
                    WHERE id = v_id AND proposed ->> 'name' = 'Second name'
                      AND reason = 'second reason') THEN
        RAISE EXCEPTION 'update did not persist proposed and reason';
    END IF;

    v_report := material_create_request_update(
        v_id, '{"category_code":"board","name":"Hijack"}'::jsonb, 'x', 'bob');
    IF v_report ->> 'code' <> 'forbidden' THEN RAISE EXCEPTION 'non-author update: %', v_report; END IF;
    BEGIN
        PERFORM material_create_request_update(
            v_id, '{"category_code":"board","name":"Bad unit","unit":"NOPE"}'::jsonb, 'x', 'alice');
        RAISE EXCEPTION 'an invalid update was accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
    END;

    -- 3. Decide: self-approval refused, different actor, reason in event ----
    v_report := material_create_request_decide(v_id, 'approve', 'x', 'alice');
    IF v_report ->> 'code' <> 'self_approval' THEN RAISE EXCEPTION 'self approval: %', v_report; END IF;
    v_report := material_create_request_decide(v_id, 'approve', 'checked against the PDF', 'bob');
    IF v_report ->> 'code' <> 'approved' THEN RAISE EXCEPTION 'approve: %', v_report; END IF;

    IF NOT EXISTS (SELECT 1 FROM material_event
                    WHERE entity = 'material_create_request'
                      AND payload ->> 'request_id' = v_id::text
                      AND payload ->> 'decision' = 'approve'
                      AND payload ->> 'reason' = 'checked against the PDF') THEN
        RAISE EXCEPTION 'decision reason was not written to material_event';
    END IF;
    -- A decided row is immutable even for its author.
    v_report := material_create_request_update(
        v_id, '{"category_code":"board","name":"Too late"}'::jsonb, 'x', 'alice');
    IF v_report ->> 'code' <> 'already_decided' THEN RAISE EXCEPTION 'update after decision: %', v_report; END IF;
    v_report := material_create_request_decide(v_id, 'reject', 'x', 'carol');
    IF v_report ->> 'code' <> 'already_decided' THEN RAISE EXCEPTION 'second decision: %', v_report; END IF;

    -- 4. Reject path and per-transition event counts ------------------------
    v_report := material_create_request_submit(
        '{"category_code":"board","name":"Reject me"}'::jsonb, 'why', 'dana');
    v_rej := (v_report ->> 'id')::bigint;
    v_report := material_create_request_decide(v_rej, 'reject', 'duplicate idea', 'bob');
    IF v_report ->> 'code' <> 'rejected' THEN RAISE EXCEPTION 'reject: %', v_report; END IF;

    SELECT count(*) INTO v_count FROM material_event
     WHERE entity = 'material_create_request'
       AND (payload ->> 'request_id')::bigint IN (v_id, v_rej);
    IF v_count <> 5 THEN  -- submit, update, approve, submit, reject
        RAISE EXCEPTION 'expected 5 events, got %', v_count;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM material_event
                    WHERE entity = 'material_create_request'
                      AND payload ->> 'request_id' = v_rej::text
                      AND payload ->> 'reason' = 'duplicate idea') THEN
        RAISE EXCEPTION 'reject reason was not recorded';
    END IF;

    -- 5. Unknown ids and invalid decision -----------------------------------
    v_report := material_create_request_decide(999999999, 'approve', 'x', 'bob');
    IF v_report ->> 'code' <> 'not_found' THEN RAISE EXCEPTION 'unknown decide: %', v_report; END IF;
    v_report := material_create_request_update(
        999999999, '{"category_code":"board","name":"x"}'::jsonb, 'x', 'alice');
    IF v_report ->> 'code' <> 'not_found' THEN RAISE EXCEPTION 'unknown update: %', v_report; END IF;
    BEGIN
        PERFORM material_create_request_decide(v_id, 'maybe', 'x', 'bob');
        RAISE EXCEPTION 'an invalid decision was accepted';
    EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
    END;

    -- No direct material write: creation happens only in the apply step.
    IF EXISTS (SELECT 1 FROM material
                WHERE name IN ('First name', 'Second name', 'Reject me')) THEN
        RAISE EXCEPTION 'a material was written during submit/update/decide';
    END IF;
END $$;

ROLLBACK;

DO $$
BEGIN
    RAISE NOTICE 'create-material contract OK: allowlist, U19/U20, in-place update with author and pending guards, self-approval, decision reason in the event, per-transition events, and no direct material write';
END $$;
