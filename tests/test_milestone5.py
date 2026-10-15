"""Milestone 5: live operations, reliability and end-to-end verification.

Every test runs against an isolated temporary database and synthetic test
credentials. No real secrets, phones, SMTP accounts or Google Maps keys are
used anywhere in this module.
"""
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import admin as admin_cli
from app import api, security
from app import db as store
from app.server import Handler
from support import create_authenticated_user

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app', 'static')

TEST_PASSWORD = 'm5-bootstrap-password-strong'
TEST_OPERATOR_PASSWORD = 'm5-operator-password-strong'
TEST_VIEWER_PASSWORD = 'm5-viewer-password-strong'
TEST_ROTATED_PASSWORD = 'm5-rotated-password-strong'
SMTP_SENTINEL = 'M5-SMTP-APP-PASSWORD-SENTINEL'
REDMINE_SENTINEL = 'M5-REDMINE-API-KEY-SENTINEL'


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


class Milestone5TestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix='stormpilot_m5_')
        self._saved_env = {}
        self.set_env('APP_DATA_DIR', self.temp_dir)
        self.set_env('STORMPILOT_COOKIE_SECURE', 'false')
        api.initialize_storage(force_reset=True)

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def set_env(self, key, value):
        self._saved_env.setdefault(key, os.environ.get(key))
        os.environ[key] = value

    def request(self, method, path, payload=None, headers=None, raw=False, full=False):
        request = Request(
            f'{self.base_url}{path}',
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
        )
        request.add_header('Content-Type', 'application/json')
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urlopen(request, timeout=5) as response:
                raw_body = response.read()
                if raw or full:
                    return response.status, raw_body, response.headers
                return response.status, json.loads(raw_body) if raw_body else {}
        except HTTPError as error:
            raw_body = error.read()
            if raw or full:
                return error.code, raw_body, error.headers
            return error.code, json.loads(raw_body) if raw_body else {}

    def login(self, username, password):
        status, _, response_headers = self.request(
            'POST', '/api/auth/login',
            {'username': username, 'password': password},
            full=True,
        )
        self.assertEqual(status, 200)
        cookies = {}
        for value in response_headers.get_all('Set-Cookie') or []:
            key, _, rest = value.partition('=')
            cookies[key] = rest.split(';', 1)[0]
        csrf = cookies['stormpilot_csrf']
        return {
            'Cookie': f"stormpilot_session={cookies['stormpilot_session']}; stormpilot_csrf={csrf}",
            'X-CSRF-Token': csrf,
        }

    def register_device(self, admin_headers, device_id, drone_id):
        status, payload = self.request(
            'POST', '/api/device/register',
            {'device_id': device_id, 'drone_id': drone_id},
            admin_headers,
        )
        self.assertEqual(status, 200, payload)
        return payload

    def send_telemetry(self, device_id, drone_id, token, latitude, longitude, timestamp, accuracy=8):
        return self.request(
            'POST', '/api/device/telemetry',
            {
                'device_id': device_id,
                'drone_id': drone_id,
                'lat': latitude,
                'lng': longitude,
                'accuracy': accuracy,
                'timestamp': utc_text(timestamp) if isinstance(timestamp, datetime) else timestamp,
            },
            {'Authorization': f'Bearer {token}'},
        )


