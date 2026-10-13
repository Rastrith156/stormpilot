"""StormPilot fleet operations API with a legacy Redmine proxy."""
import hashlib
import hmac
import json
import math
import os
import secrets
import smtplib
import sqlite3
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from app import db as store
except ImportError:  # pragma: no cover
    import db as store

DEMO_DRONES = [
    {
        'drone_id': 'SP-DRONE-01',
        'color': '#2dd4bf',
        'operation_mode': 'SIMULATED',
        'status': 'online',
        'position': {'latitude': 43.6562, 'longitude': -79.3795},
        'source': 'SIMULATED',
        'heading': 32,
        'accuracy_m': 12,
        'last_update': '2026-10-13T12:00:00Z',
        'mission': 'DEL-1001',
        'destination': 'Toronto City Hall',
        'planned_route_km': 7.4,
        'actual_distance_km': 2.8,
        'remaining_distance_km': 4.6,
        'battery_pct': 82,
        'delivery_status': 'IN_TRANSIT',
    },
    {
        'drone_id': 'SP-DRONE-02',
        'color': '#a78bfa',
        'operation_mode': 'PHONE_GPS',
        'status': 'tracking',
        'position': {'latitude': 43.6521, 'longitude': -79.3834},
        'source': 'PHONE_GPS',
        'heading': 120,
        'accuracy_m': 9,
        'last_update': '2026-10-13T12:00:12Z',
        'mission': 'DEL-1002',
        'destination': 'Distillery District',
        'planned_route_km': 6.1,
        'actual_distance_km': 3.1,
        'remaining_distance_km': 3.0,
        'battery_pct': 76,
        'delivery_status': 'ASSIGNED',
    },
    {
        'drone_id': 'SP-DRONE-03',
        'color': '#fbbf24',
        'operation_mode': 'SIMULATED',
        'status': 'staged',
        'position': {'latitude': 43.6474, 'longitude': -79.3915},
        'source': 'SIMULATED',
        'heading': 214,
        'accuracy_m': 15,
        'last_update': '2026-10-13T11:58:56Z',
        'mission': 'DEL-1003',
        'destination': "Ripley's Aquarium",
        'planned_route_km': 8.1,
        'actual_distance_km': 1.9,
        'remaining_distance_km': 6.2,
        'battery_pct': 91,
        'delivery_status': 'CREATED',
    },
    {
        'drone_id': 'SP-DRONE-04',
        'color': '#4ade80',
        'operation_mode': 'OFFLINE',
        'status': 'offline',
        'position': {'latitude': 43.6408, 'longitude': -79.4001},
        'source': 'OFFLINE',
        'heading': 0,
        'accuracy_m': None,
        'last_update': '2026-10-13T11:45:00Z',
        'mission': 'DEL-1004',
        'destination': 'Union Station',
        'planned_route_km': 9.3,
        'actual_distance_km': 0.0,
        'remaining_distance_km': 9.3,
        'battery_pct': None,
        'delivery_status': 'FAILED',
    },
]

TELEMETRY_STORE = {}
RATE_LIMITS = {}


def get_storage_path():
    return store.get_storage_path()


def list_tables():
    return store.list_tables()


def initialize_storage(force_reset=False):
    store.initialize_storage(force_reset=force_reset)
    _bootstrap_demo_drones()
    return get_storage_path()


