import contextlib
import fcntl
import io
import json
import os
import runpy
import signal
import subprocess
import sys
import threading
import time
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from unittest import mock

import status_round
import status_round_runner as runner
from test_status_round import (AT, MAX_BYTES, ORCH, OTHER_TASK, PREVIOUS_SLOT, SLOT, TASK, StatusRoundTestCase,
                               environment_with_bytecode, file_contents, install_layout, snapshot)

RUNNER_SCRIPT = Path(runner.__file__).resolve()
UTC = timezone.utc
NOW = datetime(2026, 11, 27, 10, 7, 30, tzinfo=UTC)
PREVIOUS_NOW = datetime(2026, 11, 27, 9, 50, 0, tzinfo=UTC)
TICK_FILE = '20261127T100000Z.json'
PREVIOUS_TICK_FILE = '20261127T094500Z.json'
RUNNER_PID = 4242
RUNNING_STATE = {'pid': RUNNER_PID, 'endpoint': ORCH, 'started_at': '2026-11-27T10:00:00Z', 'token': 'ab'}
OTHER_ENDPOINT = 'other-orch-session'
RAW_TEXT = '/abs/path: Permission denied'
STATUS_NAME_KEYS = ('running', 'pid', 'endpoint', 'started_at', 'current_slot', 'last_slot',
                    'current_slot_recorded', 'last_tick_path', 'last_tick', 'last_tick_problem')


class Outcome(NamedTuple):
    code: int
    stdout: str
    stderr: str


class HeldLock:
    """An exclusive flock on its own open file description, as another process would hold it."""

    def __init__(self, path, mode=fcntl.LOCK_EX):
        self.descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.descriptor, mode)

    def replace_content(self, data):
        os.ftruncate(self.descriptor, 0)
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        os.write(self.descriptor, data)

    def release(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def wait_until(condition, seconds=15.0):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


class RunnerTestCase(StatusRoundTestCase):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.waits, self.spawned, self.signals = [], [], []
        self.services = runner.Services(
            clock=lambda: self.now, wait=self.record_wait, spawn_runner=self.fake_spawn,
            send_signal=lambda pid, number: self.signals.append((pid, number)),
            orchestrator_alive=lambda root: True)

    def record_wait(self, interrupt, seconds):
        self.waits.append(seconds)
        return False

    def advance_clock(self, interrupt, seconds):
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)
        return False

    def fake_spawn(self, arguments, descriptor):
        self.spawned.append((arguments, descriptor))
        runner.write_state(descriptor, {
            'pid': RUNNER_PID, 'endpoint': arguments[arguments.index('--endpoint') + 1],
            'started_at': '2026-11-27T10:07:30Z', 'token': arguments[arguments.index('--token') + 1]})

    def invoke(self, *argv, services=None, root=True):
        prefix = ['--root', str(self.root)] if root else []
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = runner.main([*prefix, *argv], services or self.services)
        return Outcome(code, stdout.getvalue(), stderr.getvalue())

    def invoke_json(self, *argv, services=None, root=True):
        outcome = self.invoke(*argv, services=services, root=root)
        self.assertEqual((outcome.code, outcome.stderr), (0, ''))
        return json.loads(outcome.stdout)

    def assert_fixed_failure(self, outcome, line, code=1):
        self.assertEqual(outcome, Outcome(code, '', line + '\n'))
        self.assertNotIn(str(self.root), outcome.stderr)

    def hold_runner_lock(self, content=RUNNING_STATE):
        holder = HeldLock(self.root / runner.RUNNER_LOCK)
        self.addCleanup(holder.release)
        if content is not None:
            holder.replace_content(content if isinstance(content, bytes) else json.dumps(content).encode())
        return holder

    def tick(self, now=NOW, endpoint=ORCH):
        return runner.run_tick(self.root, endpoint, now, status_round)

    def read_tick(self, name=TICK_FILE):
        return json.loads((self.root / runner.TICK_DIRECTORY / name).read_text())

    def tick_files(self):
        folder = self.root / runner.TICK_DIRECTORY
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []

    def isolate_signal_handlers(self):
        """Replace the SIGTERM and SIGINT handlers by inert ones for the test, so a missing handler cannot kill it."""
        inert = {number: (lambda signum, frame: None) for number in (signal.SIGTERM, signal.SIGINT)}
        for number, handler in inert.items():
            self.addCleanup(signal.signal, number, signal.signal(number, handler))
        return inert


class ServicesTest(RunnerTestCase):
    def test_real_clock_is_an_aware_utc_time_near_now(self):
        moment = runner.real_clock()
        self.assertEqual(moment.utcoffset(), timedelta(0))
        self.assertLess(abs((datetime.now(UTC) - moment).total_seconds()), 5)

    def test_real_wait_returns_true_at_once_for_a_set_event_and_false_after_the_time(self):
        interrupted = threading.Event()
        interrupted.set()
        self.assertIs(runner.real_wait(interrupted, 30), True)
        self.assertIs(runner.real_wait(threading.Event(), 0.01), False)

    def test_spawn_starts_this_script_detached_with_the_locked_descriptor(self):
        with mock.patch.object(runner.subprocess, 'Popen') as popen:
            runner.spawn_detached_runner(['run', '--lock-fd', '7'], 7)
        popen.assert_called_once_with(
            [sys.executable, str(RUNNER_SCRIPT), 'run', '--lock-fd', '7'], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            pass_fds=(7,))

    def test_default_services_use_the_real_collaborators(self):
        defaults = runner.Services()
        self.assertEqual((defaults.clock, defaults.wait, defaults.spawn_runner, defaults.send_signal,
                          defaults.orchestrator_alive),
                         (runner.real_clock, runner.real_wait, runner.spawn_detached_runner, os.kill,
                          runner.orchestrator_lock_held))

    def test_main_without_services_runs_status_with_the_real_collaborators(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = runner.main(['--root', str(self.root), 'status'])
        self.assertEqual(code, 0)
        self.assertIs(json.loads(stdout.getvalue())['running'], False)

    def test_script_entry_exits_with_the_status_of_main(self):
        argv = [str(RUNNER_SCRIPT), '--root', str(self.root), 'status']
        with mock.patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(RUNNER_SCRIPT), run_name='__main__')
        self.assertEqual(raised.exception.code, 0)


