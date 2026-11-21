import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import server


MODEL = {'low': 1, 'likely': 2, 'high': 4, 'review_multiplier': 1.5}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ('projects', 'epics', 'tasks'):
            (self.root / name).mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def put(self, relative, obj):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))
        return path

    def epic(self, folder, keys, complete, issues=None, **extra):
        self.put(f'epics/{folder}/scope.json', {'epic_key': folder, 'issue_keys': keys,
                                                  'enumeration_complete': complete, **extra})
        if issues is not None:
            self.put(f'epics/{folder}/issues.json', issues)


class DashboardBuildTests(Fixture):
    def test_discovers_dynamic_projects_and_preserves_orphan_aggregate_and_task_data(self):
        self.put('projects.json', {'projects': [{'id': 'registered', 'name': 'Registered'}]})
        self.put('projects/aggregate.json', {'id': 'aggregate-orphan', 'statistics': {'total_issues': 7},
                                               'generated_at': '2026-11-01T00:00:00Z'})
        self.put('tasks/task-orphan/T-1.json', {'id': 'T-1', 'title': 'Local only'})
        self.epic('E', [], True, [])
        data = server.build_dashboard(self.root, MODEL)
        projects = {p['id']: p for p in data['projects']}
        self.assertEqual({'registered', 'aggregate-orphan', 'task-orphan'}, set(projects))
        self.assertTrue(projects['registered']['registered'])
        self.assertFalse(projects['aggregate-orphan']['registered'])
        self.assertEqual(['T-1'], projects['task-orphan']['task_ids'])
        self.assertEqual({'total_issues': 7}, data['reported_statistics'][0]['values']['statistics'])

    def test_scope_completeness_missing_records_and_empty_scope_are_unknown(self):
        self.epic('complete-empty', [], True, [])
        self.epic('incomplete', ['A-1'], False, [{'key': 'A-1'}])
        self.epic('missing', ['B-1'], True, [])
        data = server.build_dashboard(self.root, MODEL)
        self.assertIsNone(data['totals']['percent'])
        self.assertEqual(2, data['totals']['known_issues'])  # Missing declared issue remains visible as a placeholder.
        self.assertFalse(next(e for e in data['epics'] if e['key'] == 'missing')['scope_complete'])
        self.assertIn('missing issue records', ' '.join(data['warnings']))

    def test_complete_scope_progress_is_based_on_verified_issues_only(self):
        self.epic('E', ['A-1'], True, [{'key': 'A-1', 'local_status': 'verified_done',
                                        'verification': 'passed', 'evidence': ['url']}])
        data = server.build_dashboard(self.root, MODEL)
        self.assertEqual(100, data['totals']['percent'])
        self.assertTrue(data['issues'][0]['verified'])

    def test_issue_exposes_verification_time_for_the_completed_filter(self):
        self.epic('E', ['A-1'], True, [{'key': 'A-1', 'local_status': 'verified_done', 'evidence': ['url'],
                                        'verification': {'status': 'passed', 'verified_at': '2026-11-20T18:00:00Z'}}])
        self.assertEqual('2026-11-20T18:00:00Z', server.build_dashboard(self.root, MODEL)['issues'][0]['verified_at'])

    def test_verification_requires_all_linked_tasks_done_and_evidenced(self):
        self.put('tasks/p/T1.json', {'id': 'T1', 'status': 'done', 'evidence': ['proof']})
        self.put('tasks/p/T2.json', {'id': 'T2', 'status': 'done'})
        self.epic('E', ['A-1'], True, [{'key': 'A-1', 'projects': ['p'], 'task_ids': ['T1', 'T2'],
                                        'local_status': 'verified_done', 'verification_outcome': 'verified',
                                        'acceptance_criterion_evidence': ['proof']}])
        issue = server.build_dashboard(self.root, MODEL)['issues'][0]
        self.assertFalse(issue['verified'])
        self.assertEqual('awaiting_verification', issue['local_status'])

    def test_verification_accepts_a_cancelled_task_only_with_evidence(self):
        self.put('tasks/p/T1.json', {'id': 'T1', 'status': 'done', 'evidence': ['proof']})
        self.put('tasks/p/T2.json', {'id': 'T2', 'status': 'cancelled'})
        issue_record = {'key': 'A-1', 'projects': ['p'], 'task_ids': ['T1', 'T2'], 'local_status': 'verified_done',
                        'verification': {'status': 'passed'}, 'evidence': ['proof']}
        self.epic('E', ['A-1'], True, [issue_record])
        self.assertEqual('awaiting_verification', server.build_dashboard(self.root, MODEL)['issues'][0]['local_status'])
        self.put('tasks/p/T2.json', {'id': 'T2', 'status': 'cancelled', 'evidence': ['superseded by T1']})
        issue = server.build_dashboard(self.root, MODEL)['issues'][0]
        self.assertTrue(issue['verified'])
        self.assertEqual('verified_done', issue['local_status'])

    def test_duplicate_cross_project_issue_dedupes_and_allocates_estimate_equally(self):
        self.epic('E1', ['SHARED-1'], True, [{'key': 'SHARED-1', 'projects': ['a'], 'depends_on': ['EXT-1'],
                                              'codex_estimate_hours': {'low': 2, 'likely': 4, 'high': 6}}])
        self.epic('E2', ['SHARED-1'], True, [{'key': 'SHARED-1', 'projects': ['b'], 'depends_on': ['EXT-2'],
                                              'codex_estimate_hours': {'low': 2, 'likely': 4, 'high': 6}}])
        data = server.build_dashboard(self.root, MODEL)
        self.assertEqual(1, len(data['issues']))
        issue = data['issues'][0]
        self.assertEqual(['E1', 'E2'], issue['epic_keys'])
        self.assertEqual(['a', 'b'], issue['projects'])
        self.assertEqual({'low': 2.0, 'likely': 4.0, 'high': 6.0}, data['totals']['estimate'])
        self.assertEqual({'low': 1.0, 'likely': 2.0, 'high': 3.0}, next(p for p in data['projects'] if p['id'] == 'a')['allocated_estimate'])
        self.assertEqual(2, len(data['dependencies']))
        self.assertTrue(all(edge['unresolved'] for edge in data['dependencies']))

    def test_conflicting_snapshots_are_not_verified_or_measured(self):
        self.epic('E1', ['A-1'], True, [{'key': 'A-1', 'local_status': 'verified_done', 'verification': 'passed',
                                        'evidence': ['x'], 'actual_machine_hours': 3}])
        self.epic('E2', ['A-1'], True, [{'key': 'A-1', 'local_status': 'blocked', 'actual_machine_hours': 5}])
        data = server.build_dashboard(self.root, MODEL)
        issue = data['issues'][0]
        self.assertTrue(issue['conflict'])
        self.assertFalse(issue['verified'])
        self.assertIsNone(issue['actual_machine_hours'])
        self.assertIsNone(data['totals']['actual_machine_hours'])

    def test_agent_estimate_hours_is_read_and_preferred_over_the_original_codex_field(self):
        self.epic('E', ['A-1', 'A-2'], True, [
            {'key': 'A-1', 'agent_estimate_hours': {'low': 3, 'likely': 4, 'high': 5}},
            {'key': 'A-2', 'agent_estimate_hours': {'low': 1, 'likely': 2, 'high': 3},
             'codex_estimate_hours': {'low': 7, 'likely': 8, 'high': 9}}])
        data = server.build_dashboard(self.root, MODEL)
        self.assertEqual({'low': 4.0, 'likely': 6.0, 'high': 8.0}, data['totals']['estimate'])
        self.assertEqual(2, data['totals']['explicit_estimates'])

    def test_model_estimates_explicit_override_actual_null_and_partial_actuals(self):
        self.epic('E', ['A-1', 'A-2'], True, [
            {'key': 'A-1', 'codex_estimate_hours': {'low': 3, 'likely': 4, 'high': 5}, 'actual_machine_hours': 2.5},
            {'key': 'A-2'}])
        data = server.build_dashboard(self.root, MODEL)
        self.assertEqual({'low': 4.5, 'likely': 7.0, 'high': 11.0}, data['totals']['estimate'])
        self.assertEqual(2.5, data['totals']['actual_machine_hours'])
        self.assertEqual(1, data['totals']['measured_issues'])
        self.assertEqual(1, data['totals']['explicit_estimates'])

    def test_malformed_files_are_isolated_and_symlink_escape_rejected(self):
        (self.root / 'projects' / 'bad.json').write_text('{broken')
        outside = Path(self.temp.name).parent / 'outside-dashboard.json'
        outside.write_text(json.dumps({'id': 'leaked'}))
        try:
            (self.root / 'projects' / 'linked.json').symlink_to(outside)
            self.epic('E', [], True, [])
            data = server.build_dashboard(self.root, MODEL)
            self.assertTrue(any('bad.json' in w for w in data['warnings']))
            self.assertTrue(any('linked.json' in w for w in data['warnings']))
            self.assertNotIn('leaked', [p['id'] for p in data['projects']])
        finally:
            outside.unlink(missing_ok=True)

    def test_path_traversal_file_is_not_read(self):
        outside = self.root.parent / 'escape.json'
        outside.write_text('{"projects": [{"id": "escaped"}]}')
        try:
            result = server.read_json(self.root, self.root / '..' / 'escape.json', [])
            self.assertIsNone(result)
        finally:
            outside.unlink(missing_ok=True)

    def test_malformed_record_shapes_are_skipped_with_warning(self):
        self.put('projects/invalid.json', {'id': None, 'name': {'unexpected': 'object'}})
        self.epic('E', [], True, [None, {'key': ['not', 'a', 'key']}, {'key': 'GOOD-1'}])
        data = server.build_dashboard(self.root, MODEL)
        self.assertNotIn(None, [p['id'] for p in data['projects']])
        self.assertEqual(['GOOD-1'], [i['key'] for i in data['issues']])
        self.assertTrue(any('invalid keys ignored' in warning for warning in data['warnings']))

    def test_task_delivery_stage_comes_from_its_delivery_record(self):
        self.put('tasks/p/T1.json', {'id': 'T1', 'status': 'in_progress'})
        self.put('tasks/p/T2.json', {'id': 'T2', 'status': 'pending'})
        self.put('tasks/p/T3.json', {'id': 'T3', 'status': 'pending'})
        self.put('deliveries/T1.json', {'task_id': 'T1', 'delivery_stage': 'implementation'})
        self.put('deliveries/T3.json', {'task_id': 'T3', 'delivery_stage': 'shipping'})
        data = server.build_dashboard(self.root, MODEL)
        stages = {t['id']: t['delivery_stage'] for t in data['tasks']}
        self.assertEqual({'T1': 'implementation', 'T2': 'unknown', 'T3': 'unknown'}, stages)
        self.assertIn('deliveries/T3.json: unsupported or missing delivery_stage', data['warnings'])

    def test_external_dependencies_and_task_dependencies_are_retained(self):
        self.put('tasks/p/T1.json', {'id': 'T1', 'status': 'pending', 'depends_on': ['T0']})
        self.epic('E', ['A-1'], True, [{'key': 'A-1', 'projects': ['p']}],
                  external_dependencies=['OUTSIDE-1'])
        data = server.build_dashboard(self.root, MODEL)
        self.assertEqual([{'key': 'OUTSIDE-1', 'epic': 'E'}], data['external_dependencies'])
        self.assertEqual([{'from': 'T0', 'to': 'T1', 'kind': 'task_dependency', 'unresolved': True}],
                         data['task_dependencies'])

    def test_issue_snapshots_accept_list_wrapped_and_keyed_formats(self):
        self.epic('E1', ['A-1'], True, [{'key': 'A-1'}])
        self.put('epics/E2/scope.json', {'epic_key': 'E2', 'issue_keys': ['B-1'], 'enumeration_complete': True})
        self.put('epics/E2/issues.json', {'issues': [{'issue_key': 'B-1'}]})
        self.put('epics/E3/scope.json', {'epic_key': 'E3', 'issue_keys': ['C-1'], 'enumeration_complete': True})
        self.put('epics/E3/issues.json', {'issues': {'C-1': {'title': 'Keyed record'}}})
        data = server.build_dashboard(self.root, MODEL)
        self.assertEqual({'A-1', 'B-1', 'C-1'}, {issue['key'] for issue in data['issues']})
        self.assertEqual('Keyed record', next(i for i in data['issues'] if i['key'] == 'C-1')['title'])

    def test_project_statistics_estimate_actual_and_nested_project_metrics(self):
        self.put('projects/p.json', {'id': 'p', 'codex_estimate_hours': {'low': 2, 'likely': 3, 'high': 5},
                                     'actual_machine_hours': 1.25})
        self.epic('E', [], True, [])
        self.put('epics/E/progress.json', {'per_project': {'aggregate-only': {'metrics': {'total_issues': 4},
                                                                              'codex_estimate_hours': {'low': 4, 'likely': 6, 'high': 8},
                                                                              'actual_machine_hours': 2}}})
        data = server.build_dashboard(self.root, MODEL)
        projects = {p['id']: p for p in data['projects']}
        self.assertEqual({'low': 2, 'likely': 3, 'high': 5}, projects['p']['reported_estimate'])
        self.assertEqual(1.25, projects['p']['reported_actual_machine_hours'])
        self.assertEqual({'low': 4, 'likely': 6, 'high': 8}, projects['aggregate-only']['reported_estimate'])
        self.assertEqual(2, projects['aggregate-only']['reported_actual_machine_hours'])
        self.assertTrue(any(row['project'] == 'aggregate-only' for row in data['reported_statistics']))

    def test_invalid_estimate_ranges_and_actual_values_are_unknown(self):
        self.epic('E', ['A-1', 'A-2', 'A-3'], True, [
            {'key': 'A-1', 'codex_estimate_hours': {'low': -1, 'likely': 2, 'high': 3}, 'actual_machine_hours': -3},
            {'key': 'A-2', 'codex_estimate_hours': {'low': 3, 'likely': 2, 'high': 1}, 'actual_machine_hours': True},
            {'key': 'A-3', 'codex_estimate_hours': {'low': 1, 'likely': float('nan'), 'high': 4}}])
        data = server.build_dashboard(self.root, MODEL)
        self.assertTrue(all(issue['estimate'] is None for issue in data['issues']))
        self.assertTrue(all(issue['actual_machine_hours'] is None for issue in data['issues']))
        self.assertEqual(0, data['totals']['explicit_estimates'])

    def test_malformed_issue_record_prevents_false_complete_progress(self):
        self.put('epics/E/scope.json', {'epic_key': 'E', 'enumeration_complete': True})
        self.put('epics/E/issues.json', [{'key': 'A-1', 'local_status': 'verified_done',
                                         'verification': 'passed', 'evidence': ['proof']}, None])
        data = server.build_dashboard(self.root, MODEL)
        self.assertEqual(1, data['totals']['known_issues'])
        self.assertIsNone(data['totals']['percent'])
        self.assertFalse(data['epics'][0]['scope_complete'])

    def test_missing_or_unreadable_issue_snapshot_means_scope_is_incomplete(self):
        self.put('epics/missing/scope.json', {'epic_key': 'missing', 'enumeration_complete': True})
        self.put('epics/bad/scope.json', {'epic_key': 'bad', 'enumeration_complete': True})
        (self.root / 'epics' / 'bad' / 'issues.json').write_text('{truncated')
        data = server.build_dashboard(self.root, MODEL)
        self.assertTrue(all(not e['scope_complete'] for e in data['epics']))
        self.assertIsNone(data['totals']['percent'])
        self.assertTrue(any('issue snapshot; scope incomplete' in w for w in data['warnings']))

    def test_duplicate_project_membership_does_not_underallocate_issue_estimate(self):
        self.epic('E', ['A-1'], True, [{'key': 'A-1', 'projects': ['a', 'a'],
                                        'codex_estimate_hours': {'low': 2, 'likely': 4, 'high': 6}}])
        data = server.build_dashboard(self.root, MODEL)
        issue = data['issues'][0]
        project = next(p for p in data['projects'] if p['id'] == 'a')
        self.assertEqual(['a'], issue['projects'])
        self.assertEqual({'low': 2.0, 'likely': 4.0, 'high': 6.0}, project['allocated_estimate'])


class HandlerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.conn = HTTPConnection('127.0.0.1', self.httpd.server_port)

    def tearDown(self):
        self.conn.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def get(self, path):
        self.conn.request('GET', path)
        response = self.conn.getresponse()
        return response.status, response.read(), response.getheaders()

    def test_health_static_allowlist_headers_and_missing_file(self):
        with patch.object(server, 'ROOT', self.root):
            (self.root / 'static').mkdir()
            (self.root / 'static' / 'index.html').write_text('hello')
            status, body, headers = self.get('/healthz')
            self.assertEqual(200, status)
            self.assertEqual({'status': 'ok'}, json.loads(body))
            self.assertIn(('X-Content-Type-Options', 'nosniff'), headers)
            self.assertIn(('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'"), headers)
            self.assertEqual((200, b'hello'), self.get('/')[0:2])
            self.assertEqual(404, self.get('/../server.py')[0])

    def test_api_validation_and_modes(self):
        self.epic('E', [], True, [])
        with patch.object(server, 'ROOT', self.root), patch.dict(os.environ, {'MAILBOX_ROOT': str(self.root)}):
            status, body, _ = self.get('/api/dashboard?low=3&likely=2')
            self.assertEqual(400, status)
            self.assertEqual('invalid_request', json.loads(body)['error']['code'])
            self.assertEqual(400, self.get('/api/dashboard?mode=wat')[0])
            status, body, _ = self.get('/api/dashboard?mode=live')
            self.assertEqual(200, status)
            self.assertEqual('live', json.loads(body)['mode'])

    def test_mutation_methods_are_rejected(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            self.conn.request(method, '/api/dashboard')
            response = self.conn.getresponse()
            body = json.loads(response.read())
            self.assertEqual(405, response.status)
            self.assertEqual('read_only', body['error']['code'])


if __name__ == '__main__':
    unittest.main()
