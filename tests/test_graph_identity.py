"""Local execution records retain their identity separately from Jira scope."""
import server
from test_server import Fixture


class DuplicateTaskIdentityTests(Fixture):
    DUPLICATE_WARNING = 'T1: duplicate task identity in tasks/a/T1.json, tasks/b/T1.json; completion evidence ignored'

    def verified_issue_fixture(self, first, second):
        self.epic('E', ['A-1'], True, [{'key': 'A-1', 'projects': ['a'], 'task_ids': ['T1'],
                                        'local_status': 'verified_done', 'verification': 'passed',
                                        'evidence': ['proof']}])
        self.put('tasks/a/T1.json', {'id': 'T1', **first})
        self.put('tasks/b/T1.json', {'id': 'T1', **second})

    def test_duplicate_identity_ignores_completion_evidence_whichever_record_is_first(self):
        evidenced = {'status': 'done', 'evidence': ['proof']}
        pending = {'status': 'pending'}
        for name, first, second in (('evidenced first', evidenced, pending), ('evidenced last', pending, evidenced),
                                    ('both evidenced', evidenced, evidenced)):
            with self.subTest(name):
                self.verified_issue_fixture(first, second)
                data = server.build_dashboard(self.root)
                self.assertIn(self.DUPLICATE_WARNING, data['warnings'])
                self.assertEqual([False, False], [t['evidenced'] for t in data['tasks']])
                issue = data['issues'][0]
                self.assertFalse(issue['verified'])
                self.assertEqual('awaiting_verification', issue['local_status'])
                node = next(n for n in data['graph']['nodes'] if n['id'] == 'task:T1')
                self.assertFalse(node['ready'])

    def test_unique_task_identity_keeps_its_completion_evidence(self):
        self.verified_issue_fixture({'status': 'done', 'evidence': ['proof']}, {'id': 'T2', 'status': 'pending'})
        data = server.build_dashboard(self.root)
        self.assertEqual([], [w for w in data['warnings'] if 'duplicate' in w])
        self.assertEqual([True, False], [t['evidenced'] for t in data['tasks']])
        self.assertTrue(data['issues'][0]['verified'])


class GraphIdentityTests(Fixture):
    def test_local_task_identity_and_scope_are_not_jira_identity(self):
        self.epic('E', ['A', 'B'], True, [
            {'key': 'A', 'issue_type': 'Story', 'source_status': 'In Progress'},
            {'key': 'B', 'issue_type': 'Development Sub-Task', 'parent_key': 'A'}])
        self.put('epics/E/scope.json', {'epic_key': 'E', 'issue_keys': ['A', 'B'],
                                      'source_issue_type': 'Feature', 'source_status': 'In Progress'})
        self.put('tasks/p/T1.json', {'id': 'T1', 'title': 'E local design', 'issue_key': 'E'})
        self.put('tasks/p/T2.json', {'id': 'T2', 'title': 'A local implementation', 'issue_key': 'A'})
        data = server.build_dashboard(self.root)
        nodes = {node['id']: node for node in data['graph']['nodes']}
        self.assertEqual('Feature', nodes['epic:E']['issue_type'])
        self.assertEqual('In Progress', nodes['epic:E']['source_status'])
        self.assertEqual('Story', nodes['issue:A']['issue_type'])
        self.assertEqual('In Progress', nodes['issue:A']['source_status'])
        self.assertEqual('Development Sub-Task', nodes['issue:B']['issue_type'])
        self.assertEqual('epic', nodes['task:T1']['mapping_kind'])
        self.assertEqual([], nodes['task:T1']['issue_keys'])
        self.assertEqual(['E'], nodes['task:T1']['epic_keys'])
        self.assertEqual('issue', nodes['task:T2']['mapping_kind'])
        self.assertEqual(['A'], nodes['task:T2']['issue_keys'])
        self.assertEqual('E local design', nodes['task:T1']['label'])
        self.assertEqual({'A', 'B'}, {issue['key'] for issue in data['issues']})
