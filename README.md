# Effortrak AI REST API - local prototype

Server administrators: use the [Server Operations Guide](docs/server-operations-guide.md)
for the short cPanel start/restart procedure, settings and troubleshooting.

Manager-report questions, deployment permissions and current limitations are listed in
[the manager question catalogue](docs/manager-question-catalogue.md).

FastAPI service for API testing before adding web and Flutter chat interfaces.
It supports fictional sample data and a permission-checked CodeIgniter gateway
for real attendance and PDF/XLSX exports. No mobile or web UI changes are required.

**Default `DATA_MODE=sample` uses fictional accounts and attendance.** For real
data, follow [REAL_DATA_SETUP.md](REAL_DATA_SETUP.md). Python never connects
directly to the application database or accepts an Effortrak login token;
CodeIgniter validates the login and delegates a short-lived credential.
Demo authentication is for local testing only, even with OpenAI. Requests from
non-loopback clients are rejected; do not publish this service through a proxy.
The root `.htaccess` denies Apache static access when overrides are enabled.
Keep this folder excluded from any public Apache/Nginx document root when deploying.

## Run on Windows

From `D:\xampp\htdocs\effortrak\effortrak_ai_service`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --no-proxy-headers
```

Swagger: http://127.0.0.1:8010/docs

The default `CHAT_MODE=demo` works without an OpenAI key. It is a deterministic
offline test responder, not an LLM: use the example wording below and YYYY-MM
months. It can reuse the last month from conversation history. Fictional data
exists for September 1-3, 2026 only, not a complete month.

## Authentication

In Swagger, click **Authorize** and enter one token (without the `Bearer` prefix).
In Postman, use **Authorization -> Bearer Token**.

| Token | Company | User | Attendance access | Exports |
| --- | --- | --- | --- | --- |
| `demo-company-a-employee` | 1 | 101 | Own | Yes |
| `demo-company-a-manager` | 1 | 100 | Own and direct reports 101/102 | Yes |
| `demo-company-a-admin` | 1 | 103 | Own, direct reports, company 1 | Yes |
| `demo-company-b-employee` | 2 | 201 | Own | Yes |
| `demo-company-a-no-export` | 1 | 101 | Own | No |

Company and user scope are resolved from the token, never the request body or
prompt. These public test tokens never grant access to real company data.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Mode, readiness, and data-source label |
| GET | `/v1/me` | Authenticated demo identity and permissions |
| POST | `/v1/chat` | Chat or continue an existing conversation |
| GET | `/v1/conversations/{id}` | Owner-only message history |
| DELETE | `/v1/conversations/{id}` | Delete history and generated files |
| GET | `/v1/reports/{id}/download` | Owner-only authorized report download |

Chat request:

```json
{"message": "Show my attendance for 2026-09"}
```

Example response (IDs vary):

```json
{
  "success": true,
  "data": {
    "conversation_id": "<uuid>",
    "reply": "Offline demo attendance for 2026-09, scope self: Present: 1, Late: 1, Absent: 1. Fictional records for September 1-3, 2026 only; not a full-month report.",
    "mode": "demo",
    "data_source": "fictional_sample",
    "tools_used": ["get_attendance"],
    "artifacts": []
  }
}
```

Pass the returned `conversation_id` to continue:

```json
{"message": "Export my attendance as xlsx", "conversation_id": "<returned uuid>"}
```

The returned `artifacts` contain a relative `download_url`. GET that path on
`http://127.0.0.1:8010` with the same bearer token. PDF exports work using
`"Export my attendance for 2026-09 as pdf"`. Do not place bearer tokens in URLs.

PowerShell test:

```powershell
$headers = @{ Authorization = 'Bearer demo-company-a-employee' }
$body = @{ message = 'Show my attendance for 2026-09' } | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:8010/v1/chat' -Method Post -Headers $headers -ContentType 'application/json' -Body $body
```

## Permission tests

