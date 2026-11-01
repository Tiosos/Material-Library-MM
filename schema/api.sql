-- ===========================================================================
-- API read functions (design: docs/api-v1.md, sections 4.1 and 4.3)
-- ===========================================================================
-- Rule of the house: SQL owns the payload, HTTP owns the transport. Every document
-- the API returns is assembled here, so psql can assert it and the same SQL can back
-- a future MCP server or a batch export. Nothing in this file writes.
--
-- Load after schema/material.sql (needs v_material, v_family) and schema/search.sql.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- material_detail(uid) -> one UMR v2 document, or NULL when the uid is unknown
-- ---------------------------------------------------------------------------
-- The document is the union of the base row and its child tables, shaped exactly like
-- schema/material.schema.json: that file is the contract, so every key built here must
-- exist in it, and a key must not be invented at the HTTP layer.
--
--   * measures: preferred rows only by default; p_measures_all lifts that for editors.
--   * prices: the last 12 rows, newest first. Price history is unbounded, and a detail
--     page that ships 400 historic prices is a payload nobody renders.
--   * relations: expanded to the other material's name so a client can render
--     substitutes without a second round trip (to_name is an additive v2 key).
--   * superseded rows still resolve here (the uid must never 404 after a merge);
--     only search hides them.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION material_detail(
    p_uid          text,
    p_measures_all boolean DEFAULT false
) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH b AS (
        SELECT m.material_uid, m.rev, m.sync_seq, m.status, m.superseded_by, m.category_code,
               m.product_type, m.name, m.description, m.model_no,
               m.brand_id, m.family_id, m.variant_values,
               m.default_supplier_id, m.default_supplier_ref, m.lead_time_days,
               m.unit, m.currency, m.unit_cost, m.cost_basis,
               m.image_url, m.product_url, m.attachments,
               m.attributes, m.tags, m.synonyms,
               m.source_system, m.source_id, m.imported_at, m.confidence,
               m.created_at, m.updated_at,
               bd.canonical_name AS brand_name,
               sp.canonical_name AS supplier_name,
               f.display_name    AS family_display_name,
               f.axis_keys       AS family_axis_keys
          FROM material m
          LEFT JOIN party bd           ON bd.id = m.brand_id
          LEFT JOIN party sp           ON sp.id = m.default_supplier_id
          LEFT JOIN material_family f  ON f.family_id = m.family_id
         WHERE m.material_uid = p_uid
    )
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM b) THEN NULL ELSE (
        SELECT jsonb_build_object(
            'schema_version', '2',
            'material_uid', b.material_uid,
            'rev', b.rev,
            'sync_seq', b.sync_seq,
            'status', b.status,
            'superseded_by', b.superseded_by,
            'category_code', b.category_code,
            'product_type', b.product_type,
            'name', b.name,
            'description', b.description,
            'model_no', b.model_no,
            'brand', CASE WHEN b.brand_id IS NULL THEN NULL ELSE
                     jsonb_build_object('id', b.brand_id, 'name', b.brand_name) END,
            'supplier', CASE WHEN b.default_supplier_id IS NULL THEN NULL ELSE
                        jsonb_build_object('id', b.default_supplier_id,
                                           'name', b.supplier_name,
                                           'ref', b.default_supplier_ref) END,
            'family', CASE WHEN b.family_id IS NULL THEN NULL ELSE
                      jsonb_build_object('family_id', b.family_id,
                                         'display_name', b.family_display_name,
                                         'axis_keys', to_jsonb(b.family_axis_keys)) END,
            'variant_values', b.variant_values,
            'unit', b.unit,
            'currency', b.currency,
            'unit_cost', b.unit_cost,
            'cost_basis', b.cost_basis,
            'lead_time_days', b.lead_time_days,
            'image_url', b.image_url,
            'product_url', b.product_url,
            'attachments', b.attachments,
            'attributes', b.attributes,
            'measures', coalesce((
                SELECT jsonb_agg(jsonb_build_object(
                           'key', mk.key,
                           'value_num', mk.value_num,
                           'value_text', mk.value_text,
                           'unit', mk.ucum,
                           'tolerance', mk.tolerance,
                           'method', mk.method,
                           'standard', mk.standard,
                           'source_system', mk.source_system,
                           'source_ref', mk.source_ref,
                           'valid_from', mk.valid_from,
                           'valid_to', mk.valid_to,
                           'confidence', mk.confidence,
                           'is_preferred', mk.is_preferred) ORDER BY mk.key)
                  FROM material_measure mk
                 WHERE mk.material_uid = b.material_uid
                   AND (p_measures_all OR mk.is_preferred)), '[]'::jsonb),
            'tags', to_jsonb(b.tags),
            'synonyms', to_jsonb(b.synonyms),
            'classifications', coalesce((
                SELECT jsonb_agg(jsonb_build_object('system', c.system,
                                                    'code', c.code,
                                                    'label', c.label)
                                 ORDER BY c.system, c.code)
                  FROM material_classification c
                 WHERE c.material_uid = b.material_uid), '[]'::jsonb),
            'identifiers', coalesce((
                SELECT jsonb_agg(jsonb_build_object('kind', i.kind,
                                                    'value', i.value,
                                                    'source_system', i.source_system,
                                                    'is_primary', i.is_primary)
                                 ORDER BY i.kind, i.value)
                  FROM material_identifier i
                 WHERE i.material_uid = b.material_uid), '[]'::jsonb),
            'prices', coalesce((
                SELECT jsonb_agg(p.doc ORDER BY p.valid_from DESC, p.price_id DESC)
                  FROM (SELECT jsonb_build_object('supplier', pa.canonical_name,
                                                  'unit_cost', pr.unit_cost,
                                                  'currency', pr.currency,
                                                  'cost_basis', pr.cost_basis,
                                                  'qty_break', pr.qty_break,
                                                  'valid_from', pr.valid_from,
                                                  'valid_to', pr.valid_to,
                                                  'source_system', pr.source_system) AS doc,
                               pr.valid_from, pr.price_id
                          FROM material_price pr
                          LEFT JOIN party pa ON pa.id = pr.supplier_id
                         WHERE pr.material_uid = b.material_uid
                         ORDER BY pr.valid_from DESC, pr.price_id DESC
                         LIMIT 12) p), '[]'::jsonb),
            'relations', coalesce((
                SELECT jsonb_agg(jsonb_build_object('to_uid', r.to_uid,
                                                    'to_name', t.name,
                                                    'kind', r.kind,
                                                    'note', r.note,
                                                    'confidence', r.confidence)
                                 ORDER BY r.kind, r.to_uid)
                  FROM material_relation r
                  JOIN material t ON t.material_uid = r.to_uid
                 WHERE r.from_uid = b.material_uid), '[]'::jsonb),
            'epd', (SELECT jsonb_build_object('epd_ref', e.epd_ref,
                                              'program_operator', e.program_operator,
                                              'registration_no', e.registration_no,
                                              'gwp_a1a3', e.gwp_a1a3,
                                              'gwp_total', e.gwp_total,
                                              'recycled_content_pct', e.recycled_content_pct,
                                              'valid_from', e.valid_from,
                                              'valid_to', e.valid_to,
                                              'source_url', e.source_url,
                                              'format', e.format)
                      FROM material_epd e
                     WHERE e.material_uid = b.material_uid),
            'source_system', b.source_system,
            'source_id', b.source_id,
            'imported_at', b.imported_at,
            'confidence', b.confidence,
            'created_at', b.created_at,
            'updated_at', b.updated_at)
          FROM b
    ) END;
