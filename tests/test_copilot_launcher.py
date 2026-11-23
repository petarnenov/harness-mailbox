"""Exercise the installed launcher without Copilot credentials or a live mailbox."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import select
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


LAUNCHER = Path(__file__).resolve().parents[1] / 'workspace' / 'copilot-mailbox' / 'start'
FAKE_COPILOT = '''#!/usr/bin/env python3
import json, os, sys
keys = ('HARNESS_MAILBOX_ROOT', 'HARNESS_MAILBOX_CWD', 'HARNESS_MAILBOX_ROUTE', 'HARNESS_MAILBOX_WORKER_NUMBER')
print(json.dumps({'executable': os.path.basename(sys.argv[0]), 'cwd': os.getcwd(), 'options': sys.argv[1:], 'env': {key: os.environ.get(key) for key in keys}}), flush=True)
if os.environ.get('TEST_KEEP_COPILOT_OPEN'):
    sys.stdin.readline()
'''


class CopilotLauncherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.root = base / 'mailbox with spaces'
        self.project = base / 'sample project'
        self.project.mkdir()
        plugin = self.root / 'copilot-mailbox'
        shutil.copytree(LAUNCHER.parent, plugin)
        self.launcher = plugin / 'start'
        (self.root / 'projects.json').write_text(json.dumps({'projects': [
            {'id': 'sample', 'working_directory': str(self.project), 'repository_path': str(self.project)},
        ]}), encoding='utf-8')
        binary = base / 'copilot'
        binary.write_text(FAKE_COPILOT, encoding='utf-8')
        binary.chmod(0o700)
        self.env = {**os.environ, 'PATH': f"{base}{os.pathsep}{os.environ['PATH']}"}

    def launch(self, *args):
        return subprocess.run([self.launcher, *args], env=self.env, capture_output=True,
                              text=True, timeout=5, check=False)

    def test_worker_uses_registered_checkout_and_preserves_model_selection(self):
        result = self.launch('worker', 'sample', '--model', 'auto')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['env'], {
            'HARNESS_MAILBOX_ROOT': str(self.root), 'HARNESS_MAILBOX_CWD': str(self.project),
            'HARNESS_MAILBOX_ROUTE': 'sample', 'HARNESS_MAILBOX_WORKER_NUMBER': '1',
        })
        for value in ('--experimental', 'mailbox-worker', str(self.root / 'copilot-mailbox'), '--model', 'auto'):
            self.assertIn(value, report['options'])
        self.assertNotIn('--allow-all', report['options'])
        self.assertNotIn('--autopilot', report['options'])
        self.assertNotIn('-i', report['options'])

    def test_worker_number_identifies_each_host_without_changing_project_or_isolating_its_inbox(self):
        for host in ('copilot', 'codex', 'claude'):
            with self.subTest(host=host):
                binary = self.root.parent / host
                binary.write_text(FAKE_COPILOT)
                binary.chmod(0o700)
                result = self.launch('worker', 'sample', '--worker-number', '2', '--harness', host)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report['cwd'], str(self.project))
                self.assertEqual(report['env']['HARNESS_MAILBOX_ROUTE'], 'sample')
                self.assertEqual(report['env']['HARNESS_MAILBOX_WORKER_NUMBER'], '2')
                self.assertNotIn('--worker-number', report['options'])
                self.assertTrue((self.root / 'inbox/sample').is_dir())
                self.assertFalse((self.root / 'inbox/sample+2').exists())

    def test_invalid_worker_numbers_never_launch_or_echo_raw_input(self):
        for number in ('0', '-2', 'synthetic-private-detail'):
            with self.subTest(number=number):
                result = self.launch('worker', 'sample', '--worker-number', number)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn('synthetic-private-detail', result.stderr)

    def test_coordinator_and_workers_receive_the_exact_model_and_effort(self):
        for role in (('orchestrator', '--project', 'sample'), ('worker', 'sample')):
            with self.subTest(role=role):
                report = self.launch(*role, '--model', 'sample-model-2.1', '--reasoning-effort', 'high')
                self.assertEqual(report.returncode, 0, report.stderr)
                argv = json.loads(report.stdout)['options']
                self.assertEqual(argv[argv.index('--model') + 1], 'sample-model-2.1')
                self.assertEqual(argv[argv.index('--reasoning-effort') + 1], 'high')

    def test_native_participants_use_their_own_checkout_model_effort_and_mailbox_scope(self):
        for host in ('codex', 'claude'):
            binary = self.root.parent / host
            binary.write_text(FAKE_COPILOT, encoding='utf-8')
            binary.chmod(0o700)
            for role in ('worker', 'orchestrator'):
                with self.subTest(host=host, role=role):
                    prefix = (role, 'sample') if role == 'worker' else (role, '--project', 'sample')
                    result = self.launch(*prefix, '--harness', host, '--model', 'native-model',
                                         '--reasoning-effort', 'high')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual(report['executable'], host)
                    self.assertEqual(report['cwd'], str(self.project if role == 'worker' else self.root))
                    argv = report['options']
                    self.assertEqual(argv[argv.index('--model') + 1], 'native-model')
                    if host == 'codex':
                        self.assertNotIn('--add-dir', argv)
                        self.assertNotIn('--sandbox', argv)
                        self.assertIn('--no-daemon', argv)
                        self.assertIn('model_reasoning_effort="high"', argv)
                    else:
                        self.assertEqual(argv[argv.index('--add-dir') + 1],
                                         str(self.root if role == 'worker' else self.project))
                        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
                    self.assertNotIn('--dangerously-skip-permissions', argv)
                    self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', argv)
                    self.assertNotIn('--allow-all', argv)

    def test_native_defaults_and_objective_preserve_role_bootstrap_at_handoff(self):
        main = runpy.run_path(str(LAUNCHER))['main']
        for host, role in [('claude', 'orchestrator'), ('codex', 'worker')]:
            options = (['--project', 'sample', '--objective', 'Review the sample']
                       if role == 'orchestrator' else ['sample'])
            args = [str(self.launcher), role, *options, '--harness', host]
            if role == 'worker':
                args.extend(['--model', 'dev', '--reasoning-effort', 'low'])
            with (self.subTest(host=host), patch.dict(main.__globals__, __file__=str(self.launcher)),
                  patch('sys.argv', args), patch.dict(os.environ, {}), patch('os.chdir'),
                  patch('os.set_inheritable', wraps=os.set_inheritable) as inherit,
                  patch('os.execvp') as execute):
                try:
                    main()
                    executable, argv = execute.call_args.args
                    self.assertEqual(executable, host)
                    self.assertIn('HARNESS_MAILBOX_ROOT', argv[-1])
                    if role == 'orchestrator':
                        self.assertIn('Review the sample', argv[-1])
                        self.assertIn('60', argv[-1])
                        for instruction in ('status', 'pause', 'stop', 'status-only'):
                            self.assertIn(instruction, argv[-1].lower())
                        self.assertNotIn('--model', argv)
                        self.assertNotIn('--effort', argv)
                    else:
                        self.assertIn('dispatch_task.py', argv[-1])
                        self.assertIn('model_reasoning_effort="low"', argv)
                finally:
                    if inherit.called:
                        os.close(inherit.call_args.args[0])

    def test_copilot_only_options_are_rejected_for_other_harnesses(self):
        for host in ('claude', 'codex'):
            result = self.launch('worker', 'sample', '--harness', host, '--autopilot')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertIn('copilot', result.stderr)

    def test_unknown_project_and_path_escape_do_not_launch_copilot(self):
        for args in [('worker', '../other'), ('worker', 'missing'), ('worker',), ('sample',)]:
            with self.subTest(args=args):
                result = self.launch(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('HARNESS_MAILBOX_ROOT', result.stdout)
                self.assertNotIn('Traceback', result.stderr)

    def test_only_one_coordinator_consumes_this_mailbox_until_it_exits(self):
        command = [self.launcher, 'orchestrator']
        env = {**self.env, 'TEST_KEEP_COPILOT_OPEN': '1'}
        with subprocess.Popen(command, env=env, text=True, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as first:
            try:
                ready, _, _ = select.select([first.stdout], [], [], 5)
                self.assertTrue(ready, 'first coordinator did not reach Copilot')
                report = json.loads(first.stdout.readline())
                self.assertEqual(report['env']['HARNESS_MAILBOX_CWD'], str(self.root))
                self.assertEqual(report['env']['HARNESS_MAILBOX_ROUTE'], 'orchestrator')
                self.assertIn('mailbox-orchestrator', report['options'])
                self.assertNotIn('--autopilot', report['options'])
                self.assertNotIn('-i', report['options'])
                blocked = self.launch('orchestrator')
                self.assertNotEqual(blocked.returncode, 0)
                self.assertNotIn('HARNESS_MAILBOX_ROOT', blocked.stdout)
                first.communicate('\n', timeout=5)
                self.assertEqual(first.returncode, 0)
            finally:
                first.kill()
        replacement = self.launch('orchestrator')
        self.assertEqual(replacement.returncode, 0, replacement.stderr)

    def test_supported_cli_options_reach_the_worker_without_widening_permissions(self):
        namespace = runpy.run_path(str(LAUNCHER))
        main = namespace['main']
        session = '10000000-0000-4000-8000-000000000001'
        argv = [str(self.launcher), 'worker', 'sample', '--resume', session, '--model', 'auto',
                '--available-tools', 'read', '--allow-tool', 'read', '--disable-builtin-mcps', '--sync-session', 'off',
                '--autopilot']
        with (patch.dict(main.__globals__, __file__=str(self.launcher)), patch('sys.argv', argv),
              patch.dict(os.environ, {}), patch('os.chdir') as chdir, patch('os.execvp') as execute):
            main()
        chdir.assert_called_once_with(self.project)
        executable, args = execute.call_args.args
        self.assertEqual(executable, 'copilot')
        for flag, value in [('-C', str(self.project)), ('--agent', 'mailbox-worker'),
                            ('--plugin-dir', str(self.launcher.parent)), ('--add-dir', str(self.root)),
                            ('--model', 'auto'), ('--resume', session), ('--available-tools', 'read'),
                            ('--allow-tool', 'read')]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn('--sync-session=off', args)
        self.assertIn('--disable-builtin-mcps', args)
        self.assertIn('--autopilot', args)
        self.assertNotIn('--allow-all-tools', args)
        self.assertNotIn('--allow-all', args)

    def test_legacy_registry_shapes_resolve_only_a_single_matching_project(self):
        resolve = runpy.run_path(str(LAUNCHER))['working_directory']
        data = {'id': 'sample', 'working_directory': str(self.project)}
        for entries in ([data], {'projects': [data]}, {'sample': {'working_directory': str(self.project)}}):
            self.assertEqual(resolve(entries, 'sample'), self.project)
        for entries in ([data, data], [dict(data, working_directory='relative/path')]):
            with self.assertRaises(ValueError):
                resolve(entries, 'sample')

    def test_cli_start_error_does_not_leak_the_failed_executable_or_path(self):
        main = runpy.run_path(str(LAUNCHER))['main']
        errors = io.StringIO()
        with (patch.dict(main.__globals__, __file__=str(self.launcher)),
              patch('sys.argv', [str(self.launcher), 'worker', 'sample']), patch.dict(os.environ, {}),
              patch('os.chdir'), patch('os.execvp', side_effect=OSError('untrusted operating-system detail')),
              contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as stopped):
            main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertIn('Copilot CLI', errors.getvalue())
        self.assertNotIn('operating-system detail', errors.getvalue())

    def test_coordinator_lock_and_objective_survive_handoff_to_the_cli(self):
        main = runpy.run_path(str(LAUNCHER))['main']
        argv = [str(self.launcher), 'orchestrator', '--objective', 'Check the sample result']
        with (patch.dict(main.__globals__, __file__=str(self.launcher)), patch('sys.argv', argv),
              patch.dict(os.environ, {}), patch('os.chdir'), patch('os.execvp') as execute,
              patch('os.open', wraps=os.open) as opened, patch('os.set_inheritable', wraps=os.set_inheritable) as inherit):
            main()
        fd, inherited = inherit.call_args.args
        try:
            self.assertTrue(inherited)
            self.assertTrue(os.get_inheritable(fd))
            self.assertTrue(opened.call_args.args[1] & os.O_NOFOLLOW)
            args = execute.call_args.args[1]
            self.assertEqual(args[args.index('-i') + 1], 'Check the sample result')
            self.assertNotIn('--add-dir', args)
        finally:
            os.close(fd)

    def test_invalid_resume_and_missing_registry_fail_without_running_the_cli(self):
        (self.root / 'projects.json').unlink()
        for options in [('orchestrator',), ('worker', 'orchestrator'), ('worker', 'sample', '--resume', 'not-a-uuid')]:
            result = self.launch(*options)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('HARNESS_MAILBOX_ROOT', result.stdout)
            self.assertNotIn('Traceback', result.stderr)

    def test_unrelated_malformed_entries_do_not_hide_a_valid_dotted_project_id(self):
        route = 'Sample.v2'
        (self.root / 'projects.json').write_text(json.dumps({'projects': [
            None, {'id': '../invalid'}, {'id': 'duplicate'}, {'id': 'duplicate'},
            {'id': route, 'working_directory': str(self.project)},
        ]}), encoding='utf-8')
        result = self.launch('worker', route)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['env']['HARNESS_MAILBOX_ROUTE'], route)

    def test_neither_role_launches_against_a_symlinked_nonregular_or_oversized_registry(self):
        registry = self.root / 'projects.json'
        external = self.project / 'untrusted.json'
        registry.rename(external)
        for kind in ('symlink', 'oversized', 'fifo', 'directory'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    registry.symlink_to(external)
                elif kind == 'oversized':
                    registry.write_text(external.read_text(encoding='utf-8') + ' ' * (4 * 1024 * 1024), encoding='utf-8')
                elif kind == 'fifo':
                    os.mkfifo(registry)
                else:
                    registry.mkdir()
                for role in [('worker', 'sample'), ('orchestrator',)]:
                    result = self.launch(*role)
                    self.assertNotEqual(result.returncode, 0, f'{kind}: {role}')
                    self.assertNotIn('HARNESS_MAILBOX_ROOT', result.stdout)
                    self.assertNotIn('Traceback', result.stderr)
                    self.assertNotIn(str(external), result.stderr)
                if kind == 'directory':
                    registry.rmdir()
                else:
                    registry.unlink()

    def test_worker_start_creates_missing_routes_before_copilot_joins(self):
        expected = ['projects', 'sessions', 'inbox/sample', 'outbox/sample', 'archive/sample',
                    'tasks/sample', 'inbox/orchestrator', 'outbox/orchestrator', 'archive/orchestrator']
        self.assertTrue(all(not (self.root / path).exists() for path in expected))
        result = self.launch('worker', 'sample')
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in expected:
            self.assertTrue((self.root / path).is_dir(), path)
        marker = self.root / 'outbox' / 'sample' / 'existing-record'
        marker.write_bytes(b'existing record data')
        self.assertEqual(self.launch('worker', 'sample').returncode, 0)
        self.assertEqual(marker.read_bytes(), b'existing record data')

    def test_coordinator_can_start_first_in_a_bare_mailbox(self):
        expected = ['projects', 'sessions', 'inbox/orchestrator', 'outbox/orchestrator', 'archive/orchestrator']
        self.assertTrue(all(not (self.root / path).exists() for path in expected))
        result = self.launch('orchestrator')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all((self.root / path).is_dir() for path in expected))

    def test_bootstrap_rejects_symlinked_route_ancestors_and_leaves_for_both_roles(self):
        outside = self.project / 'external'
        outside.mkdir()
        marker = outside / 'original'
        marker.write_bytes(b'original data')
        common = self.root / 'inbox'
        for role, component in [(('worker', 'sample'), 'sample'), (('orchestrator',), 'orchestrator')]:
            for depth in ('ancestor', 'leaf'):
                with self.subTest(role=role, depth=depth):
                    if depth == 'ancestor':
                        link = common
                    else:
                        common.mkdir(exist_ok=True)
                        link = common / component
                    link.symlink_to(outside, target_is_directory=True)
                    try:
                        result = self.launch(*role)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, '')
                        self.assertIn('mailbox', result.stderr)
                        self.assertNotIn(str(outside), result.stderr)
                        self.assertNotIn('Traceback', result.stderr)
                        self.assertEqual(list(outside.iterdir()), [marker])
                        self.assertEqual(marker.read_bytes(), b'original data')
                    finally:
                        link.unlink()
                        if common.exists():
                            shutil.rmtree(common)

    def test_orchestrator_adds_only_explicit_registry_projects_and_keeps_permission_rules(self):
        other = self.project.parent / 'other project'
        other.mkdir()
        hidden = self.project.parent / 'unused project'
        hidden.mkdir()
        registry = {'projects': [
            {'id': 'sample', 'working_directory': str(self.project)},
            {'id': 'other', 'working_directory': str(other)},
            {'id': 'unused', 'working_directory': str(hidden)},
        ]}
        (self.root / 'projects.json').write_text(json.dumps(registry), encoding='utf-8')
        default = self.launch('orchestrator')
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertNotIn('--add-dir', json.loads(default.stdout)['options'])
        scoped = self.launch('orchestrator', '--project', 'sample', '--project', 'other', '--allow-tool', 'read')
        self.assertEqual(scoped.returncode, 0, scoped.stderr)
        args = json.loads(scoped.stdout)['options']
        self.assertCountEqual([value for flag, value in zip(args, args[1:]) if flag == '--add-dir'],
                              [str(self.project), str(other)])
        self.assertEqual([value for flag, value in zip(args, args[1:]) if flag == '--allow-tool'], ['read'])
        self.assertNotIn(str(hidden), args)
        for broad in ('--allow-all', '--allow-all-tools', '--allow-all-paths', '--allow-all-urls'):
            self.assertNotIn(broad, args)
        for unknown in ('missing', str(hidden)):
            rejected = self.launch('orchestrator', '--project', 'sample', '--project', unknown)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(rejected.stdout, '')
            self.assertNotIn('Traceback', rejected.stderr)
