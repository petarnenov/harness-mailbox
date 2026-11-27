import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import server


MODEL = {'low': 1, 'likely': 2, 'high': 4, 'review_multiplier': 1.5}
HUGE = 1e308
HUGE_RANGE = {'low': HUGE, 'likely': HUGE, 'high': HUGE}
SMALL_RANGE = {'low': 1, 'likely': 2, 'high': 3}
SMALL_RANGE_FLOATS = {'low': 1.0, 'likely': 2.0, 'high': 3.0}
OVERFLOW_WARNING = 'Estimate or measured-hour totals exceed the supported number range; the affected totals are unavailable'
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'"
MODEL_RANGE_MESSAGE = 'Model requires 0 ≤ low ≤ likely ≤ high ≤ 1000 and 1 ≤ review multiplier ≤ 1000'


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

    def test_issue_exposes_its_original_estimate_and_the_hours_worked_so_far(self):
        self.epic('E', ['A-1', 'A-2'], True, [{'key': 'A-1', 'estimate': {'value': 8, 'unit': 'hours'}},
                                              {'key': 'A-2', 'estimate': {'value': 3, 'unit': 'days'}}])
        self.put('jira-sync/worked-so-far.json', {'measured_at': '2026-11-22T14:00:00+00:00',
                                                  'worked_seconds': {'A-1': 5400, 'A-2': 'unknown'}})
        data = server.build_dashboard(self.root, MODEL)
        issues = {i['key']: (i['original_estimate_hours'], i['worked_hours']) for i in data['issues']}
        self.assertEqual({'A-1': (8, 1.5), 'A-2': (None, None)}, issues)
        self.assertEqual('2026-11-22T14:00:00+00:00', data['worked_measured_at'])

    def test_worked_hours_are_unknown_without_a_measurement(self):
        self.epic('E', ['A-1'], True, [{'key': 'A-1'}])
        data = server.build_dashboard(self.root, MODEL)
        self.assertIsNone(data['issues'][0]['worked_hours'])
        self.assertIsNone(data['worked_measured_at'])

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
        self.assertIn('deliveries/T3.json: unsupported or missing delivery_stage', data['delivery_warnings'])
        self.assertNotIn('deliveries/T3.json: unsupported or missing delivery_stage', data['warnings'])

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
        self.epic('E', ['A-1', 'A-2', 'A-3', 'A-4'], True, [
            {'key': 'A-1', 'codex_estimate_hours': {'low': -1, 'likely': 2, 'high': 3}, 'actual_machine_hours': -3},
            {'key': 'A-2', 'codex_estimate_hours': {'low': 3, 'likely': 2, 'high': 1}, 'actual_machine_hours': True},
            {'key': 'A-3', 'codex_estimate_hours': {'low': 1, 'likely': '2', 'high': 4}},
            {'key': 'A-4', 'codex_estimate_hours': {'low': 1, 'likely': True, 'high': 4}}])
        data = server.build_dashboard(self.root, MODEL)
        self.assertEqual(['A-1', 'A-2', 'A-3', 'A-4'], [issue['key'] for issue in data['issues']])
        self.assertEqual([], [w for w in data['warnings'] if w.startswith('epics/') or 'issue' in w])
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