- Employee token + `"Show team attendance for 2026-09"`: 403.
- Manager token + `"Show team attendance for 2026-09"`: fictional direct reports.
- Company A admin + `"Show company attendance employee 201 for 2026-09"`: 403.
- Company B token + a Company A conversation or report ID: 404.
- No-export token + `"Export attendance 2026-09 as pdf"`: 403.
- Missing/invalid token: 401. Additional `company_id`/`user_id` body fields: 422.

Every tool checks scope independently, including model-generated tool requests.
History and report downloads check user/company ownership. A changed permission
fingerprint returns 409 and requires a new conversation before old history can
be replayed. Reports are private SQLite blobs, not publicly accessible files.

## Enable OpenAI responses

### Broad Question Interpretation

Set `SEMANTIC_QUESTIONS=true` with `CHAT_MODE=openai` and `DATA_MODE=backend`
only after approving transfer of full chat questions to OpenAI. The default is
false so existing deployments retain their current behavior.

The existing `/v1/chat` endpoint uses one strict function-call interpretation to
extract a supported action, subject, period and filters. It does not add routes
or require a keyword rule for every question variation. Existing backend tools
perform name resolution, authorization and data retrieval. Final report replies
are formatted locally; retrieved task descriptions, names and precise locations
are not sent to the interpreter. Typed names and other text in the current
question are sent. Context sent to the model contains selection metadata only.

The interpreter supports the existing attendance, manager, booked-effort,
employee-task, project-task and attendance-export capabilities. It asks for
missing/ambiguous selections and explains unsupported operations. Provider
failure or invalid interpretation does not silently fall back to a different
report. Disable the flag to restore the previous question handling.

This is intent interpretation, not model fine-tuning or a guarantee that every
wording is understood. `tests/test_semantic_query.py` covers execution contracts,
privacy, context and authorization using mocked interpretations; real-model
paraphrase checks should also be performed when changing model or instructions.

Named-project booked effort is supported: "Total booked hours for IIHM last
month" resolves an authorized project using timesheets and filters bookings by
their stored project ID and booking date. Results include total booked time and
billable/non-billable breakdowns. Effort Report permission is required. This is
not planned task time, and assignment links do not duplicate booking totals.

Configure an untracked `.env` in this folder using `.env.example` as the template:

```dotenv
CHAT_MODE=openai
OPENAI_API_KEY=<your server-side OpenAI API key>
OPENAI_MODEL=<a model in your account that supports Responses function calling>
DATABASE_PATH=data/chat.sqlite3
```

Restart the service. The same REST endpoints now use the official OpenAI Python
SDK, Responses API, and validated function tools. No LangChain/LangGraph is used.
In `DATA_MODE=sample`, attendance and identities remain fictional. Model access/costs depend on your
OpenAI account. Never send your OpenAI key in chat requests.

The loop allows up to five model rounds and eight tool calls, with a 60-second
provider deadline. Reports are committed only after a complete successful turn.
Missing configuration returns 503; provider errors/timeouts return a generic 502.
Conversations persist locally in `data/chat.sqlite3`; only the last 20 messages
are sent to the model. This prototype serializes chat turns in a single process;
run one worker. There is no streaming, production rate limiting, or retention
scheduler yet.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover authentication, tenant and owner isolation, team scope, exports,
permission changes, deletion, and the SDK tool loop using a stub provider.
Live model verification requires your key and configured model.

## Real Effortrak Data

### Shared AI policy (first implementation phase)

Real backend scope is now resolved from active company-owned roles and team
assignments, not `manager_1_id` alone. Database `ADMIN` and `SUPER ADMIN` roles
have company employee scope. Other employees have self scope; `Teamlead` and
`Sublead` assignments additionally allow active `Member` assignments in their
led departments. Managers, admins and other leads are excluded from lead scope.
Inactive departments and foreign-company assignments never grant access.

