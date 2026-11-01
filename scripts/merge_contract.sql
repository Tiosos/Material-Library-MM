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

    RAISE NOTICE 'merge contract OK: % assertions, queue rows %', 2, v_rows2;
END $$;
