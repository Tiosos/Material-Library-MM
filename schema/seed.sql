-- =============================================================================
-- Material Library v2 — reference data (category tree / units / type profiles)
-- Usage: psql -f schema/material.sql -f schema/search.sql -f schema/seed.sql
-- Safe to re-run (ON CONFLICT DO UPDATE)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Category tree
-- ---------------------------------------------------------------------------
INSERT INTO material_category (code, parent_code, name, level, path, sort_order) VALUES
    ('board',                    NULL,                        'Board',            1, 'board',                    10),
    ('board.melamine',           'board',                     'Melamine Board',   2, 'board/board.melamine',     11),
    ('board.melamine.mdf',       'board.melamine',            'Melamine MDF',     3, 'board/board.melamine/board.melamine.mdf', 12),
    ('board.melamine.particle',  'board.melamine',            'Melamine Particleboard', 3, 'board/board.melamine/board.melamine.particle', 13),
    ('board.veneer',             'board',                     'Veneer Board',     2, 'board/board.veneer',       14),
    ('board.laminate',           'board',                     'Laminate',         2, 'board/board.laminate',     15),
    ('edging',                   NULL,                        'Edging',           1, 'edging',                   20),
    ('glass',                    NULL,                        'Glass',            1, 'glass',                    30),
    ('glass.toughened',          'glass',                     'Toughened Glass',  2, 'glass/glass.toughened',    31),
    ('timber',                   NULL,                        'Timber',           1, 'timber',                   40),
    ('benchtop',                 NULL,                        'Benchtop',         1, 'benchtop',                 50),
    ('benchtop.stone',           'benchtop',                  'Stone Benchtop',   2, 'benchtop/benchtop.stone',  51),
    ('hardware',                 NULL,                        'Hardware',         1, 'hardware',                 60),
    ('hardware.hinge',           'hardware',                  'Hinge',            2, 'hardware/hardware.hinge',  61),
    ('hardware.runner',          'hardware',                  'Runner',           2, 'hardware/hardware.runner', 62),
    ('hardware.handle',          'hardware',                  'Handle',           2, 'hardware/hardware.handle', 63),
    ('appliance',                NULL,                        'Appliance',        1, 'appliance',                70),
    ('custom',                   NULL,                        'Custom Made',      1, 'custom',                   80),
    ('hire',                     NULL,                        'Equipment Hire',   1, 'hire',                     90),
    ('consumable',               NULL,                        'Consumable',       1, 'consumable',              100),
    ('generic',                  NULL,                        'Generic',          1, 'generic',                 999)
ON CONFLICT (code) DO UPDATE
    SET parent_code = EXCLUDED.parent_code,
        name        = EXCLUDED.name,
        level       = EXCLUDED.level,
        path        = EXCLUDED.path,
        sort_order  = EXCLUDED.sort_order;

-- ---------------------------------------------------------------------------
-- Units (UCUM codes preferred; count units use custom codes and leave ucum NULL)
-- ---------------------------------------------------------------------------
INSERT INTO unit (code, ucum, name, dimension, si_factor) VALUES
    ('mm',   'mm',  'millimetre',   'length',      0.001),
    ('cm',   'cm',  'centimetre',   'length',      0.01),
    ('m',    'm',   'metre',        'length',      1),
    ('m2',   'm2',  'square metre', 'area',        1),
    ('m3',   'm3',  'cubic metre',  'volume',      1),
    ('kg',   'kg',  'kilogram',     'mass',        1),
    ('g',    'g',   'gram',         'mass',        0.001),
    ('L',    'L',   'litre',        'volume',      0.001),
    ('sheet', NULL, 'sheet',        'count',       1),
    ('ea',   NULL,  'each',         'count',       1),
    ('roll', NULL,  'roll',         'count',       1),
    ('pair', NULL,  'pair',         'count',       1),
    ('set',  NULL,  'set',          'count',       1),
    ('box',  NULL,  'box',          'count',       1),
    ('h',    'h',   'hour',         'time',        3600),
    ('W',    'W',   'watt',         'power',       1),
    ('V',    'V',   'volt',         'voltage',     1),
    ('A',    'A',   'ampere',       'current',     1),
    ('deg',  'deg', 'degree',         'angle',       1),
    ('degC', 'Cel', 'degree Celsius', 'temperature', 1)
ON CONFLICT (code) DO UPDATE
    SET ucum = EXCLUDED.ucum, name = EXCLUDED.name,
        dimension = EXCLUDED.dimension, si_factor = EXCLUDED.si_factor;

