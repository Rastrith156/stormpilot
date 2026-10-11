"""SQLite persistence for StormPilot's Scrum and Kanban modules.

Uses only the Python standard library, keeping the project dependency-free.
The database file lives at ``<project root>/data/stormpilot.db`` unless the
``STORMPILOT_DB`` environment variable points somewhere else.  All project
management writes go through this module so state survives restarts.
"""
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, 'data', 'stormpilot.db')

KANBAN_COLUMNS = ('backlog', 'todo', 'in_progress', 'code_review', 'testing', 'done', 'blocked')
COLUMN_LABELS = {
    'backlog': 'Backlog',
    'todo': 'To Do',
    'in_progress': 'In Progress',
    'code_review': 'Code Review',
    'testing': 'Testing',
    'done': 'Done',
    'blocked': 'Blocked',
}
PRIORITIES = ('low', 'medium', 'high', 'critical')
TASK_TYPES = ('feature', 'bug', 'security', 'testing', 'devops', 'documentation', 'infrastructure')
SPRINT_STATUSES = ('planned', 'active', 'completed')
DEFAULT_WIP_LIMITS = {
    'backlog': None, 'todo': None, 'in_progress': 3, 'code_review': 3,
    'testing': 2, 'done': None, 'blocked': None,
}
TASK_TRANSITIONS = {
    'backlog': ('todo', 'blocked'),
    'todo': ('in_progress', 'blocked'),
    'in_progress': ('code_review', 'blocked'),
    'code_review': ('testing', 'in_progress', 'blocked'),
    'testing': ('done', 'in_progress', 'blocked'),
    'done': ('testing',),
    'blocked': ('backlog', 'todo', 'in_progress', 'code_review', 'testing'),
}

