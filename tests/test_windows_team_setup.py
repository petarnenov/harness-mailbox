"""Exercise the shipped PowerShell launcher installer on a disposable Windows runner."""
import base64
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from uuid import uuid4


INSTALLER = Path(__file__).resolve().parents[1] / 'workspace/copilot-mailbox/install-windows-command.ps1'
IS_DISPOSABLE_WINDOWS = os.name == 'nt' and os.environ.get('TEAM_WINDOWS_DISPOSABLE_TEST') == '1'

# Windows PowerShell 5.1 itself writes these two subtrees into the empty redirected hive on runner image
# 20261123.321.1 (evidence: workflow run 46144728369); the installer does not write them.
WINDOWS_POWERSHELL_SUBTREES = (
    r'Software\Microsoft\Advanced INF Setup',
    r'Software\Microsoft\Windows\CurrentVersion\Internet Settings',
)


def registry_parts(path):
    return tuple(part.lower() for part in path.split('\\') if part)


def join_registry_path(*names):
    return '\\'.join(name for name in names if name)


def is_expected_registry_key(relative_path):
    parts = registry_parts(relative_path)
    if parts in ((), ('environment',)):
        return True
    for subtree in map(registry_parts, WINDOWS_POWERSHELL_SUBTREES):
        shorter = min(len(parts), len(subtree))
        if parts[:shorter] == subtree[:shorter]:
            return True
    return False


def is_registry_link(key):
    import winreg
    try:
        winreg.QueryValueEx(key, 'SymbolicLinkValue')
    except FileNotFoundError:
        return False
    return True


def child_key_names(key):
    import winreg
    names = []
    while True:
        try:
            names.append(winreg.EnumKey(key, len(names)))
        except OSError:
            return names


def read_registry_tree(root, relative_path=''):
    """Return the keys under root, children first, and the links; a link is listed and never entered."""
    import winreg
    path = join_registry_path(root, relative_path)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, winreg.REG_OPTION_OPEN_LINK, winreg.KEY_READ) as key:
        if is_registry_link(key):
            return [], [relative_path]
        names = child_key_names(key)
    keys, links = [], []
    for name in names:
        child_keys, child_links = read_registry_tree(root, join_registry_path(relative_path, name))
        keys += child_keys
        links += child_links
    return keys + [relative_path], links


def leads_to_link(relative_path, links):
    parts = registry_parts(relative_path)
    return any(registry_parts(link)[:len(parts)] == parts for link in links)


def remove_registry_keys(root, keys, links):
    import winreg
    for relative_path in keys:
        if not leads_to_link(relative_path, links):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, join_registry_path(root, relative_path))


