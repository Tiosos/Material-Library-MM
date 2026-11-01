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
