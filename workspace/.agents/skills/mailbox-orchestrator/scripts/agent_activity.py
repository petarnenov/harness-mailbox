"""Seconds in which a project's agents actually worked on an issue, read from their session transcripts.

Three hosts write transcripts, one JSON line per event with its timestamp:
- Claude Code: ~/.claude/projects/<working directory with every non-alphanumeric character as '-'>/,
  its subagents under <session>/subagents/;
- GitHub Copilot CLI: ~/.copilot/session-state/<session>/events.jsonl, whose session.start names
  the working directory;
- Codex: ~/.codex/sessions/<year>/<month>/<day>/rollout-*.jsonl, whose session_meta names the
  working directory; a subagent is a rollout of its own.
Copilot and Codex sessions belong to the project whose registered directory is the longest
prefix of the session's working directory. The agent is working between two consecutive messages
when they are at most IDLE_LIMIT apart, or when the first one started a tool (a build or a test
run) and the next one followed within TOOL_LIMIT. A tool that only asks the user (WAITING_TOOLS)
is not a running tool. A permission prompt or a sleep cannot be told from one and counts as it. Any longer gap is waiting: for an answer,
a merge, or the next message. The sessions and subagents of a project are merged, so parallel
work counts once. When several issues of the same project are open at the same moment, that
moment is shared equally between them.

A project without any transcript of these hosts yields no activity.
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Optional

IDLE_LIMIT = 5 * 60
TOOL_LIMIT = 60 * 60
WAITING_TOOLS = ('AskUserQuestion', 'ExitPlanMode')
CONVERSATION = ('user', 'assistant')
COPILOT_MESSAGES = ('user.message', 'assistant.message')
COPILOT_WAITING_TOOLS = ('ask_user',)
CODEX_MESSAGE_ITEMS = ('message', 'function_call', 'custom_tool_call', 'function_call_output', 'custom_tool_call_output')
CODEX_TOOL_ITEMS = ('function_call', 'custom_tool_call')
CODEX_WAITING_TOOLS = ('request_user_input', 'request_user_input_async')


class Transcripts(NamedTuple):
    """Where each host keeps its session transcripts; None skips that host."""
    claude: Optional[Path] = None
    copilot: Optional[Path] = None
    codex: Optional[Path] = None


def default_transcripts():
    home = Path.home()
    return Transcripts(home / '.claude' / 'projects', home / '.copilot' / 'session-state',
                       home / '.codex' / 'sessions')


def as_transcripts(transcripts_root):
    """A bare folder is the Claude Code transcript folder alone."""
    return transcripts_root if isinstance(transcripts_root, Transcripts) else Transcripts(claude=transcripts_root)


def parse_time(text):
    try:
        moment = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo else None


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def runs_tool(entry):
    """True when the message started tools and none of them only waits for the user."""
    content = (entry.get('message') or {}).get('content')
    if entry.get('type') != 'assistant' or not isinstance(content, list):
        return False
    tools = [part.get('name') for part in content if isinstance(part, dict) and part.get('type') == 'tool_use']
    return bool(tools) and not any(name in WAITING_TOOLS for name in tools)


def conversation(path):
    """(timestamp, runs a tool) for every message of one transcript, in time order."""
    messages = []
    with open(path, errors='replace') as lines:
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            moment = parse_time(entry.get('timestamp')) if isinstance(entry, dict) else None
            if moment is not None and entry.get('type') in CONVERSATION:
                messages.append((moment, runs_tool(entry)))
    return sorted(messages)


def first_entry(path):
    try:
        with open(path, errors='replace') as lines:
            entry = json.loads(lines.readline())
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None


def copilot_messages(path):
    """(timestamp, runs a tool) for every message of one Copilot session, in time order."""
    messages = []
    with open(path, errors='replace') as lines:
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get('type') not in COPILOT_MESSAGES:
                continue
            moment = parse_time(entry.get('timestamp'))
            if moment is None:
                continue
            requests = (entry.get('data') or {}).get('toolRequests') or []
            names = [item.get('name') for item in requests if isinstance(item, dict)]
            messages.append((moment, bool(names) and not any(name in COPILOT_WAITING_TOOLS for name in names)))
    return sorted(messages)


def codex_messages(path):
    """(timestamp, runs a tool) for every message and tool step of one Codex rollout, in time order."""
    messages = []
    with open(path, errors='replace') as lines:
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            payload = entry.get('payload') if isinstance(entry, dict) else None
            if not isinstance(payload, dict) or entry.get('type') != 'response_item' \
                    or payload.get('type') not in CODEX_MESSAGE_ITEMS:
                continue
            moment = parse_time(entry.get('timestamp'))
            if moment is not None:
                runs = payload.get('type') in CODEX_TOOL_ITEMS and payload.get('name') not in CODEX_WAITING_TOOLS
                messages.append((moment, runs))
    return sorted(messages)


def periods_of(messages):
    return [(earlier, later) for (earlier, tool), (later, _) in zip(messages, messages[1:])
            if later - earlier <= (TOOL_LIMIT if tool else IDLE_LIMIT)]


def working_periods(path):
    return periods_of(conversation(path))


def merged(periods):
    result = []
    for opened, closed in sorted(periods):
        if result and opened <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], closed))
        else:
            result.append((opened, closed))
    return result


def transcript_folder(transcripts_root, directory):
    return transcripts_root / re.sub(r'[^A-Za-z0-9]', '-', str(Path(directory).expanduser()))


def project_directories(root, project):
    registry = read_json(root / 'projects.json') or {}
    for entry in registry.get('projects', []):
        if entry.get('id') == project:
            return {entry[key] for key in ('working_directory', 'repository_path') if entry.get(key)}
    return set()


def registered_directories(root):
    """{directory: project id} for every registered project."""
    registry = read_json(root / 'projects.json') or {}
    return {str(Path(entry[key]).expanduser()): entry.get('id')
            for entry in registry.get('projects', []) for key in ('working_directory', 'repository_path')
            if entry.get(key)}


def session_project(directories, cwd):
    """The project owning a session started in cwd: the longest registered directory containing it."""
    if not isinstance(cwd, str) or not cwd:
        return None
    best = None
    for directory in directories:
        if (cwd == directory or cwd.startswith(directory.rstrip('/') + '/')) \
                and (best is None or len(directory) > len(best)):
            best = directory
    return directories.get(best)


def copilot_sessions(copilot_root):
    """(working directory, events file) for every Copilot session."""
    for path in sorted(copilot_root.glob('*/events.jsonl')):
        context = ((first_entry(path) or {}).get('data') or {}).get('context') or {}
        yield context.get('cwd'), path


def codex_sessions(codex_root):
    """(working directory, rollout file) for every Codex session and subagent."""
    for path in sorted(codex_root.glob('**/rollout-*.jsonl')):
        yield ((first_entry(path) or {}).get('payload') or {}).get('cwd'), path


def project_activity(root, project, transcripts_root):
    sources = as_transcripts(transcripts_root)
    periods = []
    if sources.claude:
        for directory in project_directories(root, project):
            for path in transcript_folder(sources.claude, directory).glob('**/*.jsonl'):
                periods += working_periods(path)
    directories = registered_directories(root)
    for sessions, messages_of, folder in ((copilot_sessions, copilot_messages, sources.copilot),
                                          (codex_sessions, codex_messages, sources.codex)):
        if folder:
            for cwd, path in sessions(folder):
                if session_project(directories, cwd) == project:
                    periods += periods_of(messages_of(path))
    return merged(periods)


def task_projects(root, task_ids):
    return {path.parent.name for task_id in task_ids for path in root.glob(f'tasks/*/{task_id}.json')}


def shared_seconds(activity, own_window, open_windows):
    """Active time inside own_window, each moment divided by the open windows that contain it."""
    start, end = own_window
    cuts = {start, end, *(t for period in activity for t in period),
            *(t for window in open_windows for t in window)}
    cuts = sorted(t for t in cuts if start <= t <= end)
    total = 0.0
    for left, right in zip(cuts, cuts[1:]):
        middle = (left + right) / 2
        if any(opened <= middle < closed for opened, closed in activity):
            sharing = sum(1 for opened, closed in open_windows if opened <= middle < closed)
            total += (right - left) / max(1, sharing)
    return total


def active_seconds_by_project(root, task_ids, window, windows_of_project, transcripts_root):
    """Working seconds inside window for each project of the issue's tasks.

    windows_of_project(project) lists the (opened, closed) windows of every issue of that
    project, this one included, as POSIX timestamps.
    """
    return {project: shared_seconds(project_activity(root, project, transcripts_root), window,
                                    windows_of_project(project))
            for project in task_projects(root, task_ids)}
