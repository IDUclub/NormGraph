# Recovery after interrupted extraction

Structural ingestion stores the source hash before restriction extraction runs. A matching
source hash therefore does not prove that extraction succeeded. A partially written set of
restrictions is not proof either.

`Document.extraction_complete` becomes true only after all clause extraction and graph writes
finish successfully. It is cleared before extraction and when ingestion changes the source
hash. A successful analysis with zero restrictions is complete; a document without clauses is
skipped and remains retryable. Reconcile reports skipped ingestion/extraction as failed rather
than counting it as an added or updated document.

Startup reconciliation and `POST /sync/reconcile` retry incomplete documents even when their
hash matches. Existing documents without the marker are retried once to establish completion;
the first reconcile after upgrading can therefore make additional LLM calls. Subsequent
reconciliation skips completed, unchanged documents, including those with zero restrictions.
This does not populate an empty IDU_DVD corpus: upload source documents there first.

Verification: unit tests cover interrupted extraction, partial writes, valid zero results,
idempotent replay, skipped ingestion and unchanged/changed hashes. The live Neo4j lifecycle test
checks that unchanged ingestion preserves completion and a new source hash clears it.