class ProbeTest(RunnerTestCase):
    def setUp(self):
        super().setUp()
        self.lock = self.root / runner.ORCHESTRATOR_LOCK

    def remove_lock_entry(self):
        if self.lock.is_dir() and not self.lock.is_symlink():
            self.lock.rmdir()
        elif os.path.lexists(self.lock):
            self.lock.unlink()

    def test_missing_or_non_regular_orchestrator_lock_counts_as_not_held(self):
        outside = self.root / 'held.lock'
        holder = HeldLock(outside)
        self.addCleanup(holder.release)
        builders = {
            'missing': lambda: None,
            'directory': lambda: self.lock.mkdir(),
            'symbolic link to a held file': lambda: self.lock.symlink_to(outside),
            'named pipe': lambda: os.mkfifo(self.lock),
        }
        for name, build in builders.items():
            with self.subTest(case=name):
                self.remove_lock_entry()
                build()
                self.assertIs(runner.orchestrator_lock_held(self.root), False)

    def test_lock_held_by_another_open_file_description_counts_as_held(self):
        holder = HeldLock(self.lock)
        self.addCleanup(holder.release)
        self.assertIs(runner.orchestrator_lock_held(self.root), True)
        holder.release()
        self.assertIs(runner.orchestrator_lock_held(self.root), False)

    def test_probe_leaves_no_lock_behind(self):
        self.lock.write_text('')
        self.assertIs(runner.orchestrator_lock_held(self.root), False)
        descriptor = os.open(self.lock, os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


class StateFileTest(RunnerTestCase):
    INVALID_STATES = {
        'empty': b'',
        'partial': b'{"pid": 4242',
        'not an object': b'[]',
        'no pid': b'{"endpoint": "x"}',
        'zero pid': b'{"pid": 0, "endpoint": "x"}',
        'boolean pid': b'{"pid": true, "endpoint": "x"}',
        'text pid': b'{"pid": "7", "endpoint": "x"}',
        'numeric endpoint': b'{"pid": 7, "endpoint": 3}',
        'not utf-8': b'\xff\xfe',
        'oversized': json.dumps({'pid': 7, 'endpoint': 'x' * (runner.MAX_STATE_BYTES + 1)}).encode(),
    }

    def test_busy_lock_with_empty_or_stale_state_ends_with_the_fixed_line_and_sends_no_signal(self):
        holder = self.hold_runner_lock(None)
        commands = (('start', '--endpoint', ORCH), ('stop',), ('status',))
        for name, content in self.INVALID_STATES.items():
            holder.replace_content(content)
            for command in commands:
                with self.subTest(state=name, command=command[0]):
                    self.waits.clear()
                    self.assert_fixed_failure(self.invoke(*command), runner.STATE_LINE)
                    self.assertEqual(len(self.waits), runner.STATE_ATTEMPTS)
                    self.assertEqual((self.signals, self.spawned), ([], []))

    def test_reader_waits_for_a_state_that_is_written_late(self):
        holder = self.hold_runner_lock(None)

        def write_on_third_pause(interrupt, seconds):
            self.waits.append(seconds)
            if len(self.waits) == 3:
                holder.replace_content(json.dumps(RUNNING_STATE).encode())
            return False

        services = self.services._replace(wait=write_on_third_pause)
        report = self.invoke_json('status', services=services)
        self.assertEqual((report['running'], report['pid']), (True, RUNNER_PID))
        self.assertEqual(self.waits, [runner.POLL_SECONDS] * 4)

    def test_start_truncates_earlier_state_before_spawning(self):
        lock = self.root / runner.RUNNER_LOCK
        lock.write_text(json.dumps(RUNNING_STATE))
        content_at_spawn = []

        def spawn(arguments, descriptor):
            content_at_spawn.append(lock.read_bytes())
            self.fake_spawn(arguments, descriptor)

        self.invoke_json('start', '--endpoint', ORCH, services=self.services._replace(spawn_runner=spawn))
        self.assertEqual(content_at_spawn, [b''])

    def test_write_state_replaces_longer_earlier_content_completely(self):
        holder = self.hold_runner_lock(dict(RUNNING_STATE, endpoint='e' * 200))
        runner.write_state(holder.descriptor, {'pid': 7, 'endpoint': 'x'})
        self.assertEqual((self.root / runner.RUNNER_LOCK).read_bytes(), b'{"pid": 7, "endpoint": "x"}')
        self.assertEqual(runner.read_state(self.root), {'pid': 7, 'endpoint': 'x'})

    def test_read_state_is_none_without_a_readable_regular_lock_file(self):
        self.assertIsNone(runner.read_state(self.root))
        outside = self.root / 'outside'
        outside.write_text(json.dumps(RUNNING_STATE))
        (self.root / runner.RUNNER_LOCK).symlink_to(outside)
        self.assertIsNone(runner.read_state(self.root))

    def test_unusable_runner_lock_path_ends_with_the_fixed_line(self):
        lock = self.root / runner.RUNNER_LOCK
        outside = self.root / 'outside'
        outside.write_text('')
        cases = {'named pipe': (lambda: os.mkfifo(lock), (('start', '--endpoint', ORCH),)),
                 'symbolic link': (lambda: lock.symlink_to(outside),
                                   (('start', '--endpoint', ORCH), ('stop',), ('status',)))}
        for name, (build, commands) in cases.items():
            build()
            for command in commands:
                with self.subTest(case=name, command=command[0]):
                    self.assert_fixed_failure(self.invoke(*command), runner.FAILURE_LINE)
            lock.unlink()
        self.assertEqual((self.spawned, self.signals), ([], []))


class StartTest(RunnerTestCase):
    def test_start_spawns_one_runner_with_the_locked_descriptor(self):
        held_during_spawn = []

        def spawn(arguments, descriptor):
            held_during_spawn.append(runner.runner_lock_busy(self.root))
            self.fake_spawn(arguments, descriptor)

        result = self.invoke_json('start', '--endpoint', ORCH, services=self.services._replace(spawn_runner=spawn))
        (arguments, descriptor), = self.spawned
        token = arguments[-1]
        self.assertRegex(token, r'[0-9a-f]{32}')
        self.assertEqual(arguments, ['--root', str(self.root), 'run', '--endpoint', ORCH,
                                     '--lock-fd', str(descriptor), '--token', token])
        self.assertEqual(held_during_spawn, [True])
        self.assertEqual(result, {'state': 'started', 'pid': RUNNER_PID, 'endpoint': ORCH,
                                  'started_at': '2026-11-27T10:07:30Z'})

    def test_start_accepts_the_root_after_the_command(self):
        result = self.invoke_json('start', '--endpoint', ORCH, '--root', str(self.root), root=False,
                                  services=self.services)
        self.assertEqual(result['state'], 'started')

    def test_second_start_reports_already_running_and_changes_no_file(self):
        self.hold_runner_lock()
        before = snapshot(self.root)
        result = self.invoke_json('start', '--endpoint', ORCH)
        self.assertEqual(result, {'state': 'already_running', 'pid': RUNNER_PID, 'endpoint': ORCH,
                                  'started_at': '2026-11-27T10:00:00Z'})
        self.assertEqual(self.spawned, [])
        self.assertEqual(snapshot(self.root), before)

    def test_start_for_another_endpoint_is_refused_and_changes_nothing(self):
        self.hold_runner_lock()
        before = snapshot(self.root)
        self.assert_fixed_failure(self.invoke('start', '--endpoint', OTHER_ENDPOINT), runner.OTHER_ENDPOINT_LINE)
        self.assertEqual(self.spawned, [])
        self.assertEqual(snapshot(self.root), before)

    def test_start_without_orchestrator_is_refused_with_the_fixed_line(self):
        services = self.services._replace(orchestrator_alive=lambda root: False)
        before = set(file_contents(self.root))
        self.assert_fixed_failure(self.invoke('start', '--endpoint', ORCH, services=services),
                                  runner.NO_ORCHESTRATOR_LINE)
        self.assertEqual(self.spawned, [])
        self.assertEqual(set(file_contents(self.root)) - before, {runner.RUNNER_LOCK})
        self.assertFalse(runner.runner_lock_busy(self.root))

    def test_start_probes_the_orchestrator_lock_of_the_root(self):
        probed = []
        services = self.services._replace(orchestrator_alive=lambda root: probed.append(root) or True)
        self.invoke_json('start', '--endpoint', ORCH, services=services)
        self.assertEqual(probed, [self.root])


class StopTest(RunnerTestCase):
    def test_stop_signals_the_recorded_pid_and_reports_stopped(self):
        holder = self.hold_runner_lock()

        def send_signal(pid, number):
            self.signals.append((pid, number))
            holder.release()

        result = self.invoke_json('stop', services=self.services._replace(send_signal=send_signal))
        self.assertEqual(result, {'state': 'stopped'})
        self.assertEqual(self.signals, [(RUNNER_PID, signal.SIGTERM)])

    def test_stop_without_runner_reports_not_running_and_creates_nothing(self):
        before = snapshot(self.root)
        self.assertEqual(self.invoke_json('stop'), {'state': 'not_running'})
        self.assertEqual((self.signals, snapshot(self.root)), ([], before))

    def test_stop_with_a_stale_state_in_a_free_lock_sends_no_signal(self):
        (self.root / runner.RUNNER_LOCK).write_text(json.dumps(RUNNING_STATE))
        self.assertEqual(self.invoke_json('stop'), {'state': 'not_running'})
        self.assertEqual(self.signals, [])

    def test_signal_to_a_process_that_is_already_gone_still_reports_stopped(self):
        holder = self.hold_runner_lock()

        def send_signal(pid, number):
            holder.release()
            raise ProcessLookupError

        result = self.invoke_json('stop', services=self.services._replace(send_signal=send_signal))
        self.assertEqual(result, {'state': 'stopped'})


class FixedErrorLineTest(RunnerTestCase):
    def test_unreadable_root_ends_with_the_fixed_line(self):
        missing = self.root / 'gone'
        for command in (('start', '--endpoint', ORCH), ('stop',), ('status',)):
            with self.subTest(command=command[0]):
                outcome = self.invoke('--root', str(missing), *command, root=False)
                self.assertEqual(outcome, Outcome(1, '', runner.MAILBOX_LINE + '\n'))
                self.assertNotIn(str(missing), outcome.stderr)

    def test_unreadable_root_without_sibling_scripts_ends_with_the_fixed_line(self):
        missing = self.root / 'gone'
        with mock.patch.object(runner, 'load_status_round', return_value=None):
            outcome = self.invoke('--root', str(missing), 'stop', root=False)
        self.assertEqual(outcome, Outcome(1, '', runner.MAILBOX_LINE + '\n'))

    def test_stop_works_on_an_existing_root_without_sibling_scripts(self):
        with mock.patch.object(runner, 'load_status_round', return_value=None):
            self.assertEqual(self.invoke_json('stop'), {'state': 'not_running'})

    def test_stop_without_root_needs_the_sibling_scripts_to_find_the_mailbox(self):
        with mock.patch.object(runner, 'load_status_round', return_value=None):
            self.assert_fixed_failure(self.invoke('stop', root=False), runner.SIBLING_LINE)

    def test_missing_root_argument_uses_the_default_root_of_the_scripts(self):
        with mock.patch.object(status_round, 'default_root', return_value=self.root):
            self.assertEqual(json.loads(self.invoke('stop', root=False).stdout), {'state': 'not_running'})

    def test_raw_os_error_text_reaches_neither_stderr_nor_the_tick_record(self):
        errors = {'OSError': OSError(RAW_TEXT), 'ValueError': ValueError(RAW_TEXT),
                  'OverflowError': OverflowError(RAW_TEXT)}
        for name, error in errors.items():
            with self.subTest(error=name), mock.patch.dict(runner.COMMANDS, {'status': mock.Mock(side_effect=error)}):
                self.assert_fixed_failure(self.invoke('status'), runner.FAILURE_LINE)
        with mock.patch.dict(runner.COMMANDS, {'status': mock.Mock(side_effect=status_round.MailboxError(RAW_TEXT))}):
            self.assert_fixed_failure(self.invoke('status'), runner.MAILBOX_LINE)
        with mock.patch.object(status_round, 'request_round', side_effect=OSError(RAW_TEXT)):
            self.tick()
        text = (self.root / runner.TICK_DIRECTORY / TICK_FILE).read_text()
        self.assertNotIn('/abs/path', text)
        self.assertNotIn('Permission denied', text)

    def test_argument_errors_end_with_the_fixed_line_without_parser_text(self):
        cases = {
            'no command': [],
            'unknown command': ['bogus'],
            'start without endpoint': ['start'],
            'start with unknown option': ['start', '--endpoint', ORCH, '--bogus'],
            'stop with an argument': ['stop', 'now'],
            'status with unknown option': ['status', '--bogus'],
            'run without endpoint': ['run', '--lock-fd', '5', '--token', 'ab'],
            'run without descriptor': ['run', '--endpoint', ORCH, '--token', 'ab'],
            'run without token': ['run', '--endpoint', ORCH, '--lock-fd', '5'],
            'run with zero descriptor': ['run', '--endpoint', ORCH, '--lock-fd', '0', '--token', 'ab'],
            'run with text descriptor': ['run', '--endpoint', ORCH, '--lock-fd', 'five', '--token', 'ab'],
            'abbreviated option': ['start', '--end', ORCH],
        }
        for name, argv in cases.items():
            with self.subTest(case=name):
                self.assert_fixed_failure(self.invoke(*argv, root=False), runner.USAGE_LINE, 2)
        self.assertEqual(runner.USAGE_LINE, 'status_round_runner: invalid arguments')

    def test_runner_that_cannot_start_ends_with_the_fixed_line(self):
        def refuse(arguments, descriptor):
            raise OSError(RAW_TEXT)

        def wrong_token(arguments, descriptor):
            runner.write_state(descriptor, dict(RUNNING_STATE, token='not-the-token'))

        spawners = {'spawn raises': refuse, 'no state appears': lambda arguments, descriptor: None,
                    'state of another start': wrong_token}
        for name, spawn in spawners.items():
            with self.subTest(case=name):
                self.waits.clear()
                outcome = self.invoke('start', '--endpoint', ORCH, services=self.services._replace(spawn_runner=spawn))
                self.assert_fixed_failure(outcome, runner.START_LINE)
                if name != 'spawn raises':
                    self.assertEqual(len(self.waits), runner.START_ATTEMPTS)
                self.assertNotIn('/abs/path', outcome.stderr)

    def test_runner_that_does_not_stop_ends_with_the_fixed_line(self):
        self.hold_runner_lock()
        self.assert_fixed_failure(self.invoke('stop'), runner.STOP_LINE)
        self.assertEqual(self.signals, [(RUNNER_PID, signal.SIGTERM)])
        self.assertEqual(self.waits, [runner.POLL_SECONDS] * (1 + runner.STOP_ATTEMPTS))

    def test_signal_that_cannot_be_sent_ends_with_the_fixed_line(self):
        self.hold_runner_lock()

        def refuse(pid, number):
            raise PermissionError(RAW_TEXT)

        outcome = self.invoke('stop', services=self.services._replace(send_signal=refuse))
        self.assert_fixed_failure(outcome, runner.STOP_LINE)
        self.assertNotIn('/abs/path', outcome.stderr)


class SiblingTest(RunnerTestCase):
    def test_siblings_missing_a_required_name_end_with_the_fixed_line(self):
        for command in (('start', '--endpoint', ORCH), ('status',)):
            with self.subTest(command=command[0]), \
                    mock.patch.object(runner, 'SIBLING_NAMES', runner.SIBLING_NAMES + ('no_such_name',)):
                self.assert_fixed_failure(self.invoke(*command), runner.SIBLING_LINE)
        self.assertEqual(self.spawned, [])

    def test_sibling_whose_import_fails_with_raw_text_ends_with_the_fixed_line_and_restores_the_flag(self):
        for starting in (False, True):
            with self.subTest(starting=starting), mock.patch.object(sys, 'dont_write_bytecode', starting), \
                    mock.patch.object(runner.importlib, 'import_module', side_effect=RuntimeError(RAW_TEXT)):
                self.assert_fixed_failure(self.invoke('status'), runner.SIBLING_LINE)
                self.assertIs(sys.dont_write_bytecode, starting)

    def test_sibling_import_runs_with_bytecode_writing_off_and_restores_the_flag(self):
        seen = []

        def import_module(name):
            seen.append(sys.dont_write_bytecode)
            return status_round

        with mock.patch.object(sys, 'dont_write_bytecode', False), \
                mock.patch.object(runner.importlib, 'import_module', side_effect=import_module):
            module = runner.require_siblings()
            self.assertIs(sys.dont_write_bytecode, False)
        self.assertEqual((seen, module), ([True], status_round))


class TickTest(RunnerTestCase):
    def test_published_requests_are_non_executable(self):
        self.assertIs(self.tick(), True)
        message = self.published_message()
        self.assertEqual((message['type'], message['executable']), ('status_request', False))
        self.assertEqual(self.read_tick()['request']['published'], [f'inbox/proj/{self.request_id()}.json'])

    def test_repeated_tick_in_one_slot_touches_no_file(self):
        self.assertIs(self.tick(NOW), True)
        before = snapshot(self.root)
        for now in (NOW, NOW + timedelta(minutes=6, seconds=29)):
            with self.subTest(now=now):
                self.assertIs(self.tick(now), False)
                self.assertEqual(snapshot(self.root), before)

    def test_no_existing_mailbox_file_is_changed_or_removed_by_a_tick(self):
        self.register('proj--b', 'proj', 'sess-b')
        self.write(f'tasks/proj/{TASK}.json', {'id': TASK})
        self.tick(PREVIOUS_NOW)
        before = file_contents(self.root)
        self.tick(NOW)
        after = file_contents(self.root)
        for name, content in before.items():
            with self.subTest(file=name):
                survivor = name if name in after else name.replace('inbox/', 'archive/', 1)
                self.assertEqual(after.get(survivor), content)
        requested = [self.request_id('proj', session, slot) for slot in (SLOT, PREVIOUS_SLOT) for session in ('sess-a', 'sess-b')]
        expected_new = {f'inbox/proj/{requested[0]}.json', f'inbox/proj/{requested[1]}.json',
                        f'archive/proj/{requested[2]}.json', f'archive/proj/{requested[3]}.json',
                        f'{runner.TICK_DIRECTORY}/{TICK_FILE}'}
        self.assertEqual(set(after) - set(before), expected_new)

    def test_existing_record_is_never_replaced(self):
        folder = self.root / runner.TICK_DIRECTORY
        folder.mkdir()
        existing = folder / TICK_FILE
        existing.write_text('{"kept": true}')
        before = (existing.read_bytes(), existing.stat().st_mtime_ns)
        self.assertIs(self.tick(), False)
        self.assertEqual((existing.read_bytes(), existing.stat().st_mtime_ns), before)
        self.assertEqual(self.tick_files(), [TICK_FILE])

    def test_record_id_is_the_canonical_uuid_of_its_slot(self):
        self.tick()
        record = self.read_tick()
        self.assertEqual(record['id'], str(uuid.uuid5(status_round.NAMESPACE, f'status-round-tick:{SLOT}')))
        self.assertEqual(str(uuid.UUID(record['id'])), record['id'])
        self.assertEqual((record['type'], record['slot'], record['previous_slot']), ('status_round_tick', SLOT, PREVIOUS_SLOT))
        self.assertEqual((record['recorded_at'], record['orchestrator_endpoint']), ('2026-11-27T10:07:30Z', ORCH))

    def test_status_rounds_that_is_a_symbolic_link_is_refused_after_the_request_step(self):
        (self.root / 'elsewhere').mkdir()
        (self.root / runner.TICK_DIRECTORY).symlink_to(self.root / 'elsewhere')
        with self.assertRaises(runner.RunnerError) as raised:
            self.tick()
        self.assertEqual(raised.exception.line, runner.MAILBOX_LINE)
        self.assertEqual(list((self.root / 'elsewhere').iterdir()), [])

    def test_status_rounds_that_is_a_file_makes_the_tick_fail_with_an_os_error(self):
        (self.root / runner.TICK_DIRECTORY).write_text('not a directory')
        with self.assertRaises(OSError):
            self.tick()

    def test_publish_record_leaves_no_temporary_file_when_the_record_cannot_be_written(self):
        folder = self.root / runner.TICK_DIRECTORY
        folder.mkdir()
        with self.assertRaises(ValueError):
            runner.publish_record(folder, TICK_FILE, {'value': float('nan')})
        self.assertEqual(list(folder.iterdir()), [])

    def test_publish_record_loses_a_race_without_replacing_or_leaving_a_temporary_file(self):
        folder = self.root / runner.TICK_DIRECTORY
        folder.mkdir()
        with mock.patch.object(os, 'link', side_effect=FileExistsError):
            self.assertIs(runner.publish_record(folder, TICK_FILE, {'a': 1}), False)
        self.assertEqual(list(folder.iterdir()), [])


class TickRecordTest(RunnerTestCase):
    def report(self, project, session_id, slot, **fields):
        message = status_round.build_expected_reply(project, session_id, ORCH, self.request_id(project, session_id, slot))
        del message['task_id'], message['blocker']
        reply_id = str(uuid.uuid4())
        message.update(id=reply_id, created_at='2026-11-27T09:55:00Z', status='idle', task_ids=[], summary='ok')
        message.update(fields)
        self.write(f'inbox/orchestrator/{reply_id}.json', message)

    def build_previous_round(self):
        for name, project, session_id in (('proj--silent', 'proj', 'sess-silent'), ('other--blocked', 'other', 'sess-blocked'),
                                          ('other--incomplete', 'other', 'sess-incomplete'),
                                          ('other--conflict', 'other', 'sess-conflict'), ('ghost--x', 'ghost', 'sess-ghost')):
            self.register(name, project, session_id)
        status_round.request_round(self.root, ORCH, PREVIOUS_NOW)
        self.write(f'tasks/other/{TASK}.json', {'id': TASK})
        self.write(f'tasks/other/{OTHER_TASK}.json', {'id': OTHER_TASK})
        self.report('proj', 'sess-a', PREVIOUS_SLOT)
        self.report('other', 'sess-blocked', PREVIOUS_SLOT, status='blocked', task_id=TASK,
                    blocker={'context': 'CI is red', 'question': 'Skip the flaky suite?'})
        self.report('other', 'sess-incomplete', PREVIOUS_SLOT, status='blocked', task_id=OTHER_TASK,
                    blocker={'context': 'CI is red'})
        conflict_id = self.request_id('other', 'sess-conflict', PREVIOUS_SLOT)
        self.write(f'archive/other/{conflict_id}.json', {'taken': True})
        return conflict_id

    def test_record_holds_request_result_and_previous_slot_collect(self):
        conflict_id = self.build_previous_round()
        silent_id = self.request_id('proj', 'sess-silent', PREVIOUS_SLOT)
        self.tick()
        record = self.read_tick()
        request, collect = record['request'], record['collect']
        self.assertEqual(request['slot'], SLOT)
        self.assertEqual(request['skipped'], [{'registration': 'sessions/ghost--x.json',
                                               'reason': 'project is not registered'}])
        self.assertEqual(request['superseded'], [f'archive/proj/{silent_id}.json'])
        self.assertEqual(request['supersede_conflicts'], [f'inbox/other/{conflict_id}.json'])
        self.assertEqual(len(request['published']), 5)
        self.assertEqual(collect['slot'], PREVIOUS_SLOT)
        self.assertEqual(sorted(item['session_id'] for item in collect['missing']), ['sess-conflict', 'sess-silent'])
        self.assertEqual([(item['session_id'], item['task_id'], item['question']) for item in collect['decisions_needed']],
                         [('sess-blocked', TASK, 'Skip the flaky suite?')])
        self.assertEqual([(item['session_id'], item['missing_fields']) for item in collect['incomplete']],
                         [('sess-incomplete', ['blocker.question'])])
        self.assertEqual((record['not_requested'], record['collect_omitted'], record['errors']), ([], None, []))

    def test_previous_slot_without_requests_omits_collect_with_the_fixed_text(self):
        self.tick()
        record = self.read_tick()
        self.assertEqual((record['collect'], record['collect_omitted']), (None, runner.NO_PREVIOUS_ROUND))
        self.assertEqual(record['not_requested'], [{'project': 'proj', 'session_id': 'sess-a',
                                                    'request': self.request_id(slot=PREVIOUS_SLOT)}])
        self.assertEqual(record['errors'], [])

    def test_worker_without_previous_request_is_listed_as_not_requested(self):
        status_round.request_round(self.root, ORCH, PREVIOUS_NOW)
        self.register('proj--new', 'proj', 'sess-new')
        unfiltered = status_round.with_context(self.root, status_round.collect_round(self.root, ORCH, PREVIOUS_SLOT))
        self.tick()
        record = self.read_tick()
        self.assertEqual(record['not_requested'], [{'project': 'proj', 'session_id': 'sess-new',
                                                    'request': self.request_id(session_id='sess-new', slot=PREVIOUS_SLOT)}])
        self.assertEqual(record['collect'], unfiltered)
        self.assertEqual([item['session_id'] for item in record['collect']['workers']], ['sess-a', 'sess-new'])

    def test_failed_step_is_recorded_with_its_fixed_line_and_the_loop_continues(self):
        answers = iter([True, True, False])
        services = self.services._replace(wait=self.advance_clock, orchestrator_alive=lambda root: next(answers))
        with mock.patch.object(status_round, 'request_round', side_effect=OSError(RAW_TEXT)):
            runner.run_loop(self.root, ORCH, status_round, services, threading.Event())
        self.assertEqual(self.tick_files(), [TICK_FILE, '20261127T101500Z.json'])
        for name in self.tick_files():
            with self.subTest(record=name):
                record = self.read_tick(name)
                self.assertEqual((record['request'], record['errors']),
                                 (None, [{'step': 'request', 'reason': runner.FAILURE_LINE}]))
                self.assertNotIn('/abs/path', (self.root / runner.TICK_DIRECTORY / name).read_text())

    def test_failed_collect_step_is_recorded_while_the_request_step_stands(self):
        status_round.request_round(self.root, ORCH, PREVIOUS_NOW)
        with mock.patch.object(status_round, 'collect_round', side_effect=status_round.MailboxError(RAW_TEXT)):
            self.tick()
        record = self.read_tick()
        self.assertEqual(record['request']['slot'], SLOT)
        self.assertEqual((record['collect'], record['errors']), (None, [{'step': 'collect', 'reason': runner.MAILBOX_LINE}]))

    def test_failed_worker_discovery_is_recorded_as_a_collect_error(self):
        with mock.patch.object(runner, 'collect_previous', side_effect=ValueError(RAW_TEXT)):
            self.tick()
        self.assertEqual(self.read_tick()['errors'], [{'step': 'collect', 'reason': runner.FAILURE_LINE}])

    def test_request_error_after_publication_is_recorded_as_request_null_with_error(self):
        status_round.request_round(self.root, ORCH, PREVIOUS_NOW)
        (self.root / 'inbox' / 'orchestrator').rmdir()
        self.tick()
        record = self.read_tick()
        self.assertIsNone(record['request'])
        self.assertEqual(record['errors'], [{'step': 'request', 'reason': runner.MAILBOX_LINE},
                                            {'step': 'collect', 'reason': runner.MAILBOX_LINE}])
        self.assertTrue((self.root / 'inbox' / 'proj' / f'{self.request_id()}.json').is_file())


class StatusTest(RunnerTestCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 27, 10, 9, 0, tzinfo=UTC)

    def test_status_names_runner_last_slot_and_tick(self):
        self.hold_runner_lock()
        self.tick(NOW)
        report = self.invoke_json('status')
        self.assertEqual(list(report), list(STATUS_NAME_KEYS))
        self.assertEqual((report['running'], report['pid'], report['endpoint'], report['started_at']),
                         (True, RUNNER_PID, ORCH, '2026-11-27T10:00:00Z'))
        self.assertEqual((report['current_slot'], report['last_slot'], report['current_slot_recorded']), (SLOT, SLOT, True))
        self.assertEqual(report['last_tick_path'], f'status-rounds/{TICK_FILE}')
        self.assertEqual(report['last_tick'], self.read_tick())
        self.assertIsNone(report['last_tick_problem'])

    def test_status_without_runner_still_names_the_last_tick(self):
        self.tick(NOW)
        report = self.invoke_json('status')
        self.assertEqual((report['running'], report['pid'], report['endpoint'], report['started_at']),
                         (False, None, None, None))
        self.assertEqual((report['last_slot'], report['last_tick']['slot']), (SLOT, SLOT))

    def test_status_creates_nothing_on_a_mailbox_without_records(self):
        before = snapshot(self.root)
        report = self.invoke_json('status')
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual((report['last_slot'], report['last_tick_path'], report['last_tick'], report['last_tick_problem']),
                         (None, None, None, None))
        self.assertIs(report['current_slot_recorded'], False)

    def test_current_slot_without_record_is_reported_as_not_recorded(self):
        self.tick(PREVIOUS_NOW)
        report = self.invoke_json('status')
        self.assertEqual((report['current_slot'], report['last_slot'], report['current_slot_recorded']),
                         (SLOT, PREVIOUS_SLOT, False))

    def test_names_that_are_not_tick_records_are_ignored(self):
        folder = self.root / runner.TICK_DIRECTORY
        folder.mkdir()
        for name in ('notes.txt', f'{TICK_FILE}.tmp', f'.{TICK_FILE}.0123.tmp', '20261399T250000Z.json'):
            (folder / name).write_text('{}')
        report = self.invoke_json('status')
        self.assertEqual((report['last_slot'], report['last_tick_path'], report['current_slot_recorded']), (None, None, False))

    def test_unreadable_newest_record_is_reported_with_the_fixed_text_and_a_relative_path(self):
        self.tick(PREVIOUS_NOW)
        outside = self.root / 'outside.json'
        outside.write_text(json.dumps({'slot': SLOT}))
        newest = self.root / runner.TICK_DIRECTORY / TICK_FILE
        builders = {
            'symbolic link': lambda: newest.symlink_to(outside),
            'oversized': lambda: newest.write_text(json.dumps({'slot': SLOT}) + ' ' * MAX_BYTES),
            'non-finite': lambda: newest.write_text('{"slot": "%s", "load": NaN}' % SLOT),
            'slot mismatch': lambda: newest.write_text(json.dumps({'slot': PREVIOUS_SLOT})),
            'not an object': lambda: newest.write_text('[]'),
        }
        for name, build in builders.items():
            with self.subTest(case=name):
                build()
                outcome = self.invoke('status')
                report = json.loads(outcome.stdout)
                newest.unlink()
                self.assertEqual((report['last_tick'], report['last_tick_problem']), (None, runner.UNREADABLE_TICK))
                self.assertEqual((report['last_tick_path'], report['last_slot']), (f'status-rounds/{TICK_FILE}', SLOT))
                self.assertNotIn(str(self.root), outcome.stdout)

    def test_status_rounds_that_is_a_symbolic_link_ends_with_the_fixed_line(self):
        (self.root / 'elsewhere').mkdir()
        (self.root / runner.TICK_DIRECTORY).symlink_to(self.root / 'elsewhere')
        self.assert_fixed_failure(self.invoke('status'), runner.MAILBOX_LINE)


class RunEntryTest(RunnerTestCase):
    def run_with(self, descriptor):
        return self.invoke('run', '--endpoint', ORCH, '--lock-fd', str(descriptor), '--token', 'ab')

    def open_descriptor(self, path):
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, descriptor)
        return descriptor

    def assert_run_refused(self, descriptor):
        before = snapshot(self.root)
        self.assert_fixed_failure(self.run_with(descriptor), runner.START_LINE)
        self.assertEqual(snapshot(self.root), before)

    def test_run_without_the_runner_lock_is_refused(self):
        (self.root / runner.RUNNER_LOCK).write_text('')
        self.assert_run_refused(self.open_descriptor(self.root / 'other.lock'))

    def test_run_while_another_runner_holds_the_lock_is_refused(self):
        lock = self.root / runner.RUNNER_LOCK
        self.hold_runner_lock()
        self.assert_run_refused(self.open_descriptor(lock))

    def test_run_with_a_closed_descriptor_is_refused(self):
        (self.root / runner.RUNNER_LOCK).write_text('')
        descriptor = os.open(self.root / runner.RUNNER_LOCK, os.O_RDWR)
        os.close(descriptor)
        self.assert_run_refused(descriptor)

    def test_run_with_a_lock_path_that_names_another_file_is_refused(self):
        lock = self.root / runner.RUNNER_LOCK
        descriptor = self.open_descriptor(lock)
        replacement = self.root / 'replacement'
        replacement.write_text('')
        os.replace(replacement, lock)
        self.assert_run_refused(descriptor)

    def test_run_without_a_lock_file_in_the_root_is_refused(self):
        self.assert_run_refused(self.open_descriptor(self.root / 'other.lock'))

    def test_run_writes_its_state_while_it_runs_ticks_once_and_leaves_an_empty_state_file(self):
        inert = self.isolate_signal_handlers()
        lock = self.root / runner.RUNNER_LOCK
        descriptor = self.open_descriptor(lock)
        answers, states = iter([True, False]), []

        def alive(root):
            states.append(runner.read_state(self.root))
            return next(answers)

        services = self.services._replace(wait=self.advance_clock, orchestrator_alive=alive)
        outcome = self.invoke('run', '--endpoint', ORCH, '--lock-fd', str(descriptor), '--token', 'ab', services=services)
        written = {'pid': os.getpid(), 'endpoint': ORCH, 'started_at': '2026-11-27T10:07:30Z', 'token': 'ab'}
        self.assertEqual(outcome, Outcome(0, '', ''))
        self.assertEqual(states, [written, written])
        self.assertEqual(lock.read_bytes(), b'')
        self.assertEqual(self.tick_files(), [TICK_FILE])
        for number, handler in inert.items():
            self.assertIs(signal.getsignal(number), handler)


