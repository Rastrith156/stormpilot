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
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from app import db as store
    from app import optimizer
    from app import security
except ImportError:  # pragma: no cover
    import db as store
    import optimizer
    import security

DRONE_COLORS = ('#2dd4bf', '#a78bfa', '#fbbf24', '#4ade80')
DEMO_DRONES = [
    {'drone_id': f'SP-DRONE-{number:02d}', 'color': color}
    for number, color in enumerate(DRONE_COLORS, start=1)
]
BENGALURU_DEMO_DELIVERIES = [
    {
        'drone_id': 'BLR-DEMO-01',
        'delivery_id': 'BLR-DEMO-DELIVERY-01',
        'area': 'Indiranagar',
        'position': {'latitude': 12.9719, 'longitude': 77.6412},
        'pickup': {'latitude': 12.9730, 'longitude': 77.6404},
        'destination': {'latitude': 12.9678, 'longitude': 77.6419},
        'status': 'IN_TRANSIT',
        'color': '#22d3ee',
    },
    {
        'drone_id': 'BLR-DEMO-02',
        'delivery_id': 'BLR-DEMO-DELIVERY-02',
        'area': 'MG Road',
        'position': {'latitude': 12.9757, 'longitude': 77.6033},
        'pickup': {'latitude': 12.9751, 'longitude': 77.6053},
        'destination': {'latitude': 12.9694, 'longitude': 77.6102},
        'status': 'PICKUP_CONFIRMED',
        'color': '#a78bfa',
    },
    {
        'drone_id': 'BLR-DEMO-03',
        'delivery_id': 'BLR-DEMO-DELIVERY-03',
        'area': 'Koramangala',
        'position': {'latitude': 12.9349, 'longitude': 77.6148},
        'pickup': {'latitude': 12.9354, 'longitude': 77.6135},
        'destination': {'latitude': 12.9387, 'longitude': 77.6171},
        'status': 'ASSIGNED',
        'color': '#fbbf24',
    },
    {
        'drone_id': 'BLR-DEMO-04',
        'delivery_id': 'BLR-DEMO-DELIVERY-04',
        'area': 'Jayanagar',
        'position': {'latitude': 12.9250, 'longitude': 77.5838},
        'pickup': {'latitude': 12.9241, 'longitude': 77.5835},
        'destination': {'latitude': 12.9302, 'longitude': 77.5846},
        'status': 'IN_TRANSIT',
        'color': '#4ade80',
    },
    {
        'drone_id': 'BLR-DEMO-05',
        'delivery_id': 'BLR-DEMO-DELIVERY-05',
        'area': 'Malleshwaram',
        'position': {'latitude': 13.0039, 'longitude': 77.5700},
        'pickup': {'latitude': 13.0044, 'longitude': 77.5710},
        'destination': {'latitude': 12.9978, 'longitude': 77.5709},
        'status': 'ARRIVED',
        'color': '#fb7185',
    },
]
BENGALURU_DEMO_HEAT_CELL_SIZE = 0.005
STALE_AFTER_SECONDS = 120
MAX_TRAFFIC_SAMPLES = 5000
MAX_PATH_POINTS = 100
MAX_NOTIFICATION_RETRIES = 3
RATE_LIMITS = {}
_RETENTION_LOCK = threading.Lock()
_LAST_RETENTION_PRUNE = 0.0
METRIC_DELIVERY_STATES = ('CREATED', 'ASSIGNED', 'PICKUP_CONFIRMED', 'IN_TRANSIT', 'ARRIVED', 'DELIVERED', 'CANCELLED', 'FAILED')
METRIC_OUTBOX_STATES = ('PENDING', 'PROCESSING', 'SENT', 'FAILED')
METRIC_REJECTION_REASONS = ('duplicate_or_out_of_order', 'implausible_speed', 'invalid_payload', 'stale_sample', 'unauthorized_device', 'other')
DEFAULT_TWIN_CONFIG = {
    'corridors': [{
        'id': 'CORRIDOR-CENTRAL',
        'name': 'Virtual central corridor',
        'path': [
            {'lat': 43.6500, 'lng': -79.3900},
            {'lat': 43.6520, 'lng': -79.3860},
            {'lat': 43.6540, 'lng': -79.3820},
        ],
    }],
    'zones': [{
        'id': 'ZONE-DEMO-01',
        'name': 'Operator demonstration zone',
        'center': {'lat': 43.6520, 'lng': -79.3860},
        'radius_m': 220,
    }],
    'restrictions': [{
        'zone_id': 'ZONE-DEMO-01',
        'restricted': True,
        'reason': 'Virtual scenario restriction; not an official aviation restriction.',
    }],
    'scenarios': [{
        'id': 'SHARED-CORRIDOR-DEMO',
        'name': 'Two virtual routes approaching a shared corridor',
        'routes': [
            {
                'drone_id': 'VIRTUAL-01',
                'path': [
                    {'lat': 43.6480, 'lng': -79.3920},
                    {'lat': 43.6500, 'lng': -79.3900},
                    {'lat': 43.6520, 'lng': -79.3860},
                ],
            },
            {
                'drone_id': 'VIRTUAL-02',
                'path': [
                    {'lat': 43.6560, 'lng': -79.3800},
                    {'lat': 43.6540, 'lng': -79.3820},
                    {'lat': 43.6520, 'lng': -79.3860},
                ],
            },
        ],
    }],
}


def get_storage_path():
    return store.get_storage_path()


def _prune_expired_telemetry_if_due(force=False):
    global _LAST_RETENTION_PRUNE
    now = time.monotonic()
    with _RETENTION_LOCK:
        if not force and now - _LAST_RETENTION_PRUNE < 3600:
            return
        configured = os.getenv('DATA_RETENTION_DAYS', '30').strip()
        try:
            retention_days = int(configured)
        except ValueError as error:
            raise ValueError('DATA_RETENTION_DAYS must be an integer from 1 to 3650.') from error
        if not 1 <= retention_days <= 3650:
            raise ValueError('DATA_RETENTION_DAYS must be an integer from 1 to 3650.')
        store.prune_expired_telemetry(retention_days)
        _LAST_RETENTION_PRUNE = now


def list_tables():
    return store.list_tables()


def readiness_status():
    try:
        conn = store.get_connection()
        try:
            version = conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] or 0
            required_tables = {
                'drone_registrations',
                'telemetry_samples',
                'deliveries',
                'notification_outbox',
                'device_credentials',
                'admin_users',
                'admin_sessions',
                'simulation_runs',
                'agile_sprints',
                'agile_tasks',
            }
            present = {
                row['name'] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = sorted(required_tables - present)
            if version < 5 or missing:
                return 503, {
                    'status': 'unready',
                    'storage': {
                        'ok': False,
                        'schema_version': version,
                        'missing_tables': missing,
                    },
                }
            pending_notifications = conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE state IN ('PENDING','FAILED')"
            ).fetchone()[0]
            return 200, {
                'status': 'ok',
                'storage': {
                    'ok': True,
                    'schema_version': version,
                },
                'notifications': {
                    'pending_or_retryable': pending_notifications,
                },
            }
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError, ValueError):
        return 503, {
            'status': 'unready',
            'storage': {
                'ok': False,
                'error': 'storage unavailable',
            },
        }


def prometheus_metrics(request_counts=None, uptime_seconds=None):
    lines = [
        '# HELP stormpilot_requests_total Business HTTP requests.',
        '# TYPE stormpilot_requests_total counter',
    ]
    for (route, code), count in sorted(request_counts or []):
        lines.append(
            f'stormpilot_requests_total{{route="{_metric_label(route)}",status="{_metric_label(code)}"}} {int(count)}'
        )
    if uptime_seconds is not None:
        lines.extend([
            '# HELP stormpilot_uptime_seconds Process uptime.',
            '# TYPE stormpilot_uptime_seconds gauge',
            f'stormpilot_uptime_seconds {float(uptime_seconds):.3f}',
        ])

    status, readiness = readiness_status()
    lines.extend([
        '# HELP stormpilot_ready Storage readiness status.',
        '# TYPE stormpilot_ready gauge',
        f'stormpilot_ready {1 if status == 200 else 0}',
    ])

    try:
        snapshot = _metrics_snapshot()
    except (OSError, sqlite3.DatabaseError, ValueError):
        lines.extend([
            '# HELP stormpilot_storage_available Storage metrics collection status.',
            '# TYPE stormpilot_storage_available gauge',
            'stormpilot_storage_available 0',
        ])
        return '\n'.join(lines) + '\n'

    lines.extend([
        '# HELP stormpilot_storage_available Storage metrics collection status.',
        '# TYPE stormpilot_storage_available gauge',
        'stormpilot_storage_available 1',
        '# HELP stormpilot_telemetry_samples_total Persisted telemetry samples by acceptance status.',
        '# TYPE stormpilot_telemetry_samples_total counter',
    ])
    for status_name in ('ACCEPTED', 'REJECTED'):
        lines.append(f'stormpilot_telemetry_samples_total{{status="{status_name.lower()}"}} {snapshot["telemetry_status"].get(status_name, 0)}')
    lines.extend([
        '# HELP stormpilot_telemetry_rejected_total Rejected telemetry by bounded reason.',
        '# TYPE stormpilot_telemetry_rejected_total counter',
    ])
    for reason in METRIC_REJECTION_REASONS:
        lines.append(f'stormpilot_telemetry_rejected_total{{reason="{reason}"}} {snapshot["telemetry_rejections"].get(reason, 0)}')
    lines.extend([
        '# HELP stormpilot_connected_drones Fresh phone GPS drone count.',
        '# TYPE stormpilot_connected_drones gauge',
        f'stormpilot_connected_drones {snapshot["connected_drones"]}',
        '# HELP stormpilot_stale_devices Active devices without recent telemetry.',
        '# TYPE stormpilot_stale_devices gauge',
        f'stormpilot_stale_devices {snapshot["stale_devices"]}',
        '# HELP stormpilot_deliveries_total Deliveries by bounded state.',
        '# TYPE stormpilot_deliveries_total gauge',
    ])
    for state in METRIC_DELIVERY_STATES:
        lines.append(f'stormpilot_deliveries_total{{state="{state.lower()}"}} {snapshot["deliveries"].get(state, 0)}')
    lines.extend([
        '# HELP stormpilot_notifications_total Notification outbox rows by bounded state.',
        '# TYPE stormpilot_notifications_total gauge',
    ])
    for state in METRIC_OUTBOX_STATES:
        lines.append(f'stormpilot_notifications_total{{state="{state.lower()}"}} {snapshot["notifications"].get(state, 0)}')
    lines.extend([
        '# HELP stormpilot_notification_exhausted_total Notifications that exhausted retry budget.',
        '# TYPE stormpilot_notification_exhausted_total gauge',
        f'stormpilot_notification_exhausted_total {snapshot["notification_exhausted"]}',
        '# HELP stormpilot_optimizer_proposals_total Stored optimizer proposals.',
        '# TYPE stormpilot_optimizer_proposals_total counter',
        f'stormpilot_optimizer_proposals_total {snapshot["optimizer_proposals"]}',
        '# HELP stormpilot_optimizer_applied_total Applied optimizer proposals.',
        '# TYPE stormpilot_optimizer_applied_total counter',
        f'stormpilot_optimizer_applied_total {snapshot["optimizer_applied"]}',
        '# HELP stormpilot_optimizer_rejections_total Optimizer apply rejections recorded by audit event.',
        '# TYPE stormpilot_optimizer_rejections_total counter',
        f'stormpilot_optimizer_rejections_total {snapshot["optimizer_rejections"]}',
        '# HELP stormpilot_simulation_runs_total Stored what-if scenario runs by state.',
        '# TYPE stormpilot_simulation_runs_total counter',
        f'stormpilot_simulation_runs_total{{state="stored"}} {snapshot["simulation_runs"]}',
        f'stormpilot_simulation_runs_total{{state="applied"}} {snapshot["simulation_applied"]}',
        '# HELP stormpilot_auth_throttled_keys Login throttle keys currently locked.',
        '# TYPE stormpilot_auth_throttled_keys gauge',
        f'stormpilot_auth_throttled_keys {snapshot["auth_throttled"]}',
        '# HELP stormpilot_delivery_completions_total Recorded delivery handover completions.',
        '# TYPE stormpilot_delivery_completions_total counter',
        f'stormpilot_delivery_completions_total {snapshot["delivery_completions"]}',
        '# HELP stormpilot_login_failures_total Recorded administrator login failures.',
        '# TYPE stormpilot_login_failures_total counter',
        f'stormpilot_login_failures_total {snapshot["login_failures"]}',
    ])
    return '\n'.join(lines) + '\n'


