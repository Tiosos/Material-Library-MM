-- ---------------------------------------------------------------------------
-- eval/fixture.sql - deterministic catalogue for the search evaluation harness
-- ---------------------------------------------------------------------------
-- Load order: schema/material.sql, schema/search.sql, schema/api.sql, schema/seed.sql, then this file.
--
-- What this fixture guarantees, because scripts/eval_search.py asserts on it:
--   * all five search levels are reachable: exact (a whole identifier), prefix (identifier
--     prefix), and (every word), relaxed (some word), fuzzy (a typo), plus browse (no query)
--     and none (a query that must find nothing);
--   * two measure dimensions and three storage units: thickness in mm *and* one board stored
--     as 1.8 cm, opening angle in deg, so a unit-aware filter is tested by data, not by hope;
--   * two count units (sheet, box) and one per-each unit, so a filter on unit never matches
--     everything by accident;
--   * one family with three live variants plus two dead variants (archived, superseded), so
--     merge/retire behaviour is visible in the same set;
--   * three duplicate-merge pairs for T2: one the scan must find, one skipped because a side
--     is archived, one decoy that shares a name and brand but not a category;
--   * 45 rows in total, small enough to read, large enough that "top-1 of 1 result" is not a
--     free win.
--
-- The file is re-runnable: every row it owns is deleted first, in foreign-key-safe order, and
-- every row it inserts carries the 'Fixture ' brand names and the mat_FX / fam_FX id prefix.
-- ---------------------------------------------------------------------------

BEGIN;

DELETE FROM material WHERE material_uid LIKE 'mat_FX%';
DELETE FROM material_family WHERE family_id LIKE 'fam_FX%';
DELETE FROM party WHERE canonical_name LIKE 'Fixture %';

-- ---------------------------------------------------------------------------
-- Parties (names are prefixed so a database that also holds the demo imports keeps both)
-- ---------------------------------------------------------------------------
INSERT INTO party (kind, canonical_name, country) VALUES
    ('both',  'Fixture Laminex', 'AU'),
    ('brand', 'Fixture Blum',    'AT'),
    ('brand', 'Fixture Polytec', 'AU'),
    ('brand', 'Fixture Hettich', 'DE'),
    ('brand', 'Fixture Acme',    'AU'),
    ('brand', 'Fixture Dup',     'AU');

-- ---------------------------------------------------------------------------
-- Families: one board family with a thickness axis, one hinge family with an angle axis
-- ---------------------------------------------------------------------------
INSERT INTO material_family (family_id, family_key, brand_id, series, colour, substrate, display_name, axis_keys) VALUES
    ('fam_FX000000000000000000BRD001', 'fixture|laminex|black|mdf',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'), 'Laminex',
     'Black', 'MDF', 'Fixture Laminex Black MDF', ARRAY['thickness_mm']),
    ('fam_FX000000000000000000HNG001', 'fixture|blum|clip-top',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'), 'CLIP top',
     NULL, NULL, 'Fixture Blum CLIP top hinge', ARRAY['opening_angle_deg']);

