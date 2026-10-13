"""Bounded HTTP smoke checks against a local deployment."""
import argparse
import json
import time
from urllib.error import URLError
from urllib.request import urlopen

p = argparse.ArgumentParser()
p.add_argument('--port', type=int, default=8080, choices=[8080, 8081, 8082])
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
workspace = json.loads(fetch('/api/openproject'))
assert 'configured' in workspace
print('PASS: readiness, identity, health, dispatch, metrics and OpenProject workspace')
