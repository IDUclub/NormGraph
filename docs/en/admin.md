# Administration panel

The built-in UI lives at `/admin/ui` (locally `http://localhost:8020/admin/ui`). It follows
IDU_DVD's visual and IDU login conventions, requires no frontend build, and offers dark/light
themes. `/` still redirects to Swagger.

## Configuration and login

```dotenv
NG_AUTH_HELPER_URL=https://your-idu-auth-helper.example
NG_AUTH_HELPER_API_KEY=your-helper-api-key
NG_AUTH_HELPER_TIMEOUT=15
NG_ADMIN_ROLE=ADMIN
```

Use the same auth helper as IDU_DVD; the URL is its base, without `/api/token`. Existing
`NG_SERVICE_AUTH_SERVER_URL` / `NG_SERVICE_AUTH_REALM` identify the verified Keycloak issuer.
The app still needs its existing `NG_SERVICE_AUTH_CLIENT_ID` / `NG_SERVICE_AUTH_CLIENT_SECRET`.
Restart the app and sign in with an IDU user carrying the configured realm role.

Passwords are not retained. The verified access token is kept in an HttpOnly, SameSite=Strict
cookie scoped to `/admin/ui`, with Secure on HTTPS. Expired sessions require login again.
Configure trusted proxy headers when serving HTTPS behind a reverse proxy so the app sees
the external scheme and host. Helper keys and service credentials never reach browser code.
Existing HTTP/MCP service-token authentication is unchanged; admin cookies only unlock the
admin API. All admin data and operations require the configured role.

## Inspecting documents

The Documents view searches **stored NormGraph documents**, by name substring or ID. It is
not an IDU_DVD catalogue. Detail cards show versions, corpus, user/scenario scope, clause and
restriction counts, failed clause IDs, and paginated source text and restrictions.

| State | Interpretation |
|---|---|
| Complete | `extraction_incomplete=false`; the last extraction finished without recorded clause failures. Zero restrictions is a valid result. |
| Incomplete | `extraction_incomplete=true`; extraction is running, interrupted or partial. Refresh and inspect logs to distinguish these cases. |
| Unknown | Clauses exist but no extraction marker was recorded. This includes unprocessed and legacy documents, even if restrictions exist. |
| No clauses | No clauses are stored; the document node may be a cross-reference stub. |

Overview counts cover the whole graph, including reference stubs. Kafka status is configuration,
not proof of event delivery. Technical completion does not establish semantic completeness.

## Operations

Sync accepts a document ID or exact name (all matching versions). For a user document index,
supply both user and scenario IDs. ID-based resync preserves an existing document's ownership
and rejects attempts to change its scope. Normal sync skips extraction for unchanged, completed
documents with restrictions. Detail-card extraction retries work from stored clauses and add
results. The explicit replacement option uses `replace=true`, refreshes structure and replaces
old restrictions after successful extraction; the UI asks for confirmation.

Single-document operations use long-running HTTP requests. The UI shows counters,
warnings and failed clause IDs; the last operation result lasts only for the current browser
tab. Logs provide persistent diagnostics. Avoid overlapping processing of the same document
across tabs, API calls or automatic sync. Reverse-proxy timeouts can expire before extraction
finishes; after a connection failure inspect the document and logs before retrying.

### Reprocess all restrictions and plans

The Operations button starts a background replacement after confirmation for **all loaded
documents**, including user documents, using their stored clauses. Reference placeholders without
clauses are excluded. It does not reparse files or refresh structure in IDU_DVD. The document set
is captured at startup; later uploads are handled by a subsequent run.

Documents run sequentially with `replace=true`. Replacement retains the existing extraction's
partial-failure safeguards and protection of expert plans with the same restriction ID. Errors
and warnings are counted without stopping other documents. Progress includes the current document,
counts and the first 100 errors. Closing the tab does not stop the job; reopening restores progress.

Duplicate starts and single-document admin mutations return `409` while a job is active. The job
and guard are process-local, not a distributed durable queue. A service restart interrupts the job
and resets its progress; it does not resume automatically. Avoid simultaneous service API or Kafka
processing of the same documents.

Settings are a read-only allowlist without secrets. Edit environment variables and restart to
change them. Download logs from the Operations view.

## Admin API

All `/admin/ui/api` routes require the admin session cookie. Mutations and login require
`X-NormGraph-Admin: 1` and same-origin checks; existing service APIs remain separate.

| Method and path under `/admin/ui/api` | Purpose |
|---|---|
| `GET /overview` | Graph counts and sync configuration |
| `GET /documents?query=&state=&after=&limit=50` | Search and filter stored documents |
| `GET /documents/{doc_id}` | Document status |
| `GET /documents/{doc_id}/clauses` | Paginated clauses |
| `GET /documents/{doc_id}/restrictions` | Paginated restrictions |
| `POST /sync` | `{target, by: "id" or "name", user_id?, scenario_id?, replace: false}` |
| `POST /documents/{doc_id}/extract` | `{replace: false}` |
| `POST /reprocessing` | Start background replacement for the loaded corpus; `202`, no parameters |
| `GET /reprocessing` | Latest job status, progress and errors |
| `GET /settings` | Safe configuration fields |
| `GET /logs` | Download application log |

Document, clause and restriction lists return `items`, `has_more`, `next_after` and accept the next cursor as `after`. The maximum
page size is 100. Sorting is by ID, not clause order.
