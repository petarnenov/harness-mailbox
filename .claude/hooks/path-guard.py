#!/usr/bin/env python3
"""PreToolUse guard for role-restricted subagents.

Usage: path-guard.py <role>   role in {code, test, readonly}
  code     : may edit only non-test source files
  test     : may edit only test files and the session scratchpad
  readonly : may not edit anything; shell is limited to read-only git
Bash commands that mutate files outside the build are denied for every role,
so the file-edit rules cannot be bypassed through the shell. The test role may
copy test files into a scratchpad worktree for red-at-base checks and run the
python and node test suites or the `make` verification targets; the scratchpad is Claude Code's per-user `/tmp/claude-*` root.
"""
import json
import re
import shlex
import sys

TEST_PATH = re.compile(r"((^|/)tests/|\.test\.[cm]?js$|_test\.py$|(^|/)test_[^/]+\.py$)")
SCRATCHPAD_PATH = re.compile(r"^/tmp/claude-")
SHELL_WRITE = re.compile(
    r"(sed\s+(-[a-zA-Z]*i|--in-place)|\btee\b|\bcp\b|\bmv\b|\brm\b|\btouch\b|\bmkdir\b|\bln\b|\bchmod\b|"
    r"\bdd\b|perl\s+-[a-zA-Z]*i|\bpython3?\b|\bnode\s+-e|"
    r"git\s+(checkout|reset|restore|commit|stash|apply|add|rm|mv|clean|switch|merge|rebase|cherry-pick|push|worktree)|"
    r"(^|[^0-9&>])>{1,2}\s*(?!/dev/null|&))"
)
CD_PREFIX = r"^\s*(cd\s+\S+\s*&&\s*)?"
MAKE_RUNNER = r"make\s+(?:test|lint|check|coverage|doctor|status|health)"
BUILD_RUNNER = re.compile(CD_PREFIX + r"(" + MAKE_RUNNER + r"|node\s+--(?:test|check)|sh\s+-n|git\s+(diff|log|show|status|rev-parse|ls-files))\b")
PYTHON_TEST_RUNNER = re.compile(CD_PREFIX + r"python3?\s+(-m\s+(pytest|unittest)\b|test_[^\s/]+\.py\b)")
READONLY_GIT = re.compile(r"^\s*git\s+(diff|log|show|status|rev-parse|ls-files)\b[^;|&>]*$")
SCRATCHPAD_COPY = re.compile(r"^\s*(cp\b|git\s+worktree\s+(add|remove|prune)\b)")
CHAINED = re.compile(r"[;|]|&&.*&&")
ANY_CHAIN = re.compile(r"[;|&>]")


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))
    sys.exit(0)


def is_test_path(path):
    return bool(TEST_PATH.search(path))


def is_scratchpad_path(path):
    return bool(SCRATCHPAD_PATH.match(path))


def body_after_cd(cmd):
    return re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", cmd)


def is_plain_runner(cmd):
    runner = BUILD_RUNNER.match(cmd) or PYTHON_TEST_RUNNER.match(cmd)
    return bool(runner) and not ANY_CHAIN.search(body_after_cd(cmd))


# `cp`/`git worktree` whose every path argument is a test file or lives in the scratchpad.
def is_scratchpad_copy(cmd):
    if not SCRATCHPAD_COPY.match(cmd) or ANY_CHAIN.search(cmd):
        return False
    try:
        words = shlex.split(cmd)
    except ValueError:
        return False
    paths = [w for w in words[1:] if not w.startswith("-") and ("/" in w or "." in w)]
    return bool(paths) and all(is_test_path(p) or is_scratchpad_path(p) for p in paths)


def guard_edit(role, path):
    if role == "readonly":
        deny("read-only reviewer: file edits are not allowed")
    if role == "code" and is_test_path(path):
        deny("code writer may not touch test files: " + path)
    if role == "test" and not (is_test_path(path) or is_scratchpad_path(path)):
        deny("test writer may touch only test files or the scratchpad: " + path)


def guard_shell(role, cmd):
    if role == "readonly":
        if not READONLY_GIT.match(cmd):
            deny("read-only reviewer: only read-only git (diff|log|show|status|rev-parse|ls-files) is allowed")
        return
    if not SHELL_WRITE.search(cmd):
        return
    if is_plain_runner(cmd):
        return
    if role == "test" and is_scratchpad_copy(cmd):
        return
    deny("shell commands that write files are not allowed; use Edit/Write: " + cmd[:200])


role = sys.argv[1]
event = json.load(sys.stdin)
tool = event.get("tool_name", "")
args = event.get("tool_input", {}) or {}

if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
    guard_edit(role, args.get("file_path") or args.get("notebook_path") or "")
elif tool == "Bash":
    guard_shell(role, args.get("command", ""))
sys.exit(0)
