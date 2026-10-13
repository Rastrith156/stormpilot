import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from app import api
from app import db as store
from app.server import Handler


class MilestoneTwoSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix='stormpilot_m2_')
        self.original_data_dir = os.environ.get('APP_DATA_DIR')
        self.original_admin_token = os.environ.get('STORMPILOT_ADMIN_TOKEN')
        os.environ['APP_DATA_DIR'] = self.temp_dir
        os.environ['STORMPILOT_ADMIN_TOKEN'] = 'milestone-three-test-admin-token-for-tests'
        api.initialize_storage(force_reset=True)

    def tearDown(self):
        if self.original_data_dir is None:
            os.environ.pop('APP_DATA_DIR', None)
        else:
            os.environ['APP_DATA_DIR'] = self.original_data_dir
        if self.original_admin_token is None:
            os.environ.pop('STORMPILOT_ADMIN_TOKEN', None)
        else:
            os.environ['STORMPILOT_ADMIN_TOKEN'] = self.original_admin_token
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @staticmethod
    def admin_headers():
        return {'Authorization': 'Bearer milestone-three-test-admin-token-for-tests'}

    def request(self, method, path, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(f'{self.base_url}{path}', data=data, method=method)
        req.add_header('Content-Type', 'application/json')
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_device_enrollment_and_authenticated_telemetry(self):
        status, payload = self.request(
            'POST', '/api/device/register',
            {'device_id': 'phone-001', 'drone_id': 'SP-DRONE-01'},
            self.admin_headers(),
        )
        self.assertEqual(status, 200)
        self.assertIn('token', payload)
        auth = f"Bearer {payload['token']}"
        status, payload = self.request(
            'POST',
            '/api/device/telemetry',
            {
                'device_id': 'phone-001',
                'drone_id': 'SP-DRONE-01',
                'lat': 43.6565,
                'lng': -79.3798,
                'accuracy': 12,
                'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            },
            {'Authorization': auth}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload['status'], 'accepted')

    def test_revoked_device_cannot_submit_telemetry(self):
        status, payload = self.request(
            'POST', '/api/device/register',
            {'device_id': 'phone-002', 'drone_id': 'SP-DRONE-02'},
            self.admin_headers(),
        )
        token = payload['token']
        status, payload = self.request(
            'DELETE', '/api/device/revoke',
            {'device_id': 'phone-002', 'reason': 'test revocation'},
            self.admin_headers(),
        )
        self.assertEqual(status, 200)
        status, payload = self.request(
            'POST',
            '/api/device/telemetry',
            {
                'device_id': 'phone-002',
                'drone_id': 'SP-DRONE-02',
                'lat': 43.6522,
                'lng': -79.3832,
                'accuracy': 8,
                'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            },
            {'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(status, 401)

    def test_delivery_transition_rejected_when_invalid(self):
        api.create_delivery_record('D-2201', 'SP-DRONE-03', {'lat': 43.6470, 'lng': -79.3910}, {'lat': 43.6480, 'lng': -79.3920}, planned_route_km=2.0)
        api.update_delivery_status('D-2201', 'ASSIGNED')
        with self.assertRaises(ValueError):
            api.update_delivery_status('D-2201', 'DELIVERED')

    def test_delivery_completion_queues_notification(self):
        api.create_delivery_record('D-2205', 'SP-DRONE-03', {'lat': 43.6470, 'lng': -79.3910}, {'lat': 43.6480, 'lng': -79.3920}, planned_route_km=2.0)
        for status in ('ASSIGNED', 'PICKUP_CONFIRMED', 'IN_TRANSIT', 'ARRIVED', 'DELIVERED'):
            api.update_delivery_status('D-2205', status)

        conn = store.get_connection()
        try:
            notification = conn.execute(
                'SELECT notification_type, delivery_id, state FROM notification_outbox WHERE delivery_id = ?',
                ('D-2205',),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(notification), ('DELIVERY_COMPLETED', 'D-2205', 'PENDING'))

    def test_handover_cannot_skip_arrival_or_change_drone(self):
        api.create_delivery_record('D-2207', 'SP-DRONE-03', {'lat': 43.6470, 'lng': -79.3910}, {'lat': 43.6480, 'lng': -79.3920}, planned_route_km=2.0)
        api.update_delivery_status('D-2207', 'ASSIGNED')
        status, _ = self.request('POST', '/api/delivery/confirm', {
            'delivery_id': 'D-2207', 'drone_id': 'SP-DRONE-03', 'handover_confirmed': True,
        }, self.admin_headers())
        self.assertEqual(status, 409)
        status, _ = self.request('POST', '/api/delivery/confirm', {
            'delivery_id': 'D-2207', 'drone_id': 'SP-DRONE-02', 'handover_confirmed': True,
        }, self.admin_headers())
        self.assertEqual(status, 409)

    def test_device_migration_upgrades_existing_schema(self):
        conn = store.get_connection()
        try:
            conn.execute('DROP TABLE device_credentials')
            conn.execute('DROP TABLE device_registrations')
            conn.execute("DELETE FROM schema_migrations WHERE name = '002_device_identity'")
            conn.commit()
        finally:
            conn.close()

        api.initialize_storage()

        self.assertIn('device_credentials', store.list_tables())
        self.assertIn('device_registrations', store.list_tables())

    def test_telemetry_tracks_distance_and_records_geofence_arrival(self):
        api.create_delivery_record(
            'D-2204', 'SP-DRONE-01', {'lat': 43.65, 'lng': -79.38},
            {'lat': 43.6505, 'lng': -79.38}, planned_route_km=1.0,
        )
        api.update_delivery_status('D-2204', 'ASSIGNED')
        api.update_delivery_status('D-2204', 'PICKUP_CONFIRMED')
        api.update_delivery_status('D-2204', 'IN_TRANSIT')
        _, registration = self.request(
            'POST', '/api/device/register',
            {'device_id': 'phone-distance', 'drone_id': 'SP-DRONE-01'},
            self.admin_headers(),
        )
        token = registration['token']
        headers = {'Authorization': f'Bearer {token}'}
        timestamp = datetime.now(timezone.utc)

        for offset, latitude in enumerate((43.65, 43.6501)):
            status, response = self.request(
                'POST', '/api/device/telemetry',
                {
                    'device_id': 'phone-distance', 'drone_id': 'SP-DRONE-01',
                    'lat': latitude, 'lng': -79.38, 'accuracy': 8,
                    'timestamp': (timestamp + timedelta(seconds=offset)).isoformat().replace('+00:00', 'Z'),
                },
                headers,
            )
            self.assertEqual(status, 200)

        self.assertGreater(response['distance_m'], 0)
        self.assertTrue(response['arrival_detected'])
        conn = store.get_connection()
        try:
            delivery = conn.execute('SELECT status, actual_observed_km, arrival_evidence FROM deliveries WHERE delivery_id = ?', ('D-2204',)).fetchone()
            samples = conn.execute('SELECT distance_m FROM telemetry_samples WHERE drone_id = ? AND status = ?', ('SP-DRONE-01', 'ACCEPTED')).fetchall()
        finally:
            conn.close()
        self.assertEqual(delivery['status'], 'ARRIVED')
        self.assertGreater(delivery['actual_observed_km'], 0)
        self.assertGreater(samples[-1]['distance_m'], 0)
        self.assertIn('reported_accuracy_m', json.loads(delivery['arrival_evidence']))

    def test_notification_worker_handles_unconfigured_smtp_gracefully(self):
        with mock.patch.dict(os.environ, {'SMTP_HOST': '', 'SMTP_USERNAME': '', 'SMTP_APP_PASSWORD': ''}, clear=False):
            item = api.enqueue_notification('DELIVERY_COMPLETED', 'D-2202', {'delivery_id': 'D-2202'}, delivery_id='D-2202')
            states = api.process_notification_outbox(max_jobs=5)
            self.assertIsInstance(states, list)
            self.assertEqual(item['delivery_id'], 'D-2202')
            self.assertIn({'id': item['id'], 'state': 'FAILED', 'delivery_id': 'D-2202', 'message': 'SMTP not configured'}, states)

    def test_notification_worker_sends_configured_email(self):
        env = {
            'SMTP_HOST': 'smtp.example.test',
            'SMTP_PORT': '587',
            'SMTP_USERNAME': 'ops@example.test',
            'SMTP_APP_PASSWORD': 'test-password',
            'DELIVERY_NOTIFICATION_TO': 'dispatch@example.test',
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(api.smtplib, 'SMTP') as smtp_class:
            smtp = smtp_class.return_value.__enter__.return_value
            item = api.enqueue_notification('DELIVERY_COMPLETED', 'D-2203', {'delivery_id': 'D-2203'}, delivery_id='D-2203')
            states = api.process_notification_outbox(max_jobs=5)

        self.assertEqual(states, [{'id': item['id'], 'state': 'SENT', 'delivery_id': 'D-2203'}])
        smtp.send_message.assert_called_once()
        conn = store.get_connection()
        try:
            row = conn.execute('SELECT state, sent_at FROM notification_outbox WHERE id = ?', (item['id'],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row['state'], 'SENT')
        self.assertIsNotNone(row['sent_at'])

    def test_notification_worker_schedules_retry_after_smtp_failure(self):
        env = {
            'SMTP_HOST': 'smtp.example.test',
            'SMTP_PORT': '587',
            'SMTP_USERNAME': 'ops@example.test',
            'SMTP_APP_PASSWORD': 'test-password',
            'DELIVERY_NOTIFICATION_TO': 'dispatch@example.test',
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(api.smtplib, 'SMTP') as smtp_class:
            smtp = smtp_class.return_value.__enter__.return_value
            smtp.send_message.side_effect = api.smtplib.SMTPException('temporary failure')
            item = api.enqueue_notification('DELIVERY_COMPLETED', 'D-2206', {'delivery_id': 'D-2206'}, delivery_id='D-2206')
            result = api.process_notification_outbox(max_jobs=5)
            self.assertEqual(api.process_notification_outbox(max_jobs=5), [])

        self.assertEqual(result[0]['state'], 'FAILED')
        conn = store.get_connection()
        try:
            row = conn.execute('SELECT retry_count, next_attempt_at FROM notification_outbox WHERE id = ?', (item['id'],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row['retry_count'], 1)
        self.assertIsNotNone(row['next_attempt_at'])


if __name__ == '__main__':
    unittest.main()
