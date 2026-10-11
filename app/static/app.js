/* StormPilot PM — dependency-free UI for the Scrum and Kanban modules. */
'use strict';

const state = { sprints: [], stories: [], tasks: [], board: null, openSprint: null, dispatchActivity: [], probeSamples: [], overviewRefreshing: false, recordEditor: null, recordEditorTrigger: null, confirmationResolver: null, confirmationTrigger: null, filters: { search: '', status: '', priority: '', taskType: '', assignee: '', samples: false } };
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const TASK_STATUSES = ['backlog', 'todo', 'in_progress', 'code_review', 'testing', 'done', 'blocked'];
const TASK_STATUS_LABELS = { backlog: 'Backlog', todo: 'To Do', in_progress: 'In Progress', code_review: 'Code Review', testing: 'Testing', done: 'Done', blocked: 'Blocked' };

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

let toastTimer = null;
function toast(message, isError) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('error', !!isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 5000 : 2500);
}

async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON error body */ }
  if (!res.ok) {
    const message = data && data.error ? data.error : ('HTTP ' + res.status);
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ------------------------- navigation / views -------------------------- */

function showView(name) {
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === name));
  $$('.view').forEach(view => { view.hidden = view.id !== `view-${name}`; });
  location.hash = `#${name}`;
  if (name === 'kanban') renderBoard();
}

function bindNav() {
  $$('.tab').forEach(tab => tab.addEventListener('click', () => showView(tab.dataset.view)));
  $$('[data-go-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.goView)));
  const initialView = location.hash.slice(1);
  if (['overview', 'scrum', 'kanban'].includes(initialView)) showView(initialView);
}

/* ----------------------------- data reload ----------------------------- */

async function reload() {
  const [sprints, stories, tasks] = await Promise.all([
    api('GET', '/api/sprints'), api('GET', '/api/backlog'), api('GET', '/api/tasks'),
  ]);
  state.sprints = sprints;
  state.stories = stories;
  state.tasks = tasks;
  renderStories();
  renderSprints();
  renderTaskTable();
  renderBoard();
  renderOverview();
}

/* ------------------------ live service overview ----------------------- */

function formatUptime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m ${total % 60}s`;
}

function setCheckStatus(id, isHealthy, label) {
  const element = $(`#${id}`);
  element.textContent = label || (isHealthy ? 'Operational' : 'Unavailable');
  element.classList.toggle('is-healthy', isHealthy);
  element.classList.toggle('is-unhealthy', !isHealthy);
}

async function measuredApi(method, path) {
  const started = performance.now();
  const value = await api(method, path);
  return { value, duration: performance.now() - started };
}

