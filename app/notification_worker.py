"""Poll the durable notification outbox and deliver queued email."""
import logging
import os
import time

try:
    from app.api import process_notification_outbox
except ImportError:  # pragma: no cover
    from api import process_notification_outbox


logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(), format='%(asctime)s %(levelname)s %(message)s')
LOGGER = logging.getLogger('stormpilot.notification_worker')


def main():
    try:
        poll_seconds = max(1.0, float(os.getenv('NOTIFICATION_POLL_SECONDS', '5')))
    except ValueError:
        poll_seconds = 5.0

    while True:
        try:
            for result in process_notification_outbox():
                LOGGER.info('notification job %s: %s', result['id'], result['state'])
        except Exception:
            LOGGER.exception('notification outbox poll failed')
        time.sleep(poll_seconds)


if __name__ == '__main__':
    main()
