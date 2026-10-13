"""Mocked HTTP coverage for the Redmine proxy and configuration states."""
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from app.server import Handler


class RedmineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/api/redmine'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.env = patch.dict(os.environ, {
            'REDMINE_URL': 'https://issues.example.test',
            'REDMINE_API_KEY': 'test-key-not-for-response',
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def request(self, path=None):
        try:
            with urlopen(path or self.url, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_unconfigured_instance_has_setup_state(self):
        with patch.dict(os.environ, {'REDMINE_URL': '', 'REDMINE_API_KEY': ''}):
            status, payload = self.request()
        self.assertEqual(status, 200)
        self.assertFalse(payload['configured'])

    def test_success_returns_normalized_projects_and_issues(self):
        projects = {'total_count': 1, 'projects': [{
            'id': 7, 'name': 'Flight Ops', 'identifier': 'flight-ops',
            'description': 'Dispatch platform',
        }]}
        issues = {'total_count': 1, 'issues': [{
            'id': 42, 'subject': 'Harden readiness checks', 'updated_on': '2026-10-13T08:00:00Z',
            'tracker': {'name': 'Task'}, 'status': {'name': 'In Progress'},
            'priority': {'name': 'High'}, 'assigned_to': {'name': 'Sam'},
            'project': {'name': 'Flight Ops'},
        }]}
        with patch('app.api._get', side_effect=[projects, issues]) as mocked:
            status, payload = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(payload['configured'])
        self.assertEqual(payload['projects'][0]['url'], 'https://issues.example.test/projects/flight-ops')
        self.assertEqual(payload['issues'][0]['url'], 'https://issues.example.test/issues/42')
        self.assertEqual(payload['issues'][0]['status'], 'In Progress')
        self.assertEqual(payload['counts']['issues'], 1)
        self.assertNotIn('test-key-not-for-response', json.dumps(payload))
        self.assertEqual(mocked.call_count, 2)

    def test_upstream_requests_send_api_key_in_header(self):
        from app import api

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"projects": []}'

        class Opener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response()

        opener = Opener()
        with patch('app.api.build_opener', return_value=opener):
            result = api._get('https://issues.example.test', 'private-key', '/projects.json')
        request = opener.request
        self.assertEqual(request.get_header('X-redmine-api-key'), 'private-key')
        self.assertNotIn('private-key', request.full_url)
        self.assertEqual(result, {'projects': []})

    def test_rejected_api_key_returns_non_sensitive_message(self):
        denied = HTTPError('https://issues.example.test/projects.json', 401, 'unauthorized', {}, None)
        with patch('app.api._get', side_effect=denied):
            status, payload = self.request()
        self.assertEqual(status, 502)
        self.assertIn('rejected the API key', payload['error'])
        self.assertNotIn('test-key-not-for-response', json.dumps(payload))

    def test_unavailable_instance_returns_retryable_message(self):
        with patch('app.api._get', side_effect=URLError('connection refused')):
            status, payload = self.request()
        self.assertEqual(status, 502)
        self.assertIn('unavailable', payload['error'])

    def test_invalid_url_is_not_requested(self):
        with patch.dict(os.environ, {'REDMINE_URL': 'file:///tmp'}), patch('app.api._get') as mocked:
            status, payload = self.request()
        self.assertEqual(status, 200)
        self.assertFalse(payload['configured'])
        mocked.assert_not_called()

    def test_removed_project_management_routes_are_gone(self):
        base = self.url[:self.url.index('/api/')]
        for path in ('/api/tasks', '/api/board', '/api/sprints', '/api/backlog'):
            with self.subTest(path=path):
                status, payload = self.request(base + path)
                self.assertEqual(status, 404)
                self.assertEqual(payload['error'], 'not found')


if __name__ == '__main__':
    unittest.main()