MAX_TITLE = 200
MAX_TEXT = 5000
MAX_NOTES = 10000
MAX_ASSIGNEE = 100
MAX_POINTS = 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    review_notes TEXT NOT NULL DEFAULT '',
    retro_notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'medium',
    points INTEGER NOT NULL DEFAULT 0,
    sprint_id INTEGER REFERENCES sprints(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'medium',
    assignee TEXT NOT NULL DEFAULT '',
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'todo',
    task_type TEXT NOT NULL DEFAULT 'feature',
    acceptance_criteria TEXT NOT NULL DEFAULT '',
    completion_evidence TEXT NOT NULL DEFAULT '',
    blocker_reason TEXT NOT NULL DEFAULT '',
    is_sample INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    sprint_id INTEGER REFERENCES sprints(id) ON DELETE SET NULL,
    story_id INTEGER REFERENCES user_stories(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wip_limits (
    column_name TEXT PRIMARY KEY,
    limit_value INTEGER
);
"""


class ApiError(Exception):
    """Client-facing error carrying an HTTP status code."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def db_path():
    return os.environ.get('STORMPILOT_DB') or DEFAULT_DB_PATH


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _ensure_schema(conn):
    conn.executescript(SCHEMA)
    task_columns = {row['name'] for row in conn.execute('PRAGMA table_info(tasks)')}
    for column, definition in (
        ('task_type', "TEXT NOT NULL DEFAULT 'feature'"),
        ('acceptance_criteria', "TEXT NOT NULL DEFAULT ''"),
        ('completion_evidence', "TEXT NOT NULL DEFAULT ''"),
        ('blocker_reason', "TEXT NOT NULL DEFAULT ''"),
        ('is_sample', 'INTEGER NOT NULL DEFAULT 0'),
        ('completed_at', 'TEXT'),
    ):
        if column not in task_columns:
            conn.execute(f'ALTER TABLE tasks ADD COLUMN {column} {definition}')
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_sample_task_title '
        'ON tasks(title) WHERE is_sample = 1')
    wip_columns = {row['column_name'] for row in conn.execute('SELECT column_name FROM wip_limits')}
    for column in KANBAN_COLUMNS:
        if column not in wip_columns:
            conn.execute(
                'INSERT INTO wip_limits (column_name, limit_value) VALUES (?, ?)',
                (column, DEFAULT_WIP_LIMITS[column]),
            )
    for column, limit in DEFAULT_WIP_LIMITS.items():
        conn.execute(
            'INSERT OR IGNORE INTO wip_limits (column_name, limit_value) VALUES (?, ?)',
            (column, limit),
        )


_SCHEMA_LOCK = threading.Lock()
# Database paths whose schema and WAL mode are already initialised in this
# process.  Skipping the DDL on later connections keeps per-request lock
# churn low; the set is keyed by path so tests can swap STORMPILOT_DB.
_SCHEMA_READY = set()


@contextmanager
def open_db():
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if path in _SCHEMA_READY and not os.path.exists(path):
        # The file was deleted underneath us; re-run the DDL on next connect.
        with _SCHEMA_LOCK:
            _SCHEMA_READY.discard(path)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys = ON')
        with _SCHEMA_LOCK:
            if path not in _SCHEMA_READY:
                conn.execute('PRAGMA journal_mode = WAL')
                _ensure_schema(conn)
                conn.commit()
                _SCHEMA_READY.add(path)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Validation helpers shared by the API layer.
# ---------------------------------------------------------------------------

def require_str(data, field, *, required=True, default='', max_len=MAX_TEXT):
    if field not in data and not required:
        return default
    value = data.get(field, default)
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ApiError(400, f'{field} must be a string')
    value = value.strip()
    if required and not value:
        raise ApiError(400, f'{field} is required')
    if len(value) > max_len:
        raise ApiError(400, f'{field} must be at most {max_len} characters')
    return value


def validate_enum(data, field, allowed, default):
    if field not in data:
        return default
    value = data[field]
    if value not in allowed:
        raise ApiError(400, f'{field} must be one of: {", ".join(allowed)}')
    return value


def validate_date(data, field, *, required=True):
    if field not in data or data[field] in (None, ''):
        if required:
            raise ApiError(400, f'{field} is required (YYYY-MM-DD)')
        return None
    value = data[field]
    if not isinstance(value, str):
        raise ApiError(400, f'{field} must be a date in YYYY-MM-DD format')
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        raise ApiError(400, f'{field} must be a date in YYYY-MM-DD format') from None
    return value


def check_date_order(start, end):
    if start and end and end < start:
        raise ApiError(400, 'end_date must be on or after start_date')


def reject_unknown_fields(data, allowed):
    unknown = set(data) - set(allowed)
    if unknown:
        raise ApiError(400, f'unknown fields: {", ".join(sorted(unknown))}')


# ---------------------------------------------------------------------------
# Sprints
# ---------------------------------------------------------------------------

def create_sprint(data):
    reject_unknown_fields(
        data, ('name', 'goal', 'start_date', 'end_date', 'status',
               'review_notes', 'retro_notes'))
    name = require_str(data, 'name', max_len=MAX_TITLE)
    goal = require_str(data, 'goal', required=False, max_len=MAX_TEXT)
    start = validate_date(data, 'start_date')
    end = validate_date(data, 'end_date')
    check_date_order(start, end)
    status = validate_enum(data, 'status', SPRINT_STATUSES, 'planned')
    review = require_str(data, 'review_notes', required=False, max_len=MAX_NOTES)
    retro = require_str(data, 'retro_notes', required=False, max_len=MAX_NOTES)
    with open_db() as conn:
        cur = conn.execute(
            'INSERT INTO sprints (name, goal, start_date, end_date, status,'
            ' review_notes, retro_notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (name, goal, start, end, status, review, retro, now_iso()))
        new_id = cur.lastrowid
    # Read after the write transaction has committed: get_sprint() opens its
    # own connection and would otherwise not see the uncommitted row.
    return get_sprint(new_id)


def list_sprints():
    with open_db() as conn:
        rows = conn.execute('SELECT * FROM sprints ORDER BY start_date, id').fetchall()
        return [_sprint_with_stats(conn, row) for row in rows]


def get_sprint(sprint_id):
    with open_db() as conn:
        row = conn.execute('SELECT * FROM sprints WHERE id = ?', (sprint_id,)).fetchone()
        if row is None:
            raise ApiError(404, 'sprint not found')
        result = _sprint_with_stats(conn, row)
        stories = conn.execute(
            'SELECT * FROM user_stories WHERE sprint_id = ? ORDER BY id', (sprint_id,)).fetchall()
        tasks = conn.execute(
            'SELECT * FROM tasks WHERE sprint_id = ? ORDER BY id', (sprint_id,)).fetchall()
        result['stories'] = [dict(s) for s in stories]
        result['tasks'] = [dict(t) for t in tasks]
        return result


def _sprint_with_stats(conn, row):
    result = dict(row)
    total = conn.execute(
        'SELECT COUNT(*) FROM tasks WHERE sprint_id = ?', (row['id'],)).fetchone()[0]
    done = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE sprint_id = ? AND status = 'done'",
        (row['id'],)).fetchone()[0]
    by_status = {col: 0 for col in KANBAN_COLUMNS}
    for r in conn.execute(
            'SELECT status, COUNT(*) AS n FROM tasks WHERE sprint_id = ? GROUP BY status',
            (row['id'],)).fetchall():
        by_status[r['status']] = r['n']
    result['stats'] = {
        'total_tasks': total,
        'completed_tasks': done,
        'percent': round(100.0 * done / total, 1) if total else 0.0,
        'by_status': by_status,
    }
    return result


def update_sprint(sprint_id, data):
    reject_unknown_fields(
        data, ('name', 'goal', 'start_date', 'end_date', 'status',
               'review_notes', 'retro_notes'))
    with open_db() as conn:
        row = conn.execute('SELECT * FROM sprints WHERE id = ?', (sprint_id,)).fetchone()
        if row is None:
            raise ApiError(404, 'sprint not found')
        merged = dict(row)
        merged.update(data)
        name = require_str(merged, 'name', max_len=MAX_TITLE)
        goal = require_str(merged, 'goal', required=False, max_len=MAX_TEXT)
        start = validate_date(merged, 'start_date')
        end = validate_date(merged, 'end_date')
        check_date_order(start, end)
        status = validate_enum(merged, 'status', SPRINT_STATUSES, row['status'])
        review = require_str(merged, 'review_notes', required=False, max_len=MAX_NOTES)
        retro = require_str(merged, 'retro_notes', required=False, max_len=MAX_NOTES)
        conn.execute(
            'UPDATE sprints SET name = ?, goal = ?, start_date = ?, end_date = ?,'
            ' status = ?, review_notes = ?, retro_notes = ? WHERE id = ?',
            (name, goal, start, end, status, review, retro, sprint_id))
    return get_sprint(sprint_id)


def delete_sprint(sprint_id):
    with open_db() as conn:
        cur = conn.execute('DELETE FROM sprints WHERE id = ?', (sprint_id,))
        if cur.rowcount == 0:
            raise ApiError(404, 'sprint not found')
    return {'deleted': sprint_id}


# ---------------------------------------------------------------------------
# User stories (product backlog)
# ---------------------------------------------------------------------------

def create_story(data):
    reject_unknown_fields(data, ('title', 'description', 'priority', 'points', 'sprint_id'))
    title = require_str(data, 'title', max_len=MAX_TITLE)
    description = require_str(data, 'description', required=False, max_len=MAX_TEXT)
    priority = validate_enum(data, 'priority', PRIORITIES, 'medium')
    points = _validate_points(data.get('points', 0))
    with open_db() as conn:
        sprint_id = _resolve_ref(conn, data.get('sprint_id'), 'sprints', 'sprint')
        cur = conn.execute(
            'INSERT INTO user_stories (title, description, priority, points, sprint_id,'
            ' created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (title, description, priority, points, sprint_id, now_iso()))
        row = conn.execute('SELECT * FROM user_stories WHERE id = ?', (cur.lastrowid,)).fetchone()
        return dict(row)


def list_stories():
    with open_db() as conn:
        rows = conn.execute('SELECT * FROM user_stories ORDER BY id').fetchall()
        return [dict(r) for r in rows]


def _validate_points(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_POINTS:
        raise ApiError(400, f'points must be an integer between 0 and {MAX_POINTS}')
    return value


def update_story(story_id, data):
    reject_unknown_fields(data, ('title', 'description', 'priority', 'points', 'sprint_id'))
    with open_db() as conn:
        row = conn.execute('SELECT * FROM user_stories WHERE id = ?', (story_id,)).fetchone()
        if row is None:
            raise ApiError(404, 'story not found')
        merged = dict(row)
        merged.update(data)
        title = require_str(merged, 'title', max_len=MAX_TITLE)
        description = require_str(merged, 'description', required=False, max_len=MAX_TEXT)
        priority = validate_enum(merged, 'priority', PRIORITIES, row['priority'])
        points = _validate_points(merged.get('points', row['points']))
        sprint_id = _resolve_ref(conn, merged.get('sprint_id'), 'sprints', 'sprint')
        conn.execute(
            'UPDATE user_stories SET title = ?, description = ?, priority = ?, points = ?,'
            ' sprint_id = ? WHERE id = ?',
            (title, description, priority, points, sprint_id, story_id))
        updated = conn.execute('SELECT * FROM user_stories WHERE id = ?', (story_id,)).fetchone()
        return dict(updated)


def delete_story(story_id):
    with open_db() as conn:
        cur = conn.execute('DELETE FROM user_stories WHERE id = ?', (story_id,))
        if cur.rowcount == 0:
            raise ApiError(404, 'story not found')
    return {'deleted': story_id}


# ---------------------------------------------------------------------------
# Tasks (shared between Scrum and Kanban)
# ---------------------------------------------------------------------------

TASK_FIELDS = ('title', 'description', 'priority', 'assignee', 'due_date',
               'status', 'sprint_id', 'story_id', 'task_type',
               'acceptance_criteria', 'completion_evidence', 'blocker_reason', 'is_sample')


def create_task(data):
    reject_unknown_fields(data, TASK_FIELDS)
    title = require_str(data, 'title', max_len=MAX_TITLE)
    description = require_str(data, 'description', required=False, max_len=MAX_TEXT)
    priority = validate_enum(data, 'priority', PRIORITIES, 'medium')
    assignee = require_str(data, 'assignee', required=False, max_len=MAX_ASSIGNEE)
    due_date = validate_date(data, 'due_date', required=False)
    status = validate_enum(data, 'status', KANBAN_COLUMNS, 'todo')
    task_type = validate_enum(data, 'task_type', TASK_TYPES, 'feature')
    acceptance_criteria = require_str(data, 'acceptance_criteria', required=False, max_len=MAX_TEXT)
    completion_evidence = require_str(data, 'completion_evidence', required=False, max_len=MAX_TEXT)
    blocker_reason = require_str(data, 'blocker_reason', required=False, max_len=MAX_TEXT)
    if status == 'blocked' and not blocker_reason:
        raise ApiError(400, 'blocker_reason is required for blocked tasks')
    if status == 'done' and (not acceptance_criteria or not completion_evidence):
        raise ApiError(400, 'acceptance_criteria and completion_evidence are required for Done')
    is_sample = _validate_sample_flag(data.get('is_sample', False))
    with open_db() as conn:
        sprint_id = _resolve_ref(conn, data.get('sprint_id'), 'sprints', 'sprint')
        story_id = _resolve_ref(conn, data.get('story_id'), 'user_stories', 'story')
        _check_wip(conn, status, None)
        now = now_iso()
        cur = conn.execute(
            'INSERT INTO tasks (title, description, priority, assignee, due_date,'
            ' status, task_type, acceptance_criteria, completion_evidence, blocker_reason, is_sample,'
            ' completed_at, sprint_id, story_id, created_at, updated_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (title, description, priority, assignee, due_date, status, task_type,
             acceptance_criteria, completion_evidence, blocker_reason, int(is_sample),
             now if status == 'done' else None,
             sprint_id, story_id, now, now))
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (cur.lastrowid,)).fetchone()
        return dict(row)


def list_tasks(filters=None):
    filters = filters or {}
    query = 'SELECT * FROM tasks'
    clauses, params = [], []
    if 'sprint_id' in filters:
        clauses.append('sprint_id = ?')
        params.append(filters['sprint_id'])
    if 'story_id' in filters:
        clauses.append('story_id = ?')
        params.append(filters['story_id'])
    if 'status' in filters:
        clauses.append('status = ?')
        params.append(filters['status'])
    if 'priority' in filters:
        clauses.append('priority = ?')
        params.append(filters['priority'])
    if 'task_type' in filters:
        clauses.append('task_type = ?')
        params.append(filters['task_type'])
    if 'assignee' in filters:
        clauses.append('assignee = ?')
        params.append(filters['assignee'])
    if 'is_sample' in filters:
        clauses.append('is_sample = ?')
        params.append(int(filters['is_sample']))
    if 'task_id' in filters:
        clauses.append('id = ?')
        params.append(filters['task_id'])
    if 'search' in filters:
        clauses.append('(CAST(id AS TEXT) LIKE ? OR title LIKE ? OR description LIKE ?)')
        search = f"%{filters['search']}%"
        params.extend((search, search, search))
    if clauses:
        query += ' WHERE ' + ' AND '.join(clauses)
    query += ' ORDER BY id'
    with open_db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


SAMPLE_TASKS = (
    ('Analyze the existing StormPilot architecture', 'documentation'),
    ('Verify API authentication and authorization', 'security'),
    ('Test dispatch creation and retrieval', 'testing'),
    ('Validate PostgreSQL connectivity', 'infrastructure'),
    ('Run the existing automated tests', 'testing'),
    ('Build the Docker image', 'devops'),
    ('Validate Docker Compose services', 'testing'),
    ('Verify HTTPS gateway configuration', 'security'),
    ('Validate Prometheus metrics collection', 'testing'),
    ('Verify Grafana dashboard provisioning', 'testing'),
    ('Deploy the application in a local Kubernetes cluster', 'devops'),
    ('Verify Kubernetes health probes', 'testing'),
    ('Test Kubernetes NetworkPolicy enforcement', 'security'),
    ('Validate HPA prerequisites', 'infrastructure'),
    ('Review the GitHub Actions CI workflow', 'devops'),
    ('Validate dependency and secret scanning', 'security'),
    ('Verify container image publishing and signing', 'security'),
    ('Design the missing automatic CD integration', 'devops'),
    ('Connect the GitOps image update workflow', 'devops'),
    ('Validate Argo CD reconciliation', 'testing'),
    ('Evaluate canary deployment behavior', 'testing'),
    ('Integrate the Kanban board', 'feature'),
    ('Write project documentation', 'documentation'),
    ('Prepare presentation diagrams', 'documentation'),
    ('Record test results and project limitations', 'documentation'),
)


def seed_sample_tasks():
    """Explicitly add missing illustrative tasks; never overwrite user work."""
    created = []
    with open_db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        now = now_iso()
        for title, task_type in SAMPLE_TASKS:
            existing = conn.execute(
                'SELECT id FROM tasks WHERE is_sample = 1 AND title = ?', (title,)
            ).fetchone()
            if existing:
                continue
            cur = conn.execute(
                'INSERT OR IGNORE INTO tasks (title, description, priority, assignee, due_date,'
                ' status, task_type, acceptance_criteria, blocker_reason, is_sample,'
                ' completed_at, sprint_id, story_id, created_at, updated_at)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (title, 'Illustrative project task. Verify its scope and evidence before '
                 'changing its workflow status.', 'medium', '', None, 'backlog', task_type,
                 'Document the verification evidence and satisfy the agreed task scope.',
                 '', 1, None, None, None, now, now),
            )
            if cur.rowcount:
                created.append(cur.lastrowid)
    return {'created': len(created), 'tasks': [get_task(task_id) for task_id in created]}


