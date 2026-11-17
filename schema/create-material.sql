-- schema/create-material.sql - approval-based creation of new materials.
--
-- Proposal: docs/ui-create-material.md (decisions in section 6). This is the
-- create-side counterpart of writeback.sql: a new material is a request, not a
-- direct INSERT, and another actor decides it. It deliberately does NOT touch
-- material_change_request: that table's material_uid is a FK to an existing
-- row, and its base fingerprint has nothing to compare for a row that does not
-- exist yet.
--
-- Load after writeback.sql, which owns the field allowlist and the actor
-- convention this file reuses.
--
-- Contract: scripts/create_material_contract.sql.

-- ---------------------------------------------------------------------------
-- material_create_request - one proposed row per request
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_create_request (
    id            bigserial PRIMARY KEY,
    proposed      jsonb NOT NULL,
    reason        text,
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','rejected','applied','failed')),
    -- Filled by apply. Nullable until then: there is no material to point at.
    material_uid  text REFERENCES material (material_uid) ON DELETE SET NULL,
    target_system text NOT NULL DEFAULT 'joineryflow',
    requested_by  text NOT NULL,
    requested_at  timestamptz NOT NULL DEFAULT now(),
    decided_by    text,
    decided_at    timestamptz,
    applied_at    timestamptz,
    error         text
);

CREATE INDEX IF NOT EXISTS material_create_request_status_idx
    ON material_create_request (status, requested_at DESC);

CREATE OR REPLACE FUNCTION ml_create_material_actor(p_actor text)
RETURNS text LANGUAGE sql STABLE AS $$
    SELECT coalesce(nullif(btrim(p_actor), ''),
                    nullif(current_setting('ml.actor', true), ''),
                    'anonymous');
$$;

-- New material UIDs follow the same Crockford base-32 alphabet as the master
-- table CHECK. 26 random characters from the 32-symbol domain; collision with
-- an existing row is retried by the caller's uniqueness loop.
CREATE OR REPLACE FUNCTION ml_new_material_uid()
RETURNS text LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    v_alphabet text := '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
    v_body     text := '';
    i          integer;
BEGIN
    FOR i IN 1..26 LOOP
        v_body := v_body || substr(v_alphabet, 1 + floor(random() * 32)::int, 1);
    END LOOP;
    RETURN 'mat_' || v_body;
END $$;

-- ---------------------------------------------------------------------------
-- ml_create_material_validate - the single source of truth for proposed rows
-- ---------------------------------------------------------------------------
-- All checks are entry checks with SQLSTATE 22023, matching submit(). The
-- entity tables are the authority: category_code and unit are checked against
-- their FK targets, and string lengths against submit()'s 1000-character cap.
CREATE OR REPLACE FUNCTION ml_create_material_validate(p_proposed jsonb)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_keys   text[];
    v_name   text;
    v_unit   text;
    v_cat    text;
    v_value  jsonb;
    v_key    text;
