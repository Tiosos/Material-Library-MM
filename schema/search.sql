-- =============================================================================
-- Material Library — Search v2
-- Target DB: PostgreSQL 16+ (depends on schema/material.sql and pg_trgm)
--
-- Design notes (cf. docs/data-model-v2.md §4)
--   * The level is decided by "which tier actually matched", not by the code block
--     L0 exact   the whole string equals an identifier (not a prefix, so a single letter is
--                never judged an "exact hit")
--     L1 prefix  single token with length >= 4 hitting an identifier prefix
--     L2 and     all words match (websearch semantics, space = AND)
--     L3 relaxed any word matches, ordered by ts_rank_cd
--     L4 fuzzy   trigram similarity (name/family name/synonyms), plus a "did you mean"
--   * Filters are always pushed down into SQL, never "fetch 40 rows then filter"
--   * Scores use monotonic functions with no within-batch normalisation (otherwise top1 is
--     always 1.0, so no threshold can be set and nothing can be cached)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Main search function: returns material_uid + score + the level that matched
-- ---------------------------------------------------------------------------
-- ---------------------------------------------------------------------------
-- Measure filter fragment: turns a JSON spec into one EXISTS predicate per measure
-- ---------------------------------------------------------------------------
-- A measure filter is a range in whatever unit the caller used, e.g.
--   {"thickness_mm": {"gte": 17.5, "lte": 18.5, "unit": "mm"}}
-- Every stored measure carries a value and its own UCUM code; unit.si_factor converts
-- both sides to the same base, so "18 mm" matches a row stored as "1.8 cm" and a caller
-- can ask in inches without the import having to normalize anything. The dimension must
-- agree (a thickness filter never matches a weight), which is what makes the unit
-- parameter meaningful rather than decorative.
--
-- Raised errors use SQLSTATE 22023 (invalid_parameter_value) so the HTTP edge can answer
-- 400 instead of 500: a malformed spec is the caller's mistake, not a server fault.
CREATE OR REPLACE FUNCTION ml_measure_predicate(p_measures jsonb)
RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    v_key    text;
    v_spec   jsonb;
    v_unit   text;
    v_dim    text;
    v_factor numeric;
    v_gte    numeric;
    v_lte    numeric;
    v_lo     numeric;
    v_hi     numeric;
    v_cond   text;
    v_out    text := '';
BEGIN
    IF p_measures IS NULL OR p_measures = '{}'::jsonb THEN
        RETURN '';
    END IF;
    IF jsonb_typeof(p_measures) <> 'object' THEN
        RAISE EXCEPTION 'measures must be a JSON object keyed by measure name'
            USING ERRCODE = '22023';
    END IF;

    FOR v_key, v_spec IN SELECT e.key, e.value FROM jsonb_each(p_measures) e LOOP
        IF jsonb_typeof(v_spec) <> 'object' THEN
            RAISE EXCEPTION 'measure "%": expected an object like {"gte": 17.5, "unit": "mm"}', v_key
                USING ERRCODE = '22023';
        END IF;
        v_unit := v_spec ->> 'unit';
        IF v_unit IS NULL OR btrim(v_unit) = '' THEN
            RAISE EXCEPTION 'measure "%": "unit" is required', v_key USING ERRCODE = '22023';
        END IF;
        SELECT u.dimension, u.si_factor INTO v_dim, v_factor FROM unit u WHERE u.code = v_unit;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'measure "%": unknown unit "%"', v_key, v_unit USING ERRCODE = '22023';
        END IF;
        IF v_dim IS NULL OR v_factor IS NULL THEN
            RAISE EXCEPTION 'measure "%": unit "%" is not convertible', v_key, v_unit
                USING ERRCODE = '22023';
        END IF;

        BEGIN
            v_gte := (v_spec ->> 'gte')::numeric;
            v_lte := (v_spec ->> 'lte')::numeric;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'measure "%": "gte" and "lte" must be numbers', v_key
                USING ERRCODE = '22023';
        END;
        IF v_gte IS NULL AND v_lte IS NULL THEN
            RAISE EXCEPTION 'measure "%": needs "gte" and/or "lte"', v_key USING ERRCODE = '22023';
        END IF;
        IF v_gte IS NOT NULL AND v_lte IS NOT NULL AND v_gte > v_lte THEN
            RAISE EXCEPTION 'measure "%": "gte" is greater than "lte"', v_key USING ERRCODE = '22023';
        END IF;

        -- Convert the caller's bounds into base units once, instead of per candidate row.
        v_lo := v_gte * v_factor;
        v_hi := v_lte * v_factor;
        v_cond := CASE
            WHEN v_lo IS NOT NULL AND v_hi IS NOT NULL THEN format('BETWEEN %L AND %L', v_lo, v_hi)
            WHEN v_lo IS NOT NULL THEN format('>= %L', v_lo)
            ELSE format('<= %L', v_hi)
        END;
        v_out := v_out || format(
            ' AND EXISTS (SELECT 1 FROM material_measure mk JOIN unit mu ON mu.code = mk.ucum'
            || ' WHERE mk.material_uid = m.material_uid AND mk.key = %L AND mu.dimension = %L'
            || ' AND mk.value_num * mu.si_factor %s)', v_key, v_dim, v_cond);
    END LOOP;

    RETURN v_out;
