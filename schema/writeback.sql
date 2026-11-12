-- ---------------------------------------------------------------------------
-- schema/writeback.sql - approval-based write-back state machine (T3, step 1)
-- ---------------------------------------------------------------------------
-- Load order: schema/material.sql, schema/search.sql, schema/api.sql,
-- schema/merge.sql, schema/writeback.sql, schema/seed.sql (the api job), or
-- material, search, seed, writeback (the writeback job). Additive only: no
-- column is dropped, and the five-state CHECK on material_change_request in
-- material.sql is untouched (docs/t3-writeback.md D13).
--
-- Step 1 lands the request columns, the mapping tables and submit, decide,
-- plan and queue. Step 2 adds claim, settle and retry: the five-minute lease,
-- FOR UPDATE SKIP LOCKED batch claiming, outcome handling and the explicit
-- human retry. Together they close the state machine in docs/t3-writeback.md
-- section 6; the executor in step 3 only transports the resulting plans.
--
-- T3 x T2 consistency (docs section 6.1): every approval transition and the
-- plan re-check material.status, so a merge that supersedes a row turns its
-- in-flight requests into material_inactive instead of patching a tombstone.
-- The other half of the rule lives in material_merge_preview(), which refuses
-- to merge either side of a pair while a pending or approved request is open.
--
-- Conventions copied from schema/merge.sql: caller mistakes raise SQLSTATE
-- 22023 (the HTTP edge maps them to 400 bad_request); business outcomes come
-- back as {ok, code} so the edge can map them to 404/409 without parsing
-- messages; every transition writes one explicit material_event row and sets
-- ml.actor / ml.source first.
--
-- The two mapping tables below are the SQL-side projection of the
-- write_back section of mapping/joineryflow.yaml. The YAML is the source of
-- truth; scripts/check_joineryflow_mapping.py asserts that this file agrees
-- with it, so the two cannot drift silently.
-- ---------------------------------------------------------------------------

ALTER TABLE material_change_request
    ADD COLUMN IF NOT EXISTS base            jsonb,
    ADD COLUMN IF NOT EXISTS attempts        integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS claimed_at      timestamptz,
    ADD COLUMN IF NOT EXISTS claimed_by      text,
    ADD COLUMN IF NOT EXISTS idempotency_key text,
    ADD COLUMN IF NOT EXISTS last_status     integer;

CREATE UNIQUE INDEX IF NOT EXISTS material_change_request_key_idx
    ON material_change_request (idempotency_key);
CREATE INDEX IF NOT EXISTS material_change_request_claim_idx
    ON material_change_request (status, claimed_at, requested_at);

-- ---------------------------------------------------------------------------
-- material_writeback_state - the upstream-write watermark (docs 6.3)
-- ---------------------------------------------------------------------------
-- One row per target system. settle() advances it on every 2xx, including an
-- applied_unverified one: the PATCH may well have landed upstream even when
-- the local row was deliberately not written (stale base, merged material,
-- local write failure). The importer compares a snapshot's exported_at with
-- this value, so the stale-snapshot guard keeps working after terminal requests
-- have been pruned by retention. 4xx, 5xx and timeouts do not advance it.
CREATE TABLE IF NOT EXISTS material_writeback_state (
    target_system          text PRIMARY KEY,
    last_upstream_write_at timestamptz NOT NULL,
    last_request_id        bigint,
    advances               integer NOT NULL DEFAULT 1
);

-- ---------------------------------------------------------------------------
-- material_writeback_catalog - one row per joineryflow source table
-- ---------------------------------------------------------------------------
-- endpoint and pk_column come from the per-table entries in the mapping
-- (section 1); {slug} in "PATCH /catalog/{slug}/{mid}" is the last segment of
-- endpoint, not the workspace slug, and {mid} is the numeric part of
-- material.source_id (docs/t3-writeback.md 4.2). attribute_keys is that
-- table's attributes list: an attributes.<key> diff path is only allowed when
-- the material's own category maps to a table that declares the key.
CREATE TABLE IF NOT EXISTS material_writeback_catalog (
    table_name     text PRIMARY KEY,
    category_code  text NOT NULL REFERENCES material_category (code),
    endpoint       text NOT NULL UNIQUE,
    pk_column      text NOT NULL,
    attribute_keys text[] NOT NULL DEFAULT '{}'
);

INSERT INTO material_writeback_catalog (table_name, category_code, endpoint, pk_column, attribute_keys) VALUES
    ('board_materials',    'board',    '/catalog/board-materials',    'material_id', ARRAY['colour','substrate','finish','sided','grain','rating','hmr','grain_locked','notes']),
    ('edging_materials',   'edging',   '/catalog/edging-materials',   'material_id', ARRAY['colour','edge_type','glued']),
    ('glass_materials',    'glass',    '/catalog/glass-materials',    'material_id', ARRAY['colour','edge_finish','glass_type','power_point','shaped']),
    ('timber_materials',   'timber',   '/catalog/timber-materials',   'material_id', ARRAY['colour','finish','grain','rating']),
    ('benchtop_materials', 'benchtop', '/catalog/benchtop-materials', 'material_id', ARRAY['colour','underside','edge_details','laminate_supplier','laminate_code','finish','material_type','pieces','joins','cable_entry','shaped','grain_locked','slab_id']),
    ('hardware_materials', 'hardware', '/catalog/hardware-materials', 'material_id', ARRAY['brand','hardware_type']),
    ('appliances',         'appliance','/catalog/appliances',         'material_id', ARRAY['manufacturer','model_number']),
    ('custom_made',        'custom',   '/catalog/custom-made',        'material_id', ARRAY['vendor','vendor_quote_ref','internal_ref']),
    ('equipment_hire',     'hire',     '/catalog/equipment-hire',     'hire_id',     ARRAY['rate','rate_unit','total_cost','contract_ref','project_id','hire_start','hire_end','notes'])
ON CONFLICT (table_name) DO UPDATE
    SET category_code  = EXCLUDED.category_code,
        endpoint       = EXCLUDED.endpoint,
        pk_column      = EXCLUDED.pk_column,
        attribute_keys = EXCLUDED.attribute_keys;

