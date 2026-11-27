import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import context_graph
import server

REPORT_ID = 'report-one'
RAW_FAILURE = "[Errno 13] Permission denied: '/data/decisions/T1/g1.json'"


def build_pack(root, node_id):
    return context_graph.build_context_pack(server.build_dashboard(root), root, server.read_json, node_id)


def build_graph(root):
    warnings = []
    graph = context_graph.build_context_graph(server.build_dashboard(root), root, server.read_json, warnings)
    return graph, warnings


def node_of(graph, node_id):
    return next(node for node in graph['nodes'] if node['id'] == node_id)


def edges_of(graph, kind):
    return {(edge['from'], edge['to']): edge for edge in graph['edges'] if edge['kind'] == kind}


def pack_item(items, item_id):
    return next(item for item in items if item['id'] == item_id)


class MailboxCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def put(self, relative, record):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record if isinstance(record, str) else json.dumps(record))
        return path

    def task(self, task_id, **fields):
        self.put(f'tasks/p/{task_id}.json', {'id': task_id, 'project': 'p', 'status': 'in_progress', **fields})

    def epic(self, issues):
        self.put('epics/E/scope.json', {'epic_key': 'E', 'issue_keys': [i['key'] for i in issues], 'enumeration_complete': True})
        self.put('epics/E/issues.json', issues)

    def tree_snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in sorted(self.root.rglob('*')) if path.is_file()}


class DeliveryCase(MailboxCase):
    """T1 (alice, session s-1) belongs to issue A-1; it depends on the open T0 and A-1 on EXT-1 and the open A-0 (task T9)."""

    def setUp(self):
        super().setUp()
        self.put('projects.json', {'projects': [{'id': 'p', 'name': 'P'}]})
        self.epic([{'key': 'A-1', 'task_ids': ['T1'], 'depends_on': ['EXT-1', 'A-0']}, {'key': 'A-0', 'task_ids': ['T9']}])
        self.task('T0', status='pending')
        self.task('T1', issue_key='A-1', depends_on=['T0'], owner='alice', owner_session_id='s-1', head_sha='abc1234',
                  merge_sha='def5678', evidence=['reports/p/r1.json', 'free text'], g1_decision='decisions/T1/g1.json')
        self.task('T9', owner='bob', owner_session_id='s-2')
        self.put('sessions/s-1.json', {'session_id': 's-1', 'project': 'p', 'role': 'worker', 'host': 'h', 'status': 'active',
                                       'last_seen_at': '2026-11-02T08:00:00+00:00'})
        self.put('sessions/s-2.json', {'project': 'q', 'last_seen_at': '2026-11-02 08:00:00'})
        self.put('reports/p/r1.json', {'id': REPORT_ID, 'task_id': 'T1', 'kind': 'review', 'created_at': '2026-11-03T09:00:00+00:00',
                                       'head_sha': 'abc1234'})
        self.put('reports/p/r2.json', {'id': 'report-two', 'issue_key': 'A-1', 'type': 'summary', 'supersedes_report': REPORT_ID})
        self.put('dispatch-gates/T1/rc1.json', {'task_id': 'T1', 'operation': 'dispatch', 'allowed': True, 'created_at': '2026-11-04T09:00:00+00:00'})
        self.put('dispatch-gates/T1/rc2.json', {'task_id': 'T1', 'operation': 'dispatch', 'allowed': False})
        self.put('decisions/T1/g1.json', {'decision_type': 'g1', 'verdict': 'approved', 'decided_at': '2026-11-02T10:00:00+00:00',
                                          'rationale': 'Scope accepted', 'conditions': ['tests first', 7], 'task_id': 'T1', 'request_id': 'req-1'})
        self.put('decisions/T1/g2.json', {'type': 'g2', 'result': 'rejected', 'created_at': '2026-11-05T10:00:00+00:00',
                                          'supersedes': 'decisions/T1/g1.json'})
        self.put('outbox/p/req-1.json', {'id': 'req-1', 'type': 'review_request', 'task_id': 'T1', 'subject': 'Review T1',
                                         'from': {'project': 'p'}, 'to': 'q', 'created_at': '2026-11-02T09:00:00+00:00',
                                         'body': 'SECRET-BODY-TEXT', 'token': 'SECRET-TOKEN'})
        self.put('inbox/q/reply-1.json', {'id': 'reply-1', 'type': 'review_reply', 'in_reply_to': 'req-1', 'task_id': 'T1'})


class NodeDerivationTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.graph, self.warnings = build_graph(self.root)

    def test_decision_node_takes_its_fields_from_the_record_and_its_path(self):
        node = node_of(self.graph, 'decision:T1/g1')
        self.assertEqual(('decisions/T1/g1.json', 'g1', 'approved', '2026-11-02T10:00:00+00:00', 'Scope accepted', ['tests first']),
                         (node['source'], node['decision_type'], node['verdict'], node['decided_at'], node['rationale'], node['conditions']))

    def test_decision_node_falls_back_to_type_result_and_created_at(self):
        node = node_of(self.graph, 'decision:T1/g2')
        self.assertEqual(('g2', 'rejected', '2026-11-05T10:00:00+00:00', None, []),
                         (node['decision_type'], node['verdict'], node['decided_at'], node['rationale'], node['conditions']))

    def test_message_node_is_named_by_its_id_and_never_carries_the_body(self):
        node = node_of(self.graph, 'message:req-1')
        self.assertEqual(('outbox/p/req-1.json', 'review_request', 'Review T1', 'p', 'q'),
                         (node['source'], node['message_type'], node['subject'], node['from_project'], node['to_project']))
        self.assertNotIn('SECRET', json.dumps(self.graph))

    def test_report_node_is_named_by_its_path_below_reports(self):
        node = node_of(self.graph, 'evidence:report:p/r1')
        self.assertEqual(('reports/p/r1.json', 'report', 'review'), (node['source'], node['evidence_kind'], node['record_label']))

    def test_receipt_node_is_named_by_task_directory_and_file_stem(self):
        node = node_of(self.graph, 'evidence:receipt:T1/rc1')
        self.assertEqual(('dispatch-gates/T1/rc1.json', 'dispatch allowed'), (node['source'], node['record_label']))
        self.assertEqual('dispatch denied', node_of(self.graph, 'evidence:receipt:T1/rc2')['record_label'])

    def test_commit_node_exists_once_per_sha_with_the_first_naming_record_as_source(self):
        shas = sorted(node['id'] for node in self.graph['nodes'] if node['kind'] == 'evidence' and node['id'].startswith('evidence:commit:'))
        self.assertEqual(['evidence:commit:abc1234', 'evidence:commit:def5678'], shas)
        self.assertEqual('reports/p/r1.json', node_of(self.graph, 'evidence:commit:abc1234')['source'])
        self.assertEqual('tasks/p/T1.json', node_of(self.graph, 'evidence:commit:def5678')['source'])

    def test_session_node_carries_the_recorded_attributes_and_falls_back_to_the_file_stem(self):
        node = node_of(self.graph, 'session:s-1')
        self.assertEqual(('p', 'worker', 'h', 'active', '2026-11-02T08:00:00+00:00'),
                         (node['project'], node['role'], node['host'], node['status'], node['last_seen_at']))
        self.assertEqual('sessions/s-2.json', node_of(self.graph, 'session:s-2')['source'])

    def test_the_same_message_id_in_several_boxes_is_one_node_with_the_first_sorted_path_as_source(self):
        self.put('archive/p/req-1.json', {'id': 'req-1', 'type': 'review_request'})
        graph, _ = build_graph(self.root)
        nodes = [node for node in graph['nodes'] if node['id'] == 'message:req-1']
        self.assertEqual(['archive/p/req-1.json'], [node['source'] for node in nodes])

    def test_a_message_without_an_id_is_named_by_its_file_stem(self):
        self.put('inbox/q/note.json', {'type': 'note'})
        graph, _ = build_graph(self.root)
        self.assertEqual('inbox/q/note.json', node_of(graph, 'message:note')['source'])

    def test_message_endpoints_accept_plain_strings_objects_and_the_project_field(self):
        self.put('inbox/q/shapes.json', {'id': 'shapes', 'project': 'sender', 'recipient': 'target', 'title': 'x' * 300})
        self.put('inbox/q/objects.json', {'id': 'objects', 'from': 'plain-from', 'to': {'project': 'object-to'}})
        graph, _ = build_graph(self.root)
        shapes, objects = node_of(graph, 'message:shapes'), node_of(graph, 'message:objects')
        self.assertEqual(('sender', 'target', 'x' * 200), (shapes['from_project'], shapes['to_project'], shapes['subject']))
        self.assertEqual(('plain-from', 'object-to'), (objects['from_project'], objects['to_project']))

    def test_every_context_node_and_edge_names_a_relative_source(self):
        context = [node for node in self.graph['nodes'] if node['id'].split(':')[0] in ('decision', 'message', 'evidence', 'session')]
        context_edges = [edge for edge in self.graph['edges'] if edge['kind'] not in ('membership', 'requires')]
        sources = [node['source'] for node in context] + [edge['source'] for edge in context_edges]
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(source and not source.startswith('/') and '..' not in source.split('/'))

    def test_the_same_records_give_the_same_graph_every_time(self):
        self.assertEqual(self.graph, build_graph(self.root)[0])

    def test_the_dashboard_nodes_and_edges_are_kept_unchanged(self):
        dashboard = server.build_dashboard(self.root)
        self.assertEqual(dashboard['graph']['edges'], [edge for edge in self.graph['edges'] if edge['kind'] in ('membership', 'requires')])
        self.assertEqual(dashboard['graph']['nodes'], [node for node in self.graph['nodes'] if node['kind'] in ('project', 'epic', 'issue', 'task')])


class EdgeDerivationTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.graph, self.warnings = build_graph(self.root)

    def test_decides_follows_the_task_id_field(self):
        self.assertIn(('decision:T1/g1', 'task:T1'), edges_of(self.graph, 'decides'))

    def test_decides_follows_the_issue_key_field(self):
        self.put('decisions/free/d.json', {'issue_key': 'A-1'})
        graph, _ = build_graph(self.root)
        self.assertIn(('decision:free/d', 'issue:A-1'), edges_of(graph, 'decides'))

    def test_decides_falls_back_to_the_parent_directory_when_it_names_a_known_task(self):
        self.assertIn(('decision:T1/g2', 'task:T1'), edges_of(self.graph, 'decides'))

    def test_decides_without_fields_and_with_an_unknown_directory_makes_no_edge(self):
        self.put('decisions/nothing/d.json', {'verdict': 'x'})
        self.put('decisions/top.json', {'verdict': 'x'})
        graph, _ = build_graph(self.root)
        self.assertEqual([], [pair for pair in edges_of(graph, 'decides') if pair[0] in ('decision:nothing/d', 'decision:top')])

    def test_in_reply_to_links_a_message_to_the_message_it_answers(self):
        self.assertFalse(edges_of(self.graph, 'in_reply_to')[('message:reply-1', 'message:req-1')]['unresolved'])

    def test_in_reply_to_links_a_decision_to_the_request_it_decides(self):
        self.assertIn(('decision:T1/g1', 'message:req-1'), edges_of(self.graph, 'in_reply_to'))

    def test_evidenced_by_links_tasks_and_issues_to_their_reports(self):
        edges = edges_of(self.graph, 'evidenced_by')
        self.assertIn(('task:T1', 'evidence:report:p/r1'), edges)
        self.assertIn(('issue:A-1', 'evidence:report:p/r2'), edges)

    def test_evidenced_by_links_a_task_to_its_receipts(self):
        self.assertIn(('task:T1', 'evidence:receipt:T1/rc1'), edges_of(self.graph, 'evidenced_by'))

    def test_evidenced_by_links_a_task_and_a_report_subject_to_the_commits_they_name(self):
        edges = edges_of(self.graph, 'evidenced_by')
        self.assertEqual('tasks/p/T1.json', edges[('task:T1', 'evidence:commit:def5678')]['source'])
        self.assertEqual('reports/p/r1.json', edges[('task:T1', 'evidence:commit:abc1234')]['source'])

    def test_evidenced_by_resolves_a_record_reference_in_the_task_evidence_list(self):
        self.assertFalse(edges_of(self.graph, 'evidenced_by')[('task:T1', 'evidence:report:p/r1')]['unresolved'])

    def test_evidence_items_that_are_not_record_references_make_no_edge(self):
        targets = {end for (start, end) in edges_of(self.graph, 'evidenced_by') if start == 'task:T1'}
        self.assertFalse([target for target in targets if 'free' in target])

    def test_owned_by_links_a_task_to_the_session_that_owns_it(self):
        self.assertFalse(edges_of(self.graph, 'owned_by')[('task:T1', 'session:s-1')]['unresolved'])

    def test_authorizes_links_an_allowed_dispatch_receipt_to_its_task_only(self):
        pairs = set(edges_of(self.graph, 'authorizes'))
        self.assertIn(('evidence:receipt:T1/rc1', 'task:T1'), pairs)
        self.assertNotIn(('evidence:receipt:T1/rc2', 'task:T1'), pairs)

    def test_authorizes_links_a_decision_named_by_the_task_g1_reference(self):
        self.assertIn(('decision:T1/g1', 'task:T1'), edges_of(self.graph, 'authorizes'))

    def test_a_validate_receipt_does_not_authorize_the_task(self):
        self.put('dispatch-gates/T1/rc3.json', {'task_id': 'T1', 'operation': 'validate', 'allowed': True})
        graph, _ = build_graph(self.root)
        self.assertNotIn(('evidence:receipt:T1/rc3', 'task:T1'), edges_of(graph, 'authorizes'))

    def test_supersedes_links_a_report_to_the_report_whose_id_it_names(self):
        self.assertIn(('evidence:report:p/r2', 'evidence:report:p/r1'), edges_of(self.graph, 'supersedes'))

    def test_supersedes_links_a_decision_to_the_decision_a_record_reference_names(self):
        self.assertIn(('decision:T1/g2', 'decision:T1/g1'), edges_of(self.graph, 'supersedes'))

    def test_every_edge_carries_kind_source_and_unresolved(self):
        for edge in self.graph['edges']:
            with self.subTest(edge=edge):
                self.assertTrue({'from', 'to', 'kind', 'source', 'unresolved'} <= set(edge))

    def test_repeated_references_to_the_same_target_make_one_edge(self):
        self.put('inbox/q/reply-2.json', {'id': 'reply-2', 'in_reply_to': 'req-1'})
        graph, _ = build_graph(self.root)
        self.assertEqual(1, len([e for e in graph['edges'] if e['kind'] == 'in_reply_to' and e['from'] == 'message:reply-2']))


class UnknownStaysUnknownTests(DeliveryCase):
    def test_a_reply_to_a_missing_message_is_an_unresolved_edge_with_a_warning_and_no_invented_node(self):
        self.put('inbox/q/orphan.json', {'id': 'orphan', 'in_reply_to': 'ghost-message'})
        graph, warnings = build_graph(self.root)
        self.assertTrue(edges_of(graph, 'in_reply_to')[('message:orphan', 'message:ghost-message')]['unresolved'])
        self.assertIn('inbox/q/orphan.json: in_reply_to reference could not be resolved', warnings)
        self.assertNotIn('message:ghost-message', [node['id'] for node in graph['nodes']])

    def test_an_owner_session_without_a_record_is_unresolved_with_a_warning(self):
        self.task('T2', owner_session_id='s-missing')
        graph, warnings = build_graph(self.root)
        self.assertTrue(edges_of(graph, 'owned_by')[('task:T2', 'session:s-missing')]['unresolved'])
        self.assertIn('tasks/p/T2.json: owned_by reference could not be resolved', warnings)

    def test_an_evidence_path_without_a_record_is_unresolved_with_a_warning(self):
        self.task('T2', evidence=['reports/p/nothing.json'])
        graph, warnings = build_graph(self.root)
        self.assertTrue(edges_of(graph, 'evidenced_by')[('task:T2', 'record:reports/p/nothing.json')]['unresolved'])
        self.assertIn('tasks/p/T2.json: evidenced_by reference could not be resolved', warnings)

    def test_a_superseded_report_id_without_a_record_is_unresolved(self):
        self.put('reports/p/r3.json', {'id': 'report-three', 'supersedes_report': 'nothing'})
        graph, _ = build_graph(self.root)
        self.assertTrue(edges_of(graph, 'supersedes')[('evidence:report:p/r3', 'record:report-id:nothing')]['unresolved'])

    def test_a_decision_for_an_unknown_task_is_unresolved_without_a_placeholder_node(self):
        self.put('decisions/x/d.json', {'task_id': 'T404'})
        graph, warnings = build_graph(self.root)
        self.assertTrue(edges_of(graph, 'decides')[('decision:x/d', 'task:T404')]['unresolved'])
        self.assertIn('decisions/x/d.json: decides reference could not be resolved', warnings)
        self.assertNotIn('task:T404', [node['id'] for node in graph['nodes']])

    def test_an_edge_to_a_dashboard_placeholder_is_unresolved_without_a_second_warning(self):
        self.task('T2', depends_on=['GHOST'])
        self.put('decisions/x/d.json', {'task_id': 'GHOST'})
        graph, warnings = build_graph(self.root)
        self.assertTrue(edges_of(graph, 'decides')[('decision:x/d', 'task:GHOST')]['unresolved'])
        self.assertEqual([], [warning for warning in warnings if 'GHOST' in warning])

    def test_a_free_text_supersedes_and_a_mapping_g2_decision_make_no_edge(self):
        self.put('decisions/T1/g3.json', {'task_id': 'T1', 'supersedes': 'the earlier decision'})
        self.task('T2', g2_decision={'path': 'decisions/T1/g1.json'}, g1_decision='see chat')
        graph, warnings = build_graph(self.root)
        self.assertEqual([], [pair for pair in edges_of(graph, 'supersedes') if pair[0] == 'decision:T1/g3'])
        self.assertEqual([], [pair for pair in edges_of(graph, 'authorizes') if pair[1] == 'task:T2'])
        self.assertEqual([], [warning for warning in warnings if 'T2' in warning])

    def test_a_reference_to_a_non_decision_record_never_supersedes_a_decision(self):
        self.put('decisions/T1/g4.json', {'task_id': 'T1', 'supersedes': 'reports/p/r1.json'})
        graph, _ = build_graph(self.root)
        self.assertEqual([], [pair for pair in edges_of(graph, 'supersedes') if pair[0] == 'decision:T1/g4'])

    def test_a_malformed_commit_value_yields_no_node_and_no_edge(self):
        self.task('T2', head_sha='NOT-A-SHA', merge_sha='abc')
        graph, _ = build_graph(self.root)
        self.assertEqual([], [edge for edge in graph['edges'] if edge['from'] == 'task:T2' and edge['kind'] == 'evidenced_by'])

    def test_non_string_reference_values_yield_no_edges(self):
        self.put('inbox/q/odd.json', {'id': 'odd', 'in_reply_to': ['req-1'], 'task_id': 7})
        self.put('decisions/unknown/odd.json', {'task_id': {'x': 1}, 'request_id': 5, 'supersedes': 5})
        graph, _ = build_graph(self.root)
        self.assertEqual([], [edge for edge in graph['edges'] if edge['from'] in ('message:odd', 'decision:unknown/odd')])


