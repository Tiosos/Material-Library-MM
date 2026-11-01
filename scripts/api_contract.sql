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
    v_raised   boolean;
BEGIN
    SELECT count(*) INTO v_active
      FROM material
     WHERE status NOT IN ('archived', 'superseded');

    -- 1. browse total is the count of visible rows, not the page size
    SELECT coalesce((material_search_page(p_limit => 1) ->> 'total')::bigint, 0) INTO v_total;
    IF v_total <> v_active THEN
        RAISE EXCEPTION 'browse total % <> active materials %', v_total, v_active;
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

    RAISE NOTICE 'api contract OK: % active materials, browse total %, levels browsable',
        v_active, v_total;
END;
$$;
