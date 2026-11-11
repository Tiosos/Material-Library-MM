-- Contract assertions for the joineryflow ETL, runnable with nothing but psql.
--
-- Design: docs/etl-joineryflow.md sections 8, 10 and 11 (delivery step 4).
-- CI runs this against a FRESH database after two imports of the committed
-- snapshot fixtures/joineryflow-hartwood.jsonl. These counts describe that
-- state only: a database that also holds the supplier demo catalogue or the
-- evaluation corpus will not (and should not) satisfy them.
--
-- Three numbers differ from the prose in the design document, on purpose:
--   * 13 prices, not 14. Decision D2 keeps equipment_hire.rate in attributes
--     because the unit seed has no 'day' code, so the hire row has no price.
--   * 11 thickness_mm measures, not 10. Ten are derived from the description
--     (source_ref ends with '#size') and one comes from a real thickness_mm
--     column; the design's "ten" counts the derived ones (docs section 8).
--   * rev stays 1 on every row after a re-import. sync_seq may still advance on
--     the 11 rows that carry a measure: ml_child_touch bumps the parent cursor
--     when a child row is rewritten, but ml_material_before sees no meaningful
--     parent change and leaves rev alone. No second revision is written.
--
-- Every check raises, so ON_ERROR_STOP=1 turns a broken contract into a red build.
--
-- Usage: psql -v ON_ERROR_STOP=1 -q -d <database> -f scripts/etl_contract.sql

\set ON_ERROR_STOP on

DO $$
DECLARE
    v_total      bigint;
    v_archived   bigint;
    v_active     bigint;
    v_categories bigint;
    v_spread     text;
    v_browse     bigint;
    v_derived    bigint;
    v_column     bigint;
    v_measures   bigint;
    v_values     text;
    v_hits       bigint;
    v_families   bigint;
    v_singles    bigint;
    v_axis_leaks bigint;
    v_prices     bigint;
    v_price_src  bigint;
    v_payload    bigint;
    v_cv         bigint;
    v_revs       bigint;
    v_rev_rows   bigint;
    v_updates    bigint;
