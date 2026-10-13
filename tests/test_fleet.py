import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import api
from app.server import Handler
from support import create_authenticated_user


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

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix='stormpilot_fleet_')
        self.original_data_dir = os.environ.get('APP_DATA_DIR')
        self.original_cookie_secure = os.environ.get('STORMPILOT_COOKIE_SECURE')
        os.environ['APP_DATA_DIR'] = self.temp_dir
        os.environ['STORMPILOT_COOKIE_SECURE'] = 'false'
        api.initialize_storage(force_reset=True)
        self.admin_session = create_authenticated_user()

    def tearDown(self):
        if self.original_data_dir is None:
            os.environ.pop('APP_DATA_DIR', None)
        else:
            os.environ['APP_DATA_DIR'] = self.original_data_dir
        if self.original_cookie_secure is None:
            os.environ.pop('STORMPILOT_COOKIE_SECURE', None)
        else:
            os.environ['STORMPILOT_COOKIE_SECURE'] = self.original_cookie_secure
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def request(self, method, path, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(self.base + path, data=data, method=method)
        req.add_header('Content-Type', 'application/json')
        for key, value in (headers or {}).items():
            req.add_header(key, value)
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
        api.create_delivery_record(
            'DEL-1001', 'SP-DRONE-01',
            {'lat': 43.65, 'lng': -79.38},
            {'lat': 43.651, 'lng': -79.381},
        )
        for status_name in ('ASSIGNED', 'PICKUP_CONFIRMED', 'IN_TRANSIT', 'ARRIVED'):
            api.update_delivery_status('DEL-1001', status_name)
        first = self.request('POST', '/api/delivery/confirm', {
            'delivery_id': 'DEL-1001',
            'drone_id': 'SP-DRONE-01',
            'confirmed_by': 'operator.demo',
            'handover_confirmed': True
        }, headers=self.admin_session)
        second = self.request('POST', '/api/delivery/confirm', {
            'delivery_id': 'DEL-1001',
            'drone_id': 'SP-DRONE-01',
            'confirmed_by': 'operator.demo',
            'handover_confirmed': True
        }, headers=self.admin_session)
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
