const refreshStatus = async () => {
  const response = await fetch('/api/status', { cache: 'no-store' });
  const data = await response.json();
  const target = document.querySelector('#services'); target.replaceChildren();
  const list = data.services;
  const bad = list.filter(s => s.status !== 'Operational' && s.status !== 'Checking');
  const overall = document.querySelector('#overall');
  overall.textContent = bad.length ? 'Some services are experiencing issues' : list.some(s => s.status === 'Checking') ? 'Checking service status' : 'All systems operational';
  document.querySelector('#updated-at').textContent = `Updated ${new Date(data.updated_at).toLocaleTimeString()}`;
  for (const service of list) {
    const row = document.createElement('article'); row.className = 'public-service';
    const name = document.createElement('strong'); name.textContent = service.name;
    const status = document.createElement('span'); status.textContent = service.status; status.className = `status status-${service.status === 'Operational' ? 'up' : service.status.startsWith('Degraded') ? 'degraded' : 'down'}`;
    const when = document.createElement('small'); when.textContent = service.updated_at ? `Last updated ${new Date(service.updated_at).toLocaleTimeString()}` : 'Status check starting';
    row.append(name, status, when); target.append(row);
  }
};
refreshStatus(); setInterval(refreshStatus, 5000);
