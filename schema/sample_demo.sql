-- =============================================================================
-- Material Library v2 — sample data and acceptance queries
-- Usage: psql -v ON_ERROR_STOP=1 -f schema/sample_demo.sql
-- Notes: 3 boards of the same family (16/18/25mm) + glass + hardware + one duplicate candidate to merge
-- =============================================================================

\echo '== 0. Role declaration (event and change source) =='
SELECT set_config('ml.actor', 'demo', false),
       set_config('ml.source', 'seed', false);

DELETE FROM material_change_request WHERE requested_by = 'demo';

\echo '== 1. Supplier / brand =='
INSERT INTO party (kind, canonical_name, country) VALUES
    ('both',     'Laminex',        'AU'),
    ('brand',    'Polytec',        'AU'),
    ('supplier', 'CDK Stone',      'AU'),
    ('brand',    'Blum',           'AT')
ON CONFLICT DO NOTHING;

\echo '== 2. Product family: same supplier + same series + same colour + same substrate =='
INSERT INTO material_family (family_id, family_key, brand_id, series, colour, substrate, display_name, axis_keys)
SELECT 'fam_01K9XFT7GJSXCA6JANCTXV3XH0',
       'laminex|melamine-mdf|black|mdf',
       p.id, 'Melamine MDF', 'Black', 'MDF',
       'Laminex Melamine MDF Black',
       ARRAY['thickness_mm', 'sheet_len_mm', 'sheet_wid_mm']
  FROM party p WHERE p.canonical_name = 'Laminex'
ON CONFLICT (family_id) DO NOTHING;

\echo '== 3. Three variant records (the family is an index, the records are still 3) =='
INSERT INTO material (material_uid, category_code, product_type, name, description,
                      brand_id, family_id, variant_values, default_supplier_id,
                      unit, unit_cost, attributes, synonyms, tags, source_system, source_id)
SELECT v.uid, 'board.melamine.mdf', 'Board', v.name,
       v.name || ' — double-sided melamine-faced MDF, suitable for carcasses and door panels',
       b.id, 'fam_01K9XFT7GJSXCA6JANCTXV3XH0',
       jsonb_build_object('thickness_mm', v.thickness, 'sheet_len_mm', 3600, 'sheet_wid_mm', 1200),
       s.id, 'sheet', v.cost,
       jsonb_build_object('colour', 'Black', 'substrate', 'MDF', 'finish', 'Melamine',
                          'sided', 'Double', 'sheet_len_mm', 3600, 'sheet_wid_mm', 1200),
       ARRAY['black melamine', 'mdf black', 'black board'],
       ARRAY['common', 'carcass'],
       'seed', v.uid
  FROM (VALUES
        ('mat_01K9XFT7GJSXCA6JANCTXV3XHY', 'Laminex 16mm Black Melamine MDF', 16, 38.00),
        ('mat_01K9XFT7GJSXCA6JANCTXV3XHZ', 'Laminex 18mm Black Melamine MDF', 18, 42.00),
        ('mat_01K9XFT7GJSXCA6JANCTXV3XJ0', 'Laminex 25mm Black Melamine MDF', 25, 58.00)
       ) AS v(uid, name, thickness, cost)
 CROSS JOIN party b CROSS JOIN party s
 WHERE b.canonical_name = 'Laminex' AND s.canonical_name = 'CDK Stone'
ON CONFLICT (material_uid) DO NOTHING;

INSERT INTO material (material_uid, category_code, product_type, name, brand_id, default_supplier_id,
                      unit, unit_cost, attributes, synonyms, source_system, source_id)
SELECT 'mat_01K9XG0000000000000000000A', 'glass.toughened', 'Glass',
       '6mm Toughened Clear Glass', b.id, s.id, 'm2', 120.00,
       '{"glass_type":"Toughened","thickness_mm":6,"edge_finish":"Polished","tint":"Clear"}'::jsonb,
       ARRAY['safety glass', 'tempered glass'], 'seed', 'glass-6tg'
  FROM party b, party s
 WHERE b.canonical_name = 'Polytec' AND s.canonical_name = 'CDK Stone'
ON CONFLICT (material_uid) DO NOTHING;

INSERT INTO material (material_uid, category_code, product_type, name, brand_id, default_supplier_id,
                      unit, unit_cost, attributes, synonyms, source_system, source_id)
SELECT 'mat_01K9XH0000000000000000000B', 'hardware.hinge', 'Hardware',
       'Blumotion Soft Close Hinge 110 deg Clip Top', b.id, b.id, 'ea', 6.80,
       '{"hinge_type":"Concealed","opening_angle_deg":110,"finish":"Nickel","soft_close":true,"pack_qty":1}'::jsonb,
       ARRAY['soft close hinge', 'clip top hinge'], 'seed', 'blum-71b3550'
  FROM party b WHERE b.canonical_name = 'Blum'
