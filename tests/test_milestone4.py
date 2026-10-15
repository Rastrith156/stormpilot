import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import api, security
from app import db as store
from app.server import Handler
from support import create_authenticated_user


class AdministratorAuthenticationTests(unittest.TestCase):
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
        self.temp_dir = tempfile.mkdtemp(prefix='stormpilot_m4_auth_')
        self.original_data_dir = os.environ.get('APP_DATA_DIR')
        self.original_secure = os.environ.get('STORMPILOT_COOKIE_SECURE')
        os.environ['APP_DATA_DIR'] = self.temp_dir
        os.environ['STORMPILOT_COOKIE_SECURE'] = 'false'
        api.initialize_storage(force_reset=True)
        status, user = security.bootstrap_admin('admin.primary', 'test-password-that-is-strong')
        self.assertEqual(status, 201, user)
        self.admin = self.login('admin.primary', 'test-password-that-is-strong')

    def tearDown(self):
        if self.original_data_dir is None:
            os.environ.pop('APP_DATA_DIR', None)
        else:
            os.environ['APP_DATA_DIR'] = self.original_data_dir
        if self.original_secure is None:
            os.environ.pop('STORMPILOT_COOKIE_SECURE', None)
        else:
            os.environ['STORMPILOT_COOKIE_SECURE'] = self.original_secure
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def request(self, method, path, payload=None, headers=None):
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
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}, response.headers
        except HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw else {}, error.headers

    def login(self, username, password):
        status, result, response_headers = self.request(
            'POST',
            '/api/auth/login',
            {'username': username, 'password': password},
        )
        self.assertEqual(status, 200, result)
        cookies = {}
        for value in response_headers.get_all('Set-Cookie'):
            key, _, rest = value.partition('=')
            cookies[key] = rest.split(';', 1)[0]
        csrf = cookies['stormpilot_csrf']
        return {
            'Cookie': f"stormpilot_session={cookies['stormpilot_session']}; stormpilot_csrf={csrf}",
            'X-CSRF-Token': csrf,
        }

    def test_bootstrap_is_one_time_and_password_is_never_stored_plaintext(self):
        status, result = security.bootstrap_admin('second.admin', 'another-password-that-is-strong')
        self.assertEqual(status, 409)
        self.assertIn('closed', result['error'])
        conn = store.get_connection()
        try:
            row = conn.execute('SELECT password_hash, password_salt, password_iterations FROM admin_users').fetchone()
        finally:
            conn.close()
        self.assertNotIn('test-password-that-is-strong', row['password_hash'])
        self.assertNotEqual(row['password_hash'], row['password_salt'])
        self.assertGreaterEqual(row['password_iterations'], 600000)
        conn = store.get_connection()
        try:
            self.assertEqual(conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 5)
        finally:
            conn.close()

    def test_login_issues_httponly_strict_session_and_csrf_cookies(self):
        status, result, headers = self.request(
            'POST',
            '/api/auth/login',
            {'username': 'admin.primary', 'password': 'test-password-that-is-strong'},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result['user']['role'], 'ADMIN')
        cookie_values = headers.get_all('Set-Cookie')
        self.assertTrue(any('stormpilot_session=' in value and 'HttpOnly' in value and 'SameSite=Strict' in value for value in cookie_values))
        self.assertTrue(any('stormpilot_csrf=' in value and 'SameSite=Strict' in value for value in cookie_values))
        with patch.dict(os.environ, {'STORMPILOT_COOKIE_SECURE': 'true'}):
            secure_status, _, secure_headers = security.login(
                {'username': 'admin.primary', 'password': 'test-password-that-is-strong'},
                {'X-Client-IP': '198.51.100.2'},
            )
        self.assertEqual(secure_status, 200)
        self.assertTrue(all('Secure' in value for name, value in secure_headers if name == 'Set-Cookie'))

    def test_login_logout_and_revocation(self):
        status, result, _ = self.request('GET', '/api/auth/session', headers=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(result['user']['username'], 'admin.primary')
        status, result, _ = self.request('POST', '/api/auth/logout', headers=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'logged_out')
        status, _, _ = self.request('GET', '/api/auth/session', headers=self.admin)
        self.assertEqual(status, 401)

    def test_csrf_is_required_for_cookie_authenticated_mutations(self):
        missing_csrf = {'Cookie': self.admin['Cookie']}
        status, result, _ = self.request(
            'POST',
            '/api/device/register',
            {'device_id': 'csrf-missing', 'drone_id': 'SP-DRONE-01'},
            missing_csrf,
        )
        self.assertEqual(status, 403)
        self.assertIn('CSRF', result['error'])
        bad_csrf = {**self.admin, 'X-CSRF-Token': 'not-the-issued-token'}
        status, _, _ = self.request(
            'POST',
            '/api/device/register',
            {'device_id': 'csrf-invalid', 'drone_id': 'SP-DRONE-01'},
            bad_csrf,
        )
        self.assertEqual(status, 403)

    def test_viewer_cannot_mutate_and_account_disable_revokes_existing_sessions(self):
        status, viewer, _ = self.request(
            'POST',
            '/api/admin/users',
            {'username': 'viewer.demo', 'password': 'viewer-password-that-is-strong', 'role': 'VIEWER'},
            self.admin,
        )
        self.assertEqual(status, 201)
        viewer_headers = self.login('viewer.demo', 'viewer-password-that-is-strong')
        status, _, _ = self.request(
            'POST',
            '/api/device/register',
            {'device_id': 'viewer-cannot-enroll', 'drone_id': 'SP-DRONE-01'},
            viewer_headers,
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            'PUT',
            f"/api/admin/users/{viewer['user_id']}",
            {'enabled': False},
            self.admin,
        )
        self.assertEqual(status, 200)
        status, _, _ = self.request('GET', '/api/auth/session', headers=viewer_headers)
        self.assertEqual(status, 401)
        status, _, _ = self.request(
            'POST',
            '/api/auth/login',
            {'username': 'viewer.demo', 'password': 'viewer-password-that-is-strong'},
        )
        self.assertEqual(status, 401)

    def test_expired_session_is_rejected(self):
        session_id = next(
            part.split('=', 1)[1] for part in self.admin['Cookie'].split('; ')
            if part.startswith('stormpilot_session=')
        )
        import hashlib
        conn = store.get_connection()
        try:
            conn.execute(
                'UPDATE admin_sessions SET expires_at = ? WHERE session_hash = ?',
                (
                    (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace('+00:00', 'Z'),
                    hashlib.sha256(session_id.encode('ascii')).hexdigest(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        status, _, _ = self.request('GET', '/api/auth/session', headers=self.admin)
        self.assertEqual(status, 401)

    def test_brute_force_attempts_are_rate_limited_and_audited_without_passwords(self):
        statuses = []
        for _ in range(6):
            status, _, _ = self.request(
                'POST',
                '/api/auth/login',
                {'username': 'admin.primary', 'password': 'incorrect-password-value'},
            )
            statuses.append(status)
        self.assertEqual(statuses[:5], [401] * 5)
        self.assertEqual(statuses[5], 429)
        conn = store.get_connection()
        try:
            events = conn.execute(
                "SELECT payload FROM event_log WHERE event_type IN ('ADMIN_LOGIN_FAILED','ADMIN_LOGIN_RATE_LIMITED')"
            ).fetchall()
        finally:
            conn.close()
        self.assertGreaterEqual(len(events), 6)
        self.assertTrue(all('incorrect-password-value' not in row['payload'] for row in events))

    def test_legacy_token_does_not_bypass_session_authorization(self):
        legacy = 'legacy-token-from-old-demo-configuration-value'
        status, _, _ = self.request(
            'POST',
            '/api/device/register',
            {'device_id': 'legacy-no-bypass', 'drone_id': 'SP-DRONE-01'},
            {'Authorization': f'Bearer {legacy}'},
        )
        self.assertEqual(status, 401)


class MissionSimulationAndReplayTests(unittest.TestCase):
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
        self.temp_dir = tempfile.mkdtemp(prefix='stormpilot_m4_ops_')
        self.original_data_dir = os.environ.get('APP_DATA_DIR')
        self.original_secure = os.environ.get('STORMPILOT_COOKIE_SECURE')
        os.environ['APP_DATA_DIR'] = self.temp_dir
        os.environ['STORMPILOT_COOKIE_SECURE'] = 'false'
        api.initialize_storage(force_reset=True)
        self.admin = create_authenticated_user()

    def tearDown(self):
        if self.original_data_dir is None:
            os.environ.pop('APP_DATA_DIR', None)
        else:
            os.environ['APP_DATA_DIR'] = self.original_data_dir
        if self.original_secure is None:
            os.environ.pop('STORMPILOT_COOKIE_SECURE', None)
        else:
            os.environ['STORMPILOT_COOKIE_SECURE'] = self.original_secure
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def request(self, method, path, payload=None, headers=None):
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
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def register_gps(self, drone_id, latitude, longitude):
        device_id = f'phone-{drone_id.lower()}'
        status, registered = self.request(
            'POST',
            '/api/device/register',
            {'device_id': device_id, 'drone_id': drone_id},
            self.admin,
        )
        self.assertEqual(status, 200, registered)
        timestamp = datetime.now(timezone.utc)
        status, accepted = self.request(
            'POST',
            '/api/device/telemetry',
            {
                'device_id': device_id,
                'drone_id': drone_id,
                'lat': latitude,
                'lng': longitude,
                'accuracy': 8,
                'timestamp': timestamp.isoformat().replace('+00:00', 'Z'),
            },
            {'Authorization': f"Bearer {registered['token']}"},
        )
        self.assertEqual(status, 200, accepted)
        return device_id, timestamp

    def add_delivery(self, delivery_id='M4-DELIVERY-01', deadline=None, route_km=3.5, drone_id='SP-DRONE-02'):
        return api.create_delivery_record(
            delivery_id,
            drone_id,
            {'lat': 43.6700, 'lng': -79.4200},
            {'lat': 43.6750, 'lng': -79.4250},
            planned_route_km=route_km,
            priority='high',
            deadline=deadline,
        )

    def activate_nearby_drones(self):
        self.register_gps('SP-DRONE-01', 43.6700, -79.4200)
        self.register_gps('SP-DRONE-02', 43.7200, -79.5000)

    def test_optimizer_compares_strategies_and_respects_hard_range_and_deadline_constraints(self):
        self.activate_nearby_drones()
        deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace('+00:00', 'Z')
        self.add_delivery(deadline=deadline)
        status, result = self.request('POST', '/api/missions/optimize', {}, self.admin)
        self.assertEqual(status, 200, result)
        self.assertEqual(
            [item['strategy'] for item in result['strategies']],
            ['current_assignments', 'nearest_eligible', 'optimized'],
        )
        self.assertTrue(result['recommended_assignments'], result)
        self.assertEqual(result['recommended_assignments'][0]['drone_id'], 'SP-DRONE-01')
        self.assertGreater(result['estimated_distance_savings_vs_current_km'], 0)
        self.assertTrue(result['recommended_assignments'][0]['deadline_feasible'])
        self.assertEqual(result['recommended_assignments'][0]['route_distance_method'], 'persisted_planned_route')

        with unittest.mock.patch.dict(os.environ, {'STORMPILOT_MAX_DRONE_RANGE_KM': '1'}):
            status, range_result = self.request('POST', '/api/missions/optimize', {}, self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(range_result['recommended_assignments'], [])
        self.assertTrue(any(
            'maximum range' in reason.lower()
            for item in range_result['constraint_violations']
            for reason in item['reasons']
        ))

    def test_optimization_requires_operator_approval_and_rejects_stale_concurrent_proposals(self):
        self.activate_nearby_drones()
        self.add_delivery()
        status, proposal = self.request('POST', '/api/missions/optimize', {}, self.admin)
        self.assertEqual(status, 200)
        status, result = self.request(
            'POST',
            '/api/missions/optimize/apply',
            {'optimization_id': proposal['optimization_id']},
            self.admin,
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result['assignments_applied'], 1)
        conn = store.get_connection()
        try:
            row = conn.execute(
                'SELECT drone_id, status FROM deliveries WHERE delivery_id = ?',
                ('M4-DELIVERY-01',),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(row), ('SP-DRONE-01', 'ASSIGNED'))
        status, repeated = self.request(
            'POST',
            '/api/missions/optimize/apply',
            {'optimization_id': proposal['optimization_id']},
            self.admin,
        )
        self.assertEqual(status, 409)
        self.assertIn('already', repeated['error'])

        self.add_delivery('M4-DELIVERY-02')
        status, stale = self.request('POST', '/api/missions/optimize', {}, self.admin)
        self.assertEqual(status, 200)
        conn = store.get_connection()
        try:
            conn.execute(
                "UPDATE deliveries SET status = 'CANCELLED' WHERE delivery_id = ?",
                ('M4-DELIVERY-02',),
            )
            conn.commit()
        finally:
            conn.close()
        status, response = self.request(
            'POST',
            '/api/missions/optimize/apply',
            {'optimization_id': stale['optimization_id']},
            self.admin,
        )
        self.assertEqual(status, 409)
        self.assertIn('changed', response['error'])

    def test_offline_scenario_is_isolated_repeatable_and_requires_approval(self):
        self.activate_nearby_drones()
        self.add_delivery(drone_id='SP-DRONE-01')
        conn = store.get_connection()
        try:
            before = conn.execute('SELECT drone_id, status FROM deliveries WHERE delivery_id = ?', ('M4-DELIVERY-01',)).fetchone()
            telemetry_before = conn.execute('SELECT COUNT(*) FROM telemetry_samples').fetchone()[0]
        finally:
            conn.close()
        status, scenario = self.request(
            'POST',
            '/api/scenarios',
            {'scenario_type': 'drone_offline', 'drone_id': 'SP-DRONE-01'},
            self.admin,
        )
        self.assertEqual(status, 201, scenario)
        self.assertEqual(scenario['origin'], 'SIMULATED')
        self.assertEqual(scenario['proposed_plan']['recommended_assignments'][0]['drone_id'], 'SP-DRONE-02')
        self.assertIn('SP-DRONE-01', scenario['affected_drone_ids'])
        with unittest.mock.patch.dict(os.environ, {'STORMPILOT_MAX_DRONE_RANGE_KM': '0.01'}):
            status, replay = self.request('GET', f"/api/scenarios/{scenario['scenario_id']}:replay", headers=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(
            replay['proposed_plan']['recommended_assignments'],
            scenario['proposed_plan']['recommended_assignments'],
        )
        status, history = self.request('GET', '/api/scenarios', headers=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(history['origin'], 'SIMULATED')
        status, denied = self.request(
            'POST',
            f"/api/scenarios/{scenario['scenario_id']}/apply",
            {'approved': False},
            self.admin,
        )
        self.assertEqual(status, 400)
        status, applied = self.request(
            'POST',
            f"/api/scenarios/{scenario['scenario_id']}/apply",
            {'approved': True},
            self.admin,
        )
        self.assertEqual(status, 200, applied)
        conn = store.get_connection()
        try:
            after = conn.execute('SELECT drone_id, status FROM deliveries WHERE delivery_id = ?', ('M4-DELIVERY-01',)).fetchone()
            telemetry_after = conn.execute('SELECT COUNT(*) FROM telemetry_samples').fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(tuple(before), ('SP-DRONE-01', 'CREATED'))
        self.assertEqual(tuple(after), ('SP-DRONE-02', 'ASSIGNED'))
        self.assertEqual(telemetry_before, telemetry_after)

    def test_route_replay_is_ordered_bounded_and_reports_recording_gaps(self):
        status, registered = self.request(
            'POST',
            '/api/device/register',
            {'device_id': 'replay-phone', 'drone_id': 'SP-DRONE-04'},
            self.admin,
        )
        self.assertEqual(status, 200)
        token = registered['token']
        earlier = datetime.now(timezone.utc) - timedelta(minutes=4)
        for index, latitude in enumerate((43.6700, 43.6710)):
            stamp = earlier + timedelta(minutes=4 * index)
            status, _ = self.request(
                'POST',
                '/api/device/telemetry',
                {
                    'device_id': 'replay-phone',
                    'drone_id': 'SP-DRONE-04',
                    'lat': latitude,
                    'lng': -79.4200,
                    'accuracy': 9,
                    'timestamp': stamp.isoformat().replace('+00:00', 'Z'),
                },
                {'Authorization': f'Bearer {token}'},
            )
            self.assertEqual(status, 200)
        self.add_delivery('M4-REPLAY-DELIVERY', drone_id='SP-DRONE-04')
        start_text = (earlier - timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')
        end_text = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')
        status, replay = self.request(
            'GET',
            f'/api/replay?drone_id=SP-DRONE-04&from={start_text}&to={end_text}&limit=2',
            headers=self.admin,
        )
        self.assertEqual(status, 200, replay)
        self.assertEqual(len(replay['points']), 2)
        self.assertEqual(replay['points'][0]['latitude'], 43.67)
        self.assertEqual(len(replay['gaps']), 1)
        self.assertTrue(replay['read_only'])
        report_status, report = self.request(
            'GET',
            f'/api/replay/report?delivery_id=M4-REPLAY-DELIVERY&from={start_text}&to={end_text}',
            headers=self.admin,
        )
        self.assertEqual(report_status, 200)
        self.assertEqual(report['report']['sample_count'], 2)
        empty_status, empty = self.request(
            'GET',
            f'/api/replay?drone_id=SP-DRONE-01&from=2020-01-01T00:00:00Z&to=2020-01-02T00:00:00Z',
            headers=self.admin,
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty['points'], [])


if __name__ == '__main__':
    unittest.main()
