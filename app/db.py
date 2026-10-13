import json
import os
import sqlite3
import uuid
from pathlib import Path


_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / '.stormpilot_data'


def get_data_dir():
    configured = os.getenv('APP_DATA_DIR', '').strip()
    if configured:
        return Path(configured).expanduser()
    return _DEFAULT_DATA_DIR


def get_storage_path():
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / 'stormpilot.db')


def get_connection():
    conn = sqlite3.connect(get_storage_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL')
    return conn


def list_tables():
    conn = get_connection()
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [row['name'] for row in rows]
    finally:
        conn.close()


def initialize_storage(force_reset=False):
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = get_storage_path()
    if force_reset and os.path.exists(db_path):
        os.remove(db_path)

    conn = get_connection()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            )
        ''')

        migrations = [
            (
                1,
                '001_initial_schema',
                '''
                CREATE TABLE IF NOT EXISTS drone_registrations (
                    drone_id TEXT PRIMARY KEY,
                    device_id TEXT,
                    source TEXT NOT NULL DEFAULT 'SIMULATED',
                    latitude REAL,
                    longitude REAL,
                    gps_accuracy REAL,
                    last_update TEXT,
                    status TEXT NOT NULL DEFAULT 'registered',
                    mission_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                );

                CREATE TABLE IF NOT EXISTS telemetry_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drone_id TEXT NOT NULL,
                    device_id TEXT,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    accuracy REAL,
                    device_timestamp TEXT NOT NULL,
                    server_received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    source TEXT NOT NULL DEFAULT 'PHONE_GPS',
                    status TEXT NOT NULL,
                    quality_flags TEXT,
                    rejection_reason TEXT,
                    distance_m REAL,
                    accepted INTEGER NOT NULL DEFAULT 0,
                    checksum TEXT,
                    FOREIGN KEY(drone_id) REFERENCES drone_registrations(drone_id)
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    drone_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'CREATED',
                    pickup_lat REAL,
                    pickup_lng REAL,
                    destination_lat REAL,
                    destination_lng REAL,
                    planned_route_km REAL,
                    actual_observed_km REAL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    arrival_detected_at TEXT,
                    delivered_at TEXT,
                    handover_confirmed INTEGER NOT NULL DEFAULT 0,
                    handover_confirmed_by TEXT,
                    notification_status TEXT NOT NULL DEFAULT 'PENDING',
                    arrival_evidence TEXT,
                    current_evidence TEXT,
                    UNIQUE(drone_id, delivery_id)
                );

                CREATE TABLE IF NOT EXISTS delivery_state_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL,
                    previous_status TEXT,
                    new_status TEXT NOT NULL,
                    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    reason TEXT,
                    evidence TEXT,
                    FOREIGN KEY(delivery_id) REFERENCES deliveries(delivery_id)
                );

                CREATE TABLE IF NOT EXISTS event_log (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    payload TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id TEXT PRIMARY KEY,
                    notification_type TEXT NOT NULL,
                    entity_id TEXT,
                    delivery_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    sent_at TEXT,
                    payload TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS device_registrations (
                    device_id TEXT PRIMARY KEY,
                    drone_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    source TEXT NOT NULL DEFAULT 'PHONE_GPS',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    revocation_reason TEXT,
                    last_seen TEXT,
                    expires_at TEXT,
                    FOREIGN KEY(drone_id) REFERENCES drone_registrations(drone_id)
                );

                CREATE TABLE IF NOT EXISTS device_credentials (
                    device_id TEXT PRIMARY KEY,
                    drone_id TEXT NOT NULL,
                    secret_hash TEXT NOT NULL,
                    secret_salt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    revoked_reason TEXT,
                    expires_at TEXT,
                    last_seen TEXT,
                    FOREIGN KEY(drone_id) REFERENCES drone_registrations(drone_id)
                );

                CREATE INDEX IF NOT EXISTS idx_telemetry_drone_ts ON telemetry_samples(drone_id, device_timestamp);
                CREATE INDEX IF NOT EXISTS idx_telemetry_status ON telemetry_samples(status);
                CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);
                CREATE INDEX IF NOT EXISTS idx_event_log_entity ON event_log(entity_type, entity_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_outbox_state ON notification_outbox(state, next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_device_credentials_status ON device_credentials(status, drone_id);
                ''',
            ),
            (
                2,
                '002_device_identity',
                '''
                CREATE TABLE IF NOT EXISTS device_registrations (
                    device_id TEXT PRIMARY KEY,
                    drone_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    source TEXT NOT NULL DEFAULT 'PHONE_GPS',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    revocation_reason TEXT,
                    last_seen TEXT,
                    expires_at TEXT,
                    FOREIGN KEY(drone_id) REFERENCES drone_registrations(drone_id)
                );

                CREATE TABLE IF NOT EXISTS device_credentials (
                    device_id TEXT PRIMARY KEY,
                    drone_id TEXT NOT NULL,
                    secret_hash TEXT NOT NULL,
                    secret_salt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    revoked_reason TEXT,
                    expires_at TEXT,
                    last_seen TEXT,
                    FOREIGN KEY(drone_id) REFERENCES drone_registrations(drone_id)
                );

                CREATE INDEX IF NOT EXISTS idx_device_credentials_status ON device_credentials(status, drone_id);
                '''
            ),
            (
                3,
                '003_digital_twin_configuration',
                '''
                CREATE TABLE IF NOT EXISTS digital_twin_config (
                    config_id INTEGER PRIMARY KEY CHECK (config_id = 1),
                    config_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                );
                '''
            ),
            (
                4,
                '004_admin_identity_sessions_and_simulation',
                '''
                CREATE TABLE IF NOT EXISTS admin_users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    password_iterations INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('ADMIN', 'OPERATOR', 'VIEWER')),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    password_changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                );

                CREATE TABLE IF NOT EXISTS admin_sessions (
                    session_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    csrf_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    last_seen_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES admin_users(user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_admin_sessions_user ON admin_sessions(user_id, expires_at);

                CREATE TABLE IF NOT EXISTS admin_login_attempts (
                    username_key TEXT PRIMARY KEY,
                    failures INTEGER NOT NULL DEFAULT 0,
                    window_started_at TEXT NOT NULL,
                    locked_until TEXT
                );

                CREATE TABLE IF NOT EXISTS simulation_runs (
                    scenario_id TEXT PRIMARY KEY,
                    scenario_type TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    baseline_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'SIMULATED' CHECK (origin = 'SIMULATED'),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    applied_at TEXT,
                    FOREIGN KEY(created_by) REFERENCES admin_users(user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_simulation_runs_created ON simulation_runs(created_at);
                '''
            )
        ]

        for version, name, ddl in migrations:
            already_applied = conn.execute('SELECT 1 FROM schema_migrations WHERE name = ?', (name,)).fetchone()
            if already_applied:
                continue
            try:
                conn.executescript(ddl)
            except sqlite3.DatabaseError:
                conn.rollback()
                raise
            conn.execute(
                'INSERT INTO schema_migrations(version, name) VALUES (?, ?)',
                (version, name),
            )

        conn.commit()
    finally:
        conn.close()


def record_event(event_type, entity_type, entity_id, payload=None):
    payload = payload or {}
    event_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute(
            'INSERT INTO event_log(id, event_type, entity_type, entity_id, payload) VALUES (?, ?, ?, ?, ?)',
            (
                event_id,
                event_type,
                entity_type,
                str(entity_id),
                json.dumps(payload, separators=(',', ':'), sort_keys=True),
            ),
        )
        conn.commit()
        return {'id': event_id, 'event_type': event_type, 'entity_type': entity_type, 'entity_id': str(entity_id), 'payload': payload}
    finally:
        conn.close()


def get_latest_event_for(entity_id):
    conn = get_connection()
    try:
        row = conn.execute(
            'SELECT * FROM event_log WHERE entity_id = ? ORDER BY timestamp DESC LIMIT 1',
            (str(entity_id),),
        ).fetchone()
        if row is None:
            return None
        return {
            'id': row['id'],
            'event_type': row['event_type'],
            'entity_type': row['entity_type'],
            'entity_id': row['entity_id'],
            'timestamp': row['timestamp'],
            'payload': json.loads(row['payload']) if row['payload'] else {},
        }
    finally:
        conn.close()


def prune_expired_telemetry(retention_days=30):
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM telemetry_samples WHERE status = 'ACCEPTED' AND device_timestamp < datetime('now', ?)",
            (f'-{retention_days} days',),
        )
        conn.commit()
    finally:
        conn.close()
