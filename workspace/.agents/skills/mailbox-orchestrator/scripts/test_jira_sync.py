import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jira_sync


class JiraSyncTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_start_assigns_and_targets_in_progress(self):
        request = jira_sync.queue(self.root, 'GEO-1', 'start')
        self.assertEqual('In Progress', request['target_status'])
        self.assertTrue(request['assign_current_user'])
        self.assertEqual([request], jira_sync.pending(self.root))

    def test_pr_and_verified_target_review_and_closed_without_assigning(self):
        review = jira_sync.queue(self.root, 'GEO-1', 'pr')
        closed = jira_sync.queue(self.root, 'GEO-1', 'verified')
        self.assertEqual('Development Peer Review', review['target_status'])
        self.assertEqual('Closed', closed['target_status'])
        self.assertFalse(review['assign_current_user'] or closed['assign_current_user'])

    def test_done_moves_request_to_audit_trail(self):
        request = jira_sync.queue(self.root, 'GEO-1', 'verified')
        jira_sync.done(self.root, request['id'], 'applied', 'Closed via transition 2')
        self.assertEqual([], jira_sync.pending(self.root))
        self.assertTrue((self.root / 'jira-sync' / 'done' / f"{request['id']}.json").exists())

    def test_done_requires_detail(self):
        request = jira_sync.queue(self.root, 'GEO-1', 'pr')
        with self.assertRaises(ValueError):
            jira_sync.done(self.root, request['id'], 'skipped', ' ')

    def write_issues(self, epic, *records):
        path = self.root / 'epics' / epic / 'issues.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'retrieved_at': 'earlier', 'issues': list(records)}))
        return path

    def statuses(self, path):
        return {r['key']: r['source_status'] for r in json.loads(path.read_text())['issues']}

    def test_applied_update_refreshes_the_source_status_snapshot_of_that_issue_only(self):
        issues = self.write_issues('E-1', {'key': 'GEO-1', 'source_status': 'To Do'},
                                   {'key': 'GEO-2', 'source_status': 'To Do'})
        request = jira_sync.queue(self.root, 'GEO-1', 'pr')
        jira_sync.done(self.root, request['id'], 'applied', 'transition 5')
        self.assertEqual({'GEO-1': 'Development Peer Review', 'GEO-2': 'To Do'}, self.statuses(issues))

    def test_skipped_update_leaves_the_snapshot_unchanged(self):
        issues = self.write_issues('E-1', {'key': 'GEO-1', 'source_status': 'Open'})
        request = jira_sync.queue(self.root, 'GEO-1', 'pr')
        jira_sync.done(self.root, request['id'], 'skipped', 'Story has no peer review status')
        self.assertEqual({'GEO-1': 'Open'}, self.statuses(issues))

    def test_applied_update_reaches_every_epic_listing_the_issue_and_tolerates_none(self):
        first = self.write_issues('E-1', {'key': 'GEO-1', 'source_status': 'To Do'})
        second = self.write_issues('E-2', {'key': 'GEO-1', 'source_status': 'To Do'})
        request = jira_sync.queue(self.root, 'GEO-1', 'verified')
        jira_sync.done(self.root, request['id'], 'applied', 'transition 6')
        self.assertEqual({'GEO-1': 'Closed'}, self.statuses(first))
        self.assertEqual({'GEO-1': 'Closed'}, self.statuses(second))
        unlisted = jira_sync.queue(self.root, 'GEO-9', 'start')
        jira_sync.done(self.root, unlisted['id'], 'applied', 'assigned, transition 3')

    def apply(self, issue, event, at, outcome='applied'):
        with mock.patch.object(jira_sync, 'now', return_value=at):
            request = jira_sync.queue(self.root, issue, event)
            return jira_sync.done(self.root, request['id'], outcome, 'recorded by test')

    def worked(self, issue, in_progress_at, closed_at):
        self.apply(issue, 'start', in_progress_at)
        self.apply(issue, 'verified', closed_at)

    def test_worklog_rounds_in_progress_to_closed_up_to_whole_hours(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:01+00:00')
        worklog = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.assertEqual('3h', worklog['time_spent'])
        self.assertEqual('2026-11-22T10:00:00.000+0000', worklog['started'])
        self.assertEqual('2026-11-22T12:00:01+00:00', worklog['closed_at'])
        self.assertEqual('applied start', worklog['in_progress_source'])

    def test_worklog_keeps_exact_hours_and_never_logs_less_than_one(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:00+00:00')
        self.worked('GEO-2', '2026-11-22T05:26:00+00:00', '2026-11-22T05:30:00+00:00')
        self.assertEqual('2h', jira_sync.queue(self.root, 'GEO-1', 'worklog')['time_spent'])
        self.assertEqual('1h', jira_sync.queue(self.root, 'GEO-2', 'worklog')['time_spent'])

    def test_worklog_leaves_out_the_time_its_agents_waited_for_an_answer(self):
        self.write_issues('E-1', {'key': 'GEO-1', 'task_ids': ['t-1']})
        for folder, kind, moment in (('outbox/proj', 'g1_request', '2026-11-22T11:00:00+00:00'),
                                     ('inbox/proj', 'g1_acceptance', '2026-11-22T14:30:00+00:00')):
            path = self.root / folder / f'{kind}.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'id': kind, 'type': kind, 'task_id': 't-1', 'created_at': moment}))
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T16:00:00+00:00')
        worklog = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.assertEqual('3h', worklog['time_spent'])
        self.assertEqual((6 * 3600, 3.5 * 3600), (worklog['elapsed_seconds'], worklog['waited_seconds']))

    def test_reopened_issue_gets_one_worklog_per_cycle(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T11:00:00+00:00')
        first = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        jira_sync.done(self.root, first['id'], 'applied', 'worklog 10001')
        self.worked('GEO-1', '2026-11-22T13:00:00+00:00', '2026-11-22T15:00:00+00:00')
        second = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.assertEqual(('1h', '2h'), (first['time_spent'], second['time_spent']))
        self.assertEqual('2026-11-22T13:00:00.000+0000', second['started'])

    def test_worklog_is_refused_without_a_verified_or_a_start_update(self):
        self.apply('GEO-1', 'verified', '2026-11-22T12:00:00+00:00')
        self.apply('GEO-2', 'start', '2026-11-22T10:00:00+00:00')
        for issue in ('GEO-1', 'GEO-2'):
            with self.assertRaises(ValueError):
                jira_sync.queue(self.root, issue, 'worklog')

    def skipped_start(self, issue, closed_at):
        self.apply(issue, 'start', '2026-11-22T10:00:00+00:00', outcome='skipped')
        self.apply(issue, 'verified', closed_at)

    def test_skipped_start_takes_in_progress_from_the_jira_history(self):
        self.skipped_start('GEO-1', '2026-11-22T12:00:00+00:00')
        with self.assertRaises(ValueError):
            jira_sync.queue(self.root, 'GEO-1', 'worklog')
        from_history = jira_sync.jira_history_time('2026-11-22T02:30:00-06:00')
        worklog = jira_sync.queue(self.root, 'GEO-1', 'worklog', jira_in_progress_at=from_history)
        self.assertEqual('4h', worklog['time_spent'])
        self.assertEqual('2026-11-22T08:30:00.000+0000', worklog['started'])
        self.assertEqual('jira history', worklog['in_progress_source'])

    def test_jira_history_time_is_refused_outside_the_cycle_or_beside_an_applied_start(self):
        self.worked('GEO-1', '2026-11-22T08:00:00+00:00', '2026-11-22T09:00:00+00:00')
        self.skipped_start('GEO-1', '2026-11-22T12:00:00+00:00')
        self.worked('GEO-2', '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:00+00:00')
        refused = [('GEO-1', '2026-11-22T08:30:00+00:00'), ('GEO-1', '2026-11-22T12:30:00+00:00'),
                   ('GEO-2', '2026-11-22T09:00:00+00:00')]
        for issue, moment in refused:
            with self.assertRaises(ValueError):
                jira_sync.queue(self.root, issue, 'worklog',
                                jira_in_progress_at=jira_sync.jira_history_time(moment))

    def test_jira_history_time_needs_a_utc_offset_and_the_worklog_event(self):
        with self.assertRaises(ValueError):
            jira_sync.jira_history_time('2026-11-22T08:30:00')
        with self.assertRaises(ValueError):
            jira_sync.queue(self.root, 'GEO-1', 'start',
                            jira_in_progress_at=jira_sync.jira_history_time('2026-11-22T08:30:00+00:00'))

    def test_worklog_is_refused_for_a_parent_issue(self):
        self.write_issues('E-1', {'key': 'GEO-2', 'parent_key': 'GEO-1'},
                          {'key': 'GEO-4', 'parent': {'key': 'GEO-3'}})
        for parent in ('GEO-1', 'GEO-3'):
            self.worked(parent, '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:00+00:00')
            with self.assertRaises(ValueError):
                jira_sync.queue(self.root, parent, 'worklog')

    def test_command_line_reports_a_refusal_in_one_line(self):
        arguments = ['jira_sync.py', '--root', str(self.root), 'queue', '--issue', 'GEO-1', '--event', 'worklog']
        with mock.patch.object(jira_sync.sys, 'argv', arguments), self.assertRaises(SystemExit) as refusal:
            jira_sync.main()
        self.assertEqual('jira_sync: GEO-1 needs an applied verified update before its worklog',
                         refusal.exception.code)

    def test_worklog_is_queued_once_per_issue_unless_it_was_skipped(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:00+00:00')
        skipped = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        jira_sync.done(self.root, skipped['id'], 'skipped', 'Jira rejected the worklog')
        retried = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        with self.assertRaises(ValueError):
            jira_sync.queue(self.root, 'GEO-1', 'worklog')
        jira_sync.done(self.root, retried['id'], 'applied', 'worklog 10001')
        with self.assertRaises(ValueError):
            jira_sync.queue(self.root, 'GEO-1', 'worklog')

    def test_applied_worklog_leaves_the_source_status_snapshot_unchanged(self):
        issues = self.write_issues('E-1', {'key': 'GEO-1', 'source_status': 'To Do'})
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:00+00:00')
        worklog = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        jira_sync.done(self.root, worklog['id'], 'applied', 'worklog 10001')
        self.assertEqual({'GEO-1': 'Closed'}, self.statuses(issues))

    def test_unknown_event_is_rejected(self):
        with self.assertRaises(ValueError):
            jira_sync.queue(self.root, 'GEO-1', 'reopen')


if __name__ == '__main__':
    unittest.main()
