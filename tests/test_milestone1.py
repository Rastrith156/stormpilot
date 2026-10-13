import os
import shutil
import tempfile
import unittest

from app import api


class MilestoneOnePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix='stormpilot_m1_')
        self.original_data_dir = os.environ.get('APP_DATA_DIR')
        os.environ['APP_DATA_DIR'] = self.temp_dir
        api.initialize_storage(force_reset=True)

    def tearDown(self):
        if self.original_data_dir is None:
            os.environ.pop('APP_DATA_DIR', None)
        else:
            os.environ['APP_DATA_DIR'] = self.original_data_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_storage_initializes_database_and_migrations(self):
        db_path = api.get_storage_path()
        self.assertTrue(os.path.exists(db_path))
        tables = api.list_tables()
        self.assertIn('drone_registrations', tables)
        self.assertIn('telemetry_samples', tables)
        self.assertIn('deliveries', tables)
        self.assertIn('event_log', tables)
        self.assertIn('notification_outbox', tables)

    def test_delivery_state_machine_persists_transition_and_event(self):
        delivery = api.create_delivery_record(
            delivery_id='D-2001',
            drone_id='SP-DRONE-01',
            pickup={'lat': 43.6550, 'lng': -79.3800},
            destination={'lat': 43.6500, 'lng': -79.3900},
            planned_route_km=4.5,
            priority='normal',
            deadline='2026-10-13T13:00:00Z'
        )
        self.assertEqual(delivery['status'], 'CREATED')

        updated = api.update_delivery_status('D-2001', 'ASSIGNED')
        self.assertEqual(updated['status'], 'ASSIGNED')

        event = api.get_latest_event_for('D-2001')
        self.assertIsNotNone(event)
        self.assertIn('DELIVERY', event['entity_type'])

    def test_duplicate_delivery_confirmation_is_idempotent_in_storage(self):
        first = api.record_delivery_confirmation(
            delivery_id='D-2002',
            drone_id='SP-DRONE-01',
            confirmed_by='operator.demo',
            handover_confirmed=True,
        )
        second = api.record_delivery_confirmation(
            delivery_id='D-2002',
            drone_id='SP-DRONE-01',
            confirmed_by='operator.demo',
            handover_confirmed=True,
        )
        self.assertEqual(first['status'], 'confirmed')
        self.assertEqual(second['status'], 'already_confirmed')


if __name__ == '__main__':
    unittest.main()
