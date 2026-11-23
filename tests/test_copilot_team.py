"""Verify team startup without accessing a live tmux server or a Copilot account."""
import contextlib
import fcntl
import io
import itertools
import json
import os
from pathlib import Path
import runpy
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch


PLUGIN = Path(__file__).resolve().parents[1] / 'workspace' / 'copilot-mailbox'


class CopilotTeamTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "team mailbox's files"
        self.plugin = self.root / 'copilot-mailbox'
        self.plugin.mkdir(parents=True)
        shutil.copy2(PLUGIN / 'start', self.plugin / 'start')
        self.entries = []
        for project in ('alpha', 'beta.v2', 'unselected'):
            cwd = self.root.parent / (project + ' worktree')
            cwd.mkdir()
            self.entries.append({'id': project, 'working_directory': str(cwd)})
        self.registry = self.root / 'projects.json'
        self.registry.write_text(json.dumps({'projects': self.entries}), encoding='utf-8')
        self.program = runpy.run_path(str(PLUGIN / 'team'))['main']
        self.server = TmuxDouble(self.root)
        self.addCleanup(self.server.close)
        self.binary = self.plugin / 'team'

    def invoke(self, *args, inside_tmux=False, missing=None, interactive=True, pane=None):
        errors = io.StringIO()
        environment = {'PATH': os.defpath}
        if inside_tmux:
            environment['TMUX'] = f'{self.server.socket_path},123,0'
        if pane is not None:
            environment['TMUX_PANE'] = pane
        with (patch.dict(self.program.__globals__, __file__=str(self.binary)),
              patch('subprocess.run', side_effect=self.server),
              patch.dict(os.environ, environment, clear=True),
              patch('sys.argv', [str(self.binary), *args]),
              patch('shutil.which', side_effect=lambda exe: None if exe == missing else '/bin/' + exe),
              patch('time.monotonic', side_effect=itertools.count()), patch('time.sleep'),
              patch('sys.stdin.isatty', return_value=interactive),
              patch('os.chdir'), patch('os.execvp') as attach, contextlib.redirect_stderr(errors)):
            try:
                self.program()
                status = 0
            except SystemExit as stopped:
                status = stopped.code
        return status, errors.getvalue(), attach

    def test_repeated_project_starts_distinct_workers_with_one_coordinator_and_project_scope(self):
        code, errors, attached = self.invoke('alpha', 'beta.v2', 'alpha')
        self.assertEqual(code, 0, errors)
        calls = self.server.calls
        launcher = str(self.plugin / 'team')
        sessions = [call for call in calls if call[0] == 'new-session']
        self.assertEqual(len(sessions), 1)
        coordinator = sessions[0][sessions[0].index(launcher) + 1:]
        self.assertEqual(coordinator[:2], ('--launch-pane', 'orchestrator'))
        self.assertEqual(coordinator[coordinator.index('--model') + 1], 'auto')
        self.assertCountEqual([value for flag, value in zip(coordinator, coordinator[1:]) if flag == '--project'],
                              ['alpha', 'beta.v2'])
        workers = [call[call.index(launcher) + 1:] for call in calls if call[0] == 'new-window']
        self.assertCountEqual([w[2] for w in workers], ['alpha', 'beta.v2', 'alpha'])
        numbers = [(w[2], w[w.index('--worker-number') + 1] if '--worker-number' in w else '1') for w in workers]
        self.assertCountEqual(numbers, [('alpha', '1'), ('alpha', '2'), ('beta.v2', '1')])
        self.assertEqual(set(self.server.projects.split(',')), {'alpha', 'alpha+2', 'beta.v2'})
        for worker in workers:
            self.assertEqual(worker[:2], ('--launch-pane', 'worker'))
            self.assertEqual(worker[worker.index('--model') + 1], 'auto')
        self.assertFalse(any('unselected' in call or '--allow-all' in call for call in calls))
        handoff = attached.call_args.args[1]
        self.assertIn('if-shell', handoff)
        self.assertIn('attach-session', handoff[-2])
        self.assertIn('select-window -t %0', handoff[-2])

    def test_repeat_with_same_projects_in_reverse_order_only_switches_to_existing_team(self):
        result, errors, _ = self.invoke('beta.v2', 'alpha', '--model', 'example-model',
                                      '--reasoning-effort', 'high')
        self.assertEqual(result, 0, errors)
        startup = [c for c in self.server.calls if c[0] in ('new-session', 'new-window')]
        self.assertTrue(startup)
        self.assertEqual(len(startup), 3)
        for call in startup:
            self.assertEqual(call[call.index('--model') + 1], 'example-model')
            self.assertEqual(call[call.index('--reasoning-effort') + 1], 'high')
        prior = self.server.projects
        self.server.calls.clear()
        result, errors, attached = self.invoke('alpha', 'beta.v2', inside_tmux=True)
        self.assertEqual(result, 0, errors)
        self.assertIn('switch-client', attached.call_args.args[1][-2])
        self.assertEqual(self.server.projects, prior)
        self.assertFalse(any(c[0] in ('new-session', 'new-window', 'rename-session', 'kill-session')
                             for c in self.server.calls))

    def test_roles_launch_with_independent_harness_model_and_effort(self):
        for orchestrator, worker in itertools.product(('claude', 'copilot', 'codex'), repeat=2):
            with self.subTest(orchestrator=orchestrator, worker=worker):
                self.server.close()
                self.server = TmuxDouble(self.root)
                self.addCleanup(self.server.close)
                status, error, _ = self.invoke('alpha', 'beta.v2',
                    '--orchestrator-harness', orchestrator, '--orchestrator-model', 'planner',
                    '--orchestrator-effort', 'high', '--worker-harness', worker,
                    '--worker-model', 'builder', '--worker-effort', 'low')
                self.assertEqual(status, 0, error)
                commands = [c for c in self.server.calls if c[0] in ('new-session', 'new-window')]
                self.assertEqual(len(commands), 3)
                for pane in commands:
                    expected = (orchestrator, 'planner', 'high') if pane[0] == 'new-session' else (worker, 'builder', 'low')
                    self.assertEqual(tuple(pane[pane.index(flag) + 1] for flag in
                                           ('--harness', '--model', '--reasoning-effort')), expected)

    def test_default_native_models_require_only_selected_executables_at_creation(self):
        args = ('alpha', '--orchestrator-harness', 'claude', '--worker-harness', 'codex')
        for absent in ('claude', 'codex'):
            with self.subTest(absent=absent):
                status, error, attach = self.invoke(*args, missing=absent)
                self.assertNotEqual(status, 0)
                self.assertTrue(error.strip())
                attach.assert_not_called()
                self.assertFalse(any(c[0] == 'new-session' for c in self.server.calls))
        status, error, _ = self.invoke(*args, missing='copilot')
        self.assertEqual(status, 0, error)
        created = [c for c in self.server.calls if c[0] in ('new-session', 'new-window')]
        self.assertEqual(len(created), 2)
        for pane in created:
            self.assertNotIn('--model', pane)
            self.assertNotIn('--reasoning-effort', pane)
        self.server.calls.clear()
        status, error, _ = self.invoke(*args, missing='claude')
        self.assertEqual(status, 0, error)
        self.assertFalse(any(c[0] == 'new-session' for c in self.server.calls))

    def test_fresh_team_request_refuses_an_existing_team_with_the_same_projects(self):
        code, error, _ = self.invoke('--new-team', 'alpha', '--model', 'first')
        self.assertEqual(code, 0, error)
        panes = self.server.panes.copy()
        self.server.calls.clear()
        code, error, attached = self.invoke('--new-team', 'alpha', '--model', 'second')
        self.assertNotEqual(code, 0)
        self.assertIn('Another team', error)
        self.assertEqual(self.server.panes, panes)
        self.assertFalse(any(call[0] in ('new-window', 'new-session', 'kill-session')
                             for call in self.server.calls))
        attached.assert_not_called()

    def test_reconnect_requires_matching_worker_counts_and_never_creates_a_second_coordinator(self):
        code, errors, _ = self.invoke('alpha', 'alpha', 'beta.v2')
        self.assertEqual(code, 0, errors)
        self.server.calls.clear()
        self.assertEqual(self.invoke('beta.v2', 'alpha', 'alpha')[0], 0)
        code, _, attach = self.invoke('alpha', 'beta.v2')
        self.assertNotEqual(code, 0)
        attach.assert_not_called()
        self.assertFalse(any(call[0] in ('new-window', 'new-session', 'kill-session') for call in self.server.calls))

    def test_running_team_with_different_projects_is_preserved(self):
        self.assertEqual(self.invoke('alpha')[0], 0)
        initial = self.server.projects
        self.server.calls.clear()
        code, errors, attached = self.invoke('alpha', 'beta.v2')
        self.assertNotEqual(code, 0)
        self.assertTrue(errors.strip())
        self.assertEqual(self.server.projects, initial)
        self.assertFalse(any(c[0] in ('new-session', 'new-window', 'kill-session') for c in self.server.calls))
        attached.assert_not_called()

    def test_invalid_second_project_leaves_no_running_participants(self):
        for name in ('../unselected', 'missing', 'orchestrator', 'bad;command'):
            with self.subTest(name=name):
                code, errors, attached = self.invoke('alpha', name)
                self.assertNotEqual(code, 0)
                self.assertTrue(errors.strip())
                self.assertFalse(any(c[0] in ('new-window', 'new-session') for c in self.server.calls))
                attached.assert_not_called()

    def test_case_aliases_in_explicit_project_selection_never_start_a_participant(self):
        self.registry.write_text(json.dumps({'projects': [*self.entries, dict(self.entries[0], id='ALPHA'),
                                                         dict(self.entries[0], id='Orchestrator')]}))
        for args in [('alpha',), ('alpha', 'ALPHA'), ('Orchestrator',)]:
            with self.subTest(args=args):
                code, errors, attached = self.invoke(*args)
                self.assertNotEqual(code, 0)
                self.assertTrue(errors.strip())
                self.assertFalse(any(cmd[0] in ('new-session', 'new-window') for cmd in self.server.calls))
                attached.assert_not_called()

    def test_registry_with_unavailable_checkout_reports_a_fixed_error(self):
        self.entries[0]['working_directory'] = str(self.root / 'private-checkout-name')
        self.registry.write_text(json.dumps(self.entries), encoding='utf-8')
        code, errors, attached = self.invoke('alpha')
        self.assertNotEqual(code, 0)
        self.assertTrue(errors.strip())
        self.assertNotIn('private-checkout-name', errors)
        self.assertNotIn('Traceback', errors)
        self.assertFalse(any(c[0] in ('new-session', 'new-window') for c in self.server.calls))
        attached.assert_not_called()

    def test_missing_programs_are_reported_before_creating_sessions(self):
        for missing in ('tmux', 'copilot'):
            with self.subTest(missing=missing):
                code, errors, attached = self.invoke('alpha', missing=missing)
                self.assertNotEqual(code, 0)
                self.assertTrue(errors.strip())
                self.assertFalse(any(c[0] in ('new-window', 'new-session') for c in self.server.calls))
                attached.assert_not_called()

    def test_existing_standalone_coordinator_is_preserved(self):
        with (self.root / '.copilot-mailbox-orchestrator.lock').open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            code, errors, attached = self.invoke('alpha')
        self.assertNotEqual(code, 0)
        self.assertTrue(errors.strip())
        self.assertFalse(any(c[0] in ('new-window', 'new-session', 'kill-session') for c in self.server.calls))
        attached.assert_not_called()

    def test_noninteractive_start_or_missing_role_launcher_never_spawns_a_team(self):
        code, _, attached = self.invoke('alpha', interactive=False)
        self.assertNotEqual(code, 0)
        attached.assert_not_called()
        (self.plugin / 'start').unlink()
        code, errors, attached = self.invoke('alpha')
        self.assertNotEqual(code, 0)
        self.assertIn('copilot-mailbox/start', errors)
        self.assertEqual(self.server.calls, [])
        attached.assert_not_called()

    def test_competing_lock_holder_cannot_make_an_untagged_coordinator_ready(self):
        self.server.coordinator_starts = False
        code, errors, attached = self.invoke('alpha')
        self.assertNotEqual(code, 0)
        self.assertTrue(errors.strip())
        self.assertFalse(any(c[0] == 'new-window' for c in self.server.calls))
        self.assertTrue(any(c[0] == 'kill-session' for c in self.server.calls))
        attached.assert_not_called()

    def test_internal_worker_ready_tag_follows_successful_participant_validation(self):
        code, errors, executed = self.invoke('--launch-pane', 'worker', 'alpha', '--model', 'example',
                                            inside_tmux=True, pane='%42')
        self.assertEqual(code, 0, errors)
        pinned = ('set-option', '-p', '-t', '%42', '@mailbox_initial_pid', str(os.getpid()))
        ready = ('set-option', '-p', '-t', '%42', '@copilot_route', 'alpha')
        self.assertIn(pinned, self.server.calls)
        self.assertIn(ready, self.server.calls)
        self.assertLess(self.server.calls.index(pinned), self.server.calls.index(ready))
        tool, args = executed.call_args.args
        self.assertEqual(tool, 'copilot')
        self.assertEqual(args[args.index('--agent') + 1], 'mailbox-worker')
        self.assertEqual(args[args.index('--model') + 1], 'example')
        self.server.calls.clear()
        code, _, executed = self.invoke('--launch-pane', 'worker', 'absent', inside_tmux=True, pane='%42')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.server.calls, [])
        executed.assert_not_called()

    def test_participant_invokes_readiness_hook_with_validated_route_before_exec(self):
        start = runpy.run_path(str(PLUGIN / 'start'))['main']
        history = []
        launcher = self.plugin / 'start'
        with (patch.dict(start.__globals__, __file__=str(launcher)),
              patch('sys.argv', [str(launcher), 'worker', 'alpha']),
              patch.dict(os.environ, {}, clear=True), patch('os.chdir'),
              patch('os.execvp', side_effect=lambda program, argv: history.append(program))):
            start(on_ready=lambda: history.append(os.environ['HARNESS_MAILBOX_ROUTE']))
        self.assertEqual(history, ['alpha', 'copilot'])

    def test_internal_coordinator_rejected_by_role_lock_never_reports_ready(self):
        with (self.root / '.copilot-mailbox-orchestrator.lock').open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            code, _, executed = self.invoke('--launch-pane', 'orchestrator', inside_tmux=True, pane='%8')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.server.calls, [])
        executed.assert_not_called()

    def test_internal_cli_rejects_missing_role_arguments_and_invalid_panes(self):
        for args, pane in [((), '%7'), (('worker',), '%7'), (('worker', 'alpha'), 'invalid-pane')]:
            with self.subTest(args=args, pane=pane):
                code, errors, attached = self.invoke('--launch-pane', *args, inside_tmux=True, pane=pane)
                self.assertNotEqual(code, 0)
                self.assertTrue(errors.strip())
                self.assertNotIn('Traceback', errors)
                self.assertEqual(self.server.calls, [])
                attached.assert_not_called()

    def test_existing_session_with_closed_coordinator_keeps_workers(self):
        self.assertEqual(self.invoke('alpha')[0], 0)
        self.server.calls.clear()
        self.server.panes.pop('%0')
        code, errors, attached = self.invoke('alpha')
        self.assertNotEqual(code, 0)
        self.assertTrue(errors.strip())
        self.assertNotIn('untrusted tmux failure', errors)
        self.assertFalse(any(c[0] in ('new-window', 'new-session', 'kill-session') for c in self.server.calls))
        attached.assert_not_called()

    def test_repeat_preserves_survivors_when_a_worker_has_exited(self):
        for retained in (False, True):
            with self.subTest(retained=retained):
                self.server.close()
                self.server = TmuxDouble(self.root)
                self.addCleanup(self.server.close)
                self.assertEqual(self.invoke('alpha', 'beta.v2')[0], 0)
                pane = next(k for k, v in self.server.panes.items() if v[1] == 'beta.v2')
                if retained:
                    self.server.panes[pane] = (1, 'beta.v2')
                else:
                    del self.server.panes[pane]
                surviving = self.server.panes.copy()
                self.server.calls.clear()
                status, errors, attached = self.invoke('beta.v2', 'alpha')
                self.assertNotEqual(status, 0)
                self.assertTrue(errors.strip())
                self.assertEqual(self.server.panes, surviving)
                self.assertFalse(any(c[0] in ('new-window', 'new-session', 'kill-session')
                                     for c in self.server.calls))
                attached.assert_not_called()

    def test_partial_start_cleans_only_the_session_it_created(self):
        self.server.fail_on = 'new-window'
        code, errors, attached = self.invoke('alpha', 'beta.v2')
        self.assertNotEqual(code, 0)
        self.assertTrue(errors.strip())
        self.assertNotIn('untrusted tmux failure', errors)
        self.assertNotIn('Traceback', errors)
        created, = [c for c in self.server.calls if c[0] == 'new-session']
        killed, = [c for c in self.server.calls if c[0] == 'kill-session']
        self.assertEqual(killed[killed.index('-t') + 1].lstrip('='), created[created.index('-s') + 1])
        attached.assert_not_called()


