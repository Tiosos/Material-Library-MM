-- ---------------------------------------------------------------------------
-- schema/merge.sql - duplicate candidates and the merge action (T2)
-- ---------------------------------------------------------------------------
-- Load order: schema/material.sql, schema/search.sql, schema/api.sql, schema/merge.sql,
-- schema/seed.sql, then optionally eval/fixture.sql.
--
-- Additive only: no column is dropped, no row is deleted. Step 0 of
-- docs/t2-merge.md lands the candidate decision columns; the scan, queue, preview,
-- merge and ignore functions follow in steps 1-3 of the same plan.
-- ---------------------------------------------------------------------------

ALTER TABLE material_duplicate_candidate
    ADD COLUMN IF NOT EXISTS keep_uid   text REFERENCES material (material_uid),
    ADD COLUMN IF NOT EXISTS decided_by text,
    ADD COLUMN IF NOT EXISTS decided_at timestamptz,
    ADD COLUMN IF NOT EXISTS note       text;

CREATE INDEX IF NOT EXISTS material_duplicate_status_idx
    ON material_duplicate_candidate (status, score DESC);

-- ---------------------------------------------------------------------------
-- material_duplicate_scan - fill or refresh the review queue
-- ---------------------------------------------------------------------------
-- Pairs active or draft materials that share a category and a brand and whose names are at least
-- p_threshold similar. A pair that differs on any declared variant axis (thickness, angle, ...)
-- is a variant of one product, not a duplicate of it, and is skipped: that is what keeps the
-- 16mm and 18mm boards out of the queue.
--
-- The queue is a review aid, never a decision: the function writes status 'open' rows and
-- refreshes their score. Rows a human already ignored or merged keep their status, so the queue
-- does not nag about decisions that were already made.
CREATE OR REPLACE FUNCTION material_duplicate_scan(
    p_threshold numeric DEFAULT 0.80,
    p_limit     integer DEFAULT 500
) RETURNS jsonb
LANGUAGE sql AS $$
    WITH candidates AS (
        SELECT least(a.material_uid, b.material_uid)                        AS uid_a,
               greatest(a.material_uid, b.material_uid)                     AS uid_b,
               round(similarity(lower(a.name), lower(b.name))::numeric, 4)  AS score
          FROM material a
          JOIN material b
            ON b.material_uid > a.material_uid
           AND b.category_code = a.category_code
           AND b.brand_id IS NOT DISTINCT FROM a.brand_id
         WHERE a.status IN ('active', 'draft')
           AND b.status IN ('active', 'draft')
           AND a.name IS NOT NULL
           AND b.name IS NOT NULL
           AND similarity(lower(a.name), lower(b.name))
                   >= least(greatest(coalesce(p_threshold, 0.80), 0), 1)
           AND NOT EXISTS (
                   SELECT 1 FROM jsonb_each(a.variant_values) x
                    WHERE b.variant_values ? x.key
                      AND b.variant_values -> x.key IS DISTINCT FROM x.value)
           AND NOT EXISTS (
                   SELECT 1 FROM jsonb_each(b.variant_values) x
                    WHERE a.variant_values ? x.key
                      AND a.variant_values -> x.key IS DISTINCT FROM x.value)
           -- Names that match once digits are masked but differ in the digits themselves are
           -- size or angle variants ("shelf pin 1mm" vs "shelf pin 2mm"), not duplicates.
           AND NOT (regexp_replace(lower(a.name), '[0-9]+', '#', 'g')
                        = regexp_replace(lower(b.name), '[0-9]+', '#', 'g')
                    AND regexp_replace(lower(a.name), '[^0-9]+', '', 'g')
                        <> regexp_replace(lower(b.name), '[^0-9]+', '', 'g'))
           -- Two records that carry different part numbers of the same kind are different parts,
           -- however similar their names are: the Blum catalogue repeats one name for dozens of
           -- hinges that only differ by code. A shared code, or one being a prefix of the other
           -- (a revision suffix), still passes this filter.
           AND NOT EXISTS (
                   SELECT 1 FROM material_identifier ia
                     JOIN material_identifier ib
                       ON ib.kind = ia.kind
                      AND ib.material_uid = b.material_uid
                    WHERE ia.material_uid = a.material_uid
                      AND lower(ib.value) NOT LIKE lower(ia.value) || '%'
                      AND lower(ia.value) NOT LIKE lower(ib.value) || '%')
           AND NOT EXISTS (
                   SELECT 1 FROM material_duplicate_candidate c
                    WHERE c.uid_a = least(a.material_uid, b.material_uid)
                      AND c.uid_b = greatest(a.material_uid, b.material_uid)
                      AND c.status = 'merged')
         ORDER BY score DESC, uid_a, uid_b
         LIMIT least(greatest(coalesce(p_limit, 500), 1), 5000)
    ), upserted AS (
        INSERT INTO material_duplicate_candidate AS c (uid_a, uid_b, score)
        SELECT uid_a, uid_b, score FROM candidates
        ON CONFLICT (uid_a, uid_b) DO UPDATE
           SET score = EXCLUDED.score
         WHERE c.status = 'open'
        RETURNING (xmax = 0) AS inserted
    )
    SELECT jsonb_build_object(
               'threshold',  least(greatest(coalesce(p_threshold, 0.80), 0), 1),
               'pairs',      (SELECT count(*) FROM candidates),
               'inserted',   (SELECT count(*) FROM upserted WHERE inserted),
               'updated',    (SELECT count(*) FROM upserted WHERE NOT inserted),
               'candidates', coalesce((SELECT jsonb_agg(jsonb_build_object(
                                                   'uid_a', uid_a,
                                                   'uid_b', uid_b,
                                                   'score', score) ORDER BY score DESC, uid_a)
                                         FROM candidates), '[]'::jsonb));
