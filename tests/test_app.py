import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError
from app.server import Handler

class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def get(self, path):
        return urlopen(self.base + path, timeout=5).read().decode()

    def test_health(self):
        self.assertEqual(json.loads(self.get('/healthz'))['status'], 'ok')

    def test_readiness(self):
        self.assertEqual(json.loads(self.get('/readyz'))['status'], 'ok')

    def test_identity(self):
        self.assertEqual(json.loads(self.get('/'))['project'], 'StormPilot')

    def test_favicon_is_served_without_a_missing_resource(self):
        self.assertIn('<svg', self.get('/favicon.ico'))

    def test_dispatch(self):
        self.assertEqual(json.loads(self.get('/dispatch'))['status'], 'dispatched')

    def test_metrics(self):
        self.get('/dispatch')
        self.assertIn('stormpilot_requests_total{route="/dispatch",status="200"}', self.get('/metrics'))

    def test_unknown_route(self):
        with self.assertRaises(HTTPError) as ctx:
            self.get('/missing')
        self.assertEqual(ctx.exception.code, 404)

if __name__ == '__main__':
    unittest.main()
