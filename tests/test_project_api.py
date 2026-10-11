"""Integration tests for the Scrum/Kanban project-management API and UI."""
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import db
from app.server import Handler


class ProjectApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix='stormpilot-pm-')
        os.environ['STORMPILOT_DB'] = os.path.join(cls._tmp, 'pm.db')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        os.environ.pop('STORMPILOT_DB', None)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # ------------------------------- helpers ----------------------------

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base + path, data=data, method=method,
                      headers={'Content-Type': 'application/json'} if data else {})
        try:
            with urlopen(req, timeout=15) as res:
                payload = res.read().decode()
                return res.status, json.loads(payload) if payload else None
        except HTTPError as err:
            payload = err.read().decode()
            return err.code, json.loads(payload) if payload else None

    def get(self, path):
        return self.request('GET', path)

    def post(self, path, body):
        return self.request('POST', path, body)

    def put(self, path, body):
        return self.request('PUT', path, body)

    def delete(self, path):
        return self.request('DELETE', path)

    def make_sprint(self, name='Sprint 1'):
        status, data = self.post('/api/sprints', {
            'name': name, 'goal': 'Ship it',
            'start_date': '2026-01-05', 'end_date': '2026-01-19',
        })
        self.assertEqual(status, 201, data)
        return data

    def make_task(self, title='Task A', **extra):
        body = {'title': title}
        body.update(extra)
        status, data = self.post('/api/tasks', body)
        self.assertEqual(status, 201, data)
        return data

    # ------------------------------- identity ---------------------------

    def test_identity_route_still_works(self):
        status, data = self.get('/')
        self.assertEqual(status, 200)
        self.assertEqual(data['project'], 'StormPilot')
        self.assertEqual(data['ui'], '/ui')

    def test_unknown_route_still_404(self):
        status, data = self.get('/missing')
        self.assertEqual(status, 404)
        self.assertEqual(data['error'], 'not found')

    def test_health_routes_unchanged(self):
        self.assertEqual(self.get('/healthz')[1], {'status': 'ok'})
        self.assertEqual(self.get('/readyz')[1], {'status': 'ok'})

    # ------------------------------- sprints ----------------------------

    def test_sprint_create_and_get(self):
        sprint = self.make_sprint('Sprint CRUD')
        status, fetched = self.get(f"/api/sprints/{sprint['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched['name'], 'Sprint CRUD')
        self.assertEqual(fetched['goal'], 'Ship it')
        self.assertEqual(fetched['start_date'], '2026-01-05')
        self.assertEqual(fetched['end_date'], '2026-01-19')
        self.assertEqual(fetched['stats']['total_tasks'], 0)
        self.assertEqual(fetched['stats']['percent'], 0.0)

    def test_sprint_update_and_notes(self):
        sprint = self.make_sprint('Notes sprint')
        status, updated = self.put(f"/api/sprints/{sprint['id']}", {
            'status': 'active',
            'review_notes': 'Demo went well',
            'retro_notes': 'Add more tests next time',
        })
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated['status'], 'active')
        self.assertEqual(updated['review_notes'], 'Demo went well')
        self.assertEqual(updated['retro_notes'], 'Add more tests next time')

    def test_sprint_delete(self):
        sprint = self.make_sprint('Doomed')
        status, data = self.delete(f"/api/sprints/{sprint['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data['deleted'], sprint['id'])
        status, err = self.get(f"/api/sprints/{sprint['id']}")
        self.assertEqual(status, 404)
        self.assertIn('error', err)

    # ----------------------------- validation ---------------------------

    def test_sprint_requires_name_and_dates(self):
        status, err = self.post('/api/sprints', {'name': '', 'start_date': '2026-02-01',
                                                 'end_date': '2026-02-14'})
        self.assertEqual(status, 400)
        self.assertIn('name', err['error'])
        status, err = self.post('/api/sprints', {'name': 'X'})
        self.assertEqual(status, 400)
        self.assertIn('start_date', err['error'])

    def test_sprint_rejects_bad_date_format(self):
        status, err = self.post('/api/sprints', {'name': 'X', 'start_date': '01/05/2026',
                                                 'end_date': '2026-01-19'})
        self.assertEqual(status, 400)
        self.assertIn('YYYY-MM-DD', err['error'])

    def test_sprint_rejects_end_before_start(self):
        status, err = self.post('/api/sprints', {'name': 'X', 'start_date': '2026-01-10',
                                                 'end_date': '2026-01-01'})
        self.assertEqual(status, 400)
        self.assertIn('end_date', err['error'])

    def test_sprint_rejects_unknown_fields(self):
        status, err = self.post('/api/sprints', {'name': 'X', 'start_date': '2026-01-01',
                                                 'end_date': '2026-01-02', 'bogus': 1})
        self.assertEqual(status, 400)
        self.assertIn('bogus', err['error'])

    def test_sprint_rejects_bad_status(self):
        sprint = self.make_sprint()
        status, err = self.put(f"/api/sprints/{sprint['id']}", {'status': 'finished'})
        self.assertEqual(status, 400)
        self.assertIn('status', err['error'])

    # ------------------------------- backlog ----------------------------

    def test_story_crud(self):
        status, story = self.post('/api/backlog', {
            'title': 'As a pilot I want routes', 'description': 'Fastest path',
            'priority': 'high', 'points': 5})
        self.assertEqual(status, 201, story)
        self.assertEqual(story['points'], 5)
        self.assertEqual(story['priority'], 'high')
        status, updated = self.put(f"/api/backlog/{story['id']}", {'points': 8})
        self.assertEqual(status, 200)
        self.assertEqual(updated['points'], 8)
        status, _ = self.delete(f"/api/backlog/{story['id']}")
        self.assertEqual(status, 200)
        status, err = self.put(f"/api/backlog/{story['id']}", {'points': 1})
        self.assertEqual(status, 404)

    def test_story_validation(self):
        status, err = self.post('/api/backlog', {'title': ''})
        self.assertEqual(status, 400)
        status, err = self.post('/api/backlog', {'title': 'ok', 'points': -1})
        self.assertEqual(status, 400)
        status, err = self.post('/api/backlog', {'title': 'ok', 'points': 1.5})
        self.assertEqual(status, 400)
        status, err = self.post('/api/backlog', {'title': 'ok', 'priority': 'urgent'})
        self.assertEqual(status, 400)

    def test_story_assign_to_sprint(self):
        sprint = self.make_sprint('Story sprint')
        status, story = self.post('/api/backlog', {
            'title': 'Linked story', 'sprint_id': sprint['id']})
        self.assertEqual(status, 201, story)
        self.assertEqual(story['sprint_id'], sprint['id'])
        status, detail = self.get(f"/api/sprints/{sprint['id']}")
        self.assertEqual([s['id'] for s in detail['stories']], [story['id']])
        status, err = self.post('/api/backlog', {'title': 'Bad link', 'sprint_id': 999999})
        self.assertEqual(status, 400)

    # -------------------------------- tasks -----------------------------

    def test_task_crud_and_defaults(self):
        task = self.make_task('Default task')
        self.assertEqual(task['status'], 'todo')
        self.assertEqual(task['priority'], 'medium')
        self.assertEqual(task['assignee'], '')
        self.assertIsNone(task['sprint_id'])
        status, updated = self.put(f"/api/tasks/{task['id']}", {
            'assignee': 'ada', 'due_date': '2026-03-01', 'priority': 'critical'})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated['assignee'], 'ada')
        self.assertEqual(updated['due_date'], '2026-03-01')
        self.assertEqual(updated['priority'], 'critical')
        status, _ = self.delete(f"/api/tasks/{task['id']}")
        self.assertEqual(status, 200)
        status, err = self.get(f"/api/tasks/{task['id']}")
        self.assertEqual(status, 404)

    # ------------------------ scrum integration -------------------------

    def test_assign_task_to_sprint_and_progress_stats(self):
        sprint = self.make_sprint('Progress sprint')
        t1 = self.make_task('P1', sprint_id=sprint['id'])
        t2 = self.make_task('P2', sprint_id=sprint['id'])
        t3 = self.make_task('P3', sprint_id=sprint['id'])
        self.request('POST', f"/api/tasks/{t1['id']}/move", {'status': 'done'})
        self.request('POST', f"/api/tasks/{t2['id']}/move", {'status': 'in_progress'})
        status, detail = self.get(f"/api/sprints/{sprint['id']}")
        self.assertEqual(status, 200)
        stats = detail['stats']
        self.assertEqual(stats['total_tasks'], 3)
        self.assertEqual(stats['completed_tasks'], 1)
        self.assertEqual(stats['by_status']['done'], 1)
        self.assertEqual(stats['by_status']['in_progress'], 1)
        self.assertEqual(stats['by_status']['todo'], 1)
        self.assertAlmostEqual(stats['percent'], 33.3, places=1)
        self.assertEqual({t['id'] for t in detail['tasks']},
                         {t1['id'], t2['id'], t3['id']})
        # Unassigning moves the task back out of the sprint.
        status, updated = self.put(f"/api/tasks/{t3['id']}", {'sprint_id': None})
        self.assertEqual(status, 200)
        self.assertIsNone(updated['sprint_id'])
        status, detail = self.get(f"/api/sprints/{sprint['id']}")
        self.assertEqual(detail['stats']['total_tasks'], 2)

    def test_delete_sprint_keeps_tasks(self):
        sprint = self.make_sprint('Cleanup sprint')
        task = self.make_task('Survivor', sprint_id=sprint['id'])
        self.request('DELETE', f"/api/sprints/{sprint['id']}")
        status, fetched = self.get(f"/api/tasks/{task['id']}")
        self.assertEqual(status, 200)
        self.assertIsNone(fetched['sprint_id'])

    def test_task_filters(self):
        sprint = self.make_sprint('Filter sprint')
        self.make_task('Filter in sprint', sprint_id=sprint['id'])
        self.make_task('Filter in testing', status='testing')
        status, data = self.get(f"/api/tasks?sprint_id={sprint['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(all(t['sprint_id'] == sprint['id'] for t in data))
        self.assertTrue(any(t['title'] == 'Filter in sprint' for t in data))
        status, data = self.get('/api/tasks?status=testing')
        self.assertEqual(status, 200)
        self.assertTrue(all(t['status'] == 'testing' for t in data))
        status, err = self.get('/api/tasks?status=bogus')
        self.assertEqual(status, 400)
        status, err = self.get('/api/tasks?sprint_id=abc')
        self.assertEqual(status, 400)

    # ------------------------- kanban + WIP limits ----------------------

    def test_board_shape(self):
        status, board = self.get('/api/board')
        self.assertEqual(status, 200)
        keys = [c['key'] for c in board['columns']]
        self.assertEqual(keys, ['todo', 'in_progress', 'testing', 'done'])
        for col in board['columns']:
            self.assertIn('limit', col)
            self.assertIn('count', col)
            self.assertIn('tasks', col)
            self.assertTrue(all(t['status'] == col['key'] for t in col['tasks']))
        self.assertIn('todo', board['wip_limits'])

    def test_move_task_between_columns(self):
        task = self.make_task('Mover', status='todo')
        status, moved = self.request('POST', f"/api/tasks/{task['id']}/move",
                                     {'status': 'testing'})
        self.assertEqual(status, 200, moved)
        self.assertEqual(moved['status'], 'testing')
        status, err = self.request('POST', f"/api/tasks/{task['id']}/move",
                                   {'status': 'archived'})
        self.assertEqual(status, 400)
        status, err = self.request('POST', f"/api/tasks/{task['id']}/move", {})
        self.assertEqual(status, 400)
        status, err = self.request('POST', '/api/tasks/999999/move', {'status': 'done'})
        self.assertEqual(status, 404)

    def test_wip_limits_crud_and_enforcement(self):
        status, limits = self.get('/api/wip-limits')
        self.assertEqual(status, 200)
        self.assertEqual(limits['in_progress'], 3)  # seeded default
        # Size the limit against current occupancy so the test does not
        # depend on how many tasks earlier tests left in this column.
        status, board = self.get('/api/board')
        occupied = next(c['count'] for c in board['columns']
                        if c['key'] == 'in_progress')
        limit = occupied + 1
        status, limits = self.put('/api/wip-limits', {'in_progress': limit, 'todo': None})
        self.assertEqual(status, 200, limits)
        self.assertEqual(limits['in_progress'], limit)
        a = self.make_task('WIP A')
        b = self.make_task('WIP B')
        status, moved = self.request('POST', f"/api/tasks/{a['id']}/move",
                                     {'status': 'in_progress'})
        self.assertEqual(status, 200, moved)
        # Column now full: second move must be rejected with 409.
        status, err = self.request('POST', f"/api/tasks/{b['id']}/move",
                                   {'status': 'in_progress'})
        self.assertEqual(status, 409)
        self.assertIn('WIP limit', err['error'])
        # Creating directly into a full column is also rejected.
        status, err = self.post('/api/tasks', {'title': 'WIP C', 'status': 'in_progress'})
        self.assertEqual(status, 409)
        # Moving the task out frees the slot again.
        status, moved = self.request('POST', f"/api/tasks/{a['id']}/move",
                                     {'status': 'done'})
        self.assertEqual(status, 200)
        status, moved = self.request('POST', f"/api/tasks/{b['id']}/move",
                                     {'status': 'in_progress'})
        self.assertEqual(status, 200, moved)
        # Restore the seeded default so other tests are unaffected.
        self.put('/api/wip-limits', {'in_progress': 3})

    def test_wip_limit_validation(self):
        status, err = self.put('/api/wip-limits', {'bogus': 2})
        self.assertEqual(status, 400)
        status, err = self.put('/api/wip-limits', {'todo': -1})
        self.assertEqual(status, 400)
        status, err = self.put('/api/wip-limits', {'todo': 'two'})
        self.assertEqual(status, 400)
        status, err = self.put('/api/wip-limits', {})
        self.assertEqual(status, 400)

    # ----------------------------- static UI ----------------------------

    def test_ui_is_served(self):
        with urlopen(self.base + '/ui', timeout=15) as res:
            self.assertEqual(res.status, 200)
            html = res.read().decode()
            self.assertIn('StormPilot', html)
            self.assertIn('view-scrum', html)
            self.assertIn('view-kanban', html)
        with urlopen(self.base + '/static/app.js', timeout=15) as res:
            self.assertEqual(res.status, 200)
        with urlopen(self.base + '/static/style.css', timeout=15) as res:
            self.assertEqual(res.status, 200)

    def test_static_path_traversal_blocked(self):
        status, _ = self.get('/static/..%2F..%2Fapp%2Fserver.py')
        self.assertEqual(status, 404)
        status, _ = self.get('/static/nonexistent.js')
        self.assertEqual(status, 404)

    # ---------------------------- persistence ---------------------------

    def test_changes_persist_across_connections(self):
        task = self.make_task('Persistent task', assignee='grace')
        # Fresh connection to the same file must see the data.
        rows = db.list_tasks()
        self.assertTrue(any(t['id'] == task['id'] and t['assignee'] == 'grace'
                            for t in rows))
        self.assertTrue(os.path.isfile(db.db_path()))
        # And a brand-new handler-based request sees it too.
        status, fetched = self.get(f"/api/tasks/{task['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched['assignee'], 'grace')


if __name__ == '__main__':
    unittest.main()