class RunnerLoopTest(RunnerTestCase):
    def setUp(self):
        super().setUp()
        self.services = self.services._replace(wait=self.advance_clock)

    def alive_for(self, *answers):
        sequence = iter(answers)
        return self.services._replace(orchestrator_alive=lambda root: next(sequence))

    def test_first_tick_is_for_the_current_slot(self):
        stop = threading.Event()
        services = self.services._replace(wait=lambda interrupt, seconds: stop.set())
        runner.run_loop(self.root, ORCH, status_round, services, stop)
        self.assertEqual(self.tick_files(), [TICK_FILE])
        self.assertEqual(self.read_tick()['slot'], SLOT)

    def test_one_tick_per_slot_boundary(self):
        self.now = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
        stop = threading.Event()

        def advance_until_half_past(interrupt, seconds):
            self.advance_clock(interrupt, seconds)
            if self.now >= datetime(2026, 11, 27, 10, 30, 30, tzinfo=UTC):
                stop.set()

        runner.run_loop(self.root, ORCH, status_round, self.services._replace(wait=advance_until_half_past), stop)
        self.assertEqual(self.tick_files(), [TICK_FILE, '20261127T101500Z.json', '20261127T103000Z.json'])
        self.assertEqual([self.read_tick(name)['recorded_at'] for name in self.tick_files()],
                         ['2026-11-27T10:00:00Z', '2026-11-27T10:15:00Z', '2026-11-27T10:30:00Z'])

    def test_wait_steps_are_capped_and_the_last_one_ends_at_the_boundary(self):
        self.now = datetime(2026, 11, 27, 10, 14, 45, tzinfo=UTC)
        runner.run_loop(self.root, ORCH, status_round, self.alive_for(True, False), threading.Event())
        self.assertEqual(self.waits, [15.0])
        self.now = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
        self.waits.clear()
        runner.run_loop(self.root, ORCH, status_round, self.alive_for(True, False), threading.Event())
        self.assertEqual(self.waits, [runner.WAIT_STEP_SECONDS] * 30)

    def test_loop_ends_when_no_orchestrator_holds_its_lock(self):
        runner.run_loop(self.root, ORCH, status_round, self.alive_for(True, False), threading.Event())
        self.assertEqual(self.tick_files(), [TICK_FILE])
        runner.run_loop(self.root, ORCH, status_round, self.services._replace(orchestrator_alive=lambda root: False),
                        threading.Event())
        self.assertEqual(self.tick_files(), [TICK_FILE])

    def test_loop_ends_without_a_tick_when_stop_is_already_set(self):
        stop = threading.Event()
        stop.set()
        runner.run_loop(self.root, ORCH, status_round, self.services, stop)
        self.assertEqual((self.tick_files(), self.waits), ([], []))

    def test_loop_continues_when_a_tick_record_cannot_be_published(self):
        (self.root / 'elsewhere').mkdir()
        (self.root / runner.TICK_DIRECTORY).symlink_to(self.root / 'elsewhere')
        runner.run_loop(self.root, ORCH, status_round, self.alive_for(True, True, False), threading.Event())
        self.assertEqual(list((self.root / 'elsewhere').iterdir()), [])
        self.assertTrue((self.root / runner.TICK_DIRECTORY).is_symlink())
        self.assertTrue((self.root / 'inbox' / 'proj' / f'{self.request_id(slot="2026-11-27T10:15:00Z")}.json').is_file())

    def test_sigterm_during_a_wait_step_ends_the_loop_at_once(self):
        self.isolate_signal_handlers()
        self.now = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
        services = self.services._replace(wait=runner.real_wait)
        timer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM))
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with runner.stop_on_signals() as stop:
            timer.start()
            runner.run_loop(self.root, ORCH, status_round, services, stop)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(stop.is_set())

    def test_sigint_sets_stop_too(self):
        self.isolate_signal_handlers()
        with runner.stop_on_signals() as stop:
            os.kill(os.getpid(), signal.SIGINT)
            self.assertTrue(stop.wait(2))

    def test_run_restores_the_previous_signal_handlers(self):
        inert = self.isolate_signal_handlers()
        with runner.stop_on_signals():
            for number, handler in inert.items():
                self.assertIsNot(signal.getsignal(number), handler)
        for number, handler in inert.items():
            self.assertIs(signal.getsignal(number), handler)
        with self.assertRaises(RuntimeError), runner.stop_on_signals():
            raise RuntimeError
        for number, handler in inert.items():
            self.assertIs(signal.getsignal(number), handler)


