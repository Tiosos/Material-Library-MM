-- Contract assertions for the read API, runnable with nothing but psql.
--
-- CI runs this before the HTTP tests (docs/api-v1.md 9): if the SQL payloads are wrong, the
-- reason should be visible in psql output, not inside a JSON diff from the HTTP layer.
-- Every check raises, so ON_ERROR_STOP=1 turns a broken contract into a red build.
--
-- Usage: psql -v ON_ERROR_STOP=1 -q -d <database> -f scripts/api_contract.sql

\set ON_ERROR_STOP on

DO $$
DECLARE
    v_active   bigint;
    v_total    bigint;
    v_level    text;
    v_uid      text;
    v_doc      jsonb;
    v_rows     bigint;
    v_since    bigint;
    v_next     bigint;
    v_export   bigint;
    v_groups   bigint;
    v_raised   boolean;
    v_page     jsonb;
    v_prices   numeric[];
    v_prev     numeric;
    v_piece    numeric;
    v_nulls    integer;
BEGIN
    SELECT count(*) INTO v_active
      FROM material
     WHERE status NOT IN ('archived', 'superseded');

    -- 1. browse total counts collapsed groups by default and rows with p_collapse false
    SELECT count(DISTINCT coalesce(family_id, material_uid)) INTO v_groups
      FROM material
     WHERE status NOT IN ('archived', 'superseded');

    SELECT coalesce((material_search_page(p_limit => 1) ->> 'total')::bigint, 0) INTO v_total;
    IF v_total <> v_groups THEN
        RAISE EXCEPTION 'collapsed browse total % <> groups %', v_total, v_groups;
    END IF;
    SELECT coalesce((material_search_page(p_limit => 1, p_collapse => false) ->> 'total')::bigint, 0) INTO v_total;
    IF v_total <> v_active THEN
        RAISE EXCEPTION 'expanded browse total % <> active materials %', v_total, v_active;
    END IF;

    -- 2. every level name the API documents is reachable from data that exists
    SELECT level INTO v_level FROM material_search(p_q => '', p_limit => 1);
    IF v_level IS DISTINCT FROM 'browse' THEN
        RAISE EXCEPTION 'empty query should browse, got level %', v_level;
    END IF;
    SELECT level INTO v_level FROM material_search(p_q => 'zzzz qqqq', p_limit => 1);
    IF v_level IS NOT NULL THEN
        RAISE EXCEPTION 'a query without hits must return no row, got level %', v_level;
    END IF;

    -- 3. detail is a complete document and an unknown uid is NULL, not an exception
    SELECT material_uid INTO v_uid FROM material ORDER BY material_uid LIMIT 1;
    v_doc := material_detail(v_uid);
    IF v_doc IS NULL OR v_doc ->> 'schema_version' IS DISTINCT FROM '2' THEN
        RAISE EXCEPTION 'detail(%).schema_version is not 2', v_uid;
    END IF;
    IF v_doc ->> 'material_uid' IS DISTINCT FROM v_uid AND v_doc ->> 'material_uid' <> v_uid THEN
        RAISE EXCEPTION 'detail(%) returned another uid', v_uid;
    END IF;
    IF material_detail('mat_doesnotexist') IS NOT NULL THEN
        RAISE EXCEPTION 'detail() invented a document for an unknown uid';
    END IF;

    -- 4. a malformed measure filter is a client error (SQLSTATE 22023), not an empty result
    v_raised := false;
    BEGIN
        PERFORM material_search(p_measures => '{"thickness_mm": {"gte": 18}}'::jsonb);
    EXCEPTION WHEN invalid_parameter_value THEN
        v_raised := true;
    END;
    IF NOT v_raised THEN
        RAISE EXCEPTION 'a measure without a unit must raise invalid_parameter_value';
    END IF;

    -- 5. changes paging advances by page, so a limited poll never skips rows
    SELECT count(*) INTO v_rows FROM material_changes(p_since => 0, p_limit => 2);
    IF v_rows <> 2 AND v_rows <> (SELECT count(*) FROM material) THEN
        RAISE EXCEPTION 'changes paging returned % rows for limit 2', v_rows;
    END IF;
    SELECT coalesce(max(next_since), 0) INTO v_next FROM material_changes(p_since => 0, p_limit => 2);
    SELECT coalesce(max(sync_seq), 0) INTO v_since FROM (
        SELECT sync_seq FROM material ORDER BY sync_seq LIMIT 2
    ) AS first_page;
    IF v_next <> v_since THEN
        RAISE EXCEPTION 'changes next_since % is not the last sync_seq of the page %', v_next, v_since;
    END IF;

    -- 6. export is one document per visible material
    SELECT count(*) INTO v_export FROM material_export(p_include_archived => false);
    IF v_export <> (SELECT count(*) FROM material WHERE status <> 'archived') THEN
        RAISE EXCEPTION 'export returned % documents', v_export;
    END IF;
    SELECT count(*) INTO v_export FROM material_export(p_include_archived => true);
    IF v_export <> (SELECT count(*) FROM material) THEN
        RAISE EXCEPTION 'export(include_archived) returned % documents', v_export;
    END IF;

    -- 7. the query layer: status, numeric bounds, monotonic sort, no lead time
    SELECT material_search_page(p_limit => 1, p_status => 'all',
                                p_collapse => false) INTO v_page;
    IF (v_page->>'total')::bigint <= v_active THEN
        RAISE EXCEPTION 'status=all rows % are not more than active rows %',
            v_page->>'total', v_active;
    END IF;
    SELECT (SELECT count(*) FROM material WHERE status = 'archived') INTO v_rows;
    SELECT material_search_page(p_limit => 1, p_status => 'archived',
                                p_collapse => false) INTO v_page;
    IF v_page->>'status' <> 'archived' OR (v_page->>'total')::bigint <> v_rows THEN
        RAISE EXCEPTION 'status=archived returned % rows, SQL has %',
            v_page->>'total', v_rows;
    END IF;

    -- a bound excludes NULL prices and everything below it
    SELECT (SELECT count(*) FROM material
             WHERE status NOT IN ('archived', 'superseded') AND unit_cost >= 100) INTO v_rows;
    SELECT material_search_page(p_limit => 500, p_collapse => false,
                                p_unit_cost_min => 100) INTO v_page;
    IF (v_page->>'total')::bigint <> v_rows THEN
        RAISE EXCEPTION 'unit_cost_min=100 total % <> SQL count %',
            v_page->>'total', v_rows;
    END IF;
    SELECT min((e->>'unit_cost')::numeric) INTO v_prev
      FROM jsonb_array_elements(v_page->'results') e;
    IF v_prev IS NULL OR v_prev < 100 THEN
        RAISE EXCEPTION 'a row below the cost bound leaked through: %', v_prev;
    END IF;

    -- ascending sort is monotonic and NULLs come last in both directions
    SELECT material_search_page(p_limit => 500, p_collapse => false,
                                p_sort => 'unit_cost') INTO v_page;
    SELECT array_agg((e->>'unit_cost')::numeric) INTO v_prices
      FROM jsonb_array_elements(v_page->'results') e;
    v_prev := -1; v_nulls := 0;
    FOREACH v_piece IN ARRAY v_prices LOOP
        IF v_piece IS NULL THEN
            v_nulls := v_nulls + 1;
        ELSE
            IF v_nulls > 0 THEN
                RAISE EXCEPTION 'a priced row followed a NULL in asc order';
            END IF;
            IF v_piece < v_prev THEN
                RAISE EXCEPTION 'unit_cost asc is not monotonic: % after %', v_piece, v_prev;
            END IF;
            v_prev := v_piece;
        END IF;
    END LOOP;
    IF v_nulls = 0 THEN
        RAISE EXCEPTION 'the snapshot has no unpriced row, NULLS LAST is untested';
    END IF;
    SELECT material_search_page(p_limit => 500, p_collapse => false,
                                p_sort => 'unit_cost', p_order => 'desc') INTO v_page;
    SELECT array_agg((e->>'unit_cost')::numeric) INTO v_prices
      FROM jsonb_array_elements(v_page->'results') e;
    IF (SELECT bool_or(x IS NOT NULL) FROM
          (SELECT unnest(v_prices[(SELECT count(*) FROM unnest(v_prices) p WHERE p IS NOT NULL) + 1:]) x) t) THEN
        RAISE EXCEPTION 'a priced row followed a NULL in desc order';
    END IF;

    -- the result row gained updated_at but lead time is not a material attribute
    SELECT v_page->'results'->0 INTO v_doc;
    IF NOT (v_doc ? 'updated_at') THEN
        RAISE EXCEPTION 'the result row has no updated_at: %', v_doc;
    END IF;
    IF v_doc ? 'lead_time_days' THEN
        RAISE EXCEPTION 'lead_time_days leaked back into the result row: %', v_doc;
    END IF;

    -- unknown enum values and inverted bounds are client errors, not a quiet query
    v_raised := false;
    BEGIN
        PERFORM material_search_page(p_sort => 'price');
    EXCEPTION WHEN invalid_parameter_value THEN v_raised := true;
    END;
    IF NOT v_raised THEN RAISE EXCEPTION 'an unknown sort was accepted'; END IF;
    v_raised := false;
    BEGIN
        PERFORM material_search_page(p_status => 'deleted');
    EXCEPTION WHEN invalid_parameter_value THEN v_raised := true;
    END;
    IF NOT v_raised THEN RAISE EXCEPTION 'an unknown status was accepted'; END IF;
    v_raised := false;
    BEGIN
        PERFORM material_search_page(p_unit_cost_min => 100, p_unit_cost_max => 10);
    EXCEPTION WHEN invalid_parameter_value THEN v_raised := true;
    END;
    IF NOT v_raised THEN RAISE EXCEPTION 'an inverted cost range was accepted'; END IF;

    RAISE NOTICE 'api contract OK: % active materials, browse total %, levels browsable',
        v_active, v_total;
END;
$$;
