const $ = selector => document.querySelector(selector);
const state = $('#workspace-state');

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '#';
  } catch { return '#'; }
}

function showState(title, message, kind = '') {
  state.className = `state-panel ${kind}`;
  state.innerHTML = `<span class="state-mark" aria-hidden="true">${kind === 'error' ? '!' : 'i'}</span><div><h3>${title}</h3><p>${message}</p></div>`;
  state.hidden = false;
  $('#workspace-data').hidden = true;
  $('#instance-link').hidden = true;
}

function escapeHtml(value = '') {
  return String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

async function loadWorkspace() {
  $('#refresh').disabled = true;
  try {
    const response = await fetch('/api/redmine', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Redmine is unavailable.');
    if (!data.configured) {
      showState('Connect your Redmine instance', data.message || 'Set REDMINE_URL and REDMINE_API_KEY in the server environment, then restart StormPilot. The API key remains on the server.');
      $('#last-updated').textContent = 'Not configured';
      return;
    }
    $('#instance-link').href = safeUrl(data.instance_url);
    $('#instance-link').hidden = false;
    $('#instance-host').textContent = new URL(data.instance_url).host;
    $('#project-count').textContent = Number(data.counts.projects || 0).toLocaleString();
    $('#issues-count').textContent = Number(data.counts.issues || 0).toLocaleString();
    $('#project-total').textContent = data.projects.length;
    $('#issues-total').textContent = data.issues.length;
    $('#projects').innerHTML = data.projects.length ? data.projects.map(project => `<a class="project-row" href="${escapeHtml(safeUrl(project.url))}" target="_blank" rel="noopener noreferrer"><span class="project-mark" aria-hidden="true">${escapeHtml((project.name || 'P').slice(0, 1).toUpperCase())}</span><span class="row-main"><strong>${escapeHtml(project.name)}</strong><small>${escapeHtml(project.description || 'No description')}</small></span><span class="row-status">Project</span><span class="arrow" aria-hidden="true">↗</span></a>`).join('') : '<div class="empty">No projects are visible to this Redmine account.</div>';
    $('#issues').innerHTML = data.issues.length ? data.issues.map(item => `<a class="work-row" href="${escapeHtml(safeUrl(item.url))}" target="_blank" rel="noopener noreferrer"><span class="work-id">#${escapeHtml(item.id)}</span><span class="row-main"><strong>${escapeHtml(item.subject)}</strong><small>${escapeHtml([item.project, item.tracker, item.assignee].filter(Boolean).join(' · ') || 'Issue')}</small></span><span class="work-status">${escapeHtml(item.status || 'Open')}</span></a>`).join('') : '<div class="empty">No open issues are visible in this instance.</div>';
    state.hidden = true;
    $('#workspace-data').hidden = false;
    $('#last-updated').textContent = `Updated ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  } catch (error) {
    showState('Could not reach Redmine', error.message || 'Check the server configuration and try again.', 'error');
    $('#last-updated').textContent = 'Connection issue';
  } finally {
    $('#refresh').disabled = false;
  }
}

async function refreshService() {
  try {
    const [health, metrics] = await Promise.all([fetch('/readyz', { cache: 'no-store' }), fetch('/metrics', { cache: 'no-store' })]);
    if (!health.ok || !metrics.ok) throw new Error();
    $('#service-state').textContent = 'Dispatch service operational';
    $('#service-detail').textContent = 'Health, readiness and Prometheus metrics are responding';
  } catch {
    $('#service-state').textContent = 'Dispatch service unavailable';
    $('#service-detail').textContent = 'One or more service checks did not respond';
  }
}

$('#refresh').addEventListener('click', () => { loadWorkspace(); refreshService(); });
$('#dispatch').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.innerHTML = '<span aria-hidden="true">…</span> Sending';
  try {
    const response = await fetch('/dispatch', { cache: 'no-store' });
    if (!response.ok) throw new Error('Dispatch request failed');
    button.innerHTML = '<span aria-hidden="true">✓</span> Dispatch passed';
    window.setTimeout(() => { button.innerHTML = '<span aria-hidden="true">↗</span> Test dispatch'; button.disabled = false; }, 1800);
  } catch {
    button.innerHTML = '<span aria-hidden="true">!</span> Try again';
    button.disabled = false;
  }
});

loadWorkspace();
refreshService();
