"""Bounded HTTP smoke checks against a local deployment."""
import argparse
import json
import time
from urllib.error import URLError
from urllib.request import urlopen

p = argparse.ArgumentParser()
p.add_argument('--port', type=int, default=8080, choices=[8080, 8081, 8082, 8083])
a = p.parse_args()
base = f'http://127.0.0.1:{a.port}'

def fetch(path):
    with urlopen(base + path, timeout=3) as res:
        assert res.status == 200, (path, res.status)
        return res.read().decode()

for attempt in range(30):
    try:
        assert json.loads(fetch('/readyz'))['status'] == 'ok'
        break
    except (URLError, TimeoutError):
        if attempt == 29:
            raise
        time.sleep(1)
assert json.loads(fetch('/'))['project'] == 'StormPilot'
assert json.loads(fetch('/healthz'))['status'] == 'ok'
assert json.loads(fetch('/dispatch'))['status'] == 'dispatched'
assert 'stormpilot_requests_total' in fetch('/metrics')
assert 'id="map"' in fetch('/ui')
assert 'id="adminToken"' in fetch('/drone/connect')
config = json.loads(fetch('/api/config'))
assert isinstance(config.get('map_ready'), bool)
fleet = json.loads(fetch('/api/fleet'))
assert len(fleet['drones']) == 4
assert all(drone['position_state'] in ('PHONE_GPS', 'STALE', 'OFFLINE', 'SIMULATED') for drone in fleet['drones'])
traffic = json.loads(fetch('/api/traffic?window_hours=1&cell_size_deg=0.01'))
assert traffic['window_hours'] == 1 and isinstance(traffic['cells'], list)
twin = json.loads(fetch('/api/twin'))
assert twin['origin'] == 'simulated' and twin['scenarios']
workspace = json.loads(fetch('/api/redmine'))
assert 'configured' in workspace
print('PASS: readiness, identity, health, UI, config, four-drone fleet, traffic, twin, dispatch, metrics and Redmine')