# Redirect HKCU for this PowerShell process before invoking any installer code.
ISOLATED_POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class TestRegistry {
    [DllImport("advapi32.dll")]
    public static extern int RegOverridePredefKey(IntPtr key, IntPtr replacement);
}
'@
$hive = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($env:TEAM_TEST_HIVE, $true)
if ($null -eq $hive) { throw 'Missing test registry key' }
$currentUser = [IntPtr](-2147483647)
if ([TestRegistry]::RegOverridePredefKey($currentUser, $hive.Handle.DangerousGetHandle()) -ne 0) {
    throw 'Cannot redirect the test registry'
}
try {
    if ([Environment]::GetEnvironmentVariable('TEAM_TEST_ID', 'User') -cne $env:TEAM_TEST_HIVE) {
        throw 'Test registry isolation failed'
    }
    & $env:TEAM_TEST_INSTALLER
    $status = $LASTEXITCODE
} finally {
    [void][TestRegistry]::RegOverridePredefKey($currentUser, [IntPtr]::Zero)
    $hive.Dispose()
}
[Console]::WriteLine("SHIM_STATUS=$status")
exit $status
"""


@unittest.skipUnless(IS_DISPOSABLE_WINDOWS, 'requires a disposable Windows runner with Windows PowerShell')
class WindowsShimTests(unittest.TestCase):
    def setUp(self):
        import winreg
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.local = Path(temp.name)
        self.bin = self.local / 'HarnessMailbox/bin'
        self.shim = self.bin / 'team.cmd'
        self.key_name = 'HarnessMailboxTest_' + uuid4().hex
        self.env_key = self.key_name + r'\Environment'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.env_key) as key:
            winreg.SetValueEx(key, 'TEAM_TEST_ID', 0, winreg.REG_SZ, self.key_name)
        self.addCleanup(self.remove_own_registry_tree)
        self.original_path = self.user_path('Environment')
        self.addCleanup(lambda: self.assertEqual(self.user_path('Environment'), self.original_path))
        self.shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        self.encoded = base64.b64encode(ISOLATED_POWERSHELL.encode('utf-16le')).decode('ascii')

    def remove_own_registry_tree(self):
        keys, links = read_registry_tree(self.key_name)
        remove_registry_keys(self.key_name, keys, links)
        unexpected_keys = [key for key in keys if not is_expected_registry_key(key)]
        problems = []
        if links:
            problems.append(f'registry links left in place under {self.key_name}: {links}')
        if unexpected_keys:
            problems.append(f'unexpected registry keys under {self.key_name}: {unexpected_keys}')
        if problems:
            self.fail('; '.join(problems))

    @staticmethod
    def user_path(key_name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
                return winreg.QueryValueEx(key, 'Path')[0]
        except FileNotFoundError:
            return None

    def invoke(self, check, distro='Fixture Distro', user='fixture-account'):
        env = dict(os.environ, LOCALAPPDATA=str(self.local), HARNESS_TEAM_DISTRO=distro, HARNESS_TEAM_USER=user,
                   HARNESS_TEAM_CHECK='1' if check else '0', TEAM_TEST_HIVE=self.key_name,
                   TEAM_TEST_INSTALLER=str(INSTALLER))
        result = subprocess.run([self.shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-EncodedCommand', self.encoded], env=env, capture_output=True, timeout=30)
        self.assertIn(f'SHIM_STATUS={result.returncode}'.encode('ascii'), result.stdout,
                      'PowerShell did not reach the end of the isolated test driver')
        return result.returncode

    def test_check_is_read_only_and_setup_registers_the_correct_wsl_distribution(self):
        self.assertNotEqual(self.invoke(check=True), 0)
        self.assertFalse(self.shim.exists())
        self.assertIsNone(self.user_path(self.env_key))

        self.assertEqual(self.invoke(check=False), 0)
        original = self.shim.read_bytes()
        self.assertIn(b'--distribution "Fixture Distro"', original)
        self.assertIn(b'--user "fixture-account"', original)
        self.assertIn(b'--exec /usr/bin/env python3 .local/bin/team --from-windows', original)
        self.assertEqual(self.user_path(self.env_key), str(self.bin))
        self.assertEqual(self.invoke(check=True), 0)
        self.assertEqual(self.invoke(check=False), 0)
        self.assertEqual(self.user_path(self.env_key), str(self.bin))
        self.assertNotEqual(self.invoke(check=True, distro='Other Distro'), 0)
        self.assertNotEqual(self.invoke(check=True, user='other-account'), 0)
        self.assertNotEqual(self.invoke(check=False, user='other-account'), 0)
        self.assertEqual(self.shim.read_bytes(), original)

        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.env_key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, 'Path')
        self.assertNotEqual(self.invoke(check=True), 0)
        self.assertIsNone(self.user_path(self.env_key))
        self.assertEqual(self.shim.read_bytes(), original)
        self.assertEqual(self.invoke(check=False), 0)
        self.shim.write_bytes(b'@echo foreign command\r\n')
        self.assertNotEqual(self.invoke(check=True), 0)
        self.assertNotEqual(self.invoke(check=False), 0)
        self.assertEqual(self.shim.read_bytes(), b'@echo foreign command\r\n')

    def test_upgrading_a_generated_legacy_shim_pins_the_installing_account(self):
        self.bin.mkdir(parents=True)
        prefix = b'@echo off\r\n"%SystemRoot%\\System32\\wsl.exe" --distribution "Fixture Distro" --cd ~ --exec '
        for command in (b'/usr/bin/env python3 .local/bin/team --from-windows', b'/bin/sh -lc "exec ~/.local/bin/team"'):
            with self.subTest(command=command):
                legacy = prefix + command + b'\r\nexit /b %errorlevel%\r\n'
                self.shim.write_bytes(legacy)
                self.assertNotEqual(self.invoke(check=True), 0)
                self.assertEqual(self.shim.read_bytes(), legacy)
                self.assertEqual(self.invoke(check=False), 0)
                self.assertIn(b'--user "fixture-account"', self.shim.read_bytes())
                self.assertEqual(self.invoke(check=True), 0)