class RecordReadingTests(MailboxCase):
    def setUp(self):
        super().setUp()
        self.put('projects.json', {'projects': []})

    def warnings_after(self, relative, content):
        self.put(relative, content)
        return build_graph(self.root)

    def test_a_symlinked_record_is_skipped_with_a_warning(self):
        outside = Path(self.temp.name + '-outside.json')
        outside.write_text('{"decision_type": "x"}')
        self.addCleanup(outside.unlink)
        (self.root / 'decisions').mkdir()
        (self.root / 'decisions' / 'linked.json').symlink_to(outside)
        graph, warnings = build_graph(self.root)
        self.assertIn('decisions/linked.json: symlink/outside data root', warnings)
        self.assertNotIn('decision:linked', [node['id'] for node in graph['nodes']])

    def test_an_oversized_record_is_skipped_with_a_warning(self):
        graph, warnings = self.warnings_after('decisions/big.json', '{"rationale": "' + 'x' * 4_000_000 + '"}')
        self.assertIn('decisions/big.json: exceeds 4 MB limit', warnings)
        self.assertNotIn('decision:big', [node['id'] for node in graph['nodes']])

    def test_a_record_with_a_non_finite_number_is_skipped_with_a_warning(self):
        graph, warnings = self.warnings_after('decisions/nan.json', '{"rationale": 1, "n": NaN}')
        self.assertEqual(['decisions/nan.json: Non-finite JSON number: NaN'], [w for w in warnings if w.startswith('decisions/nan.json:')])
        self.assertNotIn('decision:nan', [node['id'] for node in graph['nodes']])

    def test_a_record_that_is_not_an_object_is_skipped_with_a_warning(self):
        graph, warnings = self.warnings_after('decisions/list.json', '[1, 2]')
        self.assertIn('decisions/list.json: record is not a JSON object', warnings)
        self.assertNotIn('decision:list', [node['id'] for node in graph['nodes']])

    def test_a_malformed_record_gets_only_the_reader_warning(self):
        _, warnings = self.warnings_after('reports/bad.json', '{broken')
        self.assertEqual(['reports/bad.json: invalid JSON'], [warning for warning in warnings if warning.startswith('reports/bad')])

    def test_a_symlinked_record_directory_is_reported_and_not_entered(self):
        elsewhere = Path(self.temp.name + '-elsewhere')
        elsewhere.mkdir()
        self.addCleanup(elsewhere.rmdir)
        (elsewhere / 'd.json').write_text('{}')
        self.addCleanup((elsewhere / 'd.json').unlink)
        (self.root / 'decisions').mkdir()
        (self.root / 'decisions' / 'linked').symlink_to(elsewhere, target_is_directory=True)
        graph, warnings = build_graph(self.root)
        self.assertIn('decisions/linked: symlinked directory not followed', warnings)
        self.assertNotIn('decision:linked/d', [node['id'] for node in graph['nodes']])

    def test_a_symlinked_top_level_record_directory_is_reported_and_not_entered(self):
        elsewhere = Path(self.temp.name + '-top')
        elsewhere.mkdir()
        self.addCleanup(elsewhere.rmdir)
        (self.root / 'reports').symlink_to(elsewhere, target_is_directory=True)
        _, warnings = build_graph(self.root)
        self.assertIn('reports: symlinked directory not followed', warnings)

    def test_an_unreadable_directory_is_a_fixed_warning_without_the_operating_system_text(self):
        self.put('decisions/d.json', {})
        real_iterdir = Path.iterdir

        def failing_iterdir(path):
            if path.name == 'decisions':
                raise PermissionError(RAW_FAILURE)
            return real_iterdir(path)

        with patch.object(Path, 'iterdir', failing_iterdir):
            _, warnings = build_graph(self.root)
        self.assertIn('decisions: unreadable directory', warnings)
        self.assertNotIn('Errno', ' '.join(warnings))
        self.assertNotIn(self.temp.name, ' '.join(warnings))

    def test_missing_record_directories_are_not_a_warning(self):
        _, warnings = build_graph(self.root)
        self.assertEqual([], warnings)

    def test_records_deeper_than_their_level_are_not_read(self):
        self.put('inbox/q/deep/m.json', {'id': 'deep'})
        self.put('sessions/team/s.json', {'session_id': 'deep-session'})
        self.put('dispatch-gates/T1/sub/rc.json', {'task_id': 'T1'})
        graph, _ = build_graph(self.root)
        self.assertEqual([], [node['id'] for node in graph['nodes'] if node['id'] in ('message:deep', 'session:deep-session')])
        self.assertEqual([], [node['id'] for node in graph['nodes'] if node['id'].startswith('evidence:receipt')])

    def test_decisions_and_reports_are_read_at_any_depth(self):
        self.put('decisions/a/b/c.json', {})
        self.put('reports/x/y/z.json', {})
        graph, _ = build_graph(self.root)
        ids = {node['id'] for node in graph['nodes']}
        self.assertLessEqual({'decision:a/b/c', 'evidence:report:x/y/z'}, ids)

    def test_older_and_partial_record_shapes_are_still_read(self):
        self.put('decisions/d.json', {})
        self.put('inbox/q/m.json', {})
        self.put('reports/r.json', {'id': 5, 'head_sha': 12})
        self.put('dispatch-gates/T1/rc.json', {'allowed': 'yes', 'operation': 3})
        self.put('sessions/s.json', {'last_seen_at': 12})
        graph, _ = build_graph(self.root)
        ids = {node['id'] for node in graph['nodes']}
        self.assertLessEqual({'decision:d', 'message:m', 'evidence:report:r', 'evidence:receipt:T1/rc', 'session:s'}, ids)
        self.assertIsNone(node_of(graph, 'evidence:receipt:T1/rc')['record_label'])


