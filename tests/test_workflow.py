"""Behavioral tests for Make's shell workflow, using an isolated Docker CLI double."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


FAKE_DOCKER = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['DOCKER_LOG'], 'a') as log:
    log.write(json.dumps({'args': args, 'root': os.environ.get('MAILBOX_ROOT')}) + '\n')
if args == ['--version']:
    sys.exit(int(os.environ.get('FAIL_CLI', '0')))
if args == ['info']:
    sys.exit(int(os.environ.get('FAIL_DAEMON', '0')))
if args[0] == 'ps':
    if os.environ.get('FAIL_STATUS'):
        sys.exit(1)
    state = pathlib.Path(os.environ['DASHBOARD_STATE'])
    if state.exists():
        prefix = 'abcde1234567 ' if '{{.ID}}' in args[-1] else ''
        print(prefix + state.read_text() + ' ' + os.environ.get('SERVICE_OWNER', os.getcwd()))
    if os.environ.get('ONEOFF') and 'label=com.docker.compose.oneoff=False' not in args:
        print('running ' + os.getcwd())
if args[0] == 'inspect':
    if args[-1] != 'abcde1234567':
        sys.exit(1)
    # The Docker inspect template should prefer the client-visible label on desktop engines.
    label = os.environ.get('SERVICE_LABEL') if 'io.harness-mailbox.root' in args[2] else None
    mount = os.environ.get('SERVICE_DATA', os.environ.get('MAILBOX_ROOT', ''))
    print(label or mount)
if args[:2] == ['compose', 'version']:
    sys.exit(int(os.environ.get('FAIL_COMPOSE', '0')))
if args[0] == 'compose':
    command = args[7:]
    if '--help' in command:
        if command[0] == 'config' and not os.environ.get('OLD_CONFIG'):
            print('--environment')
        if command[0] == 'up' and not os.environ.get('OLD_UP'):
            print('--wait')
    elif command == ['config', '--quiet']:
        sys.exit(int(os.environ.get('FAIL_CONFIG', '0')))
    elif command == ['config', '--environment']:
        value = os.environ.get('MAILBOX_ROOT')
        if value is None:
            env = pathlib.Path('.env')
            if env.exists():
                value = env.read_text().split('MAILBOX_ROOT=', 1)[-1].splitlines()[0].strip()
                if value.startswith("'") and value.endswith("'"):
                    value = value[1:-1].replace("\\'", "'")
        print('MAILBOX_ROOT=' + (value or ''))
    elif command[0] == 'up' and 'DASHBOARD_STATE' in os.environ:
        if os.environ.get('FAIL_START'):
            print('synthetic-private-detail', file=sys.stderr)
            sys.exit(1)
        if os.environ.get('SLOW_START'):
            import time
            time.sleep(0.15)
        pathlib.Path(os.environ['DASHBOARD_STATE']).write_text('running')
'''


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'mailbox space $ #'
        self.app = self.root / 'dashboard'
        source = Path(__file__).resolve().parents[1]
        for directory in ('scripts', 'workspace'):
            shutil.copytree(source / directory, self.app / directory, symlinks=True,
                            ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('Makefile', 'workspace.Makefile', 'compose.yaml', 'README.md'):
            shutil.copyfile(source / name, self.app / name)
        # Installer behavior has its own fully mocked test suite. Keep workflow
        # tests incapable of invoking any host package manager or network fetch.
        (self.app / 'scripts/install-system.sh').write_text(
            '#!/bin/sh\nprintf "called\\n" >> "$INSTALLER_LOG"\n')
        self.installer_log = Path(self.temp.name) / 'installer.log'
        (self.root / 'projects.json').write_text('{"projects": []}')
        self.log = Path(self.temp.name) / 'docker-log.jsonl'
        self.docker = Path(self.temp.name) / 'fake docker'
        self.docker.write_text('#!' + sys.executable + '\n' + FAKE_DOCKER)
        self.docker.chmod(0o755)
        self.home = Path(self.temp.name).resolve() / 'home'
        self.home.mkdir()
        self.bin = self.home / '.local' / 'bin'
        tools = self.home / 'test-tools'
        tools.mkdir()
        (tools / 'python3').symlink_to(sys.executable)
        (tools / 'tmux').write_text('#!/bin/sh\nprintf "tmux 3.4\n"\n')
        (tools / 'tmux').chmod(0o755)
        self.env = dict(HOME=str(self.home), SHELL='/bin/sh', PATH=f'{tools}:/usr/bin:/bin',
                        DOCKER=str(self.docker), DOCKER_LOG=str(self.log), INSTALLER_LOG=str(self.installer_log))

    def run_workflow(self, command, **changes):
        return subprocess.run(['sh', str(self.app / 'scripts/workflow.sh'), command],
                              cwd=self.temp.name, env=dict(self.env, **changes),
                              text=True, capture_output=True, timeout=20)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_team_dashboard_is_scoped_and_only_starts_when_absent_or_stopped(self):
        state = self.app / 'state'
        for status in ('missing', 'stopped', 'running'):
            with self.subTest(status=status):
                self.log.unlink(missing_ok=True)
                if status != 'missing':
                    state.write_text(status)
                result = self.run_workflow('ensure-dashboard', PROJECT_NAME='test-dashboard',
                                           DASHBOARD_STATE=str(state), ONEOFF='1')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(state.read_text(), 'running')
                calls = [call['args'] for call in self.calls() if call['args'][0] == 'compose']
                prefix = ['compose', '--project-name', 'test-dashboard', '--project-directory', str(self.app),
                          '--file', str(self.app / 'compose.yaml')]
                self.assertTrue(all(call[:7] == prefix for call in calls), calls)
                statuses = [c['args'] for c in self.calls() if c['args'][0] == 'ps']
                self.assertTrue(statuses)
                for status_call in statuses:
                    self.assertIn('label=com.docker.compose.project=test-dashboard', status_call)
                    self.assertIn('label=com.docker.compose.service=dashboard', status_call)
                    self.assertIn('label=com.docker.compose.oneoff=False', status_call)
                starts = [call[7:] for call in calls if call[7] == 'up']
                if status == 'running':
                    self.assertFalse(starts)
                else:
                    start, = starts
                    self.assertEqual((start[0], start[-1]), ('up', 'dashboard'))
                    self.assertTrue({'-d', '--no-deps', '--no-recreate'} <= set(start))
                    self.assertNotIn('--build', start)

    def test_dashboard_with_same_compose_name_from_different_checkout_is_left_alone(self):
        state = self.app / 'state'
        foreign = self.root / 'another checkout'
        foreign.mkdir()
        for status in ('running', 'exited'):
            with self.subTest(status=status):
                self.log.unlink(missing_ok=True)
                state.write_text(status)
                result = self.run_workflow('ensure-dashboard', DASHBOARD_STATE=str(state),
                                           SERVICE_OWNER=str(foreign))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(state.read_text(), status)
                self.assertFalse(any('up' in call['args'] for call in self.calls()))

    def open_team_dashboard(self, mailbox):
        plugin = Path(__file__).resolve().parents[1] / 'workspace/copilot-mailbox'
        with patch.object(sys, 'path', [str(plugin), *sys.path]):
            import team_ui_data
        return team_ui_data.ensure_dashboard(mailbox)

    def test_team_dashboard_uses_its_mailbox_despite_a_different_saved_or_exported_root(self):
        selected = self.root / 'selected'
        selected.mkdir()
        (selected / 'dashboard').symlink_to(self.app, target_is_directory=True)
        (selected / 'projects.json').write_text('[]')
        config = f"MAILBOX_ROOT='{self.root}'\n"
        (self.app / '.env').write_text(config)
        for previous in ({}, {'MAILBOX_ROOT': str(self.root)}):
            with self.subTest(previous=previous):
                state = self.app / 'state'
                state.unlink(missing_ok=True)
                self.log.unlink(missing_ok=True)
                with patch.dict(os.environ, dict(self.env, DASHBOARD_STATE=str(state), **previous), clear=True):
                    self.assertTrue(self.open_team_dashboard(selected))
                start, = [c for c in self.calls() if 'up' in c['args']]
                self.assertEqual(start['root'], str(selected))
                self.assertEqual((self.app / '.env').read_text(), config)

    def test_team_does_not_adopt_or_restart_a_service_displaying_another_mailbox(self):
        state = self.app / 'state'
        foreign = self.root / 'different mailbox'
        foreign.mkdir()
        for status in ('running', 'exited'):
            with self.subTest(status=status):
                state.write_text(status)
                self.log.unlink(missing_ok=True)
                with patch.dict(os.environ, dict(self.env, DASHBOARD_STATE=str(state),
                                                 SERVICE_DATA=str(foreign)), clear=True):
                    self.assertFalse(self.open_team_dashboard(self.root))
                self.assertFalse(any('up' in c['args'] for c in self.calls()))
                self.assertEqual(state.read_text(), status)

    def test_team_recognizes_the_client_mailbox_label_for_a_desktop_bind_mount(self):
        state = self.app / 'state'
        state.write_text('running')
        settings = dict(self.env, DASHBOARD_STATE=str(state), SERVICE_DATA='/vm/private/mount',
                        SERVICE_LABEL=str(self.root))
        with patch.dict(os.environ, settings, clear=True):
            self.assertTrue(self.open_team_dashboard(self.root))
        self.assertFalse(any('up' in c['args'] for c in self.calls()))

    def test_unknown_dashboard_status_cannot_start_a_container(self):
        result = self.run_workflow('ensure-dashboard', DASHBOARD_STATE=str(self.app / 'state'), FAIL_STATUS='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any(call['args'][0] == 'ps' for call in self.calls()))
        self.assertFalse(any('up' in call['args'] for call in self.calls()))

    def test_team_dashboard_respects_existing_configuration_and_reports_failed_start(self):
        configured = self.root / 'configured mailbox'
        configured.mkdir()
        (configured / 'projects.json').write_text('[]')
        content = f"MAILBOX_ROOT='{configured}'\n"
        env_file = self.app / '.env'
        env_file.write_text(content)
        state = self.app / 'state'
        result = self.run_workflow('ensure-dashboard', DASHBOARD_STATE=str(state), FAIL_START='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(state.exists())
        self.assertTrue(any('up' in c['args'] for c in self.calls()))
        self.assertTrue(all(c['root'] is None for c in self.calls()))
        self.assertEqual(env_file.read_text(), content)

    def test_concurrent_menu_openings_share_one_dashboard_start(self):
        plugin = Path(__file__).resolve().parents[1] / 'workspace/copilot-mailbox'
        with patch.object(sys, 'path', [str(plugin), *sys.path]):
            import team_ui_data
        begin = threading.Barrier(2)

        def open_dashboard():
            begin.wait(timeout=5)
            return team_ui_data.ensure_dashboard(self.root)

        with (patch.dict(os.environ, dict(self.env, DASHBOARD_STATE=str(self.app / 'state'), SLOW_START='1'),
                         clear=True), ThreadPoolExecutor(max_workers=2) as executor):
            futures = [executor.submit(open_dashboard) for _ in range(2)]
            self.assertTrue(all(future.result(timeout=10) for future in futures))
        self.assertEqual((self.app / 'state').read_text(), 'running')
        self.assertEqual(sum('up' in call['args'] for call in self.calls()), 1)

    def test_setup_and_doctor_reject_an_unreadable_project_registry_without_replacing_it(self):
        self.assertEqual(self.run_workflow('setup').returncode, 0)
        registry = self.root / 'projects.json'
        path = f"{self.bin}:{self.env['PATH']}"
        for invalid in ('{"private-fixture-detail"', 'null', '{"projects":7}'):
            registry.write_text(invalid)
            for action in ('doctor', 'setup'):
                with self.subTest(invalid=invalid, action=action):
                    result = self.run_workflow(action, PATH=path)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('private-fixture-detail', result.stdout + result.stderr)
                    self.assertEqual(registry.read_text(), invalid)

    def test_doctor_is_read_only_and_reports_dependencies(self):
        result = self.run_workflow('setup')
        self.assertEqual(0, result.returncode, result.stderr)
        self.log.unlink()
        self.installer_log.unlink()
        before = {p: p.read_bytes() for p in (self.app / '.env', self.root / 'Makefile', self.home / '.profile')}
        result = self.run_workflow('doctor', PATH=f"{self.bin}:{self.env['PATH']}")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('OK: Compose configuration', result.stdout)
        self.assertIn('team', result.stdout.lower())
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertFalse(self.installer_log.exists())
        self.assertTrue(all(c['args'][0] in ('--version', 'info', 'compose') for c in self.calls()))
        self.assertTrue(all(c['args'][7] == 'config' or '--help' in c['args']
                            for c in self.calls() if c['args'][0] == 'compose' and len(c['args']) > 2))

    def test_missing_dependencies_have_actionable_nonzero_failures(self):
        for changes, message in [({'DOCKER': str(self.root / 'missing')}, 'Docker CLI is missing'),
                                 ({'FAIL_CLI': '1'}, 'Docker CLI cannot execute'),
                                 ({'FAIL_COMPOSE': '1'}, 'Compose plugin is missing'),
                                 ({'FAIL_DAEMON': '1'}, 'daemon is unavailable'),
                                 ({'OLD_CONFIG': '1'}, 'config --environment is required'),
                                 ({'OLD_UP': '1'}, 'up --wait is required')]:
            with self.subTest(changes=changes):
                result = self.run_workflow('doctor', **changes)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.app / '.env').exists())

    def test_setup_creates_configuration_and_workspace_entrypoint_once(self):
        result = self.run_workflow('setup')
        self.assertEqual(0, result.returncode, result.stderr)
        config = (self.app / '.env').read_bytes()
        self.assertIn(str(self.root), config.decode())
        self.assertTrue(self.installer_log.exists())
        self.assertEqual((self.app / 'workspace.Makefile').read_bytes(), (self.root / 'Makefile').read_bytes())
        calls = [c['args'] for c in self.calls()]
        config_index = next(i for i, c in enumerate(calls) if c[-2:] == ['config', '--environment'])
        build_index = next(i for i, c in enumerate(calls) if c[0] == 'build')
        self.assertLess(config_index, build_index)
        self.assertIn(['pull', 'node:22-alpine'], calls)
        (self.root / 'Makefile').write_text('# existing custom workspace Makefile\n')
        result = self.run_workflow('setup')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(config, (self.app / '.env').read_bytes())
        self.assertEqual('# existing custom workspace Makefile\n', (self.root / 'Makefile').read_text())

    def test_setup_detects_a_mailbox_living_in_the_app_directory(self):
        (self.root / 'projects.json').unlink()
        (self.app / 'projects.json').write_text('{"projects": []}')
        result = self.run_workflow('setup')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("MAILBOX_ROOT='" + str(self.app) + "'\n", (self.app / '.env').read_text())
        self.assertFalse((self.root / 'Makefile').exists())

    def test_setup_prefers_the_app_directory_mailbox_over_the_parent(self):
        (self.app / 'projects.json').write_text('{"projects": []}')
        result = self.run_workflow('setup')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("MAILBOX_ROOT='" + str(self.app) + "'\n", (self.app / '.env').read_text())

    def test_existing_config_and_override_are_preserved(self):
        config = b"# retained comment\nMAILBOX_ROOT='/does/not/exist'\n"
        (self.app / '.env').write_bytes(config)
        result = self.run_workflow('setup', MAILBOX_ROOT=str(self.root))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(config, (self.app / '.env').read_bytes())
        self.assertTrue(all(c['root'] == str(self.root) for c in self.calls()))

    def test_invalid_configuration_stops_before_build_or_pull(self):
        for root, message in [('/does/not/exist', 'existing directory'), ('relative', 'absolute directory'),
                              (str(self.app), 'no projects.json')]:
            with self.subTest(root=root):
                result = self.run_workflow('doctor', MAILBOX_ROOT=root)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(message, result.stderr)
        result = self.run_workflow('setup', MAILBOX_ROOT=str(self.root), FAIL_CONFIG='1')
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(any(c['args'][0] in ('build', 'pull') for c in self.calls()))
        self.assertNotEqual(0, self.run_workflow('setup', MAILBOX_ROOT='relative').returncode)

    def test_doctor_identifies_a_missing_team_with_a_working_dashboard(self):
        (self.app / '.env').write_text(f"MAILBOX_ROOT='{self.root}'\n")
        result = self.run_workflow('doctor')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('team', result.stderr.lower())
        self.assertIn('make setup', result.stderr)
        self.assertFalse(self.installer_log.exists())

    def test_doctor_reports_both_missing_team_and_unavailable_docker_on_fresh_checkout(self):
        (self.root / 'projects.json').unlink()
        result = self.run_workflow('doctor', FAIL_DAEMON='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('daemon is unavailable', result.stderr)
        self.assertIn('team', result.stderr.lower())
        self.assertIn('make setup', result.stderr)
        for path in (self.app / '.env', self.app / 'mailbox-data', self.bin, self.home / '.profile'):
            self.assertFalse(path.exists(), path)
        self.assertFalse(self.installer_log.exists())

    def test_fresh_checkout_setup_installs_team(self):
        (self.root / 'projects.json').unlink()
        profile = self.home / '.profile'
        profile.write_text('# existing profile\n')
        result = self.run_workflow('setup')
        self.assertEqual(result.returncode, 0, result.stderr)
        mailbox = self.app / 'mailbox-data'
        registry = mailbox / 'projects.json'
        self.assertEqual(json.loads(registry.read_text()), {'projects': []})
        self.assertEqual((self.app / '.env').read_text(), f"MAILBOX_ROOT='{mailbox}'\n")
        self.assertTrue((self.bin / 'team').samefile(mailbox / 'team'))
        self.assertTrue((mailbox / 'dashboard/scripts/workflow.sh').samefile(self.app / 'scripts/workflow.sh'))
        for name in ('README.md', 'AGENTS.md', 'copilot-mailbox/extensions/mailbox/extension.mjs'):
            self.assertTrue((mailbox / name).is_file(), name)
        self.assertIn('# existing profile', profile.read_text())
        doctor = subprocess.run(['sh', '-c', '. "$HOME/.profile" && exec make doctor'], env=self.env,
                                cwd=self.app, text=True, capture_output=True, timeout=20)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertIn('team', doctor.stdout.lower())
        launch = subprocess.run(['sh', '-c', '. "$HOME/.profile" && exec team'], env=self.env,
                                cwd=self.temp.name, text=True, capture_output=True, timeout=10)
        self.assertEqual(launch.stderr.strip(), 'Run team in an interactive terminal.')
        registry.write_text('{"projects":[{"project_id":"fixture"}]}')
        (mailbox / 'AGENTS.md').write_text('# Custom mailbox instructions\n')
        before = {p: p.read_bytes() for p in (registry, profile, mailbox / 'AGENTS.md', self.app / '.env')}
        result = self.run_workflow('setup')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_doctor_diagnoses_a_shadowed_team_without_executing_it(self):
        result = self.run_workflow('setup')
        self.assertEqual(result.returncode, 0, result.stderr)
        foreign = self.home / 'test-tools/team'
        foreign.write_text('#!/bin/sh\nprintf "synthetic-private-detail" > "$HOME/foreign-called"\n')
        foreign.chmod(0o755)
        result = self.run_workflow('doctor', PATH=f"{self.env['PATH']}:{self.bin}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('team', result.stderr.lower())
        self.assertNotIn('synthetic-private-detail', result.stderr)
        self.assertFalse((self.home / 'foreign-called').exists())

    def test_setup_does_not_deploy_through_a_symlinked_plugin_directory(self):
        external = self.home / 'project'
        external.mkdir()
        sentinel = external / 'start'
        sentinel.write_text('original project file')
        (self.root / 'copilot-mailbox').symlink_to(external, target_is_directory=True)
        result = self.run_workflow('setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(external.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), 'original project file')

    def test_fresh_setup_does_not_create_a_registry_through_a_symlinked_mailbox_data(self):
        (self.root / 'projects.json').unlink()
        external = self.home / 'external'
        external.mkdir()
        (self.app / 'mailbox-data').symlink_to(external, target_is_directory=True)
        result = self.run_workflow('setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse((self.app / '.env').exists())

    def test_setup_rejects_an_explicit_directory_without_a_registry(self):
        (self.root / 'projects.json').unlink()
        result = self.run_workflow('setup', MAILBOX_ROOT=str(self.root))
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.app / '.env').exists())
        self.assertFalse((self.root / 'Makefile').exists())

    def test_demo_uses_temporary_override_and_fresh_image(self):
        (self.app / 'demo').mkdir()
        (self.app / 'demo/projects.json').write_text('{}')
        config = b"MAILBOX_ROOT='/original/config'\n"
        (self.app / '.env').write_bytes(config)
        result = self.run_workflow('demo', MAILBOX_ROOT='/caller/override')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(config, (self.app / '.env').read_bytes())
        up = next(c for c in self.calls() if c['args'][-4:] == ['up', '-d', '--build', '--wait'])
        self.assertEqual(str(self.app / 'demo'), up['root'])
        self.assertEqual(['--project-name', 'mailbox-dashboard'], up['args'][1:3])

    def test_service_and_check_commands_use_intended_flags(self):
        for command in ('up', 'restart', 'down', 'status', 'logs', 'health', 'build', 'tools', 'test', 'lint', 'coverage'):
            result = self.run_workflow(command, MAILBOX_ROOT=str(self.root))
            self.assertEqual(0, result.returncode, (command, result.stderr))
        calls = [c['args'] for c in self.calls()]
        self.assertTrue(any(c[-5:] == ['up', '-d', '--build', '--force-recreate', '--wait'] for c in calls))
        runs = [c for c in calls if c[0] == 'run']
        self.assertTrue(all('--read-only' in c and ['--network', 'none'] == c[c.index('--network'):c.index('--network')+2] for c in runs))
        self.assertTrue(all(any('target=/workspace,readonly' in a for a in c) for c in runs))
        self.assertTrue(any('COVERAGE_FILE=/coverage/.coverage' in c for c in runs))
        self.assertNotEqual(0, self.run_workflow('invalid').returncode)

    def test_real_make_forwards_from_workspace_with_dollar_in_path(self):
        special = Path(self.temp.name) / 'mailbox $UNSET_REVIEW_PATH # quote\''
        special.mkdir()
        (special / 'dashboard').symlink_to(self.app, target_is_directory=True)
        shutil.copyfile(self.app / 'workspace.Makefile', special / 'Makefile')
        setup = self.run_workflow('setup')
        self.assertEqual(0, setup.returncode, setup.stderr)
        result = subprocess.run(['make', 'doctor'], cwd=special,
                                env=dict(self.env, MAILBOX_ROOT=str(self.root), PATH=f"{self.bin}:{self.env['PATH']}"),
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('OK: Compose configuration', result.stdout)

    def test_env_is_not_executed_as_shell(self):
        marker = self.root / 'must-not-exist'
        (self.app / '.env').write_text('MAILBOX_ROOT=$(touch "' + str(marker) + '")\n')
        result = self.run_workflow('doctor')
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(marker.exists())
