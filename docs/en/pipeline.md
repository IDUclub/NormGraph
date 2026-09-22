# Pipeline

NormGraph builds and maintains the graph in two layers — **structural ingest** and **restriction
extraction** — tied together by the **sync** lifecycle.

## 1. Structural ingest (`src/ingestion`)

No LLM involved; fast and idempotent.

1. Fetch the document from IDU_DVD: `GET /library/documents/{doc_id}` → assembled text + ordered
   fragments (clauses) with hierarchy, tags, source grounding, and `references`.
2. Upsert `:Document` (`MERGE` on `doc_id`, `SET += props` incl. `content_hash`).
3. Upsert each `:Clause` (`MERGE` on `node_id`) and attach `IN_DOCUMENT`.
4. Build edges once all clauses exist:
   - `PART_OF` from each fragment's `parent_id`;
   - `REFERENCES` from each fragment's `references`, choosing the target by how far IDU_DVD resolved
     it: a resolved clause id → `:Clause`; a resolved whole document → `:Document`; unresolved → a
     `:PendingReference` stub (which auto-connects once the target document is later ingested).
5. On `replace=True` (a changed document), clauses dropped by the new version are **pruned** (with
   any restrictions derived from them), so a re-ingest leaves no stale clauses.

All writes `MERGE` on natural keys, so ingesting documents out of order — or twice — converges to the
same graph.

## 2. Restriction extraction (`src/pipeline`)

Runs per clause; needs the LLM and the embedder.

### Extraction (langextract)

- `src/pipeline/prompts.py` holds the prompt and the reviewed few-shot examples (derived from
  СП 42.13330.2016), plus the seed kind vocabulary.
- `src/pipeline/extractor.py` runs `langextract.extract(...)` over the clause text with our
  provider-backed model (`src/providers/langextract_backend.py`, which routes langextract through
  the configured `LLMProvider`), then maps the result to `ExtractedRestriction`
  (`{subject, object, kind, value}` + source offsets). langextract runs in a worker thread; malformed
  or non-JSON chunk responses are retried (three attempts total per prompt). A valid empty
  `extractions` array is accepted without retry. Parsing errors are never silently suppressed.

`value` is encoded as flat string attributes (`value_operator`/`value_number`/`value_unit`/
`value_condition`) and parsed back into a structured `RestrictionValue`. A clause with conditional
norms yields several extractions — one per value.

If retries fail, that clause is excluded from writes and returned in `failed_clause_ids`, with
`incomplete=true`, `reason=invalid_llm_output` and details in `warnings`. `clauses_processed`
counts successful clauses, including those with zero norms. Other clauses still get written.
The document's `extraction_incomplete` marker persists before work starts and is cleared only
on successful completion. Sync and startup reconciliation retry incomplete documents even
when their content hash is unchanged. Backfill reports partial results as failed with counts
and warnings; its selection still targets documents with zero restrictions. Use the document
extraction endpoint to retry other incomplete documents. A document retry extracts all clauses;
within a run, response retries repeat only the failing chunk's prompt. Avoid concurrent runs for
the same document. Previously skipped chunks are not detected retroactively: re-extract those documents.

### Measurement semantics and plan generation

Entity names (`subject`, `object`) are separate from the measured indicator and calculation basis.
The extractor reads flat attributes `measurement_kind`, `measurement_indicator`, `measurement_basis`,
`measurement_numerator_entity`, and `measurement_denominator_entity` into an optional `measurement`
model. Kinds are `area_share`, `count_share`, `provision`, `distance`, `linear_size`, and `other`.
The basis is not an applicability condition: “90% of the calculated motorization level” describes
the denominator; “in rural settlements” describes applicability. Keep the full supporting sentence
in `extraction_text`.

The graph stores this internal metadata as `Restriction.measurement_json`; both backfill and
regeneration restore it. It is included in restriction identity so different calculation bases do
not collapse into one norm. Existing restrictions without metadata remain readable. The public
CheckPlan v1 schema and executor templates are unchanged.

`zonal_ratio` is an area/area template. It requires explicit numerator and denominator entities,
an area basis, and supporting area wording in the source. The numerator is the measured object's
area; the zone is the denominator territory. Percentages of demand, parking provision, vehicle
counts or population cannot use it. Old percentage restrictions without an explicit measurement
need re-extraction; regeneration alone returns `ratio_basis_not_supported`. Width/height/length
norms do not become inter-object distance buffers.

Unrepresentable measurements and entity names longer than 200 characters produce an `unsupported`
plan with reasons (including `unsupported_measurement`, `ratio_basis_not_supported`, or
`entity_label_too_long`). Names and source text are never truncated. Metadata and conditions remain
in the blocked plan's parameters. Invalid deterministic parameters also produce an unsupported plan.
Spatial guards also reject indicator labels such as “calculated radius” or “accessibility level”,
turning radii/diameters, distances requiring entrance geometry, and same-entity spacing.
Reasons include `non_spatial_entity`, `linear_size_not_distance`, `specific_geometry_required`,
and `same_entity_spacing_not_supported`. The LLM fallback cannot introduce new layer entities
or bypass these guards. They do not verify actual Urban API layer availability.
An unexpected planner exception is isolated per restriction, recorded as `planner_failed` and in
`ExtractResult.warnings`; subsequent norms still get written. Database failures still propagate.

After deployment, regenerate affected stored plans. To obtain new measurement fields, explicitly
re-extract the document. Re-extraction can change entities, metadata and therefore restriction IDs;
the default non-replacing extraction retains old restrictions too. Review existing revisions before
choosing a document replacement. Neither deployment nor startup backfill automatically migrates
all stored plans. These checks are conservative guards, not a guarantee of arbitrary LLM output's
semantic correctness.