class StaleStateTest(RunnerTestCase):
    def setUp(self):
        super().setUp()
        self.lock = self.root / runner.RUNNER_LOCK

    def hold_shared_lock(self):
        holder = HeldLock(self.lock, fcntl.LOCK_SH)
        self.addCleanup(holder.release)
        return holder

    def test_start_truncates_a_stale_state_before_the_orchestrator_probe(self):
        self.lock.write_text(json.dumps(RUNNING_STATE))
        services = self.services._replace(orchestrator_alive=lambda root: False)
        self.assert_fixed_failure(self.invoke('start', '--endpoint', ORCH, services=services), runner.NO_ORCHESTRATOR_LINE)
        self.assertEqual((self.lock.read_bytes(), self.spawned), (b'', []))

    def test_start_waits_out_a_probe_that_holds_only_a_shared_lock(self):
        self.lock.write_text(json.dumps(RUNNING_STATE))
        probe = self.hold_shared_lock()

        def release_on_third_pause(interrupt, seconds):
            self.waits.append(seconds)
            if len(self.waits) == 3:
                probe.release()
            return False

        result = self.invoke_json('start', '--endpoint', ORCH, services=self.services._replace(wait=release_on_third_pause))
        self.assertEqual(result['state'], 'started')
        self.assertEqual((len(self.spawned), self.waits), (1, [runner.POLL_SECONDS] * 3))

    def test_start_with_a_shared_holder_that_never_goes_away_ends_with_the_fixed_line(self):
        self.lock.write_text(json.dumps(RUNNING_STATE))
        self.hold_shared_lock()
        before = snapshot(self.root)
        self.assert_fixed_failure(self.invoke('start', '--endpoint', ORCH), runner.STATE_LINE)
        self.assertEqual(self.waits, [runner.POLL_SECONDS] * runner.START_ATTEMPTS)
        self.assertEqual((self.spawned, snapshot(self.root)), ([], before))

    def test_start_with_an_exclusive_holder_reports_already_running(self):
        self.hold_runner_lock()
        self.assertEqual(self.invoke_json('start', '--endpoint', ORCH)['state'], 'already_running')
        self.assertEqual(self.spawned, [])

    def test_stop_signals_only_the_pid_of_a_state_that_was_read_twice_equal(self):
        holder = self.hold_runner_lock(dict(RUNNING_STATE, pid=111))

        def replace_on_first_pause(interrupt, seconds):
            self.waits.append(seconds)
            if len(self.waits) == 1:
                holder.replace_content(json.dumps(dict(RUNNING_STATE, pid=222)).encode())
            return False

        def send_signal(pid, number):
            self.signals.append((pid, number))
            holder.release()

        services = self.services._replace(wait=replace_on_first_pause, send_signal=send_signal)
        self.assertEqual(self.invoke_json('stop', services=services), {'state': 'stopped'})
        self.assertEqual(self.signals, [(222, signal.SIGTERM)])

    def test_a_state_that_keeps_changing_ends_stop_and_status_with_the_fixed_line_and_sends_no_signal(self):
        holder = self.hold_runner_lock(dict(RUNNING_STATE, pid=1000))

        def change_every_pause(interrupt, seconds):
            self.waits.append(seconds)
            holder.replace_content(json.dumps(dict(RUNNING_STATE, pid=1000 + len(self.waits))).encode())
            return False

        services = self.services._replace(wait=change_every_pause)
        for command in ('stop', 'status'):
            with self.subTest(command=command):
                self.waits.clear()
                self.assert_fixed_failure(self.invoke(command, services=services), runner.STATE_LINE)
                self.assertEqual(len(self.waits), runner.STATE_ATTEMPTS)
                self.assertEqual(self.signals, [])


