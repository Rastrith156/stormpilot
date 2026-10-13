import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.server import Handler


class FleetTests(unittest.TestCase):
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

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(self.base + path, data=data, method=method)
        req.add_header('Content-Type', 'application/json')
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_fleet_endpoint_exposes_four_demo_drones(self):
        status, payload = self.request('GET', '/api/fleet')
        self.assertEqual(status, 200)
        self.assertEqual(len(payload['drones']), 4)
        self.assertEqual({drone['drone_id'] for drone in payload['drones']}, {
            'SP-DRONE-01', 'SP-DRONE-02', 'SP-DRONE-03', 'SP-DRONE-04'
        })

    def test_invalid_gps_sample_is_rejected(self):
        status, payload = self.request('POST', '/api/device/telemetry', {
            'device_id': 'device-1',
            'drone_id': 'SP-DRONE-01',
            'lat': 91,
            'lng': 0,
            'accuracy': 10,
            'timestamp': '2026-10-13T12:00:00Z'
        })
        self.assertEqual(status, 400)
        self.assertIn('valid GPS', payload['error'])

    def test_delivery_completion_is_idempotent(self):
        first = self.request('POST', '/api/delivery/confirm', {
            'delivery_id': 'DEL-1001',
            'drone_id': 'SP-DRONE-01',
            'confirmed_by': 'operator.demo',
            'handover_confirmed': True
        })
        second = self.request('POST', '/api/delivery/confirm', {
            'delivery_id': 'DEL-1001',
            'drone_id': 'SP-DRONE-01',
            'confirmed_by': 'operator.demo',
            'handover_confirmed': True
        })
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(second[1]['status'], 'already_confirmed')

    def test_missing_smtp_credentials_warns_without_failing(self):
        with unittest.mock.patch.dict(os.environ, {'SMTP_HOST': '', 'SMTP_USERNAME': '', 'SMTP_APP_PASSWORD': ''}, clear=False):
            status, payload = self.request('POST', '/api/email/test', {'to': 'ops@example.com'})
        self.assertEqual(status, 200)
        self.assertIn('warning', payload['status'])


if __name__ == '__main__':
    unittest.main()
