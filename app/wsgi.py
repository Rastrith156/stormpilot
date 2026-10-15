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
    response_headers = []
    headers = [('X-Content-Type-Options', 'nosniff'),
               ('X-Frame-Options', 'DENY'),
               ('Referrer-Policy', 'strict-origin-when-cross-origin')]

    body = b''
    if method not in ('GET', 'HEAD'):
        try:
            length = int(environ.get('CONTENT_LENGTH', '0') or '0')
            body = environ.get('wsgi.input', b'').read(length) if length > 0 else b''
        except Exception:
            body = b''

    if method != 'GET' and method != 'POST' and method != 'PUT' and method != 'DELETE':
        status, payload, content_type = 405, {'error': 'method not allowed'}, 'application/json'
        headers.append(('Allow', 'GET, POST, PUT, DELETE'))
    elif path.startswith('/api/'):
        status, payload = api.handle(
            method,
            path,
            body,
            headers={
                'Authorization': environ.get('HTTP_AUTHORIZATION', ''),
                'Cookie': environ.get('HTTP_COOKIE', ''),
                'X-CSRF-Token': environ.get('HTTP_X_CSRF_TOKEN', ''),
                'X-Client-IP': environ.get('REMOTE_ADDR', ''),
            },
            query=environ.get('QUERY_STRING', ''),
        )
        response_headers = payload.pop('_response_headers', []) if isinstance(payload, dict) else []
        content_type = 'application/json'
        _count('api', status)
    elif path == '/healthz':
        payload, content_type = {'status': 'ok'}, 'application/json'
    elif path == '/readyz':
        status, payload = api.readiness_status()
        content_type = 'application/json'
    elif path == '/':
        payload = {
            'project': 'StormPilot', 'mission': 'Drone delivery operations',
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
    elif path in ('/ui', '/ui/') or path == '/drone/connect' or path.startswith('/static/'):
        status, payload, content_type = _static(path)
        if path not in ('/ui', '/ui/', '/drone/connect'):
            _count('static', status)
    else:
        status, payload, content_type = 404, {'error': 'not found'}, 'application/json'
        _count('other', status)

    if content_type == 'application/json':
        body_bytes = json.dumps(payload).encode()
    elif isinstance(payload, str):
        body_bytes = payload.encode()
    else:
        body_bytes = payload
    headers.extend([('Content-Type', content_type), ('Content-Length', str(len(body_bytes)))])
    if path.startswith('/api/'):
        headers.append(('Cache-Control', 'no-store'))
        headers.extend(response_headers)
    phrase = HTTPStatus(status).phrase
    start_response(f'{status} {phrase}', headers)
    return [body_bytes]


def _static(path):
    if path in ('/ui', '/ui/'):
        name = 'index.html'
    elif path == '/drone/connect':
        name = 'connect.html'
    else:
        name = path[len('/static/'):]
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
    return api.prometheus_metrics(snapshot, time.monotonic() - START)