async function refreshOverview() {
  if (state.overviewRefreshing) return;
  state.overviewRefreshing = true;
  const updated = $('#overview-updated');
  updated.textContent = 'Refreshing service…';
  const readText = async path => {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.text();
  };
  try {
    const [liveness, readiness, identity, metrics] = await Promise.allSettled([
      measuredApi('GET', '/healthz'), api('GET', '/readyz'), api('GET', '/'), readText('/metrics'),
    ]);
    const liveOk = liveness.status === 'fulfilled' && liveness.value.value.status === 'ok';
    const readyOk = readiness.status === 'fulfilled' && readiness.value.status === 'ok';
    const metricsText = metrics.status === 'fulfilled' ? metrics.value : '';
    const metricsOk = metrics.status === 'fulfilled' && metricsText.includes('stormpilot_uptime_seconds');
    const identityData = identity.status === 'fulfilled' ? identity.value : null;
    const serviceOk = liveOk && readyOk && identityData && identityData.project === 'StormPilot';
    state.probeSamples.push({ at: Date.now(), duration: liveness.status === 'fulfilled' ? liveness.value.duration : null });
    state.probeSamples = state.probeSamples.slice(-24);

    setCheckStatus('check-liveness', liveOk);
    setCheckStatus('check-readiness', readyOk);
    setCheckStatus('check-metrics', metricsOk);
    $('#metric-health').textContent = serviceOk ? 'Operational' : 'Degraded';
    $('#metric-health').classList.toggle('value-alert', !serviceOk);
    $('#metric-health-dot').classList.toggle('is-healthy', !!serviceOk);
    $('#metric-health-dot').classList.toggle('is-unhealthy', !serviceOk);
    $('#metric-health-detail').textContent = serviceOk ? 'Health and readiness checks passing' : 'One or more service checks failed';
    $('#metric-dispatches').textContent = metricsOk ? dispatchCount(metricsText).toLocaleString() : '—';

    const uptimeMatch = metricsText.match(/^stormpilot_uptime_seconds\s+([\d.]+)/m);
    $('#metric-uptime').textContent = metricsOk && uptimeMatch ? formatUptime(Number(uptimeMatch[1])) : '—';
    $('#service-version').textContent = `VERSION ${identityData ? esc(identityData.version) : '—'}`;
    const instance = identityData ? `${identityData.instance} · ${identityData.version}` : 'Service identity unavailable';
    $('#metric-instance').textContent = identityData ? `Instance ${identityData.instance}` : 'Waiting for service identity';
    $('#service-instance').textContent = instance;
    $('#service-instance').title = identityData ? `Project: ${identityData.project}` : '';
    updated.textContent = `Updated ${new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
    renderProbeTrend();
  } catch (err) {
    $('#metric-health').textContent = 'Unavailable';
    $('#metric-health').classList.add('value-alert');
    $('#metric-health-detail').textContent = 'Could not reach the local service';
    setCheckStatus('check-liveness', false);
    setCheckStatus('check-readiness', false);
    setCheckStatus('check-metrics', false);
    state.probeSamples.push({ at: Date.now(), duration: null });
    state.probeSamples = state.probeSamples.slice(-24);
    renderProbeTrend();
    updated.textContent = 'Service check failed';
  } finally {
    state.overviewRefreshing = false;
  }
}

function renderProbeTrend() {
  const svg = $('#probe-chart');
  const empty = $('#probe-empty');
  const samples = state.probeSamples.slice(-24);
  const valid = samples.filter(sample => Number.isFinite(sample.duration));
  const latest = samples[samples.length - 1];
  $('#probe-window').textContent = `${samples.length} ${samples.length === 1 ? 'check' : 'checks'} · this session`;
  $('#probe-latest').textContent = latest && Number.isFinite(latest.duration)
    ? `${latest.duration.toFixed(1)} ms` : (latest ? 'Unavailable' : '—');
  $('#probe-average').textContent = valid.length
    ? `${(valid.reduce((total, sample) => total + sample.duration, 0) / valid.length).toFixed(1)} ms` : '—';
  svg.setAttribute('aria-label', valid.length
    ? `${valid.length} measured health-check response times from this browser session`
    : 'No successful health-check response times yet');
  empty.hidden = valid.length > 0;
  empty.textContent = samples.length && !valid.length ? 'Health probe unavailable' : 'Waiting for the first health check';
  if (!valid.length) {
    svg.innerHTML = '';
    return;
  }

  const width = 360;
  const height = 76;
  const inset = 6;
  const ceiling = Math.max(20, Math.ceil(Math.max(...valid.map(sample => sample.duration)) / 10) * 10);
  const points = samples.map((sample, index) => ({
    sample,
    x: samples.length === 1 ? width / 2 : inset + index * (width - inset * 2) / (samples.length - 1),
    y: Number.isFinite(sample.duration)
      ? height - inset - (sample.duration / ceiling) * (height - inset * 2)
      : null,
  }));
  const segments = [];
  let segment = [];
  for (const point of points) {
    if (point.y == null) {
      if (segment.length) segments.push(segment);
      segment = [];
    } else {
      segment.push(point);
    }
  }
  if (segment.length) segments.push(segment);
  const linePaths = segments.map(group => group.map((point, index) =>
    `${index ? 'L' : 'M'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' '));
  const areaPaths = segments.filter(group => group.length > 1).map(group =>
    `${group.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ')} L${group[group.length - 1].x.toFixed(1)},${(height - inset).toFixed(1)} L${group[0].x.toFixed(1)},${(height - inset).toFixed(1)} Z`);
  const circles = points.filter(point => point.y != null).map(point =>
    `<circle cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="2.2" />`).join('');
  svg.innerHTML = `<path class="probe-area" d="${areaPaths.join(' ')}"></path><path class="probe-line" d="${linePaths.join(' ')}"></path>${circles}`;
}

function dispatchCount(metricsText) {
  const lines = metricsText.split('\n');
  return lines.reduce((total, line) => {
    if (!line.startsWith('stormpilot_requests_total{') ||
        !line.includes('route="/dispatch"') || !line.includes('status="200"')) return total;
    const match = line.match(/\s([\d.]+)$/);
    return total + (match ? Number(match[1]) : 0);
  }, 0);
}

function readDispatchActivity() {
  try {
    const history = JSON.parse(localStorage.getItem('stormpilot.dispatch-history') || '[]');
    return Array.isArray(history) ? history.slice(0, 8) : [];
  } catch (_) {
    return [];
  }
}

function renderDispatchActivity() {
  const list = $('#dispatch-activity');
  const entries = state.dispatchActivity.slice(0, 4);
  $('#activity-count').textContent = `${state.dispatchActivity.length} ${state.dispatchActivity.length === 1 ? 'request' : 'requests'}`;
  if (!entries.length) {
    list.innerHTML = '<div class="empty-state">No test dispatches recorded in this browser yet.</div>';
    return;
  }
  list.innerHTML = entries.map(entry => {
    const succeeded = entry.status === 'dispatched';
    const time = new Date(entry.at).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    const detail = succeeded
      ? `Checksum ${esc(entry.checksum)} · ${esc(entry.duration)} ms`
      : esc(entry.message || 'Request failed');
    return `<div class="activity-row"><span class="activity-mark${succeeded ? ' success' : ' failure'}" aria-hidden="true">${succeeded ? '✓' : '!'}</span><span class="activity-copy"><strong>${succeeded ? 'Test dispatch returned' : 'Test dispatch failed'}</strong><small>${detail}</small></span><time datetime="${esc(entry.at)}">${esc(time)}</time></div>`;
  }).join('');
}

