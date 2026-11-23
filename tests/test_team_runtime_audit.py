"""Regression cases at the participant CLI and tmux process boundaries."""
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
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
