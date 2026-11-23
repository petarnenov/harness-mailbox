"""Exercise the per-user team command installer in an isolated shell environment."""
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1] / "workspace"


class TeamInstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.home = root / "user home"
        self.mailbox = root / "team mailbox"
        self.cwd = root / "unrelated"
        self.tools = root / "programs"
        for folder in (self.home, self.mailbox, self.cwd, self.tools):
            folder.mkdir()
        self.helper = self.mailbox / "copilot-mailbox"
        self.helper.mkdir()
        self.script = self.mailbox / "install-team.sh"
        self.link = self.home / ".local/bin/team"
        shutil.copy2(WORKSPACE / "install-team.sh", self.script)
        shutil.copy2(WORKSPACE / "team", self.mailbox / "team")
        for name in ("team_ui_data.py", "team_control.py", "install-windows-command.ps1", "team", "start"):
            shutil.copy2(WORKSPACE / "copilot-mailbox" / name, self.helper / name)
        (self.mailbox / "projects.json").write_text('{"projects": []}')
        (self.tools / "python3").symlink_to(sys.executable)
        for name in ("cat", "dirname", "grep", "ln", "mkdir"):
            (self.tools / name).symlink_to(shutil.which(name))
        account = self.tools / "id"
        account.write_text('#!/bin/sh\n[ "$1" = "-un" ] || exit 1\nprintf "fixture-account\\n"\n')
        account.chmod(0o755)
        self.env = {"PATH": str(self.tools), "HOME": str(self.home), "SHELL": "/bin/sh",
                    "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C"}

    def shell(self, *args):
        return subprocess.run(["/bin/sh", *args], cwd=self.cwd, env=self.env,
                              capture_output=True, text=True, timeout=10)

    def test_repeated_install_preserves_profile_and_starts_team_from_another_directory(self):
        profile = self.home / ".profile"
        profile.write_text("# existing shell configuration\n")
        self.assertEqual(self.shell(self.script).returncode, 0)
        first_install = profile.read_bytes()
        self.assertTrue(first_install.startswith(b"# existing shell configuration\n"))
        self.assertTrue(self.link.samefile(self.mailbox / "team"))
        self.assertEqual(self.shell(self.script).returncode, 0)
        self.assertEqual(profile.read_bytes(), first_install)
        call = self.shell("-c", '. "$HOME/.profile" && team')
        self.assertEqual(call.returncode, 1)
        self.assertIn("Run team in an interactive terminal.", call.stderr)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS shell and Homebrew runtime paths")
    def test_fresh_zsh_finds_team_and_the_runtime_that_setup_found_in_homebrew(self):
        for brew_bin in (Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
            for program in ("tmux", "python3"):
                executable = brew_bin / program
                if os.access(executable, os.X_OK):
                    break
            else:
                continue
            break
        else:
            self.skipTest("no team runtime in the macOS package-manager paths")
        if program == "python3":
            (self.tools / "python3").unlink()
        self.env.update(SHELL="/bin/zsh", ZDOTDIR=str(self.home),
                        TEAM_SHELL_PATH=str(self.tools), PATH=os.pathsep.join([str(self.tools), str(brew_bin)]))
        installed = self.shell(self.script)
        self.assertEqual(installed.returncode, 0, installed.stderr)

        self.env["PATH"] = self.env["TEAM_SHELL_PATH"]
        self.assertNotEqual(self.shell(WORKSPACE / "install-team.sh", "--check", self.mailbox).returncode, 0)
        fresh = subprocess.run(
            ["/bin/zsh", "-di", "-c", 'command -v team && command -v "$1" && /bin/sh "$2" --check "$3"',
             "zsh", program, WORKSPACE / "install-team.sh", self.mailbox],
            env=self.env, cwd=self.cwd, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(fresh.stdout.splitlines(), [str(self.link), str(executable), "OK: team launcher and PATH"])

    def test_windows_entry_starts_the_account_shell_with_its_interactive_cli_path(self):
        # Model the WSL account's login shell and its existing shell-specific startup files.
        (self.home / ".bash_profile").write_text('. "$HOME/.bashrc"\n')
        (self.home / ".bashrc").write_text(
            'case "$-" in *i*) export PATH="$HOME/interactive tools:$PATH";; esac\n'
        )
        programs = self.home / "interactive tools"
        programs.mkdir()
        copilot = programs / "copilot"
        copilot.write_text("#!/bin/sh\n")
        copilot.chmod(0o755)
        self.link.parent.mkdir(parents=True)
        self.link.write_text('#!/bin/sh\ncommand -v copilot > "$HOME/resolved"\n')
        self.link.chmod(0o755)
        script = (
            "import runpy, sys; from types import SimpleNamespace; from unittest.mock import patch; "
            "sys.argv = sys.argv[1:]; "
            "patch('pwd.getpwuid', return_value=SimpleNamespace(pw_shell='/bin/bash')).start(); "
            "runpy.run_path(sys.argv[0], run_name='__main__')"
        )
        master, terminal = pty.openpty()
        try:
            call = subprocess.run([sys.executable, "-c", script, self.mailbox / "team", "--from-windows"],
                                  cwd=self.cwd, env=self.env, stdin=terminal, stdout=terminal,
                                  stderr=subprocess.PIPE, text=True, timeout=10)
        finally:
            os.close(terminal)
            os.close(master)
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertEqual((self.home / "resolved").read_text().strip(), str(copilot))

    def test_conflict_preserves_existing_command_and_profile(self):
        self.link.parent.mkdir(parents=True)
        self.link.write_text("existing local command\n")
        profile = self.home / ".profile"
        profile.write_text("# keep\n")
        result = self.shell(self.script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("team", result.stderr)
        self.assertEqual(self.link.read_text(), "existing local command\n")
        self.assertEqual(profile.read_text(), "# keep\n")

    def test_missing_or_non_executable_participant_launcher_fails_before_install(self):
        for name in ("team", "start"):
            with self.subTest(name=name):
                launcher = self.helper / name
                launcher.chmod(0o600)
                result = self.shell(self.script)
                launcher.chmod(0o700)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.link.is_symlink())
                self.assertFalse((self.home / ".profile").exists())

    def test_unwritable_shell_profile_returns_only_the_installer_diagnostic(self):
        (self.home / ".profile").mkdir()
        result = self.shell(self.script)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), "Cannot update your shell profile.")

    def test_fish_outside_path_uses_the_configured_shell_and_keeps_errors_local(self):
        fish = self.home / "fish"
        fish.write_text('#!/bin/sh\n[ "$2" = \'fish_add_path "$HOME/.local/bin"\' ] || exit 9\n'
                        'echo private-fixture >&2\nexit "$FISH_STATUS"\n')
        fish.chmod(0o700)
        self.env["SHELL"] = str(fish)
        for status in ("0", "1"):
            with self.subTest(status=status):
                self.env["FISH_STATUS"] = status
                result = self.shell(self.script)
                self.assertEqual(result.returncode, int(status))
                self.assertNotIn("private-fixture", result.stderr)
                self.assertNotIn(str(fish), result.stderr)

    def test_wsl_passes_distribution_and_installer_to_powershell_and_reports_failure(self):
        powershell = self.tools / "powershell.exe"
        powershell.write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$HARNESS_TEAM_DISTRO" "$WSLENV" "$HARNESS_TEAM_USER" "$@" > "$HOME/invocation"\n'
            'cat > "$HOME/input"\n'
            'printf "private fixture error\\n" >&2\n'
            'exit "$MOCK_STATUS"\n'
        )
        powershell.chmod(0o755)
        self.env.update(WSL_DISTRO_NAME="Example Linux", WSLENV="EXISTING/u")
        for status in ("0", "8"):
            with self.subTest(status=status):
                self.env["MOCK_STATUS"] = status
                result = self.shell(self.script)
                self.assertEqual(result.returncode == 0, status == "0")
                args = (self.home / "invocation").read_text().splitlines()
                self.assertEqual(args[0], "Example Linux")
                self.assertIn("HARNESS_TEAM_DISTRO/w", args[1].split(":"))
                self.assertIn("EXISTING/u", args[1].split(":"))
                self.assertIn("HARNESS_TEAM_USER/w", args[1].split(":"))
                self.assertEqual(args[2], "fixture-account")
                self.assertEqual(args[-2:], ["-Command", "-"])
                self.assertEqual((self.home / "input").read_bytes(),
                                 (self.helper / "install-windows-command.ps1").read_bytes())
                if status != "0":
                    self.assertIn("Windows", result.stderr)
                    self.assertNotIn("private fixture error", result.stderr)

    def test_wsl_cannot_register_an_unknown_linux_account(self):
        (self.tools / "id").write_text('#!/bin/sh\nprintf "fixture-private-error\\n" >&2\nexit 1\n')
        powershell = self.tools / "powershell.exe"
        powershell.write_text('#!/bin/sh\n: > "$HOME/invoked"\n')
        powershell.chmod(0o755)
        self.env["WSL_DISTRO_NAME"] = "Fixture Distro"
        result = self.shell(self.script)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), "Cannot identify the Linux account that owns team.")
        self.assertFalse((self.home / "invoked").exists())

    def test_wsl_check_requests_read_only_windows_validation_and_propagates_the_result(self):
        self.assertEqual(self.shell(self.script).returncode, 0)
        self.env.update(WSL_DISTRO_NAME="Fixture Distro", WSLENV="EXISTING/u",
                        PATH=os.pathsep.join([str(self.link.parent), str(self.tools)]))
        powershell = self.tools / "powershell.exe"
        powershell.write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$HARNESS_TEAM_CHECK" "$HARNESS_TEAM_DISTRO" "$WSLENV" > "$HOME/invocation"\n'
            'cat > "$HOME/input"\n'
            'printf "private check error\\n" >&2\n'
            'exit "$MOCK_STATUS"\n'
        )
        powershell.chmod(0o755)
        profile = self.home / ".profile"
        before = profile.read_bytes()
        for status in ("0", "1"):
            with self.subTest(status=status):
                self.env["MOCK_STATUS"] = status
                result = self.shell(self.script, "--check")
                self.assertEqual(result.returncode == 0, status == "0", result.stderr)
                mode, distro, exported = (self.home / "invocation").read_text().splitlines()
                self.assertEqual(mode, "1")
                self.assertEqual(distro, "Fixture Distro")
                self.assertTrue({"HARNESS_TEAM_CHECK/w", "HARNESS_TEAM_DISTRO/w", "EXISTING/u"}
                                <= set(exported.split(":")))
                self.assertEqual((self.home / "input").read_bytes(),
                                 (self.helper / "install-windows-command.ps1").read_bytes())
                self.assertTrue(self.link.samefile(self.mailbox / "team"))
                self.assertEqual(profile.read_bytes(), before)
                self.assertNotIn("private check error", result.stdout + result.stderr)

    def test_wsl_check_uses_the_checkout_validator_when_the_installed_copy_is_older(self):
        self.assertEqual(self.shell(self.script).returncode, 0)
        installed = self.helper / "install-windows-command.ps1"
        legacy = b"# Legacy installer without check mode\nexit 0\n"
        installed.write_bytes(legacy)
        powershell = self.tools / "powershell.exe"
        powershell.write_text('#!/bin/sh\n[ "$HARNESS_TEAM_CHECK" = 1 ] && cat > "$HOME/input"\n')
        powershell.chmod(0o755)
        self.env.update(WSL_DISTRO_NAME="Fixture Distro",
                        PATH=os.pathsep.join([str(self.link.parent), str(self.tools)]))

        result = self.shell(WORKSPACE / "install-team.sh", "--check", self.mailbox)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "input").read_bytes(),
                         (WORKSPACE / "copilot-mailbox/install-windows-command.ps1").read_bytes())
        self.assertEqual(installed.read_bytes(), legacy)
