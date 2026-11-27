import io
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

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
        self.assertEqual(self.inbox_files(), sorted([f'{self.request_id()}.json', f'{next_id}.json']))

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
            'options': ['skip', 'wait'], 'decided': False, 'decision_path': None}])
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


if __name__ == '__main__':
    unittest.main()