class SymlinkLoopTest(RunnerTestCase):
    def raw_loop_text(self):
        return f"Symlink loop from '{self.root}/status-rounds'"

    def assert_newest_tick_unreadable(self, outcome):
        report = json.loads(outcome.stdout)
        self.assertEqual((outcome.code, outcome.stderr), (0, ''))
        self.assertEqual((report['last_tick'], report['last_tick_problem']), (None, runner.UNREADABLE_TICK))
        self.assertEqual(report['last_tick_path'], f'status-rounds/{TICK_FILE}')
        self.assertNotIn(str(self.root), outcome.stdout)

    def test_status_with_a_self_looping_newest_tick_name_reports_the_fixed_text(self):
        folder = self.root / runner.TICK_DIRECTORY
        folder.mkdir()
        (folder / TICK_FILE).symlink_to(TICK_FILE)
        self.assert_newest_tick_unreadable(self.invoke('status'))

    def test_status_when_the_record_read_raises_with_an_absolute_path_reports_the_fixed_text(self):
        self.tick()
        with mock.patch.object(status_round.receive, 'read_json', side_effect=RuntimeError(self.raw_loop_text())):
            outcome = self.invoke('status')
        self.assert_newest_tick_unreadable(outcome)
        self.assertNotIn('Symlink loop', outcome.stdout)

    def test_looping_root_ends_with_the_mailbox_fixed_line(self):
        looping = self.root / 'loop'
        looping.symlink_to('loop')
        for command in (('start', '--endpoint', ORCH), ('stop',), ('status',)):
            with self.subTest(command=command[0]):
                outcome = self.invoke('--root', str(looping), *command, root=False)
                self.assertEqual(outcome, Outcome(1, '', runner.MAILBOX_LINE + '\n'))

    def test_looping_root_without_sibling_scripts_ends_with_the_mailbox_fixed_line(self):
        looping = self.root / 'loop'
        looping.symlink_to('loop')
        with mock.patch.object(runner, 'load_status_round', return_value=None):
            outcome = self.invoke('--root', str(looping), 'stop', root=False)
        self.assertEqual(outcome, Outcome(1, '', runner.MAILBOX_LINE + '\n'))

    def test_root_resolution_that_raises_with_an_absolute_path_ends_with_the_mailbox_fixed_line(self):
        raw = RuntimeError(self.raw_loop_text())
        for name, target, argv, root in (('resolved_root', 'resolved_root', ('status',), True),
                                         ('default_root', 'default_root', ('status',), False)):
            with self.subTest(case=name), mock.patch.object(status_round, target, side_effect=raw):
                outcome = self.invoke(*argv, root=root)
                self.assertEqual(outcome, Outcome(1, '', runner.MAILBOX_LINE + '\n'))
        unresolvable = mock.Mock(resolve=mock.Mock(side_effect=raw))
        with self.assertRaises(runner.RunnerError) as raised:
            runner.existing_directory(unresolvable)
        self.assertEqual(raised.exception.line, runner.MAILBOX_LINE)

    def test_tick_steps_that_raise_with_raw_text_are_recorded_with_the_fixed_failure_line(self):
        status_round.request_round(self.root, ORCH, PREVIOUS_NOW)
        raw = RuntimeError(self.raw_loop_text())
        with mock.patch.object(status_round, 'request_round', side_effect=raw), \
                mock.patch.object(status_round, 'collect_round', side_effect=raw):
            self.assertIs(self.tick(), True)
        record = self.read_tick()
        self.assertEqual((record['request'], record['collect']), (None, None))
        self.assertEqual(record['errors'], [{'step': 'request', 'reason': runner.FAILURE_LINE},
                                            {'step': 'collect', 'reason': runner.FAILURE_LINE}])
        text = (self.root / runner.TICK_DIRECTORY / TICK_FILE).read_text()
        self.assertNotIn('Symlink loop', text)
        self.assertNotIn(str(self.root), text)

    def test_loop_survives_a_tick_that_raises_and_goes_on_to_the_next_slot(self):
        answers = iter([True, True, False])
        services = self.services._replace(wait=self.advance_clock, orchestrator_alive=lambda root: next(answers))
        raw = RuntimeError(self.raw_loop_text())
        with mock.patch.object(runner, 'run_tick', side_effect=[raw, True]) as run_tick:
            runner.run_loop(self.root, ORCH, status_round, services, threading.Event())
        self.assertEqual([call.args[2] for call in run_tick.call_args_list],
                         [NOW, datetime(2026, 11, 27, 10, 15, 0, tzinfo=UTC)])


