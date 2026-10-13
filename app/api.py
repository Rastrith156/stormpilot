"""Read-only OpenProject API proxy for the StormPilot UI."""
import base64
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def handle(handler, method, path):
    if method != 'GET' or path != '/api/openproject':
        _send(handler, 404, {'error': 'not found'})
        return 404
    base = os.getenv('OPENPROJECT_URL', '').strip().rstrip('/')
    token = os.getenv('OPENPROJECT_API_TOKEN', '').strip()
    if not base or not token:
        _send(handler, 200, {'configured': False})
        return 200
    parsed = urlsplit(base)
    if (parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        _send(handler, 200, {'configured': False, 'message': 'OpenProject configuration is invalid.'})
        return 200
    try:
        projects = _get(base, token, '/api/v3/projects?filters=%5B%5D&offset=1&pageSize=100')
        work = _get(
            base, token,
            '/api/v3/work_packages?filters=%5B%5D&offset=1&pageSize=20'
            '&sortBy=%5B%5B%22updatedAt%22%2C%22desc%22%5D%5D')
        project_rows = _elements(projects)
        work_rows = _elements(work)
        result = {
            'configured': True,
            'instance_url': base,
            'projects': [_project(p, base) for p in project_rows],
            'work_packages': [_work_package(w, base) for w in work_rows],
            'counts': {
                'projects': _count(projects, project_rows),
                'work_packages': _count(work, work_rows),
            },
        }
        _send(handler, 200, result)
        return 200
    except HTTPError as exc:
        if exc.code in (401, 403):
            message = 'OpenProject rejected the API token. Check the token and its read permissions.'
        else:
            message = 'OpenProject could not complete the request. Check the instance and try again.'
        _send(handler, 502, {'error': message})
        return 502
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        _send(handler, 502, {'error': 'OpenProject is unavailable or returned an invalid response. Try again shortly.'})
        return 502


def _get(base, token, path):
    credentials = base64.b64encode(f'apikey:{token}'.encode()).decode()
    request = Request(base + path, headers={
        'Accept': 'application/hal+json, application/json',
        'Authorization': f'Basic {credentials}',
        'User-Agent': 'StormPilot/1.0',
    })
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _elements(payload):
    return payload.get('_embedded', {}).get('elements', [])


def _count(payload, rows):
    return payload.get('total', len(rows))


def _project(item, base):
    return {
        'id': item.get('id'), 'name': item.get('name', 'Untitled project'),
        'description': _plain(item.get('description')),
        'status': _plain(item.get('status') or item.get('_links', {}).get('status', {}).get('title')),
        'url': f"{base}/projects/{item.get('identifier') or item.get('id')}",
    }


def _work_package(item, base):
    links = item.get('_links', {})
    return {
        'id': item.get('id'), 'subject': item.get('subject', 'Untitled work package'),
        'type': _plain(links.get('type', {}).get('title')),
        'status': _plain(links.get('status', {}).get('title')),
        'priority': _plain(links.get('priority', {}).get('title')),
        'assignee': _plain(links.get('assignee', {}).get('title')),
        'project': _plain(links.get('project', {}).get('title')),
        'updated_at': item.get('updatedAt', ''),
        'url': f"{base}/work_packages/{item.get('id')}",
    }


def _plain(value):
    if isinstance(value, dict):
        return value.get('raw', '')
    return value or ''


def _send(handler, status, payload):
    content = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Content-Length', str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)