$$;

COMMENT ON FUNCTION material_duplicate_scan(numeric, integer) IS
    'Fills or refreshes the duplicate review queue (status open). Returns pair/insert/update counts.';

-- ---------------------------------------------------------------------------
-- ml_duplicate_keeper - which of two rows looks like the better keeper
-- ---------------------------------------------------------------------------
-- A suggestion only: the API never applies it without an explicit `keep`. Prefers the more
-- complete profile, then the row with more prices, then the one used more often, then the
-- older record, then uid order so the answer is stable.
CREATE OR REPLACE FUNCTION ml_duplicate_keeper(p_uid_a text, p_uid_b text) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT m.material_uid
      FROM material m
      LEFT JOIN v_material_completeness c ON c.material_uid = m.material_uid
     WHERE m.material_uid IN (p_uid_a, p_uid_b)
     ORDER BY coalesce(c.present_count::numeric / nullif(c.required_count, 0), 0) DESC,
              (SELECT count(*) FROM material_price pr WHERE pr.material_uid = m.material_uid) DESC,
              coalesce((SELECT sum(u.ref_count) FROM material_usage u
                         WHERE u.material_uid = m.material_uid), 0) DESC,
              m.created_at ASC,
              m.material_uid
     LIMIT 1
$$;

COMMENT ON FUNCTION ml_duplicate_keeper(text, text) IS
    'Suggests which of two duplicate candidates should be kept. A suggestion, never applied on its own.';

