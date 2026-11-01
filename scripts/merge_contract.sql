-- Contract assertions for the duplicate scan, runnable with nothing but psql.
--
-- CI runs this next to scripts/api_contract.sql (docs/t2-merge.md 12): if the SQL is wrong, the
-- reason should be visible in psql output rather than inside a JSON diff from the HTTP layer.
-- Every check raises, so ON_ERROR_STOP=1 turns a broken contract into a red build.
--
-- Steps 2 and 3 (queue, preview, merge, ignore) append their assertions below; step 1 covers
-- the scan only.
--
-- Usage: psql -v ON_ERROR_STOP=1 -q -d <database> -f scripts/merge_contract.sql

\set ON_ERROR_STOP on

DO $$
DECLARE
    v_a        text := 'mat_FX000000000000000000DXPK01';
    v_a2       text := 'mat_FX000000000000000000DXPK02';
    v_b        text := 'mat_FX000000000000000000DXPK03';
    v_b2       text := 'mat_FX000000000000000000DXPK04';
    v_c        text := 'mat_FX000000000000000000DXPK05';
    v_c2       text := 'mat_FX000000000000000000DXPK06';
    v_report   jsonb;
    v_status   text;
    v_score    numeric;
    v_rows     bigint;
    v_rows2    bigint;
    v_found    boolean;
    v_plan     jsonb;
    v_events   bigint;
    v_events2  bigint;
    v_queue    jsonb;
    v_raised   boolean := false;
    v_row      jsonb;
    v_pair_ok  boolean := false;