class AuthenticationLifecycleTests(Milestone5TestCase):
    def test_bootstrap_cli_creates_first_administrator_and_refuses_a_second(self):
        stdin = io.StringIO(f'{TEST_PASSWORD}\n')
        with mock.patch('sys.stdin', stdin), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            result = admin_cli.main(['bootstrap', '--username', 'cli.admin', '--password-stdin'])
        self.assertEqual(result, 0)
        conn = store.get_connection()
        try:
            row = conn.execute(
                "SELECT username, role, password_hash, enabled FROM admin_users WHERE username = 'cli.admin'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row['role'], 'ADMIN')
        self.assertTrue(row['enabled'])
        self.assertNotIn(TEST_PASSWORD, row['password_hash'])

        with mock.patch('sys.stdin', io.StringIO('second-password-attempt\n')), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                admin_cli.main(['bootstrap', '--username', 'second.admin', '--password-stdin'])
        self.assertEqual(ctx.exception.code, 2)

    def test_role_authorization_is_enforced_on_the_server(self):
        admin = create_authenticated_user(role='ADMIN', username='m5.admin')
        status, _ = self.request(
            'POST', '/api/admin/users',
            {'username': 'm5.operator', 'password': TEST_OPERATOR_PASSWORD, 'role': 'OPERATOR'},
            admin,
        )
        self.assertEqual(status, 201)
        status, _ = self.request(
            'POST', '/api/admin/users',
            {'username': 'm5.viewer', 'password': TEST_VIEWER_PASSWORD, 'role': 'VIEWER'},
            admin,
        )
        self.assertEqual(status, 201)
        operator = self.login('m5.operator', TEST_OPERATOR_PASSWORD)
        viewer = self.login('m5.viewer', TEST_VIEWER_PASSWORD)

        status, _ = self.request('GET', '/api/admin/users', headers=admin)
        self.assertEqual(status, 200)
        status, _ = self.request('GET', '/api/admin/users', headers=operator)
        self.assertEqual(status, 403)
        status, _ = self.request('GET', '/api/admin/users', headers=viewer)
        self.assertEqual(status, 403)

        status, scenario = self.request(
            'POST', '/api/scenarios',
            {'scenario_type': 'drone_offline', 'drone_id': 'SP-DRONE-01'},
            operator,
        )
        self.assertEqual(status, 201, scenario)
        status, _ = self.request(
            'POST', '/api/scenarios',
            {'scenario_type': 'drone_offline', 'drone_id': 'SP-DRONE-01'},
            viewer,
        )
        self.assertEqual(status, 403)
        status, _ = self.request(
            'POST', '/api/device/register',
            {'device_id': 'm5-operator-denied', 'drone_id': 'SP-DRONE-01'},
            operator,
        )
        self.assertEqual(status, 403)
        status, _ = self.request(
            'POST', '/api/delivery/confirm',
            {'delivery_id': 'any', 'drone_id': 'SP-DRONE-01', 'handover_confirmed': True},
            viewer,
        )
        self.assertEqual(status, 403)
        status, _ = self.request(
            'PUT', '/api/twin/config',
            {'corridors': [], 'zones': [], 'restrictions': [], 'scenarios': []},
            viewer,
        )
        self.assertEqual(status, 403)
        status, replay = self.request(
            'GET', '/api/replay?drone_id=SP-DRONE-01', headers=viewer,
        )
        self.assertEqual(status, 200, replay)

        for path in ('/api/missions/optimize', '/api/delivery/confirm', '/api/device/register'):
            status, _ = self.request('POST', path, {}, None)
            self.assertEqual(status, 401, path)
        status, _ = self.request('GET', '/api/admin/users', headers=None)
        self.assertEqual(status, 401)

    def test_credential_rotation_revokes_sessions_and_requires_new_password(self):
        admin = create_authenticated_user(role='ADMIN', username='m5.rotate')
        status, payload = self.request('GET', '/api/admin/users', headers=admin)
        self.assertEqual(status, 200)
        user_id = next(user['user_id'] for user in payload['users'] if user['username'] == 'm5.rotate')
        status, updated = self.request(
            'PUT', f'/api/admin/users/{user_id}',
            {'password': TEST_ROTATED_PASSWORD},
            admin,
        )
        self.assertEqual(status, 200, updated)
        status, _ = self.request('GET', '/api/auth/session', headers=admin)
        self.assertEqual(status, 401, 'Password rotation must revoke existing sessions.')
        status, _ = self.request(
            'POST', '/api/auth/login',
            {'username': 'm5.rotate', 'password': 'test-only-password-with-at-least-24-chars'},
        )
        self.assertEqual(status, 401)
        status, result = self.request(
            'POST', '/api/auth/login',
            {'username': 'm5.rotate', 'password': TEST_ROTATED_PASSWORD},
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result['user']['role'], 'ADMIN')

    def test_secrets_never_reach_logs_responses_or_frontend_bundles(self):
        self.set_env('SMTP_HOST', 'smtp.example.test')
        self.set_env('SMTP_USERNAME', 'bot@example.test')
        self.set_env('SMTP_APP_PASSWORD', SMTP_SENTINEL)
        self.set_env('REDMINE_URL', 'https://redmine.example.test')
        self.set_env('REDMINE_API_KEY', REDMINE_SENTINEL)
        self.set_env('GOOGLE_MAPS_API_KEY', '')

        captured = io.StringIO()
        with redirect_stdout(captured):
            status, _, response_headers = self.request(
                'POST', '/api/auth/login',
                {'username': 'm5.secret', 'password': TEST_PASSWORD},
                full=True,
            )
            if status != 200:
                created = security.create_user('m5.secret', TEST_PASSWORD, 'ADMIN')
                self.assertEqual(created[0], 201, created)
                status, _, response_headers = self.request(
                    'POST', '/api/auth/login',
                    {'username': 'm5.secret', 'password': TEST_PASSWORD},
                    full=True,
                )
            self.assertEqual(status, 200)
            cookie_parts = [value.split(';', 1)[0] for value in response_headers.get_all('Set-Cookie')]
            csrf = next(part.split('=', 1)[1] for part in cookie_parts if part.startswith('stormpilot_csrf='))
            session_headers = {'Cookie': '; '.join(cookie_parts), 'X-CSRF-Token': csrf}
            status, registered = self.request(
                'POST', '/api/device/register',
                {'device_id': 'm5-secret-device', 'drone_id': 'SP-DRONE-01'},
                session_headers,
            )
            self.assertEqual(status, 200, registered)
            for path in ('/', '/api/config', '/api/fleet', '/api/twin', '/api/traffic',
                         '/readyz', '/api/auth/session', '/api/admin/users'):
                status, body, _ = self.request('GET', path, headers=session_headers, raw=True)
                self.assertEqual(status, 200, path)
                self.assertNotIn(SMTP_SENTINEL.encode(), body, path)
                self.assertNotIn(REDMINE_SENTINEL.encode(), body, path)
                self.assertNotIn(TEST_PASSWORD.encode(), body, path)
            status, metrics_body, _ = self.request('GET', '/metrics', raw=True)
            self.assertEqual(status, 200)
            self.assertNotIn(SMTP_SENTINEL.encode(), metrics_body)
            self.assertNotIn(REDMINE_SENTINEL.encode(), metrics_body)
            self.assertNotIn(registered['token'].encode(), metrics_body)
            status, users = self.request('GET', '/api/admin/users', headers=session_headers)
            self.assertEqual(status, 200)
            self.assertNotIn('password_hash', json.dumps(users))
            self.assertNotIn('password_salt', json.dumps(users))

        log_text = captured.getvalue()
        self.assertNotIn(TEST_PASSWORD, log_text)
        self.assertNotIn(registered['token'], log_text)
        session_cookie = session_headers['Cookie'].split('stormpilot_session=')[1].split(';')[0]
        self.assertNotIn(session_cookie, log_text)
        self.assertNotIn(SMTP_SENTINEL, log_text)
        self.assertNotIn(REDMINE_SENTINEL, log_text)

        google_key_pattern = re.compile(r'AIza[0-9A-Za-z_\-]{35}')
        for name in os.listdir(STATIC_DIR):
            if not name.endswith(('.js', '.html', '.css')):
                continue
            with open(os.path.join(STATIC_DIR, name), encoding='utf-8') as handle:
                text = handle.read()
            self.assertIsNone(google_key_pattern.search(text), f'embedded Google key in {name}')
            self.assertNotIn(SMTP_SENTINEL, text, name)
            self.assertNotIn(REDMINE_SENTINEL, text, name)
            self.assertNotIn(registered['token'], text, name)


class PhoneGpsDeliveryWorkflowTests(Milestone5TestCase):
    def test_enrollment_is_admin_authorized_unique_per_drone_and_recoverable(self):
        status, _ = self.request(
            'POST', '/api/device/register',
            {'device_id': 'm5-shared-device', 'drone_id': 'SP-DRONE-01'},
            None,
        )
        self.assertEqual(status, 401)
        admin = create_authenticated_user(role='ADMIN', username='m5.gps.admin')
        first = self.register_device(admin, 'm5-shared-device', 'SP-DRONE-01')
        status, conflict = self.request(
            'POST', '/api/device/register',
            {'device_id': 'm5-shared-device', 'drone_id': 'SP-DRONE-02'},
            admin,
        )
        self.assertEqual(status, 409, conflict)
        self.assertIn('already assigned', conflict['error'])

        now = datetime.now(timezone.utc)
        status, _ = self.send_telemetry(
            'm5-shared-device', 'SP-DRONE-01', first['token'], 43.6400, -79.4000, now,
        )
        self.assertEqual(status, 200)
        status, _ = self.request(
            'DELETE', '/api/device/revoke',
            {'device_id': 'm5-shared-device'},
            admin,
        )
        self.assertEqual(status, 200)
        status, _ = self.send_telemetry(
            'm5-shared-device', 'SP-DRONE-01', first['token'], 43.6401, -79.4001,
            now + timedelta(seconds=5),
        )
        self.assertEqual(status, 401, 'Revoked device credentials must be rejected.')

        second = self.register_device(admin, 'm5-shared-device', 'SP-DRONE-02')
        status, accepted = self.send_telemetry(
            'm5-shared-device', 'SP-DRONE-02', second['token'], 43.6500, -79.4100,
            datetime.now(timezone.utc),
        )
        self.assertEqual(status, 200, accepted)
        status, mismatch = self.send_telemetry(
            'm5-shared-device', 'SP-DRONE-01', second['token'], 43.6501, -79.4101,
            datetime.now(timezone.utc) + timedelta(seconds=5),
        )
        self.assertEqual(status, 401, 'A device token must only authorize its own drone.')

    def test_telemetry_rejects_invalid_duplicate_out_of_order_implausible_and_stale(self):
        admin = create_authenticated_user(role='ADMIN', username='m5.tl.admin')
        device = self.register_device(admin, 'm5-quality-device', 'SP-DRONE-03')
        token = device['token']
        base = datetime.now(timezone.utc) - timedelta(seconds=60)

        status, payload = self.send_telemetry(
            'm5-quality-device', 'SP-DRONE-03', token, 200.0, -79.4000, base,
        )
        self.assertEqual(status, 400, payload)
        status, payload = self.send_telemetry(
            'm5-quality-device', 'SP-DRONE-03', token, 43.6400, -79.4000, base, accuracy=-1,
        )
        self.assertEqual(status, 400, payload)
        status, payload = self.request(
            'POST', '/api/device/telemetry',
            {'device_id': 'm5-quality-device', 'drone_id': 'SP-DRONE-03',
             'lat': 43.64, 'lng': -79.4, 'accuracy': 8, 'timestamp': 'not-a-timestamp'},
            {'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(status, 400, payload)

        status, accepted = self.send_telemetry(
            'm5-quality-device', 'SP-DRONE-03', token, 43.6400, -79.4000, base,
        )
        self.assertEqual(status, 200, accepted)
        status, duplicate = self.send_telemetry(
            'm5-quality-device', 'SP-DRONE-03', token, 43.6400, -79.4000, base,
        )
        self.assertEqual(status, 400, duplicate)
        self.assertIn('duplicate', duplicate['error'])
        status, out_of_order = self.send_telemetry(
            'm5-quality-device', 'SP-DRONE-03', token, 43.6401, -79.4001,
            base - timedelta(seconds=5),
        )
        self.assertEqual(status, 400, out_of_order)
        status, implausible = self.send_telemetry(
            'm5-quality-device', 'SP-DRONE-03', token, 43.7400, -79.4000,
            base + timedelta(seconds=10), accuracy=10,
        )
        self.assertEqual(status, 400, implausible)
        self.assertIn('implausible', implausible['error'])
        status, stale = self.send_telemetry(
            'm5-quality-device', 'SP-DRONE-03', token, 43.6402, -79.4002,
            datetime.now(timezone.utc) - timedelta(seconds=600),
        )
        self.assertEqual(status, 400, stale)
        self.assertIn('stale', stale['error'])
        status, future = self.send_telemetry(
            'm5-quality-device', 'SP-DRONE-03', token, 43.6403, -79.4003,
            datetime.now(timezone.utc) + timedelta(seconds=120),
        )
        self.assertEqual(status, 400, future)
        self.assertIn('future', future['error'])

        conn = store.get_connection()
        try:
            reasons = {
                row['rejection_reason'] for row in conn.execute(
                    "SELECT rejection_reason FROM telemetry_samples WHERE status = 'REJECTED'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertIn('duplicate_or_out_of_order', reasons)
        self.assertIn('implausible_speed', reasons)

    def test_telemetry_submissions_are_rate_limited_per_device(self):
        admin = create_authenticated_user(role='ADMIN', username='m5.rate.admin')
        device = self.register_device(admin, 'm5-rate-device', 'SP-DRONE-04')
        base = datetime.now(timezone.utc) - timedelta(seconds=60)
        statuses = []
        for index in range(11):
            status, payload = self.send_telemetry(
                'm5-rate-device', 'SP-DRONE-04', device['token'],
                43.6400 + index * 0.00001, -79.4000, base + timedelta(seconds=index),
            )
            statuses.append(status)
        self.assertEqual(statuses[:10], [200] * 10, statuses)
        self.assertEqual(statuses[10], 429)

    def test_connection_states_and_distance_accumulate_without_double_counting(self):
        admin = create_authenticated_user(role='ADMIN', username='m5.state.admin')
        fleet_status, fleet = self.request('GET', '/api/fleet')
        self.assertEqual(fleet_status, 200)
        offline = next(item for item in fleet['drones'] if item['drone_id'] == 'SP-DRONE-04')
        self.assertEqual(offline['position_state'], 'OFFLINE')
        self.assertIsNone(offline['position'])

        device = self.register_device(admin, 'm5-state-device', 'SP-DRONE-04')
        base = datetime.now(timezone.utc) - timedelta(seconds=90)
        status, _ = self.send_telemetry(
            'm5-state-device', 'SP-DRONE-04', device['token'], 43.6400, -79.4000, base,
        )
        self.assertEqual(status, 200)
        status, _ = self.send_telemetry(
            'm5-state-device', 'SP-DRONE-04', device['token'], 43.6410, -79.4000,
            base + timedelta(seconds=30),
        )
        self.assertEqual(status, 200)
        expected_leg_m = api._haversine(43.6400, -79.4000, 43.6410, -79.4000)
        status, fleet = self.request('GET', '/api/fleet')
        self.assertEqual(status, 200)
        drone = next(item for item in fleet['drones'] if item['drone_id'] == 'SP-DRONE-04')
        self.assertEqual(drone['position_state'], 'PHONE_GPS')
        self.assertEqual(drone['telemetry_origin'], 'live')
        self.assertEqual(round(drone['actual_distance_km'], 3), round(expected_leg_m / 1000.0, 3))
        distance_after_accept = drone['actual_distance_km']

        status, duplicate = self.send_telemetry(
            'm5-state-device', 'SP-DRONE-04', device['token'], 43.6410, -79.4000,
            base + timedelta(seconds=30),
        )
        self.assertEqual(status, 400, duplicate)
        status, fleet = self.request('GET', '/api/fleet')
        drone = next(item for item in fleet['drones'] if item['drone_id'] == 'SP-DRONE-04')
        self.assertEqual(drone['actual_distance_km'], distance_after_accept)

        status, jump = self.send_telemetry(
            'm5-state-device', 'SP-DRONE-04', device['token'], 43.7000, -79.4000,
            base + timedelta(seconds=45), accuracy=50,
        )
        self.assertEqual(status, 200, jump)
        status, fleet = self.request('GET', '/api/fleet')
        drone = next(item for item in fleet['drones'] if item['drone_id'] == 'SP-DRONE-04')
        self.assertEqual(
            drone['actual_distance_km'], distance_after_accept,
            'GPS jumps must not be accumulated into observed distance.',
        )

        conn = store.get_connection()
        try:
            stale_time = utc_text(datetime.now(timezone.utc) - timedelta(seconds=600))
            conn.execute(
                "UPDATE telemetry_samples SET server_received_at = ?, device_timestamp = ? WHERE drone_id = 'SP-DRONE-04'",
                (stale_time, stale_time),
            )
            conn.commit()
        finally:
            conn.close()
        status, fleet = self.request('GET', '/api/fleet')
        drone = next(item for item in fleet['drones'] if item['drone_id'] == 'SP-DRONE-04')
        self.assertEqual(drone['position_state'], 'STALE')
        self.assertEqual(drone['telemetry_origin'], 'historical')
        self.assertIsNotNone(drone['position'], 'Historical positions stay visible but are labelled.')

    def test_delivery_workflow_arrival_evidence_handover_and_idempotent_completion(self):
        admin = create_authenticated_user(role='ADMIN', username='m5.delivery.admin')
        api.create_delivery_record(
            'M5-DELIVERY-01', 'SP-DRONE-02',
            {'lat': 43.6500, 'lng': -79.4200},
            {'lat': 43.6650, 'lng': -79.4050},
            planned_route_km=2.5,
        )
        api.update_delivery_status('M5-DELIVERY-01', 'ASSIGNED', 'test assignment')
        api.update_delivery_status('M5-DELIVERY-01', 'PICKUP_CONFIRMED', 'test pickup')
        api.update_delivery_status('M5-DELIVERY-01', 'IN_TRANSIT', 'test transit')

        device = self.register_device(admin, 'm5-arrival-device', 'SP-DRONE-02')
        status, arrival = self.send_telemetry(
            'm5-arrival-device', 'SP-DRONE-02', device['token'],
            43.6650, -79.4050, datetime.now(timezone.utc),
        )
        self.assertEqual(status, 200, arrival)
        self.assertTrue(arrival['arrival_detected'])
        conn = store.get_connection()
        try:
            row = conn.execute(
                "SELECT status, arrival_evidence FROM deliveries WHERE delivery_id = 'M5-DELIVERY-01'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row['status'], 'ARRIVED')
        evidence = json.loads(row['arrival_evidence'])
        self.assertLessEqual(evidence['distance_to_destination_m'], evidence['geofence_radius_m'])
        self.assertLessEqual(evidence['reported_accuracy_m'], evidence['geofence_radius_m'])

        status, confirmed = self.request(
            'POST', '/api/delivery/confirm',
            {'delivery_id': 'M5-DELIVERY-01', 'drone_id': 'SP-DRONE-02', 'handover_confirmed': True},
            admin,
        )
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(confirmed['status'], 'confirmed')
        status, repeated = self.request(
            'POST', '/api/delivery/confirm',
            {'delivery_id': 'M5-DELIVERY-01', 'drone_id': 'SP-DRONE-02', 'handover_confirmed': True},
            admin,
        )
        self.assertEqual(status, 200, repeated)
        self.assertEqual(repeated['status'], 'already_confirmed')

        conn = store.get_connection()
        try:
            delivery = conn.execute(
                "SELECT status, handover_confirmed, delivered_at FROM deliveries WHERE delivery_id = 'M5-DELIVERY-01'"
            ).fetchone()
            receipts = conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE delivery_id = 'M5-DELIVERY-01' AND notification_type = 'DELIVERY_COMPLETED'"
            ).fetchone()[0]
            states = [
                tuple(row) for row in conn.execute(
                    "SELECT previous_status, new_status FROM delivery_state_history WHERE delivery_id = 'M5-DELIVERY-01' ORDER BY id"
                ).fetchall()
            ]
        finally:
            conn.close()
        self.assertEqual(delivery['status'], 'DELIVERED')
        self.assertTrue(delivery['handover_confirmed'])
        self.assertIsNotNone(delivery['delivered_at'])
        self.assertEqual(receipts, 1, 'Repeated completion must not queue duplicate receipts.')
        self.assertIn(('ARRIVED', 'DELIVERED'), states)
        self.assertIn(('IN_TRANSIT', 'ARRIVED'), states)

    def test_phone_page_reports_gps_status_without_fabricating_positions(self):
        status, body, _ = self.request('GET', '/drone/connect', raw=True)
        self.assertEqual(status, 200)
        text = body.decode('utf-8')
        for marker in (
            'permissionValue', 'connectionValue', 'deliveryValue', 'lastFixValue',
            'accuracyValue', 'geoStatus', 'authStatus', 'watchPosition',
            'Location access was denied',
            'Location information is unavailable at the moment',
            'The request to retrieve location timed out',
            'Bearer',
        ):
            self.assertIn(marker, text, f'missing phone-page status marker: {marker}')
        self.assertNotIn('adminToken', text)
        self.assertIsNone(re.search(r'43\.\d{3,}', text), 'phone page must not embed coordinates')


class EmailReliabilityTests(Milestone5TestCase):
    def prepare_delivery(self, delivery_id='M5-MAIL-01', drone_id='SP-DRONE-02'):
        api.create_delivery_record(
            delivery_id, drone_id,
            {'lat': 43.6500, 'lng': -79.4200},
            {'lat': 43.6650, 'lng': -79.4050},
            planned_route_km=2.0,
        )
        api.update_delivery_status(delivery_id, 'ASSIGNED', 'test')
        api.update_delivery_status(delivery_id, 'PICKUP_CONFIRMED', 'test')
        api.update_delivery_status(delivery_id, 'IN_TRANSIT', 'test')
        api.update_delivery_status(delivery_id, 'ARRIVED', 'test arrival')
        api.update_delivery_status(delivery_id, 'DELIVERED', 'test handover')
        return delivery_id

    def outbox_row(self, delivery_id):
        conn = store.get_connection()
        try:
            row = conn.execute(
                'SELECT * FROM notification_outbox WHERE delivery_id = ? ORDER BY created_at DESC LIMIT 1',
                (delivery_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def force_due(self, delivery_id):
        conn = store.get_connection()
        try:
            conn.execute(
                'UPDATE notification_outbox SET next_attempt_at = ? WHERE delivery_id = ?',
                (utc_text(datetime.now(timezone.utc) - timedelta(seconds=5)), delivery_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_local_test_mode_marks_notification_sent_without_opening_smtp(self):
        self.set_env('SMTP_LOCAL_TEST_MODE', 'true')
        delivery_id = self.prepare_delivery('M5-MAIL-LOCAL')
        with mock.patch('app.api.smtplib.SMTP', side_effect=AssertionError('SMTP must not be opened')):
            processed = api.process_notification_outbox()
        self.assertTrue(any(item['state'] == 'SENT' for item in processed), processed)
        row = self.outbox_row(delivery_id)
        self.assertEqual(row['state'], 'SENT')
        self.assertIsNotNone(row['sent_at'])
        conn = store.get_connection()
        try:
            status = conn.execute(
                "SELECT notification_status FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()['notification_status']
        finally:
            conn.close()
        self.assertEqual(status, 'SENT')

    def test_transient_failures_retry_with_bounded_backoff_and_eventually_stop(self):
        self.set_env('SMTP_HOST', 'smtp.example.test')
        self.set_env('SMTP_USERNAME', 'bot@example.test')
        self.set_env('SMTP_APP_PASSWORD', SMTP_SENTINEL)
        self.set_env('DELIVERY_NOTIFICATION_TO', 'ops@example.test')
        self.set_env('SMTP_LOCAL_TEST_MODE', '')
        delivery_id = self.prepare_delivery('M5-MAIL-RETRY')

        with mock.patch('app.api.smtplib.SMTP', side_effect=OSError('connection refused')):
            api.process_notification_outbox()
        row = self.outbox_row(delivery_id)
        self.assertEqual(row['state'], 'FAILED')
        self.assertEqual(row['retry_count'], 1)
        self.assertEqual(row['last_error'], 'SMTP delivery failed')
        first_due = datetime.fromisoformat(row['next_attempt_at'].replace('Z', '+00:00'))
        first_delay = (first_due - datetime.now(timezone.utc)).total_seconds()
        self.assertTrue(50 <= first_delay <= 70, first_delay)

        self.force_due(delivery_id)
        with mock.patch('app.api.smtplib.SMTP', side_effect=OSError('connection refused')):
            api.process_notification_outbox()
        row = self.outbox_row(delivery_id)
        self.assertEqual(row['retry_count'], 2)
        second_due = datetime.fromisoformat(row['next_attempt_at'].replace('Z', '+00:00'))
        second_delay = (second_due - datetime.now(timezone.utc)).total_seconds()
        self.assertTrue(110 <= second_delay <= 130, second_delay)
        self.assertLessEqual(second_delay, 3600)

        self.force_due(delivery_id)
        with mock.patch('app.api.smtplib.SMTP', side_effect=OSError('connection refused')):
            api.process_notification_outbox()
        row = self.outbox_row(delivery_id)
        self.assertEqual(row['retry_count'], api.MAX_NOTIFICATION_RETRIES)

        self.force_due(delivery_id)
        with mock.patch('app.api.smtplib.SMTP', side_effect=AssertionError('retry budget exhausted')):
            processed = api.process_notification_outbox()
        self.assertFalse(
            any(item['id'] == row['id'] for item in processed),
            'Exhausted notifications must stop consuming delivery attempts.',
        )
        row = self.outbox_row(delivery_id)
        self.assertEqual(row['state'], 'FAILED')
        self.assertEqual(row['retry_count'], api.MAX_NOTIFICATION_RETRIES)

    def test_exhausted_failures_remain_visible_in_readiness_and_metrics(self):
        self.set_env('SMTP_LOCAL_TEST_MODE', '')
        delivery_id = self.prepare_delivery('M5-MAIL-EXHAUSTED')
        conn = store.get_connection()
        try:
            conn.execute(
                'UPDATE notification_outbox SET state = ?, retry_count = ? WHERE delivery_id = ?',
                ('FAILED', api.MAX_NOTIFICATION_RETRIES, delivery_id),
            )
            conn.commit()
        finally:
            conn.close()
        status, readiness = api.readiness_status()
        self.assertEqual(status, 200, readiness)
        self.assertGreaterEqual(readiness['notifications']['pending_or_retryable'], 1)
        metrics = api.prometheus_metrics([], 1.0)
        match = re.search(r'stormpilot_notification_exhausted_total (\d+)', metrics)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 1)
        row = self.outbox_row(delivery_id)
        self.assertEqual(row['state'], 'FAILED')

    def test_email_content_uses_persisted_delivery_evidence_only(self):
        admin = create_authenticated_user(role='ADMIN', username='m5.mail.admin')
        delivery_id = self.prepare_delivery('M5-MAIL-BODY')
        device = self.register_device(admin, 'm5-mail-device', 'SP-DRONE-02')
        self.assertIsNotNone(device['token'])
        row = self.outbox_row(delivery_id)
        conn = store.get_connection()
        try:
            delivered = conn.execute(
                'SELECT delivered_at, actual_observed_km FROM deliveries WHERE delivery_id = ?',
                (delivery_id,),
            ).fetchone()
            body = api._notification_email_body(conn, row, json.loads(row['payload']))
        finally:
            conn.close()
        self.assertIn(f'Delivery ID: {delivery_id}', body)
        self.assertIn('Drone ID: SP-DRONE-02', body)
        self.assertIn('m5-mail-device', body)
        self.assertIn(f'Observed distance (km): {round(float(delivered["actual_observed_km"] or 0), 3)}', body)
        self.assertIn(f'Completed at: {delivered["delivered_at"]}', body)
        self.assertIn('Handover confirmed:', body)
        self.assertNotIn(SMTP_SENTINEL, body)
        self.assertNotIn(device['token'], body)

    def test_smtp_credentials_stay_server_side_even_on_failure(self):
        self.set_env('SMTP_HOST', 'smtp.example.test')
        self.set_env('SMTP_USERNAME', 'bot@example.test')
        self.set_env('SMTP_APP_PASSWORD', SMTP_SENTINEL)
        self.set_env('DELIVERY_NOTIFICATION_TO', 'ops@example.test')
        self.set_env('SMTP_LOCAL_TEST_MODE', '')
        delivery_id = self.prepare_delivery('M5-MAIL-SECRETS')
        with mock.patch('app.api.smtplib.SMTP', side_effect=OSError('connection refused')):
            api.process_notification_outbox()
        row = self.outbox_row(delivery_id)
        self.assertNotIn(SMTP_SENTINEL, row['last_error'] or '')

        admin = create_authenticated_user(role='ADMIN', username='m5.smtp.admin')
        with mock.patch('app.api.smtplib.SMTP', side_effect=OSError('connection refused')):
            status, result = self.request('POST', '/api/email/test', {}, admin)
        self.assertEqual(status, 200, result)
        self.assertNotIn(SMTP_SENTINEL, json.dumps(result))
        status, config = self.request('GET', '/api/config')
        self.assertEqual(status, 200)
        self.assertNotIn(SMTP_SENTINEL, json.dumps(config))
        for name in os.listdir(STATIC_DIR):
            if not name.endswith(('.js', '.html', '.css')):
                continue
            with open(os.path.join(STATIC_DIR, name), encoding='utf-8') as handle:
                self.assertNotIn(SMTP_SENTINEL, handle.read(), name)


class MapsConfigurationTests(Milestone5TestCase):
    def test_keyless_mode_and_blank_keys_produce_a_clean_fallback(self):
        for configured in ('', '   '):
            with mock.patch.dict(os.environ, {'GOOGLE_MAPS_API_KEY': configured}):
                status, config = self.request('GET', '/api/config')
            self.assertEqual(status, 200)
            self.assertEqual(config['google_maps_api_key'], '')
            self.assertFalse(config['map_ready'])
            self.assertTrue(config['demo_mode'])

    def test_config_exposes_only_the_public_browser_key(self):
        with mock.patch.dict(os.environ, {
            'GOOGLE_MAPS_API_KEY': 'test-browser-key-not-a-real-secret',
            'SMTP_APP_PASSWORD': SMTP_SENTINEL,
            'REDMINE_API_KEY': REDMINE_SENTINEL,
        }):
            status, config = self.request('GET', '/api/config')
        self.assertEqual(status, 200)
        self.assertEqual(
            sorted(config),
            ['demo_mode', 'google_maps_api_key', 'map_ready'],
        )
        self.assertEqual(config['google_maps_api_key'], 'test-browser-key-not-a-real-secret')
        self.assertTrue(config['map_ready'])
        self.assertNotIn(SMTP_SENTINEL, json.dumps(config))
        self.assertNotIn(REDMINE_SENTINEL, json.dumps(config))

    def test_frontend_has_keyless_fallback_and_no_embedded_key(self):
        with open(os.path.join(STATIC_DIR, 'app.js'), encoding='utf-8') as handle:
            script = handle.read()
        self.assertIn('if (!config.map_ready || !config.google_maps_api_key)', script)
        self.assertIn('fallback-map', script)
        self.assertIn('Map key not configured', script)
        self.assertIsNone(re.search(r'AIza[0-9A-Za-z_\-]{35}', script))


class HealthAndObservabilityTests(Milestone5TestCase):
    def test_readiness_fails_on_missing_required_tables_and_recovers(self):
        conn = store.get_connection()
        try:
            conn.execute('DROP TABLE admin_sessions')
            conn.commit()
        finally:
            conn.close()
        status, payload = api.readiness_status()
        self.assertEqual(status, 503)
        self.assertFalse(payload['storage']['ok'])
        self.assertIn('admin_sessions', payload['storage']['missing_tables'])

    def test_readiness_reports_storage_failure_while_liveness_stays_up(self):
        with mock.patch('app.db.get_connection', side_effect=sqlite3.DatabaseError('disk unavailable')):
            status, payload = api.readiness_status()
            self.assertEqual(status, 503)
            self.assertFalse(payload['storage']['ok'])
            http_status, ready_body = self.request('GET', '/readyz')
            self.assertEqual(http_status, 503)
            self.assertEqual(ready_body['status'], 'unready')
            http_status, health_body = self.request('GET', '/healthz')
            self.assertEqual(http_status, 200)
            self.assertEqual(health_body['status'], 'ok')
            metrics = api.prometheus_metrics([], 1.0)
        self.assertIn('stormpilot_ready 0', metrics)
        self.assertIn('stormpilot_storage_available 0', metrics)

    def test_metrics_are_bounded_and_free_of_identifiers_or_secrets(self):
        self.set_env('SMTP_APP_PASSWORD', SMTP_SENTINEL)
        self.set_env('REDMINE_API_KEY', REDMINE_SENTINEL)
        self.set_env('GOOGLE_MAPS_API_KEY', '')
        admin = create_authenticated_user(role='ADMIN', username='m5.metrics.admin')
        sample_time = datetime.now(timezone.utc) - timedelta(seconds=30)
        for device_id, drone_id in (('m5-metrics-a', 'SP-DRONE-01'), ('m5-metrics-b', 'SP-DRONE-02')):
            device = self.register_device(admin, device_id, drone_id)
            status, _ = self.send_telemetry(
                device_id, drone_id, device['token'], 43.6400, -79.4000, sample_time,
            )
            self.assertEqual(status, 200)
            status, _ = self.send_telemetry(
                device_id, drone_id, device['token'], 43.6400, -79.4000, sample_time,
            )
            self.assertEqual(status, 400, 'an identical timestamp must be rejected as a duplicate')
        for _ in range(3):
            self.request('POST', '/api/auth/login', {'username': 'm5.nobody', 'password': 'wrong-password-value'})

        status, metrics, _ = self.request('GET', '/metrics', raw=True)
        self.assertEqual(status, 200)
        text = metrics.decode('utf-8')
        for family in (
            'stormpilot_telemetry_rejected_total', 'stormpilot_stale_devices',
            'stormpilot_deliveries_total', 'stormpilot_notifications_total',
            'stormpilot_notification_exhausted_total', 'stormpilot_optimizer_rejections_total',
            'stormpilot_simulation_runs_total', 'stormpilot_auth_throttled_keys',
            'stormpilot_delivery_completions_total', 'stormpilot_login_failures_total',
            'stormpilot_ready', 'stormpilot_connected_drones',
        ):
            self.assertIn(family, text, f'missing metric family {family}')

        allowed_labels = {'route', 'status', 'reason', 'state'}
        label_counts = {}
        for line in text.splitlines():
            if line.startswith('#') or not line.strip():
                continue
            match = re.match(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?', line)
            self.assertIsNotNone(match, line)
            name = match.group(1)
            if match.group(2):
                names = re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)=', match.group(2))
                unexpected = set(names) - allowed_labels
                self.assertFalse(unexpected, f'unbounded labels {unexpected} on {name}')
                label_counts.setdefault(name, set()).add(match.group(2))
        self.assertEqual(len(label_counts.get('stormpilot_telemetry_rejected_total', set())), 6)
        self.assertEqual(len(label_counts.get('stormpilot_deliveries_total', set())), 8)
        self.assertEqual(len(label_counts.get('stormpilot_notifications_total', set())), 4)

        self.assertNotIn(SMTP_SENTINEL, text)
        self.assertNotIn(REDMINE_SENTINEL, text)
        self.assertNotIn('stormpilot_session=', text)
        self.assertNotIn('Bearer ', text)


class PlanningSimulationReplayRegressionTests(Milestone5TestCase):
    def test_optimizer_respects_availability_and_configured_virtual_zones(self):
        admin = create_authenticated_user(role='ADMIN', username='m5.plan.admin')
        api.create_delivery_record(
            'M5-ZONED', 'SP-DRONE-01',
            {'lat': 43.6500, 'lng': -79.3900},
            {'lat': 43.6540, 'lng': -79.3820},
            planned_route_km=1.2,
        )
        api.create_delivery_record(
            'M5-OPEN', 'SP-DRONE-03',
            {'lat': 43.6700, 'lng': -79.4500},
            {'lat': 43.6750, 'lng': -79.4550},
            planned_route_km=2.0,
        )

        status, plan = self.request('POST', '/api/missions/optimize', {}, admin)
        self.assertEqual(status, 200, plan)
        unassigned = {item['delivery_id']: item['reason'] for item in plan['unassigned_deliveries']}
        self.assertIn('M5-ZONED', unassigned)
        self.assertIn('restricted virtual zone', unassigned['M5-ZONED'], unassigned['M5-ZONED'])
        self.assertIn('M5-OPEN', unassigned)
        self.assertIn('fresh connected position', unassigned['M5-OPEN'])

        device = self.register_device(admin, 'm5-plan-device', 'SP-DRONE-01')
        status, _ = self.send_telemetry(
            'm5-plan-device', 'SP-DRONE-01', device['token'], 43.6700, -79.4400,
            datetime.now(timezone.utc) - timedelta(seconds=20),
        )
        self.assertEqual(status, 200)
        status, plan = self.request('POST', '/api/missions/optimize', {}, admin)
        self.assertEqual(status, 200, plan)
        assigned = {item['delivery_id']: item['drone_id'] for item in plan['recommended_assignments']}
        self.assertEqual(assigned.get('M5-OPEN'), 'SP-DRONE-01')
        unassigned = {item['delivery_id']: item['reason'] for item in plan['unassigned_deliveries']}
        self.assertIn('M5-ZONED', unassigned)
        self.assertIn('restricted virtual zone', unassigned['M5-ZONED'])

    def test_replay_is_bounded_read_only_and_excludes_secrets(self):
        admin = create_authenticated_user(role='ADMIN', username='m5.replay.admin')
        device = self.register_device(admin, 'm5-replay-device', 'SP-DRONE-04')
        base = datetime.now(timezone.utc) - timedelta(seconds=200)
        first_status, _ = self.send_telemetry(
            'm5-replay-device', 'SP-DRONE-04', device['token'], 43.6400, -79.4000, base,
        )
        self.assertEqual(first_status, 200)
        second_status, _ = self.send_telemetry(
            'm5-replay-device', 'SP-DRONE-04', device['token'], 43.6410, -79.4000,
            base + timedelta(seconds=150),
        )
        self.assertEqual(second_status, 200)

        start = utc_text(base - timedelta(minutes=5))
        end = utc_text(datetime.now(timezone.utc) + timedelta(minutes=5))
        for query in (
            f'drone_id=SP-DRONE-04&limit=2500&from={start}&to={end}',
            f'drone_id=SP-DRONE-04&limit=0&from={start}&to={end}',
            f'drone_id=SP-DRONE-04&from=2020-01-01T00:00:00Z&to=2020-03-01T00:00:00Z',
            f'drone_id=SP-DRONE-04&delivery_id=X&from={start}&to={end}',
        ):
            status, payload = self.request('GET', f'/api/replay?{query}', headers=admin)
            self.assertEqual(status, 400, (query, payload))

        status, replay = self.request(
            'GET', f'/api/replay?drone_id=SP-DRONE-04&from={start}&to={end}', headers=admin,
        )
        self.assertEqual(status, 200, replay)
        self.assertTrue(replay['read_only'])
        self.assertEqual(replay['origin'], 'HISTORICAL_PERSISTED')
        self.assertEqual(len(replay['points']), 2)
        self.assertEqual(len(replay['gaps']), 1, 'the 150 second recording gap must be reported')
        self.assertIn('no coordinates are interpolated', replay['message'])

        status, report = self.request(
            'GET', f'/api/replay/report?drone_id=SP-DRONE-04&from={start}&to={end}', headers=admin,
        )
        self.assertEqual(status, 200, report)
        serialized = json.dumps(report)
        self.assertNotIn(device['token'], serialized)
        lowered = serialized.lower()
        for forbidden in ('password', 'password_hash', 'secret', 'csrf', 'session_cookie'):
            self.assertNotIn(forbidden, lowered)


if __name__ == '__main__':
    unittest.main()