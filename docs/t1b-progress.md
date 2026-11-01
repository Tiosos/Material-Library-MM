# T1b - the two things the read API deliberately left out

`docs/api-v1.md` 12 lists two gaps that decision D6 and D7 deferred. This log closes them, then
the read surface is finished and the next task is the MCP server (recorded at the bottom).

A step is only done when a command in this file proves it.

## Decisions

| # | Decision | Why |
| --- | --- | --- |
| B1 | Default `GET /v1/materials` collapses a family to one hit; `expand=variants` returns one hit per material record | A shopping surface wants "Laminex black" once, not three thicknesses (`docs/data-model-v2.md:12`). `total`, the cursor and paging are group counts, so a page is never short. |
| B2 | The collapsed hit is the highest-scoring member, ties broken by `material_uid`; it carries `variant_count` (matching members of the family, so at least 1) and `variants` (the other **matching** uids, same order) | Deterministic, and the extras are the minimum a UI needs to render "3 sizes". The list describes the current result set; the whole family is `/v1/materials/{uid}/family`. |
| B3 | The collapse lives in `material_search_page()` (`p_collapse boolean DEFAULT true`), not in `material_search()` | Ranking and the evaluation harness operate on material records; only the paged payload needs groups. `eval_search.py` therefore keeps working on the flat function. |
| B4 | Facet counts stay per material record | Facets are filters over the catalogue; collapsing them would hide values that only appear on a non-representative variant. Documented in `docs/api-v1.md` 3.2 rather than silently changed. |
| B5 | `GET /v1/export` gains `format=json`: one document `{schema_version, exported_at, count, records:[UMR...]}`, validated by a new `schema/export.schema.json` | Answers the review finding that a recipient cannot tell which version and which moment they hold (`docs/review-v1-and-plan-v2.md:213`) without giving up the line-by-line property of `jsonl`. |
| B6 | `jsonl` and `csv` are unchanged | Their strength is that every line stands alone; changing them would break existing consumers and the v1 tests. |

## Steps

| Step | Work | State |
| --- | --- | --- |
| 0 | Record decisions, add the MCP note to `README.md` and `docs/api-v1.md` 12 | [x] |
| 1 | `material_search_page()` collapse: `p_collapse`, group `total`, `variants`, `variant_count` | [x] |
| 2 | Edge: `expand` semantics in `/v1/materials`, keep `next_cursor` consistent with group totals | [x] |
| 3 | `format=json` envelope plus `schema/export.schema.json` | [x] |
| 4 | Contract assertions, pytest updates, `api_contract.sql`, CI unchanged | [x] |
| 5 | Docs (`api-v1.md` 3/4/7/12, README), progress log, commit | [x] |

## Environment

| Piece | Value |
| --- | --- |
| PostgreSQL | 16.15 on `127.0.0.1:55432`, started as a background job: `.pg/bin/postgres.exe -D .pgdata -p 55432 -c listen_addresses=127.0.0.1 -c logging_collector=off` |
| Fresh database | `python .tmp/newdb.py ml_vN [both\|fixture\|blum]` (loads `schema/material.sql`, `search.sql`, `api.sql`, `merge.sql`, `seed.sql`, then the fixture or the sample imports) |
| Python path | `PYTHONPATH=D:\material library\.pylibs` (psycopg, fastapi, pytest, jsonschema) |
| Run SQL file | `python .tmp/run_sql.py <file.sql> --db ml_vN` |
| Databases are never dropped | `DROP DATABASE` fails with `could not signal for checkpoint: Operation not permitted`, so every run uses a new name |

## Step 1 - collapse in SQL

`material_search_page(p_q, p_category, p_brand, p_supplier, p_attrs, p_limit, p_offset, p_measures,
p_collapse boolean DEFAULT true)`. The body is a CTE chain: `hits` (per material record from
`material_search`, up to 5000), `sized` (adds `coalesce(m.family_id, h.material_uid) AS group_key`),
`sizes` (`count(*)`, `array_agg(material_uid ORDER BY score DESC, material_uid)` per group),
`collapsed` (`DISTINCT ON (group_key)`), `flat` (`member_count 1`), `visible` (one of the two, chosen
by `p_collapse`), `page` (`LIMIT`/`OFFSET` over `visible`), `totals`.

