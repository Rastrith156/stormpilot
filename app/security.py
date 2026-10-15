"""Password-backed administrator identity, sessions, authorization and audit."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

try:
    from app import db as store
except ImportError:  # pragma: no cover
    import db as store

ROLES = frozenset({'ADMIN', 'OPERATOR', 'VIEWER'})
PASSWORD_ITERATIONS = 600_000
SESSION_COOKIE = 'stormpilot_session'
CSRF_COOKIE = 'stormpilot_csrf'
SESSION_HOURS = 8
LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW_MINUTES = 15
LOGIN_LOCK_MINUTES = 15


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _timestamp(value):
    return value.isoformat().replace('+00:00', 'Z')


def _password_hash(password, salt, iterations=PASSWORD_ITERATIONS):
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)


def _validate_username(username):
    if not isinstance(username, str):
        return None
    normalized = username.strip().lower()
    return normalized if re.fullmatch(r'[a-z0-9][a-z0-9._@+-]{2,63}', normalized) else None


def _validate_password(password):
    return isinstance(password, str) and 12 <= len(password) <= 1024


def create_user(username, password, role, actor='system'):
    username = _validate_username(username)
    if username is None:
        return 400, {'error': 'Username must be 3-64 characters using letters, digits, dot, underscore, @, + or hyphen.'}
    if not _validate_password(password):
        return 400, {'error': 'Password must contain between 12 and 1024 characters.'}
    if not isinstance(role, str) or role not in ROLES:
        return 400, {'error': 'Role must be ADMIN, OPERATOR or VIEWER.'}
    salt = secrets.token_bytes(16)
    password_hash = _password_hash(password, salt)
    user_id = str(uuid.uuid4())
    conn = store.get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO admin_users (
                    user_id, username, password_hash, password_salt,
                    password_iterations, role
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    base64.b64encode(password_hash).decode('ascii'),
                    base64.b64encode(salt).decode('ascii'),
                    PASSWORD_ITERATIONS,
                    role,
                ),
            )
            _audit_with_connection(conn, 'ADMIN_USER_CREATED', username, {
                'actor': actor,
                'role': role,
            })
    except sqlite3.IntegrityError:
        return 409, {'error': 'An account with that username already exists.'}
    finally:
        conn.close()
    return 201, {'user_id': user_id, 'username': username, 'role': role, 'enabled': True}


def bootstrap_admin(username, password):
    conn = store.get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            count = conn.execute('SELECT COUNT(*) FROM admin_users').fetchone()[0]
            if count:
                return 409, {'error': 'Administrator bootstrap is closed because accounts already exist.'}
            username = _validate_username(username)
            if username is None:
                return 400, {'error': 'Username must be 3-64 characters using letters, digits, dot, underscore, @, + or hyphen.'}
            if not _validate_password(password):
                return 400, {'error': 'Password must contain between 12 and 1024 characters.'}
            salt = secrets.token_bytes(16)
            user_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO admin_users (
                    user_id, username, password_hash, password_salt,
                    password_iterations, role
                ) VALUES (?, ?, ?, ?, ?, 'ADMIN')
                """,
                (
                    user_id,
                    username,
                    base64.b64encode(_password_hash(password, salt)).decode('ascii'),
                    base64.b64encode(salt).decode('ascii'),
                    PASSWORD_ITERATIONS,
                ),
            )
            _audit_with_connection(conn, 'ADMIN_USER_CREATED', username, {
                'actor': 'bootstrap',
                'role': 'ADMIN',
            })
        return 201, {'user_id': user_id, 'username': username, 'role': 'ADMIN', 'enabled': True}
    except sqlite3.IntegrityError:
        return 409, {'error': 'An account with that username already exists.'}
    finally:
        conn.close()


