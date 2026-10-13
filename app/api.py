"""StormPilot fleet operations API with a legacy Redmine proxy."""
import json
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
DELIVERY_CONFIRMATIONS = {}


def handle(method, path, body=None):
    if method == 'GET' and path == '/api/config':
        return 200, {
            'google_maps_api_key': os.getenv('GOOGLE_MAPS_API_KEY', '').strip(),
            'demo_mode': True,
            'map_ready': bool(os.getenv('GOOGLE_MAPS_API_KEY', '').strip()),
        }

    if method == 'GET' and path == '/api/fleet':
        return 200, _fleet_payload()

    if method == 'POST' and path == '/api/device/telemetry':
        return _handle_telemetry(body)

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


def _handle_telemetry(body):
    data = _read_json(body)
    if not isinstance(data, dict):
        return 400, {'error': 'Invalid GPS payload. Expected a JSON object.'}

    drone_id = str(data.get('drone_id', '')).strip()
    latitude = _float(data.get('lat'))
    longitude = _float(data.get('lng'))
    accuracy_m = _float(data.get('accuracy'))
    timestamp = data.get('timestamp')

    if (not drone_id or latitude is None or longitude is None or accuracy_m is None or accuracy_m <= 0 or not timestamp):
        return 400, {'error': 'Invalid GPS payload. Latitude, longitude, accuracy and timestamp are required.'}
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return 400, {'error': 'Invalid GPS payload. Coordinates are outside valid Earth bounds.'}

    try:
        parsed = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        iso = parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except ValueError:
        return 400, {'error': 'Invalid GPS payload. Timestamp must be a valid ISO 8601 value.'}

    valid_ids = {drone['drone_id'] for drone in DEMO_DRONES}
    if drone_id not in valid_ids:
        return 403, {'error': 'Unknown drone ID.'}

    TELEMETRY_STORE[drone_id] = {
        'drone_id': drone_id,
        'latitude': latitude,
        'longitude': longitude,
        'accuracy_m': accuracy_m,
        'last_update': iso,
        'source': 'PHONE_GPS',
        'status': 'tracking',
    }
    return 200, {'status': 'accepted', 'drone_id': drone_id, 'timestamp': iso, 'accuracy_m': accuracy_m}


def _handle_delivery_confirm(body):
    data = _read_json(body)
    if not isinstance(data, dict):
        return 400, {'error': 'Invalid delivery confirmation payload.'}

    delivery_id = str(data.get('delivery_id', '')).strip()
    drone_id = str(data.get('drone_id', '')).strip()
    if not delivery_id or not drone_id:
        return 400, {'error': 'Delivery ID and drone ID are required.'}

    previous = DELIVERY_CONFIRMATIONS.get(delivery_id)
    if previous:
        return 200, {'status': 'already_confirmed', 'delivery_id': delivery_id, 'drone_id': previous['drone_id']}

    DELIVERY_CONFIRMATIONS[delivery_id] = {
        'delivery_id': delivery_id,
        'drone_id': drone_id,
        'confirmed_by': str(data.get('confirmed_by', 'operator')).strip() or 'operator',
        'handover_confirmed': bool(data.get('handover_confirmed', False)),
        'timestamp': _utc_now(),
    }
    return 200, {'status': 'confirmed', 'delivery_id': delivery_id, 'drone_id': drone_id}


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
