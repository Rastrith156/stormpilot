"""Mocked HTTP coverage for the OpenProject proxy and configuration states."""
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from app.server import Handler


class OpenProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/api/openproject'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.env = patch.dict(os.environ, {
            'OPENPROJECT_URL': 'https://projects.example.test',
            'OPENPROJECT_API_TOKEN': 'test-token-not-for-response',
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def get(self):
        try:
            with urlopen(self.url, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_unconfigured_instance_has_setup_state(self):
        with patch.dict(os.environ, {'OPENPROJECT_URL': '', 'OPENPROJECT_API_TOKEN': ''}):
            status, payload = self.get()
        self.assertEqual(status, 200)
        self.assertFalse(payload['configured'])

    def test_success_returns_normalized_projects_and_work_packages(self):
        project_payload = {'total': 1, '_embedded': {'elements': [{
            'id': 7, 'name': 'Flight Ops', 'identifier': 'flight-ops',
            'description': {'raw': 'Dispatch platform'}, 'status': {'name': 'On track'},
        }]}}
        work_payload = {'total': 1, '_embedded': {'elements': [{
            'id': 42, 'subject': 'Harden readiness checks',
            'updatedAt': '2026-10-13T08:00:00Z',
            '_links': {'type': {'title': 'Task'}, 'status': {'title': 'In progress'},
                       'priority': {'title': 'High'}, 'assignee': {'title': 'Sam'},
                       'project': {'title': 'Flight Ops'}},
        }]}}
        with patch('app.api._get', side_effect=[project_payload, work_payload]) as mocked:
            status, payload = self.get()
        self.assertEqual(status, 200)
        self.assertTrue(payload['configured'])
        self.assertEqual(payload['projects'][0]['url'], 'https://projects.example.test/projects/flight-ops')
        self.assertEqual(payload['work_packages'][0]['url'], 'https://projects.example.test/work_packages/42')
        self.assertEqual(payload['work_packages'][0]['status'], 'In progress')
        self.assertNotIn('test-token-not-for-response', json.dumps(payload))
        self.assertEqual(mocked.call_count, 2)

    def test_rejected_api_token_returns_non_sensitive_message(self):
        denied = HTTPError('https://projects.example.test/api/v3/projects', 401, 'unauthorized', {}, None)
        with patch('app.api._get', side_effect=denied):
            status, payload = self.get()
        self.assertEqual(status, 502)
        self.assertIn('rejected the API token', payload['error'])
        self.assertNotIn('test-token-not-for-response', json.dumps(payload))

    def test_unavailable_instance_returns_retryable_message(self):
        with patch('app.api._get', side_effect=URLError('connection refused')):
            status, payload = self.get()
        self.assertEqual(status, 502)
        self.assertIn('unavailable', payload['error'])

    def test_invalid_url_is_not_requested(self):
        with patch.dict(os.environ, {'OPENPROJECT_URL': 'file:///tmp'}), patch('app.api._get') as mocked:
            status, payload = self.get()
        self.assertEqual(status, 200)
        self.assertFalse(payload['configured'])
        mocked.assert_not_called()

    def test_removed_project_management_routes_are_gone(self):
        for path in ('/api/tasks', '/api/board', '/api/sprints', '/api/backlog'):
            with self.subTest(path=path):
                status, payload = self.get_path(path)
                self.assertEqual(status, 404)
                self.assertEqual(payload['error'], 'not found')

    def get_path(self, path):
        try:
            base = self.url[:self.url.index('/api/')]
            with urlopen(base + path, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())


if __name__ == '__main__':
    unittest.main()
