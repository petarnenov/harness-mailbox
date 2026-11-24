import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from reconcile_sessions import main

OLD, NEW, OTHER = 'old-session', 'new-session', 'other-session'


class ReconcileSessionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.register(OLD, 'proj')
        self.lock = self.task('t1', OLD)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def register(self, session_id, project):
        return self.write(f'sessions/{project}--{session_id}.json',
                          {'role': 'worker', 'project': project, 'session_id': session_id})

    def task(self, task_id, owner, **fields):
        self.write(f'tasks/proj/{task_id}.json', {'id': task_id, 'status': 'in_progress', **fields})
        return self.write(f'tasks/proj/{task_id}.lock/owner.json', {'owner_session_id': owner}).parent

    def run_command(self, *args):
        return main(['--root', str(self.root), *args])

    def test_inventory_lists_locks_and_unread_messages_and_requires_a_probe(self):
        self.write('inbox/proj/m1.json', {'id': 'm1', 'intended_endpoint': OLD})
        self.write('inbox/proj/m2.json', {'id': 'm2', 'intended_endpoint': OTHER})
        (entry,) = self.run_command('inventory')
        self.assertEqual(entry['locks'], ['tasks/proj/t1.lock'])
        self.assertEqual(entry['unread_messages'], ['inbox/proj/m1.json'])
        self.assertEqual(entry['liveness'], 'probe required')

    def test_inventory_flags_a_registration_without_project_instead_of_failing(self):
        self.write('sessions/x--broken.json', {'role': 'worker', 'session_id': 'broken'})
        broken = next(e for e in self.run_command('inventory') if e['session_id'] == 'broken')
        self.assertEqual(broken['liveness'], 'malformed registration')

    def test_retire_archives_registration_and_messages_and_orphans_tasks_without_releasing_locks(self):
        self.write('inbox/proj/m1.json', {'id': 'm1', 'to_endpoint': OLD})
        result = self.run_command('retire', '--session', OLD, '--evidence', 'not listed; PING unanswered 60s')
        self.assertFalse((self.root / 'sessions/proj--old-session.json').exists())
        self.assertTrue((self.root / 'archive/sessions-removed/proj--old-session.json').exists())
        self.assertTrue((self.root / 'archive/proj/m1.json').exists())
        self.assertTrue((self.lock / 'owner.json').exists())
        orphan = json.loads((self.root / 'orphans/t1.json').read_text())
        self.assertEqual(orphan['previous_owner'], {'owner_session_id': OLD})
        self.assertEqual(result['orphaned_tasks'], ['t1'])
        self.assertTrue(list((self.root / 'decisions/sessions').glob('*.json')))

    def test_retire_reports_uncommitted_changes_in_a_named_worktree(self):
        worktree = self.root / 'wt'
        worktree.mkdir()
        subprocess.run(['git', 'init', '-q', str(worktree)], check=True)
        (worktree / 'dirty.txt').write_text('x')
        self.task('t1', OLD, evidence=[f'implementation in {worktree}'])
        self.run_command('retire', '--session', OLD, '--evidence', 'unreachable')
        orphan = json.loads((self.root / 'orphans/t1.json').read_text())
        self.assertEqual(orphan['worktrees'], [{'path': str(worktree), 'uncommitted_changes': 1}])

    def test_retire_refuses_empty_evidence_or_an_unknown_session(self):
        with self.assertRaisesRegex(ValueError, 'evidence'):
            self.run_command('retire', '--session', OLD, '--evidence', ' ')
        with self.assertRaisesRegex(ValueError, 'found 0'):
            self.run_command('retire', '--session', 'ghost', '--evidence', 'x')

    def test_takeover_moves_the_lock_with_succession_and_clears_the_orphan(self):
        self.run_command('retire', '--session', OLD, '--evidence', 'unreachable')
        self.register(NEW, 'proj')
        result = self.run_command('takeover', '--task', 't1', '--to', NEW, '--evidence', 'worktree clean, continuing T3')
        owner = json.loads((self.lock / 'owner.json').read_text())
        self.assertEqual(owner['owner_session_id'], NEW)
        self.assertEqual(owner['succession'][0]['from'], OLD)
        self.assertEqual(result['succession'][0]['to'], NEW)
        self.assertFalse((self.root / 'orphans/t1.json').exists())
        self.assertTrue((self.root / 'archive/orphans-resolved/t1.json').exists())

    def test_takeover_refuses_unorphaned_tasks_other_projects_and_a_returned_owner(self):
        self.register(NEW, 'proj')
        with self.assertRaisesRegex(ValueError, 'not orphaned'):
            self.run_command('takeover', '--task', 't1', '--to', NEW, '--evidence', 'x')
        self.run_command('retire', '--session', OLD, '--evidence', 'unreachable')
        self.register(OTHER, 'elsewhere')
        with self.assertRaisesRegex(ValueError, 'not proj'):
            self.run_command('takeover', '--task', 't1', '--to', OTHER, '--evidence', 'x')
        self.register(OLD, 'proj')
        with self.assertRaisesRegex(ValueError, 'registered again'):
            self.run_command('takeover', '--task', 't1', '--to', NEW, '--evidence', 'x')

    def test_retire_by_the_alternate_id_orphans_the_tasks_locked_under_the_other_id(self):
        for held_id, retired_by in (('sess-1', 'agent-1'), ('agent-1', 'sess-1')):
            with self.subTest(held=held_id, retired_by=retired_by):
                self.write('sessions/proj--dual.json', {'role': 'worker', 'project': 'proj',
                                                       'session_id': 'sess-1', 'agent_id': 'agent-1'})
                self.task('t2', held_id)
                result = self.run_command('retire', '--session', retired_by, '--evidence', 'unreachable')
                self.assertEqual(result['orphaned_tasks'], ['t2'])
                orphan = json.loads((self.root / 'orphans/t2.json').read_text())
                self.assertEqual(orphan['previous_owner'], {'owner_session_id': held_id})
                self.assertTrue((self.root / 'tasks/proj/t2.lock/owner.json').exists())
                (self.root / 'archive/sessions-removed/proj--dual.json').unlink()
                (self.root / 'orphans/t2.json').unlink()

    def test_retire_by_either_id_archives_the_messages_addressed_to_both_ids(self):
        for retired_by in ('sess-1', 'agent-1'):
            with self.subTest(retired_by=retired_by):
                self.write('sessions/proj--dual.json', {'role': 'worker', 'project': 'proj',
                                                       'session_id': 'sess-1', 'agent_id': 'agent-1'})
                self.write('inbox/proj/to-session.json', {'id': 'a', 'to_session_id': 'sess-1'})
                self.write('inbox/proj/to-agent.json', {'id': 'b', 'intended_endpoint': 'agent-1'})
                self.write('inbox/proj/to-other.json', {'id': 'c', 'to_endpoint': OTHER})
                self.write('archive/proj/earlier.json', {'id': 'earlier'})
                result = self.run_command('retire', '--session', retired_by, '--evidence', 'unreachable')
                self.assertEqual(sorted(result['archived_messages']),
                                 ['archive/proj/to-agent.json', 'archive/proj/to-session.json'])
                self.assertEqual(sorted(p.name for p in (self.root / 'inbox/proj').glob('*.json')),
                                 ['to-other.json'])
                self.assertEqual(json.loads((self.root / 'archive/proj/to-session.json').read_text())['id'], 'a')
                self.assertEqual(json.loads((self.root / 'archive/proj/to-agent.json').read_text())['id'], 'b')
                self.assertEqual(json.loads((self.root / 'archive/proj/earlier.json').read_text())['id'], 'earlier')
                for leftover in ('archive/sessions-removed/proj--dual.json', 'inbox/proj/to-other.json',
                                 'archive/proj/to-session.json', 'archive/proj/to-agent.json'):
                    (self.root / leftover).unlink()

    def test_retire_by_the_given_id_still_orphans_its_own_locks(self):
        result = self.run_command('retire', '--session', OLD, '--evidence', 'unreachable')
        self.assertEqual(result['orphaned_tasks'], ['t1'])

    def retire_and_take_over(self, task_id, retired, successor):
        self.run_command('retire', '--session', retired, '--evidence', 'unreachable')
        self.register(successor, 'proj')
        return self.run_command('takeover', '--task', task_id, '--to', successor, '--evidence', 'continuing')

    def test_a_second_takeover_keeps_the_first_archived_orphan_record(self):
        first = self.retire_and_take_over('t1', OLD, NEW)
        second = self.retire_and_take_over('t1', NEW, OTHER)
        archive = self.root / 'archive/orphans-resolved'
        records = sorted(archive.glob('*.json'))
        self.assertEqual(len(records), 2)
        retired_sessions = {json.loads(path.read_text())['retired_session'] for path in records}
        self.assertEqual(retired_sessions, {OLD, NEW})
        first_entry, = first['succession']
        self.assertEqual(first_entry['orphan_archive'], 'archive/orphans-resolved/t1.json')
        self.assertEqual(json.loads((archive / 't1.json').read_text())['retired_session'], OLD)
        used = second['succession'][-1]['orphan_archive']
        self.assertNotEqual(used, 'archive/orphans-resolved/t1.json')
        self.assertEqual(json.loads((self.root / used).read_text())['retired_session'], NEW)
        self.assertEqual(second['succession'][-1]['orphan_record'], 'orphans/t1.json')
        self.assertEqual(second['succession'][0], first_entry)
        owner = json.loads((self.lock / 'owner.json').read_text())
        self.assertEqual(owner['succession'], second['succession'])
        self.assertFalse((self.root / 'orphans/t1.json').exists())

    def test_first_takeover_uses_the_task_named_archive_file(self):
        result = self.retire_and_take_over('t1', OLD, NEW)
        entry, = result['succession']
        self.assertEqual(entry['orphan_archive'], 'archive/orphans-resolved/t1.json')
        self.assertEqual(entry['orphan_record'], 'orphans/t1.json')
        self.assertEqual([p.name for p in (self.root / 'archive/orphans-resolved').iterdir()], ['t1.json'])

    def test_takeover_tolerates_a_succession_entry_without_an_archive_name(self):
        self.run_command('retire', '--session', OLD, '--evidence', 'unreachable')
        owner_file = self.lock / 'owner.json'
        legacy = {'from': 'x', 'to': OLD, 'at': 't', 'evidence': 'e', 'orphan_record': 'orphans/t1.json'}
        owner = json.loads(owner_file.read_text())
        owner['succession'] = [legacy]
        owner_file.write_text(json.dumps(owner))
        self.register(NEW, 'proj')
        result = self.run_command('takeover', '--task', 't1', '--to', NEW, '--evidence', 'continuing')
        self.assertEqual(result['succession'][0], legacy)
        self.assertIn('orphan_archive', result['succession'][1])


