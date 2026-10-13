"""WSGI application used by the bounded production server."""
import json
import mimetypes
import os
import time
from http import HTTPStatus

from app import api
from app.server import COUNTS, LOCK, START, STATIC_DIR


def application(environ, start_response):
    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/') or '/'
    status = 200
    headers = [('X-Content-Type-Options', 'nosniff'),
               ('X-Frame-Options', 'DENY'),
               ('Referrer-Policy', 'strict-origin-when-cross-origin')]

    if method != 'GET':
        status, payload, content_type = 405, {'error': 'method not allowed'}, 'application/json'
        headers.append(('Allow', 'GET'))
    elif path.startswith('/api/'):
        status, payload = api.handle(method, path)
        content_type = 'application/json'
        _count('api', status)
    elif path in ('/healthz', '/readyz'):
        payload, content_type = {'status': 'ok'}, 'application/json'
    elif path == '/':
        payload = {
            'project': 'StormPilot', 'mission': 'Resilient drone dispatch',
            'instance': os.getenv('HOSTNAME', 'local'),
            'version': os.getenv('APP_VERSION', 'dev'), 'ui': '/ui',
        }
        content_type = 'application/json'
        _count(path, status)
    elif path == '/dispatch':
        value = 1
        for i in range(100000):
            value = (value * 31 + i) % 1000000007
        payload, content_type = {'status': 'dispatched', 'route_checksum': value}, 'application/json'
        _count(path, status)
    elif path == '/metrics':
        status, payload, content_type = 200, _metrics(), 'text/plain; version=0.0.4'
    elif path in ('/ui', '/ui/') or path.startswith('/static/'):
        status, payload, content_type = _static(path)
        if path not in ('/ui', '/ui/'):
            _count('static', status)
    else:
        status, payload, content_type = 404, {'error': 'not found'}, 'application/json'
        _count('other', status)

    if content_type == 'application/json':
        body = json.dumps(payload).encode()
    elif isinstance(payload, str):
        body = payload.encode()
    else:
        body = payload
    headers.extend([('Content-Type', content_type), ('Content-Length', str(len(body)))])
    if path.startswith('/api/'):
        headers.append(('Cache-Control', 'no-store'))
    phrase = HTTPStatus(status).phrase
    start_response(f'{status} {phrase}', headers)
    return [body]


def _static(path):
    name = 'index.html' if path in ('/ui', '/ui/') else path[len('/static/'):]
    if '/' in name or '\\' in name or '..' in name or not name:
        return 404, {'error': 'not found'}, 'application/json'
    full_path = os.path.join(STATIC_DIR, name)
    if not os.path.isfile(full_path):
        return 404, {'error': 'not found'}, 'application/json'
    with open(full_path, 'rb') as file:
        content = file.read()
    return 200, content, mimetypes.guess_type(full_path)[0] or 'application/octet-stream'


def _count(route, status):
    with LOCK:
        COUNTS[(route, status)] += 1


def _metrics():
    with LOCK:
        snapshot = list(COUNTS.items())
    lines = ['# HELP stormpilot_requests_total Business HTTP requests.',
             '# TYPE stormpilot_requests_total counter']
    for (route, code), count in sorted(snapshot):
        safe_route = route.replace('"', '')
        lines.append(f'stormpilot_requests_total{{route="{safe_route}",status="{code}"}} {count}')
    lines.extend(['# HELP stormpilot_uptime_seconds Process uptime.',
                  '# TYPE stormpilot_uptime_seconds gauge',
                  f'stormpilot_uptime_seconds {time.monotonic() - START:.3f}'])
    return '\n'.join(lines) + '\n'