def get_task(task_id):
    with open_db() as conn:
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise ApiError(404, 'task not found')
        return dict(row)


def update_task(task_id, data):
    reject_unknown_fields(data, TASK_FIELDS)
    with open_db() as conn:
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise ApiError(404, 'task not found')
        merged = dict(row)
        merged.update(data)
        title = require_str(merged, 'title', max_len=MAX_TITLE)
        description = require_str(merged, 'description', required=False, max_len=MAX_TEXT)
        priority = validate_enum(merged, 'priority', PRIORITIES, row['priority'])
        assignee = require_str(merged, 'assignee', required=False, max_len=MAX_ASSIGNEE)
        due_date = validate_date(merged, 'due_date', required=False)
        status = validate_enum(merged, 'status', KANBAN_COLUMNS, row['status'])
        task_type = validate_enum(merged, 'task_type', TASK_TYPES, row['task_type'])
        acceptance_criteria = require_str(
            merged, 'acceptance_criteria', required=False, max_len=MAX_TEXT)
        completion_evidence = require_str(
            merged, 'completion_evidence', required=False, max_len=MAX_TEXT)
        blocker_reason = require_str(merged, 'blocker_reason', required=False, max_len=MAX_TEXT)
        if status == 'blocked' and not blocker_reason:
            raise ApiError(400, 'blocker_reason is required for blocked tasks')
        if status != 'blocked':
            blocker_reason = ''
        if status == 'done' and (not acceptance_criteria or not completion_evidence):
            raise ApiError(400, 'acceptance_criteria and completion_evidence are required for Done')
        is_sample = _validate_sample_flag(
            merged['is_sample'] if 'is_sample' in data else bool(row['is_sample']))
        sprint_id = _resolve_ref(conn, merged.get('sprint_id'), 'sprints', 'sprint')
        story_id = _resolve_ref(conn, merged.get('story_id'), 'user_stories', 'story')
        if status != row['status']:
            _validate_task_transition(row['status'], status)
            _check_wip(conn, status, task_id)
        completed_at = row['completed_at']
        if status == 'done' and row['status'] != 'done':
            completed_at = now_iso()
        elif status != 'done':
            completed_at = None
        conn.execute(
            'UPDATE tasks SET title = ?, description = ?, priority = ?, assignee = ?,'
            ' due_date = ?, status = ?, task_type = ?, acceptance_criteria = ?,'
            ' completion_evidence = ?, blocker_reason = ?, is_sample = ?, completed_at = ?, sprint_id = ?,'
            ' story_id = ?, updated_at = ?'
            ' WHERE id = ?',
            (title, description, priority, assignee, due_date, status, task_type,
             acceptance_criteria, completion_evidence, blocker_reason, int(is_sample), completed_at,
             sprint_id, story_id, now_iso(), task_id))
        updated = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        return dict(updated)


