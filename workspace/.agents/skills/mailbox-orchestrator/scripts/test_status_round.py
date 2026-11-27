import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import context_pack
import receive
import status_round
from status_round import FAILURE_LINE, MAILBOX_LINE, USAGE_LINE, MailboxError, UsageError, main

SCRIPT = Path(status_round.__file__).resolve()
ORCH = 'orch-session'
SLOT = '2026-11-27T10:00:00Z'
PREVIOUS_SLOT = '2026-11-27T09:45:00Z'
AT = '2026-11-27T10:07:30+00:00'
NEXT_AT = '2026-11-27T10:15:00+00:00'
TASK = '11111111-2222-4333-8444-555555555555'
OTHER_TASK = '66666666-7777-4888-9999-000000000000'
LOW_ID = '00000000-0000-4000-8000-00000000000a'
HIGH_ID = 'ffffffff-ffff-4fff-8fff-fffffffffffb'
MAX_BYTES = 4 * 1024 * 1024
UTC_Z = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')


BROKEN_CONTEXT_PACK = "'''A context_pack whose import fails with a raw operating-system text.'''\nraise RuntimeError(\"[Errno 13] Permission denied: '/data/secret/context_pack.py'\")\n"


def scripts_copy(parent, context_pack_source=None):
    """A copy of the scripts, deep enough below the filesystem root, without context_pack or with a replacement."""
    scripts = Path(parent) / 'a/b/c/d/scripts'
    shutil.copytree(SCRIPT.parent, scripts, ignore=shutil.ignore_patterns('test_*', '__pycache__', 'context_pack.py'))
    if context_pack_source is not None:
        (scripts / 'context_pack.py').write_text(context_pack_source)
    return scripts


def install_layout(mailbox):
    """The installed layout: scripts below .agents/skills and the application's top-level modules in dashboard/."""
    scripts = mailbox / '.agents/skills/mailbox-orchestrator/scripts'
    shutil.copytree(SCRIPT.parent, scripts, ignore=shutil.ignore_patterns('test_*', '__pycache__'))
    (mailbox / 'dashboard').mkdir()
    for module in SCRIPT.parents[5].glob('*.py'):
        shutil.copy(module, mailbox / 'dashboard' / module.name)
    return scripts


def file_contents(root, *, skipping=()):
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob('*'))
            if path.is_file() and path.relative_to(root).parts[0] not in skipping}


def snapshot(root):
    """Every path under root with its bytes and mtime, so any write or touch shows up."""
    entries = {}
    for path in sorted(root.rglob('*')):
        name = str(path.relative_to(root))
        if path.is_symlink():
            entries[name] = ('link', os.readlink(path))
        elif path.is_dir():
            entries[name] = ('dir',)
        else:
            entries[name] = (path.read_bytes(), path.stat().st_mtime_ns)
    return entries


class StatusRoundTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.write('projects.json', {'projects': [{'id': 'proj'}, {'id': 'other'}]})
        for folder in ('proj', 'other', 'orchestrator'):
            (self.root / 'inbox' / folder).mkdir(parents=True)
        self.register('proj--a', 'proj', 'sess-a')

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def register(self, name, project, session_id, role='worker', **fields):
        return self.write(f'sessions/{name}.json',
                          {'role': role, 'project': project, 'session_id': session_id, **fields})

    def run_main(self, *args):
        with redirect_stdout(io.StringIO()):
            return main(['--root', str(self.root), *args])

    def request(self, at=AT):
        return self.run_main('request', '--endpoint', ORCH, '--at', at)

    def collect(self, slot=SLOT):
        return self.run_main('collect', '--endpoint', ORCH, '--slot', slot)

    def request_id(self, project='proj', session_id='sess-a', slot=SLOT):
        return str(uuid.uuid5(status_round.NAMESPACE, f'status-request:{slot}:{project}:{session_id}'))

    def reply_message(self, reply_id, **fields):
        """A report made only by filling the placeholders of the request's expected_reply."""
        message = status_round.build_expected_reply('proj', 'sess-a', ORCH, self.request_id())
        del message['task_id'], message['blocker']
        message.update(id=reply_id, created_at='2026-11-27T10:09:00Z', status='working', task_ids=[],
                       summary='ok')
        message.update(fields)
        return message

    def reply(self, reply_id=None, folder='inbox', **fields):
        reply_id = reply_id or str(uuid.uuid4())
        self.write(f'{folder}/orchestrator/{reply_id}.json', self.reply_message(reply_id, **fields))
        return reply_id

    def blocked_reply(self, blocker=None, **fields):
        self.write(f'tasks/proj/{TASK}.json', {'id': TASK})
        blocker = {'context': 'CI is red', 'question': 'Skip the flaky suite?'} if blocker is None else blocker
        return self.reply(status='blocked', task_id=TASK, blocker=blocker, **fields)

    def run_cli(self, *args, root=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--root', str(root or self.root), *args],
            capture_output=True, text=True, timeout=60)

    def inbox_files(self, project='proj'):
        return sorted(path.name for path in (self.root / 'inbox' / project).iterdir())

    def published_message(self, project='proj', slot=SLOT, session_id='sess-a'):
        path = self.root / 'inbox' / project / f'{self.request_id(project, session_id, slot)}.json'
        return json.loads(path.read_text())


class RequestPublicationTest(StatusRoundTestCase):
    def test_publishes_one_non_executable_request_per_worker(self):
        self.register('other--b', 'other', 'sess-b')
        result = self.request()
        self.assertEqual(result['slot'], SLOT)
        self.assertEqual(sorted(result['published']), sorted([
            f'inbox/proj/{self.request_id()}.json', f'inbox/other/{self.request_id("other", "sess-b")}.json']))
        self.assertEqual(result['existing'], [])
        self.assertEqual(result['skipped'], [])
        for project, session_id in (('proj', 'sess-a'), ('other', 'sess-b')):
            with self.subTest(project=project):
                message = self.published_message(project, session_id=session_id)
                self.assertEqual(message['type'], 'status_request')
                self.assertIs(message['executable'], False)
                self.assertEqual((message['from'], message['from_session_id']), ('orchestrator', ORCH))
                self.assertEqual((message['to_project'], message['to_session_id']), (project, session_id))
                self.assertEqual(message['intended_endpoint'], session_id)
                self.assertEqual(message['slot'], SLOT)
                self.assertEqual(self.inbox_files(project), [message['id'] + '.json'])

    def test_message_ids_are_deterministic_uuids_of_slot_project_and_endpoint(self):
        self.request()
        message = self.published_message()
        self.assertEqual(message['id'], self.request_id())
        self.assertEqual(str(uuid.UUID(message['id'])), message['id'])
        self.assertNotEqual(self.request_id(), self.request_id(slot=PREVIOUS_SLOT))
        self.assertNotEqual(self.request_id(), self.request_id(session_id='sess-z'))
        self.assertNotEqual(self.request_id(), self.request_id(project='other'))

    def test_timestamps_are_utc_with_z_suffix_and_the_slot_is_floored(self):
        for at, created_at in ((AT, '2026-11-27T10:07:30Z'), ('2026-11-27T12:07:30+02:00', '2026-11-27T10:07:30Z')):
            with self.subTest(at=at):
                message_dir = self.root / 'inbox' / 'proj'
                for leftover in message_dir.iterdir():
                    leftover.unlink()
                result = self.request(at)
                message = self.published_message()
                self.assertEqual(result['slot'], SLOT)
                self.assertEqual(message['created_at'], created_at)
                self.assertRegex(message['created_at'], UTC_Z)
                self.assertRegex(message['slot'], UTC_Z)

    def test_request_asks_for_status_and_for_blocker_context_and_question(self):
        self.request()
        message = self.published_message()
        self.assertIn('status', message['body'])
        self.assertIn('context of the blocker', message['body'])
        self.assertIn('one concrete question', message['body'])
        expected = message['expected_reply']
        self.assertEqual(expected['type'], 'status_report')
        self.assertEqual(expected['to'], 'orchestrator')
        self.assertEqual(expected['to_session_id'], ORCH)
        self.assertEqual(expected['in_reply_to'], message['id'])
        self.assertEqual((expected['from_project'], expected['from_session_id']), ('proj', 'sess-a'))
        self.assertEqual(expected['status'], 'working|idle|waiting|blocked')
        self.assertEqual(set(expected['blocker']), {'context', 'question', 'options'})
        self.assertIn('task_id', expected)
        self.assertIn('task_ids', expected)
        self.assertIn('id', expected)

    def test_no_sessions_directory_publishes_nothing(self):
        for registration in (self.root / 'sessions').iterdir():
            registration.unlink()
        (self.root / 'sessions').rmdir()
        result = self.request()
        self.assertEqual((result['published'], result['existing'], result['skipped']), ([], [], []))

    def test_files_without_json_suffix_in_sessions_are_ignored(self):
        (self.root / 'sessions' / 'notes.txt').write_text('{"role": "worker"}')
        result = self.request()
        self.assertEqual(result['skipped'], [])
        self.assertEqual(len(result['published']), 1)

    def test_default_time_comes_from_the_clock_and_collect_default_slot_matches(self):
        clock = status_round.datetime(2026, 11, 27, 10, 14, 59, tzinfo=status_round.timezone.utc)
        with mock.patch.object(status_round, 'utc_now', return_value=clock):
            requested = self.run_main('request', '--endpoint', ORCH)
            collected = self.run_main('collect', '--endpoint', ORCH)
        self.assertEqual(requested['slot'], SLOT)
        self.assertEqual(collected['slot'], SLOT)
        self.assertEqual(self.published_message()['created_at'], '2026-11-27T10:14:59Z')