function renderOverview() {
  const openTasks = state.tasks.filter(task => task.status !== 'done');
  $('#metric-open-work').textContent = openTasks.length.toLocaleString();
  $('#metric-work-detail').textContent = `${state.tasks.length} total ${state.tasks.length === 1 ? 'task' : 'tasks'} in delivery plan`;

  const work = $('#overview-work');
  if (!state.tasks.length && !state.sprints.length) {
    work.innerHTML = '<div class="empty-state">No delivery work has been logged yet.<button class="text-action" type="button" data-go-view="scrum">Create the first task <span aria-hidden="true">→</span></button></div>';
    work.querySelector('[data-go-view]').addEventListener('click', () => showView('scrum'));
  } else {
    const activeSprint = state.sprints.find(sprint => sprint.status === 'active');
    const upcomingSprint = state.sprints.filter(sprint => sprint.status === 'planned' && sprint.end_date >= new Date().toISOString().slice(0, 10))
      .sort((first, second) => first.start_date.localeCompare(second.start_date))[0];
    const sprint = activeSprint || upcomingSprint;
    const sprintSummary = sprint
      ? `<div class="work-sprint"><span class="sprint-indicator${activeSprint ? ' active' : ''}" aria-hidden="true"></span><span><strong>${esc(sprint.name)}</strong><small>${activeSprint ? 'Active sprint' : `Planned · ${esc(sprint.start_date)} to ${esc(sprint.end_date)}`}</small></span>${activeSprint ? `<b>${sprint.stats.percent}%</b>` : ''}</div>`
      : '<div class="no-sprint">No active or upcoming sprint</div>';
    const taskList = openTasks.slice(0, 4).map(task => `<div class="work-task"><span class="task-status-mark status-${esc(task.status)}" aria-hidden="true"></span><span class="work-task-title">${esc(task.title)}</span>${priorityBadge(task.priority)}</div>`).join('');
    work.innerHTML = `${sprintSummary}${taskList || '<div class="no-open-tasks">All tracked tasks are complete.</div>'}`;
  }
  renderDispatchActivity();
}

function storeDispatchActivity(entry) {
  state.dispatchActivity.unshift(entry);
  state.dispatchActivity = state.dispatchActivity.slice(0, 8);
  try { localStorage.setItem('stormpilot.dispatch-history', JSON.stringify(state.dispatchActivity)); } catch (_) { /* history remains available for this page session */ }
  renderDispatchActivity();
}

async function runTestDispatch() {
  const button = $('#dispatch-run');
  const result = $('#dispatch-result');
  button.disabled = true;
  result.className = 'dispatch-result is-pending';
  result.innerHTML = '<span class="result-mark" aria-hidden="true">↻</span><span><strong>Sending test request…</strong><small>Waiting for the dispatch service response.</small></span>';
  const started = performance.now();
  try {
    const response = await api('GET', '/dispatch');
    const duration = Math.round(performance.now() - started);
    result.className = 'dispatch-result is-success';
    result.innerHTML = `<span class="result-mark" aria-hidden="true">✓</span><span><strong>Dispatch endpoint responded</strong><small>Status: ${esc(response.status)} · Route checksum ${esc(response.route_checksum)} · ${duration} ms</small></span>`;
    storeDispatchActivity({ at: new Date().toISOString(), status: response.status, checksum: response.route_checksum, duration });
    toast('Test dispatch completed');
    await refreshOverview();
  } catch (err) {
    const duration = Math.round(performance.now() - started);
    result.className = 'dispatch-result is-error';
    result.innerHTML = `<span class="result-mark" aria-hidden="true">!</span><span><strong>Dispatch request failed</strong><small>${esc(err.message)} · ${duration} ms</small></span>`;
    storeDispatchActivity({ at: new Date().toISOString(), status: 'failed', message: err.message, duration });
    toast(`Dispatch failed: ${err.message}`, true);
    await refreshOverview();
  } finally {
    button.disabled = false;
  }
}

/* --------------------------- scrum rendering --------------------------- */

function priorityBadge(p) { return `<span class="badge b-${esc(p)}">${esc(p)}</span>`; }
function statusBadge(s) { return `<span class="badge b-status-${esc(s)}">${esc(s.replace('_', ' '))}</span>`; }

function sprintOptions(selected) {
  let html = '<option value="">Backlog</option>';
  for (const s of state.sprints) {
    html += `<option value="${s.id}"${String(selected) === String(s.id) ? ' selected' : ''}>${esc(s.name)}</option>`;
  }
  return html;
}

function renderStories() {
  const list = $('#story-list');
  if (!state.stories.length) {
    list.innerHTML = '<li class="empty">No user stories yet — add one above.</li>';
  } else {
    list.innerHTML = state.stories.map(st => {
      const sprint = state.sprints.find(s => s.id === st.sprint_id);
      return `<li data-id="${st.id}">
        <span class="grow">
          <span class="card-title">${esc(st.title)}</span>
          ${st.description ? `<div class="card-desc">${esc(st.description)}</div>` : ''}
        </span>
        <span class="muted">${st.points} pts</span>
        ${priorityBadge(st.priority)}
        ${sprint ? `<span class="badge b-sprint">${esc(sprint.name)}</span>` : ''}
        <button class="btn btn-ghost btn-small" data-act="story-edit" data-id="${st.id}">Edit</button>
        <button class="btn btn-danger btn-small" data-act="story-del" data-id="${st.id}">Delete</button>
      </li>`;
    }).join('');
  }
  $('#story-sprint-select').innerHTML = sprintOptions(null);
}