-- ---------------------------------------------------------------------------
-- Type profiles (v1 had the table but no data; here are minimal usable definitions for the 11 categories)
-- ---------------------------------------------------------------------------
INSERT INTO material_profile (category_code, display_name, field_schema, required, search_weight, unit_options, axis_options) VALUES
('board', 'Board',
 '{"type":"object","properties":{"thickness_mm":{"type":"number","minimum":0},"colour":{"type":"string"},"substrate":{"enum":["MDF","Particleboard","Plywood","HMR MDF","MR MDF"]},"finish":{"type":"string"},"sided":{"enum":["Single","Double"]},"grain":{"type":"string"},"sheet_len_mm":{"type":"number"},"sheet_wid_mm":{"type":"number"}}}'::jsonb,
 ARRAY['thickness_mm','colour','substrate'],
 '{"colour":3,"substrate":2,"finish":1}'::jsonb,
 ARRAY['sheet','m2','ea'],
 ARRAY['thickness_mm','sheet_len_mm','sheet_wid_mm']),

('board.melamine', 'Melamine Board',
 '{"type":"object","properties":{"thickness_mm":{"type":"number"},"colour":{"type":"string"},"substrate":{"type":"string"},"finish":{"type":"string"},"sided":{"enum":["Single","Double"]}}}'::jsonb,
 ARRAY['thickness_mm','colour','substrate'],
 '{"colour":3,"substrate":2}'::jsonb,
 ARRAY['sheet','m2'],
 ARRAY['thickness_mm']),

('board.melamine.mdf', 'Melamine MDF',
 '{"type":"object","properties":{"thickness_mm":{"type":"number"},"colour":{"type":"string"},"finish":{"type":"string"},"sided":{"enum":["Single","Double"]},"hmr":{"type":"boolean"}}}'::jsonb,
 ARRAY['thickness_mm','colour'],
 '{"colour":3,"finish":1}'::jsonb,
 ARRAY['sheet','m2'],
 ARRAY['thickness_mm']),

('board.melamine.particle', 'Melamine Particleboard',
 '{"type":"object","properties":{"thickness_mm":{"type":"number"},"colour":{"type":"string"},"finish":{"type":"string"}}}'::jsonb,
 ARRAY['thickness_mm','colour'],
 '{"colour":3}'::jsonb,
 ARRAY['sheet','m2'],
 ARRAY['thickness_mm']),

('board.veneer', 'Veneer Board',
 '{"type":"object","properties":{"species":{"type":"string"},"thickness_mm":{"type":"number"},"veneer_cut":{"enum":["Crown","Quarter","Rotary"]},"sheet_len_mm":{"type":"number"},"sheet_wid_mm":{"type":"number"}}}'::jsonb,
 ARRAY['species','thickness_mm'],
 '{"species":3}'::jsonb,
 ARRAY['sheet','m2'],
 ARRAY['thickness_mm']),

('board.laminate', 'Laminate',
 '{"type":"object","properties":{"thickness_mm":{"type":"number"},"colour":{"type":"string"},"finish":{"type":"string"},"sheet_len_mm":{"type":"number"},"sheet_wid_mm":{"type":"number"}}}'::jsonb,
 ARRAY['thickness_mm','colour'],
 '{"colour":3}'::jsonb,
 ARRAY['sheet','m2'],
 ARRAY['thickness_mm']),

('edging', 'Edging',
 '{"type":"object","properties":{"edge_type":{"enum":["ABS","PVC","Melamine","Timber"]},"thickness_mm":{"type":"number"},"width_mm":{"type":"number"},"roll_length_m":{"type":"number"},"colour":{"type":"string"},"glued":{"type":"boolean"}}}'::jsonb,
 ARRAY['edge_type','thickness_mm','width_mm'],
 '{"edge_type":3,"colour":2}'::jsonb,
 ARRAY['roll','m'],
 ARRAY['thickness_mm']),

('glass', 'Glass',
 '{"type":"object","properties":{"glass_type":{"enum":["Toughened","Laminated","Float","Mirror"]},"thickness_mm":{"type":"number"},"edge_finish":{"type":"string"},"power_point":{"type":"boolean"},"tint":{"type":"string"}}}'::jsonb,
 ARRAY['glass_type','thickness_mm'],
 '{"glass_type":3,"tint":1}'::jsonb,
 ARRAY['m2','ea'],
 ARRAY['thickness_mm']),

('glass.toughened', 'Toughened Glass',
 '{"type":"object","properties":{"thickness_mm":{"type":"number"},"edge_finish":{"type":"string"},"tint":{"type":"string"},"power_point":{"type":"boolean"}}}'::jsonb,
 ARRAY['thickness_mm'],
 '{"tint":2}'::jsonb,
 ARRAY['m2','ea'],
 ARRAY['thickness_mm']),

('timber', 'Timber',
 '{"type":"object","properties":{"species":{"type":"string"},"grade":{"type":"string"},"thickness_mm":{"type":"number"},"width_mm":{"type":"number"},"length_m":{"type":"number"},"moisture_pct":{"type":"number"}}}'::jsonb,
 ARRAY['species','thickness_mm','width_mm'],
 '{"species":3,"grade":1}'::jsonb,
 ARRAY['m','ea'],
 ARRAY['thickness_mm','width_mm']),

