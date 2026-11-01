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
    v_before   bigint;
    v_after    bigint;
    v_state    text;
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

    -- 4. the merge itself: dry run writes nothing, the real call moves children, hides the
    --    survivor from search but keeps it resolvable, records the decision and is idempotent.
    SELECT count(*) INTO v_before FROM material_event WHERE entity = 'material_merge';
    v_report := material_merge(v_a, v_a2, v_a, 'contract dry run', true, 'contract');
    IF v_report ->> 'code' <> 'dry_run' THEN
        RAISE EXCEPTION 'dry run returned %', v_report -> 'code';
    END IF;
    SELECT count(*) INTO v_after FROM material_event WHERE entity = 'material_merge';
    IF v_before <> v_after THEN
        RAISE EXCEPTION 'dry run wrote % event(s)', v_after - v_before;
    END IF;
    IF (SELECT status FROM material WHERE material_uid = v_a2) <> 'active' THEN
        RAISE EXCEPTION 'dry run superseded the survivor';
    END IF;

    SELECT (material_search_page(p_limit => 1) ->> 'total')::bigint INTO v_before;

    v_report := material_merge(v_a, v_a2, v_a, 'contract merge', false, 'contract');
    IF v_report ->> 'code' <> 'merged' THEN
        RAISE EXCEPTION 'merge returned % : %', v_report -> 'code', v_report;
    END IF;
    SELECT (material_search_page(p_limit => 1) ->> 'total')::bigint INTO v_after;
    IF v_before - v_after <> 1 THEN
        RAISE EXCEPTION 'search total changed by % instead of -1', v_after - v_before;
    END IF;

    SELECT status INTO v_state FROM material WHERE material_uid = v_a;
    IF v_state <> 'active' THEN
        RAISE EXCEPTION 'keeper status is %', v_state;
    END IF;
    SELECT status || '|' || coalesce(superseded_by, '') INTO v_state
      FROM material WHERE material_uid = v_a2;
    IF v_state <> 'superseded|' || v_a THEN
        RAISE EXCEPTION 'survivor is %, expected superseded by %', v_state, v_a;
    END IF;
    IF (SELECT count(*) FROM material_search(p_q => 'fixture dup smoked', p_limit => 50)
         WHERE material_uid = v_a2) <> 0 THEN
        RAISE EXCEPTION 'merged survivor is still in search results';
    END IF;
    IF (SELECT material_detail(v_a2) ->> 'status') <> 'superseded' THEN
        RAISE EXCEPTION 'merged survivor no longer resolves through material_detail';
    END IF;

    -- the survivor's children must now answer for the keeper
    IF (SELECT count(*) FROM material_identifier WHERE material_uid = v_a) < 2 THEN
        RAISE EXCEPTION 'identifiers were not moved to the keeper';
    END IF;
    IF (SELECT count(*) FROM material_price WHERE material_uid = v_a) < 2 THEN
        RAISE EXCEPTION 'prices were not moved to the keeper';
    END IF;
    IF (SELECT count(*) FROM material_price WHERE material_uid = v_a2) <> 0 THEN
        RAISE EXCEPTION 'prices were left on the survivor';
    END IF;
    IF (SELECT count(*) FROM material_measure WHERE material_uid = v_a2) <> 0 THEN
        RAISE EXCEPTION 'measures were left on the survivor';
    END IF;
    IF (SELECT count(*) FROM material_measure WHERE material_uid = v_a AND is_preferred) <> 1 THEN
        RAISE EXCEPTION 'keeper must keep exactly one preferred measure per key';
    END IF;

    SELECT status || '|' || keep_uid || '|' || decided_by INTO v_state
      FROM material_duplicate_candidate WHERE uid_a = v_a AND uid_b = v_a2;
    IF v_state <> 'merged|' || v_a || '|contract' THEN
        RAISE EXCEPTION 'candidate row is %, expected merged by the keeper', v_state;
    END IF;
    SELECT count(*) INTO v_after FROM material_event
     WHERE entity = 'material_merge' AND material_uid IN (v_a, v_a2); 
    IF v_after < 2 THEN
        RAISE EXCEPTION 'merge wrote % merge event(s), expected at least 2', v_after;
    END IF;

    v_report := material_merge(v_a, v_a2, v_a, 'retry', false, 'contract');
    IF v_report ->> 'code' <> 'already_merged' THEN
        RAISE EXCEPTION 'retry returned %', v_report -> 'code';
    END IF;

    -- 5. ignoring: records the decision, survives a re-scan, and cannot rewrite a merge
    v_report := material_duplicate_ignore(v_c, v_c2, 'contract ignore', 'contract');
    IF v_report ->> 'code' <> 'ignored' THEN
        RAISE EXCEPTION 'ignore returned %', v_report -> 'code';
    END IF;
    SELECT status || '|' || decided_by INTO v_state
      FROM material_duplicate_candidate
     WHERE uid_a = least(v_c, v_c2) AND uid_b = greatest(v_c, v_c2);
    IF v_state <> 'ignored|contract' THEN
        RAISE EXCEPTION 'ignored row is %', v_state;
    END IF;
    PERFORM material_duplicate_scan();
    SELECT status INTO v_state FROM material_duplicate_candidate
     WHERE uid_a = least(v_c, v_c2) AND uid_b = greatest(v_c, v_c2);
    IF v_state <> 'ignored' THEN
        RAISE EXCEPTION 're-scan changed an ignored row to %', v_state;
    END IF;
    v_report := material_duplicate_ignore(v_a, v_a2, 'too late', 'contract');
    IF v_report ->> 'code' <> 'conflict' THEN
        RAISE EXCEPTION 'ignore on a merged pair returned %', v_report -> 'code';
    END IF;

    RAISE NOTICE 'merge contract OK: % assertions, queue rows %', 5, v_rows2;
END $$;