def _bootstrap_demo_drones():
    conn = store.get_connection()
    try:
        for drone in DEMO_DRONES:
            conn.execute(
                '''
                INSERT INTO drone_registrations (
                    drone_id, device_id, source, latitude, longitude, gps_accuracy, last_update, status, mission_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(drone_id) DO UPDATE SET
                    device_id = excluded.device_id,
                    source = excluded.source,
                    latitude = excluded.latitude,
                    longitude = excluded.longitude,
                    gps_accuracy = excluded.gps_accuracy,
                    last_update = excluded.last_update,
                    status = excluded.status,
                    mission_id = excluded.mission_id,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                ''',
                (
                    drone['drone_id'],
                    None,
                    drone.get('source', 'SIMULATED'),
                    drone['position']['latitude'],
                    drone['position']['longitude'],
                    drone.get('accuracy_m'),
                    drone.get('last_update'),
                    drone.get('status', 'online'),
                    drone.get('mission'),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def get_latest_event_for(entity_id):
    return store.get_latest_event_for(entity_id)


def create_delivery_record(delivery_id, drone_id, pickup, destination, planned_route_km=0.0, priority='normal', deadline=None):
    created = _utc_now()
    status = 'CREATED'
    conn = store.get_connection()
    try:
        with conn:
            conn.execute(
                '''
                INSERT INTO deliveries (
                    delivery_id, drone_id, status, pickup_lat, pickup_lng, destination_lat,
                    destination_lng, planned_route_km, created_at, updated_at, current_evidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(delivery_id) DO NOTHING
                ''',
                (
                    delivery_id,
                    drone_id,
                    status,
                    float(pickup.get('lat', 0.0)),
                    float(pickup.get('lng', 0.0)),
                    float(destination.get('lat', 0.0)),
                    float(destination.get('lng', 0.0)),
                    float(planned_route_km or 0.0),
                    created,
                    created,
                    json.dumps({'priority': priority, 'deadline': deadline}, separators=(',', ':'), sort_keys=True),
                ),
            )
            conn.execute(
                'INSERT INTO delivery_state_history (delivery_id, previous_status, new_status, reason, evidence) VALUES (?, ?, ?, ?, ?)',
                (delivery_id, None, status, 'created', json.dumps({'priority': priority, 'deadline': deadline}, separators=(',', ':'), sort_keys=True)),
            )
            conn.execute(
                'INSERT INTO event_log (id, event_type, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)',
                (
                    f'delivery-create-{delivery_id}',
                    'DELIVERY_CREATED',
                    'DELIVERY',
                    delivery_id,
                    json.dumps({'drone_id': drone_id, 'pickup': pickup, 'destination': destination, 'planned_route_km': planned_route_km, 'priority': priority, 'deadline': deadline}, separators=(',', ':'), sort_keys=True),
                ),
            )
        row = conn.execute('SELECT * FROM deliveries WHERE delivery_id = ?', (delivery_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else {'delivery_id': delivery_id, 'status': status}


def update_delivery_status(delivery_id, new_status, reason='status change', evidence=None):
    allowed = {
        'CREATED': {'ASSIGNED', 'CANCELLED', 'FAILED'},
        'ASSIGNED': {'PICKUP_CONFIRMED', 'CANCELLED', 'FAILED'},
        'PICKUP_CONFIRMED': {'IN_TRANSIT', 'CANCELLED', 'FAILED'},
        'IN_TRANSIT': {'ARRIVED', 'CANCELLED', 'FAILED'},
        'ARRIVED': {'DELIVERED', 'CANCELLED', 'FAILED'},
        'DELIVERED': set(),
        'CANCELLED': set(),
        'FAILED': set(),
    }
    conn = store.get_connection()
    try:
        row = conn.execute('SELECT * FROM deliveries WHERE delivery_id = ?', (delivery_id,)).fetchone()
        if row is None:
            raise ValueError(f'Unknown delivery ID: {delivery_id}')
        current_status = row['status']
        if new_status not in allowed.get(current_status, set()):
            raise ValueError(f'Invalid delivery transition from {current_status} to {new_status}')
        updated = _utc_now()
        with conn:
            conn.execute(
                'UPDATE deliveries SET status = ?, updated_at = ?, arrival_detected_at = COALESCE(arrival_detected_at, ?), delivered_at = CASE WHEN ? = "DELIVERED" THEN ? ELSE delivered_at END WHERE delivery_id = ?',
                (new_status, updated, updated if new_status == 'ARRIVED' else None, new_status, updated if new_status == 'DELIVERED' else None, delivery_id),
            )
            conn.execute(
                'INSERT INTO delivery_state_history (delivery_id, previous_status, new_status, reason, evidence) VALUES (?, ?, ?, ?, ?)',
                (delivery_id, current_status, new_status, reason, json.dumps(evidence or {}, separators=(',', ':'), sort_keys=True)),
            )
            conn.execute(
                'INSERT INTO event_log (id, event_type, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)',
                (
                    f'delivery-state-{delivery_id}-{updated}',
                    'DELIVERY_STATE_CHANGED',
                    'DELIVERY',
                    delivery_id,
                    json.dumps({'previous_status': current_status, 'new_status': new_status, 'reason': reason, 'evidence': evidence or {}}, separators=(',', ':'), sort_keys=True),
                ),
            )
            if new_status == 'DELIVERED':
                _enqueue_notification_on_connection(
                    conn,
                    'DELIVERY_COMPLETED',
                    delivery_id,
                    {'delivery_id': delivery_id, 'drone_id': row['drone_id']},
                    delivery_id,
                )
        updated_row = conn.execute('SELECT * FROM deliveries WHERE delivery_id = ?', (delivery_id,)).fetchone()
    finally:
        conn.close()
    return dict(updated_row) if updated_row is not None else {'delivery_id': delivery_id, 'status': new_status}


def record_delivery_confirmation(delivery_id, drone_id, confirmed_by='operator', handover_confirmed=False):
    conn = store.get_connection()
    try:
        existing = conn.execute('SELECT * FROM deliveries WHERE delivery_id = ?', (delivery_id,)).fetchone()
        if existing is not None and existing['handover_confirmed']:
            return {'status': 'already_confirmed', 'delivery_id': delivery_id, 'drone_id': existing['drone_id']}
        if existing is None:
            with conn:
                conn.execute(
                    'INSERT INTO deliveries (delivery_id, drone_id, status, created_at, updated_at, handover_confirmed, handover_confirmed_by) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (delivery_id, drone_id, 'CREATED', _utc_now(), _utc_now(), int(bool(handover_confirmed)), confirmed_by),
                )
                conn.execute(
                    'INSERT INTO event_log (id, event_type, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)',
                    (
                        f'delivery-confirm-{delivery_id}',
                        'DELIVERY_CONFIRMED',
                        'DELIVERY',
                        delivery_id,
                        json.dumps({'drone_id': drone_id, 'confirmed_by': confirmed_by, 'handover_confirmed': bool(handover_confirmed)}, separators=(',', ':'), sort_keys=True),
                    ),
                )
            existing = conn.execute('SELECT * FROM deliveries WHERE delivery_id = ?', (delivery_id,)).fetchone()
        status = 'confirmed'
        with conn:
            if handover_confirmed:
                current = dict(existing)
                target = 'DELIVERED' if current['status'] in {'ARRIVED', 'IN_TRANSIT', 'ASSIGNED', 'PICKUP_CONFIRMED'} else current['status']
                conn.execute(
                    'UPDATE deliveries SET handover_confirmed = 1, handover_confirmed_by = ?, status = ?, updated_at = ?, delivered_at = CASE WHEN ? = "DELIVERED" THEN ? ELSE delivered_at END WHERE delivery_id = ?',
                    (confirmed_by, target, _utc_now(), target, _utc_now() if target == 'DELIVERED' else None, delivery_id),
                )
                conn.execute(
                    'INSERT INTO event_log (id, event_type, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)',
                    (
                        f'delivery-handed-off-{delivery_id}',
                        'DELIVERY_COMPLETED',
                        'DELIVERY',
                        delivery_id,
                        json.dumps({'drone_id': drone_id, 'confirmed_by': confirmed_by, 'handover_confirmed': True}, separators=(',', ':'), sort_keys=True),
                    ),
                )
                status = 'confirmed'
        updated = conn.execute('SELECT * FROM deliveries WHERE delivery_id = ?', (delivery_id,)).fetchone()
        return {'status': status, 'delivery_id': delivery_id, 'drone_id': updated['drone_id'], 'handover_confirmed': bool(updated['handover_confirmed'])}
    finally:
        conn.close()


def handle(method, path, body=None, headers=None):
    if method == 'GET' and path == '/api/config':
        return 200, {
            'google_maps_api_key': os.getenv('GOOGLE_MAPS_API_KEY', '').strip(),
            'demo_mode': True,
            'map_ready': bool(os.getenv('GOOGLE_MAPS_API_KEY', '').strip()),
        }

    if method == 'GET' and path == '/api/fleet':
        return 200, _fleet_payload()

    if method == 'POST' and path == '/api/device/register':
        return _handle_device_register(body)

    if method == 'DELETE' and path == '/api/device/revoke':
        return _handle_device_revoke(body)

    if method == 'POST' and path == '/api/device/telemetry':
        return _handle_telemetry(body, headers=headers)

    if method == 'POST' and path == '/api/delivery/confirm':
        return _handle_delivery_confirm(body)

    if method == 'POST' and path == '/api/email/test':
        return _handle_email_test(body)

    if method == 'GET' and path == '/api/redmine':
        return _handle_redmine()

    if method == 'GET' and path.startswith('/api/'):
        return 404, {'error': 'not found'}

    return 404, {'error': 'not found'}


def _fleet_payload():
    drones = []
    for drone in DEMO_DRONES:
        current = dict(drone)
        current['position'] = dict(drone['position'])
        tracked = TELEMETRY_STORE.get(drone['drone_id'])
        if tracked:
            current['position'] = {'latitude': tracked['latitude'], 'longitude': tracked['longitude']}
            current['source'] = tracked['source']
            current['accuracy_m'] = tracked['accuracy_m']
            current['last_update'] = tracked['last_update']
            current['status'] = tracked['status']
        drones.append(current)
    analytics = {
        'active_drones': 2,
        'active_deliveries': 2,
        'completed_deliveries': 1,
        'total_observed_distance_km': 18.7,
        'average_delivery_duration_minutes': 44,
        'delivery_success_rate': 0.86,
        'traffic_density': 'moderate',
    }
    return {'drones': drones, 'analytics': analytics}


def _handle_telemetry(body, headers=None):
    data = _read_json(body)
    if not isinstance(data, dict):
        return 400, {'error': 'Invalid GPS payload. Expected a JSON object.'}

    drone_id = str(data.get('drone_id', '')).strip()
    device_id = str(data.get('device_id', '')).strip()
    latitude = _float(data.get('lat'))
    longitude = _float(data.get('lng'))
    accuracy_m = _float(data.get('accuracy'))
    timestamp = data.get('timestamp')
    auth_token = _extract_bearer_token(headers)

    if (not drone_id or latitude is None or longitude is None or accuracy_m is None or not math.isfinite(accuracy_m) or accuracy_m <= 0 or not timestamp):
        return 400, {'error': 'Invalid GPS payload. Latitude, longitude, accuracy and timestamp are required.'}
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return 400, {'error': 'Invalid GPS payload. Coordinates are outside valid Earth bounds.'}

    if not device_id or not auth_token:
        return 401, {'error': 'Authentication required for telemetry submission.'}

    device = _authenticate_device(device_id, auth_token)
    if not device or device['drone_id'] != drone_id:
        return 401, {'error': 'Device is not authorized for this drone.'}

    if _is_rate_limited(device_id):
        return 429, {'error': 'Telemetry rate limit exceeded. Please wait a moment and retry.'}

    try:
        parsed = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        iso = parsed.isoformat().replace('+00:00', 'Z')
    except ValueError:
        return 400, {'error': 'Invalid GPS payload. Timestamp must be a valid ISO 8601 value.'}

    valid_ids = {drone['drone_id'] for drone in DEMO_DRONES}
    if drone_id not in valid_ids:
        return 403, {'error': 'Unknown drone ID.'}

    now = datetime.now(timezone.utc)
    if (now - parsed).total_seconds() > 300:
        return 400, {'error': 'Telemetry sample is stale and cannot be accepted.'}
    if (parsed - now).total_seconds() > 30:
        return 400, {'error': 'Telemetry sample timestamp is too far in the future.'}

    distance_m = 0.0
    arrival_detected = False
    conn = store.get_connection()
    try:
        registration = conn.execute(
            'SELECT * FROM device_registrations WHERE device_id = ? AND drone_id = ?',
            (device_id, drone_id),
        ).fetchone()
        if registration is None:
            conn.execute(
                'INSERT INTO device_registrations(device_id, drone_id, status, source, created_at, updated_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (device_id, drone_id, 'active', 'PHONE_GPS', _utc_now(), _utc_now(), _utc_now()),
            )
        else:
            conn.execute(
                'UPDATE device_registrations SET status = ?, source = ?, updated_at = ?, last_seen = ? WHERE device_id = ?',
                ('active', 'PHONE_GPS', _utc_now(), _utc_now(), device_id),
            )

        prior = conn.execute(
            'SELECT * FROM telemetry_samples WHERE drone_id = ? AND status = ? ORDER BY device_timestamp DESC LIMIT 1',
            (drone_id, 'ACCEPTED'),
        ).fetchone()
        quality_flags = []
        if accuracy_m > 25:
            quality_flags.append('LOW_CONFIDENCE')
        if prior is not None:
            prior_time = datetime.fromisoformat(prior['device_timestamp'].replace('Z', '+00:00')).astimezone(timezone.utc)
            if parsed <= prior_time:
                conn.execute(
                    'INSERT INTO telemetry_samples (drone_id, device_id, latitude, longitude, accuracy, device_timestamp, server_received_at, source, status, quality_flags, rejection_reason, accepted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (drone_id, device_id or None, latitude, longitude, accuracy_m, iso, _utc_now(), 'PHONE_GPS', 'REJECTED', json.dumps(quality_flags), 'duplicate_or_out_of_order', 0),
                )
                conn.commit()
                store.record_event('TELEMETRY_REJECTED', 'DRONE', drone_id, {'reason': 'duplicate_or_out_of_order', 'device_id': device_id})
                return 400, {'error': 'Telemetry sample is duplicate or out of order.'}
            distance_m = _haversine(previous_lat=prior['latitude'], previous_lng=prior['longitude'], latitude=latitude, longitude=longitude)
            elapsed_seconds = max((parsed - prior_time).total_seconds(), 1)
            speed_mps = distance_m / elapsed_seconds
            if speed_mps > 50 and accuracy_m < 40:
                conn.execute(
                    'INSERT INTO telemetry_samples (drone_id, device_id, latitude, longitude, accuracy, device_timestamp, server_received_at, source, status, quality_flags, rejection_reason, accepted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (drone_id, device_id or None, latitude, longitude, accuracy_m, iso, _utc_now(), 'PHONE_GPS', 'REJECTED', json.dumps(quality_flags), 'implausible_speed', 0),
                )
                conn.commit()
                store.record_event('TELEMETRY_REJECTED', 'DRONE', drone_id, {'reason': 'implausible_speed', 'device_id': device_id})
                return 400, {'error': 'Telemetry sample is implausible for the drone speed envelope.'}
            if distance_m > 2500 and elapsed_seconds < 30:
                quality_flags.append('GPS_JUMP')

        tracked_distance_m = 0.0 if 'GPS_JUMP' in quality_flags else distance_m

        conn.execute(
            '''
            INSERT INTO telemetry_samples (
                drone_id, device_id, latitude, longitude, accuracy, device_timestamp,
                server_received_at, source, status, quality_flags, rejection_reason, accepted, distance_m
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                drone_id,
                device_id or None,
                latitude,
                longitude,
                accuracy_m,
                iso,
                _utc_now(),
                'PHONE_GPS',
                'ACCEPTED',
                json.dumps(quality_flags),
                None,
                1,
                distance_m,
            ),
        )
        conn.execute(
            "UPDATE deliveries SET actual_observed_km = COALESCE(actual_observed_km, 0) + ? WHERE drone_id = ? AND status = 'IN_TRANSIT'",
            (tracked_distance_m / 1000.0, drone_id),
        )
        conn.execute(
            'UPDATE drone_registrations SET latitude = ?, longitude = ?, gps_accuracy = ?, last_update = ?, status = ?, source = ?, updated_at = ? WHERE drone_id = ?',
            (latitude, longitude, accuracy_m, iso, 'tracking', 'PHONE_GPS', _utc_now(), drone_id),
        )
        conn.execute(
            'UPDATE device_credentials SET last_seen = ? WHERE device_id = ?',
            (_utc_now(), device_id),
        )

        radius_m = _float(os.getenv('ARRIVAL_GEOFENCE_RADIUS_M', '50'))
        if radius_m is None or radius_m <= 0:
            radius_m = 50.0
        in_transit = conn.execute(
            "SELECT * FROM deliveries WHERE drone_id = ? AND status = 'IN_TRANSIT' AND destination_lat IS NOT NULL AND destination_lng IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
            (drone_id,),
        ).fetchone()
        if in_transit is not None:
            destination_distance_m = _haversine(
                previous_lat=in_transit['destination_lat'],
                previous_lng=in_transit['destination_lng'],
                latitude=latitude,
                longitude=longitude,
            )
            if accuracy_m <= radius_m and destination_distance_m <= radius_m:
                arrival_detected = True
                arrival_evidence = {
                    'distance_to_destination_m': round(destination_distance_m, 2),
                    'reported_accuracy_m': accuracy_m,
                    'geofence_radius_m': radius_m,
                    'telemetry_timestamp': iso,
                }
                now = _utc_now()
                conn.execute(
                    "UPDATE deliveries SET status = 'ARRIVED', arrival_detected_at = ?, arrival_evidence = ?, current_evidence = ?, updated_at = ? WHERE delivery_id = ? AND status = 'IN_TRANSIT'",
                    (now, json.dumps(arrival_evidence, separators=(',', ':'), sort_keys=True), json.dumps(arrival_evidence, separators=(',', ':'), sort_keys=True), now, in_transit['delivery_id']),
                )
                conn.execute(
                    'INSERT INTO delivery_state_history (delivery_id, previous_status, new_status, reason, evidence) VALUES (?, ?, ?, ?, ?)',
                    (in_transit['delivery_id'], 'IN_TRANSIT', 'ARRIVED', 'gps_geofence', json.dumps(arrival_evidence, separators=(',', ':'), sort_keys=True)),
                )
                conn.execute(
                    'INSERT INTO event_log (id, event_type, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)',
                    (f'delivery-arrival-{in_transit["delivery_id"]}-{now}', 'DELIVERY_ARRIVED', 'DELIVERY', in_transit['delivery_id'], json.dumps(arrival_evidence, separators=(',', ':'), sort_keys=True)),
                )
        conn.commit()
        TELEMETRY_STORE[drone_id] = {
            'drone_id': drone_id,
            'latitude': latitude,
            'longitude': longitude,
            'accuracy_m': accuracy_m,
            'last_update': iso,
            'source': 'PHONE_GPS',
            'status': 'tracking',
        }
        _record_rate_limit(device_id)
        store.record_event('TELEMETRY_ACCEPTED', 'DRONE', drone_id, {'device_id': device_id, 'accuracy_m': accuracy_m, 'distance_m': round(distance_m, 2), 'quality_flags': quality_flags})
        return 200, {'status': 'accepted', 'drone_id': drone_id, 'timestamp': iso, 'accuracy_m': accuracy_m, 'distance_m': round(distance_m, 2), 'arrival_detected': arrival_detected}
    except sqlite3.DatabaseError:
        return 500, {'error': 'Telemetry storage is temporarily unavailable.'}
    finally:
        conn.close()


def _handle_device_register(body):
    data = _read_json(body)
    if not isinstance(data, dict):
        return 400, {'error': 'Invalid device registration payload.'}
    device_id = str(data.get('device_id', '')).strip() or f'device-{secrets.token_hex(8)}'
    drone_id = str(data.get('drone_id', '')).strip()
    valid_ids = {drone['drone_id'] for drone in DEMO_DRONES}
    if drone_id not in valid_ids:
        return 400, {'error': 'Unsupported drone ID for device registration.'}

    secret = secrets.token_urlsafe(32)
    salt = secrets.token_hex(16)
    digest = _hash_secret(secret, salt)
    conn = store.get_connection()
    try:
        conn.execute(
            '''
            INSERT INTO device_credentials (device_id, drone_id, secret_hash, secret_salt, status, created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                drone_id = excluded.drone_id,
                secret_hash = excluded.secret_hash,
                secret_salt = excluded.secret_salt,
                status = 'ACTIVE',
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at,
                revoked_reason = NULL
            ''',
            (device_id, drone_id, digest, salt, _utc_now(), _utc_now(), _utc_now_plus_days(30)),
        )
        conn.execute(
            '''
            INSERT INTO device_registrations (device_id, drone_id, status, source, created_at, updated_at, last_seen, expires_at)
            VALUES (?, ?, 'active', 'PHONE_GPS', ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                drone_id = excluded.drone_id,
                status = 'active',
                source = excluded.source,
                updated_at = excluded.updated_at,
                last_seen = excluded.last_seen,
                expires_at = excluded.expires_at
            ''',
            (device_id, drone_id, _utc_now(), _utc_now(), _utc_now(), _utc_now_plus_days(30)),
        )
        conn.commit()
        store.record_event('DEVICE_REGISTERED', 'DEVICE', device_id, {'drone_id': drone_id})
    finally:
        conn.close()
    return 200, {'device_id': device_id, 'drone_id': drone_id, 'token': secret, 'status': 'active'}


def _handle_device_revoke(body):
    data = _read_json(body)
    if not isinstance(data, dict):
        return 400, {'error': 'Invalid device revocation payload.'}
    device_id = str(data.get('device_id', '')).strip()
    if not device_id:
        return 400, {'error': 'Device ID is required.'}
    conn = store.get_connection()
    try:
        conn.execute('UPDATE device_credentials SET status = ?, revoked_reason = ?, updated_at = ? WHERE device_id = ?', ('REVOKED', str(data.get('reason', 'revoked')).strip() or 'revoked', _utc_now(), device_id))
        conn.execute('UPDATE device_registrations SET status = ?, revocation_reason = ?, updated_at = ? WHERE device_id = ?', ('revoked', str(data.get('reason', 'revoked')).strip() or 'revoked', _utc_now(), device_id))
        conn.commit()
        store.record_event('DEVICE_REVOKED', 'DEVICE', device_id, {'reason': str(data.get('reason', 'revoked')).strip() or 'revoked'})
        return 200, {'status': 'revoked', 'device_id': device_id}
    finally:
        conn.close()


def _handle_delivery_confirm(body):
    data = _read_json(body)
    if not isinstance(data, dict):
        return 400, {'error': 'Invalid delivery confirmation payload.'}

    delivery_id = str(data.get('delivery_id', '')).strip()
    drone_id = str(data.get('drone_id', '')).strip()
    if not delivery_id or not drone_id:
        return 400, {'error': 'Delivery ID and drone ID are required.'}

    conn = store.get_connection()
    try:
        existing = conn.execute('SELECT * FROM deliveries WHERE delivery_id = ?', (delivery_id,)).fetchone()
        if existing is not None and existing['drone_id'] != drone_id:
            return 409, {'error': 'Delivery is assigned to a different drone.'}
        if existing is not None and existing['handover_confirmed']:
            return 200, {'status': 'already_confirmed', 'delivery_id': delivery_id, 'drone_id': existing['drone_id']}
        if data.get('handover_confirmed', False) and existing is not None and existing['status'] != 'ARRIVED':
            return 409, {'error': 'Delivery handover can only be confirmed after arrival.'}

        timestamp = _utc_now()
        confirmed_by = str(data.get('confirmed_by', 'operator')).strip() or 'operator'
        with conn:
            if existing is None:
                conn.execute(
                    'INSERT INTO deliveries (delivery_id, drone_id, status, created_at, updated_at, handover_confirmed_by) VALUES (?, ?, ?, ?, ?, ?)',
                    (delivery_id, drone_id, 'CREATED', timestamp, timestamp, confirmed_by),
                )
                conn.execute(
                    'INSERT INTO delivery_state_history (delivery_id, previous_status, new_status, reason, evidence) VALUES (?, ?, ?, ?, ?)',
                    (delivery_id, None, 'CREATED', 'created_by_confirmation', '{}'),
                )
                previous_status = 'CREATED'
            else:
                previous_status = existing['status']

            if data.get('handover_confirmed', False):
                conn.execute(
                    'UPDATE deliveries SET handover_confirmed = 1, handover_confirmed_by = ?, status = "DELIVERED", updated_at = ?, delivered_at = ? WHERE delivery_id = ?',
                    (confirmed_by, timestamp, timestamp, delivery_id),
                )
                confirmation_evidence = {'drone_id': drone_id, 'confirmed_by': confirmed_by, 'handover_confirmed': True}
                conn.execute(
                    'INSERT INTO delivery_state_history (delivery_id, previous_status, new_status, reason, evidence) VALUES (?, ?, ?, ?, ?)',
                    (delivery_id, previous_status, 'DELIVERED', 'handover_confirmed', json.dumps(confirmation_evidence, separators=(',', ':'), sort_keys=True)),
                )
            conn.execute(
                'INSERT INTO event_log (id, event_type, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)',
                (
                    f'delivery-confirm-{delivery_id}-{timestamp}',
                    'DELIVERY_CONFIRMED',
                    'DELIVERY',
                    delivery_id,
                    json.dumps({'drone_id': drone_id, 'confirmed_by': confirmed_by, 'handover_confirmed': bool(data.get('handover_confirmed', False))}, separators=(',', ':'), sort_keys=True),
                ),
            )
            if data.get('handover_confirmed', False):
                _enqueue_notification_on_connection(
                    conn,
                    'DELIVERY_COMPLETED',
                    delivery_id,
                    {'delivery_id': delivery_id, 'drone_id': drone_id},
                    delivery_id,
                )
        return 200, {'status': 'confirmed', 'delivery_id': delivery_id, 'drone_id': drone_id}
    except sqlite3.DatabaseError:
        return 500, {'error': 'Delivery confirmation storage is temporarily unavailable.'}
    finally:
        conn.close()


def _handle_email_test(body):
    data = _read_json(body) if body else {}
    if not isinstance(data, dict):
        data = {}

    smtp_host = os.getenv('SMTP_HOST', '').strip()
    smtp_port = int(os.getenv('SMTP_PORT', '587').strip() or '587')
    smtp_username = os.getenv('SMTP_USERNAME', '').strip()
    smtp_password = os.getenv('SMTP_APP_PASSWORD', '').strip()
    if not all([smtp_host, smtp_username, smtp_password]):
        return 200, {
            'status': 'warning',
            'message': 'SMTP credentials are not configured. The platform is still operating without Gmail delivery notifications.',
        }

    destination = str(data.get('to') or os.getenv('DELIVERY_NOTIFICATION_TO', '')).strip()
    if not destination:
        return 200, {'status': 'warning', 'message': 'No email destination was provided.'}

    message = EmailMessage()
    message['Subject'] = 'StormPilot Delivery Completed — TEST'
    message['From'] = smtp_username
    message['To'] = destination
    message.set_content('This is a StormPilot test message confirming the notification pipeline is configured.')
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
        return 200, {'status': 'sent', 'recipient': destination}
    except (OSError, smtplib.SMTPException, ValueError):
        return 200, {
            'status': 'warning',
            'message': 'SMTP delivery could not be completed. The app is still operating safely without sending the test notification.',
        }


def _read_json(body):
    if body in (None, b'', ''):
        return {}
    if isinstance(body, (bytes, bytearray)):
        body = body.decode('utf-8', 'replace')
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _haversine(previous_lat, previous_lng, latitude, longitude):
    radius_km = 6371.0
    lat1 = math.radians(previous_lat)
    lat2 = math.radians(latitude)
    delta_lat = math.radians(latitude - previous_lat)
    delta_lng = math.radians(longitude - previous_lng)
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_km * 1000 * c


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _handle_redmine():
    base = os.getenv('REDMINE_URL', '').strip().rstrip('/')
    api_key = os.getenv('REDMINE_API_KEY', '').strip()
    key_file = os.getenv('REDMINE_API_KEY_FILE')
    if key_file:
        try:
            with open(key_file, encoding='utf-8') as file:
                api_key = file.read().strip()
        except OSError:
            return 503, {'error': 'Redmine credentials are unavailable on the server.'}
    if not base or not api_key:
        return 200, {'configured': False}
    parsed = urlsplit(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return 200, {'configured': False, 'message': 'Redmine configuration is invalid.'}
    try:
        projects = _get(base, api_key, '/projects.json?limit=100')
        issues = _get(base, api_key, '/issues.json?status_id=open&limit=20&sort=updated_on%3Adesc')
        project_rows = _elements(projects, 'projects')
        issue_rows = _elements(issues, 'issues')
        result = {
            'configured': True,
            'instance_url': base,
            'projects': [_project(project, base) for project in project_rows],
            'issues': [_issue(issue, base) for issue in issue_rows],
            'counts': {
                'projects': projects.get('total_count', len(project_rows)),
                'issues': issues.get('total_count', len(issue_rows)),
            },
        }
        return 200, result
    except HTTPError as exc:
        if exc.code in (401, 403):
            message = 'Redmine rejected the API key. Check the key and REST API permissions.'
        else:
            message = 'Redmine could not complete the request. Check the instance and try again.'
        return 502, {'error': message}
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return 502, {'error': 'Redmine is unavailable or returned an invalid response. Try again shortly.'}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(base, api_key, path):
    request = Request(base + path, headers={
        'Accept': 'application/json',
        'X-Redmine-API-Key': api_key,
        'User-Agent': 'StormPilot/1.0',
    })
    with build_opener(_NoRedirect).open(request, timeout=5) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError('Redmine returned an unexpected response')
    return payload


def _elements(payload, key):
    rows = payload.get(key, [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _project(item, base):
    identifier = item.get('identifier') or item.get('id')
    return {
        'id': item.get('id'), 'name': item.get('name', 'Untitled project'),
        'description': item.get('description', ''), 'url': f'{base}/projects/{identifier}',
    }


def _issue(item, base):
    return {
        'id': item.get('id'), 'subject': item.get('subject', 'Untitled issue'),
        'tracker': _name(item.get('tracker')), 'status': _name(item.get('status')),
        'priority': _name(item.get('priority')), 'assignee': _name(item.get('assigned_to')),
        'project': _name(item.get('project')), 'updated_on': item.get('updated_on', ''),
        'url': f"{base}/issues/{item.get('id')}",
    }


def _name(value):
    return value.get('name', '') if isinstance(value, dict) else ''


def _hash_secret(secret, salt):
    return hashlib.pbkdf2_hmac('sha256', secret.encode('utf-8'), salt.encode('utf-8'), 200000).hex()


def _utc_now_plus_days(days=30):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace('+00:00', 'Z')


def _extract_bearer_token(headers):
    if not headers:
        return None
    auth = headers.get('Authorization') or headers.get('authorization')
    if not auth:
        return None
    token = str(auth).strip()
    if token.lower().startswith('bearer '):
        return token.split(None, 1)[1].strip()
    return token.strip() or None


def _authenticate_device(device_id, token):
    if not device_id or not token:
        return None
    conn = store.get_connection()
    try:
        row = conn.execute(
            'SELECT * FROM device_credentials WHERE device_id = ? AND status = ?',
            (device_id, 'ACTIVE'),
        ).fetchone()
        if row is None:
            return None
        expected = row['secret_hash']
        candidate = _hash_secret(token, row['secret_salt'])
        if not hmac.compare_digest(expected, candidate):
            return None
        if row['expires_at']:
            try:
                expires = datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                if expires < now:
                    return None
            except (TypeError, ValueError):
                return None
        return {'device_id': device_id, 'drone_id': row['drone_id']}
    finally:
        conn.close()


def _is_rate_limited(device_id):
    now = time.time()
    window = RATE_LIMITS.setdefault(device_id, [])
    window[:] = [item for item in window if now - item < 5]
    return len(window) >= 10


def _record_rate_limit(device_id):
    RATE_LIMITS.setdefault(device_id, []).append(time.time())


def _enqueue_notification_on_connection(conn, notification_type, entity_id, payload=None, delivery_id=None):
    idempotency = f'{notification_type}:{delivery_id or entity_id}:{json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))}'
    item_id = hashlib.sha256(idempotency.encode('utf-8')).hexdigest()
    row = conn.execute('SELECT * FROM notification_outbox WHERE idempotency_key = ?', (idempotency,)).fetchone()
    if row is not None:
        return {'id': row['id'], 'state': row['state'], 'delivery_id': row['delivery_id'], 'notification_type': row['notification_type']}
    now = _utc_now()
    conn.execute(
        'INSERT INTO notification_outbox (id, notification_type, entity_id, delivery_id, idempotency_key, state, retry_count, next_attempt_at, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (item_id, notification_type, str(entity_id), delivery_id, idempotency, 'PENDING', 0, now, now, now, json.dumps(payload or {}, separators=(',', ':'), sort_keys=True)),
    )
    return {'id': item_id, 'state': 'PENDING', 'delivery_id': delivery_id, 'notification_type': notification_type}


def enqueue_notification(notification_type, entity_id, payload=None, delivery_id=None):
    conn = store.get_connection()
    try:
        with conn:
            return _enqueue_notification_on_connection(conn, notification_type, entity_id, payload, delivery_id)
    finally:
        conn.close()


def process_notification_outbox(max_jobs=10):
    conn = store.get_connection()
    processed = []
    try:
        now = _utc_now()
        rows = conn.execute(
            "SELECT * FROM notification_outbox WHERE ((state = 'PENDING' OR (state = 'FAILED' AND retry_count < 3)) AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) OR (state = 'PROCESSING' AND julianday(updated_at) < julianday('now', '-5 minutes')) ORDER BY next_attempt_at LIMIT ?",
            (now, max_jobs),
        ).fetchall()
        for row in rows:
            with conn:
                claimed = conn.execute(
                    "UPDATE notification_outbox SET state = 'PROCESSING', updated_at = ? WHERE id = ? AND state = ?",
                    (_utc_now(), row['id'], row['state']),
                ).rowcount
            if not claimed:
                continue

            payload = _read_json(row['payload'])
            error = None
            try:
                settings = {
                    'host': os.getenv('SMTP_HOST', '').strip(),
                    'port': int(os.getenv('SMTP_PORT', '587').strip() or '587'),
                    'username': os.getenv('SMTP_USERNAME', '').strip(),
                    'password': os.getenv('SMTP_APP_PASSWORD', '').strip(),
                }
                recipient = str(payload.get('to') or os.getenv('DELIVERY_NOTIFICATION_TO', '')).strip()
                if not all((settings['host'], settings['username'], settings['password'])):
                    raise ValueError('SMTP not configured')
                if not recipient:
                    raise ValueError('notification recipient not configured')
                message = EmailMessage()
                message['Subject'] = f"StormPilot: {row['notification_type'].replace('_', ' ').title()}"
                message['From'] = settings['username']
                message['To'] = recipient
                message.set_content(
                    f"StormPilot notification for {row['entity_id']}.\n\n"
                    f"{json.dumps(payload, indent=2, sort_keys=True)}"
                )
                with smtplib.SMTP(settings['host'], settings['port'], timeout=10) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.login(settings['username'], settings['password'])
                    smtp.send_message(message)
            except (OSError, smtplib.SMTPException, ValueError) as exc:
                error = str(exc) if isinstance(exc, ValueError) else 'SMTP delivery failed'

            updated_at = _utc_now()
            if error is None:
                with conn:
                    conn.execute(
                        "UPDATE notification_outbox SET state = 'SENT', sent_at = ?, updated_at = ?, last_error = NULL WHERE id = ? AND state = 'PROCESSING'",
                        (updated_at, updated_at, row['id']),
                    )
                    if row['delivery_id']:
                        conn.execute('UPDATE deliveries SET notification_status = ? WHERE delivery_id = ?', ('SENT', row['delivery_id']))
                processed.append({'id': row['id'], 'state': 'SENT', 'delivery_id': row['delivery_id']})
            else:
                retry_count = row['retry_count'] + 1
                next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=min(60 * (2 ** (retry_count - 1)), 3600))).isoformat().replace('+00:00', 'Z')
                with conn:
                    conn.execute(
                        "UPDATE notification_outbox SET state = 'FAILED', retry_count = ?, next_attempt_at = ?, updated_at = ?, last_error = ? WHERE id = ? AND state = 'PROCESSING'",
                        (retry_count, next_attempt, updated_at, error, row['id']),
                    )
                    if row['delivery_id']:
                        conn.execute('UPDATE deliveries SET notification_status = ? WHERE delivery_id = ?', ('FAILED', row['delivery_id']))
                processed.append({'id': row['id'], 'state': 'FAILED', 'delivery_id': row['delivery_id'], 'message': error})
        return processed
    finally:
        conn.close()


initialize_storage()
