"""Regression checks for registry paths, native terminal arrival and team management."""
import contextlib
import curses
import hashlib
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
        tmux.assert_not_called()
        attach.assert_called_once()
        command = attach.call_args.args[1]
        self.assertIn('if-shell', command)
        self.assertIn('select-pane -t ' + active['panes']['orchestrator'], command[-2])

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

    def test_removal_keeps_each_route_bound_to_its_original_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            original = root / 'original'
            other = root / 'other'
            original.mkdir()
            other.mkdir()
            entries = [{'id': name, 'working_directory': str(original)} for name in ('sample', 'alias')]
            (root / 'projects.json').write_text(json.dumps({'projects': entries}))
            history = root / 'tasks/sample/task.json'
            history.parent.mkdir(parents=True)
            history.write_text('{"existing": true}')
            with (patch.object(self.data, 'managed_team', return_value=None),
                  patch.object(self.data, '_unidentified_team', return_value=False)):
                self.data.remove_project(root, 'sample', self.data.removal_snapshot(root, 'sample'))
            self.assertEqual(self.data.projects(root), [])
            for route in ('sample', 'ALIAS'):
                with self.subTest(route=route), self.assertRaises(ValueError):
                    self.data.add_project(root, other, route)
            self.assertEqual(self.data.add_project(root, other, 'independent'), 'independent')
            self.assertEqual(self.data.add_project(root, original, 'sample'), 'sample')
            self.assertEqual(history.read_text(), '{"existing": true}')

    def test_preexisting_orphan_mailbox_data_reserves_its_route(self):
        for tree in ('inbox', 'tasks'):
            with self.subTest(tree=tree), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                route = root / tree / 'retired'
                route.mkdir(parents=True)
                (route / 'history.json').write_text('{}')
                project = root / 'new'
                project.mkdir()
                (root / 'projects.json').write_text('{}')
                with self.assertRaises(ValueError):
                    self.data.add_project(root, project, 'retired')
                self.assertEqual(self.data.projects(root), [])
                self.assertEqual(self.data.add_project(root, project, 'fresh'), 'fresh')

    def test_retired_route_follows_the_original_directory_when_replaced_at_old_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / 'project'
            project.mkdir()
            (root / 'projects.json').write_text('[]')
            self.data.add_project(root, project, 'sample')
            with (patch.object(self.data, 'managed_team', return_value=None),
                  patch.object(self.data, '_unidentified_team', return_value=False)):
                self.data.remove_project(root, 'sample', self.data.removal_snapshot(root, 'sample'))
            moved = root / 'moved'
            project.rename(moved)
            project.mkdir()
            with self.assertRaises(ValueError):
                self.data.add_project(root, project, 'sample')
            self.assertEqual(self.data.add_project(root, moved, 'sample'), 'sample')
            self.assertEqual(self.data.removal_snapshot(root, 'sample')['working_directory'], str(moved))

    def _readd_after_removal(self, root, project):
        with (patch.object(self.data, 'managed_team', return_value=None),
              patch.object(self.data, '_unidentified_team', return_value=False)):
            self.data.add_project(root, project, 'sample')
            self.data.remove_project(root, 'sample', self.data.removal_snapshot(root, 'sample'))
            self.assertEqual(self.data.add_project(root, project, 'sample'), 'sample')

    def test_removing_a_readded_project_succeeds_after_its_folder_was_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / 'project'
            project.mkdir()
            (root / 'projects.json').write_text('[]')
            self._readd_after_removal(root, project)
            shutil.rmtree(project)
            with (patch.object(self.data, 'managed_team', return_value=None),
                  patch.object(self.data, '_unidentified_team', return_value=False)):
                self.data.remove_project(root, 'sample', self.data.removal_snapshot(root, 'sample'))
            self.assertEqual(self.data.projects(root), [])

    def test_removing_a_readded_project_succeeds_after_its_folder_was_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / 'project'
            project.mkdir()
            (root / 'projects.json').write_text('[]')
            self._readd_after_removal(root, project)
            project.rename(root / 'moved')
            project.mkdir()
            with (patch.object(self.data, 'managed_team', return_value=None),
                  patch.object(self.data, '_unidentified_team', return_value=False)):
                self.data.remove_project(root, 'sample', self.data.removal_snapshot(root, 'sample'))
            self.assertEqual(self.data.projects(root), [])

    @staticmethod
    def _marker(root, name):
        return root / 'archive' / ('.team-route-' + hashlib.sha256(name.casefold().encode()).hexdigest())

    def _remove_sample(self, root):
        with (patch.object(self.data, 'managed_team', return_value=None),
              patch.object(self.data, '_unidentified_team', return_value=False)):
            self.data.remove_project(root, 'sample', self.data.removal_snapshot(root, 'sample'))

    def _legacy_two_element_marker(self, root, project):
        self.data.add_project(root, project, 'sample')
        self._remove_sample(root)
        marker = self._marker(root, 'sample')
        marker.write_text(json.dumps(json.loads(marker.read_text())[:2]))
        self.assertEqual(self.data.add_project(root, project, 'sample'), 'sample')

    def test_removal_with_a_two_element_marker_succeeds_after_the_folder_vanished_or_was_replaced(self):
        for change in ('deleted', 'replaced'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                project = root / 'project'
                project.mkdir()
                (root / 'projects.json').write_text('[]')
                self._legacy_two_element_marker(root, project)
                if change == 'deleted':
                    shutil.rmtree(project)
                else:
                    project.rename(root / 'moved')
                    project.mkdir()
                self._remove_sample(root)
                self.assertEqual(self.data.projects(root), [])

    def test_route_bound_to_another_folder_stays_bound_after_removing_a_live_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = root / 'first'
            second = root / 'second'
            first.mkdir()
            second.mkdir()
            (root / 'projects.json').write_text('[]')
            with (patch.object(self.data, 'managed_team', return_value=None),
                  patch.object(self.data, '_unidentified_team', return_value=False)):
                self.data.add_project(root, first, 'sample')
                self.data.remove_project(root, 'sample', self.data.removal_snapshot(root, 'sample'))
                marker = self._marker(root, 'sample')
                bound = marker.read_bytes()
                (root / 'projects.json').write_text(json.dumps([{'id': 'sample', 'working_directory': str(second)}]))
                self.data.remove_project(root, 'sample', self.data.removal_snapshot(root, 'sample'))
                self.assertEqual(marker.read_bytes(), bound)
                self.assertEqual(self.data.projects(root), [])
                with self.assertRaises(ValueError):
                    self.data.add_project(root, second, 'sample')
                self.assertEqual(self.data.add_project(root, first, 'sample'), 'sample')

    def test_a_marker_written_by_removal_has_exactly_the_name_and_the_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / 'project'
            project.mkdir()
            (root / 'projects.json').write_text('[]')
            self.data.add_project(root, project, 'sample')
            self._remove_sample(root)
            self.assertEqual(json.loads(self._marker(root, 'sample').read_text()),
                             ['sample', self.data._directory_identity(project)])

    def test_long_non_ascii_working_directory_can_be_removed_readded_and_removed_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root
            for _ in range(6):
                project = project / ('п' * 115)
            project.mkdir(parents=True)
            (root / 'projects.json').write_text('[]')
            self.data.add_project(root, project, 'sample')
            self._remove_sample(root)
            self.assertEqual(self.data.add_project(root, project, 'sample'), 'sample')
            self._remove_sample(root)
            self.assertEqual(self.data.projects(root), [])

    def test_protect_a_marker_that_is_not_a_regular_file_still_refuses_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / 'project'
            project.mkdir()
            (root / 'projects.json').write_text(json.dumps([{'id': 'sample', 'working_directory': str(project)}]))
            self._marker(root, 'sample').mkdir(parents=True)
            with self.assertRaises(ValueError):
                self._remove_sample(root)
            self.assertEqual(self.data.projects(root), ['sample'])

    def test_failed_retirement_publication_preserves_registry_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            registry = root / 'projects.json'
            original = json.dumps([{'id': 'kept', 'working_directory': str(root)}])
            registry.write_text(original)
            confirmation = self.data.removal_snapshot(root, 'kept')
            with (patch.object(self.data, 'managed_team', return_value=None),
                  patch.object(self.data, '_unidentified_team', return_value=False)):
                with patch.object(self.data, '_publish_registry', side_effect=OSError('synthetic exception')):
                    with self.assertRaises(ValueError) as error:
                        self.data.remove_project(root, 'kept', confirmation)
                    self.assertNotIn('synthetic exception', str(error.exception))
                self.assertEqual(registry.read_text(), original)
                self.data.remove_project(root, 'kept', confirmation)
                self.assertEqual(self.data.projects(root), [])
            self.assertEqual(self.data.add_project(root, root, 'kept'), 'kept')

    def test_legacy_session_name_uses_record_to_distinguish_project_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / 'new'
            project.mkdir()
            sessions = root / 'sessions'
            sessions.mkdir()
            registry = root / 'projects.json'
            registry.write_text('[]')
            record = sessions / 'alpha--api--worker.json'
            for old in ('alpha', 'alpha--api'):
                with self.subTest(old=old):
                    record.write_text(json.dumps({'project': old}))
                    with self.assertRaises(ValueError):
                        self.data.add_project(root, project, old)
            self.assertEqual(self.data.add_project(root, project, 'alpha'), 'alpha')

    def test_legacy_epic_only_project_history_reserves_its_route(self):
        cases = [('issues.json', {'issues': [{'key': 'FIX-1', 'affected_projects': ['archived']}]}),
                 ('progress.json', {'per_project': {'other': {'project_metrics': [{'project': 'archived'}]}}})]
        for name, record in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                project = root / 'new'
                project.mkdir()
                epic = root / 'epics/FIX-1'
                epic.mkdir(parents=True)
                (epic / name).write_text(json.dumps(record))
                (root / 'projects.json').write_text('[]')
                with self.assertRaises(ValueError):
                    self.data.add_project(root, project, 'ARCHIVED')
                self.assertEqual(self.data.add_project(root, project, 'fresh'), 'fresh')

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