class GuardedReadWarningTests(Fixture):
    def setUp(self):
        super().setUp()
        self.put('projects.json', {'projects': []})

    def test_read_failures_become_relative_warnings_with_code_written_reasons(self):
        projects = self.root / 'projects'
        (projects / 'truncated.json').write_text('{broken')
        (projects / 'binary.json').write_bytes(b'\xff\xfe{"id": 1}')
        (projects / 'unreadable.json').mkdir()
        (projects / 'huge.json').write_text(' ' * 4_000_001)
        literals = ('NaN', 'Infinity', '-Infinity', '1e400', '-1e400', '1E400')
        for index, literal in enumerate(literals):
            (projects / f'number{index}.json').write_text('{"statistics": {"total_issues": %s}}' % literal)
        expected = {'truncated': 'invalid JSON', 'binary': 'invalid JSON', 'unreadable': 'unreadable file',
                    'huge': 'exceeds 4 MB limit'}
        expected.update({f'number{index}': f'Non-finite JSON number: {literal}' for index, literal in enumerate(literals)})
        warnings = server.build_dashboard(self.root, MODEL)['warnings']
        for name, reason in expected.items():
            with self.subTest(name):
                self.assertIn(f'projects/{name}.json: {reason}', warnings)
        self.assertEqual(len(expected), len(warnings))
        self.assertNotIn(str(self.root), ' '.join(warnings))

    def test_symlinked_and_outside_root_files_are_rejected_with_the_fixed_reason(self):
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / 'outside.json'
            outside.write_text(json.dumps({'id': 'leaked'}))
            (self.root / 'projects' / 'linked.json').symlink_to(outside)
            outside_warnings = []
            self.assertIsNone(server.read_json(self.root, outside, outside_warnings))
            self.assertEqual(['outside.json: symlink/outside data root'], outside_warnings)
            data = server.build_dashboard(self.root, MODEL)
        self.assertIn('projects/linked.json: symlink/outside data root', data['warnings'])
        self.assertNotIn('leaked', [project['id'] for project in data['projects']])

    def test_integer_literal_that_overflows_a_float_is_a_non_finite_warning(self):
        literal = '1' + '0' * 400
        (self.root / 'epics' / 'E').mkdir()
        (self.root / 'epics' / 'E' / 'scope.json').write_text('{"epic_key": "E", "enumeration_complete": true}')
        (self.root / 'epics' / 'E' / 'issues.json').write_text('[{"key": "A-1", "actual_machine_hours": %s}]' % literal)
        data = server.build_dashboard(self.root, MODEL)
        self.assertIn(f'epics/E/issues.json: Non-finite JSON number: {literal}', data['warnings'])
        self.assertEqual(['E'], [epic['key'] for epic in data['epics']])
        self.assertFalse(data['epics'][0]['scope_complete'])

    def test_overflowing_integer_in_worked_seconds_is_a_delivery_warning(self):
        literal = '1' + '0' * 400
        (self.root / 'jira-sync').mkdir()
        (self.root / 'jira-sync' / 'worked-so-far.json').write_text('{"worked_seconds": {"A-1": %s}}' % literal)
        data = server.build_dashboard(self.root, MODEL)
        self.assertIn(f'jira-sync/worked-so-far.json: Non-finite JSON number: {literal}', data['delivery_warnings'])
        self.assertEqual([], [w for w in data['warnings'] if w.startswith('jira-sync/')])

    def test_read_json_reports_the_reason_for_the_path_relative_to_the_root(self):
        warnings = []
        (self.root / 'broken.json').write_text('[1,')
        self.assertIsNone(server.read_json(self.root, self.root / 'broken.json', warnings))
        self.assertIsNone(server.read_json(self.root, self.root / 'absent.json', warnings))
        self.assertEqual(['broken.json: invalid JSON', 'absent.json: unreadable file'], warnings)

    def test_well_formed_mailbox_produces_no_warning(self):
        self.put('projects.json', {'projects': [{'id': 'p', 'name': 'P'}]})
        self.put('projects/p.json', {'id': 'p', 'statistics': {'total_issues': 1}})
        self.put('tasks/p/T1.json', {'id': 'T1', 'status': 'done', 'evidence': ['proof'], 'issue_key': 'A-1',
                                     'epic_key': 'E', 'title': 'Task'})
        self.put('deliveries/T1.json', {'task_id': 'T1', 'delivery_stage': 'implementation'})
        self.put('jira-sync/worked-so-far.json', {'measured_at': '2026-11-22T14:00:00+00:00', 'worked_seconds': {'A-1': 3600}})
        self.epic('E', ['A-1'], True, [{'key': 'A-1', 'projects': ['p'], 'local_status': 'verified_done',
                                        'verification': 'passed', 'evidence': ['proof'], 'task_ids': ['T1']}])
        data = server.build_dashboard(self.root, MODEL)
        self.assertEqual([], data['warnings'])
        self.assertEqual([], data['delivery_warnings'])
        self.assertTrue(data['issues'][0]['verified'])


class ProcessDataWarningTests(Fixture):
    def test_delivery_record_read_failures_are_delivery_warnings_only(self):
        self.put('tasks/p/T1.json', {'id': 'T1'})
        self.put('tasks/p/T2.json', {'id': 'T2'})
        (self.root / 'deliveries').mkdir()
        (self.root / 'deliveries' / 'T1.json').write_text('{broken')
        self.put('deliveries/T2.json', {'delivery_stage': 'shipping'})
        data = server.build_dashboard(self.root, MODEL)
        self.assertIn('deliveries/T1.json: invalid JSON', data['delivery_warnings'])
        self.assertIn('deliveries/T1.json: unsupported or missing delivery_stage', data['delivery_warnings'])
        self.assertIn('deliveries/T2.json: unsupported or missing delivery_stage', data['delivery_warnings'])
        self.assertEqual([], [w for w in data['warnings'] if w.startswith('deliveries/')])

    def test_symlinked_delivery_record_is_a_delivery_warning(self):
        self.put('tasks/p/T1.json', {'id': 'T1'})
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / 'T1.json'
            outside.write_text(json.dumps({'delivery_stage': 'implementation'}))
            (self.root / 'deliveries').mkdir()
            (self.root / 'deliveries' / 'T1.json').symlink_to(outside)
            data = server.build_dashboard(self.root, MODEL)
        self.assertIn('deliveries/T1.json: symlink/outside data root', data['delivery_warnings'])
        self.assertEqual([], [w for w in data['warnings'] if w.startswith('deliveries/')])
        self.assertEqual('unknown', data['tasks'][0]['delivery_stage'])

    def test_unreadable_worked_so_far_snapshot_is_a_delivery_warning_only(self):
        (self.root / 'jira-sync').mkdir()
        (self.root / 'jira-sync' / 'worked-so-far.json').write_text('{"worked_seconds": {"A-1": NaN}}')
        data = server.build_dashboard(self.root, MODEL)
        self.assertIn('jira-sync/worked-so-far.json: Non-finite JSON number: NaN', data['delivery_warnings'])
        self.assertEqual([], [w for w in data['warnings'] if w.startswith('jira-sync/')])

    def test_authoritative_record_read_failures_stay_in_warnings(self):
        (self.root / 'projects' / 'bad.json').write_text('{broken')
        data = server.build_dashboard(self.root, MODEL)
        self.assertIn('projects/bad.json: invalid JSON', data['warnings'])
        self.assertNotIn('projects/bad.json: invalid JSON', data['delivery_warnings'])