Employee visibility is implemented separately in `Services/Ai/AiEmployeeService`.
Module licensing and read permissions are enforced by `AiAccessService` for each
data request. Personal attendance is a licensed baseline; other attendance,
leave and task access still needs the applicable read grant. Task visibility
mode and tracker-department restrictions are also retained. No normal portal controller or global role grant changes
are required. Changes to membership, managers, task visibility, licensing or
permissions invalidate conversation policy fingerprints and old downloads.

New local-only, delegated internal POST endpoints:

- `/apiv2/ai/internal/leave-history`: stored requests overlapping a month.
- `/apiv2/ai/internal/task-summary`: monthly Task Assign status counts and planned minutes.
- `/apiv2/ai/internal/daily-tasks`: one date's Task Assign records, paginated at 50 tasks per page.

Monthly endpoints accept `scope`, nullable `employee_id`, and `month` (`YYYY-MM`). Daily tasks accept
`scope`, nullable `employee_id`, `date` (`YYYY-MM-DD`), and `page` (1-10000). They derive
company/user identity from the signed credential, not the body. They use the
same self/team/company employee policy, reject unauthorized scopes, and are
read-only. Leave reasons and client details are omitted. Daily task descriptions and status/employee
names are returned to the local renderer, not to OpenAI.
Payroll, expenses and write actions are not part of these tools. Leave balances now have a
dedicated read tool; leave history is not yet a chatbot tool. Daily tasks and monthly task summaries
are now web-chat tools when the task module is authorized. Named attendance requests are resolved locally through
the protected employee-search endpoint. Matched names/IDs are displayed locally, not sent to OpenAI as lookup
results or automatically included in later model context. Ambiguous or truncated searches require clarification.

Sample mode deliberately retains its fictional manager relationships; it must
not be used to validate the real team-assignment policy. Restart Python after
updating context schemas and start a new chat after the policy changes.

### Task chatbot and local-description privacy

Try `Show my tasks for 2026-10-01` or `Show company task summary for 2026-10`.
After a successful single-employee daily attendance query, `Show tasks for this employee for that day`
uses stored query context (not an ID guessed from assistant prose). Multiple employees require clarification.
For daily task pagination ask `Next page` or `Tasks page 2`. Permissions are rechecked on every request.

OpenAI receives only explicitly selected task metadata: authorized employee IDs, date/month/scope,
status IDs, task counts, planned minutes and pagination. Descriptions, employee names and status labels
are rendered locally and excluded from model tool results. Chat storage keeps a separate redacted
`model_content` and trusted `query_context` for follow-ups; API history still displays the original local
reply. The SQLite service adds these columns idempotently on startup. No application-database migration
or global permission grants are needed. Description analysis by the model and task changes
are not supported. Task exports are now available through the shared report exporter below.

### Named attendance requests

Try `Can you check Joydeep Ghosh attendance history for 01/10/2026` or
`Show attendance for Joydeep on 2026-10-01`, or
`Can you tell me Subhomoy Samanta task details for 01/10/2026`. Scope comes from authenticated permissions:
company for Admin/Super Admin, team for eligible leads, otherwise self. Search checks the
requested module's read permission as well as employee visibility (including existing task visibility).
A unique verified match is used as an explicit employee filter before the report is fetched; it never falls back to
listing the whole company. Shortened name tokens must match name prefixes, not an arbitrary
nearest spelling. The reply identifies the matched name/ID so the user can confirm it.
No match does not establish whether an employee exists in another scope or company.
Task descriptions, lookup results and locally handled name queries stay out of model-visible history.
Daily task details include booked time from linked timesheet entries when available, separate
from planned time. Missing bookings are not treated as recorded zeroes; daily totals cover
available linked entries across all pages. Links are checked against both company and employee.

### Conversation Context

The server stores the active report selection and its missing fields in private
`query_context` metadata. Names, dates, employee/project IDs, scope, pagination and
export format are retained across clarification replies. Recognized short replies
are handled locally; pronoun-based follow-ups retain the selected subject even if
the model omits its follow-up flag. Explicit scope changes clear the old employee
filter. Every resulting read/export still checks current backend permissions.