def move_task(task_id, status):
    """Move a task between Kanban columns (drag & drop target)."""
    if status not in KANBAN_COLUMNS:
        raise ApiError(400, f'status must be one of: {", ".join(KANBAN_COLUMNS)}')
    with open_db() as conn:
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise ApiError(404, 'task not found')
        if status != row['status']:
            _validate_task_transition(row['status'], status)
            _check_wip(conn, status, task_id)
        if status == 'done' and (not row['acceptance_criteria'] or not row['completion_evidence']):
            raise ApiError(400, 'acceptance_criteria and completion_evidence are required for Done')
        completed_at = row['completed_at']
        if status == 'done' and row['status'] != 'done':
            completed_at = now_iso()
        elif status != 'done':
            completed_at = None
        blocker_reason = row['blocker_reason'] if status == 'blocked' else ''
        if status == 'blocked' and not blocker_reason:
            raise ApiError(400, 'blocker_reason is required for blocked tasks')
        conn.execute('UPDATE tasks SET status = ?, blocker_reason = ?, completed_at = ?,'
                     ' updated_at = ? WHERE id = ?',
                     (status, blocker_reason, completed_at, now_iso(), task_id))
        updated = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        return dict(updated)


def delete_task(task_id):
    with open_db() as conn:
        cur = conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        if cur.rowcount == 0:
            raise ApiError(404, 'task not found')
    return {'deleted': task_id}