class MalformedFieldTests(Fixture):
    def setUp(self):
        super().setUp()
        self.put('projects.json', {'projects': []})

    def task(self, name='T1', **fields):
        self.put(f'tasks/p/{name}.json', {'id': name, **fields})

    def test_task_issue_key_that_is_a_string_or_list_of_strings_still_links_issues(self):
        self.epic('E', ['A-1', 'A-2'], True, [{'key': 'A-1'}, {'key': 'A-2'}])
        self.task('T1', issue_key='A-1')
        self.task('T2', issue_key=['A-1', 'A-2'])
        data = server.build_dashboard(self.root, MODEL)
        tasks = {t['id']: t for t in data['tasks']}
        issues = {i['key']: i for i in data['issues']}
        self.assertEqual(['A-1'], tasks['T1']['issue_keys'])
        self.assertEqual(['A-1', 'A-2'], tasks['T2']['issue_keys'])
        self.assertEqual(['T1', 'T2'], issues['A-1']['task_ids'])
        self.assertEqual(['T2'], issues['A-2']['task_ids'])
        self.assertEqual([], [w for w in data['warnings'] if 'invalid' in w])

    def test_invalid_issue_key_is_ignored_with_a_warning_and_projected_as_null(self):
        self.epic('E', [], True, [])
        for value in (5, True, {'a': 1}):
            with self.subTest(value=value):
                self.task(issue_key=value)
                data = server.build_dashboard(self.root, MODEL)
                self.assertIn('tasks/p/T1.json: invalid issue_key ignored', data['warnings'])
                self.assertIsNone(data['tasks'][0]['issue_key'])

    def test_invalid_epic_key_is_ignored_with_a_warning_and_projected_as_null(self):
        self.epic('E', [], True, [])
        for value in (5, True, ['E'], {'a': 1}):
            with self.subTest(value=value):
                self.task(epic_key=value)
                data = server.build_dashboard(self.root, MODEL)
                self.assertIn('tasks/p/T1.json: invalid epic_key ignored', data['warnings'])
                self.assertIsNone(data['tasks'][0]['epic_key'])

    def test_absent_or_null_keys_and_string_keys_raise_no_key_warning(self):
        self.epic('E', [], True, [])
        self.task('T1')
        self.task('T2', issue_key=None, epic_key=None)
        self.task('T3', epic_key='E')
        data = server.build_dashboard(self.root, MODEL)
        self.assertEqual([], [w for w in data['warnings'] if 'ignored' in w])
        self.assertEqual('E', next(t for t in data['tasks'] if t['id'] == 'T3')['epic_key'])

    def test_non_string_task_status_is_unknown_with_a_warning_even_when_linked_to_an_issue(self):
        self.epic('E', ['A-1'], True, [{'key': 'A-1'}])
        for value in (['done'], 5, None, {'a': 1}):
            with self.subTest(value=value):
                self.task(status=value, issue_key='A-1')
                data = server.build_dashboard(self.root, MODEL)
                self.assertIn('tasks/p/T1.json: invalid task status ignored', data['warnings'])
                self.assertEqual('unknown', data['tasks'][0]['status'])
                self.assertEqual({'unknown': 1}, data['issues'][0]['task_execution']['counts'])
                self.assertFalse(data['tasks'][0]['evidenced'])

    def test_unrecognised_string_status_stays_verbatim_without_a_warning(self):
        self.task(status='weird')
        data = server.build_dashboard(self.root, MODEL)
        self.assertEqual('weird', data['tasks'][0]['status'])
        self.assertEqual([], data['warnings'])

    def test_empty_project_id_in_an_issue_is_dropped_with_the_existing_warning(self):
        self.epic('E', ['A-1'], True, [{'key': 'A-1', 'projects': ['', 'p']}])
        data = server.build_dashboard(self.root, MODEL)
        self.assertIn('Invalid project identifier ignored', data['warnings'])
        self.assertEqual(['p'], data['issues'][0]['projects'])
        self.assertEqual(['p'], [project['id'] for project in data['projects']])

    def test_non_object_scope_is_reported_and_leaves_the_epic_incomplete(self):
        for scope in ([], None, 'text', 5):
            with self.subTest(scope=scope):
                self.put('epics/E/scope.json', scope)
                self.put('epics/E/issues.json', [])
                data = server.build_dashboard(self.root, MODEL)
                self.assertIn('epics/E/scope.json: scope is not a JSON object', data['warnings'])
                self.assertEqual(['E'], [e['key'] for e in data['epics']])
                self.assertFalse(data['epics'][0]['scope_complete'])
                self.assertIsNone(data['totals']['percent'])

    def test_unreadable_scope_gives_only_its_read_warning_and_stays_incomplete(self):
        scope = self.root / 'epics' / 'E' / 'scope.json'
        scope.parent.mkdir(parents=True)
        self.put('epics/E/issues.json', [])
        with tempfile.TemporaryDirectory() as outside_dir:
            target = Path(outside_dir) / 'scope.json'
            target.write_text('{"epic_key": "E"}')
            cases = {'symlink': ('symlink/outside data root', lambda: scope.symlink_to(target)),
                     'over 4 MB': ('exceeds 4 MB limit', lambda: scope.write_text(' ' * 4_000_001)),
                     'invalid JSON': ('invalid JSON', lambda: scope.write_text('{broken'))}
            for name, (reason, create) in cases.items():
                with self.subTest(name):
                    scope.unlink(missing_ok=True)
                    create()
                    data = server.build_dashboard(self.root, MODEL)
                    self.assertIn(f'epics/E/scope.json: {reason}', data['warnings'])
                    self.assertNotIn('epics/E/scope.json: scope is not a JSON object', data['warnings'])
                    self.assertFalse(data['epics'][0]['scope_complete'])

    def test_task_issue_key_list_without_a_resolved_issue_is_projected_unmapped_or_unresolved(self):
        self.epic('E', [], True, [])
        self.task('T1', issue_key=[])
        self.task('T2', issue_key=['X-9'])
        data = server.build_dashboard(self.root, MODEL)
        tasks = {t['id']: t for t in data['tasks']}
        self.assertEqual('unmapped', tasks['T1']['mapping_kind'])
        self.assertEqual('unresolved', tasks['T2']['mapping_kind'])
        self.assertIn('T2: unresolved issue mapping X-9', data['warnings'])

    def test_object_scope_raises_no_scope_warning(self):
        self.epic('E', [], True, [])
        self.assertEqual([], server.build_dashboard(self.root, MODEL)['warnings'])