| Check | Command | Result |
| --- | --- | --- |
| fixture browse, flat vs collapsed | `material_search_page(p_limit => 5000, p_collapse => false)` vs default | `42` active records vs `37` groups |
| variants of a family hit | `material_search_page(p_q => 'laminex')` | one hit `mat_FX...BRD016`, `variant_count 3`, `variants ["mat_FX...BRD018","mat_FX...BRD025"]`, `total 1`, level `and` |

First attempt used `array_agg(...) OVER (PARTITION BY group_key)` and failed with
`FeatureNotSupported: aggregate ORDER BY is not implemented for window functions`; the grouped
`sizes` CTE replaced it.

## Step 2 - collapse at the edge

`GET /v1/materials` takes `expand: str | None = Query(None, pattern="^variants$")`; the call site
passes `collapse=expand != "variants"` into `_search_payload`, which sends `p_collapse` and echoes
it as `collapsed` in the payload. `next_cursor` is computed from the same `total` the payload
reports, so a collapsed client never sees a cursor past the end.

## Step 3 - export envelope

`format` accepts `jsonl|csv|json`; `stream_json()` writes
`{"schema_version", "exported_at", "count", "records": [...]}` around the same `material_export`
rows the JSONL stream uses. `schema/export.schema.json` (`$id`
`https://materiallib.local/schema/export.v2.json`) validates it and `$ref`s `material.v2.json` for
each record.

Verification (`ml_b5`, fixture + samples): `GET /v1/export?format=json` returns 180 records,
`count` equals `X-Record-Count`, every record validates; `format=xml` is `400 bad_request`;
`format=jsonl` and `format=csv` are byte-identical to before.

## Step 4 - contracts and tests

| Check | Command | Result |
| --- | --- | --- |
| SQL contract | `python .tmp/run_sql.py scripts/api_contract.sql --db ml_b6` | `OK` (assertion 1 now compares the collapsed total with `count(DISTINCT coalesce(family_id, material_uid))` and the expanded total with active rows) |
| pytest | `ML_DSN=...ml_b5 python -m pytest -q tests -p no:cacheprovider` | `37 passed` (new: collapsed browse total, family hit variants, group paging walk, `format=json` envelope; facets compare against an `expand=variants` total) |
| search evaluation (fixture) | `python scripts/eval_search.py --dataset fixture --quiet` on `ml_b6` | `RESULT PASS`, holdout MRR@10 1.000 / hit@1 1.000 / zero 0.0% / level 100% |
| search evaluation (Blum) | `python scripts/eval_search.py --dataset blum --quiet` on a samples-only `ml_b10` | `RESULT PASS`; on the combined `ml_b8` it fails `bl-none-brand-not-imported` because the fixture adds two Hettich hinges - the dataset is defined against the samples only (`docs/api-v1.md` 6.1) |
| merge contract | `python .tmp/run_sql.py scripts/merge_contract.sql --db ml_b7` | `OK` (fresh fixture database; running it after pytest is invalid by design) |
| English only | `python scripts/check_english.py` | `OK: no CJK text found` |

## Step 5 - docs

`docs/api-v1.md` 3.1 (export row, `expand` row), 3.2 (`collapsed`, `variant_count`, `variants`,
group totals), 3.4 (collapse happens before paging; facets stay per record), 4.2 (the CTE chain),
7 (the `format=json` envelope next to D7), 12 (the two rows that used to say "deferred");
`README.md` status list and next step.

## Next task, recorded on request (m01835)

After T1b the next task is the **MCP server on top of the read API**. Same protocol as T1 and T2:
a design document first (`docs/mcp-server.md`), questions asked in small ordered batches, and no
code until the design is approved.