class TmuxDouble:
    """Record the executable boundary; no real server, subprocesses or shell parsing."""
    def __init__(self, root):
        self.root = root
        self.socket_dir = tempfile.TemporaryDirectory(dir='/tmp')
        self.socket_path = Path(self.socket_dir.name) / 'fixture socket'
        with socket.socket(socket.AF_UNIX) as stub:
            stub.bind(str(self.socket_path))
        self.owner = None
        self.calls = []
        self.projects = None
        self.staged = None
        self.fail_on = None
        self.coordinator_starts = True
        self.panes = {}

    def close(self):
        self.socket_dir.cleanup()
        if self.owner is not None:
            self.owner.close()
            self.owner = None

    def __call__(self, arguments, *, check, **options):
        executable, *rest = arguments
        if executable != "tmux":
            raise AssertionError(executable)
        args = tuple(map(str, rest))
        if args[:1] == ('-S',):
            if args[1] != str(self.socket_path):
                raise AssertionError('Unexpected tmux server selected')
            args = args[2:]
        self.calls.append(args)
        command = args[0]
        stdout = ''
        code = 0
        if command == self.fail_on:
            code = 1
        elif command == 'new-session':
            stdout = '%0\n'
            self.owner = (self.root / '.copilot-mailbox-orchestrator.lock').open('a')
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.panes['%0'] = (0, 'orchestrator' if self.coordinator_starts else '')
        elif command == 'display-message':
            token = 'ab' * 16
            stdout = f'$7|100|123|200|{token}|{token}|orchestrator|200 {self.socket_path}\n'
        elif command == 'new-window':
            pane = '%' + str(len(self.panes))
            self.panes[pane] = (0, args[args.index('-n') + 1])
            stdout = pane + '\n'
        elif command == 'list-panes':
            stdout = ''.join(f'{pane} {dead} {role}\n' for pane, (dead, role) in self.panes.items())
        elif command == 'has-session':
            code = int(self.owner is None and self.projects is None)
        elif command == 'show-options':
            stdout = (self.projects or '') + '\n'
        elif command == 'set-option':
            self.staged = args[-1]
        elif command == 'rename-session':
            self.projects = self.staged
        elif command == 'kill-session':
            self.staged = None
            self.close()
        if check and code:
            raise subprocess.CalledProcessError(code, args, stderr='untrusted tmux failure')
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr='')