class HandlerTests(Fixture):
    def setUp(self):
        super().setUp()
        quiet_log = patch.object(server.Handler, 'log_message')
        quiet_log.start()
        self.addCleanup(quiet_log.stop)
        self.httpd =server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
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

    def test_health_and_static_allowlist_carry_security_headers(self):
        with patch.object(server, 'ROOT', self.root):
            (self.root / 'static').mkdir()
            (self.root / 'static' / 'index.html').write_text('hello')
            status, body, headers = self.get('/healthz')
            self.assertEqual(200, status)
            self.assertEqual({'status': 'ok'}, json.loads(body))
            self.assertIn(('X-Content-Type-Options', 'nosniff'), headers)
            self.assertIn(('Content-Security-Policy', CSP), headers)
            self.assertEqual((200, b'hello'), self.get('/')[0:2])
            self.assertEqual(404, self.get('/../server.py')[0])

    def test_path_outside_the_static_allowlist_is_a_not_found_response_that_is_not_cached(self):
        with patch.object(server, 'ROOT', self.root):
            status, body, headers = self.get('/missing-file.js')
        self.assertEqual(404, status)
        self.assertEqual({'error': {'code': 'not_found', 'message': 'Not found'}}, json.loads(body))
        self.assertIn(('Cache-Control', 'no-store'), headers)
        self.assertIn(('X-Content-Type-Options', 'nosniff'), headers)

    def test_overflowing_integer_literal_in_a_record_is_served_with_a_warning(self):
        literal = '1' + '0' * 400
        (self.root / 'epics' / 'E').mkdir()
        (self.root / 'epics' / 'E' / 'scope.json').write_text('{"epic_key": "E"}')
        (self.root / 'epics' / 'E' / 'issues.json').write_text('[{"key": "A-1", "actual_machine_hours": %s}]' % literal)
        with patch.dict(os.environ, {'MAILBOX_ROOT': str(self.root)}):
            status, body, _ = self.get('/api/dashboard')
        self.assertEqual(200, status)
        self.assertIn(f'epics/E/issues.json: Non-finite JSON number: {literal}', json.loads(body)['warnings'])

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

    def test_requests_never_create_or_change_a_file_under_the_data_root(self):
        self.epic('E', ['A-1'], True, [{'key': 'A-1'}])
        self.put('tasks/p/T1.json', {'id': 'T1'})
        before = self.tree_state()
        with patch.dict(os.environ, {'MAILBOX_ROOT': str(self.root)}):
            self.get('/api/dashboard')
            self.get('/healthz')
            for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                self.conn.request(method, '/api/dashboard', body=b'{"x": 1}')
                self.conn.getresponse().read()
        self.assertEqual(before, self.tree_state())

    def test_unparsable_or_out_of_range_model_values_are_the_model_range_error(self):
        with patch.dict(os.environ, {'MAILBOX_ROOT': str(self.root)}):
            for query in ('low=abc', 'likely=x', 'high=1e999', 'low=nan', 'review_multiplier=0.5', 'low=3&likely=2'):
                with self.subTest(query=query):
                    status, body, _ = self.get('/api/dashboard?' + query)
                    self.assertEqual(400, status)
                    self.assertEqual({'error': {'code': 'invalid_request', 'message': MODEL_RANGE_MESSAGE}}, json.loads(body))

    def served_dashboard(self):
        with patch.dict(os.environ, {'MAILBOX_ROOT': str(self.root)}):
            status, body, _ = self.get('/api/dashboard')
        self.assertEqual(200, status)
        return json.loads(body)

    def huge_issue(self, key, **extra):
        return {'key': key, 'agent_estimate_hours': HUGE_RANGE, **extra}

    def verified_huge_issue(self, key, **extra):
        return self.huge_issue(key, local_status='verified_done', verification='passed', evidence=['x'], **extra)

    def assertOverflowWarningOnce(self, data):
        self.assertEqual([OVERFLOW_WARNING], [w for w in data['delivery_warnings'] if w == OVERFLOW_WARNING])
        self.assertNotIn(OVERFLOW_WARNING, data['warnings'])

    def assertNoOverflowWarning(self, data):
        self.assertNotIn(OVERFLOW_WARNING, data['delivery_warnings'])
        self.assertNotIn(OVERFLOW_WARNING, data['warnings'])

    def test_estimates_summing_beyond_the_float_range_are_served_as_null_with_one_delivery_warning(self):
        self.epic('E', ['A-1', 'A-2'], True, [self.huge_issue('A-1'), self.huge_issue('A-2')])
        data = self.served_dashboard()
        self.assertIsNone(data['totals']['estimate'])
        self.assertOverflowWarningOnce(data)

    def test_measured_hours_summing_beyond_the_float_range_leave_the_estimates_as_numbers(self):
        self.epic('E', ['A-1', 'A-2'], True, [
            {'key': 'A-1', 'agent_estimate_hours': SMALL_RANGE, 'actual_machine_hours': HUGE},
            {'key': 'A-2', 'agent_estimate_hours': SMALL_RANGE, 'actual_machine_hours': HUGE}])
        data = self.served_dashboard()
        totals = data['totals']
        self.assertIsNone(totals['actual_machine_hours'])
        self.assertEqual(2, totals['measured_issues'])
        self.assertEqual({'low': 2.0, 'likely': 4.0, 'high': 6.0}, totals['estimate'])
        self.assertEqual({'low': 2.0, 'likely': 4.0, 'high': 6.0}, totals['remaining_estimate'])
        self.assertOverflowWarningOnce(data)

    def test_overflowing_estimate_is_null_while_the_remaining_estimate_stays_finite(self):
        self.epic('E', ['A-1', 'A-2', 'A-3'], True, [
            self.verified_huge_issue('A-1'), self.verified_huge_issue('A-2'),
            {'key': 'A-3', 'agent_estimate_hours': SMALL_RANGE}])
        data = self.served_dashboard()
        self.assertIsNone(data['totals']['estimate'])
        self.assertEqual(SMALL_RANGE_FLOATS, data['totals']['remaining_estimate'])
        self.assertOverflowWarningOnce(data)

    def test_project_progress_estimate_is_null_while_its_allocated_estimate_stays_finite(self):
        self.epic('E', ['A-1', 'A-2'], True, [self.huge_issue('A-1', projects=['a', 'b']),
                                              self.huge_issue('A-2', projects=['a', 'b'])])
        data = self.served_dashboard()
        project = next(p for p in data['projects'] if p['id'] == 'a')
        self.assertIsNone(project['progress']['estimate'])
        self.assertEqual(HUGE_RANGE, project['allocated_estimate'])
        self.assertOverflowWarningOnce(data)

    def test_project_allocation_beyond_the_float_range_is_null(self):
        self.epic('E', ['A-1', 'A-2'], True, [self.huge_issue('A-1', projects=['a']), self.huge_issue('A-2', projects=['a'])])
        self.epic('F', ['B-1'], True, [{'key': 'B-1', 'agent_estimate_hours': SMALL_RANGE, 'projects': ['b']}])
        data = self.served_dashboard()
        projects = {p['id']: p for p in data['projects']}
        self.assertIsNone(projects['a']['allocated_estimate'])
        self.assertEqual(SMALL_RANGE_FLOATS, projects['b']['allocated_estimate'])
        self.assertOverflowWarningOnce(data)

    def test_a_range_is_null_as_a_whole_when_only_one_of_its_keys_overflows(self):
        high_only = {'low': 1, 'likely': 2, 'high': HUGE}
        self.epic('E', ['A-1', 'A-2'], True, [{'key': 'A-1', 'agent_estimate_hours': high_only},
                                              {'key': 'A-2', 'agent_estimate_hours': high_only}])
        data = self.served_dashboard()
        self.assertIsNone(data['totals']['estimate'])
        self.assertIsNone(data['totals']['remaining_estimate'])
        self.assertOverflowWarningOnce(data)

    def test_epic_progress_nulls_only_the_epic_whose_sums_overflow(self):
        self.epic('E1', ['A-1', 'A-2'], True, [self.huge_issue('A-1'), self.huge_issue('A-2')])
        self.epic('E2', ['B-1'], True, [{'key': 'B-1', 'agent_estimate_hours': SMALL_RANGE, 'actual_machine_hours': 1}])
        data = self.served_dashboard()
        epics = {e['key']: e['progress'] for e in data['epics']}
        self.assertIsNone(epics['E1']['estimate'])
        self.assertIsNone(epics['E1']['remaining_estimate'])
        self.assertEqual(SMALL_RANGE_FLOATS, epics['E2']['estimate'])
        self.assertEqual(1.0, epics['E2']['actual_machine_hours'])
        self.assertOverflowWarningOnce(data)

    def test_the_warning_appears_once_when_totals_epics_and_projects_all_overflow(self):
        self.epic('E1', ['A-1', 'A-2'], True, [self.huge_issue('A-1', projects=['a'], actual_machine_hours=HUGE),
                                               self.huge_issue('A-2', projects=['a'], actual_machine_hours=HUGE)])
        self.epic('E2', ['B-1', 'B-2'], True, [self.huge_issue('B-1', projects=['b']), self.huge_issue('B-2', projects=['b'])])
        self.assertOverflowWarningOnce(self.served_dashboard())

    def test_counts_beside_null_totals_are_unchanged(self):
        self.epic('E', ['A-1', 'A-2'], True, [self.verified_huge_issue('A-1', actual_machine_hours=HUGE),
                                              self.huge_issue('A-2', actual_machine_hours=HUGE)])
        totals = self.served_dashboard()['totals']
        self.assertEqual(2, totals['known_issues'])
        self.assertEqual(1, totals['verified_issues'])
        self.assertEqual(2, totals['explicit_estimates'])
        self.assertEqual(2, totals['measured_issues'])
        self.assertEqual('Complete enumerated issue scope', totals['estimate_basis'])
        self.assertEqual(50.0, totals['percent'])
        self.assertEqual({'verified_done': 1, 'pending': 1}, totals['status_counts'])

    def test_whole_number_measured_hours_summing_beyond_the_float_range_are_served_as_null(self):
        whole_number_hours = 10 ** 308
        self.epic('E', ['A-1', 'A-2'], True, [{'key': 'A-1', 'actual_machine_hours': whole_number_hours},
                                              {'key': 'A-2', 'actual_machine_hours': whole_number_hours}])
        self.assertIn('1' + '0' * 308, (self.root / 'epics' / 'E' / 'issues.json').read_text())
        data = self.served_dashboard()
        self.assertIsNone(data['totals']['actual_machine_hours'])
        self.assertEqual(2, data['totals']['measured_issues'])
        self.assertOverflowWarningOnce(data)

    def test_decimal_measured_hours_beside_overflowing_whole_numbers_are_null_in_any_order(self):
        hours = [10 ** 308, 10 ** 308, 1.5]
        for decimal_position in range(len(hours)):
            arrangement = hours[:]
            arrangement.insert(decimal_position, arrangement.pop())
            with self.subTest(decimal_position=decimal_position):
                keys = [f'A-{n}' for n in range(len(arrangement))]
                self.epic('E', keys, True, [{'key': key, 'actual_machine_hours': value}
                                            for key, value in zip(keys, arrangement)])
                data = self.served_dashboard()
                self.assertIsNone(data['totals']['actual_machine_hours'])
                self.assertEqual(3, data['totals']['measured_issues'])
                self.assertOverflowWarningOnce(data)

    def test_whole_number_and_decimal_measured_hours_still_sum_to_their_finite_total(self):
        self.epic('E', ['A-1', 'A-2'], True, [{'key': 'A-1', 'actual_machine_hours': 3},
                                              {'key': 'A-2', 'actual_machine_hours': 1.5}])
        data = self.served_dashboard()
        self.assertEqual(4.5, data['totals']['actual_machine_hours'])
        self.assertNoOverflowWarning(data)

    def test_small_whole_number_measured_hours_are_summed_without_the_overflow_warning(self):
        self.epic('E', ['A-1', 'A-2'], True, [{'key': 'A-1', 'actual_machine_hours': 3},
                                              {'key': 'A-2', 'actual_machine_hours': 5}])
        data = self.served_dashboard()
        self.assertEqual(8, data['totals']['actual_machine_hours'])
        self.assertNoOverflowWarning(data)

    def test_finite_sums_are_served_unchanged_without_the_overflow_warning(self):
        self.epic('E', ['A-1', 'A-2'], True, [
            {'key': 'A-1', 'agent_estimate_hours': SMALL_RANGE, 'actual_machine_hours': 1.5},
            {'key': 'A-2', 'agent_estimate_hours': SMALL_RANGE, 'actual_machine_hours': 2}])
        data = self.served_dashboard()
        self.assertEqual({'low': 2.0, 'likely': 4.0, 'high': 6.0}, data['totals']['estimate'])
        self.assertEqual(3.5, data['totals']['actual_machine_hours'])
        self.assertNoOverflowWarning(data)

    def test_empty_selection_and_missing_measurement_are_null_without_the_overflow_warning(self):
        self.epic('E', [], True, [])
        self.epic('F', ['A-1'], True, [{'key': 'A-1', 'agent_estimate_hours': SMALL_RANGE}])
        data = self.served_dashboard()
        empty = next(e for e in data['epics'] if e['key'] == 'E')['progress']
        self.assertIsNone(empty['estimate'])
        self.assertIsNone(empty['actual_machine_hours'])
        self.assertIsNone(data['totals']['actual_machine_hours'])
        self.assertNoOverflowWarning(data)

    def test_no_issues_at_all_gives_null_totals_without_the_overflow_warning(self):
        data = self.served_dashboard()
        self.assertIsNone(data['totals']['estimate'])
        self.assertIsNone(data['totals']['actual_machine_hours'])
        self.assertNoOverflowWarning(data)

    def test_unknown_mode_is_an_invalid_request(self):
        status, body, _ = self.get('/api/dashboard?mode=wat')
        self.assertEqual(400, status)
        self.assertEqual({'error': {'code': 'invalid_request', 'message': 'Unknown mode'}}, json.loads(body))

    def test_unexpected_projection_failure_is_a_generic_server_error_without_detail(self):
        for failure in (ValueError('secret /data/epics detail'), TypeError('unhashable secret'), KeyError('secret')):
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(server, 'build_dashboard', side_effect=failure):
                status, body, headers = self.get('/api/dashboard')
                self.assertEqual(500, status)
                self.assertEqual({'error': {'code': 'internal_error', 'message': 'Dashboard data could not be read'}}, json.loads(body))
                self.assertNotIn(b'secret', body)
                self.assertIn(('Cache-Control', 'no-store'), headers)

    def test_overflowing_number_literal_in_a_record_is_a_warning_not_a_failed_snapshot(self):
        (self.root / 'projects' / 'big.json').write_text('{"statistics": {"total_issues": 1e400}}')
        with patch.dict(os.environ, {'MAILBOX_ROOT': str(self.root)}):
            status, body, _ = self.get('/api/dashboard')
        self.assertEqual(200, status)
        self.assertIn('projects/big.json: Non-finite JSON number: 1e400', json.loads(body)['warnings'])

    def test_library_generated_errors_use_the_structured_body_and_security_headers(self):
        cases = {
            'HEAD': (b'HEAD / HTTP/1.1\r\nHost: x\r\n\r\n', 501, 'unsupported_method', 'Unsupported method'),
            'OPTIONS': (b'OPTIONS / HTTP/1.1\r\nHost: x\r\n\r\n', 501, 'unsupported_method', 'Unsupported method'),
            'malformed request line': (b'GARBAGE\r\n\r\n', 400, 'bad_request', 'Bad request'),
            'over-long request line': (b'GET /' + b'a' * 70000 + b' HTTP/1.1\r\n\r\n', 414, 'bad_request', 'Bad request'),
            'over-long header line': (b'GET / HTTP/1.1\r\nX: ' + b'a' * 70000 + b'\r\n\r\n', 431, 'bad_request', 'Bad request'),
            'unsupported protocol version': (b'GET / HTTP/9.9\r\n\r\n', 505, 'internal_error', 'Request failed'),
        }
        for name, (request, status, code, message) in cases.items():
            with self.subTest(name):
                response = self.raw_exchange(request)
                self.assertEqual(status, response['status'])
                self.assertEqual('application/json; charset=utf-8', response['headers']['content-type'])
                self.assertEqual('no-store', response['headers']['cache-control'])
                self.assertEqual('nosniff', response['headers']['x-content-type-options'])
                self.assertEqual(CSP, response['headers']['content-security-policy'])
                if request.startswith(b'HEAD'):
                    self.assertEqual(b'', response['body'])
                else:
                    self.assertEqual({'error': {'code': code, 'message': message}}, json.loads(response['body']))
                    self.assertEqual(len(response['body']), int(response['headers']['content-length']))

    def tree_state(self):
        return {str(path.relative_to(self.root)): (path.stat().st_mtime_ns, path.stat().st_size) if path.is_file() else None
                for path in sorted(self.root.rglob('*'))}

    def raw_exchange(self, request):
        with socket.create_connection(('127.0.0.1', self.httpd.server_port), timeout=5) as connection:
            connection.sendall(request)
            received = b''
            while chunk := connection.recv(65536):
                received += chunk
        head, _, body = received.partition(b'\r\n\r\n')
        status_line, *header_lines = head.decode('latin-1').split('\r\n')
        headers = {name.lower(): value.strip() for name, _, value in (line.partition(':') for line in header_lines)}
        return {'status': int(status_line.split()[1]), 'headers': headers, 'body': body}