class StopRequestTest(RunnerTestCase):
    @contextlib.contextmanager
    def recorded_pipe(self):
        real_pipe, descriptors = os.pipe, []

        def pipe():
            pair = real_pipe()
            descriptors.extend(pair)
            return pair

        with mock.patch.object(os, 'pipe', side_effect=pipe):
            yield descriptors

    def assert_closed(self, descriptors):
        self.assertEqual(len(descriptors), 2)
        for descriptor in descriptors:
            with self.subTest(descriptor=descriptor), self.assertRaises(OSError):
                os.fstat(descriptor)

    def request(self):
        stop = runner.StopRequest()
        self.addCleanup(lambda: [os.close(descriptor) for descriptor in self.open_ends(stop)])
        return stop

    @staticmethod
    def open_ends(stop):
        ends = []
        for descriptor in (stop._reader, stop._writer):
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            ends.append(descriptor)
        return ends

    def test_wait_returns_false_after_the_timeout_without_a_stop(self):
        stop = self.request()
        self.assertIs(stop.wait(0.01), False)
        self.assertIs(stop.is_set(), False)

    def test_a_stop_set_before_the_wait_ends_it_at_once(self):
        stop = self.request()
        stop.set()
        started = time.monotonic()
        self.assertIs(stop.wait(30), True)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIs(stop.is_set(), True)

    def test_a_signal_handler_ends_a_long_wait_in_under_two_seconds(self):
        self.isolate_signal_handlers()
        timer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM))
        self.addCleanup(timer.cancel)
        with runner.stop_on_signals() as stop:
            timer.start()
            started = time.monotonic()
            self.assertIs(stop.wait(30), True)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_repeated_stop_requests_raise_nothing_even_when_the_pipe_is_full(self):
        stop = self.request()
        for _ in range(100_000):
            stop.set()
        self.assertIs(stop.is_set(), True)
        self.assertIs(stop.wait(0), True)

    def test_close_closes_both_ends(self):
        with self.recorded_pipe() as descriptors:
            stop = runner.StopRequest()
        stop.close()
        self.assert_closed(descriptors)

    def test_stop_on_signals_closes_the_request_and_restores_the_handlers_when_the_block_raises(self):
        inert = self.isolate_signal_handlers()
        with self.recorded_pipe() as descriptors, self.assertRaises(RuntimeError), runner.stop_on_signals():
            raise RuntimeError
        self.assert_closed(descriptors)
        for number, handler in inert.items():
            self.assertIs(signal.getsignal(number), handler)


