"""Persistent Agile board API and authorization tests."""
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import api
from app import db as store
from app.server import Handler
from support import create_authenticated_user


class AgilePlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix='stormpilot_agile_')
        self.environment = patch.dict(os.environ, {
            'APP_DATA_DIR': self.temp_dir,
            'STORMPILOT_COOKIE_SECURE': 'false',
        })
        self.environment.start()
        api.initialize_storage(force_reset=True)
        self.admin = create_authenticated_user(username='agile.admin')
        self.operator = create_authenticated_user(role='OPERATOR', username='agile.operator')
        self.viewer = create_authenticated_user(role='VIEWER', username='agile.viewer')

    def tearDown(self):
        self.environment.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def request(self, method, path, payload=None, headers=None):
        request = Request(
            f'{self.base_url}{path}',
            data=json.dumps(payload).encode('utf-8') if payload is not None else None,
            method=method,
        )
        request.add_header('Content-Type', 'application/json')
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except HTTPError as error:
            try:
                raw = error.read()
            finally:
                error.close()
            return error.code, json.loads(raw) if raw else {}

    def create_sprint(self, headers, name='Sprint One'):
        return self.request('POST', '/api/agile/sprints', {'name': name}, headers)

    def create_task(self, headers, **overrides):
        payload = {
            'title': 'Improve delivery handoff',
            'description': 'Record the handoff workflow.',
            'priority': 'HIGH',
            'story_points': 5,
        }
        payload.update(overrides)
        return self.request('POST', '/api/agile/tasks', payload, headers)

    def test_sprints_are_persistent_validated_and_have_one_active_sprint(self):
        status, first = self.create_sprint(self.admin)
        self.assertEqual(status, 201, first)
        status, second = self.create_sprint(self.operator, 'Sprint Two')
        self.assertEqual(status, 201, second)
        self.assertEqual(first['state'], 'ACTIVE')
        self.assertEqual(second['state'], 'PLANNED')

        status, error = self.create_sprint(self.admin, 'Sprint Two')
        self.assertEqual(status, 409)
        self.assertIn('already exists', error['error'])
        status, error = self.request(
            'POST', '/api/agile/sprints',
            {'name': 'Bad dates', 'starts_on': '2026-10-21', 'ends_on': '2026-10-20'},
            self.admin,
        )
        self.assertEqual(status, 400)
        self.assertIn('on or after', error['error'])

        status, board = self.request('GET', '/api/agile', headers=self.viewer)
        self.assertEqual(status, 200)
        self.assertEqual([sprint['state'] for sprint in board['sprints']], ['ACTIVE', 'PLANNED'])
        self.assertEqual(board['counts']['TODO'], 0)
        self.assertEqual(len(board['users']), 3)

    def test_tasks_validate_assignees_and_sprints_and_can_be_updated(self):
        _, sprint = self.create_sprint(self.admin)
        status, result = self.create_task(
            self.operator,
            sprint_id=sprint['sprint_id'],
            assignee_id=next(user['user_id'] for user in self._board()['users'] if user['username'] == 'agile.viewer'),
        )
        self.assertEqual(status, 201, result)
        task_id = result['task_id']

        status, board = self.request('GET', '/api/agile', headers=self.viewer)
        self.assertEqual(status, 200)
        task = board['tasks'][0]
        self.assertEqual(task['task_id'], task_id)
        self.assertEqual(task['assignee'], 'agile.viewer')
        self.assertEqual(task['sprint_name'], 'Sprint One')
        self.assertEqual(board['counts']['TODO'], 1)

        status, error = self.create_task(self.operator, title='   ')
        self.assertEqual(status, 400)
        self.assertIn('Title must', error['error'])
        status, error = self.create_task(self.operator, story_points=True)
        self.assertEqual(status, 400)
        self.assertIn('whole number', error['error'])
        status, error = self.create_task(self.operator, sprint_id='missing-sprint')
        self.assertEqual(status, 400)

        status, result = self.request('PUT', f'/api/agile/tasks/{task_id}', {
            'title': 'Improve delivery handoff',
            'description': 'Document the updated workflow.',
            'status': 'IN_PROGRESS',
            'priority': 'URGENT',
            'story_points': 8,
            'assignee_id': None,
            'sprint_id': None,
        }, self.operator)
        self.assertEqual(status, 200, result)
        status, board = self.request('GET', '/api/agile', headers=self.viewer)
        self.assertEqual(status, 200)
        self.assertEqual(board['tasks'][0]['status'], 'IN_PROGRESS')
        self.assertEqual(board['tasks'][0]['priority'], 'URGENT')
        self.assertIsNone(board['tasks'][0]['sprint_id'])
        self.assertEqual(board['counts']['IN_PROGRESS'], 1)

    def test_sprint_lifecycle_requires_completion_before_another_sprint_starts(self):
        _, active = self.create_sprint(self.admin, 'Active sprint')
        _, planned = self.create_sprint(self.operator, 'Next sprint')

        status, error = self.request(
            'PUT', f"/api/agile/sprints/{planned['sprint_id']}", {'action': 'start'}, self.operator,
        )
        self.assertEqual(status, 409)
        self.assertIn('Complete the active sprint', error['error'])

        status, result = self.request(
            'PUT', f"/api/agile/sprints/{active['sprint_id']}", {'action': 'complete'}, self.operator,
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result['state'], 'COMPLETED')
        status, result = self.request(
            'PUT', f"/api/agile/sprints/{planned['sprint_id']}", {'action': 'start'}, self.operator,
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result['state'], 'ACTIVE')

        status, error = self.request(
            'PUT', f"/api/agile/sprints/{active['sprint_id']}", {'action': 'start'}, self.admin,
        )
        self.assertEqual(status, 409)
        status, error = self.request(
            'PUT', f"/api/agile/sprints/{planned['sprint_id']}", {'action': 'pause'}, self.admin,
        )
        self.assertEqual(status, 400)

    def test_roles_protect_mutations_and_admin_can_delete(self):
        status, _ = self.request('GET', '/api/agile')
        self.assertEqual(status, 401)
        status, error = self.create_sprint(self.viewer, 'Viewer sprint')
        self.assertEqual(status, 403)
        self.assertIn('role', error['error'].lower())
        status, error = self.create_task(self.viewer)
        self.assertEqual(status, 403)

        status, task = self.create_task(self.operator)
        self.assertEqual(status, 201, task)
        status, error = self.request(
            'DELETE', f"/api/agile/tasks/{task['task_id']}", headers=self.operator,
        )
        self.assertEqual(status, 403)
        status, result = self.request(
            'DELETE', f"/api/agile/tasks/{task['task_id']}", headers=self.admin,
        )
        self.assertEqual(status, 200, result)
        self.assertTrue(result['deleted'])
        status, error = self.request(
            'DELETE', '/api/agile/tasks/not-a-task', headers=self.admin,
        )
        self.assertEqual(status, 404)
        self.assertIn('not found', error['error'].lower())

    def _board(self):
        status, board = self.request('GET', '/api/agile', headers=self.admin)
        self.assertEqual(status, 200, board)
        return board


if __name__ == '__main__':
    unittest.main()
