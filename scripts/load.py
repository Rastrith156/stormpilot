"""Bounded loopback-only load test; never target public services."""
import argparse
import concurrent.futures
import time
from urllib.request import urlopen

p = argparse.ArgumentParser()
p.add_argument('--seconds', type=int, default=30, choices=range(1, 121))
p.add_argument('--workers', type=int, default=4, choices=range(1, 17))
p.add_argument('--port', type=int, default=8080, choices=[8080, 8081])
a = p.parse_args()
end = time.monotonic() + a.seconds

def worker(_):
    ok = failed = 0
    while time.monotonic() < end:
        try:
            with urlopen(f'http://127.0.0.1:{a.port}/dispatch', timeout=3) as response:
                response.read()
                ok += 1
        except Exception:
            failed += 1
        time.sleep(0.02)
    return ok, failed

with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
    results = list(pool.map(worker, range(a.workers)))
print({'successful': sum(x[0] for x in results), 'failed': sum(x[1] for x in results)})