function renderSprints() {
  const list = $('#sprint-list');
  if (!state.sprints.length) {
    list.innerHTML = '<p class="empty">No sprints yet — create one above.</p>';
  } else {
    list.innerHTML = state.sprints.map(sp => {
      const st = sp.stats;
      const open = state.openSprint === sp.id;
      const counts = TASK_STATUSES
        .map(key => `${TASK_STATUS_LABELS[key]} ${st.by_status[key] || 0}`).join(' · ');
      return `<article class="sprint-card" data-id="${sp.id}">
        <h3>${esc(sp.name)} ${statusBadge(sp.status)}</h3>
        ${sp.goal ? `<div class="card-desc">${esc(sp.goal)}</div>` : ''}
        <div class="sprint-dates">${esc(sp.start_date)} → ${esc(sp.end_date)}</div>
        <div class="progress" title="${st.percent}% complete"><span style="width:${st.percent}%"></span></div>
        <div class="sprint-stats">
          <strong>${st.percent}%</strong>
          <span>${st.completed_tasks}/${st.total_tasks} done</span>
        </div>
        <div class="sprint-stats">${esc(counts)}</div>
        <div class="sprint-actions">
          <button class="btn btn-ghost btn-small" data-act="sprint-toggle" data-id="${sp.id}">${open ? 'Hide' : 'Details'}</button>
          <button class="btn btn-ghost btn-small" data-act="sprint-edit" data-id="${sp.id}">Edit</button>
          <button class="btn btn-danger btn-small" data-act="sprint-del" data-id="${sp.id}">Delete</button>
        </div>
        <div class="sprint-detail${open ? ' open' : ''}">
          <label>Review notes
            <textarea data-field="review_notes" rows="2" maxlength="10000">${esc(sp.review_notes)}</textarea>
          </label>
          <label>Retrospective notes
            <textarea data-field="retro_notes" rows="2" maxlength="10000">${esc(sp.retro_notes)}</textarea>
          </label>
          <button class="btn btn-small" data-act="sprint-save-notes" data-id="${sp.id}">Save notes</button>
        </div>
      </article>`;
    }).join('');
  }
  $('#task-sprint-select').innerHTML = sprintOptions(null);
}

function renderTaskTable() {
  const tbody = $('#task-table tbody');
  if (!state.tasks.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No tasks yet.</td></tr>';
    return;
  }
  tbody.innerHTML = state.tasks.map(t => `<tr data-id="${t.id}">
    <td><span class="card-title">${esc(t.title)}</span></td>
    <td><select data-act="task-sprint">${sprintOptions(t.sprint_id)}</select></td>
    <td>${statusBadge(t.status)}</td>
    <td>${priorityBadge(t.priority)}</td>
    <td>${esc(t.assignee) || '<span class="muted">—</span>'}</td>
    <td class="muted">${esc(t.due_date) || '—'}</td>
    <td><button class="btn btn-ghost btn-small" data-act="task-edit" data-id="${t.id}">Edit</button></td>
  </tr>`).join('');
}

/* -------------------------- kanban rendering --------------------------- */

async function loadBoard() {
  state.board = await api('GET', '/api/board');
  renderBoard();
  return state.board;
}

function renderBoard() {
  if (!state.board) return;
  const summary = state.board.summary;
  $('#kanban-total').textContent = summary.total;
  $('#kanban-wip').textContent = summary.by_status.in_progress;
  $('#kanban-blocked').textContent = summary.blocked;
  $('#kanban-done').textContent = summary.completed;
  fillBoardFilters();
  const board = $('#board');
  board.innerHTML = state.board.columns.map(col => {
    const limit = col.limit;
    const badgeClass = col.over_limit ? 'over'
      : (limit != null && col.count >= limit ? 'near' : '');
    const badge = limit == null ? `${col.count}`
      : `${col.count}/${limit}`;
    const tasks = col.tasks.filter(task => taskMatchesFilters(task));
    const cards = tasks.length
      ? tasks.map(t => taskCardHtml(t)).join('')
      : '<p class="empty">Drop tasks here</p>';
    return `<section class="column" data-col="${esc(col.key)}">
      <header class="column-head">
        <h3>${esc(col.label)}</h3>
        <span class="column-count">${tasks.length}</span>
        <span class="wip-badge ${badgeClass}" title="WIP: ${limit == null ? 'unlimited' : limit}">${badge}</span>
      </header>
      <div class="column-body">${cards}</div>
    </section>`;
  }).join('');
  bindDragDrop();
}

function fillBoardFilters() {
  const statusSelect = $('#filter-status');
  if (statusSelect.options.length === 1) {
    for (const status of TASK_STATUSES) statusSelect.add(new Option(TASK_STATUS_LABELS[status], status));
  }
  const assigneeSelect = $('#filter-assignee');
  const previous = assigneeSelect.value;
  const assignees = [...new Set(state.tasks.map(task => task.assignee.trim()).filter(Boolean))].sort();
  assigneeSelect.innerHTML = '<option value="">Anyone</option>' +
    assignees.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
  if (assignees.includes(previous)) assigneeSelect.value = previous;
  state.filters.assignee = assigneeSelect.value;
}