class RequestIdempotenceTest(StatusRoundTestCase):
    def test_rerun_in_the_same_slot_publishes_nothing_and_changes_no_file(self):
        self.request()
        before = snapshot(self.root)
        for at in (AT, '2026-11-27T10:14:59+00:00'):
            with self.subTest(at=at):
                result = self.request(at)
                self.assertEqual(result['published'], [])
                self.assertEqual(result['existing'], [f'inbox/proj/{self.request_id()}.json'])
                self.assertEqual(snapshot(self.root), before)

    def test_next_slot_publishes_a_fresh_request(self):
        self.request()
        result = self.request(NEXT_AT)
        next_id = self.request_id(slot='2026-11-27T10:15:00Z')
        self.assertEqual(result['slot'], '2026-11-27T10:15:00Z')
        self.assertEqual(result['published'], [f'inbox/proj/{next_id}.json'])
        self.assertEqual(result['superseded'], [f'archive/proj/{self.request_id()}.json'])
        self.assertEqual(self.inbox_files(), [f'{next_id}.json'])

    def test_message_already_in_the_inbox_is_left_untouched(self):
        existing = self.write(f'inbox/proj/{self.request_id()}.json', {'different': 'content'})
        before = existing.read_bytes()
        result = self.request()
        self.assertEqual((result['published'], result['existing']), ([], [f'inbox/proj/{self.request_id()}.json']))
        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(self.inbox_files(), [existing.name])

    def test_archived_message_is_not_republished(self):
        archived = self.write(f'archive/proj/{self.request_id()}.json', {'different': 'content'})
        before = archived.read_bytes()
        result = self.request()
        self.assertEqual((result['published'], result['existing']), ([], [f'inbox/proj/{self.request_id()}.json']))
        self.assertEqual(archived.read_bytes(), before)
        self.assertEqual(self.inbox_files(), [])

    def test_archive_appearing_after_the_link_removes_only_the_own_link(self):
        archived = self.root / 'archive' / 'proj' / f'{self.request_id()}.json'
        real_link = os.link

        def link_then_archive(source, target):
            real_link(source, target)
            archived.parent.mkdir(parents=True, exist_ok=True)
            archived.write_text('{"archived": true}')

        with mock.patch.object(os, 'link', side_effect=link_then_archive):
            result = self.request()
        self.assertEqual((result['published'], result['existing']), ([], [f'inbox/proj/{self.request_id()}.json']))
        self.assertEqual(archived.read_text(), '{"archived": true}')
        self.assertEqual(self.inbox_files(), [])

    def test_worker_archiving_the_linked_request_before_cleanup_is_counted_existing(self):
        name = f'{self.request_id()}.json'
        archived = self.root / 'archive' / 'proj' / name
        real_link = os.link

        def link_then_worker_archives(source, target):
            real_link(source, target)
            archived.parent.mkdir(parents=True, exist_ok=True)
            os.rename(target, archived)

        with mock.patch.object(os, 'link', side_effect=link_then_worker_archives):
            result = self.request()
        self.assertEqual((result['published'], result['existing']), ([], [f'inbox/proj/{name}']))
        self.assertEqual(json.loads(archived.read_text())['id'], self.request_id())
        self.assertEqual(self.inbox_files(), [])
        self.assertEqual([path.name for path in archived.parent.iterdir()], [name])

    def test_concurrent_publisher_winning_the_link_is_counted_existing_and_leaves_no_temp(self):
        with mock.patch.object(os, 'link', side_effect=FileExistsError):
            result = self.request()
        self.assertEqual((result['published'], result['existing']), ([], [f'inbox/proj/{self.request_id()}.json']))
        self.assertEqual(self.inbox_files(), [])

    def test_failed_publish_raises_and_leaves_no_temporary_or_final_file(self):
        disk_full = OSError(28, 'No space left on device')
        with mock.patch.object(os, 'link', side_effect=disk_full), self.assertRaises(OSError):
            self.request()
        self.assertEqual(self.inbox_files(), [])


class WorkerDiscoveryTest(StatusRoundTestCase):
    def test_duplicate_registrations_of_one_worker_get_one_request(self):
        self.register('proj--a-again', 'proj', 'sess-a')
        result = self.request()
        self.assertEqual(result['published'], [f'inbox/proj/{self.request_id()}.json'])
        self.assertEqual(self.inbox_files(), [f'{self.request_id()}.json'])

    def test_two_sessions_of_one_project_each_get_a_request(self):
        self.register('proj--b', 'proj', 'sess-b')
        result = self.request()
        self.assertEqual(len(result['published']), 2)
        self.assertEqual(len(self.inbox_files()), 2)

    def test_orchestrator_role_registration_is_excluded_without_a_skip_entry(self):
        self.register('orch', 'proj', ORCH, role='orchestrator')
        result = self.request()
        self.assertEqual(result['published'], [f'inbox/proj/{self.request_id()}.json'])
        self.assertEqual(result['skipped'], [])

    def test_null_session_id_falls_back_to_the_agent_id(self):
        self.register('proj--agent', 'proj', None, agent_id='agent-7')
        result = self.request()
        self.assertIn(f'inbox/proj/{self.request_id(session_id="agent-7")}.json', result['published'])
        self.assertEqual(self.published_message(session_id='agent-7')['to_session_id'], 'agent-7')

    def test_registration_with_neither_session_nor_agent_is_skipped(self):
        self.register('proj--none', 'proj', None)
        result = self.request()
        self.assertEqual(result['skipped'], [{'registration': 'sessions/proj--none.json',
                                              'reason': 'worker endpoint is missing'}])
        self.assertEqual(len(result['published']), 1)

    def test_unusable_registrations_are_skipped_with_a_fixed_reason(self):
        valid = {'role': 'worker', 'project': 'proj', 'session_id': 'sess-x'}
        outside = self.root / 'outside.json'
        outside.write_text(json.dumps(valid))
        oversized = json.dumps(valid) + ' ' * MAX_BYTES
        cases = {
            'symlinked': ('unreadable', lambda path: path.symlink_to(outside)),
            'oversized': ('unreadable', lambda path: path.write_text(oversized)),
            'non-finite': ('unreadable', lambda path: path.write_text(
                '{"role": "worker", "project": "proj", "session_id": "s", "load": NaN}')),
            'not an object': ('unreadable', lambda path: path.write_text('["worker"]')),
            'escaping project': ('invalid', lambda path: path.write_text(json.dumps(dict(valid, project='../x')))),
            'absolute project': ('invalid', lambda path: path.write_text(json.dumps(dict(valid, project='/etc')))),
            'non-string project': ('invalid', lambda path: path.write_text(json.dumps(dict(valid, project=3)))),
            'unregistered project': ('unregistered', lambda path: path.write_text(
                json.dumps(dict(valid, project='ghost')))),
            'orchestrator project': ('unregistered', lambda path: path.write_text(
                json.dumps(dict(valid, project='orchestrator')))),
        }
        reasons = {'unreadable': 'registration is unreadable', 'invalid': 'project name is invalid',
                   'unregistered': 'project is not registered'}
        (self.root / 'sessions' / 'proj--a.json').unlink()
        for name, (reason, build) in cases.items():
            with self.subTest(case=name):
                registration = self.root / 'sessions' / 'bad.json'
                build(registration)
                result = self.request()
                registration.unlink()
                self.assertEqual(result['skipped'], [{'registration': 'sessions/bad.json',
                                                      'reason': reasons[reason]}])
                self.assertEqual((result['published'], result['existing']), ([], []))
                self.assertEqual(self.inbox_files(), [])

    def test_symlinked_or_missing_project_inbox_is_refused(self):
        self.register('other--b', 'other', 'sess-b')
        (self.root / 'inbox' / 'other').rmdir()
        (self.root / 'inbox' / 'other').symlink_to(self.root / 'inbox' / 'proj')
        result = self.request()
        self.assertEqual(result['skipped'], [{'registration': 'sessions/other--b.json',
                                              'reason': 'inbox of the project is missing or unsafe'}])
        self.assertEqual(len(result['published']), 1)
        (self.root / 'inbox' / 'other').unlink()
        result = self.request()
        self.assertEqual([entry['reason'] for entry in result['skipped']],
                         ['inbox of the project is missing or unsafe'])
        self.assertEqual(self.inbox_files(), [f'{self.request_id()}.json'])


