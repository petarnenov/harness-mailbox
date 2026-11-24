import contextlib
import fcntl
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import agent_activity
import issue_snapshots
import jira_sync

MINUTE = timedelta(minutes=1)
SHAPES = ('list', 'wrapped list', 'keyed object', 'wrapped keyed object')
FORBIDDEN_OUTPUT = ('Traceback', 'Expecting', 'Errno', 'codec', 'JSONDecodeError', 'File "')
SECRET = 'SENTINEL-SECRET-VALUE'


def snapshot_document(shape, entries):
    """entries are (object key, record) pairs; the object key only exists in the keyed shapes."""
    records = [record for _, record in entries]
    keyed = dict(entries)
    return {'list': records,
            'wrapped list': {'retrieved_at': 'earlier', 'issues': records},
            'keyed object': keyed,
            'wrapped keyed object': {'retrieved_at': 'earlier', 'issues': keyed}}[shape]


def snapshot_entries(shape):
    entries = [('row-1', {'key': 'GEO-1', 'source_status': 'To Do'}),
               ('row-2', {'issue_key': 'GEO-2', 'source_status': 'To Do'})]
    if 'keyed' in shape:
        entries.append(('GEO-3', {'source_status': 'To Do'}))
    return entries


def container_of(document):
    return document.get('issues', document) if isinstance(document, dict) else document


def shape_of(document):
    keyed = isinstance(container_of(document), dict)
    wrapped = isinstance(document, dict) and 'issues' in document
    return ('wrapped ' if wrapped else '') + ('keyed object' if keyed else 'list')


def by_identity(document):
    container = container_of(document)
    pairs = container.items() if isinstance(container, dict) else [(None, record) for record in container]
    return {record.get('key') or record.get('issue_key') or name: record for name, record in pairs}


PUBLISHING_CALLS = ('replace', 'rename', 'link')


def entry_order(document):
    """Object keys of a keyed container, identities of the records of a list container."""
    container = container_of(document)
    if isinstance(container, dict):
        return list(container)
    return [record.get('key') or record.get('issue_key') for record in container]


@contextlib.contextmanager
def watched_publication(directory):
    """Record the file names present in directory at every publication or flush call."""
    events = []
    real = {name: getattr(os, name) for name in (*PUBLISHING_CALLS, 'fsync')}

    def spy(name):
        def call(*args, **kwargs):
            events.append((name, sorted(os.listdir(directory)) if directory.exists() else []))
            return real[name](*args, **kwargs)
        return call
    with contextlib.ExitStack() as stack:
        for name in real:
            stack.enter_context(mock.patch(f'os.{name}', spy(name)))
        yield events


@contextlib.contextmanager
def failing_publication(folder_name):
    """Make every publication into a folder of that name fail the way a full disk does."""
    real = {name: getattr(os, name) for name in PUBLISHING_CALLS}

    def guard(name):
        def call(source, destination, *args, **kwargs):
            if Path(destination).parent.name == folder_name:
                raise OSError('disk full')
            return real[name](source, destination, *args, **kwargs)
        return call
    with contextlib.ExitStack() as stack:
        for name in real:
            stack.enter_context(mock.patch(f'os.{name}', guard(name)))
        yield


@contextlib.contextmanager
def locked_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def names_in(directory):
    return sorted(os.listdir(directory)) if directory.exists() else []


def flushed_before_publication(events):
    names = [name for name, _ in events]
    published = next(index for index, name in enumerate(names) if name in PUBLISHING_CALLS)
    return 'fsync' in names[:published]


def record_names_seen(events, allowed=()):
    return [name for _, listing in events for name in listing if name.endswith('.json') and name not in allowed]


class JiraSyncTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.transcripts = self.root / 'transcripts'
        patcher = mock.patch.object(jira_sync, 'TRANSCRIPTS', self.transcripts)
        patcher.start()
        self.addCleanup(patcher.stop)
        connect = mock.patch.object(socket.socket, 'connect', side_effect=OSError('network disabled'))
        self.connect = connect.start()
        self.addCleanup(connect.stop)

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

    def link(self, issue, project=None):
        """Give the issue a task in its own project (or the named one) with a registered directory."""
        project = project or f'p-{issue}'
        task = self.root / 'tasks' / project / f't-{issue}.json'
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text('{}')
        self.write_issues(f'E-{issue}', {'key': issue, 'task_ids': [f't-{issue}']})
        registry = self.root / 'projects.json'
        projects = json.loads(registry.read_text())['projects'] if registry.exists() else []
        if not any(entry['id'] == project for entry in projects):
            projects.append({'id': project, 'working_directory': f'/work/{project}'})
        registry.write_text(json.dumps({'projects': projects}))
        return project

    def talk(self, project, *moments, session='s'):
        """Write a transcript of plain messages at the given ISO moments."""
        folder = agent_activity.transcript_folder(self.transcripts, f'/work/{project}')
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / f'{session}.jsonl', 'a') as transcript:
            for moment in moments:
                transcript.write(json.dumps({'type': 'assistant', 'timestamp': moment, 'message': {}}) + '\n')

    def busy(self, project, opened, closed):
        """A message every minute from opened to closed, closed included."""
        moment, end = datetime.fromisoformat(opened), datetime.fromisoformat(closed)
        moments = []
        while moment < end:
            moments.append(moment.isoformat())
            moment += MINUTE
        self.talk(project, *moments, end.isoformat())

    def worked(self, issue, in_progress_at, closed_at, project=None):
        self.busy(self.link(issue, project), in_progress_at, closed_at)
        self.apply(issue, 'start', in_progress_at)
        self.apply(issue, 'verified', closed_at)

    def test_worked_so_far_measures_an_open_issue_up_to_now_and_saves_it_for_the_dashboard(self):
        self.busy(self.link('GEO-1'), '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:00+00:00')
        self.apply('GEO-1', 'start', '2026-11-22T10:00:00+00:00')
        with mock.patch.object(jira_sync, 'now', return_value='2026-11-22T10:30:00+00:00'):
            snapshot = jira_sync.worked_so_far(self.root, self.transcripts)
        self.assertEqual({'measured_at': '2026-11-22T10:30:00+00:00', 'worked_seconds': {'GEO-1': 1800}}, snapshot)
        self.assertEqual(snapshot, json.loads((self.root / 'jira-sync' / 'worked-so-far.json').read_text()))

    def test_worked_so_far_keeps_a_closed_issue_and_leaves_out_unstarted_and_parent_issues(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T11:00:00+00:00')
        self.link('GEO-2')
        self.worked('GEO-3', '2026-11-22T13:00:00+00:00', '2026-11-22T14:00:00+00:00')
        self.write_issues('E-child', {'key': 'GEO-4', 'parent_key': 'GEO-3'})
        with mock.patch.object(jira_sync, 'now', return_value='2026-11-23T09:00:00+00:00'):
            snapshot = jira_sync.worked_so_far(self.root, self.transcripts)
        self.assertEqual({'GEO-1': 3600}, snapshot['worked_seconds'])

    def test_worklog_rounds_in_progress_to_closed_up_to_whole_hours(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:01+00:00')
        worklog = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.assertEqual('3h', worklog['time_spent'])
        self.assertEqual('2026-11-22T10:00:00.000+0000', worklog['started'])
        self.assertEqual('2026-11-22T12:00:01+00:00', worklog['closed_at'])
        self.assertEqual('applied start', worklog['in_progress_source'])

    def test_worklog_keeps_exact_hours_and_rounds_minutes_up_to_one(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:00+00:00')
        self.worked('GEO-2', '2026-11-22T05:26:00+00:00', '2026-11-22T05:30:00+00:00')
        self.assertEqual('2h', jira_sync.queue(self.root, 'GEO-1', 'worklog')['time_spent'])
        self.assertEqual('1h', jira_sync.queue(self.root, 'GEO-2', 'worklog')['time_spent'])

    def test_worklog_counts_only_the_time_its_agents_worked(self):
        project = self.link('GEO-1')
        self.talk(project, '2026-11-22T10:00:00+00:00', '2026-11-22T10:04:00+00:00',
                  '2026-11-22T13:00:00+00:00', '2026-11-22T13:03:00+00:00')
        self.apply('GEO-1', 'start', '2026-11-22T10:00:00+00:00')
        self.apply('GEO-1', 'verified', '2026-11-22T16:00:00+00:00')
        worklog = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.assertEqual('1h', worklog['time_spent'])
        self.assertEqual((6 * 3600, 7 * 60), (worklog['elapsed_seconds'], worklog['worked_seconds']))

    def test_issues_of_one_project_open_at_the_same_time_share_its_work(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T14:00:00+00:00', project='shared')
        self.worked('GEO-2', '2026-11-22T12:00:00+00:00', '2026-11-22T14:00:00+00:00', project='shared')
        first = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        second = jira_sync.queue(self.root, 'GEO-2', 'worklog')
        self.assertEqual((3 * 3600, 3600), (first['worked_seconds'], second['worked_seconds']))

    def test_parent_issue_takes_no_share_of_its_childs_work(self):
        self.worked('GEO-2', '2026-11-22T10:00:00+00:00', '2026-11-22T14:00:00+00:00', project='shared')
        self.write_issues('E-story', {'key': 'GEO-1', 'task_ids': ['t-GEO-2']},
                          {'key': 'GEO-2', 'parent_key': 'GEO-1'})
        self.apply('GEO-1', 'start', '2026-11-22T10:00:00+00:00')
        self.apply('GEO-1', 'verified', '2026-11-22T14:00:00+00:00')
        self.assertEqual(4 * 3600, jira_sync.queue(self.root, 'GEO-2', 'worklog')['worked_seconds'])

    def test_abandoned_issue_takes_no_share(self):
        self.link('GEO-9', project='shared')
        self.apply('GEO-9', 'start', '2026-11-19T09:59:00+00:00')
        self.worked('GEO-2', '2026-11-22T10:00:00+00:00', '2026-11-22T14:00:00+00:00', project='shared')
        self.assertEqual(4 * 3600, jira_sync.queue(self.root, 'GEO-2', 'worklog')['worked_seconds'])

    def test_issue_whose_task_changed_lately_is_not_abandoned(self):
        self.link('GEO-9', project='shared')
        self.apply('GEO-9', 'start', '2026-11-10T08:00:00+00:00')
        task = self.root / 'tasks' / 'shared' / 't-GEO-9.json'
        task.write_text(json.dumps({'updated_at': '2026-11-21T09:00:00+00:00'}))
        self.worked('GEO-2', '2026-11-22T10:00:00+00:00', '2026-11-22T14:00:00+00:00', project='shared')
        self.assertEqual(2 * 3600, jira_sync.queue(self.root, 'GEO-2', 'worklog')['worked_seconds'])

    def test_issues_worked_at_the_same_time_log_exactly_the_time_worked(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T14:00:00+00:00', project='shared')
        self.busy(self.link('GEO-2', project='shared'), '2026-11-22T14:00:00+00:00', '2026-11-22T16:00:00+00:00')
        self.apply('GEO-2', 'start', '2026-11-22T12:00:00+00:00')
        first = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.apply('GEO-2', 'verified', '2026-11-22T16:00:00+00:00')
        second = jira_sync.queue(self.root, 'GEO-2', 'worklog')
        self.assertEqual((3 * 3600, 3 * 3600), (first['worked_seconds'], second['worked_seconds']))

    def test_linked_tasks_are_read_through_the_issue_key_and_local_task_ids_aliases(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:00+00:00')
        self.write_issues('E-GEO-1', {'issue_key': 'GEO-1', 'local_task_ids': ['t-GEO-1']})
        self.assertEqual('2h', jira_sync.queue(self.root, 'GEO-1', 'worklog')['time_spent'])

    def test_issue_with_two_starts_in_one_cycle_takes_a_single_share(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T14:00:00+00:00', project='shared')
        self.apply('GEO-1', 'start', '2026-11-22T11:00:00+00:00', outcome='skipped')
        self.worked('GEO-2', '2026-11-22T10:00:00+00:00', '2026-11-22T14:00:00+00:00', project='shared')
        self.assertEqual(2 * 3600, jira_sync.queue(self.root, 'GEO-2', 'worklog')['worked_seconds'])

    def test_worklog_is_refused_naming_a_project_without_recorded_work(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:00+00:00')
        other_task = self.root / 'tasks' / 'codex-project' / 't-other.json'
        other_task.parent.mkdir(parents=True)
        other_task.write_text('{}')
        self.write_issues('E-GEO-1', {'key': 'GEO-1', 'task_ids': ['t-GEO-1', 't-other']})
        with self.assertRaisesRegex(ValueError, 'no recorded agent work .* in: codex-project$'):
            jira_sync.queue(self.root, 'GEO-1', 'worklog')

    def test_worklog_is_refused_without_a_linked_task_or_any_recorded_work(self):
        self.apply('GEO-1', 'start', '2026-11-22T10:00:00+00:00')
        self.apply('GEO-1', 'verified', '2026-11-22T12:00:00+00:00')
        with self.assertRaisesRegex(ValueError, 'no linked task'):
            jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.link('GEO-1')
        with self.assertRaisesRegex(ValueError, 'no recorded agent work'):
            jira_sync.queue(self.root, 'GEO-1', 'worklog')

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
        self.busy(self.link(issue), '2026-11-22T08:30:00+00:00', closed_at)
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

    def test_command_line_without_a_root_uses_the_mailbox_of_the_script(self):
        arguments = ['jira_sync.py', 'queue', '--issue', 'GEO-1', '--event', 'start']
        with mock.patch.object(jira_sync, 'MAILBOX', self.root), mock.patch.object(jira_sync.sys, 'argv', arguments), \
                mock.patch('builtins.print'):
            jira_sync.main()
        self.assertEqual(['GEO-1'], [request['issue'] for request in jira_sync.read_requests(self.root, 'pending')])

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

    def test_estimate_queues_person_hours_for_original_estimate(self):
        request = jira_sync.queue(self.root, 'GEO-1', 'estimate', hours=6)
        self.assertEqual('6h', request['original_estimate'])
        self.assertEqual('person-hours', request['unit'])
        self.assertNotIn('target_status', request)
        self.assertEqual([request], jira_sync.pending(self.root))

    def test_estimate_needs_positive_hours_and_hours_need_the_estimate_event(self):
        for hours in (None, 0, -2):
            with self.assertRaises(ValueError):
                jira_sync.queue(self.root, 'GEO-1', 'estimate', hours=hours)
        with self.assertRaises(ValueError):
            jira_sync.queue(self.root, 'GEO-1', 'start', hours=6)

    def test_estimate_is_never_queued_twice_unless_it_was_skipped(self):
        skipped = jira_sync.queue(self.root, 'GEO-1', 'estimate', hours=6)
        jira_sync.done(self.root, skipped['id'], 'skipped', 'Original Estimate already set in Jira')
        retried = jira_sync.queue(self.root, 'GEO-1', 'estimate', hours=5)
        with self.assertRaises(ValueError):
            jira_sync.queue(self.root, 'GEO-1', 'estimate', hours=7)
        jira_sync.done(self.root, retried['id'], 'applied', 'originalEstimate 5h read back')
        with self.assertRaises(ValueError):
            jira_sync.queue(self.root, 'GEO-1', 'estimate', hours=7)

    def test_applied_estimate_reaches_the_issue_snapshot_and_keeps_its_status(self):
        issues = self.write_issues('E-1', {'key': 'GEO-1', 'source_status': 'To Do'},
                                   {'key': 'GEO-2', 'source_status': 'To Do'})
        request = jira_sync.queue(self.root, 'GEO-1', 'estimate', hours=1.5)
        jira_sync.done(self.root, request['id'], 'applied', 'originalEstimate 1.5h read back')
        records = {r['key']: r for r in json.loads(issues.read_text())['issues']}
        self.assertEqual({'value': 1.5, 'unit': 'hours', 'source': 'jira timeoriginalestimate'},
                         {k: v for k, v in records['GEO-1']['estimate'].items() if k != 'retrieved_at'})
        self.assertNotIn('estimate', records['GEO-2'])
        self.assertEqual({'GEO-1': 'To Do', 'GEO-2': 'To Do'}, self.statuses(issues))

    def test_skipped_estimate_leaves_the_snapshot_unchanged(self):
        issues = self.write_issues('E-1', {'key': 'GEO-1', 'source_status': 'To Do'})
        request = jira_sync.queue(self.root, 'GEO-1', 'estimate', hours=6)
        jira_sync.done(self.root, request['id'], 'skipped', 'Original Estimate already set in Jira')
        self.assertNotIn('estimate', json.loads(issues.read_text())['issues'][0])

    def test_unknown_event_is_rejected(self):
        with self.assertRaises(ValueError):
            jira_sync.queue(self.root, 'GEO-1', 'reopen')

    def cli(self, *arguments):
        """Run the command line in process; returns (exit status, one text of everything it printed)."""
        argv = ['jira_sync.py', '--root', str(self.root), *arguments]
        out, err = io.StringIO(), io.StringIO()
        status = 0
        with mock.patch.object(jira_sync.sys, 'argv', argv), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                jira_sync.main()
            except SystemExit as stop:
                status = stop.code
            except Exception as error:
                self.fail(f'{type(error).__name__} escaped the command line')
        if isinstance(status, str):
            err.write(status + '\n')
            status = 1
        return status, out.getvalue() + err.getvalue()

    def assert_refused(self, outcome):
        status, text = outcome
        self.assertNotIn(status, (0, None))
        for fragment in (*FORBIDDEN_OUTPUT, str(self.root), SECRET):
            self.assertNotIn(fragment, text)
        self.assertEqual(1, len([line for line in text.splitlines() if line.strip()]), text)

    def write_snapshot(self, epic, document):
        path = self.root / 'epics' / epic / 'issues.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
        return path

    def test_every_issue_snapshot_format_is_updated_by_identity_and_written_back_in_its_shape(self):
        for shape in SHAPES:
            for issue in ('GEO-1', 'GEO-2', 'GEO-3'):
                if issue == 'GEO-3' and 'keyed' not in shape:
                    continue
                with self.subTest(shape=shape, issue=issue):
                    path = self.write_snapshot('E-1', snapshot_document(shape, snapshot_entries(shape)))
                    original_names = entry_order(json.loads(path.read_text()))
                    request = jira_sync.queue(self.root, issue, 'verified')
                    jira_sync.done(self.root, request['id'], 'applied', 'transition 6')
                    document = json.loads(path.read_text())
                    self.assertEqual(shape, shape_of(document))
                    self.assertEqual(original_names, entry_order(document))
                    statuses = {name: record['source_status'] for name, record in by_identity(document).items()}
                    self.assertEqual({name: 'Closed' if name == issue else 'To Do' for name in statuses}, statuses)
                    if 'wrapped' in shape:
                        self.assertEqual('earlier', document['retrieved_at'])

    def test_applied_estimate_is_written_into_every_snapshot_format(self):
        for shape in SHAPES:
            with self.subTest(shape=shape):
                path = self.write_snapshot('E-1', snapshot_document(shape, snapshot_entries(shape)))
                request = jira_sync.queue(self.root, 'GEO-2', 'estimate', hours=3)
                jira_sync.done(self.root, request['id'], 'applied', 'originalEstimate 3h read back')
                document = json.loads(path.read_text())
                self.assertEqual(shape, shape_of(document))
                records = by_identity(document)
                self.assertEqual(3.0, records['GEO-2']['estimate']['value'])
                self.assertNotIn('estimate', records['GEO-1'])
                (self.root / 'jira-sync' / 'done' / f"{request['id']}.json").unlink()

    def test_object_key_of_an_entry_with_its_own_key_is_not_its_identity(self):
        path = self.write_snapshot('E-1', snapshot_document('keyed object', snapshot_entries('keyed object')))
        before = path.read_text()
        request = jira_sync.queue(self.root, 'row-1', 'verified')
        jira_sync.done(self.root, request['id'], 'applied', 'transition 6')
        self.assertEqual(before, path.read_text())

    def test_linked_tasks_are_read_from_every_snapshot_format(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:00+00:00')
        for shape in SHAPES:
            with self.subTest(shape=shape):
                entries = [('GEO-1', {'task_ids': ['t-GEO-1']})] if 'keyed' in shape else \
                    [('row', {'key': 'GEO-1', 'task_ids': ['t-GEO-1']})]
                self.write_snapshot('E-GEO-1', snapshot_document(shape, entries))
                worklog = jira_sync.queue(self.root, 'GEO-1', 'worklog')
                self.assertEqual('2h', worklog['time_spent'])
                jira_sync.done(self.root, worklog['id'], 'skipped', 'retry')

    def test_parent_issue_is_found_in_every_snapshot_format(self):
        self.worked('GEO-1', '2026-11-22T10:00:00+00:00', '2026-11-22T12:00:00+00:00')
        for shape in SHAPES:
            with self.subTest(shape=shape):
                child = ('GEO-2', {'parent_key': 'GEO-1'}) if 'keyed' in shape else \
                    ('row', {'key': 'GEO-2', 'parent': {'key': 'GEO-1'}})
                self.write_snapshot('E-child', snapshot_document(shape, [child]))
                with self.assertRaisesRegex(ValueError, 'parent issue'):
                    jira_sync.queue(self.root, 'GEO-1', 'worklog')

    def test_issues_json_that_cannot_be_read_refuses_the_command_in_one_line(self):
        self.apply('GEO-1', 'start', '2026-11-22T10:00:00+00:00')
        self.apply('GEO-1', 'verified', '2026-11-22T12:00:00+00:00')
        path = self.root / 'epics' / 'E-bad' / 'issues.json'
        path.parent.mkdir(parents=True)
        for content in (b'{broken', b'\xff\xfe\x00', b''):
            for arguments in (('queue', '--issue', 'GEO-1', '--event', 'worklog'), ('worked',)):
                with self.subTest(content=content, arguments=arguments):
                    path.write_bytes(content)
                    self.assert_refused(self.cli(*arguments))

    def test_symlinked_epic_snapshots_are_never_written_through(self):
        outside = self.root / 'outside'
        outside.mkdir()
        target = outside / 'issues.json'
        target.write_text(json.dumps({'issues': [{'key': 'GEO-1', 'source_status': 'To Do'}]}))
        (self.root / 'epics').mkdir()
        (self.root / 'epics' / 'LINKED').symlink_to(outside)
        file_link = self.root / 'epics' / 'E-2' / 'issues.json'
        file_link.parent.mkdir()
        file_link.symlink_to(target)
        before = target.read_text()
        request = jira_sync.queue(self.root, 'GEO-1', 'verified')
        self.cli('done', '--id', request['id'], '--outcome', 'applied', '--detail', 'transition 6')
        self.assertEqual(before, target.read_text())
        self.assertEqual(['issues.json'], names_in(outside))

    def test_failed_snapshot_write_leaves_the_original_and_no_temporary_file(self):
        path = self.write_snapshot('E-1', {'issues': [{'key': 'GEO-1', 'source_status': 'To Do'}]})
        before = path.read_bytes()
        request = jira_sync.queue(self.root, 'GEO-1', 'verified')
        with failing_publication('E-1'):
            outcome = self.cli('done', '--id', request['id'], '--outcome', 'applied', '--detail', 'x')
        self.assert_refused(outcome)
        self.assertNotIn('disk full', outcome[1])
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(['issues.json'], names_in(path.parent))

    def test_snapshot_write_uses_a_flushed_temporary_name_that_is_not_a_record(self):
        path = self.write_snapshot('E-1', {'issues': [{'key': 'GEO-1', 'source_status': 'To Do'}]})
        request = jira_sync.queue(self.root, 'GEO-1', 'verified')
        with watched_publication(path.parent) as events:
            jira_sync.done(self.root, request['id'], 'applied', 'transition 6')
        self.assertTrue(flushed_before_publication(events))
        self.assertEqual([], record_names_seen(events, allowed=('issues.json',)))
        self.assertEqual(['issues.json'], names_in(path.parent))

    def test_snapshot_update_waits_for_the_exclusive_lock_on_the_epic_folder(self):
        path = self.write_snapshot('E-1', {'issues': [{'key': 'GEO-1', 'source_status': 'To Do'}]})
        request = jira_sync.queue(self.root, 'GEO-1', 'verified')
        worker = threading.Thread(target=jira_sync.done, args=(self.root, request['id'], 'applied', 'transition 6'))
        with locked_directory(path.parent):
            worker.start()
            worker.join(0.5)
            self.assertTrue(worker.is_alive())
            self.assertEqual('To Do', self.statuses(path)['GEO-1'])
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual('Closed', self.statuses(path)['GEO-1'])

    def test_queued_request_uses_a_flushed_temporary_name_that_is_not_a_record(self):
        pending_folder = self.root / 'jira-sync' / 'pending'
        with watched_publication(pending_folder) as events:
            request = jira_sync.queue(self.root, 'GEO-1', 'start')
        self.assertTrue(flushed_before_publication(events))
        self.assertEqual([], record_names_seen(events))
        self.assertEqual([f"{request['id']}.json"], names_in(pending_folder))

    def test_done_record_uses_a_flushed_temporary_name_that_is_not_a_record(self):
        request = jira_sync.queue(self.root, 'GEO-1', 'pr')
        done_folder = self.root / 'jira-sync' / 'done'
        with watched_publication(done_folder) as events:
            jira_sync.done(self.root, request['id'], 'skipped', 'no such status')
        self.assertTrue(flushed_before_publication(events))
        self.assertEqual([], record_names_seen(events))
        self.assertEqual([f"{request['id']}.json"], names_in(done_folder))

    def test_failed_queue_publication_leaves_no_file_in_the_queue(self):
        with failing_publication('pending'):
            outcome = self.cli('queue', '--issue', 'GEO-1', '--event', 'start')
        self.assert_refused(outcome)
        self.assertNotIn('disk full', outcome[1])
        self.assertEqual([], names_in(self.root / 'jira-sync' / 'pending'))

    def test_failed_done_publication_leaves_the_request_pending_and_nothing_in_done(self):
        request = jira_sync.queue(self.root, 'GEO-1', 'pr')
        with failing_publication('done'):
            self.assert_refused(self.cli('done', '--id', request['id'], '--outcome', 'applied', '--detail', 'x'))
        self.assertEqual([f"{request['id']}.json"], names_in(self.root / 'jira-sync' / 'pending'))
        self.assertEqual([], names_in(self.root / 'jira-sync' / 'done'))

    def test_queue_never_overwrites_a_request_with_the_same_id(self):
        fixed = uuid.UUID('12345678-1234-5678-1234-567812345678')
        with mock.patch.object(jira_sync.uuid, 'uuid4', return_value=fixed):
            first = jira_sync.queue(self.root, 'GEO-1', 'start')
            with self.assertRaises((ValueError, OSError)):
                jira_sync.queue(self.root, 'GEO-2', 'pr')
        self.assertEqual([first], jira_sync.pending(self.root))

    def completed_record(self, request):
        folder = self.root / 'jira-sync' / 'done'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{request['id']}.json"
        path.write_text(json.dumps({**request, 'outcome': 'applied', 'detail': 'first',
                                    'completed_at': '2026-11-22T10:00:00+00:00'}))
        return path

    def test_done_is_refused_for_an_id_that_is_already_completed_and_keeps_its_record(self):
        request = jira_sync.queue(self.root, 'GEO-1', 'pr')
        jira_sync.done(self.root, request['id'], 'applied', 'first')
        record = self.root / 'jira-sync' / 'done' / f"{request['id']}.json"
        before = record.read_bytes()
        self.assert_refused(self.cli('done', '--id', request['id'], '--outcome', 'skipped', '--detail', 'second'))
        self.assertEqual(before, record.read_bytes())

    def test_done_is_refused_and_keeps_the_record_while_the_request_is_pending_beside_its_done_record(self):
        request = jira_sync.queue(self.root, 'GEO-1', 'pr')
        record = self.completed_record(request)
        before = record.read_bytes()
        self.assert_refused(self.cli('done', '--id', request['id'], '--outcome', 'skipped', '--detail', 'second'))
        self.assertEqual(before, record.read_bytes())

    def test_request_with_a_done_record_is_not_listed_as_pending(self):
        completed = jira_sync.queue(self.root, 'GEO-1', 'pr')
        self.completed_record(completed)
        waiting = jira_sync.queue(self.root, 'GEO-2', 'pr')
        self.assertEqual([waiting], jira_sync.pending(self.root))
        status, text = self.cli('pending')
        self.assertEqual(0, status)
        self.assertEqual([waiting['id']], [request['id'] for request in json.loads(text)])

    def test_done_refuses_an_id_that_is_not_a_canonical_uuid_and_deletes_nothing(self):
        snapshot = self.write_snapshot('E-1', {'issues': [{'key': 'GEO-1', 'source_status': 'To Do'}]})
        request = jira_sync.queue(self.root, 'GEO-1', 'pr')
        before = snapshot.read_bytes()
        bad_ids = ('../../epics/E-1/issues', 'E-1', str(snapshot.with_suffix('')), 'not-a-uuid', '',
                   request['id'].replace('-', ''))
        for bad_id in bad_ids:
            with self.subTest(request_id=bad_id):
                self.assert_refused(self.cli('done', '--id', bad_id, '--outcome', 'applied', '--detail', 'x'))
                self.assertEqual(before, snapshot.read_bytes())
                self.assertEqual([f"{request['id']}.json"], names_in(self.root / 'jira-sync' / 'pending'))
                self.assertEqual([], names_in(self.root / 'jira-sync' / 'done'))

    def test_estimate_refuses_hours_that_are_not_finite(self):
        for hours in ('nan', 'inf', '-inf'):
            with self.subTest(hours=hours):
                status, text = self.cli('queue', '--issue', 'GEO-1', '--event', 'estimate', f'--hours={hours}')
                self.assertNotIn(status, (0, None))
                self.assertEqual('jira_sync: The estimate event needs --hours, a positive number of person-hours',
                                 text.strip())
                self.assertEqual([], names_in(self.root / 'jira-sync' / 'pending'))
        for hours in (float('nan'), float('inf')):
            with self.subTest(direct=hours), self.assertRaises(ValueError):
                jira_sync.queue(self.root, 'GEO-1', 'estimate', hours=hours)

    def write_request_file(self, state, name, text):
        path = self.root / 'jira-sync' / state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_malformed_request_record_refuses_the_listing_in_one_line(self):
        for text in ('{"token": "' + SECRET, '[]', '{"id": "x"}'):
            with self.subTest(record=text):
                path = self.write_request_file('pending', f'{uuid.uuid4()}.json', text)
                self.assert_refused(self.cli('pending'))
                path.unlink()

    def test_done_refuses_a_malformed_or_unknown_request_in_one_line(self):
        self.assert_refused(self.cli('done', '--id', str(uuid.uuid4()), '--outcome', 'applied', '--detail', 'x'))
        broken = str(uuid.uuid4())
        self.write_request_file('pending', f'{broken}.json', '{"token": "' + SECRET)
        self.assert_refused(self.cli('done', '--id', broken, '--outcome', 'applied', '--detail', 'x'))
        self.assertEqual([f'{broken}.json'], names_in(self.root / 'jira-sync' / 'pending'))
        self.assertEqual([], names_in(self.root / 'jira-sync' / 'done'))

    def test_unknown_issue_and_refused_operations_end_in_one_line(self):
        self.assert_refused(self.cli('queue', '--issue', 'GEO-9', '--event', 'worklog'))
        self.assert_refused(self.cli('queue', '--issue', 'GEO-9', '--event', 'estimate'))
        self.assert_refused(self.cli('done', '--id', str(uuid.uuid4()), '--outcome', 'skipped', '--detail', ' '))

    def test_command_line_outputs_keep_their_shapes(self):
        status, text = self.cli('queue', '--issue', 'GEO-1', '--event', 'start', '--ref', 'PR 1')
        queued = json.loads(text)
        self.assertEqual(0, status)
        self.assertEqual({'id', 'issue', 'event', 'target_status', 'assign_current_user', 'ref', 'queued_at'},
                         set(queued))
        self.assertEqual([queued], json.loads(self.cli('pending')[1]))
        completed = json.loads(self.cli('done', '--id', queued['id'], '--outcome', 'applied',
                                        '--detail', 'assigned')[1])
        self.assertEqual({**queued, 'outcome': 'applied', 'detail': 'assigned'},
                         {k: v for k, v in completed.items() if k != 'completed_at'})
        self.assertEqual([], json.loads(self.cli('pending')[1]))
        self.assertFalse(self.connect.called)

    def test_no_secret_reaches_the_output_and_no_network_is_attempted(self):
        (self.root / 'jira-token').write_text(SECRET)
        with mock.patch.dict(os.environ, {'JIRA_API_TOKEN': SECRET}):
            outcomes = [self.cli('queue', '--issue', 'GEO-1', '--event', 'start'), self.cli('pending'),
                        self.cli('queue', '--issue', 'GEO-1', '--event', 'worklog'), self.cli('worked')]
        self.assertNotIn(SECRET, ''.join(text for _, text in outcomes))
        self.assertFalse(self.connect.called)

    def test_shared_hours_of_an_issue_with_a_skipped_start_are_counted_once(self):
        shared = 'shared'
        self.busy(self.link('GEO-1', shared), '2026-11-22T08:30:00+00:00', '2026-11-22T12:00:00+00:00')
        self.link('GEO-2', shared)
        self.apply('GEO-1', 'start', '2026-11-22T10:00:00+00:00', outcome='skipped')
        self.apply('GEO-1', 'verified', '2026-11-22T12:00:00+00:00')
        self.apply('GEO-2', 'start', '2026-11-22T08:30:00+00:00')
        self.apply('GEO-2', 'verified', '2026-11-22T12:00:00+00:00')
        first = jira_sync.queue(self.root, 'GEO-1', 'worklog',
                                jira_in_progress_at=jira_sync.jira_history_time('2026-11-22T08:30:00+00:00'))
        second = jira_sync.queue(self.root, 'GEO-2', 'worklog')
        self.assertEqual(3.5 * 3600, first['worked_seconds'] + second['worked_seconds'])

    def test_shared_hours_of_an_issue_started_twice_in_one_cycle_are_counted_once(self):
        shared = 'shared'
        self.busy(self.link('GEO-1', shared), '2026-11-22T09:00:00+00:00', '2026-11-22T12:00:00+00:00')
        self.link('GEO-2', shared)
        self.apply('GEO-1', 'start', '2026-11-22T09:00:00+00:00')
        self.apply('GEO-1', 'start', '2026-11-22T10:00:00+00:00')
        self.apply('GEO-1', 'verified', '2026-11-22T12:00:00+00:00')
        self.apply('GEO-2', 'start', '2026-11-22T09:00:00+00:00')
        self.apply('GEO-2', 'verified', '2026-11-22T12:00:00+00:00')
        first = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        second = jira_sync.queue(self.root, 'GEO-2', 'worklog')
        self.assertEqual(3 * 3600, first['worked_seconds'] + second['worked_seconds'])

    SHARES_WITH_SKIPPED = 'GEO-1 shares a project with GEO-2, whose start was skipped; ' \
                          'queue the worklog of GEO-2 first'

    def close_cycle(self, issue, project, started_at, start_outcome, closed_at):
        self.link(issue, project)
        self.apply(issue, 'start', started_at, outcome=start_outcome)
        self.apply(issue, 'verified', closed_at)

    def applied_issue_with_activity(self, project='shared'):
        """GEO-1 started (applied) at 09:00 and Closed at 12:00 in a project that worked all that time."""
        self.busy(self.link('GEO-1', project), '2026-11-22T09:00:00+00:00', '2026-11-22T12:00:00+00:00')
        self.apply('GEO-1', 'start', '2026-11-22T09:00:00+00:00')
        self.apply('GEO-1', 'verified', '2026-11-22T12:00:00+00:00')

    def test_worklog_of_an_applied_start_waits_for_its_skipped_start_neighbour(self):
        self.applied_issue_with_activity()
        self.close_cycle('GEO-2', 'shared', '2026-11-22T11:00:00+00:00', 'skipped', '2026-11-22T12:00:00+00:00')
        with self.assertRaises(ValueError) as refusal:
            jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.assertEqual(self.SHARES_WITH_SKIPPED, str(refusal.exception))
        self.assertEqual([], jira_sync.pending(self.root))
        skipped = jira_sync.queue(self.root, 'GEO-2', 'worklog',
                                  jira_in_progress_at=jira_sync.jira_history_time('2026-11-22T09:00:00+00:00'))
        applied = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.assertEqual(3 * 3600, skipped['worked_seconds'] + applied['worked_seconds'])

    def test_two_issues_with_skipped_starts_do_not_block_each_other(self):
        self.busy(self.link('GEO-1', 'shared'), '2026-11-22T09:00:00+00:00', '2026-11-22T12:00:00+00:00')
        self.apply('GEO-1', 'start', '2026-11-22T10:00:00+00:00', outcome='skipped')
        self.apply('GEO-1', 'verified', '2026-11-22T12:00:00+00:00')
        self.close_cycle('GEO-2', 'shared', '2026-11-22T11:00:00+00:00', 'skipped', '2026-11-22T12:00:00+00:00')
        history = jira_sync.jira_history_time('2026-11-22T09:00:00+00:00')
        for issue in ('GEO-1', 'GEO-2'):
            self.assertEqual('worklog', jira_sync.queue(self.root, issue, 'worklog',
                                                        jira_in_progress_at=history)['event'])

    def test_skipped_start_neighbour_whose_cycle_ends_before_this_one_starts_does_not_block(self):
        self.applied_issue_with_activity()
        self.close_cycle('GEO-2', 'shared', '2026-11-22T07:00:00+00:00', 'skipped', '2026-11-22T08:00:00+00:00')
        worklog = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.assertEqual(3 * 3600, worklog['worked_seconds'])

    def test_skipped_start_neighbour_whose_cycle_begins_after_this_one_closed_does_not_block(self):
        self.applied_issue_with_activity()
        self.close_cycle('GEO-2', 'shared', '2026-11-22T08:00:00+00:00', 'applied', '2026-11-22T12:30:00+00:00')
        self.apply('GEO-2', 'start', '2026-11-22T13:00:00+00:00', outcome='skipped')
        self.apply('GEO-2', 'verified', '2026-11-22T14:00:00+00:00')
        self.assertEqual('worklog', jira_sync.queue(self.root, 'GEO-1', 'worklog')['event'])

    def test_skipped_start_neighbour_in_another_project_does_not_block(self):
        self.applied_issue_with_activity()
        self.close_cycle('GEO-2', 'other', '2026-11-22T11:00:00+00:00', 'skipped', '2026-11-22T12:00:00+00:00')
        worklog = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.assertEqual(3 * 3600, worklog['worked_seconds'])

    def failures_of_snapshot_update(self):
        """(name, how to break the update, how to repair it) for each way the snapshot update can fail."""
        broken = self.root / 'epics' / 'E-2' / 'issues.json'

        @contextlib.contextmanager
        def raising(error):
            with mock.patch.object(jira_sync.issue_snapshots, 'update_snapshot', side_effect=error):
                yield

        @contextlib.contextmanager
        def malformed_snapshot():
            broken.parent.mkdir(parents=True, exist_ok=True)
            broken.write_text('{broken')
            try:
                yield
            finally:
                broken.unlink()
        return [('malformed snapshot', malformed_snapshot),
                ('snapshot error', lambda: raising(issue_snapshots.SnapshotError('The snapshot cannot be used'))),
                ('operating system error', lambda: raising(OSError('disk full')))]

    def test_failed_snapshot_update_leaves_the_request_pending_and_a_repeated_done_writes_the_value(self):
        number = 0
        for event in ('verified', 'estimate'):
            for name, failure in self.failures_of_snapshot_update():
                number += 1
                issue = f'GEO-{number}'
                with self.subTest(event=event, failure=name):
                    path = self.write_snapshot('E-1', {'issues': [{'key': issue, 'source_status': 'To Do'}]})
                    request = jira_sync.queue(self.root, issue, event, hours=2 if event == 'estimate' else None)
                    arguments = ('done', '--id', request['id'], '--outcome', 'applied', '--detail', 'x')
                    with failure():
                        outcome = self.cli(*arguments)
                    self.assert_refused(outcome)
                    self.assertNotIn('disk full', outcome[1])
                    self.assertIn(f"{request['id']}.json", names_in(self.root / 'jira-sync' / 'pending'))
                    self.assertNotIn(f"{request['id']}.json", names_in(self.root / 'jira-sync' / 'done'))
                    self.assertEqual(0, self.cli(*arguments)[0])
                    record = json.loads(path.read_text())['issues'][0]
                    written = record['source_status'] if event == 'verified' else record['estimate']['value']
                    self.assertEqual('Closed' if event == 'verified' else 2.0, written)

    def test_symlinked_epics_folder_is_never_read_or_written_through(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        target = outside / 'E-1' / 'issues.json'
        target.parent.mkdir()
        target.write_text(json.dumps({'issues': [{'key': 'GEO-1', 'source_status': 'To Do'}]}))
        (self.root / 'epics').symlink_to(outside)
        before = target.read_bytes()
        request = jira_sync.queue(self.root, 'GEO-1', 'verified')
        outcome = self.cli('done', '--id', request['id'], '--outcome', 'applied', '--detail', 'transition 6')
        self.assert_refused(outcome)
        self.assertEqual(self.EPICS_LINK_REFUSAL, outcome[1].strip())
        self.assertEqual(before, target.read_bytes())
        self.assertEqual(['issues.json'], names_in(target.parent))
        self.assertEqual([f"{request['id']}.json"], names_in(self.root / 'jira-sync' / 'pending'))
        self.assertEqual([], names_in(self.root / 'jira-sync' / 'done'))
        with self.assertRaises(issue_snapshots.SnapshotError):
            issue_snapshots.all_issue_records(self.root)

    EPICS_LINK_REFUSAL = 'jira_sync: The epics folder must not be a symbolic link'

    def test_commands_that_read_snapshots_refuse_a_symlinked_epics_folder(self):
        self.apply('GEO-1', 'start', '2026-11-22T10:00:00+00:00')
        self.apply('GEO-1', 'verified', '2026-11-22T12:00:00+00:00')
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (self.root / 'epics').symlink_to(outside)
        for arguments in (('queue', '--issue', 'GEO-1', '--event', 'worklog'), ('worked',)):
            with self.subTest(arguments=arguments):
                outcome = self.cli(*arguments)
                self.assert_refused(outcome)
                self.assertEqual(self.EPICS_LINK_REFUSAL, outcome[1].strip())
        self.assertEqual([], names_in(self.root / 'jira-sync' / 'pending'))

    def test_neighbour_still_in_progress_after_a_skipped_start_does_not_block_and_counts_from_its_skipped_start(self):
        self.applied_issue_with_activity()
        self.link('GEO-2', 'shared')
        self.apply('GEO-2', 'start', '2026-11-22T11:00:00+00:00', outcome='skipped')
        worklog = jira_sync.queue(self.root, 'GEO-1', 'worklog')
        self.assertEqual(2.5 * 3600, worklog['worked_seconds'])
        self.assertEqual('3h', worklog['time_spent'])


if __name__ == '__main__':
    unittest.main()