ON CONFLICT (material_uid) DO NOTHING;

\echo '== 4. Identifiers (prefix search relies on them) =='
INSERT INTO material_identifier (material_uid, kind, value, source_system, is_primary) VALUES
    ('mat_01K9XFT7GJSXCA6JANCTXV3XHY', 'sku', 'LAM-MEL-BLK-16', 'joineryflow', true),
    ('mat_01K9XFT7GJSXCA6JANCTXV3XHZ', 'sku', 'LAM-MEL-BLK-18', 'joineryflow', true),
    ('mat_01K9XFT7GJSXCA6JANCTXV3XJ0', 'sku', 'LAM-MEL-BLK-25', 'joineryflow', true),
    ('mat_01K9XFT7GJSXCA6JANCTXV3XHZ', 'supplier_code', 'CDK-18-BLK', 'CDK Stone', true),
    ('mat_01K9XG0000000000000000000A', 'sku', 'GJ-TG-6', 'joineryflow', true),
    ('mat_01K9XH0000000000000000000B', 'sku', 'BLUM-71B3550', 'joineryflow', true),
    ('mat_01K9XH0000000000000000000B', 'ean', '9001234567894', 'Blum', true)
ON CONFLICT DO NOTHING;

\echo '== 5. Measures: with unit, source, method (in v1 these were bare numbers in attributes) =='
INSERT INTO material_measure (material_uid, key, value_num, ucum, method, source_system, source_ref, is_preferred)
SELECT m.material_uid, 'thickness_mm',
       (m.variant_values ->> 'thickness_mm')::numeric, 'mm',
       'supplier datasheet', 'CDK Stone', 'CDK price list 2026-10', true
  FROM material m WHERE m.family_id = 'fam_01K9XFT7GJSXCA6JANCTXV3XH0'
ON CONFLICT DO NOTHING;

-- Same material, another source, recorded in cm: verifies unit conversion (1.8 cm = 18 mm)
INSERT INTO material_measure (material_uid, key, value_num, ucum, method, source_system, source_ref, is_preferred)
VALUES ('mat_01K9XFT7GJSXCA6JANCTXV3XHZ', 'thickness_mm', 1.8, 'cm',
        'measured', 'manual', 'site check', false)
ON CONFLICT DO NOTHING;

INSERT INTO material_measure (material_uid, key, value_num, ucum, method, standard, source_system, is_preferred)
VALUES ('mat_01K9XFT7GJSXCA6JANCTXV3XHZ', 'density', 730, 'kg/m3', 'lab test', 'AS/NZS 4266.4', 'manual', true)
ON CONFLICT DO NOTHING;

\echo '== 6. Price history =='
INSERT INTO material_price (material_uid, supplier_id, unit_cost, valid_from, source_system)
SELECT 'mat_01K9XFT7GJSXCA6JANCTXV3XHZ', p.id, 40.00, DATE '2026-01-01', 'joineryflow' FROM party p WHERE p.canonical_name='CDK Stone'
ON CONFLICT DO NOTHING;
INSERT INTO material_price (material_uid, supplier_id, unit_cost, valid_from, source_system)
SELECT 'mat_01K9XFT7GJSXCA6JANCTXV3XHZ', p.id, 42.00, DATE '2026-07-01', 'joineryflow' FROM party p WHERE p.canonical_name='CDK Stone'
ON CONFLICT DO NOTHING;

\echo '== 7. One duplicate candidate to merge (Polytec, same spec) =='
INSERT INTO material (material_uid, category_code, product_type, name, brand_id, default_supplier_id,
                      unit, unit_cost, attributes, source_system, source_id)
SELECT 'mat_01K9XJ0000000000000000000C', 'board.melamine.mdf', 'Board',
       'Polytec 18mm Black Melamine MDF', b.id, s.id, 'sheet', 39.50,
       '{"colour":"Black","substrate":"MDF","finish":"Melamine","thickness_mm":18}'::jsonb,
       'supplier_catalogue', 'poly-18-blk'
  FROM party b, party s WHERE b.canonical_name='Polytec' AND s.canonical_name='CDK Stone'
ON CONFLICT (material_uid) DO NOTHING;

INSERT INTO material_duplicate_candidate (uid_a, uid_b, score, status)
SELECT 'mat_01K9XFT7GJSXCA6JANCTXV3XHZ', 'mat_01K9XJ0000000000000000000C',
       round(similarity('Laminex 18mm Black Melamine MDF', 'Polytec 18mm Black Melamine MDF')::numeric, 4), 'open'
