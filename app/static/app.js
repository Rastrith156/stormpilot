/* StormPilot PM — dependency-free UI for the Scrum and Kanban modules. */
'use strict';

const state = { sprints: [], stories: [], tasks: [], board: null, openSprint: null };
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

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
  $('#view-scrum').hidden = name !== 'scrum';
  $('#view-kanban').hidden = name !== 'kanban';
  location.hash = name === 'kanban' ? '#kanban' : '#scrum';
  if (name === 'kanban') renderBoard();
}

function bindNav() {
  $$('.tab').forEach(tab => tab.addEventListener('click', () => showView(tab.dataset.view)));
  if (location.hash === '#kanban') showView('kanban');
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
      const counts = ['todo', 'in_progress', 'testing', 'done']
        .map(k => `${k.replace('_', ' ')} ${st.by_status[k]}`).join(' · ');
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
  const board = $('#board');
  board.innerHTML = state.board.columns.map(col => {
    const limit = col.limit;
    const badgeClass = col.over_limit ? 'over'
      : (limit != null && col.count >= limit ? 'near' : '');
    const badge = limit == null ? `${col.count}`
      : `${col.count}/${limit}`;
    const cards = col.tasks.length
      ? col.tasks.map(t => taskCardHtml(t)).join('')
      : '<p class="empty">Drop tasks here</p>';
    return `<section class="column" data-col="${esc(col.key)}">
      <header class="column-head">
        <h3>${esc(col.label)}</h3>
        <span class="wip-badge ${badgeClass}" title="WIP: ${limit == null ? 'unlimited' : limit}">${badge}</span>
      </header>
      <div class="column-body">${cards}</div>
    </section>`;
  }).join('');
  bindDragDrop();
}

function taskCardHtml(t) {
  const sprint = state.sprints.find(s => s.id === t.sprint_id);
  const overdue = t.due_date && t.status !== 'done' && t.due_date < new Date().toISOString().slice(0, 10);
  return `<article class="task-card p-${esc(t.priority)}${overdue ? ' overdue' : ''}"
      draggable="true" data-id="${t.id}" tabindex="0" role="button"
      title="${esc(t.description || t.title)}">
    <strong>${esc(t.title)}</strong>
    <div class="meta">
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
    form.elements.title.value = task.title;
    form.elements.description.value = task.description || '';
    form.elements.priority.value = task.priority;
    form.elements.status.value = task.status;
    form.elements.assignee.value = task.assignee || '';
    form.elements.due_date.value = task.due_date || '';
    form.elements.sprint_id.value = task.sprint_id == null ? '' : task.sprint_id;
    form.elements.story_id.value = task.story_id == null ? '' : task.story_id;
    $('#task-delete').hidden = false;
  } else {
    $('#task-modal-title').textContent = 'New task';
    $('#task-delete').hidden = true;
  }
  $('#task-modal').hidden = false;
  form.elements.title.focus();
}

function closeTaskModal() { $('#task-modal').hidden = true; }

async function submitTaskForm(ev) {
  ev.preventDefault();
  const form = ev.target;
  const id = form.elements.id.value;
  const body = {
    title: form.elements.title.value,
    description: form.elements.description.value,
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
  if (!confirm('Delete this task?')) return;
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
      if (!confirm(`Delete sprint "${sprint.name}"? Its tasks move back to the backlog.`)) return;
      await api('DELETE', `/api/sprints/${id}`);
      if (state.openSprint === id) state.openSprint = null;
      toast('Sprint deleted');
      await reload();
      await loadBoard();
    } else if (btn.dataset.act === 'sprint-edit') {
      const name = prompt('Sprint name', sprint.name);
      if (name === null) return;
      const goal = prompt('Sprint goal', sprint.goal || '');
      if (goal === null) return;
      const start = prompt('Start date (YYYY-MM-DD)', sprint.start_date);
      if (start === null) return;
      const end = prompt('End date (YYYY-MM-DD)', sprint.end_date);
      if (end === null) return;
      const status = prompt('Status (planned / active / completed)', sprint.status);
      if (status === null) return;
      await api('PUT', `/api/sprints/${id}`,
        { name, goal, start_date: start, end_date: end, status });
      toast('Sprint updated');
      await reload();
      await loadBoard();
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
      if (!confirm(`Delete story "${story.title}"?`)) return;
      await api('DELETE', `/api/backlog/${id}`);
      toast('Story deleted');
    } else if (btn.dataset.act === 'story-edit') {
      const title = prompt('Story title', story.title);
      if (title === null) return;
      const points = prompt('Story points (0-1000)', story.points);
      if (points === null) return;
      const priority = prompt('Priority (low / medium / high / critical)', story.priority);
      if (priority === null) return;
      await api('PUT', `/api/backlog/${id}`,
        { title, points: Number(points), priority });
      toast('Story updated');
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
  $('#story-form').addEventListener('submit', handleStorySubmit);
  $('#sprint-form').addEventListener('submit', handleSprintSubmit);
  $('#task-form').addEventListener('submit', submitTaskForm);
  $('#task-cancel').addEventListener('click', closeTaskModal);
  $('#task-delete').addEventListener('click', deleteCurrentTask);
  $('#task-modal').addEventListener('click', ev => {
    if (ev.target.id === 'task-modal') closeTaskModal();
  });
  document.addEventListener('keydown', ev => {
    if (ev.key === 'Escape' && !$('#task-modal').hidden) closeTaskModal();
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
  try {
    await reload();
    await loadBoard();
  } catch (err) {
    toast('Failed to load data: ' + err.message, true);
  }
}

document.addEventListener('DOMContentLoaded', init);
