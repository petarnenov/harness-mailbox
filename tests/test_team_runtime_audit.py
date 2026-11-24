"""Regression cases at the participant CLI and tmux process boundaries."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import pty
import runpy
import shutil
import struct
import subprocess
import sys
import time
import tempfile
import termios
import unittest
import uuid
from unittest.mock import patch


PLUGIN = Path(__file__).resolve().parents[1] / 'workspace/copilot-mailbox'


class RuntimeAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        # Keep tmux sockets under a short path even with platform-specific temporary homes.
        self.root = self.base / 'mailbox'
        self.project = self.base / 'application'
        self.project.mkdir()
        self.plugin = self.root / 'copilot-mailbox'
        shutil.copytree(PLUGIN, self.plugin, ignore=shutil.ignore_patterns('__pycache__'))
        (self.root / 'projects.json').write_text(json.dumps([{'id': 'app', 'working_directory': str(self.project)}]))
        provider = self.base / 'copilot'
        provider.write_text('#!/bin/sh\nprintf "%s\\n" "$HARNESS_MAILBOX_ROUTE"\n')
        provider.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.base) + os.pathsep + os.environ['PATH'])

    def launch(self, *args):
        return subprocess.run([self.plugin / 'start', *args], text=True, capture_output=True, env=self.env,
                              check=False, timeout=4)

    def test_worker_role_is_decided_by_parser_regardless_of_option_order(self):
        for args in (('--model', 'sample', 'worker', 'app'), ('worker', '--model', 'sample', 'app')):
            with self.subTest(args=args):
                outcome = self.launch(*args)
                self.assertEqual(outcome.returncode, 0, outcome.stderr)
                self.assertEqual(outcome.stdout.strip(), 'app')
        self.assertFalse((self.root / '.copilot-mailbox-orchestrator.lock').exists())

    def test_incomplete_worker_command_cannot_become_an_orchestrator(self):
        outcome = self.launch('--model', 'sample', 'worker')
        self.assertNotEqual(outcome.returncode, 0)
        self.assertEqual(outcome.stdout, '')
        self.assertFalse((self.root / '.copilot-mailbox-orchestrator.lock').exists())

    def test_malformed_nested_registry_returns_the_fixed_launcher_diagnostic(self):
        (self.root / 'projects.json').write_text('[' * 2000 + ']' * 2000)
        outcome = self.launch('worker', 'app')
        self.assertNotEqual(outcome.returncode, 0)
        self.assertIn('Could not open', outcome.stderr)
        self.assertNotIn('Traceback', outcome.stderr)

    def test_standalone_coordinator_rejects_invalid_registry_before_initialization(self):
        for data in (None, 'invalid', {'projects': None}, {'projects': 1}):
            with self.subTest(data=data):
                (self.root / 'projects.json').write_text(json.dumps(data))
                outcome = self.launch('orchestrator')
                self.assertNotEqual(outcome.returncode, 0)
                self.assertEqual(outcome.stdout, '')
                self.assertIn('Could not open', outcome.stderr)
                self.assertNotIn('Traceback', outcome.stderr)
                for name in ('inbox', 'sessions', 'tasks', '.copilot-mailbox-orchestrator.lock'):
                    self.assertFalse((self.root / name).exists(), name)

    def test_standalone_coordinator_accepts_empty_legacy_and_wrapped_registries(self):
        for data in ([], {}, {'projects': []}, {'projects': {}}):
            with self.subTest(data=data):
                (self.root / 'projects.json').write_text(json.dumps(data))
                outcome = self.launch('orchestrator')
                self.assertEqual(outcome.returncode, 0, outcome.stderr)
                self.assertEqual(outcome.stdout.strip(), 'orchestrator')

    def test_registry_for_a_missing_user_fails_without_exposing_filesystem_details(self):
        missing_home = '~missing-account-' + uuid.uuid4().hex
        (self.root / 'projects.json').write_text(json.dumps([{'id': 'app', 'working_directory': missing_home}]))
        outcome = self.launch('worker', 'app')
        self.assertNotEqual(outcome.returncode, 0)
        self.assertIn('Could not open', outcome.stderr)
        self.assertNotIn('Traceback', outcome.stderr)
        self.assertNotIn(missing_home, outcome.stderr)

    def test_cli_usage_errors_do_not_repeat_invalid_values(self):
        for name, args in [('team', ['app', '--worker-harness']), ('start', ['worker', 'app', '--harness'])]:
            for invalid in (['private-placeholder'], ['copilot', '--private-placeholder']):
                with self.subTest(name=name, invalid=invalid):
                    result = subprocess.run([self.plugin / name, *args, *invalid], env=self.env,
                                            text=True, capture_output=True, check=False, timeout=4)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('See --help', result.stderr)
                    self.assertNotIn('private-placeholder', result.stderr)

    def test_native_coordinator_prompt_includes_only_selected_project_ids(self):
        entries = [{'id': name, 'working_directory': str(self.project)} for name in ('north-route', 'east-route')]
        (self.root / 'projects.json').write_text(json.dumps(entries))
        for harness in ('claude', 'codex'):
            provider = self.base / harness
            provider.write_text('#!' + sys.executable + '\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
            provider.chmod(0o755)
            for selected, other in (('north-route', 'east-route'), ('east-route', 'north-route')):
                with self.subTest(harness=harness, selected=selected):
                    outcome = self.launch('orchestrator', '--harness', harness, '--project', selected)
                    self.assertEqual(outcome.returncode, 0, outcome.stderr)
                    argv = json.loads(outcome.stdout)
                    self.assertIn(selected, argv[-1])
                    self.assertNotIn(other, argv[-1])
                    if harness == 'codex':
                        self.assertNotIn('--add-dir', argv)

    @contextmanager
    def isolated_tmux(self, prefix='tmux-', locale='en_US.UTF-8'):
        with tempfile.TemporaryDirectory(prefix=prefix, dir='/tmp') as sockets:
            env = {'PATH': self.env['PATH'], 'HOME': str(self.base), 'SHELL': '/bin/sh',
                   'TMUX_TMPDIR': sockets, 'TERM': 'xterm', 'LC_ALL': locale}
            with (patch.dict(os.environ, env, clear=True), patch.dict(sys.modules),
                  patch.object(sys, 'path', [str(self.plugin), *sys.path])):
                # The fixture's modules carry paths to a mailbox removed at test teardown.
                sys.modules.pop('team_control', None)
                sys.modules.pop('team_ui_data', None)
                def command(*args, check=True):
                    return subprocess.run(['tmux', *args], text=True, capture_output=True, check=check, timeout=5)
                try:
                    yield command
                finally:
                    for server in ('default', 'fixture-other'):
                        socket = Path(sockets) / ('tmux-' + str(os.getuid())) / server
                        command('-S', str(socket), 'kill-server', check=False)

    @unittest.skipUnless(shutil.which('tmux'), 'requires tmux')
    def test_team_status_left_shows_coordinator_harness_and_respects_custom_format(self):
        api = runpy.run_path(str(self.plugin / 'team'))
        settings = api['RoleSettings']
        for harness in ('copilot', 'claude', 'codex'):
            provider = self.base / harness
            provider.write_text('#!/bin/sh\nexec sleep 120\n')
            provider.chmod(0o755)
        cases = [('codex', 'copilot', None, '[Codex]'), ('claude', 'codex', None, '[Claude]'),
                 ('copilot', 'claude', None, '[Copilot]'), ('codex', 'claude', '[#S] ', '[Codex]'),
                 ('codex', 'claude', 'my #W', 'my orchestrator')]
        for leader, worker, custom, expected in cases:
            with self.subTest(leader=leader, custom=custom), self.isolated_tmux() as tmux:
                tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'unrelated', '-n', 'idle', 'sleep', '120')
                if custom:
                    tmux('set-option', '-g', 'status-left', custom)
                name = api['setup_team'](self.root, ['app'], settings(leader, None, None),
                                         settings(worker, None, None))
                rendered = tmux('display-message', '-p', '-t', '=' + name + ':orchestrator',
                                '#{T:status-left}').stdout.strip()
                self.assertEqual(rendered, expected)
                other = tmux('display-message', '-p', '-t', '=unrelated:', '#{T:status-left}').stdout.strip()
                self.assertEqual(other, 'my idle' if custom == 'my #W' else '[unrelated]')

    @unittest.skipUnless(shutil.which('tmux'), 'requires tmux')
    def test_fresh_roles_use_callers_provider_profile_with_an_older_tmux_server(self):
        real_tmux = shutil.which('tmux')
        with self.isolated_tmux() as tmux:
            # Observe only the synthetic environment installed by this fixture.
            proxy = self.base / 'tmux'
            proxy.write_text(
                '#!' + sys.executable + '\nimport os, sys, json\n'
                'with open(os.path.join(os.environ["HOME"], "tmux-args"), "a") as f:\n'
                '    f.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                f'os.execv({real_tmux!r}, [{real_tmux!r}, *sys.argv[1:]])\n')
            proxy.chmod(0o755)
            os.environ.update(CODEX_HOME='earlier-profile', CLAUDE_CONFIG_DIR='removed-profile')
            tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'preexisting', 'sleep', '120')
            os.environ.update(CODEX_HOME='chosen-profile', OPENAI_API_KEY='synthetic-account-secret')
            del os.environ['CLAUDE_CONFIG_DIR']
            for harness in ('codex', 'claude'):
                provider = self.base / harness
                provider.write_text(
                    '#!' + sys.executable + '\nimport json, os, pathlib, sys, time\n'
                    'record = pathlib.Path(os.environ["HOME"]) / os.environ["HARNESS_MAILBOX_ROUTE"]\n'
                    'record.write_text(json.dumps([os.getenv("CODEX_HOME"), os.getenv("CLAUDE_CONFIG_DIR"), sys.argv]))\n'
                    'time.sleep(120)\n')
                provider.chmod(0o755)
            api = runpy.run_path(str(self.plugin / 'team'))
            settings = api['RoleSettings']
            api['setup_team'](self.root, ['app'], settings('codex', None, None), settings('claude', None, None))
            for route in ('orchestrator', 'app'):
                record = self.base / route
                deadline = time.monotonic() + 4
                while not record.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                profile, removed, argv = json.loads(record.read_text())
                with self.subTest(route=route):
                    self.assertEqual(profile, 'chosen-profile')
                    self.assertIsNone(removed)
                    self.assertNotIn('synthetic-account-secret', json.dumps(argv))
            self.assertNotIn('synthetic-account-secret', (self.base / 'tmux-args').read_text())

    @contextmanager
    def terminal_client(self, argv):
        # A real controlling terminal lets tmux take or reject stdin exactly as the operator sees it.
        pid, master = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 120, 0, 0))
                os.execvpe(argv[0], argv, os.environ.copy())
            finally:
                os._exit(125)
        try:
            yield pid
        finally:
            os.close(master)
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == 0:
                    os.kill(pid, 15)
                    os.waitpid(pid, 0)
            except (ChildProcessError, ProcessLookupError):
                pass

    def client_session(self, tmux, socket_path, pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            clients = tmux('-S', socket_path, 'list-clients', '-F', '#{session_name}', check=False)
            if clients.returncode == 0 and clients.stdout.strip():
                return clients.stdout.strip()
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return None
            time.sleep(0.025)
        self.fail('The terminal client neither attached nor exited')

    @unittest.skipUnless(shutil.which('tmux'), 'requires tmux')
    def test_cli_rejects_a_new_server_reusing_the_observed_socket_session_and_name(self):
        with self.isolated_tmux() as tmux:
            (self.base / 'copilot').write_text('#!/bin/sh\nexec sleep 120\n')
            api = runpy.run_path(str(self.plugin / 'team'))
            selection = api['session_name'](self.root)

            def restart_server(program, argv):
                before = tmux('display-message', '-p', '-t', '=' + selection + ':',
                              '#{socket_path}|#{session_id}|#{pid}').stdout.strip()
                socket_path, original_id, original_pid = before.split('|')
                tmux('-S', socket_path, 'kill-server')
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if tmux('-S', socket_path, 'has-session', check=False).returncode:
                        break
                    time.sleep(0.02)
                tmux('-S', socket_path, '-f', '/dev/null', 'new-session', '-d', '-s', selection, 'sleep', '120')
                new_id, new_pid = tmux('-S', socket_path, 'display-message', '-p', '-t', '=' + selection + ':',
                                       '#{session_id}|#{pid}').stdout.strip().split('|')
                self.assertEqual(new_id, original_id)
                self.assertNotEqual(new_pid, original_pid)
                with self.terminal_client(argv) as pid:
                    self.assertIsNone(self.client_session(tmux, socket_path, pid))

            with (patch.object(sys.stdin, 'isatty', return_value=True),
                  patch.object(sys, 'argv', ['team', 'app']), patch('os.execvp', side_effect=restart_server) as attach):
                api['main']()
            attach.assert_called_once()

    @unittest.skipUnless(shutil.which('tmux'), 'requires tmux')
    def test_plain_terminal_can_reconnect_and_stop_team_created_on_another_socket(self):
        with self.isolated_tmux() as tmux:
            tmux('-L', 'fixture-other', '-f', '/dev/null', 'new-session', '-d', '-s', 'unrelated', 'sleep', '120')
            os.environ['TMUX'] = tmux('-L', 'fixture-other', 'display-message', '-p',
                                      '#{socket_path},#{pid},0').stdout.strip()
            (self.base / 'copilot').write_text('#!/bin/sh\nexec sleep 120\n')
            shutil.copy2(self.base / 'copilot', self.base / 'codex')
            api = runpy.run_path(str(self.plugin / 'team'))
            role = api['RoleSettings']
            name = api['setup_team'](self.root, ['app'], role('codex', None, None), role('copilot', 'auto', None))
            del os.environ['TMUX']
            data = runpy.run_path(str(self.plugin / 'team_ui_data.py'))
            current = data['managed_team'](self.root)
            self.assertIsNotNone(current)
            self.assertEqual(set(current['panes']), {'orchestrator', 'app'})
            socket_path = tmux('-L', 'fixture-other', 'display-message', '-p', '#{socket_path}').stdout.strip()
            target = '=' + name + ':orchestrator'
            def label():
                return tmux('-S', socket_path, 'display-message', '-p', '-t', target, '#{T:status-left}').stdout.strip()
            # Teams created before the label fix inherit the session-name format.
            tmux('-S', socket_path, 'set-option', '-u', '-t', target, 'status-left')
            self.assertEqual(label(), '[' + name + ']')
            def confirm_target(program, args):
                with self.terminal_client(args) as pid:
                    self.assertEqual(self.client_session(tmux, socket_path, pid), name)
                    tmux('-S', socket_path, 'detach-client', '-s', '=' + name)
            with patch('os.execvp', side_effect=confirm_target) as attach:
                data['reconnect'](self.root, current)
            attach.assert_called_once()
            self.assertEqual(label(), '[Codex]')
            tmux('-S', socket_path, 'set-option', '-t', target, 'status-left', 'custom #W')
            legacy = data['active_team'](self.root)
            with patch('os.execvp', side_effect=confirm_target) as attach:
                data['reconnect'](self.root, legacy)
            attach.assert_called_once()
            self.assertEqual(label(), 'custom orchestrator')
            unrelated = tmux('-S', socket_path, 'display-message', '-p', '-t', '=unrelated:',
                             '#{pid}|#{pane_id}').stdout.strip().split('|')
            with self.terminal_client(['tmux', '-S', socket_path, 'attach-session', '-t', '=unrelated']) as pid:
                self.assertEqual(self.client_session(tmux, socket_path, pid), 'unrelated')
                def switch_target(program, args):
                    command = subprocess.run(args, capture_output=True, check=True, timeout=5)
                    self.assertEqual(command.stdout.strip(), b'')
                with (patch.dict(os.environ, {'TMUX': socket_path + ',' + unrelated[0] + ',0',
                                              'TMUX_PANE': unrelated[1]}),
                      patch('os.execvp', side_effect=switch_target)):
                    data['reconnect'](self.root, current)
                self.assertEqual(self.client_session(tmux, socket_path, pid), name)
                tmux('-S', socket_path, 'detach-client', '-s', '=' + name)
            data['stop_managed'](self.root, current, 'app')
            current = data['managed_team'](self.root)
            self.assertEqual(set(current['panes']), {'orchestrator'})
            data['stop_managed'](self.root, current)
            self.assertIsNone(data['managed_team'](self.root))
            tmux('-L', 'fixture-other', 'has-session', '-t', '=unrelated')

    @unittest.skipUnless(shutil.which('tmux'), 'requires tmux')
    def test_launcher_attaches_to_verified_session_after_its_name_is_reused(self):
        with self.isolated_tmux() as tmux:
            tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'fixture', 'sleep', '120')
            (self.base / 'copilot').write_text('#!/bin/sh\nexec sleep 120\n')
            api = runpy.run_path(str(self.plugin / 'team'))
            role = api['RoleSettings']('copilot', 'auto', None)
            name = api['setup_team'](self.root, ['app'], role, role)

            def race(program, args):
                socket_path = tmux('display-message', '-p', '-t', '=' + name + ':', '#{socket_path}').stdout.strip()
                tmux('-S', socket_path, 'rename-session', '-t', '=' + name, 'original-team')
                tmux('-S', socket_path, 'new-session', '-d', '-s', name, 'sleep', '120')
                with self.terminal_client(args) as pid:
                    self.assertEqual(self.client_session(tmux, socket_path, pid), 'original-team')

            with (patch.object(sys.stdin, 'isatty', return_value=True),
                  patch.object(sys, 'argv', ['team', 'app']), patch('os.execvp', side_effect=race) as attach):
                api['main']()
            attach.assert_called_once()

    @unittest.skipUnless(shutil.which('tmux'), 'requires tmux')
    def test_team_starts_in_c_locale_with_spaces_in_socket_directory(self):
        with self.isolated_tmux(prefix='team socket ', locale='C') as tmux:
            tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'fixture', 'sleep', '120')
            (self.base / 'copilot').write_text('#!/bin/sh\nexec sleep 120\n')
            api = runpy.run_path(str(self.plugin / 'team'))
            role = api['RoleSettings']('copilot', 'auto', None)
            api['setup_team'](self.root, ['app'], role, role)
            state = api['control']['managed_team'](self.root)
            self.assertEqual(set(state['panes']), {'app', 'orchestrator'})
            self.assertIn('team socket ', api['control']['team_socket'](self.root))

    def test_unusable_socket_records_do_not_select_other_servers(self):
        api = runpy.run_path(str(self.plugin / 'team_control.py'))
        binding = self.root / api['SOCKET_RECORD']
        missing = self.base / 'missing-socket'
        binding.write_text(str(missing))
        self.assertIsNone(api['team_socket'](self.root))
        for label, contents in [('relative', 'socket'), ('empty', ''), ('oversize', 'x' * 4097),
                                ('invalid path', 'one\x00two'), ('regular file', str(self.root / 'projects.json'))]:
            with self.subTest(label=label):
                binding.write_text(contents)
                with patch('subprocess.run') as tmux:
                    self.assertIsNone(api['managed_team'](self.root))
                    tmux.assert_not_called()
        binding.unlink()
        binding.symlink_to(self.root / 'projects.json')
        with patch('subprocess.run') as tmux:
            self.assertIsNone(api['managed_team'](self.root))
            tmux.assert_not_called()

    @unittest.skipUnless(shutil.which('tmux'), 'requires tmux')
    def test_reconnect_selects_coordinator_pane_and_ignores_replaced_worker(self):
        # A private server and a sleeping stand-in keep the host's CLI sessions untouched.
        socket_root = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(socket_root.cleanup)
        self.env.update(TMUX_TMPDIR=socket_root.name, SHELL='/bin/sh')
        with patch.dict(os.environ, self.env):
            os.environ.pop('TMUX', None)
            os.environ.pop('TMUX_PANE', None)
            def tmux(*args, check=True):
                return subprocess.run(['tmux', *args], text=True, capture_output=True, check=check, timeout=5)
            (self.base / 'copilot').write_text('#!/bin/sh\nexec sleep 120\n')
            runner = runpy.run_path(str(self.plugin / 'team'))
            role = runner['RoleSettings']('copilot', 'auto', None)
            launch = runner['setup_team']
            try:
                name = launch(self.root, ['app'], role, role)
                roles = runner['pane_roles'](name)
                coordinator = next(p for p, role_name in roles.items() if role_name == 'orchestrator')
                worker = next(p for p, role_name in roles.items() if role_name == 'app')
                foreign = tmux('split-window', '-P', '-F', '#{pane_id}', '-t', coordinator, 'sleep', '120').stdout.strip()
                launch(self.root, ['app'], role, role)
                active = tmux('display-message', '-p', '-t', '=' + name + ':', '#{pane_id}').stdout.strip()
                with self.subTest("active pane"):
                    self.assertEqual(active, coordinator)
                tmux('respawn-pane', '-k', '-t', worker, 'sleep', '120')
                with self.subTest('replaced participant'), self.assertRaises(runner['StartupError']):
                    launch(self.root, ['app'], role, role)
                # The remaining panes belong to the user after both original CLI processes exit.
                tmux('kill-pane', '-t', coordinator)
                old_session = tmux('display-message', '-p', '-t', foreign, '#{session_id}').stdout.strip()
                launch(self.root, ['app'], role, role, new_team=True)
                for preserved in [foreign, worker]:
                    session = tmux('display-message', '-p', '-t', preserved, '#{session_id}').stdout.strip()
                    self.assertEqual(session, old_session)
                current = tmux('display-message', '-p', '-t', '=' + name + ':', '#{session_id}').stdout.strip()
                self.assertNotEqual(current, old_session)
            finally:
                tmux('kill-server', check=False)
