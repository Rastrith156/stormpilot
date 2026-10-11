"""JSON API routes for the Scrum and Kanban modules.

Dispatch is intentionally tiny: the base server keeps its original routes
untouched and delegates everything under ``/api/`` here.  Responses follow
the project's plain-JSON style; errors carry ``{'error': message}``.
"""
import json
import re
from urllib.parse import parse_qs, urlparse

try:
    from app import db
except ImportError:  # script execution: python app/server.py
    import db

ROUTE_PATTERNS = [
    ('GET', re.compile(r'^/api/sprints$'), 'list_sprints'),
    ('POST', re.compile(r'^/api/sprints$'), 'create_sprint'),
    ('GET', re.compile(r'^/api/sprints/(\d+)$'), 'get_sprint'),
    ('PUT', re.compile(r'^/api/sprints/(\d+)$'), 'update_sprint'),
    ('DELETE', re.compile(r'^/api/sprints/(\d+)$'), 'delete_sprint'),
    ('GET', re.compile(r'^/api/backlog$'), 'list_stories'),
    ('POST', re.compile(r'^/api/backlog$'), 'create_story'),
    ('PUT', re.compile(r'^/api/backlog/(\d+)$'), 'update_story'),
    ('DELETE', re.compile(r'^/api/backlog/(\d+)$'), 'delete_story'),
    ('GET', re.compile(r'^/api/tasks$'), 'list_tasks'),
    ('POST', re.compile(r'^/api/tasks$'), 'create_task'),
    ('GET', re.compile(r'^/api/tasks/(\d+)$'), 'get_task'),
    ('PUT', re.compile(r'^/api/tasks/(\d+)$'), 'update_task'),
    ('DELETE', re.compile(r'^/api/tasks/(\d+)$'), 'delete_task'),
    ('POST', re.compile(r'^/api/tasks/(\d+)/move$'), 'move_task'),
    ('GET', re.compile(r'^/api/board$'), 'get_board'),
    ('GET', re.compile(r'^/api/wip-limits$'), 'get_wip_limits'),
    ('PUT', re.compile(r'^/api/wip-limits$'), 'set_wip_limits'),
]


def handle(handler, method, path):
    """Route an /api/ request. Returns the HTTP status sent, or None for
    paths outside the API (so the base server can handle them itself)."""
    if not path.startswith('/api/'):
        return None
    for verb, pattern, name in ROUTE_PATTERNS:
        if verb != method:
            continue
        match = pattern.match(path)
        if match:
            return _dispatch(handler, method, name, match.groups())
    # Known prefix but no route/method match.
    _send(handler, 404, {'error': 'not found'})
    return 404


def _dispatch(handler, method, name, groups):
    status = 500
    try:
        body = _read_body(handler) if method in ('POST', 'PUT') else None
        query = parse_qs(urlparse(handler.path).query)
        status, payload = _route(name, groups, body, query)
        _send(handler, status, payload)
    except db.ApiError as exc:
        status = exc.status
        _send(handler, status, {'error': exc.message})
    except (ValueError, json.JSONDecodeError):
        status = 400
        _send(handler, status, {'error': 'request body must be valid JSON'})
    except ConnectionError:
        # Client hung up (covers BrokenPipeError); nothing left to deliver.
        pass
    except Exception as exc:  # pragma: no cover - defensive log, matches server style
        status = 500
        print(json.dumps({'event': 'error', 'message': str(exc)}), flush=True)
        try:
            _send(handler, status, {'error': 'internal server error'})
        except ConnectionError:
            pass
    return status


def _route(name, groups, body, query):
    if name == 'list_sprints':
        return 200, db.list_sprints()
    if name == 'create_sprint':
        return 201, db.create_sprint(body or {})
    if name == 'get_sprint':
        return 200, db.get_sprint(int(groups[0]))
    if name == 'update_sprint':
        return 200, db.update_sprint(int(groups[0]), body or {})
    if name == 'delete_sprint':
        return 200, db.delete_sprint(int(groups[0]))
    if name == 'list_stories':
        return 200, db.list_stories()
    if name == 'create_story':
        return 201, db.create_story(body or {})
    if name == 'update_story':
        return 200, db.update_story(int(groups[0]), body or {})
    if name == 'delete_story':
        return 200, db.delete_story(int(groups[0]))
    if name == 'list_tasks':
        return 200, db.list_tasks(_task_filters(query))
    if name == 'create_task':
        return 201, db.create_task(body or {})
    if name == 'get_task':
        return 200, db.get_task(int(groups[0]))
    if name == 'update_task':
        return 200, db.update_task(int(groups[0]), body or {})
    if name == 'delete_task':
        return 200, db.delete_task(int(groups[0]))
    if name == 'move_task':
        if not isinstance(body, dict) or 'status' not in body:
            raise db.ApiError(400, 'status is required')
        return 200, db.move_task(int(groups[0]), body['status'])
    if name == 'get_board':
        return 200, _board_payload()
    if name == 'get_wip_limits':
        return 200, db.get_wip_limits()
    if name == 'set_wip_limits':
        return 200, db.set_wip_limits(body or {})
    raise db.ApiError(404, 'not found')  # pragma: no cover


def _board_payload():
    limits = db.get_wip_limits()
    columns = []
    for key in db.KANBAN_COLUMNS:
        tasks = db.list_tasks({'status': key})
        limit = limits.get(key)
        columns.append({
            'key': key,
            'label': db.COLUMN_LABELS[key],
            'limit': limit,
            'count': len(tasks),
            'over_limit': limit is not None and len(tasks) > limit,
            'tasks': tasks,
        })
    return {'columns': columns, 'wip_limits': limits}


def _task_filters(query):
    filters = {}
    for key in ('sprint_id', 'story_id', 'status'):
        if key not in query:
            continue
        raw = query[key][0]
        if key == 'status':
            if raw not in db.KANBAN_COLUMNS:
                raise db.ApiError(
                    400, f'status must be one of: {", ".join(db.KANBAN_COLUMNS)}')
            filters[key] = raw
        else:
            try:
                filters[key] = int(raw)
            except ValueError:
                raise db.ApiError(400, f'{key} must be an integer') from None
    return filters


def _read_body(handler):
    length = int(handler.headers.get('Content-Length') or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict):
        raise db.ApiError(400, 'request body must be a JSON object')
    return data


def _send(handler, status, payload):
    content = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)
