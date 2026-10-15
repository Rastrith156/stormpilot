"""Guard production gateway and container data-directory requirements."""
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ProductionDeploymentConfigTests(unittest.TestCase):
    def test_public_gateway_uses_trusted_tls_and_allows_required_map_and_gps_features(self):
        with open(os.path.join(ROOT, 'Caddyfile'), encoding='utf-8') as handle:
            caddyfile = handle.read()

        self.assertNotIn('tls internal', caddyfile)
        self.assertIn('geolocation=(self)', caddyfile)
        self.assertIn('https://maps.googleapis.com', caddyfile)
        self.assertIn('https://www.openstreetmap.org', caddyfile)
        self.assertIn('frame-ancestors \'none\'', caddyfile)
        self.assertIn('reverse_proxy oauth2-proxy:4180', caddyfile)

    def test_non_root_container_owns_its_persistent_data_mount_directory(self):
        with open(os.path.join(ROOT, 'Dockerfile'), encoding='utf-8') as handle:
            dockerfile = handle.read()

        self.assertIn('chown 10001:10001 /data', dockerfile)
        self.assertLess(dockerfile.index('chown 10001:10001 /data'), dockerfile.index('USER 10001:10001'))

    def test_kubernetes_smoke_manifest_has_one_writable_database_instance(self):
        with open(os.path.join(ROOT, 'k8s', 'base.yaml'), encoding='utf-8') as handle:
            manifest = handle.read()

        self.assertIn('replicas: 1', manifest)
        self.assertIn('type: Recreate', manifest)
        self.assertIn('name: APP_DATA_DIR\n              value: /data', manifest)
        self.assertIn('fsGroup: 10001', manifest)
        self.assertIn('emptyDir: {}', manifest)

    def test_compose_starts_database_migrator_before_shared_outbox_worker(self):
        with open(os.path.join(ROOT, 'compose.yaml'), encoding='utf-8') as handle:
            compose = handle.read()

        self.assertIn(
            'notification-worker:\n'
            '    build: .\n'
            '    command: ["python", "-m", "app.notification_worker"]\n'
            '    depends_on:\n'
            '      api:\n'
            '        condition: service_healthy',
            compose,
        )


if __name__ == '__main__':
    unittest.main()
