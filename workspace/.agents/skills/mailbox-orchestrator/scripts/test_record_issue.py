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
from pathlib import Path
from unittest import mock

from record_issue import main

SHAPES = ('list', 'wrapped list', 'keyed object', 'wrapped keyed object')
FORBIDDEN_OUTPUT = ('Traceback', 'Expecting', 'Errno', 'codec', 'JSONDecodeError', 'File "')
SECRET = 'SENTINEL-SECRET-VALUE'
PUBLISHING_CALLS = ('replace', 'rename', 'link')


def snapshot_document(shape, entries):
    """entries are (object key, record) pairs; the object key only exists in the keyed shapes."""
    records = [record for _, record in entries]
    keyed = dict(entries)
    return {'list': records,
            'wrapped list': {'retrieved_at': 'earlier', 'issues': records},
            'keyed object': keyed,
            'wrapped keyed object': {'retrieved_at': 'earlier', 'issues': keyed}}[shape]


def snapshot_entries(shape):
    entries = [('row-1', {'key': 'GEO-1', 'local_status': 'in_progress'}),
               ('row-2', {'issue_key': 'GEO-2', 'local_status': 'in_progress'})]
    if 'keyed' in shape:
        entries.append(('GEO-3', {'local_status': 'in_progress'}))
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


def entry_order(document):
    """Object keys of a keyed container, identities of the records of a list container."""
    container = container_of(document)
    if isinstance(container, dict):
        return list(container)
    return [record.get('key') or record.get('issue_key') for record in container]


def names_in(directory):
    return sorted(os.listdir(directory)) if directory.exists() else []


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


class RecordIssueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.other = tempfile.TemporaryDirectory()
        self.outside = Path(self.other.name)
        self.path = self.root / 'epics' / 'EPIC-1' / 'issues.json'
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({'issues': [
            {'key': 'GEO-1', 'local_status': 'in_progress', 'blockers': ['depends on GEO-2', 'needs token']},
            {'key': 'GEO-2', 'local_status': 'in_progress'}]}))
        connect = mock.patch.object(socket.socket, 'connect', side_effect=OSError('network disabled'))
        self.connect = connect.start()
        self.addCleanup(connect.stop)

    def tearDown(self):
        self.temp.cleanup()
        self.other.cleanup()

    def issue(self, key):
        return next(i for i in json.loads(self.path.read_text())['issues'] if i['key'] == key)

    def run_command(self, *args):
        main(['--root', str(self.root), '--epic', 'EPIC-1', *args])

    def invoke(self, *args, epic='EPIC-1'):
        """Run the command line in process; returns (exit status, one text of everything it printed)."""
        out, err = io.StringIO(), io.StringIO()
        status = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main(['--root', str(self.root), '--epic', epic, *args])
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
        for fragment in (*FORBIDDEN_OUTPUT, str(self.root), str(self.outside), SECRET):
            self.assertNotIn(fragment, text)
        self.assertEqual(1, len([line for line in text.splitlines() if line.strip()]), text)
        return text

    def write_snapshot(self, document, path=None):
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
        return path

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
        text = self.assert_refused(self.invoke('--issue', 'GEO-2', 'verified', '--evidence', 'r1'))
        self.assertIn('t-open', text)
        self.assertEqual(self.issue('GEO-2')['local_status'], 'in_progress')

    def test_verified_is_refused_for_done_task_without_evidence_or_with_lock(self):
        self.write_task('t-bare', issue_keys=['GEO-2'], status='done')
        locked = self.write_task('t-locked', issue_keys=['GEO-2'], status='done', evidence=['x'])
        locked.with_suffix('.lock').mkdir()
        text = self.assert_refused(self.invoke('--issue', 'GEO-2', 'verified', '--evidence', 'r1'))
        self.assertIn('t-bare: done without evidence', text)
        self.assertIn('t-locked: lock still held', text)

    def test_verified_checks_tasks_listed_on_the_issue_and_missing_records(self):
        issues = json.loads(self.path.read_text())
        issues['issues'][1]['local_task_ids'] = ['t-gone']
        self.path.write_text(json.dumps(issues))
        text = self.assert_refused(self.invoke('--issue', 'GEO-2', 'verified', '--evidence', 'r1'))
        self.assertIn('t-gone: task record missing', text)

    def test_verified_passes_when_every_linked_task_is_closed(self):
        self.write_task('t-done', issue_key='GEO-2', status='done', evidence=['merge abc'])
        self.write_task('t-other', issue_key='GEO-1', status='in_progress')
        self.run_command('--issue', 'GEO-2', 'verified', '--evidence', 'r1')
        self.assertEqual(self.issue('GEO-2')['local_status'], 'verified_done')

    def test_verified_accepts_a_cancelled_task_only_with_evidence(self):
        self.write_task('t-bare-cancel', issue_key='GEO-2', status='cancelled')
        text = self.assert_refused(self.invoke('--issue', 'GEO-2', 'verified', '--evidence', 'r1'))
        self.assertIn('t-bare-cancel: cancelled without evidence', text)
        self.write_task('t-bare-cancel', issue_key='GEO-2', status='cancelled', evidence=['user decision d1'])
        self.run_command('--issue', 'GEO-2', 'verified', '--evidence', 'r1')
        self.assertEqual(self.issue('GEO-2')['local_status'], 'verified_done')

    def test_rejects_unknown_issue_self_dependency_and_empty_evidence(self):
        before = self.path.read_bytes()
        self.assert_refused(self.invoke('--issue', 'GEO-9', 'depends-on', 'GEO-2'))
        self.assert_refused(self.invoke('--issue', 'GEO-1', 'depends-on', 'GEO-1'))
        self.assert_refused(self.invoke('--issue', 'GEO-2', 'verified', '--evidence', ' '))
        self.assertEqual(before, self.path.read_bytes())

    def test_every_issue_snapshot_format_is_updated_by_identity_and_written_back_in_its_shape(self):
        for shape in SHAPES:
            for issue in ('GEO-1', 'GEO-2', 'GEO-3'):
                if issue == 'GEO-3' and 'keyed' not in shape:
                    continue
                with self.subTest(shape=shape, issue=issue):
                    self.write_snapshot(snapshot_document(shape, snapshot_entries(shape)))
                    original_names = entry_order(json.loads(self.path.read_text()))
                    self.run_command('--issue', issue, 'depends-on', 'GEO-9')
                    document = json.loads(self.path.read_text())
                    self.assertEqual(shape, shape_of(document))
                    self.assertEqual(original_names, entry_order(document))
                    depends = {name: record.get('depends_on') for name, record in by_identity(document).items()}
                    self.assertEqual({name: ['GEO-9'] if name == issue else None for name in depends}, depends)
                    if 'wrapped' in shape:
                        self.assertEqual('earlier', document['retrieved_at'])

    def test_verified_is_recorded_in_every_snapshot_format(self):
        self.write_task('t-closed', issue_keys=['GEO-1', 'GEO-2', 'GEO-3'], status='done', evidence=['merge'])
        for shape in SHAPES:
            for issue in ('GEO-1', 'GEO-2', 'GEO-3'):
                if issue == 'GEO-3' and 'keyed' not in shape:
                    continue
                with self.subTest(shape=shape, issue=issue):
                    self.write_snapshot(snapshot_document(shape, snapshot_entries(shape)))
                    self.run_command('--issue', issue, 'verified', '--evidence', 'r1')
                    document = json.loads(self.path.read_text())
                    self.assertEqual(shape, shape_of(document))
                    statuses = {name: record['local_status'] for name, record in by_identity(document).items()}
                    self.assertEqual({name: 'verified_done' if name == issue else 'in_progress'
                                      for name in statuses}, statuses)

    def test_verified_of_an_issue_without_a_key_field_honours_its_open_linked_task(self):
        self.write_task('t-open', issue_key='GEO-3', status='in_progress')
        for shape in ('keyed object', 'wrapped keyed object'):
            with self.subTest(shape=shape):
                self.write_snapshot(snapshot_document(shape, snapshot_entries(shape)))
                before = self.path.read_bytes()
                text = self.assert_refused(self.invoke('--issue', 'GEO-3', 'verified', '--evidence', 'r1'))
                self.assertIn('t-open', text)
                self.assertEqual(before, self.path.read_bytes())

    def test_self_dependency_is_refused_for_an_issue_identified_without_a_key_field(self):
        self.write_snapshot(snapshot_document('keyed object', snapshot_entries('keyed object')))
        before = self.path.read_bytes()
        for issue in ('GEO-2', 'GEO-3'):
            with self.subTest(issue=issue):
                self.assert_refused(self.invoke('--issue', issue, 'depends-on', issue))
                self.assertEqual(before, self.path.read_bytes())

    def test_object_key_of_an_entry_with_its_own_key_is_not_its_identity(self):
        self.write_snapshot(snapshot_document('keyed object', snapshot_entries('keyed object')))
        before = self.path.read_bytes()
        self.assert_refused(self.invoke('--issue', 'row-1', 'depends-on', 'GEO-2'))
        self.assertEqual(before, self.path.read_bytes())

    def plant(self, path):
        return self.write_snapshot({'issues': [{'key': 'GEO-1', 'local_status': 'in_progress'},
                                               {'key': 'GEO-2'}]}, path)

    def test_epic_names_that_leave_the_epic_folder_are_refused_and_nothing_outside_is_written(self):
        reaching_outside = os.path.relpath(self.outside, self.root / 'epics')
        cases = [(reaching_outside, self.outside / 'issues.json'),
                 (str(self.outside), self.outside / 'issues.json'),
                 ('..', self.root / 'issues.json'),
                 ('.', self.root / 'epics' / 'issues.json'),
                 ('', self.root / 'epics' / 'issues.json'),
                 ('EPIC-1/sub', self.root / 'epics' / 'EPIC-1' / 'sub' / 'issues.json')]
        for epic, planted in cases:
            with self.subTest(epic=epic):
                self.plant(planted)
                before, siblings = planted.read_bytes(), names_in(planted.parent)
                self.assert_refused(self.invoke('--issue', 'GEO-1', 'depends-on', 'GEO-2', epic=epic))
                self.assertEqual(before, planted.read_bytes())
                self.assertEqual(siblings, names_in(planted.parent))

    def test_symlinked_epic_folder_and_symlinked_snapshot_are_refused(self):
        target = self.plant(self.outside / 'issues.json')
        (self.root / 'epics' / 'LINKED').symlink_to(self.outside)
        file_link = self.root / 'epics' / 'EPIC-2' / 'issues.json'
        file_link.parent.mkdir()
        file_link.symlink_to(target)
        before = target.read_bytes()
        for epic in ('LINKED', 'EPIC-2'):
            with self.subTest(epic=epic):
                self.assert_refused(self.invoke('--issue', 'GEO-1', 'depends-on', 'GEO-2', epic=epic))
                self.assertEqual(before, target.read_bytes())
                self.assertEqual(['issues.json'], names_in(self.outside))

    def test_symlinked_epics_folder_is_refused_and_its_target_is_untouched(self):
        target = self.plant(self.outside / 'E-1' / 'issues.json')
        shutil.rmtree(self.root / 'epics')
        (self.root / 'epics').symlink_to(self.outside)
        before, siblings = target.read_bytes(), names_in(target.parent)
        text = self.assert_refused(self.invoke('--issue', 'GEO-1', 'depends-on', 'GEO-2', epic='E-1'))
        self.assertEqual('record_issue: The epics folder must not be a symbolic link', text.strip())
        self.assertEqual(before, target.read_bytes())
        self.assertEqual(siblings, names_in(target.parent))

    def test_failed_write_leaves_the_original_and_no_temporary_file(self):
        before = self.path.read_bytes()
        with failing_publication('EPIC-1'):
            text = self.assert_refused(self.invoke('--issue', 'GEO-1', 'depends-on', 'GEO-2'))
        self.assertNotIn('disk full', text)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(['issues.json'], names_in(self.path.parent))

    def test_write_uses_a_flushed_temporary_name_that_is_not_a_record(self):
        with watched_publication(self.path.parent) as events:
            self.run_command('--issue', 'GEO-1', 'depends-on', 'GEO-2')
        names = [name for name, _ in events]
        published = next(index for index, name in enumerate(names) if name in PUBLISHING_CALLS)
        self.assertIn('fsync', names[:published])
        leaked = [n for _, listing in events for n in listing if n.endswith('.json') and n != 'issues.json']
        self.assertEqual([], leaked)
        self.assertEqual(['issues.json'], names_in(self.path.parent))

    def test_update_waits_for_the_exclusive_lock_on_the_epic_folder(self):
        worker = threading.Thread(target=self.run_command, args=('--issue', 'GEO-1', 'depends-on', 'GEO-2'))
        with locked_directory(self.path.parent):
            worker.start()
            worker.join(0.5)
            self.assertTrue(worker.is_alive())
            self.assertNotIn('depends_on', self.issue('GEO-1'))
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(['GEO-2'], self.issue('GEO-1')['depends_on'])

    def test_unreadable_or_missing_snapshots_and_unknown_epics_refuse_in_one_line(self):
        (self.root / 'epics' / 'EPIC-EMPTY').mkdir()
        self.assert_refused(self.invoke('--issue', 'GEO-1', 'depends-on', 'GEO-2', epic='EPIC-EMPTY'))
        self.assert_refused(self.invoke('--issue', 'GEO-1', 'depends-on', 'GEO-2', epic='EPIC-UNKNOWN'))
        for content in (b'{broken', b'\xff\xfe\x00', b''):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.assert_refused(self.invoke('--issue', 'GEO-1', 'depends-on', 'GEO-2'))
                self.assertEqual(content, self.path.read_bytes())

    def test_malformed_task_record_refuses_verification_in_one_line(self):
        task = self.root / 'tasks' / 'proj' / 't-broken.json'
        task.parent.mkdir(parents=True)
        for content in (b'{"token": "' + SECRET.encode(), b'\xff\xfe\x00'):
            with self.subTest(content=content):
                task.write_bytes(content)
                before = self.path.read_bytes()
                self.assert_refused(self.invoke('--issue', 'GEO-2', 'verified', '--evidence', 'r1'))
                self.assertEqual(before, self.path.read_bytes())

    def test_recorded_command_prints_its_summary_line_and_attempts_no_network(self):
        status, text = self.invoke('--issue', 'GEO-1', 'depends-on', 'GEO-2')
        self.assertEqual(0, status)
        self.assertEqual({'epic': 'EPIC-1', 'issue': 'GEO-1', 'recorded': 'depends-on'}, json.loads(text))
        self.assertFalse(self.connect.called)

    def test_no_secret_reaches_the_output(self):
        (self.root / 'jira-token').write_text(SECRET)
        with mock.patch.dict(os.environ, {'JIRA_API_TOKEN': SECRET}):
            outcomes = [self.invoke('--issue', 'GEO-1', 'depends-on', 'GEO-2'),
                        self.invoke('--issue', 'GEO-9', 'depends-on', 'GEO-2'),
                        self.invoke('--issue', 'GEO-1', 'depends-on', 'GEO-2', epic='EPIC-UNKNOWN')]
        self.assertNotIn(SECRET, ''.join(text for _, text in outcomes))
        self.assertFalse(self.connect.called)


if __name__ == '__main__':
    unittest.main()