def _metrics_snapshot():
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=STALE_AFTER_SECONDS)).isoformat().replace('+00:00', 'Z')
    future = (now + timedelta(seconds=30)).isoformat().replace('+00:00', 'Z')
    conn = store.get_connection()
    try:
        telemetry_status = {
            row['status']: row['count']
            for row in conn.execute(
                'SELECT status, COUNT(*) AS count FROM telemetry_samples GROUP BY status'
            ).fetchall()
        }
        rejection_counts = {reason: 0 for reason in METRIC_REJECTION_REASONS}
        for row in conn.execute(
            "SELECT COALESCE(rejection_reason, 'other') AS reason, COUNT(*) AS count "
            "FROM telemetry_samples WHERE status = 'REJECTED' GROUP BY COALESCE(rejection_reason, 'other')"
        ).fetchall():
            reason = row['reason'] if row['reason'] in rejection_counts else 'other'
            rejection_counts[reason] += row['count']
        delivery_counts = {
            row['status']: row['count']
            for row in conn.execute(
                'SELECT status, COUNT(*) AS count FROM deliveries GROUP BY status'
            ).fetchall()
        }
        notification_counts = {
            row['state']: row['count']
            for row in conn.execute(
                'SELECT state, COUNT(*) AS count FROM notification_outbox GROUP BY state'
            ).fetchall()
        }
        connected_drones = conn.execute(
            """
            SELECT COUNT(DISTINCT drone_id) FROM telemetry_samples
            WHERE status = 'ACCEPTED'
                AND datetime(server_received_at) >= datetime(?)
                AND datetime(device_timestamp) >= datetime(?)
                AND datetime(server_received_at) <= datetime(?)
                AND datetime(device_timestamp) <= datetime(?)
            """,
            (cutoff, cutoff, future, future),
        ).fetchone()[0]
        stale_devices = conn.execute(
            """
            SELECT COUNT(*) FROM device_registrations
            WHERE status = 'active'
                AND (last_seen IS NULL OR datetime(last_seen) < datetime(?))
            """,
            (cutoff,),
        ).fetchone()[0]
        notification_exhausted = conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE state = 'FAILED' AND retry_count >= ?",
            (MAX_NOTIFICATION_RETRIES,),
        ).fetchone()[0]
        optimizer_proposals = conn.execute(
            "SELECT COUNT(*) FROM simulation_runs WHERE scenario_type = 'OPTIMIZATION_PROPOSAL'"
        ).fetchone()[0]
        optimizer_applied = conn.execute(
            "SELECT COUNT(*) FROM simulation_runs WHERE scenario_type = 'OPTIMIZATION_PROPOSAL' AND applied_at IS NOT NULL"
        ).fetchone()[0]
        optimizer_rejections = conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE event_type = 'OPTIMIZER_REJECTED'"
        ).fetchone()[0]
        simulation_runs = conn.execute(
            "SELECT COUNT(*) FROM simulation_runs WHERE scenario_type != 'OPTIMIZATION_PROPOSAL'"
        ).fetchone()[0]
        simulation_applied = conn.execute(
            "SELECT COUNT(*) FROM simulation_runs WHERE scenario_type != 'OPTIMIZATION_PROPOSAL' AND applied_at IS NOT NULL"
        ).fetchone()[0]
        auth_throttled = conn.execute(
            'SELECT COUNT(*) FROM admin_login_attempts WHERE locked_until IS NOT NULL AND locked_until > ?',
            (_utc_now(),),
        ).fetchone()[0]
        delivery_completions = conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE event_type = 'DELIVERY_CONFIRMED'"
        ).fetchone()[0]
        login_failures = conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE event_type = 'ADMIN_LOGIN_FAILED'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        'telemetry_status': telemetry_status,
        'telemetry_rejections': rejection_counts,
        'connected_drones': connected_drones,
        'stale_devices': stale_devices,
        'deliveries': delivery_counts,
        'notifications': notification_counts,
        'notification_exhausted': notification_exhausted,
        'optimizer_proposals': optimizer_proposals,
        'optimizer_applied': optimizer_applied,
        'optimizer_rejections': optimizer_rejections,
        'simulation_runs': simulation_runs,
        'simulation_applied': simulation_applied,
        'auth_throttled': auth_throttled,
        'delivery_completions': delivery_completions,
        'login_failures': login_failures,
    }


def _metric_label(value):
    return str(value).replace('\\', '\\\\').replace('"', '').replace('\n', '')


def _record_event_on_connection(conn, event_type, entity_type, entity_id, payload=None):
    conn.execute(
        'INSERT INTO event_log(id, event_type, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)',
        (
            f'event-{secrets.token_urlsafe(18)}',
            event_type,
            entity_type,
            str(entity_id),
            json.dumps(payload or {}, separators=(',', ':'), sort_keys=True),
        ),
    )


def initialize_storage(force_reset=False):
    store.initialize_storage(force_reset=force_reset)
    _bootstrap_demo_drones()
    _prune_expired_telemetry_if_due(force=True)
    return get_storage_path()