function taskMatchesFilters(task) {
  const search = state.filters.search.trim().toLocaleLowerCase();
  const searchable = `SP-${String(task.id).padStart(3, '0')} ${task.id} ${task.title} ${task.description}`.toLocaleLowerCase();
  return (!search || searchable.includes(search)) &&
    (!state.filters.status || task.status === state.filters.status) &&
    (!state.filters.priority || task.priority === state.filters.priority) &&
    (!state.filters.taskType || task.task_type === state.filters.taskType) &&
    (!state.filters.assignee || task.assignee === state.filters.assignee) &&
    (!state.filters.samples || task.is_sample);
}

function taskCardHtml(t) {
  const sprint = state.sprints.find(s => s.id === t.sprint_id);
  const overdue = t.due_date && t.status !== 'done' && t.due_date < new Date().toISOString().slice(0, 10);
  return `<article class="task-card p-${esc(t.priority)}${overdue ? ' overdue' : ''}"
      draggable="true" data-id="${t.id}" tabindex="0" role="button"
      title="${esc(t.description || t.title)}">
    <div class="task-card-heading"><span class="task-id">SP-${String(t.id).padStart(3, '0')}</span>${t.is_sample ? '<span class="sample-badge">SAMPLE</span>' : ''}</div>
    <strong>${esc(t.title)}</strong>
    ${t.blocker_reason ? `<div class="blocker-reason"><strong>Blocked:</strong> ${esc(t.blocker_reason)}</div>` : ''}
    <div class="meta">
      <span class="type-badge">${esc(t.task_type.replace('_', ' '))}</span>
      ${priorityBadge(t.priority)}
      ${t.assignee ? `<span>@${esc(t.assignee)}</span>` : ''}
      ${t.due_date ? `<span class="due">due ${esc(t.due_date)}</span>` : ''}
      ${sprint ? `<span class="badge b-sprint">${esc(sprint.name)}</span>` : ''}
    </div>
  </article>`;
}

function bindDragDrop() {
  $$('.task-card').forEach(card => {
    card.addEventListener('dragstart', ev => {
      ev.dataTransfer.setData('text/plain', card.dataset.id);
      ev.dataTransfer.effectAllowed = 'move';
      card.classList.add('dragging');
    });
    card.addEventListener('dragend', () => card.classList.remove('dragging'));
    card.addEventListener('click', () => openTaskModal(Number(card.dataset.id)));
    card.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openTaskModal(Number(card.dataset.id));
      }
    });
  });
  $$('.column').forEach(col => {
    col.addEventListener('dragover', ev => {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
      col.classList.add('drag-over');
    });
    col.addEventListener('dragleave', () => col.classList.remove('drag-over'));
    col.addEventListener('drop', async ev => {
      ev.preventDefault();
      col.classList.remove('drag-over');
      const id = Number(ev.dataTransfer.getData('text/plain'));
      const target = col.dataset.col;
      const task = state.tasks.find(t => t.id === id);
      if (!task || task.status === target) { renderBoard(); return; }
      if (target === 'blocked' && !task.blocker_reason) {
        openTaskModal(id);
        $('#task-form').elements.status.value = 'blocked';
        updateTaskConditionalFields();
        toast('Add a blocker reason to move this task to Blocked');
        return;
      }
      if (target === 'done' && (!task.acceptance_criteria || !task.completion_evidence)) {
        openTaskModal(id);
        $('#task-form').elements.status.value = 'done';
        updateTaskConditionalFields();
        toast('Verify acceptance criteria and record completion evidence');
        return;
      }
      try {
        await api('POST', `/api/tasks/${id}/move`, { status: target });
        toast(`Moved to ${target.replace('_', ' ')}`);
      } catch (err) {
        toast(err.message, true);
      }
      await reload();
      await loadBoard();
    });
  });
}

async function submitWipForm(ev) {
  ev.preventDefault();
  const body = {};
  for (const input of ev.target.elements) {
    if (input.name) body[input.name] = input.value === '' ? null : Number(input.value);
  }
  try {
    await api('PUT', '/api/wip-limits', body);
    toast('WIP limits saved');
    await loadBoard();
  } catch (err) {
    toast(err.message, true);
  }
}

function fillWipForm() {
  if (!state.board) return;
  const form = $('#wip-form');
  for (const [key, value] of Object.entries(state.board.wip_limits)) {
    if (form.elements[key]) form.elements[key].value = value == null ? '' : value;
  }
}

/* ---------------------------- task modal ------------------------------- */

