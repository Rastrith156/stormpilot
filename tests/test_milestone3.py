import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import api
from app import db as store
from app.server import Handler
from app.wsgi import application
from support import create_authenticated_user


class MilestoneThreeTests(unittest.TestCase):

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
        self.temp_dir = tempfile.mkdtemp(prefix='stormpilot_m3_')
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
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(f'{self.base_url}{path}', data=body, method=method)
        request.add_header('Content-Type', 'application/json')
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    @property
    def admin_headers(self):
        return self.admin_session.copy()

    def register(self, device_id, drone_id):
        status, response = self.request(
            'POST',
            '/api/device/register',
            {'device_id': device_id, 'drone_id': drone_id},
            self.admin_headers,
        )
        self.assertEqual(status, 200)
        return response['token']

    def send_gps(self, device_id, drone_id, token, latitude, longitude, timestamp=None):
        return self.request(
            'POST',
            '/api/device/telemetry',
            {
                'device_id': device_id,
                'drone_id': drone_id,
                'lat': latitude,
                'lng': longitude,
                'accuracy': 8,
                'timestamp': (timestamp or datetime.now(timezone.utc)).isoformat().replace('+00:00', 'Z'),
            },
            {'Authorization': f'Bearer {token}'},
        )

    def test_enrollment_and_revocation_require_server_admin_authorization(self):
        payload = {'device_id': 'unapproved-phone', 'drone_id': 'SP-DRONE-01'}
        status, _ = self.request('POST', '/api/device/register', payload)
        self.assertEqual(status, 401)
        status, _ = self.request('POST', '/api/device/register', payload, {
            'Authorization': 'Bearer incorrect-admin-token',
        })
        self.assertEqual(status, 401)
        status, _ = self.request('DELETE', '/api/device/revoke', {'device_id': 'unapproved-phone'})
        self.assertEqual(status, 401)
        conn = store.get_connection()
        try:
            self.assertIsNone(conn.execute(
                'SELECT 1 FROM device_credentials WHERE device_id = ?',
                ('unapproved-phone',),
            ).fetchone())
        finally:
            conn.close()

    def test_legacy_shared_token_does_not_authorize_privileged_actions(self):
        legacy_token = 'legacy-shared-token-that-must-not-authorize'
        with patch.dict(os.environ, {'STORMPILOT_ADMIN_TOKEN': legacy_token}):
            status, payload = self.request(
                'POST',
                '/api/device/register',
                {'device_id': 'phone-legacy-admin', 'drone_id': 'SP-DRONE-01'},
                {'Authorization': f'Bearer {legacy_token}'},
            )
        self.assertEqual(status, 401)
        self.assertIn('login', payload['error'].lower())

    def test_admin_device_actions_are_audited_and_phone_telemetry_still_works(self):
        token = self.register('phone-admin-audit', 'SP-DRONE-01')
        status, response = self.send_gps(
            'phone-admin-audit', 'SP-DRONE-01', token, 43.6501, -79.3801,
        )
        self.assertEqual(status, 200)
        self.assertEqual(response['status'], 'accepted')
        status, _ = self.request(
            'DELETE',
            '/api/device/revoke',
            {'device_id': 'phone-admin-audit', 'reason': 'operator test'},
            self.admin_headers,
        )
        self.assertEqual(status, 200)
        conn = store.get_connection()
        try:
            events = conn.execute(
                "SELECT event_type, payload FROM event_log WHERE entity_id = ? ORDER BY timestamp",
                ('phone-admin-audit',),
            ).fetchall()
        finally:
            conn.close()
        event_types = [event['event_type'] for event in events]
        self.assertIn('DEVICE_REGISTERED', event_types)
        self.assertIn('DEVICE_REVOKED', event_types)
        self.assertTrue(all(json.loads(event['payload'])['actor'] == 'administrator' for event in events))
        status, _ = self.send_gps(
            'phone-admin-audit', 'SP-DRONE-01', token, 43.6502, -79.3801,
        )
        self.assertEqual(status, 401)

    def test_fleet_reports_four_offline_drones_without_fabricated_positions(self):
        status, fleet = self.request('GET', '/api/fleet')
        self.assertEqual(status, 200)
        self.assertEqual(len(fleet['drones']), 4)
        self.assertEqual({drone['position_state'] for drone in fleet['drones']}, {'OFFLINE'})
        self.assertTrue(all(drone['position'] is None for drone in fleet['drones']))
        self.assertTrue(all(drone['actual_distance_km'] == 0 for drone in fleet['drones']))

    def test_map_configuration_is_optional_and_returns_only_browser_key(self):
        with patch.dict(os.environ, {'GOOGLE_MAPS_API_KEY': 'browser-test-key'}):
            status, configured = self.request('GET', '/api/config')
        self.assertEqual(status, 200)
        self.assertTrue(configured['map_ready'])
        self.assertEqual(configured['google_maps_api_key'], 'browser-test-key')
        with patch.dict(os.environ, {'GOOGLE_MAPS_API_KEY': ''}):
            status, unconfigured = self.request('GET', '/api/config')
        self.assertEqual(status, 200)
        self.assertFalse(unconfigured['map_ready'])
        self.assertEqual(unconfigured['google_maps_api_key'], '')

    def test_stale_gps_is_retained_but_not_reported_as_current(self):
        token = self.register('phone-stale', 'SP-DRONE-02')
        status, _ = self.send_gps('phone-stale', 'SP-DRONE-02', token, 43.6501, -79.3801)
        self.assertEqual(status, 200)
        conn = store.get_connection()
        try:
            conn.execute(
                """
                UPDATE telemetry_samples
                SET server_received_at = strftime('%Y-%m-%dT%H:%M:%SZ','now','-5 minutes')
                WHERE drone_id = ?
                """,
                ('SP-DRONE-02',),
            )
            conn.commit()
        finally:
            conn.close()
        _, fleet = self.request('GET', '/api/fleet')
        drone = next(item for item in fleet['drones'] if item['drone_id'] == 'SP-DRONE-02')
        self.assertEqual(drone['position_state'], 'STALE')
        self.assertEqual(drone['source'], 'PHONE_GPS')
        self.assertIsNotNone(drone['position'])
        self.assertEqual(drone['telemetry_origin'], 'historical')

        old_fix_token = self.register('phone-old-fix', 'SP-DRONE-03')
        old_device_time = datetime.now(timezone.utc) - timedelta(seconds=180)
        status, _ = self.send_gps(
            'phone-old-fix',
            'SP-DRONE-03',
            old_fix_token,
            43.6502,
            -79.3802,
            timestamp=old_device_time,
        )
        self.assertEqual(status, 200)
        _, fleet = self.request('GET', '/api/fleet')
        old_fix = next(item for item in fleet['drones'] if item['drone_id'] == 'SP-DRONE-03')
        self.assertEqual(old_fix['position_state'], 'STALE')
        self.assertEqual(old_fix['telemetry_origin'], 'historical')
        _, traffic = self.request('GET', '/api/traffic?window_hours=1')
        self.assertEqual(traffic['origin'], 'historical')

    def test_retention_prunes_expired_gps_but_keeps_recent_records(self):
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        conn = store.get_connection()
        try:
            conn.executemany(
                """
                INSERT INTO telemetry_samples (
                    drone_id, latitude, longitude, device_timestamp, server_received_at,
                    source, status, accepted, distance_m
                ) VALUES (?, ?, ?, ?, ?, 'PHONE_GPS', 'ACCEPTED', 1, 0)
                """,
                [
                    ('SP-DRONE-01', 43.65, -79.38, '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z'),
                    ('SP-DRONE-01', 43.65, -79.38, now, now),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        with patch.dict(os.environ, {'DATA_RETENTION_DAYS': '30'}):
            api._prune_expired_telemetry_if_due(force=True)
        conn = store.get_connection()
        try:
            timestamps = conn.execute(
                'SELECT device_timestamp FROM telemetry_samples WHERE drone_id = ? ORDER BY id',
                ('SP-DRONE-01',),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([row['device_timestamp'] for row in timestamps], [now])

    def test_traffic_aggregation_and_filters_use_persisted_gps(self):
        first_token = self.register('phone-traffic-1', 'SP-DRONE-01')
        second_token = self.register('phone-traffic-2', 'SP-DRONE-02')
        first_fix = datetime.now(timezone.utc) - timedelta(seconds=5)
        positions = (
            ('phone-traffic-1', 'SP-DRONE-01', first_token, 43.6500, -79.3801),
            ('phone-traffic-1', 'SP-DRONE-01', first_token, 43.6500, -79.3799),
            ('phone-traffic-2', 'SP-DRONE-02', second_token, 43.6499, -79.3800),
            ('phone-traffic-2', 'SP-DRONE-02', second_token, 43.6501, -79.3800),
        )
        for index, (device_id, drone_id, token, latitude, longitude) in enumerate(positions):
            status, _ = self.send_gps(
                device_id,
                drone_id,
                token,
                latitude,
                longitude,
                first_fix + timedelta(seconds=(index % 2) * 2),
            )
            self.assertEqual(status, 200)
        _, fleet = self.request('GET', '/api/fleet')
        first_drone = next(item for item in fleet['drones'] if item['drone_id'] == 'SP-DRONE-01')
        self.assertEqual(first_drone['position_state'], 'PHONE_GPS')
        self.assertTrue(first_drone['gps_coverage_sufficient'])
        self.assertGreater(first_drone['actual_distance_km'], 0)
        self.assertEqual(first_drone['position']['latitude'], 43.65)
        self.assertEqual(len(first_drone['observed_path']), 2)
        api.create_delivery_record(
            'DEL-TRAFFIC-M3',
            'SP-DRONE-01',
            {'lat': 43.6500, 'lng': -79.3800},
            {'lat': 43.6510, 'lng': -79.3810},
            planned_route_km=1.2,
        )
        status, traffic = self.request(
            'GET',
            '/api/traffic?window_hours=1&source=PHONE_GPS',
        )
        self.assertEqual(status, 200)
        self.assertEqual(traffic['origin'], 'live')
        self.assertEqual(traffic['sample_count'], 4)
        self.assertTrue(traffic['route_intersections'])
        self.assertTrue(traffic['path_crossings'])
        self.assertEqual({item['origin'] for item in traffic['cells']}, {'live'})
        status, filtered = self.request(
            'GET',
            '/api/traffic?window_hours=1&drone_id=SP-DRONE-01&mission=DEL-TRAFFIC-M3',
        )
        self.assertEqual(status, 200)
        self.assertEqual(filtered['sample_count'], 2)
        self.assertEqual(filtered['filters']['drone_id'], 'SP-DRONE-01')
        self.assertEqual(filtered['delivery_counts']['active'], 1)
        self.assertEqual(filtered['deliveries'][0]['planned_distance_km'], 1.2)
        self.assertEqual(filtered['deliveries'][0]['pickup'], {'lat': 43.65, 'lng': -79.38})
        status, wide_grid = self.request(
            'GET',
            '/api/traffic?window_hours=1&cell_size_deg=0.02',
        )
        self.assertEqual(status, 200)
        self.assertEqual(wide_grid['cell_size_deg'], 0.02)

    def test_traffic_rejects_unbounded_or_invalid_windows(self):
        for query in ('window_hours=0', 'window_hours=8760', 'window_hours=bad'):
            with self.subTest(query=query):
                status, payload = self.request('GET', f'/api/traffic?{query}')
                self.assertEqual(status, 400)
                self.assertIn('window_hours', payload['error'])

    def test_digital_twin_default_scenario_has_deterministic_route_intersection(self):
        status, twin = self.request('GET', '/api/twin')
        self.assertEqual(status, 200)
        self.assertEqual(twin['origin'], 'simulated')
        self.assertEqual(len(twin['scenarios']), 1)
        self.assertTrue(twin['route_intersections'])
        self.assertEqual(twin['route_intersections'][0]['drone_ids'], ['VIRTUAL-01', 'VIRTUAL-02'])
        self.assertIn('not official aviation restrictions', twin['warning'])

    def test_twin_configuration_requires_admin_and_persists_valid_virtual_zone(self):
        updated_config = {
            'corridors': [{
                'id': 'CORRIDOR-TEST',
                'name': 'Test virtual corridor',
                'path': [{'lat': 43.0, 'lng': -79.0}, {'lat': 43.1, 'lng': -79.1}],
            }],
            'zones': [{
                'id': 'ZONE-TEST',
                'name': 'Test zone',
                'center': {'lat': 43.0, 'lng': -79.0},
                'radius_m': 150,
            }],
            'restrictions': [{
                'zone_id': 'ZONE-TEST',
                'restricted': True,
                'reason': 'Test-only virtual restriction',
            }],
            'scenarios': [],
        }
        status, _ = self.request('PUT', '/api/twin/config', updated_config)
        self.assertEqual(status, 401)
        status, response = self.request(
            'PUT', '/api/twin/config', updated_config, self.admin_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(response['corridors'][0]['id'], 'CORRIDOR-TEST')
        self.assertEqual(response['zones'][0]['radius_m'], 150)
        status, response = self.request(
            'PUT',
            '/api/twin/config',
            {**updated_config, 'zones': [{**updated_config['zones'][0], 'radius_m': 9000}]},
            self.admin_headers,
        )
        self.assertEqual(status, 400)
        self.assertIn('radius', response['error'])
        status, response = self.request('GET', '/api/twin')
        self.assertEqual(status, 200)
        self.assertEqual(response['corridors'][0]['id'], 'CORRIDOR-TEST')

    def test_delivery_handover_needs_admin_and_arrival_evidence_state(self):
        api.create_delivery_record(
            'DEL-AUTH-M3',
            'SP-DRONE-03',
            {'lat': 43.6500, 'lng': -79.3800},
            {'lat': 43.6510, 'lng': -79.3810},
        )
        for status_name in ('ASSIGNED', 'PICKUP_CONFIRMED', 'IN_TRANSIT', 'ARRIVED'):
            api.update_delivery_status('DEL-AUTH-M3', status_name)
        payload = {
            'delivery_id': 'DEL-AUTH-M3',
            'drone_id': 'SP-DRONE-03',
            'handover_confirmed': True,
            'confirmed_by': 'client-supplied-name-is-not-trusted',
        }
        status, _ = self.request('POST', '/api/delivery/confirm', payload)
        self.assertEqual(status, 401)
        status, result = self.request(
            'POST', '/api/delivery/confirm', payload, self.admin_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'confirmed')
        conn = store.get_connection()
        try:
            row = conn.execute(
                'SELECT status, handover_confirmed_by FROM deliveries WHERE delivery_id = ?',
                ('DEL-AUTH-M3',),
            ).fetchone()
            outbox = conn.execute(
                'SELECT state FROM notification_outbox WHERE delivery_id = ?',
                ('DEL-AUTH-M3',),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row['status'], 'DELIVERED')
        self.assertEqual(row['handover_confirmed_by'], 'test-admin')
        self.assertIsNotNone(outbox)

    def test_wsgi_passes_authorization_and_query_to_api(self):
        headers = {}
        environ = {
            'REQUEST_METHOD': 'GET',
            'PATH_INFO': '/api/traffic',
            'QUERY_STRING': 'window_hours=6',
            'CONTENT_LENGTH': '0',
            'wsgi.input': BytesIO(),
        }

        def start_response(status, response_headers):
            headers['status'] = status

        response = b''.join(application(environ, start_response))
        self.assertEqual(headers['status'], '200 OK')
        self.assertEqual(json.loads(response)['window_hours'], 6)

    def test_wsgi_forwards_cookie_and_csrf_for_device_enrollment(self):
        body = json.dumps({
            'device_id': 'phone-wsgi-admin',
            'drone_id': 'SP-DRONE-04',
        }).encode()
        captured = {}
        environ = {
            'REQUEST_METHOD': 'POST',
            'PATH_INFO': '/api/device/register',
            'QUERY_STRING': '',
            'CONTENT_LENGTH': str(len(body)),
            'HTTP_COOKIE': self.admin_session['Cookie'],
            'HTTP_X_CSRF_TOKEN': self.admin_session['X-CSRF-Token'],
            'wsgi.input': BytesIO(body),
        }

        def start_response(status, response_headers):
            captured['status'] = status

        response = b''.join(application(environ, start_response))
        self.assertEqual(captured['status'], '200 OK')
        self.assertEqual(json.loads(response)['device_id'], 'phone-wsgi-admin')


if __name__ == '__main__':
    unittest.main()