Presentation follow-ups such as `Tell me their names?`, `Who were they?`,
`Show their details` and `Just the count` reuse a complete active report selection
locally. They change the answer style, not the date, employee scope, page or report
filters, and do not replay an old export or chart. Missing/pending selections and
new or sensitive questions are not treated as presentation changes. Deploy
`app/semantic_query.py` and restart the worker for this fix; no client or database
change is required. Regression coverage is in `tests/test_report_style_memory.py`.

`Thank you` preserves the active report and its export snapshot. A new independent
question starts a new selection; missing context does not restore an older person
silently. This is active-request memory, not unrestricted recall of every person
mentioned earlier. Ambiguous requests still require clarification.

Run `python -m pytest tests/test_conversation_memory.py` for chained employee/date,
format, pagination, scope, project, pronoun and cross-module regression scenarios.
When deploying this change, update `app/semantic_query.py`, `app/task_report.py`,
`app/chat_service.py`, `app/report_export.py`, `app/visit_query.py` and `app/leave_balance_query.py` together,
then restart the AI worker. No manual database migration or client rebuild is needed.
Start a new chat for conversations whose earlier selections were already lost.

### Question Interpretation And Clarification

`app/question_policy.py` defines the interpreter's capability boundaries and reviewed
clarification messages. It distinguishes attendance hours, actual timesheet bookings,
planned task time, tracked productive/idle time, remaining leave and approved leave.
The model can return a constrained `clarification_kind` instead of guessing. Any non-null
ambiguity signal is handled before employee lookup or report execution, even if the
model also selected a read action. Clear metric choices preserve the supplied employee
and period, then use the same existing backend authorization and report implementations.

Empty/non-report contexts no longer create an implicit attendance selection. Pending
employee/visit-name handlers avoid common report/question phrases; vague work-arrangement
questions are not automatically interpreted as GPS lookups. Existing local help, visits
and exports remain separate handlers; this is an incremental change, not a full router rewrite.

The `app.semantic_query` logger records route source, action, clarification category and
follow-up flag at INFO level when application logging enables it. It does not log question
text, names, report data or credentials. No new database storage is introduced.

Run `python -m pytest tests/test_question_policy.py` for mocked semantic-contract and
permission regressions. These tests exercise routing, not a measured accuracy benchmark
against a live model. Capability instructions improve guidance but cannot guarantee every
paraphrase; ambiguous requests should clarify. The added instructions modestly increase
input tokens for existing semantic calls; no second AI call or new model is introduced.

Deploy `app/question_policy.py`, `app/semantic_query.py`, `app/visit_query.py` and `app/leave_balance_query.py` together
and restart the AI worker. Include any not-yet-deployed conversation/product-help files
listed in their sections as well. No API response-format change, database migration,
web update or mobile rebuild is required.

### Product Introduction

Questions such as `What is Effortrak?` and `Tell me about Effortrak` use the reviewed
introduction in `app/product_help.py`, without a model call or report lookup.
When semantic questions are enabled, the existing AI interpreter can also classify
less direct wording as `product_overview`; the answer still comes from the reviewed
text, not generated feature or subscription claims. Normal interpreter token charges apply.
Product answers clear the active report selection and are not exportable reports.
Deploy `app/product_help.py`, `app/chat_service.py` and `app/semantic_query.py` together
and restart the AI worker. Both web and mobile use this handler; no database migration
or client rebuild is required. Run `python -m pytest tests/test_product_help.py` to verify.

### Punch-In Time Filters

With semantic questions enabled, punch-in reports support a fixed clock cutoff, shift
start, or the configured late cutoff. Examples:

- `List employees who punched in after 11 AM last month.`
- `Show employees who punched in after shift start on 15/10/2026.`
- `Show employees more than 20 minutes after their shift start last month.`
- `Export employees late on 20 September 2026 in Excel with a pie chart.`
- `Export this month's after-shift punch-ins as PDF with pie and bar charts.`