function openTaskModal(id) {
  const form = $('#task-form');
  form.reset();
  form.elements.id.value = '';
  $('#task-story-select').innerHTML = '<option value="">None</option>' +
    state.stories.map(s => `<option value="${s.id}">${esc(s.title)}</option>`).join('');
  $('#task-sprint-select').innerHTML = sprintOptions(null);
  const task = id ? state.tasks.find(t => t.id === id) : null;
  if (task) {
    $('#task-modal-title').textContent = 'Edit task';
    form.elements.id.value = task.id;
    const completionDate = task.completed_at ? new Date(task.completed_at).toLocaleDateString() : 'Not completed';
    $('#task-record-meta').textContent = `SP-${String(task.id).padStart(3, '0')} · Created ${new Date(task.created_at).toLocaleDateString()} · ${completionDate}`;
    $('#task-record-meta').hidden = false;
    form.elements.title.value = task.title;
    form.elements.description.value = task.description || '';
    form.elements.acceptance_criteria.value = task.acceptance_criteria || '';
    form.elements.completion_evidence.value = task.completion_evidence || '';
    form.elements.task_type.value = task.task_type || 'feature';
    form.elements.blocker_reason.value = task.blocker_reason || '';
    form.elements.priority.value = task.priority;
    form.elements.status.value = task.status;
    form.elements.assignee.value = task.assignee || '';
    form.elements.due_date.value = task.due_date || '';
    form.elements.sprint_id.value = task.sprint_id == null ? '' : task.sprint_id;
    form.elements.story_id.value = task.story_id == null ? '' : task.story_id;
    $('#task-delete').hidden = false;
    $('#sample-task-note').hidden = !task.is_sample;
  } else {
    $('#task-modal-title').textContent = 'New task';
    $('#task-delete').hidden = true;
    $('#sample-task-note').hidden = true;
    $('#task-record-meta').hidden = true;
  }
  updateTaskConditionalFields();
  $('#task-modal').hidden = false;
  form.elements.title.focus();
}

function closeTaskModal() { $('#task-modal').hidden = true; }

function updateTaskConditionalFields() {
  const form = $('#task-form');
  const status = form.elements.status.value;
  $('#blocker-field').hidden = status !== 'blocked';
  $('#completion-field').hidden = status !== 'done';
  form.elements.blocker_reason.required = status === 'blocked';
  form.elements.acceptance_criteria.required = status === 'done';
  form.elements.completion_evidence.required = status === 'done';
}

async function submitTaskForm(ev) {
  ev.preventDefault();
  const form = ev.target;
  const id = form.elements.id.value;
  const body = {
    title: form.elements.title.value,
    description: form.elements.description.value,
    acceptance_criteria: form.elements.acceptance_criteria.value,
    completion_evidence: form.elements.completion_evidence.value,
    task_type: form.elements.task_type.value,
    blocker_reason: form.elements.blocker_reason.value,
    priority: form.elements.priority.value,
    status: form.elements.status.value,
    assignee: form.elements.assignee.value,
    due_date: form.elements.due_date.value || null,
    sprint_id: form.elements.sprint_id.value ? Number(form.elements.sprint_id.value) : null,
    story_id: form.elements.story_id.value ? Number(form.elements.story_id.value) : null,
  };
  try {
    if (id) await api('PUT', `/api/tasks/${id}`, body);
    else await api('POST', '/api/tasks', body);
    closeTaskModal();
    toast(id ? 'Task updated' : 'Task created');
    await reload();
    await loadBoard();
  } catch (err) {
    toast(err.message, true);
  }
}

