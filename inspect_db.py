import sqlite3
c = sqlite3.connect('.stormpilot_data/stormpilot.db')
print('TABLES:')
for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    print(' ', r[0])
print('MIGRATIONS:')
for r in c.execute('SELECT * FROM schema_migrations ORDER BY version'):
    print(' ', r)
print('DRONE COLUMNS:')
for r in c.execute('PRAGMA table_info(drone_registrations)'):
    print(' ', r)
print('DRONES:')
for r in c.execute('SELECT drone_id, device_id, source, status, latitude, longitude FROM drone_registrations'):
    print(' ', r)
c.close()
