# T2 - Duplicate merge design

Status: proposal, no code written yet.
Owner: Tiosos.
Scope decision: candidate scan + review queue + merge action + HTTP edge + CLI, with rules below.
Companion docs: `docs/data-model-v2.md` (B7 row), `docs/api-v1.md` (read API v1), `docs/t1-progress.md` (what shipped).

---

## 0. One paragraph

`material_duplicate_candidate` has existed since v2 but nothing writes to it except
`schema/sample_demo.sql`, and no code can act on it. B7 in `docs/data-model-v2.md`
is marked "Partial" for exactly that reason: the table exists, the merge action does
not. T2 closes B7 with three pieces - a scan that fills the queue, a merge function
that is auditable and reversible-by-hand, and a thin HTTP/CLI surface. The guiding
rule is the same as T1: **SQL owns the semantics, HTTP owns the transport.** Every
merge is one transaction, every field that survives is explained by a rule in this
document, and nothing is ever hard-deleted.

---

## 1. Why now

1. Duplicates are already visible in the shipped sample data. `schema/sample_demo.sql`
   inserts two records that score as a duplicate pair (`mat_01K9XFT7...` "Laminex 18mm
   Black Melamine MDF" vs `mat_01K9XJ00...` "Polytec 18mm Black Melamine MDF") and
   files a candidate row at line 120. Until T2 that row is decoration: `GET
   /v1/duplicates` does not exist and there is no way to merge the two records.
2. T1 made the read side honest. Search excludes `superseded` rows
   (`schema/search.sql`, `status NOT IN ('archived','superseded')`), `material_detail`
   still resolves a superseded uid, and `/v1/changes` reports the transition. That is
   exactly the tombstone contract a merge needs, so the merge action now has a place
   to write into.
3. Every import multiplies the problem. `docs/review-v1-and-plan-v2.md:221` puts it
   bluntly: without dedupe and merge, three imports produce three copies of "16mm
   black". The import pipeline (B9) is done, so the pressure only grows.
4. T3 (write-back) and the MCP layer both need a stable notion of "one golden record
   per real-world material". Merging is the operation that creates that record.

Deliberately **not** in T2: automatic merging, unmerge, cross-category merging,
merging into a record from a different `category_code`, and any change to the search
ranking function. See section 11.

---

## 2. What exists today

| Piece | State | Where |
| --- | --- | --- |
| `material_duplicate_candidate` table | exists, `PRIMARY KEY (uid_a, uid_b)`, `CHECK (uid_a < uid_b)`, `status IN ('open','merged','ignored')`, `score numeric(5,4)` | `schema/material.sql:344-352` |
| Candidate writers | only `schema/sample_demo.sql:120` (CI demo) | - |
| `material.superseded_by` | exists, self-FK, no cascade | `schema/material.sql:116` |
| Superseded semantics | hidden from search/facets/suggest, visible in `material_detail`, counted as a transition by `/v1/changes` | `schema/search.sql`, `schema/api.sql`, `api/main.py` |
| Audit | `material_revision` (snapshot per rev) + `material_event` (op, payload, actor) written by triggers, actor read from `current_setting('ml.actor')` | `schema/material.sql:269-289`, `:449-471` |
| Hard delete | blocked by `ml_block_delete()` unless `ml.allow_delete=on` | `schema/material.sql:479-489` |
| Child tables | `material_measure`, `material_identifier`, `material_price`, `material_relation`, `material_classification`, `material_epd`, `material_usage`, `material_revision` all `ON DELETE CASCADE`; `material_identifier` PK is `(kind, value, source_system)` (global, not per material) | `schema/material.sql:177-266` |
| Completeness signal for "which record is better" | `v_material_completeness` (required fields present / required) | `schema/material.sql:592-602` |
| Read API | `GET /v1/materials`, `/v1/materials/{uid}`, `/v1/changes`, 21 pytest tests, `scripts/api_contract.sql` | `api/main.py`, `tests/` |

So T2 adds: candidate scan, queue payload, preview, merge, ignore, and the transports.

---

## 3. Design principles

1. **One function, one transaction.** `material_merge()` is a single `plpgsql`
   function; the HTTP handler owns no merge logic. If the API is later replaced by
   MCP or a CLI, behaviour cannot drift.
2. **Preview is the contract.** The plan returned by `material_merge_preview()` is
   byte-for-byte the plan that merge applies. The API exposes it as
   `GET /v1/duplicates/{uid_a}/{uid_b}`, so an operator reads the consequences before
   pressing merge.
3. **Keeper always explicit.** The caller names `p_keep`; the scan only *suggests* a
   keeper (completeness, price rows, reference count, age). No code path picks the
   keeper on its own.
4. **Tombstone, never delete.** Survivor becomes `status='superseded'` with
   `superseded_by = keep`; the candidate rows flip to `merged`. Every child row either
   moved or was dropped as an exact duplicate, and each of those decisions lands in
   `material_event`.
5. **Rules over heuristics.** Field survival is a table (section 4), not a mood.
   Where a rule cannot decide (both sides non-null and different), the keeper wins and
   the preview lists a warning.
6. **Reversible by hand, documented.** v1 has no `unmerge()`. Section 9 gives the
   exact recovery steps using `material_revision`, which is why the audit must be
   complete on day one.

---

## 4. Field survival rules

The merged result is computed from `keep` (K) and survivor (S). Order matters: the
merge reads both rows with `FOR UPDATE`, computes the result in memory, then writes.

| Field | Rule |
| --- | --- |
| `name`, `description`, `model_no`, `product_type` | K non-empty wins; if K is null/empty, take S |
| `brand_id`, `default_supplier_id`, `default_supplier_ref`, `family_id` | K non-null wins; otherwise adopt S. If both non-null and different -> K wins + warning `family_mismatch` / `brand_mismatch` |
| `unit`, `currency`, `cost_basis`, `lead_time_days`, `unit_cost` | K non-null wins; otherwise S. Both non-null and different -> K wins + warning `commercial_mismatch` (money must not silently change) |
| `status` | K must be `active`/`draft`; the value is preserved |
| `variant_values` | K non-empty wins; otherwise S |
| `image_url`, `product_url` | K non-null wins; otherwise S |
| `attachments` | union by `url` (K first) |
| `attributes` | `S.attributes || K.attributes` (K wins on key collision), key union |
| `tags`, `synonyms` | sorted union, case-insensitive dedupe |
| `identifiers` | every S row is re-pointed to K; a triple that collides with an existing K row is deleted (same identity twice). Warning `identifier_conflict` when S has the same `kind`+`value` under a different `source_system` |
| `measures` | move S rows whose `(key)` K lacks. If both have `is_preferred` for the same key, K's row stays, S's row is kept as non-preferred only when `value_num` differs, warning `measure_conflict` |
| `prices` | all S rows re-pointed to K; exact duplicates (`supplier_id, unit_cost, currency, cost_basis, qty_break, valid_from`) dropped |
| `relations` | rows where S is `from_uid` -> `from_uid = K`; where S is `to_uid` -> `to_uid = K`; then self-loops dropped and `(from_uid, to_uid, kind)` duplicates merged |
| `classifications` | move when `(system, code)` absent on K, otherwise drop |
| `epd` | K's row wins; if K has none, S's row moves. Both present -> warning `epd_conflict` |
| `usage` | summed: `ref_count`, `search_hits`; `last_used_at` = max; S row deleted |
| `source_system`, `source_id`, `imported_at`, `confidence` | K wins (provenance is the keeper's) |
| `rev`, `sync_seq`, `search_tsv`, `updated_at` | recomputed by existing triggers on write |

Survivor's `description`/`model_no` etc. are only lost when K already had a value;
that loss is listed in the preview (`dropped` entries) and in the event payload.

---

## 5. Data model changes

All additive; no column is dropped, no table is renamed. Added to `schema/material.sql`
(and mirrored in `schema/api.sql` where a function lives there).

### 5.1 `material_duplicate_candidate` gains decision columns

```sql
ALTER TABLE material_duplicate_candidate
    ADD COLUMN IF NOT EXISTS keep_uid    text REFERENCES material (material_uid),
    ADD COLUMN IF NOT EXISTS decided_by  text,
    ADD COLUMN IF NOT EXISTS decided_at  timestamptz,
    ADD COLUMN IF NOT EXISTS note        text;
CREATE INDEX IF NOT EXISTS material_duplicate_status_idx
    ON material_duplicate_candidate (status, score DESC);
```

`keep_uid` is the winner actually used (not a suggestion). `note` carries the operator
reason. Because the table already has `PRIMARY KEY (uid_a, uid_b)`, a re-scan updates
`score` in place and never duplicates a pair.

### 5.2 New functions in `schema/merge.sql` (new file, applied after `api.sql`)

| Function | Returns | Purpose |
| --- | --- | --- |
| `material_duplicate_scan(p_threshold numeric DEFAULT 0.80, p_limit integer DEFAULT 500)` | `jsonb` `{pairs, inserted, updated, threshold, candidates}` | fills/refreshes the queue; only `active`/`draft`, only same `category_code`, `similarity(name, name) >= threshold`, skips variants and pairs already `merged` (section 5.3) |
| `material_duplicate_queue(p_status text DEFAULT 'open', p_limit integer DEFAULT 50, p_offset integer DEFAULT 0)` | `jsonb` `{total, offset, limit, rows:[{uid_a, uid_b, score, status, keep_suggestion, ...}]}` | the review list payload, one call per page |
| `material_merge_preview(p_keep text, p_merge text)` | `jsonb` | plan + warnings + suggestion ranks; read-only |
| `material_merge(p_keep text, p_merge text, p_actor text DEFAULT NULL, p_reason text DEFAULT NULL, p_dry_run boolean DEFAULT false)` | `jsonb` | the merge; `p_dry_run` runs the whole thing and rolls back |
| `material_duplicate_ignore(p_uid_a text, p_uid_b text, p_actor text, p_reason text DEFAULT NULL)` | `jsonb` | marks the pair `ignored` with an event |

Keeper suggestion inside queue/preview: ordered by
`(completeness_ratio DESC, price_rows DESC, usage_ref_count DESC, created_at ASC, material_uid)`.
It is a string, never applied automatically.

### 5.3 Scan blocking (keeps an O(n^2) future honest)

The scan compares only within a block: same `category_code` and same `brand_id`
(`IS NOT DISTINCT FROM`, so two brandless rows still compare). At 109 rows the whole table
fits in one block anyway, but the predicate is there so a 100k-row catalogue does not melt.
Pairs are canonicalised with `least(uid)/greatest(uid)` to satisfy `uid_a < uid_b`.

Four rules keep the queue about products rather than variants, all learned by running the
scan against real data:

1. **Declared axes win.** If the two rows disagree on any key of `variant_values`
   (`{"thickness_mm": 16}` vs `{"thickness_mm": 18}`), they are variants of one product,
   not duplicates, and the pair is skipped.
2. **Digit-masked names are variants.** If the names match once digits are masked
   (`shelf pin #mm`) but the digit strings differ (`1` vs `2`), it is a size, angle or
   part-number variant and the pair is skipped. This is what stops the 22 filler rows from
   producing 109 pairs; without it a fixture scan reported 110 pairs instead of 1.
3. **Different part numbers are different parts.** If both rows carry an identifier of the
   same kind and neither value is a prefix of the other, the pair is skipped. Name similarity
   is a weak signal in real catalogues: the imported Blum file repeats one name
   (`CLIP top BLUMOTION INSERTA hinge NI`) for dozens of hinges that differ only by supplier
   code, so a name-only scan produced **1,011** pairs over 109 rows. With this rule the same
   data yields 3 pairs. A shared code, or one code being the other plus a suffix
   (`FX-DUP-VEN-22` vs `FX-DUP-VEN-22A`), still counts as compatible evidence.
4. **Decided pairs stay decided.** A pair already `merged` is never re-queued, and an
   `ignored` or `merged` row keeps its status and its score (`ON CONFLICT ... WHERE status = 'open'`),
   so re-running the scan does not nag about decisions a human already made.

The scan accepts `p_threshold` (default 0.80, clamped to 0..1) and `p_limit` (default 500,
clamped to 1..5000), and returns `{threshold, pairs, inserted, updated, candidates}` where
`candidates` is the page it just scored, so a CLI needs no second query.

---

## 6. Merge algorithm (ordered, single transaction)

```
1.  SELECT ... FROM material WHERE material_uid IN (keep, merge)
      ORDER BY material_uid FOR UPDATE;              -- lock both, stable order
2.  validate: keep <> merge; both exist; both status in (active,draft);
      neither already superseded/archived; same category_code;
      caller-supplied keep is allowed to be either side of the pair.
3.  compute plan (section 4) in plpgsql variables.
4.  IF p_dry_run THEN return plan; (caller rolls back) END IF;
5.  PERFORM set_config('ml.actor', p_actor, true);
    PERFORM set_config('ml.source', 'merge', true);
6.  update K with the resolved scalar/array fields (one UPDATE -> one revision).
7.  move/repoint child rows (identifiers, measures, prices, relations,
      classifications, epd, usage), dropping exact duplicates.
8.  UPDATE material SET status='superseded', superseded_by=K, ... WHERE uid=S;
9.  UPDATE candidate rows: every pair involving S -> status='merged', keep_uid=K,
      decided_by, decided_at, note; the exact pair always included.
10. INSERT INTO material_event (material_uid, entity, op, payload, actor)
      VALUES (K, 'material_merge', 'insert', {keep, merged, plan, warnings}, actor);
      plus one row for S with op 'update' (status change is already emitted by the
      trigger; this row records why).
11. return {keep, merged, plan, warnings, events, already_merged:false}
```

Idempotency: if S is already `superseded_by = K`, return the same payload with
`already_merged: true` and HTTP 200; if S is superseded by someone else, HTTP 409.

---

## 7. HTTP edge (`api/main.py`)

All routes behind the existing `require_token` and `actor_from`; read routes log
nothing (they are not searches).

| Route | Behaviour |
| --- | --- |
| `GET /v1/duplicates` | query `status=open|ignored|merged|all` (default: `open` + `ignored`, i.e. everything still actionable), `limit` 1..100 default 20, `cursor`; payload `{schema_version, total, offset, limit, rows[], next_cursor}` from `material_duplicate_queue` |
| `GET /v1/duplicates/{uid_a}/{uid_b}` | preview plan; 404 when the pair is unknown, no writes |
| `POST /v1/duplicates/{uid_a}/{uid_b}/merge` | body `{keep, reason?, dry_run?}`; 200 report, 400 `bad_request` (keep not one of the pair, reason too long, body not an object), 409 `conflict` (already superseded by another record, archived, category mismatch), 404 `not_found` |
| `POST /v1/duplicates/{uid_a}/{uid_b}/ignore` | body `{reason?}`; marks the pair ignored, 200 |

Error shape is the T1 shape `{"error": {"code", "message", "detail"?}}`. No scan route
(decision D3): scanning writes rows, so it stays in the CLI.

---

## 8. CLI (`scripts/duplicate_scan.py`)

```
python scripts/duplicate_scan.py --dsn ... [--threshold 0.80] [--limit 500] [--json out.json]
```

- prints `scanned N pairs, inserted X, updated Y` and returns 0; exits 1 on any SQL error.
- `--json` writes `{threshold, inserted, updated, pairs:[{uid_a,uid_b,score}]}`.
- no network, no password prompts; same `--dsn` convention as `scripts/import_file.py`.
- `scripts/merge_materials.py --dsn ... --keep UID --merge UID --reason TEXT [--dry-run]`
  for environments without the HTTP edge; it calls the same SQL function and prints the
  plan instead of a JSON report.

CI: both scripts run against the fixture database (`eval/fixture.sql` gains a designed
duplicate pair, see section 10), so the scan has something real to find.

---

## 9. Recovery (no unmerge in v1)

Documented operator steps, in order:

1. Read the merge event:
   `SELECT payload FROM material_event WHERE entity='material_merge' AND at DESC LIMIT 1;`
   It names `keep`, `merged` and the plan that was applied.
2. Restore the survivor: `UPDATE material SET status='active', superseded_by=NULL WHERE material_uid = <merged>;`
   (`ml.allow_delete` is irrelevant - nothing was deleted.)
3. Restore K's previous values from `material_revision`:
   `SELECT snapshot FROM material_revision WHERE material_uid = <keep> ORDER BY rev DESC OFFSET 1 LIMIT 1;`
4. Move child rows back using the plan's `moved` list; re-run
   `material_duplicate_scan()` afterwards so the pair reappears in the queue.

This is tedious on purpose: T2's promise is audit completeness, not one-click undo.
A real `unmerge()` needs conflict rules for edits made after the merge, which is why it
is deferred (decision D5).

---

## 10. Tests and acceptance

### 10.1 Fixture additions (`eval/fixture.sql`)

Three designed pairs in `board.veneer` under brand `Fixture Dup`, plus one decoy that must NOT
pair. Their categories and measure values were chosen so no existing search golden case moves
(`eval/golden.yaml`). Row names differ from the plan sketch because a uid must match
`^mat_[0-9A-HJKMNP-TV-Z]{26}$` and the alphabet has no `U`:

| Pair | Intent |
| --- | --- |
| `mat_FX...DXPK01/DXPK02` "Fixture Dup Smoked Veneer Panel" (+ "22mm") | the pair to find: similarity 0.8649, thickness `22.0 mm` vs `2.2 cm`, the survivor carries two prices, one relation and its own identifier |
| `mat_FX...DXPK03/DXPK04` "Fixture Dup Fog Veneer Panel" | names identical, `DXPK04` archived -> must never be scanned (live row measures 19 mm to stay outside the 17.5-18.5 golden window) |
| `mat_FX...DXPK05/DXPK06` "Fixture Dup Edgeband Tape 22mm" | same name and brand, `board.veneer` vs `edging` -> must not pair (decoy) |

The fixture already cleans `mat_FX%` and `fam_FX%`, so the new rows ride the existing
re-runnability.

### 10.2 SQL contract (`scripts/merge_contract.sql`, psql-runnable like `api_contract.sql`)

1. scan on the fixture inserts exactly the expected pairs and no decoys;
2. re-scan updates instead of duplicating (`count` stable);
3. preview writes nothing (`material_event` count unchanged);
4. merge moves children, unions attributes/tags, keeps K's money, sets S to superseded;
5. candidate pair flips to `merged` with `keep_uid`;
6. `material_event` has one `material_merge` row with actor and plan;
7. merging the same pair again returns `already_merged` and writes no new event;
8. merging into an archived record raises SQLSTATE 22023;
9. search `total` drops by one and the survivor is absent from `material_search`;
10. `material_detail(<merged>)` still resolves with `status='superseded'`.

### 10.3 pytest (`tests/test_merge.py`)

HTTP-level: queue paging, preview 200 + no writes, merge 200 report, 400 keep-not-in-pair,
409 conflict, ignore 200, token gate, `dry_run` leaves the database unchanged
(`total` before == after). The merge tests create their own duplicate pair inside a
transaction and roll back, so the shared `ml_ci_api` database stays clean.

### 10.4 Acceptance criteria (all must hold to call T2 done)

1. `python scripts/duplicate_scan.py --dsn ...` on the fixture DB reports at least the
   two designed pairs and zero decoys.
2. `GET /v1/duplicates?status=open` returns those pairs with a keeper suggestion.
3. `GET /v1/duplicates/{a}/{b}` returns the plan and leaves `material_event` unchanged.
4. `POST .../merge {"keep": <uid>}` makes the keeper the only searchable record
   (`material_search` total -1, survivor absent), with survivor `status='superseded'`
   and `superseded_by` set.
5. `material_event` contains one `material_merge` row (actor + plan) and
   `material_duplicate_candidate.status='merged'` for every pair involving the survivor.
6. `POST .../merge` twice returns 200 with `already_merged: true` and writes nothing new.
7. `pytest -q tests` and `scripts/api_contract.sql` + `scripts/merge_contract.sql` pass
   locally and in the CI `api` job; `python scripts/check_english.py` prints OK.

---

## 11. Risks, caveats, non-goals

| Risk | Mitigation |
| --- | --- |
| Merging is destructive for the survivor's unique fields | preview lists every dropped value; keeper must be explicit; event payload keeps the plan |
| A merge on a 100k-row catalogue moves many child rows in one transaction | `statement_timeout` is 3s on the API pool; the CLI bypasses it. Document that large merges run via CLI with a longer timeout |
| Scan is `pg_trgm`-based, so "16mm MDF black" vs "black 16mm MDF" may score low | threshold 0.80 is deliberately conservative; the queue is a review aid, not ground truth |
| Two operators merge the same pair concurrently | both rows locked `FOR UPDATE`; the second call sees `already_merged` |
| Someone wants the survivor back | section 9; `material_revision` retains both snapshots |

Non-goals for T2: automatic merging, unmerge API, cross-category merges, merging
`archived` records, field-level arbitration by recency (only "K wins, S fills gaps"),
detection of duplicates that share a name but live in different categories, and any
change to search ranking.

---

## 12. Delivery plan (5 steps, verification per step)

| Step | Work | Verified by |
| --- | --- | --- |
| 0 | Preflight: fresh `ml_t2` database, `schema/merge.sql` stub, `eval/fixture.sql` duplicate pairs | fixture loads, `check_english` OK |
| 1 | candidate columns + index and `material_duplicate_scan()` in `schema/merge.sql` (no edits to `material.sql`); `scripts/duplicate_scan.py` | contract assertions 1-2 |
| 2 | `material_duplicate_queue()` + `material_merge_preview()` | contract assertion 3, preview shows all rule rows |
| 3 | `material_merge()` + `material_duplicate_ignore()` | contract assertions 4-8 |
| 4 | API routes + `tests/test_merge.py` + `api/requirements.txt` unchanged | `pytest` green, `GET/POST` walkthrough |
| 5 | CLI `merge_materials.py`, `scripts/merge_contract.sql`, CI job step, docs (`README`, `docs/data-model-v2.md` B7 -> Done, `docs/t2-progress.md`) | full local CI simulation + `check_english` |

Estimated effort: 2.5 - 3 days at the same pace as T1, most of it in the merge rules
and the contract script.

---

## 13. Decisions recorded (from the step-by-step questions)

| Id | Decision | Rationale |
| --- | --- | --- |
| D1 | Scope = scan + queue + merge + API/CLI, one task | keeps B7 closed in one move; splitting leaves a half-finished workflow |
| D2 | Keeper always explicit; tool only suggests | merge is irreversible in v1, so the human must own the choice |
| D3 | Field survival by type (K wins non-empty, S fills gaps, arrays union, children repointed) | loses the least data without inventing arbitration heuristics |
| D4 | Survivor = `superseded` + `superseded_by`; never deleted | matches T1 search/detail semantics and the no-hard-delete policy |
| D5 | Scan: same category, `similarity >= 0.80`, CLI only (no HTTP scan route) | precision over recall; scans write rows, so they do not belong behind GET |
| D6 | No `unmerge()` in v1; documented manual recovery from `material_revision` | unmerge needs conflict rules for post-merge edits; auditing first is cheaper |
| D7 | Merge does not go through `material_change_request` | that table models cross-system write-back approvals; merge is internal master-data maintenance |
| D8 | `material_merge()` lives in `schema/merge.sql`, not in `api.sql` | keeps read payloads and write semantics separate, mirroring T1's split |
| D9 | `GET /v1/duplicates` defaults to `open` + `ignored` | operators want to see what is still actionable (including pairs they parked), not the merged history |
| D10 | `reason` is optional everywhere; the CLI warns when it is absent | forcing prose on every merge produces "x" in the column; a warning keeps intent when it matters |
| D11 | The merge report returns the plan and warnings, not a survivor snapshot | the previous snapshot already lives in `material_revision`; duplicating it costs bytes and invites drift |

---

## 14. Resolved during review

| Question | Answer |
| --- | --- |
| Default queue filter | `open` + `ignored` (D9) |
| Report includes the survivor snapshot? | No, plan + warnings only (D11) |
| Is `reason` mandatory? | No, optional; CLI warns when missing (D10) |