-- ---------------------------------------------------------------------------
-- material_writeback_field - the inverse of write_back.fields
-- ---------------------------------------------------------------------------
-- The YAML writes fields as <upstream column>: <UMR field>; this table stores
-- the executor's direction, UMR path -> upstream column, plus the material
-- column the live value is read from when computing `before`.
CREATE TABLE IF NOT EXISTS material_writeback_field (
    umr_path        text PRIMARY KEY,
    material_column text NOT NULL,
    upstream_column text NOT NULL
);

INSERT INTO material_writeback_field (umr_path, material_column, upstream_column) VALUES
    ('name',           'name',           'description'),
    ('product_type',   'product_type',   'product_type'),
    ('unit',           'unit',           'um'),
    ('unit_cost',      'unit_cost',      'unit_cost'),
    ('lead_time_days', 'lead_time_days', 'default_lead_time_days'),
    ('product_url',    'product_url',    'product_website'),
    ('synonyms',       'synonyms',       'synonyms')
ON CONFLICT (umr_path) DO UPDATE
    SET material_column = EXCLUDED.material_column,
        upstream_column = EXCLUDED.upstream_column;

-- ---------------------------------------------------------------------------
-- ml_writeback_current_values - the live value of each diff path
-- ---------------------------------------------------------------------------
-- Field paths read the material column; attributes.<key> paths read the
-- material.attributes object under the source-named key. A missing attribute
-- key comes back as JSON null, so two recomputations always compare equal.
-- This is the fingerprint that makes stale approvals detectable (docs
-- section 6); it is field-level on purpose, because a no-op import bumps
-- sync_seq through ml_child_touch() without changing any of these values.
CREATE OR REPLACE FUNCTION ml_writeback_current_values(p_uid text, p_paths jsonb)
RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_out jsonb;
BEGIN
    IF p_paths IS NULL OR jsonb_typeof(p_paths) <> 'array' OR jsonb_array_length(p_paths) = 0 THEN
        RETURN '{}'::jsonb;
    END IF;
    SELECT coalesce(jsonb_object_agg(v.path, v.val ORDER BY v.path), '{}'::jsonb)
      INTO v_out
      FROM (
            SELECT e.path AS path,
                   CASE WHEN e.path LIKE 'attributes.%'
                        THEN m.attributes -> regexp_replace(e.path, '^attributes\.', '')
                        ELSE to_jsonb(m.*) -> f.material_column
                   END AS val
              FROM material m
              CROSS JOIN LATERAL jsonb_array_elements_text(p_paths) AS e(path)
              LEFT JOIN material_writeback_field f ON f.umr_path = e.path
             WHERE m.material_uid = p_uid
           ) v;
    RETURN coalesce(v_out, '{}'::jsonb);
END $$;

COMMENT ON FUNCTION ml_writeback_current_values(text, jsonb) IS
    'Live values of the given write-back paths; the field-level approval fingerprint.';