ON CONFLICT DO NOTHING;

-- =============================================================================
-- Acceptance queries
-- =============================================================================
\echo '== A. L0 exact whole identifier (should be exact) =='
SELECT * FROM material_search('LAM-MEL-BLK-18');

\echo '== B. L1 identifier prefix (should be prefix, not exact) =='
SELECT * FROM material_search('LAM-MEL');

\echo '== C. L2 all words match (should be and) =='
SELECT * FROM material_search('laminex black');

\echo '== D. L3 relaxed (hinge is not in the boards, boards should still be returned) =='
SELECT * FROM material_search('laminex black hinge');

\echo '== E. L4 fuzzy matching (laminx → Laminex) =='
SELECT * FROM material_search('laminx');

\echo '== F. A single letter no longer falsely reports exact (should be and/relaxed/none, never exact) =='
SELECT * FROM material_search('b') LIMIT 3;

\echo '== G. Filter push-down: the total is not affected by paging (v1 did "fetch 40 rows then filter") =='
SELECT (p ->> 'total')::int          AS total_with_filter,
       jsonb_array_length(p -> 'results') AS page_size
  FROM (SELECT material_search_page('black', p_attrs => '{"colour":"Black"}'::jsonb, p_limit => 2) AS p) s;

\echo '== H. Unit conversion: 18mm and 1.8cm are treated as the same thickness =='
SELECT m.name,
       mk.value_num, mk.ucum,
       round((mk.value_num * u.si_factor)::numeric, 6) AS metres
  FROM material m
  JOIN material_measure mk ON mk.material_uid = m.material_uid
  JOIN unit u ON u.code = mk.ucum
 WHERE mk.key = 'thickness_mm'
   AND mk.value_num * u.si_factor BETWEEN 0.0175 AND 0.0185
 ORDER BY metres, mk.ucum;

\echo '== I. Product family aggregation: 3 records → 1 family, thickness range 16~25 =='
SELECT display_name, member_count, min_thickness_mm, max_thickness_mm, min_cost, max_cost
  FROM v_family WHERE family_id = 'fam_01K9XFT7GJSXCA6JANCTXV3XH0';

\echo '== J. Facets narrow with the query (after black only black-board brands remain) =='
SELECT * FROM material_facets('black') LIMIT 12;

\echo '== K. One-shot API payload (level + total + results) =='
SELECT jsonb_pretty(material_search_page('laminex black', p_limit => 3));

\echo '== L. Completeness score (profile.required hit rate) =='
SELECT material_uid, required_count, present_count
  FROM v_material_completeness ORDER BY material_uid LIMIT 5;

\echo '== M. Incremental sync: change one price, the cursor advances, only changed rows are pulled =='
SELECT max(sync_seq) AS before_seq FROM material;
INSERT INTO material_price (material_uid, supplier_id, unit_cost, valid_from, source_system)
SELECT 'mat_01K9XFT7GJSXCA6JANCTXV3XHZ', p.id, 43.50, DATE '2026-10-01', 'joineryflow'
  FROM party p WHERE p.canonical_name='CDK Stone'
ON CONFLICT DO NOTHING;
SELECT material_uid, name, sync_seq FROM material
 WHERE sync_seq > (SELECT max(sync_seq) - 2 FROM material)
 ORDER BY sync_seq;

\echo '== N. Event stream and revision history (v1 had only a single rev number) =='
SELECT entity, op, count(*) FROM material_event GROUP BY 1, 2 ORDER BY 1, 2;
SELECT material_uid, rev, change_source, changed_at FROM material_revision ORDER BY changed_at DESC LIMIT 5;

\echo '== O. Approval-based write-back: one pending diff request =='
INSERT INTO material_change_request (material_uid, target_system, diffs, reason, requested_by)
VALUES ('mat_01K9XFT7GJSXCA6JANCTXV3XHZ', 'joineryflow',
        '[{"path":"unit_cost","before":42.00,"after":43.50}]'::jsonb,
        'CDK 2026-10 price change', 'demo')
ON CONFLICT DO NOTHING;
SELECT id, material_uid, target_system, status, diffs FROM material_change_request;

\echo '== P. Hard delete is blocked (should raise an error and delete nothing) =='
DO $$
BEGIN
    DELETE FROM material WHERE material_uid = 'mat_01K9XG0000000000000000000A';
    RAISE EXCEPTION 'BUG: delete was not blocked';
EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE 'BUG%' THEN RAISE; END IF;
    RAISE NOTICE 'blocked as expected: %', SQLERRM;
END $$;
