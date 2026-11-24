"""Behavioral guards for dependency-gated mailbox dispatch and worker validation."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import dispatch_gate


class DispatchGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_dir = self.root / 'project-source'
        self.project_dir.mkdir()
        self.loop = self.root / 'project-loop' / 'SKILL.md'
        self.loop.parent.mkdir()
        self.loop.write_text('Project loop fixture')
        self.endpoint = 'worker-one'
        self.target = str(uuid.uuid4())
        self.task_path = f'tasks/p/{self.target}.json'
        self.put('projects.json', {'projects': [{'id': 'p', 'name': 'Project',
            'repository_path': str(self.project_dir), 'working_directory': str(self.project_dir)}]})
        self.put('sessions/p-worker.json', {'project': 'p', 'role': 'worker',
            'session_id': self.endpoint, 'working_directory': str(self.project_dir)})
        self.epic([{'key': 'A', 'projects': ['p']}])
        self.task(self.target, issue_key='A')

    def tearDown(self):
        self.temp.cleanup()

    def put(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def read(self, path):
        return json.loads((self.root / path).read_text())

    def task(self, key=None, **extra):
        key = key or str(uuid.uuid4())
        value = {'id': key, 'project': 'p', 'title': 'Scoped task', 'status': 'pending',
            'depends_on': [], 'blockers': [], 'required_loop_skill': 'fixture.loop',
            'required_loop_path': str(self.loop), **extra}
        self.put(f'tasks/p/{key}.json', value)
        return key

    def update_task(self, **extra):
        value = self.read(self.task_path)
        value.update(extra)
        self.put(self.task_path, value)

    def epic(self, issues, **scope_extra):
        self.put('epics/E/scope.json', {'epic_key': 'E', 'enumeration_complete': True,
            'issue_keys': [i['key'] for i in issues], **scope_extra})
        self.put('epics/E/issues.json', issues)

    def spec(self, nodes, edges):
        self.put('epics/E/dependencies.json', {'nodes': nodes, 'edges': edges})

    def inbox(self):
        return list((self.root / 'inbox').glob('*/*.json'))

    def audit(self):
        return list((self.root / 'dispatch-gates').glob('*/*.json'))

    def evaluate(self):
        return dispatch_gate.evaluate(self.root, self.task_path)

    def send(self, **kwargs):
        return dispatch_gate.dispatch(self.root, self.task_path, self.endpoint, **kwargs)

    def validate(self, result):
        return dispatch_gate.validate(self.root, result['message_path'], self.endpoint)

    def test_independent_task_eligible_without_promoting_completion_or_writing(self):
        before = (self.root / self.task_path).read_bytes()
        result = self.evaluate()
        self.assertTrue(result['allowed'], result)
        self.assertEqual([], result['prerequisites'])
        self.assertEqual(before, (self.root / self.task_path).read_bytes())
        self.assertEqual([], self.inbox())
        self.assertEqual([], self.audit())

    def test_unready_direct_dependency_denies_with_audit_and_no_message(self):
        dep = self.task()
        self.update_task(depends_on=[dep])
        result = self.send()
        self.assertFalse(result['allowed'], result)
        self.assertIn('task:' + dep, result['prerequisites'])
        self.assertEqual([], self.inbox())
        self.assertTrue(self.audit())
        self.assertEqual('pending', self.read(self.task_path)['status'])

    def test_completed_dependency_allows_but_target_remains_pending(self):
        dep = self.task(status='done', evidence=['reports/passing-tests.json'])
        self.update_task(depends_on=[dep])
        result = self.send()
        self.assertTrue(result['allowed'], result)
        self.assertEqual('pending', self.read(self.task_path)['status'])
        self.assertTrue(self.validate(result)['allowed'])

    def test_done_dependency_without_nonblank_evidence_is_not_ready(self):
        for evidence in ([], [''], [' \t ']):
            with self.subTest(evidence=evidence):
                dep = self.task(status='done', evidence=evidence)
                self.update_task(depends_on=[dep])
                self.assertFalse(self.evaluate()['allowed'])

    def test_missing_dependency_is_unresolved_and_denied(self):
        missing = str(uuid.uuid4())
        self.update_task(depends_on=[missing])
        result = self.evaluate()
        self.assertFalse(result['allowed'])
        self.assertIn('task:' + missing, result['prerequisites'])

    def test_transitive_unready_dependency_blocks_despite_direct_done(self):
        earlier = self.task()
        direct = self.task(status='done', evidence=['proof'], depends_on=[earlier])
        self.update_task(depends_on=[direct])
        result = self.evaluate()
        self.assertFalse(result['allowed'], result)
        self.assertIn('task:' + earlier, result['prerequisites'])

    def test_jira_closed_inherited_issue_dependency_is_not_local_completion(self):
        self.epic([{'key': 'A', 'projects': ['p'], 'depends_on': ['B']},
                   {'key': 'B', 'source_status': 'Closed', 'local_status': 'pending'}])
        result = self.evaluate()
        self.assertFalse(result['allowed'], result)
        self.assertIn('issue:B', result['prerequisites'])

    def test_inherits_story_and_epic_prerequisites_without_requiring_own_issue(self):
        self.epic([{'key': 'A', 'projects': ['p'], 'parent_key': 'S'},
                   {'key': 'S', 'depends_on': ['B']},
                   {'key': 'B', 'local_status': 'verified_done', 'verification': 'passed', 'evidence': ['proof']}])
        self.assertTrue(self.evaluate()['allowed'])
        self.spec([{'id': 'milestone:release', 'status': 'unknown'}],
                  [{'from': 'milestone:release', 'to': 'epic:E', 'kind': 'requires'}])
        result = self.evaluate()
        self.assertFalse(result['allowed'], result)
        self.assertIn('milestone:release', result['prerequisites'])

    def test_unrelated_downstream_unknown_does_not_block_target(self):
        self.epic([{'key': 'A', 'projects': ['p']}], external_dependencies=[
            {'key': 'OUT', 'relationship': {'name': 'Blocks'}, 'direction': 'outward'},
            'UNKNOWN-DIRECTION'])
        self.assertTrue(self.evaluate()['allowed'])

    def test_overflowing_estimate_sums_in_the_mailbox_do_not_change_an_allowed_result(self):
        before = self.evaluate()
        huge = {'low': 1e308, 'likely': 1e308, 'high': 1e308}
        self.epic([{'key': 'A', 'projects': ['p'], 'agent_estimate_hours': huge},
                   {'key': 'B', 'agent_estimate_hours': huge}])
        after = self.evaluate()
        self.assertTrue(after['allowed'], after)
        self.assertEqual((before['allowed'], before['reasons'], before['prerequisites']),
                         (after['allowed'], after['reasons'], after['prerequisites']))

    def test_cycles_block_even_if_all_dependency_completion_is_recorded(self):
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        self.task(first, status='done', evidence=['proof'], depends_on=[second])
        self.task(second, status='done', evidence=['proof'], depends_on=[first])
        self.update_task(depends_on=[first])
        self.assertFalse(self.evaluate()['allowed'])

    def test_inherited_membership_requires_cycle_is_not_self_authorization(self):
        self.spec([], [{'from': 'task:' + self.target, 'to': 'issue:A', 'kind': 'requires'}])
        self.assertFalse(self.evaluate()['allowed'])

    def test_verified_milestone_needs_actual_nonblank_evidence(self):
        for evidence, allowed in (([' '], False), (['decision.json'], True)):
            with self.subTest(evidence=evidence):
                self.spec([{'id': 'milestone:release', 'status': 'verified',
                    'verification': 'passed', 'evidence': evidence}],
                    [{'from': 'milestone:release', 'to': 'task:' + self.target, 'kind': 'requires'}])
                self.assertEqual(allowed, self.evaluate()['allowed'])

    def test_execution_task_may_deliver_only_one_sub_task_but_investigation_may_span_several(self):
        self.epic([{'key': 'A', 'projects': ['p'], 'issue_type': 'Sub-task', 'task_ids': [self.target]},
                   {'key': 'B', 'projects': ['p'], 'issue_type': 'Sub-task', 'task_ids': [self.target]}])
        result = self.evaluate()
        self.assertFalse(result['allowed'], result)
        self.assertTrue(any('several Sub-tasks (A, B)' in reason for reason in result['reasons']), result)
        self.update_task(task_kind='intake_baseline')
        self.assertTrue(self.evaluate()['allowed'])

    def test_execution_task_for_one_sub_task_and_its_story_is_allowed(self):
        self.epic([{'key': 'A', 'projects': ['p'], 'issue_type': 'Sub-task', 'parent_key': 'S', 'task_ids': [self.target]},
                   {'key': 'S', 'issue_type': 'Story', 'task_ids': [self.target]}])
        self.assertTrue(self.evaluate()['allowed'])

    def test_target_status_and_explicit_holds_cannot_be_bypassed(self):
        for changes in ({'status': 'blocked'}, {'status': 'done'},
                        {'status': 'pending', 'blockers': ['Weekly quota stop']}):
            with self.subTest(changes=changes):
                self.update_task(status='pending', blockers=[], **{k:v for k,v in changes.items() if k not in ('status','blockers')})
                self.update_task(**changes)
                self.assertFalse(self.send()['allowed'])
                self.assertEqual([], self.inbox())

    def test_malformed_dependency_declarations_fail_closed(self):
        for value in (None, 'single-id', {}, [42], [''], [' \t ']):
            with self.subTest(value=value):
                self.update_task(depends_on=value)
                self.assertFalse(self.evaluate()['allowed'])
        task = self.read(self.task_path)
        task.pop('depends_on')
        self.put(self.task_path, task)
        self.assertFalse(self.evaluate()['allowed'])

    def test_malformed_authoritative_json_or_graph_record_denies(self):
        path = self.root / 'epics/E/dependencies.json'
        path.write_text('{not-json')
        self.assertFalse(self.evaluate()['allowed'])
        self.put('epics/E/dependencies.json', {'nodes': [], 'edges': [{'from': [], 'to': 'task:' + self.target}]})
        self.assertFalse(self.evaluate()['allowed'])

    def test_duplicate_json_keys_are_not_silently_last_wins(self):
        path = self.root / self.task_path
        value = path.read_text()
        path.write_text(value[:-1] + ', "depends_on": []}')
        self.assertFalse(self.evaluate()['allowed'])

    def test_wrong_project_id_filename_or_missing_loop_denies(self):
        for changes in ({'project': 'other'}, {'id': str(uuid.uuid4())},
                        {'required_loop_path': str(self.root / 'missing')}, {'required_loop_skill': ''}):
            with self.subTest(changes=changes):
                before = self.read(self.task_path)
                self.update_task(**changes)
                self.assertFalse(self.evaluate()['allowed'])
                self.put(self.task_path, before)

    def test_symlink_or_outside_task_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            path = Path(other) / 'task.json'
            path.write_text((self.root / self.task_path).read_text())
            self.assertFalse(dispatch_gate.evaluate(self.root, path)['allowed'])
            actual = self.root / self.task_path
            actual.unlink()
            actual.symlink_to(path)
            self.assertFalse(self.evaluate()['allowed'])

    def test_duplicate_task_identity_is_ambiguous(self):
        value = self.read(self.task_path)
        self.put('tasks/other/' + self.target + '.json', value)
        self.assertFalse(self.evaluate()['allowed'])

    def test_dispatch_requires_matching_self_registered_worker(self):
        for changes in ({'project': 'other'}, {'session_id': 'different'},
                        {'working_directory': str(self.root / 'wrong-directory')}):
            with self.subTest(changes=changes):
                value = self.read('sessions/p-worker.json')
                self.put('sessions/p-worker.json', {**value, **changes})
                self.assertFalse(self.send()['allowed'])
                self.assertEqual([], self.inbox())
                self.put('sessions/p-worker.json', value)

    def test_successful_envelope_and_receipt_are_bound_and_immutable(self):
        message_id = str(uuid.uuid4())
        result = self.send(message_id=message_id)
        self.assertTrue(result['allowed'], result)
        message = self.read(result['message_path'])
        receipt = self.read(result['audit_path'])
        self.assertEqual(message_id, message['id'])
        self.assertEqual(self.target, message['task_id'])
        self.assertEqual(self.task_path, message['task_path'])
        self.assertEqual(self.endpoint, message['intended_endpoint'])
        self.assertEqual(result['audit_path'], message['receipt_path'])
        self.assertEqual('dispatch', receipt['operation'])
        self.assertTrue(receipt['allowed'])
        self.assertTrue(receipt['fingerprint'])
        self.assertTrue(receipt['message_hash'])
        before = {p: p.read_bytes() for p in self.inbox()}
        again = self.send(message_id=message_id)
        self.assertTrue(again['allowed'], again)
        self.assertEqual(before, {p:p.read_bytes() for p in self.inbox()})

    def test_fresh_dispatch_for_same_task_does_not_queue_duplicate_work(self):
        self.assertTrue(self.send()['allowed'])
        self.send()
        self.assertEqual(1, len(self.inbox()))

    def test_archived_request_is_never_republished_and_not_executable(self):
        message_id = str(uuid.uuid4())
        result = self.send(message_id=message_id)
        source = self.root / result['message_path']
        archive = self.root / 'archive/p' / source.name
        archive.parent.mkdir(parents=True)
        source.rename(archive)
        self.send(message_id=message_id)
        self.assertEqual([], self.inbox())
        self.assertFalse(dispatch_gate.validate(self.root, archive.relative_to(self.root), self.endpoint)['allowed'])

    def test_wrong_worker_missing_or_changed_message_denies_validation(self):
        result = self.send()
        self.assertFalse(dispatch_gate.validate(self.root, result['message_path'], 'other-worker')['allowed'])
        path = self.root / result['message_path']
        value = self.read(result['message_path'])
        self.put(result['message_path'], {**value, 'body': 'Different scope'})
        self.assertFalse(self.validate(result)['allowed'])
        path.unlink()
        self.assertFalse(self.validate(result)['allowed'])

    def test_missing_or_tampered_receipt_denies_validation(self):
        result = self.send()
        path = self.root / result['audit_path']
        value = self.read(result['audit_path'])
        for changes in ({'operation': 'check'}, {'schema_version': 999}, {'allowed': False},
                        {'task_id': str(uuid.uuid4())}, {'endpoint': 'other-worker'}, {'fingerprint': 'wrong'}):
            with self.subTest(changes=changes):
                self.put(result['audit_path'], {**value, **changes})
                self.assertFalse(self.validate(result)['allowed'])
        path.unlink()
        self.assertFalse(self.validate(result)['allowed'])

    def test_stale_dependency_change_denies_worker_without_changing_task(self):
        dep = self.task(status='done', evidence=['proof'])
        self.update_task(depends_on=[dep])
        result = self.send()
        before = (self.root / self.task_path).read_bytes()
        self.task(dep, status='pending')
        self.assertFalse(self.validate(result)['allowed'])
        self.assertEqual(before, (self.root / self.task_path).read_bytes())

    def test_jira_snapshot_refresh_keeps_receipt_but_issue_dependency_change_stales_it(self):
        self.epic([{'key': 'A', 'projects': ['p'], 'source_status': 'In Progress'}])
        result = self.send()
        self.assertTrue(result['allowed'], result)
        self.epic([{'key': 'A', 'projects': ['p'], 'source_status': 'Closed',
                    'source_status_synced_at': '2026-11-22T00:00:00+00:00'}])
        self.assertTrue(self.validate(result)['allowed'])
        self.epic([{'key': 'A', 'projects': ['p'], 'source_status': 'Closed',
                    'source_status_synced_at': '2026-11-22T00:00:00+00:00', 'depends_on': ['B']},
                   {'key': 'B', 'projects': ['p'], 'local_status': 'verified_done',
                    'verification': 'passed', 'evidence': ['proof']}])
        self.assertFalse(self.validate(result)['allowed'])

    def test_relevant_graph_or_task_change_invalidates_receipt(self):
        for mutation in ('task', 'graph', 'registry'):
            with self.subTest(mutation=mutation):
                # Each subcase uses an isolated pending target/envelope and fresh snapshot.
                case = DispatchGateTests(methodName='test_independent_task_eligible_without_promoting_completion_or_writing')
                case.setUp()
                try:
                    result = case.send()
                    self.assertTrue(result['allowed'], result)
                    if mutation == 'task': case.update_task(title='Changed specification')
                    elif mutation == 'graph':
                        case.spec([{'id': 'milestone:new', 'status': 'verified',
                                    'verification': 'passed', 'evidence': ['proof']}],
                                  [{'from': 'milestone:new', 'to': 'task:' + case.target,
                                    'kind': 'requires'}])
                    elif mutation == 'registry':
                        value = case.read('projects.json');value['projects'][0]['name'] = 'Changed project';case.put('projects.json', value)
                    self.assertFalse(case.validate(result)['allowed'])
                finally: case.tearDown()

    def test_independent_workers_can_claim_and_report_without_staling_other_delivery(self):
        sibling = self.task(issue_key='A')
        result = self.send()
        other = dispatch_gate.dispatch(self.root, f'tasks/p/{sibling}.json', self.endpoint)
        self.assertTrue(other['allowed'], other)
        self.assertTrue(dispatch_gate.validate(self.root, other['message_path'], self.endpoint)['allowed'])
        self.task(sibling, issue_key='A', status='in_progress', owner_session_id=self.endpoint,
                  evidence=['progress'], updated_at='later')
        self.assertTrue(self.validate(result)['allowed'])
        self.task(sibling, issue_key='A', status='done', evidence=['reviewed proof'])
        self.assertTrue(self.validate(result)['allowed'])

    def test_unrelated_additions_updates_and_deletions_do_not_stale_receipt(self):
        result = self.send()
        sibling = self.task()
        self.put('projects/p.json', {'id': 'p', 'work_status': 'changed'})
        self.put('epics/E/progress.json', {'generated_at': 'later'})
        self.epic([{'key': 'A', 'projects': ['p']}, {'key': 'B', 'projects': ['p']}])
        self.spec([{'id': 'milestone:other', 'status': 'unknown'}],
                  [{'from': 'milestone:other', 'to': 'issue:B', 'kind': 'requires'}])
        self.put('sessions/other.json', {'project': 'p', 'role': 'worker',
            'session_id': 'other-worker', 'working_directory': str(self.project_dir)})
        self.assertTrue(self.validate(result)['allowed'])
        (self.root / f'tasks/p/{sibling}.json').unlink()
        (self.root / 'sessions/other.json').unlink()
        self.assertTrue(self.validate(result)['allowed'])

    def test_own_registration_change_stales_but_check_in_does_not(self):
        result = self.send()
        registration = self.read('sessions/p-worker.json')
        self.put('sessions/p-worker.json', {**registration, 'last_seen_at': 'later', 'status': 'busy'})
        self.assertTrue(self.validate(result)['allowed'])
        self.put('sessions/p-worker.json', {**registration, 'agent_id': 'changed-agent'})
        self.assertFalse(self.validate(result)['allowed'])

    def test_parent_change_and_new_inherited_dependency_stale_existing_receipt(self):
        result = self.send()
        self.epic([{'key': 'A', 'projects': ['p'], 'parent_key': 'S'}, {'key': 'S'}])
        self.assertFalse(self.validate(result)['allowed'])
        fresh = self.send()
        self.assertTrue(fresh['allowed'], fresh)
        self.epic([{'key': 'A', 'projects': ['p'], 'parent_key': 'S'},
                   {'key': 'S', 'depends_on': ['B']}, {'key': 'B'}])
        self.assertFalse(self.validate(fresh)['allowed'])

    def test_changed_ready_transitive_dependency_evidence_stales(self):
        earlier = self.task(status='done', evidence=['first proof'])
        direct = self.task(status='done', evidence=['proof'], depends_on=[earlier])
        self.update_task(depends_on=[direct])
        result = self.send()
        self.task(earlier, status='done', evidence=['different proof'])
        self.assertFalse(self.validate(result)['allowed'])

    def test_loop_content_change_invalidates_receipt(self):
        result = self.send()
        self.loop.write_text('Changed protocol')
        self.assertFalse(self.validate(result)['allowed'])

    def test_cli_check_is_audit_only_and_nonzero_for_blocked(self):
        dep = self.task()
        self.update_task(depends_on=[dep])
        proc = subprocess.run([sys.executable, str(Path(dispatch_gate.__file__)), '--root', str(self.root),
            'check', '--task', self.task_path], capture_output=True, text=True)
        self.assertEqual(2, proc.returncode, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertFalse(result['allowed'])
        self.assertEqual([], self.inbox())
        self.assertTrue(self.audit())
        self.assertEqual('check', self.read(result['audit_path'])['operation'])


    def test_missing_or_unsupported_target_status_denies(self):
        for status in (None, '', 'ready', 'closed'):
            with self.subTest(status=status):
                self.update_task(status=status)
                self.assertFalse(self.send()['allowed'])
                self.assertEqual([], self.inbox())
        task = self.read(self.task_path)
        task.pop('status')
        self.put(self.task_path, task)
        self.assertFalse(self.evaluate()['allowed'])

    def test_missing_owning_issue_context_cannot_hide_inherited_prerequisites(self):
        self.update_task(issue_key='NOT-ENUMERATED')
        self.assertFalse(self.evaluate()['allowed'])

    def test_source_change_during_graph_read_denies_without_message(self):
        original = dispatch_gate.build_dashboard
        def changing_graph(*args, **kwargs):
            result = original(*args, **kwargs)
            self.update_task(title='Changed while graph was being read')
            return result
        with patch.object(dispatch_gate, 'build_dashboard', changing_graph):
            self.assertFalse(self.send()['allowed'])
        self.assertEqual([], self.inbox())

    def test_unrelated_change_during_graph_read_rechecks_without_false_denial(self):
        sibling = self.task()
        original = dispatch_gate.build_dashboard
        changed = False
        def changing_graph(*args, **kwargs):
            nonlocal changed
            result = original(*args, **kwargs)
            if not changed:
                changed = True
                self.task(sibling, status='in_progress', evidence=['other progress'])
            return result
        with patch.object(dispatch_gate, 'build_dashboard', changing_graph):
            result = self.send()
        self.assertTrue(changed)
        self.assertTrue(result['allowed'], result)
        self.assertTrue(self.validate(result)['allowed'])

    def test_unrelated_change_after_receipt_does_not_prevent_publication(self):
        sibling = self.task()
        original = dispatch_gate._publish
        def changing_publish(root, path, value):
            original(root, path, value)
            if value.get('operation') == 'dispatch' and value.get('allowed'):
                self.task(sibling, status='in_progress', evidence=['other progress'])
        with patch.object(dispatch_gate, '_publish', changing_publish):
            result = self.send()
        self.assertTrue(result['allowed'], result)
        self.assertTrue(self.validate(result)['allowed'])

    def test_prerequisite_issue_supporting_task_change_stales_even_when_ready(self):
        supporting = self.task(issue_key='B', status='done', evidence=['first proof'])
        self.epic([{'key': 'A', 'depends_on': ['B']},
                   {'key': 'B', 'local_status': 'verified_done', 'verification': 'passed',
                    'evidence': ['issue proof']}])
        result = self.send()
        self.assertTrue(result['allowed'], result)
        self.task(supporting, issue_key='B', status='done', evidence=['replacement proof'])
        self.assertFalse(self.validate(result)['allowed'])

    def test_old_global_receipt_version_requires_fresh_dispatch(self):
        result = self.send()
        message = self.read(result['message_path'])
        self.put(result['message_path'], {**message, 'schema_version': 1})
        self.assertFalse(self.validate(result)['allowed'])

    def test_source_change_after_receipt_prevents_executable_publication(self):
        original = dispatch_gate._publish
        changed = False
        def changing_publish(root, path, value):
            nonlocal changed
            original(root, path, value)
            if not changed and value.get('operation') == 'dispatch' and value.get('allowed'):
                changed = True
                self.update_task(title='Changed after audit publication')
        with patch.object(dispatch_gate, '_publish', changing_publish):
            result = self.send()
        self.assertTrue(changed)
        self.assertFalse(result['allowed'], result)
        self.assertEqual([], self.inbox())
        self.assertTrue(any(self.read(p.relative_to(self.root)).get('allowed') for p in self.audit()))

    def test_failed_inbox_publication_leaves_no_execution_and_retry_recovers(self):
        original = dispatch_gate._publish
        def failing_inbox(root, path, value):
            if Path(path).parts[0] == 'inbox':
                raise OSError('Simulated publication interruption')
            return original(root, path, value)
        message_id = str(uuid.uuid4())
        with patch.object(dispatch_gate, '_publish', failing_inbox):
            result = self.send(message_id=message_id)
        self.assertFalse(result['allowed'])
        self.assertEqual([], self.inbox())
        orphan = next(p for p in self.audit() if self.read(p.relative_to(self.root)).get('allowed'))
        self.assertFalse(dispatch_gate.validate(self.root, orphan.relative_to(self.root), self.endpoint)['allowed'])
        retry = self.send(message_id=message_id)
        self.assertTrue(retry['allowed'], retry)
        self.assertTrue(self.validate(retry)['allowed'])

    def test_competing_dispatch_lock_is_preserved_and_denies(self):
        lock = self.root / 'dispatch-gates' / self.target / '.dispatch.lock'
        lock.mkdir(parents=True)
        (lock / 'owner').write_text('Other dispatcher')
        result = self.send()
        self.assertFalse(result['allowed'])
        self.assertTrue(lock.is_dir())
        self.assertEqual('Other dispatcher', (lock / 'owner').read_text())
        self.assertEqual([], self.inbox())

    def test_receipt_with_non_uuid_identity_denies(self):
        result = self.send()
        value = self.read(result['audit_path'])
        self.put(result['audit_path'], {**value, 'id': 'not-a-receipt-uuid'})
        self.assertFalse(self.validate(result)['allowed'])

    def test_cli_successful_dispatch_and_validate_and_structured_input_failure(self):
        prefix = [sys.executable, str(Path(dispatch_gate.__file__)), '--root', str(self.root)]
        proc = subprocess.run(prefix + ['dispatch', '--task', self.task_path,
            '--worker-id', self.endpoint], capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result['allowed'])
        proc = subprocess.run(prefix + ['validate', '--message', result['message_path'],
            '--worker-id', self.endpoint], capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)['allowed'])
        proc = subprocess.run(prefix + ['dispatch'], capture_output=True, text=True)
        self.assertEqual(1, proc.returncode)
        self.assertFalse(json.loads(proc.stdout)['allowed'])


    def test_malformed_registry_entry_denies_check_dispatch_and_validation_with_audit(self):
        sent = self.send()
        self.assertTrue(sent['allowed'], sent)
        registry = self.read('projects.json')
        registry['projects'].insert(0, None)
        self.put('projects.json', registry)
        checked = self.evaluate()
        self.assertFalse(checked['allowed'], checked)
        denied = self.send()
        self.assertFalse(denied['allowed'], denied)
        self.assertTrue((self.root / denied['audit_path']).is_file())
        validated = self.validate(sent)
        self.assertFalse(validated['allowed'], validated)
        self.assertTrue((self.root / validated['audit_path']).is_file())
        proc = subprocess.run([sys.executable, str(Path(dispatch_gate.__file__)), '--root', str(self.root),
            'check', '--task', self.task_path], capture_output=True, text=True)
        self.assertNotEqual(0, proc.returncode)
        result = json.loads(proc.stdout)
        self.assertFalse(result['allowed'])
        self.assertTrue((self.root / result['audit_path']).is_file())
        self.assertEqual(1, len(self.inbox()))

    def test_archived_request_copied_back_to_inbox_cannot_execute(self):
        sent = self.send()
        source = self.root / sent['message_path']
        archive = self.root / 'archive/p' / source.name
        archive.parent.mkdir(parents=True)
        source.rename(archive)
        source.write_bytes(archive.read_bytes())
        result = self.validate(sent)
        self.assertFalse(result['allowed'], result)
        self.assertEqual('pending', self.read(self.task_path)['status'])
        self.assertTrue((self.root / result['audit_path']).is_file())

    def test_duplicate_inbox_uuid_under_another_recipient_cannot_execute(self):
        sent = self.send()
        source = self.root / sent['message_path']
        duplicate = self.root / 'inbox/other-recipient' / source.name
        duplicate.parent.mkdir(parents=True)
        duplicate.write_bytes(source.read_bytes())
        result = self.validate(sent)
        self.assertFalse(result['allowed'], result)
        self.assertEqual('pending', self.read(self.task_path)['status'])
        self.assertTrue((self.root / result['audit_path']).is_file())


    def test_duplicate_prerequisite_uuid_with_conflicting_completion_fails_closed(self):
        dependency = self.task(status='pending')
        self.update_task(depends_on=[dependency])
        registry = self.read('projects.json')
        registry['projects'].append({'id': 'z', 'name': 'Other project',
            'repository_path': str(self.project_dir), 'working_directory': str(self.project_dir)})
        self.put('projects.json', registry)
        conflicting = self.read(f'tasks/p/{dependency}.json')
        conflicting.update(project='z', status='done', evidence=['verified completion proof'])
        self.put(f'tasks/z/{dependency}.json', conflicting)
        checked = self.evaluate()
        self.assertFalse(checked['allowed'], checked)
        sent = self.send()
        self.assertFalse(sent['allowed'], sent)
        self.assertEqual([], self.inbox())
        self.assertTrue((self.root / sent['audit_path']).is_file())
        self.assertEqual('pending', self.read(self.task_path)['status'])


    def test_malformed_parent_membership_cannot_hide_story_prerequisites(self):
        for parent in ({'parent_key': ['S']}, {'parent_key': 42}, {'parent': 'S'},
                       {'parent': {'key': []}}, {'parent': {'key': 42}}):
            with self.subTest(parent=parent):
                self.epic([{'key': 'A', 'projects': ['p'], **parent},
                           {'key': 'S', 'depends_on': ['B']},
                           {'key': 'B', 'local_status': 'pending'}])
                checked = self.evaluate()
                self.assertFalse(checked['allowed'], checked)
                sent = self.send()
                self.assertFalse(sent['allowed'], sent)
                self.assertEqual([], self.inbox())

    def test_malformed_task_membership_cannot_erase_owning_context(self):
        original = self.read(self.task_path)
        for membership in ({'issue_key': 42}, {'issue_keys': 'A'},
                           {'issue_keys': ['A', 42]}, {'epic_key': ['E']}):
            with self.subTest(membership=membership):
                self.put(self.task_path, {**original, **membership})
                checked = self.evaluate()
                self.assertFalse(checked['allowed'], checked)
                sent = self.send()
                self.assertFalse(sent['allowed'], sent)
                self.assertEqual([], self.inbox())

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, str(Path(dispatch_gate.__file__)), '--root', str(self.root),
                               *arguments], capture_output=True, text=True)

    def assert_code_written_denial(self, result):
        self.assertFalse(result['allowed'], result)
        self.assertTrue(result['reasons'], result)
        audit = self.read(result['audit_path'])
        self.assertFalse(audit['allowed'], audit)
        audit_without_root = json.dumps({k: v for k, v in audit.items() if k != 'root'})
        root_forms = (str(self.root), str(self.root.resolve()))
        for text in result['reasons'] + audit['reasons'] + [audit_without_root]:
            for fragment in LIBRARY_TEXT_FRAGMENTS + root_forms:
                self.assertNotIn(fragment, text)

    def denied_outcomes(self):
        evaluated = self.evaluate()
        sent = self.send()
        self.assertEqual([], self.inbox())
        return evaluated, sent

    def assert_check_and_dispatch_deny_in_code_written_text(self):
        evaluated, sent = self.denied_outcomes()
        self.assertFalse(evaluated['allowed'], evaluated)
        self.assertTrue(evaluated['reasons'])
        for text in evaluated['reasons']:
            for fragment in LIBRARY_TEXT_FRAGMENTS + (str(self.root), str(self.root.resolve())):
                self.assertNotIn(fragment, text)
        self.assert_code_written_denial(sent)

    # S2-F1

    def test_scope_that_is_not_a_json_object_gives_structured_denial_through_the_cli(self):
        for value in ([], None, 'text', 7):
            with self.subTest(scope=value):
                self.put('epics/E/scope.json', value)
                evaluated = self.evaluate()
                self.assertFalse(evaluated['allowed'], evaluated)
                checked = self.run_cli('check', '--task', self.task_path)
                self.assertNotEqual(0, checked.returncode)
                self.assertNotIn('Traceback', checked.stderr)
                result = json.loads(checked.stdout)
                self.assertFalse(result['allowed'], result)
                self.assertTrue(result['reasons'], result)
                self.assertFalse(self.read(result['audit_path'])['allowed'])
                dispatched = self.run_cli('dispatch', '--task', self.task_path, '--worker-id', self.endpoint)
                self.assertNotEqual(0, dispatched.returncode)
                self.assertNotIn('Traceback', dispatched.stderr)
                denied = json.loads(dispatched.stdout)
                self.assertFalse(denied['allowed'], denied)
                self.assertFalse(self.read(denied['audit_path'])['allowed'])
                self.assertEqual([], self.inbox())

    def test_scope_that_is_not_a_json_object_denies_validation_of_an_earlier_receipt(self):
        sent = self.send()
        self.assertTrue(sent['allowed'], sent)
        self.put('epics/E/scope.json', [])
        validated = self.validate(sent)
        self.assertFalse(validated['allowed'], validated)
        self.assertTrue((self.root / validated['audit_path']).is_file())

    # S2-F2

    def test_invalid_json_and_encoding_in_a_record_deny_without_decoder_text(self):
        record = self.root / 'epics/E/progress.json'
        for name, content in (('invalid json', b'{not-json'), ('invalid encoding', b'\xff\xfe\x00bad'),
                              ('truncated', b'{"generated_at": ')):
            with self.subTest(case=name):
                record.write_bytes(content)
                try:
                    self.assert_check_and_dispatch_deny_in_code_written_text()
                finally:
                    record.unlink()

    def test_unreadable_record_denies_without_os_text_or_path(self):
        record = self.put('epics/E/progress.json', {'generated_at': 'now'})
        original = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path.name == record.name:
                raise PermissionError(13, 'Permission denied', str(path))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', unreadable):
            self.assert_check_and_dispatch_deny_in_code_written_text()

    def test_missing_key_in_projection_denies_without_key_text(self):
        with patch.object(dispatch_gate, 'build_dashboard', lambda root: {'warnings': []}):
            self.assert_check_and_dispatch_deny_in_code_written_text()
            self.assertNotIn("'issues'", self.evaluate()['reasons'])

    def test_wrong_typed_projection_field_denies_without_type_error_text(self):
        with patch.object(dispatch_gate, 'build_dashboard', lambda root: {'warnings': [], 'issues': None}):
            self.assert_check_and_dispatch_deny_in_code_written_text()

    def test_library_value_error_from_the_projection_is_a_fixed_structure_denial(self):
        detail = f'{self.root}/secret/location library detail'

        def failing_projection(root):
            raise ValueError(detail)
        with patch.object(dispatch_gate, 'build_dashboard', failing_projection):
            self.assertEqual(['Mailbox record structure is malformed'], self.evaluate()['reasons'])
            sent = self.send()
            self.assertEqual([], self.inbox())
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                dispatch_gate.main(['--root', str(self.root), 'check', '--task', self.task_path])
        result = json.loads(printed.getvalue())
        self.assertEqual(['Mailbox record structure is malformed'], result['reasons'])
        for record_text in (printed.getvalue(), json.dumps(self.read(result['audit_path'])),
                            json.dumps(self.read(sent['audit_path']))):
            self.assertNotIn('library detail', record_text)
            self.assertNotIn('/secret/location', record_text)

    def test_invalid_json_receipt_or_envelope_denies_validation_without_decoder_text(self):
        sent = self.send()
        receipt = self.root / sent['audit_path']
        envelope = self.root / sent['message_path']
        receipt_bytes, envelope_bytes = receipt.read_bytes(), envelope.read_bytes()
        for target, original in ((receipt, receipt_bytes), (envelope, envelope_bytes)):
            for name, content in (('invalid json', b'{broken'), ('invalid encoding', b'\xff\xfe\x00bad')):
                with self.subTest(record=target.name, case=name):
                    target.write_bytes(content)
                    try:
                        self.assert_code_written_denial(self.validate(sent))
                    finally:
                        target.write_bytes(original)

    def test_unreadable_receipt_denies_validation_without_os_text_or_path(self):
        sent = self.send()
        receipt_name = Path(sent['audit_path']).name
        original = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path.name == receipt_name:
                raise PermissionError(13, 'Permission denied', str(path))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', unreadable):
            self.assert_code_written_denial(self.validate(sent))

    def test_malformed_uuid_is_not_canonical_without_raising(self):
        for value in ('not-a-uuid', '', '1234', str(uuid.uuid4()).upper(), '{' + str(uuid.uuid4()) + '}',
                      None, 7, ['x']):
            with self.subTest(value=value):
                self.assertIs(False, dispatch_gate._uuid(value))
        self.assertIs(True, dispatch_gate._uuid(self.target))

    def test_malformed_task_uuid_keeps_the_gate_target_reason(self):
        self.put('tasks/p/not-a-uuid.json', {'id': 'not-a-uuid', 'project': 'p'})
        result = dispatch_gate.evaluate(self.root, 'tasks/p/not-a-uuid.json')
        self.assertFalse(result['allowed'], result)
        self.assertTrue(any('Target must be tasks/' in reason for reason in result['reasons']), result)
        self.assertFalse(any('badly formed' in reason for reason in result['reasons']), result)

    def test_malformed_message_id_keeps_the_gate_reason_and_is_audited(self):
        result = self.send(message_id='not-a-uuid')
        self.assertFalse(result['allowed'], result)
        self.assertIn('Message ID must be a canonical UUID', result['reasons'])
        self.assert_code_written_denial(result)
        self.assertEqual([], self.inbox())

    def test_malformed_receipt_uuid_keeps_the_gate_reason(self):
        result = self.send()
        value = self.read(result['audit_path'])
        self.put(result['audit_path'], {**value, 'id': 'not-a-receipt-uuid'})
        validated = self.validate(result)
        self.assertIn('Dispatch receipt ID must be a canonical UUID', validated['reasons'])
        self.assert_code_written_denial(validated)

    def test_malformed_envelope_uuid_keeps_the_gate_reason(self):
        self.put('inbox/p/not-a-uuid.json', {'id': 'not-a-uuid'})
        validated = dispatch_gate.validate(self.root, 'inbox/p/not-a-uuid.json', self.endpoint)
        self.assertTrue(any('Worker validates an existing UUID inbox envelope only' in reason
                            for reason in validated['reasons']), validated)
        self.assert_code_written_denial(validated)

    def test_gate_own_value_error_reasons_stay_verbatim(self):
        record = self.root / 'epics/E/progress.json'
        record.write_text('{"generated_at": NaN}')
        self.assertEqual(['Non-finite JSON: NaN'], self.evaluate()['reasons'])
        record.write_text('{"generated_at": 1, "generated_at": 2}')
        self.assertEqual(['Duplicate JSON key in epics/E/progress.json: generated_at'],
                         self.evaluate()['reasons'])

    # R2-S2-F1 and R2-S2-F2 (amendment 15)

    def alias_of_root(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        alias = Path(holder.name) / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        return alias

    def assert_output_and_audit_expose_no_path(self, printed, hidden_everywhere, hidden_outside_root_field):
        result = json.loads(printed)
        audit = self.read(result['audit_path'])
        for text in (printed, json.dumps(audit)):
            for path in hidden_everywhere:
                self.assertNotIn(path, text)
        for text in (json.dumps({k: v for k, v in result.items() if k != 'root'}),
                     json.dumps({k: v for k, v in audit.items() if k != 'root'})):
            for path in hidden_outside_root_field:
                self.assertNotIn(path, text)
        return result

    def test_absolute_task_path_through_a_root_alias_is_outside_the_mailbox_root(self):
        alias = self.alias_of_root()
        through_alias = str(alias / self.task_path)
        hidden = (str(alias), str(self.root.resolve()))
        self.assertEqual(['Path is outside mailbox root'], dispatch_gate.evaluate(self.root, through_alias)['reasons'])
        checked = self.run_cli('check', '--task', through_alias)
        self.assertNotEqual(0, checked.returncode)
        self.assertNotIn('Traceback', checked.stderr)
        result = self.assert_output_and_audit_expose_no_path(checked.stdout, (str(alias),), hidden)
        self.assertEqual(['Path is outside mailbox root'], result['reasons'])
        sent = dispatch_gate.dispatch(self.root, through_alias, self.endpoint)
        self.assertEqual(['Path is outside mailbox root'], sent['reasons'])
        self.assertEqual([], self.inbox())
        dispatched = self.run_cli('dispatch', '--task', through_alias, '--worker-id', self.endpoint)
        self.assertNotEqual(0, dispatched.returncode)
        result = self.assert_output_and_audit_expose_no_path(dispatched.stdout, (str(alias),), hidden)
        self.assertEqual(['Path is outside mailbox root'], result['reasons'])
        self.assertEqual([], self.inbox())

    def test_absolute_message_path_through_a_root_alias_is_outside_the_mailbox_root(self):
        sent = self.send()
        alias = self.alias_of_root()
        through_alias = str(alias / sent['message_path'])
        hidden = (str(alias), str(self.root.resolve()))
        validated = dispatch_gate.validate(self.root, through_alias, self.endpoint)
        self.assertEqual(['Path is outside mailbox root'], validated['reasons'])
        self.assertFalse(validated['allowed'])
        cli = self.run_cli('validate', '--message', through_alias, '--worker-id', self.endpoint)
        self.assertNotEqual(0, cli.returncode)
        self.assertNotIn('Traceback', cli.stderr)
        result = self.assert_output_and_audit_expose_no_path(cli.stdout, (str(alias),), hidden)
        self.assertEqual(['Path is outside mailbox root'], result['reasons'])

    def test_integer_literal_over_the_digit_limit_is_invalid_json_in_a_task_or_other_record(self):
        oversized = '9' * 5000
        task_text = (self.root / self.task_path).read_text()
        records = {self.task_path: task_text[:-1] + ', "big": ' + oversized + '}',
                   'epics/E/progress.json': '{"generated_at": ' + oversized + '}'}
        for relative, text in records.items():
            with self.subTest(record=relative):
                path = self.root / relative
                original = path.read_text() if path.exists() else None
                path.write_text(text)
                try:
                    expected = ['Invalid JSON: ' + relative]
                    self.assertEqual(expected, self.evaluate()['reasons'])
                    checked = self.run_cli('check', '--task', self.task_path)
                    printed = checked.stdout
                    for fragment in ('Exceeds the limit', 'set_int_max_str_digits', 'Traceback'):
                        self.assertNotIn(fragment, printed + checked.stderr)
                    result = self.assert_output_and_audit_expose_no_path(
                        printed, (), (str(self.root), str(self.root.resolve())))
                    self.assertEqual(expected, result['reasons'])
                    for fragment in ('Exceeds the limit', 'set_int_max_str_digits'):
                        self.assertNotIn(fragment, json.dumps(self.read(result['audit_path'])))
                    sent = self.send()
                    self.assertEqual(expected, sent['reasons'])
                    self.assertEqual([], self.inbox())
                finally:
                    if original is None:
                        path.unlink()
                    else:
                        path.write_text(original)

    # S2-F3 through the server change (contract 5)

    def test_malformed_delivery_record_does_not_deny_an_otherwise_dispatchable_task(self):
        for name, content in (('missing stage', '{}'), ('unsupported stage', '{"delivery_stage": "nonsense"}'),
                              ('invalid json', '{not-json')):
            with self.subTest(case=name):
                record = self.root / 'deliveries' / f'{self.target}.json'
                record.parent.mkdir(exist_ok=True)
                record.write_text(content)
                try:
                    result = self.evaluate()
                finally:
                    record.unlink()
                self.assertTrue(result['allowed'], result)
                self.assertFalse(any('Authoritative graph warning' in reason for reason in result['reasons']))

    def test_malformed_worked_so_far_does_not_deny_an_otherwise_dispatchable_task(self):
        for name, content in (('invalid json', '{not-json'), ('wrong shape', '[]')):
            with self.subTest(case=name):
                record = self.root / 'jira-sync' / 'worked-so-far.json'
                record.parent.mkdir(exist_ok=True)
                record.write_text(content)
                try:
                    sent = self.send()
                finally:
                    record.unlink()
                self.assertTrue(sent['allowed'], sent)
                self.assertFalse(any('Authoritative graph warning' in reason for reason in sent['reasons']))
                self.inbox()[0].unlink()
                for stale in self.audit():
                    stale.unlink()

    # protect: authoritative warnings still deny

    def test_warning_from_an_authoritative_record_still_denies(self):
        sibling = str(uuid.uuid4())
        mutations = {
            'task': lambda: self.put(f'tasks/p/{sibling}.json', {'id': sibling, 'project': 5}),
            'issue': lambda: self.put('epics/E/issues.json', [{'key': 'A', 'projects': ['p']}, {'key': 5}]),
            'scope': lambda: self.put('epics/E/scope.json', {'epic_key': 5, 'issue_keys': ['A']}),
            'registry': lambda: self.put('projects/bad.json', {'id': ''}),
        }
        for name, mutate in mutations.items():
            with self.subTest(record=name):
                self.tearDown()
                self.setUp()
                mutate()
                evaluated, sent = self.denied_outcomes()
                self.assertTrue(any('Authoritative graph warning' in reason for reason in evaluated['reasons']),
                                evaluated)
                self.assertFalse(sent['allowed'], sent)

    # protect: denial and publication

    def test_denied_dispatch_publishes_no_allowed_record(self):
        dependency = self.task()
        self.update_task(depends_on=[dependency])
        sent = self.send()
        self.assertFalse(sent['allowed'], sent)
        self.assertEqual([], self.inbox())
        records = [self.read(path.relative_to(self.root)) for path in self.audit()]
        self.assertTrue(records)
        self.assertFalse(any(record['allowed'] for record in records))

    def test_publication_never_overwrites_an_existing_record_and_leaves_no_temporary_file(self):
        relative = Path('dispatch-gates') / self.target / 'existing.json'
        dispatch_gate._publish(self.root, relative, {'first': True})
        before = (self.root / relative).read_bytes()
        with self.assertRaises(FileExistsError):
            dispatch_gate._publish(self.root, relative, {'second': True})
        self.assertEqual(before, (self.root / relative).read_bytes())
        self.assertEqual([relative.name], sorted(p.name for p in (self.root / relative.parent).iterdir()))

    def test_successful_dispatch_leaves_no_temporary_publication_file(self):
        self.assertTrue(self.send()['allowed'])
        leftovers = [p for p in self.root.rglob('*.tmp')]
        self.assertEqual([], leftovers)


LIBRARY_TEXT_FRAGMENTS = ('Expecting', 'Errno', 'codec', 'Traceback', 'badly formed', 'Permission denied',
                          'No such file', 'NoneType', 'JSONDecodeError', 'line 1 column')


if __name__ == '__main__':
    unittest.main()