-- ---------------------------------------------------------------------------
-- material_change_request_submit - file a diff request, before computed here
-- ---------------------------------------------------------------------------
-- The caller sends only {path, after}; `before` is always read from the live
-- row, so a caller cannot forge it (docs section 7). Values are also checked
-- against what the master record can store - the unit foreign key, the
-- numeric(14,4) and integer ranges - because settle writes them home (docs
-- 6.2). Structural caller mistakes raise 22023; semantic rejections return
-- {ok, code}:
--   not_found       unknown material_uid
--   conflict        material is archived or superseded
--   forbidden_path  path outside the allowlist, named in the response
--   no_change       the submitted value equals the live value
--   open_request    a pending or approved request already covers the path
CREATE OR REPLACE FUNCTION material_change_request_submit(
    p_uid    text,
    p_diffs  jsonb,
    p_reason text DEFAULT NULL,
    p_actor  text DEFAULT NULL,
    p_target text DEFAULT 'joineryflow'
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    v_actor  text;
    v_row    material%ROWTYPE;
    v_keys   text[];
    v_paths  text[] := '{}';
    v_values jsonb := '{}'::jsonb;
    v_diffs  jsonb := '[]'::jsonb;
    v_item   jsonb;
    v_path   text;
    v_after  jsonb;
    v_before jsonb;
    v_id     bigint;
BEGIN
    IF coalesce(nullif(btrim(p_target), ''), 'joineryflow') <> 'joineryflow' THEN
        RAISE EXCEPTION 'target_system must be joineryflow in v1 (got %)', p_target
            USING ERRCODE = '22023';
    END IF;
    IF p_diffs IS NULL OR jsonb_typeof(p_diffs) <> 'array' OR jsonb_array_length(p_diffs) = 0 THEN
        RAISE EXCEPTION 'diffs must be a non-empty JSON array' USING ERRCODE = '22023';
    END IF;
    IF length(coalesce(p_reason, '')) > 500 THEN
        RAISE EXCEPTION 'reason must be at most 500 characters' USING ERRCODE = '22023';
    END IF;
    v_actor := coalesce(nullif(btrim(p_actor), ''),
                        nullif(current_setting('ml.actor', true), ''),
                        'anonymous');

    -- serialise concurrent submits for the same material (same pattern as material_merge)
    PERFORM 1 FROM material WHERE material_uid = p_uid FOR UPDATE;
    SELECT * INTO v_row FROM material WHERE material_uid = p_uid;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'code', 'not_found', 'material_uid', p_uid);
    END IF;
    IF v_row.status IN ('archived', 'superseded') THEN
        RETURN jsonb_build_object('ok', false, 'code', 'conflict',
                                  'material_uid', p_uid, 'status', v_row.status);
    END IF;

    FOR v_item IN SELECT * FROM jsonb_array_elements(p_diffs) LOOP
        IF jsonb_typeof(v_item) <> 'object'
           OR NOT (v_item ? 'path') OR NOT (v_item ? 'after')
           OR jsonb_typeof(v_item -> 'path') <> 'string'
           OR btrim(v_item ->> 'path') = '' THEN
            RAISE EXCEPTION 'each diff must be an object with a non-empty path and an after value'
                USING ERRCODE = '22023';
        END IF;
        v_path  := btrim(v_item ->> 'path');
        -- JSON null means "clear this field"; normalise it to SQL NULL so
        -- every type rule below reads its own "or null" literally.
        v_after := nullif(v_item -> 'after', 'null'::jsonb);
        IF v_path = ANY (v_paths) THEN
            RAISE EXCEPTION 'duplicate path in diffs: %', v_path USING ERRCODE = '22023';
        END IF;
        v_paths := v_paths || v_path;

        -- allowlist (docs section 7): the seven fields, or attributes.<key> of
        -- the material's own table. Everything else is rejected here, so a
        -- forbidden value can never reach the executor.
        IF v_path LIKE 'attributes.%' THEN
            v_keys := NULL;
            SELECT c.attribute_keys INTO v_keys
              FROM material_writeback_catalog c
             WHERE c.category_code = v_row.category_code;
            IF NOT FOUND
               OR regexp_replace(v_path, '^attributes\.', '') = ''
               OR position('.' IN regexp_replace(v_path, '^attributes\.', '')) > 0
               OR NOT (regexp_replace(v_path, '^attributes\.', '') = ANY (coalesce(v_keys, '{}'::text[]))) THEN
                RETURN jsonb_build_object('ok', false, 'code', 'forbidden_path',
                                          'path', v_path, 'category_code', v_row.category_code);
            END IF;
            IF v_after IS NOT NULL
               AND jsonb_typeof(v_after) NOT IN ('string', 'number', 'boolean') THEN
                RAISE EXCEPTION 'attributes.% must be a scalar or null', v_path
                    USING ERRCODE = '22023';
            END IF;
            v_before := nullif(v_row.attributes -> regexp_replace(v_path, '^attributes\.', ''),
                               'null'::jsonb);
        ELSE
            v_before := NULL;
            v_keys := NULL;
            SELECT ARRAY[f.material_column] INTO v_keys
              FROM material_writeback_field f
             WHERE f.umr_path = v_path;
            IF v_keys IS NULL THEN
                RETURN jsonb_build_object('ok', false, 'code', 'forbidden_path', 'path', v_path);
            END IF;
            -- type rules per path (docs section 7)
            IF v_path = 'name' THEN
                IF v_after IS NULL OR jsonb_typeof(v_after) <> 'string' OR btrim(v_after #>> '{}') = '' THEN
                    RAISE EXCEPTION 'name must be a non-empty string' USING ERRCODE = '22023';
                END IF;
            ELSIF v_path IN ('product_type', 'unit', 'product_url') THEN
                IF v_after IS NOT NULL AND jsonb_typeof(v_after) <> 'string' THEN
                    RAISE EXCEPTION '% must be a string or null', v_path USING ERRCODE = '22023';
                END IF;
                -- unit is a foreign key to the local unit table: a value the
                -- master record cannot store must not be approvable (docs 6.2)
                IF v_path = 'unit' AND v_after IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM unit u WHERE u.code = v_after #>> '{}') THEN
                    RAISE EXCEPTION 'unit % is not a known unit code', v_after #>> '{}'
                        USING ERRCODE = '22023';
                END IF;
            ELSIF v_path = 'unit_cost' THEN
                IF v_after IS NOT NULL AND (jsonb_typeof(v_after) <> 'number'
                                            OR (v_after #>> '{}')::numeric < 0
                                            OR (v_after #>> '{}')::numeric >= 10000000000) THEN
                    RAISE EXCEPTION 'unit_cost must be between 0 and 10^10 or null'
                        USING ERRCODE = '22023';
                END IF;
            ELSIF v_path = 'lead_time_days' THEN
                IF v_after IS NOT NULL AND (jsonb_typeof(v_after) <> 'number'
                                            OR (v_after #>> '{}') !~ '^[0-9]+$'
                                            OR (v_after #>> '{}')::numeric > 2147483647) THEN
                    RAISE EXCEPTION 'lead_time_days must be a non-negative integer or null'
                        USING ERRCODE = '22023';
                END IF;
            ELSIF v_path = 'synonyms' THEN
                IF v_after IS NOT NULL AND (jsonb_typeof(v_after) <> 'array'
                    OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(v_after) s WHERE btrim(s) = '')) THEN
                    RAISE EXCEPTION 'synonyms must be an array of non-empty strings or null' USING ERRCODE = '22023';
                END IF;
            END IF;
            v_before := nullif(to_jsonb(v_row) -> v_keys[1], 'null'::jsonb);
        END IF;

        IF v_before IS NOT DISTINCT FROM v_after THEN
            RETURN jsonb_build_object('ok', false, 'code', 'no_change', 'path', v_path);
        END IF;
        -- jsonb_set returns SQL NULL when new_value is SQL NULL, so a missing
        -- value is normalised to JSON null here and in ml_writeback_current_values;
        -- the two representations must stay byte-equal for the stale check.
        v_values := jsonb_set(v_values, ARRAY[v_path], coalesce(v_before, 'null'::jsonb), true);
        v_diffs  := v_diffs || jsonb_build_object('path', v_path, 'before', v_before, 'after', v_after);
    END LOOP;

    -- one open request per material + path (docs section 10, 409 conflict)
    IF EXISTS (
            SELECT 1
              FROM material_change_request r
              CROSS JOIN LATERAL jsonb_array_elements(r.diffs) d
             WHERE r.material_uid = p_uid
               AND r.status IN ('pending', 'approved')
               AND (d ->> 'path') = ANY (v_paths)
       ) THEN
        RETURN jsonb_build_object('ok', false, 'code', 'open_request', 'path', v_paths[1]);
    END IF;

    PERFORM set_config('ml.actor', v_actor, true);
    PERFORM set_config('ml.source', 'writeback', true);

    INSERT INTO material_change_request
           (material_uid, target_system, diffs, reason, requested_by, base)
    VALUES (p_uid, 'joineryflow', v_diffs, nullif(btrim(coalesce(p_reason, '')), ''), v_actor,
            jsonb_build_object('rev', v_row.rev, 'values', v_values))
    RETURNING id INTO v_id;

    INSERT INTO material_event (material_uid, entity, op, payload, actor)
    VALUES (p_uid, 'material_change_request', 'insert',
            jsonb_build_object('request_id', v_id,
                               'from', NULL::text, 'to', 'pending',
                               'actor', v_actor, 'reason', p_reason,
                               'paths', to_jsonb(v_paths)),
            v_actor);

    RETURN jsonb_build_object('ok', true, 'code', 'submitted', 'id', v_id,
                              'material_uid', p_uid, 'status', 'pending',
                              'diffs', v_diffs,
                              'base', jsonb_build_object('rev', v_row.rev, 'values', v_values));
END $$;

COMMENT ON FUNCTION material_change_request_submit(text, jsonb, text, text, text) IS
    'File a write-back diff request; before is computed from the live row and the path allowlist is enforced here.';

-- ---------------------------------------------------------------------------
-- material_change_request_decide - approve or reject a pending request
-- ---------------------------------------------------------------------------
-- The submitter cannot approve their own request in v1, and an approval is
-- only accepted while the diffed fields still match the base fingerprint:
-- approve on a stale base returns {ok:false, code:'stale_base'} (409 at the
-- edge). Rejection skips the staleness check; rejecting stale data is safe.
--
-- T3 x T2: approval also requires a live material. A merge (T2) can supersede
-- a row after the request was filed, and the tombstone keeps its old field
-- values, so the base check alone would still pass. Approving there would send
-- a PATCH to an upstream record another human just declared a duplicate, so
-- the transition returns material_inactive instead (docs section 6.1).
CREATE OR REPLACE FUNCTION material_change_request_decide(
    p_id       bigint,
    p_decision text,
    p_reason   text DEFAULT NULL,
    p_actor    text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    v_row       material_change_request%ROWTYPE;
    v_actor     text;
    v_status    text;
    v_current   jsonb;
    v_matstatus text;
BEGIN
    IF p_decision NOT IN ('approve', 'reject') THEN
        RAISE EXCEPTION 'decision must be approve or reject (got %)', p_decision
            USING ERRCODE = '22023';
    END IF;
    IF length(coalesce(p_reason, '')) > 500 THEN
        RAISE EXCEPTION 'reason must be at most 500 characters' USING ERRCODE = '22023';
    END IF;
    v_actor := coalesce(nullif(btrim(p_actor), ''),
                        nullif(current_setting('ml.actor', true), ''),
                        'anonymous');

    SELECT * INTO v_row FROM material_change_request WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'code', 'not_found', 'id', p_id);
    END IF;
    IF v_row.status <> 'pending' THEN
        RETURN jsonb_build_object('ok', false, 'code', 'already_decided',
                                  'id', p_id, 'status', v_row.status);
    END IF;
    IF v_actor = v_row.requested_by THEN
        RETURN jsonb_build_object('ok', false, 'code', 'self_approval',
                                  'id', p_id, 'requested_by', v_row.requested_by);
    END IF;

    v_status := CASE p_decision WHEN 'approve' THEN 'approved' ELSE 'rejected' END;
    IF p_decision = 'approve' THEN
        -- the FK guarantees the material row exists; a merge or an archive may
        -- still have moved it out of 'active' since submit (docs 6.1)
        SELECT m.status INTO v_matstatus
          FROM material m WHERE m.material_uid = v_row.material_uid;
        IF v_matstatus IN ('archived', 'superseded') THEN
            RETURN jsonb_build_object('ok', false, 'code', 'material_inactive', 'id', p_id,
                                      'material_uid', v_row.material_uid,
                                      'material_status', v_matstatus);
        END IF;
        SELECT coalesce(jsonb_agg(d ->> 'path' ORDER BY ord), '[]'::jsonb)
          INTO v_current
          FROM jsonb_array_elements(v_row.diffs) WITH ORDINALITY AS t(d, ord);
        v_current := ml_writeback_current_values(v_row.material_uid, v_current);
        IF v_current IS DISTINCT FROM (v_row.base -> 'values') THEN
            RETURN jsonb_build_object('ok', false, 'code', 'stale_base', 'id', p_id,
                                      'base', v_row.base -> 'values',
                                      'current', v_current);
        END IF;
    END IF;

    PERFORM set_config('ml.actor', v_actor, true);
    PERFORM set_config('ml.source', 'writeback', true);

    UPDATE material_change_request
       SET status = v_status, decided_by = v_actor, decided_at = now()
     WHERE id = p_id;

    INSERT INTO material_event (material_uid, entity, op, payload, actor)
    VALUES (v_row.material_uid, 'material_change_request', 'update',
            jsonb_build_object('request_id', p_id,
                               'from', 'pending', 'to', v_status,
                               'actor', v_actor, 'reason', p_reason,
                               'decision', p_decision),
            v_actor);

    RETURN jsonb_build_object('ok', true, 'code', v_status, 'id', p_id,
                              'status', v_status, 'decided_by', v_actor);
END $$;

COMMENT ON FUNCTION material_change_request_decide(bigint, text, text, text) IS
    'Approve or reject a pending write-back request; approval re-checks the base fingerprint.';

-- ---------------------------------------------------------------------------
-- material_change_request_plan - the byte-for-byte PATCH preview
-- ---------------------------------------------------------------------------
-- Pure. Returns the method, URL path, headers and body that --apply sends.
-- Authorization is added by the executor and never appears in a plan or a
-- report (docs section 8); X-Workspace-Slug is filled from the p_workspace
-- argument, which the CLI passes from ML_JF_WORKSPACE. The URL is a path: the
-- executor prefixes ML_JF_BASE_URL.
CREATE OR REPLACE FUNCTION material_change_request_plan(
    p_id        bigint,
    p_workspace text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_row  material_change_request%ROWTYPE;
    v_mat  material%ROWTYPE;
    v_cat  material_writeback_catalog%ROWTYPE;
    v_body jsonb;
    v_hdrs jsonb;
    v_key  text;
BEGIN
    SELECT * INTO v_row FROM material_change_request WHERE id = p_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'code', 'not_found', 'id', p_id);
    END IF;
    SELECT * INTO v_mat FROM material WHERE material_uid = v_row.material_uid;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'code', 'invalid_target',
                                  'id', p_id, 'source_id', NULL);
    END IF;
    -- T3 x T2: no plan for a tombstone. This is the guard the executor relies
    -- on: --apply turns a non-ok plan into settle('failed', error=code) before
    -- any HTTP call, so a merged-away material can never be patched upstream.
    IF v_mat.status IN ('archived', 'superseded') THEN
        RETURN jsonb_build_object('ok', false, 'code', 'material_inactive',
                                  'id', p_id, 'material_uid', v_mat.material_uid,
                                  'material_status', v_mat.status);
    END IF;
    IF v_mat.source_system <> 'joineryflow'
       OR v_mat.source_id !~ '^[a-z_]+:[0-9]+$' THEN
        RETURN jsonb_build_object('ok', false, 'code', 'invalid_target',
                                  'id', p_id, 'source_id', v_mat.source_id);
    END IF;
    SELECT * INTO v_cat FROM material_writeback_catalog
     WHERE table_name = split_part(v_mat.source_id, ':', 1);
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'code', 'invalid_target',
                                  'id', p_id, 'source_id', v_mat.source_id,
                                  'table', split_part(v_mat.source_id, ':', 1));
    END IF;

    SELECT coalesce(jsonb_object_agg(x.up, x.aft ORDER BY x.up), '{}'::jsonb)
      INTO v_body
      FROM (
            SELECT CASE WHEN d ->> 'path' LIKE 'attributes.%'
                        THEN regexp_replace(d ->> 'path', '^attributes\.', '')
                        ELSE f.upstream_column
                   END AS up,
                   d -> 'after' AS aft
              FROM jsonb_array_elements(v_row.diffs) d
              LEFT JOIN material_writeback_field f ON f.umr_path = d ->> 'path'
           ) x;

    v_key := coalesce(v_row.idempotency_key, 'mlcr-' || v_row.id::text);
    v_hdrs := jsonb_build_object('Content-Type', 'application/json',
                                 'Idempotency-Key', v_key);
    IF p_workspace IS NOT NULL AND btrim(p_workspace) <> '' THEN
        v_hdrs := v_hdrs || jsonb_build_object('X-Workspace-Slug', btrim(p_workspace));
    END IF;

    RETURN jsonb_build_object('ok', true, 'code', 'planned',
                              'id', p_id, 'status', v_row.status,
                              'method', 'PATCH',
                              'url', v_cat.endpoint || '/' || split_part(v_mat.source_id, ':', 2),
                              'table', v_cat.table_name, 'mid', split_part(v_mat.source_id, ':', 2),
                              'headers', v_hdrs, 'body', v_body,
                              'idempotency_key', v_key, 'base', v_row.base);
END $$;

COMMENT ON FUNCTION material_change_request_plan(bigint, text) IS
    'The exact PATCH a worker would send for one request; no writes, no Authorization header.';

-- ---------------------------------------------------------------------------
-- material_change_request_queue - the review list
-- ---------------------------------------------------------------------------
-- actionable (the default) is pending + approved + failed: everything a human
-- or the executor can still act on. rejected and applied are history.
CREATE OR REPLACE FUNCTION material_change_request_queue(
    p_status text DEFAULT 'actionable',
    p_limit  integer DEFAULT 20,
    p_offset integer DEFAULT 0
) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_status text := lower(coalesce(nullif(btrim(p_status), ''), 'actionable'));
    v_limit  integer := least(greatest(coalesce(p_limit, 20), 1), 500);
    v_offset integer := greatest(coalesce(p_offset, 0), 0);
    v_rows   jsonb;
    v_total  bigint;
BEGIN
    IF v_status NOT IN ('actionable', 'pending', 'approved', 'rejected', 'applied', 'failed', 'all') THEN
        RAISE EXCEPTION 'status must be actionable, pending, approved, rejected, applied, failed or all (got %)',
                        v_status
            USING ERRCODE = '22023';
    END IF;

    SELECT coalesce(jsonb_agg(to_jsonb(x) - 'total' ORDER BY x.id), '[]'::jsonb),
           coalesce(max(x.total), 0)
      INTO v_rows, v_total
      FROM (
            SELECT r.id,
                   r.material_uid,
                   m.name            AS material_name,
                   m.category_code,
                   r.target_system,
                   r.status,
                   r.attempts,
                   r.last_status,
                   r.error,
                   r.reason,
                   r.requested_by,
                   r.requested_at,
                   r.decided_by,
                   r.decided_at,
                   r.applied_at,
                   coalesce(r.idempotency_key, 'mlcr-' || r.id::text) AS idempotency_key,
                   (SELECT jsonb_agg(d ->> 'path' ORDER BY ord)
                      FROM jsonb_array_elements(r.diffs) WITH ORDINALITY AS t(d, ord)) AS paths,
                   count(*) OVER () AS total
              FROM material_change_request r
              JOIN material m ON m.material_uid = r.material_uid
             WHERE CASE v_status
                       WHEN 'actionable' THEN r.status IN ('pending', 'approved', 'failed')
                       WHEN 'all'        THEN true
                       ELSE r.status = v_status
                   END
             ORDER BY r.id
             LIMIT v_limit OFFSET v_offset
           ) x;

    RETURN jsonb_build_object('status', v_status,
                              'total',  v_total,
                              'offset', v_offset,
                              'limit',  v_limit,
                              'rows',   v_rows);
END $$;

COMMENT ON FUNCTION material_change_request_queue(text, integer, integer) IS
    'Review list for write-back requests; actionable = pending + approved + failed.';

-- ---------------------------------------------------------------------------
-- material_change_request_claim - take a disjoint batch on a lease (step 2)
-- ---------------------------------------------------------------------------
-- Claimable means approved and either never claimed or past the lease. The
-- five status values stay untouched: claimed_at is the in-flight marker, not a
-- sixth state (docs section 5). SKIP LOCKED lets a second worker take the next
-- rows immediately instead of blocking on this one, and both claim and settle
-- are short transactions, so no row lock is ever held across the upstream
-- HTTP call (D11, D12, acceptance criterion 5).
--
-- p_lease is a parameter so a test can exercise expiry without waiting five
-- minutes; production uses the default. A claim writes NO material_event:
-- acceptance criterion 2 counts exactly three events on a happy path (insert,
-- approve, apply), and a claim is bookkeeping rather than a state change.
CREATE OR REPLACE FUNCTION material_change_request_claim(
    p_limit   integer  DEFAULT 20,
    p_actor   text     DEFAULT NULL,
    p_lease   interval DEFAULT interval '5 minutes',
    p_only_id bigint   DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    v_actor text;
    v_limit integer;
    v_rows  jsonb;
BEGIN
    IF p_lease IS NULL OR p_lease <= interval '0' OR p_lease > interval '1 hour' THEN
        RAISE EXCEPTION 'lease must be greater than zero and at most one hour'
            USING ERRCODE = '22023';
    END IF;
    v_limit := least(greatest(coalesce(p_limit, 20), 1), 500);
    v_actor := coalesce(nullif(btrim(p_actor), ''),
                        nullif(current_setting('ml.actor', true), ''),
                        'anonymous');

    PERFORM set_config('ml.actor', v_actor, true);
    PERFORM set_config('ml.source', 'writeback', true);

    WITH candidates AS MATERIALIZED (
        SELECT r.id
          FROM material_change_request r
         WHERE r.status = 'approved'
           AND (p_only_id IS NULL OR r.id = p_only_id)
           AND (r.claimed_at IS NULL OR r.claimed_at < now() - p_lease)
         ORDER BY r.requested_at, r.id
         LIMIT v_limit
         FOR UPDATE SKIP LOCKED
    ), claimed AS (
        UPDATE material_change_request r
           SET claimed_at      = now(),
               claimed_by      = v_actor,
               attempts        = r.attempts + 1,
               idempotency_key = coalesce(r.idempotency_key, 'mlcr-' || r.id::text)
          FROM candidates c
         WHERE r.id = c.id
        RETURNING r.*
    )
    SELECT coalesce(jsonb_agg(jsonb_build_object(
                        'id', c.id,
                        'material_uid', c.material_uid,
                        'status', c.status,
                        'diffs', c.diffs,
                        'base', c.base,
                        'attempts', c.attempts,
                        'claimed_at', c.claimed_at,
                        'claimed_by', c.claimed_by,
                        'idempotency_key', c.idempotency_key
                    ) ORDER BY c.id), '[]'::jsonb)
      INTO v_rows
      FROM claimed c;

    RETURN jsonb_build_object('ok', true, 'code', 'claimed',
                              'claimed', jsonb_array_length(v_rows),
                              'actor', v_actor, 'lease', p_lease::text,
                              'rows', v_rows);
END $$;

COMMENT ON FUNCTION material_change_request_claim(integer, text, interval, bigint) IS
    'Claim a disjoint batch of approved requests with a five-minute lease; p_only_id supports the CLI --request-id.';

-- ---------------------------------------------------------------------------
-- material_change_request_settle - record the upstream outcome (step 2)
-- ---------------------------------------------------------------------------
-- p_outcome is applied (2xx), failed (known 4xx / local rejection) or unknown
-- (5xx, timeout, connection error). Both failed and unknown end in the failed
-- status; unknown is distinct only by error = 'uncertain_outcome', which
-- retry() refuses (D10). Only a worker that still owns the live lease may
-- settle, so a crashed worker that comes back late cannot overwrite a newer
-- claim, and pending / rejected / applied rows refuse the transition outright.
--
-- D8: applied re-checks the field-level base one last time. If the master data
-- moved after approval the request becomes failed with error 'stale_base' even
-- when the upstream already answered 2xx; the report calls that outcome
-- 'applied_unverified' because the two systems may legitimately diverge.
--
-- T3 x T2: applied also re-checks material.status, for the merge that lands
-- between plan() and this call. The 2xx may well be real, but it landed on a
-- row T2 has just superseded, so it is recorded as failed / material_inactive
-- and reported the same way as stale_base - a human decides (docs 6.1).
--
-- T3 x T3 (docs 6.2): a confirmed write is written home. The library is the
-- source of truth, so the approved `after` values are applied to `material` in
-- this transaction; the triggers turn that into one revision and one material
-- event, and a later submit of the same diff returns no_change instead of
-- filing it twice. A local write that fails (constraint, unknown unit, ...)
-- does not lose the 2xx: the request becomes failed / local_write_failed and
-- is reported as applied_unverified.
--
-- 6.3: any 2xx advances material_writeback_state, so a snapshot older than
-- the last upstream write stays refused even after applied rows are pruned.
CREATE OR REPLACE FUNCTION material_change_request_settle(
    p_id          bigint,
    p_outcome     text,
    p_status_code integer  DEFAULT NULL,
    p_error       text     DEFAULT NULL,
    p_actor       text     DEFAULT NULL,
    p_lease       interval DEFAULT interval '5 minutes'
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    v_row        material_change_request%ROWTYPE;
    v_actor      text;
    v_status     text := 'failed';
    v_error      text;
    v_outcome    text;
    v_paths      jsonb;
    v_current    jsonb;
    v_matstatus  text;
    v_unverified boolean := false;
    v_upstream   boolean := false;
    v_diff       jsonb;
    v_path       text;
    v_after      jsonb;
    v_col        text;
    v_attrs      jsonb;
    v_attrs_done boolean := false;
    v_sets       text[] := '{}';
BEGIN
    IF p_outcome IS NULL OR p_outcome NOT IN ('applied', 'failed', 'unknown') THEN
        RAISE EXCEPTION 'outcome must be applied, failed or unknown'
            USING ERRCODE = '22023';
    END IF;
    IF p_status_code IS NOT NULL AND (p_status_code < 100 OR p_status_code > 599) THEN
        RAISE EXCEPTION 'status_code must be between 100 and 599 or null'
            USING ERRCODE = '22023';
    END IF;
    IF length(coalesce(p_error, '')) > 500 THEN
        RAISE EXCEPTION 'error must be at most 500 characters' USING ERRCODE = '22023';
    END IF;
    IF p_lease IS NULL OR p_lease <= interval '0' OR p_lease > interval '1 hour' THEN
        RAISE EXCEPTION 'lease must be greater than zero and at most one hour'
            USING ERRCODE = '22023';
    END IF;
    v_actor := coalesce(nullif(btrim(p_actor), ''),
                        nullif(current_setting('ml.actor', true), ''),
                        'anonymous');

    SELECT * INTO v_row FROM material_change_request WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'code', 'not_found', 'id', p_id);
    END IF;
    IF v_row.status <> 'approved' THEN
        RETURN jsonb_build_object('ok', false, 'code', 'conflict',
                                  'id', p_id, 'status', v_row.status);
    END IF;
    IF v_row.claimed_at IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'code', 'not_claimed', 'id', p_id);
    END IF;
    IF v_row.claimed_by IS DISTINCT FROM v_actor THEN
        RETURN jsonb_build_object('ok', false, 'code', 'lease_owner_mismatch',
                                  'id', p_id, 'claimed_by', v_row.claimed_by,
                                  'actor', v_actor);
    END IF;
    IF v_row.claimed_at < now() - p_lease THEN
        RETURN jsonb_build_object('ok', false, 'code', 'lease_expired',
                                  'id', p_id, 'claimed_at', v_row.claimed_at);
    END IF;

    v_outcome := p_outcome;
    IF p_outcome = 'applied' THEN
        SELECT coalesce(jsonb_agg(d ->> 'path' ORDER BY ord), '[]'::jsonb)
          INTO v_paths
          FROM jsonb_array_elements(v_row.diffs) WITH ORDINALITY AS t(d, ord);
        v_current := ml_writeback_current_values(v_row.material_uid, v_paths);
        SELECT m.status, m.attributes INTO v_matstatus, v_attrs
          FROM material m WHERE m.material_uid = v_row.material_uid;
        IF v_current IS DISTINCT FROM (v_row.base -> 'values') THEN
            v_unverified := true;
            v_error   := 'stale_base';
            v_outcome := 'applied_unverified';
        ELSIF v_matstatus IN ('archived', 'superseded') THEN
            -- a merge landed while the PATCH was in flight: the upstream write
            -- may well have succeeded, but it landed on a record T2 has just
            -- declared a duplicate. Report it like stale_base: a human decides.
            v_unverified := true;
            v_error   := 'material_inactive';
            v_outcome := 'applied_unverified';
        ELSE
            v_status := 'applied';
            v_error  := NULL;
            -- T3 x T3 (docs 6.2): the library is the source of truth, so a
            -- confirmed write is written home in the same transaction. The
            -- triggers turn this into one revision and one material event.
            -- A local write that fails must not lose the 2xx: it degrades to
            -- applied_unverified instead of raising after the PATCH landed.
            BEGIN
                v_attrs := coalesce(v_attrs, '{}'::jsonb);
                FOR v_diff IN SELECT * FROM jsonb_array_elements(v_row.diffs) LOOP
                    v_path  := v_diff ->> 'path';
                    v_after := CASE WHEN jsonb_typeof(v_diff -> 'after') = 'null'
                                    THEN NULL ELSE v_diff -> 'after' END;
                    IF v_path LIKE 'attributes.%' THEN
                        v_attrs := jsonb_set(
                            v_attrs,
                            ARRAY[regexp_replace(v_path, '^attributes\.', '')],
                            coalesce(v_after, 'null'::jsonb), true);
                        v_attrs_done := true;
                    ELSE
                        SELECT f.material_column INTO v_col
                          FROM material_writeback_field f WHERE f.umr_path = v_path;
                        IF v_col IS NULL THEN
                            RAISE EXCEPTION 'no material column for path %', v_path;
                        END IF;
                        IF v_col = 'synonyms' THEN
                            -- synonyms is NOT NULL: a null after means "clear"
                            v_sets := v_sets || CASE WHEN v_after IS NULL
                                THEN 'synonyms = ''{}''::text[]'
                                ELSE format('synonyms = ARRAY(SELECT jsonb_array_elements_text(%L::jsonb))',
                                            v_after)
                                END;
                        ELSIF v_col = 'unit_cost' THEN
                            v_sets := v_sets || format('unit_cost = %s::numeric',
                                                       quote_nullable(v_after #>> '{}'));
                        ELSIF v_col = 'lead_time_days' THEN
                            v_sets := v_sets || format('lead_time_days = %s::integer',
                                                       quote_nullable(v_after #>> '{}'));
                        ELSE
                            v_sets := v_sets || format('%I = %L', v_col, v_after #>> '{}');
                        END IF;
                    END IF;
                END LOOP;
                IF v_attrs_done THEN
                    v_sets := v_sets || format('attributes = %L::jsonb', v_attrs);
                END IF;
                IF cardinality(v_sets) > 0 THEN
                    EXECUTE 'UPDATE material SET ' || array_to_string(v_sets, ', ')
                         || ' WHERE material_uid = $1' USING v_row.material_uid;
                END IF;
            EXCEPTION WHEN others THEN
                v_status     := 'failed';
                v_error      := 'local_write_failed';
                v_outcome    := 'applied_unverified';
                v_unverified := true;
            END;
        END IF;
    ELSIF p_outcome = 'unknown' THEN
        v_error := 'uncertain_outcome';
    ELSE
        v_error := coalesce(nullif(btrim(coalesce(p_error, '')), ''), 'upstream_error');
    END IF;

    -- The watermark is about the upstream write, not the local one: a 2xx on
    -- stale data or a tombstone still counts, because the PATCH may have landed.
    -- A 5xx / timeout is left to the --verify question (docs 6.3, section 16).
    v_upstream := p_outcome = 'applied'
                  AND (p_status_code IS NULL
                       OR (p_status_code >= 200 AND p_status_code < 300));
    IF v_upstream THEN
        INSERT INTO material_writeback_state AS s
               (target_system, last_upstream_write_at, last_request_id)
        VALUES (v_row.target_system, now(), p_id)
        ON CONFLICT (target_system) DO UPDATE
           SET last_upstream_write_at = greatest(s.last_upstream_write_at,
                                                 EXCLUDED.last_upstream_write_at),
               last_request_id = EXCLUDED.last_request_id,
               advances       = s.advances + 1;
    END IF;

    PERFORM set_config('ml.actor', v_actor, true);
    PERFORM set_config('ml.source', 'writeback', true);

    UPDATE material_change_request
       SET status      = v_status,
           applied_at  = CASE WHEN v_status = 'applied' THEN now() ELSE NULL END,
           error       = v_error,
           last_status = p_status_code,
           claimed_at  = NULL,
           claimed_by  = NULL
     WHERE id = p_id;

    INSERT INTO material_event (material_uid, entity, op, payload, actor)
    VALUES (v_row.material_uid, 'material_change_request', 'update',
            jsonb_build_object('request_id', p_id,
                               'from', 'approved', 'to', v_status,
                               'actor', v_actor, 'reason', p_error,
                               'outcome', v_outcome,
                               'status_code', p_status_code,
                               'unverified', v_unverified,
                               'error', v_error,
                               'material_status', v_matstatus,
                               'upstream_write', v_upstream),
            v_actor);

    RETURN jsonb_build_object('ok', true, 'code', v_status,
                              'outcome', v_outcome, 'id', p_id,
                              'status', v_status, 'error', v_error,
                              'last_status', p_status_code,
                              'applied_unverified', v_unverified);
END $$;

COMMENT ON FUNCTION material_change_request_settle(bigint, text, integer, text, text, interval) IS
    'Settle a claimed request: applied, failed or unknown; re-checks base and material status, writes the diff home, advances the upstream watermark and writes one event.';

-- ---------------------------------------------------------------------------
-- material_change_request_retry - explicit human retry of a known failure
-- ---------------------------------------------------------------------------
-- This is the last transition in docs section 6 (failed -> approved). It is
-- deliberately separate from settle and requires a new actor, because a retry
-- is a fresh approval of the same diff. An uncertain outcome can never be
-- retried: a human must GET the upstream record first (D10). The base is
-- checked again so a retry cannot approve a diff against newer master data.
CREATE OR REPLACE FUNCTION material_change_request_retry(
    p_id     bigint,
    p_actor  text DEFAULT NULL,
    p_reason text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    v_row       material_change_request%ROWTYPE;
    v_actor     text;
    v_paths     jsonb;
    v_current   jsonb;
    v_matstatus text;
BEGIN
    IF length(coalesce(p_reason, '')) > 500 THEN
        RAISE EXCEPTION 'reason must be at most 500 characters' USING ERRCODE = '22023';
    END IF;
    v_actor := coalesce(nullif(btrim(p_actor), ''),
                        nullif(current_setting('ml.actor', true), ''),
                        'anonymous');

    SELECT * INTO v_row FROM material_change_request WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'code', 'not_found', 'id', p_id);
    END IF;
    IF v_row.status <> 'failed' THEN
        RETURN jsonb_build_object('ok', false, 'code', 'conflict',
                                  'id', p_id, 'status', v_row.status);
    END IF;
    IF v_row.error = 'uncertain_outcome' THEN
        RETURN jsonb_build_object('ok', false, 'code', 'uncertain_outcome',
                                  'id', p_id, 'error', v_row.error);
    END IF;
    IF v_actor = v_row.requested_by OR v_actor = v_row.decided_by THEN
        RETURN jsonb_build_object('ok', false, 'code', 'self_approval',
                                  'id', p_id, 'requested_by', v_row.requested_by,
                                  'decided_by', v_row.decided_by);
    END IF;
    -- retry is an approval transition too, so it carries the same T3 x T2
    -- guard: a retried request must not be approved onto a tombstone
    SELECT m.status INTO v_matstatus
      FROM material m WHERE m.material_uid = v_row.material_uid;
    IF v_matstatus IN ('archived', 'superseded') THEN
        RETURN jsonb_build_object('ok', false, 'code', 'material_inactive', 'id', p_id,
                                  'material_uid', v_row.material_uid,
                                  'material_status', v_matstatus);
    END IF;

    SELECT coalesce(jsonb_agg(d ->> 'path' ORDER BY ord), '[]'::jsonb)
      INTO v_paths
      FROM jsonb_array_elements(v_row.diffs) WITH ORDINALITY AS t(d, ord);
    v_current := ml_writeback_current_values(v_row.material_uid, v_paths);
    IF v_current IS DISTINCT FROM (v_row.base -> 'values') THEN
        RETURN jsonb_build_object('ok', false, 'code', 'stale_base', 'id', p_id,
                                  'base', v_row.base -> 'values',
                                  'current', v_current);
    END IF;

    PERFORM set_config('ml.actor', v_actor, true);
    PERFORM set_config('ml.source', 'writeback', true);

    UPDATE material_change_request
       SET status      = 'approved',
           decided_by  = v_actor,
           decided_at  = now(),
           applied_at  = NULL,
           error       = NULL,
           last_status = NULL,
           claimed_at  = NULL,
           claimed_by  = NULL
     WHERE id = p_id;

    INSERT INTO material_event (material_uid, entity, op, payload, actor)
    VALUES (v_row.material_uid, 'material_change_request', 'update',
            jsonb_build_object('request_id', p_id,
                               'from', 'failed', 'to', 'approved',
                               'actor', v_actor, 'reason', p_reason,
                               'decision', 'retry'),
            v_actor);

    RETURN jsonb_build_object('ok', true, 'code', 'approved', 'id', p_id,
                              'status', 'approved', 'decided_by', v_actor);
END $$;

COMMENT ON FUNCTION material_change_request_retry(bigint, text, text) IS
    'Explicit human retry of a known-failed request; refuses uncertain outcomes and stale bases.';
