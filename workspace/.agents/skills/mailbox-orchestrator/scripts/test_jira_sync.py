import json
import tempfile
import unittest
from pathlib import Path

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

    def test_unknown_event_is_rejected(self):
        with self.assertRaises(ValueError):
            jira_sync.queue(self.root, 'GEO-1', 'reopen')


if __name__ == '__main__':
    unittest.main()
