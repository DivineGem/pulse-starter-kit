const refreshStatus = async () => {
  // This public endpoint contains only sanitized feature names and public state.
  const response = await fetch('/api/status', { cache: 'no-store' });
  const data = await response.json();
  const target = document.querySelector('#services'); target.replaceChildren();
  const list = data.services;
  // Treat initial checks as pending rather than showing a false outage on page load.
  const bad = list.filter(s => s.status !== 'Operational' && s.status !== 'Checking');
  const overall = document.querySelector('#overall');
  overall.textContent = bad.length ? 'Some services are experiencing issues' : list.some(s => s.status === 'Checking') ? 'Checking service status' : 'All systems operational';
  document.querySelector('#updated-at').textContent = `Updated ${new Date(data.updated_at).toLocaleTimeString()}`;
  for (const service of list) {
    const row = document.createElement('article'); row.className = 'public-service';
    // Use textContent for all server-provided strings to avoid HTML injection.
    const name = document.createElement('strong'); name.textContent = service.name;
    const status = document.createElement('span'); status.textContent = service.status; status.className = `status status-${service.status === 'Operational' ? 'up' : service.status.startsWith('Degraded') ? 'degraded' : 'down'}`;
    const when = document.createElement('small'); when.textContent = service.updated_at ? `Last updated ${new Date(service.updated_at).toLocaleTimeString()}` : 'Status check starting';
    row.append(name, status, when); target.append(row);
  }
};
// The public page polls independently from the backend's service-check intervals.
refreshStatus(); setInterval(refreshStatus, 5000);