('benchtop', 'Benchtop',
 '{"type":"object","properties":{"material":{"type":"string"},"thickness_mm":{"type":"number"},"length_mm":{"type":"number"},"width_mm":{"type":"number"},"edge_profile":{"type":"string"},"finish":{"type":"string"}}}'::jsonb,
 ARRAY['material','thickness_mm'],
 '{"material":3,"finish":1}'::jsonb,
 ARRAY['m2','ea'],
 ARRAY['thickness_mm']),

('benchtop.stone', 'Stone Benchtop',
 '{"type":"object","properties":{"stone_type":{"enum":["Engineered","Marble","Granite","Porcelain"]},"thickness_mm":{"type":"number"},"length_mm":{"type":"number"},"width_mm":{"type":"number"},"edge_profile":{"type":"string"}}}'::jsonb,
 ARRAY['stone_type','thickness_mm'],
 '{"stone_type":3}'::jsonb,
 ARRAY['m2','ea'],
 ARRAY['thickness_mm']),

('hardware', 'Hardware',
 '{"type":"object","properties":{"hardware_type":{"type":"string"},"finish":{"type":"string"},"mounting":{"type":"string"},"pack_qty":{"type":"number"},"opening_angle_deg":{"type":"number"}}}'::jsonb,
 ARRAY['hardware_type'],
 '{"hardware_type":3,"finish":2}'::jsonb,
 ARRAY['ea','pair','set','box'],
 ARRAY['pack_qty']),

('hardware.hinge', 'Hinge',
 '{"type":"object","properties":{"hinge_type":{"type":"string"},"opening_angle_deg":{"type":"number"},"overlay_mm":{"type":"number"},"finish":{"type":"string"},"soft_close":{"type":"boolean"},"pack_qty":{"type":"number"}}}'::jsonb,
 ARRAY['hinge_type'],
 '{"hinge_type":3,"finish":1}'::jsonb,
 ARRAY['ea','pair','box'],
 ARRAY['pack_qty']),

('hardware.runner', 'Runner',
 '{"type":"object","properties":{"runner_type":{"type":"string"},"length_mm":{"type":"number"},"load_kg":{"type":"number"},"soft_close":{"type":"boolean"}}}'::jsonb,
 ARRAY['runner_type','length_mm'],
 '{"runner_type":3}'::jsonb,
 ARRAY['pair','set'],
 ARRAY['length_mm']),

('hardware.handle', 'Handle',
 '{"type":"object","properties":{"handle_type":{"type":"string"},"centres_mm":{"type":"number"},"length_mm":{"type":"number"},"finish":{"type":"string"}}}'::jsonb,
 ARRAY['handle_type'],
 '{"handle_type":3,"finish":2}'::jsonb,
 ARRAY['ea','set'],
 ARRAY['centres_mm']),

('appliance', 'Appliance',
 '{"type":"object","properties":{"appliance_type":{"type":"string"},"width_mm":{"type":"number"},"height_mm":{"type":"number"},"depth_mm":{"type":"number"},"energy_rating":{"type":"string"},"colour":{"type":"string"}}}'::jsonb,
 ARRAY['appliance_type','width_mm'],
 '{"appliance_type":3,"colour":1}'::jsonb,
 ARRAY['ea'],
 ARRAY['width_mm']),

('custom', 'Custom Made',
 '{"type":"object","properties":{"made_by":{"type":"string"},"spec":{"type":"string"},"lead_time_days":{"type":"number"}}}'::jsonb,
 ARRAY['made_by'],
 '{"spec":2}'::jsonb,
 ARRAY['ea'],
 '{}'),

('hire', 'Equipment Hire',
 '{"type":"object","properties":{"hire_rate":{"type":"number"},"hire_unit":{"enum":["day","week","month"]},"project_scope":{"type":"string"}}}'::jsonb,
 ARRAY['hire_rate','hire_unit'],
 '{"project_scope":1}'::jsonb,
 ARRAY['h','ea'],
 '{}'),

('consumable', 'Consumable',
 '{"type":"object","properties":{"consumable_type":{"type":"string"},"size":{"type":"string"},"pack_qty":{"type":"number"},"colour":{"type":"string"}}}'::jsonb,
 ARRAY['consumable_type'],
 '{"consumable_type":3,"colour":1}'::jsonb,
 ARRAY['ea','box','roll','L'],
 '{}'),

('generic', 'Generic',
 '{"type":"object","properties":{}}'::jsonb,
 '{}',
 '{}'::jsonb,
 '{}',
 '{}')
ON CONFLICT (category_code) DO UPDATE
    SET display_name  = EXCLUDED.display_name,
        field_schema  = EXCLUDED.field_schema,
        required      = EXCLUDED.required,
        search_weight = EXCLUDED.search_weight,
        unit_options  = EXCLUDED.unit_options,
        axis_options  = EXCLUDED.axis_options;
