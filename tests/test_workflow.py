"""Behavioral tests for Make's shell workflow, using an isolated Docker CLI double."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


FAKE_DOCKER = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['DOCKER_LOG'], 'a') as log:
    log.write(json.dumps({'args': args, 'root': os.environ.get('MAILBOX_ROOT')}) + '\n')
if args == ['--version']:
    sys.exit(int(os.environ.get('FAIL_CLI', '0')))
if args == ['info']:
    sys.exit(int(os.environ.get('FAIL_DAEMON', '0')))
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
'''


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'mailbox space $ #'
        self.app = self.root / 'dashboard'
        (self.app / 'scripts').mkdir(parents=True)
        source = Path(__file__).resolve().parents[1]
        for name in ('scripts/workflow.sh', 'scripts/install-system.sh', 'workspace.Makefile', 'compose.yaml'):
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
        self.env = {k: v for k, v in os.environ.items() if k not in ('MAILBOX_ROOT', 'DOCKER', 'PROJECT_NAME')}
        self.env.update(DOCKER=str(self.docker), DOCKER_LOG=str(self.log), INSTALLER_LOG=str(self.installer_log))

    def run_workflow(self, command, **changes):
        return subprocess.run(['sh', str(self.app / 'scripts/workflow.sh'), command],
                              cwd=self.temp.name, env=dict(self.env, **changes),
                              text=True, capture_output=True, timeout=20)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_doctor_is_read_only_and_reports_dependencies(self):
        (self.app / '.env').write_text("MAILBOX_ROOT='" + str(self.root) + "'\n")
        before = (self.app / '.env').read_bytes()
        result = self.run_workflow('doctor')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('OK: Compose configuration', result.stdout)
        self.assertEqual(before, (self.app / '.env').read_bytes())
        self.assertFalse((self.root / 'Makefile').exists())
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

    def test_no_mailbox_does_not_create_env_or_root_makefile(self):
        (self.root / 'projects.json').unlink()
        result = self.run_workflow('setup')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('No mailbox directory detected', result.stderr)
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
        (self.app / 'Makefile').write_text('doctor:\n\t@sh scripts/workflow.sh doctor\n')
        result = subprocess.run(['make', 'doctor'], cwd=special,
                                env=dict(self.env, MAILBOX_ROOT=str(self.root)),
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('OK: Compose configuration', result.stdout)

    def test_env_is_not_executed_as_shell(self):
        marker = self.root / 'must-not-exist'
        (self.app / '.env').write_text('MAILBOX_ROOT=$(touch "' + str(marker) + '")\n')
        result = self.run_workflow('doctor')
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(marker.exists())
