"""Nearest eligible drone assignment engine using real phone GPS locations.

Provides Haversine distance calculation and concurrency-safe nearest-eligible
drone selection for dispatch mission assignment. Seeded coordinates are kept
for testing only and are never preferred over real participant locations.
"""
import math
import secrets
from datetime import datetime, timedelta, timezone

try:
    from app import db as store
except ImportError:  # pragma: no cover
    import db as store

EARTH_RADIUS_KM = 6371.0088

# A location older than this is considered stale and the drone is ineligible.
STALE_LOCATION_SECONDS = 180

# Eligible drone statuses for assignment.
ELIGIBLE_STATUSES = ('available',)

# Mission statuses that make a drone busy / ineligible for new assignment.
MISSION_STATUSES_BUSY = ('assigned', 'accepted', 'in_progress')


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def haversine_distance_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometers between two points (Haversine)."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    try:
        lat1 = float(lat1)
        lon1 = float(lon1)
        lat2 = float(lat2)
        lon2 = float(lon2)
    except (TypeError, ValueError):
        return None
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def _parse_time(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _drone_position_state(drone_row, latest_location, now):
    """Determine whether a drone is PHONE_GPS / STALE / OFFLINE."""
    lat = drone_row['latitude']
    lng = drone_row['longitude']
    if lat is None or lng is None:
        return 'OFFLINE', None
    received = _parse_time(latest_location['server_received_at']) if latest_location else None
    device = _parse_time(latest_location['device_timestamp']) if latest_location else None
    fresh = (
        latest_location is not None
        and received is not None
        and 0 <= (now - received).total_seconds() <= STALE_LOCATION_SECONDS
    )
    if fresh:
        return 'PHONE_GPS', latest_location
    if lat is not None and lng is not None:
        return 'STALE', latest_location
    return 'OFFLINE', None


def _eligible_drones(conn, now):
    """Return list of eligible drone dicts with their latest valid location."""
    drones = conn.execute(
        '''
        SELECT d.drone_id, d.status, d.latitude, d.longitude, d.gps_accuracy, d.last_update,
               l.latitude AS loc_lat, l.longitude AS loc_lng, l.accuracy AS loc_accuracy,
               l.device_timestamp, l.server_received_at
        FROM drone_registrations d
        LEFT JOIN location_updates l ON l.id = (
            SELECT id FROM location_updates
            WHERE drone_id = d.drone_id
            ORDER BY server_received_at DESC, id DESC LIMIT 1
        )
        ORDER BY d.drone_id
        '''
    ).fetchall()

    eligible = []
    for row in drones:
        latest_location = {
            'latitude': row['loc_lat'],
            'longitude': row['loc_lng'],
            'accuracy': row['loc_accuracy'],
            'device_timestamp': row['device_timestamp'],
            'server_received_at': row['server_received_at'],
        } if row['loc_lat'] is not None else None

        state, _ = _drone_position_state(row, latest_location, now)

        if row['status'] not in ELIGIBLE_STATUSES:
            continue
        if state != 'PHONE_GPS':
            continue

        busy = conn.execute(
            '''
            SELECT 1 FROM missions
            WHERE drone_id = ? AND status IN (?, ?, ?)
            LIMIT 1
            ''',
            (row['drone_id'], *MISSION_STATUSES_BUSY),
        ).fetchone()
        if busy:
            continue

        invitation = conn.execute(
            '''
            SELECT token, expires_at, revoked FROM invitations
            WHERE drone_id = ? AND revoked = 0
            ORDER BY expires_at DESC LIMIT 1
            ''',
            (row['drone_id'],),
        ).fetchone()
        if invitation is None:
            continue
        expires = _parse_time(invitation['expires_at'])
        if expires is None or now > expires:
            continue

        participant = conn.execute(
            'SELECT consent_given_at FROM participants WHERE drone_id = ?',
            (row['drone_id'],),
        ).fetchone()
        if participant is None or participant['consent_given_at'] is None:
            continue

        eligible.append({
            'drone_id': row['drone_id'],
            'latitude': row['loc_lat'],
            'longitude': row['loc_lng'],
            'accuracy': row['loc_accuracy'],
            'last_update': row['server_received_at'],
        })
    return eligible


def find_nearest_drone(dest_lat, dest_lng):
    """Return the drone_id of the nearest eligible drone to the destination."""
    conn = store.get_connection()
    try:
        now = _now()
        eligible = _eligible_drones(conn, now)
        if not eligible:
            return None
        best = None
        best_km = None
        for drone in eligible:
            km = haversine_distance_km(
                drone['latitude'], drone['longitude'], dest_lat, dest_lng
            )
            if km is None:
                continue
            if best_km is None or km < best_km:
                best = drone['drone_id']
                best_km = km
        return best
    finally:
        conn.close()


def assign_mission(dest_lat, dest_lng):
    """Assign a dispatch mission to the nearest eligible drone.

    Uses a database transaction with BEGIN IMMEDIATE to prevent concurrent
    dispatch requests from assigning the same available drone.

    Returns a dict with assignment info, or None if no eligible drone exists.
    """
    if dest_lat is None or dest_lng is None:
        return None
    try:
        dest_lat = float(dest_lat)
        dest_lng = float(dest_lng)
    except (TypeError, ValueError):
        return None

    conn = store.get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            now = _now()
            eligible = _eligible_drones(conn, now)
            if not eligible:
                conn.rollback()
                return None

            best = None
            best_km = None
            best_lat = None
            best_lng = None
            for drone in eligible:
                km = haversine_distance_km(
                    drone['latitude'], drone['longitude'], dest_lat, dest_lng
                )
                if km is None:
                    continue
                if best_km is None or km < best_km:
                    best = drone['drone_id']
                    best_km = km
                    best_lat = drone['latitude']
                    best_lng = drone['longitude']

            if best is None:
                conn.rollback()
                return None

            mission_id = 'mission-' + secrets.token_urlsafe(16)
            distance_km = round(best_km, 3)
            conn.execute(
                '''
                INSERT INTO missions (
                    mission_id, drone_id, destination_lat, destination_lng,
                    distance_km, status, assigned_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'assigned', ?, ?, ?)
                ''',
                (
                    mission_id, best, dest_lat, dest_lng, distance_km,
                    _iso(now), _iso(now), _iso(now),
                ),
            )
            conn.execute(
                '''
                UPDATE drone_registrations
                SET status = 'busy', mission_id = ?, updated_at = ?
                WHERE drone_id = ?
                ''',
                (mission_id, _iso(now), best),
            )
            _record_mission_event(conn, mission_id, 'assigned', {
                'drone_id': best,
                'destination_lat': dest_lat,
                'destination_lng': dest_lng,
                'distance_km': distance_km,
                'drone_lat': best_lat,
                'drone_lng': best_lng,
            })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    return {
        'mission_id': mission_id,
        'drone_id': best,
        'distance_km': distance_km,
        'destination_lat': dest_lat,
        'destination_lng': dest_lng,
        'drone_lat': best_lat,
        'drone_lng': best_lng,
        'status': 'assigned',
        'assigned_at': _iso(now),
    }


def _record_mission_event(conn, mission_id, event_type, payload=None):
    import json
    event_id = 'event-' + secrets.token_urlsafe(12)
    conn.execute(
        '''
        INSERT INTO mission_events (id, mission_id, event_type, payload, created_at)
        VALUES (?, ?, ?, ?, ?)
        ''',
        (
            event_id, mission_id, event_type,
            json.dumps(payload or {}, separators=(',', ':'), sort_keys=True),
            _iso(_now()),
        ),
    )