-- ---------------------------------------------------------------------------
-- material_merge_plan - what the merge would produce, without writing
-- ---------------------------------------------------------------------------
-- Returns NULL when either uid is unknown, so callers can answer 404 without an exception.
-- The plan drives both the preview and the merge, so there is one description of the rules
-- (docs/t2-merge.md section 4) and no drift between what it says and what it does.
CREATE OR REPLACE FUNCTION material_merge_plan(p_keep text, p_merge text)
RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
    k             material;
    s             material;
    v_fields      jsonb := '{}'::jsonb;
    v_dropped     jsonb := '[]'::jsonb;
    v_warnings    jsonb := '[]'::jsonb;
    v_att         jsonb := '[]'::jsonb;
    v_seen        text[] := '{}'::text[];
    v_tags        text[];
    v_synonyms    text[];
    v_key         text;
    r             record;
    v_id_moved    integer := 0;
    v_id_drop     integer := 0;
    v_id_conflict integer := 0;
    v_meas_moved  integer := 0;
    v_meas_drop   integer := 0;
    v_meas_demote integer := 0;
    v_meas_conf   integer := 0;
    v_price_moved integer := 0;
    v_price_drop  integer := 0;
    v_rel_from    integer := 0;
    v_rel_to      integer := 0;
    v_rel_self    integer := 0;
    v_rel_dup     integer := 0;
    v_cls_moved   integer := 0;
    v_cls_drop    integer := 0;
    v_epd_moved   integer := 0;
    v_epd_drop    integer := 0;
    v_epd_conf    integer := 0;
    v_usage       integer := 0;
