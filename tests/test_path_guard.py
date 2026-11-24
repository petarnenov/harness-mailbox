"""Behavioral tests for the role-restricted PreToolUse guard, run as the hook runs it."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


GUARD = Path(__file__).resolve().parents[1] / '.claude/hooks/path-guard.py'
SCRATCHPAD = '/tmp/claude-1000/session/scratchpad'


def decide(role, tool_name, tool_input):
    result = subprocess.run([sys.executable, str(GUARD), role], text=True, capture_output=True, timeout=20,
                            input=json.dumps({'tool_name': tool_name, 'tool_input': tool_input}))
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    if not result.stdout.strip():
        return 'allow'
    output = json.loads(result.stdout)['hookSpecificOutput']
    if output['permissionDecision'] != 'deny':
        raise AssertionError(result.stdout)
    return 'deny'


def shell(role, command):
    return decide(role, 'Bash', {'command': command})


def edit(role, path):
    return decide(role, 'Edit', {'file_path': path})


class ReadonlyRoleTests(unittest.TestCase):
    def test_protect_plain_read_only_git_is_allowed(self):
        for command in ('git diff', 'git diff HEAD~1 -- server.py', 'git log', 'git log -5 --oneline',
                        'git show 193e1c8', 'git status --short', 'git rev-parse HEAD', 'git ls-files tests',
                        'git show 193e1c8:server.py', 'git diff HEAD~1 --stat',
                        'git log --since=2.weeks -- tests/test_x.py', 'git diff HEAD^ -- static/app.js'):
            with self.subTest(command=command):
                self.assertEqual('allow', shell('readonly', command))

    def test_output_option_hidden_by_shell_quoting_or_expansion_is_denied(self):
        for command in ("git diff --outpu''t=server.py", 'git diff --out"put"=server.py',
                        'git diff --outp\\ut=server.py', 'git diff --outpu{t,t}=x', "git diff $'--outp\\x75t=x'"):
            with self.subTest(command=command):
                self.assertEqual('deny', shell('readonly', command))

    def test_quotes_and_globs_are_refused_in_a_read_only_git_command(self):
        for command in ("git log --pretty='%h'", 'git log *'):
            with self.subTest(command=command):
                self.assertEqual('deny', shell('readonly', command))

    def test_protect_other_commands_and_chains_are_denied(self):
        for command in ('ls', 'git commit -m x', 'git log; rm x', 'git log | cat', 'git log > out.txt',
                        'git log && git status'):
            with self.subTest(command=command):
                self.assertEqual('deny', shell('readonly', command))

    def test_second_command_after_a_newline_is_denied(self):
        self.assertEqual('deny', shell('readonly', 'git log\nrm -rf tests'))
        self.assertEqual('deny', shell('readonly', 'git status\nwhoami'))

    def test_command_substitution_is_denied(self):
        self.assertEqual('deny', shell('readonly', 'git log $(touch marker)'))
        self.assertEqual('deny', shell('readonly', 'git log `touch marker`'))

    def test_diff_output_option_is_denied(self):
        self.assertEqual('deny', shell('readonly', 'git diff --output=changes.patch'))

    def test_every_edit_tool_is_denied(self):
        for tool, key in (('Edit', 'file_path'), ('Write', 'file_path'), ('MultiEdit', 'file_path'),
                          ('NotebookEdit', 'notebook_path')):
            with self.subTest(tool=tool):
                self.assertEqual('deny', decide('readonly', tool, {key: 'tests/test_x.py'}))


class WriterShellTests(unittest.TestCase):
    RUNNERS = ('make test', 'make check', 'python3 -m unittest discover -s tests',
               'node --test tests/js/app.test.mjs', 'git diff', 'git status', 'git log -3',
               'cd workspace && python3 -m unittest -v')

    def test_protect_plain_runners_are_allowed(self):
        for role in ('code', 'test'):
            for command in self.RUNNERS:
                with self.subTest(role=role, command=command):
                    self.assertEqual('allow', shell(role, command))

    def test_protect_plain_shell_writes_are_denied(self):
        for role in ('code', 'test'):
            for command in ('touch tests/x.py', 'rm -rf build', 'echo x > server.py', 'sed -i s/a/b/ server.py'):
                with self.subTest(role=role, command=command):
                    self.assertEqual('deny', shell(role, command))

    def test_write_after_a_newline_is_denied_behind_a_runner(self):
        for role in ('code', 'test'):
            for command in ('make test\ntouch tests/x.py', 'python3 -m unittest\nrm -rf tests',
                            'git status\nsed -i s/a/b/ server.py'):
                with self.subTest(role=role, command=command):
                    self.assertEqual('deny', shell(role, command))

    def test_write_inside_command_substitution_is_denied_behind_a_runner(self):
        for role in ('code', 'test'):
            for command in ('make test $(touch tests/x.py)', 'make test `touch tests/x.py`',
                            'python3 -m unittest $(rm -rf tests)'):
                with self.subTest(role=role, command=command):
                    self.assertEqual('deny', shell(role, command))

    def test_write_inside_process_substitution_is_denied_behind_a_runner(self):
        for role in ('code', 'test'):
            for command in ('make test <(touch tests/x.py)', 'python3 -m unittest <(rm -rf server.py)',
                            'cd <(touch${IFS}tests/x.py) && make test',
                            'cd $(touch${IFS}tests/x.py) && make test',
                            'cd `touch${IFS}tests/x.py` && make test'):
                with self.subTest(role=role, command=command):
                    self.assertEqual('deny', shell(role, command))

    def test_protect_cd_prefix_with_a_plain_directory_argument_is_allowed(self):
        for role in ('code', 'test'):
            for command in ('cd tests && python3 -m unittest test_x.py',
                            'cd ../x && python3 -m pytest',
                            'cd /tmp/claude-x && python3 test_x.py'):
                with self.subTest(role=role, command=command):
                    self.assertEqual('allow', shell(role, command))

    def test_patch_is_a_shell_write(self):
        for role in ('code', 'test'):
            for command in ('patch -p1 change.diff', 'patch -p1 < change.diff', 'patch server.py change.diff'):
                with self.subTest(role=role, command=command):
                    self.assertEqual('deny', shell(role, command))


class ScratchpadCopyTests(unittest.TestCase):
    def test_protect_test_role_copies_test_files_into_the_scratchpad(self):
        for command in (f'cp tests/test_x.py {SCRATCHPAD}/test_x.py',
                        f'git worktree add {SCRATCHPAD}/base 193e1c8',
                        f'git worktree remove {SCRATCHPAD}/base'):
            with self.subTest(command=command):
                self.assertEqual('allow', shell('test', command))

    def test_protect_copy_of_production_file_is_denied_for_test_role(self):
        self.assertEqual('deny', shell('test', f'cp server.py {SCRATCHPAD}/server.py'))

    def test_protect_code_role_may_not_copy(self):
        self.assertEqual('deny', shell('code', f'cp tests/test_x.py {SCRATCHPAD}/test_x.py'))

    def test_second_command_after_a_newline_is_denied_after_a_scratchpad_copy(self):
        self.assertEqual('deny', shell('test', f'cp tests/test_x.py {SCRATCHPAD}/test_x.py\nrm -rf server'))


class EditPathTests(unittest.TestCase):
    def test_protect_code_role_edits_production_and_not_test_files(self):
        self.assertEqual('allow', edit('code', 'server.py'))
        self.assertEqual('deny', edit('code', 'tests/test_x.py'))

    def test_protect_test_role_edits_test_files_and_scratchpad_only(self):
        self.assertEqual('allow', edit('test', 'tests/test_x.py'))
        self.assertEqual('allow', edit('test', f'{SCRATCHPAD}/notes.py'))
        self.assertEqual('deny', edit('test', 'server.py'))

    def test_code_role_may_edit_a_production_path_written_through_the_tests_directory(self):
        self.assertEqual('allow', edit('code', 'tests/../server.py'))

    def test_test_role_may_not_edit_a_production_path_written_through_the_tests_directory(self):
        self.assertEqual('deny', edit('test', 'tests/../server.py'))
        self.assertEqual('deny', edit('test', 'tests/js/../../static/app.js'))


if __name__ == '__main__':
    unittest.main()