async function deleteCurrentTask() {
  const id = $('#task-form').elements.id.value;
  if (!id) return;
  if (!await requestConfirmation('Delete this task?', 'This task will be permanently removed from the delivery plan.', $('#task-delete'))) return;
  try {
    await api('DELETE', `/api/tasks/${id}`);
    closeTaskModal();
    toast('Task deleted');
    await reload();
    await loadBoard();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ------------------------- scrum form handlers ------------------------- */

async function handleStorySubmit(ev) {
  ev.preventDefault();
  const f = ev.target;
  const body = {
    title: f.title.value,
    description: f.description.value,
    priority: f.priority.value,
    points: Number(f.points.value || 0),
    sprint_id: f.sprint_id.value ? Number(f.sprint_id.value) : null,
  };
  try {
    await api('POST', '/api/backlog', body);
    f.reset();
    toast('Story added');
    await reload();
  } catch (err) { toast(err.message, true); }
}

async function handleSprintSubmit(ev) {
  ev.preventDefault();
  const f = ev.target;
  try {
    await api('POST', '/api/sprints', {
      name: f.name.value, goal: f.goal.value,
      start_date: f.start_date.value, end_date: f.end_date.value,
    });
    f.reset();
    toast('Sprint created');
    await reload();
  } catch (err) { toast(err.message, true); }
}

function openRecordEditor(type, id, trigger) {
  const record = type === 'story'
    ? state.stories.find(item => item.id === id)
    : state.sprints.find(item => item.id === id);
  if (!record) return;
  const form = $('#record-form');
  const fields = $('#record-fields');
  const isStory = type === 'story';
  state.recordEditor = { type, id };
  state.recordEditorTrigger = trigger;
  $('#record-modal-kicker').textContent = isStory ? 'PRODUCT BACKLOG' : 'ITERATION MANAGEMENT';
  $('#record-modal-title').textContent = isStory ? 'Edit story' : 'Edit sprint';

  if (isStory) {
    const priorities = ['low', 'medium', 'high', 'critical'].map(priority =>
      `<option value="${priority}"${record.priority === priority ? ' selected' : ''}>${priority[0].toUpperCase()}${priority.slice(1)}</option>`).join('');
    fields.innerHTML = `
      <label>Story title <input name="title" maxlength="200" value="${esc(record.title)}" required></label>
      <label>Description <textarea name="description" maxlength="5000" rows="3">${esc(record.description || '')}</textarea></label>
      <div class="form-row">
        <label>Priority <select name="priority">${priorities}</select></label>
        <label>Story points <input name="points" type="number" min="0" max="1000" value="${record.points}" required></label>
      </div>
      <label>Sprint <select name="sprint_id">${sprintOptions(record.sprint_id)}</select></label>`;
  } else {
    const statuses = ['planned', 'active', 'completed'].map(status =>
      `<option value="${status}"${record.status === status ? ' selected' : ''}>${status[0].toUpperCase()}${status.slice(1)}</option>`).join('');
    fields.innerHTML = `
      <label>Sprint name <input name="name" maxlength="200" value="${esc(record.name)}" required></label>
      <label>Sprint goal <textarea name="goal" maxlength="5000" rows="2">${esc(record.goal || '')}</textarea></label>
      <div class="form-row">
        <label>Start date <input name="start_date" type="date" value="${esc(record.start_date)}" required></label>
        <label>End date <input name="end_date" type="date" value="${esc(record.end_date)}" required></label>
      </div>
      <label>Status <select name="status">${statuses}</select></label>`;
  }
  $('#record-modal').hidden = false;
  fields.querySelector('input, textarea, select').focus();
}

function closeRecordEditor() {
  $('#record-modal').hidden = true;
  const trigger = state.recordEditorTrigger;
  state.recordEditor = null;
  state.recordEditorTrigger = null;
  if (trigger && trigger.isConnected) trigger.focus();
}

function requestConfirmation(title, message, trigger) {
  $('#confirm-title').textContent = title;
  $('#confirm-message').textContent = message;
  state.confirmationTrigger = trigger;
  $('#confirm-modal').hidden = false;
  $('#confirm-accept').focus();
  return new Promise(resolve => { state.confirmationResolver = resolve; });
}

function resolveConfirmation(accepted) {
  if (!state.confirmationResolver) return;
  const resolve = state.confirmationResolver;
  const trigger = state.confirmationTrigger;
  state.confirmationResolver = null;
  state.confirmationTrigger = null;
  $('#confirm-modal').hidden = true;
  if (trigger && trigger.isConnected) trigger.focus();
  resolve(accepted);
}

async function submitRecordEditor(ev) {
  ev.preventDefault();
  if (!state.recordEditor) return;
  const { type, id } = state.recordEditor;
  const form = ev.target;
  const body = type === 'story'
    ? {
      title: form.elements.title.value,
      description: form.elements.description.value,
      priority: form.elements.priority.value,
      points: Number(form.elements.points.value),
      sprint_id: form.elements.sprint_id.value ? Number(form.elements.sprint_id.value) : null,
    }
    : {
      name: form.elements.name.value,
      goal: form.elements.goal.value,
      start_date: form.elements.start_date.value,
      end_date: form.elements.end_date.value,
      status: form.elements.status.value,
    };
  try {
    await api('PUT', type === 'story' ? `/api/backlog/${id}` : `/api/sprints/${id}`, body);
    closeRecordEditor();
    toast(type === 'story' ? 'Story updated' : 'Sprint updated');
    await reload();
    if (type === 'sprint') await loadBoard();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------------------- delegated click handlers ----------------------- */

async function onSprintListClick(ev) {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const sprint = state.sprints.find(s => s.id === id);
  if (!sprint) return;
  try {
    if (btn.dataset.act === 'sprint-toggle') {
      state.openSprint = state.openSprint === id ? null : id;
      renderSprints();
    } else if (btn.dataset.act === 'sprint-del') {
      if (!await requestConfirmation('Delete this sprint?', `"${sprint.name}" will be removed. Its tasks will move back to the backlog.`, btn)) return;
      await api('DELETE', `/api/sprints/${id}`);
      if (state.openSprint === id) state.openSprint = null;
      toast('Sprint deleted');
      await reload();
      await loadBoard();
    } else if (btn.dataset.act === 'sprint-edit') {
      openRecordEditor('sprint', id, btn);
    } else if (btn.dataset.act === 'sprint-save-notes') {
      const card = btn.closest('.sprint-detail');
      await api('PUT', `/api/sprints/${id}`, {
        review_notes: card.querySelector('[data-field="review_notes"]').value,
        retro_notes: card.querySelector('[data-field="retro_notes"]').value,
      });
      toast('Notes saved');
      await reload();
    }
  } catch (err) { toast(err.message, true); }
}

async function onStoryListClick(ev) {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const story = state.stories.find(s => s.id === id);
  if (!story) return;
  try {
    if (btn.dataset.act === 'story-del') {
      if (!await requestConfirmation('Delete this story?', `"${story.title}" will be permanently removed from the product backlog.`, btn)) return;
      await api('DELETE', `/api/backlog/${id}`);
      toast('Story deleted');
    } else if (btn.dataset.act === 'story-edit') {
      openRecordEditor('story', id, btn);
      return;
    }
    await reload();
  } catch (err) { toast(err.message, true); }
}

async function onTaskTableChange(ev) {
  const select = ev.target.closest('select[data-act="task-sprint"]');
  if (!select) return;
  const id = Number(select.closest('tr').dataset.id);
  try {
    await api('PUT', `/api/tasks/${id}`,
      { sprint_id: select.value ? Number(select.value) : null });
    toast('Sprint assignment updated');
    await reload();
    await loadBoard();
  } catch (err) { toast(err.message, true); }
}

async function onTaskTableClick(ev) {
  const btn = ev.target.closest('button[data-act="task-edit"]');
  if (!btn) return;
  const id = Number(btn.closest('tr').dataset.id);
  await loadBoard();
  openTaskModal(id);
}

/* -------------------------------- init --------------------------------- */

async function init() {
  bindNav();
  state.dispatchActivity = readDispatchActivity();
  renderOverview();
  $('#story-form').addEventListener('submit', handleStorySubmit);
  $('#sprint-form').addEventListener('submit', handleSprintSubmit);
  $('#record-form').addEventListener('submit', submitRecordEditor);
  $('#record-cancel').addEventListener('click', closeRecordEditor);
  $('#record-cancel-icon').addEventListener('click', closeRecordEditor);
  $('#record-modal').addEventListener('click', ev => {
    if (ev.target.id === 'record-modal') closeRecordEditor();
  });
  $('#confirm-accept').addEventListener('click', () => resolveConfirmation(true));
  $('#confirm-cancel').addEventListener('click', () => resolveConfirmation(false));
  $('#confirm-close').addEventListener('click', () => resolveConfirmation(false));
  $('#confirm-modal').addEventListener('click', ev => {
    if (ev.target.id === 'confirm-modal') resolveConfirmation(false);
  });
  $('#overview-refresh').addEventListener('click', refreshOverview);
  $('#dispatch-run').addEventListener('click', runTestDispatch);
  $('#task-form').addEventListener('submit', submitTaskForm);
  $('#task-form').elements.status.addEventListener('change', updateTaskConditionalFields);
  $('#task-cancel').addEventListener('click', closeTaskModal);
  $('#task-delete').addEventListener('click', deleteCurrentTask);
  $('#task-modal').addEventListener('click', ev => {
    if (ev.target.id === 'task-modal') closeTaskModal();
  });
  document.addEventListener('keydown', ev => {
    if (ev.key === 'Escape' && !$('#task-modal').hidden) closeTaskModal();
    if (ev.key === 'Escape' && !$('#record-modal').hidden) closeRecordEditor();
    if (ev.key === 'Escape' && !$('#confirm-modal').hidden) resolveConfirmation(false);
  });
  $('#sprint-list').addEventListener('click', onSprintListClick);
  $('#story-list').addEventListener('click', onStoryListClick);
  $('#task-table').addEventListener('change', onTaskTableChange);
  $('#task-table').addEventListener('click', onTaskTableClick);
  $('#scrum-new-task').addEventListener('click', () => openTaskModal(null));
  $('#kanban-new-task').addEventListener('click', () => openTaskModal(null));
  $('#wip-toggle').addEventListener('click', () => {
    const form = $('#wip-form');
    form.hidden = !form.hidden;
    if (!form.hidden) fillWipForm();
  });
  $('#wip-form').addEventListener('submit', submitWipForm);
  const applyFilters = () => {
    state.filters.search = $('#kanban-search').value;
    state.filters.status = $('#filter-status').value;
    state.filters.priority = $('#filter-priority').value;
    state.filters.taskType = $('#filter-type').value;
    state.filters.assignee = $('#filter-assignee').value;
    state.filters.samples = $('#filter-samples').checked;
    renderBoard();
  };
  ['input', 'change'].forEach(eventName => {
    $('#kanban-search').addEventListener(eventName, applyFilters);
    ['#filter-status', '#filter-priority', '#filter-type', '#filter-assignee', '#filter-samples']
      .forEach(selector => $(selector).addEventListener(eventName, applyFilters));
  });
  $('#clear-filters').addEventListener('click', () => {
    $('#kanban-search').value = '';
    $('#filter-status').value = '';
    $('#filter-priority').value = '';
    $('#filter-type').value = '';
    $('#filter-assignee').value = '';
    $('#filter-samples').checked = false;
    applyFilters();
  });
  $('#seed-samples').addEventListener('click', async () => {
    const button = $('#seed-samples');
    button.disabled = true;
    try {
      const result = await api('POST', '/api/tasks/samples');
      toast(result.created ? `${result.created} sample tasks added to Backlog` : 'Sample tasks are already loaded');
      await reload();
      await loadBoard();
    } catch (err) {
      toast(err.message, true);
    } finally {
      button.disabled = false;
    }
  });
  try {
    await Promise.all([reload(), refreshOverview()]);
    await loadBoard();
  } catch (err) {
    toast('Failed to load data: ' + err.message, true);
  }
  window.setInterval(() => {
    if (!document.hidden) refreshOverview();
  }, 15000);
}

document.addEventListener('DOMContentLoaded', init);