BEGIN
    -- 1. the scan finds the designed pair, and nothing the design says it must skip:
    --    the archived pair (B) and the cross-category decoy (C) stay out of the queue.
    v_report := material_duplicate_scan();

    SELECT true INTO v_found
      FROM material_duplicate_candidate
     WHERE uid_a = least(v_a, v_a2)
       AND uid_b = greatest(v_a, v_a2);
    IF NOT v_found THEN
        RAISE EXCEPTION 'scan did not queue the designed duplicate pair';
    END IF;

    SELECT status, score INTO v_status, v_score
      FROM material_duplicate_candidate
     WHERE uid_a = least(v_a, v_a2) AND uid_b = greatest(v_a, v_a2);
    IF v_status <> 'open' THEN
        RAISE EXCEPTION 'designed pair has status % instead of open', v_status;
    END IF;
    IF v_score < 0.80 THEN
        RAISE EXCEPTION 'designed pair scored % (below the 0.80 floor)', v_score;
    END IF;

    IF EXISTS (SELECT 1 FROM material_duplicate_candidate
                WHERE uid_a = least(v_b, v_b2) AND uid_b = greatest(v_b, v_b2)) THEN
        RAISE EXCEPTION 'the scan queued a pair whose other side is archived';
    END IF;
    IF EXISTS (SELECT 1 FROM material_duplicate_candidate
                WHERE uid_a = least(v_c, v_c2) AND uid_b = greatest(v_c, v_c2)) THEN
        RAISE EXCEPTION 'the scan queued a pair whose sides are in different categories';
    END IF;

    -- 2. a second scan refreshes instead of duplicating: the queue length is stable and the
    --    already-open pair is reported as updated, not inserted.
    SELECT count(*) INTO v_rows FROM material_duplicate_candidate;
    v_report := material_duplicate_scan();
    SELECT count(*) INTO v_rows2 FROM material_duplicate_candidate;
    IF v_rows <> v_rows2 THEN
        RAISE EXCEPTION 're-scan changed the queue length (% -> %)', v_rows, v_rows2;
    END IF;
    IF (v_report ->> 'inserted')::int <> 0 THEN
        RAISE EXCEPTION 're-scan inserted % rows instead of updating', v_report ->> 'inserted';
    END IF;
    IF (v_report ->> 'updated')::int < 1 THEN
        RAISE EXCEPTION 're-scan did not report the refreshed pair (updated %)', v_report ->> 'updated';
    END IF;

    -- 3. the preview is the contract: it describes the plan, refuses nothing it can plan, and
    --    writes absolutely nothing while doing so.
    v_plan := material_merge_preview(v_a, v_a2);
    IF v_plan IS NULL THEN
        RAISE EXCEPTION 'preview returned null for a pair that exists';
    END IF;
    IF (v_plan ->> 'keep') NOT IN (v_a, v_a2) THEN
        RAISE EXCEPTION 'preview keep % is not one of the pair', v_plan ->> 'keep';
    END IF;
    IF (v_plan ->> 'suggestion') NOT IN (v_a, v_a2) THEN
        RAISE EXCEPTION 'preview suggestion % is not one of the pair', v_plan ->> 'suggestion';
    END IF;
    IF NOT (v_plan ->> 'can_merge')::boolean THEN
        RAISE EXCEPTION 'designed pair should be mergeable, blockers: %', v_plan -> 'blockers';
    END IF;
    IF (v_plan -> 'fields') = '{}'::jsonb AND (v_plan -> 'children') = '{}'::jsonb
       AND jsonb_array_length(v_plan -> 'dropped') = 0 THEN
        RAISE EXCEPTION 'preview returned an empty plan: %', v_plan;
    END IF;

    -- the archived half of the B pair must not be mergeable, whichever side becomes keeper
    v_plan := material_merge_preview(v_b, v_b2);
    IF v_plan IS NULL THEN
        RAISE EXCEPTION 'preview returned null for the archived pair';
    END IF;
    IF (v_plan ->> 'can_merge')::boolean THEN
        RAISE EXCEPTION 'preview allowed a merge with an archived record';
    END IF;
    IF NOT ((v_plan -> 'blockers') ?| ARRAY['keeper_status', 'survivor_status']) THEN
        RAISE EXCEPTION 'preview did not blame the archived status, blockers: %', v_plan -> 'blockers';
    END IF;

    SELECT count(*) INTO v_events FROM material_event;
    PERFORM material_merge_preview(v_a, v_a2);
    PERFORM material_merge_preview(v_a2, v_a);
    SELECT count(*) INTO v_events2 FROM material_event;
    IF v_events <> v_events2 THEN
        RAISE EXCEPTION 'preview wrote % event(s)', v_events2 - v_events;
    END IF;

    -- the queue defaults to what an operator can still act on, and rejects a nonsense filter
    v_queue := material_duplicate_queue();
    IF (v_queue ->> 'status') <> 'actionable' THEN
        RAISE EXCEPTION 'queue default status is %', v_queue ->> 'status';
    END IF;
    SELECT count(*) INTO v_rows FROM material_duplicate_candidate WHERE status IN ('open', 'ignored');
    IF (v_queue ->> 'total')::bigint <> v_rows THEN
        RAISE EXCEPTION 'queue total % <> actionable candidates %', v_queue ->> 'total', v_rows;
    END IF;
    IF jsonb_array_length(v_queue -> 'rows') <> least(v_rows, 50) THEN
        RAISE EXCEPTION 'queue returned % rows for % candidates', jsonb_array_length(v_queue -> 'rows'), v_rows;
    END IF;
    -- the designed pair must be listed wherever it falls in score order, and its suggestion must
    -- be one of its own two members
    v_queue := material_duplicate_queue('all', 500);
    FOR v_row IN SELECT value FROM jsonb_array_elements(v_queue -> 'rows') LOOP
        IF (v_row ->> 'uid_a') IN (v_a, v_a2) AND (v_row ->> 'uid_b') IN (v_a, v_a2) THEN
            v_pair_ok := true;
            IF (v_row ->> 'keep_suggestion') NOT IN (v_a, v_a2) THEN
                RAISE EXCEPTION 'queue keeper suggestion % is not one of the designed pair', v_row ->> 'keep_suggestion';
            END IF;
        END IF;
    END LOOP;
    IF NOT v_pair_ok THEN
        RAISE EXCEPTION 'queue did not list the designed pair';
    END IF;

    BEGIN
        PERFORM material_duplicate_queue('nonsense');
    EXCEPTION WHEN invalid_parameter_value THEN
        v_raised := true;
    END;
    IF NOT v_raised THEN
        RAISE EXCEPTION 'queue accepted an unknown status filter';
    END IF;

    RAISE NOTICE 'merge contract OK: % assertions, queue rows %', 3, v_rows2;
END $$;