END;
$$;

COMMENT ON FUNCTION ml_measure_predicate(jsonb) IS
    'Builds the unit-aware AND EXISTS predicate for a measures spec; raises SQLSTATE 22023 on a malformed spec.';

-- ---------------------------------------------------------------------------
-- The 9-argument shape replaces the 8-argument one. Dropping first keeps a defaulted
-- overload from making existing positional calls ambiguous.
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS material_search(text, text, text, text, jsonb, boolean, integer, integer);

CREATE OR REPLACE FUNCTION material_search(
    p_q                text    DEFAULT NULL,
    p_category         text    DEFAULT NULL,
    p_brand            text    DEFAULT NULL,
    p_supplier         text    DEFAULT NULL,
    p_attrs            jsonb   DEFAULT '{}'::jsonb,
    p_include_archived boolean DEFAULT false,
    p_limit            integer DEFAULT 40,
    p_offset           integer DEFAULT 0,
    p_measures         jsonb   DEFAULT '{}'::jsonb
) RETURNS TABLE (material_uid text, score real, level text, total bigint)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_q      text := btrim(coalesce(p_q, ''));
    v_limit  integer := least(greatest(coalesce(p_limit, 40), 1), 5000);
    v_offset integer := greatest(coalesce(p_offset, 0), 0);
    v_from   text;
    v_base   text := 'true';
    v_level  text;
    v_pred   text;
    v_score  text;
    v_order  text := 'm.name';
    v_sql    text;
    v_ok     boolean;
    v_tsq    tsquery;
    v_orq    tsquery;
    v_orts   text;
