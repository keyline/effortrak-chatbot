# Effortrak AI REST API - local prototype

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
Leave balances, payroll, expenses, new exports and write actions are not part
of this phase. Leave history is not yet a chatbot tool. Daily tasks and monthly task summaries
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
or global permission grants are needed. Description analysis by the model, task exports, booked effort,
project/client details, task changes and Flutter integration are not supported by these task tools.

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

### AI Attendance Export Options

Project task queries are also handled locally. Try `Show project IIHM task details from
01/10/2026 to 07/10/2026`, `Show project IIHM tasks for last 7 days`, or
`Show project IIHM tasks for 2026-10`. Projects must resolve uniquely inside the company
and have tasks visible under the user's employee/department permissions. Date ranges are
inclusive and limited to 93 days. Missing dates and page changes retain the selected project.
An optional explicit employee ID further restricts the project result. Project names and task
descriptions are not sent to OpenAI. Project task PDF/XLSX export is not implemented.

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
