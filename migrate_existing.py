import os
os.environ['APP_DATA_DIR'] = './.stormpilot_data'
from app import db
db.initialize_storage()
conn = db.get_connection()
print('MIGRATIONS:', [r['name'] for r in conn.execute('SELECT name FROM schema_migrations ORDER BY version')])
print('DRONES:', [(r['drone_id'], r['status']) for r in conn.execute('SELECT drone_id, status FROM drone_registrations ORDER BY drone_id')])
conn.close()
print('EXISTING DB MIGRATED')