def login(payload, headers=None):
    if not isinstance(payload, dict):
        return 400, {'error': 'Expected a JSON object.'}, []
    username = _validate_username(payload.get('username'))
    password = payload.get('password')
    if username is None or not isinstance(password, str) or len(password) > 1024:
        _record_failed_login(_header(headers, 'X-Client-IP'))
        return 401, {'error': 'Invalid username or password.'}, []

    now = _now()
    network_key = _login_network_key(_header(headers, 'X-Client-IP'))
    account_key = f'account:{username}'
    conn = store.get_connection()
    try:
        attempts = conn.execute(
            'SELECT username_key, failures, window_started_at, locked_until FROM admin_login_attempts WHERE username_key IN (?, ?)',
            (account_key, network_key),
        ).fetchall()
        if any(attempt['locked_until'] and attempt['locked_until'] > _timestamp(now) for attempt in attempts):
            with conn:
                _audit_with_connection(conn, 'ADMIN_LOGIN_RATE_LIMITED', 'login', {})
            return 429, {'error': 'Too many login attempts. Try again later.'}, []
        user = conn.execute(
            'SELECT * FROM admin_users WHERE username = ? COLLATE NOCASE',
            (username,),
        ).fetchone()
        try:
            salt = base64.b64decode(user['password_salt'], validate=True) if user else bytes.fromhex('6f4a2c0d8b1e3957a6c4f2d0e8b13579')
            expected = base64.b64decode(user['password_hash'], validate=True) if user else bytes(32)
            iterations = user['password_iterations'] if user else PASSWORD_ITERATIONS
            actual = _password_hash(password, salt, iterations)
            password_matches = hmac.compare_digest(actual, expected)
            valid = bool(user and user['enabled'] and password_matches)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            with conn:
                keys = ([account_key] if user else []) + [network_key]
                for key in keys:
                    attempt = next((item for item in attempts if item['username_key'] == key), None)
                    _record_failed_login_with_connection(conn, key, now, attempt)
                _audit_with_connection(conn, 'ADMIN_LOGIN_FAILED', username if user else 'unknown_account', {})
            return 401, {'error': 'Invalid username or password.'}, []

        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires = now + timedelta(hours=SESSION_HOURS)
        with conn:
            conn.execute('DELETE FROM admin_login_attempts WHERE username_key = ?', (account_key,))
            conn.execute('DELETE FROM admin_login_attempts WHERE username_key = ?', (network_key,))
            conn.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ? OR (revoked_at IS NOT NULL AND revoked_at <= ?)",
                (_timestamp(now), _timestamp(now - timedelta(days=30))),
            )
            conn.execute(
                """
                INSERT INTO admin_sessions (
                    session_hash, user_id, csrf_hash, created_at, expires_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    hashlib.sha256(session_id.encode('ascii')).hexdigest(),
                    user['user_id'],
                    hashlib.sha256(csrf_token.encode('ascii')).hexdigest(),
                    _timestamp(now),
                    _timestamp(expires),
                    _timestamp(now),
                ),
            )
            _audit_with_connection(conn, 'ADMIN_LOGIN_SUCCEEDED', username, {'role': user['role']})
        headers = _session_cookies(session_id, csrf_token, expires)
        return 200, {
            'user': _public_user(user),
            'expires_at': _timestamp(expires),
        }, headers
    finally:
        conn.close()


def session(headers):
    user, _, error = _authenticate(headers)
    if error:
        return 401, {'error': 'Administrator login required.'}
    return 200, {'user': user}


def logout(headers):
    user, csrf_cookie, error = _authenticate(headers, require_csrf=True)
    if error:
        return 403 if error == 'csrf' else 401, {
            'error': 'A valid administrator session and CSRF token are required.'
        }, []
    conn = store.get_connection()
    try:
        with conn:
            conn.execute(
                'UPDATE admin_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL',
                (_timestamp(_now()), hashlib.sha256(_cookie_value(headers, SESSION_COOKIE).encode('ascii')).hexdigest()),
            )
            _audit_with_connection(conn, 'ADMIN_LOGOUT', user['username'], {'role': user['role']})
    finally:
        conn.close()
    return 200, {'status': 'logged_out'}, _clear_session_cookies()


def authorize(headers, roles, action, method='GET'):
    user, _, error = _authenticate(headers, require_csrf=method not in ('GET', 'HEAD', 'OPTIONS'))
    if error:
        _audit('PRIVILEGED_ACTION_DENIED', action, {'reason': error, 'role_required': sorted(roles)})
        return None, (401 if error != 'csrf' else 403, {
            'error': 'A valid administrator session and CSRF token are required.'
            if error == 'csrf' else 'Administrator login required.'
        })
    if user['role'] not in roles:
        _audit('PRIVILEGED_ACTION_DENIED', action, {
            'reason': 'insufficient_role',
            'role': user['role'],
            'user_id': user['user_id'],
        })
        return None, (403, {'error': 'Your account role does not permit this action.'})
    if method not in ('GET', 'HEAD', 'OPTIONS'):
        _audit('PRIVILEGED_ACTION_AUTHORIZED', action, {
            'role': user['role'],
            'user_id': user['user_id'],
        })
    return user, None


def list_users():
    conn = store.get_connection()
    try:
        rows = conn.execute(
            'SELECT user_id, username, role, enabled, created_at, updated_at, password_changed_at FROM admin_users ORDER BY username'
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_user(user_id, payload, actor):
    if not isinstance(payload, dict):
        return 400, {'error': 'Expected a JSON object.'}
    allowed = {'enabled', 'role', 'password'}
    if not payload or set(payload) - allowed:
        return 400, {'error': 'Provide only enabled, role or password updates.'}
    if 'enabled' in payload and not isinstance(payload['enabled'], bool):
        return 400, {'error': 'enabled must be a boolean.'}
    if 'role' in payload and (
        not isinstance(payload['role'], str) or payload['role'] not in ROLES
    ):
        return 400, {'error': 'Role must be ADMIN, OPERATOR or VIEWER.'}
    if 'password' in payload and not _validate_password(payload['password']):
        return 400, {'error': 'Password must contain between 12 and 1024 characters.'}
    conn = store.get_connection()
    try:
        with conn:
            user = conn.execute('SELECT * FROM admin_users WHERE user_id = ?', (user_id,)).fetchone()
            if user is None:
                return 404, {'error': 'Account not found.'}
            disabling_admin = (
                user['role'] == 'ADMIN'
                and user['enabled']
                and (
                    payload.get('enabled') is False
                    or payload.get('role') in {'OPERATOR', 'VIEWER'}
                )
            )
            if disabling_admin:
                enabled_admins = conn.execute(
                    "SELECT COUNT(*) FROM admin_users WHERE role = 'ADMIN' AND enabled = 1"
                ).fetchone()[0]
                if enabled_admins <= 1:
                    return 409, {'error': 'The last enabled administrator cannot be disabled or demoted.'}
            if 'enabled' in payload:
                conn.execute(
                    'UPDATE admin_users SET enabled = ?, updated_at = ? WHERE user_id = ?',
                    (int(payload['enabled']), _timestamp(_now()), user_id),
                )
                if not payload['enabled']:
                    conn.execute(
                        'UPDATE admin_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL',
                        (_timestamp(_now()), user_id),
                    )
            if 'role' in payload:
                conn.execute(
                    'UPDATE admin_users SET role = ?, updated_at = ? WHERE user_id = ?',
                    (payload['role'], _timestamp(_now()), user_id),
                )
            if 'password' in payload:
                salt = secrets.token_bytes(16)
                conn.execute(
                    """
                    UPDATE admin_users SET password_hash = ?, password_salt = ?,
                        password_iterations = ?, password_changed_at = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        base64.b64encode(_password_hash(payload['password'], salt)).decode('ascii'),
                        base64.b64encode(salt).decode('ascii'),
                        PASSWORD_ITERATIONS,
                        _timestamp(_now()),
                        _timestamp(_now()),
                        user_id,
                    ),
                )
                conn.execute(
                    'UPDATE admin_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL',
                    (_timestamp(_now()), user_id),
                )
            _audit_with_connection(conn, 'ADMIN_USER_UPDATED', user['username'], {
                'actor': actor['user_id'],
                'fields': sorted(payload),
            })
        updated = conn.execute(
            'SELECT user_id, username, role, enabled, created_at, updated_at, password_changed_at FROM admin_users WHERE user_id = ?',
            (user_id,),
        ).fetchone()
        return 200, dict(updated)
    finally:
        conn.close()