class CommandLineRefusalTest(unittest.TestCase):
    SCRIPT = Path(__file__).with_name('reconcile_sessions.py')
    LEAK_MARKERS = ('Traceback', 'Expecting', 'Errno', 'codec', 'Unterminated', 'File "')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.write('sessions/proj--old-session.json', {'role': 'worker', 'project': 'proj', 'session_id': OLD})
        self.write('tasks/proj/t1.json', {'id': 't1', 'status': 'in_progress'})
        self.write('tasks/proj/t1.lock/owner.json', {'owner_session_id': OLD})

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def run_cli(self, *args, path=None):
        environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
        if path is not None:
            environment['PATH'] = path
        return subprocess.run([sys.executable, str(self.SCRIPT), '--root', str(self.root), *args],
                              capture_output=True, text=True, env=environment)

    def assert_refused(self, completed):
        self.assertNotEqual(completed.returncode, 0)
        report = completed.stdout + completed.stderr
        self.assertEqual(len(report.strip().splitlines()), 1, report)
        for marker in (*self.LEAK_MARKERS, self.temp.name, str(self.root)):
            self.assertNotIn(marker, report)

    def test_malformed_registration_refuses_the_inventory(self):
        for name, content in {'invalid json': '{bad', 'not an object': '[]', 'not utf-8': b'\xff\xfe'}.items():
            with self.subTest(name):
                broken = self.root / 'sessions/x--broken.json'
                if isinstance(content, bytes):
                    broken.write_bytes(content)
                else:
                    broken.write_text(content)
                self.assert_refused(self.run_cli('inventory'))

    def test_malformed_lock_owner_refuses_the_inventory(self):
        self.write('tasks/proj/t1.lock/owner.json', '{bad')
        self.assert_refused(self.run_cli('inventory'))

    def test_malformed_task_record_refuses_the_retirement(self):
        self.write('tasks/proj/t1.json', '{bad')
        self.assert_refused(self.run_cli('retire', '--session', OLD, '--evidence', 'unreachable'))

    def test_unknown_session_refuses_the_retirement(self):
        self.assert_refused(self.run_cli('retire', '--session', 'ghost', '--evidence', 'unreachable'))

    def test_empty_evidence_refuses_the_retirement(self):
        self.assert_refused(self.run_cli('retire', '--session', OLD, '--evidence', ' '))

    def test_an_occupied_archive_refuses_the_retirement_without_naming_its_path(self):
        self.write('archive/sessions-removed/proj--old-session.json', {})
        self.assert_refused(self.run_cli('retire', '--session', OLD, '--evidence', 'unreachable'))
        self.assertTrue((self.root / 'sessions/proj--old-session.json').exists())

    def test_unknown_task_refuses_the_takeover(self):
        self.write('sessions/proj--new-session.json', {'role': 'worker', 'project': 'proj', 'session_id': NEW})
        self.assert_refused(self.run_cli('takeover', '--task', 'ghost', '--to', NEW, '--evidence', 'x'))

    def test_malformed_orphan_record_refuses_the_takeover(self):
        self.write('sessions/proj--new-session.json', {'role': 'worker', 'project': 'proj', 'session_id': NEW})
        for name, content in {'invalid json': '{bad', 'missing fields': {'task_id': 't1'}, 'not an object': []}.items():
            with self.subTest(name):
                self.write('orphans/t1.json', content)
                self.assert_refused(self.run_cli('takeover', '--task', 't1', '--to', NEW, '--evidence', 'x'))

    def test_unknown_new_session_refuses_the_takeover(self):
        completed = self.run_cli('retire', '--session', OLD, '--evidence', 'unreachable')
        self.assertEqual(completed.returncode, 0)
        self.assert_refused(self.run_cli('takeover', '--task', 't1', '--to', 'ghost', '--evidence', 'x'))

    def test_missing_git_refuses_the_retirement_of_a_session_with_a_worktree(self):
        worktree = self.root / 'wt'
        (worktree / '.git').mkdir(parents=True)
        self.write('tasks/proj/t1.json', {'id': 't1', 'evidence': [f'work in {worktree}']})
        empty_path = self.root / 'no-binaries'
        empty_path.mkdir()
        self.assert_refused(self.run_cli('retire', '--session', OLD, '--evidence', 'unreachable',
                                         path=str(empty_path)))

    def test_a_worktree_git_cannot_read_reports_unknown_changes(self):
        worktree = self.root / 'wt'
        (worktree / '.git').mkdir(parents=True)
        self.write('tasks/proj/t1.json', {'id': 't1', 'evidence': [f'work in {worktree}']})
        completed = self.run_cli('retire', '--session', OLD, '--evidence', 'unreachable')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        orphan = json.loads((self.root / 'orphans/t1.json').read_text())
        self.assertEqual(orphan['worktrees'], [{'path': str(worktree), 'uncommitted_changes': None}])

    def test_successful_commands_exit_zero_with_the_json_report(self):
        completed = self.run_cli('inventory')
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)[0]['session_id'], OLD)


if __name__ == '__main__':
    unittest.main()