BEGIN
    v_from := 'material m'
           || ' LEFT JOIN party b ON b.id = m.brand_id'
           || ' LEFT JOIN party s ON s.id = m.default_supplier_id'
           || ' LEFT JOIN material_family f ON f.family_id = m.family_id'
           || ' LEFT JOIN material_usage u ON u.material_uid = m.material_uid';

    -- Filter predicates: all go into WHERE and are reused by every branch below.
    -- Superseded rows stay resolvable by uid (material_detail), but must never rank in a
    -- search result: after a merge the dead record would otherwise keep answering queries.
    IF NOT p_include_archived THEN
        v_base := v_base || ' AND m.status NOT IN (''archived'', ''superseded'')';
    END IF;
    IF p_category IS NOT NULL THEN
        v_base := v_base || format(' AND m.category_code = %L', p_category);
    END IF;
    IF p_brand IS NOT NULL THEN
        v_base := v_base || format(' AND b.canonical_name ILIKE %L', '%' || p_brand || '%');
    END IF;
    IF p_supplier IS NOT NULL THEN
        v_base := v_base || format(' AND s.canonical_name ILIKE %L', '%' || p_supplier || '%');
    END IF;
    IF p_attrs IS NOT NULL AND p_attrs <> '{}'::jsonb THEN
        v_base := v_base || format(' AND m.attributes @> %L::jsonb', p_attrs::text);
    END IF;
    -- Unit-aware measure ranges. Kept behind the same v_base string as the other filters,
    -- so every tier (exact .. fuzzy) and the browse branch apply it too.
    IF p_measures IS NOT NULL AND p_measures <> '{}'::jsonb THEN
        v_base := v_base || ml_measure_predicate(p_measures);
    END IF;

    -- Empty query: browse (by usage count + name), never return a blank page
    IF v_q = '' THEN
        v_sql := format(
            'SELECT m.material_uid, 0::real AS score, %L::text AS level,'
            ' count(*) OVER () AS total FROM %s WHERE %s'
            ' ORDER BY coalesce(u.ref_count, 0) DESC, m.name LIMIT %s OFFSET %s',
            'browse', v_from, v_base, v_limit, v_offset);
        RETURN QUERY EXECUTE v_sql;
        RETURN;
    END IF;

    -- ---------------- L0: exact whole identifier ---------------- #
    v_sql := format(
        'SELECT EXISTS (SELECT 1 FROM %s WHERE %s AND EXISTS ('
        '  SELECT 1 FROM material_identifier i WHERE i.material_uid = m.material_uid'
        '    AND lower(i.value) = lower(%L)))', v_from, v_base, v_q);
    EXECUTE v_sql INTO v_ok;
    IF v_ok THEN
        v_level := 'exact';
        v_pred  := format('EXISTS (SELECT 1 FROM material_identifier i'
                          ' WHERE i.material_uid = m.material_uid AND lower(i.value) = lower(%L))', v_q);
        v_score := '1.0';
        v_order := 'm.name';
    END IF;

    -- ---------------- L1: single-token identifier prefix ("blu" → BLUM-xxx) ---------------- #
    IF v_level IS NULL AND v_q !~ '\s' AND length(v_q) >= 4 THEN
        v_sql := format(
            'SELECT EXISTS (SELECT 1 FROM %s WHERE %s AND EXISTS ('
            '  SELECT 1 FROM material_identifier i WHERE i.material_uid = m.material_uid'
            '    AND lower(i.value) LIKE lower(%L)))', v_from, v_base, v_q || '%');
        EXECUTE v_sql INTO v_ok;
        IF v_ok THEN
            v_level := 'prefix';
            v_pred  := format('EXISTS (SELECT 1 FROM material_identifier i'
                              ' WHERE i.material_uid = m.material_uid AND lower(i.value) LIKE lower(%L))',
                              v_q || '%');
            v_score := '0.9';
            v_order := 'm.name';
        END IF;
    END IF;

    -- ---------------- L2: all words match (AND) ---------------- #
    IF v_level IS NULL THEN
        v_tsq := websearch_to_tsquery('simple', v_q);
        IF v_tsq IS NOT NULL AND v_tsq::text <> '' THEN
            v_sql := format('SELECT EXISTS (SELECT 1 FROM %s WHERE %s AND m.search_tsv @@ %L::tsquery)',
                            v_from, v_base, v_tsq::text);
            EXECUTE v_sql INTO v_ok;
            IF v_ok THEN
                v_level := 'and';
                v_pred  := format('m.search_tsv @@ %L::tsquery', v_tsq::text);
                v_score := format('ts_rank_cd(%L::float4[], m.search_tsv, %L::tsquery)',
                                  '{0.1,0.2,0.4,1.0}', v_tsq::text);
                v_order := 'score DESC, coalesce(u.ref_count, 0) DESC';
            END IF;
        END IF;
    END IF;

    -- ---------------- L3: relax to OR (still return results when 450 in "blum hinge 450" does not exist) ---------------- #
    IF v_level IS NULL THEN
        SELECT string_agg(t, ' | ') INTO v_orts
          FROM (SELECT nullif(regexp_replace(tok, '[^a-zA-Z0-9]+', '', 'g'), '') AS t
                  FROM unnest(regexp_split_to_array(lower(v_q), '\s+')) tok) x
         WHERE t IS NOT NULL;
        IF v_orts IS NOT NULL AND v_orts <> '' THEN
            v_orq := to_tsquery('simple', v_orts);
            IF v_orq IS NOT NULL AND v_orq::text <> '' THEN
                v_sql := format('SELECT EXISTS (SELECT 1 FROM %s WHERE %s AND m.search_tsv @@ %L::tsquery)',
                                v_from, v_base, v_orq::text);
                EXECUTE v_sql INTO v_ok;
                IF v_ok THEN
                    v_level := 'relaxed';
                    v_pred  := format('m.search_tsv @@ %L::tsquery', v_orq::text);
                    v_score := format('ts_rank_cd(%L::float4[], m.search_tsv, %L::tsquery)',
                                      '{0.1,0.2,0.4,1.0}', v_orq::text);
                    v_order := 'score DESC, coalesce(u.ref_count, 0) DESC';
                END IF;
            END IF;
        END IF;
    END IF;

    -- ---------------- L4: fuzzy matching (trigram, compared per "word" rather than the whole string) ---------------- #
    -- Single characters never enter the fuzzy tier: otherwise "b" would fuzz up a pile of results
    IF v_level IS NULL AND length(v_q) >= 3 THEN
        v_sql := format(
            'SELECT EXISTS (SELECT 1 FROM %s WHERE %s AND ('
            '  word_similarity(%L, m.name) > 0.45'
            '  OR word_similarity(%L, coalesce(f.display_name, '''')) > 0.45'
            '  OR EXISTS (SELECT 1 FROM unnest(m.synonyms) syn WHERE word_similarity(%L, syn) > 0.45)))',
            v_from, v_base, v_q, v_q, v_q);
        EXECUTE v_sql INTO v_ok;
        IF v_ok THEN
            v_level := 'fuzzy';
            v_pred  := format(
                '(word_similarity(%L, m.name) > 0.45'
                ' OR word_similarity(%L, coalesce(f.display_name, '''')) > 0.45'
                ' OR EXISTS (SELECT 1 FROM unnest(m.synonyms) syn WHERE word_similarity(%L, syn) > 0.45))',
                v_q, v_q, v_q);
            v_score := format(
                'greatest(similarity(m.name, %L), word_similarity(%L, m.name))', v_q, v_q);
            v_order := 'score DESC, coalesce(u.ref_count, 0) DESC';
        END IF;
    END IF;

    -- Nothing matched at all
    IF v_level IS NULL THEN
        v_level := 'none';
        v_pred  := 'false';
        v_score := '0.0';
    END IF;

    v_sql := format(
        'SELECT m.material_uid, (%s)::real AS score, %L::text AS level,'
        ' count(*) OVER () AS total'
        ' FROM %s WHERE %s AND (%s) ORDER BY %s LIMIT %s OFFSET %s',
        v_score, v_level, v_from, v_base, v_pred, v_order, v_limit, v_offset);
    RETURN QUERY EXECUTE v_sql;
