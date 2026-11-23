"""Local project registration from the team picker, including parallel writers."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch


PLUGIN = Path(__file__).resolve().parents[1] / 'workspace' / 'copilot-mailbox'


class TeamProjectRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(sys, 'path', [str(PLUGIN), *sys.path]):
            import team_ui_data
            cls.data = team_ui_data

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.root = self.base / 'mailbox'
        self.root.mkdir()
        self.registry = self.root / 'projects.json'
        self.folder = self.base / 'new project'
        self.folder.mkdir()
        self.existing = self.base / 'old project'
        self.existing.mkdir()
        self.entry = dict(id='old', repository_path=str(self.existing),
                          working_directory=str(self.existing), custom={'retain': True})

    def test_registration_preserves_all_four_registry_shapes_and_metadata(self):
        for wrapped in (False, True):
            for mapping in (False, True):
                with self.subTest(wrapped=wrapped, mapping=mapping):
                    entries = {'old': self.entry} if mapping else [self.entry]
                    before = dict(projects=entries, description='preserve') if wrapped else entries
                    self.registry.write_text(json.dumps(before))
                    self.assertEqual(self.data.add_project(self.root, self.folder, 'new'), 'new')
                    after = json.loads(self.registry.read_text())
                    if wrapped:
                        self.assertEqual(after['description'], 'preserve')
                        after = after['projects']
                    self.assertIs(type(after), type(entries))
                    if mapping:
                        self.assertEqual(after['old'], self.entry)
                        added = after['new']
                    else:
                        self.assertEqual(after[0], self.entry)
                        added = after[1]
                        self.assertEqual(added['id'], 'new')
                    self.assertEqual(added['working_directory'], str(self.folder))
                    self.assertEqual(added['repository_path'], str(self.folder))
                    self.assertEqual(self.data.projects(self.root), ['new', 'old'])
                    self.assertFalse((self.root / 'sessions').exists())
                    self.assertFalse((self.root / 'tasks').exists())

    def test_projects_id_cannot_replace_an_unwrapped_registry_with_a_wrapper(self):
        self.registry.write_text(json.dumps({'old': self.entry}))
        before = self.registry.read_bytes()
        with self.assertRaises(ValueError):
            self.data.add_project(self.root, self.folder, 'projects')
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.data.projects(self.root), ['old'])
        self.registry.write_text(json.dumps({'projects': {'old': self.entry}}))
        self.assertEqual(self.data.add_project(self.root, self.folder, 'projects'), 'projects')
        self.assertEqual(self.data.projects(self.root), ['old', 'projects'])

    def test_new_routes_reject_case_aliases_for_projects_and_the_coordinator(self):
        self.registry.write_text(json.dumps({'projects': [self.entry]}))
        before = self.registry.read_bytes()
        for candidate in ('Old', 'olD', 'ORCHESTRATOR', 'Orchestrator'):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                self.data.add_project(self.root, self.folder, candidate)
            self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.data.add_project(self.root, self.existing, 'OLD'), 'old')
        self.assertEqual(self.registry.read_bytes(), before)

    def test_picker_lists_only_immediate_folders_and_suggests_valid_names(self):
        visible = self.folder / 'Demo app'
        hidden = self.folder / '.hidden'
        visible.mkdir()
        hidden.mkdir()
        (visible / 'nested').mkdir()
        (self.folder / 'ignore-file').touch()
        self.assertEqual(self.data.folders(self.folder), [visible, hidden])
        self.assertEqual(self.data.suggested_project_id(visible), 'Demo-app')
        self.assertNotEqual(self.data.suggested_project_id(self.folder / 'orchestrator'), 'orchestrator')
        with self.assertRaises(ValueError):
            self.data.folders(self.folder / 'ignore-file')

    def test_failed_atomic_publication_preserves_registry_mode_contents_and_cleans_temp(self):
        self.registry.write_text('[]')
        self.registry.chmod(0o640)
        with patch('os.replace', side_effect=OSError('sensitive detail')), self.assertRaises(ValueError) as ex:
            self.data.add_project(self.root, self.folder, 'fresh')
        self.assertEqual(self.registry.read_text(), '[]')
        self.assertEqual(list(self.root.glob('.team-registry-*')), [])
        self.assertNotIn('sensitive detail', str(ex.exception))
        self.data.add_project(self.root, self.folder, 'fresh')
        self.assertEqual(self.registry.stat().st_mode & 0o777, 0o640)

    def test_adding_checkout_root_reuses_its_only_registered_scoped_route(self):
        subprocess.run(['git', 'init', '-q', str(self.folder)], check=True, capture_output=True)
        app = self.folder / 'app'
        app.mkdir()
        configured = dict(id='configured', name='Configured', repository_path=str(self.folder),
                          working_directory=str(app), expected_branch='main')
        self.registry.write_text(json.dumps({'projects': [configured]}))
        before = self.registry.read_bytes()
        self.assertEqual(self.data.add_project(self.root, self.folder, 'new-id'), 'configured')
        self.assertEqual(self.data.projects(self.root), ['configured'])
        self.assertEqual(self.registry.read_bytes(), before)

    def test_existing_root_alias_is_collapsed_without_modifying_records(self):
        app = self.folder / 'app'
        app.mkdir()
        configured = dict(id='configured', name='Configured', repository_path=str(self.folder),
                          working_directory=str(app), expected_branch='main')
        alias = dict(id='extra', name='repo', repository_path=str(self.folder),
                     working_directory=str(self.folder))
        self.registry.write_text(json.dumps({'projects': [alias, configured]}))
        before = self.registry.read_bytes()
        self.assertEqual(self.data.projects(self.root), ['configured'])
        self.assertEqual(self.registry.read_bytes(), before)

    def test_existing_canonical_aliases_and_sibling_projects_have_separate_identities(self):
        subprocess.run(['git', 'init', '-q', str(self.folder)], check=True, capture_output=True)
        records = []
        for name in ('api', 'web'):
            directory = self.folder / name
            directory.mkdir()
            records.append(dict(id=name, working_directory=str(directory), repository_path=str(self.folder)))
        link = self.base / 'api-link'
        link.symlink_to(self.folder / 'api', target_is_directory=True)
        records.append(dict(id='api-copy', name='api-link', working_directory=str(link),
                            repository_path=str(self.folder)))
        self.registry.write_text(json.dumps({'projects': records + [self.entry]}))
        before = self.registry.read_bytes()
        visible = self.data.projects(self.root)
        self.assertIn('old', visible)
        self.assertIn('web', visible)
        api_id, = set(visible).difference({'old', 'web'})
        self.assertIn(api_id, ('api', 'api-copy'))
        self.assertEqual(self.data.add_project(self.root, link, 'extra'), api_id)
        self.assertEqual(self.data.add_project(self.root, self.folder / 'web', 'extra'), 'web')
        with self.assertRaises(ValueError):
            self.data.add_project(self.root, self.folder, 'ambiguous')
        self.assertEqual(self.registry.read_bytes(), before)
        expected = self.data.removal_snapshot(self.root, api_id)
        self.assertEqual(set(expected['ids']), {'api', 'api-copy'})
        self.data.remove_project(self.root, api_id, expected)
        self.assertEqual(self.data.projects(self.root), ['old', 'web'])
        self.assertTrue(link.is_symlink())

    def test_add_scoped_directory_alongside_independently_configured_checkout(self):
        subprocess.run(['git', 'init', '-q', str(self.folder)], check=True, capture_output=True)
        api = self.folder / 'api'
        api.mkdir()
        checkout = dict(id='checkout', working_directory=str(self.folder), repository_path=str(self.folder),
                        required_loop_skill='root.loop')
        self.registry.write_text(json.dumps([checkout]))
        self.assertEqual(self.data.add_project(self.root, api, 'api'), 'api')
        self.assertEqual(self.data.projects(self.root), ['api', 'checkout'])
        root_record, api_record = json.loads(self.registry.read_bytes())
        self.assertEqual(root_record, checkout)
        self.assertEqual(api_record['working_directory'], str(api))
        self.assertEqual(api_record['repository_path'], str(self.folder))

    def test_distinct_configured_root_and_scoped_route_remain_independent(self):
        app = self.folder / 'app'
        app.mkdir()
        root_route = dict(id='checkout', repository_path=str(self.folder),
                          working_directory=str(self.folder), required_loop_skill='root.loop')
        app_route = dict(id='app', repository_path=str(self.folder),
                         working_directory=str(app), required_loop_skill='app.loop')
        self.registry.write_text(json.dumps([root_route, app_route]))
        self.assertEqual(self.data.projects(self.root), ['app', 'checkout'])
        self.assertEqual(self.data.add_project(self.root, self.folder, 'extra'), 'checkout')

    def test_removal_of_alias_group_preserves_shapes_unrelated_metadata_and_history(self):
        app = self.folder / 'app'
        app.mkdir()
        source = app / 'source.txt'
        source.write_text('source bytes')
        note = self.root / 'tasks' / 'configured' / 'note.json'
        note.parent.mkdir(parents=True)
        note.write_bytes(b'{"historic":true}')
        configured = dict(id='configured', repository_path=str(self.folder),
                          working_directory=str(app), expected_branch='main')
        alias = dict(id='extra', name='repo', repository_path=str(self.folder),
                     working_directory=str(self.folder))
        for mapping in (False, True):
            for wrapped in (False, True):
                with self.subTest(mapping=mapping, wrapped=wrapped):
                    entries = ([alias, self.entry, configured] if not mapping else
                               {'alias-key': alias, 'unrelated-key': self.entry, 'primary-key': configured})
                    initial = {'projects': entries, 'schema_version': 99} if wrapped else entries
                    self.registry.write_text(json.dumps(initial))
                    self.registry.chmod(0o640)
                    expected = self.data.removal_snapshot(self.root, 'configured')
                    self.assertEqual(set(expected['ids']), {'configured', 'extra'})
                    self.assertEqual(Path(expected['working_directory']), app)
                    self.data.remove_project(self.root, 'configured', expected)
                    surviving = {'unrelated-key': self.entry} if mapping else [self.entry]
                    self.assertEqual(json.loads(self.registry.read_bytes()),
                                     {'projects': surviving, 'schema_version': 99} if wrapped else surviving)
                    self.assertEqual(self.registry.stat().st_mode & 0o777, 0o640)
                    self.assertEqual(source.read_text(), 'source bytes')
                    self.assertEqual(note.read_bytes(), b'{"historic":true}')
                    before = self.registry.read_bytes()
                    with self.assertRaises(ValueError):
                        self.data.remove_project(self.root, 'configured', expected)
                    self.assertEqual(self.registry.read_bytes(), before)

    def test_stale_confirmation_or_active_alias_cannot_unregister_project(self):
        scoped = self.folder / 'app'
        scoped.mkdir()
        configured = dict(id='configured', working_directory=str(scoped),
                          repository_path=str(self.folder), expected_branch='main')
        root_alias = dict(id='alias', name='root', working_directory=str(self.folder),
                          repository_path=str(self.folder))
        self.registry.write_text(json.dumps([configured, root_alias]))
        expected = self.data.removal_snapshot(self.root, 'configured')
        before = self.registry.read_bytes()
        with patch.object(self.data, 'managed_team', return_value={'projects': ('alias',), 'panes': {'alias': '%2'}}):
            with self.assertRaises(ValueError):
                self.data.remove_project(self.root, 'configured', expected)
        self.assertEqual(self.registry.read_bytes(), before)
        configured['working_directory'] = str(self.existing)
        configured['repository_path'] = str(self.existing)
        self.registry.write_text(json.dumps([configured, root_alias]))
        changed = self.registry.read_bytes()
        with self.assertRaises(ValueError):
            self.data.remove_project(self.root, 'configured', expected)
        self.assertEqual(self.registry.read_bytes(), changed)

    def test_active_second_worker_prevents_deregistration_after_first_worker_exits(self):
        self.registry.write_text(json.dumps({'projects': [self.entry]}))
        expected = self.data.removal_snapshot(self.root, 'old')
        before = self.registry.read_bytes()
        state = {'projects': ('old', 'old+2'), 'panes': {'orchestrator': '%0', 'old+2': '%2'}}
        with patch.object(self.data, 'managed_team', return_value=state):
            with self.assertRaises(ValueError):
                self.data.remove_project(self.root, 'old', expected)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_removal_refuses_when_tmux_cannot_query_a_locked_team(self):
        self.registry.write_text(json.dumps({'projects': [self.entry]}))
        expected = self.data.removal_snapshot(self.root, 'old')
        before = self.registry.read_bytes()
        with (self.root / '.copilot-mailbox-orchestrator.lock').open('w') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            with patch('subprocess.run', side_effect=FileNotFoundError('tmux')), self.assertRaises(ValueError):
                self.data.remove_project(self.root, 'old', expected)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_concurrent_unrelated_add_and_remove_preserve_both_updates(self):
        self.registry.write_text(json.dumps({'projects': [self.entry]}))
        expected = self.data.removal_snapshot(self.root, 'old')
        barrier = Barrier(2, timeout=5)
        def add():
            barrier.wait()
            self.data.add_project(self.root, self.folder, 'new')
        def remove():
            barrier.wait()
            self.data.remove_project(self.root, 'old', expected)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(add), pool.submit(remove)]
            for job in jobs:
                job.result(timeout=8)
        self.assertEqual(self.data.projects(self.root), ['new'])

    def test_publication_failure_keeps_confirmed_registration(self):
        self.registry.write_text(json.dumps({'projects': [self.entry]}))
        expected = self.data.removal_snapshot(self.root, 'old')
        before = self.registry.read_bytes()
        with patch('os.replace', side_effect=OSError('sensitive detail')), self.assertRaises(ValueError) as error:
            self.data.remove_project(self.root, 'old', expected)
        self.assertNotIn('sensitive detail', str(error.exception))
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(list(self.root.glob('.team-registry-*')), [])

    def test_nested_git_working_directory_uses_checkout_root_for_repository_path(self):
        subprocess.run(['git', 'init', '-q', str(self.folder)], check=True, capture_output=True)
        child = self.folder / 'component'
        child.mkdir()
        self.registry.write_text('[]')
        self.assertEqual(self.data.add_project(self.root, child, 'component'), 'component')
        row, = json.loads(self.registry.read_text())
        self.assertEqual(row['working_directory'], str(child))
        self.assertEqual(row['repository_path'], str(self.folder))

    def test_existing_folder_resolves_to_its_id_and_a_used_id_never_changes_the_registry(self):
        self.registry.write_text(json.dumps({'projects': [self.entry]}))
        alias = self.base / 'alias'
        alias.symlink_to(self.existing, target_is_directory=True)
        original = self.registry.read_bytes()
        self.assertEqual(self.data.add_project(self.root, alias, 'different'), 'old')
        for folder, name in [(self.folder, 'old'), (self.folder, '../unsafe'),
                             (self.folder, 'orchestrator'), (self.base / 'absent', 'new')]:
            with self.subTest(folder=folder, name=name), self.assertRaises(ValueError):
                self.data.add_project(self.root, folder, name)
        self.assertEqual(self.registry.read_bytes(), original)

    def test_malformed_or_symlinked_registry_is_preserved(self):
        self.registry.write_text('{unreadable')
        before = self.registry.read_bytes()
        with self.assertRaises(ValueError):
            self.data.add_project(self.root, self.folder, 'new')
        self.assertEqual(self.registry.read_bytes(), before)
        outside = self.base / 'outside'
        outside.write_text('[]')
        self.registry.unlink()
        self.registry.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.data.add_project(self.root, self.folder, 'new')
        self.assertTrue(self.registry.is_symlink())
        self.assertEqual(outside.read_text(), '[]')

    def test_independent_processes_adding_to_the_same_registry_preserve_all_entries(self):
        self.registry.write_text(json.dumps({'projects': [self.entry]}))
        code = ('from pathlib import Path; from team_ui_data import add_project; '
                'import sys; sys.stdin.read(1); '
                'add_project(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])')
        children = []
        names = ['p' + str(i) for i in range(4)]
        for name in names:
            folder = self.base / name
            folder.mkdir()
            child = subprocess.Popen(
                [sys.executable, '-B', '-c', code, str(self.root), str(folder), name],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, 'PYTHONPATH': str(PLUGIN)}, text=True)
            children.append(child)
            self.addCleanup(child.kill)
        for child in children:
            child.stdin.write('x')
            child.stdin.flush()
        for child in children:
            _, err = child.communicate(timeout=8)
            self.assertEqual(child.returncode, 0, err)
        self.assertEqual(self.data.projects(self.root), ['old', *names])


class ManagedTeamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(sys, 'path', [str(PLUGIN), *sys.path]):
            import team_control
            cls.control = team_control

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.token = '0123456789abcdef' * 2
        self.rows = [self.row('%20', 'orchestrator', '900'), self.row('%21', 'alpha', '901'),
                     self.row('%22', 'beta', '902')]
        self.mutations = []
        self.has_survivors = False
        self.result = ''
        self.tmux = patch('subprocess.run', side_effect=self.respond)
        self.tmux.start()
        self.addCleanup(self.tmux.stop)

    def row(self, pane, role, pid, dead='0', marker=None, pinned=None):
        return '|'.join(('$3', pane, dead, role, 'alpha,beta', self.token,
                         self.token if marker is None else marker, '700', '500', pid, pid if pinned is None else pinned))

    def respond(self, argv, **kwargs):
        if argv[1] == 'list-panes':
            return subprocess.CompletedProcess(argv, 0, '\n'.join(self.rows), '')
        if argv[1] == 'has-session':
            return subprocess.CompletedProcess(argv, 0 if self.has_survivors else 1, '', '')
        self.mutations.append(argv)
        return subprocess.CompletedProcess(argv, 0, self.result, '')

    def test_individual_and_entire_team_stop_only_the_matching_owned_panes(self):
        snapshot = self.control.managed_team(self.root)
        self.assertEqual(snapshot['panes'], {'orchestrator': '%20', 'alpha': '%21', 'beta': '%22'})
        self.control.stop_managed(self.root, snapshot, 'beta')
        self.control.stop_managed(self.root, snapshot)
        one, whole = self.mutations
        self.assertEqual([arg for arg in one if arg.startswith('kill-')], ['kill-pane -t %22'])
        self.assertEqual([arg for arg in whole if arg.startswith('kill-')],
                         ['kill-pane -t %21', 'kill-pane -t %22', 'kill-pane -t %20'])
        self.assertIn(snapshot['identities']['%22'], one[one.index('%22') + 1])
        self.assertNotIn('kill-session', whole)

    def test_stopping_only_second_worker_keeps_first_worker_and_coordinator(self):
        self.rows.append(self.row('%24', 'alpha+2', '904'))
        self.rows = [row.replace('|alpha,beta|', '|alpha,alpha+2,beta|') for row in self.rows]
        state = self.control.managed_team(self.root)
        self.assertIsNotNone(state)
        self.assertEqual(state['panes'], {'orchestrator': '%20', 'alpha': '%21', 'alpha+2': '%24', 'beta': '%22'})
        self.control.stop_managed(self.root, state, 'alpha+2')
        calls, = self.mutations
        self.assertEqual([arg for arg in calls if arg.startswith('kill-')], ['kill-pane -t %24'])

    def test_ambiguous_or_nonconsecutive_worker_numbers_do_not_establish_ownership(self):
        originals = self.rows
        for roster in ('alpha,alpha+1', 'alpha,alpha+3', 'alpha,alpha+02', 'alpha+2',
                       'alpha,alpha+2,alpha+2', 'alpha,Alpha+2', 'alpha,orchestrator+2'):
            with self.subTest(roster=roster):
                self.rows = [row.replace('|alpha,beta|', '|' + roster + '|') for row in originals]
                self.assertIsNone(self.control.managed_team(self.root))
        self.assertEqual(self.mutations, [])

    def test_stale_snapshot_cannot_stop_replaced_worker_or_team(self):
        snapshot = self.control.managed_team(self.root)
        self.rows[1] = self.row('%21', 'alpha', '1100')
        for role in ('alpha', None):
            with self.subTest(role=role), self.assertRaises(ValueError):
                self.control.stop_managed(self.root, snapshot, role)
        self.assertEqual(self.mutations, [])

    def test_worker_respawn_before_first_observation_is_treated_as_foreign(self):
        self.has_survivors = True
        self.rows[1] = self.row('%21', 'alpha', '1801', pinned='901')
        state = self.control.managed_team(self.root)
        self.assertTrue(state['foreign'])
        self.assertNotIn('alpha', state['panes'])
        self.assertNotIn('alpha', state['owned'])
        with self.assertRaises(ValueError):
            self.control.stop_managed(self.root, state, 'alpha')
        self.assertEqual(self.mutations, [])
        self.control.stop_managed(self.root, state)
        commands = [arg for call in self.mutations for arg in call]
        self.assertIn('kill-pane -t %22', commands)
        self.assertIn('kill-pane -t %20', commands)
        self.assertNotIn('kill-pane -t %21', commands)
        self.assertNotIn('kill-session', commands)
        self.assertTrue(any('rename-session' in arg and 'mailbox-released-' in arg for arg in commands))

    def test_partial_team_remains_manageable_with_a_stopped_worker_and_foreign_pane(self):
        self.has_survivors = True
        self.rows[1] = self.row('%21', 'alpha', '901', dead='1')
        self.rows.append(self.row('%30', 'unrelated', '910', marker=''))
        state = self.control.managed_team(self.root)
        self.assertTrue(state['foreign'])
        self.assertEqual(state['projects'], ('alpha', 'beta'))
        self.assertEqual(state['panes'], {'orchestrator': '%20', 'beta': '%22'})
        for role in ('alpha', 'orchestrator', 'unrelated'):
            with self.subTest(role=role), self.assertRaises(ValueError):
                self.control.stop_managed(self.root, state, role)
        self.assertEqual(self.mutations, [])
        self.control.stop_managed(self.root, state, 'beta')
        self.assertIn('kill-pane -t %22', self.mutations[0])
        self.control.stop_managed(self.root, state)
        commands = [arg for call in self.mutations[1:] for arg in call]
        self.assertIn('kill-pane -t %20', commands)
        self.assertNotIn('kill-pane -t %30', commands)
        self.assertNotIn('kill-session', commands)
        self.assertTrue(any('rename-session' in arg and 'mailbox-released-' in arg for arg in commands))

    def test_untrusted_or_ambiguous_rows_and_changed_tmux_selection_fail_closed(self):
        original = self.rows[:]
        for rows in ([], [original[0], original[0]], [original[0].replace('$3', 'bad')],
                     [original[0], original[1].replace('$3', '$4')],
                     [original[0].replace('alpha,beta', 'orchestrator,beta')],
                     [self.row('%2', 'other', '1')]):
            with self.subTest(rows=rows):
                self.rows = rows
                self.assertIsNone(self.control.managed_team(self.root))
        self.rows = original
        self.result = 'changed'
        with self.assertRaises(ValueError):
            self.control.stop_managed(self.root, self.control.managed_team(self.root), 'alpha')

    def test_early_token_only_team_does_not_allow_destructive_operations(self):
        self.rows = [line.rsplit('|', 1)[0] + '|' for line in self.rows]
        state = self.control.managed_team(self.root)
        self.assertIn('orchestrator', state['panes'])
        self.assertFalse(state['can_stop'])
        with self.assertRaises(ValueError):
            self.control.stop_managed(self.root, state)
        self.assertEqual(self.mutations, [])

    def test_legacy_team_requires_the_live_coordinator_lock(self):
        self.rows = [line.rsplit('|', 1)[0].replace(self.token, '') + '|' for line in self.rows]
        with (self.root / '.copilot-mailbox-orchestrator.lock').open('w') as owner:
            self.assertIsNone(self.control.managed_team(self.root))
            fcntl.flock(owner, fcntl.LOCK_EX)
            proved = self.control.managed_team(self.root)
            self.assertEqual(proved['panes']['orchestrator'], '%20')
            self.assertEqual(proved['token'], '')
            self.assertFalse(proved['can_stop'])
            for role in ('alpha', None):
                with self.subTest(role=role), self.assertRaises(ValueError):
                    self.control.stop_managed(self.root, proved, role)
            self.assertEqual(self.mutations, [])
