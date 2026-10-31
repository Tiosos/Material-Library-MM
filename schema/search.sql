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
CREATE OR REPLACE FUNCTION material_search(
    p_q                text    DEFAULT NULL,
    p_category         text    DEFAULT NULL,
    p_brand            text    DEFAULT NULL,
    p_supplier         text    DEFAULT NULL,
    p_attrs            jsonb   DEFAULT '{}'::jsonb,
    p_include_archived boolean DEFAULT false,
    p_limit            integer DEFAULT 40
) RETURNS TABLE (material_uid text, score real, level text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_q      text := btrim(coalesce(p_q, ''));
    v_limit  integer := least(greatest(coalesce(p_limit, 40), 1), 200);
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

    -- Filter predicates: all go into WHERE and are reused by every branch below
    IF NOT p_include_archived THEN
        v_base := v_base || ' AND m.status <> ''archived''';
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

    -- Empty query: browse (by usage count + name), never return a blank page
    IF v_q = '' THEN
        v_sql := format(
            'SELECT m.material_uid, 0::real AS score, %L::text AS level FROM %s WHERE %s'
            ' ORDER BY coalesce(u.ref_count, 0) DESC, m.name LIMIT %s',
            'browse', v_from, v_base, v_limit);
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
        'SELECT m.material_uid, (%s)::real AS score, %L::text AS level'
        ' FROM %s WHERE %s AND (%s) ORDER BY %s LIMIT %s',
        v_score, v_level, v_from, v_base, v_pred, v_order, v_limit);
    RETURN QUERY EXECUTE v_sql;
END $$;

COMMENT ON FUNCTION material_search(text, text, text, text, jsonb, boolean, integer) IS
    'Forgiving search: five-tier graceful degradation exact → prefix → and → relaxed → fuzzy; all filters pushed down; returns the level actually used this time.';

-- ---------------------------------------------------------------------------
-- Facet: must use the same hit set as the current query/filters, never GROUP BY the whole table
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION material_facets(
    p_q       text    DEFAULT NULL,
    p_category text   DEFAULT NULL,
    p_brand   text    DEFAULT NULL,
    p_supplier text   DEFAULT NULL,
    p_attrs   jsonb   DEFAULT '{}'::jsonb,
    p_per_facet integer DEFAULT 12
) RETURNS TABLE (facet text, value text, cnt bigint)
LANGUAGE sql STABLE AS $$
    WITH hits AS (
        SELECT h.material_uid FROM material_search(p_q, p_category, p_brand, p_supplier, p_attrs, false, 5000) h
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
CREATE OR REPLACE FUNCTION material_search_page(
    p_q        text    DEFAULT NULL,
    p_category text    DEFAULT NULL,
    p_brand    text    DEFAULT NULL,
    p_supplier text    DEFAULT NULL,
    p_attrs    jsonb   DEFAULT '{}'::jsonb,
    p_limit    integer DEFAULT 20
) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH hits AS (
        SELECT * FROM material_search(p_q, p_category, p_brand, p_supplier, p_attrs, false, 5000)
    ),
    page AS (
        SELECT h.material_uid, h.score, h.level
          FROM hits h
         ORDER BY h.score DESC
         LIMIT greatest(p_limit, 1)
    )
    SELECT jsonb_build_object(
        'q', p_q,
        'level', coalesce((SELECT level FROM hits LIMIT 1), 'none'),
        'total', (SELECT count(*) FROM hits),
        'suggestion', CASE WHEN (SELECT count(*) FROM hits) = 0
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
                       'score', round(p.score::numeric, 4)))
              FROM page p JOIN v_material v USING (material_uid)), '[]'::jsonb));
$$;