class ContextApiTests(Fixture):
    RAW_FAILURE = "[Errno 13] Permission denied: '/data/decisions/T1/g1.json'"
    NODE_MESSAGE = 'Query parameter node must be task:<id> or issue:<key>'

    def setUp(self):
        super().setUp()
        quiet_log = patch.object(server.Handler, 'log_message')
        quiet_log.start()
        self.addCleanup(quiet_log.stop)
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.conn = HTTPConnection('127.0.0.1', self.httpd.server_port)
        environment = patch.dict(os.environ, {'MAILBOX_ROOT': str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.put('tasks/p/T1.json', {'id': 'T1', 'project': 'p', 'status': 'in_progress', 'depends_on': ['GHOST']})
        self.put('decisions/T1/g1.json', {'decision_type': 'g1', 'verdict': 'approved', 'rationale': 'Scope accepted'})

    def tearDown(self):
        self.conn.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def get(self, path):
        self.conn.request('GET', path)
        response = self.conn.getresponse()
        return response.status, response.read(), dict(response.getheaders())

    def tree_state(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in sorted(self.root.rglob('*')) if path.is_file()}

    def test_a_known_node_is_served_with_its_pack_the_mode_and_the_security_headers(self):
        status, body, headers = self.get('/api/context?node=task:T1')
        pack = json.loads(body)
        self.assertEqual(200, status)
        self.assertEqual(('live', 'task:T1'), (pack['mode'], pack['node']['id']))
        self.assertEqual(['decision:T1/g1'], [decision['id'] for decision in pack['decisions']])
        self.assertEqual('application/json; charset=utf-8', headers['Content-Type'])
        self.assertEqual('no-store', headers['Cache-Control'])
        self.assertEqual('nosniff', headers['X-Content-Type-Options'])
        self.assertEqual(CSP, headers['Content-Security-Policy'])

    def test_a_graph_placeholder_is_served_with_its_unresolved_status(self):
        status, body, _ = self.get('/api/context?node=task:GHOST')
        self.assertEqual((200, 'unresolved'), (status, json.loads(body)['node']['status']))

    def test_an_unknown_node_is_not_found_with_the_frozen_body(self):
        status, body, _ = self.get('/api/context?node=task:nothing')
        self.assertEqual((404, {'error': {'code': 'not_found', 'message': 'Context node not found'}}), (status, json.loads(body)))

    def test_a_missing_repeated_or_malformed_node_is_an_invalid_request_with_the_frozen_message(self):
        cases = ('/api/context', '/api/context?node=', '/api/context?node=task:T1&node=task:T1', '/api/context?node=T1',
                 '/api/context?node=epic:E', '/api/context?node=task:', '/api/context?node=task:-x', '/api/context?node=task:a/b',
                 '/api/context?node=task:' + 'a' * 129, '/api/context?node=task:T1%0A')
        for path in cases:
            with self.subTest(path=path):
                status, body, _ = self.get(path)
                self.assertEqual((400, {'error': {'code': 'invalid_request', 'message': self.NODE_MESSAGE}}), (status, json.loads(body)))

    def test_a_node_of_the_longest_allowed_identifier_is_accepted(self):
        self.assertEqual(404, self.get('/api/context?node=issue:' + 'a' * 128)[0])

    def test_an_unknown_mode_is_rejected_with_the_message_of_the_dashboard_route(self):
        status, body, _ = self.get('/api/context?node=task:T1&mode=wat')
        _, dashboard_body, _ = self.get('/api/dashboard?mode=wat')
        self.assertEqual(400, status)
        self.assertEqual(json.loads(dashboard_body)['error'], json.loads(body)['error'])

    def test_demo_mode_reads_the_demo_directory_and_not_the_mailbox_root(self):
        demo = Path(self.temp.name) / 'checkout'
        (demo / 'demo' / 'tasks' / 'p').mkdir(parents=True)
        (demo / 'demo' / 'tasks' / 'p' / 'D1.json').write_text(json.dumps({'id': 'D1', 'project': 'p'}))
        with patch.object(server, 'ROOT', demo):
            demo_status, demo_body, _ = self.get('/api/context?node=task:D1&mode=demo')
            live_status, _, _ = self.get('/api/context?node=task:D1&mode=live')
        self.assertEqual((200, 'demo', 404), (demo_status, json.loads(demo_body)['mode'], live_status))

    def test_mutating_methods_are_rejected_as_read_only(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            with self.subTest(method=method):
                self.conn.request(method, '/api/context?node=task:T1', body=b'{"x": 1}')
                response = self.conn.getresponse()
                self.assertEqual((405, 'read_only'), (response.status, json.loads(response.read())['error']['code']))

    def test_requests_leave_the_mailbox_tree_byte_identical(self):
        before = self.tree_state()
        for path in ('/api/context?node=task:T1', '/api/context?node=task:nothing', '/api/context?node=bad'):
            self.get(path)
        self.conn.request('POST', '/api/context?node=task:T1', body=b'{}')
        self.conn.getresponse().read()
        self.assertEqual(before, self.tree_state())

    def test_an_unexpected_failure_is_the_fixed_message_without_the_raw_text(self):
        with patch.object(server, 'read_json', side_effect=OSError(self.RAW_FAILURE)):
            status, body, _ = self.get('/api/context?node=task:T1')
        self.assertEqual((500, {'error': {'code': 'internal_error', 'message': 'Context data could not be read'}}), (status, json.loads(body)))
        for raw in ('Permission denied', '/data/decisions', 'Errno', 'Traceback'):
            self.assertNotIn(raw, body.decode())

    def test_a_failure_while_building_the_pack_is_the_fixed_message_without_the_raw_text(self):
        failure = RuntimeError('Traceback (most recent call last): File "/data/server.py", line 1')
        with patch.object(server, 'context_for', side_effect=failure):
            status, body, _ = self.get('/api/context?node=task:T1')
        self.assertEqual((500, 'Context data could not be read'), (status, json.loads(body)['error']['message']))
        self.assertNotIn('Traceback', body.decode())
        self.assertNotIn('/data/server.py', body.decode())

    def test_a_pack_that_cannot_be_serialised_without_non_finite_numbers_is_the_fixed_failure(self):
        with patch.object(server, 'context_for', return_value={'bad': float('nan')}):
            status, body, _ = self.get('/api/context?node=task:T1')
        self.assertEqual((500, 'internal_error'), (status, json.loads(body)['error']['code']))


class ContextCompositionTests(Fixture):
    def test_context_for_returns_the_pack_of_a_known_node_and_none_for_an_unknown_one(self):
        self.put('tasks/p/T1.json', {'id': 'T1', 'project': 'p'})
        self.assertEqual('task:T1', server.context_for(self.root, 'task:T1')['node']['id'])
        self.assertIsNone(server.context_for(self.root, 'task:nothing'))

    def test_parse_node_returns_the_single_valid_value_and_refuses_anything_else(self):
        self.assertEqual('issue:A-1', server.parse_node({'node': ['issue:A-1']}))
        for query in ({}, {'node': []}, {'node': ['task:a', 'task:b']}, {'node': ['nope']}, {'node': ['task:a b']}):
            with self.subTest(query=query):
                with self.assertRaises(server.InvalidRequest) as refusal:
                    server.parse_node(query)
                self.assertEqual('Query parameter node must be task:<id> or issue:<key>', str(refusal.exception))

    def test_mode_root_is_the_demo_directory_for_demo_and_the_mailbox_root_otherwise(self):
        with patch.dict(os.environ, {'MAILBOX_ROOT': '/mailbox'}), patch.object(server, 'ROOT', Path('/checkout')):
            self.assertEqual((Path('/checkout/demo'), Path('/mailbox')), (server.mode_root('demo'), server.mode_root('live')))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Path('/data'), server.mode_root('live'))

    def test_importing_server_does_not_import_the_context_graph(self):
        checkout = Path(server.__file__).resolve().parent
        probe = 'import sys, server, dispatch_gate; sys.exit(1 if "context_graph" in sys.modules else 0)'
        result = subprocess.run([sys.executable, '-B', '-c', probe], cwd=checkout, capture_output=True, text=True)
        self.assertEqual((0, ''), (result.returncode, result.stderr))

    def test_the_dashboard_is_the_same_with_and_without_the_records_only_the_context_graph_reads(self):
        self.put('tasks/p/T1.json', {'id': 'T1', 'project': 'p', 'owner_session_id': 's-1', 'g1_decision': 'decisions/T1/g1.json'})
        without = server.build_dashboard(self.root)
        self.put('decisions/T1/g1.json', {'verdict': 'approved'})
        self.put('outbox/p/m.json', {'id': 'm', 'type': 'review_request', 'task_id': 'T1'})
        self.put('reports/p/r.json', {'task_id': 'T1'})
        self.put('dispatch-gates/T1/rc.json', {'task_id': 'T1', 'operation': 'dispatch', 'allowed': True})
        self.put('sessions/s-1.json', {'session_id': 's-1'})
        with_records = server.build_dashboard(self.root)
        without.pop('generated_at'), with_records.pop('generated_at')
        self.assertEqual(without, with_records)


if __name__ == '__main__':
    unittest.main()
