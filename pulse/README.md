# Pulse

Pulse is the Python service-status and incident-management application in this starter kit. It checks the four mock services independently, tracks incidents and recoveries, provides an authenticated operator dashboard, publishes a sanitized public status page, and sends generic transition messages to the supplied webhook receiver.

For the full kit—including mock-service controls and the incident drill—see the [project README](../README.md). For the requirements and design diagram, see [`plan.md`](../plan.md).

## Run Pulse with the kit

From the repository root, start Pulse and its dependencies with:

```sh
docker compose up --build
```

| Pulse interface | URL | Access |
| --- | --- | --- |
| Operator login | <http://localhost:8000/login> | Username and password required |
| Operator dashboard | <http://localhost:8000/> | Requires an operator session |
| Public status page | <http://localhost:8000/status> | Public, no login |
| Public status API | <http://localhost:8000/api/status> | Public, sanitized response |
| Liveness | <http://localhost:8000/healthz> | Public process-health check |

Local default operator credentials are `operator` / `pulse-local-change-me`. Change them before sharing or deploying Pulse. Set `OPERATOR_PASSWORD` to a unique password (minimum 12 characters), or configure individual accounts with `OPERATOR_USERS`:

```sh
OPERATOR_USERS='{"alice":"a-long-unique-password","bob":"another-long-password"}' docker compose up --build
```

When using HTTPS, set `OPERATOR_COOKIE_SECURE=true`. Operator sessions are stored server-side by token hash and expire after `OPERATOR_SESSION_TTL_SECONDS` (12 hours by default).

## Monitoring behavior

Pulse gives each service its own scheduler and request timeout. A slow or failed probe does not block the other service workers.

| Service | Probe interval | Monthly reliability target |
| --- | ---: | ---: |
| Payments | 10 seconds | 99.9% |
| Claims | 15 seconds | 99.9% |
| Quotes | 30 seconds | 99% |
| Notifications | 60 seconds | 99% |

- Two consecutive failed probes mark a service down; two successful probes confirm recovery.
- A successful probe taking 1.5 seconds or longer is marked degraded.
- Probe timeout defaults to 4 seconds.
- Pulse reads health endpoints and, on relevant transitions, read-only control-state endpoints. It does not call service control POST endpoints.
- Incident records, notes, probe checks, and a webhook outbox persist in the `pulse-data` Docker volume.
- Webhook messages use generic customer-safe product wording. Operator diagnostics and notes are not included in public status responses or webhook messages.
- Reliability and error-budget values are estimates derived from collected probe history; the dashboard shows available history and sample coverage.

## Operator incident notes

After signing in, operators can add notes to incident timelines. An operator can edit notes they authored; edits are timestamped and recorded in the incident timeline. Operators cannot edit another operator's notes or Pulse-generated events. Notes are internal and are not sent to the webhook or public status page. Notes may be edited on open or resolved incidents.

## API overview

All `/api/operator/*` endpoints require a valid operator session cookie.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/api/login` | Authenticate and create a session; requires `X-Pulse-Request: 1` |
| `POST` | `/api/logout` | Revoke the current session; requires `X-Pulse-Request: 1` |
| `GET` | `/api/operator/me` | Current operator identity |
| `GET` | `/api/operator/status` | Live service states, safe diagnostics, and reliability budgets |
| `GET` | `/api/operator/metrics` | 30-day checking, detection, and recovery metrics |
| `GET` | `/api/operator/incidents` | Incidents and ordered timelines; optional `?state=OPEN` or `?state=RESOLVED` |
| `POST` | `/api/operator/incidents/{id}/notes` | Add an incident note; requires `X-Pulse-Request: 1` |
| `PATCH` | `/api/operator/incidents/{id}/notes/{event_id}` | Edit a note authored by the current operator; requires `X-Pulse-Request: 1` |
| `GET` | `/api/operator/checks/{service}` | Recent check history for one service |
| `GET` | `/api/status` | Public, audience-safe status |

## Configuration

The variables below are passed to the Pulse container by Compose. Set them in the shell or a local `.env` file (which is ignored by Git).

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPERATOR_USERNAME` | `operator` | Single operator account username |
| `OPERATOR_PASSWORD` | `pulse-local-change-me` | Single operator password; at least 12 characters |
| `OPERATOR_USERS` | unset | JSON object of operator usernames and passwords; overrides the single account |
| `OPERATOR_SESSION_TTL_SECONDS` | `43200` | Session lifetime; allowed range is 300–604800 seconds |
| `OPERATOR_COOKIE_SECURE` | `false` | Set to `true` when Pulse is served via HTTPS |
| `PULSE_PORT` | `8000` | Localhost host port for Pulse |
| `PULSE_DB` | `/data/pulse.sqlite3` | SQLite path inside the container |
| `PROBE_TIMEOUT_SECONDS` | `4` | Per-probe timeout |
| `FAILURE_THRESHOLD` | `2` | Consecutive failures before DOWN |
| `RECOVERY_THRESHOLD` | `2` | Consecutive successes before recovery |
| `SLOW_THRESHOLD_SECONDS` | `1.5` | Successful response latency at/above which status is degraded |
| `WEBHOOK_URL` | Compose receiver URL | Destination for generic transition messages |

## Backend layout

| File | Responsibility |
| --- | --- |
| `app.py` | FastAPI assembly, lifecycle, middleware, and worker startup/shutdown |
| `api.py` | Login, public status, dashboard APIs, and incident-note routes |
| `config.py` | Environment validation and service definitions |
| `security.py` | Credential validation, session-cookie handling, and write-intent checks |
| `monitor.py` | Per-service workers, health transitions, and incident creation/recovery |
| `metrics.py` | Reliability, error-budget, scheduler, detection, and recovery calculations |
| `notifications.py` | Customer-safe message formatting and durable delivery retries |
| `database.py` | SQLite schema, connection/transaction management, and timeline helpers |
| `state.py` | Lock-protected in-memory service snapshots and shutdown signal |
| `utils.py` | Shared time and percentile helpers |
| `static/` | Operator login/dashboard and public status frontend |

## Local development and tests

Compile the Python modules and validate Compose configuration from the repository root:

```sh
python3 -m compileall -q pulse
docker compose config --quiet
```

Run the supplied integration drill from the repository root after starting the kit:

```sh
bash scripts/incident-drill.sh --fast
```

This changes mock-service control modes during the drill, then resets the services on normal completion. Use `bash scripts/incident-drill.sh --reset` if you need to restore them manually.

## Current scope and planned items

The current deployment is a local Compose environment with SQLite and one Pulse process. The project plan also records future production considerations: running Pulse in a separate failure domain from monitored services, external monitoring of Pulse itself, seven-day hot incident storage with durable historical archive, and compressed probe-data archival after 30 days. Those production placement and retention workflows are not implemented by the current Compose application.
