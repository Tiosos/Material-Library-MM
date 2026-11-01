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