END $$;

COMMENT ON FUNCTION material_search(text, text, text, text, jsonb, boolean, integer, integer, jsonb) IS
    'Forgiving search: five-tier graceful degradation exact → prefix → and → relaxed → fuzzy; all filters pushed down; returns the level actually used this time, the true match total, and a page starting at p_offset.';

-- ---------------------------------------------------------------------------
-- Facet: must use the same hit set as the current query/filters, never GROUP BY the whole table
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION material_facets(
    p_q       text    DEFAULT NULL,
    p_category text   DEFAULT NULL,
    p_brand   text    DEFAULT NULL,
    p_supplier text   DEFAULT NULL,
    p_attrs   jsonb   DEFAULT '{}'::jsonb,
    p_per_facet integer DEFAULT 12,
    p_measures jsonb  DEFAULT '{}'::jsonb
) RETURNS TABLE (facet text, value text, cnt bigint)
LANGUAGE sql STABLE AS $$
    WITH hits AS (
        SELECT h.material_uid FROM material_search(p_q, p_category, p_brand, p_supplier, p_attrs, false, 5000, 0, p_measures) h
    ),
    raw AS (
        SELECT 'category'::text AS facet, m.category_code::text AS value, count(*)::bigint AS cnt
          FROM hits h JOIN material m USING (material_uid) GROUP BY 2
        UNION ALL
        SELECT 'brand', coalesce(b.canonical_name, '(none)'), count(*)::bigint
          FROM hits h JOIN material m USING (material_uid) LEFT JOIN party b ON b.id = m.brand_id GROUP BY 2
        UNION ALL
        SELECT 'supplier', coalesce(s.canonical_name, '(none)'), count(*)::bigint
          FROM hits h JOIN material m USING (material_uid) LEFT JOIN party s ON s.id = m.default_supplier_id GROUP BY 2
        UNION ALL
        SELECT 'unit', coalesce(m.unit, '(none)'), count(*)::bigint
          FROM hits h JOIN material m USING (material_uid) GROUP BY 2
    ),
    ranked AS (
        SELECT r.*, row_number() OVER (PARTITION BY r.facet ORDER BY r.cnt DESC, r.value) AS rn
          FROM raw r
    )
    SELECT facet, value, cnt FROM ranked WHERE rn <= greatest(p_per_facet, 1)
     ORDER BY facet, cnt DESC, value;
$$;

-- ---------------------------------------------------------------------------
-- Spelling suggestion: only used for "no hit" or "fuzzy hit"; candidates come from name/brand/family name
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION material_suggest(p_q text, p_limit integer DEFAULT 5)
RETURNS TABLE (suggestion text, sim real)
LANGUAGE sql STABLE AS $$
    WITH cand AS (
        SELECT DISTINCT m.name AS c FROM material m
        UNION SELECT DISTINCT p.canonical_name FROM party p
        UNION SELECT DISTINCT f.display_name FROM material_family f
    )
    SELECT c, similarity(c, p_q)::real AS sim
      FROM cand
     WHERE c % p_q
     ORDER BY sim DESC, length(c)
     LIMIT greatest(p_limit, 1);
