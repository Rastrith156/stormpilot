import os, shutil
os.environ['APP_DATA_DIR'] = './.test_migrate_data'
if os.path.exists('.test_migrate_data'):
    shutil.rmtree('.test_migrate_data')
from app import db
db.initialize_storage()
conn = db.get_connection()
print('TABLES:', [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")])
print('MIGRATIONS:', [r for r in conn.execute('SELECT version, name FROM schema_migrations ORDER BY version')])
print('DRONES:', [r for r in conn.execute('SELECT drone_id, status, source FROM drone_registrations ORDER BY drone_id')])
conn.close()
shutil.rmtree('.test_migrate_data')
print('MIGRATION TEST PASSED')
