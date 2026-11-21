import json
import tempfile
import unittest
from pathlib import Path

from record_issue import main


class RecordIssueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'epics' / 'EPIC-1' / 'issues.json'
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({'issues': [
            {'key': 'GEO-1', 'local_status': 'in_progress', 'blockers': ['depends on GEO-2', 'needs token']},
            {'key': 'GEO-2', 'local_status': 'in_progress'}]}))

    def tearDown(self):
        self.temp.cleanup()

    def issue(self, key):
        return next(i for i in json.loads(self.path.read_text())['issues'] if i['key'] == key)

    def run_command(self, *args):
        main(['--root', str(self.root), '--epic', 'EPIC-1', *args])

    def test_prerequisites_become_depends_on_and_replace_textual_dependency_blockers(self):
        self.run_command('--issue', 'GEO-1', 'depends-on', 'GEO-2')
        issue = self.issue('GEO-1')
        self.assertEqual(issue['depends_on'], ['GEO-2'])
        self.assertEqual(issue['blockers'], ['needs token'])

    def test_verified_records_passed_status_and_evidence(self):
        self.run_command('--issue', 'GEO-2', 'verified', '--evidence', 'PR #1 merge abc', 'report r1')
        issue = self.issue('GEO-2')
        self.assertEqual(issue['local_status'], 'verified_done')
        self.assertEqual(issue['verification']['status'], 'passed')
        self.assertEqual(issue['evidence'], ['PR #1 merge abc', 'report r1'])

    def write_task(self, task_id, **fields):
        path = self.root / 'tasks' / 'proj' / f'{task_id}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'id': task_id, **fields}))
        return path

    def test_verified_is_refused_while_a_linked_task_is_still_open(self):
        self.write_task('t-open', issue_key='GEO-2', status='in_progress', evidence=['x'])
        with self.assertRaisesRegex(ValueError, "t-open: status 'in_progress'"):
            self.run_command('--issue', 'GEO-2', 'verified', '--evidence', 'r1')
        self.assertEqual(self.issue('GEO-2')['local_status'], 'in_progress')

    def test_verified_is_refused_for_done_task_without_evidence_or_with_lock(self):
        self.write_task('t-bare', issue_keys=['GEO-2'], status='done')
        locked = self.write_task('t-locked', issue_keys=['GEO-2'], status='done', evidence=['x'])
        locked.with_suffix('.lock').mkdir()
        with self.assertRaises(ValueError) as refused:
            self.run_command('--issue', 'GEO-2', 'verified', '--evidence', 'r1')
        self.assertIn('t-bare: done without evidence', str(refused.exception))
        self.assertIn('t-locked: lock still held', str(refused.exception))

    def test_verified_checks_tasks_listed_on_the_issue_and_missing_records(self):
        issues = json.loads(self.path.read_text())
        issues['issues'][1]['local_task_ids'] = ['t-gone']
        self.path.write_text(json.dumps(issues))
        with self.assertRaisesRegex(ValueError, 't-gone: task record missing'):
            self.run_command('--issue', 'GEO-2', 'verified', '--evidence', 'r1')

    def test_verified_passes_when_every_linked_task_is_closed(self):
        self.write_task('t-done', issue_key='GEO-2', status='done', evidence=['merge abc'])
        self.write_task('t-other', issue_key='GEO-1', status='in_progress')
        self.run_command('--issue', 'GEO-2', 'verified', '--evidence', 'r1')
        self.assertEqual(self.issue('GEO-2')['local_status'], 'verified_done')

    def test_verified_accepts_a_cancelled_task_only_with_evidence(self):
        self.write_task('t-bare-cancel', issue_key='GEO-2', status='cancelled')
        with self.assertRaisesRegex(ValueError, 't-bare-cancel: cancelled without evidence'):
            self.run_command('--issue', 'GEO-2', 'verified', '--evidence', 'r1')
        self.write_task('t-bare-cancel', issue_key='GEO-2', status='cancelled', evidence=['user decision d1'])
        self.run_command('--issue', 'GEO-2', 'verified', '--evidence', 'r1')
        self.assertEqual(self.issue('GEO-2')['local_status'], 'verified_done')

    def test_rejects_unknown_issue_self_dependency_and_empty_evidence(self):
        with self.assertRaises(ValueError):
            self.run_command('--issue', 'GEO-9', 'depends-on', 'GEO-2')
        with self.assertRaises(ValueError):
            self.run_command('--issue', 'GEO-1', 'depends-on', 'GEO-1')
        with self.assertRaises(ValueError):
            self.run_command('--issue', 'GEO-2', 'verified', '--evidence', ' ')


if __name__ == '__main__':
    unittest.main()