BEGIN
    SELECT * INTO k FROM material WHERE material_uid = p_keep;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT * INTO s FROM material WHERE material_uid = p_merge;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    -- Scalar text: K wins unless it is empty; the survivor's value only fills a hole.
    IF coalesce(btrim(k.name), '') = '' AND s.name IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('name', s.name);
    ELSIF coalesce(btrim(k.name), '') <> '' AND s.name IS NOT NULL AND btrim(s.name) <> btrim(k.name) THEN
        v_dropped := v_dropped || jsonb_build_array(jsonb_build_object('field', 'name', 'value', s.name));
    END IF;
    IF coalesce(btrim(k.description), '') = '' AND s.description IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('description', s.description);
    ELSIF coalesce(btrim(k.description), '') <> '' AND s.description IS NOT NULL AND btrim(s.description) <> btrim(k.description) THEN
        v_dropped := v_dropped || jsonb_build_array(jsonb_build_object('field', 'description', 'value', s.description));
    END IF;
    IF coalesce(btrim(k.model_no), '') = '' AND s.model_no IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('model_no', s.model_no);
    END IF;
    IF coalesce(btrim(k.product_type), '') = '' AND s.product_type IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('product_type', s.product_type);
    END IF;

    -- References: K wins, and a real disagreement is reported rather than silently resolved.
    IF k.brand_id IS NULL AND s.brand_id IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('brand_id', s.brand_id);
    ELSIF k.brand_id IS NOT NULL AND s.brand_id IS NOT NULL AND k.brand_id <> s.brand_id THEN
        v_warnings := v_warnings || jsonb_build_array(jsonb_build_object(
            'code', 'brand_mismatch',
            'detail', format('keeper brand %s, survivor brand %s; keeper wins', k.brand_id, s.brand_id)));
        v_dropped := v_dropped || jsonb_build_array(jsonb_build_object('field', 'brand_id', 'value', s.brand_id));
    END IF;
    IF k.default_supplier_id IS NULL AND s.default_supplier_id IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('default_supplier_id', s.default_supplier_id);
    END IF;
    IF coalesce(btrim(k.default_supplier_ref), '') = '' AND s.default_supplier_ref IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('default_supplier_ref', s.default_supplier_ref);
    END IF;
    IF k.family_id IS NULL AND s.family_id IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('family_id', s.family_id);
    ELSIF k.family_id IS NOT NULL AND s.family_id IS NOT NULL AND k.family_id <> s.family_id THEN
        v_warnings := v_warnings || jsonb_build_array(jsonb_build_object(
            'code', 'family_mismatch',
            'detail', format('keeper family %s, survivor family %s; keeper wins', k.family_id, s.family_id)));
        v_dropped := v_dropped || jsonb_build_array(jsonb_build_object('field', 'family_id', 'value', s.family_id));
    END IF;

    -- Commercial fields: money never changes silently.
    IF k.unit IS NULL AND s.unit IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('unit', s.unit);
    ELSIF k.unit IS NOT NULL AND s.unit IS NOT NULL AND k.unit <> s.unit THEN
        v_warnings := v_warnings || jsonb_build_array(jsonb_build_object(
            'code', 'commercial_mismatch', 'detail', format('unit %s vs %s; keeper wins', k.unit, s.unit)));
        v_dropped := v_dropped || jsonb_build_array(jsonb_build_object('field', 'unit', 'value', s.unit));
    END IF;
    IF k.currency IS NULL AND s.currency IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('currency', s.currency);
    ELSIF k.currency IS NOT NULL AND s.currency IS NOT NULL AND k.currency <> s.currency THEN
        v_warnings := v_warnings || jsonb_build_array(jsonb_build_object(
            'code', 'commercial_mismatch', 'detail', format('currency %s vs %s; keeper wins', k.currency, s.currency)));
        v_dropped := v_dropped || jsonb_build_array(jsonb_build_object('field', 'currency', 'value', s.currency));
    END IF;
    IF k.cost_basis IS NULL AND s.cost_basis IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('cost_basis', s.cost_basis);
    ELSIF k.cost_basis IS NOT NULL AND s.cost_basis IS NOT NULL AND k.cost_basis <> s.cost_basis THEN
        v_warnings := v_warnings || jsonb_build_array(jsonb_build_object(
            'code', 'commercial_mismatch', 'detail', format('cost_basis %s vs %s; keeper wins', k.cost_basis, s.cost_basis)));
        v_dropped := v_dropped || jsonb_build_array(jsonb_build_object('field', 'cost_basis', 'value', s.cost_basis));
    END IF;
    IF k.lead_time_days IS NULL AND s.lead_time_days IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('lead_time_days', s.lead_time_days);
    ELSIF k.lead_time_days IS NOT NULL AND s.lead_time_days IS NOT NULL AND k.lead_time_days <> s.lead_time_days THEN
        v_warnings := v_warnings || jsonb_build_array(jsonb_build_object(
            'code', 'commercial_mismatch', 'detail', format('lead_time_days %s vs %s; keeper wins', k.lead_time_days, s.lead_time_days)));
        v_dropped := v_dropped || jsonb_build_array(jsonb_build_object('field', 'lead_time_days', 'value', s.lead_time_days));
    END IF;
    IF k.unit_cost IS NULL AND s.unit_cost IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('unit_cost', s.unit_cost);
    ELSIF k.unit_cost IS NOT NULL AND s.unit_cost IS NOT NULL AND k.unit_cost <> s.unit_cost THEN
        v_warnings := v_warnings || jsonb_build_array(jsonb_build_object(
            'code', 'commercial_mismatch', 'detail', format('unit_cost %s vs %s; keeper wins', k.unit_cost, s.unit_cost)));
        v_dropped := v_dropped || jsonb_build_array(jsonb_build_object('field', 'unit_cost', 'value', s.unit_cost));
    END IF;

    IF coalesce(k.variant_values, '{}'::jsonb) = '{}'::jsonb
       AND coalesce(s.variant_values, '{}'::jsonb) <> '{}'::jsonb THEN
        v_fields := v_fields || jsonb_build_object('variant_values', s.variant_values);
    END IF;
    IF k.image_url IS NULL AND s.image_url IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('image_url', s.image_url);
    END IF;
    IF k.product_url IS NULL AND s.product_url IS NOT NULL THEN
        v_fields := v_fields || jsonb_build_object('product_url', s.product_url);
    END IF;

    -- Attachments: union by url, keeper's element first.
    FOR r IN
        SELECT e, src, ord
          FROM (
                SELECT value AS e, 1 AS src, ordinality AS ord
                  FROM jsonb_array_elements(coalesce(k.attachments, '[]'::jsonb)) WITH ORDINALITY
                UNION ALL
                SELECT value, 2, ordinality
                  FROM jsonb_array_elements(coalesce(s.attachments, '[]'::jsonb)) WITH ORDINALITY
               ) x
         ORDER BY src, ord
    LOOP
        v_key := coalesce(nullif(r.e ->> 'url', ''), r.e::text);
        CONTINUE WHEN v_key = ANY (v_seen);
        v_seen := v_seen || v_key;
        v_att := v_att || jsonb_build_array(r.e);
    END LOOP;
    IF v_att IS DISTINCT FROM coalesce(k.attachments, '[]'::jsonb) THEN
        v_fields := v_fields || jsonb_build_object('attachments', v_att);
    END IF;

    -- Attributes: key union, keeper wins on collision.
    IF coalesce(s.attributes, '{}'::jsonb) || coalesce(k.attributes, '{}'::jsonb)
       IS DISTINCT FROM coalesce(k.attributes, '{}'::jsonb) THEN
        v_fields := v_fields || jsonb_build_object('attributes',
            coalesce(s.attributes, '{}'::jsonb) || coalesce(k.attributes, '{}'::jsonb));
    END IF;

    -- Tags and synonyms: case-insensitive union, deduplicated, sorted for a stable payload.
    SELECT coalesce(array_agg(t ORDER BY lower(t)), '{}'::text[]) INTO v_tags
      FROM (SELECT DISTINCT ON (lower(t)) t
              FROM unnest(coalesce(k.tags, '{}'::text[]) || coalesce(s.tags, '{}'::text[]))
                   WITH ORDINALITY AS u(t, o)
             ORDER BY lower(t), o) z;
    IF v_tags IS DISTINCT FROM coalesce(k.tags, '{}'::text[]) THEN
        v_fields := v_fields || jsonb_build_object('tags', to_jsonb(v_tags));
    END IF;
    SELECT coalesce(array_agg(t ORDER BY lower(t)), '{}'::text[]) INTO v_synonyms
      FROM (SELECT DISTINCT ON (lower(t)) t
              FROM unnest(coalesce(k.synonyms, '{}'::text[]) || coalesce(s.synonyms, '{}'::text[]))
                   WITH ORDINALITY AS u(t, o)
             ORDER BY lower(t), o) z;
    IF v_synonyms IS DISTINCT FROM coalesce(k.synonyms, '{}'::text[]) THEN
        v_fields := v_fields || jsonb_build_object('synonyms', to_jsonb(v_synonyms));
    END IF;

    -- Children: what moves, what is dropped because the keeper already has it.
    SELECT count(*) FILTER (WHERE NOT EXISTS (
               SELECT 1 FROM material_identifier ki
                WHERE ki.material_uid = p_keep AND ki.kind = si.kind
                  AND ki.value = si.value AND ki.source_system = si.source_system)),
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM material_identifier ki
                WHERE ki.material_uid = p_keep AND ki.kind = si.kind
                  AND ki.value = si.value AND ki.source_system = si.source_system))
      INTO v_id_moved, v_id_drop
      FROM material_identifier si WHERE si.material_uid = p_merge;
    SELECT count(*) INTO v_id_conflict
      FROM material_identifier si
     WHERE si.material_uid = p_merge
       AND EXISTS (SELECT 1 FROM material_identifier ki
                    WHERE ki.material_uid = p_keep AND ki.kind = si.kind
                      AND ki.value = si.value AND ki.source_system <> si.source_system);
    IF v_id_conflict > 0 THEN
        v_warnings := v_warnings || jsonb_build_array(jsonb_build_object(
            'code', 'identifier_conflict',
            'detail', format('%s identifier(s) share kind+value with the keeper under another source', v_id_conflict)));
    END IF;

    SELECT count(*) FILTER (WHERE NOT EXISTS (
               SELECT 1 FROM material_measure km
                WHERE km.material_uid = p_keep AND km.key = sm.key
                  AND km.source_system = sm.source_system AND km.valid_from = sm.valid_from)),
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM material_measure km
                WHERE km.material_uid = p_keep AND km.key = sm.key
                  AND km.source_system = sm.source_system AND km.valid_from = sm.valid_from))
      INTO v_meas_moved, v_meas_drop
      FROM material_measure sm WHERE sm.material_uid = p_merge;
    SELECT count(*) FILTER (WHERE sm.is_preferred AND EXISTS (
               SELECT 1 FROM material_measure km
                WHERE km.material_uid = p_keep AND km.key = sm.key AND km.is_preferred))
      INTO v_meas_demote
      FROM material_measure sm WHERE sm.material_uid = p_merge;
    SELECT count(*) INTO v_meas_conf
      FROM material_measure sm
     WHERE sm.material_uid = p_merge AND sm.is_preferred
       AND EXISTS (SELECT 1 FROM material_measure km
                    WHERE km.material_uid = p_keep AND km.key = sm.key AND km.is_preferred
                      AND km.value_num IS DISTINCT FROM sm.value_num);
    IF v_meas_conf > 0 THEN
        v_warnings := v_warnings || jsonb_build_array(jsonb_build_object(
            'code', 'measure_conflict',
            'detail', format('%s preferred measure(s) disagree; the keeper keeps its value and the survivor row becomes non-preferred', v_meas_conf)));
    END IF;

    SELECT count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM material_price kp
                WHERE kp.material_uid = p_keep
                  AND kp.supplier_id IS NOT DISTINCT FROM sp.supplier_id
                  AND kp.valid_from = sp.valid_from
                  AND kp.qty_break = sp.qty_break
                  AND kp.cost_basis = sp.cost_basis)),
           count(*) FILTER (WHERE NOT EXISTS (
               SELECT 1 FROM material_price kp
                WHERE kp.material_uid = p_keep
                  AND kp.supplier_id IS NOT DISTINCT FROM sp.supplier_id
                  AND kp.valid_from = sp.valid_from
                  AND kp.qty_break = sp.qty_break
                  AND kp.cost_basis = sp.cost_basis))
      INTO v_price_drop, v_price_moved
      FROM material_price sp WHERE sp.material_uid = p_merge;

    SELECT count(*) FILTER (WHERE from_uid = p_merge) INTO v_rel_from
      FROM material_relation WHERE from_uid = p_merge OR to_uid = p_merge;
    SELECT count(*) FILTER (WHERE to_uid = p_merge) INTO v_rel_to
      FROM material_relation WHERE from_uid = p_merge OR to_uid = p_merge;
    SELECT count(*) INTO v_rel_self
      FROM material_relation
     WHERE (from_uid = p_merge AND to_uid = p_keep) OR (from_uid = p_keep AND to_uid = p_merge);
    SELECT count(*) INTO v_rel_dup
      FROM material_relation sr
     WHERE (sr.from_uid = p_merge OR sr.to_uid = p_merge)
       AND sr.from_uid <> p_keep AND sr.to_uid <> p_keep
       AND EXISTS (SELECT 1 FROM material_relation kr
                    WHERE kr.kind = sr.kind
                      AND kr.from_uid = CASE WHEN sr.from_uid = p_merge THEN p_keep ELSE sr.from_uid END
                      AND kr.to_uid   = CASE WHEN sr.to_uid   = p_merge THEN p_keep ELSE sr.to_uid   END);

    SELECT count(*) FILTER (WHERE NOT EXISTS (
               SELECT 1 FROM material_classification kc
                WHERE kc.material_uid = p_keep AND kc.system = sc.system AND kc.code = sc.code)),
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM material_classification kc
                WHERE kc.material_uid = p_keep AND kc.system = sc.system AND kc.code = sc.code))
      INTO v_cls_moved, v_cls_drop
      FROM material_classification sc WHERE sc.material_uid = p_merge;

    IF EXISTS (SELECT 1 FROM material_epd WHERE material_uid = p_keep) THEN
        IF EXISTS (SELECT 1 FROM material_epd WHERE material_uid = p_merge) THEN
            v_epd_drop := 1;
            v_epd_conf := 1;
            v_warnings := v_warnings || jsonb_build_array(jsonb_build_object(
                'code', 'epd_conflict', 'detail', 'both records carry an EPD; the keeper keeps its own'));
        END IF;
    ELSIF EXISTS (SELECT 1 FROM material_epd WHERE material_uid = p_merge) THEN
        v_epd_moved := 1;
    END IF;

    SELECT count(*) INTO v_usage FROM material_usage WHERE material_uid = p_merge;

    RETURN jsonb_build_object(
        'keep',     p_keep,
        'merge',    p_merge,
        'fields',   v_fields,
        'dropped',  v_dropped,
        'warnings', v_warnings,
        'children', jsonb_build_object(
            'identifiers',     jsonb_build_object('moved', v_id_moved, 'dropped', v_id_drop, 'conflicts', v_id_conflict),
            'measures',        jsonb_build_object('moved', v_meas_moved, 'dropped', v_meas_drop,
                                                  'demoted', v_meas_demote, 'conflicts', v_meas_conf),
            'prices',          jsonb_build_object('moved', v_price_moved, 'dropped', v_price_drop),
            'relations',       jsonb_build_object('from_moved', v_rel_from, 'to_moved', v_rel_to,
                                                  'self_dropped', v_rel_self, 'duplicates_dropped', v_rel_dup),
            'classifications', jsonb_build_object('moved', v_cls_moved, 'dropped', v_cls_drop),
            'epd',             jsonb_build_object('moved', v_epd_moved, 'dropped', v_epd_drop, 'conflicts', v_epd_conf),
            'usage',           jsonb_build_object('merged', v_usage)));
