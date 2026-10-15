"""Bengaluru map samples stay explicitly simulated and separate from live fleet data."""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from app import api


class BengaluruDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix='stormpilot_bengaluru_demo_')
        self.environment = patch.dict(os.environ, {'APP_DATA_DIR': self.temp_dir})
        self.environment.start()
        api.initialize_storage(force_reset=True)

    def tearDown(self):
        self.environment.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_five_sample_deliveries_include_drone_pickup_destination_and_heat(self):
        before = api.handle('GET', '/api/fleet')[1]
        status, demo = api.handle('GET', '/api/demo/bengaluru')
        after = api.handle('GET', '/api/fleet')[1]

        self.assertEqual(status, 200)
        self.assertEqual(demo['city'], 'Bengaluru')
        self.assertEqual(demo['origin'], 'SIMULATED')
        self.assertIn('not live drones', demo['warning'])
        self.assertEqual(len(demo['drones']), 5)
        self.assertEqual(len(demo['deliveries']), 5)
        self.assertEqual(len({item['drone_id'] for item in demo['drones']}), 5)
        self.assertEqual(len({item['delivery_id'] for item in demo['deliveries']}), 5)
        self.assertGreater(demo['heat_cell_size_deg'], 0)
        self.assertTrue(demo['heat_cells'])
        self.assertEqual(sum(cell['sample_count'] for cell in demo['heat_cells']), 15)

        for delivery in demo['deliveries']:
            self.assertTrue(delivery['drone_id'].startswith('BLR-DEMO-'))
            self.assertIn(delivery['status'], {'ASSIGNED', 'PICKUP_CONFIRMED', 'IN_TRANSIT', 'ARRIVED'})
            for point_name in ('position', 'pickup', 'destination'):
                point = delivery[point_name]
                self.assertGreaterEqual(point['latitude'], 12.90)
                self.assertLessEqual(point['latitude'], 13.02)
                self.assertGreaterEqual(point['longitude'], 77.55)
                self.assertLessEqual(point['longitude'], 77.65)

        self.assertEqual(
            [(drone['drone_id'], drone['position_state']) for drone in before['drones']],
            [(drone['drone_id'], drone['position_state']) for drone in after['drones']],
        )
        self.assertEqual(before['deliveries'], after['deliveries'])

    def test_dashboard_has_simulated_layer_and_no_key_openstreetmap_fallback(self):
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'app', 'static')
        with open(os.path.join(static_dir, 'index.html'), encoding='utf-8') as page:
            html = page.read()
        with open(os.path.join(static_dir, 'app.js'), encoding='utf-8') as script:
            javascript = script.read()
        self.assertIn('showBengaluruDemo', html)
        self.assertIn('SIMULATED', html)
        self.assertIn('/api/demo/bengaluru', javascript)
        self.assertIn('openstreetmap.org/export/embed.html', javascript)
        self.assertIn('updateBengaluruDemoOverlays', javascript)


if __name__ == '__main__':
    unittest.main()