def _authenticate(headers, require_csrf=False):
    raw_session = _cookie_value(headers, SESSION_COOKIE)
    if not raw_session:
        return None, None, 'session'
    session_hash = hashlib.sha256(raw_session.encode('ascii', errors='ignore')).hexdigest()
    conn = store.get_connection()
    try:
        row = conn.execute(
            """
            SELECT s.session_hash, s.csrf_hash, s.expires_at, s.revoked_at,
                   u.user_id, u.username, u.role, u.enabled
            FROM admin_sessions s JOIN admin_users u ON u.user_id = s.user_id
            WHERE s.session_hash = ?
            """,
            (session_hash,),
        ).fetchone()
        if row is None or row['revoked_at'] or not row['enabled']:
            return None, None, 'session'
        now = _timestamp(_now())
        if row['expires_at'] <= now:
            with conn:
                conn.execute(
                    'UPDATE admin_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL',
                    (now, session_hash),
                )
            return None, None, 'session'
        csrf_cookie = _cookie_value(headers, CSRF_COOKIE)
        if require_csrf:
            header_csrf = _header(headers, 'X-CSRF-Token')
            if (
                not csrf_cookie or not header_csrf
                or not hmac.compare_digest(csrf_cookie, header_csrf)
                or not hmac.compare_digest(
                    hashlib.sha256(csrf_cookie.encode('ascii', errors='ignore')).hexdigest(),
                    row['csrf_hash'],
                )
            ):
                return None, None, 'csrf'
        with conn:
            conn.execute('UPDATE admin_sessions SET last_seen_at = ? WHERE session_hash = ?', (now, session_hash))
        return {
            'user_id': row['user_id'],
            'username': row['username'],
            'role': row['role'],
        }, csrf_cookie, None
    finally:
        conn.close()


