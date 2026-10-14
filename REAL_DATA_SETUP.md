# Real Attendance API Setup

The backend integration is implemented but disabled by default. Existing `.env`
files and application data have not been changed. Web and Flutter UI integration
is a later step. Use the CodeIgniter gateway in Postman, not Python `/v1/chat`.

## Request Flow

Postman -> CodeIgniter `/apiv2/ai/chat` -> Python -> authenticated CodeIgniter
attendance/export tools -> Python -> Postman.

CodeIgniter verifies the existing app API key, signed login token, active device,
active user/company/role, attendance license and role grants. Company and user
IDs come from verified authentication, never the message or request body.
Delegation lasts at most 120 seconds and is tied to the current device token.
Every callback rechecks session status and current permissions. The model receives
only authorized attendance fields, not access tokens, service secrets, database
credentials, GPS coordinates or payroll data. Python stores owner-scoped messages
and generated files in its own SQLite database.

## 1. Prepare Backend Permissions

Back up the database and review pending migrations before deploying. The added
migration is `2026-10-07-000001_AddAiAssistantPermissions.php`. It adds catalogue
capabilities only; it does not grant them to existing roles. Standard deployment
uses these commands from `effortrak_web`:

```powershell
D:\xampp\php\php.exe spark migrate:status
D:\xampp\php\php.exe spark migrate -n App
```

The second command applies all pending App migrations, not just the AI migration.
Use your normal reviewed deployment procedure if other migrations are pending.

In the existing role editor, assign the needed **AI ASSISTANT** capabilities:

| Capability | Permission key | Meaning |
| --- | --- | --- |
| AI CHAT | `ai.chat` | Use the chatbot; required for every request |
| AI TEAM ATTENDANCE | `ai.attendance.team` | Read active direct reports via `manager_1_id` |
| AI ATTENDANCE EXPORT | `ai.attendance.export` | Export attendance already within the user's read scope |

Existing **MY ATTENDANCE** `my-attendance` permits self reads. Existing
**ATTENDANCE** module access permits company-wide reads, matching the web
attendance page. Designation is descriptive, not an access grant. Organisation
SUPER ADMIN retains the existing RBAC bypass, but is still restricted to their
company and an active attendance license. Team access does not include indirect
reports. New AI grants should be assigned deliberately, not to all employees.

## 2. Configure Both Services

Generate one private random secret (at least 32 characters) and place the same
value in both services. Never put it in Postman, Flutter, browser code or Git.

Add to `effortrak_web/.env`:

```dotenv
ai.enabled = true
ai.serviceUrl = 'http://127.0.0.1:8010'
ai.serviceSecret = '<same-private-random-secret>'
```

Configure `effortrak_ai_service/.env`:

```dotenv
DATA_MODE=backend
BACKEND_URL=http://127.0.0.1/effortrak/effortrak_web
AI_SERVICE_SECRET=<same-private-random-secret>
CHAT_MODE=demo
DATABASE_PATH=data/backend-chat.sqlite3
```

`BACKEND_URL` is the CodeIgniter application base URL, without `/apiv2`.
Adjust it to the actual Apache virtual host or public application path. Ensure
CodeIgniter `app.baseURL` is correct so exported download links are usable.
Use a separate database path from sample mode to keep test histories separate.
If grants change, existing conversations return 409; start a new conversation.

First use `CHAT_MODE=demo` to test real reads and exports without an OpenAI key.
This is a deterministic test responder, not a language model: use `YYYY-MM`
months and the example wording below. Responses identify `effortrak_backend`.
Then enable the existing OpenAI SDK integration:

```dotenv
CHAT_MODE=openai
OPENAI_API_KEY=<your-private-openai-api-key>
OPENAI_MODEL=<responses-compatible-model-available-to-your-project>
```

Authorized attendance is sent to OpenAI in this mode. The API uses `store=False`,
which does not itself guarantee zero retention by the provider. Obtain your
organisation's approval for sending employee data before enabling it.

The OpenAI HTTP client uses `truststore` to verify TLS against the operating
system's trusted CA certificates (Windows certificate store on XAMPP). Certificate
and hostname verification remain enabled. If HTTPS inspection is used, its CA
must be approved and installed in the OS trust store by your administrator;
never work around certificate errors with `verify=False`.