def _bootstrap_demo_drones():
    conn = store.get_connection()
    try:
        for drone in DEMO_DRONES:
            conn.execute(
                '''
                INSERT INTO drone_registrations (
                    drone_id, device_id, source, latitude, longitude, gps_accuracy, last_update, status, mission_id
                ) VALUES (?, NULL, 'OFFLINE', NULL, NULL, NULL, NULL, 'OFFLINE', NULL)
                ON CONFLICT(drone_id) DO NOTHING
                ''',
                (drone['drone_id'],),
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


def handle(method, path, body=None, headers=None, query=''):
    if method == 'POST' and path == '/api/auth/login':
        status, payload, response_headers = security.login(_read_json(body), headers)
        return status, _attach_response_headers(payload, response_headers)

    if method == 'GET' and path == '/api/auth/session':
        return security.session(headers)

    if method == 'POST' and path == '/api/auth/logout':
        status, payload, response_headers = security.logout(headers)
        return status, _attach_response_headers(payload, response_headers)

    if method == 'GET' and path == '/api/agile':
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR', 'VIEWER'}, 'agile.read', method)
        if error:
            return error
        return _agile_board()

    if method == 'POST' and path == '/api/agile/sprints':
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR'}, 'agile.sprint.create', method)
        if error:
            return error
        return _create_agile_sprint(_read_json(body), user)

    if method == 'PUT' and path.startswith('/api/agile/sprints/'):
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR'}, 'agile.sprint.update', method)
        if error:
            return error
        sprint_id = path.rsplit('/', 1)[-1]
        if not sprint_id:
            return 404, {'error': 'Sprint not found.'}
        return _transition_agile_sprint(sprint_id, _read_json(body), user)

    if method == 'POST' and path == '/api/agile/tasks':
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR'}, 'agile.task.create', method)
        if error:
            return error
        return _create_agile_task(_read_json(body), user)

    if method == 'PUT' and path.startswith('/api/agile/tasks/'):
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR'}, 'agile.task.update', method)
        if error:
            return error
        task_id = path.rsplit('/', 1)[-1]
        if not task_id:
            return 404, {'error': 'Work item not found.'}
        return _update_agile_task(task_id, _read_json(body), user)

    if method == 'DELETE' and path.startswith('/api/agile/tasks/'):
        user, error = security.authorize(headers, {'ADMIN'}, 'agile.task.delete', method)
        if error:
            return error
        task_id = path.rsplit('/', 1)[-1]
        if not task_id:
            return 404, {'error': 'Work item not found.'}
        return _delete_agile_task(task_id, user)

    if method == 'GET' and path == '/api/admin/users':
        user, error = security.authorize(headers, {'ADMIN'}, 'admin.users.list', method)
        if error:
            return error
        return 200, {'users': security.list_users()}

    if method == 'POST' and path == '/api/admin/users':
        user, error = security.authorize(headers, {'ADMIN'}, 'admin.users.create', method)
        if error:
            return error
        data = _read_json(body)
        if not isinstance(data, dict):
            return 400, {'error': 'Expected a JSON object.'}
        return security.create_user(data.get('username'), data.get('password'), data.get('role'), actor=user['user_id'])

    if method == 'PUT' and path.startswith('/api/admin/users/'):
        user, error = security.authorize(headers, {'ADMIN'}, 'admin.users.update', method)
        if error:
            return error
        user_id = path.rsplit('/', 1)[-1]
        if not user_id or user_id == 'users':
            return 404, {'error': 'Account not found.'}
        return security.update_user(user_id, _read_json(body), user)

    if method == 'POST' and path == '/api/missions/optimize':
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR'}, 'missions.optimize', method)
        if error:
            return error
        return _create_optimization(_read_json(body), user)

    if method == 'POST' and path == '/api/missions/optimize/apply':
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR'}, 'missions.optimize.apply', method)
        if error:
            return error
        return _apply_optimization(_read_json(body), user)

    if method == 'GET' and path == '/api/scenarios':
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR', 'VIEWER'}, 'scenarios.list', method)
        if error:
            return error
        return _scenario_list()

    if method == 'POST' and path == '/api/scenarios':
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR'}, 'scenarios.simulate', method)
        if error:
            return error
        return _create_scenario(_read_json(body), user)

    if method == 'GET' and path.startswith('/api/scenarios/'):
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR', 'VIEWER'}, 'scenarios.read', method)
        if error:
            return error
        scenario_id = path.rsplit('/', 1)[-1]
        if scenario_id.endswith(':replay'):
            return _replay_scenario(scenario_id[:-7])
        return _get_scenario(scenario_id)

    if method == 'POST' and path.startswith('/api/scenarios/') and path.endswith('/apply'):
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR'}, 'scenarios.apply', method)
        if error:
            return error
        return _apply_scenario(path.split('/')[-2], _read_json(body), user)

    if method == 'GET' and path == '/api/replay':
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR', 'VIEWER'}, 'replay.read', method)
        if error:
            return error
        return _route_replay(query)

    if method == 'GET' and path == '/api/replay/report':
        user, error = security.authorize(headers, {'ADMIN', 'OPERATOR', 'VIEWER'}, 'replay.report', method)
        if error:
            return error
        status, report = _route_replay(query)
        if status != 200:
            return status, report
        return 200, {'report': _replay_report(report)}

    if method == 'GET' and path == '/api/config':
        return 200, {
            'google_maps_api_key': os.getenv('GOOGLE_MAPS_API_KEY', '').strip(),
            'demo_mode': True,
            'map_ready': bool(os.getenv('GOOGLE_MAPS_API_KEY', '').strip()),
        }

    if method == 'GET' and path == '/api/demo/bengaluru':
        return _bengaluru_demo_payload()

    if method == 'GET' and path == '/api/fleet':
        return 200, _fleet_payload()

    if method == 'GET' and path == '/api/traffic':
        return _traffic_payload(query)

    if method == 'GET' and path == '/api/twin':
        return _twin_payload()

    if method == 'PUT' and path == '/api/twin/config':
        user, authorization = security.authorize(headers, {'ADMIN'}, 'twin.config.update', method)
        if authorization is not None:
            return authorization
        return _save_twin_config(body)

    if method == 'POST' and path == '/api/device/register':
        user, authorization = security.authorize(headers, {'ADMIN'}, 'device.register', method)
        if authorization is not None:
            return authorization
        return _handle_device_register(body)

    if method == 'DELETE' and path == '/api/device/revoke':
        user, authorization = security.authorize(headers, {'ADMIN'}, 'device.revoke', method)
        if authorization is not None:
            return authorization
        return _handle_device_revoke(body)

    if method == 'POST' and path == '/api/device/telemetry':
        return _handle_telemetry(body, headers=headers)

    if method == 'POST' and path == '/api/delivery/confirm':
        user, authorization = security.authorize(headers, {'ADMIN', 'OPERATOR'}, 'delivery.confirm', method)
        if authorization is not None:
            return authorization
        return _handle_delivery_confirm(body, confirmed_by=user['username'])

    if method == 'POST' and path == '/api/email/test':
        user, authorization = security.authorize(headers, {'ADMIN'}, 'email.test', method)
        if authorization is not None:
            return authorization
        return _handle_email_test(body)

    if method == 'GET' and path == '/api/redmine':
        return _handle_redmine()

    if method == 'GET' and path.startswith('/api/'):
        return 404, {'error': 'not found'}
    return 404, {'error': 'not found'}


AGILE_STATUSES = ('TODO', 'IN_PROGRESS', 'REVIEW', 'DONE')
AGILE_PRIORITIES = ('LOW', 'NORMAL', 'HIGH', 'URGENT')


def _agile_board():
    conn = store.get_connection()
    try:
        sprints = [
            dict(row) for row in conn.execute(
                """
                SELECT sprint_id, name, goal, starts_on, ends_on, state, created_at
                FROM agile_sprints
                ORDER BY CASE state WHEN 'ACTIVE' THEN 0 WHEN 'PLANNED' THEN 1 ELSE 2 END,
                         starts_on IS NULL, starts_on, created_at
                """
            ).fetchall()
        ]
        tasks = [
            dict(row) for row in conn.execute(
                """
                SELECT t.task_id, t.title, t.description, t.status, t.priority,
                       t.story_points, t.assignee_id, u.username AS assignee,
                       t.sprint_id, s.name AS sprint_name, t.created_by,
                       t.created_at, t.updated_at
                FROM agile_tasks AS t
                LEFT JOIN admin_users AS u ON u.user_id = t.assignee_id
                LEFT JOIN agile_sprints AS s ON s.sprint_id = t.sprint_id
                ORDER BY CASE t.priority WHEN 'URGENT' THEN 0 WHEN 'HIGH' THEN 1
                         WHEN 'NORMAL' THEN 2 ELSE 3 END, t.created_at, t.task_id
                """
            ).fetchall()
        ]
        users = [
            {'user_id': row['user_id'], 'username': row['username']}
            for row in conn.execute(
                "SELECT user_id, username FROM admin_users WHERE enabled = 1 ORDER BY username"
            ).fetchall()
        ]
        counts = {status: 0 for status in AGILE_STATUSES}
        for task in tasks:
            counts[task['status']] += 1
        return 200, {'sprints': sprints, 'tasks': tasks, 'users': users, 'counts': counts}
    finally:
        conn.close()


def _bengaluru_demo_payload():
    heat_counts = defaultdict(int)
    for delivery in BENGALURU_DEMO_DELIVERIES:
        for point in (delivery['position'], delivery['pickup'], delivery['destination']):
            latitude_bin = math.floor(point['latitude'] / BENGALURU_DEMO_HEAT_CELL_SIZE)
            longitude_bin = math.floor(point['longitude'] / BENGALURU_DEMO_HEAT_CELL_SIZE)
            heat_counts[(latitude_bin, longitude_bin)] += 1
    heat_cells = [
        {
            'latitude': round((latitude_bin + 0.5) * BENGALURU_DEMO_HEAT_CELL_SIZE, 6),
            'longitude': round((longitude_bin + 0.5) * BENGALURU_DEMO_HEAT_CELL_SIZE, 6),
            'sample_count': count,
        }
        for (latitude_bin, longitude_bin), count in sorted(
            heat_counts.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return 200, {
        'city': 'Bengaluru',
        'origin': 'SIMULATED',
        'warning': 'Illustrative sample routes and locations only; these are not live drones, GPS telemetry, delivery orders, or flight guidance.',
        'drones': BENGALURU_DEMO_DELIVERIES,
        'deliveries': BENGALURU_DEMO_DELIVERIES,
        'heat_cell_size_deg': BENGALURU_DEMO_HEAT_CELL_SIZE,
        'heat_cells': heat_cells,
    }


def _validate_agile_task(payload):
    if not isinstance(payload, dict):
        return None, 'Expected a JSON object.'
    title = payload.get('title')
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 160:
        return None, 'Title must contain between 1 and 160 characters.'
    description = payload.get('description', '')
    if not isinstance(description, str) or len(description) > 2000:
        return None, 'Description must be 2,000 characters or fewer.'
    status = payload.get('status', 'TODO')
    if status not in AGILE_STATUSES:
        return None, 'Status must be TODO, IN_PROGRESS, REVIEW or DONE.'
    priority = payload.get('priority', 'NORMAL')
    if priority not in AGILE_PRIORITIES:
        return None, 'Priority must be LOW, NORMAL, HIGH or URGENT.'
    story_points = payload.get('story_points')
    if story_points in ('', None):
        story_points = None
    elif isinstance(story_points, bool) or not isinstance(story_points, int) or not 1 <= story_points <= 100:
        return None, 'Story points must be a whole number from 1 to 100.'
    assignee_id = payload.get('assignee_id') or None
    sprint_id = payload.get('sprint_id') or None
    if assignee_id is not None and (not isinstance(assignee_id, str) or len(assignee_id) > 64):
        return None, 'Assignee is invalid.'
    if sprint_id is not None and (not isinstance(sprint_id, str) or len(sprint_id) > 64):
        return None, 'Sprint is invalid.'
    return {
        'title': title.strip(),
        'description': description.strip(),
        'status': status,
        'priority': priority,
        'story_points': story_points,
        'assignee_id': assignee_id,
        'sprint_id': sprint_id,
    }, None


def _agile_references_valid(conn, task):
    if task['assignee_id'] and not conn.execute(
        'SELECT 1 FROM admin_users WHERE user_id = ? AND enabled = 1', (task['assignee_id'],)
    ).fetchone():
        return 'Choose an enabled team member as the assignee.'
    if task['sprint_id'] and not conn.execute(
        'SELECT 1 FROM agile_sprints WHERE sprint_id = ?', (task['sprint_id'],)
    ).fetchone():
        return 'Choose an existing sprint or move the work item to the backlog.'
    return None


def _create_agile_task(payload, user):
    task, error = _validate_agile_task(payload)
    if error:
        return 400, {'error': error}
    task_id = secrets.token_urlsafe(18)
    conn = store.get_connection()
    try:
        with conn:
            reference_error = _agile_references_valid(conn, task)
            if reference_error:
                return 400, {'error': reference_error}
            conn.execute(
                """
                INSERT INTO agile_tasks (
                    task_id, title, description, status, priority, story_points,
                    assignee_id, sprint_id, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, task['title'], task['description'], task['status'],
                    task['priority'], task['story_points'], task['assignee_id'],
                    task['sprint_id'], user['user_id'],
                ),
            )
            _record_event_on_connection(conn, 'AGILE_TASK_CREATED', 'AGILE_TASK', task_id, {
                'actor': user['username'], 'status': task['status'], 'sprint_id': task['sprint_id'],
            })
        return 201, {'task_id': task_id}
    finally:
        conn.close()


def _update_agile_task(task_id, payload, user):
    task, error = _validate_agile_task(payload)
    if error:
        return 400, {'error': error}
    conn = store.get_connection()
    try:
        with conn:
            if not conn.execute('SELECT 1 FROM agile_tasks WHERE task_id = ?', (task_id,)).fetchone():
                return 404, {'error': 'Work item not found.'}
            reference_error = _agile_references_valid(conn, task)
            if reference_error:
                return 400, {'error': reference_error}
            conn.execute(
                """
                UPDATE agile_tasks
                SET title = ?, description = ?, status = ?, priority = ?, story_points = ?,
                    assignee_id = ?, sprint_id = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                WHERE task_id = ?
                """,
                (
                    task['title'], task['description'], task['status'], task['priority'],
                    task['story_points'], task['assignee_id'], task['sprint_id'], task_id,
                ),
            )
            _record_event_on_connection(conn, 'AGILE_TASK_UPDATED', 'AGILE_TASK', task_id, {
                'actor': user['username'], 'status': task['status'], 'sprint_id': task['sprint_id'],
            })
        return 200, {'task_id': task_id}
    finally:
        conn.close()


def _delete_agile_task(task_id, user):
    conn = store.get_connection()
    try:
        with conn:
            cursor = conn.execute('DELETE FROM agile_tasks WHERE task_id = ?', (task_id,))
            if cursor.rowcount == 0:
                return 404, {'error': 'Work item not found.'}
            _record_event_on_connection(conn, 'AGILE_TASK_DELETED', 'AGILE_TASK', task_id, {
                'actor': user['username'],
            })
        return 200, {'task_id': task_id, 'deleted': True}
    finally:
        conn.close()


def _create_agile_sprint(payload, user):
    if not isinstance(payload, dict):
        return 400, {'error': 'Expected a JSON object.'}
    name = payload.get('name')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        return 400, {'error': 'Sprint name must contain between 1 and 80 characters.'}
    goal = payload.get('goal', '')
    if not isinstance(goal, str) or len(goal) > 500:
        return 400, {'error': 'Sprint goal must be 500 characters or fewer.'}
    starts_on = payload.get('starts_on') or None
    ends_on = payload.get('ends_on') or None
    for field, value in (('start date', starts_on), ('end date', ends_on)):
        if value is not None:
            try:
                if not isinstance(value, str) or datetime.strptime(value, '%Y-%m-%d').date().isoformat() != value:
                    raise ValueError
            except ValueError:
                return 400, {'error': f'Sprint {field} must be a valid date in YYYY-MM-DD format.'}
    if starts_on and ends_on and starts_on > ends_on:
        return 400, {'error': 'Sprint end date must be on or after its start date.'}

    sprint_id = secrets.token_urlsafe(18)
    conn = store.get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            active_exists = conn.execute(
                "SELECT 1 FROM agile_sprints WHERE state = 'ACTIVE' LIMIT 1"
            ).fetchone()
            state = 'PLANNED' if active_exists else 'ACTIVE'
            conn.execute(
                """
                INSERT INTO agile_sprints (sprint_id, name, goal, starts_on, ends_on, state, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sprint_id, name.strip(), goal.strip(), starts_on, ends_on, state, user['user_id']),
            )
            _record_event_on_connection(conn, 'AGILE_SPRINT_CREATED', 'AGILE_SPRINT', sprint_id, {
                'actor': user['username'], 'state': state,
            })
        return 201, {'sprint_id': sprint_id, 'state': state}
    except sqlite3.IntegrityError:
        return 409, {'error': 'A sprint with that name already exists or another sprint became active.'}
    finally:
        conn.close()


def _transition_agile_sprint(sprint_id, payload, user):
    if not isinstance(payload, dict) or set(payload) != {'action'}:
        return 400, {'error': 'Provide a sprint action: start or complete.'}
    action = payload['action']
    if action not in ('start', 'complete'):
        return 400, {'error': 'Sprint action must be start or complete.'}

    conn = store.get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            sprint = conn.execute(
                'SELECT state FROM agile_sprints WHERE sprint_id = ?', (sprint_id,)
            ).fetchone()
            if sprint is None:
                return 404, {'error': 'Sprint not found.'}
            expected_state = 'PLANNED' if action == 'start' else 'ACTIVE'
            next_state = 'ACTIVE' if action == 'start' else 'COMPLETED'
            if sprint['state'] != expected_state:
                return 409, {'error': f'Only a {expected_state.lower()} sprint can undergo this transition.'}
            if action == 'start' and conn.execute(
                "SELECT 1 FROM agile_sprints WHERE state = 'ACTIVE' LIMIT 1"
            ).fetchone():
                return 409, {'error': 'Complete the active sprint before starting another sprint.'}
            conn.execute(
                'UPDATE agile_sprints SET state = ? WHERE sprint_id = ?',
                (next_state, sprint_id),
            )
            _record_event_on_connection(conn, f'AGILE_SPRINT_{next_state}', 'AGILE_SPRINT', sprint_id, {
                'actor': user['username'], 'state': next_state,
            })
        return 200, {'sprint_id': sprint_id, 'state': next_state}
    except sqlite3.IntegrityError:
        return 409, {'error': 'Complete the active sprint before starting another sprint.'}
    finally:
        conn.close()


def _attach_response_headers(payload, headers):
    if headers:
        payload['_response_headers'] = headers
    return payload


def _create_optimization(payload, user):
    if payload not in (None, {}):
        return 400, {'error': 'Optimization currently uses persisted active delivery records; no request overrides are accepted.'}
    snapshot = optimizer.capture_snapshot()
    result = optimizer.optimize(snapshot)
    proposal_id = secrets.token_urlsafe(18)
    conn = store.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO simulation_runs (
                scenario_id, scenario_type, input_json, baseline_json,
                result_json, created_by
            ) VALUES (?, 'OPTIMIZATION_PROPOSAL', '{}', ?, ?, ?)
            """,
            (
                proposal_id,
                json.dumps(snapshot, separators=(',', ':'), sort_keys=True),
                json.dumps(result, separators=(',', ':'), sort_keys=True),
                user['user_id'],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    result['optimization_id'] = proposal_id
    result['approval_required'] = True
    return 200, result


def _apply_optimization(payload, user):
    if not isinstance(payload, dict) or not isinstance(payload.get('optimization_id'), str):
        return 400, {'error': 'optimization_id is required.'}
    conn = store.get_connection()
    try:
        with conn:
            proposal = conn.execute(
                """
                SELECT * FROM simulation_runs WHERE scenario_id = ?
                    AND scenario_type = 'OPTIMIZATION_PROPOSAL'
                """,
                (payload['optimization_id'],),
            ).fetchone()
            if proposal is None:
                return 404, {'error': 'Optimization proposal not found.'}
            if proposal['applied_at']:
                _record_event_on_connection(conn, 'OPTIMIZER_REJECTED', 'SIMULATION', payload['optimization_id'], {'reason': 'already_applied'})
                return 409, {'error': 'This optimization proposal has already been applied.'}
            baseline = json.loads(proposal['baseline_json'])
            result = json.loads(proposal['result_json'])
            current_deliveries = conn.execute(
                "SELECT delivery_id, drone_id, status FROM deliveries WHERE status NOT IN ('DELIVERED','CANCELLED','FAILED') ORDER BY delivery_id"
            ).fetchall()
            expected = {
                (item['delivery_id'], item['drone_id'], item['status'])
                for item in baseline['deliveries']
            }
            observed = {
                (row['delivery_id'], row['drone_id'], row['status'])
                for row in current_deliveries
            }
            if expected != observed:
                _record_event_on_connection(conn, 'OPTIMIZER_REJECTED', 'SIMULATION', proposal['scenario_id'], {'reason': 'stale_baseline'})
                return 409, {'error': 'Live delivery assignments changed since this proposal; optimize again before applying.'}
            current_snapshot = optimizer.capture_snapshot()
            current_states = {item['drone_id']: item['state'] for item in current_snapshot['drones']}
            proposed = result.get('recommended_assignments', [])
            for item in proposed:
                if item['drone_id'] in current_states and current_states[item['drone_id']] != 'PHONE_GPS':
                    _record_event_on_connection(conn, 'OPTIMIZER_REJECTED', 'SIMULATION', proposal['scenario_id'], {'reason': 'drone_not_connected'})
                    return 409, {'error': f"Drone {item['drone_id']} is no longer connected; optimize again."}
            updated_count = 0
            for assignment in proposed:
                prior = next(
                    (
                        item for item in baseline['deliveries']
                        if item['delivery_id'] == assignment['delivery_id']
                    ),
                    None,
                )
                if prior is None or prior['status'] != 'CREATED':
                    continue
                changed = conn.execute(
                    """
                    UPDATE deliveries SET drone_id = ?, status = 'ASSIGNED', updated_at = ?
                    WHERE delivery_id = ? AND status = 'CREATED' AND drone_id = ?
                    """,
                    (
                        assignment['drone_id'],
                        _utc_now(),
                        assignment['delivery_id'],
                        prior['drone_id'],
                    ),
                ).rowcount
                if changed != 1:
                    raise sqlite3.OperationalError('Delivery assignment changed concurrently.')
                evidence = {
                    'optimization_id': proposal['scenario_id'],
                    'approved_by': user['username'],
                    'estimated_route_distance_km': assignment['estimated_route_distance_km'],
                }
                conn.execute(
                    """
                    INSERT INTO delivery_state_history (
                        delivery_id, previous_status, new_status, reason, evidence
                    ) VALUES (?, 'CREATED', 'ASSIGNED', 'operator_approved_optimizer_assignment', ?)
                    """,
                    (assignment['delivery_id'], json.dumps(evidence, separators=(',', ':'), sort_keys=True)),
                )
                conn.execute(
                    """
                    INSERT INTO event_log(id, event_type, entity_type, entity_id, payload)
                    VALUES (?, 'MISSION_ASSIGNMENT_APPROVED', 'DELIVERY', ?, ?)
                    """,
                    (
                        f"optimizer-assignment-{proposal['scenario_id']}-{assignment['delivery_id']}",
                        assignment['delivery_id'],
                        json.dumps(evidence, separators=(',', ':'), sort_keys=True),
                    ),
                )
                updated_count += 1
            conn.execute(
                'UPDATE simulation_runs SET applied_at = ? WHERE scenario_id = ? AND applied_at IS NULL',
                (_utc_now(), proposal['scenario_id']),
            )
        return 200, {
            'status': 'applied',
            'optimization_id': proposal['scenario_id'],
            'assignments_applied': updated_count,
            'approved_by': user['username'],
        }
    except sqlite3.OperationalError:
        return 409, {'error': 'Live delivery state changed concurrently; the proposal was not applied.'}
    finally:
        conn.close()


def _scenario_list():
    conn = store.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT scenario_id, scenario_type, input_json, created_by, created_at, applied_at
            FROM simulation_runs WHERE scenario_type != 'OPTIMIZATION_PROPOSAL'
            ORDER BY created_at DESC LIMIT 100
            """
        ).fetchall()
    finally:
        conn.close()
    return 200, {
        'origin': 'SIMULATED',
        'scenarios': [{
            'scenario_id': row['scenario_id'],
            'scenario_type': row['scenario_type'],
            'inputs': json.loads(row['input_json']),
            'created_by': row['created_by'],
            'created_at': row['created_at'],
            'applied_at': row['applied_at'],
        } for row in rows],
    }


def _create_scenario(payload, user):
    normalized, error = _validate_scenario(payload)
    if error:
        return 400, {'error': error}
    snapshot = optimizer.capture_snapshot()
    baseline = optimizer.optimize(snapshot)
    scenario_changes = _scenario_changes(normalized, snapshot)
    proposed = optimizer.optimize(snapshot, scenario_changes)
    baseline_map = {item['delivery_id']: item for item in baseline['recommended_assignments']}
    proposed_map = {item['delivery_id']: item for item in proposed['recommended_assignments']}
    baseline_unassigned = {
        item['delivery_id'] for item in baseline['unassigned_deliveries']
    }
    proposed_unassigned = {
        item['delivery_id'] for item in proposed['unassigned_deliveries']
    }
    affected_deliveries = sorted(
        set(baseline_map) | set(proposed_map)
    )
    affected_deliveries = [
        delivery_id for delivery_id in affected_deliveries
        if (
            baseline_map.get(delivery_id, {}).get('drone_id')
            != proposed_map.get(delivery_id, {}).get('drone_id')
            or (delivery_id in baseline_unassigned) != (delivery_id in proposed_unassigned)
        )
    ]
    base_durations = {key: value.get('estimated_duration_minutes') for key, value in baseline_map.items()}
    proposed_durations = {key: value.get('estimated_duration_minutes') for key, value in proposed_map.items()}
    affected = sorted({
        drone for delivery_id in affected_deliveries
        for drone in (
            baseline_map.get(delivery_id, {}).get('drone_id'),
            proposed_map.get(delivery_id, {}).get('drone_id'),
        ) if drone
    })
    if normalized['scenario_type'] == 'shared_corridor':
        affected = ['SIM-DRONE-01', 'SIM-DRONE-02']
    warnings = list(proposed['constraint_violations'])
    if normalized['scenario_type'] == 'shared_corridor':
        warnings.append({
            'kind': 'simulated_shared_corridor',
            'message': 'Two virtual drones approach the same corridor; this is a deterministic tabletop scenario, not live traffic.',
        })
    outcome = {
        'origin': 'SIMULATED',
        'scenario_type': normalized['scenario_type'],
        'inputs': normalized,
        'baseline_plan': baseline,
        'proposed_plan': proposed,
        'affected_drone_ids': affected,
        'affected_delivery_ids': affected_deliveries,
        'estimated_delay_minutes': {
            delivery_id: (
                round(proposed_durations[delivery_id] - base_durations[delivery_id], 1)
                if delivery_id in proposed_durations and delivery_id in base_durations else None
            )
            for delivery_id in affected_deliveries
        },
        'estimated_distance_change_km': round(
            proposed['total_planned_distance_km'] - baseline['total_planned_distance_km'], 3,
        ),
        'infeasible_assignments': proposed['unassigned_deliveries'],
        'warnings': warnings,
        'explanations': _scenario_explanations(normalized, affected_deliveries, proposed),
        'approval_required_before_live_application': True,
    }
    scenario_id = secrets.token_urlsafe(18)
    conn = store.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO simulation_runs (
                scenario_id, scenario_type, input_json, baseline_json,
                result_json, created_by
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                scenario_id,
                normalized['scenario_type'],
                json.dumps(normalized, separators=(',', ':'), sort_keys=True),
                json.dumps(snapshot, separators=(',', ':'), sort_keys=True),
                json.dumps(outcome, separators=(',', ':'), sort_keys=True),
                user['user_id'],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    outcome['scenario_id'] = scenario_id
    return 201, outcome


def _get_scenario(scenario_id):
    row = _scenario_row(scenario_id)
    if row is None:
        return 404, {'error': 'Scenario not found.'}
    return 200, json.loads(row['result_json'])


def _replay_scenario(scenario_id):
    row = _scenario_row(scenario_id)
    if row is None or row['scenario_type'] == 'OPTIMIZATION_PROPOSAL':
        return 404, {'error': 'Scenario not found.'}
    result = json.loads(row['result_json'])
    result['origin'] = 'SIMULATED'
    result['scenario_id'] = scenario_id
    result['replayed'] = True
    return 200, result


def _apply_scenario(scenario_id, payload, user):
    if not isinstance(payload, dict) or payload.get('approved') is not True:
        return 400, {'error': 'Explicit operator approval is required.'}
    row = _scenario_row(scenario_id)
    if row is None or row['scenario_type'] == 'OPTIMIZATION_PROPOSAL':
        return 404, {'error': 'Scenario not found.'}
    if row['applied_at']:
        return 409, {'error': 'This scenario proposal was already applied.'}
    result = json.loads(row['result_json'])
    if result['inputs']['scenario_type'] == 'high_priority_delivery':
        return 409, {'error': 'A hypothetical delivery is not automatically materialized; create a real delivery through an approved operational workflow.'}
    conn = store.get_connection()
    try:
        with conn:
            current = conn.execute(
                "SELECT delivery_id, drone_id, status FROM deliveries WHERE status NOT IN ('DELIVERED','CANCELLED','FAILED') ORDER BY delivery_id"
            ).fetchall()
            baseline = json.loads(row['baseline_json'])
            expected = {
                (item['delivery_id'], item['drone_id'], item['status'])
                for item in baseline['deliveries']
            }
            observed = {(item['delivery_id'], item['drone_id'], item['status']) for item in current}
            if expected != observed:
                return 409, {'error': 'Live assignments changed since this scenario; replay against a fresh baseline.'}
            baseline_ids = {item['delivery_id'] for item in baseline['deliveries']}
            proposed = result['proposed_plan'].get('recommended_assignments', [])
            changes = 0
            for assignment in proposed:
                if assignment['delivery_id'] not in baseline_ids:
                    continue
                prior = next(item for item in baseline['deliveries'] if item['delivery_id'] == assignment['delivery_id'])
                if prior['status'] != 'CREATED':
                    continue
                changed = conn.execute(
                    """
                    UPDATE deliveries SET drone_id = ?, status = 'ASSIGNED', updated_at = ?
                    WHERE delivery_id = ? AND drone_id = ? AND status = 'CREATED'
                    """,
                    (assignment['drone_id'], _utc_now(), assignment['delivery_id'], prior['drone_id']),
                ).rowcount
                if changed != 1:
                    raise sqlite3.OperationalError('Delivery assignment changed concurrently.')
                evidence = {
                    'scenario_id': scenario_id,
                    'approved_by': user['username'],
                    'origin': 'SIMULATED_PROPOSAL_OPERATOR_APPROVED',
                }
                conn.execute(
                    """
                    INSERT INTO delivery_state_history(delivery_id, previous_status, new_status, reason, evidence)
                    VALUES (?, 'CREATED', 'ASSIGNED', 'operator_approved_scenario_assignment', ?)
                    """,
                    (assignment['delivery_id'], json.dumps(evidence, separators=(',', ':'), sort_keys=True)),
                )
                conn.execute(
                    'INSERT INTO event_log(id, event_type, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)',
                    (
                        f"scenario-assignment-{scenario_id}-{assignment['delivery_id']}",
                        'SCENARIO_ASSIGNMENT_APPROVED',
                        'DELIVERY',
                        assignment['delivery_id'],
                        json.dumps(evidence, separators=(',', ':'), sort_keys=True),
                    ),
                )
                changes += 1
            conn.execute(
                'UPDATE simulation_runs SET applied_at = ? WHERE scenario_id = ? AND applied_at IS NULL',
                (_utc_now(), scenario_id),
            )
        return 200, {
            'status': 'applied',
            'scenario_id': scenario_id,
            'assignments_applied': changes,
            'approved_by': user['username'],
            'origin': 'SIMULATED_PROPOSAL_OPERATOR_APPROVED',
        }
    except sqlite3.OperationalError:
        return 409, {'error': 'Live delivery state changed concurrently; no partial scenario changes were applied.'}
    finally:
        conn.close()


def _route_replay(query):
    params = parse_qs(query or '')
    drone_id = params.get('drone_id', [''])[0].strip()
    delivery_id = params.get('delivery_id', [''])[0].strip()
    if bool(drone_id) == bool(delivery_id):
        return 400, {'error': 'Select exactly one drone_id or delivery_id.'}
    limit_text = params.get('limit', ['1000'])[0]
    try:
        limit = int(limit_text)
    except ValueError:
        return 400, {'error': 'limit must be between 1 and 2000.'}
    if not 1 <= limit <= 2000:
        return 400, {'error': 'limit must be between 1 and 2000.'}
    now = datetime.now(timezone.utc)
    end = _parse_replay_time(params.get('to', [_iso_utc(now)])[0])
    start = _parse_replay_time(params.get('from', [_iso_utc(now - timedelta(hours=24))])[0])
    if not start or not end or end <= start or end - start > timedelta(days=30):
        return 400, {'error': 'A valid time range no longer than 30 days is required.'}
    conn = store.get_connection()
    try:
        if delivery_id:
            delivery = conn.execute('SELECT * FROM deliveries WHERE delivery_id = ?', (delivery_id,)).fetchone()
            if delivery is None:
                return 404, {'error': 'Delivery not found.'}
            drone_id = delivery['drone_id']
        else:
            delivery = conn.execute(
                'SELECT * FROM deliveries WHERE drone_id = ? ORDER BY updated_at DESC LIMIT 1',
                (drone_id,),
            ).fetchone()
        clauses = ["drone_id = ?", "status = 'ACCEPTED'", 'datetime(device_timestamp) >= datetime(?)', 'datetime(device_timestamp) <= datetime(?)']
        values = [drone_id, _iso_utc(start), _iso_utc(end), limit]
        samples = conn.execute(
            f"""
            SELECT id, latitude, longitude, accuracy, device_timestamp, server_received_at, source
            FROM telemetry_samples WHERE {' AND '.join(clauses)}
            ORDER BY device_timestamp ASC, id ASC LIMIT ?
            """,
            values,
        ).fetchall()
        events = []
        if delivery:
            events = conn.execute(
                """
                SELECT event_type, entity_type, entity_id, timestamp, payload
                FROM event_log WHERE entity_id = ? AND datetime(timestamp) >= datetime(?)
                    AND datetime(timestamp) <= datetime(?)
                ORDER BY timestamp ASC LIMIT 500
                """,
                (delivery['delivery_id'], _iso_utc(start), _iso_utc(end)),
            ).fetchall()
            state_events = conn.execute(
                """
                SELECT previous_status, new_status, changed_at, reason, evidence
                FROM delivery_state_history WHERE delivery_id = ?
                    AND datetime(changed_at) >= datetime(?) AND datetime(changed_at) <= datetime(?)
                ORDER BY changed_at ASC LIMIT 500
                """,
                (delivery['delivery_id'], _iso_utc(start), _iso_utc(end)),
            ).fetchall()
        else:
            state_events = []
    finally:
        conn.close()
    points = [{
        'latitude': row['latitude'],
        'longitude': row['longitude'],
        'accuracy_m': row['accuracy'],
        'device_timestamp': row['device_timestamp'],
        'server_received_at': row['server_received_at'],
        'source': row['source'],
        'sample_id': row['id'],
    } for row in samples]
    gaps = []
    for first, second in zip(points, points[1:]):
        first_time = _parse_replay_time(first['device_timestamp'])
        second_time = _parse_replay_time(second['device_timestamp'])
        if first_time and second_time and (second_time - first_time).total_seconds() > 120:
            gaps.append({
                'from': first['device_timestamp'],
                'to': second['device_timestamp'],
                'duration_seconds': int((second_time - first_time).total_seconds()),
            })
    delivery_data = None
    if delivery:
        delivery_data = {
            'delivery_id': delivery['delivery_id'],
            'status': delivery['status'],
            'drone_id': delivery['drone_id'],
            'planned_distance_km': delivery['planned_route_km'],
            'pickup': {'latitude': delivery['pickup_lat'], 'longitude': delivery['pickup_lng']}
                if delivery['pickup_lat'] is not None and delivery['pickup_lng'] is not None else None,
            'destination': {'latitude': delivery['destination_lat'], 'longitude': delivery['destination_lng']}
                if delivery['destination_lat'] is not None and delivery['destination_lng'] is not None else None,
        }
    return 200, {
        'origin': 'HISTORICAL_PERSISTED',
        'drone_id': drone_id,
        'delivery': delivery_data,
        'time_range': {'from': _iso_utc(start), 'to': _iso_utc(end)},
        'points': points,
        'sample_limit': limit,
        'truncated': len(points) == limit,
        'gaps': gaps,
        'events': [{
            'event_type': row['event_type'],
            'entity_id': row['entity_id'],
            'timestamp': row['timestamp'],
            'payload': _safe_json_object(row['payload']),
        } for row in events] + [{
            'event_type': 'DELIVERY_STATE_CHANGED',
            'entity_id': delivery['delivery_id'],
            'timestamp': row['changed_at'],
            'payload': {
                'previous_status': row['previous_status'],
                'new_status': row['new_status'],
                'reason': row['reason'],
                'evidence': _safe_json_object(row['evidence']),
            },
        } for row in state_events],
        'read_only': True,
        'message': 'Only accepted persisted GPS samples are shown. Missing positions remain gaps; no coordinates are interpolated.',
    }


def _replay_report(replay):
    return {
        'title': f"StormPilot mission replay: {replay['delivery']['delivery_id'] if replay['delivery'] else replay['drone_id']}",
        'origin': replay['origin'],
        'drone_id': replay['drone_id'],
        'delivery': replay['delivery'],
        'time_range': replay['time_range'],
        'sample_count': len(replay['points']),
        'first_recorded_point': replay['points'][0] if replay['points'] else None,
        'last_recorded_point': replay['points'][-1] if replay['points'] else None,
        'gps_gap_count': len(replay['gaps']),
        'events': replay['events'],
        'limitations': replay['message'],
    }


def _validate_scenario(payload):
    allowed = {
        'drone_offline', 'deadline_missed', 'corridor_unavailable',
        'range_limit', 'shared_corridor', 'mission_infeasible',
        'high_priority_delivery',
    }
    if not isinstance(payload, dict) or payload.get('scenario_type') not in allowed:
        return None, 'scenario_type must be one of the documented what-if scenarios.'
    kind = payload['scenario_type']
    fields = {
        'drone_offline': {'scenario_type', 'drone_id'},
        'deadline_missed': {'scenario_type', 'delivery_id', 'deadline'},
        'corridor_unavailable': {'scenario_type', 'corridor_id'},
        'range_limit': {'scenario_type', 'drone_id', 'remaining_range_km'},
        'shared_corridor': {'scenario_type'},
        'mission_infeasible': {'scenario_type', 'delivery_id', 'reason'},
        'high_priority_delivery': {
            'scenario_type', 'delivery_id', 'pickup', 'destination',
            'planned_route_km', 'deadline',
        },
    }[kind]
    if set(payload) - fields:
        return None, 'Scenario contains unsupported fields.'
    snapshot = optimizer.capture_snapshot()
    drone_ids = {item['drone_id'] for item in snapshot['drones']}
    delivery_ids = {item['delivery_id'] for item in snapshot['deliveries']}
    twin = snapshot.get('twin', {})
    corridor_ids = {item['id'] for item in twin.get('corridors', [])}
    if kind in {'drone_offline', 'range_limit'} and payload.get('drone_id') not in drone_ids:
        return None, 'Select a registered drone.'
    if kind == 'deadline_missed':
        if payload.get('delivery_id') not in delivery_ids or not _parse_replay_time(payload.get('deadline')):
            return None, 'Select a persisted delivery and provide an ISO-8601 deadline.'
        if _parse_replay_time(payload['deadline']) > datetime.now(timezone.utc):
            return None, 'The missed deadline must be in the past.'
    if kind == 'corridor_unavailable' and payload.get('corridor_id') not in corridor_ids:
        return None, 'Select a configured virtual corridor.'
    if kind == 'range_limit':
        try:
            value = float(payload.get('remaining_range_km'))
        except (TypeError, ValueError):
            return None, 'remaining_range_km must be a positive number.'
        if not math.isfinite(value) or value <= 0:
            return None, 'remaining_range_km must be a positive number.'
        payload = {**payload, 'remaining_range_km': value}
    if kind == 'mission_infeasible':
        if payload.get('delivery_id') not in delivery_ids:
            return None, 'Select a persisted delivery.'
        reason = payload.get('reason')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            return None, 'Provide a scenario reason no longer than 200 characters.'
    if kind == 'high_priority_delivery':
        delivery_id = payload.get('delivery_id')
        if not isinstance(delivery_id, str) or not delivery_id.strip() or len(delivery_id) > 64:
            return None, 'Provide a hypothetical delivery ID no longer than 64 characters.'
        for key in ('pickup', 'destination'):
            point = payload.get(key)
            if not isinstance(point, dict) or not _valid_coordinate(point.get('lat'), point.get('lng')):
                return None, f'{key} must contain valid lat and lng coordinates.'
        try:
            distance = float(payload.get('planned_route_km'))
        except (TypeError, ValueError):
            return None, 'planned_route_km must be a positive number.'
        if not math.isfinite(distance) or distance <= 0:
            return None, 'planned_route_km must be a positive number.'
        deadline = payload.get('deadline')
        if deadline is not None and not _parse_replay_time(deadline):
            return None, 'deadline must be an ISO-8601 timestamp or null.'
        payload = {
            **payload,
            'delivery_id': delivery_id.strip(),
            'planned_route_km': distance,
            'priority': 'high',
        }
    return payload, None


def _scenario_changes(inputs, snapshot):
    kind = inputs['scenario_type']
    if kind == 'drone_offline':
        return {'offline_drone_ids': [inputs['drone_id']]}
    if kind == 'deadline_missed':
        return {'deadline_overrides': {inputs['delivery_id']: inputs['deadline']}}
    if kind == 'corridor_unavailable':
        return {'blocked_corridor_ids': [inputs['corridor_id']]}
    if kind == 'range_limit':
        return {'range_budgets': {inputs['drone_id']: inputs['remaining_range_km']}}
    if kind == 'mission_infeasible':
        return {'forced_infeasible_delivery': {
            'delivery_id': inputs['delivery_id'],
            'reason': inputs['reason'],
        }}
    if kind == 'high_priority_delivery':
        return {'new_delivery': {
            'delivery_id': inputs['delivery_id'],
            'drone_id': None,
            'status': 'CREATED',
            'pickup': inputs['pickup'],
            'destination': inputs['destination'],
            'planned_route_km': inputs['planned_route_km'],
            'actual_observed_km': 0,
            'priority': 'high',
            'deadline': inputs.get('deadline'),
        }}
    if kind == 'shared_corridor':
        return {}
    raise ValueError('Unsupported scenario type.')


def _scenario_explanations(inputs, affected, proposed):
    kind = inputs['scenario_type']
    if kind == 'drone_offline':
        explanation = f"Drone {inputs['drone_id']} was marked offline only in the isolated baseline copy."
    elif kind == 'deadline_missed':
        explanation = f"Delivery {inputs['delivery_id']} uses the hypothetical missed deadline provided in this scenario."
    elif kind == 'corridor_unavailable':
        explanation = f"Virtual corridor {inputs['corridor_id']} is blocked for the hypothetical direct-segment checks."
    elif kind == 'range_limit':
        explanation = f"Drone {inputs['drone_id']} uses a scenario-only remaining range budget."
    elif kind == 'shared_corridor':
        explanation = 'Two virtual drones approach a shared virtual corridor; no live drone state or GPS was changed.'
    elif kind == 'mission_infeasible':
        explanation = f"Delivery {inputs['delivery_id']} receives the explicitly supplied hypothetical infeasibility constraint."
    else:
        explanation = f"Hypothetical high-priority delivery {inputs['delivery_id']} competes for current eligible demonstration drones; it is not inserted into live delivery records."
    return [explanation] + [
        f"{delivery_id} is affected by the changed eligibility, deadline, range or virtual-route constraint."
        for delivery_id in affected
    ] + [
        f"{item['delivery_id']} has no feasible assignment: {item['reason']}"
        for item in proposed['unassigned_deliveries']
    ]


def _scenario_row(scenario_id):
    conn = store.get_connection()
    try:
        return conn.execute('SELECT * FROM simulation_runs WHERE scenario_id = ?', (scenario_id,)).fetchone()
    finally:
        conn.close()


def _safe_json_object(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _valid_coordinate(latitude, longitude):
    try:
        lat, lng = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180


def _parse_replay_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso_utc(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _fleet_payload():
    _prune_expired_telemetry_if_due()
    now = datetime.now(timezone.utc)
    drones = []
    conn = store.get_connection()
    try:
        for identity in DEMO_DRONES:
            latest = conn.execute(
                """
                SELECT * FROM telemetry_samples
                WHERE drone_id = ? AND status = 'ACCEPTED'
                ORDER BY device_timestamp DESC, id DESC LIMIT 1
                """,
                (identity['drone_id'],),
            ).fetchone()
            path_rows = conn.execute(
                """
                SELECT latitude, longitude FROM telemetry_samples
                WHERE drone_id = ? AND status = 'ACCEPTED'
                ORDER BY device_timestamp DESC, id DESC LIMIT ?
                """,
                (identity['drone_id'], MAX_PATH_POINTS),
            ).fetchall()
            path_rows.reverse()

            position = None
            source = 'OFFLINE'
            state = 'OFFLINE'
            accuracy = None
            last_update = None
            if latest is not None:
                source = 'PHONE_GPS'
                state = 'PHONE_GPS' if _is_recent_sample(latest, now) else 'STALE'
                position = {'latitude': latest['latitude'], 'longitude': latest['longitude']}
                accuracy = latest['accuracy']
                last_update = latest['device_timestamp']

            distance = conn.execute(
                """
                SELECT COALESCE(SUM(CASE
                    WHEN quality_flags IS NULL OR quality_flags NOT LIKE '%GPS_JUMP%'
                    THEN COALESCE(distance_m, 0)
                    ELSE 0 END), 0)
                FROM telemetry_samples WHERE drone_id = ? AND status = 'ACCEPTED'
                """,
                (identity['drone_id'],),
            ).fetchone()[0] or 0
            delivery = conn.execute(
                """
                SELECT * FROM deliveries WHERE drone_id = ?
                ORDER BY CASE WHEN status IN ('DELIVERED','CANCELLED','FAILED') THEN 1 ELSE 0 END,
                         updated_at DESC LIMIT 1
                """,
                (identity['drone_id'],),
            ).fetchone()
            mission = None
            if delivery is not None:
                mission = {
                    'delivery_id': delivery['delivery_id'],
                    'status': delivery['status'],
                    'pickup': _coordinate(delivery['pickup_lat'], delivery['pickup_lng']),
                    'destination': _coordinate(delivery['destination_lat'], delivery['destination_lng']),
                    'planned_distance_km': delivery['planned_route_km'],
                    'actual_distance_km': round(float(delivery['actual_observed_km'] or 0), 3),
                    'remaining_planned_distance_km': (
                        round(max(0.0, float(delivery['planned_route_km']) - float(delivery['actual_observed_km'] or 0)), 3)
                        if delivery['planned_route_km'] is not None else None
                    ),
                    'notification_status': delivery['notification_status'],
                    'created_at': delivery['created_at'],
                    'delivered_at': delivery['delivered_at'],
                }

            path = [
                {'latitude': point['latitude'], 'longitude': point['longitude']}
                for point in path_rows
            ]
            drones.append({
                **identity,
                'position': position,
                'source': source,
                'position_state': state,
                'status': state,
                'operation_mode': 'PHONE_GPS' if latest is not None else 'OFFLINE',
                'accuracy_m': accuracy,
                'last_update': last_update,
                'last_received_at': latest['server_received_at'] if latest is not None else None,
                'mission': mission['delivery_id'] if mission else None,
                'delivery_id': mission['delivery_id'] if mission else None,
                'delivery_status': mission['status'] if mission else None,
                'pickup': mission['pickup'] if mission else None,
                'destination': mission['destination'] if mission else None,
                'planned_route_km': mission['planned_distance_km'] if mission else None,
                'actual_distance_km': round(float(distance) / 1000.0, 3),
                'delivery_actual_distance_km': mission['actual_distance_km'] if mission else None,
                'remaining_distance_km': mission['remaining_planned_distance_km'] if mission else None,
                'notification_status': mission['notification_status'] if mission else None,
                'observed_path': path,
                'gps_coverage_sufficient': bool(
                    state == 'PHONE_GPS'
                    and accuracy is not None
                    and accuracy <= 25
                    and len(path) >= 2
                ),
                'telemetry_origin': 'live' if state == 'PHONE_GPS' else ('historical' if latest is not None else None),
            })

        all_deliveries = conn.execute('SELECT * FROM deliveries ORDER BY updated_at DESC LIMIT 500').fetchall()
        delivery_counts = {
            row['status']: row['count']
            for row in conn.execute('SELECT status, COUNT(*) AS count FROM deliveries GROUP BY status')
        }
        total_deliveries = sum(delivery_counts.values())
        duration_row = conn.execute(
            """
            SELECT AVG((julianday(delivered_at) - julianday(created_at)) * 1440)
            FROM deliveries WHERE status = 'DELIVERED' AND delivered_at IS NOT NULL
            """
        ).fetchone()
    finally:
        conn.close()

    completed_count = delivery_counts.get('DELIVERED', 0)
    analytics = {
        'active_drones': sum(1 for item in drones if item['position_state'] == 'PHONE_GPS'),
        'active_deliveries': sum(
            count for status, count in delivery_counts.items()
            if status not in {'DELIVERED', 'CANCELLED', 'FAILED'}
        ),
        'completed_deliveries': completed_count,
        'total_observed_distance_km': round(sum(item['actual_distance_km'] for item in drones), 3),
        'average_delivery_duration_minutes': round(duration_row[0], 1) if duration_row[0] is not None else None,
        'delivery_success_rate': (
            round(completed_count / total_deliveries, 3)
            if total_deliveries else None
        ),
        'gps_origin': 'live' if any(item['position_state'] == 'PHONE_GPS' for item in drones) else 'historical',
        'delivery_origin': 'live' if any(
            _parse_timestamp(row['updated_at'])
            and (now - _parse_timestamp(row['updated_at'])).total_seconds() <= STALE_AFTER_SECONDS
            for row in all_deliveries
        ) else 'historical',
        'distance_origin': 'historical',
        'traffic_density': 'not_available',
        'data_origin': 'historical',
    }
    return {
        'drones': drones,
        'deliveries': [_delivery_summary(row) for row in all_deliveries],
        'analytics': analytics,
    }


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_recent_sample(sample, now):
    received = _parse_timestamp(sample['server_received_at'])
    device_time = _parse_timestamp(sample['device_timestamp'])
    if received is None or device_time is None:
        return False
    received_age = (now - received).total_seconds()
    device_age = (now - device_time).total_seconds()
    return (
        received_age <= STALE_AFTER_SECONDS
        and device_age <= STALE_AFTER_SECONDS
        and received_age >= -30
        and device_age >= -30
    )


def _coordinate(latitude, longitude):
    if latitude is None or longitude is None:
        return None
    return {'lat': latitude, 'lng': longitude}


def _delivery_summary(row):
    updated = _parse_timestamp(row['updated_at'])
    origin = (
        'live'
        if updated and (datetime.now(timezone.utc) - updated).total_seconds() <= STALE_AFTER_SECONDS
        else 'historical'
    )
    return {
        'delivery_id': row['delivery_id'],
        'drone_id': row['drone_id'],
        'status': row['status'],
        'pickup': _coordinate(row['pickup_lat'], row['pickup_lng']),
        'destination': _coordinate(row['destination_lat'], row['destination_lng']),
        'planned_distance_km': row['planned_route_km'],
        'actual_distance_km': round(float(row['actual_observed_km'] or 0), 3),
        'notification_status': row['notification_status'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'delivered_at': row['delivered_at'],
        'origin': origin,
    }


def _traffic_payload(query):
    _prune_expired_telemetry_if_due()
    params = parse_qs(query or '', keep_blank_values=True)
    window_text = params.get('window_hours', ['24'])[0]
    try:
        window_hours = int(window_text)
    except (TypeError, ValueError):
        return 400, {'error': 'window_hours must be one of 1, 6, 24 or 168.'}
    if window_hours not in {1, 6, 24, 168}:
        return 400, {'error': 'window_hours must be one of 1, 6, 24 or 168.'}

    drone_id = params.get('drone_id', [''])[0].strip()
    if drone_id and drone_id not in {drone['drone_id'] for drone in DEMO_DRONES}:
        return 400, {'error': 'Unknown drone_id filter.'}
    source_filter = params.get('source', ['ALL'])[0].upper()
    if source_filter not in {'ALL', 'PHONE_GPS'}:
        return 400, {'error': 'source must be ALL or PHONE_GPS.'}
    cell_size_text = params.get('cell_size_deg', ['0.01'])[0]
    try:
        cell_size = float(cell_size_text)
    except (TypeError, ValueError):
        return 400, {'error': 'cell_size_deg must be 0.005, 0.01 or 0.02.'}
    if cell_size not in {0.005, 0.01, 0.02}:
        return 400, {'error': 'cell_size_deg must be 0.005, 0.01 or 0.02.'}
    mission_id = params.get('mission', [''])[0].strip()

    clauses = ["t.status = 'ACCEPTED'", "datetime(t.device_timestamp) >= datetime('now', ?)"]
    values = [f'-{window_hours} hours']
    if drone_id:
        clauses.append('t.drone_id = ?')
        values.append(drone_id)
    if source_filter != 'ALL':
        clauses.append('t.source = ?')
        values.append(source_filter)
    if mission_id:
        clauses.append(
            "t.drone_id = (SELECT d.drone_id FROM deliveries d WHERE d.delivery_id = ? LIMIT 1)"
        )
        values.append(mission_id)

    conn = store.get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT t.id, t.drone_id, t.latitude, t.longitude, t.accuracy,
                   t.device_timestamp, t.server_received_at, t.source
            FROM telemetry_samples t WHERE {' AND '.join(clauses)}
            ORDER BY t.device_timestamp DESC, t.id DESC LIMIT ?
            """,
            (*values, MAX_TRAFFIC_SAMPLES),
        ).fetchall()
        deliveries = conn.execute(
            'SELECT * FROM deliveries ORDER BY updated_at DESC LIMIT 500'
        ).fetchall()
    finally:
        conn.close()

    selected_deliveries = [
        _delivery_summary(row) for row in deliveries
        if (not drone_id or row['drone_id'] == drone_id)
        and (not mission_id or row['delivery_id'] == mission_id)
    ]
    cells = defaultdict(lambda: {
        'samples': 0,
        'drones': set(),
        'newest_received': None,
        'newest_sample': None,
    })
    paths = defaultdict(list)
    path_latest_sample = {}
    newest_received = None
    newest_sample = None
    now = datetime.now(timezone.utc)
    for row in reversed(rows):
        key = (
            round(round(row['latitude'] / cell_size) * cell_size, 5),
            round(round(row['longitude'] / cell_size) * cell_size, 5),
        )
        cells[key]['samples'] += 1
        cells[key]['drones'].add(row['drone_id'])
        received_at = _parse_timestamp(row['server_received_at'])
        if received_at and (
            cells[key]['newest_received'] is None or received_at > cells[key]['newest_received']
        ):
            cells[key]['newest_received'] = received_at
            cells[key]['newest_sample'] = row
        path = paths[row['drone_id']]
        if len(path) >= MAX_PATH_POINTS:
            path.pop(0)
        path.append({
            'lat': row['latitude'],
            'lng': row['longitude'],
        })
        path_latest = path_latest_sample.get(row['drone_id'])
        if (
            received_at
            and (
                path_latest is None
                or received_at > _parse_timestamp(path_latest['server_received_at'])
            )
        ):
            path_latest_sample[row['drone_id']] = row
        if received_at and (newest_received is None or received_at > newest_received):
            newest_received = received_at
            newest_sample = row

    origin = 'live' if newest_sample and _is_recent_sample(newest_sample, now) else 'historical'
    cell_rows = [
        {
            'latitude': key[0],
            'longitude': key[1],
            'sample_count': value['samples'],
            'drone_count': len(value['drones']),
            'origin': (
                'live'
                if value['newest_sample'] and _is_recent_sample(value['newest_sample'], now)
                else 'historical'
            ),
        }
        for key, value in sorted(cells.items(), key=lambda item: item[1]['samples'], reverse=True)
    ]
    overlap_cells = [cell for cell in cell_rows if cell['drone_count'] > 1]
    path_crossings = []
    for index, first in enumerate(paths):
        for second in list(paths)[index + 1:]:
            for point in _path_intersections(paths[first], paths[second]):
                path_crossings.append({
                    'latitude': point['lat'],
                    'longitude': point['lng'],
                    'drone_ids': [first, second],
                    'kind': 'path_crossing',
                    'origin': origin,
                })
    filtered_drones = {drone_id} if drone_id else {drone['drone_id'] for drone in DEMO_DRONES}
    drone_distances = [
        {
            'drone_id': drone['drone_id'],
            'distance_km': drone['actual_distance_km'],
            'origin': 'historical',
        }
        for drone in _fleet_payload()['drones']
        if drone['drone_id'] in filtered_drones
    ]
    return 200, {
        'window_hours': window_hours,
        'cell_size_deg': cell_size,
        'origin': origin,
        'source': 'PHONE_GPS',
        'sample_count': len(rows),
        'sample_limit': MAX_TRAFFIC_SAMPLES,
        'cells': cell_rows,
        'overlapping_cells': overlap_cells,
        'path_crossings': path_crossings,
        'observed_paths': [
            {
                'drone_id': identity,
                'path': path,
                'origin': (
                    'live'
                    if path_latest_sample.get(identity)
                    and _is_recent_sample(path_latest_sample[identity], now)
                    else 'historical'
                ),
            }
            for identity, path in paths.items() if len(path) > 1
        ],
        'distance_per_drone': drone_distances,
        'deliveries': selected_deliveries,
        'delivery_counts': {
            'active': sum(1 for delivery in selected_deliveries if delivery['status'] not in {'DELIVERED', 'CANCELLED', 'FAILED'}),
            'completed': sum(1 for delivery in selected_deliveries if delivery['status'] == 'DELIVERED'),
        },
        'filters': {'drone_id': drone_id or None, 'mission': mission_id or None, 'source': source_filter},
        'route_intersections': path_crossings + [
            {
                'latitude': cell['latitude'],
                'longitude': cell['longitude'],
                'drone_count': cell['drone_count'],
                'kind': 'shared_grid_cell',
                'origin': origin,
            }
            for cell in overlap_cells
        ],
    }


def _twin_payload():
    conn = store.get_connection()
    try:
        row = conn.execute(
            'SELECT config_json, updated_at FROM digital_twin_config WHERE config_id = 1'
        ).fetchone()
    finally:
        conn.close()
    config = json.loads(row['config_json']) if row else DEFAULT_TWIN_CONFIG
    intersections = []
    for scenario in config['scenarios']:
        for index, first in enumerate(scenario['routes']):
            for second in scenario['routes'][index + 1:]:
                for point in _path_intersections(first['path'], second['path']):
                    intersections.append({
                        'scenario_id': scenario['id'],
                        'drone_ids': [first['drone_id'], second['drone_id']],
                        'latitude': point['lat'],
                        'longitude': point['lng'],
                        'origin': 'simulated',
                    })
    return 200, {
        **config,
        'route_intersections': intersections,
        'origin': 'simulated',
        'updated_at': row['updated_at'] if row else None,
        'warning': 'Virtual zones and routes are demonstrations, not official aviation restrictions or flight-safety guidance.',
    }


def _path_intersections(first_path, second_path):
    points = []
    epsilon = 1e-9

    def on_segment(point, start, end):
        x, y = point
        x1, y1 = start
        x2, y2 = end
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        return (
            abs(cross) <= epsilon
            and min(x1, x2) - epsilon <= x <= max(x1, x2) + epsilon
            and min(y1, y2) - epsilon <= y <= max(y1, y2) + epsilon
        )

    def add_point(longitude, latitude):
        if not any(
            abs(point['lat'] - latitude) < epsilon and abs(point['lng'] - longitude) < epsilon
            for point in points
        ):
            points.append({'lat': latitude, 'lng': longitude})

    for first_start, first_end in zip(first_path, first_path[1:]):
        for second_start, second_end in zip(second_path, second_path[1:]):
            x1, y1 = first_start['lng'], first_start['lat']
            x2, y2 = first_end['lng'], first_end['lat']
            x3, y3 = second_start['lng'], second_start['lat']
            x4, y4 = second_end['lng'], second_end['lat']
            denominator = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
            if abs(denominator) < 1e-12:
                first_line_cross = (x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)
                if abs(first_line_cross) < epsilon:
                    endpoints = ((x1, y1), (x2, y2), (x3, y3), (x4, y4))
                    for longitude, latitude in endpoints:
                        if on_segment((longitude, latitude), (x1, y1), (x2, y2)) and on_segment(
                            (longitude, latitude), (x3, y3), (x4, y4)
                        ):
                            add_point(longitude, latitude)
                continue
            determinant1 = x1 * y2 - y1 * x2
            determinant2 = x3 * y4 - y3 * x4
            longitude = (determinant1 * (x3 - x4) - (x1 - x2) * determinant2) / denominator
            latitude = (determinant1 * (y3 - y4) - (y1 - y2) * determinant2) / denominator
            if (
                min(x1, x2) - epsilon <= longitude <= max(x1, x2) + epsilon
                and min(y1, y2) - epsilon <= latitude <= max(y1, y2) + epsilon
                and min(x3, x4) - epsilon <= longitude <= max(x3, x4) + epsilon
                and min(y3, y4) - epsilon <= latitude <= max(y3, y4) + epsilon
            ):
                add_point(longitude, latitude)
    return points


def _save_twin_config(body):
    data = _read_json(body)
    validated, error = _validate_twin_config(data)
    if error:
        return 400, {'error': error}
    serialized = json.dumps(validated, separators=(',', ':'), sort_keys=True)
    conn = store.get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO digital_twin_config(config_id, config_json, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(config_id) DO UPDATE SET
                    config_json = excluded.config_json, updated_at = excluded.updated_at
                """,
                (serialized, _utc_now()),
            )
        store.record_event('TWIN_CONFIGURATION_UPDATED', 'DIGITAL_TWIN', 'configuration', {
            'corridor_count': len(validated['corridors']),
            'zone_count': len(validated['zones']),
            'scenario_count': len(validated['scenarios']),
        })
    finally:
        conn.close()
    return _twin_payload()


def _validate_twin_config(data):
    if not isinstance(data, dict) or set(data) != {'corridors', 'zones', 'restrictions', 'scenarios'}:
        return None, 'Configuration must contain corridors, zones, restrictions and scenarios.'
    if any(not isinstance(data[key], list) or len(data[key]) > 25 for key in data):
        return None, 'Configuration collections must be arrays with no more than 25 entries.'

    def valid_id(value):
        return isinstance(value, str) and value == value.strip() and 1 <= len(value) <= 64

    def valid_path(path):
        return (
            isinstance(path, list) and 2 <= len(path) <= 200
            and all(
                isinstance(point, dict)
                and _float(point.get('lat')) is not None
                and _float(point.get('lng')) is not None
                and math.isfinite(_float(point.get('lat')))
                and math.isfinite(_float(point.get('lng')))
                and -90 <= _float(point.get('lat')) <= 90
                and -180 <= _float(point.get('lng')) <= 180
                for point in path
            )
        )

    corridors = data['corridors']
    zones = data['zones']
    restrictions = data['restrictions']
    scenarios = data['scenarios']
    if any(
        not isinstance(item, dict) or not valid_id(item.get('id'))
        or not valid_path(item.get('path'))
        for item in corridors
    ):
        return None, 'Each corridor needs an ID and a path with 2-200 valid coordinates.'
    if len({item['id'] for item in corridors}) != len(corridors):
        return None, 'Corridor IDs must be unique.'
    zone_ids = set()
    for zone in zones:
        center = zone.get('center') if isinstance(zone, dict) else None
        radius = _float(zone.get('radius_m')) if isinstance(zone, dict) else None
        if (
            not isinstance(zone, dict) or not valid_id(zone.get('id'))
            or not isinstance(center, dict)
            or _float(center.get('lat')) is None or _float(center.get('lng')) is None
            or not -90 <= _float(center.get('lat')) <= 90
            or not -180 <= _float(center.get('lng')) <= 180
            or radius is None or not 10 <= radius <= 5000
        ):
            return None, 'Each zone needs an ID, valid center and radius from 10 to 5000 metres.'
        zone_ids.add(zone['id'])
    if len(zone_ids) != len(zones):
        return None, 'Zone IDs must be unique.'
    if any(
        not isinstance(item, dict) or item.get('zone_id') not in zone_ids
        or not isinstance(item.get('restricted'), bool)
        for item in restrictions
    ):
        return None, 'Restrictions must reference a configured zone and set restricted to a boolean.'
    for scenario in scenarios:
        if (
            not isinstance(scenario, dict) or not valid_id(scenario.get('id'))
            or not isinstance(scenario.get('routes'), list) or len(scenario['routes']) > 25
            or any(
                not isinstance(route, dict) or not valid_id(route.get('drone_id'))
                or not valid_path(route.get('path'))
                for route in scenario['routes']
            )
        ):
            return None, 'Each scenario needs an ID and routes with valid drone IDs and paths.'
    if len({item['id'] for item in scenarios}) != len(scenarios):
        return None, 'Scenario IDs must be unique.'

    def normalize_path(path):
        return [{'lat': _float(point['lat']), 'lng': _float(point['lng'])} for point in path]

    def normalize_name(item):
        name = item.get('name', item['id'])
        return name.strip() if isinstance(name, str) and 0 < len(name.strip()) <= 120 else None

    normalized_corridors = []
    for corridor in corridors:
        name = normalize_name(corridor)
        if name is None:
            return None, 'Corridor names must be non-empty strings no longer than 120 characters.'
        normalized_corridors.append({
            'id': corridor['id'],
            'name': name,
            'path': normalize_path(corridor['path']),
        })

    normalized_zones = []
    for zone in zones:
        name = normalize_name(zone)
        if name is None:
            return None, 'Zone names must be non-empty strings no longer than 120 characters.'
        normalized_zones.append({
            'id': zone['id'],
            'name': name,
            'center': {'lat': _float(zone['center']['lat']), 'lng': _float(zone['center']['lng'])},
            'radius_m': _float(zone['radius_m']),
        })

    normalized_restrictions = []
    for restriction in restrictions:
        reason = restriction.get('reason', 'Operator-defined virtual restriction')
        if not isinstance(reason, str) or len(reason) > 500:
            return None, 'Restriction reasons must be strings no longer than 500 characters.'
        normalized_restrictions.append({
            'zone_id': restriction['zone_id'],
            'restricted': restriction['restricted'],
            'reason': reason,
        })

    normalized_scenarios = []
    for scenario in scenarios:
        name = normalize_name(scenario)
        if name is None:
            return None, 'Scenario names must be non-empty strings no longer than 120 characters.'
        normalized_scenarios.append({
            'id': scenario['id'],
            'name': name,
            'routes': [{
                'drone_id': route['drone_id'],
                'path': normalize_path(route['path']),
            } for route in scenario['routes']],
        })
    return {
        'corridors': normalized_corridors,
        'zones': normalized_zones,
        'restrictions': normalized_restrictions,
        'scenarios': normalized_scenarios,
    }, None


def _handle_telemetry(body, headers=None):
    _prune_expired_telemetry_if_due()
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
        existing_credential = conn.execute(
            'SELECT drone_id, status FROM device_credentials WHERE device_id = ?',
            (device_id,),
        ).fetchone()
        existing_registration = conn.execute(
            'SELECT drone_id, status FROM device_registrations WHERE device_id = ?',
            (device_id,),
        ).fetchone()
        for existing in (existing_credential, existing_registration):
            if (
                existing is not None
                and str(existing['status']).upper() in {'ACTIVE', 'ACTIVE'.lower()}
                and existing['drone_id'] != drone_id
            ):
                return 409, {
                    'error': 'Device is already assigned to another drone. Revoke it before re-enrollment.',
                    'device_id': device_id,
                }

        now = _utc_now()
        expires_at = _utc_now_plus_days(30)
        with conn:
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
                (device_id, drone_id, digest, salt, now, now, expires_at),
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
                    expires_at = excluded.expires_at,
                    revocation_reason = NULL
                ''',
                (device_id, drone_id, now, now, now, expires_at),
            )
        store.record_event('DEVICE_REGISTERED', 'DEVICE', device_id, {
            'drone_id': drone_id,
            'actor': 'administrator',
        })
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
        reason = str(data.get('reason', 'revoked')).strip() or 'revoked'
        credentials = conn.execute(
            'UPDATE device_credentials SET status = ?, revoked_reason = ?, updated_at = ? WHERE device_id = ?',
            ('REVOKED', reason, _utc_now(), device_id),
        ).rowcount
        registrations = conn.execute(
            'UPDATE device_registrations SET status = ?, revocation_reason = ?, updated_at = ? WHERE device_id = ?',
            ('revoked', reason, _utc_now(), device_id),
        ).rowcount
        if not credentials and not registrations:
            return 404, {'error': 'Unknown device ID.'}
        conn.commit()
        store.record_event('DEVICE_REVOKED', 'DEVICE', device_id, {
            'reason': reason,
            'actor': 'administrator',
        })
        return 200, {'status': 'revoked', 'device_id': device_id}
    finally:
        conn.close()


def _handle_delivery_confirm(body, confirmed_by):
    data = _read_json(body)
    if not isinstance(data, dict):
        return 400, {'error': 'Invalid delivery confirmation payload.'}

    delivery_id = str(data.get('delivery_id', '')).strip()
    drone_id = str(data.get('drone_id', '')).strip()
    if not delivery_id or not drone_id or data.get('handover_confirmed') is not True:
        return 400, {'error': 'Delivery ID, drone ID and an explicit handover confirmation are required.'}

    conn = store.get_connection()
    try:
        existing = conn.execute('SELECT * FROM deliveries WHERE delivery_id = ?', (delivery_id,)).fetchone()
        if existing is None:
            return 404, {'error': 'Unknown delivery ID.'}
        if existing is not None and existing['drone_id'] != drone_id:
            return 409, {'error': 'Delivery is assigned to a different drone.'}
        if existing is not None and existing['handover_confirmed']:
            return 200, {'status': 'already_confirmed', 'delivery_id': delivery_id, 'drone_id': existing['drone_id']}
        if existing['status'] != 'ARRIVED':
            return 409, {'error': 'Delivery handover can only be confirmed after arrival.'}

        timestamp = _utc_now()
        with conn:
            changed = conn.execute(
                """
                UPDATE deliveries SET handover_confirmed = 1, handover_confirmed_by = ?,
                    status = 'DELIVERED', updated_at = ?, delivered_at = ?
                WHERE delivery_id = ? AND drone_id = ? AND status = 'ARRIVED'
                    AND handover_confirmed = 0
                """,
                (confirmed_by, timestamp, timestamp, delivery_id, drone_id),
            ).rowcount
            if not changed:
                return 409, {'error': 'Delivery state changed; reload and confirm the current delivery state.'}
            conn.execute(
                'INSERT INTO delivery_state_history (delivery_id, previous_status, new_status, reason, evidence) VALUES (?, ?, ?, ?, ?)',
                (
                    delivery_id,
                    'ARRIVED',
                    'DELIVERED',
                    'handover_confirmed',
                    json.dumps({'drone_id': drone_id, 'confirmed_by': confirmed_by, 'handover_confirmed': True}, separators=(',', ':'), sort_keys=True),
                ),
            )
            conn.execute(
                'INSERT INTO event_log (id, event_type, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)',
                (
                    f'delivery-confirm-{delivery_id}-{timestamp}',
                    'DELIVERY_CONFIRMED',
                    'DELIVERY',
                    delivery_id,
                    json.dumps({'drone_id': drone_id, 'confirmed_by': confirmed_by, 'handover_confirmed': True}, separators=(',', ':'), sort_keys=True),
                ),
            )
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

    if _smtp_local_test_mode():
        return 200, {
            'status': 'local_test',
            'message': 'SMTP local test mode is enabled; no external email was sent.',
        }

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


def _smtp_local_test_mode():
    return os.getenv('SMTP_LOCAL_TEST_MODE', '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _notification_subject(row):
    label = str(row['notification_type']).replace('_', ' ').title()
    if row['delivery_id']:
        return f"StormPilot: {label} {row['delivery_id']}"
    return f"StormPilot: {label}"


def _notification_email_body(conn, outbox_row, payload):
    lines = [
        'StormPilot notification',
        f"Type: {outbox_row['notification_type']}",
        f"Entity ID: {outbox_row['entity_id']}",
    ]
    delivery_id = outbox_row['delivery_id']
    if delivery_id:
        delivery = conn.execute(
            'SELECT * FROM deliveries WHERE delivery_id = ?',
            (delivery_id,),
        ).fetchone()
        if delivery is not None:
            device = conn.execute(
                """
                SELECT device_id FROM telemetry_samples
                WHERE drone_id = ? AND status = 'ACCEPTED' AND device_id IS NOT NULL
                ORDER BY device_timestamp DESC, id DESC LIMIT 1
                """,
                (delivery['drone_id'],),
            ).fetchone()
            if device is None:
                device = conn.execute(
                    """
                    SELECT device_id FROM device_registrations
                    WHERE drone_id = ? ORDER BY updated_at DESC LIMIT 1
                    """,
                    (delivery['drone_id'],),
                ).fetchone()
            device_id = device['device_id'] if device is not None else 'unavailable'
            lines.extend([
                '',
                'Verified delivery data:',
                f"Delivery ID: {delivery['delivery_id']}",
                f"Delivery status: {delivery['status']}",
                f"Drone ID: {delivery['drone_id']}",
                f"Device ID: {device_id}",
                f"Observed distance (km): {round(float(delivery['actual_observed_km'] or 0), 3)}",
                f"Completed at: {delivery['delivered_at'] or 'unavailable'}",
                f"Arrival detected at: {delivery['arrival_detected_at'] or 'unavailable'}",
                f"Handover confirmed: {'yes' if delivery['handover_confirmed'] else 'no'}",
            ])
        else:
            lines.extend(['', f'Persisted delivery record was not found for {delivery_id}.'])
    public_payload = _public_notification_payload(payload)
    if public_payload:
        lines.extend([
            '',
            'Additional non-secret payload:',
            json.dumps(public_payload, indent=2, sort_keys=True),
        ])
    lines.extend([
        '',
        'No SMTP password, device bearer token, session cookie or CSRF token is included in this message.',
    ])
    return '\n'.join(lines)


def _public_notification_payload(payload):
    if not isinstance(payload, dict):
        return {}
    blocked = ('password', 'token', 'secret', 'credential', 'authorization', 'cookie', 'csrf', 'to')
    return {
        str(key): value
        for key, value in payload.items()
        if not any(fragment in str(key).lower() for fragment in blocked)
    }


def process_notification_outbox(max_jobs=10):
    conn = store.get_connection()
    processed = []
    try:
        now = _utc_now()
        rows = conn.execute(
            "SELECT * FROM notification_outbox WHERE ((state = 'PENDING' OR (state = 'FAILED' AND retry_count < ?)) AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) OR (state = 'PROCESSING' AND julianday(updated_at) < julianday('now', '-5 minutes')) ORDER BY next_attempt_at LIMIT ?",
            (MAX_NOTIFICATION_RETRIES, now, max_jobs),
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
                local_test_mode = _smtp_local_test_mode()
                settings = {
                    'host': os.getenv('SMTP_HOST', '').strip(),
                    'port': int(os.getenv('SMTP_PORT', '587').strip() or '587'),
                    'username': os.getenv('SMTP_USERNAME', '').strip(),
                    'password': os.getenv('SMTP_APP_PASSWORD', '').strip(),
                }
                recipient = str(payload.get('to') or os.getenv('DELIVERY_NOTIFICATION_TO', '')).strip()
                if not local_test_mode and not all((settings['host'], settings['username'], settings['password'])):
                    raise ValueError('SMTP not configured')
                if not recipient:
                    if local_test_mode:
                        recipient = 'local-test@example.invalid'
                    else:
                        raise ValueError('notification recipient not configured')
                message = EmailMessage()
                message['Subject'] = _notification_subject(row)
                message['From'] = settings['username'] or 'stormpilot-local@example.invalid'
                message['To'] = recipient
                message.set_content(_notification_email_body(conn, row, payload))
                if not local_test_mode:
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
