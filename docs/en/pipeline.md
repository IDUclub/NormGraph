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
   any restrictions derived from them), so a re-ingest leaves no stale clauses. Before that, a new
   clause whose text is word for word an extracted old one **takes over** its restrictions (same
   ids, check plans and reviews; spans shifted to the new place) and its extraction mark — an
   IDU_DVD consolidated edition arrives as all-new fragments, most of them unchanged. Each clause
   also stores `amended_by`: the IDU_DVD amendments whose changes it carries.
6. Mirror IDU_DVD's fragment relations (`GET /library/documents/{doc_id}/relations`) as
   `(:Clause)-[:DEPENDS_ON {weight, kind}]->(:Clause)`, replacing the document's previous ones.
   Only relations between clauses of this version are kept. An IDU_DVD without relations (404) or
   an unreachable endpoint leaves the structural layer in place without them.

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

### Linked clauses and references (`clause_context.py`)

A clause often gives its value elsewhere: «по таблице 7.2», «в соответствии с п. 4.2.1», the list
items after a lead-in «должно составлять:». The graph names those clauses:

- `REFERENCES` to a clause, resolved by IDU_DVD or by NormGraph at ingest. An internal reference
  without a clause number («таблицей 6.1 настоящих Нормативов», «пункт 4.2.1») is tied to the clause
  with that number or to the fragment titled «Таблица 6.1» when the number is unique in the document;
- `REFERENCES` to a document or a pending reference: the clause with the referenced number in the
  NormGraph document whose name starts with the referenced name («СП 42.13330» → «СП 42.13330.2016 …»);
- `DEPENDS_ON`: IDU_DVD relations of weight 0.7 or more except `same_topic` (`completes`, `refines`,
  `table_ref`, `condition`, `exception`, `definition`).

IDU_DVD keeps a table as its caption («Таблица 6.1 …») and, a few fragments later, its body
(`kind=table`): a reference to the caption gets the body too. Amendment notes («(в ред.
постановления … N 1809-ПП)») are not linked clauses. The extractor reads
the linked clauses before the clause text (up to `NG_EXTRACTION_CONTEXT_CHARS` characters, 800 per
clause): a list item thus takes its object and indicator from the lead-in («— не более 250 м» is the
walking distance to a stop). A separate instruction block in the prompt made the model return
nothing, so the context is part of the text. Restrictions still come from the clause itself: one
quoted entirely from a linked clause is dropped without an error, its own clause yields it. A quote
may start in the lead-in when its end (at least two words or numbers) is the clause's text. A value
found neither in the quote nor in the clause is accepted only when a shown linked clause states it;
that clause is stored as the restriction's `value_source` (`Restriction.value_source_json`).
References whose text is not in NormGraph (the document is not loaded or the clause is not found) are
stored as `unresolved_references` of a restriction without a numeric value: candidates to load into
IDU_DVD.

The planner sees the same clauses: the rewrite and verify passes get them after the clause, and the
number-in-source check includes their text. Re-planning reads them from the graph, so «Перестроить все
планы» uses the links without re-extraction. New links appear after syncing the document with
`replace=true` (structure refresh); values given by reference after re-extraction.

### Explanations (`src/ingestion/explanations.py`)

IDU_DVD links a clarifying document to the document it explains (`explains` on the act's `/library`
document). The clarification is synced as a document of its own; for the explained document its
clauses are context, shown as `[разъяснение] <document>, п. N` the way a referenced clause is. An
explanation clause addresses the clauses it cites (a resolved reference, or the explained document's
clause by number) and, when it cites none there, the nearest ones by IDU_DVD vector search
(`NG_EXPLANATION_PER_CLAUSE`, at least `NG_EXPLANATION_MIN_SCORE`). The links are `EXPLAINS` edges,
rebuilt whenever either document syncs (IDU_DVD announces a linked or unlinked explanation as
`DocumentUpdated` of the act). Only the explained clauses whose explanations changed are extracted
again — also when the explanation is deleted. A new edition keeps the links of the clauses it carries
over. User documents are not linked.

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
not collapse into one norm. Existing restrictions without metadata remain readable.

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
and `same_entity_spacing_not_supported`. The LLM passes cannot introduce new layer entities
or bypass these guards. They do not verify actual Urban API layer availability.
An unexpected planner exception is isolated per restriction, recorded as `planner_failed` and in
`ExtractResult.warnings`; subsequent norms still get written. Database failures still propagate.

