"""System installer tests run only command doubles; no host packages are installed."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


FAKE_TOOL = r'''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
state_file = pathlib.Path(os.environ['INSTALL_STATE'])
state = json.loads(state_file.read_text())
with open(os.environ['INSTALL_LOG'], 'a') as log:
    log.write(json.dumps([name] + args) + '\n')
def save():
    state_file.write_text(json.dumps(state))
def install_team_packages():
    if 'install' in args and not state.get('runtime_install_failed'):
        if any(package in args for package in ('python', 'python3')):
            state['bad_python'] = False
        if 'tmux' in args:
            state['bad_tmux'] = False
        save()
if name == 'docker':
    if args == ['--version']: sys.exit(0 if state['cli'] else 1)
    if args == ['info']: sys.exit(0 if state['daemon'] else 1)
    if args[:2] == ['compose', 'version']: sys.exit(0 if state['compose'] else 1)
    if args[:1] == ['compose'] and '--help' in args:
        print('--environment --wait' if state['compose'] else '')
        sys.exit(0 if state['compose'] else 1)
    sys.exit(1)
elif name == 'uname':
    print(state['system'] if args == ['-s'] else state.get('arch', 'x86_64'))
elif name == 'id':
    print(state.get('uid', '0'))
elif name == 'make':
    sys.exit(1 if state.get('bad_make') else 0)
elif name == 'tmux':
    sys.exit(1 if state.get('bad_tmux') or args != ['-V'] else 0)
elif name == 'python3':
    if state.get('bad_python'):
        print('private runtime diagnostic', file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
elif name == 'brew':
    install_team_packages()
    if args == ['--prefix', 'make']:
        print(os.environ['FAKE_BREW_PREFIX'])
    if args == ['install', 'make']:
        binary = pathlib.Path(os.environ['FAKE_BREW_PREFIX']) / 'libexec/gnubin/make'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
    if args[:1] == ['list']: sys.exit(1)
    if args[-1:] == ['docker-desktop']:
        state.update(cli=True, compose=True)
        save()
elif name in ('apt-get', 'dnf'):
    install_team_packages()
    if 'make' in args:
        state['bad_make'] = False
        save()
elif name == 'open':
    if state.get('start_fail'): sys.exit(1)
    if not state.get('no_start'):
        state['daemon'] = True
        save()
elif name == 'systemctl':
    if args[:1] == ['is-active']: sys.exit(0 if state.get('active') else 1)
    if state.get('start_fail'): sys.exit(1)
    state['daemon'] = True
    save()
elif name == 'sudo':
    if state.get('sudo_fail'): sys.exit(1)
    os.execvp(args[0], args)
elif name == 'curl':
    if state.get('download_fail'): sys.exit(22)
    output = pathlib.Path(args[args.index('--output') + 1])
    url = next(x for x in args if x.startswith('https://'))
    if 'docker-compose-linux-' in url:
        output.write_text('#!/bin/sh\nprintf "Docker Compose test plugin\\n"\n')
        state['compose'] = True
        save()
    else:
        output.write_text('#!/bin/sh\napply-install\n')
elif name == 'apply-install':
    state.update(cli=True, compose=True)
    save()
'''


class SystemSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.state = self.root / 'state.json'
        self.log = self.root / 'log.jsonl'
        self.script = Path(__file__).resolve().parents[1] / 'scripts/install-system.sh'
        for name in ('docker', 'make', 'uname', 'id', 'brew', 'open', 'systemctl', 'sudo', 'curl',
                     'apply-install', 'apt-get', 'dnf', 'python3', 'tmux'):
            tool = self.bin / name
            tool.write_text('#!' + sys.executable + '\n' + FAKE_TOOL)
            tool.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        DOCKER=str(self.bin / 'docker'), INSTALL_STATE=str(self.state), INSTALL_LOG=str(self.log),
                        DOCKER_CONFIG=str(self.root / 'docker-config'), DOCKER_WAIT_SECONDS='0',
                        FAKE_BREW_PREFIX=str(self.root / 'brew-make'))
        self.env.pop('WSL_DISTRO_NAME', None)

    def run_setup(self, **state):
        data = dict(cli=True, compose=True, daemon=True, system='Linux')
        data.update(state)
        self.state.write_text(json.dumps(data))
        return subprocess.run(['/bin/sh', str(self.script)], env=self.env,
                              text=True, capture_output=True, timeout=20)

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def test_working_system_is_noop(self):
        result = self.run_setup()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('no installation needed', result.stdout)
        self.assertTrue(all(x[0] in ('docker', 'make', 'python3', 'tmux') for x in self.calls()))

    def test_team_dependencies_are_installed_even_when_docker_is_healthy(self):
        for platform, manager, python_package in [('Darwin', 'brew', 'python'), ('Linux', 'apt-get', 'python3'),
                                                   ('WSL', 'apt-get', 'python3'), ('Linux', 'dnf', 'python3')]:
            with self.subTest(platform=platform, manager=manager):
                self.log.unlink(missing_ok=True)
                if platform == 'WSL':
                    self.env['WSL_DISTRO_NAME'] = 'Fixture Distro'
                else:
                    self.env.pop('WSL_DISTRO_NAME', None)
                if manager == 'dnf':
                    (self.bin / 'apt-get').unlink()
                    self.env['PATH'] = str(self.bin)
                    (self.bin / 'sh').symlink_to(shutil.which('sh'))
                result = self.run_setup(system='Darwin' if platform == 'Darwin' else 'Linux',
                                        bad_python=True, bad_tmux=True)
                self.assertEqual(0, result.returncode, result.stderr)
                calls = self.calls()
                packages = {arg for call in calls if call[:2] == [manager, 'install'] for arg in call[2:]}
                self.assertTrue({python_package, 'tmux'} <= packages, calls)
                self.assertFalse(any(call[0] in ('open', 'curl', 'apply-install', 'systemctl') for call in calls))

    def isolated_wsl(self):
        self.env['WSL_DISTRO_NAME'] = 'Fixture Distro'
        self.env['PATH'] = str(self.bin)
        for tool in ('sh', 'mktemp', 'rm'):
            (self.bin / tool).symlink_to(shutil.which(tool))

    def test_wsl_with_desktop_reports_missing_integration_without_installing_a_second_engine(self):
        self.isolated_wsl()
        (self.bin / 'docker.exe').symlink_to(self.bin / 'docker')
        result = self.run_setup(cli=False, compose=False, daemon=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Docker Desktop', result.stderr)
        self.assertFalse(any(call[0] in ('curl', 'apply-install', 'systemctl') for call in self.calls()))

    @unittest.skipIf(Path('/mnt/wsl/docker-desktop').is_dir(), 'host has an active WSL Docker Desktop mount')
    def test_wsl_without_desktop_installs_and_starts_a_linux_engine(self):
        self.isolated_wsl()
        result = self.run_setup(cli=False, compose=False, daemon=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(['apply-install'], self.calls())
        self.assertIn(['systemctl', 'start', 'docker'], self.calls())

    def test_an_unusable_runtime_after_installation_stops_setup_with_a_fixed_diagnostic(self):
        result = self.run_setup(bad_python=True, runtime_install_failed=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Python', result.stderr)
        self.assertNotIn('private runtime diagnostic', result.stdout + result.stderr)

    def test_macos_installs_missing_desktop_then_starts_it(self):
        result = self.run_setup(cli=False, compose=False, daemon=False, system='Darwin')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(['brew', 'install', '--cask', 'docker-desktop'], self.calls())
        self.assertTrue(any(x[0] == 'open' for x in self.calls()))
        self.assertFalse(any(x[0] in ('sudo', 'apt-get', 'dnf') for x in self.calls()))

    def test_macos_unusable_make_is_installed_with_working_gnubin(self):
        result = self.run_setup(system='Darwin', bad_make=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(['brew', 'install', 'make'], self.calls())
        self.assertTrue((self.root / 'brew-make/libexec/gnubin/make').exists())
        self.assertIn('shell PATH', result.stdout)
        self.assertFalse(any('docker-desktop' in x for x in self.calls()))

    def test_linux_installs_docker_as_root_and_starts_daemon(self):
        result = self.run_setup(cli=False, compose=False, daemon=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(any('https://get.docker.com' in x for x in self.calls()))
        self.assertIn(['apply-install'], self.calls())
        self.assertIn(['systemctl', 'start', 'docker'], self.calls())
        self.assertFalse(any(x[0] == 'sudo' for x in self.calls()))

    def test_linux_installs_unusable_make_without_replacing_docker(self):
        result = self.run_setup(bad_make=True, uid='1000')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(['apt-get', 'install', '-y', '--no-install-recommends', 'make'], self.calls())
        self.assertTrue(any(x[0] == 'sudo' for x in self.calls()))
        self.assertFalse(any(x[0] in ('curl', 'apply-install', 'systemctl') for x in self.calls()))

    def test_linux_only_missing_compose_does_not_replace_engine(self):
        result = self.run_setup(compose=False)
        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.calls()
        self.assertTrue(any(any('docker-compose-linux-x86_64' in a for a in x) for x in calls))
        self.assertFalse(any('https://get.docker.com' in x for x in calls))
        self.assertFalse(any(x[0] in ('sudo', 'apt-get', 'dnf', 'apply-install', 'systemctl') for x in calls))
        self.assertTrue((self.root / 'docker-config/cli-plugins/docker-compose').is_file())

    def test_download_failure_never_executes_installer(self):
        result = self.run_setup(cli=False, compose=False, daemon=False, download_fail=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('download failed', result.stderr)
        self.assertFalse(any(x[0] == 'apply-install' for x in self.calls()))

    def test_failed_privilege_escalation_is_actionable(self):
        result = self.run_setup(cli=False, compose=False, daemon=False, uid='1000', sudo_fail=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Administrator command failed', result.stderr)
        self.assertFalse(any(x[0] == 'apply-install' for x in self.calls()))

    def test_start_failure_and_timeout_are_nonzero(self):
        result = self.run_setup(daemon=False, system='Darwin', start_fail=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('could not start', result.stderr)
        result = self.run_setup(daemon=False, system='Darwin', no_start=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Docker is not ready', result.stderr)

    def test_unusable_existing_daemon_does_not_change_permissions(self):
        result = self.run_setup(daemon=False, active=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('socket permissions were not changed', result.stderr)
        self.assertNotIn(['systemctl', 'start', 'docker'], self.calls())

    def test_unsupported_os_and_architecture_fail_before_download(self):
        result = self.run_setup(cli=False, system='Plan9')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('supports macOS and Linux', result.stderr)
        result = self.run_setup(compose=False, arch='unsupported-cpu')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Unsupported Compose CPU architecture', result.stderr)
        self.assertFalse(any(x[0] == 'curl' for x in self.calls()))
