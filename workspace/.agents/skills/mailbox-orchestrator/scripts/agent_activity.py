"""Seconds in which a project's agents actually worked on an issue, read from their session transcripts.

A Claude Code session writes one JSON line per message, with its timestamp, under
~/.claude/projects/<working directory with every non-alphanumeric character as '-'>/, and its
subagents under <session>/subagents/. The agent is working between two consecutive messages
when they are at most IDLE_LIMIT apart, or when the first one started a tool (a build or a test
run) and the next one followed within TOOL_LIMIT. A tool that only asks the user (WAITING_TOOLS)
is not a running tool. A permission prompt or a sleep cannot be told from one and counts as it. Any longer gap is waiting: for an answer,
a merge, or the next message. The sessions and subagents of a project are merged, so parallel
work counts once. When several issues of the same project are open at the same moment, that
moment is shared equally between them.

Only Claude Code writes these transcripts; a project without any yields no activity.
"""
import json
import re
from datetime import datetime
from pathlib import Path

IDLE_LIMIT = 5 * 60
TOOL_LIMIT = 60 * 60
WAITING_TOOLS = ('AskUserQuestion', 'ExitPlanMode')
CONVERSATION = ('user', 'assistant')


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


def working_periods(path):
    messages = conversation(path)
    return [(earlier, later) for (earlier, tool), (later, _) in zip(messages, messages[1:])
            if later - earlier <= (TOOL_LIMIT if tool else IDLE_LIMIT)]


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


def project_activity(root, project, transcripts_root):
    periods = []
    for directory in project_directories(root, project):
        for path in transcript_folder(transcripts_root, directory).glob('**/*.jsonl'):
            periods += working_periods(path)
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