class TickRecordShapeTest(RunnerTestCase):
    RECORD_KEYS = ['id', 'type', 'slot', 'recorded_at', 'orchestrator_endpoint', 'request', 'previous_slot',
                   'not_requested', 'collect', 'collect_omitted', 'errors']

    def test_tick_names_its_slot_the_slot_before_the_time_and_the_endpoint(self):
        tick = runner.make_tick(NOW, ORCH, status_round)
        self.assertEqual(tick, runner.Tick(datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC), SLOT, PREVIOUS_SLOT, NOW, ORCH))

    def test_record_keys_come_in_the_documented_order_and_errors_list_request_first(self):
        tick = runner.make_tick(NOW, ORCH, status_round)
        request_error = {'step': 'request', 'reason': runner.FAILURE_LINE}
        collect_error = {'step': 'collect', 'reason': runner.MAILBOX_LINE}
        record = runner.build_tick_record(tick, status_round, runner.StepOutcome(None, request_error),
                                          runner.StepOutcome(None, collect_error))
        self.assertEqual(list(record), self.RECORD_KEYS)
        self.assertEqual(record['errors'], [request_error, collect_error])
        self.assertEqual((record['request'], record['collect'], record['not_requested'], record['collect_omitted']),
                         (None, None, [], None))

    def test_record_without_errors_carries_both_results(self):
        tick = runner.make_tick(NOW, ORCH, status_round)
        previous = {'not_requested': [{'project': 'proj'}], 'collect': {'slot': PREVIOUS_SLOT}, 'collect_omitted': None}
        record = runner.build_tick_record(tick, status_round, runner.StepOutcome({'slot': SLOT}, None),
                                          runner.StepOutcome(previous, None))
        self.assertEqual(list(record), self.RECORD_KEYS)
        self.assertEqual((record['request'], record['collect'], record['not_requested'], record['errors']),
                         ({'slot': SLOT}, {'slot': PREVIOUS_SLOT}, [{'project': 'proj'}], []))

    def test_attempt_returns_the_result_without_an_error_on_success(self):
        before = snapshot(self.root)
        self.assertEqual(runner.attempt(status_round, 'request', lambda: {'slot': SLOT}),
                         runner.StepOutcome({'slot': SLOT}, None))
        self.assertEqual(snapshot(self.root), before)

    def test_attempt_returns_the_fixed_error_entry_on_failure(self):
        failures = {'os error': (OSError(RAW_TEXT), runner.FAILURE_LINE),
                    'runtime error': (RuntimeError(RAW_TEXT), runner.FAILURE_LINE),
                    'mailbox error': (status_round.MailboxError(RAW_TEXT), runner.MAILBOX_LINE)}
        before = snapshot(self.root)
        for name, (error, line) in failures.items():
            with self.subTest(case=name):
                def action(error=error):
                    raise error
                self.assertEqual(runner.attempt(status_round, 'collect', action),
                                 runner.StepOutcome(None, {'step': 'collect', 'reason': line}))
        self.assertEqual(snapshot(self.root), before)

    def test_attempt_lets_an_unanticipated_failure_through(self):
        def action():
            raise KeyError('unexpected')
        with self.assertRaises(KeyError):
            runner.attempt(status_round, 'request', action)