END $$;

COMMENT ON FUNCTION material_merge_plan(text, text) IS
    'Computes the merge plan (fields, dropped values, warnings, child moves) without writing.';

-- ---------------------------------------------------------------------------
-- material_merge_preview - the plan for one pair, plus whether it can merge at all
-- ---------------------------------------------------------------------------
-- Read-only. Accepts the pair in either order and defaults `keep` to the keeper suggestion, so
-- the UI can show "what would happen" before anyone decides.
CREATE OR REPLACE FUNCTION material_merge_preview(
    p_uid_a text,
    p_uid_b text,
    p_keep  text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_a        text := least(p_uid_a, p_uid_b);
    v_b        text := greatest(p_uid_a, p_uid_b);
    v_keep     text;
    v_other    text;
    v_sugg     text;
    a          material;
    b          material;
    v_plan     jsonb;
    v_blockers jsonb := '[]'::jsonb;
    v_pair     jsonb := NULL;
BEGIN
    SELECT * INTO a FROM material WHERE material_uid = v_a;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT * INTO b FROM material WHERE material_uid = v_b;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    v_sugg  := ml_duplicate_keeper(v_a, v_b);
    v_keep  := coalesce(p_keep, v_sugg);
    IF v_keep NOT IN (v_a, v_b) THEN
        RAISE EXCEPTION 'keep must be one of the two pair members' USING ERRCODE = '22023';
    END IF;
    v_other := CASE WHEN v_keep = v_a THEN v_b ELSE v_a END;

    IF (CASE WHEN v_keep = v_a THEN a.status ELSE b.status END) NOT IN ('active', 'draft') THEN
        v_blockers := v_blockers || jsonb_build_array('keeper_status');
    END IF;
    IF (CASE WHEN v_keep = v_a THEN b.status ELSE a.status END) NOT IN ('active', 'draft') THEN
        v_blockers := v_blockers || jsonb_build_array('survivor_status');
    END IF;
    IF a.category_code IS DISTINCT FROM b.category_code THEN
        v_blockers := v_blockers || jsonb_build_array('category_mismatch');
    END IF;
    IF (CASE WHEN v_keep = v_a THEN b.superseded_by ELSE a.superseded_by END) IS NOT NULL
       AND (CASE WHEN v_keep = v_a THEN b.superseded_by ELSE a.superseded_by END) <> v_keep THEN
        v_blockers := v_blockers || jsonb_build_array('already_superseded');
    END IF;

    v_plan := material_merge_plan(v_keep, v_other);

    SELECT jsonb_build_object('uid_a', uid_a, 'uid_b', uid_b, 'score', score, 'status', status)
      INTO v_pair
      FROM material_duplicate_candidate
     WHERE uid_a = v_a AND uid_b = v_b;

    RETURN v_plan || jsonb_build_object(
        'suggestion', v_sugg,
        'pair',       v_pair,
        'blockers',   v_blockers,
        'can_merge',  jsonb_array_length(v_blockers) = 0);
END $$;

COMMENT ON FUNCTION material_merge_preview(text, text, text) IS
    'Read-only merge preview: plan, warnings, keeper suggestion and blockers for one pair.';

-- ---------------------------------------------------------------------------
-- material_duplicate_queue - the review list, one call per page
-- ---------------------------------------------------------------------------
-- p_status is 'actionable' by default, which is 'open' plus 'ignored': what an operator can still
-- do something about. 'merged' and 'all' exist for audit.
CREATE OR REPLACE FUNCTION material_duplicate_queue(
    p_status text DEFAULT 'actionable',
    p_limit  integer DEFAULT 50,
    p_offset integer DEFAULT 0
) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_status text := lower(coalesce(nullif(btrim(p_status), ''), 'actionable'));
    v_limit  integer := least(greatest(coalesce(p_limit, 50), 1), 500);
    v_offset integer := greatest(coalesce(p_offset, 0), 0);
    v_rows   jsonb;
    v_total  bigint;
BEGIN
    IF v_status NOT IN ('actionable', 'open', 'ignored', 'merged', 'all') THEN
        RAISE EXCEPTION 'status must be actionable, open, ignored, merged or all (got %)', v_status
            USING ERRCODE = '22023';
    END IF;

    SELECT coalesce(jsonb_agg(to_jsonb(x) - 'total' ORDER BY x.score DESC, x.uid_a, x.uid_b), '[]'::jsonb),
           coalesce(max(x.total), 0)
      INTO v_rows, v_total
      FROM (
            SELECT c.uid_a,
                   c.uid_b,
                   c.score,
                   c.status,
                   ml_duplicate_keeper(c.uid_a, c.uid_b) AS keep_suggestion,
                   ma.name        AS name_a,
                   mb.name        AS name_b,
                   ma.category_code,
                   ma.unit        AS unit_a,
                   mb.unit        AS unit_b,
                   ma.unit_cost   AS unit_cost_a,
                   mb.unit_cost   AS unit_cost_b,
                   c.created_at,
                   c.decided_at,
                   c.decided_by,
                   count(*) OVER () AS total
              FROM material_duplicate_candidate c
              JOIN material ma ON ma.material_uid = c.uid_a
              JOIN material mb ON mb.material_uid = c.uid_b
             WHERE CASE v_status
                       WHEN 'actionable' THEN c.status IN ('open', 'ignored')
                       WHEN 'all'        THEN true
                       ELSE c.status = v_status
                   END
             ORDER BY c.score DESC, c.uid_a, c.uid_b
             LIMIT v_limit OFFSET v_offset
           ) x;

    RETURN jsonb_build_object('status',   v_status,
                              'total',    v_total,
                              'offset',   v_offset,
                              'limit',    v_limit,
                              'rows',     v_rows);
END $$;

COMMENT ON FUNCTION material_duplicate_queue(text, integer, integer) IS
    'Review list for duplicate candidates with a keeper suggestion per row.';