class PureHelperTests(unittest.TestCase):
    def test_aware_time_accepts_only_timezone_aware_iso_strings(self):
        for value in ('2026-11-02T10:00:00+00:00', '2026-11-02T10:00:00Z', '2026-11-02T12:00:00+02:00'):
            with self.subTest(value=value):
                self.assertIsNotNone(context_graph.aware_time(value))
        for value in ('2026-11-02T10:00:00', '2026-11-02', 'yesterday', '', None, 5, ['2026-11-02T10:00:00Z']):
            with self.subTest(value=value):
                self.assertIsNone(context_graph.aware_time(value))

    def test_aware_time_compares_instants_across_offsets(self):
        self.assertEqual(context_graph.aware_time('2026-11-02T10:00:00+00:00'), context_graph.aware_time('2026-11-02T12:00:00+02:00'))

    def test_newest_first_orders_by_instant_then_id_with_missing_and_naive_times_last(self):
        items = [{'id': 'd', 't': None}, {'id': 'c', 't': '2026-01-01T00:00:00+00:00'}, {'id': 'b', 't': '2026-01-01T00:00:00'},
                 {'id': 'a', 't': '2026-01-01T00:00:00+00:00'}, {'id': 'e', 't': '2026-02-01T00:00:00+00:00'}]
        ordered = context_graph.newest_first(items, lambda item: item['t'])
        self.assertEqual(['e', 'a', 'c', 'b', 'd'], [item['id'] for item in ordered])

    def test_reference_path_accepts_relative_json_paths_under_a_record_root(self):
        for value, expected in (('decisions/T1/g1.json', 'decisions/T1/g1.json'), ('./reports/r.json', 'reports/r.json'),
                                ('dispatch-gates/T1/x.json', 'dispatch-gates/T1/x.json'), ('inbox/q/m.json', 'inbox/q/m.json'),
                                ('outbox/q/m.json', 'outbox/q/m.json'), ('archive/q/m.json', 'archive/q/m.json')):
            with self.subTest(value=value):
                self.assertEqual(expected, context_graph.reference_path(value))

    def test_reference_path_rejects_everything_else(self):
        for value in ('/abs/decisions/x.json', 'decisions/../x.json', 'decisions//x.json', 'decisions/x.txt', 'decisions.json', 'tasks/p/T1.json',
                      'decisions/./x.json', 'decisions\\x.json', 'free text', '', None, 5, {'p': 'decisions/x.json'}, ['decisions/x.json']):
            with self.subTest(value=value):
                self.assertIsNone(context_graph.reference_path(value))

    def test_record_paths_respect_the_depth_and_sort_by_relative_path(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for relative in ('f/b/2.json', 'f/a/1.json', 'f/top.json', 'f/a/note.txt', 'f/a/deep/3.json'):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text('{}')
            found = lambda depth: [path.relative_to(root).as_posix() for path in context_graph.record_paths(root, 'f', depth, [])]
            self.assertEqual(['f/top.json'], found(0))
            self.assertEqual(['f/a/1.json', 'f/b/2.json'], found(1))
            self.assertEqual(['f/a/1.json', 'f/a/deep/3.json', 'f/b/2.json', 'f/top.json'], found(None))

    def test_is_open_request_follows_the_frozen_rule(self):
        def message(message_id, message_type):
            return {'message_id': message_id, 'message_type': message_type}

        answered = ({'replied'}, {'decided'}, {'validated'})
        cases = [('open review request', message('m', 'review_request'), True), ('a request type containing request', message('m', 'x_request_y'), True),
                 ('not a request', message('m', 'status'), False), ('missing type', message('m', None), False),
                 ('replied', message('replied', 'review_request'), False), ('decided', message('decided', 'review_request'), False),
                 ('validated execution request', message('validated', 'execution_request'), False),
                 ('validated but not an execution request', message('validated', 'review_request'), True)]
        for name, node, expected in cases:
            with self.subTest(name):
                self.assertIs(expected, context_graph.is_open_request(node, answered))

    def test_ancestor_ids_walk_membership_by_distance_then_id_and_survive_a_cycle(self):
        edges = [{'from': start, 'to': end, 'kind': 'membership'} for start, end in
                 (('issue:B', 'task:t'), ('issue:A', 'task:t'), ('epic:E', 'issue:A'), ('epic:E', 'issue:B'), ('project:p', 'epic:E'), ('task:t', 'project:p'))]
        index = context_graph.GraphIndex({'nodes': [], 'edges': edges})
        self.assertEqual(['issue:A', 'issue:B', 'epic:E', 'project:p'], context_graph.ancestor_ids(index, 'task:t'))

    def test_ancestors_ignore_requires_edges(self):
        edges = [{'from': 'task:a', 'to': 'task:t', 'kind': 'requires'}]
        self.assertEqual([], context_graph.ancestor_ids(context_graph.GraphIndex({'nodes': [], 'edges': edges}), 'task:t'))

    def test_limits_match_the_frozen_contract(self):
        self.assertEqual({'ancestors': 20, 'unmet_prerequisites': 20, 'decisions': 10, 'open_requests': 10, 'evidence': 10, 'warnings': 50},
                         context_graph.LIMITS)


class PackContentTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.pack = build_pack(self.root, 'task:T1')

    def test_a_task_pack_describes_the_node(self):
        self.assertEqual({'id': 'task:T1', 'kind': 'task', 'label': 'T1', 'status': 'in_progress', 'ready': False, 'source': 'tasks/p/T1.json'},
                         self.pack['node'])

    def test_a_task_pack_has_the_frozen_top_level_fields(self):
        self.assertEqual({'generated_at', 'node', 'ancestors', 'unmet_prerequisites', 'decisions', 'open_requests', 'evidence', 'omitted', 'warnings'},
                         set(self.pack))

    def test_ancestors_follow_membership_upward_by_distance_then_id(self):
        ids = [ancestor['id'] for ancestor in self.pack['ancestors']]
        self.assertEqual('issue:A-1', ids[0])
        self.assertIn('epic:E', ids)
        self.assertLess(ids.index('issue:A-1'), ids.index('epic:E'))

    def test_unmet_prerequisites_list_own_first_then_inherited_with_their_origin(self):
        self.assertEqual([('task:T0', None), ('issue:A-0', 'issue:A-1'), ('issue:EXT-1', 'issue:A-1')],
                         [(item['id'], item['inherited_from']) for item in self.pack['unmet_prerequisites']])

    def test_a_task_whose_only_unmet_prerequisite_belongs_to_its_issue_shows_it_as_inherited(self):
        self.task('T3', issue_key='A-1')
        self.put('epics/E/issues.json', [{'key': 'A-1', 'task_ids': ['T1', 'T3'], 'depends_on': ['EXT-1']}])
        pack = build_pack(self.root, 'task:T3')
        self.assertEqual([('issue:EXT-1', 'issue:A-1')], [(item['id'], item['inherited_from']) for item in pack['unmet_prerequisites']])

    def test_a_prerequisite_reached_several_ways_is_listed_once_at_its_nearest_origin(self):
        def node(node_id, **fields):
            return {'id': node_id, 'kind': node_id.split(':')[0], 'label': node_id, 'status': 'open', 'ready': False, 'source': '', **fields}

        nodes = [node('task:t', unmet_prerequisites=['task:x']), node('issue:i', unmet_prerequisites=['task:x', 'task:y']),
                 node('epic:e', unmet_prerequisites=['task:y']), node('task:x'), node('task:y')]
        edges = [{'from': 'issue:i', 'to': 'task:t', 'kind': 'membership'}, {'from': 'epic:e', 'to': 'issue:i', 'kind': 'membership'}]
        pack = context_graph.assemble_pack({'nodes': nodes, 'edges': edges}, [], [], 'now', 'task:t')
        self.assertEqual([('task:x', None), ('task:y', 'issue:i')], [(item['id'], item['inherited_from']) for item in pack['unmet_prerequisites']])

    def test_the_prerequisites_of_a_ready_prerequisite_are_not_listed(self):
        self.task('T2', status='done', evidence=['proof'], depends_on=['T0'])
        self.task('T3', depends_on=['T2'])
        self.assertEqual([], build_pack(self.root, 'task:T3')['unmet_prerequisites'])

    def test_the_prerequisites_of_an_unmet_prerequisite_are_not_listed(self):
        self.task('T3', depends_on=['T1'])
        self.assertEqual(['task:T1'], [item['id'] for item in build_pack(self.root, 'task:T3')['unmet_prerequisites']])

    def test_the_owner_of_a_task_prerequisite_is_the_recorded_owner_of_that_task(self):
        self.task('T3', depends_on=['T1'])
        owners = build_pack(self.root, 'task:T3')['unmet_prerequisites'][0]['owners']
        self.assertEqual([{'task_id': 'T1', 'owner': 'alice', 'session_id': 's-1', 'project': 'p', 'last_seen_at': '2026-11-02T08:00:00+00:00'}], owners)

    def test_a_prerequisite_task_without_an_owner_has_null_owner_fields(self):
        owners = pack_item(self.pack['unmet_prerequisites'], 'task:T0')['owners']
        self.assertEqual([{'task_id': 'T0', 'owner': None, 'session_id': None, 'project': None, 'last_seen_at': None}], owners)

    def test_the_owners_of_an_issue_prerequisite_are_those_of_its_member_tasks(self):
        owners = pack_item(self.pack['unmet_prerequisites'], 'issue:A-0')['owners']
        self.assertEqual([{'task_id': 'T9', 'owner': 'bob', 'session_id': 's-2', 'project': 'q', 'last_seen_at': None}], owners)

    def test_a_prerequisite_without_member_tasks_has_no_owners(self):
        self.assertEqual([], pack_item(self.pack['unmet_prerequisites'], 'issue:EXT-1')['owners'])

    def test_decisions_carry_rationale_conditions_and_the_nodes_they_are_about(self):
        decision = pack_item(self.pack['decisions'], 'decision:T1/g1')
        self.assertEqual({'id': 'decision:T1/g1', 'decision_type': 'g1', 'verdict': 'approved', 'decided_at': '2026-11-02T10:00:00+00:00',
                          'rationale': 'Scope accepted', 'rationale_truncated': False, 'conditions': ['tests first'], 'about': ['task:T1'],
                          'supersedes': None, 'source': 'decisions/T1/g1.json'}, decision)

    def test_a_decision_names_the_decision_it_supersedes(self):
        self.assertEqual('decision:T1/g1', pack_item(self.pack['decisions'], 'decision:T1/g2')['supersedes'])

    def test_decisions_are_newest_first(self):
        self.assertEqual(['decision:T1/g2', 'decision:T1/g1'], [item['id'] for item in self.pack['decisions']])

    def test_open_requests_hold_the_message_metadata_and_never_the_body(self):
        self.put('outbox/p/req-2.json', {'id': 'req-2', 'type': 'review_request', 'task_id': 'T1', 'subject': 'Second', 'body': 'SECRET-BODY-TEXT'})
        pack = build_pack(self.root, 'task:T1')
        self.assertEqual([{'id': 'message:req-2', 'type': 'review_request', 'subject': 'Second', 'from_project': None, 'to_project': None,
                           'created_at': None, 'about': ['task:T1'], 'source': 'outbox/p/req-2.json'}], pack['open_requests'])
        self.assertNotIn('SECRET', json.dumps(pack))

    def test_a_request_with_a_reply_or_a_decision_is_not_open(self):
        self.assertEqual([], self.pack['open_requests'])

    def test_requests_follow_the_open_rule_for_decisions_and_validated_execution_requests(self):
        self.put('outbox/p/exec-ok.json', {'id': 'exec-ok', 'type': 'execution_request', 'task_id': 'T1'})
        self.put('outbox/p/exec-denied.json', {'id': 'exec-denied', 'type': 'execution_request', 'task_id': 'T1'})
        self.put('outbox/p/other-task.json', {'id': 'other-task', 'type': 'review_request', 'task_id': 'T0'})
        self.put('outbox/p/info.json', {'id': 'info', 'type': 'status_update', 'task_id': 'T1'})
        self.put('dispatch-gates/T1/v1.json', {'task_id': 'T1', 'operation': 'validate', 'allowed': True, 'message_id': 'exec-ok'})
        self.put('dispatch-gates/T1/v2.json', {'task_id': 'T1', 'operation': 'validate', 'allowed': False, 'message_id': 'exec-denied'})
        pack = build_pack(self.root, 'task:T1')
        self.assertEqual(['message:exec-denied'], [item['id'] for item in pack['open_requests']])

    def test_evidence_lists_reports_receipts_commits_and_referenced_records_with_their_labels(self):
        evidence = {item['id']: item for item in self.pack['evidence']}
        self.assertEqual(('report', 'review', '2026-11-03T09:00:00+00:00', 'reports/p/r1.json'),
                         tuple(evidence['evidence:report:p/r1'][key] for key in ('kind', 'label', 'at', 'source')))
        self.assertEqual(('receipt', 'dispatch allowed', '2026-11-04T09:00:00+00:00'),
                         tuple(evidence['evidence:receipt:T1/rc1'][key] for key in ('kind', 'label', 'at')))
        self.assertEqual(('commit', 'abc1234', None), tuple(evidence['evidence:commit:abc1234'][key] for key in ('kind', 'label', 'at')))

    def test_a_record_reference_to_a_message_is_evidence_of_kind_record_labelled_with_its_type(self):
        self.task('T2', evidence=['outbox/p/req-1.json'])
        item = pack_item(build_pack(self.root, 'task:T2')['evidence'], 'message:req-1')
        self.assertEqual(('record', 'review_request', 'outbox/p/req-1.json'), (item['kind'], item['label'], item['source']))

    def test_evidence_is_newest_first_with_undated_items_last_by_id(self):
        ids = [item['id'] for item in self.pack['evidence']]
        self.assertEqual(['evidence:receipt:T1/rc1', 'evidence:report:p/r1'], ids[:2])
        self.assertEqual(sorted(ids[2:]), ids[2:])

    def test_an_issue_pack_covers_the_issue_and_its_member_tasks(self):
        pack = build_pack(self.root, 'issue:A-1')
        self.assertEqual(['task:T1'], pack_item(pack['decisions'], 'decision:T1/g1')['about'])
        self.assertIn('issue:A-1', pack_item(pack['evidence'], 'evidence:report:p/r2')['about'])
        self.assertEqual([('issue:A-0', None), ('issue:EXT-1', None)], [(i['id'], i['inherited_from']) for i in pack['unmet_prerequisites']])

    def test_every_pack_source_is_relative_or_null(self):
        pack = build_pack(self.root, 'issue:A-1')
        sources = [pack['node']['source']] + [item['source'] for name in ('decisions', 'open_requests', 'evidence') for item in pack[name]]
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(source is None or not source.startswith('/'))

    def test_a_placeholder_node_of_the_dashboard_graph_has_a_pack_with_unresolved_status_and_no_source(self):
        self.task('T2', depends_on=['GHOST'])
        pack = build_pack(self.root, 'task:GHOST')
        self.assertEqual(('unresolved', None, False), (pack['node']['status'], pack['node']['source'], pack['node']['ready']))

    def test_an_unknown_node_has_no_pack(self):
        self.assertIsNone(build_pack(self.root, 'task:nothing'))

    def test_the_pack_is_deterministic_for_the_same_records(self):
        again = build_pack(self.root, 'task:T1')
        self.assertEqual({**self.pack, 'generated_at': None}, {**again, 'generated_at': None})

    def test_building_a_pack_writes_nothing_to_the_mailbox(self):
        before = self.tree_snapshot()
        build_pack(self.root, 'issue:A-1')
        self.assertEqual(before, self.tree_snapshot())

    def test_build_warnings_reach_the_pack_sorted_and_without_duplicates(self):
        self.put('decisions/zz.json', '[1]')
        self.put('decisions/aa.json', '[1]')
        warnings = build_pack(self.root, 'task:T1')['warnings']
        self.assertEqual(sorted(set(warnings)), warnings)
        self.assertLess(warnings.index('decisions/aa.json: record is not a JSON object'), warnings.index('decisions/zz.json: record is not a JSON object'))


class PackShapeTests(DeliveryCase):
    def decision(self, name, **fields):
        self.put(f'decisions/T1/{name}.json', {'task_id': 'T1', **fields})

    def decisions(self):
        return build_pack(self.root, 'task:T1')['decisions']

    def test_a_rationale_over_the_limit_is_cut_and_flagged(self):
        self.decision('long', rationale='r' * 601)
        item = pack_item(self.decisions(), 'decision:T1/long')
        self.assertEqual((600, True), (len(item['rationale']), item['rationale_truncated']))

    def test_a_rationale_at_the_limit_is_kept_whole(self):
        self.decision('exact', rationale='r' * 600)
        item = pack_item(self.decisions(), 'decision:T1/exact')
        self.assertEqual((600, False), (len(item['rationale']), item['rationale_truncated']))

    def test_conditions_are_limited_in_count_and_length_and_keep_only_strings(self):
        self.decision('many', conditions=['c' * 250] + [f'c{n}' for n in range(14)] + [None, 3])
        item = pack_item(self.decisions(), 'decision:T1/many')
        self.assertEqual((10, 200), (len(item['conditions']), len(item['conditions'][0])))

    def test_non_list_conditions_and_non_string_fields_become_empty_or_null(self):
        self.decision('odd', conditions='all of them', rationale=['no'], verdict=5, decision_type=7, decided_at='yesterday')
        item = pack_item(self.decisions(), 'decision:T1/odd')
        self.assertEqual(([], None, None, None, None), (item['conditions'], item['rationale'], item['verdict'], item['decision_type'], item['decided_at']))

    def test_decisions_with_naive_or_missing_times_sort_after_dated_ones_by_id(self):
        self.decision('naive', decided_at='2026-12-01T00:00:00')
        self.decision('missing')
        self.decision('tie-b', decided_at='2026-11-02T10:00:00+00:00')
        self.decision('tie-a', decided_at='2026-11-02T12:00:00+02:00')
        ids = [item['id'] for item in self.decisions()]
        self.assertEqual(['decision:T1/g2', 'decision:T1/g1', 'decision:T1/tie-a', 'decision:T1/tie-b'], ids[:4])
        self.assertEqual(['decision:T1/missing', 'decision:T1/naive'], ids[4:])


class PackLimitTests(MailboxCase):
    def setUp(self):
        super().setUp()
        self.task('T1')

    def pack(self):
        return build_pack(self.root, 'task:T1')

    def test_decisions_are_limited_and_the_omitted_count_is_reported(self):
        for number in range(13):
            self.put(f'decisions/T1/d{number:02}.json', {'task_id': 'T1', 'decided_at': f'2026-11-{number + 1:02}T00:00:00+00:00'})
        pack = self.pack()
        self.assertEqual((10, 3), (len(pack['decisions']), pack['omitted']['decisions']))
        self.assertEqual('decision:T1/d12', pack['decisions'][0]['id'])

    def test_open_requests_are_limited_and_the_omitted_count_is_reported(self):
        for number in range(12):
            self.put(f'outbox/p/r{number:02}.json', {'id': f'r{number:02}', 'type': 'review_request', 'task_id': 'T1'})
        pack = self.pack()
        self.assertEqual((10, 2), (len(pack['open_requests']), pack['omitted']['open_requests']))

    def test_evidence_is_limited_and_the_omitted_count_is_reported(self):
        for number in range(11):
            self.put(f'reports/p/e{number:02}.json', {'task_id': 'T1'})
        pack = self.pack()
        self.assertEqual((10, 1), (len(pack['evidence']), pack['omitted']['evidence']))

    def test_nothing_is_omitted_from_a_small_pack(self):
        self.assertEqual({'ancestors': 0, 'unmet_prerequisites': 0, 'decisions': 0, 'open_requests': 0, 'evidence': 0, 'warnings': 0},
                         self.pack()['omitted'])

    def test_unmet_prerequisites_ancestors_and_warnings_are_limited_by_assemble_pack(self):
        names = [f'x{number:02}' for number in range(25)]
        nodes = [{'id': 'task:t', 'kind': 'task', 'label': 't', 'status': 'open', 'ready': False, 'source': 'tasks/p/t.json',
                  'unmet_prerequisites': [f'task:{name}' for name in names]}]
        nodes += [{'id': f'task:{name}', 'kind': 'task', 'label': name, 'status': 'open', 'ready': False, 'source': ''} for name in names]
        nodes += [{'id': f'issue:i{number:02}', 'kind': 'issue', 'label': f'i{number:02}', 'status': 'open', 'ready': False} for number in range(22)]
        edges = [{'from': f'issue:i{number:02}', 'to': 'task:t', 'kind': 'membership'} for number in range(22)]
        pack = context_graph.assemble_pack({'nodes': nodes, 'edges': edges}, [], [f'w{number:02}' for number in range(53)], 'now', 'task:t')
        self.assertEqual((20, 20, 50), (len(pack['ancestors']), len(pack['unmet_prerequisites']), len(pack['warnings'])))
        self.assertEqual({'ancestors': 2, 'unmet_prerequisites': 5, 'decisions': 0, 'open_requests': 0, 'evidence': 0, 'warnings': 3}, pack['omitted'])
        self.assertEqual('now', pack['generated_at'])


class BuildContextPacksTests(DeliveryCase):
    def dashboard(self):
        return server.build_dashboard(self.root)

    def without_time(self, pack):
        return {key: value for key, value in pack.items() if key != 'generated_at'}

    def build_packs(self, node_ids):
        return context_graph.build_context_packs(self.dashboard(), self.root, server.read_json, node_ids)

    def test_each_pack_equals_the_single_node_pack(self):
        node_ids = ['task:T1', 'task:T0', 'issue:A-1']
        packs = self.build_packs(node_ids)
        self.assertEqual(node_ids, list(packs))
        for node_id in node_ids:
            with self.subTest(node_id=node_id):
                self.assertEqual(self.without_time(build_pack(self.root, node_id)), self.without_time(packs[node_id]))

    def test_ids_that_are_no_graph_node_are_left_out(self):
        packs = self.build_packs(['task:missing', 'task:T1', 'issue:NOPE-1'])
        self.assertEqual(['task:T1'], list(packs))

    def test_the_graph_is_built_once_for_all_requested_nodes(self):
        with patch.object(context_graph, 'build_context_graph', wraps=context_graph.build_context_graph) as builder:
            packs = self.build_packs(['task:T1', 'task:T0', 'issue:A-1'])
        self.assertEqual((3, 1), (len(packs), builder.call_count))

    def test_no_matching_node_gives_an_empty_result_without_building_the_graph(self):
        for node_ids in ([], ['task:missing']):
            with self.subTest(node_ids=node_ids), patch.object(context_graph, 'build_context_graph') as builder:
                self.assertEqual({}, self.build_packs(node_ids))
                builder.assert_not_called()

    def test_building_packs_leaves_the_mailbox_byte_identical(self):
        before = self.tree_snapshot()
        self.build_packs(['task:T1'])
        self.assertEqual(before, self.tree_snapshot())

    def test_the_single_node_pack_is_unchanged_for_a_known_and_an_unknown_node(self):
        self.assertEqual('task:T1', build_pack(self.root, 'task:T1')['node']['id'])
        self.assertIsNone(build_pack(self.root, 'task:missing'))


if __name__ == '__main__':
    unittest.main()
