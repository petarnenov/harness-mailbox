"""Regression checks for registry paths, native terminal arrival and team management."""
import contextlib
import curses
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_team_ui import TerminalScreen, snapshot


WORKSPACE = Path(__file__).resolve().parents[1] / 'workspace'


class TeamMenuAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(sys, 'path', sys.path.copy()):
            cls.ui = runpy.run_path(str(WORKSPACE / 'team'))
        cls.data = cls.ui['data']

    def test_malformed_nested_model_catalog_still_allows_manual_selection(self):
        depth = sys.getrecursionlimit() + 100
        response = subprocess.CompletedProcess([], 0, '[' * depth + ']' * depth, '')
        wizard = self.ui['Wizard'](['project'])
        wizard.current.harness = 'codex'
        with patch('subprocess.run', return_value=response):
            wizard.refresh_models(self.data.models)
        for key in (curses.KEY_END, 10, *map(ord, 'model-id'), 10):
            wizard.handle(key, self.data.models)
        self.assertEqual(wizard.roles['orchestrator'].model, 'model-id')
        self.assertEqual((wizard.role, wizard.stage), ('worker', 'harness'))

    def test_registry_failure_leaves_running_team_reachable_and_starts_dashboard_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'projects.json').write_text('{"unreadable')
            active = snapshot('project')
            screen = TerminalScreen([10], (26, 110))
            with (patch.object(self.data, 'managed_team', return_value=active),
                  patch.object(self.data, 'ensure_dashboard', return_value=True) as dashboard,
                  patch('curses.has_colors', return_value=False), patch('curses.curs_set')):
                self.assertEqual(self.ui['run_ui'](screen, root), ('reconnect', active, None))
            dashboard.assert_called_once_with(root)
            self.assertIn('Project registry', screen.frames[-1])

    def test_reconnect_focuses_the_coordinator_pane_in_a_split_window(self):
        active = snapshot('project')
        with (patch.object(self.data, 'managed_team', return_value=active),
              patch('subprocess.run') as tmux, patch('os.execvp') as attach):
            self.data.reconnect(WORKSPACE, active)
        calls = [call.args[0] for call in tmux.call_args_list]
        commands = [word for argv in calls for word in argv]
        select_pane = commands.index('select-pane')
        self.assertEqual(commands[select_pane + 1:select_pane + 3], ['-t', active['panes']['orchestrator']])
        attach.assert_called_once()

    def test_retired_session_with_foreign_pane_allows_removal_unless_coordinator_lock_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / 'projects.json'
            registry.write_text(json.dumps([{'id': 'sample', 'working_directory': str(root)}]))
            retired = {'projects': ('sample',), 'panes': {}, 'owned': {}, 'foreign': True}

            def discovery(_root, *, include_retired=False):
                return retired if include_retired else None

            exists = subprocess.CompletedProcess([], 0, b'', b'')
            with (patch.object(self.data, 'managed_team', side_effect=discovery),
                  patch('subprocess.run', return_value=exists),
                  patch.object(self.data, 'coordinator_locked', side_effect=[True, False])):
                confirmation = self.data.removal_snapshot(root, 'sample')
                with self.assertRaises(ValueError):
                    self.data.remove_project(root, 'sample', confirmation)
                self.assertEqual(self.data.projects(root), ['sample'])
                self.data.remove_project(root, 'sample', confirmation)
                self.assertEqual(self.data.projects(root), [])

    def test_missing_wsl_account_has_a_fixed_error(self):
        with (patch.object(sys, 'argv', ['team', '--from-windows']),
              patch.object(sys.stdin, 'isatty', return_value=True),
              patch.object(sys.stdout, 'isatty', return_value=True),
              patch('pwd.getpwuid', side_effect=KeyError('private-account-data')),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(self.ui['main'](), 1)
        self.assertIn('WSL', errors.getvalue())
        self.assertNotIn('private-account-data', errors.getvalue())

    @unittest.skipUnless(shutil.which('git'), 'git is required for working tree detection')
    def test_repository_with_trailing_newline_preserves_checkout_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = root / 'checkout\n'
            component = repository / 'component'
            component.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(repository)], check=True)
            (root / 'projects.json').write_text('[]')
            self.data.add_project(root, component, 'component')
            project, = json.loads((root / 'projects.json').read_bytes())
            self.assertEqual(project['repository_path'], str(repository))

    def test_registry_update_retains_escaped_unix_filename_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            folder = root / 'component'
            folder.mkdir()
            existing = {'id': 'legacy', 'name': os.fsdecode(b'name-\xff'),
                        'working_directory': str(root / 'legacy')}
            registry = root / 'projects.json'
            registry.write_text(json.dumps([existing]))
            self.assertEqual(self.data.add_project(root, folder, 'component'), 'component')
            self.assertEqual(json.loads(registry.read_bytes())[0], existing)
