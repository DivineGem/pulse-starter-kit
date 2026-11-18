# Pulse starter kit

Pulse is a service status and incident hub. The kit includes Pulse, four mock services, and a webhook receiver for local development.

## Start the kit

```
docker compose up --build
```

> **Using Podman?** Run `podman compose up --build` instead (needs `podman-compose` or Docker Compose installed). Everything else is the same.

This starts Pulse, four mock Acko services, and a webhook receiver:

| Service | Address | Health check |
| --- | --- | --- |
| `quotes` | http://localhost:7001 | http://localhost:7001/health |
| `payments` | http://localhost:7002 | http://localhost:7002/health |
| `claims` | http://localhost:7003 | http://localhost:7003/health |
| `notifications` | http://localhost:7004 | http://localhost:7004/health |
| Webhook receiver | http://localhost:9900 | Stands in for chat, email and paging. Anything POSTed to it appears on its page. `GET /api/received` returns the list as JSON; `DELETE /api/received` clears it. |
| Pulse operator dashboard | http://localhost:8000 | Authenticated operator view: `operator` / `pulse-local-change-me` by default |
| Pulse public status page | http://localhost:8000/status | Public, customer-safe service status; no login required |

From inside Docker (for example, if you run Pulse in the same Compose network), use the service names instead: `http://claims:8080/health`, `http://webhook-receiver:9000`.

> **Local credentials:** Set a unique `OPERATOR_PASSWORD` (at least 12 characters) before sharing the dashboard. For individual operator accounts and note attribution, set `OPERATOR_USERS` to a JSON object mapping usernames to passwords of at least 12 characters, for example `OPERATOR_USERS='{"alice":"a-long-unique-password","bob":"another-long-password"}'`. Pulse binds its host port to `127.0.0.1` by default. Override the dashboard port with `PULSE_PORT`.

## What Pulse does

- Checks payments every 10 seconds, claims every 15 seconds, quotes every 30 seconds, and notifications every 60 seconds. Each service has its own worker and timeout; a slow or failed check does not block the other services. A small random initial offset spreads checks out.
- Uses two consecutive failed checks to mark a service down and two successful checks to confirm recovery. A successful check taking at least 1.5 seconds is shown as degraded.
- Opens an incident when a service becomes down, captures a safe failure category and read-only control mode for the operator, and automatically resolves the incident after confirmed recovery. Incident timelines and engineer notes remain in SQLite across restarts.
- Sends generic, customer-safe transition messages to the shared webhook receiver. Raw errors, internal hostnames, service implementation details, and engineer notes are not included in public responses or notifications.
- Calculates monthly observed availability and remaining downtime error budget: payments and claims target 99.9%; quotes and notifications target 99%. Reliability is estimated from observed probes and is not a substitute for a contractual SLO measurement system.
- Shows probe latency, scheduling delay, incident detection and recovery times, incident timelines, and monthly reliability in the authenticated operator dashboard.

Operator API calls use HTTP Basic authentication. Useful endpoints:

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `/api/status` | Public | Customer-safe current status only |
| `/api/operator/status` | Operator | Current checks, safe diagnostics, and reliability budget |
| `/api/operator/metrics` | Operator | 30-day detection, recovery, and scheduler metrics |
| `/api/operator/incidents` | Operator | Open and resolved incidents with timelines |
| `/api/operator/incidents/{id}/notes` | Operator | Add a note; POST requires `X-Pulse-Request: 1` |
| `/api/operator/checks/{service}` | Operator | Recent probe history |
| `/healthz` | Public | Pulse process liveness |

Pulse only issues GET requests to the configured health and control-state endpoints; it does not invoke service control POST endpoints. The control state is queried only when an incident opens, and remains operator-only. The public page deliberately uses product-facing feature names instead of internal service identifiers.

### Backend layout

Pulse’s Python backend is split by responsibility to make debugging easier:

| File | Responsibility |
| --- | --- |
| `pulse/app.py` | FastAPI setup, lifecycle, shared security middleware, and route assembly |
| `pulse/__init__.py` | Declares the backend as an importable Python package |
| `pulse/config.py` | Validated environment configuration and monitored service definitions |
| `pulse/api.py` | Public and authenticated operator HTTP routes |
| `pulse/monitor.py` | Independent probe workers, status transitions, and incident lifecycle |
| `pulse/metrics.py` | Monthly reliability/error-budget and scheduler/detection/recovery calculations |
| `pulse/notifications.py` | Sanitized webhook message creation and durable delivery retries |
| `pulse/database.py` | SQLite connections, schema migrations, and incident persistence helpers |
| `pulse/security.py` | Operator authentication and write-request intent checks |
| `pulse/state.py` | Lock-protected in-memory service state and worker stop signal |
| `pulse/utils.py` | Shared stateless time and percentile helpers |

## Configuration and data

Compose environment settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPERATOR_USERNAME` | `operator` | Operator dashboard/API login |
| `OPERATOR_PASSWORD` | `pulse-local-change-me` | Operator password; minimum 12 characters |
| `OPERATOR_USERS` | unset | Optional JSON object of individual operator usernames and passwords (overrides the single-user settings) |
| `PULSE_PORT` | `8000` | Host port for Pulse, bound to localhost |
| `WEBHOOK_PORT` | `9900` | Host port for the provided webhook receiver |

Pulse data lives in the `pulse-data` Docker volume. To remove the local database and all incident history, use `docker compose down -v` (this also removes other Compose volumes, if added).

> **Port already in use?** If something on your laptop already uses port 9900, start the kit with another port for the webhook receiver, for example `WEBHOOK_PORT=9950 docker compose up --build`.

## Simulating trouble

Each mock service has control endpoints. For example, for `claims` on port 7003:

| Command | Effect |
| --- | --- |
| `curl -X POST localhost:7003/control/down` | Health checks return 503 until reset |
| `curl -X POST "localhost:7003/control/slow?ms=3000"` | Health checks succeed, but only after 3 seconds |
| `curl -X POST "localhost:7003/control/flaky?rate=0.3"` | Health checks fail at random, 30% of the time |
| `curl -X POST localhost:7003/control/reset` | Back to normal |
| `curl localhost:7003/control/state` | Shows the current mode |

## The incident drill

`scripts/incident-drill.sh` breaks and repairs the services on a fixed schedule, so you can test Pulse and run a repeatable demo:

```
bash scripts/incident-drill.sh          # full drill, about 7 minutes
bash scripts/incident-drill.sh --fast   # short drill, under 3 minutes
bash scripts/incident-drill.sh --reset  # put every service back to normal
```

## Rules

- You may configure and run the kit however you like, but **don't change the mock services or the webhook receiver**: the evaluators test your product against the original kit.