class FixedErrorLineTest(StatusRoundTestCase):
    def assert_fixed_failure(self, completed, line, code):
        self.assertEqual(completed.returncode, code)
        self.assertEqual(completed.stderr, line + '\n')
        self.assertEqual(completed.stdout, '')
        self.assertNotIn(str(self.root), completed.stderr)
        self.assertNotIn('Traceback', completed.stderr)

    def test_invalid_time_or_slot_ends_with_the_fixed_line_and_exit_2(self):
        cases = {
            'naive --at': ('request', '--at', '2026-11-27T10:07:00'),
            'garbage --at': ('request', '--at', 'yesterday'),
            'naive --slot': ('collect', '--slot', '2026-11-27T10:00:00'),
            'unaligned --slot': ('collect', '--slot', '2026-11-27T10:07:00Z'),
            'seconds in --slot': ('collect', '--slot', '2026-11-27T10:00:30Z'),
            'garbage --slot': ('collect', '--slot', 'now'),
        }
        for name, (command, option, value) in cases.items():
            with self.subTest(case=name):
                completed = self.run_cli(command, '--endpoint', ORCH, option, value)
                self.assert_fixed_failure(completed, USAGE_LINE, 2)
        self.assertEqual(USAGE_LINE, 'status_round: invalid time or slot')

    def test_time_that_overflows_utc_ends_with_the_fixed_line_and_exit_2(self):
        overflow = '9999-12-31T23:59:00-01:00'
        for command, option in (('request', '--at'), ('collect', '--slot')):
            with self.subTest(command=command):
                completed = self.run_cli(command, '--endpoint', ORCH, option, overflow)
                self.assert_fixed_failure(completed, USAGE_LINE, 2)

    def test_invalid_time_or_slot_raises_usage_error_from_main(self):
        for command, option, value in (('request', '--at', 'x'), ('collect', '--slot', '2026-11-27T10:07:00Z')):
            with self.subTest(option=option), self.assertRaises(UsageError):
                self.run_main(command, '--endpoint', ORCH, option, value)

    def test_unreadable_root_or_registry_ends_with_the_fixed_line_and_exit_1(self):
        (self.root / 'projects.json').unlink()
        self.assert_fixed_failure(self.run_cli('request', '--endpoint', ORCH, '--at', AT), MAILBOX_LINE, 1)
        missing_root = self.root / 'gone'
        completed = self.run_cli('collect', '--endpoint', ORCH, root=missing_root)
        self.assertEqual((completed.returncode, completed.stderr), (1, MAILBOX_LINE + '\n'))
        self.assertNotIn(str(missing_root), completed.stderr)

    def test_unlistable_sessions_directory_ends_with_the_fixed_line(self):
        for registration in (self.root / 'sessions').iterdir():
            registration.unlink()
        (self.root / 'sessions').rmdir()
        (self.root / 'sessions').write_text('not a directory')
        self.assert_fixed_failure(self.run_cli('request', '--endpoint', ORCH, '--at', AT), MAILBOX_LINE, 1)

    def run_as_script(self, *args):
        stderr = io.StringIO()
        argv = [str(SCRIPT), '--root', str(self.root), *args]
        with mock.patch.object(sys, 'argv', argv), redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(SCRIPT), run_name='__main__')
        return raised.exception.code, stderr.getvalue()

    def test_raw_registry_failure_text_is_replaced_by_the_fixed_line(self):
        raw = PermissionError(13, 'Permission denied', str(self.root / 'projects.json'))
        with mock.patch.object(receive, 'project_ids', side_effect=raw):
            code, stderr = self.run_as_script('request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual(code, MAILBOX_LINE)
        self.assertNotIn(str(self.root), f'{code}{stderr}')
        with mock.patch.object(receive, 'project_ids', side_effect=raw), self.assertRaises(MailboxError) as raised:
            self.request()
        self.assertEqual(str(raised.exception), MAILBOX_LINE)
        self.assertNotIn('Permission denied', str(raised.exception))

    def test_raw_record_failure_text_is_replaced_by_the_fixed_line(self):
        real_read = receive.read_json
        raw = PermissionError(13, 'Permission denied', str(self.root / 'sessions' / 'proj--a.json'))

        def read(path):
            if path.parent.name == 'sessions':
                raise raw
            return real_read(path)

        with mock.patch.object(receive, 'read_json', side_effect=read):
            code, stderr = self.run_as_script('request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual(code, FAILURE_LINE)
        self.assertNotIn(str(self.root), f'{code}{stderr}')
        self.assertNotIn('Permission denied', f'{code}{stderr}')

    def test_usage_failure_under_script_entry_exits_2_with_the_fixed_line(self):
        code, stderr = self.run_as_script('request', '--endpoint', ORCH, '--at', 'x')
        self.assertEqual((code, stderr), (2, USAGE_LINE + '\n'))

    def test_successful_cli_run_prints_the_result_json(self):
        completed = self.run_cli('request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)['published'], [f'inbox/proj/{self.request_id()}.json'])


class CollectTest(StatusRoundTestCase):
    def worker_entry(self, report, project='proj'):
        return next(entry for entry in report['workers'] if entry['project'] == project)

    def test_worker_without_a_reply_is_reported_missing(self):
        report = self.collect()
        request = self.request_id()
        self.assertEqual(report['slot'], SLOT)
        self.assertEqual(report['workers'], [{'project': 'proj', 'session_id': 'sess-a', 'request': request,
                                              'reply': None, 'status': None}])
        self.assertEqual(report['missing'], [{'project': 'proj', 'session_id': 'sess-a', 'request': request}])
        self.assertEqual((report['decisions_needed'], report['incomplete']), ([], []))

    def test_valid_reply_is_reported_with_its_path_and_status(self):
        reply_id = self.reply(status='idle')
        report = self.collect()
        self.assertEqual(report['missing'], [])
        entry = self.worker_entry(report)
        self.assertEqual((entry['reply'], entry['status']), (f'inbox/orchestrator/{reply_id}.json', 'idle'))

    def test_reply_with_an_unknown_status_is_listed_without_a_status(self):
        reply_id = self.reply(status='bogus')
        entry = self.worker_entry(self.collect())
        self.assertEqual((entry['reply'], entry['status']), (f'inbox/orchestrator/{reply_id}.json', None))

    def test_reply_to_an_older_slot_is_ignored(self):
        self.reply(in_reply_to=self.request_id(slot=PREVIOUS_SLOT))
        report = self.collect()
        self.assertEqual(len(report['missing']), 1)
        self.assertIsNone(self.worker_entry(report)['reply'])

    def test_replies_that_do_not_match_the_worker_are_ignored(self):
        cases = {
            'wrong project': {'from_project': 'other'},
            'wrong session': {'from_session_id': 'sess-z'},
            'wrong in_reply_to': {'in_reply_to': str(uuid.uuid4())},
            'wrong to_session_id': {'to_session_id': 'someone-else'},
            'wrong type': {'type': 'note'},
            'wrong recipient': {'to': 'other'},
            'wrong id': {'id': str(uuid.uuid4())},
            'naive created_at': {'created_at': '2026-11-27T10:09:00'},
            'unregistered sender': {'from_project': 'ghost'},
        }
        for name, fields in cases.items():
            with self.subTest(case=name):
                reply_id = self.reply(**fields)
                report = self.collect()
                (self.root / 'inbox' / 'orchestrator' / f'{reply_id}.json').unlink()
                self.assertEqual(len(report['missing']), 1)
                self.assertIsNone(self.worker_entry(report)['reply'])

    def test_reply_without_an_id_is_listed_as_missing(self):
        reply_id = str(uuid.uuid4())
        message = self.reply_message(reply_id)
        del message['id']
        self.write(f'inbox/orchestrator/{reply_id}.json', message)
        report = self.collect()
        self.assertEqual(len(report['missing']), 1)
        self.assertIsNone(self.worker_entry(report)['reply'])

    def test_reply_with_an_unrepresentable_created_at_is_ignored_without_crashing(self):
        self.reply(created_at='0001-01-01T00:00:00+01:00')
        report = self.collect()
        self.assertEqual(len(report['missing']), 1)
        self.assertIsNone(self.worker_entry(report)['reply'])

    def test_reply_without_a_target_session_is_accepted(self):
        reply_id = self.reply(to_session_id=None)
        self.assertEqual(self.worker_entry(self.collect())['reply'], f'inbox/orchestrator/{reply_id}.json')

    def test_latest_created_at_wins_even_against_a_greater_id(self):
        self.reply(reply_id=HIGH_ID, created_at='2026-11-27T10:05:00Z', status='working')
        newer = self.reply(reply_id=LOW_ID, created_at='2026-11-27T10:12:00Z', status='idle')
        entry = self.worker_entry(self.collect())
        self.assertEqual((entry['reply'], entry['status']), (f'inbox/orchestrator/{newer}.json', 'idle'))

    def test_equal_created_at_goes_to_the_greatest_id(self):
        self.reply(reply_id=LOW_ID, status='working')
        winner = self.reply(reply_id=HIGH_ID, status='waiting')
        entry = self.worker_entry(self.collect())
        self.assertEqual((entry['reply'], entry['status']), (f'inbox/orchestrator/{winner}.json', 'waiting'))

    def test_archived_reply_is_counted(self):
        reply_id = self.reply(folder='archive', status='idle')
        report = self.collect()
        self.assertEqual(report['missing'], [])
        self.assertEqual(self.worker_entry(report)['reply'], f'archive/orchestrator/{reply_id}.json')

    def test_unrelated_files_in_the_orchestrator_inbox_are_ignored(self):
        inbox = self.root / 'inbox' / 'orchestrator'
        (inbox / 'notes.txt').write_text('x')
        (inbox / 'not-a-uuid.json').write_text('{}')
        (inbox / f'{LOW_ID}.json').write_text('["not an object"]')
        report = self.collect()
        self.assertEqual(len(report['missing']), 1)

    def test_complete_blocked_report_becomes_a_decision_needed(self):
        reply_id = self.blocked_reply({'context': ' CI is red ', 'question': 'Skip the flaky suite?',
                                       'options': ['skip', 3, 'wait']})
        report = self.collect()
        self.assertEqual(report['incomplete'], [])
        self.assertEqual(report['decisions_needed'], [{
            'project': 'proj', 'session_id': 'sess-a', 'reply': f'inbox/orchestrator/{reply_id}.json',
            'task_id': TASK, 'context': 'CI is red', 'question': 'Skip the flaky suite?',
            'options': ['skip', 'wait'], 'decided': False, 'decision_path': None,
            'context_node': f'task:{TASK}'}])
        self.assertEqual(self.worker_entry(report)['status'], 'blocked')

    def test_blocked_report_without_options_has_an_empty_option_list(self):
        self.blocked_reply()
        (decision,) = self.collect()['decisions_needed']
        self.assertEqual(decision['options'], [])

    def test_decision_file_marks_the_blocked_report_as_decided(self):
        reply_id = self.blocked_reply()
        self.write(f'decisions/{TASK}/{reply_id}.json', {'decision': 'skip'})
        (decision,) = self.collect()['decisions_needed']
        self.assertIs(decision['decided'], True)
        self.assertEqual(decision['decision_path'], f'decisions/{TASK}/{reply_id}.json')

    def test_blocked_report_missing_task_context_or_question_is_incomplete(self):
        good = {'context': 'CI is red', 'question': 'Skip?'}
        cases = {
            'no context': ({'question': 'Skip?'}, TASK, True, ['blocker.context']),
            'blank context': (dict(good, context='   '), TASK, True, ['blocker.context']),
            'no question': ({'context': 'CI is red'}, TASK, True, ['blocker.question']),
            'non-string question': (dict(good, question=4), TASK, True, ['blocker.question']),
            'no blocker object': ('stuck', TASK, True, ['blocker.context', 'blocker.question']),
            'no task record': (good, TASK, False, ['task_id']),
            'task id not a uuid': (good, 'task-1', True, ['task_id']),
            'no task id': (good, None, True, ['task_id']),
            'nothing': ('stuck', None, False, ['task_id', 'blocker.context', 'blocker.question']),
        }
        for name, (blocker, task_id, has_record, missing) in cases.items():
            with self.subTest(case=name):
                if has_record:
                    self.write(f'tasks/proj/{TASK}.json', {'id': TASK})
                reply_id = self.reply(status='blocked', task_id=task_id, blocker=blocker)
                report = self.collect()
                (self.root / 'inbox' / 'orchestrator' / f'{reply_id}.json').unlink()
                (self.root / 'tasks' / 'proj' / f'{TASK}.json').unlink(missing_ok=True)
                self.assertEqual(report['decisions_needed'], [])
                self.assertEqual(report['incomplete'], [{
                    'project': 'proj', 'session_id': 'sess-a', 'reply': f'inbox/orchestrator/{reply_id}.json',
                    'missing_fields': missing}])

    def test_blocked_report_naming_another_projects_task_is_incomplete(self):
        self.write(f'tasks/other/{OTHER_TASK}.json', {'id': OTHER_TASK})
        self.reply(status='blocked', task_id=OTHER_TASK,
                   blocker={'context': 'CI is red', 'question': 'Skip?'})
        report = self.collect()
        self.assertEqual(report['decisions_needed'], [])
        self.assertEqual(report['incomplete'][0]['missing_fields'], ['task_id'])

    def test_each_worker_is_reported_separately(self):
        self.register('other--b', 'other', 'sess-b')
        self.blocked_reply()
        self.reply(in_reply_to=self.request_id('other', 'sess-b'), from_project='other',
                   from_session_id='sess-b', status='idle')
        report = self.collect()
        self.assertEqual({entry['project']: entry['status'] for entry in report['workers']},
                         {'proj': 'blocked', 'other': 'idle'})
        self.assertEqual([decision['project'] for decision in report['decisions_needed']], ['proj'])

    def test_collect_leaves_the_mailbox_byte_identical(self):
        reply_id = self.blocked_reply()
        self.write(f'decisions/{TASK}/{reply_id}.json', {'decision': 'skip'})
        self.reply(status='idle', created_at='2026-11-27T10:01:00Z')
        self.reply(folder='archive')
        before = snapshot(self.root)
        self.collect()
        self.run_main('collect', '--endpoint', ORCH, '--slot', '2026-11-27T12:00:00+02:00')
        self.assertEqual(snapshot(self.root), before)

    def test_slot_with_a_utc_offset_is_normalised(self):
        self.assertEqual(self.collect('2026-11-27T12:00:00+02:00')['slot'], SLOT)

    def test_symlinked_or_missing_orchestrator_inbox_is_a_mailbox_error(self):
        inbox = self.root / 'inbox' / 'orchestrator'
        inbox.rmdir()
        with self.subTest(case='missing'), self.assertRaises(MailboxError):
            self.collect()
        inbox.symlink_to(self.root / 'inbox' / 'proj')
        with self.subTest(case='symlink'), self.assertRaises(MailboxError):
            self.collect()


class SupersedeTest(StatusRoundTestCase):
    def old_request(self, slot=PREVIOUS_SLOT, project='proj', session='sess-a', orchestrator=ORCH, name=None,
                    **fields):
        """An older status_request as the orchestrator would have published it, written into the inbox."""
        message = status_round.build_request(slot, project, session, orchestrator, '2026-11-27T09:50:00Z')
        message.update(fields)
        self.write(f'inbox/{project}/{name or message["id"]}.json', message)
        return message['id']

    def older_names(self, project='proj'):
        current = f'{self.request_id()}.json'
        return [name for name in self.inbox_files(project) if name != current]

    def archived_names(self, project='proj'):
        folder = self.root / 'archive' / project
        return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []

    def run_as_script(self, *args):
        stderr = io.StringIO()
        argv = [str(SCRIPT), '--root', str(self.root), *args]
        with mock.patch.object(sys, 'argv', argv), redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(SCRIPT), run_name='__main__')
        return raised.exception.code, stderr.getvalue()

    def test_request_supersedes_older_unanswered(self):
        first = self.old_request('2026-11-27T09:30:00Z')
        second = self.old_request(PREVIOUS_SLOT)
        result = self.request()
        self.assertEqual(sorted(result['superseded']),
                         sorted([f'archive/proj/{first}.json', f'archive/proj/{second}.json']))
        self.assertEqual(result['supersede_conflicts'], [])
        self.assertEqual(self.inbox_files(), [f'{self.request_id()}.json'])
        self.assertEqual(self.archived_names(), sorted([f'{first}.json', f'{second}.json']))
        archived = json.loads((self.root / 'archive' / 'proj' / f'{second}.json').read_text())
        self.assertEqual((archived['id'], archived['slot']), (second, PREVIOUS_SLOT))

    def test_request_supersedes_each_addressed_worker_separately(self):
        self.register('other--b', 'other', 'sess-b')
        mine = self.old_request()
        theirs = self.old_request(project='other', session='sess-b')
        result = self.request()
        self.assertEqual(sorted(result['superseded']),
                         sorted([f'archive/proj/{mine}.json', f'archive/other/{theirs}.json']))
        self.assertEqual(self.archived_names('other'), [f'{theirs}.json'])

    def test_request_keeps_answered_request(self):
        for folder in ('inbox', 'archive'):
            with self.subTest(reply_in=folder):
                old = self.old_request()
                reply_id = self.reply(folder=folder, in_reply_to=old)
                result = self.request()
                self.assertEqual((result['superseded'], result['supersede_conflicts']), ([], []))
                self.assertEqual(self.older_names(), [f'{old}.json'])
                (self.root / folder / 'orchestrator' / f'{reply_id}.json').unlink()
                (self.root / 'inbox' / 'proj' / f'{old}.json').unlink()

    def test_answer_to_previous_orchestrator_endpoint_counts(self):
        old = self.old_request(orchestrator='old-orch')
        self.reply(in_reply_to=old, to_session_id='old-orch')
        result = self.request()
        self.assertEqual(result['superseded'], [])
        self.assertEqual(self.older_names(), [f'{old}.json'])

    def test_answer_to_the_current_orchestrator_does_not_answer_a_request_of_another_endpoint(self):
        old = self.old_request(orchestrator='old-orch')
        self.reply(in_reply_to=old, to_session_id=ORCH)
        result = self.request()
        self.assertEqual(result['superseded'], [f'archive/proj/{old}.json'])

    def test_reply_without_to_session_id_counts_as_an_answer(self):
        old = self.old_request()
        self.reply(in_reply_to=old, to_session_id=None)
        self.assertEqual(self.request()['superseded'], [])
        self.assertEqual(self.older_names(), [f'{old}.json'])

    def test_reply_near_misses_do_not_answer(self):
        cases = {
            'id differs from the filename stem': {'id': str(uuid.uuid4())},
            'naive created_at': {'created_at': '2026-11-27T10:09:00'},
            'other worker': {'from_session_id': 'sess-z'},
            'other project': {'from_project': 'other'},
            'other recipient': {'to_session_id': 'someone-else'},
            'not a status_report': {'type': 'note'},
        }
        for name, fields in cases.items():
            with self.subTest(case=name):
                old = self.old_request()
                reply_id = self.reply(in_reply_to=old, **fields)
                result = self.request()
                (self.root / 'inbox' / 'orchestrator' / f'{reply_id}.json').unlink()
                self.assertEqual(result['superseded'], [f'archive/proj/{old}.json'])
                self.assertEqual(self.older_names(), [])
                (self.root / 'archive' / 'proj' / f'{old}.json').unlink()

    def test_reply_to_another_request_does_not_answer(self):
        old = self.old_request()
        self.reply(in_reply_to=self.request_id())
        self.assertEqual(self.request()['superseded'], [f'archive/proj/{old}.json'])

    def test_other_worker_and_non_request_untouched(self):
        foreign = self.old_request(session='sess-z')
        other_project = self.old_request(project='other', session='sess-b')
        note = str(uuid.uuid4())
        self.write(f'inbox/proj/{note}.json', {'id': note, 'type': 'task_request', 'slot': PREVIOUS_SLOT})
        (self.root / 'inbox' / 'proj' / 'notes.txt').write_text('keep')
        before = snapshot(self.root / 'inbox')
        result = self.request()
        self.assertEqual((result['superseded'], result['supersede_conflicts']), ([], []))
        after = snapshot(self.root / 'inbox')
        for name in (f'other/{other_project}.json', f'proj/{foreign}.json', f'proj/{note}.json', 'proj/notes.txt'):
            self.assertEqual(after[name], before[name])
        self.assertFalse((self.root / 'archive').exists())

    def test_older_request_near_misses_stay_in_the_inbox(self):
        other_id = str(uuid.uuid4())
        outside = self.root / 'outside.json'
        cases = {
            'later slot': lambda: self.old_request('2026-11-27T10:15:00Z'),
            'naive slot': lambda: self.old_request('2026-11-27T09:45:00'),
            'misaligned slot': lambda: self.old_request('2026-11-27T09:50:00Z'),
            'unparseable slot': lambda: self.old_request('soon'),
            'missing slot': lambda: self.old_request(None),
            'id not derived from the slot': lambda: self.old_request(id=other_id, name=other_id),
            'id differs from the filename': lambda: self.old_request(id=other_id),
            'executable': lambda: self.old_request(executable=True),
            'executable missing': lambda: self.old_request(executable=None),
            'not from the orchestrator': lambda: self.old_request(**{'from': 'worker'}),
            'no orchestrator endpoint': lambda: self.old_request(from_session_id=None),
            'other recipient project': lambda: self.old_request(to_project='other'),
            'other recipient endpoint': lambda: self.old_request(to_session_id='sess-z'),
            'other type': lambda: self.old_request(type='task_request'),
            'filename not a uuid': lambda: self.old_request(name='older-request'),
        }
        for name, build in cases.items():
            with self.subTest(case=name):
                build()
                before = snapshot(self.root / 'inbox' / 'proj')
                result = self.request()
                self.assertEqual((result['superseded'], result['supersede_conflicts']), ([], []))
                self.assertEqual(self.archived_names(), [])
                after = snapshot(self.root / 'inbox' / 'proj')
                self.assertEqual({key: after[key] for key in before}, before)
                for leftover in (self.root / 'inbox' / 'proj').iterdir():
                    leftover.unlink()
        with self.subTest(case='symlink'):
            valid = status_round.build_request(PREVIOUS_SLOT, 'proj', 'sess-a', ORCH, '2026-11-27T09:50:00Z')
            outside.write_text(json.dumps(valid))
            link = self.root / 'inbox' / 'proj' / f'{valid["id"]}.json'
            link.symlink_to(outside)
            result = self.request()
            self.assertEqual(result['superseded'], [])
            self.assertTrue(link.is_symlink())
        with self.subTest(case='not an object'):
            link.unlink()
            (self.root / 'inbox' / 'proj' / f'{valid["id"]}.json').write_text('["x"]')
            self.assertEqual(self.request()['superseded'], [])

    def test_later_slot_request_is_kept_and_counted_in_the_inbox(self):
        later = self.old_request('2026-11-27T10:15:00Z')
        earlier = self.old_request('2026-11-27T09:30:00Z')
        result = self.request()
        self.assertEqual(result['superseded'], [f'archive/proj/{earlier}.json'])
        self.assertEqual(self.inbox_files(), sorted([f'{later}.json', f'{self.request_id()}.json']))

    def test_same_slot_rerun_noop(self):
        self.old_request()
        first = self.request()
        self.assertEqual(len(first['superseded']), 1)
        before = snapshot(self.root)
        second = self.request()
        self.assertEqual((second['superseded'], second['supersede_conflicts']), ([], []))
        self.assertEqual(snapshot(self.root), before)

    def test_existing_archive_name_listed_as_conflict(self):
        old = self.old_request()
        archived = self.write(f'archive/proj/{old}.json', {'archived': 'earlier'})
        before = archived.read_bytes()
        result = self.request()
        self.assertEqual(result['superseded'], [])
        self.assertEqual(result['supersede_conflicts'], [f'inbox/proj/{old}.json'])
        self.assertEqual(archived.read_bytes(), before)
        self.assertEqual(self.older_names(), [f'{old}.json'])

    def test_worker_archived_before_link_is_not_an_error_or_a_conflict(self):
        old = self.old_request()
        with mock.patch.object(os, 'link', side_effect=self.real_link_unless_archiving(FileNotFoundError)):
            result = self.request()
        self.assertEqual((result['superseded'], result['supersede_conflicts']), ([], []))
        self.assertEqual(self.archived_names(), [])
        self.assertTrue((self.root / 'inbox' / 'proj' / f'{old}.json').exists())

    def real_link_unless_archiving(self, error):
        real_link = os.link

        def link(source, target):
            if Path(target).parent.parent.name == 'archive':
                raise error
            return real_link(source, target)

        return link

    def test_worker_archived_between_link_and_unlink_is_not_an_error(self):
        old = self.old_request()
        source = self.root / 'inbox' / 'proj' / f'{old}.json'
        real_link = os.link

        def link_then_worker_removes_inbox_copy(origin, target):
            real_link(origin, target)
            if Path(target).parent.parent.name == 'archive':
                source.unlink()

        with mock.patch.object(os, 'link', side_effect=link_then_worker_removes_inbox_copy):
            result = self.request()
        self.assertEqual(result['supersede_conflicts'], [])
        self.assertEqual(self.older_names(), [])
        self.assertEqual(self.archived_names(), [f'{old}.json'])

    def test_archive_symlink_refused_before_mkdir(self):
        outside = self.root.parent / f'{self.root.name}-outside'
        self.addCleanup(lambda: outside.exists() and outside.rmdir())
        outside.mkdir()
        cases = {
            'archive': (self.root / 'archive', outside),
            'archive project': (self.root / 'archive' / 'proj', outside),
        }
        for name, (link, target) in cases.items():
            with self.subTest(case=name):
                old = self.old_request()
                link.parent.mkdir(exist_ok=True)
                link.symlink_to(target)
                with self.assertRaises(MailboxError) as raised:
                    self.request()
                self.assertEqual(str(raised.exception), MAILBOX_LINE)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertTrue((self.root / 'inbox' / 'proj' / f'{old}.json').exists())
                link.unlink()
                (self.root / 'inbox' / 'proj' / f'{old}.json').unlink()

    def test_dangling_archive_symlink_is_not_created_through(self):
        missing = self.root.parent / f'{self.root.name}-missing'
        self.old_request()
        (self.root / 'archive').symlink_to(missing)
        with self.assertRaises(MailboxError):
            self.request()
        self.assertFalse(missing.exists())

    def test_archive_project_regular_file_ends_with_the_fixed_failure_line(self):
        old = self.old_request()
        self.write('archive/proj', {'not': 'a directory'})
        code, stderr = self.run_as_script('request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual(code, FAILURE_LINE)
        self.assertEqual(stderr, '')
        self.assertTrue((self.root / 'inbox' / 'proj' / f'{old}.json').exists())

    def test_missing_orchestrator_inbox_ends_with_the_mailbox_line_and_archives_nothing(self):
        old = self.old_request()
        (self.root / 'inbox' / 'orchestrator').rmdir()
        completed = self.run_cli('request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual((completed.returncode, completed.stdout, completed.stderr),
                         (1, '', MAILBOX_LINE + '\n'))
        self.assertTrue((self.root / 'inbox' / 'proj' / f'{self.request_id()}.json').exists())
        self.assertEqual(self.older_names(), [f'{old}.json'])
        self.assertFalse((self.root / 'archive').exists())

    def test_archive_regular_file_ends_with_the_fixed_failure_line(self):
        self.old_request()
        (self.root / 'archive').write_text('not a directory')
        completed = self.run_cli('request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual((completed.returncode, completed.stderr, completed.stdout),
                         (1, FAILURE_LINE + '\n', ''))

    def test_raw_os_error_text_is_never_printed_when_archiving_fails(self):
        self.old_request()
        real_link = os.link
        raw = f'Input/output error: {self.root}/archive/proj'

        def failing_link(source, target):
            if Path(target).parent.parent.name == 'archive':
                raise OSError(5, raw, str(target))
            return real_link(source, target)

        with mock.patch.object(os, 'link', side_effect=failing_link):
            code, stderr = self.run_as_script('request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual(code, FAILURE_LINE)
        self.assertNotIn('Input/output', f'{code}{stderr}')
        self.assertNotIn(str(self.root), f'{code}{stderr}')

    def test_raw_os_error_text_is_never_printed_when_the_inbox_copy_cannot_be_removed(self):
        old = self.old_request()
        real_unlink = Path.unlink

        def failing_unlink(path, *args, **kwargs):
            if path.name == f'{old}.json':
                raise PermissionError(13, 'Permission denied', str(path))
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, 'unlink', failing_unlink):
            code, stderr = self.run_as_script('request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual(code, FAILURE_LINE)
        self.assertNotIn('Permission denied', f'{code}{stderr}')
        self.assertNotIn(str(self.root), f'{code}{stderr}')

    def test_round_without_candidates_does_not_need_the_orchestrator_inbox(self):
        (self.root / 'inbox' / 'orchestrator').rmdir()
        result = self.request()
        self.assertEqual(result['published'], [f'inbox/proj/{self.request_id()}.json'])
        self.assertEqual((result['superseded'], result['supersede_conflicts']), ([], []))

    def test_collect_unchanged_by_superseding(self):
        self.old_request()
        self.request()
        before = snapshot(self.root)
        report = self.collect()
        self.assertEqual(set(report), {'slot', 'workers', 'missing', 'decisions_needed', 'incomplete', 'context'})
        self.assertEqual(report['missing'], [{'project': 'proj', 'session_id': 'sess-a',
                                              'request': self.request_id()}])
        self.assertEqual(snapshot(self.root), before)

    def test_cli_prints_the_superseding_result(self):
        old = self.old_request()
        completed = self.run_cli('request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual(completed.returncode, 0)
        printed = json.loads(completed.stdout)
        self.assertEqual(printed['superseded'], [f'archive/proj/{old}.json'])
        self.assertEqual(printed['supersede_conflicts'], [])


class ProtocolIntegrationTest(StatusRoundTestCase):
    def contract_report(self, request):
        report = dict(request['expected_reply'])
        report.update(id=str(uuid.uuid4()), created_at='2026-11-27T10:09:00Z', status='blocked',
                      task_ids=[TASK], summary='waiting on a decision', task_id=TASK,
                      blocker={'context': 'CI is red', 'question': 'Skip the flaky suite?'})
        return report

    def test_status_report_is_pending_for_the_orchestrator_and_reconciles_with_a_decision(self):
        self.request()
        self.write(f'tasks/proj/{TASK}.json', {'id': TASK})
        report = self.contract_report(self.published_message())
        path = self.write(f'inbox/orchestrator/{report["id"]}.json', report)
        pending = receive.pending(self.root, ORCH)
        self.assertEqual(pending, [{'id': report['id'], 'path': f'inbox/orchestrator/{path.name}',
                                    'step': 'inspect'}])
        self.assertEqual(receive.pending(self.root, 'another-orchestrator'), [])
        decision = self.write(f'decisions/{TASK}/{report["id"]}.json', {'decision': 'skip'})
        self.assertEqual(receive.pending(self.root, ORCH), [{
            'id': report['id'], 'path': f'inbox/orchestrator/{path.name}', 'step': 'reconcile',
            'decision_path': str(decision.relative_to(self.root))}])
        (decided,) = self.collect()['decisions_needed']
        self.assertIs(decided['decided'], True)
        self.assertEqual(decided['reply'], f'inbox/orchestrator/{path.name}')


class CollectContextTest(StatusRoundTestCase):
    UNAVAILABLE = {'available': False, 'packs': {}, 'omitted': 0}
    LEAKS = ('Errno', 'Permission denied', '/data/', 'Traceback')
    RAW_FAILURE = "[Errno 13] Permission denied: '/data/tasks/proj/secret.json'"

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(sys, 'path', list(sys.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server, self.graph = context_pack.load_dashboard_modules()

    def task_id(self, number):
        return f'60000000-0000-4000-8000-{number:012}'

    def block_worker(self, number, task_node=True):
        """Register worker number, give it a task and a complete blocked report for it."""
        session = f'sess-{number:02}'
        task = self.task_id(number)
        self.register(f'proj--{number:02}', 'proj', session)
        self.write(f'tasks/proj/{task}.json', {'id': task if task_node else 'renamed', 'project': 'proj', 'status': 'in_progress'})
        request = self.request_id('proj', session)
        message = status_round.build_expected_reply('proj', session, ORCH, request)
        message.update(id=str(uuid.UUID(int=number)), created_at='2026-11-27T10:09:00Z', status='blocked', task_ids=[task],
                       summary='waiting', task_id=task, blocker={'context': 'CI is red', 'question': 'Skip it?'})
        self.write(f'inbox/orchestrator/{message["id"]}.json', message)
        return task

    def collect_captured(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            report = main(['--root', str(self.root), 'collect', '--endpoint', ORCH, '--slot', SLOT])
        return report, out.getvalue(), err.getvalue()

    def without_time(self, pack):
        return {key: value for key, value in pack.items() if key != 'generated_at'}

    def test_decisions_gain_the_packs_context_pack_prints_and_a_context_node(self):
        tasks = [self.block_worker(number) for number in (1, 2)]
        report = self.collect()
        self.assertEqual([f'task:{task}' for task in tasks], [item['context_node'] for item in report['decisions_needed']])
        self.assertEqual((True, 0), (report['context']['available'], report['context']['omitted']))
        for task in tasks:
            expected = self.without_time(context_pack.read_pack(self.root, f'task:{task}'))
            self.assertEqual(expected, self.without_time(report['context']['packs'][f'task:{task}']))

    def test_every_existing_key_is_kept_and_only_context_and_context_node_are_added(self):
        self.block_worker(1)
        report = self.collect()
        self.assertEqual({'slot', 'workers', 'missing', 'decisions_needed', 'incomplete', 'context'}, set(report))
        (decision,) = report['decisions_needed']
        self.assertEqual({'project', 'session_id', 'reply', 'task_id', 'context', 'question', 'options', 'decided',
                          'decision_path', 'context_node'}, set(decision))

    def test_the_printed_report_is_the_returned_report_with_its_context(self):
        self.block_worker(1)
        out = io.StringIO()
        with redirect_stdout(out):
            report = main(['--root', str(self.root), 'collect', '--endpoint', ORCH, '--slot', SLOT])
        self.assertEqual(report, json.loads(out.getvalue()))

    def test_a_blocked_task_that_is_no_graph_node_gets_no_entry_and_no_context_node(self):
        self.block_worker(1, task_node=False)
        report = self.collect()
        self.assertEqual(({'available': True, 'packs': {}, 'omitted': 0}, False),
                         (report['context'], 'context_node' in report['decisions_needed'][0]))

    def test_only_the_first_ten_distinct_tasks_are_looked_up_and_an_unknown_one_uses_its_slot(self):
        for number in range(1, 12):
            self.block_worker(number, task_node=number != 4)
        report = self.collect()
        marked = [item['task_id'] for item in report['decisions_needed'] if 'context_node' in item]
        self.assertEqual((9, 1), (len(report['context']['packs']), report['context']['omitted']))
        self.assertEqual(9, len(marked))
        self.assertNotIn(self.task_id(4), marked)
        self.assertNotIn(f'task:{self.task_id(11)}', report['context']['packs'])
        self.assertNotIn(f'task:{self.task_id(4)}', report['context']['packs'])

    def test_the_dashboard_and_the_graph_are_built_once_for_several_tasks(self):
        for number in (1, 2):
            self.block_worker(number)
        with (mock.patch.object(self.server, 'build_dashboard', wraps=self.server.build_dashboard) as dashboard,
              mock.patch.object(self.graph, 'build_context_graph', wraps=self.graph.build_context_graph) as graph):
            report = self.collect()
        self.assertEqual((2, 1, 1), (len(report['context']['packs']), dashboard.call_count, graph.call_count))

    def test_without_a_blocked_report_the_dashboard_code_is_not_loaded(self):
        self.reply(status='idle')
        with mock.patch.object(context_pack, 'load_dashboard_modules') as loader:
            report = self.collect()
        loader.assert_not_called()
        self.assertEqual({'available': True, 'packs': {}, 'omitted': 0}, report['context'])
        self.assertEqual([], report['decisions_needed'])

    def test_a_failing_context_leaves_the_report_as_without_the_failure(self):
        self.block_worker(1)
        healthy = self.collect()
        task = self.task_id(1)
        failures = {
            'loader': mock.patch.object(context_pack, 'load_dashboard_modules',
                                        side_effect=context_pack.Refusal(context_pack.CODE_NOT_LOADED, 1)),
            'dashboard': mock.patch.object(self.server, 'build_dashboard', side_effect=OSError(self.RAW_FAILURE)),
            'unprintable': mock.patch.object(self.graph, 'build_context_packs',
                                             return_value={f'task:{task}': {'bad': float('nan')}}),
        }
        for name, failure in failures.items():
            with self.subTest(name), failure:
                report, out, err = self.collect_captured()
            self.assertEqual(self.UNAVAILABLE, report['context'])
            self.assertEqual(self.report_without_context(healthy), self.report_without_context(report))
            self.assertEqual(report, json.loads(out))
            for leak in self.LEAKS:
                self.assertNotIn(leak, out + err)
            self.assertEqual('', err)

    def report_without_context(self, report):
        stripped = {key: value for key, value in report.items() if key != 'context'}
        stripped['decisions_needed'] = [{key: value for key, value in item.items() if key != 'context_node'}
                                        for item in report['decisions_needed']]
        return stripped

    def test_collect_with_packs_attached_leaves_the_mailbox_byte_identical(self):
        self.block_worker(1)
        before = snapshot(self.root)
        report = self.collect()
        self.assertEqual(1, len(report['context']['packs']))
        self.assertEqual(snapshot(self.root), before)

    def test_request_output_has_no_context_and_does_not_load_the_dashboard_code(self):
        with mock.patch.object(context_pack, 'load_dashboard_modules') as loader:
            result = self.request()
        loader.assert_not_called()
        self.assertNotIn('context', result)

    def run_script(self, script, *args, root=None):
        return subprocess.run([sys.executable, '-B', str(script), '--root', str(root or self.root), *args],
                              capture_output=True, text=True, timeout=60)

    def test_an_unusable_context_pack_script_leaves_collect_as_without_the_failure(self):
        self.block_worker(1)
        healthy = self.run_script(SCRIPT, 'collect', '--endpoint', ORCH, '--slot', SLOT)
        self.assertEqual((0, ''), (healthy.returncode, healthy.stderr))
        for name, source in (('missing', None), ('failing on import', BROKEN_CONTEXT_PACK)):
            with self.subTest(name), tempfile.TemporaryDirectory() as parent:
                scripts = scripts_copy(parent, source)
                completed = self.run_script(scripts / 'status_round.py', 'collect', '--endpoint', ORCH, '--slot', SLOT)
                report = json.loads(completed.stdout)
                self.assertEqual((0, '', self.UNAVAILABLE), (completed.returncode, completed.stderr, report['context']))
                self.assertEqual(self.report_without_context(json.loads(healthy.stdout)), self.report_without_context(report))
                self.assertFalse(any('context_node' in item for item in report['decisions_needed']))
                for leak in (*self.LEAKS, 'context_pack', 'RuntimeError'):
                    self.assertNotIn(leak, completed.stdout + completed.stderr)

    def test_without_a_blocked_report_an_unusable_context_pack_does_not_make_the_context_unavailable(self):
        self.reply(status='idle')
        for name, source in (('missing', None), ('failing on import', BROKEN_CONTEXT_PACK)):
            with self.subTest(name), tempfile.TemporaryDirectory() as parent:
                scripts = scripts_copy(parent, source)
                completed = self.run_script(scripts / 'status_round.py', 'collect', '--endpoint', ORCH, '--slot', SLOT)
                self.assertEqual((0, ''), (completed.returncode, completed.stderr))
                report = json.loads(completed.stdout)
                self.assertEqual(({'available': True, 'packs': {}, 'omitted': 0}, []),
                                 (report['context'], report['decisions_needed']))

    def test_collect_in_an_installed_layout_with_a_pack_writes_no_context_or_dashboard_bytecode(self):
        task = self.block_worker(1)
        scripts = install_layout(self.root)
        records_before = file_contents(self.root, skipping=('.agents', 'dashboard'))
        dashboard_before = file_contents(self.root / 'dashboard')
        environment = {key: value for key, value in os.environ.items()
                       if key not in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')}
        completed = subprocess.run([sys.executable, str(scripts / 'status_round.py'), '--root', str(self.root), 'collect',
                                    '--endpoint', ORCH, '--slot', SLOT], capture_output=True, text=True, timeout=60,
                                   env=environment)
        report = json.loads(completed.stdout)
        self.assertEqual((0, '', True, [f'task:{task}']),
                         (completed.returncode, completed.stderr, report['context']['available'],
                          [item.get('context_node') for item in report['decisions_needed']]))
        self.assertEqual(records_before, file_contents(self.root, skipping=('.agents', 'dashboard')))
        self.assertEqual(dashboard_before, file_contents(self.root / 'dashboard'))
        self.assertEqual([], list(scripts.glob('__pycache__/context_pack*')))

    def test_request_works_and_keeps_its_keys_while_context_pack_is_unusable(self):
        reference = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, reference, True)
        mirror = reference / 'mailbox'
        shutil.copytree(self.root, mirror)
        expected = set(json.loads(self.run_script(SCRIPT, 'request', '--endpoint', ORCH, '--at', AT, root=mirror).stdout))
        for name, source in (('missing', None), ('failing on import', BROKEN_CONTEXT_PACK)):
            with self.subTest(name), tempfile.TemporaryDirectory() as parent:
                scripts = scripts_copy(parent, source)
                completed = self.run_script(scripts / 'status_round.py', 'request', '--endpoint', ORCH, '--at', AT)
                self.assertEqual((0, ''), (completed.returncode, completed.stderr))
                self.assertEqual(expected, set(json.loads(completed.stdout)))
                self.assertNotIn('context', json.loads(completed.stdout))
                self.assertNotIn('Permission denied', completed.stdout)

    def test_the_cli_prints_the_context_for_collect_and_none_for_request(self):
        self.block_worker(1)
        collected = self.run_cli('collect', '--endpoint', ORCH, '--slot', SLOT)
        self.assertEqual((0, ''), (collected.returncode, collected.stderr))
        printed = json.loads(collected.stdout)
        self.assertEqual([f'task:{self.task_id(1)}'], list(printed['context']['packs']))
        requested = self.run_cli('request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual(0, requested.returncode)
        self.assertNotIn('context', json.loads(requested.stdout))


SHALLOW_PROBE_SIBLING = "import sys\nSEEN_DONT_WRITE_BYTECODE = sys.dont_write_bytecode\n\n\ndef mailbox_root(script):\n    return None\n"


def shallow_scripts_copy(parent):
    """A copy of the scripts placed directly in parent, which must have fewer than five ancestors and no registry above."""
    shutil.copytree(SCRIPT.parent, parent, dirs_exist_ok=True, ignore=shutil.ignore_patterns('test_*', '__pycache__'))
    script = Path(parent) / 'status_round.py'
    ancestors = script.resolve().parents
    if len(ancestors) >= 5 or any((folder / 'projects.json').exists() for folder in ancestors):
        raise AssertionError(f'precondition not met: {script.resolve()} has {len(ancestors)} ancestors or a registry above it')
    return script


def environment_with_bytecode():
    return {key: value for key, value in os.environ.items() if key not in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')}


class ShallowLayoutTest(StatusRoundTestCase):
    """status_round.py started from a directory with fewer than five ancestors."""
    UNAVAILABLE = {'available': False, 'packs': {}, 'omitted': 0}
    MAILBOX_LINE_OUTPUT = MAILBOX_LINE + '\n'

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.shallow_directory = temp.name
        self.shallow = shallow_scripts_copy(temp.name)

    def mailbox_copy(self):
        parent = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, parent, True)
        mirror = parent / 'mailbox'
        shutil.copytree(self.root, mirror)
        return mirror

    def installed_script(self, mailbox):
        return install_layout(mailbox) / 'status_round.py'

    def run_script(self, script, *args, root, env=None):
        command = [sys.executable, str(script), *(['--root', str(root)] if root else []), *args]
        return subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)

    def run_shallow_and_installed(self, *args):
        """The same arguments from the shallow copy and from an installed layout, each on its own equal mailbox."""
        shallow_mailbox, installed_mailbox = self.mailbox_copy(), self.mailbox_copy()
        shallow = self.run_script(self.shallow, *args, root=shallow_mailbox)
        installed = self.run_script(self.installed_script(installed_mailbox), *args, root=installed_mailbox,
                                    env=environment_with_bytecode())
        return shallow, installed

    def without_context_nodes(self, report):
        stripped = {key: value for key, value in report.items() if key != 'context'}
        stripped['decisions_needed'] = [{key: value for key, value in item.items() if key != 'context_node'}
                                        for item in report['decisions_needed']]
        return stripped

    def block_with_task_node(self):
        self.blocked_reply()
        self.write(f'tasks/proj/{TASK}.json', {'id': TASK, 'project': 'proj', 'status': 'in_progress'})

    # C1
    def test_shallow_collect_of_a_blocked_report_has_exactly_the_unavailable_context(self):
        self.block_with_task_node()
        shallow, installed = self.run_shallow_and_installed('collect', '--endpoint', ORCH, '--slot', SLOT)
        report = json.loads(shallow.stdout)
        self.assertEqual((0, '', self.UNAVAILABLE), (shallow.returncode, shallow.stderr, report['context']))
        self.assertEqual(1, len(report['decisions_needed']))
        self.assertFalse(any('context_node' in item for item in report['decisions_needed']))
        self.assertEqual(self.without_context_nodes(json.loads(installed.stdout)), self.without_context_nodes(report))

    def test_shallow_collect_without_a_blocked_report_prints_what_the_installed_layout_prints(self):
        self.reply(status='idle')
        shallow, installed = self.run_shallow_and_installed('collect', '--endpoint', ORCH, '--slot', SLOT)
        self.assertEqual((0, ''), (shallow.returncode, shallow.stderr))
        self.assertEqual((0, ''), (installed.returncode, installed.stderr))
        self.assertEqual(json.loads(installed.stdout), json.loads(shallow.stdout))

    def test_shallow_request_prints_what_the_installed_layout_prints(self):
        shallow, installed = self.run_shallow_and_installed('request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual((0, ''), (shallow.returncode, shallow.stderr))
        self.assertEqual((0, ''), (installed.returncode, installed.stderr))
        self.assertEqual(json.loads(installed.stdout), json.loads(shallow.stdout))

    # C2
    def test_shallow_without_root_ends_with_the_fixed_mailbox_line_and_no_traceback(self):
        commands = {'request': ('request', '--endpoint', ORCH, '--at', AT),
                    'collect': ('collect', '--endpoint', ORCH, '--slot', SLOT)}
        for name, arguments in commands.items():
            with self.subTest(name):
                completed = self.run_script(self.shallow, *arguments, root=None)
                self.assertNotEqual(0, completed.returncode)
                self.assertEqual(('', self.MAILBOX_LINE_OUTPUT), (completed.stdout, completed.stderr))
                for leak in ('Traceback', 'IndexError', self.shallow_directory):
                    self.assertNotIn(leak, completed.stderr)

    # C5 protect, from the shallow copy
    def test_shallow_failures_keep_their_fixed_lines_and_exit_statuses(self):
        missing = self.root / 'no-such-directory'
        cases = {
            'root is not a directory': (['--root', str(missing), 'collect', '--endpoint', ORCH], 1, MAILBOX_LINE),
            'invalid time': (['--root', str(self.root), 'request', '--endpoint', ORCH, '--at', 'yesterday'], 2, USAGE_LINE),
        }
        for name, (arguments, status, line) in cases.items():
            with self.subTest(name):
                completed = self.run_script(self.shallow, *arguments, root=None)
                self.assertEqual((status, '', line + '\n'), (completed.returncode, completed.stdout, completed.stderr))

    # C3
    def test_the_installed_layout_without_root_uses_the_mailbox_that_holds_the_registry(self):
        self.block_with_task_node()
        script = self.installed_script(self.root)
        arguments = ('collect', '--endpoint', ORCH, '--slot', SLOT)
        implicit = self.run_script(script, *arguments, root=None, env=environment_with_bytecode())
        explicit = self.run_script(script, *arguments, root=self.root, env=environment_with_bytecode())
        self.assertEqual((0, ''), (implicit.returncode, implicit.stderr))
        implicit_report, explicit_report = json.loads(implicit.stdout), json.loads(explicit.stdout)
        self.assertEqual([f'task:{TASK}'], list(implicit_report['context']['packs']))
        self.assertEqual(self.without_generation_time(explicit_report), self.without_generation_time(implicit_report))

    def without_generation_time(self, report):
        packs = {node: {key: value for key, value in pack.items() if key != 'generated_at'}
                 for node, pack in report['context']['packs'].items()}
        return dict(report, context=dict(report['context'], packs=packs))


class DefaultRootTest(StatusRoundTestCase):
    def default_root_for(self, script):
        with mock.patch.object(status_round, '__file__', str(script)):
            return status_round.default_root()

    def test_the_nearest_directory_above_the_script_that_holds_the_registry_is_the_default_root(self):
        script = self.root / 'workspace/.agents/skills/mailbox-orchestrator/scripts/status_round.py'
        self.assertEqual(self.root, self.default_root_for(script))
        nested = self.root / 'inner'
        nested.mkdir()
        (nested / 'projects.json').write_text('{}')
        self.assertEqual(nested, self.default_root_for(nested / 'a/b/c/d/e/status_round.py'))

    def test_without_a_registry_a_deep_script_defaults_to_the_installed_layout_ancestor(self):
        with tempfile.TemporaryDirectory() as parent:
            script = Path(parent) / 'a/b/c/d/e/scripts/status_round.py'
            self.assertEqual(script.resolve().parents[4], self.default_root_for(script))

    def test_without_a_registry_a_shallow_script_has_no_default_root(self):
        shallow = Path('/status-round-shallow/status_round.py')
        with self.assertRaises(MailboxError) as raised:
            self.default_root_for(shallow)
        self.assertEqual(MAILBOX_LINE, str(raised.exception))

    def test_main_without_root_uses_the_default_root(self):
        with mock.patch.object(status_round, 'default_root', return_value=self.root) as default_root:
            with redirect_stdout(io.StringIO()):
                main(['request', '--endpoint', ORCH, '--at', AT])
        default_root.assert_called_once_with()
        self.assertIn(f'{self.request_id()}.json', self.inbox_files())

    def test_main_with_root_never_resolves_the_default(self):
        with mock.patch.object(status_round, 'default_root', side_effect=MailboxError(MAILBOX_LINE)) as default_root:
            self.request()
        default_root.assert_not_called()

    def test_main_without_root_and_without_a_default_root_raises_the_mailbox_error(self):
        with mock.patch.object(status_round, 'default_root', side_effect=MailboxError(MAILBOX_LINE)):
            with self.assertRaises(MailboxError):
                main(['collect', '--endpoint', ORCH])

    def test_the_parser_has_no_root_default_and_the_module_has_no_mailbox_constant(self):
        args = status_round.build_parser().parse_args(['request', '--endpoint', ORCH])
        self.assertIsNone(args.root)
        self.assertFalse(hasattr(status_round, 'MAILBOX'))


class NoBytecodeTest(StatusRoundTestCase):
    def run_installed(self, scripts, *arguments):
        return subprocess.run([sys.executable, str(scripts / 'status_round.py'), '--root', str(self.root), *arguments],
                              capture_output=True, text=True, timeout=60, env=environment_with_bytecode())

    def cache_entries(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')
                      if path.name == '__pycache__' or path.suffix == '.pyc')

    def test_collect_from_an_installed_layout_creates_no_file(self):
        self.blocked_reply()
        self.write(f'tasks/proj/{TASK}.json', {'id': TASK, 'project': 'proj', 'status': 'in_progress'})
        scripts = install_layout(self.root)
        before = snapshot(self.root)
        completed = self.run_installed(scripts, 'collect', '--endpoint', ORCH, '--slot', SLOT)
        report = json.loads(completed.stdout)
        self.assertEqual((0, '', True, [f'task:{TASK}']),
                         (completed.returncode, completed.stderr, report['context']['available'],
                          [item.get('context_node') for item in report['decisions_needed']]))
        self.assertEqual([], self.cache_entries())
        self.assertEqual(before, snapshot(self.root))

    def test_request_from_an_installed_layout_creates_only_its_mailbox_records(self):
        scripts = install_layout(self.root)
        before = set(file_contents(self.root))
        completed = self.run_installed(scripts, 'request', '--endpoint', ORCH, '--at', AT)
        self.assertEqual((0, ''), (completed.returncode, completed.stderr))
        created = sorted(set(file_contents(self.root)) - before)
        self.assertEqual([f'inbox/proj/{self.request_id()}.json'], created)
        self.assertEqual([], self.cache_entries())

    def test_the_sibling_imports_are_made_with_bytecode_writing_off_and_the_flag_is_restored(self):
        with tempfile.TemporaryDirectory() as parent:
            for module in ('receive', 'mailbox_layout'):
                (Path(parent) / f'{module}.py').write_text(SHALLOW_PROBE_SIBLING)
            for starting in (False, True):
                with self.subTest(starting=starting), mock.patch.dict(sys.modules), \
                        mock.patch.object(sys, 'path', [parent, *sys.path]), \
                        mock.patch.object(sys, 'dont_write_bytecode', starting):
                    sys.modules.pop('receive'), sys.modules.pop('mailbox_layout')
                    runpy.run_path(str(SCRIPT), run_name='status_round_probe')
                    seen = (sys.modules['receive'].SEEN_DONT_WRITE_BYTECODE,
                            sys.modules['mailbox_layout'].SEEN_DONT_WRITE_BYTECODE)
                    self.assertEqual((True, True, starting), (*seen, sys.dont_write_bytecode))


if __name__ == '__main__':
    unittest.main()