def _record_failed_login(client_ip=''):
    conn = store.get_connection()
    try:
        with conn:
            now = _now()
            for key in (_login_network_key(client_ip),):
                attempt = conn.execute(
                    'SELECT failures, window_started_at, locked_until FROM admin_login_attempts WHERE username_key = ?',
                    (key,),
                ).fetchone()
                _record_failed_login_with_connection(conn, key, now, attempt)
            _audit_with_connection(conn, 'ADMIN_LOGIN_FAILED', 'invalid_account', {})
    finally:
        conn.close()


def _record_failed_login_with_connection(conn, username, now, attempt):
    now_text = _timestamp(now)
    window_start = _timestamp(now - timedelta(minutes=LOGIN_WINDOW_MINUTES))
    if attempt and attempt['window_started_at'] >= window_start:
        failures = attempt['failures'] + 1
        locked_until = _timestamp(now + timedelta(minutes=LOGIN_LOCK_MINUTES)) if failures >= LOGIN_FAILURE_LIMIT else None
        conn.execute(
            """
            UPDATE admin_login_attempts SET failures = ?, locked_until = ?
            WHERE username_key = ?
            """,
            (failures, locked_until, username),
        )
    else:
        failures = 1
        locked_until = None
        conn.execute(
            """
            INSERT INTO admin_login_attempts(username_key, failures, window_started_at, locked_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username_key) DO UPDATE SET failures = excluded.failures,
                window_started_at = excluded.window_started_at, locked_until = excluded.locked_until
            """,
            (username, failures, now_text, locked_until),
        )


def _audit(event_type, action, payload):
    conn = store.get_connection()
    try:
        with conn:
            _audit_with_connection(conn, event_type, action, payload)
    finally:
        conn.close()


def _audit_with_connection(conn, event_type, action, payload):
    event_id = str(uuid.uuid4())
    try:
        conn.execute(
            'INSERT INTO event_log(id, event_type, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)',
            (event_id, event_type, 'ADMIN_ACTION', str(action)[:120], json.dumps(payload, separators=(',', ':'), sort_keys=True)),
        )
    except sqlite3.Error:
        # Audit persistence is part of the privileged operation's transaction; errors propagate.
        raise


def _public_user(row):
    return {
        'user_id': row['user_id'],
        'username': row['username'],
        'role': row['role'],
    }


def _header(headers, name):
    if not headers:
        return ''
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value).strip()
    return ''


def _login_network_key(client_ip):
    normalized = client_ip.strip()[:64] or 'local'
    return 'network:' + hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _cookie_value(headers, name):
    cookie_header = _header(headers, 'Cookie')
    for item in cookie_header.split(';'):
        key, separator, value = item.strip().partition('=')
        if separator and key == name:
            return value
    return ''


def _cookie_secure():
    configured = os.getenv('STORMPILOT_COOKIE_SECURE', 'true').strip().lower()
    if configured not in {'true', 'false', '1', '0'}:
        raise ValueError('STORMPILOT_COOKIE_SECURE must be true or false.')
    return configured in {'true', '1'}


def _session_cookies(session_id, csrf_token, expires):
    secure = '; Secure' if _cookie_secure() else ''
    max_age = SESSION_HOURS * 60 * 60
    return [
        ('Set-Cookie', f'{SESSION_COOKIE}={session_id}; Path=/api; Max-Age={max_age}; Expires={expires.strftime("%a, %d %b %Y %H:%M:%S GMT")}; HttpOnly; SameSite=Strict{secure}'),
        ('Set-Cookie', f'{CSRF_COOKIE}={csrf_token}; Path=/; Max-Age={max_age}; Expires={expires.strftime("%a, %d %b %Y %H:%M:%S GMT")}; SameSite=Strict{secure}'),
    ]


def _clear_session_cookies():
    secure = '; Secure' if _cookie_secure() else ''
    return [
        ('Set-Cookie', f'{SESSION_COOKIE}=; Path=/api; Max-Age=0; HttpOnly; SameSite=Strict{secure}'),
        ('Set-Cookie', f'{CSRF_COOKIE}=; Path=/; Max-Age=0; SameSite=Strict{secure}'),
    ]