After deployment, regenerate affected stored plans. To obtain new measurement fields, explicitly
re-extract the document. Re-extraction can change entities, metadata and therefore restriction IDs;
the default non-replacing extraction retains old restrictions too. Review existing revisions before
choosing a document replacement. Neither deployment nor startup backfill automatically migrates
all stored plans: plans of an older planner version are re-planned with
`POST /check-plans/replan` (dry run first). These checks are conservative guards, not a guarantee of arbitrary LLM output's
semantic correctness.

### Restriction kinds (`src/pipeline/kind_taxonomy.py`)

The restriction *kind* is one of a **closed list** of 20 kinds, stored as approved
`:RestrictionKind` nodes: `запрет_размещения`, `запрет_использования`, `требование_размещения`,
`допустимость`, `минимальное_расстояние`, `максимальное_расстояние`, `время_доступности`,
`минимальный_размер`, `максимальный_размер`, `предельная_высота`, `минимальная_доля_площади`,
`максимальная_доля_площади`, `плотность_застройки`, `обеспеченность`, `количество`, `срок`,
`физический_параметр`, `требование_к_объекту`, `процедурное_требование`, `прочее`. The prompt
lists them with what each covers and forbids new codes. (The model used to coin a kind whenever
none of the eight seed kinds fit; that grew to hundreds of kinds, most used once.)

Whatever label the model returns is kept as `kind_label` and mapped to a listed kind:

1. a norm **with a number** gets the kind of its quantity, from the unit, the measurement and the
   label: «не более 500 м» to a stop is `максимальное_расстояние` even when the model said
   `минимальное_расстояние` or `требование_размещения`; the quote is consulted only when the label
   and unit say nothing;
2. otherwise a listed label is kept, and a coined one is mapped by its words
   (`запрет_пересмотра` → `запрет_использования`, `требование_схемы` → `процедурное_требование`);
3. a label still unmatched is compared by embedding (cosine ≥ `NG_KIND_MATCH_THRESHOLD`) with the
   listed kinds; failing that, the kind is `прочее` with `kind_status="pending"`.

Restrictions extracted before the list was closed are mapped by «Свести виды и дубли» in the admin
panel (see [admin](admin.md)), without the LLM and without changing ids or plans.

### Duplicate norms (`src/pipeline/duplicates.py`)

The same norm is extracted more than once: a revised document repeats its predecessor
(СП 2.4.3648-20 and СП 2.4.2.4283-26 share about two hundred norms), a list item repeats in several
sections. Such restrictions are **kept** — each has its own provenance and plan — and share a
`duplicate_group`. Two restrictions of the shared corpus are duplicates when they have the same
`norm_key` (canonical subject and object, kind, value with its condition) and their quotes say the
same (half of the words in common, or most of the shorter quote within the longer). A norm joins
the group of its duplicates when it is written; user documents are not grouped.

Search and `applicable` return one restriction per group and list the others in its `duplicates`
(`collapse_duplicates=false` returns each); the restriction detail lists them too.

### Entity resolution / dedup

Subject and object are resolved to canonical `:Entity` nodes the same way (exact → fuzzy ≥
`NG_ENTITY_MERGE_THRESHOLD`), keeping aliases. This cross-document dedup is what lets restrictions
from different documents connect via `SHARES_ENTITY`. The deeper terminology store is a deferred
TODO — for now the canonical form is the first-seen normalized name.

### Graph write (`src/pipeline/service.py`)

For each extracted restriction:

- resolve `kind`, `subject`, `object`;
- embed `subject | object | kind [| value]` plus the clause sentence (`extraction_text`), stored with
  `embedding_version`; vectors of an older version are recomputed at startup;
- compute a **deterministic id** = hash of `clause + subject + object + kind + value` (so
  re-extraction converges instead of duplicating);
- upsert `:Restriction` and wire `DERIVED_FROM`, `HAS_SUBJECT`, `APPLIES_TO`, `OF_KIND`;
- join the duplicate group of the same norm stated elsewhere (`norm_key`, see "Duplicate norms");
- rebuild `SHARES_ENTITY` links to co-referencing restrictions.
- build a versioned `CheckPlan` with the multi-pass planner (below); unresolved norms
  receive `planner_status=unsupported` with their reasons.

Plans are stored as separate `:CheckPlan` nodes with immutable revisions. Automatic
re-extraction never overwrites a `reviewed` plan. The expert-review queue supports
approve, reject and replace while recording reviewer, timestamp and comment. Legacy
restrictions without a plan remain readable without a bulk migration.

### Multi-pass CheckPlan planner (`check_plan_planner.py`, `norm_*.py`)

A plan is `auto` only after every enabled pass accepts it:

0. **Data.** Urban API holds no red lines. A norm that needs them (`красная линия`,
   `линия (регулирования) застройки` in its subject, object, quote or candidate layers) is
   `unsupported` with `red_line_not_in_data` first among its reasons; the LLM passes are skipped,
   the deterministic candidate and the other reasons are kept for review. The guard is
   `norm_guards.mentions_red_line`: drop it once red lines reach the data and rebuild the plans.

