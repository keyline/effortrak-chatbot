# Effortrak governed natural-language reporting

This separate Python/FastAPI project interprets questions into typed query plans.
CodeIgniter authorizes each plan and generates parameterized SQL from its approved
reporting catalogue. The model never executes SQL or receives returned records.
The Web and Flutter clients render the same versioned tables, charts and files.

## Run locally

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --no-proxy-headers
```

Keep this directory outside the public document root. Use the existing authenticated
Web/App gateways. Direct requests to Python require the existing short-lived backend
delegation; the service also restricts inbound connections to loopback. Local sample
mode provides authentication/testing only and never answers business-data questions.

## Configuration and existing controls

Use `.env.example` as a reference. Do not replace deployed `.env` files wholesale.
Real reporting requires `DATA_MODE=backend`, `CHAT_MODE=openai`, the existing model/key,
backend URL and shared service secret. Full typed questions and selection metadata
reach the configured AI provider; retrieved records, descriptions and locations do not.
The retired `SEMANTIC_QUESTIONS` switch is ignored: this branch has one query engine.

Existing company/user AI access, licensing, role permissions, token accounting,
quota reservations/settlements and authorized usage pages remain authoritative.
`AI_TOKEN_METERING_ENABLED` and both existing help switches retain their behavior.
No company or user identity is accepted from question text or query-plan fields.
Salary, credentials, bank/tax identifiers and other unapproved fields are absent from
all query positions, including filters, grouping, sorting, charts and exports.
Leave reasons are an explicit exception for Admin and Super Admin with existing
leave-read permissions, within their own company. The backend checks the current
database role for every use of `leave.reason`, including filters/grouping and exports.
Other roles cannot access reasons, even on their own records. Reasons stay out of
AI prompts; the clients and export generators render authorized returned values.

## Source map

- `app/query/models.py`: typed interpretation and reporting contracts.
  Model-facing schemas separate detail and aggregate shapes and constrain names
  to catalogue vocabulary; mixed detail/aggregate selections are excluded before
  generation. Runtime Python and PHP validation remain independent safeguards.
- `app/query/refinements.py`: typed column additions, aggregate, sort, filter, period and pagination
  edits. The application copies the previous plan and changes only the requested
  operation; aggregation and sorting preserve its access selections and filters.
  These operations apply across approved datasets, without per-question matching.
- `app/query/interpreter.py`: one strict, metered interpretation call, with at most
  one additional metered call if local plan validation fails. Both share the existing
  interpretation deadline; invalid plans never execute. Diagnostic logs contain only
  validation locations/types, not questions, returned records or field values.
- `app/query/service.py`: orchestration, clarification and backend access.
  Explicit employee selectors use the user's authenticated company scope
  when the interpreter defaults to self; PHP still resolves and authorizes the
  employee. Naming yourself keeps personal scope. Role text never grants scope.
- `app/query/subjects.py`: binds a required subject extracted before the plan in
  the same AI call. New names/IDs must occur in the current question; a dropped
  plan selector is restored before authorization. Unspecified or conflicting subjects
  clarify instead of defaulting to self. Follow-ups may reuse only the exact prior
  employee selector, retaining its resolved ID when available;
  backend denials retain intent only, and every subsequent read is authorized again.
- `app/query/presentation.py`: deterministic answers, tables and chart data.
- `app/query/specialized.py`: retained shift, work, balance and location business readers.
  `punch_locations` reuses the attendance location reader for recorded punch
  times and addresses, including its separate attendance-location permission.
- `app/query/exports.py`: Excel/PDF generation, formula protection, download receipts.
- `app/assets/effortrak-logo.png`: bundled Web wordmark used by every PDF page
  header and the Excel report branding row. Include this asset when deploying
  the separate AI service; exports fall back to a text brand if it is absent.
- `app/query/store.py`: owner-scoped conversation/report/job storage and distributed leases.
- `app/query/worker.py`: durable background export consumers and periodic cleanup;
  temporary storage failures retry without restarting the API.
- `app/query/history.py`: rechecks saved self-scope asset/LMS results and LMS
  locations against current backend access on history reads. Repeated selections
  are checked once per read, without model calls. Changed results hide their old
  summaries, tables and charts and ask the user to request current records.
- `app/query/application.py`: private HTTP API and bounded request admission.
- `app/auth.py`, `backend_client.py`, `token_usage.py`: retained delegation and accounting.
  The token-delivery queue uses an index and removes confirmed entries older than
  seven days; pending/crashed calls remain intact and backend usage history is retained.
  Dataset-specific LMS scopes remain separate from generic employee scopes and
  participate in conversation/download permission revalidation.
- `app/website_help.py`, `help_features.py`, `product_help.py`: reviewed help content.
- Web `app/Services/Ai/Query`: catalogue, independent validation and SQL compiler.

The old fixed-intent interpreters, demo report loop, local report formatters and
Web/App report-prose parsers are removed. Audited PHP business readers and historical
application migrations remain where they supply existing calculations or access rules.
No non-chatbot module, employee chat/Firebase workflow or global permission grant is replaced.

## Supported query operations

Employees, effort, tasks, leave, assets, asset assignments, LMS records and LMS followups use native SQL. Attendance uses the existing
calendar service and bounded in-memory relational operations to preserve leave,
holiday, shift and override rules. It is not a raw-punch approximation.
Chat presence (`is_present`) requires a punch-in, including late, short and open sessions.
Lateness (`is_late`) follows the assigned arrival cutoff without requiring punch-out.
Native attendance and late-arrival reports share the snapshot-aware, second-precise
reader: saved daily shift settings take precedence over current configuration.
Calendar labels and payroll classification remain separate; missing punches retain
leave, holiday and week-off context. Stored Present/Late filters use these same rules.

Plans support details, approved metrics, grouping, AND filters (including scalar IN),
aggregate HAVING, sorting and pagination. Effort can group by project, employee,
billing and month. Cross-dataset arbitrary joins/formulas are rejected; add reviewed
relationships/calculations to the catalogue when needed. Current headcount is not
historical headcount. Booked effort only includes recorded bookings, not employees
with no bookings. Leave durations are whole-request days for overlapping requests.

Asset inventory is a current snapshot. Own inventory follows approved active custody;
company inventory includes unassigned serialized assets. Assignment history requires
a checkout-date period and an explicitly assigned Asset Management seat. Company
inventory also requires that seat and existing company scope. Removing the seat
invalidates the conversation/report access policy. Asset finance, documents, accessory
quantities, department custody and maintenance-event history are not exposed.
The optional inventory `employee_name` field uses current approved, unreturned
custody within employee visibility. Past, pending and foreign-company assignments
cannot supply names; unassigned assets remain in company inventory. When a column
is added to an existing report, typed refinement preserves its mode and measures.

`docs/chatbot-module-coverage.json` records the source-reviewed subscription gates,
current coverage, privacy exclusions, remaining work and rollout order for all ten
subscription modules. Partial adapters do not imply full module support. All employee
reports allow company scope only for authenticated Admin/Super Admin within their
own company. Every other role is self-only, including managers and team leads.
Existing module permissions and subscriptions still apply; team scope is unavailable.

LMS requires its company subscription, existing LMS role access and an explicitly
assigned LMS seat; Super Admin retains the existing seat exemption. Company lead
reports require Admin or Super Admin. A non-admin `lms.manage-all-leads` grant cannot
expand chatbot scope. Self scope
uses the current lead assignee, including for followups; a followup's scheduled
employee can differ from that owner. Team scope is unavailable.

`lms_leads` reports current active records across lead/prospect/client/churned types.
Use creation/conversion date filters for cohorts; stage counts describe current
stages, not historical transitions. `lms_followups` uses a required scheduled due-date
period and retains completed (`done`) and cancelled records. Open work needs
`is_active=1` plus pending/snoozed status. Completion-date reports, lead transfer/stage
history, recordings, notes, contact details, custom data and financial values are
outside this initial catalogue. Existing modules and permissions are not modified.

Project matching first tries the exact name, ignoring case and outer spaces.
Partial names tolerate spacing/punctuation differences and return up to ten choices
with authorized report records in the selected period. Users confirm an option number,
full name or project ID; selection preserves the report and makes no extra AI call.
Backend choice lists stay out of model prompts, and every selected report is authorized again.

To extend a dataset, update the PHP catalogue/compiler, Python plan vocabulary and
business guidance, then add result/permission tests. Do not add a question regex or
pass an SQL expression through the API. The compiler independently validates plans.

## Report and concurrency limits

Plain attendance requests use detail rows with status and punch times. Present/Late
requires a punch-in; missing punch-out does not remove presence. Without punch-in,
leave/holiday/week-off labels remain; other current/past days are Absent. Future
dates remain unclassified. `present_days` counts actual punch-based employee-days;
`employee_days` counts selected calendar rows and is displayed as Days covered
when no attendance-status cohort or breakdown is selected. Adding columns to a
counted cohort retains that cohort; a new plain report starts with detail rows.

- Native reporting: up to 366 inclusive days, 100 rows per chat page; pagination
  continues beyond 10,000 records (offset up to 2,147,483,647).
- Attendance: up to 93 days, with no fixed company employee limit. PHP processes
  at most 50 employees / 1550 employee-days per batch and uses temporary database
  tables for complete counts, rankings and cohort thresholds before pagination.
  Multi-day detail and employee/date aggregate pages keep all selected dates for up to 100 employees together;
  aggregate and single-day pages return up to 100 rows. Database temporary-table
  permission and MySQL 8+ / MariaDB 10.2+ window functions are required.
  Report reads allow up to 60 seconds within the existing 75-second chat deadline;
  paging bounds the response, while complete-company calculations still depend
  on database capacity and indexes.
- Charts: complete selections of at most 50 rows, suitable nonnegative numeric measures.
- Exports: complete results up to 5000 rows and 12 MiB/file; narrow larger selections.
  Specialized reports page complete reader results in groups of 100; location/visit
  readers retain their backend pages of 20. Exports start from the first page and
  revalidate the complete selection on download, regardless of the displayed page.
- Above `BACKGROUND_EXPORT_ROWS`, export generation enters a durable database queue.
  Each company can have eight queued/running export jobs. Jobs recover after an expired
  worker lease; failed jobs show a useful status and can be requested again.
- `MAX_CONCURRENT_CHATS` bounds each API process; shared company/user/conversation
  leases prevent monopolization and overlapping turns. Busy requests return 429.
- Native MySQL/MariaDB reporting SELECTs have a five-second execution budget. Canonical
  adapters have explicit workload bounds. Queries never remove access filters to retry.

Downloads recheck owner, current permissions, expiry and the authorized result fingerprint.
If source data changes, regenerate the report. Report status polling makes no model call.
Private report bytes live in shared database storage, not public folders. Object storage
can replace blob storage behind this interface if measured file volume requires it.

## Deployment and growth

Keep one API worker for local SQLite. For multiple API hosts/processes, configure a
separate PostgreSQL AI metadata database with TLS using `CONVERSATION_DATABASE_URL`.
Initialize its schema once before starting multiple instances:

```powershell
.\.venv\Scripts\python.exe -c "from app.config import Settings; from app.query.store import Store; s=Settings(); Store(s.database_file, s.conversation_database_url.get_secret_value()).close()"
```

Each host runs the authenticated PHP gateway and a loopback Python service, or an
appropriately secured private gateway arrangement. Do not expose Python by relaxing
its loopback check. Reuse the existing application authentication and secrets.

API instances may run export consumers, or set `RUN_EXPORT_WORKERS=false` there and
run `python -m app.query.worker` against the same metadata database. Keep each host's
usage-outbox SQLite file on persistent local storage; existing idempotent settlements
retry until delivered. Do not put SQLite files on a network filesystem. Provide a
process-specific `APPLICATION_LOG_PATH` or aggregate logs externally to avoid shared
file rotation conflicts.

Retention prunes expired reports/jobs and conversations older than the configured
period. Back up the metadata database and persistent usage outbox, protect storage
with infrastructure encryption, and test restores. This branch does not migrate or
delete previous chatbot history; new tables have the `nlq_` prefix.

Capacity is not an unlimited-user guarantee. Measure expected peak concurrency,
provider limits, SQL latency and export workload in staging before production rollout.
Read replicas, result caches and object storage should follow measured requirements;
no permission-sensitive result cache is enabled by default.

## Validation

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use isolated databases, mock delegation and mocked model responses. Live-model
paraphrase checks, actual MySQL/MariaDB query plans, PostgreSQL multi-host contention,
real company reconciliation and device testing are separate staging checks. Health
reports `production_ready=false` until deployment-specific acceptance is completed.
For opt-in live interpretation checks using fictional selections and the code-owned
catalogue only, run `python tests/live_nl_query_check.py` from this project directory.
This uses the configured provider/model and incurs provider charges; diagnostic
token totals are printed separately. It does not use chat sessions, query business
records, create exports, or post usage against an employee/company account.
On 2026-11-12, 186 local tests and eight synthetic live interpretation cases passed,
including employee-wise counts, sorting, effort grouping and sensitive-data denial.
These checks do not replace validation of actual company report results.
Official strict function-calling reference:
https://developers.openai.com/api/docs/guides/function-calling

The older cost/setup documents describe the retired prototype. This README and the
current configuration template describe the replacement implementation.


## Local verification on 2026-11-12

- Python service: 173 tests passed, including token accounting, tenant boundaries,
  durable-job recovery and a 32-request mocked concurrency check.
- Web AI/permission/tenant checks: 168 tests passed (2362 assertions).
- Flutter chat/report checks: 45 tests passed; affected production files passed analysis.
- Chrome: full and embedded chat passed at 1440, 390 and 320 pixel widths, including
  structured tables, charts, safe text rendering, history and queued downloads.
- A broader Web test selection reported 9 errors and 15 failures in other modules.
  Those modules were not modified; the broader suite is not fully green.

No live provider calls, production data reconciliation, PostgreSQL integration or
production-scale capacity validation were performed. Configure and validate those
in staging before rollout. Conversation history returns the latest 200 messages;
full retained history remains in private storage until its retention deadline.