# ---------------------------------------------------------------------------
# WIP limits
# ---------------------------------------------------------------------------

def get_wip_limits():
    with open_db() as conn:
        rows = conn.execute('SELECT column_name, limit_value FROM wip_limits').fetchall()
        limits = {col: DEFAULT_WIP_LIMITS[col] for col in KANBAN_COLUMNS}
        limits.update({r['column_name']: r['limit_value'] for r in rows})
        return limits


def set_wip_limits(data):
    if not isinstance(data, dict) or not data:
        raise ApiError(400, 'body must be an object with at least one column')
    for column in data:
        if column not in KANBAN_COLUMNS:
            raise ApiError(400, f'unknown column: {column}')
        value = data[column]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or value < 0):
            raise ApiError(400, f'limit for {column} must be a non-negative integer or null')
    with open_db() as conn:
        for column, value in data.items():
            conn.execute('UPDATE wip_limits SET limit_value = ? WHERE column_name = ?',
                         (value, column))
    return get_wip_limits()


def _check_wip(conn, status, exclude_task_id):
    row = conn.execute('SELECT limit_value FROM wip_limits WHERE column_name = ?',
                       (status,)).fetchone()
    limit = row['limit_value'] if row else None
    if limit is None:
        return
    query = 'SELECT COUNT(*) FROM tasks WHERE status = ?'
    params = [status]
    if exclude_task_id is not None:
        query += ' AND id != ?'
        params.append(exclude_task_id)
    count = conn.execute(query, params).fetchone()[0]
    if count >= limit:
        raise ApiError(409, f'WIP limit reached for {COLUMN_LABELS[status]} ({limit})')


def _validate_task_transition(current, target):
    if target not in TASK_TRANSITIONS.get(current, ()):
        allowed = ', '.join(COLUMN_LABELS[status] for status in TASK_TRANSITIONS.get(current, ()))
        raise ApiError(400, f'invalid task transition from {COLUMN_LABELS[current]} to '
                       f'{COLUMN_LABELS[target]}; allowed next stages: {allowed or "none"}')


def _validate_sample_flag(value):
    if not isinstance(value, bool):
        raise ApiError(400, 'is_sample must be a boolean')
    return value


def _resolve_ref(conn, value, table, label):
    if value in (None, ''):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, f'{label}_id must be an integer or null')
    exists = conn.execute(f'SELECT 1 FROM {table} WHERE id = ?', (value,)).fetchone()
    if exists is None:
        raise ApiError(400, f'{label} {value} does not exist')
    return value