### Kind vocabulary (`src/pipeline/vocabulary.py`)

The restriction *kind* is a **controlled, dynamically-extensible** vocabulary stored as
`:RestrictionKind` nodes. Resolution is two-tier:

1. **exact** — by normalized name or an existing alias;
2. **fuzzy** — embedding cosine similarity ≥ `NG_KIND_MATCH_THRESHOLD` against the kind vector
   index; the incoming label is filed as an alias of the match.

No match → a new kind node is created with `status="pending"` for later review.

### Entity resolution / dedup

Subject and object are resolved to canonical `:Entity` nodes the same way (exact → fuzzy ≥
`NG_ENTITY_MERGE_THRESHOLD`), keeping aliases. This cross-document dedup is what lets restrictions
from different documents connect via `SHARES_ENTITY`. The deeper terminology store is a deferred
TODO — for now the canonical form is the first-seen normalized name.

### Graph write (`src/pipeline/service.py`)

For each extracted restriction:

- resolve `kind`, `subject`, `object`;
- embed a short `subject | object | kind [| value]` text;
- compute a **deterministic id** = hash of `clause + subject + object + kind + value` (so
  re-extraction converges instead of duplicating);
- upsert `:Restriction` and wire `DERIVED_FROM`, `HAS_SUBJECT`, `APPLIES_TO`, `OF_KIND`;
- rebuild `SHARES_ENTITY` links to co-referencing restrictions.
- build an optional versioned `CheckPlan` from the pinned executable-template
  manifest: deterministic rules run first, followed by a bounded JSON-only LLM
  fallback; unresolved norms receive `planner_status=unsupported`.

Plans are stored as separate `:CheckPlan` nodes with immutable revisions. Automatic
re-extraction never overwrites a `reviewed` plan. The expert-review queue supports
approve, reject and replace while recording reviewer, timestamp and comment. Legacy
restrictions without a plan remain readable without a bulk migration.

`extract_document(..., replace=True)` drops old restrictions only after all clause LLM responses
are valid. On a partial extraction it retains them, writes successful clauses, returns
`replaced=false` and warns `replacement_deferred`. Structural ingestion may still prune clauses
removed from changed source text before extraction.

## 3. Sync lifecycle (`src/sync`)

Keeps the graph in step with IDU_DVD.

### Kafka consumer (`consumer.py`)

Consumes IDU_DVD's `document.events` topic via **otteroad** (Avro + Schema Registry). The event
models (`events.py`) are a byte-for-byte copy of IDU_DVD's producer models — otteroad matches
messages to handlers by the registry schema string, so they must stay identical. Handlers:

| Event | Action |
|---|---|
| `DocumentProcessed` | new document → `sync_name(replace=False)` (ingest + extract) |
| `DocumentUpdated` | changed → `sync_name(replace=True)` (re-ingest + re-extract, prune stale) |
| `DocumentDeleted` | removed → `delete_name` (drop the document/versions from the graph) |

The consumer is disabled until `NG_KAFKA_BOOTSTRAP_SERVERS` is set.

### Startup reconcile (`service.py: reconcile`)

Diffs the IDU_DVD library listing against the graph by `content_hash`: documents present in DVD but
not the graph are synced; changed ones re-synced with `replace=True`; ones gone from DVD deleted. A
single failing document never aborts the pass.

### "Only unprocessed events" & the idempotency guard

Committed offsets on a stable `group.id` (`normgraph-sync`) mean each event is processed **at most
once** across restarts; otteroad commits **after** a handler succeeds (at-least-once). On the first
start, `NG_KAFKA_AUTO_OFFSET_RESET=earliest` consumes the backlog once, then only new events run.

Because event replay (first-boot backlog, redelivery, retry, or overlap with reconcile) can re-invoke
`sync_document`, an **idempotency guard** makes it cheap: on a non-`replace` sync, if the document is
already in the graph with the same `content_hash` **and** already has restrictions, the (cheap)
ingest still runs but the expensive extraction is **skipped** (`extraction_skipped=true`). Anything
that actually changed (new/updated content, or a doc ingested-but-never-extracted) is processed.

## End-to-end

```text
event / reconcile / manual  ──▶  sync_document
   guard (unchanged + has restrictions?) ──yes──▶ ingest only, skip extraction
                                          ──no───▶ ingest ─▶ extract ─▶ restrictions + edges
```

### Grounded spatial plans

Explicit supported spatial clauses are compiled before LLM extraction. The compiler
matches the entire clause, preserves the quantified object, combines floor-dependent
distance bands into one restriction, and separates building attributes from entity
names. `measurement_json` retains the attribute, bands and neighbor count. Exact
aliases are bounded; unknown entities, qualifications and calculation bases still
require ordinary extraction/review. No condition is removed to make a plan runnable.

`functional_zones` denotes all zones; a named zone subtype remains a separate
requirement. Floor checks use `building.floors` and permit partial coverage with missing
values reported as unchecked. Clauses explicitly requiring contours demand polygons;
point-only services cannot satisfy them. LLM quotations and numeric values must be
grounded in the source; invalid output marks the extraction incomplete, preserving
the previous extraction during replacement. Section numbers are not floor limits.

After deploying this change, re-extract documents with split/incorrect restrictions
using `POST /sync/documents/{doc_id}?replace=true`. Missing-plan backfill
alone does not repair existing plans or recombine split source clauses. Regeneration
can repair an individual saved restriction only when its full source quotation is
available. Reviewed decisions should be considered before replacing a document.