$$;

COMMENT ON FUNCTION material_detail(text, boolean) IS
    'One UMR v2 document for a uid (schema/material.schema.json is the contract). NULL for an unknown uid; superseded rows still resolve so links survive a merge.';

-- ---------------------------------------------------------------------------
-- material_family(uid) -> the collapsed family row plus its variant records
-- ---------------------------------------------------------------------------
-- A family is an index, not a record: 16/18/25 mm are three material rows that share
-- one family_id. The API serves this shape so a client can show "3 variants" without
-- pretending the three rows are three different products.
-- NULL when the uid is unknown or the material has no family (the API maps that to 404).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION material_family(p_uid text) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH target AS (
        SELECT family_id FROM material WHERE material_uid = p_uid AND family_id IS NOT NULL
    ),
    fam AS (
        SELECT v.* FROM v_family v JOIN target t USING (family_id)
    )
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM fam) THEN NULL ELSE (
        SELECT jsonb_build_object(
            'family', jsonb_build_object('family_id', f.family_id,
                                         'display_name', f.display_name,
                                         'brand', f.brand,
                                         'axis_keys', to_jsonb(f.axis_keys),
                                         'member_count', f.member_count,
                                         'min_cost', f.min_cost,
                                         'max_cost', f.max_cost,
                                         'min_thickness_mm', f.min_thickness_mm,
                                         'max_thickness_mm', f.max_thickness_mm),
            'members', coalesce((
                SELECT jsonb_agg(jsonb_build_object('material_uid', m.material_uid,
                                                    'name', m.name,
                                                    'status', m.status,
                                                    'variant_values', m.variant_values,
                                                    'unit', m.unit,
                                                    'unit_cost', m.unit_cost)
                                 ORDER BY m.name, m.material_uid)
                  FROM material m
                  JOIN target t ON t.family_id = m.family_id
                 WHERE m.status <> 'archived'), '[]'::jsonb))
          FROM fam f
    ) END;