`punch_basis` is `fixed_time`, `shift_start`, or `late_cutoff`; `punch_after` is an
HH:MM:SS clock time for fixed-time requests; `min_delay_minutes` is a strict delay
relative to shift start/late cutoff. The existing `threshold` remains an occurrence
count, never minutes. Comparisons include seconds: exactly 11:00:00 does not match
after 11 AM, and exactly 20 minutes does not match more than 20 minutes. Overnight
shifts are anchored across midnight; fixed clock cutoffs remain clock comparisons.
Only the first active recorded punch per employee/day is assessed. Calendar and
payroll statuses are not modified. Existing 93-day and authorized-employee limits apply.

The backend prefers per-day shift settings in `attendance_transaction` when captured
context exists. Reconstructed snapshots are labelled `reconstructed_snapshot`;
missing snapshots use `current_configuration` and are explicitly estimates, not
verified historical assignments. Valid saved snapshots are labelled `saved_snapshot`.
Days without valid comparison times are unassessed, not silently counted as on time.
No new table/column is needed; deployments without the existing snapshot table use
the labelled current-setting fallback. Fixed-time comparisons do not depend on shift policy.

Filtered reports and PDF/XLSX exports use the same backend arguments. Requested pie
and bar charts summarize matching employee-days per employee; they do not claim to
show all company employees or the proportion of late versus on-time employees.
Both chart types can be included in one file. Unsuitable/empty pie data is omitted
with an explanation while preserving the tables. Export and download reauthorize
the original selection, and changed data/access requires displaying the report again.

In-chat punch-in reports also support explicitly requested pie/bar charts (including
both together). Ordinary requests and period-only follow-ups remain report-only.
Style follow-ups retain the report's employee scope, period, occurrence threshold
and punch-time filters. Chart values come from the same authorized manager report;
no employee values are sent to the question interpreter. In-chat charts require a
complete selection and up to 93 days. Empty results have no
chart; unknown counts are never converted to zero. Existing web/mobile chart renderers
are reused. Deploy AI-service `app/semantic_query.py` and `app/report_charts.py`
together and restart the worker; no client or database update is required for this addition.

Company/team punch-in reports now read authorized employees in keyset batches of 50,
with bulk attendance reads per batch. There is no 50-employee rejection on this report
path. The full result is retained for PDF/XLSX and export/download reauthorization;
chat displays 50 matching employee rows per page. Ask `next page` or `page 2` to
navigate while retaining scope, period and time filters. Page notices show overall
counts separately from this-page counts. Aggregate questions count the full result.
Charts with more than 50 categories show the top 19 employees plus one explicitly
labelled Other employees group, preserving totals. They never silently drop employees.
Existing 93-day, response/snapshot/file-size and export-cell safety limits remain;
oversized exports must be narrowed rather than returned as incomplete files.
The shared large-company implementation now covers every existing report family:
attendance, booked effort, task summaries/insights, project tasks, work summaries,
approved leave/history, leave rankings, locations and visits. Employee selection and
download authorization no longer reject companies above 50 employees; work reports
no longer reject selections above 10 employees. Tenant, role, department, module and
task-visibility filters remain mandatory. This applies to existing and future companies.

Chat detail is paginated (normally 50 rows; visits retain 20). Complete report totals
remain separate from page totals. `next page`/`page N` retain the employee scope and
period; changing the scope or period resets the page. Leave rankings intentionally
remain top-N per leave type, but scan all eligible balances rather than stopping at 5,000.

PDF/XLSX exports collect every matching page, unless the user explicitly requested a
latest-N batch. Saved chat pages expand only when exporting, and permissions/data are
checked again during generation and download. Truncated or changing data never becomes
a successful partial export. Safety bounds remain: 2 MiB snapshot, 20,000 rows,
500 fetched pages, 100,000 cells, and 8 MiB file. Exceeding a bound requires a narrower
export selection, not a smaller company. Work periods remain at most 31 days.

