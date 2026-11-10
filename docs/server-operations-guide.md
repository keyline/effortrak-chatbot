# Effortrak AI Service Quick Guide

Updated: 10 November 2026. For the existing cPanel deployment; confirm paths with your host.
No server reboot or database reset is needed.

## 1. Open cPanel Terminal

Activate Python and open the application folder:

```bash
source /home/keyline2/virtualenv/effortrakchatbot/3.13/bin/activate
cd /home/keyline2/effortrakchatbot
```

## 2. Check the Service

```bash
ss -ltnp 'sport = :8010'
curl --fail --silent --show-error http://127.0.0.1:8010/health
```

If healthy and no changes are pending, leave it running. If systemd, Supervisor
or your host manages it, use that manager's restart procedure instead of the
manual steps below. cPanel's Restart button may not control a manual worker.

## 3. Stop the Old Worker When Restarting

Skip this step if nothing is listening. Enter the PID shown in Step 2:

```bash
read -r -p "Enter the Uvicorn PID: " AI_PID
ps -p "$AI_PID" -o pid,user,args
```

Confirm it is your Effortrak Uvicorn process on port 8010, then stop it:

```bash
if [[ "$AI_PID" =~ ^[1-9][0-9]*$ && "$AI_PID" != 1 ]]; then
  kill -TERM "$AI_PID"
fi
ss -ltnp 'sport = :8010'
```

Wait until the port is free. Do not stop unrelated Python processes.
If the worker reappears automatically, ask the host which manager owns it.

## 4. Start in the Background

Run only when port 8010 is free, from the activated environment in Step 1:

```bash
mkdir -p logs
chmod 700 logs
umask 077
nohup python -u -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8010 --no-proxy-headers --no-access-log --workers 1 \
  >> logs/uvicorn.log 2>&1 < /dev/null &
```

Run one worker only. Do not use `--reload` or expose port 8010 publicly.
`nohup` survives terminal closure, not crashes or reboots. Ask the host to
configure a managed service for automatic recovery.

## 5. Verify

```bash
curl --fail --silent --show-error http://127.0.0.1:8010/health
tail -n 50 logs/uvicorn.log
tail -n 50 logs/application.log
```

Expected: `status: ok`, `mode: openai`, `data_source: effortrak_backend`.
The current `production_ready: false` label is not a startup error.

Also check `https://effortrakchatbot.keylines.in/health`. Hard-refresh the
dashboard, start a new chat and test a report. Health alone does not test
model access, backend permissions or token recording.

## Updating Code or Settings

1. Back up the release, private `.env` files and AI SQLite state.
2. Stop the worker gracefully and deploy the approved web/Python release.
3. If dependencies changed, run `python -m pip install -r requirements.txt`
   and `python -m pip check` in the activated environment.
4. Start and verify using Steps 4 and 5. Hard-refresh after browser JS/CSS changes.

Preserve the existing `DATABASE_PATH`; do not replace `.env` or the database
with developer copies. Copy SQLite files only while stopped, or use SQLite
backup tools. Python code and AI `.env` changes require a worker restart;
FAQ-text-only changes do not. Run SQL only when a release explicitly requires it.

### Report Export Update

Deploy the AI service and web `AiExportChart.php`, `AiAttendanceService.php` and
`AiAttendanceExportService.php` together. Back up AI SQLite state first, then restart
the worker. SQLite adds private export-snapshot columns automatically; no MySQL
migration or mobile UI rebuild is needed for the existing download controls.

Test on web and mobile: display an authorized report, then ask `Make that a PDF`
and `Give me this report in Excel format`. PDF contains rendered charts; Excel
contains editable charts. Files contain the displayed selection, not undisplayed
pages. If data or permissions change, regenerate the report before downloading.
Old chat answers need a fresh report before using the new export follow-ups.

## Essential Settings

- AI `.env`: `DATA_MODE=backend`, `CHAT_MODE=openai`.
- AI `.env`: `BACKEND_URL=https://uat3.effortrak.com/` (web application root).
- AI `.env`: `AI_TOKEN_METERING_ENABLED=true` after usage-schema setup.
- Web `.env`: `ai.enabled = true`.
- Web `.env`: `ai.serviceUrl = 'https://effortrakchatbot.keylines.in'`.
- Both servers: matching private `AI_SERVICE_SECRET` / `ai.serviceSecret`.

Use production URLs outside UAT. On different servers, use HTTPS between
services, not `127.0.0.1`. Keep OpenAI keys and secrets private. If a process
manager defines environment values, update them there too.

