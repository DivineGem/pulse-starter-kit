# Pulse project progress

**Status:** Working local Compose implementation; production deployment and archival work remain planned.

## Implemented

### Monitoring

- Independent worker and interval per mock service: payments (10s), claims (15s), quotes (30s), notifications (60s).
- Per-request timeout, initial scheduler jitter, and timing metrics.
- Two-consecutive-failure down threshold and two-success recovery threshold.
- Successful responses at or above the configured latency threshold are marked degraded.
- Probe state, response latency, failure category, and scheduler delay are recorded.
- Read-only control-state context is used for internal incident diagnosis; Pulse does not call control POST endpoints.

### Incidents and operator workflow

- Incidents open on detected service failures/degradation and resolve automatically after confirmed recovery.
- Incident timeline records detection, status changes, recovery, notes, and note edits.
- Operators can add notes and edit their own notes on open or resolved incidents; authorship and edit timestamps are retained.
- Dedicated username/password login page replaces the browser auth popup.
- Server-side session tokens are random, stored hashed, placed in HttpOnly/SameSite cookies, expire, and can be revoked by logout.
- Multiple operator accounts can be configured.

### Dashboards and notifications

- Authenticated operator dashboard shows service state, timestamps, latency, monthly reliability/error budget, scheduler/detection/recovery metrics, incidents, and timelines.
- Public status page and `/api/status` work without login and expose customer-facing names/status only. The page requests a snapshot on open/reload and does not poll continuously.
- Generic audience-safe status transition messages are delivered to the shared webhook receiver through a persistent retry outbox.

### Persistence and run experience

- SQLite records probes, incidents, timeline events, operator sessions, and pending webhook notifications in a named Docker volume.
- Compose starts Pulse, the four mock services, and webhook receiver with one command: `docker compose up --build`.
- README documents project startup, simulation controls, endpoints, Pulse modules, and configuration.
- `pulse/README.md` documents the Pulse service itself.
- `plan.md` records user requirements and the high-level design diagram.
- `transcripts/` records the project conversation.

## Verification completed

- Python modules compile with `python3 -m compileall -q pulse`.
- Compose config validates with `docker compose config --quiet`.
- Pulse image builds and starts healthy in Docker Compose.
- Public status and authenticated operator APIs respond.
- Login, logout, protected APIs, incident note creation/editing, and note edit timeline audit were exercised.
- Controlled claims down/recovery and payments slow/recovery behavior were exercised; mock services were reset afterward.

## Planned / not yet implemented

- Separate production Pulse hosting from the monitored services' host/failure domain.
- Add an external monitoring provider/check for Pulse's own host/process availability.
- Seven-day hot storage for incidents followed by durable persistent archival; open incidents should remain hot until resolved.
- Compressed archival of detailed probe history after 30 days while keeping incident/reliability summaries retrievable.
- Production-grade deployment sizing, availability, TLS setup, backup/restore, and cloud-specific cost confirmation.

## Known scope notes

- Reliability and detection/coverage figures are estimates from available probe data; they are not a contractual SLO accounting system.
- Current SQLite storage is persistent but no retention/compaction/archive job is implemented yet.
- The supplied webhook receiver is a shared demo sink, so notifications intentionally use generic audience-safe content rather than audience-specific channels.
- Default local operator credentials are for development only and should be changed before sharing the dashboard.