class VanishingRunnerTest(RunnerTestCase):
    def setUp(self):
        super().setUp()
        self.holder = self.hold_runner_lock(None)
        self.vanishing = self.services._replace(wait=self.release_on_first_pause)

    def release_on_first_pause(self, interrupt, seconds):
        self.waits.append(seconds)
        self.holder.release()
        return False

    def test_stop_of_a_runner_that_ends_while_its_state_is_read_reports_not_running(self):
        self.assertEqual(self.invoke_json('stop', services=self.vanishing), {'state': 'not_running'})
        self.assertEqual((self.signals, self.waits), ([], [runner.POLL_SECONDS]))

    def test_status_of_a_runner_that_ends_while_its_state_is_read_reports_it_as_not_running(self):
        before = snapshot(self.root)
        report = self.invoke_json('status', services=self.vanishing)
        self.assertEqual((report['running'], report['pid'], report['endpoint'], report['started_at']),
                         (False, None, None, None))
        self.assertEqual((self.waits, snapshot(self.root)), ([runner.POLL_SECONDS], before))

    def test_start_while_the_runner_ends_takes_the_lock_and_spawns_exactly_one_runner(self):
        result = self.invoke_json('start', '--endpoint', ORCH, services=self.vanishing)
        self.assertEqual((result['state'], len(self.spawned)), ('started', 1))
        self.assertEqual(self.waits, [runner.POLL_SECONDS])

    def test_start_that_keeps_finding_a_vanished_runner_ends_with_the_state_line_after_the_bound(self):
        with mock.patch.object(runner, 'take_runner_lock', return_value=False) as take, \
                mock.patch.object(runner, 'report_running', side_effect=runner.RunnerGone):
            outcome = self.invoke('start', '--endpoint', ORCH)
        self.assert_fixed_failure(outcome, runner.STATE_LINE)
        self.assertEqual((take.call_count, self.spawned), (runner.START_ATTEMPTS, []))

    def test_busy_lock_whose_state_never_becomes_readable_ends_with_the_state_line_after_the_bound(self):
        with self.assertRaises(runner.RunnerError) as raised:
            runner.read_busy_state(self.root, self.services)
        self.assertEqual(raised.exception.line, runner.STATE_LINE)
        self.assertEqual(self.waits, [runner.POLL_SECONDS] * runner.STATE_ATTEMPTS)

    def test_stable_state_is_returned_although_the_lock_is_busy(self):
        self.holder.replace_content(json.dumps(RUNNING_STATE).encode())
        self.assertEqual(runner.read_busy_state(self.root, self.services), RUNNING_STATE)
        self.assertEqual(self.waits, [runner.POLL_SECONDS])


class InstalledRunnerTestCase(StatusRoundTestCase):
    """Real processes against a copy of the scripts in the installed layout, with the orchestrator lock held."""

    def setUp(self):
        super().setUp()
        self.scripts = install_layout(self.root)
        self.script = self.scripts / 'status_round_runner.py'
        orchestrator = HeldLock(self.root / runner.ORCHESTRATOR_LOCK)
        self.addCleanup(orchestrator.release)
        self.addCleanup(self.stop_leftover_runner)

    def run_installed(self, *args):
        return subprocess.run([sys.executable, str(self.script), '--root', str(self.root), *args],
                              capture_output=True, text=True, timeout=60, env=environment_with_bytecode())

    def run_installed_json(self, *args):
        completed = self.run_installed(*args)
        self.assertEqual((completed.returncode, completed.stderr), (0, ''))
        return json.loads(completed.stdout)

    def stop_leftover_runner(self):
        self.run_installed('stop')
        if not wait_until(lambda: not runner.runner_lock_busy(self.root), 5):
            state = runner.read_state(self.root)
            if state:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(state['pid'], signal.SIGKILL)

    def wait_for_first_tick(self):
        self.assertTrue(wait_until(lambda: any((self.root / runner.TICK_DIRECTORY).glob('*.json'))))


class DetachedRunnerTest(InstalledRunnerTestCase):
    def test_started_runner_outlives_start_and_holds_the_lock(self):
        started = self.run_installed_json('start', '--endpoint', ORCH)
        self.assertEqual((started['state'], started['endpoint']), ('started', ORCH))
        self.assertNotEqual(started['pid'], os.getpid())
        os.kill(started['pid'], 0)
        self.assertTrue(runner.runner_lock_busy(self.root))
        self.assertEqual(runner.read_state(self.root)['pid'], started['pid'])
        self.wait_for_first_tick()
        report = self.run_installed_json('status')
        self.assertEqual((report['running'], report['pid']), (True, started['pid']))
        self.assertEqual(report['last_tick']['type'], 'status_round_tick')

    def test_second_start_starts_no_second_runner(self):
        first = self.run_installed_json('start', '--endpoint', ORCH)
        second = self.run_installed_json('start', '--endpoint', ORCH)
        self.assertEqual((second['state'], second['pid']), ('already_running', first['pid']))

    def test_stop_ends_a_real_runner_within_the_bound(self):
        started = self.run_installed_json('start', '--endpoint', ORCH)
        self.assertEqual(self.run_installed_json('stop'), {'state': 'stopped'})
        self.assertFalse(runner.runner_lock_busy(self.root))
        self.assertTrue(wait_until(lambda: not self.process_alive(started['pid']), 5))
        self.assertEqual(self.run_installed_json('stop'), {'state': 'not_running'})
        self.assertIs(self.run_installed_json('status')['running'], False)

    def test_stop_then_start_runs_with_the_new_endpoint(self):
        self.run_installed_json('start', '--endpoint', ORCH)
        refused = self.run_installed('start', '--endpoint', OTHER_ENDPOINT)
        self.assertEqual((refused.returncode, refused.stdout, refused.stderr), (1, '', runner.OTHER_ENDPOINT_LINE + '\n'))
        self.assertEqual(self.run_installed_json('stop'), {'state': 'stopped'})
        restarted = self.run_installed_json('start', '--endpoint', OTHER_ENDPOINT)
        self.assertEqual(restarted['endpoint'], OTHER_ENDPOINT)
        self.assertEqual(self.run_installed_json('status')['endpoint'], OTHER_ENDPOINT)

    @staticmethod
    def process_alive(pid):
        """False once the process has exited, also while a container without init leaves it unreaped."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
        except OSError:
            return True
        return fields[0] not in ('Z', 'X')


class InstalledLayoutTest(InstalledRunnerTestCase):
    def cache_entries(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')
                      if path.name == '__pycache__' or path.suffix == '.pyc')

    def test_start_status_stop_write_no_bytecode_and_no_file_beyond_lock_and_tick_record(self):
        (self.root / 'sessions' / 'proj--a.json').unlink()
        before = set(file_contents(self.root))
        started = self.run_installed_json('start', '--endpoint', ORCH)
        self.wait_for_first_tick()
        self.assertEqual(self.run_installed_json('status')['pid'], started['pid'])
        self.assertEqual(self.run_installed_json('stop'), {'state': 'stopped'})
        created = set(file_contents(self.root)) - before
        self.assertEqual({name for name in created if name != runner.RUNNER_LOCK and not name.startswith('status-rounds/')},
                         set())
        self.assertIn(runner.RUNNER_LOCK, created)
        self.assertEqual(self.cache_entries(), [])

    def test_older_sibling_scripts_end_start_with_the_fixed_line(self):
        (self.scripts / 'status_round.py').write_text('"""An older status_round without the runner entry points."""\n')
        before = file_contents(self.root)
        completed = self.run_installed('start', '--endpoint', ORCH)
        self.assertEqual((completed.returncode, completed.stdout, completed.stderr), (1, '', runner.SIBLING_LINE + '\n'))
        self.assertEqual(file_contents(self.root), before)
        self.assertEqual(self.cache_entries(), [])


if __name__ == '__main__':
    unittest.main()
