-- =============================================================================
-- Material Library — Universal Material Record (UMR)  v2
-- Target DB: PostgreSQL 16+   extensions: pg_trgm / unaccent
--
-- Changes in v2 vs v1 (see docs/data-model-v2.md)
--   1. Fixes DDL that v1 could not execute: search_tsv was a "generated column with a
--      subquery" and PG rejects it outright; it is now a plain column maintained by a
--      trigger (generation expressions may only use immutable functions, no subqueries)
--   2. Adds the category tree material_category (v1 had only 11 flat category values)
--   3. Adds supplier/brand entity party (v1 used bare text, so Laminex / Laminex Australia split into two facets)
--   4. Adds unit table unit + measure table material_measure (v1 used bare JSON, so 18 and 1.8cm were not comparable)
--   5. Adds product family material_family (16/18/25mm of the same supplier and material form one family, but remain 3 records)
--   6. Adds revision material_revision + event material_event (v1's rev had no history, so ?since= was unreliable)
--   7. Adds approval-based write-back material_change_request (one diff request per change)
--   8. Adds a home for completeness / dedupe / EPD
--
-- Invariants
--   * One row = one purchasable/SKU-level record, material_uid is permanently stable, external systems store only it
--   * Soft archive only, hard delete is forbidden (blocked by trigger)
--   * Every write path bumps material.sync_seq; incremental sync trusts it alone, not timestamps
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE SEQUENCE IF NOT EXISTS ml_sync_seq;

-- ---------------------------------------------------------------------------
-- 1. Category tree: a hierarchy such as Board > Melamine Board > MDF
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_category (
    code        text PRIMARY KEY,
    parent_code text REFERENCES material_category (code) ON DELETE RESTRICT,
    name        text NOT NULL,
    level       smallint NOT NULL DEFAULT 1,
    path        text NOT NULL,
    sort_order  integer NOT NULL DEFAULT 100,
    CONSTRAINT material_category_code_ck CHECK (code ~ '^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$')
);
CREATE INDEX IF NOT EXISTS material_category_parent_idx ON material_category (parent_code, sort_order);

COMMENT ON TABLE material_category IS
    'Category tree. v1 used the 11 flat strings in material.category, which cannot express multi-level categories.';

-- ---------------------------------------------------------------------------
-- 2. Supplier / brand entity + aliases (v1 used bare text, so the same company split into multiple facets)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS party (
    id             bigserial PRIMARY KEY,
    kind           text NOT NULL CHECK (kind IN ('supplier', 'brand', 'manufacturer', 'both')),
    canonical_name text NOT NULL,
    country        char(2),
    website        text,
    notes          text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS party_name_uidx ON party (lower(canonical_name), kind);

CREATE TABLE IF NOT EXISTS party_alias (
    party_id bigint NOT NULL REFERENCES party (id) ON DELETE CASCADE,
    alias    text NOT NULL,
    PRIMARY KEY (party_id, alias)
);
CREATE UNIQUE INDEX IF NOT EXISTS party_alias_uidx ON party_alias (lower(alias));

-- ---------------------------------------------------------------------------
-- 3. Unit table: stores UCUM codes, used for measure and filter conversion
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS unit (
    code      text PRIMARY KEY,
    ucum      text,
    name      text NOT NULL,
    dimension text NOT NULL,
    si_factor numeric(20, 10) NOT NULL DEFAULT 1,
    CONSTRAINT unit_dimension_ck CHECK (dimension IN
        ('length','area','volume','mass','time','power','voltage','current','temperature','count','angle'))
);

-- ---------------------------------------------------------------------------
-- 4. Type profile
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_profile (
    category_code text PRIMARY KEY REFERENCES material_category (code) ON DELETE RESTRICT,
    display_name  text NOT NULL,
    field_schema  jsonb NOT NULL DEFAULT '{}'::jsonb,
    required      text[] NOT NULL DEFAULT '{}',
    search_weight jsonb NOT NULL DEFAULT '{}'::jsonb,
    unit_options  text[] NOT NULL DEFAULT '{}',
    axis_options  text[] NOT NULL DEFAULT '{}'
);

-- ---------------------------------------------------------------------------
-- 5. Product family: a family is not a record, it is an index. 16/18/25mm are still 3 material rows
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_family (
    family_id    text PRIMARY KEY CHECK (family_id ~ '^fam_[0-9A-HJKMNP-TV-Z]{26}$'),
    family_key   text NOT NULL,
    brand_id     bigint REFERENCES party (id),
    series       text,
    colour       text,
    substrate    text,
    display_name text NOT NULL,
    axis_keys    text[] NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS material_family_key_uidx ON material_family (family_key);

-- ---------------------------------------------------------------------------
-- 6. Core table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material (
    material_uid     text PRIMARY KEY CHECK (material_uid ~ '^mat_[0-9A-HJKMNP-TV-Z]{26}$'),
    rev              integer NOT NULL DEFAULT 1,
    status           text NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','draft','archived','superseded')),
    superseded_by    text REFERENCES material (material_uid),

    category_code    text NOT NULL REFERENCES material_category (code) ON DELETE RESTRICT,
    product_type     text,
    name             text NOT NULL,
    description      text,
    brand_id         bigint REFERENCES party (id),
    model_no         text,

    family_id        text REFERENCES material_family (family_id) ON DELETE SET NULL,
    variant_values   jsonb NOT NULL DEFAULT '{}'::jsonb,

    default_supplier_id  bigint REFERENCES party (id),
    default_supplier_ref text,
    lead_time_days   integer CHECK (lead_time_days IS NULL OR lead_time_days >= 0),
    unit             text REFERENCES unit (code) ON DELETE RESTRICT,
    currency         char(3) NOT NULL DEFAULT 'AUD' CHECK (currency ~ '^[A-Z]{3}$'),
    unit_cost        numeric(14, 4) CHECK (unit_cost IS NULL OR unit_cost >= 0),
    cost_basis       text NOT NULL DEFAULT 'ex_gst'
                     CHECK (cost_basis IN ('ex_gst','inc_gst','ex_tax','inc_tax')),

    image_url        text,
    product_url      text,
    attachments      jsonb NOT NULL DEFAULT '[]'::jsonb,

    attributes       jsonb NOT NULL DEFAULT '{}'::jsonb,
    tags             text[] NOT NULL DEFAULT '{}',
    synonyms         text[] NOT NULL DEFAULT '{}',

    source_system    text,
    source_id        text,
    source_payload   jsonb,
    imported_at      timestamptz,
    confidence       numeric(3, 2) NOT NULL DEFAULT 1.00
                     CHECK (confidence >= 0 AND confidence <= 1),

    search_tsv       tsvector,
    sync_seq         bigint NOT NULL DEFAULT nextval('ml_sync_seq'),

    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    created_by       text,
    updated_by       text
);

COMMENT ON COLUMN material.sync_seq IS
    'Monotonically increasing sync cursor. External systems pull increments with ?since=<sync_seq>; do not use updated_at (bulk imports would be missed).';
COMMENT ON COLUMN material.variant_values IS
    'This record''s values on the family variant axes, e.g. {"thickness_mm": 18}. The axis keys are declared by material_family.axis_keys.';

CREATE INDEX IF NOT EXISTS material_category_idx   ON material (category_code) WHERE status <> 'archived';
CREATE INDEX IF NOT EXISTS material_family_idx     ON material (family_id);
CREATE INDEX IF NOT EXISTS material_sync_idx       ON material (sync_seq);
CREATE INDEX IF NOT EXISTS material_source_idx     ON material (source_system, source_id);
CREATE INDEX IF NOT EXISTS material_attributes_idx ON material USING gin (attributes jsonb_path_ops);
CREATE INDEX IF NOT EXISTS material_tags_idx       ON material USING gin (tags);
CREATE INDEX IF NOT EXISTS material_synonyms_idx   ON material USING gin (synonyms);
CREATE INDEX IF NOT EXISTS material_tsv_idx        ON material USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS material_name_trgm_idx  ON material USING gin (name gin_trgm_ops);

-- External classification codes (ETIM / eCl@ss / UNSPSC / Uniclass)
CREATE TABLE IF NOT EXISTS material_classification (
    material_uid text NOT NULL REFERENCES material (material_uid) ON DELETE CASCADE,
    system       text NOT NULL,
    code         text NOT NULL,
    label        text,
    PRIMARY KEY (material_uid, system, code)
);

-- ---------------------------------------------------------------------------
-- 7. Identifier
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_identifier (
    material_uid  text NOT NULL REFERENCES material (material_uid) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN
                  ('sku','barcode','ean','gtin','supplier_code','legacy_id','catalog_code',
                   'internal_ref','slab_id','model_no','cv_code','contract_ref','other')),
    value         text NOT NULL,
    source_system text NOT NULL DEFAULT 'unknown',
    is_primary    boolean NOT NULL DEFAULT false,
    PRIMARY KEY (kind, value, source_system)
);
CREATE INDEX IF NOT EXISTS material_identifier_uid_idx ON material_identifier (material_uid);
CREATE INDEX IF NOT EXISTS material_identifier_val_idx ON material_identifier (lower(value) text_pattern_ops);

-- ---------------------------------------------------------------------------
-- 8. Measures: numbers you have to stand behind go here, with unit and source
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_measure (
    material_uid  text NOT NULL REFERENCES material (material_uid) ON DELETE CASCADE,
    key           text NOT NULL,
    value_num     numeric(18, 6),
    value_text    text,
    ucum          text,
    tolerance     numeric(18, 6),
    method        text,
    standard      text,
    source_system text NOT NULL DEFAULT 'manual',
    source_ref    text,
    valid_from    date NOT NULL DEFAULT CURRENT_DATE,
    valid_to      date,
    confidence    numeric(3, 2) NOT NULL DEFAULT 1.00 CHECK (confidence >= 0 AND confidence <= 1),
    is_preferred  boolean NOT NULL DEFAULT false,
    PRIMARY KEY (material_uid, key, source_system, valid_from),
    CONSTRAINT material_measure_value_ck CHECK (value_num IS NOT NULL OR value_text IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS material_measure_key_idx ON material_measure (key, material_uid);
CREATE UNIQUE INDEX IF NOT EXISTS material_measure_pref_uidx
    ON material_measure (material_uid, key) WHERE is_preferred;

COMMENT ON TABLE material_measure IS
    'Measures with unit and source. Filtering 18mm converts with unit.si_factor, so 18mm = 1.8cm = 0.018m are always comparable.';

-- ---------------------------------------------------------------------------
-- 9. Price history
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_price (
    price_id      bigserial PRIMARY KEY,
    material_uid  text NOT NULL REFERENCES material (material_uid) ON DELETE CASCADE,
    supplier_id   bigint REFERENCES party (id),
    unit_cost     numeric(14, 4) NOT NULL CHECK (unit_cost >= 0),
    currency      char(3) NOT NULL DEFAULT 'AUD' CHECK (currency ~ '^[A-Z]{3}$'),
    cost_basis    text NOT NULL DEFAULT 'ex_gst'
                  CHECK (cost_basis IN ('ex_gst','inc_gst','ex_tax','inc_tax')),
    qty_break     numeric(12, 3) NOT NULL DEFAULT 0,
    valid_from    date NOT NULL DEFAULT CURRENT_DATE,
    valid_to      date,
    source_system text,
    recorded_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS material_price_uid_idx ON material_price (material_uid, valid_from DESC);
CREATE UNIQUE INDEX IF NOT EXISTS material_price_uidx
    ON material_price (material_uid, supplier_id, valid_from, qty_break, cost_basis);

-- ---------------------------------------------------------------------------
-- 10. Relations
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_relation (
    from_uid      text NOT NULL REFERENCES material (material_uid) ON DELETE CASCADE,
    to_uid        text NOT NULL REFERENCES material (material_uid) ON DELETE CASCADE,
    kind          text NOT NULL
                  CHECK (kind IN ('substitute','accessory','supersedes','bundled_with','similar')),
    note          text,
    bidirectional boolean NOT NULL DEFAULT true,
    confidence    numeric(3, 2) NOT NULL DEFAULT 1.00 CHECK (confidence >= 0 AND confidence <= 1),
    PRIMARY KEY (from_uid, to_uid, kind),
    CONSTRAINT material_relation_self_ck CHECK (from_uid <> to_uid)
);
CREATE INDEX IF NOT EXISTS material_relation_to_idx ON material_relation (to_uid, kind);

-- ---------------------------------------------------------------------------
-- 11. Revisions and events
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_revision (
    material_uid  text NOT NULL REFERENCES material (material_uid) ON DELETE CASCADE,
    rev           integer NOT NULL,
    snapshot      jsonb NOT NULL,
    change_source text,
    changed_by    text,
    changed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (material_uid, rev)
);

CREATE TABLE IF NOT EXISTS material_event (
    seq          bigserial PRIMARY KEY,
    material_uid text,
    entity       text NOT NULL,
    op           text NOT NULL CHECK (op IN ('insert','update','delete')),
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    actor        text,
    at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS material_event_uid_idx ON material_event (material_uid, seq DESC);
CREATE INDEX IF NOT EXISTS material_event_entity_idx ON material_event (entity, at DESC);

-- ---------------------------------------------------------------------------
-- 12. Approval-based write-back
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_change_request (
    id            bigserial PRIMARY KEY,
    material_uid  text NOT NULL REFERENCES material (material_uid) ON DELETE CASCADE,
    target_system text NOT NULL,
    diffs         jsonb NOT NULL,
    reason        text,
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','rejected','applied','failed')),
    requested_by  text NOT NULL,
    requested_at  timestamptz NOT NULL DEFAULT now(),
    decided_by    text,
    decided_at    timestamptz,
    applied_at    timestamptz,
    error         text
);
CREATE INDEX IF NOT EXISTS material_change_request_status_idx ON material_change_request (status, requested_at);

-- ---------------------------------------------------------------------------
-- 13. Usage counts and search instrumentation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_usage (
    material_uid text PRIMARY KEY REFERENCES material (material_uid) ON DELETE CASCADE,
    ref_count    integer NOT NULL DEFAULT 0,
    search_hits  integer NOT NULL DEFAULT 0,
    last_used_at timestamptz
);

CREATE TABLE IF NOT EXISTS search_log (
    id      bigserial PRIMARY KEY,
    q       text,
    level   text,
    total   integer,
    filters jsonb NOT NULL DEFAULT '{}'::jsonb,
    took_ms integer,
    actor   text,
    at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS search_log_at_idx ON search_log (at DESC);

CREATE TABLE IF NOT EXISTS search_click (
    log_id       bigint NOT NULL REFERENCES search_log (id) ON DELETE CASCADE,
    material_uid text NOT NULL,
    rank         integer NOT NULL,
    at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (log_id, material_uid)
);

-- ---------------------------------------------------------------------------
-- 14. Dedupe candidates and EPD placeholder
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS material_duplicate_candidate (
    uid_a      text NOT NULL REFERENCES material (material_uid) ON DELETE CASCADE,
    uid_b      text NOT NULL REFERENCES material (material_uid) ON DELETE CASCADE,
    score      numeric(5, 4) NOT NULL,
    status     text NOT NULL DEFAULT 'open' CHECK (status IN ('open','merged','ignored')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (uid_a, uid_b),
    CONSTRAINT material_duplicate_order_ck CHECK (uid_a < uid_b)
);

CREATE TABLE IF NOT EXISTS material_epd (
    material_uid         text PRIMARY KEY REFERENCES material (material_uid) ON DELETE CASCADE,
    epd_ref              text,
    program_operator     text,
    registration_no      text,
    gwp_a1a3             numeric(14, 4),
    gwp_total            numeric(14, 4),
    recycled_content_pct numeric(5, 2),
    valid_from           date,
    valid_to             date,
    source_url           text,
    format               text,
    raw                  jsonb
);

-- ===========================================================================
-- Triggers
-- ===========================================================================
CREATE OR REPLACE FUNCTION ml_jsonb_changed(p_old jsonb, p_new jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(jsonb_object_agg(coalesce(n.key, o.key),
             jsonb_build_object('before', o.value, 'after', n.value)), '{}'::jsonb)
    FROM jsonb_each(coalesce(p_old, '{}'::jsonb)) o
    FULL JOIN jsonb_each(coalesce(p_new, '{}'::jsonb)) n ON n.key = o.key
    WHERE o.value IS DISTINCT FROM n.value;
$$;

CREATE OR REPLACE FUNCTION ml_tsv_from_row(r material) RETURNS tsvector
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_ids      text;
    v_brand    text;
    v_family   text;
    v_cat      text;
    v_supplier text;
    v_d        text;
BEGIN
    SELECT string_agg(i.value, ' ') INTO v_ids
      FROM material_identifier i WHERE i.material_uid = r.material_uid;
    SELECT p.canonical_name INTO v_brand FROM party p WHERE p.id = r.brand_id;
    SELECT f.display_name INTO v_family FROM material_family f WHERE f.family_id = r.family_id;
    SELECT c.name INTO v_cat FROM material_category c WHERE c.code = r.category_code;
    SELECT p.canonical_name INTO v_supplier FROM party p WHERE p.id = r.default_supplier_id;
    SELECT concat_ws(' ', r.description, r.attributes::text,
                     array_to_string(r.synonyms, ' '), array_to_string(r.tags, ' '),
                     (SELECT string_agg(a.key || ' ' || a.value, ' ')
                        FROM jsonb_each_text(r.attributes) a),
                     (SELECT string_agg(mk.key || ' ' || coalesce(mk.value_num::text, '') || ' '
                                        || coalesce(mk.ucum, ''), ' ')
                        FROM material_measure mk WHERE mk.material_uid = r.material_uid))
      INTO v_d;

    RETURN setweight(to_tsvector('simple', unaccent(coalesce(v_ids, ''))), 'A')
        || setweight(to_tsvector('simple', unaccent(coalesce(r.name, '') || ' ' || coalesce(r.model_no, '')
                                   || ' ' || coalesce(v_brand, '') || ' ' || coalesce(v_family, ''))), 'B')
        || setweight(to_tsvector('simple', unaccent(coalesce(v_cat, '') || ' ' || coalesce(v_supplier, '')
                                   || ' ' || coalesce(r.unit, '') || ' ' || coalesce(r.product_type, ''))), 'C')
        || setweight(to_tsvector('simple', unaccent(coalesce(v_d, ''))), 'D');
END $$;

-- BEFORE: only compute the derived columns into NEW
CREATE OR REPLACE FUNCTION ml_material_before() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_changed    jsonb;
    v_meaningful boolean := false;
    v_forced     boolean := coalesce(current_setting('ml.touch', true), 'off') = 'on';
BEGIN
    NEW.search_tsv := ml_tsv_from_row(NEW);

    IF TG_OP = 'INSERT' THEN
        NEW.rev        := coalesce(NEW.rev, 1);
        NEW.updated_at := now();
        NEW.sync_seq   := nextval('ml_sync_seq');
    ELSE
        v_changed := ml_jsonb_changed(to_jsonb(OLD), to_jsonb(NEW))
                     - ARRAY['search_tsv', 'updated_at', 'sync_seq', 'rev'];
        v_meaningful := (v_changed <> '{}'::jsonb);

        IF v_meaningful OR v_forced THEN
            NEW.updated_at := now();
            NEW.sync_seq   := nextval('ml_sync_seq');
            IF v_meaningful THEN NEW.rev := OLD.rev + 1; ELSE NEW.rev := OLD.rev; END IF;
        ELSE
            NEW.rev        := OLD.rev;
            NEW.updated_at := OLD.updated_at;
            NEW.sync_seq   := OLD.sync_seq;
        END IF;
    END IF;

    RETURN NEW;
END $$;

-- AFTER: write the revision snapshot and the event (during BEFORE the parent row does not exist yet, so the FK would fail)
CREATE OR REPLACE FUNCTION ml_material_after() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_changed    jsonb;
    v_meaningful boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_meaningful := true;
    ELSE
        v_changed := ml_jsonb_changed(to_jsonb(OLD), to_jsonb(NEW))
                     - ARRAY['search_tsv', 'updated_at', 'sync_seq', 'rev'];
        v_meaningful := (v_changed <> '{}'::jsonb);
    END IF;

    IF v_meaningful THEN
        INSERT INTO material_revision (material_uid, rev, snapshot, change_source)
        VALUES (NEW.material_uid, NEW.rev, to_jsonb(NEW) - 'search_tsv',
                coalesce(current_setting('ml.source', true), 'unknown'));
        INSERT INTO material_event (material_uid, entity, op, payload, actor)
        VALUES (NEW.material_uid, 'material', lower(TG_OP),
                CASE WHEN TG_OP = 'INSERT'
                     THEN jsonb_build_object('after', to_jsonb(NEW) - 'search_tsv')
                     ELSE jsonb_build_object('changed', v_changed) END,
                current_setting('ml.actor', true));
    END IF;
    RETURN NULL;
END $$;

-- After a child-table change: refresh the parent search vector and bump the sync cursor
CREATE OR REPLACE FUNCTION ml_child_touch() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_uid text;
BEGIN
    IF TG_TABLE_NAME = 'material_relation' THEN
        v_uid := coalesce((to_jsonb(NEW) ->> 'from_uid'), (to_jsonb(OLD) ->> 'from_uid'));
    ELSE
        v_uid := coalesce((to_jsonb(NEW) ->> 'material_uid'), (to_jsonb(OLD) ->> 'material_uid'));
    END IF;

    IF v_uid IS NOT NULL THEN
        PERFORM set_config('ml.touch', 'on', true);
        UPDATE material SET updated_at = now() WHERE material_uid = v_uid;
        PERFORM set_config('ml.touch', 'off', true);
    END IF;

    INSERT INTO material_event (material_uid, entity, op, payload, actor)
    VALUES (v_uid, TG_TABLE_NAME, lower(TG_OP),
            jsonb_build_object('changed', ml_jsonb_changed(to_jsonb(OLD), to_jsonb(NEW))),
            current_setting('ml.actor', true));
    RETURN NULL;
END $$;

-- Archive only, never hard delete
CREATE OR REPLACE FUNCTION ml_block_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF coalesce(current_setting('ml.allow_delete', true), 'off') <> 'on' THEN
        RAISE EXCEPTION 'Hard delete is not allowed; set status=archived instead (set ml.allow_delete=on for maintenance)';
    END IF;
    RETURN OLD;
END $$;

DROP TRIGGER IF EXISTS trg_material_before ON material;
CREATE TRIGGER trg_material_before BEFORE INSERT OR UPDATE ON material
    FOR EACH ROW EXECUTE FUNCTION ml_material_before();

DROP TRIGGER IF EXISTS trg_material_after ON material;
CREATE TRIGGER trg_material_after AFTER INSERT OR UPDATE ON material
    FOR EACH ROW EXECUTE FUNCTION ml_material_after();

DROP TRIGGER IF EXISTS trg_material_bd ON material;
CREATE TRIGGER trg_material_bd BEFORE DELETE ON material
    FOR EACH ROW EXECUTE FUNCTION ml_block_delete();

DROP TRIGGER IF EXISTS trg_identifier_touch ON material_identifier;
CREATE TRIGGER trg_identifier_touch AFTER INSERT OR UPDATE OR DELETE ON material_identifier
    FOR EACH ROW EXECUTE FUNCTION ml_child_touch();

DROP TRIGGER IF EXISTS trg_measure_touch ON material_measure;
CREATE TRIGGER trg_measure_touch AFTER INSERT OR UPDATE OR DELETE ON material_measure
    FOR EACH ROW EXECUTE FUNCTION ml_child_touch();

DROP TRIGGER IF EXISTS trg_price_touch ON material_price;
CREATE TRIGGER trg_price_touch AFTER INSERT OR UPDATE OR DELETE ON material_price
    FOR EACH ROW EXECUTE FUNCTION ml_child_touch();

DROP TRIGGER IF EXISTS trg_relation_touch ON material_relation;
CREATE TRIGGER trg_relation_touch AFTER INSERT OR UPDATE OR DELETE ON material_relation
    FOR EACH ROW EXECUTE FUNCTION ml_child_touch();

-- ===========================================================================
-- Views
-- ===========================================================================
CREATE OR REPLACE VIEW v_material AS
SELECT m.material_uid,
       m.name,
       m.category_code,
       m.status,
       m.rev,
       m.sync_seq,
       b.canonical_name AS brand,
       s.canonical_name AS supplier,
       m.family_id,
       f.display_name   AS family_name,
       f.axis_keys,
       m.variant_values,
       m.unit,
       m.unit_cost,
       m.currency,
       m.cost_basis,
       m.lead_time_days,
       m.attributes,
       m.synonyms,
       m.tags,
       m.model_no,
       m.source_system,
       m.source_id,
       m.updated_at,
       coalesce((SELECT jsonb_object_agg(mk.key, jsonb_build_object('value', mk.value_num, 'unit', mk.ucum))
                   FROM material_measure mk
                  WHERE mk.material_uid = m.material_uid AND mk.is_preferred), '{}'::jsonb) AS measures
  FROM material m
  LEFT JOIN party b           ON b.id = m.brand_id
  LEFT JOIN party s           ON s.id = m.default_supplier_id
  LEFT JOIN material_family f ON f.family_id = m.family_id;

CREATE OR REPLACE VIEW v_family AS
SELECT f.family_id,
       f.display_name,
       f.brand_id,
       b.canonical_name AS brand,
       f.axis_keys,
       count(m.material_uid) AS member_count,
       min(m.unit_cost)      AS min_cost,
       max(m.unit_cost)      AS max_cost,
       min((m.variant_values ->> 'thickness_mm')::numeric) AS min_thickness_mm,
       max((m.variant_values ->> 'thickness_mm')::numeric) AS max_thickness_mm
  FROM material_family f
  LEFT JOIN party b    ON b.id = f.brand_id
  LEFT JOIN material m ON m.family_id = f.family_id AND m.status <> 'archived'
 GROUP BY f.family_id, f.display_name, f.brand_id, b.canonical_name, f.axis_keys;

-- Completeness: how many of profile.required can be matched (attributes or measure both count)
CREATE OR REPLACE VIEW v_material_completeness AS
SELECT m.material_uid,
       p.category_code,
       p.required,
       cardinality(p.required) AS required_count,
       (SELECT count(*) FROM unnest(p.required) r
         WHERE m.attributes ? r
            OR EXISTS (SELECT 1 FROM material_measure mk
                        WHERE mk.material_uid = m.material_uid AND mk.key = r)) AS present_count
  FROM material m
  JOIN material_profile p ON p.category_code = m.category_code;
