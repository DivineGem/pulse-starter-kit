const api = async (url, options = {}) => {
  const response = await fetch(url, { cache: 'no-store', ...options });
  if (response.status === 401) throw new Error('Operator authentication required. Reload and sign in.');
  if (!response.ok) throw new Error(`Pulse request failed (${response.status})`);
  return response.json();
};
const cell = (row, text, className = '') => {
  const td = document.createElement('td'); td.textContent = text ?? '—';
  if (className) td.className = className; row.append(td); return td;
};
const timeText = value => value ? new Date(value).toLocaleString() : '—';
const secondsList = values => values?.length ? values.map(v => Number(v).toFixed(1)).join(', ') : '—';
const statusClass = value => `status status-${String(value).toLowerCase()}`;

async function load() {
  const [overview, efficiency, incidentData] = await Promise.all([
    api('/api/operator/status'), api('/api/operator/metrics'),
    api(`/api/operator/incidents${document.querySelector('#incident-filter').value ? `?state=${document.querySelector('#incident-filter').value}` : ''}`)
  ]);
  const summary = document.querySelector('#pulse-summary'); summary.replaceChildren();
  const openCount = incidentData.incidents.filter(i => i.state === 'OPEN').length;
  for (const [title, value, detail] of [
    ['Pulse monitor', overview.pulse.status === 'healthy' ? 'Healthy' : 'Degraded', `${overview.pulse.stale_schedulers.length} stale · ${overview.pulse.scheduler_errors.length} errors · sampled ${new Date(overview.pulse.checked_at).toLocaleTimeString()}`],
    ['Services monitored', String(overview.services.length), 'Independent probe schedules'],
    ['Open incidents', String(openCount), 'Automatically resolved after recovery']
  ]) {
    const card = document.createElement('article'); card.className = 'summary-card';
    const h = document.createElement('div'); h.className = 'eyebrow'; h.textContent = title;
    const strong = document.createElement('strong'); strong.textContent = value;
    const p = document.createElement('p'); p.textContent = detail;
    card.append(h, strong, p); summary.append(card);
  }
  document.querySelector('#updated-at').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  const services = document.querySelector('#services'); services.replaceChildren();
  for (const service of overview.services) {
    const row = document.createElement('tr');
    cell(row, service.label); const status = cell(row, service.status, statusClass(service.status));
    cell(row, timeText(service.last_checked)); cell(row, service.latency_ms == null ? '—' : `${service.latency_ms.toFixed(1)} ms`);
    cell(row, `Every ${service.interval_seconds}s`);
    cell(row, `${service.reliability.observed_percent == null ? '—' : `${service.reliability.observed_percent.toFixed(3)}%`} / ${service.reliability.target_percent}%`);
    cell(row, `${service.reliability.remaining_error_budget_seconds.toFixed(0)} sec${service.reliability.error_budget_exhausted ? ' (exhausted)' : ''}`);
    cell(row, service.open_incident_id ? `#${service.open_incident_id}` : '—');
    if (service.last_error || service.control_mode) status.title = `Last probe category: ${service.last_error || 'none'}; control mode: ${service.control_mode || 'unknown'}`;
    services.append(row);
  }
  const metricRows = document.querySelector('#metrics'); metricRows.replaceChildren();
  for (const metric of efficiency.services) {
    const row = document.createElement('tr');
    cell(row, metric.name); cell(row, metric.probe_count_30d); cell(row, metric.failed_checks_30d);
    cell(row, `${metric.estimated_observation_coverage_percent}%`);
    cell(row, `${metric.avg_scheduler_delay_ms} ms`); cell(row, `${metric.max_scheduler_delay_ms} ms`);
    cell(row, metric.median_detection_seconds == null ? '—' : `${metric.median_detection_seconds}s med / ${metric.p95_detection_seconds}s p95`);
    cell(row, metric.median_recovery_seconds == null ? '—' : `${metric.median_recovery_seconds}s med / ${metric.p95_recovery_seconds}s p95`);
    cell(row, metric.open_incident_duration_seconds == null ? '—' : `${metric.open_incident_duration_seconds}s`);
    metricRows.append(row);
  }
  renderIncidents(incidentData.incidents);
}

function renderIncidents(incidents) {
  const container = document.querySelector('#incidents'); container.replaceChildren();
  if (!incidents.length) { const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = 'No incidents recorded.'; container.append(empty); return; }
  for (const incident of incidents) {
    const article = document.createElement('article'); article.className = 'incident';
    const heading = document.createElement('div'); heading.className = 'incident-heading';
    const title = document.createElement('h3'); title.textContent = `${incident.service} · Incident #${incident.id}`;
    const state = document.createElement('span'); state.textContent = incident.state; state.className = statusClass(incident.state === 'OPEN' ? 'DOWN' : 'UP');
    heading.append(title, state);
    const meta = document.createElement('p'); meta.className = 'muted'; meta.textContent = `Detected ${timeText(incident.detected_at)} · ${incident.recovered_at ? `Recovered ${timeText(incident.recovered_at)}` : 'Ongoing'} · Detection ${incident.detection_seconds == null ? 'unknown' : `${Number(incident.detection_seconds).toFixed(1)}s`} · Recovery ${incident.recovery_seconds == null ? 'ongoing' : `${Number(incident.recovery_seconds).toFixed(1)}s`}`;
    const list = document.createElement('ol'); list.className = 'timeline';
    for (const item of incident.events) {
      const li = document.createElement('li'); const stamp = document.createElement('time'); stamp.textContent = timeText(item.at);
      const msg = document.createElement('span'); msg.textContent = `${item.author}: ${item.message}`;
      li.append(stamp, msg); list.append(li);
    }
    article.append(heading, meta, list);
    const form = document.createElement('form'); form.className = 'note-form';
    const input = document.createElement('input'); input.name = 'note'; input.maxLength = 2000; input.placeholder = 'Add an internal incident note'; input.required = true;
    const button = document.createElement('button'); button.type = 'submit'; button.textContent = 'Add note'; form.append(input, button);
    form.addEventListener('submit', async e => {
      e.preventDefault(); button.disabled = true;
      try { await api(`/api/operator/incidents/${incident.id}/notes`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Pulse-Request': '1' }, body: JSON.stringify({ note: input.value }) }); await load(); }
      catch (error) { alert(error.message); button.disabled = false; }
    });
    article.append(form);
    container.append(article);
  }
}

document.querySelector('#incident-filter').addEventListener('change', () => load().catch(showError));
function showError(error) { document.querySelector('#updated-at').textContent = error.message; }
load().catch(showError);
setInterval(() => load().catch(showError), 5000);