BEGIN
    -- 1. Row counts: 18 active materials, no archived rows, six categories,
    --    and all 18 visible to the expanded browse page.
    SELECT count(*),
           count(*) FILTER (WHERE status = 'archived'),
           count(*) FILTER (WHERE status = 'active')
      INTO v_total, v_archived, v_active
      FROM material;
    IF v_total <> 18 OR v_archived <> 0 OR v_active <> 18 THEN
        RAISE EXCEPTION 'expected 18 active / 0 archived / 18 total, got % / % / %',
            v_active, v_archived, v_total;
    END IF;

    SELECT count(DISTINCT category_code) INTO v_categories FROM material;
    IF v_categories <> 6 THEN
        RAISE EXCEPTION 'expected 6 categories with rows, got %', v_categories;
    END IF;
    SELECT string_agg(category_code || ':' || n, ' ' ORDER BY category_code)
      INTO v_spread
      FROM (SELECT category_code, count(*) AS n FROM material GROUP BY 1) AS s;
    IF v_spread <> 'appliance:1 benchtop:1 board:10 custom:1 hardware:4 hire:1' THEN
        RAISE EXCEPTION 'category spread is %', v_spread;
    END IF;

    SELECT coalesce((material_search_page(p_limit => 100, p_collapse => false)
                     ->> 'total')::bigint, 0) INTO v_browse;
    IF v_browse <> 18 THEN
        RAISE EXCEPTION 'expanded browse total is %, expected 18', v_browse;
    END IF;

    -- 2. Measures: ten derived #size values, one real column value, eleven
    --    thickness_mm rows, all stored in mm. The derived values are the exact
    --    multiset named in docs section 8, so a name-parsing regression shows here.
    SELECT count(*) FILTER (WHERE source_ref LIKE '%#size'),
           count(*) FILTER (WHERE source_ref IS NULL OR source_ref NOT LIKE '%#size'),
           count(*)
      INTO v_derived, v_column, v_measures
      FROM material_measure WHERE key = 'thickness_mm';
    IF v_derived <> 10 OR v_column <> 1 OR v_measures <> 11 THEN
        RAISE EXCEPTION 'thickness_mm derived/column/total = %/%/%, expected 10/1/11',
            v_derived, v_column, v_measures;
    END IF;
    SELECT string_agg(v, ',' ORDER BY v) INTO v_values
      FROM (SELECT trim_scale(value_num)::text AS v
              FROM material_measure
             WHERE key = 'thickness_mm' AND source_ref LIKE '%#size') AS s;
    IF v_values <> '12,16,16,18,18,19,19,19,25,32' THEN
        RAISE EXCEPTION 'derived thickness values changed: %', v_values;
    END IF;
    IF EXISTS (SELECT 1 FROM material_measure
                WHERE key = 'thickness_mm' AND ucum <> 'mm') THEN
        RAISE EXCEPTION 'a thickness_mm measure is not stored in mm';
    END IF;

    -- 3. Search: the two 18 mm boards are found by name, and the same two are
    --    found by a unit-aware measure window in mm, cm and m (docs section 11.4).
    SELECT count(*) INTO v_hits FROM material_search('18mm');
    IF v_hits <> 2 THEN
        RAISE EXCEPTION 'material_search(18mm) returned % rows, expected 2', v_hits;
    END IF;
    SELECT count(*) INTO v_hits
      FROM material_search(p_measures =>
          '{"thickness_mm":{"gte":17.5,"lte":18.5,"unit":"mm"}}'::jsonb);
    IF v_hits <> 2 THEN
        RAISE EXCEPTION 'measure window in mm returned % rows, expected 2', v_hits;
    END IF;
    SELECT count(*) INTO v_hits
      FROM material_search(p_measures =>
          '{"thickness_mm":{"gte":1.75,"lte":1.85,"unit":"cm"}}'::jsonb);
    IF v_hits <> 2 THEN
        RAISE EXCEPTION 'measure window in cm returned % rows, expected 2', v_hits;
    END IF;
    SELECT count(*) INTO v_hits
      FROM material_search(p_measures =>
          '{"thickness_mm":{"gte":0.0175,"lte":0.0185,"unit":"m"}}'::jsonb);
    IF v_hits <> 2 THEN
        RAISE EXCEPTION 'measure window in m returned % rows, expected 2', v_hits;
    END IF;

    -- 4. Families: 17 single-member families, and every variant_values key is
    --    declared by its family's axis_keys (docs section 8).
    SELECT count(*) INTO v_families FROM material_family;
    IF v_families <> 17 THEN
        RAISE EXCEPTION 'expected 17 families, got %', v_families;
    END IF;
    SELECT count(*) INTO v_singles
      FROM (SELECT family_id FROM material WHERE family_id IS NOT NULL
             GROUP BY family_id HAVING count(*) = 1) AS s;
    IF v_singles <> 17 THEN
        RAISE EXCEPTION 'expected 17 single-member families, got %', v_singles;
    END IF;
    SELECT count(*) INTO v_axis_leaks
      FROM material m JOIN material_family f USING (family_id)
     WHERE EXISTS (SELECT 1 FROM jsonb_object_keys(m.variant_values) AS k(v)
                    WHERE NOT (k.v = ANY (f.axis_keys)));
    IF v_axis_leaks <> 0 THEN
        RAISE EXCEPTION '% rows declare a variant key outside family axis_keys', v_axis_leaks;
    END IF;

    -- 5. Prices and sparse data: 13 joineryflow price rows, the hire rate stays
    --    an attribute, every row keeps source_payload, and the two CV codes
    --    arrived as identifiers (decisions D2 and D3).
    SELECT count(*), count(*) FILTER (WHERE source_system = 'joineryflow')
      INTO v_prices, v_price_src FROM material_price;
    IF v_prices <> 13 OR v_price_src <> 13 THEN
        RAISE EXCEPTION 'prices total/source = %/%, expected 13/13', v_prices, v_price_src;
    END IF;
    IF EXISTS (SELECT 1 FROM material_price p JOIN material m USING (material_uid)
                WHERE m.category_code = 'hire') THEN
        RAISE EXCEPTION 'equipment_hire.rate became a price row (decision D2)';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM material
                    WHERE category_code = 'hire' AND attributes ? 'rate') THEN
        RAISE EXCEPTION 'the hire rate is missing from attributes (decision D2)';
    END IF;
    SELECT count(*) FILTER (WHERE source_payload IS NOT NULL
                              AND source_payload <> '{}'::jsonb)
      INTO v_payload FROM material;
    IF v_payload <> 18 THEN
        RAISE EXCEPTION 'source_payload is present on % of 18 rows', v_payload;
    END IF;
    SELECT count(*) INTO v_cv FROM material_identifier
     WHERE kind = 'cv_code' AND source_system = 'cv';
    IF v_cv <> 2 THEN
        RAISE EXCEPTION 'expected 2 cv_code identifiers, got %', v_cv;
    END IF;

    -- 6. Idempotency (static evidence): two imports must not create a second
    --    revision or a parent UPDATE event, and rev stays 1 on all 18 rows.
    SELECT count(*) FILTER (WHERE rev <> 1) INTO v_revs FROM material;
    IF v_revs <> 0 THEN
        RAISE EXCEPTION '% materials have rev <> 1 after a re-import', v_revs;
    END IF;
    SELECT count(*) INTO v_rev_rows FROM material_revision;
    IF v_rev_rows <> 18 THEN
        RAISE EXCEPTION 'material_revision has % rows, expected one per material', v_rev_rows;
    END IF;
    SELECT count(*) INTO v_updates FROM material_event
     WHERE entity = 'material' AND op = 'update';
    IF v_updates <> 0 THEN
        RAISE EXCEPTION 'a re-import wrote % parent UPDATE events', v_updates;
    END IF;

    RAISE NOTICE 'etl contract OK: % materials, % categories, % derived measures, % prices, % families',
        v_total, v_categories, v_derived, v_prices, v_families;
END $$;