1. **Deterministic.** The whole-clause grammar (`spatial_rules.py`), then the allowlisted triple
   planner. Precision guards (`norm_guards.py`) block defects seen in production: a quantity label
   used as a layer (`отступ от красной линии`, reason `non_spatial_entity` /
   `measure_label_as_entity`), an upper bound read as a minimum (`operator_direction_conflict`), a
   rhythm along a line (`не реже чем через 100 м`, `periodic_spacing_not_supported`), vertical
   depth (`depth_not_distance`), in-building scale (`distance_below_territorial_scale`, below
   `NG_CHECK_PLAN_MIN_DISTANCE_M`) and shares of building parts (windows/walls).
2. **Grounding.** Every layer entity must be a canonical Urban API type (service, physical object
   or functional zone code) from `NG_URBAN_API_URL`; otherwise `entity_not_in_catalog`. A name
   resolves in any case and number, and a norm's own wording of a type («общеобразовательные
   организации» → «Школа», «краеведческий музей» → «Музей», `catalog_aliases.py`) when it names
   exactly one catalog type. The compliance executor resolves entities against the same
   dictionaries.
3. **Rewrite (LLM).** A norm without a grounded plan is re-read from its whole clause (with document
   name and breadcrumb) into a closed `NormSpec` (`norm_spec.py`). The LLM never writes a plan:
   `SpecCompiler` builds it and refuses when an entity is not in the catalog, the number does not
   occur in the clause (`value_not_in_source`), the operator contradicts the template or the unit does
   not fit. A clause with conditions or case-dependent values (settlement type, storeys, population,
   exceptions) lists them in `NormSpec.conditions` and `NormSpec.variants`: every variant is compiled
   and the strictest one is applied to all objects (largest minimum distance, smallest maximum
   distance or accessibility, densest provision norm; `variants_not_comparable` when the variants are
   of different kinds). Such a plan stays `auto` and carries `applicability`
   (`mode: strictest_variant`, `conditions`, `variants`, `applied`); gMART marks its verdict as the
   strictest norm whose conditions must still be checked. A value stated for one type of
   residential buildings (`NormSpec.housing`: individual up to 3 floors, low-rise up to 4,
   mid-rise 5–8, multi-storey from 9) is checked on those buildings only: the plan carries
   `scope` (layer, floors attribute, range, condition) and the values for other building types
   do not compete for the strictest one. Transport accessibility is planned with
   `mode: transport` and a radius at `NG_CHECK_PLAN_TRANSPORT_SPEED_KMH` (a rough estimate without a
   road graph, flagged in gMART reports); provision norms in square metres per 1000 residents are
   refused (`provision_area_not_in_data`: Urban API holds no floor areas of services), and so is a
   number without «не менее/не более» in its clause (`direction_not_in_source`). The rewrite runs up
   to `NG_CHECK_PLAN_REWRITE_VOTES` times at different temperatures and is accepted when
   `NG_CHECK_PLAN_REWRITE_AGREEMENT` votes compile to the same plan (otherwise
   `rewrite_votes_disagree`); a vote judging the norm uncheckable ends the pass. Every call is
   seeded by the restriction, so re-planning the same norm samples the same answers; a long clause
   is shown as its head (a table's column titles) plus the restriction's own row. Obvious
   non-territorial norms (millimetres, materials, documents, …) skip this pass.
4. **Verify (LLM).** An independent prompt sees the clause and a plain-language rendering of the plan
   and answers `faithful`, `checked_side_ok`, `direction_ok`, `value_ok`, `unconditional`,
   `territorial` (for a strictest-variant plan `strictest_ok` instead of `unconditional`); any
   `false` blocks the plan (`verifier_<question>_failed`). Deterministic plans are verified too;
   grammar plans are exact and are not.

A blocked plan keeps the best candidate in `params.candidate_plan` for review. Each revision stores
`planner_version` and a `trace` of its passes (`GET /check-plans/{id}/revisions`).

Templates of CheckPlan v1: `distance_from_source` (minimum distance, or `source_geometry` for a
prohibition inside zones/objects), `distance_table`, `presence_within`, `accessibility_within`
(walking time or route length, measured as a straight-line buffer `buffer_v1`:
`(minutes × 80 m/min | metres) / 1.3`), `object_attribute_threshold` (floors, height via
`floors_to_height_v1`, building/plot area via `geometry_area_m2_v1`), `zonal_attribute_threshold`,
`zonal_ratio` and `service_provision` (places per 1000 residents or "1 object per N residents" —
`residents_per_service`, every resident is then demand — and accessibility from the norm, computed by
ObjectEffectsAPI `CalculateNormativeProvision`).

`scripts/evaluate_check_plans.py` re-plans a live corpus offline (read-only: restrictions from
NormGraph, clause texts from IDU_DVD) and writes transitions, block reasons and a review sample.
With `--gold scripts/check_plan_gold.jsonl --repeats 3` it scores the planner against
hand-labelled clauses instead: precision (automatic plans that match the label), recall (labelled
plans found) and stability (clauses planned the same way in every run), with a report of every
disagreement.

`extract_document(..., replace=True)` replaces restrictions clause by clause: a clause's old
restrictions are dropped only once its new LLM output is valid. On a partial extraction the failed
clauses keep their previous restrictions, the others get the fresh ones; it returns
`replaced=false`, warns `replacement_partial` and stores the failed clause ids on the document
(`extraction_failed_clause_ids`, cleared when a run starts, so an interrupted run leaves none).
`extract_document(..., clause_ids=[...])` re-extracts and replaces only those clauses. Structural
ingestion may still prune clauses removed from changed source text before extraction.

Every clause extracted with a valid result records `extracted_hash` — its text (whitespace-insensitive)
under `EXTRACTION_VERSION` (`src/pipeline/reuse.py`; bump it when extraction changes what it returns
for the same text). A sync of a changed document runs `extract_document(..., replace=True,
reuse=True)`: clauses whose hash still matches keep their restrictions, only new and edited text goes
to the LLM, and only those clauses lose their previous restrictions. The admin re-extraction still
re-does everything.

## 3. Sync lifecycle (`src/sync`)

Keeps the graph in step with IDU_DVD.

### Kafka consumer (`consumer.py`)

Consumes IDU_DVD's `document.events` topic via **otteroad** (Avro + Schema Registry). The event
models (`events.py`) are a byte-for-byte copy of IDU_DVD's producer models — otteroad matches
messages to handlers by the registry schema string, so they must stay identical. Handlers queue a
job on the sync queue (below), stamped with the event's broker timestamp:

| Event | Job |
|---|---|
| `DocumentProcessed` | new document → `sync_name(replace=False)` (ingest + extract) |
| `DocumentUpdated` | changed → `sync_name(replace=True)` (re-ingest + re-extract, prune stale) |
| `DocumentDeleted` | removed → `delete_name` (drop the document/versions from the graph) |

The consumer is disabled until `NG_KAFKA_BOOTSTRAP_SERVERS` is set.

### Sync queue (`queue.py`)

Kafka events and reconcile only schedule jobs; one worker runs them **starting with the document
changed in IDU_DVD most recently** (the event's broker timestamp; `uploaded_at` for reconcile). A
fresh upload no longer waits for a backlog (a bulk reparse, a first boot) — it overtakes it right
after the document in progress.

Jobs are keyed per document (name + `user_id`/`scenario_id`; `doc_id` for reconcile). A new event for
a document that is still waiting merges into its job: deletions run first, then one sync of IDU_DVD's
current state, so one document's own events keep their order. `document_removed=true` cancels a
pending sync. A reconcile job decides what to do when it runs, so a document an event already synced
is not processed again.

The offset is committed as soon as a job is queued, so pending jobs are persisted in Neo4j
(`:SyncJob`) and restored on start. A failing job (IDU_DVD or Neo4j down) is retried with a pause
growing from 60 s to 15 min and is never dropped. `GET /sync/status` shows the queue.

### Startup reconcile (`service.py: reconcile`)

Diffs the IDU_DVD library listing against the graph by `content_hash`: documents present in DVD but
not the graph are synced; changed ones re-synced with `replace=True`; ones gone from DVD deleted.
Documents are visited newest first and scheduled on the sync queue; the result counters report what
was queued (`queued`). A
document whose edition label alone changed (IDU_DVD relabels editions without an event — manual
edits, `POST /documents/version-repair`) is re-ingested structurally, keeping its restrictions. A
single failing document never aborts the pass.

An incomplete document with an unchanged hash is retried: stale clauses are pruned first, then only
its failed clauses are re-extracted. The whole document is re-extracted when pruning removed
clauses (IDU_DVD reparsed it under the same hash) or the failed clauses are unknown. A document an
event has just re-extracted therefore costs only its few failed clauses, not a second pass.

### "Only unprocessed events" & the idempotency guard

Committed offsets on a stable `group.id` (`normgraph-sync`) mean each event is processed **at most
once** across restarts; otteroad commits **after** a handler succeeds, i.e. once the job is
persisted in the queue (at-least-once). On the first
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