BEGIN
    IF p_proposed IS NULL OR jsonb_typeof(p_proposed) <> 'object' THEN
        RAISE EXCEPTION 'proposed must be a JSON object' USING ERRCODE = '22023';
    END IF;

    SELECT array_agg(k) INTO v_keys
      FROM jsonb_object_keys(p_proposed) AS k
     WHERE k NOT IN (
        'category_code','name','product_type','description','brand_id','model_no',
        'family_id','default_supplier_id','default_supplier_ref','lead_time_days',
        'unit','currency','cost_basis','unit_cost','image_url','product_url',
        'synonyms','tags','attributes'
     );
    IF array_length(v_keys, 1) > 0 THEN
        RAISE EXCEPTION 'unknown proposed field: %', v_keys[1] USING ERRCODE = '22023';
    END IF;

    v_cat := nullif(btrim(coalesce(p_proposed ->> 'category_code', ''), E' \t\r\n'), '');
    IF v_cat IS NULL THEN
        RAISE EXCEPTION 'category_code is required' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM material_category WHERE code = v_cat) THEN
        RAISE EXCEPTION 'unknown category_code: %', v_cat USING ERRCODE = '22023';
    END IF;

    v_name := p_proposed ->> 'name';
    -- btrim()'s default set is spaces only: a name of newlines must fail here,
    -- exactly as material_change_request_submit() refuses it (U19).
    IF v_name IS NULL OR btrim(v_name, E' \t\r\n') = '' THEN
        RAISE EXCEPTION 'name must be a non-empty string' USING ERRCODE = '22023';
    END IF;

    v_unit := nullif(btrim(coalesce(p_proposed ->> 'unit', ''), E' \t\r\n'), '');
    IF v_unit IS NOT NULL AND NOT EXISTS (SELECT 1 FROM unit WHERE code = v_unit) THEN
        RAISE EXCEPTION 'unknown unit: %', v_unit USING ERRCODE = '22023';
    END IF;

    -- One ceiling per string value, including strings inside arrays and the
    -- attributes object, so no client can skip the edit path's U20 rule.
    FOR v_key, v_value IN SELECT * FROM jsonb_each(p_proposed) LOOP
        IF jsonb_typeof(v_value) = 'string' AND length(v_value #>> '{}') > 1000 THEN
            RAISE EXCEPTION 'value for % exceeds 1000 characters', v_key
                USING ERRCODE = '22023';
        ELSIF jsonb_typeof(v_value) = 'array' AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(v_value) s
                 WHERE jsonb_typeof(s) = 'string' AND length(s #>> '{}') > 1000) THEN
            RAISE EXCEPTION 'value for % exceeds 1000 characters', v_key
                USING ERRCODE = '22023';
        ELSIF v_key = 'attributes' AND jsonb_typeof(v_value) <> 'object' THEN
            RAISE EXCEPTION 'attributes must be a JSON object' USING ERRCODE = '22023';
        END IF;
    END LOOP;
END $$;

COMMENT ON FUNCTION ml_create_material_validate(jsonb) IS
    'Entry checks for a proposed material: whitelisted fields, required category_code and name, '
    'valid unit, and the 1000-character cap. Shared by submit and update.';

-- ---------------------------------------------------------------------------
-- material_create_request_submit
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION material_create_request_submit(
    p_proposed jsonb,
    p_reason   text DEFAULT NULL,
    p_actor    text DEFAULT NULL,
    p_target   text DEFAULT 'joineryflow'
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    v_actor text;
    v_id    bigint;
BEGIN
    IF coalesce(nullif(btrim(p_target), ''), 'joineryflow') <> 'joineryflow' THEN
        RAISE EXCEPTION 'target_system must be joineryflow in v1 (got %)', p_target
            USING ERRCODE = '22023';
    END IF;
    IF length(coalesce(p_reason, '')) > 500 THEN
        RAISE EXCEPTION 'reason must be at most 500 characters' USING ERRCODE = '22023';
    END IF;
    v_actor := ml_create_material_actor(p_actor);
    PERFORM ml_create_material_validate(p_proposed);

    INSERT INTO material_create_request (proposed, reason, target_system, requested_by)
    VALUES (p_proposed, nullif(btrim(p_reason), ''), 'joineryflow', v_actor)
    RETURNING id INTO v_id;

    INSERT INTO material_event (material_uid, entity, op, payload, actor)
    VALUES (NULL, 'material_create_request', 'insert',
            jsonb_build_object('request_id', v_id, 'actor', v_actor,
                               'proposed', p_proposed, 'reason', p_reason),
            v_actor);

    RETURN jsonb_build_object('ok', true, 'code', 'pending', 'id', v_id,
                              'status', 'pending', 'requested_by', v_actor);
END $$;

COMMENT ON FUNCTION material_create_request_submit(jsonb, text, text, text) IS
    'File a proposed new material for approval. Validates with ml_create_material_validate; '
    'the material row is not created here.';

-- ---------------------------------------------------------------------------
-- material_create_request_update - only the author, only while pending
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION material_create_request_update(
    p_id       bigint,
    p_proposed jsonb,
    p_reason   text DEFAULT NULL,
    p_actor    text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    v_row      material_create_request%ROWTYPE;
    v_actor    text;
    v_before   jsonb;
BEGIN
    IF length(coalesce(p_reason, '')) > 500 THEN
        RAISE EXCEPTION 'reason must be at most 500 characters' USING ERRCODE = '22023';
    END IF;
    v_actor := ml_create_material_actor(p_actor);
    PERFORM ml_create_material_validate(p_proposed);

    SELECT * INTO v_row FROM material_create_request WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'code', 'not_found', 'id', p_id);
    END IF;
    IF v_row.status <> 'pending' THEN
        RETURN jsonb_build_object('ok', false, 'code', 'already_decided',
                                  'id', p_id, 'status', v_row.status);
    END IF;
    IF v_row.requested_by <> v_actor THEN
        RETURN jsonb_build_object('ok', false, 'code', 'forbidden',
                                  'id', p_id, 'requested_by', v_row.requested_by);
    END IF;

    v_before := jsonb_build_object('proposed', v_row.proposed, 'reason', v_row.reason);
    UPDATE material_create_request
       SET proposed = p_proposed,
           reason   = nullif(btrim(p_reason), '')
     WHERE id = p_id;

    INSERT INTO material_event (material_uid, entity, op, payload, actor)
    VALUES (NULL, 'material_create_request', 'update',
            jsonb_build_object('request_id', p_id, 'actor', v_actor,
                               'before', v_before,
                               'after', jsonb_build_object('proposed', p_proposed,
                                                           'reason', p_reason)),
            v_actor);

    RETURN jsonb_build_object('ok', true, 'code', 'pending', 'id', p_id,
                              'status', 'pending', 'requested_by', v_actor);
END $$;

COMMENT ON FUNCTION material_create_request_update(bigint, jsonb, text, text) IS
    'Rewrite a pending create request in place. Author only; one update event; keeps the id. '
    'A decided request is immutable - reject and file a new one.';

-- ---------------------------------------------------------------------------
-- material_create_request_decide
-- ---------------------------------------------------------------------------
-- No stale / inactive checks: a create has no base to go stale against and no
-- existing material to archive. The only guards are the shared ones: a valid
-- decision, a different actor, and a still-pending row.
CREATE OR REPLACE FUNCTION material_create_request_decide(
    p_id       bigint,
    p_decision text,
    p_reason   text DEFAULT NULL,
    p_actor    text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    v_row    material_create_request%ROWTYPE;
    v_actor  text;
    v_status text;
BEGIN
    IF p_decision NOT IN ('approve', 'reject') THEN
        RAISE EXCEPTION 'decision must be approve or reject (got %)', p_decision
            USING ERRCODE = '22023';
    END IF;
    IF length(coalesce(p_reason, '')) > 500 THEN
        RAISE EXCEPTION 'reason must be at most 500 characters' USING ERRCODE = '22023';
    END IF;
    v_actor := ml_create_material_actor(p_actor);

    SELECT * INTO v_row FROM material_create_request WHERE id = p_id FOR UPDATE;
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
    UPDATE material_create_request
       SET status = v_status, decided_by = v_actor, decided_at = now()
     WHERE id = p_id;

    INSERT INTO material_event (material_uid, entity, op, payload, actor)
    VALUES (NULL, 'material_create_request', 'update',
            jsonb_build_object('request_id', p_id,
                               'from', 'pending', 'to', v_status,
                               'actor', v_actor, 'reason', p_reason,
                               'decision', p_decision),
            v_actor);

    RETURN jsonb_build_object('ok', true, 'code', v_status, 'id', p_id,
                              'status', v_status, 'decided_by', v_actor);
END $$;

COMMENT ON FUNCTION material_create_request_decide(bigint, text, text, text) IS
    'Decide a pending create request. Different actor required; the reason is written to '
    'material_event, which is its only home.';