## Common Issues

- **AI unavailable:** check the listener, local health, log and HTTPS proxy.
- **Port already in use:** identify the existing worker; do not start duplicates.
- **Old answers:** restart the actual worker, hard-refresh and ask again.
- **Credential/clock errors:** ask the host to synchronize both servers' UTC clocks.
- **Access denied:** check company AI enablement, modules, role and employee scope.
- **Usage is zero:** check metering, backend URL, company/month and web schema.
  FAQ answers and chart restyling do not consume model tokens.
- **Pending tokens:** preserve the SQLite outbox and investigate settlement errors.
  Never delete reservations or reset usage counters to clear pending tokens.

Never share keys, tokens, `.env`, databases or unredacted logs in support tickets.

## Interruption Recovery And Diagnosis

The hosting review found PHP (`lsphp`) memory cgroup OOM events, but did not
establish why the Python service stopped. Do not classify this as a confirmed
Python OOM. Logging and automatic recovery help with future incidents; they do
not establish or remove the original cause.

### Deploy The Diagnostic Update

Deploy `app/main.py`, `app/config.py` and the new `app/diagnostics.py` together.
No new dependencies or database migration are required. Preserve the server
`.env` and databases. Optionally set `APPLICATION_LOG_PATH=logs/application.log`
in the existing `.env`; that is the default, relative to the application folder.
Stop and restart the actual worker once using the steps above. Verify local
and public health and an authorized chat/report.

`application.log` contains UTC timestamps, process IDs, startup/readiness,
graceful shutdown, a heartbeat every 60 seconds, request status and duration,
and failure types with stack locations. Request diagnostics omit headers,
query strings, actual path IDs, request bodies and exception messages. Rotation
keeps five backups of at most approximately 5 MiB each, plus the active file.
Only one worker may write this log. Keep logs outside the public document root.
Uvicorn console logs remain separate and may contain raw exception messages;
review and redact them before sharing. The manual console redirect does not
rotate; have the host configure rotation or use the managed configuration below.

### Have The Host Enable Automatic Recovery

Send the host this request:

> Please configure a supported process manager for our single Uvicorn worker,
> with automatic restart after exit and server reboot, bounded stdout/stderr
> logs, and retained exit codes/signals. The command and paths are in
> deploy/supervisor.conf.example. Confirm the account paths, stop the existing
> manual worker before enabling the manager, and verify port 8010 and health.
> Please also review the account's PHP OOM events, memory limits and usage at
> the incident time, and advise what PHP process or workload caused the limit
> to be reached and whether Python shares that limit.

The [Supervisor example](../deploy/supervisor.conf.example) is for the host's
existing Supervisor installation; it is not a standalone configuration. If
they use systemd or another manager, ask for the equivalent behavior. Do not
start a second manager or worker. Supervisor's restart and log rotation options
are documented in its [official configuration reference](https://supervisord.org/configuration.html).
The manager itself must be enabled at boot. Startup retries are finite; failed
startup needs an alert and investigation. Restarting a running but unresponsive
process requires separate health monitoring, not just exit-based restart.

### If It Happens Again

Before restarting, collect these read-only checks in cPanel Terminal. Health
requests have time limits so an unresponsive worker cannot block collection:

```bash
date -u '+%Y-%m-%dT%H:%M:%SZ'
ss -ltnp 'sport = :8010'
ps -u "$(id -un)" -o pid,ppid,lstart,etime,rss,args
curl --max-time 10 --fail --silent --show-error http://127.0.0.1:8010/health
curl --max-time 15 --fail --silent --show-error https://effortrakchatbot.keylines.in/health
tail -n 100 logs/application.log
tail -n 100 logs/uvicorn.log
```

Keep a private copy of the relevant logs, including rotated files. If managed,
also obtain the manager's process status and exit history from the host.
A missing `service_stopped` entry may indicate an abrupt exit, but does not
prove OOM. SIGKILL, power loss and some native failures cannot be logged by
application code. A stale heartbeat can also mean a blocked event loop.
If local health works but public health fails, investigate the proxy/network.
If both health endpoints work but chat fails, inspect request failures and the
backend/provider path. Health is not an end-to-end chat check.

Open a support ticket immediately with the exact UTC time, the affected URL
and symptom, the process PID and redacted diagnostics. Ask the host to correlate
CloudLinux resource-limit faults and manager exit signals for that time window.
Then use the active manager's recovery procedure, or the manual steps above
if unmanaged. Record the recovery time too. Enable external health alerts
through the host or your existing monitoring provider.
