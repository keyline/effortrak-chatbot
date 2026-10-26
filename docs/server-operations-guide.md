# Effortrak AI Service Quick Guide

Updated: 26 October 2026. For the existing cPanel deployment; confirm paths with your host.
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
nohup python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8010 --no-proxy-headers \
  >> logs/uvicorn.log 2>&1 < /dev/null &
```

Run one worker only. Do not use `--reload` or expose port 8010 publicly.
`nohup` survives terminal closure, not crashes or reboots. Ask the host to
configure a managed service for automatic recovery.

## 5. Verify

```bash
curl --fail --silent --show-error http://127.0.0.1:8010/health
tail -n 50 logs/uvicorn.log
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
