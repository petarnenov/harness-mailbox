"""Exercise the terminal team selector using an isolated registry, home and screen."""
import contextlib
import curses
import fcntl
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


WORKSPACE = Path(__file__).resolve().parents[1] / 'workspace'
PLUGIN = WORKSPACE / 'copilot-mailbox'


def settings(harness='copilot', model=None, effort=None):
    return dict(harness=harness, model=model, effort=effort)


def snapshot(*projects):
    panes = {'orchestrator': '%20'}
    panes.update({name: '%' + str(21 + i) for i, name in enumerate(projects)})
    return dict(projects=projects, session='$1', panes=panes, token='abcd' * 8, can_stop=True)


class TeamUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(sys, 'path', [str(PLUGIN), *sys.path]):
            import team_ui_data
            cls.data = team_ui_data
            cls.ui = runpy.run_path(str(WORKSPACE / 'team'))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.root = self.base / 'mailbox'
        self.root.mkdir()
        self.config = self.base / 'display preferences'
        self.environ = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.config), 'HOME': str(self.base)})
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.registry = self.root / 'projects.json'
        self.entries = []
        for name in ('alpha', 'beta'):
            project = self.base / name
            project.mkdir()
            self.entries.append(dict(id=name, working_directory=str(project), repository_path=str(project)))
        self.registry.write_text(json.dumps({'projects': self.entries}))

    def test_theme_defaults_to_dark_and_persists_only_the_chosen_palette(self):
        self.assertEqual(self.data.read_theme(), 'dark')
        self.assertTrue(self.data.write_theme('light'))
        self.assertEqual(self.data.read_theme(), 'light')
        self.assertTrue(self.data.write_theme('dark'))
        self.assertEqual(self.data.read_theme(), 'dark')
        self.assertFalse(self.data.write_theme('not-a-theme'))
        self.assertEqual(self.data.read_theme(), 'dark')
        self.assertEqual(list(self.root.iterdir()), [self.registry])

    def test_corrupt_or_inaccessible_preference_does_not_prevent_opening_menu(self):
        self.assertTrue(self.data.write_theme('light'))
        preference, = [p for p in self.config.rglob('*') if p.is_file()]
        for bad in (b'\xffbroken', b'light   not-a-theme'):
            with self.subTest(bad=bad):
                preference.write_bytes(bad)
                self.assertEqual(self.data.read_theme(), 'dark')
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(preference)}):
            self.assertFalse(self.data.write_theme('light'))
            self.assertEqual(self.data.read_theme(), 'dark')

    def test_registry_supplies_project_ids_and_rejects_unsafe_source(self):
        self.assertEqual(sorted(self.data.projects(self.root)), ['alpha', 'beta'])
        self.registry.unlink()
        outside = self.base / 'private-name.json'
        outside.write_text('[]')
        self.registry.symlink_to(outside)
        with self.assertRaises(ValueError) as raised:
            self.data.projects(self.root)
        self.assertNotIn('private-name', str(raised.exception))

    def test_catalog_filters_by_visibility_and_keeps_effort_specific_to_model(self):
        def model(slug, visibility, levels):
            return dict(slug=slug, visibility=visibility,
                        supported_reasoning_levels=[{'effort': level} for level in levels])
        payload = {'models': [model('general', 'list', ['low', 'high']),
                              model('small', 'list', []), model('internal', 'hide', ['high'])]}
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), '')) as query:
            self.assertEqual(self.data.models('codex'), {'general': ['low', 'high'], 'small': []})
            self.assertEqual(query.call_count, 1)
            self.assertIn('models', query.call_args.args[0])
            self.assertIn('timeout', query.call_args.kwargs)

    def test_unavailable_native_catalog_does_not_expose_diagnostics(self):
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('private-command', 1)):
            for host in ('claude', 'codex'):
                with self.subTest(host=host):
                    self.assertEqual(self.data.models(host), {})
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '}{', 'private')):
            self.assertEqual(self.data.models('codex'), {})

    def test_copilot_catalog_reads_enabled_models_and_model_specific_efforts(self):
        rows = [
            {'id': 'example-model', 'policy': {'state': 'enabled'},
             'supportedReasoningEfforts': ['low', 'high']},
            {'id': 'second', 'policy': {'state': 'unconfigured'}},
            {'id': 'pending', 'policy': {'state': 'disabled'}},
            {'id': 'auto'},
            {'id': '--private'},
        ]
        with patch.object(self.data, '_copilot_rows', return_value=rows):
            self.assertEqual(self.data.models('copilot'), {'example-model': ['low', 'high'], 'second': []})

    def test_copilot_queries_the_local_cli_over_its_framed_model_protocol(self):
        binary = self.base / 'copilot'
        binary.write_text("#!" + sys.executable + "\n" + """import json
import sys

line = sys.stdin.buffer.readline()
length = int(line.decode().strip().split(':')[1])
assert sys.stdin.buffer.readline().strip() == b''
request = json.loads(sys.stdin.buffer.read(length))
assert request['method'] == 'models.list'
for message in [
    {'jsonrpc': '2.0', 'method': 'notification'},
    {'jsonrpc': '2.0', 'id': request['id'], 'result': {'models': [{'id': 'via-stdio'}]}},
]:
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(f'Content-Length: {len(body)}\\r\\n\\r\\n'.encode() + body)
    sys.stdout.buffer.flush()
""")
        binary.chmod(0o755)
        with patch.dict(os.environ, {'PATH': str(self.base) + os.pathsep + os.defpath}):
            self.assertEqual(self.data.models('copilot'), {'via-stdio': []})
            binary.write_text('#!/bin/sh\nexit 1\n')
            self.assertEqual(self.data.models('copilot'), {})

    def test_claude_queries_models_at_initialization_without_sending_a_prompt(self):
        rows = [{'value': 'default'}, {'value': 'sample', 'supportedEffortLevels': ['medium', 'high']}]
        payload = {'type': 'control_response', 'response': {
            'request_id': 'models', 'subtype': 'success', 'response': {'models': rows}}}
        reply = json.dumps({'type': 'system'}) + '\n' + json.dumps(payload) + '\n'
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, reply, '')) as query:
            self.assertEqual(self.data.models('claude'), {'sample': ['medium', 'high']})
            argv = query.call_args.args[0]
            self.assertIn('--no-session-persistence', argv)
            self.assertIn('--strict-mcp-config', argv)
            request = json.loads(query.call_args.kwargs['input'])
            self.assertEqual(request['type'], 'control_request')
            self.assertEqual(request['request'], {'subtype': 'initialize'})

    def test_claude_model_and_review_show_live_version_but_launch_uses_native_alias(self):
        rows = [{'value': 'opus', 'displayName': 'Opus 5.5', 'supportedEffortLevels': ['medium', 'high']}]
        down = curses.KEY_DOWN
        keys = [32, 10, down, 10, down, 10, down, 10, down, 10, down, 10, down, down, 10, 10]
        with patch.object(self.data, '_claude_rows', return_value=rows):
            screen, result, _ = self.screen_session(keys, catalog=self.data.models)
        self.assertEqual(result, ('launch', ['alpha'], settings('claude', 'opus', 'medium'),
                                  settings('claude', 'opus', 'high')))
        self.assertTrue(any('Automatic selection' in frame and 'Opus 5.5 (opus)' in frame
                            for frame in screen.frames))
        self.assertEqual(screen.frames[-1].count('Opus 5.5 (opus)'), 2)
        with patch('os.execv') as execute:
            self.data.launch(self.root, *result[1:])
        argv = execute.call_args.args[1]
        for role, effort in [('orchestrator', 'medium'), ('worker', 'high')]:
            self.assertEqual(argv[argv.index('--' + role + '-model') + 1], 'opus')
            self.assertEqual(argv[argv.index('--' + role + '-effort') + 1], effort)

    def test_claude_refresh_replaces_version_and_uses_alias_for_unusable_display_name(self):
        menu = self.ui['Wizard'](['alpha'])
        rows = [{'value': 'opus', 'displayName': 'Opus 5.5'}]
        with patch.object(self.data, '_claude_rows', return_value=rows):
            for key in [32, 10, curses.KEY_DOWN, 10]:
                menu.handle(key, self.data.models)
            self.assertIn(('opus', 'Opus 5.5 (opus)'), menu.options())
            rows[0]['displayName'] = 'Synthetic next version'
            menu.handle(ord('r'), self.data.models)
            self.assertIn(('opus', 'Synthetic next version (opus)'), menu.options())
            self.assertNotIn('Opus 5.5', str(menu.options()))
            for invalid in [{}, {'displayName': '\x1b[31munsafe'}, {'displayName': '   '},
                            {'displayName': 5}, {'displayName': 'opus'}]:
                with self.subTest(invalid=invalid):
                    rows[:] = [{'value': 'opus', **invalid}]
                    menu.handle(ord('r'), self.data.models)
                    self.assertIn(('opus', 'opus'), menu.options())

    def test_active_team_requires_real_lock_and_matching_live_roles(self):
        def tmux(args, **kwargs):
            if 'list-panes' in args:
                return subprocess.CompletedProcess(args, 0, panes, '')
            return subprocess.CompletedProcess(args, 0, '', '')
        lock = self.root / '.copilot-mailbox-orchestrator.lock'
        panes = '$1|%20|0|orchestrator|alpha,beta\n$1|%21|0|alpha|alpha,beta\n$1|%22|0|beta|alpha,beta\n'
        with patch('subprocess.run', side_effect=tmux), lock.open('w') as handle:
            self.assertIsNone(self.data.active_team(self.root))
            fcntl.flock(handle, fcntl.LOCK_EX)
            observed = self.data.active_team(self.root)
            self.assertEqual(observed, (('alpha', 'beta'), '$1', '%20'))
            with patch.dict(os.environ, {'TMUX': ''}), patch('os.execvp') as reconnect:
                self.data.reconnect(self.root, observed)
                reconnect.assert_called_once_with('tmux', ['tmux', 'attach-session', '-t', '$1'])
            original = panes
            for replacement in (original.replace('$1', '$2'), original.replace('%20', '%40')):
                panes = replacement
                with patch('os.execvp') as attach, self.assertRaises(ValueError):
                    self.data.reconnect(self.root, observed)
                attach.assert_not_called()
            panes = original.replace('0|alpha|', '1|alpha|')
            self.assertIsNone(self.data.active_team(self.root))
            with patch('os.execvp') as reconnect, self.assertRaises(ValueError):
                self.data.reconnect(self.root, observed)
            reconnect.assert_not_called()

    def test_active_team_query_handles_absent_host_tools(self):
        lock = self.root / '.copilot-mailbox-orchestrator.lock'
        with lock.open('w') as owner, patch('subprocess.run', side_effect=FileNotFoundError('synthetic tool')) as tmux:
            fcntl.flock(owner, fcntl.LOCK_EX)
            self.assertIsNone(self.data.active_team(self.root))
            self.assertTrue(tmux.called)

    def test_launch_validates_each_role_and_projects_before_execution(self):
        with patch('os.execv') as execute:
            for selection, orchestrator, worker in [
                ([], settings(), settings()), (['missing'], settings(), settings()),
                (['alpha'], settings(model='--flag'), settings()),
                (['alpha'], settings(), settings(harness='other')),
                (['alpha'], settings(), settings(model='demo', effort='--flag')),
            ]:
                with self.subTest(selection=selection, orchestrator=orchestrator, worker=worker):
                    with self.assertRaises(ValueError):
                        self.data.launch(self.root, selection, orchestrator, worker)
            execute.assert_not_called()
        with patch('os.execv', side_effect=OSError('sensitive detail')), self.assertRaises(ValueError) as ex:
            self.data.launch(self.root, ['alpha'], settings(), settings())
        self.assertNotIn('sensitive detail', str(ex.exception))

    def test_registry_detects_duplicates_and_supports_keyed_ids(self):
        self.registry.write_text(json.dumps({'alpha': {}, 'orchestrator': {}, '../invalid': {}}))
        self.assertEqual(self.data.projects(self.root), ['alpha'])
        for data in ([{'id': 'alpha'}, {'id': 'alpha'}], {'projects': 'invalid'}):
            with self.subTest(data=data):
                self.registry.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    self.data.projects(self.root)

    def test_theme_write_failure_preserves_last_saved_choice(self):
        self.assertTrue(self.data.write_theme('light'))
        with patch('os.replace', side_effect=OSError('synthetic failure')):
            self.assertFalse(self.data.write_theme('dark'))
        self.assertEqual(self.data.read_theme(), 'light')
        self.assertEqual([p.name for p in self.config.rglob('*') if p.is_file()], ['team-theme'])

    def test_active_team_enter_requests_reattach_with_original_participants(self):
        menu = self.ui['Wizard'](['alpha', 'beta'], active=snapshot('beta'))
        self.assertEqual(menu.stage, 'active')
        self.assertEqual(menu.handle(10, lambda _: {}), 'reconnect')
        self.assertEqual(menu.selected, ['beta'])
        self.assertNotIn('GitHub Copilot', str(menu.options()))

    def test_worker_counts_are_chosen_independently_for_each_project(self):
        keys = [32, ord('+'), ord('+'), curses.KEY_DOWN, 32, ord('+'), ord('-'), *[10] * 6]
        screen, result, _ = self.screen_session(keys)
        self.assertEqual(result, ('launch', ['alpha'] * 3 + ['beta'], settings(), settings()))
        review = screen.frames[-1]
        for expected in ('alpha  ×3', 'beta  ×1', '1 orchestrator', '4 workers'):
            self.assertIn(expected, review)

    def test_review_back_restores_each_roles_own_harness_model_and_effort(self):
        menu = self.ui['Wizard'](['alpha'])
        def catalog(host):
            return {host + '-sample': ['low', 'high']}
        down = curses.KEY_DOWN
        def press(*keys):
            for key in keys:
                menu.handle(key, catalog)
        press(32, 10, down, 10, down, 10, down, 10)
        self.assertEqual((menu.role, menu.stage), ('worker', 'harness'))
        press(down, down, 10, down, 10, down, down, 10)
        self.assertEqual(menu.stage, 'review')
        self.assertIn('claude-sample', str(menu.options()))
        self.assertIn('codex-sample', str(menu.options()))
        press(27)
        self.assertEqual((menu.role, menu.stage, menu.options()[menu.cursor][0]), ('worker', 'effort', 'high'))
        press(27, 27, 27)
        self.assertEqual((menu.role, menu.stage, menu.options()[menu.cursor][0]), ('orchestrator', 'effort', 'low'))
        press(10, 10)
        self.assertEqual((menu.role, menu.options()[menu.cursor][0]), ('worker', 'codex-sample'))

    def screen_session(self, keys, size=(28, 110), colors=256, projects=None, active=(), catalog=None):
        screen = TerminalScreen(keys, size)
        with (patch.object(self.data, 'managed_team', return_value=active or None),
              patch.object(self.data, 'projects', return_value=['alpha', 'beta'] if projects is None else projects),
              patch.object(self.data, 'models', side_effect=catalog if callable(catalog) else lambda _: catalog or {}),
              patch('curses.has_colors', return_value=bool(colors)),
              patch('curses.start_color'),
              patch('curses.use_default_colors'),
              patch('curses.curs_set'),
              patch('curses.COLORS', colors, create=True),
              patch('curses.COLOR_PAIRS', max(1, colors * 8), create=True),
              patch('curses.color_pair', side_effect=lambda n: n << 8),
              patch('curses.init_pair') as define):
            result = self.ui['run_ui'](screen, self.root)
        return screen, result, define.call_args_list

    def test_both_new_team_and_active_team_check_dashboard_once_when_opened(self):
        for state in (None, snapshot('alpha')):
            with self.subTest(active=bool(state)):
                with patch.object(self.data, 'ensure_dashboard', create=True, return_value=True) as ensure:
                    screen, result, _ = self.screen_session([ord('t'), ord('q')], active=state)
                ensure.assert_called_once_with(self.root)
                menu = 'Active team' if state else 'Select projects'
                self.assertIn(menu, screen.frames[-1])
                self.assertIsNone(result)

    def test_dashboard_failure_shows_english_message_and_worker_selection_still_works(self):
        workflow = self.root / 'dashboard/scripts/workflow.sh'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('echo synthetic-private-detail >&2\nexit 1\n')
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            screen, result, _ = self.screen_session([32, 10, 10, 10, 10, 10, 10])
        self.assertEqual(result[0:2], ('launch', ['alpha']))
        frames = '\n'.join(screen.frames)
        self.assertIn('Dashboard unavailable.', frames)
        self.assertNotIn('synthetic-private-detail', frames + stderr.getvalue())

    def test_windows_handoff_enters_configured_shell_before_dashboard_check(self):
        main = self.ui['main']
        with (patch.object(sys.stdin, 'isatty', return_value=True),
              patch.object(sys.stdout, 'isatty', return_value=True),
              patch.object(sys, 'argv', ['team', '--from-windows']),
              patch('pwd.getpwuid', return_value=SimpleNamespace(pw_shell='/bin/sh')),
              patch('os.execv') as login, patch('curses.wrapper') as menu,
              patch.object(self.data, 'ensure_dashboard', create=True) as ensure):
            self.assertEqual(main(), 0)
        login.assert_called_once()
        menu.assert_not_called()
        ensure.assert_not_called()

    def test_dashboard_check_times_out_on_a_busy_lock_and_on_an_unresponsive_docker(self):
        workflow = self.root / 'dashboard/scripts/workflow.sh'
        workflow.parent.mkdir(parents=True)
        marker = self.root / 'attempt'
        workflow.write_text('touch "$MARKER"\nsleep 5\n')
        with (patch.dict(os.environ, MARKER=str(marker)),
              patch.object(self.data, 'DASHBOARD_START_TIMEOUT', 0.25)):
            with workflow.open('rb') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.assertFalse(self.data.ensure_dashboard(self.root))
                self.assertFalse(marker.exists())
            self.assertFalse(self.data.ensure_dashboard(self.root))
            self.assertTrue(marker.exists())
            workflow.write_text('exit 0\n')
            self.assertTrue(self.data.ensure_dashboard(self.root))

    def test_colocated_dashboard_runs_its_workflow_and_nested_dashboard_takes_precedence(self):
        def add_workflow(directory):
            script = directory / 'scripts/workflow.sh'
            script.parent.mkdir(parents=True)
            script.write_text('printf "%s\\n" "$0" >> "$MARKER"\n')
            return script
        marker = self.root / 'startup-log'
        colocated = add_workflow(self.root)
        with patch.dict(os.environ, MARKER=str(marker)):
            self.assertFalse(self.data.ensure_dashboard(self.root))
            self.assertFalse(marker.exists())
            (self.root / 'compose.yaml').write_text('services: {}\n')
            self.assertTrue(self.data.ensure_dashboard(self.root))
            nested = add_workflow(self.root / 'dashboard')
            (self.root / 'dashboard/compose.yaml').write_text('services: {}\n')
            self.assertTrue(self.data.ensure_dashboard(self.root))
        self.assertEqual(marker.read_text().splitlines(), [str(colocated), str(nested)])

    def test_mixed_and_same_host_selections_forward_distinct_settings_to_the_launcher(self):
        down = curses.KEY_DOWN
        for orchestrator, worker, first_moves, second_moves in (
            ('claude', 'codex', 1, 2), ('copilot', 'copilot', 0, 0),
        ):
            with self.subTest(orchestrator=orchestrator, worker=worker):
                catalog = lambda host: {host + '-large': ['low', 'high'], host + '-small': ['low']}
                keys = [32, 10, *([down] * first_moves), 10, down, 10, down, down, 10,
                        *([down] * second_moves), 10, down, down, 10, down, 10, 10]
                screen, result, _ = self.screen_session(keys, catalog=catalog)
                left = settings(orchestrator, orchestrator + '-large', 'high')
                right = settings(worker, worker + '-small', 'low')
                self.assertEqual(result, ('launch', ['alpha'], left, right))
                for role in (left, right):
                    self.assertIn(role['model'], screen.frames[-1])
                    self.assertIn(role['effort'], screen.frames[-1])
                with patch('os.execv') as execute:
                    self.data.launch(self.root, *result[1:])
                argv = execute.call_args.args[1]
                self.assertIn('--new-team', argv)
                self.assertIn('alpha', argv)
                for role, expected in [('orchestrator', left), ('worker', right)]:
                    for key, value in expected.items():
                        self.assertEqual(argv[argv.index('--' + role + '-' + key) + 1], value)

    def test_model_id_can_be_entered_when_the_catalog_is_unavailable(self):
        model = 'next-q3.1'
        keys = [32, 10, 10, curses.KEY_END, 10, *map(ord, model), 20, 10,
                27, ord('r'), 10, 10, 10, 10]
        screen, result, _ = self.screen_session(keys)
        self.assertEqual(result, ('launch', ['alpha'], settings(model=model), settings()))
        self.assertIn(model, screen.frames[-1])
        self.assertEqual(self.data.read_theme(), 'light')
        self.assertTrue(any('Loading models' in frame for frame in screen.frames))

    def test_refresh_reads_the_new_catalog_and_manual_edits_can_be_cancelled(self):
        wizard = self.ui['Wizard'](['alpha'])
        catalogs = iter(({'old': ['high']}, {'current': ['low']}))
        def press(*keys):
            for key in keys:
                wizard.handle(key, lambda _: next(catalogs))
        press(32, 10, 10)
        self.assertIn(('old', 'old'), wizard.options())
        press(curses.KEY_DOWN, 10, curses.KEY_DOWN, 10)
        self.assertEqual((wizard.roles['orchestrator'].model, wizard.roles['orchestrator'].effort), ('old', 'high'))
        press(27, 27, ord('r'))
        self.assertIsNone(wizard.current.model)
        self.assertIsNone(wizard.current.effort)
        self.assertIn(('current', 'current'), wizard.options())
        self.assertNotIn(('old', 'old'), wizard.options())
        press(curses.KEY_END, 10, 10)
        self.assertEqual(wizard.stage, 'manual')
        self.assertTrue(wizard.message)
        press(ord('a'), curses.KEY_BACKSPACE, ord('b'), 27)
        self.assertEqual(wizard.stage, 'model')
        self.assertIsNone(wizard.current.model)

    def test_theme_key_repaints_the_background_preserves_project_and_survives_restart(self):
        screen, result, pairs = self.screen_session([curses.KEY_DOWN, 32, ord('t'), 10, 10, 10, 10, 10, 10])
        self.assertEqual(result, ('launch', ['beta'], settings(), settings()))
        self.assertEqual(self.data.read_theme(), 'light')
        background = screen.background >> 8
        backgrounds = [call.args[2] for call in pairs if call.args[0] == background]
        self.assertEqual(len(set(backgrounds)), 2)
        self.assertTrue(screen.frames)
        self.assertTrue(any('beta' in f for f in screen.frames))
        second, result, _ = self.screen_session([ord('q')])
        self.assertIsNone(result)
        self.assertIn('[T] Dark theme', second.frames[0])
        self.assertIn('[T] Light theme', screen.frames[0])

    def test_color_fallbacks_small_window_and_resize_keep_navigation_available(self):
        for size, count in [((9, 32), 8), ((18, 56), 16), ((24, 86), 0)]:
            with self.subTest(size=size, colors=count):
                self.data.write_theme('dark')
                screen, result, _ = self.screen_session([curses.KEY_RESIZE, ord('t'), ord('q')], size, count)
                self.assertIsNone(result)
                self.assertEqual(self.data.read_theme(), 'light')
                self.assertTrue(any(frame.strip() for frame in screen.frames))

    def test_tiny_terminal_quits_from_the_manual_model_editor(self):
        keys = [32, 10, 10, curses.KEY_END, 10, ord('m'), (9, 32), ord('t'), ord('Q')]
        screen, result, _ = self.screen_session(keys)
        self.assertIsNone(result)
        self.assertEqual(self.data.read_theme(), 'light')
        self.assertEqual(list(screen.keys), [])

    def test_cancel_after_project_choice_never_launches(self):
        screen, result, _ = self.screen_session([32, 10, 27, ord('q')], (16, 52), 8)
        self.assertIsNone(result)
        self.assertTrue(any('alpha' in frame for frame in screen.frames))

    def test_empty_registry_and_active_team_remain_readable_on_small_screen(self):
        screen, result, _ = self.screen_session([ord('q')], (14, 48), 8, projects=[])
        self.assertIsNone(result)
        self.assertIn('Add project', screen.frames[-1])
        screen, result, _ = self.screen_session([10], (16, 48), 8, active=snapshot('beta'))
        self.assertEqual(result, ('reconnect', snapshot('beta'), None))
        self.assertIn('beta', screen.frames[0])

    def test_stop_controls_require_confirmation_and_target_the_visible_snapshot(self):
        state = snapshot('alpha', 'beta')
        down = curses.KEY_DOWN
        for role, before in [('alpha', [down]), (None, [down, down, down])]:
            with self.subTest(role=role), patch.object(self.data, 'stop_managed') as stop:
                screen, _, _ = self.screen_session([*before, 10, 10, ord('q')], active=state)
                self.assertTrue(any('Cancel' in f and 'Confirm' in f for f in screen.frames))
                stop.assert_not_called()
                self.screen_session([*before, 10, down, 10, ord('q')], active=state)
                stop.assert_called_once_with(self.root, state, role=role)

    def test_confirm_second_worker_by_its_number_after_the_first_exits(self):
        state = snapshot('alpha', 'alpha+2', 'beta')
        del state['panes']['alpha']
        keys = [curses.KEY_DOWN, curses.KEY_DOWN, 10, curses.KEY_DOWN, 10, ord('q')]
        with patch.object(self.data, 'stop_managed') as stop:
            screen, _, _ = self.screen_session(keys, active=state)
        self.assertIn('Stopped worker · alpha #1', screen.frames[0])
        self.assertIn('Stop worker · alpha #2', screen.frames[0])
        self.assertTrue(any('Confirm stop · alpha #2' in frame for frame in screen.frames))
        stop.assert_called_once_with(self.root, state, role='alpha+2')

    def test_unpinned_team_offers_reconnect_and_omits_stop_controls(self):
        state = snapshot('alpha')
        state['can_stop'] = False
        with patch.object(self.data, 'stop_managed') as stop:
            screen, result, _ = self.screen_session([10], active=state)
            self.assertEqual(result, ('reconnect', state, None))
            self.assertIn('Open orchestrator', screen.frames[0])
            self.assertNotIn('Stop worker', screen.frames[0])
            self.assertNotIn('Stop entire team', screen.frames[0])
            stop.assert_not_called()

    def test_foreign_pane_is_excluded_from_the_confirmed_managed_team(self):
        state = snapshot('alpha', 'beta')
        state['foreign'] = True
        del state['panes']['alpha']
        down = curses.KEY_DOWN
        with patch.object(self.data, 'stop_managed') as stop:
            screen, result, _ = self.screen_session([down, 10, down, down, 10, down, 10, ord('q')], active=state)
            self.assertIsNone(result)
            self.assertIn('Stopped worker · alpha', screen.frames[0])
            self.assertIn('Stop worker · beta', screen.frames[0])
            self.assertTrue(any('Confirm stop' in frame for frame in screen.frames))
            stop.assert_called_once_with(self.root, state, role=None)

    def test_left_keeps_home_menu_and_highlight_until_explicit_quit(self):
        left = curses.KEY_LEFT
        for active, extra, selected_row in (
            (None, [ord(' ')], '▸ [●] beta'),
            (snapshot('alpha', 'beta'), [], '▸ Stop worker · alpha'),
        ):
            with self.subTest(active=bool(active)):
                keys = [left, curses.KEY_DOWN, *extra, left, left, ord('q')]
                screen, result, _ = self.screen_session(keys, active=active)
                self.assertIsNone(result)
                self.assertEqual(list(screen.keys), [])
                for frame in screen.frames[-3:]:
                    self.assertIn(selected_row, frame)

    def test_remove_highlighted_project_requires_confirmation_and_preserves_files(self):
        menu = self.ui['Wizard'](['alpha', 'beta'], root=self.root)
        marker = self.base / 'alpha' / 'source.txt'
        marker.write_text('source')
        before = self.registry.read_bytes()
        def press(*keys):
            for key in keys:
                menu.handle(key, lambda _: {})
        press(32, ord('D'))
        self.assertNotEqual(menu.stage, 'projects')
        press(10)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(menu.selected, ['alpha'])
        press(curses.KEY_DC, curses.KEY_DOWN, 10)
        self.assertEqual(menu.stage, 'projects')
        self.assertEqual(menu.projects, ['beta'])
        self.assertEqual(menu.selected, [])
        self.assertEqual(self.data.projects(self.root), ['beta'])
        self.assertEqual(marker.read_text(), 'source')

    def test_stopping_team_reloads_projects_and_drops_deregistered_worker_before_next_launch(self):
        state = snapshot('alpha', 'beta')
        del state['panes']['alpha']
        def keys():
            self.registry.write_text(json.dumps({'projects': self.entries[1:]}))
            yield from [curses.KEY_DOWN] * 3  # Stop team below both workers.
            yield from [10, curses.KEY_DOWN, 10]  # Confirm.
            yield from [10] * 6  # Keep the selected project and the defaults for each role.
        screen = TerminalScreen(keys(), (28, 110))
        with (patch.object(self.data, 'managed_team', side_effect=[state, None]),
              patch.object(self.data, 'stop_managed') as stop,
              patch.object(self.data, 'models', return_value={}),
              patch('curses.has_colors', return_value=False), patch('curses.curs_set')):
            result = self.ui['run_ui'](screen, self.root)
        stop.assert_called_once_with(self.root, state, role=None)
        self.assertEqual(result, ('launch', ['beta'], settings(), settings()))
        self.assertNotIn('alpha', screen.frames[-1])

    def test_active_menu_removes_only_chosen_nonmember_and_keeps_live_snapshot(self):
        state = snapshot('beta')
        menu = self.ui['Wizard'](['alpha', 'beta'], state, self.root)
        before = self.registry.read_bytes()
        def press(*keys):
            for key in keys:
                menu.handle(key, lambda _: {})
        with patch.object(self.data, 'managed_team', return_value=state):
            press(ord('d'))
            self.assertEqual(menu.stage, 'remove_list')
            press(27)
            self.assertEqual(menu.stage, 'active')
            self.assertEqual(self.registry.read_bytes(), before)
            press(ord('d'), 10)
            self.assertEqual(menu.stage, 'remove_confirm')
            labels = [label for _, label in menu.options()]
            self.assertTrue(any('alpha' in label for label in labels))
            self.assertTrue(any(str(self.base / 'alpha') in label for label in labels))
            press(curses.KEY_DOWN, 10)
        self.assertEqual(self.data.projects(self.root), ['beta'])
        self.assertEqual(menu.selected, ['beta'])
        self.assertEqual(menu.active, state)

    def test_highlighted_selected_project_is_displayed_once_on_wide_screen(self):
        screen, _, _ = self.screen_session([ord(' '), ord('q')], size=(30, 110))
        self.assertIn('[●] alpha', screen.frames[-1])
        self.assertEqual(screen.frames[-1].count('alpha'), 1)

    def test_remove_shortcut_ignores_add_row_and_repeated_add_keeps_single_selected_id(self):
        menu = self.ui['Wizard'](['alpha', 'beta'], root=self.root)
        before = self.registry.read_bytes()
        menu.handle(curses.KEY_END, lambda _: {})
        menu.handle(curses.KEY_DC, lambda _: {})
        self.assertEqual(menu.stage, 'projects')
        self.assertEqual(self.registry.read_bytes(), before)
        menu.folder = self.base / 'alpha'
        menu.folder_origin = 'projects'
        for project_id in ('first-id', 'second-id'):
            menu.project_input = project_id
            menu.save_project()
            self.assertEqual(menu.selected, ['alpha'])
            self.assertEqual([key for key, _ in menu.options()].count('alpha'), 1)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_active_team_can_register_a_folder_without_changing_its_members(self):
        state = snapshot('beta')
        menu = self.ui['Wizard'](['alpha', 'beta'], state, self.root)
        for key in (ord('a'), curses.KEY_HOME, 10, 21, *map(ord, 'extra'), 10):
            menu.handle(key, lambda _: {})
        self.assertEqual(menu.stage, 'active')
        self.assertEqual(menu.selected, ['beta'])
        self.assertEqual(menu.active, snapshot('beta'))
        self.assertIn('extra', self.data.projects(self.root))

    def test_folder_picker_adds_selected_directory_and_cancel_leaves_registry_unchanged(self):
        project = self.base / 'candidate'
        project.mkdir()
        menu = self.ui['Wizard'](['alpha', 'beta'], root=self.root)
        def press(*keys):
            for key in keys:
                menu.handle(key, lambda _: {})
        before = self.registry.read_bytes()
        press(ord('a'), 27)
        self.assertEqual(menu.stage, 'projects')
        self.assertEqual(self.registry.read_bytes(), before)
        press(ord('a'))
        paths = [label for _, label in menu.options()]
        index = next(i for i, label in enumerate(paths) if 'candidate' in label)
        press(curses.KEY_HOME, *([curses.KEY_DOWN] * index), 10, curses.KEY_HOME, 10)
        self.assertEqual(menu.stage, 'project_id')
        press(21, *map(ord, 'alpha'), 10)
        self.assertEqual(menu.stage, 'project_id')
        self.assertTrue(menu.message)
        self.assertEqual(self.registry.read_bytes(), before)
        press(21, *map(ord, 'fresh-id'), 10)
        self.assertEqual(menu.stage, 'projects')
        self.assertEqual(menu.selected, ['fresh-id'])
        self.assertIn('fresh-id', self.data.projects(self.root))
        self.assertFalse((self.root / 'sessions').exists())

    def test_folder_arrows_and_enter_open_nested_paths_and_space_selects_current_folder(self):
        home = self.base / 'browse'
        group = home / 'Company'
        project = group / '02 Project'
        (group / '01 Other').mkdir(parents=True)
        (project / 'src').mkdir(parents=True)
        menu = self.ui['Wizard'](['alpha', 'beta'], root=self.root)
        before = self.registry.read_bytes()
        with patch.object(Path, 'home', return_value=home):
            menu.handle(ord('a'), lambda _: {})
        self.assertEqual(menu.options()[menu.cursor][0], group)
        for key in (curses.KEY_RIGHT, curses.KEY_DOWN, 10):
            menu.handle(key, lambda _: {})
        self.assertEqual((menu.stage, menu.folder), ('folder', project))
        menu.handle(curses.KEY_LEFT, lambda _: {})
        self.assertEqual((menu.stage, menu.folder), ('folder', group))
        self.assertEqual(menu.options()[menu.cursor][0], project)
        menu.handle(curses.KEY_RIGHT, lambda _: {})
        self.assertEqual(menu.options()[menu.cursor][0], project / 'src')
        menu.handle(ord(' '), lambda _: {})
        self.assertEqual(menu.stage, 'project_id')
        self.assertEqual(menu.project_input, '02-Project')
        self.assertEqual(self.registry.read_bytes(), before)
        menu.handle(10, lambda _: {})
        self.assertEqual(menu.selected, ['02-Project'])
        records = json.loads(self.registry.read_text())['projects']
        self.assertEqual(Path(records[-1]['working_directory']), project.resolve())

    def test_missing_subfolder_does_not_escape_picker_or_change_existing_selection(self):
        parent = self.base / 'browse'
        missing = parent / 'disappearing'
        missing.mkdir(parents=True)
        menu = self.ui['Wizard'](['alpha', 'beta'], root=self.root)
        menu.handle(ord(' '), lambda _: {})
        before = self.registry.read_bytes()
        with patch.object(Path, 'home', return_value=parent):
            menu.handle(ord('a'), lambda _: {})
        missing.rmdir()
        menu.handle(curses.KEY_RIGHT, lambda _: {})
        self.assertEqual((menu.stage, menu.folder), ('folder', parent))
        self.assertTrue(menu.message)
        menu.handle(27, lambda _: {})
        self.assertEqual((menu.stage, menu.selected), ('projects', ['alpha']))
        self.assertEqual(self.registry.read_bytes(), before)

    def test_left_at_filesystem_root_stays_in_picker_until_escape(self):
        menu = self.ui['Wizard']([], root=self.root)
        root = Path('/')
        with patch.object(self.data, 'folders', return_value=[]):
            menu.open_folder(root)
            menu.handle(curses.KEY_LEFT, lambda _: {})
        self.assertEqual((menu.stage, menu.folder), ('folder', root))
        menu.handle(27, lambda _: {})
        self.assertEqual(menu.stage, 'projects')

    def test_main_hands_off_only_after_menu_returns_and_reports_sanitized_failure(self):
        main = self.ui['main']
        with (patch.object(sys.stdin, 'isatty', return_value=True),
              patch.object(sys.stdout, 'isatty', return_value=True),
              patch('curses.wrapper') as menu,
              patch.object(self.data, 'reconnect') as reconnect,
              patch.object(self.data, 'launch') as launch):
            menu.return_value = ('reconnect', snapshot('alpha'), None)
            self.assertEqual(main(), 0)
            reconnect.assert_called_once_with(WORKSPACE, snapshot('alpha'))
            launch.assert_not_called()
            menu.return_value = ('launch', ['beta'], settings(), settings())
            self.assertEqual(main(), 0)
            launch.assert_called_once_with(WORKSPACE, ['beta'], settings(), settings())
            launch.side_effect = ValueError('synthetic-private-detail')
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(), 1)
            self.assertIn('Could not open team', error.getvalue())
            self.assertNotIn('synthetic-private-detail', error.getvalue())

    def test_minimum_screen_can_browse_every_selected_project_before_launch(self):
        projects = [f'project-{i:02}' for i in range(12)]
        keys = [32]
        for _ in projects[1:]:
            keys.extend([curses.KEY_DOWN, 32])
        keys.extend([10, 10, 10, 10, 10])
        keys.extend([curses.KEY_DOWN, 10])
        keys.extend([curses.KEY_DOWN] * (len(projects) + 7))
        keys.append(curses.KEY_HOME)
        keys.append(10)
        screen, result, _ = self.screen_session(keys, (12, 42), 8, projects=projects)
        self.assertEqual(result, ('launch', projects, settings(), settings()))
        self.assertTrue(any('Space Select  +/- Count  A Add  D Remove' in frame for frame in screen.frames))
        self.assertTrue(any('↑↓ Move  Enter OK  Esc Back  Q Quit' in frame for frame in screen.frames))
        review_start = next(i for i, frame in enumerate(screen.frames) if 'Start team' in frame)
        review = '\n'.join(screen.frames[review_start:])
        for project in projects:
            self.assertIn(project, review)
        self.assertEqual(list(screen.keys), [])

    def test_horizontal_scroll_distinguishes_long_project_ids_in_a_narrow_terminal(self):
        prefix = 'synthetic-project-with-a-long-shared-prefix-'
        projects = [prefix + 'first', prefix + 'second']
        keys = [curses.KEY_DOWN, curses.KEY_RIGHT, curses.KEY_RIGHT,
                curses.KEY_RIGHT, 32, 10, 10, 10, 10, 10, 10]
        screen, result, _ = self.screen_session(keys, (12, 42), 8, projects=projects)
        self.assertEqual(result, ('launch', [projects[1]], settings(), settings()))
        self.assertTrue(any('second' in frame for frame in screen.frames))


class TerminalScreen:
    """Model positional text with clipping checks and a finite real-key input stream."""
    def __init__(self, keys, size):
        self.keys = iter(keys)
        self.size = size
        self.frames = []
        self.erase()

    def getmaxyx(self):
        return self.size

    def bkgd(self, char, attrs):
        self.background = attrs

    def erase(self):
        h, w = self.size
        self.rows = [[' '] * w for _ in range(h)]

    def addnstr(self, y, x, s, width, attr=0):
        h, w = self.size
        if not 0 <= y < h or not 0 <= x < w or width < 0:
            raise AssertionError((self.size, y, x, width))
        display = s[:width]
        if len(display) > w - x:
            raise AssertionError(('line overflow', self.size, y, x, display))
        self.rows[y][x:x + len(display)] = display

    def refresh(self):
        self.frames.append('\n'.join(''.join(row).rstrip() for row in self.rows))

    def keypad(self, _):
        pass

    def getch(self):
        try:
            event = next(self.keys)
            if isinstance(event, tuple):
                self.size = event
                return curses.KEY_RESIZE
            return event
        except StopIteration:
            raise AssertionError('menu still polling after exit/launch')


if __name__ == '__main__':
    unittest.main()
