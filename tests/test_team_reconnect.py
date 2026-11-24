"""Check the server and pane used for the orchestrator label on reconnect."""
import os
from pathlib import Path
import runpy
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch


CONTROL = Path(__file__).resolve().parents[1] / 'workspace/copilot-mailbox/team_control.py'


class ReconnectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = runpy.run_path(str(CONTROL))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sock = str(self.root / 'team socket')
        endpoint = socket.socket(socket.AF_UNIX)
        self.addCleanup(endpoint.close)
        endpoint.bind(self.sock)
        self.identity = '$7|100|123|200|||orchestrator|200'

    def test_reconnect_reads_only_the_recorded_local_socket(self):
        record = self.root / self.api['SOCKET_RECORD']
        self.assertIsNone(self.api['team_socket'](self.root))
        self.api['remember_socket'](self.root, self.sock)
        self.assertEqual(self.api['team_socket'](self.root), self.sock)
        for content, mode in [(b'', 0o600), (b'relative', 0o600), (b'/x\x00', 0o600),
                              (os.fsencode(record), 0o600), (os.fsencode(self.sock), 0o666)]:
            with self.subTest(content=content, mode=mode):
                record.write_bytes(content)
                record.chmod(mode)
                with self.assertRaisesRegex(ValueError, 'verify'):
                    self.api['team_socket'](self.root)

    def test_invalid_identity_cannot_probe_or_update_the_label(self):
        for pane, identity in [('%x', self.identity), ('%10', 'bad'),
                               ('%10', self.identity.replace('orchestrator', 'worker')),
                               ('%10', self.identity + '9')]:
            with self.subTest(pane=pane, identity=identity), patch('subprocess.run') as probe:
                with self.assertRaisesRegex(ValueError, 'connection changed'):
                    self.api['attach'](self.sock, pane, identity, focus_only=True)
                probe.assert_not_called()

    def test_focus_without_exec_checks_identity_even_when_label_lookup_fails(self):
        for response in (b'', b'The team connection changed.'):
            with self.subTest(response=response), patch('subprocess.run', side_effect=[
                    subprocess.TimeoutExpired('tmux', 2), subprocess.CompletedProcess([], 0, response)]) as run:
                if response:
                    with self.assertRaisesRegex(ValueError, 'connection changed'):
                        self.api['attach'](self.sock, '%10', self.identity, focus_only=True)
                else:
                    self.api['attach'](self.sock, '%10', self.identity, focus_only=True)
                argv = run.call_args.args[0]
                self.assertEqual(argv[:6], ['tmux', '-S', self.sock, 'if-shell', '-F', '-t'])
                self.assertIn(self.identity, argv[-3])
                self.assertIn('select-pane -t %10', argv[-2])
                self.assertNotIn('set-option', argv[-2])

    def test_nested_or_cross_server_reconnect_updates_only_the_observed_server(self):
        other = self.root / 'other'
        other.touch()
        for current, action in [(self.sock, 'switch-client'), (str(other), 'attach-session'),
                                (str(self.root / 'removed'), 'attach-session')]:
            with (self.subTest(current=current), patch.dict(os.environ, TMUX=current + ',1,2', TMUX_PANE='%99'),
                  patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '')) as probe,
                  patch('os.execvp') as connect):
                self.api['attach'](self.sock, '%10', self.identity)
                self.assertEqual(probe.call_args.args[0][:3], ['tmux', '-S', self.sock])
                argv = connect.call_args.args[1]
                self.assertEqual(argv[:3], ['tmux', '-S', self.sock])
                self.assertIn(action + " -t '$7'", argv[-2])
                self.assertEqual('TMUX_PANE' in os.environ, action == 'switch-client')