Install dependencies and restart Python:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --no-proxy-headers
```

Run CodeIgniter with Apache/XAMPP or a multi-worker server. Do not use the
single-thread PHP development server: the Python callback needs another PHP
worker while the gateway waits. Python stays loopback-only and the services
must run on the same host in this first phase. Restart Apache if required to
pick up environment settings. Never expose the AI service folder as static files.

## 3. Test in Postman

Obtain a normal, currently active Effortrak app login token. Send these headers
to the CodeIgniter gateway (replace the values privately):

```http
Key: <existing-app-PROJECTKEY>
Authorization: Bearer <existing-effortrak-app-login-token>
Content-Type: application/json
```

| Method | Backend path | Purpose |
| --- | --- | --- |
| GET | `/apiv2/ai/me` | Confirm server-resolved identity, scopes and export permission |
| POST | `/apiv2/ai/chat` | Ask a question or continue a conversation |
| GET | `/apiv2/ai/conversations/{id}` | Owner-only history |
| DELETE | `/apiv2/ai/conversations/{id}` | Delete a conversation and its files |
| GET | `/apiv2/ai/reports/{id}/download` | Download with the same login headers |

Chat body:

```json
{"message":"Show my attendance for 2026-09"}
```

Export body:

```json
{"message":"Export my attendance for 2026-09 as xlsx"}
```

Use `pdf` instead of `xlsx` for a PDF. Pass the returned `conversation_id` in
subsequent chat requests. Follow the artifact `download_url` with the same
authentication headers; links are not public. Reports use shared My Attendance
calendar rules, not payroll payable-day calculations. XLSX fields are written
as literal text, preventing spreadsheet formula execution from employee names.

Verify isolation with two companies and users having different role grants:
foreign-employee reads must fail, another owner must receive 404 for conversation
or report IDs, and logout/revoking the device must invalidate access. Removing
grants or changing direct-report membership invalidates prior conversation policy.
Requests cannot set `company_id`, `user_id` or role grants in the body.

## Errors and Limits

- 401: missing/wrong API key, expired login/delegation, or inactive device/user.
- 403: missing AI/read/export grant, inactive license, or out-of-scope employee.
- 409: permission policy changed; start a fresh conversation.
- 413: more than 50 employees or a report above 8 MB; narrow the scope.
- 429: prototype per-user gateway limit reached (20 requests/minute).
- 502: invalid/unavailable upstream service or AI provider.
- 503: integration disabled, missing secret/model settings, or unavailable limiter.

Attendance details sent to the model are capped at 120 rows with an explicit
truncation flag; full calendar summaries and authorized report exports remain
available. Tool loops and provider time are bounded. SQLite is local storage,
not an encrypted production audit store. Production rollout still requires
retention policies, distributed/atomic rate limiting, concurrency/load testing,
audit logging, monitoring, and external deployment hardening.

## Web Chat

The organisation portal includes an AI Assistant at `/admin/ai-assistant`, linked
by the chat icon in the header for eligible employees. Flutter is unchanged.
The web endpoints use the existing browser session and global CSRF protection;
no app API key or mobile login token is sent from JavaScript. Web delegation is
cached for at most 120 seconds and explicitly revoked by organisation sign-out.
Platform impersonation and client accounts cannot use this bridge. Current
user/company/role/licensing and tool scopes are still checked on every callback.

Recent conversations are owner- and permission-policy-scoped. History restores
report links, and each download rechecks permission. The browser renders model
output as plain text, not executable HTML, and stores no transcripts in browser
storage. Sending data to OpenAI still needs organisation approval as described
above. The existing AI capability migration and role grants are required for
non-SUPER ADMIN users; the frontend does not automatically grant access.

Browser workflow tests render the actual view and mock service responses. They
require Playwright and Chrome (paths can be overridden with
`PLAYWRIGHT_MODULE_PATH`, `BROWSER_BINARY`, and `PHP_BINARY`):

```powershell
# From effortrak_web
node tests/browser/ai-assistant.cjs
```

## Automated Tests

Python tests use mocked HTTP callbacks; backend tests use in-memory SQLite and
do not modify your application database:

```powershell
# From effortrak_ai_service
.\.venv\Scripts\python.exe -m pytest -q
```

```powershell
# From effortrak_web (absolute paths avoid local PHPUnit working-directory issues)
D:\xampp\php\php.exe -d extension=sqlite3 vendor\phpunit\phpunit\phpunit --no-configuration --bootstrap D:\xampp\htdocs\effortrak\effortrak_web\tests\bootstrap.php D:\xampp\htdocs\effortrak\effortrak_web\tests\unit\AiAccessServiceTest.php
```