$$;

COMMENT ON FUNCTION material_family(text) IS
    'Collapsed family row (v_family) plus its variant member records. NULL when the uid is unknown or has no family.';

-- ---------------------------------------------------------------------------
-- material_changes(since, limit) -> the incremental sync feed
-- ---------------------------------------------------------------------------
-- Pull with the sync_seq of the last record you saw, not with updated_at: bulk imports
-- and child-table edits bump sync_seq through triggers (schema/material.sql:162), updated_at
-- does not. next_since is the cursor for the next poll; when the page is empty it equals
-- p_since so a client can keep polling with the same value.
--
-- The feed is row-shaped, not UMR-shaped: a sync consumer needs to know *what* changed
-- (status, superseded_by, rev) far more than it needs the 40-field document, and the uid
-- lets it fetch the document only for the rows it cares about.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION material_changes(
    p_since bigint  DEFAULT 0,
    p_limit integer DEFAULT 500
) RETURNS TABLE (
    material_uid    text,
    rev             integer,
    sync_seq        bigint,
    status          text,
    superseded_by   text,
    name            text,
    category_code   text,
    brand           text,
    supplier        text,
    unit            text,
    unit_cost       numeric,
    currency        text,
    updated_at      timestamptz,
    total_available bigint,
    next_since      bigint
)
LANGUAGE sql STABLE AS $$
    WITH backlog AS (
        SELECT v.material_uid,
               v.rev,
               v.sync_seq,
               v.status,
               m.superseded_by,
               v.name,
               v.category_code,
               v.brand,
               v.supplier,
               v.unit,
               v.unit_cost,
               v.currency::text AS currency,
               v.updated_at
          FROM v_material v
          JOIN material m ON m.material_uid = v.material_uid
         WHERE v.sync_seq > coalesce(p_since, 0)
    ),
    page AS (
        SELECT *
          FROM backlog
         ORDER BY sync_seq
         LIMIT least(greatest(coalesce(p_limit, 500), 1), 5000)
    ),
    meta AS (
        SELECT (SELECT count(*) FROM backlog)::bigint                                AS total_available,
               coalesce((SELECT max(sync_seq) FROM page), coalesce(p_since, 0))       AS next_since
    )
    SELECT p.material_uid,
           p.rev,
           p.sync_seq,
           p.status,
           p.superseded_by,
           p.name,
           p.category_code,
           p.brand,
           p.supplier,
           p.unit,
           p.unit_cost,
           p.currency,
           p.updated_at,
           m.total_available,
           m.next_since
      FROM page p
     CROSS JOIN meta m
     ORDER BY p.sync_seq;
$$;

COMMENT ON FUNCTION material_changes(bigint, integer) IS
    'Incremental sync feed: rows with sync_seq > p_since ordered by sync_seq, with total_available and next_since for the next poll.';

-- ---------------------------------------------------------------------------
-- material_export() -> every record as a UMR v2 document
-- ---------------------------------------------------------------------------
-- The payload of /v1/export. Encoding (JSONL lines or CSV columns) is a transport
-- concern and stays in the HTTP layer; what this function guarantees is that every row
-- is the same document material_detail returns, so an exported file can be validated
-- against schema/material.schema.json as a whole.
-- Archived rows are left out (they are the deleted state); superseded rows are included
-- by default because an export is a snapshot, not a search.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION material_export(p_include_archived boolean DEFAULT false)
RETURNS TABLE (material_uid text, doc jsonb)
LANGUAGE sql STABLE AS $$
    SELECT m.material_uid, material_detail(m.material_uid)
      FROM material m
     WHERE p_include_archived OR m.status <> 'archived'
     ORDER BY m.material_uid;
$$;

COMMENT ON FUNCTION material_export(boolean) IS
    'Every non-archived record as a UMR v2 document (same shape as material_detail), ordered by material_uid.';
