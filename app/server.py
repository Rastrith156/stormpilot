import json
import mimetypes
import os
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    from app import api
except ImportError:  # script execution: python app/server.py
    import api

COUNTS = Counter()
LOCK = threading.Lock()
START = time.monotonic()
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path.startswith('/api/'):
            status, payload = api.handle('GET', path, query=parsed_url.query)
            self._send_json(status, payload)
            with LOCK:
                COUNTS[('api', status or 500)] += 1
            return
        if path in ('/ui', '/ui/') or path == '/drone/connect' or path.startswith('/static/'):
            self._serve_static(path)
            return
        status = 200
        content_type = 'application/json'
        if path in ('/healthz', '/readyz'):
            body = {'status': 'ok'}
        elif path == '/':
            body = {'project': 'StormPilot', 'mission': 'Drone delivery operations',
                    'instance': os.getenv('HOSTNAME', 'local'),
                    'version': os.getenv('APP_VERSION', 'dev'),
                    'ui': '/ui'}
        elif path == '/dispatch':
            value = 1
            for i in range(100000):
                value = (value * 31 + i) % 1000000007
            body = {'status': 'dispatched', 'route_checksum': value}
        elif path == '/metrics':
            content_type = 'text/plain; version=0.0.4'
            with LOCK:
                snapshot = list(COUNTS.items())
            lines = ['# HELP stormpilot_requests_total Business HTTP requests.',
                     '# TYPE stormpilot_requests_total counter']
            for (route, code), count in sorted(snapshot):
                lines.append(f'stormpilot_requests_total{{route="{route}",status="{code}"}} {count}')
            lines += ['# HELP stormpilot_uptime_seconds Process uptime.',
                      '# TYPE stormpilot_uptime_seconds gauge',
                      f'stormpilot_uptime_seconds {time.monotonic() - START:.3f}']
            body = '\n'.join(lines) + '\n'
        else:
            status = 404
            body = {'error': 'not found'}
        if path not in ('/healthz', '/readyz', '/metrics'):
            route = path if path in ('/', '/dispatch') else 'other'
            with LOCK:
                COUNTS[(route, status)] += 1
        payload = (json.dumps(body) if isinstance(body, dict) else body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self._write_api('POST')

    def do_PUT(self):
        self._write_api('PUT')

    def do_DELETE(self):
        self._write_api('DELETE')

    def _write_api(self, method):
        path = urlparse(self.path).path
        if path.startswith('/api/'):
            parsed_url = urlparse(self.path)
            content_length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(content_length) if content_length > 0 else b''
            status, payload = api.handle(
                method, path, body,
                headers=dict(self.headers.items()),
                query=parsed_url.query,
            )
            self._send_json(status, payload)
            with LOCK:
                COUNTS[('api', status or 500)] += 1
            return
        payload = json.dumps({'error': 'not found'}).encode()
        self.send_response(404)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_static(self, path):
        if path in ('/ui', '/ui/'):
            name = 'index.html'
        elif path == '/drone/connect':
            name = 'connect.html'
        else:
            name = path[len('/static/'):]
        if '/' in name or '\\' in name or '..' in name or not name:
            status, content, ctype = 404, json.dumps({'error': 'not found'}).encode(), 'application/json'
        else:
            full = os.path.join(STATIC_DIR, name)
            if os.path.isfile(full):
                status = 200
                with open(full, 'rb') as fh:
                    content = fh.read()
                ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
            else:
                status, content, ctype = 404, json.dumps({'error': 'not found'}).encode(), 'application/json'
        if path not in ('/ui', '/ui/', '/drone/connect'):
            route = 'static'
            with LOCK:
                COUNTS[(route, status)] += 1
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt, *args):
        print(json.dumps({'event': 'http', 'message': fmt % args}), flush=True)


if __name__ == '__main__':
    ThreadingHTTPServer((os.getenv('HOST', '127.0.0.1'), int(os.getenv('PORT', '8080'))), Handler).serve_forever()