$$;

-- ---------------------------------------------------------------------------
-- One-shot API payload: level + total + results (including family/measures)
-- ---------------------------------------------------------------------------
-- One-shot API payload: level + total + results (including family/measures)
-- ---------------------------------------------------------------------------
-- p_offset moves the page window; total always counts the whole match set, so a client on page
-- 9 still learns how many matches exist. `offset` and `limit` are echoed so the HTTP edge can
-- build an opaque cursor without re-deriving the clamp.
--
-- p_collapse (default true) answers a shopping question once per product family: the hit is the
-- highest scoring member, ties broken by uid, and it carries `variants` (the other member uids) and
-- `variant_count` (family size). `total` counts groups in that mode, so paging is never short.
-- Pass false for one row per material record (what `expand=variants` does at the edge).
DROP FUNCTION IF EXISTS material_search_page(text, text, text, text, jsonb, integer, integer);
CREATE OR REPLACE FUNCTION material_search_page(
    p_q        text    DEFAULT NULL,
    p_category text    DEFAULT NULL,
    p_brand    text    DEFAULT NULL,
    p_supplier text    DEFAULT NULL,
    p_attrs    jsonb   DEFAULT '{}'::jsonb,
    p_limit    integer DEFAULT 20,
    p_offset   integer DEFAULT 0,
    p_measures jsonb   DEFAULT '{}'::jsonb,
    p_collapse boolean DEFAULT true
) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH hits AS (
        SELECT * FROM material_search(p_q, p_category, p_brand, p_supplier, p_attrs, false, 5000, 0, p_measures)
    ),
    sized AS (
        SELECT coalesce(m.family_id, h.material_uid) AS group_key,
               h.material_uid, h.score, h.level, h.total
          FROM hits h
          JOIN material m ON m.material_uid = h.material_uid
    ),
    sizes AS (
        SELECT s.group_key,
               count(*) AS member_count,
               array_agg(s.material_uid ORDER BY s.score DESC, s.material_uid) AS members
          FROM sized s
         GROUP BY s.group_key
    ),
    collapsed AS (
        SELECT DISTINCT ON (s.group_key)
               s.material_uid, s.score, s.level, z.member_count, z.members
          FROM sized s
          JOIN sizes z USING (group_key)
         ORDER BY s.group_key, s.score DESC, s.material_uid
    ),
    flat AS (
        SELECT h.material_uid, h.score, h.level, 1 AS member_count, ARRAY[h.material_uid] AS members
          FROM hits h
    ),
    visible AS (
        SELECT * FROM collapsed WHERE coalesce(p_collapse, true)
        UNION ALL
        SELECT * FROM flat WHERE NOT coalesce(p_collapse, true)
    ),
    page AS (
        SELECT * FROM visible
         ORDER BY score DESC, material_uid
         LIMIT greatest(p_limit, 1)
        OFFSET greatest(p_offset, 0)
    ),
    totals AS (
        SELECT CASE WHEN coalesce(p_collapse, true)
                    THEN (SELECT count(*) FROM collapsed)
                    ELSE coalesce((SELECT max(total) FROM hits), 0)
               END AS n
    )
    SELECT jsonb_build_object(
        'q', p_q,
        'level', coalesce((SELECT level FROM page LIMIT 1),
                          (SELECT level FROM visible LIMIT 1),
                          'none'),
        'total', (SELECT n FROM totals),
        'offset', greatest(p_offset, 0),
        'limit', greatest(p_limit, 1),
        'collapsed', coalesce(p_collapse, true),
        'suggestion', CASE WHEN (SELECT n FROM totals) = 0
                           THEN (SELECT suggestion FROM material_suggest(p_q, 1))
                           ELSE NULL END,
        'results', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                       'material_uid', v.material_uid,
                       'name', v.name,
                       'category', v.category_code,
                       'brand', v.brand,
                       'supplier', v.supplier,
                       'family_id', v.family_id,
                       'family_name', v.family_name,
                       'variant_values', v.variant_values,
                       'unit', v.unit,
                       'unit_cost', v.unit_cost,
                       'variant_count', p.member_count,
                       'variants', CASE WHEN coalesce(p_collapse, true)
                                        THEN to_jsonb(array_remove(p.members, p.material_uid))
                                        ELSE '[]'::jsonb END,
                       'score', round(p.score::numeric, 4)))
              FROM page p JOIN v_material v USING (material_uid)), '[]'::jsonb));
$$;