Deploy all modified AI Python modules together, including `app/report_paging.py`, and
restart the AI worker. Deploy the modified web `app/Services/Ai` services together.
Refresh web JS/CSS for page labels. Updated mobile models/screen provide styled page
notices; older apps still receive the same text API shape. No database migration is needed.

Deploy the large-company change backend first: `app/Services/Ai/AiEmployeeService.php`,
`AiManagerReportService.php`, and `AiPunchTimeReport.php` in that same directory.
Then deploy AI-service `app/backend_client.py`, `app/manager_query.py`,
`app/semantic_query.py`, `app/report_charts.py`, `app/report_export.py`, and
`app/punch_pdf.py`, and restart the worker. Deploy web `public/assets/js/ai-assistant.js`
and refresh cached assets. Mobile page notices use `lib/models/ai_late_punch_report.dart`
and `lib/pages/mainpages/ai_assistant/ai_late_punch_report_view.dart`; release a new app
build for that presentation update. Existing clients still receive the page notice as
report text. No database migration or new permissions are required.

Punch-in Excel exports open on an employee-summary `Report` sheet. `Punch-in Records`
contains every returned date-wise record in one filterable Excel table, with native
dates, clock times and sortable duration values. `Notes` preserves filters, source-quality
and coverage explanations; requested chart sheets remain available. Original source
tables and reply text are retained in hidden technical sheets, not removed or used as
an access-control mechanism. The default view hides redundant reference times,
employee IDs and source metadata; estimated/unspecified sources and unassessed-day
counts stay visible when relevant. Delays use hours:minutes:seconds.
Punch-in PDFs contain a short summary, one date-wise table with repeated page
headers, requested charts side by side, and report notes. They do not repeat the
chat answer or technical source tables. Other report types keep their existing layout.
For this PDF/Excel presentation update, deploy `app/punch_workbook.py`,
`app/punch_pdf.py` and `app/report_export.py` together and restart the AI worker. Generate a new export;
previously downloaded or stored report files are not rewritten. No client or database
change is needed. Run `python -m pytest tests/test_punch_workbook.py tests/test_report_export.py tests/test_punch_time.py`.

Deploy the backend first: `app/Controllers/Api/AiController.php`,
`app/Services/Ai/AiManagerReportService.php`, and the new
`app/Services/Ai/AiPunchTimeReport.php`. Then deploy AI-service `app/schemas.py`,
`app/semantic_query.py`, `app/backend_client.py`, `app/manager_query.py`, and
`app/report_export.py` together and restart the worker. Include any not-yet-deployed
dependencies from earlier sections. No mobile rebuild, web UI update or new database
migration is needed. New filters fail closed if the backend cannot echo the exact filter.

### Shared Report Exports

Display a report, then ask `Give me this report in Excel format` or `Make that a PDF`.
This works with attendance, tasks/project tasks, manager reports, booked effort,
work summaries/reviews, leave-balance rankings, employee counts and visits/locations.
Only implemented report tools are exportable; help instructions are not reports.

- PDF contains rendered charts and tables. Excel (XLSX) contains editable native charts and data sheets.
- Existing on-screen charts are retained. Other reports get meaningful summary charts when possible;
  locations remain data tables, not numeric-coordinate charts. Up to 10 charts are included.
- The file covers the displayed result and returned data, not undisplayed pages. Existing scope,
  pagination, missing-data and truncation notes still apply. Oversized results require a narrower selection.
- Export follow-ups do not call OpenAI or send report records to the model.
- Existing export permission is required. Original read permissions and filters are checked again
  when generating AND downloading the file. Changed data/access requires a fresh report.
- Saved report snapshots are private server-side metadata, never API history fields or model context.
- Answers created before this update must be regenerated before using shared exports.

Deployment: back up the AI SQLite state, deploy the Python changes and web
`AiExportChart.php`, `AiAttendanceService.php`, `AiAttendanceExportService.php` together,
then restart the AI worker. SQLite adds nullable `export_snapshot` columns automatically.
No MySQL migration, new dependency or mobile UI rebuild is required for these attachments.
Existing direct attendance exports remain supported and now include status charts.

