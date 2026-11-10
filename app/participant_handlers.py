"""Participant invitation, consent, telemetry and mission handlers.

Supports the five-drone smartphone simulation: invitations, participant consent,
phone GPS telemetry, and mission lifecycle actions. All handlers are imported
by app.api and routed through the existing `handle()` dispatcher.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

try:
    from app import db as store
    from app import assignment_engine
except ImportError:  # pragma: no cover
    import db as store
    import assignment_engine


def _invitation_token():
    return 'inv-' + secrets.token_urlsafe(32)


def _parse_participant_time(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _handle_generate_invitations(data, user):
    if not isinstance(data, dict):
        return 400, {'error': 'Expected a JSON object.'}
    count = data.get('count', 5)
    try:
        count = int(count)
    except (TypeError, ValueError):
        return 400, {'error': 'Count must be a whole number.'}
    if count < 1 or count > 20:
        return 400, {'error': 'Count must be between 1 and 20.'}
    expires_in_hours = data.get('expires_in_hours', 168)
    try:
        expires_in_hours = int(expires_in_hours)
    except (TypeError, ValueError):
        return 400, {'error': 'expires_in_hours must be a whole number.'}
    if expires_in_hours < 1 or expires_in_hours > 720:
        return 400, {'error': 'expires_in_hours must be between 1 and 720.'}

    conn = store.get_connection()
    try:
        drones = conn.execute(
            'SELECT drone_id FROM drone_registrations ORDER BY drone_id LIMIT ?',
            (count,),
        ).fetchall()
        if len(drones) < count:
            return 400, {'error': f'Only {len(drones)} drone identities available; need {count}.'}
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(hours=expires_in_hours)).isoformat().replace('+00:00', 'Z')
        created = []
        with conn:
            for row in drones:
                drone_id = row['drone_id']
                token = _invitation_token()
                invitation_id = 'inv-' + secrets.token_urlsafe(16)
                conn.execute(
                    'DELETE FROM invitations WHERE drone_id = ?',
                    (drone_id,),
                )
                conn.execute(
                    'INSERT INTO invitations (invitation_id, token, drone_id, expires_at, revoked, created_at, updated_at) '
                    'VALUES (?, ?, ?, ?, 0, ?, ?)',
                    (invitation_id, token, drone_id, expires_at, _now_iso(), _now_iso()),
                )
                conn.execute(
                    'INSERT OR IGNORE INTO participants (participant_id, drone_id, created_at, updated_at) '
                    'VALUES (?, ?, ?, ?)',
                    ('participant-' + secrets.token_urlsafe(12), drone_id, _now_iso(), _now_iso()),
                )
                created.append({
                    'drone_id': drone_id,
                    'token': token,
                    'expires_at': expires_at,
                })
            conn.commit()
    except sqlite3.DatabaseError:
        return 500, {'error': 'Invitation storage is temporarily unavailable.'}
    finally:
        conn.close()

    store.record_event('INVITATIONS_GENERATED', 'ADMIN_ACTION', user.get('user_id', 'system'), {
        'count': count, 'expires_in_hours': expires_in_hours,
    })
    return 201, {'invitations': created}


def _handle_list_invitations():
    conn = store.get_connection()
    try:
        rows = conn.execute(
            '''
            SELECT i.invitation_id, i.token, i.drone_id, i.expires_at, i.revoked,
                   i.used_by, i.created_at, i.updated_at,
                   p.participant_id, p.consent_given_at, p.consent_revoked_at
            FROM invitations i
            LEFT JOIN participants p ON p.drone_id = i.drone_id
            ORDER BY i.drone_id
            '''
        ).fetchall()
        invitations = []
        now = datetime.now(timezone.utc)
        for row in rows:
            expires = _parse_participant_time(row['expires_at'])
            expired = expires is not None and now > expires
            active = (row['revoked'] == 0 and not expired and row['used_by'] is None)
            invitations.append({
                'invitation_id': row['invitation_id'],
                'drone_id': row['drone_id'],
                'expires_at': row['expires_at'],
                'revoked': bool(row['revoked']),
                'expired': expired,
                'active': active,
                'used_by': row['used_by'],
                'participant_id': row['participant_id'],
                'consent_given_at': row['consent_given_at'],
                'consent_revoked_at': row['consent_revoked_at'],
                'created_at': row['created_at'],
                'updated_at': row['updated_at'],
            })
    except sqlite3.DatabaseError:
        return 500, {'error': 'Invitation storage is temporarily unavailable.'}
    finally:
        conn.close()
    return 200, {'invitations': invitations}


def _handle_revoke_invitation(invitation_id, user):
    conn = store.get_connection()
    try:
        row = conn.execute('SELECT * FROM invitations WHERE invitation_id = ?', (invitation_id,)).fetchone()
        if row is None:
            return 404, {'error': 'Invitation not found.'}
        with conn:
            conn.execute(
                'UPDATE invitations SET revoked = 1, updated_at = ? WHERE invitation_id = ?',
                (_now_iso(), invitation_id),
            )
            conn.commit()
    except sqlite3.DatabaseError:
        return 500, {'error': 'Invitation storage is temporarily unavailable.'}
    finally:
        conn.close()

    store.record_event('INVITATION_REVOKED', 'ADMIN_ACTION', user.get('user_id', 'system'), {
        'invitation_id': invitation_id, 'drone_id': row['drone_id'],
    })
    return 200, {'status': 'revoked', 'invitation_id': invitation_id, 'drone_id': row['drone_id']}


def _handle_participant_consent(data, headers):
    if not isinstance(data, dict):
        return 400, {'error': 'Expected a JSON object.'}
    token = str(data.get('token', '')).strip()
    consent = data.get('consent', True)
    device_info = str(data.get('device_info', '')).strip()[:128]
    if not token:
        return 400, {'error': 'Invitation token is required.'}
    if not isinstance(consent, bool):
        return 400, {'error': 'consent must be a boolean.'}

    conn = store.get_connection()
    try:
        invitation = conn.execute('SELECT * FROM invitations WHERE token = ?', (token,)).fetchone()
        if invitation is None:
            return 401, {'error': 'Invalid invitation token.'}
        if invitation['revoked'] == 1:
            return 401, {'error': 'Invitation has been revoked.'}
        expires = _parse_participant_time(invitation['expires_at'])
        now = datetime.now(timezone.utc)
        if expires is None or now > expires:
            return 401, {'error': 'Invitation has expired.'}
        drone_id = invitation['drone_id']

        if consent:
            participant = conn.execute(
                'SELECT * FROM participants WHERE drone_id = ?', (drone_id,)
            ).fetchone()
            if participant is None:
                conn.execute(
                    'INSERT INTO participants (participant_id, drone_id, consent_given_at, consent_revoked_at, device_info, created_at, updated_at) '
                    'VALUES (?, ?, ?, NULL, ?, ?, ?)',
                    ('participant-' + secrets.token_urlsafe(12), drone_id, _now_iso(), device_info, _now_iso(), _now_iso()),
                )
            else:
                conn.execute(
                    'UPDATE participants SET consent_given_at = ?, consent_revoked_at = NULL, device_info = ?, updated_at = ? WHERE drone_id = ?',
                    (_now_iso(), device_info, _now_iso(), drone_id),
                )
            conn.commit()
        else:
            conn.execute(
                'UPDATE participants SET consent_revoked_at = ?, device_info = ?, updated_at = ? WHERE drone_id = ?',
                (_now_iso(), device_info, _now_iso(), drone_id),
            )
            conn.execute(
                'UPDATE location_sessions SET active = 0, stopped_at = ?, active = 0 WHERE drone_id = ? AND active = 1',
                (_now_iso(), drone_id),
            )
            conn.commit()
    except sqlite3.DatabaseError:
        return 500, {'error': 'Participant storage is temporarily unavailable.'}
    finally:
        conn.close()

    store.record_event('PARTICIPANT_CONSENT', 'PARTICIPANT', invitation['drone_id'], {
        'drone_id': invitation['drone_id'], 'consent': consent,
    })
    return 200, {'status': 'consent_recorded', 'drone_id': invitation['drone_id'], 'consent': bool(consent)}


def _handle_participant_telemetry(data, headers):
    if not isinstance(data, dict):
        return 400, {'error': 'Expected a JSON object.'}
    token = str(data.get('token', '')).strip()
    latitude = data.get('latitude')
    longitude = data.get('longitude')
    accuracy = data.get('accuracy')
    device_timestamp = str(data.get('timestamp', '')).strip()
    if not token:
        return 401, {'error': 'Invitation token is required.'}
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return 400, {'error': 'Valid latitude and longitude are required.'}
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return 400, {'error': 'Coordinates are outside valid Earth bounds.'}
    if accuracy is not None:
        try:
            accuracy = float(accuracy)
        except (TypeError, ValueError):
            return 400, {'error': 'Accuracy must be a number.'}
    if not device_timestamp:
        device_timestamp = _now_iso()

    conn = store.get_connection()
    try:
        invitation = conn.execute(
            'SELECT * FROM invitations WHERE token = ? AND revoked = 0', (token,)
        ).fetchone()
        if invitation is None:
            return 401, {'error': 'Invalid or revoked invitation token.'}
        expires = _parse_participant_time(invitation['expires_at'])
        now = datetime.now(timezone.utc)
        if expires is None or now > expires:
            return 401, {'error': 'Invitation has expired.'}
        drone_id = invitation['drone_id']
        participant = conn.execute(
            'SELECT * FROM participants WHERE drone_id = ?', (drone_id,)
        ).fetchone()
        if participant is None or participant['consent_given_at'] is None:
            return 403, {'error': 'Participant has not given consent.'}

        session = conn.execute(
            'SELECT * FROM location_sessions WHERE drone_id = ? AND active = 1 ORDER BY started_at DESC LIMIT 1',
            (drone_id,),
        ).fetchone()
        if session is None:
            session_id = 'session-' + secrets.token_urlsafe(16)
            conn.execute(
                'INSERT INTO location_sessions (session_id, participant_id, drone_id, active, started_at, created_at) '
                'VALUES (?, ?, ?, 1, ?, ?, ?)',
                (session_id, participant['participant_id'], drone_id, _now_iso(), _now_iso(), _now_iso()),
            )
            session_id_value = session_id
        else:
            session_id_value = session['session_id']

        parsed_ts = _parse_participant_time(device_timestamp) or now
        conn.execute(
            'INSERT INTO location_updates (session_id, drone_id, latitude, longitude, accuracy, device_timestamp, server_received_at, source) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (session_id_value, drone_id, latitude, longitude, accuracy, device_timestamp, _now_iso(), 'PHONE_GPS'),
        )
        conn.execute(
            'UPDATE location_sessions SET last_location_at = ? WHERE session_id = ?',
            (_now_iso(), session_id_value),
        )
        conn.execute(
            'UPDATE drone_registrations SET latitude = ?, longitude = ?, gps_accuracy = ?, last_update = ?, status = ?, source = ?, updated_at = ? WHERE drone_id = ?',
            (latitude, longitude, accuracy, _now_iso(), 'available', 'PHONE_GPS', _now_iso(), drone_id),
        )
        conn.commit()
    except sqlite3.DatabaseError:
        return 500, {'error': 'Location storage is temporarily unavailable.'}
    finally:
        conn.close()

    store.record_event('LOCATION_UPDATE', 'DRONE', drone_id, {
        'drone_id': drone_id, 'latitude': latitude, 'longitude': longitude,
        'accuracy_m': accuracy, 'device_timestamp': device_timestamp,
    })
    return 200, {'status': 'accepted', 'drone_id': drone_id, 'timestamp': _now_iso()}


def _participant_headers(headers):
    if not headers:
        return None, 'Authorization header is required.'
    auth = headers.get('Authorization') or headers.get('authorization')
    if not auth:
        return None, 'Authorization header is required.'
    token = str(auth).strip()
    if token.lower().startswith('bearer '):
        token = token.split(None, 1)[1].strip()
    if not token:
        return None, 'Authorization header is required.'
    conn = store.get_connection()
    try:
        invitation = conn.execute('SELECT * FROM invitations WHERE token = ? AND revoked = 0', (token,)).fetchone()
        if invitation is None:
            return None, 'Invalid or revoked invitation token.'
        expires = _parse_participant_time(invitation['expires_at'])
        now = datetime.now(timezone.utc)
        if expires is None or now > expires:
            return None, 'Invitation has expired.'
        return {'token': token, 'drone_id': invitation['drone_id']}, None
    finally:
        conn.close()


def _handle_participant_mission(headers):
    participant, error = _participant_headers(headers)
    if error:
        return 401, {'error': error}
    drone_id = participant['drone_id']
    conn = store.get_connection()
    try:
        mission = conn.execute(
            'SELECT * FROM missions WHERE drone_id = ? ORDER BY created_at DESC LIMIT 1',
            (drone_id,),
        ).fetchone()
        if mission is None:
            return 200, {'mission': None, 'drone_id': drone_id}
        return 200, {
            'mission': {
                'mission_id': mission['mission_id'],
                'drone_id': mission['drone_id'],
                'destination_lat': mission['destination_lat'],
                'destination_lng': mission['destination_lng'],
                'distance_km': mission['distance_km'],
                'status': mission['status'],
                'assigned_at': mission['assigned_at'],
                'accepted_at': mission['accepted_at'],
                'started_at': mission['started_at'],
                'completed_at': mission['completed_at'],
                'cancelled_at': mission['cancelled_at'],
                'created_at': mission['created_at'],
                'updated_at': mission['updated_at'],
            },
            'drone_id': drone_id,
        }
    except sqlite3.DatabaseError:
        return 500, {'error': 'Mission storage is temporarily unavailable.'}
    finally:
        conn.close()


def _handle_participant_mission_action(path, action, headers, data):
    participant, error = _participant_headers(headers)
    if error:
        return 401, {'error': error}
    drone_id = participant['drone_id']
    mission_id = path.split('/')[-2]
    if not mission_id:
        return 404, {'error': 'Mission not found.'}

    valid_actions = ('accept', 'decline', 'start', 'complete')
    if action not in valid_actions:
        return 400, {'error': 'Invalid mission action.'}

    conn = store.get_connection()
    try:
        mission = conn.execute('SELECT * FROM missions WHERE mission_id = ? AND drone_id = ?', (mission_id, drone_id)).fetchone()
        if mission is None:
            return 404, {'error': 'Mission not found or not assigned to this drone.'}
        now = _now_iso()
        current_status = mission['status']

        if action == 'accept':
            if current_status != 'assigned':
                return 409, {'error': f'Cannot accept a mission in {current_status} state.'}
            new_status = 'accepted'
            updates = {'status': 'accepted', 'accepted_at': now, 'updated_at': now}
            event_type = 'MISSION_ACCEPTED'
        elif action == 'decline':
            if current_status != 'assigned':
                return 409, {'error': f'Cannot decline a mission in {current_status} state.'}
            new_status = 'declined'
            updates = {'status': 'declined', 'updated_at': now}
            conn.execute(
                'UPDATE drone_registrations SET status = \'available\', mission_id = NULL, updated_at = ? WHERE drone_id = ?',
                (now, drone_id),
            )
            event_type = 'MISSION_DECLINED'
        elif action == 'start':
            if current_status != 'accepted':
                return 409, {'error': f'Cannot start a mission in {current_status} state.'}
            new_status = 'in_progress'
            updates = {'status': 'in_progress', 'started_at': now, 'updated_at': now}
            event_type = 'MISSION_STARTED'
        elif action == 'complete':
            if current_status != 'in_progress':
                return 409, {'error': f'Cannot complete a mission in {current_status} state.'}
            new_status = 'completed'
            updates = {'status': 'completed', 'completed_at': now, 'updated_at': now}
            conn.execute(
                'UPDATE drone_registrations SET status = \'available\', mission_id = NULL, updated_at = ? WHERE drone_id = ?',
                (now, drone_id),
            )
            event_type = 'MISSION_COMPLETED'

        set_clause = ', '.join(f'{k} = ?' for k in updates)
        values = list(updates.values()) + [mission_id]
        with conn:
            conn.execute(f'UPDATE missions SET {set_clause} WHERE mission_id = ?', values)
            conn.execute(
                'INSERT INTO mission_events (mission_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)',
                (mission_id, event_type, json.dumps({
                    'drone_id': drone_id, 'status': new_status, 'action': action,
                }, separators=(',', ':'), sort_keys=True), now),
            )
            conn.commit()
    except sqlite3.DatabaseError:
        return 500, {'error': 'Mission storage is temporarily unavailable.'}
    finally:
        conn.close()

    store.record_event(event_type, 'MISSION', mission_id, {
        'mission_id': mission_id, 'drone_id': drone_id, 'status': new_status, 'action': action,
    })
    return 200, {'status': new_status, 'mission_id': mission_id, 'drone_id': drone_id}


def _handle_create_mission(data, user):
    if not isinstance(data, dict):
        return 400, {'error': 'Expected a JSON object.'}
    drone_id = str(data.get('drone_id', '')).strip()
    destination_lat = data.get('destination_lat')
    destination_lng = data.get('destination_lng')
    if not drone_id:
        return 400, {'error': 'drone_id is required.'}
    try:
        destination_lat = float(destination_lat)
        destination_lng = float(destination_lng)
    except (TypeError, ValueError):
        return 400, {'error': 'Valid destination_lat and destination_lng are required.'}
    if not (-90 <= destination_lat <= 90 and -180 <= destination_lng <= 180):
        return 400, {'error': 'Destination coordinates are outside valid Earth bounds.'}

    from app import assignment_engine

    conn = store.get_connection()
    try:
        drone = conn.execute(
            'SELECT * FROM drone_registrations WHERE drone_id = ?', (drone_id,)
        ).fetchone()
        if drone is None:
            return 404, {'error': f'Drone {drone_id} not found.'}
        if drone['status'] != 'available':
            return 409, {'error': f'Drone {drone_id} is not available (status: {drone["status"]}).'}
        existing = conn.execute(
            'SELECT mission_id FROM missions WHERE drone_id = ? AND status IN (?, ?, ?) ORDER BY created_at DESC LIMIT 1',
            (drone_id, 'assigned', 'accepted', 'in_progress'),
        ).fetchone()
        if existing:
            return 409, {'error': f'Drone {drone_id} already has an active mission ({existing["mission_id"]}).'}

        distance_km = assignment_engine.haversine_distance_km(
            drone['latitude'], drone['longitude'], destination_lat, destination_lng,
        ) if drone['latitude'] is not None and drone['longitude'] is not None else None

        mission_id = 'mission-' + secrets.token_urlsafe(16)
        now = _now_iso()
        with conn:
            conn.execute(
                'INSERT INTO missions (mission_id, drone_id, destination_lat, destination_lng, distance_km, status, assigned_at, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (mission_id, drone_id, destination_lat, destination_lng, distance_km, 'assigned', now, now, now),
            )
            conn.execute(
                'UPDATE drone_registrations SET status = \'assigned\', mission_id = ?, updated_at = ? WHERE drone_id = ?',
                (mission_id, now, drone_id),
            )
            conn.commit()
    except sqlite3.DatabaseError:
        return 500, {'error': 'Mission storage is temporarily unavailable.'}
    finally:
        conn.close()

    store.record_event('MISSION_CREATED', 'MISSION', mission_id, {
        'mission_id': mission_id, 'drone_id': drone_id,
        'destination_lat': destination_lat, 'destination_lng': destination_lng,
        'distance_km': distance_km,
    })
    return 201, {'mission_id': mission_id, 'drone_id': drone_id, 'status': 'assigned', 'distance_km': distance_km}


def _handle_list_missions():
    conn = store.get_connection()
    try:
        rows = conn.execute(
            'SELECT * FROM missions ORDER BY created_at DESC'
        ).fetchall()
        missions = []
        for row in rows:
            missions.append({
                'mission_id': row['mission_id'],
                'drone_id': row['drone_id'],
                'destination_lat': row['destination_lat'],
                'destination_lng': row['destination_lng'],
                'distance_km': row['distance_km'],
                'status': row['status'],
                'assigned_at': row['assigned_at'],
                'accepted_at': row['accepted_at'],
                'started_at': row['started_at'],
                'completed_at': row['completed_at'],
                'cancelled_at': row['cancelled_at'],
                'created_at': row['created_at'],
                'updated_at': row['updated_at'],
            })
        return 200, {'missions': missions, 'count': len(missions)}
    except sqlite3.DatabaseError:
        return 500, {'error': 'Mission storage is temporarily unavailable.'}
    finally:
        conn.close()


def _handle_get_mission(mission_id):
    conn = store.get_connection()
    try:
        row = conn.execute('SELECT * FROM missions WHERE mission_id = ?', (mission_id,)).fetchone()
        if row is None:
            return 404, {'error': 'Mission not found.'}
        return 200, {
            'mission': {
                'mission_id': row['mission_id'],
                'drone_id': row['drone_id'],
                'destination_lat': row['destination_lat'],
                'destination_lng': row['destination_lng'],
                'distance_km': row['distance_km'],
                'status': row['status'],
                'assigned_at': row['assigned_at'],
                'accepted_at': row['accepted_at'],
                'started_at': row['started_at'],
                'completed_at': row['completed_at'],
                'cancelled_at': row['cancelled_at'],
                'created_at': row['created_at'],
                'updated_at': row['updated_at'],
            }
        }
    except sqlite3.DatabaseError:
        return 500, {'error': 'Mission storage is temporarily unavailable.'}
    finally:
        conn.close()


def _handle_cancel_mission(mission_id, user):
    conn = store.get_connection()
    try:
        mission = conn.execute('SELECT * FROM missions WHERE mission_id = ?', (mission_id,)).fetchone()
        if mission is None:
            return 404, {'error': 'Mission not found.'}
        if mission['status'] in ('completed', 'cancelled', 'declined'):
            return 409, {'error': f'Cannot cancel a mission in {mission["status"]} state.'}
        now = _now_iso()
        with conn:
            conn.execute(
                'UPDATE missions SET status = ?, cancelled_at = ?, updated_at = ? WHERE mission_id = ?',
                ('cancelled', now, now, mission_id),
            )
            conn.execute(
                'UPDATE drone_registrations SET status = \'available\', mission_id = NULL, updated_at = ? WHERE drone_id = ?',
                (now, mission['drone_id']),
            )
            conn.execute(
                'INSERT INTO mission_events (mission_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)',
                (mission_id, 'MISSION_CANCELLED', json.dumps({
                    'drone_id': mission['drone_id'], 'cancelled_by': user.get('user_id', 'system'),
                    'action': 'cancel',
                }, separators=(',', ':'), sort_keys=True), now),
            )
            conn.commit()
    except sqlite3.DatabaseError:
        return 500, {'error': 'Mission storage is temporarily unavailable.'}
    finally:
        conn.close()

    store.record_event('MISSION_CANCELLED', 'MISSION', mission_id, {
        'mission_id': mission_id, 'drone_id': mission['drone_id'],
        'cancelled_by': user.get('user_id', 'system'),
    })
    return 200, {'status': 'cancelled', 'mission_id': mission_id, 'drone_id': mission['drone_id']}