-- ---------------------------------------------------------------------------
-- Materials: 6 boards (2 live in the second brand), 6 hinges, 4 accessories, 1 archived,
-- 1 superseded, 6 duplicate-pair rows, then 22 generated rows. 45 rows in total.
-- ---------------------------------------------------------------------------
INSERT INTO material (
    material_uid, category_code, product_type, name, brand_id, model_no, family_id,
    variant_values, default_supplier_id, unit, currency, unit_cost, attributes, tags, source_system
) VALUES
    -- board family, live variants
    ('mat_FX000000000000000000BRD016', 'board.melamine.mdf', 'board', 'Fixture Laminex 16mm Black MDF',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'), 'LAM-BLK-16', 'fam_FX000000000000000000BRD001',
     '{"thickness_mm": 16}', (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'),
     'sheet', 'AUD', 38.00, '{"colour": "Black", "substrate": "MDF", "finish": "Melamine"}', ARRAY['board'], 'fixture'),
    ('mat_FX000000000000000000BRD018', 'board.melamine.mdf', 'board', 'Fixture Laminex 18mm Black MDF',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'), 'LAM-BLK-18', 'fam_FX000000000000000000BRD001',
     '{"thickness_mm": 18}', (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'),
     'sheet', 'AUD', 42.00, '{"colour": "Black", "substrate": "MDF", "finish": "Melamine"}', ARRAY['board'], 'fixture'),
    ('mat_FX000000000000000000BRD025', 'board.melamine.mdf', 'board', 'Fixture Laminex 25mm Black MDF',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'), 'LAM-BLK-25', 'fam_FX000000000000000000BRD001',
     '{"thickness_mm": 25}', (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'),
     'sheet', 'AUD', 58.00, '{"colour": "Black", "substrate": "MDF", "finish": "Melamine"}', ARRAY['board'], 'fixture'),
    ('mat_FX000000000000000000PTC018', 'board.melamine', 'board', 'Fixture Polytec 18mm Grey Melamine',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Polytec'), 'PLY-GRY-18', NULL,
     '{"thickness_mm": 18}', (SELECT id FROM party WHERE canonical_name = 'Fixture Polytec'),
     'sheet', 'AUD', 36.00, '{"colour": "Grey", "substrate": "Particleboard", "finish": "Melamine"}', ARRAY['board'], 'fixture'),
    ('mat_FX000000000000000000PTC019', 'board.melamine', 'board', 'Fixture Polytec 18mm White Melamine',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Polytec'), 'PLY-WHT-18', NULL,
     '{"thickness_mm": 18}', (SELECT id FROM party WHERE canonical_name = 'Fixture Polytec'),
     'sheet', 'AUD', 36.00, '{"colour": "White", "substrate": "Particleboard", "finish": "Melamine"}', ARRAY['board'], 'fixture'),

    -- hinge family, live variants
    ('mat_FX000000000000000000HNG110', 'hardware.hinge', 'hinge', 'Fixture Blum CLIP top 110 hinge',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'), 'FX-BLM-71B110', 'fam_FX000000000000000000HNG001',
     '{"opening_angle_deg": 110}', (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'),
     'ea', 'AUD', 6.80, '{"hinge_type": "CLIP top", "soft_close": false}', ARRAY['hinge'], 'fixture'),
    ('mat_FX000000000000000000HNG120', 'hardware.hinge', 'hinge', 'Fixture Blum CLIP top 120 hinge',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'), 'FX-BLM-71B120', 'fam_FX000000000000000000HNG001',
     '{"opening_angle_deg": 120}', (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'),
     'ea', 'AUD', 7.10, '{"hinge_type": "CLIP top", "soft_close": false}', ARRAY['hinge'], 'fixture'),
    ('mat_FX000000000000000000HNG155', 'hardware.hinge', 'hinge', 'Fixture Blum CLIP top 155 hinge',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'), 'FX-BLM-71B155', 'fam_FX000000000000000000HNG001',
     '{"opening_angle_deg": 155}', (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'),
     'ea', 'AUD', 8.40, '{"hinge_type": "CLIP top", "soft_close": false}', ARRAY['hinge'], 'fixture'),
    ('mat_FX000000000000000000HNG170', 'hardware.hinge', 'hinge', 'Fixture Blum CLIP top 170 hinge',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'), 'FX-BLM-71B170', 'fam_FX000000000000000000HNG001',
     '{"opening_angle_deg": 170}', (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'),
     'ea', 'AUD', 9.20, '{"hinge_type": "CLIP top", "soft_close": true}', ARRAY['hinge'], 'fixture'),
    ('mat_FX000000000000000000HTN110', 'hardware.hinge', 'hinge', 'Fixture Hettich Sensys 110 hinge',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Hettich'), 'FX-HTN-909110', NULL,
     '{"opening_angle_deg": 110}', (SELECT id FROM party WHERE canonical_name = 'Fixture Hettich'),
     'ea', 'AUD', 4.90, '{"hinge_type": "Sensys", "soft_close": true}', ARRAY['hinge'], 'fixture'),
    ('mat_FX000000000000000000HTN165', 'hardware.hinge', 'hinge', 'Fixture Hettich Sensys 165 hinge',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Hettich'), 'FX-HTN-909165', NULL,
     '{"opening_angle_deg": 165}', (SELECT id FROM party WHERE canonical_name = 'Fixture Hettich'),
     'ea', 'AUD', 5.60, '{"hinge_type": "Sensys", "soft_close": true}', ARRAY['hinge'], 'fixture'),

    -- accessories: covered by count units, no family
    ('mat_FX000000000000000000ACC050', 'hardware.hinge', 'accessory', 'Fixture Blum INSERTA hinge box of 50',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'), 'FX-BLM-71B050', NULL,
     '{}', (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'),
     'box', 'AUD', 210.00, '{"pack_qty": 50}', ARRAY['hinge', 'bulk'], 'fixture'),
    ('mat_FX000000000000000000ACC010', 'hardware', 'accessory', 'Fixture Blum hinge mounting plate',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'), 'FX-BLM-173L8100', NULL,
     '{}', (SELECT id FROM party WHERE canonical_name = 'Fixture Blum'),
     'ea', 'AUD', 1.20, '{"mounting": "screw-on"}', ARRAY['hinge'], 'fixture'),
    ('mat_FX000000000000000000ACC020', 'hardware', 'accessory', 'Fixture Hettich cover cap bag of 200',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Hettich'), 'FX-HTN-908020', NULL,
     '{}', (SELECT id FROM party WHERE canonical_name = 'Fixture Hettich'),
     'box', 'AUD', 24.00, '{"pack_qty": 200}', ARRAY['cap'], 'fixture'),
    ('mat_FX000000000000000000ACC030', 'hardware', 'accessory', 'Fixture Hettich Sensys mounting plate',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Hettich'), 'FX-HTN-907030', NULL,
     '{}', (SELECT id FROM party WHERE canonical_name = 'Fixture Hettich'),
     'ea', 'AUD', 1.50, '{"mounting": "expando"}', ARRAY['hinge'], 'fixture'),

    -- dead variants: archived (never searchable) and superseded (resolvable by uid, hidden from search)
    ('mat_FX000000000000000000ARC018', 'board.melamine.mdf', 'board', 'Fixture Laminex 18mm Black MDF (discontinued)',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'), 'LAM-BLK-18-OLD', 'fam_FX000000000000000000BRD001',
     '{"thickness_mm": 18}', (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'),
     'sheet', 'AUD', 40.00, '{"colour": "Black", "substrate": "MDF", "finish": "Melamine"}', ARRAY['board'], 'fixture'),
    ('mat_FX000000000000000000RPD018', 'board.melamine.mdf', 'board', 'Fixture Laminex 18mm Black MDF (old part no)',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'), 'LAM-BLK-18-A', 'fam_FX000000000000000000BRD001',
     '{"thickness_mm": 18}', (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'),
     'sheet', 'AUD', 41.00, '{"colour": "Black", "substrate": "MDF", "finish": "Melamine"}', ARRAY['board'], 'fixture');

UPDATE material SET status = 'archived'
 WHERE material_uid = 'mat_FX000000000000000000ARC018';

UPDATE material SET status = 'superseded', superseded_by = 'mat_FX000000000000000000BRD018'
 WHERE material_uid = 'mat_FX000000000000000000RPD018';

-- Duplicate-merge pairs for T2 (see docs/t2-merge.md section 10.1). All of them live in
-- category board.veneer / brand 'Fixture Dup' or in edging, which no search golden case
-- touches, so adding them cannot move an existing expect_total. A1/A2 is the pair the scan
-- must find; B1/B2 is skipped because B2 is archived; C1/C2 is the decoy that shares a name
-- and brand but sits in a different category, so the scan must not pair it.
INSERT INTO material (material_uid, category_code, product_type, name, brand_id, model_no,
                      variant_values, default_supplier_id, unit, currency, unit_cost,
                      attributes, tags, synonyms, source_system) VALUES
    ('mat_FX000000000000000000DXPK01', 'board.veneer', 'board', 'Fixture Dup Smoked Veneer Panel',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'), 'DUP-A1',
     '{"thickness_mm": 22}', (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'),
     'sheet', 'AUD', 55.00, '{"colour": "Smoke", "substrate": "MDF"}', ARRAY['board'], ARRAY['veneer'], 'fixture'),
    ('mat_FX000000000000000000DXPK02', 'board.veneer', 'board', 'Fixture Dup Smoked Veneer Panel 22mm',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'), 'DUP-A2',
     '{"thickness_mm": 22}', (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'),
     'sheet', 'AUD', 57.00, '{"finish": "Satin", "substrate": "MDF"}', ARRAY['board'], ARRAY['veneer', 'panel'], 'fixture_legacy'),
    ('mat_FX000000000000000000DXPK03', 'board.veneer', 'board', 'Fixture Dup Fog Veneer Panel',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'), 'DUP-B1', '{}',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'),
     'sheet', 'AUD', 48.00, '{"colour": "Fog"}', ARRAY['board'], ARRAY['veneer'], 'fixture'),
    ('mat_FX000000000000000000DXPK04', 'board.veneer', 'board', 'Fixture Dup Fog Veneer Panel',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'), 'DUP-B2', '{}',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'),
     'sheet', 'AUD', 48.00, '{"colour": "Fog"}', ARRAY['board'], ARRAY['veneer'], 'fixture'),
    ('mat_FX000000000000000000DXPK05', 'board.veneer', 'board', 'Fixture Dup Edgeband Tape 22mm',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'), 'DUP-C1', '{}',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'),
     'roll', 'AUD', 19.00, '{"colour": "Smoke"}', ARRAY['edging'], ARRAY['tape'], 'fixture'),
    ('mat_FX000000000000000000DXPK06', 'edging', 'edging', 'Fixture Dup Edgeband Tape 22mm',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'), 'DUP-C2', '{}',
     (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'),
     'roll', 'AUD', 19.00, '{"colour": "Smoke"}', ARRAY['edging'], ARRAY['tape'], 'fixture');

UPDATE material SET status = 'archived'
 WHERE material_uid = 'mat_FX000000000000000000DXPK04';

-- 22 generated filler rows: distinct names, per-each unit, no family and no measures, so they
-- exercise "everything else" without competing with any assertion above.
INSERT INTO material (material_uid, category_code, product_type, name, brand_id, unit, currency, unit_cost, attributes, tags, source_system)
SELECT 'mat_FX' || lpad(n::text, 21, '0') || 'GEN',
       'hardware',
       'fitting',
       'Fixture Acme shelf pin ' || n || 'mm',
       (SELECT id FROM party WHERE canonical_name = 'Fixture Acme'),
       'ea',
       'AUD',
       round((0.40 + n * 0.05)::numeric, 4),
       jsonb_build_object('diameter_mm', n),
       ARRAY['fitting'],
       'fixture'
  FROM generate_series(1, 22) AS n;

-- ---------------------------------------------------------------------------
-- Measures: thickness in mm and in cm (the cm row is what makes unit conversion testable),
-- opening angles in deg
-- ---------------------------------------------------------------------------
INSERT INTO material_measure (material_uid, key, value_num, ucum, is_preferred, source_system) VALUES
    ('mat_FX000000000000000000BRD016', 'thickness_mm', 16.0, 'mm', true, 'fixture'),
    ('mat_FX000000000000000000BRD018', 'thickness_mm', 1.8,  'cm', true, 'fixture'),
    ('mat_FX000000000000000000BRD025', 'thickness_mm', 25.0, 'mm', true, 'fixture'),
    ('mat_FX000000000000000000PTC018', 'thickness_mm', 18.0, 'mm', true, 'fixture'),
    ('mat_FX000000000000000000PTC019', 'thickness_mm', 18.0, 'mm', true, 'fixture'),
    ('mat_FX000000000000000000ARC018', 'thickness_mm', 18.0, 'mm', true, 'fixture'),
    ('mat_FX000000000000000000RPD018', 'thickness_mm', 18.0, 'mm', true, 'fixture'),
    ('mat_FX000000000000000000HNG110', 'opening_angle_deg', 110.0, 'deg', true, 'fixture'),
    ('mat_FX000000000000000000HNG120', 'opening_angle_deg', 120.0, 'deg', true, 'fixture'),
    ('mat_FX000000000000000000HNG155', 'opening_angle_deg', 155.0, 'deg', true, 'fixture'),
    ('mat_FX000000000000000000HNG170', 'opening_angle_deg', 170.0, 'deg', true, 'fixture'),
    ('mat_FX000000000000000000HTN110', 'opening_angle_deg', 110.0, 'deg', true, 'fixture'),
    ('mat_FX000000000000000000HTN165', 'opening_angle_deg', 165.0, 'deg', true, 'fixture'),
    -- the duplicate pair stores 22 mm twice in two different units on purpose: a merge that
    -- ignored units would leave the keeper with two preferred thickness rows
    ('mat_FX000000000000000000DXPK01', 'thickness_mm', 22.0, 'mm', true, 'fixture'),
    ('mat_FX000000000000000000DXPK02', 'thickness_mm', 2.2,  'cm', true, 'fixture_legacy'),
    ('mat_FX000000000000000000DXPK03', 'thickness_mm', 19.0, 'mm', true, 'fixture');

-- ---------------------------------------------------------------------------
-- Identifiers: whole-value exact matches and one prefix that spans three boards
-- ---------------------------------------------------------------------------
INSERT INTO material_identifier (material_uid, kind, value, source_system, is_primary) VALUES
    ('mat_FX000000000000000000BRD016', 'supplier_code', 'FX-LAM-MEL-BLK-16', 'fixture', true),
    ('mat_FX000000000000000000BRD018', 'supplier_code', 'FX-LAM-MEL-BLK-18', 'fixture', true),
    ('mat_FX000000000000000000BRD025', 'supplier_code', 'FX-LAM-MEL-BLK-25', 'fixture', true),
    ('mat_FX000000000000000000PTC018', 'supplier_code', 'FX-PLY-MEL-GRY-18', 'fixture', true),
    ('mat_FX000000000000000000HNG110', 'supplier_code', 'FX-BLM-71B110', 'fixture', true),
    ('mat_FX000000000000000000HNG120', 'supplier_code', 'FX-BLM-71B120', 'fixture', true),
    ('mat_FX000000000000000000HNG155', 'supplier_code', 'FX-BLM-71B155', 'fixture', true),
    ('mat_FX000000000000000000HNG170', 'supplier_code', 'FX-BLM-71B170', 'fixture', true),
    ('mat_FX000000000000000000HTN110', 'supplier_code', 'FX-HTN-909110', 'fixture', true),
    ('mat_FX000000000000000000HTN165', 'supplier_code', 'FX-HTN-909165', 'fixture', true),
    ('mat_FX000000000000000000ACC050', 'supplier_code', 'FX-BLM-71B050', 'fixture', true),
    ('mat_FX000000000000000000ACC010', 'supplier_code', 'FX-BLM-173L8100', 'fixture', true),
    ('mat_FX000000000000000000ACC020', 'supplier_code', 'FX-HTN-908020', 'fixture', true),
    ('mat_FX000000000000000000ACC030', 'supplier_code', 'FX-HTN-907030', 'fixture', true),
    -- duplicate pair identities: distinct codes, so a merge has to move one row across
    ('mat_FX000000000000000000DXPK01', 'supplier_code', 'FX-DUP-VEN-22', 'fixture', true),
    ('mat_FX000000000000000000DXPK02', 'supplier_code', 'FX-DUP-VEN-22A', 'fixture_legacy', true),
    ('mat_FX000000000000000000DXPK03', 'supplier_code', 'FX-DUP-FOG-18', 'fixture', true),
    ('mat_FX000000000000000000DXPK04', 'supplier_code', 'FX-DUP-FOG-18', 'fixture_legacy', true),
    ('mat_FX000000000000000000DXPK05', 'supplier_code', 'FX-DUP-TAPE-22', 'fixture', true),
    ('mat_FX000000000000000000DXPK06', 'supplier_code', 'FX-DUP-TAPE-22', 'fixture_legacy', true);

-- ---------------------------------------------------------------------------
-- One price row and one relation: enough for the detail contract, not enough to bloat the file
-- ---------------------------------------------------------------------------
INSERT INTO material_price (material_uid, supplier_id, unit_cost, currency, cost_basis, valid_from, source_system) VALUES
    ('mat_FX000000000000000000BRD018', (SELECT id FROM party WHERE canonical_name = 'Fixture Laminex'),
     42.00, 'AUD', 'ex_gst', DATE '2026-01-01', 'fixture'),
    -- the survivor in the duplicate pair carries its own price history, which a merge must move
    ('mat_FX000000000000000000DXPK02', (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'),
     57.00, 'AUD', 'ex_gst', DATE '2026-02-01', 'fixture_legacy'),
    ('mat_FX000000000000000000DXPK02', (SELECT id FROM party WHERE canonical_name = 'Fixture Dup'),
     54.00, 'AUD', 'ex_gst', DATE '2026-05-01', 'fixture_legacy');

INSERT INTO material_relation (from_uid, to_uid, kind, note, confidence) VALUES
    ('mat_FX000000000000000000HNG170', 'mat_FX000000000000000000HNG155', 'substitute',
     'Use the 155 degree hinge when the 170 degree one is out of stock', 0.80),
    -- a relation hanging off the survivor: after the merge it must point at the keeper
    ('mat_FX000000000000000000DXPK02', 'mat_FX000000000000000000BRD018', 'similar',
     'Same smoked veneer look, different core', 0.70);

-- Keep the statistics honest: the fixture is small, but a stale plan on an empty import would
-- make the first evaluation run in a fresh database slower than everything after it.
ANALYZE material;
ANALYZE material_measure;
ANALYZE material_identifier;

COMMIT;