### AI Attendance Export Options

Project task queries are also handled locally. Try `Show project IIHM task details from
01/10/2026 to 07/10/2026`, `Show project IIHM tasks for last 7 days`, or
`Show project IIHM tasks for 2026-10`. Projects must resolve uniquely inside the company
and have tasks visible under the user's employee/department permissions. Date ranges are
inclusive and limited to 93 days. Missing dates and page changes retain the selected project.
An optional explicit employee ID further restricts the project result. Project names and task
descriptions are not sent to OpenAI. Project task PDF/XLSX exports use the shared report exporter below.

Latest-task queries support `Show me last 5 tasks of ARC Promotion` or, after selecting
a project, `Show last 5 tasks`. Without explicit dates they search all authorized project
history, newest task date first, with descending task ID as the tie-breaker. Add a month
or date range to restrict the search. Request `next 5 tasks` for the next batch; keep the
same batch size (1 to 50). Summary totals cover all matching tasks, not only the batch.

Project wording also supports `last five task history of NAME Project`, `five most
recent tasks for NAME`, `recent tasks related to NAME project`, and `Project: NAME
latest tasks`. Recent tasks default to five when no count is stated. If the project
name is missing or ambiguous, the assistant retains the count, period and employee
filter while asking for the name. A name-only clarification is looked up locally.
After a task result, `show me more`, `next batch`, and `continue` retain the project
and batch size. Unknown or ambiguous requests must not produce unverified no-data claims.

Exports are handled locally without sending report contents to OpenAI. Existing attendance
screens, mobile code and legacy AI monthly export calls are unchanged.

- `Export my attendance today as pdf`
- `Export company attendance last 7 days as pdf`
- `Export team attendance 2026-09 as xlsx`
- `Export Joydeep Ghosh attendance from 01/09/2026 to 12/09/2026 as pdf`

Dates use DD/MM/YYYY or YYYY-MM-DD. Custom ranges include both endpoints, may cross months,
and are limited to 93 days. Wide ranges are split into seven-day sections with whole-period
totals. Today and Last 7 Days use the server's Asia/Kolkata date; Last 7 Days includes today.
Named employees must resolve uniquely within authenticated visibility. Permissions are checked
at generation and download. Today report contact details and coordinates remain local.
The protected AI export endpoint accepts `report_type` (`today`, `last_7_days`,
`monthly_details`, `custom_range`), `scope`, optional `employee_id`, `format`, and either
`month` for monthly details or `start_date`/`end_date` for a custom range. Attendance status
calculations reuse My Attendance calendar rules; blank statuses are not absences.

The CodeIgniter gateway, delegated authentication, real attendance tools and
PDF/XLSX exports are implemented. Follow [REAL_DATA_SETUP.md](REAL_DATA_SETUP.md)
to configure both services, deploy the permission migration and test in Postman.
They remain disabled until explicitly configured. `CHAT_MODE` selects the
responder; `DATA_MODE` independently selects sample versus real backend data.
Add production rate limiting, retention, auditing and deployment hardening
before publishing. Do not switch to production simply by changing either mode.

## Client Visits and Recorded Locations

The shared web/mobile chatbot supports these read-only questions and common alternative wordings:

- "Which client did Joydeep Ghosh last visit?"
- "Show Joydeep Ghosh's visits today."
- "Who visited ABC Client in the last 7 days?"
- "Show Joydeep Ghosh's visits to ABC Client from 01/10/2026 to 15/10/2026."
- "Where is Joydeep Ghosh now?"
- "Show Joydeep Ghosh's recorded location on 06/10/2026."
- "Show LMS lead ABC location."

Visits use Field Sales check-in records, newest first. Check-out times are not stored and
are not invented. Use an inclusive period of up to 93 days; "last 7 days" includes today.
An undated latest-visit question checks recorded history. Undated visit lists ask for a
period. Lists have 20 rows per page; "next page" retains the employee/client/period.
Employee IDs can be supplied during name clarification. Ambiguous or unavailable names
require clarification; they never select an arbitrary match. Scope selection is bounded
by existing active-employee visibility, without a company-size selection cap.

Employee location means the latest stored adaptive GPS point, optionally within a date
range, not an on-demand live device reading. The response includes capture time, source,
available accuracy, and mock-GPS warnings. Historical GPS may also come from the archive.
LMS locations are saved lead coordinates/addresses: scheduled follow-ups are not proof
of a visit. LMS lead visibility is restricted by assignment and manage-all permissions.

The backend rechecks company AI enablement, module subscription, role permissions, and
employee visibility for every request. Visits require Field Sales plus CLIENT VISIT LOGS;
adaptive GPS requires adaptive_tracking plus LOCATION HUB. LMS requires its subscription
and LMS permission, with individual access/assignment restrictions. Attendance locations
retain their separate AI attendance-location permission.

Stored addresses are preferred. Missing addresses can use the existing Google geocoding
helper and GOOGLE_MAPS_API_KEY, with at most two uncached lookups per request, a one-day
company-scoped cache, and a five-minute unsuccessful-lookup cache. These lookups can incur
Maps charges. If a lookup is unavailable or the budget is exhausted, coordinates and an
Open in Maps link remain available. Addresses are approximate, not proof of presence.
Capture tables and GPS workflows are not changed by this feature.

Deploy the updated PHP backend and AI service together, then restart the Python service.
The new protected endpoint is POST apiv2/ai/internal/visit-location. No new SQL migration
is needed on an updated database containing the existing visit/GPS/LMS tables. Deploy web
assets for clickable map links; rebuild the Flutter app for native map-link buttons.
Users may need a new chat because the available-module policy fingerprint has changed.
Recognized visit/location questions bypass the LLM, including when semantic questions are
enabled; visit/address results and these questions are redacted from model-facing history.

### Leave Balance Rankings

Examples: "Show me employees with the highest leave balance this year", "Who has the
most leave remaining?", "Top 5 employees with highest Annual Leave balance for 2026-27",
and "Rank my team by unused leave balance". A follow-up such as "Top 5 instead" keeps
the selected year and leave type. Common wording runs locally without an LLM call;
other paraphrases use the existing semantic interpreter when enabled.

Uses the existing Leave Balance page's stored allocated-minus-used figures, including
manual adjustments. "This year" means the company's active financial year, shown in
the answer. Explicit years select the FY start year (2026 or 2026-27). Each leave type
is ranked separately, highest first, with up to 50 employees per type (default 10).
Highest-balance rankings show only positive remaining balances by default. Explicit
requests can select all, zero or negative remaining balances; "Only zero" or "Show all"
follow-ups retain the selected year/type. Empty zero-only categories are omitted from
positive rankings. Negative stored Used values are preserved, never converted to positive.
Lowest/least/fewest remaining leave requests sort smallest first and include zero but
exclude negative balances by default. Explicit negative/all filters remain available.
Examples: "Show 5 employees with the lowest Paid Leave balance" and "Who has the least
leave remaining?" Direction-only follow-ups such as "lowest instead" keep the year/type.
Both directions default to 10 employees per leave type and allow at most 50 per type;
the web/app View more control expands only those returned rows, not an unlimited list.
Unlimited entitlements are counted separately, not ranked as negative balances. Missing
records are not zero. This is not a historical as-of snapshot or projected entitlement.

Requires the Attendance subscription, Leave Balance function permission, and existing
company/team employee visibility. Read-only: no changes to leave requests or balances.
Deploy both PHP and Python changes and restart the AI service. The protected endpoint
is POST apiv2/ai/internal/leave-balance. No new SQL migration or Flutter rebuild is
required on the updated application schema; both web and mobile use the shared chat API.
